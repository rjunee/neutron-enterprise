# ne-net

Stdlib-only network-security **leaf** package. Home of the ONE consolidated
SSRF / sovereignty endpoint validator **and** the ONE `AIR_GAP` egress guard,
shared across the platform (NE-CKC10 / P2-23).

## Why this package exists

The endpoint validator that refuses SSRF-shaped operator/config-supplied URLs
(cloud-metadata IPs, packed-integer IPv4 bypasses, link-local / multicast /
reserved / unspecified literals, embedded credentials, query/fragment) — plus the
`AIR_GAP` env read and `AirGapViolationError` type — used to exist in **seven+
near-duplicate copies** (`ne.gateway.ssrf`, `ne.brain.sovereignty`,
`ne.serve.sovereignty`, `ne.permissions.sovereignty`,
`ne.permissions_rebac.sovereignty`, `ne.connect.endpoints`,
`ne.memory_graph.config`, and the `_AIR_GAP_TRUE` copies in `ne.gateway.egress` /
both ne-chat clients).

Duplicated security code drifts. NE-CKC10 collapses the classifier and the
AIR_GAP guard into this single package; every former copy is now a thin
construction-time wrapper that delegates here and keeps only its package-local
exception type (passed via `error_cls`) and, for the gateway lane, the
log-redactor (passed via `describe`).

## Primitives

- `ne.net.ssrf.validate_endpoint_url` — gateway lane (require_https + allow_private knobs).
- `ne.net.ssrf.validate_in_vpc_endpoint` — in-VPC sidecar lane (private/loopback/dotless/`.internal`/allowlist).
- `ne.net.ssrf.validate_external_https_url` — external lane: https-only, DNS-name-only, optional **exact** allowlist (`None` ⇒ any DNS name).
- `ne.net.ssrf.validate_loopback_endpoint` — strict loopback lane: loopback IP / `localhost` / allowlist only (rejects private IPs and dotless names).
- `ne.net.airgap.air_gap_engaged` / `ne.net.airgap.AirGapViolationError` — the single fail-closed AIR_GAP guard.

## Why not in `ne-gateway`?

`ne.gateway` is the classifier's historical home, but it cannot host the shared
util: `import ne.gateway` executes `ne/gateway/__init__.py`, which drags
`litellm` into the import graph. `ne-brain` and `ne-serve` must **not** couple to
a model-provider SDK (constraint C3, NE-CKC2). So the shared validator lives here,
in a leaf with **zero third-party dependencies** (pure stdlib `ipaddress` /
`urllib.parse`).

The leaf-ness is enforced two ways: the `ne-net-leaf` import-linter contract
(`ne.net` may import no other `ne.*` package and no `httpx`/`requests`), and a
runtime subprocess guard in `tests/test_import.py` (`import ne.net` pulls no
`litellm`/`openai`/`httpx`/`ne.gateway`/`ne.brain`/`ne.serve`).

## The IPv6 `::1` fix

On CPython, `IPv6Address('::1').is_reserved` is `True` while
`IPv4Address('127.0.0.1').is_reserved` is `False`. The old unconditional
`is_link_local or is_multicast or is_reserved or is_unspecified` hard-reject
therefore fired on `::1` before any loopback handling — IPv6 loopback was
spuriously refused while IPv4 loopback flowed to the loopback policy. The reserved
test is now exempted for loopback (`is_reserved and not is_loopback`), so `::1`
follows the same loopback policy as `127.0.0.1`. This changes `::1` and nothing
else — `::`, `fe80::/10`, multicast, and `64:ff9b::/96` all stay rejected.

## Consumers

| Consumer | Delegates to |
| --- | --- |
| `ne.gateway.ssrf` (re-export) + `ne.gateway.egress` | `validate_endpoint_url` |
| `ne.brain.sovereignty.validate_in_vpc_endpoint` | `validate_in_vpc_endpoint` |
| `ne.serve.sovereignty.validate_in_vpc_endpoint` | `validate_in_vpc_endpoint` |
| `ne.connect.endpoints.validate_source_url` | `validate_in_vpc_endpoint` + `validate_external_https_url` |
| `ne.permissions.sovereignty.validate_external_https_url` | `validate_external_https_url` |
| `ne.permissions_rebac.sovereignty.validate_openfga_url` | `validate_in_vpc_endpoint` |
| `ne.memory_graph.config.require_loopback_or_allowlisted` | `validate_loopback_endpoint` |
| `ne.brain.sovereignty` / `ne.permissions*` / `ne.connect` / `ne.chat` / `ne.gateway.egress` / `ne.govern` / `ne.eval` / `ne.memory_graph` / `ne.brain_graph` / `ne.schema.codegen` | `ne.net.airgap.air_gap_engaged` |

`ne.gateway.errors.AirGapViolationError` is **deliberately distinct** (an
`EgressBlockedError` subclass) and is NOT `ne.net.airgap.AirGapViolationError`.

## Zero-dependency invariant

`ne-net` declares `dependencies = []` (constraint C4). The shared red-team corpus
in `ne.net.testing` is parametrized into every consumer's test suite plus
`ne-net`'s own, proving they reject the identical bypass set.
