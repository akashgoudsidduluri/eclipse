"""W1 core tests — missions CRUD, lifecycle, kill switch, approvals,
migration (PR #5, §5).

Mock planners only — no API key, no live model, no network. Mail
sends use ECLIPSE_MAIL_TRANSPORT=mock. All test data is fake
(Test User, example.com, localhost fixtures).

NOTE on types.py: server/missions/types.py is W2's file and may
not exist yet while the workers build in parallel. These tests use
the REAL registry when it is importable; otherwise they install
the stand-in below, which implements exactly the §3.5/§3.7
contract (three types, unknown-key rejection, type defaults). If
a run used the stand-in (USING_STANDIN_TYPES is True), re-run
against W2's real types.py at integration.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import types as _stdlib_types
import unittest
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# --- types.py stand-in (W2's file; see the module docstring) ---
try:
    from server.missions import types as _real_types  # noqa: F401

    USING_STANDIN_TYPES = False
except ImportError:
    USING_STANDIN_TYPES = True

    _HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

    @dataclass
    class MissionTypeSpec:
        type: str
        label: str
        config_schema: dict
        default_bounds: dict
        default_interval_min: int = 60
        discover: object = None
        act: object = None

    def _is_int(v):
        return type(v) is int

    def _is_bool(v):
        return type(v) is bool

    def _validate_config_job_hunt(config):
        allowed = {"sources", "min_score", "roles", "watch_new_only"}
        unknown = set(config) - allowed
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        out = {"sources": ["gmail"], "min_score": 60, "roles": [],
               "watch_new_only": True}
        if "sources" in config:
            if not isinstance(config["sources"], list):
                raise ValueError("sources must be a list")
            out["sources"] = list(config["sources"])
        if "min_score" in config:
            if not _is_int(config["min_score"]) or not 0 <= config["min_score"] <= 100:
                raise ValueError("min_score must be an int 0-100")
            out["min_score"] = config["min_score"]
        if "roles" in config:
            if not isinstance(config["roles"], list):
                raise ValueError("roles must be a list")
            out["roles"] = list(config["roles"])
        if "watch_new_only" in config:
            if not _is_bool(config["watch_new_only"]):
                raise ValueError("watch_new_only must be a bool")
            out["watch_new_only"] = config["watch_new_only"]
        return out

    def _validate_config_cold_mail(config):
        allowed = {"role_ids", "recipients", "attach_resume",
                   "max_drafts_per_run"}
        unknown = set(config) - allowed
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        out = {"role_ids": [], "recipients": [], "attach_resume": False,
               "max_drafts_per_run": 5}
        for key in ("role_ids", "recipients"):
            if key in config:
                if not isinstance(config[key], list):
                    raise ValueError(f"{key} must be a list")
                out[key] = list(config[key])
        if "attach_resume" in config:
            if not _is_bool(config["attach_resume"]):
                raise ValueError("attach_resume must be a bool")
            out["attach_resume"] = config["attach_resume"]
        if "max_drafts_per_run" in config:
            if not _is_int(config["max_drafts_per_run"]) or config["max_drafts_per_run"] < 1:
                raise ValueError("max_drafts_per_run must be an int >= 1")
            out["max_drafts_per_run"] = config["max_drafts_per_run"]
        return out

    def _validate_config_price_watch(config):
        allowed = {"url", "selector", "condition", "target_price",
                   "currency", "one_shot"}
        unknown = set(config) - allowed
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        for key in ("url", "selector"):
            if not config.get(key) or not isinstance(config[key], str):
                raise ValueError(f"{key} is required for price_watch")
        out = {"url": config["url"], "selector": config["selector"],
               "condition": "changed", "target_price": None,
               "currency": "INR", "one_shot": True}
        if "condition" in config:
            cond = config["condition"]
            if cond not in ("changed", "appeared", "disappeared") and not (
                    isinstance(cond, str) and cond.startswith("contains:")):
                raise ValueError(f"unknown condition {cond!r}")
            out["condition"] = cond
        if "target_price" in config:
            if config["target_price"] is not None and (
                    not _is_int(config["target_price"])
                    or config["target_price"] < 0):
                raise ValueError("target_price must be an int >= 0 or null")
            out["target_price"] = config["target_price"]
        if "currency" in config:
            if not isinstance(config["currency"], str):
                raise ValueError("currency must be a string")
            out["currency"] = config["currency"]
        if "one_shot" in config:
            if not _is_bool(config["one_shot"]):
                raise ValueError("one_shot must be a bool")
            out["one_shot"] = config["one_shot"]
        return out

    _CONFIG_VALIDATORS = {
        "job_hunt": _validate_config_job_hunt,
        "cold_mail": _validate_config_cold_mail,
        "price_watch": _validate_config_price_watch,
    }

    _BOUNDS_KEYS = {"max_actions_per_day", "max_applies_per_day",
                    "max_sends_per_day", "max_spend_inr", "allowed_sites",
                    "require_approval_for", "auto_approve",
                    "quiet_hours_ist", "min_score"}

    def _default_bounds_for(mission_type):
        return {
            "max_actions_per_day": 5,
            "max_applies_per_day": 3 if mission_type == "job_hunt" else 0,
            "max_sends_per_day": 5 if mission_type == "cold_mail" else 0,
            "max_spend_inr": 0,
            "allowed_sites": [],
            "require_approval_for": ["apply", "send"],
            "auto_approve": False,
            "quiet_hours_ist": None,
            "min_score": 60,
        }

    def validate_config(mission_type, config):
        if mission_type not in _CONFIG_VALIDATORS:
            raise ValueError(f"unknown mission type {mission_type!r}")
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        return _CONFIG_VALIDATORS[mission_type](config)

    def validate_bounds(mission_type, bounds):
        if mission_type not in _CONFIG_VALIDATORS:
            raise ValueError(f"unknown mission type {mission_type!r}")
        if not isinstance(bounds, dict):
            raise ValueError("bounds must be an object")
        unknown = set(bounds) - _BOUNDS_KEYS
        if unknown:
            raise ValueError(f"unknown bounds keys: {sorted(unknown)}")
        out = _default_bounds_for(mission_type)
        for key in ("max_actions_per_day", "max_applies_per_day",
                    "max_sends_per_day", "max_spend_inr"):
            if key in bounds:
                if not _is_int(bounds[key]) or bounds[key] < 0:
                    raise ValueError(f"{key} must be an int >= 0")
                out[key] = bounds[key]
        if "allowed_sites" in bounds:
            if not isinstance(bounds["allowed_sites"], list):
                raise ValueError("allowed_sites must be a list")
            out["allowed_sites"] = list(bounds["allowed_sites"])
        if "require_approval_for" in bounds:
            req = bounds["require_approval_for"]
            if not isinstance(req, list) or not set(req) <= {"apply", "send"}:
                raise ValueError(
                    "require_approval_for must be a subset of"
                    " ['apply', 'send']")
            out["require_approval_for"] = list(req)
        if "auto_approve" in bounds:
            if not _is_bool(bounds["auto_approve"]):
                raise ValueError("auto_approve must be a bool")
            out["auto_approve"] = bounds["auto_approve"]
        if "quiet_hours_ist" in bounds:
            qh = bounds["quiet_hours_ist"]
            if qh is not None:
                if (not isinstance(qh, dict) or set(qh) != {"start", "end"}
                        or not _HHMM.match(qh["start"])
                        or not _HHMM.match(qh["end"])):
                    raise ValueError(
                        "quiet_hours_ist must be null or"
                        " {start: 'HH:MM', end: 'HH:MM'}")
                out["quiet_hours_ist"] = dict(qh)
            else:
                out["quiet_hours_ist"] = None
        if "min_score" in bounds:
            if not _is_int(bounds["min_score"]) or not 0 <= bounds["min_score"] <= 100:
                raise ValueError("min_score must be an int 0-100")
            out["min_score"] = bounds["min_score"]
        return out

    _standin = _stdlib_types.ModuleType("server.missions.types")
    _standin.MissionTypeSpec = MissionTypeSpec
    _standin.MISSION_TYPES = {
        "job_hunt": MissionTypeSpec(
            type="job_hunt", label="Job hunt",
            config_schema={
                "sources": (list, False, ["gmail"], None),
                "min_score": (int, False, 60, None),
                "roles": (list, False, [], None),
                "watch_new_only": (bool, False, True, None),
            },
            default_bounds=_default_bounds_for("job_hunt")),
        "cold_mail": MissionTypeSpec(
            type="cold_mail", label="Cold mail",
            config_schema={
                "role_ids": (list, False, [], None),
                "recipients": (list, False, [], None),
                "attach_resume": (bool, False, False, None),
                "max_drafts_per_run": (int, False, 5, None),
            },
            default_bounds=_default_bounds_for("cold_mail")),
        "price_watch": MissionTypeSpec(
            type="price_watch", label="Price watch",
            config_schema={
                "url": (str, True, None, None),
                "selector": (str, True, None, None),
                "condition": (str, False, "changed", None),
                "target_price": (int, False, None, None),
                "currency": (str, False, "INR", None),
                "one_shot": (bool, False, True, None),
            },
            default_bounds=_default_bounds_for("price_watch")),
    }
    _standin.validate_config = validate_config
    _standin.validate_bounds = validate_bounds
    sys.modules["server.missions.types"] = _standin
    import server.missions as _missions_pkg

    _missions_pkg.types = _standin
# --- end of types.py stand-in ---

from fastapi import HTTPException  # noqa: E402
from server.api import mail_api, missions_api, tasks_api  # noqa: E402
from server.core import config, db  # noqa: E402
from server.mail import drafts as draft_mod  # noqa: E402
from server.missions import bounds, links, models  # noqa: E402
from server.tasks import loop  # noqa: E402


class MissionTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_data_dir = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()
        # Mail sends in these tests use the mock transport — never
        # real Gmail (server/mail/send.py:52-66).
        self._old_transport = os.environ.get("ECLIPSE_MAIL_TRANSPORT")
        os.environ["ECLIPSE_MAIL_TRANSPORT"] = "mock"

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._orig_data_dir
        if self._old_transport is None:
            os.environ.pop("ECLIPSE_MAIL_TRANSPORT", None)
        else:
            os.environ["ECLIPSE_MAIL_TRANSPORT"] = self._old_transport
        self.tmp.cleanup()

    def _create(self, type="job_hunt", name="Test mission",
                config=None, bounds=None):
        return models.create_mission(
            self.conn, type, name, config or {}, bounds or {})

    def _price_config(self):
        return {"url": "https://example.com/product/1",
                "selector": ".price"}

    def _task_for(self, mission_id, goal="Do a thing", status=None):
        task_id = loop.create_task(self.conn, goal)
        links.set_task_mission(self.conn, task_id, mission_id)
        if status is not None:
            self.conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (status, task_id))
            self.conn.commit()
        return task_id

    def _grant(self, task_id, detail=None):
        now = int(time.time())
        grant_id = uuid.uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO grants (id, origin, scope, expires_at,"
            " created_at, detail) VALUES (?, ?, 'approve', ?, ?, ?)",
            (grant_id, task_id, now + 600, now,
             json.dumps(detail or {})),
        )
        self.conn.commit()
        return grant_id

    def _pending(self, task_id, action="submit", step=1, detail=None,
                 ttl=600):
        # Direct insert: record_pending_approval cannot carry a
        # draft_id in the detail — this is the row the runner
        # records for a mail send (action "mail_send",
        # detail {"draft_id": ...}).
        now = int(time.time())
        approval_id = f"appr-{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO pending_approvals (id, task_id, step, action,"
            " detail, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (approval_id, task_id, step, action,
             json.dumps(detail or {}), now, now + ttl),
        )
        self.conn.commit()
        return approval_id

    def _claim(self, task_id, role_id="role_1"):
        self.conn.execute(
            "INSERT INTO receipts (id, task_id, role_id, title, company,"
            " url, status, confirmation, created_at)"
            " VALUES (?, ?, ?, '', '', '', 'claimed', '', ?)",
            (uuid.uuid4().hex[:12], task_id, role_id, int(time.time())),
        )
        self.conn.commit()

    def _draft(self, task_id, recipient="recruiter@example.com",
               role_id="role_1", status="draft"):
        draft_id = uuid.uuid4().hex[:12]
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO mail_drafts (id, task_id, recipient, role_id,"
            " subject, body, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (draft_id, task_id, recipient, role_id,
             "Test subject", "Test body", status, now, now),
        )
        self.conn.commit()
        return draft_id

    def _grants(self, task_id):
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM grants WHERE origin = ?"
            " AND scope = 'approve'",
            (task_id,),
        ).fetchone()["c"]

    def _pending_count(self, task_id):
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM pending_approvals"
            " WHERE task_id = ?",
            (task_id,),
        ).fetchone()["c"]

    def _claims(self, task_id):
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM receipts WHERE task_id = ?"
            " AND status = 'claimed'",
            (task_id,),
        ).fetchone()["c"]

    def _assert_http(self, status_code, fn, *args, **kwargs):
        with self.assertRaises(HTTPException) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.status_code, status_code)
        return ctx.exception


class TestMigration(MissionTestBase):
    def test_migrate_creates_mission_tables(self):
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn("missions", tables)
        self.assertIn("mission_runs", tables)
        self.assertIn("mission_activity", tables)
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(tasks)")}
        self.assertIn("mission_id", cols)
        indexes = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn("idx_tasks_mission", indexes)
        self.assertIn("idx_missions_status_next", indexes)
        self.assertIn("idx_mission_runs_mission", indexes)
        self.assertIn("idx_mission_activity_mission", indexes)
        self.assertIn("idx_mission_activity_kind_day", indexes)

    def test_migrate_on_pre_pr5_db(self):
        # A database from before PR #5: the old tasks table (no
        # mission_id) and no mission tables at all.
        old_dir = tempfile.TemporaryDirectory()
        try:
            path = Path(old_dir.name) / "eclipse.db"
            raw = sqlite3.connect(path)
            raw.execute(
                "CREATE TABLE tasks(id TEXT PRIMARY KEY, goal TEXT,"
                " constraints TEXT, status TEXT, created_at INTEGER,"
                " updated_at INTEGER, receipt TEXT)")
            raw.execute(
                "CREATE TABLE facts(id TEXT PRIMARY KEY, text TEXT,"
                " source TEXT, created_at INTEGER)")
            raw.commit()
            raw.close()
            config.DATA_DIR = Path(old_dir.name)
            db.migrate()
            conn2 = db.connect()
            try:
                tables = {r[0] for r in conn2.execute(
                    "SELECT name FROM sqlite_master"
                    " WHERE type = 'table'")}
                self.assertIn("missions", tables)
                self.assertIn("mission_runs", tables)
                self.assertIn("mission_activity", tables)
                cols = {r[1] for r in conn2.execute(
                    "PRAGMA table_info(tasks)")}
                self.assertIn("mission_id", cols)
            finally:
                conn2.close()
        finally:
            config.DATA_DIR = Path(self.tmp.name)
            old_dir.cleanup()

    def test_migrate_twice_is_a_noop(self):
        mission = self._create()
        db.migrate()
        db.migrate()
        self.assertIsNotNone(
            models.get_mission(self.conn, mission["id"]))


class TestMissionCRUD(MissionTestBase):
    def test_create_get_list(self):
        first = self._create(name="First")
        second = self._create(name="Second", type="cold_mail")
        self.assertTrue(first["id"].startswith("m_"))
        self.assertEqual(first["status"], "draft")
        self.assertEqual(first["type"], "job_hunt")
        self.assertEqual(first["config"]["min_score"], 60)
        self.assertEqual(first["bounds"]["max_actions_per_day"], 5)
        self.assertIsNone(first["next_run_at"])
        self.assertIsNone(first["last_run_at"])
        got = models.get_mission(self.conn, first["id"])
        self.assertEqual(got["name"], "First")
        self.assertIsInstance(got["config"], dict)
        self.assertIsInstance(got["bounds"], dict)
        # Newest first.
        listed = models.list_missions(self.conn)
        self.assertEqual([m["id"] for m in listed],
                         [second["id"], first["id"]])
        self.assertEqual(
            [m["id"] for m in models.list_missions(self.conn, "draft")],
            [second["id"], first["id"]])
        self.assertEqual(models.list_missions(self.conn, "active"), [])

    def test_create_via_api(self):
        result = missions_api.create_mission(missions_api.MissionIn(
            type="price_watch", name="Watch the widget",
            config=self._price_config()))
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["type"], "price_watch")
        self.assertEqual(result["config"]["url"],
                         "https://example.com/product/1")
        # Bounds defaulted per type: a price watch applies to
        # nothing and sends nothing.
        self.assertEqual(result["bounds"]["max_applies_per_day"], 0)
        self.assertEqual(result["bounds"]["max_sends_per_day"], 0)

    def test_create_unknown_type_is_422(self):
        self._assert_http(
            422, missions_api.create_mission,
            missions_api.MissionIn(type="time_travel", name="Nope"))

    def test_create_unknown_config_key_is_422(self):
        self._assert_http(
            422, missions_api.create_mission,
            missions_api.MissionIn(
                type="job_hunt", name="Nope",
                config={"teleport": True}))

    def test_create_bad_config_value_is_422(self):
        self._assert_http(
            422, missions_api.create_mission,
            missions_api.MissionIn(
                type="job_hunt", name="Nope",
                config={"min_score": 101}))

    def test_create_price_watch_without_url_is_422(self):
        self._assert_http(
            422, missions_api.create_mission,
            missions_api.MissionIn(
                type="price_watch", name="Nope",
                config={"selector": ".price"}))

    def test_create_bad_bounds_is_422(self):
        for bad in ({"max_applies_per_day": -1},
                    {"max_actions_per_day": "many"},
                    {"unknown_bound": 1},
                    {"require_approval_for": ["delete"]},
                    {"auto_approve": "yes"},
                    {"quiet_hours_ist": {"start": "25:00", "end": "07:00"}}):
            with self.subTest(bad=bad):
                self._assert_http(
                    422, missions_api.create_mission,
                    missions_api.MissionIn(
                        type="job_hunt", name="Nope", bounds=bad))

    def test_create_empty_name_is_422(self):
        with self.assertRaises(ValueError):
            models.create_mission(self.conn, "job_hunt", "   ", {}, {})

    def test_get_unknown_is_404(self):
        self._assert_http(404, missions_api.get_mission, "m_missing")

    def test_list_invalid_status_is_422(self):
        self._assert_http(422, missions_api.list_missions,
                          status="flying")

    def test_patch_name_config_bounds(self):
        mission = self._create()
        patched = missions_api.update_mission(
            mission["id"],
            missions_api.MissionPatch(
                name="Renamed",
                config={"min_score": 80},
                bounds={"max_applies_per_day": 1}))
        self.assertEqual(patched["name"], "Renamed")
        self.assertEqual(patched["config"]["min_score"], 80)
        self.assertEqual(patched["bounds"]["max_applies_per_day"], 1)
        # Unchanged bounds keep their values.
        self.assertEqual(patched["bounds"]["max_actions_per_day"], 5)

    def test_patch_bounds_while_paused_is_allowed(self):
        mission = self._create()
        models.transition(self.conn, mission["id"], "active")
        models.transition(self.conn, mission["id"], "paused")
        patched = missions_api.update_mission(
            mission["id"],
            missions_api.MissionPatch(
                bounds={"max_applies_per_day": 1}))
        self.assertEqual(patched["bounds"]["max_applies_per_day"], 1)

    def test_patch_no_fields_is_422(self):
        mission = self._create()
        self._assert_http(
            422, missions_api.update_mission, mission["id"],
            missions_api.MissionPatch())

    def test_patch_unknown_is_404(self):
        self._assert_http(
            404, missions_api.update_mission, "m_missing",
            missions_api.MissionPatch(name="x"))

    def test_patch_bad_config_is_422(self):
        mission = self._create()
        self._assert_http(
            422, missions_api.update_mission, mission["id"],
            missions_api.MissionPatch(config={"min_score": -5}))

    def test_delete_draft_removes_runs_and_activity_but_keeps_tasks(self):
        mission = self._create()
        task_id = self._task_for(mission["id"])
        models.record_activity(self.conn, mission["id"], "discovered",
                               {"role_id": "role_1"})
        self.conn.execute(
            "INSERT INTO mission_runs (id, mission_id, task_id, status,"
            " started_at, ended_at, detail) VALUES (?, ?, ?, 'ok', ?, ?, '')",
            ("mr_aaaaaaaaaaaa", mission["id"], task_id,
             int(time.time()), int(time.time())))
        self.conn.commit()
        result = missions_api.delete_mission(mission["id"])
        self.assertEqual(result, {"deleted": mission["id"]})
        self.assertIsNone(models.get_mission(self.conn, mission["id"]))
        self.assertEqual(
            models.list_activity(self.conn, mission["id"]), [])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM mission_runs"
                " WHERE mission_id = ?",
                (mission["id"],)).fetchone()["c"], 0)
        # The task is the audit trail — it survives.
        self.assertIsNotNone(loop.get_task(self.conn, task_id))

    def test_delete_stopped_ok(self):
        mission = self._create()
        models.stop_mission(self.conn, mission["id"])
        self.assertEqual(
            missions_api.delete_mission(mission["id"]),
            {"deleted": mission["id"]})

    def test_delete_active_or_paused_is_409(self):
        mission = self._create()
        models.transition(self.conn, mission["id"], "active")
        self._assert_http(409, missions_api.delete_mission, mission["id"])
        models.transition(self.conn, mission["id"], "paused")
        self._assert_http(409, missions_api.delete_mission, mission["id"])

    def test_delete_unknown_is_404(self):
        self._assert_http(404, missions_api.delete_mission, "m_missing")


class TestStateMachine(MissionTestBase):
    def test_legal_transitions(self):
        mission = self._create()
        now = int(time.time())
        active = missions_api.activate_mission(mission["id"])
        self.assertEqual(active["status"], "active")
        self.assertIsNotNone(active["next_run_at"])
        self.assertGreaterEqual(active["next_run_at"], now)
        paused = missions_api.pause_mission(mission["id"])
        self.assertEqual(paused["status"], "paused")
        self.assertIsNone(paused["next_run_at"])
        resumed = missions_api.resume_mission(mission["id"])
        self.assertEqual(resumed["status"], "active")
        self.assertIsNotNone(resumed["next_run_at"])
        # Activate from paused is the same as resume.
        missions_api.pause_mission(mission["id"])
        self.assertEqual(
            missions_api.activate_mission(mission["id"])["status"],
            "active")
        stopped = missions_api.stop_mission(mission["id"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertIsNone(stopped["next_run_at"])

    def test_draft_to_stopped_and_draft_to_done_paths(self):
        mission = self._create()
        self.assertEqual(
            missions_api.stop_mission(mission["id"])["status"], "stopped")
        # Active -> done (one-shot completion, the runner's path).
        mission2 = self._create(name="Second")
        models.transition(self.conn, mission2["id"], "active")
        done = models.transition(self.conn, mission2["id"], "done")
        self.assertEqual(done["status"], "done")
        self.assertIsNone(done["next_run_at"])

    def test_illegal_transitions_are_409(self):
        mission = self._create()
        # pause / resume from draft.
        self._assert_http(409, missions_api.pause_mission, mission["id"])
        self._assert_http(409, missions_api.resume_mission, mission["id"])
        # done from paused is not a path.
        models.transition(self.conn, mission["id"], "active")
        models.transition(self.conn, mission["id"], "paused")
        with self.assertRaises(models.IllegalTransitionError):
            models.transition(self.conn, mission["id"], "done")
        # Everything out of stopped is illegal.
        missions_api.stop_mission(mission["id"])
        self._assert_http(409, missions_api.activate_mission, mission["id"])
        self._assert_http(409, missions_api.pause_mission, mission["id"])
        self._assert_http(409, missions_api.resume_mission, mission["id"])
        self._assert_http(409, missions_api.stop_mission, mission["id"])
        # Everything out of done is illegal.
        mission2 = self._create(name="Second")
        models.transition(self.conn, mission2["id"], "active")
        models.transition(self.conn, mission2["id"], "done")
        self._assert_http(409, missions_api.activate_mission,
                          mission2["id"])
        self._assert_http(409, missions_api.stop_mission, mission2["id"])

    def test_lifecycle_on_unknown_mission_is_404(self):
        self._assert_http(404, missions_api.activate_mission, "m_missing")
        self._assert_http(404, missions_api.pause_mission, "m_missing")
        self._assert_http(404, missions_api.resume_mission, "m_missing")
        self._assert_http(404, missions_api.stop_mission, "m_missing")

    def test_patch_while_active_or_terminal_is_409(self):
        mission = self._create()
        models.transition(self.conn, mission["id"], "active")
        self._assert_http(
            409, missions_api.update_mission, mission["id"],
            missions_api.MissionPatch(name="Nope"))
        missions_api.stop_mission(mission["id"])
        self._assert_http(
            409, missions_api.update_mission, mission["id"],
            missions_api.MissionPatch(name="Nope"))

    def test_transition_records_state_changed_activity(self):
        mission = self._create()
        models.transition(self.conn, mission["id"], "active")
        activity = models.list_activity(self.conn, mission["id"])
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["kind"], "state_changed")
        self.assertEqual(
            json.loads(activity[0]["detail"]),
            {"from": "draft", "to": "active"})


class TestStopKillSwitch(MissionTestBase):
    def test_stop_revokes_grants_consumes_approvals_releases_claims_cancels_tasks(self):
        mission = self._create()
        models.transition(self.conn, mission["id"], "active")
        task_live = self._task_for(mission["id"], "Apply to role_1")
        task_done = self._task_for(mission["id"], "Apply to role_2",
                                   status="done")
        other = self._create(name="Other mission")
        other_task = self._task_for(other["id"], "Other task")
        self._grant(task_live)
        self._grant(task_done)
        self._grant(other_task)
        self._pending(task_live)
        self._pending(other_task)
        self._claim(task_live)

        stopped = missions_api.stop_mission(mission["id"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertIsNone(stopped["next_run_at"])

        # Grants revoked for this mission's tasks only.
        self.assertEqual(self._grants(task_live), 0)
        self.assertEqual(self._grants(task_done), 0)
        self.assertEqual(self._grants(other_task), 1)
        # Pending approvals consumed for this mission only.
        self.assertEqual(self._pending_count(task_live), 0)
        self.assertEqual(self._pending_count(other_task), 1)
        # Role claims released.
        self.assertEqual(self._claims(task_live), 0)
        # Non-terminal tasks cancelled; terminal ones untouched;
        # other missions' tasks untouched.
        self.assertEqual(loop.get_task(self.conn, task_live)["status"],
                         "cancelled")
        self.assertEqual(loop.get_task(self.conn, task_done)["status"],
                         "done")
        self.assertEqual(loop.get_task(self.conn, other_task)["status"],
                         "created")
        # Audit + activity record the stop.
        audit_rows = self.conn.execute(
            "SELECT action FROM audit_log WHERE action = 'mission.stopped'"
        ).fetchall()
        self.assertEqual(len(audit_rows), 1)
        changed = models.list_activity(self.conn, mission["id"],
                                       kind="state_changed")
        self.assertEqual(len(changed), 2)  # activate + stop

    def test_stop_twice_second_is_409_and_side_effects_do_not_repeat(self):
        mission = self._create()
        models.transition(self.conn, mission["id"], "active")
        task_id = self._task_for(mission["id"])
        self._grant(task_id)
        missions_api.stop_mission(mission["id"])
        # A grant minted after the stop (e.g. by a racing approve)
        # must NOT be swept by a second, refused stop.
        self._grant(task_id)
        self._assert_http(409, missions_api.stop_mission, mission["id"])
        self.assertEqual(self._grants(task_id), 1)

    def test_stop_all(self):
        live = self._create(name="Live")
        models.transition(self.conn, live["id"], "active")
        draft = self._create(name="Draft")
        paused = self._create(name="Paused")
        models.transition(self.conn, paused["id"], "active")
        models.transition(self.conn, paused["id"], "paused")
        already = self._create(name="Already stopped")
        models.stop_mission(self.conn, already["id"])

        result = missions_api.stop_all_missions()
        self.assertEqual(set(result["stopped"]),
                         {live["id"], draft["id"], paused["id"]})
        for mission_id in (live["id"], draft["id"], paused["id"]):
            self.assertEqual(
                models.get_mission(self.conn, mission_id)["status"],
                "stopped")
        self.assertEqual(
            models.get_mission(self.conn, already["id"])["status"],
            "stopped")


class TestMissionApprovals(MissionTestBase):
    def test_approvals_listing_joins_tasks(self):
        mission = self._create()
        task_id = self._task_for(mission["id"])
        other_task = loop.create_task(self.conn, "Not a mission task")
        live = self._pending(
            task_id, action="submit", step=1,
            detail={"element_id": "el_submit", "role_id": "role_1"})
        self._pending(task_id, action="submit", step=2, ttl=-10)
        self._pending(other_task, action="submit", step=1)

        result = missions_api.get_mission_approvals(mission["id"])
        self.assertEqual(result["mission_id"], mission["id"])
        self.assertEqual(len(result["approvals"]), 1)
        approval = result["approvals"][0]
        self.assertEqual(approval["approval_id"], live)
        self.assertEqual(approval["task_id"], task_id)
        self.assertEqual(approval["step"], 1)
        self.assertEqual(approval["action"], "submit")
        self.assertEqual(approval["element_id"], "el_submit")
        self.assertEqual(approval["role_id"], "role_1")

    def test_approvals_listing_flattens_draft_id(self):
        mission = self._create(type="cold_mail")
        task_id = self._task_for(mission["id"])
        draft_id = self._draft(task_id)
        self._pending(task_id, action="mail_send", step=1,
                      detail={"draft_id": draft_id})
        result = missions_api.get_mission_approvals(mission["id"])
        self.assertEqual(len(result["approvals"]), 1)
        self.assertEqual(result["approvals"][0]["draft_id"], draft_id)

    def test_approvals_aggregate_covers_all_missions(self):
        first = self._create(name="First")
        second = self._create(name="Second")
        task_a = self._task_for(first["id"])
        task_b = self._task_for(second["id"])
        other = loop.create_task(self.conn, "Not a mission task")
        self._pending(task_a)
        self._pending(task_b)
        self._pending(other)
        result = missions_api.list_all_pending_approvals()
        self.assertEqual(len(result["approvals"]), 2)
        self.assertEqual(
            {a["mission_id"] for a in result["approvals"]},
            {first["id"], second["id"]})

    def test_get_mission_detail(self):
        mission = self._create()
        task_id = self._task_for(mission["id"])
        models.record_activity(self.conn, mission["id"], "discovered",
                               {"role_id": "role_1"})
        self._pending(task_id)
        detail = missions_api.get_mission(mission["id"])
        self.assertEqual(detail["id"], mission["id"])
        self.assertEqual(len(detail["tasks"]), 1)
        self.assertEqual(detail["tasks"][0]["id"], task_id)
        self.assertEqual(detail["tasks"][0]["status"], "created")
        self.assertEqual(len(detail["activity"]), 1)
        self.assertEqual(detail["activity"][0]["kind"], "discovered")
        self.assertEqual(len(detail["pending_approvals"]), 1)

    def test_approve_mail_mints_draft_grant_and_send_consumes_it(self):
        mission = self._create(type="cold_mail")
        models.transition(self.conn, mission["id"], "active")
        task_id = self._task_for(mission["id"])
        draft_id = self._draft(task_id)
        approval_id = self._pending(
            task_id, action="mail_send", step=1,
            detail={"draft_id": draft_id})

        result = missions_api.approve_mail(
            mission["id"],
            missions_api.ApproveMailIn(approval_id=approval_id,
                                       draft_id=draft_id))
        self.assertEqual(result, {"draft_id": draft_id,
                                  "status": "approved"})
        self.assertEqual(
            draft_mod.get_draft(self.conn, draft_id)["status"],
            "approved")
        # The grant names the draft, so the existing send finds it.
        self.assertIsNotNone(
            missions_api._live_grant_for_draft(self.conn, task_id,
                                               draft_id))
        # The pending approval is consumed.
        self.assertIsNone(loop.get_pending_approval(self.conn, approval_id))
        # The mission log records the approval.
        approved = models.list_activity(self.conn, mission["id"],
                                        kind="approved")
        self.assertEqual(len(approved), 1)
        # The existing send endpoint consumes the grant and sends
        # (mock transport — no real Gmail).
        sent = mail_api.send_draft(draft_id)
        self.assertEqual(sent["status"], "sent")
        self.assertEqual(
            draft_mod.get_draft(self.conn, draft_id)["status"], "sent")
        self.assertEqual(self._grants(task_id), 0)

    def test_approve_mail_made_up_id_is_404(self):
        # The Bug 3 regression, mission-flavoured: a made-up
        # approval id must mint no grant.
        mission = self._create(type="cold_mail")
        task_id = self._task_for(mission["id"])
        draft_id = self._draft(task_id)
        self._assert_http(
            404, missions_api.approve_mail, mission["id"],
            missions_api.ApproveMailIn(approval_id="made-up-123",
                                       draft_id=draft_id))
        self.assertEqual(self._grants(task_id), 0)
        self.assertEqual(
            draft_mod.get_draft(self.conn, draft_id)["status"], "draft")

    def test_approve_mail_wrong_mission_is_409(self):
        mission = self._create(type="cold_mail", name="Mine")
        other = self._create(type="cold_mail", name="Theirs")
        task_id = self._task_for(other["id"])
        draft_id = self._draft(task_id)
        approval_id = self._pending(
            task_id, action="mail_send", step=1,
            detail={"draft_id": draft_id})
        self._assert_http(
            409, missions_api.approve_mail, mission["id"],
            missions_api.ApproveMailIn(approval_id=approval_id,
                                       draft_id=draft_id))
        self.assertEqual(self._grants(task_id), 0)

    def test_approve_mail_expired_is_409(self):
        mission = self._create(type="cold_mail")
        task_id = self._task_for(mission["id"])
        draft_id = self._draft(task_id)
        approval_id = self._pending(
            task_id, action="mail_send", step=1,
            detail={"draft_id": draft_id}, ttl=-10)
        self._assert_http(
            409, missions_api.approve_mail, mission["id"],
            missions_api.ApproveMailIn(approval_id=approval_id,
                                       draft_id=draft_id))
        self.assertEqual(self._grants(task_id), 0)

    def test_approve_mail_replay_is_409(self):
        mission = self._create(type="cold_mail")
        task_id = self._task_for(mission["id"])
        draft_id = self._draft(task_id)
        approval_id = self._pending(
            task_id, action="mail_send", step=1,
            detail={"draft_id": draft_id})
        missions_api.approve_mail(
            mission["id"],
            missions_api.ApproveMailIn(approval_id=approval_id,
                                       draft_id=draft_id))
        self._assert_http(
            409, missions_api.approve_mail, mission["id"],
            missions_api.ApproveMailIn(approval_id=approval_id,
                                       draft_id=draft_id))
        self.assertEqual(self._grants(task_id), 1)

    def test_approve_mail_wrong_draft_is_409(self):
        mission = self._create(type="cold_mail")
        task_id = self._task_for(mission["id"])
        draft_a = self._draft(task_id, role_id="role_1")
        draft_b = self._draft(task_id, role_id="role_2")
        approval_id = self._pending(
            task_id, action="mail_send", step=1,
            detail={"draft_id": draft_a})
        self._assert_http(
            409, missions_api.approve_mail, mission["id"],
            missions_api.ApproveMailIn(approval_id=approval_id,
                                       draft_id=draft_b))
        # Nothing consumed: the approval still covers draft A.
        self.assertIsNotNone(
            loop.get_pending_approval(self.conn, approval_id))
        self.assertEqual(self._grants(task_id), 0)

    def test_approve_mail_submit_approval_cannot_send(self):
        # A submit approval (no draft_id in its detail) must not
        # be spendable on a mail send.
        mission = self._create(type="cold_mail")
        task_id = self._task_for(mission["id"])
        draft_id = self._draft(task_id)
        approval_id = self._pending(
            task_id, action="submit", step=1,
            detail={"element_id": "el_submit"})
        self._assert_http(
            409, missions_api.approve_mail, mission["id"],
            missions_api.ApproveMailIn(approval_id=approval_id,
                                       draft_id=draft_id))
        self.assertIsNotNone(
            loop.get_pending_approval(self.conn, approval_id))
        self.assertEqual(self._grants(task_id), 0)
        self.assertEqual(
            draft_mod.get_draft(self.conn, draft_id)["status"], "draft")

    def test_approve_mail_unknown_draft_or_mission_is_404(self):
        mission = self._create(type="cold_mail")
        task_id = self._task_for(mission["id"])
        approval_id = self._pending(task_id, action="mail_send", step=1,
                                    detail={"draft_id": "d_missing"})
        self._assert_http(
            404, missions_api.approve_mail, mission["id"],
            missions_api.ApproveMailIn(approval_id=approval_id,
                                       draft_id="d_missing"))
        self._assert_http(
            404, missions_api.approve_mail, "m_missing",
            missions_api.ApproveMailIn(approval_id=approval_id,
                                       draft_id="d_missing"))

    def test_approve_mail_sent_draft_is_409(self):
        mission = self._create(type="cold_mail")
        task_id = self._task_for(mission["id"])
        draft_id = self._draft(task_id, status="sent")
        approval_id = self._pending(
            task_id, action="mail_send", step=1,
            detail={"draft_id": draft_id})
        self._assert_http(
            409, missions_api.approve_mail, mission["id"],
            missions_api.ApproveMailIn(approval_id=approval_id,
                                       draft_id=draft_id))
        # The pending approval survives — the refusal was about
        # the draft, not the approval.
        self.assertIsNotNone(
            loop.get_pending_approval(self.conn, approval_id))


class TestActivity(MissionTestBase):
    def test_record_and_list_activity(self):
        mission = self._create()
        first = models.record_activity(
            self.conn, mission["id"], "discovered", {"role_id": "role_1"})
        second = models.record_activity(
            self.conn, mission["id"], "matched", {"role_id": "role_1"},
            run_id="mr_aaaaaaaaaaaa")
        self.assertTrue(first.startswith("ma_"))
        # Newest first.
        rows = models.list_activity(self.conn, mission["id"])
        self.assertEqual([r["id"] for r in rows], [second, first])
        self.assertEqual(rows[0]["run_id"], "mr_aaaaaaaaaaaa")
        # Detail is the stored JSON string (§3.3 shape).
        self.assertEqual(json.loads(rows[0]["detail"]),
                         {"role_id": "role_1"})
        self.assertEqual(
            [r["id"] for r in models.list_activity(
                self.conn, mission["id"], kind="discovered")],
            [first])
        self.assertEqual(
            models.list_activity(self.conn, mission["id"], limit=1),
            [rows[0]])

    def test_record_unknown_kind_is_422(self):
        mission = self._create()
        with self.assertRaises(ValueError):
            models.record_activity(self.conn, mission["id"], "teleported",
                                   {})

    def test_record_unknown_mission_is_keyerror(self):
        with self.assertRaises(KeyError):
            models.record_activity(self.conn, "m_missing", "discovered",
                                   {})

    def test_activity_endpoint(self):
        mission = self._create()
        models.record_activity(self.conn, mission["id"], "discovered", {})
        # limit is passed explicitly: the Query(...) default only
        # resolves inside FastAPI; a direct call gets the Query
        # object, which SQLite cannot bind.
        result = missions_api.get_mission_activity(mission["id"], limit=50)
        self.assertEqual(result["mission_id"], mission["id"])
        self.assertEqual(len(result["activity"]), 1)
        self._assert_http(
            404, missions_api.get_mission_activity, "m_missing")
        self._assert_http(
            422, missions_api.get_mission_activity, mission["id"],
            kind="teleported")


class TestMissionTypes(MissionTestBase):
    def test_types_endpoint_lists_the_registry(self):
        result = missions_api.list_mission_types()
        by_type = {t["type"]: t for t in result["types"]}
        # PR #7 adds email_monitor (inbox watch).
        self.assertEqual(set(by_type),
                         {"job_hunt", "cold_mail", "price_watch",
                          "email_monitor"})
        for spec in result["types"]:
            self.assertIn("label", spec)
            self.assertIn("config_schema", spec)
            self.assertIn("default_bounds", spec)
            self.assertIn("default_interval_min", spec)
            # The whole response must be JSON-serialisable (the
            # registry's validators are callables — they must not
            # leak into the response).
            json.dumps(spec)
        self.assertEqual(by_type["job_hunt"]["label"], "Job hunt")
        self.assertEqual(
            by_type["job_hunt"]["default_bounds"]["max_applies_per_day"],
            3)
        self.assertEqual(
            by_type["cold_mail"]["default_bounds"]["max_sends_per_day"],
            5)
        self.assertEqual(
            by_type["price_watch"]["default_bounds"]["max_applies_per_day"],
            0)
        # Schema entries carry their type name and default (the
        # real registry says "integer", the §3.7 tuple sketch says
        # "int" — accept either; the default is what the UI needs).
        self.assertIn(
            by_type["job_hunt"]["config_schema"]["min_score"]["type"],
            ("int", "integer"))
        self.assertEqual(
            by_type["job_hunt"]["config_schema"]["min_score"]["default"],
            60)
        self.assertEqual(
            by_type["price_watch"]["config_schema"]["url"]["required"],
            True)


if __name__ == "__main__":
    unittest.main()
