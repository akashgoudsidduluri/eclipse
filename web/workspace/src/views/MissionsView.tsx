import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  api,
  isHttpError,
  type Draft,
  type IntakeResponse,
  type MailLogEntry,
  type Mission,
  type MissionActivity,
  type MissionApproval,
  type MissionBounds,
  type MissionDetail,
  type MissionType,
  type MissionTypeSpec,
  type Profile,
  type ResumeStatus,
} from "../api";
import { displayValue, fmtRelative, usePoll, useRelativeNow } from "../hooks";
import { ConfirmDialog } from "../components/ConfirmDialog";
import {
  ActivityLine,
  ApprovalBanner,
  Disclosure,
  EmptyState,
  ErrorBanner,
  HumanError,
  RawData,
  RelativeTime,
  SectionHeader,
  ShowMore,
  Spinner,
  StatusChip,
  humanStatus,
  statusTone,
  SummaryChips,
} from "../components/ui";
import "./MissionsView.css";

/** Missions (PR #5, rebuilt PR #6): chat intake → standing
 * mission → monitor → act → 9 AM briefing. The view creates
 * missions from a drafted intake message, shows their live
 * state, and holds the approval gate for their consequential
 * actions. It never executes — browser work runs in the
 * extension, server work in the runner. Missions only run
 * while this laptop and server are on.
 *
 * PR #6: summary first, JSON never on screen. The draft is
 * plain sentences plus small edit fields (the raw draft sits
 * behind RawData); mission cards lead with their state and
 * the number that matters; config reads as chips; activity
 * reads as sentences; failures stay failures, in plain words.
 *
 * Modern-web-guidance applied here (guides: forms,
 * ime-safe-enter-submit): labelled controls with help text
 * via aria-describedby, fieldset/legend for the bounds
 * group, IME-safe Enter-to-submit on the composer, and
 * actionable submit-button language. */

const KNOWN_TYPES: { type: MissionType; label: string }[] = [
  { type: "job_hunt", label: "Job hunt" },
  { type: "cold_mail", label: "Cold mail" },
  { type: "price_watch", label: "Price watch" },
  { type: "email_monitor", label: "Inbox watch" },
];

/** Missing intake fields, phrased as the question the user
 * would answer. Unknown fields fall back to the raw name —
 * never silently dropped. */
const MISSING_FIELD_PROMPTS: Record<string, string> = {
  stipend_min: "Minimum stipend (₹/month)?",
  allowed_sites: "Which sites may it use?",
  recipients: "Who should it mail?",
  role_ids: "Which roles should it mail about?",
  roles: "Which roles should it mail about?",
  locations: "Where should it look — Remote, Hyderabad, or somewhere else?",
  url: "Which page should it watch?",
  selector: "Which element (CSS selector) should it watch?",
  target_price: "At what price should it act?",
  currency: "Which currency?",
  min_score: "Minimum match score (0–100)?",
  sources: "Where should it look — Gmail, pasted roles, or both?",
  keywords: "Which words should it watch for in your inbox?",
};

/** Config keys the draft editor renders, per type. Anything
 * else in the draft config is carried through untouched —
 * the editor only writes the keys it renders, and a type
 * switch drops keys the new type does not know (the server
 * rejects unknown keys). */
const COMMON_CONFIG_KEYS = ["interval_min", "max_facts"];
const TYPE_CONFIG_KEYS: Record<MissionType, string[]> = {
  job_hunt: ["sources", "min_score", "stipend_min", "locations", "roles", "watch_new_only"],
  cold_mail: ["role_ids", "roles", "recipients", "attach_resume", "max_drafts_per_run"],
  price_watch: ["url", "selector", "condition", "target_price", "currency", "one_shot", "watch_id"],
  email_monitor: ["keywords"],
};

/** Missing fields the draft editor answers inline, per type
 * (roles is answered by the role-ids field for cold mail;
 * allowed_sites lives in the bounds editor). Anything else
 * still missing gets a plain list above the editor — never
 * silently dropped. */
const EDITABLE_MISSING: Record<MissionType, string[]> = {
  job_hunt: ["locations", "stipend_min", "min_score", "sources"],
  cold_mail: ["recipients", "role_ids", "roles"],
  price_watch: ["url", "selector", "target_price", "currency"],
  // email_monitor keywords always have a default, so the
  // field is never missing — nothing to answer inline.
  email_monitor: [],
};

type ActivityTone = "ok" | "warn" | "bad" | "info" | "plain";

function typeLabel(type: string, specs: MissionTypeSpec[]): string {
  return (
    specs.find((s) => s.type === type)?.label ??
    KNOWN_TYPES.find((t) => t.type === type)?.label ??
    type
  );
}

function formatINR(n: number): string {
  return `₹${n.toLocaleString("en-IN")}`;
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function humanizeKey(key: string): string {
  return key.replace(/_/g, " ");
}

/** Unix SECONDS in the future → "in 40 min" / "in 3 hr" /
 * "tomorrow" / a date. fmtRelative only looks backwards;
 * next-run times need this direction. Past or null clamps
 * to "now" / "—". */
function fmtInFuture(ts: number | null | undefined, nowMs: number): string {
  if (ts === null || ts === undefined) return "—";
  const diffS = Math.floor(ts - nowMs / 1000);
  if (diffS <= 0) return "now";
  if (diffS < 90) return "in 1 min";
  const mins = Math.floor(diffS / 60);
  if (mins < 60) return `in ${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours === 1 ? "in 1 hr" : `in ${hours} hr`;
  const days = Math.floor(hours / 24);
  if (days < 7) return days === 1 ? "tomorrow" : `in ${days} days`;
  return new Date(ts * 1000).toLocaleDateString();
}

/** An API error message is usually "422 on /missions:
 * {"detail":"..."}" — pull the detail out so the human
 * error shows the reason, not the transport wrapper. Falls
 * back to the whole message when there is no detail. */
function apiErrorDetail(msg: string): string {
  const brace = msg.indexOf("{");
  if (brace >= 0) {
    try {
      const parsed = JSON.parse(msg.slice(brace)) as { detail?: unknown };
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      /* fall through to the whole message */
    }
  }
  return msg;
}

/* ---------- config accessors (the config is untyped JSON) ---------- */

function cfgStr(config: Record<string, unknown>, key: string): string | null {
  const v = config[key];
  return typeof v === "string" && v !== "" ? v : null;
}

function cfgNum(config: Record<string, unknown>, key: string): number | null {
  const v = config[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function cfgStrList(config: Record<string, unknown>, key: string): string[] {
  const v = config[key];
  return Array.isArray(v)
    ? v.filter((x): x is string => typeof x === "string")
    : [];
}

function cfgBool(config: Record<string, unknown>, key: string): boolean | null {
  const v = config[key];
  return typeof v === "boolean" ? v : null;
}

function urlHost(url: string): string {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

function urlPath(url: string): string {
  try {
    const u = new URL(url);
    return u.pathname === "/" ? "" : u.pathname;
  } catch {
    return "";
  }
}

function conditionSummary(condition: string): string {
  if (condition.startsWith("contains:")) {
    return `when it mentions “${condition.slice("contains:".length)}”`;
  }
  switch (condition) {
    case "appeared":
      return "when the element appears";
    case "disappeared":
      return "when the element disappears";
    default:
      return "when the price changes";
  }
}

function intervalSummary(mins: number): string {
  if (mins >= 1440 && mins % 1440 === 0) {
    const days = mins / 1440;
    return days === 1 ? "checks every day" : `checks every ${days} days`;
  }
  if (mins >= 60 && mins % 60 === 0) {
    return mins === 60 ? "checks every hour" : `checks every ${mins / 60} hours`;
  }
  return `checks every ${mins} min`;
}

/** The draft, as plain sentences: "Watch for matching roles
 * · Remote or Hyderabad · ₹10,000+ stipend · at most 3
 * applies a day". Only set fields contribute — an empty
 * draft still reads as one honest sentence. */
function draftSummaryParts(
  type: MissionType,
  config: Record<string, unknown>,
  bounds: Partial<MissionBounds>,
): string[] {
  const parts: string[] = [];
  const interval = cfgNum(config, "interval_min");
  if (type === "job_hunt") {
    parts.push("Watch for matching roles");
    const locations = cfgStrList(config, "locations");
    if (locations.length > 0) parts.push(locations.join(" or "));
    const stipend = cfgNum(config, "stipend_min");
    if (stipend !== null) parts.push(`${formatINR(stipend)}+ stipend`);
    const score = cfgNum(config, "min_score");
    if (score !== null) parts.push(`match score ${score} or higher`);
    const sources = cfgStrList(config, "sources");
    if (sources.length > 0) parts.push(`from ${sources.join(" and ")}`);
    const applies = bounds.max_applies_per_day;
    if (applies !== undefined && applies > 0) {
      parts.push(`at most ${applies} ${applies === 1 ? "apply" : "applies"} a day`);
    }
  } else if (type === "cold_mail") {
    parts.push("Mail recruiters");
    const recipients = cfgStrList(config, "recipients");
    if (recipients.length > 0) {
      parts.push(
        `to ${recipients.slice(0, 2).join(", ")}${recipients.length > 2 ? ` +${recipients.length - 2} more` : ""}`,
      );
    }
    const roleIds = cfgStrList(config, "role_ids");
    if (roleIds.length > 0) {
      parts.push(`about ${roleIds.length} role${roleIds.length === 1 ? "" : "s"}`);
    }
    if (cfgBool(config, "attach_resume")) parts.push("with your resume attached");
    const sends = bounds.max_sends_per_day;
    if (sends !== undefined && sends > 0) {
      parts.push(`at most ${sends} ${sends === 1 ? "send" : "sends"} a day`);
    }
  } else {
    parts.push("Watch the price");
    const url = cfgStr(config, "url");
    if (url) parts.push(urlHost(url));
    const condition = cfgStr(config, "condition");
    if (condition) parts.push(conditionSummary(condition));
    const target = cfgNum(config, "target_price");
    if (target !== null) parts.push(`target ${formatINR(target)}`);
    if (cfgBool(config, "one_shot")) parts.push("stops once the target is hit");
  }
  if (interval !== null) parts.push(intervalSummary(interval));
  return parts;
}

/** A stored mission's config as human chips. Keys the chips
 * do not know fall through to a humanized label + value —
 * nothing is silently dropped. Roles are excluded: they get
 * their own RawData expand, never a JSON blob on screen. */
function configChips(
  type: string,
  config: Record<string, unknown>,
): { label: string; value: string }[] {
  const items: { label: string; value: string }[] = [];
  const seen = new Set<string>();
  const push = (key: string, label: string, value: string | null) => {
    seen.add(key);
    if (value !== null && value !== "") items.push({ label, value });
  };
  const interval = cfgNum(config, "interval_min");
  push("interval_min", "", interval !== null ? intervalSummary(interval) : null);
  if (type === "job_hunt") {
    const locations = cfgStrList(config, "locations");
    push("locations", "", locations.length > 0 ? locations.join(", ") : null);
    const stipend = cfgNum(config, "stipend_min");
    push("stipend_min", "", stipend !== null ? `${formatINR(stipend)}+ stipend` : null);
    const score = cfgNum(config, "min_score");
    push("min_score", "Min score", score !== null ? String(score) : null);
    const sources = cfgStrList(config, "sources");
    push("sources", "Sources", sources.length > 0 ? sources.join(", ") : null);
    const roles = Array.isArray(config.roles) ? config.roles : [];
    push("roles", "Pasted roles", roles.length > 0 ? String(roles.length) : null);
    push(
      "watch_new_only",
      "",
      cfgBool(config, "watch_new_only") === false ? "Re-checks seen roles" : null,
    );
  } else if (type === "cold_mail") {
    const recipients = cfgStrList(config, "recipients");
    push(
      "recipients",
      "Recipients",
      recipients.length > 0
        ? recipients.length > 2
          ? `${recipients.slice(0, 2).join(", ")} +${recipients.length - 2} more`
          : recipients.join(", ")
        : null,
    );
    const roleIds = cfgStrList(config, "role_ids");
    push(
      "role_ids",
      "Mailing about",
      roleIds.length > 0 ? `${roleIds.length} role${roleIds.length === 1 ? "" : "s"}` : null,
    );
    const roles = Array.isArray(config.roles) ? config.roles : [];
    push("roles", "Pasted roles", roles.length > 0 ? String(roles.length) : null);
    push("attach_resume", "", cfgBool(config, "attach_resume") ? "Resume attached" : null);
    const drafts = cfgNum(config, "max_drafts_per_run");
    push("max_drafts_per_run", "Drafts per run", drafts !== null ? `≤${drafts}` : null);
  } else if (type === "price_watch") {
    const url = cfgStr(config, "url");
    push("url", "Watching", url ? truncate(urlHost(url) + urlPath(url), 48) : null);
    const selector = cfgStr(config, "selector");
    push("selector", "Element", selector ? truncate(selector, 40) : null);
    const condition = cfgStr(config, "condition");
    push("condition", "Acts", condition ? conditionSummary(condition) : null);
    const target = cfgNum(config, "target_price");
    push("target_price", "Target", target !== null ? formatINR(target) : null);
    const currency = cfgStr(config, "currency");
    push("currency", "Currency", currency && currency !== "INR" ? currency : null);
    push("one_shot", "", cfgBool(config, "one_shot") ? "Stops at target" : null);
  }
  for (const [k, v] of Object.entries(config)) {
    if (seen.has(k) || k === "roles") continue;
    items.push({ label: humanizeKey(k), value: displayValue(v) });
  }
  return items;
}

/** A mission's bounds as chips: the caps first, then the
 * approval gate. */
function boundsChips(m: Mission): { label: string; value: string }[] {
  const b = m.bounds ?? ({} as MissionBounds);
  const items: { label: string; value: string }[] = [];
  if ((b.max_applies_per_day ?? 0) > 0) {
    items.push({ label: "", value: `≤${b.max_applies_per_day} applies/day` });
  }
  if ((b.max_sends_per_day ?? 0) > 0) {
    items.push({ label: "", value: `≤${b.max_sends_per_day} sends/day` });
  }
  items.push({ label: "", value: `≤${b.max_actions_per_day ?? 0} actions/day` });
  if (b.auto_approve) {
    items.push({ label: "", value: "Standing approval" });
  } else if ((b.require_approval_for ?? []).length > 0) {
    items.push({
      label: "Approval needed",
      value: (b.require_approval_for ?? []).join(", "),
    });
  } else {
    items.push({ label: "", value: "No approval gate" });
  }
  return items;
}

function filterConfigForType(
  config: Record<string, unknown>,
  type: MissionType,
): Record<string, unknown> {
  const keep = new Set([...COMMON_CONFIG_KEYS, ...TYPE_CONFIG_KEYS[type]]);
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(config)) {
    if (keep.has(k)) out[k] = v;
  }
  return out;
}

function activityTone(kind: string): ActivityTone {
  switch (kind) {
    case "applied":
    case "sent":
    case "approved":
      return "ok";
    case "refused":
    case "failed":
      return "bad";
    case "approval_needed":
    case "waiting_for_extension":
      return "warn";
    case "run_started":
    case "discovered":
    case "matched":
    case "drafted":
    case "price_observed":
      return "info";
    default:
      return "plain";
  }
}

/** Activity detail is a redacted JSON string server-side;
 * parse it once for the human title. */
function parseActivityDetail(detail: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(detail);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
    return null;
  } catch {
    return null;
  }
}

/** Activity detail rendered as readable key: value pairs,
 * or the raw string when it is not JSON. Only ever shown
 * behind the row's Details expand. */
function formatActivityDetail(detail: string): string {
  const parsed = parseActivityDetail(detail);
  if (parsed) {
    return Object.entries(parsed)
      .map(([k, v]) => `${k}: ${displayValue(v)}`)
      .join(" · ");
  }
  try {
    return displayValue(JSON.parse(detail) as unknown);
  } catch {
    return detail;
  }
}

/** The SEEDED demo entries keep their label — the detail
 * carries "SEEDED for briefing verification" verbatim. */
function isSeeded(detail: string): boolean {
  return detail.includes("SEEDED");
}

/** One activity row, as a sentence: "Applied to Software
 * Engineer Intern at Example Labs", "Matched a role (score
 * 88)". Unknown kinds fall back to the kind with underscores
 * as spaces — never dropped, never JSON. */
function activityTitle(a: MissionActivity): string {
  const d = parseActivityDetail(a.detail);
  const get = (k: string): string | null => {
    const v = d?.[k];
    return v === undefined || v === null ? null : String(v);
  };
  let base: string;
  switch (a.kind) {
    case "applied": {
      const title = get("title");
      const company = get("company");
      base = title
        ? `Applied to ${title}${company ? ` at ${company}` : ""}`
        : "Applied to a role";
      break;
    }
    case "sent":
      base = "Sent a mail";
      break;
    case "drafted":
      base = "Drafted a mail";
      break;
    case "matched": {
      const score = get("score");
      base = `Matched a role${score ? ` (score ${score})` : ""}`;
      break;
    }
    case "discovered": {
      const title = get("title");
      base = title ? `Found ${title}` : "Found a role";
      break;
    }
    case "run_started":
      base = "Run started";
      break;
    case "state_changed": {
      const from = get("from");
      const to = get("to");
      base = from && to ? `Changed from ${from} to ${to}` : "Status changed";
      break;
    }
    case "failed": {
      const reason = get("reason");
      base = reason ? `Run failed — ${truncate(reason, 80)}` : "Run failed";
      break;
    }
    case "refused":
      base = "Refused an action outside its bounds";
      break;
    case "approval_needed": {
      const action = get("action");
      base = action ? `Needs approval to ${action}` : "Needs your approval";
      break;
    }
    case "price_observed": {
      const price = get("price");
      base = price ? `Price observed: ${price}` : "Price observed";
      break;
    }
    case "approved":
      base = "Approval granted";
      break;
    case "waiting_for_extension":
      base = "Waiting for Chrome";
      break;
    default:
      base = humanizeKey(a.kind);
  }
  return isSeeded(a.detail) ? `${base} · Seeded` : base;
}

/** One send-log line in plain words: what happened, and to
 * whom. Unknown statuses fall back to the raw status so
 * nothing is ever hidden. */
function mailLogTitle(entry: MailLogEntry): string {
  if (entry.status === "sent") return `Sent to ${entry.recipient}`;
  if (entry.status === "bounced") return `Bounced — ${entry.recipient}`;
  return `${entry.status} — ${entry.recipient}`;
}

function extractionTone(e: string | null): "ok" | "warn" | "bad" | "plain" {
  switch (e) {
    case "ok":
      return "ok";
    case "limited":
      return "warn";
    case "failed":
      return "bad";
    default:
      return "plain";
  }
}

function extractionLabel(e: string | null): string {
  switch (e) {
    case "ok":
      return "parsed cleanly";
    case "limited":
      return "partly parsed";
    case "failed":
      return "parsing found nothing usable";
    default:
      return "extraction unknown";
  }
}

/** The editable bounds block for a draft mission. Bounds are
 * the one part of the draft with a fully specified shape
 * (DESIGN.md §3.5), so they get real controls. */
function BoundsEditor({
  bounds,
  onChange,
  missing,
}: {
  bounds: Partial<MissionBounds>;
  onChange: (next: Partial<MissionBounds>) => void;
  missing?: string[];
}) {
  const setNum = (
    key: "max_actions_per_day" | "max_applies_per_day" | "max_sends_per_day" | "min_score",
    value: string,
  ) => {
    onChange({ ...bounds, [key]: value === "" ? undefined : Number(value) });
  };
  const toggleApproval = (kind: string) => {
    const current = bounds.require_approval_for ?? [];
    onChange({
      ...bounds,
      require_approval_for: current.includes(kind)
        ? current.filter((k) => k !== kind)
        : [...current, kind],
    });
  };
  const sitesMissing = missing?.includes("allowed_sites") ?? false;
  return (
    <fieldset>
      <legend>Bounds — the mission cannot act outside these</legend>
      <div className="form-grid">
        <div>
          <label htmlFor="draft-max-actions">Max actions per day</label>
          <input
            id="draft-max-actions"
            type="number"
            min={0}
            value={bounds.max_actions_per_day ?? ""}
            onChange={(e) => setNum("max_actions_per_day", e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="draft-max-applies">Max applies per day</label>
          <input
            id="draft-max-applies"
            type="number"
            min={0}
            value={bounds.max_applies_per_day ?? ""}
            onChange={(e) => setNum("max_applies_per_day", e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="draft-max-sends">Max sends per day</label>
          <input
            id="draft-max-sends"
            type="number"
            min={0}
            value={bounds.max_sends_per_day ?? ""}
            onChange={(e) => setNum("max_sends_per_day", e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="draft-min-score">Minimum match score (0–100, job hunt)</label>
          <input
            id="draft-min-score"
            type="number"
            min={0}
            max={100}
            value={bounds.min_score ?? ""}
            onChange={(e) => setNum("min_score", e.target.value)}
          />
        </div>
      </div>
      <div className={sitesMissing ? "draft-field-missing" : undefined}>
        <label htmlFor="draft-sites">
          Allowed sites (comma-separated, blank = current site only)
          {sitesMissing ? <span className="draft-needed"> · Needed</span> : null}
        </label>
        <input
          id="draft-sites"
          value={(bounds.allowed_sites ?? []).join(", ")}
          onChange={(e) =>
            onChange({
              ...bounds,
              allowed_sites: e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            })
          }
          placeholder="internshala.com, linkedin.com"
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="none"
          spellCheck={false}
        />
        {sitesMissing && (
          <p className="draft-field-help draft-field-help-warn">
            {MISSING_FIELD_PROMPTS.allowed_sites}
          </p>
        )}
      </div>
      <fieldset>
        <legend>Needs your approval before</legend>
        <label className="check-row">
          <input
            type="checkbox"
            checked={(bounds.require_approval_for ?? []).includes("apply")}
            onChange={() => toggleApproval("apply")}
          />
          Applying to a role
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={(bounds.require_approval_for ?? []).includes("send")}
            onChange={() => toggleApproval("send")}
          />
          Sending a mail
        </label>
      </fieldset>
      <label className="check-row">
        <input
          type="checkbox"
          checked={bounds.auto_approve ?? false}
          onChange={(e) => onChange({ ...bounds, auto_approve: e.target.checked })}
        />
        Standing approval — apply/send within bounds without asking
      </label>
      {bounds.auto_approve && (
        <p role="note" className="warning">
          Standing approval only covers action kinds you UNCHECK above
          (“Needs your approval before”) — a kind left checked still asks
          you every time. Caps still apply, and every action still goes
          through the approval record.
        </p>
      )}
    </fieldset>
  );
}

/** One field of the draft config editor: label, control, and
 * either a missing-field prompt (warning) or plain help
 * text. A missing field is marked "Needed" on its label. */
function DraftField({
  id,
  label,
  missing,
  prompt,
  help,
  wide,
  children,
}: {
  id: string;
  label: string;
  missing?: boolean;
  prompt?: string;
  help?: string;
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <div
      className={`draft-field${wide ? " draft-field-wide" : ""}${missing ? " draft-field-missing" : ""}`}
    >
      <label htmlFor={id}>
        {label}
        {missing ? <span className="draft-needed"> · Needed</span> : null}
      </label>
      {children}
      {missing && prompt ? (
        <p className="draft-field-help draft-field-help-warn">{prompt}</p>
      ) : help ? (
        <p className="draft-field-help">{help}</p>
      ) : null}
    </div>
  );
}

/** The draft's config, as real fields — never a JSON
 * textarea. Fields the draft left empty prompt inline with
 * the question the user would answer. Unknown keys are
 * carried through untouched: the editor only writes the
 * keys it renders. */
function DraftConfigEditor({
  type,
  config,
  missing,
  onChange,
}: {
  type: MissionType;
  config: Record<string, unknown>;
  missing: string[];
  onChange: (next: Record<string, unknown>) => void;
}) {
  const set = (key: string, value: unknown) => onChange({ ...config, [key]: value });
  const setNum = (key: string, raw: string) =>
    set(key, raw === "" ? null : Number(raw));
  const setList = (key: string, raw: string) =>
    set(
      key,
      raw
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    );
  const toggleList = (key: string, item: string, on: boolean) => {
    const cur = cfgStrList(config, key);
    set(key, on ? [...cur, item] : cur.filter((s) => s !== item));
  };
  const isMissing = (key: string) => missing.includes(key);

  const condition = cfgStr(config, "condition") ?? "changed";
  const conditionKind = condition.startsWith("contains:") ? "contains" : condition;
  const containsText = condition.startsWith("contains:")
    ? condition.slice("contains:".length)
    : "";
  const roles = Array.isArray(config.roles) ? config.roles : [];

  return (
    <div className="draft-fields">
      {type === "job_hunt" && (
        <>
          <DraftField
            id="draft-locations"
            label="Locations"
            missing={isMissing("locations")}
            prompt={MISSING_FIELD_PROMPTS.locations}
            help="Comma-separated — blank means anywhere"
            wide
          >
            <input
              id="draft-locations"
              value={cfgStrList(config, "locations").join(", ")}
              onChange={(e) => setList("locations", e.target.value)}
              placeholder="Remote, Hyderabad"
              autoComplete="off"
            />
          </DraftField>
          <DraftField
            id="draft-stipend"
            label="Minimum stipend (₹/month)"
            missing={isMissing("stipend_min")}
            prompt={MISSING_FIELD_PROMPTS.stipend_min}
          >
            <input
              id="draft-stipend"
              type="number"
              min={0}
              value={cfgNum(config, "stipend_min") ?? ""}
              onChange={(e) => setNum("stipend_min", e.target.value)}
              placeholder="10000"
            />
          </DraftField>
          <DraftField
            id="draft-config-min-score"
            label="Minimum match score (0–100)"
            missing={isMissing("min_score")}
            prompt={MISSING_FIELD_PROMPTS.min_score}
          >
            <input
              id="draft-config-min-score"
              type="number"
              min={0}
              max={100}
              value={cfgNum(config, "min_score") ?? ""}
              onChange={(e) => setNum("min_score", e.target.value)}
            />
          </DraftField>
          <div className="draft-field draft-field-wide">
            <span className="draft-field-label">Where to look</span>
            <div className="draft-checks">
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={cfgStrList(config, "sources").includes("gmail")}
                  onChange={(e) => toggleList("sources", "gmail", e.target.checked)}
                />
                Gmail
              </label>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={cfgStrList(config, "sources").includes("pasted")}
                  onChange={(e) => toggleList("sources", "pasted", e.target.checked)}
                />
                Pasted roles
              </label>
            </div>
            {isMissing("sources") && (
              <p className="draft-field-help draft-field-help-warn">
                {MISSING_FIELD_PROMPTS.sources}
              </p>
            )}
          </div>
          <div className="draft-field draft-field-wide">
            <label className="check-row">
              <input
                type="checkbox"
                checked={cfgBool(config, "watch_new_only") ?? true}
                onChange={(e) => set("watch_new_only", e.target.checked)}
              />
              Only act on roles not seen before
            </label>
          </div>
        </>
      )}

      {type === "cold_mail" && (
        <>
          <DraftField
            id="draft-recipients"
            label="Recipients"
            missing={isMissing("recipients")}
            prompt={MISSING_FIELD_PROMPTS.recipients}
            help="Email addresses, comma-separated — the mission never invents addresses"
            wide
          >
            <input
              id="draft-recipients"
              value={cfgStrList(config, "recipients").join(", ")}
              onChange={(e) => setList("recipients", e.target.value)}
              placeholder="recruiter@example.com"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
            />
          </DraftField>
          <DraftField
            id="draft-role-ids"
            label="Role ids to mail about"
            missing={isMissing("role_ids") || isMissing("roles")}
            prompt={
              isMissing("role_ids")
                ? MISSING_FIELD_PROMPTS.role_ids
                : MISSING_FIELD_PROMPTS.roles
            }
            help="Ids from a job hunt's pasted roles, comma-separated"
            wide
          >
            <input
              id="draft-role-ids"
              value={cfgStrList(config, "role_ids").join(", ")}
              onChange={(e) => setList("role_ids", e.target.value)}
              placeholder="role_fit"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
            />
          </DraftField>
          <div className="draft-field">
            <label className="check-row">
              <input
                type="checkbox"
                checked={cfgBool(config, "attach_resume") ?? false}
                onChange={(e) => set("attach_resume", e.target.checked)}
              />
              Attach the stored resume to each mail
            </label>
          </div>
          <DraftField id="draft-max-drafts" label="New drafts per run (at most)">
            <input
              id="draft-max-drafts"
              type="number"
              min={1}
              max={20}
              value={cfgNum(config, "max_drafts_per_run") ?? ""}
              onChange={(e) => setNum("max_drafts_per_run", e.target.value)}
            />
          </DraftField>
        </>
      )}

      {type === "price_watch" && (
        <>
          <DraftField
            id="draft-url"
            label="Page to watch"
            missing={isMissing("url")}
            prompt={MISSING_FIELD_PROMPTS.url}
            wide
          >
            <input
              id="draft-url"
              type="url"
              value={cfgStr(config, "url") ?? ""}
              onChange={(e) => set("url", e.target.value || null)}
              placeholder="https://…"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
            />
          </DraftField>
          <DraftField
            id="draft-selector"
            label="Price element (CSS selector)"
            missing={isMissing("selector")}
            prompt={MISSING_FIELD_PROMPTS.selector}
            wide
          >
            <input
              id="draft-selector"
              value={cfgStr(config, "selector") ?? ""}
              onChange={(e) => set("selector", e.target.value || null)}
              placeholder=".price, #price, or text:₹"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
            />
          </DraftField>
          <DraftField id="draft-condition" label="Act when">
            <select
              id="draft-condition"
              value={conditionKind}
              onChange={(e) => {
                const v = e.target.value;
                if (v === "contains") set("condition", `contains:${containsText}`);
                else set("condition", v);
              }}
            >
              <option value="changed">The price changes</option>
              <option value="appeared">The element appears</option>
              <option value="disappeared">The element disappears</option>
              <option value="contains">The element contains text…</option>
            </select>
          </DraftField>
          {conditionKind === "contains" && (
            <DraftField id="draft-condition-text" label="Text to look for">
              <input
                id="draft-condition-text"
                value={containsText}
                onChange={(e) => set("condition", `contains:${e.target.value}`)}
                placeholder="In stock"
              />
            </DraftField>
          )}
          <DraftField
            id="draft-target-price"
            label="Target price"
            missing={isMissing("target_price")}
            prompt={MISSING_FIELD_PROMPTS.target_price}
          >
            <input
              id="draft-target-price"
              type="number"
              min={0}
              value={cfgNum(config, "target_price") ?? ""}
              onChange={(e) => setNum("target_price", e.target.value)}
            />
          </DraftField>
          <DraftField
            id="draft-currency"
            label="Currency"
            missing={isMissing("currency")}
            prompt={MISSING_FIELD_PROMPTS.currency}
          >
            <input
              id="draft-currency"
              value={cfgStr(config, "currency") ?? ""}
              onChange={(e) => set("currency", e.target.value || null)}
              maxLength={10}
              placeholder="INR"
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
            />
          </DraftField>
          <div className="draft-field draft-field-wide">
            <label className="check-row">
              <input
                type="checkbox"
                checked={cfgBool(config, "one_shot") ?? true}
                onChange={(e) => set("one_shot", e.target.checked)}
              />
              Stop once the target is hit
            </label>
          </div>
        </>
      )}

      <DraftField id="draft-interval" label="Check every (minutes)" help="15 minutes or more">
        <input
          id="draft-interval"
          type="number"
          min={15}
          max={1440}
          value={cfgNum(config, "interval_min") ?? ""}
          onChange={(e) => setNum("interval_min", e.target.value)}
        />
      </DraftField>
      <DraftField
        id="draft-max-facts"
        label="Fact budget"
        help="0 turns off fact storage for this mission"
      >
        <input
          id="draft-max-facts"
          type="number"
          min={0}
          value={cfgNum(config, "max_facts") ?? ""}
          onChange={(e) => setNum("max_facts", e.target.value)}
        />
      </DraftField>

      {(type === "job_hunt" || type === "cold_mail") && roles.length > 0 && (
        <div className="draft-field draft-field-wide">
          <p className="draft-roles-line">
            {roles.length} pasted role{roles.length === 1 ? "" : "s"} — kept as
            drafted.
          </p>
          <RawData label="Roles" data={roles} />
        </div>
      )}
    </div>
  );
}

/** A mission card's one-line state. A failed run still reads
 * as failed — a HumanError with the plain line up front and
 * the technical text behind Details. */
function MissionState({ m, nowMs }: { m: Mission; nowMs: number }) {
  if (!m.last_run_status) {
    return <p className="mission-state muted">Never run</p>;
  }
  switch (m.last_run_status) {
    case "ok":
      return (
        <p className="mission-state">
          Last run ok · <RelativeTime ts={m.last_run_at} />
        </p>
      );
    case "failed": {
      const when = m.last_run_at ? fmtRelative(m.last_run_at, nowMs) : null;
      return (
        <div className="mission-state">
          <HumanError
            title={`Last run failed${when && when !== "—" ? ` ${when}` : ""}`}
            detail={m.last_error ?? undefined}
          />
        </div>
      );
    }
    case "waiting_for_extension":
      return (
        <p className="mission-state warning">
          Waiting for Chrome — open Chrome to continue
        </p>
      );
    case "stalled":
      return (
        <div className="mission-state">
          <p className="warning">
            Stalled · <RelativeTime ts={m.last_run_at} />
          </p>
          {m.last_error ? (
            <Disclosure summary="Why it stalled">
              <p className="muted">{m.last_error}</p>
            </Disclosure>
          ) : null}
        </div>
      );
    default:
      return (
        <p className="mission-state">
          Last run: {m.last_run_status} · <RelativeTime ts={m.last_run_at} />
        </p>
      );
  }
}

/** The detail pane under a selected mission card: config,
 * pending approvals, linked tasks, activity timeline — each
 * behind a Disclosure. Approvals open by default when there
 * is something to decide; everything else stays collapsed
 * until asked for. */
function MissionDetailPane({
  detail,
  onSelectTask,
  onApprove,
  approving,
  nowMs,
}: {
  detail: MissionDetail;
  onSelectTask?: (taskId: string) => void;
  onApprove: (a: MissionApproval) => void;
  approving: string | null;
  nowMs: number;
}) {
  const { mission, activity, tasks, pending_approvals } = detail;
  const sorted = [...activity].sort((a, b) => b.created_at - a.created_at);
  const [shownActivity, setShownActivity] = useState(5);
  const [shownTasks, setShownTasks] = useState(5);
  const roles = Array.isArray(mission.config.roles) ? mission.config.roles : [];
  const now = Date.now();
  return (
    <div className="mission-detail">
      <Disclosure summary="Config">
        {Object.keys(mission.config).length === 0 ? (
          <p className="muted">No config stored.</p>
        ) : (
          <>
            <SummaryChips items={configChips(mission.type, mission.config)} />
            {roles.length > 0 ? <RawData label="Roles" data={roles} /> : null}
          </>
        )}
      </Disclosure>

      <Disclosure
        summary={`Pending approvals (${pending_approvals.length})`}
        defaultOpen={pending_approvals.length > 0}
      >
        {pending_approvals.length === 0 ? (
          <p className="muted">Nothing waiting on you right now.</p>
        ) : (
          <ul className="plain-list">
            {pending_approvals.map((a) => {
              const expired = a.expires_at * 1000 < now;
              const parts: string[] = [];
              if (a.step !== null) parts.push(`step ${a.step}`);
              if (a.role_id) parts.push(`role ${a.role_id}`);
              if (a.draft_id) parts.push(`draft ${a.draft_id}`);
              return (
                <li key={a.approval_id}>
                  <StatusChip tone={expired ? "plain" : "warn"}>
                    {expired ? "expired" : humanStatus(a.action)}
                  </StatusChip>
                  {parts.length > 0 ? <span> · {parts.join(" · ")}</span> : null}
                  <p className="mission-approval-meta muted">
                    requested{" "}
                    <RelativeTime ts={a.created_at} /> ·{" "}
                    {expired ? (
                      <>
                        expired <RelativeTime ts={a.expires_at} />
                      </>
                    ) : (
                      <>expires {fmtInFuture(a.expires_at, nowMs)}</>
                    )}
                  </p>
                  {expired ? (
                    <p className="muted">
                      Expired — the mission will ask again if it still needs
                      this.
                    </p>
                  ) : (
                    <div className="task-actions">
                      <button
                        type="button"
                        className="primary"
                        onClick={() => onApprove(a)}
                        disabled={approving !== null}
                      >
                        {approving === a.approval_id ? (
                          <Spinner label="Approving" />
                        ) : (
                          "Approve"
                        )}
                      </button>
                      {onSelectTask && (
                        <button type="button" onClick={() => onSelectTask(a.task_id)}>
                          Open task
                        </button>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Disclosure>

      <Disclosure summary={`Linked tasks (${tasks.length})`}>
        {tasks.length === 0 ? (
          <p className="muted">No tasks spawned yet — the first run creates one.</p>
        ) : (
          <>
            <ul className="plain-list">
              {tasks.slice(0, shownTasks).map((t) => (
                <li key={t.id}>
                  <StatusChip tone={statusTone(t.status)}>{humanStatus(t.status)}</StatusChip>{" "}
                  <span>{t.goal}</span>
                  <p className="muted">
                    <span className="mono">{t.id}</span> ·{" "}
                    <RelativeTime ts={t.created_at} />
                  </p>
                  {onSelectTask && (
                    <div className="task-actions">
                      <button type="button" onClick={() => onSelectTask(t.id)}>
                        Open in Agent view
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
            <ShowMore
              total={tasks.length}
              shown={Math.min(shownTasks, tasks.length)}
              onShowMore={() => setShownTasks((s) => s + 5)}
              label="tasks"
            />
          </>
        )}
      </Disclosure>

      <Disclosure summary={`Activity (${sorted.length})`}>
        {sorted.length === 0 ? (
          <p className="muted">Nothing recorded yet.</p>
        ) : (
          <>
            <div className="activity-list">
              {sorted.slice(0, shownActivity).map((a) => (
                <ActivityLine
                  key={a.id}
                  tone={activityTone(a.kind)}
                  title={activityTitle(a)}
                  detail={formatActivityDetail(a.detail)}
                  when={a.created_at}
                />
              ))}
            </div>
            <ShowMore
              total={sorted.length}
              shown={Math.min(shownActivity, sorted.length)}
              onShowMore={() => setShownActivity((s) => s + 5)}
              label="activities"
            />
          </>
        )}
      </Disclosure>
    </div>
  );
}

export function MissionsView({
  token,
  onSelectTask,
  onGoToAgent,
}: {
  token: string | null;
  onSelectTask?: (taskId: string) => void;
  onGoToAgent?: () => void;
}) {
  const [missions, setMissions] = useState<Mission[]>([]);
  // 24h activity counts per mission; null = the count call
  // failed, so the card says "unknown", never a fake zero.
  const [activityCounts, setActivityCounts] = useState<Record<string, number | null>>({});
  // The activity itself, per mission — the Jobs status
  // section below reads matched/applied counts off the same
  // fetch, so it costs no extra call. A mission missing here
  // means its activity fetch failed.
  const [missionActivity, setMissionActivity] = useState<
    Record<string, MissionActivity[]>
  >({});
  // Mail status (PR #7: the Mail view is gone; its status
  // lives here). Null = not loaded yet or the fetch failed —
  // mailFailed tells the two apart.
  const [mailDrafts, setMailDrafts] = useState<Draft[] | null>(null);
  const [mailLogEntries, setMailLogEntries] = useState<MailLogEntry[] | null>(null);
  const [mailFailed, setMailFailed] = useState(false);
  const [draftsShown, setDraftsShown] = useState(5);
  const [logShown, setLogShown] = useState(5);
  const [appliedShown, setAppliedShown] = useState(5);
  // Live pending approvals per mission, for the banner at the
  // top of the view. A failed fetch keeps the previous value
  // — a gap in polling must not hide a pending approval.
  const [pendingByMission, setPendingByMission] = useState<
    Record<string, MissionApproval[]>
  >({});
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastOk, setLastOk] = useState<number | null>(null);
  const nowMs = useRelativeNow();

  // Meta state (profile, resume, type registry): load once,
  // refreshed after resume changes.
  const [profile, setProfile] = useState<Profile | null>(null);
  const [profileMissing, setProfileMissing] = useState(false);
  const [resume, setResume] = useState<ResumeStatus | null>(null);
  // PR #6 Worker 11 (bug #8): a non-404 failure of
  // getProfile/getResume used to leave profile/resume null
  // forever, so the "Checking…" spinners hung when the
  // server was dead. metaError carries that failure to the
  // resume/profile spots, which show a HumanError + Retry
  // (retry calls loadMeta) instead of the spinner. The 404
  // paths keep their meaning (nothing stored = not an
  // error) and never set this.
  const [metaError, setMetaError] = useState<string | null>(null);
  const [typeSpecs, setTypeSpecs] = useState<MissionTypeSpec[]>([]);

  // Intake composer + draft.
  const [message, setMessage] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [intake, setIntake] = useState<IntakeResponse | null>(null);
  const [draftName, setDraftName] = useState("");
  const [draftType, setDraftType] = useState<MissionType>("job_hunt");
  // The draft config is an object, edited field by field —
  // never a JSON textarea. The raw draft sits behind RawData.
  const [draftConfig, setDraftConfig] = useState<Record<string, unknown>>({});
  const [draftBounds, setDraftBounds] = useState<Partial<MissionBounds>>({});
  const [creating, setCreating] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);

  // Resume attach.
  const [resumeFile, setResumeFile] = useState<File | null>(null);
  const [resumeText, setResumeText] = useState("");
  const [resumeBusy, setResumeBusy] = useState<"upload" | "paste" | null>(null);
  const [resumeMsg, setResumeMsg] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Selected mission detail.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<MissionDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // Lifecycle actions + approvals.
  const [pending, setPending] = useState<string | null>(null);
  const [stopId, setStopId] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [approving, setApproving] = useState<string | null>(null);

  const load = async () => {
    try {
      const data = await api.listMissions();
      setMissions(data.missions);
      setError(null);
      setLastOk(Date.now());
      // 24h activity counts, one call per mission; a failed
      // count shows as unknown, never as zero.
      const counts = await Promise.allSettled(
        data.missions.map((m) => api.missionActivity(m.id, 100)),
      );
      const next: Record<string, number | null> = {};
      const activities: Record<string, MissionActivity[]> = {};
      const dayAgo = Date.now() / 1000 - 24 * 3600;
      data.missions.forEach((m, i) => {
        const r = counts[i];
        next[m.id] =
          r.status === "fulfilled"
            ? r.value.activity.filter((a) => a.created_at >= dayAgo).length
            : null;
        if (r.status === "fulfilled") activities[m.id] = r.value.activity;
      });
      setActivityCounts(next);
      setMissionActivity(activities);
      // Mail status: drafts + send log, same calls the old
      // Mail view made. A failure keeps the previous lists
      // and flags the section, never a fake empty state.
      const [draftsRes, logRes] = await Promise.allSettled([
        api.listDrafts(),
        api.mailLog(),
      ]);
      if (draftsRes.status === "fulfilled") setMailDrafts(draftsRes.value.drafts);
      if (logRes.status === "fulfilled") setMailLogEntries(logRes.value.log);
      setMailFailed(
        draftsRes.status === "rejected" || logRes.status === "rejected",
      );
      // Pending approvals per mission, for the top banner.
      const approvalResults = await Promise.allSettled(
        data.missions.map((m) => api.missionApprovals(m.id)),
      );
      setPendingByMission((prev) => {
        const merged = { ...prev };
        data.missions.forEach((m, i) => {
          const r = approvalResults[i];
          if (r.status === "fulfilled") merged[m.id] = r.value.approvals;
        });
        return merged;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const loadMeta = async () => {
    setMetaError(null);
    try {
      const data = await api.getProfile();
      setProfile(data.profile);
      setProfileMissing(false);
    } catch (err) {
      if (isHttpError(err, 404)) {
        setProfile(null);
        setProfileMissing(true);
      } else {
        // Non-404: the profile check failed, not "no
        // profile". Report it next to the spinner it would
        // otherwise hang (bug #8), not as a view-level
        // banner — the missions list above may be fine.
        setMetaError(err instanceof Error ? err.message : String(err));
      }
    }
    try {
      const r = await api.getResume();
      setResume(r);
    } catch (err) {
      // A 404 means no resume is stored — not an error.
      // (It also covers the missions router not existing yet.)
      if (isHttpError(err, 404)) {
        setResume({
          stored: false,
          filename: null,
          uploaded_at: null,
          extraction: null,
          detail: null,
        });
      } else {
        setMetaError(err instanceof Error ? err.message : String(err));
      }
    }
    try {
      const t = await api.missionTypes();
      setTypeSpecs(t.types);
    } catch {
      // Types are a label nicety; the view falls back to the
      // three known types when the registry is unreachable.
    }
  };

  usePoll(load, [token], 15000);
  usePoll(loadMeta, [token], 0);

  // The detail pane polls while a mission is selected:
  // approvals expire in 10 minutes, so a load-once pane
  // would show dead Approve buttons.
  useEffect(() => {
    setDetail(null);
    setDetailError(null);
    if (!selectedId || !token) return;
    let alive = true;
    setDetailLoading(true);
    const loadDetail = async () => {
      try {
        const d = await api.getMission(selectedId);
        if (alive) {
          setDetail(d);
          setDetailError(null);
        }
      } catch (err) {
        if (alive) setDetailError(err instanceof Error ? err.message : String(err));
      } finally {
        if (alive) setDetailLoading(false);
      }
    };
    void loadDetail();
    const timer = setInterval(loadDetail, 15000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [selectedId, token]);

  const draftMission = async () => {
    const text = message.trim();
    if (!text) return;
    setDrafting(true);
    setDraftError(null);
    try {
      const res = await api.intakeMission(text);
      setIntake(res);
      setDraftName(res.draft.name || typeLabel(res.draft.type, typeSpecs));
      setDraftType(res.draft.type);
      setDraftConfig(res.draft.config ?? {});
      setDraftBounds(res.draft.bounds ?? {});
    } catch (err) {
      setDraftError(err instanceof Error ? err.message : String(err));
    } finally {
      setDrafting(false);
    }
  };

  const changeDraftType = (next: MissionType) => {
    setDraftType(next);
    // Config keys belong to their type — the server rejects
    // unknown keys, so a type switch drops keys the new type
    // does not know instead of sending a doomed create.
    setDraftConfig((prev) => filterConfigForType(prev, next));
  };

  const create = async () => {
    if (!draftName.trim()) {
      setCreateError("Give the mission a name.");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const m = await api.createMission({
        type: draftType,
        name: draftName.trim(),
        config: draftConfig,
        bounds: draftBounds,
      });
      setIntake(null);
      setMessage("");
      setSelectedId(m.id);
      await load();
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  const upload = async () => {
    if (!resumeFile) return;
    setResumeBusy("upload");
    setResumeMsg(null);
    try {
      const r = await api.uploadResume(resumeFile);
      setResume(r);
      setResumeFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      setResumeMsg(
        r.extraction === "ok"
          ? "Resume stored and parsed."
          : r.extraction === "limited"
            ? "Resume stored — only some fields could be read."
            : r.extraction === "failed"
              ? "Resume stored, but parsing found nothing usable."
              : "Resume stored.",
      );
      await loadMeta();
    } catch (err) {
      setResumeMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setResumeBusy(null);
    }
  };

  const pasteResume = async () => {
    if (resumeText.trim().length < 10) {
      setResumeMsg("The resume text needs at least 10 characters.");
      return;
    }
    setResumeBusy("paste");
    setResumeMsg(null);
    try {
      const r = await api.pasteResumeText(resumeText);
      setResume(r);
      setResumeText("");
      setResumeMsg(
        r.extraction === "ok"
          ? "Resume stored and parsed."
          : r.extraction === "limited"
            ? "Resume stored — only some fields could be read."
            : r.extraction === "failed"
              ? "Resume stored, but parsing found nothing usable."
              : "Resume stored.",
      );
      await loadMeta();
    } catch (err) {
      setResumeMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setResumeBusy(null);
    }
  };

  const runAction = async (
    id: string,
    kind: "activate" | "pause" | "resume" | "run-now" | "stop" | "delete",
  ) => {
    setPending(`${kind}:${id}`);
    setError(null);
    try {
      if (kind === "activate") await api.activateMission(id);
      if (kind === "pause") await api.pauseMission(id);
      if (kind === "resume") await api.resumeMission(id);
      if (kind === "run-now") await api.runMissionNow(id);
      if (kind === "stop") await api.stopMission(id);
      if (kind === "delete") {
        await api.deleteMission(id);
        if (selectedId === id) setSelectedId(null);
      }
      await load();
      if (selectedId === id && kind !== "delete") {
        const d = await api.getMission(id);
        setDetail(d);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const approve = async (missionId: string, a: MissionApproval) => {
    setApproving(a.approval_id);
    setDetailError(null);
    try {
      // Draft approvals go through the mission approve-mail
      // endpoint (it marks the draft approved and mints a
      // draft-scoped grant); task approvals go through the
      // existing task gate. Either way the approval id comes
      // from the server's pending record, never a client-made id.
      if (a.draft_id) {
        await api.approveMissionMail(missionId, a.approval_id, a.draft_id);
      } else {
        await api.approveTask(a.task_id, a.approval_id);
      }
      if (selectedId) {
        const d = await api.getMission(selectedId);
        setDetail(d);
      }
      await load();
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : String(err));
    } finally {
      setApproving(null);
    }
  };

  /** Open a mission's detail pane and bring it into view, so
   * a banner Review lands on the approvals, not the top of
   * the page. Respects reduced motion. */
  const reviewMission = (id: string) => {
    setSelectedId(id);
    setTimeout(() => {
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      document
        .getElementById(`mission-detail-${id}`)
        ?.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "nearest" });
    }, 50);
  };

  const typeOptions =
    typeSpecs.length > 0
      ? typeSpecs.map((s) => ({ type: s.type, label: s.label }))
      : KNOWN_TYPES;

  // Live (unexpired) pending approvals across all missions,
  // for the banner at the top of the view.
  const bannerNow = Date.now();
  const pendingEntries = missions.flatMap((m) =>
    (pendingByMission[m.id] ?? [])
      .filter((a) => a.expires_at * 1000 >= bannerNow)
      .map((a) => ({ mission: m, approval: a })),
  );
  const pendingMissions = [
    ...new Map(pendingEntries.map((e) => [e.mission.id, e.mission])).values(),
  ];

  // Missing fields with no inline editor (e.g. "roles" for a
  // job hunt, or a field a future planner reports) still get
  // a plain list above the editor — never silently dropped.
  const unrenderedMissing = intake
    ? intake.missing_fields.filter(
        (f) => f !== "allowed_sites" && !EDITABLE_MISSING[draftType].includes(f),
      )
    : [];

  // ---- Jobs status (PR #7: the Jobs view is gone) ----
  // Matched / applied / discovered, read off the job-hunt
  // missions' activity — the same fetch that feeds the 24h
  // counts above, so this costs no extra call. Activity is
  // capped at 100 entries per mission by that fetch, so the
  // counts are "of the recent activity", not all-time.
  const jobMissions = missions.filter((m) => m.type === "job_hunt");
  const jobActivity = jobMissions.flatMap((m) => missionActivity[m.id] ?? []);
  const jobActivityUnknown = jobMissions.some((m) => !(m.id in missionActivity));
  const matchedCount = jobActivity.filter((a) => a.kind === "matched").length;
  const discoveredCount = jobActivity.filter((a) => a.kind === "discovered").length;
  const appliedActivity = jobActivity
    .filter((a) => a.kind === "applied")
    .sort((a, b) => b.created_at - a.created_at);

  // ---- Mail status (PR #7: the Mail view is gone) ----
  // Drafts by status + the send log, newest first.
  const sortedDrafts = [...(mailDrafts ?? [])].sort((a, b) => b.created_at - a.created_at);
  const sortedLog = [...(mailLogEntries ?? [])].sort((a, b) => b.created_at - a.created_at);
  const draftStatusCount = (status: string) =>
    (mailDrafts ?? []).filter((d) => d.status === status).length;

  return (
    <div className="view missions-view">
      <div className="view-head">
        <h2>Missions</h2>
        <p className="muted">
          A mission watches and prepares on its own schedule — job hunt,
          cold mail, or price watch. Every apply and send still asks you
          first, unless you switch on standing approval. Missions run
          while this laptop and server are on. Job matches and mail
          drafts from your missions show below as status — new ones
          start in chat.
        </p>
      </div>
      {pendingEntries.length > 0 && (
        <ApprovalBanner count={pendingEntries.length}>
          {pendingEntries.length === 1 ? (
            <>
              <button
                type="button"
                className="primary"
                onClick={() =>
                  void approve(pendingEntries[0].mission.id, pendingEntries[0].approval)
                }
                disabled={approving !== null}
              >
                {approving === pendingEntries[0].approval.approval_id ? (
                  <Spinner label="Approving" />
                ) : (
                  "Approve"
                )}
              </button>
              <button type="button" onClick={() => reviewMission(pendingEntries[0].mission.id)}>
                Review
              </button>
            </>
          ) : (
            pendingMissions.map((m) => (
              <button key={m.id} type="button" onClick={() => reviewMission(m.id)}>
                Review {m.name}
              </button>
            ))
          )}
        </ApprovalBanner>
      )}
      {error && <ErrorBanner error={error} lastOk={lastOk} onRetry={() => void load()} />}

      <section aria-label="Describe a mission" className="pane">
        <SectionHeader title="Describe a mission" />
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void draftMission();
          }}
        >
          <label htmlFor="mission-message">Describe the mission in one message</label>
          <textarea
            id="mission-message"
            name="message"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => {
              // IME-safe Enter-to-submit (guide:
              // ime-safe-enter-submit): Enter drafts,
              // Shift+Enter is a newline, and an Enter that
              // only confirms an IME candidate never submits.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
                void draftMission();
              }
            }}
            placeholder="e.g. watch for SDE intern roles in Hyderabad, min ₹10,000 stipend, apply to at most 3 a day"
            rows={3}
            maxLength={2000}
            required
            aria-describedby="mission-message-help"
          />
          <p id="mission-message-help" className="muted">
            One message is enough — the server drafts the mission and you
            review it before anything is created. Enter drafts, Shift+Enter
            adds a line. {message.length}/2000 characters.
          </p>
          <button type="submit" className="primary" disabled={drafting || !message.trim()}>
            {drafting ? <Spinner label="Drafting" /> : "Draft mission"}
          </button>
          {draftError && (
            <HumanError
              title="Couldn't draft the mission"
              detail={apiErrorDetail(draftError)}
            />
          )}
        </form>

        {intake && (
          <div className="inline-form draft-card" aria-label="Draft mission">
            <SectionHeader title="Draft mission — review before creating" />
            <p role="status">
              {/* The intake source is always labelled, never hidden. */}
              <StatusChip tone={intake.source === "planner" ? "info" : "plain"}>
                {intake.source === "planner"
                  ? "Drafted by the planner"
                  : "Drafted by rules"}
              </StatusChip>{" "}
              <StatusChip tone={intake.confidence === "high" ? "ok" : "warn"}>
                confidence: {intake.confidence}
              </StatusChip>
            </p>
            {intake.warnings.length > 0 && (
              <ul className="draft-warnings">
                {intake.warnings.map((w, i) => (
                  <li key={i} className="warning">
                    {w}
                  </li>
                ))}
              </ul>
            )}
            <p className="draft-summary">
              {draftSummaryParts(draftType, draftConfig, draftBounds).join(" · ")}
            </p>
            {unrenderedMissing.length > 0 && (
              <div>
                <p className="secondary">
                  Still missing — say it in a new message:
                </p>
                <ul className="plain-list">
                  {unrenderedMissing.map((f) => (
                    <li key={f}>{MISSING_FIELD_PROMPTS[f] ?? f}</li>
                  ))}
                </ul>
              </div>
            )}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void create();
              }}
            >
              <label htmlFor="draft-name">Mission name</label>
              <input
                id="draft-name"
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                maxLength={120}
                required
              />
              <label htmlFor="draft-type">Type</label>
              <select
                id="draft-type"
                value={draftType}
                onChange={(e) => changeDraftType(e.target.value as MissionType)}
              >
                {typeOptions.map((t) => (
                  <option key={t.type} value={t.type}>
                    {t.label}
                  </option>
                ))}
              </select>
              {/* The draft-config id moved here from the removed
                JSON textarea: it still marks the draft config
                region, now as real fields. */}
              <fieldset id="draft-config" className="draft-config">
                <legend>What it watches</legend>
                <DraftConfigEditor
                  type={draftType}
                  config={draftConfig}
                  missing={intake.missing_fields}
                  onChange={setDraftConfig}
                />
              </fieldset>
              <BoundsEditor
                bounds={draftBounds}
                onChange={setDraftBounds}
                missing={intake.missing_fields}
              />
              <RawData
                label="Draft JSON"
                data={{
                  type: draftType,
                  name: draftName,
                  config: draftConfig,
                  bounds: draftBounds,
                }}
              />
              <div className="task-actions">
                <button type="submit" className="primary" disabled={creating}>
                  {creating ? <Spinner label="Creating" /> : "Create mission"}
                </button>
                <button type="button" onClick={() => setIntake(null)}>
                  Discard draft
                </button>
              </div>
              {createError && (
                <HumanError
                  title="Couldn't create the mission"
                  detail={apiErrorDetail(createError)}
                />
              )}
            </form>
          </div>
        )}

        <div className="inline-form resume-block">
          <SectionHeader title="Resume" />
          <div role="status" className="resume-summary secondary">
            {resume === null ? (
              metaError ? (
                <HumanError
                  title="Couldn't check for a stored resume"
                  detail={metaError}
                  onRetry={() => void loadMeta()}
                />
              ) : (
                <Spinner label="Checking for a stored resume" />
              )
            ) : resume.stored ? (
              <>
                <span>
                  Resume stored
                  {resume.filename ? (
                    <>
                      : <span className="mono">{resume.filename}</span>
                    </>
                  ) : (
                    ""
                  )}
                  {resume.uploaded_at ? (
                    <>
                      {" "}
                      · uploaded <RelativeTime ts={resume.uploaded_at} />
                    </>
                  ) : (
                    ""
                  )}
                </span>
                <StatusChip tone={extractionTone(resume.extraction)}>
                  {extractionLabel(resume.extraction)}
                </StatusChip>
                {resume.detail ? (
                  <span className="muted">({resume.detail})</span>
                ) : null}
              </>
            ) : (
              "No resume stored yet — cold-mail missions attach it to drafts."
            )}
          </div>
          <div className="resume-forms">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void upload();
              }}
            >
              <label htmlFor="resume-file">Attach a resume file</label>
              {/* The native file control reads as a browser
                  default, so the input is visually hidden and a
                  styled label is the picker (PR #6 critique).
                  The input stays focusable — never display: none
                  — and the focus ring shows on the label. */}
              <input
                id="resume-file"
                ref={fileInputRef}
                type="file"
                accept=".pdf,.txt,.md,.doc,.docx"
                onChange={(e) => setResumeFile(e.target.files?.[0] ?? null)}
                className="file-input-hidden"
              />
              <label htmlFor="resume-file" className="file-picker">
                <span className="file-picker-button">Choose file</span>
                <span className="file-picker-name">
                  {resumeFile ? resumeFile.name : "No file chosen"}
                </span>
              </label>
              <button
                type="submit"
                className="primary"
                disabled={!resumeFile || resumeBusy !== null}
              >
                {resumeBusy === "upload" ? <Spinner label="Uploading" /> : "Upload resume"}
              </button>
            </form>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void pasteResume();
              }}
            >
              <label htmlFor="resume-text">Or paste resume text</label>
              <textarea
                id="resume-text"
                value={resumeText}
                onChange={(e) => setResumeText(e.target.value)}
                rows={4}
                placeholder="Paste the resume text here…"
              />
              <button type="submit" disabled={!resumeText.trim() || resumeBusy !== null}>
                {resumeBusy === "paste" ? <Spinner label="Saving" /> : "Save pasted resume"}
              </button>
            </form>
          </div>
          {resumeMsg && (
            <p role="status" className="secondary">
              {resumeMsg}
            </p>
          )}
          <div className="muted profile-line">
            {profileMissing ? (
              <>
                No job profile stored — job hunt and cold mail missions match
                roles against it.{" "}
                {onGoToAgent ? (
                  <button type="button" onClick={onGoToAgent}>
                    Tell the Agent in chat
                  </button>
                ) : (
                  "Tell the Agent in chat."
                )}
              </>
            ) : profile ? (
              <>
                Job profile stored{profile.name ? ` for ${profile.name}` : ""}
                {profile.skills.length > 0
                  ? ` · ${profile.skills.length} skill${profile.skills.length === 1 ? "" : "s"}`
                  : ""}
                .
              </>
            ) : metaError ? (
              <HumanError
                title="Couldn't check for a job profile"
                detail={metaError}
                onRetry={() => void loadMeta()}
              />
            ) : (
              "Checking for a job profile…"
            )}
          </div>
        </div>
      </section>

      <section aria-label="Missions" className="pane">
        <SectionHeader title={`Missions (${missions.length})`} />
        {loading && <Spinner label="Loading missions" />}
        {!loading && missions.length === 0 && (
          <EmptyState>No missions yet — describe one above to start.</EmptyState>
        )}
        <ul className="watch-list">
          {missions.map((m) => {
            const count = activityCounts[m.id];
            const countKnown = m.id in activityCounts;
            return (
              <li
                key={m.id}
                className={
                  m.last_run_status === "failed"
                    ? "mission-card mission-card-failed"
                    : "mission-card"
                }
              >
                <div className="watch-head">
                  <strong>{m.name}</strong>
                  <span>
                    <StatusChip tone="plain">
                      {typeLabel(m.type, typeSpecs)}
                    </StatusChip>{" "}
                    <StatusChip tone={statusTone(m.status)}>{m.status}</StatusChip>
                  </span>
                </div>
                <MissionState m={m} nowMs={nowMs} />
                <SummaryChips items={boundsChips(m)} />
                <p className="mission-meta muted num">
                  {!countKnown
                    ? "Activity in the last 24 hours: loading…"
                    : count === null
                      ? "Activity in the last 24 hours: unknown"
                      : `${count} activit${count === 1 ? "y" : "ies"} in the last 24 hours`}
                  {m.next_run_at
                    ? ` · Next run ${fmtInFuture(m.next_run_at, nowMs)}`
                    : m.status === "active"
                      ? " · Next run not scheduled"
                      : ""}
                </p>
                {m.bounds?.auto_approve && (
                  <p className="warning">
                    Standing approval: applies/sends within bounds without
                    asking.
                  </p>
                )}
                <div className="task-actions">
                  <button
                    type="button"
                    onClick={() => setSelectedId(selectedId === m.id ? null : m.id)}
                    aria-expanded={selectedId === m.id}
                    aria-controls={`mission-detail-${m.id}`}
                    disabled={pending !== null}
                  >
                    {selectedId === m.id ? "Hide details" : "Details"}
                  </button>
                  {m.status === "draft" && (
                    <button
                      type="button"
                      onClick={() => void runAction(m.id, "activate")}
                      disabled={pending !== null}
                    >
                      {pending === `activate:${m.id}` ? "Activating…" : "Activate"}
                    </button>
                  )}
                  {m.status === "active" && (
                    <>
                      <button
                        type="button"
                        onClick={() => void runAction(m.id, "pause")}
                        disabled={pending !== null}
                      >
                        {pending === `pause:${m.id}` ? "Pausing…" : "Pause"}
                      </button>
                      <button
                        type="button"
                        title="Schedules an immediate tick — the runner picks it up within a minute."
                        onClick={() => void runAction(m.id, "run-now")}
                        disabled={pending !== null}
                      >
                        {pending === `run-now:${m.id}` ? "Scheduling…" : "Run now"}
                      </button>
                    </>
                  )}
                  {m.status === "paused" && (
                    <>
                      <button
                        type="button"
                        onClick={() => void runAction(m.id, "resume")}
                        disabled={pending !== null}
                      >
                        {pending === `resume:${m.id}` ? "Resuming…" : "Resume"}
                      </button>
                      <button
                        type="button"
                        title="Schedules an immediate tick — the runner picks it up within a minute."
                        onClick={() => void runAction(m.id, "run-now")}
                        disabled={pending !== null}
                      >
                        {pending === `run-now:${m.id}` ? "Scheduling…" : "Run now"}
                      </button>
                    </>
                  )}
                  {(m.status === "active" ||
                    m.status === "paused" ||
                    m.status === "draft") && (
                    <button
                      type="button"
                      onClick={() => setStopId(m.id)}
                      disabled={pending !== null}
                    >
                      Stop
                    </button>
                  )}
                  {(m.status === "draft" || m.status === "stopped") && (
                    <button
                      type="button"
                      onClick={() => setDeleteId(m.id)}
                      disabled={pending !== null}
                    >
                      Delete
                    </button>
                  )}
                </div>
                {selectedId === m.id && (
                  <div id={`mission-detail-${m.id}`} className="inline-form">
                    {detailLoading && !detail ? (
                      <Spinner label="Loading detail" />
                    ) : detailError ? (
                      <HumanError
                        title="Couldn't load this mission's details"
                        detail={detailError}
                      />
                    ) : detail ? (
                      <MissionDetailPane
                        detail={detail}
                        onSelectTask={onSelectTask}
                        onApprove={(a) => void approve(detail.mission.id, a)}
                        approving={approving}
                        nowMs={nowMs}
                      />
                    ) : null}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </section>

      {/* PR #7: the Jobs view is gone — its status lives here.
        Job hunts are set up in chat now; this section only
        reports what the missions found and applied to. */}
      <section aria-label="Jobs status" className="pane">
        <SectionHeader title="Jobs status" />
        <p className="muted">
          What the job-hunt missions found and applied to, from their
          recent activity. New job hunts start in chat — tell the Agent
          the role, location, and stipend. Receipts for each apply also
          show under the task in the Agent view.
        </p>
        {loading ? (
          <Spinner label="Loading jobs status" />
        ) : jobMissions.length === 0 ? (
          <EmptyState>
            No job-hunt missions yet — describe one in chat to start.
          </EmptyState>
        ) : (
          <>
            <SummaryChips
              items={[
                { label: "Roles found", value: String(discoveredCount) },
                { label: "Matched", value: String(matchedCount) },
                { label: "Applied", value: String(appliedActivity.length) },
              ]}
            />
            {jobActivityUnknown && (
              <p className="muted">
                Activity for one mission couldn&apos;t be fetched, so these
                counts may be low.
              </p>
            )}
            <h3>Applied</h3>
            {appliedActivity.length === 0 ? (
              <EmptyState>
                Nothing applied to yet — matches wait for your approval
                unless a mission has standing approval.
              </EmptyState>
            ) : (
              <>
                <div className="activity-list">
                  {appliedActivity.slice(0, appliedShown).map((a) => (
                    <ActivityLine
                      key={a.id}
                      tone="ok"
                      title={activityTitle(a)}
                      detail={formatActivityDetail(a.detail)}
                      when={a.created_at}
                    />
                  ))}
                </div>
                <ShowMore
                  total={appliedActivity.length}
                  shown={Math.min(appliedShown, appliedActivity.length)}
                  onShowMore={() => setAppliedShown((s) => s + 5)}
                  label="applies"
                />
              </>
            )}
          </>
        )}
      </section>

      {/* PR #7: the Mail view is gone — its status lives here.
        Cold-mail missions are set up in chat now; this
        section only reports their drafts and sends. Drafts
        waiting on approval appear in the mission approvals
        above. */}
      <section aria-label="Mail status" className="pane">
        <SectionHeader title="Mail status" />
        <p className="muted">
          Drafts the cold-mail missions wrote, and what was actually
          sent. New cold-mail missions start in chat.
        </p>
        {mailFailed && (
          <p className="muted">
            Couldn&apos;t load the latest mail status — showing the last
            data fetched.
          </p>
        )}
        {mailDrafts === null && mailLogEntries === null && !mailFailed ? (
          <Spinner label="Loading mail status" />
        ) : (
          <>
            <SummaryChips
              items={[
                { label: "Waiting", value: String(draftStatusCount("draft")) },
                { label: "Approved", value: String(draftStatusCount("approved")) },
                { label: "Sent", value: String(draftStatusCount("sent")) },
                { label: "Bounced", value: String(draftStatusCount("bounced")) },
              ]}
            />
            <h3>Drafts</h3>
            {sortedDrafts.length === 0 ? (
              <EmptyState>
                No drafts yet — a cold-mail mission writes one per recruiter.
              </EmptyState>
            ) : (
              <>
                <ul className="plain-list">
                  {sortedDrafts.slice(0, draftsShown).map((d) => (
                    <li key={d.id} className="status-draft-row">
                      <span className="status-draft-main">
                        <span className="status-draft-recipient">{d.recipient}</span>
                        <span className="muted status-draft-subject">{d.subject}</span>
                        <span className="muted status-draft-meta">
                          <RelativeTime ts={d.created_at} />
                        </span>
                      </span>
                      <StatusChip tone={statusTone(d.status)}>
                        {humanStatus(d.status)}
                      </StatusChip>
                    </li>
                  ))}
                </ul>
                <ShowMore
                  total={sortedDrafts.length}
                  shown={Math.min(draftsShown, sortedDrafts.length)}
                  onShowMore={() => setDraftsShown((s) => s + 5)}
                  label="drafts"
                />
              </>
            )}
            <h3>Send log</h3>
            {sortedLog.length === 0 ? (
              <EmptyState>
                No mails sent yet. Every send is logged here, per recruiter.
              </EmptyState>
            ) : (
              <>
                <ul className="mail-log-list">
                  {sortedLog.slice(0, logShown).map((entry) => (
                    <li key={entry.id}>
                      <ActivityLine
                        tone={statusTone(entry.status)}
                        title={mailLogTitle(entry)}
                        detail={
                          <p className="muted">
                            Role {entry.role_id}
                            {entry.detail ? ` · ${entry.detail}` : ""}
                          </p>
                        }
                        when={entry.created_at}
                      />
                    </li>
                  ))}
                </ul>
                <ShowMore
                  total={sortedLog.length}
                  shown={Math.min(logShown, sortedLog.length)}
                  onShowMore={() => setLogShown((s) => s + 5)}
                  label="entries"
                />
              </>
            )}
          </>
        )}
      </section>

      <ConfirmDialog
        open={stopId !== null}
        title="Stop this mission?"
        confirmLabel="Stop mission"
        danger
        onConfirm={() => {
          const id = stopId;
          setStopId(null);
          if (id) void runAction(id, "stop");
        }}
        onCancel={() => setStopId(null)}
      >
        <p>
          Stopping is terminal — the mission cannot be restarted. Its live
          approval grants are revoked, pending approvals are consumed, and
          its running tasks are cancelled.
        </p>
      </ConfirmDialog>
      <ConfirmDialog
        open={deleteId !== null}
        title="Delete this mission?"
        confirmLabel="Delete mission"
        danger
        onConfirm={() => {
          const id = deleteId;
          setDeleteId(null);
          if (id) void runAction(id, "delete");
        }}
        onCancel={() => setDeleteId(null)}
      >
        <p>
          The mission and its activity log are deleted. Receipts, drafts and
          tasks stay — they are the audit trail.
        </p>
      </ConfirmDialog>
    </div>
  );
}
