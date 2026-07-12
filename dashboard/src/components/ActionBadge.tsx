import type { ScoredAccount } from "@/types";
import { useI18n } from "@/lib/i18n";

type Action = ScoredAccount["recommended_action"];

const STYLES: Record<Action, { bg: string; fg: string }> = {
  "call":      { bg: "var(--action-call-bg)",      fg: "var(--action-call-fg)" },
  "watch":     { bg: "var(--action-watch-bg)",     fg: "var(--action-watch-fg)" },
  "auto-cure": { bg: "var(--action-autocure-bg)",  fg: "var(--action-autocure-fg)" },
};

/**
 * Recommended-action badge. Three discrete actions per the frozen contract
 * ("call" | "watch" | "auto-cure"). The "call" badge is the highest-urgency —
 * solid red, high-contrast — so the work-list reads priority-first. Label is
 * localized (EN/中文) via the i18n dictionary; the action key stays the frozen
 * contract value.
 */
export function ActionBadge({ action }: { action: Action }) {
  const { t } = useI18n();
  const s = STYLES[action];
  const label = t(`action.${action}`);
  return (
    <span
      className="badge"
      style={{ background: s.bg, color: s.fg }}
      aria-label={t("action.aria", { label })}
    >
      {label}
    </span>
  );
}
