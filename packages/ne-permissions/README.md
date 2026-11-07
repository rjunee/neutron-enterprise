# ne-permissions

The principal store that makes C7 ACL trimming real: principals and groups are
synced from the customer IdP, and the querying principal's group memberships
expand at **query time** so NE-043 can filter retrieval to exactly what the
caller may read at source.

## The decided ACL model (SPEC §5 NE-042 — do not re-litigate)

> Chunks are stamped with source ACLs as-is (groups + users); the querying
> principal's group memberships expand at query time; revocation lag =
> principal-sync interval (target ≤15 min); membership changes do NOT trigger
> re-index.

Concretely:

* **Chunks keep their source ACLs.** NE-044 connectors stamp each chunk with the
  principals/groups the source grants (untouched by this package except the
  NE-040 duplicate-`resource_id` union fix). A chunk ACL is never rewritten when
  a membership changes.
* **Expansion happens at query time.** `PrincipalExpander.expand(principal)`
  returns the principal id plus its transitive group ids (plus the well-known
  labels below for known principals). NE-043 matches a chunk when
  `expand(principal) & set(chunk.acl)` is non-empty.
* **Membership changes do NOT re-index.** A membership added or revoked at the
  source takes effect on the **next sync** — no chunk is re-written.

### Revocation lag = `sync_interval_seconds`

Because revocation moves only through query-time expansion, the worst-case delay
between a source revocation and it taking effect is exactly one sync interval.
`sync_interval_seconds` defaults to **900** (the ≤15-minute target) and is
configurable. Values above 900 are permitted but move the revocation-lag SLA —
they are documented here, never silently capped. The hosting process (NE-034
cron) calls `PrincipalSyncService.run_once(source)` every `sync_interval_seconds`.

## Fail-closed expansion contract (NE-CKC2)

`expand` can NEVER return an empty set (empty-means-match-all is structurally
impossible — the result always contains the principal id):

* **unknown / unsynced principal → exactly itself** (plus any verified
  token-claim groups it carries) — never `TENANT_ALL`, never widened;
* **transitive nested groups** are followed (cycle-safe, depth-guarded by
  `max_expansion_depth`);
* **tombstoned principals/groups** drop out structurally (their edges are deleted
  at tombstone time).

### Well-known ACL labels

Connectors stamp these for grants that are broader than named principals; they
resolve ONLY for store-known (synced, non-tombstoned) principals:

| Label | Constant | Connector stamps it for |
| --- | --- | --- |
| `acl:tenant-all` | `TENANT_ALL` | DOMAIN / ORG / ANYONE-in-tenant grants |
| `acl:public-link` | `PUBLIC_LINK` | ANYONE-WITH-LINK grants |

The `acl:` namespace is **RESERVED**: the OIDC sync source drops reserved-prefixed
(`role:` / `acl:`) claim groups before they become store principals or edges, and
`expand()` strips `acl:*` from both principal-supplied seeds and BFS-reachable
ids, so `TENANT_ALL` / `PUBLIC_LINK` resolve **exclusively** via the store-known
`is_known` gate — never through a forged token claim or a poisoned store edge
(NE-CKD2, Checkpoint-D finding #2).

### Reserved-prefixed principal ids (NE-FIX-IDENTITY P2-1 defense-in-depth)

If the querying principal's **id itself** is reserved-prefixed (`acl:*` /
`role:*`), `expand()` skips the store entirely and returns exactly the
non-matching `RESERVED_PRINCIPAL_SENTINEL` — it echoes neither the pid nor
`TENANT_ALL` / `PUBLIC_LINK`, so it can match no chunk ACL while preserving the
never-empty invariant. The sentinel is drawn from an **unforgeable, per-process
random** namespace (Codex P1): a fixed literal would itself be a matchable ACL
label that an opaque, attacker-influenceable chunk ACL could carry verbatim and
thereby authorize every rejected reserved principal — the entropy makes that
impossible. ne-identity already rejects such a subject at the
verifier/provisioner boundary, so a reserved pid should never reach here; this is
a belt-and-braces CKD2 guard for any residual path that constructs a principal
directly.

## Principal-id opacity

Principal ids are stored **exactly as the source gives them** (Entra object ids,
OIDC subjects) — no prefixing. This is the contract NE-044 relies on: whatever id
Graph reports for a user/group is what the connector stamps into a chunk ACL, so
the two match without translation.

## Modes

| `mode` | Source | Notes |
| --- | --- | --- |
| `oidc-claims` (default) | `OidcClaimsPrincipalSource` | Consumes ne-identity's already-verified `TokenClaims`; never verifies tokens itself. Observation-driven (never tombstones). |
| `entra` | `EntraPrincipalSource` | Thin-httpx Microsoft Graph app-only sync (groups/delta + per-group **direct** member enumeration, delta-link-before-full ordering). Direct edges are stored and the transitive closure is computed at query time so a delta's per-group recompute never leaves a stale ancestor grant. Egress — unconstructable under `AIR_GAP`. |
| `fixture` | `FixturePrincipalSource` | Zero-network recorded snapshots for CI. |

### Configuration reference (`PermissionsSettings`, `NE_`-prefixed env)

| Setting | Default | Meaning |
| --- | --- | --- |
| `mode` | `oidc-claims` | Source selector. |
| `database_url` | `sqlite:///ne_permissions.db` | Store DB (SQLite-CI / Postgres-parity). |
| `sync_interval_seconds` | `900` | **Revocation lag** (see above). |
| `groups_claim` | `groups` | Token claim carrying group ids (oidc-claims). |
| `max_expansion_depth` | `32` | BFS depth guard. |
| `fixture_path` | `None` | Fixture JSON (fixture mode). |
| `mapping_path` | `None` | Static ACL mapping JSON applied explicitly with `ne permissions mapping apply`; composes with every mode. |
| `entra_tenant_id` / `entra_client_id` / `entra_client_secret` | `""` | Entra app-only credentials (secret is a masked `SecretStr`). |
| `graph_base_url` / `login_base_url` | Graph v1.0 / login.microsoftonline.com | Config-supplied, validated fail-closed against the allowed-host allowlists. |
| `graph_allowed_hosts` / `login_allowed_hosts` | commercial + sovereign clouds | Exact-match allowlists (US Gov, DoD, China clouds included). |

### Sync hardening

Two fail-closed guards protect the Entra sync (bulletproofing 2026-07-10):

* **Per-request Graph link re-guard + page cap.** Construction-time validation
  only covers the two base URLs. Every URL actually fetched — a response-supplied
  `@odata.nextLink` / `deltaLink` or a persisted delta cursor read back from
  `ne_permissions_sync_state` — is re-validated against the **single** configured
  Graph host **and port** before the app-only token is attached (https-only, no
  userinfo / IP-literal / packed-IPv4 / cloud-metadata host, exact host pin, so a
  link to a *different* sovereign cloud is refused too, and a same-host link on a
  *different* port cannot receive the token). Persisted cursors are additionally
  re-guarded on read so a poisoned row is rejected before any I/O. Pagination is
  capped at `_MAX_PAGES` (10 000) and **raises** rather than truncating, so a
  cyclic `nextLink` cannot loop forever and a partial full sync can never be
  applied (truncation would mass-tombstone).
* **Source-scoped full-sync stale detection.** A full sync tombstones only stale
  principals **of the syncing source** (`all_principal_ids(source=name)`), so an
  Entra full sync can no longer tombstone SCIM-provisioned principals or
  cascade-delete their edges.

## NE-043 integration

`expand` is deliberately **sync** (pure indexed reads). NE-043 calls it from its
retrieval path to build the Qdrant should-match-any ACL filter:

```python
labels = expander.expand(principal)          # frozenset[str], never empty
# Qdrant: match a chunk whose `acl` field shares ANY label with `labels`.
```

If NE-043 needs an async surface, wrap it in `asyncio.to_thread` (that wrapper is
NE-043's, not pre-built here).

## Auditing

Every `run_once` emits exactly one config_hash-stamped
`permissions.principal_sync` `AuditEvent` (success, or error then re-raise) plus
one `permissions.membership_change` event per member whose direct group set
changed, through the frozen `AuditSink`.

## Single-tenant (follows from C1)

PipesHub's multi-tenant `orgId` dimension is deliberately dropped — there is no
tenant column anywhere. One deployment = one customer.
