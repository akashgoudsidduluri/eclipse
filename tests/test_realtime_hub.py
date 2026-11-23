"""Realtime hub + live driver tests (PR #7, W3).

No real browser, no real socket: the hub is exercised with fake
WebSocket objects, and the driver with a FakeHub that answers
init_session / observe / execute from canned observations. The
full "open google" drive runs end to end against a temp SQLite
database — deterministic plan, no planner, no keys.
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server.api import realtime_api  # noqa: E402
from server.core import config, db  # noqa: E402
from server.realtime import driver as driver_mod  # noqa: E402
from server.realtime.hub import (  # noqa: E402
    ExtensionDisconnected,
    Hub,
)
from server.tasks import loop  # noqa: E402


class FakeSocket:
    """A stand-in WebSocket: records sent messages, never delivers."""

    def __init__(self, fail_send: bool = False):
        self.sent: list[dict] = []
        self.fail_send = fail_send

    async def send_json(self, message: dict) -> None:
        if self.fail_send:
            raise ConnectionError("socket is dead")
        self.sent.append(message)


class TestHub(unittest.IsolatedAsyncioTestCase):
    async def test_send_action_future_matching(self):
        hub = Hub()
        hub.register_extension(FakeSocket())
        send_task = asyncio.create_task(
            hub.send_action("task-1", "observe", {})
        )
        await asyncio.sleep(0)  # let send_action register its slot
        self.assertEqual(hub.in_flight_actions, 1)
        action_id = hub._action_results.keys()
        self.assertEqual(len(list(action_id)), 1)
        only_id = next(iter(hub._action_results))
        hub.resolve_action(only_id, {"ok": True, "observation": {"url": "x"}})
        result = await send_task
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["observation"]["url"], "x")
        self.assertEqual(hub.in_flight_actions, 0)

    async def test_send_action_timeout(self):
        hub = Hub()
        hub.register_extension(FakeSocket())
        result = await hub.send_action("task-1", "observe", {}, timeout_s=0.05)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TIMEOUT")
        self.assertTrue(result["error"]["recoverable"])
        # The slot is cleaned up exactly once.
        self.assertEqual(hub.in_flight_actions, 0)

    async def test_disconnect_fails_in_flight(self):
        hub = Hub()
        sock = FakeSocket()
        hub.register_extension(sock)
        send_task = asyncio.create_task(
            hub.send_action("task-1", "observe", {}, timeout_s=30)
        )
        await asyncio.sleep(0)
        self.assertEqual(hub.in_flight_actions, 1)
        hub.drop_extension(sock)
        result = await send_task
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "EXTENSION_DISCONNECTED")
        self.assertEqual(hub.in_flight_actions, 0)
        self.assertFalse(hub.is_extension_connected)

    async def test_send_action_no_socket_raises(self):
        hub = Hub()
        with self.assertRaises(ExtensionDisconnected):
            await hub.send_action("task-1", "observe", {})

    async def test_send_falls_back_to_older_socket(self):
        hub = Hub()
        good = FakeSocket()
        dead = FakeSocket(fail_send=True)
        hub.register_extension(good)
        hub.register_extension(dead)  # newest first — dead
        send_task = asyncio.create_task(
            hub.send_action("task-1", "observe", {}, timeout_s=0.2)
        )
        result = await send_task
        # The dead socket was dropped, the good one got the message,
        # and the wait timed out (nobody resolved it).
        self.assertEqual(result["error"]["code"], "TIMEOUT")
        self.assertEqual(len(good.sent), 1)
        self.assertEqual(hub.extension_count, 1)

    async def test_send_failure_on_all_sockets_raises(self):
        hub = Hub()
        hub.register_extension(FakeSocket(fail_send=True))
        with self.assertRaises(ExtensionDisconnected):
            await hub.send_action("task-1", "observe", {})
        self.assertEqual(hub.in_flight_actions, 0)

    async def test_abort_task_actions(self):
        hub = Hub()
        hub.register_extension(FakeSocket())
        task_a = asyncio.create_task(
            hub.send_action("task-a", "observe", {}, timeout_s=30)
        )
        task_b = asyncio.create_task(
            hub.send_action("task-b", "observe", {}, timeout_s=30)
        )
        await asyncio.sleep(0)
        self.assertEqual(hub.in_flight_actions, 2)
        hub.abort_task_actions("task-a")
        result_a = await task_a
        self.assertFalse(result_a["ok"])
        self.assertEqual(result_a["error"]["code"], "CANCELLED")
        self.assertEqual(hub.in_flight_actions, 1)
        task_b.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task_b

    def test_resolve_unknown_id_is_a_noop(self):
        hub = Hub()
        hub.resolve_action("no-such-id", {"ok": True})  # must not raise

    def test_tab_registry(self):
        hub = Hub()
        self.assertIsNone(hub.tab_for_task("task-1"))
        hub.register_tab("task-1", 42, "https://www.google.com", "Google")
        entry = hub.tab_for_task("task-1")
        self.assertEqual(entry["tab_id"], 42)
        self.assertEqual(entry["url"], "https://www.google.com")
        hub.set_tab_url("task-1", "https://www.youtube.com")
        self.assertEqual(hub.tab_for_task("task-1")["url"],
                         "https://www.youtube.com")
        snapshot = hub.tabs_snapshot()
        self.assertIn("task-1", snapshot)
        snapshot["task-1"]["tab_id"] = 999  # a copy, not the registry
        self.assertEqual(hub.tab_for_task("task-1")["tab_id"], 42)
        self.assertEqual(hub.drop_tab(42), "task-1")
        self.assertIsNone(hub.tab_for_task("task-1"))
        self.assertIsNone(hub.drop_tab(42))
        hub.register_tab("task-1", 7)
        hub.drop_task_tabs("task-1")
        self.assertIsNone(hub.tab_for_task("task-1"))

    def test_drive_lock_is_stable_per_task(self):
        hub = Hub()
        self.assertIs(hub.drive_lock("a"), hub.drive_lock("a"))
        self.assertIsNot(hub.drive_lock("a"), hub.drive_lock("b"))


class TestWsToken(unittest.TestCase):
    def setUp(self):
        self._orig_token = config._token
        config._token = "test-token-ws"

    def tearDown(self):
        config._token = self._orig_token

    def test_valid_token(self):
        self.assertTrue(realtime_api.is_valid_ws_token("test-token-ws"))

    def test_invalid_tokens(self):
        self.assertFalse(realtime_api.is_valid_ws_token("wrong"))
        self.assertFalse(realtime_api.is_valid_ws_token(""))
        self.assertFalse(realtime_api.is_valid_ws_token(None))


class FakeHub(Hub):
    """A Hub whose send_action answers from canned observations —
    a fake browser for the driver. Observations are served in
    order: the first observe gets observations[0], the next gets
    observations[1], and so on (the last one repeats)."""

    def __init__(self, observations: list[dict], perform_ok: bool = True):
        super().__init__()
        self.observations = observations
        self.perform_ok = perform_ok
        self.calls: list[dict] = []
        self._obs_index = 0
        self.register_extension(FakeSocket())

    async def send_action(self, task_id, action_type, payload=None,
                          timeout_s=45.0):
        self.calls.append({"task_id": task_id, "type": action_type,
                           "payload": payload})
        if action_type == "init_session":
            return {"ok": True, "tab_id": 7,
                    "url": (payload or {}).get("url") or ""}
        if action_type == "observe":
            obs = self.observations[
                min(self._obs_index, len(self.observations) - 1)
            ]
            self._obs_index += 1
            return {"ok": True, "observation": obs}
        if action_type == "execute":
            if self.perform_ok:
                return {"ok": True, "url": (payload or {}).get("action", {}).get("url", "")}
            return {"ok": False, "detail": "tab closed mid-step"}
        if action_type == "close_session":
            return {"ok": True}
        return {"ok": False, "detail": f"unknown {action_type}"}


def _never_call(prompt, **kwargs):
    raise AssertionError("the planner must not be called for this goal")


class TestDriver(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_data_dir = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._orig_data_dir
        self.tmp.cleanup()

    def _planned_open_google(self) -> str:
        task_id = loop.create_task(self.conn, "open google")
        loop.plan_task(self.conn, task_id, call_fn=_never_call)
        return task_id

    async def test_drive_open_google_end_to_end(self):
        task_id = self._planned_open_google()
        hub = FakeHub([
            {"url": "chrome://newtab/", "elements": []},
            {"url": "https://www.google.com", "elements": []},
        ])
        result = await driver_mod.drive_task(hub, task_id)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["steps_driven"], 1)
        task = loop.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "done")
        # The drive opened a tab, observed twice, executed once.
        types = [c["type"] for c in hub.calls]
        self.assertEqual(types, ["init_session", "observe", "execute", "observe"])
        # The executed action was the deterministic navigate.
        executed = next(c for c in hub.calls if c["type"] == "execute")
        self.assertEqual(executed["payload"]["action"], {
            "action": "navigate", "url": "https://www.google.com",
        })
        # The tab is registered (and left open — no close_session).
        self.assertEqual(hub.tab_for_task(task_id)["tab_id"], 7)

    async def test_drive_step_returns_verified(self):
        task_id = self._planned_open_google()
        hub = FakeHub([
            {"url": "", "elements": []},
            {"url": "https://www.google.com", "elements": []},
        ])
        result = await driver_mod.drive_step(hub, task_id)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["step"], 1)

    async def test_drive_offline_extension(self):
        task_id = self._planned_open_google()
        hub = Hub()  # no extension registered
        result = await driver_mod.drive_step(hub, task_id)
        self.assertEqual(result["status"], "extension_offline")
        task = loop.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "planned")

    async def test_drive_skips_mission_tasks(self):
        task_id = self._planned_open_google()
        self.conn.execute(
            "UPDATE tasks SET mission_id = ? WHERE id = ?",
            ("mission-1", task_id),
        )
        self.conn.commit()
        hub = FakeHub([{"url": "", "elements": []}])
        result = await driver_mod.drive_step(hub, task_id)
        self.assertEqual(result["status"], "mission_task")
        self.assertEqual(hub.calls, [])

    async def test_drive_no_such_task(self):
        hub = FakeHub([{"url": "", "elements": []}])
        result = await driver_mod.drive_step(hub, "no-such-task")
        self.assertEqual(result["status"], "no_task")

    async def test_drive_perform_failure_resets_step(self):
        task_id = self._planned_open_google()
        hub = FakeHub(
            [{"url": "", "elements": []}],
            perform_ok=False,
        )
        result = await driver_mod.drive_step(hub, task_id)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["retries"], 1)
        step = self.conn.execute(
            "SELECT status, retries FROM steps WHERE task_id = ? AND n = 1",
            (task_id,),
        ).fetchone()
        # Reset to pending so the next sweep retries it.
        self.assertEqual(step["status"], "pending")
        self.assertEqual(step["retries"], 1)

    async def test_drive_perform_failure_three_times_fails_task(self):
        task_id = self._planned_open_google()
        hub = FakeHub(
            [{"url": "", "elements": []}],
            perform_ok=False,
        )
        for i in range(3):
            result = await driver_mod.drive_step(hub, task_id)
        self.assertEqual(result["status"], "failed")
        task = loop.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "failed")

    async def test_drive_pending_tasks_sweep(self):
        task_a = self._planned_open_google()
        task_b = loop.create_task(self.conn, "open youtube")
        loop.plan_task(self.conn, task_b, call_fn=_never_call)
        # One shared fake browser: both tasks observe a blank tab
        # then their target. The observations repeat per task
        # because each drive re-observes; serve a blank tab first
        # and a "reached" URL second for each — the second
        # observation's URL only needs to differ from the first
        # for verify's URL-change fallback... but the navigate
        # check needs the target host. Serve per-task sequences
        # via a hub that answers from the task's own plan.
        hub = FakeHub([
            {"url": "", "elements": []},
            {"url": "https://www.google.com", "elements": []},
            {"url": "", "elements": []},
            {"url": "https://www.youtube.com", "elements": []},
        ])
        summary = await driver_mod.drive_pending_tasks(hub)
        self.assertEqual(sorted(summary["tasks"]), sorted([task_a, task_b]))
        self.assertEqual(loop.get_task(self.conn, task_a)["status"], "done")
        self.assertEqual(loop.get_task(self.conn, task_b)["status"], "done")

    async def test_drive_pending_tasks_offline_is_noop(self):
        self._planned_open_google()
        hub = Hub()
        summary = await driver_mod.drive_pending_tasks(hub)
        self.assertEqual(summary, {"tasks": [], "results": {}})

    def test_execute_failure_parses_approval_id(self):
        result = driver_mod._execute_failure(
            "task-1", 1,
            ValueError(
                "step 1 needs approval before submit"
                " (POST /tasks/{id}/approve with approval_id"
                " appr-0123456789ab)"
            ),
        )
        self.assertEqual(result["status"], "approval_needed")
        self.assertEqual(result["approval_id"], "appr-0123456789ab")

    def test_execute_failure_plain_error(self):
        result = driver_mod._execute_failure(
            "task-1", 1, ValueError("already applied to role_1")
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("already applied", result["detail"])


if __name__ == "__main__":
    unittest.main()
