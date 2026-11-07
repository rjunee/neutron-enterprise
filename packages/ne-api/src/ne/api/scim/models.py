# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SCIM 2.0 wire models (Users + Groups) — hand-rolled pydantic, zero deps.

Field names match the SCIM wire format verbatim (``userName``, ``displayName``,
``Resources``, ``Operations``, ...) so no aliasing is needed. Every response is
served with media type ``application/scim+json`` (see ``ne.api.scim.router``).

RESOURCE models (``ScimUser``/``ScimGroup``/``ScimGroupMember``/``ScimMeta``)
use ``extra="ignore"``: real IdP default mappings (Entra/Okta) send standard
SCIM attributes outside this unit's supported subset (``name``, ``emails``,
``title``, a member ``type``, ...) on create/replace bodies, and rejecting the
whole request would break push provisioning — the same tolerance the PATCH
handler applies to unknown paths (ignored-but-audited). ENVELOPE models
(``PatchRequest``/``PatchOperation``, plus the response-only ``ListResponse``/
``ScimError``) keep ``extra="forbid"``: the RFC 7644 message envelopes are
fixed shapes, so an unknown envelope field is a malformed message
(``invalidSyntax``), not an unmapped attribute.

Inbound ``meta`` is dropped wholesale (server-owned, RFC 7643 §3.1 read-only):
``ScimMeta`` has required subfields, so ``extra="ignore"`` alone does not
protect a *partial* inbound ``meta`` block from a 400 — a before-validator on
each resource model discards any wire-provided ``meta`` value entirely, since
the service never consumes inbound ``meta`` and always constructs its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

__all__ = [
    "ERROR_SCHEMA",
    "GROUP_SCHEMA",
    "LIST_RESPONSE_SCHEMA",
    "PATCH_OP_SCHEMA",
    "USER_SCHEMA",
    "ListResponse",
    "PatchOperation",
    "PatchRequest",
    "ScimError",
    "ScimGroup",
    "ScimGroupMember",
    "ScimMeta",
    "ScimUser",
    "describe_validation_error",
]

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_RESPONSE_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_OP_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


def _coerce_bool(value: Any) -> Any:
    """Entra quirk: booleans sometimes arrive as the strings 'True'/'False'."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return value


def _drop_inbound_meta(value: Any) -> Any:
    """Drop any wire-provided ``meta`` value; the service owns ``meta``.

    ``ScimMeta`` has required subfields, so ``extra="ignore"`` does not save a
    *partial* inbound ``meta`` block (e.g. ``{"resourceType": "User"}``) from
    failing validation. Inbound ``meta`` is read-only per RFC 7643 §3.1 and is
    never consumed by the service — it always constructs its own ``ScimMeta``
    — so any wire value (dict/str/garbage) is dropped to ``None``. An already
    -constructed ``ScimMeta`` instance (the response-serialization path) is
    passed through untouched.
    """
    return value if (value is None or isinstance(value, ScimMeta)) else None


class ScimMeta(BaseModel):
    """``meta`` block — resource kind, timestamps, and its own SCIM location."""

    model_config = ConfigDict(extra="ignore")

    resourceType: str
    created: datetime
    lastModified: datetime
    location: str


class ScimUser(BaseModel):
    """The SCIM ``User`` resource — request body AND response representation.

    ``id`` is server-assigned (see ``ne.api.scim.service``'s ID RULE) so it is
    optional on inbound create/update bodies; every response always sets it.
    """

    model_config = ConfigDict(extra="ignore")

    schemas: tuple[str, ...] = (USER_SCHEMA,)
    id: str | None = None
    userName: str
    externalId: str | None = None
    displayName: str | None = None
    active: bool = True
    meta: ScimMeta | None = None

    @field_validator("active", mode="before")
    @classmethod
    def _coerce_active(cls, value: Any) -> Any:
        return _coerce_bool(value)

    @field_validator("meta", mode="before")
    @classmethod
    def _drop_meta(cls, value: Any) -> Any:
        return _drop_inbound_meta(value)


class ScimGroupMember(BaseModel):
    """One ``members`` entry — a principal id and an optional display label."""

    model_config = ConfigDict(extra="ignore")

    value: str
    display: str | None = None


class ScimGroup(BaseModel):
    """The SCIM ``Group`` resource — request body AND response representation."""

    model_config = ConfigDict(extra="ignore")

    schemas: tuple[str, ...] = (GROUP_SCHEMA,)
    id: str | None = None
    displayName: str
    externalId: str | None = None
    members: tuple[ScimGroupMember, ...] = ()
    meta: ScimMeta | None = None

    @field_validator("meta", mode="before")
    @classmethod
    def _drop_meta(cls, value: Any) -> Any:
        return _drop_inbound_meta(value)


class ListResponse(BaseModel):
    """A SCIM ``ListResponse`` envelope. ``Resources`` carries plain dicts —

    already-serialized (``mode="json", exclude_none=True``) resource views, so
    this model needs no knowledge of the nested resource's own schema/typing.
    """

    model_config = ConfigDict(extra="forbid")

    schemas: tuple[str, ...] = (LIST_RESPONSE_SCHEMA,)
    totalResults: int
    startIndex: int
    itemsPerPage: int
    Resources: tuple[dict[str, Any], ...] = ()


class PatchOperation(BaseModel):
    """One ``PATCH`` operation. ``op`` is normalized case-insensitively."""

    model_config = ConfigDict(extra="forbid")

    op: str
    path: str | None = None
    value: Any = None

    @field_validator("op")
    @classmethod
    def _normalize_op(cls, value: str) -> str:
        return value.strip().lower()


class PatchRequest(BaseModel):
    """The SCIM ``PatchOp`` request envelope (``RFC 7644 §3.5.2``)."""

    model_config = ConfigDict(extra="forbid")

    schemas: tuple[str, ...] = (PATCH_OP_SCHEMA,)
    Operations: tuple[PatchOperation, ...]


class ScimError(BaseModel):
    """The SCIM ``Error`` response body (``RFC 7644 §3.12``)."""

    model_config = ConfigDict(extra="forbid")

    schemas: tuple[str, ...] = (ERROR_SCHEMA,)
    status: str
    scimType: str | None = None
    detail: str | None = None


# The names a refusal MAY speak: the SCIM wire vocabulary this server publishes
# — every declared model field above, plus the RFC 7644 §3.4.2 protocol query
# parameters — all of which are already advertised on ``/scim/v2/Schemas`` and
# ``/ServiceProviderConfig``, and the FastAPI location prefixes. Anything else in
# a pydantic ``loc`` is caller-supplied text.
_PROTOCOL_PARAMS = frozenset({"filter", "startIndex", "count"})
_LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})
_SPEAKABLE_FIELDS: frozenset[str] = (
    frozenset(
        name
        for model in (
            ListResponse,
            PatchOperation,
            PatchRequest,
            ScimError,
            ScimGroup,
            ScimGroupMember,
            ScimMeta,
            ScimUser,
        )
        for name in model.model_fields
    )
    | _PROTOCOL_PARAMS
    | _LOCATIONS
)


def _field_path(loc: Sequence[Any]) -> str | None:
    """Render a pydantic ``loc`` as a dotted path, or ``None`` if it is caller data.

    A ``loc`` part is echoed ONLY when it is a name this server itself publishes
    (``_SPEAKABLE_FIELDS``) or a list INDEX. For ``extra_forbidden`` — and for any
    future error type that reports a caller-supplied key — the last part is text
    the caller chose: unbounded, and capable of carrying an injection payload into
    whatever reads the refusal. Such a ``loc`` is refused a name here and COUNTED
    by the caller of this helper. The test is membership, not the error ``type``,
    so a new pydantic error carrying a caller-controlled ``loc`` is covered the day
    it appears rather than the day someone notices.

    That last sentence is pinned INDEPENDENTLY of the ``extra_forbidden`` branch in
    :func:`describe_validation_error` by
    ``test_a_caller_supplied_loc_is_refused_a_name_under_any_error_type``, which
    feeds a caller-shaped ``loc`` under a NON-``extra_forbidden`` type: without it
    the two guards are mutually redundant for today's models and either could be
    deleted with the suite still green.
    """
    parts: list[str] = []
    for piece in loc:
        if isinstance(piece, int):
            parts.append(str(piece))
            continue
        text = str(piece)
        if text not in _SPEAKABLE_FIELDS:
            return None
        parts.append(text)
    return ".".join(parts) if parts else None


def describe_validation_error(errors: Sequence[Mapping[str, Any]]) -> str:
    """The ONE C12-safe SCIM validation-refusal message: FIELDS, never VALUES.

    Both SCIM surfaces that turn a pydantic failure into a 400 ``invalidSyntax``
    — the hand-parsed PATCH envelope and the ``RequestValidationError`` handler
    for FastAPI-bound bodies and query parameters — compose their ``detail``
    here, so there is one message policy rather than two that drift.

    Only two things are read out of each error: ``loc`` (filtered through
    :func:`_field_path`) and ``type`` (a fixed pydantic-core vocabulary such as
    ``missing`` / ``bool_parsing``). ``input`` is the rejected value verbatim;
    ``msg`` and ``ctx`` can embed it. SCIM bodies carry user identity attributes
    — userName, externalId, displayName, group member ids — so none of those
    three is ever read, and the caller of this function also severs the exception
    chain (``raise ... from None``): pydantic's own rendering puts the value back
    as ``input_value=`` in ``traceback.format_exception`` output, which is the
    text any ``logger.exception`` / Sentry / OTel ``record_exception`` ships.
    """
    parts: list[str] = []
    undeclared = 0
    for error in errors:
        kind = str(error.get("type", "invalid"))
        # The ``extra_forbidden`` branch is a CLASSIFICATION guard, not the value
        # guard: an undeclared field must be reported as undeclared even when the
        # caller picked a key that happens to be a name this server publishes (at
        # which point ``_field_path`` would happily render it, and the refusal
        # would misdescribe a rejected extra key as an invalid declared field).
        # Pinned on its own by
        # ``test_extra_forbidden_is_counted_as_undeclared_even_for_a_published_name``.
        # Containment of caller-chosen keys is ``_field_path``'s membership test.
        field = None if kind == "extra_forbidden" else _field_path(error.get("loc") or ())
        if field is None:
            undeclared += 1
            continue
        if kind == "missing":
            parts.append(f"{field!r} is required")
        else:
            parts.append(f"{field!r} is invalid ({kind})")
    if undeclared:
        noun = "field" if undeclared == 1 else "fields"
        parts.append(f"{undeclared} undeclared {noun} rejected")
    if not parts:
        return "request failed validation"
    return "request failed validation: " + "; ".join(parts)
