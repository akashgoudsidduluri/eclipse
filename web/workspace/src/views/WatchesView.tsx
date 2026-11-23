import { useState } from "react";
import "./WatchesView.css";
import { api, isHttpError, type Briefing, type Watch, type WatchEvent } from "../api";
import { usePoll } from "../hooks";
import { ConfirmDialog } from "../components/ConfirmDialog";
import {
  ActivityLine,
  Disclosure,
  EmptyState,
  ErrorBanner,
  RelativeTime,
  SectionHeader,
  ShowMore,
  Spinner,
  StatusChip,
  SummaryChips,
  statusTone,
} from "../components/ui";

/** Human labels for the server's condition names. Values sent
 * to the server never change — only what the chips show. */
const CONDITION_LABELS: Record<string, string> = {
  changed: "changes",
  appeared: "appears",
  disappeared: "disappears",
  contains: "contains",
  not_contains: "does not contain",
};

/** How many events show before "Show more". */
const EVENTS_PAGE_SIZE = 8;

/** Watches + briefing (plan §8). Status only (PR #7): new
 * watches are set up in chat — tell the Agent what page to
 * watch — so this view lists the watches that exist, their
 * events, and the morning briefing, with no setup form.
 * Watches only run while Chrome is open — the extension
 * checks pages in open tabs on a chrome.alarms schedule; the
 * server never polls a page by itself. The manual check here
 * is for reporting an observation you made yourself. */
export function WatchesView({ token }: { token: string | null }) {
  const [watches, setWatches] = useState<Watch[]>([]);
  const [events, setEvents] = useState<WatchEvent[]>([]);
  const [eventsShown, setEventsShown] = useState(EVENTS_PAGE_SIZE);
  const [due, setDue] = useState<Watch[]>([]);
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastOk, setLastOk] = useState<number | null>(null);

  const [repairId, setRepairId] = useState<string | null>(null);
  const [repairSelector, setRepairSelector] = useState("");
  const [repairUrl, setRepairUrl] = useState("");

  const [checkId, setCheckId] = useState<string | null>(null);
  const [checkValue, setCheckValue] = useState("");
  const [checkFound, setCheckFound] = useState(true);
  const [checkResult, setCheckResult] = useState<string | null>(null);

  const [muteMinutes, setMuteMinutes] = useState(60);
  const [pending, setPending] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [building, setBuilding] = useState(false);

  const load = async () => {
    try {
      const [w, e, d] = await Promise.all([
        api.watches(),
        api.watchEvents(undefined, 50),
        api.dueWatches(),
      ]);
      setWatches(w.watches);
      setEvents(e.events);
      setDue(d.watches);
      setError(null);
      setLastOk(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const loadBriefing = async () => {
    try {
      const b = await api.briefingLatest();
      setBriefing(b);
    } catch (err) {
      // A 404 means none exists — not an error.
      if (!isHttpError(err, 404)) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }
  };

  usePoll(load, [token], 15000);
  usePoll(loadBriefing, [token], 0);

  const runWatchAction = async (
    id: string,
    kind: "mute" | "unmute" | "delete" | "repair" | "check",
  ) => {
    setPending(`${kind}:${id}`);
    setError(null);
    setCheckResult(null);
    try {
      if (kind === "mute") await api.muteWatch(id, muteMinutes);
      if (kind === "unmute") await api.unmuteWatch(id);
      if (kind === "delete") await api.deleteWatch(id);
      if (kind === "repair") {
        await api.repairWatch(id, {
          selector: repairSelector.trim() || undefined,
          url: repairUrl.trim() || undefined,
        });
        setRepairId(null);
      }
      if (kind === "check") {
        const result = await api.checkWatch(
          id,
          checkFound,
          checkFound ? checkValue : null,
        );
        setCheckResult(
          result.fired
            ? `Fired: ${result.reason}`
            : result.became_broken
              ? `Watch broke: ${result.reason}`
              : `No change: ${result.reason}`,
        );
        setCheckId(null);
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const buildBriefing = async () => {
    setBuilding(true);
    setError(null);
    try {
      const b = await api.buildBriefing();
      setBriefing(b);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBuilding(false);
    }
  };

  return (
    <div className="view watches-view">
      <div className="view-head">
        <h2>Watches &amp; Briefing</h2>
        <p className="muted">
          Watches follow a page element and fire when it changes. They only
          run while Chrome is open — the extension checks, never the server.
          New watches start in chat: tell the Agent what page to watch.
        </p>
      </div>
      {error && <ErrorBanner error={error} lastOk={lastOk} onRetry={() => void load()} />}
      {checkResult && (
        <p role="status" className="secondary">
          {checkResult}
        </p>
      )}

      <section aria-label="Watches" className="pane">
        <SectionHeader
          title={`Watches (${watches.length})`}
          action={
            <span className="muted">
              {due.length > 0
                ? `${due.length} due for a check`
                : "None due right now"}
            </span>
          }
        />
        {/* The mute length lives with the mute actions: one
          * shared setting, directly above the rows whose Mute
          * buttons use it. */}
        <div className="mute-setting">
          <label htmlFor="mute-minutes">Mute length (minutes)</label>
          <input
            id="mute-minutes"
            type="number"
            min={1}
            max={1440}
            value={muteMinutes}
            onChange={(e) => setMuteMinutes(Number(e.target.value))}
          />
          <span className="muted">applies to every Mute button below</span>
        </div>
        {loading && (
          <div className="skeleton-stack" aria-hidden="true">
            <div className="skeleton skeleton-block" style={{ height: 96 }} />
            <div className="skeleton skeleton-block" style={{ height: 96 }} />
          </div>
        )}
        {!loading && watches.length === 0 && (
          <EmptyState>
            No watches yet. Ask the Agent in chat to watch a page — the
            extension checks it in an open tab, on schedule.
          </EmptyState>
        )}
        <ul className="watch-list">
          {watches.map((w) => (
            <li key={w.id} className={w.status === "broken" ? "watch-broken" : ""}>
              <div className="watch-head">
                <a href={w.url} target="_blank" rel="noreferrer" className="watch-url" title={w.url}>
                  {w.url}
                </a>
                <StatusChip tone={statusTone(w.status)}>{w.status}</StatusChip>
              </div>
              <SummaryChips
                items={[
                  { label: "", value: CONDITION_LABELS[w.condition] ?? w.condition },
                  { label: "", value: `every ${w.interval_min} min` },
                  ...(w.misses > 0
                    ? [
                        {
                          label: "",
                          value: `${w.misses} missed check${w.misses === 1 ? "" : "s"}`,
                        },
                      ]
                    : []),
                ]}
              />
              <p className="muted watch-meta">
                {w.last_checked ? (
                  <>
                    Checked <RelativeTime ts={w.last_checked} />
                  </>
                ) : (
                  "Never checked"
                )}
                {w.last_fired_at ? (
                  <>
                    {" · "}Fired <RelativeTime ts={w.last_fired_at} />
                  </>
                ) : (
                  ""
                )}
                {w.status === "muted" && w.muted_until ? (
                  <>
                    {" · "}Muted until <RelativeTime ts={w.muted_until} />
                  </>
                ) : (
                  ""
                )}
              </p>
              <Disclosure summary="Details" className="watch-details">
                <dl className="kv">
                  <dt>Page</dt>
                  <dd className="watch-detail-url">{w.url}</dd>
                  <dt>Selector</dt>
                  <dd className="mono">{w.selector}</dd>
                  <dt>Last value</dt>
                  <dd>{w.last_value ?? "No baseline yet"}</dd>
                  <dt>Created</dt>
                  <dd>
                    <RelativeTime ts={w.created_at} />
                  </dd>
                </dl>
              </Disclosure>
              <div className="task-actions">
                {w.status === "muted" ? (
                  <button
                    type="button"
                    onClick={() => void runWatchAction(w.id, "unmute")}
                    disabled={pending !== null}
                  >
                    Unmute
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => void runWatchAction(w.id, "mute")}
                    disabled={pending !== null}
                  >
                    {pending === `mute:${w.id}` ? "Muting…" : `Mute ${muteMinutes}m`}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => {
                    setRepairId(w.id);
                    setRepairSelector(w.selector);
                    setRepairUrl(w.url);
                  }}
                  disabled={pending !== null}
                >
                  Repair
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setCheckId(w.id);
                    setCheckValue(w.last_value ?? "");
                    setCheckFound(true);
                  }}
                  disabled={pending !== null}
                >
                  Report a check
                </button>
                <button
                  type="button"
                  onClick={() => setDeleteId(w.id)}
                  disabled={pending !== null}
                >
                  Delete
                </button>
              </div>
              {repairId === w.id && (
                <form
                  className="inline-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void runWatchAction(w.id, "repair");
                  }}
                >
                  <p className="muted">
                    Repair re-picks the element a broken watch follows and
                    restarts it from a fresh baseline.
                  </p>
                  <label htmlFor={`repair-selector-${w.id}`}>New selector</label>
                  <input
                    id={`repair-selector-${w.id}`}
                    value={repairSelector}
                    onChange={(e) => setRepairSelector(e.target.value)}
                  />
                  <label htmlFor={`repair-url-${w.id}`}>New URL (optional)</label>
                  <input
                    id={`repair-url-${w.id}`}
                    type="url"
                    value={repairUrl}
                    onChange={(e) => setRepairUrl(e.target.value)}
                  />
                  <div className="task-actions">
                    <button type="submit" className="primary" disabled={pending !== null}>
                      {pending === `repair:${w.id}` ? "Repairing…" : "Repair watch"}
                    </button>
                    <button type="button" onClick={() => setRepairId(null)}>
                      Cancel
                    </button>
                  </div>
                </form>
              )}
              {checkId === w.id && (
                <form
                  className="inline-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void runWatchAction(w.id, "check");
                  }}
                >
                  <p className="muted">
                    Report what you see on the page right now. The server
                    evaluates it against the watch's condition — a firing
                    is written to the audit log.
                  </p>
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={checkFound}
                      onChange={(e) => setCheckFound(e.target.checked)}
                    />
                    The element is on the page
                  </label>
                  {checkFound && (
                    <>
                      <label htmlFor={`check-value-${w.id}`}>Current value</label>
                      <input
                        id={`check-value-${w.id}`}
                        value={checkValue}
                        onChange={(e) => setCheckValue(e.target.value)}
                        required
                      />
                    </>
                  )}
                  <div className="task-actions">
                    <button type="submit" className="primary" disabled={pending !== null}>
                      {pending === `check:${w.id}` ? "Checking…" : "Submit check"}
                    </button>
                    <button type="button" onClick={() => setCheckId(null)}>
                      Cancel
                    </button>
                  </div>
                </form>
              )}
            </li>
          ))}
        </ul>
      </section>

      <div className="split">
        <section aria-label="Watch events" className="pane">
          <SectionHeader title="Events" />
          {events.length === 0 ? (
            <EmptyState>No watch has fired yet. Firings land here.</EmptyState>
          ) : (
            <>
              <ul className="watch-events-list">
                {events.slice(0, eventsShown).map((e) => (
                  <li key={e.id}>
                    <ActivityLine
                      tone={e.kind === "fired" ? "warn" : "bad"}
                      title={e.detail}
                      detail={
                        e.old_value !== null || e.new_value !== null ? (
                          <p className="muted watch-event-values">
                            {e.old_value !== null && (
                              <span>Was: {e.old_value}</span>
                            )}
                            {e.new_value !== null && (
                              <span>Now: {e.new_value}</span>
                            )}
                          </p>
                        ) : undefined
                      }
                      when={e.created_at}
                    />
                  </li>
                ))}
              </ul>
              <ShowMore
                total={events.length}
                shown={eventsShown}
                onShowMore={() => setEventsShown(events.length)}
                label="events"
              />
            </>
          )}
        </section>

        <section aria-label="Morning briefing" className="pane">
          <SectionHeader
            title="Morning briefing"
            action={
              <button
                type="button"
                className="primary"
                onClick={() => void buildBriefing()}
                disabled={building}
              >
                {building ? <Spinner label="Building" /> : "Build briefing now"}
              </button>
            }
          />
          <p className="muted">
            A briefing never sends mail — it shows, you act.
          </p>
          {!briefing ? (
            <EmptyState>
              No briefing built yet — build one to see what the morning
              report looks like.
            </EmptyState>
          ) : briefing.cards.length === 0 ? (
            <p className="muted">
              Briefing from <RelativeTime ts={briefing.created_at} /> — nothing
              to report.
            </p>
          ) : (
            <div>
              <p className="muted">
                Built <RelativeTime ts={briefing.created_at} />
              </p>
              <ul className="briefing-cards">
                {briefing.cards.map((c, i) => (
                  <li key={`${c.kind}-${i}`}>
                    <Disclosure summary={c.title}>
                      <p>{c.body}</p>
                      {c.action && c.action_label && (
                        // Sprint 9 fix (review M5): offer cards carry a
                        // Gmail id, not a URL — render those as plain text,
                        // never an <a> with no href.
                        c.action.startsWith("http") ? (
                          <a href={c.action} target="_blank" rel="noreferrer">
                            {c.action_label}
                          </a>
                        ) : (
                          <span className="muted">{c.action_label}</span>
                        )
                      )}
                    </Disclosure>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      </div>

      <ConfirmDialog
        open={deleteId !== null}
        title="Delete this watch?"
        confirmLabel="Delete watch"
        danger
        onConfirm={() => {
          const id = deleteId;
          setDeleteId(null);
          if (id) void runWatchAction(id, "delete");
        }}
        onCancel={() => setDeleteId(null)}
      >
        <p>
          The watch and its history are deleted. The extension stops
          checking the page.
        </p>
      </ConfirmDialog>
    </div>
  );
}
