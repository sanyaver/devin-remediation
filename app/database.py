import sqlite3, os
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", "/app/data/sessions.db")

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_number TEXT,
                issue_title TEXT,
                session_id TEXT,
                status TEXT DEFAULT 'dispatched',
                pr_url TEXT,
                devin_url TEXT,
                triggered_at TEXT,
                completed_at TEXT,
                duration_seconds INTEGER
            )
        """)

def insert_session(issue_number, issue_title, session_id, devin_url):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO sessions (issue_number, issue_title, session_id, devin_url, triggered_at)
            VALUES (?, ?, ?, ?, ?)
        """, (issue_number, issue_title, session_id, devin_url, datetime.now(timezone.utc).isoformat()))

def update_session(session_id, status, pr_url=None, completed_at=None, duration=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE sessions SET status=?, pr_url=?, completed_at=?, duration_seconds=?
            WHERE session_id=?
        """, (status, pr_url, completed_at, duration, session_id))

def get_all_sessions():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM sessions ORDER BY triggered_at DESC"
        ).fetchall()]
