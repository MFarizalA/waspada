import type { DisputeRecord } from "@/types";
import { useI18n } from "@/lib/i18n";
import { riskLevelColor, riskLevelLabel } from "@/lib/riskLevel";
import styles from "./Escalations.module.css";

/**
 * G3 — Human Gate surface (UX research: "override at any stage" is a core
 * agentic-UX principle, and our human gate had no UI — escalations only showed
 * as a badge buried in the debate).
 *
 * The Approval Gate decides *inline* during the run, so the payload carries
 * DECIDED escalations (approved / rejected), not a pending queue. This panel
 * therefore surfaces **what the society deferred to the human and how it was
 * ruled** — making the human-in-the-loop visible and auditable. An interactive
 * pending queue (live approve/reject) needs a backend pending-state + endpoint
 * (WA-097 follow-up); this is the read-only half.
 *
 * Renders nothing when no dispute escalated (the common case — the society
 * resolved everything itself), so it appears exactly when the human was needed.
 */
const DECISION: Record<string, { tone: string; key: string }> = {
  escalated_approved: { tone: "var(--pasar-teal-600)", key: "esc.approved" },
  escalated_rejected: { tone: "var(--sev-critical)", key: "esc.rejected" },
};

export function Escalations({
  dialogue,
  onJumpToDebate,
}: {
  dialogue: DisputeRecord[] | undefined;
  onJumpToDebate?: (loanId: string) => void;
}) {
  const { t } = useI18n();
  if (dialogue === undefined) return null;
  const escalations = dialogue.filter((d) => d.resolution.startsWith("escalated"));
  if (escalations.length === 0) return null;

  return (
    <section className={styles.panel} aria-labelledby="escalations-heading">
      <header className={styles.header}>
        <div className={styles.headerText}>
          <h2 id="escalations-heading" className={styles.title}>
            {t("esc.title")}
          </h2>
          <p className={styles.subtitle}>{t("esc.subtitle")}</p>
        </div>
        <span className={styles.count}>
          {t("esc.count", { count: escalations.length })}
        </span>
      </header>
      <ul className={styles.list} role="list">
        {escalations.map((d) => {
          const dec = DECISION[d.resolution] ?? { tone: "var(--text-subtle)", key: d.resolution };
          return (
            <li
              key={d.loan_id}
              className={styles.item}
              style={{ ["--tone" as string]: dec.tone } as React.CSSProperties}
            >
              <div className={styles.itemHead}>
                <span className={styles.loanId}>{d.loan_id}</span>
                <span className={styles.clash}>
                  <strong style={{ color: riskLevelColor(d.model_band) }}>
                    {riskLevelLabel(t, d.model_band)}
                  </strong>
                  <span className={styles.vs} aria-hidden="true">
                    {t("ad.vs")}
                  </span>
                  <strong style={{ color: riskLevelColor(d.auditor_view) }}>
                    {riskLevelLabel(t, d.auditor_view)}
                  </strong>
                </span>
                <span className={styles.decision}>{t(dec.key)}</span>
              </div>
              {d.rationale ? <p className={styles.rationale}>{d.rationale}</p> : null}
              {onJumpToDebate ? (
                <button
                  type="button"
                  className={styles.jump}
                  onClick={() => onJumpToDebate(d.loan_id)}
                >
                  {t("esc.jump")}
                </button>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
