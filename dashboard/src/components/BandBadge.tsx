import type { ScoredAccount } from "@/types";
import { score } from "@/lib/format";
import { useI18n } from "@/lib/i18n";

/** The frozen risk-level vocabulary (waspada/schema.py RISK_LEVELS), low→high. */
const KNOWN_LEVELS = new Set(["Very Low", "Low", "Medium", "High", "Very High"]);

/** Map a risk level to its color from the design-token risk ramp. */
function bandColor(band: string): string {
  switch (band) {
    case "Very Low":
    case "Low": return "var(--risk-low)";
    case "Medium": return "var(--risk-moderate)";
    case "High": return "var(--risk-elevated)";
    case "Very High": return "var(--risk-high)";
    default:   return "var(--text-subtle)";
  }
}

/**
 * Compact pill showing the risk level (quintile-derived score band).
 * Color-coded via the design-system risk ramp so the analyst can scan severity
 * at a glance. The label localizes (EN/中文) for known levels; an unknown value
 * renders raw so payload drift stays visible instead of crashing.
 */
export function BandBadge({ band }: { band: string }) {
  const { t } = useI18n();
  const label = KNOWN_LEVELS.has(band) ? t(`band.val.${band}`) : band;
  return (
    <span
      className="badge"
      style={{ background: bandColor(band), color: "#fff" }}
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
    <span style={{ color: bandColor(account.score_band), fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>
      {score(account.p_default)}
    </span>
  );
}
