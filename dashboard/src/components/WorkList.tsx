import { useMemo, useState } from "react";
import type { ScoredAccount } from "@/types";
import { segmentLabel, idr } from "@/lib/format";
import { ActionBadge } from "@/components/ActionBadge";
import { BandBadge, ScoreText } from "@/components/BandBadge";
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
 * that, when present, jumps to the matching debate card. The contest marker
 * makes the audit/review outcome visible at a glance (e.g. an "Overridden" row
 * no longer reads as a plain collector call). NOTE: the row's
 * `recommended_action` itself is the frozen backend field (WA-006); reflecting
 * the debate's resolved action in the work-list is a backend change (WA-014).
 */
export function WorkList({ accounts, contestedLoanIds, onSelectAccount, onJumpToDebate }: WorkListProps) {
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
          <h2 id="worklist-heading" className={styles.title}>Work list</h2>
          <p className={styles.subtitle}>
            Showing <strong>{sorted.length}</strong> of {accounts.length} ranked accounts
          </p>
        </div>

        <div className={styles.controls}>
          <label className={styles.topNLabel}>
            Top
            <select
              className={styles.topNSelect}
              value={topN}
              onChange={(e) => setTopN(Number(e.target.value) as typeof topN)}
              aria-label="Number of accounts to show"
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
          <caption className="visually-hidden">
            Ranked collections work-list, sortable by probability of default.
            Select a row to view account detail.
          </caption>
          <thead>
            <tr>
              <th scope="col" className={styles.colRank}>#</th>
              <th scope="col" className={styles.colLoan}>Loan</th>
              <th scope="col" className={styles.colSegment}>Segment</th>
              <th scope="col">
                <button
                  type="button"
                  className={styles.sortBtn}
                  onClick={toggleSort}
                  aria-sort={sortDir === "desc" ? "descending" : "ascending"}
                >
                  P(default)
                  <span className={styles.sortIcon} aria-hidden="true">
                    {sortDir === "desc" ? "▾" : "▴"}
                  </span>
                </button>
              </th>
              <th scope="col" className={styles.colBand}>Band</th>
              {showEl && (
                <th scope="col" className={styles.colEl}>Exp. loss</th>
              )}
              <th scope="col">Action</th>
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
                <td className={styles.band}><BandBadge band={account.score_band} /></td>
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
                        title="This account was contested in the Agent Society debate"
                        aria-label={`Account ${account.loan_id} was contested — jump to debate`}
                        onClick={(e) => {
                          e.stopPropagation();
                          onJumpToDebate?.(account.loan_id);
                        }}
                      >
                        ⚖ contested
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
          <span className={styles.assumptionsLabel}>Expected loss assumptions:</span>{" "}
          LGD&nbsp;=&nbsp;45% (Basel foundation-IRB, unsecured consumer).
          EAD&nbsp;=&nbsp;outstanding_principal (amortizing installment).
          EL&nbsp;=&nbsp;PD&nbsp;×&nbsp;LGD&nbsp;×&nbsp;EAD.
        </p>
      )}
    </section>
  );
}
