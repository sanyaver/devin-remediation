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
    config       = get_setup_config() or {}
    total        = len(sessions)
    completed    = sum(1 for s in sessions if s["status"] == "completed")
    running      = sum(1 for s in sessions if s["status"] == "dispatched")
    needs_review = sum(1 for s in sessions if s["status"] == "needs_review")
    prs_opened   = sum(1 for s in sessions if s.get("pr_url"))

    playbook_id  = config.get("playbook_id", "—")
    schedule_id  = config.get("schedule_id", "—")
    next_scan    = _next_monday_6am()

    rows = ""
    for s in sessions:
        cat_label, cat_color = _category_from_title(s.get("issue_title", ""))
        status     = s["status"]
        status_map = {
            "completed":    ('<span class="badge badge-green">Remediated</span>', ),
            "dispatched":   ('<span class="badge badge-yellow">Running</span>', ),
            "needs_review": ('<span class="badge badge-red">Needs Review</span>', ),
        }
        status_badge = status_map.get(status, (f'<span class="badge badge-gray">{status}</span>',))[0]
        pr_cell  = (f'<a href="{s["pr_url"]}" target="_blank" class="link">View PR →</a>'
                    if s.get("pr_url") else '<span class="muted">—</span>')
        duration = _format_duration(s.get("duration_seconds"))
        triggered = s["triggered_at"][:16].replace("T", " ") if s.get("triggered_at") else "—"

        rows += f"""
        <tr>
          <td><span class="issue-num">#{s['issue_number']}</span></td>
          <td><span class="cat-badge" style="background:{cat_color}22;color:{cat_color};border:1px solid {cat_color}44">{cat_label}</span></td>
          <td class="title-cell">{s['issue_title'][:55]}</td>
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
  .logo-mark {{
    width: 32px; height: 32px; border-radius: 8px;
    background: linear-gradient(135deg, #6366f1, #8b5cf6);
    display: flex; align-items: center; justify-content: center;
    font-size: 15px; font-weight: 700; color: white;
  }}
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
  .content {{ padding: 28px 32px; max-width: 1200px; }}

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

  /* ── Footer cards ── */
  .footer-grid {{
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 12px;
  }}
  .info-card {{
    background: #0d0e11;
    border: 1px solid #1e2130;
    border-radius: 12px;
    padding: 16px 20px;
  }}
  .info-card .card-title {{
    font-size: 11px; font-weight: 600;
    color: #64748b; text-transform: uppercase;
    letter-spacing: .05em; margin-bottom: 10px;
  }}
  .info-row {{
    display: flex; justify-content: space-between;
    font-size: 12px; color: #94a3b8;
    padding: 4px 0; border-bottom: 1px solid #111318;
  }}
  .info-row:last-child {{ border-bottom: none; }}
  .info-row .info-val {{ color: #e2e8f0; font-family: ui-monospace, monospace; font-size: 11px; }}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <div class="logo-mark">D</div>
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

  <!-- Footer cards -->
  <div class="footer-grid">
    <div class="info-card">
      <div class="card-title">Pipeline Configuration</div>
      <div class="info-row">
        <span>Playbook</span>
        <span class="info-val">!remediate · {playbook_id[:24] if playbook_id and playbook_id != '—' else '—'}…</span>
      </div>
      <div class="info-row">
        <span>Knowledge</span>
        <span class="info-val">superset-codebase-context</span>
      </div>
      <div class="info-row">
        <span>Schedule</span>
        <span class="info-val">Weekly · Mon 06:00 UTC</span>
      </div>
      <div class="info-row">
        <span>Trigger</span>
        <span class="info-val">Webhook · /webhook/scan</span>
      </div>
    </div>
    <div class="info-card">
      <div class="card-title">Devin API</div>
      <div class="info-row">
        <span>API version</span>
        <span class="info-val">v3 · org-scoped</span>
      </div>
      <div class="info-row">
        <span>Sessions endpoint</span>
        <span class="info-val">/organizations/{{org}}/sessions</span>
      </div>
      <div class="info-row">
        <span>Analytics</span>
        <span class="info-val"><a href="/api/metrics" target="_blank" class="link">View metrics →</a></span>
      </div>
      <div class="info-row">
        <span>Config</span>
        <span class="info-val"><a href="/api/config" target="_blank" class="link">View IDs →</a></span>
      </div>
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
