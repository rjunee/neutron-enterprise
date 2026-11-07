# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-INGEST: the production-usable ``fixture`` connector sync (first-seed / demo).

Wraps the import-cheap, zero-network :class:`~ne.connect.testing.fixture_connector.
FixtureConnector` (the entry-point-registered ``fixture`` connector) as a governed
sync source, so ``ne connect sync fixture`` / the seeded cron can seed a demo corpus
(the NE-077 'Acme corpus ingested' gate step) WITHOUT a live tenant. Sanitization is
the recorder's job — a committed fixture carries innocuous fake data only (plan §1.4).

The write path is the EXISTING governed :class:`~ne.brain.write.KnowledgeWriter`
handed in by the composer (:func:`ne.connect.runner.build_ingest_stack`); this module
only maps the recorded fixture into an ODCS contract + a :class:`SyncRunner`, exactly
as the real connectors do. The run mode is ALWAYS ``backfill`` — a re-run is
dedup-ledger idempotent (unchanged records report ``skipped_unchanged``).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_settings import SettingsConfigDict

from ne.brain.write import WriteQuarantine
from ne.connect.bridge import SOURCE_IDENTITY_MAPPER_REVISION, SyncReport, SyncRunner
from ne.connect.contracts import (
    OdcsDataContract,
    PermissionMode,
    PermissionModelSpec,
    PiiClass,
)
from ne.connect.state import ConnectorStateStore
from ne.connect.testing.fixture_connector import FixtureConnector
from ne.contracts.config import NeSettings
from ne.contracts.governance import AuditSink
from ne.contracts.knowledge import KnowledgeWrite

__all__ = ["FixtureSyncOutcome", "FixtureSyncSettings", "run_fixture_sync"]


class FixtureSyncSettings(NeSettings):
    """Configuration for the recorded-fixture sync source (``NE_CONNECT_FIXTURE_*``).

    Fail-loud (the m365 validator precedent): an empty ``path`` or ``owner`` raises
    at construction — so an UNCONFIGURED fixture source surfaces as a pydantic
    ``ValidationError`` and becomes a ``SyncNotConfiguredError`` upstream, never a
    silent no-op.
    """

    model_config = SettingsConfigDict(
        env_prefix="NE_CONNECT_FIXTURE_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    #: Path to the recorded-fixture JSON (the FixtureConnector replay source).
    path: str = ""
    #: The ODCS data-contract owner (a real accountable owner string).
    owner: str = ""
    #: Conservative PII default for a demo corpus.
    pii: PiiClass = PiiClass.PERSONAL
    #: ISO-8601 freshness SLA (a recorded corpus refreshes daily by default).
    freshness_sla: str = "P1D"

    @model_validator(mode="after")
    def _validate(self) -> FixtureSyncSettings:
        if not self.path.strip():
            raise ValueError(
                "fixture connector requires a non-empty path (NE_CONNECT_FIXTURE_PATH — "
                "the recorded-fixture JSON)"
            )
        if not self.owner.strip():
            raise ValueError(
                "fixture connector requires a non-empty owner (NE_CONNECT_FIXTURE_OWNER — "
                "the ODCS data-contract owner)"
            )
        return self


class FixtureSyncOutcome(BaseModel):
    """Immutable outcome of a single :func:`run_fixture_sync`."""

    model_config = ConfigDict(frozen=True)

    report: SyncReport
    mode: str


async def run_fixture_sync(
    settings: FixtureSyncSettings,
    *,
    writer: KnowledgeWrite,
    state_store: ConnectorStateStore,
    audit_sink: AuditSink | None,
    actor: str,
    kpi_tag: str | None = None,
    quarantine: WriteQuarantine | None = None,
    source_free: bool = False,
) -> FixtureSyncOutcome:
    """Run one governed fixture sync (ALWAYS backfill); return its counts outcome.

    ``source_free`` is the registry grant from ``SYNC_SOURCES`` (never a caller's
    opinion): it is what lets the shared runner author ``source_status=no_source``
    on this deployment-supplied corpus. Its fail-closed default is ``False``, so a
    direct call that does not carry the grant writes source-bearing records that
    the read path refuses until they hold a source-authority stamp.
    """
    connector = FixtureConnector.from_path(settings.path)
    contract = OdcsDataContract(
        source=connector.source,
        owner=settings.owner,
        schema_def={"type": "object"},
        freshness_sla=settings.freshness_sla,
        pii=settings.pii,
        permissions=PermissionModelSpec(mode=PermissionMode.SOURCE_ACL),
    )
    runner = SyncRunner(
        connector,
        contract,
        writer,
        state_store,
        actor=actor,
        audit_sink=audit_sink,
        cursor_field=None,
        kpi_tag=kpi_tag,
        quarantine=quarantine,
        source_free=source_free,
        mapper_revision=SOURCE_IDENTITY_MAPPER_REVISION,
    )
    report = await runner.run(mode="backfill")
    return FixtureSyncOutcome(report=report, mode="backfill")
