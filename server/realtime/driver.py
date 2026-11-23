"""Live driver for chat tasks (PR #7, W3): the server-side
observe -> execute -> verify loop, over the WebSocket hub.

Mirrors how the missions driver drives mission jobs
(extension/src/missions.ts: observe -> POST execute -> perform
-> re-observe -> POST verify), but server-driven and for ANY
task — a chat-created task has no mission, so the missions
alarm never picks it up. The driver runs the SAME loop functions
the HTTP endpoints run (loop.execute_step / loop.verify_step),
so every privacy gate holds: redact before planner, fail-closed
validation, the human gate for submits (an approval-needed step
stops the drive in ask_user; the user approves in the workspace
and the sweep resumes the task), never re-execute a done step.

Boundaries:
- Mission-linked tasks (mission_id set) are NOT driven here —
  the missions alarm owns them. Driving both ways would double
  -drive one task.
- Tasks stay in SQLite; the hub keeps only live sockets and
  the ephemeral tab registry. A restart loses tabs, never tasks.
- The driver never logs observation contents (ids and counts
  only) — observations can carry page text.
- The driver loop is a 30s asyncio sweep (started from
  main.py's lifespan, next to the missions runner thread). A
  freshly planned task also gets an immediate drive from the
  plan endpoint (BackgroundTasks), so "open google" starts
  moving at once instead of waiting for the next sweep; the
  per-task lock makes the two triggers safe to overlap.

A step whose browser action fails (tab closed mid-step, action
rejected) is reset to pending with its retry count bumped —
the same 3-retry rule verify_step applies — so the next sweep
retries it instead of stranding the task in "executing".
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time

from ..core import audit, db
from ..planner import validate
from ..tasks import loop
from .hub import ExtensionDisconnected, Hub

logger = logging.getLogger("eclipse.realtime")

# The sweep interval (the missions runner ticks every 60s; chat
# tasks should start faster than that, but a sweep is a cheap
# SQLite query when nothing is planned).
DRIVER_INTERVAL_S = 30.0
# An "executing" step older than this belongs to a drive that
# died mid-step (server restart, service worker killed) — the
# same 1-hour rule the missions runner uses.
STALE_EXECUTING_S = 3600

# Terminal + paused states: the driver never touches these
# ("done" reports as its own status, not "stopped").
_STOP_STATUSES = ("cancelled", "failed", "paused")

_APPROVAL_ID_RE = re.compile(r"approval_id (appr-[0-9a-f]+)")

# The driver loop task (one per process, owned by main.py).
_driver_task: asyncio.Task | None = None


# ----------------------------------------------------------------
# Sync SQLite helpers (each opens its own connection — the driver
# runs on the event loop, so every blocking call goes through
# asyncio.to_thread with a fresh connection per call).
# ----------------------------------------------------------------

def _load_task(task_id: str) -> dict | None:
    conn = db.connect()
    try:
        return loop.get_task(conn, task_id)
    finally:
        conn.close()


def _next_open_step(task_id: str) -> dict | None:
    """The next step to drive: the lowest-n step that is pending
    OR executing. "Executing" here means a verify-failed retry:
    verify_step leaves a retried step "executing" (it only flips
    to failed on the 3rd retry), so a pending-only query would
    strand it forever. Re-driving it inside the per-task lock is
    safe — no other drive of this task can be mid-step — and
    execute_step re-plans it with the previous attempt in the
    prompt (its repeat guards still apply)."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT n FROM steps WHERE task_id = ?"
            " AND status IN ('pending', 'executing')"
            " ORDER BY n ASC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _url_from_constraints(constraints: str) -> str | None:
    """The "url:" line from a task's constraints — the page the
    driver should open the task's tab at. Same rule as
    missions/runner._role_url_from_task (kept local, not
    imported — the runner module is mission-owned)."""
    for line in (constraints or "").splitlines():
        if line.strip().lower().startswith("url:"):
            return line.split(":", 1)[1].strip() or None
    return None


def _execute(task_id: str, n: int, observation: dict) -> dict:
    conn = db.connect()
    try:
        return loop.execute_step(conn, task_id, n, observation)
    finally:
        conn.close()


def _verify(task_id: str, n: int, new_observation: dict,
            expected_end_state: str, previous_url: str,
            previous_observation: dict) -> dict:
    conn = db.connect()
    try:
        return loop.verify_step(
            conn, task_id, n, new_observation, expected_end_state,
            previous_url, previous_observation,
        )
    finally:
        conn.close()


def _reset_step_after_perform_failure(
    task_id: str, n: int, detail: str,
) -> dict:
    """A browser-side action failure (tab closed, executor
    refusal) leaves the step "executing" — reset it to pending
    with the retry count bumped, so the next sweep retries it.
    After 3 retries the step fails and the task fails, mirroring
    verify_step's terminal branch (same MAX_RETRIES_PER_STEP).
    Returns {"retries": n, "failed": bool}."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT retries FROM steps WHERE task_id = ? AND n = ?",
            (task_id, n),
        ).fetchone()
        retries = int(row["retries"] or 0) + 1 if row else 1
        if retries >= loop.MAX_RETRIES_PER_STEP:
            conn.execute(
                "UPDATE steps SET status = 'failed', error = ?, retries = ?"
                " WHERE task_id = ? AND n = ?",
                (f"browser action failed after {retries} retries: {detail}",
                 retries, task_id, n),
            )
            conn.execute(
                "UPDATE tasks SET status = 'failed', updated_at = ?"
                " WHERE id = ?",
                (int(time.time()), task_id),
            )
            conn.commit()
            audit.append(conn, task_id, "task.drive_failed",
                         {"step": n, "retries": retries})
            return {"retries": retries, "failed": True}
        conn.execute(
            "UPDATE steps SET status = 'pending', error = ?, retries = ?"
            " WHERE task_id = ? AND n = ?",
            (detail, retries, task_id, n),
        )
        conn.commit()
        audit.append(conn, task_id, "task.drive_retry",
                     {"step": n, "retries": retries})
        return {"retries": retries, "failed": False}
    finally:
        conn.close()


def _reset_stale_executing(now: int | None = None) -> int:
    """Steps left "executing" for over an hour belong to a drive
    that died mid-step — reset them to pending (the missions
    runner's rule, applied to chat tasks too)."""
    now = int(time.time()) if now is None else now
    conn = db.connect()
    try:
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
    finally:
        conn.close()


def _planned_chat_tasks() -> list[str]:
    """Ids of planned tasks with no mission (chat tasks) that
    have an open step (pending, or executing after a verify
    retry — see _next_open_step). Mission tasks are excluded —
    the missions alarm owns them. A pre-PR #5 database has no
    mission_id column; the fallback drives every planned task
    (there can be no mission tasks on such a database)."""
    conn = db.connect()
    try:
        try:
            rows = conn.execute(
                "SELECT t.id FROM tasks t WHERE t.status = 'planned'"
                " AND t.mission_id IS NULL"
                " AND EXISTS (SELECT 1 FROM steps s WHERE s.task_id = t.id"
                " AND s.status IN ('pending', 'executing'))"
                " ORDER BY t.created_at ASC",
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT t.id FROM tasks t WHERE t.status = 'planned'"
                " AND EXISTS (SELECT 1 FROM steps s WHERE s.task_id = t.id"
                " AND s.status IN ('pending', 'executing'))"
                " ORDER BY t.created_at ASC",
            ).fetchall()
        return [r["id"] for r in rows]
    finally:
        conn.close()


def _is_mission_task(task: dict) -> bool:
    return bool(task.get("mission_id"))


# ----------------------------------------------------------------
# One step, over the hub.
# ----------------------------------------------------------------

async def _observe(hub: Hub, task_id: str) -> dict:
    """Observe the task's tab via the hub. Returns the
    observation dict ({url, elements}). Raises
    ExtensionDisconnected / RuntimeError on failure."""
    result = await hub.send_action(task_id, "observe", {})
    if not isinstance(result, dict) or not result.get("ok"):
        detail = ""
        if isinstance(result, dict):
            err = result.get("error") or {}
            detail = err.get("message") or result.get("detail") or ""
        raise RuntimeError(f"observe failed: {detail or result}")
    observation = result.get("observation")
    if not isinstance(observation, dict):
        raise RuntimeError(f"observe returned no observation: {result!r}"[:200])
    return observation


async def _ensure_tab(hub: Hub, task_id: str, start_url: str | None) -> dict:
    """Make sure the task has a registered tab (init_session
    opens one when needed). Returns the tab entry."""
    existing = hub.tab_for_task(task_id)
    if existing is not None:
        return existing
    payload: dict = {}
    if start_url:
        payload["url"] = start_url
    result = await hub.send_action(task_id, "init_session", payload)
    if not isinstance(result, dict) or not result.get("ok"):
        detail = ""
        if isinstance(result, dict):
            err = result.get("error") or {}
            detail = err.get("message") or result.get("detail") or ""
        raise RuntimeError(f"init_session failed: {detail or result}")
    tab_id = result.get("tab_id")
    if not isinstance(tab_id, int):
        raise RuntimeError(f"init_session returned no tab id: {result!r}"[:200])
    hub.register_tab(task_id, tab_id, result.get("url") or start_url or "")
    entry = hub.tab_for_task(task_id)
    assert entry is not None  # registered just above
    return entry


async def drive_step(hub: Hub, task_id: str) -> dict:
    """Drive ONE pending step of one task: ensure tab -> observe
    -> execute (one validated action) -> perform -> re-observe
    -> verify. Returns a status dict; never raises for an
    expected failure (approval, offline extension, bad step) —
    those are statuses the caller acts on. Only a bug raises.
    """
    task = await asyncio.to_thread(_load_task, task_id)
    if task is None:
        return {"status": "no_task", "task_id": task_id}
    if _is_mission_task(task):
        return {"status": "mission_task", "task_id": task_id}
    if task["status"] == "done":
        return {"status": "done", "task_id": task_id}
    if task["status"] in _STOP_STATUSES:
        return {"status": "stopped", "task_id": task_id,
                "task_status": task["status"]}
    if task["status"] == "ask_user":
        # Waiting on the human gate, not on the browser.
        return {"status": "approval_needed", "task_id": task_id}
    if task["status"] != "planned":
        return {"status": "not_planned", "task_id": task_id,
                "task_status": task["status"]}
    step = await asyncio.to_thread(_next_open_step, task_id)
    if step is None:
        return {"status": "no_pending", "task_id": task_id}
    n = step["n"]
    if not hub.is_extension_connected:
        return {"status": "extension_offline", "task_id": task_id}
    try:
        await _ensure_tab(hub, task_id, _url_from_constraints(task.get("constraints") or ""))
    except ExtensionDisconnected:
        return {"status": "extension_offline", "task_id": task_id}
    except RuntimeError as exc:
        return {"status": "error", "task_id": task_id, "step": n,
                "detail": str(exc)}
    # 1. Observe.
    try:
        observation = await _observe(hub, task_id)
    except ExtensionDisconnected:
        return {"status": "extension_offline", "task_id": task_id}
    except RuntimeError as exc:
        return {"status": "error", "task_id": task_id, "step": n,
                "detail": str(exc)}
    # 2. Execute (one validated action from the loop — the same
    # call the HTTP endpoint makes, so the gates are identical).
    try:
        exec_result = await asyncio.to_thread(_execute, task_id, n, observation)
    except loop.GroundingError:
        # Grounding failure (§6.3): re-observe once and re-plan
        # against the fresh observation — never twice.
        try:
            observation = await _observe(hub, task_id)
        except (ExtensionDisconnected, RuntimeError) as exc:
            if isinstance(exc, ExtensionDisconnected):
                return {"status": "extension_offline", "task_id": task_id}
            return {"status": "error", "task_id": task_id, "step": n,
                    "detail": str(exc)}
        try:
            exec_result = await asyncio.to_thread(_execute, task_id, n, observation)
        except loop.GroundingError as exc:
            return {"status": "error", "task_id": task_id, "step": n,
                    "detail": f"grounding failed twice: {exc}"}
        except (ValueError, validate.ValidationError) as exc:
            return _execute_failure(task_id, n, exc)
    except (ValueError, validate.ValidationError) as exc:
        return _execute_failure(task_id, n, exc)
    # 3. Perform the action in the tab (guarded executor).
    try:
        performed = await hub.send_action(task_id, "execute", {
            "action": exec_result["action"],
            "options": {
                "taskId": task_id,
                "allowedSites": exec_result.get("allowed_sites"),
                "expected": exec_result.get("element"),
                "fill": exec_result.get("fill"),
            },
        })
    except ExtensionDisconnected:
        await asyncio.to_thread(
            _reset_step_after_perform_failure, task_id, n,
            "extension disconnected during execute",
        )
        return {"status": "extension_offline", "task_id": task_id}
    if not isinstance(performed, dict) or not performed.get("ok"):
        detail = ""
        if isinstance(performed, dict):
            err = performed.get("error") or {}
            detail = err.get("message") or performed.get("detail") or ""
        outcome = await asyncio.to_thread(
            _reset_step_after_perform_failure, task_id, n,
            detail or "action failed in the browser",
        )
        return {"status": "failed" if outcome["failed"] else "error",
                "task_id": task_id, "step": n,
                "detail": detail or "action failed in the browser",
                "retries": outcome["retries"]}
    # 4. Re-observe and verify (baseline diff — the pre-action
    # observation is the baseline, so pre-existing text never
    # verifies a step that did nothing).
    try:
        after = await _observe(hub, task_id)
    except ExtensionDisconnected:
        return {"status": "extension_offline", "task_id": task_id}
    except RuntimeError as exc:
        return {"status": "error", "task_id": task_id, "step": n,
                "detail": str(exc)}
    verdict = await asyncio.to_thread(
        _verify, task_id, n, after,
        exec_result.get("expected_end_state", ""),
        observation.get("url", ""), observation,
    )
    if verdict.get("verified"):
        return {"status": "verified", "task_id": task_id, "step": n,
                "reason": verdict.get("reason", "")}
    if verdict.get("retry"):
        return {"status": "retry", "task_id": task_id, "step": n,
                "reason": verdict.get("reason", ""),
                "backoff_s": verdict.get("backoff_s", 1)}
    return {"status": "failed", "task_id": task_id, "step": n,
            "reason": verdict.get("reason", "")}


def _execute_failure(task_id: str, n: int, exc: Exception) -> dict:
    """Map an execute_step failure to a drive status. An
    approval-needed error (the human gate) is approval_needed
    with the approval id when the loop recorded one; anything
    else is an error (the loop already failed the step or set
    the task ask_user as appropriate)."""
    text = str(exc)
    if isinstance(exc, ValueError) and (
        "needs approval" in text or "approval needed" in text
    ):
        match = _APPROVAL_ID_RE.search(text)
        return {"status": "approval_needed", "task_id": task_id,
                "step": n, "detail": text,
                "approval_id": match.group(1) if match else None}
    return {"status": "error", "task_id": task_id, "step": n,
            "detail": text}


# ----------------------------------------------------------------
# Whole tasks + the sweep.
# ----------------------------------------------------------------

async def drive_task(hub: Hub, task_id: str,
                     max_steps: int = loop.MAX_STEPS_PER_TASK) -> dict:
    """Drive a task to completion (or to a stop): step after
    step until no pending step remains, the task stops, or the
    human gate / an error pauses it. The per-task lock means a
    second drive of the same task waits instead of doubling up.
    Returns the last step's status plus a step count."""
    lock = hub.drive_lock(task_id)
    async with lock:
        driven = 0
        last: dict = {"status": "no_pending", "task_id": task_id}
        for _ in range(max_steps):
            last = await drive_step(hub, task_id)
            status = last.get("status")
            if status == "verified":
                driven += 1
                continue
            if status == "retry":
                # Verify asked for a retry after a backoff — the
                # step is still executing, so re-drive it after
                # the backoff (execute_step re-plans it).
                backoff = last.get("backoff_s") or 1
                try:
                    await asyncio.sleep(min(float(backoff), 10))
                except (TypeError, ValueError):
                    await asyncio.sleep(1)
                driven += 1
                continue
            # Every other status stops the drive: approval, error,
            # offline extension, no pending step, stopped task.
            break
        return {**last, "steps_driven": driven}


async def drive_pending_tasks(hub: Hub) -> dict:
    """One sweep: reset stale executing steps, then drive every
    planned chat task with a pending step, sequentially. A
    no-op when nothing is planned or the extension is offline.
    Returns {"tasks": [...ids...], "results": {id: status}}."""
    if not hub.is_extension_connected:
        return {"tasks": [], "results": {}}
    await asyncio.to_thread(_reset_stale_executing)
    task_ids = await asyncio.to_thread(_planned_chat_tasks)
    results: dict[str, dict] = {}
    for task_id in task_ids:
        # A task already being driven (plan-triggered drive, or a
        # manual POST) is skipped, not queued behind the lock —
        # the sweep runs every 30s and will catch it next time.
        if hub.drive_lock(task_id).locked():
            continue
        try:
            results[task_id] = await drive_task(hub, task_id)
        except Exception as exc:  # noqa: BLE001 - one bad task must not abort the sweep
            logger.warning("drive sweep failed for task %s: %s", task_id, exc)
            results[task_id] = {"status": "error", "task_id": task_id,
                                "detail": str(exc)}
    return {"tasks": list(results), "results": results}


async def _driver_loop(hub: Hub, interval_s: float = DRIVER_INTERVAL_S) -> None:
    """The sweep loop: drive planned chat tasks every interval.
    Ticks never raise (a failed sweep logs and retries next
    tick) — the loop must survive anything short of shutdown."""
    while True:
        try:
            await drive_pending_tasks(hub)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must never die on a tick
            logger.warning("driver sweep failed: %s", exc)
        await asyncio.sleep(interval_s)


def start_driver_loop(hub: Hub, interval_s: float = DRIVER_INTERVAL_S) -> None:
    """Start the sweep loop (called from main.py's lifespan).
    Idempotent: a second start is a no-op."""
    global _driver_task
    if _driver_task is not None and not _driver_task.done():
        return
    _driver_task = asyncio.create_task(_driver_loop(hub, interval_s))
    logger.info("realtime driver loop started (every %.0fs)", interval_s)


def stop_driver_loop() -> None:
    """Stop the sweep loop (called from main.py's shutdown)."""
    global _driver_task
    if _driver_task is not None:
        _driver_task.cancel()
        _driver_task = None
