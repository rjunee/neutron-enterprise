# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The chat agent's tool menu — the ONLY place a tool handler exists (C6).

A tool handler lives ONLY inside a :class:`ToolBinding` closure here; ne-api
registers NO route or helper that invokes a handler, the executor, or the
gateway-as-tool directly. Two v1 tools live here: ``read.knowledge.search`` — the
spec's illustrative ``knowledge.search`` would be DENIED by the real SPEC §4.2
rego taxonomy (``tiers.rego`` classifies by prefix; an unclassified action is
deny-by-default), so the concrete name is prefixed ``read.`` (tier 0; precedent:
ne-runtime tests use ``read.docs.search``) — and ``read.trace`` (NE-167a,
agent-native parity for the assembled governed trace view, NE-167a).

The turn principal reaches the handler through :data:`TURN_PRINCIPAL`, a ContextVar
SET ONLY by the chat route from ``current_principal`` (the verified bearer) BEFORE
the turn task is created, and reset in ``finally``. An unset carrier fails CLOSED
(``RuntimeError`` → an audited ``tool.execute`` error), so no request-supplied data
can ever reach the retriever (or the trace assembler) as the acting principal.

NE-T2X-5-RECORDER-WIRE (design §6.1/§6.3, A-P1-5): ``read.knowledge.search``'s
``_search`` handler is the ONLY handler in this module (or anywhere in ne-api's
governed tool menu) that can retrieve assurance-labeled graph elements — it is
the sole caller of :func:`ne.runtime.provisional.current_provisional_recorder`.
``read.trace`` (below) reads the audit chain, never the graph; the ne.skills
tools read the skill registry, never the graph; verified-answer T3 tools
(``ne.api.verified_tools``) return deterministic template ROWS with no
``GraphPath``/assurance-labeled element at all. Recording fires on ANY
retrieval that touched a provisional graph fact, whether or not the current
tool-text rendering below happens to surface graph paths (it does not, today)
— under-recording is exactly the A-P1-5 risk this seam exists to close, so the
binding is fail-toward-flagging: the turn TOUCHED provisional context, full
stop.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import anyio.to_thread
import sqlalchemy as sa

from ne.api.agent_skills import current_run_skill_pins
from ne.api.citation_capture import record_served_citations
from ne.api.knowledge_scope import (
    DEFAULT_KNOWLEDGE_SCOPE_RESOLVER,
    KnowledgeScopeResolver,
    current_run_knowledge_scope,
)
from ne.api.knowledge_source_evidence import ExistingKnowledgeRead, KnowledgeSourceEvidenceAuthor
from ne.api.trace_assembly import assemble_trace
from ne.api.wire import graph_path_views, provisional_summary
from ne.brain.current_source_filter import (
    CurrentSourceDisclosureEvaluator,
    SourceAuthorityEvaluationError,
    authority_stamp_from_chunk,
    chunk_is_source_free,
)
from ne.brain.pipeline import RerankedRetrievalResult
from ne.brain.withholding import SourceHealthWithholding, begin_source_health_observation
from ne.contracts.knowledge import KnowledgeRead
from ne.contracts.runtime import ToolSpec
from ne.contracts.source_authority import EvidenceAssurance, SourceAuthorityStamp
from ne.contracts.types import ApprovalTier, Principal, RetrievalResult, ScoredChunk
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.middleware import current_tool_principal
from ne.runtime.provisional import current_provisional_recorder
from ne.runtime.source_results import require_authenticated_context
from ne.runtime.tools import ToolBinding
from ne.skills.config import SkillsSettings
from ne.skills.registry import SqlAlchemySkillRegistry
from ne.skills.tools import (
    SEARCH_TOOL_NAME,
    VIEW_TOOL_NAME,
    build_search_handler,
    build_skill_tool_specs,
    build_view_handler,
)
from ne.skills.visibility import GroupExpander

TOOL_NAME = "read.knowledge.search"
TRACE_TOOL_NAME = "read.trace"

#: The turn principal carrier — set ONLY by the chat route from the verified bearer.
TURN_PRINCIPAL: ContextVar[Principal | None] = ContextVar("ne_api_turn_principal", default=None)

#: The deterministic arg policy the guardrails enforce for the knowledge tool. A
#: tool absent from ``guardrails.tools`` is rejected fail-closed, so this MUST be
#: merged into ``govern_settings.guardrails.tools`` before the config hash freezes.
KNOWLEDGE_SEARCH_ARG_POLICY = ToolArgPolicy(
    args={"query": ArgSpec(kind=ToolArgKind.text, required=True)}
)

#: The arg policy for ``read.trace`` — one required ``identifier``-kind ``trace_id``
#: (the ``^[A-Za-z0-9_.-]{1,256}`` regex admits the 32-hex ``uuid4().hex`` trace ids
#: this platform mints). Same fail-closed pre-freeze merge requirement as above.
TRACE_ARG_POLICY = ToolArgPolicy(
    args={"trace_id": ArgSpec(kind=ToolArgKind.identifier, required=True)}
)


def _result_header(*, shown: int, total: int, withholding: SourceHealthWithholding) -> str:
    """The one honest line above a governed search's chunks.

    ONE grammar, always: ``[showing M of N retrieved chunks]`` with an OPTIONAL
    ``; K withheld ...`` clause. The clause is additive so
    ``ne.api.workflows.parse_retrieved_text`` — which RAISES on an envelope it does
    not recognise — keeps reading every header this tool can emit, and reads the
    withheld count out of it rather than inferring one.

    WHY THE CLAUSE EXISTS. ``[showing 0 of 0 retrieved chunks]`` is a TRUE count
    and a FALSE impression when a degraded connector is what emptied the result:
    the read matched content and disclosed none of it. A model told "0 of 0" reads
    that as a bad QUERY and broadens — which cannot possibly help, because the
    withholding is not about the query at all. That is the exact loop this clause
    ends: three consecutive broadenings against a corpus that was 100% withheld,
    stopped by the no-progress rule with a true-but-misleading "nothing was
    retrieved".

    ``total`` is widened to ``shown + withheld`` when withholding occurred, so
    ``N`` never claims fewer matches than the read actually had in hand.

    C12: counts and a fixed rule name ONLY. The connector is never named — which
    source is degraded is a deployment-level fact the /connect status surface
    owns, not something a Tool result hands to a model.
    """
    if not withholding.withheld:
        return f"[showing {shown} of {total} retrieved chunks]"
    matched = max(total, shown + withholding.withheld)
    reason = (
        "their governed source is currently degraded; this is a source-health outage, "
        "not an empty corpus, so rephrasing or broadening the query cannot surface them"
    )
    return (
        f"[showing {shown} of {matched} retrieved chunks; "
        f"{withholding.withheld} withheld — {reason}]"
    )


def _stale_note(withholding: SourceHealthWithholding) -> str | None:
    """The served-stale caveat line, or ``None`` when nothing served is stale.

    Emitted when chunks were SERVED from a source whose most recent sync attempt
    failed (the ``stale`` leg of the read-health split — a short transient
    failure run, not a denied ACL resolution and not a persisted outage, both of
    which withhold instead). The agent can then state the freshness caveat in
    its answer rather than being silently blinded or silently confident.

    Rendering constraints, both load-bearing for ``ne.api.workflows``'s strict
    envelope parser: the line is SEPARATE from the counts header (the header
    regex admits no second clause), and it contains no ``": "`` (the tolerant
    body parser would otherwise ingest it as a chunk). C12 as ever: a count and
    fixed wording only — the connector is never named.
    """
    if not withholding.stale_served:
        return None
    noun = "chunk" if withholding.stale_served == 1 else "chunks"
    return (
        f"[{withholding.stale_served} {noun} shown above came from a source whose most "
        "recent sync attempt failed — they reflect the last verified sync and may lag "
        "the source; state this caveat if the answer relies on them]"
    )


@dataclass(frozen=True, slots=True)
class KnowledgeSearchEvidence:
    """Existing-Run-only 009 authoring dependencies for the search Tool."""

    author: KnowledgeSourceEvidenceAuthor
    context_provider: Callable[[], ExistingKnowledgeRead | None]


class KnowledgeResultClass(StrEnum):
    """What KIND of result a governed knowledge read produced.

    The three cases below used to collapse into one ``LookupError`` reading
    "knowledge source evidence unavailable", which made an empty corpus, a
    source-free corpus and a genuine authority hole indistinguishable — to the
    operator AND to whoever read the audit record.
    """

    #: Nothing was retrieved. There is no source to vouch for, and saying so is
    #: the honest answer; this is NOT a refusal.
    EMPTY = "empty"
    #: Every retained chunk carries the server-authored source-free mark. There
    #: is no external authority head to name, so no contributor stamp exists —
    #: by design, not by omission (009 evidence is source-bearing-only).
    SOURCE_FREE = "source_free"
    #: At least one retained chunk is source-bearing, and every source-bearing
    #: chunk named its exact current-authority contributor.
    SOURCE_BEARING = "source_bearing"


@dataclass(frozen=True, slots=True)
class KnowledgeResultEvidence:
    """One retrieval's classification plus its exact deduplicated contributors."""

    classification: KnowledgeResultClass
    stamps: tuple[SourceAuthorityStamp, ...]


def _chunk_source_evidence(result: RetrievalResult) -> KnowledgeResultEvidence:
    """Classify a retained result and return every exact retained contributor.

    Fail-closed on the ONE case that is a genuine hole: a chunk that is not
    server-authored source-free and carries no readable authority stamp cannot be
    vouched for, so the WHOLE result is refused rather than served minus that
    chunk (a partial disclosure would silently answer from content whose
    authority is unknown).
    """
    stamps: list[SourceAuthorityStamp] = []
    for scored in result.chunks:
        if chunk_is_source_free(scored.chunk):
            continue
        stamp = authority_stamp_from_chunk(scored.chunk)
        if stamp is None:
            raise LookupError(
                f"chunk {scored.chunk.chunk_id!r} is source-bearing but carries no readable "
                "source-authority stamp; its authority cannot be established"
            )
        stamps.append(stamp)
    if stamps:
        return KnowledgeResultEvidence(
            classification=KnowledgeResultClass.SOURCE_BEARING,
            stamps=tuple(dict.fromkeys(stamps)),
        )
    classification = (
        KnowledgeResultClass.EMPTY if not result.chunks else KnowledgeResultClass.SOURCE_FREE
    )
    return KnowledgeResultEvidence(classification=classification, stamps=())


class CurrentSourceAuthorizedKnowledgeRead:
    """Apply canonical current-head/ACL authorization to every T1 disclosure.

    ``scan_multiplier`` — AUTHORIZATION headroom, and ONLY that
    =========================================================
    :meth:`search` asks its delegate for more rows than the caller's page,
    because :meth:`_authorized` drops rows: a chunk whose source-authority stamp
    the evaluator DENIES, and a source-bearing chunk carrying no readable stamp.
    Without headroom a single denial would return a short page.

    That window is NOT a store fetch. The delegate below this wrapper is
    ``ne.brain.pipeline.RetrievalPipeline`` (optionally under a ``GraphRetriever``),
    which treats whatever ``top_k`` it receives as a FINAL page and over-fetches
    ``candidate_multiplier`` times that many candidates to rerank. So this
    wrapper's window is the pipeline's OUTPUT size, and the pipeline multiplies
    it again — the two over-fetches compounded, and nothing reconciled them.
    At ``scan_multiplier=8`` and ``candidate_multiplier=4`` an Ask for 5 chunks
    reranked ``5 * 8 * 4 = 160`` documents, while ``ne.brain.reranker``'s
    docstring (20) and the documented pool (40) both still described the pool as
    they were BEFORE this wrapper existed. Measured on the demo box:
    ``retriever_ms=761 rerank_ms=227271`` — rerank was 98.9% of a 229 s search,
    in 5 sequential HTTP calls (TEI refuses rerank batches above 32).

    NE-RERANK-FANOUT splits the total between the two stages instead of letting
    them multiply: ``2`` here (authorization headroom) and ``4`` below (rerank
    pool), for ``5 -> 10 -> 40`` candidates on the default Ask — the documented
    figure, restored. Both purposes survive; neither multiplier is 1. A
    ``max_candidate_k`` ceiling on the pipeline bounds the product for the
    larger ``top_k`` values ``/v1/brain/search`` accepts (up to 50).

    ⚠️ What this DOES cost, stated rather than buried: the headroom is now 2x
    the page, not 8x. A principal for whom MORE THAN HALF of a reranked page is
    denied can now receive a short page where the old window would have refilled
    it. Page size cap, ordering (rerank order, first ``top_k`` authorized) and
    the returned envelope are otherwise byte-identical. This wrapper issues no
    continuation token — the score-keyed cursor lives in the separate
    ``ne.brain.current_source_filter`` selector lane, which this path never
    touches — so no outstanding continuation is affected.

    ⚠️ QUALITY IS UNMEASURED at the new pool. The Checkpoint-C eval gates were
    measured with the pre-NE-RERANK-FANOUT pipeline (P2-19); a smaller rerank pool can
    change which chunks surface. Re-measure before
    quoting them.
    """

    def __init__(
        self,
        delegate: KnowledgeRead,
        evaluator: CurrentSourceDisclosureEvaluator,
        *,
        scan_multiplier: int = 2,
    ) -> None:
        if scan_multiplier < 1:
            raise ValueError(f"scan_multiplier must be >= 1, got {scan_multiplier}")
        self._delegate = delegate
        self._evaluator = evaluator
        self._scan_multiplier = scan_multiplier

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        scan_limit = min(1000, max(top_k, top_k * self._scan_multiplier))
        result = await self._delegate.search(
            query, principal=principal, top_k=scan_limit, filters=filters
        )
        return await self._authorized(result, principal=principal, top_k=top_k)

    async def retrieve(self, chunk_ids: Sequence[str], *, principal: Principal) -> RetrievalResult:
        result = await self._delegate.retrieve(chunk_ids, principal=principal)
        return await self._authorized(result, principal=principal, top_k=len(chunk_ids))

    async def _authorized(
        self, result: RetrievalResult, *, principal: Principal, top_k: int
    ) -> RetrievalResult:
        context = None
        kept: list[ScoredChunk] = []
        # One retrieval, one principal, one access context: chunks of the same
        # artifact carry byte-identical stamps, so a stamp already decided in
        # THIS call is not re-asked (the memo lives and dies with this loop —
        # nothing is cached across retrievals, and a fault still raises loud).
        decided: dict[object, Any] = {}
        for scored in result.chunks:
            if chunk_is_source_free(scored.chunk):
                kept.append(scored)
            else:
                stamp = authority_stamp_from_chunk(scored.chunk)
                if stamp is None:
                    continue
                if context is None:
                    context = require_authenticated_context()
                    if context.principal_id != principal.id or tuple(
                        context.authorization_groups
                    ) != tuple(sorted(set(principal.groups))):
                        raise PermissionError(
                            "principal does not match authenticated source context"
                        )
                decision = decided.get(stamp)
                if decision is None:
                    try:
                        decision = await self._evaluator.evaluate(
                            stamp,
                            principal=principal,
                            access_context_hash=context.context_hash,
                        )
                    except Exception as exc:
                        # A denial is an answer and drops one chunk; an evaluator
                        # FAULT is the absence of one. Swallowing it here is what
                        # let a total authorization outage read as "nothing
                        # matched" — loud, and it names the class only (C12).
                        raise SourceAuthorityEvaluationError(
                            "the current-source disclosure evaluator failed "
                            f"({type(exc).__name__}); authorization is unknown"
                        ) from exc
                    decided[stamp] = decision
                if decision.authorized:
                    kept.append(scored)
            if len(kept) == top_k:
                break
        chunk_ids = {item.chunk.chunk_id for item in kept}
        return RetrievalResult(
            chunks=tuple(kept),
            citations=tuple(
                citation for citation in result.citations if citation.chunk_id in chunk_ids
            ),
            graph_paths=result.graph_paths,
        )


#: The arg policies the guardrails enforce for the two governed skill tools. A tool
#: absent from ``guardrails.tools`` is rejected fail-closed, so these MUST be merged
#: into ``govern_settings.guardrails.tools`` before the config hash freezes (only when
#: skills are enabled — the conditional-stamp discipline keeps default-off identical).
SKILLS_SEARCH_ARG_POLICY = ToolArgPolicy(
    args={"query": ArgSpec(kind=ToolArgKind.text, required=True)}
)
#: The ``^[A-Za-z0-9_.-]{1,256}`` identifier regex admits slug skill names.
SKILLS_VIEW_ARG_POLICY = ToolArgPolicy(
    args={"name": ArgSpec(kind=ToolArgKind.identifier, required=True)}
)


def build_skill_bindings(
    engine: sa.Engine,
    expander: GroupExpander,
    settings: SkillsSettings,
    *,
    run_pinned: bool = False,
) -> tuple[ToolBinding, ToolBinding]:
    """Pair the ne.skills tool specs + handlers into governed ToolBindings.

    ``principal_provider`` is the ONE universal runtime carrier
    (:func:`ne.runtime.middleware.current_tool_principal`, set by the executor) — it
    works for chat, worker cron, AND MCP because the executor sets it around every
    handler call. ``expander`` is the SAME ``PrincipalExpander`` the retrieval
    trimming uses (the F2 one-expander discipline).
    """
    registry = SqlAlchemySkillRegistry(engine)
    search_spec, view_spec = build_skill_tool_specs()
    search_handler = build_search_handler(registry, expander, settings, current_tool_principal)
    pin_kwargs = {"pin_provider": current_run_skill_pins} if run_pinned else {}
    view_handler = build_view_handler(
        registry,
        expander,
        settings,
        current_tool_principal,
        **pin_kwargs,
    )
    assert search_spec.name == SEARCH_TOOL_NAME
    assert view_spec.name == VIEW_TOOL_NAME
    return (
        ToolBinding(spec=search_spec, handler=search_handler),
        ToolBinding(spec=view_spec, handler=view_handler),
    )


def build_chat_bindings(
    retriever: KnowledgeRead,
    *,
    top_k: int = 5,
    extraction_mode: str,
    evidence: KnowledgeSearchEvidence | None = None,
    scope_resolver: KnowledgeScopeResolver = DEFAULT_KNOWLEDGE_SCOPE_RESOLVER,
) -> tuple[ToolBinding, ...]:
    """Build the chat agent's governed knowledge-search tool binding.

    ``extraction_mode`` is REQUIRED, keyword-only, and carries NO default
    (NE-T2X-5-RECORDER-WIRE, the NE-HGATE B-P0-1 precedent): it is the
    deployment's configured :attr:`~ne.brain_graph.config.BrainGraphSettings.
    extraction_mode`, threaded explicitly from the composition root
    (``ne.api.platform.build_platform``) — NEVER a guessed default at this
    call site, and NEVER sourced from request/model data.

    ``scope_resolver`` resolves the Agent-declared knowledge scope carried by
    ``ne.api.knowledge_scope.RUN_KNOWLEDGE_SCOPE`` into a narrowing store filter.
    It carries a default because its resolution TABLE is the single source of
    truth, not a deployment choice — and, unlike ``extraction_mode``, a forgotten
    wiring cannot silently widen anything: the handler reads the carrier
    unconditionally and a bound scope that will not resolve raises rather than
    searching. The parameter exists so a test can supply its own table.
    """

    async def _search(args: Mapping[str, object]) -> str:
        principal = TURN_PRINCIPAL.get()
        if principal is None:  # fail closed — no request data can act as the principal
            raise RuntimeError("no turn principal bound; refusing to search")
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query must not be empty")
        existing: ExistingKnowledgeRead | None = None
        if evidence is not None:
            existing = evidence.context_provider()
            if existing is None or existing.owner_principal_id != principal.id:
                raise LookupError("existing canonical Run evidence context is unavailable")

        # THE AGENT'S DECLARED KNOWLEDGE BOUNDARY, ENFORCED (see
        # ``ne.api.knowledge_scope``). The carrier is read INSIDE the handler, at
        # request time — never captured at build time — so the closure can never
        # hold a stale scope from another Run (the seam-as-data discipline the
        # provisional recorder below follows for the same reason).
        #
        # ``None`` means no Agent Run is in scope (an interactive chat turn) and
        # the search stays unfiltered under the principal's own ACL. A BOUND scope
        # either resolves to a narrowing filter or raises ``KnowledgeScopeUnresolved``
        # — it NEVER falls back to ``None``. That fallback is precisely the bug this
        # closes: a declared scope that matched nothing used to widen silently to
        # every corpus in the store, so an Agent granted one source read another.
        # The exception is deliberately not caught: it surfaces as an audited
        # ``tool.execute`` error, which is the loud failure the contract requires.
        scope = current_run_knowledge_scope()
        filters = None if scope is None else scope_resolver.filters(scope)

        # ``filters`` is a CALLER filter: ``ne.brain.retriever.and_combine_filters``
        # AND-combines it with the ACL trimming hook by intersection and rejects
        # ``$``-prefixed caller keys, so scope can only ever narrow. Authorization
        # (PrincipalAclFilter, and the current-head/ACL re-resolution in
        # ``CurrentSourceAuthorizedKnowledgeRead``) still runs unchanged on top.
        # Start this read's source-health observation BEFORE the search, so the
        # rendering below can tell an empty corpus from a total withholding (see
        # ``ne.brain.withholding``). Counts only; it changes nothing about which
        # chunks are served.
        withholding = begin_source_health_observation()
        result = await retriever.search(query, principal=principal, top_k=top_k, filters=filters)

        # NE-T2X-5-RECORDER-WIRE (design §6.1/§6.3, A-P1-5): record THIS
        # retrieval's provisional-graph-fact contribution into the ambient
        # turn accumulator, if one is active. The seam is read INSIDE the
        # handler, at request time — current_provisional_recorder() is called
        # HERE, never at build_chat_bindings() time, so the closure never
        # captures a stale/None recorder (the seam-as-data discipline: the
        # closure holds the CALLABLE seam, evaluated per-call). The count/refs
        # are DERIVED from the ONE pinned wire.py::provisional_summary
        # traversal via the ONE graph_path_views domain->view mapping — this
        # handler NEVER hand-rolls a second count over result.graph_paths
        # (the derive-don't-transliterate discipline). Recording fires even
        # though the tool-text rendering below drops graph paths entirely
        # (fail toward flagging — see the module docstring); a ``None``
        # recorder (no active ``provisional_context_scope`` — e.g. a future
        # non-turn caller) is a byte-identical no-op.
        summary = provisional_summary(graph_path_views(result.graph_paths))
        if summary.provisional_source_count > 0:
            recorder = current_provisional_recorder()
            if recorder is not None:
                recorder.record(
                    provisional_source_count=summary.provisional_source_count,
                    provisional_source_refs=summary.provisional_source_refs,
                    extraction_mode=extraction_mode,
                )

        # Plain-text rendering — chunk_id + text only; NO acl, no metadata dump (C12).
        # The header is counts-only (M shown, N pre-rerank candidates) and is
        # prompt-level honesty ONLY — retrieval chunks are NOT grounding-gate corpus
        # material (grounded mode excludes this tool entirely). A bare KnowledgeRead
        # delegate (no rerank stage, so no RerankedRetrievalResult) exposes no
        # candidate count; the M-of-M fallback below is literally true in that case:
        # M retrieved, M shown.
        shown = len(result.chunks)
        total = result.candidate_count if isinstance(result, RerankedRetrievalResult) else shown
        lines = [_result_header(shown=shown, total=total, withholding=withholding)]
        lines.extend(f"{sc.chunk.chunk_id}: {sc.chunk.text}" for sc in result.chunks)
        stale_note = _stale_note(withholding)
        if stale_note is not None:
            lines.append(stale_note)
        rendered = "\n".join(lines)

        if evidence is not None:
            # Raises when a source-bearing chunk could not name its authority —
            # the fail-closed leg. EMPTY and SOURCE_FREE are legitimate outcomes,
            # not holes: neither has a contributor to author, and 009 Knowledge
            # evidence is source-bearing-only (``claim`` REQUIRES >=1 stamp). The
            # rendering they return carries no source-bearing material, exactly
            # as it does when no evidence author is configured at all.
            classified = _chunk_source_evidence(result)
            if classified.classification is KnowledgeResultClass.SOURCE_BEARING:
                assert existing is not None
                protected = rendered.encode("utf-8")
                claimed = await evidence.author.claim(
                    existing=existing,
                    protected_result=protected,
                    locator={
                        "kind": "knowledge_search",
                        "contributors": [
                            stamp.model_dump(mode="json") for stamp in classified.stamps
                        ],
                        "chunk_ids": [scored.chunk.chunk_id for scored in result.chunks],
                    },
                    source_stamps=classified.stamps,
                    read_kind="search",
                    assurance=EvidenceAssurance.DIRECT,
                )
                rendered = claimed.protected_ref

        # Capture only after the principal-scoped retrieval and the complete
        # handler rendering succeeded. ``RetrievalResult.citations`` is already
        # the retriever's ACL-trimmed source-of-record set; never reconstruct it
        # from rendered chunks or model prose.
        record_served_citations(result)
        return rendered

    binding = ToolBinding(
        spec=ToolSpec(
            name=TOOL_NAME,
            # WHAT THIS TOOL COVERS, stated so the model can tell it apart from the
            # verified-answer lane (NE-LANE-CHOICE). "Search the governed knowledge
            # base" named the STORE rather than the CONTENT, and a store described as
            # holding the knowledge is the obvious first call for any question at all
            # -- including the record questions this tool cannot answer, whose amounts
            # and dates live in graph rows and never in a prose chunk. Live 2026-08-27:
            # a total-recorded-amount question searched here, found only process prose,
            # and reported the figure unavailable while a granted read.kg tool held the
            # rows. The description now says what comes BACK, and names the one thing
            # that does not, which is the fact the choice turns on. It names no other
            # tool: the model still chooses.
            description=(
                "Search governed documents for passages of prose (policy, procedure "
                "and notice wording). Returns written text only, not a record's "
                "stored field values."
            ),
            tier=ApprovalTier.TIER_0,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Free-text question or phrase searched over the governed"
                            " Meridian servicing knowledge base"
                        ),
                    }
                },
                "required": ["query"],
            },
        ),
        handler=_search,
    )
    return (binding,)


def build_trace_bindings(
    audit_engine: sa.Engine, ops_roles: tuple[str, ...]
) -> tuple[ToolBinding, ...]:
    """Build the ``read.trace`` governed tool binding (NE-167a agent-native parity).

    The handler mirrors ``routers/trace.py``'s ops-gate (same ``ApiSettings.ops_roles``
    group-disjoint fail-closed check — empty ``ops_roles`` refuses everyone) and
    returns the SAME ``TraceAssemblyView`` JSON the HTTP route serves, so route/tool
    structural parity holds by construction. Sync SQL (``assemble_trace`` — replay +
    verify_chain) runs in ONE ``anyio.to_thread`` hop, the ``routers/audit.py``
    discipline.
    """

    async def _read_trace(args: Mapping[str, object]) -> str:
        principal = TURN_PRINCIPAL.get()
        if principal is None:  # fail closed — no request data can act as the principal
            raise RuntimeError("no turn principal bound; refusing to read trace")
        if not ops_roles or set(principal.groups).isdisjoint(ops_roles):
            raise PermissionError("trace read requires an ops role")
        trace_id = str(args.get("trace_id", "")).strip()
        if not trace_id:
            raise ValueError("trace_id must not be empty")
        view = await anyio.to_thread.run_sync(
            functools.partial(assemble_trace, audit_engine, trace_id)
        )
        if view is None:
            raise ValueError(f"trace not found: {trace_id}")
        return view.model_dump_json()

    binding = ToolBinding(
        spec=ToolSpec(
            name=TRACE_TOOL_NAME,
            description=(
                "Read one agent run's assembled governed trace "
                "(ops-gated, identifiers/counts/costs only)"
            ),
            tier=ApprovalTier.TIER_0,
            input_schema={
                "type": "object",
                "properties": {"trace_id": {"type": "string"}},
                "required": ["trace_id"],
            },
        ),
        handler=_read_trace,
    )
    return (binding,)
