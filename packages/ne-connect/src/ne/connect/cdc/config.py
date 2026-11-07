# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed, fail-closed configuration for the CDC sink (NE-084).

Mirrors :mod:`ne.connect.sql.config` (NE-046) with the SAME fail-closed
discipline: a table configured without an ACL block is UNREPRESENTABLE (``acl``
has NO default), because a CDC row — like a SQL row — carries no native per-row
ACL and "no config" must never be interpreted as "public".

NE-CKD2 reserved-namespace consistency: a table's ACL config may declare EXACTLY
one reserved label, ``acl:tenant-all`` — an explicit operator statement that the
table is org-wide visible. No other ``acl:*``/``role:*`` static principal is
accepted. A source ROW's principal-column VALUE is checked separately, and more
strictly, by :func:`ne.connect.cdc.acl.resolve_row_principals` — DATA may never
mint a reserved label, not even ``acl:tenant-all``.

This module is deliberately ``dlt``-free and ``httpx``-free (the import-hygiene
guardian locks it): it imports only pydantic, stdlib, and
:class:`~ne.connect.contracts.PiiClass`.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ne.connect.cdc.acl import TENANT_ALL
from ne.connect.contracts import PiiClass

__all__ = [
    "CdcSinkConfig",
    "CdcTableAclConfig",
    "CdcTableConfig",
]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Byte-identical to ``ne.connect.cdc.acl`` / ``ne.connect.sql.config`` — a
#: package-local literal (a parity test locks the equality, NE-CKD2).
_RESERVED_PREFIXES = ("acl:", "role:")


def _check_identifier(name: str, field: str) -> str:
    """Return ``name`` if it is a bare identifier, else raise ``ValueError``."""
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(
            f"{field} {name!r} is not a valid bare identifier (must match ^[A-Za-z_][A-Za-z0-9_]*$)"
        )
    return name


class CdcTableAclConfig(BaseModel):
    """The operator-declared ACL for one configured CDC table (fail-closed).

    EXACTLY ONE of ``principals`` (a static tuple applied to every row) or
    ``principal_column`` (a per-row column resolved by
    :func:`ne.connect.cdc.acl.resolve_row_principals`) must be set — both or
    neither is a ``ValidationError``. The validator is byte-semantically
    identical to :class:`ne.connect.sql.config.SqlTableAclConfig` (a parity test
    locks it).
    """

    model_config = ConfigDict(frozen=True)

    principals: tuple[str, ...] = ()
    principal_column: str | None = None
    principal_separator: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> CdcTableAclConfig:
        has_static = bool(self.principals)
        has_column = self.principal_column is not None
        if has_static == has_column:
            raise ValueError(
                "CdcTableAclConfig requires EXACTLY ONE of `principals` (static ACL) "
                "or `principal_column` (per-row ACL) — both or neither is a config error"
            )
        if self.principal_separator is not None and not has_column:
            raise ValueError("principal_separator requires principal_column to be set")
        if has_column:
            assert self.principal_column is not None  # narrows for the type checker
            _check_identifier(self.principal_column, "principal_column")
        normalized: list[str] = []
        for principal in self.principals:
            stripped = principal.strip()
            if not stripped:
                raise ValueError("a static ACL principal must be non-empty after stripping")
            if stripped.startswith(_RESERVED_PREFIXES) and stripped != TENANT_ALL:
                raise ValueError(
                    f"static ACL principal {principal!r} uses a reserved namespace "
                    "(acl:/role:) — the ONLY reserved label operator config may use is "
                    f"exactly {TENANT_ALL!r} (NE-CKD2)"
                )
            normalized.append(stripped)
        # Store the STRIPPED principals (padded config must not emit a whitespace-laden
        # ACL that never matches a real principal / is never recognized as tenant-wide).
        object.__setattr__(self, "principals", tuple(normalized))
        return self


class CdcTableConfig(BaseModel):
    """Config for one captured table: key identity, doc shaping, ACL, PII class.

    ``acl`` has NO default: a table without an ACL block cannot be constructed
    (fail-closed). ``key_columns`` (min 1) form the record identity used to
    derive a stable ``item_id`` so an UPDATE replaces — and a DELETE removes —
    the SAME chunk an INSERT created. ``content_columns`` empty ⇒ the full
    ``after`` row is the content; otherwise the projection in configured order.
    """

    model_config = ConfigDict(frozen=True)

    key_columns: tuple[str, ...] = Field(min_length=1)
    content_columns: tuple[str, ...] = ()
    acl: CdcTableAclConfig
    #: PERSONAL is the conservative default for arbitrary row data.
    pii_class: PiiClass = PiiClass.PERSONAL


class CdcSinkConfig(BaseModel):
    """The full CDC sink config: a source name + the per-table map (min 1).

    ``tables`` keys are table identities as produced by
    :func:`ne.connect.cdc.envelope.table_identity` (``"public.customers"`` for
    Postgres/SQL Server, ``"appdb.customers"`` for MySQL).
    """

    model_config = ConfigDict(frozen=True)

    source_name: str = "debezium"
    tables: dict[str, CdcTableConfig] = Field(min_length=1)

    @classmethod
    def from_json(cls, text: str) -> CdcSinkConfig:
        """Parse a JSON document into a :class:`CdcSinkConfig`.

        Raises a loud :class:`ValueError` on ANY parse/validation failure. The
        error NEVER echoes the input text (it may carry secret-shaped material).
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"CDC tables config is not valid JSON: {exc.msg}") from None
        try:
            return cls.model_validate(data)
        except Exception as exc:
            raise ValueError(f"CDC tables config failed validation: {type(exc).__name__}") from exc
