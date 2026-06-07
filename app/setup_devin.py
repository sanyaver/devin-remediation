import logging
from devin import (
    list_playbooks, create_playbook, PLAYBOOK_TITLE,
    list_knowledge, create_knowledge, KNOWLEDGE_NAME,
    list_schedules, create_schedule, SCHEDULE_NAME,
)
from database import get_setup_config, save_setup_config

log = logging.getLogger(__name__)


def setup_devin() -> dict:
    """
    Idempotent startup: ensure the org-level playbook, knowledge note, and
    weekly schedule exist in Devin. IDs are persisted to the local DB so
    subsequent restarts skip the API calls entirely.

    Returns dict with keys: playbook_id, knowledge_id, schedule_id (any may be None on failure).
    """
    config = get_setup_config() or {}
    changed = False

    # ── Playbook ──────────────────────────────────────────────────────────────
    playbook_id = config.get("playbook_id")
    if not playbook_id:
        try:
            existing = [p for p in list_playbooks()
                        if p.get("title") == PLAYBOOK_TITLE]
            if existing:
                playbook_id = existing[0].get("playbook_id") or existing[0].get("id")
                log.info(f"Playbook already exists: {playbook_id}")
            else:
                playbook_id = create_playbook()
                log.info(f"Created playbook: {playbook_id}")
            changed = True
        except Exception as e:
            log.warning(f"Playbook setup skipped: {e}")

    # ── Knowledge ─────────────────────────────────────────────────────────────
    knowledge_id = config.get("knowledge_id")
    if not knowledge_id:
        try:
            existing = [n for n in list_knowledge()
                        if n.get("name") == KNOWLEDGE_NAME]
            if existing:
                knowledge_id = existing[0].get("note_id") or existing[0].get("id")
                log.info(f"Knowledge note already exists: {knowledge_id}")
            else:
                knowledge_id = create_knowledge()
                log.info(f"Created knowledge note: {knowledge_id}")
            changed = True
        except Exception as e:
            log.warning(f"Knowledge setup skipped: {e}")

    # ── Schedule ──────────────────────────────────────────────────────────────
    schedule_id = config.get("schedule_id")
    if not schedule_id:
        try:
            existing = [s for s in list_schedules()
                        if s.get("name") == SCHEDULE_NAME]
            if existing:
                schedule_id = existing[0].get("schedule_id") or existing[0].get("id")
                log.info(f"Schedule already exists: {schedule_id}")
            else:
                schedule_id = create_schedule(playbook_id or "")
                log.info(f"Created schedule: {schedule_id}")
            changed = True
        except Exception as e:
            log.warning(f"Schedule setup skipped: {e}")

    result = {
        "playbook_id":  playbook_id,
        "knowledge_id": knowledge_id,
        "schedule_id":  schedule_id,
    }
    if changed:
        save_setup_config(result)

    log.info(f"Devin setup complete: playbook={playbook_id} "
             f"knowledge={knowledge_id} schedule={schedule_id}")
    return result
