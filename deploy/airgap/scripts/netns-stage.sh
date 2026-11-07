#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# STAGE step for the NE-073 no-egress CI harness.
#
# Creates the `ne-airgap` network namespace WITH temporary egress (a veth pair
# NAT'd to the host) so images/models can be pulled/downloaded, then starts a
# SECOND, PRIVATE dockerd inside that namespace. Egress is removed later by
# netns-seal.sh — this script only ever ADDS the temporary staging path, never
# anything that survives seal.
#
# The inner dockerd's control socket (a unix domain socket under /mnt) is the
# ONE cross-boundary channel this design accepts: unix sockets are not network
# sockets and bypass netns routing entirely — it never carries traffic that
# could reach an external host, only docker API calls from the root ns.
#
# The inner dockerd is launched via netns-inner-dockerd.sh (not directly) for
# two reasons: (1) `ip netns exec` detaches /sys into a bare sysfs, breaking
# cgroup-v2 detection on GitHub's cgroup-v2-only ubuntu-24.04 runners, so the
# launcher remounts cgroup2 in the private mount namespace first; and (2) a
# second dockerd started without an explicit `--containerd` silently attaches
# to the host's shared containerd (running tasks in the host mount+network
# namespaces — empty-rootfs crashes AND a bypassed egress seal), so the
# launcher starts a DEDICATED containerd inside this namespace and points
# dockerd at it. See deploy/airgap/README.md §2.1 (cgroup) and §2.2
# (containerd) for the full mechanism writeups.
#
# Requires: Linux, root (sudo), iproute2, iptables, nftables (installed here),
# a `docker` binary on PATH for both daemons.

set -euo pipefail

NS=ne-airgap
HOST_VETH=ne-veth-host
NS_VETH=ne-veth-ns
HOST_IP=10.199.0.1
NS_IP=10.199.0.2
PREFIX=30
STATE_DIR=/mnt/ne-airgap
NAT_COMMENT=ne-airgap-stage

echo "== netns-stage: creating ${NS} =="

sudo ip netns add "${NS}"
sudo ip netns exec "${NS}" ip link set lo up

# veth pair: host end stays in the root ns, ns end moves into ne-airgap.
sudo ip link add "${HOST_VETH}" type veth peer name "${NS_VETH}"
sudo ip link set "${NS_VETH}" netns "${NS}"

sudo ip addr add "${HOST_IP}/${PREFIX}" dev "${HOST_VETH}"
sudo ip link set "${HOST_VETH}" up

sudo ip netns exec "${NS}" ip addr add "${NS_IP}/${PREFIX}" dev "${NS_VETH}"
sudo ip netns exec "${NS}" ip link set "${NS_VETH}" up
sudo ip netns exec "${NS}" ip route add default via "${HOST_IP}"

# Host-side forwarding + NAT so the namespace can actually reach the internet
# during staging. Tagged with a comment so netns-seal.sh can find+delete
# exactly these rules (and nothing else on the runner).
sudo sysctl -w net.ipv4.ip_forward=1 >/dev/null

sudo iptables -t nat -A POSTROUTING -s "${NS_IP}/${PREFIX}" -m comment --comment "${NAT_COMMENT}" -j MASQUERADE
sudo iptables -A FORWARD -s "${NS_IP}/${PREFIX}" -m comment --comment "${NAT_COMMENT}" -j ACCEPT
sudo iptables -A FORWARD -d "${NS_IP}/${PREFIX}" -m comment --comment "${NAT_COMMENT}" -j ACCEPT

# DNS inside the namespace during staging only.
sudo mkdir -p /etc/netns/"${NS}"
echo "nameserver 1.1.1.1" | sudo tee /etc/netns/"${NS}"/resolv.conf >/dev/null

# nftables must be installed BEFORE seal (never after — post-seal there is no
# egress to apt-get with).
if ! command -v nft >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y nftables
fi

sudo mkdir -p "${STATE_DIR}"/{docker-data,docker-exec,run,models,secret-store}
sudo chown "$(id -u):$(id -g)" "${STATE_DIR}" "${STATE_DIR}"/{docker-data,docker-exec,run,models,secret-store}

# Explicit empty --config-file so the OUTER daemon's /etc/docker/daemon.json is
# never inherited by the inner daemon (fully independent config surfaces).
echo '{}' > "${STATE_DIR}/daemon.json"

echo "== netns-stage: starting inner dockerd (cgroup2 remount via netns-inner-dockerd.sh) =="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC2024
# The redirect below intentionally runs as the invoking runner user (not
# root): STATE_DIR was chown'd to that user above, and `sudo ip netns exec`
# only elevates the command it runs, not this shell's own redirects — same
# behavior as the direct-dockerd launch this replaces.
sudo ip netns exec "${NS}" "${SCRIPT_DIR}/netns-inner-dockerd.sh" "${STATE_DIR}" \
  >"${STATE_DIR}/dockerd.log" 2>&1 &
disown

echo "== netns-stage: waiting for inner dockerd readiness (up to 60s) =="
ready=0
for _ in $(seq 1 60); do
  if sudo docker -H "unix://${STATE_DIR}/run/docker.sock" info >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done

if [ "${ready}" -ne 1 ]; then
  echo "ERROR: inner dockerd did not become ready within 60s" >&2
  cat "${STATE_DIR}/dockerd.log" >&2 || true
  cat "${STATE_DIR}/containerd.log" >&2 || true
  exit 1
fi

# Readiness is NOT just "the daemon answers `docker info`" — it must be able to
# actually RUN a container. Historically the gate stopped at `docker info`, so a
# daemon that came up healthy but whose tasks were being executed by the wrong
# (host) containerd — empty rootfs, host-egress namespace — sailed through
# staging and only exploded at the first real `docker run` post-seal, where an
# empty-rootfs failure is indistinguishable from a genuine egress-dead result.
# We therefore run a tiny throwaway container HERE, while egress is still up, so
# any "daemon up but containers can't run" regression fails LOUD at the stage
# step. hello-world does zero networking and exits 0; it is digest-pinned (see
# deploy/airgap/README.md §6 provenance) and never shipped.
SMOKE_IMAGE="hello-world@sha256:96498ffd522e70807ab6384a5c0485a79b9c7c08ca79ba08623edcad1054e62d"
echo "== netns-stage: container-run smoke (egress still up) via ${SMOKE_IMAGE} =="
if ! sudo docker -H "unix://${STATE_DIR}/run/docker.sock" pull -q "${SMOKE_IMAGE}" >/dev/null; then
  echo "ERROR: could not pull the container-run smoke image ${SMOKE_IMAGE} during staging (egress should be up here)" >&2
  cat "${STATE_DIR}/dockerd.log" >&2 || true
  exit 1
fi
if ! sudo docker -H "unix://${STATE_DIR}/run/docker.sock" run --rm "${SMOKE_IMAGE}" >/dev/null 2>&1; then
  echo "ERROR: inner dockerd answers 'docker info' but CANNOT RUN a container — the daemon is up but its tasks are broken (e.g. attached to the wrong containerd, so runc pivot_roots into an empty rootfs). Refusing to proceed to seal." >&2
  cat "${STATE_DIR}/dockerd.log" >&2 || true
  cat "${STATE_DIR}/containerd.log" >&2 || true
  exit 1
fi

echo "== netns-stage: ${NS} up, inner dockerd ready + container-run smoke passed at unix://${STATE_DIR}/run/docker.sock =="
