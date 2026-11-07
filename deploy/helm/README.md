# deploy/helm

The production Kubernetes tier — a Helm umbrella chart at
`neutron-enterprise/` reproducing the hardened pilot Compose tier
(`deploy/compose/pilot/compose.yaml`, the executable spec) as Kubernetes
manifests. **NE-071a** shipped the CORE-profile FOUNDATION (`ne-api`,
`postgres`, `keycloak`, `opa`, a host-based Ingress, the C11 first-party
push-digest pin, the C12 installer-mode secret flow); **NE-089** added the
default-OFF `ne-api` HA surface (PDB/affinity/spread); **NE-071b** added the
durable **`ne-worker`** Deployment (the NE-103c worker on the shared ne-app
image) plus the default-OFF prod-hardening surface — NetworkPolicy, an
OpenShift Route alternative, and a pg_dumpall backup CronJob. **NE-071c**
completes the chart to **full-platform parity**: the **brain** services
(`qdrant` + `tei-embed`/`tei-rerank`) ride the **default deploy** (ne-api's
retrieval backends), and two default-OFF profiles land — **obs**
(`otel-collector` only) and **serve** (`vllm`, GPU-gated, render-only in CI).
See `docs/architecture/prod-tier-helm-terraform-design.md` for the full 071/072
design (NE-072 Terraform is not built yet — see "Pending" below).

## What's here

```
deploy/helm/
  neutron-enterprise/
    Chart.yaml, values.yaml, values.schema.json
    templates/_helpers.tpl                    # shared securityContext/image-ref helpers
    templates/confighash-configmap.yaml       # promoted bundle TOMLs, config_hash annotation
    templates/ingress.yaml                    # host-based Ingress (suppressed when route.enabled)
    templates/route.yaml                      # NE-071b OpenShift Route alt (route.enabled)
    templates/networkpolicy-default-deny.yaml # NE-071b C2 deny-all (networkPolicy.enabled)
    templates/networkpolicy-allow.yaml        # NE-071b C2 least-privilege allows
    templates/backup-cronjob.yaml             # NE-071b pg_dumpall CronJob (backup.enabled)
    templates/backup-pvc.yaml                 # NE-071b backup PVC (backup.enabled)
    charts/ne-api/                            # Deployment + Service + PDB (first-party image)
    charts/ne-worker/                         # NE-071b Deployment + PDB (same image, no Service)
    charts/postgres/                          # StatefulSet + headless Service + initdb ConfigMap
    charts/keycloak/                          # StatefulSet + Service
    charts/opa/                               # Deployment + Service + policies ConfigMap
    charts/qdrant/                            # NE-071c StatefulSet + Service (brain, default deploy)
    charts/tei/                               # NE-071c tei-embed + tei-rerank StatefulSets + Services (brain)
    charts/obs/                               # NE-071c otel-collector Deployment + Service + config CM (profiles.obs)
    charts/vllm/                              # NE-071c vllm Deployment + Service (profiles.serve.enabled, GPU-gated)
  examples/values-production-ha.yaml          # NE-089/NE-071b HA overlay (ne-api + ne-worker)
  tests/values-ci.yaml                        # render-only fixture (helm lint/template/kubeconform)
  tests/values-kind.yaml                      # kind-specific trims for the live smoke (zarf-safe: no profile flips)
  tests/values-kind-obs.yaml                  # NE-071c one-knob obs overlay for the kind smoke (helm-smoke only)
  tests/values-kind-nobrain.yaml              # NE-071c-FU one-knob brain-OFF overlay for the kind smoke (helm-smoke only)
  README.md                                   # this file
```

## ⚠️ Rolling upgrades and the resumed-run terminal outcome (POST_DEMO item 4)

**Read this before the first `helm upgrade` that crosses the commit which introduced
`approved_effect_complete`.**

A run that resumes a human-approved effect now publishes the terminal outcome
`approved_effect_complete` instead of `execution_complete`. A worker from a release BEFORE that
commit meets a value its closed `outcome` vocabulary does not contain when DBOS recovery hands it
such a run. ⚠️ **What it then DOES is unverified — see the next paragraph, which this sentence used to
contradict three lines ahead of it.** It may reject the value, or it may recover the run and succeed
SILENTLY.

⚠️ **The exact failure seam is UNVERIFIED, and this note has already named it wrongly twice.** A
resumed run's terminal is composed under the `agent.run.approval.resume` workflow, whose step returns
a bare `str` and is never validated as `RunWorkflowResult` — so the old pod may not fail loudly at
all; it may recover the run and **succeed silently**. What IS established: the durable record is
already correct, nothing is mis-stated, and there is no retry storm. **How many runs this can affect is
worked out further down this page, and there is no per-rollout cap** — do not size
the risk from this paragraph.

**Why that matters here specifically:** `values-production-ha.yaml` runs `ne-worker` at two replicas,
and `charts/ne-worker/templates/deployment.yaml` declares no `strategy:` — so Kubernetes defaults to
`RollingUpdate` with `maxSurge: 25%`, which rounds **up** to 1. **Old and new worker pods overlap on
every upgrade, even at `replicas: 1`.** There is no single-worker window to rely on.

**The failure is bounded, but do not plan against a specific symptom.** The run's durable record is
already correct and nothing is mis-stated; there is no retry storm; and it clears when the old pod
drains. What the old pod actually DOES is unverified — it may reject the value loudly, or it may
recover the run and succeed silently (see the note above). Plan for **every in-flight resumed run
handled by an old pod behaving unpredictably**, not for a named exception — and note there is no
one-per-rollout limit: the `agent-runs` queue runs at `worker_concurrency = turn_concurrency`, which
defaults to **4**, so several resumptions can be in flight across the overlap at once. ⚠️ **That 4 is a
CONCURRENCY bound, not the number of affected runs** — an adversarial pass was right that the earlier
pointer implied a count. ⚠️ **AND IT IS PER POD, NOT PER CLUSTER** — a
Codex pass was right that the earlier wording said "an old pod" and so understated the shipped HA
overlay by its replica count. DBOS validates `worker_concurrency` against a SEPARATE cluster-wide
`concurrency` (`dbos/_queue.py:118-125`), and `main.py:501` sets only `worker_concurrency`, so there
is no cluster-wide cap at all: with `values-production-ha.yaml`'s `replicas: 2`, BOTH old pods run 4
each — **8 concurrent, and `old_replicas x turn_concurrency` in general**. The affected total is
roughly `old_replicas x turn_concurrency x throughput x drain duration` and is bounded by none of
them. Plan for "an unbounded number, `old_replicas x turn_concurrency` at a time, until the old pods
drain" — and read `turn_concurrency` from YOUR deployment (`NE_WORKER_TURN_CONCURRENCY`), not from the
default. ⚠️ **This closing line said `old_replicas x 4` one revision ago**, one clause after the
sentence that had just generalized away from 4 — a corrected count left standing in the only
imperative sentence in the paragraph, which is the one an operator acts on. Second time this file has
done exactly that.

**If you want that window to be zero,** the worker Deployment must use
`strategy: {type: Recreate}` — the old pod terminates before the new one starts, so no overlap exists
at any replica count. The worker is a background processor with durable recovery, so the brief gap
costs throughput, not correctness.

⚠️ **This is currently a CHART PATCH, not a values override.**
`charts/ne-worker/templates/deployment.yaml` renders `replicas` and goes straight to `template:` — it
has no `strategy:` block and reads no `.Values.strategy`, and `strategy` appears in neither
`values.yaml` nor `values.schema.json`. **Adding `ne-worker: {strategy: {type: Recreate}}` to an
overlay renders a byte-identical Deployment and silently changes nothing** — you would believe you had
removed the overlap and still have it. Patch the template, or accept the rollout window knowingly.
This is a deployment choice, deliberately not made for you — but it should not be a choice you think
you have made and have not.

## Promotion runbook

```
# 1. Build the first-party image (egress-permitted staging window).
bash deploy/airgap/scripts/build-ne-app.sh

# 2. Push it to the CUSTOMER's registry and capture the C11 digest pin.
ne release push --image ne-app:0.1.0 --registry <customer-registry>/neutron \
  --out /tmp/digest.json

# 3. Promote a per-customer config bundle (same bundle machinery as the pilot tier).
ne pilot promote --customer <name> --hostname <hostname> --out /tmp/bundle \
  --runtime-config <path> --govern-config <path> --secrets-config <path>

# 4. Render the bundle into the config_hash/hostname/bundle-files overlay.
ne k8s render --bundle /tmp/bundle --out /tmp/overlay.json   # (optional — `ne k8s up` does this internally)

# 5. Install: resolves bundle secrets -> applies the installer Secret (STDIN
#    only) -> ensures the namespace -> `helm upgrade --install`.
ne k8s up --bundle /tmp/bundle --namespace neutron --release ne \
  -f /tmp/digest.json -f <any-extra-values.yaml> --wait --timeout 15m

# 6. Smoke.
kubectl -n neutron get pods
kubectl -n neutron port-forward svc/ne-api 8000:8000 &
curl -fsS http://127.0.0.1:8000/healthz
```

## C11 — first-party image pin: push-digest vs. build-in-seal

The pilot Compose tier pins `ne-app` by a **local build-in-seal**: build
during the egress-permitted staging window, capture the image id via
`--iidfile`, `pull_policy: never` so the sealed stack never tries to pull it
(`deploy/compose/pilot/README.md`). That mechanism does not exist on
multi-node K8s (nodes pull from a registry, they don't share a local docker
daemon), so this chart introduces **`ne release push`**: it tags+pushes the
same locally-built image to the customer's registry and captures the
**registry digest** (verified live: `docker image inspect --format '{{json
.RepoDigests}}'`, matched by repository — NOT `index 0`, which can return an
unrelated pre-existing digest from an image the local docker daemon also
tagged/pulled under a different name; this was caught during the builder's
local kind pre-flight). The chart's `values.schema.json` + a `_helpers.tpl
fail` both refuse to render with `global.image.digest` empty — the same
fail-closed posture as `ne up`'s `_check_app_image` preflight. Third-party
images (postgres/keycloak/opa) keep the exact pilot `tag@sha256` pins,
byte-verified against compose by the guardian test suite
(`tools/ne-cli/tests/test_helm_chart.py`). The push-digest path is
**guardian-restricted to `ne-app:X.Y.Z` only** — no third-party image may use
it (C11's exception must never widen).

## C12 — secret hygiene: installer-mode only

Helm **never** renders a `Secret` object in this chart — `helm get
values`/release history stays clean. `ne k8s up` resolves
`secrets/required.json` through the bundle's SecretStore (the exact same code
path `ne up` uses for the pilot tier, `ne.cli.pilotcfg.resolve_required_secrets`)
and applies a single namespaced `Secret/ne-platform-secrets` via `kubectl
apply -f -` with the resolved values on **STDIN only** — never argv, never
disk. If secret resolution fails, `ne k8s up` aborts before touching the
cluster at all (no namespace, no secret, no helm call). DSN-shaped env values
(the five `*_DATABASE_URL` vars) are composed **inside the container** via
`$(NE_PILOT_POSTGRES_PASSWORD)` dependent-env interpolation, so a resolved
password never appears pre-assembled in any manifest. `external-secrets`/`csi`
modes are documented in the design but not templated until 071b/072.

PRE-009C protected transport has a deliberately narrower explicit fresh-install
boundary selected with `--admit-protected-transport`. Before any Namespace,
Secret, or Helm mutation, `ne k8s up` reads
exact `platform_database_url` (`neutron`) and `dbos_database_url` (`ne_dbos`)
fields from SecretStore path `k8s/protected-transport-bootstrap` and proves both
external PostgreSQL inventories empty. The bundled PostgreSQL remains present
for Keycloak and OpenFGA; only Neutron and DBOS use the external bindings. A
failed first install may retry only with the same protected release/Secret
markers and another empty-inventory proof. A healthy admitted release upgrades
normally while preserving its live protected bindings. The default command
remains pre-cutover and does not read this bootstrap secret.

## OPA governance policy (PDP) — NE-PILOT-OPA-BUNDLE

`charts/opa/files/ne_system.rego` (`package ne.dev`) is a byte-copy of the
pilot Compose tier's placeholder (guardian:
`tools/ne-cli/tests/test_helm_chart.py::test_bundled_files_byte_match_pilot`)
and is the ONLY policy a **bare** `helm template`/`helm install` ever
renders — it makes no real authorization decisions. That committed
ConfigMap and Deployment mount are never edited by this unit.

`ne k8s render`/`ne k8s up` stamp the REAL first-party governance bundle
(`packages/ne-govern/policies`) into the values overlay as
`opa.governBundle.files` (`ne.cli.opa_bundle.govern_bundle_files` — the same
staging module `ne up`/`ne demo up` use for the Compose tier). When that
overlay is present, `charts/opa/templates/govern-policies-configmap.yaml`
renders an additional `ConfigMap/opa-govern-policies` and the opa
`Deployment` gains one read-only **`subPath`** volume mount per policy file
at `/policies/<relpath>` (e.g. `/policies/ne/govern/decision.rego`) — never
a directory mount, for the same kubelet `..data` symlink-farm reason
documented above (Live pre-flight section): a plain ConfigMap directory
mount makes OPA's directory loader walk three paths to the same file and
refuse to start. `opa.governBundle.files` defaults to `{}` in
`values.yaml`, so a bare `helm template`/`helm install` (no CLI overlay)
renders **nothing** extra — byte-identical to today. With the overlay
applied, a live `POST /v1/data/ne/govern/decision` is DEFINED instead of
failing closed against the `ne.dev` placeholder (see
`packages/ne-govern/src/ne/govern/opa.py`'s fail-closed `OpaClient`).

## NE_CONFIG_FILE deviation (design decision, not the builder's to relitigate)

The design sketch imagined per-module `NE_*_CONFIG_FILE` env vars pointing the
settings loaders at the promoted bundle's TOMLs inside the pod. That
mechanism **does not exist**: only a single global `NE_CONFIG_FILE` exists
(`packages/ne-contracts/src/ne/contracts/config.py:60-126`), and the
per-module settings classes (`RuntimeSettings`, `GovernSettings`, ...) are
`extra='forbid'`, so one shared file cannot serve every loader. The chart
therefore mirrors the pilot Compose tier **exactly**: container config is
**env-only** (the same `NE_*` keys the compose `ne-api` service sets — see the
Deployment template). The promoted bundle's three TOMLs still ride along, in
the `ne-platform-config` ConfigMap, mounted **read-only** at
`/etc/neutron/config` — but purely for **identity and rollout**: the
`neutron.dev/config-hash` pod-template annotation (sourced from the SAME
`global.configHash` that ConfigMap carries) forces a rollout whenever the
promoted bundle changes, even though the container never reads the mounted
files. **Tracked follow-up**: if/when a per-module `*_CONFIG_FILE` env is
added to the settings loaders (a chokepoint-adjacent change, out of NE-071a's
packaging-only scope), wire it here and drop the env-only path. The
runtime-hash == pod-annotation **live** equality assertion (i.e., that a
running ne-api pod's actual computed config hash matches the annotation) is
deferred to NE-071c/072 smoke depth — 071a proves the annotation is stamped
correctly, not that a live pod's internal state agrees with it (no
NE_CONFIG_FILE means there is nothing in-pod to compare against yet).

The Agent registry is different: PRE-008 keeps the repository authority at
`deploy/agents/registry.toml`, guards an exact byte copy under chart
`files/agents/registry.toml`, and renders it through `ne-agent-registry` binaryData
so ConfigMap mounting preserves bytes. Both API and worker mount that one object
read-only at `/etc/neutron/agents/registry.toml`; Zarf inherits it by vendoring
the chart and carries no second registry source.

PRE-011F treats the Skill artifact the same way without transferring authorship:
the repository authority remains the unchanged
`deploy/skills/meridian-hardship-draft/SKILL.md`, while exact byte copies under
the umbrella and both first-party subcharts feed one `ne-skill-bundle` binary
ConfigMap. API and worker mount it read-only at `/etc/neutron/skills` and share
`NE_SKILLS_BUNDLE_DIR` plus the chart-computed raw `SKILL.md` SHA; only the
worker reconciles it. An API init container and the production factory's
API-owned verifier are read-only: they re-parse the mounted pack and verify its
raw rollout hash and exact Agent pin against the shared hash-checked registry row
before API platform/catalog construction. Zarf carries these bytes solely by
vendoring the chart.

## No wait-for initContainer

The design sketch mentioned an optional `wait-for` initContainer on `ne-api`
for postgres readiness (cheap CrashLoop-noise avoidance). NE-071a
**deliberately drops it**: it would add a new pinned third-party image for a
cosmetic improvement, and `ne-api`'s own `startupProbe`
(`periodSeconds: 5, failureThreshold: 24`, the same ~120s window the compose
`start_period: 60s` + retries covers) plus Kubernetes' restart policy already
handle first-boot ordering correctly — a few CrashLoopBackOff cycles while
Keycloak/Postgres come up, then a clean start. Revisit if the noise becomes
an operational nuisance in 071b/c.

## K8s 1.31 floor vs. the kind smoke's node image

The chart's `kubeVersion: ">=1.31.0-0"` floor matches the SPEC row. Current
kind (v0.32.0) defaults to Kubernetes 1.36.1 node images and no longer
publishes v1.31.x — **the SPEC/design floor is past upstream kind's node-image
support window**. Resolution: the floor stays `>=1.31.0-0` in `Chart.yaml`
(that's the platform's stated compatibility promise), the schema-floor is
proven directly with `kubeconform -kubernetes-version 1.31.0` against the
rendered manifests (no live cluster needed for that proof), and the live kind
smoke runs on a currently-SUPPORTED node image
(`kindest/node:v1.34.8@sha256:02722c2dedddcfc00febf5d27fbeb9b7b2c14294c82109ff4a85d89ac9ba3256`,
from the kind v0.32.0 release notes) rather than an EOL 1.31.x image.

## Live pre-flight (builder, local — see PR body for the authoritative CI result)

The builder ran the full installer path locally against a real kind cluster
(darwin/arm64, scratchpad-pinned `helm`/`kind`/`kubectl`) with a local
`registry:3` mirror:

- `ne release push` — LIVE, against the local registry. Caught and fixed a
  real bug: `docker image inspect --format '{{index .RepoDigests 0}}'` can
  return an **unrelated** digest when the local image carries multiple
  `RepoDigests` entries (e.g. from the image it was originally tagged from);
  fixed to select the entry matching the just-pushed repository.
- `ne k8s up` (installer mode) — LIVE: namespace ensured, the
  `ne-platform-secrets` Secret applied via `kubectl apply -f -` on STDIN,
  `helm upgrade --install` ran to completion. **postgres**, **keycloak**, and
  **opa** (real, pilot-pinned images) all reached `Running`/`Ready`, verified
  live: `psql -U ne -d neutron -c '\l'` shows both the `neutron` and
  `keycloak` databases (initdb ConfigMap worked); Keycloak's `/health/ready`
  on :9000 returned `200 OK` (DB migration completed); OPA's `/health`
  returned `200` continuously. Also caught and fixed a real bug in the OPA
  chart: mounting the policies `ConfigMap` as a plain directory produces
  kubelet's `..data`/`..<timestamp>` symlink farm, which OPA's directory
  loader walked as three paths to the same file and refused to start
  (`multiple default rules ... found`); fixed by mounting the single rego
  key via `subPath` instead of a directory mount.
- **`ne-api`: LIVE-PARKED.** The builder's local docker CLI has no `buildx`
  component (`ERROR: BuildKit is enabled but the buildx component is missing`),
  so `deploy/airgap/scripts/build-ne-app.sh` cannot build the real image
  locally; the local pre-flight substituted a stub (`hello-world` retagged as
  `ne-app:0.1.0`) purely to exercise the push/render/apply plumbing — it
  correctly reached `Running` for the Deployment/Service/probe wiring up to
  the point where the stub process exits (expected; it is not the real app,
  so `/healthz` is never served and the pod CrashLoopBackOffs). This is an
  environment limitation, not a chart defect: `ne release push`'s digest
  capture, the Deployment's image/env/probe/hardening wiring, and the
  Ingress/Service topology were all otherwise exercised against the real
  postgres/keycloak/opa services. **`.github/workflows/helm-smoke.yml`'s
  `kind-smoke` job is the authoritative live proof** — its runner builds the
  REAL `ne-app` image (standard GitHub Actions Ubuntu runners ship
  `docker buildx`) and asserts the pushed digest appears in the running pod's
  `imageID` plus a real `/healthz` 200.

## ne-worker (NE-071b)

The durable worker (NE-103c) rides the **core** profile as its own
`charts/ne-worker` subchart — a Deployment running the **SAME first-party
ne-app image** as `ne-api` (`neutron.firstPartyImageRef`, the C11 pin) with the
"one image, diff command" seam: `command: ["python","-m","ne.worker"]`
overrides the image's default `["python","-m","ne.api"]` CMD (the ne-app image
has no ENTRYPOINT). It exists because `ne-api` ships
`NE_API_TURN_EXECUTION=enqueue`, so without a worker consuming the DBOS queue,
enqueued turns would park with no consumer.

- **Env parity.** The worker's env is a byte-copy of `ne-api`'s (the template is
  deliberately duplicated, NOT refactored into a shared helper — ne-api's
  template stays untouched for byte-parity) plus one tail entry,
  `NE_WORKER_DBOS_DATABASE_URL`, which **equals** `ne-api`'s
  `NE_API_DBOS_SYSTEM_DATABASE_URL` (both point at the `ne_dbos` system DB — the
  NE-103c alignment). The guardian
  (`tools/ne-cli/tests/test_helm_chart.py`) asserts the rendered shared-env
  entries are byte-identical between the two Deployments, so config_hash parity
  cannot drift; it also asserts the worker's key-set matches the compose
  `ne-worker` service.
- **Probes.** The worker has NO HTTP server (`run_admin_server=False`), so
  liveness/startup are **exec** probes running the NE-103c heartbeat check
  (`python -m ne.worker --health`, reading the `/tmp/ne-worker.health` marker on
  the emptyDir) — NOT an httpGet. The startupProbe gives a generous 300s boot
  window (covering the litellm-import boot) and gates Ready/`helm --wait` on a
  real successful boot; there is no readinessProbe (the worker takes no
  traffic).
- **No Service/Ingress.** The worker exposes no port.
- **Replicas.** Default `1` (DBOS `worker_concurrency` handles throughput).
  Multi-replica is DBOS-safe (share-by-storage; in-flight turns re-run from the
  top on pod loss) — the HA overlay bumps it to `2` with a PDB/anti-affinity.

## Brain services — qdrant + TEI (NE-071c, default deploy)

The **brain** profile (`profiles.brain`, **default-TRUE**) rides the DEFAULT
render: `qdrant` (vector store) and two Text-Embeddings-Inference StatefulSets
`tei-embed` (Qwen3-Embedding-0.6B) + `tei-rerank` (bge-reranker-v2-m3), each a
byte-parity reproduction of the pilot compose brain-profile services. They exist
because `ne-api`'s retrieval path points at them in-cluster:
`NE_API_QDRANT_URL=http://qdrant:6333` and `NE_API_TEI_URL=http://tei-embed:80`
(both byte-identical to compose — a rendered-Service guardian proves they
resolve).

- **Why default-ON, not a separate `core` condition?** The default render must
  carry the retrieval backends (that IS "core/default deploy"). `profiles.brain`
  default-TRUE produces an identical default render while giving sealed/air-gap
  installs an **off-switch** (`--set profiles.brain=false`) — the guardian
  `test_brain_toggle_off_removes_brain_objects` proves ne-api still renders
  without them.
- **CI coverage split (NE-071c-FU).** The live `kind-smoke` job installs the
  chart with `deploy/helm/tests/values-kind-nobrain.yaml` (`profiles.brain=false`)
  because the two TEI model servers OOM a single GitHub-hosted kind node
  (tei-rerank is OOMKilled — exit code 137 — at its 4Gi limit loading
  bge-reranker-v2-m3 while tei-embed is also loaded). The brain DATA-PLANE
  bring-up is live-validated instead in `.github/workflows/no-egress.yml` Phases
  B1/B2 (compose: single-node, sequential, pre-staged weights); `kind-smoke`
  live-proves the app tier + chart install. ne-api boots healthy with brain off
  — its retrieval/rerank clients are lazy per-query HTTP clients built from URLs,
  with no boot-time connect.
- **Init-file permissions (NE-071c-FU).** qdrant (uid 1000) writes
  `.qdrant-initialized` to its working dir `/qdrant` (image rootfs, not
  uid-1000-writable) → `Failed to create init file indicator ... Permission
  denied (os error 13)`. The StatefulSet now sets
  `QDRANT_INIT_FILE_PATH=/qdrant/init/.qdrant-initialized` onto an emptyDir
  (upstream qdrant-helm pattern); the image digest is unchanged (C11
  byte-parity — env/volume only).
- **The PORT-8080 non-root adaptation.** The TEI image defaults to listening on
  port **80**, which a non-root (uid 1000) process cannot bind. The pods set
  `PORT=8080` and listen there; each Service maps **port 80 → targetPort 8080**,
  so `NE_API_TEI_URL=http://tei-embed:80` stays **byte-identical to compose**
  while the pod runs unprivileged. NetworkPolicy allows target the **pod** port
  8080 (post-Service-DNAT), not 80.
- **Air-gap / sealed installs — weights are NOT vendored.** `HF_HUB_OFFLINE`
  defaults to `"0"` (mirrors the pilot `${NE_PILOT_HF_OFFLINE:-0}` posture: first
  boot downloads ~3.4GB of model weights from HuggingFace). A fully-disconnected
  install (real air-gap, the NE-086 weekly sealed zarf deploy) **cannot reach
  Ready** unless you either (a) pre-stage the weights into the `cache` PVCs and
  set `tei.hfOffline="1"`, or (b) set `profiles.brain=false`. The TEI weights are
  deliberately not vendored into the zarf tarball (they are large model blobs,
  not container images).
- **`networkPolicy.enabled=true` triggers the SAME offline requirement — even on
  an otherwise-online cluster.** On an *enforcing* CNI, the default-deny egress
  baseline (see "NetworkPolicy (C2)" below) reopens only DNS + internal services
  and opens **no internet/443 lane** (the C2 posture), so the first-boot
  HuggingFace download is severed exactly as in an air-gap. A hardened brain
  install therefore has the same two remedies: pre-stage the `cache` PVCs +
  `tei.hfOffline="1"`, or `profiles.brain=false`. This is a documented
  operational prerequisite, **not** a render-time failure — the rendered
  NetworkPolicy objects are inert on a non-enforcing CNI (where TEI downloads
  fine), so the chart does not hard-fail the render.
- **Storage.** `qdrant.storage.size` (default 16Gi, `qdrant-data` PVC + an
  ephemeral `/qdrant/snapshots` emptyDir); `tei.embed.storage.size` /
  `tei.rerank.storage.size` (default 8Gi each, the `cache` PVC at `/data`).
- **HA.** qdrant ships as a single-replica reference StatefulSet; Qdrant's own
  cluster mode (replication factor ≥2 + snapshots) is the production HA answer —
  see "HA production profile" below.

## Observability — obs profile (default-OFF)

`profiles.obs` (**default-FALSE**) renders **only** an `otel-collector`
Deployment (+ Service on OTLP 4317/4318 + a ConfigMap whose `config.yaml` is a
**byte-copy** of `deploy/compose/pilot/otel/otel-collector-config.yaml`, mounted
via `subPath`). The collector runs hardened as uid **10001** with
`readOnlyRootFilesystem: true`.

- **No probes — documented distroless exemption.** The
  `opentelemetry-collector-contrib` image is distroless (no shell/wget/curl to
  probe with), exactly like the compose service; Kubernetes treats the pod Ready
  once the container is running. The guardian asserts the absence of
  startup/readiness/liveness probes.
- **Image pin is frozen at 0.155.0 (C11).** Upstream 0.156.0 exists, but C11
  byte-parity to the pilot compose pin is binding — **do NOT bump** the chart in
  isolation (a compose-tier bump is the follow-up path).
- **Langfuse is DEV-TIER-ONLY — never templated here.** Langfuse uses unpinned
  image tags and needs clickhouse + S3 + redis; the pilot compose explicitly
  excludes it as a C2 invariant. It therefore **cannot** satisfy the C11
  byte-match mandate and is **not** part of the obs subchart. Observability in
  prod = the OTLP collector only; wire its `debug` exporter to a real backend
  as needed.

## Serving — serve profile (default-OFF, GPU-gated, render-only in CI)

`profiles.serve.enabled` (**default-FALSE**) renders a single `vllm` Deployment
(+ Service on 8000) — byte-parity with `deploy/compose/serve/compose.yaml`. It is
**render-only in CI** (never scheduled live on kind — no GPU).

- **`vllm.mode` = `gpu` | `cpu`, fail-closed.** The mode selects the image
  (`vllm/vllm-openai` vs `vllm/vllm-openai-cpu`), the model dir + served name,
  and (cpu) `VLLM_CPU_KVCACHE_SPACE`. Any other value **fails the render** with a
  `{{ fail }}` naming the bad value. The Deployment/Service are named `vllm` in
  both modes, so the gateway menu `api_base http://vllm:8000/v1` resolves either
  way.
- **GPU scheduling gate.** In gpu mode the container carries a
  `limits."nvidia.com/gpu": <gpuCount>` (default 1) — the standard device-plugin
  request — plus pass-through `vllm.nodeSelector`/`vllm.tolerations` (render
  nothing at defaults). cpu mode carries no GPU limit.
- **`vllm.weights.existingClaim` contract.** Empty ⇒ an `emptyDir` at `/models`
  so `helm template`/lint/CI render stays green — but a pod with an empty
  `/models` **cannot serve**. A REAL GPU deploy MUST point `existingClaim` at a
  **pre-staged RWO PVC** of model weights (`HF_HUB_OFFLINE=1` means the pod never
  downloads).
- **`/dev/shm` + `HOME=/tmp`.** vLLM needs a large shared-memory segment
  (`vllm.shmSize`, default 16Gi, a `Memory`-medium emptyDir at `/dev/shm`); the
  non-root `HOME=/tmp` adaptation keeps its caches on the writable emptyDir.
- **Untested-live.** vllm runs non-root uid 1000; a live GPU deploy could surface
  permission issues that the render-only CI cannot catch — treat the first live
  GPU bring-up as a validation step.

## NetworkPolicy (C2)

Default-OFF (`networkPolicy.enabled: false`). When enabled, the chart renders a
**default-deny** baseline (empty podSelector, both policyTypes, no rules) plus
an explicit least-privilege **allow** set:

- **allow-dns** — every pod may resolve DNS (53/UDP+TCP).
- **allow-ne-api** — ingress on `8000` ONLY from the ingress-controller
  namespace (the SPEC "ingress→ne-api:8000 only" lane); egress to
  postgres:5432, keycloak:8080, opa:8181, **qdrant:6333**, **tei-embed:8080**,
  **tei-rerank:8080** (the NE-071c brain backends — the egress targets the POD
  port 8080, NOT the Service port 80, since a NetworkPolicy matches the
  destination pod port post-DNAT), **otel-collector:4317+4318** (obs), and
  vllm:8000 (serve — a forward-compatible no-op until that profile lands the pod).
- **allow-ne-worker** — NO ingress (nothing may dial the worker); egress to the
  **same** governed set as ne-api. The SPEC row's "ne-worker↔postgres" is the
  MINIMUM, not an exclusive list (design §1.7 "ne-worker→same set"): the durable
  executor runs governed turns, so severing OPA (the PDP chokepoint, C6) or the
  model backends would break turn execution.
- **allow-keycloak** — ingress `8080` from the ingress controller **and** from
  ne-api/ne-worker (in-network JWKS/token fetch); egress to postgres:5432
  (Keycloak's own DB).
- **allow-postgres** — ingress `5432` from ne-api/ne-worker/keycloak/ne-backup.
- **allow-opa** — ingress `8181` from ne-api/ne-worker.
- **allow-ne-backup** — egress to postgres:5432 (matches only the backup pod).
- **allow-qdrant** / **allow-tei-embed** / **allow-tei-rerank** (NE-071c, gated
  on `profiles.brain`) — ingress on 6333 / **8080** / **8080** from
  ne-api/ne-worker (the pod port 8080, per the non-root adaptation).
- **allow-otel-collector** (NE-071c, gated on `profiles.obs`) — ingress on OTLP
  4317+4318 from ne-api/ne-worker.
- **allow-vllm** (NE-071c, gated on `profiles.serve.enabled`) — ingress on 8000
  from ne-api/ne-worker.

**The keycloak:8080 ingress-controller allow is deliberate and required:** the
browser hits the Keycloak vhost for the OIDC login redirect, so a customer who
enables `networkPolicy` without it would break login. The
`ingressControllerNamespaceLabels` value defaults to the ingress-nginx
namespace's `kubernetes.io/metadata.name` label — override it for a different
controller.

**Requires a NetworkPolicy-ENFORCING CNI** (Calico/Cilium/etc.): the objects
render on any cluster but are inert without an enforcing CNI, and this unit
ships render-level (shape) guardians only — no live enforcement test. **No
frontier/443 egress lane** is opened here (the pilot menu is in-VPC vllm only);
perimeter/FQDN egress control is **NE-072**.

> **Preflight the brain weights before enabling enforcement.** Because no
> internet/443 egress lane is opened, an *enforcing* CNI severs the first-boot
> HuggingFace weight download the default-ON TEI pods perform (`tei.hfOffline`
> defaults to `"0"`). Enabling `networkPolicy` on a fresh brain install would
> leave `tei-embed`/`tei-rerank` hanging un-Ready. **Before** flipping
> `networkPolicy.enabled=true`: pre-stage the embed/rerank `cache` PVCs and set
> `tei.hfOffline="1"`, or set `profiles.brain=false` (see "Brain services"
> above). This is deliberately a documented prerequisite rather than a
> render-time `fail` — the policies are inert on a non-enforcing CNI, where TEI
> downloads normally, so a hard fail would false-block that valid case.

### Flowise add-on egress clamp (NE-088 H2)

`templates/networkpolicy-flowise.yaml` renders two additive NetworkPolicies,
both gated behind **BOTH** `networkPolicy.enabled` AND `profiles.flowise`
(default `false`) — so they render **nothing** by default and every guardian
above stays byte-green. **`flowise-egress-clamp`** default-denies the
`app=flowise` workload (no ingress; the UI is reached via `kubectl port-forward`,
which is API-server-proxied and unaffected) and permits egress **only** to
`ne-api:8000` (DNS rides `allow-dns`). Because a K8s connection needs **both**
ends to allow it — and the baseline `allow-ne-api` admits ingress only from the
ingress-controller namespace — the second policy **`allow-ne-api-from-flowise`**
(`Ingress`-only, so `ne-api`'s egress is untouched) opens the matching `ne-api`
ingress from `app=flowise` on 8000, completing the one governed lane. Together
these are the tightest K8s-**enforceable** posture for the CVSS-10/KEV-history
Flowise add-on.

The umbrella chart ships **no** Flowise workload (Flowise is a compose-only
optional add-on — `deploy/compose/flowise/`); this policy therefore **pre-clamps**
any customer-deployed pod labelled `app=flowise`. Compose itself cannot enforce
per-container egress on a NAT bridge — this NetworkPolicy is the enforceable
analogue (see `deploy/compose/flowise/README.md` §(h)).

## OpenShift

`global.openshift: true` makes `_helpers.tpl` omit
`runAsUser`/`runAsGroup`/`fsGroup` from **every** pod securityContext (the
restricted SCC's `MustRunAsRange` assigns UIDs from the namespace's allocated
range), keeping `runAsNonRoot: true`. NE-071b render-tests this seam
(`test_openshift_render_omits_fixed_uids` — no fixed UID on any of the 5
workloads); a **live CRC/OpenShift smoke is still out of scope**.

`route.enabled: true` renders two `route.openshift.io/v1` **Routes** (console →
ne-api:8000, auth → keycloak:8080, edge TLS with the cluster router's default
cert) and **suppresses the Ingress** (mutually-exclusive front door). Supply a
customer cert via the Route spec / `oc create route edge --cert/--key`. The
Route CRD schema is absent from vanilla kubeconform, so the flagged-render
kubeconform guardian deliberately excludes `route.enabled`.

**Honest note:** the bundled `postgres:18.6-alpine` (and keycloak) images are
NOT arbitrary-UID-safe under the `restricted-v2` SCC. Options: bind the
`anyuid` SCC to the workload's ServiceAccount, use an **external** managed
Postgres, or adopt the CloudNativePG seam (also the stateful-HA answer). This is
documented, not solved, in this unit.

## Backups & restore

Default-OFF (`backup.enabled: false`). When enabled, the chart renders a
`batch/v1` **CronJob** (`ne-backup`, default `schedule: "0 2 * * *"`,
`concurrencyPolicy: Forbid`) plus a `ReadWriteOnce` **PVC**
(`ne-backup-storage`, default 8Gi). The job runs `pg_dumpall --clean
--if-exists` — a single cluster-wide dump that covers **neutron + keycloak +
ne_dbos** in one run — writing `/backups/ne-<UTC-ts>.sql`, then prunes to the
`backup.keep` most-recent snapshots (busybox-safe `ls -1t | tail -n +N | while
read` loop, no `xargs -r`). It reuses the chart's **already-pinned postgres
image** (matching client/server versions; C11 — no new registry image) and runs
hardened as uid `70` (the alpine postgres user) with the shared cap-drop/RO-
rootfs/seccomp context; `PGPASSWORD` comes from the installer Secret via
`secretKeyRef` (C12 — no DSN literal).

**Restore runbook.** Stop writers, restore, restart:

```
kubectl -n neutron scale deploy/ne-api deploy/ne-worker --replicas=0
kubectl -n neutron exec -i statefulset/postgres -- \
  psql -U ne -d postgres < ne-<ts>.sql          # pg_dumpall output is a full-cluster script
kubectl -n neutron scale deploy/ne-api --replicas=<n> deploy/ne-worker --replicas=<n>
```

RPO/RTO guidance is in design §1.8 and `deploy/backup/README.md`.

### Velero (NE-071d)

This chart ships TWO independent backup halves, closing the literal SPEC
NE-071 wording ("Velero + DB-native backups with RPO/RTO per profile"):

1. **DB-native / logical** — the `ne-backup` CronJob above (`backup.enabled`).
2. **Velero / volume + cluster-state** — a `velero.io/v1` **Schedule**
   (`templates/velero-schedule.yaml`, default-OFF via
   `backup.velero.enabled`) that references the CLUSTER-level Velero already
   provisioned by `deploy/terraform-azure/velero.tf`. This chart NEVER
   installs or re-provisions Velero itself — it only emits a Schedule custom
   resource into Velero's own install namespace.

**Prerequisites.** Enabling `backup.velero.enabled` requires the
terraform-azure `install_velero=true` variable to have already been applied
(the Velero CRDs + server, namespace `velero`, BackupStorageLocation
`default` — chart 12.1.0 / appVersion 1.18.1). `helm template`/`helm lint`
render cleanly either way; a real `helm upgrade --install` fails on the
unknown `velero.io/v1` apiVersion if the CRDs aren't present on the cluster.

**Values surface** (nested under `backup.velero` — see `values.yaml` for the
full comment):

- `enabled` (bool, default `false`)
- `namespace` (default `velero`) — Velero only reconciles Schedules that live
  in its OWN install namespace, so this is deliberately NOT
  `.Release.Namespace`.
- `storageLocation` (default `default`) — the BackupStorageLocation name.
- `profile` (default `production`) — selects an entry from `profiles` below.
  **Fails closed**: an unknown profile name aborts the render.
- `profiles.pilot` / `profiles.production` — each a `{schedule, ttl}` pair.

**Per-profile RPO/RTO:**

| Profile | Cron | TTL | RPO | RTO |
| --- | --- | --- | --- | --- |
| `pilot` | `0 3 * * *` (daily, 03:00 UTC) | `168h0m0s` (7 restore points) | ≤ 24h | ≤ 30 min for ≤ 10 GiB corpora (matches `deploy/backup/README.md`'s drilled bound) |
| `production` | `0 */6 * * *` (every 6h) | `336h0m0s` (14 days of restore points) | ≤ 6h | ≤ 60 min for ≤ 100 GiB corpora (namespace-scoped `velero restore` including PV data; excludes cluster-rebuild/terraform time) |

The `pilot` cron is deliberately staggered to 03:00 UTC — ONE HOUR after the
`ne-backup` CronJob's default 02:00 run — so the newest logical dump is
already sitting on the `ne-backup-storage` PVC when the volume backup
executes.

**The postgres pre-hook.** The Schedule runs a `postgres-checkpoint` hook
(`labelSelector: {app: postgres}`) that execs `psql -U ne -d neutron -c
CHECKPOINT` on the postgres pod immediately before the volume snapshot —
flushing WAL/dirty pages shortens restore-time crash recovery (bounds RTO).
It uses the same in-container auth path as the existing readinessProbe
(`pg_isready -U ne -d neutron`). `onError: Continue` makes the hook a no-op
when no postgres pod exists (e.g. `profiles.core=false`) — the Schedule is
therefore deliberately **NOT** core-gated, unlike the `ne-backup` CronJob:
namespace-level DR is content-agnostic.

**Restore sketch** (full runbook lives with the terraform/Velero install):

```
velero backup get
velero restore create --from-backup <backup-name> --include-namespaces <ns>
# restore the namespace, then let the StatefulSets re-attach their PVs.
# The pg_dumpall snapshots on the restored ne-backup-storage PVC remain the
# logical fallback if a finer-grained (single-table) recovery is needed.
```

Snapshot mechanics (CSI vs fs-backup vs VolumeSnapshotLocation) follow the
cluster's Velero install (`deploy/terraform-azure/velero.tf`), not this
chart — the Schedule intentionally omits `snapshotVolumes` /
`useOwnerReferencesInBackup` / `paused` to stay provider-agnostic (C9).

## HA production profile (NE-089)

**Additive, default-OFF surface** — the base chart render (`helm template` with
no extra `-f`) is byte-identical to before this unit; every knob below defaults
to a no-op so `tools/ne-cli/tests/test_helm_chart.py`'s parity guardians stay
green unmodified. Enable it via `deploy/helm/examples/values-production-ha.yaml`
(an overlay — see that file's header for how to apply it on top of a real
promoted values file), or hand-roll equivalent values.

**Stateless `ne-api` (safe to scale freely — ADR-001).** `ne-api` keeps no
sticky in-pod state; every session/approval/audit/permission write lands in
Postgres. This unit adds three pass-through values:

- `ne-api.podDisruptionBudget.enabled` / `.minAvailable` — a vanilla
  `policy/v1` `PodDisruptionBudget` (`charts/ne-api/templates/pdb.yaml`,
  renders nothing when `enabled: false`).
- `ne-api.affinity` — passed through verbatim into the pod spec (`with` —
  renders nothing when `{}`). The example overlay sets a preferred
  pod-anti-affinity on `app: ne-api` / `kubernetes.io/hostname` so replicas
  prefer separate nodes.
- `ne-api.topologySpreadConstraints` — same pass-through pattern, for a
  zone-aware spread (`whenUnsatisfiable: ScheduleAnyway` — never blocks a
  rollout on an unbalanced cluster).

Bump `ne-api.replicas` alongside these (the example overlay uses `3`).

**Durable `ne-worker` (NE-071b — DBOS-safe at N>1).** The worker gets the SAME
three pass-through knobs (`ne-worker.podDisruptionBudget`, `.affinity`,
`.topologySpreadConstraints`) via its own `charts/ne-worker/templates/pdb.yaml`
and the affinity/spread pass-throughs in its Deployment. DBOS share-by-storage
makes multiple workers safe (in-flight turns re-run from the top on pod loss),
so the example overlay sets `replicas: 2` with a PDB (`minAvailable: 1`) and the
same anti-affinity/zone-spread shape as ne-api. These are ADDITIVE to NE-089's
ne-api surface — the ne-api blocks are untouched.

**OPA — also stateless.** Bump `opa.replicas` freely; no PDB/affinity surface
was needed (the existing Deployment already tolerates N replicas per the
NE-071a chart).

**STATEFUL services — values alone do NOT give you HA; read this before
bumping replicas on any of the below.**

- **postgres** — the bundled subchart is a single-replica REFERENCE
  StatefulSet (pilot/dev-cluster scale). Production HA requires either a
  CloudNativePG-class operator (streaming replication, automated failover) or
  a customer-managed HA Postgres service — NOT a `replicas:` bump on this
  chart's StatefulSet (that would just start N independent, non-replicated
  Postgres instances sharing nothing). This is a tracked NFR fast-follow; Velero + DB-native backups
  (separately NE-071 scope) are documented in "Backups & restore" above.
- **keycloak** — also single-replica by default. Keycloak DOES support
  clustering, but it requires a distributed cache configuration (Infinispan
  JGroups discovery, typically `KC_CACHE=ispn` + a Kubernetes-native JGroups
  stack config) BEFORE you raise `replicas` above 1 — simply bumping replicas
  without that config yields split-brain session/cache state, not HA. Not
  wired in this unit; a follow-up if/when multi-replica Keycloak is needed.
- **qdrant** — ships with the NE-071c brain profile (default deploy) as a
  single-replica reference StatefulSet (`qdrant.storage`/`qdrant.resources`
  values now exist). Production HA wants a replication factor ≥2 (Qdrant's own
  cluster mode) plus a snapshot schedule for disaster recovery — NOT a bare
  `replicas:` bump on this StatefulSet. The single-replica default is pilot/
  reference scale.

## Pending — NOT yet built

- **~~brain / obs / serve profiles~~ — DONE (NE-071c).** qdrant +
  tei-embed/tei-rerank ride the default deploy (`profiles.brain`), otel-collector
  is behind `profiles.obs`, and vllm is behind `profiles.serve.enabled`
  (GPU-gated, render-only in CI). See the "Brain services", "Observability", and
  "Serving" sections above. Langfuse is DEV-tier-only and deliberately never
  templated. (The `agents` profile is retired: the durable ne-worker rides
  `core`, per the NE-103c decision.)
- **Stateful-service HA (Postgres/Keycloak/Qdrant operators)** — the bundled
  single-replica StatefulSets are reference/pilot scale; production HA needs a
  CloudNativePG-class Postgres operator (or external managed DB) and a
  clustered-cache Keycloak config, NOT a `replicas:` bump. See the STATEFUL
  notes under "HA production profile" below.
- **External-secrets / CSI secret modes** — only installer-mode secrets ship;
  the `external-secrets`/`csi` modes are designed but not templated.
- **~~Velero backup templating~~ — DONE (NE-071d).** `backup.velero.enabled`
  templates a velero.io/v1 Schedule (per-profile RPO/RTO) into the
  terraform-provisioned Velero's namespace; see "Backups & restore".
- **NE-072 Terraform (Azure AKS reference + EKS smoke) + perimeter FQDN egress
  control** — separate unit. The NetworkPolicy set here opens no frontier/443
  egress lane.
- **Live enforcement/CRC smoke** — the NetworkPolicy and OpenShift Route/SCC
  surfaces are render-level (shape) guardians only; a NetworkPolicy-enforcing-
  CNI test and a live CRC/OpenShift smoke are out of this unit's scope.
- **docker-save air-gap export for `ne release push`** — documented as a
  071/072 runbook follow-up; only the online registry-push path ships now.

## Testing

- `tools/ne-cli/tests/test_helm_chart.py` — the parity guardian: chart
  layout/versions (the 9-subchart condition map), C11 third-party digest
  byte-match against `deploy/compose/pilot/compose.yaml`, bundled SQL/rego
  byte-match, C12 no-secret-literal scanners (chart sources AND rendered output),
  rendered hardening/env/probe parity, the C11 fail-closed schema checks (empty
  digest/configHash), and (when `kubeconform` is on `PATH`) the K8s 1.31.0
  schema-floor proof. NE-071c adds the brain/obs/serve guardians:
  `test_new_subchart_digests_byte_match_compose`,
  `test_obs_otel_config_byte_matches_pilot`,
  `test_default_render_includes_brain_and_excludes_optional`,
  `test_qdrant_parity`, `test_tei_parity`,
  `test_ne_api_brain_urls_resolve_in_cluster`, `test_obs_render_behind_flag`,
  `test_serve_render_behind_flag`, `test_serve_cpu_mode_and_fail_closed`,
  `test_brain_toggle_off_removes_brain_objects`, and
  `test_networkpolicy_covers_new_services`. NE-071c-FU adds
  `test_kind_nobrain_overlay_is_one_knob` and
  `test_kind_nobrain_overlay_removes_brain_objects`. NE-071d adds the Velero
  Schedule render guardians: `test_velero_schedule_render_behind_flag`,
  `test_velero_pilot_profile_expresses_stated_rpo`,
  `test_velero_unknown_profile_fails_closed`, and
  `test_velero_schedule_independent_of_core_profile`.
- `tools/ne-cli/tests/test_release_push.py` — `ne release push` unit tests
  (docker mocked): happy path, RepoDigests-index-0 pitfall regression,
  push-stdout fallback, fail-closed on no digest / no matching repo digest,
  third-party image refusal, registry trailing-slash normalization.
- `tools/ne-cli/tests/test_k8s_cli.py` — `ne k8s render`/`ne k8s up` (real
  bundle + real SecretStore, kubectl/helm mocked): overlay correctness,
  tamper rejection, Secret-via-STDIN-only, `-f` ordering, abort-before-mutation
  on secret-resolution failure.
- `.github/workflows/helm-smoke.yml` — `render` (helm lint + `helm template |
  kubeconform -strict`, the guardian pytest as a must-run-not-skip gate) and
  `kind-smoke` (the live proof: build → push → promote → `ne k8s up` →
  healthz + pushed-digest assertion). NE-071c-FU: the kind smoke now brings up
  **core + obs** — brain is scoped out via the `values-kind-nobrain.yaml`
  overlay (see "Brain services: CI coverage split"), so the app tier + chart
  install are live-proven while the brain data-plane is validated in the
  no-egress compose lane; serve (vllm) stays render-only.
- `tools/ne-cli/tests/test_zarf_package.py` — the zarf image-set guardian;
  NE-071c adds `test_zarf_new_core_images_byte_match_chart_and_compose`
  (qdrant/tei pins present in `zarf.yaml`; otel/vllm deliberately absent) and the
  6-image count update.
- `tools/ne-cli/tests/test_opa_bundle.py` — the shared `ne.cli.opa_bundle`
  staging module (no docker/helm needed): byte-parity against
  `packages/ne-govern/policies`, `*_test.rego` exclusion, generated compose
  overlay shape, ConfigMap-key charset validity, fail-closed on a non-checkout.
- `tools/ne-cli/tests/test_helm_opa_govern.py` (NE-PILOT-OPA-BUNDLE, skipif no
  `helm`/`kubeconform`) — the Helm-tier govern-bundle guardians: default render
  carries no `opa-govern-policies` ConfigMap/mounts; with the overlay applied,
  the rendered ConfigMap byte-matches the source policies, the opa Deployment
  gains one `subPath` mount per file, and `kubeconform -strict` passes.
- `tools/ne-cli/tests/test_opa_bundle_live.py` (LIVE-PARKED, skipif no docker
  daemon) — starts the real pinned OPA image with the staged bundle mounted
  and asserts `POST /v1/data/ne/govern/decision` is DEFINED, plus a negative
  control proving the same query is UNDEFINED against the shipped placeholder
  alone (the exact fail-closed gap this unit fixes).
