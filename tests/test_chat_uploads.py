"""Chat uploads + chat mission intake tests (PR #7, W2).

Run: /tmp/eclipse-venv/bin/python -m unittest tests.test_chat_uploads -v
Set TMPDIR to a workspace dir (e.g. TMPDIR=~/workspace/tmp-pytest):
/tmp is a 512M tmpfs and the API class starts a real server with a
temp data dir.

Three parts:
- TestUploadStorage: store_upload / extraction / proposed facts,
  direct module calls on a temp DATA_DIR (no server, no network).
- TestChatIntake: parse_intake for every mission type from a chat
  message, resume+message combined, the clarifying question, and
  the draft card. The planner is an injected call_fn.
- TestChatUploadsApi: the real HTTP surface (uvicorn subprocess,
  the test_chat.py pattern) — upload auth/caps/types, the intake
  endpoint with upload_ids + store_in_chat, mission creation
  linking uploads.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zlib
from pathlib import Path

from server.core import config, db
from server.missions import intake as intake_mod
from server.missions import uploads as uploads_mod
from server.planner.router import PlannerError

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-chat-uploads"

RESUME_TEXT = """Prateek Kumar
B.Tech IT, 3rd year, CGPA 8.75
Skills: Python, FastAPI, React
Looking for SDE intern roles, prefer Remote and Hyderabad
Minimum stipend 15000
prateek@example.com
98765 43210
"""


def _minimal_pdf(text: str) -> bytes:
    """A tiny one-page PDF with one FlateDecode content stream
    (the test_missions_runner.py pattern)."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    stream = zlib.compress(content)
    parts = [b"%PDF-1.4\n"]
    offsets = {}

    def _obj(num: int, body: bytes) -> bytes:
        offsets[num] = sum(len(p) for p in parts)
        return b"%d 0 obj\n" % num + body + b"\nendobj\n"

    parts.append(_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    parts.append(_obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"))
    parts.append(_obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox"
                     b" [0 0 612 792] /Contents 4 0 R"
                     b" /Resources << /Font << /F1 5 0 R >> >> >>"))
    parts.append(_obj(4, b"<< /Length %d /Filter /FlateDecode >>\n"
                     b"stream\n" % len(stream) + stream + b"\nendstream"))
    parts.append(_obj(5, b"<< /Type /Font /Subtype /Type1"
                     b" /BaseFont /Helvetica >>"))
    xref_pos = sum(len(p) for p in parts)
    body = b"".join(parts)
    body += b"xref\n0 6\n0000000000 65535 f \n"
    for num in range(1, 6):
        body += b"%010d 00000 n \n" % offsets[num]
    body += (b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
             + str(xref_pos).encode() + b"\n%%EOF\n")
    return body


def _minimal_docx(lines: list[str]) -> bytes:
    """A real .docx built with python-docx (in-memory)."""
    from io import BytesIO

    from docx import Document

    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


# A 1x1 transparent PNG.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000000020001e221bc330000000"
    "049454e44ae426082"
)


def _raising_call(exc: Exception):
    def _call(prompt: str, **kwargs):
        raise exc

    return _call


def _planner_call(payload: dict):
    """A fake call_fn returning one JSON draft, capturing prompts."""

    def _call(prompt: str, **kwargs):
        _call.prompts.append(prompt)
        return json.dumps(payload), "fake:model", 1.0

    _call.prompts = []
    return _call


class UploadsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_dir = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        db.migrate()
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.addCleanup(setattr, config, "DATA_DIR", self._old_dir)
        self.addCleanup(self.tmp.cleanup)

    def _upload_resume(self) -> dict:
        return uploads_mod.store_upload(
            self.conn, "Prateek_Resume.txt", RESUME_TEXT.encode("utf-8"))


# ----------------------------------------------------------------
# Storage + extraction (direct module calls)
# ----------------------------------------------------------------

class TestUploadStorage(UploadsTestCase):
    def test_txt_upload_extracts_and_proposes_facts(self):
        meta = self._upload_resume()
        self.assertEqual(meta["extraction_status"], "ok")
        self.assertIn("SDE intern roles", meta["extracted_text"])
        self.assertEqual(meta["filename"], "Prateek_Resume.txt")
        self.assertEqual(meta["size_bytes"], len(RESUME_TEXT.encode()))
        fields = {f["field"]: f["value"] for f in meta["proposed_facts"]}
        self.assertEqual(fields["name"], "Prateek Kumar")
        self.assertIn("Python", fields["skills"])
        self.assertIn("Hyderabad", fields["locations"])
        self.assertEqual(fields["stipend_min"], 15000)
        self.assertEqual(fields["cgpa"], 8.75)
        self.assertIn("SDE intern roles", fields["target_roles"])
        # PII is part of the proposal shown to the user in chat.
        self.assertEqual(fields["email"], "prateek@example.com")

    def test_pdf_upload_extracts_text(self):
        meta = uploads_mod.store_upload(
            self.conn, "resume.pdf",
            _minimal_pdf("Prateek Kumar, SDE Intern"))
        self.assertEqual(meta["extraction_status"], "ok")
        self.assertIn("Prateek Kumar", meta["extracted_text"])

    def test_pdf_without_text_is_limited(self):
        meta = uploads_mod.store_upload(
            self.conn, "blank.pdf", _minimal_pdf(""))
        self.assertEqual(meta["extraction_status"], "limited")
        self.assertIsNone(meta["extracted_text"])
        self.assertTrue(Path(meta["stored_path"]).exists())

    def test_docx_upload_extracts_text(self):
        meta = uploads_mod.store_upload(
            self.conn, "resume.docx",
            _minimal_docx(["Prateek Kumar", "Python, FastAPI"]))
        self.assertEqual(meta["extraction_status"], "ok")
        self.assertIn("Prateek Kumar", meta["extracted_text"])

    def test_image_upload_stored_without_extraction(self):
        meta = uploads_mod.store_upload(
            self.conn, "screenshot.png", _TINY_PNG)
        self.assertEqual(meta["extraction_status"], "unsupported")
        self.assertIsNone(meta["extracted_text"])
        self.assertEqual(meta["proposed_facts"], [])
        self.assertTrue(Path(meta["stored_path"]).exists())

    def test_stored_file_is_0600_and_name_is_not_guessable(self):
        meta = self._upload_resume()
        path = Path(meta["stored_path"])
        self.assertTrue(path.exists())
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        # The stored name is a uuid, never the user's filename.
        self.assertNotIn("Prateek", meta["stored_name"])
        self.assertTrue(meta["stored_name"].endswith(".txt"))
        self.assertEqual(meta["stored_name"][:32].isalnum(), True)
        self.assertTrue(str(path).startswith(str(uploads_mod.uploads_dir())))

    def test_oversize_upload_rejected(self):
        with self.assertRaises(ValueError):
            uploads_mod.store_upload(
                self.conn, "big.pdf", b"x" * (uploads_mod.MAX_UPLOAD_BYTES + 1))

    def test_empty_upload_rejected(self):
        with self.assertRaises(ValueError):
            uploads_mod.store_upload(self.conn, "empty.txt", b"")

    def test_unsupported_type_rejected(self):
        with self.assertRaises(ValueError):
            uploads_mod.store_upload(self.conn, "run.exe", b"MZ fake")

    def test_traversal_filename_is_sanitised(self):
        meta = uploads_mod.store_upload(
            self.conn, "../../etc/passwd.txt", b"hello\n")
        stored = Path(meta["stored_path"])
        self.assertTrue(stored.is_relative_to(uploads_mod.uploads_dir()))
        self.assertEqual(meta["filename"], "passwd.txt")

    def test_get_and_list_uploads(self):
        first = self._upload_resume()
        second = uploads_mod.store_upload(
            self.conn, "notes.md", b"# notes\n")
        self.assertEqual(
            uploads_mod.get_upload(self.conn, first["id"])["id"],
            first["id"])
        self.assertIsNone(uploads_mod.get_upload(self.conn, "upl-missing"))
        listed = uploads_mod.list_uploads(self.conn)
        self.assertEqual([u["id"] for u in listed],
                         [second["id"], first["id"]])

    def test_link_uploads_to_message_and_mission(self):
        meta = self._upload_resume()
        found = uploads_mod.link_uploads(
            self.conn, [meta["id"], "upl-missing"],
            chat_message_id="msg-abc", mission_id="m_xyz")
        self.assertEqual(found, [meta["id"]])
        relinked = uploads_mod.get_upload(self.conn, meta["id"])
        self.assertEqual(relinked["chat_message_id"], "msg-abc")
        self.assertEqual(relinked["mission_id"], "m_xyz")
        by_message = uploads_mod.list_uploads(
            self.conn, chat_message_id="msg-abc")
        self.assertEqual([u["id"] for u in by_message], [meta["id"]])

    def test_resume_text_for_unknown_upload_is_empty(self):
        self.assertEqual(
            uploads_mod.resume_text_for_uploads(self.conn, ["upl-nope"]),
            "")


# ----------------------------------------------------------------
# Chat mission intake (direct parse_intake calls)
# ----------------------------------------------------------------

class TestChatIntake(UploadsTestCase):
    def _rules(self, message: str, **kwargs) -> dict:
        return intake_mod.parse_intake(
            message, call_fn=_raising_call(PlannerError("down")),
            **kwargs)

    def test_job_hunt_from_chat_message(self):
        result = self._rules("keep applying for ML intern roles")
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["draft"]["type"], "job_hunt")
        self.assertIn("apply", result["draft_card"].lower())
        self.assertIsNone(result["clarifying_question"])

    def test_cold_mail_from_chat_message(self):
        result = self._rules(
            "send cold mails to hiring@example.com about SDE roles")
        self.assertEqual(result["draft"]["type"], "cold_mail")
        self.assertEqual(result["draft"]["config"]["recipients"],
                         ["hiring@example.com"])
        self.assertIn("cold mail", result["draft_card"].lower())

    def test_price_watch_from_chat_message(self):
        result = self._rules(
            "watch the price of https://example.com/phone and tell"
            " me when it drops below ₹30,000")
        self.assertEqual(result["draft"]["type"], "price_watch")
        self.assertEqual(result["draft"]["config"]["target_price"], 30000)
        self.assertIn("watch", result["draft_card"].lower())

    def test_email_monitor_from_chat_message(self):
        result = self._rules("watch my inbox for interview mails")
        self.assertEqual(result["draft"]["type"], "email_monitor")
        self.assertEqual(result["draft"]["config"]["keywords"],
                         ["interview"])
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["missing_fields"], [])
        self.assertIn("inbox", result["draft_card"].lower())

    def test_email_monitor_default_keywords_when_none_named(self):
        result = self._rules("keep an eye on my inbox")
        self.assertEqual(result["draft"]["type"], "email_monitor")
        self.assertEqual(
            result["draft"]["config"]["keywords"],
            ["interview", "offer", "shortlisted", "selected"])

    def test_email_monitor_planner_path(self):
        call = _planner_call({
            "type": "email_monitor",
            "name": "Inbox watch — interview",
            "config": {"keywords": ["interview"]},
            "bounds": {},
            "missing_fields": [],
        })
        result = intake_mod.parse_intake(
            "watch my inbox for interview mails", call_fn=call)
        self.assertEqual(result["source"], "planner")
        self.assertEqual(result["draft"]["type"], "email_monitor")
        self.assertIn("inbox", result["draft_card"].lower())

    def test_resume_plus_message_combined_job_hunt(self):
        meta = self._upload_resume()
        result = self._rules(
            "keep applying for ML intern roles",
            upload_ids=[meta["id"]], conn=self.conn)
        self.assertEqual(result["draft"]["type"], "job_hunt")
        # Locations + stipend came from the resume, so nothing
        # is missing and confidence is high.
        self.assertEqual(result["draft"]["config"]["locations"],
                         ["Remote", "Hyderabad"])
        self.assertEqual(result["draft"]["config"]["stipend_min"], 15000)
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["upload_ids"], [meta["id"]])
        fields = {f["field"] for f in result["proposed_facts"]}
        self.assertIn("name", fields)
        self.assertIn("skills", fields)
        self.assertIn("From your resume", result["draft_card"])

    def test_resume_plus_message_combined_cold_mail(self):
        meta = self._upload_resume()
        result = self._rules(
            "send cold mails to hiring@example.com about SDE roles",
            upload_ids=[meta["id"]], conn=self.conn)
        self.assertEqual(result["draft"]["type"], "cold_mail")
        # An uploaded resume is attached to the mails.
        self.assertTrue(result["draft"]["config"]["attach_resume"])
        self.assertTrue(result["proposed_facts"])

    def test_resume_text_never_reaches_the_planner(self):
        call = _planner_call({
            "type": "job_hunt", "name": "Hunt",
            "config": {"locations": ["Hyderabad"]},
            "bounds": {}, "missing_fields": [],
        })
        intake_mod.parse_intake(
            "keep applying for ML intern roles",
            call_fn=call, resume_text=RESUME_TEXT)
        self.assertEqual(len(call.prompts), 1)
        prompt = call.prompts[0]
        self.assertNotIn("prateek@example.com", prompt)
        self.assertNotIn("98765 43210", prompt)
        self.assertNotIn("Prateek Kumar", prompt)
        self.assertNotIn("8.75", prompt)

    def test_clarifying_question_for_missing_url(self):
        result = self._rules("watch the price of those headphones")
        self.assertEqual(result["draft"]["type"], "price_watch")
        self.assertEqual(
            result["clarifying_question"],
            "Which product page should I watch? Paste the link.")

    def test_clarifying_question_for_missing_recipients(self):
        result = self._rules("send cold mails to recruiters")
        self.assertEqual(result["draft"]["type"], "cold_mail")
        self.assertEqual(
            result["clarifying_question"],
            "Who should I mail? Give me the email addresses.")

    def test_clarifying_question_for_missing_target_price(self):
        result = self._rules(
            "watch the price of https://example.com/phone and tell"
            " me when it drops below")
        self.assertEqual(result["draft"]["type"], "price_watch")
        self.assertEqual(
            result["clarifying_question"],
            "Below what price should I tell you?")

    def test_no_clarifying_question_when_not_blocked(self):
        # A job hunt without locations/stipend is still a usable
        # draft (the profile fills them) — no question is asked.
        result = self._rules("watch for SDE intern roles")
        self.assertIsNone(result["clarifying_question"])
        # A selector is never asked for in chat.
        result = self._rules(
            "watch the price of https://example.com/phone")
        self.assertIn("selector", result["missing_fields"])
        self.assertIsNone(result["clarifying_question"])


# ----------------------------------------------------------------
# HTTP surface (real uvicorn subprocess, the test_chat.py pattern)
# ----------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _multipart_body(filename: str, data: bytes,
                    fields: dict[str, str] | None = None):
    """A multipart/form-data body with a 'file' part (+ text
    fields). Returns (body, content_type)."""
    boundary = "----eclipse-test-boundary"
    chunks: list[bytes] = []
    for key, value in (fields or {}).items():
        chunks.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{key}\"\r\n\r\n"
            f"{value}\r\n".encode("utf-8"))
    chunks.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\";"
        f" filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
        + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class TestChatUploadsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env["ECLIPSE_DATA_DIR"] = cls.tmp.name
        env["ECLIPSE_TOKEN"] = TOKEN
        env.pop("ANTSEED_API_KEY", None)
        env.pop("GROQ_API_KEY", None)
        env["ANTSEED_USE_SURROGATE"] = "0"
        env["OLLAMA_BASE_URL"] = "http://127.0.0.1:1/v1"
        venv_python = REPO_ROOT / ".venv" / "bin" / "python"
        python = str(venv_python) if venv_python.exists() else sys.executable
        cls.proc = subprocess.Popen(
            [python, "-m", "uvicorn", "server.main:app",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            status, _ = cls._request("GET", "/health")
            if status == 200:
                break
            time.sleep(0.3)
        else:
            cls.proc.terminate()
            raise RuntimeError("server did not come up for upload API test")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.tmp.cleanup()

    @classmethod
    def _request(cls, method: str, path: str, token: str | None = TOKEN,
                 data: bytes | None = None,
                 content_type: str | None = None):
        req = urllib.request.Request(f"{cls.base}{path}", method=method)
        if token is not None:
            req.add_header("X-Eclipse-Token", token)
        if content_type is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, data=data, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"detail": raw}
        except urllib.error.URLError:
            # Server not up yet (poll loop) or down; caller decides.
            return 0, {}

    @classmethod
    def _json(cls, method: str, path: str, payload: dict,
              token: str | None = TOKEN):
        return cls._request(method, path, token=token,
                            data=json.dumps(payload).encode(),
                            content_type="application/json")

    def _upload(self, filename: str, data: bytes,
                token: str | None = TOKEN, fields=None):
        body, content_type = _multipart_body(filename, data, fields)
        return self._request("POST", "/chat/uploads", token=token,
                             data=body, content_type=content_type)

    def test_upload_txt_happy_path(self):
        status, body = self._upload(
            "resume.txt", RESUME_TEXT.encode("utf-8"))
        self.assertEqual(status, 201)
        self.assertEqual(body["filename"], "resume.txt")
        self.assertEqual(body["extraction_status"], "ok")
        self.assertIn("SDE intern roles", body["extracted_text"])
        fields = {f["field"] for f in body["proposed_facts"]}
        self.assertIn("name", fields)
        self.assertIn("File stored", body["note"])
        # The stored file on disk is 0600.
        mode = stat.S_IMODE(Path(body["stored_path"]).stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_upload_pdf_extracts(self):
        status, body = self._upload(
            "resume.pdf", _minimal_pdf("Prateek Kumar, SDE Intern"))
        self.assertEqual(status, 201)
        self.assertEqual(body["extraction_status"], "ok")
        self.assertIn("Prateek Kumar", body["extracted_text"])

    def test_upload_docx_extracts(self):
        status, body = self._upload(
            "resume.docx", _minimal_docx(["Prateek Kumar", "Python"]))
        self.assertEqual(status, 201)
        self.assertEqual(body["extraction_status"], "ok")
        self.assertIn("Prateek Kumar", body["extracted_text"])

    def test_upload_image_is_stored_not_extracted(self):
        status, body = self._upload("shot.png", _TINY_PNG)
        self.assertEqual(status, 201)
        self.assertEqual(body["extraction_status"], "unsupported")
        self.assertIsNone(body["extracted_text"])

    def test_upload_oversize_rejected(self):
        status, _body = self._upload(
            "big.pdf", b"x" * (uploads_mod.MAX_UPLOAD_BYTES + 1))
        self.assertEqual(status, 422)

    def test_upload_bad_type_rejected(self):
        status, _body = self._upload("run.exe", b"MZ fake")
        self.assertEqual(status, 422)

    def test_upload_requires_multipart(self):
        status, _body = self._request(
            "POST", "/chat/uploads", data=b"not multipart",
            content_type="text/plain")
        self.assertEqual(status, 422)

    def test_upload_no_file_part_rejected(self):
        boundary = "----eclipse-test-boundary"
        body = (
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"other\"\r\n\r\n"
            f"value\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        status, _ = self._request(
            "POST", "/chat/uploads", data=body,
            content_type=f"multipart/form-data; boundary={boundary}")
        self.assertEqual(status, 422)

    def test_upload_unauthenticated_rejected(self):
        status, _body = self._upload(
            "resume.txt", b"hello\n", token=None)
        self.assertEqual(status, 401)
        status, _body = self._upload(
            "resume.txt", b"hello\n", token="wrong-token")
        self.assertEqual(status, 401)

    def test_get_and_list_uploads(self):
        status, created = self._upload("notes.md", b"# hello\n")
        self.assertEqual(status, 201)
        status, one = self._request(
            "GET", f"/chat/uploads/{created['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(one["filename"], "notes.md")
        status, missing = self._request("GET", "/chat/uploads/upl-nope")
        self.assertEqual(status, 404)
        status, listed = self._request("GET", "/chat/uploads?limit=50")
        self.assertEqual(status, 200)
        ids = [u["id"] for u in listed["uploads"]]
        self.assertIn(created["id"], ids)

    def test_intake_with_upload_and_store_in_chat(self):
        status, upload = self._upload(
            "resume.txt", RESUME_TEXT.encode("utf-8"))
        self.assertEqual(status, 201)
        status, result = self._json("POST", "/missions/intake", {
            "message": "keep applying for ML intern roles",
            "upload_ids": [upload["id"]],
            "store_in_chat": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["draft"]["type"], "job_hunt")
        self.assertEqual(result["draft"]["config"]["locations"],
                         ["Remote", "Hyderabad"])
        self.assertTrue(result["proposed_facts"])
        self.assertIn("chat_message_id", result)
        # The message + draft-card reply are in the thread.
        status, history = self._request("GET", "/chat/history?limit=50")
        self.assertEqual(status, 200)
        texts = [m["text"] for m in history["messages"]]
        self.assertIn("keep applying for ML intern roles", texts)
        # The upload is linked to the stored user message.
        status, linked = self._request(
            "GET",
            f"/chat/uploads?chat_message_id={result['chat_message_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(
            [u["id"] for u in linked["uploads"]], [upload["id"]])

    def test_intake_without_store_in_chat_leaves_thread_alone(self):
        status, before = self._request("GET", "/chat/history?limit=200")
        self.assertEqual(status, 200)
        count_before = len(before["messages"])
        status, result = self._json("POST", "/missions/intake", {
            "message": "watch my inbox for interview mails",
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["draft"]["type"], "email_monitor")
        self.assertNotIn("chat_message_id", result)
        status, after = self._request("GET", "/chat/history?limit=200")
        self.assertEqual(len(after["messages"]), count_before)

    def test_create_mission_links_uploads(self):
        status, upload = self._upload(
            "resume.txt", RESUME_TEXT.encode("utf-8"))
        self.assertEqual(status, 201)
        status, mission = self._json("POST", "/missions", {
            "type": "job_hunt",
            "name": "Job hunt — test",
            "config": {"locations": ["Hyderabad"]},
            "upload_ids": [upload["id"]],
        })
        self.assertEqual(status, 201)
        status, relinked = self._request(
            "GET", f"/chat/uploads/{upload['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(relinked["mission_id"], mission["id"])


if __name__ == "__main__":
    unittest.main()
