/** Persistent WebSocket client (PR #7, W3): the extension's live
 * connection to the server, so ANY task — not just mission jobs —
 * gets driven in the browser.
 *
 * The missions driver polls GET /missions/due on a 15-minute alarm
 * and only drives mission-linked jobs. A chat-created task has no
 * mission, so nothing ever drove it. This client holds one
 * WebSocket to /realtime/extension (token-authenticated via the
 * ?token= query parameter — a WebSocket handshake cannot carry
 * headers) and executes the server's actions as they arrive:
 *   init_session {task_id, url?}  -> open/find the task's tab
 *   observe {task_id}             -> observe the task's tab
 *   execute {task_id, action, options} -> perform one action
 *   close_session {task_id}       -> drop the task's tab mapping
 * Every action carries an action_id the client echoes back in its
 * {type: "result", action_id, payload} reply, so the server can
 * match results to waiting futures.
 *
 * Reconnect: a dropped socket reconnects with backoff (1s, 2s, 4s,
 * ... capped at 30s). A service worker can be suspended while
 * idle — the next wake (browser startup, token save, missions
 * alarm) re-runs ensureRealtimeConnected in background.ts, and
 * connect() is idempotent, so overlapping wakes are harmless.
 *
 * Deps-injected like missions.ts (createSocket, getToken,
 * handlers, timers), so the client is testable with a fake socket
 * and no real Chrome — see extension/tests/realtime.test.mjs.
 * The real chrome.* wiring lives in background.ts.
 */

export const REALTIME_WS_PATH = "/realtime/extension";
export const REALTIME_RECONNECT_BASE_MS = 1000;
export const REALTIME_RECONNECT_MAX_MS = 30000;

export type PlannerAction = {
  action: string;
  element_id?: string;
  text?: string;
  url?: string;
  seconds?: number;
  reason?: string;
};

export type ExecuteOptions = {
  taskId?: string;
  allowedSites?: string[];
  expected?: {
    id: string; tag: string; role: string; name: string;
    text: string; sensitive: boolean;
  };
  fill?: Record<string, string>;
};

export type PerformResult = { ok: boolean; detail?: string; url?: string };

/** The slice of the WebSocket API the client uses (the real
 * WebSocket in the service worker, a fake in tests). */
export type SocketLike = {
  readyState: number;
  send(data: string): void;
  close(): void;
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: string }) => void) | null;
  onclose: ((ev: unknown) => void) | null;
  onerror: ((ev: unknown) => void) | null;
};

export const SocketState = {
  CONNECTING: 0,
  OPEN: 1,
  CLOSING: 2,
  CLOSED: 3,
} as const;

export type RealtimeHandlers = {
  /** Open (or find) the task's tab; returns its id + URL. */
  initSession: (
    taskId: string,
    url?: string,
  ) => Promise<{ ok: boolean; tab_id?: number; url?: string; detail?: string }>;
  /** Observe the task's tab (walk + vision + redaction + gate). */
  observe: (taskId: string) => Promise<{ url: string; elements: unknown[] }>;
  /** Perform one validated action in the task's tab. */
  execute: (
    taskId: string,
    action: PlannerAction,
    options: ExecuteOptions,
  ) => Promise<PerformResult>;
  /** Drop the task's tab mapping (the tab itself stays open). */
  closeSession: (taskId: string) => Promise<{ ok: boolean }>;
};

export type RealtimeDeps = {
  /** The server origin, e.g. "http://127.0.0.1:8000". */
  serverBase: string;
  /** The stored server token (throws when none is saved). */
  getToken: () => Promise<string>;
  /** Build a socket (the real WebSocket, or a fake in tests). */
  createSocket: (url: string) => SocketLike;
  handlers: RealtimeHandlers;
  onConnect?: () => void;
  onDisconnect?: () => void;
  /** Injectable timers (tests drive reconnects by hand). */
  setTimeoutFn?: (fn: () => void, ms: number) => unknown;
  clearTimeoutFn?: (id: unknown) => void;
};

/** Backoff for reconnect attempt n (0-based): 1s, 2s, 4s, ...
 * capped at 30s. Pure, so the schedule is testable. */
export function reconnectDelayMs(attempt: number): number {
  const n = Math.max(0, Math.floor(attempt));
  return Math.min(REALTIME_RECONNECT_BASE_MS * 2 ** n, REALTIME_RECONNECT_MAX_MS);
}

/** The WebSocket URL for a server origin + token. */
export function realtimeWsUrl(serverBase: string, token: string): string {
  const wsBase = serverBase.replace(/^http/, "ws");
  return `${wsBase}${REALTIME_WS_PATH}?token=${encodeURIComponent(token)}`;
}

export class RealtimeClient {
  private socket: SocketLike | null = null;
  private reconnectAttempt = 0;
  private reconnectTimer: unknown = null;
  private stopped = false;
  private readonly setTimeoutFn: (fn: () => void, ms: number) => unknown;
  private readonly clearTimeoutFn: (id: unknown) => void;

  constructor(private readonly deps: RealtimeDeps) {
    this.setTimeoutFn =
      deps.setTimeoutFn ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimeoutFn =
      deps.clearTimeoutFn ?? ((id) => clearTimeout(id as ReturnType<typeof setTimeout>));
  }

  get connected(): boolean {
    return this.socket !== null && this.socket.readyState === SocketState.OPEN;
  }

  /** Connect (idempotent: a live or connecting socket is left
   * alone, so overlapping service-worker wakes are harmless).
   * A missing token rejects WITHOUT scheduling a reconnect —
   * the token-change listener in background.ts starts the
   * client when the token appears. */
  async connect(): Promise<void> {
    if (this.stopped) return;
    if (
      this.socket !== null &&
      (this.socket.readyState === SocketState.OPEN ||
        this.socket.readyState === SocketState.CONNECTING)
    ) {
      return;
    }
    let token: string;
    try {
      token = await this.deps.getToken();
    } catch (err) {
      console.warn("[eclipse] realtime: no server token, not connecting:", err);
      return;
    }
    const url = realtimeWsUrl(this.deps.serverBase, token);
    const socket = this.deps.createSocket(url);
    this.socket = socket;
    socket.onopen = () => {
      this.reconnectAttempt = 0;
      this.send({ type: "hello" });
      this.deps.onConnect?.();
    };
    socket.onmessage = (ev) => {
      void this.handleMessage(ev.data);
    };
    socket.onclose = () => {
      if (this.socket === socket) this.socket = null;
      this.deps.onDisconnect?.();
      this.scheduleReconnect();
    };
    socket.onerror = () => {
      // The close handler does the reconnect; an error without
      // a close still leaves a dead socket, so close it.
      try {
        socket.close();
      } catch {
        // Already closed — the close handler runs anyway.
      }
    };
  }

  /** Stop the client (no reconnect). The socket is closed. */
  disconnect(): void {
    this.stopped = true;
    if (this.reconnectTimer !== null) {
      this.clearTimeoutFn(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.socket !== null) {
      const socket = this.socket;
      this.socket = null;
      try {
        socket.close();
      } catch {
        // Already closed.
      }
    }
  }

  /** Tell the server a tab was closed (fire-and-forget — a
   * missing connection just drops the notice; the next drive
   * re-opens the tab anyway). */
  notifyTabClosed(tabId: number): void {
    this.send({ type: "tab_closed", tab_id: tabId });
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer !== null) return;
    const delay = reconnectDelayMs(this.reconnectAttempt);
    this.reconnectAttempt += 1;
    this.reconnectTimer = this.setTimeoutFn(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, delay);
  }

  private send(obj: Record<string, unknown>): void {
    if (this.socket !== null && this.socket.readyState === SocketState.OPEN) {
      this.socket.send(JSON.stringify(obj));
    }
  }

  /** Dispatch one server message and reply with its result.
   * A handler failure is a {ok: false} result, never a dropped
   * message — the server's future must always resolve. */
  private async handleMessage(data: string): Promise<void> {
    let message: {
      type?: string;
      action_id?: string;
      task_id?: string;
      url?: string;
      action?: PlannerAction;
      options?: ExecuteOptions;
    };
    try {
      message = JSON.parse(data) as typeof message;
    } catch {
      return; // Not JSON — ignore.
    }
    const actionId = message.action_id;
    if (typeof actionId !== "string" || !actionId) return;
    const taskId = message.task_id ?? "";
    const handlers = this.deps.handlers;
    try {
      if (message.type === "init_session") {
        const result = await handlers.initSession(taskId, message.url);
        this.send({ type: "result", action_id: actionId, payload: result });
      } else if (message.type === "observe") {
        const observation = await handlers.observe(taskId);
        this.send({
          type: "result",
          action_id: actionId,
          payload: { ok: true, observation },
        });
      } else if (message.type === "execute") {
        if (!message.action) {
          this.send({
            type: "result",
            action_id: actionId,
            payload: { ok: false, detail: "execute needs an action" },
          });
          return;
        }
        const result = await handlers.execute(
          taskId,
          message.action,
          message.options ?? {},
        );
        this.send({ type: "result", action_id: actionId, payload: result });
      } else if (message.type === "close_session") {
        const result = await handlers.closeSession(taskId);
        this.send({ type: "result", action_id: actionId, payload: result });
      } else {
        this.send({
          type: "result",
          action_id: actionId,
          payload: {
            ok: false,
            error: { code: "UNKNOWN_TYPE", message: `unknown action type ${message.type}` },
          },
        });
      }
    } catch (err) {
      this.send({
        type: "result",
        action_id: actionId,
        payload: {
          ok: false,
          detail: err instanceof Error ? err.message : String(err),
        },
      });
    }
  }
}
