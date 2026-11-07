# Console redesign — Round 2: three directions

Open `index.html` for the live side-by-side. Each render is fully self-contained (fonts embedded as
data-URIs, zero external requests) and carries a theme toggle in its top bar. Fixture data is
byte-identical to round 1's previews, so every visible difference is the design. Every text/surface
pair in every direction and both themes measures ≥ 4.5:1 (WCAG relative-luminance check run
programmatically over the full palette; worst pair per direction listed under each table).

Shipped values referenced below are the console's current `src/styles/tokens.css`.

---

## Direction A — Meridian ("the control room, rebuilt") — RECOMMENDED

**The idea in a sentence:** keep the Cold Criticality identity — cold blue-charcoal, cherenkov cyan,
mono identifiers — and rebuild everything that made it flat: the ladder, the type, the geometry, the
elevation.

**Palette.** The three-step near-black ladder becomes six steps with real spread: sunken `#070b10` →
ground `#10161e` → panel `#1a222d` → raised `#232e3b` → overlay `#2c3947`, with hairlines `#2f3b4a` /
`#465569`. The floor lightens so panels can genuinely lift; tables live inside elevated cards with an
inset edge-light and a two-layer shadow instead of being drawn as hairlines on the void. Cyan is
spent with intent only: the selection spine, the primary action, the live badge — `--cherenkov
#43d9ff` itself is untouched, deliberately, as the one surviving hex.

**Type.** IBM Plex Sans replaces B612 (technical voice without the cockpit oddness at text sizes);
JetBrains Mono replaces B612 Mono for identifiers. The scale gains rungs and air: title 22/28 600,
section 16/24 600, body 14/22, queue cells 13.5/20 on 40px rows (compact 36 remains a density mode),
labels 600 11/16 caps.

**Geometry & elevation.** Radius 2 → 4 (controls) / 8 (cards, wells). Sunken wells get inset
shadow; the docked decision pane floats on a real pane shadow with a raised header band.

**Optimised for:** the approver's queue — density with hierarchy, brand continuity, the sovereign
control-room seriousness the product trades on.
**It sacrifices:** distance. It is the closest of the three to today — same hue family, same accent —
so it reads as "same brand, new product" rather than a different company.

### Token diff vs shipped (Direction A)

| Token | Shipped | A dark | Changed |
|---|---|---|---|
| `--ground` | `#0c1014` | `#10161e` | ✔ |
| `--panel` | `#151b22` | `#1a222d` | ✔ |
| `--panel-raised` | `#1b222b` | `#232e3b` | ✔ |
| `--panel-sunken` | — (none) | `#070b10` | ✔ new step |
| `--panel-overlay` | — (none) | `#2c3947` | ✔ new step |
| `--rule` | `#232c36` | `#2f3b4a` | ✔ |
| `--rule-strong` | `#313c48` | `#465569` | ✔ |
| `--text` | `#e8edf2` | `#eaf0f6` | ✔ |
| `--text-dim` | `#8b98a5` | `#a3b4c5` | ✔ |
| `--text-faint` | `#5a6673` | `#68798d` | ✔ |
| `--cherenkov` | `#43d9ff` | `#43d9ff` | ✘ kept (deliberate) |
| `--font-ui` | B612 | IBM Plex Sans | ✔ |
| `--font-mono` | B612 Mono | JetBrains Mono | ✔ |
| `--type-title` | 700 28/36 | 600 22/28, −0.01em | ✔ |
| `--type-section` | 700 18/24 | 600 16/24 | ✔ |
| `--type-body` | 400 14/20 | 400 14/22 | ✔ |
| `--type-cell` | 400 13/18 | 400 13.5/20 | ✔ |
| `--type-small` | 400 12/16 | 400 12.5/18 | ✔ |
| `--type-label` | 700 11/16 | 600 11/16, +0.07em | ✔ |
| `--radius` | `2px` | `4px` / `8px` (cards) | ✔ |
| `--row-height` | `36px` | `40px` (compact 36) | ✔ |
| `--topbar-height` | `48px` | `52px` | ✔ |
| `--rail-width` | `240px` | `248px` | ✔ |
| shadows | none in token layer | card / pane / overlay / well-inset | ✔ new |
| status colours | 5 | retuned ×5 + tints at 10–12% | ✔ |

**Count: 24 of 25 listed tokens differ** (every §0 row differs). Worst measured contrast pair:
`--text-dim` on overlay 5.55:1 (dark), 6.18:1 (light).

---

## Direction B — Hairline ("monochrome restraint")

**The idea in a sentence:** hierarchy carried entirely by surface lift and hairlines — the work area
floats as one rounded canvas over a near-neutral graphite floor, colour all but disappears, and
status is a glyph and a word, never a filled badge.

**Palette.** Neutral graphite, barely cool: floor `#0e0f10`, canvas `#161719`, raised `#1d1e21`,
overlay `#252629`, hairlines `#26272b` / `#38393f`. The rail and topbar sit on the floor with no
chrome of their own; the entire working surface is one elevated canvas (radius 12, ring + shadow) —
the Linear/Attio structural move, and the most immediate visual separator from today. One accent
(`#4cc9f0`, softened from cherenkov) appears exactly three ways: primary action, focus, selection.
Escalated/Approved/Denied are coloured text with a glyph — no fills anywhere in the table.

**Type.** Inter for UI (the neutral instrument-panel choice), JetBrains Mono for identifiers. Title
20/28 600, body 14/21, cells 13.5/20 on 38px rows, labels sentence-case 500 — no all-caps outside
nothing: even table headers are sentence case, which quiets the whole page.

**Geometry.** 6px controls, 10px cards, 12px canvas. Shadows are soft and structural (a ring plus
two layers), never decorative.

**Optimised for:** calm. Ask reads best here of the three; nothing shouts, and the eye ranks
everything by lift alone. Cheapest to keep coherent as the product grows.
**It sacrifices:** identity and status glanceability — it could be any excellently-built modern tool,
and an approver scanning for the one Escalated row gets a smaller signal than A's tinted pills.

### Token diff vs shipped (Direction B)

| Token | Shipped | B dark | Changed |
|---|---|---|---|
| `--ground` | `#0c1014` | `#0e0f10` (neutral, de-blued) | ✔ |
| `--panel` | `#151b22` | `#161719` | ✔ |
| `--panel-raised` | `#1b222b` | `#1d1e21` | ✔ |
| `--panel-sunken` / `--panel-overlay` | — | `#0a0a0b` / `#252629` | ✔ new steps |
| `--rule` / `--rule-strong` | `#232c36` / `#313c48` | `#26272b` / `#38393f` | ✔ |
| `--text` / `--text-dim` / `--text-faint` | `#e8edf2` / `#8b98a5` / `#5a6673` | `#ededef` / `#a5a7af` / `#6e7077` | ✔ |
| accent | `#43d9ff` | `#4cc9f0` | ✔ |
| `--font-ui` | B612 | Inter | ✔ |
| `--font-mono` | B612 Mono | JetBrains Mono | ✔ |
| `--type-title` | 700 28/36 | 600 20/28, −0.012em | ✔ |
| `--type-body` | 400 14/20 | 400 14/21 | ✔ |
| `--type-cell` | 400 13/18 | 400 13.5/20 | ✔ |
| `--type-label` | 700 11/16 caps | 500 11.5–12/16 sentence case | ✔ |
| status rendering | filled pills | glyph + coloured word, no fills | ✔ |
| `--radius` | `2px` | `6px` / `10px` / canvas `12px` | ✔ |
| `--row-height` | `36px` | `38px` | ✔ |
| shell | full-bleed panels | floating canvas over floor | ✔ structural |
| shadows | none | canvas-ring / float | ✔ new |

**Count: 23 of 24 listed rows differ** (every §0 row differs; only the fixture data is shared).
Worst measured contrast pair: `--text-dim` on overlay 6.30:1 (dark), 5.80:1 on sunken (light).

---

## Direction C — Atelier ("warm architectural")

**The idea in a sentence:** the approval record rendered like something expensive — graphite warmed
toward umber, the roomiest density, deep soft elevation, and a two-accent system (cherenkov for
interaction, a quiet gold for tier/attention).

**Palette.** Warm graphite: floor `#151310`, panel `#1e1b17`, raised `#282420`, overlay `#322d27`,
warm hairlines `#353026` / `#4c4536`, ivory text `#f2eee5`. Light theme is warm paper (`#f2efe7`
floor, near-white cards) — the biggest light-mode departure of the three. Accent `#4fd6e8` plus gold
`#e0a13f` for the tier chip and the queue badge; status pills are soft full-round tags in sentence
case.

**Type.** Geist Sans / Geist Mono. The biggest reading sizes of the three: body 15/24, answers
15/25, title 24/32, cells 13.5/20 on 44px rows with generous cell padding. Overline labels at +0.06
to +0.1em tracking give the sheet its architectural rhythm.

**Geometry & elevation.** 8px controls, 12px cards, and the decision pane is a floating sheet —
inset from the edges, radius 14, deep two-layer shadow — rather than a docked rail.

**Optimised for:** first impressions and reading comfort; the Tier-3 wire-transfer sheet looks like
a document worth signing. Demos extremely well.
**It sacrifices:** rows per screen (44px rows show the least data of the three), brand continuity
(warm + gold is a real departure from "cold criticality"), and the two-accent system demands
discipline the other directions don't need.

### Token diff vs shipped (Direction C)

| Token | Shipped | C dark | Changed |
|---|---|---|---|
| `--ground` | `#0c1014` | `#151310` (warm) | ✔ |
| `--panel` | `#151b22` | `#1e1b17` | ✔ |
| `--panel-raised` | `#1b222b` | `#282420` | ✔ |
| `--panel-sunken` / `--panel-overlay` | — | `#0e0d0a` / `#322d27` | ✔ new steps |
| `--rule` / `--rule-strong` | `#232c36` / `#313c48` | `#353026` / `#4c4536` | ✔ |
| `--text` / `--text-dim` / `--text-faint` | `#e8edf2` / `#8b98a5` / `#5a6673` | `#f2eee5` / `#b5ab9a` / `#7e7463` | ✔ |
| accent | `#43d9ff` | `#4fd6e8` + gold `#e0a13f` | ✔ (two-accent) |
| `--font-ui` | B612 | Geist Sans | ✔ |
| `--font-mono` | B612 Mono | Geist Mono | ✔ |
| `--type-title` | 700 28/36 | 600 24/32, −0.015em | ✔ |
| `--type-body` | 400 14/20 | 400 15/24 | ✔ |
| `--type-cell` | 400 13/18 | 400 13.5/20 | ✔ |
| `--type-label` | 700 11/16 | 500 10.5–11/16, +0.06–0.1em | ✔ |
| status rendering | square pills | full-round soft tags, sentence case | ✔ |
| `--radius` | `2px` | `8px` / `12px` / sheet `14px` | ✔ |
| `--row-height` | `36px` | `44px` | ✔ |
| detail pane | docked | floating inset sheet | ✔ structural |
| light theme | cool grey/white | warm paper/cream | ✔ |
| shadows | none | card / sheet (deep, soft) | ✔ new |

**Count: all 19 listed rows differ** (every §0 row differs). Worst measured contrast pair:
`--text-dim` on overlay 6.01:1 (dark), 5.04:1 on sunken (light).

---

## Typeface decisions (all three)

All faces are OFL-1.1, vendored as latin woff2 via the existing `scripts/vendor-fonts.mjs` pattern
(pinned @fontsource jsDelivr URLs, sha256 recorded, licence texts committed, **not** in the npm
closure — exactly as B612 is handled today). Byte costs are the latin subsets actually embedded in
these renders, from `@fontsource/*@5.3.0`:

| Direction | UI face (weights) | Data face (weights) | Total woff2 |
|---|---|---|---|
| A | IBM Plex Sans (400/500/600/700) — 93.9 KB | JetBrains Mono (400/500/700) — 64.9 KB | **158.8 KB** |
| B | Inter (400/500/600/700) — 96.7 KB | JetBrains Mono (400/500) — 43.0 KB | **139.7 KB** |
| C | Geist Sans (400/500/600/700) — 138.9 KB | Geist Mono (400/500) — 20.0 KB | **158.9 KB** |

Vendoring path per direction: `src/assets/fonts/{family}-latin-{weight}.woff2` from
`https://cdn.jsdelivr.net/fontsource/fonts/{family}@5.3.0/latin-{weight}-normal.woff2`.

---

## Ranking (design lead)

1. **A — Meridian. I would ship this.** The owner's round-1 instinct — keep the control-room
   character — was right; round 1 failed by changing nothing, not by keeping the character. A moves
   every token that caused the flatness (ladder, faces, scale, radius, elevation, density) while
   keeping the two things that are genuinely brand: the cold hue family and the cherenkov accent.
   It is the best queue-clearing screen of the three, and the one an Ally Financial platform team
   reads as purpose-built rather than fashionable. Honest caveat: it clears the stranger test with
   the smallest margin — "same brand, new product," not "different company."
2. **B — Hairline.** The most disciplined execution and the best Ask screen; it clears the stranger
   test instantly. Second, not first, because it spends the product's visual identity to get there —
   nothing on screen says "sovereign infrastructure" rather than "well-made SaaS" — and glyph-only
   status is measurably weaker for the highest-frequency job (scanning a queue for the one
   escalation).
3. **C — Atelier.** The strongest first impression and the best-dressed detail sheet; unmistakably
   different within two seconds. Third because it optimises the demo over the Tuesday afternoon:
   44px rows cost real density in every governance list, warm-plus-gold is the largest brand risk,
   and the two-accent system is the easiest of the three to get wrong in future screens.

If the deciding criterion is maximum distance from today, choose C. If it is calm neutrality,
choose B. On the product's own terms — an approver's daily instrument for a regulated environment —
A is the one I would put in front of a customer.
