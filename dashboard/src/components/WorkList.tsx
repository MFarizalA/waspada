import { useMemo, useState } from "react";
import type { ScoredAccount } from "@/types";
import { segmentLabel } from "@/lib/format";
import { ActionBadge } from "@/components/ActionBadge";
import { BandBadge, ScoreText } from "@/components/BandBadge";
import styles from "./WorkList.module.css";

type SortDir = "asc" | "desc";

interface WorkListProps {
  accounts: ScoredAccount[];
  /** Controlled selection — opens the per-account drawer. */
  onSelectAccount: (account: ScoredAccount) => void;
}

const TOP_N_OPTIONS = [10, 25, 50, 100] as const;

/**
 * The ranked work-list. Sortable by p_default (the primary sort the analyst
 * uses: worst-first). Top-N selector caps the rendered rows — the work-list can
 * be large in production; we render only what's asked for, defaulting to top-10.
 *
 * Default sort is p_default DESC (highest-risk first), matching how the
 * pipeline emits the work_list (WA-006). The sort control lets the analyst flip
 * to ASC to surface the clean tail.
 */
export function WorkList({ accounts, onSelectAccount }: WorkListProps) {
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [topN, setTopN] = useState<(typeof TOP_N_OPTIONS)[number]>(10);

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
              <th scope="col">Action</th>
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
                <td><ActionBadge action={account.recommended_action} /></td>
                <td className={styles.openHint} aria-hidden="true">›</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
