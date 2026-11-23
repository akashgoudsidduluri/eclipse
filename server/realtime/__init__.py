"""Realtime live driver (PR #7, W3): the server-side WebSocket hub
and the observe -> execute -> verify driver for chat tasks.

The missions driver (extension 15-min alarm + GET /missions/due)
only drives mission-linked jobs. A chat-created task has no
mission, so nothing ever drove it in the browser. This package
closes that gap: the extension holds a persistent WebSocket to
the server (see extension/src/realtime.ts), the hub routes
per-action requests to it and matches results back via
asyncio Futures, and the driver runs the same
observe -> execute -> verify loop the missions driver runs —
but for ANY task, starting the moment it is planned.

Task records stay in SQLite (the hub keeps only live sockets,
in-flight futures and the ephemeral tab registry — a restart
loses tabs, never tasks). Privacy gates stay in the task loop
(redact before planner, fail-closed, human gate for submits);
this package never sees page text except inside observations
it forwards to loop.execute_step / loop.verify_step, and never
logs observation contents.
"""
