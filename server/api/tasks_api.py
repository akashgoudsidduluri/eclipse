"""Task API (Sprint 3, plan §6.1). Token-authenticated, like the rest.

POST /tasks                              {goal, constraints?} -> 201 {id}
GET  /tasks/{id}                         -> task + steps + receipts
GET  /tasks                              -> list (filter by status)
POST /tasks/{id}/plan                    -> decompose the goal (1 planner call)
POST /tasks/{id}/steps/{n}/execute       {observation} -> one validated action
POST /tasks/{id}/steps/{n}/verify        {observation, expected_end_state, previous_url?}
                                         -> {verified, reason, retry}
POST /tasks/{id}/approve                 {approval_id} -> approve a pending step
GET  /tasks/{id}/approvals               -> live pending approvals for the task
POST /tasks/{id}/cancel | /pause | /resume
"""
from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core import db
from ..core.auth import require_token
from ..planner import validate
from ..realtime import driver as realtime_driver
from ..realtime.hub import hub as realtime_hub
from ..tasks import approvals, loop

router = APIRouter(tags=["tasks"])


class TaskIn(BaseModel):
    goal: str = Field(..., min_length=1, max_length=2000)
    constraints: str = Field(default="", max_length=2000)


class ObservationIn(BaseModel):
    url: str = Field(default="")
    elements: list[dict] = Field(default_factory=list)


class ExecuteIn(BaseModel):
    observation: ObservationIn


class VerifyIn(BaseModel):
    observation: ObservationIn
    # The pre-action observation: verify is a baseline diff, so the
    # caller must send the observation the step started from. Without
    # it, pre-existing text and pre-filled values would verify a step
    # that did nothing.
    previous_observation: ObservationIn
    expected_end_state: str = Field(default="", max_length=500)
    previous_url: str = Field(..., min_length=1, max_length=500)


def _conn():
    return db.connect()


def _task_or_404(task_id: str) -> dict:
    conn = _conn()
    try:
        task = loop.get_task(conn, task_id)
    finally:
        conn.close()
    if task is None:
        raise HTTPException(status_code=404, detail=f"no task {task_id}")
    return task


@router.post("/tasks", status_code=201)
def create_task(body: TaskIn, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        task_id = loop.create_task(conn, body.goal, body.constraints)
    finally:
        conn.close()
    return {"id": task_id, "status": "created"}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, _token: str = Depends(require_token)):
    task = _task_or_404(task_id)
    conn = _conn()
    try:
        steps = []
        for r in conn.execute(
            "SELECT n, action_json, status, element_id, result, error, retries"
            " FROM steps WHERE task_id = ? ORDER BY n",
            (task_id,),
        ).fetchall():
            row = dict(r)
            # Sprint 9 fix (review B3): the steps table has no
            # description column — the description lives inside
            # action_json. Flatten it so clients can render the
            # plan without parsing JSON themselves.
            try:
                row["description"] = (
                    json.loads(row.get("action_json") or "{}").get("description")
                    or ""
                )
            except (ValueError, TypeError):
                row["description"] = ""
            steps.append(row)
        checkpoints = [
            dict(r)
            for r in conn.execute(
                "SELECT step_n, summary, created_at FROM checkpoints"
                " WHERE task_id = ? ORDER BY step_n",
                (task_id,),
            ).fetchall()
        ]
        # §6.1: the task response returns status, plan, steps AND
        # receipts.
        from ..jobs import receipts as _receipts

        report = _receipts.task_report(conn, task_id)
    finally:
        conn.close()
    return {**task, "steps": steps, "checkpoints": checkpoints,
            "receipts": report["submitted"] + report["failed"]}


@router.get("/tasks")
def list_tasks(status: str | None = None, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC"
            ).fetchall()
    finally:
        conn.close()
    return {"tasks": [dict(r) for r in rows]}


@router.post("/tasks/{task_id}/plan")
def plan_task(task_id: str, background_tasks: BackgroundTasks,
              _token: str = Depends(require_token)):
    _task_or_404(task_id)
    conn = _conn()
    try:
        steps = loop.plan_task(conn, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except validate.ValidationError as exc:
        raise HTTPException(status_code=502, detail=f"planner plan invalid: {exc}") from exc
    except ValueError as exc:
        # A state refusal (re-planning a planned/finished task) is
        # a conflict, not a planner failure.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - planner failures are 502s
        raise HTTPException(status_code=502, detail=f"planner failed: {exc}") from exc
    finally:
        conn.close()
    # PR #7 (W3): a freshly planned task starts driving at once
    # when the extension is live — the user should not have to
    # press anything else after Plan. Fire-and-forget: the drive
    # returns at once when the extension is offline (or the task
    # is mission-linked), and the 30s driver sweep picks the
    # task up when the extension reconnects.
    background_tasks.add_task(realtime_driver.drive_task, realtime_hub, task_id)
    return {"task_id": task_id, "steps": steps}


@router.post("/tasks/{task_id}/steps/{n}/execute")
def execute_step(task_id: str, n: int, body: ExecuteIn, _token: str = Depends(require_token)):
    _task_or_404(task_id)
    conn = _conn()
    try:
        result = loop.execute_step(
            conn, task_id, n, body.observation.model_dump()
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except loop.GroundingError as exc:
        # The planner named an element that is not in the current
        # observation: the caller re-observes once and re-plans (§6.3).
        raise HTTPException(
            status_code=409, detail={"reobserve": True, "error": str(exc)}
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except validate.ValidationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        conn.close()
    return result


@router.post("/tasks/{task_id}/steps/{n}/verify")
def verify_step(task_id: str, n: int, body: VerifyIn, _token: str = Depends(require_token)):
    _task_or_404(task_id)
    conn = _conn()
    try:
        result = loop.verify_step(
            conn,
            task_id,
            n,
            body.observation.model_dump(),
            body.expected_end_state,
            body.previous_url,
            body.previous_observation.model_dump(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # A state refusal (verifying a step that never executed, or
        # a stopped task) is a conflict, not a server error.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        conn.close()
    return result


class ApproveIn(BaseModel):
    approval_id: str = Field(..., min_length=1, max_length=120)
    # What the approval covers (§5.4): the step, the element and
    # the role. A grant scoped to one element/role cannot be
    # consumed by a different submit.
    step: int | None = Field(default=None, ge=1)
    element_id: str | None = Field(default=None, max_length=200)
    role_id: str | None = Field(default=None, max_length=200)


@router.get("/tasks/{task_id}/approvals")
def list_pending_approvals(task_id: str, _token: str = Depends(require_token)):
    """Live pending approvals for a task (the human gate, §5.4):
    the ids the client must POST to /tasks/{id}/approve. Expired
    approvals are not listed (approving one is a 409)."""
    _task_or_404(task_id)
    conn = _conn()
    try:
        import time

        now = int(time.time())
        rows = conn.execute(
            "SELECT id, task_id, step, action, detail, created_at, expires_at"
            " FROM pending_approvals WHERE task_id = ? AND expires_at > ?"
            " ORDER BY created_at ASC",
            (task_id, now),
        ).fetchall()
    finally:
        conn.close()
    approvals = []
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (ValueError, TypeError):
            detail = {}
        approvals.append({
            "approval_id": row["id"], "task_id": row["task_id"],
            "step": row["step"], "action": row["action"],
            **{k: v for k, v in detail.items() if v is not None},
            "created_at": row["created_at"], "expires_at": row["expires_at"],
        })
    return {"task_id": task_id, "approvals": approvals}


@router.post("/tasks/{task_id}/approve")
def approve_step(task_id: str, body: ApproveIn, _token: str = Depends(require_token)):
    """Approve a pending step (the approval card flow, §5.4/§6.1).

    The approval_id must name a LIVE pending approval recorded by
    the task loop when the step reached the human gate (Bug 3:
    the old handler accepted any made-up id, on any task state,
    because no pending-approval store existed at all). An unknown
    id is a 404; a pending approval on a finished task or past
    its expiry is a 409. On success the pending approval is
    consumed and a 10-minute grant is minted — the same grant
    semantics as before (one approval = one action). The grant's
    scope comes from the pending record, not the request body:
    the client cannot widen what the gate asked for.

    The gate itself lives in server/tasks/approvals.py
    (approve_pending) — this endpoint is a thin HTTP wrapper
    around it, so the mission runner's auto-satisfy path (PR #5,
    Q1 standing approval) uses the exact same gate.
    """
    _task_or_404(task_id)
    conn = _conn()
    try:
        try:
            return approvals.approve_pending(conn, task_id, body.approval_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        conn.close()


@router.delete("/tasks/{task_id}/grants")
def revoke_grants(task_id: str, _token: str = Depends(require_token)):
    """Revoke every outstanding approval grant for a task (§5.4:
    the user can revoke any grant in one click)."""
    _task_or_404(task_id)
    conn = _conn()
    try:
        cur = conn.execute(
            "DELETE FROM grants WHERE origin = ? AND scope = 'approve'",
            (task_id,),
        )
        conn.commit()
        revoked = cur.rowcount
    finally:
        conn.close()
    return {"task_id": task_id, "revoked": revoked}


# Terminal states are final: nothing (pause, cancel, approve,
# resume) moves a task out of one. resume_task already enforced
# this; pause/cancel did not (Bug 4).
TERMINAL_TASK_STATUSES = ("cancelled", "failed", "done")


def _set_status(task_id: str, status: str) -> dict:
    task = _task_or_404(task_id)
    # Terminal states are final: pausing or cancelling a finished
    # task must not resurrect it (Bug 4: pause on a cancelled task
    # flipped it to "paused", and resume then worked).
    if task["status"] in TERMINAL_TASK_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"task {task_id} is {task['status']}; refusing to set {status}",
        )
    conn = _conn()
    try:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = strftime('%s','now') WHERE id = ?",
            (status, task_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"task_id": task_id, "status": status}


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, _token: str = Depends(require_token)):
    result = _set_status(task_id, "cancelled")
    # A cancelled task will never submit: release its role claims
    # at once instead of holding the roles for the claim TTL.
    conn = _conn()
    try:
        loop.release_claims_for_task(conn, task_id)
    finally:
        conn.close()
    return result


@router.post("/tasks/{task_id}/pause")
def pause_task(task_id: str, _token: str = Depends(require_token)):
    return _set_status(task_id, "paused")


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: str, _token: str = Depends(require_token)):
    """Resume after a human-only handoff: the user completed the
    human step, so the handoff steps are marked done and the task
    continues. If the handoff deadline passed, the task fails."""
    _task_or_404(task_id)
    conn = _conn()
    try:
        status = loop.resume_task(conn, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # A state refusal (resuming a done/cancelled/failed task)
        # is a conflict, not a server error.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        conn.close()
    return {"task_id": task_id, "status": status}
