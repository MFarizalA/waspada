import { useCallback, useEffect, useRef, useState } from "react";

import type { DisputeRecord, DisputeRound } from "@/types";
import { getAuthToken } from "@/lib/auth";

/**
 * Live debate stream (WA-022) — the streaming companion to `useLiveRun`.
 *
 * Where `useLiveRun` POSTs to `/api/run?brain=qwen` and waits for the whole
 * debate to come back as one JSON envelope, this hook opens an SSE
 * `EventSource` on `/api/run/stream?brain=qwen` and appends rounds to the
 * AgentDialogue panel as they arrive — so judges watch the Risk Auditor,
 * Actuary, and Credit Arbiter argue in real time, not 30s later.
 *
 * The backend endpoint is specified (GET /api/run/stream?brain=qwen, SSE) but
 * not yet implemented. This hook is built against the frozen event shape from
 * the WA-022 brief so it's ready to consume the real stream the moment Backend
 * ships it:
 *
 *   data: {"type":"round","round_no":1,"speaker":"risk_auditor",
 *          "model":"qwen3.6-flash","claim":"...","confidence":0.72,
 *          "evidence":["..."]}
 *   data: {"type":"resolution","loan_id":"LN00961668","resolution":"upheld",
 *          "resolved_by":"arbiter","rationale":"..."}
 *   data: {"type":"done"}
 *
 * Mapping: `round` events accumulate into a single in-progress DisputeRecord;
 * `resolution` closes it (attaches the verdict + rationale) and the next round
 * opens a fresh dispute. `done` ends the stream. Rounds before any resolution
 * land in a synthetic "open" dispute keyed by the stream — same shape the panel
 * already renders, so no new card markup is needed.
 *
 * Auth: EventSource can't set Authorization headers, so the Bearer rides as a
 * `?token=` query param (getAuthToken). The backend is expected to accept that
 * fallback for the stream route only.
 *
 * Graceful degradation: if EventSource is unavailable (SSR / very old browser)
 * or fires `onerror` before any data arrived, the hook reports `error` so the
 * panel can fall back to the existing "Run live" request/response button.
 */

const STREAM_PATH = "/api/run/stream";
const OPEN_LOAN_ID = "LIVE";

/** A streaming round event — structurally identical to a stored DisputeRound. */
interface StreamRoundEvent {
  type: "round";
  round_no: number;
  speaker: string;
  model: string | null;
  claim: string;
  confidence: number | null;
  evidence: string[];
}

/** A streaming resolution event — closes the current dispute. */
interface StreamResolutionEvent {
  type: "resolution";
  loan_id: string;
  resolution: DisputeRecord["resolution"];
  resolved_by: string;
  rationale: string;
}

type StreamEvent = StreamRoundEvent | StreamResolutionEvent | { type: "done" };

/** State machine for the stream. `connecting` shows the debating spinner until
 *  the first round arrives; `live` shows rounds as they stream in. */
export type LiveStreamState =
  | { status: "idle" }
  | { status: "connecting" }
  | { status: "live"; disputes: DisputeRecord[]; done: boolean }
  | { status: "error"; message: string };

interface UseLiveDebateStreamResult {
  state: LiveStreamState;
  /** Open the SSE stream. No-op if already connecting/live. */
  start: () => void;
  /** Close the stream and return to the fixture view. */
  stop: () => void;
}

/** Type-narrow a raw JSON object into one of the three known event shapes.
 *  Anything malformed is dropped (returns null) so one bad chunk can't crash
 *  the whole stream — the next valid event still renders. */
function parseEvent(data: string): StreamEvent | null {
  let obj: unknown;
  try {
    obj = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof obj !== "object" || obj === null) return null;
  const ev = obj as { type?: unknown };
  if (ev.type === "done") return { type: "done" };
  if (ev.type === "round") {
    const r = obj as Partial<StreamRoundEvent>;
    if (
      typeof r.round_no === "number" &&
      typeof r.speaker === "string" &&
      typeof r.claim === "string"
    ) {
      return {
        type: "round",
        round_no: r.round_no,
        speaker: r.speaker,
        model: r.model ?? null,
        claim: r.claim,
        confidence: r.confidence ?? null,
        evidence: Array.isArray(r.evidence) ? r.evidence : [],
      };
    }
    return null;
  }
  if (ev.type === "resolution") {
    const res = obj as Partial<StreamResolutionEvent>;
    if (
      typeof res.loan_id === "string" &&
      typeof res.resolution === "string" &&
      typeof res.resolved_by === "string"
    ) {
      return {
        type: "resolution",
        loan_id: res.loan_id,
        resolution: res.resolution as DisputeRecord["resolution"],
        resolved_by: res.resolved_by,
        rationale: typeof res.rationale === "string" ? res.rationale : "",
      };
    }
    return null;
  }
  return null;
}

export function useLiveDebateStream(): UseLiveDebateStreamResult {
  const [state, setState] = useState<LiveStreamState>({ status: "idle" });
  const sourceRef = useRef<EventSource | null>(null);

  // Close on unmount / strict-mode double-invoke teardown.
  useEffect(() => {
    return () => {
      sourceRef.current?.close();
      sourceRef.current = null;
    };
  }, []);

  const stop = useCallback(() => {
    sourceRef.current?.close();
    sourceRef.current = null;
    setState({ status: "idle" });
  }, []);

  const start = useCallback(() => {
    // Guard against duplicate streams (double-click, React strict mode).
    if (sourceRef.current) return;

    // EventSource is a browser API — bail if it's missing (SSR / old runtime)
    // and let the panel fall back to the request/response path.
    if (typeof EventSource === "undefined") {
      setState({
        status: "error",
        message: "This browser can't open a live stream — use Run live instead.",
      });
      return;
    }

    setState({ status: "connecting" });

    // EventSource can't set headers; carry the Bearer as a query param. The
    // backend stream route is expected to accept this fallback.
    const token = getAuthToken();
    const url = token
      ? `${STREAM_PATH}?brain=qwen&token=${encodeURIComponent(token)}`
      : `${STREAM_PATH}?brain=qwen`;

    const es = new EventSource(url);
    sourceRef.current = es;

    es.onmessage = (msg) => {
      const ev = parseEvent(msg.data);
      if (!ev) return; // drop malformed; keep the stream alive for the next event

      if (ev.type === "round") {
        const round: DisputeRound = {
          round_no: ev.round_no,
          speaker: ev.speaker,
          model: ev.model,
          claim: ev.claim,
          confidence: ev.confidence,
          evidence: ev.evidence,
        };
        setState((prev) => {
          const disputes =
            prev.status === "live" || prev.status === "connecting"
              ? prev.status === "live"
                ? [...prev.disputes]
                : []
              : [];
          const done = prev.status === "live" ? prev.done : false;
          const last = disputes[disputes.length - 1];
          if (last && last.loan_id === OPEN_LOAN_ID) {
            // Still arguing an open dispute — append the turn.
            last.rounds = [...last.rounds, round];
          } else {
            // First turn of a new dispute. Most of the metadata (opened_by,
            // model_band, auditor_view) isn't streamed yet — the backend only
            // sends the verdict at `resolution`. Render with neutral defaults
            // until the resolution event fills them in; the card stays usable
            // because the round card is the load-bearing UI.
            disputes.push({
              loan_id: OPEN_LOAN_ID,
              opened_by: round.speaker,
              model_band: "—",
              auditor_view: "—",
              rounds: [round],
              resolution: "upheld", // placeholder; replaced on resolution event
              resolved_by: round.speaker,
              rationale: "Debate in progress…",
            });
          }
          return { status: "live", disputes, done };
        });
      } else if (ev.type === "resolution") {
        setState((prev) => {
          if (prev.status !== "live") return prev;
          const disputes = [...prev.disputes];
          const last = disputes[disputes.length - 1];
          if (last && last.loan_id === OPEN_LOAN_ID) {
            // Close the in-progress dispute with the real verdict.
            disputes[disputes.length - 1] = {
              ...last,
              loan_id: ev.loan_id,
              resolution: ev.resolution,
              resolved_by: ev.resolved_by,
              rationale: ev.rationale,
            };
          }
          return { status: "live", disputes, done: prev.done };
        });
      } else if (ev.type === "done") {
        setState((prev) =>
          prev.status === "live"
            ? { status: "live", disputes: prev.disputes, done: true }
            : prev,
        );
        es.close();
        if (sourceRef.current === es) sourceRef.current = null;
      }
    };

    // Fire on initial connection failure AND on mid-stream disconnect. The
    // readyState distinguishes the two: CONNECTING (0) = never got the first
    // byte → treat as a hard error and fall back; OPEN (1) / CLOSED (2) = the
    // stream was working, so the browser's auto-reconnect will likely recover
    // — stay quiet and let it retry rather than flashing an error.
    es.onerror = () => {
      if (es.readyState === EventSource.CONNECTING) {
        setState((prev) => {
          // Only error if we never received anything — a stream that delivered
          // some rounds then dropped is left showing what it got.
          if (prev.status === "connecting") {
            return {
              status: "error",
              message:
                "Couldn't open the live stream — the backend may be offline. Try Run live instead.",
            };
          }
          return prev;
        });
        es.close();
        if (sourceRef.current === es) sourceRef.current = null;
      }
      // else: mid-stream hiccup — EventSource auto-reconnects; leave it be.
    };
  }, []);

  return { state, start, stop };
}
