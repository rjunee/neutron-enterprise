# deploy/airgap/zarf — the full air-gap Zarf bundle (NE-086)

This directory is the **shippable air-gap delivery vehicle** for Neutron
Enterprise: a single self-contained [Zarf](https://zarf.dev) package that
installs the **entire** platform (the Helm umbrella chart at
`deploy/helm/neutron-enterprise` + **every** image it deploys) into a **fully
disconnected** Kubernetes cluster from one tarball — with **zero registry
egress at deploy time**.

## 1. What / why — the C2 delivery vehicle

SPEC §2 **C2** is the core sovereignty guarantee: the platform must install and
run with no external network. NE-073 (`deploy/airgap/`) proves that property in
CI with a sealed network namespace. **NE-086 is the productized form of the same
threat model** — instead of a CI workflow, it produces a *bundle*:

* **`zarf package create`** runs ONCE in the **egress-open staging window**
  (exactly like copying media onto an air-gapped site before disconnecting it).
  It vendors every image below plus the Helm chart into
  `zarf-package-neutron-enterprise-amd64-0.1.0.tar.zst`.
* **`zarf package deploy`** (and `zarf init`) then need **zero egress**. Zarf
  pushes the vendored images into its own in-cluster registry
  (`zarf-docker-registry`) and installs the chart with every image ref rewritten
  to that registry via the Zarf agent's `###ZARF_REGISTRY###` mutation. No
  external pull ever happens.

This mirrors the helm-smoke kind flow (`.github/workflows/helm-smoke.yml`) for
the *connected* install, and the NE-073 stage→seal→prove flow for the *offline*
guarantee — see `.github/workflows/zarf-package.yml`, which does both in one job.

## 2. C4 — Zarf is a customer-side / CI packaging TOOL, never a runtime dep

Zarf (`zarf-dev/zarf`, Defense Unicorns) is **Apache-2.0** (verified 2026-07-09
against the raw `LICENSE` on the repo's `main`). Per SPEC **C4** it is a
customer-side / CI packaging tool **exactly like `helm` and `terraform`**:

* it is **never shipped in the product runtime**, never baked into any product
  image, and **never added to `uv.lock`** or any `pyproject.toml`. NE-086 adds
  **zero** new Python dependencies.
* it is **pinned and sha256-verified** in the workflow.

**Version pin — flagged, not silent (memory: never silently downgrade a
critical stage).** Zarf is pre-1.0; the research brief mandates version pinning.
We pin **v0.80.0** (released 2026-06-25, ~2 weeks of soak). v0.81.0 shipped
2026-07-09 — the SAME day this unit was planned — and its notes list **breaking
changes** (per-host plain-http transport negotiation, legacy-signature removal),
so we deliberately stay one release behind. Recorded checksums (from each
release's `checksums.txt`, fetched 2026-07-09):

| Artifact | v0.80.0 (pinned) | v0.81.0 (recorded, for a later deliberate bump) |
|---|---|---|
| `zarf_v<ver>_Linux_amd64` | `647cd4243f7b00b45bc87336d9249ba955a9dfa68632a5e4c8c0a4b4d87ba24d` | `ade63751bd8c276621cb1b685403429f6ed70aa5748f224bae17adcbdb51463f` |
| `zarf-init-amd64-v<ver>.tar.zst` | `c9d3aacb5651692382d96af7a41268b10871ead0bb598cabfecb6df8c41820a4` | `e1701ed2b05cc67623d4e4644aa60a65b693f82bd1309b9f70dccb9c749c663d` |

## 3. Image set + C11 no-drift

The package vendors **exactly** the chart's byte-parity image set. The three
third-party pins in `zarf.yaml` are **byte-copies** of
`deploy/helm/neutron-enterprise/values.yaml` (== `deploy/compose/pilot/
compose.yaml`); the parity guardian
`tools/ne-cli/tests/test_zarf_package.py` forces `zarf.yaml == chart rendered
set` and fails CI on any drift. When the chart grows the brain/serve profiles
(qdrant/TEI/vLLM — NE-071 follow-ups), the guardian will fail until this bundle
grows with it.

| Image | Digest | License |
|---|---|---|
| `postgres:18.6-alpine` | `sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873` | PostgreSQL License |
| `quay.io/keycloak/keycloak:26.7.4` | `sha256:82a77884f3af238beab1e7afd63b5f530e1b5c0590bd7aa60b40a40463e29b2c` | Apache-2.0 |
| `openpolicyagent/opa:1.18.2-debug` | `sha256:7ee75476b179699061e61e277e1aafa7d13e126034fcdcba4beca8d233c3e865` | Apache-2.0 |
| `ne-app:0.1.0` (first-party) | create-time from `ne release push` (§4) | Apache-2.0 |

**Zarf preserves manifest digests** into its in-cluster registry — the agent's
mutation rewrites only the registry *host*, not the digest.

### §digests — the multi-arch nuance

For a multi-arch third-party image pinned by **index** digest, Zarf stores/pushes
the **amd64 platform manifest**, so a running pod's `imageID` digest can
legitimately differ from the index-digest pin. The live proof therefore:

* **hard-asserts** the imageID digest only for the single-manifest **first-party**
  `ne-app` image (owner dec ii local-digest pin), and
* **soft-logs** the third-party live imageID digests (prints, never fails on an
  index→platform difference).

The static `zarf.yaml == chart` parity guardian carries the C11 no-drift
guarantee regardless of that translation.

## 4. First-party image (owner dec ii): build-in-seal + local-digest pin

The `ne-app` image is **not** on a public registry, so C11's `tag@sha256` rule
cannot pin it from a literal. It enters the bundle via the SAME mechanism
already merged for the helm-smoke kind flow (no second pinning mechanism):

```
build-ne-app.sh  →  staging registry:3  →  ne release push  →  digest.json
                                                                    │
        zarf package create --set NE_APP_IMAGE=<repo>:0.1.0@<digest> ...
```

`ne release push` captures the registry digest at the moment of transfer and
writes `{global:{image:{repository,tag,digest}}}`. The workflow derives
`NE_APP_IMAGE`/`NE_APP_REPOSITORY`/`NE_APP_DIGEST` from it and passes them as
`zarf package create --set` package templates. The chart's C11 fail-closed
`global.image.digest` schema is satisfied with the **real** digest — never a
committed fake literal. This is the direct analogue of compose's
`pull_policy: never` + the Phase-A staged-id-match pin.

## 5. RUNBOOK

### Staging side (egress PERMITTED)

1. Download + **sha256-verify** the pinned zarf binary AND the matching
   `zarf-init-amd64-v0.80.0.tar.zst` (checksums in §2).
2. Build the first-party image: `bash deploy/airgap/scripts/build-ne-app.sh`.
3. Run the pinned staging registry and push:
   ```sh
   docker run -d -p 127.0.0.1:5001:5000 --name zarf-staging-registry <REGISTRY_IMAGE>
   uv run ne release push --image ne-app:0.1.0 --registry localhost:5001/neutron \
     --out digest.json
   ```
4. **Real engagement only:** merge the engagement's `ne k8s render` overlay
   (real `configHash` + the promoted bundle's per-module TOMLs) into
   `values/zarf-overlay.yaml`, and any sizing/HA overrides into
   `values/zarf-tuning.yaml`. (CI leaves the overlay at the fixture defaults and
   overwrites the tuning file with `deploy/helm/tests/values-kind.yaml`.)
5. Create the package (add `--plain-http` for the plain-HTTP staging registry;
   sign with `--signing-key <cosign-key>` for a release — CI builds unsigned
   because no key material lives in CI):
   ```sh
   NE_APP_REPOSITORY=$(jq -r .global.image.repository digest.json)
   NE_APP_DIGEST=$(jq -r .global.image.digest digest.json)
   zarf package create deploy/airgap/zarf -o . --plain-http --confirm \
     --set NE_APP_IMAGE="${NE_APP_REPOSITORY}:0.1.0@${NE_APP_DIGEST}" \
     --set NE_APP_REPOSITORY="${NE_APP_REPOSITORY}" \
     --set NE_APP_DIGEST="${NE_APP_DIGEST}"
   ```
6. **Transfer media** onto the air-gapped site: the zarf **binary**, the
   **zarf-init** tarball, the **zarf-package** tarball, the promoted config
   bundle + secret store, and — for a fast `tei-embed` (NE-TEI-ONNX) —
   the pre-staged `tei-cache/` directory built by `ne-serve stage-tei` (a
   `python -m ne.serve stage-tei Qwen/Qwen3-Embedding-0.6B --dest tei-cache
   --onnx-artifact <artifact> --export-manifest <manifest>` run alongside
   step 2, same egress-up staging window; see
   `deploy/compose/pilot/README.md`'s "Operator pre-stage: tei-embed ONNX
   cache" section for the full recipe). Skippable — `tei-embed` then serves
   from the slow candle CPU backend instead of failing.

### Disconnected side (ZERO egress)

```sh
zarf init --confirm                       # deploys zarf-docker-registry + agent
                                          # from the LOCAL init tarball (cwd)
kubectl create namespace neutron --dry-run=client -o json | kubectl apply -f -
# installer-mode C12 Secret — the `ne k8s up` step (e) seam, stdin-pipe only:
uv run python deploy/airgap/zarf/scripts/emit_ci_secret.py --bundle <bundle> \
  | kubectl apply -f -
zarf package deploy zarf-package-neutron-enterprise-amd64-0.1.0.tar.zst --confirm \
  --set NE_HOSTNAME=<vhost> --set NE_CONFIG_HASH=<promoted-config-hash> \
  [--key <cosign.pub>]
```

**Operator PVC pre-stage (NE-TEI-ONNX, only if a `tei-cache/` was carried in
step 6):** the `tei` sub-chart's `tei-embed` StatefulSet claims its `/data`
mount via a `volumeClaimTemplates` entry named `cache`, giving the PVC
`cache-tei-embed-0` in the `neutron` namespace once the StatefulSet's pod
exists. Copy the pre-staged tree in with a throwaway debug pod mounting that
same PVC and the ALREADY-PINNED TEI image (no new image pin) as the copy
container, then verify:

```sh
kubectl -n neutron run tei-cache-stage --rm -i --restart=Never \
  --image="$(kubectl -n neutron get statefulset tei-embed \
    -o jsonpath='{.spec.template.spec.containers[0].image}')" \
  --overrides='{"spec":{"containers":[{"name":"tei-cache-stage","image":"'"$(
    kubectl -n neutron get statefulset tei-embed \
      -o jsonpath='{.spec.template.spec.containers[0].image}'
  )"'","command":["sleep","3600"],"volumeMounts":[{"name":"data","mountPath":"/data"}]}],"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"cache-tei-embed-0"}}]}}' &
kubectl -n neutron wait --for=condition=Ready pod/tei-cache-stage --timeout=60s
kubectl -n neutron cp ./tei-cache tei-cache-stage:/staged
kubectl -n neutron exec tei-cache-stage -- cp -a /staged/. /data/
kubectl -n neutron delete pod tei-cache-stage
uv run python -m ne.serve stage-tei-verify --dest ./tei-cache   # boundary re-check (host copy)
```

Skippable — `tei-embed` restarts on its default first-boot HF download path
(the StatefulSet's own `HF_HUB_OFFLINE`/telemetry env is unaffected either
way) and serves from the slow candle CPU backend instead.

Verify: `kubectl -n neutron get pods`, all pods Ready, `/healthz` → 200 via
port-forward, and every container `imageID` served from Zarf's in-cluster
registry (never docker.io/quay.io/ghcr.io).

## 6. CI lane (`.github/workflows/zarf-package.yml`)

One job, `package-and-disconnected-deploy` (single job — a multi-GB package is
never shipped between jobs). Phases:

* **STAGING (egress open):** install pinned helm+zarf (sha256-verified), start
  the staging registry, build+push ne-app, `zarf package create`, then the
  **contents proof** (`scripts/assert_package_contents.py`: the built tarball's
  resolved definition carries all 4 digest-pinned images with **zero** surviving
  `###` tokens, plus `images/index.json` + `images/blobs/sha256/` + the vendored
  chart `.tgz`), then the parity guardian under a **must-run-not-skip** gate
  (helm is installed, so the render test MUST execute).
* **Anti-vacuous canary (NE-073 lesson):** a **positive** canary
  (`crictl pull` a pinned throwaway image) MUST succeed **before** the seal —
  proving egress was alive — otherwise the later "dead" proof is vacuous. The
  staging registry is then **destroyed** so the ne-app image can only come from
  Zarf's in-cluster registry.
* **SEAL:** `iptables -I DOCKER-USER -s <kind-subnet> ! -d <kind-subnet> -j DROP`
  (plus the v6 equivalent when present). The **negative** canary (same pull)
  MUST now fail.
* **DISCONNECTED DEPLOY:** `zarf init` + `zarf package deploy` from on-disk
  tarballs only; all pods Ready, `/healthz` 200, every imageID from Zarf's
  in-cluster registry, the ne-api imageID hard-asserted to carry the pushed
  digest, and the seal **re-asserted at job end**.

### Local-run notes

`zarf package create` + the parity guardian run on Linux **or** macOS (Docker +
helm + the pinned zarf binary). The disconnected **deploy** phase needs Linux +
Docker + `sudo iptables` (the DOCKER-USER seal) and does not run on macOS. If a
release engagement or a future runner hits disk pressure on the ~2–3 GB package
+ kind + inner registries, move Zarf's temp dir under `/mnt` with `--tmpdir`
(the NE-073 disk-relief precedent).

## 7. Deviations / limits

* **amd64-only** package for now (the init package + node image are amd64).
* NetworkPolicy / OpenShift-Route / backup flags stay **default-off** (unchanged
  from the chart) — the bundle is profile-agnostic OCI content.
* The `bundle.files` ConfigMap TOMLs are **empty** in the CI package (the
  fixture path); a real engagement supplies them via step 4 above.
* **Live-park fallback:** if the disconnected-deploy phase proves genuinely
  infeasible on GitHub runners after honest attempts, the parked form keeps the
  create + contents-assert + parity guardian as the PR gate and moves the deploy
  phase behind `workflow_dispatch`, with this runbook as the manual proof — flag
  the park explicitly in the PR body.
