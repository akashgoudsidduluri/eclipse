"""Chat API (PR #6, brain reworked in PR #7): the Agent view is
a real chat thread.

POST /chat/message {text} -> 200 {message_id, reply_text,
  kind: "chat" | "task_created", task_id, source: "planner" | "rules",
  provider: "ollama" | "antseed" | "groq" | ... | null,
  draft?: <mission-intake result, only for mission requests>}
GET  /chat/history?limit=50 -> {messages: [{id, role, text, kind,
  task_id, source, provider, created_at}]} — oldest first,
  created_at unix SECONDS, source "planner"/"rules" on agent
  rows (null on user rows, so a reload still shows how each
  reply was made). provider names the model that answered
  ("ollama", "antseed", "groq") on planner rows, null on
  rules/user rows — every reply is labelled with its source.

Token-authenticated like every other router. 422 on empty/blank
text (or over 2000 chars). No other endpoints: task progress is
rendered by the UI from the existing task endpoints.

How one message is answered (respond):
  1. Steering first, when the conversation has an active
     (non-terminal) task: stop/cancel/pause act on that task
     through the EXISTING task endpoints (tasks_api.cancel_task /
     pause_task — the same path the UI uses, claims released on
     cancel); a refinement appends to the task's stored
     constraints and never creates a task. A clear new-task
     instruction is never steering — it starts a new task.
  2. Identity + "remember ...": a name statement ("i'm Akash")
     is stored as a profile fact and confirmed ("Nice to meet
     you, Akash — I'll remember that"); a correction ("no, my
     name is Y") OVERWRITES the stored name, never re-asks.
     "remember my X is Y" stores a fact with a 90-day TTL
     (budgets.expire_stale_facts, maintained here for the
     global scope — the mission runner only expires mission
     scopes); a repeat for the same key overwrites the old
     value. PII/credential values are REFUSED, never stored.
     All of this is rules, never the planner, never a task.
  3. Small talk (greetings, thanks, goodbyes, capability
     questions) is answered by the rules at once — it never
     reaches the planner and can never create a task. This is the
     fix for the live-test bug: "hi" created two dead tasks.
  3b. Standing missions: a message asking for continuous
     watching/applying ("keep applying for SDE intern roles",
     "watch my inbox for interview mails") is a MISSION draft,
     never a one-off task — the intake drafts it (planner
     first, rules on failure, same as POST /missions/intake)
     and the reply carries the draft for the UI's draft card.
     Nothing is created until the user confirms the card.
     Without continuous language ("find me a job") the message
     stays a task.
  4. Everything else goes to the planner when one is configured
     (last 6 chat messages + recalled memory facts in the prompt,
     PII-redacted first — the loop.plan_task pattern). The planner
     is the conversational fallback: it answers statements and
     questions naturally, with history. Any planner failure
     or unusable output falls back to the rules, labelled
     source "rules" — the same planner-or-labelled-rules pattern
     as the mission intake. Three guards: the planner may only
     upgrade a rules-ambiguous fragment to a task when the
     conversation gives it something to resolve against (with no
     history the rules win); a planner failure never 500s a
     greeting (step 3 already answered those); and the rules
     clarifier is the LAST resort — it is never asked twice in a
     row (the last clarifier is tracked from the conversation,
     and a repeat becomes an honest no-model reply instead).
     The planner prompt bakes in: assume defaults and start, at
     most ONE clarifying question, only when genuinely blocked,
     a reply of only questions is a failure.
  5. A task is created through the SAME path as POST /tasks
     (loop.create_task — same validation, same gates), with the
     user's message as the goal. The created task is then driven
     by the existing loop + extension like any other task.

Storage: one chat_messages row per message, user and agent alike
(the thread IS the task conversation — no second memory store).
Facts live in the existing memory/fact store (keyword.py) — the
chat brain invents no second store.
"""
from __future__ import annotations

import logging
import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..chat import classify
from ..core import db
from ..core.auth import require_token
from ..memory import budgets, keyword
from ..missions import intake as intake_mod
from ..planner import router as planner_router
from ..planner import scan
from ..tasks import loop
from . import tasks_api

logger = logging.getLogger("eclipse.chat")

router = APIRouter(tags=["chat"])

MAX_MESSAGE_LEN = classify.MAX_MESSAGE_LEN
# Chat messages in the planner prompt (the brief's "last 6").
HISTORY_CONTEXT = 6

# Fact topics for chat-captured facts (the existing fact store,
# keyword.py — no second store). The profile name lives in the
# "profile" scope, so it NEVER expires (budgets.py rule);
# "remember ..." facts live in the "global" scope and age out
# after the 90-day TTL via expire_stale_facts.
PROFILE_NAME_TOPIC = "profile:name"
REMEMBERED_TOPIC_PREFIX = "remembered:"
REMEMBERED_FREE_TOPIC = "remembered"
# The profile name's fact text (the model reads it in
# REMEMBERED FACTS, so it is a sentence, not a bare name).
_NAME_FACT_RE = re.compile(r"^The user's name is (.+)\.$")

# Refusal: a "remember ..." whose key or value names a
# credential is never stored. Structured PII (email, phone,
# card, Aadhaar, PAN, dob) is caught separately by scan.scan.
_CREDENTIAL_WORDS = re.compile(
    r"\b(password|passwd|pwd|otp|one-time passcode|cvv|cvc|pin"
    r"|secret|api[- ]?key|apikey|access[- ]?token|private[- ]?key"
    r"|auth[- ]?token|recovery[- ]?code)\b",
    re.IGNORECASE,
)
REMEMBER_REFUSAL = (
    "I won't store that — it looks like private details (a"
    " password, a code, or personal ID), and I never store"
    " those. Tell me something else to remember and I'll keep it."
)


class MessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)


def _conn():
    return db.connect()


def _store(conn, role: str, text: str, kind: str,
           task_id: str | None, source: str | None,
           provider: str | None = None) -> str:
    """One chat_messages row. Returns its id. `provider` names
    the model that answered (planner rows only)."""
    message_id = f"msg-{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO chat_messages (id, role, text, kind, task_id,"
        " source, provider, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (message_id, role, text, kind, task_id, source, provider,
         int(time.time())),
    )
    conn.commit()
    return message_id


def _answer(conn, text: str, reply_text: str, kind: str,
            task_id: str | None, source: str,
            provider: str | None = None,
            draft: dict | None = None) -> dict:
    """Store the user message AND the agent reply, and build the
    POST /chat/message response. The message_id is the agent
    reply's row (the message the UI just received). `draft` is
    the mission-intake result, only for mission requests — the
    key is omitted otherwise, so existing replies are
    byte-identical."""
    _store(conn, "user", text, "chat", None, None)
    message_id = _store(conn, "agent", reply_text, kind, task_id, source,
                        provider)
    response = {
        "message_id": message_id, "reply_text": reply_text,
        "kind": kind, "task_id": task_id, "source": source,
        "provider": provider,
    }
    if draft is not None:
        response["draft"] = draft
    return response


def _provider_from_model(model: str | None) -> str | None:
    """The provider name from a planner model label
    ("ollama:qwen2.5:7b" -> "ollama"). None when the label is
    missing or empty — a reply is never labelled with a guess."""
    if not model:
        return None
    name = model.split(":", 1)[0].strip()
    return name or None


def _last_agent_text(history: list[dict]) -> str | None:
    """The most recent agent message in the history (oldest
    first), or None. The no-repeat guard compares the rules
    clarifier against this: the same question twice in a row
    is a failure."""
    for message in reversed(history):
        if message["role"] == "agent":
            return message["text"]
    return None


# ----------------------------------------------------------------
# Chat-captured facts (profile name + "remember ...") — all in
# the existing fact store (keyword.py). No second store.
# ----------------------------------------------------------------

def _name_fact_text(name: str) -> str:
    return f"The user's name is {name}."


def _stored_profile_name(conn) -> str | None:
    """The stored profile name, or None when the user has not
    told us their name yet."""
    row = conn.execute(
        "SELECT text FROM facts WHERE scope = ? AND topic = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (keyword.PROFILE_SCOPE, PROFILE_NAME_TOPIC),
    ).fetchone()
    if row is None:
        return None
    match = _NAME_FACT_RE.match(row["text"])
    return match.group(1) if match else None


def _store_profile_name(conn, name: str) -> None:
    """Store the profile name, overwriting any older one: a
    correction replaces the fact, it never sits beside it.
    Scope "profile" means it never expires (budgets.py rule)."""
    conn.execute(
        "DELETE FROM facts WHERE scope = ? AND topic = ?",
        (keyword.PROFILE_SCOPE, PROFILE_NAME_TOPIC),
    )
    conn.commit()
    keyword.store_fact(
        conn, _name_fact_text(name), source="chat",
        topic=PROFILE_NAME_TOPIC, scope=keyword.PROFILE_SCOPE,
    )


def _remembered_topic(key: str | None) -> str:
    return f"{REMEMBERED_TOPIC_PREFIX}{key}" if key else REMEMBERED_FREE_TOPIC


def _stored_remembered_fact(conn, key: str | None) -> str | None:
    """The stored fact text for one remember key, or None."""
    row = conn.execute(
        "SELECT text FROM facts WHERE scope = ? AND topic = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (keyword.GLOBAL_SCOPE, _remembered_topic(key)),
    ).fetchone()
    return row["text"] if row else None


def _store_remembered_fact(conn, key: str | None, fact_text: str) -> None:
    """Store a "remember ..." fact, overwriting the old value
    for the same key. Scope "global" + a non-profile topic, so
    the 90-day TTL applies (expire_stale_facts)."""
    conn.execute(
        "DELETE FROM facts WHERE scope = ? AND topic = ?",
        (keyword.GLOBAL_SCOPE, _remembered_topic(key)),
    )
    conn.commit()
    keyword.store_fact(
        conn, fact_text, source="chat",
        topic=_remembered_topic(key), scope=keyword.GLOBAL_SCOPE,
    )


def _remember_refusal(key: str | None, value: str) -> bool:
    """True when a remember value must NOT be stored: it names
    a credential, or it contains structured PII (email, phone,
    card, Aadhaar, PAN, date of birth)."""
    if _CREDENTIAL_WORDS.search(f"{key or ''} {value}"):
        return True
    return bool(scan.scan(value))


def _recent_history(conn, limit: int = HISTORY_CONTEXT) -> list[dict]:
    """The last `limit` chat messages, oldest first, for the
    planner prompt. Ordered by (created_at, rowid): two messages
    stored in the same second still come out in insertion order."""
    rows = conn.execute(
        "SELECT role, text FROM chat_messages"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [{"role": r["role"], "text": r["text"]} for r in reversed(rows)]


def _memory_facts(conn, text: str) -> list[str]:
    # Mirror loop._memory_facts: recall() returns list[dict], the
    # prompt builders join strings (Bug 1: passing the dicts
    # through crashed plan_task with a 502).
    return [f["text"] for f in keyword.recall(conn, text, limit=10)]


def _active_task(conn) -> dict | None:
    """The most recent task linked to this conversation that is
    still active (not cancelled / failed / done). The terminal
    set comes from tasks_api, so the two cannot drift apart."""
    placeholders = ", ".join("?" * len(tasks_api.TERMINAL_TASK_STATUSES))
    row = conn.execute(
        "SELECT t.* FROM chat_messages m JOIN tasks t ON t.id = m.task_id"
        f" WHERE t.status NOT IN ({placeholders})"
        " ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1",
        tasks_api.TERMINAL_TASK_STATUSES,
    ).fetchone()
    return dict(row) if row else None


def _append_refinement(conn, task_id: str, text: str) -> None:
    """Append a mid-task refinement to the task's stored
    constraints. The loop re-reads the task row on every step
    (execute_step puts CONSTRAINTS in each step's prompt), so a
    refinement shapes every step still to run; a task still in
    "created" gets it in its plan too. The plan's existing step
    list does not change — the reply says so honestly."""
    task = loop.get_task(conn, task_id)
    old = (task.get("constraints") or "") if task else ""
    new = (old + "\n" if old else "") + f"Refinement: {text}"
    conn.execute(
        "UPDATE tasks SET constraints = ?, updated_at = ? WHERE id = ?",
        (new[-2000:], int(time.time()), task_id),
    )
    conn.commit()


def _capitalised(label: str) -> str:
    return label[0].upper() + label[1:] if label else label


def respond(conn, text: str, call_fn=None) -> dict:
    """Answer one chat message (see the module docstring for the
    decision order). `call_fn` is the injectable planner call —
    router.call_planner by default, a fake in tests. Raises
    ValueError only for an empty or oversized message (the
    endpoint maps it to 422); a planner failure never raises."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must not be empty")
    if len(text) > MAX_MESSAGE_LEN:
        raise ValueError(
            f"text must be at most {MAX_MESSAGE_LEN} characters"
        )
    text = text.strip()
    history = _recent_history(conn)
    active = _active_task(conn)
    # TTL maintenance: "remember ..." facts age out after 90
    # days (budgets.expire_stale_facts, ttl_days=90). The
    # mission runner only expires mission scopes, so chat
    # maintains the global scope here — one cheap DELETE per
    # message, and it never fails a reply.
    try:
        budgets.expire_stale_facts(conn, mission_id=keyword.GLOBAL_SCOPE)
    except Exception as exc:  # noqa: BLE001 - fact maintenance never fails a reply
        logger.warning("chat fact expiry failed: %s", exc)

    # 1. Steering: an active task is steered before anything else.
    steering = classify.classify_steering(text)
    if active is not None and steering in ("cancel", "pause"):
        label = classify.task_label(active["goal"])
        try:
            if steering == "cancel":
                # The EXISTING cancel path (same as POST
                # /tasks/{id}/cancel): terminal-state guard +
                # role claims released.
                tasks_api.cancel_task(active["id"])
                reply = f"Stopped {label}."
            else:
                tasks_api.pause_task(active["id"])
                reply = (
                    f"Paused {label} — resume it from the task"
                    " view whenever you're ready."
                )
        except HTTPException:
            # Raced with the task finishing: say so honestly.
            verb = "stop" if steering == "cancel" else "pause"
            reply = (
                f"{_capitalised(label)} already finished, so there"
                f" was nothing to {verb}."
            )
        return _answer(conn, text, reply, "chat", active["id"], "rules")
    if active is not None and steering == "refine":
        phrase = classify.refinement_phrase(text)
        _append_refinement(conn, active["id"], text)
        if active["status"] == "created":
            reply = (
                f"Noted — {phrase}. I've added it to the task,"
                " so the plan will follow it."
            )
        else:
            reply = (
                f"Noted — {phrase}. I've added it to the task's"
                " rules, so the steps still to run follow it."
            )
        return _answer(conn, text, reply, "chat", active["id"], "rules")
    if active is None and steering in ("cancel", "pause"):
        verb = "stop" if steering == "cancel" else "pause"
        return _answer(
            conn, text,
            f"There's no task running right now — nothing to {verb}.",
            "chat", None, "rules",
        )

    # 2. Identity + "remember ...": stored as facts, confirmed
    # in the reply — rules only, never the planner, never a task.
    decision = classify.classify_rules(text)
    if decision["reason"] in ("identity", "correction"):
        name = decision["identity"]["name"]
        stored = _stored_profile_name(conn)
        if stored == name:
            reply = (
                f"You already told me — you're {name}."
                " I've still got that saved."
            )
        elif stored is not None:
            # A correction (or a plain re-statement with a new
            # name): overwrite the stored fact, never re-ask.
            _store_profile_name(conn, name)
            reply = f"Got it — I'll call you {name} from now on."
        else:
            _store_profile_name(conn, name)
            reply = decision["reply_text"]
        return _answer(conn, text, reply, "chat", None, "rules")
    if decision["reason"] == "remember":
        payload = decision["remember"]
        if _remember_refusal(payload["key"], payload["value"]):
            return _answer(conn, text, REMEMBER_REFUSAL, "chat", None, "rules")
        if payload["key"] == "name":
            # "remember my name is X" is a profile fact, like
            # "i'm X" — same store, same overwrite rule.
            stored = _stored_profile_name(conn)
            name = payload["value"]
            if stored == name:
                reply = (
                    f"You already told me — you're {name}."
                    " I've still got that saved."
                )
            elif stored is not None:
                _store_profile_name(conn, name)
                reply = f"Got it — I'll call you {name} from now on."
            else:
                _store_profile_name(conn, name)
                reply = decision["reply_text"]
            return _answer(conn, text, reply, "chat", None, "rules")
        previous = _stored_remembered_fact(conn, payload["key"])
        if previous == payload["fact_text"]:
            reply = (
                "You've already told me that — I've still got it"
                " saved."
            )
        else:
            _store_remembered_fact(conn, payload["key"], payload["fact_text"])
            if previous is not None and payload["key"]:
                reply = (
                    f"Got it — I've updated that: your {payload['key']}"
                    f" is now {payload['value']}."
                )
            else:
                reply = decision["reply_text"]
        return _answer(conn, text, reply, "chat", None, "rules")

    # 3. Small talk: rules answer at once, never the planner.
    if decision["hard_chat"]:
        return _answer(
            conn, text, decision["reply_text"], "chat", None, "rules"
        )

    # 3b. Standing missions: a message asking for continuous
    # watching/applying is a mission draft, never a one-off
    # task — without this, "watch my inbox for interview mails"
    # became a dead task sitting in `created` (found in live
    # screenshot verification, 2026-11-23). The intake drafts
    # it (planner first, rules on failure — parse_intake's own
    # pattern, same as POST /missions/intake) and the reply
    # carries the draft for the UI's draft card. Nothing is
    # created until the user confirms the card. The reply text
    # is the draft card, plus the one clarifying question when
    # the intake is blocked on something answerable in chat.
    if intake_mod.is_mission_request(text):
        result = intake_mod.parse_intake(text, call_fn=call_fn, conn=conn)
        reply_text = result["draft_card"]
        if result.get("clarifying_question"):
            reply_text += "\n\n" + result["clarifying_question"]
        return _answer(conn, text, reply_text, "chat", None,
                       result.get("source", "rules"), draft=result)

    # 4. Planner first (when one is configured), rules on any
    # failure — the mission-intake pattern, labelled. The planner
    # is the conversational fallback: non-task messages go to a
    # model WITH history (last 6 messages + recalled facts),
    # PII-redacted first.
    call_fn = call_fn or planner_router.call_planner
    try:
        facts = _memory_facts(conn, text)
        prompt = classify.build_chat_prompt(text, history, facts)
        # Second PII scan before the outbound call (§6.4), the
        # loop.plan_task pattern.
        prompt, _hits = scan.redact(prompt)
        raw, model, _ms = call_fn(prompt)
        provider = _provider_from_model(model)
        parsed = classify.parse_planner_reply(raw)
        if (
            parsed["kind"] == "task_created"
            and decision["reason"] == "ambiguous"
            and not history
        ):
            # The planner upgraded a context-free fragment to a
            # task — "internships" with no conversation behind it
            # is not an instruction. The rules win; with history
            # (a reply to a clarifying question) the planner's
            # resolution stands.
            raise ValueError(
                "planner upgraded a context-free fragment to a task"
            )
        kind = parsed["kind"]
        reply_text = parsed["reply_text"]
        constraints = parsed.get("constraints") or ""
        source = "planner"
    except Exception as exc:  # noqa: BLE001 - any planner failure falls back to rules
        logger.info("chat planner path failed (%s); using rules", exc)
        kind = decision["kind"]
        reply_text = decision["reply_text"]
        constraints = ""
        source = "rules"
        provider = None
        if decision["reason"] == "ambiguous":
            # The rules clarifier is the LAST resort — and it is
            # never asked twice in a row. "i'm Akash" got the same
            # question twice in the live test; when the clarifier
            # we would ask IS the last thing we asked, say so
            # honestly instead of re-asking.
            if _last_agent_text(history) == reply_text:
                reply_text = classify.REPEATED_QUESTION_REPLY
        # reason "statement" needs no special case: the rules
        # reply for a statement IS the honest no-model message
        # (NO_MODEL_REPLY), never a clarifying question.

    # 5. Task creation goes through the SAME path as POST /tasks.
    task_id = None
    if kind == "task_created":
        task_id = loop.create_task(conn, text, constraints)
    return _answer(conn, text, reply_text, kind, task_id, source, provider)


@router.post("/chat/message")
def post_message(body: MessageIn, _token: str = Depends(require_token)):
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="text must not be blank")
    conn = _conn()
    try:
        try:
            return respond(conn, body.text)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()


@router.get("/chat/history")
def get_history(limit: int = Query(default=50, ge=1, le=200),
                _token: str = Depends(require_token)):
    """The thread, oldest first. `limit` keeps the most recent N
    messages (a long thread returns its tail, not its head)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, role, text, kind, task_id, source, provider,"
            " created_at FROM chat_messages"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    messages = [dict(r) for r in reversed(rows)]
    return {"messages": messages}
