import os, asyncio, logging, httpx, time
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

from database import init_db, insert_session, update_session, get_all_sessions, get_setup_config
from devin import (
    create_session, get_session, get_session_messages, extract_pr_url,
    generate_session_insights, get_session_insights, get_org_metrics,
)
from github import get_issue, post_dispatched_comment, post_completed_comment, post_failed_comment
from setup_devin import setup_devin

TERMINAL_STATUSES = {"exit", "error", "suspended"}
POLL_INTERVAL     = 30
active_polls: dict = {}
devin_config: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("DB initialised")
    global devin_config
    devin_config = setup_devin()
    yield

app = FastAPI(lifespan=lifespan)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_duration(secs: int | None) -> str:
    if not secs:
        return "running…"
    m, s = divmod(secs, 60)
    if m >= 60:
        return f"{m // 60}h {m % 60}m"
    return f"{m}m {s}s"

def _category_from_title(title: str) -> tuple[str, str]:
    t = title.upper()
    if "[SECURITY]" in t: return "SECURITY", "#ef4444"
    if "[BUG]"      in t: return "BUG",      "#f97316"
    if "[PERF]"     in t: return "PERF",      "#3b82f6"
    if "[TEST]"     in t: return "TEST",      "#8b5cf6"
    return "OTHER", "#64748b"

def _next_monday_6am() -> str:
    now  = datetime.now(timezone.utc)
    days = (7 - now.weekday()) % 7 or 7
    nxt  = (now + timedelta(days=days)).replace(hour=6, minute=0, second=0, microsecond=0)
    return nxt.strftime("%a %b %d, %Y · 06:00 UTC")


# ── Poll loop ─────────────────────────────────────────────────────────────────

async def poll_session(issue_number: str, session_id: str, started_at: datetime):
    log.info(f"Polling {session_id} for issue #{issue_number}")
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            data   = get_session(session_id)
            status = data.get("status", "unknown")
            log.info(f"Session {session_id}: {status}")

            prs    = data.get("pull_requests") or []
            pr_url = None
            if prs:
                first = prs[0]
                pr_url = (first.get("pr_url") or first.get("url")
                          if isinstance(first, dict) else first)
            if not pr_url:
                pr_url = extract_pr_url(get_session_messages(session_id))

            if pr_url or status in TERMINAL_STATUSES:
                duration = int((datetime.now(timezone.utc) - started_at).total_seconds())
                if pr_url:
                    update_session(session_id, "completed", pr_url,
                                   datetime.now(timezone.utc).isoformat(), duration)
                    post_completed_comment(issue_number, pr_url, duration)
                    log.info(f"Issue #{issue_number} → {pr_url}")
                    # Trigger Devin insights generation (async, best-effort)
                    generate_session_insights(session_id)
                    asyncio.create_task(_fetch_insights(session_id))
                else:
                    update_session(session_id, "needs_review", None,
                                   datetime.now(timezone.utc).isoformat(), duration)
                    post_failed_comment(issue_number, session_id)
                    log.warning(f"Issue #{issue_number} needs human review")
                break
        except Exception as e:
            log.error(f"Poll error for {session_id}: {e}")

async def _fetch_insights(session_id: str):
    """Wait for insights to be ready, then store the category."""
    await asyncio.sleep(60)
    try:
        insights  = get_session_insights(session_id)
        category  = insights.get("category") or insights.get("task_category") or ""
        if category:
            update_session(session_id, "completed", category=category)
            log.info(f"Insights stored for {session_id}: {category}")
    except Exception as e:
        log.warning(f"Insights fetch failed for {session_id}: {e}")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.post("/webhook/scan")
async def receive_scan(request: Request):
    payload    = await request.json()
    findings   = payload.get("findings", [])
    dispatched = []

    playbook_id  = devin_config.get("playbook_id")
    knowledge_id = devin_config.get("knowledge_id")
    knowledge_ids = [knowledge_id] if knowledge_id else None

    for finding in findings:
        issue_number = str(finding["issue_number"])
        try:
            issue   = get_issue(issue_number)
            title   = issue["title"]
            body    = issue.get("body", "")

            session    = create_session(issue_number, title, body,
                                        playbook_id=playbook_id,
                                        knowledge_ids=knowledge_ids)
            session_id = session["session_id"]
            devin_url  = session.get("url", f"https://app.devin.ai/sessions/{session_id}")

            insert_session(issue_number, title, session_id, devin_url)
            post_dispatched_comment(issue_number, session_id, devin_url)

            started_at = datetime.now(timezone.utc)
            task = asyncio.create_task(poll_session(issue_number, session_id, started_at))
            active_polls[session_id] = task

            dispatched.append({"issue": issue_number, "session_id": session_id})
            log.info(f"Dispatched Devin for issue #{issue_number} → {session_id}")

        except Exception as e:
            log.error(f"Failed to dispatch for issue #{issue_number}: {e}")

    return {"dispatched": dispatched, "count": len(dispatched)}

@app.post("/scan/trigger")
async def trigger_scan():
    """Demo trigger — fires all four issues through the scan webhook."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "http://localhost:8000/webhook/scan",
            json={"findings": [
                {"issue_number": 1},
                {"issue_number": 2},
                {"issue_number": 3},
                {"issue_number": 7},
            ]},
            timeout=30,
        )
    return r.json()

@app.get("/api/sessions")
async def api_sessions():
    return get_all_sessions()

@app.get("/api/metrics")
async def api_metrics():
    now        = int(time.time())
    time_after = now - 30 * 24 * 3600   # last 30 days
    return get_org_metrics(time_after, now)

@app.get("/api/config")
async def api_config():
    return get_setup_config() or {}


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    sessions     = get_all_sessions()
    total        = len(sessions)
    completed    = sum(1 for s in sessions if s["status"] == "completed")
    running      = sum(1 for s in sessions if s["status"] == "dispatched")
    needs_review = sum(1 for s in sessions if s["status"] == "needs_review")
    prs_opened   = sum(1 for s in sessions if s.get("pr_url"))
    next_scan    = _next_monday_6am()

    # Devin analytics API — best-effort, 30-day window
    now = int(time.time())
    try:
        metrics      = get_org_metrics(now - 30 * 86400, now)
        sm           = metrics.get("sessions") or {}
        pm           = metrics.get("prs") or {}
        api_sessions = sm.get("sessions_created_count", "—")
        api_prs      = pm.get("prs_created_count", "—")
        api_merged   = pm.get("prs_merged_count", "—")
        origin       = sm.get("sessions_created_by_origin") or {}
        total_s      = sm.get("sessions_created_count") or 1
        api_pct      = f"{int(origin.get('api', 0) / total_s * 100)}%" if total_s else "—"
    except Exception:
        api_sessions = api_prs = api_merged = api_pct = "—"

    rows = ""
    for s in sessions:
        cat_label, cat_color = _category_from_title(s.get("issue_title", ""))
        status     = s["status"]
        status_badges = {
            "completed":    '<span class="badge badge-green">Remediated</span>',
            "dispatched":   '<span class="badge badge-yellow">Running</span>',
            "needs_review": '<span class="badge badge-red">Needs Review</span>',
        }
        status_badge = status_badges.get(status, f'<span class="badge badge-gray">{status}</span>')
        pr_cell  = (f'<a href="{s["pr_url"]}" target="_blank" class="link">View PR →</a>'
                    if s.get("pr_url") else '<span class="muted">—</span>')
        duration  = _format_duration(s.get("duration_seconds"))
        triggered = s["triggered_at"][:16].replace("T", " ") if s.get("triggered_at") else "—"

        rows += f"""
        <tr>
          <td><span class="issue-num">#{s['issue_number']}</span></td>
          <td><span class="cat-badge" style="background:{cat_color}22;color:{cat_color};border:1px solid {cat_color}44">{cat_label}</span></td>
          <td class="title-cell">{s['issue_title'][:60]}</td>
          <td>{status_badge}</td>
          <td><a href="{s['devin_url']}" target="_blank" class="link">Session →</a></td>
          <td>{pr_cell}</td>
          <td class="mono">{duration}</td>
          <td class="muted mono">{triggered}</td>
        </tr>"""

    empty_row = ('<tr><td colspan="8" class="empty-state">'
                 'No sessions yet — trigger a scan to begin</td></tr>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Devin Remediation Pipeline</title>
<meta http-equiv="refresh" content="20">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: #08090a;
    color: #e2e8f0;
    min-height: 100vh;
  }}

  /* ── Header ── */
  .header {{
    border-bottom: 1px solid #1e2130;
    padding: 20px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: #0d0e11;
  }}
  .header-left {{ display: flex; align-items: center; gap: 12px; }}
  .logo-img {{ width: 32px; height: 32px; border-radius: 6px; }}
  .header-title {{ font-size: 15px; font-weight: 600; color: #f1f5f9; }}
  .header-sub {{ font-size: 12px; color: #64748b; margin-top: 1px; }}
  .header-right {{ display: flex; align-items: center; gap: 8px; }}
  .schedule-pill {{
    font-size: 11px; color: #94a3b8;
    background: #1a1d24; border: 1px solid #2d3140;
    border-radius: 20px; padding: 5px 12px;
    display: flex; align-items: center; gap: 6px;
  }}
  .dot-pulse {{
    width: 6px; height: 6px; border-radius: 50%;
    background: #4ade80; box-shadow: 0 0 0 0 rgba(74,222,128,.4);
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{
    0%   {{ box-shadow: 0 0 0 0 rgba(74,222,128,.4); }}
    70%  {{ box-shadow: 0 0 0 6px rgba(74,222,128,0); }}
    100% {{ box-shadow: 0 0 0 0 rgba(74,222,128,0); }}
  }}

  /* ── Content ── */
  .content {{ padding: 28px 32px; max-width: 1200px; margin: 0 auto; }}
  .header-inner {{ max-width: 1200px; margin: 0 auto; display: flex; align-items: center; justify-content: space-between; width: 100%; }}

  /* ── Stat bar ── */
  .stat-bar {{
    font-size: 12px; color: #64748b;
    margin-bottom: 24px;
    display: flex; align-items: center; gap: 8px;
  }}
  .stat-bar .num {{ color: #e2e8f0; font-weight: 600; }}
  .stat-bar .arrow {{ color: #334155; }}

  /* ── Metrics ── */
  .metrics {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 12px;
    margin-bottom: 24px;
  }}
  .metric {{
    background: #0d0e11;
    border: 1px solid #1e2130;
    border-radius: 12px;
    padding: 16px 20px;
  }}
  .metric .val {{
    font-size: 28px; font-weight: 700; line-height: 1;
    margin-bottom: 4px;
  }}
  .metric .lbl {{
    font-size: 11px; color: #64748b; text-transform: uppercase;
    letter-spacing: .04em;
  }}
  .val-white    {{ color: #f1f5f9; }}
  .val-green    {{ color: #4ade80; }}
  .val-yellow   {{ color: #fbbf24; }}
  .val-red      {{ color: #f87171; }}
  .val-indigo   {{ color: #818cf8; }}

  /* ── Actions ── */
  .actions {{ display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }}
  .btn-primary {{
    background: #4f46e5;
    color: white; border: none;
    padding: 10px 20px; border-radius: 8px;
    cursor: pointer; font-size: 13px; font-weight: 500;
    display: flex; align-items: center; gap: 8px;
    transition: background .15s;
  }}
  .btn-primary:hover {{ background: #4338ca; }}
  .btn-primary:disabled {{ background: #2d2f3e; color: #64748b; cursor: default; }}
  .refresh-note {{ font-size: 11px; color: #334155; }}

  /* ── Table ── */
  .table-wrap {{
    border: 1px solid #1e2130;
    border-radius: 12px;
    overflow: hidden;
    margin-bottom: 24px;
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  thead {{ background: #0d0e11; }}
  th {{
    text-align: left; padding: 11px 14px;
    font-size: 11px; font-weight: 500;
    color: #475569; text-transform: uppercase;
    letter-spacing: .05em;
    border-bottom: 1px solid #1e2130;
  }}
  td {{ padding: 13px 14px; border-bottom: 1px solid #111318; vertical-align: middle; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover td {{ background: #0d0e11; }}

  .issue-num {{ font-weight: 600; color: #94a3b8; font-size: 12px; }}
  .title-cell {{ color: #cbd5e1; max-width: 280px; }}
  .mono {{ font-family: ui-monospace, "SF Mono", monospace; font-size: 12px; }}
  .muted {{ color: #475569; }}
  .link {{ color: #818cf8; text-decoration: none; }}
  .link:hover {{ text-decoration: underline; }}
  .empty-state {{ text-align: center; padding: 48px; color: #334155; font-size: 13px; }}

  /* ── Badges ── */
  .badge {{
    font-size: 11px; font-weight: 500;
    padding: 3px 9px; border-radius: 20px;
    display: inline-block;
  }}
  .badge-green  {{ background: #052e16; color: #4ade80; border: 1px solid #14532d; }}
  .badge-yellow {{ background: #1c1400; color: #fbbf24; border: 1px solid #713f12; }}
  .badge-red    {{ background: #1a0505; color: #f87171; border: 1px solid #7f1d1d; }}
  .badge-gray   {{ background: #1a1d24; color: #94a3b8; border: 1px solid #2d3140; }}
  .cat-badge {{
    font-size: 10px; font-weight: 600;
    padding: 2px 8px; border-radius: 4px;
    letter-spacing: .04em;
    display: inline-block;
  }}

  /* ── Analytics strip ── */
  .analytics-strip {{
    background: #0d0e11;
    border: 1px solid #1e2130;
    border-radius: 12px;
    padding: 18px 24px;
  }}
  .strip-label {{
    font-size: 10px; font-weight: 600;
    color: #475569; text-transform: uppercase;
    letter-spacing: .06em; margin-bottom: 14px;
  }}
  .strip-stats {{
    display: flex; align-items: center; gap: 0;
    margin-bottom: 14px;
  }}
  .strip-stat {{ flex: 1; }}
  .strip-val {{ font-size: 22px; font-weight: 700; color: #f1f5f9; line-height: 1; }}
  .strip-key {{ font-size: 11px; color: #64748b; margin-top: 3px; }}
  .strip-divider {{
    width: 1px; height: 36px;
    background: #1e2130; margin: 0 24px; flex-shrink: 0;
  }}
  .strip-meta {{
    font-size: 11px; color: #475569;
    border-top: 1px solid #1e2130; padding-top: 12px;
  }}
  .strip-meta code {{
    font-family: ui-monospace, monospace;
    color: #94a3b8; font-size: 11px;
    background: #1a1d24; padding: 1px 5px; border-radius: 3px;
  }}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="header-inner">
    <div class="header-left">
      <img class="logo-img" src="data:image/png;base64,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" alt="Devin">
      <div>
        <div class="header-title">Devin Remediation Pipeline</div>
        <div class="header-sub">Autonomous code health · Apache Superset</div>
      </div>
    </div>
    <div class="header-right">
      <div class="schedule-pill">
        <div class="dot-pulse"></div>
        Next scan: {next_scan}
      </div>
    </div>
  </div>
</div>

<!-- Content -->
<div class="content">

  <!-- Stat bar -->
  <div class="stat-bar">
    <span><span class="num">{total}</span> findings</span>
    <span class="arrow">→</span>
    <span><span class="num">{total}</span> Devin sessions dispatched</span>
    <span class="arrow">→</span>
    <span><span class="num">{prs_opened}</span> PRs opened</span>
    <span class="arrow">→</span>
    <span><span class="num">0</span> engineer-hours</span>
  </div>

  <!-- Metrics -->
  <div class="metrics">
    <div class="metric">
      <div class="val val-white">{total}</div>
      <div class="lbl">Findings</div>
    </div>
    <div class="metric">
      <div class="val val-green">{completed}</div>
      <div class="lbl">Remediated</div>
    </div>
    <div class="metric">
      <div class="val val-yellow">{running}</div>
      <div class="lbl">In Progress</div>
    </div>
    <div class="metric">
      <div class="val val-red">{needs_review}</div>
      <div class="lbl">Needs Review</div>
    </div>
    <div class="metric">
      <div class="val val-indigo">{prs_opened}</div>
      <div class="lbl">PRs Opened</div>
    </div>
  </div>

  <!-- Actions -->
  <div class="actions">
    <button class="btn-primary" onclick="triggerScan()" id="scan-btn">
      ▶ &nbsp;Run Security Scan
    </button>
    <span class="refresh-note">↻ Auto-refreshing every 20s</span>
  </div>

  <!-- Table -->
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Issue</th>
          <th>Category</th>
          <th>Title</th>
          <th>Status</th>
          <th>Devin Session</th>
          <th>Pull Request</th>
          <th>Duration</th>
          <th>Triggered</th>
        </tr>
      </thead>
      <tbody>
        {rows if rows else empty_row}
      </tbody>
    </table>
  </div>

  <!-- Analytics strip (Devin API) -->
  <div class="analytics-strip">
    <div class="strip-label">Devin API · 30-day analytics</div>
    <div class="strip-stats">
      <div class="strip-stat">
        <div class="strip-val">{api_sessions}</div>
        <div class="strip-key">Sessions via API</div>
      </div>
      <div class="strip-divider"></div>
      <div class="strip-stat">
        <div class="strip-val">{api_prs}</div>
        <div class="strip-key">PRs created</div>
      </div>
      <div class="strip-divider"></div>
      <div class="strip-stat">
        <div class="strip-val">{api_merged}</div>
        <div class="strip-key">PRs merged</div>
      </div>
      <div class="strip-divider"></div>
      <div class="strip-stat">
        <div class="strip-val">{api_pct}</div>
        <div class="strip-key">API-triggered</div>
      </div>
    </div>
    <div class="strip-meta">
      Playbook&nbsp;<code>!remediate</code>&nbsp;·&nbsp;
      Knowledge&nbsp;<code>superset-codebase-context</code>&nbsp;·&nbsp;
      Schedule&nbsp;<code>Mon 06:00 UTC</code>&nbsp;·&nbsp;
      API&nbsp;<code>v3&nbsp;org-scoped</code>
    </div>
  </div>

</div>

<script>
async function triggerScan() {{
  const btn = document.getElementById('scan-btn');
  btn.textContent = 'Dispatching…';
  btn.disabled = true;
  try {{
    const resp = await fetch('/scan/trigger', {{ method: 'POST' }});
    const data = await resp.json();
    btn.textContent = `${{data.count}} sessions dispatched`;
    setTimeout(() => location.reload(), 2500);
  }} catch (e) {{
    btn.textContent = 'Error — check logs';
    btn.disabled = false;
  }}
}}
</script>
</body>
</html>"""
