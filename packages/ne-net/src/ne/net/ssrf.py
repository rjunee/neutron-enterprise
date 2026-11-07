# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The single consolidated SSRF / sovereignty endpoint validator (NE-CKC10).

Derived, via ``ne.gateway.ssrf`` (NE-012), from ABI
(https://github.com/jupyter-naas/abi) provider_runtime.py endpoint validation.
MIT License, Copyright (C) 2025 Naas.ai.

Before NE-CKC10 this classifier existed in SEVEN near-duplicate copies
(``ne.gateway.ssrf``, ``ne.brain.sovereignty``, ``ne.serve.sovereignty``,
``ne.permissions.sovereignty``, ``ne.permissions_rebac.sovereignty``,
``ne.connect.endpoints``, ``ne.memory_graph.config``) — a security-code drift
hazard. This module is the ONE home; every former copy is now a thin
construction-time wrapper that delegates here and keeps only its package-local
exception type (passed in via ``error_cls``) and, for the gateway lane, the
log-redaction shim (passed in via ``describe``). The lane-specific primitives are:
:func:`validate_endpoint_url` (gateway), :func:`validate_in_vpc_endpoint`
(ne-brain / ne-serve / ne-connect / ne-permissions-rebac in-VPC sidecars),
:func:`validate_external_https_url` (ne-permissions Graph/login + ne-connect
external sources — https-only, DNS-only, optional exact allowlist), and
:func:`validate_loopback_endpoint` (ne-memory-graph sidecar — strict loopback /
localhost / allowlist only). The AIR_GAP guard lives alongside in
:mod:`ne.net.airgap`.

It CANNOT live in ``ne.gateway``: ``import ne.gateway`` executes
``ne/gateway/__init__.py`` which drags ``litellm`` into the import graph, and
ne-brain / ne-serve must not couple to a model provider SDK (C3, NE-CKC2). So
the shared validator lives in this stdlib-only leaf package ``ne-net``, whose
leaf-ness (no first-party / provider imports) is enforced by the ``ne-net-leaf``
import-linter contract and a runtime subprocess guard.

The IPv6 ``::1`` fix (NE-CKC7 / NE-CKC2 follow-up)
--------------------------------------------------
On CPython (verified 3.13.5) ``IPv6Address('::1').is_reserved`` is ``True`` while
``IPv4Address('127.0.0.1').is_reserved`` is ``False``. The old unconditional
``is_link_local or is_multicast or is_reserved or is_unspecified`` hard-reject
therefore fired on ``::1`` in Phase A — before any loopback handling — so IPv6
loopback was spuriously refused while IPv4 loopback flowed to the loopback
policy. The reserved test is now exempted for loopback
(``ip.is_reserved and not ip.is_loopback``), so ``::1`` falls through to each
caller's loopback policy exactly like ``127.0.0.1``. This changes ``::1`` and
nothing else: ``::`` (unspecified), ``fe80::/10`` (link-local), multicast, and
``64:ff9b::/96`` (reserved, not loopback) all stay rejected.

Pure stdlib (``ipaddress``, ``urllib.parse``): no ``litellm`` / provider import.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from ne.net.airgap import air_gap_engaged

_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal"}
"""Cloud instance-metadata endpoints — rejected unconditionally."""

_LOCAL_SUFFIXES = (".local", ".localhost", ".internal")
"""Hostname suffixes treated as private/in-cluster DNS."""

_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}
"""Bare loopback hostnames rejected on the gateway lane when ``allow_private=False``."""

_USERINFO_RE = re.compile(r"[^/@:]+(:[^/@]*)?@")
"""Userinfo-shaped token in the pre-path portion of a URL: ``user@`` or
``user:pass@``. The char class excludes ``/`` so a match can never cross a path
segment. Used by :func:`redact_url_for_logs` to mask credentials in URLs that
``urllib`` does not parse into a ``//netloc`` (see that function)."""

_SENSITIVE_QUERY_KEYS = {
    "token",
    "api_key",
    "key",
    "access_token",
    "auth",
    "authorization",
}
"""Query-string keys whose values are masked by :func:`redact_url_for_logs`."""


def redact_url_for_logs(url: str) -> str:
    """Return ``url`` safe to emit into logs/telemetry.

    Lives HERE, in the stdlib-only leaf, rather than in ``ne.gateway.ssrf``
    where it was written (NE-012). Reason (NE-AUDIT-CRON P2-1): importing the
    submodule ``ne.gateway.ssrf`` first executes ``ne/gateway/__init__.py``,
    which eagerly imports ``ne.gateway.gateway`` and therefore the whole
    provider-SDK closure (``litellm``). Any non-gateway process that wanted only
    this ~40-line ``re``/``urllib`` redactor paid +140MB RSS and +5.6s of boot
    for it — ne-worker's audit-checkpoint loop did exactly that, on EVERY worker,
    even with the loop default-off. The function has zero gateway coupling, so it
    belongs in the leaf; ``ne.gateway.ssrf`` re-exports it and every existing
    importer is unchanged. The ``ne-net-leaf`` import-linter contract now keeps
    the redactor permanently free of first-party/provider imports.

    Two independent maskings are applied:

    * **netloc userinfo** (NE-012 P2 fold via NE-014) — embedded credentials are
      masked unconditionally: ``https://user:pass@host`` renders as
      ``https://***:***@host``, and a bearer-style ``https://token@host`` (a
      username with no password) renders as ``https://***@host``, so a
      misconfigured ``api_base`` never leaks a cleartext credential into an SSRF
      error message or a log line. A malformed/out-of-range port in the input is
      tolerated (dropped) — this helper is total and never raises.
    * **sensitive query-key values** — replaced by ``REDACTED`` (ABI logic).
    * **non-//netloc userinfo** — ``urllib`` populates ``parsed.username`` /
      ``parsed.password`` ONLY for ``//netloc`` URLs, so a scheme-less
      (``user:pass@host/v1`` parses ``scheme='user'``, ``netloc=''``) or
      single-slash (``https:/user:pass@host`` parses ``netloc=''``,
      ``path='/user:pass@host'``) credentialed endpoint would otherwise echo its
      cleartext credential. When no netloc parses but an ``@`` is present, the raw
      string is pre-masked with :data:`_USERINFO_RE` (``user:pass@`` →
      ``***:***@``, ``token@`` → ``***@``) before the netloc logic runs. This is a
      fail-closed logging redactor: over-masking an email-like token in prose is
      accepted; under-masking a credential is not.

    For a URL with neither embedded userinfo nor a sensitive query key the input
    is returned unchanged (identity).
    """
    parsed = urlparse(url)

    if parsed.username is None and parsed.password is None and not parsed.netloc and "@" in url:
        # No //netloc parsed, so parsed.username/password are both None even for a
        # credentialed URL. Pre-mask userinfo-shaped tokens in the RAW string, then
        # re-parse so the identity/return logic below operates on the masked form.
        url = _USERINFO_RE.sub(lambda m: "***:***@" if m.group(1) else "***@", url)
        parsed = urlparse(url)

    if parsed.username is not None or parsed.password is not None:
        host = parsed.hostname or ""
        if ":" in host:  # IPv6 literal needs brackets inside a netloc
            host = f"[{host}]"
        # Malformed/out-of-range port (e.g. ':99999' or ':abc'): redaction is a
        # logging helper and MUST be total — drop the port rather than raise.
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is not None:
            host = f"{host}:{port}"
        userinfo = "***:***" if parsed.password is not None else "***"
        netloc = f"{userinfo}@{host}"
    else:
        netloc = parsed.netloc

    query = parsed.query
    if parsed.query:
        redacted_pairs = [
            (k, "REDACTED" if k.lower() in _SENSITIVE_QUERY_KEYS else v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        query = urlencode(redacted_pairs)

    if netloc == parsed.netloc and query == parsed.query:
        return url
    return urlunparse(parsed._replace(netloc=netloc, query=query))


class UnsafeEndpointError(ValueError):
    """Default error type for the shared validator.

    Every consumer passes its own package-local exception via ``error_cls`` so
    the historical per-package exception types are preserved exactly; this class
    is the fallback used by the validator's own tests / direct callers.
    """


def normalize_host(host: str) -> str:
    """Canonicalize a hostname for comparison: lowercase, trailing dot removed."""
    return host.lower().rstrip(".")


def looks_like_packed_ipv4(host: str) -> bool:
    """True if a dotless host is a packed-integer IPv4 form a libc resolver
    (``inet_aton``) would map to a real address, e.g. ``'2852039166'`` (decimal),
    ``'0xA9FEA9FE'`` (hex), or ``'025177524776'`` (octal) — all == 169.254.169.254.

    ``ipaddress.ip_address`` rejects these strings, so they slip past the IP-literal
    check and would otherwise reach a dotless service-name heuristic and be mistaken
    for an in-VPC compose service. They must NOT get the in-VPC shortcut (fail-closed;
    blocks metadata SSRF).
    """
    lowered = host.lower()
    if lowered.startswith("0x"):
        rest = lowered[2:]
        return bool(rest) and all(c in "0123456789abcdef" for c in rest)
    # Pure digits cover both decimal ('2852039166') and octal ('0251...') forms —
    # both are inet_aton-parseable packed integers, never a real DNS label.
    return host.isdigit()


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _is_private_or_local_ip(host: str) -> bool:
    """True if ``host`` is an IP literal in a private / loopback range."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def _is_public_ip_literal(host: str) -> bool:
    """True if ``host`` is an IP literal that is neither private nor loopback.

    ``host`` must already have survived Phase A, so link-local / multicast /
    unspecified / non-loopback-reserved literals cannot reach here.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback)


def provably_in_vpc_shape(host: str) -> bool:
    """True when ``host``'s SHAPE alone proves it is inside the perimeter.

    These are the hosts :func:`validate_in_vpc_endpoint` admits with NO allowlist
    entry at all: a private / loopback IP literal, ``localhost``, a dotless
    compose/service name (packed-integer IPv4 forms excluded — they are real,
    possibly metadata, addresses), or a ``.local`` / ``.localhost`` / ``.internal``
    name. No operator declaration is needed to trust these, and none is revoked by
    an air gap, which restricts EGRESS and not in-VPC traffic.

    RESIDUAL — "provably" is a claim about SHAPE, not about resolution
    ------------------------------------------------------------------
    The name is honest about the classifier, not about the network. Two of these
    shapes can be made to resolve outside the perimeter by a host's resolver
    configuration, and this function cannot see that:

    * a **dotless** name is expanded through the resolver's ``search`` domains, so
      ``http://rerank`` can resolve as ``rerank.<search-domain>`` at a public
      address. This is how every compose/Kubernetes deployment addresses its
      sidecars (``http://tei-rerank``, ``http://qdrant``), which is why it is
      trusted — and revoking it under an air gap would kill the sealed on-box lane
      an air gap exists to protect;
    * ``.internal`` / ``.local`` / ``.localhost`` are reserved namespaces, but a
      split-horizon resolver can still map them anywhere.

    Both predate this predicate: ``validate_in_vpc_endpoint`` has always accepted
    these shapes, and ``ne.gateway.egress.EgressPolicy._is_in_vpc`` carries the
    identical dotless branch. Neither is closed by validating harder here —
    ``httpx`` resolves independently at connect time, so a check-then-connect gap
    (DNS rebinding) survives regardless. Closing this class needs the connection
    PINNED to a validated address (a custom resolver/transport), platform-wide,
    or a network-enforced egress boundary underneath the process. Until then a
    software air gap is a strong default, not a proof, and this docstring is the
    place that says so rather than implying otherwise.
    """
    return (
        _is_private_or_local_ip(host)
        or host == "localhost"
        or ("." not in host and not looks_like_packed_ipv4(host))
        or host.endswith(_LOCAL_SUFFIXES)
    )


def allowlist_revoked_by_air_gap(host: str, *, air_gap: bool = False) -> bool:
    """True when an engaged air gap REVOKES an explicit ``allowed_hosts`` grant.

    **The platform rule: under an engaged air gap the operator allowlist
    contributes NOTHING. Only provably in-VPC shapes pass.** A host admitted
    solely because someone listed it — a public IP literal OR a DNS name — is
    refused; a host whose shape already proves it in-VPC
    (:func:`provably_in_vpc_shape`) is untouched, because it would have passed
    without the allowlist and an air gap restricts egress, not in-VPC traffic.

    Origin: :mod:`ne.storage.s3`, which is where this platform first wrote the
    rule down and gave its reason — "an env var must not be able to punch a hole
    through the air gap" — by discarding its allowlist entirely under the gap
    (``allowed = []``). This predicate is that same rule expressed as a function
    so the in-VPC lane enforces it too.

    Why shape and not resolution
    ----------------------------
    The tempting alternative — resolve an allowlisted name and revoke it if it
    lands on a public address — is the wrong instrument, and the reasoning
    matters because an earlier revision of this function used it to justify
    ADMITTING names:

    * resolving does not close the hole it appears to close. An independent
      resolution happens inside ``httpx`` at connect time, so a check-then-connect
      gap (DNS rebinding) survives any amount of validating here;
    * it would put a blocking ``getaddrinfo`` on a per-request hot path
      (:func:`ne.brain.sovereignty.assert_air_gap_permits` runs before every
      outbound batch, inside async client code), and then have to choose between
      failing open when resolution fails (useless) and failing closed (which is
      what this rule does anyway, without the syscall).

    Every one of those objections is an argument against *resolving*. None is an
    argument for *admitting*. Revocation needs no resolution at all: it is a pure
    shape rule, strictly simpler than the alternative it replaces.

    Why a name is not trusted on the operator's word
    ------------------------------------------------
    Because the resulting control is defeated by the *spelling* of a string. The
    same server refused as ``8.8.8.8`` was admitted as ``dns.google``, and
    ``deploy/inference-operator/vast-rig.sh`` derives its host as ``public_ipaddr or ssh_host``
    — the fallback is a DNS name, so an honest operator hit this by accident, not
    only an adversary. A control that turns on lexical form is not a boundary.
    The cost of closing it is a sealed deployment whose in-VPC sidecar carries a
    corporate FQDN outside the ``.internal`` / ``.local`` / dotless / private-IP
    shapes: it now fails LOUDLY at construction, with a remedy in the message,
    rather than the gap silently admitting egress while the console reports the
    deployment sealed. ``ne.storage.s3`` already made that trade for object
    storage before this function existed.

    KNOWN DIVERGENCES, named rather than mirrored — "the platform rule" describes
    what THIS predicate enforces, not yet a property every lane has:

    * ne-gateway's ``EgressPolicy._is_in_vpc`` (``egress.py``, trailing
      ``return host in settings.allowed_private_vllm_hosts``) still admits an
      allowlisted DNS name under ``AIR_GAP``, on the private-vllm lane;
    * ``ne.storage.azure_blob`` returns EARLY for a public Azure suffix that is
      allowlisted, so such a host never reaches this predicate under the gap —
      a deliberate, documented "an Azure Private Endpoint resolves it privately
      in-VNet" assertion.

    Both are pre-existing and both are weaker than the rule here. They are tracked
    to their own units; this function does not copy a divergence forward, and no
    caller should read "the platform rule" as meaning those lanes already enforce
    it.

    Air gap is read from BOTH terms (as the gateway reads it,
    ``settings.air_gap or _air_gap_env()``): the caller-supplied ``air_gap`` — a
    runtime posture the caller owns, e.g. ne-api's governed egress register — OR
    the bare process-global ``AIR_GAP`` flag. The env term is consulted
    unconditionally so a caller that forgets to pass its posture still fails
    closed; ``air_gap`` can only ever ADD restriction.

    This is the single implementation of the rule. :func:`validate_in_vpc_endpoint`
    calls it at construction time; a caller that holds a long-lived validated
    endpoint (the rerank adapters, whose air gap can be engaged AFTER
    construction by a governed runtime flip) calls it again before each request.
    """
    return (air_gap or air_gap_engaged()) and not provably_in_vpc_shape(host)


def _reject_ssrf_shape(
    endpoint: str,
    *,
    require_https: bool,
    error_cls: type[Exception],
    describe: Callable[[str], str] | None,
) -> str:
    """Run the shared Phase-A SSRF-shape rejections and return the normalized host.

    Order (identical across all consumers): scheme; https-required (only when
    ``require_https``); malformed/out-of-range port; embedded credentials;
    query/fragment; missing hostname; cloud-metadata host; packed-integer IPv4;
    hard IP-class reject (link-local / multicast / unspecified / reserved, with
    loopback exempted from ONLY the reserved test — the ``::1`` fix).

    ``describe`` (when not ``None``) is the gateway lane's log-redactor: the
    message is prefixed with ``endpoint '<redacted-url>': ``. When ``describe`` is
    ``None`` the message names AT MOST the parsed hostname / scheme — NEVER the
    URL and NEVER userinfo — so an embedded credential can never leak into a log.
    """
    prefix = f"endpoint {describe(endpoint)!r}: " if describe is not None else ""
    parsed = urlparse(endpoint)

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise error_cls(f"{prefix}scheme {scheme!r} is not permitted (only http/https)")
    if require_https and scheme != "https":
        raise error_cls(f"{prefix}https is required for this lane, got http")

    try:
        _ = parsed.port
    except ValueError:
        raise error_cls(f"{prefix}malformed or out-of-range port") from None

    if parsed.username or parsed.password:
        raise error_cls(f"{prefix}credentials embedded in the URL are not permitted")
    if parsed.query or parsed.fragment:
        raise error_cls(f"{prefix}query string / fragment is not permitted on an endpoint URL")

    hostname = parsed.hostname
    if not hostname:
        raise error_cls(f"{prefix}missing hostname")

    host = normalize_host(hostname)

    if host in _METADATA_HOSTS:
        raise error_cls(f"{prefix}cloud-metadata host {host!r} is never permitted")

    if looks_like_packed_ipv4(host):
        raise error_cls(
            f"{prefix}host {host!r} is a packed-integer IPv4 form (inet_aton-resolvable, "
            "e.g. 2852039166 == 169.254.169.254) and is never permitted"
        )

    if _is_ip_literal(host):
        ip = ipaddress.ip_address(host)
        # The ::1 fix: loopback is exempted from ONLY the reserved test, so IPv6
        # loopback stops being double-classified (is_reserved is True for ::1 on
        # CPython) and instead flows to the caller's loopback policy — matching
        # 127.0.0.1 exactly. :: / fe80::/10 / multicast / 64:ff9b::/96 stay rejected.
        if (
            ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or (ip.is_reserved and not ip.is_loopback)
        ):
            raise error_cls(
                f"{prefix}IP {host!r} is link-local / multicast / reserved / unspecified "
                "and is never permitted (blocks the instance-metadata range even in-VPC)"
            )

    return host


def validate_endpoint_url(
    endpoint: str,
    *,
    require_https: bool,
    allow_private: bool,
    error_cls: type[Exception] = UnsafeEndpointError,
    describe: Callable[[str], str] | None = None,
) -> str:
    """Gateway-lane validator: SSRF shape, then (unless ``allow_private``) reject
    private/loopback ranges, bare loopback hostnames, and private-DNS suffixes.

    Rejected ALWAYS (regardless of ``allow_private``): non-http(s) scheme; http
    when ``require_https``; malformed/out-of-range port; missing hostname;
    embedded credentials; query string / fragment; cloud-metadata host;
    packed-integer IPv4 form; link-local / multicast / unspecified / non-loopback
    reserved IP literals.

    Rejected when ``allow_private=False`` (additionally): private-range / loopback
    IP literals (now including IPv6 ``::1``, handled exactly like ``127.0.0.1``);
    ``localhost`` / ``127.0.0.1`` / ``::1``; hostnames ending ``.local`` /
    ``.localhost`` / ``.internal``.

    Returns the endpoint with any trailing ``/`` stripped.
    """
    prefix = f"endpoint {describe(endpoint)!r}: " if describe is not None else ""
    host = _reject_ssrf_shape(
        endpoint, require_https=require_https, error_cls=error_cls, describe=describe
    )

    if not allow_private:
        if _is_private_or_local_ip(host):
            raise error_cls(f"{prefix}private/loopback IP {host!r} is not permitted on this lane")
        if host in _LOCAL_HOSTNAMES:
            raise error_cls(f"{prefix}loopback host {host!r} is not permitted on this lane")
        if host.endswith(_LOCAL_SUFFIXES):
            raise error_cls(f"{prefix}private-DNS host {host!r} is not permitted on this lane")

    return endpoint.rstrip("/")


def validate_in_vpc_endpoint(
    endpoint: str,
    *,
    allowed_hosts: Sequence[str] = (),
    air_gap: bool = False,
    error_cls: type[Exception] = UnsafeEndpointError,
    external_hint: str = (
        "pass allowed_hosts=(...,) if it is private DNS reachable only inside the VPC"
    ),
) -> str:
    """In-VPC-lane validator (ne-brain / ne-serve): SSRF shape, then ACCEPT only a
    provably-in-VPC or explicitly allowlisted host.

    Phase A is :func:`_reject_ssrf_shape` (``require_https=False``, no redactor —
    messages name at most the parsed hostname, never the URL). Phase B accepts: a
    host in ``allowed_hosts`` (case- and trailing-dot-normalized — a DNS name OR
    an IP literal; checked FIRST, and only AFTER Phase A, so the metadata range,
    packed-IPv4 forms, and link-local/multicast/reserved literals can never be
    allowlisted back in); a private / loopback IP literal (now including ``::1``);
    ``localhost``; a dotless service name (packed IPv4 already excluded in Phase
    A); or a host ending ``.local`` / ``.localhost`` / ``.internal``. A
    non-allowlisted, non-private, non-loopback IP literal is rejected as
    not-in-VPC; anything else is treated as external.

    **AIR_GAP dominates the allowlist entirely.** With the air gap engaged (the
    caller's ``air_gap`` term OR the process-global ``AIR_GAP`` flag), a host
    admitted ONLY by ``allowed_hosts`` is refused — a public IP literal and a DNS
    name alike. A host whose shape already proves it in-VPC is unaffected, since
    it would have been admitted without the allowlist. See
    :func:`allowlist_revoked_by_air_gap` for the rule, its origin in
    :mod:`ne.storage.s3`, and the ne-gateway divergence it deliberately does not
    copy. With no air gap engaged this parameter changes nothing.

    (NE-SEARCH-LATENCY closed the IP-literal gap: this function's own contract —
    and every consumer docstring mirroring it — always read "or a host present
    in ``allowed_hosts``", but the implementation consulted the allowlist only
    for DNS names, silently un-allowlisting a public IP literal such as a rented
    demo-GPU rerank endpoint. The allowlist is opt-in and empty by default, so
    the fail-closed default is byte-identical. The follow-up fix added the
    air-gap dominance above: making the allowlist reach IP literals also made it
    an egress path a sealed deployment had no way to shut, which is the one line
    of the gateway precedent the original "mirrors allowed_private_vllm_hosts"
    prose did not actually mirror.)

    Returns the endpoint with any trailing ``/`` stripped.
    """
    host = _reject_ssrf_shape(endpoint, require_https=False, error_cls=error_cls, describe=None)

    if host in {normalize_host(h) for h in allowed_hosts}:
        if allowlist_revoked_by_air_gap(host, air_gap=air_gap):
            raise error_cls(
                f"AIR_GAP is engaged: the allowlist cannot admit host {host!r} — under "
                "an air gap the operator allowlist contributes nothing and only "
                "provably in-VPC hosts pass (private/loopback IP, localhost, a dotless "
                "service name, or a .local/.localhost/.internal name). Point this "
                "endpoint at an in-VPC host, or disengage the air gap."
            )
        return endpoint.rstrip("/")
    if _is_ip_literal(host):
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback:
            return endpoint.rstrip("/")
        raise error_cls(f"IP {host!r} is not private/loopback and is not in-VPC")
    if host == "localhost":
        return endpoint.rstrip("/")
    if "." not in host:  # dotless compose/service name (packed IPv4 excluded in Phase A)
        return endpoint.rstrip("/")
    if host.endswith(_LOCAL_SUFFIXES):
        return endpoint.rstrip("/")

    raise error_cls(f"endpoint host {host!r} looks external; {external_hint}")


def validate_external_https_url(
    url: str,
    *,
    allowed_hosts: Sequence[str] | None = None,
    error_cls: type[Exception] = UnsafeEndpointError,
) -> str:
    """External-lane validator (ne-permissions Graph/login, ne-connect external
    sources): SSRF shape (https REQUIRED), then a DNS-name-only host with an
    optional exact allowlist.

    Phase A is :func:`_reject_ssrf_shape` (``require_https=True``, no redactor —
    messages name at most the parsed hostname, never the URL). Then: an IP-literal
    host is rejected (external endpoints on this lane must be DNS names); finally,
    when ``allowed_hosts`` is a sequence the normalized host MUST be a member by
    exact match (no wildcard, no in-VPC escape hatch), and when ``allowed_hosts``
    is ``None`` ANY DNS name passes.

    Returns ``url`` with any trailing ``/`` stripped.
    """
    host = _reject_ssrf_shape(url, require_https=True, error_cls=error_cls, describe=None)

    if _is_ip_literal(host):
        raise error_cls(
            f"host {host!r} is an IP literal; external endpoints on this lane must be DNS names"
        )

    if allowed_hosts is not None and host not in {normalize_host(h) for h in allowed_hosts}:
        raise error_cls(f"host {host!r} is not in the allowed-hosts allowlist for this lane")

    return url.rstrip("/")


def validate_loopback_endpoint(
    url: str,
    *,
    allowed_hosts: Sequence[str] = (),
    error_cls: type[Exception] = UnsafeEndpointError,
) -> str:
    """Loopback-lane validator (ne-memory-graph sidecar): SSRF shape, then ACCEPT
    ONLY a loopback IP literal, ``localhost``, or an explicitly allowlisted host.

    STRICTER than :func:`validate_in_vpc_endpoint`: a private (non-loopback) IP
    literal (``10.0.0.5``) and a dotless service name (``svc``) are REJECTED here
    even though the in-VPC lane accepts them. Phase A is :func:`_reject_ssrf_shape`
    (``require_https=False``, no redactor). The reject message names ONLY the host
    and contains the word ``external``.

    Returns ``url`` with any trailing ``/`` stripped.
    """
    host = _reject_ssrf_shape(url, require_https=False, error_cls=error_cls, describe=None)

    if _is_ip_literal(host):
        if ipaddress.ip_address(host).is_loopback:
            return url.rstrip("/")
    elif host == "localhost" or host in {normalize_host(h) for h in allowed_hosts}:
        return url.rstrip("/")

    raise error_cls(
        f"endpoint host {host!r} looks external; only loopback (127.0.0.1/::1), "
        "localhost, or an explicitly allowlisted host is permitted on this lane"
    )
