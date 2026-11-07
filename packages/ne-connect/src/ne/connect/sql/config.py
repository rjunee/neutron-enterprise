# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed, fail-closed configuration for the SQL connector (NE-046).

``SqlConnectorSettings`` is an :class:`~ne.contracts.config.NeSettings` subclass
(values layer init > ``NE_SQL_*`` env > TOML > defaults). ``connection_url`` is a
``SecretStr`` (C12 — never logged, never echoed in an error).

The unit invariant lives in :class:`SqlTableConfig`: ``acl`` has NO default, so a
table configured without an ACL block is UNREPRESENTABLE — pydantic rejects it
at construction. SQL rows carry no native per-row ACL, so "no config" must never
be interpreted as "public"; this makes that fail-open path a type error instead
of a runtime surprise (the same NE-CKC2 discipline
``ne.connect.contracts.PermissionModelSpec`` enforces for the connect-wide
fail-closed backstop).

NE-CKD2 reserved-namespace consistency: a table's ACL config may declare EXACTLY
one reserved label, ``acl:tenant-all`` — an explicit operator statement that the
table is org-wide visible. No other ``acl:*``/``role:*`` static principal is
accepted (those namespaces are reserved for genuine tenant-wide / platform-role
grants, never a config typo or accidental principal collision). A source ROW's
principal-column VALUE is checked separately, and more strictly, by
:func:`ne.connect.sql.acl.resolve_row_principals` — DATA may never mint a
reserved label, not even ``acl:tenant-all`` (only operator config may).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from ne.connect.contracts import PiiClass
from ne.connect.sql.acl import TENANT_ALL
from ne.contracts.config import NeSettings

__all__ = ["SqlConnectorSettings", "SqlTableAclConfig", "SqlTableConfig"]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Byte-identical to ``ne.connect.sql.acl``'s (module-private) reserved-namespace
#: tuple — kept as a local literal here rather than importing the underscored
#: name across the module boundary; a dev-dep test locks both against
#: ``ne.permissions.models`` (NE-CKD2).
_RESERVED_PREFIXES = ("acl:", "role:")

#: Metadata keys the connector itself stamps (see ``sql/sync.py``'s record
#: mapper) — a configured ``metadata_columns`` entry must not shadow one.
_RESERVED_METADATA_KEYS = frozenset(
    {"connector", "source_id", "record_id", "pii_class", "table", "source", "provenance"}
)


def _check_identifier(name: str, field: str) -> str:
    """Return ``name`` if it is a bare SQL identifier, else raise ``ValueError``.

    Blocks injection-shaped table/column names (``"users; DROP TABLE x"``,
    ``"id --"``) at config-construction time — the only free-text SQL surface
    this connector leaves is the explicitly operator-trusted ``row_filter``.
    """
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(
            f"{field} {name!r} is not a valid bare SQL identifier "
            "(must match ^[A-Za-z_][A-Za-z0-9_]*$)"
        )
    return name


class SqlTableAclConfig(BaseModel):
    """The operator-declared ACL for one configured table (fail-closed, NE-046).

    EXACTLY ONE of ``principals`` (a static tuple applied to every row) or
    ``principal_column`` (a per-row column resolved by
    :func:`ne.connect.sql.acl.resolve_row_principals`) must be set — both or
    neither is a ``ValidationError``.
    """

    model_config = ConfigDict(frozen=True)

    principals: tuple[str, ...] = ()
    principal_column: str | None = None
    principal_separator: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> SqlTableAclConfig:
        has_static = bool(self.principals)
        has_column = self.principal_column is not None
        if has_static == has_column:
            raise ValueError(
                "SqlTableAclConfig requires EXACTLY ONE of `principals` (static ACL) "
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
        # Store the STRIPPED principals (Codex P2): validation used .strip() but the
        # model kept the raw value, so a padded principal (" g-eng ", " acl:tenant-all ")
        # emitted a whitespace-laden ACL that never matched a real principal / was never
        # recognized as tenant-wide (fail-safe under-grant, but silently wrong config).
        object.__setattr__(self, "principals", tuple(normalized))
        return self


class SqlTableConfig(BaseModel):
    """Config for one crawled table: identity, doc shaping, cursor, ACL.

    ``acl`` has NO default: a table without an ACL block cannot be constructed
    (fail-closed — see module docstring). Every identifier field is validated as
    a bare SQL name; the only free-text SQL surface is the explicitly-trusted
    ``row_filter``.
    """

    model_config = ConfigDict(frozen=True)

    table: str
    schema_name: str | None = None
    id_column: str
    content_columns: tuple[str, ...] = Field(min_length=1)
    metadata_columns: tuple[str, ...] = ()
    cursor_column: str | None = None
    cursor_type: Literal["string", "int", "datetime"] = "string"
    row_filter: str | None = None
    acl: SqlTableAclConfig

    @model_validator(mode="after")
    def _validate(self) -> SqlTableConfig:
        _check_identifier(self.table, "table")
        if self.schema_name is not None:
            _check_identifier(self.schema_name, "schema_name")
        _check_identifier(self.id_column, "id_column")
        if self.cursor_column is not None:
            _check_identifier(self.cursor_column, "cursor_column")
        for col in self.content_columns:
            _check_identifier(col, "content_columns item")
        for col in self.metadata_columns:
            _check_identifier(col, "metadata_columns item")
            if col in _RESERVED_METADATA_KEYS:
                raise ValueError(
                    f"metadata_columns item {col!r} collides with a reserved metadata "
                    f"key ({sorted(_RESERVED_METADATA_KEYS)})"
                )
        return self

    @property
    def table_ref(self) -> str:
        """``schema.table`` when a schema is configured, else bare ``table``."""
        return f"{self.schema_name}.{self.table}" if self.schema_name else self.table


class SqlConnectorSettings(NeSettings):
    """Configuration for the SQL (Postgres/MSSQL, v1) connector."""

    model_config = SettingsConfigDict(
        env_prefix="NE_SQL_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    #: SECRET — C12: never logged, never echoed by an engine-construction error.
    connection_url: SecretStr = SecretStr("")
    source_name: str = "sql"
    owner: str = ""
    freshness_sla: str = "P1D"
    pii: PiiClass = PiiClass.PERSONAL
    principal_namespace: str = "oidc"
    tables: tuple[SqlTableConfig, ...] = ()

    #: Operator-declared in-VPC DNS names (same semantics as the TEI endpoint
    #: allowlists) — see ``ne.connect.sql.engine`` for the C2 host policy.
    allowed_hosts: tuple[str, ...] = ()
    #: Fixture/test escape hatch: ``sqlite://`` is otherwise rejected (v1 only
    #: supports Postgres/MSSQL in production; sqlite is never a live dialect).
    allow_sqlite: bool = False
    chunk_size: int = Field(default=1000, ge=1)

    @model_validator(mode="after")
    def _validate(self) -> SqlConnectorSettings:
        if not self.owner.strip():
            raise ValueError(
                "SQL connector requires a non-empty owner (the ODCS data-contract "
                "owner; set NE_SQL_OWNER or owner=)"
            )
        if not self.connection_url.get_secret_value():
            raise ValueError("SQL connector requires a non-empty connection_url")
        if not self.tables:
            raise ValueError(
                "SQL connector requires at least one configured table (no "
                "tenant-wide/table-wide crawl in v1)"
            )
        refs = [table.table_ref for table in self.tables]
        if len(refs) != len(set(refs)):
            raise ValueError("SqlConnectorSettings.tables must have unique table_ref values")
        return self
