/**
 * Severity derivation for dashboard alerts.
 *
 * The frozen `Alert` contract (waspada/schema.py L138-146) has NO `severity`
 * field — it carries `value` and `threshold` only. "Alerts show severity" is an
 * acceptance requirement, so severity is DERIVED on the frontend from the breach
 * ratio (value / threshold), never added to the contract. This keeps the seam
 * with the pipeline frozen.
 */

export type Severity = "critical" | "high" | "moderate" | "low" | "info";

export interface SeverityAssessment {
  severity: Severity;
  /** true when value exceeds threshold (an actual breach). */
  breached: boolean;
  /** value / threshold, how far past the line. 1.0 == exactly at threshold. */
  ratio: number;
}

/**
 * Map a (value, threshold) pair to a severity.
 *
 * Bands are chosen so a 10-25% overage reads as "high", a larger overage as
 * "critical", and anything under threshold still gets a soft "info"/"low" tag
 * so the analyst sees tolerance is being tracked, not just breaches.
 *
 * Threshold of 0 is treated as "no threshold set" → info.
 */
export function assessAlert(value: number, threshold: number): SeverityAssessment {
  if (threshold <= 0) {
    return { severity: "info", breached: false, ratio: 0 };
  }
  const ratio = value / threshold;
  if (ratio >= 1.5) return { severity: "critical", breached: true, ratio };
  if (ratio >= 1.1) return { severity: "high", breached: true, ratio };
  if (ratio >= 1.0) return { severity: "moderate", breached: true, ratio };
  if (ratio >= 0.85) return { severity: "low", breached: false, ratio };
  return { severity: "info", breached: false, ratio };
}

/** Display label for a severity. */
export function severityLabel(s: Severity): string {
  switch (s) {
    case "critical": return "Critical";
    case "high": return "High";
    case "moderate": return "Moderate";
    case "low": return "Low";
    case "info": return "Info";
  }
}
