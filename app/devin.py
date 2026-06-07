import os, re, httpx
from dotenv import load_dotenv
load_dotenv()

API_KEY = os.getenv("DEVIN_API_KEY")
ORG_ID = os.getenv("DEVIN_ORG_ID")
BASE = "https://api.devin.ai/v3"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
REPO_URL = f"https://github.com/{os.getenv('GITHUB_REPO')}"

def build_prompt(issue_number: str, issue_title: str, issue_body: str) -> str:
    return f"""You are working in the repository: {REPO_URL}

## Task
{issue_title}

## Issue Details
{issue_body}

## Success Criteria
- All existing tests must still pass after your changes
- Run the relevant test suite with pytest before opening a PR
- If you modify any requirements file, verify pip install succeeds
- Open a PR with "Fixes #{issue_number}" in the description

## Constraints
- Do not modify files outside the scope of this fix
- Follow existing code style — check neighboring code for patterns
- If the fix is ambiguous, explain your reasoning in the PR description

## Verification Steps
1. Make the required code changes
2. Run the relevant tests: pytest
3. Confirm no regressions introduced
4. Open a pull request targeting the main branch
"""

def create_session(issue_number: str, issue_title: str, issue_body: str) -> dict:
    prompt = build_prompt(issue_number, issue_title, issue_body)
    r = httpx.post(
        f"{BASE}/organizations/{ORG_ID}/sessions",
        headers=HEADERS,
        json={
            "prompt": prompt,
            "tags": [f"issue-{issue_number}", "security-remediation", "automated"]
        },
        timeout=30
    )
    r.raise_for_status()
    return r.json()

def get_session(session_id: str) -> dict:
    r = httpx.get(
        f"{BASE}/organizations/{ORG_ID}/sessions/{session_id}",
        headers=HEADERS,
        timeout=30
    )
    r.raise_for_status()
    return r.json()

def get_session_messages(session_id: str) -> list:
    r = httpx.get(
        f"{BASE}/organizations/{ORG_ID}/sessions/{session_id}/messages",
        headers=HEADERS,
        timeout=30
    )
    r.raise_for_status()
    return r.json().get("items", [])

def extract_pr_url(messages: list) -> str | None:
    pattern = r'https://github\.com/[^\s\)>\"]+/pull/\d+'
    for msg in reversed(messages):
        content = msg.get("message", "")
        if isinstance(content, str):
            match = re.search(pattern, content)
            if match:
                return match.group(0)
    return None
