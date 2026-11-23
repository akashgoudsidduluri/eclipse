"""Mission intake (PR #5, §3.3; extended PR #7): one chat
message -> a mission draft, for the user to confirm before
anything is created. The intake endpoint never creates a
mission.

Two paths, always labelled:
- planner: the message (PII-redacted FIRST — see below) goes to
  the planner with the intake prompt; the JSON draft is
  validated against the type registry. A valid draft returns
  source "planner".
- rules: deterministic keyword + regex extraction, local only.
  Used when the planner fails (PlannerError), returns invalid
  JSON, or returns a draft that fails validation. Source
  "rules", and the warnings say why.

PR #7: chat is the ONE interface for setting missions up, so
the intake handles ALL mission types from a plain chat message
— job_hunt, cold_mail, price_watch, and email_monitor ("watch
my inbox for interview mails"). A resume uploaded in chat
(upload_ids) enriches the draft locally: locations / stipend
from the resume fill a job hunt, attach_resume turns on for a
cold mail, and the resume's profile facts come back as
PROPOSED facts for the user to confirm in chat before anything
is stored. The planner path never sees resume text — only the
redacted message; enrichment is local-only, after the draft.

PII: the planner path redacts the message with the server-side
second PII scan (server/planner/scan.py) BEFORE the prompt is
built — the prompt passed to call_fn contains no raw email or
phone (asserted in tests). The rules path runs locally on the
original message (it never leaves the device), so it can
extract a recipient address the user typed. Never silently fall
back to a mock planner: PlannerError means rules, labelled.

parse_intake returns:
  {"draft": {"type", "name", "config", "bounds"},
   "source": "planner" | "rules",
   "confidence": "high" | "low",
   "missing_fields": [...], "warnings": [...],
   "clarifying_question": <str | None>,
   "draft_card": <plain-language summary for chat>,
   "proposed_facts": [...], "upload_ids": [...]}
`missing_fields` names config/bounds fields the message did not
say enough to fill — the UI prompts for them. The draft's
config may hold None for a missing required field (validated
with validate_config_draft); create-time validation still
requires it. `clarifying_question` is at most ONE question, and
only when the intake is genuinely blocked on something the
user can answer in chat (a product URL, a recipient address, a
target price) — a CSS selector is never asked for in chat, it
stays a missing field for the page picker.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from ..jobs import profile as profile_mod
from ..planner import prompts, router, scan
from . import types as _types
from . import uploads as uploads_mod

logger = logging.getLogger("eclipse.missions")

MAX_MESSAGE_LEN = 2000

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s<>\")\]]+")
_MONEY_RE = re.compile(
    r"(?:₹|Rs\.?|INR)\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?", re.IGNORECASE
)
_TARGET_PRICE_RE = re.compile(
    r"(?:below|under|drops?\s+to|less\s+than|at\s+most)\s+₹?\s*"
    r"(\d[\d,]*(?:\.\d+)?)\s*(k)?",
    re.IGNORECASE,
)
_STIPEND_RE = re.compile(
    # The gap excludes commas: "min Rs 10,000 stipend, apply
    # to at most 3 a day" must NOT skip the comma and capture
    # the 3 as the stipend (PR #6 verification found ₹3).
    r"stipend[^0-9,]{0,20}₹?\s*(\d[\d,]*)\s*(k)?", re.IGNORECASE
)
# The number can come first: "min 10k stipend", "15000 stipend".
_STIPEND_AFTER_RE = re.compile(
    r"(\d[\d,]*)\s*(k)?\s*stipend", re.IGNORECASE
)
_CAP_RE = re.compile(
    r"(?:at most|maximum|max|up to|only)\s+(\d+)\s+"
    r"(applies|applications|apply|mails|emails|sends|actions?)",
    re.IGNORECASE,
)
_MIN_SCORE_RE = re.compile(
    r"(?:score|min(?:imum)?\s+score)[^0-9]{0,10}(\d{1,3})", re.IGNORECASE
)
_SITE_RE = re.compile(
    r"(?:on|from|at)\s+([a-z0-9][a-z0-9.-]*\.(?:com|in|org|co|ai|net))\b",
    re.IGNORECASE,
)

_KNOWN_LOCATIONS = (
    "Remote", "Hyderabad", "Bangalore", "Bengaluru", "Chennai",
    "Pune", "Mumbai", "Delhi", "Noida", "Gurugram", "Kolkata",
    "Ahmedabad", "Jaipur", "Indore", "Coimbatore",
)

# Keyword sets for the rule path. A word match scores 1; the
# type with the highest score wins (ties break toward the most
# specific signal — see _pick_type; a message with no signal at
# all defaults to job_hunt, and the user corrects the draft
# anyway).
# email_monitor deliberately has NO bare "mail"/"email" keyword
# — those belong to cold_mail; inbox monitoring is signalled by
# "inbox" and by mail phrases ("interview mail", "offer mail").
_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "price_watch": (
        "price", "prices", "watch", "drop", "deal", "discount",
        "cost", "cheaper", "sale", "amazon", "flipkart", "below",
    ),
    "cold_mail": (
        "mail", "mails", "email", "emails", "cold", "recruiter",
        "reach out", "write to", "contact", "send a mail",
        "send an email",
    ),
    "job_hunt": (
        "job", "jobs", "role", "roles", "intern", "apply",
        "applying", "hunt", "sde", "developer", "engineer",
        "opening", "hiring", "vacancy", "interview",
    ),
    "email_monitor": (
        "inbox", "monitor", "monitoring", "alert", "notify",
        "interview mail", "interview mails", "offer mail",
        "offer mails", "watch my inbox", "check my inbox",
    ),
}

# Keywords an email_monitor draft watches for, when the
# message names them (otherwise the type default applies).
_EMAIL_MONITOR_KEYWORDS = (
    "interview", "offer", "shortlisted", "selected",
    "application", "rejection", "onboarding", "hr",
)

# Missing fields the user CAN answer in chat, in the order
# they are asked — at most ONE clarifying question per intake.
# A CSS selector is never in this map: it is not answerable in
# chat (the user picks it from the page), so it stays a missing
# field and never blocks the draft card.
_CLARIFYING_QUESTIONS: dict[str, str] = {
    "url": "Which product page should I watch? Paste the link.",
    "recipients": "Who should I mail? Give me the email addresses.",
    "target_price": "Below what price should I tell you?",
}


def _word_count(text: str, word: str) -> int:
    return len(re.findall(rf"\b{re.escape(word)}\b", text))


def _score_types(text: str) -> dict[str, int]:
    lowered = text.lower()
    scores = {}
    for mission_type, words in _TYPE_KEYWORDS.items():
        scores[mission_type] = sum(_word_count(lowered, w) for w in words)
    # "monitor" is a generic continuous verb, not an inbox
    # signal: "monitor the price" is a price watch, not an
    # inbox watch. It only counts for email_monitor when an
    # inbox/mail word is present too ("monitor my inbox",
    # "monitor my email" still count).
    if not re.search(r"\b(inbox|mail|mails|email|emails)\b", lowered):
        scores["email_monitor"] -= (
            _word_count(lowered, "monitor")
            + _word_count(lowered, "monitoring")
        )
    # A URL plus any price word is decisive for a price watch;
    # an email address plus any mail word is decisive for cold
    # mail. Without the companion signal, a bare URL or address
    # is not enough (a job post URL is not a price watch).
    if _URL_RE.search(text) and scores["price_watch"] > 0:
        scores["price_watch"] += 3
    if _EMAIL_RE.search(text) and scores["cold_mail"] > 0:
        scores["cold_mail"] += 3
    # "inbox" is unambiguous — watching an inbox is never a
    # price watch or a job hunt.
    if re.search(r"\binbox\b", text, re.IGNORECASE):
        scores["email_monitor"] += 3
    return scores


def _pick_type(text: str) -> tuple[str, dict[str, int]]:
    scores = _score_types(text)
    best = max(scores.values())
    if best == 0:
        return "job_hunt", scores
    # Tie order: the most specific signal wins — an inbox
    # mention beats a generic "watch", a price beats a mail.
    for mission_type in ("email_monitor", "price_watch",
                         "cold_mail", "job_hunt"):
        if scores[mission_type] == best:
            return mission_type, scores
    return "job_hunt", scores


# Continuous-language markers: the difference between a
# one-off task ("find me a job") and a standing mission
# ("keep applying for jobs"). A bare "watch" is NOT a marker —
# "watch this video" is a task, not a price watch; "watch my
# inbox" and "watch the price" are specific enough to count.
_CONTINUOUS_MARKERS = (
    "keep", "continuously", "continuous", "always", "every day",
    "daily", "whenever", "alert", "notify", "tell me when",
    "let me know when", "monitor", "track", "keep an eye",
    "regularly", "on repeat", "ongoing", "each day",
    "every morning", "watch my inbox", "check my inbox",
    "watch the price", "watch for",
)


def is_mission_request(message: str) -> bool:
    """A standing-mission ask, not a one-off task: continuous
    language AND a mission-type signal. Both halves are
    required — "keep this tab open" has the language but no
    type signal, and "find me a job" has the signal but no
    continuous language. Pure; the chat router uses it so a
    plain chat message ("watch my inbox for interview mails")
    drafts a mission instead of becoming a dead one-off task."""
    if not isinstance(message, str) or not message.strip():
        return False
    lowered = message.lower()
    continuous = any(
        re.search(rf"\b{re.escape(marker)}\b", lowered)
        for marker in _CONTINUOUS_MARKERS
    )
    if not continuous:
        return False
    return max(_score_types(message).values()) > 0


def _money_value(match) -> int | None:
    if match is None:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    if match.group(2):
        value *= 1000
    return int(value)


def _extract_caps(text: str, bounds: dict) -> None:
    for match in _CAP_RE.finditer(text):
        count = int(match.group(1))
        kind = match.group(2).lower()
        if kind in ("applies", "applications", "apply"):
            bounds["max_applies_per_day"] = count
        elif kind in ("mails", "emails", "sends"):
            bounds["max_sends_per_day"] = count
        else:
            bounds["max_actions_per_day"] = count


def _extract_locations(text: str) -> list[str]:
    found = []
    for loc in _KNOWN_LOCATIONS:
        if re.search(rf"\b{re.escape(loc)}\b", text, re.IGNORECASE):
            found.append(loc)
    return found


def _draft_name(mission_type: str, config: dict) -> str:
    if mission_type == "job_hunt":
        locations = config.get("locations") or []
        return "Job hunt" + (f" — {locations[0]}" if locations else "")
    if mission_type == "cold_mail":
        recipients = config.get("recipients") or []
        if recipients:
            noun = "recipient" if len(recipients) == 1 else "recipients"
            return f"Cold mail — {len(recipients)} {noun}"
        return "Cold mail"
    if mission_type == "email_monitor":
        keywords = config.get("keywords") or []
        if keywords:
            return f"Inbox watch — {', '.join(keywords[:2])}"
        return "Inbox watch"
    url = config.get("url") or ""
    try:
        host = urlparse(url).netloc or ""
    except ValueError:
        host = ""
    return "Price watch" + (f" — {host}" if host else "")


def _rules_draft(message: str) -> dict:
    """The deterministic fallback: keyword type detection +
    regex extraction, local only. Never sent anywhere."""
    mission_type, scores = _pick_type(message)
    config: dict = {}
    bounds: dict = {}
    missing: list[str] = []
    warnings: list[str] = []
    urls = _URL_RE.findall(message)
    url = urls[0].rstrip(".,") if urls else None
    emails = _EMAIL_RE.findall(message)

    if mission_type == "price_watch":
        if url:
            config["url"] = url
        else:
            missing.append("url")
        # A selector cannot be extracted from prose — the user
        # always picks it (in the workspace, or from the page).
        missing.append("selector")
        target = _money_value(_MONEY_RE.search(message))
        if target is None:
            target = _money_value(_TARGET_PRICE_RE.search(message))
        if target is not None:
            config["target_price"] = target
        elif re.search(r"\b(drop|below|cheaper|fall)\b", message, re.IGNORECASE):
            missing.append("target_price")
    elif mission_type == "cold_mail":
        if emails:
            config["recipients"] = sorted(set(emails))
        else:
            missing.append("recipients")
        # Rules cannot know which roles to mail about — the user
        # picks them (from a job hunt, or pasted in).
        missing.append("roles")
        if re.search(r"\b(resume|cv)\b", message, re.IGNORECASE):
            config["attach_resume"] = True
    elif mission_type == "email_monitor":
        # Keywords the message names ("interview", "offer");
        # when it names none, the type default applies
        # (validate_config_draft fills it) — nothing is missing.
        named = [
            kw for kw in _EMAIL_MONITOR_KEYWORDS
            if re.search(rf"\b{re.escape(kw)}\b", message, re.IGNORECASE)
        ]
        if named:
            config["keywords"] = named
    else:  # job_hunt
        locations = _extract_locations(message)
        if locations:
            config["locations"] = locations
        else:
            missing.append("locations")
        stipend = _money_value(_STIPEND_RE.search(message))
        if stipend is None:
            stipend = _money_value(_STIPEND_AFTER_RE.search(message))
        if stipend is None:
            stipend = _money_value(_MONEY_RE.search(message))
        if stipend is not None:
            config["stipend_min"] = stipend
        else:
            missing.append("stipend_min")
        score_match = _MIN_SCORE_RE.search(message)
        if score_match:
            value = int(score_match.group(1))
            if 0 <= value <= 100:
                config["min_score"] = value
                bounds["min_score"] = value

    _extract_caps(message, bounds)
    sites = sorted({s.lower() for s in _SITE_RE.findall(message)})
    if sites:
        bounds["allowed_sites"] = sites

    # A decisive type signal (score >= 2 and strictly ahead)
    # with nothing missing is high confidence; anything else is
    # low — the user is the judge of a rule-based draft.
    ordered = sorted(scores.values(), reverse=True)
    decisive = ordered[0] >= 2 and (len(ordered) < 2 or ordered[0] > ordered[1])
    confidence = "high" if decisive and not missing else "low"
    if scores[mission_type] == 0:
        warnings.append(
            "Could not tell what kind of mission this is —"
            " guessed job hunt. Pick the type if it is wrong."
        )
    draft_config = _types.validate_config_draft(mission_type, config)
    draft_bounds = _types.validate_bounds(mission_type, bounds)
    draft = {
        "type": mission_type,
        "name": _draft_name(mission_type, draft_config),
        "config": draft_config,
        "bounds": draft_bounds,
    }
    return {
        "draft": draft, "source": "rules", "confidence": confidence,
        "missing_fields": missing, "warnings": warnings,
    }


def _extract_json(raw: str) -> dict:
    """Pull the first {...} object out of planner output
    (fences and prose tolerated). Raises ValueError when there
    is no parseable JSON object."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in planner output")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("planner output is not a JSON object")
    return parsed


def _planner_draft(message: str, call_fn) -> dict:
    """The planner path. The message is redacted BEFORE the
    prompt is built — the prompt passed to call_fn carries no
    raw PII. Raises PlannerError / ValueError on failure (the
    caller falls back to rules)."""
    redacted, hits = scan.redact(message)
    if hits:
        logger.info(
            "intake redacted %d PII hits before the planner call",
            len(hits),
        )
    prompt = prompts.build_intake_prompt(redacted)
    raw, _model, _ms = call_fn(prompt)
    parsed = _extract_json(raw)
    mission_type = parsed.get("type")
    if mission_type not in _types.MISSION_TYPES:
        raise ValueError(f"planner returned unknown type {mission_type!r}")
    # Draft validation: required fields may be absent (they
    # become missing_fields); unknown keys / bad values fail.
    config = _types.validate_config_draft(mission_type, parsed.get("config") or {})
    bounds = _types.validate_bounds(mission_type, parsed.get("bounds") or {})
    name = parsed.get("name")
    if not isinstance(name, str) or not name.strip():
        name = _draft_name(mission_type, config)
    name = name.strip()[:120]
    missing: list[str] = []
    for item in parsed.get("missing_fields") or []:
        if isinstance(item, str) and item and item not in missing:
            missing.append(item)
    # Required fields the planner left empty are missing too,
    # whatever the planner claimed.
    spec = _types.MISSION_TYPES[mission_type]
    for field_name, field_spec in spec.config_schema.items():
        if field_spec.get("required") and not config.get(field_name):
            if field_name not in missing:
                missing.append(field_name)
    warnings = []
    if hits:
        warnings.append(
            "Personal details in your message were removed before"
            " drafting — check the draft for anything that needs them."
        )
    confidence = "high" if not missing else "low"
    return {
        "draft": {"type": mission_type, "name": name,
                  "config": config, "bounds": bounds},
        "source": "planner", "confidence": confidence,
        "missing_fields": missing, "warnings": warnings,
    }


def _clarifying_question(missing: list[str]) -> str | None:
    """At most ONE question, only when the intake is genuinely
    blocked on something the user can answer in chat. The
    priority order (url, recipients, target_price) is the order
    a mission needs them; a selector is never asked (see the
    map's comment)."""
    for field in ("url", "recipients", "target_price"):
        if field in missing and field in _CLARIFYING_QUESTIONS:
            return _CLARIFYING_QUESTIONS[field]
    return None


def _format_facts_line(proposed_facts: list[dict]) -> str:
    """One plain line summarising the proposed resume facts
    ("Name — Python, FastAPI; Hyderabad"), or ""."""
    parts = []
    for fact in proposed_facts or []:
        value = fact.get("value")
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        parts.append(f"{fact.get('label', fact.get('field'))}: {value}")
    return "; ".join(parts)


def _draft_card(draft: dict, missing: list[str],
                proposed_facts: list[dict]) -> str:
    """The draft as plain language for the chat thread — what
    the mission will do, its limits, and what is still needed.
    No jargon, no config keys."""
    mission_type = draft["type"]
    config = draft.get("config") or {}
    bounds = draft.get("bounds") or {}
    lines = []
    if mission_type == "job_hunt":
        text = "I'll keep looking for roles"
        locations = config.get("locations") or []
        if locations:
            text += f" in {', '.join(locations)}"
        stipend = config.get("stipend_min")
        if stipend:
            text += f", minimum stipend ₹{stipend:,} a month"
        cap = bounds.get("max_applies_per_day", 3)
        text += f", and apply to at most {cap} a day."
        if bounds.get("auto_approve"):
            text += " Applications go ahead on their own once they fit."
        else:
            text += " I'll ask you before each application."
        lines.append(text)
    elif mission_type == "cold_mail":
        recipients = config.get("recipients") or []
        count = len(recipients)
        noun = "person" if count == 1 else "people"
        text = f"I'll draft cold mails to {count} {noun}" if count else \
            "I'll draft cold mails"
        cap = bounds.get("max_sends_per_day", 5)
        text += f" and send at most {cap} a day."
        if config.get("attach_resume"):
            text += " Your resume goes with each mail."
        if not bounds.get("auto_approve"):
            text += " I'll ask you before each send."
        lines.append(text)
    elif mission_type == "price_watch":
        url = config.get("url") or "that product page"
        text = f"I'll watch {url}"
        target = config.get("target_price")
        if target is not None:
            text += f" and tell you when the price drops below ₹{target:,}."
        else:
            text += " and tell you when the price changes."
        lines.append(text)
        if "selector" in missing:
            lines.append(
                "I'll need you to point at the price on the page"
                " the first time it runs."
            )
    elif mission_type == "email_monitor":
        keywords = config.get("keywords") or []
        if keywords:
            text = (
                "I'll watch your inbox for mails about"
                f" {', '.join(keywords)} and tell you about them"
                " in the morning briefing."
            )
        else:
            text = (
                "I'll watch your inbox for interview and offer"
                " mails and tell you about them in the morning"
                " briefing."
            )
        lines.append(text)
    if missing:
        lines.append(f"Still needed: {', '.join(missing)}.")
    facts_line = _format_facts_line(proposed_facts)
    if facts_line:
        lines.append(
            f"From your resume: {facts_line}. Say yes and I'll"
            " save these as your profile."
        )
    return " ".join(lines)


def _enrich_with_resume(result: dict, message: str,
                        resume_text: str | None,
                        proposed_facts: list[dict],
                        upload_ids: list[str]) -> dict:
    """Fold resume context into an intake result (planner or
    rules — the enrichment is local-only either way). A job
    hunt takes locations / stipend from the resume when the
    message did not say them; a cold mail attaches the resume
    when one was uploaded; a job hunt with target roles in the
    resume gets them in its name. Fields the resume fills leave
    missing_fields. The result gains proposed_facts, upload_ids,
    the (at most one) clarifying question, and the draft card."""
    draft = result["draft"]
    mission_type = draft["type"]
    config = draft.get("config") or {}
    missing = list(result.get("missing_fields") or [])
    profile = None
    if resume_text:
        profile = profile_mod.parse_resume(resume_text)
    if profile is not None:
        if mission_type == "job_hunt":
            if not config.get("locations") and profile.get("locations"):
                config["locations"] = list(profile["locations"])
                if "locations" in missing:
                    missing.remove("locations")
            if (config.get("stipend_min") is None
                    and profile.get("stipend_min") is not None):
                config["stipend_min"] = profile["stipend_min"]
                if "stipend_min" in missing:
                    missing.remove("stipend_min")
        if mission_type == "cold_mail" and upload_ids:
            config["attach_resume"] = True
    if mission_type == "job_hunt" and result.get("source") == "rules":
        targets = next(
            (f.get("value") for f in proposed_facts
             if f.get("field") == "target_roles"),
            None,
        )
        if targets and draft.get("name") in ("Job hunt", None):
            draft["name"] = f"Job hunt — {targets[0]}"
        elif profile is not None and config.get("locations") \
                and draft.get("name") == "Job hunt":
            draft["name"] = _draft_name(mission_type, config)
    # A rules draft that was low-confidence only because fields
    # were missing is high-confidence once the resume fills them.
    if (result.get("source") == "rules" and not missing
            and result.get("confidence") == "low"):
        _picked, scores = _pick_type(message)
        ordered = sorted(scores.values(), reverse=True)
        if ordered[0] >= 2 and ordered[0] > ordered[1]:
            result["confidence"] = "high"
    result["missing_fields"] = missing
    result["proposed_facts"] = proposed_facts
    result["upload_ids"] = list(upload_ids)
    result["clarifying_question"] = _clarifying_question(missing)
    result["draft_card"] = _draft_card(draft, missing, proposed_facts)
    return result


def parse_intake(message: str, call_fn=None, resume_text: str | None = None,
                 upload_ids: list[str] | None = None,
                 conn=None) -> dict:
    """Parse one chat message into a mission draft.

    Planner first (redacted), rules on any planner failure —
    labelled, never a mock planner, never an exception for a
    planner failure. Raises ValueError only for an empty or
    oversized message (the API maps it to 422).

    Resume context (PR #7): pass `resume_text` directly, or
    `upload_ids` + a DB `conn` to load the extracted text of
    chat uploads. The resume NEVER reaches the planner — it
    only enriches the draft locally afterwards (see
    _enrich_with_resume)."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must not be empty")
    if len(message) > MAX_MESSAGE_LEN:
        raise ValueError(
            f"message must be at most {MAX_MESSAGE_LEN} characters"
        )
    message = message.strip()
    upload_ids = list(upload_ids or [])
    proposed_facts: list[dict] = []
    if conn is not None and upload_ids:
        if resume_text is None:
            resume_text = uploads_mod.resume_text_for_uploads(
                conn, upload_ids)
        proposed_facts = uploads_mod.proposed_facts_for_uploads(
            conn, upload_ids)
    elif resume_text:
        proposed_facts = uploads_mod.derive_proposed_facts(resume_text)
    call_fn = call_fn or router.call_planner
    try:
        result = _planner_draft(message, call_fn)
    except Exception as exc:  # noqa: BLE001 - any planner failure falls back to rules
        logger.info("intake planner path failed (%s); using rules", exc)
        result = _rules_draft(message)
        result["warnings"] = [
            "The planner could not draft this mission, so rules"
            " were used instead — check the draft before creating it."
        ] + result["warnings"]
    return _enrich_with_resume(result, message, resume_text,
                               proposed_facts, upload_ids)
