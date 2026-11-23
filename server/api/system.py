"""System endpoints: health, metrics, quota, brain. All token-authenticated."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..core import auth, db
from ..planner import router as planner_router

router = APIRouter(dependencies=[Depends(auth.require_token)], tags=["system"])


@router.get("/health")
def health() -> dict:
    """Liveness check. Token required, like every other endpoint."""
    return {"status": "ok", "service": "eclipse", "version": "0.1.0"}


@router.get("/metrics")
def metrics() -> dict:
    """Calls used, tasks, audit size. Token counts are 0 until the
    planner lands in Sprint 4."""
    conn = db.connect()
    try:
        tasks = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        audit = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log"
        ).fetchone()["c"]
        quota = conn.execute(
            "SELECT model, used_today, limit_day, reset_at FROM quota"
            " ORDER BY model"
        ).fetchall()
    finally:
        conn.close()
    return {
        "tasks": tasks,
        "audit_entries": audit,
        "quota": [dict(row) for row in quota],
        "tokens": {"in": 0, "out": 0},
        "note": "planner built (Sprint 3); token counts not wired yet — 0 until a live call is logged",
    }


@router.get("/quota")
def quota() -> dict:
    """Per-model calls used today, limit, and reset time."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT model, used_today, limit_day, reset_at FROM quota"
            " ORDER BY model"
        ).fetchall()
    finally:
        conn.close()
    return {"quota": [dict(row) for row in rows]}


@router.get("/brain")
def brain() -> dict:
    """Which model providers are configured, in chain order, and
    whether each one answers a cheap probe. Names and models
    only — never keys. A missing model is visible here, not
    silent: a provider with reachable=false (Ollama not running,
    a dead gateway) shows up instead of failing every chat
    message quietly. The probe is a 2s GET /models per
    provider, so this endpoint can take a few seconds when
    providers are down."""
    return planner_router.brain_status(probe=True)
