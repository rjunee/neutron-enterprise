#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# SEAL step for the NE-073 no-egress CI harness — the point of no return.
#
# Removes the staging veth path entirely (no default route, no interface to
# the root ns) and installs a belt-and-braces nftables counter-drop policy
# inside the namespace. After this script runs, no packet can leave
# `ne-airgap` to anything but loopback/private ranges reachable ONLY via
# in-namespace docker bridges — there is no route out at all.
#
# Everything after this script executes (install, phases A/B1/B2/C) runs with
# egress topologically impossible, not merely firewalled.

set -euo pipefail

NS=ne-airgap
HOST_VETH=ne-veth-host
NS_VETH=ne-veth-ns
NAT_COMMENT=ne-airgap-stage

echo "== netns-seal: removing default route + destroying the staging veth =="
sudo ip -n "${NS}" route del default || true
sudo ip -n "${NS}" link del "${NS_VETH}" || true
# Deleting either end of a veth pair destroys both; the host end may already
# be gone.
sudo ip link del "${HOST_VETH}" 2>/dev/null || true

echo "== netns-seal: removing the tagged staging NAT/FORWARD rules (root ns) =="
# NOTE: `iptables -C` needs the EXACT rule spec used at -A time (netns-stage.sh
# adds the MASQUERADE rule with `-s 10.199.0.2/30`, which the kernel stores in
# network form as 10.199.0.0/30) — omitting the -s match here would never
# match, silently leaving the NAT rule behind on reused runners.
while sudo iptables -t nat -C POSTROUTING -s 10.199.0.0/30 -m comment --comment "${NAT_COMMENT}" -j MASQUERADE 2>/dev/null; do
  sudo iptables -t nat -D POSTROUTING -s 10.199.0.0/30 -m comment --comment "${NAT_COMMENT}" -j MASQUERADE
done
while sudo iptables -C FORWARD -m comment --comment "${NAT_COMMENT}" -j ACCEPT -s 10.199.0.0/30 2>/dev/null; do
  sudo iptables -D FORWARD -m comment --comment "${NAT_COMMENT}" -j ACCEPT -s 10.199.0.0/30
done
while sudo iptables -C FORWARD -m comment --comment "${NAT_COMMENT}" -j ACCEPT -d 10.199.0.0/30 2>/dev/null; do
  sudo iptables -D FORWARD -m comment --comment "${NAT_COMMENT}" -j ACCEPT -d 10.199.0.0/30
done

echo "== netns-seal: installing belt-and-braces nftables counter-drop policy =="
sudo ip netns exec "${NS}" nft -f - <<'NFT'
table inet ne_airgap_seal {
  chain output {
    type filter hook output priority -100; policy accept;
    ip daddr { 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } accept
    ip6 daddr ::1 accept
    counter drop
  }
  chain forward {
    type filter hook forward priority -100; policy accept;
    ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } accept
    counter drop
  }
}
NFT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${SCRIPT_DIR}/netns-assert-sealed.sh"

echo "== netns-seal: ${NS} sealed — egress is topologically impossible =="
