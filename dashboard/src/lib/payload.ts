import { isDashboardPayload, type DashboardPayload } from "@/types";

/**
 * Load the dashboard payload. For the demo we read a committed fixture JSON so
 * the dashboard runs with no live backend (WA-011 acceptance).
 *
 * Real-run mode: set `VITE_PAYLOAD_URL` to point at the orchestrator output path
 * (WA-006) and this loader will fetch it instead. The shape is identical — the
 * contract is frozen.
 */
export async function loadPayload(): Promise<DashboardPayload> {
  const url = import.meta.env.VITE_PAYLOAD_URL ?? defaultFixtureUrl();
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    throw new Error(`Failed to load payload (${res.status} ${res.statusText}) from ${url}`);
  }
  const data: unknown = await res.json();
  if (!isDashboardPayload(data)) {
    throw new Error("Payload does not match DashboardPayload shape (work_list/portfolio_health/alerts).");
  }
  return data;
}

/** Resolve the committed fixture URL relative to the app base. */
function defaultFixtureUrl(): string {
  // Vite serves files in the project root; the fixture lives at /fixtures/.
  return `${import.meta.env.BASE_URL}fixtures/sample-payload.json`;
}
