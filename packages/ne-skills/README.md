# ne-skills

Skill Forge **read side** (NE-SKILL-1a). Ships a skill library that EXISTS and is
USED — bundled/engagement packs reconciled into a Postgres/SQLite registry,
discoverable and firable by agents through two governed tools, ACL-trimmed by the
platform's one `PrincipalExpander` — before any forging exists.

## Modules

- `registry.py` — append-only version/lifecycle/invocation tables + folded reads;
  the **R-28 hash-check chokepoint** (`verified_current_packs` / `get_current`)
  every read path shares.
- `standard.py` — the deterministic house-standard validator (typed violations).
- `packio.py` — SKILL.md + YAML frontmatter (D-4); `yaml.safe_load` only.
- `reconcile.py` — boot reconcile, edit-wins by content hash; invalid → quarantine,
  manual → quarantined-pending-forge.
- `visibility.py` — the ONE-ACL filter: a package-local `GroupExpander` Protocol
  (no ne-permissions import) + `visible()`/`match_labels()`.
- `active_set.py` — ACL ∩ capability filter → usage-ranked → capped index.
- `tools.py` — `read.skills.search` / `read.skills.view` specs + handler factories.
- `substrate.py` — `PydanticAiSkillSubstrate` read methods + the
  `ne.plugins.skill_substrate = pydantic-ai` entry point.
- `config.py` — `SkillsSettings` (joins the composed platform config hash).

## The D-10 boundary (stack-agnostic core)

The **runtime** closure is `ne-contracts + pydantic + sqlalchemy + pyyaml + anyio`
only. ne-skills imports NO provider SDK and NO `pydantic-ai` / `dbos` / `ne.runtime`
— governance, the executor, and the model path are composition-root concerns. The
boundary is enforced by `tests/test_import_boundary.py` and the `.importlinter` C3
rosters. `ne-permissions` / `ne-runtime` / `ne-govern` / `ne-mcp` enter only as
`[dependency-groups] dev` (the ACL permission-trap + executor/MCP suites).

## The Forge (write side, NE-SKILL-1b)

The forge folds completed work into `skill_candidate` proposals that ride the
NE-066 gate, and lands approved skills through the one-transaction apply seam.

- `signature.py` — `workflow_signature(snapshot)`: the sha256 over the ordered,
  names-only `step.action` tuple (the workflow identity — content-addressed dedup).
- `authoring.py` — the toolless `skill-author` prompt + `render_author_task`
  (fences untrusted intent/steps inside `<untrusted-workflow-data>` markers) +
  `parse_author_output` (forces `source=forged`, `acl_labels=()`, empty provenance).
- `substrate.py` — `snapshots()` (audit-log-derived: `subagent.complete` +
  `session.run` terminals, trace-joined to `tool.execute` steps) + `author()`
  (dispatches the injected author-dispatch seam).
- `forge.py` — `SkillForge.run_once()`: floor worthiness (`succeeded ∧ ≥2 distinct
  actions`), author the top candidate, validate + R-23 lint, propose in ONE
  transaction (proposal + cursor + outbox + `skill.forge_run` audit).
- `governed_forge.py` — the tier-1 `write.internal.skills.forge_run` cron surface.
- `apply.py` — the `apply_skill_candidate` steward seam: approve → register in ONE
  transaction via the review service's `record_apply(extra_writes=…)` same-conn
  hook (R-20), with a C10 hash-chained `skill.apply` entry binding
  `{content_hash, acl_labels, approver, review_id}`.

### Enabling the forge

The forge cron is **default-off** (`forge_enabled=False`) pending NE-SKILL-2 — no
forge GA until the eval-gate + full scorer land (R-6). To enable in a dev stack:

- Set `NE_SKILLS_FORGE_ENABLED=true` (+ `NE_SKILLS_FORGE_CRON`, `_BATCH_LIMIT`).
- The `skill-authoring` gateway task route MUST exist (maps to a private/open-weight
  model — snapshot intents are customer text; frontier is structurally refused,
  C3). The dev stack seeds it via `tools/ne-cli` (`ne demo up --gpu-vllm`).

### Adversarial posture (R-23, hazard A)

The author agent's input is attacker-influenced and its output is injected
fleet-wide. Defenses: the untrusted record is FENCED in the prompt; `standard.py`
runs a fail-closed body-size cap (reject, never truncate) + the same four-rule
injection lint (embedded-URL / curl-pipe / base64 / fetch-then-follow) over
**both** the body (`lint_body` → `body-*`) and the **description**
(`lint_description` → `description-*`) of **every** pack (forged and
hand-dropped); the console renders the body inert with explicit "untrusted —
injected into every agent" framing. A hijacked author has zero tools and its draft
can at most become a HITL-gated `proposed` row.

The description carries one rule the body does not — `description-control-chars`,
which rejects any `\x00-\x1f` / `\x7f`. The description is the higher-value target
of the two fields: the body is served only through the governed
`read.skills.view` firing, while the description is rendered into
`active_set.render_index` and reaches the model on every turn, unasked. A newline
in it also used to inject extra index lines, and both withdraw paths
(`registry.retire`, `substrate.withdraw`) line-filter on the single-line prefix
`- {name} (`, so a forged line survived removal permanently. `render_index` (and
the `read.skills.search` result block) now flatten control characters at render
time as well, so the rendered index is always exactly `len(packs) + 1` lines
whatever the registry holds.

Both lints REJECT and never rewrite: `description` is inside `content_hash`
(`_HASH_ORDERED`), so a normalizing validator would move the hash of an
already-registered pack and the R-28 check would then read it as TAMPERED.

### The 1b→2 eval-gate window (R-6 honesty)

The `skill_candidate` payload ships `eval_run: null` as the NE-SKILL-2 hook. 1b
does NOT enforce an eval gate — HITL + the apply seam gate everything. Do not
announce forge GA until NE-SKILL-2 is in.

### Snapshot source exclusion (verified deviation)

Cron `job.run` events are NOT a v1 snapshot source: the scheduler emits `job.run`
OUTSIDE `run_trace_scope`, so it carries no `trace_id` to join its `tool.execute`
steps. v1 sources = sub-agent runs (`subagent.complete`) + interactive sessions
(the D-2 `session.run` event this unit adds). Cron coverage is a follow-up.

## Birth gate (NE-SKILL-2)

NE-SKILL-2 turns the 1b forge into a governed **birth gate** — the moat versus
skill libraries that rot at scale. Four controls sit between a floor-passing
snapshot and a surfaced proposal:

- **Worthiness ranking** (`worthiness.py`). The FLOOR is unchanged (`succeeded ∧
  ≥2 distinct actions`). Over the floor, candidates are RANKED by a deterministic
  composite key — repetition-in-30d (PRIMARY: the same `workflow_signature` seen
  again over the sightings history), then distinct principals (generalizability),
  then step count (efficiency), then recency, then a signature tie-break.
  Worthiness **RANKS, it never DROPS**: a worthy candidate over the cap persists
  in `ne_skill_forge_queue` and is authored on a later run — only suppression,
  a standard violation, or an eval failure removes a candidate (each honestly
  counted in `ForgeOutcome`).
- **Rate cap.** ≤2 OPEN `skill_candidate`/`skill_update_candidate` proposals
  (status folded from the review log, mirroring `steward_metrics.folded_status_map`)
  and ~1 new proposal/day (a 20h cron-jitter-tolerant window over the last
  skill-lane proposal). Excess queues silently, ranked.
- **Decline suppression** (`suppression.py`) — the **first consumer** of the
  NE-066 rejection signal. The `thumbs_down` signal already existed (every
  `ProposalReviewService.reject` emits one carrying `rejected_proposal_id` /
  `proposal_lane` / `rejection_code`) but nothing consumed it; this module BUILDS
  the fold: a rejected `workflow_signature` is suppressed for 90 days UNLESS its
  recurrence grows by ≥3 sightings after the rejection (or the window expires).
  A legitimate re-proposal bumps the cluster epoch (`{sig}:r{n+1}`), minting a
  fresh content address.
- **Eval gate** (`evals.py`). `SkillTriggerEvalSuite` implements the frozen
  `ne.contracts.evalobs.EvalSuite` Protocol and runs a **deterministic**
  routing+utility eval through the REAL `read.skills.search` scorer
  (`ne.skills.tools.score_pack`/`search_packs`) — no judge, no embeddings, no
  egress (SPEC §3.1 keyword-default). Metrics (all thresholded at 1.0,
  absent ⇒ FAIL): `trigger_recall` (each trigger ranks the pack top-k),
  `near_miss_precision` (other packs' triggers must not rank it top-1; 8
  stopword-free generic decoys must not score a full trigger-substring hijack),
  `utility_step_coverage` and `utility_tools_grounded` (§4.7 utility band — the
  pack must reference the actions that made the workflow succeed). An
  **anti-vacuous** guard (NE-CKC6) fails an empty trigger set or an empty
  near-miss set — the suite can never pass by testing nothing. Every run is
  recorded append-only via `EvalRunStore` with its `config_hash`.

### routing_hash binding (D-12)

The eval binds to the label-EXCLUDED `routing_hash`, never the full
`content_hash`. Because `acl_labels` are stamped by the steward only **at apply**
and are excluded from `routing_hash` by the frozen contract, relabel-at-apply can
never invalidate a passing eval (the full `content_hash` still carries labels for
version identity + hash-pinned serve).

### Two enforcement points (fail-closed)

The passing run's ref (`build_eval_ref`: `suite`, `run_id`, `config_hash`,
`routing_hash`, `passed`, metrics) is attached to the proposal payload's
`eval_run` field. Both governed seams then ENFORCE it:

1. **`apply.py`** re-verifies via `verify_skill_eval_ref` before the label
   decision — the pack is refused unless the ref is present, `passed`, its
   `routing_hash` matches the recomputed pack, and the stored `EvalRunStore` row
   (the tamper-resistant anchor) also passed with a matching `routing_hash`.
2. **The steward `approve` route** runs the same check kind-guarded
   (`verify_skill_proposal_payload`) — a `skill(_update)_candidate` without a
   passing, store-anchored eval is a 409; generic proposal kinds take zero new
   code paths.

The forge cron stays **default-off** (`forge_enabled=False`) — GA is an
orchestrator decision, not this unit.

## Anti-pollution: DUPLICATION (NE-SKILL-3)

The dedup layer stops the library filling with near-identical siblings. It runs at
**proposal time over the WHOLE verified registry** (all sources, not just forged)
and, on a duplicate, proposes an **UPDATE to the existing skill — never a `-2`
sibling**.

- `near_dup.py` — the stdlib-clean detector.
  - **Default install (no embedder — SPEC §3.1 keyword degradation, C4-clean):** a
    shared exact trigger phrase (casefold+strip match between trigger lists) OR
    token-set **Jaccard ≥ `jaccard_threshold`** (default `0.5`) over the candidate
    text (`name + description + triggers`). Boundaries: `0.49` no-flag, `0.50`
    flag. Fully functional with no embedder wired.
  - **Embedder opt-in (`ne.plugins.embedder` slot, in-VPC TEI, C2-clean):** ONE
    batched `embed` over the candidate + the non-same-name library, pure-python
    cosine (no numpy). `cosine ≥ cosine_duplicate` (default `0.85`) ⇒ `duplicate`;
    `[cosine_overlap, cosine_duplicate)` (default `0.70`) ⇒ `overlap` (warn-only,
    stays a `skill_candidate`). Thresholds are `NearDupConfig`, tuned on the real
    library. The embedder arrives ONLY as the frozen
    `ne.contracts.knowledge.Embedder` Protocol, injected at the composition root —
    `near_dup.py` opens no sockets and imports no HTTP client (D-10 preserved).
    The embedder-path live test **skips-with-reason** when the slot is unconfigured
    (`NE_SKILLS_TEST_EMBEDDER_URL` unset) — never vacuously green.

### Update-not-sibling

On a `duplicate` hit the forge calls `substrate.author(snapshot, existing=<pack>)`
(which forces `name=existing.name`, `version=existing.version + 1`) and emits a
**`SKILL_UPDATE_CANDIDATE`** targeting `(existing.name, current_version + 1)`. The
registry's unique `(name, version)` + append-only versions + the apply seam's
exists/version checks make a `-2` name **structurally impossible** — the unit adds
NO suffix logic anywhere; the prior version row stays restorable by construction.
The update path's eval library **EXCLUDES the same-name pack** being updated (else
`near_miss_precision` would false-fail against its own predecessor). The
already-proposed fold reads BOTH kinds, so an open update candidate is not
re-proposed on a later run.

## Edit-before-approve (NE-SKILL-3, security-relevant)

The `apply-skill` seam accepts steward edits to `name` / `description` /
`triggers` **only — never `body`** (a body edit is reject-and-re-forge; the wire
`SkillEditFieldsRequest` and `apply.SkillEdits` are `extra="forbid"`, so a smuggled
`body` key is a LOUD 422). An update candidate's `name` is refused (renaming would
mint a sibling). Edits rebuild the pack via `SkillPack.model_validate` (**never
`model_copy`** — an empty-triggers / over-length edit must re-trip pydantic).

Because a routing-relevant edit changes the `routing_hash`, the seam runs a
**FRESH synchronous `SkillTriggerEvalSuite`** on the edited, PRE-label pack
**before** the one-transaction commit and **REFUSES the commit fail-closed** if it
fails — no review transition, no registry row, no `skill.apply` audit entry. The
fresh eval replays the names-only `WorkflowSnapshot` carried in the proposal
payload (a legacy payload without it + edits is a 409). The `skill.apply` audit
detail records `edited` + the `eval_run_id` (fresh when edited). An UNEDITED apply
is byte- and behaviour-identical to NE-SKILL-2 (the stored-ref path is unchanged).

### Not in this unit (tracked follow-ups)

- **R-27** (edit-wins reconcile needs an `origin_hash` distinct from
  `content_hash`) is a real NE-SKILL-1a reconcile gap, NOT the dedup path — the
  update path only appends version rows through the existing apply seam, so no
  registry-schema reshape is forced here.
- **Embedder composition wiring** into `ne-worker`/`skill_forge.py` (a real TEI
  `Embedder` + threshold env knobs) is a one-line composition-root follow-up; the
  default (Jaccard) install is fully wired via constructor defaults.
- **Trigger-conflict score-margin lint** is NE-SKILL-4 (the Gardener), not here.

## Anti-pollution ROT — the rot layer (NE-SKILL-4)

The final anti-pollution unit keeps the ~150-pack **active set curated** while the
full library stays searchable. Five pieces:

- **`telemetry.py` — the invocation fold.** `fold_invocations` folds
  `ne_skill_invocations` into a materialized `ne_skill_usage` rollup (per-skill
  fired / success / fail / unknown + `last_fired_at`), cursor-idempotent (the
  cursor watermark and the counters move in ONE transaction). `attribute_outcomes`
  does best-effort approximate attribution (design §10-Q6) of NULL-outcome firings
  from a terminal `session.run`/`subagent.complete` audit event joined by
  `session_id ↔ trace_id`.
  - **R-26 (normative):** for the pydantic-ai binding, `read.skills.view` is the
    **SOLE writer** of `ne_skill_invocations`. `collect_invocations` stays in the
    frozen Protocol for the out-of-band CC binding (NE-SKILL-5) but is **NEVER
    drained in-process** — a drain would re-insert view-written rows and inflate
    the counters. The fold reads `ne_skill_invocations` directly; a source-level
    test asserts `telemetry.py` never references `collect_invocations`.
  - **Best-effort telemetry:** `read.skills.view` serves the body even when the
    invocation INSERT fails (logged, not raised). The hash-chained `tool.execute`
    audit event is the **canonical** firing record from which counts are
    reconcilable, so a dropped telemetry row can only UNDERCOUNT — which merely
    DELAYS retirement (the safe direction), never corrupts a lifecycle decision.

- **`retirement.py` — the retirement lane.** A candidate is an ACTIVE pack unfired
  for 90d **and** with < 3 lifetime fires (measured against `max(last_fired_at,
  registered_at, last_restored_at)` so a fresh register/restore resets the window;
  clock-injected). `retire_skill` appends a `ne_skill_lifecycle('retired')` event
  (retire **loudly** — reason + date ride the reason field) and withdraws the name
  from **every** materialized surface; content rows in `ne_skill_versions` are
  **NEVER deleted** — restore is one lifecycle transition. Failure flags (≥50% fail
  over ≥4 known-outcome fires) trigger **REVIEW, never auto-retire** (attribution
  approximate).
  - **D-8:** retired packs leave the active set AND default `read.skills.search`
    **structurally** (the status fold in `verified_current_packs`). The steward
    library list (`GET /v1/steward/skills`) is the explicit full-library surface
    and keeps showing retired rows with a one-tap Restore.

- **Resurrection guard (`reconcile.py`).** Retirement is a lifecycle-log fact bundle
  presence cannot override — content-hash edit-wins is **NOT** the protection.
  Boot reconcile emits a `registered`/`restored` transition **only** for absent
  (register) or quarantined-repaired (restore) names; a RETIRED name is never
  re-quarantined by a broken file (`skipped_retired`), closing the
  retired → quarantined → restored(active) laundering path. A retired pack may
  still edit-wins-append a content version (harmless — it stays retired, the newer
  content is what a future restore serves).

- **`gardener.py` + `governed_gardener.py` — the monthly Skill Gardener.** A
  deterministic, **model-free** tier-1 governed cron tool
  (`write.internal.skills.gardener_run`, monthly `30 4 1 * *`) — the same isolated
  cron-agent pattern as the forge, minus the sub-agent (zero egress, C2/C3). One
  `run_once()` produces **ONE persisted digest card** with: retirement candidates
  (incl. **superseded**-by-near-dup on the lower-ranked pack, reusing NE-SKILL-3's
  `find_near_dup`), failure flags, trigger overlaps, near-dups, unknown
  `requires_tools`, bundled-origin hash drift, and stale (>14d open) proposals. The
  tool RESULT is **counts-only** (C12); names/bodies live only in the persisted
  report read by the steward-gated digest route.
  - **D-7 (stated posture, not inherited):** the Gardener **FLAGS** stale proposals
    but **NEVER rejects** — an auto-reject would be the platform's first non-human
    review-log transition (a C6-posture change buried in a maintenance job). A human
    batch-taps the reject via the existing steward reject route (the console loops
    one governed reject per item).

- **Creation-time trigger-overlap lint (`standard.py` + `apply.py`).** The
  `apply-skill` gate rejects an apply whose pack shares a normalized trigger phrase
  with another ACTIVE pack (closing the steward-edit hole; the model-authored
  overlap path is already routed to UPDATE-not-sibling by NE-SKILL-3). The Gardener
  runs the same lint across the active set at sweep time.

### Included §6.5 enhancements vs deferred follow-ups

INCLUDED: superseded-retirement (ties naturally to the near-dup sweep) + the
materialized `ne_skill_usage` counters rollup. DEFERRED (tracked, NOT half-built):
full-library soft-cap / LRU-by-utility (**NE-SKILL-ROT-CAP**), `requires_tools`-vs-
actually-invoked semantic-hijack cross-ref (**NE-SKILL-ROT-XREF**), a CI gate
running `standard.py` over the bundled dir (**NE-SKILL-CI-AUDIT**), and
`ne_skill_invocations` time-partition/retention (**NE-SKILL-ROT-RETENTION**).
LIVE-PARKABLE: the monthly cron firing on the compose stack; the Slack digest
garnish (rides the frozen `ChatAdapter.send_digest` unchanged).
