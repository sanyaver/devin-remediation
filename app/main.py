import os, asyncio, logging, httpx
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

from database import init_db, insert_session, update_session, get_all_sessions
from devin import create_session, get_session, get_session_messages, extract_pr_url
from github import get_issue, post_dispatched_comment, post_completed_comment, post_failed_comment

TERMINAL_STATUSES = {"stopped", "error", "suspended", "finished", "completed"}
POLL_INTERVAL = 30
active_polls = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("DB initialized")
    yield

app = FastAPI(lifespan=lifespan)

async def poll_session(issue_number: str, session_id: str, started_at: datetime):
    """Background task: polls Devin until completion, then updates DB and GitHub."""
    log.info(f"Polling session {session_id} for issue #{issue_number}")
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            data = get_session(session_id)
            status = data.get("status", "unknown")
            log.info(f"Session {session_id} status: {status}")

            # Check pull_requests even while running — Devin keeps sessions open after opening a PR
            prs = data.get("pull_requests") or []
            pr_url = prs[0].get("url") if prs and isinstance(prs[0], dict) else (prs[0] if prs else None)
            if not pr_url:
                messages = get_session_messages(session_id)
                pr_url = extract_pr_url(messages)

            if pr_url or status in TERMINAL_STATUSES:
                duration = int((datetime.now(timezone.utc) - started_at).total_seconds())

                if pr_url:
                    update_session(session_id, "completed", pr_url,
                                   datetime.now(timezone.utc).isoformat(), duration)
                    post_completed_comment(issue_number, pr_url, duration)
                    log.info(f"Issue #{issue_number} resolved → {pr_url}")
                else:
                    update_session(session_id, "needs_review", None,
                                   datetime.now(timezone.utc).isoformat(), duration)
                    post_failed_comment(issue_number, session_id)
                    log.warning(f"Issue #{issue_number} needs human review")
                break
        except Exception as e:
            log.error(f"Poll error for {session_id}: {e}")

@app.post("/webhook/scan")
async def receive_scan(request: Request):
    payload = await request.json()
    findings = payload.get("findings", [])
    dispatched = []

    for finding in findings:
        issue_number = str(finding["issue_number"])
        try:
            issue = get_issue(issue_number)
            title = issue["title"]
            body = issue.get("body", "")

            session = create_session(issue_number, title, body)
            session_id = session["session_id"]
            devin_url = session.get("url", f"https://app.devin.ai/sessions/{session_id}")

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
    """Demo endpoint — fires the hardcoded 3 issues through the webhook."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "http://localhost:8000/webhook/scan",
            json={"findings": [
                {"issue_number": 1},
                {"issue_number": 2},
                {"issue_number": 3},
            ]},
            timeout=30
        )
    return r.json()

@app.get("/api/sessions")
async def api_sessions():
    return get_all_sessions()

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    sessions = get_all_sessions()
    total = len(sessions)
    completed = sum(1 for s in sessions if s["status"] == "completed")
    running = sum(1 for s in sessions if s["status"] == "dispatched")
    needs_review = sum(1 for s in sessions if s["status"] == "needs_review")

    rows = ""
    for s in sessions:
        status_icon = {"completed": "✅", "dispatched": "🟡", "needs_review": "⚠️"}.get(s["status"], "❓")
        pr_cell = f'<a href="{s["pr_url"]}" target="_blank">View PR →</a>' if s["pr_url"] else "—"
        duration = f'{s["duration_seconds"]}s' if s["duration_seconds"] else "running..."
        rows += f"""<tr>
          <td>#{s['issue_number']}</td>
          <td>{s['issue_title'][:50]}</td>
          <td>{status_icon} {s['status']}</td>
          <td><a href="{s['devin_url']}" target="_blank">Session →</a></td>
          <td>{pr_cell}</td>
          <td>{duration}</td>
          <td>{s['triggered_at'][:16] if s['triggered_at'] else ''}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html><head>
<title>Devin Remediation Dashboard</title>
<meta http-equiv="refresh" content="20">
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 0; padding: 24px; background: #0f0f10; color: #e2e8f0; }}
  h1 {{ font-size: 20px; font-weight: 600; margin-bottom: 4px; }}
  .subtitle {{ color: #94a3b8; font-size: 13px; margin-bottom: 24px; }}
  .metrics {{ display: flex; gap: 16px; margin-bottom: 24px; }}
  .metric {{ background: #1e1e2e; border: 1px solid #2d2d3d; border-radius: 10px; padding: 16px 24px; }}
  .metric .value {{ font-size: 32px; font-weight: 700; }}
  .metric .label {{ font-size: 12px; color: #94a3b8; margin-top: 2px; }}
  .completed {{ color: #4ade80; }} .running {{ color: #fbbf24; }} .review {{ color: #f87171; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 10px 12px; color: #94a3b8; border-bottom: 1px solid #2d2d3d; font-weight: 500; }}
  td {{ padding: 12px; border-bottom: 1px solid #1e1e2e; }}
  tr:hover td {{ background: #1e1e2e; }}
  a {{ color: #818cf8; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .trigger-btn {{ background: #4f46e5; color: white; border: none; padding: 10px 20px;
    border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 500; margin-bottom: 24px; }}
  .trigger-btn:hover {{ background: #4338ca; }}
  .auto-refresh {{ font-size: 11px; color: #64748b; margin-top: 12px; }}
</style>
</head><body>
<h1>Devin Security Remediation</h1>
<p class="subtitle">Autonomous vulnerability remediation pipeline · Auto-refreshes every 20s</p>

<div class="metrics">
  <div class="metric"><div class="value">{total}</div><div class="label">Total findings</div></div>
  <div class="metric"><div class="value completed">{completed}</div><div class="label">Remediated</div></div>
  <div class="metric"><div class="value running">{running}</div><div class="label">In progress</div></div>
  <div class="metric"><div class="value review">{needs_review}</div><div class="label">Needs human review</div></div>
</div>

<button class="trigger-btn" onclick="triggerScan()">&#9654; Trigger Security Scan</button>

<table>
  <thead><tr>
    <th>Issue</th><th>Title</th><th>Status</th>
    <th>Devin session</th><th>Pull request</th><th>Duration</th><th>Triggered</th>
  </tr></thead>
  <tbody>{rows if rows else '<tr><td colspan="7" style="color:#64748b;text-align:center;padding:40px">No sessions yet — trigger a scan to begin</td></tr>'}</tbody>
</table>
<p class="auto-refresh">&#8635; Auto-refreshing every 20 seconds</p>

<script>
async function triggerScan() {{
  const btn = document.querySelector('.trigger-btn');
  btn.textContent = 'Dispatching...';
  btn.disabled = true;
  const resp = await fetch('/scan/trigger', {{method: 'POST'}});
  const data = await resp.json();
  btn.textContent = `${{data.count}} sessions dispatched`;
  setTimeout(() => location.reload(), 2000);
}}
</script>
</body></html>"""
