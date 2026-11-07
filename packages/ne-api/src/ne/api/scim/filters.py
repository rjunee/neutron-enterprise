# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SCIM filter parsing — exactly two shapes, fail-closed (SPEC row SCIM-2).

(a) A query-string filter ``<attr> eq "<value>"`` (``GET /Users?filter=...``).
(b) A PATCH path filter ``members[value eq "<id>"]`` (Entra's remove-member
    form, e.g. ``PATCH /Groups/{id}`` with ``path: 'members[value eq "x"]'``).

Anything else is rejected — never silently ignored — with a named ``scimType``
(``invalidFilter`` / ``invalidPath``) the router maps to a 400 ``ScimError``.
Pure functions, unit-testable without any store/service wiring.

C12: a rejection names the SUPPORTED SHAPE and the SUPPORTED ATTRIBUTES — both
already published on ``/scim/v2/Schemas`` and ``/ServiceProviderConfig`` — and
never the caller's text. A SCIM filter value is a userName, an externalId or a
group member id, and a ``members[value eq "..."]`` PATCH path carries the id of
the principal being removed; ``ScimFilterError``'s message is copied verbatim
into the ``ScimBadRequestError`` the router raises and is therefore whatever a
``logger.exception`` on that refusal would print.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

__all__ = ["ScimFilterError", "parse_eq_filter", "parse_members_value_path"]


class ScimFilterError(Exception):
    """A filter/path expression outside the two supported shapes."""

    def __init__(self, scim_type: str, detail: str) -> None:
        super().__init__(detail)
        self.scim_type = scim_type
        self.detail = detail


# `<attr> eq "<value>"` — attr is alnum/dot/underscore; value may contain
# escaped quotes/backslashes (\" and \\), matching the RFC 7644 ABNF's escaping.
_EQ_FILTER_RE = re.compile(r'^\s*(?P<attr>[A-Za-z0-9_.]+)\s+eq\s+"(?P<value>(?:[^"\\]|\\.)*)"\s*$')
_EQ_FILTER_SHAPE = '<attr> eq "<value>"'

# `members[value eq "<id>"]` — the PATCH path-filter form.
_MEMBERS_VALUE_PATH_RE = re.compile(r'^members\[\s*value\s+eq\s+"(?P<value>(?:[^"\\]|\\.)*)"\s*\]$')
_MEMBERS_VALUE_PATH_SHAPE = 'members[value eq "<id>"]'


def _unescape(raw: str) -> str:
    return raw.replace('\\"', '"').replace("\\\\", "\\")


def parse_eq_filter(raw_filter: str, allowed_attrs: Mapping[str, str]) -> tuple[str, str]:
    """Parse ``<attr> eq "<value>"``; return ``(canonical_attr, value)``.

    ``allowed_attrs`` maps ``lower(attr) -> canonical wire name`` (e.g.
    ``{"username": "userName", "externalid": "externalId"}``). Any other shape,
    or an attr outside ``allowed_attrs``, raises :class:`ScimFilterError` with
    ``scim_type="invalidFilter"`` — fail closed, never a best-effort guess.
    """
    match = _EQ_FILTER_RE.match(raw_filter)
    if match is None:
        raise ScimFilterError("invalidFilter", f"filter must have the shape {_EQ_FILTER_SHAPE}")
    canonical = allowed_attrs.get(match.group("attr").lower())
    if canonical is None:
        supported = ", ".join(sorted(allowed_attrs.values()))
        raise ScimFilterError(
            "invalidFilter", f"filter attribute is not supported; supported: {supported}"
        )
    return canonical, _unescape(match.group("value"))


def parse_members_value_path(path: str) -> str:
    """Parse ``members[value eq "<id>"]``; return the member id.

    Raises :class:`ScimFilterError` with ``scim_type="invalidPath"`` for any
    other shape.
    """
    match = _MEMBERS_VALUE_PATH_RE.match(path.strip())
    if match is None:
        raise ScimFilterError(
            "invalidPath", f"members path filter must have the shape {_MEMBERS_VALUE_PATH_SHAPE}"
        )
    return _unescape(match.group("value"))
