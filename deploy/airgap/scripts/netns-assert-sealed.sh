#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Fails (non-zero) unless the `ne-airgap` namespace is fully sealed. Run three
# times by the no-egress job: right after netns-seal.sh, again after the
# stack is brought up (proving bring-up did not reopen a route), and once
# more at job end (proving the whole run never reopened one).
#
# A sealed namespace means ALL of:
#   (a) no default route, v4 or v6;
#   (b) every interface is lo, docker0, a docker custom bridge (br-*), or a
#       veth whose `master` is one of those in-namespace bridges (i.e.
#       container plumbing — never a path back to the root ns);
#   (c) the belt-and-braces nftables table `inet ne_airgap_seal` exists with
#       both counter-drop rules (output + forward chains);
#   (d) the DNS resolver recorded for the namespace is unreachable (no route
#       to it) — the resolv.conf entry is a dead artifact, not a live path.

set -euo pipefail

NS=ne-airgap
FAIL=0

fail() {
  echo "SEAL VIOLATION: $1" >&2
  FAIL=1
}

echo "== netns-assert-sealed: checking ${NS} =="

# (a) no default route, v4 or v6.
v4_default="$(sudo ip netns exec "${NS}" ip route show default 2>/dev/null || true)"
if [ -n "${v4_default}" ]; then
  fail "IPv4 default route present: ${v4_default}"
fi
v6_default="$(sudo ip netns exec "${NS}" ip -6 route show default 2>/dev/null || true)"
if [ -n "${v6_default}" ]; then
  fail "IPv6 default route present: ${v6_default}"
fi

# (b) interface topology.
iface_lines="$(sudo ip netns exec "${NS}" ip -o link show 2>/dev/null || true)"
while IFS= read -r line; do
  [ -z "${line}" ] && continue
  # Format: "N: name@peer: <FLAGS> ... " or "N: name: <FLAGS> ..."
  raw_name="$(echo "${line}" | awk -F': ' '{print $2}')"
  name="${raw_name%%@*}"
  case "${name}" in
    lo|docker0)
      continue
      ;;
    br-*)
      continue
      ;;
    veth*)
      master="$(echo "${line}" | grep -oE 'master [^ ]+' | awk '{print $2}' || true)"
      if [ -z "${master}" ]; then
        fail "veth ${name} has no master bridge (dangling cross-boundary path?)"
        continue
      fi
      case "${master}" in
        docker0|br-*)
          continue
          ;;
        *)
          fail "veth ${name} has unexpected master ${master}"
          ;;
      esac
      ;;
    *)
      fail "unexpected interface ${name} present in ${NS} (only lo/docker0/br-*/veth-to-bridge allowed)"
      ;;
  esac
done <<<"${iface_lines}"

# (c) nftables seal table present with counter-drop in both chains.
nft_dump="$(sudo ip netns exec "${NS}" nft list table inet ne_airgap_seal 2>/dev/null || true)"
if [ -z "${nft_dump}" ]; then
  fail "nft table inet ne_airgap_seal is missing"
else
  output_drop_count="$(echo "${nft_dump}" | awk '/chain output/,/^\t}/' | grep -c 'counter.*drop' || true)"
  forward_drop_count="$(echo "${nft_dump}" | awk '/chain forward/,/^\t}/' | grep -c 'counter.*drop' || true)"
  if [ "${output_drop_count}" -lt 1 ]; then
    fail "nft chain output has no counter-drop rule"
  fi
  if [ "${forward_drop_count}" -lt 1 ]; then
    fail "nft chain forward has no counter-drop rule"
  fi
fi

# (d) the recorded DNS resolver is unreachable (no route to it) from in-ns.
RESOLV_CONF="/etc/netns/${NS}/resolv.conf"
if [ -f "${RESOLV_CONF}" ]; then
  ns_dns="$(awk '/^nameserver/{print $2; exit}' "${RESOLV_CONF}")"
  if [ -n "${ns_dns}" ]; then
    if sudo ip netns exec "${NS}" ip route get "${ns_dns}" >/dev/null 2>&1; then
      fail "a route to the recorded DNS resolver ${ns_dns} still exists"
    fi
  fi
fi

if [ "${FAIL}" -ne 0 ]; then
  echo "== netns-assert-sealed: SEALED CHECK FAILED — egress may be possible ==" >&2
  exit 1
fi

echo "== netns-assert-sealed: ${NS} is sealed (no route, topology closed, nft counters present) =="
