# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ODCS-shaped data contracts + the typed permission model (NE-040).

:class:`OdcsDataContract` implements the FROZEN
``ne.contracts.integration.DataContract`` Protocol: owner, JSON-Schema record
schema (proven well-formed at construction), ISO-8601 freshness SLA, PII class,
and a TYPED :class:`PermissionModelSpec` (the NE-042/043 seam).

Fail-closed permission model (NE-CKC2 lesson): a ``public-within-tenant`` source
without a non-empty ``default_acl`` is UNREPRESENTABLE — the pydantic validator
rejects it at construction, so an unlabeled-record configuration can never exist
at the type level.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, localcontext
from enum import StrEnum
from functools import cached_property
from typing import Any

import jsonschema
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ne.contracts.frozen import AnyMap
from ne.contracts.source_authority import SourceContractIdentity

_MAX_MESSAGES = 5
_M365_SOURCE_KIND = "m365-sharepoint"

# Strict ISO-8601 duration: at least one component; a bare 'P' or 'PT' is rejected;
# the time part (T...) requires at least one H/M/S component.
_ISO8601_DURATION = re.compile(
    r"^P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+(?:\.\d+)?S)?)?$"
)

_FIXED_DURATION = re.compile(
    r"^P(?:(?P<weeks>\d+)W|(?:(?P<days>\d+)D)?"
    r"(?:T(?=\d)(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?)$"
)


def _fixed_duration_seconds(value: str) -> Decimal | None:
    matched = _FIXED_DURATION.fullmatch(value)
    if matched is None:
        return None
    with localcontext() as context:
        context.prec = max(100, len(value) * 2)
        parts = {name: Decimal(raw or "0") for name, raw in matched.groupdict().items()}
        return (
            parts["weeks"] * 7 * 24 * 60 * 60
            + parts["days"] * 24 * 60 * 60
            + parts["hours"] * 60 * 60
            + parts["minutes"] * 60
            + parts["seconds"]
        )


def _canonical_fixed_seconds(value: Decimal) -> str:
    sign, raw_digits, raw_exponent = value.as_tuple()
    if not isinstance(raw_exponent, int):
        raise ValueError("M365 freshness SLA must be finite")
    exponent = raw_exponent
    digits = list(raw_digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    if len(digits) > 76 or abs(exponent) > 38:
        raise ValueError("M365 freshness SLA exceeds exact precision/scale bounds")
    return format(Decimal((sign, tuple(digits), exponent)), "f")


def _strict_json_value(value: Any, *, path: str = "$") -> Any:
    """Return deterministic literal JSON or reject ambiguous/non-JSON members."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, member in value.items():
            if not isinstance(key, str):
                raise ValueError(f"contract descriptor key at {path} must be a string")
            result[key] = _strict_json_value(member, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [
            _strict_json_value(member, path=f"{path}[{index}]")
            for index, member in enumerate(value)
        ]
    if isinstance(value, (set, frozenset)):
        raise ValueError(f"contract descriptor at {path} cannot contain an unordered set")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"contract descriptor at {path} cannot contain a non-finite number")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(
        f"contract descriptor at {path} contains non-JSON value {type(value).__name__}"
    )


class PiiClass(StrEnum):
    """The PII sensitivity class of a source's records."""

    NONE = "none"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"


class PermissionMode(StrEnum):
    """How a source's records are permissioned once ingested."""

    SOURCE_ACL = "source-acl"
    PUBLIC_WITHIN_TENANT = "public-within-tenant"


def validate_iso8601_duration(value: str) -> str:
    """Return ``value`` if it is a strict ISO-8601 duration, else raise ``ValueError``.

    Requires at least one component and rejects a bare ``'P'`` / ``'PT'`` — a
    freshness SLA that parses to "no duration" is a silent misconfiguration.
    """
    if not _ISO8601_DURATION.match(value):
        raise ValueError(
            f"freshness_sla {value!r} is not a valid ISO-8601 duration "
            "(e.g. 'PT15M', 'P1D', 'P1DT12H')"
        )
    return value


def _to_plain(value: Any) -> Any:
    """Recursively convert a frozen/JSON-shaped value into plain ``dict``/``list``.

    ``jsonschema`` type-checks ``"object"`` via ``isinstance(x, dict)`` and a
    ``FrozenDict`` is a ``Mapping`` but not a ``dict``; this normalizes both the
    schema and instance payloads so validation sees genuine ``dict``/``list``.
    Local copy of the ne-brain write-path helper semantics (the underscore-
    private original is not imported across the package seam).
    """
    if isinstance(value, Mapping):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_plain(v) for v in value]
    return value


class PermissionModelSpec(BaseModel):
    """Typed permission model for a source (the NE-042/043 permission seam).

    ``principal_namespace`` names which principal store the ACL labels resolve
    against (e.g. ``'oidc'`` groups). The ``model_validator`` makes a
    ``public-within-tenant`` mode without a non-empty ``default_acl``
    UNREPRESENTABLE — the NE-CKC2 fail-open lesson enforced at the type level.
    """

    model_config = ConfigDict(frozen=True)

    mode: PermissionMode
    default_acl: tuple[str, ...] = ()
    principal_namespace: str = "oidc"

    @model_validator(mode="after")
    def _require_default_acl_when_public(self) -> PermissionModelSpec:
        if self.mode is PermissionMode.PUBLIC_WITHIN_TENANT and not self.default_acl:
            raise ValueError(
                "public-within-tenant permission mode REQUIRES a non-empty default_acl "
                "(an unlabeled-record configuration is fail-open and forbidden — NE-CKC2)"
            )
        return self


class OdcsDataContract(BaseModel):
    """ODCS-shaped data contract implementing the frozen ``DataContract`` Protocol.

    The JSON-Schema ``schema_def`` is proven well-formed at construction via
    ``Draft202012Validator.check_schema`` (a malformed schema fails LOUD as a
    ``ValueError``), and ``freshness_sla`` is validated as a strict ISO-8601
    duration. ``permissions`` is the TYPED :class:`PermissionModelSpec`; the
    Protocol's ``pii_class`` / ``permission_model`` properties return the plain
    strings the frozen surface declares.
    """

    model_config = ConfigDict(frozen=True)

    source: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    schema_def: AnyMap
    freshness_sla: str
    pii: PiiClass
    permissions: PermissionModelSpec

    @model_validator(mode="after")
    def _validate_schema_and_freshness(self) -> OdcsDataContract:
        validate_iso8601_duration(self.freshness_sla)
        try:
            jsonschema.Draft202012Validator.check_schema(_to_plain(self.schema_def))
        except SchemaError as exc:
            raise ValueError(
                f"invalid JSON-Schema for data contract source {self.source!r}: {exc.message}"
            ) from exc
        return self

    @cached_property
    def _validator(self) -> jsonschema.Draft202012Validator:
        return jsonschema.Draft202012Validator(_to_plain(self.schema_def))

    # -- frozen DataContract Protocol surface -------------------------------
    @property
    def pii_class(self) -> str:
        return self.pii.value

    @property
    def permission_model(self) -> str:
        return self.permissions.mode.value

    def validate(  # type: ignore[override]  # pyright: ignore[reportIncompatibleMethodOverride]
        self, record: Mapping[str, Any]
    ) -> Sequence[str]:
        """Return ``'path: message'`` strings for schema violations (capped at 5).

        Empty sequence == the record conforms. Mirrors the KnowledgeWriter
        ``_validate_payload`` message shape (sorted by json_path).
        """
        errors = sorted(
            self._validator.iter_errors(_to_plain(record)),
            key=lambda e: str(e.json_path),
        )
        return [f"{e.json_path}: {e.message}" for e in errors[:_MAX_MESSAGES]]


def m365_source_contract_identity(
    contract: OdcsDataContract,
    *,
    mapper_revision: str,
    config_revision: str,
) -> SourceContractIdentity:
    """Hash the exact credential-free M365 contract interpretation descriptor.

    ``DataContract`` has no id/version member, so the supported-source factory owns
    this identity.  Credential material is deliberately not accepted by the API.
    """
    if not mapper_revision or not config_revision:
        raise ValueError("M365 mapper_revision and config_revision must be non-empty")
    fixed_seconds = _fixed_duration_seconds(contract.freshness_sla)
    if fixed_seconds is None or fixed_seconds <= 0:
        raise ValueError("M365 authority requires an exact fixed-duration freshness SLA")
    canonical_fixed_seconds = _canonical_fixed_seconds(fixed_seconds)
    descriptor = {
        "config_revision": config_revision,
        "freshness_sla": contract.freshness_sla,
        "freshness_sla_seconds": canonical_fixed_seconds,
        "mapper_revision": mapper_revision,
        "owner": contract.owner,
        "permission_model": _strict_json_value(contract.permissions.model_dump(mode="json")),
        "pii_class": contract.pii.value,
        "schema": _strict_json_value(contract.schema_def),
        "source": contract.source,
        "wire": 1,
    }
    canonical = json.dumps(
        descriptor,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return SourceContractIdentity(
        source_kind=_M365_SOURCE_KIND,
        descriptor_schema_version=1,
        descriptor_hash=hashlib.sha256(canonical).hexdigest(),
    )
