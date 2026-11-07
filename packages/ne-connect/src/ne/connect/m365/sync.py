# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""run_m365_sync — drive the M365 connector through the governed write path (NE-044).

Wires :class:`~ne.connect.m365.connector.M365GraphConnector` into the framework
:class:`~ne.connect.bridge.SyncRunner`. Durability contract: the per-drive
``deltaLink`` tokens are committed to the ledger ONLY AFTER a successful run. A
run with quarantined items (including per-item permissions-fetch quarantines)
STILL commits its deltaLink — progress is durable and a single wedged item cannot
stall the crawl. Only an ABORTED run (throttling exhaustion, a 5xx, or a crash)
leaves the OLD links in place; the next run re-enumerates from them and the dedup
ledger skips already-written items — no progress is lost, no duplicate is written.
``cursor_field=None`` because resumability comes from the deltaLink + dedup ledger,
not lexicographic record cursors.

The SyncRunner emits exactly ONE ``config_hash``-stamped ``connector.sync``
AuditEvent (contract untouched). ACL quarantines are surfaced as ONE additional
``connector.acl_quarantine`` ``denied`` event via the NE-005 event spine.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.brain.current_source_authority import (
    SourceAuthorityClaimConflictError,
    SourceReconciliationScopeBusyError,
)
from ne.brain.ingest.contracts import DocumentParser
from ne.brain.parsed_evidence import (
    PARSED_RECORD_METADATA_KEY,
    ParsedDocumentEvidenceDescriptor,
)
from ne.brain.write import CuratedKnowledgeItem, WriteQuarantine
from ne.connect.authority_admission import (
    M365AuthorityAdmission,
    M365DeletionObservation,
    SqlAlchemyHierarchicalAdmission,
    SqlAlchemySweepCursor,
    SweepCadencePlan,
    drive_scope,
    observed_permissions,
    reject_overlapping_drive_scopes,
    site_scope,
)
from ne.connect.bridge import (
    ChunkReader,
    SyncReport,
    SyncRunner,
    canonical_item_id,
    canonical_payload_json,
    clear_stale_source_links,
    default_record_mapper,
)
from ne.connect.contracts import OdcsDataContract
from ne.connect.endpoints import validate_source_url
from ne.connect.identity import (
    SOURCE_ACL_METADATA_KEY,
    SOURCE_SNAPSHOT_METADATA_KEY,
    source_acl_hash,
    source_ref_for,
    source_snapshot_hash,
)
from ne.connect.m365.acl import map_permissions
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.connector import M365GraphConnector, ResolvedPermissions
from ne.connect.m365.contract import (
    m365_contract_sla_seconds,
    m365_data_contract,
    m365_source_link_mapper_revision,
    m365_sweep_plan,
)
from ne.connect.m365.graph import Client, Transport
from ne.connect.source_url import canonical_external_source_url
from ne.connect.state import ConnectorStateStore
from ne.contracts.governance import AuditSink
from ne.contracts.integration import Record
from ne.contracts.knowledge import KnowledgeWrite
from ne.contracts.source_authority import SourceReconciliationClaim, SourceReconciliationScope
from ne.events import new_audit_event
from ne.net.ssrf import normalize_host
from ne.schema import AuditOutcome

__all__ = ["M365SyncOutcome", "make_m365_record_mapper", "run_m365_sync"]

_LOG = logging.getLogger(__name__)

_TARGET_BINDING_VERSION = 1

_DELTA_PREFIX = "delta:"

#: The reconciliation lease one crawl holds over a site or drive scope.
#:
#: The lease does two unrelated jobs and they pull in opposite directions. While
#: the crawl RUNS it is a mutex, and it wants to be long enough to cover the
#: work. After the crawl DIES it is a dead-man switch — a worker killed mid-flight
#: never reaches its ``finally``, so the scope stays blocked for whatever is left
#: of the lease — and that wants to be as short as possible.
#:
#: Sizing it to the mutex job alone is what produced the 2026-08-27 outage: a
#: crawl interrupted at 10:06:02 (``ne_job_runs`` recorded ``InterruptedJobRun``)
#: held its scope until 10:21, and the ``*/2`` sweep fired into it seven times.
#: The lease was 900s for work that takes ~2s when incremental and 8m09s on a
#: full 68-point backfill (10:22:04 → 10:30:13, measured on the same box).
#:
#: :data:`_AUTHORITY_LEASE_RENEW_SECONDS` splits the two jobs apart, so this
#: number now answers ONE question — how long a DEAD holder blocks the scope —
#: and is derived from the sweep cadence rather than from the crawl:
#:
#: * ``NE_M365_AUTHORITY_SWEEP_CADENCE_SECONDS`` defaults to 120, so the seeded
#:   cron is ``*/2``. 180s is 1.5 cadences: a killed crawl costs AT MOST one
#:   skipped firing, where 900s cost seven.
#: * It must survive missed renewals rather than expire under a slow page. At a
#:   60s renewal interval it tolerates two consecutive misses — the whole of one
#:   Graph request timeout — before the scope is considered abandoned.
#: * Below ~2 cadences there is no honest floor left: an expiry mid-crawl costs
#:   the run (``finalize`` refuses an expired claim), and the renewal loop needs
#:   room to be late without being wrong.
#:
#: A crawl LONGER than this is fine and expected — it renews. Only a crawl that
#: has stopped renewing, i.e. one whose process is gone, gives the scope up.
_AUTHORITY_LEASE_SECONDS = 180

#: How often a live crawl proves it still holds its scopes.
#:
#: One third of the lease, so two consecutive renewals can fail — or simply be
#: late behind a blocking Graph page — before the lease lapses. Renewal never
#: advances the epoch or the fencing token, so it cannot steal a scope from a
#: successor; if this claim has already lost the scope the CAS fails and the
#: renewal loop stops, leaving the crawl to fail loudly at its next ``finalize``
#: (which independently refuses an expired or superseded claim).
_AUTHORITY_LEASE_RENEW_SECONDS = 60


class M365SyncOutcome(BaseModel):
    """Immutable outcome of a single :func:`run_m365_sync`."""

    model_config = ConfigDict(frozen=True)

    report: SyncReport
    acl_quarantined: int
    mode: str
    #: True when this firing did NO work because another reconciler holds the
    #: scope. The report's zeros then mean "not attempted", never "nothing found":
    #: no cursor advances, no deltaLink commits and no ``connector.sync`` event is
    #: emitted, so freshness keeps ageing from the last run that actually swept.
    scope_busy: bool = False


def _parsed_document_prose(payload: Mapping[str, object]) -> str:
    """Render a parsed record's document prose for searchable chunk text.

    Prefers the parser's structural ``body_blocks`` (join of the stripped,
    non-empty block texts) and falls back to the pre-joined ``body`` string.
    Both renderings are the SAME join by construction (``m365/content.py``
    builds ``body`` exactly this way), which keeps ``sha256(chunk.text)`` equal
    to lineage's document-version content hash (``_snapshot`` hashes ``body``)
    and to ``answer_ledger._lineage_content_hash`` — do not add markup here
    without revisiting both.

    Returns ``""`` when no prose can be rendered; the caller then keeps the
    canonical-JSON content rather than writing an empty chunk.
    """
    blocks = payload.get("body_blocks")
    if isinstance(blocks, Sequence) and not isinstance(blocks, (str, bytes)):
        texts = [
            text.strip()
            for block in blocks
            if isinstance(block, Mapping)
            and isinstance(text := block.get("text"), str)
            and text.strip()
        ]
        if texts:
            return "\n\n".join(texts)
    body = payload.get("body")
    if isinstance(body, str) and body.strip():
        return body
    return ""


def make_m365_record_mapper(
    settings: M365ConnectorSettings,
    *,
    take_parsed_evidence: Callable[
        [str, str, dict[str, object], tuple[str, ...], str],
        ParsedDocumentEvidenceDescriptor | None,
    ]
    | None = None,
) -> Callable[[Record, OdcsDataContract, tuple[str, ...]], CuratedKnowledgeItem]:
    """Build the driveItem mapper with optional validated source provenance."""

    def _map(
        record: Record, contract: OdcsDataContract, acl: tuple[str, ...]
    ) -> CuratedKnowledgeItem:
        payload = dict(record.payload)
        parsed = payload.get("body_ingestion_status") == "parsed"
        source_url = canonical_external_source_url(
            payload.pop("web_url", None),
            allowed_hosts=settings.source_link_allowed_hosts,
        )
        # The upstream URL envelope never becomes searchable content. Only the
        # normalized, host-bound locator may survive as optional metadata.
        item = default_record_mapper(record.model_copy(update={"payload": payload}), contract, acl)
        # A PARSED document's searchable text is the document PROSE, never the
        # record envelope. ``default_record_mapper`` serializes the whole Graph
        # record — ETag, ULIDs, parser hashes, timestamps, ``body_blocks`` — as
        # canonical JSON ``content``, which drowned the ~400 chars of policy
        # prose in ~1,200 chars of scaffolding and left the SOP unrankable for
        # the exact queries it governs. The prose is rendered from the parser's
        # ``body_blocks`` (falling back to the pre-joined ``body`` string; both
        # are the same join by construction in ``m365/content.py``), and the
        # exact record payload moves WHOLE into metadata under
        # ``PARSED_RECORD_METADATA_KEY`` so the writer's parsed-evidence
        # binding and lineage's document-version snapshot keep reading the
        # identical payload they used to parse out of ``chunk.text``.
        # Non-parsed records (the structured ``.negraph.json`` operational
        # rows) keep their canonical-JSON content byte-identical.
        prose = _parsed_document_prose(payload) if parsed else None
        if prose:
            item = CuratedKnowledgeItem.model_validate(
                {
                    **item.model_dump(),
                    "content": prose,
                    "metadata": {
                        **item.metadata,
                        PARSED_RECORD_METADATA_KEY: json.loads(canonical_payload_json(payload)),
                    },
                }
            )
        descriptor = (
            take_parsed_evidence(
                record.source_id,
                record.record_id,
                payload,
                acl,
                item.item_id,
            )
            if take_parsed_evidence is not None
            else None
        )
        if parsed and descriptor is None:
            raise ValueError("M365 parsed record is missing exact parser evidence")
        if not parsed and descriptor is not None:
            raise ValueError("M365 parser evidence does not match record state")
        metadata = dict(item.metadata)
        metadata.update(
            {
                "source_system": contract.source,
                "source_ref": source_ref_for(record),
                SOURCE_ACL_METADATA_KEY: source_acl_hash(acl),
                SOURCE_SNAPSHOT_METADATA_KEY: source_snapshot_hash(contract.source, record),
            }
        )
        if source_url is not None:
            metadata["source_url"] = source_url
        return CuratedKnowledgeItem.model_validate(
            {
                **item.model_dump(),
                "metadata": metadata,
                "parsed_source_evidence": descriptor,
            }
        )

    return _map


def _m365_target_binding_digest(settings: M365ConnectorSettings) -> str:
    """Hash the exact credential-free cloud, authority, tenant, and crawl target."""

    def _authority(value: str) -> str:
        parsed = urlsplit(validate_source_url(value, allow_external=True))
        host = normalize_host(parsed.hostname or "")
        netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))

    payload = json.dumps(
        {
            "connector": "m365",
            "target_binding_version": _TARGET_BINDING_VERSION,
            "source": settings.source_name,
            "cloud": {
                "graph_authority": _authority(settings.graph_base_url),
                "login_authority": _authority(settings.login_base_url),
            },
            "tenant_id": settings.tenant_id.strip().lower(),
            "site_ids": sorted(set(settings.site_ids)),
            "drive_ids": sorted(set(settings.drive_ids)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class _M365AuthorityDriver:
    """Sequence the hierarchical claim/reconcile steps around one crawl.

    It exists so ``run_m365_sync`` reads as the ORDER the SPEC requires: claim
    the site parent, discover, reject an overlapping drive, claim each drive,
    crawl, tombstone observed deletions, reconcile the descendant union, and
    only then let the delta cursor commit. The driver holds no Writer of its own
    — every authority-advancing call goes through the single admission object.
    """

    def __init__(
        self,
        admission: M365AuthorityAdmission,
        *,
        engine: sa.Engine,
        settings: M365ConnectorSettings,
        deployment_id: str,
        connector_instance_id: str,
    ) -> None:
        self._admission = admission
        self._engine = engine
        self._settings = settings
        self._hierarchy = SqlAlchemyHierarchicalAdmission(engine)
        self._cursors = SqlAlchemySweepCursor(engine)
        self._deployment_id = deployment_id
        self._connector_instance_id = connector_instance_id
        self._site_claims: dict[str, SourceReconciliationClaim] = {}
        self._drive_claims: dict[str, SourceReconciliationClaim] = {}
        #: The heartbeat that keeps this run's SHORT leases alive. It starts with
        #: the first claim and is cancelled in :meth:`release`; a process that
        #: dies takes it with it, which is exactly the signal the dead-man lease
        #: is reading.
        self._renewals: asyncio.Task[None] | None = None

    def _site_scope(self, site_id: str) -> SourceReconciliationScope:
        return site_scope(
            deployment_id=self._deployment_id,
            connector_instance_id=self._connector_instance_id,
            graph_tenant_id=self._settings.tenant_id,
            site_id=site_id,
        )

    def _drive_scope(self, drive_id: str) -> SourceReconciliationScope:
        return drive_scope(
            deployment_id=self._deployment_id,
            connector_instance_id=self._connector_instance_id,
            graph_tenant_id=self._settings.tenant_id,
            drive_id=drive_id,
        )

    async def _claim(self, scope: SourceReconciliationScope) -> SourceReconciliationClaim:
        claim = await self._admission.claim_scope(scope, lease_seconds=_AUTHORITY_LEASE_SECONDS)
        self._start_renewals()
        return claim

    def _start_renewals(self) -> None:
        """Begin proving liveness, once, on the first claim this run takes."""
        if self._renewals is None:
            self._renewals = asyncio.create_task(self._renew_forever())

    async def _renew_forever(self) -> None:
        """Extend every held lease until the run releases them — or dies.

        A crawl legitimately outlives its lease (a full backfill of a 68-point
        drive measured 8m09s against a 180s lease), and this is how it keeps the
        scope: by repeatedly demonstrating that its process is still there. The
        alternative — a lease long enough to cover the worst crawl anyone has
        seen — makes every CRASH cost that same worst case, which is the failure
        this loop exists to shrink.
        """
        while True:
            await asyncio.sleep(_AUTHORITY_LEASE_RENEW_SECONDS)
            await self._renew_once()

    async def _renew_once(self) -> None:
        for held in (self._site_claims, self._drive_claims):
            for key, claim in list(held.items()):
                try:
                    held[key] = await self._admission.renew_scope(
                        claim, lease_seconds=_AUTHORITY_LEASE_SECONDS
                    )
                except SourceAuthorityClaimConflictError:
                    # This claim no longer holds the scope: its lease already
                    # lapsed and a successor took it. Renewing is not possible
                    # and pretending otherwise would be a lie, so stop renewing
                    # THIS one and leave the crawl to fail at its next admission
                    # — ``finalize`` independently refuses a superseded claim, so
                    # nothing can be admitted under it in the meantime. The other
                    # scopes are unaffected and keep renewing.
                    _LOG.warning(
                        "m365 reconciliation lease lost while crawling (scope=%s epoch=%s)",
                        claim.scope.scope_hash,
                        claim.reconciliation_epoch,
                    )
                    held.pop(key, None)

    async def _stop_renewals(self) -> None:
        renewals, self._renewals = self._renewals, None
        if renewals is None:
            return
        renewals.cancel()
        try:
            await renewals
        except asyncio.CancelledError:
            pass
        except Exception:
            # The heartbeat died on its own (a database blip, say). It is a
            # BACKGROUND task, so re-raising here would replace whatever outcome
            # the crawl itself reached — including the real exception this
            # ``release`` is unwinding. The consequence of a dead heartbeat is
            # already fail-safe: the lease is not renewed, so it lapses within
            # one window and the scope frees itself.
            _LOG.warning("m365 reconciliation lease renewal stopped early", exc_info=True)

    async def before_site_discovery(self, site_id: str) -> None:
        """CAS-claim the site parent BEFORE its ``/drives`` discovery request."""
        self._site_claims[site_id] = await self._claim(self._site_scope(site_id))

    async def before_drive_io(self, discovered: tuple[tuple[str, str], ...]) -> None:
        """Reject an overlapping drive, then claim every child before any I/O."""
        reject_overlapping_drive_scopes(
            configured_drive_ids=list(self._settings.drive_ids),
            discovered_drive_ids=[drive for drive, site in discovered if site],
        )
        for drive_id, site_id in discovered:
            claim = await self._claim(self._drive_scope(drive_id))
            self._drive_claims[drive_id] = claim
            self._admission.bind_drive_claim(drive_id, claim, site_id=site_id)
        for site_id in self._settings.site_ids:
            children = [
                (drive_id, self._drive_scope(drive_id))
                for drive_id, site in discovered
                if site == site_id
            ]
            self._hierarchy.record_descendants(self._site_scope(site_id), children)

    async def tombstone_deletions(
        self, observations: Sequence[M365DeletionObservation]
    ) -> tuple[str, ...]:
        """Durably tombstone every observed deletion; return their receipt ids."""
        receipts: list[str] = []
        for observation in observations:
            receipt = await self._admission.admit_deletion(observation)
            receipts.append(receipt.receipt_id)
        return tuple(receipts)

    async def sweep_quiet_points(
        self,
        connector: M365GraphConnector,
        *,
        plan: SweepCadencePlan,
        sla_seconds: Decimal,
    ) -> int:
        """Re-read one bounded batch of admitted points per claimed drive.

        The batch is taken from the persisted point cursor, each point's
        permission collection is fetched page by page with the exact next page
        persisted between pages, and only a COMPLETED read admits. Nothing here
        can refresh a point it did not actually re-read: the admission requires
        the evidence the read mints, and the ledger records the DB instant of
        that read rather than the fact that a sweep ran.
        """
        swept = 0
        async with connector.graph_session() as client:
            for drive_id, claim in list(self._drive_claims.items()):
                candidates = self._hierarchy.unswept_points(
                    claim.scope, epoch=claim.reconciliation_epoch
                )
                if not candidates:
                    continue
                batch = self._cursors.plan(
                    claim.scope, candidates=candidates, batch_size=plan.batch_size
                )
                for identity_hash, item_id in batch.points:
                    resumed = identity_hash == batch.resume_point
                    read = await self._read_point(
                        connector,
                        client,
                        claim.scope,
                        drive_id=drive_id,
                        item_id=item_id,
                        identity_hash=identity_hash,
                        page_url=batch.resume_page_url if resumed else None,
                        principals=batch.resume_principals if resumed else (),
                    )
                    if read is None:
                        continue
                    record, resolved = read
                    await self._admit_swept_point(
                        record,
                        resolved,
                        claim=claim,
                        identity_hash=identity_hash,
                        item_id=item_id,
                        epoch=batch.sweep_epoch,
                    )
                    self._cursors.complete_point(claim.scope, identity_hash=identity_hash)
                    swept += 1
                self._hierarchy.require_points_swept_within_sla(
                    claim.scope, sla_seconds=sla_seconds
                )
        return swept

    async def _read_point(
        self,
        connector: M365GraphConnector,
        client: Client,
        scope: SourceReconciliationScope,
        *,
        drive_id: str,
        item_id: str,
        identity_hash: str,
        page_url: str | None,
        principals: tuple[str, ...],
    ) -> tuple[Record, ResolvedPermissions] | None:
        """Fetch one point's complete permissions, then its exact source bytes."""
        collected = list(principals)
        pages = 0
        next_url = page_url
        tainted = False
        while True:
            page = await connector.read_permission_page(
                client, drive_id, item_id, page_url=next_url
            )
            pages += 1
            outcome = map_permissions(list(page.items))
            tainted = tainted or outcome.tainted
            collected.extend(outcome.principals)
            next_url = page.next_link
            # Persisted BEFORE the next request: a crash resumes this exact page
            # with these exact principals instead of restarting the collection.
            self._cursors.record_page(
                scope,
                identity_hash=identity_hash,
                next_page_url=next_url,
                principals=collected,
            )
            if next_url is None:
                break
        deduped = tuple(dict.fromkeys(collected))
        if tainted or not deduped:
            # Fail closed: the point simply is not re-read, so it stays stale and
            # the SLA gate below leaves the scope Unknown.
            return None
        resolved = ResolvedPermissions(
            principals=deduped,
            tainted=False,
            evidence=observed_permissions(
                drive_id=drive_id,
                item_id=item_id,
                principals=deduped,
                pages_read=pages,
                collection_complete=True,
            ),
        )
        record = await connector.fetch_point_record(
            client, drive_id, item_id, acl=resolved.principals
        )
        if record is None:
            return None
        return record, resolved

    async def _admit_swept_point(
        self,
        record: Record,
        resolved: ResolvedPermissions,
        *,
        claim: SourceReconciliationClaim,
        identity_hash: str,
        item_id: str,
        epoch: int,
    ) -> None:
        """Admit one re-read point through the SAME seam the delta path uses."""
        del identity_hash, epoch
        canonical_bytes = canonical_payload_json(record.payload).encode()
        prepared = await self._admission.prepare(
            record,
            acl=resolved.principals,
            canonical_bytes=canonical_bytes,
            item_id=canonical_item_id(
                self._settings.source_name, record.source_id, record.record_id
            ),
            evidence=resolved.evidence,
        )
        await self._admission.finalize(prepared)
        del item_id

    def require_reconciled_sites(self, discovered: tuple[tuple[str, str], ...]) -> None:
        """Refuse site success while a previously admitted drive is unaccounted for."""
        for site_id in self._settings.site_ids:
            self._hierarchy.require_reconciled_union(
                self._site_scope(site_id),
                discovered_keys=[drive for drive, site in discovered if site == site_id],
            )

    async def release(self) -> None:
        """Release every held claim so the scopes are immediately re-claimable.

        The heartbeat stops FIRST: a renewal racing the release could otherwise
        push a lease this run has finished with back out to a full window, which
        is the 900-second dead-man in miniature.
        """
        await self._stop_renewals()
        for drive_id, claim in self._drive_claims.items():
            self._admission.release_drive_claim(drive_id)
            await self._admission.release_scope(claim)
        for claim in self._site_claims.values():
            await self._admission.release_scope(claim)


async def run_m365_sync(
    settings: M365ConnectorSettings,
    *,
    writer: KnowledgeWrite,
    state_store: ConnectorStateStore,
    audit_sink: AuditSink | None,
    actor: str,
    transport: Transport | None = None,
    kpi_tag: str | None = None,
    quarantine: WriteQuarantine | None = None,
    parser: DocumentParser | None = None,
    authority: M365AuthorityAdmission | None = None,
    chunk_reader: ChunkReader | None = None,
) -> M365SyncOutcome:
    """Run one durable, fail-closed M365 sync; return its :class:`M365SyncOutcome`."""
    contract = m365_data_contract(settings)
    state_store.claim_target_binding(
        contract.source,
        _m365_target_binding_digest(settings),
    )
    mapper_revision = m365_source_link_mapper_revision(settings)
    refresh_required = state_store.begin_mapper_refresh(contract.source, mapper_revision)
    refresh_committed = False
    mode = "backfill"
    sweep_plan = None if authority is None else m365_sweep_plan(settings)
    driver = (
        None
        if authority is None
        else _M365AuthorityDriver(
            authority,
            engine=authority.engine,
            settings=settings,
            deployment_id=authority.deployment_id,
            connector_instance_id=authority.connector_instance_id,
        )
    )

    try:
        delta_state = {
            key.removeprefix(_DELTA_PREFIX): value
            for key, value in (
                {} if refresh_required else state_store.load_state(contract.source)
            ).items()
            if key.startswith(_DELTA_PREFIX)
        }
        connector = M365GraphConnector(
            settings,
            transport=transport,
            delta_state=delta_state,
            parser=parser,
            before_site_discovery=None if driver is None else driver.before_site_discovery,
            before_drive_io=None if driver is None else driver.before_drive_io,
        )
        mode = "incremental" if delta_state else "backfill"

        async def _before_success(report: SyncReport) -> None:
            nonlocal refresh_committed
            del report
            if driver is not None:
                # Identity-bearing tombstones, the bounded quiet-point sweep and
                # the descendant-union check all run BEFORE any deltaLink is
                # written. A cursor that advanced first would claim work had been
                # done that no receipt records, and no later run would revisit it.
                await driver.tombstone_deletions(connector.deleted_observations)
                if sweep_plan is not None:
                    await driver.sweep_quiet_points(
                        connector,
                        plan=sweep_plan,
                        sla_seconds=m365_contract_sla_seconds(settings),
                    )
                driver.require_reconciled_sites(connector.discovered_drives)
            if connector.acl_quarantined > 0 and audit_sink is not None:
                await audit_sink.append(
                    new_audit_event(
                        action="connector.acl_quarantine",
                        resource=f"connector://{contract.source}",
                        outcome=AuditOutcome.denied,
                        actor=actor,
                        detail=(
                            f"quarantined={connector.acl_quarantined} "
                            f"fetch_failed={connector.acl_fetch_failed} "
                            f"ids={connector.acl_quarantined_ids}"
                        ),
                        kpi_tag=kpi_tag,
                    )
                )
            if not refresh_required:
                return
            await clear_stale_source_links(
                writer,
                state_store,
                source=contract.source,
                mapper_revision=mapper_revision,
            )
            for drive_id, link in connector.pending_delta_links.items():
                state_store.set_cursor(contract.source, f"{_DELTA_PREFIX}{drive_id}", link)
            state_store.commit_mapper_refresh(contract.source, mapper_revision)
            refresh_committed = True

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
            mapper=make_m365_record_mapper(
                settings,
                take_parsed_evidence=connector.take_parsed_evidence,
            ),
            mapper_revision=mapper_revision,
            before_success=_before_success,
            authority=authority,
            permission_evidence=connector.permission_evidence,
            chunk_reader=chunk_reader,
        )
        report = await runner.run(mode=mode)  # type: ignore[arg-type]
        if not refresh_required:
            for drive_id, link in connector.pending_delta_links.items():
                state_store.set_cursor(contract.source, f"{_DELTA_PREFIX}{drive_id}", link)
    except SourceReconciliationScopeBusyError:
        # An expected overlap, not a failure: this scope's previous crawl is still
        # sweeping, or was killed and its lease has not lapsed yet. Nothing was
        # claimed, so nothing was admitted, so the run has no work to undo beyond
        # the mapper refresh it opened. Raising here would report a broken
        # connector every time a crawl outlives its own cron interval.
        #
        # ONLY this subclass is skipped. A bare SourceAuthorityClaimConflictError
        # means the caller's view of authority is wrong — the expected head moved,
        # or another writer inserted the scope underneath it — and still raises.
        if refresh_required:
            if refresh_committed:
                state_store.invalidate_mapper_revision(contract.source, mapper_revision)
            else:
                state_store.rollback_mapper_refresh(contract.source, mapper_revision)
        _LOG.info(
            "m365 sync skipped: another reconciler holds the scope (source=%s mode=%s)",
            contract.source,
            mode,
        )
        # Zeros that mean NOT ATTEMPTED. No cursor moved and no connector.sync
        # event was emitted, so /v1/connect/status keeps ageing freshness from the
        # last crawl that actually read the source — a skipped firing cannot pass
        # itself off as a successful empty one. Returning here still runs the
        # ``finally`` below, which releases any scope this run did manage to claim.
        return M365SyncOutcome(
            report=SyncReport(
                mode=mode,
                records_seen=0,
                written=0,
                skipped_unchanged=0,
                invalid=0,
                quarantined=0,
                cursors=state_store.load_state(contract.source),
            ),
            acl_quarantined=0,
            mode=mode,
            scope_busy=True,
        )
    except BaseException:
        if refresh_required:
            if refresh_committed:
                state_store.invalidate_mapper_revision(contract.source, mapper_revision)
            else:
                state_store.rollback_mapper_refresh(contract.source, mapper_revision)
        raise
    finally:
        if driver is not None:
            await driver.release()

    # Re-read cursors AFTER the atomic finalizer so the report is not stale (FIX-D).
    report = report.model_copy(update={"cursors": state_store.load_state(contract.source)})

    return M365SyncOutcome(report=report, acl_quarantined=connector.acl_quarantined, mode=mode)
