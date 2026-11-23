import { Component, lazy, Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import { api, clearToken, getToken, setToken, type QuotaModel } from "./api";
import { AgentView } from "./views/AgentView";
import { MissionsView } from "./views/MissionsView";
import { WatchesView } from "./views/WatchesView";
import { ConnectorsView } from "./views/ConnectorsView";
import { VaultView } from "./views/VaultView";
import { MemoryView } from "./views/MemoryView";
import { ProofView } from "./views/ProofView";
import { AuditView } from "./views/AuditView";
import { MotionBackground } from "./components/MotionBackground";
import { EclipseHero } from "./components/EclipseHero";
import "./tokens.css";
import "./app.css";

const BoundaryScene = lazy(() =>
  import("./components/BoundaryScene").then((m) => ({ default: m.BoundaryScene })),
);

type ViewId =
  | "agent"
  | "missions"
  | "watches"
  | "connectors"
  | "vault"
  | "memory"
  | "proof"
  | "audit";

/** Nav icons (PR #6 Worker 11): one small geometric mark
 * per view, 18px, 1.5 stroke, currentColor — the same
 * stroke and size across all eight, so the labels align in
 * one column. */
function NavIcon({ d, extra }: { d: string; extra?: ReactNode }) {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
      {extra}
    </svg>
  );
}

const VIEWS: { id: ViewId; label: string; icon: ReactNode }[] = [
  { id: "agent", label: "Agent", icon: <NavIcon d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z" /> },
  {
    id: "missions",
    label: "Missions",
    icon: (
      <NavIcon
        d="M12 12h.01"
        extra={
          <>
            <circle cx="12" cy="12" r="9" />
            <circle cx="12" cy="12" r="5" />
            <circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" />
          </>
        }
      />
    ),
  },
  {
    id: "watches",
    label: "Watches & Briefing",
    icon: <NavIcon d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6Z" extra={<circle cx="12" cy="12" r="3" />} />,
  },
  { id: "connectors", label: "Connectors", icon: <NavIcon d="M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0V7ZM12 16v5" /> },
  { id: "vault", label: "Vault", icon: <NavIcon d="m11 13 8-8M17 5l3 3" extra={<circle cx="8" cy="16" r="4" />} /> },
  {
    id: "memory",
    label: "Memory & RAG",
    icon: <NavIcon d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z" />,
  },
  {
    id: "proof",
    label: "Proof",
    icon: <NavIcon d="M12 3 5 6v5c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6l-7-3ZM9 12l2 2 4-4" />,
  },
  {
    id: "audit",
    label: "Audit & Trace",
    icon: (
      <NavIcon
        d="M8 6h13M8 12h13M8 18h13"
        extra={
          <>
            <circle cx="4" cy="6" r="1" fill="currentColor" stroke="none" />
            <circle cx="4" cy="12" r="1" fill="currentColor" stroke="none" />
            <circle cx="4" cy="18" r="1" fill="currentColor" stroke="none" />
          </>
        }
      />
    ),
  },
];

/** Error boundary (review B1): one view crashing must never
 * blank the whole app again. A failed view shows its own
 * error state; the rest of the workspace keeps working. */
class ViewErrorBoundary extends Component<
  { name: string; children: ReactNode },
  { failed: boolean; message: string }
> {
  state = { failed: false, message: "" };

  static getDerivedStateFromError(err: Error) {
    return { failed: true, message: err.message };
  }

  render() {
    if (this.state.failed) {
      return (
        <section aria-label={this.props.name} className="pane">
          <h2>{this.props.name}</h2>
          <p role="alert" className="danger">
            This view crashed: {this.state.message}. The rest of the workspace
            is still working — fix the view or reload the page.
          </p>
        </section>
      );
    }
    return this.props.children;
  }
}

function NavList({
  view,
  approvalCount,
  onNavigate,
}: {
  view: ViewId;
  approvalCount: number;
  onNavigate: (v: ViewId) => void;
}) {
  return (
    <ul className="nav-list">
      {VIEWS.map((v) => {
        const badge = v.id === "agent" && approvalCount > 0 ? approvalCount : 0;
        return (
          <li key={v.id}>
            <button
              type="button"
              className={v.id === view ? "nav-current" : ""}
              aria-current={v.id === view ? "page" : undefined}
              aria-label={
                badge > 0
                  ? `${v.label}, ${badge} waiting for approval`
                  : v.label
              }
              title={v.label}
              onClick={() => onNavigate(v.id)}
            >
              <span className="nav-icon" aria-hidden="true">
                {v.icon}
              </span>
              <span className="nav-label">{v.label}</span>
              {badge > 0 && (
                <span className="nav-badge" aria-hidden="true">
                  {badge}
                </span>
              )}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/** The footer status bar. Rendered INSIDE .shell in the
 * connected view (a full-width wrapped row — the sticky
 * sidebar's range ends at the shell's content bottom, so
 * the footer must live inside it) and after the token gate
 * when not connected. */
function StatusBar({
  token,
  serverUp,
  selectedId,
  view,
  currentLabel,
}: {
  token: string | null;
  serverUp: boolean | null;
  selectedId: string | null;
  view: ViewId;
  currentLabel: string;
}) {
  return (
    <footer className="statusbar">
      <p role="status" aria-live="polite">
        {token
          ? serverUp === false
            ? "Server unreachable — showing the last data fetched."
            : selectedId && view === "agent"
              ? `Task ${selectedId} selected.`
              : `${currentLabel} view.`
          : "Not connected."}
      </p>
      <p className="muted">Local only — the server binds 127.0.0.1, and raw PII never leaves this device.</p>
    </footer>
  );
}

export default function App() {
  const [token, setTokenState] = useState<string | null>(() => getToken());
  const [tokenInput, setTokenInput] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [tokenRejected, setTokenRejected] = useState(false);
  const [view, setView] = useState<ViewId>("agent");
  const [navCollapsed, setNavCollapsed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showScene, setShowScene] = useState(false);
  const [scenePaused, setScenePaused] = useState(false);
  const [serverUp, setServerUp] = useState<boolean | null>(null);
  const [quota, setQuota] = useState<QuotaModel[]>([]);
  const [pendingApprovals, setPendingApprovals] = useState(0);
  const drawerRef = useRef<HTMLDialogElement | null>(null);
  // PR #7: the sidebar is sticky below the topbar, and the
  // topbar's height varies with viewport width (it wraps) —
  // so the real height is measured and published as a CSS
  // variable the sidebar's `top` reads. ResizeObserver keeps
  // it correct when the HUD appears/disappears or the window
  // resizes; app.css carries a fallback until the first
  // measurement lands.
  const appRef = useRef<HTMLDivElement | null>(null);
  const topbarRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const bar = topbarRef.current;
    const app = appRef.current;
    if (!bar || !app) return;
    const setHeight = () =>
      app.style.setProperty("--topbar-height", `${bar.offsetHeight}px`);
    setHeight();
    const observer = new ResizeObserver(setHeight);
    observer.observe(bar);
    return () => observer.disconnect();
  }, []);

  const saveToken = () => {
    const t = tokenInput.trim();
    if (!t) return;
    setToken(t);
    setTokenState(t);
    setTokenInput("");
    setTokenRejected(false);
  };

  const forgetToken = () => {
    clearToken();
    setTokenState(null);
    setQuota([]);
    setServerUp(null);
    setPendingApprovals(0);
  };

  // Sprint 9 fix (review M18): a 401 anywhere returns the user
  // to the token gate with a plain message — the views no longer
  // strand the user behind 401 banners. The api client fires
  // `eclipse-unauthorized` on a 401; this listener acts on it.
  useEffect(() => {
    const onUnauthorized = () => {
      clearToken();
      setTokenState(null);
      setTokenRejected(true);
    };
    window.addEventListener("eclipse-unauthorized", onUnauthorized);
    return () => window.removeEventListener("eclipse-unauthorized", onUnauthorized);
  }, []);

  // Sprint 9 fix (review M13): the top bar says "Connected" only
  // when the server answers /health — token presence alone is
  // not a connection.
  //
  // PR #6: the same 30s poll also fetches the ask_user task
  // list (server-side status filter — one cheap query) for
  // the nav approval badge. Real data, never a fake count; on
  // error the last count stands rather than flashing to 0.
  useEffect(() => {
    if (!token) {
      setServerUp(null);
      setQuota([]);
      setPendingApprovals(0);
      return;
    }
    let alive = true;
    const check = async () => {
      try {
        const [health, q, waiting] = await Promise.all([
          api.health(),
          api.quota(),
          api.listTasks("ask_user"),
        ]);
        if (!alive) return;
        setServerUp(health.status === "ok");
        setQuota(q.models);
        setPendingApprovals(waiting.tasks.length);
      } catch {
        if (alive) setServerUp(false);
      }
    };
    void check();
    const timer = setInterval(check, 30000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [token]);

  // The mobile nav is a native modal <dialog> (guide: dialog /
  // navigation-drawer): the browser provides the top layer,
  // Esc handling and focus return.
  useEffect(() => {
    const dialog = drawerRef.current;
    if (!dialog) return;
    if (drawerOpen && !dialog.open) {
      dialog.showModal();
    } else if (!drawerOpen && dialog.open) {
      dialog.close();
    }
  }, [drawerOpen]);

  useEffect(() => {
    const dialog = drawerRef.current;
    if (!dialog) return;
    const onClose = () => setDrawerOpen(false);
    dialog.addEventListener("close", onClose);
    return () => dialog.removeEventListener("close", onClose);
  }, []);

  const navigate = (v: ViewId) => {
    setView(v);
    setDrawerOpen(false);
  };

  const currentLabel = VIEWS.find((v) => v.id === view)?.label ?? "Agent";

  // Quota totals for the compact HUD summary ("Quota
  // 12/1,000"); the per-model detail lives behind a click.
  const quotaUsed = quota.reduce((sum, m) => sum + m.used_today, 0);
  const quotaLimit = quota.reduce((sum, m) => sum + m.limit, 0);
  const quotaHot = quotaLimit > 0 && quotaUsed / quotaLimit >= 0.8;

  return (
    <div className="app" ref={appRef}>
      {/* PR #6 motion: fixed drifting-gradient layer behind all
          content (aria-hidden, pointer-events none). The shell,
          token gate and statusbar sit above it via z-index. */}
      <MotionBackground />
      <a href="#main" className="skip-link">
        Skip to main content
      </a>
      <header className="topbar" ref={topbarRef}>
        {token && (
          <button
            type="button"
            className="menu-button"
            onClick={() => setDrawerOpen(true)}
            aria-label="Open navigation menu"
            aria-expanded={drawerOpen}
            aria-controls="nav-drawer"
          >
            ☰
          </button>
        )}
        <h1>Eclipse</h1>
        <p className="tagline">The browser agent that sees for you — and eclipses what no one else should see.</p>
        {token && (
          <div className="topbar-hud" role="status" aria-live="polite">
            <span className={serverUp ? "success" : serverUp === false ? "danger" : "muted"}>
              {serverUp ? "● Connected" : serverUp === false ? "● Server unreachable" : "● Checking…"}
            </span>
            {quota.length > 0 && (
              <details className="quota-details">
                <summary
                  className={`quota-summary ${quotaHot ? "warning" : ""}`}
                  aria-label={`Quota used today: ${quotaUsed} of ${quotaLimit} calls. Expand for per-model detail.`}
                >
                  Quota {quotaUsed.toLocaleString()}
                  {quotaLimit > 0 ? `/${quotaLimit.toLocaleString()}` : ""}
                </summary>
                <div className="quota-panel">
                  {quota.map((m) => (
                    <div key={m.name} className="quota-row">
                      <span>{m.name}</span>
                      <span
                        className={
                          m.limit > 0 && m.used_today / m.limit >= 0.8
                            ? "warning"
                            : "muted"
                        }
                        title={`${m.name}: ${m.used_today} of ${m.limit} calls today`}
                      >
                        {m.used_today}/{m.limit}
                      </span>
                    </div>
                  ))}
                </div>
              </details>
            )}
          </div>
        )}
        <div className="topbar-actions">
          {token && (
            <button type="button" onClick={() => setShowScene((v) => !v)} aria-pressed={showScene}>
              {showScene ? "Hide the Privacy Boundary" : "Show the Privacy Boundary"}
            </button>
          )}
          {token && (
            <button type="button" onClick={forgetToken}>
              Forget token
            </button>
          )}
        </div>
      </header>

      {!token ? (
        <main id="main" className="token-gate-wrap">
          {/* PR #6: the first screen gets the eclipse hero, so
              the gate reads as the product's front door, not a
              bare login card. Same hero as the chat empty state. */}
          <EclipseHero />
          <div className="token-gate">
            <h2>Connect to the local server</h2>
            {tokenRejected && (
              <p role="alert" className="danger">
                That token was rejected — check <span className="mono">data/.eclipse-token</span> on the server and paste it again.
              </p>
            )}
            <p>
              The workspace talks to the Eclipse server on localhost:8000.
              Paste the server token (from <span className="mono">data/.eclipse-token</span>).
              It is kept in this tab only and never written to a file — close
              the tab and it is gone.
            </p>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                saveToken();
              }}
            >
              <label htmlFor="server-token">Server token</label>
              <div className="token-row">
                <input
                  id="server-token"
                  type={showToken ? "text" : "password"}
                  value={tokenInput}
                  onChange={(e) => setTokenInput(e.target.value)}
                  autoComplete="off"
                />
                <button
                  type="button"
                  onClick={() => setShowToken((v) => !v)}
                  aria-pressed={showToken}
                >
                  {showToken ? "Hide" : "Show"}
                </button>
              </div>
              <button type="submit" className="primary" disabled={!tokenInput.trim()}>
                Connect
              </button>
            </form>
            <p className="muted">
              Local only — the server binds 127.0.0.1, and raw PII never leaves
              this device.
            </p>
          </div>
        </main>
      ) : (
        <div className="shell">
          <nav
            aria-label="Workspace"
            className={`sidebar ${navCollapsed ? "sidebar-collapsed" : ""}`}
          >
            <NavList view={view} approvalCount={pendingApprovals} onNavigate={navigate} />
            <button
              type="button"
              className="collapse-button"
              onClick={() => setNavCollapsed((v) => !v)}
              aria-label={navCollapsed ? "Expand navigation" : "Collapse navigation"}
              aria-pressed={navCollapsed}
            >
              {navCollapsed ? (
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="m13 17 5-5-5-5" />
                  <path d="m6 17 5-5-5-5" />
                </svg>
              ) : (
                <>
                  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="m11 17-5-5 5-5" />
                    <path d="m18 17-5-5 5-5" />
                  </svg>
                  <span>Collapse</span>
                </>
              )}
            </button>
          </nav>

          <main id="main" className="content">
            {showScene && (
              <section aria-label="Privacy Boundary" className="pane scene-pane">
                <div className="section-header">
                  <h2>The Privacy Boundary</h2>
                  <button
                    type="button"
                    onClick={() => setScenePaused((v) => !v)}
                    aria-pressed={scenePaused}
                  >
                    {scenePaused ? "Resume motion" : "Pause motion"}
                  </button>
                </div>
                <Suspense fallback={<p className="muted">Loading the scene…</p>}>
                  <BoundaryScene paused={scenePaused} />
                </Suspense>
                <p className="muted">
                  Green dots are redacted tokens — they pass through the wall to the
                  planner. Red dots are raw personal data — they bounce off.
                </p>
              </section>
            )}
            <ViewErrorBoundary key={view} name={currentLabel}>
              {view === "agent" && (
                <AgentView token={token} selectedId={selectedId} onSelect={setSelectedId} />
              )}
              {view === "missions" && (
                <MissionsView
                  token={token}
                  onSelectTask={(id) => {
                    setSelectedId(id);
                    setView("agent");
                  }}
                  onGoToAgent={() => setView("agent")}
                />
              )}
              {view === "watches" && <WatchesView token={token} />}
              {view === "connectors" && <ConnectorsView token={token} />}
              {view === "vault" && <VaultView token={token} />}
              {view === "memory" && <MemoryView token={token} />}
              {view === "proof" && <ProofView token={token} />}
              {view === "audit" && <AuditView token={token} />}
            </ViewErrorBoundary>
          </main>
          <StatusBar
            token={token}
            serverUp={serverUp}
            selectedId={selectedId}
            view={view}
            currentLabel={currentLabel}
          />
        </div>
      )}

      <dialog
        id="nav-drawer"
        ref={drawerRef}
        aria-label="Navigation"
        className="nav-drawer"
        closedby="any"
      >
        <div className="drawer-head">
          <h2>Eclipse</h2>
          <button type="button" onClick={() => setDrawerOpen(false)} aria-label="Close menu">
            ✕
          </button>
        </div>
        <NavList view={view} approvalCount={pendingApprovals} onNavigate={navigate} />
      </dialog>

      {!token && (
        <StatusBar
          token={token}
          serverUp={serverUp}
          selectedId={selectedId}
          view={view}
          currentLabel={currentLabel}
        />
      )}
    </div>
  );
}
