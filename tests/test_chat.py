"""Chat tests (PR #6): the Agent view is a real chat thread.

Three layers:
- TestClassifyRules: the pure decision module (no DB, no network).
- TestChatRespond: respond() in-process on a temp DB, planner
  injected (fake / raising) — the planner path + rules fallback.
- TestChatApi: the live server subprocess (planner disabled via
  env, like test_tasks.py) — the HTTP contract, steering, and
  history. Each test starts from a clean thread: setUp clears the
  chat + task tables in the server's DB, the same direct-DB
  pattern test_tasks.py uses for steps.

Run: /tmp/eclipse-venv/bin/python -m unittest tests.test_chat -v
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server.api import chat_api  # noqa: E402
from server.chat import classify  # noqa: E402
from server.core import config, db  # noqa: E402
from server.planner.router import PlannerError  # noqa: E402
from server.tasks import loop  # noqa: E402

TOKEN = "test-token-chat-123"


# ----------------------------------------------------------------
# Pure classification
# ----------------------------------------------------------------

class TestClassifyRules(unittest.TestCase):
    def test_greetings_are_chat_never_a_task(self):
        for text in ("hi", "hello", "good morning", "hey",
                     "good evening", "hi there"):
            with self.subTest(text=text):
                result = classify.classify_rules(text)
                self.assertEqual(result["kind"], "chat")
                self.assertTrue(result["hard_chat"])
                self.assertEqual(result["reason"], "greeting")

    def test_greeting_reply_greets_back(self):
        self.assertTrue(
            classify.classify_rules("hi")["reply_text"].startswith("Hi!"))
        self.assertTrue(
            classify.classify_rules("good morning")["reply_text"]
            .startswith("Good morning!"))

    def test_greeting_plus_instruction_is_a_task(self):
        # "hi, find me jobs" is a task wearing a greeting — the
        # greeting must not swallow the instruction.
        result = classify.classify_rules(
            "hi, find me remote SDE intern roles")
        self.assertEqual(result["kind"], "task_created")

    def test_small_talk_variants(self):
        cases = {
            "thanks": "thanks",
            "thank you so much": "thanks",
            "bye": "bye",
            "goodbye": "bye",
            "how are you": "how_are_you",
            "what can you do": "capabilities",
            "help": "capabilities",
            "who are you": "capabilities",
        }
        for text, reason in cases.items():
            with self.subTest(text=text):
                result = classify.classify_rules(text)
                self.assertEqual(result["kind"], "chat")
                self.assertTrue(result["hard_chat"])
                self.assertEqual(result["reason"], reason)

    def test_imperative_with_object_is_a_task(self):
        for text in (
            "Find me remote SDE intern roles in Hyderabad",
            "watch for SDE intern roles in Hyderabad",
            "find internships",
            "please find remote roles",
            "can you watch prices on amazon",
            "i want you to draft a mail to the recruiter",
            "look for SDE roles",
            "apply to the Acme role",
            "remind me to check my mail",
        ):
            with self.subTest(text=text):
                result = classify.classify_rules(text)
                self.assertEqual(result["kind"], "task_created", text)

    def test_task_reply_confirms_in_plain_words(self):
        reply = classify.classify_rules(
            "Find me remote SDE intern roles in Hyderabad")["reply_text"]
        self.assertIn("remote SDE intern roles in Hyderabad", reply)

    def test_apply_task_reply_promises_approval(self):
        reply = classify.classify_rules(
            "watch for SDE roles and apply to matching ones")["reply_text"]
        self.assertIn("ask before each apply", reply)

    def test_verb_without_object_is_ambiguous(self):
        # "find it" has no object — it is a fragment, not a task.
        result = classify.classify_rules("find it")
        self.assertEqual(result["kind"], "chat")
        self.assertEqual(result["reason"], "ambiguous")

    def test_ambiguous_fragment_asks_one_question(self):
        result = classify.classify_rules("internships")
        self.assertEqual(result["kind"], "chat")
        self.assertEqual(result["reason"], "ambiguous")
        self.assertIn("intern roles", result["reply_text"])
        self.assertIn("?", result["reply_text"])


class TestClassifySteering(unittest.TestCase):
    def test_stop_commands_cancel(self):
        for text in ("stop", "cancel it", "please stop", "never mind",
                     "stop the job hunt", "no, cancel it"):
            with self.subTest(text=text):
                self.assertEqual(
                    classify.classify_steering(text), "cancel")

    def test_pause_commands_pause(self):
        for text in ("pause this", "pause", "hold on"):
            with self.subTest(text=text):
                self.assertEqual(
                    classify.classify_steering(text), "pause")

    def test_refinements_refine(self):
        for text in ("remote only", "no, remote only",
                     "only in Hyderabad", "min 15k stipend",
                     "not that company", "remote", "Hyderabad",
                     "don't apply to startups", "under 5000"):
            with self.subTest(text=text):
                self.assertEqual(
                    classify.classify_steering(text), "refine")

    def test_new_task_instruction_is_never_steering(self):
        # An instruction with an active task starts a NEW task —
        # even one carrying a constraint word ("min 15k").
        self.assertIsNone(classify.classify_steering(
            "find ML roles in Bangalore"))
        self.assertIsNone(classify.classify_steering(
            "find roles with min 15k stipend"))

    def test_greeting_and_bare_no_are_not_steering(self):
        self.assertIsNone(classify.classify_steering("hi"))
        self.assertIsNone(classify.classify_steering("thanks"))
        # A bare "no" answers a question; it refines nothing.
        self.assertIsNone(classify.classify_steering("no"))


class TestPlannerPromptAndParsing(unittest.TestCase):
    def test_prompt_carries_history_and_facts(self):
        prompt = classify.build_chat_prompt(
            "remote ones",
            [{"role": "user", "text": "internships"},
             {"role": "agent", "text": "Are you looking for intern roles?"}],
            ["user is in Hyderabad"],
        )
        self.assertIn("MESSAGE: remote ones", prompt)
        self.assertIn("user: internships", prompt)
        self.assertIn("user is in Hyderabad", prompt)

    def test_parse_valid_planner_reply(self):
        parsed = classify.parse_planner_reply(
            '```json\n{"kind": "task_created",'
            ' "reply_text": "On it.", "constraints": "remote only"}\n```'
        )
        self.assertEqual(parsed["kind"], "task_created")
        self.assertEqual(parsed["reply_text"], "On it.")
        self.assertEqual(parsed["constraints"], "remote only")

    def test_parse_rejects_garbage_and_bad_kinds(self):
        for raw in ("not json at all",
                    '{"kind": "moon", "reply_text": "hi"}',
                    '{"kind": "chat"}',
                    '{"kind": "chat", "reply_text": "  "}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    classify.parse_planner_reply(raw)


# ----------------------------------------------------------------
# respond() in-process, planner injected
# ----------------------------------------------------------------

def _planner_call(payload: dict):
    def _call(prompt: str, **kwargs):
        _call.prompts.append(prompt)
        return json.dumps(payload), "fake:model", 1.0

    _call.prompts = []
    return _call


def _raising_call(exc: Exception):
    def _call(prompt: str, **kwargs):
        raise exc

    return _call


class TestChatRespond(unittest.TestCase):
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

    def _task_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM tasks").fetchone()[0]

    def test_planner_task_path_creates_task_with_constraints(self):
        call = _planner_call({
            "kind": "task_created",
            "reply_text": "I'll look for those roles.",
            "constraints": "remote only",
        })
        # PR #7: "watch for SDE intern roles" now drafts a
        # mission (standing watch), so the planner task path is
        # exercised with a plain one-off instruction.
        result = chat_api.respond(
            self.conn, "find SDE intern roles in Hyderabad",
            call_fn=call,
        )
        self.assertEqual(result["source"], "planner")
        self.assertEqual(result["kind"], "task_created")
        self.assertIsNotNone(result["task_id"])
        task = loop.get_task(self.conn, result["task_id"])
        self.assertEqual(task["goal"],
                         "find SDE intern roles in Hyderabad")
        self.assertEqual(task["constraints"], "remote only")

    def test_planner_chat_path_creates_no_task(self):
        call = _planner_call({
            "kind": "chat", "reply_text": "Sure thing.",
            "constraints": None,
        })
        result = chat_api.respond(
            self.conn, "what did you find so far", call_fn=call)
        self.assertEqual(result["source"], "planner")
        self.assertEqual(result["kind"], "chat")
        self.assertIsNone(result["task_id"])
        self.assertEqual(self._task_count(), 0)

    def test_planner_failure_falls_back_to_rules(self):
        result = chat_api.respond(
            self.conn, "Find me remote SDE intern roles in Hyderabad",
            call_fn=_raising_call(PlannerError("every endpoint failed")),
        )
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["kind"], "task_created")
        self.assertIsNotNone(result["task_id"])

    def test_planner_garbage_falls_back_to_rules(self):
        def _call(prompt: str, **kwargs):
            return "not json at all", "fake:model", 1.0

        result = chat_api.respond(
            self.conn, "Find me remote SDE intern roles",
            call_fn=_call)
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["kind"], "task_created")

    def test_greeting_never_calls_the_planner(self):
        # The planner would raise if called at all: a greeting
        # must be answered by the rules, never a model call.
        result = chat_api.respond(
            self.conn, "hi",
            call_fn=_raising_call(AssertionError("planner called")),
        )
        self.assertEqual(result["kind"], "chat")
        self.assertEqual(result["source"], "rules")
        self.assertIsNone(result["task_id"])
        self.assertEqual(self._task_count(), 0)

    def test_planner_cannot_upgrade_a_context_free_fragment(self):
        # "internships" as the first message, planner wrongly says
        # task: the rules win — a bare noun is never a task.
        call = _planner_call({
            "kind": "task_created",
            "reply_text": "I'll find internships.",
            "constraints": None,
        })
        result = chat_api.respond(self.conn, "internships", call_fn=call)
        self.assertEqual(result["kind"], "chat")
        self.assertEqual(result["source"], "rules")
        self.assertIsNone(result["task_id"])
        self.assertEqual(self._task_count(), 0)

    def test_planner_may_resolve_a_fragment_with_history(self):
        # "internships" -> clarification; "remote ones" answers it.
        # With history behind it, the planner's resolution stands.
        chat_api.respond(
            self.conn, "internships",
            call_fn=_raising_call(PlannerError("no planner")),
        )
        call = _planner_call({
            "kind": "task_created",
            "reply_text": "I'll find remote intern roles.",
            "constraints": "remote only",
        })
        result = chat_api.respond(self.conn, "remote ones", call_fn=call)
        self.assertEqual(result["kind"], "task_created")
        self.assertEqual(result["source"], "planner")
        self.assertIsNotNone(result["task_id"])
        self.assertIn("internships", call.prompts[0])


# ----------------------------------------------------------------
# HTTP contract against a live server (planner disabled)
# ----------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(base_url, method, path, token=TOKEN, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    if token is not None:
        req.add_header("X-Eclipse-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())
    except urllib.error.URLError:
        return 0, {}


class TestChatApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env["ECLIPSE_DATA_DIR"] = cls.tmp.name
        env["ECLIPSE_TOKEN"] = TOKEN
        # No planner keys, and Ollama pointed at a dead port:
        # every non-greeting message must fall back to the rules,
        # labelled source "rules" (same env as test_tasks.py).
        env.pop("ANTSEED_API_KEY", None)
        env.pop("GROQ_API_KEY", None)
        env["ANTSEED_USE_SURROGATE"] = "0"
        env["OLLAMA_BASE_URL"] = "http://127.0.0.1:1/v1"
        venv_python = REPO_ROOT / ".venv" / "bin" / "python"
        python = str(venv_python) if venv_python.exists() else sys.executable
        cls.proc = subprocess.Popen(
            [python, "-m", "uvicorn", "server.main:app",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            status, _ = _request(cls.base, "GET", "/health")
            if status == 200:
                break
            time.sleep(0.3)
        else:
            raise RuntimeError("server did not come up for chat API test")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.tmp.cleanup()

    def setUp(self):
        # A clean thread per test (direct-DB clear, the same
        # test-only pattern test_tasks.py uses for steps).
        conn = sqlite3.connect(f"{self.tmp.name}/eclipse.db")
        try:
            for table in ("chat_messages", "tasks", "steps",
                          "checkpoints", "receipts"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        finally:
            conn.close()

    def _chat(self, text):
        return _request(self.base, "POST", "/chat/message",
                        body={"text": text})

    def _tasks(self):
        status, body = _request(self.base, "GET", "/tasks")
        self.assertEqual(status, 200, body)
        return body["tasks"]

    def _create_task_via_chat(self):
        status, body = self._chat(
            "Find me remote SDE intern roles in Hyderabad")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "task_created")
        return body["task_id"]

    def test_greeting_creates_no_task(self):
        for text in ("hi", "hello", "good morning"):
            status, body = self._chat(text)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["kind"], "chat", body)
            self.assertIsNone(body["task_id"], body)
        self.assertEqual(self._tasks(), [])

    def test_imperative_creates_task_visible_via_tasks_api(self):
        task_id = self._create_task_via_chat()
        status, body = _request(self.base, "GET", f"/tasks/{task_id}")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["goal"],
                         "Find me remote SDE intern roles in Hyderabad")
        self.assertEqual(body["status"], "created")

    def test_ambiguous_fragment_asks_for_more_detail(self):
        status, body = self._chat("internships")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "chat")
        self.assertIsNone(body["task_id"])
        self.assertIn("?", body["reply_text"])
        self.assertEqual(self._tasks(), [])

    def test_history_oldest_first_with_linkage(self):
        self._chat("hi")
        task_id = self._create_task_via_chat()
        self._chat("thanks")
        status, body = _request(self.base, "GET", "/chat/history")
        self.assertEqual(status, 200, body)
        messages = body["messages"]
        self.assertEqual(len(messages), 6)
        self.assertEqual(
            [m["role"] for m in messages],
            ["user", "agent", "user", "agent", "user", "agent"])
        self.assertEqual(messages[0]["text"], "hi")
        self.assertEqual(messages[2]["text"],
                         "Find me remote SDE intern roles in Hyderabad")
        created = [m["created_at"] for m in messages]
        self.assertEqual(created, sorted(created))
        task_reply = messages[3]
        self.assertEqual(task_reply["kind"], "task_created")
        self.assertEqual(task_reply["task_id"], task_id)
        self.assertIsNone(messages[1]["task_id"])

    def test_history_carries_source_on_agent_rows(self):
        self._chat("hi")
        status, body = _request(self.base, "GET", "/chat/history")
        self.assertEqual(status, 200, body)
        messages = body["messages"]
        self.assertEqual(len(messages), 2)
        self.assertIsNone(messages[0]["source"])
        self.assertEqual(messages[1]["source"], "rules")

    def test_rules_source_when_no_planner_configured(self):
        status, body = self._chat(
            "Find me remote SDE intern roles in Hyderabad")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["source"], "rules")

    def test_auth_and_validation(self):
        status, _ = _request(self.base, "POST", "/chat/message",
                             token=None, body={"text": "hi"})
        self.assertEqual(status, 401)
        status, _ = _request(self.base, "GET", "/chat/history", token=None)
        self.assertEqual(status, 401)
        status, _ = self._chat("   ")
        self.assertEqual(status, 422)
        status, _ = self._chat("")
        self.assertEqual(status, 422)

    def test_stop_cancels_the_active_task(self):
        task_id = self._create_task_via_chat()
        status, body = self._chat("stop")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "chat")
        self.assertEqual(body["task_id"], task_id)
        self.assertIn("Stopped", body["reply_text"])
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], task_id)
        self.assertEqual(tasks[0]["status"], "cancelled")

    def test_pause_pauses_the_active_task(self):
        task_id = self._create_task_via_chat()
        status, body = self._chat("pause this")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "chat")
        self.assertEqual(body["task_id"], task_id)
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "paused")

    def test_stop_with_no_active_task_is_honest(self):
        status, body = self._chat("stop")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "chat")
        self.assertIsNone(body["task_id"])
        self.assertIn("nothing to stop", body["reply_text"])
        self.assertEqual(self._tasks(), [])

    def test_refinement_updates_constraints_no_new_task(self):
        task_id = self._create_task_via_chat()
        status, body = self._chat("remote only")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "chat")
        self.assertEqual(body["task_id"], task_id)
        self.assertIn("remote only", body["reply_text"])
        # The task is still "created" (unplanned), so the reply
        # says the plan will follow the refinement.
        self.assertIn("plan will follow it", body["reply_text"])
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        status, task = _request(self.base, "GET", f"/tasks/{task_id}")
        self.assertEqual(status, 200, task)
        self.assertIn("remote only", (task["constraints"] or "").lower())

    def test_refinement_without_active_task_falls_through(self):
        status, body = self._chat("remote only")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "chat")
        self.assertIsNone(body["task_id"])
        self.assertIn("?", body["reply_text"])
        self.assertEqual(self._tasks(), [])

    def test_new_instruction_with_active_task_starts_a_new_task(self):
        first = self._create_task_via_chat()
        # PR #7: "watch the price of headphones" now drafts a
        # price-watch mission instead of a task.
        status, body = self._chat("find the price of headphones")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "task_created")
        self.assertNotEqual(body["task_id"], first)
        self.assertEqual(len(self._tasks()), 2)


if __name__ == "__main__":
    unittest.main()
