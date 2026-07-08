import { useMemo } from "react";

import type { DisputeRecord, DisputeRound } from "@/types";
import { useLiveRun } from "@/lib/useLiveRun";
import { useLiveDebateStream } from "@/lib/useLiveDebateStream";
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
 * The panel renders the committed fixture by default. Two live paths swap in a
 * fresh debate:
 *  - "Run live (Qwen)" — POST /api/run?brain=qwen, whole debate returned in one
 *    JSON envelope (useLiveRun). Best when the stream endpoint is down.
 *  - "Watch live" — SSE on /api/run/stream?brain=qwen, rounds appended as they
 *    arrive (useLiveDebateStream). The debating spinner holds until the first
 *    round lands, then transitions to live cards.
 * Only the debate surface swaps; the rest of the dashboard keeps its fixture.
 */
export function AgentDialogue({ dialogue }: AgentDialogueProps) {
  const { state, run, reset } = useLiveRun();
  const stream = useLiveDebateStream();
  const isLive = state.status === "ok";
  const isStreaming = stream.state.status === "live" || stream.state.status === "connecting";

  // The stream takes precedence when active — it's the most "live" view. Then a
  // completed Run-live run. Then the fixture.
  let effective: DisputeRecord[];
  let active: "fixture" | "run" | "stream";
  if (isStreaming) {
    effective =
      stream.state.status === "live" ? stream.state.disputes : [];
    active = "stream";
  } else if (isLive) {
    effective = state.payload.agent_dialogue ?? [];
    active = "run";
  } else {
    effective = dialogue ?? [];
    active = "fixture";
  }
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
          {(isLive || isStreaming) && (
            <span
              className={styles.liveBadge}
              title={isStreaming ? "Watching the live stream" : "Showing a live Qwen run"}
            >
              <span className={styles.liveDot} aria-hidden="true" />
              {isStreaming ? "Streaming" : "Live"}
            </span>
          )}
          {effective.length > 0 && (
            <span className={styles.countBadge}>
              {effective.length} {effective.length === 1 ? "dispute" : "disputes"}
              {escalated > 0 ? ` · ${escalated} escalated` : ""}
            </span>
          )}
          {active === "stream" ? (
            <button type="button" className={styles.resetBtn} onClick={stream.stop}>
              Stop stream
            </button>
          ) : isLive ? (
            <button type="button" className={styles.resetBtn} onClick={reset}>
              Back to fixture
            </button>
          ) : (
            <>
              <button
                type="button"
                className={styles.watchBtn}
                onClick={stream.start}
                disabled={running}
                aria-pressed={isStreaming}
              >
                {stream.state.status === "connecting" ? (
                  <>
                    <span className={styles.spinner} aria-hidden="true" />
                    Connecting…
                  </>
                ) : (
                  "Watch live"
                )}
              </button>
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
            </>
          )}
        </div>
      </header>

      {/* Polite live region: announces run state to assistive tech without
          stealing focus. role="alert" on the error block promotes it. */}
      <p className={styles.visuallyHidden} role="status" aria-live="polite">
        {isStreaming
          ? stream.state.status === "connecting"
            ? "Opening the live debate stream…"
            : stream.state.status === "live" && stream.state.done
              ? "Live debate complete."
              : "Streaming the live debate…"
          : running
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

      {stream.state.status === "error" && (
        <p className={styles.runError} role="alert">
          <strong>Couldn’t watch live:</strong> {stream.state.message}
        </p>
      )}

      {/* Mid-run placeholder so judges aren’t staring at a static fixture while
          the Qwen call is in flight (~15-60s), or while the SSE stream is
          connecting before its first round lands. */}
      {running || stream.state.status === "connecting" ? (
        <div className={styles.runningState} aria-hidden="true">
          <span className={styles.spinnerLarge} />
          <p>
            The Risk Auditor, Actuary, and Credit Arbiter are debating the riskiest accounts over Qwen…
          </p>
        </div>
      ) : effective.length === 0 ? (
        <p className={styles.empty}>
          {active === "stream"
            ? stream.state.status === "live" && stream.state.done
              ? "No disputes this stream — the auditor agreed with every audited score."
              : "Waiting for the first round…"
            : isLive
              ? "No disputes this live run — the auditor agreed with every audited score."
              : "No disputes this run — the auditor agreed with every audited score."}
        </p>
      ) : (
        <ul className={styles.disputeList} role="list">
          {effective.map((dispute) => (
            <DisputeCard key={dispute.loan_id} dispute={dispute} />
          ))}
          {/* While streaming and not yet done, show a quiet "more coming" tail
              under the live cards so the panel reads as actively streaming. */}
          {active === "stream" && !(stream.state.status === "live" && stream.state.done) && (
            <li className={styles.streamingTail} aria-hidden="true">
              <span className={styles.spinnerSmall} />
              Streaming more rounds…
            </li>
          )}
        </ul>
      )}
    </section>
  );
}
