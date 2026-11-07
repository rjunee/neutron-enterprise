# NE-083 — Temporal-Graph Agent Memory Upgrade: Spike Design + Backend Decision Memo

**Status:** spike memo (build-ready design + backend recommendation)
**Unit:** NE-083 — "Temporal-graph agent memory upgrade: bi-temporal KG behind
MemoryProvider (Graphiti+Neo4j CE out-of-process OR AGE driver contribution — spike first);
migrates NE-037 episodic store, no interface change."
**Hard constraint:** the frozen `ne.contracts.runtime.MemoryProvider` Protocol
(`packages/ne-contracts/src/ne/contracts/runtime.py:155-167`) does not change. Callers see a
drop-in backend swap.
**Related:** NE-037 (agent memory v1, merged PR #45) · NE-081/082 (Brain T2 on
Apache AGE) · SPEC §2 C1-C12 · the Graphiti-AGE-driver
contribution decision (NE-083 spike), which no longer sits in SPEC §12 — it was extracted
in PR #518 and resolved in `docs/architecture/ne083-graphiti-age-decision-record.md`.

---

## 0. Executive summary

Upgrade agent memory from the NE-037 flat episodic store to a **bi-temporal knowledge graph**:
facts carry both *valid time* (when true in the world) and *transaction time* (when the system
learned/superseded them), enabling point-in-time recall ("what did the agent believe on date X"),
non-destructive supersession, and relationship-aware recall — all behind the existing
`MemoryProvider` seam that `AgentMemory` already implements
(`packages/ne-runtime/src/ne/runtime/memory_store.py:311-321`, whose docstring explicitly
anticipates this swap).

**Backend recommendation: Option B — adopt Graphiti (Apache-2.0) and contribute an Apache AGE
driver, running agent memory on the SAME AGE-on-Postgres infrastructure Tier-2 (NE-081) deploys.**
Option A (Graphiti + Neo4j CE out-of-process) is C4-*permitted* by the letter of the gate
(which names exactly this case) and is retained as (i) the dev-spike vehicle and (ii) the
fallback if the AGE driver spike hits a hard AGE-capability wall. The full tradeoff is §3.4.

Key structural insight from reading NE-037: the episodic store is **already primitively
bi-temporal at the row level** — every event stores `occurred_at` (valid-time instant) AND
`recorded_at` (transaction time) (`memory_store.py:75-76`) and the store is append-only by
construction (`memory_store.py:12-16`). NE-083 does not have to retrofit temporality onto the
log; it has to add **fact-level** temporal semantics (valid intervals, supersession, entity
structure, point-in-time queries, semantic recall) as a *derived graph index over the existing
log*. That framing drives the whole migration design (§4): the episodic table stays the durable
source of truth and the audit-grade promotion path (`memory_promotion.py`) is untouched.

---

## 1. The bi-temporal model

### 1.1 What the two time axes buy agent memory

| Axis | Meaning | NE mapping |
|---|---|---|
| **Valid time** | when the fact was true in the world | NE-037 `MemoryEvent.occurred_at` (contracts `runtime.py:78`); graph facts get an interval `valid_from`/`valid_to` |
| **Transaction time** | when the system recorded / superseded its belief | NE-037 `recorded_at` column (`memory_store.py:76`); graph facts get `created_at`/`expired_at` |

Capabilities this unlocks (none exist in v1's keyword recall, `memory_store.py:347-371`):

1. **Point-in-time belief queries** — "what did the agent believe on date X" =
   facts where `created_at <= X` AND (`expired_at` is null OR `expired_at > X`), optionally
   intersected with a valid-time window. This is the audit/debug question ("why did the agent
   decide that on July 3rd?") that a flat log can only answer by replaying everything.
2. **Non-destructive supersession** — when a new learning contradicts an old fact
   ("the steward now approves vendor X" vs. an earlier rejection), the old fact edge gets
   `invalid_at`/`expired_at` stamped; **nothing is deleted or updated in place**. This preserves
   the NE-037 audit-grade append-only invariant (`memory_store.py:12-16`) at the fact level:
   supersession is new information, exactly as `promotion-approved` is an appended event rather
   than a row mutation.
3. **Current-belief recall by default** — recall filters to facts not yet invalidated, so an
   agent stops resurfacing stale learnings (v1 returns every historical match newest-first).
4. **Entity-centric memory** — decisions/outcomes/verdicts about the *same* customer, system, or
   proposal link to one entity node, so "what do I know about ACME's invoice process" traverses
   relationships instead of substring-matching event content.

### 1.2 Graphiti's native fit

Graphiti (getzep/graphiti, Apache-2.0) implements exactly this model: episodic
nodes (raw inputs, never mutated) → LLM-extracted entity nodes + relation edges, where every edge
carries the four timestamps (`valid_at`/`invalid_at` for the world, `created_at`/`expired_at` for
the system), incremental contradiction-driven edge invalidation (no graph recomputation), and
hybrid semantic + BM25 + graph-distance retrieval. It was selected for precisely this slot in the
stack design ("what the agents have learned about this company over time, as distinct from what
the documents say"). Building this machinery natively
is the last-resort path under the build-vs-adopt principle (SPEC §11; owner directive 2026-07-04)
— see §3.5 for the honest native-option analysis.

### 1.3 Mapping onto the frozen MemoryProvider operations

The Protocol (`ne/contracts/runtime.py:155-167`) is two methods; both map cleanly:

```
async def append(self, event: MemoryEvent) -> None
```
→ (1) append the episodic row to `ne_agent_memory_events` exactly as today — this INSERT remains
the durable, transactional source of truth and keeps every NE-037 invariant; then (2) enqueue the
event for graph ingestion (a Graphiti "episode" named by `event_id`, `reference_time =
occurred_at`, group-partitioned by `agent_id`). Extraction is asynchronous and retryable from the
log (see §4.2) — `append()`'s success semantics do not change.

```
async def recall(self, query, *, since=None, until=None, limit=20) -> Sequence[MemoryEvent]
```
→ hybrid search over the agent's graph partition: semantic + BM25 over fact edges and episodic
nodes, `since`/`until` interpreted against **valid time** (same axis v1 filters on —
`occurred_at`, `memory_store.py:230-235` — so window semantics are preserved), defaulting to
current-belief (uninvalidated) facts. Results project to frozen `MemoryEvent`s
(kind/content/occurred_at/provenance — `runtime.py:73-80`): episodic hits project exactly as
today (`memory_store.py:115-122`); fact hits project with `kind="learned-fact"`,
`content=` the fact text, `occurred_at=valid_from` (interval → representative instant; §6 Q3),
`provenance=` a structured pointer to source episode ids (which are event_ids, so facts remain
traceable to audited appends).

**Richer temporal surface without touching the Protocol:** NE-037 set the precedent that
backend-specific richness lives on the concrete store, not the frozen seam — `recall_shared` and
`get` exist only on `SqlAlchemyEpisodicStore` (`memory_store.py:264-308`), and `AgentMemory.recall`
narrows to the Protocol. The temporal backend does the same: `recall_as_of(query, *, as_of)`,
`fact_history(entity)`, `recall_shared(...)` are methods on the new store class; callers holding
the Protocol see no change; the contract-conformance test
(`packages/ne-runtime/tests/test_memory_provider_contract.py:34-43`) passes unmodified against
the new facade.

---

## 2. Architecture (both options share this shape)

```
ne-runtime (in-process, unchanged callers)
 ├── AgentMemory facade — implements frozen MemoryProvider (memory_store.py:311)
 │     └── NEW: TemporalAgentMemory facade, same Protocol, selected by factory config
 ├── SqlAlchemyEpisodicStore — UNCHANGED source-of-truth log + promotion substrate
 └── MemoryPromoter — UNTOUCHED (memory_promotion.py) — curation gate preserved
        │ frozen KnowledgeWrite seam (unchanged)
        ▼
ne-memory-graph service (NEW, out-of-process, adopt-via-isolation)
 ├── Graphiti (Apache-2.0) — hash-pinned isolated venv/container
 ├── LLM: OpenAI-compat endpoint → ne-gateway route (C3) or in-VPC vLLM (AIR_GAP lane)
 ├── Embedder: TEI (in-VPC, C3-exempt but registry-tagged)
 └── Graph backend:
       Option A: Neo4j CE 5.x container (GPLv3, out-of-process, Bolt/Apache-2.0 driver)
       Option B: Apache AGE on the customer's platform Postgres (shared with NE-081 T2)
```

Graphiti runs **out-of-process in both options**, for reasons independent of the backend choice:
its dependency closure (openai SDK, backend drivers, tenacity/numpy stack) must not enter the
platform runtime `uv.lock` — the openai import would trip C3's import-linter rule ("no
module imports a provider SDK directly") and the closure bloats the sovereign deployment.
This is the established adopt-via-isolation pattern (SPEC §11 heavy-closure principle; Docling,
linkml, vLLM, OPA all landed this way). `ne-runtime` talks to the service over a thin internal
HTTP/IPC client living behind the `TemporalAgentMemory` facade; the service is loopback-bound in
the Compose pilot exactly like qdrant/tei (`deploy/compose/pilot/README.md:46-50`).

**Degradation mode:** if the graph service is down, `append()` still succeeds (episodic row is
the transaction; graph ingestion is queued) and `recall()` falls back to the v1 keyword scan over
the episodic store — the platform never loses memory writes and never fails agent runs on a
memory-index outage. This fallback is cheap because the v1 path is retained wholesale.

---

## 3. The backend decision (the spike)

### 3.1 Option A — Graphiti + Neo4j CE, out-of-process

**Capabilities.** Works today with zero driver work: Neo4j is Graphiti's primary backend
(full Cypher, native fulltext + vector indexes). Fastest path to a running bi-temporal memory.

**The C4 license analysis — honest version.** Neo4j Community Edition is **GPLv3**. The C4 gate
does NOT ban GPL; it classifies it "**out-of-process only** = GPL (e.g. Neo4j CE as a separate
service over Apache-2.0 drivers)" — the gate literally names this exact deployment.
Does the mitigation hold legally? Yes:

- GPLv3 copyleft attaches to *derivative works* that are *conveyed*. NE code speaking Bolt to a
  separate Neo4j process through the Apache-2.0 `neo4j` Python driver is arm's-length client use,
  not derivation — the same posture as any app using a GPL database server.
- Shipping the Neo4j CE container image inside the customer bundle is GPL "mere aggregation";
  the obligation is source availability *for Neo4j CE itself* (upstream pointer + written offer
  in the deploy NOTICE, and vendored source in the NE-086 air-gap bundle where upstream isn't
  reachable). No copyleft reaches first-party code (LICENSE: Apache-2.0 — every `ne-*` file
  header).
- The license-scanner CI gate (NE-002) scans `uv.lock`; Neo4j CE never appears in it (it is a
  container), and the Python-side deps (`graphiti-core` Apache-2.0, `neo4j` driver Apache-2.0)
  are clean — subject to the standard per-version re-verification at build time (the
  dependency-intake criteria; "every Trident loop re-verifies").

So Option A is **not license-blocked**. The honest case against it is operational and strategic:

1. **Every customer runs a second database.** C10 as it then stood (single-tenant invariant; that C10 was REMOVED 2026-07-17 and the slot refilled with audit-log integrity) means single-tenant deployments —
   Neo4j CE ships into *every* engagement, air-gapped ones included (NE-086 bundle grows by a
   JVM database image).
2. **CE is deliberately hobbled**: no HA/clustering, no RBAC, no online backup — the research
   brief flags exactly this ("no HA/RBAC/online backup; EE always customer-purchased"). Agent memory is a durability-critical store; putting
   it on the one component in the stack with no online backup while the platform Postgres has
   NE-071 "Velero + DB-native backups with RPO/RTO per profile" is a real
   operational regression. Neo4j EE fixes it but is customer-purchased-only.
3. **Two graph stores in one deployment.** T2 knowledge graph is on AGE (the default;
   NE-081). Memory-on-Neo4j means two graph engines, two query dialects, two ops runbooks —
   against the Postgres-gravity rationale that made AGE the default in the first place
   (`spec-research-2026-07-03.md:27`).
4. **GPLv3-in-the-bundle is a sales/legal-review friction line item** for regulated customers
   even when compliant — nonzero cost every engagement, forever.

### 3.2 Option B — Graphiti + contributed Apache AGE driver

**Shape.** Graphiti's backend abstraction is a driver interface (added upstream when FalkorDB /
Neptune / Kuzu support landed). Contribute (or initially vendor) an **AGE driver**: Graphiti runs
against Apache AGE (Apache-2.0) on the *same customer Postgres* that NE-081's T2
`GraphStore` adapter targets (`ne/contracts/knowledge.py:152-160` — "openCypher subset per
Apache AGE"). This is the option the research brief pre-identified as "a strategic OSS option"
(`spec-research-2026-07-03.md:47`) and SPEC §12 carried as the open decision.

**What must actually be built** (spike must size each):

1. **Driver core** — session/execute-query implementation over `psycopg` +
   AGE's `cypher()` SQL wrapper; agtype ↔ Python marshalling. Moderate, well-trodden
   (the `apache-age-python` driver, Apache-2.0, is a reference/possible base).
2. **Cypher-dialect gap** — AGE speaks an openCypher *subset* (the T2 contract
   already constrains to it). Graphiti's per-backend query variants mechanism exists (FalkorDB
   and Neptune already diverge from Neo4j syntax), so the work is writing the AGE variant set,
   not inventing the mechanism. The spike enumerates Graphiti's query inventory against AGE
   capabilities — this is the **primary feasibility gate**.
3. **Fulltext search shim** — AGE has no fulltext index. But AGE-on-Postgres means the driver can
   delegate to **Postgres `tsvector`** over a side table (or expression index) keyed by graph
   element id. Precedent exists upstream: Graphiti's Neptune driver already delegates fulltext to
   an external search index (OpenSearch) — an AGE driver delegating to the host Postgres is
   *architecturally cleaner* (same database, same transaction domain).
4. **Vector-similarity shim** — likewise no graph-native vector index; delegate to **pgvector**
   (already the NE-091 alternate slot) in the same Postgres.
5. **Conformance** — run Graphiti's driver/integration test suite against AGE via testcontainers
   (the NE-085 dual-backend testcontainers pattern is the in-repo precedent).

**Why it wins strategically:**

- **C4 clean everywhere**: Apache-2.0 across Graphiti, AGE, driver, pgvector. Nothing to
  mitigate, no NOTICE machinery for GPL, no legal-review friction.
- **One graph infrastructure**: memory shares AGE + Postgres with T2 (NE-081) — one engine, one
  backup/HA story (the customer's Postgres, already first-class in NE-071 RPO/RTO), one ops
  runbook, and single-tenant deployments stay lean (C10, air-gap bundle unchanged).
- **Durability parity**: agent memory inherits platform-Postgres backup/HA instead of
  CE-Neo4j's nothing.
- **Build-vs-adopt aligned**: this is "adopt + contribute", not rebuild — Graphiti's temporal
  logic, extraction pipeline, and search orchestration are adopted intact; only the storage
  driver is written (the memory `build-vs-adopt-default-adopt` records isolation/contribution as
  the preferred pattern over native rebuilds).
- **Upstream-risk is bounded**: if the PR stalls upstream, the driver lives as a vendored
  out-of-tree driver in the `ne-memory-graph` service (Graphiti's driver interface is public);
  contribution is upside (community maintenance), not a dependency.

**Costs/risks:** real engineering effort (estimate at spike exit; expect low-weeks for
driver+shims, plus conformance hardening); AGE dialect gaps could force query rewrites deep in
Graphiti (mitigated by the upstream per-backend variant mechanism, but the spike must prove the
worst queries); Graphiti upstream API churn on the driver interface; soft dependency on NE-081
landing the AGE deployment profile (NE-081 is "buildable NOW" and prioritized;
`packages/ne-brain-graph` is currently a stub, `src/ne/brain_graph/__init__.py`).

### 3.3 Cross-cutting (both options): C2 / C3 / C12

- **C2**: both backends are fully in-network containers — no egress, no phone-home
  (Neo4j CE telemetry disabled; AGE has none). Graphiti's *ingestion* makes LLM + embedding
  calls: LLM via an OpenAI-compat endpoint pointed at the gateway-governed route (frontier lane)
  or at in-VPC vLLM (AIR_GAP lane — C2's open-weight-only mode keeps working, with an expected
  extraction-quality delta to note in the eval, §6 Q5). Embeddings via TEI (in-VPC, C3-exempt,
  registry-tagged). **Integration prerequisite:** an OpenAI-compatible HTTP surface for
  out-of-process consumers — `ModelGateway` is currently an in-process Protocol
  (`ne/contracts/models.py:69-77`); either ne-serve exposes a thin gateway-backed
  `/v1/chat/completions` facade or the memory service embeds a mini gateway client. Spike decides
  (§6 Q2).
- **C3**: no provider SDK enters platform modules — Graphiti and its openai import
  are quarantined in the isolated service (§2).
- **C12/C6**: memory recall is runtime plumbing, not an agent tool — no new PDP surface. Graph
  ingestion runs no tools. Promotion remains the only path by which memory content becomes
  retrievable knowledge, and it is untouched (§4.3).

### 3.4 Recommendation

**Choose Option B (Graphiti + contributed AGE driver) as the build target.** Run the spike in two
short lanes: (A) stand up Graphiti+Neo4j CE in dev to prove the `TemporalAgentMemory` facade,
the MemoryEvent↔episode mapping, and recall-quality vs. v1 — this de-risks everything
backend-independent and is throwaway-cheap; (B) the AGE-driver feasibility probe (query-inventory
vs. AGE dialect, tsvector/pgvector shim prototypes, one end-to-end episode→fact→invalidation→
point-in-time-recall pass on AGE).

**Decision rule at spike exit:** if lane B shows no *hard* AGE blocker (a Graphiti query
unexpressible even via the driver-variant mechanism + Postgres shims), build B. If a hard blocker
surfaces, fall back to A — which is C4-permitted (out-of-process), just operationally
worse — and record the AGE gap upstream. The tradeoff stated plainly: **A buys ~weeks of schedule
by permanently costing every deployment a second, backup-less, GPLv3 database; B spends bounded
engineering once to keep the fleet on one Apache-2.0 Postgres-anchored graph stack.** For a
platform whose pitch is sovereign, lean, single-tenant deployments, B is the right spend. The
facade work transfers between options wholesale, so the fallback costs almost nothing extra.

### 3.5 Why not native (no Graphiti at all)?

Honest check against the build-vs-adopt principle: the *storage* half is temptingly small — the
episodic table already has both time axes, and a bare bi-temporal fact table on Postgres is a
weekend. But the *value* of NE-083 is Graphiti's hard 20%: LLM extraction into a coherent entity
graph, contradiction detection driving edge invalidation, and tuned hybrid retrieval. Rebuilding
those is exactly the "reflexive reimplementation of a trusted lib" the owner directive forbids;
adopting only the storage schema while rebuilding the pipeline forfeits the tested behavior that
motivated the SPEC's Graphiti naming ("Adopt"). Native
is recorded as rejected unless BOTH spike lanes fail (which would reopen the design, not silently
downgrade it).

---

## 4. Migration: NE-037 episodic store → temporal KG

### 4.1 Principle: the log is the truth; the graph is a derived index

`ne_agent_memory_events` (`memory_store.py:67-81`) is **kept, unchanged, as the durable
source of truth**. The graph is a rebuildable projection. This gives migration-by-replay,
zero-risk rollback (drop the graph, fall back to v1 recall), and preserves every NE-037 invariant
(append-only `memory_store.py:12-16`; per-agent isolation `memory_store.py:18-23`; SQLite/Postgres
parity for CI) without re-proving them.

### 4.2 Steps

1. **Backfill**: replay each agent's episodic events in `occurred_at` order through graph
   ingestion (`group_id = agent_id`; episode name = `event_id`; `reference_time = occurred_at`).
   Idempotent by episode name → resumable. Runs per-agent, throttled (each event is an LLM
   extraction call — cost/time scales with corpus; see §6 Q5).
2. **Dual-write**: `TemporalAgentMemory.append()` = episodic INSERT (unchanged transaction) +
   enqueue graph ingestion keyed by `event_id`. Ingestion failure never fails `append()`; a
   catch-up worker replays the gap from the log (watermark = last ingested `recorded_at` per
   agent). No new queue infrastructure is required — the log *is* the queue.
3. **Shadow-read validation**: for a burn-in window, run v1 keyword recall and graph recall
   side-by-side; assert the graph path never loses an event the keyword path surfaces (facts may
   legitimately *add* results); emit divergence metrics.
4. **Cutover**: factory flag flips `create_agent_memory` (`ne/runtime/factory.py:496-506`) to
   return the temporal facade. Rollback = flip back; the episodic path never stopped working.
5. **Contract proof**: `test_memory_provider_contract.py` runs against the new facade unmodified
   (`_as_provider` pyright conformance, `test_memory_provider_contract.py:34-43`), plus a swap
   test per C5 (this slot now has ≥2 implementations, so the conformance kit gains
   a memory-provider suite run against both).

### 4.3 Promotion / curation gate: structurally unchanged

`MemoryPromoter` (`ne/runtime/memory_promotion.py:76-332`) reads learned-fact events **by
event_id from `SqlAlchemyEpisodicStore`** (`memory_promotion.py:115, 239`) and writes through the
frozen `KnowledgeWrite` seam with `learned-by-agent` provenance under the audit-first /
unwind-on-audit-failure ordering (`memory_promotion.py:39-51`). Because the episodic store
remains the truth, **not one line of the promoter changes** — no governance bypass is even
constructible from the graph side, since the graph service holds no `KnowledgeWrite` reference
(the same structural property as `propose()` never touching `self._writer`,
`memory_promotion.py:16-18`).

**New capability, same gate:** graph-*synthesized* facts (a fact edge distilled across several
episodes) that an agent wants promoted are first **appended as a new `learned-fact` episodic
event** (content = fact text, provenance = source episode event_ids), then proposed via the
existing `propose()` → distinct-curator `promote()` flow. Synthesis output thereby enters the
same audited, review-gated pipeline as any other learning — promotion never reads from the graph
directly.

---

## 5. Governance invariants (checklist)

| Invariant | How preserved |
|---|---|
| No interface change | Frozen `MemoryProvider` untouched (`runtime.py:155-167`); new facade passes the existing conformance test; richer temporal ops live on the store class per NE-037 precedent (`memory_store.py:264-308`) |
| Memory reads permission-scoped | Facade binds one `agent_id` exactly as `AgentMemory` does (`memory_store.py:323-334`); graph queries are `group_id`-partitioned to that agent; cross-agent existence never leaks (parity with `memory_store.py:293-308`); sharing remains the explicit `shared_with` allowlist path only |
| Promotion review-gated | `MemoryPromoter` unchanged; distinct-curator rule (`memory_promotion.py:222-227`), audit-first ordering, quarantine unwind all intact; synthesized facts route through append→propose→promote (§4.3) |
| Append-only / no destructive overwrite | Episodic table unchanged; graph supersession = `invalid_at`/`expired_at` stamps, never deletes |
| C2 offline | Both backends in-network; AIR_GAP lane = vLLM extraction + TEI embeddings; no egress from the memory service |
| C4 | Option B: all Apache-2.0, gate-clean. Option A fallback: GPL out-of-process per C4 + NOTICE/source-offer + air-gap source vendoring, tracked as a deployment-license line item |
| C6/C12 | No new agent-tool surface; ingestion executes no tools; audit events for backfill/cutover emitted via the standard sink |
| C5 swap test | Memory slot gains a two-implementation conformance suite at cutover |

---

## 6. Decomposition (spike-then-build) and open questions

### Units

| Unit | Content | Deps | Size |
|---|---|---|---|
| **NE-083s (spike)** | Lane A: dev Graphiti+Neo4j CE — facade shape, event↔episode mapping, recall-quality probe. Lane B: AGE driver feasibility — Graphiti query inventory vs. AGE dialect, tsvector/pgvector shims, end-to-end bi-temporal pass on AGE. **Deliverable: backend decision + effort sizing + this memo updated** (closes SPEC §12 "Graphiti-AGE-driver contribution decision", SPEC §12) | none hard; NE-081's AGE deploy profile helps lane B (else spike stands up its own AGE container) | M |
| **NE-083b (build 1)** | `ne-memory-graph` isolated service + `TemporalAgentMemory` facade + factory flag + degradation fallback + backfill/dual-write/shadow-read + conformance & swap tests | NE-083s; gateway OpenAI-compat facade (Q2) | L |
| **NE-083c (build 2, Option B path)** | AGE driver hardened + upstream PR (vendored until merged) + testcontainers conformance vs. Neo4j reference | NE-083s decision; NE-081 (shared AGE infra) | M-L |

### Open questions / risks

1. **[Option A residual] GPLv3 optics** — compliant but review-friction per engagement; if the
   fallback fires, add to the §12 lawyer-session list alongside the ELv2 items (SPEC §12).
2. **Gateway HTTP surface** — out-of-process Graphiti needs an OpenAI-compat endpoint;
   `ModelGateway` is an in-process Protocol today (`ne/contracts/models.py:69-77`). Small
   ne-serve facade vs. in-service gateway client — spike decides; either way calls stay
   gateway-governed (C3) and AIR_GAP-accounted.
3. **MemoryEvent is instant-shaped, facts are intervals** — the frozen `occurred_at:
   datetime` (`runtime.py:78`) forces interval facts to project a representative instant
   (`valid_from`) on recall; full intervals are visible only via store-level extras. Acceptable
   (Protocol semantics preserved); flag to callers in the facade docstring.
4. **Recall semantics shift** — v1 is exact substring/kind match (`memory_store.py:361-371`);
   graph recall is semantic. The Protocol pins shape, not ranking; the shadow-read burn-in (§4.2)
   plus a small recall-quality eval (ne-eval fixture set) is the honest guard against silent
   regressions. Also inherit the NE-070 note: `recall(limit=0)` off-by-one —
   fix in the new facade rather than porting the bug.
5. **Extraction cost + AIR_GAP quality** — backfill and every append incur LLM extraction; large
   memories = real token spend under guardrail caps, and open-weight extraction quality in
   AIR_GAP mode is unmeasured. Spike lane A produces per-event cost numbers + an open-weight
   extraction sample; do not silently downgrade this stage (extraction quality is
   reasoning-critical — flag model routing plainly in the build unit).
6. **[Option B] Upstream acceptance** — mitigated by vendoring; the driver must be written to
   Graphiti's public driver interface at a pinned version, with the pin re-verified per Trident
   loop.
7. **Migration correctness** — replay determinism is *not* guaranteed (LLM extraction is
   stochastic); correctness is defined as (a) episodic log intact (trivially true — untouched),
   (b) shadow-read no-loss property, (c) fact→source-event provenance always resolvable. The
   graph is an index, not a ledger — this definition must be stated in the build unit's
   acceptance criteria to keep audit expectations honest.
8. **Per-agent isolation strength in Graphiti** — `group_id` partitioning must be verified as a
   hard filter in *every* search/traversal path (spike test: adversarial cross-group query), or
   the facade adds a defense-in-depth post-filter; NE-037's isolation bar is
   existence-never-leaks (`memory_store.py:18-23`).
