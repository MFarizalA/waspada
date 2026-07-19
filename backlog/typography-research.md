---
id: typography-research
state: research
owner: Bimo (brand decision) · researched by Claude 2026-07-20
feeds: tokens.css (--font-sans/--font-display/--font-mono), branding-research.md Direction A
---

# WASPADA · Typography research — finance × tech, "safety + progress"

## 1. What the industry actually uses
- **The fintech default:** **Inter** and **IBM Plex Sans** dominate — excellent tabular figures,
  clear numerals, professional tone. Inter reads lighter/more modern; Plex feels more
  established/corporate. Roboto and Source Sans are the older safe picks.
- **The counter-trend (2026):** distinction. Wise built a custom "tilted" face precisely to stand
  out from "a sea of Inter and Roboto"; bold high-contrast type is the emerging fintech look.
  Lesson: the *system* face can be common, but the *brand moment* (wordmark, hero) wants character.
- **Trust psychology:** serifs read heritage/authority (banks, +trust in studies); sans reads
  modern/approachable. **Geometric** sans (Futura-like: perfect circles) signals precision +
  forward motion — good for headlines, weaker for long text. **Grotesque/neo-grotesque**
  (Helvetica/Inter-like) signals no-nonsense reliability — the "safety" register.
- **The finance-specific hard requirement:** *numbers are the product.* Tabular figures, clear
  1/l/I and 0/O distinction, solid currency rendering. A misaligned decimal erodes trust more than
  any font choice adds.

## 2. Translating "safety + progress" into type decisions
| Signal | Typographic carrier |
|---|---|
| **Safety** (waspada!) | even-rhythm grotesque body, generous x-height, open apertures, no quirky terminals; tabular numerals everywhere data lives |
| **Progress** | a geometric-leaning display face for headings/wordmark — slightly tighter, technical; motion comes from weight contrast (700–750 headings vs 400–500 body), not decoration |
| **Accountability** (our audit story) | a real monospace for evidence, loan IDs, model names — code-like = verifiable |

The pairing pattern: **grotesque body (safety) + geometric display (progress) + mono (audit)** —
three roles, exactly the `--font-sans` / `--font-display` / `--font-mono` slots tokens.css already
has.

## 3. WASPADA constraints
- **Bilingual EN + 简体中文** — any chosen Latin face must pair with **Noto Sans SC / Source Han
  Sans SC** (already in our stack); avoid faces whose weight/width clash with CJK companions.
- **Self-hosting** — FC-served app, no font CDN dependency wanted; woff2 subsets, 2 files max
  (display + mono; body can stay system).
- **The work-list is a number table** — whatever we do, `font-variant-numeric: tabular-nums` on
  p_default / EL / rate columns is the single highest-impact typographic fix, free, today.

## 4. Options
### A. Stay system-stack (current) — $0, ship-now
Segoe/PingFang/Noto via `ui-sans-serif`. Honest, fast, CJK-perfect. Zero distinction — the Wise
lesson unaddressed. **Add `tabular-nums` + weight discipline (750/600/400) and it's 80% there.**

### B. IBM Plex family — the "established fintech" pick  ⭐ recommended post-hackathon
**IBM Plex Sans** (display+body) + **IBM Plex Mono** (audit/evidence) + Plex Sans SC exists for
CJK. Open-source (OFL), engineered for data UIs, corporate-credible, and its slightly squared
grotesque quietly matches our chevron mark's geometry. One family = coherent, 2 woff2 subsets.

### C. Geometric display over system body — max "progress" on a budget
**Sora** or **Manrope** (OFL, geometric-leaning) for wordmark/headings only; body stays system;
mono = JetBrains Mono or Plex Mono. Cheapest way to add character; risk: two-source look if the
display's roundness fights Noto SC.

## 5. Recommendation
- **Now (pre-deadline):** Option A hardening — `tabular-nums` on all numeric columns, heading
  weight 700→750, letter-spacing +0.02em on uppercase labels. No new files, no risk.
- **Post-hackathon:** Option B (Plex Sans + Plex Mono, self-hosted woff2 subsets) wired into
  `--font-display`/`--font-mono`; body stays system for CJK safety. Revisit a characterful
  wordmark-only face (the Wise move) when the brand matures.

## Sources
- fontalternatives.com (fintech fonts 2026: Inter/Plex verdict, tabular-figures requirement)
- fuselabcreative.com (fintech UX 2026), noboringdesign.com (tech brand fonts), fello.agency
- medium.com/design-bootcamp "readable money" (numerals as trust), Wise custom-font case
- Psychology: dool.agency, brandvm.com, designyourway.net, confetti.design (serif trust deltas,
  geometric-vs-grotesque perception)
