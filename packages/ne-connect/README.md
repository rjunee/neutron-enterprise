# ne-connect

The **connector framework** (NE-040): a dlt-based foundation for building source
connectors that feed the governed NE-023 knowledge-write path. This package ships
the FRAMEWORK, not the individual connectors — Microsoft 365 / SharePoint,
Salesforce, Slack, SQL, etc. are NE-044/045 and build on top of what is here.

## Architecture

```
Connector (frozen Protocol)  ──▶  SyncRunner  ──▶  KnowledgeWriter (NE-023)  ──▶  QdrantChunkStore
      │                              │  ▲
      │ discover/backfill/           │  │ AclMapper.resolve (fail-closed)
      │ incremental/permissions      │  │ OdcsDataContract.validate (ODCS schema)
      ▼                              ▼  │
 records + ACL entries       ConnectorStateStore (sqlite3: cursors + dedup)
```

### OdcsDataContract + the fail-closed permission model

`OdcsDataContract` implements the frozen `ne.contracts.integration.DataContract`
Protocol: `source`, `owner`, a JSON-Schema `schema_def` (proven well-formed at
construction via `Draft202012Validator.check_schema` — a malformed schema fails
LOUD), an ISO-8601 `freshness_sla`, a `PiiClass`, and a TYPED
`PermissionModelSpec` (the NE-042/043 permission seam, exposed as
`.permissions`; the Protocol's `pii_class` / `permission_model` return the plain
strings the frozen surface declares).

The permission model is **fail-closed by construction** (the NE-CKC2 lesson):

- `PermissionModelSpec(mode=PUBLIC_WITHIN_TENANT, default_acl=())` is a
  `ValidationError` — an unlabeled-record configuration is *unrepresentable* at
  the type level.
- `AclMapper.resolve()` has **no code path that returns an empty ACL**: an
  unresolvable ACL raises `AclResolutionError` (fail-closed; the record is never
  written). A parametrized invariant test proves the never-empty guarantee.

### dlt confinement (C1 — no phone-home)

Every dlt import goes through the single chokepoint `ne/connect/_dlt.py`, which
forces `RUNTIME__DLTHUB_TELEMETRY=false` by a **plain assignment BEFORE**
`import dlt` (dlt reads it at import time). This mirrors ne-gateway's
`_litellm.py` pattern. Importing `ne.connect` itself does **not** import dlt —
the framework import stays cheap and the telemetry ordering stays explicit.
Three tests guard this: a fresh-interpreter subprocess proof, a source-order
check, and a grep asserting `_dlt.py` is the sole import site. (Adding a
`dlt`-forbidden contract to `/.importlinter` is orchestrator follow-up.)

### State store (cursor semantics)

`ConnectorStateStore` is stdlib `sqlite3` only — the state store itself never
uses sqlalchemy. (Historical note: earlier versions of this README claimed "no
sqlalchemy anywhere under `src/`"; that is now scoped — the NE-046 SQL connector
subpackage, `ne/connect/sql/`, uses sqlalchemy exclusively as its own source
connection layer, entirely separate from the state store below.) It owns two
tables: resumable incremental `cursors` per
`(source, source_id)` and a `dedup` content-hash ledger per
`(source, source_id, record_id)`. **Cursors are opaque strings compared
LEXICOGRAPHICALLY** — ISO-8601 UTC timestamps satisfy this. NE-044/045 connectors
MUST emit lexicographically monotonic cursors (or supply a custom mapper).

**NE-040 P2 fold (NE-044):** the dedup key gained `source_id`. A source like M365
legitimately reuses a `record_id` (a driveItem id) across drives, so keying on
`(source, record_id)` alone would let an item in one drive suppress a different
item sharing its id in another. A pre-fold on-disk ledger (two-column key) is
transparently **dropped and recreated** on open — the dedup table is a resync-safe
cache, so the only cost is one idempotent re-write on the next run (never data
loss). `cursors` are untouched by the migration.

### SyncRunner flow

`SyncRunner.run(mode="backfill"|"incremental")`:

1. build the ACL index from `connector.permissions()`, load cursor state;
2. per record — reject a same-run duplicate `(source_id, record_id)` identity
   fail-closed (two rows claiming one identity is a source data-integrity fault;
   last-write-wins would silently guess — and pre-fix, an intra-batch duplicate
   aborted the whole run in the writer); validate against the ODCS schema
   (invalid → counted and skipped, never aborts the run: NE-026 per-item
   discipline — and, when the runner holds the write-quarantine lane, PARKED
   inspectably with the **contract validator's own messages** as the reason,
   under the canonical item id so a later accepted fix clears the park);
   content-hash dedup (unchanged → skipped); resolve the ACL fail-closed;
3. buffer and flush in durable batches (`batch_size`): each flush upserts through
   the governed `KnowledgeWriter`, marks the dedup ledger and persists the
   per-source max cursor for **accepted items only** (crash-resume: prior batches
   stay durable);
4. `snapshot=True` (full-snapshot sources like the polled `csv` file): parks
   under this connector's provenance prefix whose identity did not appear in
   this run are removed — the failing source row was fixed-under-a-new-identity
   or deleted, so the quarantine tracks the source's CURRENT state (`unwound:`
   writer-rescue parks are never touched);
5. emit **exactly one** `config_hash`-stamped `connector.sync` `AuditEvent` per
   run (`outcome=success`, counts in `detail`). An `AclResolutionError` stops the
   run LOUDLY after emitting a `denied`-outcome sync event.

Records enter the brain as `CuratedKnowledgeItem`s: ACL-stamped, provenance-class
`ingested`, PII class in metadata, `item_id` a hex SHA-256 (never contains `:`,
respecting the KnowledgeWriter spine-namespace rejection).

## Sync runner (NE-INGEST — the P1-4 execution surface)

`ne.connect.runner` is the import-cheap composer that BOTH the ne-worker connector-sync
cron (`ne.worker.ingest`) and the `ne connect sync` CLI build on. It only INVOKES the
existing `run_<source>_sync` entrypoints — it reimplements no connector logic.

- **`SYNC_SOURCES`** — a registry of the 9 pull sources
  (`m365`/`teams`/`slack`/`jira`/`zendesk`/`salesforce`/`sql`/`fixture`/`csv`) as lazily
  resolved dotted `"module:attr"` refs (settings class + `run_*_sync`). Resolution is
  via `importlib`, so importing `ne.connect.runner` drags neither `dlt` nor the sql
  connector (the dlt-confinement guardian asserts this). **`cdc` is deliberately
  absent** — NE-084 CDC is a PUSH-based Debezium webhook sink (`ne.connect.cdc`), not a
  pull sync; it has no `run_*_sync` and is governed via the ne-api `/cdc` route.
- **`ConnectSyncSettings`** (`NE_CONNECT_SYNC_*`, `extra='forbid'`): `sources`
  (JSON array), `cron` (default `"0 * * * *"`), `cron_overrides` (JSON per-source),
  `state_path`, `quarantine_path`, `kpi_tag`. Fail-loud validators: every source must
  be a registry key, no duplicates, `cron_overrides` keys ⊆ `sources`, and
  state/quarantine paths are REQUIRED once any source is set. `cron_for(source)`
  resolves the override then the default.
- **Per-connector env prefixes** — each pull source's settings class reads its OWN
  namespace (matching the platform convention): `NE_JIRA_*` / `NE_SALESFORCE_*` /
  `NE_SLACK_*` / `NE_SQL_*` / `NE_TEAMS_*` / `NE_ZENDESK_*` / `NE_M365_*` (fixture:
  `NE_CONNECT_FIXTURE_*`; csv: `NE_CONNECT_CSV_*`). Bare `NE_<FIELD>` vars do not bind any connector, and
  `NE_TEAMS_*` (connector) is disjoint from ne-chat's `NE_TEAMS_CHAT_*` (env binding is
  prefix + exact field name).
- **`fixture` source** (`ne.connect.fixture_sync`, `NE_CONNECT_FIXTURE_*`): the
  production-usable recorded-fixture sync (first-seed/demo/NE-077 'Acme corpus
  ingested'), wrapping the zero-network `FixtureConnector`. Fail-loud on empty
  `path`/`owner`; always `backfill` (re-sync is dedup-idempotent).
- **`csv` source** (`ne.connect.csvfile`, `NE_CONNECT_CSV_*`): a POLLED
  spreadsheet export — the file is a full snapshot re-read on every cron fire,
  never a one-time import. Values stay strings; SURROUNDING whitespace and
  zero-width artifacts (NBSP, tab, CR, ZWSP/BOM) are trimmed from every header
  and cell before validation — nothing inside a value is ever altered, and a
  whitespace-only cell becomes the empty string, still present (undecodable
  bytes surface as visible `\xNN` escapes and fail the schema);
  the ODCS row contract is the operator-owned JSON-Schema at
  `NE_CONNECT_CSV_SCHEMA_PATH`, so every quarantine reason is the contract
  validator's own message. Contract-invalid rows and same-run duplicate
  identities are PARKED in the write quarantine (see the bridge notes below);
  an unchanged file dedups to a no-op; a fixed row's accepted write clears its
  park; a deleted row's park reconciles away (`snapshot=True`). Permission
  model is `public-within-tenant` over the REQUIRED `NE_CONNECT_CSV_DEFAULT_ACL`
  (a sheet has no source ACL system — the audience is declared, never implied).
- **`build_ingest_stack(...)`** builds the governed `KnowledgeWriter` over the composed
  `ChunkStore` — EXACTLY the ne-api `_build_cdc_service` recipe (`CONNECTOR_RECORD_SHAPE`,
  `ProvenanceClass.INGESTED`, the shared audit sink + an `OutboxShapeViolationSink`), so
  cron + CLI syncs write through the EXISTING governed path (C7 fail-closed ACLs).

## Conformance kit + recorded-fixture CI (for NE-044/045)

`ne.connect.testing.kits.connector.ConnectorConformanceKit` ships with the
framework. Subclass it, override `create_connector` / `create_contract` /
`incremental_state`, and get six tests for free — including
`test_zero_network_replay`, which drains discover/backfill/permissions under a
disabled `socket.socket`, proving **recorded-fixture CI mode needs no live
tenant**.

`ne.connect.testing.fixture_connector.FixtureConnector` replays a committed JSON
(or in-memory) fixture with zero network I/O and is registered under the
`ne.plugins.connector` entry point as `fixture`. Fixture shape:

```json
{
  "source": "acme-crm",
  "objects": [{"source_id": "acme-crm/contacts", "kind": "resource", "name": "contacts"}],
  "records": [{"record_id": "c1", "source_id": "acme-crm/contacts",
               "cursor": "2026-01-01T00:00:00Z", "payload": {"name": "Ada"}}],
  "acl": [{"resource_id": "acme-crm/contacts", "principals": ["oidc:sales"]}]
}
```

Use `dump_fixture(...)` to author fixtures. **SANITIZATION IS THE RECORDER'S
JOB** — committed fixtures carry innocuous fake data only, never credentials or
PII (plan §1.4 secrets hygiene).

## C2 — source-URL validation

A connector that takes a source base URL from config MUST call
`validate_source_url(url, ...)` at construction (`FixtureConnector` takes no URL,
so it is exempt; NE-044/045 briefs inherit this requirement). In-VPC sources are
accepted (including under `AIR_GAP`); external sources require `https`, a real
DNS name (no IP literal / packed-IPv4), and carry no credentials / query /
metadata host. Error messages name only the parsed hostname (never the full URL).

**NE-CKC10 note:** `endpoints.py` **consumes** the existing validated path
`ne.brain.sovereignty.validate_in_vpc_endpoint` rather than minting a third
sovereignty mirror. When the orchestrator hoists a shared sovereignty validator
into a neutral package (NE-CKC10), this module becomes a thin consumer of it.

## Dependency note (ADR candidate)

`ne-brain` is a runtime dependency (the framework consumes `CuratedKnowledgeItem`
/ `KnowledgeWriter` from it), which drags qdrant-client / fastembed / sqlalchemy
into ne-connect's closure. This is accepted deliberately — the ne-ingest process
ships ne-brain anyway — but is flagged here should the orchestrator later prefer
hoisting `CuratedKnowledgeItem` into ne-contracts (ADR candidate).

## M365 / SharePoint connector (NE-044)

`ne.connect.m365` is a thin-`httpx` Microsoft Graph app-only connector (NO
`msgraph-sdk` — NE-042 `entra.py` precedent). It delta-syncs SharePoint/OneDrive
driveItems, fetches and parses supported file bodies through the existing
`ne-brain` `DoclingParser`, and resolves each item's permissions into fail-closed
`AclEntry` rows feeding NE-042/043 trimming. `run_m365_sync(settings, writer=…,
state_store=…, audit_sink=…, actor=…)` drives the resulting body-bearing Records
through the framework `SyncRunner` and governed `KnowledgeWriter`.

### Config (`M365ConnectorSettings`)

```toml
tenant_id = "…"                # Entra tenant (GUID)
client_id = "…"                # app registration (GUID)
auth_method = "certificate"    # or "secret"
certificate_pem_path = "/secrets/graph-cert.pem"   # certificate mode
private_key_pem_path = "/secrets/graph-key.pem"    # certificate mode
# client_secret = "…"          # secret mode (SecretStr) instead of the PEM pair
site_ids = ["site-eng"]        # explicit scope — at least one of site_ids/drive_ids
# drive_ids = ["drive-1"]      # (NO tenant-wide crawl in v1)
requests_per_second = 4.0      # token bucket (conservative default estimate)
burst = 8
max_content_bytes = 26214400 # metadata + streamed-body ceiling (25 MiB)
max_content_redirects = 3    # bounded manual redirect following
# Exact host(s) Graph may redirect /content to; bare DNS names only.
content_allowed_hosts = ["contoso.sharepoint.com"]
```

Certificate mode signs an RS256 **client assertion** (JWT with `x5t`/`x5t#S256`
thumbprints) — no shared secret leaves the process. Both `graph_base_url` and
`login_base_url` are validated at construction (C2: https, allowlisted hosts, no
IP/packed-IPv4/metadata) and every followed `@odata.nextLink`/`deltaLink` host is
re-checked, so a hostile Graph response cannot redirect the crawl off-host.
Egress under `AIR_GAP` is refused before any I/O. The client secret / private key
appear only in the token POST body or local signing — never in logs, exceptions,
URLs, or fixtures.

### File-body ingestion

For PDF, DOCX, PPTX, HTML, and Markdown files whose Graph MIME type agrees with
the file suffix, the connector derives the canonical
`/drives/{drive}/items/{item}/content` URL locally. It never reads or persists
`@microsoft.graph.downloadUrl`. Graph's normal pre-authenticated redirect is
followed manually only when its origin passes the full SSRF guard and its exact
host appears in `content_allowed_hosts`; the Graph bearer header is stripped
before that request. Redirect count, Graph's declared item size,
`Content-Length`, and streamed bytes are all bounded before the ephemeral file
reaches Docling.

The emitted Record preserves the existing driveItem metadata (`id`, `name`,
`web_url`, timestamps, size, MIME, path, eTag) and adds `body`, Docling structural
`body_blocks`, the binary SHA-256, source format/schema, and
`body_ingestion_status="parsed"`. The existing Record→SyncRunner→KnowledgeWriter
path retains the exact source ACL mapping and supplies governed provenance,
deduplication, audit, and durable delta semantics.

Unsupported file types remain explicit metadata-only records with
`body_ingestion_status="unsupported"`; they never pretend body ingestion
succeeded. For a supported file, an invalid/oversized metadata size, unsafe or
excessive redirect, failed/oversized response, parser failure, empty body, or
parser identity/format mismatch aborts the crawl. No Record is emitted and no
deltaLink advances, so retry/resume cannot convert a body failure into a false
metadata-only success.

### ACL mapping (the security core — fail-closed)

| Graph permission | → principal label |
| --- | --- |
| direct `grantedToV2.user.id` / `.group.id` | raw Entra GUID |
| `link.scope == "anonymous"` | `acl:public-link` (`PUBLIC_LINK`) |
| `link.scope == "organization"` | `acl:tenant-all` (`TENANT_ALL`) |
| `link.scope == "users"` | named identities from `grantedToIdentitiesV2` |
| `link.scope == "existingAccess"` | **skipped** (grants nothing new) |
| `siteGroup`/`siteUser` with an Everyone claim (`spo-grid-all-users`, `c:0(.s\|true`, "everyone except external") | `acl:tenant-all` |
| `siteGroup`/`siteUser` whose `loginName` is a claims-encoded Entra object id (`c:0t.c\|tenant\|<guid>`, `c:0o.c\|federateddirectoryclaimprovider\|<guid>`) | raw Entra GUID |
| `siteGroup`/`siteUser` with any other `loginName` | **dropped with reason** |
| `application`/`device` identity | **dropped** |
| a **source-forged** `acl:…`/`role:…` id | **tainted → quarantine** |

`TENANT_ALL`/`PUBLIC_LINK` are byte-identical to `ne.permissions.models` (a
dev-dep test locks the equality — NE-CKD2 reserved-namespace consistency). An
item that is **tainted** or resolves to **zero principals** is **quarantined**:
no record, no `AclEntry`, counted and surfaced as a `connector.acl_quarantine`
`denied` audit event — never emitted world-readable. Quarantine also covers a
**non-retryable per-item permissions-fetch failure** (a `4xx` that is not `429`,
or a `410` on the permissions URL — the item is gone): that one item is
quarantined and the crawl continues, so a persistent `403`/`404` can no longer
wedge all progress. A `429`-exhaustion or any `5xx` on a permissions fetch, by
contrast, still **aborts** the run and commits **no** deltaLink. The contract is
`SOURCE_ACL` with an **empty** `default_acl` as the backstop: any item that
somehow reached the runner unlabeled aborts the run via `AclResolutionError`
rather than defaulting open.

### Delta / resume semantics

Per-drive `deltaLink` tokens are persisted as `delta:{drive_id}` cursors and
committed **only after a successful run**. A run with quarantined items (including
per-item permissions-fetch quarantines) **still commits** its deltaLink — progress
is durable and one wedged item cannot stall the crawl. A throttled-out, `5xx`-, or
crashed run keeps the old link; the re-run re-enumerates from it and the dedup
ledger skips already-written items — durable, loses nothing, writes no duplicates.
A `410 Gone` (expired token) on a **delta** URL triggers an in-run **full-delta
resync** for that drive (the `Location` header is deliberately not followed). The
first run is a full backfill; subsequent runs auto-select incremental off the
persisted ledger.

Every followed `@odata.nextLink` / `deltaLink` **and** every persisted delta
cursor is re-validated against the full external egress policy before any
token-bearing request — https-only, no embedded userinfo, no IP-literal /
packed-IPv4 / cloud-metadata host, plus the host allowlist — so a hostile Graph
response can neither downgrade the crawl to cleartext, smuggle credentials, nor
redirect it off-host (the guard is the first statement of every Graph GET, so a
rejected link never even triggers a token POST).

### Adaptive throttling

A token bucket (`requests_per_second`/`burst`) paces requests; `429`/`503`/`504`
retry honoring `Retry-After` (capped) or a capped exponential backoff, up to
`max_attempts` before a loud `GraphSyncError`. Defaults (`rps=4`, `burst=8`) are
**conservative estimates** to be recalibrated against a live tenant's 429 cadence
(live-parked below).

### Deferred (documented scope cuts, not omissions)

- **Deletion propagation** — `deleted`-facet items are counted and skipped; issuing
  tombstones/removals to the brain is deferred.
- **SharePoint lists / pages** — only driveItems are crawled in v1.

### LIVE-PARKED: real-tenant runbook

`fixture-first` per NE-000 — CI runs entirely on recorded Graph JSON via
`httpx.MockTransport` (zero sockets). To exercise a real dev tenant:

1. Register an Entra app; grant **application** permissions `Sites.Read.All` +
   `Files.Read.All` and grant admin consent.
2. Upload a certificate (or create a client secret); set `tenant_id`/`client_id`
   and the cert PEM paths (or `client_secret`).
3. Set `site_ids`/`drive_ids` to the scope under test and run `run_m365_sync`.

Real-tenant e2e and live 429-cadence calibration are **env-gated, not required for
merge** — SharePoint permission payloads vary by tenant age (siteUser loginName
claim formats especially); the fail-closed drop/quarantine design under-grants or
quarantines unknown shapes rather than ever over-granting, and the live run will
recalibrate the fixtures.

The two claims encodings in the table above are the documented SPO forms whose
value segment IS an object id, and they are matched STRICTLY: the full prefix,
plus a syntactically valid GUID as the ENTIRE remaining value. Two documented
neighbours are deliberately NOT resolved — `i:0#.f|membership|<upn>` (the Entra
user form, whose value is a UPN, not an object id) and the M365 group **owners**
variant that suffixes the guid (`<guid>_o`), which would otherwise widen an
owners-only grant to the whole membership. Both keep falling through to
**dropped**. This is a false-negative fix only: the mapper can now read an id the
payload already carried, so it can never admit a principal the tenant's own ACL
did not name.

## SQL connector (NE-046)

`ne.connect.sql` is a config-driven connector over dlt's core `sql_database`
source (Postgres + MSSQL, v1), reached ONLY through the `ne.connect._dlt`
chokepoint (`sql_table` / `incremental` are imported from there — no second dlt
import site). **Importing `ne.connect.sql` imports dlt/sqlalchemy**; it is
therefore NOT re-exported from `ne.connect`'s package `__init__` — import it
explicitly: `from ne.connect.sql import ...`.

### Config example

```toml
[sql]
connection_url = "postgresql://svc_reader@db.internal:5432/crm"  # SecretStr
owner = "data-eng@acme.test"
source_name = "sql-crm"
allowed_hosts = ["db.internal"]  # or rely on the in-VPC/`.internal` default policy

[[sql.tables]]
table = "docs"
id_column = "id"
content_columns = ["title", "body"]
metadata_columns = ["updated_at"]
cursor_column = "updated_at"
cursor_type = "string"
row_filter = "status = 'published'"
[sql.tables.acl]
principals = ["g-eng"]  # static ACL mode: every row in this table is g-eng-visible

[[sql.tables]]
table = "notes"
id_column = "id"
content_columns = ["note"]
cursor_column = "id"
cursor_type = "int"
[sql.tables.acl]
principal_column = "owner_principal"  # per-row ACL mode
principal_separator = ","             # "u-ada,g-eng" -> ("u-ada", "g-eng")
```

Or via init kwargs:

```python
from ne.connect.sql import SqlConnectorSettings, SqlTableAclConfig, SqlTableConfig

settings = SqlConnectorSettings(
    connection_url="postgresql://svc_reader@db.internal:5432/crm",
    owner="data-eng@acme.test",
    tables=(
        SqlTableConfig(
            table="docs", id_column="id", content_columns=("title", "body"),
            acl=SqlTableAclConfig(principals=("g-eng",)),
        ),
    ),
)
```

### Fail-closed ACL (the security core — "no config != public")

SQL rows carry **no native per-row ACL** — unlike a Graph driveItem, there is no
permission payload to crawl. `SqlTableAclConfig` is therefore a **required**
field on `SqlTableConfig` with no default: a table configured without an `acl`
block is **unrepresentable** — pydantic rejects it at construction, so the
fail-open "no config means public" path can never exist at the type level.

Two ACL modes, mutually exclusive (XOR, enforced at construction):

- **static** (`principals=(...)`) — every row in the table gets the same,
  operator-declared ACL.
- **per-row** (`principal_column=...`, optional `principal_separator=...`) —
  each row's ACL comes from a named column's VALUE, resolved by
  `ne.connect.sql.acl.resolve_row_principals`.

A per-row value is **quarantined** (no `Record`, no `AclEntry`, counted, surfaced
as one `connector.acl_quarantine` `denied` audit event per run — never emitted
world-readable) whenever it resolves to **zero principals** (`NULL`/empty) OR is
**tainted**: a non-string item in a list value, or ANY principal starting with a
reserved namespace (`acl:`/`role:` — NE-CKD2). **Source DATA may never mint a
reserved label** — not even `acl:tenant-all` (a row whose principal-column value
literally IS `"acl:tenant-all"` is quarantined as a forgery, not honored).
**Operator CONFIG**, by contrast, may declare EXACTLY `acl:tenant-all` as a
static principal (an explicit "this whole table is org-wide visible"
statement) — no other reserved label is ever accepted in config either.

The ODCS contract backstop is `SOURCE_ACL` with an **empty** `default_acl`: if an
unlabeled record ever somehow reached the `SyncRunner`, `AclMapper` raises
`AclResolutionError` and aborts the run LOUDLY rather than defaulting the record
open.

**SQL ACL is coarse and config-driven by nature (a v1 limitation)**: there is no
per-row grant list to discover, only what the operator declares. This is
qualitatively different from the M365 connector's crawled, per-item Graph
permissions — document this to customers configuring SQL sources.

### Table -> document mapping

Deterministic, config-ordered document shaping (`ne.connect.sql.sync.make_sql_record_mapper`):
`content` joins `content_columns` in configured order with `"\n\n"` (a `NULL`
column is skipped, never stringified as `"None"`; an all-`NULL` row therefore
yields `""`, which the writer's shape contract — `minLength: 1` — quarantines,
counted in `report.quarantined`, not silently dropped). `metadata` carries the
configured `metadata_columns` (non-`str`/`int`/`float`/`bool` values are
stringified) plus the connector-stamped `connector` / `source_id` / `record_id`
/ `pii_class` / `table` keys — a `metadata_columns` entry colliding with any of
those (or `source`/`provenance`) is rejected at config-construction time.
`item_id` follows the same sha256-of-`source\nsource_id\nrecord_id` convention
as the framework's `default_record_mapper`.

### Incremental / cursor semantics

Each configured table may declare a `cursor_column` and a `cursor_type`
(`string`/`int`/`datetime`) — cursors are compared **TYPED**, never
lexicographically (an int cursor of `10` correctly sorts after `9`; a bare
string compare would get this backwards). Persisted ledger keys are
`cursor:{table_ref}` (`table_ref` = `schema.table` or bare `table`), committed
**only after a successful run** (the same durability contract as the M365
connector's `deltaLink`). An `>=` incremental boundary re-fetches the boundary
row, which the `(source, source_id, record_id)` dedup ledger absorbs as a no-op.
A table with no `cursor_column` is always fully re-scanned (dedup still
prevents re-writing unchanged rows).

**ACL-quarantined rows still advance the cursor** — a v1 limitation shared with
the "fix config, don't expect auto-resync" trade-off: if a row was quarantined
because of a config mistake (e.g. the wrong `principal_column`), fixing the
config does NOT automatically re-surface that row on the next incremental run
(its cursor position has already been passed). **Reset the state ledger**
(`ConnectorStateStore.set_cursor` to an earlier value, or drop the `cursor:*`
rows) to force a resync after a config fix.

### Dialect allowlist + C2 in-VPC host policy (`ne.connect.sql.engine`)

`create_validated_engine` is the **only** `sa.create_engine` call site under
`ne-connect` `src/` (a structural guardian test enforces this). Two fail-closed
gates, both at construction time, before any I/O:

1. **Dialect allowlist** — `postgresql`/`postgresql+psycopg` (normalized to the
   psycopg3 driver) and `mssql`/`mssql+pyodbc` (normalized to pyodbc) are
   accepted; `sqlite` is accepted ONLY when `allow_sqlite=True` (the
   fixture/test escape hatch — never a live-deployment dialect). Anything else
   (MySQL, a bare ODBC DSN, an exotic dialect) is rejected.
2. **C2 in-VPC host policy** — a DB connection is EGRESS and must be provably
   in-VPC (or explicitly allowlisted via `allowed_hosts`). This REUSES the
   consolidated NE-CKC10 validator, `ne.brain.sovereignty.validate_in_vpc_endpoint`
   (no fourth SSRF mirror is minted) — private/loopback IPs, `localhost`,
   dotless service names, `.local`/`.localhost`/`.internal` hosts, and
   `allowed_hosts` entries are accepted; cloud-metadata hosts, packed-IPv4
   forms, and external DNS names are rejected.

`connection_url` is a `SecretStr` (C12): every error this module raises names
only the dialect or the parsed hostname — never the full URL, never
credentials, never a driver's own exception args.

### Deployment prerequisites

- **Postgres** — `psycopg[binary]` is bundled; no extra system package needed.
- **MSSQL** — live connections need the SYSTEM **Microsoft ODBC Driver 18 for
  SQL Server** (`msodbcsql18`, Microsoft EULA) installed on the host/container,
  AND a system ODBC driver manager (**unixODBC** on Linux/macOS, iODBC also
  supported) — `pyodbc`'s compiled extension dynamically links against the
  driver manager's shared library even to be **imported**, so both are
  deployment (and local dev) prerequisites, never Python dependencies; neither
  ever enters `uv.lock` or the C4 license gate.
- Configured DB hosts must be in-VPC (private IP, `.internal`/`.local`, a
  dotless service name) or explicitly listed in `allowed_hosts`.

### LIVE-PARKED

Real Postgres/MSSQL e2e against a live instance is **fixture-first per §0.6** —
CI runs entirely against a file-backed SQLite fixture (`tests/sql_fixture.py`);
`test_sql_engine.py` proves both MSSQL URL forms validate without importing a
DBAPI, and proves `pyodbc` imports only on hosts where the unixODBC runtime is
present. A missing Python `pyodbc` dependency remains a hard failure. Real-tenant
e2e and MSSQL live 429/throttling calibration are env-gated, not required for merge.
## Slack connector (NE-045)

`ne.connect.slack` is a thin-`httpx` Slack Web API connector (NO `slack_sdk` —
its own urllib/aiohttp transport stack would bypass both the C2 egress guardian
and the mandated `httpx.MockTransport` fixture-first CI; the needed surface is 6
read methods). It ships as a **customer-internal app** built from a shipped
manifest, backfills + incrementally syncs channel **message text + metadata**,
and resolves each channel's membership/type into fail-closed `AclEntry` rows
feeding NE-042/043 trimming. `run_slack_sync(settings, writer=…, state_store=…,
audit_sink=…, actor=…)` drives the whole pipeline through the framework
`SyncRunner`.

### Config (`SlackConnectorSettings`)

```toml
bot_token = "xoxb-…"                      # SecretStr — the installed app's Bot User OAuth Token
owner = "data-eng@acme.example"           # ODCS data-contract owner
conversation_types = ["public_channel", "private_channel", "mpim"]  # "im" excluded by default
# channel_ids = ["C0123456"]              # empty = all listed conversations
history_requests_per_minute = 30.0        # customer-internal-app default (conservative estimate)
requests_per_minute = 15.0
history_page_size = 200
```

### Install: the shipped customer-internal app manifest

1. Go to **api.slack.com/apps → Create New App → From an app manifest**.
2. Select the target workspace, paste the contents of
   `packages/ne-connect/src/ne/connect/slack/manifest.json`, and confirm.
3. **Install to Workspace** and copy the generated `xoxb-…` Bot User OAuth Token
   into `NE_SLACK_BOT_TOKEN` (or `bot_token=` in config) — never commit it.
4. `/invite @neutron-connector` into each channel you want synced. A bot only
   ever reads channels it has been invited into — this is itself a natural ACL
   floor beneath the connector's own channel-membership mapping.

The manifest requests exactly 7 least-privilege **read** scopes
(`channels:history`, `channels:read`, `groups:history`, `groups:read`,
`mpim:history`, `mpim:read`, `users:read`) — no write scope of any kind, no
`user` token scopes, and no `im:history`/`im:read` (1:1 DMs are excluded by
default; enabling them requires editing both the manifest and
`conversation_types`). Socket Mode, org-wide deploy, and token rotation are all
off. A dev-locked test (`test_slack_manifest.py`) ratchets the manifest against
scope drift.

### ACL mapping (the security core — fail-closed)

| Channel shape | → principal label |
| --- | --- |
| public channel (`is_channel` and not `is_private`) | `acl:tenant-all` (`TENANT_ALL`) — workspace-readable **within this single-tenant deployment** (single-tenancy follows from C1 self-host: one deployment = one customer, so workspace == tenant), **never** cross-tenant |
| private channel / mpim / im | the deduped member-id tuple (`conversations.members`) |
| externally shared (Slack Connect: `is_ext_shared`/`is_shared`/`is_pending_ext_shared`) | **QUARANTINE** — out of v1 scope (membership spans organizations this deployment cannot enumerate) |
| channel type unresolvable (no recognized flag) | **QUARANTINE** |
| member list unfetchable (a non-retryable `4xx` / a non-`ratelimited` `ok:false`) | **QUARANTINE** |
| a member id in a reserved namespace (`acl:…`/`role:…`) forged by the source | **tainted → QUARANTINE** (NE-CKD2 forgery guard) |

`TENANT_ALL` is byte-identical to `ne.permissions.models` (a dev-dep test locks
the equality). It is emitted **only** by the mapper for a genuine public
channel — never lifted from a source id. A quarantined channel produces **no
record and no `AclEntry`** — counted and surfaced as a `connector.acl_quarantine`
`denied` audit event — never emitted world-readable. The contract is
`SOURCE_ACL` with an **empty** `default_acl` backstop: any message that somehow
reached the runner unlabeled aborts the run via `AclResolutionError` rather than
defaulting open. `users.list` provenance enrichment (the `user_name` field) is
**not** security-bearing — a degraded/failed fetch drops that field but never
quarantines a channel.

### Post-2026-03 rate limits: why this ships as a customer-internal app

Slack tightened rate limits for **non-Marketplace DISTRIBUTED** apps: as of
2026-03-03, `conversations.history`/`conversations.replies` are capped at
**1 request/minute and 15 objects/page** on existing installs (see
`docs.slack.dev` changelogs 2025-05-29 "Rate limit changes for non-Marketplace
apps" and 2025-06-03 "Rate limits clarity"). **Customer-internal apps are
explicitly exempt** — they retain the historical Tier-3-class limits (roughly
50+ requests/minute, up to ~1000 objects/page) — which is precisely why this
connector is shipped as a customer-internal manifest app rather than a public
one. The configured defaults (`history_requests_per_minute=30`,
`requests_per_minute=15`) are still conservative estimates for that regime; if a
deployment is ever reclassified as non-Marketplace-distributed, set
`history_requests_per_minute<=1.0` and `history_page_size<=15` to match. A
`Retry-After` header is always honored (capped), and Slack's `ok:false
error:"ratelimited"` envelope on an HTTP 200 is treated as retryable too. Per
NE-CLEAN3 (superseding NE-CLEAN1's prior run-abort policy), exhaustion of a
**per-channel** call (members-fetch or history/replies) raises
`SlackRateLimitedError` and is treated as a retryable SKIP of that one
channel — its partial records/`AclEntry` this run are rolled back, its cursor
is left untouched, and the run continues with the remaining channels (counted
in `rate_limited`/`rate_limited_ids`, never in `acl_quarantined` — a
rate-limited channel is not a permanent ACL failure and is retried in full
next run). **Run-level** calls (`conversations.list`, `users.list`) still
raise plain `SlackSyncError` and ABORT the whole run on rate-limit
exhaustion, as do genuine auth/permission errors and `5xx` exhaustion on any
call.

### Cursor / durability semantics

Per-channel `ts` cursors (`ts:{channel_id}`) are persisted and committed **only
after a successful run**. A run with quarantined or rate-limited-skipped
channels **still commits** the surviving channels' cursors — progress is
durable and one wedged or throttled channel cannot stall the crawl. A skipped
channel keeps its old cursor and is re-fetched, in full, on the next run. A
run-level abort (enumeration/`users.list` rate-limit exhaustion, `5xx`, or a
crash) keeps ALL old cursors; the re-run re-fetches the overlap (via
`oldest`) and the `(source, source_id, record_id)` dedup ledger absorbs it —
duplicate-free, no progress lost either way. The first run is a full
backfill; subsequent runs auto-select incremental off the persisted ledger.

### Deferred (documented scope cuts, not omissions)

- **Message edits/deletions of already-synced history** are not re-fetched by
  the oldest-cursor incremental crawl.
- **New replies to a thread whose parent predates the cursor window** are
  missed until the next full backfill (same deferral class as M365's deletion
  propagation).
- **Files/attachments/canvases** are never ingested — text + metadata only.
- **1:1 DMs** (`im`) are excluded by default (no `im` scope in the shipped
  manifest).
- **Slack Connect externally-shared channels** are quarantined in v1 (their
  cross-organization membership cannot be enumerated by this deployment).

### LIVE-PARKED: real-workspace runbook

`fixture-first` per NE-000 — CI runs entirely on recorded Slack Web API JSON via
`httpx.MockTransport` (zero sockets). To exercise a real dev workspace:

1. Create the app from the shipped manifest in a dev workspace (see Install
   above) and invite the bot into a few test channels (public, private, mpim).
2. Set `bot_token` (`NE_SLACK_BOT_TOKEN`) and `owner`; run `run_slack_sync` for an
   initial backfill, then again for an incremental run.
3. Observe real `Retry-After` cadence against `conversations.history` /
   `conversations.replies` and recalibrate `history_requests_per_minute` /
   `history_page_size` against the workspace's actual Tier-3 allowance.

Real-workspace e2e and live rate-limit calibration are **env-gated, not
required for merge** — real Slack payload shapes (subtype variants, shared-
channel edge cases) may diverge from the hand-recorded fixtures; the
fail-closed quarantine design never over-grants on an unrecognized shape, and
the live run will recalibrate the fixtures and defaults.

## Salesforce connector (NE-047)

`ne.connect.salesforce` is a thin-`httpx` connected-app OAuth Salesforce
connector (NO `simple-salesforce` — it is requests-based/synchronous and would
bypass both the C2 egress guardian and the mandated `httpx.MockTransport`
fixture-first CI, the same call NE-045 made for `slack_sdk`; see the
Dependency note below and the package NOTICE). It backfills a config-driven
set of SObjects via **Bulk API 2.0** query jobs, incrementally syncs off a
**`SystemModstamp`** cursor, and resolves each object's `ObjectPermissions`
rows into fail-closed, **OBJECT-LEVEL** `AclEntry` rows feeding NE-042/043
trimming. `run_salesforce_sync(settings, writer=…, state_store=…,
audit_sink=…, actor=…)` drives the whole pipeline through the framework
`SyncRunner`. Importing this subpackage does **not** import dlt (C1 stays
untouched) and adds **zero** new third-party dependencies.

### Connected-app setup

Two OAuth flows are supported (`auth_method`):

1. **`jwt_bearer`** (default) — create a connected app, upload a self-signed
   certificate under **Digital Signatures**, and pre-authorize a specific
   integration user (**Permitted Users: Admin approved users are pre-authorized**
   + assign the user via a permission set). The private key signs an RS256
   assertion POSTed to the **login** host (`login.salesforce.com` /
   `test.salesforce.com` for sandboxes) — no shared secret ever leaves the
   process.
2. **`client_credentials`** — configure the connected app's **Client
   Credentials Flow** with a designated run-as user. The token request goes to
   the **org's own My Domain** endpoint (`https://<org>.my.salesforce.com/services/oauth2/token`)
   — `login.salesforce.com` does **not** serve this grant type.

Either way, the connected app needs only the **`api`** OAuth scope — no
`refresh_token`/`offline_access`, no write scopes. The run-as/integration user
needs **Read** on every configured SObject (via profile or permission set) —
that Read grant is exactly what `ObjectPermissions` mirrors into this
connector's ACL.

### Config (`SalesforceConnectorSettings`)

```toml
auth_method = "jwt_bearer"
client_id = "3MVG9…"                       # connected-app consumer key
username = "integration-user@acme.com"     # jwt_bearer only
private_key_pem_path = "/secrets/sf-key.pem"  # jwt_bearer only
# client_secret = "…"                      # client_credentials only (SecretStr)
instance_url = "https://acme.my.salesforce.com"
owner = "data-eng@acme.example"            # ODCS data-contract owner
api_version = "v65.0"

[[objects]]
name = "Account"
content_fields = ["Name", "Description"]
metadata_fields = ["Industry"]

[[objects]]
name = "Opportunity"
content_fields = ["Name"]
metadata_fields = ["StageName", "Amount"]
```

Or via init kwargs:

```python
from ne.connect.salesforce import SalesforceConnectorSettings, SalesforceObjectConfig

settings = SalesforceConnectorSettings(
    auth_method="client_credentials",
    client_id="3MVG9…",
    client_secret="…",
    instance_url="https://acme.my.salesforce.com",
    owner="data-eng@acme.example",
    objects=(
        SalesforceObjectConfig(
            name="Account", content_fields=("Name", "Description"), metadata_fields=("Industry",)
        ),
    ),
)
```

`objects` is **required non-empty** — explicit per-object scoping, no org-wide
crawl in v1 (the same discipline as the M365/SQL connectors). Every object
name and field name is validated as a bare Salesforce API name
(`^[A-Za-z][A-Za-z0-9_]*$` — no dots) at config-construction time: this is the
**SOQL-injection guard**, since every one of these names is assembled directly
into a SOQL string by the connector. A relationship field (`Account.Name`) is
a documented v1 cut.

### Object -> document mapping

Deterministic, config-ordered document shaping
(`ne.connect.salesforce.sync.make_salesforce_record_mapper`): `content` joins
`content_fields` in configured order with `"\n\n"` (a `None`/empty-string field
is skipped — Bulk CSV renders a Salesforce `NULL` as `''`; an all-empty row
therefore yields `""`, which the writer's shape contract — `minLength: 1` —
quarantines, counted in `report.quarantined`, not silently dropped).
`metadata` carries the configured `metadata_fields` (non-`str`/`int`/`float`/
`bool` values are stringified) plus the connector-stamped `connector` /
`source_id` / `record_id` / `pii_class` / `object` keys. `item_id` follows the
same sha256-of-`source\nsource_id\nrecord_id` convention as the framework's
`default_record_mapper` — `source_id` **is** the configured object name (no
prefix parsing needed, unlike the SQL connector's `table_ref:id` scheme).

### THE HONEST ACL LIMITATION — object-level v1, not record-level

Salesforce's real sharing model composes **organization-wide defaults (OWD)**,
the **role hierarchy**, **profile/permission-set** object- and field-level
permissions, and **manual/Apex sharing rules** into a per-RECORD visibility
decision. **This connector mirrors NONE of the record-level machinery in v1**
— only "can a profile/permission-set read this SObject at all"
(`ObjectPermissions.PermissionsRead`) is mirrored, as
`sf-profile:<ProfileId>` / `sf-permset:<PermissionSetId>` principal labels.

**Concretely**: a record that a user cannot see in Salesforce because of a
restrictive org-wide default or a role-hierarchy boundary **MAY still be
retrievable through this connector** if their profile or an assigned
permission set grants object `Read` on that SObject. Record-level sharing is
**deferred to v1.1**.

**Guidance**: only sync objects whose org-wide default is public-read (or
whose intended access boundary genuinely **is** "can this
profile/permission-set read the object at all" — e.g. a shared reference/lookup
object). For anything with meaningful record-level sharing (most CRM data:
Accounts/Opportunities/Cases with owner- or territory-based visibility), treat
this connector's v1 scope as **coarser than Salesforce's own security model**
and scope `objects` accordingly.

Fail-closed regardless: an SObject whose object permissions cannot be resolved
(zero surviving grants) or that carries a **forged reserved-namespace** id
(`acl:…`/`role:…` — NE-CKD2) is **quarantined object-wide** — never extracted,
never world-readable.

### Quarantine vs. run-abort semantics

| Condition | Outcome |
| --- | --- |
| An SObject's `ObjectPermissions` outcome is **tainted** (a forged `acl:`/`role:` id) | **QUARANTINE** — object-wide: no Bulk job created, no `Record`, no `AclEntry` |
| An SObject's `ObjectPermissions` outcome carries **zero principals** (no read grants found) | **QUARANTINE** — same as above |
| The `ObjectPermissions` crawl query itself fails (any status, any host fault) | **RUN ABORT** — no perms resolved for ANY object, nothing written, no cursor commits |
| A Bulk job enters `Failed`/`Aborted`, or polling exhausts `bulk_max_polls` | **RUN ABORT** for that object's data (message carries object name + state only — C12) |
| A CSV row is missing its configured `id_field` | **`InvalidRecordError`** (config bug, not data quality — fails loud) |
| `429` exhaustion / any `5xx` on any API call | **RUN ABORT** (retried up to `max_attempts` first) |

Quarantined objects are counted and surfaced as ONE `connector.acl_quarantine`
`denied` audit event per run (`quarantined=N fetch_failed=0 ids=[...]`) — never
emitted world-readable.

### Cursor / durability contract

Per-object `SystemModstamp` cursors (`modstamp:{object}`) are persisted and
committed **only after a successful run**. **Cursors are compared
LEXICOGRAPHICALLY** — this is correct ONLY because Salesforce emits a uniform
ISO-8601 `...Z` format in Bulk CSV output (a format change would surface as a
*stale* cursor, never data loss — the `(source, source_id, record_id)` dedup
ledger absorbs re-reads regardless). A strict `>` incremental boundary means
there is no boundary-row re-read to absorb (unlike a `>=` scheme); the dedup
ledger's job here is purely to make a **ledger-reset resync** duplicate-free:
after resetting/dropping a `modstamp:*` cursor row to force a resync, every
already-written record is re-fetched but its unchanged content-hash causes it
to be skipped, not duplicated. The first run is a full backfill; subsequent
runs auto-select incremental off the persisted ledger. A run with quarantined
objects **still commits** the surviving objects' cursors — one wedged/
misconfigured object cannot stall the others' progress.

### Bulk API 2.0 flow + `api_version` guidance

Each surviving object's crawl is: `POST .../jobs/query` (create) ->
poll `GET .../jobs/query/{id}` every `bulk_poll_interval_seconds` up to
`bulk_max_polls` until `state == "JobComplete"` -> page
`GET .../jobs/query/{id}/results` (CSV, `maxRecords=bulk_max_records_per_page`)
following the `Sforce-Locator` response header until it is absent/`null`.
`api_version` defaults to `"v65.0"` (Winter '26 — GA in every production and
dev org at time of writing) and is config-overridable; bump it as newer
Salesforce API versions become the org's baseline (validated against
`^v\d+\.\d$`).

### Deferred (documented scope cuts, not omissions)

- **Record-level sharing** (OWD, role hierarchy, sharing rules, manual/Apex
  shares) — see the ACL limitation section above; v1.1.
- **Relationship fields** (dotted names, e.g. `Account.Name`) — a documented
  v1 cut (the SOQL-injection guard requires a bare identifier).
- **Deletion propagation** — `IsDeleted` is never queried; tombstones/removals
  are deferred (same deferral class as the M365/SQL connectors).

### Dependency note (build-vs-adopt)

`simple-salesforce` (Apache-2.0) was evaluated during planning and **NOT
adopted**: it is a synchronous, `requests`-based transport, which would bypass
the C2 egress guardian's import allowlist, the per-request SSRF/instance-host
re-validation, the `AIR_GAP` construction guard, and the mandated
`httpx.MockTransport` fixture-first CI (NE-000) — the same call NE-045 made
for `slack_sdk`. dlt has no importable in-package Salesforce source either
(only a vendored `dlt init` scaffold built on `simple-salesforce`), so it
inherits the same disqualifying issues; this connector plugs into the
dlt-based NE-040 framework (bridge/state/contracts) without adding a fourth
dlt import site. See the package `NOTICE` for the full record.

### LIVE-PARKED: real-org runbook

`fixture-first` per NE-000 — CI runs entirely on recorded Salesforce REST/Bulk
JSON+CSV via `httpx.MockTransport` (zero sockets). To exercise a real dev org:

1. Create a **Developer Edition** org (or use an existing sandbox). Create a
   **Connected App** (Setup -> App Manager -> New Connected App), enable
   OAuth, upload a certificate (for `jwt_bearer`) or note the consumer
   secret (for `client_credentials`), and grant the `api` scope.
2. For `jwt_bearer`: pre-authorize a specific integration user (Permitted
   Users -> Admin approved users are pre-authorized) and assign that user via
   a permission set. For `client_credentials`: configure the Client
   Credentials Flow with a designated run-as user.
3. Seed a small set of `objects` (e.g. `Account`, a custom object with a
   restrictive OWD) and set `instance_url` to the org's My Domain base.
4. Run `run_salesforce_sync` for an initial **backfill**; verify the written
   record count and ACL labels match the run-as user's actual profile/
   permission-set object-Read grants.
5. Edit a record in the org (e.g. update an Account's `Description`), wait for
   `SystemModstamp` to advance, and run `run_salesforce_sync` again — confirm
   only that record is re-written (**incremental** verification).
6. Temporarily revoke object Read on one configured object (via profile/
   permission set) and re-run — confirm that object is **quarantined**
   (`connector.acl_quarantine` denied event, zero new Bulk job for it) rather
   than silently still-readable.

Real-org e2e and `api_version` forward-drift calibration (against a
Summer '26 / `v67.0` org) are **env-gated, not required for merge** —
`ObjectPermissions` payload shapes and Bulk API pagination behavior are stable
across API versions, but the fail-closed quarantine design never over-grants
on an unrecognized shape, and the live run will recalibrate the fixtures.

## Write surfaces (NE-050)

The connectors are read-focused, but three **thin write clients** back the governed
write-back actions in `ne-runtime` (NE-050). They wrap the already-constructed,
C2/SSRF-guarded read clients and add **no** egress surface of their own (the httpx
import allowlist is unchanged — they import no HTTP client):

- `SalesforceWriteClient.create_record(sobject, fields)` — REST sObject create; the
  sObject name and every field **name** pass `_check_identifier` before any request;
  field **values** travel only in the JSON body.
- `GraphWriteClient.create_draft(mailbox=…, subject=…, body=…, to=…)` — Graph
  `POST /users/{mailbox}/messages` creates a DRAFT (`isDraft` true, never sent); the
  mailbox is validated fail-closed and the URL is SSRF-re-guarded before any token spend;
  the response must attest `isDraft=true` and a non-empty message id or the action fails closed
  (`GraphClient.post_json`).
- `SqlWriteClient.update_record(table=…, key_column=…, key_value=…, fields=…)` — an
  allowlisted, **bound-parameter-only** single-row UPDATE over the validated NE-046
  engine; an empty write allowlist can never write (fail-closed).

All three validate identifiers / mailboxes / URLs **before** any statement or request,
use bound params / JSON bodies only (no string interpolation), and raise status-only
errors (never a value or credential). They must **only** be reached via ne-runtime's
governed `ActionTool`s — never called directly from application code.

## Jira connector (NE-093)

`ne.connect.jira` is a thin-`httpx` Basic-auth Jira Cloud connector (NO
`atlassian-python-api` — it is requests-based/synchronous and would bypass both
the C2 egress guardian and the mandated `httpx.MockTransport` fixture-first CI,
the same call NE-045 made for `slack_sdk` and NE-047 for `simple-salesforce`;
see the Dependency note below and the package `NOTICE`). It backfills a
config-driven set of projects — **issues** (enhanced-JQL search), **comments**,
and **sprints** (Agile board → sprint) — incrementally syncs off an `updated`
cursor, and resolves each project's permission scheme into fail-closed,
**PROJECT-BROWSE-LEVEL** `AclEntry` rows feeding NE-042/043 trimming.
`run_jira_sync(settings, writer=…, state_store=…, audit_sink=…, actor=…)` drives
the whole pipeline through the framework `SyncRunner`. Importing this subpackage
does **not** import dlt (C1 stays untouched) and adds **zero** new dependencies.

### Connected-app setup

Create an Atlassian API token (id.atlassian.com → Security → API tokens) for a
service account with **Browse Projects** on the configured projects. The token
+ that account's email form the Basic `Authorization` header. The account needs
only read scope.

### Config (`JiraConnectorSettings`)

```python
from ne.connect.jira import JiraConnectorSettings, JiraProjectConfig

settings = JiraConnectorSettings(
    base_url="https://acme.atlassian.net",
    email="svc@acme.example",
    api_token="…",  # SecretStr
    owner="data-eng@acme.example",  # ODCS data-contract owner
    projects=(
        JiraProjectConfig(key="ACME", include_comments=True, include_sprints=True),
    ),
)
```

`projects` is **required non-empty** — explicit per-project scoping, no
site-wide crawl in v1. Each project key is validated `^[A-Z][A-Z0-9_]{1,63}$`
and each `issue_fields` name `^[a-zA-Z][a-zA-Z0-9_]*$` at config-construction
time: this is the **JQL-injection guard**, since the key is assembled directly
into a JQL string (`project = ACME …`). The `updated` cursor is likewise regex-
validated (`^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$`) before interpolation.

### ACL mapping (the security core — fail-closed)

Only the permission scheme's **`BROWSE_PROJECTS`** grants are mirrored, into:

- `jira-group:<groupId>` — a group grant
- `jira-role:<projectId>:<roleId>` — a project-role grant (scoped to the project)
- `jira-user:<accountId>` — an individual-user grant
- `jira-project:<projectId>` — appended to every surviving project's principal
  tuple (AFTER the quarantine decision, so it can never mask an unresolvable scheme)
- `acl:tenant-all` — minted ONLY for an `applicationRole` / `anyone` grant
  (any-logged-in-user / public), byte-equal to `ne.permissions.models`

### THE HONEST ACL LIMITATION — project-browse-level v1, not issue-level

Jira composes the project **browse** grant with per-issue **security levels**
and per-comment **visibility restrictions** into the real visibility decision.
**This connector mirrors only the project browse grant in v1.** An issue
carrying a non-null `fields.security` (an issue-security level) and a comment
with a `visibility` block (or `jsdPublic == false`) are **QUARANTINED per-item**
— never admitted under the coarser project ACL. Role-actor **membership**
resolution (which users a `jira-role:` grant expands to) is the NE-042 principal
store's job, not this mapper's. A project whose scheme cannot be resolved (zero
browse grants) or that carries a **forged reserved-namespace** id
(`acl:`/`role:` — NE-CKD2) is **quarantined project-wide** — never extracted,
never world-readable.

### Quarantine vs. run-abort semantics

| Condition | Outcome |
| --- | --- |
| A project's scheme is **tainted** (forged `acl:`/`role:` holder id) | **QUARANTINE** — project-wide: no issue/comment/sprint fetch, no `Record`, no `AclEntry` |
| A project's scheme carries **zero** browse grants | **QUARANTINE** — same as above |
| An issue with a non-null `fields.security` (issue-security level) | **QUARANTINE per-item** |
| A comment with a `visibility` restriction or `jsdPublic == false` | **QUARANTINE per-item** |
| A permission-scheme / search / comment / board / sprint fetch fails (any status) | **RUN ABORT** — nothing written, no cursor commits |
| An issue/comment payload missing its id | **`InvalidRecordError`** (fails loud) |
| `429` exhaustion / any `5xx` on any call | **RUN ABORT** (retried up to `max_attempts` first) |

Project- and item-level quarantines are counted and surfaced as ONE
`connector.acl_quarantine` `denied` audit event per run.

### Cursor / durability contract

Per-project `updated` cursors (`updated:{key}` ledger keys, formatted
`%Y-%m-%d %H:%M` UTC) are committed **only after a successful run**. JQL
comparison has **minute granularity**, so the incremental window is
`updated >= "<minute>"`; boundary re-reads are absorbed by the
`(source, source_id, record_id)` dedup ledger. **Sprints have no `updated`
cursor** on the Agile API and are re-fetched every run — unchanged sprints are
dedup-skipped, never duplicated. A run with quarantined projects still commits
the surviving projects' cursors.

### Rate-limit posture

Jira Cloud enforces per-account cost-based rate limits and returns `Retry-After`
on 429; the client's `AdaptiveThrottle` honors it (capped) and retries transient
5xx up to `max_attempts`. Irrelevant to fixture CI (zero sockets), documented
for live use.

### Deferred (documented scope cuts, not omissions)

- **Issue security levels & restricted comments** — quarantined per-item, not
  resolved (v1.1).
- **Deletion propagation** — deleted issues/comments are not tombstoned.
- **Attachments / worklogs** — not synced in v1.

### Dependency note (build-vs-adopt)

The dlt-hub `verified-sources` jira template and `atlassian-python-api` were
evaluated and **NOT adopted**: both are `requests`-based / synchronous, which
would bypass the C2 egress guardian's import allowlist, the per-request SSRF /
`/rest/`-path guard, the `AIR_GAP` construction guard, and the mandated
`httpx.MockTransport` fixture-first CI. The dlt templates are consulted as
endpoint/resource-semantics **reference only**. See the package `NOTICE`.

### LIVE-PARKED: real-instance runbook

`fixture-first` per NE-000 — CI runs entirely on recorded Jira REST/Agile JSON
via `httpx.MockTransport` (zero sockets). To exercise a real Jira Cloud site
(owner dev instances per NE-000, out of scope for this unit):

1. Create an API token for a service account with **Browse Projects** on the
   target projects; set `base_url` to the site (`https://<site>.atlassian.net`),
   `email`, `api_token`.
2. Configure `projects` with the real project keys and run `run_jira_sync` for
   an initial **backfill**; verify the written record count and that ACL labels
   match the projects' actual browse grants.
3. Edit an issue (advance its `updated`), wait a minute, and re-run — confirm
   only that issue (and its comments) is re-written (**incremental**).
4. Set an issue's **security level** (or restrict a comment's visibility) and
   re-run — confirm it is **quarantined** (never still-readable).
5. **Endpoint note**: the connector uses the enhanced
   `GET /rest/api/3/search/jql` with `nextPageToken` — the legacy offset-
   paginated `/rest/api/3/search` has been deprecated/removed by Atlassian; if
   Atlassian iterates the search endpoint family again before live seeding,
   recalibrate the fixtures against the then-current shape.

## Zendesk connector (NE-093)

`ne.connect.zendesk` is a thin-`httpx` Basic-auth (`email/token`) Zendesk
connector (NO `zenpy` — it is `requests`-based/synchronous **and** GPL-family,
so it would bypass both the C2 egress guardian and the mandated
`httpx.MockTransport` fixture-first CI, and would need out-of-process isolation
under C4 anyway). It backfills **tickets** (with comments) via the cursor-based
incremental export and **Help Center articles** via the Help Center incremental
export, incrementally resumes off the persisted `after_cursor` / `end_time`, and
resolves each ticket/article/comment into fail-closed `AclEntry` rows.
`run_zendesk_sync(settings, writer=…, state_store=…, audit_sink=…, actor=…)`
drives the pipeline through the framework `SyncRunner`. Importing this
subpackage does **not** import dlt and adds **zero** new dependencies.

### Config (`ZendeskConnectorSettings`)

```python
from ne.connect.zendesk import ZendeskConnectorSettings

settings = ZendeskConnectorSettings(
    base_url="https://acme.zendesk.com",
    email="svc@acme.example",
    api_token="…",  # SecretStr
    owner="support-eng@acme.example",
    sync_tickets=True,
    sync_articles=True,
    include_ticket_comments=True,
)
```

At least one of `sync_tickets` / `sync_articles` must be enabled.

### ACL mapping (the security core — fail-closed)

- a ticket's `requester_id` → `zd-user:<id>`, `assignee_id` → `zd-agent:<id>`,
  `group_id` → `zd-group:<id>`, `organization_id` → `zd-org:<id>`;
- a **PRIVATE** (internal) comment narrows the ticket ACL to its **staff subset**
  (`zd-group:` / `zd-agent:` retained; `zd-user:` / `zd-org:` stripped) under a
  distinct `ticket/<id>/internal` resource — an end user cannot see an internal note;
- a Help Center article's `user_segment_id` / `user_segment_ids` →
  `zd-segment:<id>`; a **null segment** is Zendesk's "everyone" → `acl:tenant-all`
  (minted ONLY here, byte-equal to `ne.permissions.models`).

### THE HONEST ACL LIMITATION — visibility fields, not custom-role policies

This connector mirrors **requester/assignee/group/org** ticket visibility and
Help Center **user segments**. Zendesk's **agent ticket-access policies**,
**custom roles**, and **brand restrictions** are NOT mirrored in v1.
Collaborators/followers are a documented cut. Fail-closed regardless: a
**principal-less** ticket (with its comments), an **empty-staff-subset** private
comment, a **draft** article, and any **forged reserved-namespace** id are
**QUARANTINED** — never world-readable. One `AclEntry` per ticket
(memory-bounded-corpus assumption, sibling parity).

### Quarantine vs. run-abort semantics

| Condition | Outcome |
| --- | --- |
| A ticket with **no** requester/assignee/group/org (or a forged id) | **QUARANTINE** — ticket + its comments (never fetched) |
| A `deleted`-status ticket | **SKIPPED** (deletion propagation deferred) |
| A PRIVATE comment whose **staff subset is empty** | **QUARANTINE per-item** |
| A **draft** article, or a tainted/zero-segment article | **QUARANTINE** |
| Any HTTP fault / `429`/`5xx` exhaustion | **RUN ABORT** — no cursor commits |
| A ticket/article/comment missing its id | **`InvalidRecordError`** (fails loud) |

Quarantines are surfaced as ONE `connector.acl_quarantine` `denied` event per run.

### Cursor / durability contract

The tickets `after_cursor` (`cursor:tickets`) and the articles `end_time`
(`cursor:articles`) are committed **only after a successful run**. Both are
opaque/monotonic and resumed on the next run; the dedup ledger absorbs any
boundary re-read. The response-embedded `after_url` / `next_page` **full URLs
are never followed** — the connector re-issues against its pinned base with the
opaque, validated cursor only (the pinned-base SSRF discipline).

### Rate-limit posture

The incremental-export endpoints are rate-limited (the ticket cursor export is
**~10 requests/minute** endpoint-specific); `Retry-After` on 429 is honored
(capped) and transient 5xx retried up to `max_attempts`. This makes real
backfills slow but is **irrelevant to fixture CI** (zero sockets); it is
documented for live use.

### Deferred (documented scope cuts, not omissions)

- **Agent access policies / custom roles / brand restrictions** — not mirrored (v1.1).
- **Collaborators / followers** — not added to the ticket ACL.
- **Deletion propagation** — `deleted`-status tickets are skipped, not tombstoned.
- **Community posts** — not synced.

### Dependency note (build-vs-adopt)

The dlt-hub `verified-sources` zendesk template and `zenpy` were evaluated and
**NOT adopted**: both are `requests`-based / synchronous (and `zenpy` is
GPL-family), which would bypass the C2 egress guardian's import allowlist, the
`/api/v2/`-path guard, the `AIR_GAP` construction guard, and the mandated
`httpx.MockTransport` fixture-first CI. The dlt templates are consulted as
endpoint/resource-semantics **reference only**. See the package `NOTICE`.

### LIVE-PARKED: real-instance runbook

`fixture-first` per NE-000 — CI runs entirely on recorded Zendesk JSON via
`httpx.MockTransport` (zero sockets). To exercise a real Zendesk instance (owner
dev instances per NE-000, out of scope for this unit):

1. Create an API token (Admin Center → Apps and integrations → APIs → Zendesk
   API) for an agent account; set `base_url` (`https://<subdomain>.zendesk.com`),
   `email`, `api_token`.
2. Run `run_zendesk_sync` for an initial **backfill** (mind the ~10 req/min
   incremental-export limit — a large backfill is slow); verify the written
   record count and that private-comment chunks carry only the staff subset.
3. Update a ticket, re-run — confirm only the changed ticket is re-written
   (**incremental**) and no duplicates appear.
4. Create a **draft** article and a **principal-less** ticket and re-run —
   confirm both are **quarantined** (never readable).

## Microsoft Teams connector (NE-080)

`ne.connect.teams` crawls **channel messages (and replies)** for explicitly
scoped teams via the Microsoft Graph `messages/delta` endpoints, on the same
sibling template as the M365/SharePoint connector (thin app-only Graph client,
certificate- or secret-signed client-credentials auth reusing
`ne/connect/m365`'s `build_client_assertion`). It is registered via the EXISTING
`ne.plugins.connector` entry-point (`teams = ne.connect.teams.connector`).

### Fail-closed ACL model

Each channel's `membershipType` resolves a fail-closed ACL
(`ne.connect.teams.acl.map_channel_acl`) — the connector NEVER fetches a
quarantined channel's messages:

| membershipType | outcome |
| --- | --- |
| `standard` | `teams-team:<team>` (visible to the whole team) |
| `private` | `teams-channel:<team>/<channel>` (the private roster) |
| `shared` | **QUARANTINE** — cross-tenant shared channels are out of v1 scope |
| unknown / missing | **QUARANTINE** (fail-closed) |
| team/channel id with a reserved `acl:`/`role:` prefix | **QUARANTINE** (tainted — forged-label guard) |

`principal_namespace='teams'` with an EMPTY `default_acl` (the fail-closed
backstop). A message missing its id aborts the run LOUD (`InvalidRecordError`).
Membership-EDGE seeding (which principals hold `teams-team:X` /
`teams-channel:X/Y`) is principal-store-side (NE-042), exactly like the Zendesk
connector — the connector only stamps the LABEL a chunk requires. The trimming
cross-check (`test_teams_trimming_crosscheck.py`) proves a standard-channel
member cannot retrieve a private-channel chunk and quarantined channels leak to
nobody.

### v1 limitations

Channel messages only (1:1 / group chats are not crawled); shared channels are
quarantined; the delta fixtures are hand-modeled from the documented API shapes
(the tenant is owner-gated), so the live run recalibrates them + the throttle
defaults (the NE-093 precedent).

### LIVE-PARKED: real-tenant runbook (NE-000)

CI runs entirely on recorded Graph JSON via `httpx.MockTransport` (zero sockets).
To exercise a real tenant (owner-gated per NE-000):

1. Provision an NE-000 dev tenant and submit the Microsoft **protected-API**
   request form for `ChannelMessage.Read.All` / the Teams Export APIs (metering
   was removed 2025-08-25, so there is no billing model to select).
2. Register an Entra app with `Team.ReadBasic.All` + `Channel.ReadBasic.All` +
   `ChannelMessage.Read.All` **application** permissions; configure
   `tenant_id`/`client_id` and a certificate (or secret) plus the scoped
   `team_ids`.
3. Seed the `teams-team:` / `teams-channel:` membership edges via the
   principal-sync extension (the NE-042-side roster crawl over Graph `/members`).
4. Run a **backfill**, then **incremental**; recalibrate the throttle defaults
   against the live 429 cadence.

## CDC sink (`ne.connect.cdc`) — NE-084

A **push** ingestion adapter (vs. the polling connectors above): it maps
**Debezium Server** change events onto the SAME governed, fail-closed
`ne.brain.write.KnowledgeWriter` path. Import it **explicitly** —
`from ne.connect.cdc import CdcSinkService` — it is deliberately NOT re-exported
from `ne.connect` (the sql/jira precedent).

**`dlt`-free AND `httpx`-free by construction.** `ne.connect.cdc` imports only
stdlib, pydantic, `ne.brain.write`, `ne.contracts`, and `ne.connect.bridge` —
never `dlt` (importing `ne.connect.sql.*` would drag the dlt runtime) and never
an HTTP client (it CONSUMES change events; it never fetches). ne-api depends on
`ne.connect.cdc` at runtime, so this confinement is load-bearing and locked by
`tests/test_cdc_import.py` (the C2 egress guardian's allowlist stays untouched).

**Fail-closed ACL core.** `ne.connect.cdc.acl` is a byte-identical semantic copy
of `ne.connect.sql.acl` (a parity test, `test_cdc_acl_parity.py`, asserts equal
output) — a source row may never mint a reserved label (`acl:`/`role:`, incl.
`acl:tenant-all`); an unresolvable / tainted / zero-principal row is quarantined,
never written. DELETE → `KnowledgeWriter.quarantine` (park-then-delete,
idempotent). Item identity is a hex SHA-256 keyed on the configured
`key_columns`, so an UPDATE replaces — and a DELETE removes — the SAME chunk an
INSERT created. See `deploy/compose/cdc/README.md` for the fail-closed-ingestion
analysis and the op-mapping table.

The webhook auth (`decode_webhook_secret` / `verify_standard_webhook`, pure
stdlib) implements the Standard-Webhooks HMAC scheme the Debezium HTTP sink
signs with — a hardcoded known-answer vector (`test_cdc_webhook_auth.py`) proves
the Python verifier and Debezium's Java signer agree on the wire format.

### LIVE-PARKED: real-source-DB CDC e2e (NE-093 precedent)

CI runs entirely on recorded Debezium change-event envelope fixtures
(`tests/fixtures/cdc/`, INSERT/UPDATE/DELETE across Postgres/MySQL/SQL Server +
a schema-wrapped variant + a batch array) — no live source DB, no docker daemon.
A real source-Postgres → live Debezium Server → running ne-api sub-minute
freshness measurement is live-parked; the fixtures + the known-answer HMAC vector
are the standing proof.
