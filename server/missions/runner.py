"""Mission runner (PR #5, §3.6): HYBRID — a server background
thread tick for server-side work + extension alarms (pull) for
browser work. The server cannot push to the extension (§2a), so
browser work is pulled via GET /missions/due; everything that
needs no browser (Gmail scan, matching, drafting, cap
enforcement, briefing) runs here, where it works while Chrome
is closed.

Thread: one daemon thread ("mission-runner"), started lazily
from main.py lifespan (W1 adds the call — the same pattern as
the vault sweeper). Tick every 60 s; a fresh connection per
tick; single-threaded, so cap check-then-act races are bounded.
`tick_once` / `run_mission` are synchronous and testable
without the thread.

A tick, in order: (1) re-read mission status (stop check);
(2) server-side discovery; (3) matching/scoring; (4) drafting
(mail) / watch setup (price); (5) if a browser action is
needed: check the extension heartbeat — stale (>1800 s) means
the run ends `waiting_for_extension`, not an error; fresh
means the run's task is served to the extension via
/missions/due; (6) settle receipts/activity; (7) update
last_run_at / next_run_at / last_run_status. The runner
re-checks mission status before every action — a mission
stopped mid-tick stops acting mid-tick.

Approvals (Q1 standing approval): every consequential action
still goes through the pending-approval mechanism. When a
mission action needs approval, the runner uses the pending
approvals recorded against the run's tasks. If the mission has
auto_approve=true AND the action kind is NOT in
require_approval_for AND the action is within bounds/caps, the
runner auto-satisfies the pending approval via
server/tasks/approvals.py::approve_pending (W1's shared helper
— the same gate as the human approve endpoint). Over cap →
refused (activity row + audit, run continues). Out of bounds
or auto_approve=false → the pending approval is left for the
user. Vault fills, sends and submits ALL go through this path:
vault-fill approvals are never auto-satisfied (filling secrets
unattended is not something standing approval covers — the
user decides those).

Failures: transient errors (planner, Gmail, 5xx) fail the run
and retry next tick; 3 consecutive failed runs auto-pause the
mission. A run that ends waiting_for_extension does not count
toward the failure auto-pause; 3 consecutive waiting runs
make the run status `stalled` (the mission stays active and
recovers automatically when the extension returns).

One mission = one task per run (§3.1): a job-hunt run spawns
at most one apply task (the top-matched role); a cold-mail run
spawns one task anchoring that run's drafts. Roles discovered
but not acted on re-surface the next day (discovery dedups
within a 24-hour window — see discovery._discovered_ids), so
one-task-per-run never loses a role for good.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from ..core import audit, db
from ..jobs import applied as applied_mod
from ..mail import drafts as draft_mod
from ..mail import send as send_mod
from ..monitor import watches as watch_mod
from ..tasks import loop
from . import discovery, resume as resume_mod

logger = logging.getLogger("eclipse.missions")

TICK_INTERVAL_S = 60
EXTENSION_STALE_S = 1800  # 30 minutes without a /missions/due or check-in
MAX_CONSECUTIVE_FAILURES = 3  # then auto-pause
MAX_CONSECUTIVE_WAITING = 3  # then the run status is "stalled"
STALE_EXECUTING_S = 3600  # an "executing" step older than this is dead
BRIEFING_HOUR_IST = 9
IST = ZoneInfo("Asia/Kolkata")

LAST_EXTENSION_SEEN_KEY = "missions:last_extension_seen_at"
LAST_BRIEFING_DAY_KEY = "briefing:last_day_ist"

TERMINAL_TASK_STATUSES = ("cancelled", "failed", "done")
WAITING_STATUSES = ("waiting_for_extension", "stalled")

_thread: threading.Thread | None = None
_stop_event = threading.Event()
_start_lock = threading.Lock()


class _RunFailed(Exception):
    """A run failed (transient or config error) — the run ends
    "failed", the mission retries next tick."""


def _now() -> int:
    """Now, epoch seconds. A module-level helper so tests can
    monkeypatch the clock (runner._now) without freezegun."""
    return int(time.time())


# ----------------------------------------------------------------
# W1 shared contracts (server/missions/models.py, bounds.py,
# links.py; server/tasks/approvals.py). Imported lazily inside
# the functions that use them: the workers build in parallel,
# and a missing module must degrade to a clear note, never an
# import-time crash of this module.
# ----------------------------------------------------------------

def _models():
    from . import models

    return models


def _bounds():
    from . import bounds

    return bounds


def _links():
    from . import links

    return links


def _approvals():
    from ..tasks import approvals

    return approvals


# ----------------------------------------------------------------
# Small state helpers
# ----------------------------------------------------------------

def _memory_get(conn, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM memory WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row is not None else None


def _memory_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO memory (key, value, updated_at)"
        " VALUES (?, ?, strftime('%s','now'))"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = strftime('%s','now')",
        (key, value),
    )
    conn.commit()


def _parse_mission_row(row) -> dict:
    mission = dict(row)
    for key in ("config", "bounds"):
        raw = mission.pop(f"{key}_json", None)
        try:
            mission[key] = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            mission[key] = {}
    return mission


def _get_mission(conn, mission_id: str) -> dict | None:
    """Fresh mission row (W1's getter, with a raw-SQL fallback
    for the world before models.py lands)."""
    try:
        return _models().get_mission(conn, mission_id)
    except ImportError:
        row = conn.execute(
            "SELECT * FROM missions WHERE id = ?", (mission_id,)
        ).fetchone()
        return _parse_mission_row(row) if row is not None else None


def _fresh_status(conn, mission_id: str) -> str | None:
    mission = _get_mission(conn, mission_id)
    return mission["status"] if mission is not None else None


def _record_activity(conn, mission_id: str, kind: str, detail: dict,
                     run_id: str | None = None) -> None:
    """Record one activity row (W1's models.record_activity).
    A missing models module degrades to a log line — the run
    must not fail for a bookkeeping write."""
    try:
        _models().record_activity(conn, mission_id, kind, detail,
                                  run_id=run_id)
    except ImportError:
        logger.warning(
            "models module missing — activity %s for %s not recorded",
            kind, mission_id,
        )


def _activity_details(conn, mission_id: str, kind: str) -> list[dict]:
    """Parsed detail dicts for one mission's activity of one
    kind (for dedup checks)."""
    try:
        rows = _models().list_activity(conn, mission_id, limit=200,
                                       kind=kind)
    except ImportError:
        rows = [
            dict(r) for r in conn.execute(
                "SELECT * FROM mission_activity"
                " WHERE mission_id = ? AND kind = ?"
                " ORDER BY created_at DESC LIMIT 200",
                (mission_id, kind),
            ).fetchall()
        ]
    out = []
    for row in rows:
        try:
            out.append(json.loads(row["detail"] or "{}"))
        except (ValueError, TypeError):
            continue
    return out


def _transition(conn, mission_id: str, to_status: str) -> None:
    """Move a mission (W1's state machine; raw-SQL fallback for
    the pre-models world — active->paused only)."""
    try:
        _models().transition(conn, mission_id, to_status)
        return
    except ImportError:
        pass
    if to_status == "paused":
        conn.execute(
            "UPDATE missions SET status = 'paused', updated_at = ?,"
            " next_run_at = NULL WHERE id = ? AND status = 'active'",
            (_now(), mission_id),
        )
        conn.commit()


def extension_heartbeat_fresh(conn, now: int | None = None) -> bool:
    """Has the extension called /missions/due or /missions/check-in
    within EXTENSION_STALE_S? No heartbeat on record means stale
    (the extension has never checked in — waiting, not an error)."""
    now = _now() if now is None else now
    last = _memory_get(conn, LAST_EXTENSION_SEEN_KEY)
    if last is None:
        return False
    try:
        last_seen = int(last)
    except ValueError:
        return False
    return now - last_seen <= EXTENSION_STALE_S


def _in_quiet_hours(bounds: dict, now: int) -> bool:
    """Is `now` inside the mission's quiet hours (IST)? The
    window may cross midnight (22:00-07:00)."""
    quiet = (bounds or {}).get("quiet_hours_ist")
    if not quiet:
        return False
    try:
        start_h, start_m = (int(p) for p in quiet["start"].split(":"))
        end_h, end_m = (int(p) for p in quiet["end"].split(":"))
    except (KeyError, TypeError, ValueError):
        return False
    current = datetime.fromtimestamp(now, tz=IST)
    minutes = current.hour * 60 + current.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def _ist_day_string(now: int) -> str:
    return datetime.fromtimestamp(now, tz=IST).strftime("%Y-%m-%d")


# ----------------------------------------------------------------
# Run rows
# ----------------------------------------------------------------

def _start_run(conn, mission_id: str) -> dict:
    run_id = f"mr_{uuid.uuid4().hex[:12]}"
    now = _now()
    conn.execute(
        "INSERT INTO mission_runs (id, mission_id, task_id, status,"
        " started_at, ended_at, detail) VALUES (?, ?, NULL, 'running',"
        " ?, NULL, NULL)",
        (run_id, mission_id, now),
    )
    conn.commit()
    return {"id": run_id, "mission_id": mission_id, "task_id": None,
            "status": "running", "started_at": now, "ended_at": None,
            "detail": None}


def _end_run(conn, run_id: str, status: str, detail: str,
             task_id: str | None) -> dict:
    now = _now()
    conn.execute(
        "UPDATE mission_runs SET status = ?, ended_at = ?, detail = ?,"
        " task_id = COALESCE(?, task_id) WHERE id = ?",
        (status, now, detail, task_id, run_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM mission_runs WHERE id = ?", (run_id,)
    ).fetchone()
    return dict(row) if row is not None else {
        "id": run_id, "status": status, "detail": detail,
        "task_id": task_id, "ended_at": now,
    }


def _consecutive_run_status_count(conn, mission_id: str,
                                  statuses: tuple[str, ...]) -> int:
    """How many of the most recent (finished) runs have a status
    in `statuses`, counting back from the newest? The current
    run is still 'running', so it never pollutes the count."""
    rows = conn.execute(
        "SELECT status FROM mission_runs WHERE mission_id = ?"
        " AND status != 'running'"
        " ORDER BY started_at DESC, rowid DESC LIMIT 10",
        (mission_id,),
    ).fetchall()
    count = 0
    for row in rows:
        if row["status"] in statuses:
            count += 1
        else:
            break
    return count


def _update_mission_run_fields(conn, mission_id: str, *, last_run_at: int,
                               next_run_at: int | None,
                               last_run_status: str,
                               last_error: str | None) -> None:
    conn.execute(
        "UPDATE missions SET last_run_at = ?, next_run_at = ?,"
        " last_run_status = ?, last_error = ?, updated_at = ?"
        " WHERE id = ?",
        (last_run_at, next_run_at, last_run_status, last_error,
         _now(), mission_id),
    )
    conn.commit()


def _set_config_watch_id(conn, mission_id: str, config: dict,
                         watch_id: str) -> None:
    """Persist the watch id the runner created into the mission
    config (runner-owned bookkeeping — raw SQL, not the
    user-facing update_mission, which refuses edits while
    active)."""
    updated = dict(config)
    updated["watch_id"] = watch_id
    conn.execute(
        "UPDATE missions SET config_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(updated), _now(), mission_id),
    )
    conn.commit()


def _reset_stale_executing(conn, now: int) -> int:
    """A step left 'executing' for over an hour belongs to an
    alarm run that died mid-step (service worker killed, tab
    closed). Reset it to 'pending' so the driver can re-drive
    it — the server-side repeat guards (never re-execute a done
    step, never re-click a done element) still hold."""
    cur = conn.execute(
        "UPDATE steps SET status = 'pending'"
        " WHERE status = 'executing' AND started_at IS NOT NULL"
        " AND started_at < ?",
        (now - STALE_EXECUTING_S,),
    )
    conn.commit()
    if cur.rowcount:
        logger.info("reset %d stale executing steps to pending",
                    cur.rowcount)
    return cur.rowcount


# ----------------------------------------------------------------
# Fact store (W5) — see _record_run_facts
# ----------------------------------------------------------------

def _fact_text_for_candidate(candidate: dict, mission: dict) -> tuple[str, str] | None:
    """(fact text, topic) for one discovered candidate, or None.
    Text is short and redacted — never raw PII."""
    if candidate["kind"] == "role":
        role = candidate["role"] or {}
        match = candidate.get("match") or {}
        title = (role.get("title") or "untitled role")[:80]
        company = (role.get("company") or "")[:80]
        score = match.get("score")
        line = f"saw role {title}"
        if company:
            line += f" at {company}"
        if score is not None:
            line += f", score {score}"
        return discovery.redact_candidate_text(line), "role_seen"
    if candidate["kind"] == "price_event":
        detail = candidate.get("detail") or {}
        observed = detail.get("observed_price")
        host = ""
        try:
            host = urlparse((mission.get("config") or {}).get("url") or "").netloc
        except ValueError:
            host = ""
        if observed is not None:
            line = f"price {observed} observed"
        else:
            line = "price changed"
        if host:
            line += f" at {host}"
        return discovery.redact_candidate_text(line), "price"
    return None


def _fact_text_for_action(action: dict) -> tuple[str, str] | None:
    kind = action.get("kind")
    if kind == "applied":
        return f"applied to role {action.get('role_id')}", "applied"
    if kind == "sent":
        return f"sent mail for role {action.get('role_id')}", "mail_sent"
    return None


def _record_run_facts(conn, mission: dict, candidates: list[dict],
                      actions: list[dict]) -> str | None:
    """Write mission-scoped facts and maintain them (W5 fact
    store). Called at the end of each run's discovery/action
    phase. Returns a note for the run detail, or None.

    W5's modules may not exist yet: the imports are lazy, and
    an ImportError (or a pre-W5 store_fact without `scope`)
    skips fact work for the run with a note — it never fails
    the run. Signatures per the W5 contract:
    store_fact(conn, text, source, topic=None, scope="global"),
    compact_mission_facts(conn, mission_id, threshold=500),
    enforce_fact_budget(conn, mission_id, max_facts),
    expire_stale_facts(conn, mission_id=None, ttl_days=90).
    """
    try:
        from ..memory import budgets as _budgets
        from ..memory import compaction as _compaction
        from ..memory.keyword import store_fact as _store_fact
    except ImportError:
        return "fact store unavailable (W5 modules not present yet) — facts skipped"
    mission_id = mission["id"]
    max_facts = (mission.get("config") or {}).get("max_facts", 1000)
    if max_facts == 0:
        return None  # fact storage disabled for this mission
    try:
        for candidate in candidates:
            made = _fact_text_for_candidate(candidate, mission)
            if made is not None:
                text, topic = made
                _store_fact(conn, text, source="mission", topic=topic,
                            scope=mission_id)
        for action in actions:
            made = _fact_text_for_action(action)
            if made is not None:
                text, topic = made
                _store_fact(conn, text, source="mission", topic=topic,
                            scope=mission_id)
        _compaction.compact_mission_facts(conn, mission_id)
        _budgets.enforce_fact_budget(conn, mission_id, max_facts)
        _budgets.expire_stale_facts(conn, mission_id)
    except TypeError:
        # Pre-W5 keyword.py: store_fact has no `scope` parameter.
        return "fact store outdated (store_fact has no scope) — facts skipped"
    except Exception as exc:  # noqa: BLE001 - fact work never fails a run
        logger.warning("mission %s fact maintenance failed: %s",
                       mission_id, exc)
        return f"fact maintenance failed: {str(exc)[:100]}"
    return None


# ----------------------------------------------------------------
# Discovery / settle helpers
# ----------------------------------------------------------------

def _min_score(mission: dict) -> int:
    """The effective min score: the config value wins, else the
    bounds value, else 60 (§3.5 lists min_score in bounds; the
    job_hunt config carries it too)."""
    config = mission.get("config") or {}
    bounds = mission.get("bounds") or {}
    if config.get("min_score") is not None:
        return int(config["min_score"])
    return int(bounds.get("min_score", 60))


def _settle_completed(conn, mission: dict, run_id: str) -> list[dict]:
    """Submitted receipts for the mission's tasks that have no
    'applied' activity yet → record them. This is how an apply
    the extension driver verified lands in the mission's caps
    (caps count 'applied' activity)."""
    mission_id = mission["id"]
    seen = {
        (d.get("task_id"), d.get("role_id"))
        for d in _activity_details(conn, mission_id, "applied")
    }
    rows = conn.execute(
        "SELECT r.id AS receipt_id, r.task_id, r.role_id, r.url"
        " FROM receipts r JOIN tasks t ON r.task_id = t.id"
        " WHERE t.mission_id = ? AND r.status = 'submitted'",
        (mission_id,),
    ).fetchall()
    settled = []
    for row in rows:
        key = (row["task_id"], row["role_id"])
        if key in seen:
            continue
        _record_activity(conn, mission_id, "applied",
                         {"task_id": row["task_id"],
                          "role_id": row["role_id"],
                          "url": row["url"],
                          "receipt_id": row["receipt_id"]},
                         run_id=run_id)
        settled.append({"kind": "applied", "role_id": row["role_id"],
                        "task_id": row["task_id"]})
    return settled


# ----------------------------------------------------------------
# Action phase — one function per mission type
# ----------------------------------------------------------------

def _in_flight_apply_count(conn, mission_id: str, day_start: int) -> int:
    """Apply tasks spawned today that have not settled yet
    (non-terminal) — they count against the cap alongside
    completed applies, or one run could spawn past the cap
    before any apply verifies."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE mission_id = ?"
        " AND created_at >= ? AND status NOT IN"
        " ('cancelled', 'failed', 'done')",
        (mission_id, day_start),
    ).fetchone()
    return int(row["c"]) if row else 0


def _refuse(conn, mission: dict, run_id: str, action: str,
            reason: str, attempted: dict, notes: list[str]) -> None:
    """An out-of-bounds refusal: activity row + audit entry,
    and the run continues (§3.5 rule 3)."""
    detail = {"reason": reason, "attempted": action, **attempted}
    _record_activity(conn, mission["id"], "refused", detail,
                     run_id=run_id)
    audit.append(conn, None, "mission.refused",
                 {"mission_id": mission["id"], "action": action,
                  "reason": reason})
    notes.append(f"{action} refused: {reason}")


def _spawn_apply_task(conn, mission: dict, role: dict,
                      call_fn) -> str:
    """Spawn + plan the run's apply task (one task per run).
    Returns the task id. Raises _RunFailed when planning fails
    (the planner is the transient part — retry next tick)."""
    title = (role.get("title") or "the role")[:100]
    company = (role.get("company") or "")[:100]
    goal = f"Apply to {title}" + (f" at {company}" if company else "")
    role_id = role.get("id") or role.get("url") or ""
    lines = [f"role: {role_id}"]
    if role.get("url"):
        lines.append(f"url: {role['url']}")
    allowed = (mission.get("bounds") or {}).get("allowed_sites") or []
    if allowed:
        lines.append("allowed sites: " + ", ".join(allowed))
    task_id = loop.create_task(conn, goal, "\n".join(lines))
    try:
        _links().set_task_mission(conn, task_id, mission["id"])
    except ImportError:
        conn.execute(
            "UPDATE tasks SET mission_id = ? WHERE id = ?",
            (mission["id"], task_id),
        )
        conn.commit()
    try:
        loop.plan_task(conn, task_id, call_fn=call_fn)
    except Exception as exc:  # noqa: BLE001 - planner failure fails the run
        raise _RunFailed(f"planning the apply task failed: {exc}") from exc
    return task_id


def _act_job_hunt(conn, mission: dict, matched: list[dict],
                  run_id: str, call_fn, notes: list[str]) -> tuple[str | None, dict]:
    """Apply to the top-matched role (one task per run)."""
    if not matched:
        return None, {"actions": []}
    best = max(matched, key=lambda c: (c.get("match") or {}).get("score", 0))
    role = best["role"] or {}
    role_id = role.get("id") or role.get("url") or ""
    url = role.get("url") or ""
    if applied_mod.is_already_applied(conn, role_id or url, url or None):
        notes.append(f"already applied to {role_id} — skipped")
        return None, {"actions": []}
    # The authoritative cap check, immediately before spawning.
    try:
        allowed, reason = _bounds().check_bounds(conn, mission, "apply")
    except ImportError:
        allowed, reason = True, None
    if not allowed:
        _refuse(conn, mission, run_id, "apply", reason or "cap reached",
                {"role_id": role_id}, notes)
        return None, {"actions": []}
    # In-flight guard: tasks spawned today that have not settled
    # yet also count, or the run could spawn past the cap (the
    # completed-applies half is counted by check_bounds above).
    try:
        day_start = _bounds().ist_day_start()
    except ImportError:
        day_start = 0
    max_applies = (mission.get("bounds") or {}).get("max_applies_per_day", 3)
    in_flight = _in_flight_apply_count(conn, mission["id"], day_start)
    if in_flight >= max_applies:
        _refuse(conn, mission, run_id, "apply",
                "apply cap reached counting in-flight tasks"
                f" ({in_flight}/{max_applies})",
                {"role_id": role_id}, notes)
        return None, {"actions": []}
    task_id = _spawn_apply_task(conn, mission, role, call_fn)
    notes.append(f"spawned apply task {task_id} for role {role_id}")
    return task_id, {"actions": []}


def _record_mail_pending_approval(conn, task_id: str,
                                  draft_id: str) -> str:
    """Record the pending approval for a draft send (action
    "mail_send", detail {"draft_id": ...}) — the task
    pending-approval pattern (§3.2), never the mail endpoint's
    accept-any-id behaviour. Idempotent per live (task, draft):
    re-recording reuses the existing id."""
    now = _now()
    rows = conn.execute(
        "SELECT id, detail FROM pending_approvals WHERE task_id = ?"
        " AND step = 0 AND action = 'mail_send' AND expires_at > ?"
        " ORDER BY created_at ASC",
        (task_id, now),
    ).fetchall()
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (ValueError, TypeError):
            continue
        if detail.get("draft_id") == draft_id:
            return row["id"]
    approval_id = f"appr-{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO pending_approvals (id, task_id, step, action,"
        " detail, created_at, expires_at) VALUES (?, ?, 0, 'mail_send',"
        " ?, ?, ?)",
        (approval_id, task_id, json.dumps({"draft_id": draft_id}),
         now, now + loop.PENDING_APPROVAL_TTL_S),
    )
    conn.commit()
    return approval_id


def _act_cold_mail(conn, mission: dict, matched: list[dict],
                   run_id: str, notes: list[str]) -> tuple[str | None, dict]:
    """Draft cold mails (one task per run anchors the drafts).
    Drafts are created, then each gets a mail_send pending
    approval; the auto-approve sweep below decides whether it
    is satisfied now or waits for the user."""
    config = mission.get("config") or {}
    recipients = config.get("recipients") or []
    if not recipients:
        notes.append("cold_mail has no recipients — nothing to draft")
        return None, {"actions": []}
    if not matched:
        notes.append("no roles fit for mailing this run")
        return None, {"actions": []}
    from ..jobs import profile as profile_mod

    stored = profile_mod.get_profile(conn)
    if stored is None:
        notes.append("no profile stored — cannot draft (share a resume first)")
        return None, {"actions": []}
    max_drafts = int(config.get("max_drafts_per_run") or 5)
    top = max(matched, key=lambda c: (c.get("match") or {}).get("score", 0))
    title = ((top["role"] or {}).get("title") or "roles")[:80]
    task_id = loop.create_task(
        conn, f"Cold-mail recruiters about {title}", ""
    )
    try:
        _links().set_task_mission(conn, task_id, mission["id"])
    except ImportError:
        conn.execute(
            "UPDATE tasks SET mission_id = ? WHERE id = ?",
            (mission["id"], task_id),
        )
        conn.commit()
    # No planning: a mail task has no browser steps.
    created = 0
    for candidate in matched:
        if created >= max_drafts:
            break
        role = candidate["role"] or {}
        role_id = role.get("id") or role.get("url") or ""
        reasons = (candidate.get("match") or {}).get("reasons") or []
        for recipient in recipients:
            if created >= max_drafts:
                break
            draft_id = draft_mod.create_draft(
                conn, task_id, recipient, role, stored, reasons
            )
            if draft_id is None:
                continue  # dedup: already mailed / live draft
            domain = recipient.split("@")[-1] if "@" in recipient else ""
            _record_activity(conn, mission["id"], "drafted",
                             {"draft_id": draft_id, "role_id": role_id,
                              "recipient_domain": domain},
                             run_id=run_id)
            _record_mail_pending_approval(conn, task_id, draft_id)
            created += 1
    notes.append(f"created {created} cold-mail draft(s)")
    return task_id, {"actions": []}


def _act_price_watch(conn, mission: dict, candidates: list[dict],
                     run_id: str, notes: list[str]) -> tuple[None, dict]:
    """Ensure the watch exists; check the target price. No task
    is spawned — price work is watch-driven, not task-driven."""
    config = mission.get("config") or {}
    watch_id = config.get("watch_id")
    if not watch_id:
        watch = watch_mod.create_watch(
            conn, config["url"], config["selector"],
            config.get("condition") or "changed",
            interval_min=max(15, int(config.get("interval_min") or 60)),
        )
        _set_config_watch_id(conn, mission["id"], config, watch["id"])
        notes.append(f"created watch {watch['id']} for {config['url']}")
        return None, {"actions": []}
    target = config.get("target_price")
    if target is None:
        return None, {"actions": []}
    for candidate in candidates:
        if candidate["kind"] != "price_event":
            continue
        observed = (candidate.get("detail") or {}).get("observed_price")
        if observed is None or observed > target:
            continue
        notes.append(f"target hit: observed {observed} <= target {target}")
        if config.get("one_shot", True):
            _transition(conn, mission["id"], "done")
            audit.append(conn, None, "mission.completed",
                         {"mission_id": mission["id"],
                          "observed_price": observed,
                          "target_price": target})
            notes.append("mission completed (one-shot target hit)")
        break
    return None, {"actions": []}


# ----------------------------------------------------------------
# Approvals: the auto-approve sweep + the send sweep
# ----------------------------------------------------------------

def _record_approval_needed_once(conn, mission: dict, run_id: str,
                                 approval: dict) -> None:
    """Record an 'approval_needed' activity row for a pending
    approval — once per approval id, so a 60-second tick does
    not spam the timeline while the user decides."""
    seen = {
        d.get("approval_id")
        for d in _activity_details(conn, mission["id"], "approval_needed")
    }
    if approval["approval_id"] in seen:
        return
    _record_activity(conn, mission["id"], "approval_needed",
                     {"approval_id": approval["approval_id"],
                      "task_id": approval["task_id"],
                      "action": approval["action"],
                      "step": approval.get("step")},
                     run_id=run_id)


def _send_with_grant(conn, mission: dict, draft: dict, run_id: str,
                     notes: list[str], actions: list[dict]) -> None:
    """Send an approved draft whose live grant exists: consume
    the grant (one approval = one send, the mail_api rule),
    attach the stored resume when configured, send, record."""
    from ..api.mail_api import _live_grant_for_draft

    draft_id = draft["id"]
    grant_id = _live_grant_for_draft(conn, draft["task_id"], draft_id)
    if grant_id is None:
        notes.append(f"no live grant for draft {draft_id} — send skipped")
        return
    attach = None
    if (mission.get("config") or {}).get("attach_resume"):
        attach = resume_mod.resume_file_path(
            resume_mod.get_resume_meta(conn))
        if attach is None:
            notes.append(
                f"attach_resume requested but no resume is stored —"
                f" draft {draft_id} not sent"
            )
            return
    conn.execute("DELETE FROM grants WHERE id = ?", (grant_id,))
    try:
        result = send_mod.send_draft(conn, draft_id, attach=attach)
    except Exception as exc:  # noqa: BLE001 - a failed send is activity, not a run failure
        # The grant delete commits with send_draft's own failure
        # log write (the mail_api rule: the approval authorised
        # one send attempt, and the attempt happened).
        _record_activity(conn, mission["id"], "failed",
                         {"draft_id": draft_id,
                          "reason": f"send failed: {str(exc)[:200]}"},
                         run_id=run_id)
        notes.append(f"send failed for draft {draft_id}: {exc}")
        return
    if result.status == "sent":
        _record_activity(conn, mission["id"], "sent",
                         {"draft_id": draft_id,
                          "role_id": draft["role_id"]},
                         run_id=run_id)
        actions.append({"kind": "sent", "role_id": draft["role_id"],
                        "draft_id": draft_id})
        notes.append(f"sent draft {draft_id}")
    else:
        # bounced / no_reply: the mail log holds the outcome;
        # the run records it and moves on.
        _record_activity(conn, mission["id"], "failed",
                         {"draft_id": draft_id,
                          "reason": f"send outcome: {result.status}"},
                         run_id=run_id)
        notes.append(f"draft {draft_id} send outcome: {result.status}")


def _auto_approve_sweep(conn, mission: dict, run_id: str,
                        notes: list[str], actions: list[dict]) -> None:
    """Auto-satisfy the mission's live pending approvals where
    standing approval allows (Q1): auto_approve=true, the kind
    not in require_approval_for, within bounds/caps. Anything
    else is left for the user; over cap is a refusal (activity
    + audit, run continues). Vault fills are never
    auto-satisfied."""
    try:
        pending = _links().pending_approvals_for_mission(
            conn, mission["id"])
    except ImportError:
        notes.append("links module missing — auto-approve sweep skipped")
        return
    try:
        bounds_mod = _bounds()
        approvals_mod = _approvals()
    except ImportError:
        notes.append("bounds/approvals module missing — approvals left for the user")
        for approval in pending:
            _record_approval_needed_once(conn, mission, run_id, approval)
        return
    for approval in pending:
        action = approval.get("action")
        kind = {"submit": "apply", "mail_send": "send"}.get(action)
        if kind is None:
            # Vault fill (and anything unknown): the user decides.
            _record_approval_needed_once(conn, mission, run_id, approval)
            continue
        allowed, reason = bounds_mod.auto_approvable(conn, mission, kind)
        if not allowed:
            if reason in ("auto_approve_off", "requires_human_approval"):
                _record_approval_needed_once(conn, mission, run_id, approval)
                continue
            # Over cap → refused (activity + audit, run continues).
            _refuse(conn, mission, run_id, kind,
                    reason or "cap reached",
                    {"approval_id": approval["approval_id"]}, notes)
            continue
        if _fresh_status(conn, mission["id"]) != "active":
            notes.append("mission stopped mid-tick — auto-approve stopped")
            break
        try:
            approvals_mod.approve_pending(
                conn, approval["task_id"], approval["approval_id"],
                expect_draft_id=(
                    approval.get("draft_id") if kind == "send" else None
                ),
            )
        except (KeyError, ValueError) as exc:
            notes.append(
                f"auto-approve failed for {approval['approval_id']}: {exc}"
            )
            continue
        _record_activity(conn, mission["id"], "approved",
                         {"approval_id": approval["approval_id"],
                          "task_id": approval["task_id"],
                          "action": kind},
                         run_id=run_id)
        actions.append({"kind": "approved", "action": kind,
                        "approval_id": approval["approval_id"]})
        notes.append(
            f"auto-approved {kind} ({approval['approval_id']}) under standing approval"
        )
        if kind == "send":
            draft_id = approval.get("draft_id")
            draft = draft_mod.get_draft(conn, draft_id) if draft_id else None
            if draft is not None and draft["status"] == "draft":
                # The approval was for this draft's exact body —
                # approve it, then send under the minted grant.
                if draft_mod.approve_draft(conn, draft_id):
                    draft = draft_mod.get_draft(conn, draft_id)
            if draft is not None and draft["status"] == "approved":
                _send_with_grant(conn, mission, draft, run_id, notes, actions)


def _send_approved_drafts(conn, mission: dict, run_id: str,
                          notes: list[str], actions: list[dict]) -> None:
    """Send drafts the USER approved (via the workspace) whose
    grant is still live: the mission completes approved sends
    on its own ticks. Drafts without a live grant wait."""
    try:
        tasks = _links().tasks_for_mission(conn, mission["id"])
    except ImportError:
        return
    for task in tasks:
        for draft in draft_mod.list_drafts(conn, task["id"], status="approved"):
            _send_with_grant(conn, mission, draft, run_id, notes, actions)


# ----------------------------------------------------------------
# The run
# ----------------------------------------------------------------

def _mission_has_planned_task(conn, mission_id: str) -> bool:
    """Does the mission have a task in 'planned' status — i.e.
    browser work waiting for the extension?"""
    try:
        tasks = _links().tasks_for_mission(conn, mission_id)
    except ImportError:
        tasks = [
            dict(r) for r in conn.execute(
                "SELECT id, goal, status, created_at FROM tasks"
                " WHERE mission_id = ?",
                (mission_id,),
            ).fetchall()
        ]
    return any(t["status"] == "planned" for t in tasks)


def run_mission(conn, mission: dict, call_fn=None,
                gmail=None) -> dict:
    """Run one mission for one tick. Returns the mission_runs
    row. `call_fn` (planner) and `gmail` (connector) are
    injectable for tests; both default to the real ones."""
    mission_id = mission["id"]
    fresh = _get_mission(conn, mission_id)
    if fresh is None:
        return {"status": "skipped", "mission_id": mission_id,
                "reason": "mission not found"}
    if fresh["status"] != "active":
        return {"status": "skipped", "mission_id": mission_id,
                "reason": f"mission is {fresh['status']}"}
    mission = fresh
    config = mission.get("config") or {}
    bounds = mission.get("bounds") or {}
    now = _now()
    run = _start_run(conn, mission_id)
    run_id = run["id"]
    notes: list[str] = []
    status = "ok"
    last_error: str | None = None
    task_id: str | None = None
    # Pre-initialised so the failure handlers below can still
    # record what the run discovered (a failed run must not lose
    # its facts — discovery already happened).
    candidates: list[dict] = []
    actions: list[dict] = []
    try:
        _record_activity(conn, mission_id, "run_started",
                         {"run_id": run_id}, run_id=run_id)
        quiet = _in_quiet_hours(bounds, now)
        if quiet:
            notes.append(
                "quiet hours — action work deferred (monitoring still runs)"
            )
        # --- discovery (server-side) ---
        try:
            candidates = discovery.discover(conn, mission, gmail=gmail)
        except Exception as exc:  # noqa: BLE001 - transient failure, retry next tick
            raise _RunFailed(f"discovery failed: {exc}") from exc
        if (((mission["type"] == "job_hunt"
                and "gmail" in (config.get("sources") or []))
                or mission["type"] == "email_monitor")
                and not discovery.gmail_available(conn)):
            notes.append(
                "gmail_off: Gmail connector is off — offer-mail"
                " discovery skipped (not a failure)"
            )
        min_score = _min_score(mission)
        matched: list[dict] = []
        for candidate in candidates:
            detail = dict(candidate.get("detail") or {})
            detail["source"] = candidate.get("source")
            detail["kind"] = candidate.get("kind")
            if candidate["kind"] == "role":
                role = candidate.get("role") or {}
                detail["title"] = discovery.redact_candidate_text(
                    role.get("title") or ""
                )[:100]
                _record_activity(conn, mission_id, "discovered", detail,
                                 run_id=run_id)
                match = candidate.get("match") or {}
                if (match.get("fits")
                        and match.get("score", 0) >= min_score
                        and not candidate.get("needs_confirmation")):
                    matched.append(candidate)
                    _record_activity(
                        conn, mission_id, "matched",
                        {"role_id": detail.get("role_id"),
                         "score": match.get("score")},
                        run_id=run_id)
            elif candidate["kind"] == "price_event":
                _record_activity(conn, mission_id, "price_observed",
                                 detail, run_id=run_id)
        notes.append(
            f"discovered {len(candidates)} candidate(s),"
            f" {len(matched)} matched"
        )
        # --- settle completed applies from earlier runs ---
        settled = _settle_completed(conn, mission, run_id)
        actions = list(settled)
        # --- action phase (skipped in quiet hours) ---
        if not quiet:
            if _fresh_status(conn, mission_id) != "active":
                notes.append("mission stopped mid-tick — no actions taken")
            elif mission["type"] == "job_hunt":
                task_id, outcome = _act_job_hunt(
                    conn, mission, matched, run_id, call_fn, notes)
                actions.extend(outcome.get("actions", []))
            elif mission["type"] == "cold_mail":
                task_id, outcome = _act_cold_mail(
                    conn, mission, matched, run_id, notes)
                actions.extend(outcome.get("actions", []))
            elif mission["type"] == "price_watch":
                _task_id, outcome = _act_price_watch(
                    conn, mission, candidates, run_id, notes)
                actions.extend(outcome.get("actions", []))
            elif mission["type"] == "email_monitor":
                # PR #7: monitoring only — matches are recorded
                # (discovered/matched activity + facts above);
                # no task is spawned, nothing is applied or sent.
                notes.append(
                    "inbox watch: matches recorded, no actions taken"
                )
            if _fresh_status(conn, mission_id) == "active":
                _auto_approve_sweep(conn, mission, run_id, notes, actions)
                _send_approved_drafts(conn, mission, run_id, notes, actions)
        # --- facts (W5): never fails the run ---
        fact_note = _record_run_facts(conn, mission, candidates, actions)
        if fact_note:
            notes.append(fact_note)
        # --- browser wait? ---
        if (_mission_has_planned_task(conn, mission_id)
                and not extension_heartbeat_fresh(conn, _now())):
            prev_waiting = _consecutive_run_status_count(
                conn, mission_id, WAITING_STATUSES)
            status = (
                "stalled" if prev_waiting >= MAX_CONSECUTIVE_WAITING - 1
                else "waiting_for_extension"
            )
            _record_activity(conn, mission_id, "waiting_for_extension",
                             {"reason": "no extension heartbeat within"
                              f" {EXTENSION_STALE_S}s"},
                             run_id=run_id)
            notes.append(
                "waiting for the extension (open Chrome) — browser"
                " work is queued, nothing was pretended"
            )
    except _RunFailed as exc:
        status = "failed"
        last_error = discovery.redact_candidate_text(str(exc))[:200]
        _record_activity(conn, mission_id, "failed",
                         {"reason": last_error}, run_id=run_id)
        notes.append(f"run failed: {last_error}")
        # The run failed, but discovery may already have found
        # things — record those facts anyway (the helper is
        # exception-safe and dedups, so a success-path write is
        # never duplicated).
        fact_note = _record_run_facts(conn, mission, candidates, actions)
        if fact_note:
            notes.append(fact_note)
        _maybe_auto_pause(conn, mission, notes)
    except Exception as exc:  # noqa: BLE001 - an unexpected error fails the run, never the tick
        logger.exception("mission %s run crashed", mission_id)
        status = "failed"
        last_error = discovery.redact_candidate_text(str(exc))[:200]
        _record_activity(conn, mission_id, "failed",
                         {"reason": last_error}, run_id=run_id)
        notes.append(f"run failed: {last_error}")
        fact_note = _record_run_facts(conn, mission, candidates, actions)
        if fact_note:
            notes.append(fact_note)
        _maybe_auto_pause(conn, mission, notes)
    finally:
        ended = _end_run(conn, run_id, status,
                         "; ".join(notes) if notes else status, task_id)
        current = _get_mission(conn, mission_id)
        if current is not None:
            if current["status"] == "active":
                interval = max(15, int(
                    (current.get("config") or {}).get("interval_min") or 60
                ))
                next_run_at = _now() + interval * 60
            else:
                next_run_at = None
            _update_mission_run_fields(
                conn, mission_id, last_run_at=_now(),
                next_run_at=next_run_at, last_run_status=status,
                last_error=last_error,
            )
    return ended


def _maybe_auto_pause(conn, mission: dict, notes: list[str]) -> None:
    """3 consecutive failed runs → auto-pause (a mission must
    not burn quota forever on a broken config)."""
    prev_failed = _consecutive_run_status_count(
        conn, mission["id"], ("failed",))
    if prev_failed < MAX_CONSECUTIVE_FAILURES - 1:
        return
    if _fresh_status(conn, mission["id"]) != "active":
        return
    _transition(conn, mission["id"], "paused")
    audit.append(conn, None, "mission.auto_paused",
                 {"mission_id": mission["id"],
                  "reason": f"{MAX_CONSECUTIVE_FAILURES} consecutive failed runs"})
    notes.append(
        f"auto-paused after {MAX_CONSECUTIVE_FAILURES} consecutive failed runs"
    )


# ----------------------------------------------------------------
# Briefing trigger (§3.9): the tick is the backstop when Chrome
# is closed. Build at most one briefing per IST calendar day.
# ----------------------------------------------------------------

def _maybe_build_briefing(conn, now: int) -> bool:
    today = _ist_day_string(now)
    if _memory_get(conn, LAST_BRIEFING_DAY_KEY) == today:
        return False
    nine = datetime.fromtimestamp(now, tz=IST).replace(
        hour=BRIEFING_HOUR_IST, minute=0, second=0, microsecond=0)
    if now < int(nine.timestamp()):
        return False
    try:
        from ..monitor.briefing import build_briefing

        build_briefing(conn)
    except Exception as exc:  # noqa: BLE001 - a failed briefing never fails the tick
        logger.warning("mission tick briefing build failed: %s", exc)
        return False
    # The day guard (W3's build_briefing sets the same key; this
    # set makes the guard hold even before that lands).
    _memory_set(conn, LAST_BRIEFING_DAY_KEY, today)
    logger.info("mission tick built the %s briefing", today)
    return True


# ----------------------------------------------------------------
# The tick
# ----------------------------------------------------------------

def tick_once(conn, call_fn=None, gmail=None) -> dict:
    """One tick, synchronously: reset stale executing steps,
    run every due active mission, check the briefing rule.
    Returns {"missions_run", "runs", "briefing_built"}."""
    now = _now()
    _reset_stale_executing(conn, now)
    rows = conn.execute(
        "SELECT id FROM missions WHERE status = 'active'"
        " AND next_run_at IS NOT NULL AND next_run_at <= ?"
        " ORDER BY next_run_at ASC",
        (now,),
    ).fetchall()
    runs = []
    for row in rows:
        mission = _get_mission(conn, row["id"])
        if mission is None:
            continue
        try:
            runs.append(run_mission(conn, mission, call_fn=call_fn,
                                    gmail=gmail))
        except Exception as exc:  # noqa: BLE001 - one mission never kills the tick
            logger.exception("mission %s tick crashed", row["id"])
            runs.append({"mission_id": row["id"], "status": "error",
                         "error": str(exc)[:200]})
    briefing_built = _maybe_build_briefing(conn, now)
    return {"missions_run": len(runs), "runs": runs,
            "briefing_built": briefing_built}


def _run_loop() -> None:
    while not _stop_event.is_set():
        try:
            conn = db.connect()
            try:
                tick_once(conn)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - the loop never dies on a tick error
            logger.exception("mission runner tick failed")
        _stop_event.wait(TICK_INTERVAL_S)


def start_runner() -> None:
    """Start the mission runner thread (idempotent). Called from
    main.py lifespan (W1 adds the call)."""
    global _thread
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(
            target=_run_loop, name="mission-runner", daemon=True)
        _thread.start()
        logger.info("mission runner started (tick every %ds)",
                    TICK_INTERVAL_S)


def stop_runner() -> None:
    """Stop the runner thread (tests / shutdown)."""
    _stop_event.set()
    global _thread
    thread, _thread = _thread, None
    if thread is not None:
        thread.join(timeout=5)


# ----------------------------------------------------------------
# Due browser jobs (GET /missions/due, in missions_runner_api.py)
# ----------------------------------------------------------------

def _role_url_from_task(conn, task_id: str) -> str | None:
    """The role URL from a task's constraints ("url: ..." line)
    — the page the driver must find a tab for."""
    task = loop.get_task(conn, task_id)
    if task is None:
        return None
    for line in (task.get("constraints") or "").splitlines():
        if line.strip().lower().startswith("url:"):
            return line.split(":", 1)[1].strip() or None
    return None


def _next_pending_step(conn, task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT n FROM steps WHERE task_id = ? AND status = 'pending'"
        " ORDER BY n ASC LIMIT 1",
        (task_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def due_browser_jobs(conn, now: int | None = None) -> list[dict]:
    """Browser work waiting for the extension, across active
    missions: one job per planned task (its next pending step)
    plus due watch checks for price missions (belt and braces
    alongside the watch alarms). An 'ask_user' task has no job
    — it waits on approval, not on the browser."""
    now = _now() if now is None else now
    jobs: list[dict] = []
    try:
        missions = _models().list_missions(conn, status="active")
    except ImportError:
        missions = [
            _parse_mission_row(r) for r in conn.execute(
                "SELECT * FROM missions WHERE status = 'active'"
                " ORDER BY created_at ASC"
            ).fetchall()
        ]
    for mission in missions:
        try:
            tasks = _links().tasks_for_mission(conn, mission["id"])
        except ImportError:
            tasks = [
                dict(r) for r in conn.execute(
                    "SELECT id, goal, status, created_at FROM tasks"
                    " WHERE mission_id = ? ORDER BY created_at DESC",
                    (mission["id"],),
                ).fetchall()
            ]
        for task in tasks:
            if task["status"] != "planned":
                continue
            step = _next_pending_step(conn, task["id"])
            if step is None:
                continue
            jobs.append({
                "mission_id": mission["id"], "type": mission["type"],
                "mission_name": mission.get("name") or mission["id"],
                "job": {
                    "kind": "execute_step",
                    "task_id": task["id"], "step": step["n"],
                    "url": _role_url_from_task(conn, task["id"]),
                },
            })
        if mission["type"] == "price_watch":
            watch_id = (mission.get("config") or {}).get("watch_id")
            if not watch_id:
                continue
            watch = watch_mod.get_watch(conn, watch_id)
            if watch is None or watch["status"] != "active":
                continue
            last_checked = watch.get("last_checked")
            interval_s = int(watch.get("interval_min") or 60) * 60
            if last_checked is None or last_checked + interval_s <= now:
                jobs.append({
                    "mission_id": mission["id"], "type": mission["type"],
                    "mission_name": mission.get("name") or mission["id"],
                    "job": {
                        "kind": "watch_check",
                        "watch_id": watch_id,
                        "url": watch["url"],
                        "selector": watch["selector"],
                    },
                })
    return jobs
