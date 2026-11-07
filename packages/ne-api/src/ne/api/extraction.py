# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ExtractionRuntime`` — the ne-api composition of the NE-T2X extraction
pathway across all three modes (design §2.2/§9 NE-T2X-4).

Mode 1 (``ingest_review``) runs :class:`~ne.brain_graph.extraction.job.ExtractionJob`
plain — proposals only, exactly as NE-T2X-2 shipped it. Modes 2/3
(``read_flagged``/``autonomous``) additionally attest
``immediate_write_composed=True`` on the SAME job (an attestation, not a new
capability on the job — see ``job.py``'s module/error docstrings) and then, for
every batch the run produced, call
:func:`~ne.brain_graph.extraction.apply.write_provisional_extraction` — the
SAME validate-then-write core ``apply_extraction`` uses, stamping
``assurance="extracted"`` and leaving the proposal ``"proposed"`` (design
§2.3's confirmation ledger). ``write_provisional_extraction``'s PROPOSED-only
skip semantics make re-running this runtime over the same content-addressed
proposal ids self-healing: an already-approved/rejected batch from a prior
run is simply left alone.

**Per-batch failure isolation (Argus P2).** ``write_provisional_extraction``
can refuse a SPECIFIC batch (``ExtractionGateError``/``GraphAclError`` — a
stale/tampered payload or an ``EDGE_DEFS``/``ENTITY_CONTRACTS`` schema
regeneration between proposal creation and write, both reachable
non-adversarially per that function's own docstrings). Because proposal ids
are content-addressed and therefore IDENTICAL across re-runs (E-P1-1), an
unisolated raise would fail the SAME batch at the SAME position on every
future run, permanently blocking every batch built after it. :meth:`run`
therefore catches those two error types PER PROPOSAL, records
``{"error": <exception class name>, "detail": <message>}`` into that
proposal's ``writes`` entry, and continues to the next batch — a genuine
write bug elsewhere is not swallowed by this (only these two documented
fail-closed refusal types are caught).

``emit_mode_event`` appends the ``graph.extraction_mode`` audit event once, at
composition-root startup (design §5.1/B-P1-3) — the YOLO-enablement record.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ne.contracts.governance import AuditSink
from ne.events import new_audit_event
from ne.schema import AuditOutcome

if TYPE_CHECKING:
    import sqlalchemy as sa

    from ne.brain_graph.extraction.apply import ChunkAclResolver, ExtractionApplyStore
    from ne.brain_graph.extraction.job import ExtractionJob, ExtractionRunReport
    from ne.contracts.knowledge import ChunkStore
    from ne.contracts.source_authority import CurrentSourceAuthorityReader
    from ne.flywheel.review import ProposalReviewService

__all__ = [
    "ChunkStoreAclResolver",
    "ExtractionRunResult",
    "ExtractionRuntime",
    "ImmediateWriteContext",
]

#: The actor stamped on system-emitted extraction audit events this module
#: produces directly (the mode event; per-batch writes carry their own
#: caller-supplied actor). Mirrors the repo's ``"system:<component>"``
#: convention (e.g. ``ne.skills.registry``'s ``"system:register"``).
_SYSTEM_ACTOR = "system:extraction"


@dataclass(frozen=True)
class ExtractionRunResult:
    """One :meth:`ExtractionRuntime.run` outcome: the job report plus, for
    modes 2/3, the per-proposal immediate-write counts (empty for mode 1 —
    nothing was written at run time)."""

    report: ExtractionRunReport
    writes: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ImmediateWriteContext:
    """Modes 2/3 only: the write-capable composition
    :meth:`ExtractionRuntime.run` needs to provisionally write each batch
    ``job.run`` just produced."""

    engine: sa.Engine
    review_service: ProposalReviewService
    store: ExtractionApplyStore
    acl_resolver: ChunkAclResolver
    schema_version: str
    clock: Callable[[], datetime]
    source_authority: CurrentSourceAuthorityReader
    authority_access_context_hash: str


@dataclass(frozen=True)
class ExtractionRuntime:
    """The composed extraction pathway for ONE ``build_platform`` process.

    ``mode``/``config_hash`` mirror the frozen platform posture; ``sink`` is
    the SAME audit sink every other governed component in the process shares
    (C10). ``write_ctx`` is ``None`` for mode 1 (``ingest_review``) — there is
    nothing to immediately write — and set for modes 2/3.
    """

    job: ExtractionJob
    mode: str
    sink: AuditSink
    config_hash: str
    write_ctx: ImmediateWriteContext | None = None

    async def run(self, *, source: str | None = None) -> ExtractionRunResult:
        """Run one extraction pass. Mode 1: proposals only (unchanged NE-T2X-2
        behavior). Modes 2/3: proposals AND an immediate provisional write per
        batch produced this run (design §2.2), each write independently
        idempotent (PROPOSED-only skip semantics).

        A single batch's write failing (``ExtractionGateError``/
        ``GraphAclError`` — see the module docstring) does NOT abort the run:
        the failure is recorded into that proposal's ``writes`` entry and
        every OTHER batch this run built is still attempted."""
        if self.write_ctx is None:
            report = await self.job.run(source=source)
            return ExtractionRunResult(report=report)

        report = await self.job.run(source=source, immediate_write_composed=True)
        from ne.brain_graph.errors import GraphAclError
        from ne.brain_graph.extraction.apply import (
            ExtractionGateError,
            write_provisional_extraction,
        )

        writes: dict[str, dict[str, Any]] = {}
        for proposal_id in report.batch_proposal_ids:
            try:
                writes[proposal_id] = await write_provisional_extraction(
                    self.write_ctx.engine,
                    self.write_ctx.review_service,
                    self.write_ctx.store,
                    self.write_ctx.acl_resolver,
                    proposal_id,
                    actor=_SYSTEM_ACTOR,
                    audit_sink=self.sink,
                    config_hash=self.config_hash,
                    schema_version=self.write_ctx.schema_version,
                    clock=self.write_ctx.clock,
                    source_authority=self.write_ctx.source_authority,
                    authority_access_context_hash=(self.write_ctx.authority_access_context_hash),
                )
            except (ExtractionGateError, GraphAclError) as exc:
                writes[proposal_id] = {"error": type(exc).__name__, "detail": str(exc)}
        return ExtractionRunResult(report=report, writes=writes)

    async def emit_mode_event(self) -> None:
        """Append the ``graph.extraction_mode`` audit event (design §5.1,
        B-P1-3) — mode + config_hash, counts-only detail (C12). Called ONCE,
        at composition-root startup, by the ne-api lifespan when extraction is
        composed; never otherwise."""
        await self.sink.append(
            new_audit_event(
                action="graph.extraction_mode",
                resource="graph://extraction/mode",
                outcome=AuditOutcome.success,
                actor=_SYSTEM_ACTOR,
                detail=json.dumps({"mode": self.mode}),
                config_hash=self.config_hash,
                provenance={"source": "ne-api", "component": "extraction.runtime"},
            )
        )


class ChunkStoreAclResolver:
    """A :class:`~ne.brain_graph.extraction.apply.ChunkAclResolver` over the
    platform's composed ``ChunkStore`` (contracts ``knowledge.py``): resolves
    an evidencing chunk's ACL via ``ChunkStore.get`` — the same unscoped
    fetch-by-id audit-reconstruction surface the C10 read layer uses. An
    unknown ``chunk_ref`` (or a store with no matching chunk) returns ``()``,
    which the shared validate-then-write core treats as fail-closed (refuses
    the write; see ``apply.py``'s ``_resolve_batch_evidence_acls``).
    """

    def __init__(self, chunk_store: ChunkStore) -> None:
        self._chunk_store = chunk_store

    async def acl_for(self, chunk_ref: str) -> tuple[str, ...]:
        chunks: Sequence[Any] = await self._chunk_store.get([chunk_ref])
        if not chunks:
            return ()
        return tuple(chunks[0].acl)
