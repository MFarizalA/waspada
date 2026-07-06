/**
 * WASPADA dashboard data contract — TypeScript mirror of the FROZEN contract in
 * `waspada/schema.py` (locked in WA-001). Every field name and shape here is
 * derived verbatim from the Python TypedDicts so the frontend consumes the
 * agreed API exactly. Do NOT change this unilaterally — if the contract is
 * wrong, raise it (see WA-001 / `schema.py`).
 *
 * Source of truth: waspada/schema.py (lines 125-153), concretely exercised by
 * tests/test_schema.py::test_dashboard_payload_is_typeddict_shape.
 */

/** A portfolio slice by product and region (Segment dataclass, flattened to JSON). */
export interface Segment {
  product: string;
  region: string;
}

/**
 * One ranked work-list row. This is a `ScoredAccounts` dataclass serialized to
 * a JSON record — `segment` is nested (not flattened) per the frozen contract.
 *
 * `recommended_action` ∈ {"call", "watch", "auto-cure"} (schema.py L116).
 */
export interface ScoredAccount {
  loan_id: string;
  p_default: number; // P(eventual default) ∈ [0, 1]
  score_band: string; // risk quintile band, e.g. "Q1".."Q5"
  segment: Segment;
  recommended_action: "call" | "watch" | "auto-cure";
}

/** Portfolio-level cross-sectional aggregates (PortfolioHealth TypedDict). */
export interface PortfolioHealth {
  npl_ratio: number; // fraction of accounts in delinquent/default status
  vintage_default_rate: Record<string, number>; // default rate keyed by issue_date cohort
  status_mix: Record<string, number>; // proportion of accounts per current_status value
}

/**
 * A cohort/portfolio deterioration alert (Alert TypedDict). NOTE: the frozen
 * contract has NO `severity` field — severity is DERIVED on the frontend from
 * the value/threshold breach ratio (see lib/severity.ts). Do not add it here.
 *
 * `segment` is null for portfolio-wide alerts, else a product/region slice.
 */
export interface Alert {
  metric: string; // e.g. "npl_ratio", "vintage_default_rate"
  value: number;
  threshold: number;
  message: string;
  segment: Segment | null; // null = portfolio-wide
}

/** The frozen frontend hand-off: ranked work-list + health + alerts. */
export interface DashboardPayload {
  work_list: ScoredAccount[];
  portfolio_health: PortfolioHealth;
  alerts: Alert[];
}

/** Narrowing guard so a malformed fixture fails loudly at load, not in render. */
export function isDashboardPayload(x: unknown): x is DashboardPayload {
  if (typeof x !== "object" || x === null) return false;
  const p = x as Record<string, unknown>;
  return (
    Array.isArray(p.work_list) &&
    typeof p.portfolio_health === "object" && p.portfolio_health !== null &&
    Array.isArray(p.alerts)
  );
}
