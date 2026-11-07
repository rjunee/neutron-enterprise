# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Canonical, server-owned source-of-record links for connector metadata.

These helpers turn connector-authoritative configuration and source identifiers
into an optional ``source_url`` metadata value.  They never accept credentials,
signed/download URLs, response-controlled hosts, or browser-composed origins.
Invalid or unconfigured provenance degrades to ``None`` so ingestion and ACL
enforcement continue without exposing an unsafe locator.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from ne.net.ssrf import UnsafeEndpointError, normalize_host, validate_external_https_url

__all__ = [
    "canonical_external_source_url",
    "canonical_salesforce_instance_origin",
    "salesforce_record_source_url",
]

_MAX_SOURCE_URL_LENGTH = 2048
_MAX_PERCENT_DECODE_PASSES = 8
_SALESFORCE_OBJECT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SALESFORCE_RECORD_ID_RE = re.compile(r"^(?:[A-Za-z0-9]{15}|[A-Za-z0-9]{18})$")
_AMBIGUOUS_PATH_ESCAPE_RE = re.compile(r"%(?:00|2e|2f|5c)", re.IGNORECASE)
_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def canonical_external_source_url(
    value: object,
    *,
    allowed_hosts: Sequence[str],
) -> str | None:
    """Return a canonical HTTPS locator bound to an exact configured host.

    ``None`` is the fail-closed result for absent authority or any malformed,
    credential-bearing, query/fragment-bearing, off-host, or ambiguous URL.
    Error paths never echo ``value``.
    """
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_SOURCE_URL_LENGTH
        or not allowed_hosts
        or any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)
    ):
        return None
    try:
        validated = validate_external_https_url(value, allowed_hosts=allowed_hosts)
        parsed = urlsplit(validated)
        port = parsed.port
    except (UnsafeEndpointError, ValueError):
        return None

    if port not in (None, 443):
        return None
    path = parsed.path
    if (
        not path.startswith("/")
        or path == "/"
        or "\\" in path
        or _AMBIGUOUS_PATH_ESCAPE_RE.search(path)
        or _INVALID_PERCENT_ESCAPE_RE.search(path)
    ):
        return None
    decoded_path = path
    for _ in range(_MAX_PERCENT_DECODE_PASSES):
        next_path = unquote(decoded_path)
        if (
            "\\" in next_path
            or _AMBIGUOUS_PATH_ESCAPE_RE.search(next_path)
            or _INVALID_PERCENT_ESCAPE_RE.search(next_path)
            or any(unicodedata.category(char).startswith("C") for char in next_path)
            or any(segment in {".", ".."} for segment in next_path.split("/"))
        ):
            return None
        if next_path == decoded_path:
            break
        decoded_path = next_path
    else:
        # An attacker-controlled path that still changes after the bounded
        # decode budget is ambiguous by construction. Never pass it through.
        return None

    host = normalize_host(parsed.hostname or "")
    if not host:
        return None
    # The external-lane validator already proved scheme, authority, host, and
    # absence of userinfo/query/fragment. Rebuild only those canonical parts;
    # an explicit default port is deliberately omitted.
    return urlunsplit(("https", host, path.rstrip("/"), "", ""))


def salesforce_record_source_url(
    *,
    instance_url: str,
    instance_allowed_hosts: Sequence[str],
    object_name: str,
    record_id: str,
) -> str | None:
    """Build one Salesforce Lightning record link from validated authorities."""
    if not _SALESFORCE_OBJECT_RE.fullmatch(object_name):
        return None
    if not _SALESFORCE_RECORD_ID_RE.fullmatch(record_id):
        return None

    origin = canonical_salesforce_instance_origin(
        instance_url=instance_url,
        source_link_allowed_hosts=instance_allowed_hosts,
    )
    if origin is None:
        return None

    path = f"/lightning/r/{quote(object_name, safe='')}/{quote(record_id, safe='')}/view"
    return canonical_external_source_url(
        f"{origin}{path}",
        allowed_hosts=(urlsplit(origin).hostname or "",),
    )


def canonical_salesforce_instance_origin(
    *,
    instance_url: str,
    source_link_allowed_hosts: Sequence[str],
) -> str | None:
    """Return the exact non-secret mapping decision for Salesforce links."""
    if (
        not instance_url
        or len(instance_url) > _MAX_SOURCE_URL_LENGTH
        or not source_link_allowed_hosts
        or any(
            char.isspace() or unicodedata.category(char).startswith("C") for char in instance_url
        )
    ):
        return None
    try:
        validated_origin = validate_external_https_url(instance_url, allowed_hosts=None)
        parsed = urlsplit(validated_origin)
        port = parsed.port
    except (UnsafeEndpointError, ValueError):
        return None
    host = normalize_host(parsed.hostname or "")
    if (
        not host
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
        or not _host_matches_patterns(host, source_link_allowed_hosts)
    ):
        return None
    return f"https://{host}"


def _host_matches_patterns(host: str, allowed_hosts: Sequence[str]) -> bool:
    """Match Salesforce's configured exact-or-dot-suffix host authority."""
    for raw_entry in allowed_hosts:
        entry = normalize_host(raw_entry)
        if entry.startswith("."):
            if host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False
