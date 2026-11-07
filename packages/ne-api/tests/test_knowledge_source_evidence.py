# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused controls for the package-local 011C evidence author."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from ne.api.knowledge_source_evidence import (
    ExistingKnowledgeRead,
    KnowledgeEvidenceDetail,
    KnowledgeSourceEvidenceAuthor,
    current_existing_knowledge_read,
)
from ne.api.tools import TURN_PRINCIPAL, KnowledgeSearchEvidence, build_chat_bindings
from ne.contracts.source_authority import (
    NO_SOURCE_STATUS,
    SOURCE_STATUS_METADATA_KEY,
    EvidenceAssurance,
    GovernedLocalT3SourcePoint,
    SourceAuthorityStamp,
    SourceBoundToolResultSetRef,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
    VerifiedEmptyResultReceipt,
    VerifiedRowContributorReceipt,
    canonical_source_authority_stamps,
)
from ne.contracts.types import Chunk, Principal, RetrievalResult, ScoredChunk
from ne.runtime.run_audit_receipts import (
    ExistingRunEvidenceContext,
    ExistingRunEvidenceProvider,
    RunToolCallContext,
    run_tool_call_scope,
)
from ne.runtime.run_store import (
    AgentRun,
    EvidenceRecord,
    EvidenceSetRecord,
    RunAuditDecisionKind,
    RunAuditReceipt,
    RunState,
)
from ne.runtime.source_results import decode_protected_reference


async def _invoke(
    handler: Callable[[Mapping[str, object]], Awaitable[str]], args: Mapping[str, object]
) -> str:
    return await handler(args)


def _stamp() -> SourceAuthorityStamp:
    contract = SourceContractIdentity(
        source_kind="governed_local_t3",
        descriptor_schema_version=1,
        descriptor_hash="1" * 64,
    )
    source = SourceIdentity(
        deployment_id="deployment-1",
        source_kind="governed_local_t3",
        connector_instance_id="ontology-1",
        point=GovernedLocalT3SourcePoint(dataset_name="meridian", contributor_key="contributor-1"),
        generation=1,
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="2" * 64,
        content_snapshot_hash="3" * 64,
        acl_snapshot_hash="4" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision="revision-1",
        acl_snapshot_hash="4" * 64,
        source_contract=contract,
        contract_epoch=1,
        authority_receipt_id="receipt-1",
        authority_receipt_hash="5" * 64,
    )


def _run_tool_context() -> RunToolCallContext:
    now = datetime(2026, 8, 23, tzinfo=UTC)
    run = AgentRun(
        run_id="run-1",
        conversation_id="conversation-1",
        owner_principal_id="principal-1",
        department="engineering",
        conversation_agent_name="Meridian Analyst",
        agent_id="agent-1",
        agent_version=1,
        strategy="agentic",
        config_hash="1" * 64,
        capability_snapshot_hash="2" * 64,
        capability_snapshot_receipt_id="capability-receipt-1",
        sequence=1,
        display_label="Run 1",
        status=RunState.RUNNING,
        created_at=now,
        updated_at=now,
        terminal_at=None,
    )
    receipt = RunAuditReceipt(
        run_audit_receipt_id="run-audit-receipt-1",
        run_id=run.run_id,
        attempt_id="attempt-1",
        run_event_id="run-event-1",
        step_id="step-1",
        tool_call_id="call-1",
        approval_id=None,
        decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
        audit_event_id="audit-event-1",
        audit_sequence=1,
        audit_record_hash="3" * 64,
        config_hash=run.config_hash,
        created_at=now,
    )
    return RunToolCallContext(
        run=run,
        receipt=receipt,
        conversation_id=run.conversation_id,
        turn_id="turn-1",
        turn_sequence=1,
        attachment_id="attachment-1",
        evidence=ExistingRunEvidenceProvider.from_existing(run=run, receipt=receipt),
    )


def test_current_read_uses_only_exact_run_tool_call_carrier_and_resets() -> None:
    assert current_existing_knowledge_read() is None
    context = _run_tool_context()
    with run_tool_call_scope(context):
        first = current_existing_knowledge_read()
        second = current_existing_knowledge_read()
        assert first == ExistingKnowledgeRead(
            evidence=ExistingRunEvidenceContext(
                run_id="run-1",
                step_id="step-1",
                tool_call_id="call-1",
                read_ordinal=0,
            ),
            owner_principal_id="principal-1",
            attempt_id="attempt-1",
            turn_id="turn-1",
        )
        assert second is not None
        assert second.evidence.read_ordinal == 1
    assert current_existing_knowledge_read() is None

    with (
        pytest.raises(RuntimeError, match="handler failed"),
        run_tool_call_scope(_run_tool_context()),
    ):
        assert current_existing_knowledge_read() is not None
        raise RuntimeError("handler failed")
    assert current_existing_knowledge_read() is None


class _Store:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def claim_evidence(self, **kwargs: Any) -> EvidenceRecord:
        self.calls.append(kwargs)
        declaration = kwargs["declaration"]
        located = declaration.locator_plaintext is not None
        return EvidenceRecord(
            evidence_id="evidence-1",
            invocation_key="invocation-1",
            locator_status="located" if located else "evidence_locator_unavailable",
            locator_id="locator-1" if located else None,
            pending_result_id="pending-1",
            producing_event_id="event-1",
        )

    async def claim_evidence_set(self, **kwargs: Any) -> EvidenceSetRecord:
        self.calls.append(kwargs)
        declaration = kwargs["declaration"]
        evidence = tuple(
            EvidenceRecord(
                evidence_id=f"evidence-{index}",
                invocation_key=f"invocation-{index}",
                locator_status="located",
                locator_id=f"locator-{index}",
                pending_result_id="pending-set",
                producing_event_id=f"event-{index}",
            )
            for index, _contributor in enumerate(declaration.rows[0].contributors)
        )
        return EvidenceSetRecord(
            evidence_set_id="set-1",
            set_key="set-key",
            pending_result_id="pending-set",
            evidence=evidence,
            row_event_ids=("row-event-1",),
        )


def test_claim_uses_exact_existing_run_identity_and_returns_only_opaque_ref() -> None:
    store = _Store()
    author = KnowledgeSourceEvidenceAuthor(store)
    protected = b"unique protected knowledge bytes"
    existing = ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=3
        ),
        owner_principal_id="principal-1",
        attempt_id="attempt-1",
        turn_id="turn-1",
    )

    claimed = asyncio.run(
        author.claim(
            existing=existing,
            protected_result=protected,
            locator={"revision": "revision-1"},
            source_stamps=(_stamp(),),
            read_kind="search",
            assurance=EvidenceAssurance.DIRECT,
        )
    )

    [call] = store.calls
    assert call["run_id"] == "run-1"
    assert call["attempt_id"] == "attempt-1"
    declaration = call["declaration"]
    assert declaration.step_id == "step-1"
    assert declaration.tool_call_id == "call-1"
    assert declaration.read_ordinal == 3
    assert declaration.pending_result_plaintext == protected
    assert protected.decode() not in claimed.protected_ref
    reference = decode_protected_reference(claimed.protected_ref)
    assert reference is not None
    assert reference.reference_id == "pending-1"
    assert declaration.safe_event_detail == {
        "read_kind": "search",
        "evidence_status": "located",
        "contributor_count": 1,
        "evidence_assurance": "direct",
    }


def test_author_exposes_no_caller_authored_safe_event_detail() -> None:
    parameters = inspect.signature(KnowledgeSourceEvidenceAuthor.claim).parameters
    assert "safe_event_detail" not in parameters


@pytest.mark.parametrize(
    "forbidden",
    [
        "source_identity",
        "locator",
        "title",
        "text",
        "arguments",
        "output",
        "query",
        "result",
    ],
)
def test_closed_detail_rejects_forbidden_knowledge_fields(forbidden: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(KnowledgeEvidenceDetail).validate_python(
            {
                "read_kind": "search",
                "evidence_status": "located",
                "contributor_count": 1,
                "evidence_assurance": "direct",
                forbidden: "protected sentinel",
            }
        )


def test_read_kind_and_evidence_assurance_are_closed_independent_vocabularies() -> None:
    adapter = TypeAdapter(KnowledgeEvidenceDetail)
    for read_kind in ("search", "graph_path", "verified_t3", "verified_t3_empty"):
        detail = {
            "read_kind": read_kind,
            "evidence_status": "located",
            "contributor_count": 1,
            "evidence_assurance": "direct",
        }
        if read_kind == "verified_t3":
            detail.update({"row_anchor": "a" * 64, "receipt_hash": "b" * 64})
        elif read_kind == "verified_t3_empty":
            detail.update({"receipt_hash": "b" * 64})
        assert adapter.validate_python(detail).read_kind == read_kind
    for invalid in ("source", "confirmed", "extracted", "unknown"):
        with pytest.raises(ValidationError):
            adapter.validate_python(
                {
                    "read_kind": invalid,
                    "evidence_status": "located",
                    "contributor_count": 1,
                    "evidence_assurance": "direct",
                }
            )


def test_missing_contributor_refuses_before_any_store_effect() -> None:
    store = _Store()
    author = KnowledgeSourceEvidenceAuthor(store)
    existing = ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=0
        ),
        owner_principal_id="principal-1",
        attempt_id="attempt-1",
        turn_id="turn-1",
    )

    try:
        asyncio.run(
            author.claim(
                existing=existing,
                protected_result=b"secret",
                locator=None,
                source_stamps=(),
                read_kind="search",
                assurance=EvidenceAssurance.DIRECT,
            )
        )
    except ValueError as exc:
        assert "contributor stamp" in str(exc)
    else:  # pragma: no cover - assertion spelling makes failure explicit
        raise AssertionError("missing contributor stamp was accepted")
    assert store.calls == []


def test_locator_status_is_derived_by_the_author_on_both_arms() -> None:
    store = _Store()
    author = KnowledgeSourceEvidenceAuthor(store)
    existing = ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=0
        ),
        owner_principal_id="principal-1",
        attempt_id="attempt-1",
        turn_id="turn-1",
    )
    arms = (({"revision": "r"}, "located"), (None, "evidence_locator_unavailable"))
    for locator, expected in arms:
        claimed = asyncio.run(
            author.claim(
                existing=existing,
                protected_result=b"secret",
                locator=locator,
                source_stamps=(_stamp(),),
                read_kind="search",
                assurance=EvidenceAssurance.DIRECT,
            )
        )
        declaration = store.calls[-1]["declaration"]
        assert declaration.safe_event_detail["evidence_status"] == expected
        assert claimed.evidence.locator_status == expected


def test_contributor_count_is_exact_and_refuses_above_the_bound() -> None:
    store = _Store()
    author = KnowledgeSourceEvidenceAuthor(store)
    existing = ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=0
        ),
        owner_principal_id="principal-1",
        attempt_id="attempt-1",
        turn_id="turn-1",
    )
    stamps = tuple(
        _stamp().model_copy(
            update={
                "authority_receipt_id": f"receipt-{index}",
                "authority_receipt_hash": f"{index:064x}",
            }
        )
        for index in range(129)
    )
    with pytest.raises(ValueError, match="exceeds contributor bound"):
        asyncio.run(
            author.claim(
                existing=existing,
                protected_result=b"secret",
                locator=None,
                source_stamps=stamps,
                read_kind="search",
                assurance=EvidenceAssurance.DIRECT,
            )
        )
    assert store.calls == []


def test_verified_rows_author_one_ordered_evidence_member_per_contributor() -> None:
    store = _Store()
    author = KnowledgeSourceEvidenceAuthor(store)
    existing = ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=0
        ),
        owner_principal_id="principal-1",
        attempt_id="attempt-1",
        turn_id="turn-1",
    )
    second = _stamp().model_copy(
        update={"authority_receipt_id": "receipt-2", "authority_receipt_hash": "6" * 64}
    )
    receipt = VerifiedRowContributorReceipt(
        template_id="template-1",
        template_version=1,
        canonical_row_hash="a" * 64,
        contributor_schema_version=1,
        contributors=canonical_source_authority_stamps((_stamp(), second)),
    )
    claimed = asyncio.run(
        author.claim_verified_set(
            existing=existing,
            protected_result=b"protected verified result",
            receipts=(receipt,),
        )
    )

    [call] = store.calls
    [row] = call["declaration"].rows
    assert row.row_anchor != receipt.canonical_row_hash
    assert len(row.row_anchor) == 64
    assert row.receipt_hash == receipt.receipt_hash
    assert tuple(item.source_stamp for item in row.contributors) == receipt.contributors
    assert all(item.assurance is EvidenceAssurance.DIRECT for item in row.contributors)
    reference = decode_protected_reference(claimed.protected_ref)
    assert isinstance(reference, SourceBoundToolResultSetRef)
    assert reference.evidence_ids == ("evidence-0", "evidence-1")


def test_verified_empty_result_authors_one_snapshot_bound_evidence_record() -> None:
    store = _Store()
    author = KnowledgeSourceEvidenceAuthor(store)
    existing = ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=0
        ),
        owner_principal_id="principal-1",
        attempt_id="attempt-1",
        turn_id="turn-1",
    )
    receipt = VerifiedEmptyResultReceipt(
        template_id="template-1",
        template_version=1,
        canonical_args_hash="a" * 64,
        dataset_graphs_hash="b" * 64,
        contributor_schema_version=1,
        contributors=(_stamp(),),
    )
    claimed = asyncio.run(
        author.claim_verified_empty(
            existing=existing,
            protected_result=b"[showing 0 of 0 rows]",
            receipt=receipt,
        )
    )

    [call] = store.calls
    declaration = call["declaration"]
    assert declaration.source_stamps == receipt.contributors
    assert declaration.safe_event_detail == {
        "read_kind": "verified_t3_empty",
        "evidence_status": "located",
        "contributor_count": 1,
        "evidence_assurance": "direct",
        "receipt_hash": receipt.receipt_hash,
    }
    reference = decode_protected_reference(claimed.protected_ref)
    assert reference is not None
    assert reference.reference_id == "pending-1"


def test_search_tool_persists_protected_bytes_but_returns_only_opaque_ref() -> None:
    stamp = _stamp()
    result = RetrievalResult(
        chunks=(
            ScoredChunk(
                chunk=Chunk(
                    chunk_id="chunk-1",
                    document_id="doc-1",
                    text="protected sentinel",
                    metadata={"ne_source_authority_stamp": stamp.model_dump(mode="json")},
                ),
                score=1.0,
            ),
        ),
        citations=(),
    )

    class _Retriever:
        async def search(self, *_: Any, **__: Any) -> RetrievalResult:
            return result

        async def retrieve(self, *_: Any, **__: Any) -> RetrievalResult:
            raise NotImplementedError

    store = _Store()
    author = KnowledgeSourceEvidenceAuthor(store)
    existing = ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=0
        ),
        owner_principal_id="principal-1",
        attempt_id="attempt-1",
        turn_id="turn-1",
    )
    (binding,) = build_chat_bindings(
        _Retriever(),  # type: ignore[arg-type]
        extraction_mode="read_flagged",
        evidence=KnowledgeSearchEvidence(author=author, context_provider=lambda: existing),
    )
    token = TURN_PRINCIPAL.set(Principal(id="principal-1", groups=("analyst",)))
    try:
        output = asyncio.run(_invoke(binding.handler, {"query": "sentinel"}))
    finally:
        TURN_PRINCIPAL.reset(token)

    assert "protected sentinel" not in output
    [call] = store.calls
    assert b"protected sentinel" in call["declaration"].pending_result_plaintext


def test_search_without_existing_run_context_has_zero_retrieval_or_evidence_effects() -> None:
    calls: list[str] = []

    class _Retriever:
        async def search(self, *_: Any, **__: Any) -> RetrievalResult:
            calls.append("search")
            return RetrievalResult(chunks=(), citations=())

        async def retrieve(self, *_: Any, **__: Any) -> RetrievalResult:
            raise NotImplementedError

    store = _Store()
    (binding,) = build_chat_bindings(
        _Retriever(),  # type: ignore[arg-type]
        extraction_mode="read_flagged",
        evidence=KnowledgeSearchEvidence(
            author=KnowledgeSourceEvidenceAuthor(store), context_provider=lambda: None
        ),
    )
    token = TURN_PRINCIPAL.set(Principal(id="principal-1", groups=("analyst",)))
    try:
        try:
            asyncio.run(_invoke(binding.handler, {"query": "sentinel"}))
        except LookupError as exc:
            assert "existing canonical Run" in str(exc)
        else:
            raise AssertionError("unbound evidence context was accepted")
    finally:
        TURN_PRINCIPAL.reset(token)
    assert calls == []
    assert store.calls == []


# -- 015A0-ENFORCEMENT: the three outcomes that used to be one error ----------


def _existing_read() -> ExistingKnowledgeRead:
    return ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=0
        ),
        owner_principal_id="principal-1",
        attempt_id="attempt-1",
        turn_id="turn-1",
    )


def _search_tool(result: RetrievalResult, store: _Store) -> Any:
    class _Retriever:
        async def search(self, *_: Any, **__: Any) -> RetrievalResult:
            return result

        async def retrieve(self, *_: Any, **__: Any) -> RetrievalResult:
            raise NotImplementedError

    existing = _existing_read()
    (binding,) = build_chat_bindings(
        _Retriever(),  # type: ignore[arg-type]
        extraction_mode="read_flagged",
        evidence=KnowledgeSearchEvidence(
            author=KnowledgeSourceEvidenceAuthor(store), context_provider=lambda: existing
        ),
    )
    return binding


def _run_search(result: RetrievalResult, store: _Store) -> str:
    binding = _search_tool(result, store)
    token = TURN_PRINCIPAL.set(Principal(id="principal-1", groups=("analyst",)))
    try:
        return asyncio.run(_invoke(binding.handler, {"query": "sentinel"}))
    finally:
        TURN_PRINCIPAL.reset(token)


def _scored(chunk_id: str, *, metadata: Mapping[str, Any]) -> ScoredChunk:
    return ScoredChunk(
        chunk=Chunk(
            chunk_id=chunk_id,
            document_id=f"doc-{chunk_id}",
            text=f"{chunk_id} body",
            metadata=dict(metadata),
        ),
        score=1.0,
    )


def test_a_search_that_found_nothing_says_so_instead_of_claiming_evidence() -> None:
    """Empty is an ANSWER, not a hole: there is no source to vouch for."""
    store = _Store()
    output = _run_search(RetrievalResult(chunks=(), citations=()), store)
    assert output == "[showing 0 of 0 retrieved chunks]"
    assert store.calls == []


def test_a_source_free_result_is_served_without_a_contributor_stamp() -> None:
    """009 Knowledge evidence is source-bearing-only; a source-free read has none.

    The mark is the server-authored one written by the governed ingest path — the
    read side never infers it from a connector label.
    """
    store = _Store()
    result = RetrievalResult(
        chunks=(_scored("free-1", metadata={SOURCE_STATUS_METADATA_KEY: NO_SOURCE_STATUS}),),
        citations=(),
    )
    output = _run_search(result, store)
    assert "free-1 body" in output
    assert store.calls == []


def test_a_source_bearing_chunk_with_no_stamp_refuses_the_whole_result() -> None:
    """The paired mutation: authority that cannot be established is still refused.

    Distinguishably so — the message names the chunk and the reason, and is not
    the unbound-Run-context message the same call site can also raise.
    """
    store = _Store()
    result = RetrievalResult(
        chunks=(
            _scored("free-1", metadata={SOURCE_STATUS_METADATA_KEY: NO_SOURCE_STATUS}),
            _scored("bearing-1", metadata={"source_system": "wiki"}),
        ),
        citations=(),
    )
    with pytest.raises(LookupError) as raised:
        _run_search(result, store)
    message = str(raised.value)
    assert "bearing-1" in message
    assert "authority cannot be established" in message
    assert "existing canonical Run" not in message
    assert store.calls == []


def test_an_evaluator_fault_is_loud_rather_than_an_invisible_denial() -> None:
    """A denial drops one chunk; an outage must not look like one.

    The pairing: the same code path that correctly withholds a denied chunk must
    NOT quietly withhold a chunk it simply could not decide about.
    """
    from ne.api.tools import CurrentSourceAuthorizedKnowledgeRead
    from ne.brain.current_source_filter import (
        SOURCE_AUTHORITY_STAMP_METADATA_KEY,
        SourceAuthorityEvaluationError,
    )
    from ne.contracts.identity import AuthenticatedRequestContext
    from ne.contracts.source_authority import SourceAuthorityDecision
    from ne.runtime.source_results import authenticated_request_scope

    stamped = _scored(
        "bearing-1",
        metadata={SOURCE_AUTHORITY_STAMP_METADATA_KEY: _stamp().model_dump(mode="json")},
    )
    result = RetrievalResult(chunks=(stamped,), citations=())

    class _Retriever:
        async def search(self, *_: Any, **__: Any) -> RetrievalResult:
            return result

        async def retrieve(self, *_: Any, **__: Any) -> RetrievalResult:
            return result

    class _Evaluator:
        def __init__(self, *, fault: bool) -> None:
            self._fault = fault

        async def evaluate(self, *_: Any, **__: Any) -> Any:
            if self._fault:
                raise TimeoutError("authority head unreachable")

            class _Denied:
                authorized = False
                decision = SourceAuthorityDecision.PERMANENT_WITHHELD

            return _Denied()

    principal = Principal(id="principal-1", groups=("analyst",))
    context = AuthenticatedRequestContext(
        principal_id="principal-1",
        authorization_groups=("analyst",),
        issuer="https://issuer.test",
        expires_at=4102444800,
    )

    def _search(*, fault: bool) -> RetrievalResult:
        retriever = CurrentSourceAuthorizedKnowledgeRead(
            _Retriever(),  # type: ignore[arg-type]
            _Evaluator(fault=fault),  # type: ignore[arg-type]
        )
        with authenticated_request_scope(context):
            return asyncio.run(retriever.search("q", principal=principal, top_k=5))

    # A DENIAL is an answer: the chunk is withheld and the read succeeds empty.
    assert _search(fault=False).chunks == ()
    # A FAULT is the absence of one, and must not be served as the same thing.
    with pytest.raises(SourceAuthorityEvaluationError) as raised:
        _search(fault=True)
    assert "TimeoutError" in str(raised.value)


def test_partial_multikey_read_claims_its_empty_member_coverage_entry() -> None:
    """NE-KG-MULTIKEY partial coverage: rows AND the empty-member receipt, one set.

    The live failure this pins: a windowed multi-key read matched 2 of 3 requested
    keys, minted an empty-member receipt for the third — and the claim path DROPPED
    it, so the durable evidence trail could not distinguish "asked for 3, matched 2"
    from a fully-matched read. The receipt now rides the same atomic set.
    """

    store = _Store()
    author = KnowledgeSourceEvidenceAuthor(store)
    existing = ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=0
        ),
        owner_principal_id="principal-1",
        attempt_id="attempt-1",
        turn_id="turn-1",
    )
    row_receipt = VerifiedRowContributorReceipt(
        template_id="template-1",
        template_version=1,
        canonical_row_hash="a" * 64,
        contributor_schema_version=1,
        contributors=(_stamp(),),
    )
    empty_receipt = VerifiedEmptyResultReceipt(
        template_id="template-1",
        template_version=1,
        canonical_args_hash="b" * 64,
        dataset_graphs_hash="c" * 64,
        contributor_schema_version=1,
        contributors=(_stamp(),),
    )

    claimed = asyncio.run(
        author.claim_verified_set(
            existing=existing,
            protected_result=b"protected partial verified result",
            receipts=(row_receipt,),
            empty_member_receipt=empty_receipt,
        )
    )

    [call] = store.calls
    rows = call["declaration"].rows
    assert len(rows) == 2
    data_row, coverage_row = rows
    assert data_row.receipt_hash == row_receipt.receipt_hash
    assert coverage_row.receipt_hash == empty_receipt.receipt_hash
    # The coverage anchor derives under its OWN domain tag — never a row anchor.
    assert coverage_row.row_anchor != data_row.row_anchor
    assert len(coverage_row.row_anchor) == 64
    [coverage_member] = coverage_row.contributors
    assert coverage_member.safe_event_detail == {
        "read_kind": "verified_t3_empty",
        "evidence_status": "located",
        "contributor_count": 1,
        "evidence_assurance": "direct",
        "receipt_hash": empty_receipt.receipt_hash,
    }
    # The locator is the EXACT closed shape the projection parses for an empty read.
    locator = json.loads(coverage_member.locator_plaintext)
    assert set(locator) == {"kind", "receipt", "evidence_assurance"}
    assert locator["kind"] == "verified_t3_empty"
    assert locator["receipt"] == empty_receipt.model_dump(mode="json")
    # The served reference's stamp set covers the coverage contributors too, so it
    # stays equal to the canonical set the run store packs from EVERY declared row.
    reference = decode_protected_reference(claimed.protected_ref)
    assert isinstance(reference, SourceBoundToolResultSetRef)
    assert reference.stamps == canonical_source_authority_stamps(
        row_receipt.contributors + empty_receipt.contributors
    )


def test_fully_matched_multikey_read_declares_exactly_its_row_receipts() -> None:
    """A genuinely complete read is unaffected: no coverage entry, same declaration."""

    store = _Store()
    author = KnowledgeSourceEvidenceAuthor(store)
    existing = ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=0
        ),
        owner_principal_id="principal-1",
        attempt_id="attempt-1",
        turn_id="turn-1",
    )
    row_receipt = VerifiedRowContributorReceipt(
        template_id="template-1",
        template_version=1,
        canonical_row_hash="a" * 64,
        contributor_schema_version=1,
        contributors=(_stamp(),),
    )

    asyncio.run(
        author.claim_verified_set(
            existing=existing,
            protected_result=b"protected complete verified result",
            receipts=(row_receipt,),
            empty_member_receipt=None,
        )
    )

    [call] = store.calls
    rows = call["declaration"].rows
    assert len(rows) == 1
    assert rows[0].receipt_hash == row_receipt.receipt_hash
    assert all(
        item.safe_event_detail["read_kind"] == "verified_t3" for item in rows[0].contributors
    )
