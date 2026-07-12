import { useMemo } from "react";

import type { DisputeRecord, DisputeRound } from "@/types";
import { useLiveRun } from "@/lib/useLiveRun";
import { useLiveDebateStream } from "@/lib/useLiveDebateStream";
import { useI18n, type TFunc } from "@/lib/i18n";
import styles from "./AgentDialogue.module.css";

interface AgentDialogueProps {
  /** Absent (undefined) = the payload predates the Agent Society feature → render nothing. */
  dialogue: DisputeRecord[] | undefined;
}

/** Chip color per speaker (labels are localized via i18n at render time). */
const SPEAKER_COLOR: Record<string, string> = {
  risk_auditor: "var(--sev-high)",
  risk_model: "var(--sev-info)",
  arbiter: "var(--pasar-teal-700)",
  human: "var(--pasar-teal-500)",
};
const KNOWN_SPEAKERS = new Set(["risk_auditor", "risk_model", "arbiter", "human"]);

type Resolution = DisputeRecord["resolution"];

const RESOLUTION_COLOR: Record<Resolution, string> = {
  upheld: "var(--pasar-teal-600)",
  overridden: "var(--sev-moderate)",
  escalated_approved: "var(--sev-info)",
  escalated_rejected: "var(--sev-critical)",
};

function speakerColor(name: string): string {
  return SPEAKER_COLOR[name] ?? "var(--text-subtle)";
}
/** Localized speaker label; unknown speakers fall back to the raw agent name. */
function speakerLabel(t: TFunc, name: string): string {
  return KNOWN_SPEAKERS.has(name) ? t(`speaker.${name}`) : name;
}

function Round({ round }: { round: DisputeRound }) {
  const { t } = useI18n();
  const color = speakerColor(round.speaker);
  return (
    <li className={styles.round}>
      <div className={styles.roundHead}>
        <span className="badge" style={{ background: color, color: "#fff" }}>
          {speakerLabel(t, round.speaker)}
        </span>
        <span className={styles.agentName}>{round.speaker}</span>
        {round.model && <span className={styles.modelTag}>{round.model}</span>}
        {round.confidence !== null && (
          <span className={styles.confidence}>
            <span className={styles.confidenceTrack} aria-hidden="true">
              <span
                className={styles.confidenceFill}
                style={{ width: `${Math.round(round.confidence * 100)}%`, background: color }}
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
  const { t } = useI18n();
  const resColor = RESOLUTION_COLOR[dispute.resolution];
  return (
    <li
      id={`debate-${dispute.loan_id}`}
      className={styles.dispute}
      style={{ borderLeftColor: resColor }}
      tabIndex={-1}
    >
      <div className={styles.disputeHead}>
        <span className={styles.loanId}>{dispute.loan_id}</span>
        <span className={styles.clash}>
          <span className={styles.clashSide}>
            {t("speaker.risk_model")} <strong>{dispute.model_band}</strong>
          </span>
          <span className={styles.vs} aria-hidden="true">
            {t("ad.vs")}
          </span>
          <span className={styles.clashSide}>
            {t("speaker.risk_auditor")} <strong>{dispute.auditor_view}</strong>
          </span>
        </span>
        <span className="badge" style={{ background: resColor, color: "#fff" }}>
          {t(`res.${dispute.resolution}`)}
        </span>
      </div>
      <ol className={styles.rounds} role="list">
        {dispute.rounds.map((round) => (
          <Round key={round.round_no} round={round} />
        ))}
      </ol>
      <p className={styles.rationale}>
        <span className={styles.rationaleLabel}>
          {t("ad.resolvedBy", { resolver: speakerLabel(t, dispute.resolved_by) })}
        </span>{" "}
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
  const { t } = useI18n();
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
            {t("ad.title")}
          </h2>
          <p className={styles.subtitle}>{t("ad.subtitle")}</p>
        </div>
        <div className={styles.actions}>
          {(isLive || isStreaming) && (
            <span
              className={styles.liveBadge}
              title={isStreaming ? t("ad.streaming.title") : t("ad.live.title")}
            >
              <span className={styles.liveDot} aria-hidden="true" />
              {isStreaming ? t("ad.streaming") : t("ad.live")}
            </span>
          )}
          {effective.length > 0 && (
            <span className={styles.countBadge}>
              {t(effective.length === 1 ? "ad.disputeOne" : "ad.disputeMany", { count: effective.length })}
              {escalated > 0 ? t("ad.escalated", { count: escalated }) : ""}
            </span>
          )}
          {active === "stream" ? (
            <button type="button" className={styles.resetBtn} onClick={stream.stop}>
              {t("ad.stopStream")}
            </button>
          ) : isLive ? (
            <button type="button" className={styles.resetBtn} onClick={reset}>
              {t("ad.backToFixture")}
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
                    {t("ad.connecting")}
                  </>
                ) : (
                  t("ad.watchLive")
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
                    {t("ad.running")}
                  </>
                ) : (
                  t("ad.runLive")
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
            ? t("ad.sr.opening")
            : stream.state.status === "live" && stream.state.done
              ? t("ad.sr.complete")
              : t("ad.sr.streaming")
          : running
            ? t("ad.sr.running")
            : isLive
              ? t("ad.sr.loaded")
              : ""}
      </p>

      {state.status === "error" && (
        <p className={styles.runError} role="alert">
          <strong>{t("ad.runError")}</strong> {state.message}
        </p>
      )}

      {stream.state.status === "error" && (
        <p className={styles.runError} role="alert">
          <strong>{t("ad.watchError")}</strong> {stream.state.message}
        </p>
      )}

      {/* Mid-run placeholder so judges aren’t staring at a static fixture while
          the Qwen call is in flight (~15-60s), or while the SSE stream is
          connecting before its first round lands. */}
      {running || stream.state.status === "connecting" ? (
        <div className={styles.runningState} aria-hidden="true">
          <span className={styles.spinnerLarge} />
          <p>{t("ad.debatingBody")}</p>
        </div>
      ) : effective.length === 0 ? (
        <p className={styles.empty}>
          {active === "stream"
            ? stream.state.status === "live" && stream.state.done
              ? t("ad.empty.streamDone")
              : t("ad.empty.streamWait")
            : isLive
              ? t("ad.empty.live")
              : t("ad.empty.fixture")}
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
              {t("ad.streamingMore")}
            </li>
          )}
        </ul>
      )}
    </section>
  );
}
