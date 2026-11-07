# ne-api

The governed console-backend HTTP service — the **composition root** that wires
the platform's three governance chokepoints behind OIDC and exposes them to the
frozen `ne-console` contract:

- **C6** — every tool call through the ONE `GovernedToolExecutor`.
- **C7** — every principal-scoped read through the ONE ACL-trimmed `HybridRetriever`.
- **HITL** — every Tier-2/3 approval through the real `DurableApprovalGate`.

`ne-api` is a composition root and read-only consumer of those chokepoints: it
wires them, it never reimplements or modifies them.

This package is the **NE-104a** slice: the composition root, auth spine, `/v1/me`,
chat (session open + turn-then-stream SSE), and brain search/retrieve. Ops
surfaces (approvals/audit/eval/kpi) are NE-104b; steward surfaces are NE-104c.

## Endpoints (104a)

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | Public (exact-match auth-exempt). `{"status":"ok"}`. |
| GET | `/config.json` | Public, exact-match, OUTSIDE the OIDC wrap (NE-CONSOLE-ISSUER). Runtime console OIDC config `{issuer, client_id, audience, api_base}` — issuer/audience from the SAME `IdentitySettings.oidc` the verifier trusts. `Cache-Control: no-store`. NEVER a secret (C12). |
| GET | `/v1/me` | `{id, groups}` from the verified bearer; claims never echoed. |
| POST | `/v1/chat/sessions` | `{agent_name}` → `{session_id, agent_name}`. `department` from the bearer, never the body. |
| POST | `/v1/chat/sessions/{id}/messages` | `{prompt}` → `text/event-stream` (turn-then-stream; ADDITIVE `tool` frames — see below). |
| POST | `/v1/chat/sessions/{id}/signals/thumbs` | `{up, target_ref, comment?, message_id?}` (`extra=forbid`) → `{event_id}` (NE-137 flywheel intake). Session verified via the R3 lookup (404 unknown); provenance server-built (surface=console); 503 when the flywheel store is unset. |
| POST | `/v1/chat/sessions/{id}/signals/wrong-source` | `{answer_ref, citation_ref, comment?, message_id?}` (`extra=forbid`) → `{event_id}`. As thumbs: identity NEVER on the wire; comment scrubbed+capped server-side (C12). |
| POST | `/v1/brain/search` | `{query, top_k}` → `{chunks, citations}` (acl-stripped). |
| POST | `/v1/brain/retrieve` | `{chunk_ids}` → `{chunks, citations}` (acl-stripped). |
| GET | `/v1/graph/entities` | `?q=&entity_class=&source=&assurance=&cursor=&limit=25` (limit≤100) → `{entities:[{key,entity_class,display_name,source_system,assurance}],next_cursor}`. Principal-trimmed, source-health-aware entity discovery; `q` is exact case-sensitive key or case-insensitive partial display name; no totals/facets/properties/source refs. |
| GET | `/v1/graph/facets` | no parameters → `{entity_classes:[str],source_systems:[str],truncated}`. The classes and source systems PRESENT in the calling principal's own visible slice, folded from the same ACL-trimmed scan `/v1/graph/entities` serves rows from — never the compile-time `ne-schema` registry. Empty lists are the honest empty state; no counts (the scan is bounded, `truncated` says when that bound was hit). Uncached: the answer is a function of the caller's label set. |
| POST | `/v1/graph/query` | `{anchor_keys\|anchor_citations, hops≤2, limit_nodes≤500, limit_edges≤2000}` → `{nodes, edges, truncated}` (acl-stripped; 404 `"graph tier not enabled"` when the T2 tier is off). |
| GET | `/v1/graph/neighborhood` | `?entity=<key>&hops=1` (≤4) `&limit_nodes≤500&limit_edges≤2000` → `{nodes, edges, truncated}` (acl-stripped). |
| POST | `/v1/graph/paths` | `{from, to, max_hops≤4, limit≤20}` → `{paths, truncated}` (acl-stripped). `max_hops` is further clamped to `BrainGraphSettings.max_hops` (default 3) inside `EntityGraph`. |

All `/v1/*` paths are served under the `/api` mount (the console's
`API_BASE="/api"`, same-origin). Every non-2xx body is `{"detail": "<string>"}`.

### The additive `tool` SSE event (NE-137, inline PDP lane)

The chat turn stream gains a NEW event type — `event: tool` with payload
`{type:"tool", name, tier, decision, audit_seq}` (+ `approval_id` ONLY on pending) —
emitted as each governed tool call transits the C6 `GovernedToolExecutor`. The
`token`/`citations`/`done`/`error` frames are **byte-identical** to pre-NE-137 (the
`tool` frame is a new type, never a change to the existing ones — proven by unchanged
`test_sse.py`/`test_hitl_e2e.py`). Tool frames always precede the terminal frame.

Decision semantics (derived from the REAL audit events the real PDP / gate / executor
emit — never fabricated):

| audit event | `decision` | notes |
|---|---|---|
| `tool.execute[success]` | `allow` | `tier` from the same turn's remembered `pdp.decide` (the execute event carries none — renders `null`/`—` if unknown) |
| `pdp.decide[denied]` | `deny` | the real PDP deny (SCRAM); `tool.unknown`'s own pdp.decide-denied carries it (no double card) |
| `approval.requested` | `pending` | `approval_id` = the REAL `DurableApprovalGate` request_id, actionable at `/v1/approvals/pending` |
| `tool.approval[denied]` | `deny` | a real approval rejection |
| `tool.validate[denied]` | `deny` | a real pre-gate typed-input refusal (NE-ACTION-VALIDATE-BEFORE-GATE); `tier` from the same turn's remembered `pdp.decide` |

`audit_seq` is the REAL hash-chain `seq` for the event, read back by `event_id`
(degrades to `null` — rendered `—` — when unresolvable, never invented). A
`ToolTransitObservingSink` wraps the ONE audit sink and observes these off the SAME
`pdp.decide`/`tool.validate`/`approval.requested`/`tool.approval`/`tool.execute` writes AFTER delegating
`append` to the inner sink (C6 fail-closed untouched); it publishes ONLY when a chat
turn set the per-turn transit channel. **Enqueue mode** (`NE_API_TURN_EXECUTION=enqueue`)
carries the SAME lane (NE-TURN-PROGRESS): ne-worker's `chat.turn.execute` step binds
the channel and streams each REAL transit as a `tool` `TurnFrame` WHILE the turn runs
(step-context DBOS stream writes, at-least-once; the relay's monotonic seq dedup drops
re-run overlap, and terminal `done`/`error` frames are delivered regardless of seq so
a turn that dies after N hops still shows its failure).

**Graph chamber (NE-133).** The `/v1/graph/*` routes wrap the ONE composed
`EntityGraph` (`Platform.entity_graph`) — the SAME instance inside the T2
`GraphRetriever`, built with the SAME `PrincipalExpander` as the `/v1/brain`
chunk-side `PrincipalAclFilter`, so C7 trimming holds by instance identity (a
caller never reaches a node/edge/path outside their ACLs). Wire shapes are
acl-stripped by construction (no `acl` field, the `ChunkView` discipline); the
`≤500`-node/`≤2000`-edge caps are honored honestly with a `truncated` flag (cap+1
fetch discipline — `truncated` is true only when the result actually overflowed).
When the T2 tier is disabled (`BrainGraphSettings.enabled=False`, the default) all
three answer `404 "graph tier not enabled"` — the honest degrade, not an error.

**Retrieval rerank (SPEC §3.1).** The composition root (`build_platform`) wraps the
ONE acl-trimmed `HybridRetriever` in `RetrievalPipeline(HybridRetriever,
TEIReranker(tei_rerank_url), candidate_multiplier=4)` BY DEFAULT — both the chat
knowledge tool and the `/v1/brain` routes consume this ONE reranked retriever, so
shipped retrieval matches the Checkpoint-C eval-gated pipeline. Rerank rides ABOVE
the acl-trim (C7 trims exactly once, below the pipeline — no second permission
path). Setting `NE_API_TEI_RERANK_URL=""` EXPLICITLY disables the stage (degrade to
the bare retriever with a logged warning); a set-but-external URL fails LOUD at boot
(C2). Adding `tei_rerank_url` to `ApiSettings` is an EXPECTED release-level
`config_hash` schema change (the 104b/104c precedent), PR-flagged, NOT a mid-process
mutation.

`NE_API_RETRIEVAL_TOP_K` (default `5`) sets the chat knowledge-tool `top_k`
end-to-end — it is threaded into `build_chat_bindings` AND is the exact `top_k`
`RetrievalPipeline.search` reranks down to (no second rerank knob). Because the
pipeline over-fetches `candidate_k = top_k * candidate_multiplier` (multiplier `4`)
fused candidates and reranks ALL of them through TEI in one call, raising this
setting multiplies per-search TEI rerank calls/tokens by the same multiplier. The
knowledge-tool return now opens with an honest `[showing {M} of {N} retrieved
chunks]` header (`M` = chunks returned, `N` = pre-rerank candidate count; counts-only,
prompt-level honesty per C12 — retrieval chunks are NOT grounding-gate corpus
material).

## Ops surfaces (NE-104b)

The HITL approval + audit-read + eval + KPI slice. The governed spine is REAL:
approve/reject delegate to the `DurableApprovalGate` (ne-api never constructs an
`ApprovalOutcome`, never a parallel state machine — grep-proven); audit reads are
payload-authoritative.

| Method | Path | Role | Notes |
|---|---|---|---|
| GET | `/v1/approvals/pending` | `approver_roles` | `{approvals: PendingApprovalView[]}` (payload-redacted queue — unchanged by the disclosure route below). |
| GET | `/v1/approvals/{id}/payload` | `approver_roles`, THEN the gate's named-approver policy | Owner ruling 2026-07-31 (NE-APPROVAL-PAYLOAD): the governed, per-request, audited disclosure — `{request_id, action, tier, principal_id, target_resource, description, material_parameters, reversibility}`. TWO layers, narrowest wins: (1) the same `approver_roles` pre-check as every other approvals route; (2) `DurableApprovalGate.read_payload`'s own policy — Tier-3 requires membership in `tier3_named_approvers` (both dual-flow approvers qualify); Tier<3 is decided by layer (1) alone. A role-holder outside the Tier-3 list gets **403** with a body carrying NO payload fields (`detail` names the policy, verbatim, never the data) — audited `api.approval.unauthorized` (`reason: "payload-withheld"`). Success audits `api.approval.payload_read`. 404/409 mirror approve/reject's mapping. |
| POST | `/v1/approvals/{id}/approve` | `approver_roles` | **empty body** (`extra=forbid` — a smuggled `approver` is 422). Approver = the verified bearer. `None`→`pending_second`; else `{status, approver, decided_at}`. |
| POST | `/v1/approvals/{id}/reject` | `approver_roles` | `{reason}` → `{status:"rejected", approver, decided_at}`. |
| GET | `/v1/audit/events` | `ops_roles` | `?actor&action&from&to&limit` → `{events, verification}` newest-first + a server-computed `ChainVerificationView`. |
| GET | `/v1/audit/export` | `ops_roles` | `?actor&action&from&to&limit` → streamed `text/csv`: a self-describing comment preamble (the echoed filters, CR/LF-escaped — `rows_matched`/`rows_exported`/`truncated` + the same server-computed chain verdict) followed by rows in `seq` DESC — exactly the window `/v1/audit/events` would return for the same filters, every cell sourced from the same `audit_record_view` projection (C7/C12) and formula-injection-safe (a leading `=`/`+`/`-`/`@`/tab/CR cell is apostrophe-escaped, CWE-1236). A trailing comment block (`# rows_written`/`# truncated_confirmed`/`# export_complete: true`) is appended AFTER the last data row: it is the DEFINITIVE fact (an extra keyset read anchored at the last row actually sent), not the preamble's up-front estimate — its absence marks a partial file. The CSV ROW STREAM is bounded: `audit_export_batch_rows`-sized keyset batches (strict `before_seq` cursor — no full-window row materialisation), capped at `audit_export_max_rows`; exceeding the cap is never silent (`# truncated: true` in the CSV + `X-Neutron-Export-Truncated: true` response header). The embedded verdict is a SEPARATE computation (`verify_chain` over the same `from`/`to` window) with its own cost shape: when a window is supplied the verify walk is bounded to that window's row range, but an export with NO `from`/`to` runs the FULL, unbounded chain verify (`ne.audit.verify._verify_full`), which materialises every row up to the head to compute the verdict — independent of, and unbounded by, the CSV streaming/cap discipline above. Same `_require_ops` chokepoint as every other audit route (an unentitled key gets the identical 403, audited `api.audit.unauthorized`); the success path writes nothing to the chain. |
| GET | `/v1/audit/replay` | `ops_roles` | `?trace_id&event_id` (≥1) → `{events, config_hashes, context_ids}`. No selector → 422; unknown → empty 200. |
| GET | `/v1/trace/{trace_id}` | `ops_roles` | The Logfire-style trace-assembly view (NE-167a): `{trace_id, rollup, models, nodes, verification}` — a nested typed span tree (subagent/route/model-call/redaction/knowledge/pdp/approval/guardrail; an unrecognized action renders as its raw event, §0 law 5), per-model cost/token aggregates, a run rollup, and a FULL-CHAIN (unbounded) `ChainVerificationView` verdict — a window derived from `replay()`'s own tamperable-column-filtered events cannot safely bound the verify walk (see `trace_assembly.py`) — reusing `replay()` + `verify_chain()` over the SAME `_require_ops` gate as `/v1/audit/*`. Unknown trace_id → 404. Thin-client: every cost/duration/nesting/verdict computation is server-side; only counts/deltas/identifiers/model-lane/cost cross (C7 — never ACL label values, principal labels, query text, or message content). |
| GET | `/v1/eval/runs` | any authenticated | `?suite&limit` → `{runs: EvalRunView[]}`. 503 when `eval_database_url` unset. |
| GET | `/v1/kpi/summary` | any authenticated | `{kpis: KpiEntryView[], generated_at}` from the ne-obs collectors. |
| GET | `/v1/posture` | any authenticated | The sovereignty-posture surface (NE-131): `{constraints, air_gap, egress_allowlist, model_lanes, config_hash, config_hash_history, attestation_source}`. Read-only, deployment-level facts only — no user data/ACLs/secrets (the kpi.py access precedent). |
| GET | `/v1/connect/status` | any authenticated | The connector-sync status surface (NE-132): `{connectors: [{id, kind, cursor_age_s, acl_mirror, last_sync_seq, quarantined_count}], quarantine_total, quarantine_reasons}`. Read-only, deployment-level facts only — no user data/ACLs/reason payloads. |

`/v1/posture` details:

- **C1-C12 attestation** — `constraints` is copied VERBATIM from the drift-guarded
  `ne.api.posture.CONSTRAINT_ATTESTATIONS`, a static snapshot of build-time guardian evidence (each `enforcement` == the
  criterion's title, each `evidence` == its sorted evidence). `attested` is `True`
  iff the readiness harness reported the criterion PASS when the table was last
  regenerated; that harness is not part of the open-source release. A
  constraint with no mechanical guardian is reported honestly `attested=false` /
  `mode="manual"` (§0 law 5, never fabricated) — none currently (C10 single-tenant
  was retired 2026-07-17, following from C1 self-host).
- **Effective egress** — `air_gap` + `egress_allowlist` are reported from the SAME
  boot-frozen `EgressSettings` INSTANCE the live C2 guard enforces (constructed once
  in `build_platform`, threaded through `ApiState` — NE-UI-P2-CLEANUP), ORed with the
  bare-global `ne.net.airgap.air_gap_engaged()` which the enforcing
  `EgressPolicy._check_frontier` ALSO reads per call. Never a per-request env /
  `ne.toml` re-read — a post-boot config change is reported only after a restart,
  matching the frozen `config_hash` semantics (§0 law 5 reported==enforced). Under
  AIR_GAP the frontier lane is killed, so the EFFECTIVE `egress_allowlist` is empty
  (report-only, no policy).
- **Model lanes** — `model_lanes` is read from the composed `GatewaySettings.models`
  as opaque `{model_id, provider, lane}` data (no provider SDK import, C3). The
  model-registry license/origin tags are NOT composed into ne-api and are OMITTED
  rather than fabricated. `None` gateway settings ⇒ honest empty list.
- **config_hash + history** — `config_hash` is the process's frozen hash (§0 law 5);
  `config_hash_history` is derived from the REAL audit store (one span per distinct
  `config_hash` in `ne_audit_log`, oldest-first by `MIN(seq)`; the in-force span's
  `to` is `null`). The span's `from`/`to` keys are the wire form (`from` is aliased).

`/v1/connect/status` details (all REAL from connector-sync state — §0 law 5, see
`ne.api.connect_status`):

- **Freshness** — `cursor_age_s` is the age of a source's newest `cursors.updated_at`
  read READ-ONLY from the connector-state sqlite (`ConnectorStateStore`); the CDC
  lane's freshness is the age of the latest `cdc.ingest` audit event (its push-lane
  signal). No cursor / no event ⇒ `null` (honestly unknown, never fake-fresh).
- **ACL-mirror** — `acl_mirror` is the LATEST `connector.sync` / `cdc.ingest` audit
  outcome mapped fail-closed: `success`→`ok`, anything else (`denied`/`error`, the
  AclResolutionError path)→`degraded`; no event ⇒ `null`. `last_sync_seq` is that
  event's chain seq.
- **Quarantine** — `quarantined_count` counts the write-quarantine rows attributed to
  a connector by provenance prefix, read READ-ONLY from the real
  `SqliteWriteQuarantine` store(s). `quarantine_total` is `null` when NO store is
  readable (never a fake zero). `quarantine_reasons` is the sanitized reason-CATEGORY
  ledger — each `reason` is an ALLOWLISTED fixed label (e.g. `schema-validation`,
  `unknown-table`, `acl-unresolvable`, `other`) matched from the raw reason by a
  closed signature set, never a fragment of the raw text, so no JSON path / offending
  value / record id / payload text reaches the wire (C7).
- **Honesty** — a connector with no configured credential is ABSENT; a
  configured-but-never-synced one carries all-null health; an unreadable state /
  quarantine file degrades to `null`, never a 500 or a fabricated green state.

**Ontology + verified answers (NE-135).** The governed T3 read + verified-answer
surface for the flagship UI. Every route is OIDC-gated (any authenticated principal)
and, except for the package-backed active-model projection, answers `503
ontology-disabled` when no T3 store is configured (default). The active-model route
still returns the installed generated operational model in that state and reports the
optional ontology graph as `not_loaded` and issue tracking as `unconfigured`. The
ontology READS ride the EXISTING gateway C12 `sparql` steward surface (SELECT/ASK-only,
banned-keyword refusal) over a FIXED module-constant query filtered to the
`urn:ne:ontology:*` schema graphs — no raw-SPARQL surface is ever exposed; `invoke`
routes EXCLUSIVELY through the frozen `GovernedToolExecutor` (`read.kg.<id>`, tier-0),
so PDP + audit + guardrails + C7 DatasetSpec trimming all engage.

| Method | Path | Role | Notes |
|---|---|---|---|
| GET | `/v1/ontology/model` | any authenticated | Read-only active `Neutron Enterprise Operational Schema` projection: generated concepts/fields/relationships, compiled validation rules, exact source/JSON-Schema/SHACL fingerprints, labelled standards, optional-graph state and issue/validation health. Shared model metadata is persona-invariant; an absent optional graph is not reported as zero active concepts. No query executor or management input. |
| GET | `/v1/ontology/classes` | any authenticated | BFO/CCO/neop class tree `{classes:[{iri,label,parents,source}], total, sources, shapes_hash}`, sourced ONLY from `urn:ne:ontology:*` graphs via the C12-gated `sparql`. 502 (class name only) on a backend fault. |
| GET | `/v1/ontology/shapes` | any authenticated | Compiled SHACL shapes `{shapes:[{iri,target_class,scope}], shapes_hash}` (scope `per-write`\|`global`), matching the store's `ontology_version().shapes_hash`. |
| GET | `/v1/ontology/violations` | any authenticated | The ShapeViolation quarantine feed REDACTED to `{configured, count, entries:[{quarantine_id,created_at,graph_iri,shapes_hash,violation_count,quarantined_whole,provenance}]}` — NO `delta_nt`, NO violation message strings (C12). `configured=false` when the lane is unset. |
| GET | `/v1/ontology/violations/{quarantine_id}` | `ops_roles` | Inspect-only safe issue detail: affected reference, rule reference, safe validator reason, lifecycle state and provenance. Empty/disjoint `ops_roles` fails closed and is audited; the rejected RDF payload is structurally absent. No release/revalidate action exists. |
| GET | `/v1/verified/templates` | any authenticated | `{enabled, registry_hash, templates:[{template_id,version,title,tool_name,args_schema,columns,citation_var,max_rows}]}`. `enabled=false` envelope when verified answers are off. |
| POST | `/v1/verified/invoke` | any authenticated | `{template_id, version?, args?}` (`extra=forbid`) → `{answer, replay_match}`. Executes `read.kg.<id>` ONLY through the frozen `GovernedToolExecutor` (twice, for the replay diff) → the deterministic table + per-row `urn:ne:entity` citations + `config_hash` + `ontology_version`, C7-trimmed per the caller's labels. Unknown template → audited 404 `unknown-template`; a STAGED template whose executor binding is missing → 404 `template-tool-unregistered` (a platform regression, deliberately distinct from the absent-template refusal); version mismatch → audited 409; PDP-denied → 403 `tool-denied`; guardrail-violating args → 422 `guardrail-violation`. All fail closed. |

Approve/reject error map: 403 (role pre-check OR `ApprovalPolicyError` — the
Tier-3 named-approver-list message is surfaced VERBATIM), 404 not-found, 409
already-decided/expired. Unauthorized attempts are AUDITED
(`api.approval.unauthorized` / `api.audit.unauthorized`); successful decisions
layer a redacted `api.approval.submitted` record (ids/decisions/error-classes
only — never payloads).

**R2 (audit ordering + verification window).** Rows are `seq` DESC (newest-first,
the ops-console default; the console sends only `limit`, no cursor). Every page
carries `verify_chain(from, to)` over the SAME filter window, so the verdict
describes exactly the slice reported — a tampered row inside the window flips
`verification.valid` to false, served verbatim.

**KPI window.** The current calendar month. Populated read-only from
`ne.obs.collect_window` (which calls only `replay`/`list_runs`/`read_signals`) when
the audit + eval + signal stores are all configured; a fresh deployment therefore
shows zeros (not an error) until the month accrues events. When `eval_database_url`
or `NE_FLYWHEEL_SIGNAL_DATABASE_URL` is unset, the route returns the documented
empty scaffold `{"kpis": [], "generated_at": ...}` — never an invented KPI store.
`None`-valued metrics (an instrumentation gap) are SKIPPED, not zeroed.

**R8 (roles are deployment config).** `approver_roles` / `ops_roles` are
**empty-by-default ⇒ fail closed**: a mis-deployed `ne-api` lets nobody approve or
read audit (correct, but the pilot Keycloak realm — NE-103b — must provision
matching groups). Empty roles ⇒ 403 for EVERYONE, and the gate/store is never
reached.

New env vars: `NE_API_APPROVER_ROLES`, `NE_API_OPS_ROLES`,
`NE_API_EVAL_DATABASE_URL` (empty ⇒ `/v1/eval/runs` answers 503),
`NE_FLYWHEEL_SIGNAL_DATABASE_URL` (the KPI signal seam), and the ne-obs floor
knobs `NE_OBS_MINUTES_SAVED_PER_TURN` / `NE_OBS_FLOOR_DISCOUNT`.

`NE_API_AGENT_MANIFEST_PATH` configures the PRE-008 declarative Agent catalog.
Empty means an honest empty catalog. A configured missing or malformed TOML fails
boot. `GET /v1/agents` and `GET /v1/agents/{agent_id}` expose metadata only,
apply principal visibility before pagination, and return `private, no-store`;
there is deliberately no launch endpoint or runtime-definition conversion.

PRE-009B adds the principal projection that PRE-011E registers at the production
composition root. Its routes include
`GET|POST /v1/agents/{agent_id}/conversations`, conversation get and nested Run
list, plus Agent Run list/get/events. The POST accepts no caller identity or body
fields: it mints a new blank conversation from the visible path Agent plus the
authenticated principal's exact owner and department, returns `201`, and never
reuses an existing tuple. Every read requires the exact stored owner,
creation department, visible Agent and (where nested) Conversation; an ops role
does not widen ownership, and known-hidden versus random identifiers share the
same 404 response. Conversation titles are deterministic server-time fallbacks,
never prompt-derived. Run and event wire models contain only safe metadata,
hashes, counts and opaque relations. The only displayed claim body is a bounded
source-free UTF-8 slice returned by `AgentRunStore.materialize_source_free_claim_slice`
under its exact owner/target/ref/output hash. Source-bound claims remain opaque;
the projection never decrypts or emits a locator, protected payload, prompt,
private model context, system/developer text, or chain-of-thought. All responses
are `private, no-store`, cursors have a deterministic total order, and conversation
creation is the only write on this projection; it defines no retry, cancel,
resume, launch, or mutation of an existing conversation.

**config_hash note (release-level).** This slice adds `ApiSettings.eval_database_url`
and the `"ne-obs"` entry to the composed `freeze_platform_config` inputs, so the
composed `config_hash` differs from the just-merged 104a. This is a deliberate,
release-level schema change (called out here + in the PR), NOT a mid-process
mutation — the freeze still runs ONCE at factory time before any emitter.

The single sanctioned cross-package additive edit is `ne.audit.list_records` — a
read-only, payload-authoritative filtered SELECT mirroring `replay()` (columns in
WHERE only; every returned value from the hash-covered payload; `seq` DESC; clamped
limit). No audit write path or chain code is touched.

## Steward surfaces (NE-104c)

The flywheel review surface — the six steward endpoints over the REAL NE-066
`ProposalReviewService` + `SqlAlchemyProposalStore` on ONE flywheel engine (so a
transition's review row + rejection signal + outbox + audit-chain event commit
atomically). ne-api NEVER writes `ne_proposal_reviews`, never folds-then-writes a
status, never runs a parallel state machine — the REAL service owns every
transition (grep-proven by the source-scan guardian). All six routes require
membership in `steward_roles`.

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/steward/proposals` | Non-terminal (`proposed`\|`approved`) queue, oldest-first. Live status folded via ONE aggregated query; each row rendered through `render_proposal_diff`. STRUCTURALLY omits raw `summary`/`payload` — proposal text reaches the wire ONLY inside the sanitized `artifact`. |
| POST | `/v1/steward/proposals/{id}/approve` | `{note}` (blank→`None`) → `ReviewEventView`. |
| POST | `/v1/steward/proposals/{id}/reject` | `{code: RejectionCode, note}` → `ReviewEventView`; atomically emits the `thumbs_down` rejection→signal feedback. Unknown code → 422. |
| POST | `/v1/steward/proposals/{id}/apply` | `{applied_ref, note}` → `ReviewEventView`. RECORDS a human apply only — **ne-api NEVER executes an apply**. Empty/whitespace `applied_ref` → 422; legal only from `approved` else 409. |
| GET | `/v1/steward/digest` | `?week_start` (ISO date) → `StewardDigestView`. |
| GET | `/v1/steward/latency` | → `StewardLatencyView`. |

Transition error map (order matters — `UnknownProposalError`/`InvalidTransitionError`/
`ReviewAuditError` are all `ReviewError`): `ReviewAuditError` → opaque 500 (fail
closed); `UnknownProposalError` → 404 `proposal not found`; `InvalidTransitionError`
→ 409 (library-safe message, rendered verbatim); other `ReviewError` → 422. Every
success layers a redacted `api.steward.submitted` audit record (ids/decisions/status
only — never note text); stale/unauthorized attempts audit `api.steward.stale` /
`api.steward.unauthorized`.

**Provenance (server-side only, R4).** Steward actions build `SignalProvenance`
SERVER-SIDE — the ONLY construction in ne-api (grep-proven): `principal` = the
verified bearer, `surface = SignalSurface.CONSOLE`, `session_id` = the token `sid`
claim when present else a fresh `uuid4`. **`session_id` is a server correlation id,
NOT a chat session.** The request bodies are `extra=forbid`, so a smuggled
`actor`/`surface`/`session_id` is a LOUD 422 that never reaches the service.

**Digest / latency (ne-api-computed, read-only SQL).** ne-flywheel carries no
digest/latency computation; `steward_metrics.py` implements them as READ-ONLY
`sa.select` aggregations over `ne_fix_proposals` + `ne_proposal_reviews` (no write,
no chokepoint touched). The week window is `[week_start 00:00 UTC, +7d)` — INCLUSIVE
start, EXCLUSIVE end; the default `week_start` is the current ISO-week Monday.
`digest.by_lane` OMITS zero-activity lanes (the console empty-state contract) and
orders by `FixLane` declaration order. Latency `median_wait_s` = `statistics.median`
of per-proposal waits (first review − proposal created); `p90_wait_s` = NEAREST-RANK
`sorted[ceil(0.9·n)−1]`; both `null` when `decided_count == 0`. `pending_count` =
folded-`proposed` count; `oldest_pending_age_s` = `null` when zero pending;
`generated_at` = now. Malformed `week_start` → 422.

**R8 (roles are deployment config).** `steward_roles` is **empty-by-default ⇒ fail
closed**: empty ⇒ 403 for EVERYONE (even a group-rich principal), disjoint groups ⇒
403. The check runs as a route DEPENDENCY on the write routes, BEFORE body
validation — so an unauthorized caller with a malformed body is 403+audited, never
422. The 503 store-unconfigured check runs AFTER the role check (403 wins). The
pilot Keycloak realm (NE-103b) must provision matching steward-role groups.

**R10 (queue scale).** The queue reads `read_proposals(limit=500)` (a documented
pilot-scale cap) and folds status via a SINGLE aggregated query — never a
per-proposal `status_of` N+1.

New env var: `NE_API_STEWARD_ROLES`. The steward review service + KPI signal store
share `NE_FLYWHEEL_SIGNAL_DATABASE_URL`; when unset the steward routes answer 503.

**config_hash note (release-level).** `_ensure_api_claims` now unions `'sid'` (in
addition to `'department'`) into the JIT claim allowlist so the provenance
`session_id` can carry the verified token's `sid` claim (R4). Adding `'sid'` changes
the composed `config_hash` for identical deployment config vs merged 104b — a
deliberate, release-level schema change (the 104b eval/ne-obs precedent), NOT a
mid-process mutation; the freeze still runs ONCE at factory time before any emitter.

## SCIM 2.0 provisioning (`/scim/v2`) — NE-089

A thin, first-party SCIM 2.0 provider (Users + Groups) so an enterprise IdP
(Entra ID / Okta) can PUSH user/group lifecycle into the platform, mapping to
the SAME fail-closed `Principal`/group model the `ne-identity` JIT provisioner
uses. Every write is governed and audited — it is a provisioning path, never a
bypass of the C7 fail-closed ACL model.

**Enablement.** Set `NE_API_SCIM_BEARER_TOKEN` (>=16 chars) via your
deployment's secret mechanism. **Unset (default) ⇒ the `/scim/v2` mount does
not exist** — every route 404s, not "exists but unauthenticated". A configured
secret shorter than 16 chars fails LOUD at `create_app` factory time
(`ValueError`) — never a silently-weak runtime secret. The mount sits OUTSIDE
`build_identity`'s OIDC wrap, gated by its own `hmac.compare_digest` bearer
check (applied as a router-level FastAPI dependency, so no route can be added
un-gated by omission) — see the "Two-layer mount" topology below; `/scim/v2`
is ordered AFTER `/healthz` and BEFORE the OIDC-wrapped `/api` Mount, and a
valid SCIM bearer token can **never** authenticate an `/api/*` request (the
two auth schemes are structurally disjoint — grep-proven by
`test_scim_auth.py::test_scim_token_never_authenticates_api`).

**Entra / Okta setup.** Point the IdP's SCIM tenant URL at
`https://<host>/scim/v2`, set the secret token, and — this is the important
mapping decision — **map the IdP's `externalId` to its own stable object id**
(Entra: `objectId`; Okta: the user/group `id`). The ID RULE
(`ne.api.scim.service`) computes `principal_id = externalId or userName` (users)
/ `externalId or displayName` (groups), matching the NE-042 principal-id-
opacity contract — so SCIM-provisioned ids line up with whatever a connector
(NE-044) stamps onto chunk ACLs for the same directory object. Default IdP
attribute mappings need no pruning: standard SCIM attributes outside the
supported subset (`name`, `emails`, `title`, member `type`/`$ref`, ...) are
accepted and ignored on create/replace bodies (and unknown PATCH paths are
ignored-but-audited) — only the RFC 7644 message envelopes are strict. An
inbound `meta` block is dropped wholesale, even a partial one (`meta` is
server-owned/read-only per RFC 7643 §3.1; responses always carry a
server-computed `meta`).

**Governed + audited write path.** Every mutation goes ONLY through the
EXISTING `ne.permissions.store.PrincipalStore` public API (`upsert_principals`,
`mark_removed`, `set_group_members`, `is_known`) — the SAME primitives
`PrincipalSyncService` uses. Each mutation emits ONE
`permissions.scim_provision` audit event (success, or error + re-raise) whose
`detail` carries ids/names/flags ONLY (C12 — never raw request bodies or
tokens), plus one `permissions.membership_change` event per member whose
direct group set changed — byte-shape-compatible with
`ne.permissions.sync.PrincipalSyncService`'s events. A group push filters
candidate members through `store.is_known()`: a tombstoned or never-provisioned
member id is SKIPPED (recorded as `skipped_members` in the audit detail), so a
group push can never re-grant access to a deactivated user.

**Deactivate semantics (fail-closed by construction).** `PATCH .../active=false`
maps to `store.mark_removed` — the SAME tombstone + membership-edge-cascade
primitive an Entra directory sync tombstone uses. The moment that transaction
commits, the principal is no longer store-known: `acl:tenant-all` /
`acl:public-link` / every group-derived ACL match disappears from
`PrincipalExpander.expand()` STRUCTURALLY, not via a runtime filter — proven
end-to-end in `test_scim_users.py::test_deactivate_removes_retrieval_access_e2e`
(a live `POST /api/v1/brain/search` result flips from non-empty to empty
across a SCIM deactivate). **Residual access note (the NE-042
revocation-lag model, not a defect):** a principal holding an ALREADY-verified
token whose claims carry group ids keeps matching those VERIFIED token-claim
groups until the token expires — token-claim groups are trusted independently
of the store (see `ne.permissions.expand`). SCIM deactivation closes the
store-derived paths (`acl:tenant-all`/`acl:public-link`/store group edges)
immediately; the token-TTL-bounded tail is the same documented model every
other revocation path in this platform has, not something a provisioning
protocol can or should override. `DELETE` uses the identical `mark_removed`
primitive plus a KV tombstone flag, so a deleted resource then 404s (a
deactivated-but-not-deleted resource stays visible on `GET`/list, with
`active: false`, as SCIM/Entra expect).

**Supported subset.**

| Method | Path | Notes |
|---|---|---|
| GET | `/scim/v2/ServiceProviderConfig` \| `/ResourceTypes` \| `/Schemas` | Static discovery documents. |
| POST | `/scim/v2/Users` \| `/Groups` | 201; duplicate live `userName`/`displayName` — or a resource id (`externalId`-derived) colliding with a live resource — → 409 `uniqueness` (a create never overwrites or revives an existing live resource). Standard SCIM attributes outside the supported subset (`name`, `emails`, member `type`/`$ref`, ...) are IGNORED, not rejected — default Entra/Okta mappings work unmodified. |
| GET | `/scim/v2/Users` \| `/Groups` | `?filter=<attr> eq "<value>"` (Users: `userName`/`externalId`; Groups: `displayName`/`externalId`; case-insensitive attr, case-insensitive value match) `&startIndex&count` → `ListResponse`. Excludes deleted resources. |
| GET | `/scim/v2/Users/{id}` \| `/Groups/{id}` | 404 `ScimError` when absent or deleted. |
| PUT | `/scim/v2/Users/{id}` \| `/Groups/{id}` | Full replace of identity attributes; Groups: member set is RECOMPUTED (delete-then-insert), never accreted. `active`/tombstone state is untouched by PUT — only PATCH's `active` op (or `DELETE`) changes it. |
| PATCH | `/scim/v2/Users/{id}` | `path: "active"` (or a bare `{"active": bool}` value, incl. Entra's `"True"`/`"False"` string quirk) → deactivate/reactivate; `userName`/`displayName` replace; any OTHER path is IGNORED but audited (`ignored_paths`) — rejecting unknown paths breaks real Entra provisioning flows. |
| PATCH | `/scim/v2/Groups/{id}` | `path: "members"` add/remove/replace, plus Entra's `members[value eq "<id>"]` path-filter remove form; `displayName` replace. |
| DELETE | `/scim/v2/Users/{id}` \| `/Groups/{id}` | 204; tombstones (never row-deletes) — `mark_removed` + a KV `deleted` flag. |

**Third-party SCIM library survey (adopt-rejected).** The `python-scim` org's
`scim2-models`/`scim2-server` packages are Apache-2.0 (C4-clean), but
`scim2-server` self-describes as a "lightweight SCIM2 server prototype" and
this unit's needed subset (User/Group CRUD+PATCH, `eq` filter) is small —
per the SPEC row's explicit "prefer a thin first-party SCIM handler"
directive and the connector build-vs-adopt precedent, this ships as a
hand-rolled `ne.api.scim` package instead. **Zero new third-party
dependencies** — `ne check licenses` and the per-package `NOTICE` are
unaffected by this unit.

**config_hash note (release-level).** This slice adds
`ApiSettings.scim_bearer_token` (a `SecretStr`, masked by
`compute_config_hash`), so the composed `config_hash` differs from the
just-merged 104c for identical deployment config — the exact 104b/104c
precedent above (ne-obs/eval_database_url, then the `'sid'` claim union), NOT
a mid-process mutation.

## CDC sink (`/cdc/v1`) — NE-084

An **OFF-BY-DEFAULT** sub-minute change-data-capture freshness lane. Debezium
Server's HTTP sink POSTs row-change events to `POST /cdc/v1/events`, a SEPARATE
small FastAPI app mounted OUTSIDE the OIDC wrap (Debezium is a machine caller,
not an OIDC user) and gated by its OWN **Standard-Webhooks HMAC** signature.

**Enablement.** Set `NE_API_CDC_WEBHOOK_SECRET` (a `whsec_`+base64, 24..64 raw
bytes — the Debezium HTTP-sink secret format), `NE_API_CDC_TABLES` (the governed
per-table key/content/ACL/PII map, JSON for `ne.connect.cdc.CdcSinkConfig`), and
`NE_API_CDC_QUARANTINE_PATH`. **Unset (default) ⇒ the `/cdc/v1` mount does not
exist ⇒ 404** (fail-closed — the feature is off, not "present but
unauthenticated"). A malformed secret / missing table map / missing quarantine
path fails **LOUD** at factory time (`ValueError`), never a silent runtime 401.
`cdc_webhook_secret` is a `SecretStr` (masked from the config_hash and logs).

**Fail-closed governed ingestion (C7).** Change events flow through the SAME
`ne.brain.write.KnowledgeWriter` path as the polling connectors — no new write
path, no chokepoint change. INSERT/UPDATE → `upsert` with the row's ACL derived
by the SQL-connector-identical `resolve_row_principals`; an unresolvable /
tainted / reserved-label-forging / zero-principal row → **quarantine lane, never
written** (an UPDATE whose ACL turned unresolvable also removes the stale chunk);
DELETE → `quarantine` (park-then-delete, idempotent). Unknown-table /
unsupported-op are quarantine-lane'd and ACK'd (never a 4xx that would wedge
Debezium's retry loop). `200` is returned **only after** the durable governed
apply — Debezium retries any non-2xx, so no event is ACK'd before it is
governed-written / quarantined.

**Auth (fail-closed).** Every POST is verified via the Standard-Webhooks scheme
(`hmac.compare_digest`, ±300s replay window). A missing / wrong-secret /
stale-or-future-timestamp / tampered request → `401` with a uniform
`"unauthorized"` detail (no oracle). An oversize body (>8 MiB) → `413` before any
HMAC/parse work. The webhook secret can **never** authenticate an `/api/*`
request (the mount topology is the boundary). See `deploy/compose/cdc/README.md`
for the overlay and `test_cdc_sink_api.py` for the gate proofs.

**config_hash note.** The three `NE_API_CDC_*` `ApiSettings` fields are an
EXPECTED release-level `config_hash` schema change (the NE-089
`scim_bearer_token` precedent verbatim).

## Two-layer mount

```
outer Starlette app
├── /healthz              → public route (also exempt in the middleware, belt-and-braces)
├── /config.json          → public route (runtime console OIDC config; OUTSIDE the OIDC wrap — NE-CONSOLE-ISSUER)
├── /scim/v2  → a SEPARATE bearer-gated FastAPI app — ONLY when NE_API_SCIM_BEARER_TOKEN is set
├── /cdc/v1   → a SEPARATE HMAC-gated FastAPI app — ONLY when NE_API_CDC_WEBHOOK_SECRET is set
├── /api  → OidcAuthMiddleware( inner FastAPI app carrying every /v1/* router )
└── /     → StaticFiles (console SPA, index fallback) — ONLY when serve_console_dist is set
```

**Runtime console config (`/config.json`, NE-CONSOLE-ISSUER).** The `ne-console`
SPA reads its OIDC config (`issuer`/`client_id`/`audience`/`api_base`) at RUNTIME
from this unauthenticated, same-origin, exact-match Route instead of a Vite-baked
constant — so ONE built console image self-configures its login from whatever
deployment serves it (one issuer per deployment — follows from C1). The payload's `issuer` and
`audience` are sourced from the SAME `platform.identity_settings.oidc` the OIDC
verifier trusts (ONE issuer source of truth — the console and the backend can never
disagree), `client_id` from `NE_API_CONSOLE_CLIENT_ID`, and `api_base` is the `/api`
mount path. It sits beside `/healthz` OUTSIDE the OIDC wrap and BEFORE the `/api`
Mount, so it is reachable pre-login and a `config.json` file inside the console dist
can never shadow it (Route-before-StaticFiles). It is NOT added to the middleware
exempt list — `GET /api/config.json` stays 401. It returns ONLY public OIDC discovery
values — NEVER a secret (C12; the SPA is a public PKCE client with no credential), and
`Cache-Control: no-store` so an issuer rotation is never served stale.

The inner app is wrapped by `ne.identity.build_identity` (verifier + JIT
provisioner + `OidcAuthMiddleware`). `/healthz` is the ONLY public *API* route and
is an OUTER route — never under the OIDC-wrapped mount, so there is no
middleware-bypass hole and no prefix exemption. `/scim/v2` (NE-089, see above)
is likewise OUTSIDE the OIDC wrap — it carries its OWN bearer gate and is
mounted BEFORE `/api` so it can never shadow it.

**Static console serving (NE-103b, owner dec iii).** When
`serve_console_dist` is set, `create_app` appends a `StaticFiles` mount at `/` as
the LAST route, serving the console dist same-origin with the API. Unknown paths
fall back to `index.html` (client-side SPA routing); a non-GET/HEAD (`405`) or an
unreadable-file (`401`) is propagated, never swallowed into an index 200. The
mount sits **OUTSIDE** `build_identity`'s OIDC wrap — the mount topology is the
auth boundary, and because Starlette matches routes in list order, `/healthz` and
the OIDC-guarded `/api` are matched FIRST and the static server never sees them
(so it cannot bypass auth for `/api`). A configured-but-missing dist fails LOUD at
factory time (a `ValueError`, not a silently-empty console).

## 12-step composition order (spec §3.2, run eagerly at factory time)

1. settings (+ secrets; the `_ensure_api_claims` `department`+`sid` claim union;
   the merged knowledge-tool arg policy)
2. `freeze_platform_config` — **ONCE, before any event-emitting component**
3. runtime engine (one per URL; `ensure_transactional_sqlite` + runtime schema)
4. the ONE audit sink
5. the ONE `DurableApprovalGate` (over the runtime engine, sharing the sink)
6. the NE-070H durable shared spend ledger + rate counter
7. permissions stack → `PrincipalAclFilter(expander)` (never `None`)
8. brain: embedder + sparse + chunk store → `HybridRetriever(acl_filters=acl)`
9. gateway (real `LiteLLMGateway` with `egress_guard` in prod; a seam in tests)
10. the ONE `GovernedToolExecutor` via `create_tool_executor` (sink + gate REQUIRED,
    NE-CKD1 recipe: shared ledger/counter + `AgentScopeGuardrail`)
11. the session store (over the ONE runtime engine); the ONE flywheel engine
    (steward review service + proposal store + KPI signal store share it); the eval
    read store
12. inner FastAPI app → `build_identity` OIDC wrap → two-layer mount

The ASGI lifespan only asserts the frozen `config_hash` at startup and disposes
engines at shutdown — the ordering guarantee is the eager factory-time sequence.

Steps 1–11 are now the SHARED `ne.api.platform.build_platform` recipe, called by
BOTH `ne-api` and the durable `ne-worker` (one recipe, two processes — the strongest
structural guarantee the worker cannot compose a weaker governed stack). `create_app`
keeps only the ne-api-specific surfaces (eval/flywheel/kpi read stores, `ApiState`,
routers, the OIDC wrap + mount topology, the lifespan).

## Enqueue mode — the durable turn path (NE-103c)

`ne-api` runs a chat turn one of two ways; the browser sees byte-identical SSE
either way (`ne-api` is the SOLE SSE encoder):

- **`inline`** (default, `NE_API_TURN_EXECUTION=inline`) — NE-104a's in-process
  `turn_event_stream`, unchanged. The passthrough default.
- **`enqueue`** (`NE_API_TURN_EXECUTION=enqueue`, the pilot setting) — after ALL
  pre-stream validation (department fail-closed, R3 session match, 404), `ne-api`
  mints `turn_id` server-side, builds a `TurnRequest` EXCLUSIVELY from the verified
  bearer principal + the matched session (never request input beyond the prompt),
  enqueues the durable `chat.turn` workflow via `DBOSClient`
  (`workflow_id=turn_id`), emits a redacted `api.chat.turn_enqueued` surface record
  (ids only, never the prompt), then relays the worker's frame stream through
  `relay_event_stream`.

Both modes establish the same per-turn action idempotency scope
(NE-ACTION-DETERMINISTIC-REQID): `send_message` mints `turn_id` ONCE (`turn-<uuid4>`
by default) and both `_streamer` (inline) and `_enqueue_stream` share it verbatim
(NE-SCOPE-CHAT, pinned by `tests/test_chat_action_scope.py`); enqueue's worker wraps
`session.run` in `action_scope(req.turn_id)` (`ne-worker` `workflows.py`).

**Idempotency-Key header (NE-INV-CHAT-IDEMPOTENCY, Finding B)** — an OPTIONAL
`POST /v1/chat/sessions/{id}/messages` request header, mirroring
`GovernedMcpServer`'s `TURN_META_KEY` `_meta` escape hatch (NE-SCOPE-MCP/U2) for
chat's HTTP transport. Without it, a transport-level retry (timeout, proxy retry,
double-click) mints a brand-new `turn_id` -> a brand-new `action_scope`, so a
governed write the turn issues is not deduplicated across the two turns. When the
header value matches `^[A-Za-z0-9._:-]{1,128}$` (opaque, charset-bounded — never task
text, C12), `turn_id` is derived from `sha256` over a length-prefixed (netstring-style,
`_netstring_encode`) encoding of `(session_id, principal_id, token)` — session- AND
principal-prefixed (chat sessions are per (agent, department) and shared by every
principal in that department, so a session-only prefix would let same-department
colleagues' identical tokens over-dedup each other's writes). The length-prefixed
encoding is UNAMBIGUOUS: a plain `:`-joined string would let a component containing
`:` (principal ids legitimately look like `user:ada`) collide across distinct
`(principal_id, token)` pairs and derive the same `turn_id` — `_netstring_encode`
closes that (Codex C1 finding). A retried POST with the same key reuses the SAME
`turn_id` ⇒ same
`action_scope` ⇒ NE-050's write-dedup (`action_tools.py`) collapses the repeated
governed write to one connector call, and enqueue mode re-enqueues the same DBOS
workflow id. An absent or invalid header falls back BYTE-IDENTICALLY to the `uuid4`
default (never a 4xx — fail-safe, not fail-closed, matching the MCP precedent); an
invalid token is logged by length only, never its raw value. Pinned by
`tests/test_chat_action_scope.py`, including two end-to-end proofs that drive the
dedup contract ACROSS two separate POSTs through the REAL governed executor with
full Tier-2 HITL approval on each turn (`test_same_idempotency_key_dedups_governed_write_across_posts`:
connector called once; `test_different_idempotency_keys_are_two_real_writes`: connector
called twice, no over-dedup).

The relay multiplexes keep-alives, de-dupes frames by `seq`, strips `seq` before
encoding (so browser payloads stay exactly spec §2.3), and stops at the terminal
`done`/`error`. On a `turn_timeout_s` wall-clock deadline with no terminal frame it
emits `error {"detail": "turn still running; the approval queue and session history
hold the outcome"}` and closes the browser leg **without cancelling the workflow** —
the durable turn outlives the browser (a material upgrade over the in-process
timeout, which abandoned the turn). Enqueue mode requires
`NE_API_DBOS_SYSTEM_DATABASE_URL` (empty ⇒ boot `ValueError`).

`ne-api`'s import graph stays decoration-free (import-linter forbids `ne.api`
importing `ne.worker` or `ne.runtime.approvals_durable`) so BOTH processes can safely
run `NE_RUNTIME_DURABILITY=dbos` for `config_hash` parity — see ADR-0006.

## Settings (`NE_API_*`)

| Field | Env | Purpose |
|---|---|---|
| `qdrant_url` | `NE_API_QDRANT_URL` | brain store endpoint (required) |
| `qdrant_collection` | `NE_API_QDRANT_COLLECTION` | chunk collection (required) |
| `tei_url` | `NE_API_TEI_URL` | embedder endpoint (required) |
| `tei_rerank_url` | `NE_API_TEI_RERANK_URL` | TEI reranker endpoint (default `http://tei-rerank:80`; empty ⇒ rerank disabled with logged warning) |
| `retrieval_top_k` | `NE_API_RETRIEVAL_TOP_K` | chat knowledge-tool `top_k`, threaded into the binding + pipeline rerank (default `5`; `candidate_k = top_k * 4` multiplies TEI rerank calls) |
| `approver_roles` | `NE_API_APPROVER_ROLES` | HITL role pre-check (104b) |
| `steward_roles` | `NE_API_STEWARD_ROLES` | steward role pre-check (104c) |
| `ops_roles` | `NE_API_OPS_ROLES` | audit read access (104b) |
| `chat_agents` | `NE_API_CHAT_AGENTS` | createSession allowlist (default `console-chat`) |
| `serve_console_dist` | `NE_API_SERVE_CONSOLE_DIST` | console dist served same-origin at `/` (SPA index fallback, OUTSIDE the OIDC wrap; empty ⇒ no mount; NE-103b) |
| `console_client_id` | `NE_API_CONSOLE_CLIENT_ID` | PUBLIC SPA/PKCE client_id advertised by `/config.json` (default `ne-console`; NEVER a secret; NE-CONSOLE-ISSUER) |
| `sse_keepalive_s` | `NE_API_SSE_KEEPALIVE_S` | keep-alive cadence (default 15) |
| `turn_timeout_s` | `NE_API_TURN_TIMEOUT_S` | turn timeout (R9, default 120) |
| `turn_execution` | `NE_API_TURN_EXECUTION` | `inline` (default) or `enqueue` — the durable ne-worker path (NE-103c) |
| `dbos_system_database_url` | `NE_API_DBOS_SYSTEM_DATABASE_URL` | the `ne_dbos` system DB for the `DBOSClient` (required in enqueue mode) |
| `bind_host` / `bind_port` | `NE_API_BIND_HOST` / `_BIND_PORT` | fail-safe local bind |
| `scim_bearer_token` | `NE_API_SCIM_BEARER_TOKEN` | SCIM provisioning bearer secret (NE-089); empty ⇒ `/scim/v2` absent; `SecretStr`, masked in `config_hash`; must be ≥16 chars when set |

Everything else rides the existing module settings: `NE_RUNTIME_SESSION_DATABASE_URL`,
`NE_IDENTITY_*` (issuer/audience REQUIRED), `NE_GATEWAY_*`, `NE_AUDIT_*`,
`NE_PERMISSIONS_*`, `NE_SECRETS_*`, `NE_EVAL_*`, `NE_FLYWHEEL_*`, OPA URL via
`NE_GOVERN_OPA_URL`.

### AMENDMENT A1 — department claim propagation

Chat sessions derive `department` from `principal.claims['department']`, but the
JIT provisioner's claim allowlist historically dropped it. A1 makes the allowlist
configurable via `IdentitySettings.principal_claims`
(`NE_IDENTITY_PRINCIPAL_CLAIMS`), defaulting byte-identically to the historic
`("email","preferred_username","name")`. `ne-api`'s composition root ALWAYS unions
`"department"` into that allowlist (it is the surface that needs it). Claims still
propagate ONLY from the JWKS-verified token, never request input; a missing
department claim stays fail-closed (chat 422). The pilot Keycloak realm (NE-103b)
must map `department` into a token claim.

## Decided R-items

- **R3 — session lookup by id.** The store exposes only `open_session` (race-free
  get-or-create). The messages route re-opens per configured agent for the
  caller's department and matches `session_id`; a foreign department's id can
  never match (no existence leak) — a miss is a 404.
- **R6 — governed search citations are relayed.** The production
  `read.knowledge.search` binding captures the exact ACL-trimmed `Citation` rows
  returned by successful retrieval. Chat emits zero or one ordered `citations`
  frame after `token` and before `done`; empty, denied, errored, timed-out, and
  no-search turns emit none. The wire may enrich a citation with optional
  `source_url` only when the exact served chunk carries a canonical connector-
  owned locator whose source identity matches the citation; absent, revoked,
  malformed, unmatched, or conflicting locators remain absent.
- **R7 — turn-then-stream.** `run()` is atomic: open the stream, keep-alive while
  it runs, then emit ONE `token` (full output) + a terminal `done`. Both carry
  `event:` AND `type:`. True token streaming is a later ne-runtime seam.
- **R9 — turn timeout.** A turn that outruns `turn_timeout_s` is cancelled and
  emits a terminal `error` frame; the pending approval row survives and stays
  actionable from the approvals queue.

The chat tool name is **`read.knowledge.search`** (not the spec's illustrative
`knowledge.search`): the real `tiers.rego` taxonomy denies an unclassified action
prefix, and `read.*` is tier 0. 104b/c and the console agent menu must stay
consistent with this name.

**Provisional-context recording (`NE-T2X-5-RECORDER-WIRE`, design §6.1/§6.3).**
`read.knowledge.search` is the ONLY handler in the governed tool menu that can
retrieve assurance-labeled graph elements, so it is the ONE production caller
of `ne.runtime.provisional.current_provisional_recorder()`. After each
retrieval, the handler derives that call's provisional contribution — count +
opaque `llm:<sha16>:<ref>` evidence refs (C12: never fact text/displays) —
from the ONE pinned `ne.api.wire.provisional_summary` traversal (via the
shared `graph_path_views` domain→view mapper), and records it into the active
turn scope with the deployment's configured
`BrainGraphSettings.extraction_mode` (threaded explicitly from
`build_platform`, never guessed). Recording happens even though the current
tool-text rendering does not surface graph paths — under-recording would be
the A-P1-5 risk this seam exists to close, so the binding is fail-toward-
flagging. This feeds `ne.runtime.middleware`'s `context_provisional` PDP-input
threading, which is what lets the §6.3 tier escalation (Tier-1 `write.
internal.*` → Tier-2 require-approval) actually fire. See
`tests/test_provisional_recorder_wiring.py` for the value-discriminating
extracted-vs-backbone proof through the real app.

**`read.trace`** (NE-167a, agent-native parity) is a second always-on tier-0
governed tool riding the SAME frozen `GovernedToolExecutor`: it takes one
required `trace_id` (identifier-kind arg) and returns the SAME
`TraceAssemblyView` JSON as `GET /v1/trace/{trace_id}`. The handler mirrors the
route's `ops_roles` gate in-handler (group-disjoint or empty `ops_roles` ⇒
refused — surfaced to the caller as a `ToolExecutionError`, audited as
`tool.execute[error]` by the C6 executor) rather than at the PDP layer, since
`read.*` classifies tier 0 by the Rego taxonomy regardless of role. An agent
with the ops role can therefore read the same assembled trace a human ops
console renders — agent-native parity by construction.

## Flowise add-on endpoints (NE-088)

The optional Flowise add-on (see `deploy/compose/flowise/README.md`, OFF BY
DEFAULT) reaches the platform's governed tool surface through two additive routes.
A Flowise flow is **just another OIDC-authenticated caller** of the ONE
`GovernedToolExecutor` — this router holds no handler and creates no second
governance path, so a flow-initiated tool call gets the SAME `PDP decide →
stricter-of tier HITL → audit → execute` as a first-party chat turn (proven by
`tests/test_flowise_parity.py`).

- **`GET /v1/flowise/tools`** — the governed tool catalog: specs only
  (name/tier/input_schema), never handlers. No audit on the read.
- **`POST /v1/flowise/tools/execute`** — `{ "tool": ..., "args": {...} }` runs the
  tool through the governed executor. Same PDP decision doc, same audit shape,
  same tier-2/3 HITL blocking as a chat turn. Error semantics:
  - **403** — PDP-denied (`ToolDeniedError`/`GuardrailViolation`) or approval
    refused (`ApprovalNotGrantedError`); there is no bypass.
  - **404** — unknown tool (still audited as `tool.unknown`).
  - **422** — invalid typed input for a write action (`ActionValidationError`,
    refused PRE-gate; already audited as `tool.validate[denied]`, never consumes
    a human decision).
  - **502** — handler failure (the raw handler error is never leaked).

Every call also emits one redacted `api.flowise.execute` surface record
(`surface=flowise`); the load-bearing governed events (`pdp.decide` /
`tool.validate` / `tool.approval` / `tool.execute` / `tool.unknown`) come from
the executor unchanged, which is what the parity suite compares.

## Optional ReBAC wiring (NE-087 O-4)

`build_platform` optionally activates the OpenFGA fine-grained-sharing module. It is
**OFF by default** (`RebacSettings.enabled = False`): the C7 filter and the composed
`config_hash` are byte-identical to a build without it. When
`NE_REBAC_ENABLED=true` (plus a pinned `NE_REBAC_STORE_ID` /
`NE_REBAC_AUTHORIZATION_MODEL_ID`, `NE_REBAC_API_URL` = the in-VPC
`http://openfga:8080`, and the preshared `NE_REBAC_API_TOKEN`), the composition root
injects `OpenFgaSharedDocumentResolver` as the single C7 union seam
(`PrincipalAclFilter(shared_docs=...)`), stamps the settings into the frozen hash,
retains the confined client on `Platform.rebac_client`, and closes it in the
lifespan shutdown (the ne-worker root does the same in `_shutdown_platform`). The
env is supplied by `ne up --profile rebac` — see `deploy/compose/pilot/README.md`.

## Tier-2 graph retrieval (NE-BRAIN-T2-WIRING)

`build_platform` optionally composes the Brain **Tier-2** knowledge-graph retriever
(`GraphRetriever`/`EntityGraph` from `ne-brain-graph`) over the reranked T1 pipeline,
so a deployment with persistent AGE and governed graph producers can compose the
full brain ladder (T1 hybrid → T2 graph → T3 ontology/verified-answers). It is **OFF
by default**: with the tier disabled the composed
retriever is the plain `RetrievalPipeline`, the `config_hash` is byte-identical to a
build without it (the settings are stamped into the frozen hash **only** when
enabled), and every `/v1/brain/*` response is identical except for the additive
`graph_paths: []` key.

**Enablement** is driven by `BrainGraphSettings`, which reads the `NE_BRAIN_GRAPH_` env
prefix (the per-subsystem convention): `NE_BRAIN_GRAPH_ENABLED` (the enable flag),
`NE_BRAIN_GRAPH_DATABASE_URL` (the Apache-AGE Postgres DSN), and
`NE_BRAIN_GRAPH_GRAPH_NAME`.

When enabled:

* **F2 shared-expander invariant (C7)**: the SAME `PrincipalExpander` instance that
  backs the chunk retriever's `PrincipalAclFilter` is injected into `EntityGraph`, so
  graph reads and chunk reads resolve the identical label set for a principal — the
  graph tier can never over-return relative to the caller's ACLs (grep-guarded in
  `test_no_bypass`, proven by the composed-platform provable-trimming test).
* **`graph_paths` wire shape**: each path is `{hops: [{subject, edge_kind, obj,
  source_system, source_ref}]}` where `subject`/`obj` are `{entity_class, entity_key,
  display}`. Built field-by-field (never `model_dump`), acl-free by construction, and
  trimmed-by-construction (only ever from `EntityGraph` output).
* **Native backend only**: `graph_expansion_backend` must be `native`; the LightRAG
  sidecar (blocking venv build, out-of-process) is **refused at boot** with a clear
  `ValueError` — this unit ships no sidecar host.
* **Degrade-to-T1+T3**: when the tier is off the composition logs `graph tier (T2)
  disabled` at INFO (off is the shipped default). The `database_url` DSN is **never**
  logged (it may carry credentials); only `graph_name` is. A graph-store failure on
  the live route surfaces as HTTP 502 whose `detail` is the exception class name only.

`AgeGraphStore` connects lazily, so an unreachable AGE Postgres surfaces at the first
query (a 502), not at boot — keeping API liveness independent of the graph store.

## Demo seams (`ne demo`)

`ne.api.demo_seams` is the SINGLE source of truth for `ne demo`'s DEFAULT-OFF
governed exhibits — each is a plain `ApiSettings` boolean, stamped into the
frozen `config_hash` **only** when on, so a non-demo deployment's hash is
byte-identical to today. The demo overlay (`tools/ne-cli/src/ne/cli/demo/live.py`)
flips these flags identically for both `ne-api` and `ne-worker` (§6.2/Q5 parity).

* **T3 ontology lift** (`demo_ontology_lift`) — runs the real boot
  `PropertyGraphLift` from the composed graph store into the T3
  `ValidatingTripleStore` so a verified-answer template returns a real,
  deterministic table.
* **Tier-2 HITL write-back** (`demo_writeback_tool`) — registers the
  `write.external.demo.send` `ToolBinding` through the frozen C6 executor so
  the write-back arc can deny-then-HITL-approve-then-execute LIVE.

The stock `ne demo` overlay deliberately leaves T2 disabled. It does not stage a
static graph or a localhost AGE substitute. `/v1/graph/*` therefore returns the
honest `404 {"detail":"graph tier not enabled"}` until an operator composes
persistent AGE and governed source producers.
* **NE-168 governed multi-lane delegation** (`demo_delegation`) — composes a
  `GovernedSubAgentDispatcher` (via `ne.runtime.factory.create_subagent_dispatcher`,
  sharing the SAME ledger/counter/scope-rail as the platform's tool executor)
  with three `AgentDefinition`s: `triage` and `reason` route to the
  `private-vllm` lane (real, governed model calls); `adjudicate` routes to the
  `frontier-api` lane, which the NE-161 RUNTIME GATE
  (`allow_frontier_subagent_routing`, left at its default `False`) refuses
  BEFORE any model call — a governed **DENY** exhibit, never a live frontier
  egress. The dispatcher is exposed on `ApiState.demo_dispatcher` (`None` when
  the flag is off) and driven by an ops-gated `POST /v1/demo/delegate`, which
  dispatches all three legs inside ONE `run_trace_scope` so every
  `subagent.route` audit event they emit shares a single `trace_id` — readable
  back via `GET /v1/trace/{trace_id}` (NE-167a). Absence of the flag is an
  honest 404, never a silently-degraded route. The response carries
  identifiers/enums/bools only (never model output text or task prompts).
* **NE-GA-G7 grounded-hallucination** (`demo_grounded_hallucination`) — wraps
  the composed model gateway in `DemoGroundedHallucinationGateway` (over
  `_default_gateway`, so pass-through traffic keeps the pre-egress redactor +
  egress guard). For a `[grounded-hallucination]`-marked grounded-analyst
  prompt it scripts one governed accounts fetch, then a canned ungrounded
  draft — so the demo's refuse leg fires LIVE against the real pod model, not
  only in the CI fixture arc. The REAL, unmodified `ne.govern.grounding` gate
  still runs on the fabricated draft and refuses it (audited
  `grounding.ungrounded-claim`, withheld-notice output): the seam only
  supplies the fabrication, proving the guarantee, never bypassing it.
  Requires `OntologySettings.grounded_agent_enabled` (else a boot
  `ValueError`). Unmarked traffic is delegated to the wrapped gateway
  byte-transparently.

## Governed write actions (NE-ACTION-WIRE)

`ApiSettings.write_actions_enabled` (env `NE_API_WRITE_ACTIONS_ENABLED`, default
`False`) registers the 3 NE-050 reference governed write-back actions as
standard `ToolBinding`s in the SAME frozen C6 executor `build_platform` already
composes (chat/trace/verified/demo/skills — never a second executor), composed
via `build_action_bindings` (the U3-blessed seam; the earlier `create_write_actions`
factory no longer exists) with a dedup store riding the platform's runtime
engine (the same engine the `DurableApprovalGate` already uses). Adding the
field is an EXPECTED release-level `config_hash` schema change (the
`demo_writeback_tool` precedent); enabling the flag additionally moves the hash
via the `guardrails.tools` merge below (an honest, test-pinned stamp).

`ApiSettings.write_action_tools` (env `NE_API_WRITE_ACTION_TOOLS`, default `[]`)
is the canonical per-action selector. It accepts only the exact three names in
the table below and must be sorted and duplicate-free. The legacy boolean and
the selector are mutually exclusive. Each selected action loads, hashes, and
constructs only its own connector leg; unselected SQL, Salesforce, or M365
ambient settings are never inspected. The legacy boolean retains its exact
all-three behavior for existing deployments.

The 3 tools and their declared tiers:

| Tool | Tier | Source system |
| --- | --- | --- |
| `write.internal.crm.note` | Tier-2 | Salesforce (`SalesforceWriteClient`) |
| `write.external.ticket.update` | Tier-3 | SQL (`SqlWriteClient`, `tickets` table) |
| `draft.email.create` | Tier-2 | M365 Graph (`GraphWriteClient`, draft only — never sent) |

Each action's `*_ARG_POLICY` (`ne.runtime.reference_actions`) is merged into
`GovernSettings.guardrails.tools` BEFORE `freeze_platform_config` — an
unmerged action is fail-closed-rejected as a toolargs violation before any
model judgment runs (C12).

**Exposure**: every non-grounded chat agent sees the selected write tools, gated
PURELY by the existing PDP + approval-tier HITL system — the Tier-2/3 approval
gate IS the access control; there is no separate allowlist mechanism. The
grounded agent stays structurally excluded via its existing
`ToolsetScopedExecutor` wrap (verified-reads-only) — unaffected by this flag.

**Per-leg connector settings** (each leg constructs independently from its own
env-driven `NeSettings`; production never touches `AppSeams.write_action_clients`,
which exists purely for tests):

* SQL / ticket leg — `NE_SQL_*` (`SqlConnectorSettings`); live and
  exercisable today (writes to the deployment's own SQL).
* Salesforce / CRM-note leg — `NE_SALESFORCE_*` (`SalesforceConnectorSettings`).
* M365 / email-draft leg — `NE_M365_*` (`M365ConnectorSettings`).

**Fail-loud boot contract**: `NE_API_WRITE_ACTIONS_ENABLED=true` with ANY leg's
settings unconfigured (a `pydantic.ValidationError` on that leg's `NeSettings`)
is ONE boot `ValueError` naming every missing leg + its env prefix — never a
silently-absent tool, never a runtime surprise. A leg that is configured but
INVALID (a bad SQL dialect/host, a C2-rejected endpoint) raises its own
already-loud, already-named error instead.

The per-action selector applies the same contract only to selected legs. A
selection containing `draft.email.create` additionally requires at least one
exact `NE_API_EMAIL_DRAFT_ALLOWED_MAILBOXES` entry. Mailboxes are normalized,
config-hashed, and checked by an in-process wrapper before Graph token
acquisition or request construction. Graph must then return a non-empty message
id and `isDraft=true`; otherwise the action fails closed.

Live Salesforce/M365 writes additionally require the owner's Step 9 tenant
write-scope grants — creds can be configured (boot passes) while the
tenant's write scope is still pending; a runtime write then fails loud with
the real API error. `demo_writeback_tool` (above) is a separate, unrelated
no-egress demo stand-in and is untouched by this flag.

## Running

```
python -m ne.api      # uvicorn factory over ne.api.app:app_factory
```

Live-stack acceptance (real Keycloak issuer, OPA sidecar, Qdrant/TEI, Postgres,
real console login) is parked to NE-103b/NE-077 (fixture-first per plan §0.6).

## Governed verified-answer-template curation (NE-ONTOLOGY-FLYWHEEL)

Two steward routes close the flywheel loop for the T3 verified-answer layer. Both
answer `403` (non-steward, audited, before body validation) → `503` (flywheel
unconfigured) → `409` ("verified answers are not enabled on this deployment", when
`ApiState.verified_curation is None`) before doing any work.

- `POST /v1/steward/proposals/verified-template` — body `{template_toml, summary}`.
  The TOML is validated by the `QueryTemplate` load gate at propose-time; a failing
  template is `422` with a denied audit event (error class only, never the TOML) and
  writes **no** proposal row (fail-closed). Success creates a
  `verified_answer_candidate` proposal on the `ontology` lane through the NE-066 gate.
- `POST /v1/steward/proposals/{proposal_id}/apply-verified-template` — body
  `{note}`. Requires NE-066 status `approved` (else `409`, fail-closed; wrong-kind →
  `409`, unknown proposal → `404`). Persists the template to the governed
  `ne_verified_templates` store, re-fingerprints, audits the projected `config_hash`
  delta, and folds the review row to `applied`. Returns the review row + the
  old/new `config_hash` and `registry_hash` + `restart_required: true`. The
  `applied_ref` is server-derived — never client-supplied.

The composition (`platform.py`) attaches a `VerifiedCurationContext` (boot dir + a
`config_hash` projection closure) to the `Platform` only when verified answers are
enabled; default-off the `config_hash` and tool menu stay byte-identical. The
projection recomputes BOTH hashed inputs a HITL template touches — the template
registry fingerprint AND the `ne-govern` guardrail tool-policy map — so the audited
delta equals the next restart's frozen hash exactly (activation is
**restart-to-activate**; see `packages/ne-brain-ontology/README.md`).

## Grounded-agent composition wiring (NE-GA-G5)

`build_platform` builds a `GroundingPolicy` immediately next to the NE-VAF-U4
`verified_router` — the same root legally importing both `ne.runtime` and
`ne.brain_ontology` (C3) — guarded on `OntologySettings.grounded_agent_enabled`
(requires verified answers enabled; a grounded-on/verified-off boot fails loud).
The policy's granted tools are the verified-registry `read.kg.*` names ONLY, its
`render_fallback` wraps `render_answer_text`, and its `guardrail_engine`/`pdp`
ride the SAME platform sink as every other governed path. Both chat surfaces
(`routers/chat.py`'s `_open` and, identically, `ne-worker`'s
`execute_turn_step`) thread `grounding=` **only when** the request's
`agent_name == OntologySettings.grounded_agent_name` — every other agent is
byte-identically unaffected. The SSE `done` frame gains an additive `grounded`
key mirroring `verified` (absent when `None`); `ne-worker` inherits the same
`GroundingPolicy` via `build_platform` (one recipe, two processes) so an
enqueued grounded turn's structural provenance is identical to the inline leg.
