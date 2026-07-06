import type { PortfolioHealth } from "@/types";
import { pct, sortCohorts } from "@/lib/format";
import styles from "./PortfolioHealth.module.css";

interface PortfolioHealthProps {
  health: PortfolioHealth;
}

/** Bar color for a vintage rate — red above 10%, teal otherwise. */
function vintageBarColor(rate: number): string {
  return rate >= 0.1 ? "var(--chart-bar-trend)" : "var(--chart-bar)";
}

/**
 * Portfolio health panel: the three aggregates the analyst monitors.
 *  - NPL ratio as a headline stat
 *  - Vintage default-rate as a bar chart (by issue-year cohort)
 *  - Status mix as a horizontal bar list
 *
 * Charts are hand-rolled CSS bars — no chart library. Keeps the bundle small
 * (cheap Android / 3G) and the markup accessible (each bar is a labelled cell).
 * Roll rates are NOT in the MVP payload (deferred to the Freddie Mac panel
 * stretch — see schema.py L121-123), so we render the three contract fields.
 */
export function PortfolioHealth({ health }: PortfolioHealthProps) {
  const vintages = sortCohorts(health.vintage_default_rate);
  const maxVintage = Math.max(...vintages.map(([, r]) => r), 0.001);
  const statuses = Object.entries(health.status_mix).sort((a, b) => b[1] - a[1]);

  return (
    <section className={styles.panel} aria-labelledby="health-heading">
      <h2 id="health-heading" className={styles.title}>Portfolio health</h2>

      {/* NPL ratio headline */}
      <div className={styles.nplCard}>
        <div>
          <p className={styles.nplLabel}>NPL ratio</p>
          <p className={styles.nplValue}>{pct(health.npl_ratio)}</p>
          <p className={styles.nplHint}>
            Fraction of accounts in delinquent or default status.
          </p>
        </div>
        <div
          className={styles.nplMeter}
          role="meter"
          aria-valuemin={0}
          aria-valuemax={1}
          aria-valuenow={Number(health.npl_ratio.toFixed(3))}
          aria-label={`NPL ratio ${pct(health.npl_ratio)}`}
        >
          <span
            className={styles.nplMeterFill}
            style={{ width: `${Math.min(health.npl_ratio * 100, 100)}%` }}
          />
        </div>
      </div>

      {/* Vintage default-rate chart */}
      <div className={styles.subsection}>
        <h3 className={styles.subtitle}>Vintage default rate</h3>
        {vintages.length === 0 ? (
          <p className={styles.empty}>No vintage data.</p>
        ) : (
          <ul className={styles.barList} role="list">
            {vintages.map(([cohort, rate]) => (
              <li key={cohort} className={styles.barRow}>
                <span className={styles.barLabel}>{cohort}</span>
                <span className={styles.barTrack}>
                  <span
                    className={styles.barFill}
                    style={{
                      width: `${(rate / maxVintage) * 100}%`,
                      background: vintageBarColor(rate),
                    }}
                  />
                </span>
                <span className={styles.barValue}>{pct(rate)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Status mix */}
      <div className={styles.subsection}>
        <h3 className={styles.subtitle}>Status mix</h3>
        {statuses.length === 0 ? (
          <p className={styles.empty}>No status mix data.</p>
        ) : (
          <ul className={styles.barList} role="list">
            {statuses.map(([status, prop]) => (
              <li key={status} className={styles.barRow}>
                <span className={styles.barLabel}>{status}</span>
                <span className={styles.barTrack}>
                  <span
                    className={styles.barFill}
                    style={{ width: `${prop * 100}%`, background: "var(--chart-bar)" }}
                  />
                </span>
                <span className={styles.barValue}>{pct(prop)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
