"""One SQLite file, one writer (the server). WAL mode.

Schema is section 6.2 of the finalised plan. Vault items are never in
SQLite. Audit log is append-only.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, goal TEXT, constraints TEXT, status TEXT,
  created_at INTEGER, updated_at INTEGER, receipt TEXT);
CREATE TABLE IF NOT EXISTS steps(
  id TEXT PRIMARY KEY, task_id TEXT, n INTEGER, action_json TEXT,
  status TEXT, started_at INTEGER, ended_at INTEGER, ms INTEGER,
  element_id TEXT, result TEXT, error TEXT, retries INTEGER DEFAULT 0,
  scan_blocks INTEGER DEFAULT 0, grounding_failures INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS checkpoints(
  id TEXT PRIMARY KEY, task_id TEXT, step_n INTEGER, summary TEXT,
  state_json TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS memory(
  key TEXT PRIMARY KEY, value TEXT, kind TEXT, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS facts(
  id TEXT PRIMARY KEY, text TEXT, source TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS routines(
  id TEXT PRIMARY KEY, name TEXT, trigger TEXT, action TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, action TEXT,
  payload_hash TEXT, prev_hash TEXT, hash TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS rag_chunks(
  id TEXT PRIMARY KEY, text TEXT, kind TEXT, meta TEXT,
  created_at INTEGER);
CREATE TABLE IF NOT EXISTS watches(
  id TEXT PRIMARY KEY, url TEXT, selector TEXT, last_value TEXT,
  threshold TEXT, status TEXT, muted_until INTEGER, created_at INTEGER,
  condition TEXT DEFAULT 'changed', interval_min INTEGER DEFAULT 60,
  misses INTEGER DEFAULT 0, last_checked INTEGER,
  last_fired_at INTEGER);
CREATE TABLE IF NOT EXISTS watch_events(
  id TEXT PRIMARY KEY, watch_id TEXT, kind TEXT, old_value TEXT,
  new_value TEXT, detail TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS briefings(
  id TEXT PRIMARY KEY, created_at INTEGER, cards_json TEXT);
CREATE TABLE IF NOT EXISTS grants(
  id TEXT PRIMARY KEY, origin TEXT, scope TEXT, expires_at INTEGER,
  created_at INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS pending_approvals(
  id TEXT PRIMARY KEY, task_id TEXT, step INTEGER, action TEXT,
  detail TEXT, created_at INTEGER, expires_at INTEGER);
CREATE TABLE IF NOT EXISTS quota(
  model TEXT PRIMARY KEY, used_today INTEGER, limit_day INTEGER,
  reset_at INTEGER);
CREATE TABLE IF NOT EXISTS receipts(
  id TEXT PRIMARY KEY, task_id TEXT, role_id TEXT, title TEXT, company TEXT,
  url TEXT, status TEXT, confirmation TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS mail_drafts(
  id TEXT PRIMARY KEY, task_id TEXT, recipient TEXT, role_id TEXT,
  subject TEXT, body TEXT, status TEXT, created_at INTEGER,
  updated_at INTEGER);
CREATE TABLE IF NOT EXISTS mail_log(
  id TEXT PRIMARY KEY, draft_id TEXT, recipient TEXT, role_id TEXT,
  status TEXT, detail TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS connectors(
  name TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1, state TEXT,
  last_checked INTEGER, last_error TEXT);
CREATE TABLE IF NOT EXISTS calendar_events(
  id TEXT PRIMARY KEY, task_id TEXT, event_id TEXT, summary TEXT,
  start TEXT, end TEXT, location TEXT, created_at INTEGER);
-- PR #5: missions — one row per standing mission. Timestamps are
-- integer SECONDS (never ms; the audit log is the only ms table).
CREATE TABLE IF NOT EXISTS missions(
  id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
  status TEXT NOT NULL, config_json TEXT NOT NULL,
  bounds_json TEXT NOT NULL, created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL, last_run_at INTEGER,
  next_run_at INTEGER, last_run_status TEXT, last_error TEXT);
CREATE INDEX IF NOT EXISTS idx_missions_status_next
  ON missions(status, next_run_at);
-- PR #5: mission runs — one row per tick that started work.
CREATE TABLE IF NOT EXISTS mission_runs(
  id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, task_id TEXT,
  status TEXT NOT NULL, started_at INTEGER NOT NULL,
  ended_at INTEGER, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_mission_runs_mission
  ON mission_runs(mission_id, started_at DESC);
-- PR #5: mission activity — the append-only receipt log.
CREATE TABLE IF NOT EXISTS mission_activity(
  id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, run_id TEXT,
  kind TEXT NOT NULL, detail TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_mission_activity_mission
  ON mission_activity(mission_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mission_activity_kind_day
  ON mission_activity(mission_id, kind, created_at);
-- PR #6: chat — the Agent view's message thread. One row per
-- message, user and agent alike. Timestamps are integer SECONDS
-- (never ms). source is "planner" or "rules" on agent rows, NULL
-- on user rows; task_id links the reply that created a task.
-- PR #7: provider names the model that answered ("ollama",
-- "antseed", "groq") on planner rows, NULL on rules/user rows.
CREATE TABLE IF NOT EXISTS chat_messages(
  id TEXT PRIMARY KEY, role TEXT, text TEXT, kind TEXT,
  task_id TEXT NULL, source TEXT NULL, provider TEXT NULL,
  created_at INTEGER);
CREATE INDEX IF NOT EXISTS idx_chat_messages_created
  ON chat_messages(created_at);
-- PR #7: chat file uploads — one row per attached file. The
-- bytes live under data/uploads/ (0600, non-guessable name);
-- this table holds the metadata, the locally extracted text,
-- the proposed profile facts (JSON), and the linkage to the
-- chat message it was sent with and the mission created from
-- it (both NULL until linked).
CREATE TABLE IF NOT EXISTS chat_uploads(
  id TEXT PRIMARY KEY, original_name TEXT, stored_name TEXT,
  stored_path TEXT, size_bytes INTEGER, mime TEXT,
  extraction_status TEXT, extracted_text TEXT,
  proposed_facts_json TEXT, chat_message_id TEXT NULL,
  mission_id TEXT NULL, created_at INTEGER);
CREATE INDEX IF NOT EXISTS idx_chat_uploads_message
  ON chat_uploads(chat_message_id);
"""

# Verified free top 5 (Sprint 0 probe, 2026-11-21). gpt-oss-120b is
# tracked here but returns empty content live, so it is not the planner.
# 200/day is the user's verified free limit for these models.
FREE_MODELS = [
    "gpt-oss-120b",
    "google-gemma-4-31b-it",
    "nvidia-nemotron-3-nano-30b-a3b",
    "google-gemma-4-26b-a4b-it",
    "mistral-small-3-2-24b-instruct",
]

_write_lock = threading.Lock()


def _next_midnight_utc_epoch() -> int:
    now = datetime.now(timezone.utc)
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return int(midnight.timestamp()) + 86400


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection to the Eclipse SQLite file (WAL mode)."""
    path = db_path or (config.DATA_DIR / "eclipse.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # Wait up to 5s on write contention instead of failing at once.
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def migrate(db_path: Path | None = None) -> None:
    """Create the schema if missing; seed and roll over the quota table."""
    with _write_lock:
        conn = connect(db_path)
        try:
            conn.executescript(SCHEMA)
            # Sprint 2: facts gain a topic column (keyword memory).
            cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)")}
            if "topic" not in cols:
                conn.execute("ALTER TABLE facts ADD COLUMN topic TEXT")
            # Sprint 3: steps gain a retry counter (verify retries).
            step_cols = {r[1] for r in conn.execute("PRAGMA table_info(steps)")}
            if "retries" not in step_cols:
                conn.execute("ALTER TABLE steps ADD COLUMN retries INTEGER DEFAULT 0")
            # Sprint 3 fix: second-PII-scan blocks and grounding
            # failures are tracked per step (the SCHEMA string
            # declares them too, for fresh databases).
            if "scan_blocks" not in step_cols:
                conn.execute("ALTER TABLE steps ADD COLUMN scan_blocks INTEGER DEFAULT 0")
            if "grounding_failures" not in step_cols:
                conn.execute("ALTER TABLE steps ADD COLUMN grounding_failures INTEGER DEFAULT 0")
            # Sprint 4: receipts (one per verified submit, §6.9).
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "receipts" not in tables:
                conn.execute(
                    "CREATE TABLE receipts("
                    " id TEXT PRIMARY KEY, task_id TEXT, role_id TEXT,"
                    " title TEXT, company TEXT, url TEXT, status TEXT,"
                    " confirmation TEXT, created_at INTEGER)"
                )
            # Sprint 4 fix: one receipt per (task, role) — a duplicate
            # POST must not create a second row.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_task_role"
                " ON receipts(task_id, role_id)"
            )
            # Audit fix: at most one SUBMITTED receipt per role,
            # whatever path wrote it — defence in depth behind the
            # loop-level role claim and the idempotent receipt
            # write. Older duplicate submitted receipts (written
            # before the claim existed) are removed first, keeping
            # the newest, or the index creation would fail and the
            # server would not start on an existing DB.
            conn.execute(
                "DELETE FROM receipts WHERE status = 'submitted'"
                " AND rowid NOT IN (SELECT MAX(rowid) FROM receipts"
                " WHERE status = 'submitted' GROUP BY role_id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS"
                " idx_receipts_role_submitted"
                " ON receipts(role_id) WHERE status = 'submitted'"
            )
            # Sprint 5: cold-mail drafts + log (Flow 2).
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "mail_drafts" not in tables:
                conn.execute(
                    "CREATE TABLE mail_drafts("
                    " id TEXT PRIMARY KEY, task_id TEXT, recipient TEXT,"
                    " role_id TEXT, subject TEXT, body TEXT, status TEXT,"
                    " created_at INTEGER, updated_at INTEGER)"
                )
            if "mail_log" not in tables:
                conn.execute(
                    "CREATE TABLE mail_log("
                    " id TEXT PRIMARY KEY, draft_id TEXT, recipient TEXT,"
                    " role_id TEXT, status TEXT, detail TEXT,"
                    " created_at INTEGER)"
                )
            # Sprint 5 fix: dedup is a constraint, not a check —
            # two concurrent drafts for the same (recipient, role)
            # must not both succeed (check-then-insert races).
            # The index is partial: only a live draft (draft /
            # approved / sent) blocks a new one — a bounced or
            # no-reply draft must not block a retry.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS"
                " idx_mail_drafts_recipient_role"
                " ON mail_drafts(LOWER(recipient), role_id)"
                " WHERE status IN ('draft', 'approved', 'sent')"
            )
            # Sprint 4 fix: grants carry a detail (JSON: the step,
            # element and role the approval covers, §5.4) so one
            # approval cannot be consumed by a different submit.
            grant_cols = {r[1] for r in conn.execute("PRAGMA table_info(grants)")}
            if "detail" not in grant_cols:
                conn.execute("ALTER TABLE grants ADD COLUMN detail TEXT")
            # Bug 3 fix: pending approvals — the human gate is a
            # real store now, not UI flow. The loop records a
            # pending approval when a step reaches the gate, and
            # approve requires a live record for that task (a
            # made-up approval_id used to mint a real grant).
            # The SCHEMA string declares the table for fresh
            # databases; existing ones get it here.
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "pending_approvals" not in tables:
                conn.execute(
                    "CREATE TABLE pending_approvals("
                    " id TEXT PRIMARY KEY, task_id TEXT, step INTEGER,"
                    " action TEXT, detail TEXT, created_at INTEGER,"
                    " expires_at INTEGER)"
                )
            # Sprint 6: the rag_chunks table was reshaped (id, text,
            # kind, meta) — the Sprint 1 shape (doc_id, chunk, vec)
            # never held data (RAG was not built), so recreate it
            # when the old columns are present.
            rag_cols = {r[1] for r in conn.execute("PRAGMA table_info(rag_chunks)").fetchall()}
            if rag_cols and "chunk" in rag_cols:
                conn.execute("DROP TABLE rag_chunks")
                conn.execute(
                    "CREATE TABLE rag_chunks("
                    " id TEXT PRIMARY KEY, text TEXT, kind TEXT, meta TEXT,"
                    " created_at INTEGER)"
                )
            # Sprint 7: connectors registry + calendar events table.
            # The SCHEMA string declares both for fresh databases;
            # existing ones get them here, plus the three connector
            # rows (all on by default).
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "connectors" not in tables:
                conn.execute(
                    "CREATE TABLE connectors("
                    " name TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1,"
                    " state TEXT, last_checked INTEGER, last_error TEXT)"
                )
            if "calendar_events" not in tables:
                conn.execute(
                    "CREATE TABLE calendar_events("
                    " id TEXT PRIMARY KEY, task_id TEXT, event_id TEXT,"
                    " summary TEXT, start TEXT, end TEXT, location TEXT,"
                    " created_at INTEGER)"
                )
            for name in ("gmail", "calendar", "drive"):
                conn.execute(
                    "INSERT OR IGNORE INTO connectors"
                    "(name, enabled, state, last_checked, last_error)"
                    " VALUES (?, 1, 'unknown', NULL, NULL)",
                    (name,),
                )
            # Sprint 8: watches gain condition/interval/miss
            # tracking; watch_events + briefings tables.
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "watch_events" not in tables:
                conn.execute(
                    "CREATE TABLE watch_events("
                    " id TEXT PRIMARY KEY, watch_id TEXT, kind TEXT,"
                    " old_value TEXT, new_value TEXT, detail TEXT,"
                    " created_at INTEGER)"
                )
            if "briefings" not in tables:
                conn.execute(
                    "CREATE TABLE briefings("
                    " id TEXT PRIMARY KEY, created_at INTEGER,"
                    " cards_json TEXT)"
                )
            watch_cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(watches)")}
            for col, ddl in (
                ("condition", "ALTER TABLE watches ADD COLUMN condition TEXT DEFAULT 'changed'"),
                ("interval_min", "ALTER TABLE watches ADD COLUMN interval_min INTEGER DEFAULT 60"),
                ("misses", "ALTER TABLE watches ADD COLUMN misses INTEGER DEFAULT 0"),
                ("last_checked", "ALTER TABLE watches ADD COLUMN last_checked INTEGER"),
                ("last_fired_at", "ALTER TABLE watches ADD COLUMN last_fired_at INTEGER"),
            ):
                if col not in watch_cols:
                    conn.execute(ddl)
            # PR #5: missions, mission_runs, mission_activity
            # tables + the tasks.mission_id link (one mission =
            # one task per run; the runner sets the link right
            # after create_task). The SCHEMA string declares the
            # tables for fresh databases; existing ones get them
            # here, and the ALTER is guarded by PRAGMA
            # table_info, the db.py pattern.
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "missions" not in tables:
                conn.execute(
                    "CREATE TABLE missions("
                    " id TEXT PRIMARY KEY, type TEXT NOT NULL,"
                    " name TEXT NOT NULL, status TEXT NOT NULL,"
                    " config_json TEXT NOT NULL,"
                    " bounds_json TEXT NOT NULL,"
                    " created_at INTEGER NOT NULL,"
                    " updated_at INTEGER NOT NULL, last_run_at INTEGER,"
                    " next_run_at INTEGER, last_run_status TEXT,"
                    " last_error TEXT)"
                )
            if "mission_runs" not in tables:
                conn.execute(
                    "CREATE TABLE mission_runs("
                    " id TEXT PRIMARY KEY, mission_id TEXT NOT NULL,"
                    " task_id TEXT, status TEXT NOT NULL,"
                    " started_at INTEGER NOT NULL, ended_at INTEGER,"
                    " detail TEXT)"
                )
            if "mission_activity" not in tables:
                conn.execute(
                    "CREATE TABLE mission_activity("
                    " id TEXT PRIMARY KEY, mission_id TEXT NOT NULL,"
                    " run_id TEXT, kind TEXT NOT NULL,"
                    " detail TEXT NOT NULL, created_at INTEGER NOT NULL)"
                )
            task_cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(tasks)")}
            if "mission_id" not in task_cols:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN mission_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_mission"
                " ON tasks(mission_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_missions_status_next"
                " ON missions(status, next_run_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mission_runs_mission"
                " ON mission_runs(mission_id, started_at DESC)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mission_activity_mission"
                " ON mission_activity(mission_id, created_at DESC)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mission_activity_kind_day"
                " ON mission_activity(mission_id, kind, created_at)")
            # PR #6: chat_messages — the Agent view's thread. The
            # SCHEMA string declares it for fresh databases;
            # existing ones get it here. The source column guard
            # covers a database migrated by an earlier PR #6 cut
            # that created the table without it.
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "chat_messages" not in tables:
                conn.execute(
                    "CREATE TABLE chat_messages("
                    " id TEXT PRIMARY KEY, role TEXT, text TEXT, kind TEXT,"
                    " task_id TEXT NULL, source TEXT NULL,"
                    " provider TEXT NULL, created_at INTEGER)"
                )
            chat_cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(chat_messages)")}
            if "source" not in chat_cols:
                conn.execute(
                    "ALTER TABLE chat_messages ADD COLUMN source TEXT NULL")
            # PR #7: provider names the model that answered; a
            # database migrated before PR #7 gets it here.
            if "provider" not in chat_cols:
                conn.execute(
                    "ALTER TABLE chat_messages ADD COLUMN provider TEXT NULL")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_messages_created"
                " ON chat_messages(created_at)")
            # PR #7: chat_uploads — the Agent view's attached
            # files. The SCHEMA string declares it for fresh
            # databases; existing ones get it here.
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "chat_uploads" not in tables:
                conn.execute(
                    "CREATE TABLE chat_uploads("
                    " id TEXT PRIMARY KEY, original_name TEXT,"
                    " stored_name TEXT, stored_path TEXT,"
                    " size_bytes INTEGER, mime TEXT,"
                    " extraction_status TEXT, extracted_text TEXT,"
                    " proposed_facts_json TEXT,"
                    " chat_message_id TEXT NULL,"
                    " mission_id TEXT NULL, created_at INTEGER)"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_uploads_message"
                " ON chat_uploads(chat_message_id)")
            reset_at = _next_midnight_utc_epoch()
            for model in FREE_MODELS:
                conn.execute(
                    "INSERT OR IGNORE INTO quota"
                    "(model, used_today, limit_day, reset_at)"
                    " VALUES (?, 0, 200, ?)",
                    (model, reset_at),
                )
            # Daily rollover: a reset_at in the past means a new day.
            now = int(time.time())
            conn.execute(
                "UPDATE quota SET used_today = 0, reset_at = ?"
                " WHERE reset_at <= ?",
                (reset_at, now),
            )
            # PR #5 fact store at scale: fact_hash + scope columns,
            # dedup backfill, and the FTS5 index over facts. The
            # migration lives in server/memory (W5) and is
            # idempotent; the import is function-level so db.py
            # stays importable without the memory package.
            from ..memory import facts_migrate

            facts_migrate.apply_facts_migrations(conn)
            conn.commit()
        finally:
            conn.close()
