# Round 3 · Task 1 Part A — typeface recommendation

**Recommendation: Source Sans 3 (UI) + Fira Mono (data).**
Replaces IBM Plex Sans + JetBrains Mono, which the owner read as "a bit harsh".

## The pairing

| Role | Face | Package (pinned) | Weights | Latin woff2 bytes |
|---|---|---|---|---|
| UI | Source Sans 3 | `@fontsource/source-sans-3@5.3.0` | 400/500/600/700 | 62,612 |
| Data | Fira Mono | `@fontsource/fira-mono@5.3.0` | 400/500/700 | 51,116 |
| **Total** | | | | **113,728** |

Today's committed set (B612 ×4 + Archivo variable) is **162,540 bytes**; the pairing lands
**48,812 bytes (30%) under budget**. Both licenses are OFL-1.1 — each package's `LICENSE` was
fetched and contains the SIL OPEN FONT LICENSE text. Vendoring follows the existing
`scripts/vendor-fonts.mjs` pattern exactly (pinned jsDelivr fontsource URLs, sha256 recorded,
committed as static assets, **never** in package.json / the npm closure, zero runtime
requests). Per-file sha256 + byte counts are in `fonts.json` in this directory.

## Why — from the renders, not reputation

Every claim below was judged from screenshots of `dir-a-approvals.html` re-rendered with the
candidate as the **only** change (palette, layout, sizes untouched), read back at 2x, plus a
specimen page per candidate with an in-page measurement of figure spacing.

**Source Sans 3** (vs Plex Sans at the same sizes):
- 13.5px table cells: humanist bowls and open apertures on `a e g s` where Plex is chopped
  flat — the row of `agent://sales-drafts` cells visibly loses the "engineered rhythm"
  harshness while column color stays even and quiet.
- 11px uppercase labels (`WHAT HAPPENS IF APPROVED`, table headers): slightly narrower than
  Plex/Hanken, calm, no crowding at 0.07em tracking.
- 14px body in the detail pane reads soft but plain — no rounded-startup flavor.
- **Default figures are tabular** (measured: `1111` == `0000` width with no feature applied,
  and with `tnum`). Counts and timers align even in spans where the round-2 CSS forgot
  `font-variant-numeric` — a real robustness win over Inter/Jakarta, whose defaults are
  proportional (Inter measured 219.6 vs 340.7; Jakarta 200.3 vs 395.3 at 135px).
- Holds up in the light theme (checked; slightly lighter color than Plex, still 400-weight
  even at cell sizes).

**Fira Mono** (vs JetBrains Mono):
- Rounded, humanist terminals on `w t r e` — `payments.wire_transfer` and
  `write.internal.notes.append` at 13px lose JetBrains' stiff squareness but keep full
  technical seriousness; it sits naturally next to Source Sans 3 (both humanist).
- `250,000.00` and the `42:10` timers render with round, open digits; the amber `-00:42`
  overdue timer stays instantly readable.
- Marked zero distinct from `O`; `I l 1 |` all distinct at 13px (read from the zoomed
  ambiguity row).
- Real 400/500/700 weights — the render uses all three.

## Runners-up, and why each lost

- **Hanken Grotesk** (UI, 54,880 B) — the close second. Warmer and rounder than SS3, default
  tabular figures too, and it held both themes. Lost on: larger x-height makes dense 13.5px
  tables a touch louder; the flat foot-serif `1` gives timer/date strings a busier texture;
  overall it drifts one step toward "friendly UI kit" where SS3 stays infrastructure-calm.
- **Plus Jakarta Sans** (UI, 48,560 B) — smoother, yes, but reads geometric-soft/brand-y (the
  exact "startup-playful" failure mode); `I`/`l` identical at 12.5px; **default figures
  proportional** (200.3 vs 395.3) so any span missing `tnum` misaligns — the specimen's
  alignment table showed exactly that failure before the feature was applied.
- **Inter** (UI, 96,744 B) — the neutral control. Cleaner spacing than Plex but terminals stay
  blunt; it does not deliver the "smoother/warmer" the owner asked for. Also proportional
  default figures and the heaviest candidate by 34-48 KB.
- **Geist Mono** (mono, 30,160 B) — the byte-cost winner and a fine face, but visibly cooler
  and more geometric than Fira next to a humanist sans; it modernizes rather than warms. Keep
  on file if the budget ever tightens: SS3 + Geist Mono = 92,772 B.
- **Commit Mono** (mono, 143,352 B for 3 weights) — pleasant, deliberately neutral, but the
  fontsource build is ~48 KB **per weight**; the pairing total would be 205,964 B — 27% over
  the 162 KB budget. Disqualified on byte cost.
- **DM Mono** — excluded before rendering: no 700 weight exists in the package and the render
  uses mono 700.

## Migration notes (for the unit that adopts this — not done here)

- Vendor via the `scripts/vendor-fonts.mjs` pattern: add the six pinned URLs + two LICENSE
  files, record sha256 in `src/assets/fonts/README.md`, remove the B612/Archivo files it
  replaces (or leave that to the same unit — either way keep total ≤ 162,540 B; this set
  leaves 48.8 KB of headroom).
- `--font-ui: "Source Sans 3", system-ui, sans-serif` · `--font-mono: "Fira Mono",
  ui-monospace, SFMono-Regular, monospace`.
- The round-2 `.ne-mono { font-size: 0.93em }` optical compensation still looks right with
  Fira Mono at 13px next to SS3 (checked in the pairing render); no size retune needed.
- Keep `font-variant-numeric: tabular-nums` where round 2 has it — SS3 makes it redundant
  (tabular by default) but explicit intent survives any future face swap.

## What I would still change with another pass

- Render an Ask/transcript screen with the pairing (Part B owns that surface; only Approvals
  was in scope for the bake-off).
- Measure contrast deltas: SS3 400 is slightly lighter than Plex 400 at 13.5px; worth a
  programmatic 4.5:1 sweep on `--text-dim`/`--text-faint` pairs during adoption (the palette
  is unchanged, so any pass/fail is unchanged in *color* terms, but perceived weight shifts).

## Files in this directory

- `type-index.html` — all candidates side by side, with the recommendation.
- `ui-*.html` — Approvals re-rendered per UI candidate (mono held at JetBrains so the sans is
  the only variable). `mono-*.html` — Approvals with Source Sans 3 + each mono candidate
  (`mono-fira-mono.html` **is** the recommended pairing render).
- `specimen-*.html` — per-candidate specimen: locked scale, weights, numerals with measured
  tabular-figure verdict, ambiguity traps, mono-beside-sans.
- `fonts.json` — pinned sources, per-file bytes + sha256, per-candidate totals, budget math.

Naming note: the design system is "Cold Criticality" throughout; the dead round-2 label does
not appear in any file here (verified by scan), including the scrubbed preview-bar strings
inherited from the round-2 render.
