"""Task loop (Sprint 3, plan §6.2; fix pass 2026-11-21): create →
plan → per-step observe → plan one action → validate → execute
(guarded) → verify by re-observing → checkpoint.

The server drives the loop; the extension is the hands (it walks the
page, performs the validated action, and re-observes). Each HTTP
call is one loop turn:
  POST /tasks/{id}/plan            → decompose the goal
  POST /tasks/{id}/steps/{n}/execute (with the redacted observation)
                                   → returns one validated action
  POST /tasks/{id}/steps/{n}/verify (with the new observation AND
    the pre-action observation for a baseline diff)
                                   → verified or retry/fail

Limits (§11): max 25 steps/task, max 3 retries/step (backoff
1s/2s/4s — the caller sleeps; the server records the retry count),
max 3 planner calls/step. Never retry a submit/charge/send: the
server refuses to re-execute a step that is already done, and
refuses a click on an element that already has a done click step
in the same task (the extension's in-memory guard is the second
layer, not the only one).
End states: done (verified), ask_user, handoff (human-only),
refuse (policy), failed (limits hit, with reason + checkpoint).

Privacy (§6.2 step 3, §6.4): before ANY outbound planner call, the
assembled prompt is rescanned with the server-side second PII scan
(server/planner/scan.py). A hit is re-redacted and logged; two
blocks on the same step stop the task.

Verify (§6.2: "action ran is not verification"): every check is a
BASELINE DIFF against the pre-action observation. Pre-existing text,
pre-existing elements and pre-filled field values never verify a
step. A step is done only when the end state is newly true.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid

from ..core import audit, db
from ..memory import keyword
from ..planner import context as ctx
from ..planner import deterministic as det
from ..planner import prompts, router, scan, validate

logger = logging.getLogger("eclipse.tasks")

MAX_STEPS_PER_TASK = 25
MAX_RETRIES_PER_STEP = 3
MAX_PLANNER_CALLS_PER_STEP = 3
RETRY_BACKOFF_S = (1, 2, 4)
HANDOFF_TIMEOUT_S = 600  # plan §5.5 step 4: never wait forever
ROLE_CLAIM_TTL_S = 600  # a role claim lives 10 minutes (the approval window)
PENDING_APPROVAL_TTL_S = 600  # a pending approval lives 10 minutes (the approval window)

# A step whose description mentions one of these is human-only.
HUMAN_ONLY_MARKERS = (
    "captcha",
    "3-d secure",
    "3d secure",
    "otp",
    "passkey",
    "verification code",
    "2fa",
    "two-factor",
    "security code",
    "authenticator",
)


class GroundingError(validate.ValidationError):
    """The planner named an element that is not in the current
    observation. The caller must re-observe once and re-plan (§6.3);
    re-planning against the same stale observation is not allowed.
    """


def create_task(conn, goal: str, constraints: str = "") -> str:
    task_id = uuid.uuid4().hex[:12]
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, goal, constraints, status, created_at, updated_at)"
        " VALUES (?, ?, ?, 'created', ?, ?)",
        (task_id, goal, constraints, now, now),
    )
    conn.commit()
    return task_id


def get_task(conn, task_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def _set_task_status(conn, task_id: str, status: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (status, int(time.time()), task_id),
    )
    conn.commit()


def _memory_facts(conn, goal: str) -> list[str]:
    # recall() returns list[dict] (id/text/source/topic/score) —
    # the prompt builders join strings, so extract the text here
    # (Bug 1: passing the dicts through crashed plan_task with
    # "TypeError: sequence item 0: expected str instance, dict
    # found" — a 502 — whenever a stored fact matched the goal).
    return [f["text"] for f in keyword.recall(conn, goal, limit=10)]


def _is_local_only(constraints: str) -> bool:
    """A task whose constraints say "local only" never touches the
    cloud planner (plan §9: never silently fall back from local
    mode to cloud)."""
    text = (constraints or "").lower()
    return "local only" in text or "local-only" in text


def _audit(conn, task_id: str, action: str, payload: dict) -> None:
    """Append a redacted audit entry (payloads carry ids and
    results only — never raw PII or prompt text)."""
    audit.append(conn, task_id, action, payload)


def _insert_plan(conn, task_id: str, steps: list[dict], model: str) -> list[dict]:
    """Store a plan's steps and mark the task planned (shared by
    the deterministic path and the planner path — one insert,
    one audit entry, no drift)."""
    now = int(time.time())
    for i, step in enumerate(steps, start=1):
        conn.execute(
            "INSERT INTO steps (id, task_id, n, action_json, status,"
            " started_at, ended_at, ms, element_id, result, error, retries,"
            " scan_blocks, grounding_failures)"
            " VALUES (?, ?, ?, ?, 'pending', ?, NULL, NULL, NULL, NULL, NULL, 0, 0, 0)",
            (
                uuid.uuid4().hex[:12],
                task_id,
                i,
                json.dumps(step),
                now,
            ),
        )
    _set_task_status(conn, task_id, "planned")
    _audit(conn, task_id, "task.plan", {"steps": len(steps), "model": model})
    return steps


def plan_task(conn, task_id: str, call_fn=None) -> list[dict]:
    """Decompose the goal into steps (one planner call — unless
    the goal is a pure navigation, which plans deterministically
    with no model at all; see server/planner/deterministic.py)."""
    task = get_task(conn, task_id)
    if task is None:
        raise KeyError(f"no task {task_id}")
    # Planning is one-shot: a second plan call would INSERT a
    # duplicate set of steps (same n) and resurrect a finished
    # task. A plan that failed part-way leaves the task "created",
    # so re-planning after a failure still works.
    if task["status"] != "created":
        raise ValueError(
            f"task {task_id} is {task['status']}; refusing to re-plan"
        )
    # PR #7 (W3): a pure navigation goal ("open google") plans
    # WITHOUT any planner call — this runs BEFORE the prompt is
    # built, so a deterministic goal never touches the planner,
    # its quota, or the network. The resolver enforces the
    # task's allow-list itself; a goal it cannot resolve (or
    # that the allow-list rejects) falls through to the planner
    # path below unchanged.
    det_steps = det.deterministic_plan(
        task["goal"], allowed_sites=_allowed_sites(task["constraints"])
    )
    if det_steps is not None:
        logger.info("task %s planned deterministically (navigate)", task_id)
        return _insert_plan(conn, task_id, det_steps, "deterministic")
    call_fn = call_fn or router.call_planner
    facts = _memory_facts(conn, task["goal"])
    prompt = prompts.build_plan_prompt(task["goal"], task["constraints"], facts)
    # Second PII scan before the outbound call (§6.4).
    prompt, hits = scan.redact(prompt)
    if hits:
        logger.warning("task %s plan prompt had %d PII hits (re-redacted)", task_id, len(hits))
    raw, model, ms = call_fn(prompt, local_only=_is_local_only(task["constraints"]))
    logger.info("task %s planned via %s in %.0f ms", task_id, model, ms)
    steps = validate.parse_plan(raw)
    return _insert_plan(conn, task_id, steps, model)


def _get_step(conn, task_id: str, n: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM steps WHERE task_id = ? AND n = ?", (task_id, n)
    ).fetchone()
    return dict(row) if row else None


def checkpoint_summary(conn, task_id: str) -> str:
    row = conn.execute(
        "SELECT summary FROM checkpoints WHERE task_id = ?"
        " ORDER BY step_n DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["summary"] if row else ""


def last_results(conn, task_id: str, limit: int = 3) -> list[str]:
    rows = conn.execute(
        "SELECT n, result FROM steps WHERE task_id = ? AND status = 'done'"
        " ORDER BY n DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    return [f"step {r['n']}: {r['result']}" for r in reversed(rows)]


def write_checkpoint(conn, task_id: str, step_n: int, summary: str, state: dict) -> None:
    """After each verified step: a 10-line summary + the state JSON.

    Resume reads the latest checkpoint, never the full history (§6.5).
    """
    lines = [ln.strip() for ln in summary.strip().splitlines() if ln.strip()][:10]
    conn.execute(
        "INSERT INTO checkpoints (id, task_id, step_n, summary, state_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            uuid.uuid4().hex[:12],
            task_id,
            step_n,
            "\n".join(lines),
            json.dumps(state),
            int(time.time()),
        ),
    )
    conn.commit()


def _allowed_sites(constraints: str) -> list[str] | None:
    """Parse an "allowed sites: a.com, b.com" line from constraints."""
    for line in (constraints or "").splitlines():
        if line.strip().lower().startswith("allowed sites:"):
            sites = line.split(":", 1)[1]
            return [s.strip() for s in sites.split(",") if s.strip()]
    return None


def _origin_of(url: str) -> str:
    """The origin (scheme + host) of a URL — the vault keys
    stored passwords by origin."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.hostname}" if parsed.hostname else ""
    except Exception:  # noqa: BLE001 - a bad URL is not an origin
        return ""


def _strip_url(url: str) -> str:
    """Origin + pathname only (no query string, no fragment).
    The same rule the executor's safeUrl applies — used for
    verify reasons and receipt confirmations, which persist."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.hostname:
            return f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
        return url
    except Exception:  # noqa: BLE001 - a bad URL is returned as-is
        return url


def _is_human_only_description(description: str) -> bool:
    lowered = (description or "").lower()
    return any(m in lowered for m in HUMAN_ONLY_MARKERS)


def _role_from_constraints(constraints: str) -> dict:
    """The apply flow carries the role in the task constraints
    ("role: role_1", "url: http://…"). Without it the loop has no
    role context and cannot check "already applied" (§6.9)."""
    role_id = url = None
    for line in (constraints or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("role:"):
            role_id = stripped.split(":", 1)[1].strip()
        elif stripped.lower().startswith("url:"):
            url = stripped.split(":", 1)[1].strip()
    return {"role_id": role_id, "url": url}


def _is_consequential(action: dict, elements: list[dict]) -> bool:
    """Is this action consequential (a real submit / send / pay)?
    The server now sees the element's type and whether it sits in
    a form (ElementInfo carries both) — the precise signal, not
    just the name. "Apply now" on a listing is NOT consequential:
    it only opens the form. A button inside a form IS (a
    multi-step "Continue" submits the form), as is any button
    whose type is submit, or whose name says submit / confirm /
    pay / send. This matches the executor's guard, so one
    application costs one approval, not two."""
    if action.get("action") != "click":
        return False
    target = next(
        (e for e in elements if e.get("id") == action.get("element_id")), None
    )
    if target is None:
        return False
    el_type = str(target.get("type") or "").lower()
    if el_type in ("submit", "image"):
        return True
    if target.get("in_form"):
        return True
    text = f"{target.get('name','')} {target.get('text','')}".lower()
    return any(w in text for w in ("submit", "confirm", "place order", "pay", "send"))


# Element labels that mark a pagination / list-navigation control:
# these are DESIGNED to be clicked repeatedly (page 2, page 3, …),
# so the cross-step repeat guard must not block them. Anything else
# the planner re-clicks across steps is a stall, not pagination.
_PAGINATION_LABELS = (
    "next", "prev", "previous", "load more", "show more",
    "more", "more results", "see more",
)


def _is_pagination_control(action: dict, elements: list[dict]) -> bool:
    """Is this click on a pagination control (a "Next" button, a
    page-number link)? The label is the element's name or text,
    normalised; a bare number is a page link."""
    if action.get("action") != "click":
        return False
    target = next(
        (e for e in elements if e.get("id") == action.get("element_id")), None
    )
    if target is None:
        return False
    # Either the name or the text may carry the label (an element
    # named "Next" with text "Next" must not become "next next").
    for raw in (target.get("name") or "", target.get("text") or ""):
        label = raw.strip().lower().strip("→›»«‹← ").strip()
        label = " ".join(label.split())
        if label in _PAGINATION_LABELS or (label and label.isdigit()):
            return True
    return False


# Consequential goal verbs (§5.4): purchases, sends and
# applications all need a fresh approval. "Log in to the test
# portal" has none of these, so the login-fixture flow stays
# approval-free.
_CONSEQUENTIAL_GOAL_WORDS = (
    "apply", "buy", "purchase", "pay", "send", "mail", "email",
    "transfer", "checkout", "order", "subscribe", "book",
)


def _requires_approval(task: dict) -> bool:
    """A task requires approval before a consequential action when
    its goal names one (buy, pay, send, apply, …) or its
    constraints say so."""
    goal = (task.get("goal") or "").lower()
    constraints = (task.get("constraints") or "").lower()
    return (
        any(re.search(rf"\b{w}\b", goal) for w in _CONSEQUENTIAL_GOAL_WORDS)
        or "approval" in constraints
    )


def _consume_approval(conn, task_id: str, element_id: str | None = None,
                      role_id: str | None = None) -> bool:
    """Consume one valid approval grant for this task (a grant is
    single-use; purchases never get a lasting allow, §5.4).

    Single-use: the grant is picked by SELECT and spent by a
    DELETE-by-id whose rowcount must be 1, so two concurrent
    submits cannot spend the same grant (the loser's DELETE
    affects 0 rows). Action-scoped: a grant whose detail names an
    element or role only covers that element or role — an approval
    for role A cannot be consumed by a submit for role B. A grant
    that names neither covers any action on the task ("step" and
    "approval_id" in the detail are metadata, not scope).
    """
    now = int(time.time())
    # Pick one matching grant, preferring the most specific one.
    rows = conn.execute(
        "SELECT id, detail FROM grants WHERE origin = ? AND scope = 'approve'"
        " AND expires_at > ? ORDER BY created_at ASC",
        (task_id, now),
    ).fetchall()
    chosen = None
    for row in rows:
        detail = {}
        if row["detail"]:
            try:
                detail = json.loads(row["detail"])
            except (ValueError, TypeError):
                detail = {}
        scoped_element = detail.get("element_id")
        scoped_role = detail.get("role_id")
        if not scoped_element and not scoped_role:
            chosen = row["id"]
            break
        if element_id and scoped_element == element_id:
            chosen = row["id"]
            break
        if role_id and scoped_role == role_id:
            chosen = row["id"]
            break
    if chosen is None:
        return False
    cur = conn.execute("DELETE FROM grants WHERE id = ?", (chosen,))
    conn.commit()
    return cur.rowcount == 1


def record_pending_approval(
    conn, task_id: str, step: int, action: str,
    element_id: str | None = None, role_id: str | None = None,
    ttl_s: int = PENDING_APPROVAL_TTL_S,
) -> str:
    """Record a pending approval for a step that reached the human
    gate (Bug 3 fix: the gate used to exist only in the UI — any
    made-up approval_id minted a real grant). Returns the
    approval_id the client must POST to /tasks/{id}/approve.

    Idempotent per gate: a live pending approval for the same
    (task, step, action) is reused, so re-executing a gated step
    does not mint a second approval id for the same gate.
    """
    now = int(time.time())
    existing = conn.execute(
        "SELECT id FROM pending_approvals WHERE task_id = ? AND step = ?"
        " AND action = ? AND expires_at > ? ORDER BY created_at ASC LIMIT 1",
        (task_id, step, action, now),
    ).fetchone()
    if existing is not None:
        return existing["id"]
    approval_id = f"appr-{uuid.uuid4().hex[:12]}"
    detail = {
        k: v for k, v in {
            "element_id": element_id, "role_id": role_id,
        }.items() if v is not None
    }
    conn.execute(
        "INSERT INTO pending_approvals (id, task_id, step, action, detail,"
        " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (approval_id, task_id, step, action, json.dumps(detail),
         now, now + ttl_s),
    )
    conn.commit()
    return approval_id


def get_pending_approval(conn, approval_id: str) -> dict | None:
    """The pending approval with this id, or None (unknown, or
    already consumed)."""
    row = conn.execute(
        "SELECT * FROM pending_approvals WHERE id = ?", (approval_id,)
    ).fetchone()
    return dict(row) if row else None


def consume_pending_approval(conn, approval_id: str) -> bool:
    """Consume (delete) a pending approval. Returns False when no
    such approval exists — a replay finds nothing to consume."""
    cur = conn.execute(
        "DELETE FROM pending_approvals WHERE id = ?", (approval_id,))
    conn.commit()
    return cur.rowcount == 1


def _has_live_claim(conn, task_id: str, role_key: str, url: str | None) -> bool:
    """Does ANOTHER task hold a live claim on this role (an
    in-flight submit)? A claim is a receipts row with status
    'claimed', recorded at execute time (the receipt itself only
    lands at verify time — without the claim, two tasks for the
    same role both pass the already-applied gate and both submit).
    Claims older than the TTL are stale (the task crashed, or its
    submit failed) and no longer block. My own task's claim never
    blocks me (a verify-failed retry re-executes the step)."""
    now = int(time.time())
    rows = conn.execute(
        "SELECT task_id FROM receipts WHERE status = 'claimed'"
        " AND role_id = ? AND created_at > ?",
        (role_key, now - ROLE_CLAIM_TTL_S),
    ).fetchall()
    if url:
        rows += conn.execute(
            "SELECT task_id FROM receipts WHERE status = 'claimed'"
            " AND url = ? AND created_at > ?",
            (url, now - ROLE_CLAIM_TTL_S),
        ).fetchall()
    return any(r["task_id"] != task_id for r in rows)


def _claim_role(conn, task_id: str, role_key: str, url: str | None) -> str | None:
    """Claim a role for this task's in-flight submit (§6.9 race).
    Returns the claim id, or None when another task already holds
    a live claim. The check and the insert run under BEGIN
    IMMEDIATE (when the caller holds no transaction), so two
    tasks racing for the same role serialize: the loser sees the
    winner's claim instead of writing its own. My own claim (any
    age) is refreshed, not duplicated — a verify-failed retry
    re-executes the step, and the (task, role) unique index would
    reject a second claim row."""
    began = False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        began = True
    try:
        own = conn.execute(
            "SELECT id FROM receipts WHERE task_id = ? AND role_id = ?"
            " AND status = 'claimed'",
            (task_id, role_key),
        ).fetchone()
        if own is not None:
            conn.execute(
                "UPDATE receipts SET created_at = ? WHERE id = ?",
                (int(time.time()), own["id"]),
            )
            conn.commit()
            return own["id"]
        if _has_live_claim(conn, task_id, role_key, url):
            if began:
                conn.rollback()
            return None
        claim_id = uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO receipts (id, task_id, role_id, title, company, url,"
            " status, confirmation, created_at) VALUES (?, ?, ?, '', '', ?, 'claimed', '', ?)",
            (claim_id, task_id, role_key, url or "", int(time.time())),
        )
        conn.commit()
        return claim_id
    except Exception:
        if began:
            conn.rollback()
        raise


def _release_claim(conn, claim_id: str) -> None:
    """Release one role claim (approval failed after claiming, so
    this task cannot submit — it must not hold the role)."""
    conn.execute(
        "DELETE FROM receipts WHERE id = ? AND status = 'claimed'",
        (claim_id,),
    )
    conn.commit()


def _release_claim_for_task(conn, task_id: str, role_key: str) -> None:
    """Release this task's claim on one role (its submit step
    failed verification, so the submit never landed — the role
    must be retryable at once, not after the claim TTL)."""
    conn.execute(
        "DELETE FROM receipts WHERE task_id = ? AND role_id = ? AND status = 'claimed'",
        (task_id, role_key),
    )
    conn.commit()


def release_claims_for_task(conn, task_id: str) -> int:
    """Release every live role claim this task holds (a cancelled
    task will never submit, so it must not hold its roles for the
    rest of the claim TTL). Returns the number released."""
    cur = conn.execute(
        "DELETE FROM receipts WHERE task_id = ? AND status = 'claimed'",
        (task_id,),
    )
    conn.commit()
    return cur.rowcount


def _settle_receipt(conn, task_id: str, role_key: str, title: str,
                    url: str, confirmation: str) -> None:
    """Settle the receipt for a verified submit (§6.9 race):
    promote this task's execute-time claim to submitted, or — if
    another task already submitted this role (my claim expired
    while the submit was in flight) — drop the stale claim and
    record nothing (one submitted receipt per role, ever). With
    no claim on file (an execute from before claims existed),
    record a fresh receipt. The check + write run under BEGIN
    IMMEDIATE, so two tasks verifying the same role at once
    cannot both land a submitted receipt; the fresh-record path
    relies on record_receipt's own per-role idempotency."""
    from ..jobs import applied as _applied
    from ..jobs import receipts as _receipts

    began = False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        began = True
    try:
        if _applied.is_already_applied(conn, role_key, url or None):
            conn.execute(
                "DELETE FROM receipts WHERE task_id = ? AND role_id = ?"
                " AND status = 'claimed'",
                (task_id, role_key),
            )
            conn.commit()
            return
        cur = conn.execute(
            "UPDATE receipts SET title = ?, url = ?, status = 'submitted',"
            " confirmation = ?, created_at = ?"
            " WHERE task_id = ? AND role_id = ? AND status = 'claimed'",
            (title, url, confirmation, int(time.time()), task_id, role_key),
        )
        if cur.rowcount == 1:
            conn.commit()
            return
        conn.commit()
    except Exception:
        if began:
            conn.rollback()
        raise
    # No claim to promote: a fresh receipt. record_receipt is
    # itself idempotent per role, so a submit that landed between
    # the check above and this insert still yields one receipt.
    _receipts.record_receipt(
        conn, task_id, role_key, title, "", url, confirmation,
        status="submitted",
    )


def _handoff(conn, task_id: str, n: int, reason: str) -> None:
    """Human-only step (§5.5): pause, save a checkpoint, log the
    handoff in the audit trail, and record the 10-minute deadline.
    The task never waits forever: resume after the deadline fails
    it instead (see resume_task).

    Idempotent: a step already handed off keeps its original
    checkpoint and deadline — re-executing it (it raises again on
    the human-only description or element) must not reset the
    10-minute clock, or a retry loop could wait forever.
    """
    existing = _get_step(conn, task_id, n)
    if existing is not None and existing["status"] == "handoff":
        return
    deadline = int(time.time()) + HANDOFF_TIMEOUT_S
    conn.execute(
        "UPDATE steps SET status = 'handoff', error = ? WHERE task_id = ? AND n = ?",
        (reason, task_id, n),
    )
    write_checkpoint(
        conn,
        task_id,
        n,
        f"Step {n} handed off to the user: {reason}.\n"
        f"Handoff deadline: {deadline} (10 minutes).\n"
        f"Task: {get_task(conn, task_id)['goal']}",
        {"step": n, "handoff": reason, "deadline": deadline},
    )
    _audit(conn, task_id, "task.handoff", {"step": n, "reason": reason, "deadline": deadline})
    _set_task_status(conn, task_id, "handoff")


def resume_task(conn, task_id: str) -> str:
    """Resume after a human-only handoff: the user completed the
    human step, so mark every handoff step done and let the task
    continue from the checkpoint. If the handoff deadline passed,
    the task fails instead (§5.5 step 4)."""
    task = get_task(conn, task_id)
    if task is None:
        raise KeyError(f"no task {task_id}")
    # Resume only makes sense from a handoff (the user completed
    # the human step) or a pause. Resuming a done, cancelled or
    # failed task would silently resurrect it — the workspace
    # Resume button is only enabled for paused tasks.
    if task["status"] not in ("handoff", "paused"):
        raise ValueError(
            f"task {task_id} is {task['status']}; nothing to resume"
        )
    now = int(time.time())
    handoff_steps = conn.execute(
        "SELECT n FROM steps WHERE task_id = ? AND status = 'handoff'",
        (task_id,),
    ).fetchall()
    # Phase 1: check EVERY handoff deadline first. A single
    # expired handoff fails the task — without marking any step
    # done first (the old loop marked the earlier steps done, then
    # failed the task on the expired one, leaving a failed task
    # with done handoff steps).
    for row in handoff_steps:
        # The deadline lives in the handoff checkpoint's state_json.
        cp = conn.execute(
            "SELECT state_json FROM checkpoints WHERE task_id = ? AND step_n = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (task_id, row["n"]),
        ).fetchone()
        deadline = 0
        if cp:
            try:
                deadline = int(json.loads(cp["state_json"]).get("deadline", 0))
            except (ValueError, TypeError):
                deadline = 0
        if deadline and now > deadline:
            _set_task_status(conn, task_id, "failed")
            _audit(conn, task_id, "task.handoff_timeout", {"step": row["n"]})
            return "failed"
    # Phase 2: every deadline is live — the user completed the
    # human steps, so mark them done and continue from the checkpoint.
    for row in handoff_steps:
        conn.execute(
            "UPDATE steps SET status = 'done', result = 'completed by user (handoff)',"
            " ended_at = ? WHERE task_id = ? AND n = ?",
            (now, task_id, row["n"]),
        )
    conn.commit()
    _set_task_status(conn, task_id, "planned")
    _audit(conn, task_id, "task.resume", {"steps_resumed": len(handoff_steps)})
    return "planned"


def execute_step(
    conn,
    task_id: str,
    n: int,
    observation: dict,
    call_fn=None,
) -> dict:
    """Plan one action for step n and return it, validated.

    `observation` is the redacted observation from the extension:
    {url, elements: [{id, tag, role, name, text, value, visible,
    interactable}]}. Returns {action, expected_end_state, model}.
    Raises validate.ValidationError after 3 failed planner calls,
    GroundingError when the planner names an element that is not in
    the observation (caller must re-observe once and re-plan, §6.3),
    or KeyError for a missing task/step.
    """
    call_fn = call_fn or router.call_planner
    task = get_task(conn, task_id)
    if task is None:
        raise KeyError(f"no task {task_id}")
    # The task's own state gates execution: a cancelled or paused
    # task stays stopped, and a failed or finished task never runs
    # again (the old code only looked at the step, so pause/cancel
    # were advisory). "ask_user" still executes: an approval
    # returns the task to "planned", and a re-execute without one
    # re-asks instead of running. "handoff" also still executes:
    # the page itself gates progress after a handoff — the OTP form
    # is still showing, so the next step cannot meaningfully run
    # until the user completes the human step — and the benchmark
    # drives every step of a fixture regardless, counting a
    # post-handoff refusal as a fixture error. Blocking handoff
    # here broke two benchmark tests; the page-state gate is the
    # real protection.
    if task["status"] in ("cancelled", "paused", "failed", "done"):
        raise ValueError(f"task {task_id} is {task['status']}; refusing to execute")
    step = _get_step(conn, task_id, n)
    if step is None:
        raise KeyError(f"no step {n} for task {task_id}")
    if n > MAX_STEPS_PER_TASK:
        raise ValueError("task exceeds the 25-step limit")
    # Never re-execute a step that is already done (§6.9: never
    # retry a submit — the server is the durable backstop; the
    # extension's in-memory guard is only the second layer).
    if step["status"] == "done":
        raise ValueError(f"step {n} is already done; refusing to re-execute")
    if step["status"] in ("cancelled", "failed"):
        raise ValueError(f"step {n} is {step['status']}; refusing to execute")
    plan_step = json.loads(step["action_json"])
    if _is_human_only_description(plan_step.get("description", "")):
        _handoff(conn, task_id, n, f"human-only step: {plan_step['description']}")
        raise ValueError(f"step {n} is human-only: {plan_step['description']}")

    elements = observation.get("elements", [])
    element_ids = {e["id"] for e in elements if isinstance(e.get("id"), str)}
    interactable_ids = {
        e["id"] for e in elements if isinstance(e.get("id"), str) and e.get("interactable")
    }
    # The observation URL goes to the cloud planner: strip the
    # query string and fragment first (a password-reset token or a
    # session id in the query must not leave the device — the same
    # origin + pathname rule verify and the receipts already use).
    obs_text = ctx.format_observation(_strip_url(observation.get("url", "")), elements)
    summary = checkpoint_summary(conn, task_id)
    results = last_results(conn, task_id)
    facts = _memory_facts(conn, task["goal"])
    fixed = [
        prompts.REDACTION_CONTRACT,
        prompts.ACTION_SCHEMA,
        f"GOAL: {task['goal']}",
        f"CONSTRAINTS: {task['constraints']}" if task["constraints"] else "",
        f"PROGRESS SO FAR:\n{summary}" if summary else "",
        "LAST STEP RESULTS:\n" + "\n".join(results) if results else "",
        "REMEMBERED FACTS:\n" + "\n".join(facts) if facts else "",
    ]
    # A re-executed step (verification failed last time) tells the
    # planner what it already tried — without this the planner
    # repeats the failed action blindly (the live stall: "click
    # Search" three times on step 5). A repeated click is also
    # hard-blocked below; other actions are only flagged, because
    # a re-scroll or a re-type can legitimately succeed.
    prev_action = plan_step.get("action")
    prev_element = plan_step.get("element_id")
    if prev_action:
        prev_line = (
            "PREVIOUS ATTEMPT ON THIS STEP (verification failed —"
            f" the end state was not reached): {prev_action}"
        )
        if prev_element:
            prev_line += f" {prev_element}"
        if prev_action == "click" and prev_element:
            prev_line += (
                f". Do not click {prev_element} again on this step;"
                " choose a different action."
            )
        fixed.append(prev_line)
    prompt = ctx.fit_to_budget(fixed, obs_text)
    if ctx.estimate_tokens(prompt) > ctx.MAX_TOKENS:
        logger.warning(
            "task %s step %s prompt exceeds the 8K budget (%d tokens)",
            task_id, n, ctx.estimate_tokens(prompt),
        )
    allowed_sites = _allowed_sites(task["constraints"])
    local_only = _is_local_only(task["constraints"])

    # PR #7 (W3): a deterministic plan step already carries its
    # action ({"action": "navigate", "url": ...} — only
    # deterministic plans do; validate.parse_plan strips these
    # keys from planner plans). It executes with NO planner
    # call: the URL was resolved from the user's own words at
    # plan time and is re-validated here against the task's
    # allow-list, so a constraint added since planning (a
    # refinement) still fails closed. current_url is NOT passed
    # to validate: the destination is the user's own typed goal,
    # not planner output a page could have steered, so the
    # current-site rule does not apply. The navigate target is
    # added to the returned allow-list so the executor permits
    # this one navigation from a fresh tab (executor.ts fails
    # closed to the current site without an allow-list entry).
    # A navigate is never consequential (no submit/send/pay), so
    # no approval gate applies; the human-only, already-done and
    # task-state guards above all still apply.
    if plan_step.get("action") == "navigate" and plan_step.get("url"):
        det_action = {"action": "navigate", "url": plan_step["url"]}
        validate.validate_action(
            det_action, element_ids, interactable_ids, allowed_sites,
        )
        stored = dict(det_action)
        stored["step"] = plan_step.get("step", n)
        stored["description"] = plan_step.get("description", "")
        stored["expected_end_state"] = plan_step.get("expected_end_state", "")
        stored["_consequential"] = False
        det_now = int(time.time())
        conn.execute(
            "UPDATE steps SET action_json = ?, element_id = ?, status = 'executing',"
            " started_at = ?, ms = ?, grounding_failures = 0"
            " WHERE task_id = ? AND n = ?",
            (json.dumps(stored), None, det_now, 0, task_id, n),
        )
        conn.commit()
        _audit(conn, task_id, "task.execute",
               {"step": n, "action": "navigate", "model": "deterministic"})
        from urllib.parse import urlparse as _urlparse

        det_host = (_urlparse(det_action["url"]).hostname or "").lower()
        det_allowed = sorted({*(allowed_sites or []), det_host} - {""})
        return {
            "action": det_action,
            "expected_end_state": plan_step.get("expected_end_state", ""),
            "model": "deterministic",
            "task_id": task_id,
            "element": None,
            "allowed_sites": det_allowed,
            "fill": {},
            "fill_denied_reason": None,
        }

    # Second PII scan before the outbound planner call (§6.4).
    # A hit is re-redacted and logged; two blocks on the same step
    # stop the task.
    prompt, hits = scan.redact(prompt)
    if hits:
        blocks = int(step.get("scan_blocks") or 0) + 1
        conn.execute(
            "UPDATE steps SET scan_blocks = ? WHERE task_id = ? AND n = ?",
            (blocks, task_id, n),
        )
        conn.commit()
        _audit(conn, task_id, "task.pii_block", {"step": n, "hits": len(hits), "block": blocks})
        if blocks >= 2:
            conn.execute(
                "UPDATE steps SET status = 'failed', error = ? WHERE task_id = ? AND n = ?",
                ("second PII scan blocked twice on this step", task_id, n),
            )
            _set_task_status(conn, task_id, "failed")
            raise validate.ValidationError(
                "second PII scan blocked twice on this step; task stopped"
            )

    last_error = ""
    for attempt in range(1, MAX_PLANNER_CALLS_PER_STEP + 1):
        attempt_prompt = prompt
        if last_error:
            attempt_prompt += (
                f"\n\nYour previous action was rejected: {last_error}. "
                "Return a corrected action."
            )
            # The retry text is appended AFTER the base prompt was
            # redacted — re-scan it before the outbound call, so a
            # retry never sends unredacted text (Sprint 6 review).
            attempt_prompt, retry_hits = scan.redact(attempt_prompt)
            if retry_hits:
                _audit(
                    conn,
                    task_id,
                    "task.pii_block_retry",
                    {"step": n, "attempt": attempt, "hits": len(retry_hits)},
                )
        raw, model, ms = call_fn(attempt_prompt, local_only=local_only)
        try:
            action = validate.parse_action(raw)
            validate.validate_action(
                action, element_ids, interactable_ids, allowed_sites,
                elements=elements, current_url=observation.get("url", ""),
            )
        except validate.ValidationError as exc:
            # Grounding failure: do NOT re-plan against the same
            # stale observation (§6.3). Tell the caller to
            # re-observe once and re-plan. Two failed groundings
            # in a row → ask the user.
            if "not in the current observation" in str(exc):
                failures = int(step.get("grounding_failures") or 0) + 1
                conn.execute(
                    "UPDATE steps SET grounding_failures = ? WHERE task_id = ? AND n = ?",
                    (failures, task_id, n),
                )
                conn.commit()
                _audit(conn, task_id, "task.grounding_miss",
                       {"step": n, "failures": failures})
                if failures >= 2:
                    _set_task_status(conn, task_id, "ask_user")
                    raise GroundingError(
                        f"two failed groundings in a row on step {n}; asking the user"
                    ) from exc
                raise GroundingError(
                    f"element not in the current observation (re-observe once and re-plan): {exc}"
                ) from exc
            last_error = str(exc)
            logger.warning("task %s step %s planner attempt %d rejected: %s", task_id, n, attempt, exc)
            continue
        # Human-only check on the LIVE action: a step that targets
        # a CAPTCHA element (or a verification-code field) is a
        # handoff even when the plan description did not say so.
        # "otp" and "passkey" and "3-d secure" are in the list too:
        # the fixture's OTP field is named "OTP (sent to your
        # phone)" — neither "captcha" nor "verification code" —
        # so without them the OTP step fails in the executor
        # ("placeholder [OTP] has no vault value") and retries 3
        # times before asking the user with a generic failure.
        target = next(
            (e for e in elements if e.get("id") == action.get("element_id")), None
        )
        if target:
            target_text = f"{target.get('name','')} {target.get('text','')}".lower()
            if any(marker in target_text for marker in (
                "captcha", "verification code", "otp", "passkey",
                "3-d secure", "3d secure",
            )):
                _handoff(conn, task_id, n, f"action targets a human-only element: {action.get('element_id')}")
                raise ValueError(f"step {n} targets a human-only element")
        # Same-step repeat (the live stall: step 5 re-planned
        # "click Search" after verification failed, because the
        # guard below only looked at DONE steps): this step already
        # tried this exact click and verification failed — reject
        # it and let the planner choose something else within this
        # step's 3-call budget. Clicks only: a re-scroll or a
        # re-type on the same step can legitimately succeed.
        if (
            action.get("action") == "click"
            and prev_action == "click"
            and action.get("element_id")
            and action.get("element_id") == prev_element
        ):
            last_error = (
                f"element {action['element_id']} was already clicked "
                f"on this step (step {n}) and verification failed; "
                "choose a different action for this step"
            )
            logger.warning(
                "task %s step %s planner attempt %d repeated this step's click: %s",
                task_id, n, attempt, last_error,
            )
            continue
        # Never retry a submit: a click on an element that already
        # has a done click step in this task is a REPEAT, not an
        # instant failure — tell the planner which step already
        # clicked it and let it re-plan within this step's 3-call
        # budget (fix list item 2 prep: the live planner repeated
        # "click el_apply_1" and stalled the job-listing fixture
        # twice, because the old code raised here instead of
        # re-planning). After 3 repeats the step fails with the
        # "could not produce a valid action in 3 calls" error below
        # and the task asks the user — still fail-closed.
        # Pagination controls ("Next", "Load more", page numbers)
        # are exempt: they are designed to be clicked repeatedly,
        # and blocking them strands any multi-page flow.
        if (
            action.get("action") == "click"
            and action.get("element_id")
            and not _is_pagination_control(action, elements)
        ):
            done_rows = conn.execute(
                "SELECT n, action_json FROM steps WHERE task_id = ? AND status = 'done'",
                (task_id,),
            ).fetchall()
            repeated_step = None
            for done_row in done_rows:
                try:
                    done_action = json.loads(done_row["action_json"] or "{}")
                except ValueError:
                    continue
                if (
                    done_action.get("action") == "click"
                    and done_action.get("element_id") == action["element_id"]
                ):
                    repeated_step = done_row["n"]
                    break
            if repeated_step is not None:
                last_error = (
                    f"element {action['element_id']} was already clicked "
                    f"successfully in step {repeated_step} of this task; "
                    "choose a different element for this step"
                )
                logger.warning(
                    "task %s step %s planner attempt %d repeated a click: %s",
                    task_id, n, attempt, last_error,
                )
                continue
        # Already applied (§6.9): before a consequential click,
        # check the receipts + memory. A role applied to in an
        # earlier task (or on another page/element) is never
        # re-submitted — the loop's same-element-in-same-task check
        # alone is not enough. The role comes from the task
        # constraints ("role: …", "url: …").
        role_ctx = _role_from_constraints(task.get("constraints") or "")
        claim_id: str | None = None
        if _is_consequential(action, elements) and (
            role_ctx["role_id"] or role_ctx["url"]
        ):
            from ..jobs import applied as _applied

            if _applied.is_already_applied(
                conn, role_ctx["role_id"] or role_ctx["url"], role_ctx["url"]
            ):
                _set_task_status(conn, task_id, "ask_user")
                _audit(conn, task_id, "task.already_applied",
                       {"step": n, "role_id": role_ctx["role_id"],
                        "url": role_ctx["url"]})
                raise ValueError(
                    f"already applied to {role_ctx['role_id'] or role_ctx['url']}"
                    " (receipt on file — no re-submit)"
                )
            # Claim the role for this task's in-flight submit
            # (§6.9 race): the receipt only lands at verify time,
            # so without a claim two tasks for the same role both
            # pass the gate above and both submit before either
            # receipt exists. The claim is promoted to the
            # submitted receipt at verify time, released if this
            # task cannot submit (approval below), and expires
            # after 10 minutes if the task dies mid-submit.
            claim_id = _claim_role(
                conn, task_id, role_ctx["role_id"] or role_ctx["url"],
                role_ctx["url"],
            )
            if claim_id is None:
                _set_task_status(conn, task_id, "ask_user")
                _audit(conn, task_id, "task.already_applying",
                       {"step": n, "role_id": role_ctx["role_id"],
                        "url": role_ctx["url"]})
                raise ValueError(
                    "another task is already applying to "
                    f"{role_ctx['role_id'] or role_ctx['url']}"
                    " (in-flight submit — no double-apply)"
                )
        # Approval before a consequential action (§5.4): a submit /
        # apply / confirm click on a task that requires approval
        # needs a valid grant. Without one, the task asks the user
        # (the approval card) and the step does not execute. The
        # grant is consumed here — one approval, one submit — and
        # only a grant scoped to this element/role can be spent.
        if _is_consequential(action, elements) and _requires_approval(task):
            if not _consume_approval(
                conn, task_id,
                element_id=action.get("element_id"),
                role_id=role_ctx["role_id"],
            ):
                # The claim is released: this task cannot submit
                # without approval, so it must not hold the role.
                if claim_id is not None:
                    _release_claim(conn, claim_id)
                # The gate is RECORDED (Bug 3): the pending
                # approval is the only id approve will accept, and
                # it rides in the error so the caller can show the
                # approval card with the real id.
                approval_id = record_pending_approval(
                    conn, task_id, n, "submit",
                    element_id=action.get("element_id"),
                    role_id=role_ctx["role_id"],
                )
                _set_task_status(conn, task_id, "ask_user")
                _audit(conn, task_id, "task.approval_needed",
                       {"step": n, "action": action.get("action"),
                        "element_id": action.get("element_id"),
                        "approval_id": approval_id})
                raise ValueError(
                    f"step {n} needs approval before submit"
                    " (POST /tasks/{id}/approve with approval_id "
                    f"{approval_id})"
                )
            _audit(conn, task_id, "task.approval_consumed", {"step": n})
        # Valid: record the step as executing and return the action.
        # The executor needs more than the action: the element's
        # info (the fingerprint), the allowed sites and the task id
        # (the live path had none of these, so the fingerprint
        # check was dead and every submit was refused).
        # The stored action keeps the plan's description and
        # expected end state (the old code overwrote action_json
        # with the bare action, so the plan description vanished
        # from GET /tasks and a re-execute returned an empty
        # expected end state). Only the plan keys are kept — the
        # plan keys always win, so a planner-supplied "description"
        # cannot rewrite the plan, and stale action fields from a
        # previous attempt (a "text" from an old type) never
        # survive into the new action.
        # A validated action also resets the grounding counter:
        # the rule is two failed groundings IN A ROW, and a success
        # breaks the row.
        stored = dict(action)
        stored["step"] = plan_step.get("step", n)
        stored["description"] = plan_step.get("description", "")
        stored["expected_end_state"] = plan_step.get("expected_end_state", "")
        # Whether this action was consequential, decided now while
        # the target element is in view: verify_step records a
        # receipt only for consequential clicks, and by verify time
        # the element is gone (the page moved on).
        stored["_consequential"] = _is_consequential(action, elements)
        now = int(time.time())
        conn.execute(
            "UPDATE steps SET action_json = ?, element_id = ?, status = 'executing',"
            " started_at = ?, ms = ?, grounding_failures = 0"
            " WHERE task_id = ? AND n = ?",
            (json.dumps(stored), action.get("element_id"), now, int(ms), task_id, n),
        )
        conn.commit()
        _audit(conn, task_id, "task.execute",
               {"step": n, "action": action.get("action"),
                "element_id": action.get("element_id"), "model": model})
        target = next(
            (e for e in elements if e.get("id") == action.get("element_id")), None
        )
        # The fill map (Sprint 5, §6.7): when the action's text
        # carries placeholders, attach the placeholder → value map
        # for the extension. Secret placeholders need the vault
        # unlocked for the task AND an approval grant (approval
        # BEFORE vault fill). The map travels only in this local
        # execute response — never to the planner, never in a log.
        # Never log the execute response body (it may carry the
        # fill map with a full card number).
        fill_map: dict[str, str] = {}
        fill_denied_reason: str | None = None
        if action.get("action") == "type" and action.get("text"):
            import re as _re

            from ..vault import fill as _fill
            from ..api.vault_api import VAULT as _VAULT

            tokens = _re.findall(r"\[[A-Z0-9_]+\]", action["text"])
            if tokens:
                now = int(time.time())
                # The fill needs an approval grant for the TASK —
                # not a mail-draft approval (whose detail names a
                # draft_id). A mail approval must never authorise a
                # card fill. The grant is not consumed by a fill
                # (a checkout fills several fields in one task),
                # but its scope is checked on every fill.
                approval_grants = [
                    dict(row) for row in conn.execute(
                        "SELECT id, detail FROM grants WHERE origin = ?"
                        " AND scope = 'approve' AND expires_at > ?",
                        (task_id, now),
                    ).fetchall()
                ]
                fill_map, fill_denied_reason = _fill.build_fill_map(
                    conn, task_id, tokens,
                    _origin_of(observation.get("url", "")),
                    _VAULT, approval_grants,
                    constraints=task.get("constraints") or "",
                    target=target,
                )
                # Approval-before-fill ordering (B3): a secret
                # placeholder with no fill is not a silent empty
                # map — the user must be asked for approval (the
                # type steps are not consequential, so the loop
                # would otherwise never prompt). Set the task to
                # ask_user and raise the approval-needed error, so
                # the user gets the approval card with a clear
                # reason, not a generic failure after 3 retries.
                secret_tokens = [t for t in tokens if t in _fill.SECRET_PLACEHOLDERS]
                if secret_tokens and any(t not in fill_map for t in secret_tokens):
                    # The gate is RECORDED (Bug 3): like the submit
                    # gate above, the pending approval is the only
                    # id approve will accept — without it the user
                    # could never approve past a fill denial.
                    approval_id = record_pending_approval(
                        conn, task_id, n, "vault fill",
                    )
                    _set_task_status(conn, task_id, "ask_user")
                    _audit(conn, task_id, "task.fill_denied",
                           {"step": n, "tokens": secret_tokens,
                            "reason": fill_denied_reason,
                            "approval_id": approval_id})
                    raise ValueError(
                        f"approval needed before the vault can fill: "
                        f"{fill_denied_reason or 'no fill available'}"
                        " (POST /tasks/{id}/approve with approval_id "
                        f"{approval_id})"
                    )
        return {
            "action": action,
            "expected_end_state": plan_step.get("expected_end_state", ""),
            "model": model,
            "task_id": task_id,
            "element": target,
            "allowed_sites": _allowed_sites(task.get("constraints") or ""),
            "fill": fill_map,
            "fill_denied_reason": fill_denied_reason,
        }
    # 3 failed planner calls: two failed groundings in a row → ask user.
    conn.execute(
        "UPDATE steps SET status = 'failed', error = ? WHERE task_id = ? AND n = ?",
        (f"planner validation failed 3 times: {last_error}", task_id, n),
    )
    _set_task_status(conn, task_id, "ask_user")
    _audit(conn, task_id, "task.ask_user", {"step": n, "reason": last_error})
    raise validate.ValidationError(
        f"planner could not produce a valid action in 3 calls: {last_error}"
    )


def _baseline(observation: dict) -> dict:
    """The pre-action observation, reduced to what verify diffs
    against: element ids, their text/value, the URL, and the page
    text. Pre-existing evidence never verifies a step."""
    elements = observation.get("elements", []) if observation else []
    by_id: dict[str, dict] = {}
    texts: set[str] = set()
    for el in elements:
        el_id = el.get("id")
        if isinstance(el_id, str):
            by_id[el_id] = el
            for field in ("name", "text", "value"):
                v = (el.get(field) or "").strip()
                if v:
                    texts.add(v)
    return {
        "ids": set(by_id),
        "by_id": by_id,
        "texts": texts,
        "url": observation.get("url", "") if observation else "",
    }


def verify_step(
    conn,
    task_id: str,
    n: int,
    new_observation: dict,
    expected_end_state: str,
    previous_url: str = "",
    previous_observation: dict | None = None,
) -> dict:
    """Verify step n by re-observing (plan §6.2: "action ran" is not
    verification). Returns {verified, reason, retry}.

    Every check is a baseline diff against the pre-action
    observation: the end state must be NEWLY true. A confirmation
    that was already on the page, a field that was already filled,
    and an element that was already present do not verify anything.
    """
    step = _get_step(conn, task_id, n)
    if step is None:
        raise KeyError(f"no step {n} for task {task_id}")
    # Verify only runs on a live task's executing step. The old
    # code checked neither: a pending step (no action ever ran)
    # verified on a bare URL change, and verifying a cancelled
    # task's last step flipped the task to "done".
    task = get_task(conn, task_id)
    if task is not None and task["status"] in ("cancelled", "paused", "failed", "done"):
        raise ValueError(f"task {task_id} is {task['status']}; refusing to verify")
    if step["status"] != "executing":
        raise ValueError(f"step {n} is {step['status']}; refusing to verify (not executing)")
    action = json.loads(step["action_json"]) if step["action_json"] else {}
    if action.get("action") == "wait":
        return _mark_done(conn, task_id, n, "wait elapsed")
    elements = new_observation.get("elements", [])
    ids = {e["id"] for e in elements if isinstance(e.get("id"), str)}
    texts = " ".join(
        f"{e.get('name','')} {e.get('text','')} {e.get('value','')}" for e in elements
    )
    # The verify reason and the receipt confirmation persist in the
    # steps table and the checkpoint summaries — strip the query
    # string and fragment from both URLs first (a password-reset
    # token or an email in a query string must not be stored).
    # This is the same origin + pathname rule the executor's
    # safeUrl applies.
    url = _strip_url(new_observation.get("url", ""))
    previous_url = _strip_url(previous_url or "")
    expected = expected_end_state or ""
    el_id = action.get("element_id")
    acted = next((e for e in elements if e.get("id") == el_id), None)
    base = _baseline(previous_observation or {"url": previous_url, "elements": []})

    reason = ""
    verified = False
    # 1. An element id NAMED in the expected end state is now present
    #    and is NEW (or its text/value changed). Only synthetic
    #    eclipse_el_<n> ids (and, for older observations, el_<n> ids)
    #    and ids quoted in the end state count — a bare English word
    #    that happens to equal an id does not.
    candidates = set(re.findall(r"eclipse_el_\d+|el_\d+", expected))
    for quoted in re.findall(r'"([^"]+)"', expected):
        if quoted in ids:
            candidates.add(quoted)
    for named in candidates:
        if named in ids:
            before = base["by_id"].get(named)
            after = next((e for e in elements if e.get("id") == named), None)
            if before is None:
                verified, reason = True, f"element {named} appeared"
                break
            before_sig = f"{before.get('name','')}|{before.get('text','')}|{before.get('value','')}"
            after_sig = f"{after.get('name','')}|{after.get('text','')}|{after.get('value','')}"
            if before_sig != after_sig:
                verified, reason = True, f"element {named} changed"
                break
    # 2. A quoted text now appears that was NOT there before.
    if not verified:
        for quoted in re.findall(r'"([^"]+)"', expected):
            if quoted and quoted in texts and quoted not in base["texts"]:
                verified, reason = True, f'text "{quoted}" appeared'
                break
    # 3. Type/select: the acted-on field's value CHANGED (was empty
    #    or different before, non-empty now). A pre-filled value
    #    does not verify the typing.
    if not verified and action.get("action") in ("type", "select") and acted:
        value = (acted.get("value") or "").strip()
        before = base["by_id"].get(el_id) or {}
        before_value = (before.get("value") or "").strip()
        if value and value != before_value:
            verified, reason = True, f"{el_id} value changed"
    # 4. Click: the URL changed, the acted-on element was present
    #    before and is gone now, or a confirmation word appears
    #    that was NOT in the baseline text.
    if not verified and action.get("action") == "click":
        if url and previous_url and url != previous_url:
            verified, reason = True, f"URL changed to {url}"
        elif el_id and el_id in base["ids"] and el_id not in ids:
            verified, reason = True, "acted-on element is gone (page moved on)"
        else:
            base_text = " ".join(sorted(base["texts"])).lower()
            for word in ("confirmation", "success", "submitted", "applied"):
                if word in texts.lower() and word not in base_text:
                    verified, reason = True, f"confirmation text appeared ({word})"
                    break
    # 4b. Navigate (PR #7, W3): the new URL's host IS the
    # target host, and the previous URL's host was different —
    # or there was no previous URL (a fresh tab, where there is
    # no baseline to diff against and reaching the target host
    # is the whole end state). Navigating to the page already
    # showing does NOT verify (the end state was pre-existing);
    # the generic URL-change fallback below still catches a
    # navigate that landed on a different page of the same host.
    if not verified and action.get("action") == "navigate" and action.get("url"):
        from urllib.parse import urlparse as _urlparse

        target_host = (_urlparse(action["url"]).hostname or "").lower()
        new_host = (
            _urlparse(new_observation.get("url", "")).hostname or ""
        ).lower()
        prev_host = (
            (_urlparse(previous_url).hostname or "").lower()
            if previous_url else ""
        )
        if target_host and new_host == target_host and new_host != prev_host:
            verified, reason = True, f"reached {target_host}"
    # 5. Generic fallback: the URL changed.
    if not verified and url and previous_url and url != previous_url:
        verified, reason = True, f"URL changed to {url}"
    if verified:
        # A verified SUBMIT records its receipt HERE (§6.9) — the
        # loop, not an external POST, is what makes "already
        # applied" durable. The role comes from the task
        # constraints; without a role there is nothing to record
        # (the login flow). The confirmation is the end-state
        # text, redacted before it is stored. Only consequential
        # clicks count: an "Apply now" click that merely opens the
        # form is not a submit, and recording it as one blocked
        # the real submit with a false "already applied" (the flag
        # was set at execute time, when the element was in view;
        # rows written before the flag existed default to True, so
        # an in-flight submit keeps its receipt).
        if action.get("action") == "click" and action.get("_consequential", True):
            task_row = conn.execute(
                "SELECT constraints FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            role_ctx = _role_from_constraints(
                (task_row["constraints"] or "") if task_row else ""
            )
            if role_ctx["role_id"] or role_ctx["url"]:
                from ..jobs import applied as _applied
                from ..planner import scan as _scan

                confirmation, _hits = _scan.redact(
                    f"{expected} — {reason} ({url or ''})"
                )
                step_row = conn.execute(
                    "SELECT element_id FROM steps WHERE task_id = ? AND n = ?",
                    (task_id, n),
                ).fetchone()
                el = (step_row["element_id"] if step_row else None) or ""
                acted_el = next(
                    (e for e in (new_observation or {}).get("elements", [])
                     if e.get("id") == el), None
                )
                role_key = role_ctx["role_id"] or role_ctx["url"] or ""
                _settle_receipt(
                    conn, task_id, role_key,
                    acted_el.get("name", "") if acted_el else "",
                    role_ctx["url"] or url or "", confirmation,
                )
                _applied.mark_applied(
                    conn, role_key,
                    role_ctx["url"] or url or None,
                )
                _audit(conn, task_id, "task.receipt",
                       {"step": n, "role_id": role_ctx["role_id"]})
        return _mark_done(conn, task_id, n, reason)
    # Not verified: retry up to 3 times, then fail the step.
    retries = int(step.get("retries") or 0) + 1
    conn.execute(
        "UPDATE steps SET retries = ? WHERE task_id = ? AND n = ?",
        (retries, task_id, n),
    )
    conn.commit()
    _audit(conn, task_id, "task.verify_failed",
           {"step": n, "retries": retries, "expected": expected})
    if retries >= MAX_RETRIES_PER_STEP:
        conn.execute(
            "UPDATE steps SET status = 'failed', error = ? WHERE task_id = ? AND n = ?",
            (f"verify failed after {retries} retries: {expected}", task_id, n),
        )
        _set_task_status(conn, task_id, "failed")
        # The submit never landed: release this step's role claim
        # (if any) so the role is retryable at once instead of
        # waiting out the 10-minute claim TTL.
        if action.get("action") == "click" and action.get("_consequential", True):
            role_ctx = _role_from_constraints(
                (task.get("constraints") or "") if task else ""
            )
            if role_ctx["role_id"] or role_ctx["url"]:
                _release_claim_for_task(
                    conn, task_id, role_ctx["role_id"] or role_ctx["url"]
                )
        return {"verified": False, "reason": f"failed after {retries} retries", "retry": False}
    backoff = RETRY_BACKOFF_S[min(retries - 1, len(RETRY_BACKOFF_S) - 1)]
    return {
        "verified": False,
        "reason": f"end state not seen ({expected}); retry {retries}/3 after {backoff}s",
        "retry": True,
        "backoff_s": backoff,
    }


def _mark_done(conn, task_id: str, n: int, reason: str) -> dict:
    step = _get_step(conn, task_id, n)
    now = int(time.time())
    started = int(step["started_at"]) if step and step["started_at"] else now
    conn.execute(
        "UPDATE steps SET status = 'done', ended_at = ?, ms = ?, result = ?"
        " WHERE task_id = ? AND n = ?",
        (now, (now - started) * 1000, reason, task_id, n),
    )
    # Checkpoint: 10-line summary of what is done and what is next.
    summary = (
        f"Step {n} done: {reason}.\n"
        f"Task: {get_task(conn, task_id)['goal']}\n"
        f"Last results: {'; '.join(last_results(conn, task_id))}"
    )
    write_checkpoint(conn, task_id, n, summary, {"step": n, "result": reason})
    _audit(conn, task_id, "task.verify", {"step": n, "reason": reason})
    # If every step is done, the task is done. A handoff step still
    # outstanding blocks "done" (it is not done until the user
    # completes it and the task resumes).
    remaining = conn.execute(
        "SELECT COUNT(*) AS c FROM steps WHERE task_id = ?"
        " AND status IN ('pending', 'executing', 'handoff')",
        (task_id,),
    ).fetchone()["c"]
    # A failed step blocks "done" too: the old code only counted
    # unfinished steps, so a task whose step 1 failed and whose
    # step 2 then verified reported "done" with the goal half
    # achieved. The task keeps its current status (ask_user or
    # failed) until the user deals with the failed step.
    failed = conn.execute(
        "SELECT COUNT(*) AS c FROM steps WHERE task_id = ? AND status = 'failed'",
        (task_id,),
    ).fetchone()["c"]
    if remaining == 0 and failed == 0:
        _set_task_status(conn, task_id, "done")
    return {"verified": True, "reason": reason, "retry": False}
