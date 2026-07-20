import type { OriginationHealth } from "@/types";
import { useI18n } from "@/lib/i18n";
import { usd } from "@/lib/format";
import styles from "./ModelCard.module.css";

/**
 * WA-039 — the Origination lane's health panel (approval rate, projected
 * default of the approved book, approved volume, application band mix).
 * Reuses the ModelCard's compact metric-strip styling so the side column stays
 * visually coherent; the band-mix bar reuses the same risk-ramp segments.
 */
const BAND_ORDER = ["Very Low", "Low", "Medium", "High", "Very High"] as const;

function pct(x: number | null | undefined, digits = 1): string {
  return x == null ? "—" : `${(x * 100).toFixed(digits)}%`;
}

export function OriginationHealthPanel({ health }: { health: OriginationHealth }) {
  const { t } = useI18n();
  const mix = health.band_mix ?? {};
  return (
    <section className={styles.card} aria-labelledby="orighealth-heading">
      <header className={styles.head}>
        <div className={styles.headText}>
          <h2 id="orighealth-heading" className={styles.title}>{t("oh.title")}</h2>
        </div>
      </header>

      <div className={styles.metrics}>
        <div className={styles.metric}>
          <span className={styles.mLabel}>{t("oh.approvalRate")}</span>
          <span className={styles.mValue}>{pct(health.approval_rate)}</span>
        </div>
        <div className={styles.metric}>
          <span className={styles.mLabel}>{t("oh.projectedDefault")}</span>
          <span className={styles.mValue}>{pct(health.projected_default_rate)}</span>
        </div>
        <div className={styles.metric}>
          <span className={styles.mLabel}>{t("oh.approvedVolume")}</span>
          <span className={styles.mValue}>{usd(health.approved_volume ?? 0)}</span>
        </div>
      </div>

      {Object.keys(mix).length > 0 && (
        <div className={styles.bands} role="img" aria-label={t("oh.bandMix")}>
          {BAND_ORDER.map((b) => (
            <span
              key={b}
              className={styles.bandSeg}
              style={{ width: `${(mix[b] ?? 0) * 100}%` }}
              data-band={b}
              title={`${b}: ${pct(mix[b] ?? 0, 0)}`}
            />
          ))}
        </div>
      )}
    </section>
  );
}
