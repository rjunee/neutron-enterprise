# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Provider-issued Salesforce organization identity parsing and canonicalization."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ne.connect.errors import SalesforceSyncError
from ne.net.ssrf import normalize_host

__all__ = ["canonical_salesforce_organization_id", "organization_id_from_identity_url"]

_ORG_ID = re.compile(r"^00D[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?$")
_SUFFIX = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


def canonical_salesforce_organization_id(value: str) -> str:
    """Validate a provider org id and return its 18-character case-safe form."""
    if not isinstance(value, str) or _ORG_ID.fullmatch(value) is None:
        raise SalesforceSyncError("Salesforce identity returned an invalid organization_id")
    if len(value) == 18:
        # Refuse a forged/inconsistent case-safe suffix rather than normalizing it.
        if canonical_salesforce_organization_id(value[:15]) != value:
            raise SalesforceSyncError("Salesforce identity returned an invalid organization_id")
        return value
    suffix = ""
    for offset in range(0, 15, 5):
        bits = 0
        for index, character in enumerate(value[offset : offset + 5]):
            if "A" <= character <= "Z":
                bits |= 1 << index
        suffix += _SUFFIX[bits]
    return value + suffix


def organization_id_from_identity_url(identity_url: str, *, allowed_hosts: tuple[str, ...]) -> str:
    """Extract an org id only from an allowlisted HTTPS ``/id/{org}/{user}`` URL."""
    try:
        parsed = urlsplit(identity_url)
        host = normalize_host(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise SalesforceSyncError("Salesforce token returned a malformed Identity URL") from exc
    allowed = any(
        host.endswith(entry) if entry.startswith(".") else host == entry for entry in allowed_hosts
    )
    parts = parsed.path.split("/")
    if (
        parsed.scheme.lower() != "https"
        or not host
        or port not in (None, 443)
        or not allowed
        or parsed.query
        or parsed.fragment
        or len(parts) != 4
        or parts[0] != ""
        or parts[1] != "id"
        or not parts[3]
    ):
        raise SalesforceSyncError("Salesforce token returned an untrusted Identity URL")
    return canonical_salesforce_organization_id(parts[2])
