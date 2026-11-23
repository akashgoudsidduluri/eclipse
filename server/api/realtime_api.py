"""Realtime API (PR #7, W3): the live-driver endpoints.

- WebSocket /realtime/extension — the extension's persistent
  connection (see server/realtime/hub.py for the protocol).
  Browsers cannot set headers on a WebSocket handshake, so the
  token travels as a ?token= query parameter and is checked
  with the same constant-time comparison as the header auth; a
  missing or wrong token closes the socket with code 4401
  before any message is read. The token is never logged.
- GET /realtime/status — is the extension connected, which
  tabs are registered, how many actions are in flight.
- POST /realtime/drive/{task_id} — start driving a planned chat
  task now (202 at once; the drive runs in the background).
  Mission tasks are refused (the missions alarm owns them);
  an offline extension is a 503.

All REST routes are token-gated like the rest of the API.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from ..core import config, db
from ..core.auth import require_token
from ..realtime import driver as driver_mod
from ..realtime.hub import hub
from ..tasks import loop

logger = logging.getLogger("eclipse.realtime")

router = APIRouter(tags=["realtime"])

# WebSocket close code for a failed token check (4401 is the
# conventional "unauthorized" code in the 4xxx application range).
WS_CLOSE_UNAUTHORIZED = 4401


def is_valid_ws_token(token: str | None) -> bool:
    """Constant-time token check for the WebSocket handshake
    (the header dependency cannot run on a WebSocket)."""
    if not token:
        return False
    return hmac.compare_digest(token, config.get_token())


@router.get("/realtime/status")
def realtime_status(_token: str = Depends(require_token)):
    """Live-driver status: extension presence, tab registry,
    in-flight actions. Tab URLs are origins + paths the user
    asked the agent to open (no query strings are stored —
    the hub records only what init_session returns)."""
    return {
        "extension_connected": hub.is_extension_connected,
        "extension_count": hub.extension_count,
        "in_flight_actions": hub.in_flight_actions,
        "tabs": hub.tabs_snapshot(),
    }


@router.post("/realtime/drive/{task_id}", status_code=202)
def drive_now(task_id: str, _token: str = Depends(require_token)):
    """Start driving a planned chat task now. Returns at once;
    the drive runs in the background (progress is visible via
    the existing task endpoints)."""
    conn = db.connect()
    try:
        task = loop.get_task(conn, task_id)
    finally:
        conn.close()
    if task is None:
        raise HTTPException(status_code=404, detail=f"no task {task_id}")
    if task.get("mission_id"):
        raise HTTPException(
            status_code=409,
            detail="mission task — the missions alarm drives it",
        )
    if task["status"] in ("cancelled", "failed", "done"):
        raise HTTPException(
            status_code=409, detail=f"task {task_id} is {task['status']}",
        )
    if not hub.is_extension_connected:
        raise HTTPException(
            status_code=503,
            detail="extension not connected (is Chrome running with"
            " the Eclipse extension enabled?)",
        )
    hub.spawn(driver_mod.drive_task(hub, task_id))
    return {"task_id": task_id, "status": "driving"}


@router.websocket("/realtime/extension")
async def extension_socket(websocket: WebSocket):
    """The extension's persistent connection. The token comes
    from the ?token= query parameter (WebSocket handshakes
    cannot carry headers). On connect, a sweep is triggered so
    planned chat tasks start driving as soon as the extension
    comes online; on disconnect, in-flight actions fail at once
    (see hub.drop_extension)."""
    token = websocket.query_params.get("token")
    if not is_valid_ws_token(token):
        # Accept-then-close: Starlette turns a close() before
        # accept() into a bare HTTP 403, which loses the 4401
        # code — so the handshake completes and the socket is
        # closed at once, before any message is read.
        await websocket.accept()
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        logger.warning("realtime extension socket rejected (bad token)")
        return
    await websocket.accept()
    hub.register_extension(websocket)
    # The extension just came online — drive anything waiting.
    hub.spawn(driver_mod.drive_pending_tasks(hub))
    try:
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            msg_type = message.get("type")
            if msg_type == "result":
                action_id = message.get("action_id")
                payload = message.get("payload")
                if isinstance(action_id, str) and isinstance(payload, dict):
                    hub.resolve_action(action_id, payload)
            elif msg_type == "hello":
                # A (re)connected extension may have missed work
                # — sweep again (idempotent; locked tasks skip).
                hub.spawn(driver_mod.drive_pending_tasks(hub))
            elif msg_type == "tab_closed":
                tab_id = message.get("tab_id")
                if isinstance(tab_id, int):
                    hub.drop_tab(tab_id)
            elif msg_type == "heartbeat":
                pass  # presence is the socket itself
            else:
                logger.debug("realtime: ignoring unknown message type %r",
                             msg_type)
    except WebSocketDisconnect:
        pass
    finally:
        hub.drop_extension(websocket)
