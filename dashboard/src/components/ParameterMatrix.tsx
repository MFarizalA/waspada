import { useState } from "react";

import { apiFetch } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import styles from "./ParameterMatrix.module.css";

/**
 * WA-095 — the human parameter matrix (the governance control surface).
 *
 * The analyst sets the policy the whole society plays by BEFORE a run: the
 * band→action grid, the debate knobs (dispute gap, arbiter confidence, audit K),
 * the work-list cap, and the cohort-alert thresholds. "Run with this matrix"
 * POSTs it to /api/run; the backend validates (a bad matrix → 400), governs the
 * run, and stamps a policy_id so the result is traceable to the exact policy.
 *
 * Client-side validation mirrors the backend guardrails so a nonsensical matrix
 * is caught before the round-trip.
 */
const BANDS = ["Very High", "High", "Medium", "Low", "Very Low"] as const;
const ACTIONS = ["call", "watch", "auto-cure"] as const;

type Matrix = {
  band_to_action: Record<string, string>;
  dispute_gap: number;
  arbiter_confidence: number;
  audit_k: number;
  top_n: number;
  npl_threshold: number;
  vintage_threshold: number;
};

const DEFAULT_MATRIX: Matrix = {
  band_to_action: {
    "Very High": "call", High: "watch", Medium: "watch",
    Low: "auto-cure", "Very Low": "auto-cure",
  },
  dispute_gap: 2,
  arbiter_confidence: 0.6,
  audit_k: 8,
  top_n: 50,
  npl_threshold: 0.2,
  vintage_threshold: 0.15,
};

type RunState =
  | { status: "idle" }
  | { status: "running" }
  | { status: "done"; policyId: string; accounts: number; disputes: number }
  | { status: "error"; message: string };

function clientValidate(m: Matrix): string | null {
  if (m.dispute_gap < 1 || m.dispute_gap > 4) return "Dispute gap must be 1–4.";
  if (m.arbiter_confidence < 0 || m.arbiter_confidence > 1) return "Arbiter confidence must be 0–1.";
  if (m.audit_k < 1) return "Audit K must be ≥ 1.";
  if (m.top_n < 1) return "Work-list cap must be ≥ 1.";
  for (const k of ["npl_threshold", "vintage_threshold"] as const) {
    if (m[k] < 0 || m[k] > 1) return "Alert thresholds must be 0–1.";
  }
  return null;
}

export function ParameterMatrix({ brain = "mock" }: { brain?: string }) {
  const { t } = useI18n();
  const [m, setM] = useState<Matrix>(DEFAULT_MATRIX);
  const [run, setRun] = useState<RunState>({ status: "idle" });
  const [open, setOpen] = useState(false);

  const setAction = (band: string, action: string) =>
    setM((prev) => ({ ...prev, band_to_action: { ...prev.band_to_action, [band]: action } }));
  const setNum = (key: keyof Matrix, value: number) =>
    setM((prev) => ({ ...prev, [key]: value }));

  const submit = async () => {
    const err = clientValidate(m);
    if (err) { setRun({ status: "error", message: err }); return; }
    setRun({ status: "running" });
    try {
      const res = await apiFetch(`/api/run?brain=${encodeURIComponent(brain)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ policy: m }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setRun({ status: "error", message: body?.error ?? `${res.status} ${res.statusText}` });
        return;
      }
      const payload = body.payload ?? body;
      setRun({
        status: "done",
        policyId: payload?.policy_card?.policy_id ?? "—",
        accounts: (payload?.work_list ?? []).length,
        disputes: (payload?.agent_dialogue ?? []).length,
      });
    } catch (e) {
      setRun({ status: "error", message: e instanceof Error ? e.message : String(e) });
    }
  };

  return (
    <section className={styles.panel} aria-labelledby="matrix-heading">
      <header className={styles.head}>
        <button
          type="button"
          className={styles.toggle}
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
        >
          <span className={styles.caret} aria-hidden="true">{open ? "▾" : "▸"}</span>
          <span>
            <span id="matrix-heading" className={styles.title}>{t("pm.title")}</span>
            <span className={styles.subtitle}>{t("pm.subtitle")}</span>
          </span>
        </button>
      </header>

      {open && (
        <div className={styles.body}>
          {/* band → action grid */}
          <div className={styles.grid} role="group" aria-label={t("pm.grid")}>
            {BANDS.map((band) => (
              <div key={band} className={styles.gridRow}>
                <span className={styles.band} data-band={band}>{t(`band.val.${band}`)}</span>
                <div className={styles.actions}>
                  {ACTIONS.map((a) => (
                    <button
                      key={a}
                      type="button"
                      className={styles.actionBtn}
                      data-on={m.band_to_action[band] === a ? "1" : undefined}
                      onClick={() => setAction(band, a)}
                    >
                      {t(`action.${a}`)}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>

          {/* scalar knobs */}
          <div className={styles.knobs}>
            <Knob label={t("pm.disputeGap")} value={m.dispute_gap} min={1} max={4} step={1}
                  onChange={(v) => setNum("dispute_gap", v)} />
            <Knob label={t("pm.arbiterConf")} value={m.arbiter_confidence} min={0} max={1} step={0.05}
                  onChange={(v) => setNum("arbiter_confidence", v)} />
            <Knob label={t("pm.auditK")} value={m.audit_k} min={1} max={50} step={1}
                  onChange={(v) => setNum("audit_k", v)} />
            <Knob label={t("pm.topN")} value={m.top_n} min={1} max={200} step={1}
                  onChange={(v) => setNum("top_n", v)} />
            <Knob label={t("pm.npl")} value={m.npl_threshold} min={0} max={1} step={0.01}
                  onChange={(v) => setNum("npl_threshold", v)} />
            <Knob label={t("pm.vintage")} value={m.vintage_threshold} min={0} max={1} step={0.01}
                  onChange={(v) => setNum("vintage_threshold", v)} />
          </div>

          <div className={styles.foot}>
            <button
              type="button"
              className={styles.run}
              onClick={submit}
              disabled={run.status === "running"}
            >
              {run.status === "running" ? t("pm.running") : t("pm.run")}
            </button>
            {run.status === "done" && (
              <span className={styles.result}>
                {t("pm.applied", { id: run.policyId, accounts: run.accounts, disputes: run.disputes })}
              </span>
            )}
            {run.status === "error" && (
              <span className={styles.error} role="alert">{run.message}</span>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function Knob({
  label, value, min, max, step, onChange,
}: {
  label: string; value: number; min: number; max: number; step: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className={styles.knob}>
      <span className={styles.knobLabel}>{label}</span>
      <input
        className={styles.knobInput}
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}
