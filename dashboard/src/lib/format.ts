/** Display formatters for the dashboard — locale-aware, cheap-Android friendly. */

const PCT = new Intl.NumberFormat("en-US", {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

/**
 * IDR currency formatter. The portfolio is Indonesian consumer credit, so EL
 * is denominated in rupiah. We use the `en-ID` locale (thousands separators)
 * with the ISO code "IDR" rather than the Rp symbol so it reads unambiguously in
 * both light/dark themes and doesn't depend on a localised glyph rendering.
 */
const IDR = new Intl.NumberFormat("en-ID", {
  style: "currency",
  currency: "IDR",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

/** 45_250_000 → "IDR 45,250,000". Rupiah is whole-rupiah — no fraction digits. */
export function idr(v: number): string {
  return IDR.format(Math.round(v));
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
