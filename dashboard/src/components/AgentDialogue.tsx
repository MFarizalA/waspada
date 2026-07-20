import { useMemo, useState } from "react";

import type { DisputeRecord, DisputeRound, ScoredAccount } from "@/types";
import { useLiveRun } from "@/lib/useLiveRun";
import { useLiveDebateStream } from "@/lib/useLiveDebateStream";
import { usePacedReveal } from "@/lib/usePacedReveal";
import { useI18n, type TFunc } from "@/lib/i18n";
import { riskLevelColor, riskLevelLabel, riskLevelDisplay } from "@/lib/riskLevel";
import { confidenceTier, CONFIDENCE_TONE, CONFIDENCE_LABEL } from "@/lib/confidence";
import { DebateFlow } from "./DebateFlow";
import styles from "./AgentDialogue.module.css";

interface AgentDialogueProps {
  /** Absent (undefined) = the payload predates the Agent Society feature → render nothing. */
  dialogue: DisputeRecord[] | undefined;
  /** The payload's work_list — joined against each dispute's loan_id to pair
   *  the Actuary's p_default with its risk-level label (FICO-style). Optional
   *  and defaults to empty so an omitted prop degrades to label-only, never
   *  crashes. */
  accounts?: ScoredAccount[];
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
        {round.confidence !== null && (() => {
          const tier = confidenceTier(round.confidence);
          return (
            <span className={styles.confidence}>
              {tier && (
                <span
                  className={styles.confTier}
                  style={{ background: CONFIDENCE_TONE[tier] }}
                  title={t(CONFIDENCE_LABEL[tier])}
                  aria-label={t(CONFIDENCE_LABEL[tier])}
                />
              )}
              <span className={styles.confidenceTrack} aria-hidden="true">
                <span
                  className={styles.confidenceFill}
                  style={{ width: `${Math.round(round.confidence * 100)}%`, background: color }}
                />
              </span>
              {Math.round(round.confidence * 100)}%
            </span>
          );
        })()}
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

function DisputeCard({ dispute, pDefault }: { dispute: DisputeRecord; pDefault?: number }) {
  const { t } = useI18n();
  // While the reveal is still unfolding this dispute, don't spoil its verdict.
  const pending = dispute.pendingResolution === true;
  const resColor = pending ? "var(--text-subtle)" : RESOLUTION_COLOR[dispute.resolution];
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
            {t("speaker.risk_model")}{" "}
            <strong style={{ color: riskLevelColor(dispute.model_band) }}>
              {riskLevelDisplay(t, dispute.model_band, pDefault)}
            </strong>
          </span>
          <span className={styles.vs} aria-hidden="true">
            {t("ad.vs")}
          </span>
          <span className={styles.clashSide}>
            {t("speaker.risk_auditor")}{" "}
            <strong style={{ color: riskLevelColor(dispute.auditor_view) }}>
              {riskLevelLabel(t, dispute.auditor_view)}
            </strong>
          </span>
        </span>
        <span className="badge" style={{ background: resColor, color: "#fff" }}>
          {pending ? t("flow.deliberating") : t(`res.${dispute.resolution}`)}
        </span>
      </div>
      <ol className={styles.rounds} role="list">
        {dispute.rounds.map((round) => (
          <Round key={round.round_no} round={round} />
        ))}
      </ol>
      {!pending && (
        <p className={styles.rationale}>
          <span className={styles.rationaleLabel}>
            {t("ad.resolvedBy", { resolver: speakerLabel(t, dispute.resolved_by) })}
          </span>{" "}
          {dispute.rationale}
        </p>
      )}
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
export function AgentDialogue({ dialogue, accounts = [] }: AgentDialogueProps) {
  const { t } = useI18n();
  const { state, run, reset } = useLiveRun();
  const stream = useLiveDebateStream();
  const isLive = state.status === "ok";
  const isStreaming = stream.state.status === "live" || stream.state.status === "connecting";
  // The debate starts idle on login — the analyst runs the society, and only
  // then does the transcript appear. The committed fixture is offered as an
  // explicit "sample run" (demo fallback) rather than shown as a done debate.
  const [showSample, setShowSample] = useState(false);

  // The stream takes precedence when active — it's the most "live" view. Then a
  // completed Run-live run. Then the fixture (only once revealed).
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
    effective = showSample ? (dialogue ?? []) : [];
    active = "fixture";
  }
  const running = state.status === "running";

  // Human-paced reveal: turn the ordered debate data into a step-by-step reveal
  // the analyst controls (play/pause/step/replay). Reset only when a new session
  // starts — keyed on source + whether the sample was revealed. NOT on the data
  // itself, so a growing SSE array (whose first dispute id flips LIVE→real
  // mid-stream) keeps advancing instead of restarting.
  const revealKey = `${active}|${showSample ? "sample" : ""}`;
  const reveal = usePacedReveal(effective, { resetKey: revealKey });
  const revealed = reveal.revealed;

  // loan_id -> p_default, for the FICO-style paired display on the Actuary's
  // side of the clash line. A completed "Run live" carries its own fresh
  // work_list (preferred — the live run's own numbers); the SSE stream never
  // sends one, so streaming falls back to the fixture's `accounts` prop. A
  // genuinely unmatched loan_id (e.g. the stream's synthetic "LIVE" id before
  // a resolution lands) simply misses the map — riskLevelDisplay degrades to
  // the label alone, never crashes.
  const liveAccounts = isLive ? state.payload.work_list : undefined;
  const pDefaultByLoanId = useMemo(
    () => new Map((liveAccounts ?? accounts).map((a) => [a.loan_id, a.p_default])),
    [liveAccounts, accounts],
  );

  // Count only resolved (non-pending) escalations so the badge never spoils an
  // outcome the reveal hasn't reached yet.
  const escalated = useMemo(
    () =>
      revealed.filter((d) => !d.pendingResolution && d.resolution.startsWith("escalated")).length,
    [revealed],
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

      {/* Mid-run placeholder: instead of a blank spinner while the Qwen call is
          in flight (~15-60s) or the SSE stream connects, show the society SPINE
          right away so there's real structure on screen — the debate branch
          then streams in beneath it, node by node. */}
      {running || stream.state.status === "connecting" ? (
        <DebateFlow disputes={[]} pending />
      ) : effective.length === 0 ? (
        active === "fixture" ? (
          // Idle on login: show the society spine + a call to action rather than
          // a pre-baked "done" debate or a "no disputes" line — the debate only
          // appears once the analyst starts a run. The committed fixture is
          // offered as an explicit "sample run" (demo fallback).
          <div className={styles.idle}>
            <DebateFlow disputes={[]} pending captionKey="flow.idle" />
            {(dialogue?.length ?? 0) > 0 && (
              <button
                type="button"
                className={styles.sampleBtn}
                onClick={() => setShowSample(true)}
              >
                {t("ad.watchDebate")}
              </button>
            )}
          </div>
        ) : (
          <p className={styles.empty}>
            {active === "stream"
              ? stream.state.status === "live" && stream.state.done
                ? t("ad.empty.streamDone")
                : t("ad.empty.streamWait")
              : t("ad.empty.live")}
          </p>
        )
      ) : (
        <>
          {/* Transport controls — the human drives the reveal: pause on a turn to
              read it, step one turn at a time, or replay the whole debate. */}
          <div className={styles.controls} role="group" aria-label={t("ad.controlsLabel")}>
            <button
              type="button"
              className={styles.ctrlBtn}
              data-primary="1"
              onClick={reveal.playing ? reveal.pause : reveal.play}
              disabled={reveal.done}
            >
              {reveal.playing ? t("ad.pause") : t("ad.play")}
            </button>
            <button type="button" className={styles.ctrlBtn} onClick={reveal.step} disabled={reveal.done}>
              {t("ad.step")}
            </button>
            <button type="button" className={styles.ctrlBtn} onClick={reveal.replay} disabled={reveal.atStart}>
              {t("ad.replay")}
            </button>
            <span className={styles.speed} role="group" aria-label={t("ad.speedLabel")}>
              <button
                type="button"
                data-on={reveal.speed === "normal" ? "1" : undefined}
                onClick={() => reveal.setSpeed("normal")}
              >
                {t("ad.speedNormal")}
              </button>
              <button
                type="button"
                data-on={reveal.speed === "slow" ? "1" : undefined}
                onClick={() => reveal.setSpeed("slow")}
              >
                {t("ad.speedSlow")}
              </button>
            </span>
            <span className={styles.progress} aria-hidden="true">
              {reveal.cursor} / {reveal.totalSteps}
            </span>
          </div>

          {/* WA-091: node-graph view of the society + the currently-revealing
              dispute's branch. Fed the PACED `revealed` set (rounds sliced to the
              reveal cursor), so each turn pops in as the human watches. */}
          {revealed.length === 0 ? (
            <DebateFlow disputes={[]} pending captionKey="flow.convening" />
          ) : (
            <DebateFlow
              disputes={revealed}
              selectedIndex={reveal.done ? undefined : reveal.activeDisputeIndex}
            />
          )}

          {/* The "…is thinking" beat — names the next speaker while the reveal
              waits for its turn, so the human sees the society working. */}
          {reveal.thinking && (
            <div className={styles.thinking} role="status" aria-live="polite">
              <span className={styles.thinkingDot} aria-hidden="true" />
              {t("ad.thinking", { speaker: speakerLabel(t, reveal.thinking.speaker) })}
            </div>
          )}

          <ul className={styles.disputeList} role="list">
            {revealed.map((dispute) => (
              <DisputeCard
                key={dispute.loan_id}
                dispute={dispute}
                pDefault={pDefaultByLoanId.get(dispute.loan_id)}
              />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
