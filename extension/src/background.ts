/** Service worker (Sprint 2 observe; Sprint 3 execute).
 *
 * Wires the content script to the offscreen document and to the
 * guarded executor. The full orchestration (planner calls,
 * checkpoints) lives on the server (Sprint 3, server/tasks/loop.py);
 * this file only owns the browser side:
 *   observe: content script walks the DOM → service worker captures
 *     the screenshot → offscreen document observes + redacts + gates
 *     → the gated redacted payload comes back here.
 *   execute: one validated planner action → content script performs
 *     it on the live page (guarded executor) → the result comes back.
 */
const OFFSCREEN_URL = "offscreen.html";
const SERVER = "http://127.0.0.1:8000";

import { BRIEFING_ALARM, checkDueWatches, watchAlarmName, type Watch } from "./monitor.js";
import { MISSIONS_ALARM, MISSIONS_PERIOD_MIN, defaultNotifyApproval, runDueMissions, type ElementInfo, type ExecuteStepResult, type MissionDue, type Observation, type PlannerAction } from "./missions.js";
import { notifyBriefing, notifyWatchFired } from "./notifications.js";
import { RealtimeClient, type SocketLike } from "./realtime.js";

async function serverToken(): Promise<string> {
  const stored = await chrome.storage.local.get("eclipseToken");
  const token = stored["eclipseToken"];
  if (typeof token !== "string" || token.length === 0) {
    throw new Error("no Eclipse server token stored (paste it in the Eclipse popup)");
  }
  return token;
}

async function serverCall(path: string, init?: RequestInit): Promise<unknown> {
  const token = await serverToken();
  const res = await fetch(`${SERVER}${path}`, {
    ...init,
    headers: { "X-Eclipse-Token": token, "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) throw new Error(`server ${res.status} on ${path}`);
  return res.json();
}

/** (Re)schedule one alarm per watch plus the daily briefing
 * alarm. Called when the workspace saves a watch and on
 * service-worker startup. */
async function scheduleWatchAlarms(): Promise<void> {
  try {
    const data = (await serverCall("/monitor/watches")) as { watches: Watch[] };
    const existing = await chrome.alarms.getAll();
    for (const alarm of existing) {
      if (alarm.name.startsWith("eclipse-watch-") || alarm.name === BRIEFING_ALARM) {
        await chrome.alarms.clear(alarm.name);
      }
    }
    for (const watch of data.watches) {
      // M17: active and muted watches get an alarm. A muted
      // watch becomes due when its mute expires — with no alarm
      // nothing would ever wake the worker to check it, so a
      // muted watch stayed muted forever unless another watch's
      // alarm fired first (/due filters it out until the mute
      // expires, so the early wake-ups are harmless). Broken
      // watches get none: /due never returns them until they
      // are repaired.
      if (
        (watch.status !== "active" && watch.status !== "muted") ||
        watch.interval_min < 15
      )
        continue;
      await chrome.alarms.create(watchAlarmName(watch.id), {
        periodInMinutes: watch.interval_min,
      });
    }
    // PR #5 (W2): one missions alarm — the extension pulls due
    // mission browser work every 15 minutes (the server cannot
    // push; see server/missions/runner.py). Re-creating an
    // existing alarm replaces it, so no clear is needed.
    await chrome.alarms.create(MISSIONS_ALARM, {
      periodInMinutes: MISSIONS_PERIOD_MIN,
    });
    // M13: the first briefing lands tomorrow morning at 09:00,
    // not up to 24h after startup.
    await chrome.alarms.create(BRIEFING_ALARM, {
      delayInMinutes: minutesUntilBriefing(new Date()),
      periodInMinutes: 1440,
    });
  } catch (err) {
    // B5: log instead of swallowing — if scheduling fails the
    // watches silently never run, and nobody would know.
    console.warn("[eclipse] could not schedule watch alarms:", err);
  }
}

// B5: the workspace (or any caller) reschedules alarms after a
// watch is created, muted, deleted, or repaired — new watches
// must not wait for a Chrome restart to start checking.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "eclipse.rescheduleWatches") {
    void scheduleWatchAlarms().then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});

// B5: the token appears when the user saves it in the popup
// (popup writes chrome.storage.local) — schedule then, not
// only at startup.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes["eclipseToken"]?.newValue) {
    void scheduleWatchAlarms();
    // PR #7 (W3): the realtime driver needs the token too —
    // connect it as soon as the token exists.
    void ensureRealtimeConnected();
  }
});

/** Local hour of the daily briefing alarm (PR #5 §3.9: 09:00
 * — the user's machine is in IST, so local 09:00 is IST 09:00;
 * the server tick is the backstop when Chrome is closed). */
export const BRIEFING_HOUR_LOCAL = 9;

/** Minutes from `now` until the next 09:00 briefing alarm.
 * Pure (injectable clock) so the alarm time is testable with a
 * fixed clock — see extension/tests/notifications.test.mjs. */
export function minutesUntilBriefing(now: Date): number {
  const next = new Date(now);
  next.setHours(BRIEFING_HOUR_LOCAL, 0, 0, 0);
  if (next.getTime() <= now.getTime()) next.setDate(next.getDate() + 1);
  return Math.ceil((next.getTime() - now.getTime()) / 60000);
}

/** Normalise a URL for tab matching: origin + pathname without
 * a trailing slash + search. Two URLs that differ only by a
 * trailing slash are the same page. */
export function normaliseWatchUrl(raw: string): string {
  const u = new URL(raw);
  const path = u.pathname.replace(/\/+$/, "") || "/";
  return `${u.origin}${path}${u.search}`;
}

/** Find an open tab whose URL matches (B3: the tab's ACTUAL
 * URL, not just its origin — the same rule as the watch
 * branch's inline matcher). Used by the missions branch. */
async function findTabByUrl(url: string): Promise<number | null> {
  let wanted: string;
  try {
    wanted = normaliseWatchUrl(url);
  } catch {
    return null;
  }
  const origin = new URL(url).origin;
  const tabs = await chrome.tabs.query({ url: `${origin}/*` });
  for (const tab of tabs) {
    if (!tab.url || tab.id === undefined) continue;
    try {
      if (normaliseWatchUrl(tab.url) === wanted) return tab.id;
    } catch {
      continue;
    }
  }
  return null;
}

/** POST an execute step, mapping the server's 409s to the
 * driver's result shape. serverCall throws without the body,
 * so this uses fetch directly: the grounding 409 carries
 * {reobserve: true}, and the approval gate carries the
 * approval id in its error text ("... with approval_id
 * appr-..."). */
async function executeStepOnServer(
  taskId: string,
  step: number,
  observation: Observation,
): Promise<ExecuteStepResult> {
  const token = await serverToken();
  const res = await fetch(`${SERVER}/tasks/${taskId}/steps/${step}/execute`, {
    method: "POST",
    headers: { "X-Eclipse-Token": token, "Content-Type": "application/json" },
    body: JSON.stringify({ observation }),
  });
  const data = (await res.json().catch(() => ({}))) as {
    detail?: unknown;
    action?: PlannerAction;
    expected_end_state?: string;
    element?: ElementInfo;
    allowed_sites?: string[];
    fill?: Record<string, string>;
  };
  if (res.ok) {
    return {
      ok: true,
      action: data.action as PlannerAction,
      expected_end_state: data.expected_end_state ?? "",
      element: data.element,
      allowed_sites: data.allowed_sites,
      fill: data.fill,
    };
  }
  const detail = data?.detail;
  if (
    res.status === 409 &&
    detail !== null &&
    typeof detail === "object" &&
    (detail as { reobserve?: unknown }).reobserve === true
  ) {
    return {
      ok: false,
      reobserve: true,
      detail: String((detail as { error?: unknown }).error ?? "grounding failed"),
    };
  }
  const text = typeof detail === "string" ? detail : `server ${res.status}`;
  const approvalMatch = /approval_id (appr-[0-9a-f]+)/.exec(text);
  if (res.status === 409 && /needs approval|approval needed/i.test(text)) {
    return {
      ok: false,
      approvalNeeded: true,
      approvalId: approvalMatch?.[1],
      detail: text,
    };
  }
  return { ok: false, detail: text };
}

/** Per-task tab registry (PR #7, W3): task id -> tab id. The
 * server keeps a mirror (hub tab registry); this map is the
 * authority for which tab a task's actions land in. A task's
 * tab is opened by init_session — a chat task gets a real tab
 * immediately, instead of the missions rule (skip the job
 * unless the user already has the page open). */
const taskTabs = new Map<string, number>();

/** Open (or find) a task's tab. An existing tab is reused when
 * it is still open, so a re-drive never opens a second tab; a
 * closed tab is re-created, so a drive self-heals. The tab
 * starts in the background — observeTab activates it when
 * driving starts. */
async function findOrCreateTaskTab(
  taskId: string,
  url?: string,
): Promise<{ tab_id: number; url: string }> {
  const existing = taskTabs.get(taskId);
  if (existing !== undefined) {
    try {
      const tab = await chrome.tabs.get(existing);
      return { tab_id: existing, url: tab.url ?? url ?? "" };
    } catch {
      // The tab is gone — fall through and open a new one.
      taskTabs.delete(taskId);
    }
  }
  const tab = await chrome.tabs.create(
    url ? { url, active: false } : { active: false },
  );
  if (tab.id === undefined) throw new Error("could not open a tab for the task");
  taskTabs.set(taskId, tab.id);
  return { tab_id: tab.id, url: tab.url ?? url ?? "" };
}

/** Observe a task's tab. A blank / browser-internal tab has no
 * content script, so the walk would fail: return a minimal
 * observation instead (the first step is usually a navigate,
 * which needs no elements). Real pages always take the full
 * observe path — errors there propagate, so a broken vision
 * pipeline is never silent. */
async function observeTaskTab(taskId: string): Promise<Observation> {
  const tabId =
    taskTabs.get(taskId) ?? (await findOrCreateTaskTab(taskId)).tab_id;
  const tab = await chrome.tabs.get(tabId);
  const tabUrl = tab.url ?? "";
  if (!/^https?:\/\//i.test(tabUrl)) {
    return { url: tabUrl, elements: [] };
  }
  return (await observeTab(tabId)) as Observation;
}

/** Wait for a tab to finish loading (or the timeout — the
 * driver re-observes and verifies anyway, so a slow page must
 * not fail the step here). */
function waitForTabLoad(tabId: number, timeoutMs: number): Promise<void> {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    };
    const listener = (updatedId: number, info: { status?: string }) => {
      if (updatedId === tabId && info.status === "complete") finish();
    };
    chrome.tabs.onUpdated.addListener(listener);
    setTimeout(finish, timeoutMs);
  });
}

/** Perform one validated action in a task's tab. A navigate
 * from a blank tab has no content script to run the executor
 * in, so it goes via the tabs API instead. The server already
 * validated the action (allow-list included) before sending it
 * over the token-authenticated socket; the scheme check here
 * is the fail-closed backstop. Other actions go to the content
 * script's guarded executor, exactly like the missions driver. */
async function executeTaskAction(
  tabId: number,
  action: PlannerAction,
  options: {
    taskId?: string;
    allowedSites?: string[];
    expected?: ElementInfo;
    fill?: Record<string, string>;
  },
): Promise<{ ok: boolean; detail?: string; url?: string }> {
  if (action.action === "navigate") {
    const url = action.url ?? "";
    if (!/^https?:\/\//i.test(url)) {
      return { ok: false, detail: "navigate needs a full http(s) URL" };
    }
    await chrome.tabs.update(tabId, { url });
    await waitForTabLoad(tabId, 15000);
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    return { ok: true, url: tab?.url ?? url };
  }
  const result = (await chrome.tabs.sendMessage(tabId, {
    type: "execute",
    action,
    options,
  })) as { ok: boolean; detail?: string; url?: string };
  return result;
}

/** The realtime client (PR #7, W3): one persistent WebSocket
 * to the server, driving ANY task. Created lazily — the
 * service worker may wake without a token (or without a
 * network), and connect() handles that by waiting for the
 * token-change listener. */
let realtimeClient: RealtimeClient | null = null;

function getRealtimeClient(): RealtimeClient {
  if (realtimeClient) return realtimeClient;
  realtimeClient = new RealtimeClient({
    serverBase: SERVER,
    getToken: serverToken,
    // The real WebSocket's MessageEvent is structurally wider
    // than SocketLike's {data} — the cast is the seam.
    createSocket: (url) => new WebSocket(url) as unknown as SocketLike,
    handlers: {
      initSession: async (taskId, url) => {
        const tab = await findOrCreateTaskTab(taskId, url);
        return { ok: true, tab_id: tab.tab_id, url: tab.url };
      },
      observe: async (taskId) => observeTaskTab(taskId),
      execute: async (taskId, action, options) => {
        const tabId =
          taskTabs.get(taskId) ?? (await findOrCreateTaskTab(taskId)).tab_id;
        return executeTaskAction(tabId, action, options);
      },
      closeSession: async (taskId) => {
        // The tab stays open — the user should see the result
        // ("open google" must not close Google again).
        taskTabs.delete(taskId);
        return { ok: true };
      },
    },
    onDisconnect: () => {
      console.warn("[eclipse] realtime disconnected; reconnecting with backoff");
    },
  });
  return realtimeClient;
}

/** Connect the realtime driver (idempotent — safe to call on
 * every service-worker wake). Failures log, never throw: the
 * missions alarm re-runs this within 15 minutes anyway. */
async function ensureRealtimeConnected(): Promise<void> {
  try {
    await getRealtimeClient().connect();
  } catch (err) {
    console.warn("[eclipse] could not connect the realtime driver:", err);
  }
}

// A closed tab drops its task mapping at once (and the server
// is told, so its mirror drops the stale entry too). If the
// socket is down the notice is lost — harmless, because the
// next drive re-opens the tab anyway. The ?. is for the node
// test fakes, whose chrome.tabs has no onRemoved (real Chrome
// always does); background.ts registers listeners at import
// time, so an unguarded access fails every test that imports it.
chrome.tabs.onRemoved?.addListener((tabId) => {
  for (const [taskId, id] of taskTabs) {
    if (id === tabId) {
      taskTabs.delete(taskId);
      realtimeClient?.notifyTabClosed(tabId);
    }
  }
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === BRIEFING_ALARM) {
    (async () => {
      try {
        const briefing = (await serverCall("/monitor/briefing", { method: "POST" })) as {
          cards?: unknown[];
        };
        // Notify once per IST day (notifyBriefing dedups) — the
        // server day guard may return an already-built briefing,
        // and that is still worth one notification.
        const count = Array.isArray(briefing.cards) ? briefing.cards.length : 0;
        void notifyBriefing(count);
      } catch {
        // The briefing is best-effort; the user can rebuild it
        // on demand from the workspace.
      }
    })();
    return;
  }
  if (alarm.name === MISSIONS_ALARM) {
    (async () => {
      try {
        // B4/M16: a failed run is logged, not silently dropped.
        const summary = await runDueMissions({
          fetchDue: async () => {
            const data = (await serverCall("/missions/due")) as {
              missions: MissionDue[];
            };
            return data.missions;
          },
          findTab: findTabByUrl,
          observe: async (tabId) =>
            (await observeTab(tabId)) as Observation,
          executeStep: async (taskId, step, observation) =>
            executeStepOnServer(taskId, step, observation),
          performAction: async (tabId, action, options) => {
            // The same message the eclipse.execute listener
            // forwards — the driver calls the content script
            // directly (it already runs in the service worker).
            const result = (await chrome.tabs.sendMessage(tabId, {
              type: "execute",
              action,
              options,
            })) as { ok: boolean; detail?: string; url?: string };
            return result;
          },
          verifyStep: async (
            taskId,
            step,
            observation,
            previousObservation,
            expectedEndState,
            previousUrl,
          ) => {
            const result = (await serverCall(
              `/tasks/${taskId}/steps/${step}/verify`,
              {
                method: "POST",
                body: JSON.stringify({
                  observation,
                  previous_observation: previousObservation,
                  expected_end_state: expectedEndState,
                  previous_url: previousUrl,
                }),
              },
            )) as { verified: boolean; reason: string; retry: boolean };
            return result;
          },
          checkWatch: async (job, tabId) => {
            // The monitor path, for a price mission's watch:
            // ask the content script for the selector's value,
            // report it, return the verdict.
            const obs = (await chrome.tabs.sendMessage(tabId, {
              type: "eclipse.watchCheck",
              selector: job.selector,
            })) as { found: boolean; value: string | null };
            const verdict = (await serverCall(
              `/monitor/watches/${job.watch_id}/check`,
              { method: "POST", body: JSON.stringify(obs) },
            )) as {
              fired: boolean;
              broken: boolean;
              status: string;
              reason: string;
            };
            if (verdict.fired) {
              // A price mission's watch fires like any watch —
              // the user hears about it (W3's notification).
              void notifyWatchFired(job.url ?? job.watch_id ?? "");
            }
            return verdict;
          },
          onApprovalNeeded: defaultNotifyApproval,
        });
        if (summary.errors.length > 0) {
          console.warn("[eclipse] missions run errors:", summary);
        }
      } catch (err) {
        console.warn("[eclipse] missions alarm run failed:", err);
      }
      // PR #5 §2c fix (W2): reschedule at the end of every
      // missions run, so a watch created in the workspace gets
      // an alarm within 15 minutes even without a restart.
      await scheduleWatchAlarms();
      // PR #7 (W3): the worker is awake — make sure the
      // realtime driver is connected too (a suspended worker
      // drops its socket; the alarm is the backstop wake).
      void ensureRealtimeConnected();
    })();
    return;
  }
  if (!alarm.name.startsWith("eclipse-watch-")) return;
  (async () => {
    try {
      // B4/M16: a failed run is logged, not silently dropped.
      // W3: remember due watch URLs so a firing can name its
      // page in the notification (checkDueWatches only returns
      // counts, not the fired watch's URL).
      const dueUrls = new Map<string, string>();
      const summary = await checkDueWatches({
        fetchDue: async () => {
          const data = (await serverCall("/monitor/due")) as { watches: Watch[] };
          for (const w of data.watches) dueUrls.set(w.id, w.url);
          return data.watches;
        },
        findTab: async (url) => {
          // B3: match the tab's ACTUAL URL, not just its origin.
          // An origin-only match could evaluate the watch against
          // a different page on the same site — a missing element
          // there counts as a miss, and for a "disappeared"
          // watch it fires a false alert.
          let wanted: string;
          try {
            wanted = normaliseWatchUrl(url);
          } catch {
            return null;
          }
          const origin = new URL(url).origin;
          const tabs = await chrome.tabs.query({ url: `${origin}/*` });
          for (const tab of tabs) {
            if (!tab.url || tab.id === undefined) continue;
            try {
              if (normaliseWatchUrl(tab.url) === wanted) return tab.id;
            } catch {
              continue;
            }
          }
          return null;
        },
        observeTab: async (tabId, watch) => {
          const result = (await chrome.tabs.sendMessage(tabId, {
            type: "eclipse.watchCheck",
            selector: watch.selector,
          })) as { found: boolean; value: string | null };
          return result;
        },
        reportCheck: async (watchId, obs) => {
          const verdict = (await serverCall(`/monitor/watches/${watchId}/check`, {
            method: "POST",
            body: JSON.stringify(obs),
          })) as { fired: boolean; broken: boolean; status: string; reason: string };
          if (verdict.fired) {
            // Watches used to fire silently into the DB — now
            // the user hears about it (Chrome must be running;
            // the firing is also in the DB + the briefing).
            void notifyWatchFired(dueUrls.get(watchId) ?? watchId);
          }
          return verdict;
        },
      });
      if (summary.errors.length > 0) {
        console.warn("[eclipse] watch check errors:", summary);
      }
    } catch (err) {
      console.warn("[eclipse] watch alarm run failed:", err);
    }
  })();
});

chrome.runtime.onStartup.addListener(() => {
  void scheduleWatchAlarms();
  void ensureRealtimeConnected();
});

async function ensureOffscreen(): Promise<void> {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: [chrome.runtime.ContextType.OFFSCREEN_DOCUMENT],
  });
  if (contexts.length > 0) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_URL,
    reasons: [chrome.offscreen.Reason.USER_MEDIA],
    justification: "On-device vision (YOLO, OCR, BlazeFace, Florence-2) via WebGPU.",
  });
}

/** Observe a tab: walk the DOM, capture the screenshot, run
 * the offscreen observe + redact + gate. Extracted from the
 * eclipse.observe listener (PR #5, W2) so the missions driver
 * can call it directly — a service worker cannot reliably
 * message its own listeners, so the driver does not send
 * eclipse.observe to itself. The listener below still calls
 * this function; behaviour is unchanged. */
async function observeTab(
  tabId: number,
  taskKind?: string,
  egressTarget?: string,
): Promise<unknown> {
  // captureVisibleTab screenshots the ACTIVE tab: if the target
  // tab is not active, the screenshot would show a different
  // page than the walk — the vision results (YOLO boxes, OCR,
  // faces, blackout boxes) would describe the wrong page.
  // Activate the target first so the walk and the screenshot
  // agree. A closed tab makes tabs.get reject → the caller sees
  // the error.
  const tab = await chrome.tabs.get(tabId);
  if (!tab.active) {
    await chrome.tabs.update(tabId, { active: true });
  }
  const walk = await chrome.tabs.sendMessage(tabId, { type: "walk" });
  const dataUrl = await chrome.tabs.captureVisibleTab({ format: "png" });
  // Decode the screenshot to raw pixels for the offscreen doc.
  const img = await fetch(dataUrl).then((r) => r.blob());
  const bitmap = await createImageBitmap(img);
  const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
  const ctx = canvas.getContext("2d")!;
  ctx.drawImage(bitmap, 0, 0);
  const pixels = ctx.getImageData(0, 0, bitmap.width, bitmap.height);
  await ensureOffscreen();
  // An unknown taskKind would reach the gate as neither
  // "image" nor "mixed", so vision would silently stop being
  // required — default to "mixed" (the strictest) instead.
  const kind =
    taskKind === "dom" || taskKind === "image" || taskKind === "mixed"
      ? taskKind
      : "mixed";
  const result = await chrome.runtime.sendMessage({
    type: "observe",
    url: walk.url,
    viewport: walk.viewport,
    elements: walk.elements,
    screenshot: { data: pixels.data, width: bitmap.width, height: bitmap.height },
    taskKind: kind,
    egressTarget: egressTarget ?? "http://127.0.0.1:8000",
  });
  if (result?.blackoutBoxes?.length) {
    await chrome.tabs.sendMessage(tabId, { type: "blackout", boxes: result.blackoutBoxes });
  }
  return result;
}

chrome.runtime.onMessage.addListener(
  (
    msg: {
      type: string;
      tabId?: number;
      taskKind?: string;
      egressTarget?: string;
      action?: { action: string; element_id?: string; text?: string; url?: string; seconds?: number; reason?: string };
      // From the server's execute response: the task id (the
      // submit guard key), the allowed sites, the element info
      // the planner saw (the fingerprint), and the vault fill map
      // (placeholder → value, Sprint 5). Without these the live
      // path has no taskId, no allow-list and no fingerprint.
      options?: {
        taskId?: string;
        allowedSites?: string[];
        expected?: {
          id: string; tag: string; role: string; name: string;
          text: string; sensitive: boolean;
        };
        fill?: Record<string, string>;
      };
    },
    _sender,
    sendResponse,
  ) => {
    if (msg.type === "eclipse.execute" && msg.action) {
      (async () => {
        try {
          // The tab must be named: executing in "the active tab"
          // would land the action (including a vault-filled
          // password) in whatever tab is foreground if the user
          // switched tabs between observe and execute.
          const tabId = msg.tabId;
          if (!tabId) {
            sendResponse({ ok: false, detail: "eclipse.execute needs a tabId", url: "" });
            return;
          }
          const result = await chrome.tabs.sendMessage(tabId, {
            type: "execute",
            action: msg.action,
            options: msg.options ?? {},
          });
          sendResponse(result);
        } catch (err) {
          sendResponse({ ok: false, detail: err instanceof Error ? err.message : String(err), url: "" });
        }
      })();
      return true; // async response
    }
    if (msg.type !== "eclipse.observe") return false;
  (async () => {
    try {
      const tabId = msg.tabId ?? (await chrome.tabs.query({ active: true, currentWindow: true }))[0]?.id;
      if (!tabId) {
        sendResponse({ error: "no active tab" });
        return;
      }
      sendResponse(await observeTab(tabId, msg.taskKind, msg.egressTarget));
    } catch (err) {
      sendResponse({ error: err instanceof Error ? err.message : String(err) });
    }
  })();
  return true; // async response
});
