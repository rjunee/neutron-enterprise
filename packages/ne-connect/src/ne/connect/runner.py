# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-INGEST: the lazy connector-sync registry + governed ingest-stack composer.

Closes the ADR-001 keystone gap P1-4 (2026-07-10 bulletproofing review; SPEC §11
decision 2026-07-10) in the OWNER-APPROVED LIGHTWEIGHT form: NO new long-running
process. This module is the shared, import-cheap surface that BOTH the ne-worker
governed cron seeder (:mod:`ne.worker.ingest`) and the ``ne connect sync`` one-shot
CLI (:mod:`ne.cli.connect`) build on. It only INVOKES the EXISTING per-connector
``run_<source>_sync`` entrypoints on the EXISTING governed ``KnowledgeWriter`` path
(C7 fail-closed ACL derivation) — it does NOT reimplement any connector logic.

IMPORT DISCIPLINE (dlt confinement — the ne-api ``_build_cdc_service`` precedent):
module-level imports are ONLY stdlib + pydantic/pydantic-settings + ``ne.contracts``.
The connector subpackages are NEVER imported at module level — they are named as
dotted ``"module:attr"`` refs in :data:`SYNC_SOURCES` and resolved LAZILY via
:mod:`importlib`, so importing ``ne.connect.runner`` pulls neither ``dlt`` (the sql
connector's heavy closure) nor any provider client. ``ne.brain.write`` /
``ne.events`` / connector imports live INSIDE functions. (Importing this module
does eagerly load ``ne.connect``'s ``__init__`` — which re-exports m365/slack — but
that closure is dlt-free by construction; the guardian test asserts the meaningful
invariant: dlt + the sql connector stay unloaded.)

The per-source sync tool is named ``write.internal.connect.sync_<source>`` — the
``write.internal.`` prefix classifies it at tier 1 under ``tiers.rego`` /
:func:`ne.runtime.actions.tier_for_action` (the flywheel ``governed.py``
``write.internal.flywheel.curation_run`` precedent): an internal, proposals-style
governed write that never applies an external change.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import SettingsConfigDict

from ne.connect.sync_gate import connector_sync_gate
from ne.contracts.config import NeSettings

if TYPE_CHECKING:
    import sqlalchemy as sa

    from ne.brain.lineage import SqlAlchemyDocumentLineageStore
    from ne.brain.write import WriteQuarantine
    from ne.connect.authority_admission import M365AuthorityAdmission
    from ne.connect.bridge import SyncReport
    from ne.connect.state import ConnectorStateStore
    from ne.contracts.governance import AuditSink
    from ne.contracts.knowledge import ChunkStore, Embedder, KnowledgeWrite

__all__ = [
    "SYNC_SOURCES",
    "SYNC_TOOL_PREFIX",
    "ConnectSyncSettings",
    "IngestStack",
    "SourceAuthorityUnavailableError",
    "SourceDisposition",
    "SyncNotConfiguredError",
    "SyncSourceEntry",
    "build_ingest_stack",
    "build_sync_settings",
    "configured_source_name",
    "read_only_source_name",
    "run_configured_sync",
    "sync_tool_name",
]

#: Every per-source sync tool is ``write.internal.connect.sync_<source>``. The
#: ``write.internal.`` prefix ⇒ tier 1 (``tiers.rego`` / ``tier_for_action``).
SYNC_TOOL_PREFIX = "write.internal.connect.sync_"


def sync_tool_name(source: str) -> str:
    """The tier-1 governed tool name that runs one ``source`` sync."""
    return f"{SYNC_TOOL_PREFIX}{source}"


class SourceDisposition(StrEnum):
    """Exhaustive, code-authored source-authority class for one sync source.

    This is the ``ne.api.steward_disclosure.ProposalDisposition`` discipline
    applied to ingest: the class of a source is decided HERE, in reviewed code,
    keyed on the registry name — never on a connector-supplied ``contract.source``
    label, a settings string or anything a record payload can reach. The default
    is the fail-closed middle case, so a source added without a decision is
    withheld rather than disclosed.

    * :attr:`SUPPORTED_SOURCE_BEARING` — an external system whose permissions can
      drift and for which this deployment composes a source-authority producer.
      Its records MUST carry a :class:`~ne.contracts.source_authority.
      SourceAuthorityStamp` to be disclosed.
    * :attr:`UNSUPPORTED_SOURCE_BEARING` — an external system with no authority
      producer. Its stored ACL can silently go stale against the source, so its
      records are withheld at read: authority cannot be established, and saying
      so is the honest answer. THE DEFAULT.
    * :attr:`SOURCE_FREE` — content the deployment itself supplied (a local
      fixture or CSV file it placed and whose ACL it authored). There is no
      external authority head to re-resolve; the governed write's own ACL, which
      the retriever's ACL filter already enforces, IS the disclosure boundary.
      Only this class earns the server-authored ``source_status=no_source`` mark.
    """

    SUPPORTED_SOURCE_BEARING = "supported_source_bearing"
    UNSUPPORTED_SOURCE_BEARING = "unsupported_source_bearing"
    SOURCE_FREE = "source_free"


@dataclass(frozen=True)
class SyncSourceEntry:
    """A lazily-resolved connector-sync entry: dotted ``module:attr`` refs.

    ``settings_ref`` names the connector's ``NeSettings`` subclass and ``run_ref``
    its ``async def run_<source>_sync(settings, *, writer, state_store, audit_sink,
    actor, kpi_tag=...)`` entrypoint. Both are resolved via :mod:`importlib` only
    when a source is actually used, so the registry itself imports nothing heavy.

    ``disposition`` is a per-entry CAPABILITY, not a description. It is the only
    thing that lets :func:`run_configured_sync` pass the authority seam into a
    connector (:attr:`~SourceDisposition.SUPPORTED_SOURCE_BEARING`) or grant it
    the server-authored source-free mark (:attr:`~SourceDisposition.SOURCE_FREE`),
    so no source can acquire either by being configured, renamed or reordered.
    """

    settings_ref: str
    run_ref: str
    disposition: SourceDisposition = SourceDisposition.UNSUPPORTED_SOURCE_BEARING

    @property
    def authority_bearing(self) -> bool:
        """True iff this source may be handed a source-authority producer."""
        return self.disposition is SourceDisposition.SUPPORTED_SOURCE_BEARING

    @property
    def source_free(self) -> bool:
        """True iff this source's records may be marked ``source_status=no_source``."""
        return self.disposition is SourceDisposition.SOURCE_FREE


#: The 9 pull-sync sources. ``cdc`` is DELIBERATELY ABSENT: NE-084 CDC is a
#: PUSH-based Debezium webhook sink (``ne.connect.cdc``), not a pull connector — it
#: has no ``run_*_sync`` entrypoint and is governed via the ne-api ``/cdc`` route,
#: not a scheduled sync. All real syncs share the uniform kwargs signature
#: ``(settings, *, writer, state_store, audit_sink, actor, kpi_tag, quarantine)``
#: (sql's extra ``engine=None`` and the graph connectors' ``transport=None``
#: default fine). ``quarantine`` (NE-CONNECT-CSV) is the write-quarantine lane the
#: bridge parks contract-invalid / duplicate-identity records into — inspectable
#: with the validator's own reason, instead of a bare ``invalid`` counter.
SYNC_SOURCES: Mapping[str, SyncSourceEntry] = {
    "m365": SyncSourceEntry(
        "ne.connect.m365.config:M365ConnectorSettings",
        "ne.connect.m365.sync:run_m365_sync",
        disposition=SourceDisposition.SUPPORTED_SOURCE_BEARING,
    ),
    "teams": SyncSourceEntry(
        "ne.connect.teams.config:TeamsConnectorSettings",
        "ne.connect.teams.sync:run_teams_sync",
    ),
    "slack": SyncSourceEntry(
        "ne.connect.slack.config:SlackConnectorSettings",
        "ne.connect.slack.sync:run_slack_sync",
    ),
    "jira": SyncSourceEntry(
        "ne.connect.jira.config:JiraConnectorSettings",
        "ne.connect.jira.sync:run_jira_sync",
    ),
    "zendesk": SyncSourceEntry(
        "ne.connect.zendesk.config:ZendeskConnectorSettings",
        "ne.connect.zendesk.sync:run_zendesk_sync",
    ),
    "salesforce": SyncSourceEntry(
        "ne.connect.salesforce.config:SalesforceConnectorSettings",
        "ne.connect.salesforce.sync:run_salesforce_sync",
        disposition=SourceDisposition.SUPPORTED_SOURCE_BEARING,
    ),
    "sql": SyncSourceEntry(
        "ne.connect.sql.config:SqlConnectorSettings",
        "ne.connect.sql.sync:run_sql_sync",
    ),
    # fixture/csv are the two lanes whose "source" is a file THIS DEPLOYMENT
    # placed, with an ACL this deployment authored. Nothing external can revoke
    # or re-permission them behind the store, so there is no authority head to
    # re-resolve and the governed write's own ACL is the whole boundary.
    "fixture": SyncSourceEntry(
        "ne.connect.fixture_sync:FixtureSyncSettings",
        "ne.connect.fixture_sync:run_fixture_sync",
        disposition=SourceDisposition.SOURCE_FREE,
    ),
    "csv": SyncSourceEntry(
        "ne.connect.csvfile.config:CsvConnectorSettings",
        "ne.connect.csvfile.sync:run_csv_sync",
        disposition=SourceDisposition.SOURCE_FREE,
    ),
}


class SyncNotConfiguredError(Exception):
    """A LISTED source whose connector settings are absent/invalid in the env.

    Raised (naming the source) when a source's ``NeSettings`` subclass fails its
    own fail-loud validator (a pydantic ``ValidationError``) — e.g. the fixture
    source with no ``NE_CONNECT_FIXTURE_PATH``/``OWNER``. Distinct from an UNKNOWN
    source (a plain ``ValueError`` listing the known names).
    """


class SourceAuthorityUnavailableError(RuntimeError):
    """An authority-enabled source has no correctly typed producer capability."""


class ConnectSyncSettings(NeSettings):
    """Which connector sources are configured, and which receive a cron schedule.

    Mirrors ``WorkerSettings``: ``env_prefix='NE_CONNECT_SYNC_'``, ``extra='forbid'``
    (a typo fails loud), TOML-layered. ``sources`` / ``cron_overrides`` are parsed
    from JSON strings in the env (pydantic-settings): e.g.
    ``NE_CONNECT_SYNC_SOURCES='["fixture","m365"]'`` and
    ``NE_CONNECT_SYNC_CRON_OVERRIDES='{"m365":"*/15 * * * *"}'``. Fail-closed
    default: ``sources=()`` ⇒ nothing seeded, nothing composed, a clean no-op.
    """

    model_config = SettingsConfigDict(
        env_prefix="NE_CONNECT_SYNC_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    #: Configured pull sources exposed through status. Runnable capability is
    #: governed independently by ``runnable_sources``.
    #: Every entry MUST be a :data:`SYNC_SOURCES` key. Empty ⇒ fail-closed no-op.
    sources: tuple[str, ...] = ()
    #: Optional exact subset of ``sources`` runnable through recurring worker
    #: jobs and governed manual job requests. ``None`` preserves the historical
    #: all-sources behavior. An explicit empty tuple makes every configured
    #: source a frozen snapshot/status lane and removes connector job capability.
    #: A configured source outside this subset keeps its descriptor and retained
    #: corpus readable, but it performs no provider pull and exposes no manual
    #: request binding; status must label that posture truthfully.
    runnable_sources: tuple[str, ...] | None = None
    #: The default cron for every source (hourly — matches the connectors'
    #: ``freshness_sla`` PT1H defaults). Cron SYNTAX is NOT validated here (cronsim
    #: is not a ne-connect dep); ``CronScheduler.schedule`` fails loud at seed time.
    cron: str = "0 * * * *"
    #: Per-source cron overrides (the SPEC 'interval config-surfaced per source').
    #: Every key MUST be a configured source. An explicit entry is AUTHORITATIVE:
    #: it outranks the cadence an authority-bearing source would otherwise derive
    #: from its sweep plan (``ne.worker.ingest._seeded_cron``).
    cron_overrides: dict[str, str] = Field(default_factory=dict)
    #: The connector state (cursor + dedup) sqlite path. REQUIRED when sources set.
    state_path: str = ""
    #: The write-quarantine sqlite path. REQUIRED when sources set.
    quarantine_path: str = ""
    #: Optional KPI tag threaded into every sync run + audit event (C8).
    kpi_tag: str = ""

    @model_validator(mode="after")
    def _validate(self) -> ConnectSyncSettings:
        known = set(SYNC_SOURCES)
        unknown = [s for s in self.sources if s not in known]
        if unknown:
            raise ValueError(
                f"unknown connector source(s) {unknown!r}; valid sources are {sorted(known)!r}"
            )
        if len(set(self.sources)) != len(self.sources):
            raise ValueError(f"duplicate connector source(s) in {list(self.sources)!r}")
        if self.runnable_sources is not None:
            if len(set(self.runnable_sources)) != len(self.runnable_sources):
                raise ValueError(
                    f"duplicate runnable connector source(s) in {list(self.runnable_sources)!r}"
                )
            unsourced = [source for source in self.runnable_sources if source not in self.sources]
            if unsourced:
                raise ValueError(f"runnable_sources names non-configured source(s) {unsourced!r}")
            canonical_runnable = tuple(
                source for source in self.sources if source in set(self.runnable_sources)
            )
            if self.runnable_sources != canonical_runnable:
                raise ValueError("runnable_sources must follow configured source order")
        orphan = [s for s in self.cron_overrides if s not in self.sources_to_run]
        if orphan:
            raise ValueError(
                f"cron_overrides names non-runnable source(s) {orphan!r}; a cron "
                "override may only target a runnable source"
            )
        if self.sources and (not self.state_path or not self.quarantine_path):
            raise ValueError(
                "NE_CONNECT_SYNC_STATE_PATH and NE_CONNECT_SYNC_QUARANTINE_PATH are "
                "REQUIRED when NE_CONNECT_SYNC_SOURCES is non-empty (the CDC "
                "required-when-enabled precedent)"
            )
        return self

    @property
    def sources_to_run(self) -> tuple[str, ...]:
        """Runnable subset, defaulting compatibly to every configured source."""
        if self.runnable_sources is None:
            return self.sources
        selected = frozenset(self.runnable_sources)
        return tuple(source for source in self.sources if source in selected)

    def cron_for(self, source: str) -> str:
        """The cron for ``source`` — its override if any, else the default ``cron``."""
        return self.cron_overrides.get(source, self.cron)


class _HasReport(Protocol):
    """The uniform shape of every ``*SyncOutcome`` — carries a :class:`SyncReport`."""

    report: SyncReport


def _resolve_entry(source: str) -> SyncSourceEntry:
    try:
        return SYNC_SOURCES[source]
    except KeyError as exc:
        raise ValueError(
            f"unknown connector source {source!r}; valid sources are {sorted(SYNC_SOURCES)!r}"
        ) from exc


def _load_ref(dotted: str) -> Any:
    """Resolve a dotted ``module:attr`` ref via importlib (lazy connector import)."""
    module_name, _, attr = dotted.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def build_sync_settings(source: str, *, settings: Any | None = None) -> Any:
    """Resolve + construct ``source``'s connector settings from the environment.

    An UNKNOWN source raises ``ValueError`` (listing the known names). A LISTED
    source whose fail-loud validator rejects the current env (a pydantic
    ``ValidationError``) becomes a :class:`SyncNotConfiguredError` naming the source
    — so a misconfigured-but-listed source fails LOUD at worker boot / CLI start
    rather than silently no-op'ing. ``settings`` short-circuits construction (a
    pre-built settings object is returned as-is).
    """
    entry = _resolve_entry(source)
    if settings is not None:
        return settings
    settings_cls = _load_ref(entry.settings_ref)
    try:
        return settings_cls()  # type: ignore[call-arg]  # env-driven
    except ValidationError as exc:
        raise SyncNotConfiguredError(
            f"connector source {source!r} is not configured: {exc.error_count()} "
            "settings error(s) (set its prefixed connector env, e.g. NE_JIRA_*; the "
            "fixture source uses NE_CONNECT_FIXTURE_*)"
        ) from exc


def configured_source_name(source: str, *, settings: Any | None = None) -> str:
    """Return the configured connector's canonical executable identity.

    ``source`` is the deployment registry alias (for example ``"m365"``), while
    connector contracts may deliberately stamp another configured identity (the
    M365 default is ``"m365-sharepoint"``). Executable paths are strict: a listed
    but unconfigured connector raises :class:`SyncNotConfiguredError` and therefore
    cannot acquire a request binding under an invented public-alias identity.
    """
    resolved = build_sync_settings(source, settings=settings)
    name = getattr(resolved, "source_name", source)
    if not isinstance(name, str) or not name.strip():
        raise SyncNotConfiguredError(
            f"connector source {source!r} has an empty configured source_name"
        )
    return name


def read_only_source_name(source: str) -> str:
    """Resolve state/audit identity, falling back only for read-only inspection.

    Status and quarantine remain inspectable when a listed connector's executable
    credentials are absent. The fallback is deliberately separate from
    :func:`configured_source_name` so it cannot create a tool binding or capability.
    """
    try:
        return configured_source_name(source)
    except SyncNotConfiguredError:
        return source


async def run_configured_sync(
    source: str,
    *,
    writer: KnowledgeWrite,
    state_store: ConnectorStateStore,
    audit_sink: AuditSink | None,
    actor: str,
    kpi_tag: str | None = None,
    settings: Any | None = None,
    quarantine: WriteQuarantine | None = None,
    authority: Any | None = None,
    chunk_reader: Any | None = None,
    on_lock_wait: Callable[[str], None] | None = None,
    post_sync: Callable[[], Awaitable[None]] | None = None,
) -> SyncReport:
    """Run ONE governed sync for ``source`` and return its counts-only report.

    Resolves + constructs the connector settings (see :func:`build_sync_settings`),
    then awaits the connector's EXISTING ``run_<source>_sync`` on the passed
    governed ``writer`` / ``state_store`` / ``audit_sink`` — the C7 fail-closed ACL
    derivation and the ``connector.sync`` audit event are the connector's own,
    untouched. ``quarantine`` (the SAME lane the writer parks shape failures
    into — ``IngestStack.quarantine``) lets the bridge park contract-invalid and
    duplicate-identity records inspectably. Returns ``outcome.report`` (every
    ``*SyncOutcome`` carries one).

    NE-CONNECT-SEEDLOCK: the whole run is held inside the cross-process gate keyed
    on ``state_store.path`` (:mod:`ne.connect.sync_gate`). This is the ONE
    chokepoint every sync goes through — the ne-worker cron tool and the ``ne
    connect sync`` one-shot both land here — so gating it makes it structurally
    impossible for two processes to write one connector state database at once,
    which is what produced ``database is locked`` on every demo bring-up. A store
    with no on-disk path (``:memory:``) is ungated. ``on_lock_wait`` is invoked
    once if the gate is actually contended, so a wait is visible rather than
    silent.

    ``post_sync`` runs INSIDE the same gate hold, after the connector run
    succeeds. It exists for follow-up work that writes the SAME shared state
    store — concretely the worker's m365 Tier-2 graph projection, which opens
    its own long ``BEGIN IMMEDIATE`` mapper refresh on that store. Run OUTSIDE
    the gate (as the worker once did, after this function returned), that
    refresh overlapped sibling connector syncs admitted by later ticks, and a
    sibling's writes then failed against the foreign refresh every tick for the
    whole crawl. Holding the gate across the follow-up keeps "the ONE chokepoint
    every sync goes through" true for every writer of the state database, not
    just the connector body. A ``post_sync`` failure propagates exactly as a
    connector failure does — the run is recorded failed.
    """
    entry = _resolve_entry(source)
    resolved = build_sync_settings(source, settings=settings)
    authority_enabled = bool(getattr(resolved, "authority_enabled", False))
    if entry.authority_bearing and authority_enabled and authority is None:
        raise SourceAuthorityUnavailableError(
            f"{source} requested source authority but its producer is unavailable"
        )
    if authority is not None:
        if source == "m365":
            from ne.connect.authority_admission import M365AuthorityAdmission

            if not isinstance(authority, M365AuthorityAdmission):
                raise TypeError("m365 received a foreign source-authority capability")
        elif source == "salesforce":
            from ne.connect.salesforce.authority import SalesforceAuthorityFactory

            if not isinstance(authority, SalesforceAuthorityFactory):
                raise TypeError("salesforce received a foreign source-authority capability")
    run_fn = _load_ref(entry.run_ref)
    extra: dict[str, Any] = {}
    if entry.authority_bearing and authority is not None:
        extra["authority"] = authority
        # Travels with the authority seam and only with it: the read is there to
        # keep a stamped chunk from being rewritten on every sweep, and a source
        # that cannot be handed authority has no stamp to apply.
        if chunk_reader is not None:
            extra["chunk_reader"] = chunk_reader
    # The server-authored source-free mark is granted the same way the authority
    # seam is: only the registry can hand it out, and only to an entry whose
    # disposition says so. A connector never asks for it and cannot assert it.
    if entry.source_free:
        extra["source_free"] = True
    async with connector_sync_gate(
        getattr(state_store, "path", None), source=source, on_wait=on_lock_wait
    ):
        outcome: _HasReport = await run_fn(
            resolved,
            writer=writer,
            state_store=state_store,
            audit_sink=audit_sink,
            actor=actor,
            kpi_tag=kpi_tag,
            quarantine=quarantine,
            **extra,
        )
        if post_sync is not None:
            await post_sync()
    return outcome.report


@dataclass
class IngestStack:
    """The governed write stack shared by every sync in one process root."""

    writer: KnowledgeWrite
    state_store: ConnectorStateStore
    #: The write-quarantine lane the writer parks into — exposed so callers can
    #: hand the SAME lane to ``run_configured_sync(quarantine=...)`` (the bridge's
    #: contract-invalid / duplicate parks land beside the writer's shape parks).
    quarantine: WriteQuarantine | None = None
    #: The ONE composed M365 authority producer, or ``None``. This is the only
    #: production carrier of authority-issuance capability: the API never builds
    #: one, and a source wrapper cannot reach the Writer inside it.
    authority: M365AuthorityAdmission | None = None
    #: Why authority is absent although the connector asked for it. A non-empty
    #: reason means the M365 scope stays Unknown and must not be advertised — it
    #: is NOT permission to sync the source without authority.
    authority_unavailable_reason: str | None = None
    #: Organization-late-bound Salesforce capability. It cannot construct an
    #: admission until the sync validates the provider OAuth identity.
    salesforce_authority_factory: Any | None = None
    salesforce_authority_unavailable_reason: str | None = None
    #: Narrow READ seam over the SAME composed chunk store ``writer`` writes to,
    #: handed only to an authority-bearing source so its bridge can tell whether a
    #: chunk already carries the stamp its re-admission would apply. It confers no
    #: query, delete or bulk-read reach (see ``ne.connect.bridge.ChunkReader``).
    chunk_reader: Any | None = None

    def authority_for(self, source: str) -> Any | None:
        """Return only the producer capability owned by ``source``."""
        if source == "m365":
            return self.authority
        if source == "salesforce":
            return self.salesforce_authority_factory
        return None

    def authority_unavailable_for(self, source: str) -> str | None:
        if source == "m365":
            return self.authority_unavailable_reason
        if source == "salesforce":
            return self.salesforce_authority_unavailable_reason
        return None


def build_ingest_stack(
    *,
    chunk_store: ChunkStore,
    runtime_engine: sa.Engine,
    sink: AuditSink,
    settings: ConnectSyncSettings,
    embedder: Embedder | None = None,
    document_lineage: SqlAlchemyDocumentLineageStore | None = None,
) -> IngestStack:
    """Build the governed ``KnowledgeWriter`` + state store — the CDC writer recipe.

    EXACTLY mirrors ne-api ``_build_cdc_service``'s writer recipe over the SAME
    composed ``chunk_store`` so cron + CLI syncs write through the EXISTING governed
    path (C7): a ``SqliteWriteQuarantine``, the ``CONNECTOR_RECORD_SHAPE`` contract,
    ``ProvenanceClass.INGESTED``, the shared ``audit_sink``, and an
    ``OutboxShapeViolationSink`` over the runtime engine. Connector ACL/write logic
    is untouched — this only assembles the writer the connectors write INTO.
    """
    from ne.brain.lineage import SqlAlchemyDocumentLineageStore
    from ne.brain.write import (
        KnowledgeWriter,
        OutboxShapeViolationSink,
        ProvenanceClass,
        SqliteWriteQuarantine,
    )
    from ne.connect import CONNECTOR_RECORD_SHAPE
    from ne.connect.state import ConnectorStateStore
    from ne.events.outbox import ensure_outbox_schema

    quarantine = SqliteWriteQuarantine(Path(settings.quarantine_path))
    ensure_outbox_schema(runtime_engine)  # idempotent; OutboxShapeViolationSink needs it
    writer = KnowledgeWriter(
        chunk_store,
        quarantine,
        {CONNECTOR_RECORD_SHAPE.name: CONNECTOR_RECORD_SHAPE},
        default_shape=CONNECTOR_RECORD_SHAPE.name,
        actor="connect-sync",
        default_provenance_class=ProvenanceClass.INGESTED,
        audit_sink=sink,
        violation_sink=OutboxShapeViolationSink(runtime_engine),
        lineage=SqlAlchemyDocumentLineageStore(runtime_engine),
    )
    authority, unavailable = _compose_m365_authority(
        runtime_engine=runtime_engine,
        chunk_store=chunk_store,
        settings=settings,
        embedder=embedder,
        document_lineage=document_lineage,
    )
    salesforce_factory, salesforce_unavailable = _compose_salesforce_authority_factory(
        runtime_engine=runtime_engine,
        chunk_store=chunk_store,
        settings=settings,
        embedder=embedder,
        document_lineage=document_lineage,
    )

    async def _read_one_chunk(item_id: str) -> Any:
        found = await chunk_store.get([item_id])
        return found[0] if found else None

    return IngestStack(
        writer=writer,
        state_store=ConnectorStateStore(settings.state_path),
        quarantine=quarantine,
        chunk_reader=_read_one_chunk,
        authority=authority,
        authority_unavailable_reason=unavailable,
        salesforce_authority_factory=salesforce_factory,
        salesforce_authority_unavailable_reason=salesforce_unavailable,
    )


def _compose_salesforce_authority_factory(
    *,
    runtime_engine: sa.Engine,
    chunk_store: ChunkStore,
    settings: ConnectSyncSettings,
    embedder: Embedder | None,
    document_lineage: SqlAlchemyDocumentLineageStore | None,
) -> tuple[Any | None, str | None]:
    """Compose Salesforce's provider-organization-late-bound capability."""
    if "salesforce" not in settings.sources:
        return None, None
    salesforce_settings = build_sync_settings("salesforce")
    if not getattr(salesforce_settings, "authority_enabled", False):
        return None, None
    if embedder is None:
        return None, (
            "the Salesforce authority producer needs the composed embedder to create "
            "immutable artifacts; the scope stays Unknown"
        )

    from ne.brain.current_source_authority import (
        SqlAlchemyCurrentSourceAuthorityWriter,
        bind_deployment_construction_id,
        ensure_source_authority_schema,
    )
    from ne.brain.write import ImmutableKnowledgeArtifactStore, ensure_immutable_artifact_schema
    from ne.connect.authority_admission import (
        SqlAlchemyAdmissionAttemptStore,
        SqlAlchemyHierarchicalAdmission,
        SqlAlchemySourceContractHeadStore,
        SweepCadenceError,
        ensure_admission_schema,
    )
    from ne.connect.salesforce.authority import SalesforceAuthorityFactory
    from ne.connect.salesforce.contract import (
        salesforce_contract_sla_seconds,
        salesforce_sweep_plan,
    )

    sla_seconds = salesforce_contract_sla_seconds(salesforce_settings)
    plan = salesforce_sweep_plan(salesforce_settings)
    if plan is None:
        return None, "the Salesforce permission sweep is not fully configured"
    try:
        plan.require_sub_sla(sla_seconds)
    except SweepCadenceError as exc:
        return None, str(exc)

    ensure_source_authority_schema(runtime_engine)
    ensure_immutable_artifact_schema(runtime_engine)
    ensure_admission_schema(runtime_engine)
    construction_id = bind_deployment_construction_id(runtime_engine)
    factory = SalesforceAuthorityFactory(
        engine=runtime_engine,
        writer=SqlAlchemyCurrentSourceAuthorityWriter(
            runtime_engine, construction_id, freshness_sla_seconds=sla_seconds
        ),
        artifacts=ImmutableKnowledgeArtifactStore(
            runtime_engine,
            cast(Any, chunk_store),
            embedder,  # type: ignore[arg-type]
        ),
        hierarchy=SqlAlchemyHierarchicalAdmission(runtime_engine),
        attempts=SqlAlchemyAdmissionAttemptStore(runtime_engine),
        contract_heads=SqlAlchemySourceContractHeadStore(runtime_engine),
        deployment_id=construction_id,
        settings=salesforce_settings,
        lineage=document_lineage,
    )
    return factory, None


def _compose_m365_authority(
    *,
    runtime_engine: sa.Engine,
    chunk_store: ChunkStore,
    settings: ConnectSyncSettings,
    embedder: Embedder | None,
    document_lineage: SqlAlchemyDocumentLineageStore | None,
) -> tuple[M365AuthorityAdmission | None, str | None]:
    """Compose the one M365 authority producer, or say exactly why there is none.

    A refusal returns a REASON rather than raising: a sweep that cannot beat its
    SLA, or a missing embedder, must leave the M365 scope Unknown and
    unadvertised, not take down an unrelated connector's cron in the same root.
    """
    from ne.brain.current_source_authority import (
        SqlAlchemyCurrentSourceAuthorityWriter,
        bind_deployment_construction_id,
        ensure_source_authority_schema,
    )
    from ne.brain.write import ImmutableKnowledgeArtifactStore, ensure_immutable_artifact_schema
    from ne.connect.authority_admission import (
        M365AuthorityAdmission,
        SourceAuthorityLineageSink,
        SqlAlchemyAdmissionAttemptStore,
        SqlAlchemyHierarchicalAdmission,
        SqlAlchemySourceContractHeadStore,
        SweepCadenceError,
        claim_or_load_connector_instance,
        connector_instance_scope,
        ensure_admission_schema,
    )
    from ne.connect.m365.contract import (
        m365_admitted_target_digest,
        m365_contract_identity,
        m365_contract_sla_seconds,
        m365_sweep_plan,
    )

    if "m365" not in settings.sources:
        return None, None
    m365_settings = build_sync_settings("m365")
    if not getattr(m365_settings, "authority_enabled", False):
        return None, None
    if embedder is None:
        return None, (
            "the M365 authority producer needs the composed embedder to create "
            "immutable artifacts; the scope stays Unknown"
        )
    sla_seconds = m365_contract_sla_seconds(m365_settings)
    plan = m365_sweep_plan(m365_settings)
    if plan is None:  # pragma: no cover - settings validation already requires all four
        return None, "the M365 permission sweep is not fully configured"
    try:
        plan.require_sub_sla(sla_seconds)
    except SweepCadenceError as exc:
        return None, str(exc)

    ensure_source_authority_schema(runtime_engine)
    ensure_immutable_artifact_schema(runtime_engine)
    ensure_admission_schema(runtime_engine)
    construction_id = bind_deployment_construction_id(runtime_engine)
    instance = claim_or_load_connector_instance(
        runtime_engine,
        deployment_id=construction_id,
        source_name=m365_settings.source_name,
        admitted_target_digest=m365_admitted_target_digest(m365_settings),
    )
    head_store = SqlAlchemySourceContractHeadStore(runtime_engine)
    head = head_store.claim_or_load(
        connector_instance_scope(
            deployment_id=construction_id,
            connector_instance_id=instance.connector_instance_id,
            graph_tenant_id=m365_settings.tenant_id,
        ),
        source_generation=instance.source_generation,
        contract=m365_contract_identity(m365_settings),
    )
    admission = M365AuthorityAdmission(
        engine=runtime_engine,
        writer=SqlAlchemyCurrentSourceAuthorityWriter(
            runtime_engine, construction_id, freshness_sla_seconds=sla_seconds
        ),
        artifacts=ImmutableKnowledgeArtifactStore(runtime_engine, chunk_store, embedder),  # type: ignore[arg-type]
        hierarchy=SqlAlchemyHierarchicalAdmission(runtime_engine),
        attempts=SqlAlchemyAdmissionAttemptStore(runtime_engine),
        contract_heads=head_store,
        contract_head=head,
        instance=instance,
        deployment_id=construction_id,
        graph_tenant_id=m365_settings.tenant_id,
        source_name=m365_settings.source_name,
    )
    if document_lineage is not None:
        lineage: SourceAuthorityLineageSink = document_lineage
        admission.bind_lineage(lineage)
    return admission, None
