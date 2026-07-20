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
 *
 * `expected_loss` is an ADDITIVE optional key (WA-024): Expected Loss in USD =
 * p_default × LGD(0.45) × EAD(outstanding_principal). Absent on older payloads
 * — the work-list renders it only when present (graceful absence).
 */
export interface ScoredAccount {
  loan_id: string;
  /** WA-039: origination rows carry their own id (loan_id is aliased to it). */
  application_id?: string;
  p_default: number; // P(eventual default) ∈ [0, 1] — the MODEL's score, never rewritten
  score_band: string; // the MODEL's risk level, e.g. "Very Low".."Very High"
  segment: Segment;
  recommended_action: "call" | "watch" | "auto-cure" | "approve" | "refer" | "reject";
  expected_loss?: number; // USD at risk = PD × LGD(0.45) × EAD (WA-024, additive optional)

  /**
   * The risk level AFTER the Agent Society's debate (WA-048, additive optional).
   * Equals `score_band` wherever the model's band stood; differs only when a
   * dispute went against the model and the override was applied. This is what
   * `recommended_action` is derived from — the society's ruling reaches the
   * work-list instead of dying in the transcript. Absent on pre-WA-048 payloads.
   */
  final_band?: string;
  /** Why the society moved the band (present only when final_band ≠ score_band). */
  override_reason?: string;

  /**
   * G1: the model's single largest signed driver behind this score, e.g.
   * `"dti=31.20 ↑"` (↑ pushed toward default, ↓ toward safe). Additive optional —
   * present only when the run supplied the fitted model + features (WA-050
   * `explain`); absent on older payloads. Shown as a "why this row" chip.
   */
  top_driver?: string;
}

/** Portfolio-level cross-sectional aggregates (PortfolioHealth TypedDict). */
export interface PortfolioHealth {
  npl_ratio: number; // fraction of accounts in delinquent/default status
  vintage_default_rate: Record<string, number>; // default rate keyed by issue_date cohort
  status_mix: Record<string, number>; // proportion of accounts per current_status value
  /** Sum of per-account Expected Loss in USD (WA-024, additive optional). */
  total_expected_loss?: number;
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

/**
 * One turn in an agent-to-agent risk debate (DisputeRound dataclass).
 * `model` is the LLM behind the turn (e.g. "qwen3.6-flash"); null = a
 * deterministic speaker. `evidence` is the cited feature values / portfolio
 * stats grounding the claim (see HACKATHON.md § debate protocol).
 */
export interface DisputeRound {
  round_no: number;
  speaker: string; // agent name, e.g. "risk_auditor"
  model: string | null;
  claim: string;
  confidence: number | null; // speaker's stated confidence ∈ [0, 1]
  evidence: string[];
}

/**
 * A contested account's full negotiation record (Dispute dataclass, serialized
 * per the shape frozen in HACKATHON.md — additive to the WA-001 contract).
 */
export interface DisputeRecord {
  loan_id: string;
  opened_by: string;
  model_band: string; // the Actuary's risk level, e.g. "Very High"
  auditor_view: string; // the Risk Auditor's independent read: "Low" | "Medium" | "High"
  rounds: DisputeRound[];
  resolution: "upheld" | "overridden" | "escalated_approved" | "escalated_rejected";
  resolved_by: string; // "risk_model" (conceded) | "arbiter" | "human"
  rationale: string;
  /** WA-048: the risk level the society ruled for ("" when the model's band stands). */
  revised_band?: string;
  /** WA-048: whether that ruling actually reached the work-list (see the direction rule). */
  applied?: boolean;
  /** Client-only (usePacedReveal): true while the debate is still revealing and
   *  the outcome hasn't been reached yet — consumers hide the resolution/rationale
   *  so the reveal doesn't spoil its own ending. Absent = show the outcome. */
  pendingResolution?: boolean;
}

/**
 * The frozen frontend hand-off: ranked work-list + health + alerts.
 * `agent_dialogue` is an ADDITIVE optional key (Qwen-pivot, HACKATHON.md):
 * absent on older payloads, so the guard below does not require it.
 */
/**
 * WA-093: per-run model-monitoring card (additive optional). Present only when
 * the run scored with a fitted model; older payloads omit it.
 */
export interface ModelCard {
  model_id?: string | null;
  auc?: number | null;
  brier_raw?: number | null;
  brier_calibrated?: number | null;
  calibrated?: boolean;
  n_train?: number | null;
  n_test?: number | null;
  split_method?: string | null;
  n_scored?: number;
  observed_default_rate?: number | null;
  band_distribution?: Record<string, number>;
  trained_at?: string | null;
  psi?: Record<string, number>;
  drift_flags?: string[];
  drift_significant?: string[];
  max_psi?: number;
}

/** WA-039: origination-book aggregates (the origination lane's health shape). */
export interface OriginationHealth {
  approval_rate: number;
  projected_default_rate: number;
  band_mix: Record<string, number>;
  approved_volume: number;
}

export interface DashboardPayload {
  /** WA-039: which lane produced this payload; absent = collections. */
  lane?: "collections" | "origination";
  work_list: ScoredAccount[];
  portfolio_health: PortfolioHealth | OriginationHealth;
  alerts: Alert[];
  agent_dialogue?: DisputeRecord[];
  model_card?: ModelCard;
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
