"""Chat uploads API (PR #7, W2): files attached in chat.

- POST /chat/uploads (multipart/form-data, part "file",
  optional text field "chat_message_id") -> 201 with the
  upload's metadata: original name, size, mime, extraction
  status, extracted text (local only — never sent to a model),
  proposed profile facts (for the user to confirm in chat),
  and a plain-English note about the extraction outcome.
- GET /chat/uploads/{upload_id} -> one upload's metadata.
- GET /chat/uploads?chat_message_id= -> recent uploads,
  newest first (the thread renders its file chips from this).

All routes token-gated, like the rest. 10 MB cap; allowed
types are PDF, text, Markdown, Word (.docx) and common images
(see server/missions/uploads.py). An oversized file, an empty
file, or an unsupported type is a 422 — nothing is stored.

Multipart note: python-multipart is NOT in requirements.txt,
so the upload is parsed with the stdlib `email` package (the
same approach as the resume upload in missions_runner_api.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..core import db
from ..core.auth import require_token
from ..missions import uploads as uploads_mod

router = APIRouter(tags=["chat"])


def _conn():
    return db.connect()


def _parse_multipart(body: bytes, content_type: str):
    """Parse a multipart/form-data body with the stdlib `email`
    package (no python-multipart in requirements.txt). Returns
    (filename, file_bytes, fields) for the part named "file"
    plus any text fields; (None, None, {}) when there is no
    such part."""
    from email import policy
    from email.parser import BytesParser

    header = (
        b"Content-Type: " + content_type.encode("utf-8", "replace")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
    )
    try:
        message = BytesParser(policy=policy.default).parsebytes(header + body)
    except Exception:  # noqa: BLE001 - an unparseable body is "no file part"
        return None, None, {}
    if not message.is_multipart():
        return None, None, {}
    filename = None
    data = None
    fields: dict[str, str] = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if name == "file":
            payload = part.get_payload(decode=True)
            filename = part.get_filename()
            data = payload if isinstance(payload, bytes) else None
        elif isinstance(name, str):
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                fields[name] = payload.decode("utf-8", errors="replace")
    return filename, data, fields


def _upload_response(meta: dict) -> dict:
    """The upload metadata plus a plain-English note about the
    extraction outcome (the caller is told when extraction was
    limited or unsupported — never left to guess)."""
    status = meta.get("extraction_status")
    if status == "ok":
        note = "File stored; text extracted."
    elif status == "limited":
        note = (
            "File stored, but text extraction found little or"
            " nothing in it — the file is kept and can still be"
            " used for the mission."
        )
    else:  # unsupported
        note = (
            "File stored, but this format cannot be"
            " text-extracted — the file is kept and can still be"
            " used for the mission."
        )
    return {**meta, "note": note}


@router.post("/chat/uploads", status_code=201)
async def upload_chat_file(request: Request,
                           _token: str = Depends(require_token)):
    """Upload a file attached in chat (multipart/form-data,
    part "file"). Stored under the server data dir, 0600, with
    a non-guessable name; text extracted locally (pypdf /
    python-docx / plain text). The response carries the
    proposed profile facts when the file looks like a resume —
    the user confirms them in chat before anything is stored
    as profile facts."""
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type.lower():
        raise HTTPException(
            status_code=422,
            detail="expected multipart/form-data with a 'file' part",
        )
    body = await request.body()
    filename, data, fields = _parse_multipart(body, content_type)
    if data is None:
        raise HTTPException(
            status_code=422, detail="no 'file' part in the upload"
        )
    chat_message_id = fields.get("chat_message_id") or None
    conn = _conn()
    try:
        try:
            meta = uploads_mod.store_upload(
                conn, filename or "upload", data,
                mime=None, chat_message_id=chat_message_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()
    return _upload_response(meta)


@router.get("/chat/uploads/{upload_id}")
def get_upload(upload_id: str, _token: str = Depends(require_token)):
    """One upload's metadata. 404 for an unknown id."""
    conn = _conn()
    try:
        meta = uploads_mod.get_upload(conn, upload_id)
        if meta is None:
            raise HTTPException(
                status_code=404, detail=f"no upload {upload_id}")
        return _upload_response(meta)
    finally:
        conn.close()


@router.get("/chat/uploads")
def list_uploads(
    chat_message_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _token: str = Depends(require_token),
):
    """Recent uploads, newest first — optionally only the ones
    linked to one chat message."""
    conn = _conn()
    try:
        uploads = uploads_mod.list_uploads(
            conn, limit=limit, chat_message_id=chat_message_id)
    finally:
        conn.close()
    return {"uploads": [_upload_response(u) for u in uploads]}
