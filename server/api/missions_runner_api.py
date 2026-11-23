"""Missions runner API (PR #5, W2): intake + the extension pull
endpoints + run-now + resume handling.

- POST /missions/intake {message, upload_ids?, chat_message_id?,
  store_in_chat?} -> a mission draft (never creates a mission;
  planner failure falls through to rules). PR #7: upload_ids
  attach chat-uploaded files (a resume) to the intake — the
  extracted text enriches the draft locally and the resume's
  profile facts come back as PROPOSED facts; store_in_chat
  (default false) also stores the message + the draft-card
  reply in the chat thread, so a mission set up from chat
  stays in the thread.
- GET /missions/due -> browser jobs waiting for the extension
  (also the extension heartbeat: every call updates
  memory["missions:last_extension_seen_at"]).
- POST /missions/check-in {} -> lightweight heartbeat.
- POST /missions/{id}/run-now -> schedule an immediate tick
  (sets next_run_at = now; does NOT run inline).
- POST /missions/resume (multipart file upload) -> store the
  resume + extract its text (stdlib only, see
  server/missions/resume.py).
- POST /missions/resume-text {text} -> store pasted resume text.
- GET /missions/resume -> the stored resume's metadata.

All routes token-gated. Mount order matters (W1's main.py
edit): this router must be mounted BEFORE missions_api, or
GET /missions/due and GET /missions/resume would be read as
mission ids by W1's GET /missions/{mission_id} (W1's router
docstring already records the /due half of this).

Multipart note: python-multipart is NOT in requirements.txt
and PR #5 adds no new dependencies, so the resume upload is
parsed with the stdlib `email` package (see
_parse_multipart_file) instead of FastAPI's UploadFile.
"""
from __future__ import annotations

import sqlite3
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..core import db
from ..core.auth import require_token
from ..missions import intake as intake_mod
from ..missions import resume as resume_mod
from ..missions import runner
from ..missions import models
from ..missions import uploads as uploads_mod

router = APIRouter(tags=["missions"])


def _conn():
    return db.connect()


def _heartbeat(conn) -> int:
    now = int(time.time())
    runner._memory_set(conn, runner.LAST_EXTENSION_SEEN_KEY, str(now))
    return now


class IntakeIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    # PR #7: chat uploads attached to the message (a resume).
    # Their extracted text enriches the draft locally; unknown
    # ids are ignored (the intake still works on the message).
    upload_ids: list[str] = Field(default_factory=list)
    # The chat message this intake belongs to (when the caller
    # already stored one) — stamped onto the uploads.
    chat_message_id: str | None = Field(default=None, max_length=120)
    # Store the message + the draft-card reply in the chat
    # thread (the Agent view's flow). Default false: callers
    # that are not the chat thread (the Missions view form)
    # keep the old behaviour — no thread rows.
    store_in_chat: bool = False


class ResumeTextIn(BaseModel):
    text: str = Field(..., min_length=10, max_length=20000)
    filename: str = Field(default="resume.txt", max_length=200)


def _store_chat_pair(conn, user_text: str, agent_text: str,
                     source: str | None = None) -> tuple[str, str]:
    """Store one user message + one agent reply in
    chat_messages (the same shape chat_api._store writes — the
    intake router writes its own rows rather than importing the
    chat router's private helper). Returns (user_id, agent_id).
    The agent row's kind is "chat": a draft card is a reply,
    not a task. `source` labels how the reply was made
    ("planner"/"rules"), like chat_api does."""
    now = int(time.time())
    user_id = f"msg-{uuid.uuid4().hex[:12]}"
    agent_id = f"msg-{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO chat_messages (id, role, text, kind, task_id,"
        " source, created_at) VALUES (?, 'user', ?, 'chat', NULL,"
        " NULL, ?)",
        (user_id, user_text, now),
    )
    conn.execute(
        "INSERT INTO chat_messages (id, role, text, kind, task_id,"
        " source, created_at) VALUES (?, 'agent', ?, 'chat', NULL,"
        " ?, ?)",
        (agent_id, agent_text, source, now),
    )
    conn.commit()
    return user_id, agent_id


@router.post("/missions/intake")
def post_intake(body: IntakeIn, _token: str = Depends(require_token)):
    """Draft a mission from one chat message. Planner failure
    is NOT an error — it falls through to the rule-based path
    (source "rules"). With upload_ids, the uploads' extracted
    text enriches the draft locally (never sent to a model)
    and their proposed facts come back for the user to confirm
    in chat. With store_in_chat, the message + the draft-card
    reply are stored in the chat thread and the uploads are
    linked to the stored user message."""
    conn = _conn()
    try:
        try:
            result = intake_mod.parse_intake(
                body.message, upload_ids=body.upload_ids, conn=conn)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        message_id = body.chat_message_id
        if body.store_in_chat:
            reply = result["draft_card"]
            if result.get("clarifying_question"):
                reply += "\n\n" + result["clarifying_question"]
            user_id, agent_id = _store_chat_pair(
                conn, body.message.strip(), reply,
                source=result.get("source"))
            message_id = user_id
            result["chat_message_id"] = user_id
            result["chat_reply_id"] = agent_id
        if message_id and body.upload_ids:
            uploads_mod.link_uploads(
                conn, body.upload_ids, chat_message_id=message_id)
        return result
    finally:
        conn.close()


@router.get("/missions/due")
def due_missions(_token: str = Depends(require_token)):
    """Browser work waiting for the extension (the missions
    alarm pulls this every 15 minutes). Also the extension
    heartbeat. An empty list means nothing needs the browser."""
    conn = _conn()
    try:
        now = _heartbeat(conn)
        try:
            jobs = runner.due_browser_jobs(conn, now)
        except sqlite3.OperationalError:
            # Missions tables not migrated yet (pre-PR #5 DB) —
            # the heartbeat still counts, and there is no work.
            jobs = []
        return {"missions": jobs, "server_time": now}
    finally:
        conn.close()


@router.post("/missions/check-in")
def check_in(_token: str = Depends(require_token)):
    """Lightweight heartbeat: the extension reports it is alive
    even when it has no browser work."""
    conn = _conn()
    try:
        now = _heartbeat(conn)
        return {"ok": True, "server_time": now}
    finally:
        conn.close()


@router.post("/missions/{mission_id}/run-now")
def run_now(mission_id: str, _token: str = Depends(require_token)):
    """Schedule an immediate tick for an active or paused
    mission (sets next_run_at = now). Does NOT run inline —
    the runner thread picks it up on its next tick (<=60 s).
    A paused mission stays paused; the tick runs it as soon as
    it is resumed."""
    conn = _conn()
    try:
        mission = models.get_mission(conn, mission_id)
        if mission is None:
            raise HTTPException(
                status_code=404, detail=f"no mission {mission_id}"
            )
        if mission["status"] not in ("active", "paused"):
            raise HTTPException(
                status_code=409,
                detail=f"mission {mission_id} is {mission['status']};"
                " run-now needs an active or paused mission",
            )
        now = int(time.time())
        conn.execute(
            "UPDATE missions SET next_run_at = ?, updated_at = ?"
            " WHERE id = ?",
            (now, now, mission_id),
        )
        conn.commit()
        return models.get_mission(conn, mission_id)
    finally:
        conn.close()


def _parse_multipart_file(body: bytes, content_type: str):
    """Parse a multipart/form-data body with the stdlib `email`
    package (no python-multipart in requirements.txt). Returns
    (filename, file_bytes) for the part named "file", or
    (None, None) when there is no such part."""
    from email import policy
    from email.parser import BytesParser

    header = (
        b"Content-Type: " + content_type.encode("utf-8", "replace")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
    )
    try:
        message = BytesParser(policy=policy.default).parsebytes(header + body)
    except Exception:  # noqa: BLE001 - an unparseable body is "no file part"
        return None, None
    if not message.is_multipart():
        return None, None
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if name != "file":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return part.get_filename(), None
        return part.get_filename(), payload
    return None, None


def _resume_response(meta: dict) -> dict:
    """The resume metadata plus a plain-English note about the
    extraction outcome (the caller is told when extraction was
    limited — never left to guess)."""
    status = meta.get("extraction_status")
    if status == "ok":
        note = "Resume stored; text extracted."
    elif status == "limited":
        note = (
            "Resume stored, but text extraction found little or"
            " nothing in it — the file is kept and can still be"
            " attached to mails."
        )
    else:  # unsupported
        note = (
            "Resume stored, but this format cannot be"
            " text-extracted — the file is kept and can still be"
            " attached to mails."
        )
    return {**meta, "note": note}


@router.post("/missions/resume")
async def upload_resume(request: Request,
                        _token: str = Depends(require_token)):
    """Upload a resume file (multipart/form-data, part "file").
    Stored under the server data dir, text extracted stdlib-only
    (plain text directly, PDFs best-effort). An unsupported
    format or a failed extraction still stores the file and
    says so — extracted text is never faked."""
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type.lower():
        raise HTTPException(
            status_code=422,
            detail="expected multipart/form-data with a 'file' part",
        )
    body = await request.body()
    filename, data = _parse_multipart_file(body, content_type)
    if data is None:
        raise HTTPException(
            status_code=422, detail="no 'file' part in the upload"
        )
    conn = _conn()
    try:
        try:
            meta = resume_mod.store_resume(conn, filename or "resume", data)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()
    return _resume_response(meta)


@router.post("/missions/resume-text")
def upload_resume_text(body: ResumeTextIn,
                       _token: str = Depends(require_token)):
    """Store pasted resume text (stored as a .txt file; the text
    IS the extraction, so it is always "ok")."""
    conn = _conn()
    try:
        try:
            meta = resume_mod.store_resume_text(
                conn, body.text, filename=body.filename)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()
    return _resume_response(meta)


@router.get("/missions/resume")
def get_resume(_token: str = Depends(require_token)):
    """The stored resume's metadata (filename, extraction
    status, extracted text). 404 when no resume was stored."""
    conn = _conn()
    try:
        meta = resume_mod.get_resume_meta(conn)
        if meta is None:
            raise HTTPException(
                status_code=404, detail="no resume stored yet"
            )
        return _resume_response(meta)
    finally:
        conn.close()
