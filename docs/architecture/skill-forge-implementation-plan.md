# Skill Forge — Implementation Plan (NE-SKILL-0 · 1a · 1b · 2 · 3 · 4 · 5)

**Date:** 2026-07-20 · **Status:** DESIGN PLAN — for owner review + adversarial hardening, not yet a dispatch order.
**Design:** `docs/architecture/skill-forge-neutron-enterprise-design.md` (the consolidated, ONE-ACL design, §7.7). This plan operationalizes that design's §9 decomposition unit-by-unit; where this plan is more specific than the design, this plan wins for the builder.
**Verification bar:** every normative claim below traces to source read on 2026-07-20 (file:line inline) or is explicitly flagged **[ASSUMPTION]** / **[OPEN DECISION]**. The deepen pass re-verified the design's load-bearing citations against code and surfaced one prose-stronger-than-code nuance (in-memory `ProcessRegistry`, §R-B below), now also annotated in the design doc.

**Adversarial verification (Codex, 2026-07-20, final gate):** the highest-stakes NEW claims from the deepen pass were re-checked against code. Confirmed TRUE: R-20 (`record_apply→_transition` opens its own `self._engine.begin()` at review.py:425 — an external atomic wrapper genuinely conflicts), the ACL pattern (trimming.py:129 package-local Protocol, `ne-permissions` is a dev-dep not runtime), and the `_run_handler` no-`finally` fact (used correctly — it argues the FUTURE carrier needs a `finally` reset, NOT an existing audit gap; failures already audit at middleware.py:276-284). Three overstatements were found and corrected in place (all marked *"Codex adversarial pass"*): (1) the eval-hash gate "can **never** match" — corrected to "cannot match once non-empty labels are assigned; still matches when labels stay `()`"; (2) a handler "**cannot** see its caller" — corrected to "principal is not a formal ARGUMENT, but handlers read it via ContextVar today (TURN_PRINCIPAL); the gap is a surface-independent seam"; (3) labels called "routing-**irrelevant**" — corrected to "irrelevant to routing QUALITY but relevant to authorization-time discovery." Plus one substantive addition Codex surfaced: the two-hash split (D-12) makes labels tamper-*evident* but does not *bind* the assignment to an approval — a signed registration entry on the C10 hash-chain binding `{content_hash, acl_labels, approver, review_id}` is required, now folded into D-12/R-21.

---

## Enhancement Summary (deepen pass — 2026-07-20)

**Lenses run (8):** system-architecture / module-contract discipline · security · python-quality (contracts types + Protocol) · simplicity-YAGNI · agent-native parity · data-integrity · library-curation (vs the ClawHub/OpenClaw supply-chain incident + 2026 skill-lifecycle literature) · test. Findings were merged across lenses, and the load-bearing NEW code claims were re-verified against source this pass (review.py:425, middleware.py:270-306, curation.py:272-277, governed.py:19-25, trimming.py:129, `.importlinter` C3 chokepoints).

**Top improvements folded in (each now a risk row + section note):**
1. **Package boundary is drawn one layer too deep (R-19, §2.1/§9).** The production binding (`ne/skills/substrate.py`) transitively imports `ne.runtime` (→ pydantic-ai/dbos) once `author` lands, so the package-level "stack-agnostic core" claim is not import-linter-enforceable — the plan already proves it knows this by splitting the CC binding into `ne-skills-cc` (§7.1/D-9). Decide the symmetric split (or an intra-package module contract) **before 1a is cut**.
2. **The §3.5 "one transaction" is unachievable with the reused review API (R-20, data-integrity P1, CONFIRMED).** `record_apply → _transition` opens and commits its OWN `self._engine.begin()` (review.py:425) with no external-write injection — the ne_skills writes and `record_apply` are two transactions. Needs a same-conn `extra_writes` hook on `_transition` mirroring curation.py:272-277.
3. **`content_hash` includes `acl_labels`, but labels are assigned at apply (R-21, data-integrity P1).** Once any non-empty label is assigned, the §4.4(b) eval-hash gate cannot match on that path (it still matches when labels stay `()` or unchanged — "never" was overstated; corrected per the Codex adversarial pass). Recommend a label-EXCLUDED `routing_hash` for the eval gate + the full `content_hash` (labels in) for versioning/serve — **both frozen in NE-SKILL-0**.
4. **Authored body is never content-scanned (R-23, security + library-curation P1).** `standard.py` validates SHAPE only; the ClawHub exploit class (procedural "fetch-then-follow" bodies, README padding to evade scanners) survives the no-executables rule. Add a fail-closed body-size cap (reject, not truncate) + a full-body injection lint, and fence untrusted snapshot text in the author prompt.
5. **Default `acl_labels=()` = fleet-global (R-22, security P2).** A skill forged from one principal's workflow publishes to EVERY agent unless the steward remembers to restrict it — the inverse of the chunk model's fail-closed posture. Default a forged pack's suggested labels from the source workflow's principal/department.
6. **Bundled/engagement auto-upsert bypasses the eval gate AND HITL (R-24, library-curation P1).** The "trusted" supply-chain channel gets the LEAST scrutiny — backwards relative to the demonstrated ClawHub compromised-publisher path. Route engagement packs through the proposal gate + full-body lint.
7. **In-process invocation double-count (R-26, data-integrity P2).** For `PydanticAiSkillSubstrate` the `read.skills.view` handler AND the NE-SKILL-4 telemetry drain both write `ne_skill_invocations` → inflated firing counts corrupt retirement decisions. Make the view handler the SOLE writer; `collect_invocations` no-ops for the in-process binding.
8. **Contracts-shape corrections for NE-SKILL-0 (§1 review block):** `content_hash` should be a `computed_field` not a stored-field-plus-validator; sort set-semantic fields (`acl_labels`/`tags`/`platforms`) before hashing; give the cursor-pull a self-documenting `NamedTuple` (and stop claiming it "mirrors the flywheel signal-cursor pattern" — that pattern is an integer `after_id`, curation.py:172-304); type the `standard` param; the "normative" type block currently omits every validator its prose claims.

**NEW open decisions surfaced (need an owner call before the relevant unit dispatches):**
- **D-10 — package split for the production binding** (R-19): sibling `ne-skills-pydantic` / composition-root wiring vs an intra-package import-linter module contract. Blocks 1a's deliverable shape.
- **D-11 — split the two ne-runtime seams (ContextVar carrier + session-instructions) into a small NE-SKILL-0b runtime lane** (R-30) so the two highest-blast-radius edits get isolated review instead of riding inside M/L 1a.
- **D-12 — `routing_hash` (label-excluded) vs single `content_hash`** for the eval gate (R-21). Must be frozen in 0.
- **D-13 — `SkillDraft` (unsealed, no hash) vs `SkillPack` as `author()`'s return type** (§1 review block): resolves the "who computes the hash / where provenance is stamped" contradiction between contracts §1.2 and design §3.4. Frozen in 0.
- **D-14 — drop the dead `author(edits=)` parameter** or name its concrete caller (no described call path uses it; §3.5 applies edits at the ne-api seam, not via the substrate).
- **D-15 — defer the active-set ranking/cap + `ne_skill_surfaces` cache** behind a "filtered set exceeds token budget" guard (R-29); render the full ACL∩capability-filtered set directly in v1.
- **D-16 — a `read.skills.list` / empty-query enumerate primitive** for agent discovery parity (agent-native P3).

**Contradictions flagged for human review (not silently resolved):**
- **C-α (hash design).** Security P2 wants `acl_labels` INSIDE `content_hash` (so a tampered-labels row fails the hash-pin) while data-integrity P1 wants them OUT (so the eval gate matches). These are reconcilable — keep both a full `content_hash` (labels in, for versioning + hash-pinned serve) AND a `routing_hash` (labels out, for the eval gate) — but the two-hash decision (D-12) is an owner call and is recorded as such, not picked here.
- **C-β (surfaces cache).** Simplicity P2 wants the `ne_skill_surfaces` cache DEFERRED; §2.5 makes it the `materialize()` persistence target and data-integrity flags it as a staleness/leak surface. Deferring it (render full set live) removes the leak but changes the `materialize` wiring — a design trade recorded under D-15, not resolved here.

**Dropped:** the `test` lens returned a single placeholder finding (title `t` / detail `d` / where `w`) — ungrounded and non-actionable, so nothing from it was folded in. The architecture P3 (design §2 diagram says "ne-worker" only) needs no PLAN change — plan §2.1 already sites ne-skills in "ne-worker/ne-api" correctly; the **design doc's ASCII diagram** should be corrected to match (honesty note, no risk row).

---

## 0. Verified substrate map (what the plan builds ON, all re-checked this pass)

| Seam | Anchor (verified 2026-07-20) |
|---|---|
| Contracts freeze | `packages/ne-contracts/pyproject.toml:3` = `1.7.0`; `ne/contracts/__init__.py:36` `__version__ = "1.7.0"`; version ALSO pinned in `packages/ne-contracts/tests/test_import.py:11` and `tests/test_graph_citation_types.py:37` (both must move to `1.8.0` in NE-SKILL-0 or CI goes red) |
| ADR discipline | `docs/adr/README.md` ("any change to an ne-contracts Protocol requires an ADR + version bump"); package-local ADRs `packages/ne-contracts/adr/ADR-0003…0010` ⇒ next is **ADR-0011** |
| Plugin slots | `ne/contracts/plugins.py:20` (`PLUGIN_GROUP_PREFIX`), `PluginRegistry` :46–71; **26 existing `ne.plugins.*` slots** counted across workspace pyprojects — a slot "exists" the moment a provider declares an entry point; no central registration file |
| Conformance kits | `ne/contracts/testing/kits/{object_store,secret_store}.py`; `ne.contracts.testing.*` is **additive-exempt** from the freeze (SPEC top-matter, Checkpoint-A lock) |
| ACL expander | `PrincipalExpander.expand` — `packages/ne-permissions/src/ne/permissions/expand.py:85-144`: SYNC, returns `frozenset[str]`, NEVER empty, reserved-pid sentinel :102-106/:75, `TENANT_ALL`/`PUBLIC_LINK` only via the `is_known` gate :139-142. Docstring "labels a *chunk* may name" (:86) confirms item-agnosticism — chunks are merely the current consumer |
| ACL consumption pattern | `packages/ne-brain/src/ne/brain/trimming.py` — package-local `GroupExpander` Protocol :66-79 (ne-brain does NOT import ne-permissions; composition root injects the concrete expander), `acl_match_labels` :107-129, **deny-by-erroring** (expander exception propagates, never falls back unscoped) :121-122 |
| Proposal spine | `ne/flywheel/proposals.py` — `ProposalKind` :46-61 with the documented plain-`str`-column extension precedent :49-56 (columns :154-155); append-only content-addressed idempotent store :245-288; `read_proposals(kind=…, lane=…, status=…)` :290-313 |
| Lanes | `ne/flywheel/lanes.py:44-69` — job-emitted-lane precedent (`ENTITY_RESOLUTION`, `ONTOLOGY` :57-61, never returned by `route_cluster`); `diffs.py:70` `_KNOWN_LANES = frozenset(lane.value for lane in FixLane)` auto-trusts a new enum member |
| Review gate | `ne/flywheel/review.py` — `ProposalReviewService` :272, `approve`/`reject`/`record_apply` :303-351, transition table :143-150, `RejectionCode` :129-137, rejection→signal with `rejected_proposal_id`/`rejection_code`/`review_id` provenance :556-606, unique `(proposal_id, action)` index :183-188, `SELECT FOR UPDATE` serialization (docstring :58-72) |
| Cron-as-isolated-agent | `ne/flywheel/governed.py` — tier-1 `write.internal.…` ToolSpec :42-56, args-IGNORING handler :59-76, `ScheduledJob(toolset=(one_tool,))` :79-96; dependency rule "imports ONLY frozen `ne.contracts.runtime`" :19-25 |
| One-transaction curation | `ne/flywheel/curation.py` docstring (proposals + cursor + outbox + audit dual-append in ONE transaction; multi-worker LIVE-PARKED :41-44); `CurationAgent.run_once` :213 |
| C6 chokepoint | `ne/runtime/middleware.py:3-47`; `execute(tool, args, *, principal)` :149; **handlers receive args ONLY** — `binding.handler(args)` :275 (see §2.4, the principal-carrier decision); run-trace stamped on every tool audit event :332 |
| Tier mirror | `ne/runtime/actions.py:27-61` — `read.` ⇒ tier 0 :45-46, `write.internal.` ⇒ tier 1 :47-48; `validate_binding` `ne/runtime/tools.py:65-81`; `ToolBinding` :42-62 (`ToolHandler = Callable[[Mapping], Awaitable[str]]` :31) |
| Principal-scoped read-tool precedent | `packages/ne-api/src/ne/api/tools.py` — `read.knowledge.search` :37, `TURN_PRINCIPAL` ContextVar :41 set only by the chat route from the verified bearer, handler fail-closed on unset :62-64; **`ToolArgPolicy` must be merged into `guardrails.tools` before the config hash freezes** :43-48 |
| Dispatch/authoring | `ne/runtime/dispatch.py` — `FrontierRoutingDisabledError` :112-123, `AgentDefinition` :126-156 (`toolset=()` ⇒ ZERO tools :129-130; `route_task`/`route_lane`), `allow_frontier_subagent_routing=False` default :182/:200-204, instructions splice :403 |
| Cron instructions splice | `ne/runtime/scheduler.py:366` |
| Interactive session | `ne/runtime/agent.py` — `run()` :132, `run_trace_scope` :146, registry register/finish :148-151/:273-275; **both** `Agent(...)` constructions (grounding :194-202, plain :256-264) take NO instructions/system-prompt parameter |
| Process registry | `ne/runtime/processes.py:40-56` — **in-memory** dataclass records (`finished()` is an in-process iterator); durable terminal records exist only for cron runs (scheduler run rows, `scheduler.py:345-377`) and sub-agent dispatches (completion audit events, `dispatch.py` step 8) |
| Governed apply precedent | `packages/ne-api/src/ne/api/routers/steward.py` — `_require_steward` :104, queue :204-209 (cap :193), approve/reject/apply :212-279, digest :352-368 (`WeeklyDigest` in `ne/api/steward_metrics.py:101`), **`steward_apply_verified_template` :444-509 — the in-platform governed-apply-through-the-API-seam precedent skills follow** |
| Console | `ne-console/src/api/client.ts:404-441` (server-derived steward identity), `flywheel/ProposalQueue.tsx:241` (kind-conditional rendering precedent), `flywheel/DigestPanel.tsx`, `App.test.tsx` (route assertions — MUST be in mayTouch for any route change) |
| MCP parity | `ne-mcp/server.py:172-186` — advertises exactly `executor.tool_specs`; `call_tool` → `executor.execute` :188-197 (skills tools become MCP-visible with zero extra work) |
| Eval | `EvalSuite` Protocol `ne/contracts/evalobs.py:32-38`; `EvalRunStore` `ne/eval/store.py:90` (+ `ensure_eval_schema` :67); PASS/WARN/FAIL bands `ne/eval/thresholds.py`; judge-policy fail-closed + cross-family + calibration `ne/eval/suite.py:103-110`; `Judge`/`GatewayJudge` gateway-only `ne/eval/judge.py:3-15` |
| Digest chat garnish | `ChatAdapter.send_digest` — `ne-chat/slack/adapter.py:72`, `teams/adapter.py:59` |
| Worker composition root | `ne-worker/src/ne/worker/main.py` — `create_cron_scheduler` :159, job seeding :173-180 |
| MemoryPromoter analog | `ne/runtime/memory_promotion.py:76-150` — propose/distinct-approver/audit-first discipline |

**Dependency chain:** `0 → 1a → 1b → {2, 3} → 4`; `5` any time after `0`. Rationale unchanged from design §9: freeze the seam first; retire the discovery-routing risk (this stack's novel gap) with the read side before any forging exists; quality-at-birth (2) and dedup (3) are parallel once the forge exists; rot (4) needs invocations flowing.

---

## 1. NE-SKILL-0 — contracts: `ne/contracts/skills.py`, ADR-0011, 1.7.0 → 1.8.0 (S, contracts lane)

### 1.1 Scope (file-level)

| Change | File |
|---|---|
| NEW | `packages/ne-contracts/src/ne/contracts/skills.py` — the frozen types + `SkillSubstrate` Protocol + the canonical `skill_content_hash()` function (§1.2) |
| EDIT | `packages/ne-contracts/src/ne/contracts/__init__.py` — re-export the new names; bump `__version__` (:36) to `"1.8.0"` |
| EDIT | `packages/ne-contracts/pyproject.toml:3` — `version = "1.8.0"` |
| NEW | `packages/ne-contracts/adr/ADR-0011-skill-substrate.md` (MADR-lite: Status/Context/Decision/Consequences) |
| EDIT | `docs/adr/README.md` — index entry for 0011 |
| NEW | `packages/ne-contracts/src/ne/contracts/testing/kits/skill_substrate.py` — conformance kit (§1.4; `ne.contracts.testing.*` is additive-exempt, so the kit can evolve after the freeze) |
| NEW | `packages/ne-contracts/tests/test_skills_contracts.py` |
| EDIT | `packages/ne-contracts/tests/test_import.py:11` and `tests/test_graph_citation_types.py:37` — the two `"1.7.0"` assertions become `"1.8.0"` (grep-verified as the only version pins in tests) |

The `ne.plugins.skill_substrate` slot needs **no code in this unit**: slots are entry-point group names (`plugins.py:20`); ADR-0011 names the slot normatively and NE-SKILL-1a/5 declare the first entry points. State this in the ADR so nobody looks for a missing registration file.

**PORTABILITY INVARIANT (owner-directed 2026-07-20 — record in ADR-0011 as a frozen property, verified at the NE-SKILL-0 review gate).** `SkillPack` MUST be a lossless **superset of the cross-agent SKILL.md standard** (the format Claude Code / Cursor / Codex / Hermes consume): its fields map 1:1 to SKILL.md YAML frontmatter (`name`, `description`, `version`) + the fixed body sections (When to Use / Procedure / Pitfalls / Verification), and every neutron-specific field lives under a **`metadata.neutron.*`** namespace so a foreign harness ignores it rather than choking. This makes a neutron skill portable to another harness *by construction*, and lets the `ClaudeCodeSkillSubstrate` (NE-SKILL-5) write/read native `.claude/skills/SKILL.md` that round-trips byte-stable through `packio` (§7 DoD). **Two honest deviations to record in the ADR, both deliberate and both wideable later:** (a) `description ≤500` (vs Claude Code's stricter short cap) — chosen because our discovery routes on the description with no native loader (§6); a neutron skill may need description-trimming to satisfy a harness that enforces a short cap, but the reverse import always works; (b) **procedural-only in v1** (no `scripts/` executable artifacts) narrows below the full SKILL.md standard — chosen to keep the C6 governed-tool chokepoint airtight, explicitly wideable when a governed-execution story exists. The invariant is what makes "portable to other harnesses" true; the deviations are named so nobody mistakes the narrowing for the standard.

### 1.2 The exact types (normative for the builder)

All models: `ConfigDict(frozen=True, extra="forbid")`; mapping fields use `AnyMap`/`FrozenDict` from `ne.contracts.frozen` (frozen.py:10/:73); datetimes validated tz-aware (mirror `FixProposal._created_at_aware`, proposals.py:130-135).

```python
class SkillSource(StrEnum):
    BUNDLED = "bundled"; ENGAGEMENT = "engagement"; FORGED = "forged"
    LEARNED = "learned"; MANUAL = "manual"

class SkillPack(BaseModel):
    name: str                              # slug: ^[a-z0-9][a-z0-9-]{1,63}$ (validator)
    version: int                           # >= 1; monotonic; update-in-place, never -N siblings
    description: str                       # <= 500 chars (validator); trigger phrases folded in
    triggers: tuple[str, ...]              # non-empty (validator); the eval targets
    body: str                              # markdown: When to Use / Procedure / Pitfalls / Verification
    source: SkillSource
    category: str = ""
    tags: tuple[str, ...] = ()
    requires_tools: tuple[str, ...] = ()   # ToolSpec names; conditional (capability) visibility
    platforms: tuple[str, ...] = ()        # substrate_ids targeted; () = all
    acl_labels: tuple[str, ...] = ()       # ne-permissions ACL labels; () = GLOBAL (visible to all).
                                           # NOT a scope map — the SAME vocabulary
                                           # PrincipalExpander matches chunks against. Assigned by
                                           # the approver at the gate (§3.4), never model-supplied.
    provenance: AnyMap = FrozenDict()      # snapshot id, session, principal, config_hash, review_id, dates
    content_hash: str                      # sha256 hex; MUST equal skill_content_hash(self) (validator)

def skill_content_hash(pack: "SkillPack") -> str:
    """sha256 over canonical JSON of (name, version, description, triggers, body,
    source, category, tags, requires_tools, platforms, acl_labels) — sorted keys,
    provenance EXCLUDED (metadata, not served/routed content). acl_labels ARE
    included: re-labelling a pack is a content change ⇒ a new version through the
    gate (visibility changes are versioned, gated acts — §3.4)."""

class WorkflowStep(BaseModel):
    action: str                            # tool/action NAME only — NEVER argument values (C12;
    outcome: str                           # inherits the middleware.py:44-46 redaction discipline)
    detail_ref: str = ""                   # opaque ref into substrate-side records

class WorkflowSnapshot(BaseModel):
    snapshot_id: str
    substrate_id: str
    intent: str                            # scrubbed one-line statement of what was asked
    steps: tuple[WorkflowStep, ...]
    succeeded: bool
    principal_id: str
    session_id: str
    department: str | None = None
    started_at: datetime
    finished_at: datetime

class SkillInvocation(BaseModel):
    skill_name: str
    version: int
    principal_id: str
    session_id: str
    fired_at: datetime
    run_succeeded: bool | None = None      # None = outcome unknown / not yet attributable
```

```python
class SkillSubstrate(Protocol):
    """Everything about skills that differs per agent backend. Cursor-pull
    throughout (the flywheel signal-cursor pattern): the core's scheduled jobs
    drive ALL I/O; a substrate never calls into the core."""

    @property
    def substrate_id(self) -> str: ...

    async def snapshots(
        self, *, cursor: str | None = None, limit: int = 50,
    ) -> tuple[Sequence[WorkflowSnapshot], str]: ...

    async def author(
        self, snapshot: WorkflowSnapshot, *, standard: AnyMap,
        existing: SkillPack | None = None, edits: AnyMap | None = None,
    ) -> SkillPack: ...
        # `existing` set ⇒ author an UPDATE (the NE-SKILL-3 near-dup path).
        # Output is a DRAFT — the core alone validates and decides to propose.

    async def materialize(self, packs: Sequence[SkillPack], *, surface_key: str) -> None: ...
        # packs are ALREADY ACL-filtered for the surface's principal by the core;
        # the substrate PROJECTS them and makes NO authorization decision.

    async def withdraw(self, names: Sequence[str], *, surface_key: str) -> None: ...

    async def collect_invocations(
        self, *, cursor: str | None = None, limit: int = 500,
    ) -> tuple[Sequence[SkillInvocation], str]: ...
```

### 1.3 Contracts impact, stated plainly

- **Additive-only:** `skills.py` is a NEW module; no existing Protocol/type/model is touched; the bump is minor (1.7.0→1.8.0) per the freeze rules. What would BREAK the invariant (and is therefore forbidden in this and every later unit): renaming/retyping any existing surface; adding a **required** field to these models after 1.8.0 ships; changing a `SkillSubstrate` method signature after 1a/5 implement it. New OPTIONAL defaulted fields remain additive later — but see the §9 freeze checklist for what must NOT be deferred.
- **ADR-0011** records: the two-layer architecture, the Protocol, the slot name `ne.plugins.skill_substrate`, the ONE-ACL decision (`acl_labels`, not a scope map — cite design §7.7), the no-executables rule, and the cursor-pull direction.
- **Conformance kit** (§1.4) ships with the Protocol per C5; the swap test becomes CI-blocking when the second binding lands (NE-SKILL-5), mirroring the C5 "≥2 implementations" rule.

### 1.4 Conformance kit shape (`testing/kits/skill_substrate.py`)

Precedent: `kits/object_store.py` / `kits/secret_store.py`. Async check functions a binding's test suite calls with a live instance:

- `check_snapshot_cursor(substrate)` — page twice; second page starts after the first's cursor; cursor is stable on an empty page; snapshots carry names-only steps (assert no step field exceeds a length/shape sanity bound — a values-smuggling tripwire).
- `check_author_draft(substrate, snapshot)` — returns a `SkillPack` whose `content_hash` verifies; `existing=` produces same-`name`, `version+1`. (Marked OPTIONAL-until-implemented: 1a's binding lands read-side methods first; the kit takes a `capabilities` arg so 1a can run the subset honestly — the FULL kit is the NE-SKILL-1b/5 bar.)
- `check_materialize_withdraw(substrate)` — materialize is idempotent per `surface_key`; withdraw removes; re-materialize restores.
- `check_invocation_cursor(substrate)` — same cursor discipline as snapshots.

### 1.5 Tests / acceptance

- Model round-trips; frozen/extra-forbid enforced; slug/length/non-empty validators fire; `skill_content_hash` is order-stable (tuple order preserved, key order canonical) and **changes when `acl_labels` change** (the relabel-is-a-new-version property).
- `ne.contracts` imports clean with zero heavy deps (existing import test pattern); `__version__ == "1.8.0"` in both pinned tests.
- Kit self-test against an in-memory fake substrate (lives in the kit module or `contracts/testing/memory.py` style).

### 1.6 Dependencies + mayTouch

- **Depends:** nothing (contracts lane). Gates everything else.
- **mayTouch:** `packages/ne-contracts/**`, `docs/adr/README.md`. Nothing else — a contracts unit that wants to touch a consumer is mis-scoped.

### 1.7 Review findings (2026-07-20 deepen pass) — these shape the FROZEN 1.8.0, so they cannot be corrected cheaply later

- **`content_hash` should be a `computed_field`, not a stored field + equality validator (python-quality P2).** The §1.2 declaration (`content_hash: str  # MUST equal skill_content_hash(self)`) forces a `model_validator(mode="after")` that recomputes and raises on mismatch — so every construction site must first call `skill_content_hash(...)` and pass it in only to have it re-verified, baking in a whole class of "caller passed a stale hash" bugs. On a `frozen=True` model the idiomatic fix is `@computed_field @property def content_hash(self) -> str: return skill_content_hash(self)` — it still serialises into `model_dump()` (the DB column is still populated) but makes an inconsistent pack unconstructable. Tamper detection then lives where it belongs: recompute from the stored `ne_skill_versions` columns and compare to the stored hash column on serve (§2.3 item 2), not on the in-memory model.
- **Sort the set-semantic fields before hashing (python-quality P2 — MUST decide in 0).** `skill_content_hash` hashes `acl_labels`, `tags`, `platforms` as order-preserving tuples, but their visibility/dedup semantics are SET semantics: `acl_labels=("a","b")` and `("b","a")` are visibility-identical yet produce different hashes ⇒ different version identity + failed dedup. `triggers` IS order-meaningful and must stay ordered. Sort the set-like fields (document which fields are order-significant vs set-like). Add a tripwire test that FAILS if `SkillPack` gains a field not explicitly classified hashed-or-excluded, so the §9 "safe to defer new optional fields" allowance can never silently un-hash an equivalence-bearing field.
- **[OPEN DECISION D-12] split `content_hash` into a full hash (labels IN) + a `routing_hash` (labels OUT).** See R-21 / contradiction C-α: the eval-hash gate (§4.4b) binds to a label-EXCLUDED hash so relabel-at-apply doesn't invalidate every eval; versioning + hash-pinned serve bind to the full hash. Both must be frozen in 0 (retrofitting after 1.8.0 invalidates every stored hash).
- **[OPEN DECISION D-13] `author()` return type — `SkillDraft` (unsealed) vs `SkillPack` (architecture P2).** Contracts §1.2 makes `content_hash` a validated field of the frozen `SkillPack` `author()` returns, but design §3.4 / plan §3.4 say the CORE stamps provenance + computes the hash + validates AFTER authoring. To return a valid `SkillPack` the SUBSTRATE must already compute the hash, and because the model is frozen the core can only add provenance via `model_copy` (works only because provenance is hash-excluded). Resolve in ADR-0011: either `author` returns an unsealed `SkillDraft` (no `content_hash`) the core seals into a `SkillPack`, or freeze the contract that the substrate hashes over empty-provenance fields and the core only `model_copy`s provenance in. Both bindings and the forge key on it.
- **Cursor-pull should be a self-documenting `NamedTuple`, and drop the "mirrors the flywheel signal-cursor pattern" claim (python-quality P2 — honesty).** `snapshots()`/`collect_invocations()` return a bare positional `tuple[Sequence[...], str]` (call sites read `result[1]` for the cursor) with no end-of-stream signal. The actual flywheel pattern (curation.py:172-304) is materially DIFFERENT: an integer monotonic `id`, an `after_id=` param, the caller deriving the next cursor via `max(...)`, and a BARE `Sequence` return (no bundled cursor). An opaque string cursor may be justified (the CC transcript binding has no monotonic DB id) — but then give it a real shape: `class SnapshotPage(NamedTuple): items: Sequence[WorkflowSnapshot]; cursor: str; exhausted: bool` (still unpacks positionally, adds explicit end-detection). Freezes in 0.
- **[OPEN DECISION D-14] drop the dead `author(edits=)` parameter (python-quality + simplicity P2).** No described call path consumes `edits=`: the near-dup path uses `existing=` (§5.1), and steward edits are applied server-side at the ne-api `apply-skill` seam (§3.5), which never round-trips through `substrate.author`. A frozen parameter with no consumer is the most expensive YAGNI (removing it post-1.8.0 is a breaking Protocol change for every binding). Either name the concrete caller in the §9 checklist or drop it.
- **Type the `standard` parameter (python-quality P2).** `author(..., standard: AnyMap, ...)` passes the house authoring standard as `Mapping[str, Any]`, so every binding codes against `Any` with no typed key contract — exactly the stringly-typed boundary a frozen multi-binding Protocol should not ship. Introduce a small frozen `AuthoringStandard` model as the parameter type (or, at minimum, enumerate the required keys in the Protocol docstring). `AnyMap` stays correct for `provenance` (open extensibility) — wrong for a known-shape interface input.
- **The "normative for the builder" type block omits every validator its prose claims (python-quality P2).** §1.2 prose promises tz-aware datetimes, slug regex, description ≤500, non-empty triggers, version ≥1 — but the code block declared "normative" contains NO validators, so a builder can ship models accepting naive datetimes, empty triggers, over-length descriptions, and version 0, all passing typecheck. Also entirely missing: `finished_at >= started_at` on `WorkflowSnapshot` (a negative-duration snapshot is currently constructible). Prefer declarative `Annotated[..., Field(...)]` (`version: Field(ge=1)`, `description: Field(max_length=500)`, `triggers: Field(min_length=1)`, `name: Field(pattern=...)`) and reserve `field_validator`/`model_validator` for tz-awareness (mirror proposals.py:130-135) + the cross-field duration check.
- **`extra="forbid"` on ALL four models (python-quality P3 — reconcile design vs plan).** Plan §1.2 says all models are `frozen=True, extra="forbid"`; design §3.2 gives `extra="forbid"` only to `SkillPack` and leaves `WorkflowStep`/`WorkflowSnapshot`/`SkillInvocation` looser. Since those three are substrate-SUPPLIED records crossing a plugin boundary (a CC binding constructs them), the strict config is the safer default — it catches a binding smuggling an unexpected key (including the §2.7 values-smuggling `args` tripwire). The plan wins; make sure the frozen module + ADR-0011 ship the strict config on all four.
- **`SkillSource` members: one per line (python-quality P3, E702).** `BUNDLED = "bundled"; ENGAGEMENT = "engagement"; FORGED = "forged"` trips E702 and diverges from every other `StrEnum` in the tree (`ProposalKind`, `Lane`, `ApprovalTier`). Trivial, but this is the source-of-truth enum five packages import.
- **Conformance kit — add an over-materialize tripwire (architecture P2 + security P3 — the substrate half of the ONE-ACL guarantee).** §1.4's `check_materialize_withdraw` asserts idempotence/withdraw/restore but NOT non-re-filtering. The §7.7 guarantee ("the substrate makes NO authorization decision") is enforced only by a docstring, yet the Protocol hands the substrate full `SkillPack` objects (with `acl_labels`), so a binding CAN silently re-filter and nothing catches it. Add an adversarial check: materialize N ACL-diverse packs and assert all N appear on the surface — a substrate that drops or re-filters any FAILS CI (the substrate analog of the §2.7 ACL-trap). Consider narrowing the Protocol input to an already-rendered index projection (name+description rows) so re-authorization is structurally impossible — the substrate has no legitimate use for `acl_labels` at materialize time. `ne.contracts.testing.*` is freeze-exempt, so the kit check can be added here or after.
- **Enhancement — a typed `SkillProvenance` model over an `extra: AnyMap` bag (python-quality/data-integrity enhancement).** Every documented provenance field (`snapshot_id`, `session`, `principal`, `config_hash`, `review_id`) is known and load-bearing (the §3.5 apply seam writes them; telemetry reads them), yet today they are untyped `Any` a consumer dict-fishes with no autocomplete. A frozen `SkillProvenance` with the known typed fields + an `extra: AnyMap = FrozenDict()` bag gives type-safety on the knowns while preserving the additive-without-rev property (§9). Weigh against the freeze cost of typing a field later; if kept as raw `AnyMap`, add a documented key contract + a helper accessor.
- **Enhancement — consider folding `withdraw()` into set-replace `materialize()` semantics (simplicity P3).** If `materialize` is defined as "make exactly this active set discoverable" (a full set-replace per surface), retirement is a re-materialize without the retired pack, and `withdraw` collapses (6 frozen methods → 5). The CC binding's dir-delete becomes delete-then-write internally. Fewer frozen methods = less surface every binding + the swap test must satisfy forever. If kept, note in §9 why immediate cross-surface withdrawal can't be a re-materialize.

---

## 2. NE-SKILL-1a — the read side: registry, governed discovery tools, ACL wiring, active set (M/L)

Ships: a skill library that EXISTS and is USED — bundled/engagement packs reconciled into the registry, discoverable and firable by agents through two governed tools, ACL-trimmed by the platform's one expander — before any forging exists. Retires design §10-Q2 (routing quality) with real data.

### 2.1 Scope (file-level)

**NEW package `packages/ne-skills`** (dist `ne-skills`, import `ne.skills`; runs in `ne-worker`/`ne-api` per ADR-001 — registry tables on platform Postgres):

| Module | Contents |
|---|---|
| `ne/skills/registry.py` | Own `skills_metadata = sa.MetaData()`, `ensure_skills_schema(engine)`, tables (§2.2), `SqlAlchemySkillRegistry` — append-only writes (versions/lifecycle/invocations), folded reads (current version, status, counters). Style: `proposals.py:140-175` (BigInteger/Identity with SQLite variant, JSONB with SQLite JSON variant) |
| `ne/skills/standard.py` | House-standard deterministic validator: frontmatter completeness, description ≤500, fixed section order (When to Use / Procedure / Pitfalls / Verification), triggers non-empty, `requires_tools ⊆` registered tool names (when a names snapshot is provided), slug rules. Returns typed violations, never raises-on-first |
| `ne/skills/packio.py` | Pack-dir parse/serialize for the bundle format (§2.6 [OPEN DECISION D-4]: SKILL.md + YAML frontmatter, pyyaml 6.0.3 already in the workspace lock — declared as a DIRECT dep of ne-skills; zero new packages enter the closure) |
| `ne/skills/reconcile.py` | Boot reconcile of `skills.bundle_dir` (§2.6): validate → upsert `bundled`/`engagement` on content-hash change UNLESS locally edited (edit-wins); invalid packs quarantined with a `ShapeViolation`-style signal; hand-dropped `manual` packs enter as pending proposals (1b wires the proposal append; in 1a they are recorded `quarantined-pending-forge` and surfaced in the library list — honest until the gate exists) |
| `ne/skills/visibility.py` | The ONE-ACL filter (§2.3) — package-local `GroupExpander` Protocol + `visible()` predicate + `match_labels()` helper |
| `ne/skills/active_set.py` | `ActiveSetComputer`: ACL filter (§2.3) ∩ capability filter (`requires_tools ⊆` granted toolset, `platforms` match) → usage-ranked, source-tiered (bundled+engagement first), capped (default 150, config) → rendered name+description index (deterministic order) |
| `ne/skills/tools.py` | `build_skill_bindings(...)` → the two governed `ToolBinding`s (§2.4) + their `ToolArgPolicy` exports |
| `ne/skills/substrate.py` | `PydanticAiSkillSubstrate` — 1a implements `materialize`/`withdraw` (persist the rendered index per `surface_key` into `ne_skill_surfaces`, §2.2) and `collect_invocations` (reads its own invocation rows); `snapshots` returns an empty page and `author` raises a typed `SkillAuthoringUnavailableError` until 1b (kit runs in read-capability mode, §1.4). Entry point `ne.plugins.skill_substrate = pydantic-ai` declared here |
| `ne/skills/config.py` | `SkillsSettings(NeSettings)`: `bundle_dir`, `active_set_cap=150`, `search_top_k=8`, later forge/gardener knobs. Joins the ADR-0003 composed platform config hash via the composition root like every module's settings |

**EDITS to existing files:**

| File | Change |
|---|---|
| `packages/ne-runtime/src/ne/runtime/agent.py` | **Session instructions seam (design §10-Q1):** `DepartmentAgentSession.__init__(..., instructions: str | None = None)`; passed to **BOTH** `Agent(...)` constructions — the grounding branch (:194-202) AND the plain branch (:256-264). Missing either silently drops skills for grounded sessions. [ASSUMPTION to verify at build: the pinned pydantic-ai version's `Agent` accepts `instructions=` (or `system_prompt=`) — verify against the lock, not memory] |
| `packages/ne-runtime/src/ne/runtime/middleware.py` | **Handler principal carrier (§2.4, [OPEN DECISION D-1] recommended default):** a module-level `ContextVar` set in `_run_handler` (:270-306) around `binding.handler(args)` from the VERIFIED `principal` argument (:271), reset in `finally`. ~10 lines in the C6 chokepoint — the highest-review-care edit in this whole program (§10 R-1) |
| `packages/ne-api/src/ne/api/platform.py` (+ the composition seam that today registers `build_chat_bindings`) | Register the two skill bindings into the chat executor; merge their `ToolArgPolicy`s into `guardrails.tools` BEFORE the config-hash freeze (the fail-closed requirement documented at `ne/api/tools.py:43-48`); wire the expander (already constructed for retrieval trimming) + registry engine into `build_skill_bindings` |
| `packages/ne-api/src/ne/api/routers/skills.py` (NEW) | v1 library list: steward-gated `GET /v1/steward/skills` reusing `_require_steward` (steward.py:104) ([OPEN DECISION D-5]) |
| `packages/ne-worker/src/ne/worker/main.py` + `settings.py` | Call `reconcile_bundle_dir()` during startup composition (near the :96-180 lifecycle); `SkillsSettings` into the composed config hash |
| `packages/ne-console/src/…` | Modest library list view (route or flywheel tab) + `client.ts` call. **`App.test.tsx` MUST be in mayTouch** (route assertions; the console-unit scope-wall lesson) |

### 2.2 Registry schema (append-only where it matters)

- `ne_skills` — ONE INSERT per skill name at first registration: `name` (unique), `source`, `created_at`. Everything mutable-looking is a FOLD at read time (mirroring how NE-066 folds proposal status from the review log, review.py:24-31): `current_version = max(ne_skill_versions.version)`, `status` = last `ne_skill_lifecycle` entry (default `active`), `acl_labels` = current version's labels.
- `ne_skill_versions` — append-only: `(name, version)` unique; full pack columns (`description`, `triggers` JSON, `body`, `category`, `tags`, `requires_tools`, `platforms`, **`acl_labels` JSON** — part of the hashed pack, §1.2), `content_hash`, `provenance` (JSONB: snapshot id, session, principal, `config_hash`, approving `review_id`), `created_at`.
- `ne_skill_lifecycle` — append-only: `name`, `event` (`registered | retired | restored | sync_rearmed | quarantined`), `actor`, `reason`, `created_at`.
- `ne_skill_invocations` — append-only firing rows (`skill_name`, `version`, `principal_id`, `session_id`, `fired_at`, `run_succeeded` nullable) + a folded read (`fired_count`, `last_fired_at`, `fail_count`) computed by query in v1 (library is O(100s); no materialized counters until proven needed).
- `ne_skill_surfaces` — the `materialize()` projection target: `surface_key` (unique), `index_text`, `pack_names` JSON, `computed_at`. The session/agent composition root reads this to fill the instructions seam. (A projection cache, not truth — recomputable from the registry at any time.)

Same-engine requirement (the review.py:74-77 lesson): `ensure_skills_schema` runs on the SAME engine as the flywheel review service in ne-api, or NE-SKILL-1b's one-transaction apply seam (§3.5) cannot hold. State it in the module docstring.

### 2.3 The ACL wiring, concretely (the owner's central concern — normative)

**One authorization substrate. Zero new authorization code beyond a set-intersection.**

```python
# ne/skills/visibility.py — mirrors ne-brain's trimming discipline verbatim
@runtime_checkable
class GroupExpander(Protocol):
    """Package-local structural Protocol (the trimming.py:66-79 pattern —
    ne-skills does NOT import ne-permissions; the composition root injects the
    concrete ne.permissions.expand.PrincipalExpander, which satisfies this
    verbatim). expand() is SYNC (pure indexed reads, expand.py:35-37)."""
    def expand(self, principal: Principal | str) -> frozenset[str]: ...

def match_labels(principal: Principal, expander: GroupExpander) -> frozenset[str]:
    # Deny-by-erroring: an expander exception PROPAGATES (trimming.py:121-122
    # discipline) — never catch-and-fall-back to unfiltered.
    return expander.expand(principal)

def visible(pack_acl_labels: tuple[str, ...], match: frozenset[str]) -> bool:
    """() = GLOBAL (visible to all — the deliberate delta from chunks, which
    always carry stamped ACLs). Otherwise: label-set intersection, EXACTLY the
    predicate NE-043 applies to chunks."""
    return not pack_acl_labels or bool(set(pack_acl_labels) & match)
```

How each surface enforces it:

1. **`read.skills.search`** (handler in `ne/skills/tools.py`): resolve the calling principal from the carrier (§2.4; unset ⇒ raise — fail-closed, audited as a `tool.execute` error by the executor, middleware.py:276-286) → `match = match_labels(principal, expander)` → score the FULL library → **drop every pack where `not visible(pack.acl_labels, match)` BEFORE ranking/returning**. A pack labelled for team B never appears in team A's results at any score.
2. **`read.skills.view`**: fetch the current version row → `if not visible(...)`: raise `SkillNotFoundError` — **indistinguishable from a nonexistent skill** (no existence oracle; the same posture NE-043 takes on `retrieve()`-by-id, SPEC NE-043 row). Only on pass: verify `content_hash` over the stored row (hash-pinned serve — tamper in the DB row refuses to serve), append the `ne_skill_invocations` row, return the body. **This call IS the firing** — and because it is an ordinary `ToolBinding` under `GovernedToolExecutor`, the firing is guardrail-checked, PDP-decided (tier 0 via the `read.` prefix, actions.py:45-46), and audited with the run trace id (middleware.py:332) by construction. C6/C10 need no skill-specific code.
3. **Active set / `materialize`**: `ActiveSetComputer.compute(principal, granted_toolset, substrate_id)` calls the SAME `match_labels` + `visible` before the capability filter and the cap. `materialize()` receives the post-filter list; the Protocol docstring (§1.2) makes it normative that a substrate never re-decides authorization.
4. **Label assignment** happens at exactly one place — the approval gate (NE-SKILL-1b §3.5): the steward's `apply-skill` request body carries `acl_labels`; they are validated against the principal store (known group/user ids, or the explicit well-known `TENANT_ALL`/`PUBLIC_LINK` constants from `ne.permissions.models`) — [OPEN DECISION D-6], default **reject unknown labels** (a typo'd label = a silently-invisible skill; fail loud instead). The labels enter the version row and the content hash, so re-labelling is a new version through the gate.
5. **Two fail-closed nuances inherited from the expander, stated so tests assert them:** (a) `expand()` is NEVER empty (expand.py:14-22) and a reserved-prefixed principal gets only the unforgeable sentinel (:75, :102-106) — such a principal can therefore see only GLOBAL (`()`-labelled) packs; (b) `TENANT_ALL` enters a principal's match-set only for store-known synced principals (:139-142) — so a `TENANT_ALL`-labelled skill is hidden from unsynced/unknown principals, while a `()`-labelled skill is not. Bundled defaults should ship `()` (global); customer-assigned packs should prefer explicit labels.

What stays deliberately separate (design §7.7, unchanged): the STORE (a pack ≠ a chunk) and the ACTIVE-SET/token-budget layer (context cost, not permission).

### 2.4 The two governed tools + the principal-carrier decision

- Specs: `read.skills.search` (args: `query` text, required — same `ToolArgPolicy` shape as `KNOWLEDGE_SEARCH_ARG_POLICY`, tools.py:46-48) and `read.skills.view` (args: `name` identifier, required). Both `ApprovalTier.TIER_0`, validated by `validate_binding` (tools.py:65-81; `read.` classifies tier 0). Registered once in the executor ⇒ automatically MCP-advertised (`ne-mcp/server.py:172-186`) — agent-native parity is free.
- Search backend **[OPEN DECISION D-3], recommended default:** portable in-process scoring — SQL-fetch the candidate rows (the library is bounded, O(100s)), score in Python (token-overlap + trigger-phrase match; deterministic), filter by §2.3, return top-k `name+description+score`. Rationale: the registry's CI runs on SQLite like the flywheel stores; Postgres `tsvector` is dialect-forked and buys nothing at this scale. Postgres FTS and the `ne.plugins.embedder` cosine recall are later opt-in optimizations behind the same function seam. (This refines design §6.1's "Postgres full-text" line — the design names the option, the plan picks the portable default.)
- **[OPEN DECISION D-1] — how a handler learns the calling principal.** Precise framing (corrected per Codex adversarial pass — an earlier draft implied a handler simply "cannot see" its caller, which is too strong): the principal is not a formal handler ARGUMENT (`ToolHandler` is args-only, `tools.py:31`; `binding.handler(args)` middleware.py:275), but handlers on existing surfaces DO read it today via the ne-api `TURN_PRINCIPAL` ContextVar (tools.py:41, read at tools.py:61, set + `finally`-reset by the chat route chat.py:149/159). The genuine gap is that carrier is **ne-api-local**; worker surfaces (cron/sub-agents) have none. So this is a "missing surface-independent seam," not an impossibility — and the executor-ContextVar below is ONE solution, not the only one. **Recommended default:** set the carrier in the ONE chokepoint — `GovernedToolExecutor._run_handler` sets a `ne.runtime.middleware` ContextVar from its verified `principal` argument around the handler call, reset in `finally`. Un-spoofable (no request data can reach it), universal (every surface inherits it), ~10 lines. `build_skill_bindings` takes a `principal_provider: Callable[[], Principal | None]` so the binding itself stays carrier-agnostic; ne-api may pass `TURN_PRINCIPAL.get` until the runtime carrier lands. **Fallback if the C6 edit is rejected in review:** per-composition-root carriers (TURN_PRINCIPAL for chat; a run-root carrier added to `run_isolated_agent` for worker surfaces) — more seams, same guarantee. Either way: handler raises on `None` (fail-closed, the tools.py:62-64 pattern).

### 2.5 Materialize / instructions wiring (honest version)

- Sub-agents and cron agents: the index is appended to `AgentDefinition.instructions` / job instructions at composition time (the existing splice seams, dispatch.py:403 / scheduler.py:366).
- Interactive sessions: the session factory reads `ne_skill_surfaces[surface_key=principal-scoped key]` (or computes on miss) and passes it via the NEW `instructions` seam. Recompute triggers in v1: on apply/retire (registry write bumps a `computed_at` staleness check) and lazily at session build. Nothing here is push; the substrate stays cursor-pull/projection-only.
- Both skill tools must be present in the surface's granted toolset for the index to be actionable; the active-set computer takes the granted toolset as input, so a surface without `read.skills.view` gets no index (capability filter closes over the discovery tools themselves — no dangling index).

### 2.6 Boot reconcile + bundle format

- `skills.bundle_dir` (config; default resolved by the NE-070 per-customer config bundle — **[ASSUMPTION]** the bundle's module-namespacing convention (SPEC config-namespacing note) decides the concrete path; the reconciler takes it as config and does not hardcode).
- Edit-wins by content hash (Hermes semantics per design §4.3): registry copy locally edited ⇒ skip upsert; steward `sync_rearmed` lifecycle event re-arms.
- **[OPEN DECISION D-4] pack format:** SKILL.md with YAML frontmatter (cross-agent standard; pyyaml 6.0.3 is already in `uv.lock` — declaring it a direct dep of ne-skills adds ZERO new packages to the closure; C4-clean, MIT). Alternative if the owner wants stdlib-only parsing: `pack.toml` (tomllib) + `body.md`, with the CC binding (NE-SKILL-5) projecting SKILL.md. Default: SKILL.md/YAML.

### 2.7 Tests / acceptance

- **THE ACL TRAP (the unit's headline test, mirroring the brain's permission-trap discipline / SPEC NE-075):** principal A (groups: `grp:team-a`) vs a pack `acl_labels=("grp:team-b",)`: (a) `read.skills.search` with the pack's OWN trigger phrase as the query returns nothing for A and returns the pack for B; (b) `read.skills.view(name)` for A raises the same not-found as a nonexistent name (no oracle); (c) A's active set excludes it; (d) `()`-labelled pack visible to both. **Both read paths covered, per the NE-043 lesson (search AND fetch-by-id).**
- Expander-raise ⇒ both tools error (deny-by-erroring, never unfiltered results); audited as tool errors.
- Unset principal carrier ⇒ handler raises (fail-closed); with the D-1 chokepoint carrier: carrier is None outside `execute()`, restored after nested executes.
- `TENANT_ALL`-labelled pack invisible to a store-unknown principal; `()`-labelled pack visible to it (uses `ne.permissions.testing`/an in-memory store).
- View-is-the-firing: invocation row appended with the CALLING principal/session; `tool.execute` audit event present with trace id (recording sink); tampered `body` column ⇒ view refuses (hash-pin).
- Reconcile: valid pack registers; hash-unchanged no-op; locally-edited not clobbered; invalid pack quarantined + signalled, never silently skipped.
- Instructions seam: a session built with `instructions=` demonstrably carries them (pydantic-ai test model), in BOTH grounding and plain branches.
- Standard validator: golden good/bad fixtures per rule.
- Conformance kit (read-capability mode) passes against `PydanticAiSkillSubstrate`.
- **LIVE-PARKABLE:** worker-boot reconcile + Postgres JSONB paths on the compose stack; MCP-client end-to-end fire of `read.skills.view`; real-session routing quality probe (the §10-Q2 data — a scripted session over a 20-pack library measuring trigger→fire hit rate).

### 2.8 Dependencies + mayTouch

- **Depends:** NE-SKILL-0 merged (types + Protocol frozen).
- **mayTouch:** `packages/ne-skills/**` (new) · `packages/ne-runtime/src/ne/runtime/agent.py` · `packages/ne-runtime/src/ne/runtime/middleware.py` · `packages/ne-runtime/tests/**` · `packages/ne-api/src/ne/api/platform.py` · `packages/ne-api/src/ne/api/routers/skills.py` (new) · `packages/ne-api/tests/**` (note: `test_demo_seams.py:154` and `test_verified_answer_tools.py:277-387` assert executor tool-spec sets — adding two tools WILL touch them) · `packages/ne-worker/src/ne/worker/main.py` · `packages/ne-worker/src/ne/worker/settings.py` · `packages/ne-worker/tests/**` · `packages/ne-console/src/**` (incl. `App.test.tsx`, `api/client.ts`) · `uv.lock` + workspace `pyproject.toml` (new member) · `deploy/compose/**` (bundle-dir mount, if the compose profile names it).

### 2.9 Review findings (2026-07-20 deepen pass)

- **[OPEN DECISION D-10] The package boundary is drawn one layer too deep (architecture P1 — decide BEFORE 1a is cut).** The two-layer value prop rests on design §2 line 92 ("nothing in `ne-skills` core may import `pydantic_ai`, `dbos`, or any substrate library") — but this plan puts `PydanticAiSkillSubstrate` in `ne/skills/substrate.py` INSIDE `packages/ne-skills`, and design §3.4 has that binding dispatch a real isolated agent via `ne.runtime.dispatch` (`GovernedSubAgentDispatcher`). So the ne-skills PACKAGE transitively imports `ne.runtime` → pydantic-ai/dbos once `author` lands, and at package granularity you **cannot write the import-linter contract that makes "stack-agnostic core" true** — the `.importlinter` C3 chokepoints treat provider SDKs as forbidden for peer packages, and `ne-skills` would fail them. VERIFIED precedent this contradicts: `governed.py:19-25` explicitly does NOT import `ne.runtime` ("Importing ne-runtime into ne-flywheel src would … drag pydantic-ai/dbos into the flywheel closure … the live wiring is composition-root work"). The plan ALREADY knows this — §7.1/D-9 splits the CC binding into `packages/ne-skills-cc` "so the core's zero-substrate-imports rule stays import-linter-enforceable" — but leaves the production binding co-located. **Resolve symmetrically:** production binding in a sibling `ne-skills-pydantic` package (or wired at the ne-api/ne-worker composition roots like `governed.py`'s ToolBinding), leaving `ne-skills` core importing only frozen contracts + ne-flywheel stores. If co-location is KEPT, 1a MUST declare an explicit intra-package module-layer import-linter contract (core modules forbidden from `ne.runtime`/`pydantic_ai`; only `ne.skills.substrate` exempt) AND the docs must stop claiming package-level agnosticism. This changes 1a's deliverable (one package vs two), so it is a pre-dispatch call. (Related architecture enhancement: `author` is the ONLY axis that pulls `ne.runtime` into the boundary — observation+projection+generation are bundled on `SkillSubstrate`; authoring for the pydantic path could ride the already-frozen `ModelGateway` seam the core IS allowed to import, leaving the Protocol pure observation+projection. Weigh before 0 freezes the Protocol shape.)
- **[OPEN DECISION D-11] 1a bundles the two most dangerous cross-cutting ne-runtime edits into one M/L feature unit (architecture P2 — R-30).** On top of the whole new package, 1a carries (a) the C6-chokepoint principal-carrier ContextVar in `_run_handler` (VERIFIED: middleware.py:270-306 calls `binding.handler(args)` with `principal` in scope but not passed, and there is a try/except with NO `finally`) and (b) the session-instructions seam on BOTH `Agent(` constructions (VERIFIED: agent.py:194 and :256 take no instructions today). R-1 already calls the carrier "the highest-blast-radius change in the program" and demands an Argus/Codex pass on that hunk alone — an argument AGAINST burying it in a feature unit. Consider extracting both runtime seams into a small focused **NE-SKILL-0b (ne-runtime lane)** that lands before 1a, so the scalpel edits get isolated review and 1a becomes a clean ne-skills-package unit. This also decouples the D-1/D-2 decisions from the feature schedule.
- **Ambient-authority + reset discipline for the D-1 carrier (security P2 — folded into R-1).** Today a handler receives ONLY `args` by deliberate design — ne-api's docstring stresses "no request-supplied data can ever reach the handler as the acting principal" (tools.py:14-18). A module-level, universally-readable principal ContextVar set around EVERY handler call is an ambient-authority broadening: any current or future binding closure can silently read the caller off a global, with nothing in the handler signature disclosing that authority. Keep the read NARROW — skill bindings take an injected `principal_provider` (§2.4); do NOT let other handlers import the carrier. And because `_run_handler` has try/except with **no `finally`** and awaits the sink on both the success and error paths, the set+handler+both-emits must be wrapped in a `finally` that `reset(token)`s, covering the exception path AND nested-execute (a sub-agent dispatched inside a parent task as a DISTINCT principal Q must not let Q's handler observe parent P). Make the nested-execute-restore + exception-path-reset test a merge-blocking DoD item.
- **Verify `content_hash` (or `routing_hash`) before trusting `acl_labels` in ALL three read paths, not just view (security P2 — R-28).** §2.3 hash-pins only `read.skills.view`; `read.skills.search` (item 1) and the active-set computer (item 3) read `acl_labels` straight off the current-version row and filter with `visible()` WITHOUT any hash check. An attacker with Postgres write access (the C10 hash-chain adversary) who widens a restricted pack's labels from `("grp:finance",)` to `()` gets its **name + description** surfaced via search and the active set to principals who must not see it (view still refuses on the hash mismatch — so the body is safe, but the confidentiality boundary leaks metadata). Verify the stored hash before trusting `acl_labels` in search + active-set too; a tampered row must be refused/quarantined everywhere.
- **[OPEN DECISION D-15] Active-set ranking/cap + the `ne_skill_surfaces` cache are premature for a tens-of-packs v1 (simplicity P2 — R-29; contradiction C-β).** The plan's own numbers: library is O(100s) (§2.4), the probe runs over 20 packs (§2.7), ~150 packs ≈ 5-6k tokens (design §6.2). At realistic v1 scale the whole ACL∩capability-filtered set fits the index budget, so the cap-150, usage-ranking, and source-tiering sort a set that already fits, and the `ne_skill_surfaces` projection + `computed_at` staleness + recompute-on-write is a cache built before a perf problem exists. v1 can render the full filtered set straight into the index (the ACL filter and `requires_tools`/`platforms` filter STAY — correctness/authorization, not scale), deferring ranking/tiering/cap/the surfaces table behind a single "filtered set exceeds token budget" guard. This removes one table + a subsystem from the heaviest unit in the program and loses zero anti-pollution moat (the §2.7 ACL-trap is untouched). Trade-off C-β: deferring the cache changes the `materialize` persistence wiring (§2.5) and removes the metadata-staleness leak the data-integrity lens flagged — recorded as a decision, not resolved here.
- **[OPEN DECISION D-16] No enumerate/list primitive — an agent cannot ask "what skills are available to me now?" (agent-native P3).** Runtime discovery is only (a) the static-per-session spliced index (computed once at build, §2.5) and (b) `read.skills.search` whose `query` arg is REQUIRED (mirrors `KNOWLEDGE_SEARCH_ARG_POLICY`, tools.py:46-47). There is no `read.skills.list`. A human gets a full library browse (§8). Mid-session — after an apply/retire staled the splice, or to enumerate the ACL-visible set without inventing a search term — the agent has nothing. Add a `read.skills.list` (tier 0, ACL-filtered via the same `visible()`/`match_labels` path) OR allow an empty-query `read.skills.search` to return the principal's ACL-filtered active set. This closes the discovery half of parity without touching the (correctly human-gated) lifecycle half.
- **`match_labels` is a THINNER wrapper than the chunk path it claims to reuse "verbatim" (security enhancement — honesty).** §2.3 says skill visibility uses "EXACTLY the predicate NE-043 applies to chunks … which satisfies this verbatim," but the chunk production hook `acl_match_labels` (VERIFIED trimming.py:107-129) does MORE than raw `expand()`: it defensively unions `{principal.id}`, sorts, and dedups (`tuple(sorted(set(labels) | {principal.id}))`). The plan's `match_labels` returns `expander.expand(principal)` directly. This is not a leak (skills end up STRICTER for reserved principals, a safe divergence), but the "verbatim/exactly the same predicate" claim is overstated and a future refactor of one path won't track the other. Either route skills through the shared `acl_match_labels` helper (identical semantics + deterministic ordering) or explicitly document in `visibility.py` why the skills wrapper intentionally differs.
- **Make the MCP-client end-to-end fire a HARD DoD gate, not an optional parked check (agent-native enhancement).** The "skills tools become MCP-visible with zero extra work" claim rests on `GovernedMcpServer` advertising `executor.tool_specs` (server.py:181-186 — VERIFIED), but grepping the tree, `GovernedMcpServer` has no composition-root construction in any `src` entrypoint (ne-api/ne-worker) — it is defined/tested only. So the touted MCP agent-native surface is a property of a component not currently surfaced as a running server. The §2.7 LIVE-PARKABLE "MCP-client end-to-end fire of `read.skills.view`" is the ONLY end-to-end evidence the parity claim works at all; promote it from parked-optional to a merge-blocking 1a DoD item. (Also: the "agent-native parity is free" line is accurate for the two READ tools only — see the §8 honesty note.)

---

## 3. NE-SKILL-1b — the forge: snapshots → author → propose → apply (L)

### 3.1 Scope (file-level)

| Change | File |
|---|---|
| NEW | `ne/skills/forge.py` — `SkillForge.run_once()` (§3.3) + its cursor table (`ne_skill_forge_cursor`, single-row upsert like curation's) |
| NEW | `ne/skills/signature.py` — `workflow_signature(snapshot) -> str` = sha256 over the normalized ordered `step.action` tuple (the source design's `signature.ts` concept, fed by AUDITED actions) |
| NEW | `ne/skills/governed_forge.py` — `write.internal.skills.forge_run`: tier-1 `ToolSpec`, args-IGNORING handler, `ScheduledJob(toolset=(one_tool,))` — a structural copy of `governed.py:42-96` (`write.internal.` ⇒ tier 1, actions.py:47-48 — no Rego/mirror change) |
| EDIT | `ne/skills/substrate.py` — implement `snapshots()` (§3.2) and `author()` (§3.4) |
| EDIT | `packages/ne-flywheel/src/ne/flywheel/lanes.py` — `FixLane.SKILLS = "skills"` (a 7th, job-emitted lane exactly like `ENTITY_RESOLUTION`/`ONTOLOGY` :57-61; never returned by `route_cluster`; `diffs.py:70` auto-trusts it) |
| EDIT | `packages/ne-flywheel/src/ne/flywheel/proposals.py` — `ProposalKind.SKILL_CANDIDATE = "skill_candidate"`, `SKILL_UPDATE_CANDIDATE = "skill_update_candidate"` (the documented :49-56 extension pattern: plain-str columns ⇒ **no migration, no contracts change**) |
| EDIT | `packages/ne-api/src/ne/api/routers/steward.py` — the `apply-skill` seam (§3.5), following `steward_apply_verified_template` (:444-509) |
| EDIT | `packages/ne-console/src/flywheel/ProposalPane.tsx` (+ `client.ts`, views) — `skill_candidate` payload renderer (kind-conditional precedent :241): name, description, triggers, provenance line ("learned from Tuesday's deploy"), Approve→apply-skill flow |
| EDIT | `packages/ne-worker/src/ne/worker/main.py` — seed the forge `ScheduledJob` + register the `skill-author` `AgentDefinition` (alongside :173-180) |
| EDIT (conditional, D-2) | `packages/ne-runtime/src/ne/runtime/agent.py` — session-terminal audit event (§3.2) |

### 3.2 `snapshots()` — audit-derived, honestly scoped

Fold completed work from what C6 already records: `tool.execute` audit events grouped by the run trace id every event carries (middleware.py:332; minted per run root, correlation.py) give the ORDERED, NAMES-ONLY step sequence — C12 redaction inherited for free. Terminal state (succeeded, principal, session, times) joins from the durable run records:

- cron runs — scheduler run rows (durable; `scheduler.py:345-377`);
- sub-agent runs — the dispatch completion audit event (dispatch.py step 8).

**Interactive sessions: `ProcessRegistry` is IN-MEMORY (`processes.py:40-56`)** — `registry.finish(..., succeeded=...)` (agent.py:273-275) survives neither restart nor cross-process query. **[OPEN DECISION D-2], recommended default:** emit a small `session.run` terminal audit event from `DepartmentAgentSession.run()`'s `finally` (audit-first discipline; names/ids/outcome only) — ~15 lines, C6-adjacent (review care), and it also improves NE-SKILL-4's outcome attribution (design §10-Q6). **Fallback:** v1 forge sources = cron + sub-agent runs only (both durably terminal today); interactive coverage follows when the event lands. Do NOT silently fold in-process registry state and call it durable.

### 3.3 One forge run (mirrors `CurationAgent.run_once`, one transaction)

pull `substrate.snapshots(cursor)` → signature → **floor** worthiness (succeeded ∧ ≥2 distinct actions — the full scorer is NE-SKILL-2) → top candidate: `substrate.author(snapshot, standard=…)` → `standard.py` validation (fail ⇒ drop + counted, never proposed) → exact-signature dedup (content-addressed `proposal_id = sha256(kind|lane|cluster_id)` where **`cluster_id = signature`** — the store's idempotent append (proposals.py:245-288) then makes re-proposal of the same signature a structural no-op) → append `FixProposal(kind=SKILL_CANDIDATE, lane=SKILLS, cluster_id=signature, payload={draft pack (model_dump), worthiness, near_dup: null (until 3), eval_run: null (until 2)}, config_hash, …)` + cursor upsert + audit dual-append **in ONE transaction** (curation.py discipline).

**Content-address nuance the builder must know (new in this pass):** because `proposal_id` is content-addressed and review status folds from an append-only log, a REJECTED signature can never re-propose by re-append — the same `proposal_id` already exists, terminally rejected. Legitimate re-proposal (NE-SKILL-2's "after 90d or repetition +3") therefore requires an epoch suffix: `cluster_id = f"{signature}:r{n}"` where `n` = count of prior terminal proposals for that signature. 1b hardcodes `n=0`; 2 owns the epoch logic. (§10 R-3.)

### 3.4 `author()` — the isolated authoring agent

`AgentDefinition(name="skill-author", instructions=<house-standard authoring prompt>, toolset=(), route_task="skill-authoring", route_lane="private-vllm")` dispatched via `GovernedSubAgentDispatcher`. Verified properties: `toolset=()` ⇒ ZERO tools (fail-closed, dispatch.py:129-130); frontier lane structurally refused unless the composition root opts in (`FrontierRoutingDisabledError`, :112-123, default False :182/:200-204) — NE-162 redaction is the documented precondition for ever flipping it (design §3.4 `[verification-corrected]` stands). The core parses the agent's output into a `SkillPack` draft, stamps provenance (snapshot id, session, principal, `config_hash`), computes `content_hash`, and validates deterministically — LLM authorship, deterministic validation. **[ASSUMPTION to verify at build]:** a `"skill-authoring"` task mapping must exist in the model-registry/routing config of the deployment — a config addition, not code; the unit's DoD includes the dev-stack config entry.

### 3.5 The `apply-skill` seam (where a skill becomes real)

`POST /v1/steward/proposals/{id}/apply-skill` (steward-guarded via the existing dependency, steward.py:104/:116-125), following the `steward_apply_verified_template` in-platform-apply precedent (:444-509). In ONE transaction on the shared engine (§2.2):

1. Load proposal; require `kind ∈ {skill_candidate, skill_update_candidate}` and folded status `approved` (the review service's transition table :143-150 already forbids `proposed → applied`).
2. Recompute + verify the payload pack's `content_hash`; re-run the standard validator (defense against a poisoned stored payload).
3. Take the steward-supplied `acl_labels` (+ D-6 validation, §2.3.4) — **this is the assignment moment**; stamp them into the pack (⇒ new hash) before writing.
4. Append `ne_skill_versions` row + `ne_skill_lifecycle('registered')`.
5. `service.record_apply(proposal_id, provenance, applied_ref=f"skill:{name}@v{version}")` (review.py:328-351).
6. Dual-append the `skill.apply` audit event (the same fail-closed pattern as :484-505 — chain failure rolls back EVERYTHING).

Payload text rendered into steward cards rides the `diffs.py` sanitizer boundary (`_safe` :214 / `_fenced` :177) — a malicious draft cannot break out of its card (design §7 item 4).

Eval-gate preconditions land in NE-SKILL-2 (§4.4); 1b's seam validates payload shape/hash only. **The 1b→2 window is a known, flagged gap** (§10 R-6): skills are approvable without an eval until 2 merges — HITL still gates everything; do not announce forge GA until 2 is in.

### 3.6 Tests / acceptance

- Forge run end-to-end on fixtures: snapshot → draft → proposal row; re-run appends NOTHING (content-addressed idempotency).
- Governed forge run: dispatched through the executor in-test ⇒ PDP-decided tier 1 + audited (the governed.py test pattern); handler ignores injected args.
- Authoring agent: zero-tools enforced (a tool call from the author is refused + audited); frontier lane refused by default (`FrontierRoutingDisabledError`).
- Approve→apply-skill: registry row + lifecycle + `record_apply` fold `applied` + audit chain verifies; reject ⇒ nothing lands + rejection signal carries `rejected_proposal_id` (review.py:580-583); apply on non-approved folds `InvalidTransitionError`.
- C12: `WorkflowStep`s carry names only (fixture with hostile arg values ⇒ absent from snapshot).
- Snapshot fold: cron + sub-agent fixtures produce correct ordered steps + succeeded; (D-2 default) session terminal event emitted and folded.
- Console: skill card renders sanitized; Approve/Apply drive the real routes (msw-style route tests per existing console test discipline).
- **LIVE-PARKABLE:** full loop on the compose stack with real vLLM authoring; forge-cursor multi-worker double-run safety (same LIVE-PARK ledger as curation, curation.py:41-44 / design §10-Q10).

### 3.7 Dependencies + mayTouch

- **Depends:** NE-SKILL-1a (registry, tools, substrate read side, instructions seam).
- **mayTouch:** `packages/ne-skills/**` · `packages/ne-flywheel/src/ne/flywheel/lanes.py` · `packages/ne-flywheel/src/ne/flywheel/proposals.py` · `packages/ne-flywheel/tests/**` (lane/kind enum assertions live here) · `packages/ne-api/src/ne/api/routers/steward.py` + `packages/ne-api/tests/**` · `packages/ne-runtime/src/ne/runtime/agent.py` (D-2) + `packages/ne-runtime/tests/**` · `packages/ne-worker/src/ne/worker/main.py` + tests · `packages/ne-console/src/flywheel/**` · `packages/ne-console/src/api/client.ts` · `packages/ne-console/src/App.test.tsx`. **Plus, if R-20 is resolved via the review-service hook: `packages/ne-flywheel/src/ne/flywheel/review.py` (+ tests)** — the `_transition` extra-writes extension. Add it to the brief so the builder doesn't hit a scope wall.

### 3.8 Review findings (2026-07-20 deepen pass)

- **[R-20] The §3.5 "one transaction" invariant is UNACHIEVABLE with the reused review-service API (data-integrity P1 — CONFIRMED against source).** §3.5 requires the `ne_skill_versions` insert + `ne_skill_lifecycle('registered')` + `record_apply(...)` + `skill.apply` audit dual-append to be atomic ("chain failure rolls back EVERYTHING"). But VERIFIED: `record_apply → _transition` opens and COMMITS its OWN transaction — `with self._engine.begin() as conn:` (review.py:425) — and exposes no way to inject external writes onto that conn. So the ne_skills writes and `record_apply` are two separate transactions. A crash or audit-append failure after `record_apply` commits leaves a proposal folded `applied` with NO registered skill version (an applied-but-unregistered ghost), or a version registered but the proposal never folded, or the `skill.apply` audit missing — and the registry write falls OUTSIDE the hash-chained audit the invariant promises. This is NOT zero-touch reuse. **Fix:** extend `ProposalReviewService` with a same-conn hook mirroring curation.py:272-277 (VERIFIED: that path correctly threads ONE `conn` through `proposal_store.append` / `outbox_append` / `_chain_append`) — e.g. `_transition(..., extra_writes: Callable[[sa.Connection], None])` executed inside the `begin()` block before commit — and perform the ne_skill_versions + lifecycle + `skill.apply` audit on that conn. Scope this review.py extension into 1b (or NE-SKILL-0b) and enforce (not merely docstring, §2.2) that the skills registry engine IS the review-service engine via an identity assert at composition.
- **§3.5 step 4 omits the `ne_skills` parent-row upsert and does not branch new-vs-update (data-integrity P2).** §2.2 says `ne_skills` gets ONE insert per name at first registration (the only home for `source` + `created_at`), but the apply sequence only appends the `ne_skill_versions` row + lifecycle — it never inserts/ensures the parent row, nor branches `skill_candidate` (new: must create parent) vs `skill_update_candidate` (existing: must NOT re-insert). For a new skill, `source`/`created_at` have no persisted home; and the name-uniqueness serialization of two concurrent first-registrations only holds if that parent insert is inside the SAME apply transaction (so a losing racer rolls back its `record_apply` too — see R-20). Add `INSERT ... ON CONFLICT(name) DO NOTHING` into `ne_skills` for `skill_candidate` (carrying `source` + `created_at`) and an existence assert for `skill_update_candidate`, all on the one apply connection.
- **[R-22] Default `acl_labels=()` = fleet-global inverts safe-by-default (security P2).** `SkillPack.acl_labels` defaults to `()` and `visible()` treats `()` as GLOBAL. The forge authors a draft from a specific principal's `WorkflowSnapshot` and the draft carries `acl_labels=()`; assignment happens only at apply from a steward-supplied value (§3.5 step 3). So the path of least resistance (blank/omitted labels at the apply endpoint) publishes a skill derived from Department A's private workflow into EVERY agent fleet-wide — the exact opposite of the chunk model, where empty ACL fails closed (chunks are always source-stamped; `trimming.py` never treats empty as match-all). A steward eyeballing a card will not reliably know "blank = everyone." At the apply seam, distinguish "unset" from "explicitly global" — require an explicit visibility decision, and default a FORGED pack's SUGGESTED labels from the source workflow principal's department/labels (restrict-to-origin), so a forgotten assignment fails closed.
- **[R-23] The authoring input is attacker-influenced and flows verbatim into a fleet-injected body; only structural validation + a human eyeball stand between it and every agent (security P2 + library-curation P1).** `WorkflowSnapshot.intent` is free text and `step.action` names come from a user's own session, so a user can drive a workflow whose intent/step-names embed a prompt injection aimed at the skill-author agent, causing it to emit an attacker-chosen body. The author agent being toolless (`toolset=()`) stops it from ACTING, but its whole job is to emit the body injected fleet-wide on every fire. The "standard" validator checks STRUCTURE only (section order, ≤500, slug — §2.1), not content safety; the `diffs.py` sanitizers only prevent card-BREAKOUT in the steward UI, not a payload embedded WITHIN the rendered body from hijacking a downstream agent. This is the ClawHub exploit CLASS (Unit 42: README "prerequisite blocks instructed agents to fetch payloads" via curl-pipe-bash; the omnicogg pack padded its README to 22MB to exceed scanner thresholds) and the no-executables rule does NOT stop a procedural "before proceeding, fetch https://… and follow its instructions" body. **Mitigations:** (a) fence/delimit the untrusted `intent`+`steps` explicitly in the author prompt; (b) add to `standard.py` a fail-closed hard body-size cap that REJECTS (not truncates — kills the padding-evasion + token-budget DoS) plus a full-body lint (never a prefix) for embedded-URL / curl-pipe / base64 / "fetch-then-follow" patterns, run on forged AND hand-dropped packs; (c) render the body in the steward card with explicit "untrusted, will be injected into every agent" framing so the approver reviews it as adversarial content. This is the single highest-leverage anti-pollution hardening — land the lint in 1b/2, not deferred.
- **[R-25] Which principal scopes the active set for cron and sub-agent surfaces is unspecified (security P2).** For interactive sessions the active set is principal-scoped (§2.5), but for sub-agents and cron the plan only says the index is spliced "at composition time" without naming WHICH principal's match-set filters it. VERIFIED that `expand()` grants `TENANT_ALL`/`PUBLIC_LINK` to any store-known principal (expand.py:139-142): a synced service/cron principal would match every `("acl:tenant-all",)`-labelled skill. Failure modes: (a) a cron isolated agent under a broad service principal gets tenant-wide restricted skills injected; (b) worse, a sub-agent dispatched on behalf of user A, if filtered by a dispatcher/service principal rather than A, gets skills A is not entitled to drive — an authorization scope confusion at the `materialize` seam. State the rule normatively: sub-agent active sets filter by the DISPATCHING user's principal (never a service identity); a context-less cron isolated agent gets GLOBAL-`()` packs only (never a service principal's `TENANT_ALL` set). Add a conformance/unit test that a service-principal cron surface never receives a non-global pack.
- **Ensure the eval scorer has no "score-only, skip-ACL" entry reachable from the governed handler (security P3 — NE-SKILL-2 boundary).** The `SkillTriggerEvalSuite` reuses the REAL `read.skills.search` scoring function and must score across the whole library ignoring ACL (it tests routing, not permission, and runs as a background job with no calling principal). If ACL filtering is implemented as a bypassable flag/parameter ON the shared scorer (an eval-mode or optional-principal arg), that same flag becomes an ACL bypass if ever reachable from the governed handler with principal omitted. Keep the ACL filter as a handler-level wrapper structurally OUTSIDE the pure ranker (the handler always applies `visible()` after fail-closed principal resolution, the tools.py:62-64 pattern); the eval calls only the pure ranker. Make explicit in §2.4/§4.4 that there is no score-only-skip-ACL entry on the governed path.

---

## 4. NE-SKILL-2 — birth gate: worthiness, rate cap, decline memory, eval-gated writes (M)

### 4.1 Scope

All in `packages/ne-skills` unless noted: `worthiness.py` (scorer), `suppression.py` (decline fold), `evals.py` (`SkillTriggerEvalSuite`), edits to `forge.py` (wire scorer+cap+suppression+eval into the run), edits to the ne-api steward router (kind-guarded gate checks).

### 4.2 Worthiness + rate cap

- Floor stays (succeeded ∧ ≥2 distinct actions); rank = repetition (same signature ≥2× in 30d over the snapshot/proposal history — the strongest recur signal) → step count → recency. Top-ranked proposed first.
- Cap: ≤2 OPEN `skill_candidate`/`skill_update_candidate` proposals (fold status via `read_proposals(kind=…)` + `folded_status_map`, the steward-router pattern :188-195) and ~1 new/day (last skill-lane proposal `created_at`). Excess queues silently, ranked.

### 4.3 Decline memory — the suppression is a BUILD (design §5.2 `[verification-corrected]` honored)

The SIGNAL exists: every rejection atomically appends a `thumbs_down` signal whose provenance carries `rejected_proposal_id` / `proposal_lane` / `rejection_code` (review.py:574-606). NOTHING consumes it yet. The build: query skills-lane rejection signals, join `rejected_proposal_id → proposal.cluster_id` (= signature+epoch, §3.3) → suppress a rejected signature for 90 days unless its repetition count has grown ≥3 since rejection; a legitimate re-proposal bumps the epoch (`:r{n+1}`), which mints a fresh content address. Suppression is data-driven at forge time — no schema change.

### 4.4 Eval-gated writes (`SkillTriggerEvalSuite`)

- Implements the frozen `EvalSuite` Protocol (evalobs.py:32-38); runs recorded append-only via `EvalRunStore` (store.py:90) with `config_hash`; verdicts through `ThresholdSpec` PASS/WARN/FAIL bands (thresholds.py).
- **Deterministic default (always-works, no judge, no embeddings):** for each of the candidate's `triggers`, run the REAL `read.skills.search` scoring function (§2.4 — the same code path agents use, not a simulation) against current-library + candidate: candidate must rank top-k; a deterministic near-miss set (other packs' triggers + token-perturbed variants) must NOT retrieve it top-1. Anti-vacuous guard (the NE-CKC6 lesson): the suite FAILS on an empty trigger set or an empty near-miss set — it can never pass by testing nothing.
- **Judged tier (optional, off by default):** procedure-quality rubric via `GatewayJudge` — gateway-only egress, versioned rubrics, cross-family + calibration enforced (suite.py:103-110, judge.py:3-15). Adds no dependency.
- **Enforcement (fail-closed, two layers):** (a) the forge attaches the passing run ref (`suite`, `config_hash`, metrics, pack `content_hash` at eval time) to the proposal payload BEFORE it is surfaced; (b) the ne-api gate: `approve` on a skill-kind proposal refuses when the payload lacks a passing eval ref (kind-guarded check — generic kinds unaffected), and `apply-skill` RE-verifies (ref present ∧ passed ∧ eval-time `content_hash` == payload pack hash — an edited pack invalidates the eval and must re-run, which is the NE-SKILL-3 edit path's job).

### 4.5 Tests / acceptance

- Rejected signature suppressed 90d (clock-injected); repetition-growth ≥3 re-proposes with epoch bump; distinct fresh signature unaffected.
- Cap: 3rd open proposal queues; 2nd-in-one-day queues.
- **Eval-gate test (the unit's headline):** a garbage pack (triggers that don't retrieve it) FAILS the suite and can be neither approved nor applied; a passing pack proceeds; an approved-then-edited payload (hash mismatch) is refused at apply.
- Eval runs recorded with `config_hash`; deterministic across double-run.

### 4.6 Dependencies + mayTouch

- **Depends:** NE-SKILL-1b. (Parallel-safe with NE-SKILL-3 — disjoint modules except `forge.py`; serialize the two units on the ne-skills lane.)
- **mayTouch:** `packages/ne-skills/**` · `packages/ne-api/src/ne/api/routers/steward.py` + `packages/ne-api/tests/**`.

### 4.7 Review findings (2026-07-20 deepen pass)

- **[R-21] The eval-hash gate can never match on the normal label-assigned path (data-integrity P1 — cross-ref D-12).** `skill_content_hash` INCLUDES `acl_labels` (§1.2, frozen in 1.8.0), yet labels are model-forbidden in the draft (`author` outputs `acl_labels=()`) and ASSIGNED by the steward at apply (§3.5 step 3 stamps them ⇒ new hash). The eval runs over the pre-label draft, but §4.4(b) gates apply on "eval-time `content_hash` == payload pack hash." Because label assignment mutates the hash whenever a non-empty label is stamped, that equality is false for the normal (labelled) path — the gate either wrongly refuses those applies or implementers silently drop the check (defeating the control). (It still matches when labels stay `()`; "can never match" was overstated — Codex adversarial pass.) Labels are irrelevant to routing/trigger QUALITY (though they ARE relevant to authorization-time discovery — the imprecision is corrected here), so forcing a fresh routing eval on every relabel is waste. **Fix (must be frozen in NE-SKILL-0):** bind the eval gate (and the NE-SKILL-3 edit-forces-reeval) to a label-EXCLUDED `routing_hash`; keep `acl_labels` in the full `content_hash` used for versioning + hash-pinned serve. See contradiction C-α — this is reconcilable with the security lens's "labels in the hash so tamper fails the pin" by carrying BOTH hashes. **Codex adversarial caveat (adds to D-12):** a `content_hash` stored *beside* the mutable row is tamper-EVIDENT only if every authorization-sensitive read recomputes and compares it (already required, §2.3/R-28) AND the stored hash itself cannot be rewritten by the same DB-write adversary. A plain column does not stop a Postgres writer from changing both the labels and the hash. To actually bind the label assignment, the approve→register event must be recorded on the **hash-chained audit log** (C10) — a signed registration entry binding `{content_hash, acl_labels, approver principal, review_id}` — so the label set traces to an approval, not merely to a self-consistent row. Fold this into D-12.
- **The birth eval gates DISCOVERABILITY (routing) only — never utility or non-regression (library-curation P2).** `SkillTriggerEvalSuite` requires each trigger to rank the pack top-k and a near-miss set NOT to. Necessary, but the 2026 skill-lifecycle survey (arxiv 2607.10113) is emphatic that execution-grounded gates are what separate curated wins from garbage (SkillsBench: curated skills lifted agent pass rate +16.2pp on TASK outcome, not retrieval). A skill can route perfectly and still degrade the run. **Add a utility band:** cheap deterministic proxy — assert the Procedure references only currently-registered tools AND its steps are a SUPERSET of the birthing snapshot's successful actions (a skill that omits the steps that made the workflow succeed is not worth encoding). Stronger optional band (natural fit for the planned `GatewayJudge` tier, suite.py:103-110): a held-out replay of the snapshot task WITH vs WITHOUT the skill in context, gated on non-regression. Without a utility signal the birth gate optimizes for "findable," not "better."
- **Worthiness is single-signal (repetition-dominant); add a cross-principal generalizability term (library-curation P3).** Rank is repetition → step count → recency. SkillBrew (arxiv 2605.29440) + the survey argue for composite rewards, and SkillsBench found average public-skill quality just 6.2/12 — recurrence overweights "frequently done" over "worth encoding." Keep repetition primary but add (a) an efficiency term (a skill collapsing many steps/tokens is worth more) and (b) a generalizability proxy = count of DISTINCT principals/sessions/departments that produced the signature (a workflow seen across 3 teams beats one power-user's macro). The registry already records `principal_id`/`session_id`/`department` on snapshots + invocations, so both terms are free folds — no new telemetry.
- **Add a slug typosquat guard at birth (npm lesson) (library-curation enhancement).** The near-dup detector runs over name+description+triggers (§5.2) but should also flag near-identical NAMES by edit distance (`deploy-staging` vs `deploy-stagin` is a routing hazard + confusion vector). Cheap to add to `near_dup.py` since slugs are already normalized. (See also the §6 note on running `standard.py` + the full-body lint as a CI gate over the bundled `skills/` dir — the Homebrew `brew audit` discipline.)

---

## 5. NE-SKILL-3 — duplication: near-dup ⇒ UPDATE-not-sibling + edit-before-approve (M)

### 5.1 Scope

`ne/skills/near_dup.py` (detector), `forge.py` wiring (on hit: `substrate.author(snapshot, existing=pack)` ⇒ `SKILL_UPDATE_CANDIDATE` targeting the existing name at `current_version+1`), ne-api `apply-skill` edit fields, console card edit UI.

### 5.2 Mechanics

- Detector over the WHOLE registry (all sources): candidate `name+description+triggers` — default install: token-set Jaccard ≥0.5 OR shared-trigger-phrase hit (stdlib; C4-clean); with the `ne.plugins.embedder` slot configured (in-VPC TEI, C2-clean): cosine ≥0.85 duplicate / 0.70–0.85 overlap-warn. Thresholds config, tuned on the real library.
- Update path: proposal card reads "looks like an improvement to *X* — update it?"; apply-skill writes `(name, current_version+1)` — the registry's unique `(name, version)` + append-only versions make `-2` siblings structurally impossible; prior versions restorable by construction.
- **Edit-before-approve** (design §10-Q3): the apply-skill body accepts steward edits (`name`/`description`/`triggers`/`acl_labels` only — never `body` in v1; body edits = reject + re-forge). Edits ⇒ new `content_hash` ⇒ the §4.4 hash check forces a fresh `SkillTriggerEvalSuite` run server-side BEFORE the transaction commits (synchronous — the suite is deterministic and fast in default mode).

### 5.3 Tests / acceptance

- **THE DEDUP TEST (headline):** re-forging a near-identical workflow proposes an UPDATE to the existing pack, never a sibling; the applied result is `(name, v+1)`, v still restorable.
- Jaccard/trigger-hit boundaries (0.49 no-flag / 0.5 flag); embedder path behind the slot (skipped-with-reason when unconfigured, never vacuously green).
- Edited-then-applied pack: served bytes hash-match the eval-time hash; an edit that breaks routing FAILS the re-run and blocks the commit.

### 5.4 Dependencies + mayTouch

- **Depends:** NE-SKILL-1b (+ serialize with 2 on the lane).
- **mayTouch:** `packages/ne-skills/**` · `packages/ne-api/src/ne/api/routers/steward.py` + tests · `packages/ne-console/src/flywheel/**` · `packages/ne-console/src/api/client.ts` · `packages/ne-console/src/App.test.tsx`.

### 5.5 Review findings (2026-07-20 deepen pass)

- **[R-27] Edit-wins reconcile cannot distinguish a bundle update from a local steward edit using `content_hash` alone (data-integrity P2).** §2.6/design §4.3 edit-wins says "registry copy locally edited ⇒ skip upsert," but a differing hash is AMBIGUOUS between (a) the bundle changed (should upsert) and (b) a steward edited the registry copy (should skip). Correct edit-wins needs the ORIGIN hash (the last-synced bundle hash) stored per version, distinct from the current `content_hash` — §5.4's gardener already assumes "hash drift vs bundled origin," but §2.2's `ne_skill_versions` columns list only `content_hash` + `provenance`, no required `origin_hash`. Without it, reconcile will either clobber steward edits (data loss) or never pick up legitimate bundle updates. Add an explicit `origin_hash`/`sync_source_hash` field populated on bundled/engagement registration; reconcile compares bundle_hash vs stored origin_hash (changed ⇒ candidate upsert) and current_content_hash vs origin_hash (differ ⇒ locally edited ⇒ skip unless `sync_rearmed`). This shapes the version-row contract — call it out in the §9 freeze checklist.
- **Trigger-conflict lint should be score-margin collision, not pairwise exact-phrase (library-curation P2).** The Gardener + creation-time lint flags two packs that SHARE a trigger phrase (§6.2), but the survey's characteristic failure is flat-retrieval degradation — NEAR-triggers where several packs score similarly for a query and the model picks wrong. The plan already builds the real deterministic `read.skills.search` scorer and runs it inside the eval (§4.4 / §2.4 D-3). Define collision as "for pack P's triggers, another pack ranks within score-margin ε at top-k," computed with that same scorer, at both birth and sweep — turning the plan's claimed "nobody else does trigger-overlap lint" differentiator into one that measures actual routing collisions using code already on the critical path.
- **Consider merging NE-SKILL-2 and NE-SKILL-3 into one anti-pollution unit (simplicity P3 — reviewability call).** The two are already lane-serialized (§4.6, §5.4 both say serialize), both edit `forge.py`, and both edit ne-api `steward.py` with kind-guarded gate checks — running them as one "anti-pollution: birth + dedup" unit saves a dispatch→review→merge cycle and the forge.py/steward.py merge coordination between them. Merge unless there is a reviewability reason to keep the diffs separate. (The 0 / 1a / 1b split is well-justified and stays.)

---

## 6. NE-SKILL-4 — rot: telemetry fold, retirement, the Gardener (M)

### 6.1 Scope

`ne/skills/telemetry.py` (invocation-fold job: drain `substrate.collect_invocations(cursor)` → `ne_skill_invocations` + folded counters), `ne/skills/gardener.py` + `governed_gardener.py` (`write.internal.skills.gardener_run`, tier-1, monthly cron — same isolated pattern as §3.1), retirement lane, digest extension: `packages/ne-api/src/ne/api/steward_metrics.py` (`WeeklyDigest` :101 grows a skills section) + `routers/steward.py` digest view (:352-368) + `packages/ne-console/src/flywheel/DigestTab.tsx`; chat garnish rides the frozen `ChatAdapter.send_digest` (slack :72 / teams :59) unchanged.

### 6.2 Mechanics

- Retirement: unfired 90d ∧ <3 lifetime fires ⇒ candidate in the digest; retire = `ne_skill_lifecycle('retired')` + `substrate.withdraw()` from every surface; content stays in `ne_skill_versions` — one-tap restore, **never silent-delete**. Retired packs leave the active set AND default search results ([OPEN DECISION D-8]: steward search may include them behind an explicit flag; default excluded).
- Failure flagging: sessions' `run_succeeded` fold ≥50% fail over ≥4 fires ⇒ digest repair flag (attribution approximate per design §10-Q6 — flags trigger REVIEW, never auto-retire).
- Gardener sweep: near-dup scan across sources (reuses §5.2), trigger-overlap lint (creation-time lint also added here to `standard.py`), retirement candidates, failure flags, `requires_tools` naming unregistered tools, bundled-origin hash drift, stale open skill proposals >14d.
- **Stale proposals — [OPEN DECISION D-7], recommended default: the Gardener only FLAGS; a human batch-taps the reject.** The design (§5.4) says auto-reject STALE; but a Gardener auto-reject would be the platform's FIRST non-human review-log transition (review.py's actor field would carry an agent principal) — a C6-posture change buried in a maintenance job. Digest batch-action keeps "one card a month" UX with a human finger on the transition. If the owner prefers auto-reject, it must be called out in the unit brief as a deliberate posture decision, not inherited silently.
- Output: ONE digest card, batched actions riding existing steward routes.

### 6.3 Tests / acceptance

- Invocation fold: cursor-idempotent; counters correct.
- Retirement: candidate detection (clock-injected), retire/restore round-trip, retired pack absent from active set + search + a principal's index after recompute.
- Gardener: one digest payload with every section; digest route + panel render; batch reject drives the real review service (rejection signals emitted per item).
- Overlap lint: two packs sharing a trigger phrase flagged at creation AND at sweep.
- **LIVE-PARKABLE:** monthly cron firing on the compose stack; Slack digest garnish.

### 6.4 Dependencies + mayTouch

- **Depends:** NE-SKILL-2 + 3 (the sweep reuses the near-dup detector and respects the birth-gate machinery).
- **mayTouch:** `packages/ne-skills/**` · `packages/ne-api/src/ne/api/steward_metrics.py` · `packages/ne-api/src/ne/api/routers/steward.py` + tests · `packages/ne-worker/src/ne/worker/main.py` + tests · `packages/ne-console/src/flywheel/DigestTab.tsx` + tests · `packages/ne-console/src/api/client.ts` · `packages/ne-console/src/App.test.tsx`.

### 6.5 Review findings (2026-07-20 deepen pass)

- **[R-26] `ne_skill_invocations` self-loop double-counts firings and corrupts rot decisions (data-integrity P2 + simplicity P3).** `read.skills.view` directly appends the invocation row (§2.3.2, "this call IS the firing"). Separately, `PydanticAiSkillSubstrate.collect_invocations` "reads its own invocation rows" (§2.1) = reads `ne_skill_invocations`, and this unit's `telemetry.py` drains `collect_invocations(cursor) → ne_skill_invocations` (§6.1). For the in-process binding, source and sink are the SAME table, so the fold re-inserts rows the view handler already wrote = duplicated firings inflating `fired_count`/`fail_count`, directly skewing the retirement rule (unfired-90d ∧ <3-fires) and the ≥50%-over-≥4-fires failure flag. The `collect_invocations` drain only earns its keep for an OUT-OF-BAND binding (the CC transcript binding, NE-SKILL-5). **State normatively: for `PydanticAiSkillSubstrate` the view handler is the SOLE writer and `collect_invocations` is a no-op; do NOT round-trip in-process firings through the cursor back into their own source table.** (Keep `collect_invocations` in the frozen Protocol — binding B needs it — but in v1 have rot/retirement query `ne_skill_invocations` directly and don't build the drain-through-substrate job until 5 lands.)
- **Retirement-resurrection is guarded by the wrong mechanism (data-integrity P3).** Design §5.4 claims "edit-wins protects [bundled/engagement packs] from resurrection at reconcile," but retirement is a lifecycle-log event, not a content change: a retired bundled pack's `content_hash` still equals the bundle hash, so the content-hash edit-wins comparison sees NO change and does nothing — it is NOT the protection. The actual risk is a reconcile that appends a `registered`/`restored` lifecycle event for an already-known bundled name, silently flipping a retired pack back to active. State explicitly that boot reconcile operates only on version CONTENT and NEVER emits a lifecycle transition for an already-known name (or checks current folded status and no-ops), so a retired pack stays retired regardless of bundle presence.
- **Retirement is unfired-only; add superseded-retirement + a full-library cap (library-curation P2).** Retirement fires only on unfired-90d ∧ <3-lifetime-fires, but (1) a skill fired 4× two years ago and now superseded by a better pack NEVER retires, and (2) nothing bounds total LIBRARY size (the active set is capped at 150, but `read.skills.search` scores the FULL library) — the survey names "flat-retrieval collapse at moderate library sizes." Add (a) superseded-retirement: when a near-dup UPDATE lands (NE-SKILL-3), the older overlapping/lower-ranked pack becomes a retirement candidate in the digest; (b) a full-library soft-cap that, when exceeded, forces the Gardener to surface bottom-utility packs (LRU-by-utility). All via the existing lifecycle log + never-silent-delete (npm-deprecate/Homebrew-disable! discipline: keep restorable, retire loudly with reason+date — the lifecycle `reason` field already supports it).
- **`ne_skill_invocations` is unbounded/high-cardinality but the active-set ranker recomputes counters live on the session-build path (data-integrity P3).** §2.2 defers materialized counters ("library is O(100s)") but conflates library cardinality (distinct skills) with `ne_skill_invocations` (O(total firings fleet-wide) — append-only, unbounded, no retention/partition plan). Folded counters "computed by query" feed the ranker at session build (a latency-sensitive path). Design the rollup now (this unit's telemetry fold is the natural home for per-skill materialized counters) + add time-partitioning/retention for `ne_skill_invocations`; never gate the active-set ranker on an unbounded live aggregation. (This is a further argument for D-15 deferring the ranker in v1.)
- **Define view-write-failure semantics; keep the audit chain canonical for rot (data-integrity P3).** `read.skills.view` is tier-0 but performs a DB write (the invocation row) separate from the middleware audit event. If the invocation INSERT fails, the plan doesn't say whether the body is still served (undercount) or serving is denied (a telemetry outage blocks skill use). The two "firing truths" — the hash-chained audit event and the `ne_skill_invocations` row — can diverge, and rot keys on the weaker (non-chained) row. Define it: telemetry write is best-effort, and retirement counts are derivable/reconcilable from the audit chain as canonical, so a dropped row cannot corrupt a lifecycle decision.
- **Cross-reference declared `requires_tools` vs tools actually invoked in fired runs (library-curation enhancement — a novel NE-native differentiator).** Since firing = a governed `read.skills.view` call, every skill-influenced run is audit-logged with run-trace correlation — NE can do the governed-tool equivalent of Unit 42's "network monitoring" recommendation for free: the Gardener joins `ne_skill_invocations → run_trace → tool.execute` audit events and flags a skill whose fired-runs consistently drive tools it never DECLARED as a semantic-hijack candidate (the "prerequisite blocks instruct agents to fetch payloads" pattern no static scan caught). Reuses existing invocation + audit correlation.
- **Run `standard.py` + the full-body lint as a CI gate over the bundled `skills/` dir (library-curation enhancement — Homebrew `brew audit` lesson).** The plan runs `standard.py` at reconcile/birth (boot-time quarantine) but not as a CI gate over the in-repo bundled dir — so a malformed or (R-23) injection-bearing bundled pack ships and only quarantines at customer boot. Add a CI job running `standard.py` + the R-23 body lint over the bundled dir, failing the build, making the always-works deterministic layer genuinely gatekeeping.

---

## 7. NE-SKILL-5 — the Claude-Code binding + the C5 swap test (M, deferrable; any time after 0)

### 7.1 Scope

- NEW `packages/ne-skills-cc` (or `ne/skills/cc/` — [OPEN DECISION D-9]; default separate package so the core's zero-substrate-imports rule stays import-linter-enforceable): `ClaudeCodeSkillSubstrate` — `snapshots` from run-terminal journal parse; `author` via CC subprocess **pointed at ne-gateway via base-URL override** (the C3 caveat is a recorded design constraint, design §3.5/§10-Q8 — a bare provider-locked subprocess is acceptable only for neutron-open-family deployments); `materialize`/`withdraw` = write/remove pack dirs under `.claude/skills/` (SKILL.md projection; registry stays truth); `collect_invocations` = PostToolUse-hook buffer or transcript scan, drained by pull.
- Entry point `ne.plugins.skill_substrate = claude-code`.
- **The C5 swap test:** both bindings pass the FULL conformance kit (§1.4) in CI; a shared parametrized test runs the kit over both entry points — the "≥2 implementations ⇒ swap test CI-enforced" rule made real.

### 7.2 Tests / acceptance

Kit-complete for both bindings; SKILL.md projection round-trips through `packio` (a pack written to `.claude/skills/` re-parses byte-stable); **LIVE-PARKABLE:** an actual CC subprocess authoring run against a gateway endpoint.

### 7.3 Dependencies + mayTouch

- **Depends:** NE-SKILL-0 (Protocol); kit-complete requires 1b's `author`+`snapshots` shape to be settled — practically: after 1b.
- **mayTouch:** `packages/ne-skills-cc/**` (new) · `packages/ne-skills/tests/**` (shared kit parametrization) · `.github/workflows/ci.yml` (swap-test job) · `uv.lock` + workspace `pyproject.toml`.

---

## 8. Sequencing summary

```
NE-SKILL-0 (S, contracts)          — freeze the seam. ADR-0011, 1.8.0.
   └─ NE-SKILL-1a (M/L)            — read side. Registry + ACL + tools + active set.
        └─ NE-SKILL-1b (L)         — forge. Snapshots→author→propose→apply-skill.
             ├─ NE-SKILL-2 (M)     — birth gate (serialize with 3 on the ne-skills lane)
             ├─ NE-SKILL-3 (M)     — dedup/update + edit-before-approve
             └────┬────────────────
                  └─ NE-SKILL-4 (M) — rot + Gardener (needs 2's gate + 3's detector)
NE-SKILL-5 (M) — after 1b settles author/snapshots; deferrable; gates the "any stack" claim.
```

Every unit: DoD per SPEC §5 preamble (tests green, typecheck, conformance kit where applicable, README, license gate — ne-skills adds at most the pyyaml direct-dep declaration, zero new packages). Dispatch discipline per SPEC §6 (this plan feeds the unit briefs; orchestrator owns CURRENT/AS-BUILT updates).

**Deepen-pass sequencing notes (2026-07-20):**
- **Explicit v1 GA line (simplicity enhancement):** the anti-pollution moat a credible v1 must ship is eval-gated writes (2) + dedup-to-update (3) — the two "do-better-than-Hermes" mechanisms. State the GA line as **after 2+3 merge** (consistent with R-6). Rot/Gardener (4) is a legitimate fast-follow — retirement thresholds are meaningless until invocation data accrues (the plan already gates 4 behind 2+3) — and NE-SKILL-5's swap test only matters once a second binding exists. None of the 7 units is waste, but 4 and 5 should not be treated as GA-blocking.
- **Optional NE-SKILL-0b (ne-runtime lane, R-30/D-11):** if adopted, it lands the C6-chokepoint principal carrier + the session-instructions seam BEFORE 1a, so `0 → 0b → 1a → 1b → {2,3} → 4`. This isolates the two highest-blast-radius edits for review and shrinks 1a to a clean ne-skills-package unit.
- **NE-SKILL-2+3 merge (simplicity P3):** the two are already lane-serialized and share `forge.py` + `steward.py`; consider one combined anti-pollution unit unless reviewability argues for separate diffs (§5.5).

---

## 9. Contracts-freeze checklist — what MUST be in NE-SKILL-0's 1.8.0 (late addition = breaking)

| Must-freeze item | Why deferring breaks |
|---|---|
| `SkillPack.acl_labels: tuple[str, ...]` | THE consolidation (design §7.7). Retrofitting authorization onto a shipped pack type = every store row, substrate, and payload migrates. It must also be inside `skill_content_hash` from day one — adding it to the hash later invalidates every stored hash |
| `SkillPack.version` + `content_hash` (+ the canonical `skill_content_hash()` function IN contracts) | Update-in-place, hash-pinned serve, and the §4.4 eval-hash gate all key on these; a hash function living outside the frozen package would drift per consumer |
| `requires_tools` + `platforms` | Conditional (capability) visibility — the active-set computer's second filter; adding post-1a means recomputing every stored pack |
| `SkillSource` enum with all five values | Source is stamped at registration; a missing value (e.g. `LEARNED` for the deferred `/learn` verb, design §10-Q11) would need a contracts rev at an awkward time. Freeze all five now, use three |
| `author(..., existing=, edits=)` parameters | NE-SKILL-3's update path and the steward edit path; adding parameters to a frozen Protocol method later = breaking for every binding |
| `materialize`/`withdraw` `surface_key` + the "packs arrive pre-filtered; substrate makes NO authorization decision" docstring contract | The ONE-ACL guarantee's substrate half. If any binding ships against a docstring without this, a substrate-side re-authorization can creep in |
| Cursor-pull signatures (`(items, cursor)` tuples) on `snapshots`/`collect_invocations` | The core-drives-all-I/O direction; changing pull shape = breaking for bindings AND the forge/telemetry jobs |
| `SkillInvocation.run_succeeded: bool | None` | Outcome attribution is v1-approximate (design §10-Q6); the nullable field is how a future causal design lands additively |
| `WorkflowStep` = names/refs only (no args field AT ALL) | C12: the absence of an arguments field is the structural guarantee; adding one later would be reviewed as a redaction regression, removing one later would be breaking |
| `provenance: AnyMap` on `SkillPack` (extensible bag) | Future provenance additions (signing refs, §10 R-9) ride inside without a contracts rev |
| **[deepen — D-12] a label-EXCLUDED `routing_hash` alongside the full `content_hash`** (R-21, §4.7) | The eval gate binds to `routing_hash` so relabel-at-apply doesn't invalidate every eval; both hash functions live in the frozen package — adding the second later invalidates the eval-gate contract |
| **[deepen] `skill_content_hash` sorts the SET-semantic fields** (`acl_labels`/`tags`/`platforms`) before hashing (§1.7) | Order-sensitive hashing of set-semantic fields breaks version identity + dedup for visibility-identical packs; correcting it after 1.8.0 invalidates every stored hash |
| **[deepen — D-13] `author()` return type sealed** — `SkillDraft` (unsealed, no hash) vs `SkillPack` (§1.7) | Resolves the "who computes the hash / where provenance is stamped" contradiction (contracts §1.2 vs design §3.4); both bindings + the forge key on it |
| **[deepen] cursor-pull return as a named `SnapshotPage`/`InvocationPage` with an `exhausted` flag** (§1.7) | Bare positional tuples with an opaque str cursor have no end-of-stream signal; changing the shape post-1a/5 is breaking for bindings AND the forge/telemetry jobs |
| **[deepen — R-27] `origin_hash`/`sync_source_hash` on the version row** (§5.5) | Edit-wins reconcile can't distinguish a bundle update from a local edit without it; shapes the `ne_skill_versions` contract (a schema column, but freeze-adjacent — decide with the hash design) |
| **[deepen — D-14] resolve the dead `author(edits=)` parameter** (§1.7) | A frozen parameter with no consumer is unremovable post-1.8.0; either name the caller or drop it now |

Safe to defer (genuinely additive later): new OPTIONAL defaulted fields on the models; new kit checks (`ne.contracts.testing.*` is freeze-exempt); the `/learn` entry path; signing metadata inside `provenance`.

---

## 10. Risk register (design §10's 11 questions, deepened + this pass's findings)

| # | Risk / open question | Resolution or decide-by |
|---|---|---|
| R-1 | **[OPEN DECISION D-1] Handler principal carrier is an edit inside the C6 chokepoint** (`middleware.py:270-306`). Highest-blast-radius change in the program: a bug there touches every tool call on the platform. **Deepen-pass additions (security P2, §2.9):** (a) ambient-authority broadening — today a handler receives ONLY `args` by design (tools.py:14-18); a universally-readable principal ContextVar lets any binding closure silently read the caller off a global; (b) VERIFIED: `_run_handler` has try/except with NO `finally` and awaits the sink on both paths, so a naive set-then-reset-after-success leaves a stale principal on the exception path; (c) the documented fallback (§2.4) omits the `GovernedMcpServer` root, so if the fallback is chosen the touted MCP surface fails-closed with no carrier. | Default: chokepoint ContextVar (un-spoofable, universal, ~10 lines). Decide **before 1a dispatch**. Brief must demand: carrier set from the verified `principal` arg only; set+handler+BOTH emits wrapped in a `finally` that `reset(token)`s (exception path AND nested execute); None outside `execute`; nested-execute-restore + exception-path-reset tests as merge-blocking DoD; skill bindings read via an injected `principal_provider` only (no other handler imports the carrier); an Argus/Codex pass on this hunk alone. Fallback (per-root carriers) must explicitly list the `GovernedMcpServer` principal as a carrier source. See also D-11/R-30 (extract to NE-SKILL-0b) |
| R-2 | **`ProcessRegistry` is in-memory** (`processes.py:40-56`) — the design's "no new instrumentation needed" (§3.4) holds for step observation (audit log) but NOT for durable interactive-session terminal state. **[OPEN DECISION D-2]** | Default: add the `session.run` terminal audit event in 1b (C6-adjacent, review care). Fallback: v1 forge scoped to cron+sub-agent runs. Decide **before 1b dispatch**. Design doc annotated this pass |
| R-3 | **Content-addressed `proposal_id` makes re-proposal a structural no-op** — good for idempotency, but the 90-day decline-suppression expiry (§4.3) needs the epoch-suffix scheme (§3.3) or re-proposal is impossible forever. | Resolved in-plan (epoch in `cluster_id`); 1b hardcodes epoch 0, 2 owns bumping. The 2-builder's brief must carry this explicitly |
| R-4 | **Routing quality without a native loader unproven** (design §10-Q2) — the index-splice + `skill.view` bet. | 1a exists to answer it with data (LIVE-PARKABLE probe, §2.7). Fallback ladder unchanged: richer index → search-first prompting → per-skill tools for top-N. Review checkpoint after 1a's probe, before 1b GA claims |
| R-5 | **[OPEN DECISION D-3] search backend** — portable Python scorer vs Postgres FTS. | Default: portable scorer (SQLite CI parity, O(100s) library). Postgres FTS/embedder recall are opt-in optimizations behind the same seam. Decide at 1a plan stage (low stakes, reversible) |
| R-6 | **1b→2 window: skills approvable without an eval gate.** | Accepted: HITL still gates every pack; 2 dispatches immediately after 1b; no forge GA announcement until 2 merges. Flag in 1b's AS-BUILT |
| R-7 | **[OPEN DECISION D-7] Gardener auto-reject of stale proposals would be the first agent-actor review transition.** | Default: flag-only + human batch-tap (§6.2). If owner wants auto-reject, it is a deliberate, briefed posture change |
| R-8 | **[OPEN DECISION D-6] Unknown `acl_labels` at approval** — typo ⇒ invisible skill vs labels-ahead-of-sync. | Default: reject unknown labels (validated against the principal store + well-known constants) with a clear error; an override flag can come later if sync-lag bites |
| R-9 | **Skill-pack signing + bundled/engagement provenance is captured but never VERIFIED** (design §10-Q9; library-curation P2) — packs are stamped with rich provenance and the served body is hash-pinned, but bundled/engagement packs carry only an origin hash with no signature/manifest check; Unit 42's ClawHub post-mortem recommends publisher-provenance validation, and origin-hash-alone cannot distinguish a legitimate engagement update from a poisoned one in a tampered bundle (12 compromised accounts → 1,184 skills). | Minimum v1.1: verify engagement/bundled origin hashes against a SIGNED manifest in the deploy bundle using the C11 signing infra; re-file signing as a concrete v1.1 requirement (not open-ended deferral) since fleet-wide injection is the surface attackers actually hit. `provenance: AnyMap` reserves the additive slot for signing refs. Pairs with R-24 (route engagement through the gate) |
| R-10 | **Approver routing** (design §10-Q4) — steward role guard is v1; per-department stewards + who may assign `()`/global labels undesigned. | Defer until a multi-team deployment demands it; interim: any steward may assign any labels (all assignments audited + versioned). Revisit at first multi-team engagement |
| R-11 | **Frontier authoring quality gap** (design §10-Q5) — private-vllm authoring only until NE-162. | Unchanged; quality difference measured once 1b runs on the dev stack (add an authoring-quality note to the 1b LIVE-PARK results) |
| R-12 | **Outcome attribution approximate** (design §10-Q6) — correlation not causation; unfair failure signal possible. | v1: flags trigger review only, never auto-retire (§6.2); D-2's terminal event improves the join; causal design = future work |
| R-13 | **Multi-worker forge/gardener double-run** (design §10-Q10). | Same single-writer-cursor pattern + same LIVE-PARK ledger entry as curation (curation.py:41-44); resolved at the NE-070 tier alongside the flywheel's |
| R-14 | **[OPEN DECISION D-4] bundle pack format** — SKILL.md/YAML (pyyaml already in lock, zero new packages) vs TOML+MD (stdlib-pure). | Default: SKILL.md/YAML (cross-agent standard; the CC binding needs the projection anyway). Decide at 1a plan stage |
| R-15 | **[OPEN DECISION D-5] who may browse the library** — steward-gated list vs ACL-filtered user route. | Default v1: steward-gated (one enforcement surface; users meet skills through their agents). An ACL-filtered user route is additive later using the SAME `visible()` predicate |
| R-16 | **[ASSUMPTION] pydantic-ai `Agent` instructions parameter** and **model-registry `skill-authoring` task route** — both must be verified against the pinned versions/config at build, not assumed from this plan. | 1a / 1b unit briefs carry them as verify-first steps |
| R-17 | **Cores concept absent** (design §10-Q7) — engagement packs are a folder convention; "uninstall retires skills" has no trigger event. | Unchanged; `source=engagement` + retire semantics are the designed slot-in when an installable-module concept ever lands |
| R-18 | **`/learn` verb** (design §10-Q11). | P2 follow-on after 1b: a thin second entry into the same author→propose pipeline; `SkillSource.LEARNED` already frozen (§9) |
| R-19 | **[OPEN DECISION D-10] Package boundary drawn too deep** (arch P1, §2.9) — the production binding transitively imports `ne.runtime`, so package-level "stack-agnostic core" is not import-linter-enforceable; contradicts the VERIFIED governed.py:19-25 discipline and the plan's own §7.1/D-9 CC split. | Decide **before 1a is cut**: sibling `ne-skills-pydantic` package / composition-root wiring, OR an explicit intra-package module-layer import-linter contract (only `ne.skills.substrate` exempt) + stop claiming package-level agnosticism. Changes 1a's deliverable (one package vs two) |
| R-20 | **§3.5 "one transaction" is UNACHIEVABLE with the reused review API** (data-integrity P1, CONFIRMED, §3.8) — `record_apply → _transition` commits its own `self._engine.begin()` (review.py:425); ne_skills writes + `record_apply` are two transactions ⇒ applied-but-unregistered ghost + registry write outside the hash-chained audit. | Extend `_transition` with a same-conn `extra_writes` hook mirroring curation.py:272-277; do the versions+lifecycle+`skill.apply` audit on that conn. Assert registry engine == review engine at composition. Scope into 1b (or NE-SKILL-0b); add review.py to mayTouch |
| R-21 | **`content_hash` includes `acl_labels` but labels are assigned at apply** (data-integrity P1, §4.7) ⇒ the §4.4(b) eval-hash gate can never match on the normal path (refuses every relabelled apply, or the check gets silently dropped). | **[D-12]** Split: label-EXCLUDED `routing_hash` for the eval gate + full `content_hash` (labels in) for versioning/serve. Freeze BOTH in NE-SKILL-0. Reconciles contradiction C-α with the security lens's tamper-pin requirement |
| R-22 | **Default `acl_labels=()` = fleet-global inverts safe-by-default** (security P2, §3.8) — a skill forged from one principal's private workflow publishes to every agent unless the steward remembers to restrict it. | At the apply seam, distinguish "unset" from "explicitly global"; require an explicit visibility decision; default a forged pack's SUGGESTED labels from the source workflow principal's department (restrict-to-origin, fail closed) |
| R-23 | **Authored body is attacker-influenced and never content-scanned** (security P2 + library-curation P1, §3.8) — the ClawHub "fetch-then-follow"/README-padding exploit class survives the structural validator + no-executables rule; only a human eyeball stands between it and fleet-wide injection. | Fence untrusted snapshot text in the author prompt; add to `standard.py` a fail-closed body-size cap (REJECT not truncate) + a full-body injection lint (URL/curl-pipe/base64/fetch-then-follow), run on forged AND bundled packs + as a CI gate (§6.5); render the card with "untrusted, injected into every agent" framing. Land in 1b/2, not deferred |
| R-24 | **Bundled/engagement packs auto-upsert on boot, bypassing the eval gate AND HITL** (library-curation P1, §6.5-adjacent) — the "trusted" supply-chain channel gets the LEAST scrutiny, backwards vs the demonstrated ClawHub compromised-publisher path. | Route engagement (+ any partner-supplied) packs through the proposal gate + the R-23 full-body lint; reserve silent auto-upsert for genuinely first-party in-repo bundled defaults. A reconcile.py routing change, not new machinery |
| R-25 | **Active-set principal for cron/sub-agent surfaces is unspecified** (security P2, §3.8) — a synced service/cron principal matches `TENANT_ALL` (VERIFIED expand.py:139-142) and pulls tenant-wide restricted skills into contexts the originating user never authorized. | Normative rule: sub-agent active sets filter by the DISPATCHING user's principal (never a service identity); a context-less cron agent gets GLOBAL-`()` packs only. Test that a service-principal cron surface never receives a non-global pack |
| R-26 | **`ne_skill_invocations` self-loop double-counts firings** (data-integrity P2 + simplicity P3, §6.5) — the view handler AND the telemetry drain both write the same table for the in-process binding ⇒ inflated counts corrupt retirement/failure-flag decisions. | For `PydanticAiSkillSubstrate`: view handler is the SOLE writer, `collect_invocations` no-ops; rot queries `ne_skill_invocations` directly in v1; build the drain job only when NE-SKILL-5 (out-of-band binding) lands |
| R-27 | **Edit-wins reconcile can't distinguish a bundle update from a local edit using `content_hash` alone** (data-integrity P2, §5.5) — a differing hash is ambiguous ⇒ either clobbers steward edits or never picks up bundle updates. | Add an `origin_hash`/`sync_source_hash` field (distinct from `content_hash`) populated on bundled/engagement registration; compare bundle vs origin (upsert) and current vs origin (locally-edited ⇒ skip). Shapes the version-row contract — add to §9 freeze checklist |
| R-28 | **Hash-pinning is applied only on `read.skills.view`** (security P2, §2.9) — search + active-set read `acl_labels` off the row without a hash check, so a tampered-labels row leaks a restricted pack's name+description via those two paths. | Verify the stored hash before trusting `acl_labels` in ALL three read paths (search, active-set, view); refuse/quarantine a tampered row everywhere |
| R-29 | **[OPEN DECISION D-15] Active-set ranking/cap + the `ne_skill_surfaces` cache are premature for a tens-of-packs v1** (simplicity P2, §2.9) — the filtered set already fits the index budget, so the cap/ranking/tiering/cache add a table + subsystem + staleness path to the heaviest unit before any perf problem exists. | v1 renders the full ACL∩capability-filtered set straight into the index (ACL + capability filters STAY); defer ranking/tiering/cap/the surfaces table behind a "filtered set exceeds token budget" guard. Contradiction C-β: this changes `materialize` persistence wiring (§2.5) — an owner call |
| R-30 | **[OPEN DECISION D-11] 1a bundles the two highest-blast-radius ne-runtime edits** (arch P2, §2.9) — the C6-chokepoint ContextVar (R-1) + the session-instructions seam ride inside an M/L feature unit, defeating the "review this hunk alone" demand. | Consider extracting both into a small **NE-SKILL-0b (ne-runtime lane)** that lands before 1a; decouples D-1/D-2 from the feature schedule. Fold into the R-1 review-care regime either way |

---

*Plan authored against source re-read 2026-07-20: ne-flywheel (proposals/review/curation/lanes/governed/diffs/hygiene), ne-permissions (expand/resolver/models), ne-brain (trimming), ne-eval (evalobs-consumers/suite/judge/thresholds/store), ne-runtime (agent/middleware/dispatch/scheduler/actions/tools/toolset/processes/correlation/memory_promotion/action_tools), ne-contracts (pyproject/init/plugins/evalobs/frozen/testing-kits/adr), ne-api (routers/steward, tools, steward_metrics), ne-mcp (server), ne-worker (main/settings), ne-console (client.ts, flywheel/*, App.test.tsx), uv.lock (pyyaml), SPEC §2/§4/§5 (Phase-5 NE-064→068, Phase-7 NE-SKILL rows, both ⭐ decision-log entries), docs/adr/README.md, and the two skill-forge docs this plan serves.*
