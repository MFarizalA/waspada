import { useMemo, useState } from "react";
import type { ScoredAccount } from "@/types";
import { segmentLabel, idr } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { ActionBadge } from "@/components/ActionBadge";
import { BandCell, ScoreText } from "@/components/BandBadge";
import styles from "./WorkList.module.css";

type SortDir = "asc" | "desc";

interface WorkListProps {
  accounts: ScoredAccount[];
  /** Loan_ids that the Agent Society disputed (agent_dialogue). Marked "contested". */
  contestedLoanIds?: Set<string>;
  /** Controlled selection — opens the per-account drawer. */
  onSelectAccount: (account: ScoredAccount) => void;
  /** Jump to the matching debate card in AgentDialogue (scrolls into view). */
  onJumpToDebate?: (loanId: string) => void;
}

const TOP_N_OPTIONS = [10, 25, 50, 100] as const;

/**
 * The ranked work-list. Sortable by p_default (the primary sort the analyst
 * uses: worst-first). Top-N selector caps the rendered rows — the work-list can
 * be large in production; we render only what's asked for, defaulting to top-25
 * so the Agent Society's contested rows surface alongside the standard ranking.
 *
 * Default sort is p_default DESC (highest-risk first), matching how the
 * pipeline emits the work_list (WA-006). The sort control lets the analyst flip
 * to ASC to surface the clean tail.
 *
 * Rows whose loan_id the Agent Society disputed are flagged "contested" — a pill
 * that, when present, jumps to the matching debate card.
 *
 * WA-048 closed the loop: when the society overruled the model, `recommended_action`
 * and `final_band` already reflect the ruling (the backend adjudicates before the
 * payload is built), and the band cell (`BandCell`) shows the model→final
 * transition. An overridden row therefore no longer reads as a plain collector
 * call — the change is in the data, not just a marker.
 */
export function WorkList({ accounts, contestedLoanIds, onSelectAccount, onJumpToDebate }: WorkListProps) {
  const { t } = useI18n();
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [topN, setTopN] = useState<(typeof TOP_N_OPTIONS)[number]>(25);
  const showContested = contestedLoanIds != null && contestedLoanIds.size > 0;
  // WA-024: the EL column appears only when the payload carries expected_loss.
  // Additive optional key — older payloads without it stay valid (graceful absence).
  const showEl = accounts.some((a) => typeof a.expected_loss === "number");

  const sorted = useMemo(() => {
    const copy = [...accounts];
    copy.sort((a, b) =>
      sortDir === "desc" ? b.p_default - a.p_default : a.p_default - b.p_default,
    );
    return copy.slice(0, topN);
  }, [accounts, sortDir, topN]);

  function toggleSort() {
    setSortDir((d) => (d === "desc" ? "asc" : "desc"));
  }

  return (
    <section className={styles.workList} aria-labelledby="worklist-heading">
      <header className={styles.header}>
        <div>
          <h2 id="worklist-heading" className={styles.title}>{t("wl.title")}</h2>
          <p className={styles.subtitle}>
            {t("wl.showing", { shown: sorted.length, total: accounts.length })}
          </p>
        </div>

        <div className={styles.controls}>
          <label className={styles.topNLabel}>
            {t("wl.top")}
            <select
              className={styles.topNSelect}
              value={topN}
              onChange={(e) => setTopN(Number(e.target.value) as typeof topN)}
              aria-label={t("wl.showCount")}
            >
              {TOP_N_OPTIONS.map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </label>
        </div>
      </header>

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <caption className="visually-hidden">{t("wl.caption")}</caption>
          <thead>
            <tr>
              <th scope="col" className={styles.colRank}>{t("wl.col.rank")}</th>
              <th scope="col" className={styles.colLoan}>{t("wl.col.loan")}</th>
              <th scope="col" className={styles.colSegment}>{t("wl.col.segment")}</th>
              <th scope="col">
                <button
                  type="button"
                  className={styles.sortBtn}
                  onClick={toggleSort}
                  aria-sort={sortDir === "desc" ? "descending" : "ascending"}
                >
                  {t("wl.col.pdefault")}
                  <span className={styles.sortIcon} aria-hidden="true">
                    {sortDir === "desc" ? "▾" : "▴"}
                  </span>
                </button>
              </th>
              <th scope="col" className={styles.colBand}>{t("wl.col.band")}</th>
              {showEl && (
                <th scope="col" className={styles.colEl}>{t("wl.col.el")}</th>
              )}
              <th scope="col">{t("wl.col.action")}</th>
              {showContested && (
                <th scope="col" className={styles.colContested}>
                  <span className="visually-hidden">Agent Society contest</span>
                </th>
              )}
              <th scope="col" className={styles.colOpen}><span className="visually-hidden">Open detail</span></th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((account, i) => (
              <tr
                key={account.loan_id}
                className={styles.row}
                tabIndex={0}
                onClick={() => onSelectAccount(account)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelectAccount(account);
                  }
                }}
              >
                <td className={styles.rank}>{i + 1}</td>
                <td className={styles.loanId}>{account.loan_id}</td>
                <td className={styles.segment}>{segmentLabel(account.segment)}</td>
                <td className={styles.score}><ScoreText account={account} /></td>
                <td className={styles.band}>
                  <BandCell account={account} />
                  {account.top_driver && (
                    <span
                      className={styles.driver}
                      title={t("wl.driver.title")}
                    >
                      {account.top_driver}
                    </span>
                  )}
                </td>
                {showEl && (
                  <td className={styles.el}>
                    {typeof account.expected_loss === "number"
                      ? idr(account.expected_loss)
                      : <span className={styles.elMissing}>—</span>}
                  </td>
                )}
                <td><ActionBadge action={account.recommended_action} /></td>
                {showContested && (
                  <td className={styles.colContested}>
                    {contestedLoanIds!.has(account.loan_id) && (
                      <button
                        type="button"
                        className={styles.contestedPill}
                        title={t("wl.contested.title")}
                        aria-label={t("wl.contested.aria", { id: account.loan_id })}
                        onClick={(e) => {
                          e.stopPropagation();
                          onJumpToDebate?.(account.loan_id);
                        }}
                      >
                        {t("wl.contested")}
                      </button>
                    )}
                  </td>
                )}
                <td className={styles.openHint} aria-hidden="true">›</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {showEl && (
        <p className={styles.assumptions}>
          <span className={styles.assumptionsLabel}>{t("wl.assumptions.label")}</span>{" "}
          {t("wl.assumptions.body")}
        </p>
      )}
    </section>
  );
}
