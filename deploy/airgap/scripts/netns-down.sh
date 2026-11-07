#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Best-effort teardown for the NE-073 no-egress CI harness. Intended to run
# in an `if: always()` workflow step so a mid-run failure never leaves the
# runner with a dangling namespace / inner dockerd / iptables rules. Every
# step here is best-effort (`|| true`) — a partial prior failure must never
# stop teardown from attempting everything else.

set -uo pipefail

NS=ne-airgap
STATE_DIR=/mnt/ne-airgap
NAT_COMMENT=ne-airgap-stage
INNER_DOCKER_HOST="${NE_AIRGAP_DOCKER_HOST:-unix://${STATE_DIR}/run/docker.sock}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

echo "== netns-down: tearing down compose stacks (best-effort) =="
DOCKER_HOST="${INNER_DOCKER_HOST}" "${REPO_ROOT}/deploy/airgap/scripts/compose-ci.sh" \
  --profile core --profile brain --profile obs --profile cpu down --volumes || true

echo "== netns-down: stopping inner dockerd =="
if [ -f "${STATE_DIR}/run/dockerd.pid" ]; then
  sudo kill "$(cat "${STATE_DIR}/run/dockerd.pid")" || true
fi

echo "== netns-down: stopping the dedicated inner containerd =="
# The inner dockerd runs its OWN containerd (see netns-inner-dockerd.sh FIX 2).
# dockerd does not supervise an externally-addressed containerd, so it must be
# reaped explicitly — otherwise `ip netns del` below would orphan it.
if [ -f "${STATE_DIR}/run/containerd.pid" ]; then
  sudo kill "$(cat "${STATE_DIR}/run/containerd.pid")" || true
fi

echo "== netns-down: deleting the ${NS} namespace =="
sudo ip netns del "${NS}" || true

echo "== netns-down: removing per-namespace resolv.conf =="
sudo rm -f "/etc/netns/${NS}/resolv.conf" || true

echo "== netns-down: removing any leftover tagged staging iptables rules =="
# `-s 10.199.0.0/30` must match the exact rule spec netns-stage.sh added
# (kernel-normalized network form of 10.199.0.2/30) or -C never matches.
while sudo iptables -t nat -C POSTROUTING -s 10.199.0.0/30 -m comment --comment "${NAT_COMMENT}" -j MASQUERADE 2>/dev/null; do
  sudo iptables -t nat -D POSTROUTING -s 10.199.0.0/30 -m comment --comment "${NAT_COMMENT}" -j MASQUERADE || true
done
while sudo iptables -C FORWARD -m comment --comment "${NAT_COMMENT}" -j ACCEPT -s 10.199.0.0/30 2>/dev/null; do
  sudo iptables -D FORWARD -m comment --comment "${NAT_COMMENT}" -j ACCEPT -s 10.199.0.0/30 || true
done
while sudo iptables -C FORWARD -m comment --comment "${NAT_COMMENT}" -j ACCEPT -d 10.199.0.0/30 2>/dev/null; do
  sudo iptables -D FORWARD -m comment --comment "${NAT_COMMENT}" -j ACCEPT -d 10.199.0.0/30 || true
done

echo "== netns-down: done (best-effort) =="
