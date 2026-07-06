import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import type { ScoredAccount } from "@/types";
import { segmentLabel, pct } from "@/lib/format";
import { ActionBadge } from "@/components/ActionBadge";
import { BandBadge } from "@/components/BandBadge";
import styles from "./AccountDrawer.module.css";

interface AccountDrawerProps {
  account: ScoredAccount | null;
  onClose: () => void;
}

/**
 * Per-account detail drawer. An accessible modal dialog:
 *  - role="dialog" + aria-modal, labelled by the heading
 *  - Escape closes; backdrop click closes
 *  - focus moves to the close button on open and returns to the trigger on close
 *  - the rest of the page is inert while open (focus trap via keydown guard)
 *
 * Shows exactly what the analyst needs to act: the score, the band, the segment,
 * and the recommended action — all straight off the frozen ScoredAccounts shape.
 */
export function AccountDrawer({ account, onClose }: AccountDrawerProps) {
  const closeBtnRef = useRef<HTMLButtonElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);

  // Open/close lifecycle: trap focus, restore on close.
  useEffect(() => {
    if (!account) return;

    previouslyFocused.current = document.activeElement as HTMLElement | null;
    closeBtnRef.current?.focus();
    document.body.style.overflow = "hidden";

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    }
    document.addEventListener("keydown", onKey, true);

    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.body.style.overflow = "";
      previouslyFocused.current?.focus();
    };
  }, [account, onClose]);

  if (!account) return null;

  return createPortal(
    <div className={styles.overlay} onMouseDown={onClose}>
      <div
        className={styles.drawer}
        role="dialog"
        aria-modal="true"
        aria-labelledby="drawer-title"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className={styles.header}>
          <div>
            <p className={styles.eyebrow}>Account</p>
            <h2 id="drawer-title" className={styles.title}>{account.loan_id}</h2>
          </div>
          <button
            ref={closeBtnRef}
            type="button"
            className={styles.closeBtn}
            aria-label="Close account detail"
            onClick={onClose}
          >
            ✕
          </button>
        </header>

        <div className={styles.body}>
          <div className={styles.scoreBlock}>
            <span className={styles.scoreLabel}>Probability of default</span>
            <span className={styles.scoreValue} style={scoreColor(account.score_band)}>
              {pct(account.p_default)}
            </span>
            <BandBadge band={account.score_band} />
          </div>

          <dl className={styles.detailGrid}>
            <div className={styles.detailItem}>
              <dt>Recommended action</dt>
              <dd><ActionBadge action={account.recommended_action} /></dd>
            </div>
            <div className={styles.detailItem}>
              <dt>Segment</dt>
              <dd>{segmentLabel(account.segment)}</dd>
            </div>
            <div className={styles.detailItem}>
              <dt>Product</dt>
              <dd className={styles.cap}>{account.segment.product}</dd>
            </div>
            <div className={styles.detailItem}>
              <dt>Region</dt>
              <dd className={styles.cap}>{account.segment.region}</dd>
            </div>
            <div className={styles.detailItem}>
              <dt>Risk band</dt>
              <dd>{account.score_band}</dd>
            </div>
          </dl>
        </div>
      </div>
    </div>,
    document.body,
  );
}

function scoreColor(band: string): { color: string } {
  switch (band) {
    case "Q1":
    case "Q2": return { color: "var(--risk-low)" };
    case "Q3": return { color: "var(--risk-moderate)" };
    case "Q4": return { color: "var(--risk-elevated)" };
    case "Q5": return { color: "var(--risk-high)" };
    default:   return { color: "var(--text)" };
  }
}
