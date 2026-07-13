import type { ScoredAccount } from "@/types";
import { score } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { riskLevelColor, riskLevelLabel } from "@/lib/riskLevel";

/**
 * Compact pill showing the risk level (quintile-derived score band).
 * Color-coded via the design-system risk ramp so the analyst can scan severity
 * at a glance. The label localizes (EN/中文) for known levels; an unknown value
 * renders raw so payload drift stays visible instead of crashing.
 */
export function BandBadge({ band }: { band: string }) {
  const { t } = useI18n();
  const label = riskLevelLabel(t, band);
  return (
    <span
      className="badge"
      style={{ background: riskLevelColor(band), color: "#fff" }}
      aria-label={t("band.aria", { band: label })}
    >
      {label}
    </span>
  );
}

/**
 * The p_default score, shown as a colored number. The number itself carries the
 * precision (two decimals); the color encodes the risk level for fast scanning.
 */
export function ScoreText({ account }: { account: ScoredAccount }) {
  return (
    <span style={{ color: riskLevelColor(account.score_band), fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>
      {score(account.p_default)}
    </span>
  );
}
