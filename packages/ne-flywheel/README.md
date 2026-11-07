# ne-flywheel

Correction-signal capture — the backend input side of the improvement
flywheel (NE-064). NE-065's scheduled curation agent (steward queue backend)
consumes this package's `SqlAlchemyCorrectionSignalStore.read_signals()` to
cluster captured signals into fix proposals. NE-066 adds the
**propose-then-approve gate** — a deterministic diff renderer plus an
append-only, audited review state machine — the human-review boundary between
"the flywheel suggests a fix" and "a change lands".

**Scope of this unit (NE-064): BACKEND capture only** — the typed contract,
the durable append-only store, and the `CorrectionSignalCapture` emit
service. Console/Slack/API UI wiring that actually *calls* the emit API is
follow-on work; this README's "Emit contract for surfaces" section is that
follow-on work's spec.

## Signal kinds

Every signal maps onto the FROZEN `ne.schema.CorrectionSignal` event via its
`CorrectionKind` enum:

| User/system action | Payload model | `CorrectionKind` |
| --- | --- | --- |
| Thumbs up on an answer | `ThumbsSignal(up=True, target_ref=...)` | `thumbs_up` |
| Thumbs down on an answer | `ThumbsSignal(up=False, target_ref=...)` | `thumbs_down` |
| User edited an AI-drafted document | `DraftEditSignal(draft_ref=..., edit_ratio=...)` | `edit_distance` |
| User flagged a citation as wrong | `WrongSourceSignal(answer_ref=..., citation_ref=...)` | `wrong_source` |
| User re-asked the same question | `ReQuerySignal(original_ref=..., requery_ref=...)` | `re_query` |
| A Tier-2/3 HITL approval was rejected | `ApprovalRejectionSignal(approval_id=..., tier=...)` | `approval_rejection` |

## Emit contract for surfaces (console / Slack / API — follow-on work)

A surface (ne-console, ne-chat/Slack, a future public API) that wants to emit
a correction signal:

1. **Construct provenance** from the AUTHENTICATED request context — never
   from client-supplied identity claims:

   ```python
   from ne.flywheel import SignalProvenance, SignalSurface

   provenance = SignalProvenance(
       principal=principal,          # the resolved ne.contracts.types.Principal
       surface=SignalSurface.CONSOLE,  # or SLACK / API
       session_id=session_id,
       turn_id=turn_id,               # optional
       message_id=message_id,         # optional
   )
   ```

   `SignalProvenance` rejects an empty `principal.id` or empty `session_id`
   at construction (fail closed — no signal is ever built unauthenticated).

2. **Pick the payload model** for the user action per the table above. For a
   draft-edit signal, compute the magnitude LOCALLY with
   `ne.flywheel.draft_edit_ratio(original, edited)` — never send the raw
   before/after text to this package; only the ratio (+ an optional, short,
   already-scrubbed `diff_summary`) travels further (C12/privacy, see below).

3. **Call `CorrectionSignalCapture.capture(...)`**:

   ```python
   from ne.flywheel import capture_from_settings

   capture = capture_from_settings()  # or CorrectionSignalCapture(engine)
   event_id = await capture.capture(provenance, payload)
   ```

   `capture()` builds the typed `CorrectionSignal` event (config_hash-stamped,
   non-optional), validates it, scrubs any free-text field, and durably writes
   it — atomically — to both the signal store and the platform event outbox.
   A validation failure raises `SignalRejectedError` before anything is
   persisted.

### Canonical provenance keys

Every captured signal's `provenance` map carries these keys (the ones NE-065
and any downstream consumer can rely on):

| Key | Always present? | Meaning |
| --- | --- | --- |
| `surface` | yes | `console` / `slack` / `api` |
| `principal` | yes | the acting principal's id (also stamped as `actor`) |
| `session_id` | yes | the emitting session |
| `turn_id` | when supplied | the agent turn within the session |
| `message_id` | when supplied | the specific message the signal is about |
| `answer_ref` | `wrong_source` only | the answer the flagged citation appeared in |
| `requery_ref` | `re_query` only | the id of the user's re-asked query |
| `approval_tier` | `approval_rejection` only | the rejected request's tier, as a string |

## Privacy / sovereignty stance (C2, C12)

* **No raw draft/query text is ever stored.** A draft-edit signal carries only
  the numeric `edit_ratio` (computed client/surface-side via
  `draft_edit_ratio`) plus an optional, already-short `diff_summary` — never
  the original or edited document bodies.
* **C12 secret hygiene**: every free-text field (`comment` / `diff_summary` /
  `reason`) passes `ne.flywheel.scrub_text` BEFORE persistence — deterministic
  stdlib-`re` redaction of AWS keys, GitHub/Slack tokens, Bearer headers,
  JWTs, PEM private-key blocks, and `key=value` credential assignments,
  applied in both the store row and the outbox payload. This is deliberately
  *not* routed through `ne-govern`/Presidio (that is the PII guardrail plane;
  this is a narrower, deterministic credential-*shape* scrub with no
  dependency on the OPA/guardrail stack).
* **C2 — customer data, in-VPC, no egress**: correction signals ARE customer
  data. `ne.flywheel` performs zero network I/O; the signal store rides
  whatever Postgres/SQLite engine the deployment already runs in-VPC. Nothing
  in this package ever leaves the deployment.

## Audit stance

Every capture atomically dual-writes: the durable store row AND a
`correction_signal` event on the platform event outbox (`ne.events.append`),
in ONE transaction on ONE connection. That outbox emission of the correction
signal *is* the audit-relevant record for this unit — there is deliberately
**no** separate `ne-audit` hash-chain write here. Correction signals are
customer FEEDBACK data, not privileged actions, and an approval-rejection
signal in particular duplicates a fact already audited at source by NE-035's
HITL gate (the rejection itself produces an audited `ApprovalEvent` there).

## Retrieval API (for NE-065 clustering)

```python
store.read_signals(after_id=cursor, limit=200, kinds=["thumbs_down", "wrong_source"])
```

Ordered by the append id ascending — the same cursor discipline as the
ne-events outbox. `kinds`, `surface`, `since`/`until` filter further. The
store's ENTIRE public mutation surface is `append` (idempotent on duplicate
`event_id`) — no update/delete/edit method exists; a correction signal is
never revised or removed in place.

## Curation agent (NE-065)

The `CurationAgent` is the flywheel *engine*: a scheduled, governed, fully
deterministic routine. One `run_once()`:

1. **Cursor-reads** the next batch of correction signals from the NE-064 store
   (`read_signals(after_id=cursor, limit=curation_batch_limit)`).
2. **Clusters** them deterministically — a union-find over shared normalized
   refs (`subject_ref` plus the kind-specific provenance refs `answer_ref` /
   `requery_ref`). `thumbs_up` is dropped first (positive feedback is not a
   correction). Same input SET in ANY order ⇒ identical clusters and identical
   content-addressed `cluster_id`s.
3. **Routes** each cluster to exactly one of four fix lanes.
4. **Appends** a typed `FixProposal` per cluster (plus a golden-set candidate
   for every retrieval cluster) into the append-only proposal store, advances
   its cursor, and dual-appends its `curation.run` audit event — all in ONE
   transaction.

### The four fix lanes

Routing is a documented, first-match **precedence**: `policy > schema >
retrieval > content`. Every cluster routes to exactly one lane by construction
(content is the catch-all).

| Lane | Wins when the cluster has… | Which `CorrectionKind`s | The proposed change |
| --- | --- | --- | --- |
| `policy` | any `approval_rejection` | `approval_rejection` | an OPA policy-diff (governance/guardrail/ACL) |
| `schema` | any ref whose scheme ∈ {`schema`,`field`,`record`,`extraction`,`contract`} | (ref-driven, any kind) | a data-contract / extraction change |
| `retrieval` | any `wrong_source` / `re_query` | `wrong_source`, `re_query` | a retrieval-config review (the right doc existed but wasn't surfaced) |
| `content` | otherwise | `thumbs_down`, `edit_distance` | a source-document fix (the content itself is wrong) |

A cluster mixing kinds is routed by the highest-precedence concern it contains
(a governance denial dominates everything; a data-contract gap dominates a
retrieval symptom). The semantics are a deliberate v1 judgment call, documented
in `lanes.py` so it can be reviewed as a decision and retuned by NE-066
rejection feedback later.

Each `FixProposal` is typed: `lane`, `cluster_id`, evidence (`event_ids` +
`signal_ids`), a templated `summary`, `confidence` (`0.4 + 0.1·n`, capped 0.9),
and `priority` (HIGH for policy or ≥3 signals, MEDIUM at 2, else LOW).

### Proposals are proposals — never changes

This unit **only produces typed proposals**. It NEVER applies a change to
content, retrieval config, schema, or policy. Every proposal ships
`status="proposed"` (hard-validated); NE-066's propose-then-approve gate owns
every status transition and the actual application via human review. The
engine's complete business-write surface is `ne_fix_proposals` + its own
`ne_curation_cursor` + its own `curation.run` audit event — nothing else is ever
written.

### Golden-set candidates

Every retrieval-lane cluster additionally emits a `golden_set_candidate`
proposal — a ref-based `GoldenSetCandidate` (a `question_ref`, the
`wrong_source_refs`, and evidence event ids) that feeds the NE-024 eval golden
set (→ NE-075). `expected_source_refs` is **empty in v1 on purpose** (C12: raw
query/doc text never persists), so the steward resolves the actual question +
expected source at NE-066 review from the refs and evidence.

### Graph-extraction batches (NE-T2X)

`ProposalKind.GRAPH_EXTRACTION_CANDIDATE` is a job-emitted kind (like
`ENTITY_MERGE_CANDIDATE`) on the existing `entity_resolution` lane — the
NE-T2X ne-brain-graph LLM-extraction job (future) proposes one
`graph_extraction_candidate` per (document × run), never per assertion.
Its typed payload is a `GraphExtractionBatch`: a `document_ref` + `run_id` +
`source_system` and a non-empty tuple of `GraphExtractionAssertion` rows
(`link` / `new_entity` / `new_edge`, each carrying typed contract fields, a
display name, and an evidence chunk **ref** — never raw chunk text).
`render_proposal_diff` renders the whole batch as ONE reviewable artifact
with a `### Row N` block per assertion and three per-row decision checkboxes
(`- [ ] approve` / `- [ ] edit (note the correction)` / `- [ ] decline`), so a
steward reviews a document's extraction in one pass instead of one act per
assertion.
Content-addressed helpers (`graph_extraction_row_id`,
`graph_extraction_proposal_id`) make re-running extraction over the same
document store-idempotent against model-phrasing/confidence drift across
runs — **not** against a display re-wording that survives the design's
`canonical_form` normalization into a genuinely different `entity_key`
(design §3.3: that fragments deliberately into a second node, healed by the
existing Splink ER lane rather than fuzzy keying; see the docstring on
`graph_extraction_proposal_id` for the full caveat). All three extraction
modes (ingest-gate / read-flagged / retroactive-autonomous review) ride this
one proposal kind through the unchanged NE-066 state machine.
Because the rendered artifact is the steward's only view of a batch (the raw
payload is kept off the API wire), the renderer never lets a malformed
stored row vanish silently — a non-`### Row`-shaped entry in `assertions` is
counted explicitly as `- unrenderable rows: N` alongside `- assertions: N`,
so what the steward sees can never understate what was stored.

### Governance & scheduling (NE-034)

The run ships as a single **tier-1** tool `write.internal.flywheel.curation_run`
(the `write.internal.` prefix classifies tier 1 under `tiers.rego`) plus its
handler and a `ScheduledJob` that grants **exactly that one tool** — so the cron
agent is isolated, governed, and toolset-scoped by NE-034's existing machinery,
with no broad tool access. The composition root wires it (live wiring is
composition-root / NE-070 work):

```python
from ne.flywheel import (
    curation_agent_from_settings,
    curation_tool_spec,
    curation_tool_handler,
    scheduled_curation_job,
)
from ne.runtime.tools import ToolBinding

agent = curation_agent_from_settings()          # CurationAgent over the in-VPC engine
binding = ToolBinding(                           # into the ONE GovernedToolExecutor
    spec=curation_tool_spec(),
    handler=curation_tool_handler(agent),
)
executor.register(binding)
scheduler.schedule(scheduled_curation_job(cron="0 2 * * *"))  # CronScheduler
```

Every `curation.run` **dual-appends** the same `AuditEvent` to the ne-events
outbox AND the tamper-evident `ne_audit_log` hash chain in the run's
transaction (plan §1.4 event-spine rule). Fail-closed: any dual-append failure
raises `CurationAuditError` and the whole run rolls back — no proposals, no
cursor advance, nothing without its audit record (C6). A zero-signal run still
emits its audit event (a scheduled governance-relevant run happened). The
handler **ignores model-supplied args** entirely — the run has no injectable
surface.

### Determinism & the LLM seam

No LLM participates in any clustering or routing decision — CI never depends on
live-LLM nondeterminism. Proposal *text* sits behind the `SummaryComposer`
Protocol; the default `TemplateSummaryComposer` is pure templating, a scripted
composer replaces it in tests, and a gateway-backed composer is future NE-066+
work injected at that seam (no gateway import lives in this package).

Multi-worker note: the cursor upsert is single-writer-safe (same as the NE-034
scheduler); real multi-worker double-curation safety under concurrent Postgres
workers is LIVE-PARKED to NE-070.

## Propose-then-approve (NE-066)

The apply-gate. A steward reviews each typed proposal as a concrete diff and,
if it is good, applies it *through the normal git-PR / OPA-bundle-deploy flow*
and records that the change landed. **v1 is propose + TRACKED MANUAL apply
only** — the platform renders a reviewable diff and tracks apply status; it
NEVER applies a change itself. Auto-apply and auto-re-index are v1.1.

### Lifecycle

```
proposed --approve--> approved --apply--> applied      (applied is terminal)
   \                      /
    \--reject-->  rejected  <--reject--/                (rejected is terminal)
```

`proposed -> applied` is deliberately illegal: human approval is a distinct,
separately-recorded act, so a change can never reach `applied` without a human
having first approved it. Every transition writes ONE append-only
`ne_proposal_reviews` row (who / when / config_hash) and dual-appends a
`proposal.<action>` audit event to the ne-events outbox AND the ne-audit hash
chain in the SAME transaction (fail-closed: a chain-append failure raises
`ReviewAuditError` and rolls the whole transition back). The current status is
a FOLD over the append-only review log — NE-065's `ne_fix_proposals` store is
never UPDATEd (its `status` column stays `"proposed"`).

### Rendering a reviewable diff

`render_proposal_diff` is a pure, deterministic, injection-safe function:

```python
from ne.flywheel import render_proposal_diff, ProposedFileChange

# NE-065 v1 proposals carry no concrete file content -> a NEW-FILE review stub:
artifact = render_proposal_diff(proposal)
artifact.kind      # DiffArtifactKind.GIT_UNIFIED_DIFF  (content/retrieval/schema)
                   # DiffArtifactKind.OPA_POLICY_DIFF    (policy -> comment-only .rego)
artifact.patch     # a git-style unified diff (a human applies via a PR)
artifact.pr_body   # deterministic PR-body markdown (metadata + summary + patch)

# When a steward has authored the actual reviewed content, pass a concrete
# before/after change (the TRUSTED seam — that text IS the artifact):
artifact = render_proposal_diff(
    proposal,
    change=ProposedFileChange(path="docs/runbook.md", before_text=old, after_text=new),
)
```

Injection-safety trust boundary (C12): EVERY proposal-carried string (`summary`,
golden-set `notes` + refs, and all `str` metadata fields) is **untrusted** and
reaches an artifact through exactly ONE of three deterministic sanitizers — one
uniform, greppable boundary:

- `_safe()` — inline / single-line interpolation (bullet values, comma-joined
  refs, metadata fields): drops non-printables, collapses all whitespace to
  single spaces (line-start markdown becomes structurally impossible), and
  backslash-escapes mid-line markdown/GitHub-significant characters (code spans,
  links, raw HTML, emphasis, tables, `#123` issue-closing refs, `@` mentions);
- `_fenced()` — multi-line untrusted markdown, wrapped in a dynamic fence longer
  than any backtick run inside it (a ``` ``` ``` in the text can never break out);
- `_comment_lines()` — the policy lane's rego, where every added line is a `#`
  comment (rego source in a summary can never escape).

The PR **title**, the `# {title}` **H1**, and all **filenames** derive
EXCLUSIVELY from TRUSTED fields — an allowlist-validated lane (an out-of-enum
lane fails closed with a `ValueError`) plus the `sha256(proposal_id)` stem —
never from the summary. A `ProposedFileChange` is the one trusted content input;
its `path` is still validated against traversal / absolute / backslash / NUL /
control-and-non-printable characters. The renderer has NO write capability by
construction (no SQLAlchemy / network import) — it produces artifacts, never
mutations.

### Tracked manual apply

A steward applies the change through the normal flow, then records it:

```python
from ne.flywheel import ProposalReviewService, RejectionCode

svc = ProposalReviewService(engine)
svc.approve(proposal_id, provenance, note="looks correct")
# ... the human opens + merges the PR / deploys the OPA bundle ...
svc.record_apply(proposal_id, provenance, applied_ref="https://git.example/pr/42")
```

`applied_ref` is the PR URL / commit sha / OPA-bundle version. `record_apply`
only RECORDS a human-performed apply (who / when / ref); it NEVER performs one.
There is no code path in `src` that calls it, no agent-facing tool exposes any
review action, and the renderer cannot write — so `applied` is reachable only
via an explicit human-driven call (structurally tested).

### Rejection feedback (loop closed)

Every `reject` atomically appends a typed correction signal back into the
NE-064 store (and its outbox mirror) in the same transaction as the review row:

- `signal_kind = thumbs_down` (honest "explicit negative feedback on an output"
  semantics; deliberately **not** `approval_rejection`, which would route every
  rejection into the policy lane via `route_cluster`);
- `subject_ref = "proposal:<id>"`;
- provenance carries `rejected_proposal_id`, `proposal_lane`, `rejection_code`
  (`incorrect` / `unsafe` / `duplicate` / `stale` / `out_of_scope` / `other`),
  and `review_id`.

A rejected fix is itself a correction signal ("this proposed fix was wrong"),
so the flywheel learns. **Known v1 wrinkle:** these fed-back `proposal:<id>`
signals may themselves be clustered by a future curation run into content-lane
meta-proposals about proposals; retuning/consumption of the feedback is
explicitly later work (per `lanes.py`) — this unit closes the loop, it does not
tune it.

### Safety invariants

- **v1 propose + tracked manual apply ONLY** — no auto-apply, no auto-re-index.
- The unit's COMPLETE write surface is `ne_proposal_reviews` +
  `ne_correction_signals` (rejection feedback) + `ne_outbox_events` +
  `ne_audit_log`/`ne_audit_head`. It NEVER mutates the content / schema /
  config / policy / vector stores, and never UPDATEs `ne_fix_proposals`.
- Diffs are deterministic, injection-safe artifacts (data rendered as a patch,
  never evaluated).
- Every transition is audited (config_hash-stamped, dual-append, fail-closed).
- Concurrency (P2-13): each transition takes a `SELECT ... FOR UPDATE` row lock
  on the proposal BEFORE folding status, so racing approve/reject serialize on
  Postgres — the loser folds the winner's committed status and either raises
  `InvalidTransitionError` or records an honest follow-on transition (a rejected
  proposal never reaches `applied`). The unique `(proposal_id, action)` index
  remains belt-and-braces against same-action duplicates. `FOR UPDATE` is a
  no-op on SQLite (CI/dev-only); the live multi-worker interleave proof is
  env-gated (`NE_TEST_POSTGRES_URL`).

### Composition-root wiring

```python
from ne.flywheel import review_service_from_settings

# ensures the full same-engine closure (reviews + proposals + signals + outbox
# + audit chain) on NE_FLYWHEEL_SIGNAL_DATABASE_URL:
svc = review_service_from_settings()
```

The composition root MUST hand this SAME engine to the signal store and the
audit sink, or the same-transaction dual-append (review row + rejection signal
+ audit event) cannot hold. The pysqlite BEGIN-recipe gap (SPEC §11) is
NE-CLEAN2's cross-cutting fix; production is Postgres.

## Configuration

| Setting | Env var | Default |
| --- | --- | --- |
| `signal_database_url` | `NE_FLYWHEEL_SIGNAL_DATABASE_URL` | `""` (empty; caller must set or pass an `engine=` directly) |
| `max_text_chars` | `NE_FLYWHEEL_MAX_TEXT_CHARS` | `4000` |
| `curation_batch_limit` | `NE_FLYWHEEL_CURATION_BATCH_LIMIT` | `500` |
| `curation_cron` | `NE_FLYWHEEL_CURATION_CRON` | `"0 2 * * *"` |

Also configurable via `ne.toml` under the `NE_FLYWHEEL_` prefix (see
`FlywheelSettings`).

Status: backend capture (NE-064) + the scheduled curation agent + typed fix
proposals + golden-set candidates (NE-065) + the propose-then-approve gate
(diff renderer + review state machine + rejection feedback, NE-066). The
steward-queue console UI (NE-067) and auto-apply + auto-re-index (v1.1) are
follow-on work.
