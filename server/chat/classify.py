"""Chat classification (PR #6): pure decision logic for the Agent
view's chat thread. No DB, no network, no planner calls — every
function here is unit-testable on its own.

The bug this fixes (Prateek's live test): typing "hi" twice created
two dead tasks stuck at "created". A greeting must NEVER create a
task, so the rules are authoritative for small talk: greetings,
thanks, goodbyes and capability questions are answered locally and
never reach the planner (chat_api.respond returns them before any
planner call).

Decision order in classify_rules:
  1. greeting / small talk  -> kind "chat" (hard: never a task)
  2. identity statement / correction ("i'm Akash", "no, my name
     is Y") -> kind "chat" (hard: never a task) + an "identity"
     payload the caller stores as a profile fact
  3. "remember ..."         -> kind "chat" (hard) + a "remember"
     payload the caller stores (or refuses, for PII/credentials)
  4. imperative + object    -> kind "task_created"
  5. other statements ("i'm a student", "i live in Hyderabad")
     -> kind "chat", reason "statement": the model answers
     conversationally; the rules fallback is an honest
     no-model message, never a clarifying question
  6. anything else          -> kind "chat" + ONE clarifying question

PR #7 fix (Prateek's live test): "i'm Akash" used to become the
bare name "Akash" — the "i'm " prefix was stripped and the
fragment got the same clarifying question twice. The name
statement is now its own category, and the "i'm " prefix is
NEVER stripped into a bare name: parse_identity runs before the
task check, and _PREFIXES is only used to find an imperative
verb (where "i'm looking for jobs" still strips to the verb
phrase "looking for").

Steering (classify_steering) runs BEFORE this decision, but only
when the conversation has an active task: stop/cancel/pause act on
that task, and a refinement narrows it — neither creates a task.
A clear new-task instruction is never steering: it starts a new
task instead.
"""
from __future__ import annotations

import json
import re

MAX_MESSAGE_LEN = 2000

# ----------------------------------------------------------------
# Normalisation
# ----------------------------------------------------------------

_GREETING_RE = re.compile(
    r"^(good morning|good afternoon|good evening|good day|hello|hiya"
    r"|howdy|hey|hi|yo|namaste)\b[,.!?\s]*(.*)$"
)
_GREETING_FILLER = {"", "there", "eclipse", "again", "there eclipse"}


def _clean(text: str) -> str:
    """Lowercased, whitespace-collapsed, punctuation-trimmed."""
    return re.sub(r"\s+", " ", text.strip().lower()).strip(" .!?,")


def _strip_prefix(text: str, prefixes: tuple[str, ...]) -> str:
    """Strip leading prefixes (matched case-insensitively) from
    the ORIGINAL text, preserving its case. Loops: "please, can
    you find…" strips both."""
    stripped = text.strip()
    changed = True
    while changed:
        changed = False
        lowered = stripped.lower()
        for prefix in prefixes:
            if lowered.startswith(prefix):
                stripped = stripped[len(prefix):].strip()
                changed = True
                break
    return stripped


_PREFIXES = (
    "please ",
    "can you ", "could you ", "will you ", "would you ",
    "i want you to ", "i need you to ", "i'd like you to ",
    "help me to ", "help me ",
    "i want to ", "i want ", "i need to ", "i need ",
    "i'd like to ", "i'd like ",
    "i am ", "i'm ",
)

# ----------------------------------------------------------------
# Small talk (hard chat — never a task, never the planner)
# ----------------------------------------------------------------

_CAPABILITY = (
    "I can search for job roles and apply to matching ones, watch a"
    " price on a page, and draft cold mails — and I always ask"
    " before applying, sending, or spending anything. Just tell me"
    " what you need."
)
_WHO_AM_I = (
    "I'm Eclipse, your browser agent — I do web tasks for you and"
    " ask before anything that applies, sends, or spends."
)


def _greeting_reply(greeting: str) -> str:
    # Sentence case, not title case: "Good morning!", not
    # "Good Morning!".
    opener = greeting[0].upper() + greeting[1:] if greeting else greeting
    return (
        f"{opener}! I'm Eclipse — I can find and apply to roles,"
        " watch prices, or draft cold mails for you. What would you"
        " like me to do?"
    )


def _small_talk(text: str) -> tuple[str, str] | None:
    """(reason, reply) when the message is small talk, else None.

    `text` is the cleaned (lowercased) message. Every match is
    anchored: "hi, find me jobs" is NOT a greeting — the remainder
    after the greeting carries an instruction, so it falls through
    to the task check.
    """
    # Greeting only (plus optional filler): "hi", "good morning",
    # "hi there". A greeting followed by anything else is not a
    # greeting — "hi, find me jobs" is a task.
    match = _GREETING_RE.match(text)
    if match:
        remainder = match.group(2).strip(" .!?,")
        if remainder in _GREETING_FILLER:
            return ("greeting", _greeting_reply(match.group(1)))
    # "how are you" / "how's it going" / "what's up" — only when
    # the message carries no instruction with it.
    if re.search(
        r"\bhow are you\b|\bhow'?s it going\b|\bwhat'?s up\b"
        r"|\bhow are u\b",
        text,
    ) and not _has_imperative(text):
        return (
            "how_are_you",
            "I'm doing well, thanks — ready when you are. Want me"
            " to find roles, watch a price, or draft a mail?",
        )
    # Thanks, on its own: "thanks", "thank you so much".
    thanks = re.match(
        r"^(thanks|thank you|thx|ty)\b[,.!?\s]*(.*)$", text
    )
    if thanks:
        remainder = thanks.group(2).strip(" .!?,")
        if remainder in ("", "a lot", "so much", "very much", "eclipse",
                         "for the help", "for your help", "a bunch"):
            return (
                "thanks",
                "You're welcome — happy to help. Anything else"
                " you'd like me to do?",
            )
    # Goodbye, on its own.
    bye = re.match(
        r"^(bye|goodbye|good night|goodnight|see you|see ya)\b"
        r"[,.!?\s]*(.*)$",
        text,
    )
    if bye:
        remainder = bye.group(2).strip(" .!?,")
        if remainder in ("", "eclipse", "for now"):
            return ("bye", "Goodbye! I'll be here whenever you need me.")
    # Capabilities / identity — only when the message is the
    # question, not a question plus an instruction.
    if not _has_imperative(text):
        if re.search(
            r"what can you do|what do you do|what can u do"
            r"|how do you work|what are you for",
            text,
        ):
            return ("capabilities", _CAPABILITY)
        if re.search(r"who are you|what are you\b", text):
            return ("capabilities", _WHO_AM_I)
        if re.match(r"^help(\s+me)?[.!?\s]*$", text) or re.match(
            r"^i need help[.!?\s]*$", text
        ):
            return ("capabilities", _CAPABILITY)
    return None


# ----------------------------------------------------------------
# Task instructions (imperative verb + object)
# ----------------------------------------------------------------

_TASK_VERBS = (
    "find", "search", "apply", "watch", "track", "monitor",
    "draft", "write", "send", "check", "open", "book",
    "remind", "get", "fetch", "list", "create", "start",
)
_TASK_VERB_PHRASES = ("look for", "looking for")
# Words after the verb that do not count as an object: "find it"
# has no object, so it is ambiguous, not a task.
_OBJECT_STOPWORDS = {
    "me", "for", "a", "an", "the", "some", "any", "my", "to",
    "on", "in", "at", "up", "out", "it", "this", "that", "them",
    "us", "please", "out",
}
_CONSEQUENTIAL_WORDS = (
    "apply", "send", "buy", "pay", "book", "order", "subscribe",
    "checkout", "purchase",
)


def _verb_and_object(text: str) -> tuple[str, str] | None:
    """(verb, object-text) when the message starts with an
    imperative verb after its greeting/polite prefixes, else None.
    "hi, find me jobs" counts — the greeting is stripped first."""
    core = text
    greeting = _GREETING_RE.match(core)
    if greeting and greeting.group(2).strip():
        core = greeting.group(2).strip()
    core = _strip_prefix(core, _PREFIXES)
    for phrase in _TASK_VERB_PHRASES:
        if core.startswith(phrase + " ") or core == phrase:
            return (phrase, core[len(phrase):].strip())
    first = core.split(" ", 1)
    if first[0] in _TASK_VERBS:
        rest = first[1] if len(first) > 1 else ""
        return (first[0], rest.strip())
    return None


def _has_object(object_text: str) -> bool:
    tokens = re.findall(r"[a-z0-9]+", object_text.lower())
    return any(t not in _OBJECT_STOPWORDS for t in tokens)


def _has_imperative(text: str) -> bool:
    """Does the message carry an imperative verb + object? Used to
    keep small talk and steering from swallowing instructions."""
    found = _verb_and_object(text)
    return found is not None and _has_object(found[1])


def _is_task_instruction(text: str) -> bool:
    return _has_imperative(text)


def _task_reply(text: str) -> str:
    """Confirm a task in plain words, from the user's own message.
    "Find me remote SDE intern roles in Hyderabad" becomes
    "On it — I'll find remote SDE intern roles in Hyderabad."
    Consequential goals get the approval note: Eclipse always asks
    before applying, sending, or spending."""
    cleaned = text.strip()
    greeting = _GREETING_RE.match(cleaned.lower())
    if greeting and greeting.group(2).strip():
        # Slice the original by the greeting's length, keeping case.
        cleaned = cleaned[len(greeting.group(1)):].lstrip(" ,.!?").strip()
    cleaned = _strip_prefix(cleaned, _PREFIXES)
    # "find me X" reads better as "find X".
    for verb in ("find", "search", "get"):
        if cleaned.lower().startswith(f"{verb} me "):
            cleaned = cleaned[len(verb) + 4:].strip()
            cleaned = f"{verb} {cleaned}"
            break
    sentence = cleaned[0].lower() + cleaned[1:] if cleaned else cleaned
    sentence = sentence.rstrip(".!") + "."
    reply = f"On it — I'll {sentence}"
    lowered = text.lower()
    if "apply" in lowered or "application" in lowered:
        reply += " I'll ask before each apply."
    elif "send" in lowered or "mail" in lowered:
        reply += " I'll ask before sending anything."
    elif any(w in lowered for w in _CONSEQUENTIAL_WORDS):
        reply += " I'll ask before anything that applies, sends, or spends."
    return reply


# ----------------------------------------------------------------
# Clarification (ambiguous fragments — ONE question, never a task)
# ----------------------------------------------------------------

_JOB_WORDS = (
    "intern", "job", "role", "sde", "hiring", "career", "vacancy",
    "opening", "engineer", "developer", "fresher",
)
_PRICE_WORDS = ("price", "deal", "discount", "cheap", "cost", "sale", "offer")
_MAIL_WORDS = ("mail", "email", "recruiter", "outreach", "cover letter")


def _clarification(text: str) -> str:
    """One clarifying question, tailored to the fragment's topic.
    "internships" asks about intern roles; anything else gets the
    generic question plus an example of a message that works."""
    lowered = text.lower()
    if any(re.search(rf"\b{w}", lowered) for w in _JOB_WORDS):
        return (
            "Are you looking for intern roles? Tell me a bit more —"
            " remote or a city, and any stipend floor?"
        )
    if any(re.search(rf"\b{w}\b", lowered) for w in _PRICE_WORDS):
        return (
            "Want me to watch a price? Send me the page link and"
            " the price to tell you about."
        )
    if any(re.search(rf"\b{w}\b", lowered) for w in _MAIL_WORDS):
        return (
            "Want me to draft a cold mail? Tell me who it's for"
            " and what role it's about."
        )
    return (
        "Tell me a bit more — what should I do with that? For"
        " example: 'find remote SDE intern roles in Hyderabad'."
    )


# ----------------------------------------------------------------
# Identity, statements, corrections, "remember ..." (PR #7)
# ----------------------------------------------------------------

# Honest replies for when no model answers. Neither asks a
# question: a reply of only questions is a failure, and asking
# the same clarifying question twice is worse — the caller
# (chat_api.respond) uses REPEATED_QUESTION_REPLY when the rules
# clarifier would repeat the last question word for word.
NO_MODEL_REPLY = (
    "I couldn't reach a model to chat about that, so I can't"
    " answer properly right now. If you tell me as an"
    " instruction — for example 'find remote SDE intern roles"
    " in Hyderabad' — I can start it straight away."
)
REPEATED_QUESTION_REPLY = (
    "I don't want to keep asking the same question — I couldn't"
    " reach a model to work out what you mean. If you say it as"
    " an instruction, like 'find remote SDE intern roles in"
    " Hyderabad', I can start it straight away."
)

# First words after "i'm" / "i am" that are NOT a name: "i'm
# looking for jobs" is a task, "i'm tired" is a state, "i'm in
# Hyderabad" is a statement — none of them names the user.
_NAME_BLOCKLIST = {
    "looking", "searching", "trying", "waiting", "going", "coming",
    "working", "studying", "living", "staying", "doing", "getting",
    "thinking", "wondering", "hoping", "planning", "feeling",
    "here", "back", "done", "not", "so", "just", "really", "sorry",
    "afraid", "glad", "happy", "sad", "tired", "busy", "free",
    "in", "at", "on", "new", "the", "a", "an", "sure", "fine",
    "ok", "okay", "good", "great", "lost", "stuck", "confused",
    "ready", "interested", "keen", "open", "available",
    # Clause words a greedy name capture must never swallow:
    # "my name is Akash and I'm looking for jobs" is not the
    # name "Akash And I'm" — it is a task carrying a name.
    "and", "or", "but", "i'm", "im", "i", "am",
}
# Words that can never be part of a name (a verb or a stopword
# where a name should be means the pattern misfired).
_NAME_STOPWORDS = _NAME_BLOCKLIST | set(_TASK_VERBS) | _OBJECT_STOPWORDS

_CORRECTION_LEADS = ("no", "nope", "actually", "wait")


def _tidy_name(raw: str) -> str | None:
    """A plausible person name (1–3 words, letters only), or
    None. All-lowercase input is title-cased ("akash" -> "Akash");
    mixed-case input is kept as written ("McDonald" stays).
    A trailing vocative ("man", "bro", "dude") is dropped when
    another word remains — "my name is akash man" names Akash,
    not "Akash Man". A lone vocative stays a name ("Man" alone
    is a surname). Note: no comma handling here — a comma in
    the remainder means a second clause, and the callers that
    must tolerate one ("my name is", "call me") capture only
    the name words with a bounded regex instead. The "i'm"
    branch deliberately keeps the full capture, so "i'm Akash,
    find me jobs" stays a task, not an identity."""
    name = re.sub(r"\s+", " ", raw.strip().strip(" .!?,\"'")).strip()
    if not name:
        return None
    words = name.split(" ")
    if len(words) > 1 and words[-1].lower() in {"man", "bro", "dude", "mate"}:
        words = words[:-1]
        name = " ".join(words)
    if len(words) > 3:
        return None
    for word in words:
        if not re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", word):
            return None
        if word.lower() in _NAME_STOPWORDS:
            return None
    if name == name.lower():
        name = " ".join(w.capitalize() for w in words)
    return name


def parse_identity(text: str) -> dict | None:
    """A name statement or correction, or None. Pure.

    Matches (case-insensitive, punctuation tolerated):
    - "i'm Akash" / "i am Akash" — the remainder must be 1–3
      words and must not start with a verb/state word, so "i'm
      looking for jobs" is NOT a name (it is a task) and "i'm
      tired" is NOT a name either.
    - "my name is Akash" / "my name's Akash"
    - "call me Akash"
    - "it's Akash" (capitalised name only — "it's raining" is
      not a name)
    - corrections: a leading "no," / "actually" / "wait", or
      "i just told you ...", marks correction=True.
    Returns {"name", "correction"} — the caller stores the name
    as a profile fact and confirms it. NEVER returns a bare
    name with the "i'm" stripped and the statement lost: the
    return value always carries the statement's meaning.
    """
    cleaned = _clean(text)
    correction = False
    core = cleaned
    for lead in _CORRECTION_LEADS:
        if core == lead or core.startswith(lead + " ") or core.startswith(lead + ","):
            correction = True
            core = core[len(lead):].strip(" ,").strip()
            break
    if "just told you" in core or "already told you" in core or "i said" in core:
        correction = True
    # "i just told you my name is Y" / "i said i'm Y": drop the
    # preamble so the patterns below see the statement itself.
    # The comma after "you" is tolerated ("i just told you, my
    # name is Y") — without it the strip silently misses and
    # the whole message falls through to a clarifier (found in
    # live screenshot verification, 2026-11-23).
    core = re.sub(r"^i (?:just |already )?told you[,\s]+", "", core)
    core = re.sub(r"^i said[,\s]+", "", core)

    name: str | None = None
    # The name capture is BOUNDED (1–3 letter-words, stopping
    # at the first comma or non-name word): "my name is akash,
    # man" captures "akash", never "akash, man". The "i'm"
    # branch below deliberately keeps the full capture, so
    # "i'm Akash, find me jobs" stays a task.
    _NAME_WORDS = r"([A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){0,2})"

    def _followed_by_task(match: re.Match) -> bool:
        """A task verb after the matched name ("my name is
        Akash, find me jobs") — the message is a task carrying
        a name, not a name statement, so identity must not
        swallow the instruction."""
        rest = core[match.end():].lower()
        if any(phrase in rest for phrase in _TASK_VERB_PHRASES):
            return True
        return any(w in _TASK_VERBS for w in re.findall(r"[a-z]+", rest))

    match = re.match(rf"^(?:my name is|my name's)\s+{_NAME_WORDS}", core)
    task_after_name = False
    if match:
        name = _tidy_name(match.group(1))
        if name is not None and _followed_by_task(match):
            name = None
            task_after_name = True
    if name is None and not task_after_name:
        # Belt and braces: "my name is X" anywhere in the
        # message, for preambles the strip above never saw
        # ("listen, my name is Akash").
        match = re.search(rf"my name is\s+{_NAME_WORDS}", core)
        if match:
            name = _tidy_name(match.group(1))
            if name is not None and _followed_by_task(match):
                name = None
    if name is None:
        match = re.match(rf"^call me\s+{_NAME_WORDS}", core)
        if match:
            name = _tidy_name(match.group(1))
            if name is not None and _followed_by_task(match):
                name = None
    if name is None:
        match = re.match(r"^(?:i'm|i am|im)\s+(.+)$", core)
        if match:
            remainder = match.group(1)
            first = remainder.split(" ", 1)[0]
            if first not in _NAME_BLOCKLIST:
                name = _tidy_name(remainder)
    if name is None:
        # "it's Akash" — capitalised on the ORIGINAL text only.
        match = re.match(
            r"^it's\s+([A-Z][A-Za-z'\-]*(?: [A-Z][A-Za-z'\-]*){0,2})[.!?]*$",
            text.strip(),
        )
        if match:
            name = _tidy_name(match.group(1))
    if name is None:
        return None
    return {"name": name, "correction": correction}


def parse_remember(text: str) -> dict | None:
    """An explicit "remember ..." fact capture, or None. Pure.

    - "remember my city is Hyderabad" -> {"key": "city",
      "value": "Hyderabad", "fact_text": "My city is Hyderabad."}
    - "remember that i prefer remote roles" -> {"key": None,
      "value": "i prefer remote roles",
      "fact_text": "I prefer remote roles."}
    - "remember i'm Akash" -> key "name" (the caller stores it
      as a profile fact, like an identity statement).
    The caller (chat_api) refuses PII/credential values before
    storing — this parser only extracts, it never judges.

    Matching runs on the ORIGINAL text (case-insensitive), not
    the cleaned lowercase form: the value keeps the user's own
    case ("Hyderabad", not "hyderabad"). Only the key is
    lowercased — it is an identifier, used to find the old
    value when a correction overwrites it.
    """
    match = re.match(
        r"^\s*remember(?:\s+that)?\s+(.+?)\s*[.!?]*$",
        text.strip(), re.IGNORECASE,
    )
    if not match:
        return None
    rest = re.sub(r"\s+", " ", match.group(1).strip())
    if not rest:
        return None
    # "remember to check my mail" is a reminder, not a fact —
    # it is not stored (it falls through to the task/model path).
    if re.match(r"^to\s+", rest, re.IGNORECASE):
        return None
    key_match = re.match(r"^my\s+(.+?)\s+is\s+(.+)$", rest, re.IGNORECASE)
    if key_match:
        key = re.sub(r"\s+", " ", key_match.group(1).strip()).lower()
        value = key_match.group(2).strip().strip(" .!?")
        if (
            key
            and value
            and len(value) <= 200
            and len(key.split(" ")) <= 3
            and all(re.fullmatch(r"[a-z]+", w) for w in key.split(" "))
        ):
            return {
                "key": key,
                "value": value,
                "fact_text": f"My {key} is {value}.",
            }
        # A key that does not validate falls through to the
        # free-form fact below — the user still gets it stored.
    name_match = re.match(r"^(?:i'm|i am|im)\s+(.+)$", rest, re.IGNORECASE)
    if name_match:
        name = _tidy_name(name_match.group(1))
        if name is not None:
            return {
                "key": "name",
                "value": name,
                "fact_text": f"My name is {name}.",
            }
    if len(rest) > 200:
        return None
    fact_text = rest.strip().strip(" .!?")
    if not fact_text:
        return None
    fact_text = fact_text[0].upper() + fact_text[1:] + "."
    return {"key": None, "value": rest, "fact_text": fact_text}


_STATEMENT_RES = (
    # "i'm a student" / "i'm in Hyderabad" / "i'm from Pune"
    re.compile(r"^(i'm|i am|im)\s+(a|an|the|in|from|at|on)\b"),
    re.compile(r"^i\s+(live|work|study)\b"),
    re.compile(r"^i\s+(like|love|prefer|enjoy|hate)\b"),
    re.compile(
        r"^my\s+(city|college|university|location|country|state"
        r"|company|team|goal|preference|favourite|favorite)\s+is\b"
    ),
)


def _is_statement(cleaned: str) -> bool:
    """A statement about the user that is neither a task nor a
    name: the model answers it conversationally, and the rules
    fallback is an honest no-model message — never a clarifying
    question about something the user just told us."""
    return any(rx.search(cleaned) for rx in _STATEMENT_RES)


def classify_rules(text: str) -> dict:
    """The rules decision for one message. Pure.

    Returns {"kind", "reply_text", "reason", "hard_chat"} plus,
    for identity/remember decisions, an "identity" or "remember"
    payload the caller acts on:
    - kind "chat" + hard_chat True: small talk, an identity
      statement/correction, or a "remember ...". The caller must
      never send this to the planner and never create a task.
      Identity replies confirm the name ("Nice to meet you,
      Akash — I'll remember that"); corrections confirm the
      update ("Got it — I'll remember you as Y from now on.").
    - kind "task_created": a clear instruction (imperative + an
      object). The goal is the message itself.
    - kind "chat" + reason "statement": a statement about the
      user. The model answers it with history; the rules
      fallback is NO_MODEL_REPLY (honest, no question).
    - kind "chat" + reason "ambiguous": a fragment the rules
      cannot act on. The caller may still ask the planner, which
      has the conversation history to resolve it — but with no
      history the rules win (see chat_api.respond). The rules
      clarifier is the LAST resort and must never repeat the
      last question word for word (see chat_api.respond).
    """
    cleaned = _clean(text)
    small = _small_talk(cleaned)
    if small is not None:
        return {
            "kind": "chat", "reply_text": small[1],
            "reason": small[0], "hard_chat": True,
        }
    identity = parse_identity(text)
    if identity is not None:
        name = identity["name"]
        if identity["correction"]:
            reply = f"Got it — I'll remember you as {name} from now on."
            reason = "correction"
        else:
            reply = f"Nice to meet you, {name} — I'll remember that."
            reason = "identity"
        return {
            "kind": "chat", "reply_text": reply, "reason": reason,
            "hard_chat": True, "identity": identity,
        }
    remember = parse_remember(text)
    if remember is not None:
        if remember["key"] == "name":
            reply = (
                f"Got it — I'll remember your name is"
                f" {remember['value']}."
            )
        elif remember["key"]:
            reply = (
                f"Got it — I'll remember your {remember['key']} is"
                f" {remember['value']}."
            )
        else:
            value = remember["value"]
            value = value[0].upper() + value[1:] if value else value
            reply = f"Got it — I'll remember that {value}."
        return {
            "kind": "chat", "reply_text": reply, "reason": "remember",
            "hard_chat": True, "remember": remember,
        }
    if _is_task_instruction(cleaned):
        return {
            "kind": "task_created", "reply_text": _task_reply(text),
            "reason": "task", "hard_chat": False,
        }
    if _is_statement(cleaned):
        return {
            "kind": "chat", "reply_text": NO_MODEL_REPLY,
            "reason": "statement", "hard_chat": False,
        }
    return {
        "kind": "chat", "reply_text": _clarification(cleaned),
        "reason": "ambiguous", "hard_chat": False,
    }


# ----------------------------------------------------------------
# Steering (only when a task is active — see chat_api.respond)
# ----------------------------------------------------------------

_STOP_RES = (
    re.compile(
        r"^(please\s+)?(stop|cancel|halt|abort)"
        r"(\s+(it|this|that|the(\s+\w+){1,2}|everything|all))?"
        r"(\s+please)?[.!?]*$"
    ),
    re.compile(
        r"^(never\s+mind|nevermind|forget\s+it|forget\s+about\s+it"
        r"|call\s+it\s+off|that'?s\s+enough)[.!?]*$"
    ),
)
_PAUSE_RES = (
    re.compile(
        r"^(please\s+)?pause"
        r"(\s+(it|this|that|the(\s+\w+){1,2}))?"
        r"(\s+please)?[.!?]*$"
    ),
    re.compile(r"^hold\s+on[.!?]*$"),
)
# A bare constraint word, on its own, narrows the active task:
# "remote" means remote only; "Hyderabad" means Hyderabad only.
_BARE_CONSTRAINT_WORDS = {
    "remote", "hybrid", "onsite", "on-site", "wfh",
    "hyderabad", "bangalore", "bengaluru", "chennai", "pune",
    "mumbai", "delhi", "noida", "gurugram", "gurgaon", "kolkata",
    "ahmedabad", "jaipur", "indore", "coimbatore",
}
_CITY_WORDS = (
    "hyderabad", "bangalore", "bengaluru", "chennai", "pune",
    "mumbai", "delhi", "noida", "gurugram", "gurgaon", "kolkata",
    "ahmedabad", "jaipur", "indore", "coimbatore",
)


def _is_refinement(text: str) -> bool:
    """Is this message a constraint change for the active task
    ("remote only", "min 15k stipend", "not that company")?
    Only called after the new-task check, so an instruction like
    "find roles with min 15k stipend" never lands here."""
    if re.search(r"\bonly\b", text):
        return True
    if re.search(
        r"\b(min|minimum|at least|at most|max|maximum|under|below"
        r"|over|above)\b",
        text,
    ):
        return True
    # "no" / "not" need a word after them: a bare "no" is an
    # answer to a question, not a refinement.
    if re.search(
        r"^not\s+\w|\bnot that\b|^don'?t\b|^do not\b|\bexclude\b"
        r"|\bskip\b|\bavoid\b|^no\s+\w",
        text,
    ):
        return True
    if re.search(r"\binstead\b", text):
        return True
    if re.search(r"^change\b|^make it\b|^also\b", text):
        return True
    words = text.split()
    if len(words) == 1 and words[0] in _BARE_CONSTRAINT_WORDS:
        return True
    # A short message naming a place or a work mode narrows the
    # hunt: "in Hyderabad", "remote roles".
    if len(words) <= 4 and (
        re.search(r"\b(remote|hybrid|onsite|wfh)\b", text)
        or any(re.search(rf"\b{city}\b", text) for city in _CITY_WORDS)
    ):
        return True
    return False


def classify_steering(text: str) -> str | None:
    """Steering intent for a message arriving while a task is
    active. Returns "cancel" | "pause" | "refine" | None. Pure.

    A clear new-task instruction is NEVER steering — it starts a
    new task instead (checked first). A leading "no," / "actually"
    is stripped before the checks, so "no, remote only" refines
    and "no, cancel it" cancels.
    """
    cleaned = _clean(text)
    core = re.sub(r"^(no,|actually|well)[,.]?\s+", "", cleaned)
    if _is_task_instruction(core):
        return None
    if any(rx.match(core) for rx in _STOP_RES):
        return "cancel"
    if any(rx.match(core) for rx in _PAUSE_RES):
        return "pause"
    if _is_refinement(core):
        return "refine"
    return None


def refinement_phrase(text: str) -> str:
    """The refinement in the user's own words, trimmed for the
    confirmation reply: "No, remote only" -> "remote only"."""
    cleaned = text.strip().strip(".!")
    lowered = cleaned.lower()
    for prefix in ("no, ", "no ", "actually ", "please "):
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            lowered = cleaned.lower()
            break
    if cleaned:
        cleaned = cleaned[0].lower() + cleaned[1:]
    return cleaned


def task_label(goal: str) -> str:
    """A short name for a task, for steering replies: "the job
    hunt", "the price watch", "the mail run", else "the task"."""
    lowered = (goal or "").lower()
    if any(w in lowered for w in ("intern", "job", "role", "hiring")):
        return "the job hunt"
    if any(w in lowered for w in ("price", "watch")):
        return "the price watch"
    if any(w in lowered for w in ("mail", "email")):
        return "the mail run"
    return "the task"


# ----------------------------------------------------------------
# Planner path (prompt + output parsing — still pure text work)
# ----------------------------------------------------------------

_CHAT_PROMPT_INSTRUCTIONS = """\
You are Eclipse, a privacy-first browser agent. Classify the user's message and draft a reply. Return ONE JSON object, nothing else (no prose, no markdown fence):
{"kind": "chat" | "task_created", "reply_text": "<your reply>", "constraints": "<task limits or null>"}

Rules:
- Talk like a helpful assistant, not a form: answer statements and questions naturally, using the conversation and the REMEMBERED FACTS. Never ask for a detail that is already in the conversation or the facts — if the user already told you their name or city, use it.
- Assume defaults and start: if the message is close enough to an instruction, treat it as one. Ask at most ONE clarifying question, only when you are genuinely blocked — and never ask the same question twice. A reply that is only questions is a failure: lead with what you can do, and only then ask what you need.
- If the user states a fact about themselves (their name, city, preference), acknowledge it briefly — the server stores it separately, so do not ask them to repeat it. If they correct something ("no, my name is Y"), accept the correction, do not re-ask.
- kind "chat" for greetings, small talk, thanks, goodbyes, questions about what you can do, statements about the user, and messages too vague to act on. A bare noun like "internships" is too vague — ask ONE clarifying question instead of creating a task.
- kind "task_created" ONLY when the message is a clear instruction to do something in the browser (find roles, watch a price, draft a mail, check a page). The goal is the message itself. A short reply to your clarifying question ("remote ones", "yes, Hyderabad") CAN be a task — resolve it against the conversation above.
- NEVER return task_created for a greeting or small talk.
- reply_text is plain words, one or two sentences. For a task, confirm what you will do and that you will ask before anything that applies, sends, or spends.
- constraints: only real limits the message states (remote only, a city, a stipend floor), or null. Never invent one.
- Only use what the message and the conversation actually say. Never invent a site, a role, a URL, or a price."""


def build_chat_prompt(
    text: str, history: list[dict], facts: list[str]
) -> str:
    """The planner prompt: instructions + the last few chat
    messages + recalled memory facts + the new message. `history`
    is oldest-first [{"role", "text"}]; `facts` are plain strings.
    The caller redacts the assembled prompt before the call (the
    loop.plan_task pattern)."""
    parts = [_CHAT_PROMPT_INSTRUCTIONS]
    if history:
        lines = [f"{m['role']}: {m['text']}" for m in history]
        parts.append("CONVERSATION SO FAR:\n" + "\n".join(lines))
    if facts:
        parts.append("REMEMBERED FACTS:\n" + "\n".join(facts))
    parts.append(f"MESSAGE: {text}")
    return "\n\n".join(parts)


def _extract_json(raw: str) -> dict:
    """Pull the first {...} object out of planner output (fences
    and prose tolerated). Raises ValueError when there is no
    parseable JSON object. (Same shape as the intake's — the
    planner is a text model, not a JSON API.)"""
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


def parse_planner_reply(raw: str) -> dict:
    """Validate one planner reply. Returns {"kind", "reply_text",
    "constraints"}; raises ValueError on anything unusable (the
    caller falls back to rules)."""
    parsed = _extract_json(raw)
    kind = parsed.get("kind")
    if kind not in ("chat", "task_created"):
        raise ValueError(f"planner returned unknown kind {kind!r}")
    reply = parsed.get("reply_text")
    if not isinstance(reply, str) or not reply.strip():
        raise ValueError("planner returned no reply_text")
    constraints = parsed.get("constraints")
    if constraints is not None and not isinstance(constraints, str):
        raise ValueError("planner constraints are not a string")
    return {
        "kind": kind,
        "reply_text": reply.strip()[:1000],
        "constraints": (constraints or "").strip() or None,
    }
