"""Mission runner tests (PR #5, W2): ticks, caps, approvals,
waiting-for-extension, auto-pause, briefing trigger, fact
wiring — plus resume storage/extraction and the runner API
routes (direct function calls; no TestClient in the venv).

Run: .venv/bin/python -m unittest tests.test_missions_runner -v
All data fake; the planner and Gmail are injected doubles.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
import zlib
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, Request

from server.api import missions_runner_api as runner_api
from server.api import missions_runner_api as runner_api_mod
from server.connectors.base import ConnectorError
from server.core import config, db
from server.jobs import profile as profile_mod
from server.mail import drafts as draft_mod
from server.missions import discovery, models, resume as resume_mod, runner
from server.missions import links as links_mod
from server.monitor import watches as watch_mod
from server.tasks import loop
from tests.test_missions_discovery import ROLE_FIT, FakeGmail, RaisingGmail

TOKEN = "eclipse-test-token"


def _fake_plan(prompt, local_only=False):
    return (json.dumps([
        {"step": 1, "description": "open the role page",
         "expected_end_state": "role page open"},
        {"step": 2, "description": "click apply",
         "expected_end_state": "application submitted"},
    ]), "fake:model", 1.0)


def _raising_plan(prompt, local_only=False):
    raise RuntimeError("planner exploded")


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_dir = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()
        profile_mod.store_profile(self.conn, dict(profile_mod.TEST_PROFILE))
        self.addCleanup(self.conn.close)
        self.addCleanup(setattr, config, "DATA_DIR", self._old_dir)
        self.addCleanup(self.tmp.cleanup)

    def _mission(self, mission_type="job_hunt", config=None, bounds=None,
                 activate=True):
        mission = models.create_mission(
            self.conn, mission_type, f"Test {mission_type}",
            config or {}, bounds or {},
        )
        if activate:
            mission = models.transition(self.conn, mission["id"], "active")
        return mission

    def _make_due(self, mission_id: str) -> None:
        self.conn.execute(
            "UPDATE missions SET next_run_at = ? WHERE id = ?",
            (int(time.time()) - 1, mission_id),
        )
        self.conn.commit()

    def _make_future(self, mission_id: str) -> None:
        self.conn.execute(
            "UPDATE missions SET next_run_at = ? WHERE id = ?",
            (int(time.time()) + 86400, mission_id),
        )
        self.conn.commit()

    def _set_heartbeat(self, age_s: int = 0) -> None:
        runner._memory_set(
            self.conn, runner.LAST_EXTENSION_SEEN_KEY,
            str(int(time.time()) - age_s))

    def _runs(self, mission_id: str) -> list[dict]:
        # rowid order = insertion order: several runs can share
        # one started_at second, so started_at alone misorders.
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM mission_runs WHERE mission_id = ?"
            " ORDER BY rowid ASC",
            (mission_id,),
        ).fetchall()]

    def _activities(self, mission_id: str, kind: str | None = None):
        # list_activity returns detail as the stored JSON string;
        # parse it so tests can read fields.
        rows = models.list_activity(self.conn, mission_id)
        parsed = []
        for row in rows:
            row = dict(row)
            try:
                row["detail"] = json.loads(row["detail"] or "{}")
            except (ValueError, TypeError):
                row["detail"] = {}
            parsed.append(row)
        if kind is None:
            return parsed
        return [r for r in parsed if r["kind"] == kind]

    def _tasks(self, mission_id: str) -> list[dict]:
        return links_mod.tasks_for_mission(self.conn, mission_id)


class TestTickAndDueSelection(RunnerTestCase):
    def test_due_selection_by_next_run_at(self):
        due = self._mission(config={"sources": ["pasted"], "roles": []})
        future = self._mission(config={"sources": ["pasted"], "roles": []})
        self._make_future(future["id"])
        runner.tick_once(self.conn, call_fn=_fake_plan)
        self.assertEqual(len(self._runs(due["id"])), 1)
        self.assertEqual(len(self._runs(future["id"])), 0)

    def test_paused_mission_never_runs(self):
        mission = self._mission(config={"sources": ["pasted"], "roles": []})
        models.transition(self.conn, mission["id"], "paused")
        runner.tick_once(self.conn, call_fn=_fake_plan)
        self.assertEqual(len(self._runs(mission["id"])), 0)

    def test_tick_reschedules_active_mission(self):
        mission = self._mission(config={"sources": ["pasted"], "roles": []})
        runner.tick_once(self.conn, call_fn=_fake_plan)
        fresh = models.get_mission(self.conn, mission["id"])
        self.assertGreater(fresh["next_run_at"], int(time.time()))
        self.assertEqual(fresh["last_run_status"], "ok")

    def test_start_runner_idempotent_and_stop(self):
        runner.start_runner()
        first = runner._thread
        runner.start_runner()
        self.assertIs(runner._thread, first)
        self.assertTrue(first.is_alive())
        runner.stop_runner()
        self.assertFalse(first.is_alive())


class TestWaitingForExtension(RunnerTestCase):
    def test_stale_heartbeat_means_waiting_not_error(self):
        mission = self._mission(config={
            "sources": ["pasted"], "roles": [ROLE_FIT]})
        # No heartbeat at all: the run spawns + plans the task,
        # then waits — it never pretends the apply happened.
        runner.tick_once(self.conn, call_fn=_fake_plan)
        runs = self._runs(mission["id"])
        self.assertEqual(runs[0]["status"], "waiting_for_extension")
        tasks = self._tasks(mission["id"])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "planned")
        waiting = self._activities(mission["id"], "waiting_for_extension")
        self.assertEqual(len(waiting), 1)

    def test_fresh_heartbeat_recovers(self):
        mission = self._mission(config={
            "sources": ["pasted"], "roles": [ROLE_FIT]})
        runner.tick_once(self.conn, call_fn=_fake_plan)
        self.assertEqual(self._runs(mission["id"])[0]["status"],
                         "waiting_for_extension")
        self._set_heartbeat(age_s=10)
        self._make_due(mission["id"])
        runner.tick_once(self.conn, call_fn=_fake_plan)
        runs = self._runs(mission["id"])
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[1]["status"], "ok")
        # The run did not spawn a second apply task.
        self.assertEqual(len(self._tasks(mission["id"])), 1)

    def test_waiting_does_not_count_toward_auto_pause(self):
        mission = self._mission(config={
            "sources": ["pasted"], "roles": [ROLE_FIT]})
        for _ in range(3):
            runner.tick_once(self.conn, call_fn=_fake_plan)
            self._make_due(mission["id"])
        fresh = models.get_mission(self.conn, mission["id"])
        self.assertEqual(fresh["status"], "active")
        runs = self._runs(mission["id"])
        self.assertEqual(runs[-1]["status"], "stalled")

    def test_due_jobs_list_planned_tasks(self):
        mission = self._mission(config={
            "sources": ["pasted"], "roles": [ROLE_FIT]})
        runner.tick_once(self.conn, call_fn=_fake_plan)
        jobs = runner.due_browser_jobs(self.conn)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["mission_id"], mission["id"])
        self.assertEqual(job["job"]["kind"], "execute_step")
        self.assertEqual(job["job"]["step"], 1)
        self.assertEqual(job["job"]["url"], ROLE_FIT["url"])


class TestFailuresAndAutoPause(RunnerTestCase):
    def test_three_consecutive_failures_auto_pause(self):
        mission = self._mission(config={"sources": ["gmail"]})
        for i in range(3):
            runner.tick_once(self.conn, call_fn=_fake_plan,
                             gmail=RaisingGmail())
            runs = self._runs(mission["id"])
            self.assertEqual(runs[-1]["status"], "failed")
            if i < 2:
                self._make_due(mission["id"])
        fresh = models.get_mission(self.conn, mission["id"])
        self.assertEqual(fresh["status"], "paused")
        self.assertIsNone(fresh["next_run_at"])
        audit_rows = self.conn.execute(
            "SELECT * FROM audit_log WHERE action = 'mission.auto_paused'"
        ).fetchall()
        self.assertEqual(len(audit_rows), 1)

    def test_failure_then_success_resets_the_count(self):
        mission = self._mission(config={"sources": ["gmail"]})
        runner.tick_once(self.conn, call_fn=_fake_plan, gmail=RaisingGmail())
        self._make_due(mission["id"])
        runner.tick_once(self.conn, call_fn=_fake_plan,
                         gmail=FakeGmail([]))
        # The second run was ok (no offers, no failure), so the
        # consecutive-failure count reset. One more failure does
        # not auto-pause.
        self._make_due(mission["id"])
        runner.tick_once(self.conn, call_fn=_fake_plan, gmail=RaisingGmail())
        fresh = models.get_mission(self.conn, mission["id"])
        self.assertEqual(fresh["status"], "active")

    def test_planner_failure_fails_the_run_not_the_tick(self):
        mission = self._mission(config={
            "sources": ["pasted"], "roles": [ROLE_FIT]})
        other = self._mission(config={"sources": ["pasted"], "roles": []})
        runner.tick_once(self.conn, call_fn=_raising_plan)
        self.assertEqual(self._runs(mission["id"])[0]["status"], "failed")
        self.assertEqual(self._runs(other["id"])[0]["status"], "ok")


class TestQuietHoursAndCaps(RunnerTestCase):
    def test_quiet_hours_skip_actions_but_monitor(self):
        now_ist = datetime.fromtimestamp(time.time(), tz=runner.IST)
        start = (now_ist.hour - 1) % 24
        end = (now_ist.hour + 1) % 24
        mission = self._mission(
            config={"sources": ["pasted"], "roles": [ROLE_FIT]},
            bounds={"quiet_hours_ist": {
                "start": f"{start:02d}:00", "end": f"{end:02d}:00"}})
        runner.tick_once(self.conn, call_fn=_fake_plan)
        # Discovery still ran; no apply task was spawned.
        self.assertEqual(len(self._activities(mission["id"], "discovered")), 1)
        self.assertEqual(len(self._tasks(mission["id"])), 0)
        self.assertEqual(self._runs(mission["id"])[0]["status"], "ok")

    def test_cap_refusal_records_activity_and_audit(self):
        mission = self._mission(
            config={"sources": ["pasted"], "roles": [ROLE_FIT]},
            bounds={"max_applies_per_day": 0})
        runner.tick_once(self.conn, call_fn=_fake_plan)
        refused = self._activities(mission["id"], "refused")
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["detail"]["attempted"], "apply")
        self.assertEqual(len(self._tasks(mission["id"])), 0)
        self.assertEqual(self._runs(mission["id"])[0]["status"], "ok")
        audit_rows = self.conn.execute(
            "SELECT * FROM audit_log WHERE action = 'mission.refused'"
        ).fetchall()
        self.assertEqual(len(audit_rows), 1)

    def test_stop_mid_tick_respected(self):
        mission = self._mission(config={
            "sources": ["pasted"], "roles": [ROLE_FIT]})
        real_discover = discovery.discover

        def _stopping_discover(conn, mission_dict, gmail=None):
            # The user hits the kill switch while discovery runs.
            conn.execute(
                "UPDATE missions SET status = 'stopped' WHERE id = ?",
                (mission_dict["id"],))
            conn.commit()
            return real_discover(conn, mission_dict, gmail=gmail)

        with mock.patch.object(discovery, "discover", _stopping_discover):
            runner.tick_once(self.conn, call_fn=_fake_plan)
        self.assertEqual(len(self._tasks(mission["id"])), 0)
        runs = self._runs(mission["id"])
        self.assertIn("stopped mid-tick", runs[0]["detail"])


class TestAutoApproveSweep(RunnerTestCase):
    def _pending_submit(self, mission_id: str, role_id: str = "role_fit"):
        task_id = loop.create_task(
            self.conn, "Apply to SDE Intern at Example Labs", "")
        links_mod.set_task_mission(self.conn, task_id, mission_id)
        approval_id = loop.record_pending_approval(
            self.conn, task_id, 2, "submit", role_id=role_id)
        return task_id, approval_id

    def test_auto_approve_satisfies_submit_under_standing_approval(self):
        mission = self._mission(
            config={"sources": ["pasted"], "roles": []},
            bounds={"auto_approve": True, "require_approval_for": []})
        task_id, approval_id = self._pending_submit(mission["id"])
        runner.tick_once(self.conn, call_fn=_fake_plan)
        self.assertIsNone(loop.get_pending_approval(self.conn, approval_id))
        grants = self.conn.execute(
            "SELECT * FROM grants WHERE origin = ?", (task_id,)).fetchall()
        self.assertEqual(len(grants), 1)
        approved = self._activities(mission["id"], "approved")
        self.assertEqual(len(approved), 1)

    def test_require_approval_for_blocks_auto_approve(self):
        mission = self._mission(
            config={"sources": ["pasted"], "roles": []},
            bounds={"auto_approve": True,
                    "require_approval_for": ["apply"]})
        _, approval_id = self._pending_submit(mission["id"])
        runner.tick_once(self.conn, call_fn=_fake_plan)
        self.assertIsNotNone(
            loop.get_pending_approval(self.conn, approval_id))
        needed = self._activities(mission["id"], "approval_needed")
        self.assertEqual(len(needed), 1)
        self.assertEqual(len(self._activities(mission["id"], "approved")), 0)

    def test_auto_approve_off_leaves_pending_for_the_user(self):
        mission = self._mission(
            config={"sources": ["pasted"], "roles": []},
            bounds={"auto_approve": False})
        _, approval_id = self._pending_submit(mission["id"])
        runner.tick_once(self.conn, call_fn=_fake_plan)
        self.assertIsNotNone(
            loop.get_pending_approval(self.conn, approval_id))
        self.assertEqual(len(self._activities(mission["id"], "approved")), 0)

    def test_over_cap_refuses_the_pending_approval(self):
        mission = self._mission(
            config={"sources": ["pasted"], "roles": []},
            bounds={"auto_approve": True, "require_approval_for": [],
                    "max_applies_per_day": 0})
        _, approval_id = self._pending_submit(mission["id"])
        runner.tick_once(self.conn, call_fn=_fake_plan)
        # Refused, not consumed — the user can still approve it.
        self.assertIsNotNone(
            loop.get_pending_approval(self.conn, approval_id))
        refused = self._activities(mission["id"], "refused")
        self.assertEqual(len(refused), 1)
        self.assertEqual(len(self._activities(mission["id"], "approved")), 0)

    def test_vault_fill_is_never_auto_satisfied(self):
        mission = self._mission(
            config={"sources": ["pasted"], "roles": []},
            bounds={"auto_approve": True, "require_approval_for": []})
        task_id = loop.create_task(self.conn, "Apply to SDE Intern", "")
        links_mod.set_task_mission(self.conn, task_id, mission["id"])
        approval_id = loop.record_pending_approval(
            self.conn, task_id, 1, "fill")
        runner.tick_once(self.conn, call_fn=_fake_plan)
        self.assertIsNotNone(
            loop.get_pending_approval(self.conn, approval_id))
        self.assertEqual(len(self._activities(mission["id"], "approved")), 0)


class TestColdMailFlow(RunnerTestCase):
    def _mail_mission(self, **bounds):
        base = {"auto_approve": True, "require_approval_for": []}
        base.update(bounds)
        return self._mission(
            "cold_mail",
            config={"recipients": ["recruiter@example.com"],
                    "roles": [ROLE_FIT]},
            bounds=base,
        )

    @mock.patch.dict(os.environ, {"ECLIPSE_MAIL_TRANSPORT": "mock"})
    def test_mail_draft_auto_approved_and_sent(self):
        mission = self._mail_mission()
        runner.tick_once(self.conn, call_fn=_fake_plan)
        sent = self._activities(mission["id"], "sent")
        self.assertEqual(len(sent), 1)
        approved = self._activities(mission["id"], "approved")
        self.assertEqual(len(approved), 1)
        log = self.conn.execute(
            "SELECT * FROM mail_log WHERE status = 'sent'").fetchall()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["recipient"], "recruiter@example.com")

    @mock.patch.dict(os.environ, {"ECLIPSE_MAIL_TRANSPORT": "mock"})
    def test_mail_waits_without_standing_approval(self):
        mission = self._mail_mission(auto_approve=False)
        runner.tick_once(self.conn, call_fn=_fake_plan)
        self.assertEqual(len(self._activities(mission["id"], "sent")), 0)
        needed = self._activities(mission["id"], "approval_needed")
        self.assertEqual(len(needed), 1)
        tasks = self._tasks(mission["id"])
        drafts = draft_mod.list_drafts(self.conn, tasks[0]["id"])
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["status"], "draft")

    @mock.patch.dict(os.environ, {"ECLIPSE_MAIL_TRANSPORT": "mock"})
    def test_mail_send_cap_refuses(self):
        mission = self._mail_mission(max_sends_per_day=0)
        runner.tick_once(self.conn, call_fn=_fake_plan)
        self.assertEqual(len(self._activities(mission["id"], "sent")), 0)
        refused = self._activities(mission["id"], "refused")
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["detail"]["attempted"], "send")


class TestPriceWatchFlow(RunnerTestCase):
    def test_watch_created_then_target_hit_completes_one_shot(self):
        mission = self._mission(
            "price_watch",
            config={"url": "https://example.com/product/1",
                    "selector": ".price", "target_price": 5000,
                    "one_shot": True},
        )
        runner.tick_once(self.conn, call_fn=_fake_plan)
        fresh = models.get_mission(self.conn, mission["id"])
        watch_id = fresh["config"]["watch_id"]
        self.assertIsNotNone(watch_id)
        watch_mod.check_watch(self.conn, watch_id, True, "₹6,999")
        fired = watch_mod.check_watch(self.conn, watch_id, True, "₹4,999")
        self.assertTrue(fired["fired"])
        self._make_due(mission["id"])
        runner.tick_once(self.conn, call_fn=_fake_plan)
        done = models.get_mission(self.conn, mission["id"])
        self.assertEqual(done["status"], "done")
        # The observation is recorded as a price_observed activity.
        observed = [a for a in self._activities(mission["id"], "price_observed")
                    if a["detail"].get("observed_price") is not None]
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["detail"]["observed_price"], 4999.0)

    def test_price_above_target_keeps_mission_active(self):
        mission = self._mission(
            "price_watch",
            config={"url": "https://example.com/product/1",
                    "selector": ".price", "target_price": 5000,
                    "one_shot": True},
        )
        runner.tick_once(self.conn, call_fn=_fake_plan)
        fresh = models.get_mission(self.conn, mission["id"])
        watch_mod.check_watch(
            self.conn, fresh["config"]["watch_id"], True, "₹6,999")
        watch_mod.check_watch(
            self.conn, fresh["config"]["watch_id"], True, "₹6,499")
        self._make_due(mission["id"])
        runner.tick_once(self.conn, call_fn=_fake_plan)
        still = models.get_mission(self.conn, mission["id"])
        self.assertEqual(still["status"], "active")
        observed = [a for a in self._activities(mission["id"], "price_observed")
                    if a["detail"].get("observed_price") is not None]
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["detail"]["observed_price"], 6499.0)


class TestFactWiring(RunnerTestCase):
    def test_runner_calls_the_fact_helper(self):
        """The call site exists: a stubbed helper is invoked once
        per run (real W5 modules may or may not be present)."""
        mission = self._mission(config={
            "sources": ["pasted"], "roles": [ROLE_FIT]})
        calls = []

        def _stub(conn, mission_dict, candidates, actions):
            calls.append((mission_dict["id"], len(candidates)))
            return None

        with mock.patch.object(runner, "_record_run_facts", _stub):
            runner.tick_once(self.conn, call_fn=_fake_plan)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], mission["id"])
        self.assertEqual(calls[0][1], 1)

    def test_real_fact_helper_never_fails_the_run(self):
        mission = self._mission(config={
            "sources": ["pasted"], "roles": [ROLE_FIT]})
        runner.tick_once(self.conn, call_fn=_fake_plan)
        run = self._runs(mission["id"])[0]
        self.assertEqual(run["status"], "waiting_for_extension")
        # While W5 is absent the helper returns a note; once W5
        # lands it returns None (facts stored). Either way the
        # run succeeds.
        note = runner._record_run_facts(self.conn, mission, [], [])
        self.assertTrue(note is None or "fact store" in note)

    def test_failed_run_still_stores_discovered_facts(self):
        """Live-verified bug (integration): a run whose planner
        raises failed before the fact call site, so the role it
        had already discovered left no fact. The failure handler
        now records the run's facts anyway."""
        try:
            from server.memory import keyword  # noqa: F401
        except ImportError:
            self.skipTest("W5 fact-store modules not present yet")
        mission = self._mission(config={
            "sources": ["pasted"], "roles": [ROLE_FIT]})
        runner.tick_once(self.conn, call_fn=_raising_plan)
        run = self._runs(mission["id"])[0]
        self.assertEqual(run["status"], "failed")
        rows = self.conn.execute(
            "SELECT topic FROM facts WHERE scope = ?",
            (mission["id"],),
        ).fetchall()
        topics = [r["topic"] for r in rows]
        self.assertIn("role_seen", topics)

    def test_facts_stored_live_when_w5_present(self):
        """With W5's modules in the tree, a run stores scoped,
        redacted facts — one per candidate plus one per action.
        Skipped (deferred note instead) when W5 is absent."""
        try:
            from server.memory import budgets, compaction, keyword
        except ImportError:
            self.skipTest("W5 fact-store modules not present yet")
        mission = self._mission(config={
            "sources": ["pasted"], "roles": [ROLE_FIT]})
        runner.tick_once(self.conn, call_fn=_fake_plan)
        rows = self.conn.execute(
            "SELECT text, source, topic, scope FROM facts"
            " WHERE scope = ?",
            (mission["id"],),
        ).fetchall()
        self.assertGreaterEqual(len(rows), 1)
        seen = [dict(r) for r in rows]
        self.assertTrue(any(r["topic"] == "role_seen" for r in seen))
        for fact in seen:
            self.assertEqual(fact["source"], "mission")
            self.assertEqual(fact["scope"], mission["id"])
            # No raw PII in fact text.
            self.assertNotIn("@", fact["text"])


class TestBriefingTrigger(RunnerTestCase):
    def test_briefing_built_once_per_ist_day(self):
        # 2026-11-23 10:00 IST = 04:30 UTC — after the 09:00 rule.
        fixed = int(datetime(2026, 11, 23, 4, 30,
                             tzinfo=timezone.utc).timestamp())
        with mock.patch.object(runner, "_now", return_value=fixed):
            built = runner.tick_once(self.conn, call_fn=_fake_plan)
            self.assertTrue(built["briefing_built"])
            built_again = runner.tick_once(self.conn, call_fn=_fake_plan)
            self.assertFalse(built_again["briefing_built"])
        rows = self.conn.execute("SELECT * FROM briefings").fetchall()
        self.assertEqual(len(rows), 1)

    def test_no_briefing_before_nine(self):
        # 2026-11-23 08:00 IST = 02:30 UTC.
        fixed = int(datetime(2026, 11, 23, 2, 30,
                             tzinfo=timezone.utc).timestamp())
        with mock.patch.object(runner, "_now", return_value=fixed):
            built = runner.tick_once(self.conn, call_fn=_fake_plan)
            self.assertFalse(built["briefing_built"])
        rows = self.conn.execute("SELECT * FROM briefings").fetchall()
        self.assertEqual(len(rows), 0)


class TestStaleExecuting(RunnerTestCase):
    def test_stale_executing_step_reset_to_pending(self):
        task_id = loop.create_task(self.conn, "Apply to SDE Intern", "")
        loop.plan_task(self.conn, task_id, call_fn=_fake_plan)
        stale = int(time.time()) - 7200
        self.conn.execute(
            "UPDATE steps SET status = 'executing', started_at = ?"
            " WHERE task_id = ? AND n = 1",
            (stale, task_id),
        )
        self.conn.commit()
        runner.tick_once(self.conn, call_fn=_fake_plan)
        rows = self.conn.execute(
            "SELECT * FROM steps WHERE task_id = ? AND n = 1",
            (task_id,),
        ).fetchone()
        self.assertEqual(rows["status"], "pending")


# ----------------------------------------------------------------
# Resume storage + extraction (server/missions/resume.py)
# ----------------------------------------------------------------

def _minimal_pdf(text: str) -> bytes:
    """A tiny one-page PDF with one FlateDecode content stream."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    stream = zlib.compress(content)
    parts = []
    parts.append(b"%PDF-1.4\n")
    offsets = {}

    def _obj(num: int, body: bytes) -> bytes:
        offsets[num] = sum(len(p) for p in parts)
        return b"%d 0 obj\n" % num + body + b"\nendobj\n"

    parts.append(_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    parts.append(_obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"))
    parts.append(_obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox"
                     b" [0 0 612 792] /Contents 4 0 R"
                     b" /Resources << /Font << /F1 5 0 R >> >> >>"))
    parts.append(_obj(4, b"<< /Length %d /Filter /FlateDecode >>\n"
                     b"stream\n" % len(stream) + stream + b"\nendstream"))
    parts.append(_obj(5, b"<< /Type /Font /Subtype /Type1"
                     b" /BaseFont /Helvetica >>"))
    xref_pos = sum(len(p) for p in parts)
    body = b"".join(parts)
    body += b"xref\n0 6\n0000000000 65535 f \n"
    for num in range(1, 6):
        body += b"%010d 00000 n \n" % offsets[num]
    body += (b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
             + str(xref_pos).encode() + b"\n%%EOF\n")
    return body


class TestResumeStorage(RunnerTestCase):
    def test_store_plain_text_resume(self):
        meta = resume_mod.store_resume(
            self.conn, "Prateek_Resume.txt",
            b"Prateek\nSoftware Engineer Intern\nPython, FastAPI\n")
        self.assertEqual(meta["extraction_status"], "ok")
        self.assertIn("Software Engineer Intern", meta["extracted_text"])
        self.assertTrue(Path(meta["stored_path"]).exists())
        self.assertEqual(resume_mod.get_resume_meta(self.conn), meta)

    def test_store_pdf_extracts_text(self):
        meta = resume_mod.store_resume(
            self.conn, "resume.pdf", _minimal_pdf("Prateek, SDE Intern"))
        self.assertEqual(meta["extraction_status"], "ok")
        self.assertIn("Prateek, SDE Intern", meta["extracted_text"])

    def test_unsupported_format_is_stored_not_faked(self):
        meta = resume_mod.store_resume(
            self.conn, "resume.docx", b"PK\x03\x04fake docx bytes")
        self.assertEqual(meta["extraction_status"], "unsupported")
        self.assertIsNone(meta["extracted_text"])
        self.assertTrue(Path(meta["stored_path"]).exists())

    def test_pdf_without_text_is_limited(self):
        meta = resume_mod.store_resume(
            self.conn, "blank.pdf", _minimal_pdf(""))
        self.assertEqual(meta["extraction_status"], "limited")
        self.assertIsNone(meta["extracted_text"])

    def test_store_pasted_text(self):
        meta = resume_mod.store_resume_text(
            self.conn, "Prateek\nSDE Intern\n")
        self.assertEqual(meta["extraction_status"], "ok")
        self.assertIn("SDE Intern", meta["extracted_text"])
        self.assertEqual(meta["filename"], "resume.txt")

    def test_filename_sanitised_no_traversal(self):
        meta = resume_mod.store_resume(
            self.conn, "../../etc/passwd.txt", b"hello\n")
        stored = Path(meta["stored_path"])
        self.assertTrue(stored.is_relative_to(resume_mod.resumes_dir()))
        self.assertTrue(stored.name.startswith("passwd"))
        self.assertEqual(meta["filename"], "passwd.txt")

    def test_oversized_resume_rejected(self):
        with self.assertRaises(ValueError):
            resume_mod.store_resume(
                self.conn, "big.txt", b"x" * (resume_mod.MAX_RESUME_BYTES + 1))

    def test_second_upload_replaces_the_first_file(self):
        first = resume_mod.store_resume(self.conn, "a.txt", b"first\n")
        resume_mod.store_resume(self.conn, "b.txt", b"second\n")
        self.assertFalse(Path(first["stored_path"]).exists())
        meta = resume_mod.get_resume_meta(self.conn)
        self.assertEqual(meta["filename"], "b.txt")

    def test_stored_file_is_owner_only(self):
        meta = resume_mod.store_resume(self.conn, "a.txt", b"secret\n")
        mode = Path(meta["stored_path"]).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_resume_file_path_none_before_upload(self):
        self.assertIsNone(resume_mod.resume_file_path(None))
        self.assertIsNone(resume_mod.get_resume_meta(self.conn))


# ----------------------------------------------------------------
# Runner API routes (server/api/missions_runner_api.py)
# Direct function calls: the venv has no httpx/TestClient.
# ----------------------------------------------------------------

class RunnerApiTestCase(RunnerTestCase):
    def _request(self, body: bytes, content_type: str) -> Request:
        scope = {
            "type": "http", "method": "POST", "path": "/missions/resume",
            "headers": [(b"content-type", content_type.encode())],
            "query_string": b"", "server": ("testserver", 80),
            "scheme": "http", "client": ("testclient", 50000),
        }

        async def _receive():
            return {"type": "http.request", "body": body,
                    "more_body": False}

        return Request(scope, _receive)


class TestRunnerApiRoutes(RunnerApiTestCase):
    def test_router_registers_all_routes(self):
        app = FastAPI()
        app.include_router(runner_api.router)
        # FastAPI 0.165 mounts included routers lazily behind an
        # _IncludedRouter wrapper — collect paths from both.
        paths = set()
        for route in app.routes:
            if hasattr(route, "path"):
                paths.add(route.path)
            original = getattr(route, "original_router", None)
            if original is not None:
                paths |= {r.path for r in original.routes
                          if hasattr(r, "path")}
        for expected in (
            "/missions/intake", "/missions/due", "/missions/check-in",
            "/missions/{mission_id}/run-now", "/missions/resume",
            "/missions/resume-text",
        ):
            self.assertIn(expected, paths)

    def test_due_lists_jobs_and_updates_heartbeat(self):
        mission = self._mission(config={
            "sources": ["pasted"], "roles": [ROLE_FIT]})
        runner.tick_once(self.conn, call_fn=_fake_plan)
        out = runner_api.due_missions(_token=TOKEN)
        self.assertEqual(len(out["missions"]), 1)
        job = out["missions"][0]
        self.assertEqual(job["mission_id"], mission["id"])
        self.assertEqual(job["job"]["kind"], "execute_step")
        seen = self.conn.execute(
            "SELECT value FROM memory WHERE key = ?",
            (runner.LAST_EXTENSION_SEEN_KEY,)).fetchone()
        self.assertIsNotNone(seen)
        self.assertAlmostEqual(int(seen["value"]), int(time.time()),
                               delta=5)

    def test_check_in_updates_heartbeat(self):
        out = runner_api.check_in(_token=TOKEN)
        self.assertTrue(out["ok"])
        seen = self.conn.execute(
            "SELECT value FROM memory WHERE key = ?",
            (runner.LAST_EXTENSION_SEEN_KEY,)).fetchone()
        self.assertIsNotNone(seen)

    def test_run_now_sets_next_run_now(self):
        mission = self._mission(config={"sources": ["pasted"], "roles": []})
        self._make_future(mission["id"])
        out = runner_api.run_now(mission["id"], _token=TOKEN)
        self.assertEqual(out["id"], mission["id"])
        fresh = models.get_mission(self.conn, mission["id"])
        self.assertAlmostEqual(fresh["next_run_at"], int(time.time()),
                               delta=5)

    def test_run_now_rejects_draft_and_unknown(self):
        draft = models.create_mission(self.conn, "job_hunt", "Draft", {}, {})
        with self.assertRaises(Exception) as ctx:
            runner_api.run_now(draft["id"], _token=TOKEN)
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(Exception) as ctx:
            runner_api.run_now("mission_missing", _token=TOKEN)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_intake_route_wires_parse_intake(self):
        with mock.patch.object(
            runner_api_mod.intake_mod, "parse_intake",
            return_value={"draft": {"type": "job_hunt"}, "source": "rules",
                          "confidence": "low", "missing_fields": [],
                          "warnings": []},
        ) as parse:
            body = runner_api.IntakeIn(message="watch for SDE roles")
            out = runner_api.post_intake(body, _token=TOKEN)
        # PR #7: the route passes the upload context through
        # (empty here — no files attached to this message).
        parse.assert_called_once_with(
            "watch for SDE roles", upload_ids=[], conn=mock.ANY)
        self.assertEqual(out["source"], "rules")

    def test_get_resume_404_before_upload(self):
        with self.assertRaises(Exception) as ctx:
            runner_api.get_resume(_token=TOKEN)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_resume_text_route_round_trip(self):
        body = runner_api.ResumeTextIn(text="Prateek\nSDE Intern\n")
        out = runner_api.upload_resume_text(body, _token=TOKEN)
        self.assertEqual(out["extraction_status"], "ok")
        self.assertIn("SDE Intern", out["extracted_text"])
        got = runner_api.get_resume(_token=TOKEN)
        self.assertEqual(got["filename"], "resume.txt")

    def test_parse_multipart_file_unit(self):
        boundary = "----testboundary"
        payload = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file";'
            ' filename="resume.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "Prateek\nSDE Intern\n"
            f"\r\n--{boundary}--\r\n"
        ).encode()
        filename, data = runner_api._parse_multipart_file(
            payload, f"multipart/form-data; boundary={boundary}")
        self.assertEqual(filename, "resume.txt")
        self.assertEqual(data, b"Prateek\nSDE Intern\n")

    def test_parse_multipart_file_returns_none_for_bad_body(self):
        # No file part → (None, None); the route turns that
        # into a 422.
        self.assertEqual(
            runner_api._parse_multipart_file(b"not multipart", "text/plain"),
            (None, None))
        self.assertEqual(
            runner_api._parse_multipart_file(
                b"--b\r\n\r\nx\r\n--b--\r\n",
                "multipart/form-data; boundary=b"),
            (None, None))

    def test_upload_resume_route_via_request(self):
        boundary = "----testboundary"
        payload = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file";'
            ' filename="resume.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "Prateek\nSDE Intern\n"
            f"\r\n--{boundary}--\r\n"
        ).encode()
        request = self._request(
            payload, f"multipart/form-data; boundary={boundary}")
        out = asyncio.run(runner_api.upload_resume(request, _token=TOKEN))
        self.assertEqual(out["extraction_status"], "ok")
        self.assertIn("SDE Intern", out["extracted_text"])
        self.assertIn("note", out)
        got = runner_api.get_resume(_token=TOKEN)
        self.assertEqual(got["filename"], "resume.txt")

    def test_upload_resume_route_rejects_unsupported_note(self):
        boundary = "----testboundary"
        payload = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file";'
            ' filename="resume.docx"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            "PK fake docx"
            f"\r\n--{boundary}--\r\n"
        ).encode()
        request = self._request(
            payload, f"multipart/form-data; boundary={boundary}")
        out = asyncio.run(runner_api.upload_resume(request, _token=TOKEN))
        self.assertEqual(out["extraction_status"], "unsupported")
        self.assertIsNone(out["extracted_text"])
        self.assertIn("cannot be text-extracted", out["note"])


if __name__ == "__main__":
    unittest.main()
