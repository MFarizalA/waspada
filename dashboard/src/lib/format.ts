/** Display formatters for the dashboard — locale-aware, cheap-Android friendly. */

const PCT = new Intl.NumberFormat("en-US", {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

/**
 * USD currency formatter. The book is the real Lending Club portfolio (US
 * consumer credit), so Expected Loss is denominated in dollars. `en-US` locale
 * with ISO code "USD"; whole-dollar (no cents) since EL is an estimate, not a
 * ledger figure.
 */
const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

/** 45_250 → "$45,250". Whole-dollar — EL is an estimate, cents are noise. */
export function usd(v: number): string {
  return USD.format(Math.round(v));
}

/** 0.122 → "12.2%". Treats the input as a fraction in [0,1]. */
export function pct(v: number): string {
  return PCT.format(v);
}

/** 0.91 → "0.91" — p_default shown raw so the analyst sees the score, not a rounded %. */
export function score(v: number): string {
  return v.toFixed(2);
}

/** "npl_ratio" → "NPL Ratio" — humanize a snake_case metric key for display. */
export function humanizeMetric(key: string): string {
  if (key === "npl_ratio") return "NPL Ratio";
  if (key === "vintage_default_rate") return "Vintage Default Rate";
  return key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** {product, region} → "installment · TX", or "Portfolio-wide" when null. */
export function segmentLabel(seg: { product: string; region: string } | null): string {
  if (!seg) return "Portfolio-wide";
  return `${seg.product} · ${seg.region}`;
}

/** Sort vintage cohorts chronologically (string keys like "2019".."2023"). */
export function sortCohorts(rates: Record<string, number>): Array<[string, number]> {
  return Object.entries(rates).sort(([a], [b]) => a.localeCompare(b));
}
