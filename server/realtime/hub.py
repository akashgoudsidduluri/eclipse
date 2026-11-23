"""WebSocket hub (PR #7, W3): routes actions to the live extension.

Architecture follows the PRIVAGENT pattern (backend/app/hub.py)
adapted to Eclipse: one hub, a list of live extension sockets
(newest preferred), one asyncio.Future per action id, and a
per-task tab registry. Eclipse differences: task records live
in SQLite, never in the hub (PRIVAGENT keeps TaskRecords
in-memory); there is no client/progress socket (the workspace
polls the existing task endpoints); and there are no cloud
providers or mock replies anywhere in this module.

Message protocol (server -> extension, each with a unique
action_id the extension echoes back in its result):
  init_session {task_id, url?}  -> {ok, tab_id, url}
      Open (or find) the task's tab. url is the tab's start
      URL when the server knows one, else the tab starts blank
      and the first step navigates it.
  observe {task_id}             -> {ok, observation}
      Observe the task's tab (walk + vision + redaction + gate,
      the existing observeTab path).
  execute {task_id, action, options} -> {ok, detail?, url?}
      Perform one validated action in the task's tab (the
      guarded executor; navigate goes via chrome.tabs.update
      so it works from a blank tab with no content script).
  close_session {task_id}       -> {ok}
      Drop the task's tab registration. The tab itself is left
      open — the user should see the result ("open google" must
      not close Google again).

Extension -> server:
  result {action_id, payload}   -> resolves the waiting future.
  hello {}                      -> the extension just connected;
      the API layer sweeps planned chat tasks for driving.
  tab_closed {tab_id}           -> drop the registry entry (the
      user closed the tab; the next drive re-opens it).
  heartbeat {}                  -> ignored (socket presence is
      the liveness signal).

Timeouts: an action that gets no result within timeout_s
resolves to {"ok": False, "error": {"code": "TIMEOUT", ...}}
(a recoverable error the driver treats like any other action
failure — the step is reset to pending, not failed outright).
A dropped socket fails every in-flight future at once with
EXTENSION_DISCONNECTED, so a drive never hangs on a dead
service worker.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

logger = logging.getLogger("eclipse.realtime")

# An action that gets no result in this long is a timeout, not a
# hang. 45s covers a slow page load + vision observe; the driver
# treats it as a retryable failure.
DEFAULT_ACTION_TIMEOUT_S = 45.0


class ExtensionDisconnected(RuntimeError):
    """No live extension socket (or the send failed)."""


class ActionResultSlot:
    """One in-flight extension action.

    The future is bound to the running loop (not
    get_event_loop, which can bind to a stale loop after an
    asyncio.run). Created only inside send_action, which always
    runs on the server's event loop.
    """

    def __init__(self, task_id: str = "") -> None:
        self.task_id = task_id
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()


class Hub:
    """Live extension sockets + per-action futures + tab registry."""

    def __init__(self) -> None:
        self.extension_sockets: list = []
        self._action_results: dict[str, ActionResultSlot] = {}
        # task_id -> {"tab_id": int, "url": str, "title": str,
        # "updated_at": int}. Ephemeral by nature: a restart loses
        # tabs, never tasks (tasks live in SQLite).
        self._tabs: dict[str, dict] = {}
        # One drive lock per task: the plan-triggered drive, the
        # 30s sweep and a manual POST /realtime/drive must never
        # drive the same task twice at once.
        self._drive_locks: dict[str, asyncio.Lock] = {}
        # Strong references to fire-and-forget tasks, so the loop
        # cannot garbage-collect them mid-flight.
        self._bg_tasks: set[asyncio.Task] = set()

    # -- extension sockets --------------------------------------

    @property
    def is_extension_connected(self) -> bool:
        return len(self.extension_sockets) > 0

    @property
    def extension_count(self) -> int:
        return len(self.extension_sockets)

    @property
    def in_flight_actions(self) -> int:
        return len(self._action_results)

    def register_extension(self, ws) -> None:
        if ws not in self.extension_sockets:
            self.extension_sockets.append(ws)
            logger.info(
                "extension connected (%d live)", len(self.extension_sockets)
            )

    def drop_extension(self, ws) -> None:
        """Drop a dead socket and fail its in-flight actions at
        once — a drive must never hang on a dead service worker."""
        if ws in self.extension_sockets:
            self.extension_sockets.remove(ws)
            logger.info(
                "extension disconnected (%d live)", len(self.extension_sockets)
            )
        if not self.extension_sockets:
            self._fail_all_in_flight("EXTENSION_DISCONNECTED")

    def _fail_all_in_flight(self, code: str) -> None:
        for action_id, slot in list(self._action_results.items()):
            self._action_results.pop(action_id, None)
            if not slot.future.done():
                slot.future.set_result({
                    "ok": False,
                    "error": {
                        "code": code,
                        "message": "extension disconnected",
                        "recoverable": True,
                    },
                })

    # -- actions -------------------------------------------------

    async def send_action(
        self, task_id: str, action_type: str, payload: dict | None = None,
        timeout_s: float = DEFAULT_ACTION_TIMEOUT_S,
    ) -> dict:
        """Send one action to the extension and await its result.

        Raises ExtensionDisconnected when no socket is live (or
        every send fails). Returns {"ok": False, "error":
        {"code": "TIMEOUT", ...}} on timeout — a recoverable
        error, not an exception, so the driver can retry the
        step. Every action cleans its slot up exactly once.
        """
        if not self.extension_sockets:
            raise ExtensionDisconnected(
                "extension not connected (is Chrome running with the"
                " Eclipse extension enabled?)"
            )
        action_id = uuid.uuid4().hex
        slot = ActionResultSlot(task_id)
        self._action_results[action_id] = slot
        message: dict = {
            "type": action_type,
            "action_id": action_id,
            "task_id": task_id,
        }
        if payload:
            message.update(payload)
        # Prefer the newest socket, fall back to older live ones
        # (a half-closed socket can linger in the registry).
        sent = False
        last_exc: Exception | None = None
        for ws in reversed(list(self.extension_sockets)):
            try:
                await ws.send_json(message)
                sent = True
                break
            except Exception as exc:  # noqa: BLE001 - any send failure means dead
                last_exc = exc
                self.drop_extension(ws)
        if not sent:
            self._action_results.pop(action_id, None)
            if not slot.future.done():
                slot.future.cancel()
            raise ExtensionDisconnected(f"send failed: {last_exc}")
        try:
            return await asyncio.wait_for(slot.future, timeout_s)
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": {
                    "code": "TIMEOUT",
                    "message": f"extension action {action_type} timed out",
                    "recoverable": True,
                },
            }
        except asyncio.CancelledError:
            if not slot.future.done():
                slot.future.cancel()
            raise
        finally:
            # Never leak result slots: every action cleans up once.
            self._action_results.pop(action_id, None)

    def resolve_action(self, action_id: str, payload: dict) -> None:
        """Match an extension result back to its waiting future.
        Unknown or already-resolved ids are ignored (a late
        result for a timed-out action is harmless)."""
        slot = self._action_results.pop(action_id, None)
        if slot is not None and not slot.future.done():
            slot.future.set_result(payload)

    def abort_task_actions(self, task_id: str) -> None:
        """Fail every in-flight action for a cancelled task at
        once — without this, send_action keeps waiting for the
        extension timeout and the loop only notices the cancel
        after the action finishes. The browser may already be
        executing that one action; the loop still stops before
        the next one."""
        for action_id, slot in list(self._action_results.items()):
            if slot.task_id != task_id:
                continue
            self._action_results.pop(action_id, None)
            if not slot.future.done():
                slot.future.set_result({
                    "ok": False,
                    "error": {
                        "code": "CANCELLED",
                        "message": "task cancelled",
                        "recoverable": False,
                    },
                })

    # -- per-task tab registry ------------------------------------

    def register_tab(
        self, task_id: str, tab_id: int, url: str = "", title: str = "",
    ) -> None:
        """Record (or refresh) a task's tab. One tab per task:
        init_session reuses the registered tab when it is still
        open, so a re-drive never opens a second tab."""
        self._tabs[task_id] = {
            "tab_id": tab_id, "url": url, "title": title,
            "updated_at": int(time.time()),
        }

    def tab_for_task(self, task_id: str) -> dict | None:
        entry = self._tabs.get(task_id)
        return dict(entry) if entry is not None else None

    def set_tab_url(self, task_id: str, url: str) -> None:
        if task_id in self._tabs:
            self._tabs[task_id]["url"] = url
            self._tabs[task_id]["updated_at"] = int(time.time())

    def drop_task_tabs(self, task_id: str) -> None:
        self._tabs.pop(task_id, None)

    def drop_tab(self, tab_id: int) -> str | None:
        """Drop the registry entry for a closed tab id. Returns
        the task it belonged to, or None."""
        for task_id, entry in list(self._tabs.items()):
            if entry.get("tab_id") == tab_id:
                del self._tabs[task_id]
                return task_id
        return None

    def tabs_snapshot(self) -> dict[str, dict]:
        """A copy of the tab registry (for /realtime/status)."""
        return {k: dict(v) for k, v in self._tabs.items()}

    # -- drive locks + background tasks ---------------------------

    def drive_lock(self, task_id: str) -> asyncio.Lock:
        """The one lock for a task's drive (created on demand).
        Locks are never removed: a task id is small, and
        removing a lock while another coroutine holds a
        reference to it would break mutual exclusion."""
        lock = self._drive_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._drive_locks[task_id] = lock
        return lock

    def spawn(self, coro) -> asyncio.Task:
        """Keep a strong reference to a fire-and-forget task so
        the event loop cannot garbage-collect it mid-flight."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task


# The server's one hub (main.py wires the API router to it; the
# driver loop and the plan endpoint drive through it).
hub = Hub()
