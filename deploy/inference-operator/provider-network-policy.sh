#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

namespace=neutron-inference-provider
host_link=ne-provider-host
namespace_link=ne-provider-net
runtime=/run/neutron/inference-provider
receipt="$runtime/egress-receipt.json"
hosts_file="$runtime/hosts"
policy=/opt/neutron/inference-operator/provider-egress-policy.json
policy_sha=8f06a75d0c58f058d6b12293c4233bf2d05a10f009377278bbc6af4bcb7ca06d
provider=console.vast.ai
provider_gid=10002
operator_gid=10001

[[ "$(id -u)" == 0 ]] || exit 70
teardown() {
  ip netns del "$namespace" 2>/dev/null || true
  ip link del "$host_link" 2>/dev/null || true
  nft delete table inet ne_inference_provider_host 2>/dev/null || true
  rm -f -- "$receipt" "$hosts_file"
}

if [[ "${1:-}" == teardown ]]; then
  teardown
  exit 0
fi
[[ "$#" == 0 ]] || exit 64
if ! /usr/local/bin/ne-inference-provider-air-gap-condition; then
  teardown
  exit 77
fi
[[ "$(sha256sum "$policy" | cut -d' ' -f1)" == "$policy_sha" ]] || exit 70

mapfile -t addresses < <(
  { getent ahostsv4 "$provider" || true; } \
    | awk '{print $1}' | sort -u | head -n 16
)
[[ "${#addresses[@]}" -ge 1 ]] || exit 69
for address in "${addresses[@]}"; do
  [[ "$address" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || exit 70
done

teardown
install -d -o root -g "$operator_gid" -m 750 "$runtime"
ip netns add "$namespace"
ip link add "$host_link" type veth peer name "$namespace_link"
ip link set "$namespace_link" netns "$namespace"
ip address add 169.254.77.1/30 dev "$host_link"
ip link set "$host_link" up
ip -n "$namespace" address add 169.254.77.2/30 dev "$namespace_link"
ip -n "$namespace" link set lo up
ip -n "$namespace" link set "$namespace_link" up
ip -n "$namespace" route add default via 169.254.77.1

outbound_interface="$(ip route show default | awk 'NR==1 {print $5}')"
[[ "$outbound_interface" =~ ^[a-zA-Z0-9_.:-]+$ ]] || { teardown; exit 70; }
[[ "$(cat /proc/sys/net/ipv4/ip_forward)" == 1 ]] || { teardown; exit 69; }
nft -f - <<EOF
table inet ne_inference_provider_host {
  chain forward {
    type filter hook forward priority filter; policy accept;
    iifname "$host_link" oifname "$outbound_interface" accept
    iifname "$outbound_interface" oifname "$host_link" ct state established,related accept
  }
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr 169.254.77.2 oifname "$outbound_interface" masquerade
  }
}
EOF

provider_set="$(IFS=,; echo "${addresses[*]}")"
ip netns exec "$namespace" nft -f - <<EOF
table inet ne_inference_provider {
  set provider_v4 { type ipv4_addr; elements = { $provider_set }; }
  set ssh_grants_v4 { type ipv4_addr . inet_service; flags timeout; timeout 15m; }
  chain input {
    type filter hook input priority filter; policy drop;
    iifname "lo" accept
    ct state established,related accept
  }
  chain output {
    type filter hook output priority filter; policy drop;
    oifname "lo" accept
    ct state established,related accept
    ip daddr @provider_v4 tcp dport 443 accept
    ip daddr . tcp dport @ssh_grants_v4 accept
  }
}
EOF

{
  printf '127.0.0.1 localhost\n'
  for address in "${addresses[@]}"; do
    printf '%s %s\n' "$address" "$provider"
  done
} > "$hosts_file"
chown root:"$provider_gid" "$hosts_file"
chmod 640 "$hosts_file"

namespace_inode="$(stat -Lc '%i' "/run/netns/$namespace")"
/usr/bin/python3 - "$receipt" "$namespace_inode" "$policy_sha" "${addresses[@]}" <<'PY'
import json, os, sys, tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

destination = Path(sys.argv[1])
document = {
    "addresses": sorted(sys.argv[4:]),
    "authority": "console.vast.ai:443",
    "expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
    "netns_inode": int(sys.argv[2]),
    "policy_sha256": sys.argv[3],
    "schema": "ne-inference-provider-egress-v1",
}
payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
descriptor, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
try:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        os.fchown(handle.fileno(), 0, 10002)
    os.chmod(temporary, 0o640)
    os.replace(temporary, destination)
    directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
