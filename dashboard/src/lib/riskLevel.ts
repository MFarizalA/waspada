import type { TFunc } from "@/lib/i18n";
import { score } from "@/lib/format";

/**
 * The risk-level domain (waspada/schema.py RISK_LEVELS) — the single home for
 * risk-level color, localized label, and the FICO-style paired display
 * ("0.91 (Very High)"). Mirrors `_BAND_ORDINAL` in
 * `waspada/agents/risk_auditor.py` as the canonical risk-level concept on the
 * frontend side.
 *
 * Previously this color/label logic was independently duplicated three ways —
 * `BandBadge`'s `bandColor`, `AccountDrawer`'s hand-copied `scoreColor`
 * (a different fallback color), and `AgentDialogue`'s debate clash line
 * (no color/i18n at all). Consolidated here so there's one place to fix a
 * color or add a level, not three.
 */

export const RISK_LEVELS = ["Very Low", "Low", "Medium", "High", "Very High"] as const;

const KNOWN_LEVELS = new Set<string>(RISK_LEVELS);

/** Map a risk level to its color from the design-token risk ramp. Unknown
 *  values render in a neutral color rather than throwing, so payload drift
 *  stays visible instead of crashing. */
export function riskLevelColor(level: string): string {
  switch (level) {
    case "Very Low":
    case "Low": return "var(--risk-low)";
    case "Medium": return "var(--risk-moderate)";
    case "High": return "var(--risk-elevated)";
    case "Very High": return "var(--risk-high)";
    default: return "var(--text-subtle)";
  }
}

/** Localized label for a known level; unknown values render raw. */
export function riskLevelLabel(t: TFunc, level: string): string {
  return KNOWN_LEVELS.has(level) ? t(`band.val.${level}`) : level;
}

/**
 * FICO-style paired display: "0.91 (Very High)". `pDefault` is omitted when
 * the speaker has no numeric score (the Risk Auditor's independent view is
 * qualitative-only) — falls back to the label alone.
 */
export function riskLevelDisplay(t: TFunc, level: string, pDefault?: number): string {
  const label = riskLevelLabel(t, level);
  if (pDefault === undefined || pDefault === null) return label;
  return t("band.paired", { score: score(pDefault), level: label });
}
