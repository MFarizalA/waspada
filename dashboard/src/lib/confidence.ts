/**
 * G2 — categorical confidence signal (UX research: agentic-transparency patterns).
 *
 * Numeric confidence alone doesn't land; the research is explicit that trust
 * comes from a 3-tier categorical signal — confident / review-worthy / the agent
 * itself is uncertain. We keep the exact % (finance wants precision) and add a
 * colour-coded tier dot beside it, so the *level of trust* is glanceable and
 * separate from *who spoke* (the speaker colour).
 */
export type ConfidenceTier = "high" | "medium" | "low";

/** Map a stated confidence ∈ [0,1] to a tier. ``null`` for a missing/undefined
 *  confidence (a deterministic speaker with no self-assessment). */
export function confidenceTier(conf: number | null | undefined): ConfidenceTier | null {
  if (conf == null || Number.isNaN(conf)) return null;
  if (conf >= 0.75) return "high";
  if (conf >= 0.5) return "medium";
  return "low";
}

/** Token colour per tier (green = confident, amber = review, red = uncertain). */
export const CONFIDENCE_TONE: Record<ConfidenceTier, string> = {
  high: "var(--pasar-teal-600)",
  medium: "var(--sev-moderate)",
  low: "var(--sev-critical)",
};

/** Short i18n key suffix for the tier label (speaker.* style). Used for the
 *  dot's accessible title; falls back to the raw key if untranslated. */
export const CONFIDENCE_LABEL: Record<ConfidenceTier, string> = {
  high: "conf.high",
  medium: "conf.medium",
  low: "conf.low",
};
