import { useEffect, useState } from "react";

import type { DashboardPayload, ScoredAccount } from "@/types";
import { loadPayload } from "@/lib/payload";
import { useAuth } from "@/lib/auth";
import { WorkList } from "@/components/WorkList";
import { PortfolioHealth } from "@/components/PortfolioHealth";
import { Alerts } from "@/components/Alerts";
import { AgentDialogue } from "@/components/AgentDialogue";
import { AccountDrawer } from "@/components/AccountDrawer";
import { AuthScreen } from "@/components/AuthScreen";
import styles from "./App.module.css";

type LoadState =
  | { status: "loading" }
  | { status: "ready"; payload: DashboardPayload }
  | { status: "error"; message: string };

/**
 * WASPADA EWS Collections dashboard root.
 *
 * Loads the frozen `DashboardPayload` (committed fixture for the demo; the
 * real-run mode points `VITE_PAYLOAD_URL` at the orchestrator output — see
 * lib/payload.ts) and lays out the three analyst panels + the per-account
 * drawer. The payload shape never changes between demo and real run; the
 * contract is frozen (WA-001).
 *
 * Auth (WA-028): the whole dashboard is gated behind `useAuth`. Unauthenticated
 * users see the AuthScreen; a 401 on any protected call (via apiFetch in
 * useLiveRun) clears the session and bounces back to login.
 */
export function App() {
  const { status } = useAuth();

  // While we're validating a stored token against /me, show a minimal splash
  // instead of flashing the login form for already-authed returning users.
  if (status === "loading") {
    return <div className={styles.app} />;
  }
  if (status !== "authenticated") {
    return <AuthScreen />;
  }
  return <Dashboard />;
}

/** The authenticated dashboard. Split out so it only pays for its state/hooks
 *  once a session exists. */
function Dashboard() {
  const { user, logout } = useAuth();
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [selected, setSelected] = useState<ScoredAccount | null>(null);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    loadPayload()
      .then((payload) => {
        if (!cancelled) setState({ status: "ready", payload });
      })
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        if (!cancelled) setState({ status: "error", message });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className={styles.app}>
      <header className={styles.topbar}>
        <div className={styles.brand}>
          <img src="favicon.svg" alt="" width="28" height="28" className={styles.brandMark} />
          <div>
            <h1 className={styles.title}>WASPADA · EWS</h1>
            <p className={styles.subtitle}>Early-warning collections view</p>
          </div>
        </div>
        <div className={styles.session}>
          <p className={styles.mode}>
            {state.status === "ready" ? "Fixture demo" : ""}
          </p>
          {user && <span className={styles.userEmail}>{user.email}</span>}
          <button
            type="button"
            className={styles.logoutBtn}
            onClick={logout}
            aria-label="Sign out"
          >
            Sign out
          </button>
        </div>
      </header>

      <main className={styles.main}>
        {state.status === "loading" && (
          <p className={styles.status} role="status" aria-live="polite">
            Loading portfolio…
          </p>
        )}

        {state.status === "error" && (
          <p className={styles.statusError} role="alert">
            Couldn’t load the dashboard payload: {state.message}
          </p>
        )}

        {state.status === "ready" && (() => {
          const dialogue = state.payload.agent_dialogue ?? [];
          const contestedIds = new Set(dialogue.map((d) => d.loan_id));
          const jumpToDebate = (loanId: string) => {
            document.getElementById(`debate-${loanId}`)?.scrollIntoView({
              behavior: "smooth",
              block: "center",
            });
          };
          return (
          <>
            {contestedIds.size > 0 && (
              <div className={styles.contestAnchor}>
                <button
                  type="button"
                  onClick={() =>
                    document.getElementById("agent-dialogue-heading")?.scrollIntoView({
                      behavior: "smooth",
                      block: "start",
                    })
                  }
                >
                  ⚖ {contestedIds.size} {contestedIds.size === 1 ? "account" : "accounts"} contested — see debate ↓
                </button>
              </div>
            )}
            <div className={styles.grid}>
              <div className={styles.colMain}>
                <WorkList
                  accounts={state.payload.work_list}
                  contestedLoanIds={contestedIds}
                  onSelectAccount={setSelected}
                  onJumpToDebate={jumpToDebate}
                />
              </div>
              <div className={styles.colSide}>
                <PortfolioHealth health={state.payload.portfolio_health} />
                <Alerts alerts={state.payload.alerts} />
              </div>
            </div>
            <div className={styles.fullRow}>
              <AgentDialogue dialogue={dialogue} />
            </div>
          </>
          );
        })()}
      </main>

      <AccountDrawer account={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
