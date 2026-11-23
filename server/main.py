"""Eclipse local server. FastAPI, localhost:8000, bind 127.0.0.1 only.

Run from the repo root:
    .venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8000

Model keys live in a .env file at the repo root (see .env.example)
or in the real environment — never in the repo (.env is
gitignored). The server loads .env at import time, before any
server module reads the environment (PR #7 fix: the server
NEVER loaded .env, so keys in a .env file never reached the
process and the planner chain was dead on a laptop).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Logging first: uvicorn only configures its own loggers, so
# without this the root logger's WARNING default swallows every
# eclipse.* INFO line — including the brain startup line below.
# basicConfig is a no-op when a handler already exists (tests).
logging.basicConfig(level=logging.INFO)

# .env FIRST — before any server import that reads env. config
# reads ECLIPSE_DATA_DIR at import time, and the planner router
# reads provider keys when the chain is built, so both must see
# the loaded vars. Real environment variables win (override=False
# is the default). The repo-root .env loads first so the server
# works from any cwd; a .env in the working directory is honoured
# too. Never log the values — names only, in the startup line.
#
# Both paths are EXPLICIT — a bare load_dotenv() does NOT do
# what it looks like. python-dotenv's find_dotenv(usecwd=False)
# starts from the calling file's directory (server/), not the
# process cwd — it only searches the cwd in interactive
# contexts (python -c, REPL). Verified 2026-11-23: under
# uvicorn a bare load_dotenv() missed a .env in the working
# directory entirely, while Path.cwd() / ".env" finds it.
_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env")
load_dotenv(Path.cwd() / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import audit_api, chat_api, connectors_api, jobs_api, mail_api, memory_api, missions_api, missions_runner_api, monitor_api, proof_api, rag_api, realtime_api, system, tasks_api, uploads_api, vault_api
from .core import config, db
from .missions import runner as missions_runner
from .realtime import driver as realtime_driver
from .realtime.hub import hub as realtime_hub

logger = logging.getLogger("eclipse.main")


def _log_brain_status() -> None:
    """Startup log line: which model providers are actually
    configured, in chain order. Names only, never keys. A
    missing model is visible in the log, not silent — if the
    chain is Ollama-only, Ollama must be running locally or
    every chat message falls back to the rules."""
    from .planner import router as planner_router

    try:
        status = planner_router.brain_status(probe=False)
    except Exception as exc:  # noqa: BLE001 - a status failure must not stop startup
        logger.warning("could not read brain status: %s", exc)
        return
    chain = status["chain"]
    logger.info(
        "brain chain: %s",
        " -> ".join(chain) if chain else "none configured",
    )
    for provider in status["providers"]:
        logger.info(
            "brain provider %s: model %s, auth %s",
            provider["name"], provider["model"], provider["auth"],
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.migrate()
    config.get_token()  # generate + persist; never logged
    _log_brain_status()
    # PR #5: the mission runner thread (60s tick, daemon). It is
    # a no-op until a mission is active; ticks never raise.
    missions_runner.start_runner()
    # PR #7 (W3): the realtime driver loop (30s asyncio sweep).
    # A no-op until a chat task is planned AND the extension is
    # connected; ticks never raise.
    realtime_driver.start_driver_loop(realtime_hub)
    yield
    realtime_driver.stop_driver_loop()


app = FastAPI(
    title="Eclipse",
    version="0.1.0",
    lifespan=lifespan,
    # No unauthenticated docs: the plan says every request carries
    # X-Eclipse-Token, and /docs, /redoc and /openapi.json are routes.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    # Sprint 3 adds task routes (POST) and the plan's memory
    # forget routes (PATCH/DELETE); widen from GET/POST.
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["X-Eclipse-Token", "Content-Type"],
    allow_credentials=False,
)

app.include_router(system.router)
app.include_router(audit_api.router)
app.include_router(chat_api.router)
# PR #7: chat file uploads (POST /chat/uploads).
app.include_router(uploads_api.router)
app.include_router(memory_api.router)
app.include_router(tasks_api.router)
app.include_router(jobs_api.router)
app.include_router(vault_api.router)
app.include_router(mail_api.router)
app.include_router(rag_api.router)
app.include_router(proof_api.router)
app.include_router(connectors_api.router)
app.include_router(monitor_api.router)
# PR #5: missions. The runner router MUST be mounted BEFORE
# missions_api: GET /missions/due (and /missions/resume) would
# otherwise match missions_api's GET /missions/{mission_id}
# with id "due" / "resume".
app.include_router(missions_runner_api.router)
app.include_router(missions_api.router)
# PR #7 (W3): the live driver — WebSocket /realtime/extension
# plus /realtime/status and /realtime/drive.
app.include_router(realtime_api.router)

# Sprint 9: the workspace (Vite + React build) is served from the
# local server at /workspace. No token is needed to load the page
# itself — it holds no data; every data call is token-gated.
# The mount is registered after the API routers so it cannot
# shadow an API route.
_WORKSPACE_DIR = Path(__file__).resolve().parents[1] / "web" / "workspace" / "dist"
if _WORKSPACE_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/workspace", StaticFiles(directory=str(_WORKSPACE_DIR), html=True), name="workspace")
else:
    # Sprint 9 fix (review M9): a missing dist/ used to fail
    # silently — /workspace just 404'd. Log it so the cause is
    # visible in the server log.
    logger.warning(
        "workspace dist/ not found at %s — /workspace is not mounted;"
        " run `npm run build` in web/workspace",
        _WORKSPACE_DIR,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
