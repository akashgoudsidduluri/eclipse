"""Smoke test the FastAPI server skeleton (Sprint 1).

Starts uvicorn on a scratch port with a temp data dir and a fixed
test token, then checks: token auth (401 without and with a wrong
token, 200 with the right one), /health, /quota seeds 5 models,
/audit/verify passes on the hash chain and FAILS on a tampered chain,
and the SQLite schema has all 18 tables with WAL mode.

The server is the only SQLite writer in production. The direct
sqlite appends and tampers in this file are test-only, to prove the
audit chain.
"""
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import typing
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-123"

EXPECTED_TABLES = {
    "tasks", "steps", "checkpoints", "memory", "facts", "routines",
    "audit_log", "rag_chunks", "watches", "grants", "quota", "receipts",
    "mail_drafts", "mail_log", "connectors", "calendar_events",
    "watch_events", "briefings", "pending_approvals",
    "missions", "mission_runs", "mission_activity",
    # PR #6: the Agent view's chat thread.
    "chat_messages",
    # PR #7: chat file uploads.
    "chat_uploads",
    # PR #5 fact store: the FTS5 virtual table + its shadow tables.
    "facts_fts", "facts_fts_config", "facts_fts_content",
    "facts_fts_data", "facts_fts_docsize", "facts_fts_idx",
}


def _free_port() -> int:
    """Pick an unused localhost port (small race, fine for tests)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(base_url: str, path: str, token: str | None = TOKEN):
    req = urllib.request.Request(f"{base_url}{path}")
    if token is not None:
        req.add_header("X-Eclipse-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())
    except urllib.error.URLError:
        # Server not up yet (poll loop) or down; caller decides.
        return 0, {}


def _start_server(env: dict, port: int):
    """Start uvicorn in a subprocess. Returns (proc, stderr_file)."""
    stderr_file = tempfile.NamedTemporaryFile(
        mode="w+", suffix=".log", delete=False
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=stderr_file,
    )
    return proc, stderr_file


def _wait_for_health(base_url: str, token: str | None,
                     timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = _request(base_url, "/health", token=token)
        if status == 200:
            return True
        time.sleep(0.5)
    return False


class _ServerTestCase(unittest.TestCase):
    """Shared start/stop for a server subprocess on a free port."""

    TOKEN_ENV: dict = {}
    port: int = 0
    base_url: str = ""
    _tmp: tempfile.TemporaryDirectory | None = None
    _proc: subprocess.Popen | None = None
    _stderr_file: typing.TextIO | None = None

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env["ECLIPSE_DATA_DIR"] = cls._tmp.name
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.update(cls.TOKEN_ENV)
        cls._proc, cls._stderr_file = _start_server(env, cls.port)

    @classmethod
    def tearDownClass(cls):
        if cls._proc is not None:
            cls._proc.terminate()
            try:
                cls._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls._proc.kill()
        if cls._stderr_file is not None:
            cls._stderr_file.close()
        if cls._tmp is not None:
            cls._tmp.cleanup()

    @classmethod
    def _read_stderr(cls) -> str:
        try:
            with open(cls._stderr_file.name, encoding="utf-8") as f:
                return f.read()[-2000:]
        except OSError:
            return "(stderr unavailable)"


class TestServerSmoke(_ServerTestCase):
    TOKEN_ENV = {"ECLIPSE_TOKEN": TOKEN}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not _wait_for_health(cls.base_url, TOKEN):
            cls._proc.kill()
            raise RuntimeError(
                "server did not start in 30s\n" + cls._read_stderr()
            )

    def test_health_requires_token(self):
        status, _ = _request(self.base_url, "/health", token=None)
        self.assertEqual(status, 401)

    def test_health_wrong_token(self):
        status, _ = _request(self.base_url, "/health", token="wrong-token")
        self.assertEqual(status, 401)

    def test_health_with_token(self):
        status, body = _request(self.base_url, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "eclipse")

    def test_quota_seeds_five_models(self):
        status, body = _request(self.base_url, "/quota")
        self.assertEqual(status, 200)
        models = {row["model"] for row in body["quota"]}
        self.assertEqual(len(models), 5)
        for row in body["quota"]:
            self.assertEqual(row["limit_day"], 200)
            self.assertEqual(row["used_today"], 0)

    def test_audit_chain_verifies(self):
        status, body = _request(self.base_url, "/audit/verify")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_audit_chain_after_append(self):
        # Test-only direct write: append one entry, then verify via API.
        sys.path.insert(0, str(REPO_ROOT))
        from server.core import audit, db

        conn = db.connect(Path(self._tmp.name) / "eclipse.db")
        try:
            entry = audit.append(conn, "task-test", "test.append",
                                 {"note": "smoke test entry"})
        finally:
            conn.close()
        self.assertIn("hash", entry)

        status, body = _request(self.base_url, "/audit/verify")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["checked"], 1)

        status, body = _request(
            self.base_url, "/audit?task_id=task-test"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["entries"]), 1)

    def test_audit_chain_tamper_is_caught(self):
        # Test-only direct write: append, then tamper with its hash.
        # A verifier that always returns ok would pass every other
        # test; this one must fail it.
        sys.path.insert(0, str(REPO_ROOT))
        from server.core import audit, db

        db_path = Path(self._tmp.name) / "eclipse.db"
        conn = db.connect(db_path)
        try:
            entry = audit.append(conn, "task-tamper", "test.tamper",
                                 {"note": "tamper test entry"})
            conn.execute(
                "UPDATE audit_log SET hash = ? WHERE id = ?",
                ("0" * 64, entry["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        try:
            status, body = _request(self.base_url, "/audit/verify")
            self.assertEqual(status, 200)
            self.assertFalse(body["ok"])
            self.assertEqual(body["first_bad_id"], entry["id"])
        finally:
            # Restore the original hash so later tests see a clean
            # chain — the tamper must not poison the shared test DB.
            conn = db.connect(db_path)
            try:
                conn.execute(
                    "UPDATE audit_log SET hash = ? WHERE id = ?",
                    (entry["hash"], entry["id"]),
                )
                conn.commit()
            finally:
                conn.close()

    def test_schema_tables_and_wal(self):
        import sqlite3

        conn = sqlite3.connect(Path(self._tmp.name) / "eclipse.db")
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            # sqlite_sequence exists because audit_log uses AUTOINCREMENT;
            # it is SQLite's internal table, not part of the schema.
            tables = {r[0] for r in rows} - {"sqlite_sequence"}
            self.assertEqual(tables, EXPECTED_TABLES)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode, "wal")
        finally:
            conn.close()


class TestServerTokenFile(_ServerTestCase):
    """Server without ECLIPSE_TOKEN: the token file must be created
    0600, and the token in it must authenticate."""

    # No ECLIPSE_TOKEN in the environment on purpose.
    TOKEN_ENV = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The server creates the token file on startup; poll for it.
        cls.token_file = Path(cls._tmp.name) / ".eclipse-token"
        deadline = time.time() + 30
        cls.file_token = None
        while time.time() < deadline:
            if cls.token_file.is_file():
                cls.file_token = cls.token_file.read_text().strip()
                if cls.file_token and _wait_for_health(
                    cls.base_url, cls.file_token, timeout=2
                ):
                    return
            time.sleep(0.5)
        cls._proc.kill()
        raise RuntimeError(
            "server did not start with a file token in 30s\n"
            + open(cls._stderr_file.name, encoding="utf-8").read()[-2000:]
        )

    def test_token_file_created_0600(self):
        mode = stat.S_IMODE(os.stat(self.token_file).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertTrue(self.file_token)

    def test_health_with_file_token(self):
        status, body = _request(
            self.base_url, "/health", token=self.file_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_health_without_token(self):
        status, _ = _request(self.base_url, "/health", token=None)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
