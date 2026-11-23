import { useState } from "react";
import {
  api,
  isHttpError,
  type ConnectorLiveStatus,
  type ConnectorRow,
  type DriveFile,
  type FreebusyResult,
  type GmailOffer,
} from "../api";
import { displayValue, usePoll, useRelativeNow, useTaskList } from "../hooks";
import { ConfirmDialog } from "../components/ConfirmDialog";
import {
  Disclosure,
  EmptyState,
  ErrorBanner,
  HumanError,
  RawData,
  RelativeTime,
  SectionHeader,
  Spinner,
  StatusChip,
  humanStatus,
  statusTone,
  type ChipTone,
} from "../components/ui";
import "./ConnectorsView.css";

/** Turn a datetime-local value into an RFC3339 timestamp with
 * a timezone offset (the server requires one). */
function toRfc3339(local: string): string {
  return new Date(local).toISOString();
}

/** A future unix-seconds timestamp as "in 8 min" / "in 2 hr".
 * fmtRelative is past-facing ("2 min ago") and clamps future
 * timestamps to "just now", so approvals need their own
 * formatter. Past timestamps read "expired". */
function fmtIn(ts: number, nowMs: number): string {
  const diffS = Math.floor(ts - nowMs / 1000);
  if (diffS <= 0) return "expired";
  if (diffS < 90) return "in under a minute";
  const mins = Math.floor(diffS / 60);
  if (mins < 60) return mins === 1 ? "in 1 min" : `in ${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours === 1 ? "in 1 hr" : `in ${hours} hr`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "in 1 day" : `in ${days} days`;
}

/** One plain-words line for a connector's CONNECTION state.
 * This is about whether Eclipse can actually reach the
 * account — never the enabled on/off flag, which only says
 * whether Eclipse is allowed to use it (shown separately). */
function stateLine(row: ConnectorRow, live?: ConnectorLiveStatus): string {
  const state = live?.state ?? row.state;
  switch (state) {
    case "connected":
      return "Connected and ready.";
    case "needs_reconnect":
      return "Not connected — the Google grant expired or was revoked.";
    case "unavailable":
      return "Unavailable right now — the check couldn't reach it.";
    case null:
    case undefined:
      return "Not checked yet — check the live status to see whether it's connected.";
    default:
      return `Status: ${state.replaceAll("_", " ")}.`;
  }
}

/** The connection chip: Connected / Not connected /
 * Unavailable / Not checked yet. The enabled on/off flag is
 * never shown as a connection — "On" used to read as
 * connected when the account was never linked. */
function connectionChip(state: string | null | undefined): {
  label: string;
  tone: ChipTone;
} {
  switch (state) {
    case "connected":
      return { label: "Connected", tone: "ok" };
    case "needs_reconnect":
      return { label: "Not connected", tone: "warn" };
    case "unavailable":
      return { label: "Unavailable", tone: "bad" };
    case null:
    case undefined:
      return { label: "Not checked yet", tone: "plain" };
    default:
      return { label: humanStatus(state), tone: statusTone(state) };
  }
}

/** One offer mail: subject, sender and date, redacted body
 * clamped to three lines with a way back to the full text. */
function OfferRow({ offer }: { offer: GmailOffer }) {
  const [expanded, setExpanded] = useState(false);
  const long = offer.body_redacted.length > 220;
  return (
    <li className="offer-row">
      <p className="offer-subject">{offer.subject}</p>
      <p className="muted offer-meta">
        From {offer.from} · {offer.date}
      </p>
      <p className={expanded || !long ? "offer-body" : "offer-body offer-clamp"}>
        {offer.body_redacted}
      </p>
      {long && (
        <button type="button" onClick={() => setExpanded((v) => !v)}>
          {expanded ? "Show less" : "Show full mail"}
        </button>
      )}
    </li>
  );
}

/** Connectors view: Gmail, Calendar, Drive — on/off, live
 * status, offers, free/busy, event creation and uploads.
 *
 * External actions need a single-use approval naming the exact
 * action (§7): approve the plain-words card first, then the
 * action consumes the approval. A failed action leaves the
 * approval usable for a retry. */
export function ConnectorsView({ token }: { token: string | null }) {
  const [rows, setRows] = useState<ConnectorRow[]>([]);
  const [live, setLive] = useState<Record<string, ConnectorLiveStatus>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [disableName, setDisableName] = useState<string | null>(null);
  const now = useRelativeNow();

  const [offers, setOffers] = useState<GmailOffer[] | null>(null);
  const [offersError, setOffersError] = useState<string | null>(null);

  const [fbStart, setFbStart] = useState("");
  const [fbEnd, setFbEnd] = useState("");
  const [freebusy, setFreebusy] = useState<FreebusyResult | null>(null);

  const [resume, setResume] = useState<DriveFile | null>(null);
  const [resumeMissing, setResumeMissing] = useState(false);

  const { tasks } = useTaskList(token);

  // Calendar event form + its two-step approval.
  const [eventTaskId, setEventTaskId] = useState("");
  const [eventSummary, setEventSummary] = useState("");
  const [eventStart, setEventStart] = useState("");
  const [eventEnd, setEventEnd] = useState("");
  const [eventDescription, setEventDescription] = useState("");
  const [eventLocation, setEventLocation] = useState("");
  const [eventApprovalId, setEventApprovalId] = useState<string | null>(null);
  const [eventApprovalExpires, setEventApprovalExpires] = useState<number | null>(null);
  const [eventResult, setEventResult] = useState<string | null>(null);

  // Drive upload form + its two-step approval.
  const [uploadTaskId, setUploadTaskId] = useState("");
  const [uploadPath, setUploadPath] = useState("");
  const [uploadName, setUploadName] = useState("");
  const [uploadApprovalId, setUploadApprovalId] = useState<string | null>(null);
  const [uploadResult, setUploadResult] = useState<string | null>(null);

  const load = async () => {
    try {
      const data = await api.listConnectors();
      setRows(data.connectors);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  usePoll(load, [token], 30000);

  const checkLive = async (name: string) => {
    setPending(`status:${name}`);
    setError(null);
    try {
      const status = await api.connectorStatus(name);
      setLive((prev) => ({ ...prev, [name]: status }));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const setEnabled = async (name: string, enabled: boolean) => {
    setPending(`${enabled ? "enable" : "disable"}:${name}`);
    setError(null);
    try {
      if (enabled) await api.enableConnector(name);
      else await api.disableConnector(name);
      setDisableName(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const loadOffers = async () => {
    setPending("offers");
    setOffersError(null);
    try {
      const data = await api.gmailOffers();
      setOffers(data.offers);
    } catch (err) {
      setOffersError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const checkFreebusy = async () => {
    if (!fbStart || !fbEnd) return;
    setPending("freebusy");
    setError(null);
    try {
      const data = await api.calendarFreebusy(toRfc3339(fbStart), toRfc3339(fbEnd));
      setFreebusy(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const loadResume = async () => {
    setPending("resume");
    setError(null);
    setResumeMissing(false);
    try {
      const data = await api.driveResume();
      setResume(data);
    } catch (err) {
      if (isHttpError(err, 404)) {
        setResume(null);
        setResumeMissing(true);
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setPending(null);
    }
  };

  const approveEvent = async () => {
    setPending("approve-event");
    setError(null);
    setEventResult(null);
    try {
      const approval = await api.createConnectorApproval({
        task_id: eventTaskId,
        kind: "calendar_event",
        summary: eventSummary.trim(),
        start: toRfc3339(eventStart),
      });
      setEventApprovalId(approval.approval_id);
      setEventApprovalExpires(approval.expires_at);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const createEvent = async () => {
    if (!eventApprovalId) return;
    setPending("create-event");
    setError(null);
    try {
      const event = await api.createCalendarEvent({
        task_id: eventTaskId,
        approval_id: eventApprovalId,
        summary: eventSummary.trim(),
        start: toRfc3339(eventStart),
        end: toRfc3339(eventEnd),
        description: eventDescription.trim(),
        location: eventLocation.trim(),
      });
      setEventResult(
        event.duplicate
          ? `That event already exists (id ${event.id ?? "—"}) — no duplicate was created.`
          : `Event created (id ${event.id ?? "—"}).`,
      );
      setEventApprovalId(null);
      setEventApprovalExpires(null);
    } catch (err) {
      // A failed create leaves the approval usable for a retry.
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const approveUpload = async () => {
    setPending("approve-upload");
    setError(null);
    setUploadResult(null);
    try {
      const approval = await api.createConnectorApproval({
        task_id: uploadTaskId,
        kind: "drive_upload",
        path: uploadPath.trim(),
      });
      setUploadApprovalId(approval.approval_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const upload = async () => {
    if (!uploadApprovalId) return;
    setPending("upload");
    setError(null);
    try {
      const file = await api.driveUpload({
        task_id: uploadTaskId,
        approval_id: uploadApprovalId,
        path: uploadPath.trim(),
        name: uploadName.trim() || undefined,
      });
      setUploadResult(`Uploaded to Drive (file id ${file.id}).`);
      setUploadApprovalId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const gmailOn = rows.find((r) => r.name === "gmail")?.enabled;
  const calendarOn = rows.find((r) => r.name === "calendar")?.enabled;
  const driveOn = rows.find((r) => r.name === "drive")?.enabled;

  return (
    <div className="view connectors-view">
      <div className="view-head">
        <h2>Connectors</h2>
        <p className="muted">
          Gmail, Calendar and Drive, through your own grants. Turning a
          connector off stops Eclipse using it — it does not revoke the
          Google grant; that happens in the platform Settings.
        </p>
      </div>
      {error && <ErrorBanner error={error} onRetry={() => void load()} />}

      <section aria-label="Connector status" className="pane">
        <SectionHeader title="Status" />
        {loading && <Spinner label="Loading connectors" />}
        {!loading && rows.length === 0 && (
          <EmptyState>
            No connectors are registered yet. The server registers Gmail,
            Calendar and Drive on first run.
          </EmptyState>
        )}
        <ul className="connector-grid">
          {rows.map((row) => {
            const status = live[row.name];
            const state = status?.state ?? row.state;
            const chip = connectionChip(state);
            // The server's connect URL comes back with the
            // live status check — it is the only way to link
            // an account from here. Shown whenever the
            // connector is not already connected.
            const connectUrl = state === "connected" ? undefined : status?.connect_url;
            return (
              <li key={row.name} className="connector-card">
                <div className="connector-card-head">
                  <h3>{row.name}</h3>
                  <span className="connector-chips">
                    <StatusChip tone={chip.tone}>{chip.label}</StatusChip>
                  </span>
                </div>
                <p className="connector-state">{stateLine(row, status)}</p>
                <p className="muted connector-checked">
                  {row.last_checked ? (
                    <>
                      Checked <RelativeTime ts={row.last_checked} />
                    </>
                  ) : (
                    "Not checked yet"
                  )}
                </p>
                {row.last_error && (
                  <HumanError
                    title="The last check failed"
                    detail={row.last_error}
                    onRetry={() => void checkLive(row.name)}
                  />
                )}
                {connectUrl ? (
                  <p className="connector-connect">
                    {state === "needs_reconnect"
                      ? "The Google grant expired or was revoked. "
                      : state === "unavailable"
                        ? "The check couldn't reach it — try connecting again. "
                        : "This account isn't linked yet. "}
                    <a className="btn btn-primary" href={connectUrl} target="_blank" rel="noreferrer">
                      Connect
                    </a>
                  </p>
                ) : state !== "connected" ? (
                  <p className="muted connector-connect-hint">
                    Check the live status to get a Connect link for this account.
                  </p>
                ) : null}
                {status?.error && (
                  <HumanError title="The live check failed" detail={status.error} />
                )}
                {/* The enabled switch is a SEPARATE control
                  * from the connection above: it only stops
                  * Eclipse using the connector, it is not a
                  * connection and never revokes the grant. */}
                <label className="check-row connector-enabled-row">
                  <input
                    type="checkbox"
                    className="toggle"
                    checked={row.enabled}
                    disabled={pending !== null}
                    onChange={(e) => {
                      if (e.target.checked) void setEnabled(row.name, true);
                      else setDisableName(row.name);
                    }}
                    aria-label={`Use ${row.name}`}
                  />
                  <span>
                    Use this connector — {row.enabled ? "On" : "Off"}
                  </span>
                </label>
                <p className="muted connector-enabled-help">
                  Off only stops Eclipse using it; the Google grant stays
                  until revoked in the platform Settings.
                </p>
                <div className="task-actions connector-actions">
                  <button
                    type="button"
                    onClick={() => void checkLive(row.name)}
                    disabled={pending !== null}
                  >
                    {pending === `status:${row.name}` ? "Checking…" : "Check live status"}
                  </button>
                </div>
                <RawData label="Details" data={row} />
              </li>
            );
          })}
        </ul>
      </section>

      <div className="split">
        <section aria-label="Gmail offers" className="pane">
          <SectionHeader
            title="Gmail offers"
            action={
              <button
                type="button"
                onClick={() => void loadOffers()}
                disabled={pending !== null || !gmailOn}
              >
                {pending === "offers" ? "Scanning…" : "Scan inbox"}
              </button>
            }
          />
          <p className="muted">
            Offer and interview mails, with bodies and subjects redacted
            before they reach this screen.
          </p>
          {!gmailOn && <p className="muted">Turn Gmail on to scan the inbox.</p>}
          {offersError && (
            <HumanError
              title="The inbox scan failed"
              detail={offersError}
              onRetry={() => void loadOffers()}
            />
          )}
          {offers === null ? (
            <EmptyState>No scan yet.</EmptyState>
          ) : offers.length === 0 ? (
            <EmptyState>No offer or interview mails found.</EmptyState>
          ) : (
            <ul className="plain-list offer-list">
              {offers.map((o) => (
                <OfferRow key={o.id} offer={o} />
              ))}
            </ul>
          )}
        </section>

        <section aria-label="Calendar free and busy" className="pane">
          <SectionHeader title="Calendar free/busy" />
          {!calendarOn && <p className="muted">Turn Calendar on to check free/busy.</p>}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void checkFreebusy();
            }}
          >
            <label htmlFor="fb-start">From</label>
            <input
              id="fb-start"
              type="datetime-local"
              value={fbStart}
              onChange={(e) => setFbStart(e.target.value)}
              required
            />
            <label htmlFor="fb-end">To</label>
            <input
              id="fb-end"
              type="datetime-local"
              value={fbEnd}
              onChange={(e) => setFbEnd(e.target.value)}
              required
            />
            <button type="submit" disabled={pending !== null || !calendarOn}>
              {pending === "freebusy" ? "Checking…" : "Check"}
            </button>
          </form>
          {freebusy && (
            <div className="freebusy-result">
              <p role="status" className={freebusy.free ? "success" : "warning"}>
                {freebusy.free
                  ? "Free — nothing on the calendar in that window."
                  : `Busy — ${freebusy.busy.length} event${freebusy.busy.length === 1 ? "" : "s"} in that window.`}
              </p>
              {freebusy.busy.length > 0 && (
                <ul className="plain-list">
                  {freebusy.busy.map((slot, i) => (
                    <li key={i} className="mono freebusy-slot">
                      {slot.start} → {slot.end}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </section>
      </div>

      <section aria-label="Create a calendar event" className="pane">
        <SectionHeader title="Create a calendar event" />
        {!calendarOn && <p className="muted">Turn Calendar on to create events.</p>}
        {tasks.length === 0 ? (
          <EmptyState>No tasks yet — an event approval belongs to a task.</EmptyState>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (eventApprovalId) void createEvent();
              else void approveEvent();
            }}
          >
            <label htmlFor="event-task">Task</label>
            <select
              id="event-task"
              value={eventTaskId}
              onChange={(e) => setEventTaskId(e.target.value)}
              required
              disabled={eventApprovalId !== null}
            >
              <option value="">Pick a task…</option>
              {tasks.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.goal} ({t.status})
                </option>
              ))}
            </select>
            <div className="form-grid">
              <div>
                <label htmlFor="event-summary">Summary</label>
                <input
                  id="event-summary"
                  value={eventSummary}
                  onChange={(e) => setEventSummary(e.target.value)}
                  required
                  disabled={eventApprovalId !== null}
                />
              </div>
              <div>
                <label htmlFor="event-location">Location</label>
                <input
                  id="event-location"
                  value={eventLocation}
                  onChange={(e) => setEventLocation(e.target.value)}
                  disabled={eventApprovalId !== null}
                />
              </div>
              <div>
                <label htmlFor="event-start">Start</label>
                <input
                  id="event-start"
                  type="datetime-local"
                  value={eventStart}
                  onChange={(e) => setEventStart(e.target.value)}
                  required
                  disabled={eventApprovalId !== null}
                />
              </div>
              <div>
                <label htmlFor="event-end">End</label>
                <input
                  id="event-end"
                  type="datetime-local"
                  value={eventEnd}
                  onChange={(e) => setEventEnd(e.target.value)}
                  required
                  disabled={eventApprovalId !== null}
                />
              </div>
            </div>
            <label htmlFor="event-description">Description</label>
            <textarea
              id="event-description"
              value={eventDescription}
              onChange={(e) => setEventDescription(e.target.value)}
              rows={3}
              disabled={eventApprovalId !== null}
            />
            {eventApprovalId === null ? (
              <div>
                <p className="muted">
                  Step 1 of 2: approve this exact event. The approval is
                  single-use and names this summary and start time.
                </p>
                <button
                  type="submit"
                  className="primary"
                  disabled={pending !== null || !calendarOn}
                >
                  {pending === "approve-event" ? "Approving…" : "Approve this event"}
                </button>
              </div>
            ) : (
              <div className="approval-card" role="group" aria-label="Event approved">
                <h3>Approved — create the event?</h3>
                <p>
                  “{eventSummary}” from {displayValue(toRfc3339(eventStart))}.
                  The approval expires{" "}
                  {eventApprovalExpires ? fmtIn(eventApprovalExpires, now) : "in 10 minutes"}.
                  Creating the same event twice never duplicates it.
                </p>
                <div className="task-actions">
                  <button type="submit" className="primary" disabled={pending !== null}>
                    {pending === "create-event" ? "Creating…" : "Create event"}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setEventApprovalId(null);
                      setEventApprovalExpires(null);
                    }}
                  >
                    Change details
                  </button>
                </div>
              </div>
            )}
            {eventResult && (
              <p role="status" className="success">
                {eventResult}
              </p>
            )}
          </form>
        )}
      </section>

      <div className="split">
        <section aria-label="Resume in Drive" className="pane">
          <SectionHeader
            title="Resume in Drive"
            action={
              <button
                type="button"
                onClick={() => void loadResume()}
                disabled={pending !== null || !driveOn}
              >
                {pending === "resume" ? "Looking…" : "Find resume"}
              </button>
            }
          />
          <p className="muted">
            The newest file in Drive whose name contains “resume” or “CV”.
            This is a separate lookup from the local resume file the mail
            flow attaches — the two may hold different copies.
          </p>
          {!driveOn && <p className="muted">Turn Drive on to look it up.</p>}
          {resumeMissing && <EmptyState>No resume found in Drive.</EmptyState>}
          {resume && (
            <dl className="kv">
              <dt>Name</dt>
              <dd>{resume.name ?? "—"}</dd>
              <dt>File id</dt>
              <dd className="mono">{resume.id}</dd>
              <dt>Modified</dt>
              <dd>{resume.modifiedTime ?? "—"}</dd>
              {resume.webViewLink && (
                <>
                  <dt>Link</dt>
                  <dd>
                    <a href={resume.webViewLink} target="_blank" rel="noreferrer">
                      Open in Drive
                    </a>
                  </dd>
                </>
              )}
            </dl>
          )}
        </section>

        <section aria-label="Upload to Drive" className="pane">
          <SectionHeader title="Upload to Drive" />
          {!driveOn && <p className="muted">Turn Drive on to upload.</p>}
          {tasks.length === 0 ? (
            <EmptyState>No tasks yet — an upload approval belongs to a task.</EmptyState>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (uploadApprovalId) void upload();
                else void approveUpload();
              }}
            >
              <label htmlFor="upload-task">Task</label>
              <select
                id="upload-task"
                value={uploadTaskId}
                onChange={(e) => setUploadTaskId(e.target.value)}
                required
                disabled={uploadApprovalId !== null}
              >
                <option value="">Pick a task…</option>
                {tasks.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.goal} ({t.status})
                  </option>
                ))}
              </select>
              <label htmlFor="upload-path">Absolute path (inside the home directory)</label>
              <input
                id="upload-path"
                value={uploadPath}
                onChange={(e) => setUploadPath(e.target.value)}
                placeholder="/home/user/…"
                required
                disabled={uploadApprovalId !== null}
              />
              <label htmlFor="upload-name">Name in Drive (optional)</label>
              <input
                id="upload-name"
                value={uploadName}
                onChange={(e) => setUploadName(e.target.value)}
                disabled={uploadApprovalId !== null}
              />
              {uploadApprovalId === null ? (
                <button type="submit" className="primary" disabled={pending !== null || !driveOn}>
                  {pending === "approve-upload" ? "Approving…" : "Approve this upload"}
                </button>
              ) : (
                <div className="approval-card" role="group" aria-label="Upload approved">
                  <h3>Approved — upload the file?</h3>
                  <p>
                    “{uploadPath}”{uploadName ? ` as “${uploadName}”` : ""}. The
                    approval names this path and nothing else.
                  </p>
                  <div className="task-actions">
                    <button type="submit" className="primary" disabled={pending !== null}>
                      {pending === "upload" ? "Uploading…" : "Upload"}
                    </button>
                    <button type="button" onClick={() => setUploadApprovalId(null)}>
                      Change file
                    </button>
                  </div>
                </div>
              )}
              {uploadResult && (
                <p role="status" className="success">
                  {uploadResult}
                </p>
              )}
            </form>
          )}
        </section>
      </div>

      <Disclosure summary="How approvals work">
        <p className="muted">
          Creating an event or uploading a file takes two steps: first
          approve the exact action, then run it. The approval is
          single-use and names the task, the summary or the path — nothing
          else. A failed run leaves the approval usable for a retry.
        </p>
      </Disclosure>

      <ConfirmDialog
        open={disableName !== null}
        title={`Turn off ${disableName ?? "this connector"}?`}
        confirmLabel="Turn off"
        danger
        onConfirm={() => {
          const name = disableName;
          if (name) void setEnabled(name, false);
        }}
        onCancel={() => setDisableName(null)}
      >
        <p>
          Eclipse stops using {disableName}. Sends, scans and lookups through
          it will fail until it is turned back on. The Google grant itself
          is not revoked here.
        </p>
      </ConfirmDialog>
    </div>
  );
}
