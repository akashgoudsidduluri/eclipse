/** API client for the Eclipse server (localhost:8000).
 *
 * The token lives in sessionStorage only — it is cleared when
 * the tab closes, never written to a file or a log. Every call
 * sends it as X-Eclipse-Token. The server never stores raw
 * observations, so there is no "raw PII" endpoint to call.
 *
 * Sprint 9 review fix: the first version of this file typed the
 * server responses from memory, and the types were wrong in
 * five places (quota wrapper + keys, flat task detail, proof
 * metric keys, latency shape, /health shape). The server is the
 * contract; the mappings below adapt its real shapes to the
 * shapes the views render. They are the load-bearing lines of
 * this file — if a server key changes, this file must change
 * with it, not the other way round.
 *
 * Workspace redesign: every endpoint in server/api/ is covered
 * here, typed from the route handlers. Two endpoints are typed
 * but deliberately NOT wired to any UI button — execute and
 * verify need a live browser observation ({url, elements}) that
 * only the extension produces; the workspace has no observation
 * source, so a button would send a fabricated observation.
 * Execution stays in the extension.
 */
export type TaskSummary = {
  id: string;
  goal: string;
  status: string;
  created_at: number;
  updated_at: number;
  receipt: string | null;
};

export type Step = {
  n: number;
  description: string;
  status: string;
  action_json: string | null;
  element_id: string | null;
  result: string | null;
  error: string | null;
  retries: number;
};

export type Checkpoint = {
  step_n: number;
  summary: string;
  created_at: number;
};

export type Receipt = {
  id: string;
  task_id: string;
  role_id: string;
  title: string;
  company: string;
  url: string;
  status: string;
  confirmation: string;
  created_at: number;
};

export type TaskDetail = {
  task: TaskSummary & { constraints: string };
  steps: Step[];
  checkpoints: Checkpoint[];
  receipts: Receipt[];
};

/** ---- chat (PR #6) ---- */

/** One chat message, as GET /chat/history returns it (oldest
 * first). `source` is only present when the server labels
 * the reply — history rows may omit it, so it is optional
 * and the UI shows no source note rather than guessing one.
 * created_at is unix seconds. */
/** ---- chat uploads (PR #7) ---- */

/** One proposed profile fact from an uploaded resume —
 * shown in chat for the user to confirm before anything is
 * stored as profile facts. */
export type ProposedFact = {
  field: string;
  label: string;
  value: string | number | string[];
};

/** A file attached in chat, as POST /chat/uploads and
 * GET /chat/uploads return it. `extracted_text` is the
 * locally extracted text (never sent to a model);
 * `proposed_facts` is empty for non-resume files. */
export type ChatUpload = {
  id: string;
  filename: string;
  stored_name: string;
  stored_path: string;
  size_bytes: number;
  mime: string;
  extraction_status: "ok" | "limited" | "unsupported";
  extracted_text: string | null;
  proposed_facts: ProposedFact[];
  chat_message_id: string | null;
  mission_id: string | null;
  uploaded_at: number;
  note: string;
};

export type ChatMessage = {
  id: string;
  role: "user" | "agent";
  text: string;
  kind: "chat" | "task_created";
  task_id: string | null;
  created_at: number;
  source?: "planner" | "rules";
  /** PR #7: which provider answered ("ollama" | "antseed" |
   * "groq" | …), or null when rules answered. History rows
   * carry it from the chat_messages.provider column. */
  provider?: string | null;
  /** Files attached to this message (in-session sends, and
   * history rows matched by chat_message_id). */
  attachments?: ChatUpload[];
};

/** POST /chat/message: the agent's reply. kind "chat" is a
 * plain reply — a greeting never creates a task; kind
 * "task_created" anchors a task card under the reply via
 * task_id. source names the path that answered; provider
 * names the model that answered (null when rules did).
 * draft is present only when the message was a standing-
 * mission ask — the intake result, for the UI's draft card
 * (same shape as POST /missions/intake returns). */
export type ChatReply = {
  message_id: string;
  reply_text: string;
  kind: "chat" | "task_created";
  task_id: string | null;
  source: "planner" | "rules";
  provider: string | null;
  draft?: IntakeResponse | null;
};

/** A pending human-gate approval, as the server records it
 * (server/core/db.py pending_approvals). The server issues the
 * id when the step reaches the gate — the client must approve
 * one of these, never a made-up id (Bug 3 fix). */
export type PendingApproval = {
  approval_id: string;
  task_id: string;
  step: number | null;
  action: string;
  element_id?: string;
  role_id?: string;
  created_at: number;
  expires_at: number;
};

/** One planned step, as the planner returns it
 * (planner/validate.py parse_plan). */
export type PlanStep = {
  step: number;
  description: string;
  expected_end_state: string;
};

/** A browser observation, as the extension sends it. The
 * workspace never constructs one — see the header note. */
export type Observation = {
  url: string;
  elements: Record<string, unknown>[];
};

export type ExecuteResult = {
  action: Record<string, unknown>;
  expected_end_state: string;
  model: string;
  task_id: string;
  element: Record<string, unknown> | null;
  allowed_sites: string[];
  fill: Record<string, string>;
  fill_denied_reason: string | null;
};

export type VerifyResult = {
  verified: boolean;
  reason: string;
  retry: boolean;
  backoff_s?: number;
};

export type QuotaModel = {
  name: string;
  used_today: number;
  limit: number;
  reset_at: number;
};

export type SystemMetrics = {
  tasks: number;
  audit_entries: number;
  quota: {
    model: string;
    used_today: number;
    limit_day: number;
    reset_at: number;
  }[];
  tokens: { in: number; out: number };
  note: string;
};

export type ProofMetrics = {
  metrics: {
    grounding: number | null;
    pii_recall: number | null;
    pii_precision: number | null;
    redaction_tokens: number | null;
    resource_mb: number | null;
    latency_p50_s: number | null;
  } | null;
  targets: Record<string, number>;
  honesty: string;
  run_date: string | null;
  fixtures: number | null;
  worst_page: string | null;
  worst_pii_fixture: string | null;
  reason: string | null;
};

export type SentEntry = {
  id: string;
  at: number;
  endpoint: string;
  model: string;
  tokens_est: number;
  ms: number;
  ok: boolean;
  prompt_redacted: string;
  pii_types: string[];
};

export type EgressHit = {
  entry_id: string;
  endpoint: string;
  type: string;
};

export type EgressResult = {
  passed: boolean;
  hit_count: number;
  hits: EgressHit[];
  checked: number;
  patterns: string[];
  note: string;
};

export type RedactionResult = {
  generated_at: string | null;
  recoverable_tokens: number | null;
  passed: boolean | null;
  pii_recall: number | null;
  pii_precision: number | null;
  per_fixture: Record<string, string[]> | null;
  label_based_not_regex: Record<string, string[]>;
  note: string | null;
  reason: string | null;
};

export type LatencyFixture = {
  steps: number;
  verified: number;
  elapsed_s: number;
  errors: Record<string, string>;
};

export type LatencyResult = {
  generated_at: string | null;
  p50_s: number | null;
  mean_s: number | null;
  max_s: number | null;
  target: number | null;
  p50_within_target: boolean | null;
  worst_fixture: string | null;
  worst_fixture_s: number | null;
  per_fixture: Record<string, LatencyFixture> | null;
  planner: string | null;
  note: string | null;
  reason: string | null;
};

export type AuditEntry = {
  id: number;
  task_id: string | null;
  action: string;
  payload_hash: string;
  prev_hash: string;
  hash: string;
  /** Unix MILLISECONDS — the only ms timestamp the server
   * returns (server/core/audit.py). Render with
   * fmtAuditDateTime, never fmtDateTime. */
  created_at: number;
};

export type AuditVerifyResult = {
  ok: boolean;
  checked: number;
  first_bad_id: number | null;
  reason: string | null;
};

export type Watch = {
  id: string;
  url: string;
  selector: string;
  condition: string;
  interval_min: number;
  last_value: string | null;
  status: string;
  last_checked: number | null;
  last_fired_at: number | null;
  misses: number;
  muted_until: number | null;
  created_at: number;
};

export type WatchEvent = {
  id: string;
  watch_id: string;
  kind: string;
  old_value: string | null;
  new_value: string | null;
  detail: string;
  created_at: number;
};

export type WatchCheckResult = {
  watch_id: string;
  fired: boolean;
  broken: boolean;
  became_broken: boolean;
  status: string;
  reason: string;
};

export type BriefingCard = {
  kind: string;
  title: string;
  body: string;
  action: string | null;
  action_label: string;
};

export type Briefing = {
  id: string;
  created_at: number;
  cards: BriefingCard[];
};

/** The parsed resume profile (jobs/profile.py parse_resume).
 * Fields the parser did not find stay null / empty. */
export type Profile = {
  name: string | null;
  email: string | null;
  phone: string | null;
  roll: string | null;
  degree: string | null;
  year: number | null;
  cgpa: number | null;
  skills: string[];
  locations: string[];
  stipend_min: number | null;
};

/** One role, as POST /jobs/match and POST /mail/drafts accept it. */
export type RoleInput = {
  id: string;
  title: string;
  company?: string;
  site?: string;
  url?: string;
  stipend?: number | null;
  location?: string;
  deadline?: string | null;
  skills?: string[];
  min_year?: number | null;
  degree?: string | null;
  min_cgpa?: number | null;
  requires_experience?: boolean;
};

export type MatchResult = {
  role_id: string;
  fits: boolean;
  score: number;
  reasons: string[];
  missing: string[];
  warnings: string[];
  deadline: string | null;
  stipend: number | null;
  location: string;
};

export type MatchResponse = {
  shortlist: MatchResult[];
  excluded: MatchResult[];
  count: number;
};

export type ReceiptReport = {
  task_id: string;
  submitted: Receipt[];
  failed: Receipt[];
  count: number;
};

export type Draft = {
  id: string;
  task_id: string;
  recipient: string;
  role_id: string;
  subject: string;
  body: string;
  status: string;
  created_at: number;
  updated_at: number;
};

export type MailLogEntry = {
  id: string;
  draft_id: string;
  recipient: string;
  role_id: string;
  status: string;
  detail: string;
  created_at: number;
};

export type BounceResult = {
  recipient: string;
  bounced: boolean;
  detail: string;
};

export type DraftsCreated = {
  created: { draft_id: string; email: string; role_id: string }[];
  skipped: { email: string; role_id: string | null; reason: string | string[] }[];
};

export type VaultGrant = {
  grant_id: string;
  task_id: string;
  expires_at: number;
  expires_in_s: number;
  card_last4: string | null;
  has_card: boolean;
};

export type VaultStatus = {
  vault_file: string;
  unlocked: boolean;
  has_grants: boolean;
  grants: VaultGrant[];
};

export type VaultUnlockResult = {
  task_id: string;
  grant_id: string;
  expires_at: number;
  expires_in_s: number;
  card_last4: string | null;
};

export type VaultFillResult = {
  ok: boolean;
  reason?: string;
  has_card?: boolean;
  has_password?: boolean;
  card_last4?: string | null;
};

export type ConnectorRow = {
  name: string;
  enabled: boolean;
  state: string | null;
  last_checked: number | null;
  last_error: string | null;
};

export type ConnectorLiveStatus = {
  name: string;
  state: string;
  error?: string;
  connect_url?: string;
};

export type GmailOffer = {
  id: string;
  from: string;
  subject: string;
  date: string;
  body_redacted: string;
};

export type BusySlot = {
  start: string;
  end: string;
};

export type FreebusyResult = {
  free: boolean;
  busy: BusySlot[];
};

/** A Drive file as the CLI returns it. Only `id` is
 * guaranteed; the rest is rendered only when present. */
export type DriveFile = {
  id: string;
  name?: string;
  modifiedTime?: string;
  webViewLink?: string;
  mimeType?: string;
};

/** A created calendar event. The live Google shape nests
 * start/end ({dateTime}); the dedup return is flat strings —
 * so start/end are typed unknown and rendered via a safe
 * display helper in the view. */
export type CalendarEventResult = {
  id?: string;
  summary?: string;
  start?: unknown;
  end?: unknown;
  htmlLink?: string | null;
  duplicate?: boolean;
};

export type Fact = {
  id: string;
  text: string;
  source: string;
  topic: string | null;
  score: number;
};

export type RagChunk = {
  id: string;
  text: string;
  kind: string;
  meta: Record<string, unknown>;
  score: number;
};

export type RagStatus = {
  chunks: number;
  model: string;
  dimensions: number;
  threshold: number;
  model_available: boolean;
};

/** ---- missions (PR #5) ---- */

export type MissionType = "job_hunt" | "cold_mail" | "price_watch" | "email_monitor";

export type MissionStatus = "draft" | "active" | "paused" | "stopped" | "done";

export type MissionBounds = {
  max_actions_per_day: number;
  max_applies_per_day: number;
  max_sends_per_day: number;
  max_spend_inr?: number;
  allowed_sites: string[];
  require_approval_for: string[];
  auto_approve: boolean;
  quiet_hours_ist?: { start: string; end: string } | null;
  min_score?: number;
};

export type Mission = {
  id: string;
  type: MissionType;
  name: string;
  status: MissionStatus;
  config: Record<string, unknown>;
  bounds: MissionBounds;
  created_at: number;
  updated_at: number;
  last_run_at: number | null;
  next_run_at: number | null;
  last_run_status: string | null;
  last_error: string | null;
};

/** A mission draft from POST /missions/intake — the POST
 * /missions body minus status. Four types since PR #7:
 * job_hunt, cold_mail, price_watch, and email_monitor (the
 * inbox-watch mission). The server validates the type. */
export type MissionDraft = {
  type: MissionType;
  name: string;
  config: Record<string, unknown>;
  bounds: Partial<MissionBounds>;
};

export type IntakeResponse = {
  draft: MissionDraft;
  source: "planner" | "rules";
  confidence: "high" | "low";
  missing_fields: string[];
  warnings: string[];
  /** PR #7: at most one question, only when the intake is
   * genuinely blocked on something answerable in chat. */
  clarifying_question?: string | null;
  /** PR #7: the draft as plain language for the chat thread. */
  draft_card?: string;
  /** PR #7: proposed profile facts from an attached resume. */
  proposed_facts?: ProposedFact[];
  /** PR #7: the uploads the intake used. */
  upload_ids?: string[];
  /** PR #7: set when the intake stored the exchange in the
   * chat thread (store_in_chat). */
  chat_message_id?: string;
  chat_reply_id?: string;
};

export type MissionActivity = {
  id: string;
  mission_id: string;
  run_id: string | null;
  kind: string;
  /** Redacted JSON, as a string — parse it before rendering. */
  detail: string;
  created_at: number;
};

export type MissionTask = {
  id: string;
  goal: string;
  status: string;
  created_at: number;
};

/** A live pending approval on one of a mission's spawned
 * tasks. Same shape as PendingApproval plus the draft id
 * for mail-send approvals. */
export type MissionApproval = {
  approval_id: string;
  task_id: string;
  step: number | null;
  action: string;
  element_id?: string;
  role_id?: string;
  draft_id?: string;
  created_at: number;
  expires_at: number;
};

export type MissionDetail = {
  mission: Mission;
  activity: MissionActivity[];
  tasks: MissionTask[];
  pending_approvals: MissionApproval[];
};

/** One mission type from GET /missions/types — the registry
 * entry the server validates config against. The config
 * schema shape is server-defined; the view renders config
 * as editable JSON rather than guessing its fields. */
export type MissionTypeSpec = {
  type: MissionType;
  label: string;
  config_schema: Record<string, unknown>;
  default_bounds: MissionBounds;
  default_interval_min: number;
};

/** The stored resume, as GET /missions/resume reports it.
 * `extraction` is "ok" when the resume parsed cleanly,
 * "limited" when only some fields came out, "failed" when
 * parsing found nothing usable. */
export type ResumeStatus = {
  stored: boolean;
  filename: string | null;
  uploaded_at: number | null;
  extraction: "ok" | "limited" | "failed" | null;
  detail: string | null;
};

/** The server's resume shape (missions_runner_api
 * _resume_response): no `stored` flag, `stored_at` not
 * `uploaded_at`, `extraction_status` not `extraction`,
 * `note` not `detail`. The client maps it — the server is
 * the contract. Integration-verified against the live server
 * (an earlier build cast the raw shape to ResumeStatus, so
 * `stored` was undefined and the UI always said no resume
 * was stored). */
type ServerResume = {
  filename: string | null;
  stored_name?: string;
  stored_path?: string;
  size_bytes?: number;
  extraction_status?: string;
  extracted_text?: string;
  stored_at?: number;
  note?: string;
};

function toResumeStatus(r: ServerResume): ResumeStatus {
  return {
    stored: true,
    filename: r.filename ?? null,
    uploaded_at: r.stored_at ?? null,
    extraction:
      r.extraction_status === "ok"
        ? "ok"
        : r.extraction_status === "limited"
          ? "limited"
          : "failed",
    detail: r.note ?? null,
  };
}

/** The server's real shapes (what the endpoints return). */
type ServerQuotaRow = {
  model: string;
  used_today: number;
  limit_day: number;
  reset_at: number;
};

type ServerTask = TaskSummary & {
  constraints: string;
  steps: Step[];
  checkpoints: Checkpoint[];
  receipts: Receipt[];
};

type ServerProofMetrics = {
  generated_at: string | null;
  fixtures: number | null;
  metrics: {
    visual_grounding_first_try: number | null;
    pii_recall: number | null;
    pii_precision: number | null;
    redaction_recoverable_tokens: number | null;
    resource_mb: number | null;
    latency_p50_s: number | null;
  } | null;
  targets: Record<string, number>;
  honesty: string;
  worst_page: string | null;
  worst_pii_fixture: string | null;
  reason?: string;
};

type ServerLatency = {
  generated_at: string | null;
  step_time_s: { p50: number | null; mean: number | null; max: number | null } | null;
  latency_target_s: number | null;
  p50_within_target: boolean | null;
  worst_fixture: string | null;
  worst_fixture_s: number | null;
  per_fixture: Record<string, LatencyFixture> | null;
  planner: string | null;
  note?: string;
  reason?: string;
};

type ServerRedaction = {
  generated_at: string | null;
  recoverable_tokens: number | null;
  passed: boolean | null;
  pii_recall: number | null;
  pii_precision: number | null;
  per_fixture: Record<string, string[]> | null;
  label_based_not_regex: Record<string, string[]>;
  note?: string;
  reason?: string;
};

const TOKEN_KEY = "eclipseToken";

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY);
}

/** Was this the error the api client throws for a given HTTP
 * status? Used for 404-as-empty-state (no profile yet, no
 * briefing yet, no resume in Drive). */
export function isHttpError(err: unknown, status: number): boolean {
  return err instanceof Error && err.message.startsWith(`${status} on `);
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  if (!token) throw new Error("no token — paste the server token first");
  const res = await fetch(path, {
    ...init,
    headers: {
      "X-Eclipse-Token": token,
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (res.status === 401) {
    // Sprint 9 fix (review M18): a bad token returns the user
    // to the token gate instead of stranding the panes behind
    // 401 banners. App.tsx listens for this event.
    window.dispatchEvent(new Event("eclipse-unauthorized"));
    throw new Error("unauthorized — check the token");
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} on ${path}: ${text.slice(0, 200)}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  // ---- system ----
  health: () =>
    call<{ status: string; service: string; version: string }>("/health"),
  systemMetrics: () => call<SystemMetrics>("/metrics"),
  /** B1: the server returns {quota: [{model, used_today,
   * limit_day, reset_at}]} — the wrapper key and the item keys
   * both differ from what the first version typed. */
  quota: async (): Promise<{ models: QuotaModel[] }> => {
    const s = await call<{ quota: ServerQuotaRow[] }>("/quota");
    return {
      models: s.quota.map((r) => ({
        name: r.model,
        used_today: r.used_today,
        limit: r.limit_day,
        reset_at: r.reset_at,
      })),
    };
  },

  // ---- tasks ----
  listTasks: (status?: string) =>
    call<{ tasks: TaskSummary[] }>(
      status ? `/tasks?status=${encodeURIComponent(status)}` : "/tasks",
    ),
  /** B2: the server returns a FLAT task ({...task, steps,
   * checkpoints, receipts}) — there is no `task` key. Adapt it
   * to the nested shape the view renders. */
  getTask: async (id: string): Promise<TaskDetail> => {
    const s = await call<ServerTask>(`/tasks/${id}`);
    const { steps, checkpoints, receipts, ...task } = s;
    return { task, steps, checkpoints, receipts };
  },
  createTask: (goal: string, constraints: string) =>
    call<{ id: string; status: string }>("/tasks", {
      method: "POST",
      body: JSON.stringify({ goal, constraints }),
    }),
  planTask: (id: string) =>
    call<{ task_id: string; steps: PlanStep[] }>(`/tasks/${id}/plan`, {
      method: "POST",
    }),
  /** Typed but unwired in the UI: execute needs a live browser
   * observation only the extension can produce. */
  executeStep: (id: string, n: number, observation: Observation) =>
    call<ExecuteResult>(`/tasks/${id}/steps/${n}/execute`, {
      method: "POST",
      body: JSON.stringify({ observation }),
    }),
  /** Typed but unwired in the UI: see executeStep. */
  verifyStep: (
    id: string,
    n: number,
    body: {
      observation: Observation;
      previous_observation: Observation;
      expected_end_state: string;
      previous_url: string;
    },
  ) =>
    call<VerifyResult>(`/tasks/${id}/steps/${n}/verify`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  approveTask: (
    id: string,
    approvalId: string,
    scope?: { step?: number; element_id?: string; role_id?: string },
  ) =>
    call<{ task_id: string; status: string; approval_id: string }>(
      `/tasks/${id}/approve`,
      {
        method: "POST",
        body: JSON.stringify({ approval_id: approvalId, ...scope }),
      },
    ),
  /** Live pending approvals for a task — the ids the server
   * will accept at approveTask. Expired ones are not listed. */
  listPendingApprovals: (id: string) =>
    call<{ task_id: string; approvals: PendingApproval[] }>(
      `/tasks/${id}/approvals`,
    ),
  revokeGrants: (id: string) =>
    call<{ task_id: string; revoked: number }>(`/tasks/${id}/grants`, {
      method: "DELETE",
    }),
  cancelTask: (id: string) =>
    call<{ task_id: string; status: string }>(`/tasks/${id}/cancel`, {
      method: "POST",
    }),
  pauseTask: (id: string) =>
    call<{ task_id: string; status: string }>(`/tasks/${id}/pause`, {
      method: "POST",
    }),
  resumeTask: (id: string) =>
    call<{ task_id: string; status: string }>(`/tasks/${id}/resume`, {
      method: "POST",
    }),

  // ---- chat ----
  /** Send a chat message. The ONLY way the Agent view starts
   * work — a greeting returns kind "chat" with no task; a
   * real request returns kind "task_created" with the task
   * id. The view never calls createTask. */
  chatMessage: (text: string) =>
    call<ChatReply>("/chat/message", {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  /** Chat history, oldest first. Limit defaults to 50. */
  chatHistory: (limit = 50) =>
    call<{ messages: ChatMessage[] }>(`/chat/history?limit=${limit}`),

  // ---- chat uploads (PR #7) ----
  /** Upload a file attached in chat. XMLHttpRequest (not
   * fetch) so the composer can show upload progress. The
   * response carries the extracted text and the proposed
   * profile facts when the file looks like a resume. */
  uploadChatFile: (file: File, onProgress?: (pct: number) => void): Promise<ChatUpload> =>
    new Promise((resolve, reject) => {
      const token = getToken();
      if (!token) {
        reject(new Error("no token — paste the server token first"));
        return;
      }
      const form = new FormData();
      form.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/chat/uploads");
      xhr.setRequestHeader("X-Eclipse-Token", token);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) {
          onProgress(Math.round((e.loaded / e.total) * 100));
        }
      };
      xhr.onload = () => {
        if (xhr.status === 401) {
          window.dispatchEvent(new Event("eclipse-unauthorized"));
          reject(new Error("unauthorized — check the token"));
          return;
        }
        if (xhr.status < 200 || xhr.status >= 300) {
          reject(
            new Error(
              `${xhr.status} on /chat/uploads: ${xhr.responseText.slice(0, 200)}`,
            ),
          );
          return;
        }
        resolve(JSON.parse(xhr.responseText) as ChatUpload);
      };
      xhr.onerror = () => reject(new Error("upload failed — check the server"));
      xhr.send(form);
    }),
  /** Recent chat uploads, newest first — optionally only the
   * ones linked to one chat message (the thread renders its
   * file chips from this). */
  listChatUploads: (chatMessageId?: string) => {
    const q = chatMessageId
      ? `?chat_message_id=${encodeURIComponent(chatMessageId)}`
      : "";
    return call<{ uploads: ChatUpload[] }>(`/chat/uploads${q}`);
  },

  // ---- jobs ----
  setProfile: (resumeText: string) =>
    call<{ profile: Profile }>("/jobs/profile", {
      method: "POST",
      body: JSON.stringify({ resume_text: resumeText }),
    }),
  getProfile: () => call<{ profile: Profile }>("/jobs/profile"),
  matchRoles: (roles: RoleInput[]) =>
    call<MatchResponse>("/jobs/match", {
      method: "POST",
      body: JSON.stringify({ roles }),
    }),
  addReceipt: (
    taskId: string,
    receipt: {
      role_id: string;
      title: string;
      company?: string;
      url?: string;
      confirmation: string;
      status?: "submitted" | "failed";
    },
  ) =>
    call<{ receipt_id: string; task_id: string; status: string }>(
      `/tasks/${taskId}/receipts`,
      { method: "POST", body: JSON.stringify(receipt) },
    ),
  getReceipts: (taskId: string) =>
    call<ReceiptReport>(`/tasks/${taskId}/receipts`),
  checkApplied: (roleId: string, url: string) =>
    call<{ role_id: string; url: string; already_applied: boolean }>(
      `/jobs/applied?role_id=${encodeURIComponent(roleId)}&url=${encodeURIComponent(url)}`,
    ),

  // ---- mail ----
  createDrafts: (
    taskId: string,
    recipients: { email: string; role: RoleInput }[],
  ) =>
    call<DraftsCreated>("/mail/drafts", {
      method: "POST",
      body: JSON.stringify({ task_id: taskId, recipients }),
    }),
  listDrafts: (taskId?: string, status?: string) => {
    const params = new URLSearchParams();
    if (taskId) params.set("task_id", taskId);
    if (status) params.set("status", status);
    const q = params.toString();
    return call<{ drafts: Draft[] }>(`/mail/drafts${q ? `?${q}` : ""}`);
  },
  approveDraft: (draftId: string, approvalId: string) =>
    call<{ draft_id: string; status: string }>(
      `/mail/drafts/${draftId}/approve`,
      {
        method: "POST",
        body: JSON.stringify({ approval_id: approvalId }),
      },
    ),
  editDraft: (draftId: string, body: string) =>
    call<{ draft_id: string; status: string }>(`/mail/drafts/${draftId}`, {
      method: "PATCH",
      body: JSON.stringify({ body }),
    }),
  sendDraft: (draftId: string, attachResume: boolean) =>
    call<{
      draft_id: string;
      status: string;
      detail: string;
      attached_resume: string | null;
    }>(`/mail/drafts/${draftId}/send`, {
      method: "POST",
      body: JSON.stringify({ attach_resume: attachResume }),
    }),
  bounceCheck: (taskId: string) =>
    call<{ task_id: string; checked: BounceResult[] }>("/mail/bounce-check", {
      method: "POST",
      body: JSON.stringify({ task_id: taskId }),
    }),
  mailLog: (taskId?: string) =>
    call<{ log: MailLogEntry[] }>(
      taskId ? `/mail/log?task_id=${encodeURIComponent(taskId)}` : "/mail/log",
    ),

  // ---- vault ----
  createVault: (password: string) =>
    call<{ ok: boolean; vault_file: string }>("/vault/create", {
      method: "POST",
      body: JSON.stringify({ password }),
    }),
  unlockVault: (body: {
    password: string;
    task_id: string;
    card_number?: string;
    card_expiry?: string;
    card_cvv?: string;
  }) =>
    call<VaultUnlockResult>("/vault/unlock", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  lockVault: (taskId?: string) =>
    call<{ locked: boolean; revoked: number }>("/vault/lock", {
      method: "POST",
      body: JSON.stringify(taskId ? { task_id: taskId } : {}),
    }),
  fillCheck: (taskId: string, origin: string, field: string) =>
    call<VaultFillResult>("/vault/fill", {
      method: "POST",
      body: JSON.stringify({ task_id: taskId, origin, field }),
    }),
  revokeAllVault: () =>
    call<{ revoked: number }>("/vault/revoke-all", { method: "POST" }),
  vaultStatus: (taskId?: string) =>
    call<VaultStatus>(
      taskId ? `/vault/status?task_id=${encodeURIComponent(taskId)}` : "/vault/status",
    ),
  storePassword: (body: {
    task_id: string;
    origin: string;
    field: string;
    value: string;
  }) =>
    call<{ ok: boolean; origin: string; field: string }>(
      "/vault/store-password",
      { method: "POST", body: JSON.stringify(body) },
    ),

  // ---- memory & RAG ----
  storeFact: (text: string, source: string, topic?: string | null) =>
    call<{ id: string }>("/memory/facts", {
      method: "POST",
      body: JSON.stringify({ text, source, topic: topic ?? null }),
    }),
  recallFacts: (query: string, limit = 5) =>
    call<{ facts: Fact[] }>(
      `/memory/facts?query=${encodeURIComponent(query)}&limit=${limit}`,
    ),
  indexChunk: (id: string, text: string, kind = "fact", meta: Record<string, unknown> = {}) =>
    call<{ ok: boolean; id: string }>("/rag/index", {
      method: "POST",
      body: JSON.stringify({ id, text, kind, meta }),
    }),
  queryRag: (q: string, limit = 5) =>
    call<{ results: RagChunk[]; model_available: boolean }>(
      `/rag/query?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  forgetChunk: (chunkId: string) =>
    call<{ ok: boolean; id: string }>(`/rag/${encodeURIComponent(chunkId)}`, {
      method: "DELETE",
    }),
  ragStatus: () => call<RagStatus>("/rag/status"),

  // ---- proof ----
  /** B4: the server's metric keys are the benchmark JSON keys
   * (visual_grounding_first_try, redaction_recoverable_tokens)
   * and the run date is `generated_at`. Map them. */
  proofMetrics: async (): Promise<ProofMetrics> => {
    const s = await call<ServerProofMetrics>("/proof/metrics");
    if (!s.metrics) {
      return {
        metrics: null,
        targets: s.targets,
        honesty: s.honesty,
        run_date: null,
        fixtures: s.fixtures ?? null,
        worst_page: s.worst_page ?? null,
        worst_pii_fixture: s.worst_pii_fixture ?? null,
        reason: s.reason ?? null,
      };
    }
    return {
      metrics: {
        grounding: s.metrics.visual_grounding_first_try,
        pii_recall: s.metrics.pii_recall,
        pii_precision: s.metrics.pii_precision,
        redaction_tokens: s.metrics.redaction_recoverable_tokens,
        resource_mb: s.metrics.resource_mb,
        latency_p50_s: s.metrics.latency_p50_s,
      },
      targets: s.targets,
      honesty: s.honesty,
      run_date: s.generated_at,
      fixtures: s.fixtures,
      worst_page: s.worst_page,
      worst_pii_fixture: s.worst_pii_fixture,
      reason: null,
    };
  },
  whatWasSent: (q = "", limit = 50) =>
    call<{ count: number; entries: SentEntry[] }>(
      `/proof/what-was-sent?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  egress: () => call<EgressResult>("/proof/egress"),
  redaction: async (): Promise<RedactionResult> => {
    const s = await call<ServerRedaction>("/proof/redaction");
    return {
      generated_at: s.generated_at,
      recoverable_tokens: s.recoverable_tokens,
      passed: s.passed,
      pii_recall: s.pii_recall,
      pii_precision: s.pii_precision,
      per_fixture: s.per_fixture,
      label_based_not_regex: s.label_based_not_regex ?? {},
      note: s.note ?? null,
      reason: s.reason ?? null,
    };
  },
  /** B5: the server returns {step_time_s: {p50, mean, max},
   * latency_target_s, ...} — not {p50_s, target}. Map it. */
  latency: async (): Promise<LatencyResult> => {
    const s = await call<ServerLatency>("/proof/latency");
    return {
      generated_at: s.generated_at,
      p50_s: s.step_time_s?.p50 ?? null,
      mean_s: s.step_time_s?.mean ?? null,
      max_s: s.step_time_s?.max ?? null,
      target: s.latency_target_s,
      p50_within_target: s.p50_within_target,
      worst_fixture: s.worst_fixture,
      worst_fixture_s: s.worst_fixture_s,
      per_fixture: s.per_fixture,
      planner: s.planner,
      note: s.note ?? null,
      reason: s.reason ?? null,
    };
  },

  // ---- connectors ----
  listConnectors: () => call<{ connectors: ConnectorRow[] }>("/connectors"),
  connectorStatus: (name: string) =>
    call<ConnectorLiveStatus>(`/connectors/${name}/status`),
  enableConnector: (name: string) =>
    call<ConnectorRow>(`/connectors/${name}/enable`, { method: "POST" }),
  disableConnector: (name: string) =>
    call<ConnectorRow>(`/connectors/${name}/disable`, { method: "POST" }),
  createConnectorApproval: (body: {
    task_id: string;
    kind: "calendar_event" | "drive_upload";
    summary?: string;
    start?: string;
    path?: string;
  }) =>
    call<{ approval_id: string; expires_at: number }>("/connectors/approvals", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  gmailOffers: (maxResults = 20) =>
    call<{ offers: GmailOffer[] }>(
      `/connectors/gmail/offers?max_results=${maxResults}`,
    ),
  calendarFreebusy: (start: string, end: string) =>
    call<FreebusyResult>(
      `/connectors/calendar/freebusy?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`,
    ),
  createCalendarEvent: (body: {
    task_id: string;
    approval_id: string;
    summary: string;
    start: string;
    end: string;
    description?: string;
    location?: string;
  }) =>
    call<CalendarEventResult>("/connectors/calendar/events", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  driveResume: () => call<DriveFile>("/connectors/drive/resume"),
  driveUpload: (body: {
    task_id: string;
    approval_id: string;
    path: string;
    name?: string;
  }) =>
    call<DriveFile>("/connectors/drive/upload", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ---- monitor ----
  createWatch: (body: {
    url: string;
    selector: string;
    condition: string;
    interval_min: number;
  }) =>
    call<Watch>("/monitor/watches", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  watches: (status?: string) =>
    call<{ watches: Watch[] }>(
      status ? `/monitor/watches?status=${encodeURIComponent(status)}` : "/monitor/watches",
    ),
  getWatch: (id: string) => call<Watch>(`/monitor/watches/${id}`),
  repairWatch: (id: string, body: { selector?: string; url?: string }) =>
    call<Watch>(`/monitor/watches/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteWatch: (id: string) =>
    call<{ deleted: string }>(`/monitor/watches/${id}`, { method: "DELETE" }),
  muteWatch: (id: string, minutes = 60) =>
    call<Watch>(`/monitor/watches/${id}/mute`, {
      method: "POST",
      body: JSON.stringify({ minutes }),
    }),
  unmuteWatch: (id: string) =>
    call<Watch>(`/monitor/watches/${id}/unmute`, { method: "POST" }),
  dueWatches: () => call<{ watches: Watch[] }>("/monitor/due"),
  checkWatch: (id: string, found: boolean, value?: string | null) =>
    call<WatchCheckResult>(`/monitor/watches/${id}/check`, {
      method: "POST",
      body: JSON.stringify({ found, value: value ?? null }),
    }),
  watchEvents: (watchId?: string, limit = 50) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (watchId) params.set("watch_id", watchId);
    return call<{ events: WatchEvent[] }>(`/monitor/events?${params}`);
  },
  briefingLatest: () => call<Briefing>("/monitor/briefing/latest"),
  buildBriefing: () =>
    call<Briefing>("/monitor/briefing", { method: "POST" }),

  // ---- audit ----
  audit: (taskId?: string) =>
    call<{ entries: AuditEntry[] }>(
      taskId ? `/audit?task_id=${encodeURIComponent(taskId)}` : "/audit",
    ),
  verifyAudit: () => call<AuditVerifyResult>("/audit/verify"),
  exportAudit: () =>
    call<{ exported_at: number; entries: AuditEntry[] }>("/audit/export"),

  // ---- missions ----
  /** Chat intake: free text in, a draft mission out. The
   * server labels the source — "planner" or "rules" — and
   * the UI must show it, never hide it. A planner failure
   * falls through to the rule path server-side; it is not
   * an error here. PR #7: `uploadIds` attaches chat-uploaded
   * files (a resume) — their text enriches the draft locally
   * and their proposed facts come back for confirmation;
   * `storeInChat` also stores the exchange in the thread. */
  intakeMission: (
    message: string,
    options?: { uploadIds?: string[]; storeInChat?: boolean },
  ) =>
    call<IntakeResponse>("/missions/intake", {
      method: "POST",
      body: JSON.stringify({
        message,
        upload_ids: options?.uploadIds ?? [],
        store_in_chat: options?.storeInChat ?? false,
      }),
    }),
  createMission: (body: {
    type: MissionType;
    name: string;
    config: Record<string, unknown>;
    bounds?: Partial<MissionBounds>;
    /** PR #7: chat uploads the mission was created from. */
    upload_ids?: string[];
  }) =>
    call<Mission>("/missions", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listMissions: (status?: string) =>
    call<{ missions: Mission[] }>(
      status
        ? `/missions?status=${encodeURIComponent(status)}`
        : "/missions",
    ),
  /** The doc shows GET /missions/{id} as {mission, activity,
   * tasks, pending_approvals}; if the server ever returns the
   * mission flat (like getTask), adapt it here so the view
   * never has to guess. */
  getMission: async (id: string): Promise<MissionDetail> => {
    const s = await call<Record<string, unknown>>(`/missions/${id}`);
    if (s.mission && typeof s.mission === "object") {
      return {
        mission: s.mission as Mission,
        activity: (s.activity as MissionActivity[]) ?? [],
        tasks: (s.tasks as MissionTask[]) ?? [],
        pending_approvals: (s.pending_approvals as MissionApproval[]) ?? [],
      };
    }
    // Flat shape: the mission fields sit at the top level.
    const { activity, tasks, pending_approvals, ...mission } = s;
    return {
      mission: mission as unknown as Mission,
      activity: (activity as MissionActivity[]) ?? [],
      tasks: (tasks as MissionTask[]) ?? [],
      pending_approvals: (pending_approvals as MissionApproval[]) ?? [],
    };
  },
  updateMission: (
    id: string,
    body: {
      name?: string;
      config?: Record<string, unknown>;
      bounds?: Partial<MissionBounds>;
    },
  ) =>
    call<Mission>(`/missions/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  activateMission: (id: string) =>
    call<Mission>(`/missions/${id}/activate`, { method: "POST" }),
  pauseMission: (id: string) =>
    call<Mission>(`/missions/${id}/pause`, { method: "POST" }),
  resumeMission: (id: string) =>
    call<Mission>(`/missions/${id}/resume`, { method: "POST" }),
  stopMission: (id: string) =>
    call<Mission>(`/missions/${id}/stop`, { method: "POST" }),
  /** Run-now only schedules an immediate tick; the runner
   * picks it up — nothing runs inline. */
  runMissionNow: (id: string) =>
    call<Mission>(`/missions/${id}/run-now`, { method: "POST" }),
  deleteMission: (id: string) =>
    call<{ deleted: string }>(`/missions/${id}`, { method: "DELETE" }),
  missionActivity: (id: string, limit = 50, kind?: string) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (kind) params.set("kind", kind);
    return call<{ mission_id: string; activity: MissionActivity[] }>(
      `/missions/${id}/activity?${params}`,
    );
  },
  missionApprovals: (id: string) =>
    call<{ mission_id: string; approvals: MissionApproval[] }>(
      `/missions/${id}/approvals`,
    ),
  /** Approve a mission mail draft for sending — through the
   * TASK pending-approval gate, never the mail endpoint's
   * accept-any-id behaviour. The approval_id must name a live
   * pending approval recorded for this draft; on success the
   * draft is marked approved and a draft-scoped grant is
   * minted, so POST /mail/drafts/{id}/send consumes it.
   * Task (non-draft) approvals use approveTask instead. */
  approveMissionMail: (missionId: string, approvalId: string, draftId: string) =>
    call<{ draft_id: string; status: string }>(
      `/missions/${missionId}/approve-mail`,
      {
        method: "POST",
        body: JSON.stringify({ approval_id: approvalId, draft_id: draftId }),
      },
    ),
  missionTypes: () => call<{ types: MissionTypeSpec[] }>("/missions/types"),
  /** Resume upload is multipart: FormData must set its own
   * Content-Type boundary, so this bypasses the JSON `call`
   * helper (which would force application/json) and repeats
   * its 401 handling. The resume endpoints come from the
   * PR #5 task spec — they are not in DESIGN.md §3.3, so
   * verify the shapes against the server at integration. */
  uploadResume: async (file: File): Promise<ResumeStatus> => {
    const token = getToken();
    if (!token) throw new Error("no token — paste the server token first");
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/missions/resume", {
      method: "POST",
      headers: { "X-Eclipse-Token": token },
      body: form,
    });
    if (res.status === 401) {
      window.dispatchEvent(new Event("eclipse-unauthorized"));
      throw new Error("unauthorized — check the token");
    }
    if (!res.ok) {
      const text = await res.text();
      throw new Error(
        `${res.status} on /missions/resume: ${text.slice(0, 200)}`,
      );
    }
    return toResumeStatus((await res.json()) as ServerResume);
  },
  pasteResumeText: async (resumeText: string): Promise<ResumeStatus> => {
    // The server's ResumeTextIn takes `text`, not `resume_text`.
    const r = await call<ServerResume>("/missions/resume-text", {
      method: "POST",
      body: JSON.stringify({ text: resumeText }),
    });
    return toResumeStatus(r);
  },
  getResume: async (): Promise<ResumeStatus> => {
    const r = await call<ServerResume>("/missions/resume");
    return toResumeStatus(r);
  },
};
