# ne-console

The Neutron Enterprise web console — the platform's primary user-facing surface
(NE-061). A React 19 + TypeScript + Vite single-page app: OIDC login, an agent
chat view with streaming responses and inline citations, and a brain-search
view. It is a **thin client** over the governed backend HTTP API.

## Thin-client security model (C6 / C7)

The console has **zero direct data access**. It never talks to Qdrant, Postgres,
a vector store, or any data plane; it does no embedding, retrieval, ranking, or
ACL logic. Every byte it renders arrives from the **governed backend HTTP API**
(ne-serve / ne-gateway) through the single `src/api/` module, so C6 governance,
C7 ACL-trimming, and audit are all enforced **server-side**.

> **HARD RULE (SPEC §5 NE-061 dep note):** brain-search / console MUST NOT
> surface any non-fixture / untrimmed corpus — it renders ONLY what the
> C7-trimmed API returns for the logged-in principal; citations show ONLY
> sources the trimmed results include (a user must never see a document /
> citation they lack ACL for).

This is enforced structurally, twice:

- **ESLint guardian** (`eslint.config.js`): `fetch(`, `new EventSource`,
  `new WebSocket`, and `XMLHttpRequest` are lint errors **outside `src/api/`**.
- **Structural guardian test** (`src/test/thin-client-guardian.test.ts`): an
  fs-walk proves network primitives occur only under `src/api/`, no client
  secret is embedded, no data-plane endpoint string exists, and the governed
  API base env var is read only in `src/api/config.ts`.

The typed `ChunkView` deliberately **omits** the server-side `acl` field: the
client never receives, sees, or evaluates ACLs. Citation click-through goes
through the principal-scoped `/brain/retrieve` seam and renders an
access-denied message (never content) when the trimmed API returns nothing.

Auth is OIDC **Authorization Code + PKCE (S256)** against the platform IdP
(dev: Keycloak realm `ne-dev`, public client `ne-console`). The browser never
holds a client secret; the access token scopes every API request via the
`Authorization: Bearer` header (never a query-param token).

## Design system (NE-130)

The flagship "Cold Criticality" interface is built on a **token layer** — the
single point of aesthetic change. Every component styles **through** CSS custom
properties in `src/styles/tokens.css`; no component hardcodes a color, a px
font-size, or a font-family.

- **Token layer contract.** `src/styles/tokens.css` holds the palette, type
  scale, spacing, and motion tokens (design §1.1–§1.4). Aesthetic changes are a
  token-layer edit — "aesthetic changes should just be a simple layer on top."
- **Two themes, `data-theme` wins both ways.** Dark "Control Room" is the
  `:root` default; light "Daylight Inspection" applies via
  `@media (prefers-color-scheme: light)` scoped `:root:not([data-theme="dark"])`
  **and** an explicit `:root[data-theme="light"]` block — so an explicit theme
  overrides the OS preference in both directions.
- **Self-hosted fonts (zero external requests — by construction).** Source Sans 3
  and Fira Mono are vendored woff2 under `src/assets/fonts/` with
  `@font-face` + `font-display: block` in `src/styles/fonts.css`. There is no CDN
  or Google-Fonts link anywhere (C1/C2). Re-vendor with
  `node scripts/vendor-fonts.mjs`; the fonts are NOT in the npm closure (OFL-1.1
  is out of the npm license policy's free list — see `NOTICE`).
- **Token-hardcode guardian.** `src/test/token-hardcode-guardian.test.ts`
  fs-walks the production sources and fails the build if any component `.css` /
  `.tsx` (outside `tokens.css`) carries a raw hex, an `rgb()/hsl()/…` color, a px
  font-size, or a non-`var(--font-…)` font-family. Palette regression cannot
  merge.
- **One shared audit poll (§6.7 discipline).** The rail's transit pulses and the
  chain-verdict lamp read a single `AuditPollProvider` (default 5s, backoff on
  403/5xx) — never add per-component polling. A 403 (non-ops principal) degrades
  to a hollow "REQUIRES OPS KEY" state; no crash, no fake data.
- **One shared posture load (§6.7, fetch-once).** The `/posture` board, the `/`
  sovereignty hero, and the containment rail's C1–C12 lamps / AIR_GAP / config_hash
  plate all read a single `PostureProvider` that loads `GET /v1/posture` ONCE on
  mount (config_hash + attestation change only on redeploy — no polling; the audit
  poll stays the single poll). Loading/error render hollow, never lit.
- **Honest unattested/awaiting states (§0 law 5).** Surfaces whose ne-api route has
  not shipped render honestly: as of NE-131 the C1–C12 annunciators, AIR_GAP/egress,
  and config_hash plate are LIVE from `GET /v1/posture` (proven when the server's
  drift-guarded `attested` flag is true),
  and fall back to hollow when posture is loading/unreachable. **No byte counter is
  ever rendered** — the posture payload carries no egress-byte measurement, so the
  rail/board show the sanctioned-egress allowlist / SEALED state instead of a
  fabricated "0 BYTES" (§0 law 5).
  As of NE-132 the `/connect` Connectors view is LIVE from `GET /v1/connect/status`:
  the 8 canonical connector lanes (an unconfigured one shown as honest NOT
  CONFIGURED — IDLE, never fake-green) + the quarantine vessel, refetched off the ONE
  shared audit poll and pulsing `--red` once on a real quarantine increase. Nothing
  is simulated.

## Navigation (NE-CONSOLE-IA)

The rail carries six destinations, a work structure rather than a pitch order:
**Home** (everyone, every session — what is waiting on you), **Workspace**
(Ask · Search · Library — the knowledge worker, many times a day),
**Approvals** (the approver, daily when active), **Operations** (Sources ·
Models · Improvement · Evaluations — the platform owner + steward, weekly),
**Governance** (Audit log · Posture — security/IT/risk, monthly and on
incident), and **Insights** (KPI — the business owner, monthly). The model —
`src/rail/navSections.ts` — is a section that is either a direct link (Home,
Approvals) or a group of children, which absorbs a future surface (e.g. a
long-running-agent workspace) as one more row without a restructure.

**Role-filtered, not two products.** `visibleSections()` hides a section or
item only when the server's own `GET /v1/me` `entitlements` list positively
refuses its surface (`src/auth/entitlements.ts`'s `ROUTE_SURFACE` map); an
operator sees additional destinations, never a different console, and a
deployment that sends no entitlements list renders every destination exactly
as it always has (unknown is not denied). This is a legibility affordance
only — the server still refuses every request on its own terms regardless of
what the rail shows (C7); `src/test/nav-refusal.test.tsx` proves a principal
who reaches a hidden route gets the server's own refusal, not a client-side
block.

Run trace and provenance are drill-downs, opened from an answer, an approval,
or an audit row (`/trace`, `/trace/:traceId`) — the rail carries no node for
them. `/models` exists as an honest empty destination under Operations; its
content is a separate unit.

## Backend API contract (ne-api)

The console consumes the following governed endpoints (all under the relative
`API_BASE`, default `/api`; every request carries `Authorization: Bearer`).
The `ne-api` backend that implements this contract is **not yet built** — it fell
through the gap between the §4 topology and the work-item plan and is now planned
as **NE-104** (it was NOT built by NE-070/NE-077, contrary to the original note here).
Phase-5 tests run against an MSW mock that replays these shapes. Wire types are snake_case and
mirror the frozen `ne-contracts` models (`Citation`, `ScoredChunk`,
`RetrievalResult`, `AgentRunResult`).

| Method & path                             | Request                                   | Response                                                                                                                                                                                                                                                                                                                                  |
| ----------------------------------------- | ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /v1/me`                              | —                                         | `PrincipalView { id, groups[] }`                                                                                                                                                                                                                                                                                                          |
| `POST /v1/chat/sessions`                  | `{ agent_name }`                          | `ChatSession { session_id, agent_name }`                                                                                                                                                                                                                                                                                                  |
| `POST /v1/chat/sessions/{id}/messages`    | `{ prompt }`                              | `text/event-stream` of `StreamEvent` frames                                                                                                                                                                                                                                                                                               |
| `POST /v1/brain/search`                   | `{ query, top_k }`                        | `RetrievalResult { chunks[], citations[] }` (already C7-trimmed)                                                                                                                                                                                                                                                                          |
| `POST /v1/brain/retrieve`                 | `{ chunk_ids[] }`                         | `RetrievalResult` (principal-scoped; empty ⇒ no access)                                                                                                                                                                                                                                                                                   |
| `GET /v1/approvals/pending`               | —                                         | `{ approvals: PendingApprovalView[] }` (server-scoped Tier-2/3 queue, payload-free)                                                                                                                                                                                                                                                       |
| `GET /v1/approvals/{id}/payload`          | —                                         | `ApprovalPayloadView` (owner ruling 2026-07-31: governed, audited, named-approver-only disclosure) or a typed `ApiError` (403 detail rendered verbatim)                                                                                                                                                                                   |
| `POST /v1/approvals/{id}/approve`         | `{}` (NO approver — server derives it)    | `ApprovalActionResult { status, approver?, decided_at? }`                                                                                                                                                                                                                                                                                 |
| `POST /v1/approvals/{id}/reject`          | `{ reason }` (NO approver)                | `ApprovalActionResult` (UI verb: Reject)                                                                                                                                                                                                                                                                                                  |
| `GET /v1/audit/events`                    | `?actor&action&from&to&limit`             | `AuditEventsPage { events: AuditRecordView[], verification: ChainVerificationView }`                                                                                                                                                                                                                                                      |
| `GET /v1/audit/replay`                    | `?trace_id \| ?event_id`                  | `ReplayResultView { events[], config_hashes[], context_ids[] }`                                                                                                                                                                                                                                                                           |
| `GET /v1/eval/runs`                       | `?suite&limit`                            | `{ runs: EvalRunView[] }` (per-metric value/threshold/status, server `passed`)                                                                                                                                                                                                                                                            |
| `GET /v1/kpi/summary`                     | —                                         | `KpiSummaryView { kpis: KpiEntryView[], generated_at }` (SCAFFOLD — NE-068 U-P-B)                                                                                                                                                                                                                                                         |
| `GET /v1/posture`                         | —                                         | `PostureView { constraints[], air_gap, egress_allowlist[], model_lanes[], config_hash, config_hash_history[], attestation_source }` — the C1–C12 attestation (server-real `attested`), effective AIR_GAP/egress, model lanes, and the frozen config_hash + its audit-derived timeline (NE-131)                                            |
| `GET /v1/connect/status`                  | —                                         | `ConnectStatusView { connectors: [{ id, kind, cursor_age_s, acl_mirror, last_sync_seq, quarantined_count }], quarantine_total, quarantine_reasons[] }` — per-connector freshness, fail-closed ACL-mirror state, last-sync audit seq, and quarantine counts + sanitized reason categories (NE-132). Nullable = honestly absent (§0 law 5). |
| `GET /v1/steward/proposals`               | —                                         | `{ proposals: StewardProposalView[] }` (server-scoped, non-terminal proposed\|approved)                                                                                                                                                                                                                                                   |
| `POST /v1/steward/proposals/{id}/approve` | `{ note }` (NO actor — server derives it) | `ReviewEventView` (drives NE-066's `ProposalReviewService.approve`)                                                                                                                                                                                                                                                                       |
| `POST /v1/steward/proposals/{id}/reject`  | `{ code, note }` (NO actor)               | `ReviewEventView` (drives `ProposalReviewService.reject`; feeds the rejection→signal loop)                                                                                                                                                                                                                                                |
| `POST /v1/steward/proposals/{id}/apply`   | `{ applied_ref, note }` (NO actor)        | `ReviewEventView` (drives `ProposalReviewService.record_apply` — RECORDS a human apply, never performs one)                                                                                                                                                                                                                               |
| `GET /v1/steward/digest`                  | `?week_start`                             | `StewardDigestView { week_start, week_end, generated_at, totals, by_lane[] }` (server-computed, deterministic)                                                                                                                                                                                                                            |
| `GET /v1/steward/latency`                 | —                                         | `StewardLatencyView { median_wait_s, p90_wait_s, decided_count, pending_count, oldest_pending_age_s, generated_at }`                                                                                                                                                                                                                      |

`StreamEvent` (SSE `data:` JSON, routed by the `event:` field):

- `{ type: "token", delta }` — append to the in-flight assistant message.
- `{ type: "citations", citations: Citation[] }` — inline citations.
- `{ type: "tool", name, tier, decision, approval_id?, audit_seq }` — NE-137: one
  governed tool call transiting the C6 executor. Feeds the inline PDP lane. The
  `decision`/`tier`/`audit_seq` are SERVER-derived from the REAL PDP/gate/executor
  audit events — the console **renders** them, never decides allow/deny.
- `{ type: "done", session_id, output }` — finalize.
- `{ type: "error", detail }` — surface an error.

`Citation` adds one server-side enrichment over the contract model:
`source_url?` — a click-through deep link the API resolves; it is never
constructed client-side.

### Chat/search flagship (NE-137)

- **Inline PDP lane** (`src/chat/PdpLane.tsx`). A thin strip under the composer;
  each governed tool call is a card transiting **SOURCE → PDP → TIER → EXECUTE**
  from the `{ type: "tool" }` frames. `allow` completes the track to EXECUTE with a
  `--green` verdict lamp; `deny` is the **SCRAM** — a control rod drops over the card
  (`--motion-rod`, one-frame `--red` flash) with the verbatim verdict
  `"DENY — no bypass path exists"`; `pending` is an Operator-Amber card linking to
  `/approvals` with the REAL `approval_id`. `tier`/`audit_seq` render honestly as
  `—` when the server could not resolve one (design §0 law 5 — never invented). All
  colour/motion resolves through NE-130 tokens (`chat.css`); reduced-motion collapses
  every animation to its end state. **Enqueue mode** streams the same `tool` frames
  (NE-TURN-PROGRESS: ne-worker emits each governed transit while the turn runs).
- **Cherenkov token flow.** A streaming assistant vessel gets `ne-message--live`,
  whose `chat.css` animation is a subtle Cherenkov sheen INSIDE the vessel only
  (tokens only; disabled under reduced-motion).
- **PULL-TO-PROVE.** Every citation chip gains an adjacent mono `PROVE` segment
  (chat + search) that deep-links `/knowledge?citation=<source_ref>` — NE-134's
  anchor lights the cited nodes (the Evidence Thread).
- **Flywheel intake.** Every finalized, non-error answer shows thumbs-up/down →
  `client.signalThumbs`; every citation shows `FLAG SOURCE` → `client.signalWrongSource`.
- **Uncaught-rejection-free submit** (NE-137-FU-REJECT). The chat submit path can
  never surface an uncaught promise rejection by construction: the session bootstrap
  (`client.createSession`) and the stream run inside ONE abort/unmount-aware guard in
  `useAgentStream.send` (which never rejects — it resolves with the turn's session id
  or `undefined` when aborted/unmounted), and `ChatPage`'s synchronous DOM handler
  catches as a structural backstop. A real createSession/stream failure surfaces on
  the inert message error line; an unmount mid-submit settles silently.
  Both live in `src/api/client.ts` (the ONE network seam — thin-client guardian);
  identity is NEVER on the wire (the server builds provenance from the bearer).
  Success engraves a `signal recorded` state (disabled); failure is an inert line.
- **/search ledger reskin** (`src/search/search.css`). Ranked `ScoredChunk`s as
  ledger rows with right-aligned mono (tabular) scores; two engraved placards state
  facts — the retrieval detail `"hybrid: dense + BM25 → fusion → rerank; top_k 10 of
40 candidates"` (REAL: `RetrievalPipeline candidate_multiplier=4` over `top_k=10`)
  and the C7 placard `"already trimmed — ACLs never reach this client."` Each row
  PULL-TO-PROVEs.

## Ops surfaces (NE-062)

Four RequireAuth routes add the console's operational surfaces. All obey the same
thin-client discipline: every byte arrives from a governed, server-scoped API via
the single `src/api/` client; there is no client-side ACL, approval, or verify
logic. Wire types mirror the merged backend models (`PendingApproval`,
`ApprovalStatus` + `pending_second`, `AuditRecord`, `ChainVerification`,
`ReplayResult`, `EvalRunRecord`, and the `KpiRecorder` vocabulary) 1:1 in
snake_case — this table and the endpoint table above are the normative contract
that Phase-6 ne-api must implement.

- **`/approvals` — the NORMATIVE approval path ("The Gate", NE-140).** A VIEW +
  signed action-submission onto the REAL NE-035 `DurableApprovalGate`, never a
  second or parallel gate. The Cold Criticality upgrade (design §4.7/§9) renders
  each queue row with an **amber tier badge** (generic `Tier {n}`, works 0–3), a
  **live SLA countdown rendered from the server `sla_deadline`** — the client only
  counts down, it never computes the deadline — an expiry countdown, and an amber
  **escalated** badge. The LISTING stays NE-035 payload-free — that redaction is
  unchanged and remains real. What's new (owner ruling 2026-07-31,
  NE-APPROVAL-PAYLOAD): every row also carries a **governed, reveal-on-demand
  payload card**. Initially it shows a "PAYLOAD — GOVERNED READ" placard + a
  **Reveal payload** button (no request fires before the click). On click the
  console calls `GET /v1/approvals/{id}/payload`; a 200 renders **TARGET
  RESOURCE / MATERIAL PARAMETERS / REVERSIBILITY** as inert text (a `<table>`
  over the parameter entries — never `dangerouslySetInnerHTML` or markdown,
  C12: this is model-influenced content); a 403 (a role-holder who is NOT the
  named approver for that request) renders a **"PAYLOAD WITHHELD"** placard
  with the server's `detail` shown VERBATIM — the console holds ZERO
  disclosure logic of its own; the gate alone decides who reads what. Approve/Reject (Operator Amber) POST to the governed approval API;
  **the request body carries NO approver identity** — the server derives the
  approver from the verified OIDC bearer (approve `{}`, reject `{ reason }`), and
  the returned `approver`/`decided_at` are shown verbatim. Tier-3 named-approver,
  the **distinct-second-check** (`pending_second`), SLA escalation, and expiry all
  stay authoritative **in the gate** server-side: a Tier-3 first approval replaces
  the row's buttons with a **`pending_second` distinct-second-approver placard**
  (driven solely by the POST response — the list wire type has no status field, so
  nothing list-level is fabricated), and a client-"expired" row disables its
  buttons as a courtesy, but the server verdict governs (a 403/409 is rendered,
  never overridden). An **empty-role 403** on the queue renders the honest "your
  key does not open this panel" placard (the server audits the refusal), distinct
  from the generic failure state. A decided row leaves the list only via a
  **refetched** server response, never a local mutation — a **409 already-decided**
  submit surfaces the server detail and then refetches so the stale row leaves via
  the server list; because that refetch removes the row carrying the inline error,
  the server's 409 detail ALSO persists verbatim in a page-level `role="alert"`
  notice (with Dismiss) that outlives the refetch (NE-UI-P2-CLEANUP). List
  refetches (initial load and every post-decision refetch)
  are **latest-request-wins** via the shared guard, so a slower earlier response
  can never resurrect a decided row as actionable, and a failed refetch clears the
  rows atomically with the error/forbidden placard (no stale actionable rows).
- **`/audit` — the Audit Chain view (NE-139).** Renders `AuditRecordView` rows as
  a mono ledger (`ChainLedger`) in ascending seq order (a **pure presentation**
  sort of the audit router's DESC page) with a per-row left-gutter Cherenkov chain
  tick. The chain verdict is the **server-computed** `ChainVerification` rendered
  verbatim as **CHAIN VALID / BREAK AT SEQ N**; a BREAK is shown prominently
  (`role="alert"`). The client **never** hashes, compares
  `prev_hash`/`record_hash`, or computes verification — the verification **sweep**
  (`useVerificationSweep`, ~60ms/link via `--motion-chain`) is **presentation
  only**: its sole chain-aware input is `first_break_seq` (used to stop the
  animation), and it collapses to an instant stamp under `prefers-reduced-motion`.
  An explicit server-authority placard states this on the page. Filters
  actor/action/from/to/**limit (clamped ≤500 client-side)** are query-param
  builders only (no client-side authorization filtering). The **PDP-DECISIONS**
  toggle is a **client-side presentation narrowing** (the C6 `tool.*` outcomes ∪
  `api.*.unauthorized`) because the governed `action` filter is **exact-match** and
  cannot express an OR-of-prefixes in one request; it narrows DISPLAY of
  already-authorized rows and never touches the verdict, which always covers the
  full fetched window. List fetches are **latest-request-wins** (a superseded
  slower/earlier response is discarded), so the displayed verdict always belongs
  to the displayed rows; a failed refetch clears rows + verdict under a fail-closed
  banner. A **replay provenance thread** (`ReplayThread`) renders `GET
/v1/audit/replay` — ordered events, config-hash plates, context ids — via an
  **explicit Trace id / Event id selector** (radio) so a UUID trace id is always
  sent as `trace_id` (no prefix heuristic). The view is **ops_roles-gated**: a 403
  yields a **REQUIRES OPS KEY** fail-closed placard (and the rail's chain-verdict
  lamp degrades to its hollow "requires ops key" state, §6.4). The ghosted tamper
  inset is an **explicit illustration** (placeholder glyphs, never fabricated
  hashes) that names the live `first_break_seq` only when the server reports a real
  break.
- **`/evals` — eval dashboards.** `EvalRunView` rows with per-metric
  value-vs-threshold CSS bars; the PASSED/FAILED badge is taken **exclusively**
  from the server's `passed` field (never recomputed from metrics).
- **`/kpi` — KPI dashboard.** Tiles from `GET /v1/kpi/summary`. **SCAFFOLD**: full
  U-P-B reporting (NE-068) is not yet built and will feed this surface; the wire
  shape mirrors the `KpiRecorder` vocabulary (`kpi_tag`/`value`/`dimensions`).

No charting library is used (C4: zero new npm deps) — eval/KPI visuals are
semantic HTML + CSS width-percentage bars/tiles. Live full-stack e2e (console →
real ne-api → real NE-035 gate / NE-030 chain / NE-024 store, live OIDC, live SLA
expiry/escalation, live Tier-3 two-human ceremony) is **BLOCKED on the unbuilt
`ne-api` (NE-104)** — it was never built by NE-070/NE-077; the contract published
above is what `ne-api` must implement.

## /trace — the delegation trace view (NE-167b)

A route pair (`/trace` entry form + `/trace/:traceId`) rendering the
Logfire-style trace assembly — the demo "wow" screen. It fetches `GET
/v1/trace/{trace_id}` (NE-167a) through the one governed `src/api/` client
seam and renders the server's `TraceAssemblyView` **verbatim**: a collapsible
span tree row-aligned (one CSS grid, `display: contents` per row) with its
waterfall bar so the tree and the timeline never need scroll-sync code, a
per-node detail drawer with pinned attrs and clickable `event_id`/`trace_id`
links back into `/audit`, a per-model aggregate strip, routing-tier chroma
(`--amber` frontier / `--green` local / achromatic unpinned), shape-not-value
redaction chips (`▓ CATEGORY (N)`, never a redacted value), and the
server-authoritative chain-verification badge (`CHAIN VERIFIED` /
`BREAK AT SEQ N`).

**Thin-client (C6/C7), render-only.** Every displayed cost, token count,
duration, aggregate, and the verify verdict is a **verbatim server field** —
the client never sums, subtracts, hashes, or re-derives any of it. The one
arithmetic in the view is the waterfall's `offsetPct`/`widthPct` CSS layout
positioning of server timestamps (pure presentation, exactly like `/audit`'s
client-side seq sort); that percentage is never itself rendered as a number.
A contradictory-but-`valid:true` fixture (a rollup total that disagrees with
the model aggregates) still renders both verbatim — proof, not assertion,
that nothing here is recomputed.

**§0 law 5 — honest rendering.** `TraceNodeView.kind` is typed as a plain
`string`, not a closed union: an emitter action the NE-167a assembler does not
recognize (its own `"event"` raw fallback, or any future kind) renders its
scrubbed `detail` in a block labeled **"RAW EVENT (unrecognized emitter)"** —
no invented icon, no fabricated typed fields. Per-model aggregate token totals
that are `0` render **"—"**, never a fabricated hard zero: the server's
`ModelAggregateView.input_tokens`/`output_tokens` are non-nullable ints that
default absent per-node provenance to `0`, so `0` is ambiguous between
"unknown" and "genuinely zero" — and a completed model call cannot genuinely
have zero tokens (the NE-167a Codex P2 follow-up, closed here). An unknown
`trace_id` renders **"TRACE NOT FOUND"**; a non-ops principal gets **"REQUIRES
OPS KEY"** (the `/audit` convention) — never a crash, never fabricated data.

**Reachability.** There is no rail nav node for `/trace` (NE-CONSOLE-IA: a trace
is opened ABOUT something — an answer, an approval, an audit row — never
browsed to as a destination): the `/audit` ledger's TRACE column links each
row's `trace_id` into `/trace/:id`, and a bare `/trace` accepts a hand-entered
id. Both themes
work by construction (every color/font/space in `trace.css` resolves through
an NE-130 token); `prefers-reduced-motion` collapses the waterfall bar's
grow-in animation to its final geometry instantly, via both a JS-computed
modifier class (mirroring `DemoOverlay.tsx`'s `matchMedia` hook) and a
belt-and-braces `@media (prefers-reduced-motion: reduce)` block.

## Steward surfaces (NE-067)

Two new RequireAuth routes deliver the console side of the propose-then-approve
flywheel gate (NE-066), plus a health metric:

- **`/steward` — the steward review queue.** A VIEW + action-submission surface
  onto the REAL NE-066 `ProposalReviewService` (`approve` / `reject` /
  `record_apply`) — **never a parallel state machine, never a client-side
  apply**. It lists non-terminal (`proposed`/`approved`) fix proposals; each
  card shows the lane, status, priority, confidence, evidence counts, and the
  proposal's `DiffArtifact` — NE-066's ALREADY-SANITIZED `patch` / `pr_body` —
  rendered as **inert preformatted text** (plain React text nodes inside
  `<pre>`; the console does not re-render, re-sanitize, or interpret it as
  markdown/HTML). This is enforced structurally by guardian check **(e)** in
  `src/test/thin-client-guardian.test.ts`, which asserts no production source
  under `src/` contains `dangerouslySetInnerHTML`. Available actions derive
  **only** from the server's `status` field (button visibility is a courtesy);
  Approve/Reject/Mark-Applied POST to the governed review API with **NO
  client-supplied actor/principal/surface/session** — the server derives the
  steward identity from the verified OIDC bearer. Reject always carries one of
  the six typed `RejectionCode` values, which feeds NE-066's
  rejection→correction-signal loop **server-side**. Mark-Applied requires a
  non-empty `applied_ref` and only **records** who/when/what was applied — the
  console never performs the apply. A decided proposal leaves the queue only
  via a **refetched** server response, never a local splice; a 409 (illegal
  transition) or 422 (empty `applied_ref`) is rendered verbatim and the row
  stays actionable. A separate steward-latency health strip
  (`GET /v1/steward/latency`) renders server-computed median/p90 wait and
  backlog age (humanized by a local `formatSeconds` helper); its failure
  renders an inline alert and never blocks the queue.
- **`/steward/digest` — weekly steward digest.** Renders `GET
/v1/steward/digest` verbatim: the server-computed week window, four totals
  (proposed/approved/rejected/applied), and per-lane counts in **server
  order** — no local date math or sorting, so the render is deterministic
  given a fixed API response.

### Backend-seam note (Phase-6 ne-api)

Phase-6 ne-api implements these six endpoints over
`ne.flywheel.review.ProposalReviewService` (`approve` / `reject` /
`record_apply` / `read_reviews` + the status fold),
`ne.flywheel.diffs.render_proposal_diff` (the sanitized artifact is produced
server-side per request or cached), and SQL aggregations over
`ne_fix_proposals` ⋈ `ne_proposal_reviews` for the digest (per-lane counts in
`[week_start, week_end)`) and the latency metric (median/p90 of first-decision
`created_at` − proposal `created_at`; backlog = non-terminal proposals).
`SignalProvenance` (actor principal, `surface="console"`, `session_id`) is
constructed **server-side** from the verified OIDC bearer — the console never
supplies identity.

## Graph Chamber (NE-134) — `/knowledge`

The signature view: a full-bleed `<canvas>` force-directed renderer over the
governed, ACL-trimmed NE-133 graph routes. The console types only the six-field
`GraphNodeWire`/`GraphEdgeWire` shapes (mirroring `graph_wire.py`) and renders
EXACTLY what the trimmed routes return — no client-side graph store, no filtering
(C7). All network goes through the three new `src/api/client.ts` methods
(`graphQuery` / `graphNeighborhood` / `graphPaths`); the chamber never fetches.

**View anatomy** (`src/knowledge/`):

- **AnchorRail** (left) — a T1 `/brain/search` box → cited-chunk stack; each
  row's ANCHOR button adds its citation `source_ref` to the anchor set, which
  drives exactly ONE `POST /v1/graph/query` per change.
- **GraphChamber** (center) — the canvas + d3-force layout. Interaction and test
  assertions route through an accessible DOM sibling layer (a visually-hidden
  "Graph nodes" `<ul>` of buttons, a `<N> NODES · <M> EDGES` ticker, and the
  `role=status` truncation placard), so screen readers and jsdom (no 2D context)
  work without pixels. Ticks are driven off the render loop; rAF runs only while
  `alpha ≥ 0.005`; `prefers-reduced-motion` settles synchronously to one static
  frame. Colours resolve from NE-130 tokens at draw time via `chamberTheme.ts`
  (luminance via `globalAlpha`, never colour literals — token-hardcode guardian).
  Glyphs: 5 entity classes (Account=square, Person=circle, Document=page-pentagon,
  Ticket=diamond, Order=hexagon); 9 edge kinds + SAME_AS.
- **DetailDrawer** (right) — the selected node's six fields, **BLOOM**
  (`GET /v1/graph/neighborhood?entity=<key>&hops=1`, merged with position
  preservation + dedupe by key), and **TRACE** (`POST /v1/graph/paths
{from,to,max_hops,limit}`) with per-hop provenance placards. A hop whose
  `source_ref` matches a session citation exposes OPEN SOURCE CHUNK
  (`/v1/brain/retrieve` via the shared EvidencePanel); otherwise the honest
  no-chunk fallback renders — never a fabricated `chunk_id` (§0 law 5).
- **T3Drawer** (bottom) — ONTOLOGY · VERIFIED ANSWERS · CONFORMANCE tabs, filled
  by NE-136 through the `tabBodies` typed slot (see the T3 drawer section below);
  any id left unfilled keeps its honest `AwaitingUnit` stub.

**Honest states** — dark/empty (naming `POST /v1/graph/query`), loading, the 404
tier-disabled placard (naming `BrainGraphSettings.enabled`), the fail-closed
error placard (status/class only), and the by-design cap/truncation placard
(server `truncated` OR a client BLOOM-merge overflow of the 500-node/2000-edge
caps).

**Deep-link param contract** (consumed by NE-136/137; keep stable):

- `?anchor=<key>` (repeatable) → graph query `anchor_keys`
- `?citation=<source_ref>` (repeatable) → graph query `anchor_citations`
- `?entity=urn:ne:entity:<key>` → `parseEntityUrn` → an `anchor_key` + lights
  that node after load

NE-136's drawer tabs light chamber nodes in-page via the exported
`KnowledgeChamberContext` (`lightEntity(key)`).

### T3 drawer (NE-136) — verified answers + conformance

The bottom drawer (`src/knowledge/t3/`) fills the three tabs over the governed
NE-135 ontology/verified routes — all through `src/api/client.ts`
(`ontologyClasses` / `ontologyShapes` / `ontologyViolations` /
`verifiedTemplates` / `verifiedInvoke`); the tabs never fetch.

- **ONTOLOGY** — a BFO-2020/CCO-2.1/neop:\* class browser (a tree grouped by
  source and indented by `parents` links, mono IRIs) + a per-class alignment
  placard + the compiled SHACL shape list, from `GET /v1/ontology/classes` +
  `GET /v1/ontology/shapes`.
- **VERIFIED ANSWERS** — the template catalog from `GET /v1/verified/templates`,
  a typed-args form **generated** from each template's strict `args_schema`
  (`argsForm.ts` maps str/int/bool/date/datetime; an unsupported type fails
  closed with INVOKE disabled — never guessed), and INVOKE →
  `POST /v1/verified/invoke {template_id,version,args}`. **C6:** that route
  executes server-side ONLY through the frozen `GovernedToolExecutor`
  (`read.kg.<id>`, tier-0) — the client constructs no SPARQL and names no store
  endpoint; the body carries no principal/actor field (identity is the OIDC
  bearer). The result table renders the deterministic rows with per-row
  `urn:ne:entity` citation chips (click → `lightEntity`), the truncated flag, the
  `config_hash` + `ontology_version` HashPlate stamps, and a **replay-diff badge**
  driven by the SERVER's `replay_match` (its real double-invoke `answers_equal`
  compare, `executed_at` excluded — never faked client-side). The ASK gesture
  surfaces a chamber-selected entity as a USE SELECTED prefill on string args.
- **CONFORMANCE** — the redacted ShapeViolation quarantine feed from
  `GET /v1/ontology/violations` (counts/ids/hashes only — no `delta_nt`, no SHACL
  messages, C12) with the quarantined-whole fail-closed placard.

**Honest states** — every tab shares one treatment: a 503 renders the
ontology-disabled/verified-disabled placard, `enabled:false`/`configured:false`
and empty render engraved route placards, and any other error is a role=alert
fail-closed placard (status + server detail only). Nothing is ever simulated
(§0 law 5).

**Vendored d3** — the force library is committed static ES-module source under
`src/vendor/d3/` (ISC), NOT an npm dependency. Re-vendor with:

```bash
node scripts/vendor-d3.mjs
```

It fetches the pinned upstream `src/` from immutable jsDelivr URLs, applies the
three recorded import rewrites, and prints the sha256 of every file (recorded in
`src/vendor/d3/README.md`). See that README + NOTICE for the C4/C11 audit trail.

**LIVE-PARKABLE:** MSW fixtures mirror `graph_wire.py` exactly; live e2e against a
real AGE-backed graph tier + the 500-node/2000-edge 60fps profiling pass are
parked to Phase 6.

## Dual-Key Compare — the trim lens (NE-141)

The **COMPARE** control (top-right of `/knowledge`, enabled once a graph is
anchored) is the honest **trim lens** (design §5.3). Clicking it runs a **real
second-persona OIDC login** — Authorization Code + PKCE against the pilot Keycloak
realm, in a popup, with `prompt:"login"` forcing re-authentication (without it
Keycloak's SSO cookie would silently re-issue persona A and the compare would be a
lie). The second token is held **in memory only** — a `UserManager` configured with
`InMemoryWebStorage`-backed user **and** state stores — scoped to the compare pane;
it is **never** written to sessionStorage/localStorage/cookies and **never** mixed
with the primary session token.

The whole second-persona seam lives in **`src/api/compareAuth.ts`** (the only place
the key-B token exists): `acquireCompareKey(cfg)` runs the popup and returns an
in-closure `CompareKey`; `createCompareClient(key, onKeyExpired)` is a **separate**
`ApiClient` (governed `client.ts` reuse) bound to the key-B bearer — its 401 handler
renders an honest expired state and **never** triggers the primary `signinRedirect`.
The popup window returns to the **`/auth/compare-callback`** route, which relays the
response URL to `window.opener` via `completeComparePopup` — the **parent** window
performs the code→token exchange, so no token ever materialises in the popup.

The pane runs the **identical** graph/retrieve/verified-invoke bodies under **each**
bearer; the **server** trims each response independently (C7 — the client never
diffs privileged data, it only renders what each key's server response contained).
The contraction (design §5.4) renders the primary chamber **contracting**: nodes
present under key A but absent under key B **implode** (damped scale-to-zero, 200ms,
staggered by BFS hop distance from the anchor), edges retract, and the node-count
ticker rolls **down** to key B's server-returned counts — **no red X's**. A placard
states the honest line verbatim: _"Both views are exactly what the server sent each
keyholder. The client never saw what was removed."_ Side-by-side chunk counts and
verified-answer row counts accompany; where no session chunks / no recorded invoke
exist, an honest "unavailable" line renders instead of a fabricated number. Every
failure (popup blocked/closed, IdP error, key-B 401/403/404/5xx) renders an honest
placard — **never** a faked trimmed view — and no contraction runs without **both**
real graph responses. `prefers-reduced-motion` collapses the contraction, ticker,
and stamp to instant final state.

**KEY WITHDRAWN**: exiting the compare (the WITHDRAW KEY button, or navigating away)
**discards** the second token — best-effort `revokeTokens()`, then `removeUser()`,
then nulls the closure so `getAccessToken()` returns `undefined` — and snaps the
"KEY WITHDRAWN" stamp before the pane closes.

Governance: thin-client (the compare fetch context is `client.ts` reuse under
`src/api/`; no `client_secret`, no baked `VITE_OIDC_*` — public PKCE client, runtime
`/config.json` issuer), C7 (two server-trimmed responses, no client-side
impersonation — identity is server-derived from each bearer), §0 law 5 (the
contraction shows the REAL A∖B difference, never a fabricated removal), token-driven
(`compare.css` is token-only), OIDC-gated. **No new dependencies** — `oidc-client-ts`
(already the primary login's dependency) provides the popup/second-`UserManager`
APIs; NOTICE is unchanged.

**LIVE-PARKABLE:** in vitest the popup is mocked at the `src/api/compareAuth` seam
and the compare bearer is real on the wire (MSW routes each key's response by
`Authorization` header); a real second-persona login against the pilot Keycloak
realm (real popup, real server-side trimming of the two responses) is demo-time
verification in the composed deployment.

## Flywheel Dashboard (NE-138)

`/flywheel` is the **Flywheel Dashboard** (design §4.8): a Cold-Criticality view
over the **existing** governed `/v1/steward/*` routes — it adds **zero** ne-api
routes and calls only the `client.ts` methods above. Per design §2 the NE-067
steward pages are **retired**: `/steward` → `/flywheel` and `/steward/digest` →
`/flywheel?tab=digest` are client redirects, and every security-invariant test
the deleted `StewardQueuePage`/`DigestPage` suites carried is re-shipped 1:1 in
the flywheel suite (`src/flywheel/FlywheelPage.test.tsx`).

- **Hero — the breeding-cycle ring** (`BreedingRing`): a hand-rolled inline-SVG
  schematic loop SIGNALS → CLUSTERS → PROPOSALS → HUMAN (the one amber sector) →
  TRACKED APPLY → RE-FINGERPRINT. It is **event-detented, never decoratively
  spinning** (§0 law 4): the pointer advances **one detent per real event** —
  a local governed review response (approve/reject/apply) or each new
  `/flywheel`-routed event surfaced by the ONE shared audit poll. A pure,
  unit-tested reducer (`useFlywheelDetents`) **dedupes** the audit echo of the
  operator's own act so it counts exactly once, reconciled by **event count**
  (NE-UI-P2-CLEANUP): N `/flywheel` events in one poll batch consume up to N
  pending local echoes and any remainder advances as real foreign events, so 2+
  local reviews completing within one poll interval can never starve a later
  genuine foreign event's detent; the first poll never pulses (no fabricated
  mount motion). A reject plays a **finite one-shot** token travelling
  HUMAN → SIGNALS (the loop closing on itself). `prefers-reduced-motion` collapses
  all motion to instant state changes; there is no endlessly-looping animation.
- **Queue tab** — the proposal queue over `GET /v1/steward/proposals` with lane
  chips (all five lanes incl. the job-emitted `entity_resolution`), kind, a
  confidence gauge (**never full** — a steward still approves), mono evidence
  ids, and the sanitized `DiffArtifact` rendered **inert** (see the guardian
  note in the steward section). Entity-merge candidates additionally show a
  **deterministic side-by-side** extracted from the renderer's
  `_entity_merge_lines` grammar (`EntityMergeCompare`), with an honest null
  fallback to the inert artifact on any format drift. Approve / typed-code Reject
  / **Record-Apply** mirror the `proposed → approved → applied` state machine
  (reject terminal; `proposed → applied` is impossible in the UI because it is
  illegal on the server). Record-Apply copy states verbatim that it **records a
  human git/OPA apply — the console never executes an apply**.
- **Digest tab** — `GET /v1/steward/digest` (per-lane counts incl.
  `entity_resolution`) + `GET /v1/steward/latency` (median/p90 wait, oldest-pending
  age) as steward-SLA gauges, each an **independent** stream (one failing never
  blanks the other), all rendered server-verbatim.
- **Not built:** the optional flagged `GET /v1/flywheel/signals` intake feed is
  **not** implemented; the ring's upstream intake stage renders as an honest
  placard, never fabricated signal/cluster data (§0 law 5).

## /evals + /kpi (NE-143 reskin)

NE-143 reskins the two **existing** governed views onto the NE-130 "Cold
Criticality" rail. It is a pure presentation change: **no routes are added or
changed** — data still flows exclusively through `client.listEvalRuns` /
`client.kpiSummary` over `GET /v1/eval/runs` and `GET /v1/kpi/summary`, and the
diff is confined to the eval/kpi views, their per-view CSS, the eval/kpi
sections of the MSW fixtures, tests, and this README.

- **`/evals` — "The Accuracy Plane".** `EvalRunView` rows render as ledger rows
  with **paired value-vs-threshold gauges** (hand-rolled CSS width percentages —
  no charting lib, C4), a **server-authoritative verdict lamp**, and
  `golden_set_hash` / `config_hash` `HashPlate` plates. The PASSED/FAILED lamp
  renders `run.passed` **verbatim** and each per-metric status renders
  `run.statuses[name]` **verbatim** — the console **never** recomputes pass/fail
  from the metric numbers (design §0 law 5 / §6.6). An authority placard states
  "verdict computed server-side"; a framing placard states that eval gates are
  CI-enforced and golden sets are fed by the flywheel's `GoldenSetCandidates`.
  Proven by the beats-thresholds-but-`passed:false` fixture test.
- **`/kpi` — the U-P-B board.** Three columns on the **Utilization ·
  Proficiency · Business Value** schema, rendered from `GET /v1/kpi/summary`.
  Numerals render in mono via a **damped needle counter** (`DampedNumeral`) that
  animates only when governed data arrives/changes and skips entirely under
  `prefers-reduced-motion` or when `requestAnimationFrame` is unavailable (§0
  law 4); the JSX always contains the exact final value, so tests are
  deterministic. The `kpi_tag` is first-class (rendered verbatim).
  - **EXPECTED slots mirror `ne-api routers/kpi.py` `_entries`.** The client may
    not _compute_ a KPI, only _mirror_ the server's tags. Each column has a
    fixed expected-slot list (`utilization.*`, `proficiency.*`,
    `business_value.hours_saved_floor`); keep those lists in sync with the
    router.
  - **Honest absence, never a fabricated zero.** A tag the server skips (a
    `None` value = an instrumentation gap) renders a hollow **NOT INSTRUMENTED**
    slot, not a zero (design §0 law 5 / §5 do-not-fabricate). An unknown-prefix
    tag is surfaced under an **UNCLASSIFIED** column, never silently dropped.
  - **`business_value.hours_saved_floor` is a C8 supporting metric.** It is
    labeled **"SUPPORTING METRIC — C8"** and "reported as a floor"; the column
    header notes it is led by named KPIs. Business value is never overclaimed
    (§7 forbidden copy clean).
  - **Honest empty state.** When the read seams are unconfigured the route
    returns `{kpis: [], generated_at}`; the board renders a **"KPI READ SEAMS
    UNCONFIGURED"** placard (the `generated_at` footer still renders). A 5xx
    fails closed to a **"KPI PANEL UNAVAILABLE"** `role="alert"` placard.

Both views style through the NE-130 tokens only (`evals.css` / `kpi.css` on a
new `ne-evalsplane` / `ne-kpiboard` namespace); the token-hardcode and
thin-client guardians stay green. The legacy `.ne-evals` / `.ne-kpi` rule blocks
in `styles.css` are now **dead selectors**, left untouched on purpose to avoid
conflicts with the concurrent console units; their cleanup is deferred to a
later unit. Zero new dependencies — `NOTICE` and the npm license closure are
unchanged.

## The work surface at `/`

`/` is the **home screen an operator opens on a Tuesday**, not a pitch. It answers
one question — _what needs me right now_ — and it decides which home to show by
**asking the server**, never by reading `groups` and branching client-side: a lane
is open because the governed API answered `200` for this bearer, and closed because
the server refused it with a `403` it audited. When no decision queue is open to a
key, the `WAITING ON YOU` band drops to the foot of the page (a CSS `order` change
on already-mounted content — no remount, no refetch) and that person's own work
leads.

Four bands: **WAITING ON YOU** (pending approvals + open review proposals),
**SINCE YOUR LAST VISIT** (new chain records against a stored visit mark, plus
whether the configuration in force changed under you), **YOUR RECENT WORK** (what
you asked, re-runnable in one click), **THE DEPLOYMENT** (four standing facts, each
linking to its proof).

- **Nothing is fabricated.** Every figure carries its source; a refused lane says
  so; the chain delta states that it is bounded by the most recent page of records;
  an unavailable value renders as an em dash with the reason, never as a zero.
- **The local record is labelled as local.** `landing/workStorage.ts` keeps a visit
  mark and a short ask journal in `localStorage` under the versioned `ne-work:v1`
  key, per principal. Two of the questions a daily user arrives with — "what
  happened since I was last here" and "what did I ask recently" — have **no server
  answer available to every key** (`/v1/audit/events` is ops-gated; there is no
  chat/search history endpoint). The alternative to a local record is inventing
  history or a home screen that cannot answer them. So the page keeps the record
  and states its provenance on screen: _"Recorded in this browser as you asked —
  not read from the chain."_
- **Re-running an ask never auto-sends.** `ASK AGAIN` links to `/chat?prompt=…` or
  `/search?q=…`; the target **pre-fills** its input at mount and the human still
  presses Send. A link cannot start a governed turn on someone's behalf.

## Guided tour — the dismissible first-run panel (NE-142)

The nine-station walkthrough (Beats 0–7, design §3) used to be the **body of the
landing route**: the product opened on its own demo script. It is now the pattern
every product uses — a first-run panel — and the content itself is unchanged.

- **First run.** On a browser that has never dismissed it, the panel opens once, by
  itself, and introduces itself: what it is, that dismissing it is permanent, and
  where to reopen it.
- **One action kills it.** `DISMISS — DON'T SHOW AGAIN` (and `Escape`, on the same
  terms — the keyboard path is not weaker) closes the panel **and** writes the
  dismissal to `localStorage` under `ne-tour:v1`. There is no confirmation step.
  The store is `localStorage`, not `sessionStorage`, on purpose: a dismissal that
  dies with the tab is not a dismissal. Reads **fail closed** — if storage cannot
  be read, the panel does _not_ auto-open, because a tour that fails to appear is
  one click from recovery while a tour that cannot be dismissed is not.
- **It stays reachable.** The rail's `GUIDED TOUR` control reopens the same
  stations, without the first-run introduction.
- **Non-modal, no scrim.** The console underneath stays live and usable while the
  panel is open — a first-run panel must never hold the product hostage, and the
  check-off depends on real navigation producing real audit seqs.
- **Nine numbered stations** (display numerals `0,1,2,3,3b,4,5,6,7`): COLD
  START · FUEL · FIRST NEUTRONS · THE CORE · THE TRIM LENS · CONTROL RODS ·
  BREEDING · THE LOG · WHOSE TRUST BOUNDARY. Each carries one **claim-being-proven
  placard** (design/SPEC-verbatim, e.g. `C7 — permission trimming can never
over-return`), presenter cue text in `--type-small`, and deep links into the
  real routes.
- **Arrow-key navigation.** `ArrowRight`/`ArrowLeft` move between stations
  (clamped, **no wraparound**); the strip's numeral buttons jump directly. Keys
  originating in an `input`/`textarea`/`select`/`[contenteditable]` are ignored so
  presenter typing in the chat composer never moves stations.
- **Resumable.** The current station index and check-offs persist under the
  versioned `sessionStorage` key `ne-demo:v1`; a reload + re-summon resumes at the
  saved station. `open` is **never** persisted — it is seeded from the `ne-tour:v1`
  dismissal alone. The two stores are deliberately different: a place in a
  walkthrough is a property of the sitting, "stop showing me this" is a property of
  the person.
- **The demo audits itself — real seqs only (§0 law 5).** A station **checks off**
  with the real audit **seq** it produced, read from the **one shared audit poll**
  (`useAuditPoll`, §6.7 — no new fetch, no per-component polling). Attribution is
  deliberately weak: it records "the newest governed event observed while this
  station is armed" (a background event could satisfy it) — honest, never presented
  as causal proof, and **impossible to fabricate** (the module holds no other seq
  source). Before a seq advances the station shows `AWAITING AUDIT SEQ`; a
  **role-degraded** (403) poll shows `CHECK-OFF REQUIRES OPS KEY` (§6.4); an
  unreachable poll shows `AUDIT POLL UNREACHABLE`.
- **Honest flag path.** A station whose route has not shipped (`awaitingUnit`)
  renders `AWAITING NE-xxx` with **no links** — flagged, never faked. All nine
  views are shipped today, so this path is reached only by the fixture test.
- **Deep-link decision (scripted PATH, never scripted DATA).** Stations link only
  to route/query forms the views already accept **and that carry no data**. The
  SPEC-row examples `/knowledge?citation=…` and `/audit?trace_id=…` are
  deliberately **not** hardcoded — baking a specific citation IRI / trace_id would
  be scripted data (§0 law 5); those stations link to the plain route and the cue
  text directs the presenter to pull the citation / paste the `trace_id` **live**.
  `/flywheel?tab=queue` is used because `tab` is an accepted, data-free
  `FlywheelPage` param.
- **Motion (§5.4).** The entrance slide-up and the check-off tick are the only
  motion, event-driven and damped; `prefers-reduced-motion` collapses them to
  instant via the `ne-demo--reduced` class plus a backing media block. Styles
  resolve through NE-130 tokens only (`demo.css`); the token-hardcode and
  thin-client guardians stay green.

## Dev setup

```bash
nvm use            # Node 24 (see .nvmrc)
npm ci             # reproducible install from the committed lockfile
npm run dev        # Vite dev server; /api is proxied to http://localhost:8000
```

Run the dev backend + Keycloak with `ne dev up` (Keycloak on :8081, realm
`ne-dev`, public client `ne-console`).

**Runtime OIDC config (NE-CONSOLE-ISSUER).** The console no longer bakes its
issuer/client_id at build time. At startup it fetches `/config.json` from ne-api
(`issuer`/`client_id`/`audience`/`api_base` — the SAME `IdentitySettings` the
backend verifies tokens with), and initializes its OIDC/PKCE client from those
runtime values, so one built console image self-configures its login from whatever
deployment serves it. The dev proxy (`vite.config.ts`) forwards `/config.json` to
`http://localhost:8000`, so `npm run dev` login now requires a locally running
ne-api (`ne dev up`) to serve the config — the old zero-backend baked-issuer
default is deliberately gone. The config fetch is **fail-closed**: if it fails or
returns an invalid payload, the app renders an error screen with a Retry button
and never falls back to a wrong-issuer login. The only remaining build-time knob is
`VITE_API_BASE_URL` (named solely in `src/api/config.ts`).

## Frontend gate (exact commands)

```bash
npm ci
npm run typecheck     # tsc -b (strict, noEmit)
npm run lint          # eslint flat config incl. the thin-client guardian rules
npm run format:check  # prettier --check
npm run test          # vitest run (MSW-2 fixtures)
npm run build         # tsc -b && vite build
```

And the npm C4 license gate (from the repo root):

```bash
uv run python tools/supply-chain/npm_license_gate.py --project packages/ne-console
```

All tests are fixture-first against MSW 2 replaying the governed API shape. Live
full-stack e2e (console → real ne-api → trimmed corpus) is parked to Phase 6.
