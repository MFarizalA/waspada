import type { ScoredAccount } from "@/types";

type Action = ScoredAccount["recommended_action"];

const STYLES: Record<Action, { bg: string; fg: string; label: string }> = {
  "call":      { bg: "var(--action-call-bg)",      fg: "var(--action-call-fg)",      label: "Call" },
  "watch":     { bg: "var(--action-watch-bg)",     fg: "var(--action-watch-fg)",     label: "Watch" },
  "auto-cure": { bg: "var(--action-autocure-bg)",  fg: "var(--action-autocure-fg)",  label: "Auto-cure" },
};

/**
 * Recommended-action badge. Three discrete actions per the frozen contract
 * ("call" | "watch" | "auto-cure"). The "call" badge is the highest-urgency —
 * solid red, high-contrast — so the work-list reads priority-first.
 */
export function ActionBadge({ action }: { action: Action }) {
  const s = STYLES[action];
  return (
    <span
      className="badge"
      style={{ background: s.bg, color: s.fg }}
      aria-label={`Recommended action: ${s.label}`}
    >
      {s.label}
    </span>
  );
}
