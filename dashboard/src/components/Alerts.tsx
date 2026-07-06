import { useMemo } from "react";
import type { Alert } from "@/types";
import { assessAlert, severityLabel, type Severity } from "@/lib/severity";
import { humanizeMetric, pct, segmentLabel } from "@/lib/format";
import styles from "./Alerts.module.css";

interface AlertsProps {
  alerts: Alert[];
}

interface DecoratedAlert {
  alert: Alert;
  severity: Severity;
}

const SEVERITY_ORDER: Record<Severity, number> = {
  critical: 0, high: 1, moderate: 2, low: 3, info: 4,
};

/** Left-border color per severity (matches the token ramp in tokens.css). */
const SEVERITY_COLOR: Record<Severity, string> = {
  critical: "var(--sev-critical)",
  high: "var(--sev-high)",
  moderate: "var(--sev-moderate)",
  low: "var(--sev-low)",
  info: "var(--sev-info)",
};

const BREACH_SEVERITIES: ReadonlySet<Severity> = new Set(["critical", "high", "moderate"]);

/**
 * Segment-deterioration alerts with severity. The frozen contract carries no
 * `severity` field, so each alert is decorated here (on the client) via
 * `assessAlert(value, threshold)` — see lib/severity.ts. Breaches sort first
 * (critical → high → moderate), then non-breaches.
 *
 * The list is an aria-live="polite" region so newly-arriving alerts are
 * announced to assistive tech without stealing focus.
 */
export function Alerts({ alerts }: AlertsProps) {
  const decorated = useMemo<DecoratedAlert[]>(() => {
    return alerts
      .map((alert) => ({ alert, severity: assessAlert(alert.value, alert.threshold).severity }))
      .sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]);
  }, [alerts]);

  const breachCount = decorated.filter((d) => BREACH_SEVERITIES.has(d.severity)).length;

  return (
    <section className={styles.panel} aria-labelledby="alerts-heading">
      <header className={styles.header}>
        <h2 id="alerts-heading" className={styles.title}>Alerts</h2>
        {breachCount > 0 && (
          <span className={styles.breachBadge}>
            {breachCount} active {breachCount === 1 ? "breach" : "breaches"}
          </span>
        )}
      </header>

      <ul className={styles.list} role="list" aria-live="polite" aria-atomic="false">
        {decorated.length === 0 ? (
          <li className={styles.empty}>No alerts. Portfolio within tolerance.</li>
        ) : (
          decorated.map(({ alert, severity }, i) => (
            <li
              key={`${alert.metric}-${i}`}
              className={styles.alertRow}
              style={{ borderLeftColor: SEVERITY_COLOR[severity] }}
            >
              <div className={styles.rowTop}>
                <span
                  className={styles.severityTag}
                  style={{
                    background: SEVERITY_COLOR[severity],
                    color: "#fff",
                  }}
                >
                  {severityLabel(severity)}
                </span>
                <span className={styles.metric}>{humanizeMetric(alert.metric)}</span>
                <span className={styles.segment}>{segmentLabel(alert.segment)}</span>
              </div>
              <p className={styles.message}>{alert.message}</p>
              <dl className={styles.stats}>
                <div>
                  <dt>Value</dt>
                  <dd>{pct(alert.value)}</dd>
                </div>
                <div>
                  <dt>Threshold</dt>
                  <dd>{pct(alert.threshold)}</dd>
                </div>
              </dl>
            </li>
          ))
        )}
      </ul>
    </section>
  );
}
