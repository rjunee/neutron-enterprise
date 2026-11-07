# Neutron Enterprise Console — Redesign Specification

**Status:** authoritative build spec. Supersedes the shipped console page-by-page, the NE-CONSOLE-IA
navigation rename, and every screen-level decision in the "Cold Criticality" redesign (§ references
in source comments). Where this document conflicts with existing code, this document wins.

**Governing inputs, in priority order:**
1. SPEC §11 "CONSOLE INTERACTION DESIGN" — the owner's four rulings (one role-filtered console;
   the console is writable through the governed path; Access ships read+write; Models is a
   destination), plus the ordered additions: Settings, audit export, a command palette.
2. `.impeccable.md` — the three rules this redesign is judged on: **state, don't persuade** ·
   **no repo internals on any screen** · **content is the product, chrome is quiet**.
3. `.audit-findings.md` — 193 audited defects in the current console. Every one of them is resolved
   by this spec, almost all by replacement rather than patching.

**What this is:** a screen-by-screen build spec. Each screen section states the job, the layout, every
interactive affordance, the data contract (field → API route), and the final copy for every visible
string including empty/loading/refused/error states. A build team should be able to build the whole
console from this document without asking a design question.

**What this is not:** a patch list against the current pages. The current pages are the wrong shape —
explanation-first, card-heavy, zero sortable columns anywhere — and are replaced.

---

## 0. The verdict this spec answers

The owner reviewed the shipped console: *"a pitch deck in the form of a web app… not at all what an
admin interface for an enterprise AI system that gets used every day should look like."* After an
approved interaction-design proposal produced only a navigation rename: *"I asked for a full redesign
into a tool that people would use operationally every day."*

Measured, on the deployed console: zero sortable columns in the entire product; Approvals — the
highest-frequency screen — has no table, no filters, no count; Search has no result toolbar; Audit has
no export; a twelve-row compliance annunciator sits beside the chat box.

The reference class is the enterprise admin console — M365 admin center, Okta, AWS, Datadog, Linear —
with better craft. The measure of done is the second week of use: count the clicks to the thing
someone came for.

### Non-negotiables (repeated here because every screen is built against them)

- **Thin client (C7).** The console renders server verdicts. It never recomputes a hash, a verdict,
  a cost, or a permission. Role-filtered navigation is legibility only; the server still refuses,
  and the refused state below is how that refusal renders.
- **No fabricated data.** An unavailable value renders as `—` with a short factual note ("Not
  instrumented"). Never a zero that means "unknown", never a sample row, never an illustration.
- **No repo internals.** No source paths, no `NE-####` ids, no endpoint URLs, no internal setting
  names, no build vocabulary ("this build", "the wire", "the mapper") in any visible string,
  tooltip, or aria-label. Guardian tests enforce this.
- **State, don't persuade.** No sentence on any screen argues that the product is trustworthy,
  explains its own integrity, or narrates what it is *not* doing. The data is the claim.
- **Keep** the token palette (`tokens.css`), light/dark with the existing `data-theme` contract, and
  `--brand-teal` declared once and never themed.

### Vocabulary rules for all copy

- Sentence case everywhere: page titles, table headers, buttons, labels. The only uppercase
  elements are status pills and the 11px group labels in the navigation rail.
- No endpoint names, HTTP verbs, or status codes in visible strings. "409" becomes "already
  decided"; "GET /v1/posture" becomes nothing.
- Relative times in tables ("14 min ago", "2 h ago"), absolute ISO-local timestamp in the `title`
  attribute and in detail panes. Always inside `<time dateTime="…">`.
- Reactor vocabulary only where it names something real; plain words otherwise. "Posture" is the
  page title; "Sovereignty posture" is a slogan and is banned.
- The words "proven", "governed", "sovereign", "verbatim", "honest/honestly", "fabricated", and
  "trimmed" never appear in user-visible copy. They are design constraints, not interface text.

---

## 1. The shell

### 1.1 Structure

```
┌──────────┬──────────────────────────────────────────────┐
│          │ Top bar (48px): ⌘K search · status · account │
│   Rail   ├──────────────────────────────────────────────┤
│  (240px) │ Page: title row + toolbar + content          │
│          │                                              │
└──────────┴──────────────────────────────────────────────┘
```

Two pieces of persistent chrome: a **navigation rail** (left, 240px, collapsible to 56px icon rail
below 1100px viewport width) and a **top bar** (48px). Everything else is page content.

### 1.2 The rail

Contents, top to bottom — and nothing else:

1. **Wordmark** — `NeutronMark` + "Neutron Enterprise", links to `/`.
2. **Deployment name** — `deployment_name` from `GET /config.json`; when absent, the host
   (`window.location.host`) alone, no instruction to configure it anywhere in the UI.
3. **Navigation**, grouped. Group labels 11px uppercase `--type-label`; items sentence case,
   13px, 32px row height:

   | Group | Items → route |
   |---|---|
   | *(none)* | Home → `/` |
   | Workspace | Ask → `/ask` · Search → `/search` · Library → `/library` |
   | *(none)* | Approvals → `/approvals` *(carries the live count badge)* |
   | Operations | Sources → `/sources` · Models → `/models` · Improvement → `/improvement` · Evaluations → `/evaluations` |
   | Governance | Audit log → `/audit` · Posture → `/posture` · Access → `/access` |
   | Insights | KPIs → `/insights` |
   | *(none)* | Settings → `/settings` *(pinned at the rail's foot)* |

   Route renames ship with redirects: `/chat→/ask`, `/knowledge→/library`, `/connect→/sources`,
   `/flywheel→/improvement`, `/evals→/evaluations`, `/kpi→/insights`, `/steward→/improvement`.
   `/trace/:id` remains a routed drill-down with no rail entry.

4. Nothing below the nav. **Deleted from the rail:** the twelve-constraint annunciator (moves to
   Posture), the guided-tour button (moves to the account menu), the theme toggle (moves to
   Settings and the account menu), config hash, chain lamp, air-gap row, `TRIMMED AS:` block.

**Approvals badge:** rendered on the Approvals item from the shared pending poll. Text is
`{count}` alone; when any item is past its SLA the badge takes the overdue tint and the `title`/
accessible name reads `{count} pending, {overdue} overdue`. Refused or unread queue renders no
badge (never a zero for a queue this key was not shown).

**Role filtering** renders the server's verdicts and only those (`visibleSections`/
`railSections`): an item is dropped only on a positive server refusal from `GET /v1/me`
entitlements. While that answer is **in flight**, a surface-gated item's slot renders as a
`.ne-skeleton` loading row (§2.4's vocabulary) — pending is not permitted, so no link is ever
shown and then removed when the verdict lands. A deployment whose answer arrives *without* an
entitlements list renders every item: unknown is not denied. The palette's destination index and
the `g`-chord's reachable set derive from the same filtered sections, so all three surfaces agree
in every state. The server refusal path keeps working for direct URLs (renders the standard
refused state, §2.6).

### 1.3 The top bar

Left → right:

1. **Rail collapse toggle** (icon, `aria-label="Collapse navigation"`).
2. **Search field** — a button styled as an input, placeholder **"Search or jump to… ⌘K"**. Opens
   the command palette (§3). Width ≤ 480px, centered-leaning-left like GitHub/Linear.
3. **System status control** (right cluster). A single compact chip that is *quiet when healthy*:
   - Healthy: a small filled dot + "Operational" — drawn in `--text-dim`, no color alarm.
   - Any degradation (chain break, posture unreachable, connector degraded): the dot takes the
     fault color and the chip names the worst fact, e.g. **"Chain break at entry 4,812"**.
   - Click opens a popover — the one home of global state:

     | Row | Value | Source | Links to |
     |---|---|---|---|
     | Audit chain | `Valid · 4,812 records` / `Break at entry {seq}` | shared audit poll `verification.valid`, `.checked`, `.first_break_seq` | `/audit` (`?seq={first_break_seq}` on break) |
     | Controls | `12 of 12 verified` | `GET /v1/posture` `constraints[].attested` | `/posture` |
     | Network | `Air gap on` / `{n} hosts allowed` | `posture.air_gap`, `egress_allowlist.length` | `/posture` |
     | Configuration | `a41f…c2 · in force since 12 Jun` | `posture.config_hash`, `config_hash_history` (span with `to === null`) | `/posture#configuration` |

     Hash truncated to 8 chars with a copy button (`aria-label="Copy configuration hash"`);
     the full hash lives in `title` and in the clipboard.
   - When the popover's own reads are refused for this key, the affected rows read
     `Not visible to your role` and the chip stays quiet — a role boundary is not a fault.
4. **Account menu** — avatar/initial button, name = principal id. Menu items, in order:
   **Signed in as {id}** (static, with groups as a sub-line) · **Theme: Dark / Light / System**
   (radio) · **Density: Compact / Comfortable** (radio) · **Guided tour** · **Sign out**.

The per-page `StatusStrip`, `PageFrame` purpose line, page foot ("rendered {time} · {principal}"),
and the `Readout` hero strips are **deleted**. A page is a title row and content.

### 1.4 Page anatomy

Every screen uses one of two shells:

- **List screen** (most screens): a 40px **title row** — `h1` (18px `--type-section`) at left,
  primary action button at right — then a **toolbar** (search-within, filters, count, export),
  then the table, then a **pagination row**. No prose between title and toolbar, ever.
- **Work screen** (Ask): the split layout specified on the screen itself.

Detail is always **progressive disclosure**: a row click opens a **detail pane** (right-side panel,
420px, resizable, `Esc` closes, focus-trapped) or navigates to a drill-down route for objects with
their own identity (`/trace/:id`). Detail panes never navigate away from the list; the list keeps
scroll position and selection.

---

## 2. Cross-cutting standards

### 2.1 The table standard

Every list in the product is a real `<table>` built to one standard:

- **Rows:** 36px compact (default) / 44px comfortable. Density is a user setting (account menu +
  Settings), persisted locally, applied product-wide.
- **Type:** 13px `--type-body` cells; identifiers and hashes in `--font-mono`; every numeric and
  time column gets `font-variant-numeric: tabular-nums` and right-alignment (class `ne-num`).
- **Headers:** sentence case, 12px, `--text-dim`, sticky under the toolbar on scroll.
- **Sort:** sortable columns are `<th><button>` with `aria-sort` on the active header and a
  ▲/▼ glyph. Click toggles direction; the default sort is stated per screen. Sorting is
  client-side over the fetched window unless the screen says otherwise; when the window is
  partial, the pagination row says so ("Sorted within the loaded 500 rows").
- **Row affordance:** whole row is clickable (opens detail) with visible focus ring; row hover
  reveals a right-aligned overflow `⋯` menu where a row has secondary actions.
- **Selection:** only screens that declare bulk actions get checkboxes. Default: none.
- **Skeleton loading:** three shimmer rows in the table's own geometry plus visually-hidden
  `role="status"` text "Loading {things}". Never a spinner paragraph.
- **Column resilience:** a null field renders `—` (with a `title` naming why when the reason is
  known and short, e.g. "Not recorded").

### 2.2 The toolbar standard

One 40px row directly above every table: `[search-within] [filter controls…] [spacer] [count] [export]`.

- Search-within is a client-side substring filter over the fetched window, placeholder
  "Filter {things}…", cleared with `Esc`.
- Filters are compact selects/toggle-chips; an active non-default filter renders a dismissible
  chip. The count is always live: **"{n} {things}"**, extended by state where the screen says so
  ("4 pending · 1 overdue").
- **Export** is a quiet button labeled "Export CSV" that exports the currently filtered/sorted
  window with a header row naming the applied filters and the export timestamp. Screens with a
  server-side export say so in their own section.

### 2.3 Status pills

The one state primitive survives, restyled: 11px uppercase word + leading dot glyph, 1px border,
tinted background, `--radius`. **Never color-only** — the word carries the meaning. The closed
vocabulary shrinks to what screens actually render:

`PENDING` `OVERDUE` `EXPIRED` `APPROVED` `DENIED` `AWAITING SECOND` `ESCALATED` ·
`ALLOW` `DENY` · `VALID` `BREAK` · `PASSED` `FAILED` · `OK` `DEGRADED` `SYNCING` `IDLE` ·
`VERIFIED` `MANUAL` · `ACTIVE` `RETIRED` `QUARANTINED` · `UNCONFIRMED` · `UNPRICED`

Colors: affirm `--state-affirm`, caution `--amber`, fault `--red`, neutral `--state-gated`,
success `--green`. A role-scoped refusal is neutral, never fault.

### 2.4 State copy grammar (empty · loading · refused · error)

Four states, one grammar: **fact, then next action, ≤ 15 words, no arguing.** These are the only
templates; per-screen copy below instantiates them.

- **Loading:** skeleton rows + hidden "Loading {things}". Standalone (non-table) loads render
  "Loading…" once, 13px `--text-dim`.
- **Empty:** one sentence naming the fact + one action. E.g. "No pending approvals." No
  explanation of how the feature works — that lives in the guided tour and nowhere else.
- **Refused (403):** "Your role doesn't include {thing}. Contact your administrator for access."
  Rendered as a centered block with a neutral (not fault) pill reading the page name. Never a
  paragraph about audited attempts, fail-closed gates, or what the server would have done.
- **Error (network / 5xx):** "Couldn't load {things}." + a **Retry** button. On write failure:
  "Couldn't save. Try again." with the server's `detail` string, verbatim, as secondary text when
  present.

### 2.5 Governed writes — the pending state is a first-class UI state

Every write in the console goes through the server's governed path (PDP → approval gate → chain).
The UI contract, uniform across screens:

1. The user submits from a detail pane or form. The submit button names the consequence
   ("Request change", "Approve — this will execute").
2. The server's answer is one of: **applied** (tier-0/1, no approval needed), **pending approval**
   (returns a `request_id`), or **denied by policy** (403 with `detail`).
3. **Pending is rendered on the object, not as a toast.** The object's row gains a `PENDING`
   pill and a sub-line: "Change requested by {you} · awaiting approval" linking to
   `/approvals?request={request_id}`. The old value remains the displayed value — the console
   never pre-renders an unapplied change. The pending marker survives reloads because it is read
   back from the server (the pending-change read is part of each write endpoint's contract, §14).
4. Denied renders inline where the submit happened: "Denied by policy." + the server's reason
   verbatim + "View decision" linking to the audit record when the key can read it.
5. When the approval resolves, the object row reflects the server's new state on the next read;
   the console never flips it locally.

### 2.6 Keyboard contract (global)

- `⌘K` / `Ctrl+K` — command palette. `/` focuses the nearest search/filter field.
- `g` then `h/a/s/l/o/m/i/e/u/p/c/,` — go to Home / Approvals / Search / Library / sOurces /
  Models / Improvement / Evaluations / aUdit / Posture / aCcess / Settings. (Palette lists these.)
- In tables: `j/k` or `↓/↑` move row focus, `Enter` opens the detail pane, `Esc` closes it,
  `o` opens the row's overflow menu.
- Screen-specific keys are listed per screen; none may shadow typing in an input.

### 2.7 Numbers, money, hashes

- Tabular figures everywhere a number can be compared vertically.
- Money: `$1,284.06` with the currency the server states; a cost the server reports as `null` or
  as `0.0` for a self-hosted lane renders **Unpriced**, never `$0.00` and never blank (SPEC §11
  honesty trap).
- Token counts ≥ 10,000 group thousands; durations `1.2 s` / `840 ms`.
- Hashes truncate to 8 chars + copy affordance; full value in `title` and clipboard.

---

## 3. The command palette

Opens over any screen (`⌘K`, or the top-bar search). A modal input + result list, 560px, centered
at 20vh.

**Indexes, in rank order:**
1. **Typed object ids** — a query that says WHICH KIND of object it carries routes directly, and a
   grammar match is always the top result, labeled with its type. `approval {uuid}` and `seq {n}`
   take the kind from words the operator typed; `sha256:<64 hex>` is a self-describing content
   address. A bare canonical UUID is deliberately not a grammar at all — approval requests, runs,
   conversations, jobs and processes all draw their ids from `str(uuid.uuid4())`, so that shape
   names five id spaces at once and identifies none of them.
   **One grammar is an exception, and is kept as one.** A bare 32-lowercase-hex string routes to
   `/trace/{id}` on shape alone. `uuid.uuid4().hex` is the mint at a dozen-plus non-test sites in
   id spaces an operator can see — evaluation and extraction `run_id`, memory-promotion
   `proposal_id`, answer-ledger and memory `event_id`, brain `write_id` — so a pasted proposal id
   lands on `/trace/{proposal-id}`, as the top result, labeled "Trace", with nothing on screen
   saying a kind was inferred. It is a retained shape guess, disclosed here rather than described
   as a typed grammar, and it is not a model for a new one.
2. **Destinations** — the rail's items plus in-page tabs (e.g. "Improvement › Digest"). Prefix
   match beats substring; role-refused destinations are omitted, and gated destinations whose
   entitlement answer is still in flight are not yet offered (same filtered sections as the rail).
3. **Actions** — verb entries that jump and focus: "Ask a question", "New search",
   "Export audit log", "Re-verify chain", "Switch theme", "Open guided tour".
4. **Recents** — the last 10 palette selections, shown when the input is empty.

**Rank 1 — one row per grammar `idResults` implements**, each with a worked example. The binding
test holds this table's row count to the module's branch count and executes every query, so a
fifth branch fails here even when it reuses an existing type label. How WIDE each grammar is, this
table does not say; that is the counter-example table's job, row by row.

| Query | Enter goes to | Type label |
|---|---|---|
| `approval 9d4a1c2e-4b8f-4c1a-8e3d-2f7a9b0c5d61` | `/approvals?request=9d4a1c2e-4b8f-4c1a-8e3d-2f7a9b0c5d61` | Approval request |
| `seq 4210` | `/audit?seq=4210` | Audit entry |
| `4f6b0c8d1e2a3b4c5d6e7f8091a2b3c4` | `/trace/4f6b0c8d1e2a3b4c5d6e7f8091a2b3c4` | Trace |
| `sha256:817440fa1e68b8aafae4a7191e34aeaa3105c408b8c1860d93b05c4c54b02587` | `/improvement?proposal=sha256%3A817440fa1e68b8aafae4a7191e34aeaa3105c408b8c1860d93b05c4c54b02587` | Proposal |

Each query below resolves no rank-1 row at all and falls through to the destinations — a
recoverable outcome, unlike a confident route to the wrong screen. The list is a sample of the
edges that matter, not an enumeration of everything the grammars refuse:

| Query | Why it resolves nothing |
|---|---|
| `9d4a1c2e-4b8f-4c1a-8e3d-2f7a9b0c5d61` | a bare `uuid4`: five issuers share the shape and none of them is readable from it |
| `approval 4f6b0c8d1e2a3b4c5d6e7f8091a2b3c4` | naming a kind does not widen it — the id must still be an issued `uuid4` |
| `seq 0` | the audit screen's anchor parser owns anchor validity, and `0` is not an entry |
| `sha256:817440fa` | a proposal id is the whole 64-hex content address; a prefix is a fixture convenience |
| `817440fa1e68b8aafae4a7191e34aeaa3105c408b8c1860d93b05c4c54b02587` | the trace grammar is exactly 32 hex, not any run of hex |
| `4F6B0C8D1E2A3B4C5D6E7F8091A2B3C4` | the trace grammar is lowercase; the wire never serves it uppercase |

Content search (documents, entities, audit full-text) is **not** in the v1 palette — it requires a
server search endpoint the API does not expose; flagged in §14. When a query matches nothing the
palette says so in one static line, and the same line names the typed form that does resolve a
request — a pasted bare UUID lands here by design, and this line is then the only thing on screen.
It never quotes the query back. The line is the `NO_MATCH_COPY` export of
`packages/ne-console/src/rail/paletteIndex.ts`, verbatim:

```text
No matches. Content search runs from the Search screen. An approval request is reached by typing approval <request-id>.
```

`packages/ne-console/src/rail/paletteDocBinding.test.ts` holds this section and that module to
each other: both tables are executed against `idResults`, this section's grammar rows are matched
one-to-one against the module's rank-1 branches, the rank-3 labels are compared with
`PALETTE_ACTIONS`, and the block above with `NO_MATCH_COPY`.

**Keyboard:** type to filter; `↓/↑` move; `Enter` opens; `⌘Enter` opens in a new tab; `Esc`
closes. First result preselected. `aria-activedescendant` listbox semantics.

---

## 4. Home — `/`

**The job.** Every signed-in user, at the start of every session. One question: *what needs me, and
did anything change?* Ten-second scan, then leave. An approver leaves into Approvals; an analyst
leaves into Ask; an admin confirms the tiles are quiet and closes the tab.

### Layout

Top to bottom, all above the fold at 1440×900:

1. **`h1` = the deployment name** — `deployment_name` from `GET /config.json`; fallback: the
   serving host. No tagline, no product name (the product name is in the rail), no purpose
   sentence.
2. **Status tiles** — one row of up to four compact tiles (label 12px dim, value 20px, sub-line
   12px dim). Each tile is a link. A tile whose read was **refused for this role is omitted
   entirely** — no lock explanation on the home screen; a tile whose read **errored** shows `—`
   with sub-line "Couldn't load".

   | Tile | Value | Sub-line | Source | Links |
   |---|---|---|---|---|
   | Approvals | `{n} pending` | `oldest {age}` (omit when 0) | `GET /v1/approvals/pending` (count, min `requested_at`) | `/approvals` |
   | Audit log | `Valid` / `Break` | `{checked} records` / `at entry {seq}` | shared poll `verification` | `/audit` |
   | Controls | `{attested}/{total} verified` | `Air gap on` / `{n} hosts allowed` | `GET /v1/posture` | `/posture` |
   | Sources | `{n} connected` | `{d} degraded` (omit when 0) | `GET /v1/connect/status` | `/sources` |

3. **Waiting on you** — a table (§2.1) merging the queues this key's server-granted surfaces
   feed. Sources: pending approvals (approver surface) and open proposals (steward surface). Max
   8 rows, sorted urgency-first (approvals by `sla_deadline` asc, then proposals by `created_at`
   asc). Columns:

   | Column | Content | Source |
   |---|---|---|
   | Type | `Approval` / `Review` | which queue |
   | Item | `action` (mono) / `artifact.title` | `PendingApprovalView.action`, `StewardProposalView.artifact.title` |
   | From | requester / lane | `principal_id` / `lane` |
   | Age | relative | `requested_at` / `created_at` |
   | Due | countdown or `—` | `sla_deadline` (approvals only) |
   | Status | pill | PENDING / OVERDUE / ESCALATED |

   Row click navigates to the owning screen with that row's detail pane open
   (`/approvals?request={id}`, `/improvement?proposal={id}`). **No in-place approve on Home** —
   there is exactly one decision surface (Approvals), with its confirm and reason machinery;
   duplicating it here re-creates the two-surfaces-disagreeing class of defect.
   Footer link when truncated: **"{n} more in Approvals"** / **"{n} more in Improvement"**.
4. **Recent activity** — ops keys only; omitted entirely otherwise. The last 10 non-refusal
   events from the shared audit poll: Seq · Time · Actor · Action · Outcome, plus link
   **"Open audit log"**. No sort (a 10-row teaser of a sortable screen; the real ledger is one
   click away — that is the documented reason).

**Deleted from Home:** the browser-local "recent asks" journal and its disclaimers, the
"since your last visit" localStorage diffing, closed-lane explanation cards, the inspect drawer,
the deployment-facts band (now the top-bar status popover and the tiles), and the in-flow
first-run tour (the tour remains a dismissible overlay on genuine first run, reopenable from the
account menu, never rendered in the page flow).

### Copy

| State | String |
|---|---|
| Waiting-on-you, both queues granted+empty | `Nothing is waiting on you.` |
| Waiting-on-you, no queue granted | *section omitted entirely* |
| Waiting-on-you loading | skeleton rows + hidden `Loading your queue` |
| Waiting-on-you error | `Couldn't load your queue.` + `Retry` |
| Tile error sub-line | `Couldn't load` |
| Recent activity empty | `No recent activity.` |
| Section headings | `Waiting on you` · `Recent activity` |

---

## 5. Ask — `/ask` (today `/chat`)

**The job.** The analyst, many times a day: ask a question, get a cited answer, open the citation,
and be able to find yesterday's answer again. The citation surviving challenge is the product; the
history is what makes it a tool instead of a toy.

### Layout

Three regions: **history pane** (left, 240px, collapsible), **thread** (center, prose measure
72ch), **source pane** (right, 420px, opens on demand).

**History pane** — top: primary button **"New question"**. Below: this principal's sessions,
newest first; each row = first-prompt excerpt (2-line clamp) + relative time. Selecting a row
loads its messages. Search-within field at top: "Filter questions…".
> **Data contract — NEW ENDPOINT (§18 B2).** The API has create-session and stream-message only;
> no session list, no message history (`POST /v1/chat/sessions`, `POST …/messages`). The pane
> requires `GET /v1/chat/sessions` and `GET /v1/chat/sessions/{id}/messages`. **Until B2 ships**,
> the pane shows only sessions created this visit, with a one-line note at its foot:
> `History isn't stored yet — questions live for this visit only.` The browser-localStorage
> journal from the old Home is deleted, not moved here: a history that silently vanishes on
> another machine is worse than a stated absence.

**Thread** — messages in order. User turns render on a `--panel` block, no role label; assistant
turns render plain, no role label (structure carries the distinction). Streaming shows the text
plus a `▍` cursor — no shimmer animation. During a turn the composer's button becomes **"Stop"**.

- **Citations.** Inline `[n]` chips at the answer spans; after the answer, one compact line per
  citation: `[n] {source_system} · {document_id}` with two hover/focus-revealed actions:
  **"View source"** (opens the source pane) and **"Flag"**. Chips and lines are keyboard
  reachable. `aria-label="View source for citation {n}"`.
- **Source pane.** Loads `POST /v1/brain/retrieve` for the citation's `chunk_id`: chunk text,
  metadata as a key/value list, `Open in source system` (when `source_url` present),
  `View in Library` (when the citation resolves to a graph entity). States:
  loading `Loading source…`; refused `You don't have access to this source.`; error
  `Couldn't load the source.` + Retry.
- **Tool calls** (`ToolTransitEvent` on the SSE stream) render as one-line rows in the thread,
  in sequence: `{name} · Tier {tier ?? "—"} · {Allowed | Denied | Pending approval}`.
  - `deny`: DENY pill + **"View decision"** → `/audit?seq={audit_seq}` (rendered only when
    `audit_seq` is non-null and the key holds ops; otherwise no link). No animation.
  - `pending`: PENDING pill + **"Awaiting approval"** → `/approvals?request={approval_id}`.
    This is the §2.5 first-class pending state: the row keeps its pill for the life of the
    thread; it never converts client-side.
- **Feedback** per finished answer: two quiet text buttons **"Helpful"** / **"Not helpful"**
  (`POST …/signals/thumbs`). After submit: `Feedback recorded`. Failure:
  `Couldn't save feedback — try again`. Flagging a citation posts
  `…/signals/wrong-source`; confirmation `Citation flagged`.
- **Turn failure**: `No answer was produced.` + Retry. **Refused by policy** (server refusal
  event): DENY pill + `Refused by policy.` + the server's reason verbatim.

**Composer** — full-width textarea, placeholder **"Ask a question"**, `Enter` sends,
`Shift+Enter` newline. Below it nothing: no example prompts, no capability list, no retrieval
placard (the `WorkedPrompts` component is deleted).

### Data contract

| Element | Route / field |
|---|---|
| Session create | `POST /v1/chat/sessions` `{agent_name}` → `session_id` |
| Stream | `POST /v1/chat/sessions/{id}/messages` SSE: `token.delta`, `citations`, `tool` (`name/tier/decision/approval_id/audit_seq`), `done`, `error.detail` |
| Citation text | `POST /v1/brain/retrieve` `{chunk_ids}` → `chunks[].chunk.text/metadata`, `citations[]` |
| Feedback | `POST …/signals/thumbs` · `POST …/signals/wrong-source` |
| History | **NEW ENDPOINT B2** |

### Copy

| State | String |
|---|---|
| Empty thread | `Ask a question about your connected sources.` |
| No department claim (422 on session create) | `Your account isn't assigned to a department, so questions can't be routed. Contact your administrator.` |
| Stream error | `The answer stream failed.` + `Try again` |
| Timeout (deadline from config) | `No answer arrived in time.` + `Try again` |
| Refused (403) | standard §2.4 refused block, `{thing}` = "Ask" |

---

## 6. Search — `/search`

**The job.** The analyst who wants the record, not a synthesis: run a retrieval, scan ranked
results, open the underlying chunk, and — in a demo or an access review — run the same query as a
second identity to show server-side trimming.

### Layout

1. **Query row** — full-width input, placeholder **"Search connected sources"**, primary button
   **"Search"**, and a results-size select `10 / 25 / 50` (maps to `top_k`, default 10).
   `Enter` submits. While in flight: inline `Searching… {n}s` + **"Cancel"** (AbortSignal). The
   client deadline comes from deployment config (`search_deadline_ms`), default 180 s.
2. **Results toolbar** (rendered only after a run): count **"{n} results · {t} s"** · sort select
   **Sort: Relevance / Source** · source-system filter chips (derived from the result set's
   citations, client-side) · **Export CSV**. There is deliberately **no recency sort**: chunk
   `metadata` is an opaque map with no guaranteed timestamp; sorting on a field that may not
   exist would silently misorder. If a deployment's metadata later standardises a date field,
   recency joins the select.
3. **Results table** (§2.1). Default sort: relevance (score desc, the server's own ranking).

   | Column | Content | Source | Sortable |
   |---|---|---|---|
   | # | rank 1..n | position | no (identity of the ranking) |
   | Source | `source_system` | matching `citations[]` entry, `—` when unmatched | yes (A–Z) |
   | Document | `document_id` (mono, truncated middle) | `chunks[].chunk.document_id` | yes |
   | Excerpt | 2-line clamp | `chunk.text` | no |
   | Score | 2 d.p., right-aligned | `chunks[].score` | yes |

4. **Detail pane** on row click: full chunk text, metadata key/value list, citation ref,
   **"Open in source system"** (`source_url` when present), **"View in Library"** (graph anchor
   via `anchor_citations`). When the result set carries `graph_paths`, the pane's foot lists
   **"Related entities"** as chips (`display` text, UNCONFIRMED pill when
   `assurance` marks it provisional) linking to `/library?entity={entity_key}`. When
   `provisional_source_count > 0`, the toolbar count extends: `· {n} unconfirmed`.
5. **Compare as a second identity** — toolbar overflow action **"Compare as another user…"**.
   Keeps the existing second-principal OIDC popup flow. Result renders as one table: the union
   of both result sets, columns `# · Source · Document · You · {other id}` where the last two
   are `Returned (score)` or `Not returned`. A one-line caption:
   `Each column is that identity's own result set, filtered by the server.` Exit via
   **"End comparison"**. Popup abandoned past its deadline: `Sign-in wasn't completed.` + retry.

### Copy

| State | String |
|---|---|
| Idle | `Search documents across your connected sources.` |
| Loading | `Searching… {n}s` + `Cancel` |
| No results | `No results for "{query}". Try fewer or different terms.` |
| Error | `Search failed.` + `Retry` |
| Timeout | `The search timed out.` + `Try again` |
| Cancelled | `Search cancelled.` |

Data contract: `POST /v1/brain/search` `{query, top_k}` → `RetrievalResult`
(`chunks[]`, `citations[]`, `graph_paths[]`, `provisional_source_count`). Export CSV columns:
rank, source_system, document_id, chunk_id, score, excerpt.

---

## 7. Library — `/library` (today `/knowledge`)

**The job.** The steward and the challenged analyst: inspect what the knowledge layer holds —
entities, relations, their provenance and confirmation status — and answer "where did this fact
come from" without reading a graph query language.

### Layout — three tabs

**Entities** (default). Toolbar: entity search input **"Find an entity…"** + class filter.
> **NEW ENDPOINT (§18 B8):** entity search by name does not exist — the graph routes require an
> exact key or citation anchor. Until B8 ships, the search input accepts an exact entity key and
> the screen's entry points are the **"View in Library"** links from Agents and Search; the input's
> empty-result copy is `No entity with that key. Open one from a citation in Agents or Search.`

With nothing open, the tab's idle body is the **directory**: the registered zero-argument
verified-answer templates, offered in a `Verified query` picker (titles = each template's own
`description`), run server-side, ACL-trimmed, each row activating its entity record through the
row's own `urn:ne:entity:<key>` citation. A provenance line under the toolbar states what the
picker's entries are (`VERIFIED_AUTHORED` in `knowledge/copy.ts`): verified queries authored for
this deployment and deployed per engagement — **never phrased as an in-product authoring flow;
none exists**. Column display order is the label/identifier rule (NE-LIBRARY-LEGIBILITY): the
template's declared `label_var` column leads, its `citation_var` identifier renders LAST — still
monospaced and copyable (it is the citation anchor the audit chain records); a template declaring
no `label_var` renders its served order verbatim, and no label is invented from another column.
The same rule orders the verified-answer result table in Reference (`displayOrder`,
`knowledge/directory.ts`). A deployment registering no zero-argument template has no browse and
says the Entities-idle line instead.

Selecting an entity renders the **entity record**:
- Header: `display_name` (h2) · class chip (`entity_class`) · UNCONFIRMED pill when
  `assurance` marks it LLM-extracted (`isProvisionalAssurance`), with `title`
  "Extracted by a model and not yet confirmed against a source".
- **Properties** table: Key · Value (`properties`, rendered inert, `—` for empty).
- **Relations** table (from `graphNeighborhood(entity, {hops:1})`): Relation (`edge_kind`) ·
  Direction (→/←) · Entity (`display_name`, link re-anchors) · Source (`source_system`) ·
  Status (UNCONFIRMED pill or nothing). Sortable: Relation, Entity, Source. Default: Relation A–Z.
- Provenance line per row via detail: `source_ref` shown human-readable portion first, full ref
  in `title` (the `parseExtractedSourceRef` treatment).
- Actions: **"Open in graph"** (switches to Graph tab anchored here) ·
  **"Paths to another entity…"** (prompt for a second key → `POST /v1/graph/paths`, results as
  hop chains).

**Graph.** The existing canvas (GraphChamber) survives as the visual explorer: anchor input, hops
control (1–4, server-capped), the truncation state rendered as a toolbar note
`Showing the first {nodes} nodes — narrow the anchor to see the rest.` Node click opens the same
entity record in the detail pane. No simulation-only decoration beyond the canvas itself.

**Reference.** Four read-only sections for the platform architect:
- **Ontology classes** — table: Label · IRI (mono) · Source · Parents. Filter input; count.
  Source versions from `sources` map render as a caption line: `{source} {version}` joined by `·`.
  (`GET /v1/ontology/classes`)
- **Shapes** — table: Shape (IRI) · Target class · Scope. (`GET /v1/ontology/shapes`)
- **Quarantined changes** — when `configured`: table Quarantine id · Created · Graph ·
  Violations (count) · Provenance · Whole-graph? (yes/—). When not:
  `Violation tracking isn't configured on this deployment.` (`GET /v1/ontology/violations`)
- **Verified answers** — when `enabled`: template table Title · Version · Rows limit; row click
  opens a run form generated from `args_schema` (label = field title, validation from
  pattern/maxLength, required from `required[]`), button **"Run"** →
  `POST /v1/verified/invoke`. Result: a table of `columns` × `rows`, each row's citation cell
  linking to `/library?entity={key}` (from `citations[i]`), plus one caption line:
  `replay_match === true` → `Re-executed and reproduced.` · `false` →
  `Re-execution returned a different result.` (with caution tint). Truncation:
  `Showing {returned_rows}{total_rows ? ` of {total_rows}` : ""} rows.` When `enabled: false`:
  `Verified answers are off on this deployment.`

### Copy

| State | String |
|---|---|
| Entities idle (no zero-arg template registered) | `No verified query on this deployment can browse without an input yet — queries are authored per engagement. Open an entity from a citation in Agents or Search.` |
| Verified authored-ness (Entities directory + Reference › Verified answers) | `Verified queries authored for this deployment — each runs on the server, permission-trimmed and replay-checked. New queries are authored and deployed per engagement, not built here.` |
| Entity not found | `No entity with that key.` |
| Graph empty anchor | `Anchor an entity to draw its neighborhood.` |
| Any section loading | skeletons + hidden `Loading {section}` |
| Any section error | `Couldn't load {section}.` + `Retry` |
| Graph off (503) | `The knowledge graph isn't enabled on this deployment.` |

---

## 8. Approvals — `/approvals`

**The job.** The approver. Opens the console because the badge says something is waiting. Needs:
what is pending, who asked, **what exactly happens if approved**, what tier — and a decision in as
few actions as possible, then a receipt. Highest-frequency screen in the product; specified at the
highest resolution and built first.

### Layout

**Title row:** `h1` "Approvals". No purpose sentence, no metaphor, no explanation of the gate —
that lives in the guided tour.

**Toolbar:** tier filter (`All tiers / Tier 2 / Tier 3`) · requester select (distinct
`principal_id` values in the window) · **"Overdue only"** toggle chip · search-within
("Filter approvals…") · live count **"{n} pending · {o} overdue"** (the `· {o} overdue` part only
when o > 0) · **Export CSV**.

**Pending table** (§2.1). Default sort: **Due ascending** — the item about to breach is the item
being worked. All columns sortable except Status.

| Column | Content | Source field | Notes |
|---|---|---|---|
| Action | mono, primary cell | `action` | |
| Tier | numeral | `tier` | right-aligned |
| Requested by | principal | `principal_id` | |
| Requested | relative age (`14 min ago`) | `requested_at` | absolute in `title` |
| Due | countdown `mm:ss` under 1 h, else time/date | `sla_deadline` | past-due cells take the caution tint |
| Expires | time/date | `expires_at` | |
| Status | pill | derived | PENDING / OVERDUE / ESCALATED / EXPIRED / AWAITING SECOND |

Live refresh: the shared 15 s poll refetches the queue; a row decided elsewhere disappears on
refetch (and an open pane for it shows the already-decided state below). New rows appear in sort
position; the toolbar count and rail badge update together — they read the same store.

**No bulk select, by ruling:** each approval is an individually accountable decision with its own
confirm and its own reason; a select-all-approve control is the exact failure a governed gate
exists to prevent. (Bulk *export* is the whole-table Export CSV.)

### The decision pane (row click, `Enter`)

Order, top to bottom:

1. **Header:** `action` (mono, 15px) · `TIER {n}` pill · status pill.
2. **What happens if approved** — the payload block:
   - Target: `target_resource` (mono)
   - Description: `description`, the action's own words — served by the endpoint and
     decision-relevant, so it renders even though the original three-item list predates the
     endpoint existing
   - Parameters: one labelled row per key of `material_parameters`, values rendered exactly as
     sent — never formatted, never interpreted
   - Reversibility: `reversibility`, **derived by the gate from the row's tier** — never the
     stored string (a payload author could otherwise tell an approver a Tier-3 wire is
     "reversible"; see SPEC §11)
   > **Data contract — B1 HAS SHIPPED (PR #381, merged 2026-08-03).** `GET /v1/approvals/pending`
   > still serves exactly `request_id, action, tier, principal_id, requested_at, sla_deadline,
   > expires_at, escalated` and **the payload is deliberately NOT on it** — the owner's ruling is
   > that other approvers may see an entry exists without reading it. The payload comes from
   > `client.readApprovalPayload(request_id)`, a **named-approver-only governed read, and every
   > read is audited.**
   >
   > **REVEAL-ON-DEMAND, NOT FETCH-ON-OPEN.** Because the read is audited, opening a row must not
   > fetch: that would write an audit record every time someone glanced at the queue. The pane
   > shows `Request details aren't shown until you ask. Every read is audited.` and a `Reveal`
   > control. Disclosure state is keyed BY REQUEST ID so a slow response for row A cannot land in
   > row B's pane.
   >
   > *This blockquote previously said the payload was "not on any endpoint" and specified a
   > degraded one-line block. That was true when written and false by the time the screen was
   > built — and the screen was correctly built against it, which is why it shipped reading the
   > payload off the queue listing. A spec that has outlived its own subject is not neutral; it
   > actively produces the defect.*
3. **Unconfirmed facts** — only when `provisional_source_count` is present and > 0:
   `⚠ {n} unconfirmed extracted fact{s} in this request's context.` with the refs listed
   underneath (human-readable portion, full ref in `title`), each linking to its Improvement
   proposal when parseable. When the field is absent from the wire: **render nothing** — no
   "not reported by this build" line. (Threading `provisional_source_count` onto the pending
   view is part of B1.)
4. **Request facts:** Requested by · Requested at (absolute) · Due (absolute) · Expires
   (absolute) · Request id (mono, copy button).
5. **Actions:**
   - **Approve** — primary. Click opens an inline confirm: `Approve {action}? This will
     execute.` with **"Approve"** / **"Cancel"**. Tier 3 first approval adds the line
     `Tier 3 — a second, different approver must also approve.` above the confirm.
   - **Deny** — secondary. Reveals a required textarea, placeholder **"Why are you denying
     this?"**; **"Confirm deny"** stays disabled until non-empty. A denial on a chain read by
     model-risk reviewers does not land with a blank justification.
   - **Tier-3 approve withhold rule** (kept from the current build, restated quietly): while the
     payload is undisclosed (B1 not shipped, or the detail read failed), a Tier-3 card disables
     Approve with the reason string `Approve is unavailable while the request details can't be
     shown.` Deny remains available.
   - Expired requests disable both: status pill EXPIRED, line `This request expired before a
     decision.`
6. **In-flight:** buttons disabled, inline `Submitting… {n}s`.

### The receipt (first-class state, not a toast)

On a decision the row does **not** vanish: it moves to the **Decided** section (below the pending
table) and the pane converts in place:

- Verdict pill (APPROVED / DENIED / AWAITING SECOND) + `by {approver} at {decided_at}` — both
  server-returned, rendered verbatim.
- **Audit link:** the chain seq is resolved from the shared audit poll (`seqForRequest`). While
  unresolved: `Writing to the audit log…` (quiet, `role="status"`). Resolved:
  **"View audit entry"** → `/audit?seq={seq}`. For a key that cannot read the chain: the seq line
  is simply absent — the receipt itself is the record this key holds.
- **"Copy receipt"** — copies request id, action, tier, verdict, approver, decided-at as plain
  text lines. Confirmation: `Copied`.
- `pending_second`: pill AWAITING SECOND + `Approved by you. A second, different approver must
  decide.` The row stays in **Pending** (it still is) with that pill.

**Decided section.** Header `Decided` with count; collapsed by default when empty. Rows: this
session's decisions (from the responses the console received), plus — for ops keys — approval
decisions from the recent chain window (`event_type/action` matching approval decisions, classed
as decisions). Columns: Action · Tier · Decision (pill) · Approver · Decided · Audit entry
(seq link, ops keys). For non-ops keys the chain rows are simply absent — no paragraph about
role gaps.

### Conflicts and errors

| Case | Rendering |
|---|---|
| 409 already decided | In the pane: `Already decided — {server detail}.` The queue refetches; the row leaves the pending table. |
| Write failure | `Couldn't submit the decision. Try again.` + server `detail` verbatim as secondary text |
| Queue load error | `Couldn't load approvals.` + `Retry` |
| Refused | standard §2.4 refused block: `Your role doesn't include approvals. Contact your administrator for access.` |
| Empty | `No pending approvals.` — one line, nothing else. The Decided section still renders when it has rows. |
| Overdue-filter empty | `Nothing is overdue.` + `Show all {n}` |

### Keyboard

`j/k` rows · `Enter` open pane · `a` approve (opens the confirm; a second `Enter` confirms) ·
`d` deny (focuses the reason field) · `Esc` close pane/confirm · `e` export. Keys never fire
while focus is in an input.

### Deleted from this screen

The em-dash Readout strip, the queue notice prose, the per-card "what this queue does not carry"
drawers, the evidence drawers listing wire fields, the "Raise a governed action" demo trigger
(moves to a palette action, §3 — ops-gated, labeled `Run a traced delegation (demo)`), the
`RefusalProof` page, and every string containing an endpoint path or the word "gate".

---

## 9. Sources — `/sources` (today `/connect`)

**The job.** The steward or platform owner, weekly or on an ingestion incident: is every connector
fresh, did any ACL mirror degrade, what got quarantined and why.

### Layout

**Toolbar:** kind filter (distinct `kind` values) · search-within · count `{n} sources` ·
Export CSV.

**Connector table.** Default sort: Source A–Z. Sortable: Source, Kind, Last checked, Newest
record, Quarantined.

Freshness is TWO facts, each under its own name (SPEC §12 item 9): **Last checked** is when the
sync cycle last RAN (`last_checked_age_s` — the latest `connector.sync`/`cdc.ingest` event's age;
advances every cycle whether or not anything changed), **Newest record** is how old the newest
ACCEPTED record is (`cursor_age_s` — on a pull lane the cursor row's last write; on the CDC push
lane the newest `cdc.ingest` batch whose counts say it accepted a record, so a heartbeat/empty/
all-quarantined batch advances only Last checked). A connector polling a quiet source reads
`Last checked: 2 min ago · Newest record: 1 d ago` — both true. The single `Last sync` column this
replaces rendered the record age under a run-time label, so a quiet Salesforce read "Last sync:
yesterday" against a 15-minute cron.

| Column | Content | Source field | Null rendering |
|---|---|---|---|
| Source | id (primary) | `id` | |
| Kind | | `kind` | |
| Last checked | relative, from age | `last_checked_age_s` | `Never checked` (`title`: "No sync cycle has run for this source") |
| Newest record | relative, from age | `cursor_age_s` | `—` (`title`: "No record cursor recorded") |
| Permissions check | OK / DEGRADED pill | `acl_mirror` | `—` (`title`: "No sync recorded yet") |
| Quarantined | count, right-aligned | `quarantined_count` | `—` (`title`: "Quarantine store unreadable") |

`Never checked` is words, not a dash: a lane no cycle has ever run for must be unmistakable next
to one that checked seconds ago and found nothing. A FAILED cycle still shows its run time —
the failure is the DEGRADED pill's fact, not a reason to render the run as absent.

Row click → detail pane: the same fields absolute (`last_sync_seq` shown as
`Sync record: entry {seq}` linking to `/audit?seq={seq}` for ops keys; plain text otherwise).
DEGRADED pane adds one operational line: `The permissions mirror failed its last check. Results
from this source are withheld until it passes.`

**Quarantine section** below the table. Header: `Quarantine — {quarantine_total} items` (or
`Quarantine — total unavailable` when null, `title`: "The quarantine store could not be read").
Table: Reason · Count, sorted count desc. (`quarantine_reasons`.)

This screen is **read-only in v1**: the API exposes no sync-trigger or connector-config write.
"Sync now" is listed in §18 as a candidate, not ordered.

### Copy

| State | String |
|---|---|
| Empty | `No sources connected.` |
| Loading | skeletons + hidden `Loading sources` |
| Error | `Couldn't load sources.` + `Retry` |
| Quarantine empty | `Nothing in quarantine.` |

Data: `GET /v1/connect/status` → `connectors[]`, `quarantine_total`, `quarantine_reasons[]`.

---

## 10. Models — `/models`

**The job.** The platform owner (and, in a demo, the technical buyer): which models this
deployment routes to, what each is allowed to do, and what usage and spend look like — with the
allowed-model policy, endpoint bindings, and air gap **writable through the governed path**
(SPEC §11 ruling 4).

Two tabs: **Policy** (default) and **Usage**.

### Policy tab

**Lanes table.** Default sort: Lane A–Z, then Model.

**Configured, not measured.** Every column here is a read of the frozen gateway settings —
including the two model-identity columns, which exist so `frontier-default` is answerable
without asking a human (the alias alone answers nothing). The serving panel above is the
measured surface: it carries an age and a latency, and it covers only the private lanes
ne-api may probe. These columns are therefore headed **Configured model** and **Declared
window**, and the table carries no timestamp, pill or freshness of its own. A configured
mapping must never render as evidence that a lane is live.

| Column | Content | Source | v1 fallback (before §18 B3) |
|---|---|---|---|
| Model | `model_id` (mono) — the routing ALIAS | B3 `GET /v1/models` | `posture.model_lanes[].model_id` |
| Provider | | 〃 | `posture.model_lanes[].provider` |
| Lane | | 〃 | `posture.model_lanes[].lane` |
| Configured model | what the alias RESOLVES to (mono) | 〃 | `posture.model_lanes[].configured_model` |
| Declared window | configured context window, tokens | 〃 | `posture.model_lanes[].context_window` |
| Endpoint | binding host, credential masked (`•••• {last4}`) | B3 | column omitted |
| Tasks routed | count + chip list in pane | B3 (`TaskRoute`) | column omitted |
| Status | ACTIVE pill / PENDING (governed change awaiting approval) | B3 | column omitted |

Row click → pane: full binding detail, the task routes naming this lane, and the write actions:
**"Edit binding…"**, **"Remove from policy…"**. Above the table: **"Allow a model…"** (primary
action, opens a form: model id, provider, lane, endpoint, credential reference — never a raw
secret in the console).

**Network row** above the table: `Air gap` + ON/OFF state + **"Request change"**. All three
writes follow §2.5 exactly: submit → server answers applied / **pending approval** / denied;
pending renders on the row (PENDING pill + `Change requested · awaiting approval` →
`/approvals?request={id}`); the displayed policy remains the in-force one until the server says
otherwise.

> **Data contract.** Reads and writes are §18 B3 (`GET /v1/models`; governed
> `POST /v1/models/policy`, `POST /v1/models/binding`, `POST /v1/network/air-gap`) — the SPEC §11
> arbiter's "one read router, three governed write endpoints". **`POST /v1/network/air-gap` is
> SHIPPED (NE-AIRGAP-TOGGLE)**: the Network row's "Request change" is live — engage is the Tier-1
> `write.internal.network.egress.restrict` action (applies immediately), disengage is the Tier-2
> `write.external.network.egress.expand` action (parks for approval); the pending read-back rides
> `posture.air_gap_pending` and `posture.air_gap` is the LIVE effective posture (the same
> provider the enforcing egress guard reads per call). The model-policy read router and the
> `policy`/`binding` writes do not exist yet; until they do, the table renders the three
> posture-served columns and those write actions are absent (not disabled — absent).

**Serving panel (SHIPPED — NE-SERVING-STATUS)** below the lanes table: the LIVE observation of
every privately-served lane, from ops-gated `GET /v1/models/serving`. The server (never the
browser — C6/C7) probes each non-frontier lane's configured endpoint and serves a MEASURED
verdict: `SERVING` with the model id the endpoint itself reports (+ its runtime serving window
and engine version), or `UNREACHABLE` / `NO ANSWER` (timeout) / `DEGRADED` (answered wrong) /
`NOT CHECKED` (SSRF-shaped address, never contacted, fail-closed). Every verdict carries
`checked_at` (a cached verdict keeps its original stamp — age is shown, never hidden) and a
failed lane shows `Last answered {ago}` vs `Hasn't answered yet` (served `last_ok_at`). The
served endpoint identity is `scheme://host[:port]` only — rebuilt from the parsed URL, so a
config credential structurally cannot reach the wire. The panel renders NOTHING on a deployment
with no probeable lane, and nothing for a role the server refuses the ops surface (the Home-tile
omission rule); a failed read renders an outage note, never an empty deployment. This panel is
what turns the `private-vllm` string from a label into evidence a buyer can check.

### Usage tab

**Window control:** month select (defaults to current). **Summary tiles:** Calls · Input tokens ·
Output tokens · Spend. Spend sums priced calls only and, when any lane is unpriced, reads
`${total} + unpriced` with `title` "Self-hosted lanes report no market price".

**Per-model table.** Default sort: Spend desc, unpriced rows last.

| Column | Source (B4) | Null/zero rule |
|---|---|---|
| Model · Lane | CostEvent rollup | |
| Calls | | |
| Input / Output tokens | | `—` when the server cannot vouch a total — never a fabricated 0 |
| Spend | `cost_usd` sum | **UNPRICED** pill when the server marks the lane unpriced (self-hosted `0.0`); never `$0.00` |

**Per-user usage is not rendered.** `CostEvent.actor` carries the gateway's service actor, not the
calling principal (SPEC §11 prerequisite). Until principal threading lands server-side, the tab
carries one line where that table would be: `Per-user usage isn't recorded yet.` Rendering it off
the service actor would be fabricated attribution on the screen built to prove measurement.

Export CSV on both tables.

> **Data contract — CORRECTED 2026-08-03.** Usage requires §18 B4
> (`GET /v1/models/usage?window=`) aggregating the **audit chain's per-call provenance**
> (NE-163a: lane, task, model_name, response_model, cost_usd, input_tokens, output_tokens),
> **NOT the CostEvent outbox as originally written.** `ne_events.outbox.prune_acked` DELETES
> acked rows once every registered consumer cursor has passed them — the outbox is freight, not
> a store. Measured live 2026-08-03: **210 outbox rows against 11,175 chain records.** An
> analytics endpoint built on it would have silently lost most of its history. The chain row is
> durable, hash-linked, and byte-equal in content. Until B4, the
> Usage tab body is: `Usage reporting isn't available on this deployment yet.` — one line, no
> preview, no invented rows.

### Copy

| State | String |
|---|---|
| Policy empty | `No models configured.` |
| Loading / error | standard (`models` / `usage` as the thing) |
| Write denied | `Denied by policy.` + server reason verbatim |
| Write pending | `Change requested · awaiting approval` |

---

## 11. Improvement — `/improvement` (today `/flywheel`)

**The job.** The steward, a few times a week: work the proposal queue (what the system wants to
change and why), curate the skill library, and check the weekly digest. Every approve/apply here
is a governed write with a chain record.

Three tabs: **Queue** (default), **Skills**, **Digest**.

### Queue tab

**Toolbar:** lane filter (`content / retrieval / schema / policy / entity_resolution / ontology /
skills`) · status filter (`Proposed / Approved`) · search-within · count `{n} proposals` ·
Export CSV.

**Table.** Default sort: Created desc. Sortable: Lane, Title, Confidence, Priority, Created,
Status.

| Column | Source field |
|---|---|
| Lane | `lane` |
| Title | `artifact.title` |
| Kind | `kind` |
| Confidence | `confidence` (2 d.p., right-aligned) |
| Priority | `priority` |
| Created | `created_at` (relative) |
| Status | pill (`status`: PROPOSED→PENDING, APPROVED) |

**Detail pane:** title · lane · cluster id · confidence · evidence counts (event/signal ids
behind a disclosure) · the **diff** (`artifact.patch` as inert `<pre>`, scrollable, mono 12px;
`pr_body` beneath as inert text — never parsed as markdown/HTML, per the existing structural
guardian) · actions:

- **Approve** (note optional) → `POST …/approve`.
- **Reject** — requires a reason code select (`Incorrect / Unsafe / Duplicate / Stale /
  Out of scope / Other`) + optional note → `POST …/reject`.
- **Record apply** (approved proposals) — field `Applied reference` (required; the PR URL /
  commit / bundle version the human applied) + note → `POST …/apply`. The console never performs
  an apply; the button records that a human did.
- **Skill proposals** (`skill` non-null): the pane renders the candidate field-by-field — name,
  version, description, triggers, suggested labels, provenance line, and the body as inert text
  under the heading `Skill text` with one warning line: `⚠ On apply this text is added to every
  agent that matches it. Review it as untrusted input.` Steward may edit name / description /
  triggers in place (edits re-validated server-side); body edits are not offered — reject
  instead. **Apply** requires the visibility decision: radio `Only these labels:` (label
  multiselect) XOR `Visible to everyone` → `POST …/apply-skill`.

Result of any action: the row updates from the server response (`to_status`), with the review
event's actor/time shown in the pane: `{action} by {actor} at {created_at}`.

### Skills tab

Table (existing columns kept, restyled to §2.1): Name · Source · Version · Status pill
(ACTIVE / RETIRED / QUARANTINED, plus a fault-tinted **TAMPERED** pill when `tampered`) ·
Description · Labels · Fired (count) · Last fired (relative, `—` never). Default sort: Name A–Z;
sortable Name, Status, Fired, Last fired. Search-within.

Row overflow menu: **Retire…** (active) / **Restore…** (retired) — both require a reason field
(`Why?`) and post `…/retire` / `…/restore`. Tampered rows get a pane line:
`Stored content no longer matches its recorded hash.`

### Digest tab

- **This week:** four stat tiles — Proposed · Approved · Rejected · Applied (`totals`).
- **By lane:** table Lane · Proposed · Approved · Rejected · Applied (`by_lane`).
- **Review latency:** `Median wait {median_wait_s→h}` · `p90 {p90_wait_s→h}` ·
  `{pending_count} waiting · oldest {oldest_pending_age_s→h}` — nulls render `—`.
  (`GET /v1/steward/latency`)
- **Skill health** (when `skills` non-null): the rot-card tables verbatim from the server —
  Retirement candidates (Name · Reason · Fired) · Failure flags (Name · Fires · Failed · Ratio) ·
  Trigger overlaps (Phrase · Skills) · Near-duplicates (A · B · Method · Score) ·
  Unknown required tools (Skill · Tools) · Stale proposals (Id · Kind · Name · Age). Absent →
  section omitted.
- Week navigation: `‹ previous week` / `next week ›` via `week_start` param.

### Copy

| State | String |
|---|---|
| Queue empty | `No open proposals.` |
| Skills empty | `No skills in the library.` |
| Digest empty week | `No steward activity this week.` |
| Refused | standard, thing = "Improvement" |
| Action failure | `Couldn't submit. Try again.` + server detail |

---

## 12. Evaluations — `/evaluations` (today `/evals`)

**The job.** The platform owner, before and after a model or config change: did the suites pass,
is the judge healthy, what moved. Visited rarely; must be correct and exportable.

**Toolbar:** suite filter (server-side `suite` param) · search-within · count `{n} runs` ·
Export CSV.

**Runs table.** Default sort: When desc. Sortable: Suite, When, Verdict, Judge error rate.

| Column | Source field | Notes |
|---|---|---|
| Suite | `suite` | |
| Run | `run_id` (mono, middle-truncated) | |
| When | `created_at` (relative) | |
| Verdict | PASSED / FAILED pill | `passed`, server-authoritative |
| Judge error rate | percent, 1 d.p., right-aligned | `judge_error_rate` |
| Judge | `judge_model_id ?? judge_family ?? "—"` | |
| Config | `config_hash` (8 chars, copy) | |

**Detail pane:** run id · golden set hash · rubric versions (k/v) · **Metrics table**: Metric ·
Value · Threshold · Status (from `metrics` / `thresholds` / `statuses`, right-aligned numerics,
row tinted on a failing status). No aggregate "GATE" readout — the per-run verdicts are the fact;
an all-runs conjunction is a CI property, not a deployment one.

Export CSV columns: suite, run_id, created_at, passed, judge_error_rate, judge_model_id,
config_hash, plus one column per metric present in the window.

| State | String |
|---|---|
| Empty | `No evaluation runs recorded. Runs appear after a suite executes.` |
| Filter empty | `No runs for suite "{suite}".` + `Clear filter` |
| Not configured (503) | `Evaluation storage isn't configured on this deployment.` |
| Loading / error | standard (`evaluation runs`) |

Data: `GET /v1/eval/runs?suite=&limit=` → `runs[]` (`EvalRunView`).

---

## 13. Audit log — `/audit`

**The job.** The compliance reviewer, periodically and under stakes: pull the trail for an actor,
an action, or a window; verify the chain stands; export what they pulled, with the hashes, without
special pleading. Ops on incident: find the record, replay the decision, open the trace.

### Layout

**Verdict strip** (directly under the title row, before the toolbar): the chain verdict as a
single line — VALID pill + `{checked} records {verb}` · or BREAK pill +
`Break at entry {first_break_seq}` — plus a quiet **"Re-verify"** button (refetches; ordinary
loading state, no sweep animation). The verdict is `verification` from the same response as the
rows; the console never computes it.

**The verb is the SERVER's `verification.basis`, never a constant.** `full-chain` means an
unbounded `verify_chain` re-hashed every record on that request → "verified".
`anchored-extension` means only the rows appended since a head a full verify blessed (up to
`CHAIN_ANCHOR_MAX_AGE_S` ago) were re-hashed → "covered", and the server's own
`verification.detail` — stating how far the proof reaches — is DISCLOSED by the line:
the line is rendered as a `<button>` (dotted underline, ▾/▴ caret) carrying
`aria-expanded` / `aria-controls` / `aria-describedby`, and it reveals a paragraph below the
toolbar. It is NOT a `title` tooltip; a tooltip is mouse-only, and this sentence is the only
thing qualifying the claim beside it. Under a `full-chain` verdict the line is plain text with
no disclosure — the count is already the whole claim — and so it is under BREAK and
RE-VERIFYING, where the read in hand is routinely a passing one whose `detail` would
contradict the accusation. `windowed` is not a whole-chain reading at all and says so.
/trace's strip renders the same read through the same rule, in the same shape, because it
renders the same poll — and there the qualification is load-bearing, because that line leads
with "Entire audit chain".

**Re-verify asks for the real walk** (`GET /v1/audit/events?full_verify=true`). Every break the
console latches is confirmed off a `full-chain` verdict, and a standing break may not be
withdrawn by a weaker proof — an `anchored-extension` proof never re-reads the prefix, so it
cannot see the tamper it would be clearing. Without the parameter the one control an operator
has to clear a latch could not produce a verdict strong enough to clear it.

**Toolbar:** Actor input · Action input · From / To datetime pickers · window select
(`100 / 500 / 1000` → `limit`) · **Apply** · event-class chips **Decisions** / **Tool calls** /
**Access refusals ({n})** (client-side class toggles; refusals off by default, the chip carries
the hidden count) · count `Showing {visible} of {fetched}` · **Export CSV**.

**Ledger table.** Default sort: Seq desc. Sortable: Seq, Time, Actor, Action, Outcome.

| Column | Source field | Notes |
|---|---|---|
| Seq | `seq` | right-aligned; deep-link target — `?seq=` fetches the page CONTAINING the entry (`anchor_seq` on the ledger read), scrolls to + highlights the row; a jump that cannot land renders a named notice (see the state table) |
| Time | `occurred_at` | relative; absolute in `title` |
| Actor | `actor` | |
| Action | `action ?? event_type` | mono |
| Resource | `resource ?? "—"` | mono, middle-truncated |
| Outcome | `outcome ?? "—"` | |
| Trace | `View` link when `trace_id` | → `/trace/{trace_id}` |

**Break rendering:** on `valid === false`, the row at `first_break_seq` carries a BREAK pill;
rows after it render dimmed with an inline band above them: `Chain break at entry {seq}. Records
after this point failed verification.` There is **no illustrated/simulated break** anywhere — the
ghost-row "what tamper looks like" aside is deleted.

**Row detail pane:** every field of the record as labelled rows — event id, event type, kpi tag,
trace id, config hash, previous hash, record hash (each hash 8 chars + copy, full in `title`) —
and the payload as pretty-printed inert JSON in a scrollable block. Pane actions:
**"Replay decision"** (`GET /v1/audit/replay?event_id=` → renders the replay result inline in the
pane: the event list in the same table grammar, plus `Config hashes: …` and `Context: …` lines)
and **"Open trace"** when `trace_id` is set.

**Pagination.** The read is limit-plus-anchor: `limit` sizes the window; `anchor_seq` positions
it (the page containing that entry, anchor mid-window, clamped at either end of the filtered
set). Under the table: `Showing the most recent {n} records. Narrow with filters or raise the
window.` — or, anchored, `Showing {n} records around|near entry {seq}. …` ("most recent" would
be a false statement about an anchored window). A wire `before_seq` cursor is §18 B5a; when it
ships this line becomes real `‹ Older / Newer ›` paging.

**The `?seq=` deep link fails LOUDLY.** A navigation that does not land highlighted on its entry
renders an amber notice (role=alert) naming which entry failed and why — the reachability verdict
is the SERVER's `anchor` field, never a client row-count inference (C7). Applying new filters
drops the anchor (the reader re-scoped the window); clearing filters keeps it (that is the
recovery when filters excluded the anchor).

**Export.**
- **Export CSV** (ships now): the currently filtered, sorted, fetched window, all columns
  including the three hashes, with a two-line header naming the applied filters and the export
  timestamp. Filename `audit-{deployment}-{from}-{to}.csv`.
- **Full export** is §18 B5b (server-side NDJSON stream over the full filter range, not bounded
  by `limit`) — required for the compliance function; until it ships the Export menu has one
  entry.

| State | String |
|---|---|
| Empty window | `No records match these filters.` + `Clear filters` |
| Loading / error | standard (`the audit log`) |
| Refused | standard, thing = "the audit log" |
| Replay failure | `Couldn't replay this decision.` + `Retry` |
| Anchor past the head | `Entry {n} isn't in the audit log — the log ends at entry {head}.` |
| Anchor missing in-range | `Entry {n} is missing from the audit log, which currently ends at entry {head}.` |
| Anchor filtered out | `Entry {n} exists but the applied filters exclude it.` + `Clear filters` |
| Anchor view-hidden | `Entry {n} is in the loaded window but hidden by the current view filters.` + `Show entry` |
| Anchor malformed | `“{raw}” isn't an audit entry number, so there is no entry to jump to.` |
| Anchor verdict absent, row absent | `Entry {n} couldn't be located in the loaded records.` |

### `/trace/{trace_id}` (drill-down, unchanged role)

Reached from Audit, Ask tool rows, and Approvals receipts — never from the rail. Keeps the
existing assembly rendering (rollup strip, per-model aggregates, span tree, verification line)
restyled to §2 standards: rollup as stat tiles (Events · Duration · Cost — cost `Unpriced` when
null), aggregates as a §2.1 table, the span tree rows at 32px with kind-typed detail on
selection. All strings obey §0 vocabulary (no span-kind jargon in labels; the server's own
`detail` text renders verbatim for unrecognized kinds).

---

## 14. Posture — `/posture`

**The job.** Security/IT/risk, monthly and on incident — and the buyer's architect during
inspection: what is enforced, what state is it in, what changed. This page is where the
compliance annunciator lives now that the rail no longer carries it.

### Layout — three sections, one page, in this order

**1. Controls.** Toolbar: search-within · count `{n} controls · {a} verified`. Table, default
sort: Id (C1…C12), sortable Id, Control, Status.

| Column | Source field | Notes |
|---|---|---|
| Id | `id` | C1…C12 |
| Control | `short` | sentence case |
| Status | VERIFIED / MANUAL pill | `attested` (`mode` in `title`) |
| Enforcement | `enforcement` | see server-copy note below |

Row detail pane: status, mode, enforcement — and **nothing else**. The evidence file list is
deleted from the UI; the wire's `evidence` array is not rendered (paths are repo internals).

> **Server copy change (§18 B9):** the served `enforcement` strings are build-system
> descriptions ("no-egress CI test (NE-073)", "import-linter: no provider SDK outside
> ne-gateway") and violate the internals rule at the source. They must be rewritten server-side
> as operator statements — e.g. C1 `Outbound network blocked and verified at build` · C6
> `No code path can skip the policy check` · C7 `Results are filtered to the user's permissions
> before they are returned`. B9 also adds `last_verified` per constraint; until it ships there
> is no "Last verified" column rather than an invented one.

**2. Network.** Labelled rows: `Air gap` — On/Off · `Allowed hosts` — the `egress_allowlist` as a
one-column table when non-empty, else `None`. One link row: `Model routing is on Models →`
(`/models`) — the model-lanes table leaves this page.

**3. Configuration.** The hash in force as a plate (8 chars + copy, `In force since {from}`),
then the history table: Configuration (hash 8 + copy) · From · To (`—` for in force), sorted From
desc. One-line caption: `Every audit record carries the configuration in force when it was
written.` — this is the single sentence of explanation permitted on this page, because it tells
the reviewer how to use the table.

`attestation_source` is not rendered anywhere.

| State | String |
|---|---|
| Loading / error | standard (`posture`) |
| Models empty (on /models) | see §10 |

Data: `GET /v1/posture` → `constraints[]`, `air_gap`, `egress_allowlist`, `config_hash`,
`config_hash_history[]`, `air_gap_pending` (NE-AIRGAP-TOGGLE: the §2.5 pending marker for a
parked governed air-gap change; `air_gap`/`egress_allowlist` are the LIVE effective posture the
enforcing guard reads, and the runtime posture's timeline is the chain's
`network.egress.changed` events — deliberately NOT a `config_hash` move, see
`ne.api.network_egress`).

---

## 15. Access — `/access` (new screen)

**The job.** The admin answering, from inside the product: who can sign in, what does each role
open, and who can approve a Tier-3 action — today unanswerable anywhere in the console
(SPEC §11 ruling 3). Read **and** write: role assignment routes through the governed path.

> **Entirely §18 B6.** No principals/roles/access endpoints exist (`GET /v1/me` only; SCIM is an
> IdP-provisioning surface behind its own bearer, not a console API). This screen ships with B6.
> Until then the route is absent from the rail and the palette — a screen has no business
> existing before its data does.

### Layout — two tabs

**People** (default). Toolbar: search-within · surface filter (Approvals / Improvement / Ops) ·
count `{n} people`. Table, default sort Principal A–Z:

| Column | Content (B6) |
|---|---|
| Principal | id |
| Groups | chip list, `+n` overflow |
| Opens | surface chips: `Approvals` · `Improvement` · `Ops` — computed by the server from the same role tuples the routes enforce |

Row pane: full group list; per-surface grant provenance (`via group {g}`); write action
**"Change roles…"** — group membership add/remove, submitted as a governed write (§2.5: applied /
pending-with-link / denied). The console never edits the IdP directly; the endpoint is the
governed proxy.

**Roles.** Table: Role (group) · Opens (surfaces) · Members (count). Row pane: member list,
surface mapping. A pinned section at the top of the tab:
**Tier-3 approvers** — the named-approver list the gate enforces, as a table: Principal · Since.
(The gate holds this list today and surfaces it only in rejection errors; B6 exposes it as a
read.) Write: **"Edit Tier-3 approvers…"** — governed, §2.5.

| State | String |
|---|---|
| People empty | `No principals found.` |
| Refused | standard, thing = "Access" |
| Write pending | `Change requested · awaiting approval` |

---

## 16. Settings — `/settings` (new screen)

**The job.** Two distinct kinds of setting, kept visibly apart: what this **person** prefers
(local, instant) and what this **deployment** is configured to do (governed, approved).

### Layout — two sections on one page

**Your preferences** (stored in this browser, applied instantly):
- `Theme` — Dark / Light / System (radio; drives `data-theme` exactly as today).
- `Density` — Compact / Comfortable (radio; §2.1).
- `Guided tour` — button **"Reopen the guided tour"**.

**Deployment** (server configuration):
- `Deployment name` — the name shown in the rail and on Home.
- `Search time limit` — seconds before a search gives up client-side.
- `Service address` — read-only, the API base in force.

> **Data contract.** Today these live in the operator-managed `config.json`
> (`deployment_name`, `search_deadline_ms`, `api_base`) and there is no settings endpoint. v1
> renders the Deployment section **read-only** from `GET /config.json` with one caption:
> `Managed in this deployment's configuration.` §18 B7 (`GET /v1/settings`, governed
> `PATCH /v1/settings`) makes the two writable fields editable with the §2.5 pending pattern.
> (Note the served `config.json` today emits only the four OIDC fields; `deployment_name` and
> `search_deadline_ms` are operator-injected optional keys — B7 also makes the backend the
> source of the name.)
> No other config values are surfaced here until they have a governed write behind them —
> Settings is not a viewer for internal configuration.

| State | String |
|---|---|
| Section headings | `Your preferences` · `Deployment` |
| Save (B7) pending | `Change requested · awaiting approval` |

---

## 17. Insights — `/insights` (today `/kpi`)

**The job.** The business owner, monthly: utilization, proficiency, and business value for the
period — every number a served measurement, every gap a stated gap, nothing argued.

### Layout

**Period control:** a select showing the served period label (`dimensions.period`, `YYYY-MM`).
The API serves only the current month; the select is enabled when §18 B10 (period history param)
ships and until then shows the single period. **Export CSV** beside it.

**Three metric tables** (Metric · Value · Unit), one per group, values right-aligned tabular:

| Group | Tags (`kpi_tag`) |
|---|---|
| Utilization | `utilization.agent_turns` · `tool_calls` · `active_users` · `sessions` · `events_total` |
| Proficiency | `proficiency.eval_runs` · `eval_pass_rate` · `correction_signals_total` · `positive_signals` · `negative_signals` · `negative_signal_rate` |
| Business value | `business_value.hours_saved_floor` |

Rules, replacing every paragraph on the current page:
- A tag absent from the response renders its row as `—` with the note `Not instrumented` — one
  cell, not a headline, not an explanation block.
- `events_total` carries a one-line row note: `Includes this console's own reads.`
- `negative_signal_rate` with a zero denominator renders `—` with note `No signals recorded`.
- A zero value whose sibling witness metric is non-zero in the same window renders the value
  plus note `Attribution gap` (`title` carries the witness: "0 recorded while {n}
  {witness} were counted"). Never a paragraph.
- `hours_saved_floor` renders the served value with unit; its basis note is one line:
  `Floor estimate — configured rate assumptions are applied by the server.` No formula prose.
- Tags outside the known set render in a fourth table `Other` with note
  `Not in the standard metric set`.

Footer line: `Generated {generated_at, local time}`.

| State | String |
|---|---|
| Empty | `No metrics recorded for this period.` |
| Loading / error | standard (`metrics`) |

Data: `GET /v1/kpi/summary` → `kpis[]` (`kpi_tag`, `value`, `unit`, `dimensions.period`,
`window_start`, `window_end`), `generated_at`. Export CSV: tag, value, unit, window_start,
window_end.

---

## 18. Backend work this spec requires (NEW ENDPOINTS)

Every screen above renders only served data; these are the endpoints the screens name. Shapes are
indicative — final wire models follow the existing `wire.py` conventions (snake_case, ISO-8601,
no client-supplied identity, redaction server-side).

| # | Endpoint | Shape | Powers | Priority |
|---|---|---|---|---|
| **B1** | `GET /v1/approvals/{request_id}` (approver-gated) | pending fields + `target_resource`, `parameters`, `reversibility`, `provisional_source_count`, `provisional_source_refs` | Approvals decision pane §8; §2.5 pending markers (a pending change is findable by its target) | **SHIPPED — PR #381, merged 2026-08-03.** Named-approver-only, audited per read; the queue listing stays payload-free by ruling. |
| **B2** | `GET /v1/chat/sessions` · `GET /v1/chat/sessions/{id}/messages` (principal-scoped) | session list (id, first prompt, created); message history with citations | Ask history §5; Home's removed browser journal is not resurrected — this replaces it properly | P0 |
| **B3** | `GET /v1/models` · governed `POST /v1/models/policy` · `POST /v1/models/binding` · `POST /v1/network/air-gap` | `ModelEntry`/`ModelBinding`/`TaskRoute`/`EgressSettings.air_gap` read; writes route PDP→HITL→chain and return applied/pending(request_id)/denied | Models Policy tab §10 | P1 (SPEC §11 arbiter: "one read router, three governed write endpoints"). **`POST /v1/network/air-gap` SHIPPED — NE-AIRGAP-TOGGLE**: PDP-tiered by direction (Tier-1 restrict applies immediately, Tier-2 expand parks), chain-recorded (`network.egress.changed`, atomic with the durable posture register on the audit engine), pending read-back on `posture.air_gap_pending`, enforced per call by the gateway guard in BOTH process roots. The read router and the `policy`/`binding` writes remain unbuilt. |
| **B4** | `GET /v1/models/usage?window=YYYY-MM` | **Audit-chain provenance** rollup by model+lane (NOT the outbox — it prunes; see §10): calls, input/output tokens, cost_usd, `unpriced: bool` per lane; totals | Models Usage tab §10 | P1. **Per-principal grouping SHIPS** — PR #378 merged and threads the calling principal; historical/system calls render as one honest "Unattributed (service)" row, never inferred. The earlier exclusion was written before #378 landed. |
| **B5a** | `before_seq` cursor param on `GET /v1/audit/events` | keyset paging | Audit paging §13 | P1 |
| **B5b** | `GET /v1/audit/export` (ops-gated, NDJSON stream, same filters, unbounded) | full-fidelity export incl. hashes | Audit full export §13 | P1 — compliance-disqualifying gap today |
| **B6** | `GET /v1/access/principals` · `GET /v1/access/roles` · `GET /v1/access/tier3-approvers` · governed `POST /v1/access/assignments` | principals w/ groups + server-computed surface grants; role→surface map; the gate's named Tier-3 list; governed membership writes | Access §15 | P1 (owner ruling 3) |
| **B7** | `GET /v1/settings` · governed `PATCH /v1/settings` | `deployment_name`, `search_deadline_ms` (extensible) | Settings deployment section §16 | P2 |
| **B8** | `GET /v1/graph/entities?q=` (ACL-trimmed) | entity name/class search → `GraphNodeView[]` | Library entity search §7; palette content lookup §3 | P2 |
| **B9** | Posture: operator-language `enforcement` strings; `last_verified` per constraint | server copy + one field | Posture §14; also fixes the internals leak at its source | P1 (small) |
| **B10** | `period` param on `GET /v1/kpi/summary` + served period list | month history | Insights period select §17 | P3 |
| — | `GET /v1/models/serving` (ops-gated) | per-lane live serving observation: state (ok/timeout/unreachable/http_error/malformed/not_probed), endpoint identity (host:port only), endpoint-reported model ids + serving window + engine version, `checked_at`/`last_ok_at` | Models §10 Serving panel | **SHIPPED — NE-SERVING-STATUS.** Server-side probe of non-frontier lanes' configured endpoints (the destination the gateway already sends prompts to — no new egress class), Phase-A SSRF shape gate fail-closed, bounded timeout, short TTL cache that carries the observation's age. |
| — | Candidate, not ordered: connector `sync now` trigger | | Sources §9 stays read-only without it | backlog |

One non-endpoint server item rides B1: thread `provisional_source_count` onto
`pending_approval_view` (the read-model already carries it; the mapper drops it).

---

## 19. Forward compatibility — the agent-orchestration surface

The owner intends long-running project agents with their own native surface ("not just a chat
box"). This IA absorbs it **without restructure**, by construction:

- **Navigation:** "Agents" lands as one more Workspace item (`{label: "Agents", to: "/agents"}`)
  — the rail model is already a discriminated section/item list built for exactly this insertion.
- **Attention:** agent runs needing input become one more source feeding Home's "Waiting on you"
  table (§4) — the table's Type column already namespaces its sources.
- **Actions:** an agent's proposed action is already the product's normal case — it parks at the
  approval gate and appears in Approvals like any Tier-2/3 item. No second decision surface is
  ever built for agents; that is the point of having exactly one.
- **Evidence:** an agent run is a trace; `/trace/{id}` is already the drill-down grammar.
- **Cost:** agent runs emit CostEvents; the Models Usage tab picks them up with a task dimension.

What this spec deliberately does **not** do is design the Agents screen. It reserves the route,
the nav slot, the Home feed contract, and the rule that agent actions decide in Approvals.

---

## 20. Demolition list

Deleted concepts, so no builder preserves them out of caution. Their tests are replaced, not
ported:

- The rail annunciator (twelve constraint rows), rail config-hash plate, rail chain lamp, rail
  air-gap row, `TRIMMED AS:` block, guided-tour rail button, `constraints.ts` fallback rows.
- `PageFrame` purpose lines, `StatusStrip`, page foot ("rendered … · principal"), the `Readout`
  hero-strip pattern and `DampedNumeral`.
- `EvidenceDrawer` and the global Inspect mode (progressive disclosure is the detail pane, always
  available); `RefusalProof`; `ProveOverlay` / PULL-TO-PROVE (citations open the source pane);
  `AwaitingUnit` placeholders (a screen without data renders its stated fallback, §10/§15).
- The SCRAM rod/flash animations, the Cherenkov streaming shimmer, the verification sweep
  animation, the approvals loading needle. Streaming keeps the `▍` cursor; loading keeps
  skeletons; `prefers-reduced-motion` continues to disable what little motion remains
  (`--motion-*` tokens stay).
- `WorkedPrompts`, the Home browser journal (`workStorage`), the "since your last visit"
  localStorage diff, `M365Compare`/E7 (already deleted — must not return), the demo
  delegate button on Approvals (palette action now), the nine-station tour as page content
  (overlay only).
- Every string containing: an endpoint path or HTTP verb/status, a `NE-####` id, a source file
  path, "this build", "the wire", "server-side/client-side", "verbatim", "governed", "proven",
  "fabricated", "honest", or a sentence explaining what the console did *not* do.

---

## 21. Acceptance — how this build is judged

A reviewer should be able to check every box mechanically:

1. Every list screen has: sortable headers (`aria-sort`), a toolbar with count and filter, a
   search-within, an export where §-specified, and documented reasons for any missing sort.
2. Approvals: from a cold load with one pending item, a decision (open → read payload → approve
   → confirm) is **3 interactions** and the receipt with verdict/approver/time is on screen
   without navigation.
3. Guardian greps stay green and are extended: no visible string, tooltip, or aria-label matches
   `/v1/`, `NE-[0-9]`, `\.py`, `\.tsx?`, `GET |POST `, `HTTP [0-9]`, or the banned-vocabulary
   list in §20.
4. Zero fabricated values: every `—` traceable to a null/absent field; no `$0.00` on an unpriced
   lane; no zero rendered from an unread source; no sample/illustrative rows anywhere.
5. Thin client holds: no client-side hash comparison, verdict computation, cost summation beyond
   display arithmetic on served aggregates, or permission trimming; direct navigation to a
   role-hidden route still renders the server's refusal via the standard refused state.
6. Both themes pass 4.5:1 on all text including pills; no state is color-only; every table is
   keyboard-traversable; the palette and `g`-shortcuts work on every screen.
7. The tokens file is byte-compatible except additions; `--brand-teal` remains declared once,
   unthemed.
8. The demo arc (cited answer · permission trap · propose→approve→execute with refusal and
   Tier-3 second approver · prove-it-afterwards · KPI · model switch) is walkable end-to-end on
   the new screens without entering a state this spec does not define.

