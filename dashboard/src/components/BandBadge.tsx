import type { ScoredAccount } from "@/types";
import { score } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { riskLevelColor, riskLevelLabel } from "@/lib/riskLevel";

/**
 * Compact pill showing the risk level (quintile-derived score band).
 * Color-coded via the design-system risk ramp so the analyst can scan severity
 * at a glance. The label localizes (EN/中文) for known levels; an unknown value
 * renders raw so payload drift stays visible instead of crashing.
 */
export function BandBadge({ band }: { band: string }) {
  const { t } = useI18n();
  const label = riskLevelLabel(t, band);
  return (
    <span
      className="badge"
      style={{ background: riskLevelColor(band), color: "#fff" }}
      aria-label={t("band.aria", { band: label })}
    >
      {label}
    </span>
  );
}

/**
 * The band cell for a work-list row (WA-048). Normally just the model's
 * `BandBadge`. When the Agent Society overruled the model — `final_band` present
 * and different from `score_band` — it renders the model band struck through
 * beside the society's final band, so the analyst sees the decision *changed* and
 * why (override reason on hover). This is what makes the debate visible in the
 * work-list instead of only in the transcript. Falls back to the plain badge for
 * pre-WA-048 payloads (no `final_band`).
 */
export function BandCell({ account }: { account: ScoredAccount }) {
  const { t } = useI18n();
  const model = account.score_band;
  const final = account.final_band;
  const overridden = typeof final === "string" && final !== model;

  if (!overridden) return <BandBadge band={model} />;

  const reason = account.override_reason ?? "";
  return (
    <span
      className="bandOverride"
      style={{ display: "inline-flex", alignItems: "center", gap: "0.375rem" }}
      title={t("band.override.title", {
        from: riskLevelLabel(t, model),
        to: riskLevelLabel(t, final),
        reason,
      })}
      aria-label={t("band.override.aria", {
        from: riskLevelLabel(t, model),
        to: riskLevelLabel(t, final),
      })}
    >
      <span
        style={{
          textDecoration: "line-through",
          opacity: 0.55,
          color: riskLevelColor(model),
          fontSize: "0.85em",
        }}
      >
        {riskLevelLabel(t, model)}
      </span>
      <span aria-hidden="true" style={{ opacity: 0.55 }}>→</span>
      <BandBadge band={final} />
    </span>
  );
}

/**
 * The p_default score, shown as a colored number. The number itself carries the
 * precision (two decimals); the color encodes the risk level for fast scanning.
 * Colored by the model's own band — p_default is the model's number and is never
 * rewritten by the debate.
 */
export function ScoreText({ account }: { account: ScoredAccount }) {
  return (
    <span style={{ color: riskLevelColor(account.score_band), fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>
      {score(account.p_default)}
    </span>
  );
}
