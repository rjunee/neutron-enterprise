# deploy/compose/pilot — hardened single-VM pilot tier (NE-070)

The engagement-ready pilot stack the NE-077 gate installs FROM. Built ON the
dev/serve compose tiers (NOT a rewrite): it `include`s `../serve/compose.yaml`
(the vLLM `gpu`/`cpu` serving profiles ride along unmodified) and composes its
own hardened `core` / `brain` / `obs` profiles.

**Never run `docker compose` here directly.** Use the installer, which runs
preflights, injects secrets from a per-customer config bundle into an ephemeral
`0600` env file, and waits on healthchecks:

```
ne pilot promote --customer acme --hostname pilot.acme.example --out ./acme-bundle
ne up --profile pilot --bundle ./acme-bundle          # core + brain + obs
ne up --profile core  --bundle ./acme-bundle          # a single profile
ne up --profile pilot --serve-cpu --bundle ./acme-bundle   # + CPU tiny-model vLLM
ne down --bundle ./acme-bundle [--volumes]
```

## Profiles

| Profile | Services | Notes |
|---------|----------|-------|
| `core`  | caddy, postgres, age-admission, keycloak, opa, ne-api, ne-worker | ingress + identity + PDP + persistent AGE DB/admission + the first-party app backend + durable worker |
| `brain` | qdrant, tei-embed, tei-rerank | vector store + embeddings/rerank |
| `obs`   | otel-collector | OTLP collector (loopback) |
| `agents`| *(reserved — no services yet)* | NE-103c placed the durable `ne-worker` in **core** (not `agents`): the SPEC §5 row wins over the design's `agents` note — ne-api ships enqueue mode so its executor must co-start, and air-gap Phase A runs core. `agents` stays reserved for future first-party roles; `ne up --profile agents` warns and starts nothing |
| `gpu` / `cpu` | vllm / vllm-cpu (from `../serve`) | surfaced by `ne up --serve-gpu` / `--serve-cpu` (see "serve mapping") |

`ne up --profile pilot` expands to `core` + `brain` + `obs`. The `serve-gpu`
name is the CLI surface for the included serve tier's compose-level `gpu`
profile; `--serve-cpu` selects `cpu`. Without `--serve-gpu`/`--serve-cpu` no
serve container starts (frontier/vLLM lanes off by default — C2).

## TLS / port topology

Caddy is the **only** service that publishes off-box, and it terminates TLS on
every exposed port:

| Port | Bind | Purpose |
|------|------|---------|
| `0.0.0.0:443`  | Caddy | console + /api, same-origin via ne-api (Caddy TLS → `reverse_proxy ne-api:8000`) |
| `0.0.0.0:8443` | Caddy | Keycloak, TLS-terminated → `reverse_proxy keycloak:8080` |
| `127.0.0.1:5432` | postgres | on-VM only |
| `127.0.0.1:8181` | opa | on-VM only |
| `127.0.0.1:6333/6334` | qdrant | on-VM only |
| `127.0.0.1:8085/8086` | tei-embed / tei-rerank | on-VM only |
| `127.0.0.1:4317/4318` | otel-collector | on-VM only |

Keycloak, tei, qdrant etc. bind loopback because on-VM processes (gateway,
worker) consume them locally — loopback is on-box, never off-box. No plaintext
service port is reachable off the VM. `test_pilot_stack.py` enforces the
caddy-only rule mechanically.

### JWKS posture (in-network key fetch — tracked residual)

`ne-api` verifies Keycloak tokens against the realm JWKS. The token `iss` stays
the **external** `https://<hostname>:8443/realms/ne-pilot` issuer (exact-match,
via `KC_HOSTNAME`), but the KEY FETCH is wired to the **in-network**
`http://keycloak:8080/.../certs` with `NE_IDENTITY_OIDC__ALLOW_INSECURE_HTTP=true`.
This is the same plaintext-in-network posture as the existing
Caddy→`keycloak:8080` leg on the isolated compose network, and it is the only
workable path today: `HttpJwksSource` (stdlib urllib) has no custom-CA option to
trust Caddy's `tls internal` local CA, so it cannot fetch the JWKS over the 8443
self-signed vhost. **Tracked follow-up NE-IDENTITY-JWKS-CA**: add a CA-bundle
setting to the identity verifier, then flip `JWKS_URI` to the https 8443 vhost and
drop `allow_insecure_http`.

## Cert provisioning

- **Self-signed default** (`./caddy/Caddyfile`, `tls internal`): Caddy's LOCAL CA
  issues certs — zero ACME, zero egress (C2). Clients trust the CA at
  `/data/caddy/pki/authorities/local/root.crt` inside the caddy volume, or use
  `curl -k`. Good for dev-pilot and CI.
- **Real certs** (`./caddy/Caddyfile.customcerts`, `tls /certs/tls.crt
  /certs/tls.key`): drop operator-provisioned cert + key into `./certs/`
  (git-ignored — NEVER committed) and set `NE_PILOT_CADDYFILE=./caddy/Caddyfile.customcerts`
  in the bundle env before `ne up`.

## Pilot Keycloak realm (demo)

The `keycloak` service imports a **checked-in demo realm** on start
(`--import-realm`), `keycloak/ne-pilot-realm.json` — the full detail is in
[`keycloak/README.md`](keycloak/README.md). It seeds three demo users so the pilot
is loginable end-to-end today:

| Username   | Password        | Group / platform role         |
|------------|-----------------|-------------------------------|
| `approver` | `approver-demo` | `approver` — HITL approval    |
| `steward`  | `steward-demo`  | `steward` — flywheel review   |
| `ops`      | `ops-demo`      | `ops` — audit read            |

These are the **only** sanctioned literal credentials in the pilot tree (C12,
owner dec v): documented demo creds replaced entirely by real IdP brokering (Entra
ID / Okta federated into Keycloak). The public PKCE-S256 `ne-console` client's
`redirectUris` carry a `${NE_PILOT_HOSTNAME}` placeholder that Keycloak resolves
at import from the same env var the bundle injects. **Re-import caveat:**
`--import-realm` SKIPS a realm that already exists — editing the JSON does not
generally re-apply to an already-initialized Postgres. `ne demo up` has one
non-destructive exception: when both `displayName` and `displayNameHtml` exist in
the exact realm artifact, its sanctioned Keycloak Admin REST provisioning step
reconciles and verifies those presentation fields idempotently. All other realm
changes still require an explicit Admin REST migration or a fresh deployment
(CI is immune — fresh volumes each run).

## Secrets (C12)

No secret literal exists in any file here — every credential is a
`${NE_PILOT_*:?...}` **required** interpolation that fails closed if unset:

| Env var | SecretStore path/field |
|---------|------------------------|
| `NE_PILOT_POSTGRES_PASSWORD` | `pilot/postgres` · `password` |
| `NE_PILOT_KEYCLOAK_ADMIN_PASSWORD` | `pilot/keycloak` · `admin_password` |

`ne-api` reads the platform Postgres through five `*_DATABASE_URL` env vars
(`NE_RUNTIME_SESSION_/NE_AUDIT_/NE_PERMISSIONS_/NE_FLYWHEEL_SIGNAL_/NE_API_EVAL_`),
each a `postgresql+psycopg://ne:${NE_PILOT_POSTGRES_PASSWORD}@postgres:5432/neutron`
DSN — the SAME injected secret, no new secret. The password **must be URL-safe**
for the DSN (the bundle generator's `token_urlsafe` already is). These use a
PLAIN `${VAR}` interpolation (not the required-with-message `${VAR:?msg}` form)
on purpose: a mid-DSN required-with-message interpolation of the password var
reads to the C12 secret-hygiene scanner as a literal credential assignment and
false-trips it. Fail-closed is preserved anyway — the `postgres` service
declares that same var with the required-with-message form, and both services
are core-profile and always co-started, so an unset var hard-fails the whole
`ne up` at compose interpolation time.

NE-103c adds a dedicated `ne_dbos` database (created by `initdb/01-databases.sql`)
for DBOS's own workflow/queue schema (SPEC §4.1). Both `ne-api` (its `DBOSClient`,
via `NE_API_DBOS_SYSTEM_DATABASE_URL`) and `ne-worker` (its DBOS launch, via
`NE_WORKER_DBOS_DATABASE_URL`) target it. Both processes share ONE `x-ne-app-env`
compose anchor and run `NE_RUNTIME_DURABILITY=dbos` + `NE_API_TURN_EXECUTION=enqueue`
so their frozen `config_hash` matches (ADR-0006) — any env divergence would fork the
hash and split audit correlation.

PRE-008 mounts `deploy/agents/registry.toml` read-only at
`/etc/neutron/agents/registry.toml` in both `ne-api` and `ne-worker`, and the
shared environment points both roots to that exact path. The file is declarative
catalog configuration only; its two Meridian definitions intentionally remain
unavailable until their pinned Knowledge/Skill/workflow dependencies ship. Every
supported Compose executor computes `NE_AGENT_REGISTRY_RAW_SHA256` from the exact
registry bytes selected by its pilot base and overwrites any ambient value. That
required interpolation stamps the same rollout label onto exactly `ne-api` and
`ne-worker`; unsupported raw Compose use fails closed when the binding is absent.

PRE-011F likewise mounts the exact repository `deploy/skills` tree read-only at
`/etc/neutron/skills` in both processes. Every governed Compose executor hashes
the selected raw `SKILL.md` bytes, overwrites hostile ambient
`NE_SKILL_BUNDLE_RAW_SHA256`, and stamps that same value on both roots. Only
`ne-worker` reconciles the bundle into the shared Postgres Skill registry,
before platform/catalog construction, worker globals, or DBOS. `ne-api` starts
only after worker health and its API-owned read-only gate re-parses the mounted
pack and rechecks the exact raw hash, Agent pin, and hash-verified shared row.
The Skill procedure never enters an environment variable, label, log, or
readiness response.

`ne up` resolves these via the bundle's SecretStore (pilot default = `sops-file`,
per SPEC §3.3 — **not** OpenBao, which is the prod profile; `-dev` in-memory
OpenBao is unacceptable in a hardened tier) into `<bundle>/runtime/pilot.env`
(mode `0600`, values never logged), then passes it as `--env-file`.
`test_pilot_secret_hygiene.py` is the guardian: it fails if any committed pilot
file carries a literal secret value.

### Protected Agent transport generation (PRE-009C)

`ne-api` and `ne-worker` share one `ne-app:0.1.0` image anchor, the exact
`NE_AGENT_TRANSPORT_GENERATION=agent-transport-v1` discriminator, and the same
null-valued `NE_AGENT_PRIVATE_PAYLOAD_KEY_ID` /
`NE_AGENT_PRIVATE_PAYLOAD_KEK_B64` SecretStore pass-through references. The
governed deployer resolves the shared local tag to one exact `sha256` image ID;
a role-local image, generation, or private-payload reference is invalid.

On a populated Pilot that does not already run this generation, the only
permitted scale/restart adapter is:

```bash
ne agent-transport cutover --bundle BUNDLE --target pilot \
  --expected-image-digest sha256:EXACT_IMAGE_ID --backup-dest BACKUP_DEST
```

It holds the upgrade lock, scales both roots to zero, creates and verifies the encrypted platform +
`ne_dbos` backup, migrates/scans/fences, and restarts both roots on the identical
image/generation. Raw Compose scaling and worker-first rolling
replacement are unsupported for this boundary. See
[`docs/ops/agent-transport-cutover.md`](../../../docs/ops/agent-transport-cutover.md)
for the journal, rollback boundary, and roll-forward recovery procedure.

### Canonical default Ask (PRE-010G)

The shared `x-ne-app-env` anchor declares `NE_API_DEFAULT_ASK_GENERATION` as a
null-valued pass-through. Base `ne up` leaves it absent in every profile,
including the `pilot` pseudo-profile and `core + obs` air-gap Phase A. The
generated `ne demo` overlay writes `default-ask/canonical-run/v1` into both roots
only when the existing live-writeback staging is present—the boundary that also
supplies the admitted Meridian source and model/task producer path. It is the
second half of the readiness gate the transport generation above opens: the
composition root keeps the default Ask closed unless the Agent Run store composed,
the transport generation matches, AND this pin is exactly the canonical generation.
`ApiSettings` types the field as a closed `Literal`, so an arbitrary generation
fails at settings load rather than selecting a different activation.

`ne-api` admits the Ask and `ne-worker` executes the canonical Run, so the pin must
reach both roots; a deployment carrying it on one root only accepts work it cannot
run. The base CLI also scrubs an ambient `NE_API_DEFAULT_ASK_GENERATION`, so the
caller's shell cannot bypass the live-overlay boundary. The Helm chart pins the
transport generation but deliberately not this one, so a Kubernetes deployment
loads the dormant `""` and the default Ask stays closed.

The admitted live-demo pin MOVES the frozen `config_hash` — it is a real `ApiSettings` value and
`ne-api` is unconditionally in the hash preimage. Both roots move together, so
API/worker parity is unchanged, and both Appendix-E capture phases carry the same
new hash so their comparison is unaffected. A `config_hash` recorded from a Pilot
before this pin will not match one recorded after. Base bring-ups omit it from
both processes, leaving API, worker, and the database fence dormant.

## Air-gap (C2)

The tier is air-gappable: pinned digests (below), `tls internal` local CA, all
telemetry disabled, no phone-home. Before an offline bring-up, pre-stage:

- **TEI weights** into the `tei-embed-cache` / `tei-rerank-cache` volumes (see
  "Operator pre-stage: tei-embed ONNX cache" below for the fast-ort path), then
  set `NE_PILOT_HF_OFFLINE=1` in the bundle env.
- **vLLM weights** into the serve tier's `./models` (see `../serve/README.md`).

**Honesty correction (NE-TEI-ONNX, 2026-07-17):** `HF_HUB_OFFLINE=1` is a
**no-op** for TEI `cpu-1.9.3` — the pinned `hf-hub` 0.4.2 Rust crate it ships
never reads that env var at all (verified against the crate source). The env
key is kept (harmless, and meaningful to any Python-side `huggingface_hub`
tooling an operator runs alongside it), but it grants **no** offline guarantee
by itself. TEI's actual offline behavior is *implicit*: `hf-hub`'s `ApiRepo::get`
is cache-first — if `refs/<revision>` → `snapshots/<hash>/<file>` already
exists locally, it is served with **zero** network calls, online or offline.
With a fully pre-staged cache TEI still *attempts* a few warn-only calls at
boot (`api.info()`, legacy `sentence_*_config.json` probes); on the sealed,
**connection-refused** network this harness proves (phases B1/B2), those
attempts fail instantly and TEI proceeds from cache. The actual offline
**guarantee** is the combination this repo actually verifies: a **sealed
network** (raw-IP egress dead, phases A/B1/B2/C) **+** a **pre-staged,
sha256-verified cache** (`stage-tei`/`stage-tei-verify`) — not the env var.

**Caveat for field deployments:** a *connection-refused* sealed net (this
harness, most enterprise egress-deny firewalls) makes those warn-only attempts
fail instantly. A *blackholed* network (packets silently dropped, no RST) can
instead make each attempt hang for a TCP connect timeout — a slow-boot risk,
not a correctness one. TEI's own documented zero-attempt mode is a local-
directory `--model-id` command override, not taken here because it would edit
the byte-guarded pilot compose `command:`.

### Operator pre-stage: tei-embed ONNX cache (NE-TEI-ONNX)

The official `Qwen/Qwen3-Embedding-0.6B` checkpoint ships **no ONNX files**, so
TEI's fast `ort` backend 404s and it silently falls back to the candle CPU
backend (17-64 s/embed). `tools/model-export` self-exports + parity-gates a
fp32 ONNX build (see its README). NE-TEI-GEN (owner decision 2026-07-19): the
artifact is **not hosted anywhere** — an egress-OK staging host GENERATES it on
demand + caches it locally (`$NE_TEI_ONNX_CACHE`), and the sealed bundle carries
it for air-gap. `ne-serve stage-tei` pre-stages the **exact** hub-cache tree TEI
reads, injecting the sha256-verified ONNX pair:

```sh
# Egress-OK staging host — default `stage-tei` GENERATES + caches the fast-ONNX
# pair (first run downloads ~2.3 GB + exports, tens of minutes; cached under
# $NE_TEI_ONNX_CACHE thereafter), then stages it:
python -m ne.serve stage-tei Qwen/Qwen3-Embedding-0.6B --dest ./tei-cache

# Air-gap / explicit override — inject a prebuilt pair from the sealed bundle
# (the artifact travels WITH its export-manifest.json; --no-generate is implied
# because an explicit --onnx-artifact always wins):
python -m ne.serve stage-tei Qwen/Qwen3-Embedding-0.6B \
    --dest ./tei-cache \
    --onnx-artifact qwen3-embedding-0.6b-onnx-fp32-97b0c61.tar.gz \
    --export-manifest tools/model-export/manifests/qwen3-embedding-0.6b-onnx-fp32.json

# Copy the staged tree into the running (or not-yet-running) tei-embed-cache
# volume using the ALREADY-PINNED TEI image as the copy container — no new
# image pin, no bare `docker cp` into a path the container doesn't expect:
docker run --rm \
    -v tei-embed-cache:/data \
    -v "$(pwd)/tei-cache":/staged:ro \
    --entrypoint cp ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.3@sha256:ad950d30878eceb72aaf32024d26fa2b1d04a75304fa0b4776b49aa1941fea07 \
    -a /staged/. /data/

# At the air-gap boundary, re-verify the staged bytes before trusting them:
python -m ne.serve stage-tei-verify --dest ./tei-cache
```

Passing `--no-generate` is **safe, not fatal**: `stage-tei --no-generate` still
pins + sha256-hashes the official snapshot, and TEI simply serves from the
slower candle CPU backend. A sealed (`AIR_GAP=1`) host with an empty cache
fail-closes rather than attempting egress — supply the bundle artifact via
`--onnx-artifact`. `ne demo up` (default generates; `--tei-onnx-artifact`
overrides) and the no-egress CI workflow follow the same `stage-tei` →
volume-copy → `stage-tei-verify` sequence (see `.github/workflows/no-egress.yml`
and `tools/ne-cli/src/ne/cli/demo/live.py`).

**Follow-up, NOT this unit:** `tei-rerank` (`BAAI/bge-reranker-v2-m3`) ALSO
ships no ONNX files, so it stays on candle regardless — a separate,
tracked spike (different model/pooling type — classifier/CLS path, not the
last-token embedding path this unit solved), covered meanwhile by
NE-DEMO-FIX1's 180 s rerank timeout escape hatch.

The full offline bundle proof is owned by NE-073/NE-086; this tier provides the
knobs.

## OPA governance policy (PDP) — NE-PILOT-OPA-BUNDLE

`opa/policies/ne_system.rego` (`package ne.dev`) is the ONLY policy file
COMMITTED into this compose tier — it exists so a bare `docker compose up`
(no CLI, no bundle) has something for OPA to serve, and it makes no real
authorization decisions (`default allow := false`). It stays byte-identical
to `deploy/helm/neutron-enterprise/charts/opa/files/ne_system.rego`
(guardian: `tools/ne-cli/tests/test_helm_chart.py::test_bundled_files_byte_match_pilot`).

**A by-the-runbook `ne up --profile core|pilot --bundle <dir>` never ships
that placeholder alone.** It stages the REAL first-party governance bundle
(`packages/ne-govern/policies` — `ne/govern/{decision,subagent,tiers,version}.rego`
+ `.manifest`, excluding `*_test.rego`) into `<bundle>/runtime/opa-policies`
and layers a GENERATED compose overlay
(`<bundle>/runtime/opa-govern.overlay.yaml`) that re-mounts `/policies` onto
the staged directory — the pinned `compose.yaml` above is never edited
(compose merges same-target volume mounts, later `-f` wins). This is the
same mechanism `ne demo up` has used since NE-DEMO-LIVE-SEAMS
(`tools/ne-cli/src/ne/cli/opa_bundle.py`, shared by both call sites), now
wired into the shipped installer path too. The result: a live
`POST /v1/data/ne/govern/decision` is DEFINED (OPA's real decision
document) instead of failing closed with `PolicyBackendError` against the
`ne.dev` placeholder — see `packages/ne-govern/src/ne/govern/opa.py`. Any
operator `--overlay` still layers AFTER the generated one, so it can
re-mount `/policies` again if needed. A non-`core` bring-up (e.g.
`--profile brain` alone) stages nothing.

## Image provenance (C11)

Every image is `tag@sha256` digest-pinned. Verified 2026-07-07 (index/manifest-list
digests, `docker inspect RepoDigests` / registry `Docker-Content-Digest`).

| Service | Image tag | Digest | License | Source |
|---------|-----------|--------|---------|--------|
| caddy | `caddy:2.11.4` | `sha256:af5fdcd76f2db5e4e974ee92f96ee8c0fc3edb55bd4ba5032547cbf3f65e486d` | Apache-2.0 | hub.docker.com/_/caddy |
| postgres / age-admission | exact local image ID `sha256:6ba66e01b43cbfde0f0a03699f609fed1e218612aab7b2119052e0fd5aeb4f26` | source revision `7b7cede30d7af58ac3259b51cf59c76f1e95754c`; PG 18.6 + AGE 1.7.0 | PostgreSQL + Apache-2.0 | first-party build; `deploy/docker/ne-postgres-age/README.md` |
| keycloak | `quay.io/keycloak/keycloak:26.7.4` | `sha256:82a77884f3af238beab1e7afd63b5f530e1b5c0590bd7aa60b40a40463e29b2c` | Apache-2.0 | quay.io/keycloak/keycloak |
| opa | `openpolicyagent/opa:1.18.2-debug` | `sha256:7ee75476b179699061e61e277e1aafa7d13e126034fcdcba4beca8d233c3e865` | Apache-2.0 | hub.docker.com/r/openpolicyagent/opa |
| qdrant | `qdrant/qdrant:v1.18.2` | `sha256:75eab8c4ba42096724fdcfde8b4de0b5713d529dde32f285a1f86fdcb2c9e50c` | Apache-2.0 | hub.docker.com/r/qdrant/qdrant |
| tei-embed / tei-rerank | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.3` | `sha256:ad950d30878eceb72aaf32024d26fa2b1d04a75304fa0b4776b49aa1941fea07` | Apache-2.0 | ghcr.io/huggingface/text-embeddings-inference |
| otel-collector | `otel/opentelemetry-collector-contrib:0.155.0` | `sha256:4935caa35e9a4cb387e35732e8fb22b2b5759af8d12e7043357f03837f6e8df5` | Apache-2.0 | hub.docker.com/r/otel/opentelemetry-collector-contrib |
| openfga / openfga-migrate (`rebac`) | `openfga/openfga:v1.18.3` | `sha256:01a6000aa6040a4d0bde6ea1d3359ac3b9f21dc972ac6103a87b38659a836776` | Apache-2.0 | hub.docker.com/r/openfga/openfga (multi-arch index digest read from the live registry 2026-09-09; head of the v1.18 line; license re-verified Apache-2.0 same day). Patch releases are cumulative, so the CVE-2026-33729/55170/55689 fixes noted at v1.18.1 remain included. v1.18.2 adds a Go 1.26.5 toolchain rebuild (Go stdlib CVE-2026-39822) — base-image hygiene, NOT remediation of an exposure here: the `rebac` profile ships OFF by default, so this service has never run in the deployed stack. No CISA-KEV. |
| ne-api | `ne-app:0.1.0` | *(none — built in-seal, local image, `pull_policy: never`; base digests in `deploy/docker/ne-app/README.md`)* | Apache-2.0 (first-party) | built locally by `deploy/airgap/scripts/build-ne-app.sh` |

vLLM (`gpu`/`cpu`) is pinned in `../serve/compose.yaml` (ne-serve-owned). The
The first-party `ne-app` and `ne-postgres-age` images are the two closed local
artifact families. `ne-app` retains its tag + `pull_policy: never` Phase-A ID
check. PostgreSQL/AGE is stricter in the executable Compose: the service names
the exact local image ID and `pull_policy: never`, while the one-shot
`age-admission` gate binds that digest and the verified source revision to the
live `pg_control_system().system_identifier`. Its canonical receipt contains
exactly those three fields on a separate retained volume. API and worker cannot
start until it succeeds. The gate creates only extension `age` and empty graph
namespace `ne_graph`; graph ingestion/query activation remains separately
governed and is not implied by substrate readiness.

## ReBAC (rebac profile) — NE-087 (O-4)

Optional OpenFGA fine-grained document sharing, **OFF by default**. The `rebac`
profile adds the `openfga` server + a one-shot `openfga-migrate` job (hardened: uid
`65532`, read-only rootfs, `cap_drop: ALL`, `--playground-enabled=false`,
`--metrics-enabled=false`, loopback-only `127.0.0.1:8080`). ne-api/ne-worker only
activate the resolver once the store/model ids are sealed into the bundle. Direct
`docker compose` use is UNSUPPORTED — the `NE_REBAC_*` app env resolves ONLY through
`ne up`'s ephemeral 0600 env-file (five NULL-valued pass-through keys on the shared
anchor; unset ⇒ omitted from the container ⇒ default-off byte-identical).

Bring-up / bootstrap flow:

1. Populate the OpenFGA preshared key in the bundle SecretStore at `pilot/openfga`
   (field `api_token`). It is **always-required** — compose interpolates the
   required `${NE_PILOT_OPENFGA_API_TOKEN}` on the openfga service at parse time,
   regardless of the selected profiles.
2. Start the server + migrate only (no app wiring yet):
   `ne up --profile rebac --bundle <dir>`
3. Create the store + pinned model (token read from env ONLY — never argv, C12):
   `NE_REBAC_API_TOKEN=${OPENFGA_TOKEN} uv run python -m ne.permissions_rebac.bootstrap --api-url http://127.0.0.1:8080`
4. Paste the printed `store_id` / `authorization_model_id` into the bundle's
   `config/ne-rebac.toml`, then re-seal:
   `ne pilot promote --rebac-config <file> ...`
5. Boot with the resolver ACTIVE:
   `ne up --profile pilot --profile rebac --bundle <dir>`

## Tier-2 graph (`--graph`)

The persistent Apache AGE substrate is part of `core` — `postgres` carries the
extension and `age-admission` gates both app services on the exact admitted image
digest and source revision. The Tier-2 graph tier that READS it is a separate,
deployment-level decision:

`ne up --profile pilot --graph --bundle <dir>`

`--graph` writes `NE_BRAIN_GRAPH_ENABLED=true` into the ephemeral 0600
`--env-file`; that name is declared NULL-VALUED in the shared `x-ne-app-env`
anchor, so a bring-up that does not pass `--graph` leaves both `ne-api` and
`ne-worker` without the key and `BrainGraphSettings.enabled` stays `False`. It
requires the `core` profile, because that is the profile that starts and admits
the database the tier addresses.

Rolling back is `ne up` without `--graph`. `ne up`/`ne down` scrub ambient
`NE_BRAIN_GRAPH_*` from the compose subprocess environment, so an
`NE_BRAIN_GRAPH_ENABLED` still exported in the shell that ran the activated
bring-up cannot hold the tier on across that rollback — compose would otherwise
give the shell value precedence over the `--env-file`.

`--graph` composes graph READ over whatever the admitted AGE volume already
holds. It configures no connector source and populates nothing:
`NE_BRAIN_GRAPH_SOURCES` is deliberately undeclared in this tier.

## Hardening (guardian-enforced)

Every pilot-defined service carries `security_opt: [no-new-privileges:true]`,
`restart: unless-stopped`, `deploy.resources.limits` (memory + cpus), and a
healthcheck (otel-collector is the documented distroless exemption — no shell to
probe with). The stateless proxy/sidecars (caddy, opa, otel-collector) also
`cap_drop: [ALL]`; caddy adds back only `NET_BIND_SERVICE` (it binds 443/8443).

## Status / boundaries

- The first-party `ne-app` image exists (NE-103a): the `ne-api` backend is wired
  into the `core` profile — hardened, no published ports, healthchecked, built
  in-seal + local-digest-pinned.
- **NE-103b landed — the pilot is loginable.** ne-api serves the console SPA
  same-origin at `/` (owner dec iii); Caddy 443 `reverse_proxy ne-api:8000`
  (console + `/api` behind one TLS front door); the checked-in pilot Keycloak
  realm (`ne-pilot`, public PKCE `ne-console` client, seeded approver/steward/ops
  demo users) imports on start; and OIDC-live login works — the sealed-netns
  Phase A `test_password_grant_login_end_to_end` is the automated proof (a real
  Keycloak-minted token authorized by ne-api via the in-network JWKS). **Still
  parked:** the browser auth-code+PKCE redirect UX and the brain retrieval e2e →
  the NE-077 console demo; and **NE-103c:** the `ne-worker` cron service (a
  `command:` override on the same image).
- **`NE_GATEWAY_MODELS` — keep+flag decision (NE-103b).** The gateway model menu
  stays a single compose-authored `ModelDef` entry rather than being sourced from
  the config bundle: menus are `ModelEntry` REGISTRY entries (not the `ModelDef`
  ROUTING defs this var parses), the pilot vLLM serves exactly one model, and the
  running gateway config is already `config_hash`-covered (create_app freezes
  gateway settings). This literal is intentionally byte-untouched by
  **NE-BUNDLE-GATEWAY-CONFIG** (which shipped the demo-side frontier swap —
  `ne demo up --frontier anthropic|openai`, see `docs/demo/README.md` "Frontier
  model swap" — entirely through the demo's runtime compose OVERLAY, never this
  pinned pilot compose file). Bundle↔runtime menu unification for the **pilot**
  deployment itself (making `deploy/compose/pilot/compose.yaml`'s
  `NE_GATEWAY_MODELS` sourced from the config bundle, the same `ModelEntry`
  REGISTRY vs `ModelDef` ROUTING unification this note originally flagged) is the
  remaining, explicitly-scoped-out residual — tracked as
  **NE-PILOT-GATEWAY-MENU**.
- **`NE_GATEWAY_MODELS.context_window` — REQUIRED (NE-CTXB-2a).** Every entry now
  MUST carry `context_window`: the **serving** window, i.e. the deployment's vLLM
  `--max-model-len`, NOT the model card's advertised max (the RunPod
  `--max-model-len` incident proved the card value is wrong at runtime). The
  compose default is a conservative `32768`; a deployment that serves a larger
  window should raise it to the actual served value, and one that serves less MUST
  lower it. `ModelDef` is now `extra="forbid"`, so a typo'd key (e.g.
  `context_windw`) fails boot LOUDLY instead of being silently dropped. Optional
  `max_output_tokens` defaults to `min(4096, context_window // 8)` (must stay
  `< context_window // 2`). An un-updated `NE_GATEWAY_MODELS` that omits
  `context_window` will fail boot by design.
- Resource limits are chosen generously for a 16–32 vCPU reference VM; untested
  under sustained load until NE-077.
- The serve GPU profile targets an Azure-owner-gated GPU host (live-parked); the
  `--serve-cpu` tiny-model path and the hard-fail-without-NVIDIA path are the
  testable stand-ins.
