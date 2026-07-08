import { useCallback, useEffect, useRef, useState } from "react";

import { isDashboardPayload, type DashboardPayload } from "@/types";
import { apiFetch } from "@/lib/auth";

/**
 * Live run hook — drives the "Run live (Qwen)" action on the Agent Society
 * panel. POSTs to `/api/run?brain=qwen` (api/main.py), which is a Bearer-gated
 * route (Depends(current_user)). `apiFetch` attaches the token from the auth
 * context and, on a 401, clears the session — which returns the user to the
 * login screen via the App gate. The response envelope is
 * `{ payload, report, alert_summary, steps }`; only the frozen
 * `DashboardPayload` is consumed here.
 *
 * The panel renders the committed fixture by default; this hook swaps in the
 * live `agent_dialogue` (and live payload) on success, surfaces a message on
 * error/timeout, and never leaves the user on a bare spinner. The frozen
 * contract (types.ts) is the validation gate — a live response that fails the
 * `isDashboardPayload` guard is surfaced as an error, not rendered half-formed.
 */
export type LiveRunState =
  | { status: "idle" }
  | { status: "running" }
  | { status: "ok"; payload: DashboardPayload }
  | { status: "error"; message: string };

interface UseLiveRunResult {
  state: LiveRunState;
  /** Kick off POST /api/run?brain=qwen. No-op while a run is already in flight. */
  run: () => void;
  /** Drop the live result, return to the fixture view. */
  reset: () => void;
}

/** Abort + timeout for a live run. Qwen reasoning can take ~15-60s. */
const RUN_TIMEOUT_MS = 120_000;
const RUN_URL = "/api/run?brain=qwen";

export function useLiveRun(): UseLiveRunResult {
  const [state, setState] = useState<LiveRunState>({ status: "idle" });
  // Track the in-flight AbortController so React strict-mode double-invoke and
  // unmount both cancel cleanly instead of racing into setState on an unmounted
  // component.
  const inflight = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => inflight.current?.abort();
  }, []);

  const run = useCallback(() => {
    if (inflight.current) return; // guard double-clicks

    const controller = new AbortController();
    inflight.current = controller;
    setState({ status: "running" });

    // Race the fetch against a timeout so a hung backend doesn't freeze the UI.
    const timer = setTimeout(() => controller.abort(), RUN_TIMEOUT_MS);

    apiFetch(RUN_URL, {
      method: "POST",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) {
          // Backend emits `{ "error": "..." }` on 500 (api/main.py L116-119).
          let detail = `${res.status} ${res.statusText}`;
          try {
            const body = (await res.json()) as { error?: string };
            if (body?.error) detail = body.error;
          } catch {
            /* keep the status-text fallback */
          }
          throw new Error(detail);
        }
        return res.json() as Promise<unknown>;
      })
      .then((raw: unknown) => {
        // /api/run wraps the payload: { payload, report, alert_summary, steps }.
        const env = raw as { payload?: unknown };
        const payload = env?.payload;
        if (!isDashboardPayload(payload)) {
          throw new Error(
            "Live response did not match the dashboard payload shape.",
          );
        }
        if (!controller.signal.aborted) setState({ status: "ok", payload });
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) {
          // Distinguish our timeout-abort from a real network error.
          setState({
            status: "error",
            message: `Live run timed out after ${Math.round(
              RUN_TIMEOUT_MS / 1000,
            )}s. Try again, or view the fixture debate below.`,
          });
          return;
        }
        const message =
          err instanceof Error && err.message
            ? err.message
            : "Network error — couldn't reach /api/run.";
        setState({ status: "error", message });
      })
      .finally(() => {
        clearTimeout(timer);
        if (inflight.current === controller) inflight.current = null;
      });
  }, []);

  const reset = useCallback(() => {
    inflight.current?.abort();
    inflight.current = null;
    setState({ status: "idle" });
  }, []);

  return { state, run, reset };
}
