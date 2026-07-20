import { useCallback, useEffect, useMemo, useState } from "react";

import type { DisputeRecord } from "@/types";

/**
 * usePacedReveal — drive a HUMAN-paced, controllable reveal of the agent debate.
 *
 * The live SSE stream can't guarantee readable pacing (round-1 challenges arrive
 * bunched from a parallel audit, and FC/proxy buffering can collapse the rest), so
 * we treat any `DisputeRecord[]` — committed fixture, one-shot Run-live result, or a
 * growing SSE array — purely as an ORDERED data source and reveal it on our own
 * clock. Each turn (a debate round, then the dispute's resolution) appears one at a
 * time so the human watches the society reason, and can pause / step / replay.
 *
 * The reveal cursor is always clamped to the data currently available, so a growing
 * SSE array is revealed no faster than it arrives, and a complete fixture paces all
 * the way through. Honours `prefers-reduced-motion` by revealing everything at once.
 */

export type RevealSpeed = "slow" | "normal";

/** ms between turns. "No need to be fast" — deliberately readable. */
const INTERVAL_MS: Record<RevealSpeed, number> = { slow: 3000, normal: 2000 };

/** One reveal beat: a debate round, or a dispute's terminal resolution. */
type Step = { dispute: number; round: number } | { dispute: number; resolution: true };

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true
  );
}

export interface PacedReveal {
  /** Disputes sliced to the reveal cursor; the active one carries
   *  `pendingResolution` until its outcome beat is reached. */
  revealed: DisputeRecord[];
  /** Index (into `revealed`) of the dispute currently unfolding — drives the
   *  flow-graph's selected branch so it auto-follows the reveal. */
  activeDisputeIndex: number;
  /** The speaker whose turn is next, shown as a "…is thinking" beat. Null when
   *  paused, done, or reduced-motion. */
  thinking: { speaker: string } | null;
  playing: boolean;
  done: boolean;
  atStart: boolean;
  cursor: number;
  totalSteps: number;
  speed: RevealSpeed;
  play: () => void;
  pause: () => void;
  step: () => void;
  replay: () => void;
  setSpeed: (s: RevealSpeed) => void;
}

export function usePacedReveal(
  disputes: DisputeRecord[],
  opts: { autoPlay?: boolean; resetKey?: string } = {},
): PacedReveal {
  const { autoPlay = true, resetKey = "" } = opts;
  const reduced = prefersReducedMotion();

  // Flat reveal sequence: every round, then a resolution beat, per dispute in order.
  const steps = useMemo<Step[]>(() => {
    const s: Step[] = [];
    disputes.forEach((d, di) => {
      d.rounds.forEach((_, ri) => s.push({ dispute: di, round: ri }));
      s.push({ dispute: di, resolution: true });
    });
    return s;
  }, [disputes]);
  const totalSteps = steps.length;

  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(autoPlay);
  const [speed, setSpeed] = useState<RevealSpeed>("normal");

  // A new reveal session (source switch, or the caller bumps resetKey) restarts the
  // cursor. Reduced-motion reveals everything immediately.
  useEffect(() => {
    setCursor(reduced ? Number.MAX_SAFE_INTEGER : 0);
    setPlaying(!reduced && autoPlay);
    // autoPlay is a stable config value; resetKey/reduced are the real triggers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey, reduced]);

  // Never reveal past the data that has actually arrived.
  const clampedCursor = Math.min(cursor, totalSteps);

  // Auto-advance one beat per interval while playing and data remains.
  useEffect(() => {
    if (reduced || !playing || clampedCursor >= totalSteps) return;
    const id = setTimeout(() => setCursor((c) => c + 1), INTERVAL_MS[speed]);
    return () => clearTimeout(id);
  }, [reduced, playing, clampedCursor, totalSteps, speed]);

  const revealed = useMemo<DisputeRecord[]>(() => {
    const out: DisputeRecord[] = [];
    let left = clampedCursor;
    for (let i = 0; i < disputes.length && left > 0; i++) {
      const d = disputes[i];
      const roundsShown = Math.min(left, d.rounds.length);
      left -= roundsShown;
      const resolutionShown = left > 0 && roundsShown === d.rounds.length;
      if (resolutionShown) left -= 1;
      if (roundsShown > 0) {
        out.push({
          ...d,
          rounds: d.rounds.slice(0, roundsShown),
          pendingResolution: !resolutionShown,
        });
      }
    }
    return out;
  }, [disputes, clampedCursor]);

  const activeDisputeIndex = Math.max(0, revealed.length - 1);

  const thinking = useMemo<{ speaker: string } | null>(() => {
    if (reduced || !playing || clampedCursor >= totalSteps) return null;
    const next = steps[clampedCursor];
    if (!next) return null;
    if ("resolution" in next) {
      return { speaker: disputes[next.dispute]?.resolved_by || "arbiter" };
    }
    return { speaker: disputes[next.dispute]?.rounds[next.round]?.speaker || "risk_auditor" };
  }, [reduced, playing, clampedCursor, totalSteps, steps, disputes]);

  const play = useCallback(() => setPlaying(true), []);
  const pause = useCallback(() => setPlaying(false), []);
  const step = useCallback(() => {
    setPlaying(false);
    setCursor((c) => Math.min(c + 1, totalSteps));
  }, [totalSteps]);
  const replay = useCallback(() => {
    setCursor(0);
    setPlaying(true);
  }, []);

  return {
    revealed,
    activeDisputeIndex,
    thinking,
    playing,
    done: totalSteps > 0 && clampedCursor >= totalSteps,
    atStart: clampedCursor === 0,
    cursor: clampedCursor,
    totalSteps,
    speed,
    play,
    pause,
    step,
    replay,
    setSpeed,
  };
}
