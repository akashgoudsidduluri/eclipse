"""Deterministic no-key navigation tests (PR #7, W3).

server/planner/deterministic.py resolves a pure navigation goal
("open google") to a navigate step with zero model calls, and
server/tasks/loop.py plans + executes + verifies such a step
without ever calling the planner. No API key, no live model, no
Ollama: the "no planner" proof is a call_fn that raises if it is
ever invoked.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server.core import config, db  # noqa: E402
from server.planner import deterministic as det  # noqa: E402
from server.tasks import loop  # noqa: E402


def _never_call(prompt, **kwargs):
    raise AssertionError("the planner must not be called for this goal")


class TestResolveNavigate(unittest.TestCase):
    def test_known_sites(self):
        cases = {
            "open google": "https://www.google.com",
            "open youtube": "https://www.youtube.com",
            "open github": "https://github.com",
            "open linkedin": "https://www.linkedin.com",
            "open internshala": "https://internshala.com",
            "open twitter": "https://x.com",
            "open x": "https://x.com",
            "open wikipedia": "https://www.wikipedia.org",
            "open stack overflow": "https://stackoverflow.com",
            "open leetcode": "https://leetcode.com",
            "open gmail": "https://mail.google.com",
        }
        for goal, url in cases.items():
            with self.subTest(goal=goal):
                resolved = det.resolve_navigate(goal)
                self.assertIsNotNone(resolved, goal)
                self.assertEqual(resolved, {"action": "navigate", "url": url})

    def test_verb_variants(self):
        for goal in ("go to youtube", "goto youtube", "visit youtube",
                     "navigate to youtube", "load youtube",
                     "show me youtube", "take me to youtube"):
            with self.subTest(goal=goal):
                resolved = det.resolve_navigate(goal)
                self.assertIsNotNone(resolved, goal)
                self.assertEqual(resolved["url"], "https://www.youtube.com")

    def test_case_and_politeness(self):
        for goal in ("Open Google", "OPEN GOOGLE", "please open google",
                     "Please open google.", "hey, open google",
                     "can you open google", "could you please open youtube"):
            with self.subTest(goal=goal):
                resolved = det.resolve_navigate(goal)
                self.assertIsNotNone(resolved, goal)

    def test_trailing_words_and_punctuation(self):
        for goal in ("open google website", "open google site",
                     "open google.", "open google!", "open youtube app"):
            with self.subTest(goal=goal):
                resolved = det.resolve_navigate(goal)
                self.assertIsNotNone(resolved, goal)

    def test_raw_urls(self):
        resolved = det.resolve_navigate("open https://github.com/Prateek2007-cmd")
        self.assertEqual(resolved["url"], "https://github.com/Prateek2007-cmd")
        resolved = det.resolve_navigate("go to http://example.com/page?q=1")
        self.assertEqual(resolved["url"], "http://example.com/page?q=1")

    def test_bare_domains(self):
        self.assertEqual(
            det.resolve_navigate("open google.com")["url"],
            "https://google.com",
        )
        self.assertEqual(
            det.resolve_navigate("open www.youtube.com")["url"],
            "https://www.youtube.com",
        )
        self.assertEqual(
            det.resolve_navigate("open en.wikipedia.org/wiki/Delhi")["url"],
            "https://en.wikipedia.org/wiki/Delhi",
        )

    def test_ambiguous_returns_none(self):
        for goal in (
            "", "hello", "open", "open the settings",
            "open google and search for cats",
            "open google then click the first result",
            "find me a job", "search google for cats",
            "open my emails", "what is google",
            "open the fridge", "go to the shops",
        ):
            with self.subTest(goal=goal):
                self.assertIsNone(det.resolve_navigate(goal), goal)

    def test_non_http_url_rejected(self):
        self.assertIsNone(det.resolve_navigate("open ftp://example.com"))
        self.assertIsNone(det.resolve_navigate("open javascript:alert(1)"))

    def test_allow_list_rejection(self):
        # The task is constrained to github.com — google is outside it.
        self.assertIsNone(
            det.resolve_navigate("open google", allowed_sites=["github.com"])
        )
        # The target itself is allowed.
        resolved = det.resolve_navigate(
            "open google", allowed_sites=["google.com"]
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["url"], "https://www.google.com")
        # Subdomains of an allowed site are allowed (the validate.py rule).
        resolved = det.resolve_navigate(
            "open mail.google.com", allowed_sites=["google.com"]
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["url"], "https://mail.google.com")


class TestDeterministicPlan(unittest.TestCase):
    def test_plan_shape(self):
        steps = det.deterministic_plan("open google")
        self.assertIsNotNone(steps)
        self.assertEqual(len(steps), 1)
        step = steps[0]
        self.assertEqual(step["step"], 1)
        self.assertEqual(step["action"], "navigate")
        self.assertEqual(step["url"], "https://www.google.com")
        self.assertIn("google.com", step["expected_end_state"])
        self.assertIn("https://www.google.com", step["description"])

    def test_plan_none_for_non_navigation(self):
        self.assertIsNone(det.deterministic_plan("find me a job"))
        self.assertIsNone(det.deterministic_plan("open google and search"))

    def test_plan_reads_allowed_sites_from_constraints(self):
        steps = det.deterministic_plan(
            "open google", constraints="allowed sites: github.com"
        )
        self.assertIsNone(steps)
        steps = det.deterministic_plan(
            "open google", constraints="allowed sites: google.com"
        )
        self.assertIsNotNone(steps)


class TestLoopIntegration(unittest.TestCase):
    """plan -> execute -> verify for "open google" with no planner."""

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

    def test_plan_open_google_without_planner(self):
        task_id = loop.create_task(self.conn, "open google")
        steps = loop.plan_task(self.conn, task_id, call_fn=_never_call)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["action"], "navigate")
        self.assertEqual(steps[0]["url"], "https://www.google.com")
        task = loop.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "planned")
        row = self.conn.execute(
            "SELECT action_json FROM steps WHERE task_id = ? AND n = 1",
            (task_id,),
        ).fetchone()
        stored = json.loads(row["action_json"])
        self.assertEqual(stored["action"], "navigate")
        self.assertEqual(stored["url"], "https://www.google.com")

    def test_non_navigation_still_uses_planner(self):
        def fake_plan(prompt, **kwargs):
            return (
                '[{"step": 1, "description": "Do the thing",'
                ' "expected_end_state": "done"}]',
                "fake:test", 1.0,
            )

        task_id = loop.create_task(self.conn, "find me a job")
        steps = loop.plan_task(self.conn, task_id, call_fn=fake_plan)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["description"], "Do the thing")
        # A planner plan carries no action keys — it never takes
        # the deterministic execute branch.
        self.assertNotIn("action", steps[0])

    def test_allow_list_rejection_falls_through_to_planner(self):
        calls = []

        def fake_plan(prompt, **kwargs):
            calls.append(prompt)
            return (
                '[{"step": 1, "description": "Do the thing",'
                ' "expected_end_state": "done"}]',
                "fake:test", 1.0,
            )

        task_id = loop.create_task(
            self.conn, "open google", "allowed sites: github.com"
        )
        loop.plan_task(self.conn, task_id, call_fn=fake_plan)
        self.assertEqual(len(calls), 1)

    def test_execute_deterministic_step_without_planner(self):
        task_id = loop.create_task(self.conn, "open google")
        loop.plan_task(self.conn, task_id, call_fn=_never_call)
        # A blank tab: no URL, no elements — a navigate needs neither.
        result = loop.execute_step(
            self.conn, task_id, 1, {"url": "", "elements": []},
            call_fn=_never_call,
        )
        self.assertEqual(result["model"], "deterministic")
        self.assertEqual(result["action"], {
            "action": "navigate", "url": "https://www.google.com",
        })
        # The navigate target is authorised for this step, so the
        # executor permits it from a fresh tab.
        self.assertIn("www.google.com", result["allowed_sites"])
        self.assertEqual(result["fill"], {})
        step = self.conn.execute(
            "SELECT status FROM steps WHERE task_id = ? AND n = 1",
            (task_id,),
        ).fetchone()
        self.assertEqual(step["status"], "executing")

    def test_verify_navigate_reaches_host(self):
        task_id = loop.create_task(self.conn, "open google")
        loop.plan_task(self.conn, task_id, call_fn=_never_call)
        loop.execute_step(
            self.conn, task_id, 1, {"url": "", "elements": []},
            call_fn=_never_call,
        )
        verdict = loop.verify_step(
            self.conn, task_id, 1,
            {"url": "https://www.google.com", "elements": []},
            "The browser shows www.google.com",
            "",
            {"url": "", "elements": []},
        )
        self.assertTrue(verdict["verified"])
        self.assertIn("google.com", verdict["reason"])
        task = loop.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "done")

    def test_verify_navigate_from_new_tab_page(self):
        task_id = loop.create_task(self.conn, "open youtube")
        loop.plan_task(self.conn, task_id, call_fn=_never_call)
        loop.execute_step(
            self.conn, task_id, 1,
            {"url": "chrome://newtab/", "elements": []},
            call_fn=_never_call,
        )
        verdict = loop.verify_step(
            self.conn, task_id, 1,
            {"url": "https://www.youtube.com", "elements": []},
            "The browser shows www.youtube.com",
            "chrome://newtab/",
            {"url": "chrome://newtab/", "elements": []},
        )
        self.assertTrue(verdict["verified"])

    def test_verify_navigate_already_there_does_not_verify(self):
        # The end state was pre-existing (already on google) — the
        # baseline-diff rule says "action ran" is not verification.
        task_id = loop.create_task(self.conn, "open google")
        loop.plan_task(self.conn, task_id, call_fn=_never_call)
        loop.execute_step(
            self.conn, task_id, 1,
            {"url": "https://www.google.com", "elements": []},
            call_fn=_never_call,
        )
        verdict = loop.verify_step(
            self.conn, task_id, 1,
            {"url": "https://www.google.com", "elements": []},
            "The browser shows www.google.com",
            "https://www.google.com",
            {"url": "https://www.google.com", "elements": []},
        )
        self.assertFalse(verdict["verified"])
        self.assertTrue(verdict["retry"])


if __name__ == "__main__":
    unittest.main()
