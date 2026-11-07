# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C2 in-VPC catalog validation — fail-loud at composition time, no lazy checks.

:func:`validated_catalog` is the ONE place (composition-wise) the private in-VPC MCP
server catalog is assembled. Every accepted entry is a :class:`ValidatedCatalogEntry` —
a proof-carrying subclass whose own before-validator runs the consolidated ne.net
validator (``validate_in_vpc_endpoint`` — NE-CKC10) over the endpoint, rejecting
cloud-metadata hosts, packed-integer IPv4, embedded credentials, and external hosts,
while accepting private IPs, ``localhost`` / ``::1``, dotless compose service names,
``.local`` / ``.localhost`` / ``.internal`` suffixes, and explicitly allowlisted
private-DNS hosts. Because construction ITSELF validates + normalizes (trailing-``/``
stripped), an instance carrying an unvalidated endpoint is unrepresentable; the
returned ``ValidatedCatalogEntry`` type is the proof token :class:`ExternalMcpToolProvider`
demands. Duplicate ``name``s are refused — a composition root that mis-configures the
catalog fails at startup, never at first tool call.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import model_validator

from ne.mcp.config import McpServerCatalogEntry, McpSettings
from ne.mcp.errors import McpCatalogError
from ne.net import validate_in_vpc_endpoint


class ValidatedCatalogEntry(McpServerCatalogEntry):
    """Proof-carrying C2-validated catalog entry.

    Constructing one ALWAYS runs ``ne.net.validate_in_vpc_endpoint``, so an instance
    with an unvalidated endpoint is unrepresentable; :func:`validated_catalog` is the
    sole intended producer; :class:`ExternalMcpToolProvider` refuses anything else.

    Inherits ``frozen`` + ``extra='forbid'`` and the name/tier validators from
    :class:`McpServerCatalogEntry`; adds NO fields. The C2 validation MUST run in
    ``mode='before'`` (the model is frozen, so the normalized endpoint cannot be
    written back after construction).
    """

    @model_validator(mode="before")
    @classmethod
    def _c2_validate_endpoint(cls, data: Any) -> Any:
        # Fail closed: only field-values (a mapping) may construct this proof type. A
        # non-Mapping path (e.g. an already-built model) would skip validation, so it is
        # refused rather than passed through.
        if not isinstance(data, Mapping):
            raise McpCatalogError(
                "ValidatedCatalogEntry must be constructed from field values "
                "(name/endpoint/...), got " + type(data).__name__
            )
        data = dict(data)
        normalized = validate_in_vpc_endpoint(
            str(data.get("endpoint", "")),
            allowed_hosts=tuple(data.get("allowed_hosts") or ()),
            error_cls=McpCatalogError,
        )
        data["endpoint"] = normalized
        return data


def validated_catalog(settings: McpSettings) -> tuple[ValidatedCatalogEntry, ...]:
    """Return the catalog with every endpoint C2-validated + normalized, or raise.

    Each entry is rebuilt as a :class:`ValidatedCatalogEntry` via EXPLICIT kwargs so the
    class's own before-validator runs the C2 validate+normalize (pydantic v2
    ``model_copy`` deliberately BYPASSES validators, so it must not be used here).

    Raises :class:`McpCatalogError` on the first entry whose endpoint fails in-VPC
    validation or on a duplicate ``name`` (fail loud — nothing partial is returned).
    """
    seen: set[str] = set()
    validated: list[ValidatedCatalogEntry] = []
    for entry in settings.catalog:
        if entry.name in seen:
            raise McpCatalogError(
                f"duplicate catalog server name {entry.name!r}; each in-VPC MCP server "
                "must have a unique name (it is prefix-locked into the tool namespace)"
            )
        seen.add(entry.name)
        validated.append(
            ValidatedCatalogEntry(
                name=entry.name,
                endpoint=entry.endpoint,
                tier=entry.tier,
                allowed_hosts=entry.allowed_hosts,
            )
        )
    return tuple(validated)
