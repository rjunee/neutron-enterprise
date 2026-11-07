# Skill Forge on Neutron Enterprise — the stack-agnostic port (NE-SKILL-*)

**Date:** 2026-07-20 · **Status:** DESIGN PROPOSAL — for owner review, NOT settled; gates NE-SKILL-1 per SPEC §5 Phase-7 preamble
**Source design:** the neutron-open skill-ecosystem design of 2026-07-20 (imported, neutron-open-bound — this doc is the port it flags as "tracked separately")
**Owner directive (2026-07-20):** the source design *"needs to be re-written to work on our pydantic stack (ideally modular so works with any stack we support)."*

Every module claim below is grounded in source read on 2026-07-20 (paths cited inline). Where the port is genuinely uncertain, it is flagged in §10 rather than guessed at.

**Cross-model verification pass (Codex, 2026-07-20):** the load-bearing reuse claims were checked against the code. Confirmed TRUE: migration-free `ProposalKind`/`FixLane` string columns (the NE-066 reuse), `tier_for_action("read.*") ⇒ tier 0` + the `GovernedToolExecutor` audit seam, the missing `DepartmentAgentSession` instructions seam (§10-Q1), and the `FrontierRoutingDisabledError` default. Two overstatements were found and corrected in place (both marked `[verification-corrected]`): the decline-memory reuse (§5.2 — the signal exists, the suppression is a build) and the frontier-routing gate (§3.4 — default-off + opt-in, not hard-enforced-until-redaction). One precision nit stands as a caveat: `run_trace_id` correlation holds for calls inside a supported run root, not literally every call.

**Revision — ONE ACL for the whole brain (owner-directed 2026-07-20, §7.7):** an earlier draft gave skills a bespoke `scope: {departments, roles}` field. Owner flagged it as a duplicate authorization path / maintenance hazard. Verified against the code that `ne-permissions.PrincipalExpander` is item-agnostic (chunks are merely its current consumer) and consolidated: skill visibility now rides the **same** ACL-label + expander + PDP + audit spine as knowledge, with pack `acl_labels` assigned at approval (the only delta from chunks — assigned vs source-derived). The store and the token-budget active-set layer stay separate because those concerns genuinely differ. Terminology also corrected: **"the brain" is the product umbrella** (knowledge + skills + policy under one governance spine), not the `ne-brain` package alone.

---

## 0. TL;DR

The source design's substance survives intact: the four-sources model, the propose→approve locked gate, the three-point anti-pollution machinery (birth / duplication / rot), the Skill Gardener, eval-gated writes as the do-better-than-Hermes move, the bounded active set + full-library search, and the dead-simple UX. What changes is the substrate it is bolted to:

| | neutron-open (source) | neutron-enterprise (this port) |
|---|---|---|
| Language / runtime | TypeScript, Claude Code REPL subprocess | Python 3.12, Pydantic-AI + DBOS (`ne-runtime`) |
| Propose→approve spine | bespoke (`forge.ts` + SQLite proposals table) | **reused**: `ne-flywheel` `FixProposal` store + `ProposalReviewService` (NE-065/066) |
| Registry | SQLite, file-is-truth, single owner home dir | Postgres, registry-is-truth for agent-authored packs, disk reconciled on boot (§4) |
| Skill discovery | Claude Code's native `.claude/skills/` loader | **the biggest porting gap** — active-set instruction splice + governed `read.skills.search`/`read.skills.view` progressive disclosure (§6) |
| Invocation telemetry | PostToolUse hook on the Skill tool | the governed `read.skills.view` tool handler itself (§6.3) |
| Governance of a skill firing | none (native loader is invisible to governance) | **C6 by construction**: firing = a governed tool call → guardrails → PDP → audit (§7) |
| Backend coupling | hardwired to Claude Code | `SkillSubstrate` Protocol in `ne-contracts` with ≥2 bindings (§3) |

Two explicit layers: a **stack-agnostic core** (new `packages/ne-skills`, the large majority) and a **per-substrate adapter** (a small frozen Protocol). Adding a Protocol to `ne-contracts` (currently **1.7.0**, frozen, additive-only) requires an ADR + version bump — this design says so plainly and budgets a unit for it (§9).

**Terminology — "the brain" is the product umbrella, not one package.** A customer (and our marketing — "the flywheel updates the brain") reasons about the brain as the *whole governed substrate* that makes the deployment smart and gets smarter: knowledge, skills, the risk-tier/policy rules, provenance, and the flywheel loop that curates all of it. That substrate is *physically* several stores (`ne-brain` for knowledge chunks, `ne-skills` for skill packs, `ne-govern` for policy) — a fine implementation detail — **provided they share ONE governance spine**: one PDP, one audit chain, and one ACL model (§7.7). This document uses "the brain" for the umbrella concept and names the specific package (`ne-brain`) when it means only the knowledge layer. The whole point of the port is that skills join the brain *through the shared spine*, not as a parallel system beside it.

---

## 1. What is preserved from the source design (the research is not lost)

Verbatim-in-substance carries, each re-homed in the sections noted:

1. **Four sources, one registry** (source §3.1): bundled defaults, engagement/Core packs, forged, manual+`/learn` — §4.2. (The "Cores" source has no neutron-enterprise analog; nearest mapping flagged in §10-Q7.)
2. **Propose-then-approve stays the hard, locked gate** — nothing reaches the library silently. Re-homed onto NE-066's `ProposalReviewService` (§5), which is *stronger* than the source's bespoke gate: append-only review log, `SELECT FOR UPDATE` race-serialization, dual-appended hash-chained audit, rejection→correction-signal feedback (`packages/ne-flywheel/src/ne/flywheel/review.py`).
3. **Anti-pollution at three entry points** (source §3.2): birth (worthiness scoring, rate cap ≤2 open / ~1 per day, decline suppression 90d, eval-gated writes), duplication (near-dup → propose UPDATE not sibling; Jaccard ≥0.5 default, cosine 0.85 / 0.70–0.85 with embeddings; update-in-place with version bump, never `-2` siblings), rot (fired/last-fired telemetry, 90-day + <3-lifetime-fires retirement lane, never silent-delete, failure flagging ≥50% over ≥4 fires) — §5.2–§5.4.
4. **The Skill Gardener**: monthly sweep → ONE digest card with batch one-tap actions — §5.4, built as a cron-as-isolated-agent exactly like the flywheel curation job.
5. **Hermes copy list** (source §2.2): write-approval staging (≈ our gate), `/learn` as a lightweight capture verb (deferred, §9), conditional visibility via `requires_tools`/`platforms` (§6.2 — maps beautifully: the registry can check `requires_tools` against the deployment's actual registered `ToolSpec` names), content-hash edit-wins sync for bundled packs (§4.3), scan-at-install trust tiers (deferred until third-party skills are consumed).
6. **Hermes do-better list** — all four kept and now structurally cheap here: eval-gated lifecycle (`ne-eval`, §5.2), routing beyond a flat list (active set + search, §6), usage-driven lifecycle (§5.4), provenance (every forged skill stamps workflow/session/config_hash, §4.1).
7. **Dead-simple UX** (source §3.5): non-blocking proposal cards, monthly digest, invisible-by-default firing with an attribution line — §8. The non-blocking property (#382 in the source) is free here: the forge is an async scheduled job, structurally incapable of gating task work.
8. **House authoring standard** (source §3.1): frontmatter `name` / `description` (triggers folded, ≤500 chars) / `version` / `source` / `metadata.neutron.{category, tags, requires_tools, platforms, provenance}`; fixed body sections *When to Use / Procedure / Pitfalls / Verification*; LLM authorship + deterministic validation — §4.1/§5.1.

One deliberate v1 narrowing versus the source ecosystem: **enterprise skill packs are procedural/knowledge artifacts only — no executable scripts.** A skill may reference existing governed tools by name in its Procedure; it may never carry a new executable. Executable artifacts would be a new handler surface outside the `GovernedToolExecutor` C6 chokepoint (`packages/ne-runtime/src/ne/runtime/middleware.py:3-17` — the ONLY structural path from a tool name to execution) and are deferred until they can ride the full `ActionDefinition` registration discipline (`ne/runtime/action_tools.py`). Skills change agent behavior via *context*, never via new code paths. This is a constraint the source design did not need (Claude Code sandboxes script execution; we have a governance perimeter to keep airtight).

---

## 2. Two-layer architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│ STACK-AGNOSTIC CORE — new package `ne-skills` (runs in ne-worker,        │
│ per ADR-001 topology; registry tables on platform Postgres)              │
│                                                                           │
│  SkillRegistry          Postgres: ne_skills / ne_skill_versions /        │
│                         ne_skill_invocations (+ boot reconcile of the    │
│                         deploy-bundle skills/ dir, edit-wins by hash)    │
│  SkillForge             capture → signature → worthiness → author →      │
│                         near-dup → eval → PROPOSE (a FixProposal row)    │
│  SkillReview seam       delegates 100% to ne-flywheel NE-066             │
│                         ProposalReviewService + /v1/steward/* routes     │
│  SkillEvalSuite         implements frozen ne.contracts.EvalSuite;        │
│                         trigger evals; gate-before-approve               │
│  SkillGardener          monthly cron-as-isolated-agent → digest card     │
│  ActiveSetComputer      per-department/session bounded skill surface     │
│  skill tools            read.skills.search / read.skills.view (tier 0)  │
│                         — governed ToolBindings, NOT substrate-specific  │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │ SkillSubstrate Protocol (ne-contracts,
                                │ NEW — ADR + 1.7.0 → 1.8.0)
        ┌───────────────────────┴─────────────────────────┐
        │                                                 │
┌───────▼───────────────────────┐   ┌─────────────────────▼─────────────────┐
│ PydanticAiSkillSubstrate      │   │ ClaudeCodeSkillSubstrate              │
│ (this repo's production       │   │ (neutron-open-family installs /       │
│  binding, in ne-skills)       │   │  dev harnesses; proves the seam)      │
│ snapshots: audited tool-call  │   │ snapshots: run-terminal transcript    │
│  sequences per session        │   │  adapter (trident-adapter's slot)     │
│ author: ne-runtime isolated   │   │ author: CC subprocess pointed at      │
│  agent via ModelGateway       │   │  ne-gateway (C3 caveat, §10-Q8)       │
│ materialize: instruction      │   │ materialize: write packs into         │
│  splice + tool catalog        │   │  .claude/skills/ (native loader)      │
│ invocations: skill.view       │   │ invocations: PostToolUse hook /       │
│  handler counters (pull)      │   │  transcript scan (pull)               │
└───────────────────────────────┘   └───────────────────────────────────────┘
```

**Boundary rule:** nothing in `ne-skills` core may import `pydantic_ai`, `dbos`, or any substrate library. The core touches agents only through the frozen contracts (`ModelGateway`, `EvalSuite`, `AuditSink`, flywheel stores) and through `SkillSubstrate`. Mirror of the existing discipline in `ne-flywheel/governed.py:20-25` ("imports ONLY the frozen `ne.contracts.runtime` types — it does NOT import `ne.runtime`").

---

## 3. The `SkillSubstrate` Protocol (the crux)

### 3.1 Contract impact — stated plainly

`ne-contracts` is frozen at **1.7.0** (`packages/ne-contracts/pyproject.toml:3`). A grep of the contracts + govern trees confirms **no skill-related Protocol, type, or plugin slot exists anywhere today** — this is net-new surface. Per the freeze rules (`docs/adr/README.md`, SPEC §6): additive-only change + **ADR-0011** (in `packages/ne-contracts/adr/`, following ADR-0003…0010) + version bump **1.7.0 → 1.8.0** + `contracts` lane. A new entry-point slot `ne.plugins.skill_substrate` joins the existing 26 slots via the established `PluginRegistry` mechanism (`ne/contracts/plugins.py:20-67`). A conformance kit ships with the Protocol per C5.

### 3.2 The types (new module `ne/contracts/skills.py`, all frozen pydantic)

```python
class SkillSource(StrEnum):
    BUNDLED = "bundled"; ENGAGEMENT = "engagement"; FORGED = "forged"
    LEARNED = "learned"; MANUAL = "manual"

class SkillPack(BaseModel):            # the portable artifact — substrate-neutral
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str                          # slug
    version: int                       # monotonic; update-in-place, never -2 siblings
    description: str                   # ≤500 chars, trigger phrases folded in
    triggers: tuple[str, ...]          # explicit trigger phrases (eval targets)
    body: str                          # markdown: When to Use / Procedure / Pitfalls / Verification
    source: SkillSource
    category: str = ""
    tags: tuple[str, ...] = ()
    requires_tools: tuple[str, ...] = ()   # ToolSpec names; conditional visibility
    platforms: tuple[str, ...] = ()        # substrate_ids this pack targets ("" = all)
    acl_labels: tuple[str, ...] = ()       # ne-permissions ACL labels; empty = global (visible to all).
                                           #   NOT a bespoke scope model — the SAME label vocabulary
                                           #   PrincipalExpander matches chunks against (§7.7). Assigned
                                           #   by the approver (a skill has no source system to derive
                                           #   from), then filtered by the SHARED per-principal expander.
    provenance: AnyMap = FrozenDict()      # workflow snapshot id, session, principal, config_hash, dates
    content_hash: str                      # sha256 over canonical pack bytes

class WorkflowStep(BaseModel):         # one observed action in completed work
    model_config = ConfigDict(frozen=True)
    action: str                        # tool/action NAME only — never argument values (C12
    outcome: str                       #  redaction discipline, mirrors audit detail rules)
    detail_ref: str = ""               # opaque ref back into substrate-side records

class WorkflowSnapshot(BaseModel):     # substrate-neutral completed-work record
    model_config = ConfigDict(frozen=True)
    snapshot_id: str
    substrate_id: str
    intent: str                        # scrubbed one-line statement of what was asked
    steps: tuple[WorkflowStep, ...]
    succeeded: bool
    principal_id: str
    session_id: str
    department: str | None = None
    started_at: datetime; finished_at: datetime

class SkillInvocation(BaseModel):      # one firing, for usage telemetry
    model_config = ConfigDict(frozen=True)
    skill_name: str; version: int
    principal_id: str; session_id: str
    fired_at: datetime
    run_succeeded: bool | None = None  # None = outcome unknown/not yet attributable
```

### 3.3 The Protocol

```python
class SkillSubstrate(Protocol):
    """Everything about skills that differs per agent backend. Cursor-pull style
    throughout (mirrors the flywheel signal-cursor pattern) so the core's
    scheduled jobs drive all I/O and a substrate never calls into the core."""

    @property
    def substrate_id(self) -> str: ...                     # "pydantic-ai" | "claude-code" | ...

    async def snapshots(
        self, *, cursor: str | None = None, limit: int = 50,
    ) -> tuple[Sequence[WorkflowSnapshot], str]:
        """Completed multi-step work since cursor. HOW completed work is observed
        is the substrate's business (audit-derived here; transcript-derived on CC)."""

    async def author(
        self, snapshot: WorkflowSnapshot, *, standard: AnyMap,
        existing: SkillPack | None = None, edits: AnyMap | None = None,
    ) -> SkillPack:
        """LLM-author (never template) a draft pack from a snapshot; `existing`
        set ⇒ author an UPDATE to that pack (near-dup path). Output is a DRAFT —
        the core deterministically validates it against the house standard and
        the core alone decides whether it is ever proposed."""

    async def materialize(self, packs: Sequence[SkillPack], *, surface_key: str) -> None:
        """Make the given ACTIVE SET discoverable on one discovery surface.
        The packs are ALREADY ACL-filtered for that surface's principal by the
        core (§6.2 filter 1 / §7.7) — the substrate just projects them; it makes
        no authorization decision. `surface_key` = a session/principal for an
        interactive agent, or a shared surface for cron. Instruction-splice
        here; dir-write on CC."""

    async def withdraw(self, names: Sequence[str], *, surface_key: str) -> None:
        """Remove retired/deactivated skills from the discovery surface."""

    async def collect_invocations(
        self, *, cursor: str | None = None, limit: int = 500,
    ) -> tuple[Sequence[SkillInvocation], str]:
        """Firings since cursor, for the registry's usage fold."""
```

### 3.4 Binding A — `PydanticAiSkillSubstrate` (this repo's production binding)

* **`snapshots`** — derived from what C6 already records: every tool call flows through `GovernedToolExecutor` and emits an audit event carrying tool name / tier / outcome, correlated by `run_trace_id` (`ne/runtime/correlation.py`, stamped in `middleware.py`), plus session/process records in `ProcessRegistry` and the session store (`agent.py:146-152, 273-275` — `registry.finish(process_id, succeeded=...)`). The binding folds a finished session/process's ordered audited tool-name sequence into `WorkflowStep`s — **no new instrumentation of the agent loop is needed for step observation**, and steps are *observed*, not templated (fixing the source's hardcoded-Trident-steps gap, source §1.2 item 2). Names-only steps also inherit the audit redaction discipline (no argument values) for free. [deepen-verified caveat 2026-07-20: `ProcessRegistry` is IN-MEMORY (`processes.py:40-56`; `finished()` is an in-process iterator) — durable run-terminal records exist today for cron runs (scheduler run rows) and sub-agent dispatches (completion audit events), but an interactive session's terminal state does not survive a restart. The implementation plan (NE-SKILL-1b, decision D-2) resolves this with a small `session.run` terminal audit event or by scoping v1 snapshots to cron + sub-agent runs.]
* **`author`** — dispatches an isolated authoring agent through the existing runtime: an `AgentDefinition` (`ne/runtime/dispatch.py:126-151`) named `skill-author`, `toolset=()` (zero tools — fail-closed, it can only write text), `route_task="skill-authoring"` so the model registry picks the model (**C3: every generative call rides `ModelGateway`; no provider SDK anywhere near this path**), `route_lane="private-vllm"` by default — snapshot intents are customer-derived text, and frontier-lane sub-agent routing is **disabled by default** — it raises `FrontierRoutingDisabledError` (`dispatch.py:112`) unless a composition root explicitly passes `allow_frontier_subagent_routing=True` (`dispatch.py:171`); NE-162 pre-egress redaction is the documented precondition for ever flipping that opt-in, enforced by convention (comment + audit-as-denied), NOT by a code dependency on redaction-readiness. [verification-corrected: an earlier draft overstated this as "code-enforced inert".] The core then validates the draft deterministically (frontmatter completeness, description length, section order, `requires_tools` ⊆ registered tool names). *Deterministic validation, LLM authorship* — the source design's exact split (source §3.1).
* **`materialize`** — two coordinated moves per discovery surface (§6), over the core-supplied ACL-filtered active set (§7.7 — the substrate never re-decides authorization): (1) render the active set's name+description index into that surface's agent instructions (sub-agents and cron agents already have an `instructions` seam — `dispatch.py:403`, `scheduler.py:366`; the interactive `DepartmentAgentSession` needs a small instructions seam added, §10-Q1); (2) ensure the two governed skill tools are in the surface's granted toolset.
* **`collect_invocations`** — reads the binding's own invocation rows, written by the `read.skills.view` tool handler at fire time (§6.3). Pull-with-cursor so the Gardener/telemetry fold job drives it.

### 3.5 Binding B — `ClaudeCodeSkillSubstrate` (proves "any stack we support")

* **`snapshots`** — the slot the source design's `trident-adapter.ts` occupied: a run-terminal observer parses the completed run/journal into `WorkflowSnapshot`s with *observed* steps.
* **`author`** — spawn-and-stdio Claude Code subprocess handed the snapshot + house standard, exactly the source design §3.1 mechanism. **C3 caveat stated honestly:** a bare `claude` subprocess is a hardcoded-provider path. Acceptable only when (a) the deployment IS a Claude-Code-substrate product (neutron-open family), or (b) the subprocess is pointed at `ne-gateway` via its base-URL override so routing stays in the model plane. This binding is a Protocol-proof and a bridge for the neutron-open port-back — it is not this repo's production path (§10-Q8).
* **`materialize`/`withdraw`** — write/remove pack dirs under `.claude/skills/` (the native loader IS the discovery surface on this substrate; the registry stays authoritative and the dir is a projection).
* **`collect_invocations`** — PostToolUse hook on the Skill tool, or transcript scan at run-terminal (both from the source design §3.2-C), buffered locally and drained by the pull.

With both bindings sketched against one Protocol, "modular so it works with any stack we support" is a contract with a conformance kit, not an aspiration. C5 note: once both bindings exist, the swap test is CI-enforced.

---

## 4. The registry (stack-agnostic core)

### 4.1 Schema — Postgres, append-only where it matters

Following the platform storage spine (SPEC §3, "Postgres is the platform's default state store") and the flywheel's table style (`proposals.py:140-175` — own `MetaData`, `ensure_*_schema`, BigInteger/Identity with SQLite CI variant):

* **`ne_skills`** — one row per skill name: `name` (unique), `source`, `category`, `acl_labels` (JSONB array of ne-permissions labels — see §7.7; NOT a bespoke scope model), `status` (`active | retired | archived`), `current_version`, `created_at`. Status transitions are folded from an append-only `ne_skill_lifecycle` log (mirroring how NE-066 folds proposal status from the review log rather than mutating rows, `review.py:24-31`).
* **`ne_skill_versions`** — append-only: `(name, version)` unique, full pack `body` + frontmatter columns, `content_hash`, `provenance` (JSONB: snapshot id, session, principal, config_hash, approving `review_id`), `created_at`. Version history restorable by construction — the source design's "prior SKILL.md kept in registry blob" (source §3.2-B), done properly.
* **`ne_skill_invocations`** — append-only firing rows (from `collect_invocations`), plus a folded read (`fired_count`, `last_fired_at`, `fail_count`) for the Gardener and the active-set ranker.

### 4.2 Four sources, mapped to this stack

| Source | Mechanism here | Provenance stamp |
|---|---|---|
| **Bundled defaults** | `skills/` dir in the deployment bundle (compose/helm config bundle, NE-070's per-customer config bundle); boot-time reconcile into the registry | `source=bundled`, origin hash |
| **Engagement packs** | per-customer packs in the same bundle dir (the nearest analog of the source design's "Cores" — see §10-Q7) | `source=engagement` |
| **Forged** | SkillForge propose→approve (§5) | `source=forged` + full workflow provenance |
| **Manual / learned** | hand-drop a pack dir into the bundle (reconciled on boot, enters as a *proposal* unless operator-marked trusted), or a later `/learn` verb through the same proposal gate | `source=manual` / `learned` |

**Delta from the source design, flagged:** the source's premise was *file-is-truth, registry-derived* (source §3, single-owner `~/.claude/skills/`). Here the **registry is authoritative for agent-authored packs** and the disk is (a) an *input* for bundled/engagement/manual packs (reconciled on boot, edit-wins by content hash — Hermes semantics preserved) and (b) a *projection* for substrates that discover from disk (the CC binding). Reasons: this is a multi-user server deployment with no owner home dir; C6/C10 require the approve→register transition to be transactional with its audit record; and per-role/team scoping (NE-SKILL-1 row) needs queryable state. The convention-over-configuration property the source valued survives at the reconcile seam: dropping a valid pack dir into the bundle still works.

### 4.3 Boot reconcile

On `ne-worker` start: scan the bundle `skills/` dir → validate each pack against the house standard → upsert `bundled`/`engagement` packs whose content hash changed **unless** the registry copy was locally edited (edit-wins; `reset --restore` analog = a steward action re-arming sync). Invalid packs are quarantined with a `ShapeViolation`-style signal, never silently skipped. Hand-dropped `manual` packs enter as pending proposals (the gate is source-blind).

---

## 5. Lifecycle — delegated to ne-flywheel, gated by ne-eval

### 5.1 Propose (SkillForge, a scheduled governed job)

The forge reuses the flywheel spine rather than reinventing it — exactly what the SPEC Phase-7 preamble locks ("reuses NE-066 propose-then-approve + NE-067 steward queue"):

* **New `ProposalKind` values** `skill_candidate` and `skill_update_candidate` — the `kind` column is a plain `str`, and the docstring at `proposals.py:49-56` records exactly this extension pattern twice already (`ENTITY_MERGE_CANDIDATE`, `VERIFIED_ANSWER_CANDIDATE`): **no migration, no contracts change**.
* **New `FixLane.SKILLS = "skills"`** — a job-emitted lane like `ENTITY_RESOLUTION` and `ONTOLOGY` (`lanes.py:44-69`): never returned by `route_cluster`, auto-trusted by the diff renderer because `_KNOWN_LANES` derives from the enum (`diffs.py:66-70`). A `ne-flywheel` change, not a contracts change.
* **The forge run** is a tier-1 cron-as-isolated-agent granted exactly one tool, `write.internal.skills.forge_run` — a verbatim structural copy of the curation job (`governed.py:39-96`): `ToolSpec(tier=1)`, args-ignoring handler, `ScheduledJob(toolset=(one_tool,))`. `write.internal.` classifies at tier 1 under the existing `tier_for_action` mirror (`actions.py:46-48`) — **no Rego/mirror change needed**.
* One run: pull `substrate.snapshots()` from the cursor → compute the **workflow signature** (sha256 over the normalized ordered step-action shape — the source's `signature.ts` concept, fed by *audited* actions here) → score worthiness (§5.2) → for the top candidate, `substrate.author()` → deterministic standards validation → near-dup check (§5.3; on hit, re-author as an UPDATE against the existing pack) → run the trigger eval (§5.2) → append a `FixProposal(kind=skill_candidate, lane=SKILLS, payload={draft pack, worthiness, near-dup analysis, eval-run ref})` via the append-only `SqlAlchemyProposalStore` (`proposals.py:229-288` — idempotent content-addressed `proposal_id`, so re-runs append nothing). Proposals + cursor + audit dual-append commit in one transaction, mirroring `CurationAgent.run_once` (`curation.py:3-43`).

### 5.2 Anti-pollution — BIRTH (NE-SKILL-2)

* **Worthiness score replaces the boolean gate** (source §3.2-A): floor = succeeded ∧ ≥2 distinct actions; rank = repetition (same/similar signature ≥2× in 30 days — the strongest recurrence signal), step count, recency. Top-ranked candidate proposed first.
* **Rate cap:** ≤2 open `skill_candidate` proposals, ~1 new/day. Excess candidates queue silently, ranked. Proposal spam is pollution of steward attention.
* **Decline memory — the SIGNAL exists; the suppression is a (small) build on top of it.** Every NE-066 rejection atomically emits a `thumbs_down` correction signal whose provenance carries `rejected_proposal_id` / `rejection_code` (`review.py:33-46, 556-606`), and `RejectionCode.DUPLICATE / STALE / OUT_OF_SCOPE / UNSAFE` already exist (`review.py:129-137`). What does NOT exist yet is any consumption of that signal — `review.py` explicitly records that acting on captured feedback is later work. So NE-SKILL-2's worthiness scorer must BUILD the fold: query skill-lane rejection signals and suppress a rejected signature for 90 days unless its repetition count grows ≥3. The persisted signal saves us the capture half; the suppress half is new code. [verification-corrected: an earlier draft claimed this was "a query, not a build" — the reusable substrate is real, the suppression logic is not.]
* **Eval-gated writes** (the do-better-than-Hermes move, and the NE-SKILL-2 row's hard requirement): a `SkillTriggerEvalSuite` implementing the frozen `ne.contracts.EvalSuite` Protocol (`evalobs.py:32` — `name` + `async run(*, config_hash) -> EvalRunResult`) runs **before the proposal is surfaced** and its passing run is a **precondition the approve seam enforces** (§5.5). v1 metrics, honoring the §3.1 brain-tier degradation story:
  * *Deterministic (always-works default, no judge, no embeddings):* routing eval through the real `read.skills.search` seam — each of the pack's `triggers` must rank the pack top-k against the current library; a generated near-miss set must NOT. This tests the actual discovery path agents will use, not a simulation.
  * *Judged (optional tier):* procedure-quality rubric via the existing cross-family `Judge`/`GatewayJudge` machinery (`ne/eval/judge.py:3-15` — gateway-only egress, versioned rubrics, calibration-artifact enforcement in `suite.py:103-110`). Off by default; adds no dependency.
  * Thresholds use the existing `ThresholdSpec` PASS/WARN/FAIL tolerance bands (`thresholds.py`), runs recorded append-only with `config_hash` (the `EvalRunStore` pattern).

### 5.3 Anti-pollution — DUPLICATION (NE-SKILL-3)

* **Near-dup detection at proposal time, across ALL sources**: candidate `name+description+triggers` vs the whole registry. Default install (SPEC §2.5 / §3.1: keyword+graph always works; embeddings opt-in): **token-set Jaccard ≥ 0.5 OR shared-trigger-phrase hit → flag**. Stdlib only — zero new dependencies (C4 clean). With an embedder configured (the existing `ne.plugins.embedder` slot; in-VPC TEI, so C2-clean): cosine ≥ 0.85 → duplicate, 0.70–0.85 → overlap warning. Thresholds are the source design's starting points, to be tuned against the real library.
* **On hit: propose an UPDATE, never a sibling** — `substrate.author(snapshot, existing=<pack>)` produces a revision; the proposal card reads "this looks like an improvement to *deploy-staging* — update it?" Duplication failure mode → maintenance success mode (source §3.2-B, unchanged).
* **Update-in-place with version bump** is native to the registry schema (§4.1); the `-2`-suffix pollution the source called out cannot occur by construction.

### 5.4 Anti-pollution — ROT (NE-SKILL-4)

* **Telemetry fold:** a scheduled job drains `substrate.collect_invocations()` into `ne_skill_invocations` and refreshes the folded counters.
* **Retirement lane:** unfired 90 days ∧ <3 lifetime fires → retirement candidate. Retire = lifecycle-log append (`status=retired`) + `substrate.withdraw()` from every discovery surface. Content stays in `ne_skill_versions` — restorable in one action, **never silent-delete**. Bundled/engagement packs retire the same way (edit-wins protects them from resurrection at reconcile).
* **Failure flagging:** associated runs failing ≥50% over ≥4 fires → flagged for repair in the digest. (Outcome attribution is approximate in v1 — §10-Q6.)
* **The Skill Gardener:** monthly `ScheduledJob` granted exactly `write.internal.skills.gardener_run` (tier 1, same isolated-cron pattern as §5.1). Sweep: near-dup scan across sources, trigger-overlap lint (still a genuine ecosystem differentiator — nobody else does it), retirement candidates, failure flags, `requires_tools` referencing tools no longer registered, hash drift vs bundled origin, stale open proposals (>14 days → auto-reject `STALE`, the quiet "skipped" state). Output: **ONE digest card** with batched actions, delivered through the existing steward digest surface (§8).

### 5.5 Approve / edit / decline / apply — the gate, reused not rebuilt

Skill proposals ride the **exact** NE-066 machinery: `ProposalReviewService.approve / reject(code=…) / record_apply` (`review.py:303-351`), surfaced by the **existing** `/v1/steward/proposals*` routes (`ne-api/routers/steward.py:204-286`) and the **existing** console Flywheel queue (`ne-console/src/flywheel/FlywheelPage.tsx`, `ProposalQueue.tsx`) — the NE-067 steward queue the SPEC decision-log mandates reusing. Race-safety, append-only review log, dual-append audit, rejection-signal feedback: all inherited.

**The apply step is where skills genuinely extend the flywheel, and the precedent already exists.** NE-066 v1 is "proposals + tracked manual apply" (`review.py:17-22`) because flywheel fixes land via git-PR/OPA-bundle flows. A skill apply is a platform-internal registry write — and the ontology lane already faced exactly this: `steward_apply_verified_template` (`steward.py:448`) performs the governed apply *through the API seam* and records it. Skills follow that pattern: a `POST /v1/steward/proposals/{id}/apply_skill` seam (steward-guarded like `_require_steward`, `steward.py:104`) that — in one transaction — appends the `ne_skill_versions` row + lifecycle log entry, calls `record_apply(applied_ref="skill:<name>@v<n>")`, and dual-appends the audit event. Eval-gate enforcement lives here too: the seam refuses to approve/apply a `skill_candidate` whose payload lacks a passing, config_hash-stamped eval-run ref (fail closed).

**Edit before approve:** the current steward routes accept a `note` but not payload edits (`steward.py:215-259`). The source UX ("change a word, tap approve") needs an edit path: proposed as edit-fields on the skill apply seam (name/description/triggers only), which re-runs the trigger eval against the edited pack before committing. Flagged as a small ne-api delta (§10-Q3).

**Approval authority:** the NE-SKILL-1 row says "owner-or-manager approval." The steward-role guard (`_require_steward`) is the v1 authority; per-department approval routing is an open question (§10-Q4). Note the strongest in-repo analog for the whole propose→distinct-approver→commit shape is `MemoryPromoter` (`ne/runtime/memory_promotion.py:76-174` — agent proposes, a *distinct named principal* promotes, provenance-stamped, audit-first): the skill gate matches its discipline.

---

## 6. Discovery + invocation — the biggest porting gap, treated as such

neutron-open leans on Claude Code's native `.claude/skills/` loader: packs on disk are discovered and progressively disclosed by the harness itself, routing on `description`. **Pydantic-AI has no such loader.** Nothing discovers a pack; nothing routes on a description; nothing meters a firing. The port must build the discovery surface. Three candidate mechanisms were considered:

| Option | Mechanism | Verdict |
|---|---|---|
| (a) One tool per skill | each active skill registered as its own `ToolSpec` | REJECTED for v1: N×catalog bloat in every model call, N×`validate_binding`/tier churn, and tool-choice routing quality on hundreds of near-idle tools is poor |
| (b) **Index splice + two governed tools** | active set's name+description index in instructions; `read.skills.search` + `read.skills.view` for disclosure | **CHOSEN** — reproduces Hermes/CC progressive disclosure (Level 0 index / Level 1 body) with exactly 2 catalog entries, and makes every firing a governed, audited act |
| (c) Retrieval-only (no splice) | agent must always call `skill.search` first | kept as the degraded fallback if the splice underperforms; weakest routing (the model doesn't know what it doesn't know) |

### 6.1 The two governed tools (core-owned, substrate-independent)

* **`read.skills.search`** — tier 0 (`read.` prefix classifies at tier 0 in the existing mirror, `actions.py:44-46` — no Rego change). Query the FULL library (not just the active set): keyword/BM25 on the always-works default (Postgres full-text over name/description/triggers — no new dependency), embedding recall when the embedder slot is configured. Returns name+description+score. A library hit outside the active set is thereby reachable — the agent pays context only for what it pulls.
* **`read.skills.view`** — tier 0. Returns the pack body for injection into the working context. **This call IS the firing**: the handler appends the invocation row (fired counters, §5.4) and, because it is an ordinary `ToolBinding` executed by `GovernedToolExecutor`, the firing is guardrail-checked, PDP-decided, and audited **by construction** (`middleware.py:3-23`). The source design needed a PostToolUse hook bolted onto an ungoverned native loader; here the C6 chokepoint does governance *and* telemetry at one seam. This is the port's biggest structural win.

Both tools are also automatically agent-native on every surface: `GovernedMcpServer` advertises whatever `executor.tool_specs` carries (`ne-mcp/server.py:172-195`), so MCP clients get `skills` search/view (and the steward decide flow rides the existing governed steward tools/routes) with no extra work.

### 6.2 The active set (scale layer)

The registry computes, **per principal**, the active set: bundled defaults + engagement packs + usage-ranked forged/manual skills, gated by TWO filters kept deliberately distinct because they answer different questions:

1. **ACL visibility (authorization — the SHARED brain spine, §7.7):** a pack is visible only if its `acl_labels` intersect the principal's match-set from `PrincipalExpander.expand(principal)` — the *exact* filter that trims knowledge chunks. Empty `acl_labels` = global. This is NOT a skill-specific scope model; it is the brain's one ACL, applied to a second item type.
2. **Conditional visibility (capability — skill-specific, not authorization):** a pack whose `requires_tools` ⊄ the principal-session's granted toolset, or whose `platforms` excludes the substrate, never enters the surface (Hermes's best idea, checkable against real `ToolSpec` grants). This is about *can it even run here*, not *may this person see it* — genuinely skill-specific, duplicates nothing.

Cap ~150 packs ≈ 5–6k tokens of index (source §3.3 numbers; tune down if session budgets demand). `materialize()` pushes the resulting index; usage-ranking makes the surface self-tuning; everything else stays searchable via `read.skills.search` (itself ACL-filtered by the same expander, so search can never surface a pack the principal may not see).

**Trigger-overlap lint** at creation + gardener time resolves collisions (merge via the near-dup path, or disambiguate descriptions). Same-name shadowing precedence: manual > forged > engagement > bundled.

### 6.3 Injection seam honesty

Sub-agents and cron agents have an `instructions` seam today (`dispatch.py:403`, `scheduler.py:366`) — the splice lands there cleanly. The interactive `DepartmentAgentSession` builds its `Agent` with no system-prompt/instructions parameter (`agent.py:194-202, 255-264`) — a small ne-runtime addition is required to carry the skill index (and is generally useful). Flagged: §10-Q1. Routing quality of "index in instructions + model decides to call `skill.view`" versus a native harness loader is **unproven on this stack** — which is precisely why the trigger eval (§5.2) tests routing through the real search seam, and why NE-SKILL-1a ships the read side early (§9) so the question is answered with data before the forge exists.

---

## 7. Governance additions the source design never had to make (C6/C10/C12)

Stated as additions because neutron-open's governance is thinner — these are neutron-enterprise obligations, not gold-plating:

1. **A skill firing is an agent action** → PDP-decided + audited, no bypass (C6). Achieved structurally: firing = `read.skills.view` through the one governed executor (§6.1). Firings therefore also land in the hash-chained tamper-evident audit log (C10) with `config_hash`, principal, session — "which skill influenced this run" is replayable.
2. **Skill lifecycle actions are audited**: propose/approve/reject/apply ride NE-066's dual-append (`review.py:48-56`); forge and gardener runs are tier-1 governed tool calls with their own audit trail (§5.1/§5.4); registry writes happen only inside those governed paths.
3. **C12 — a skill body is a persistent injection surface.** An approved pack's body is injected into agent context on every fire; a poisoned skill is a durable prompt-injection vector with fleet-wide blast radius. Mitigations layered: human approval on EVERY version (updates re-enter the gate — no silent revision), content-hash-pinned versions (the view handler serves exactly the approved bytes), provenance auditability back to the birthing workflow, deterministic standards validation, the no-executables rule (§1), and — unchanged from the platform's baseline posture — deterministic guardrails + the tier ladder mean even a fully hijacked agent cannot execute Tier-2/3 actions without a human (`middleware.py:36-42`). Residual risk flagged in §10-Q9 (signing).
4. **Untrusted-text discipline in review artifacts**: skill proposal payloads rendered into steward cards follow the `diffs.py` sanitizer boundary (`_safe`/`_fenced`; `diffs.py:24-41`) — a malicious draft cannot break out of its card.
5. **Model-independence (C3)**: authoring routes through `ModelGateway` via task-based routing; nothing in `ne-skills` imports a provider SDK (import-linter enforced platform-wide). The CC binding's caveat is recorded (§3.5, §10-Q8).
6. **C4 licensing**: the core needs **zero new runtime dependencies** (SQLAlchemy/pydantic already in closure; Jaccard is stdlib; FTS is Postgres). Optional embedding recall reuses the in-VPC TEI embedder already shipped. Nothing to allowlist.

### 7.7 One ACL for the whole brain — no duplicate authorization path (owner-directed 2026-07-20)

**Decision:** skill visibility uses the platform's single authorization substrate, not a skill-specific scope model. `ne-permissions.PrincipalExpander.expand(principal)` returns the frozenset of ACL labels a principal matches (`expand.py:79-100`); its docstring says "labels a *chunk* may name" only because chunks are its *current* consumer — the mechanism is item-agnostic (principal match-set ∩ item labels). A skill is filtered by the **same** expander against its `acl_labels`. Firing a skill is already a `read.skills.view` call through the one `GovernedToolExecutor` (§6.1), so the PDP decision and audit are shared too. Result: **one ACL/governance/provenance spine across knowledge and skills**, which is also how a customer reasons about it — the "brain" is the whole governed substrate, and it has one permission model.

**The one honest difference** (a difference in label *origin*, not a second ACL engine): a knowledge chunk *derives* its labels from a source system at ingestion (NE-044 stamps SharePoint/etc. permissions onto chunks); a forged skill has no source system, so its `acl_labels` are *assigned by the approver* at the gate ("this skill is for finance"). Same vocabulary, same expander, same PDP, same audit — assigned vs derived is the only delta, and it lives entirely at creation time.

**What stays legitimately separate, and is NOT duplication:** (a) the STORE — a skill pack is a versioned, tool-fired artifact, not a chunk (separate table, exactly as chunks/graph/audit are already separate stores under the one brain); (b) the ACTIVE-SET / token-budget layer (§6.2 filter 2) — capping the routing surface is about context cost, not permission, and is genuinely skill-specific. Consolidating the ACL while keeping these separate is the whole point: shared where the concern is identical (authorization), separate where the artifacts genuinely differ.

> *An earlier draft of this design gave skills a bespoke `scope: {departments, roles}` field with its own visibility logic. Owner flagged it as a duplicate authorization path (maintenance hazard). Verified correct against the code and consolidated onto `ne-permissions` — this section is the record. NE-SKILL-0's contracts must land `acl_labels`, not a scope map.*

---

## 8. UX surfaces (mapped to what exists)

| Surface | Mechanism | Delta required |
|---|---|---|
| Proposal card (approve/edit/decline) | console `/flywheel` `ProposalQueue` — skill proposals arrive as ordinary steward proposals via `GET /v1/steward/proposals` | a payload renderer for `skill_candidate` kind (name, description, triggers, "learned from Tuesday's deploy" provenance line); edit fields (§5.5) |
| Steward decisions | existing `POST /v1/steward/proposals/{id}/approve\|reject` (server-derived identity, `client.ts:416-437`) + new `apply_skill` seam | one new route (§5.5) |
| Gardener digest | existing digest surface: `GET /v1/steward/digest` → console `DigestPanel` (`/flywheel?tab=digest`); chat garnish via frozen `ChatAdapter.send_digest` (Slack `slack/adapter.py:72`, Teams `teams/adapter.py:59`) | extend the digest payload with the skills section + batch actions |
| Agent-native parity | `GovernedMcpServer` auto-advertises the skills tools (§6.1); steward flow reachable through governed API | none beyond the tools themselves |
| A skill firing | invisible by default; the audit event is the attribution record; console chat can render a "⚡ used skill: deploy-staging" chip from the run's audit trail (the `/audit` + `/trace` views already exist) | small console chip, optional v1 |
| Library browse (`/skills`) | v1: the flywheel page filtered to the skills lane + registry list endpoint | a modest console view; the DEFAULT management UX remains the monthly digest — a user who never opens it still gets a curated library |

Total user thinking required, unchanged from the source: tap Approve occasionally; act on one digest card a month.

---

## 9. Work-item decomposition — confirming and revising NE-SKILL-1..4

The SPEC rows are directionally right; two structural deltas are flagged.

**Δ1 — a contracts unit is missing.** NE-SKILL-1 cannot start with an unfrozen seam: `SkillPack`/`WorkflowSnapshot`/`SkillInvocation`/`SkillSubstrate` + slot + ADR-0011 + 1.8.0 must land through the contracts lane first (SPEC §6 discipline). Proposed **NE-SKILL-0 (S, contracts lane)**.

**Δ2 — split NE-SKILL-1 into read side and write side.** The L row bundles registry+capture+authoring+HITL+library+discovery. The read side (library exists, agents can discover and fire hand-authored/bundled skills) is independently shippable, independently valuable, and **de-risks the §6 discovery question with real data before any forging exists**. The write side then builds the forge against a proven discovery surface.

| Unit | Scope | Size | vs SPEC row |
|---|---|---|---|
| **NE-SKILL-0** | ADR-0011 + `ne/contracts/skills.py` (types + `SkillSubstrate`) + `ne.plugins.skill_substrate` slot + conformance-kit stub + 1.7.0→1.8.0 | S | **NEW — flagged delta** |
| **NE-SKILL-1a** | `ne-skills` package: registry schema + boot reconcile (bundled/engagement, edit-wins) + house-standard validator + `read.skills.search`/`read.skills.view` governed tools + active-set computer + `PydanticAiSkillSubstrate.materialize/collect_invocations` + the session instructions seam (§10-Q1) + console library list | M/L | **SPLIT of NE-SKILL-1 — flagged** |
| **NE-SKILL-1b** | SkillForge job: `snapshots()` (audit/session-derived) + signature + worthiness v0 (floor only) + authoring agent via gateway + `skill_candidate` proposals on the SKILLS lane + `apply_skill` steward seam + console card renderer | L | remainder of NE-SKILL-1 |
| **NE-SKILL-2** | worthiness ranking + repetition signal + rate cap + decline suppression (rejection-signal fold) + `SkillTriggerEvalSuite` + eval-gate enforcement at the apply seam | M | **CONFIRMED** as specced |
| **NE-SKILL-3** | near-dup (Jaccard default / embedder opt-in) + propose-UPDATE path (`author(existing=…)`) + edit-before-approve fields | M | **CONFIRMED**; edit-path scope added |
| **NE-SKILL-4** | invocation fold + retirement lane + Gardener cron + digest integration + trigger-overlap lint + failure flags | M | **CONFIRMED** as specced |
| **NE-SKILL-5** | `ClaudeCodeSkillSubstrate` binding + dual-binding conformance/swap test (C5) | M | **NEW, deferrable — flagged**; without it "any stack" is one stack |

Dependency order: 0 → 1a → 1b → {2, 3} → 4; 5 any time after 0 (its authoring caveat resolved per §10-Q8). Sequencing rationale mirrors the source's W4 ordering ("without quality at birth, every later mechanism curates garbage") with one amendment: discovery risk (this stack's novel gap) is retired first via 1a.

---

## 10. OPEN QUESTIONS — where neutron-enterprise lacks something neutron-open assumed

1. **Interactive-session instructions seam.** `DepartmentAgentSession` constructs its pydantic-ai `Agent` with no system-prompt parameter (`agent.py:194-202, 255-264`); only sub-agents/cron have `instructions`. The skill index needs a session-level instructions seam added to ne-runtime (small, generally useful, but it touches the C6-guarded session path — needs its own review care). Owner of this change: NE-SKILL-1a (proposed).
2. **Routing quality without a native loader is unproven.** The splice+search design (§6) is a reasoned bet, not a measured one. If the trigger evals show weak routing at realistic library sizes, fallback ladder: richer index (descriptions → descriptions+triggers), search-first prompting, or per-skill tools for the top-N hottest skills only. 1a exists to answer this early.
3. **Edit-before-approve** does not exist on the steward routes today (`steward.py:215-259` — `note` only). Proposed as edit-fields on the `apply_skill` seam with eval re-run. Small ne-api delta.
4. **Who approves skills?** v1 = the existing steward role guard. The NE-SKILL-1 row says "owner-or-manager"; per-department/manager approval routing (a department-scoped steward) is undesigned — deliberately deferred until multi-team deployments demand it. Also open: may a department steward approve a *global* skill (empty `acl_labels`)?
5. **Frontier-lane authoring.** Default is `route_lane="private-vllm"` for the authoring agent (snapshot intents are customer text). If an engagement wants frontier-quality authoring, that waits on NE-162 pre-egress redaction (the existing code-enforced gate in `dispatch.py`). Until then air-gapped/private installs author on the open-weight lane — quality difference unmeasured.
6. **Run-outcome attribution is approximate.** `SkillInvocation.run_succeeded` correlates a session's overall success with the skills it viewed — correlation, not causation; a good skill in a failing run accrues unfair failure signal. v1 accepts this (the failure flag only triggers review, never auto-retirement); a causal design is future work.
7. **No Cores concept exists here.** The source's "Core-bundled skills" (and its skills-as-Core export path) map to the per-customer deployment bundle (§4.2) — a folder convention, not a packaged capability unit with install/uninstall lifecycle. If neutron-enterprise ever grows an installable-module concept, the registry's `source=engagement` + retire-on-uninstall semantics are designed to slot in; until then "uninstalling a Core retires its skills" has no trigger event.
8. **The CC binding vs C3.** A Claude Code subprocess is a provider-locked authoring path unless pointed at `ne-gateway`. The binding is a Protocol-proof + neutron-open bridge, not a production path for this repo; if it ever ships in an engagement, the gateway-override configuration becomes mandatory and license/ToS review applies. (No code from `@neutronai/skill-forge` is vendored — the port is design-level; nothing new enters the C4 gate.)
9. **Skill pack signing.** Content-hash pinning + gate + audit may not satisfy a regulated buyer for fleet-injected prompt content; cosign-style signing of approved packs (riding the existing C11 signing infra) is a plausible hardening rider. Not in v1 scope; flagged for the security dossier when NE-SKILL-* ships.
10. **Forge/gardener multi-worker safety.** The forge cursor follows the curation single-writer pattern; real concurrent-worker double-run safety is LIVE-PARKED exactly as `curation.py:40-43` records for the flywheel — same ledger, same NE-070-tier resolution.
11. **`/learn` verb** (Hermes copy #2): deliberately out of the numbered units — it is a thin second entry point into the same author→propose pipeline once 1b exists. Proposed as a P2 follow-on, not lost.

---

## 11. Constraint checklist (§2 hard constraints, explicit)

| Constraint | How this design satisfies it |
|---|---|
| C1/C2 | Everything in-VPC: Postgres registry, in-VPC TEI for optional embeddings, gateway-only model calls; no new egress paths |
| C3 | Authoring via `ModelGateway` task routing; no provider SDK in `ne-skills` (import-linter covers it); CC-binding caveat recorded (§10-Q8) |
| C4 | Zero new runtime dependencies; nothing to allowlist |
| C5 | New Protocol ships with conformance kit; swap test enforced once binding #2 exists (NE-SKILL-5) |
| C6 | Skill firing, forge run, gardener run, and every lifecycle transition are governed tool calls or NE-066 transitions — all PDP-decided and audit-appended, no bypass (§7) |
| C7 | Skill visibility rides the **same** `ne-permissions` ACL as the brain (§7.7): a pack's `acl_labels` are matched against `PrincipalExpander.expand(principal)` — one authorization path for knowledge and skills, no bespoke second model. Skills are still not a write-back path; the no-executables rule holds. |
| C10 | All skill events ride the hash-chained log via existing dual-append patterns |
| C12 | Guardrails-first ordering inherited; pack bodies sanitized at card-render, hash-pinned at serve, human-gated at every version (§7.3) |
| Frozen contracts | ADR-0011 + 1.8.0, additive-only, contracts lane (NE-SKILL-0) |
| §3.1 degradation | Keyword/FTS + Jaccard is the always-works default; embeddings strictly opt-in via the existing slot |

---

*Port authored against source read at: `ne-flywheel` (proposals/review/curation/lanes/governed/capture/diffs), `ne-eval` (suite/judge/thresholds/golden), `ne-runtime` (agent/middleware/tools/toolset/dispatch/actions/approvals/action_tools/memory_promotion), `ne-govern` (pdp/guardrails/factory), `ne-contracts` (governance/runtime/knowledge/evalobs/plugins/config, v1.7.0, ADR index), `ne-mcp` (server/catalog/client), `ne-chat` (approvals/conversation/adapters), `ne-console` (App routes, flywheel/approvals views, api client), `ne-api` (steward/approvals routers), SPEC §2/§3/§4/§5 Phase-5+7 rows + both 2026-07-20 ⭐ decision-log entries. This document is the in-repo design the Phase-7 preamble requires to land and be reviewed before NE-SKILL-1 builds.*
