# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Canonical-drift tripwire: ne-serve's mirror vs the real ne.gateway.ssrf.

ne-gateway is an ne-serve dev-group dependency, so this TESTS-ONLY import is
allowed; the RUNTIME import stays banned by
``test_import.py::test_import_pulls_no_provider_sdk`` (importing ne.gateway drags
litellm in — C3). This asserts the SUPERSET-REJECTION property: every SSRF-shaped
vector the canonical validator rejects, the ne-serve mirror ALSO rejects, and
both accept the benign in-VPC set.

The property is intentionally ONE-DIRECTIONAL: ne-serve's Phase B is STRICTER
(the endpoint must be in-VPC / allowlisted), whereas
``ne.gateway.ssrf.validate_endpoint_url`` with ``allow_private=True`` also admits
public hosts. So a vector accepted by the gateway is not required to be accepted
by the mirror — only the rejection set is a superset, and the shared benign set
is accepted by both.
"""

from __future__ import annotations

import pytest

import ne.gateway.errors as gw_errors
import ne.gateway.ssrf as gw_ssrf
from ne.serve.errors import UnsafeEndpointError
from ne.serve.sovereignty import validate_in_vpc_endpoint

# Every Phase-A hostile vector the mirror rejects; the gateway rejects them too.
_HOSTILE_VECTORS = [
    "ftp://vllm.local",
    "http://host:99999",
    "http://user:pass@vllm.local",
    "http://vllm.local/?k=v",
    "http://vllm.local/#f",
    "http://",
    "http://169.254.169.254",
    "http://metadata.google.internal",
    "http://169.254.10.10",
    "http://224.0.0.1",
    "http://0.0.0.0",
    "http://2852039166",
    "http://0xA9FEA9FE",
    "http://025177524776",
]

# Benign in-VPC endpoints both validators accept. NE-CKC10 adds IPv6 loopback
# '[::1]', which both validators must now accept (previously spuriously rejected).
_BENIGN_VECTORS = [
    "http://localhost:8090",
    "http://127.0.0.1:8090",
    "http://[::1]:8090",
    "http://10.0.0.5:8000",
    "http://vllm.local",
    "http://svc.internal",
]


@pytest.mark.parametrize("endpoint", _HOSTILE_VECTORS)
def test_gateway_rejection_is_mirrored(endpoint: str) -> None:
    # Confirm the canonical validator rejects (anchors the vector as truly hostile)...
    with pytest.raises(gw_errors.UnsafeEndpointError):
        gw_ssrf.validate_endpoint_url(endpoint, require_https=False, allow_private=True)
    # ...then confirm the ne-serve mirror rejects it too (superset property).
    with pytest.raises(UnsafeEndpointError):
        validate_in_vpc_endpoint(endpoint)


@pytest.mark.parametrize("endpoint", _BENIGN_VECTORS)
def test_benign_in_vpc_accepted_by_both(endpoint: str) -> None:
    gw_ssrf.validate_endpoint_url(endpoint, require_https=False, allow_private=True)
    validate_in_vpc_endpoint(endpoint)  # neither raises
