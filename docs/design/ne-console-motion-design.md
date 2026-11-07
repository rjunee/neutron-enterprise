# Console motion and visual layer

**Status:** dispatch-ready design. Extends `docs/design/ne-console-redesign-spec.md` — its
non-negotiables (§0), token contract (§21.7), demolition list (§20) and acceptance rules (§21)
govern everything below. Where this document amends the motion law carried in
`packages/ne-console/src/styles/primitives.css` (§0 law 4), §2 states the amended law in full.
Unit ids are M-series; sequencing against the observability plan's U-series is in §8.

**The ask this answers:** next-generation motion — "flowing dots", "apple style animations as
elements appear or disappear" — that is not cheesy, does not distract from the core data, and
slows nothing down. Those three constraints are the design: this console has already been
rejected twice for reading as a pitch deck, and decorative animation is the shortest road back.
Every effect below is judged on the fortieth use of the day, then checked on the first.

---

## 1. Stance — what motion is for here

The console's subject is governed flow: a request enters, a policy decides, a human approves, an
action executes, a hash-linked record is written. Motion in this console exists to make **cause
and consequence** visible at the moment they occur — something happened, this is where it landed,
and it is now part of the record. It is an annunciator, not an ambience. Every proposed
animation must answer three questions: **(1)** which served event or operator action triggers it,
**(2)** what fact it makes legible that the still frame does not, **(3)** what static signal
carries the same fact when motion is unavailable. Fail any one and the animation is decoration;
it is rejected. The owner's "flowing dots" instinct is correct about the domain — this product
is literally a flow with gates — and its honest form is **discrete**: a dot appears because an
event was observed, moves to where the record now lives, and stops. Continuous ambient flow, in
a product whose central claim is that it does not fabricate, is a lie drawn at 60fps.

---

## 2. The law — §0 law 4, upheld and made enforceable

"Nothing decorative moves" **survives**. It was right; it was only under-specified — a comment,
not a contract, and its one existing utility (`.ne-motion-settle { transition: all … }`)
violates the spirit it sits under. The amended law replaces the comment with clauses a guardian
test can hold and a reviewer can quote.

> **Law 4 — Motion is evidence.**
>
> 1. **Caused, or absent.** Every animation starts because a served event arrived or the
>    operator acted. A screen at rest runs zero animations. `animation-iteration-count:
>    infinite` may appear only on the loading skeleton (`.ne-skeleton`); nowhere else.
> 2. **Depicts only what happened.** No synthetic replays of computations the client only saw
>    the result of; no intermediate values that were never true (no count-up numerals); no
>    motion on an empty, fallback, loading-error, or refused state; no activity animation when
>    nothing is active.
> 3. **Compositor grammar.** Entering, exiting, and travelling motion animates `transform` and
>    `opacity` only. Paint properties (`color`, `background-color`, `border-color`,
>    `box-shadow`, `fill`) may animate only as a one-shot pulse of at most `--motion-pulse` on
>    at most one element per triggering event. Animating or transitioning any layout property
>    (`width`, `height`, `top/left/right/bottom`, `inset`, `margin*`, `padding*`, `font-size`,
>    `flex-*`, `grid-*`, `gap`) is a defect. `transition: all` is a defect.
> 4. **A denial never eases.** Refusal, break, and fault states appear instantly at full
>    strength. Affirmative completions settle with `--ease-damped`. `--ease-damped` is the only
>    easing curve in the product; a second curve is a defect.
> 5. **Ceilings.** At most 8 elements animate concurrently; a batch larger than the per-effect
>    cap collapses to one aggregate affordance; any load choreography completes within 800 ms.
> 6. **Reduced motion loses nothing.** The global `prefers-reduced-motion` collapse stays.
>    Every animated signal has a static co-signal (word, glyph, color, count) carrying the same
>    fact — motion is always a redundant channel, never the only one.
> 7. **No state gates on animation.** Data renders in the same frame the client learns it;
>    animation is applied to already-final state. No handler waits for `animationend` or
>    `transitionend` to set application state (removing the animated node itself is the one
>    permitted use, via the shared one-shot utility).

Clauses 1, 3, and the `transition: all` ban are machine-checked (M0 guardian, §7). Clauses 2, 4,
6 are review-checked by quoting the clause; clause 5 and 7 are unit-tested per effect.

**Named existing defect:** `.ne-motion-settle` in `primitives.css` declares `transition: all`.
M0 replaces it with explicit `transform, opacity` and sweeps its call sites.

---

## 3. The token scale — extended, not replaced

The existing scale is correct and is kept whole. Semantic roles, so effects pick durations by
meaning rather than taste:

| Token | Value | Role |
|---|---|---|
| `--ease-damped` | `cubic-bezier(0.2,0,0,1)` | the only curve — critically damped, zero overshoot |
| `--motion-drum` | 40 ms | micro feedback on direct manipulation |
| `--motion-chain` | 60 ms | stagger step between sequenced siblings |
| `--motion-snap` | 90 ms | input feedback; popover/palette appearance; all exits |
| `--motion-rod` | 180 ms | pane travel; the longest any surface takes to arrive |
| `--motion-settle` | 200 ms | a state seating into place (verdicts, receipts) |
| `--motion-pulse` | 400 ms | one-shot event afterglow decay |
| `--motion-breathe` | 4 s | loading skeleton only |

**One addition:** `--motion-transit: 260ms` — travel between two screen regions (the receipt
transit dot, M6, its only consumer). Pane-local travel stays `--motion-rod`. No other token is
minted; a proposal needing a duration outside this scale is proposing the wrong effect.

Asymmetry rule: enter at the effect's stated duration, exit at `--motion-snap`. Leaving must
never be slower than arriving — exits are on the operator's critical path.

---

## 4. Motion inventory

Every effect: trigger event · property · duration/easing · what it makes legible · reduced-motion
fallback · performance note. Anything not in this table does not animate.

### 4.1 Surface enter/exit (detail panes, span pane, decision pane, palette, popovers)

- **Trigger:** operator opens or closes the surface.
- **Motion:** panes enter `translateX(8px)→0` + fade, `--motion-rod`, damped; palette and
  popovers enter `scale(0.98)→1` + fade, `--motion-snap`. All exits: fade, `--motion-snap`.
- **Legible:** origin and belonging — a pane that arrives from the row's side reads as detail
  *of* that row; one that pops reads as a page change and costs the reader their place.
- **Reduced motion:** instant; the pane title names its subject.
- **Performance:** transform/opacity, one element. Content mounts and focus moves in the same
  tick the surface opens — the animation dresses a surface that is already interactive.

### 4.2 Live-table arrival protocol (audit ledger now; Runs after U2)

The answer to "a live poll lands rows without the table jumping under a reader's cursor",
built once at the table layer.

- **Trigger:** the shared audit poll returns events with seq above the highest rendered.
- **Two modes, chosen by reader position:**
  - **At the live edge** (scrolled to top, no hover/focus inside the table, no row selected):
    rows insert as a plain data update — one frame, no height animation — and each new row
    (≤ 5) carries a one-shot seal: 2px left-edge tick + row tint in `--cherenkov` decaying over
    `--motion-pulse` (the paint one-shot clause).
  - **Anywhere else, or more than 5 new rows:** nothing inserts. Rows buffer; a chip
    **"{n} new records"** appears at the table head (`translateY(-4px)` + fade,
    `--motion-snap`). Click or return-to-top flushes the buffer in one frame; flushed rows do
    not seal (the chip already announced them).
- **Legible:** which of 50 visually identical rows just arrived, and that the surface is live —
  without ever moving content under a cursor or scroll position.
- **Reduced motion:** seals off; the chip (static text with a count) is the arrival signal, and
  monotonic seq numbers carry the same fact permanently.
- **Performance:** hard cap 5 concurrent one-shots; zero animation at rest; row insertion is
  reflow-as-data-update, never an animated height. The 500-row answer: the table renders its
  page as always — arrival motion applies to at most 5 elements regardless of batch size.

### 4.3 Rail transit pulse (exists — kept, respecified)

- **Trigger:** `transitEvents` from the shared poll — one per governed event mapped to a nav
  stage; never on first load (no fabricated pulses; already enforced by `useAuditPoll` tests).
- **Motion:** the nav item's dot, opacity `0→1→0`, `--motion-pulse`, one-shot, deduped per poll.
- **Legible:** peripheral awareness of *where* governed activity is happening while the
  operator works elsewhere — the console's heartbeat, at the cost of one dot.
- **Reduced motion:** none needed as motion is redundant here — the facts live in the audit
  ledger and the Approvals badge count.
- **Performance:** ≤ 9 dots by construction (one per mapped route, unique per poll batch).

### 4.4 Approvals badge tick

- **Trigger:** the pending count increases between polls.
- **Motion:** `scale(1→1.06→1)`, `--motion-snap`, one-shot.
- **Legible:** a number that changes silently in the periphery is invisible; one tick marks the
  moment. The badge never pulses continuously — a queue that blinks all day trains the
  approver to ignore it.
- **Reduced motion:** the count itself.
- **Performance:** one element, transform only.

### 4.5 Verdict grammar — settle and arrest

The PDP answer: an allow and a deny feel different before the word is read.

- **Trigger:** a state pill, verdict line, or receipt renders a **newly received** server
  verdict (approval decided, eval verdict, PDP outcome in a trace pane, chat-turn refusal,
  chain verdict transition). Never on re-render of an already-known state.
- **Motion:** affirmative/complete states **settle** — `translateY(3px)→0` + fade,
  `--motion-settle`, damped: the state seats into place. Refusal/break/fault states **arrest** —
  no transition, instant full-strength render. Law 4 clause 4 is this effect's contract: a
  denial never eases.
- **Legible:** pre-verbal outcome. Ease means passed; a hard cut means stopped. The motion
  grammar and the color grammar say the same thing in different channels.
- **Reduced motion:** both instant; StatePill's glyph + word + measured-contrast color carry it.
- **Performance:** one element, transform/opacity (arrest is zero-cost by definition).

### 4.6 Receipt sequencing

- **Trigger:** the decision receipt returns from the server with its chain seq.
- **Motion:** the verdict line settles (4.5), then the **"Recorded in audit log · entry
  {seq}"** line settles `--motion-chain` later.
- **Legible:** order of appearance mirrors order of fact — decision first, record second. The
  product's core claim (decisions become chain records) taught in 260 ms without a sentence.
- **Reduced motion:** static; the text order carries it.
- **Performance:** two elements, one-shot.

### 4.7 Receipt transit dot — the signature effect (M6, taste-gated)

The one deliberate Minority Report note, and the honest form of "flowing dots".

- **Trigger:** the operator's **own** decision receipt returns with a chain seq. Never fires
  from polled events, never more than once per decision.
- **Motion:** a 3px `--cherenkov` dot travels (FLIP, `transform` translate) from the receipt's
  seq line to the rail's Audit log item over `--motion-transit`, fades on arrival; the Audit
  nav dot gives its standard transit pulse (4.3).
- **Legible:** provenance as geometry — *your decision just became that record, and it lives
  there.* First-session pedagogy for the product's central claim.
- **Reduced motion:** the receipt line itself — same fact, with a link.
- **Performance:** one fixed-position element, transform/opacity, zero layout participation.
- **Gate:** ships last, behind owner review on the demo rig. Kill criterion: if it draws the
  eye on the fortieth decision of the week rather than the first, it is deleted — one component
  and one class, no coupling.

### 4.8 Waterfall chronology reveal (trace)

- **Trigger:** a trace assembly renders (load or trace switch) **and** span count ≤ 40.
- **Motion:** each waterfall bar draws `scaleX(0)→1`, transform-origin left, `--motion-rod`,
  staggered **in start-time order** by `min(--motion-chain, (800ms − 180ms) / n)` — the whole
  choreography ≤ 800 ms.
- **Legible:** the reveal order *is* the data: concurrent spans visibly start together, the
  serial chain visibly relays, and the eye is handed the request's actual chronology once —
  which a dense static waterfall makes the reader reconstruct by scanning x-offsets.
- **Reduced motion:** static bars — position and length already encode timing completely.
- **Performance:** transform-only; one-shot on load; > 40 spans renders fully static (the
  answer to a large trace is not "animate 500 bars"); nothing at rest.

### 4.9 Status chip latch transitions

- **Trigger:** the latched chain verdict transitions (ok → break, or an explicit re-verify
  clears a latched break).
- **Motion:** break **arrests** (4.5 — instant fault color and text). A passing re-verify
  **settles**. No animation on steady-state polls.
- **Legible:** grammar consistency — the worst fact in the product obeys the same physics as
  every other refusal.
- **Reduced motion:** chip text and color.

### 4.10 Loading skeleton (exists — kept, bounded)

Opacity breathe, `--motion-breathe`, the one allowlisted `infinite` — permitted because loading
is a state, not rest. Capped at 8 skeleton rows. Reduced motion: static blocks (already
collapsed globally). Streaming answers keep the `▍` cursor: the text itself arriving is the
strongest honest animation in the product, and it needs no dressing.

---

## 5. The visual layer beyond motion

Where "rizz" is static, it survives screenshots, reduced motion, and the fortieth use for free.

- **The ledger spine (M3).** The audit table's seq gutter carries a continuous vertical
  hairline (`--rule-strong`) with a node at each row — the chain drawn as a chain. On a page
  whose served verdict is valid, the spine is unbroken. When `first_break_seq` falls in the
  page, the spine renders a literal 2px gap with a `--red` tick above that row, and stops
  carrying continuity below it. Tamper-evidence becomes something you *see*, not a sentence —
  and it is pure served-verdict rendering, thin-client clean.
- **Critical-path tiering (rides M5).** In the waterfall, bars on the root-to-leaf chain of
  maximum cumulative duration render full-strength; off-path bars render at the dim tier. The
  eye follows the path that made the trace slow. (Display arithmetic over served durations —
  the §21.5 precedent U10 already uses.)
- **Selection that survives a screenshot.** Selected rows: `--panel-raised` fill plus a 2px
  `--cherenkov` left edge — state carried by geometry and fill, not by hover or motion.
- **Liveness without twitching.** A live surface says so in text: the toolbar count line reads
  `{n} records · updated {relative}` from the shared poll's last success. Relative times tick
  as DOM text. Nothing blinks; the arrival protocol (4.2) is the only movement, and only when
  something arrives.
- **Depth discipline.** Exactly two elevations (`--panel`, `--panel-raised`) plus the scrim —
  unchanged. No new shadows, no glow surfaces, no `backdrop-filter` (a per-frame blur tax on
  every scroll). `--glow` remains a focus-treatment token only. `--brand-teal` remains declared
  once, unthemed, used only by the mark.
- **Palette untouched.** Both themes, existing contrast guarantees, chroma-as-evidence — all
  standing. This document adds zero colors.

---

## 6. Deliberately not doing

Each entry names its reason so it is not re-proposed as novelty in three months.

1. **Continuous flowing dots / particles along lines.** The console observes discrete events
   through a poll. Ambient flow animates activity that is not being observed — fabricated
   activity in a product whose claim is that it does not fabricate. The honest forms ship:
   discrete transit pulses (4.3) and the receipt transit (4.7). *(Law 4 cl. 1–2.)*
2. **A verification sweep travelling the chain.** The server verifies as a unit and reports a
   verdict; the client never observes a row-by-row scan. Animating one depicts a timeline that
   did not happen. The redesign spec already demolished this animation (§20); the ledger spine
   (§5) carries the same meaning statically and truthfully. *(cl. 2.)*
3. **Count-up / odometer numerals.** An intermediate frame showing 47 while the value moved
   12→96 renders a number that was never true. `DampedNumeral` is demolished and stays
   demolished; changed figures get one tick (4.4), not a lie per frame. *(cl. 2.)*
4. **Spring, bounce, elastic easing.** Overshoot signals overshoot. A governed system is
   critically damped; one curve exists. *(cl. 4.)*
5. **Route/page transitions.** An operator navigates hundreds of times a day; navigation is
   instant. Continuity is the panes' job (4.1), not the pages'.
6. **Hover transitions.** Delay dressed as polish. Hover states switch instantly — this is
   most of what "snappy" means at the feel level.
7. **Glow, bloom, neon edge effects.** The demolished Cherenkov shimmer, re-proposed. Chroma
   is evidence in this system; a glowing chrome spends the alarm channel on decoration.
8. **3D, perspective, parallax.** Minority Report is gesture theater for a standing operator;
   this is software an approver opens twenty times a week on a laptop. We take its *subject*
   (data with trajectory and consequence), not its staging.
9. **Animated empty states, loading-error states, or refused states.** Motion on a screen with
   no data is the pitch-deck failure in one frame. Fallbacks are static by law. *(cl. 2.)*
10. **A continuously pulsing pending badge.** Attention is spent on change, not on existence
    (4.4).
11. **Animated chart draws (U9 and after).** Charts follow the dataviz standard and render
    static; a bar that grows on load is a count-up in disguise. *(cl. 2.)*
12. **A parallel motion vocabulary.** No per-screen easings, no component-local duration
    constants. The token scale of §3 is the entire vocabulary; the guardian bans raw durations.

---

## 7. Performance — budgets and how each is checked

| Budget | Rule | Check |
|---|---|---|
| Input path | Zero animation blocks input or gates state; acted-on state renders same-tick | Unit tests mount surfaces and assert content/focus available synchronously (M1); guardian bans `animationend`/`transitionend` state handlers outside the shared one-shot utility (M0) |
| Properties | `transform`/`opacity` for all continuous/enter/exit motion; paint one-shots per law cl. 3; **layout animation is a defect** | Motion guardian statically parses every production `.css`: transition/keyframes declarations naming a layout property fail; `transition: all` fails (M0) |
| Idle cost | A console at rest runs **zero** animations — no core burned, no battery drawn | Guardian: `infinite` outside `.ne-skeleton` fails (M0). Manual QA (demo-rig runbook): 10 s idle DevTools performance capture on `/audit` with the live poll shows no animation frames between polls; `document.getAnimations().length === 0` at rest |
| Concurrency | ≤ 8 elements animating at once; per-effect caps: 5 row seals, 9 rail dots, 40 waterfall bars | Unit tests bidirectional: 6-row batch → chip only + zero seals; 3-row at-edge → 3 seals + no chip (M2); 41 spans → zero animated bars; 40 → animated (M5) |
| Choreography | Any load sequence ≤ 800 ms; one-shots ≤ `--motion-pulse` | Stagger-math unit test (M5); durations must resolve through `--motion-*` tokens — guardian bans raw `ms` in animation/transition shorthand outside `tokens.css` (M0) |
| Big tables | A 500-row table animates at most the per-effect cap, ever; insertion is a one-frame data update, never an animated height | M2 cap tests; layout-property guardian makes animated height structurally impossible |
| Frame rate | All shipped effects are compositor-eligible by construction | Follows from the property budget; manual spot-check on the demo rig (FPS meter during: poll landing 5 rows, pane open, 40-span trace load) |

Honesty note on tooling: jsdom does not run animations, so the automated layer proves the
*structural* budgets (properties, infinity, caps, gating) and the runtime frame-rate claim is a
documented manual check on the demo rig. That split is stated here so nobody mistakes green
guardians for a measured 60fps.

---

## 8. Build plan

Sizes: S ≤ 1 day, M ≤ 3 days. Every unit's mayTouch includes
`packages/ne-console/src/App.test.tsx` (route assertions) and the new
`src/test/motion-guardian.test.ts` once M0 lands. Guardian tests follow the repo idiom
(`redesign-guardian.test.ts`): fs-walk + self-tests that bite a planted violation and spare a
legal shape, bidirectional per the vacuous-guard rule.

Sequencing principle (the owner's "once we have parity"): **no motion unit lands on a surface
whose data view has not merged.** Audit, Approvals, Trace, Evaluations and the shell are
shipped, so most M-units are unblocked today; the two exceptions are named.

**M0 (S) — the law, the guardian, the defect.** Amend the law comment in `primitives.css` to
§2's text; fix `.ne-motion-settle` (`transition: all` → `transform, opacity`) and sweep call
sites; add `--motion-transit` to `tokens.css`; ship `src/test/motion-guardian.test.ts` with the
four static rules of §7 plus self-tests (planted `transition: width` bites; legal
`transition: transform` passes; planted `infinite` outside skeleton bites; `--motion-breathe`
on `.ne-skeleton` passes; planted `animation: x 300ms` raw-duration bites; token-resolved
passes). *Blocked on: nothing. This is unit #1 — everything else cites its guardian.*

**M1 (S) — surface enter/exit grammar (4.1).** Detail/span/decision panes, palette, popovers,
status popover. Guardian additions: synchronous-mount tests (content queryable in the same
render as open); exit duration asserted `--motion-snap`. *Blocked on: M0.*

**M2 (M) — live-table arrival protocol (4.2) on `/audit`.** Buffer, chip, at-edge seals, caps,
flush. Built at the table/page layer so `/runs` consumes it unchanged. Guardian: the §7
concurrency tests; interacting-reader test (hover/selection forces buffer mode even at top);
flush is one frame (no per-row seals on flush); reduced-motion serves the chip path.
*Blocked on: M0. `/audit` is shipped.*
**M2b (S) — the same protocol on `/runs`.** *Blocked on: U2 (Runs screen, observability plan).
Dispatch only after U2 merges — an arrival protocol on an unbuilt table is motion before data.*

**M3 (S) — the ledger spine (§5) on `/audit`.** Static. Guardian bidirectional: page fixture
with `first_break_seq` in range renders exactly one spine gap at that seq; valid-verdict
fixture renders zero gaps; the gap never renders from any client-computed comparison (grep: the
spine component reads only served `verification`). *Blocked on: M0.*

**M4 (S) — verdict grammar (4.5, 4.6, 4.9) + badge tick (4.4).** Guardian bidirectional: every
refusal/break state class carries **no** settle animation (planted settle-on-deny bites); affirm
states carry it; settle fires only on newly-received verdicts (re-render of a known state
animates nothing); receipt line order pinned. *Blocked on: M0. Approvals is shipped.*

**M5 (M) — waterfall chronology reveal + critical-path tiering (4.8, §5).** Guardian: the
40-span cap both directions; stagger-sum ≤ 800 ms unit test; reduced-motion renders static
bars; tiering arithmetic unit-tested against a fixture whose critical path is not the first
child (the naive-implementation trap). *Blocked on: M0. `/trace` is shipped; U3/B12 latency
enriches the bars it animates but is not a hard dependency.*

**M6 (S) — receipt transit dot (4.7).** Last, deliberately. Guardian: fires only from the
operator's own decision receipt (a polled approval event fires nothing — bidirectional); once
per decision; the dot is `position: fixed` and participates in no layout. Ships behind an
owner screening on the demo rig with the kill criterion of 4.7 attached. *Blocked on: M0, M4.*

**Order of dispatch:** M0 → M1 + M4 (parallel) → M2 + M3 (parallel) → M5 → M2b (gated on U2) →
M6 (gated on owner taste). Nothing else in this plan waits on the observability waves; Usage
(U7) and the U9 chart adopt the grammar of M1/M4 as ordinary consumers and get no bespoke
motion.

---

## 9. Acceptance

1. The motion guardian is green, and stays green under its own self-tests.
2. Redesign-spec §21 holds unchanged on every touched screen (both themes, keyboard, no
   fabricated values, thin client).
3. Every shipped effect appears in §4 with its trigger; any animation found in the product but
   not in §4 is a defect by definition.
4. A reviewer can reject a bad animation by quoting one clause of Law 4; a builder can
   implement any §4 effect from its entry without asking a design question.
5. The demo beat is a side effect, not a goal: open Approvals → decide → watch the verdict
   settle, the record line land, and (if M6 survives its gate) the decision travel to the
   ledger — and an idle console, left open on `/audit` for the rest of the day, draws nothing
   at all.
