# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed, fail-closed configuration for the CSV-file connector (NE-CONNECT-CSV).

``CsvConnectorSettings`` is an :class:`~ne.contracts.config.NeSettings` subclass
(``NE_CONNECT_CSV_*`` env, TOML-layered, ``extra='forbid'``), mirroring
:class:`ne.connect.fixture_sync.FixtureSyncSettings`'s fail-loud discipline: an
unconfigured-but-listed source surfaces as a pydantic ``ValidationError`` and
becomes a ``SyncNotConfiguredError`` upstream — never a silent no-op.

Permission model: a spreadsheet has NO source ACL system to mirror, so the
operator DECLARES the audience — ``default_acl`` feeds a
``public-within-tenant`` :class:`~ne.connect.contracts.PermissionModelSpec`,
whose own validator makes an empty audience UNREPRESENTABLE (NE-CKC2). The
schema is NOT hardcoded: ``schema_path`` names the ODCS JSON-Schema artifact the
data-contract owner maintains next to the sheet, so the contract (and therefore
every quarantine reason) is an operator-owned artifact, not connector code.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import SettingsConfigDict

from ne.connect.contracts import PiiClass
from ne.contracts.config import NeSettings

__all__ = ["CsvConnectorSettings"]


class CsvConnectorSettings(NeSettings):
    """Configuration for the polled CSV-file connector (``NE_CONNECT_CSV_*``)."""

    model_config = SettingsConfigDict(
        env_prefix="NE_CONNECT_CSV_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    #: Path to the polled CSV file (UTF-8, comma-delimited, header row REQUIRED).
    path: str = ""
    #: Path to the ODCS JSON-Schema for one row (the operator-owned contract
    #: artifact; loaded + proven well-formed at sync start, fail-loud).
    schema_path: str = ""
    #: The ODCS data-contract owner (a real accountable owner string).
    owner: str = ""
    #: The column holding a row's stable record id (a blank/absent value falls
    #: back to a line-number identity so the row still parks inspectably).
    id_column: str = "id"
    #: The declared audience (``public-within-tenant`` — a sheet has no source
    #: ACL system). JSON list in the env, e.g. ``'["grp:demo-all"]'``.
    default_acl: tuple[str, ...] = ()
    #: Conservative PII default — hand-maintained sheets carry names + emails.
    pii: PiiClass = PiiClass.PERSONAL
    #: ISO-8601 freshness SLA; the demo lane polls every 15 minutes.
    freshness_sla: str = "PT15M"
    #: The principal store the ACL labels resolve against.
    principal_namespace: str = "oidc"

    @model_validator(mode="after")
    def _validate(self) -> CsvConnectorSettings:
        if not self.path.strip():
            raise ValueError(
                "csv connector requires a non-empty path (NE_CONNECT_CSV_PATH — "
                "the polled CSV file)"
            )
        if not self.schema_path.strip():
            raise ValueError(
                "csv connector requires a non-empty schema_path (NE_CONNECT_CSV_SCHEMA_PATH "
                "— the ODCS JSON-Schema for one row; the contract is an operator-owned "
                "artifact, never connector code)"
            )
        if not self.owner.strip():
            raise ValueError(
                "csv connector requires a non-empty owner (NE_CONNECT_CSV_OWNER — "
                "the ODCS data-contract owner)"
            )
        if not self.id_column.strip():
            raise ValueError("csv connector requires a non-empty id_column")
        if not self.default_acl:
            raise ValueError(
                "csv connector requires a non-empty default_acl (NE_CONNECT_CSV_DEFAULT_ACL "
                "— a sheet has no source ACL system, so the audience must be DECLARED; "
                "an unlabeled-record configuration is fail-open and forbidden, NE-CKC2)"
            )
        return self
