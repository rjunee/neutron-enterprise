# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ExtractionJob`` — mode 1 (``ingest_review``): gateway-routed candidates →
review-gated ``graph_extraction_candidate`` proposals (design §3.1, sibling of
``er/job.py``).

**Mode-1 structural invariant (the T-ER-1 mirror, verbatim).** The job's SOLE
write surface is the NE-065 proposal store: its constructor accepts a
SCAN/LOOKUP-only :class:`ExtractionScanStore` (``scan_nodes`` — no upsert or
delete reachable from the job object) and NEVER holds a
:class:`~ne.brain_graph.extraction.writer.ExtractionWriter` or any other
write-capable store. There is no writer parameter anywhere on this class —
modes 2/3 (which DO write immediately) are NE-T2X-4's composition, not this
unit's. Tested structurally exactly like ``test_er_review_gate.py``.

**Master gate + mode fail-closed (Codex C1/C2, round-4; attestation semantics
added NE-T2X-4).** :meth:`ExtractionJob.run` refuses BEFORE any work — before
``ensure_proposal_schema``, before any gateway call — in three cases:
``extraction_enabled=False`` raises :class:`ExtractionDisabledError` (this job
is a public API, not reachable only through the CLI's own gate; design §2.5's
"byte-identical when disabled" promise must hold for every caller);
``extraction_mode != "ingest_review"`` AND ``immediate_write_composed=False``
(the default) raises :class:`ExtractionModeNotImplementedError` (modes 2/3
promise an ADDITIONAL immediate graph write this job structurally cannot
perform on its own — see the ``immediate_write_composed`` parameter below);
``extraction_mode == "ingest_review"`` AND ``immediate_write_composed=True``
raises ``ValueError`` (mode 1 has no immediate write — a caller passing the
attestation for mode 1 is a composition bug, not a runtime condition to
silently ignore).

**``immediate_write_composed`` — an attestation, not a capability
(NE-T2X-4).** This job's write-capable-store-free structure (the T-ER-1
mirror above) is UNCHANGED: there is still no writer parameter on this class
and no write-capable store reachable from it. ``immediate_write_composed`` is
a keyword-only ``bool`` a HOST composition (ne-api's ``ExtractionRuntime``,
NE-T2X-4) passes to ATTEST that it — not this job — owns an immediate-write
path for the batches this run produces (via
:func:`~ne.brain_graph.extraction.apply.write_provisional_extraction`,
applied by the host per ``ExtractionRunReport.batch_proposal_ids`` AFTER
``run()`` returns). Passing it does not grant this job any new capability; it
only relaxes the mode-gate refusal so a correctly-composed host can run modes
2/3 without this job raising a mode error about a promise the HOST, not this
job, fulfills.

**Gateway routing (design B-P1-4/C3, the dead-assist lesson).** Every
completion request carries ``task="graph-extract"``
(:data:`~ne.brain_graph.extraction.EXTRACTION_TASK`). ``gateway.complete`` is
NEVER wrapped in ``try/except`` here — an unrouted task or any gateway failure
PROPAGATES out of :meth:`ExtractionJob.run`. A feature that silently produces
zero proposals behind green CI because its task was never routed is the exact
defect class NE-HGATE was built to catch; the real ``build_platform`` boot
fail-loud and the production ``UnknownTaskError`` live in NE-T2X-4 (design
§9) — this unit only pins the JOB-SIDE half of that discipline (no swallow).

**Determinism, stated honestly (design E-P1-1).** Proposal ids are
content-addressed over the TYPED assertion set via U1's
``graph_extraction_proposal_id``/``graph_extraction_row_id`` helpers (never
re-derived locally), so re-running this job over the SAME chunks is
store-idempotent even though the underlying model completion is NOT claimed to
be byte-deterministic — unlike ``test_construction_determinism.py``'s
connector-sync lane (T-CON-3), where the SAME input byte-for-byte always
produces the SAME output because there is no model in that loop.
:mod:`.test_extraction_determinism` pins this contrast directly.

**``links_auto`` is counted, never queued, never written (design DECISION
point, stated explicitly per the build brief).** Design §3.1/§3.2 sketch
auto-links (a ``kind="link"`` entry whose confidence clears
``extraction_auto_link_threshold``) as a mode-independent WRITE. That
collides head-on with THIS unit's binding mode-1 structural invariant: the
job holds no write-capable store, so a link can never be applied here even
if it wanted to be. This unit therefore DECIDES the ``ingest_review``
semantics: an auto-link is counted into ``ExtractionRunReport.links_auto``
and DROPPED — never appended to a proposal (a below-auto-band link IS
queued, as a review-gated ``LINK`` assertion), never written to the graph.
Under DECISION-3 (§3.6) a link has no graph representation anyway (no
``MENTIONS`` edges), so in mode 1 its whole function is candidate-first
grounding + shrinking the review queue per §2.4's honest-recommendation
note — the counter exists so an operator can see how much of that shrink is
happening.

**Modes 2/3 decision (NE-T2X-4): ``links_auto`` stays counted-and-dropped in
EVERY mode, not just mode 1.** DECISION-3 (§3.6) means a link has no graph
representation at all (no ``MENTIONS`` edges) regardless of how a caller
vouches for NEW elements — modes 2/3 move the vouching POINT for
``new_entity``/``new_edge`` assertions (immediate write vs. gated write),
they do not create a graph representation for links where none exists. So
this counter's semantics are mode-independent: it is always an
operator-visible measure of candidate-first grounding shrinking the
new-element flux, never a write, in ``ingest_review``, ``read_flagged``, or
``autonomous`` alike.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.brain_graph.config import _EDGE_INDEX, BrainGraphSettings
from ne.brain_graph.extraction import EXTRACTION_TASK
from ne.brain_graph.extraction.identity import canonical_form, llm_entity_key
from ne.brain_graph.extraction.prompt import render_extraction_system_prompt
from ne.brain_graph.extraction.writer import validate_extracted_properties
from ne.contracts.governance import AuditSink
from ne.contracts.models import CompletionRequest, ModelGateway
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReader,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
)
from ne.contracts.types import ChatMessage
from ne.events import new_audit_event
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import (
    FixProposal,
    GraphExtractionAssertion,
    GraphExtractionAssertionKind,
    GraphExtractionBatch,
    ProposalKind,
    SqlAlchemyProposalStore,
    ensure_proposal_schema,
    graph_extraction_proposal_id,
    graph_extraction_row_id,
    ne_fix_proposals,
)
from ne.schema import ENTITY_CLASSES, AuditOutcome

__all__ = [
    "ChunkSource",
    "ExtractionChunk",
    "ExtractionDisabledError",
    "ExtractionJob",
    "ExtractionModeNotImplementedError",
    "ExtractionRunReport",
    "ExtractionScanStore",
]

#: The extraction system prompt, rendered once at import time from the wall's
#: own schema authorities (NE-T2X-PROMPT-SCHEMA — see
#: ``ne.brain_graph.extraction.prompt``'s module docstring). A pure function
#: of import-time constants, so this is deterministic across every process
#: and every run (E-P1-1) — never re-rendered per request.
_EXTRACTION_SYSTEM_PROMPT = render_extraction_system_prompt()


class ExtractionDisabledError(Exception):
    """Raised by :meth:`ExtractionJob.run` when ``extraction_enabled=False``
    (Codex C2, round-4).

    ``ExtractionJob`` is a public API, reachable by any host composition, not
    only through the CLI's own ``extraction_enabled`` gate (``cli.py``'s
    ``_extraction_run``/``_extraction_purge``). Design §2.5 promises that the
    disabled master gate is byte-identical to today's extraction-free
    posture EVERYWHERE: no extraction module composed, no gateway task
    required, no ``graph_extraction_candidate`` proposal ever emitted, no new
    audit event. Before this check, a caller that constructed
    ``ExtractionJob`` directly (bypassing the CLI) with
    ``extraction_enabled=False`` but a non-empty ``extraction_sources`` still
    got a full run — gateway calls, proposals, and a ``graph.extraction``
    audit event — silently violating that promise. The check runs FIRST in
    :meth:`ExtractionJob.run`, before ``ensure_proposal_schema`` or any
    gateway call."""


class ExtractionModeNotImplementedError(Exception):
    """Raised by :meth:`ExtractionJob.run` when ``extraction_mode`` is not
    ``"ingest_review"`` AND the caller did not pass
    ``immediate_write_composed=True`` (Codex C1, round-4; attestation
    semantics NE-T2X-4).

    This job's structural invariant (T-ER-1 mirror) means it holds no
    write-capable store and can only ever produce review-gated proposals on
    its own. ``read_flagged``/``autonomous`` (design §2.2) promise an
    ADDITIONAL immediate graph write — a capability this job structurally
    does not have. A HOST composition that DOES own that capability (ne-api's
    ``ExtractionRuntime``, NE-T2X-4) attests to it via
    ``immediate_write_composed=True``, which relaxes this refusal; a direct
    caller of this job (no such host) gets no such attestation and this error
    still fires, exactly as before NE-T2X-4. Before the original check,
    selecting either mode silently ran the ingest_review proposal-only
    workflow and reported that configured mode in
    ``ExtractionRunReport.mode``, giving a deployment materially different
    behavior than the mode promised with no signal anything was wrong. The
    check runs FIRST in :meth:`ExtractionJob.run`, before any gateway call or
    write."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ExtractionChunk(_Frozen):
    """One T1 chunk of an eligible source, as this job needs it."""

    chunk_ref: str
    document_ref: str
    source_system: str
    text: str
    acl: tuple[str, ...] = ()
    source_authority_stamps: tuple[SourceAuthorityStamp, ...] = ()


class ExtractionRunReport(_Frozen):
    """Counts-and-ids summary of one :meth:`ExtractionJob.run` (C12 shape — this
    is also exactly what the ``graph.extraction`` audit event's detail carries).

    **Counter honesty (Argus P3-1).** ``batches_built``/``batch_proposal_ids``
    count the proposal batches THIS RUN BUILT (content-addressed over the
    typed assertion set, so identical across re-runs over unchanged chunks —
    see :mod:`.test_extraction_determinism`). ``proposals_appended``/
    ``appended_proposal_ids`` count only the rows that ACTUALLY LANDED AS NEW
    in the store this run: ``SqlAlchemyProposalStore.append`` is idempotent
    on ``proposal_id`` (a duplicate INSERT rolls back to the existing row), so
    a no-op re-run over unchanged chunks BUILDS the same batches but APPENDS
    none — ``proposals_appended`` reports ``0`` and ``appended_proposal_ids``
    is empty in that case. Before this split, a single dishonest ``proposals``
    counter was reported (and audited) as if every built batch had landed,
    which overstates what happened on any re-run.
    """

    run_id: str
    mode: str
    sources: tuple[str, ...]
    chunks_scanned: int
    chunks_skipped_no_acl: int
    mentions: int
    links_auto: int
    batches_built: int
    proposals_appended: int
    discarded_below_threshold: int
    quarantined_invalid: int
    batch_proposal_ids: tuple[str, ...]
    appended_proposal_ids: tuple[str, ...]


class ExtractionScanStore(Protocol):
    """The SCAN-ONLY store surface the job needs (T-ER-1 mirror: no upsert/delete)."""

    async def scan_nodes(self, entity_class: str | None = None) -> Sequence[Mapping[str, Any]]: ...


class ChunkSource(Protocol):
    """Host-composed source of eligible chunks for one ``source_system``."""

    async def chunks(self, source_system: str) -> Sequence[ExtractionChunk]: ...


def _resolve_endpoint(
    raw_key: str,
    class_hint: Any,
    candidates: Mapping[str, tuple[str, str]],
    local_entities: Mapping[tuple[str, str], str],
) -> tuple[str, str] | None:
    """Resolve a ``new_edge`` endpoint to ``(key, entity_class)``.

    Either an EXISTING candidate key (the class comes from the candidate
    vocabulary, ``class_hint`` is ignored), or a same-chunk-response
    ``new_entity`` display name (requires ``class_hint`` to look it up in
    ``local_entities``, keyed by ``(entity_class, canonical_form(display))``).
    ``None`` if neither resolves — the caller counts that as invalid (F1).
    """
    if raw_key in candidates:
        return raw_key, candidates[raw_key][0]
    if isinstance(class_hint, str):
        key = local_entities.get((class_hint, canonical_form(raw_key)))
        if key is not None:
            return key, class_hint
    return None


def _mint_assertion(
    kind: GraphExtractionAssertionKind,
    *,
    entity_class: str = "",
    entity_key: str = "",
    display: str = "",
    properties: dict[str, Any] | None = None,
    subject_key: str = "",
    edge_kind: str = "",
    object_key: str = "",
    confidence: float,
    evidence_ref: str,
) -> GraphExtractionAssertion:
    """Construct a :class:`GraphExtractionAssertion` with a content-addressed
    ``row_id`` (U1's :func:`graph_extraction_row_id`, never re-derived locally).

    ``row_id`` is excluded from its OWN content address by construction (the
    helper hashes kind/class/keys/edge-triple/evidence_ref), so a placeholder
    value here is safe: mint the draft, compute the real id, then swap it in
    via ``model_copy`` (the model is frozen).
    """
    draft = GraphExtractionAssertion(
        row_id="pending",
        kind=kind,
        entity_class=entity_class,
        entity_key=entity_key,
        display=display,
        properties=properties or {},
        subject_key=subject_key,
        edge_kind=edge_kind,
        object_key=object_key,
        confidence=confidence,
        evidence_ref=evidence_ref,
    )
    return draft.model_copy(update={"row_id": graph_extraction_row_id(draft)})


class ExtractionJob:
    """Export candidates → gateway-routed completion → validation wall → proposals."""

    def __init__(
        self,
        scan_store: ExtractionScanStore,
        proposal_store_engine: sa.Engine,
        settings: BrainGraphSettings,
        gateway: ModelGateway,
        chunk_source: ChunkSource,
        *,
        audit_sink: AuditSink,
        config_hash: str,
        clock: Callable[[], datetime],
        source_authority: CurrentSourceAuthorityReader | None = None,
        authority_principal_id: str = "",
        authority_access_context_hash: str = "",
    ) -> None:
        # NOTE: ``scan_store`` is typed SCAN-ONLY — this job structurally cannot
        # reach a graph write (T-ER-1 mirror). The engine (proposal store) is the
        # ONLY write surface reachable from this object.
        self._store = scan_store
        self._engine = proposal_store_engine
        self._settings = settings
        self._gateway = gateway
        self._chunk_source = chunk_source
        self._audit_sink = audit_sink
        self._config_hash = config_hash
        self._clock = clock
        self._source_authority = source_authority
        self._authority_principal_id = authority_principal_id
        self._authority_access_context_hash = authority_access_context_hash

    async def _authority_is_current(self, chunk: ExtractionChunk) -> bool:
        return await self._stamps_are_current(chunk.source_authority_stamps)

    async def _stamps_are_current(self, stamps: tuple[SourceAuthorityStamp, ...]) -> bool:
        if self._source_authority is None:
            return True
        if not stamps:
            return False
        for stamp in stamps:
            try:
                resolution = await self._source_authority.resolve(
                    stamp,
                    principal_id=self._authority_principal_id,
                    access_context_hash=self._authority_access_context_hash,
                )
            except Exception:
                return False
            if resolution.decision is not SourceAuthorityDecision.AUTHORIZED:
                return False
        return True

    async def run(
        self, *, source: str | None = None, immediate_write_composed: bool = False
    ) -> ExtractionRunReport:
        """Run one extraction pass. ``immediate_write_composed`` is a
        keyword-only HOST attestation (NE-T2X-4) — see the class/error
        docstrings; it grants no capability on this object."""
        # Codex C2 (round-4): fail closed BEFORE any work — no schema init, no
        # gateway call, no proposal, no audit event — when the master gate is
        # off. ExtractionJob is a public API; the CLI's own extraction_enabled
        # check (cli.py `_extraction_run`) is a convenience early-exit, not a
        # substitute for this one.
        if not self._settings.extraction_enabled:
            raise ExtractionDisabledError(
                "ExtractionJob.run refused: extraction_enabled=False (design §2.5 — no "
                "extraction module composed, no gateway task, no proposal, no audit event "
                "when the master gate is off)"
            )
        # Codex C1 (round-4) + NE-T2X-4 attestation semantics: fail closed for
        # any mode/attestation combination this job cannot honestly run.
        # ingest_review (mode 1, the T-ER-1 mirror — this job holds no
        # write-capable store) NEVER takes the attestation: mode 1 has no
        # immediate write, so a caller passing it is a composition bug, not a
        # runtime condition. read_flagged/autonomous promise an immediate
        # graph write this job cannot perform ON ITS OWN — running under one
        # of those settings WITHOUT a host's attestation would silently give
        # a deployment different behavior than its configured mode promises.
        if self._settings.extraction_mode == "ingest_review":
            if immediate_write_composed:
                raise ValueError(
                    "ExtractionJob.run refused: immediate_write_composed=True with "
                    "extraction_mode='ingest_review' — mode 1 has no immediate write; "
                    "passing the attestation for it is a composition bug"
                )
        elif not immediate_write_composed:
            raise ExtractionModeNotImplementedError(
                "ExtractionJob.run refused: extraction_mode="
                f"{self._settings.extraction_mode!r} requires a composed immediate-write "
                "host (ne-api's ExtractionRuntime, NE-T2X-4) to pass "
                "immediate_write_composed=True — this job alone cannot perform the "
                "immediate graph write 'read_flagged'/'autonomous' promise"
            )

        run_id = uuid.uuid4().hex
        if source is not None:
            sources = tuple(s for s in self._settings.extraction_sources if s == source)
        else:
            sources = self._settings.extraction_sources

        ensure_proposal_schema(self._engine)
        proposal_store = SqlAlchemyProposalStore(self._engine)

        candidates = await self._candidate_vocabulary()
        candidate_lines = self._candidate_lines(candidates)

        chunks_scanned = 0
        chunks_skipped_no_acl = 0
        mentions = 0
        links_auto = 0
        discarded_below_threshold = 0
        quarantined_invalid = 0
        per_document: dict[str, dict[str, GraphExtractionAssertion]] = {}
        doc_source: dict[str, str] = {}
        doc_stamps: dict[str, tuple[SourceAuthorityStamp, ...]] = {}

        for src in sources:
            chunks = await self._chunk_source.chunks(src)
            for chunk in chunks:
                chunks_scanned += 1
                if not chunk.acl:
                    chunks_skipped_no_acl += 1
                    continue
                if not await self._authority_is_current(chunk):
                    quarantined_invalid += 1
                    continue
                request = self._build_request(candidate_lines, chunk)
                # B-P1-4: NEVER caught — an unrouted task or gateway fault propagates.
                completion = await self._gateway.complete(request)
                # A model call is a time boundary: drift while it ran means the
                # generated text is never persisted as a proposal.
                if not await self._authority_is_current(chunk):
                    quarantined_invalid += 1
                    continue
                stats, assertions = self._validate_wall(completion.text, chunk, candidates)
                mentions += stats["mentions"]
                links_auto += stats["links_auto"]
                discarded_below_threshold += stats["discarded_below_threshold"]
                quarantined_invalid += stats["quarantined_invalid"]
                if not assertions:
                    continue
                bucket = per_document.setdefault(chunk.document_ref, {})
                doc_source.setdefault(chunk.document_ref, src)
                existing_stamps = doc_stamps.get(chunk.document_ref, ())
                doc_stamps[chunk.document_ref] = tuple(
                    sorted(
                        set((*existing_stamps, *chunk.source_authority_stamps)),
                        key=lambda stamp: stamp.authority_receipt_hash,
                    )
                )
                for assertion in assertions:
                    existing = bucket.get(assertion.row_id)
                    if existing is None or assertion.confidence > existing.confidence:
                        bucket[assertion.row_id] = assertion

        batch_proposal_ids: list[str] = []
        appended_proposal_ids: list[str] = []
        batches_built = 0
        for document_ref, bucket in per_document.items():
            if not bucket:
                continue
            assertions = tuple(sorted(bucket.values(), key=lambda a: a.row_id))
            stamps = doc_stamps.get(document_ref, ())
            if not await self._stamps_are_current(stamps):
                quarantined_invalid += len(assertions)
                continue
            proposal_id, sha16 = graph_extraction_proposal_id(document_ref, assertions, stamps)
            batch = GraphExtractionBatch(
                document_ref=document_ref,
                run_id=run_id,
                source_system=doc_source.get(document_ref, ""),
                source_authority_stamps=stamps,
                assertions=assertions,
            )
            max_conf = max(a.confidence for a in assertions)
            priority = ProposalPriority.HIGH if max_conf >= 0.99 else ProposalPriority.MEDIUM
            proposal = FixProposal(
                proposal_id=proposal_id,
                kind=ProposalKind.GRAPH_EXTRACTION_CANDIDATE,
                lane=FixLane.ENTITY_RESOLUTION,
                cluster_id=f"extract:{sha16}",
                evidence_event_ids=(),
                evidence_signal_ids=(),
                summary=(
                    f"graph-extraction batch over {document_ref} ({len(assertions)} assertions)"
                ),
                confidence=max_conf,
                priority=priority,
                config_hash=self._config_hash,
                created_at=self._clock(),
                payload=batch.model_dump(mode="json"),
            )
            with self._engine.begin() as conn:
                # Argus P3-1: check-then-append on the SAME connection/
                # transaction as the append itself, so the two are
                # consistent. This job is single-writer (CLI/worker
                # composition owns the engine for the duration of `run`), so
                # the check-then-act window is not a concurrency hazard here;
                # `proposals_appended` is informational, not a correctness
                # gate, so we deliberately do not add cross-process locking
                # for it.
                already_stored = (
                    conn.execute(
                        sa.select(ne_fix_proposals.c.id).where(
                            ne_fix_proposals.c.proposal_id == proposal.proposal_id
                        )
                    ).first()
                    is not None
                )
                proposal_store.append(conn, proposal)
            batch_proposal_ids.append(proposal.proposal_id)
            batches_built += 1
            if not already_stored:
                appended_proposal_ids.append(proposal.proposal_id)

        report = ExtractionRunReport(
            run_id=run_id,
            mode=self._settings.extraction_mode,
            sources=sources,
            chunks_scanned=chunks_scanned,
            chunks_skipped_no_acl=chunks_skipped_no_acl,
            mentions=mentions,
            links_auto=links_auto,
            batches_built=batches_built,
            proposals_appended=len(appended_proposal_ids),
            discarded_below_threshold=discarded_below_threshold,
            quarantined_invalid=quarantined_invalid,
            batch_proposal_ids=tuple(batch_proposal_ids),
            appended_proposal_ids=tuple(appended_proposal_ids),
        )
        await self._emit_run_audit(report)
        return report

    # -- candidate vocabulary (R8 non-principal batch read) -------------------

    async def _candidate_vocabulary(self) -> dict[str, tuple[str, str]]:
        nodes = await self._store.scan_nodes()
        return {
            str(n["key"]): (str(n.get("entity_class", "")), str(n.get("display_name", "")))
            for n in nodes
        }

    def _candidate_lines(self, candidates: Mapping[str, tuple[str, str]]) -> str:
        lines = [f"{cls}|{key}|{display}" for key, (cls, display) in sorted(candidates.items())]
        return "\n".join(lines[:200])

    def _build_request(self, candidate_lines: str, chunk: ExtractionChunk) -> CompletionRequest:
        # NE-T2X-PROMPT-SCHEMA: the system prompt now STATES the assertion
        # schema (kind/confidence/per-kind fields, entity classes, edge
        # triples) DERIVED from the wall's own authorities, rather than
        # claiming a schema the model was never shown — see
        # ``ne.brain_graph.extraction.prompt``'s module docstring.
        system = ChatMessage(role="system", content=_EXTRACTION_SYSTEM_PROMPT)
        user = ChatMessage(
            role="user",
            content=(
                f"Candidates (class|key|display):\n{candidate_lines}\n\nChunk text:\n{chunk.text}"
            ),
        )
        # NE-T2X-PROMPT-SCHEMA PRE-DECIDED FALLBACK (build brief Step 2):
        # ``params={"response_format": {"type": "json_object"}}`` was tried
        # and produced ``TypeError: Unsupported response_format type -
        # FrozenDict({'type': 'json_object'})`` -- but this is NOT a rig/
        # provider rejection. The rig's vLLM ACCEPTS the param over raw HTTP
        # (verified: `curl .../v1/chat/completions` with
        # `"response_format":{"type":"json_object"}` -> 200). The failure is
        # REPO-SIDE and reproduces with zero network: ``CompletionRequest.
        # params`` is an ``AnyMap``, so pydantic freezes the nested dict into
        # a ``FrozenDict``; ``ne.gateway.gateway._call_params`` only thaws the
        # OUTER layer (``dict(request.params)``), so a nested-object param
        # like ``response_format`` reaches litellm's
        # ``type_to_response_format_param`` still wrapped in a ``FrozenDict``,
        # which fails its ``isinstance(response_format, dict)`` check. This is
        # a genuine gateway gap (ANY nested-object request param takes the
        # same path, not just ``response_format``) -- filed in the PR body as
        # a follow-up for the gateway owner, not fixed here (out of
        # mayTouch). (Since CLOSED by NE-GATEWAY-FROZENDICT: ``_call_params``
        # now thaws nested params; the drop below persists deliberately
        # until the follow-up unit wires the param.) Per the pre-decided
        # fallback the param is dropped entirely; the prompt above (not a
        # response-format constraint) is the fix, and it is fully sufficient
        # standalone -- verified against the live rig (see PR body).
        return CompletionRequest(messages=(system, user), task=EXTRACTION_TASK, trace_id=None)

    # -- validation wall (F1) --------------------------------------------------

    def _validate_wall(
        self, text: str, chunk: ExtractionChunk, candidates: Mapping[str, tuple[str, str]]
    ) -> tuple[dict[str, int], list[GraphExtractionAssertion]]:
        stats = {
            "mentions": 0,
            "links_auto": 0,
            "discarded_below_threshold": 0,
            "quarantined_invalid": 0,
        }
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            stats["quarantined_invalid"] += 1
            return stats, []
        if not isinstance(data, dict) or not isinstance(data.get("assertions"), list):
            stats["quarantined_invalid"] += 1
            return stats, []

        pending: list[dict[str, Any]] = []
        edge_entries: list[tuple[dict[str, Any], float]] = []
        local_entities: dict[tuple[str, str], str] = {}

        for entry in data["assertions"]:
            if not isinstance(entry, dict):
                stats["quarantined_invalid"] += 1
                continue
            stats["mentions"] += 1
            kind = entry.get("kind")
            conf_raw = entry.get("confidence")
            if isinstance(conf_raw, bool) or not isinstance(conf_raw, (int, float)):
                stats["quarantined_invalid"] += 1
                continue
            confidence = float(conf_raw)
            if not (0.0 <= confidence <= 1.0):
                stats["quarantined_invalid"] += 1
                continue

            if kind == "new_edge":
                edge_entries.append((entry, confidence))
                continue
            if kind == "link":
                self._handle_link(entry, confidence, candidates, stats, pending)
                continue
            if kind == "new_entity":
                self._handle_new_entity(entry, confidence, stats, pending, local_entities)
                continue
            stats["quarantined_invalid"] += 1

        for entry, confidence in edge_entries:
            self._handle_new_edge(entry, confidence, candidates, local_entities, stats, pending)

        assertions = [_mint_assertion(evidence_ref=chunk.chunk_ref, **item) for item in pending]
        return stats, assertions

    def _handle_link(
        self,
        entry: Mapping[str, Any],
        confidence: float,
        candidates: Mapping[str, tuple[str, str]],
        stats: dict[str, int],
        pending: list[dict[str, Any]],
    ) -> None:
        entity_key = entry.get("entity_key")
        if not isinstance(entity_key, str) or entity_key not in candidates:
            stats["quarantined_invalid"] += 1
            return
        entity_class = candidates[entity_key][0]
        if confidence < self._settings.extraction_min_confidence:
            stats["discarded_below_threshold"] += 1
            return
        if confidence >= self._settings.extraction_auto_link_threshold:
            # Counted, never queued, never written — mode-1 DECISION, see the
            # module docstring's "links_auto is counted, never queued, never
            # written" paragraph. NE-T2X-4 owns what this means once a
            # write-capable store exists (modes 2/3).
            # Pinned (mutation-verified) by tests/test_extraction_auto_link_band.py
            # — deleting the `return` below must turn that module RED.
            stats["links_auto"] += 1
            return
        pending.append(
            {
                "kind": GraphExtractionAssertionKind.LINK,
                "entity_class": entity_class,
                "entity_key": entity_key,
                "confidence": confidence,
            }
        )

    def _handle_new_entity(
        self,
        entry: Mapping[str, Any],
        confidence: float,
        stats: dict[str, int],
        pending: list[dict[str, Any]],
        local_entities: dict[tuple[str, str], str],
    ) -> None:
        entity_class = entry.get("entity_class")
        display = entry.get("display")
        raw_props = entry.get("properties")
        if (
            not isinstance(entity_class, str)
            or entity_class not in ENTITY_CLASSES
            or not isinstance(display, str)
            or not display.strip()
            or not isinstance(raw_props, dict)
        ):
            stats["quarantined_invalid"] += 1
            return
        llm_key = llm_entity_key(entity_class, display)
        try:
            validated = validate_extracted_properties(entity_class, raw_props, llm_key)
        except Exception:
            stats["quarantined_invalid"] += 1
            return
        if confidence < self._settings.extraction_min_confidence:
            stats["discarded_below_threshold"] += 1
            return
        # Recorded for same-chunk edge resolution ONLY once the entity itself
        # survives banding — a below-threshold new_entity is never queued into
        # `pending`, so it must not be a resolvable endpoint either (Argus P1:
        # an edge pointing at a dropped entity would batch-construct fine but
        # then permanently jam `apply_extraction` with zero writes, stranding
        # every OTHER valid element in the same proposal).
        local_entities[(entity_class, canonical_form(display))] = llm_key
        properties = {k: v for k, v in validated.items() if k != "id"}
        pending.append(
            {
                "kind": GraphExtractionAssertionKind.NEW_ENTITY,
                "entity_class": entity_class,
                "entity_key": llm_key,
                "display": display,
                "properties": properties,
                "confidence": confidence,
            }
        )

    def _handle_new_edge(
        self,
        entry: Mapping[str, Any],
        confidence: float,
        candidates: Mapping[str, tuple[str, str]],
        local_entities: Mapping[tuple[str, str], str],
        stats: dict[str, int],
        pending: list[dict[str, Any]],
    ) -> None:
        edge_kind = entry.get("edge_kind")
        subj_raw = entry.get("subject_key")
        obj_raw = entry.get("object_key")
        if (
            not isinstance(edge_kind, str)
            or not isinstance(subj_raw, str)
            or not isinstance(obj_raw, str)
        ):
            stats["quarantined_invalid"] += 1
            return
        subject = _resolve_endpoint(
            subj_raw, entry.get("subject_class"), candidates, local_entities
        )
        obj = _resolve_endpoint(obj_raw, entry.get("object_class"), candidates, local_entities)
        if subject is None or obj is None:
            stats["quarantined_invalid"] += 1
            return
        subject_key, subject_class = subject
        object_key, object_class = obj
        if (subject_class, edge_kind, object_class) not in _EDGE_INDEX:
            stats["quarantined_invalid"] += 1
            return
        if confidence < self._settings.extraction_min_confidence:
            stats["discarded_below_threshold"] += 1
            return
        pending.append(
            {
                "kind": GraphExtractionAssertionKind.NEW_EDGE,
                "subject_key": subject_key,
                "edge_kind": edge_kind,
                "object_key": object_key,
                "confidence": confidence,
            }
        )

    # -- audit (C12: counts and ids only) --------------------------------------

    async def _emit_run_audit(self, report: ExtractionRunReport) -> None:
        await self._audit_sink.append(
            new_audit_event(
                action="graph.extraction",
                resource=f"graph://extraction/{report.run_id}",
                outcome=AuditOutcome.success,
                actor="extraction-job",
                detail=json.dumps(
                    {
                        "run_id": report.run_id,
                        "mode": report.mode,
                        "sources": list(report.sources),
                        "chunks_scanned": report.chunks_scanned,
                        "chunks_skipped_no_acl": report.chunks_skipped_no_acl,
                        "mentions": report.mentions,
                        "links_auto": report.links_auto,
                        "batches_built": report.batches_built,
                        "proposals_appended": report.proposals_appended,
                        "discarded_below_threshold": report.discarded_below_threshold,
                        "quarantined_invalid": report.quarantined_invalid,
                    }
                ),
                config_hash=self._config_hash,
                provenance={"source": "ne-brain-graph", "component": "extraction.job"},
            )
        )
