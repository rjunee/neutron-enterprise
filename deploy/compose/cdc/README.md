# CDC sub-minute freshness lane (NE-084)

An **OFF-BY-DEFAULT** change-data-capture lane. Instead of waiting for a polling
connector's next sync, a customer's source database (Postgres / MySQL / SQL
Server) streams row changes via **Debezium Server** into the knowledge store
with **sub-minute freshness** — through the **SAME governed, fail-closed ACL
ingestion path** the polling connectors use. A CDC event is NOT an ungoverned
back-door: every captured row enters via the existing NE-023 `KnowledgeWriter`.

This is a **standalone overlay project** (`name: ne-cdc`), NOT part of the pilot
stack. The default pilot compose ships **no** Debezium service (proven by the
exact-match pilot guardian + `tools/ne-cli/tests/test_cdc_addon.py`).

## What it is

* `compose.yaml` — one service, `debezium` (digest-pinned `quay.io/debezium/server`),
  profile-gated `["cdc"]`, joining the running pilot network as external, with
  NO published ports (outbound-only: the source DB + the in-network ne-api sink).
* `pilot-override.yaml` — the opt-in that turns the CDC mount ON in ne-api (and
  ne-worker, for config_hash parity) by setting the three `NE_API_CDC_*` vars.
* `.env.example` — the required-secret template (copy to a 0600 env file).

## Bring-up (AFTER the pilot is running)

```sh
# 1. Prepare secrets (never commit real values):
cp deploy/compose/cdc/.env.example .env.cdc && chmod 600 .env.cdc
#    NE_CDC_WEBHOOK_SECRET="whsec_$(openssl rand -base64 32)"
#    (the SAME whsec_ value goes to Debezium AND to ne-api as NE_CDC_TABLES' sibling)

# 2. Turn the sink mount ON through a governed pilot executor. Raw pilot Compose
#    is unsupported: admitted executors must derive the exact selected Agent
#    registry hash and override hostile ambient interpolation. The Meridian demo
#    below provides the shipped integration-specific executor.

# 3. Start Debezium Server (the standalone overlay):
docker compose -f deploy/compose/cdc/compose.yaml --env-file .env.cdc --profile cdc up -d
docker compose -f deploy/compose/cdc/compose.yaml exec debezium curl -fsS http://127.0.0.1:8080/q/health
```

`NE_CDC_WEBHOOK_SECRET` (Debezium's `debezium.sink.http.authentication.webhook.secret`)
and ne-api's `NE_API_CDC_WEBHOOK_SECRET` MUST be the SAME `whsec_` value — the
Standard-Webhooks HMAC gate fails closed otherwise.

## Meridian fee-postings demo preset (BD-12a)

The Thursday demo has one deliberately narrow opt-in: synthetic
`public.fee_postings` from the committed Meridian corpus. It does **not** turn
CDC on in the pilot compose, `ne demo up`, or any customer deployment. Layer
this directory's preset over the pilot project only after the demo is healthy,
with the Debezium digest and the accepted PostgreSQL 18.6 digest (the same
`postgres:18.6-alpine` pin the Helm chart and Zarf bundle carry) already loaded.
The operator supplies `NE_CDC_DEMO_DB_ADMIN_PASSWORD` and
`NE_CDC_SOURCE_DB_PASSWORD` from a `0600` secrets file; the committed
`meridian-fee-postings.env` contains only non-secret table/source configuration.

The source service publishes no port, joins only `ne-pilot_default`, uses a
distinct replication-only login, and grants it `SELECT` on exactly
`public.fee_postings`. Its publication has auto-creation disabled, so a config
mistake cannot silently broaden capture to sibling tables.

This preset does not package Apache AGE. The reviewed official PG18+AGE image
failed the current Critical/High image-security gate; live AGE remains an
explicit residual, never a claim of this lane.

## Source-database prerequisites (per engine)

* **Postgres** — `wal_level=logical`; the `pgoutput` plugin (default here); a
  replication-grant role (`REPLICATION`) and a publication/replication-slot for
  the captured tables. Env is Postgres by default.
* **MySQL** — `binlog_format=ROW`, `binlog_row_image=FULL`; a user with
  `REPLICATION SLAVE, REPLICATION CLIENT`. Swap the connector class + add the
  schema-history store:
  ```
  DEBEZIUM_SOURCE_CONNECTOR_CLASS=io.debezium.connector.mysql.MySqlConnector
  DEBEZIUM_SOURCE_SCHEMA_HISTORY_INTERNAL=io.debezium.storage.file.history.FileSchemaHistory
  DEBEZIUM_SOURCE_SCHEMA_HISTORY_INTERNAL_FILE_FILENAME=/debezium/data/schema-history.dat
  ```
  MySQL change events carry `source.db` (no `schema`), so the table identity is
  `db.table` — configure `NE_API_CDC_TABLES` keys accordingly (e.g. `appdb.customers`).
* **SQL Server** — enable CDC on the DB + tables (`sys.sp_cdc_enable_db` /
  `sys.sp_cdc_enable_table`); a CDC-reader login. Swap the connector class + add
  the same `FileSchemaHistory` pair as MySQL:
  ```
  DEBEZIUM_SOURCE_CONNECTOR_CLASS=io.debezium.connector.sqlserver.SqlServerConnector
  ```
  SQL Server carries `source.schema` (e.g. `dbo`), so the identity is `schema.table`.

## C2 egress analysis

Debezium is **outbound-only**: it connects to the configured source DB and to
`http://ne-api:8000/cdc/v1/events` on the in-network pilot bridge. It publishes
**no ports**, and `OTEL_SDK_DISABLED=true` pins telemetry off (the image also
defaults it off). The Python sink adapter (`ne.connect.cdc`) imports **no HTTP
client** — it never adds egress surface (the ne-connect C2 egress guardian's
allowlist is untouched; a CDC import-hygiene guardian proves it).

## C12 secrets

Source-DB credentials and the webhook secret are `${NE_CDC_*:?}` **required
interpolations** — zero literals (guardian-enforced). ne-api's
`cdc_webhook_secret` is a `SecretStr` (masked from the config_hash and logs). The
sink authenticates **every** POST via the Standard-Webhooks HMAC scheme
(`hmac.compare_digest`, ±300s replay window, uniform 401) — the NE-089 SCIM-gate
precedent. An unauthenticated / replayed / tampered change-event POST is rejected.

## C4 / C11 — dependency-intake provenance

| item | value |
|------|-------|
| component | Debezium Server (`debezium/server`) |
| version | **3.6.0.Final** (released 2026-07-01; 3.5.2.Final is the maintenance line) |
| license | **Apache-2.0** (GitHub `spdx_id` on `debezium/debezium-server`; source ASL-2.0 headers) |
| maintained | actively (repo pushed 2026-07-09) |
| **CISA KEV** | **NO Debezium entries** (searched 2026-07-10; recent adds are Langflow/Joomla/ColdFusion — nothing Debezium). No known exploited-RCE history — no NE-088-style STOP condition. |
| registry | **quay.io** (Docker Hub STOPPED at 3.0.0.Final) |
| digest | `quay.io/debezium/server:3.6.0.Final@sha256:0c5e1eeb2bcaf44facb386a7fdad53d4bb232b812979b4068e989b6ed1008b5c` |
| digest captured | **2026-09-27** via the quay tag API (`https://quay.io/api/v1/repository/debezium/server/tag/?specificTag=3.6.0.Final&onlyActiveTags=false`) |
| runtime base | `registry.access.redhat.com/ubi8/openjdk-21` (ships `curl`; Quarkus health at `:8080/q/health`) |
| Python deps added | **NONE** (out-of-process, digest-pinned, NEVER in `uv.lock`) |

quay's tag history for `3.6.0.Final` shows one digest — `sha256:0c5e1eeb…` — since
2026-07-28T04:09:49Z with `end_ts: null`; it is an OCI image index with `linux/amd64` and
`linux/arm64` children. quay garbage-collects superseded digests — the digest pinned before
this one returns HTTP 404 (verified 2026-09-27) — so a stale pin fails to pull rather than
pulling an old image. Re-verify the digest before a release and update this table:

```sh
curl -s "https://quay.io/api/v1/repository/debezium/server/tag/?specificTag=3.6.0.Final&onlyActiveTags=false"
```

The guardian checks the **shape** (`quay.io/debezium/server:3.6.0.Final@sha256:<64hex>`),
not a specific digest, so a documented digest refresh does not break CI.

## FAIL-CLOSED INGESTION ANALYSIS (C7)

Each Debezium change event maps to exactly one governed action:

| Debezium `op` | meaning | governed action |
|---------------|---------|-----------------|
| `c` / `r` | insert / snapshot-read | `KnowledgeWriter.upsert` with the row's derived ACL |
| `u` | update | `upsert` (same `item_id` — replaces the chunk) |
| `d` | delete | `KnowledgeWriter.quarantine` (park-then-delete) |
| `t` / `m` / other | truncate / message / unknown | quarantine-lane record (`unsupported-op:<op>`) + ACK |

* **ACL derivation** is the SQL-connector-identical `resolve_row_principals`: a
  static operator tuple, or a per-row `principal_column`. A source ROW may never
  mint a reserved label (`acl:`/`role:` — incl. `acl:tenant-all`): a forged one
  **taints** the outcome.
* **Unresolvable / tainted / zero-principal → QUARANTINE**, never written. If the
  row previously existed (an UPDATE whose ACL turned unresolvable) the **stale
  chunk is also removed** so it cannot linger as readable knowledge.
* **DELETE / tombstone** removes the corresponding chunk (park-then-delete). A
  delete of a never-ingested row is an **idempotent no-op** (no wedge, no error
  to Debezium). Note: the HTTP sink never sends value-null tombstone RECORDS
  (`op=d` deletes ARE sent); the parser tolerates a null body as empty anyway.
* **Item identity** is a hex SHA-256 keyed on the row's `key_columns`, so an
  UPDATE replaces — and a DELETE removes — the SAME chunk an INSERT created. No
  raw key VALUE ever enters chunk metadata / audit detail / a quarantine reason
  (C7/C12 no-row-data invariant).
* **Unknown table** (a captured table with no governed map entry) is
  quarantine-lane'd and ACK'd — never a 4xx (a 4xx would wedge Debezium's retry
  loop forever). Configure `NE_API_CDC_TABLES` for every table in
  `DEBEZIUM_SOURCE_TABLE_INCLUDE_LIST`.

### Residuals (documented, fail-closed direction — NOT silent)

* **TRUNCATE / unknown ops** leave previously-ingested chunks readable (v1
  limitation): the op is quarantine-lane'd + counted, and a full re-sync is the
  guidance to reconcile after a source TRUNCATE. This is the fail-closed
  direction (park + document), never a silent drop.
* **Quarantine lane on tmpfs** (`/tmp/ne-cdc-quarantine.sqlite` in the pilot
  override): a container restart loses the lane RECORDS — **inspectability**, not
  safety. Quarantined rows were never written to the knowledge store. Point
  `NE_API_CDC_QUARANTINE_PATH` at a persistent volume to retain lane history.

### Why 200-only-after-durable-apply

The `/cdc/v1/events` route returns `200` **only after** the governed `apply`
completes (durable upsert / quarantine). Debezium retries any non-2xx, so no
event is ACK'd before it is governed-written or quarantined — no data loss.

## Live-parked end-to-end proof (NE-093 precedent)

A real source-Postgres → live Debezium Server container → running ne-api
end-to-end freshness measurement (sub-minute latency) is **live-parked**: the
build host has no docker daemon, and a live source DB is out of scope for CI.
The standing CI proof is the recorded Debezium change-event envelope fixtures
(`packages/ne-connect/tests/fixtures/cdc/`), the hardcoded known-answer HMAC
signature vector, and these static overlay guardians.
