# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-brain sovereignty guards: in-VPC endpoint validation + AIR_GAP (NE-CKC2).

C2 (data sovereignty / egress): ne-brain's outbound HTTP clients
(:class:`ne.brain.embedder.TEIEmbedder`, :class:`ne.brain.reranker.TEIReranker`)
POST corpus text / queries to a *config-supplied* base URL. A compromised or
misauthored endpoint would exfiltrate the customer corpus. This module validates
each such endpoint at CONSTRUCTION time (SSRF shape + in-VPC/allowlist,
fail-closed): a bad endpoint raises :class:`UnsafeEndpointError` and never
receives a byte of corpus/query text.

Thin wrapper over the shared ne-net leaf (NE-CKC10)
---------------------------------------------------
The SSRF/in-VPC classification logic used to be a package-local mirror here (to
avoid ``import ne.gateway`` dragging ``litellm`` in — a C3 breach). NE-CKC10
consolidated all three mirrors into the stdlib-only leaf :mod:`ne.net.ssrf`:
:func:`validate_in_vpc_endpoint` now delegates there, passing this package's own
:class:`UnsafeEndpointError`. The leaf imports stdlib only (enforced by the
``ne-net-leaf`` import-linter contract), so C3-safety is preserved — no
``litellm`` / ``ne.gateway`` enters ne-brain's import graph.

This module keeps ONLY the ne-brain-local piece — the package-local
``UnsafeEndpointError`` — and RE-EXPORTS the shared AIR_GAP guard
(``AirGapViolationError``, ``air_gap_engaged``) from :mod:`ne.net.airgap` and
``normalize_host`` from :mod:`ne.net.ssrf`. The AIR_GAP re-export is deliberately
the SAME class object as ``ne.net.airgap.AirGapViolationError`` (NE-CKC10 / P2-23),
so every out-of-scope importer that still does ``from ne.brain.sovereignty import
AirGapViolationError`` and every cross-package ``isinstance`` check keeps working
unchanged.

Pure stdlib + ``ne.net`` (itself stdlib-only): no ``litellm``, no ``ne.gateway``
import (C3).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from urllib.parse import urlparse

import ne.net.ssrf as _net_ssrf

# Re-exported so out-of-scope importers keep working: AIR_GAP guard from the ONE
# home (ne.net.airgap — SAME class object) and normalize_host from ne.net.ssrf
# (imported from here by ne.connect.endpoints).
from ne.net.airgap import AirGapViolationError, air_gap_engaged
from ne.net.ssrf import normalize_host

__all__ = [
    "AirGapViolationError",
    "UnsafeEndpointError",
    "air_gap_engaged",
    "normalize_host",
    "validate_in_vpc_endpoint",
]


class UnsafeEndpointError(ValueError):
    """A config-supplied endpoint is SSRF-shaped or not provably in-VPC.

    Raised at CONSTRUCTION time (fail-closed) so a bad/exfil endpoint never
    receives a byte of corpus or query text. Messages interpolate ONLY the
    parsed hostname (never the full URL), so an embedded credential can never
    appear in an error message or log line.
    """


def validate_in_vpc_endpoint(
    endpoint: str, *, allowed_hosts: Sequence[str] = (), air_gap: bool = False
) -> str:
    """Validate a config-supplied in-VPC endpoint URL, fail-closed.

    Delegates to :func:`ne.net.ssrf.validate_in_vpc_endpoint` (the single shared
    classifier since NE-CKC10) with this package's :class:`UnsafeEndpointError`.

    **Phase A — SSRF shape (always rejected)**: non-http(s) scheme; a
    malformed/out-of-range port; credentials embedded in the URL; a query string
    or fragment; a missing hostname; a cloud-metadata host; a packed-integer IPv4
    form; an IP literal that is link-local / multicast / unspecified / non-loopback
    reserved.

    **Phase B — must be in-VPC or allowlisted**: a private/loopback IP literal
    (including IPv6 ``::1``, now handled exactly like ``127.0.0.1`` per NE-CKC10);
    ``localhost``; a dotless service name (not a packed IPv4); a host ending
    ``.local`` / ``.localhost`` / ``.internal``; or a host present in
    ``allowed_hosts`` (case-/trailing-dot-insensitive). Anything else raises
    :class:`UnsafeEndpointError`.

    **Phase B is dominated ENTIRELY by AIR_GAP**: with the air gap engaged
    (``air_gap`` OR the process-global ``AIR_GAP`` flag), a host admitted ONLY by
    ``allowed_hosts`` is REFUSED — a public IP literal and a DNS name alike. The
    platform rule is that under an air gap the operator allowlist contributes
    nothing and only provably in-VPC shapes pass (private/loopback IP,
    ``localhost``, a dotless service name, ``.local`` / ``.localhost`` /
    ``.internal``); those are untouched, because they need no grant in the first
    place. Its origin is :mod:`ne.storage.s3` ("an env var must not be able to
    punch a hole through the air gap"). See
    :func:`ne.net.ssrf.allowlist_revoked_by_air_gap` — the ONE implementation of
    the rule, shared with the per-request re-check in :mod:`ne.brain.reranker` —
    for why the rule is about SHAPE and not resolution, and for the tracked
    ne-gateway divergence it does not copy.

    Consequence for a sealed deployment: an in-VPC sidecar reached by a corporate
    FQDN outside those shapes will now fail LOUDLY at construction under an
    engaged gap. That is the intended trade — the alternative admitted egress
    while reporting the deployment sealed.

    Returns ``endpoint`` with any trailing ``/`` stripped. Every error message
    names ONLY the parsed hostname (never the full URL); the credentials-rejection
    message names no URL component at all, so an embedded secret can never leak.
    """
    return _net_ssrf.validate_in_vpc_endpoint(
        endpoint,
        allowed_hosts=allowed_hosts,
        air_gap=air_gap,
        error_cls=UnsafeEndpointError,
        external_hint=(
            "pass allowed_hosts=(...,) if it is private DNS reachable only inside the VPC"
        ),
    )


def air_gap_posture(air_gap: Callable[[], bool] | None) -> bool:
    """Resolve a caller-supplied runtime air-gap posture (absent ⇒ ``False``).

    The process-global ``AIR_GAP`` flag is deliberately NOT read here: it is read
    unconditionally inside :func:`ne.net.ssrf.allowlist_revoked_by_air_gap`, so
    this term can only ever ADD restriction, never remove it. A provider that
    raises is treated as engaged (fail-closed) — an unreadable posture must never
    open egress.
    """
    if air_gap is None:
        return False
    try:
        return bool(air_gap())
    except Exception:  # pragma: no cover - defensive; providers are fail-closed already
        return True


def assert_air_gap_permits(base_url: str, air_gap: Callable[[], bool] | None, *, what: str) -> None:
    """Re-assert the air-gap rule against an ALREADY-validated ``base_url``.

    Called before every outbound batch by ne-brain's two corpus-text clients
    (:class:`ne.brain.embedder.TEIEmbedder`, :mod:`ne.brain.reranker`'s two
    adapters). Construction-time validation cannot cover a governed air-gap flip
    that lands *after* the client is built, and that flip is exactly the beat the
    sealed-lane claim rests on.

    ONE implementation, called from every such client, because two clients that
    each carried their own copy of this assertion is how the reranker gained the
    per-request check and the embedder silently did not.

    ``what`` names the payload at risk (e.g. ``"corpus text"``) — a caller-supplied
    literal, never a value.

    This re-asserts the SAME rule as construction: under an engaged gap a host
    admitted only by the operator allowlist — IP literal or DNS name — is refused,
    while provably in-VPC shapes keep flowing. See
    :func:`ne.net.ssrf.allowlist_revoked_by_air_gap`.
    """
    host = normalize_host(urlparse(base_url).hostname or "")
    if _net_ssrf.allowlist_revoked_by_air_gap(host, air_gap=air_gap_posture(air_gap)):
        raise AirGapViolationError(
            f"AIR_GAP is engaged: refusing to send {what} to host {host!r} — it is "
            "not a provably in-VPC host, and under an air gap the operator "
            "allowlist that admitted it contributes nothing."
        )
