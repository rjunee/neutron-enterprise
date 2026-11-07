# deploy/compose/dev — Neutron Enterprise dev stack

Copyright (c) 2026 Ryan Junee.
SPDX-License-Identifier: Apache-2.0

A `docker compose` profile that brings up the local infra a Neutron
Enterprise developer needs day to day: Postgres, Qdrant, Keycloak, OPA, two
TEI (text-embeddings-inference) instances (embeddings + reranker), OpenBao
in dev mode, and an OTel collector. Langfuse v3 (plus its ops subsystem —
ClickHouse, Valkey, SeaweedFS) is an **optional** compose profile per
SPEC §3.3; the base stack never requires it.

This is a **local development stack only**. It is not the pilot/production
deployment tier — see `deploy/compose/README.md` and NE-070 for the hardened
single-VM pilot installer.

## Usage

Prefer the `ne dev` CLI wrapper (`tools/ne-cli`) over invoking `docker
compose` directly — it fixes the project name, compose file path, and
profile flags for you.

```
ne dev up               # start the base 8 services
ne dev up --langfuse     # also start the Langfuse profile (6 more services)
ne dev ps                # show status of all dev-stack containers
ne dev seed               # idempotently seed Qdrant/OpenBao smoke data
ne dev down                # stop the stack (containers only)
ne dev down --volumes       # stop the stack and delete all named volumes
```

Equivalent raw `docker compose` invocations (for reference):

```
docker compose --project-name ne-dev -f deploy/compose/dev/compose.yaml up --detach --wait
docker compose --project-name ne-dev -f deploy/compose/dev/compose.yaml --profile langfuse up --detach --wait
docker compose --project-name ne-dev -f deploy/compose/dev/compose.yaml --profile langfuse down --volumes
docker compose --project-name ne-dev -f deploy/compose/dev/compose.yaml --profile worm up --detach --wait minio
```

The **`worm` profile** (one service, `minio`, port 9000) brings up a real
Object-Lock-capable S3 server so `packages/ne-storage`'s `s3` ObjectStore
adapter conformance/live tests
(`test_s3_conformance.py`/`test_s3_worm_live.py`) have something real to run
against — see the "Test server" section of
[`../../../packages/ne-storage/README.md`](../../../packages/ne-storage/README.md).
It is **test/dev infrastructure only**: MinIO is AGPL-3.0 and its upstream
community repository is archived (2026-04-25); it is never embedded in or
shipped with the product. Note it is a **separate** S3-compatible server from
`seaweedfs` above (the Langfuse profile's S3 gateway) — SeaweedFS's Object
Lock does not actually enforce COMPLIANCE-mode retention
(seaweedfs/seaweedfs#8350, #7194), so it cannot stand in for this lane.

## Ports

| Service      | Port(s)        |
|--------------|----------------|
| postgres     | 5432           |
| qdrant       | 6333, 6334     |
| keycloak     | 8081           |
| opa          | 8181           |
| tei-embed    | 8085           |
| tei-rerank   | 8086           |
| openbao      | 8200           |
| otel-collector | 4317, 4318   |
| langfuse-web (profile `langfuse`) | 3000 |
| openfga (profile `rebac`) | 8087 |
| minio (profile `worm`) | 9000 |

## Dev credentials (dev only — never for pilot/prod)

These are well-known, throwaway credentials baked into the compose file for
local development convenience. NE-070 owns hardened credential generation
for the pilot tier; none of these values are acceptable outside a
developer's own machine.

| What                | Value                          |
|---------------------|---------------------------------|
| Postgres            | `nedev` / `nedev` (db `neutron`) |
| Keycloak admin      | `admin` / `admin`               |
| Keycloak dev user   | `dev` / `dev` (realm `ne-dev`)   |
| OpenBao root token  | `ne-dev-root`                    |
| SeaweedFS S3 keys   | `nedev` / `nedevsecret`          |
| MinIO (profile `worm`) keys | `ne-test` / `ne-test-secret` |

## Component evidence (verified 2026-07-03)

All images below were checked for current tag existence and license at
planning time (Docker Hub / quay.io / ghcr.io registry APIs). Re-verify
before bumping any pin.

### Always-on services

| Service | Image | License | Notes |
|---|---|---|---|
| postgres | `postgres:18.6-alpine` | PostgreSQL License (OSI) | Latest 18.x — 18.6, released 2026-08-13 (postgresql.org/docs/release/18.6/; 18.5 was never released). **PG18 moved the data volume mount point to `/var/lib/postgresql` (not `.../data`)** — this compose file uses the correct path. Moving an existing 18.4 volume to 18.6 needs no dump/restore (release notes), but run `ANALYZE` on tables that carry GIN indexes (18.6 release note on possibly-corrupt `reltuples`). |
| qdrant | `qdrant/qdrant:v1.18.2` | Apache-2.0 | Latest stable (github.com/qdrant/qdrant/releases). **Drift note:** SPEC §3.3 snapshots "Qdrant 1.17" as of the spec's writing; per the build-time re-verify rule this unit pins the current 1.18.2 instead. Flagged in the PR body for the spec owner. |
| keycloak | `quay.io/keycloak/keycloak:26.7.4` | Apache-2.0 | Latest 26.7.x (2026-09-16, CVE fixes). |
| opa | `openpolicyagent/opa:1.18.2-debug` | Apache-2.0 | Latest (2026-07-02). `-debug` variant (busybox shell) chosen only so a container healthcheck can run in dev. |
| tei-embed / tei-rerank | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.3` | Apache-2.0 | **License re-checked carefully**: Hugging Face's TEI is Apache-2.0, and the maintainers declared the Apache-2.0 relicense "irreversible" (github.com/huggingface/text-embeddings-inference issue #232). The historical restrictive-license episode (HFOIL) belongs to **TGI** (text-generation-inference), a different HF project — not TEI. C4-clean. v1.9.3 is latest. |
| openbao | `openbao/openbao:2.6.3` | MPL-2.0 | Patch GA 2026-09-23 on the 2.6.x line (2.7.0 went GA the same day and is the weekly upgrade lane's decision, not this pin); 2.6.1/2.6.2/2.6.3 are patch releases (no storage-format/API break). 2.6.3 fixes nine repository advisories: CRITICAL GHSA-j6wc-jpvg-xfxq (RCE via `sys/storage/raft/snapshot-force` plugin-catalog replacement), HIGH GHSA-x8fg-h69x-p28f (PKI ACME issues certificates with unvalidated SANs), HIGH GHSA-mjch-vcw3-hhmf (policy cache authorises across namespaces), HIGH GHSA-fg5x-7whg-6c28 (ACL bypass via non-canonical URL), MEDIUM GHSA-2cjw-94fw-wqjx (OIDC UI open redirect), LOW GHSA-8xxq-mq9m-xmhw / GHSA-hr5j-3j78-4vh2 / GHSA-8gmq-wv9h-fcwp / GHSA-cg72-x35g-xfp8. The 2.6 line fixes the 2026-07-14 advisory batch: CRITICAL GHSA-34fc-gh42-pj53 (recovery-mode token timing leak), HIGH GHSA-59w7-v8rr-pr4p (templated-ACL wildcard privilege escalation), MEDIUM GHSA-xp3c-3jw3-4vcr, LOW GHSA-444v-8vxr-p36h. C11 digest `sha256:a60afafda36337abe833c4a63894bf1095098f29abea4091e7e555a33dd52889` (docker.io manifest-list, resolved 2026-09-25). |
| otel-collector | `otel/opentelemetry-collector-contrib:0.155.0` | Apache-2.0 | Latest (2026-06-24). |

### ReBAC profile (optional, NE-087 O-4)

| Service | Image | License | Notes |
|---|---|---|---|
| openfga / openfga-migrate | `openfga/openfga:v1.18.3` | Apache-2.0 | Started with `ne dev up --rebac`; HTTP API on 8087. Dev-only well-known preshared key (`nedev-openfga-token`). Bootstrap the store/model against `http://localhost:8087` (`python -m ne.permissions_rebac.bootstrap`), then export `NE_REBAC_*` manually for a locally-run ne-api. |

### worm profile (optional, NE-S3-OBJECTSTORE)

| Service | Image | License | Notes |
|---|---|---|---|
| minio | `coollabsio/minio@sha256:69b55a1c1c5dc285ce04db96689f5b2102317fc77a50680a1874ca6efd1c87f9` (`RELEASE.2025-10-15T17-29-55Z`, multi-arch index) | AGPL-3.0 | **TEST/DEV INFRASTRUCTURE ONLY — never embedded, never shipped.** The upstream MinIO community project is archived and publishes no images: `minio/minio` is gone from Docker Hub, `quay.io/minio/minio` refuses anonymous pulls, and dl.min.io returns 410 for community binaries. This pin is the coollabsio/minio build (https://github.com/coollabsio/minio): its Dockerfile clones https://github.com/minio/minio, checks out the release tag, compiles the server with `go build`, and adds a minisign-verified `mc`. The image's `org.opencontainers.image.revision` label is inherited from its ubi9-micro base image, so the image itself does not record the MinIO source commit. That tag contains the upstream fix for CVE-2025-62506 / GHSA-jjjj-jwhf-8rgr (commit `c1a49490`). Digest-pinned since no newer upstream release will appear. Chosen over SeaweedFS (the `langfuse` profile's S3 gateway, above) specifically because SeaweedFS's Object Lock does NOT enforce COMPLIANCE-mode retention (seaweedfs/seaweedfs#8350, #7194) — using it here would make the WORM proof vacuous. Run with `MINIO_UPDATE=off` (C1: no phone-home). |

### Langfuse profile (optional, SPEC §3.3)

| Service | Image | License | Notes |
|---|---|---|---|
| langfuse-web / langfuse-worker | `langfuse/langfuse:3.205.0` / `langfuse/langfuse-worker:3.205.0` | MIT (core) | EE features are license-key-gated in the same image; this stack sets **no** license-key env var, so only the MIT core ever activates. Latest v3 as of 2026-07-03 (Langfuse ships near-daily; pin is exact). |
| clickhouse | `clickhouse/clickhouse-server:25.8.26.11-alpine` | Apache-2.0 | Current LTS line (endoflife.date/clickhouse). |
| valkey | `valkey/valkey:8.1.10-alpine` | BSD-3-Clause | Latest of the 8.x line, deliberately **not** 9.x — Langfuse's infra docs (langfuse.com/self-hosting/infrastructure/cache) confirm Valkey support with Redis-7 wire compatibility maintained by 8.x, and require `maxmemory-policy=noeviction` (set in this compose file). Chosen over Redis: Redis 8 is tri-licensed including AGPL/SSPL, which is C4-BANNED. Routine currency bump 8.1.9 → 8.1.10 (upstream 2026-08-31, tag pushed 2026-09-07). Upstream marks 8.1.10 a security release for GHSA-jcj7-v34w-v9vv (use-after-free in RDMA connection handling via `CLIENT KILL`); **this stack was never exposed** — that path requires a build with `USE_RDMA` *and* a configured RDMA listener, and this service is the stock alpine image with neither. Taken for currency, not remediation. C11 digest `sha256:d2e18f3410b6f616de1417f570fa55261af2898b9c5b2cfb6781ce2373ea43d1` (docker.io OCI image index, read from the live manifest 2026-09-08). |
| seaweedfs | `chrislusf/seaweedfs:4.37` | Apache-2.0 | Latest (2026-06-29). Serves as the dev S3-compatible object store for Langfuse event/media uploads. Chosen over MinIO: MinIO is AGPL-licensed with a gutted community edition — C4-BANNED. |

### Models (downloaded by TEI at container start)

| Model | License |
|---|---|
| `Qwen/Qwen3-Embedding-0.6B` (SPEC §3.3 default embedder) | Apache-2.0 |
| `BAAI/bge-reranker-v2-m3` (SPEC-named reranker fallback) | Apache-2.0 |

Qwen3-Reranker's own TEI support is formally verified at NE-022, not here;
this unit uses the SPEC-named fallback reranker.

## Telemetry posture (C1: no phone-home)

Every telemetry knob available in this stack is turned off:

- `QDRANT__TELEMETRY_DISABLED=true` (qdrant)
- `--disable-telemetry` (opa command)
- `TELEMETRY_ENABLED=false` (langfuse-web, langfuse-worker)
- `HF_HUB_DISABLE_TELEMETRY=1` (tei-embed, tei-rerank)
- SeaweedFS: no telemetry-off flag was found in the `4.37` binary's `weed
  server -h` output at time of writing; SeaweedFS's telemetry is opt-in via
  a separate reporting URL that is not configured here, so nothing is sent
  by default. Flagged here for future re-check if that changes upstream.

## Network egress note

TEI downloads its model weights from huggingface.co the first time each of
`tei-embed` / `tei-rerank` starts (cached afterwards in the
`tei-embed-cache` / `tei-rerank-cache` volumes). This is dev-machine-only
egress; controlling or eliminating egress for air-gapped/production
deployments is the scope of NE-013, NE-070, and NE-073, not this unit.

### Optional: pre-stage the tei-embed ONNX cache (fast ort backend, NE-TEI-ONNX)

`Qwen/Qwen3-Embedding-0.6B` ships no ONNX files, so the fresh-download path
above still leaves `tei-embed` on the slow candle CPU backend
(17-64 s/embed). To get the fast `ort` backend on a dev box, pre-stage the
same hub-cache tree the pilot/CI tiers use, then point compose's `tei-embed`
volume at it via a **one-line compose override** (this repo's own
`deploy/compose/dev/compose.yaml` stays byte-untouched):

```sh
python -m ne.serve stage-tei Qwen/Qwen3-Embedding-0.6B \
    --dest ./tei-cache \
    --onnx-artifact tools/model-export/dist/qwen3-embedding-0.6b-onnx-fp32-97b0c61.tar.gz \
    --export-manifest tools/model-export/manifests/qwen3-embedding-0.6b-onnx-fp32.json
python -m ne.serve stage-tei-verify --dest ./tei-cache

cat > tei-onnx-override.yaml <<'EOF'
services:
  tei-embed:
    volumes:
      - ./tei-cache:/data
EOF

docker compose --project-name ne-dev \
  -f deploy/compose/dev/compose.yaml -f tei-onnx-override.yaml up --detach --wait tei-embed
```

Compose merges `volumes:` entries by container target, so the bind REPLACES
the named `tei-embed-cache:/data` mount only for this invocation. Skipping
this recipe is safe — the default first-boot HF download path above still
works, just slower (documented, not a defect).

## Live-verification ledger (parked)

The build host for this unit had the Docker CLI present but no running
Docker daemon, so live bring-up could not be exercised. Recorded as
pending for a follow-up pass with a live daemon:

- `ne dev up` brings all 8 base services to healthy (`up --wait` returns 0).
- `ne dev seed` exits 0 (Qdrant collection `ne_dev_smoke` created, OpenBao
  secret written, Keycloak realm `ne-dev` answering).
- `ne dev up --langfuse` additionally brings the Langfuse UI up at
  `http://localhost:3000`.
- `ne dev down --volumes` tears everything down cleanly.

Static validation (compose YAML parsing/schema, `docker compose ... config
-q`, and the enumerated pytest suite) has been run and is green.
