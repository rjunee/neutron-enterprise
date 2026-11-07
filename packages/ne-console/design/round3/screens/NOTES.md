# Round 3 · Task 2 — recomposed screens (Home · Audit log)

This file covers the two screens of this run: **Home** and **Audit log**. Search, Posture and
Knowledge Graph belong to a separate run. Palette, geometry and type scale are the locked
Cold Criticality baseline; the typefaces are the settled round-3 pairing — Source Sans 3 for UI,
Fira Mono for data — reusing the vendored woff2 from `../type/`.

---

## Home — `home.html`

**The job.** Every signed-in user, at the start of every session: *what needs me, and did
anything change?*

**The composition change.** The queue leads; status is demoted to the margin.

- **"Waiting on you" is the first content on the page.** The first row a reader's eye lands on
  is the single most urgent item in their queue (approvals by deadline ascending, then reviews
  oldest-first — the served order). The overdue row carries an amber inset edge plus the
  ⚠ OVERDUE word; one click opens the item on its owning screen.
- **The four tiles became a 288px "standing facts" margin column** — Approvals, Audit log,
  Controls, Sources as compact linked facts (label / value / sub), readable in one saccade and
  never occupying the fold. The shipped page opened with a full-width band of placards, which
  is status furniture: a number that announces itself and sends you elsewhere spends the visit.
- **Recent activity stays below the queue**, ten rows, deliberately unsortable, with one exit:
  "Open audit log".
- **Narrow (≤1180px):** one column — queue first, then activity, facts folding to a 2×2 grid
  at the foot. The queue's lead survives the narrow viewport.

**What was removed, and why.**
- The full-width tile band as the opening act. Nothing was deleted from the data — all four
  facts survive, linked, in the margin. Removal of *position*, not capability.
- No decide-in-place affordance was added (deliberate non-addition): a queue row links to the
  one decision surface; duplicating approve here recreates the two-surfaces-disagreeing defect.

**The click-count argument (second week of use).** The thing people come to Home for is the
queue row, every session. Shipped: scan four tiles → scroll to queue → click row (eye passes
~4 placards first; on a laptop the queue head sits at the fold). Recomposed: the most urgent
row is the first line of content — zero scrolling, one click. Status facts cost a glance to the
right margin instead of the whole first screenful; on the second week nobody reads a
"sovereignty" placard, but everyone reads row one.

**States designed** (gallery at the foot of the render): queue loading (static skeleton rows in
table geometry), queue empty ("Nothing is waiting on you."), one-lane-failed partial (arrived
rows kept, failure stated beneath), both-lanes-failed (fact + Retry), facts loading/failed
(skeleton / em dash + "Couldn't load"), refused facts omitted entirely, activity failed (the
retry lives on the ledger which owns the read), and the reader-key page (no queue granted, chain
refused → neither section drawn at all).

**Named divergences from the round-2 CSS baseline** (values untouched; usage changed):
- `--text-faint` is no longer used for load-bearing text (rail group labels, field labels now
  use `--text-dim`): measured contrast of faint-on-panel is 3.6:1 dark / 3.1:1 light, below the
  4.5:1 floor. Faint survives only on the ⌘K keycap hint, which duplicates the adjacent
  placeholder text.
- Status pills carry the shipped StatePill glyph vocabulary (● ◑ ⚠ ⊘ ⊟ + word) instead of
  round 2's uniform dot — status is never colour alone, and the glyph differentiates OVERDUE
  from ESCALATED for a colour-blind reader where both sit in amber.
- PENDING renders in the neutral pill (round 2's choice), not StatePill's amber mapping: a
  queue at rest should be quiet; amber is spent on the exceptional rows. Flagged as a
  recommendation for the shipped StatePill mapping rather than silently diverging.

---

*The three sections below cover the remaining screens of this round: **Search**, **Posture** and
**Knowledge Graph**, composed against the same settled baseline as Home and Audit above.*

## Search — `search.html`

**The job.** Find the document, see why it matched, and get into it.

**The composition change.** From a text box over cards to a command row over a ranked two-line
result list with a docked record pane.

- **The query is a command row**, not a form field in a toolbar: one prominent input, the window
  size, the run button, and (in flight) the elapsed counter and Cancel. Below it, the toolbar the
  shipped page never had: filter-within, sort, source facet chips, the count line
  (`10 results · 1.8 s · 2 unconfirmed`), Export CSV and the second-identity comparison.
- **Line one of every row is the document's own name** — the served human identity (title →
  leading heading → source path → record id → hash last), never the hash while anything better
  was served. Line two is the matched text, two lines clamped, with the literal query terms
  marked. Rank and score hold fixed gutters so ten rows scan as one column of names.
- **The term marks are a fact about the text, not the ranking**: only verbatim occurrences of the
  query terms are marked; the score column remains the server's only relevance claim, drawn as
  the served number plus a quiet 56px meter.
- **The record pane docks on the right in the audit screen's exact anatomy** (head / fields /
  body / footer): document hash and citation ref up top, the full matched text, the opaque
  metadata map rendered as served, related-entity chips (UNCONFIRMED pill on extracted ones),
  and the two ways out — "Open in source system" (only when the server sent a deep link) and
  "View in Graph" (only when the served traversal proves the citation anchors an entity).
- **Narrow (≤1280px):** the pane leaves the flow and overlays below the toolbar line with the
  pane shadow — the toolbar keeps its full width (the round-2 clipped-toolbar defect, designed
  against and shown in the gallery at a forced 1100px).

**What was removed, and why.**
- The card chrome per result. A card border around every result spent ~24px of vertical rhythm
  per row on decoration; ten results now fit where six cards did, and the eye tracks one title
  column instead of ten boxes.
- The hash as a result headline. It survives in the pane's Document field (it is what the audit
  chain records); it headlines only when nothing better was served (row 10 shows this honestly).

**The click-count argument (second week of use).** The analyst's loop is: run, scan, open, leave
for the source system. Shipped: run → open a card to find out *which document it even is* (the
headline was a hash) → back → open the next — identification cost one click per candidate.
Recomposed: identification is free (titles + marked terms in the list), so the second-week loop
is run → one click on the right row → one click out to the source system. The comparison beat —
previously buried behind a button that could fail silently — is a standing toolbar action whose
every outcome (union table, per-identity completeness, second-identity refusal, sign-in failure)
has a drawn state.

**States designed** (gallery): idle with this tab's recent searches (capture replay vs re-run,
"held in this tab only" disclosure), run in flight (static skeletons, live counter, Cancel),
replayed capture band and discarded-capture band, the server's two completeness statements
(authority-limited with scan bounds; completeness-unstated) drawn neutral — never the fault
colour, three different empty facts (proven-empty vs authority-limited-empty vs
filtered-to-nothing), failed / timed out / cancelled / refused as four distinct sentences, the
two-identity comparison with duplicate served ranks and "Not returned" cells, and the ~1100px
overlay treatment.

## Posture — `posture.html`

**The job.** Is the deployment configured the way we claim — and on what class of evidence.

**The composition change.** The answer leads; the evidence follows. A claim strip of three served
facts answers the screen's question before any table, and the twelve constraints become a true
annunciator panel — the one screen where that vocabulary names something real.

- **The claim strip**: Controls (`12 of 12 attested · PASSED`), Egress boundary (`● Sealed`,
  with the served reason sentence and the server-authored observation age), Configuration (the
  fingerprint hash chip, in force since, observed age). A reviewer's second-week visit ends here,
  zero clicks, unless something is off.
- **The annunciator**: twelve tiles (id · name · state), 4×3 on a hairline grid. At rest every
  tile is panel-quiet — a compliance instrument, not an alarm board. State is glyph plus word:
  ● PASSED (green text, no lit block), ⚠ FAILED (the only red on the page, tile edge-marked),
  ⊟ MANUAL (gated neutral — unverified is not broken). A tile click fills the docked pane
  (evidence class, build result, mode, the server's enforcement sentence rendered as sent).
- **Evidence classes are named where they apply**, one sentence each: BUILD-TIME evidence is not
  a runtime health check; the network section is a DIRECT RUNTIME READ, not a liveness probe;
  configuration is an AUDIT-BACKED read. Freshness comes only from the two server-authored
  observation instants, and the ages keep advancing between reads.
- **Network and Configuration stay as two quiet cards**: air gap / allowed hosts (with the
  one-place egress derivation — SEALED when the air gap is on *or* nothing is sanctioned), and
  the fingerprint plate over the history table (`To` is an em dash for the span in force).

**What was removed, and why.**
- The controls *table* as the primary surface. Twelve rows of sortable table spent the reader's
  first screenful re-proving what one glance at an annunciator states; the filter and count
  survive in the card head, the sortable detail lives one click away in the pane.
- The model-lanes content stays gone (it belongs to Models); the link row that tells the reader
  where it went is kept, as the shipped source requires.

**The prior defect, designed around.** A role-scoped refusal once drew three fault-coloured
blocks on a reader's home screen. Here the vocabulary is fixed and shown side by side in the
gallery: FAILED is the only fact that earns red; MANUAL is neutral; a refusal is ⊘ NOT GRANTED
in `--state-gated`, and a refused page shows the refusal alone — no strip, no tiles, no count.
Likewise OPEN egress renders neutral with the sanctioned hosts enumerated — a configured state,
never an alarm.

**The click-count argument (second week of use).** The compliance reviewer's weekly question is
answered by the strip in zero clicks; locating the one exception is one fixation (the single red
tile) plus one click for its enforcement line. Shipped: scan twelve cards (or sort a table) to
establish "nothing changed", then a separate section for egress and a timeline for config —
three scrolls to say "yes, as claimed".

**States designed** (gallery): fault vs manual vs gated in one frame, whole-read refused, OPEN
boundary with host list, skeleton tiles, failed read that withdraws prior values, the
freshness-unknown sentence, aging observation stamps, and the one-guardian-red strip variant
(`11 of 12 · 1 failed`).

## Knowledge Graph — `graph.html`

**The job.** See the entity and its relations, and get to the evidence.

**The composition change.** The whole viewport becomes the workspace. Where the shipped page
stacked an eyebrow, a display title, a description sentence, a modes row and a truth row above
the canvas, one 48px workbar now carries all of it — place, the Graph|Record mode control, the
depth select, and the served count (`13 nodes · 16 edges`). Round 2 reclaimed ~120px by shrinking
the header; this round removes the header as a concept. Below the workbar, a three-column floor:
entity rail (280px) · canvas · record pane (392px).

- **The canvas is the one sunken surface in the product** — `--sunken` with the well inset, the
  same level vocabulary the audit JSON well uses, so the graph reads as *the material under
  glass* while every control sits on panel above it. Class is shape (the shipped registry:
  Account square · Person circle · Document page · Ticket diamond · Order hexagon — never
  colour), a quiet legend names the five plus the dashed=unconfirmed rule, and selection is the
  one cherenkov spend: the selected node ringed, **its own edges lit and their relationship
  kinds named on the canvas**; every other edge stays at rest. An extracted assertion is a
  dashed edge labelled `ACQUIRED · UNCONFIRMED` — the claim and its assurance in the same
  glance. Zoom controls bottom-left, legend top-right, the server's cap notice top-left.
- **The entity rail is a finder, not a data dump**: search, three compact filter selects, then
  rows of glyph + name + `class · source` (an extracted entity carries ◇ unconfirmed). The raw
  entity key left the rows for the row's `title` and the record pane — nine keys of mono noise
  per screenful was the single biggest legibility cost in the shipped rail.
- **The record pane is docked, not a route change**: node click fills it, the canvas never
  moves. Provenance leads (assurance pill + the source system's own ref as a copyable chip —
  the path to the evidence), then quick properties and quick relationships with per-relation
  UNCONFIRMED pills, then Open full record and the paths finder. Record mode is the same
  entity at reading depth: all properties, all relations each with its provenance ref.
- **Narrow (≤1100px):** the rail folds into a workbar "Find entities" toggle (a focus-trapped
  sheet, as shipped), and the pane overlays the canvas with the pane shadow. The canvas keeps
  the full width; the workbar is never clipped.

**What was removed, and why.**
- The three stacked header rows (eyebrow, h1+description, modes row, truth row) — ~150px of
  standing furniture over the product's signature visual. Their content survives: place in the
  workbar crumb, mode as a segmented control, the truth line as the workbar count.
- The raw key line on every rail row (moved to hover title + pane), and the assurance *word* on
  every row (it remains a filter, a ◇ mark on extracted rows, and a pill in the pane) — the rail
  now shows twice the entities per screenful.

**The click-count argument (second week of use).** The analyst's loop is entity → relations →
evidence. Shipped: select entity → graph draws → click node → drawer opens over a third column
→ "Open full record" → route change to read provenance — three clicks and a navigation to reach
the evidence ref. Recomposed: the pane is standing; one click on any node puts the provenance
chip and the relation list on screen with the canvas still visible, and the second week's habit
— compare the picture with the record — needs zero re-navigation. The kinds named on the lit
edges also answer "how is this connected" with no click at all, which the shipped canvas made
you open the drawer to learn.

**States designed** (gallery): Record mode in full (properties, relations with provenance refs,
paths finder), the rail's six read states (loading / empty / filtered-empty / disabled /
refused-neutral / failed), the canvas's seven (prompt, loading, no-visible-graph, refused, off,
error, dead evidence link), the server's cap notice, and the ~1100px fold.

---

## index.html · states.html

`index.html` is the landing page: all five renders linked, one paragraph of "what changed and
why" each. `states.html` collects the empty / loading / refused / error set for Search, Posture
and the Knowledge Graph in one page (Home and Audit log carry their own galleries inside their
renders, from the earlier run of this round).

## Named divergences from the round-2 CSS baseline (Search · Posture · Graph)

Values untouched; usage changed — continuing the two Home/Audit divergences already recorded
above, which these screens inherit:

- **Search result rows are two-line (~56px), not the 40px table row.** `--row-height` governs
  single-line tables (audit, config history, compare view — all keep it). A result is a name
  plus the text that matched; forcing it into one line is how the shipped page ended up
  headlining hashes. The rank/score gutters keep table discipline.
- **The Graph workbar is 48px, not `--topbar-height`.** It is a toolbar, not a second topbar;
  48px matches the audit toolbar's height vocabulary and returns the difference to the canvas.
- **The chamber's edge/kind labels use `--text-dim`, never `--text-faint`** (measured 9.3:1 dark
  / 5.2:1 light on the sunken canvas). Faint remains confined to the ⌘K keycap hint inherited
  from the shell — the one place it survives, because it duplicates the adjacent placeholder.
- **Term marks in Search** (`<mark>`) use `--selected-tint` plus a cherenkov-deep underline with
  inherited text colour — no new colour enters the palette, and the mark survives both themes at
  body-text contrast.

## Measured contrast (worst pairs, computed over the token values, tints alpha-blended)

Every load-bearing pair clears 4.5:1 in both themes. Worst three per theme:

| Theme | Pair | Ratio |
|---|---|---|
| dark | `--text-faint` ⌘K keycap hint on panel (decorative duplicate, inherited) | 3.59 |
| dark | red FAILED on red-tint tile | 5.47 |
| dark | `--state-affirm` on affirm-tint | 5.49 |
| light | `--text-faint` ⌘K keycap hint on panel (decorative duplicate, inherited) | 3.07 |
| light | green PASSED on green-tint | 4.88 |
| light | dim chamber labels on sunken canvas | 5.19 |
