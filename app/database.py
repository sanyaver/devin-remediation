import sqlite3, os
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", "/app/data/sessions.db")

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_number     TEXT,
                issue_title      TEXT,
                session_id       TEXT,
                status           TEXT DEFAULT 'dispatched',
                pr_url           TEXT,
                devin_url        TEXT,
                triggered_at     TEXT,
                completed_at     TEXT,
                duration_seconds INTEGER,
                category         TEXT
            )
        """)
        # Migrate: add category column if this is an existing DB
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN category TEXT")
        except Exception:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS setup_config (
                id           INTEGER PRIMARY KEY,
                playbook_id  TEXT,
                knowledge_id TEXT,
                schedule_id  TEXT,
                updated_at   TEXT
            )
        """)

def insert_session(issue_number, issue_title, session_id, devin_url):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO sessions (issue_number, issue_title, session_id, devin_url, triggered_at)
            VALUES (?, ?, ?, ?, ?)
        """, (issue_number, issue_title, session_id, devin_url,
              datetime.now(timezone.utc).isoformat()))

def update_session(session_id, status, pr_url=None, completed_at=None,
                   duration=None, category=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE sessions
            SET status=?, pr_url=?, completed_at=?, duration_seconds=?, category=?
            WHERE session_id=?
        """, (status, pr_url, completed_at, duration, category, session_id))

def get_all_sessions():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM sessions ORDER BY triggered_at DESC"
        ).fetchall()]

def get_setup_config() -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM setup_config WHERE id = 1").fetchone()
        return dict(row) if row else None

def save_setup_config(config: dict):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO setup_config
                (id, playbook_id, knowledge_id, schedule_id, updated_at)
            VALUES (1, ?, ?, ?, ?)
        """, (config.get("playbook_id"), config.get("knowledge_id"),
              config.get("schedule_id"), datetime.now(timezone.utc).isoformat()))
