"""Chat file uploads (PR #7, W2): store files the user attaches
in chat, extract their text locally, and derive PROPOSED profile
facts for mission intake.

Storage: ECLIPSE_DATA_DIR/uploads/ (config.DATA_DIR), filename
non-guessable (uuid hex + sanitised extension, never the user's
name verbatim), files written 0600 (an upload may be a resume —
PII). Metadata lives in the chat_uploads table (see
server/core/db.py): original name, size, mime, extraction
status, extracted text, proposed facts, chat linkage
(chat_message_id) and mission linkage (mission_id, set when a
mission is created from the upload).

Extraction, local only — file bytes NEVER go to a model:
- .txt / .md: decoded as UTF-8 directly.
- .pdf: pypdf (requirements.txt). When pypdf is unavailable or
  the parse fails, the stdlib-only extractor from resume.py is
  the fallback — a failed extraction is "limited", never faked.
- .docx: python-docx (requirements.txt); missing library or a
  bad file means "unsupported".
- images (.png/.jpg/.jpeg/.gif/.webp): stored, no text
  extraction (no OCR in Eclipse) — "unsupported".
Anything else is rejected at upload time (422), not stored.

Proposed facts: parse_resume (server/jobs/profile.py) runs
locally over the extracted text; the facts it finds are
PROPOSED — shown in chat, stored as profile facts only after
the user confirms (the intake draft card carries them; the UI
calls POST /jobs/profile on confirm). PII (email/phone) is part
of the proposal shown to the user in their own chat, but is
never sent to a model: the intake planner path only ever sees
the PII-redacted chat message, never resume text.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from ..core import config
from ..jobs import profile as profile_mod
from . import resume as resume_mod

UPLOADS_DIRNAME = "uploads"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_EXTRACTED_CHARS = 20000

ALLOWED_EXTENSIONS = (
    ".pdf", ".txt", ".md", ".docx",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
)
# Extensions whose bytes can yield text (the rest are stored
# only). Images are accepted uploads with no extraction.
TEXT_EXTENSIONS = (".txt", ".md", ".pdf", ".docx")

# The mime the browser claimed, mapped from the extension when
# the claim is missing or implausible — the extension is the
# authority (a client-supplied mime is untrusted input).
_MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Lines that state what the person wants to do next — the
# source of the proposed "target roles" fact.
_TARGET_ROLE_RE = re.compile(
    r"(?:seeking|looking\s+for|interested\s+in|aspiring\s+"
    r"(?:to\s+be\s+)?(?:a\s+|an\s+)?|target\s+role[s]?\s*:?)\s*"
    r"([A-Za-z][A-Za-z ./#+]{2,60})",
    re.IGNORECASE,
)


def uploads_dir() -> Path:
    """The uploads directory (created on demand)."""
    path = config.DATA_DIR / UPLOADS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_private(path: Path, data: bytes) -> None:
    """Write a file 0600 from the first byte (an upload may be
    PII) — the same os.open pattern as config.get_token."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _extract_pdf_text(data: bytes) -> str:
    """PDF text via pypdf, falling back to the stdlib-only
    extractor (resume.py) when pypdf is missing or the parse
    fails. Returns "" when nothing extractable is found."""
    try:
        from pypdf import PdfReader  # noqa: PLC0415 - optional dep, lazy
    except ImportError:
        return resume_mod.extract_pdf_text(data)
    try:
        from io import BytesIO

        reader = PdfReader(BytesIO(data))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - one bad page skips, not fails
                continue
        text = "\n".join(pages).strip()
        if text:
            return re.sub(r"[ \t]+", " ", text).strip()
    except Exception:  # noqa: BLE001 - any pypdf failure falls back
        pass
    return resume_mod.extract_pdf_text(data)


def _extract_docx_text(data: bytes) -> str | None:
    """DOCX text via python-docx. Returns None when the library
    is missing or the file is not a readable document (the
    caller records that as "unsupported", never as text)."""
    try:
        from docx import Document  # noqa: PLC0415 - optional dep, lazy
    except ImportError:
        return None
    try:
        from io import BytesIO

        doc = Document(BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs).strip()
        return text or None
    except Exception:  # noqa: BLE001 - a bad docx is "unsupported"
        return None


def extract_text(data: bytes, filename: str) -> tuple[str | None, str]:
    """Extract text from upload bytes. Returns (text_or_None,
    status) with status "ok" | "limited" | "unsupported".
    Never raises for bad input — a failure is "limited" (for a
    text format that yielded nothing) or "unsupported" (for a
    format with no extractor), not an exception."""
    suffix = Path(filename or "").suffix.lower()
    if suffix in (".txt", ".md"):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        text = text.strip()
        if not text:
            return None, "limited"
        return text[:MAX_EXTRACTED_CHARS], "ok"
    if suffix == ".pdf":
        try:
            text = _extract_pdf_text(data)
        except Exception:  # noqa: BLE001 - any parse failure is "limited"
            return None, "limited"
        if not text:
            return None, "limited"
        return text[:MAX_EXTRACTED_CHARS], "ok"
    if suffix == ".docx":
        text = _extract_docx_text(data)
        if text is None:
            return None, "unsupported"
        return text[:MAX_EXTRACTED_CHARS], "ok"
    return None, "unsupported"


def derive_proposed_facts(text: str | None) -> list[dict]:
    """Proposed profile facts from extracted resume text, as a
    list of {"field", "label", "value"} — shown in chat for the
    user to confirm before anything is stored. Empty when there
    is no text or nothing was found. Local only; the profile
    parser is deterministic regex, no model call."""
    if not text or not text.strip():
        return []
    parsed = profile_mod.parse_resume(text)
    facts: list[dict] = []

    def _add(field: str, label: str, value) -> None:
        if value is None or value == "" or value == []:
            return
        facts.append({"field": field, "label": label, "value": value})

    _add("name", "Name", parsed.get("name"))
    _add("email", "Email", parsed.get("email"))
    _add("phone", "Phone", parsed.get("phone"))
    _add("degree", "Degree", parsed.get("degree"))
    _add("year", "Year", parsed.get("year"))
    _add("cgpa", "CGPA", parsed.get("cgpa"))
    _add("skills", "Skills", parsed.get("skills"))
    _add("locations", "Locations", parsed.get("locations"))
    _add("stipend_min", "Minimum stipend (₹/month)",
         parsed.get("stipend_min"))
    targets: list[str] = []
    for match in _TARGET_ROLE_RE.finditer(text):
        role = match.group(1).strip().rstrip(".,;")
        if role and len(role) <= 60 and role not in targets:
            targets.append(role)
    _add("target_roles", "Target roles", targets[:5])
    return facts


def _row_to_meta(row: sqlite3.Row) -> dict:
    meta = dict(row)
    meta["uploaded_at"] = meta.pop("created_at")
    meta["filename"] = meta.pop("original_name")
    try:
        meta["proposed_facts"] = json.loads(
            meta.pop("proposed_facts_json") or "[]")
    except (ValueError, TypeError):
        meta["proposed_facts"] = []
    return meta


def get_upload(conn: sqlite3.Connection, upload_id: str) -> dict | None:
    """One upload's metadata, or None for an unknown id."""
    row = conn.execute(
        "SELECT * FROM chat_uploads WHERE id = ?", (upload_id,)
    ).fetchone()
    return _row_to_meta(row) if row is not None else None


def list_uploads(conn: sqlite3.Connection, limit: int = 50,
                 chat_message_id: str | None = None) -> list[dict]:
    """Recent uploads, newest first — optionally only the ones
    linked to one chat message (the thread renders its file
    chips from this)."""
    if chat_message_id is not None:
        rows = conn.execute(
            "SELECT * FROM chat_uploads WHERE chat_message_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (chat_message_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM chat_uploads"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_meta(r) for r in rows]


def link_uploads(conn: sqlite3.Connection, upload_ids: list[str],
                 chat_message_id: str | None = None,
                 mission_id: str | None = None) -> list[str]:
    """Stamp a chat-message id and/or mission id onto uploads.
    Returns the ids that were actually found (unknown ids are
    skipped, never an error — the caller already validated the
    ones it cares about)."""
    found: list[str] = []
    for upload_id in upload_ids or []:
        sets = []
        params: list = []
        if chat_message_id is not None:
            sets.append("chat_message_id = ?")
            params.append(chat_message_id)
        if mission_id is not None:
            sets.append("mission_id = ?")
            params.append(mission_id)
        if not sets:
            break
        params.append(upload_id)
        cur = conn.execute(
            f"UPDATE chat_uploads SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        if cur.rowcount:
            found.append(upload_id)
    conn.commit()
    return found


def resume_text_for_uploads(conn: sqlite3.Connection,
                            upload_ids: list[str]) -> str:
    """The extracted text of the given uploads, concatenated
    (capped at MAX_EXTRACTED_CHARS) — the resume context the
    intake enriches a draft with. Uploads with no extracted
    text contribute nothing. Never raises for unknown ids."""
    pieces: list[str] = []
    total = 0
    for upload_id in upload_ids or []:
        meta = get_upload(conn, upload_id)
        if meta is None:
            continue
        text = meta.get("extracted_text")
        if not text:
            continue
        pieces.append(text)
        total += len(text)
        if total >= MAX_EXTRACTED_CHARS:
            break
    return "\n\n".join(pieces)[:MAX_EXTRACTED_CHARS]


def proposed_facts_for_uploads(conn: sqlite3.Connection,
                               upload_ids: list[str]) -> list[dict]:
    """The proposed facts of the given uploads, merged (first
    value wins per field)."""
    merged: list[dict] = []
    seen_fields: set[str] = set()
    for upload_id in upload_ids or []:
        meta = get_upload(conn, upload_id)
        if meta is None:
            continue
        for fact in meta.get("proposed_facts") or []:
            if fact.get("field") not in seen_fields:
                seen_fields.add(fact["field"])
                merged.append(fact)
    return merged


def store_upload(conn: sqlite3.Connection, filename: str,
                 data: bytes, mime: str | None = None,
                 chat_message_id: str | None = None) -> dict:
    """Store an uploaded file and extract its text. Returns the
    metadata dict (also saved to the chat_uploads table).
    Raises ValueError for an empty file, one over the 10 MB
    cap, or an extension outside the allow-list."""
    if not data:
        raise ValueError("uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"file is over the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap"
        )
    safe = resume_mod.sanitise_filename(filename)
    suffix = Path(safe).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(
            "unsupported file type — upload a PDF, text, Markdown,"
            " Word (.docx) or image file"
        )
    # Non-guessable stored name: a uuid, never the user's name.
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    target = uploads_dir() / stored_name
    _write_private(target, data)
    text, status = extract_text(data, safe)
    facts = derive_proposed_facts(text)
    upload_id = f"upl-{uuid.uuid4().hex[:12]}"
    now = int(time.time())
    conn.execute(
        "INSERT INTO chat_uploads (id, original_name, stored_name,"
        " stored_path, size_bytes, mime, extraction_status,"
        " extracted_text, proposed_facts_json, chat_message_id,"
        " mission_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?,"
        " ?, ?, NULL, ?)",
        (upload_id, safe, stored_name, str(target), len(data),
         mime or _MIME_BY_EXTENSION.get(suffix, "application/octet-stream"),
         status, text, json.dumps(facts), chat_message_id, now),
    )
    conn.commit()
    meta = get_upload(conn, upload_id)
    assert meta is not None  # just inserted
    return meta
