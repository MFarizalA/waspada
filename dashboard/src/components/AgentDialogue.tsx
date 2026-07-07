import { useMemo } from "react";

import type { DisputeRecord, DisputeRound } from "@/types";
import { useLiveRun } from "@/lib/useLiveRun";
import styles from "./AgentDialogue.module.css";

interface AgentDialogueProps {
  /** Absent (undefined) = the payload predates the Agent Society feature → render nothing. */
  dialogue: DisputeRecord[] | undefined;
}

/** Character card per speaker — display name + chip color (tokens only). */
const SPEAKERS: Record<string, { label: string; color: string }> = {
  risk_auditor: { label: "Risk Auditor", color: "var(--sev-high)" },
  risk_model: { label: "Actuary", color: "var(--sev-info)" },
  arbiter: { label: "Credit Arbiter", color: "var(--pasar-teal-700)" },
  human: { label: "Analyst", color: "var(--pasar-teal-500)" },
};

type Resolution = DisputeRecord["resolution"];

const RESOLUTIONS: Record<Resolution, { label: string; color: string }> = {
  upheld: { label: "Upheld", color: "var(--pasar-teal-600)" },
  overridden: { label: "Overridden", color: "var(--sev-moderate)" },
  escalated_approved: { label: "Escalated · approved", color: "var(--sev-info)" },
  escalated_rejected: { label: "Escalated · rejected", color: "var(--sev-critical)" },
};

function speakerOf(name: string): { label: string; color: string } {
  return SPEAKERS[name] ?? { label: name, color: "var(--text-subtle)" };
}

function Round({ round }: { round: DisputeRound }) {
  const speaker = speakerOf(round.speaker);
  return (
    <li className={styles.round}>
      <div className={styles.roundHead}>
        <span className="badge" style={{ background: speaker.color, color: "#fff" }}>
          {speaker.label}
        </span>
        <span className={styles.agentName}>{round.speaker}</span>
        {round.model && <span className={styles.modelTag}>{round.model}</span>}
        {round.confidence !== null && (
          <span className={styles.confidence}>
            <span className={styles.confidenceTrack} aria-hidden="true">
              <span
                className={styles.confidenceFill}
                style={{ width: `${Math.round(round.confidence * 100)}%`, background: speaker.color }}
              />
            </span>
            {Math.round(round.confidence * 100)}%
          </span>
        )}
      </div>
      <p className={styles.claim}>{round.claim}</p>
      {round.evidence.length > 0 && (
        <ul className={styles.evidenceList} role="list">
          {round.evidence.map((item, i) => (
            <li key={i} className={styles.evidence}>
              {item}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function DisputeCard({ dispute }: { dispute: DisputeRecord }) {
  const res = RESOLUTIONS[dispute.resolution];
  const resolver = speakerOf(dispute.resolved_by);
  return (
    <li
      id={`debate-${dispute.loan_id}`}
      className={styles.dispute}
      style={{ borderLeftColor: res.color }}
      tabIndex={-1}
    >
      <div className={styles.disputeHead}>
        <span className={styles.loanId}>{dispute.loan_id}</span>
        <span className={styles.clash}>
          <span className={styles.clashSide}>
            Actuary <strong>{dispute.model_band}</strong>
          </span>
          <span className={styles.vs} aria-hidden="true">
            vs
          </span>
          <span className={styles.clashSide}>
            Risk Auditor <strong>{dispute.auditor_view}</strong>
          </span>
        </span>
        <span className="badge" style={{ background: res.color, color: "#fff" }}>
          {res.label}
        </span>
      </div>
      <ol className={styles.rounds} role="list">
        {dispute.rounds.map((round) => (
          <Round key={round.round_no} round={round} />
        ))}
      </ol>
      <p className={styles.rationale}>
        <span className={styles.rationaleLabel}>Resolved by {resolver.label.toLowerCase()}:</span>{" "}
        {dispute.rationale}
      </p>
    </li>
  );
}

/**
 * The Agent Society panel — renders the risk debate: which of the model's
 * scores the auditor challenged, the round-by-round argument with cited
 * evidence, and how each dispute was resolved (concession, arbiter ruling, or
 * human escalation). Contract: `DashboardPayload.agent_dialogue` (additive,
 * optional — see HACKATHON.md § dispute record).
 *
 * By default the panel renders the fixture debate. The "Run live (Qwen)"
 * action POSTs to `/api/run?brain=qwen` and, on success, swaps in the live
 * `agent_dialogue` (same frozen shape — the backend conforms per WA-014). Only
 * the debate surface swaps; the rest of the dashboard keeps its fixture view.
 */
export function AgentDialogue({ dialogue }: AgentDialogueProps) {
  const { state, run, reset } = useLiveRun();
  const isLive = state.status === "ok";
  // On a successful live run, the panel shows the live debate (possibly empty —
  // the auditor may agree with every audited score). `?? []` covers a live
  // payload that omits the additive key. `effective` is always a concrete array
  // so the memo and render stay type-narrow; the pre-WA-014 guard below still
  // hides the panel entirely when the fixture predates agent_dialogue.
  const liveDialogue: DisputeRecord[] = isLive ? state.payload.agent_dialogue ?? [] : [];
  const effective: DisputeRecord[] = isLive ? liveDialogue : (dialogue ?? []);
  const running = state.status === "running";

  const escalated = useMemo(
    () => effective.filter((d) => d.resolution.startsWith("escalated")).length,
    [effective],
  );

  // Pre-WA-014 payloads have no agent_dialogue at all — the panel is invisible.
  if (dialogue === undefined) return null;

  return (
    <section
      id="agent-dialogue"
      className={styles.panel}
      aria-labelledby="agent-dialogue-heading"
      tabIndex={-1}
    >
      <header className={styles.header}>
        <div className={styles.headerText}>
          <h2 id="agent-dialogue-heading" className={styles.title}>
            Agent Society · Risk Debate
          </h2>
          <p className={styles.subtitle}>
            The Risk Auditor audits the riskiest scores; contested calls are argued, ruled, and — when
            unresolved — escalated to the analyst.
          </p>
        </div>
        <div className={styles.actions}>
          {isLive && (
            <span className={styles.liveBadge} title="Showing a live Qwen run">
              <span className={styles.liveDot} aria-hidden="true" />
              Live
            </span>
          )}
          {effective.length > 0 && (
            <span className={styles.countBadge}>
              {effective.length} {effective.length === 1 ? "dispute" : "disputes"}
              {escalated > 0 ? ` · ${escalated} escalated` : ""}
            </span>
          )}
          {isLive ? (
            <button type="button" className={styles.resetBtn} onClick={reset}>
              Back to fixture
            </button>
          ) : (
            <button
              type="button"
              className={styles.runBtn}
              onClick={run}
              disabled={running}
              aria-busy={running}
            >
              {running ? (
                <>
                  <span className={styles.spinner} aria-hidden="true" />
                  Running…
                </>
              ) : (
                "Run live (Qwen)"
              )}
            </button>
          )}
        </div>
      </header>

      {/* Polite live region: announces run state to assistive tech without
          stealing focus. role="alert" on the error block promotes it. */}
      <p className={styles.visuallyHidden} role="status" aria-live="polite">
        {running
          ? "Running the live Qwen debate…"
          : isLive
            ? "Live debate loaded."
            : ""}
      </p>

      {state.status === "error" && (
        <p className={styles.runError} role="alert">
          <strong>Couldn’t run live:</strong> {state.message}
        </p>
      )}

      {/* Mid-run placeholder so judges aren’t staring at a static fixture while
          the Qwen call is in flight (~15-60s). */}
      {running ? (
        <div className={styles.runningState} aria-hidden="true">
          <span className={styles.spinnerLarge} />
          <p>
            The Risk Auditor, Actuary, and Credit Arbiter are debating the riskiest accounts over Qwen…
          </p>
        </div>
      ) : effective.length === 0 ? (
        <p className={styles.empty}>
          {isLive
            ? "No disputes this live run — the auditor agreed with every audited score."
            : "No disputes this run — the auditor agreed with every audited score."}
        </p>
      ) : (
        <ul className={styles.disputeList} role="list">
          {effective.map((dispute) => (
            <DisputeCard key={dispute.loan_id} dispute={dispute} />
          ))}
        </ul>
      )}
    </section>
  );
}
