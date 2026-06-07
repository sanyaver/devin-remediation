import os, re, httpx
from dotenv import load_dotenv
load_dotenv()

API_KEY = os.getenv("DEVIN_API_KEY")
ORG_ID  = os.getenv("DEVIN_ORG_ID")
BASE    = "https://api.devin.ai/v3"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
REPO_URL = f"https://github.com/{os.getenv('GITHUB_REPO')}"

# ── Playbook ──────────────────────────────────────────────────────────────────

PLAYBOOK_TITLE = "Security Remediation — Standard Procedure"
PLAYBOOK_BODY  = """\
## Procedure

1. Read the GitHub issue in full before touching any file
2. Identify the specific file(s) and function(s) involved
3. Make the minimal change that resolves the issue — do not refactor beyond scope
4. If you modify a requirements file, run: pip install -e .
5. Run the relevant test suite: pytest tests/unit_tests/ -x -q
6. If tests fail, diagnose and fix before proceeding — never open a PR with failing tests
7. Open a PR targeting the main branch with "Fixes #<issue_number>" in the description

## Specifications

- All pre-existing tests must pass after your changes
- PR description must reference the issue with "Fixes #N"
- Changes must be scoped to files directly relevant to the stated issue

## Advice

- Check neighbouring code for style patterns before editing
- For type-related fixes, use pandas.api.types — not isinstance() — when working with DataFrame columns
- For performance fixes, prefer @functools.lru_cache or class-level caching over module-level dicts
- For test additions, mirror the structure of existing files in tests/unit_tests/

## Forbidden Actions

- Do not modify files outside the direct scope of this issue
- Do not open a PR if any tests are failing
- Do not add new dependencies unless the issue explicitly requires it
"""

# ── Knowledge ─────────────────────────────────────────────────────────────────

KNOWLEDGE_NAME    = "superset-codebase-context"
KNOWLEDGE_TRIGGER = "when working in the sanyaver/superset repository"
KNOWLEDGE_BODY    = """\
Repository: https://github.com/sanyaver/superset
(Fork of Apache Superset — open-source BI and data visualisation platform)

Test commands:
  Unit tests:       pytest tests/unit_tests/ -x -q
  Single module:    pytest tests/unit_tests/utils/test_core.py -x -q

Key file locations:
  Utility functions:  superset/utils/core.py
  DB engine specs:    superset/db_engine_specs/base.py
  Dependencies:       setup.py (install_requires) and requirements/base.txt

Code conventions:
  - Python 3.9+ type annotations throughout
  - Use pandas.api.types for DataFrame column type checking — not isinstance()
  - Use functools.lru_cache for memoization on pure functions
  - black formatting enforced — run: black superset/ before committing
  - bleach is used for HTML sanitisation in chart/dashboard rendering

When fixing dependency versions:
  - Check both setup.py install_requires AND requirements/base.txt
  - Verify with: pip install -e .
"""

# ── Schedule ──────────────────────────────────────────────────────────────────

SCHEDULE_NAME   = "Weekly Security Scan — Superset"
SCHEDULE_PROMPT = """\
Scan the sanyaver/superset repository for new issues in these categories:
1. [SECURITY] security vulnerabilities — outdated dependencies with known CVEs,
   unsafe HTML rendering, SQL injection risks
2. [PERF] performance regressions — functions that recompute on every request
   with no caching
3. [TEST] test coverage gaps — utility functions or modules with no unit tests
4. [BUG] correctness bugs — type misclassifications, wrong return values,
   unhandled edge cases

For each finding, check whether a GitHub issue already exists in sanyaver/superset.
If not, open a new issue with the appropriate label prefix and a description that
includes: the affected file and function, the problem, and the recommended fix.
"""


# ── API helpers ───────────────────────────────────────────────────────────────

def _get(path: str, **kwargs) -> dict:
    r = httpx.get(f"{BASE}{path}", headers=HEADERS, timeout=30, **kwargs)
    r.raise_for_status()
    return r.json()

def _post(path: str, body: dict) -> dict:
    r = httpx.post(f"{BASE}{path}", headers=HEADERS, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


# ── Playbook CRUD ─────────────────────────────────────────────────────────────

def list_playbooks() -> list:
    data = _get(f"/organizations/{ORG_ID}/playbooks")
    return data.get("playbooks") or data.get("items") or []

def create_playbook() -> str:
    data = _post(f"/organizations/{ORG_ID}/playbooks", {
        "title": PLAYBOOK_TITLE,
        "body":  PLAYBOOK_BODY,
        "macro": "!remediate",
    })
    return data.get("playbook_id") or data.get("id")


# ── Knowledge CRUD ────────────────────────────────────────────────────────────

def list_knowledge() -> list:
    data = _get(f"/organizations/{ORG_ID}/knowledge/notes")
    return data.get("notes") or data.get("items") or []

def create_knowledge() -> str:
    data = _post(f"/organizations/{ORG_ID}/knowledge/notes", {
        "name":       KNOWLEDGE_NAME,
        "body":       KNOWLEDGE_BODY,
        "trigger":    KNOWLEDGE_TRIGGER,
        "pinned_repo": os.getenv("GITHUB_REPO"),
    })
    return data.get("note_id") or data.get("id")


# ── Schedule CRUD ─────────────────────────────────────────────────────────────

def list_schedules() -> list:
    data = _get(f"/organizations/{ORG_ID}/schedules")
    return data.get("schedules") or data.get("items") or []

def create_schedule(playbook_id: str) -> str:
    data = _post(f"/organizations/{ORG_ID}/schedules", {
        "name":        SCHEDULE_NAME,
        "prompt":      SCHEDULE_PROMPT,
        "frequency":   "0 6 * * 1",   # Every Monday 06:00 UTC
        "playbook_id": playbook_id,
        "tags":        ["security-scan", "automated", "weekly"],
    })
    return data.get("schedule_id") or data.get("id")


# ── Sessions ──────────────────────────────────────────────────────────────────

def build_prompt(issue_number: str, issue_title: str, issue_body: str) -> str:
    return f"""Repository: {REPO_URL}
Issue #{issue_number}: {issue_title}

{issue_body}

When complete, open a pull request with "Fixes #{issue_number}" in the description.
"""

def create_session(issue_number: str, issue_title: str, issue_body: str,
                   playbook_id: str = None, knowledge_ids: list = None) -> dict:
    body: dict = {
        "prompt": build_prompt(issue_number, issue_title, issue_body),
        "tags":   [f"issue-{issue_number}", "security-remediation", "automated"],
    }
    if playbook_id:
        body["playbook_id"] = playbook_id
    if knowledge_ids:
        body["knowledge_ids"] = knowledge_ids
    return _post(f"/organizations/{ORG_ID}/sessions", body)

def get_session(session_id: str) -> dict:
    return _get(f"/organizations/{ORG_ID}/sessions/{session_id}")

def get_session_messages(session_id: str) -> list:
    data = _get(f"/organizations/{ORG_ID}/sessions/{session_id}/messages")
    return data.get("items") or []


# ── Insights ──────────────────────────────────────────────────────────────────

def generate_session_insights(session_id: str):
    try:
        _post(f"/organizations/{ORG_ID}/sessions/{session_id}/insights/generate", {})
    except Exception:
        pass

def get_session_insights(session_id: str) -> dict:
    try:
        return _get(f"/organizations/{ORG_ID}/sessions/{session_id}/insights")
    except Exception:
        return {}


# ── Org metrics ───────────────────────────────────────────────────────────────

def get_org_metrics(time_after: int, time_before: int) -> dict:
    params = {"time_after": time_after, "time_before": time_before}
    try:
        sessions = _get(f"/organizations/{ORG_ID}/metrics/sessions", params=params)
    except Exception:
        sessions = {}
    try:
        prs = _get(f"/organizations/{ORG_ID}/metrics/prs", params=params)
    except Exception:
        prs = {}
    return {"sessions": sessions, "prs": prs}


# ── Utilities ─────────────────────────────────────────────────────────────────

def extract_pr_url(messages: list) -> str | None:
    pattern = r'https://github\.com/[^\s\)>\"]+/pull/\d+'
    for msg in reversed(messages):
        content = msg.get("message", "")
        if isinstance(content, str):
            m = re.search(pattern, content)
            if m:
                return m.group(0)
    return None
