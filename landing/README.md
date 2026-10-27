# LCP landing page

The public marketing page for the control plane, served at `/welcome/` (see
`handler._serve_landing`). Plain HTML, CSS and ~5 KB of JS — no build step, no
framework, so it ships inside the existing container image (`COPY landing/`).

## Files

| file | role |
|---|---|
| `index.html` | the page |
| `styles.css` | the whole visual system: tokens, layout, components, responsive |
| `main.js` | reveal observers, tagline word lighting, mobile menu |
| `favicon.svg` | brand mark |
| `404.html` | branded not-found page for `/welcome/<unknown>` |
| `fonts/*.woff2` | Geist + Geist Mono, self-hosted (no CDN, no third-party request) |

## The visual system is DERIVED, not borrowed

Every value comes from the product's own world, not from a reference platform:

- **Canvas and surfaces** (`#0F1115`, `#171A21`, `#1F242C`) are the dashboard's
  existing dark palette — the landing had to look like the product it advertises.
- **Mono for data.** The page's numbers (request counts, spend, latencies, the
  `POST /l2/...` path) are set in Geist Mono with tabular numerals, because that
  is how the dashboard renders them. The marketing page and the product share a
  type language for numbers.
- **One accent, split by role.** `--action` (`#3A6BE8`) is the fill that carries
  a white label; `--action-text` (`#8FB0FF`) is the same hue lightened for text
  and icons on dark. One token cannot serve both (a fill needs luminance ≤ 0.1833
  for a white label, accent text needs ≥ 0.2154), so the split is explicit.

## Contrast — measured, not asserted

Every pair below was computed with the `wcag-contrast.py` instrument from the
`web-design-system` skill. The token set returns **0 failing pairs**.

| pair | ratio | floor |
|---|---|---|
| body text on canvas | 15.56 | 4.5 |
| body text on surface | 14.33 | 4.5 |
| secondary text on canvas | 7.27 | 4.5 |
| secondary text on surface | 6.70 | 4.5 |
| white label on `--action` fill | 4.72 | 4.5 |
| `--action-text` on canvas | 8.84 | 4.5 |
| `--border` (input/focus boundary) vs canvas | 3.47 | 3.0 |
| `--border` vs surface | 3.20 | 3.0 |

Two decisions worth recording:

- **The tagline's muted state is 0.4 opacity, not 0.3.** At 36px on this canvas
  0.3 measures 2.41:1 and fails the 3:1 large-text floor; 0.4 measures 3.34:1.
- **`--hairline` (`#262B36`) is a decorative separator** at 1.23:1 and is
  deliberately exempt from the text-contrast rule. `--border` exists separately
  for the boundaries that carry meaning (inputs, focus rings).

## Verification performed on this page

- **axe-core 4.10.2**, `color-contrast` + `link-name` + `button-name` +
  `heading-order` + `landmark-one-main` + `region` + `image-alt`: **0 violations**.
  `incomplete` is non-zero and honest: 17 nodes sit over the hero's video or a
  gradient, so axe cannot resolve their backdrop automatically. Those are the
  hero copy over the video poster, the proof list, and the closing CTA band.
- **Viewport ladder** 390 / 768 / 1280 / 1920 px: `docScrollWidth == innerWidth`
  at every step, zero elements wider than the viewport. No horizontal scroll.
- **Touch targets**: every interactive element measures ≥44px in its own box
  (nav links, buttons, FAQ summaries, footer links, brand mark).
- **Keyboard**: real Tab key events through the mobile overlay stay inside it;
  `Escape` closes it and returns focus to the toggle; `body` scroll is locked
  while open and restored on close.
- **Motion**: `prefers-reduced-motion` disables every transition and renders the
  reveals and the tagline already-lit. Reveals use IntersectionObserver only —
  there is no `window.addEventListener('scroll')` anywhere in `main.js`.

## Editing it

Tokens live at the top of `styles.css`. Change a value there and re-run the
contrast instrument before shipping; the rest of the page follows the tokens.
