/** Realtime client tests (PR #7, W3).
 *
 * No real Chrome, no server: a fake socket stands in for the
 * WebSocket, fake handlers stand in for the tab drivers, and
 * fake timers drive reconnects by hand.
 *
 * Run: npm test (pretest compiles src/realtime.ts to tests-dist/).
 */
import test from "node:test";
import assert from "node:assert/strict";

const {
  RealtimeClient,
  reconnectDelayMs,
  realtimeWsUrl,
} = await import("../tests-dist/realtime.js");

// ---- Fakes -----------------------------------------------------

const OPEN = 1;
const CONNECTING = 0;
const CLOSED = 3;

class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = CONNECTING;
    this.sent = [];
    this.closed = false;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
  }

  send(data) {
    this.sent.push(JSON.parse(data));
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.readyState = CLOSED;
    this.onclose?.({});
  }

  /** Drive the socket open (as the server accepting would). */
  open() {
    this.readyState = OPEN;
    this.onopen?.({});
  }

  /** Deliver one server message. */
  receive(obj) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }

  /** Drop the socket (as a dead service worker would). */
  drop() {
    this.readyState = CLOSED;
    this.onclose?.({});
  }
}

function makeTimers() {
  const pending = [];
  return {
    pending,
    setTimeoutFn: (fn, ms) => {
      const id = pending.length;
      pending.push({ fn, ms, cleared: false });
      return id;
    },
    clearTimeoutFn: (id) => {
      if (pending[id]) pending[id].cleared = true;
    },
    /** Run the next pending timer (fails when none is pending). */
    runNext() {
      const next = pending.find((t) => !t.cleared && !t.ran);
      assert.ok(next, "expected a pending reconnect timer");
      next.ran = true;
      next.fn();
      return next.ms;
    },
  };
}

function makeClient(overrides = {}) {
  const sockets = [];
  const timers = makeTimers();
  const handlerCalls = [];
  const handlers = {
    initSession: async (taskId, url) => {
      handlerCalls.push({ type: "init_session", taskId, url });
      return { ok: true, tab_id: 7, url: url ?? "" };
    },
    observe: async (taskId) => {
      handlerCalls.push({ type: "observe", taskId });
      return { url: "https://www.google.com", elements: [] };
    },
    execute: async (taskId, action, options) => {
      handlerCalls.push({ type: "execute", taskId, action, options });
      return { ok: true, url: "https://www.google.com" };
    },
    closeSession: async (taskId) => {
      handlerCalls.push({ type: "close_session", taskId });
      return { ok: true };
    },
    ...(overrides.handlers ?? {}),
  };
  const client = new RealtimeClient({
    serverBase: "http://127.0.0.1:8000",
    getToken: async () => "test-token",
    createSocket: (url) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    handlers,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
    ...(overrides.client ?? {}),
  });
  return { client, sockets, timers, handlerCalls };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

// ---- URL + backoff (pure) --------------------------------------

test("realtimeWsUrl builds the ws URL with an encoded token", () => {
  assert.equal(
    realtimeWsUrl("http://127.0.0.1:8000", "tok/en?&"),
    "ws://127.0.0.1:8000/realtime/extension?token=tok%2Fen%3F%26",
  );
});

test("reconnectDelayMs doubles to a 30s cap", () => {
  assert.equal(reconnectDelayMs(0), 1000);
  assert.equal(reconnectDelayMs(1), 2000);
  assert.equal(reconnectDelayMs(2), 4000);
  assert.equal(reconnectDelayMs(5), 30000);
  assert.equal(reconnectDelayMs(99), 30000);
  assert.equal(reconnectDelayMs(-1), 1000);
});

// ---- Connect + hello -------------------------------------------

test("connect opens the socket and sends hello on open", async () => {
  const { client, sockets } = makeClient();
  await client.connect();
  assert.equal(sockets.length, 1);
  assert.ok(sockets[0].url.startsWith("ws://127.0.0.1:8000/realtime/extension?token="));
  assert.equal(client.connected, false);
  sockets[0].open();
  assert.equal(client.connected, true);
  assert.deepEqual(sockets[0].sent, [{ type: "hello" }]);
  client.disconnect();
});

test("connect is idempotent while connecting or open", async () => {
  const { client, sockets } = makeClient();
  await client.connect();
  await client.connect();
  assert.equal(sockets.length, 1);
  sockets[0].open();
  await client.connect();
  assert.equal(sockets.length, 1);
  client.disconnect();
});

test("a missing token does not connect and does not reconnect", async () => {
  const { client, sockets, timers } = makeClient({
    client: {
      getToken: async () => {
        throw new Error("no Eclipse server token stored");
      },
    },
  });
  await client.connect();
  assert.equal(sockets.length, 0);
  assert.equal(timers.pending.length, 0);
  client.disconnect();
});

// ---- Dispatch --------------------------------------------------

test("init_session dispatches to the handler and replies with its result", async () => {
  const { client, sockets, handlerCalls } = makeClient();
  await client.connect();
  sockets[0].open();
  sockets[0].receive({
    type: "init_session", action_id: "a1", task_id: "task-1",
    url: "https://www.google.com",
  });
  await tick();
  await tick();
  assert.deepEqual(handlerCalls, [
    { type: "init_session", taskId: "task-1", url: "https://www.google.com" },
  ]);
  assert.deepEqual(sockets[0].sent[1], {
    type: "result",
    action_id: "a1",
    payload: { ok: true, tab_id: 7, url: "https://www.google.com" },
  });
  client.disconnect();
});

test("observe wraps the observation in an ok result", async () => {
  const { client, sockets } = makeClient();
  await client.connect();
  sockets[0].open();
  sockets[0].receive({ type: "observe", action_id: "a2", task_id: "task-1" });
  await tick();
  await tick();
  assert.deepEqual(sockets[0].sent[1], {
    type: "result",
    action_id: "a2",
    payload: { ok: true, observation: { url: "https://www.google.com", elements: [] } },
  });
  client.disconnect();
});

test("execute passes the action and options through", async () => {
  const { client, sockets, handlerCalls } = makeClient();
  await client.connect();
  sockets[0].open();
  const action = { action: "navigate", url: "https://www.google.com" };
  const options = { taskId: "task-1", allowedSites: ["www.google.com"] };
  sockets[0].receive({
    type: "execute", action_id: "a3", task_id: "task-1", action, options,
  });
  await tick();
  await tick();
  assert.deepEqual(handlerCalls, [
    { type: "execute", taskId: "task-1", action, options },
  ]);
  assert.deepEqual(sockets[0].sent[1], {
    type: "result",
    action_id: "a3",
    payload: { ok: true, url: "https://www.google.com" },
  });
  client.disconnect();
});

test("execute without an action replies with an error result", async () => {
  const { client, sockets, handlerCalls } = makeClient();
  await client.connect();
  sockets[0].open();
  sockets[0].receive({ type: "execute", action_id: "a4", task_id: "task-1" });
  await tick();
  await tick();
  assert.equal(handlerCalls.length, 0);
  assert.equal(sockets[0].sent[1].payload.ok, false);
  client.disconnect();
});

test("close_session dispatches and replies", async () => {
  const { client, sockets, handlerCalls } = makeClient();
  await client.connect();
  sockets[0].open();
  sockets[0].receive({ type: "close_session", action_id: "a5", task_id: "task-1" });
  await tick();
  await tick();
  assert.deepEqual(handlerCalls, [{ type: "close_session", taskId: "task-1" }]);
  assert.deepEqual(sockets[0].sent[1].payload, { ok: true });
  client.disconnect();
});

test("an unknown type replies with an error result, not a hang", async () => {
  const { client, sockets } = makeClient();
  await client.connect();
  sockets[0].open();
  sockets[0].receive({ type: "teleport", action_id: "a6", task_id: "task-1" });
  await tick();
  await tick();
  assert.equal(sockets[0].sent[1].payload.ok, false);
  assert.equal(sockets[0].sent[1].payload.error.code, "UNKNOWN_TYPE");
  client.disconnect();
});

test("a handler failure replies with ok:false, never a dropped message", async () => {
  const { client, sockets } = makeClient({
    handlers: {
      observe: async () => {
        throw new Error("tab is gone");
      },
    },
  });
  await client.connect();
  sockets[0].open();
  sockets[0].receive({ type: "observe", action_id: "a7", task_id: "task-1" });
  await tick();
  await tick();
  assert.deepEqual(sockets[0].sent[1], {
    type: "result",
    action_id: "a7",
    payload: { ok: false, detail: "tab is gone" },
  });
  client.disconnect();
});

test("a message without an action_id is ignored", async () => {
  const { client, sockets, handlerCalls } = makeClient();
  await client.connect();
  sockets[0].open();
  sockets[0].receive({ type: "observe", task_id: "task-1" });
  sockets[0].receive("not json");
  await tick();
  await tick();
  assert.equal(handlerCalls.length, 0);
  assert.equal(sockets[0].sent.length, 1); // hello only
  client.disconnect();
});

// ---- Reconnect -------------------------------------------------

test("a dropped socket reconnects with backoff", async () => {
  const { client, sockets, timers } = makeClient();
  await client.connect();
  sockets[0].open();
  assert.equal(client.connected, true);
  sockets[0].drop();
  assert.equal(client.connected, false);
  assert.equal(timers.pending.length, 1);
  const firstDelay = timers.runNext();
  assert.equal(firstDelay, 1000);
  await tick();
  assert.equal(sockets.length, 2);
  sockets[1].open();
  assert.equal(client.connected, true);
  // A second drop restarts the backoff from 1s (a successful
  // connect resets the attempt count).
  sockets[1].drop();
  const secondDelay = timers.runNext();
  assert.equal(secondDelay, 1000);
  client.disconnect();
});

test("backoff grows across consecutive drops without a connect", async () => {
  const { client, sockets, timers } = makeClient();
  await client.connect();
  sockets[0].open();
  sockets[0].drop();
  timers.runNext(); // 1000ms -> second socket
  await tick();
  assert.equal(sockets.length, 2);
  // The second socket drops before ever opening.
  sockets[1].drop();
  const secondDelay = timers.runNext();
  assert.equal(secondDelay, 2000);
  client.disconnect();
});

test("disconnect stops reconnecting", async () => {
  const { client, sockets, timers } = makeClient();
  await client.connect();
  sockets[0].open();
  client.disconnect();
  assert.equal(client.connected, false);
  // The close fired by disconnect() must not schedule a timer.
  assert.equal(timers.pending.filter((t) => !t.cleared).length, 0);
  assert.equal(sockets.length, 1);
});

test("notifyTabClosed sends a tab_closed notice when connected", async () => {
  const { client, sockets } = makeClient();
  await client.connect();
  sockets[0].open();
  client.notifyTabClosed(42);
  assert.deepEqual(sockets[0].sent[1], { type: "tab_closed", tab_id: 42 });
  client.disconnect();
});

test("notifyTabClosed is a no-op when disconnected", async () => {
  const { client } = makeClient();
  client.notifyTabClosed(42); // must not throw
  client.disconnect();
});
