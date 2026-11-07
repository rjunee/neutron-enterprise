# deploy/airgap — the no-egress CI harness (NE-073)

This directory implements SPEC §2's C1/C2 CI proof: the **no-egress CI
harness**. It installs the full NE-070 pilot stack inside a network namespace
with **no egress path whatsoever**, then proves — inside that sealed
namespace — that (a) the frontier model lane is dead and (b) the open-weight
(CPU vLLM) lane keeps serving governed inference.

The full **offline bundle** (Zarf-pattern, for real customer air-gap installs)
now lives at **`deploy/airgap/zarf/` (NE-086, built)** — a self-contained Zarf
package that installs the whole platform into a disconnected cluster from one
tarball. **This** harness remains the CI *proof* of the sovereignty properties;
the Zarf package is the shippable *artifact* reusing the same threat model.

## Persistent PostgreSQL/AGE Pilot artifact

During the connected staging window, build the admitted artifact at the exact
source revision and export it for transfer:

```sh
NE_POSTGRES_AGE_SOURCE_REV=7b7cede30d7af58ac3259b51cf59c76f1e95754c \
  bash deploy/airgap/scripts/build-ne-postgres-age.sh
bash deploy/airgap/scripts/package-ne-postgres-age.sh --out ./airgap-images
```

Transfer the emitted tar and JSON manifest with the normal air-gap media. On
the disconnected Pilot host, sha256-check the tar against the manifest, run
`docker load --input ...`, and verify the loaded image ID and source label match
the manifest before `ne up`. The Pilot Compose then admits that exact ID against
the retained PostgreSQL system identifier before either application root starts.

## 1. Mechanism: stage → seal → prove → install → test

The `no-egress` workflow (`.github/workflows/no-egress.yml`) runs one job on
`ubuntu-latest` through five phases. **Note (NE-CI-NOEGRESS-VLLM-FLAKE, see
§1.1 below):** phase C (step 5's open-weight lane) only runs in the *full*
lane (push to `main`, weekly cron, manual dispatch); the *PR* lane runs
phases 1-4 plus phase 5's deterministic sub-steps only.

1. **Stage.** Create the `ne-airgap` network namespace WITH temporary egress
   (a veth pair NAT'd through the runner's own network) and start a SECOND,
   **private** `dockerd` inside it (`deploy/airgap/scripts/netns-stage.sh` →
   `netns-inner-dockerd.sh`, which first remounts the cgroup2 hierarchy — see
   §2.1). Pull every pilot/serve image, pre-warm the TEI embed/rerank weight
   caches, and pre-stage + integrity-verify the vLLM CPU model — all while
   egress is still open, exactly like transferring media onto an air-gapped
   site before disconnecting it.
2. **Anti-vacuous canary (must SUCCEED).** Before sealing, `canary.sh
   expect-reachable https://api.anthropic.com` MUST pass (host- and
   container-level). If the staging network is somehow already broken, the
   later "prove-dead" step would pass for a vacuous reason (nothing was ever
   reachable) — this canary makes the whole proof non-vacuous.
3. **Seal (the point of no return).** `deploy/airgap/scripts/netns-seal.sh`
   deletes the staging veth pair and the default route (v4 **and** v6),
   deletes the tagged NAT/FORWARD rules in the root namespace, and installs a
   belt-and-braces `nftables` counter-drop policy inside the namespace. After
   this point, **every** step that runs in a given lane (install, phases
   A/B1/B2, plus C in the full lane — see §1.1) runs with egress
   **topologically impossible** — not merely firewalled.
4. **Prove-dead.** `canary.sh expect-dead` against `api.anthropic.com`,
   `api.openai.com`, and the DNS-independent raw IP `1.1.1.1` — all three MUST
   fail closed (host- and container-level), or the job fails.
5. **Install + test.** The REAL `ne up --profile core --profile obs --bundle
   ...` installer brings up the pilot stack **inside** the sealed namespace.
   `deploy/airgap/tests/test_airgap_phase_a.py` (frontier-dead, both layers +
   healthy install) runs via the executing-gate wrapper
   (`run-airgap-pytest.sh`), then phases B1/B2 (brain, offline) run — and, in
   the full lane only (§1.1), C (open-weight lane, offline) too — in sequence
   against a **phased** bring-up (see §4). `netns-assert-sealed.sh` re-checks
   the seal after bring-up and again at job end. Teardown (`netns-down.sh`)
   always runs.

### 1.1 PR lane vs full lane (NE-CI-NOEGRESS-VLLM-FLAKE)

Phase C (the vLLM-CPU open-weight bring-up) intermittently failed a
**REQUIRED** merge gate for reasons unrelated to the diff: the EngineCore
subprocess dies inside `determine_available_memory` with `RuntimeError:
cancelled (shm_broadcast)` — a worker-process death during memory probing on
a resource-constrained GitHub-hosted runner, not a code fault (byte-identical
signature every time, ~18% first-attempt failure rate, always green on a
re-run of the same commit — PRs #319/#320/#328/#331). A REQUIRED gate that
cries wolf trains reviewers to wave it through, which is worse than no gate
at all. `.github/workflows/no-egress.yml` therefore splits into two lanes,
same job, same required-check name (`no-egress`):

* **Still blocks every PR:** the stack-images pull smoke, the anti-vacuous
  canary, the SEAL, all three prove-dead probes, Phase A (air-gapped install
  + in-namespace TLS smoke + the fail-closed container canary), Phase B1,
  Phase B2, re-assert-seal — plus the **deterministic** half of the
  open-weight lane's staging: the `cpu`-profile image pull (by pin) and the
  vLLM weight download + hash verify. A PR that breaks an image pin or the
  `ne.serve download`/`verify` path still goes red on the PR gate.
* **No longer blocks a PR** (moved, not deleted): the nondeterministic
  vLLM-CPU engine bring-up loop and its `test_airgap_phase_c.py` pytest.
  These now run on **every push to main** (the `push` trigger has no path
  filter), the **weekly cron**, and **manual `workflow_dispatch`** — so the
  coverage is retained at a real cadence, not lost.
* **Where a Phase C failure surfaces** once it's off the PR gate: the commit
  status on the `main` push, the run in the Actions tab, and GitHub's normal
  failure notification to the actor who pushed it. It is **not** silent.
* **Triage rule:** the step emits an annotation classifying the failure
  signature — `::warning title=vllm-cpu bring-up exhausted 5 attempts::...`
  when the logs match the tracked `determine_available_memory` /
  `shm_broadcast` / `EngineCore` signature, `::error title=...::UNKNOWN
  failure signature...` otherwise. **Both are real failures that need a
  human look** — the warning annotation is a triage hint (probably the known
  runner-resource flake), never an auto-dismiss. The classifier is
  annotation-only: it always falls through to the same `exit 1` either way.
* **Retry budget stays frozen at 5** — NE-071c-FU already raised it 3→5
  without curing the flake; raising it again was rejected as a fix by
  NE-CI-NOEGRESS-VLLM-FLAKE.
* **Force the full lane on a PR branch:** `gh workflow run no-egress.yml
  --ref <branch>` runs Phase C on demand without merging first.
* **Known limitation:** `concurrency.cancel-in-progress: true` means rapid
  consecutive merges to `main` can cancel an intermediate run before Phase C
  finishes on that particular commit — the head of `main` always eventually
  gets a completed run, but an in-between commit's Phase C result may never
  materialize. This is an accepted tradeoff of the existing concurrency
  group, unchanged by this unit.

### 1.2 First-party build-in-seal (NE-103a)

The pilot `core` profile now includes the first-party `ne-app` image (running
`ne-api`). Unlike every third-party image, it is **not** on a public registry —
so it cannot be `compose pull`ed, and the C11 `tag@sha256` rule cannot pin it.
Instead the workflow builds it **during the staging window** (egress up, before
the seal) with `deploy/airgap/scripts/build-ne-app.sh --iidfile
/mnt/ne-airgap/ne-app.iid` — exactly like copying media onto an air-gapped site
before disconnecting it. This is owner decision (ii): **build-in-seal +
local-digest pin**.

The `--iidfile` captures the built image's local id. Compose sets
`pull_policy: never` on `ne-api`, so the sealed `ne up` never tries to pull it,
and `compose pull` cleanly skips it (the build step is ordered **before** the
pull step). Air-gap Phase A then asserts the running `ne-pilot-ne-api-1`
container's image id equals the staged `--iidfile` id
(`test_ne_api_runs_the_staged_local_image`) — **that match is the pin** — and
that `/healthz` answers `{"status":"ok"}` in-container with zero egress. The
image's own three base images ARE `tag@sha256`-pinned (see
`deploy/docker/ne-app/README.md`). The full OIDC-live bring-up (needs the pilot
Keycloak realm) parks to NE-103b; Phase A brings ne-api up healthy on core-only
infra because its JWKS/fastembed/qdrant/TEI edges are all lazy.

## 2. Threat model — why no packet can leave

After `netns-seal.sh` runs, the `ne-airgap` namespace has:

* **no interface to the root namespace.** The staging veth pair
  (`ne-veth-host` / `ne-veth-ns`) is destroyed outright — there is no wire
  left, not merely a disabled one.
* **no default route**, IPv4 or IPv6. `netns-assert-sealed.sh` asserts both
  are empty at seal time, after bring-up, and at job end.
* **no DNS path.** `/etc/netns/ne-airgap/resolv.conf` still names a
  nameserver (`1.1.1.1`), but with no default route there is no way to reach
  it — the file is a dead artifact, not a live path. (This also closes the
  classic "dockerd's embedded DNS silently forwards through the host
  resolver" exfil channel some naive egress-blocking designs miss: the
  embedded resolver has nothing to forward to.)
* **only interfaces are `lo`, `docker0`/`br-*`, and veths whose `master` is
  one of those in-namespace bridges** — i.e. pure container plumbing for the
  pilot stack's own containers, asserted by `netns-assert-sealed.sh`'s
  interface-topology check. Anything else is a FAIL.
* **belt-and-braces `nftables`** (`table inet ne_airgap_seal`): even if some
  future change reintroduced a route, the `output`/`forward` hook chains
  accept only loopback/private-range traffic and `counter drop` everything
  else. This is defense in depth on top of the topology, not the primary
  guarantee.

The **one** documented cross-boundary channel is the inner `dockerd`'s unix
control socket (`/mnt/ne-airgap/run/docker.sock`), reachable from the root
namespace because unix domain sockets are filesystem objects, not network
sockets — they bypass netns routing entirely. This is control-plane only
(Docker API calls issued from the CI job's outer process); it never carries
application data and cannot be used to reach an external host.

### 2.1 cgroup v2: how the inner dockerd gets its cgroups

`ip netns exec` unshares a mount namespace so the target namespace can have
its own `/sys/class/net` view — but doing so **detaches the host's `/sys`
wholesale** and mounts a fresh, bare sysfs in its place (Red Hat bug 892927,
"ip netns exec unmounts /sys/fs/cgroup"; see also moby/moby#21090, "Docker
daemon in network namespace"). dockerd detects the cgroup version via
`statfs(2)` on `/sys/fs/cgroup`; with that path now a bare sysfs instead of
`cgroup2fs`, it falls back to the legacy cgroup v1 code path and dies with
`Devices cgroup isn't mounted` — fatal on GitHub's `ubuntu-24.04` hosted
runners, which are cgroup-v2-only (no v1 hierarchies exist to fall back to).

The fix, in `deploy/airgap/scripts/netns-inner-dockerd.sh`, is the same
recipe docker-library's `docker:dind` image uses: inside this **private**
mount namespace (never the host's), remount the single host-global cgroup2
hierarchy at `/sys/fs/cgroup`, hard-assert `cgroup2fs` (fail loudly, no
fallback), then `exec dockerd` forced onto the `cgroupfs` driver
(`--exec-opt native.cgroupdriver=cgroupfs`) under a dedicated `/ne-airgap`
top-level cgroup subtree (`--cgroup-parent /ne-airgap`):

* We are root in the **init user namespace** here (no UID remapping), so
  mounting cgroup2 needs no cgroup-delegation dance.
* `ip netns exec`'s mount propagation is `rslave` — this remount is entirely
  private to the namespace's mount namespace and can never leak back to the
  host's mount table.
* The dedicated `/ne-airgap` subtree means the inner daemon's containers
  never contend with the host daemon's systemd-managed cgroups; runc's fs2
  (cgroup v2) manager auto-enables the controllers (`+cpu +memory +io
  +pids`) on ancestor cgroups as needed, so no manual `subtree_control`
  writes are required.

### 2.2 containerd isolation: why the inner dockerd runs its own containerd

dockerd does not run containers itself — it delegates every task to a
**containerd**, which spawns the `containerd-shim` + `runc` that actually
create the container. Those live in *containerd's* mount and network
namespaces, **not** dockerd's.

A second dockerd started without an explicit `--containerd <socket>` attaches
to whatever containerd is already listening at the well-known socket. On any
runner that already runs Docker — every GitHub-hosted runner does — that is
the **host's system containerd**, running in the **host's** mount + network
namespaces. (Reproduced locally: the inner dockerd spawns *no* containerd
child; its only containerd is the host's, parented to pid 1, in the root
namespaces.) Two failures follow, both fatal to this harness:

1. **Empty-rootfs crash.** The host containerd's shim/runc run in the host
   mount namespace and cannot see the inner daemon's `overlay2` storage — a
   different `--data-root` whose merged rootfs is mounted *only* in the inner
   private mount namespace. `runc` pivot_roots into an empty rootfs, so the
   first `docker run` dies with `exec: "/entrypoint.sh": no such file or
   directory` (or `"sh": executable file not found`) plus a
   `reading from a closed fifo` stdio error in `dockerd.log`.
2. **Silent egress-proof invalidation (worse).** Those tasks also run in the
   **host** network namespace, with full host egress, instead of the sealed
   `ne-airgap` netns. The container-level canary would then probe from the
   wrong namespace — the whole no-egress guarantee would be vacuous *without
   ever failing loudly*.

The fix, in `netns-inner-dockerd.sh`, is the `docker:dind`-equivalent recipe
for a shared-host filesystem: start a **dedicated containerd inside this
namespace** (its own `root`/`state` dirs and its own grpc socket under
`STATE_DIR`), wait fail-loud for its socket, then launch dockerd with
`--containerd <that socket>`. containerd — and every shim/runc it spawns —
then inherits *this* mount + network namespace: container rootfs is visible
(containers actually run) and container egress is the sealed netns (the proof
is sound). Verified locally under the exact `ip netns exec` recipe (arm64):
with the shared/host containerd `docker run` fails as above; with the
dedicated containerd it succeeds and the shim's `/proc/<pid>/ns/{mnt,net}`
match the inner dockerd's. `netns-stage.sh`'s readiness gate now runs a tiny
`hello-world` container (while egress is still up) so this can never again
slip past staging and surface only at the post-seal canary.

## 3. Frontier-dead, proven at BOTH layers

`deploy/airgap/tests/test_airgap_phase_a.py` proves the frontier lane is dead
two independent ways in the same run:

* **Software-refusal.** The real `LiteLLMGateway`, wired with
  `EgressPolicy(air_gap=True)`, raises `AirGapViolationError` for a frontier
  request **before litellm is ever invoked** — the cost sink stays empty.
  This is NE-012/NE-CKC2's existing C2 guardian, exercised for real here.
* **Network-impossibility.** No default route (v4/v6); a raw-IP connect to
  `1.1.1.1:443` raises `OSError`; `api.anthropic.com`/`api.openai.com` don't
  even resolve (`socket.gaierror`); and a direct `httpx` POST to
  `https://api.anthropic.com/v1/messages` — simulating a fully
  policy-bypassed client that skips our software guard entirely — still fails
  with `httpx.TransportError`. The workflow's own canaries (host- and
  container-level, including one via the inner dockerd's default bridge
  after bring-up) reinforce the same result from outside pytest.

Neither proof alone would be sufficient (software-only leaves open "what if
the guard is buggy/bypassed"; network-only leaves open "what if the policy
layer never engages"). Both hold in the same sealed run.

## 4. Open-weight-alive, proven with real inference

`deploy/airgap/tests/test_airgap_phase_c.py` brings up `vllm-cpu` (CPU-mode
vLLM, `Qwen/Qwen3-0.6B`, weights pre-staged and integrity-verified via
`python -m ne.serve verify` at the air-gap boundary, `HF_HUB_OFFLINE=1`)
**inside the sealed namespace** and drives a real health check, model list,
and chat completion through `ne.serve.client.ServeClient` — the exact client
NE-013 ships. The completion is real generated text from a real CPU forward
pass, not a mock. Raw-IP and frontier-DNS dead-checks run in the same test
session, proving the inference happened **while** egress was impossible.

This is the SPEC C2 guarantee end to end: `AIR_GAP` kills the frontier lane
completely while the open-weight/private-vllm lane — governed inference —
keeps working.

## 5. Why phased bring-up (not one shot)

GitHub-hosted runners for **private** repositories are 2 vCPU / 7 GB RAM / 14
GB SSD root disk (+ ~60 GB on `/mnt`) — confirmed against GitHub's hosted-
runner reference; the 4-vCPU upgrade applies to public repos only. Bringing
up `core` + `brain` + `obs` + `cpu` simultaneously does not fit that budget
(TEI embed/rerank alone are ~4 GB each; vLLM CPU needs several GB of RSS on
top of its KV cache). The harness instead runs phases **sequentially**:

* Phase A: `core` + `obs` (~1.6 GB) — installer proof + frontier-dead.
* Phase B1/B2: `brain`, one TEI service at a time (~3-4 GB) — offline
  serving proof.
* Phase C: `cpu` only, with `VLLM_CPU_KVCACHE_SPACE=1` and
  `--max-model-len 2048` (`deploy/airgap/compose.ci.yaml` overlay) — CPU
  inference in ~4.5 GB.

All heavy state (the inner dockerd's `--data-root`, the pre-staged model
weights, the pilot bundle, and — NE-TEI-ONNX / NE-TEI-GEN — the staged
tei-embed hub cache under `/mnt/ne-airgap/tei-cache`; the fast-ONNX pair is
generated-on-demand + locally cached on egress-OK hosts, or carried in the
sealed bundle for air-gap — nothing is release-downloaded) lives under
`/mnt/ne-airgap` (the runner's larger, separate volume), never the small root
disk.

A single-shot, simultaneous full-stack bring-up is deferred to the **NE-077**
real-VM gate (a machine sized like an actual pilot deployment) — this
harness's job is the no-egress *guarantee*, not full-stack load capacity.

## 6. Canary image provenance

| Image | Digest | License | Verified | Purpose |
|---|---|---|---|---|
| `curlimages/curl:8.21.0` | `sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13` | curl license (MIT-like, permissive) | 2026-07-07 | CI-canary only — a throwaway container used to probe egress from the same network path a compromised pilot-stack container would have. **Never shipped in any release artifact.** |
| `hello-world` (`:latest`) | `sha256:96498ffd522e70807ab6384a5c0485a79b9c7c08ca79ba08623edcad1054e62d` | MIT (Docker Inc.) | 2026-07-07 | CI-only container-run smoke pulled during staging (egress up) by `netns-stage.sh` to prove the inner dockerd can actually RUN a container — not merely answer `docker info` — before seal. Does zero networking; exits 0. **Never shipped in any release artifact.** |

Digest resolved via the Docker Registry HTTP API v2 manifest lookup
(equivalent to `docker buildx imagetools inspect curlimages/curl:8.21.0`,
which was unavailable in the build environment used for this unit — the raw
registry digest is authoritative either way and is what `canary.sh` and
`test_static_harness.py` both pin against). `deploy/airgap/tests/
test_static_harness.py` cross-checks the digest embedded in `canary.sh`
against this table on every run, so the two can never silently drift apart.

## 7. Local-run notes

This harness needs a real Linux kernel with network-namespace + `nftables`
support and root (`sudo`) — it is **not** runnable on macOS (no netns, no
Docker-in-netns story) or in an unprivileged container. To run it locally on
Linux:

```sh
sudo deploy/airgap/scripts/netns-stage.sh
# ... pull/pre-warm/pre-stage as the workflow does ...
sudo deploy/airgap/scripts/netns-seal.sh
deploy/airgap/scripts/canary.sh expect-dead https://api.anthropic.com
sudo ip netns exec ne-airgap sudo -u "$(whoami)" env HOME="$HOME" PATH="$PATH" \
  DOCKER_HOST=unix:///mnt/ne-airgap/run/docker.sock \
  "$PWD/.venv/bin/ne" up --profile core --profile obs --bundle /mnt/ne-airgap/bundle
deploy/airgap/scripts/run-airgap-pytest.sh deploy/airgap/tests/test_airgap_phase_a.py
sudo deploy/airgap/scripts/netns-down.sh
```

The **static** guardian (`test_static_harness.py`) and the phase modules'
module-level `NE_AIRGAP` skip need nothing special —
`uv run pytest deploy/airgap/tests -q` runs anywhere (phase modules report
as skipped outside the harness, which is correct: the live proof only means
something inside the sealed namespace).

## 8. Relationship to other units

* **NE-CKC2 / NE-012 gateway guardians** (`packages/ne-gateway/tests/
  test_no_egress.py`) are the unit-level C2 proof that `EgressPolicy` behaves
  correctly against litellm's `mock_response` boundary. This harness
  **composes with** those guardians (it exercises the SAME `EgressPolicy` /
  `AirGapViolationError` code path for real) and never replaces them — they
  stay the fast, always-on first line; this harness is the slow, full-stack,
  genuinely-network-blocked second line.
* **HF_HUB_OFFLINE / vendored assets** (NE-CKC9 and friends) are exercised,
  never weakened: `HF_HUB_OFFLINE=1`/`NE_PILOT_HF_OFFLINE=1` are set on every
  post-seal step, and the vLLM weights are pre-staged + `ne.serve
  verify`-checked before the seal — the harness never reaches HuggingFace at
  runtime. **Honesty note (NE-TEI-ONNX):** for `tei-embed`/`tei-rerank`
  specifically, `HF_HUB_OFFLINE` is a documented no-op (the pinned `hf-hub`
  0.4.2 Rust crate TEI ships never reads it) — TEI's actual offline guarantee
  is the sealed network this harness proves **plus** the pre-staged,
  sha256-verified cache `ne-serve stage-tei`/`stage-tei-verify` build (see
  `deploy/compose/pilot/README.md`'s Air-gap section for the full
  correction). The env var is kept anyway (harmless; meaningful to Python-
  side hub tooling), it just isn't what makes phases B1/B2 pass offline.
* **NE-086** is the full **offline bundle** (Zarf-pattern) for actual customer
  air-gap installs — a packaging/deployment deliverable now built at
  `deploy/airgap/zarf/` (`zarf.yaml` + parity guardian + the
  `.github/workflows/zarf-package.yml` stage→seal→deploy proof). This unit stays
  the CI *proof*; NE-086 reuses the same threat model (sealed network, no egress
  at deploy time) but produces a shippable bundle, not a workflow.
* **NE-077** is the real-VM (>=16 GB) gate where the FULL stack (core + brain
  + obs + cpu) comes up simultaneously — this harness's phased bring-up is a
  CI-runner-budget adaptation, not the target production shape.

## 9. Trigger policy

* **`push` to `main`** — always run (this is a durable guardian, not just a
  PR check).
* **`pull_request`**, path-filtered to `deploy/**`, `packages/ne-gateway/**`,
  `packages/ne-serve/**`, `packages/ne-secrets/**`, `tools/ne-cli/**`, and the
  workflow file itself — the harness is expensive (a nested dockerd + several
  image pulls + real inference), so it only runs on PRs that could plausibly
  affect the no-egress guarantee.
* **`schedule` (weekly, Tuesdays 05:23 UTC)** — catches drift from anything
  the path filter might miss (e.g. an upstream base-image CVE fix, or a
  runner image change).
* **`workflow_dispatch`** — manual re-run for investigation.
