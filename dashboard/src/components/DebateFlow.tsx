import { Fragment, useState } from "react";

import type { DisputeRecord, DisputeRound } from "@/types";
import { useI18n, type TFunc } from "@/lib/i18n";
import styles from "./DebateFlow.module.css";

/**
 * WA-091 — a node-graph view of the debate, shown above the transcript.
 *
 * Two layers, both driven by the SAME `DisputeRecord[]` the transcript renders
 * (so it updates live as SSE rounds arrive — no new data source):
 *   1. the **society spine** — the deterministic pipeline every run walks
 *      (Data Engineer → Data Analyst → Actuary → Skeptic → Arbiter → Gate);
 *   2. the selected dispute's **debate branch** — Actuary's band → Skeptic's
 *      challenge → the round-by-round argument → the terminal resolution, with
 *      labeled edges derived from each round's verdict.
 *
 * Read-only (a visualization, not a builder). Hand-rolled SVG/CSS, zero deps,
 * fully token-themed so it re-themes with the palette.
 */

// Per-speaker icon + accent tone (tokens, so it re-themes). Mirrors the
// transcript's speaker colours.
const SPEAKER_META: Record<string, { icon: string; tone: string }> = {
  risk_model: { icon: "📊", tone: "var(--sev-info)" },
  risk_auditor: { icon: "🔎", tone: "var(--sev-high)" },
  arbiter: { icon: "⚖️", tone: "var(--pasar-teal-700)" },
  human: { icon: "👤", tone: "var(--pasar-teal-500)" },
};
const KNOWN_SPEAKERS = new Set(["risk_model", "risk_auditor", "arbiter", "human"]);

// The static society spine (the orchestrator's deterministic order). The three
// debate roles are highlighted — they're where the argument happens.
const SPINE: { key: string; icon: string; debate?: boolean }[] = [
  { key: "data_engineer", icon: "🛠️" },
  { key: "data_analyst", icon: "📈" },
  { key: "risk_model", icon: "📊", debate: true },
  { key: "risk_auditor", icon: "🔎", debate: true },
  { key: "arbiter", icon: "⚖️", debate: true },
  { key: "gate", icon: "🛡️" },
];
const SPINE_LABEL: Record<string, string> = {
  data_engineer: "Data Engineer",
  data_analyst: "Data Analyst",
  gate: "Approval Gate",
};

const RES_TONE: Record<string, string> = {
  upheld: "var(--pasar-teal-600)",
  overridden: "var(--sev-moderate)",
  escalated_approved: "var(--sev-info)",
  escalated_rejected: "var(--sev-critical)",
};

function speakerIcon(sp: string): string {
  return SPEAKER_META[sp]?.icon ?? "•";
}
function speakerTone(sp: string): string {
  return SPEAKER_META[sp]?.tone ?? "var(--text-subtle)";
}
function speakerLabel(t: TFunc, sp: string): string {
  if (KNOWN_SPEAKERS.has(sp)) return t(`speaker.${sp}`);
  return SPINE_LABEL[sp] ?? sp;
}

/** The verdict token an edge carries: the leading UPPERCASE word of the claim
 *  (UPHOLD / CONCEDE / OVERRIDE / ESCALATE), else a role-appropriate default. */
function edgeVerdict(round: DisputeRound): string {
  const m = round.claim?.match(/^\s*([A-Z]{3,})\b/);
  if (m) return m[1].toLowerCase();
  return round.speaker === "risk_auditor" ? "challenges" : "responds";
}

function truncate(s: string, n = 96): string {
  if (!s) return "";
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

function toneStyle(tone: string) {
  return { ["--tone" as string]: tone } as React.CSSProperties;
}

function FlowNode({
  icon, tone, title, badge, body, conf, model,
}: {
  icon: string;
  tone: string;
  title: string;
  badge?: string;
  body?: string;
  conf?: number | null;
  model?: string | null;
}) {
  return (
    <div className={styles.node} style={toneStyle(tone)}>
      <div className={styles.nodeHead}>
        <span className={styles.nodeIcon} aria-hidden="true">{icon}</span>
        <span className={styles.nodeTitle}>{title}</span>
        {badge ? <span className={styles.nodeBadge}>{badge}</span> : null}
      </div>
      {body ? <p className={styles.nodeBody}>{body}</p> : null}
      {conf != null || model ? (
        <div className={styles.nodeMeta}>
          {conf != null ? <span>conf {Math.round(conf * 100)}%</span> : null}
          {model ? <span className={styles.nodeModel}>{model}</span> : null}
        </div>
      ) : null}
    </div>
  );
}

function Edge({ label, sub }: { label: string; sub?: string }) {
  return (
    <div className={styles.edge} aria-hidden="true">
      <span className={styles.edgeLine} />
      <span className={styles.edgePill}>
        {label}
        {sub ? <em className={styles.edgeSub}>{sub}</em> : null}
      </span>
      <span className={styles.edgeLine} />
    </div>
  );
}

export function DebateFlow({ disputes }: { disputes: DisputeRecord[] }) {
  const { t } = useI18n();
  const [sel, setSel] = useState(0);
  if (!disputes || disputes.length === 0) return null;

  const idx = Math.min(sel, disputes.length - 1);
  const d = disputes[idx];
  const lastRound = d.rounds[d.rounds.length - 1];

  return (
    <div className={styles.flow}>
      {/* Layer 1 — the agent society spine (always shown). */}
      <div className={styles.spine} role="img" aria-label="Agent society pipeline">
        {SPINE.map((n, i) => (
          <Fragment key={n.key}>
            <span className={styles.spineNode} data-debate={n.debate ? "1" : undefined}>
              <span className={styles.spineIcon} aria-hidden="true">{n.icon}</span>
              {speakerLabel(t, n.key)}
            </span>
            {i < SPINE.length - 1 ? (
              <span className={styles.spineArrow} aria-hidden="true">›</span>
            ) : null}
          </Fragment>
        ))}
      </div>

      {/* Dispute selector when more than one account is contested. */}
      {disputes.length > 1 ? (
        <div className={styles.tabs} role="tablist" aria-label="Contested accounts">
          {disputes.map((dd, i) => (
            <button
              key={dd.loan_id}
              type="button"
              role="tab"
              aria-selected={i === idx}
              className={styles.tab}
              data-on={i === idx ? "1" : undefined}
              onClick={() => setSel(i)}
            >
              {dd.loan_id}
            </button>
          ))}
        </div>
      ) : null}

      {/* Layer 2 — the selected dispute's debate branch. */}
      <div className={styles.branch}>
        <FlowNode
          icon="📊"
          tone="var(--sev-info)"
          title={speakerLabel(t, "risk_model")}
          badge={d.model_band}
          body={`Scored account ${d.loan_id} as ${d.model_band}.`}
        />
        <Edge label="opens dispute" sub={`${speakerLabel(t, "risk_auditor")}: ${d.auditor_view}`} />
        {d.rounds.map((r, i) => (
          <Fragment key={r.round_no}>
            <FlowNode
              icon={speakerIcon(r.speaker)}
              tone={speakerTone(r.speaker)}
              title={speakerLabel(t, r.speaker)}
              badge={`R${r.round_no}`}
              body={truncate(r.claim)}
              conf={r.confidence}
              model={r.model}
            />
            {i < d.rounds.length - 1 ? <Edge label={edgeVerdict(r)} /> : null}
          </Fragment>
        ))}
        <Edge label={lastRound ? edgeVerdict(lastRound) : "resolves"} />
        <div
          className={styles.resNode}
          style={toneStyle(RES_TONE[d.resolution] ?? "var(--text-subtle)")}
        >
          <span className={styles.resIcon} aria-hidden="true">🏁</span>
          <div>
            <div className={styles.resTitle}>{t(`res.${d.resolution}`)}</div>
            <div className={styles.resMeta}>
              {t("ad.resolvedBy", { resolver: speakerLabel(t, d.resolved_by) })}
              {d.revised_band ? ` · → ${d.revised_band}` : ""}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
