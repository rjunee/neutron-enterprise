#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# netns-inner-dockerd.sh STATE_DIR
#
# Invoked by netns-stage.sh as: `ip netns exec ne-airgap netns-inner-dockerd.sh
# <STATE_DIR>`, running as root INSIDE the private mount namespace that `ip
# netns exec` creates for the target network namespace.
#
# This launcher solves TWO distinct failures that only surface when a SECOND
# dockerd is started inside a network namespace on a runner that already runs
# Docker (every GitHub-hosted runner does). Both are documented in full so a
# future editor never "simplifies" either away.
#
# ---------------------------------------------------------------------------
# FIX 1 — cgroup v2 detection (web-verified; RH bug 892927 / moby#21090)
# ---------------------------------------------------------------------------
#   `ip netns exec` unshares a mount namespace so it can bind-mount a
#   namespace-private /sys (giving the namespace's own network devices under
#   /sys/class/net) — and doing so DETACHES THE HOST'S /sys WHOLESALE and
#   mounts a fresh, bare sysfs in its place. The host's cgroup v2 hierarchy at
#   /sys/fs/cgroup goes with it.
#
#   dockerd detects the cgroup version by statfs(2)'ing /sys/fs/cgroup. With
#   that path now a bare sysfs (not cgroup2fs), dockerd falls back to the
#   legacy cgroup v1 code path and dies with "Devices cgroup isn't mounted" —
#   fatal on GitHub's ubuntu-24.04 hosted runners, which are cgroup-v2-only.
#
#   Remedy (same recipe docker-library's docker:dind uses): inside this PRIVATE
#   mount namespace, remount the single host-global cgroup2 hierarchy at
#   /sys/fs/cgroup, then launch dockerd forced onto the cgroupfs driver under a
#   dedicated /ne-airgap top-level subtree. We are root in the init user
#   namespace (no UID remapping), so mounting cgroup2 needs no delegation
#   dance; and `ip netns exec`'s mount propagation is rslave, so this remount
#   is private to the namespace and can never leak back to the host.
#
# ---------------------------------------------------------------------------
# FIX 2 — a DEDICATED containerd, so tasks run in THIS namespace (locally
#         root-caused + reproduced; see deploy/airgap/README.md §2.2)
# ---------------------------------------------------------------------------
#   dockerd does not run containers itself — it hands each task to a containerd,
#   which spawns a containerd-shim + runc that live in CONTAINERD'S mount and
#   network namespaces. When dockerd is started WITHOUT an explicit
#   `--containerd <socket>`, it attaches to whatever containerd is already
#   listening at the well-known socket — and on a runner that already runs
#   Docker, that is the HOST's system containerd, running in the HOST's mount +
#   network namespaces (verified: the inner dockerd spawns NO containerd child;
#   its only containerd is the host's, pid-1-parented, in the root namespaces).
#
#   Consequences of that silent attach — both fatal to this harness:
#     1. CRASH. The host containerd's shim/runc run in the HOST mount namespace
#        and cannot see the inner daemon's overlay2 storage (a different
#        --data-root, whose merged rootfs is mounted only in THIS private mount
#        namespace). runc pivot_roots into an empty rootfs, so the very first
#        `docker run` dies with `exec: "/entrypoint.sh": no such file or
#        directory` (or `"sh": executable file not found`) and a
#        "reading from a closed fifo" stdio error in dockerd.log.
#     2. SILENT EGRESS-PROOF INVALIDATION (worse). Those tasks also run in the
#        HOST network namespace — with full host egress — instead of the sealed
#        ne-airgap netns. The container-level canary would then be probing from
#        the wrong namespace entirely, so the whole no-egress guarantee would be
#        vacuous without ever failing loudly.
#
#   Remedy: start a DEDICATED containerd INSIDE this private namespace, with its
#   own root/state dirs and its own grpc socket under STATE_DIR, then launch
#   dockerd with `--containerd <that socket>`. containerd — and every
#   containerd-shim/runc it spawns — then inherits THIS mount + network
#   namespace: the container rootfs is visible (containers actually run) and
#   container egress is the sealed netns (the proof is sound). Verified locally
#   under the exact `ip netns exec` recipe (arm64): with the shared/host
#   containerd `docker run` fails as above; with the dedicated containerd it
#   succeeds and the shim's /proc/<pid>/ns/{mnt,net} match the inner dockerd's.
#
# Fails LOUDLY, never downgrading: if /sys/fs/cgroup is not cgroup2fs after the
# remount, if the containerd binary is missing, or if the dedicated containerd
# never comes up, this script exits 1 with a clear message rather than letting
# dockerd limp on in a broken/insecure mode further downstream.
#
# See deploy/airgap/README.md §2.1 (cgroup) and §2.2 (containerd) for the full
# mechanism writeups.

set -euo pipefail

STATE_DIR="${1:?usage: netns-inner-dockerd.sh STATE_DIR}"

CONTAINERD_SOCK="${STATE_DIR}/run/containerd.sock"
CONTAINERD_CONFIG="${STATE_DIR}/containerd.toml"
CONTAINERD_LOG="${STATE_DIR}/containerd.log"
CONTAINERD_PIDFILE="${STATE_DIR}/run/containerd.pid"

# --- FIX 1: cgroup v2 remount (private to this mount namespace) -------------
if ! mountpoint -q /sys/fs/cgroup; then
  mount -t cgroup2 cgroup2 /sys/fs/cgroup
fi

fstype="$(stat -f -c %T /sys/fs/cgroup)"
if [ "${fstype}" != "cgroup2fs" ]; then
  echo "ERROR: /sys/fs/cgroup is not cgroup2fs after remount (got: ${fstype}) — refusing to fall back to a legacy cgroup-v1 launch" >&2
  exit 1
fi

# --- FIX 2: dedicated containerd inside THIS namespace ----------------------
if ! command -v containerd >/dev/null 2>&1; then
  echo "ERROR: containerd binary not found on PATH — the inner dockerd MUST run its own containerd inside this namespace (attaching to the host's shared containerd would run every task in the host mount+network namespaces: empty-rootfs crashes AND a silently-bypassed egress seal). Refusing to start." >&2
  exit 1
fi

mkdir -p "${STATE_DIR}/containerd-root" "${STATE_DIR}/containerd-state" "${STATE_DIR}/run"

# Minimal, fully self-contained containerd config: private root/state and a
# private grpc socket, both under STATE_DIR. CRI is disabled (dockerd drives
# containerd directly; we never need the Kubernetes CRI listener), and no
# ambient cgroup is claimed for the containerd process itself.
cat > "${CONTAINERD_CONFIG}" <<EOF
version = 2
root = "${STATE_DIR}/containerd-root"
state = "${STATE_DIR}/containerd-state"

[grpc]
  address = "${CONTAINERD_SOCK}"

[plugins."io.containerd.grpc.v1.cri"]
  disable = true

[cgroup]
  path = ""
EOF

containerd --config "${CONTAINERD_CONFIG}" >"${CONTAINERD_LOG}" 2>&1 &
containerd_pid=$!
echo "${containerd_pid}" > "${CONTAINERD_PIDFILE}"

# Wait (fail-loud) for the dedicated containerd's socket to appear.
ready=0
for _ in $(seq 1 60); do
  if [ -S "${CONTAINERD_SOCK}" ]; then
    ready=1
    break
  fi
  if ! kill -0 "${containerd_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

if [ "${ready}" -ne 1 ]; then
  echo "ERROR: dedicated containerd did not open its socket at ${CONTAINERD_SOCK} within 30s — refusing to fall back to the host's shared containerd (which would run tasks in the host namespaces)" >&2
  cat "${CONTAINERD_LOG}" >&2 || true
  exit 1
fi

exec dockerd \
  --config-file "${STATE_DIR}/daemon.json" \
  --host "unix://${STATE_DIR}/run/docker.sock" \
  --data-root "${STATE_DIR}/docker-data" \
  --exec-root "${STATE_DIR}/docker-exec" \
  --pidfile "${STATE_DIR}/run/dockerd.pid" \
  --containerd "${CONTAINERD_SOCK}" \
  --group docker \
  --exec-opt native.cgroupdriver=cgroupfs \
  --cgroup-parent /ne-airgap
