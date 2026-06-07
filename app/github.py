import os, httpx
from dotenv import load_dotenv
load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

def get_issue(issue_number: str) -> dict:
    r = httpx.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_number}",
        headers=HEADERS, timeout=15
    )
    r.raise_for_status()
    return r.json()

def comment_on_issue(issue_number: str, body: str):
    r = httpx.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_number}/comments",
        headers=HEADERS,
        json={"body": body},
        timeout=15
    )
    r.raise_for_status()
    return r.json()

def post_dispatched_comment(issue_number: str, session_id: str, devin_url: str):
    comment_on_issue(issue_number, f"""🤖 **Devin dispatched**

A Devin session has been automatically created to remediate this issue.

| Field | Value |
|---|---|
| Session ID | `{session_id}` |
| Devin session | [View live]({devin_url}) |
| Status | 🟡 Running |

_This issue was automatically triaged by the security remediation pipeline._""")

def post_completed_comment(issue_number: str, pr_url: str, duration: int):
    mins = duration // 60
    secs = duration % 60
    comment_on_issue(issue_number, f"""✅ **Devin completed — PR ready for review**

| Field | Value |
|---|---|
| Pull Request | {pr_url} |
| Time to remediate | {mins}m {secs}s |
| Status | ✅ Complete |

_Review and merge the PR to close this issue._""")

def post_failed_comment(issue_number: str, session_id: str):
    comment_on_issue(issue_number, f"""⚠️ **Devin needs human review**

Session `{session_id}` was unable to complete this remediation automatically.

This issue has been flagged for engineer review — the session complexity exceeded what could be automated confidently. This is expected for ~30% of findings and is the correct behavior.

[View session](https://app.devin.ai/sessions/{session_id}) for details.""")
