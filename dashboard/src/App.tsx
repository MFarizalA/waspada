import { useEffect, useState } from "react";

import type { DashboardPayload, ScoredAccount } from "@/types";
import { loadPayload } from "@/lib/payload";
import { useAuth } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
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
/** Smooth-scroll to a section heading (the megamenu nav targets). */
function scrollToId(id: string, block: ScrollLogicalPosition = "start") {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block });
}

function Dashboard() {
  const { user, logout } = useAuth();
  const { t, toggle } = useI18n();
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
          <img src="favicon.svg" alt="" width="30" height="30" className={styles.brandMark} />
          <div>
            <h1 className={styles.title}>{t("brand.name")}</h1>
            <p className={styles.subtitle}>{t("brand.sub")}</p>
          </div>
        </div>

        <nav className={styles.nav} aria-label={t("brand.sub")}>
          <button type="button" className={styles.navLink} onClick={() => scrollToId("worklist-heading")}>
            {t("nav.worklist")}
          </button>
          <button type="button" className={styles.navLink} onClick={() => scrollToId("health-heading")}>
            {t("nav.health")}
          </button>
          <button type="button" className={styles.navLink} onClick={() => scrollToId("alerts-heading")}>
            {t("nav.alerts")}
          </button>
          <button type="button" className={styles.navLink} onClick={() => scrollToId("agent-dialogue-heading")}>
            {t("nav.debate")}
          </button>
        </nav>

        <div className={styles.session}>
          <p className={styles.mode}>
            {state.status === "ready" ? t("top.fixtureDemo") : ""}
          </p>
          <button
            type="button"
            className={styles.langBtn}
            onClick={toggle}
            aria-label={t("lang.label")}
          >
            {t("lang.toggle")}
          </button>
          {user && <span className={styles.userEmail}>{user.email}</span>}
          <button
            type="button"
            className={styles.logoutBtn}
            onClick={logout}
            aria-label={t("top.signOut")}
          >
            {t("top.signOut")}
          </button>
        </div>
      </header>

      <main className={styles.main}>
        {state.status === "loading" && (
          <p className={styles.status} role="status" aria-live="polite">
            {t("top.loading")}
          </p>
        )}

        {state.status === "error" && (
          <p className={styles.statusError} role="alert">
            {t("top.loadError", { message: state.message })}
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
                  onClick={() => scrollToId("agent-dialogue-heading")}
                >
                  {t(contestedIds.size === 1 ? "top.contestedOne" : "top.contestedMany", {
                    count: contestedIds.size,
                  })}
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
              <AgentDialogue dialogue={dialogue} accounts={state.payload.work_list} />
            </div>
          </>
          );
        })()}
      </main>

      <AccountDrawer account={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
