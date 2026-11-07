#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# One endpoint formatter for every rentable GPU provider.  Lifecycle adapters
# still use each provider's public address for SSH/control-plane operations, but
# the model endpoint handed to Neutron must be an already-working private
# overlay address.  There is deliberately no DNS or public-address fallback.

private_rig_endpoint_host() {
  local raw="${NE_RIG_PRIVATE_HOST:-}" normalized
  [ -n "$raw" ] || die "NE_RIG_PRIVATE_HOST is required for 'endpoint'. Set it to the rig's RFC1918/ULA address on an already-working private overlay (for example WireGuard). The rig scripts do not create that overlay, and never fall back to the provider's public host."
  normalized="$(python3 - "$raw" <<'PY'
import ipaddress
import sys

raw = sys.argv[1]
try:
    address = ipaddress.ip_address(raw)
except ValueError:
    raise SystemExit(1)

private_v4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
private_v6 = ipaddress.ip_network("fc00::/7")
allowed = (
    address.version == 4 and any(address in network for network in private_v4)
) or (address.version == 6 and address in private_v6)
if not allowed:
    raise SystemExit(1)

value = address.compressed
print(f"[{value}]" if address.version == 6 else value)
PY
)" || die "NE_RIG_PRIVATE_HOST must be an RFC1918 IPv4 or ULA IPv6 literal on the rig's private overlay; public IPs, DNS names, loopback, link-local, and special-use addresses are refused."
  printf '%s' "$normalized"
}

private_rig_endpoint_url() {  # internal-port, suffix (/v1 or empty)
  local internal="$1" suffix="${2:-}" host
  host="$(private_rig_endpoint_host)" || return 1
  printf 'http://%s:%s%s\n' "$host" "$internal" "$suffix"
}
