"""Brain tests (PR #7): the chat brain — classifier categories,
conversational fallback, "remember ..." fact capture, the
local-first provider chain, and the .env fix.

Layers:
- TestClassifyIdentity: pure classifier — name statements,
  corrections, statements, "remember ..." parsing.
- TestChatRespondBrain: respond() in-process on a temp DB,
  planner injected — fact storage/overwrite, refusal, the
  no-repeat clarifier guard, honest no-model replies, provider
  labelling.
- TestProviderChain: router chain order, env override, probe,
  brain_status (no key material anywhere in the output).
- TestDotenv: a .env file's vars reach the router, and importing
  server.main loads a .env from the working directory.
- TestBrainEndpoint: GET /brain's handler in-process + the route
  is registered on the app.

Run: ~/workspace/eclipse-venv/bin/python -m unittest tests.test_brain_pr7 -v
(All keys below are fakes for tests; no real secret appears here.)
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server.api import chat_api, system as system_api  # noqa: E402
from server.chat import classify  # noqa: E402
from server.core import config, db  # noqa: E402
from server.memory import keyword  # noqa: E402
from server.planner import router  # noqa: E402
from server.planner.router import PlannerError  # noqa: E402

FAKE_KEY = "fake-test-key-not-real"


# ----------------------------------------------------------------
# Pure classification: identity / statement / correction / remember
# ----------------------------------------------------------------

class TestClassifyIdentity(unittest.TestCase):
    def test_im_akash_is_identity_not_a_fragment(self):
        # The live-test bug: "i'm Akash" used to become the bare
        # name "Akash" and get the same clarifying question twice.
        result = classify.classify_rules("i'm Akash")
        self.assertEqual(result["kind"], "chat")
        self.assertEqual(result["reason"], "identity")
        self.assertTrue(result["hard_chat"])
        self.assertEqual(result["identity"], {"name": "Akash", "correction": False})
        self.assertIn("Nice to meet you, Akash", result["reply_text"])
        self.assertIn("remember", result["reply_text"])

    def test_my_name_is_and_lowercase_name(self):
        for text, name in (
            ("my name is Akash", "Akash"),
            ("My name is Priya", "Priya"),
            ("i'm akash", "Akash"),
            ("i am Yash", "Yash"),
            ("call me Akash", "Akash"),
            ("it's Akash", "Akash"),
        ):
            with self.subTest(text=text):
                result = classify.classify_rules(text)
                self.assertEqual(result["reason"], "identity", text)
                self.assertEqual(result["identity"]["name"], name, text)
                self.assertFalse(result["identity"]["correction"], text)

    def test_corrections_are_corrections(self):
        for text in (
            "no, my name is Yash",
            "no my name is Yash",
            "actually i'm Yash",
            "wait, my name is Yash",
            "i just told you my name is Yash",
        ):
            with self.subTest(text=text):
                result = classify.classify_rules(text)
                self.assertEqual(result["reason"], "correction", text)
                self.assertEqual(result["identity"]["name"], "Yash", text)
                self.assertTrue(result["identity"]["correction"], text)
                self.assertIn("Yash", result["reply_text"])

    def test_correction_with_comma_and_vocative(self):
        # The exact live-test follow-up (screenshot verification
        # 2026-11-23): the comma after "you" defeated the
        # preamble strip and ", man" defeated the name tidy, so
        # the message fell through to the same clarifier again.
        for text in (
            "i just told you, my name is akash, man",
            "i already told you my name is Yash",
            "i told you, my name is Yash",
            "my name is akash, man",
            "my name is akash man",
            "listen, my name is Akash",
        ):
            with self.subTest(text=text):
                result = classify.classify_rules(text)
                self.assertIn(result["reason"], ("identity", "correction"), text)
                self.assertEqual(
                    result["identity"]["name"],
                    "Akash" if "akash" in text.lower() else "Yash",
                    text,
                )

    def test_name_plus_instruction_is_not_an_identity_either(self):
        # The bounded name capture must not swallow an
        # instruction that follows the name — same rule as the
        # "i'm Akash, find me jobs" case.
        for text in (
            "my name is Akash, find me jobs",
            "my name is Akash and I'm looking for jobs",
            "call me Akash, find me jobs",
        ):
            with self.subTest(text=text):
                result = classify.classify_rules(text)
                self.assertNotIn("identity", result, text)

    def test_im_looking_for_jobs_is_still_a_task(self):
        # The "i'm " prefix strip must keep working for verb
        # phrases: only a bare name is an identity statement.
        result = classify.classify_rules("i'm looking for jobs")
        self.assertEqual(result["kind"], "task_created")
        self.assertNotIn("identity", result)

    def test_im_tired_is_not_a_name(self):
        result = classify.classify_rules("i'm tired")
        self.assertNotIn("identity", result)
        self.assertEqual(result["kind"], "chat")

    def test_its_raining_is_not_a_name(self):
        result = classify.classify_rules("it's raining")
        self.assertNotIn("identity", result)

    def test_name_plus_instruction_is_not_an_identity(self):
        # "i'm Akash, find me jobs" is not a clean name statement
        # — it must not capture "Akash, find me jobs" as a name.
        result = classify.classify_rules("i'm Akash, find me jobs")
        self.assertNotIn("identity", result)

    def test_statements_are_statements(self):
        for text in (
            "i am a student",
            "i'm a student",
            "i live in Hyderabad",
            "i'm in Hyderabad",
            "i prefer remote roles",
            "i like remote work",
            "my city is Hyderabad",
        ):
            with self.subTest(text=text):
                result = classify.classify_rules(text)
                self.assertEqual(result["kind"], "chat", text)
                self.assertEqual(result["reason"], "statement", text)
                self.assertFalse(result["hard_chat"], text)
                # The rules fallback for a statement is honest,
                # never a clarifying question.
                self.assertNotIn("?", result["reply_text"], text)

    def test_remember_key_value(self):
        result = classify.classify_rules("remember my city is Hyderabad")
        self.assertEqual(result["reason"], "remember")
        self.assertTrue(result["hard_chat"])
        self.assertEqual(result["remember"]["key"], "city")
        self.assertEqual(result["remember"]["value"], "Hyderabad")
        self.assertEqual(result["remember"]["fact_text"], "My city is Hyderabad.")
        self.assertIn("Hyderabad", result["reply_text"])

    def test_remember_free_form_and_name(self):
        result = classify.classify_rules("remember that i prefer remote roles")
        self.assertEqual(result["reason"], "remember")
        self.assertIsNone(result["remember"]["key"])
        self.assertEqual(result["remember"]["fact_text"], "I prefer remote roles.")
        result = classify.classify_rules("remember i'm Akash")
        self.assertEqual(result["remember"]["key"], "name")
        self.assertEqual(result["remember"]["value"], "Akash")

    def test_remember_to_is_not_a_fact(self):
        # "remember to check my mail" is a reminder phrasing,
        # not a fact to store.
        result = classify.classify_rules("remember to check my mail")
        self.assertNotEqual(result["reason"], "remember")
        self.assertNotIn("remember", result)

    def test_greeting_and_task_unchanged(self):
        self.assertEqual(classify.classify_rules("hi")["reason"], "greeting")
        self.assertEqual(
            classify.classify_rules("Find me remote SDE intern roles")["kind"],
            "task_created",
        )
        self.assertEqual(
            classify.classify_rules("internships")["reason"], "ambiguous"
        )


# ----------------------------------------------------------------
# respond() in-process: facts, fallback, labelling
# ----------------------------------------------------------------

def _planner_call(payload: dict, model: str = "ollama:qwen2.5:7b"):
    def _call(prompt: str, **kwargs):
        _call.prompts.append(prompt)
        return json.dumps(payload), model, 1.0

    _call.prompts = []
    return _call


def _raising_call(exc: Exception):
    def _call(prompt: str, **kwargs):
        raise exc

    return _call


def _facts(conn, topic=None):
    if topic is None:
        rows = conn.execute("SELECT * FROM facts").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM facts WHERE topic = ?", (topic,)
        ).fetchall()
    return [dict(r) for r in rows]


class TestChatRespondBrain(unittest.TestCase):
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

    def test_name_capture_stores_profile_fact_and_confirms(self):
        result = chat_api.respond(
            self.conn, "i'm Akash",
            call_fn=_raising_call(AssertionError("planner called")),
        )
        self.assertEqual(result["kind"], "chat")
        self.assertEqual(result["source"], "rules")
        self.assertIsNone(result["provider"])
        self.assertIn("Nice to meet you, Akash", result["reply_text"])
        facts = _facts(self.conn, "profile:name")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["text"], "The user's name is Akash.")
        self.assertEqual(facts[0]["scope"], keyword.PROFILE_SCOPE)
        self.assertIsNone(result["task_id"])

    def test_repeated_name_is_not_stored_twice(self):
        chat_api.respond(self.conn, "i'm Akash",
                         call_fn=_raising_call(PlannerError("no planner")))
        result = chat_api.respond(self.conn, "i'm Akash",
                                  call_fn=_raising_call(PlannerError("no planner")))
        self.assertIn("already told me", result["reply_text"])
        self.assertEqual(len(_facts(self.conn, "profile:name")), 1)

    def test_mission_message_returns_draft_not_task(self):
        # Live screenshot verification 2026-11-23: "watch my
        # inbox for interview mails" became a dead one-off task
        # in `created`. A standing-mission ask must draft a
        # mission instead — the reply carries the draft for the
        # UI's draft card and no task is created.
        result = chat_api.respond(
            self.conn, "watch my inbox for interview mails",
            call_fn=_raising_call(PlannerError("no planner")),
        )
        self.assertEqual(result["kind"], "chat")
        self.assertIsNone(result["task_id"])
        self.assertIn("draft", result)
        self.assertEqual(result["draft"]["draft"]["type"], "email_monitor")
        self.assertIn("Inbox watch", result["draft"]["draft"]["name"])
        self.assertIn("inbox", result["reply_text"])
        tasks = self.conn.execute("SELECT * FROM tasks").fetchall()
        self.assertEqual(len(tasks), 0)

    def test_keep_applying_returns_job_hunt_draft(self):
        result = chat_api.respond(
            self.conn, "keep applying for remote SDE intern roles",
            call_fn=_raising_call(PlannerError("no planner")),
        )
        self.assertEqual(result["kind"], "chat")
        self.assertIn("draft", result)
        self.assertEqual(result["draft"]["draft"]["type"], "job_hunt")

    def test_one_off_task_has_no_draft(self):
        result = chat_api.respond(
            self.conn, "open google",
            call_fn=_raising_call(PlannerError("no planner")),
        )
        self.assertEqual(result["kind"], "task_created")
        self.assertIsNotNone(result["task_id"])
        self.assertNotIn("draft", result)

    def test_name_correction_overwrites(self):
        chat_api.respond(self.conn, "i'm Akash",
                         call_fn=_raising_call(PlannerError("no planner")))
        result = chat_api.respond(self.conn, "no, my name is Yash",
                                  call_fn=_raising_call(PlannerError("no planner")))
        self.assertIn("Yash", result["reply_text"])
        facts = _facts(self.conn, "profile:name")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["text"], "The user's name is Yash.")
        self.assertEqual(chat_api._stored_profile_name(self.conn), "Yash")

    def test_plain_restatement_with_a_new_name_overwrites(self):
        chat_api.respond(self.conn, "i'm Akash",
                         call_fn=_raising_call(PlannerError("no planner")))
        result = chat_api.respond(self.conn, "i'm Yash",
                                  call_fn=_raising_call(PlannerError("no planner")))
        self.assertIn("call you Yash", result["reply_text"])
        self.assertEqual(len(_facts(self.conn, "profile:name")), 1)

    def test_remember_stores_fact_with_global_scope(self):
        result = chat_api.respond(
            self.conn, "remember my city is Hyderabad",
            call_fn=_raising_call(AssertionError("planner called")),
        )
        self.assertEqual(result["kind"], "chat")
        self.assertIn("Hyderabad", result["reply_text"])
        facts = _facts(self.conn, "remembered:city")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["text"], "My city is Hyderabad.")
        self.assertEqual(facts[0]["scope"], keyword.GLOBAL_SCOPE)
        # The fact is recallable by the chat's own recall path.
        recalled = keyword.recall(self.conn, "which city am i in")
        self.assertTrue(any("Hyderabad" in f["text"] for f in recalled))

    def test_remember_correction_overwrites_old_value(self):
        chat_api.respond(self.conn, "remember my city is Hyderabad",
                         call_fn=_raising_call(PlannerError("no planner")))
        result = chat_api.respond(self.conn, "remember my city is Pune",
                                  call_fn=_raising_call(PlannerError("no planner")))
        self.assertIn("updated", result["reply_text"])
        self.assertIn("Pune", result["reply_text"])
        facts = _facts(self.conn, "remembered:city")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["text"], "My city is Pune.")

    def test_remember_same_value_twice_is_a_noop(self):
        chat_api.respond(self.conn, "remember my city is Hyderabad",
                         call_fn=_raising_call(PlannerError("no planner")))
        result = chat_api.respond(self.conn, "remember my city is Hyderabad",
                                  call_fn=_raising_call(PlannerError("no planner")))
        self.assertIn("already told me", result["reply_text"])
        self.assertEqual(len(_facts(self.conn, "remembered:city")), 1)

    def test_remember_name_goes_to_the_profile_store(self):
        chat_api.respond(self.conn, "remember my name is Akash",
                         call_fn=_raising_call(PlannerError("no planner")))
        self.assertEqual(chat_api._stored_profile_name(self.conn), "Akash")
        self.assertEqual(_facts(self.conn, "remembered:name"), [])

    def test_remember_refuses_credentials_and_pii(self):
        cases = (
            "remember my password is hunter2",
            "remember my otp is 123456",
            "remember my email is someone@example.com",
            "remember my phone number is 9876543210",
            "remember my api key is sk-fake-123",
        )
        for text in cases:
            with self.subTest(text=text):
                result = chat_api.respond(
                    self.conn, text,
                    call_fn=_raising_call(PlannerError("no planner")),
                )
                self.assertIn("won't store", result["reply_text"], text)
        self.assertEqual(_facts(self.conn), [])

    def test_remembered_fact_expires_after_90_days(self):
        # The TTL is the existing expiry: a global-scope remember
        # fact older than 90 days is deleted by the maintenance
        # respond() runs; the profile name never expires.
        import time as _time

        chat_api.respond(self.conn, "remember my city is Hyderabad",
                         call_fn=_raising_call(PlannerError("no planner")))
        chat_api.respond(self.conn, "i'm Akash",
                         call_fn=_raising_call(PlannerError("no planner")))
        old = int(_time.time()) - 91 * 86400
        self.conn.execute(
            "UPDATE facts SET created_at = ? WHERE topic = ?",
            (old, "remembered:city"),
        )
        self.conn.execute(
            "UPDATE facts SET created_at = ? WHERE topic = ?",
            (old, "profile:name"),
        )
        self.conn.commit()
        chat_api.respond(self.conn, "hi",
                         call_fn=_raising_call(PlannerError("no planner")))
        self.assertEqual(_facts(self.conn, "remembered:city"), [])
        self.assertEqual(len(_facts(self.conn, "profile:name")), 1)

    def test_model_answers_with_history_and_facts(self):
        chat_api.respond(self.conn, "i'm Akash",
                         call_fn=_raising_call(PlannerError("no planner")))
        chat_api.respond(self.conn, "internships",
                         call_fn=_raising_call(PlannerError("no planner")))
        call = _planner_call({
            "kind": "chat", "reply_text": "You're Akash, looking for intern roles.",
            "constraints": None,
        })
        result = chat_api.respond(self.conn, "what is my name", call_fn=call)
        self.assertEqual(result["source"], "planner")
        self.assertEqual(result["provider"], "ollama")
        prompt = call.prompts[0]
        self.assertIn("internships", prompt)
        self.assertIn("The user's name is Akash.", prompt)

    def test_model_answer_is_pii_redacted_before_the_call(self):
        # A raw email in the message must not reach the model:
        # the prompt is redacted first (the loop.plan_task pattern).
        call = _planner_call({"kind": "chat", "reply_text": "Noted.",
                              "constraints": None})
        chat_api.respond(
            self.conn, "what do you know about someone@example.com",
            call_fn=call)
        self.assertNotIn("someone@example.com", call.prompts[0])
        self.assertIn("[EMAIL_1]", call.prompts[0])

    def test_clarifier_is_never_asked_twice_in_a_row(self):
        first = chat_api.respond(
            self.conn, "internships",
            call_fn=_raising_call(PlannerError("no planner")),
        )
        self.assertIn("?", first["reply_text"])
        second = chat_api.respond(
            self.conn, "internships",
            call_fn=_raising_call(PlannerError("no planner")),
        )
        self.assertNotEqual(second["reply_text"], first["reply_text"])
        self.assertNotIn("?", second["reply_text"])
        self.assertIn("same question", second["reply_text"])

    def test_statement_without_a_model_is_honest_not_a_question(self):
        result = chat_api.respond(
            self.conn, "i am a student",
            call_fn=_raising_call(PlannerError("no planner")),
        )
        self.assertEqual(result["kind"], "chat")
        self.assertEqual(result["reply_text"], classify.NO_MODEL_REPLY)
        self.assertNotIn("?", result["reply_text"])
        self.assertIsNone(result["task_id"])

    def test_provider_label_rules_is_null_planner_is_named(self):
        rules_result = chat_api.respond(
            self.conn, "Find me remote SDE intern roles in Hyderabad",
            call_fn=_raising_call(PlannerError("no planner")),
        )
        self.assertEqual(rules_result["source"], "rules")
        self.assertIsNone(rules_result["provider"])
        call = _planner_call({
            "kind": "task_created", "reply_text": "On it.",
            "constraints": None,
        }, model="groq:gpt-oss-120b")
        # PR #7: "watch the price of headphones" now drafts a
        # price-watch mission instead of a task, so the planner
        # path is exercised with a plain one-off instruction.
        planner_result = chat_api.respond(
            self.conn, "find the price of headphones", call_fn=call)
        self.assertEqual(planner_result["source"], "planner")
        self.assertEqual(planner_result["provider"], "groq")

    def test_history_rows_carry_provider(self):
        call = _planner_call({
            "kind": "chat", "reply_text": "Hello from the model.",
            "constraints": None,
        }, model="antseed:google-gemma-4-31b-it")
        chat_api.respond(self.conn, "what did you find so far", call_fn=call)
        rows = self.conn.execute(
            "SELECT role, source, provider FROM chat_messages"
            " ORDER BY rowid"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["source"])
        self.assertIsNone(rows[0]["provider"])
        self.assertEqual(rows[1]["source"], "planner")
        self.assertEqual(rows[1]["provider"], "antseed")


# ----------------------------------------------------------------
# Provider chain: local-first order, env override, status
# ----------------------------------------------------------------

_DEAD = "http://127.0.0.1:1/v1"


def _clean_provider_env():
    """Env with all provider vars pinned to dead ports / fakes,
    so chain tests never touch the real network or real keys."""
    return {
        "ANTSEED_API_KEY": FAKE_KEY,
        "ANTSEED_BASE_URL": _DEAD,
        "ANTSEED_MODEL": "google-gemma-4-31b-it",
        "GROQ_API_KEY": FAKE_KEY,
        "GROQ_BASE_URL": _DEAD,
        "GROQ_MODEL": "gpt-oss-120b",
        "OLLAMA_BASE_URL": _DEAD,
        "OLLAMA_MODEL": "qwen2.5:7b",
    }


class TestProviderChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_data_dir = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()

    def tearDown(self):
        config.DATA_DIR = self._orig_data_dir
        self.tmp.cleanup()

    def test_default_chain_is_local_first(self):
        env = _clean_provider_env()
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("ECLIPSE_PROVIDER_ORDER", None)
            os.environ.pop("ORCA_ROUTER_BASE_URL", None)
            os.environ.pop("ORCA_ROUTER_API_KEY", None)
            os.environ.pop("ORCA_ROUTER_MODEL", None)
            names = [e.name for e in router.default_chain()]
        self.assertEqual(names, ["ollama", "antseed", "groq"])

    def test_chain_order_overridable_by_env(self):
        env = _clean_provider_env()
        env["ECLIPSE_PROVIDER_ORDER"] = "groq,ollama,antseed"
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("ORCA_ROUTER_BASE_URL", None)
            names = [e.name for e in router.default_chain()]
        self.assertEqual(names, ["groq", "ollama", "antseed"])

    def test_chain_order_unknown_names_ignored_unlisted_kept(self):
        env = _clean_provider_env()
        env["ECLIPSE_PROVIDER_ORDER"] = "bogus,groq"
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("ORCA_ROUTER_BASE_URL", None)
            names = [e.name for e in router.default_chain()]
        self.assertEqual(names, ["groq", "ollama", "antseed"])

    def test_ollama_model_default_is_qwen(self):
        env = _clean_provider_env()
        env.pop("OLLAMA_MODEL")
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("OLLAMA_MODEL", None)
            chain = router.default_chain()
        ollama = next(e for e in chain if e.name == "ollama")
        self.assertEqual(ollama.model, "qwen2.5:7b")

    def test_antseed_default_base_url_is_akash_gateway(self):
        self.assertEqual(
            router.ANTSEED_DEFAULT_BASE_URL,
            "https://akash-antseed.duckdns.org/v1",
        )
        env = _clean_provider_env()
        env.pop("ANTSEED_BASE_URL")
        env["ANTSEED_USE_SURROGATE"] = "0"
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("ANTSEED_BASE_URL", None)
            chain = router.default_chain()
        antseed = next(e for e in chain if e.name == "antseed")
        self.assertEqual(
            antseed.base_url, "https://akash-antseed.duckdns.org/v1"
        )

    def test_call_planner_tries_ollama_first(self):
        seen: list[str] = []

        def transport(url, headers, payload, timeout):
            seen.append(url)
            if "11434" in url:
                raise ConnectionError("ollama not running")
            return {
                "choices": [{"message": {"content": '{"action": "wait",'
                                                    ' "seconds": 1,'
                                                    ' "reason": "x"}'}}]
            }

        env = _clean_provider_env()
        env["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434/v1"
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("ECLIPSE_PROVIDER_ORDER", None)
            os.environ.pop("ORCA_ROUTER_BASE_URL", None)
            text, model, _ms = router.call_planner(
                "do something", transport=transport
            )
        self.assertIn("11434", seen[0])
        self.assertTrue(model.startswith("antseed:"))
        self.assertIn("wait", text)

    def test_probe_dead_port_unreachable_live_port_reachable(self):
        dead = router.Endpoint(name="ollama", base_url=_DEAD,
                               model="qwen2.5:7b")
        self.assertFalse(router.probe_reachable(dead))

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                # 401: the service answers but refuses auth —
                # still reachable (a different problem from dead).
                self.send_response(401)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            live = router.Endpoint(
                name="groq", base_url=f"http://127.0.0.1:{port}/v1",
                model="gpt-oss-120b", api_key=FAKE_KEY,
            )
            self.assertTrue(router.probe_reachable(live))
        finally:
            server.shutdown()
            server.server_close()

    def test_brain_status_names_only_no_keys(self):
        env = _clean_provider_env()
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("ECLIPSE_PROVIDER_ORDER", None)
            os.environ.pop("ORCA_ROUTER_BASE_URL", None)
            status = router.brain_status(probe=True)
        self.assertEqual(status["chain"], ["ollama", "antseed", "groq"])
        self.assertEqual(len(status["providers"]), 3)
        for provider in status["providers"]:
            self.assertFalse(provider["reachable"])  # dead ports
            self.assertIn(provider["auth"], ("none", "key", "surrogate"))
        # No key material anywhere in the output.
        self.assertNotIn(FAKE_KEY, json.dumps(status))


# ----------------------------------------------------------------
# .env loading
# ----------------------------------------------------------------

class TestDotenv(unittest.TestCase):
    def test_dotenv_vars_reach_the_router(self):
        from dotenv import load_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "GROQ_API_KEY=fake-dotenv-key\n"
                "GROQ_BASE_URL=http://127.0.0.1:1/v1\n"
                "ANTSEED_USE_SURROGATE=0\n"
            )
            saved = {k: os.environ.pop(k, None) for k in
                     ("GROQ_API_KEY", "GROQ_BASE_URL", "ANTSEED_USE_SURROGATE")}
            try:
                load_dotenv(env_file)
                chain = router.default_chain()
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            groq = next(e for e in chain if e.name == "groq")
            self.assertEqual(groq.api_key, "fake-dotenv-key")
            self.assertEqual(groq.base_url, "http://127.0.0.1:1/v1")

    def test_importing_server_main_loads_working_directory_dotenv(self):
        # server.main loads a .env from the working directory at
        # import time (plus the repo-root .env). Run in a
        # subprocess with a temp cwd so no real .env is touched.
        # A repo-root .env defining GROQ_API_KEY would win over
        # the temp one (it loads first), so skip honestly then.
        # The check runs as a real .py script file, NOT python -c:
        # python-dotenv treats -c as interactive and searches the
        # cwd even for a bare load_dotenv(), which would hide the
        # uvicorn bug (a bare load_dotenv() misses a working-dir
        # .env under a real __main__ — verified 2026-11-23).
        repo_env = REPO_ROOT / ".env"
        if repo_env.exists() and "GROQ_API_KEY" in repo_env.read_text():
            self.skipTest("repo-root .env defines GROQ_API_KEY")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text(
                "GROQ_API_KEY=fake-subprocess-key\n"
            )
            script = Path(tmp, "check_env.py")
            script.write_text(
                "import os, server.main\n"
                "print(os.environ.get('GROQ_API_KEY', 'MISSING'))\n"
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO_ROOT)
            env["ECLIPSE_DATA_DIR"] = tmp
            env.pop("GROQ_API_KEY", None)
            proc = subprocess.run(
                [sys.executable, str(script)],
                cwd=tmp, env=env, capture_output=True, text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertEqual(proc.stdout.strip(), "fake-subprocess-key")

    def test_real_env_wins_over_dotenv(self):
        from dotenv import load_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("GROQ_API_KEY=fake-dotenv-key\n")
            saved = os.environ.get("GROQ_API_KEY")
            os.environ["GROQ_API_KEY"] = "fake-real-env-key"
            try:
                load_dotenv(env_file)
                self.assertEqual(os.environ["GROQ_API_KEY"],
                                 "fake-real-env-key")
            finally:
                if saved is None:
                    os.environ.pop("GROQ_API_KEY", None)
                else:
                    os.environ["GROQ_API_KEY"] = saved


# ----------------------------------------------------------------
# GET /brain
# ----------------------------------------------------------------

class TestBrainEndpoint(unittest.TestCase):
    def test_brain_handler_reports_chain_and_reachability(self):
        env = _clean_provider_env()
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("ECLIPSE_PROVIDER_ORDER", None)
            os.environ.pop("ORCA_ROUTER_BASE_URL", None)
            status = system_api.brain()
        self.assertEqual(status["chain"], ["ollama", "antseed", "groq"])
        self.assertEqual(
            [p["name"] for p in status["providers"]],
            ["ollama", "antseed", "groq"],
        )
        self.assertTrue(all(p["reachable"] is False
                            for p in status["providers"]))
        self.assertNotIn(FAKE_KEY, json.dumps(status))

    def test_brain_route_registered_on_the_app(self):
        import server.main as main

        # FastAPI 0.165 keeps included routers lazy
        # (_IncludedRouter placeholders with no .path), so the
        # route is checked on the system router itself — plus
        # that main.app includes that very router.
        paths = {getattr(r, "path", None)
                 for r in system_api.router.routes}
        self.assertIn("/brain", paths)
        included = [getattr(r, "original_router", None)
                    for r in main.app.routes]
        self.assertIn(system_api.router, included)


if __name__ == "__main__":
    unittest.main()
