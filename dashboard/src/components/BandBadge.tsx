import type { ScoredAccount } from "@/types";
import { score } from "@/lib/format";

/** Map a risk quintile band to its color from the Pasar token ramp. */
function bandColor(band: string): string {
  switch (band) {
    case "Q1":
    case "Q2": return "var(--risk-low)";
    case "Q3": return "var(--risk-moderate)";
    case "Q4": return "var(--risk-elevated)";
    case "Q5": return "var(--risk-high)";
    default:   return "var(--text-subtle)";
  }
}

/**
 * Compact pill showing the score band (risk quintile). Color-coded via the
 * design-system risk ramp so the analyst can scan severity at a glance.
 */
export function BandBadge({ band }: { band: string }) {
  return (
    <span
      className="badge"
      style={{ background: bandColor(band), color: "#fff" }}
      aria-label={`Risk band ${band}`}
    >
      {band}
    </span>
  );
}

/**
 * The p_default score, shown as a colored number. The number itself carries the
 * precision (two decimals); the color encodes the band for fast scanning.
 */
export function ScoreText({ account }: { account: ScoredAccount }) {
  return (
    <span style={{ color: bandColor(account.score_band), fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>
      {score(account.p_default)}
    </span>
  );
}
