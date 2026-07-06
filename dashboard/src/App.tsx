import { useEffect, useState } from "react";

import type { DashboardPayload, ScoredAccount } from "@/types";
import { loadPayload } from "@/lib/payload";
import { WorkList } from "@/components/WorkList";
import { PortfolioHealth } from "@/components/PortfolioHealth";
import { Alerts } from "@/components/Alerts";
import { AccountDrawer } from "@/components/AccountDrawer";
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
 */
export function App() {
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
        <p className={styles.mode}>
          {state.status === "ready" ? "Fixture demo" : ""}
        </p>
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

        {state.status === "ready" && (
          <div className={styles.grid}>
            <div className={styles.colMain}>
              <WorkList
                accounts={state.payload.work_list}
                onSelectAccount={setSelected}
              />
            </div>
            <div className={styles.colSide}>
              <PortfolioHealth health={state.payload.portfolio_health} />
              <Alerts alerts={state.payload.alerts} />
            </div>
          </div>
        )}
      </main>

      <AccountDrawer account={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
