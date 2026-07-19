import type { ModelCard as ModelCardData } from "@/types";
import { useI18n } from "@/lib/i18n";
import styles from "./ModelCard.module.css";

/**
 * WA-093 — model-monitoring card. The reporting/audit surface: which model
 * scored this run and how well (AUC + calibration), the realised default rate,
 * the served band mix, and any feature drift flagged vs the reference cohort.
 * Renders nothing when the payload carries no card (older / model-less runs).
 */
function pct(x: number | null | undefined, digits = 1): string {
  return x == null ? "—" : `${(x * 100).toFixed(digits)}%`;
}
function num(x: number | null | undefined, digits = 3): string {
  return x == null ? "—" : x.toFixed(digits);
}

export function ModelCard({ card }: { card: ModelCardData | undefined }) {
  const { t } = useI18n();
  if (!card) return null;

  const drift = card.drift_significant ?? [];
  const bands = card.band_distribution ?? {};
  const bandOrder = ["Very Low", "Low", "Medium", "High", "Very High"] as const;

  return (
    <section className={styles.card} aria-labelledby="modelcard-heading">
      <header className={styles.head}>
        <div className={styles.headText}>
          <h2 id="modelcard-heading" className={styles.title}>{t("mc.title")}</h2>
          {card.model_id ? (
            <span className={styles.modelId} title={t("mc.modelId")}>{card.model_id}</span>
          ) : null}
        </div>
        <span
          className={styles.calib}
          data-on={card.calibrated ? "1" : undefined}
          title={t(card.calibrated ? "mc.calibrated.on" : "mc.calibrated.off")}
        >
          {t(card.calibrated ? "mc.calibrated" : "mc.uncalibrated")}
        </span>
      </header>

      <div className={styles.metrics}>
        <div className={styles.metric}>
          <span className={styles.mLabel}>{t("mc.auc")}</span>
          <span className={styles.mValue}>{num(card.auc)}</span>
        </div>
        <div className={styles.metric}>
          <span className={styles.mLabel}>{t("mc.defaultRate")}</span>
          <span className={styles.mValue}>{pct(card.observed_default_rate)}</span>
        </div>
        <div className={styles.metric}>
          <span className={styles.mLabel}>{t("mc.brier")}</span>
          <span className={styles.mValue}>
            {num(card.brier_calibrated ?? card.brier_raw)}
          </span>
        </div>
        <div className={styles.metric}>
          <span className={styles.mLabel}>{t("mc.scored")}</span>
          <span className={styles.mValue}>{card.n_scored ?? "—"}</span>
        </div>
      </div>

      {Object.keys(bands).length > 0 && (
        <div className={styles.bands} role="img" aria-label={t("mc.bandMix")}>
          {bandOrder.map((b) => {
            const share = bands[b] ?? 0;
            return (
              <span
                key={b}
                className={styles.bandSeg}
                style={{ width: `${share * 100}%` }}
                data-band={b}
                title={`${b}: ${pct(share, 0)}`}
              />
            );
          })}
        </div>
      )}

      {drift.length > 0 && (
        <p className={styles.drift}>
          {t("mc.drift", { features: drift.join(", ") })}
        </p>
      )}
    </section>
  );
}
