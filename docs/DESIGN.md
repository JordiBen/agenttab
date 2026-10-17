# AgentTab design system (docs/DESIGN.md)

Owner-approved direction: **one motif: the receipt and its stamp.** This file is the source of truth for every visual surface: the landing page, the demo workspace, the AG Grid tables, the Bryntum schedule, video frames and Devpost screenshots.

## 1. The idea
> On this page, **AgentTab's own guard log** becomes **a till receipt that prints line by line while the visitor watches, and the injected purchase is stamped BLOCKED.**

Competitor test: Bursar and other agent-wallet products have no running receipt with a stamp per decision. AgentTab logs every decision with the layer that made it, and that log *is* the receipt.

**The motif is the receipt's torn, sawtooth edge.** It's the only decorative shape on the page, used at 3–4 very different scales:
1. **Huge:** the hero receipt, a flat bone-white strip ~70vw tall, cropped by the viewport, with a sawtooth bottom edge.
2. **Section breaks:** one full-width band per half of the page with a sawtooth top or bottom edge, instead of rules or gradients.
3. **Buttons:** the primary button is a solid olive block with a small sawtooth bottom edge (same tooth geometry, scaled).
4. **Text size:** a tiny sawtooth tick as the bullet in lists.

Tooth geometry, everywhere: an isosceles triangle, width : height = 2 : 1. Draw it with one CSS `clip-path: polygon()` or a `mask` built from a repeating linear pattern of that triangle. No other shapes: no circles, blobs, glows or extra geometry.

**The stamp** is the second element, used only where a real verdict exists: a rotated (-6°) word in heavy capitals, flat fill, no border tricks, no texture. `BLOCKED` in stamp red, `PAID` in olive, `HELD` (waiting for a human) in amber-ink. A stamp appears only on a real decision line and never as decoration.

## 2. The live thing
The hero receipt is live: it replays real guard decisions from `/api/replay`, and the visitor's own demo purchases print onto it. The primary action is **"Try to make the agent overspend"**: it opens the demo with a funded sandbox agent, then the injected-purchase button. The Gauntlet stays as a secondary action lower on the page.

## 3. Tokens
```css
:root {
  /* colour: olive on bone. Two verdict colours because pass/blocked is a real pair. */
  --bg: #ecebe7;        /* page ground (bone) */
  --paper: #f4f4f1;     /* receipt paper, a cool neutral bone one step lighter than the ground */
  --surface: #e1dfd9;   /* tiles, grid header */
  --ink: #38450f;       /* all text, large and small */
  --muted: #5d683a;     /* secondary text; still >= 4.5:1 on --bg and --paper */
  --accent: #38450f;    /* = ink; filled buttons, PAID stamp */
  --accent-ink: #ecebe7;
  --stamp-block: #a3261b;  /* BLOCKED stamp only. Check >= 4.5:1 on --paper. */
  --stamp-hold: #7a5200;   /* HELD stamp only. Check >= 4.5:1 on --paper. */
  --field-1: #38450f; --field-1-ink: #ecebe7;   /* footer: solid olive field, bone text */

  /* type: five steps, nothing smaller than 13px, body >= 16px */
  --step-small: clamp(0.8125rem, 0.79rem + 0.1vw, 0.875rem);
  --step-body:  clamp(1.0625rem, 1rem + 0.25vw, 1.1875rem);
  --step-lead:  clamp(1.25rem, 1.1rem + 0.7vw, 1.625rem);
  --step-head:  clamp(2rem, 1.3rem + 3.2vw, 4rem);
  --step-hero:  clamp(3.5rem, 2rem + 6vw, 8rem);

  --radius: 6px;        /* the only radius on the page */
  --tooth: 12px;        /* sawtooth width at section scale; height = width / 2 */
  --gutter: clamp(16px, 4vw, 48px);
  --ease: cubic-bezier(.2,.7,.2,1);
}
```
- **Font:** one rounded geometric sans for everything, display weight 500 (tracking -0.025em, leading 1.05) and text 400/600 (leading 1.5). The font is **Roundo** (Fontshare; approved, SIL OFL 1.1 per the licence file shipped in the Fontshare download). Self-host the woff2 in `static/fonts/` with `License/OFL.txt` next to it. No Nunito, no CDN font links.
- **Numbers on the receipt** use `font-variant-numeric: tabular-nums` in the same face. No monospace anywhere.
- **Light theme only for the landing page.** The dashboard may keep a dark mode only if it uses these same roles.

## 4. Page structure (landing)
1. **Hero:** headline at `--step-hero`, one or two lines max, flush left on the margin. Suggested copy: "Your agent gets a tab. Not your card." Keep the claim honest. One supporting sentence at `--step-lead`, max 46ch. Primary sawtooth button: "Try to make the agent overspend". On the right, entering from the edge and cropped by the viewport, the giant receipt (`data-ui`), printing real `/api/replay` lines: `POST purchase · vendor · item · $amount`, then the layer and the verdict stamp. The injected line gets BLOCKED stamped across it. Honesty labels stay in a small line under the button: "PayPal sandbox · test money" and the live AI guard status from `/healthz`.
2. **How it works, as one bar:** the purchase path as one horizontal bar spanning the page (`data-ui`), with segments sized by real share of decisions from the eval: catalog/allowlist → caps & budgets → AI guard → human approval → PayPal payment. Each segment is a flat colour from the palette (ink tints only, no gradients). Under each segment, one clause at body size starting with its order word (First, Then, ...), not numbered.
3. **Proof, as tiles:** a board of flat tiles (no borders, no shadows). The first, double-width tile is in the accent colour and holds the heading. The others hold the real numbers from `eval/results.json`, each stated with its honest denominator. **Rules alone vs rules + AI must both be shown**: the gap is the AI's real contribution. Plus the held-out set result once it exists. The scorecard table stays below for detail.
4. **Showcase, the receipt list:** a vertical list of 4–6 real purchase scenarios (legit sample, SaaS seat, injected instruction, gift card, social-network target). Choosing one swaps the receipt fragment on a flat sawtooth-edged stage beside it (240–300ms crossfade) to show that scenario's real verdict and layer. Inactive items stay `--muted` at full strength. No fading below 4.5:1.
5. **MCP:** a plain two-column section: "Works with any MCP client" plus the copyable config fragment (`data-ui`).
6. **Footer:** solid olive field, bone text. The wordmark built from blocks, each letter knocked out of its own rectangle, with differing heights and 4px gaps. Links in plain columns. No "by WebIntel Tools".

**Pictures (floor: at least four real images):** use **real screenshots of AgentTab itself** (the demo workspace, the purchases grid, the schedule, the PayPal sandbox approval page), cropped inside the sawtooth shape or sitting on a flat sawtooth stage. Never device frames, never fake dashboards of grey bars, no stock photos of people.

## 5. App surfaces (demo workspace, owner console)
- Same tokens. Real interface chrome is allowed in `data-ui` (table lines, inputs), but no decoration: no glass, no glow, no gradients, no resting shadows.
- **AG Grid v33:** theme with the Theming API (`themeQuartz.withParams({...})`, check the v33 docs), using the same colours and font. No `!important` overrides.
- **Bryntum:** its `--b-*` CSS variables mapped to these tokens. Event colours come only from the palette and verdict colours.
- **Verdicts** everywhere (grid cells, schedule events, toasts) use the stamp vocabulary: PAID / HELD / BLOCKED / REFUNDED.

## 6. Remove list (the linter must reach 0 FAIL on the landing page)
Gradients and gradient text, glass/backdrop-blur, resting shadows and glows, 1px outlined boxes (except real inputs/tables in `data-ui`), hairline rules, eyebrow kickers, monospace labels, emoji used as icons, 01/02/03 numbering, violet, Inter, film grain, aurora blobs, the big-number-small-label stat row.

## 7. Must not change
- Every element id and class that `static/app.js`, `static/schedule.js` and the e2e tests select on. Check with grep before renaming anything.
- Every factual number and claim, unless a re-run changes it. Then take the number from the result files, never type it by hand.
- Honesty labels: "PayPal sandbox", test money, live AI guard status.
- No CDN scripts or fonts. WCAG AA contrast, visible focus, `prefers-reduced-motion` respected (the receipt prints instantly), no horizontal scroll at 390px.

## 8. Motion
One loud moment: the receipt printing line by line, and the stamp landing (scale 1.15 → 1, 180ms, `--ease`) on BLOCKED. Elsewhere: short entrances for section parts (opacity + 12px rise, 240ms), and feedback on every control. Nothing loops forever except the receipt replay, and that pauses when off-screen.

## 9. How it's checked
- design lint: 0 FAIL.
- Screenshots at 1440 / 768 / 390: 0 FAIL.
- Critique pass, and Playwright clicks through the full demo flow.
- The owner sees before and after side by side and approves before merge.
