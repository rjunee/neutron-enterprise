# NE-071 + NE-072 — Production deployment tier: Helm umbrella chart + Terraform AKS/EKS (build-ready design)

**Status:** design complete, builds GATED on the NE-103a image merge (branch `ne/NE-103a`,
in flight). Fable-designed 2026-07-09 under the front-load posture.
**Units:** NE-071 — "Helm umbrella chart: production tier on vanilla K8s 1.31+,
OpenShift-restricted-SCC compat, Velero + DB-native backups with RPO/RTO per profile, HA notes";
NE-072 — "Terraform Azure AKS reference (VNet, Private Endpoints, Entra OIDC,
egress NSGs) + EKS smoke (C9 proof)".
**Stubs replaced:** `deploy/helm/README.md`, `deploy/terraform-azure/README.md`.
**Prime directive:** this is *packaging only* — the SAME first-party `ne-app` image and the SAME
promoted config bundle the pilot Compose tier uses, re-expressed as Kubernetes manifests and
cloud plumbing. No chokepoint (C6), gateway, or module code changes. The architecture doc's
promotion arrow is the contract: "PILOT --same images, promoted config--> PROD"
(`docs/architecture/neutron-enterprise-architecture.md:367`, status note :370).

---

## 0. Source of truth — what the chart must reproduce

The pilot Compose tier is the executable spec. Every hardening decision below is a 1:1
translation of an existing, guardian-enforced pilot invariant; none is new policy.

### 0.1 The NE-103a first-party image (branch `ne/NE-103a`)

`deploy/docker/ne-app/Dockerfile` (all bases tag@sha256-pinned, C11):

| Property | Dockerfile fact | K8s consequence |
|---|---|---|
| One image, multi-role | exec-form `CMD ["python","-m","ne.api"]`, **no ENTRYPOINT** (Dockerfile:71-74) | `ne-api` Deployment uses image default; `ne-worker` (post-NE-103c) is the same image with `command: ["python","-m","ne.worker"]` — one `image:` value shared by both workloads |
| Non-root | `useradd --system --uid 10001 ne` (Dockerfile:58), `USER 10001:10001` (Dockerfile:69) | `runAsNonRoot: true`; `runAsUser: 10001` on vanilla K8s, **omitted** under OpenShift (§1.5) |
| Writes only /tmp | `HOME=/tmp` (Dockerfile:64-65); compose runs `read_only: true` + `tmpfs: /tmp` | `readOnlyRootFilesystem: true` + `emptyDir` mounted at `/tmp` — the image is already arbitrary-UID-safe because nothing outside `/tmp` is written |
| Port | `EXPOSE 8000` (Dockerfile:70), uvicorn binds `NE_API_BIND_HOST:8000` | containerPort 8000; Service `ne-api:8000` |
| Console SPA carried | `/app/console-dist` copied in stage A | served by ne-api itself via `NE_API_SERVE_CONSOLE_DIST` (ne-api spec §3.4, `docs/architecture/ne-api-service-spec.md:361`) — **no separate console container** in the chart |
| Healthcheck | compose probes `http://127.0.0.1:8000/healthz` with the venv python (compose.yaml:236-247), `start_period: 60s` (litellm import cost) | liveness+readiness `httpGet /healthz :8000`; `initialDelaySeconds`/`startupProbe` sized to the same 60 s window; `/healthz` is auth-exempt by design (ne-api spec §1.8) |

### 0.2 The pilot Compose topology (`deploy/compose/pilot/compose.yaml` on `ne/NE-103a`)

Profiles `core` / `brain` / `obs` + included serve tier (`deploy/compose/serve/compose.yaml`
`gpu`/`cpu`), all third-party images tag@sha256-pinned (C11, compose.yaml:12), zero secret
literals — every credential is a `${NE_PILOT_*:?}` interpolation resolved from the bundle
SecretStore at `ne up` (C12, compose.yaml:15-17), telemetry off everywhere,
`no-new-privileges` everywhere, `cap_drop: ALL` on the stateless services, loopback-only
binds except Caddy (compose.yaml:13-14).

### 0.3 The ne-api environment surface (compose.yaml:148-247)

The chart's `ne-api` Deployment must reproduce this env block exactly (values-templated, same
names): `NE_API_BIND_HOST`, `NE_API_QDRANT_URL`, `NE_API_QDRANT_COLLECTION=ne_chunks`,
`NE_API_TEI_URL`, `NE_GATEWAY_MODELS` (JSON model menu — fail-closed if empty),
`NE_GOVERN_OPA_URL`, `NE_IDENTITY_OIDC__ISSUER`, `NE_IDENTITY_OIDC__AUDIENCE`,
`NE_API_APPROVER_ROLES`/`OPS_ROLES`/`STEWARD_ROLES`, and the five module DB URLs —
`NE_RUNTIME_SESSION_DATABASE_URL`, `NE_AUDIT_DATABASE_URL`,
`NE_PERMISSIONS_DATABASE_URL` (the former bare-`NE_DATABASE_URL` quirk — renamed by the
NE-PERMISSIONS-FOLLOWUP in NE-CLEAN4: `packages/ne-permissions/src/ne/permissions/config.py`
sets `env_prefix="NE_PERMISSIONS_"` consistent with its siblings, and compose + both Helm
charts carry the new name), `NE_FLYWHEEL_SIGNAL_DATABASE_URL`,
`NE_API_EVAL_DATABASE_URL`. DSN passwords come from a K8s Secret key (§1.6), never a literal.

### 0.4 The config bundle machinery (`tools/ne-cli/src/ne/cli/pilotcfg.py`, `up.py`)

- Bundle = `config/ne-runtime.toml` + `config/ne-govern.toml` + `config/ne-secrets.toml` +
  `secrets/required.json` (env-var → SecretStore `{path, field}`, NAMES only) +
  `manifest.json` carrying `config_hash` + tamper-evident `manifest_hash` (pilotcfg.py:8-40).
- `config_hash` is the composed NE-CKC12 platform hash over exactly the module set
  `ne.runtime.factory.freeze_platform_config` freezes — the same hash stamped on every
  AuditEvent (pilotcfg.py:29-35).
- `ne up` resolves `required.json` through `ne.secrets.create_secret_store` into an ephemeral
  0600 env file, brings the stack up, deletes the file (up.py `_write_runtime_env`,
  `_remove_runtime_env`).
- `ne-secrets` ships two adapters today: `sops-file` (pilot default) and `openbao` (prod
  profile) — `packages/ne-secrets/src/ne/secrets/config.py:23-57`.

### 0.5 NE-100 backup machinery (`tools/ne-backup`, `deploy/backup/`)

DB-native encrypted backups (pg_dump + qdrant snapshots + volume cold-tar), customer key in
the bundle SecretStore at `pilot/backup`/`key_b64`, stated pilot RPO ≤ 24 h / RTO ≤ 30 min
with an executable drill (`deploy/backup/README.md`). The pilot runner reaches postgres via
`docker exec` and qdrant over loopback — both need a K8s-native transport (§1.8).

---

## 1. NE-071 — Helm umbrella chart architecture

### 1.1 Chart layout

```
deploy/helm/
  neutron-enterprise/                  # the umbrella chart (apiVersion v2)
    Chart.yaml                         # version = platform release; kubeVersion: ">=1.31.0-0"
    values.yaml                        # the full documented surface (§1.3)
    values.schema.json                 # JSON-schema validation (digest-required, enums)
    templates/
      _helpers.tpl                     # securityContext helpers, image-ref helper (§1.4)
      networkpolicy-default-deny.yaml  # C2 posture, umbrella-level (§1.7)
      networkpolicy-allow.yaml         # per-service L4 allows generated from the topology map
      ingress.yaml                     # console/api vhost + keycloak vhost (§1.9)
      confighash-configmap.yaml        # the promoted bundle's module TOMLs + config_hash (§3)
    charts/                            # local first-party subcharts (file://, not registry deps)
      ne-api/                          # Deployment (stateless) + Service + PDB + HPA(optional)
      ne-worker/                       # Deployment, same image, command override (enabled post-103c)
      postgres/                        # StatefulSet + headless Service + PVC
      keycloak/                        # StatefulSet + Service (+ realm-import Job, post-103b)
      opa/                             # Deployment + ConfigMap(policies) + Service
      qdrant/                          # StatefulSet + Service + PVC
      tei/                             # two releases via alias: tei-embed, tei-rerank
      otel-collector/                  # Deployment + ConfigMap
      vllm/                            # Deployment (GPU or CPU mode) + Service + PVC(weights)
      ne-backup/                       # CronJob + drill Job + restore runbook (§1.8)
  README.md                            # replaces the stub; install + promotion runbook
```

**Decision — first-party subcharts, not adopted upstream charts.** This is a deliberate,
justified exception to the adopt-first default: (a) the pilot compose file is a
guardian-enforced executable spec — each service is a single container with exact pinned
digest, exact env, exact hardening, and the guardians (`tools/ne-cli/tests/test_pilot_stack.py`)
must extend to the chart with the same 1:1 assertions, which upstream charts' large indirect
values surfaces defeat; (b) Bitnami's 2025 registry/licensing lockdown makes the main
umbrella-chart ecosystem for postgres/keycloak supply-chain-encumbered, failing the
dependency-intake criteria; (c) every subchart here is ~3 small manifests. Where
adoption genuinely wins — HA PostgreSQL — the design adopts an operator instead (CloudNativePG,
Apache-2.0) as the documented HA option rather than reimplementing replication (§1.10, open
question Q2). Velero itself is adopted, not wrapped (§1.8).

### 1.2 Service mapping: pilot Compose → K8s

| Compose service (compose.yaml line) | K8s kind | Profile | Notes |
|---|---|---|---|
| `ne-api` (:148) | Deployment, ≥2 replicas | core | stateless by design (arch doc §4.1 process table); Service `ne-api:8000` |
| `ne-worker` (NE-103c, `packages/ne-worker`) | Deployment, 1..N | agents | same image, `command: ["python","-m","ne.worker"]`; DBOS partitions work safely at N>1 (ne103c design) |
| `postgres` (:74) | StatefulSet, 1 replica (v1.1 default) | core | PVC `pg-data` at `/var/lib/postgresql` (PG18 mount, compose.yaml:88); initdb ConfigMap from `deploy/compose/pilot/initdb/01-databases.sql` |
| `keycloak` (:102) | StatefulSet, 1 (HA note §1.10) | core | same `start --http-enabled --proxy-headers xforwarded` args; `KC_HOSTNAME` from ingress host |
| `opa` (:249) | Deployment, 2 replicas | core | policies ConfigMap from `deploy/compose/pilot/opa/policies/ne_system.rego`; same `--disable-telemetry` |
| `qdrant` (:279) | StatefulSet, 1 replica | brain | PVC `qdrant-data`; `QDRANT__TELEMETRY_DISABLED` |
| `tei-embed` / `tei-rerank` (:303/:330) | Deployment ×2 (aliased subchart) | brain | model-cache PVC or pre-staged weights image (air-gap, §1.7); `HF_HUB_OFFLINE` value |
| `otel-collector` (:356) | Deployment | obs | config ConfigMap from `deploy/compose/pilot/otel/otel-collector-config.yaml` |
| `vllm` / `vllm-cpu` (serve/compose.yaml:28/:76) | Deployment | serve (`mode: gpu|cpu`) | GPU: `nvidia.com/gpu: 1` resource + nodeSelector/toleration for the GPU pool (§2.4); weights PVC read-only; same telemetry-off env block |
| `caddy` (:34) | **replaced by Ingress** (§1.9) | core | Caddy was the single-VM TLS answer; K8s clusters bring an ingress controller. The Caddyfile's three vhosts map to Ingress rules |
| compose `include` of serve tier | subchart `vllm` | serve | same "off by default" posture: `profiles.serve.enabled: false` (C2, serve lanes opt-in) |

Compose `depends_on: service_healthy` ordering translates to readiness gates: every consumer
already fails-closed and retries (that is why compose can order them), so the chart relies on
readiness probes + restart policy, plus an optional `wait-for` initContainer on `ne-api` for
postgres (cheap, avoids first-boot CrashLoop noise while Keycloak runs its DB migration).

### 1.3 values.yaml surface (profiles mirror the Compose profiles)

```yaml
global:
  imageRegistry: ""          # customer registry prefix (ACR/ECR/Harbor); "" = images as-listed
  openshift: false           # §1.5 — omits fixed UIDs/fsGroup, restricted-SCC mode
  configHash: ""             # REQUIRED, stamped by `ne k8s render` from the bundle manifest (§3)
  hostname: ""               # REQUIRED — the console/API vhost (pilot NE_PILOT_HOSTNAME analogue)
  airGap: false              # C2: removes the frontier egress NetworkPolicy + asserts AIR_GAP env

profiles:                    # 1:1 with pilot README profile table (deploy/compose/pilot/README.md:23-33)
  core: true                 # ne-api, postgres, keycloak, opa, ingress
  brain: true                # qdrant, tei-embed, tei-rerank
  agents: false              # ne-worker — flips to true default once NE-103c merges
  obs: true                  # otel-collector
  serve:
    enabled: false           # off by default (C2 posture, serve/compose.yaml note)
    mode: gpu                # gpu | cpu  (the `ne up --serve-gpu|--serve-cpu` analogue)

image:                       # ONE first-party image for ne-api + ne-worker (§1.4)
  repository: ne-app
  tag: "0.1.0"
  digest: ""                 # REQUIRED non-empty (values.schema.json) — the C11 pin, §1.4

neApi:
  replicas: 2
  resources: {limits: {memory: 2Gi, cpu: "2"}}   # == compose limits (compose.yaml:229-234)
  env: {}                    # merged over the §0.3 baseline rendered by the template
  gatewayModels: []          # rendered into NE_GATEWAY_MODELS; sourced from the bundle (103b rider). NE-CTXB-2a: each entry now REQUIRES context_window (the served --max-model-len) or ne-api fails boot.
  pdb: {minAvailable: 1}
neWorker:
  replicas: 1
postgres:
  image: {repository: postgres, tag: 18.4-alpine, digest: "sha256:ecafd342..."}  # compose.yaml:75
  storage: {size: 64Gi, storageClassName: ""}
  external:                  # §2.2 — managed-DB variant (Azure Flexible Server via Private Endpoint)
    enabled: false
    hostSecretRef: {}        # host/port/sslmode from a Secret; disables the postgres subchart
keycloak: {image: {...digest...}, ...}
opa: {image: {...digest...}, replicas: 2}
qdrant: {image: {...digest...}, storage: {size: 64Gi}}
tei: {image: {...digest...}, offline: true}     # HF_HUB_OFFLINE=1 default in prod (weights pre-staged)
otel: {image: {...digest...}}
vllm: {image: {...digest...}, servedName: qwen3.6-35b-a3b, weights: {pvcName: "", size: 200Gi}}

secrets:
  mode: installer            # installer | external-secrets | csi   (§1.6)
  existingSecret: ""         # name of the pre-created Secret when mode != installer

ingress:
  className: ""              # customer's controller; OpenShift Route fallback documented
  tls: {secretName: ""}      # customer-provided cert (the pilot's Caddyfile.customcerts analogue)
  keycloakHost: ""           # the 8443 vhost analogue: auth.<hostname> (§1.9)

networkPolicy:
  enabled: true              # deny-by-default + generated allows (C2, §1.7)

backup:                      # §1.8
  enabled: true
  schedule: "0 2 * * *"      # == ne-backup.timer daily 02:00 (deploy/backup/README.md)
  destination: {pvcName: "", objectStore: {}}
  velero: {enabled: false, schedule: "0 3 * * *", includeVolumes: [pg-data, qdrant-data]}
```

Every third-party subchart's `image` block requires a `digest` field —
`values.schema.json` marks it `minLength: 7`, and the guardian (§1.11) asserts the shipped
`values.yaml` digests byte-match the pilot compose digests + provenance table
(`deploy/compose/pilot/README.md`). Digest drift between tiers becomes a test failure, not a
review comment.

### 1.4 Pinned images (C11) and the first-party image's registry story

Third-party images keep the exact pilot `tag@sha256` pins; the image-ref helper renders
`{{ registry }}/{{ repository }}:{{ tag }}@{{ digest }}` — Kubernetes resolves by digest, so a
retagged registry copy cannot substitute content.

The first-party `ne-app` image needs a **new pin mechanism**: the pilot's build-in-seal +
`--iidfile` local-image-id pin with `pull_policy: never`
(compose.yaml:149-162, `deploy/airgap/scripts/build-ne-app.sh:8-14`) does not exist on
multi-node K8s — nodes pull from a registry. Design:

- **`ne release push --image ne-app:0.1.0 --registry <acr>.azurecr.io/neutron`** (new ne-cli
  command, NE-071 scope): pushes the locally-built image to the *customer's* registry, captures
  the **repo digest** from the push result, and writes it into the render-time values overlay
  (`image.digest`). The push digest is the K8s analogue of the `--iidfile` image id: captured
  at the moment of transfer, asserted thereafter.
- The chart REFUSES to render with `image.digest` empty (schema + a template `fail`), so an
  unpinned first-party image cannot deploy — the same fail-closed posture as `ne up`'s
  `_check_app_image` preflight (up.py:90-110).
- Air-gapped clusters: `ne release push` also emits a `docker save` tarball + digest manifest
  for import into an in-network registry; the digest survives the transfer (it is
  content-addressed).
- This stays inside the C11 first-party exception's spirit: no third-party image may use the
  push-digest path; the guardian enforces that every non-`ne-app` repository in values carries
  an upstream registry digest identical to the compose pin.

### 1.5 Pod hardening parity + OpenShift restricted-SCC compatibility

A single helper (`_helpers.tpl`) renders the security contexts so parity is structural:

```yaml
# pod-level                                   # container-level
securityContext:                              securityContext:
  runAsNonRoot: true                            allowPrivilegeEscalation: false   # == no-new-privileges
  {{- if not .Values.global.openshift }}        capabilities: {drop: ["ALL"]}
  runAsUser: <image uid>                        readOnlyRootFilesystem: <per-service>
  runAsGroup: <image gid>                       seccompProfile: {type: RuntimeDefault}
  fsGroup: <volume gid>
  {{- end }}
```

Parity map (each row is an existing compose invariant):

| Compose directive | K8s field |
|---|---|
| `security_opt: no-new-privileges:true` (every service) | `allowPrivilegeEscalation: false` (every container) |
| `cap_drop: ALL` (caddy/ne-api/opa/otel) | `capabilities.drop: ["ALL"]` — extended to ALL containers in the chart (postgres/keycloak/qdrant/tei run fine without added caps under K8s; the compose gap was conservatism, not need). Exception: none — Caddy's `NET_BIND_SERVICE` (compose.yaml:52-56) disappears with Caddy itself; nothing binds <1024 in-cluster |
| `read_only: true` + `tmpfs: /tmp` (ne-api, compose.yaml:220-222) | `readOnlyRootFilesystem: true` + `emptyDir` at `/tmp` for ne-api/ne-worker/opa/otel; stateful services get `readOnlyRootFilesystem: false` v1 (open question Q3 tracks tightening with targeted emptyDirs) |
| `deploy.resources.limits` (every service) | `resources.limits` copied verbatim; requests default to limits/2 |
| healthchecks (every service) | readiness+liveness probes with the same endpoints/intervals; distroless otel-collector (no shell — compose.yaml:374-377) gets an HTTP probe against its extension port instead of none |

**OpenShift restricted-v2 SCC rules honored:**

1. **No fixed UIDs when `global.openshift=true`** — restricted SCC's `MustRunAsRange` assigns
   an arbitrary UID from the project's annotation range and *rejects* pods pinning
   `runAsUser` outside it. The helper omits `runAsUser`/`runAsGroup`/`fsGroup` entirely and
   keeps `runAsNonRoot: true` (the SCC-assigned UID is always non-root, and the ne-app image's
   `USER 10001:10001` numeric form satisfies the non-root *image* check on vanilla K8s too).
2. **Arbitrary-UID safety of ne-app is already real**: `HOME=/tmp`, read-only rootfs, writes
   confined to the mounted `/tmp` emptyDir; the venv under `/app` is world-readable. No chart
   work needed — assert it in the kind CI smoke by running the container with a random UID
   (`runAsUser: 1000700001`) and hitting `/healthz`.
3. **Third-party images are the risk** (Risk R1): `postgres:18.4-alpine` official images are
   NOT arbitrary-UID-safe (initdb chown expectations); `quay.io/keycloak/keycloak` and
   `qdrant/qdrant` are OpenShift-tolerant; TEI/vLLM untested. v1.1 ships `global.openshift`
   as **supported for the first-party workloads + documented per-infra-service compat matrix**,
   with the managed/external-DB variant (§2.2 `postgres.external`) as the recommended
   OpenShift pairing for postgres, and CNPG (whose images are arbitrary-UID-safe) as the
   in-cluster alternative. This scoping is stated in the chart README, not discovered by the
   customer.
4. No `hostPath`, no `hostNetwork`, no privileged, no added capabilities anywhere in the
   chart — nothing requires more than restricted-v2. The vLLM GPU pod is the one exception
   candidate (GPU device plugin), which is why serve is profile-gated and its OpenShift story
   is documented as "requires the NVIDIA GPU operator's SCC" (Risk R6).

### 1.6 Secrets (C12 secret-hygiene, SPEC C12 posture per compose.yaml:15-17)

Zero secret literals in the chart, values, or rendered manifests — the values surface carries
secret *names/refs only*, exactly like `secrets/required.json` carries `{path, field}` names
(pilotcfg.py:55-59). Three modes, one template seam (`secrets.mode`):

1. **`installer` (default — the pilot-parity path).** `ne k8s up --bundle <dir>` (new ne-cli
   command mirroring `ne up`, §3) resolves `secrets/required.json` through the bundle
   SecretStore (`ne.secrets.create_secret_store`, same code path as up.py:258-292) and creates
   ONE namespaced `Secret/ne-platform-secrets` with keys = the env-var names
   (`NE_PILOT_POSTGRES_PASSWORD`, `NE_PILOT_KEYCLOAK_ADMIN_PASSWORD`, + prod additions).
   Deployments consume via `secretKeyRef`; DSN-shaped values (the five DB URLs) are composed
   *inside the container env* from `$(NE_PILOT_POSTGRES_PASSWORD)` dependent-env interpolation
   so the password never appears pre-assembled in a manifest. The Secret is created by the
   installer, never by Helm (no secret material in release manifests/history — `helm get
   values` stays clean).
2. **`external-secrets` (prod profile, recommended for AKS/EKS).** The customer runs
   External Secrets Operator; the chart renders `ExternalSecret` CRs pointing at the
   customer's store. The natural pairing is the platform's own **OpenBao** prod SecretStore
   (`packages/ne-secrets/src/ne/secrets/openbao.py`): the same paths recorded in
   `required.json` (`pilot/postgres` etc.) become the ESO `remoteRef.key`s — the bundle's
   secret-name manifest is the single source of truth in every mode.
3. **`csi` (documented, not templated in v1.1).** Azure Key Vault / AWS Secrets Manager CSI
   provider mounting into the pod; documented in the NE-072 references with the mapping table,
   because env-var-shaped settings (pydantic-settings) prefer the Secret/ESO path — CSI files
   would need an env bridge.

App-level SecretStore use (agent credentials, NE-036; backup key) is orthogonal: the pods get
`config/ne-secrets.toml` from the promoted-bundle ConfigMap (§3) selecting `openbao` in prod,
and reach the OpenBao service over the cluster network. OpenBao itself is customer-operated or
a later optional subchart — NOT in NE-071 scope (the `sops-file` adapter cannot work in-pod;
`installer` mode fully covers deployments without OpenBao).

### 1.7 Network posture (C2) — deny-by-default + the single frontier lane

The pilot enforces C2 topologically: loopback-only binds + Caddy sole publisher + telemetry
off. The K8s translation:

- **`networkpolicy-default-deny.yaml`**: deny all ingress + egress in the release namespace.
- **Generated allows** from a static topology map in `_helpers.tpl` (consumer → producer:port),
  mirroring the compose wiring: ingress-controller→ne-api:8000, ingress→keycloak:8080,
  ne-api→{postgres:5432, keycloak:8080, opa:8181, qdrant:6333, tei-embed:80, tei-rerank:80,
  vllm:8000}, ne-worker→same set, all→otel-collector:4317-4318, all→kube-dns:53. The guardian
  diff-tests this map against the compose env URLs so a new dependency cannot appear without a
  policy row.
- **The frontier lane**: one egress policy on the `ne-api`/`ne-worker` pods only (the gateway
  lives in-process there) allowing 443 to the frontier API range. Vanilla NetworkPolicy is
  L3/L4 — it cannot express FQDNs; therefore the chart's lane policy is deliberately coarse
  (allow 443 egress from gateway pods) and the **precise FQDN allowlist is enforced at the
  cloud layer** (NE-072 NSG/Azure Firewall FQDN rules, §2.3). Clusters running Cilium can
  tighten in-cluster with `CiliumNetworkPolicy` FQDN rules — shipped as an optional template
  behind `networkPolicy.ciliumFqdn.enabled` (documented, off by default). This split — coarse
  in-cluster, exact at the perimeter — is stated plainly in the README (Risk R4).
- **`global.airGap=true`** removes the frontier egress policy entirely and injects
  `AIR_GAP=true` env (the C2 proof posture). TEI/vLLM already run offline
  (`HF_HUB_OFFLINE`, weights pre-staged — serve/compose.yaml:21-24).

### 1.8 Backups: Velero + DB-native (NE-100 promoted), RPO/RTO per profile

Two complementary layers, exactly as the SPEC row demands:

**DB-native (primary, application-consistent).** The `ne-backup` subchart ships a CronJob
(default `0 2 * * *`, matching `ne-backup.timer` — `deploy/backup/README.md`) running the
existing `tools/ne-backup` tool with a **new `k8s` runtime backend** (NE-071 scope, small):
the pilot backend reaches postgres via `docker exec` and qdrant via loopback
(`deploy/backup/README.md` destination-guidance section); the k8s backend instead
(a) runs `pg_dump` in-pod over TCP to the `postgres` Service — the CronJob pod uses the SAME
pinned postgres image so dump/restore client versions match the server, (b) drives qdrant
snapshots over `http://qdrant:6333` (the snapshot API is already the pilot mechanism),
(c) writes to `backup.destination` (PVC or S3-compatible object store via the existing
ObjectStore contract). Encryption, manifest, retention (`prune --keep 7`), key handling
(`pilot/backup`/`key_b64`, no escrow) are unchanged — same tool, same restore runbook, same
drill; the drill (`.github/workflows/backup-restore-drill.yml`) gains a kind-cluster variant.

**Velero (secondary, infrastructure-level).** `backup.velero.enabled` renders Velero
`Schedule`/`BackupStorageLocation` CRs (Velero itself is installed by the customer/NE-072
terraform — adopted, not wrapped): PV snapshots of `pg-data`/`qdrant-data` + namespace object
backup, with a **pre-hook** that pauses the ne-backup CronJob during the snapshot window.
Velero gives fast whole-namespace DR; ne-backup gives portable, encrypted,
application-consistent restores (and is the only layer that is crash-consistent-proof for
postgres). The README states the precedence: restore DB state from ne-backup, cluster state
from Velero.

**RPO/RTO per profile** (documented in the chart README, drill-measured where marked):

| Profile | State | Mechanism | RPO | RTO |
|---|---|---|---|---|
| core (postgres) | all module DBs (one `neutron` DB, §0.3) | ne-backup pg_dump (+ Velero PV snap) | ≤ 24 h default; schedule configurable to 1 h (pg_dump-scale permitting) | ≤ 30 min at pilot-scale corpora (≤10 GiB), drill-asserted (`NE_BACKUP_DRILL_RTO_SECONDS`) |
| core (keycloak) | realm/users in postgres `keycloak` DB | same dump run | same as core | same |
| brain (qdrant) | vectors | qdrant snapshot per run | ≤ 24 h; **re-ingestable** from sources (the connectors are the recovery of last resort) | snapshot upload dominated |
| brain (tei caches) | model weights cache | none — re-staged artifacts | n/a | re-stage time |
| agents (ne-worker) | DBOS workflow state in postgres | same dump run | same as core | same; in-flight turns re-run from the top (ne103c design fail-safe note) |
| obs | none (collector is a pipe) | none | n/a | n/a |
| serve (vllm) | weights PVC (read-only) | excluded from Velero by default (size); re-stage via `ne serve download` | n/a | weight re-stage |

Cross-service skew note carried over verbatim: a backup run is not a global point-in-time
across postgres+qdrant; skew bounded by run duration (`deploy/backup/README.md`).

### 1.9 Ingress / TLS (the Caddy replacement)

The pilot Caddyfile (`deploy/compose/pilot/caddy/Caddyfile`) defines three surfaces: a
loopback healthz, the 443 console/API vhost, and the 8443 Keycloak vhost. In K8s:

- One `Ingress` with two hosts: `{{ .Values.global.hostname }}` → `ne-api:8000` (which serves
  both the API and the console SPA per §0.1) and `{{ .Values.ingress.keycloakHost }}`
  (default `auth.<hostname>`) → `keycloak:8080`. Keycloak keeps
  `--proxy-headers xforwarded` and `KC_HOSTNAME=https://<keycloakHost>` — same trust model as
  compose.yaml:106-113, controller replaces Caddy as the sole TLS terminator.
- TLS: customer-provided cert Secret (`ingress.tls.secretName`) — the prod analogue of
  `Caddyfile.customcerts`. No cert-manager dependency in v1.1 (it would add an ACME egress
  path that fights C2; documented as a customer option for internal issuers).
- OpenShift: the README documents the `Route` equivalent (or `route.openshift.io` objects
  behind `global.openshift` — decided at build time by whichever the kind/CRC smoke can
  actually exercise; Q5).
- `NE_IDENTITY_OIDC__ISSUER` becomes `https://<keycloakHost>/realms/ne-pilot` — the pilot's
  `:8443` port-based split becomes host-based (compose.yaml:181).

### 1.10 HA notes (the SPEC row's "HA notes" deliverable)

- **ne-api** — stateless (arch doc §4.1: "stateless, scalable"): default 2 replicas,
  `PodDisruptionBudget minAvailable: 1`, `topologySpreadConstraints` across zones/nodes,
  optional HPA. SSE sessions reconnect via the durable stream fallback (ne103c design §4).
- **ne-worker** — 1..N; DBOS coordinates via Postgres, share-by-storage singletons
  (ne103c design: NE-070H ledger/approval store), so replicas scale without a leader.
- **opa** — 2 replicas + PDB; policies are a ConfigMap, so it is trivially horizontal.
- **postgres** — v1.1 default is a single-replica StatefulSet (the honest pilot-parity
  posture). HA options, in order: (a) `postgres.external` → cloud-managed HA postgres
  (zone-redundant Azure Flexible Server, §2.2) — the *recommended* production answer;
  (b) CloudNativePG operator (adopt, Apache-2.0) for in-cluster replication — designed-for
  seam (`postgres.cnpg.enabled` reserved), built when an engagement demands it (Q2). Never
  hand-rolled streaming replication.
- **keycloak** — 1 replica v1.1; >1 requires `KC_CACHE=ispn` with `jgroups` K8s discovery
  (headless service + `KC_CACHE_STACK=kubernetes`) — the StatefulSet + headless Service are
  already the right shape; documented as the flip, not defaulted (Keycloak clustering has
  sharp first-boot-migration edges; Risk R5).
- **qdrant** — single-node v1.1 (pilot parity). Qdrant distributed mode changes the API
  surface (collection replication factor) — engagement-time option, noted not defaulted.
- **tei / vllm** — stateless; replicas scale by GPU/CPU budget.
- Stateful pods get `podAntiAffinity` off (single replica) but zone-pinned PVs documented;
  stateless pods get preferred anti-affinity + spread constraints by default.

### 1.11 Chart CI + guardians (no cloud required)

- `helm lint` + `helm template | kubeconform -kubernetes-version 1.31.0` in PR CI.
- **Parity guardian** (`tools/ne-cli/tests/test_helm_chart.py`, mirrors
  `test_pilot_stack.py`): renders the chart and asserts — every image digest matches the pilot
  compose pin byte-for-byte (except `ne-app`, which must be digest-non-empty); every container
  has `allowPrivilegeEscalation: false` + `capabilities.drop: [ALL]` + `runAsNonRoot`;
  ne-api env keys == the compose env keys (the same required-env-keys guardian that exists for
  compose, `tools/ne-cli/tests/test_pilot_stack.py`); zero secret literals in rendered output
  (the C12 scanner pattern from `test_pilot_secret_hygiene.py` applied to `helm template`);
  default-deny NetworkPolicy present; `openshift=true` render contains no `runAsUser`.
- **kind smoke** (`.github/workflows/helm-smoke.yml`): kind 1.31 cluster, install
  core+brain+obs with `secrets.mode=installer` fed by a CI test bundle, wait ready, assert
  `/healthz` 200 + one authenticated console call + the arbitrary-UID run (§1.5.2). This is
  the merge gate; cloud smokes (§2.5) are scheduled/owner-gated.

---

## 2. NE-072 — Terraform Azure AKS reference + EKS smoke

### 2.1 Layout

```
deploy/terraform-azure/
  README.md                 # replaces the stub: prereqs, apply order, promotion runbook
  versions.tf               # azurerm ~> 4.x pinned, required_version pinned
  main.tf / variables.tf / outputs.tf
  modules/
    network/                # VNet, subnets, NSGs, private DNS zones
    aks/                    # cluster, node pools, workload identity, OIDC issuer
    registry/               # ACR (Premium — private endpoint support), content-trust off (digest-pinned instead)
    data/                   # optional: Azure Database for PostgreSQL Flexible Server (private)
    storage/                # backup/Velero storage account (private endpoint, versioning, immutability opt)
    identity/               # Entra app registration for the Keycloak broker + workload identities
    egress/                 # Azure Firewall (or NAT+NSG minimal variant) — the FQDN allowlist (C2)
deploy/terraform-eks/       # the C9 smoke — deliberately minimal (§2.5)
```

State backend, subscription, region are variables; the reference defaults to the owner's
provisioned region (East US 2 — the GPU quota request target).

### 2.2 Network: VNet, subnets, Private Endpoints, private DNS

- **VNet** `10.20.0.0/16` with subnets: `snet-aks-system`, `snet-aks-user`, `snet-aks-gpu`
  (delegated to AKS), `snet-private-endpoints` (PE NICs), `snet-egress` (firewall/NAT).
- **Private AKS cluster** (`private_cluster_enabled = true`): API server via Private Endpoint;
  `privatelink.<region>.azmk8s.io` private DNS zone linked to the VNet (and to the ops VNet/
  VPN that runs `helm`/`ne k8s up` — the runbook is explicit that deployment happens from
  inside the network; Risk R7).
- **Private Endpoints** for: ACR (`privatelink.azurecr.io`), the backup/Velero storage account
  (`privatelink.blob.core.windows.net`), Key Vault if `secrets.mode=csi`
  (`privatelink.vaultcore.azure.net`), and — when the managed-DB variant is on — PostgreSQL
  Flexible Server (`privatelink.postgres.database.azure.com`). Each zone is created + linked by
  the `network` module; PE DNS is the classic failure mode, so `terraform output` prints a
  resolution checklist and the README carries a `dig` verification step (Risk R3).
- **Managed-DB variant** (`var.managed_postgres = true`): zone-redundant Flexible Server,
  private access only; feeds the chart's `postgres.external` values (§1.10) — this is the
  recommended production HA answer and the OpenShift-postgres escape hatch (§1.5.3).

### 2.3 Egress: deny-by-default + the single frontier lane (C2)

- **NSGs** on every subnet: default-deny outbound to Internet; explicit allows only for
  intra-VNet, the AKS-required service tags (`AzureCloud.<region>` scoped rules for
  MCR/management traffic — enumerated per the AKS outbound-requirements doc, pinned in the
  module with comments), and NTP.
- **The frontier lane**: NSGs cannot filter FQDNs, so the reference routes cluster egress
  through **Azure Firewall** (`egress` module, `outboundType = userDefinedRouting` + a default
  route to the firewall) with an application-rule collection allowlisting exactly the
  frontier-model FQDNs the gateway menu names (variable `frontier_fqdns`, default
  `["api.anthropic.com"]`-shaped, empty ⇒ air-gap). This is the perimeter half of the §1.7
  split: NetworkPolicy bounds *which pods* may egress; the firewall bounds *where to*.
- **Minimal-cost variant** (`var.egress_mode = "nat"`): NAT Gateway + NSG service-tag rules
  only — honest note that FQDN precision is lost (pilot-adjacent budgets); default is
  `firewall`.
- `AIR_GAP` deployments set `frontier_fqdns = []` — the firewall then has zero
  Internet-application rules and the AKS-required infra rules only.

### 2.4 AKS cluster + the GPU serve pool

- K8s **1.31+** (the chart's `kubeVersion`), system pool (2× D4ds_v5), user pool
  (autoscaling D8ds_v5 for core/brain/agents/obs), and an optional **GPU pool**:
  `Standard_NV36ads_A10_v5` (the NVadsA10v5 family the owner's quota request targets — East
  US 2, limit 36 vCPU ⇒ one NV36 node),
  `--node-taint nvidia.com/gpu=present:NoSchedule`, scale-to-zero. The vllm subchart's GPU
  mode sets the matching toleration + `nodeSelector` + `nvidia.com/gpu: 1`. GPU pool creation
  is gated on `var.gpu_node_pool = true` so `terraform apply` succeeds before the quota grant
  lands (Risk R2).
- **Workload identity + OIDC issuer enabled**; used by Velero (storage account access) and
  ESO (Key Vault) — no static cloud credentials in-cluster.
- **Entra ID OIDC (the Keycloak-broker story, arch doc §4.2)**: Keycloak remains broker-only —
  Entra stays the IdP of record. The `identity` module creates the Entra **app registration**
  for the broker (redirect URI `https://auth.<hostname>/realms/ne-pilot/broker/entra/endpoint`,
  ID-token claims for groups), outputs `client_id`/tenant endpoints, and writes the client
  secret **directly to the customer secret store** (Key Vault) — never to TF state outputs.
  Wiring the broker *into the realm* is NE-103b's realm-import artifact extended with an
  `identityProviders` entry — NE-072 provisions the Entra side + documents the join. AKS's own
  admin access uses Entra-integrated RBAC (`azure_active_directory_role_based_access_control`).
- **How the chart deploys onto it** (runbook order): `terraform apply` → `ne release push
  --registry <acr>` (§1.4) → `ne pilot promote` (bundle) → `ne k8s render --bundle` (§3) →
  `ne k8s up` (installer Secret + `helm upgrade --install`) → smoke (`/healthz` via the
  ingress, one governed call). Velero installed by terraform (helm_release) with the storage
  module's blob container + workload identity.

### 2.5 EKS smoke (C9 proof)

> **Promotion note (2026-07-18, NE-AWS-14):** this section is the historical
> NE-072 design record. `deploy/terraform-eks/` has since been promoted in
> place to a hardened C2/C9-parity AWS reference (NE-AWS-1..13) — current
> state and promotion design: `deploy/terraform-eks/README.md`.

Deliberately minimal — it exists to prove "same chart, different cloud", not to be a second
reference (`deploy/terraform-eks/`):

- Terraform: the upstream `terraform-aws-modules/eks` module pinned by version+hash, 1.31,
  one managed node group (2× m6i.xlarge), default VPC module, no GPU, no firewall (the C2
  perimeter story is proven on Azure; EKS smoke proves *portability*).
- Deploy: the SAME umbrella chart, core+brain+obs profiles, `secrets.mode=installer`, images
  from ECR (pushed by the same `ne release push` — proving the registry story is
  cloud-neutral), assert `/healthz` + one authenticated console call + config_hash annotation
  equality with the source bundle.
- CI: the `eks-hardened-apply` workflow (weekly schedule dropped at NE-AWS-13),
  `workflow_dispatch` only, gated on AWS credentials being configured (skips cleanly
  otherwise), `terraform destroy` in `always()`. The C9 enforcement row ("EKS smoke
  at NE-072") is satisfied by this workflow existing + a recorded green run
  in the release evidence, not by per-PR cloud spend.

---

## 3. The promotion path: pilot bundle → prod values (config_hash-consistent)

The concrete mechanism — "same images, promoted config" made executable:

1. **Same bundle.** The per-customer bundle from `ne pilot promote` is reused as-is: module
   TOMLs, `secrets/required.json`, `manifest.json` with `config_hash` + `manifest_hash`
   (pilotcfg.py:8-40). No prod-specific bundle format.
2. **`ne k8s render --bundle <dir> --out values-<customer>.yaml`** (new ne-cli command,
   NE-071 scope): verifies the bundle (`ne pilot verify` — fail-closed on `manifest_hash`,
   pilotcfg.py:21-27), then emits a values overlay containing `global.configHash` (from the
   manifest), `global.hostname` (from the manifest — the same TRUSTED field `ne up` feeds to
   `KC_HOSTNAME`), and the bundle files inlined for the `confighash-configmap.yaml` template
   (module TOMLs mounted read-only at `/etc/neutron/config/`; `NE_*_CONFIG_FILE` env points
   the settings loaders at them, same per-module-TOML shape the bundle already mandates —
   pilotcfg.py:37-40).
3. **Hash consistency is asserted, not assumed.** The rendered ConfigMap carries
   `neutron.dev/config-hash: <hash>`; every first-party pod gets the same annotation. At
   startup ne-api/ne-worker already freeze and stamp the composed platform config_hash on
   every AuditEvent (`ne.runtime.factory.freeze_platform_config`, NE-CKC12) — the kind/EKS
   smoke asserts the runtime-frozen hash equals the annotation equals the pilot bundle's
   `manifest.json` value. Same bundle ⇒ same bytes ⇒ same hash on both tiers; a prod config
   drift is a visible hash mismatch in the audit stream.
4. **Rollout-on-change for free:** the pod-template annotation means a re-promoted bundle
   (new config_hash) triggers a rolling restart on `helm upgrade` — the standard
   checksum-annotation pattern, keyed on the platform's own hash instead of a template sha.
5. **Same image:** `image.digest` in the overlay is the `ne release push` digest of the very
   image the pilot ran (§1.4) — the tag (`ne-app:0.1.0`, single-sourced in
   `deploy/airgap/scripts/build-ne-app.sh:26` / up.py:39) and the content are carried; only
   the pin mechanism changes (local iidfile → registry digest).
6. **Secrets promote by NAME:** `required.json` is backend-agnostic (paths+fields only), so
   moving `sops-file` → `openbao`/Key Vault means repopulating the same paths in the prod
   store and flipping `config/ne-secrets.toml` — `ne k8s up` (installer mode) or ESO
   (external-secrets mode) resolves them identically (§1.6). `ne-secrets.toml` is
   deliberately outside the config_hash preimage (pilotcfg.py:34-36), so the backend flip
   does NOT change the platform identity hash — exactly the property promotion needs.

---

## 4. Governance / constraints mapping

| Constraint | Where honored |
|---|---|
| C2 no-egress / single frontier lane | default-deny NetworkPolicies + gateway-pod-only egress policy (§1.7); Azure Firewall FQDN allowlist + deny-by-default NSGs (§2.3); `global.airGap` strips the lane; serve profile off by default; telemetry-off env blocks carried verbatim |
| C9 cloud-portability | no cloud API touched by any chart template (cloud specifics live ONLY in terraform); AKS = reference, EKS smoke = proof (§2.5); kind CI proves no-cloud installability |
| C10 single-tenant (as it then stood; that C10 was REMOVED 2026-07-17) | one release = one customer namespace = one bundle; no multi-tenant values dimension exists; terraform reference is per-customer subscription/VNet |
| C11 pinned digests | all third-party `tag@sha256` pins byte-copied from the pilot compose + schema-required `digest` fields + parity guardian (§1.4, §1.11); first-party push-digest pin with fail-closed empty check |
| C12 secret hygiene (compose.yaml:15-17 convention) | no secret literal in chart/values/manifests; installer-created Secret from the bundle SecretStore, names-only refs, C12 scanner run over `helm template` (§1.6, §1.11). (The C12 prompt-injection guardrails are app-layer — untouched, packaging only) |
| No chokepoint changes | zero changes to ne-govern/ne-runtime/ne-gateway/ne-api code; the only ne-cli additions are `ne release push`, `ne k8s render`, `ne k8s up` (installer plumbing) + a `k8s` runtime backend in tools/ne-backup |

---

## 5. Decomposition + sequencing

Both units strictly after NE-103a merges (image tag/digest + compose env are their input).
NE-103b (console vhost/realm) and NE-103c (ne-worker) refine but do not block: the chart lands
with `profiles.agents=false` and the 103b realm-import Job stubbed, flipped by one-line riders
when those merge — same parking pattern the compose tier used (compose.yaml:163-168 comments).

| Sub-unit | Lands | Gate |
|---|---|---|
| **NE-071a — umbrella core** | chart skeleton, `_helpers.tpl` hardening/image helpers, subcharts ne-api/postgres/keycloak/opa, ingress, `secrets.mode=installer` + `ne k8s render`/`ne k8s up`, `ne release push`, values.schema.json, parity guardian, kind smoke CI | NE-103a merged |
| **NE-071b — profiles + posture** | qdrant/tei/otel/vllm subcharts (brain/obs/serve), NetworkPolicies + airGap, `global.openshift` + arbitrary-UID smoke, ESO mode templates | 071a |
| **NE-071c — HA + backups** | PDBs/spread/replicas defaults, keycloak-HA + CNPG/external-postgres seams documented, ne-backup `k8s` runtime + CronJob subchart, Velero Schedules + hooks, RPO/RTO README, kind drill variant | 071a; ne-worker rider on NE-103c |
| **NE-072a — AKS reference** | modules network/aks/registry/identity/egress/storage/data, private DNS zones, Entra broker app registration, GPU pool (flag-gated on quota), Velero install, deploy runbook, AKS live smoke (owner-gated) | 071b; GPU pool additionally owner-quota-gated |
| **NE-072b — EKS smoke** | `deploy/terraform-eks/`, ECR push path, `eks-hardened-apply.yml` workflow (renamed from the original eks-smoke workflow at NE-AWS-13), C9 evidence recording | 071b + AWS account (owner) |

Parallelism: 071a→071b→071c sequential (one chart lane); 072a can start against 071b's chart;
072b against the same. The Fable-designed artifacts here are complete inputs for Forge builds.

---

## 6. Open questions + risks

**Open questions (need an owner/architect call before or during build):**

- **Q1 — secret mode default for the AKS reference:** `installer` (pilot-parity, zero new
  operators) vs `external-secrets`+OpenBao (SPEC prod-profile posture). Design
  default: `installer` for the chart, ESO documented+templated; the AKS README recommends ESO.
  Confirm.
- **Q2 — in-cluster HA postgres:** adopt CloudNativePG now vs reserve the seam and lead with
  the managed-DB variant. Design: reserve + managed-first (adopt-not-build either way). An
  engagement with hard in-cluster HA pulls CNPG forward.
- **Q3 — readOnlyRootFilesystem on third-party stateful images** (postgres/keycloak/qdrant/tei):
  chase the per-image writable-path lists in v1.1 or accept rw-root with all caps dropped.
  Design: accept rw-root v1.1, tracked tightening item.
- **Q4 — EKS smoke cadence/spend:** weekly scheduled vs release-evidence-only manual runs.
- **Q5 — OpenShift verification vehicle:** CRC/OpenShift-local in CI is heavy; is a documented
  manual verification + arbitrary-UID kind test sufficient for the v1.1 claim?

**Risks:**

- **R1 — OpenShift arbitrary-UID on third-party images** (worst: official `postgres` image).
  Mitigation: scoped claim (first-party fully compatible; per-service matrix; external/CNPG
  postgres path) — §1.5.3. Do not promise blanket restricted-SCC for the whole stack in v1.1.
- **R2 — GPU quota is owner-gated and slow** (NVadsA10v5 East US 2 request in flight): the serve pool is flag-gated so 072a applies
  green without it; the serve-profile live proof may trail the unit — CPU-mode vllm keeps the
  lane testable meanwhile (same trick as `ne up --serve-cpu`).
- **R3 — Private Endpoint DNS** is the top field-failure mode (unresolvable `privatelink.*`
  from the deploy host / peered VNets). Mitigation: zones created+linked in-module, output
  checklist, README `dig` verification step, ops-access note for the private API server (R7).
- **R4 — FQDN egress cannot be enforced by vanilla NetworkPolicy**: in-cluster the lane is
  L4-coarse; exactness lives at the Azure Firewall. Stated plainly; Cilium FQDN template
  optional. On clusters without either, C2's FQDN precision degrades to the pod-scoped 443
  rule — documented, never silent.
- **R5 — Keycloak first-boot migration + future clustering**: the pilot's 60-retry health gate
  (compose.yaml:131-146) translates to generous startupProbes; HA flip has jgroups/cache
  sharp edges — deliberately not defaulted.
- **R6 — vLLM GPU pods on OpenShift** need the NVIDIA GPU operator's SCC — out of
  restricted-v2 scope by nature; documented exception.
- **R7 — private cluster operability**: `ne k8s up`/CI must run inside the network (runner in
  VNet or VPN). The reference includes a small ops-runner pattern in the README rather than
  weakening to a public API server.
- **R8 — first-party digest pin changes shape** (local iidfile → registry push digest): the
  air-gap Phase-A image-id assertion does not carry to K8s as-is; the push-digest + schema
  fail-closed check is the replacement and must be guardian-covered from day one (§1.4,
  §1.11) so the C11 exception never widens.
