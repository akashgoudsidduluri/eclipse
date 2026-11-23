import { useEffect, useState } from "react";
import { api, type TaskSummary } from "./api";

/** Poll a loader on mount and every `intervalMs`. The loader
 * sets its own state; this hook only owns the timer and the
 * alive flag. Pass intervalMs 0 for load-once. */
export function usePoll(
  load: () => Promise<void> | void,
  deps: unknown[],
  intervalMs: number,
) {
  useEffect(() => {
    let alive = true;
    const run = async () => {
      if (!alive) return;
      await load();
    };
    void run();
    if (intervalMs <= 0) {
      return () => {
        alive = false;
      };
    }
    const timer = setInterval(() => void run(), intervalMs);
    return () => {
      alive = false;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, intervalMs]);
}

/** The task list, shared by every view that needs a task
 * picker (Mail, Vault, Connectors, Jobs receipts). Polled
 * slowly — pickers do not need live data. */
export function useTaskList(token: string | null) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    const load = async () => {
      try {
        const data = await api.listTasks();
        if (alive) {
          setTasks(data.tasks);
          setError(null);
        }
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      }
    };
    void load();
    const timer = setInterval(load, 15000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [token]);

  return { tasks, error };
}

/** A short id for approvals the client generates. The server
 * accepts any id the client supplies and records it in the
 * grant (tasks_api approve, mail_api approve). */
export function newApprovalId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `appr-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** PR #7: bytes → "842 B" / "12 KB" / "2.4 MB" for the chat
 * file chips. Pure — covered by tests/format.test.mjs. */
export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  const mb = bytes / (1024 * 1024);
  return `${mb >= 10 ? Math.round(mb) : mb.toFixed(1)} MB`;
}

/** Unix seconds → short local time, or "—" for null. */
export function fmtTime(ts: number | null | undefined): string {
  if (ts === null || ts === undefined) return "—";
  return new Date(ts * 1000).toLocaleTimeString();
}

/** Unix seconds → short local date + time, or "—" for null. */
export function fmtDateTime(ts: number | null | undefined): string {
  if (ts === null || ts === undefined) return "—";
  return new Date(ts * 1000).toLocaleString();
}

/** Unix MILLISECONDS → short local date + time, or "—" for
 * null. Audit entries ONLY: the server writes audit
 * created_at in ms (server/core/audit.py) while every other
 * timestamp it returns is seconds. Passing an audit
 * timestamp to fmtDateTime multiplies it a second time and
 * lands in year 58863 — the old pane's plain
 * new Date(created_at) was correct, and this is that, with
 * the null guard. */
export function fmtAuditDateTime(ts: number | null | undefined): string {
  if (ts === null || ts === undefined) return "—";
  return new Date(ts).toLocaleString();
}

/** PR #6: "now", re-ticking once a minute so RelativeTime
 * labels stay fresh ("2 min ago" → "3 min ago"). One cheap
 * setState per minute per mounted RelativeTime — no
 * second-level ticking, the labels never need it. */
export function useRelativeNow(intervalMs: number = 60000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/** PR #6: unix SECONDS → "just now" / "2 min ago" / "3 hr
 * ago" / "2 days ago", or a short date for anything older
 * than a week. Null-safe ("—"). Future timestamps (clock
 * skew) clamp to "just now". Pure — pass `nowMs` in tests. */
export function fmtRelative(
  ts: number | null | undefined,
  nowMs: number = Date.now(),
): string {
  if (ts === null || ts === undefined) return "—";
  const diffS = Math.max(0, Math.floor(nowMs / 1000) - ts);
  if (diffS < 45) return "just now";
  if (diffS < 90) return "1 min ago";
  const mins = Math.floor(diffS / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours === 1 ? "1 hr ago" : `${hours} hr ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return days === 1 ? "1 day ago" : `${days} days ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

/** A value the server returns with an uncertain shape (a
 * calendar event's nested start/end) rendered safely. */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    if (typeof obj.dateTime === "string") return obj.dateTime;
    if (typeof obj.date === "string") return obj.date;
    try {
      return JSON.stringify(value);
    } catch {
      return "—";
    }
  }
  return "—";
}
