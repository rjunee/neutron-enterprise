# ne-api — Service Specification (build-ready)

**Status:** SPEC for the unbuilt `ne-api` service (topology row: "console backend,
KnowledgeRead/Write, gateway, identity middleware | stateless, horizontal-scalable").
The NE-103 architect pass confirmed `ne-api` was never assigned a
work-item and never built; the console SPA (`packages/ne-console`) is finished and
was built against a MOCKED ~19-endpoint contract. This document turns that frozen
consumer contract plus the merged governed libraries into a producer spec a build
agent can implement without guessing.

**Security framing:** `ne-api` is the primary HTTP surface exposing the platform's
three governance chokepoints — **C6** (every tool call through `GovernedToolExecutor`),
**C7** (every principal-scoped read through the ACL-trimmed `HybridRetriever`), and
**HITL** (every Tier-2/3 approval through the real `DurableApprovalGate`). `ne-api`
is a **composition root and read-only consumer** of those chokepoints: it wires
them, it never reimplements or modifies them. Getting that wiring right — and
proving no bypass exists — is the whole point of this unit.

---

## AMENDMENT A1 (2026-07-09) — department claim propagation (NE-104a build finding, orchestrator-authorized)

**Gap found during NE-104a build:** chat sessions derive `department` from `principal.claims["department"]` (§1.2 #2; enforced fail-closed at `ne-runtime/agent.py:103`), but the JIT provisioner copies only a HARDCODED whitelist `_CLAIM_WHITELIST = ("email","preferred_username","name")` into `Principal.claims` for human console users (`ne-identity/provisioning.py:40,90-93`) — so a verified-bearer `department` claim is silently DROPPED, and every chat endpoint would 422 / the E2E test 403. The spec assumed the claim propagates; it does not.

**Resolution (authorized, additive, security-preserving):** make the JIT claim-propagation allowlist **configurable via `IdentitySettings`** — a new field (e.g. `principal_claims: tuple[str, ...]`) **defaulting to the current `("email","preferred_username","name")`** so un-configured deployments behave EXACTLY as today; a deployment that wants department-scoped chat adds `"department"`. This mirrors the EXISTING configurable `groups_claim` (`ne-identity/config.py:59`) — department is an IdP-asserted attribute exactly like groups. Security posture is preserved: claims propagate ONLY from the JWKS-verified IdP token (never request input); a missing/unconfigured department claim stays fail-closed (422/403). This small ne-identity change is added to NE-104a's mayTouch (analogous to the `ne.audit.list_records` blessing for 104b). **The pilot Keycloak realm (NE-103b) must map department into a token claim** for the console login flow.

---

## 0. The frozen consumer contract

Everything the console sends and expects is defined in four files. They are the
contract; `ne-api` conforms to them, not the other way around:

| File | Defines |
|---|---|
| `packages/ne-console/src/api/client.ts` | every endpoint: method, path, body/query, response type; the 401→re-auth path (client.ts:129-131, 187-190); the `{"detail": string}` error-body convention (client.ts:137-148) |
| `packages/ne-console/src/api/types.ts` | all wire types (snake_case; ISO-8601 UTC datetimes as strings) |
| `packages/ne-console/src/api/sse.ts` | the SSE frame grammar the chat stream must emit |
| `packages/ne-console/src/api/config.ts` | `API_BASE` defaults to the **relative** path `/api` (config.ts:13) — same-origin deployment; all endpoint paths below are relative to it |

Auth on every request: `Authorization: Bearer <OIDC access token>` only — never a
query-param token (client.ts:6-8; enforced server-side by
`ne-identity/middleware.py:37-58`). The console's OIDC client is Authorization-Code
+ PKCE S256, public client `ne-console`, dev issuer
`http://localhost:8081/realms/ne-dev`, scope `openid profile email`
(`packages/ne-console/src/auth/oidcConfig.ts:13-23`). A 401 from ANY endpoint —
JSON or SSE — triggers exactly one `onUnauthorized()` → `auth.signinRedirect()`
(`useApiClient.ts:11-23`).

**Security crux inherited from the console tests** (these are executable consumer
assertions the server MUST honor):

- `approve` POSTs an **empty body** — no approver/principal/actor field
  (`client.ts:217-225`; asserted in `client.test.ts:146` "…with the bearer and NO
  approver body" and `client.test.ts:164`).
- Steward actions carry only `note` / `code` / `applied_ref` — never
  actor/principal/surface/session (`client.ts:277-306`; asserted
  `client.test.ts:261, 291, 318, 345`: `expect(body).not.toHaveProperty("actor")`).
- The Tier-3 policy 403's human-readable reason surfaces through the error body's
  `detail` (`client.test.ts:402-416` expects `"approver is not in the Tier-3
  named-approver list"` — the exact string raised by
  `ne-runtime/approvals.py:185-188`).
- `ChunkView` deliberately omits `acl` (`types.ts:10-13, 26-32`) — the server
  trims and strips; the client never sees ACLs.
- `StewardProposalView` deliberately omits raw `summary`/`payload`
  (`types.ts:246-265`) — proposal-carried free text reaches the console ONLY
  inside the server-sanitized `artifact`.

The identical server-side precedent is `ne-chat`'s `ApprovalActionService`
(`packages/ne-chat/src/ne/chat/approvals.py:10-13`): "The APPROVER IDENTITY is
ALWAYS the mapped `principal.id` — never the raw Slack user id." For `ne-api` the
mapping step is even shorter: the verified bearer IS the principal.

---

## 1. Endpoint-by-endpoint specification

All paths below are mounted under the API base (`/api` same-origin; see §3 for the
mount strategy). 19 routes total: 18 console endpoints + `/healthz`.

Global conventions:

- **Principal**: every governed route reads the principal ONLY via
  `ne.identity.middleware.current_principal` (middleware.py:124-133), which reads
  `request.state.principal` attached by `OidcAuthMiddleware` from the verified
  bearer (middleware.py:98-103). No route ever reads an identity field from a
  request body or query string. There is no other principal source in the process.
- **Error body**: every non-2xx response is `{"detail": "<string>"}` — the console
  parses only a string `detail` (client.ts:137-148). FastAPI's default 422
  validation body (`detail` as a list) MUST be replaced by a
  `RequestValidationError` exception handler that flattens to a single string.
- **401**: produced exclusively by the middleware (missing/invalid bearer;
  middleware.py:86-94, body shape middleware.py:105-121, `WWW-Authenticate: Bearer`).
  Routes themselves never mint 401 except `current_principal`'s defensive check.
- **Datetimes**: serialize as ISO-8601 UTC strings (pydantic `datetime` JSON mode);
  all library reads already normalize to tz-aware UTC (`approvals.py:89-91`,
  `review.py:201-203`, `proposals.py:171-174`).

### 1.1 Identity

| # | Endpoint | Spec |
|---|---|---|
| 1 | **GET `/v1/me`** | Console: `client.ts:154-156` → `PrincipalView {id, groups}` (`types.ts:53-56`). **Delegates to:** nothing — it reflects the middleware-attached `Principal` (`ne-contracts/types.py:32-37`: `id`, `groups`, `claims`) back as `{id, groups: list(groups)}`. Do NOT echo `claims` (they may carry department/roles internals the console doesn't need). **Governance:** principal from bearer only. **Errors:** 401 only. |

### 1.2 Chat (C6 surface)

| # | Endpoint | Spec |
|---|---|---|
| 2 | **POST `/v1/chat/sessions`** | Console: `client.ts:158-163`, body `{agent_name: string}` (the console currently always sends `"console-chat"`, `ChatPage.tsx:26`) → `ChatSession {session_id, agent_name}` (`types.ts:47-50`). **Delegates to:** `ne.runtime.factory.create_department_session(agent_name, department, store=…, gateway=…, executor=…, settings=…)` (factory.py:447-467), which calls `SqlAlchemySessionStore.open_session(agent_name, department)` (store.py:145-157 — get-or-create, race-free under multi-worker Postgres). **`department` is NEVER client-supplied**: derive it as `principal.claims.get("department", "")` from the verified bearer — the same claim `DepartmentAgentSession.run` later enforces (agent.py:103-108). Validate `agent_name` against a configured allowlist (`ApiSettings.chat_agents`, §3.4) → unknown agent = 422. **Governance:** the session is constructed over THE process-wide `GovernedToolExecutor` (C6) — see §3.2. **Errors:** 401; 422 unknown `agent_name` or missing department claim (fail closed: a principal with no `department` claim cannot open a session — mirror the mismatch error class). |
| 3 | **POST `/v1/chat/sessions/{session_id}/messages`** (SSE) | Console: `client.ts:165-193`, body `{prompt: string}`, response `text/event-stream`. Full streaming spec in §2. **Delegates to:** `DepartmentAgentSession.run(prompt, principal=principal)` (agent.py:96-138). Resolve the session by re-deriving `(agent_name, department)`: keep an in-process map is WRONG for stateless horizontal scaling — instead re-open via `store.open_session(agent_name, department)` for each configured agent and match `session_id`, or (simpler, recommended) add the lookup described in §7-R3. If the path `session_id` does not belong to `(any configured agent, principal's department)` → 404 (this is the ownership check; a principal can never send into another department's session because `run()` re-checks `claims['department']` fail-closed, agent.py:103-108 — the 404 just avoids leaking existence). **Governance:** C6 — every tool call inside the turn goes through `GovernedToolExecutor.execute` by closure construction (agent.py:14-18, runner-built tools agent.py:149-153); unknown model-emitted tools route through the same executor (agent.py:143-147); audit-append failure kills the run (agent.py:28-31). **Errors:** 401 (middleware, pre-stream — sse.ts:59-61 surfaces it, client.ts:187-190 re-auths); 404 unknown session; 422 empty prompt; `SessionDepartmentMismatchError` → 403 pre-stream; errors after streaming starts → `event: error` frame (§2.4). |

### 1.3 Brain (C7 surface)

| # | Endpoint | Spec |
|---|---|---|
| 4 | **POST `/v1/brain/search`** | Console: `client.ts:195-200`, body `{query: string, top_k: number}` (default 10) → `RetrievalResult {chunks: ScoredChunk[], citations: Citation[]}` (`types.ts:35-44`). **Delegates to:** `HybridRetriever.search(query, principal=principal, top_k=top_k)` (`ne-brain/retriever.py:178-210`). NO caller `filters` parameter is exposed over HTTP — the console never sends one, so `ne-api` never forwards one; the ONLY filter in play is the ACL filter. **Governance (C7):** the retriever is constructed with `acl_filters=PrincipalAclFilter(expander)` (`ne-brain/trimming.py:92-107` — fail-closed, never `None`, never `{}`), which AND-combines into the store query (retriever.py:199-207). **Response mapping:** convert `ne.contracts.types.Chunk` → `ChunkView` by copying `chunk_id/document_id/text/metadata` and **dropping `acl`** (types.ts:26-32; server Chunk carries ACL labels, `ne-contracts/types.py:47-52`). Clamp `top_k` to `1..=50` (422 outside). **Errors:** 401; 422 empty query / bad top_k; 502 on TEI/Qdrant backend failure (detail: error class only, never backend URLs). |
| 5 | **POST `/v1/brain/retrieve`** | Console: `client.ts:202-207`, body `{chunk_ids: string[]}` → `RetrievalResult`. **Delegates to:** `HybridRetriever.retrieve(chunk_ids, principal=principal)` (retriever.py:212-243) — the ACL-consistent fetch-by-id: non-empty ACL mapping routes through `QdrantChunkStore.get_filtered` (retriever.py:232-239; store lacking filtered fetch fails CLOSED with `NotImplementedError` → 500, never an unscoped fallback). A chunk the principal cannot see is silently absent from the result (not an error) — trimming, not authorization failure. Same `ChunkView` acl-stripping as #4. Cap `chunk_ids` length (e.g. 100) → 422. **Errors:** 401; 422 empty/oversized list. |

Citations: `ne.contracts.types.Citation` (types.py:64-70) has NO `source_url`;
the console's optional `source_url` (`types.ts:21-23`) is a server-side
enrichment `ne-api` MAY add later — v1 omits it (the field is optional). See §7-R6.

### 1.4 Approvals (HITL surface — NE-062)

| # | Endpoint | Spec |
|---|---|---|
| 6 | **GET `/v1/approvals/pending`** | Console: `client.ts:211-215` → `{approvals: PendingApprovalView[]}` (`types.ts:90-99`). **Delegates to:** `DurableApprovalGate.list_pending()` (`ne-runtime/approvals.py:297-312`) — returns `PendingApproval` models that mirror the wire type field-for-field (request_id, action, tier, principal_id, requested_at, sla_deadline, expires_at, escalated; approvals.py:74-87). The read-model is deliberately payload-REDACTED (types.ts:85-89) — do not join in payloads. **Governance:** authenticated read; viewing the queue requires membership in `ApiSettings.approver_roles` (403 otherwise) — the queue names principals and actions, ops-sensitive. **Errors:** 401; 403 non-approver. |
| 7 | **POST `/v1/approvals/{request_id}/approve`** | Console: `client.ts:217-225`, body `{}` (empty — SECURITY CRUX) → `ApprovalActionResult {status, approver?, decided_at?}` (`types.ts:108-112`). **Delegates to:** `DurableApprovalGate.approve(request_id, approver=principal.id)` (approvals.py:153-221) — the REAL NE-035 gate; `ne-api` NEVER constructs an `ApprovalOutcome` or runs a parallel state machine (mirror `ne-chat/approvals.py:3-9`). **MANDATORY fail-closed pre-check first** (pattern: `ApprovalActionService.act` step 2, `ne-chat/approvals.py:112-119`): an EMPTY configured `approver_roles` OR `set(principal.groups).isdisjoint(approver_roles)` → 403, gate NEVER called, audit `api.approval.unauthorized` (needed because the gate itself accepts any non-empty approver string for tier<3, approvals.py:221 — the role pre-check is the tier-2 authorization layer, exactly as in ne-chat). **Result mapping** (mirror `ne-chat/approvals.py:144-158`): gate returns `None` → Tier-3 first check recorded → `{status: "pending_second"}` (types.ts:103-106); an `ApprovalOutcome` → `{status: outcome.status.value, approver: outcome.approver, decided_at: outcome.decided_at}`. **Errors:** 401; 403 role pre-check OR `ApprovalPolicyError` (surface `str(exc)` as detail — the console renders the Tier-3 list message verbatim, client.test.ts:402-416); 404 `ApprovalRequestNotFoundError`; 409 `ApprovalAlreadyDecidedError` (incl. expiry, approvals.py:171-180). |
| 8 | **POST `/v1/approvals/{request_id}/reject`** | Console: `client.ts:227-234`, body `{reason: string}` (may be `""`) → `ApprovalActionResult`. **Delegates to:** `DurableApprovalGate.reject(request_id, approver=principal.id, reason=reason)` (approvals.py:223-262) after the SAME role pre-check as #7. Returns `{status: "rejected", approver, decided_at}`. **Errors:** as #7. |

Every gate transition self-audits with the frozen config_hash and fails closed on
sink failure (approvals.py:468-497) — `ne-api` adds NOTHING to that path. `ne-api`
additionally emits its own surface-origin audit record `api.approval.submitted` /
`api.approval.unauthorized` (redacted: ids/decisions/error classes only), layered
on top of the gate's events exactly as ne-chat layers `chat.approval.submitted`
(`ne-chat/approvals.py:186-205`).

### 1.5 Audit (NE-062)

| # | Endpoint | Spec |
|---|---|---|
| 9 | **GET `/v1/audit/events`** | Console: `client.ts:236-245`, query `actor?`, `action?`, `from?`, `to?` (ISO-8601), `limit?` → `AuditEventsPage {events: AuditRecordView[], verification: ChainVerificationView}` (`types.ts:154-157`). **Delegates to:** (a) a payload-authoritative filtered SELECT over `ne_audit_log` — **this read helper does not exist yet in ne-audit** (finding §7-R1): `ne.audit.replay.replay()` (replay.py:76-137) supports trace/event/resource/time but NOT actor/action; the columns exist (`store.py` table; used in WHERE clauses at replay.py:124-132) and `AuditRecord` (store.py:108-126) mirrors `AuditRecordView` (types.ts:120-136) field-for-field. Add `ne.audit.list_records(engine, *, actor=None, action=None, start=None, end=None, limit=100)` following replay's discipline: columns ONLY in WHERE, every returned value from the hash-covered payload via `_record_from_row` (replay.py:11-19). (b) `ne.audit.verify.verify_chain(engine, start=…, end=…)` (verify.py:400-410) → `ChainVerification` (verify.py:114) mirrors `ChainVerificationView` (types.ts:143-150). The verdict ships WITH every page (types.ts:152-153) — server-computed, console renders verbatim. Default/clamp `limit` (default 100, max 500); order `seq` DESC (newest first) — see §7-R2. **Governance:** authenticated; restrict to `ApiSettings.ops_roles` (the audit log names actors/actions platform-wide) → 403 otherwise. **Errors:** 401; 403; 422 malformed from/to. |
| 10 | **GET `/v1/audit/replay`** | Console: `client.ts:247-253`, query `trace_id?` and/or `event_id?` (at least one — client always sends one) → `ReplayResultView {events, config_hashes, context_ids}` (`types.ts:160-164`). **Delegates to:** `ne.audit.replay.replay(engine, trace_id=…, event_id=…)` (replay.py:76-137) → `ReplayResult` (replay.py:43-49) mirrors the wire type exactly (tuples → arrays). No selector → 422 (mirrors replay's `ValueError`, replay.py:96-100); an unknown selector returns the EMPTY result with 200 (replay.py:107-108 — not an error). **Governance:** as #9 (ops_roles). |

### 1.6 Eval + KPI (NE-062)

| # | Endpoint | Spec |
|---|---|---|
| 11 | **GET `/v1/eval/runs`** | Console: `client.ts:255-263`, query `suite?`, `limit?` → `{runs: EvalRunView[]}`. **Delegates to:** `ne.eval.store.EvalRunStore.list_runs(suite=suite, limit=limit)` (`ne-eval/store.py:118-127` — newest-first, already ordered server-side). `EvalRunRecord` (store.py:47-65) mirrors `EvalRunView` (types.ts:168-182) field-for-field; `passed` is server-authoritative (types.ts:166-167). Store is sync — call via `anyio.to_thread.run_sync` (as with all sync stores, §3.3). Default limit 100, clamp ≤ 500. **Governance:** authenticated (any principal — eval results carry no user data). **Errors:** 401; 422 bad limit. |
| 12 | **GET `/v1/kpi/summary`** | Console: `client.ts:265-267` → `KpiSummaryView {kpis: KpiEntryView[], generated_at}` (`types.ts:186-199`). **Populated.** U-P-B rows come from `ne.obs.collect_window` over the configured audit/eval/signal read seams; when those seams are unconfigured the route returns the documented-empty scaffold `{"kpis": [], "generated_at": <now UTC>}` (200) and the console's KpiPage renders its empty state. **C8 spend attribution** adds `spend.outcome.<kpi_tag>` rows (unit `usd`, served call count on `dimensions.calls`) plus `spend.unattributed`, `spend.attributed_usd`, `spend.total_usd`, `spend.attributed_share`, and `spend.calls_missing_cost`, from `ne.obs.collect_spend` over the same durable, hash-covered audit chain. These are AGGREGATIONS of recorded `cost_usd` grouped by the tag each call actually carried — never a synthesized or imputed KPI: untagged spend stays in `spend.unattributed`, an unpriced call keeps its outcome but is summed nowhere and counted in `spend.calls_missing_cost`, and both shares are omitted rather than defaulted when their denominator is zero. `spend.attributed_share` is DOLLAR-weighted; `spend.attributed_call_share` is the call-weighted coverage figure and is the one to read for coverage. **GATING (honest finding):** the whole route — spend rows included — returns the empty scaffold unless `kpi_sources` is set, and `app.py` sets it only when BOTH an eval store and a flywheel engine are configured. A deployment with a full audit chain and real spend but no eval/flywheel DB therefore serves no spend rows at all. **Governance:** authenticated. |

### 1.7 Steward (NE-066/067)

All five write/read surfaces sit over the REAL `ProposalReviewService`
(`ne-flywheel/review.py:246-355`) and `SqlAlchemyProposalStore`
(`ne-flywheel/proposals.py:279-311`), composed on ONE engine via
`review_service_from_settings` (`ne-flywheel/factory.py:65-90`) so review row +
rejection signal + dual-appended audit event commit atomically (review.py:185-198).

Actions build the required `SignalProvenance` (`ne-flywheel/signals.py:45-70`)
**server-side only**: `principal=<bearer principal>`, `surface=SignalSurface.CONSOLE`
(signals.py:40), `session_id=<server-derived per-request id>` (§7-R4). The body
NEVER carries any of these (client.test.ts:291, 318, 345). All steward routes
require membership in `ApiSettings.steward_roles` (fail-closed: empty ⇒ nobody;
same pattern as approvals #7) → 403 otherwise.

| # | Endpoint | Spec |
|---|---|---|
| 13 | **GET `/v1/steward/proposals`** | Console: `client.ts:271-275` → `{proposals: StewardProposalView[]}` — non-terminal (`proposed`\|`approved`) only, server-scoped and ordered (client.ts:88-90). **Delegates to:** (a) `SqlAlchemyProposalStore.read_proposals(limit=…)` (proposals.py:279-302; note the stored `status` column is the append-time constant — the LIVE status is folded from the review log); (b) fold current status per proposal via `ProposalReviewService.status_of(proposal_id)` (review.py:329-339) or one aggregated query over `ne_proposal_reviews` (review.py:149-182) — filter to `proposed|approved`; (c) render the sanitized artifact via `ne.flywheel.diffs.render_proposal_diff(proposal)` (diffs.py:372-430; deterministic, injection-safe, trusted-only title, summary confined to a fenced untrusted section). **Response mapping:** `StoredFixProposal` (proposals.py:177-195) → `StewardProposalView` (types.ts:252-265) copying proposal_id/lane/kind/cluster_id/confidence/priority/evidence_event_ids/evidence_signal_ids/config_hash/created_at, `status` = folded status, `artifact` = `DiffArtifact` (diffs.py:126-138) → `DiffArtifactView` (types.ts:235-243). **NEVER serialize `summary` or `payload`** (types.ts:246-251). Order: `created_at` ASC (oldest-first queue). **Errors:** 401; 403. |
| 14 | **POST `/v1/steward/proposals/{proposal_id}/approve`** | Console: `client.ts:277-285`, body `{note: string}` (may be `""` → pass `None` when blank) → `ReviewEventView` (`types.ts:272-286`). **Delegates to:** `ProposalReviewService.approve(proposal_id, provenance, note=note)` (review.py:277-285). `StoredReviewEvent` (review.py:206-224) mirrors `ReviewEventView` 1:1 (drop the surrogate `id`; wire has no `id` field — serialize the remaining 13 fields). **Errors:** 401; 403 role; 404 `UnknownProposalError` (errors.py:39); **409** `InvalidTransitionError` (errors.py:48; raised review.py:412-418 — the console renders 409 verbatim and keeps the row actionable, StewardQueuePage.tsx:13, 172-176); 422 validation. |
| 15 | **POST `/v1/steward/proposals/{proposal_id}/reject`** | Console: `client.ts:287-295`, body `{code: RejectionCodeWire, note: string}` → `ReviewEventView`. **Delegates to:** `ProposalReviewService.reject(proposal_id, provenance, code=RejectionCode(code), note=…)` (review.py:287-300) — atomically emits the `thumbs_down` rejection→signal feedback (review.py:295-299, 435-444). `code` MUST validate against the `RejectionCode` enum (review.py:121-129; wire values verbatim, types.ts:222-223) → 422 on unknown code. **Errors:** as #14. |
| 16 | **POST `/v1/steward/proposals/{proposal_id}/apply`** | Console: `client.ts:297-306`, body `{applied_ref: string, note: string}` → `ReviewEventView`. **Delegates to:** `ProposalReviewService.record_apply(proposal_id, provenance, applied_ref=…, note=…)` (review.py:302-325). RECORDS a human-performed apply only — `ne-api` NEVER executes an apply (review.py:310-315; client.ts:104-107). Empty/whitespace `applied_ref` → **422** (`ReviewError`, review.py:317-318). Legal only from `approved` → else **409** `InvalidTransitionError`. **Errors:** as #14 plus 422 empty applied_ref. |
| 17 | **GET `/v1/steward/digest`** | Console: `client.ts:308-315`, query `week_start?` (ISO date) → `StewardDigestView` (`types.ts:300-306`). **Finding: no digest computation exists in ne-flywheel** — grep confirms no digest/latency function anywhere in `packages/ne-flywheel/src`. Per the NE-067 AS-BUILT note ("the Phase-6 ne-api seam note (ProposalReviewService/render_proposal_diff + digest/latency SQL aggregations)"), **`ne-api` implements these as read-only SQL aggregations** over `ne_fix_proposals` + `ne_proposal_reviews` on the flywheel engine: week window = `[week_start 00:00 UTC, +7d)` (default: current ISO week's Monday); `proposed` = proposals with `created_at` in window; `approved/rejected/applied` = review rows with matching `action`+`created_at` in window; grouped by lane (join review→proposal for lane), plus totals. Deterministic given inputs (client.ts:108-109; types.ts:297-299). Malformed `week_start` → 422. **Governance:** steward_roles. |
| 18 | **GET `/v1/steward/latency`** | Console: `client.ts:317-319` → `StewardLatencyView` (`types.ts:314-321`). **Same finding as #17 — ne-api-computed SQL aggregation:** per proposal, wait = first review row's `created_at` − proposal `created_at`; `median_wait_s`/`p90_wait_s` over decided proposals (null when `decided_count == 0`); `pending_count` = folded-status `proposed` count; `oldest_pending_age_s` = age of oldest pending (null when zero pending); `generated_at` = now. Nulls exactly per types.ts:309-313. **Governance:** steward_roles. |

### 1.8 Health

| # | Endpoint | Spec |
|---|---|---|
| 19 | **GET `/healthz`** | Public (auth-exempt). Returns `{"status": "ok"}` 200 when the process is up; optionally `{"status": "degraded", …}` 503 if the DB engine ping fails. Exemption is EXACT-match in the middleware (middleware.py:82-84, docstring 14-15: `/healthX` still requires auth) — configure `OidcConfig.exempt_paths=("/healthz",)` (identity config.py:60). No secrets, versions, or config in the body. |

---

## 2. SSE streaming spec — `POST /v1/chat/sessions/{id}/messages`

The consumer parser is `packages/ne-console/src/api/sse.ts` (own ~80-LOC parser;
native EventSource can't POST or send Authorization — sse.ts:4-9).

### 2.1 Response envelope (fail-closed on the client)

- Status MUST be **200** with `Content-Type: text/event-stream` — anything else
  throws `SseError` before body parse (sse.ts:59-67). Recommended headers:
  `Cache-Control: no-store`, `X-Accel-Buffering: no`.
- Auth failures happen BEFORE streaming: the ASGI middleware answers 401 JSON
  (middleware.py:105-121); the console maps a streaming 401 onto the SAME
  re-auth path as JSON requests (client.ts:187-190). Pre-stream route errors
  (404/403/422) return plain JSON `{"detail": …}` with the right status — the
  client surfaces them as `SseError(status)`.

### 2.2 Frame grammar (sse.ts:105-124)

```
event: <type>\n
data: <one-line JSON payload>\n
\n
```

- Frames are terminated by a blank line; CRLF is normalized (sse.ts:78-92).
- Multi-line `data:` is joined with `\n` (sse.ts:123) — emit single-line JSON.
- Lines starting with `:` are comments and skipped (sse.ts:109) → keep-alives.
- The client routes on the SSE `event:` field, falling back to the payload's
  `type` (client.ts:172-178). Emit BOTH: `event: token` AND `"type": "token"`
  in the JSON.

### 2.3 Event union (types.ts:63-67, consumed by `useAgentStream.ts:69-83`)

| event | payload | when |
|---|---|---|
| `token` | `{"type":"token","delta": string}` | zero or more; deltas accumulate client-side |
| `citations` | `{"type":"citations","citations": Citation[]}` | zero or one; attaches evidence to the message |
| `done` | `{"type":"done","session_id": string,"output": string}` | terminal success; `output` is the FULL final text (client replaces accumulated deltas — useAgentStream.ts:75-81) |
| `error` | `{"type":"error","detail": string}` | terminal failure after streaming started |

### 2.4 Server behavior (v1 — turn-then-stream)

`DepartmentAgentSession.run` is **non-streaming**: it returns
`AgentRunResult {output, session_id}` (`ne-contracts/runtime.py:82-86`) after the
full pydantic-ai loop. v1 therefore:

1. Validate session + department + prompt (pre-stream JSON errors, §1.2 #3).
2. Open the event-stream response immediately; while `run()` is in flight, emit a
   comment keep-alive `: keep-alive\n\n` every 15 s (the parser drops comments;
   this defeats proxy idle timeouts during long HITL-gated turns — a turn that
   hits a `require-approval` blocks inside the gate's `await_outcome` poll loop,
   approvals.py:264-290, potentially for minutes).
3. On success: emit ONE `token` frame with the full output as `delta`, then
   `done {session_id, output}`. (Contract-legal: "zero or more" tokens; `done`
   carries the authoritative full text.) True incremental token streaming
   requires a ne-runtime addition (§7-R7) and upgrades transparently later.
4. On failure after the stream opened (`SessionDepartmentMismatchError` race,
   `RuntimeAuditError`, gateway errors): emit `error {detail: <error class /
   safe message>}` and close. Never leak stack traces or backend URLs.
5. `citations`: after a successful governed `read.knowledge.search`, emit zero or
   one frame containing the exact ACL-trimmed `RetrievalResult.citations`, after
   `token` and before `done`. Stable ordered de-duplication unions multiple
   searches. Empty, denied, errored, timed-out, and no-search turns emit none.
6. Client abort (`AbortSignal`, sse.ts:34) → the ASGI receive channel closes;
   cancel the in-flight turn task.

Run the turn concurrently with the keep-alive ticker (e.g. `asyncio.create_task`
+ an async generator multiplexing turn-completion and ticks), mirroring the
`spawn` seam pattern of the Slack ingress (`ne-chat/slack/ingress.py:80-97`).

---

## 3. Composition root design

### 3.1 App assembly & mount strategy

Precedent: `create_slack_ingress` builds a plain `fastapi.FastAPI` with injected
seams (`ne-chat/slack/ingress.py:86-102`). `ne-api` follows the same
factory-function style: `ne.api.app.create_app(settings) -> Starlette`.

**Two-layer mount** (preserves the middleware's exact-match fail-closed exemption
semantics, middleware.py:14-15):

```
outer Starlette app
├── /healthz                     → public route (also listed in exempt_paths as belt-and-braces)
├── /api  → OidcAuthMiddleware( inner FastAPI app with all /v1/* routers )
└── /     → OPTIONAL StaticFiles(console dist/, html=True)   [owner decision iii]
```

- The inner FastAPI app carries ALL governed routers; it is wrapped by
  `ne.identity.factory.build_identity(identity_settings, inner_app)`
  (factory.py:31-52 — verifier + `InMemoryPrincipalStore` + `JitProvisioner` +
  `OidcAuthMiddleware`; the module docstring names this "the convenience wiring
  for the ne-api process", factory.py:7-9). Route paths inside the inner app are
  `/v1/...`; the console's `API_BASE="/api"` prefix comes from the mount.
- Static console serving (if enabled) is OUTSIDE the auth middleware — no
  prefix-exemption hole is ever added to the middleware. The SPA handles its own
  OIDC redirect flow.
- No route exists that bypasses the middleware except `/healthz` and static.

### 3.2 Lifespan (startup order is normative)

FastAPI lifespan context, in THIS order:

1. **Settings** — construct all module settings (env-driven, §3.4). Load secrets
   via `ne.secrets.factory.create_secret_store(SecretsSettings())` and inject
   (DB passwords, OIDC client internals) — never in compose/env/logs (C12,
   NE-070 precedent).
2. **`freeze_platform_config(settings=runtime_settings, govern_settings=govern_settings, extra_modules={"ne-identity": identity_settings, "ne-gateway": gateway_settings, "ne-audit": audit_settings, "ne-eval": eval_settings, "ne-flywheel": flywheel_settings, "ne-api": api_settings})`**
   (`ne-runtime/factory.py:102-146`) — ONCE, BEFORE any component that emits
   events; this is the ADR-0003 composed platform hash that stamps every
   AuditEvent (`ne-contracts/config.py:238-253`). NE-CKC12 precedent: the NE-070
   pilot config bundle stamps the SAME hash.
3. **Engines** — one SQLAlchemy engine per database URL, `ensure_transactional_sqlite`
   applied (audit/engine.py:55); ensure schemas via each library's `ensure_*`
   (approval: factory.py:306-308; runtime: store.py:136-138; review closure:
   review.py:185-198; eval: store.py:67; audit: store.py:212). For the pilot all
   ride the platform Postgres.
4. **Audit sink** — `ne.audit.factory.create_audit_sink(audit_settings)`
   (audit/factory.py:12-29). ONE sink instance shared by every component below.
5. **Approval gate** — `create_approval_gate(sink=sink, settings=runtime_settings)`
   (factory.py:278-314) → the ONE `DurableApprovalGate`.
6. **Shared guardrail state** — `ledger, counter = create_shared_guardrail_state(database_url)`
   (factory.py:149-179; pass the platform Postgres URL so caps are durable +
   worker-shared, NE-070H).
7. **Permissions / C7** — `store, expander, sync = build_permissions_stack(permissions_settings, sink)`
   (`ne-permissions/factory.py:52-61`); `acl = PrincipalAclFilter(expander)`
   (trimming.py:92-107).
8. **Brain** — `TEIEmbedder(api_settings.tei_url, …)` (embedder.py:37-47),
   `FastEmbedBM25Encoder()` (sparse.py:34), `QdrantChunkStore(AsyncQdrantClient(api_settings.qdrant_url), api_settings.qdrant_collection, embedder, sparse_encoder, group_expander=expander)`
   (qdrant.py:89-99), `retriever = HybridRetriever(store, embedder, sparse_encoder, acl_filters=acl)`
   (retriever.py:166-176).
9. **Gateway** — `LiteLLMGateway(gateway_settings, cost_sink, audit_sink=sink)`
   (`ne-gateway/gateway.py:69-84`).
10. **C6 executor** — per the NE-CKD1 normative recipe (factory.py:30-47):
    `scope_rail = AgentScopeGuardrail(credential_service)` (composition root MAY
    import ne-identity; factory.py:32-33), then
    `executor = create_tool_executor(bindings, sink=sink, approval_gate=gate, ledger=ledger, counter=counter, extra_guardrails=(scope_rail,))`
    (factory.py:182-222 — sink and gate are REQUIRED kwargs; no gate-less path
    exists by construction). `bindings` = the chat agent's tool menu (v1: a
    `knowledge.search` `ToolBinding` (tools.py:43-58) whose handler calls
    `retriever.search` with the turn principal; menu is `ApiSettings`-driven).
    If `ne-api` ever runs a second executor (dispatcher/cron), it MUST share
    this SAME ledger/counter/scope-rail (factory.py:20-29) — v1 runs exactly one.
11. **Stores** — `SqlAlchemySessionStore.from_settings(runtime_settings)`
    (store.py:129-139); `review_service_from_settings(flywheel_settings)`
    (flywheel/factory.py:65-90) + `SqlAlchemyProposalStore(review_service.engine)`
    (same engine — the atomicity requirement, review.py:189-193);
    `EvalRunStore(eval_engine)`; the audit read engine.
12. **Identity wrap** — `build_identity(identity_settings, inner_app)` (§3.1).

All constructed singletons live on `app.state`; routers access them via FastAPI
dependencies in `ne/api/deps.py` (`current_principal` re-exported from
`ne.identity.middleware` — the ONLY principal read path, middleware.py:124-133).

### 3.3 Sync-library discipline

`EvalRunStore`, `ProposalReviewService`, `SqlAlchemyProposalStore`, `replay`,
`verify_chain`, and the digest/latency SQL are **sync**. Call them via
`anyio.to_thread.run_sync` (or `run_in_threadpool`) from async routes so the
event loop (which also carries SSE turns) never blocks on DB I/O.

### 3.4 Settings model

New `ne.api.settings.ApiSettings(NeSettings)`, `env_prefix="NE_API_"`,
`extra="forbid"`, `toml_file=ne.toml` (house style: runtime/config.py:87-92):

| Field | Env | Purpose |
|---|---|---|
| `qdrant_url: str` | `NE_API_QDRANT_URL` | brain store endpoint |
| `qdrant_collection: str` | `NE_API_QDRANT_COLLECTION` | chunk collection |
| `tei_url: str` | `NE_API_TEI_URL` | embedder endpoint |
| `approver_roles: tuple[str,...] = ()` | `NE_API_APPROVER_ROLES` | HITL role pre-check (empty ⇒ nobody — fail closed) |
| `steward_roles: tuple[str,...] = ()` | `NE_API_STEWARD_ROLES` | steward role pre-check (empty ⇒ nobody) |
| `ops_roles: tuple[str,...] = ()` | `NE_API_OPS_ROLES` | audit read access (empty ⇒ nobody) |
| `chat_agents: tuple[str,...] = ("console-chat",)` | `NE_API_CHAT_AGENTS` | createSession allowlist |
| `serve_console_dist: str = ""` | `NE_API_SERVE_CONSOLE_DIST` | optional static mount path |
| `sse_keepalive_s: float = 15.0` | `NE_API_SSE_KEEPALIVE_S` | §2.4 |

Everything else rides the EXISTING module settings (all env-driven already):
`NE_RUNTIME_SESSION_DATABASE_URL` (runtime/config.py:104), `NE_RUNTIME_APPROVALS__TIER3_NAMED_APPROVERS`
(nested, config.py:51), `NE_IDENTITY_*` (issuer/audience/jwks REQUIRED, no
defaults — identity/config.py:5-16, 131), `NE_GATEWAY_*` (vllm/model menu,
gateway/config.py:60), `NE_AUDIT_*`, `NE_EVAL_*`, `NE_FLYWHEEL_SIGNAL_DATABASE_URL`
(flywheel/factory.py:73-83), OPA URL via `GovernSettings`, permissions DB via
`PermissionsSettings`. The dev OIDC issuer must match the console's
(`http://localhost:8081/realms/ne-dev`, oidcConfig.ts:13; audience per the
Keycloak client — NE-103b provisions the pilot realm).

### 3.5 Process & runtime

- **New dependency: `uvicorn`** — currently declared NOWHERE in the tree.
  Entry point: `python -m ne.api` →
  `uvicorn.run("ne.api.app:create_app", factory=True, host=…, port=…)`.
  Multi-worker safe: all cross-request state is in Postgres (sessions, approvals,
  guardrail caps NE-070H, reviews) — the service is stateless per SPEC §4.
- **Non-root**: the (NE-103a) image runs as a dedicated UID; `ne-api` itself
  binds a high port (8000) behind the NE-070 Caddy TLS proxy.
- `/healthz` per §1.8.

---

## 4. Governance invariants (the security core — build MUST honor, review MUST verify)

1. **C6 — no ungoverned tool path.** The ONLY executor is built by
   `create_tool_executor` with `sink=` and `approval_gate=` (required kwargs —
   factory.py:193-198: "there is no factory path that omits either"). Chat tools
   exist only as runner-built closures over `GovernedToolExecutor.execute`
   (agent.py:14-18, 149-153); `ne-api` MUST NOT register any FastAPI route or
   helper that invokes a tool handler, the gateway-as-tool, or a write action
   directly. Grep-provable: no import of tool handler modules outside the
   bindings list; no `GovernedToolExecutor(` construction outside the factory
   call.
2. **C7 — no untrimmed read.** Both `/v1/brain/*` routes call ONLY
   `HybridRetriever.search/retrieve` on the ONE retriever constructed with
   `acl_filters=PrincipalAclFilter(expander)` (retriever.py:171, trimming.py:92-107).
   `ne-api` never constructs a `QdrantChunkStore` query directly, never passes
   `filters=` from user input, and its wire models structurally CANNOT serialize
   `acl` (define `ChunkView` with exactly four fields; test it).
3. **HITL — the real gate, never a parallel state machine.** Approve/reject route
   into `DurableApprovalGate.approve/reject` only (approvals.py:153-262);
   `ne-api` never constructs `ApprovalOutcome`, never writes approval rows, never
   re-implements tier logic (mirror of `ne-chat/approvals.py:3-9`). The gate's
   4-layer never-auto-approve proof (approvals.py:19-25) is inherited untouched.
   Steward actions route into `ProposalReviewService` only — never direct
   `ne_proposal_reviews` writes.
4. **Identity from the verified bearer ONLY.** Every actor/approver/provenance
   identity is `current_principal(request).id` — attached by `OidcAuthMiddleware`
   after JWKS verification + JIT provisioning (middleware.py:91-103). No request
   body/query/header other than `Authorization` influences identity. The consumer
   contract enforces this from the other side (client.test.ts:146, 291, 318, 345);
   the server contract tests re-assert it (§5.3): a body smuggling
   `{"approver": "mallory"}` into approve MUST be ignored (or 422 via
   `extra="forbid"` on request models — pick `extra="forbid"`, it's louder).
5. **Fail-closed authorization pre-checks.** Empty `approver_roles` /
   `steward_roles` / `ops_roles` ⇒ 403 for everyone (the ne-chat pattern,
   approvals.py:112-119). Unauthorized attempts are AUDITED (authenticated-but-
   unauthorized asymmetry per ingress.py:9-11).
6. **No chokepoint modification.** `ne-api` adds ZERO code to ne-contracts,
   ne-govern, ne-runtime middleware, ne-brain trimming/retriever, or the approval
   gate. The only sanctioned cross-package addition is the additive read helper
   `ne.audit.list_records` (§7-R1). Frozen contracts stay frozen.
7. **config_hash discipline.** `freeze_platform_config` runs ONCE before any
   event-emitting component (factory.py:108-115); every audit record `ne-api`
   causes (gate transitions, review transitions, api-surface records) carries the
   composed hash. `ConfigHashNotFrozenError` at startup is a boot failure, not a
   500.
8. **Redaction.** `ne-api`-emitted audit detail carries ids / decisions / error
   classes only — never prompts, chunk text, approval payloads, or tokens
   (gate discipline approvals.py:29-33; chat discipline ne-chat/approvals.py:34-36).
   401 bodies carry no reason oracle (middleware.py:16-18). Error `detail` strings
   surface library policy messages (they are deliberately safe, e.g.
   approvals.py:185-188) but never exception reprs from infrastructure failures.
9. **Department scoping.** Chat department is derived from
   `principal.claims["department"]` and re-enforced inside `run()`
   (agent.py:103-108) — defense in depth: even a routing bug cannot cross
   departments.

---

## 5. Package layout & testing strategy

### 5.1 Layout

```
packages/ne-api/
├── pyproject.toml            # deps: fastapi, uvicorn, pydantic, pydantic-settings, sqlalchemy, anyio,
│                             #       ne-contracts, ne-identity, ne-runtime, ne-govern, ne-brain,
│                             #       ne-audit, ne-eval, ne-flywheel, ne-gateway, ne-permissions,
│                             #       ne-events, ne-schema, ne-secrets
│                             # (uvicorn is the tree's FIRST server dep)
├── src/ne/api/
│   ├── __init__.py
│   ├── __main__.py           # python -m ne.api → uvicorn.run(factory)
│   ├── app.py                # create_app(settings) — §3.1/§3.2 composition root
│   ├── settings.py           # ApiSettings (§3.4)
│   ├── deps.py               # current_principal re-export; app.state accessors; role-check dependencies
│   ├── wire.py               # ALL response/request pydantic models mirroring types.ts (extra="forbid"
│   │                         #   on request models; ChunkView with NO acl; StewardProposalView with
│   │                         #   NO summary/payload)
│   ├── errors.py             # exception handlers: library error → status map (§1), string-detail 422
│   ├── sse.py                # SSE frame encoder + keep-alive multiplexer (§2)
│   ├── steward_metrics.py    # digest/latency SQL aggregations (§1.7 #17/#18)
│   └── routers/
│       ├── me.py  chat.py  brain.py  approvals.py  audit.py  evals.py  kpi.py  steward.py
├── tests/
│   ├── conftest.py           # app factory over SQLite + in-memory fakes; token-minting helper
│   ├── test_contract_shapes.py
│   ├── test_auth.py
│   ├── test_no_bypass.py
│   ├── test_sse.py
│   ├── test_approvals_api.py test_steward_api.py test_brain_api.py test_audit_api.py
│   └── test_composition.py
└── README.md
```

### 5.2 Test approach

Use `httpx.AsyncClient(transport=ASGITransport(app=…))` against a `create_app`
built over: SQLite engines (every store supports SQLite — approvals, sessions,
review, eval, audit chain), a fake `TokenVerifier` + real `JitProvisioner` behind
the REAL `OidcAuthMiddleware` (pattern: identity's own middleware tests), a stub
`ModelGateway`, an in-memory `ChunkStore` implementing `get_filtered`
(`ne.brain.testing` fixtures), and the REAL `DurableApprovalGate` /
`ProposalReviewService` / `GovernedToolExecutor` — the governed spine is never
mocked; only network edges (model, embedder, qdrant) are.

### 5.3 Mandatory test classes

1. **Contract tests (~19 endpoints).** For every route in §1: exact path/method,
   response JSON keys == the types.ts interface field set (encode the field lists
   as literals in the test — a drifted key is a console break), datetime format,
   list-envelope keys (`approvals`, `runs`, `proposals`, `events`+`verification`).
2. **Auth tests.** No bearer → 401 with `WWW-Authenticate: Bearer` on EVERY /v1
   route (parameterized sweep); `/healthz` → 200 unauthenticated; query-param
   token `?access_token=` → 401 (middleware.py:13-14); garbage bearer → 401 with
   no reason oracle.
3. **No-bypass / identity tests (the security core).**
   - approve with body `{"approver": "mallory@evil"}` → 422 (extra=forbid), and
     the gate row's approver is untouched;
   - steward approve with body `{"actor": …, "surface": …, "session_id": …}` →
     422; a legal call records `actor == <bearer principal id>` and
     `surface == "console"` in the returned `ReviewEventView` and in
     `ne_proposal_reviews`;
   - brain search as a principal with group G sees ONLY G-labeled chunks; a
     groupless principal + the real `PrincipalAclFilter` still gets a non-empty
     self-label filter (trimming.py:96-100) matching nothing foreign; response
     JSON contains NO `acl` key anywhere (recursive assert);
   - proposals list JSON contains NO `summary`/`payload` keys;
   - Tier-3 named-approver flow: non-listed approver → 403 with the exact detail
     substring `"not in the Tier-3 named-approver list"`; first listed approver →
     `pending_second`; same approver again → 403 (distinct-second,
     approvals.py:214-217); distinct second → `approved`;
   - empty `approver_roles` → 403 even for a group-rich principal (fail closed);
   - audit events with `actor=X` filter returns only payload-actor-X rows and the
     page carries a `verification` verdict; a tampered row (direct SQL UPDATE of
     a payload) flips `verification.valid` to false — served verbatim.
4. **Governance-wiring test (`test_composition.py`).** Boot `create_app`; assert
   process config_hash is frozen (get_config_hash succeeds) BEFORE any request;
   assert the executor instance on `app.state` has the approval gate identity-
   equal to the gate the approvals router uses (one gate); assert exactly one
   AuditSink instance is shared (identity check across gate / review service /
   executor); a chat turn whose tool hits `require-approval` creates a REAL
   pending row visible via `GET /v1/approvals/pending`, and approving it via the
   HTTP route releases the turn (end-to-end HITL through HTTP — the single most
   valuable test in the unit).
5. **SSE tests.** Happy path: `token` then `done` frames, correct
   `text/event-stream` + 200, payload `type` == frame `event`; keep-alive comment
   emitted during a slow (fake-clock) turn; post-stream error → `error` frame;
   pre-stream 401 → JSON 401 (parse with a vendored copy of the sse.ts grammar
   rules: blank-line framing, comment skip, multi-line data join).
6. **Steward transition mapping.** approve twice → second is 409; apply from
   `proposed` → 409; empty `applied_ref` → 422; unknown proposal → 404; unknown
   rejection code → 422; reject → a `thumbs_down` signal row exists (the
   rejection→signal loop observed through HTTP).

---

## 6. Decomposition recommendation

**Split into three serial-then-parallel units** (adopting and refining the NE-104
sketch). One L-sized unit would concentrate ~19 endpoints ×
3 chokepoints into a single adversarial review — too much surface for
review-granularity on a security-critical build; three units keep each review
focused on ONE governance story.

| Unit | Scope | Review focus | Depends on |
|---|---|---|---|
| **NE-104a** — composition root + auth + chat + brain | package skeleton, `ApiSettings`, `create_app` lifespan (§3.2 steps 1-12), OIDC wrap + `/healthz` + mount strategy, `/v1/me`, chat sessions + SSE turn, brain search/retrieve, `wire.py` for those, uvicorn entrypoint | C6 + C7 + auth spine + config_hash freeze | nothing (first) |
| **NE-104b** — ops surfaces | approvals pending/approve/reject (+ role pre-check + api-surface audit records), audit events/replay (incl. the `ne.audit.list_records` additive helper §7-R1), eval runs, kpi scaffold | HITL no-bypass + audit read discipline | 104a (app skeleton, deps.py, error map) |
| **NE-104c** — steward surfaces | proposals list (+status fold + artifact render), approve/reject/apply, digest + latency SQL (`steward_metrics.py`) | NE-066 provenance + sanitized-artifact discipline + the two ne-api-computed aggregations | 104a (skeleton); parallel with 104b (disjoint routers/stores) |

Rationale: 104a is the keystone (everything imports its skeleton) and carries the
two hardest problems (SSE + the full composition root), so it gets the deepest
review; 104b and 104c are then mechanically parallel — disjoint routers, disjoint
engines, shared only through `deps.py`/`errors.py` landed in 104a. Each unit
lands with its §5.3 test classes; the end-to-end HITL-through-HTTP test (§5.3-4)
lands in 104b when both halves exist. NE-103a (image) follows 104a; NE-103b
(Caddy vhost + pilot Keycloak realm/client/approver users) can proceed in
parallel once 104a fixes issuer/audience expectations.

---

## 7. Open questions / risks (build must resolve; none may be silently hand-waved)

- **R1 — ne-audit lacks the filtered list read.** `/v1/audit/events` needs
  actor/action/time-window listing; `replay()` (replay.py:76-137) doesn't accept
  actor/action, and `_record_from_row` is private. **Required additive change:**
  public `ne.audit.list_records(...)` (§1.5 #9) following the payload-
  authoritative discipline (replay.py:11-19). Small, read-only, non-chokepoint —
  but it IS a cross-package edit and must be called out in the 104b PR.
- **R2 — audit list ordering/pagination is unspecified by the consumer.** The
  console sends only `limit` (client.ts:76-82) — no cursor. Decide: `seq` DESC
  (newest-first, recommended for an ops console) and document; verification
  windowing (`verify_chain(start,end)` vs whole-chain per page — whole-window
  matching the from/to filter is recommended, verify.py:400-410) is a build
  decision to document in the route docstring.
- **R3 — session lookup by id.** `SqlAlchemySessionStore` exposes
  `open_session(agent, department)` (get-or-create) and `load_history(session_id)`
  but no `get_session(session_id) -> (agent, department)`. v1 workaround: with
  one configured agent, `open_session("console-chat", dept)` and 404 on mismatch
  (§1.2 #3). Clean fix: an additive `session_meta(session_id)` read on the store
  (ne-runtime, non-chokepoint) — decide in 104a.
- **R4 — provenance `session_id` for steward actions.** `SignalProvenance`
  requires non-empty `session_id` (signals.py:48-50) and the console sends none
  (by design). Recommended: the token's `sid` claim when present, else a fresh
  `uuid4` per request; document that `ReviewEventView.session_id` is a server
  correlation id, not a chat session.
- **R5 — KPI summary is populated.** U-P-B rows come from `ne.obs.collect_window`;
  the empty list is now only the unconfigured-seams floor (§1.6 #12). Spend
  attribution rows are aggregations of recorded `cost_usd` grouped by the
  `kpi_tag` each call carried — a sum of measurements, not a synthesized KPI.
  The standing rule still holds for everything else: do not invent a KPI the
  stores cannot evidence.
- **R6 — chat citations have a binding-owned producer.** The governed
  `read.knowledge.search` binding records exact ACL-trimmed citations only after
  successful retrieval and handler rendering. A per-turn accumulator relays the
  stable ordered union without changing `AgentRunResult` or inferring from model
  prose. `Citation` still has no `source_url`; the console field remains optional.
- **R7 — true token streaming needs a ne-runtime seam.** `DepartmentAgentSession.run`
  is atomic; `ModelGateway.stream` exists (models.py:69-79) but the pydantic-ai
  loop doesn't surface it. v1 turn-then-stream (§2.4) is contract-legal; a later
  `run_stream` addition to ne-runtime upgrades UX without any console change.
- **R8 — approver/steward/ops role names are deployment config.** The role
  pre-check groups (§3.4) must match the pilot Keycloak realm's groups (which
  does not exist yet — NE-103b provisions it). Empty-by-default means a
  misdeployed ne-api is fail-closed (nobody can approve), which is correct but
  must be called out in the runbook.
- **R9 — long-lived SSE vs approval SLAs.** A chat turn blocked on a Tier-2
  approval can pend for hours (SLA 4h, approvals.py:43); no keep-alive keeps a
  browser tab open that long. Decide a turn timeout (e.g. 120 s → emit `error`
  "approval pending; retry later") — the pending approval row survives and is
  actionable from `/approvals`; the request re-runs later. Must be documented UX.
- **R10 — proposals list scale.** Status folding via per-proposal `status_of` is
  N+1; fine at pilot scale, but 104c should implement the single aggregated
  fold query from the start (review log is tiny; the join is trivial).
