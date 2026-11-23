"""Mission intake tests (PR #5, W2): planner path + rule fallback.

Run: .venv/bin/python -m unittest tests.test_missions_intake -v
No DB, no network: the planner is an injected call_fn.
"""
from __future__ import annotations

import json
import unittest

from server.missions import intake as intake_mod
from server.missions import types as types_mod
from server.planner.router import PlannerError


def _planner_call(payload: dict):
    """A fake call_fn returning one JSON draft."""

    def _call(prompt: str, **kwargs):
        _call.prompts.append(prompt)
        return json.dumps(payload), "fake:model", 1.0

    _call.prompts = []
    return _call


def _raising_call(exc: Exception):
    def _call(prompt: str, **kwargs):
        raise exc

    return _call


class TestParseIntakePlannerPath(unittest.TestCase):
    def test_planner_valid_json_returns_planner_draft(self):
        call = _planner_call({
            "type": "job_hunt",
            "name": "SDE intern hunt",
            "config": {"locations": ["Hyderabad"], "stipend_min": 10000},
            "bounds": {"max_applies_per_day": 3},
            "missing_fields": [],
        })
        result = intake_mod.parse_intake(
            "watch for SDE intern roles in Hyderabad, min 10k stipend",
            call_fn=call,
        )
        self.assertEqual(result["source"], "planner")
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["draft"]["type"], "job_hunt")
        self.assertEqual(result["draft"]["name"], "SDE intern hunt")
        self.assertEqual(
            result["draft"]["config"]["locations"], ["Hyderabad"])
        self.assertEqual(result["draft"]["config"]["stipend_min"], 10000)
        self.assertEqual(result["draft"]["bounds"]["max_applies_per_day"], 3)
        self.assertEqual(result["missing_fields"], [])

    def test_planner_missing_required_field_becomes_missing_not_fallback(self):
        # The planner cannot know a CSS selector — the draft
        # comes back with the selector missing, not discarded.
        call = _planner_call({
            "type": "price_watch",
            "name": "Price watch",
            "config": {"url": "https://example.com/product/1",
                       "target_price": 4999},
            "bounds": {},
            "missing_fields": ["selector"],
        })
        result = intake_mod.parse_intake(
            "tell me when the headphones drop below 4999",
            call_fn=call,
        )
        self.assertEqual(result["source"], "planner")
        self.assertEqual(result["confidence"], "low")
        self.assertIn("selector", result["missing_fields"])
        self.assertIsNone(result["draft"]["config"]["selector"])
        self.assertEqual(result["draft"]["config"]["target_price"], 4999)

    def test_planner_invalid_json_falls_back_to_rules(self):
        def _call(prompt: str, **kwargs):
            return "not json at all", "fake:model", 1.0

        result = intake_mod.parse_intake(
            "watch for SDE intern roles in Hyderabad", call_fn=_call
        )
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["draft"]["type"], "job_hunt")
        self.assertTrue(any("planner" in w.lower()
                            for w in result["warnings"]))

    def test_planner_error_falls_back_to_rules_never_raises(self):
        result = intake_mod.parse_intake(
            "watch for SDE intern roles in Hyderabad, min 10k stipend",
            call_fn=_raising_call(PlannerError("every endpoint failed")),
        )
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["draft"]["type"], "job_hunt")
        self.assertEqual(result["draft"]["config"]["stipend_min"], 10000)

    def test_planner_unknown_type_falls_back_to_rules(self):
        call = _planner_call({
            "type": "moon_base",
            "name": "Moon",
            "config": {},
            "bounds": {},
        })
        result = intake_mod.parse_intake(
            "watch for SDE intern roles", call_fn=call
        )
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["draft"]["type"], "job_hunt")

    def test_planner_unknown_config_key_falls_back_to_rules(self):
        call = _planner_call({
            "type": "job_hunt",
            "name": "Hunt",
            "config": {"teleport": True},
            "bounds": {},
        })
        result = intake_mod.parse_intake("watch for SDE roles", call_fn=call)
        self.assertEqual(result["source"], "rules")

    def test_pii_is_redacted_before_the_planner_call(self):
        seen = {}

        def _call(prompt: str, **kwargs):
            seen["prompt"] = prompt
            return json.dumps({
                "type": "cold_mail", "name": "Cold mail",
                "config": {"recipients": []}, "bounds": {},
                "missing_fields": ["recipients"],
            }), "fake:model", 1.0

        intake_mod.parse_intake(
            "mail the recruiter at recruiter@example.com or call"
            " 98765 43210 about the SDE role",
            call_fn=_call,
        )
        prompt = seen["prompt"]
        self.assertNotIn("recruiter@example.com", prompt)
        self.assertNotIn("98765 43210", prompt)
        self.assertNotIn("9876543210", prompt)
        self.assertIn("[EMAIL_1]", prompt)
        self.assertIn("[PHONE_1]", prompt)


class TestParseIntakeRulesPath(unittest.TestCase):
    def _rules(self, message: str) -> dict:
        return intake_mod.parse_intake(
            message, call_fn=_raising_call(PlannerError("down")))

    def test_rules_job_hunt_extracts_location_stipend_cap(self):
        result = self._rules(
            "watch for SDE intern roles in Hyderabad, min stipend"
            " 15000, apply to at most 3 a day"
        )
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["draft"]["type"], "job_hunt")
        self.assertEqual(result["draft"]["config"]["locations"], ["Hyderabad"])
        self.assertEqual(result["draft"]["config"]["stipend_min"], 15000)
        self.assertEqual(result["draft"]["bounds"]["max_applies_per_day"], 3)
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["confidence"], "high")

    def test_rules_job_hunt_k_stipend_means_thousands(self):
        result = self._rules("SDE intern roles, stipend 15k")
        self.assertEqual(result["draft"]["config"]["stipend_min"], 15000)

    def test_rules_job_hunt_stipend_before_comma_not_cap(self):
        # PR #6 verification: "min Rs 10,000 stipend, apply to
        # at most 3 a day" parsed the stipend as 3 (the regex
        # skipped the comma and took the cap number).
        result = self._rules(
            "watch for SDE intern roles in Hyderabad, min Rs 10,000"
            " stipend, apply to at most 3 a day"
        )
        self.assertEqual(result["draft"]["config"]["stipend_min"], 10000)
        self.assertEqual(result["draft"]["bounds"]["max_applies_per_day"], 3)

    def test_rules_job_hunt_stipend_without_number_is_missing(self):
        # A bare "stipend" followed by a cap must not capture
        # the cap as the stipend — it stays a missing field.
        result = self._rules(
            "watch for SDE intern roles in Hyderabad, stipend,"
            " apply to at most 3 a day"
        )
        self.assertIsNone(result["draft"]["config"]["stipend_min"])
        self.assertIn("stipend_min", result["missing_fields"])
        self.assertEqual(result["draft"]["bounds"]["max_applies_per_day"], 3)

    def test_rules_price_watch_extracts_url_and_target(self):
        result = self._rules(
            "watch the price of https://example.com/headphones and"
            " tell me when it drops below ₹4,999"
        )
        self.assertEqual(result["draft"]["type"], "price_watch")
        self.assertEqual(result["draft"]["config"]["url"],
                         "https://example.com/headphones")
        self.assertEqual(result["draft"]["config"]["target_price"], 4999)
        # A selector can never be extracted from prose.
        self.assertIn("selector", result["missing_fields"])
        self.assertNotIn("url", result["missing_fields"])

    def test_rules_price_watch_without_url_lists_it_missing(self):
        result = self._rules("watch the price of those headphones")
        self.assertEqual(result["draft"]["type"], "price_watch")
        self.assertIn("url", result["missing_fields"])
        self.assertIn("selector", result["missing_fields"])

    def test_rules_cold_mail_extracts_recipients(self):
        result = self._rules(
            "cold mail recruiter@example.com and hiring@example.com"
            " about SDE roles, attach my resume"
        )
        self.assertEqual(result["draft"]["type"], "cold_mail")
        self.assertEqual(
            sorted(result["draft"]["config"]["recipients"]),
            ["hiring@example.com", "recruiter@example.com"],
        )
        self.assertTrue(result["draft"]["config"]["attach_resume"])
        self.assertIn("roles", result["missing_fields"])

    def test_rules_sites_become_allowed_sites(self):
        result = self._rules(
            "watch for SDE intern roles on internshala.com"
        )
        self.assertEqual(result["draft"]["bounds"]["allowed_sites"],
                         ["internshala.com"])

    def test_rules_vague_message_guesses_job_hunt_with_warning(self):
        result = self._rules("do the thing")
        self.assertEqual(result["draft"]["type"], "job_hunt")
        self.assertEqual(result["confidence"], "low")
        self.assertTrue(any("guessed" in w for w in result["warnings"]))

    def test_empty_message_rejected(self):
        with self.assertRaises(ValueError):
            intake_mod.parse_intake("   ")

    def test_oversized_message_rejected(self):
        with self.assertRaises(ValueError):
            intake_mod.parse_intake("x" * 2001)


class TestTypesValidation(unittest.TestCase):
    """The registry validation intake depends on (W1's core
    tests cover it in depth; these pin the W2-facing rules)."""

    def test_auto_approve_true_is_accepted_q1_override(self):
        # Q1 override: standing approval IS part of PR #5 — the
        # design doc's 422 gate is NOT implemented.
        bounds = types_mod.validate_bounds(
            "job_hunt", {"auto_approve": True,
                         "require_approval_for": []})
        self.assertTrue(bounds["auto_approve"])
        self.assertEqual(bounds["require_approval_for"], [])

    def test_unknown_type_rejected(self):
        with self.assertRaises(ValueError):
            types_mod.validate_config("moon_base", {})

    def test_unknown_config_key_rejected(self):
        with self.assertRaises(ValueError):
            types_mod.validate_config("job_hunt", {"teleport": True})

    def test_unknown_bounds_key_rejected(self):
        with self.assertRaises(ValueError):
            types_mod.validate_bounds("job_hunt", {"teleport": True})

    def test_bad_bounds_rejected(self):
        with self.assertRaises(ValueError):
            types_mod.validate_bounds(
                "job_hunt", {"max_applies_per_day": -1})
        with self.assertRaises(ValueError):
            types_mod.validate_bounds(
                "job_hunt", {"require_approval_for": ["teleport"]})
        with self.assertRaises(ValueError):
            types_mod.validate_bounds(
                "job_hunt", {"quiet_hours_ist": {"start": "25:00",
                                                 "end": "07:00"}})

    def test_role_needs_id_and_title(self):
        with self.assertRaises(ValueError):
            types_mod.validate_config(
                "job_hunt", {"roles": [{"title": "No id"}]})
        config = types_mod.validate_config(
            "job_hunt",
            {"roles": [{"id": "r1", "title": "SDE Intern",
                        "stipend": 15000}]})
        self.assertEqual(config["roles"][0]["id"], "r1")

    def test_price_watch_requires_url_and_selector(self):
        with self.assertRaises(ValueError):
            types_mod.validate_config(
                "price_watch", {"url": "https://example.com/p"})
        with self.assertRaises(ValueError):
            types_mod.validate_config(
                "price_watch", {"url": "not a url", "selector": ".p"})

    def test_cold_mail_recipients_must_be_emails(self):
        with self.assertRaises(ValueError):
            types_mod.validate_config(
                "cold_mail", {"recipients": ["not-an-email"]})

    def test_type_defaults(self):
        self.assertEqual(
            types_mod.default_bounds("job_hunt")["max_applies_per_day"], 3)
        self.assertEqual(
            types_mod.default_bounds("cold_mail")["max_sends_per_day"], 5)
        self.assertEqual(
            types_mod.default_bounds("price_watch")["max_applies_per_day"], 0)


class TestIsMissionRequest(unittest.TestCase):
    """PR #7: the chat router's standing-mission detector —
    continuous language AND a mission-type signal."""

    def test_mission_requests(self):
        for text in (
            "watch my inbox for interview mails",
            "keep applying for remote SDE intern roles",
            "continuously apply across platforms for this role",
            "tell me when the price drops below 4999",
            "monitor the price of headphones",
            "send me cold mails every day",
            "alert me when an offer mail arrives",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    intake_mod.is_mission_request(text), text)

    def test_one_off_tasks_are_not_missions(self):
        for text in (
            "find me a job",
            "open google",
            "watch this video",
            "keep this tab open",
            "hi",
            "send a mail to ravi@example.com",
            "what is the price of headphones",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    intake_mod.is_mission_request(text), text)

    def test_monitor_the_price_picks_price_watch(self):
        # "monitor" is a generic continuous verb, not an inbox
        # signal — it only counts for email_monitor with an
        # inbox/mail word present.
        mission_type, _ = intake_mod._pick_type(
            "monitor the price of headphones")
        self.assertEqual(mission_type, "price_watch")
        mission_type, _ = intake_mod._pick_type("monitor my inbox")
        self.assertEqual(mission_type, "email_monitor")


if __name__ == "__main__":
    unittest.main()
