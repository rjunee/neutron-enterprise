# NE-087 — OpenFGA ReBAC module (producer spec, build-ready)

**Status:** producer spec for the SPEC §5 Phase-7 row `NE-087 | OpenFGA ReBAC module (fine-grained sharing where source ACLs aren't enough) | M` (component decision "Data authz | OpenFGA (optional ReBAC module, v1.1)"; on the fast-follow list). Authorized under the 2026-07-08 full-Phase-7 scope expansion.
**Author:** Fable architect pass, 2026-07-09. Every load-bearing claim is file:line-cited against merged main.
**Security framing:** this module touches C7 (permission trimming) — the constraint Checkpoint D just spent an audit→fix→re-audit loop hardening (the `acl:` smuggling class). NE-087 must EXTEND the fail-closed label model, never weaken it. The single most security-critical decision — the union-vs-intersection composition rule — is §2 and is decided, defended, and test-pinned here.

---

## 0. What exists today (the model NE-087 extends)

The merged C7 stack (NE-042/043, hardened by NE-CKC2 + NE-CKD2):

- **Connectors mirror source ACLs fail-closed.** `ne.connect.acl.AclMapper.resolve` can NEVER return an empty ACL — an unlabeled record is an error, not a write (packages/ne-connect/src/ne/connect/acl.py:41-70); paginated grants are UNIONed, never last-write-wins (acl.py:22-38). Chunks carry those labels in `Chunk.acl` (packages/ne-contracts/src/ne/contracts/types.py:47-54).
- **The principal store + query-time expansion.** `PrincipalStore` (SQLAlchemy Core, single-tenant, recompute-not-accrete edges — packages/ne-permissions/src/ne/permissions/store.py:16-20) feeds `PrincipalExpander.expand()` (packages/ne-permissions/src/ne/permissions/expand.py:54-102): result always contains the principal id (never empty), unknown principals expand to exactly themselves, the `acl:` namespace is RESERVED — stripped from seeds (expand.py:68), BFS frontier (expand.py:85), and final sweep (expand.py:95) — so `TENANT_ALL`/`PUBLIC_LINK` (`acl:tenant-all`/`acl:public-link`, packages/ne-permissions/src/ne/permissions/models.py:33-44) are granted EXCLUSIVELY by the `is_known` gate (expand.py:97-101). Revocation lag = sync interval (packages/ne-permissions/src/ne/permissions/config.py:24-36).
- **The trimming seam.** `ne.brain.trimming.PrincipalAclFilter` (packages/ne-brain/src/ne/brain/trimming.py:92-107) wraps `acl_match_labels` (trimming.py:67-89 — never empty, deny-by-erroring) over a package-local `GroupExpander` Protocol (trimming.py:51-64; ne-brain must NOT import ne-permissions — the cycle is documented at trimming.py:20-37 and ne-permissions/sovereignty.py:12-30). It is injected at the composition root as `HybridRetriever(acl_filters=...)` (packages/ne-brain/src/ne/brain/retriever.py:171), called on BOTH `search()` (retriever.py:192) and `retrieve()` fetch-by-id (retriever.py:228), AND-combined with any caller filter by intersection-on-collision (`and_combine_filters`, retriever.py:106-160), and compiled by the shared `_build_filter` grammar into a Qdrant filter where an empty/missing `acl` payload matches NOTHING (packages/ne-brain/src/ne/brain/qdrant.py:293-316, `_ACL_FIELD = "acl"` qdrant.py:54).
- **The PDP is for actions, coupled to audit by construction.** `GovernedPdp` has no sink-less constructor and withholds any decision it cannot audit (packages/ne-govern/src/ne/govern/pdp.py:57-105).
- **What the label model CANNOT express** (the NE-087 gap): a per-document grant to a subject the source system never named. "This doc is visible to project-X members plus Jane, but not the rest of the department" requires either re-stamping the chunk's `acl` payload (violating the decided NE-042 model — chunks keep source ACLs as-is, membership changes never re-index) or a second, relationship-based grant channel. NE-087 is that channel.

---

## 1. The ReBAC model

### 1.1 OpenFGA authorization model (DSL, schema 1.1)

Deliberately minimal for v1 — one object type, one consumable relation, one management relation:

```
model
  schema 1.1

type user

type group
  relations
    define member: [user]

type document
  relations
    define owner: [user]
    define viewer: [user, group#member, owner]
```

- `user:<principal_id>` — the subject id is EXACTLY the ne-identity bearer-derived `Principal.id` (packages/ne-contracts/src/ne/contracts/types.py:32-37), same opacity contract as the label system (models.py:5-9: ids stored exactly as the source gives them).
- `group:<group_id>` — group ids are EXACTLY the NE-042 store/IdP group ids. **Group membership is NOT stored in OpenFGA** (see §3.4 — contextual tuples; the NE-042 store stays the single source of group truth).
- `document:<document_id>` — the object is the brain document (`Chunk.document_id`, types.py:52), not the chunk. A share grants the whole document; its chunks match via the `document_id` payload field (§3.2). Chunk-level shares are out of scope for v1.
- `viewer` is the ONLY relation the read path consumes. `owner` exists so revocation/management authority is modelled (the sharer becomes `owner`; owners may revoke, see §3.5) — it also folds into `viewer` so an owner always sees what they shared.
- **No deny/negative tuples. None.** The model contains no `but not`, no exclusion, no intersection operators. ReBAC in Neutron is a strictly additive grant channel (§2); a model that could subtract access would let a ReBAC write silently override a source-system grant and would create a second trimming authority. The model file is pinned (§4.4) and a conformance test asserts the written model contains only the types/relations above (§5, T-12).

The model is stored in-repo at `packages/ne-permissions-rebac/src/ne/permissions_rebac/model/neutron.fga` and written to the server at bootstrap; the resulting `authorization_model_id` is pinned in settings (§4.3) and every API call passes it explicitly — a drifted/edited server-side model is therefore unreachable (fail-closed model pinning; mirrors the C11 digest-pin philosophy, deploy/compose/pilot/compose.yaml:12).

### 1.2 The composition rule — UNION of two independently fail-closed grant channels

**THE RULE (normative, test-pinned):**

```
visible(principal, chunk) :=
      source_acl_leg(principal, chunk)      # existing NE-042/043, UNCHANGED
   ∨  rebac_leg(principal, chunk)           # NEW: OpenFGA viewer grant on chunk.document_id

source_acl_leg(p, c) := chunk.acl ∩ expand(p) ≠ ∅          # expand per expand.py:54-102
rebac_leg(p, c)      := c.document_id ∈ ListObjects(user:p.id, viewer, document)
                        (with contextual group tuples from expand(p), §3.4)
```

**Fail-closed direction of each leg — absence of a grant = no access:**
- source leg: a chunk with empty/missing `acl` matches nothing (qdrant.py:304-306); an unlabeled record cannot even be written (acl.py:64-69); an unknown principal expands to itself only (expand.py:14-26).
- rebac leg: no tuple ⇒ `ListObjects` returns nothing ⇒ the leg contributes ∅. Server unreachable ⇒ the leg contributes ∅ (degrade) or the query errors (deny) — NEVER "all" (§3.6). Tuples are minted exclusively by the governed share action (§3.5) — there is no bulk/implicit tuple source.
- The union of two explicit-grant whitelists is itself an explicit-grant whitelist. `visible` is monotone in grants: removing any grant can only shrink the visible set; no ReBAC state can ever REMOVE a source-granted match, and no source state can remove a ReBAC grant's effect on its own leg.

**Why union and not intersection (the defense):**

1. **Intersection cannot deliver the feature.** Under `source AND rebac`, ReBAC could only narrow what the source already grants. The SPEC row's purpose — "fine-grained sharing where source ACLs **aren't enough**" — is precisely granting a subject the source ACL never named (Jane, on a doc whose SharePoint ACL excludes her). AND-composition makes every such share a no-op. An intersection overlay is a *restriction* feature; the platform already owns restriction at the PDP/OPA layer (C6) and at the caller-filter AND-combine (retriever.py:106-138) — conflating the two would put a second, competing deny authority inside C7.
2. **Union is the same shape the model already trusts.** `TENANT_ALL`/`PUBLIC_LINK` are today union-ed into a principal's match set through exactly one strictly-controlled gate (expand.py:97-101). The rebac leg is a third grant channel of identical discipline: one gate (the governed share action), reserved-namespace-hardened, audited, additive.
3. **The genuine cost of union is real and is governed at GRANT time, not at CHECK time.** Union means a platform share can make a doc visible to someone the source system denies. That is the explicit, owner-facing design intent of a share — but it must never happen silently: every tuple write passes the PDP (`rebac.share`, default Tier-1, escalatable per-deployment via Rego — C6) and the tamper-evident audit chain; the sharer must themselves currently be able to read the document (§3.5 "share-requires-read" — you cannot amplify access to content you cannot see); shares are enumerable and revocable; and the module ships **disabled by default** (`enabled=False`, §4.3) so a deployment without an explicit fine-grained-sharing requirement has zero behavior change.
4. **Deny semantics stay in exactly one place per plane.** Source-system revocation continues to work: revoking Jane's *share tuple* removes her rebac leg immediately (§3.5); revoking her *source* access removes the source leg at the next label evaluation. A doc she holds BOTH ways requires both revocations — that is inherent to any dual-channel grant system and is surfaced in the console share view (open question O-5) rather than papered over with a deny-tuple mechanism that would break monotonicity.

**The exact statement for §3 invariant I-1:** *ReBAC never grants access the source ACL denies, EXCEPT through an explicit, PDP-approved, audited, revocable `rebac.share` grant created by a principal who could already read the document — and ReBAC can NEVER remove access the source ACL grants.*

---

## 2. The integration seam (read path)

### 2.1 Placement: inside the C7 trimming filter, NOT behind the PDP

The ReBAC check runs **in the retrieval filter, alongside the label match, inside the existing single `acl_filters` chokepoint** — not in the ACL expander, and not behind OPA:

- **Not in `PrincipalExpander`:** the expander's output is a *label set* matched against `chunk.acl` arrays (trimming.py:67-89). A per-document grant is not expressible as a principal label without stamping the document's chunks — i.e. re-indexing on share/revoke, which the decided ACL model forbids ("membership changes do NOT trigger re-index") and which would make revocation lag = re-index lag.
- **Not behind the PDP:** `GovernedPdp` decides *actions* (one decision per tool call, audited each time — pdp.py:73-105). Retrieval trimming is a per-result-set data-plane concern; routing every candidate chunk through OPA would be a per-query N-decision latency disaster and would bloat the audit chain with non-action noise. C6 (actions) and C7 (data visibility) remain separate planes. OpenFGA is NOT a second PDP: it is a relationship store consulted by the C7 filter and mutated only via PDP-governed actions (§3.5). Single-PDP discipline (one policy engine) is untouched.
- **Inside `PrincipalAclFilter`:** the platform has exactly one production trimming hook, wired at the composition root and enforced by the C7 guardian tests (retriever.py:10-14). NE-087 widens what that hook *returns*; it does not add a second hook or a second retriever path. Every principal-scoped read path — `search()` AND `retrieve()` fetch-by-id (the NE-043 DoD) — inherits the rebac leg automatically because both route through the same hook (retriever.py:192, 228).

### 2.2 The filter shape: an OR-group inside the AND-composed filter

Today the hook returns `{"acl": [labels...]}` (trimming.py:106-107) and `_build_filter` compiles each key to a must-condition (qdrant.py:62-83). NE-087 adds ONE reserved grammar form:

```python
# PrincipalAclFilter.__call__ with a shared-docs resolver wired (production, rebac enabled):
{"$any": [
    {"acl": [<expanded labels>]},              # source leg (unchanged content)
    {"document_id": [<shared document ids>]},  # rebac leg
]}
# resolver absent (rebac disabled / not installed) — IDENTICAL to today:
{"acl": [<expanded labels>]}
# resolver wired but principal has zero shares — collapses to the source leg:
{"acl": [<expanded labels>]}
```

- `_build_filter` gains the reserved key `"$any"`: its value (a non-empty list of sub-mappings, each compiled with the existing grammar) becomes a nested `models.Filter(should=[...])` condition appended to the outer `must` list — OR within the group, AND with everything else. Both `search` (qdrant query path) and `get_filtered` (scroll path, qdrant.py:188-210) compile through `_build_filter`, so `retrieve()` stays symmetric by construction.
- **Anti-smuggling (this is the Checkpoint-D lesson applied to the new grammar):** `$any` is reserved to the trimming hook. `and_combine_filters._check_filter_value` (retriever.py:141-143) MUST reject `$any` (and any `$`-prefixed key) in the CALLER filter position with `ValueError` — a caller-supplied OR-group could otherwise widen the AND-composition into match-more, which is exactly the "unreserved token smuggles tenant-all" bug class (checkpoint-d-boundary-audit.md:17-19). Belt-and-braces: `ne-api` exposes no caller `filters` parameter over HTTP at all (docs/architecture/ne-api-service-spec.md:108). When the ACL mapping carries `$any` and the caller mapping carries plain keys, the plain keys AND-combine outside the group (unchanged intersection semantics, retriever.py:131-138).
- **Fail-closed shape invariants:** the `acl` branch is never empty (contains `principal.id`, trimming.py:84-89); an empty shared-ids list means the `$any` wrapper is NOT emitted (no `MatchAny(any=[])` OR-branch — harmless, but collapsing keeps the disabled/enabled-no-shares paths byte-identical to today's filter, provable by test T-4). The hook still NEVER returns `None` and NEVER returns `{}` (trimming.py:92-101 contract intact).

### 2.3 The resolver seam (import architecture, C3/C5)

Mirror of the `GroupExpander` pattern exactly (trimming.py:50-64 — package-local structural Protocol, concrete impl injected at the composition root, no ne-brain→ne-permissions import edge):

```python
# packages/ne-brain/src/ne/brain/trimming.py (additive)
@runtime_checkable
class SharedDocumentResolver(Protocol):
    """Query-time principal -> ids of documents shared to it via ReBAC (NE-087).

    Satisfied by ne.permissions_rebac.resolver.OpenFgaSharedDocumentResolver,
    injected at the composition root. Async: unlike expand() (pure indexed SQL,
    trimming.py:34-37), this is a network call to the in-VPC OpenFGA service.
    Returns a possibly-empty frozenset; MUST raise on backend failure in
    'deny' mode and return EMPTY (never 'all') in 'degrade' mode (NE-087 §3.6).
    """
    async def shared_document_ids(self, principal: Principal | str) -> frozenset[str]: ...
```

`PrincipalAclFilter.__init__` gains `shared_docs: SharedDocumentResolver | None = None`. Because the resolver is async and the current `acl_filters` seam is a sync callable (retriever.py:171, called sync at retriever.py:192), **NE-087a makes the hook async-capable (additive):** `HybridRetriever` accepts the existing sync callable OR one returning an awaitable; `search`/`retrieve` `await` the result when `inspect.isawaitable(...)`. Existing sync hooks (all current tests, eval fixtures) are untouched; `PrincipalAclFilter.__call__` becomes `async` ONLY in its rebac-carrying form (a `PrincipalAclFilter` without a resolver stays sync — zero behavior change for every existing composition). Blocking network I/O inside the event loop is thereby avoided without touching the frozen ne-contracts surface (neither `acl_filters` nor `GroupExpander` is a ne-contracts Protocol — trimming.py:22-28).

### 2.4 Principal → ReBAC-subject mapping (bearer-derived, never client-supplied)

The subject is ALWAYS `user:{principal.id}` where `principal` is the ne-identity-middleware-derived `Principal` the retrieval/API layer already threads (ne-api-service-spec.md:63: "ALWAYS the mapped principal.id — never the raw ... user id"). No API — HTTP, MCP, or share endpoint — accepts a subject id from the client for the *check* path; the resolver takes the `Principal` object, not a string from the wire. Contextual group tuples (§3.4) come from `expand(principal)` — the same verified-claims + synced-store source the label leg trusts — never from request bodies.

---

## 3. Tuple lifecycle + governance invariants (the security core)

### 3.1 Invariants (normative; each has a test in §5)

- **I-1 (composition):** the §1.2 rule verbatim — ReBAC never grants access the source ACL denies EXCEPT via an explicit, PDP-approved, audited, revocable share created by a principal who can currently read the document; ReBAC can never remove source-granted access. (Tests T-1, T-2, T-3, T-8.)
- **I-2 (fail-closed absence):** a subject with no ReBAC tuple and no source-ACL match sees nothing; an unknown/unsynced principal gets no contextual group tuples beyond its verified token claims and no `acl:*` gate labels (expand.py:14-26 inherited). (T-1.)
- **I-3 (no reserved-namespace smuggling):** `acl:*` ids are rejected as share subjects and objects at `ShareService` validation, stripped from contextual tuples before any OpenFGA call (mirror expand.py:44/68/85/95), and no OpenFGA response can inject labels — the rebac leg contributes only `document_id` values into a `document_id` field condition, never anything into the `acl` field. `$any` is rejected in caller filters (§2.2). (T-6, T-7.)
- **I-4 (single tuple source):** tuples are written EXCLUSIVELY by the governed share action (§3.5). Connectors do NOT mirror source ACLs into OpenFGA — source truth stays in the label channel (acl.py), platform-share truth stays in OpenFGA; dual-writing source ACLs would create a second copy that survives source revocation and silently converts the union rule into "source-ACL-of-yesterday OR source-ACL-of-today". (T-12 asserts the ne-connect tree has no openfga import; .importlinter contract, §4.5.)
- **I-5 (audit completeness):** every share/revoke emits BOTH the PDP's own audit event (pdp.py:96-105, by construction) and a `rebac.share.created`/`rebac.share.revoked` `AuditEvent` (via `ne.events.new_audit_event`, packages/ne-events/src/ne/events/factory.py:58) appended BEFORE the tuple write is acknowledged — audit-append failure ⇒ the share fails closed (the `GovernedPdp`/`DurableApprovalGate` discipline; no NE-CKD5-class optional-sink constructor: `ShareService` has NO sink-less constructor). (T-9.)
- **I-6 (propagation fail-closed):** revocation of a tuple is effective on the next query — v1 runs NO positive result cache (§6 risk R-2 discusses the latency trade); group-membership changes propagate at the NE-042 sync interval, the SAME revocation-lag SLA the label leg already documents (config.py:24-36) — NE-087 introduces no new, longer lag class. (T-3, T-10.)
- **I-7 (chokepoint additive):** `PrincipalAclFilter` remains the single production hook; the C7 guardian suite runs unchanged and green with the module disabled; with it enabled, every existing negative test (untrimmed-console ban, retrieve-by-id-cannot-fetch-what-search-hides) still passes. No second retriever, no bypass of `and_combine_filters`, no weakening of the `{}`-match-nothing branch (retriever.py:23-28). (T-4, T-5, T-11.)
- **I-8 (server-down = fail-closed):** OpenFGA unreachable/timeout/5xx NEVER yields more access than OpenFGA-up. `degrade` mode: rebac leg = ∅ (shared-only docs vanish; source leg unaffected) + a `rebac.check.degraded` signal; `deny` mode: the exception propagates (deny-by-erroring, the trimming.py:16-18 discipline). There is no code path that maps a backend error to "skip trimming" or "match all". (T-8.)

### 3.2 Object granularity

Shares target `document:<document_id>`. The rebac leg filters on the chunk payload's `document_id` field (present in every point — `Chunk.model_validate(point.payload)` round-trips the full model, qdrant.py:280-282; types.py:52). Sharing exposes ALL chunks of the document — matching the mental model of "share this doc" and the citation unit (retriever.py:90-103).

### 3.3 Relations consumed

Read path: `viewer` only. Management: `owner` (who may revoke; the PDP additionally allows steward/admin roles to revoke regardless of ownership — Rego policy, not model, decides that).

### 3.4 Group grants WITHOUT syncing groups into OpenFGA — contextual tuples

A share to "project X" writes ONE tuple: `(group:proj-x#member, viewer, document:d1)`. At check time the resolver passes **contextual tuples** `[(user:<pid>, member, group:<gid>)]` for every gid in `expand(principal) − {pid} − {acl:*}` into `ListObjects`. Consequences:

- The NE-042 store remains the ONLY source of membership truth (store.py:16-20); no second directory to sync, drift, or tombstone-lag (the recompute-not-accrete and tombstone-cascade guarantees, store.py:182-208, apply automatically because the contextual tuples are derived per-query from the live store).
- Membership revocation lag = the existing sync interval, identical to the label leg (I-6).
- The reserved-namespace strip (I-3) happens at contextual-tuple construction, so a hostile token group `acl:tenant-all` can neither become a ReBAC group membership nor (already, per expand.py:68) a label. A share to `group:acl:tenant-all` is independently rejected at write time.

### 3.5 The governed share action (the ONLY tuple writer)

`ne.permissions_rebac.share.ShareService` — constructor requires `(client, resolver_deps, pdp: PolicyDecisionPoint, sink: AuditSink)` (no optional sinks; NE-CKD5 lesson, checkpoint-d-boundary-audit.md:29-32):

```
share(document_id, subject, relation="viewer", *, sharer: Principal)   # subject: user:<id> | group:<id>
revoke(document_id, subject, relation="viewer", *, actor: Principal)
list_shares(document_id | principal)                                   # console/steward view
```

Order of operations in `share()` (each step fail-closed, single exit):
1. **Validate ids:** non-empty; subject/object contain no `acl:` prefix at any position that becomes an OpenFGA id (I-3); relation ∈ {viewer}; subject type ∈ {user, group}.
2. **Subject known:** `PrincipalStore.is_known(subject_id)` (store.py:328-341) must be true — sharing to an unsynced/unknown/tombstoned principal or group is rejected (fail-closed; no dangling grants that spring alive when an id is later minted).
3. **Share-requires-read:** the sharer must currently satisfy the §1.2 union for `document_id` (evaluated via the same filter path — a `get_filtered` probe on any chunk of the doc). A principal cannot amplify access to content it cannot see; a prompt-injected agent holding only `read.own_docs` scope cannot exfiltrate-by-sharing the rest of the corpus (C12 posture).
4. **PDP:** `pdp.decide(PolicyRequest(action="rebac.share", resource=f"document:{document_id}", principal=sharer, context={subject, relation}))` — the audited GovernedPdp (pdp.py:73-105); DENY/REQUIRE_APPROVAL honored (REQUIRE_APPROVAL routes through the NE-035 gate like any tiered action). Default tier 1; deployments raise it (or deny `rebac.share` entirely for label classes) in Rego.
5. **Audit-then-write:** append `rebac.share.created` (actor, subject, relation, object, decision provenance) via the sink; on append failure raise — no tuple written (I-5). Then write the tuple with the pinned `authorization_model_id`. On tuple-write failure after audit: raise; the audited-but-absent grant is the safe direction (audit says "attempted", access never materialized).

`revoke()` mirrors: validate → authority check (actor is owner OR policy-allowed) → PDP `rebac.revoke` → audit `rebac.share.revoked` → delete tuple. Deletion is idempotent.

**Exposure:** a pair of `ne-api` endpoints (`POST /v1/documents/{document_id}/shares`, `DELETE .../shares/{share_id}`, `GET .../shares`) following the ne-api governance template (bearer-derived principal, role pre-checks, 403-before-gate — ne-api-service-spec.md:120), and/or an MCP action tool registered like any governed write-back tool (C6). These endpoints are additive to the ne-api spec's 19 (open question O-4).

### 3.6 OpenFGA-down failure mode

`RebacSettings.unavailable_mode: Literal["degrade","deny"] = "degrade"`:
- **degrade (default):** resolver catches backend/timeout errors, emits `rebac.check.degraded` (ne-obs signal + counter) and returns `frozenset()` — the union collapses to the source leg. Never widens (a revoked-but-uncheckable tuple contributes nothing — degrade is revocation-safe); shared-only documents temporarily disappear (over-restriction, the platform's safe direction). Rationale: an *optional* sharing module must not convert a sidecar outage into a platform-wide retrieval outage.
- **deny:** the exception propagates — the whole query fails (deny-by-erroring, consistent with trimming.py:16-18). For deployments where "shared docs silently missing" is itself unacceptable.
- `share()`/`revoke()` under outage ALWAYS fail (never queue silently) in both modes.

### 3.7 Tuple lifecycle / GC

- **Document deletion:** the brain delete path (`QdrantChunkStore.delete`, qdrant.py:284-291, and the KnowledgeWrite delete flow) triggers best-effort tuple cleanup (`delete_tuples_for(document_id)`), PLUS a periodic reconciliation sweep (`ne rebac gc`, tools CLI): page OpenFGA `Read` by object type `document`, drop tuples whose document no longer exists. Orphaned tuples are hygiene, not a hole — a deleted document has no chunks to match — but they distort share listings and `ListObjects` size, so the sweep is part of DoD.
- **Principal tombstoning:** tombstoned subjects fail `is_known` so new shares to them are rejected; existing `user:<id>` tuples become inert for label-leg-only... no — inert entirely for group grants (no contextual tuples are built for a tombstoned principal's groups because its edges were cascade-deleted, store.py:185-188) but a direct `user:` tuple would still resolve for a principal that authenticates with clean claims yet is store-tombstoned. Therefore the resolver checks `is_known(pid)` first and returns ∅ for unknown/tombstoned principals — the rebac leg is gated the same way `TENANT_ALL` is (expand.py:97-101). (Test T-1b.)
- **Expiry (v1-optional, schema-ready):** OpenFGA tuple conditions support expiring grants (`condition grant_not_expired(current_time, expires_at)`); v1 ships the model without conditions and records expiry as open question O-6.

---

## 4. Package layout, dependencies, deployment, settings

### 4.1 New package `packages/ne-permissions-rebac/` (separate dist, NOT a submodule)

A submodule inside ne-permissions would add the OpenFGA SDK to every deployment's ne-permissions install (pyproject deps, packages/ne-permissions/pyproject.toml:7-13) — wrong for an *optional* v1.1 module. Separate package, import root `ne.permissions_rebac`:

```
packages/ne-permissions-rebac/
├── pyproject.toml                  # name="ne-permissions-rebac", module-name="ne.permissions_rebac"
├── model/neutron.fga               # the pinned authorization model (§1.1)
├── src/ne/permissions_rebac/
│   ├── __init__.py
│   ├── _client.py                  # the ONLY module importing openfga_sdk (C3 chokepoint pattern,
│   │                               #   mirror ne.gateway._litellm / ne.mcp._sdk — .importlinter:30-104)
│   ├── config.py                   # RebacSettings(NeSettings)
│   ├── resolver.py                 # OpenFgaSharedDocumentResolver (satisfies ne.brain SharedDocumentResolver)
│   ├── share.py                    # ShareService (§3.5)
│   ├── bootstrap.py                # store create + model write + model-id pin verification
│   ├── gc.py                       # reconciliation sweep (§3.7)
│   ├── sovereignty.py              # in-VPC URL validation + AIR_GAP posture (mirror
│   │                               #   ne-permissions/sovereignty.py:1-35; NE-CKC10 consolidation candidate)
│   ├── errors.py
│   └── testing/fakes.py            # in-memory tuple store implementing the resolver+client surface for CI
└── tests/
```

Deps: `ne-contracts`, `ne-events`, `ne-permissions` (for `PrincipalStore.is_known` + expander types; legal edge — rebac is a leaf consumer, ne-brain imports NEITHER, so no cycle per trimming.py:25-28), `openfga-sdk` (pinned). Entry-point registration follows the ne-permissions plugin pattern (pyproject.toml:22-28), e.g. `[project.entry-points."ne.plugins.shared_document_resolver"] openfga = "ne.permissions_rebac.resolver:OpenFgaSharedDocumentResolver"`.

### 4.2 Dependencies (C4/C11 intake)

| Dep | License | Notes |
|---|---|---|
| OpenFGA server (`openfga/openfga` image) | **Apache-2.0** (CNCF project) — VERIFY at the pinned version at build time | out-of-process service; digest-pinned (§4.4) |
| `openfga-sdk` (Python) | **Apache-2.0** — verify; its closure (aiohttp/urllib3/dateutil/aenum) is permissive but MUST pass the C4 gate at lock time | pinned + hash-verified in uv.lock |

**Adopt-first (owner directive), with a recorded fallback:** if the SDK's closure trips the C4 gate or drags conflicting pins (the dlt/RAGAS lesson), the fallback is a ~5-endpoint httpx client inside `_client.py` (`/stores`, `/authorization-models`, `/check`, `/list-objects`, `/write`, `/read`) — the `_client.py` chokepoint makes the swap invisible to the rest of the module. Telemetry: OpenFGA server phones nowhere (C1); verify at build and disable playground/metrics-export flags explicitly.

### 4.3 Settings

```python
class RebacSettings(NeSettings):                      # layers init > NE_ env > TOML (config.py:5-8 pattern)
    enabled: bool = False                             # optional module; False = filter identical to today
    api_url: str = "http://openfga:8081"              # validated in-VPC at construction (sovereignty.py)
    store_id: str = ""                                # from bootstrap
    authorization_model_id: str = ""                  # PINNED; every call passes it; empty+enabled = error
    api_token: SecretStr = SecretStr("")              # preshared key between ne-api and openfga
    timeout_s: float = 3.0
    unavailable_mode: Literal["degrade", "deny"] = "degrade"   # §3.6
    max_shared_documents: int = 10_000                # ListObjects page-through cap; overflow truncates
                                                      # (over-restriction direction) + emits a signal
```

`enabled=True` with missing store/model id fails at construction (model_validator, mirroring config.py:65-74). Settings join `freeze_platform_config` `extra_modules` at the composition root (ne-api-service-spec.md:285).

### 4.4 Compose service (C11)

Add to `deploy/compose/pilot/compose.yaml` (and dev), `profiles: ["rebac"]` — mirroring the `opa` service shape (compose.yaml:148-175: loopback-only port, `no-new-privileges`, `cap_drop: ALL`, mem/cpu limits, healthcheck) and the C11 discipline "every image is tag@sha256 digest-pinned" (compose.yaml:12):

```yaml
  openfga:
    image: openfga/openfga:<version>@sha256:<digest>   # builder resolves + pins at build time (C11)
    profiles: ["rebac"]
    command: ["run", "--playground-enabled=false"]
    environment:
      OPENFGA_DATASTORE_ENGINE: postgres
      OPENFGA_DATASTORE_URI: postgres://ne:${NE_PILOT_POSTGRES_PASSWORD}@postgres:5432/openfga?sslmode=disable
      OPENFGA_AUTHN_METHOD: preshared
      OPENFGA_AUTHN_PRESHARED_KEYS: ${NE_PILOT_OPENFGA_API_TOKEN:?set via the config bundle}
    ports: ["127.0.0.1:8081:8080"]     # in-network only; never off-box (C2)
    depends_on: { postgres: { condition: service_healthy } }
    security_opt: ["no-new-privileges:true"]
    cap_drop: ["ALL"]
    healthcheck: { test: ["CMD", "/openfga", "healthcheck"] }   # or wget /healthz per image contents
```

- `openfga` database added to the postgres initdb scripts (compose.yaml:87 pattern — same instance, separate database; single-tenant C10 unaffected).
- `openfga migrate` runs as a one-shot init step; `bootstrap.py` then writes `model/neutron.fga`, records `store_id`/`authorization_model_id` into the config bundle, and VERIFIES on every process start that the pinned model id resolves to a model whose types/relations equal the in-repo file (fail-closed on drift).
- AIR_GAP: OpenFGA is in-VPC — alive under AIR_GAP like OPA/Qdrant (C2/C3); `sovereignty.py` rejects any non-in-VPC `api_url`.

### 4.5 C3 import contract (.importlinter)

- Add `ne.permissions_rebac` to `root_packages` (.importlinter:2-27).
- New contract `c3-openfga-sdk-chokepoint` (exact mirror of `c3-mcp-sdk-chokepoint`, .importlinter:72-104): `openfga_sdk` forbidden from every `ne.*` module, `ignore_imports = ne.permissions_rebac._client -> openfga_sdk`.
- ne-brain's additive changes (§2.2/2.3) import NOTHING new — the resolver is a package-local Protocol; the existing no-cycle rule (trimming.py:20-37) is preserved.

---

## 5. Tests — the no-bypass suite (DoD)

Unit tests run against `testing/fakes.py` (in-memory tuple store); integration tests against a real digest-pinned OpenFGA via testcontainers where the host allows (else CI service job); the composition tests extend the NE-043 provable-trimming suite and the C7 guardian.

- **T-1 fail-closed absence:** principal with no source-ACL match and no tuple → `search()` empty AND `retrieve(chunk_id)` empty for a KNOWN chunk id. **T-1b:** store-unknown/tombstoned principal with a live `user:` tuple → rebac leg ∅ (§3.7).
- **T-2 exact-grant share:** share doc D to `user:jane` (source ACL excludes her): Jane retrieves D's chunks on BOTH paths; a same-group non-shared colleague still cannot; Jane still cannot see any OTHER doc (the grant is per-object, no widening); Jane's `acl`-leg labels are unchanged (share adds no labels).
- **T-3 revocation:** delete the tuple → next query hides D on both paths (no cache); group-share revocation via NE-042 membership removal → hidden after the store edge is gone (contextual-tuple derivation, no OpenFGA write needed).
- **T-4 union semantics proven:** matrix over (source-leg ∈ {grant, deny}) × (rebac-leg ∈ {grant, deny}) → visible iff either grants; PLUS: rebac tuples for a doc the source ALREADY grants change nothing; disabled-module filter output is byte-identical to today's (`{"acl": [...]}`).
- **T-5 AND-composition intact:** caller filter ∧ `$any`-carrying ACL filter — caller constraints still intersect (a caller filter can only narrow, never widen, both legs); regression against retriever.py:106-160 semantics.
- **T-6 reserved-namespace negatives:** share to subject `acl:tenant-all` / `group:acl:public-link` / object containing `acl:` → rejected before any client call; a token whose groups claim contains `acl:tenant-all` produces NO contextual tuple for it and gains no shared docs from it.
- **T-7 grammar smuggling negatives:** caller filter containing `$any` (or any `$`-key) → `ValueError` from `and_combine_filters`; hook-emitted `$any` with an empty rebac branch is never emitted (collapses, §2.2); a `document_id` VALUE equal to a label string cannot cross into the `acl` condition (field isolation).
- **T-8 server-down fail-closed:** OpenFGA unreachable/timeout/500 — `degrade`: results = source-leg-only (never full corpus, never error-masked-as-empty-trimming), `rebac.check.degraded` signal emitted; `deny`: query raises; `share()`/`revoke()` raise in both modes. NO mode yields a superset of the healthy-server result set.
- **T-9 audit completeness:** `share()` with a failing sink → no tuple written, error raised; successful share/revoke → PDP audit event AND `rebac.share.*` event present; `ShareService` has no sink-less/pdp-less constructor (constructor signature test).
- **T-10 share-requires-read + PDP gate:** a sharer who cannot see D (neither leg) gets a rejection before the PDP call; a PDP DENY blocks the tuple; REQUIRE_APPROVAL routes to the gate and writes nothing until approved.
- **T-11 chokepoint/guardian:** the existing C7 guardian suite green with module disabled AND enabled; `retrieve()`-by-id cannot fetch what `search()` hides in every T-1..T-4 state (NE-043 DoD inherited); no code path constructs a Qdrant query bypassing `_build_filter`.
- **T-12 structural:** importlinter contracts (§4.5) green; `ne-connect` contains no openfga import (I-4); written authorization model equals `model/neutron.fga` exactly (no deny operators); model-id pin mismatch at startup → construction error; C4 license check green at the locked SDK version; compose image digest-pinned (existing C11 CI lint).

---

## 6. Decomposition, sequencing, open questions

### 6.1 Decomposition — split the M row into three units

SPEC sizes NE-087 as one M, but it crosses two lanes (ne-brain seam + new package) and Checkpoint D proved cross-seam composition is where C7 bugs live. Split:

- **NE-087a (S, ne-brain lane):** async-capable `acl_filters` + `SharedDocumentResolver` Protocol + `$any` OR-group in `_build_filter`/`and_combine_filters` (with the T-5/T-7 rejection tests) + `PrincipalAclFilter(shared_docs=...)`. Pure additive; mergeable and inert on its own (no resolver exists yet → behavior identical).
- **NE-087b (M, ne-permissions lane):** the `ne-permissions-rebac` package — `_client`, resolver (contextual tuples + `is_known` gate + degrade/deny), `ShareService`, bootstrap/model-pin, gc, settings, sovereignty, fakes, T-1..T-3/T-6/T-8/T-9/T-10 against the fake + testcontainer.
- **NE-087c (S, deploy/ne-api lane):** compose service + initdb + digest pin + composition-root wiring in ne-api (+ the share endpoints per O-4) + the end-to-end no-bypass suite (T-4/T-11 against the real stack) + docs.

087a ∥ 087b are parallelizable (087b unit-tests against the Protocol shape); 087c requires both.

### 6.2 Sequencing

The ne-permissions/ne-govern lane is DISJOINT from the ne-api/ne-brain critical path (NE-104a in flight) with one exception: **087a touches ne-brain** — schedule it when the ne-brain lane is free, or immediately (the diff is small and additive: retriever await-branch, filter grammar, trimming hook kwarg). 087c's composition-root wiring lands as a rider on/after NE-104 (the ne-api root is where `PrincipalAclFilter` is constructed — ne-api-service-spec.md:108). Phase-7 parallel-lane authorization applies (NE-087 listed "buildable NOW").

### 6.3 Open questions / risks

- **O-1 (owner-visible policy):** union means a platform share can exceed source-system permissions — the module default is `enabled=False`, and Rego can deny `rebac.share` per resource class; confirm per-engagement that this is contractually acceptable, and whether some label classes (e.g. compliance-restricted sources) must be unshareable platform-wide (a Rego deny-list shipped as a template).
- **O-2 (default failure mode):** `degrade` vs `deny` default (§3.6). Spec default = degrade (availability of the primary leg); flag to owner — a deployment whose workflows DEPEND on shares may prefer deny.
- **R-2 (latency/scale):** one `ListObjects` round-trip per query (both paths) with no cache in v1 (I-6 revocation immediacy). In-VPC single-digit-ms expected, but a heavy sharer approaches `max_shared_documents`; if p95 hurts, add a short (≤5s) TTL cache as an EXPLICIT SLA change (documented like `sync_interval_seconds`, config.py:33-36) — never silently.
- **R-3 (tuple hygiene):** GC sweep correctness (§3.7); orphan tuples are non-exploitable but must not accrete unboundedly; sweep is in 087b DoD.
- **O-4 (ne-api surface):** the share endpoints are additive to the ne-api spec's mocked-19 contract — console share UI is NOT in NE-087 scope (a later console unit); v1 exposure can be steward-roles-only via API/MCP.
- **O-5 (dual-channel revocation UX):** a subject granted by BOTH legs needs both revocations (§1.2 defense 4); the console share view must show source-leg visibility alongside shares so admins aren't surprised. Track with O-4.
- **O-6 (expiring shares):** OpenFGA tuple conditions — schema-ready, deferred; decide default TTL policy with O-1.
- **O-7 (Tier-2 AGE graph interaction):** `ne-brain-graph` is today an empty stub (packages/ne-brain-graph/src/ne/brain_graph/ contains only `__init__.py`), so NE-081/082 will build AGAINST this spec, not vice versa: graph-derived context that quotes or derives from document content MUST be trimmed by the SAME §1.2 union over the provenance `document_id`s of the facts' source chunks — i.e., the graph plane consumes the same `PrincipalAclFilter`-shaped seam (labels + shared-doc ids), never a parallel ACL system in AGE. Whether a shared document also shares its *extracted entities* (vs only its text) is an NE-081 design decision — record the default as "entity/relationship rows carry source-document provenance and are visible iff ≥1 provenance document is visible to the principal (union over provenance)".
- **O-8 (platform-native share groups):** "project X" as a non-IdP, platform-defined group (steward-curated membership) needs a governed membership surface — v2; v1 subjects are users + IdP-synced groups only (`is_known`-gated).
