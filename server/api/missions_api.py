"""Missions API (PR #5, plan §3.3). Token-authenticated, like the rest.

POST /missions                              {type, name, config?, bounds?} -> 201 {mission}
GET  /missions?status=                      -> {missions: [...]} (newest first)
GET  /missions/types                        -> the type registry (labels + schemas + defaults)
GET  /missions/approvals-pending            -> live pending approvals across all missions
GET  /missions/{id}                         -> mission + activity + tasks + pending approvals
PATCH /missions/{id}                        -> rename / reconfigure (not while active)
POST /missions/{id}/activate | /pause | /resume | /stop
POST /missions/stop-all                     -> stop every live mission (global kill switch)
DELETE /missions/{id}                       -> draft/stopped only
GET  /missions/{id}/activity?limit=&kind=   -> the mission's activity log
GET  /missions/{id}/approvals               -> live pending approvals for the mission
POST /missions/{id}/approve-mail            {approval_id, draft_id} -> approve a mail
                                            draft through the TASK pending-approval
                                            gate (never the mail endpoint's
                                            accept-any-id behaviour)

Envelopes follow the monitor convention: single resources bare,
lists wrapped. Errors: 404 unknown id, 409 state refusal, 422
validation.

Route order matters: the static GET routes (/missions/types,
/missions/approvals-pending) are registered BEFORE
/missions/{mission_id}, or "types" would be read as a mission id.
The runner router (missions_runner_api, W2) owns GET
/missions/due and must be mounted BEFORE this router in main.py,
for the same reason.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..core import db
from ..core.auth import require_token
from ..mail import drafts as draft_mod
from ..missions import links, models
from ..missions import uploads as uploads_mod
from ..tasks import approvals, loop

router = APIRouter(tags=["missions"])


class MissionIn(BaseModel):
    type: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=120)
    config: dict = Field(default_factory=dict)
    # Bounds optional — the type defaults fill them in.
    bounds: dict | None = Field(default=None)
    # PR #7: chat uploads the mission was created from (a
    # resume attached in chat) — linked to the mission for
    # provenance. Unknown ids are ignored.
    upload_ids: list[str] = Field(default_factory=list)


class MissionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    config: dict | None = Field(default=None)
    bounds: dict | None = Field(default=None)


class ApproveMailIn(BaseModel):
    approval_id: str = Field(..., min_length=1, max_length=120)
    draft_id: str = Field(..., min_length=1, max_length=120)


def _conn():
    return db.connect()


def _mission_or_404(conn, mission_id: str) -> dict:
    mission = models.get_mission(conn, mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail=f"no mission {mission_id}")
    return mission


def _json_safe(value) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def _schema_to_json(schema: dict) -> dict:
    """Serialise one type's config schema for the UI. The registry
    form (§3.7) is field -> (type, required, default, validator);
    the validator is a callable and is dropped — the UI gets the
    type name, the required flag and the default. Dict-form specs
    pass through with non-JSON values dropped."""
    out = {}
    for field, spec in (schema or {}).items():
        if isinstance(spec, dict):
            out[field] = {k: v for k, v in spec.items() if _json_safe(v)}
        elif isinstance(spec, (tuple, list)) and len(spec) >= 3:
            ftype, required, default = spec[0], spec[1], spec[2]
            out[field] = {
                "type": getattr(ftype, "__name__", str(ftype)),
                "required": bool(required),
                "default": default if _json_safe(default) else None,
            }
        else:
            out[field] = {"type": str(spec)}
    return out


@router.post("/missions", status_code=201)
def create_mission(body: MissionIn, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        try:
            mission = models.create_mission(
                conn, body.type, body.name, body.config, body.bounds or {})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if body.upload_ids:
            uploads_mod.link_uploads(
                conn, body.upload_ids, mission_id=mission["id"])
        return mission
    finally:
        conn.close()


@router.get("/missions")
def list_missions(status: str | None = None,
                  _token: str = Depends(require_token)):
    if status is not None and status not in models.MISSION_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {', '.join(models.MISSION_STATUSES)}",
        )
    conn = _conn()
    try:
        return {"missions": models.list_missions(conn, status)}
    finally:
        conn.close()


@router.get("/missions/types")
def list_mission_types(_token: str = Depends(require_token)):
    """The type registry: labels + config schemas + bounds
    defaults, so the UI never hardcodes a mission type."""
    # Imported here, not at module level: types.py is W2's file
    # (see models.create_mission).
    from ..missions import types as _types

    types = []
    for spec in _types.MISSION_TYPES.values():
        types.append({
            "type": spec.type,
            "label": spec.label,
            "config_schema": _schema_to_json(spec.config_schema),
            "default_bounds": spec.default_bounds,
            "default_interval_min": spec.default_interval_min,
        })
    return {"types": types}


@router.get("/missions/approvals-pending")
def list_all_pending_approvals(_token: str = Depends(require_token)):
    """Every live mission approval in one call — the extension
    alarm checks this once per cycle (§3.9) instead of one call
    per mission."""
    conn = _conn()
    try:
        return {"approvals": links.all_pending_approvals(conn)}
    finally:
        conn.close()


@router.get("/missions/{mission_id}")
def get_mission(mission_id: str, _token: str = Depends(require_token)):
    conn = _conn()
    try:
        mission = _mission_or_404(conn, mission_id)
        activity = models.list_activity(conn, mission_id, limit=20)
        tasks = links.tasks_for_mission(conn, mission_id)
        pending = links.pending_approvals_for_mission(conn, mission_id)
    finally:
        conn.close()
    return {**mission, "activity": activity, "tasks": tasks,
            "pending_approvals": pending}


@router.patch("/missions/{mission_id}")
def update_mission(mission_id: str, body: MissionPatch,
                   _token: str = Depends(require_token)):
    if body.name is None and body.config is None and body.bounds is None:
        raise HTTPException(
            status_code=422, detail="give a name, config, or bounds to update"
        )
    conn = _conn()
    try:
        try:
            return models.update_mission(
                conn, mission_id, name=body.name, config=body.config,
                bounds=body.bounds)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # IllegalTransitionError is a ValueError subclass — catch
        # it first, or every state refusal would surface as a 422.
        except models.IllegalTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()


def _transition(mission_id: str, to_status: str) -> dict:
    conn = _conn()
    try:
        try:
            return models.transition(conn, mission_id, to_status)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except models.IllegalTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/missions/{mission_id}/activate")
def activate_mission(mission_id: str, _token: str = Depends(require_token)):
    """draft|paused -> active; sets next_run_at = now."""
    return _transition(mission_id, "active")


@router.post("/missions/{mission_id}/pause")
def pause_mission(mission_id: str, _token: str = Depends(require_token)):
    """active -> paused; next_run_at = NULL."""
    return _transition(mission_id, "paused")


@router.post("/missions/{mission_id}/resume")
def resume_mission(mission_id: str, _token: str = Depends(require_token)):
    """paused -> active (activate from paused only — resuming a
    draft is an activate, not a resume)."""
    conn = _conn()
    try:
        mission = _mission_or_404(conn, mission_id)
        if mission["status"] != "paused":
            raise HTTPException(
                status_code=409,
                detail=f"mission {mission_id} is {mission['status']};"
                " only a paused mission can resume",
            )
        try:
            return models.transition(conn, mission_id, "active")
        except models.IllegalTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/missions/{mission_id}/stop")
def stop_mission(mission_id: str, _token: str = Depends(require_token)):
    """The kill switch: active|paused|draft -> stopped (TERMINAL).
    Revokes the mission's live grants, consumes its pending
    approvals, releases its role claims, cancels its
    non-terminal spawned tasks."""
    conn = _conn()
    try:
        try:
            return models.stop_mission(conn, mission_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except models.IllegalTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/missions/stop-all")
def stop_all_missions(_token: str = Depends(require_token)):
    """Global kill switch: stop every draft/active/paused
    mission. Terminal missions are left alone."""
    conn = _conn()
    try:
        stopped = []
        for mission in models.list_missions(conn):
            if mission["status"] in ("draft", "active", "paused"):
                models.stop_mission(conn, mission["id"])
                stopped.append(mission["id"])
        return {"stopped": stopped}
    finally:
        conn.close()


@router.delete("/missions/{mission_id}")
def delete_mission(mission_id: str, _token: str = Depends(require_token)):
    """draft|stopped only. Deletes the mission + its runs +
    activity; receipts/drafts/tasks are kept (audit trail)."""
    conn = _conn()
    try:
        try:
            models.delete_mission(conn, mission_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except models.IllegalTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"deleted": mission_id}
    finally:
        conn.close()


@router.get("/missions/{mission_id}/activity")
def get_mission_activity(
    mission_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    kind: str | None = None,
    _token: str = Depends(require_token),
):
    if kind is not None and kind not in models.ACTIVITY_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"kind must be one of {', '.join(models.ACTIVITY_KINDS)}",
        )
    conn = _conn()
    try:
        _mission_or_404(conn, mission_id)
        return {"mission_id": mission_id,
                "activity": models.list_activity(conn, mission_id,
                                                 limit=limit, kind=kind)}
    finally:
        conn.close()


@router.get("/missions/{mission_id}/approvals")
def get_mission_approvals(mission_id: str,
                          _token: str = Depends(require_token)):
    """Live pending approvals across the mission's spawned tasks.
    Approving a task approval uses the EXISTING
    POST /tasks/{id}/approve — the task gate is the gate."""
    conn = _conn()
    try:
        _mission_or_404(conn, mission_id)
        return {"mission_id": mission_id,
                "approvals": links.pending_approvals_for_mission(
                    conn, mission_id)}
    finally:
        conn.close()


def _live_grant_for_draft(conn, task_id: str, draft_id: str) -> str | None:
    """The id of a live approval grant naming this draft, or None
    (same query as mail_api._live_grant_for_draft — the approve
    endpoint and the send endpoint share one definition of "this
    draft is approved")."""
    import time

    now = int(time.time())
    rows = conn.execute(
        "SELECT id, detail FROM grants WHERE origin = ?"
        " AND scope = 'approve' AND expires_at > ?",
        (task_id, now),
    ).fetchall()
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if detail.get("draft_id") == draft_id:
            return row["id"]
    return None


@router.post("/missions/{mission_id}/approve-mail")
def approve_mail(mission_id: str, body: ApproveMailIn,
                 _token: str = Depends(require_token)):
    """Approve a mission mail draft for sending — through the
    TASK pending-approval gate, never the mail endpoint's
    accept-any-id behaviour (D8): the approval_id must name a
    live pending approval the runner recorded for this draft
    (action "mail_send", detail {"draft_id": ...}). On success a
    grant naming the draft is minted, so the existing
    POST /mail/drafts/{id}/send consumes it. A made-up id is a
    404; a wrong-mission draft, an expired approval, a consumed
    approval, or an approval for a different draft is a 409."""
    conn = _conn()
    try:
        _mission_or_404(conn, mission_id)
        draft = draft_mod.get_draft(conn, body.draft_id)
        if draft is None:
            raise HTTPException(
                status_code=404, detail=f"no draft {body.draft_id}")
        task = loop.get_task(conn, draft["task_id"])
        if task is None or task.get("mission_id") != mission_id:
            raise HTTPException(
                status_code=409,
                detail=f"draft {body.draft_id} does not belong to"
                f" mission {mission_id}",
            )
        if draft["status"] not in ("draft", "approved"):
            raise HTTPException(
                status_code=409,
                detail=f"draft {body.draft_id} is {draft['status']}",
            )
        if (draft["status"] == "approved"
                and _live_grant_for_draft(conn, draft["task_id"],
                                          body.draft_id) is not None):
            raise HTTPException(
                status_code=409,
                detail=f"draft {body.draft_id} is approved",
            )
        try:
            approvals.approve_pending(
                conn, draft["task_id"], body.approval_id,
                expect_draft_id=body.draft_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if draft["status"] == "draft":
            draft_mod.approve_draft(conn, body.draft_id)
        models.record_activity(
            conn, mission_id, "approved",
            {"draft_id": body.draft_id, "task_id": draft["task_id"]},
        )
        return {"draft_id": body.draft_id, "status": "approved"}
    finally:
        conn.close()
