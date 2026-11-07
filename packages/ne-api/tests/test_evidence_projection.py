# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused proof for exact-id, current-authority evidence projection."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal, cast

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from ne.api.evidence_projection import (
    EvidenceProjector,
    EvidenceResourceNotFound,
    MaterializedDisclosure,
    PersistedAssociation,
    PersistedClaim,
    PersistedEvidence,
    PersistedRunEvidence,
    PersistedVerifier,
    RunStoreCurrentEvidenceMaterializer,
    ServedVersionKey,
    ServedVersionResolver,
    SqlAlchemyEvidenceProjectionReader,
    _locator_artifact_keys,
)
from ne.api.evidence_wire import ServedDocumentVersionView
from ne.api.governance_projection import PersistedAuditReceipt
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    VerifiedEmptyResultReceipt,
    VerifiedRowContributorReceipt,
)
from ne.contracts.types import Principal
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_store import AgentRunStore, StoredSourcePayload, ensure_run_schema
from ne.runtime.source_results import authenticated_request_scope
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .test_knowledge_source_evidence import _stamp

NOW = datetime(2026, 8, 22, tzinfo=UTC)
H = "a" * 64


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _Reader:
    def __init__(self, run: PersistedRunEvidence | None) -> None:
        self.run = run
        self.calls: list[tuple[str, str, str]] = []

    async def load_exact(
        self, *, agent_id: str, run_id: str, owner_principal_id: str
    ) -> PersistedRunEvidence | None:
        self.calls.append((agent_id, run_id, owner_principal_id))
        return self.run


class _Materializer:
    def __init__(
        self,
        *,
        locators: dict[str, MaterializedDisclosure],
        output: MaterializedDisclosure,
    ) -> None:
        self.locators = locators
        self.output = output
        self.calls: list[tuple[str, str]] = []

    async def resolve_locator(self, evidence: PersistedEvidence, **kwargs: object):
        self.calls.append(("locator", evidence.evidence_id))
        return self.locators[evidence.evidence_id]

    async def resolve_output(self, run: PersistedRunEvidence, **kwargs: object):
        self.calls.append(("output", run.run_id))
        return self.output

    async def resolve_claim(
        self, run: PersistedRunEvidence, claim: PersistedClaim, **kwargs: object
    ):
        self.calls.append(("claim", claim.claim_id))
        if self.output.payload is None:
            return MaterializedDisclosure("unknown")
        return MaterializedDisclosure(
            self.output.decision,
            self.output.payload[claim.span_start_byte : claim.span_end_byte],
        )


def _locator(title: str) -> bytes:
    return json.dumps(
        {
            "source_system": "Meridian",
            "title": title,
            "revision": "revision-7",
            "content_hash": "b" * 64,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _graph_locator() -> bytes:
    path = {
        "hops": [
            {
                "subject": {
                    "entity_class": "provider",
                    "entity_key": "acme",
                    "display": "Acme",
                    "assurance": "source",
                    "source_ref": "document-a",
                },
                "edge_kind": "services",
                "obj": {
                    "entity_class": "customer",
                    "entity_key": "meridian",
                    "display": "Meridian",
                    "assurance": "confirmed",
                    "source_ref": "document-b",
                },
                "source_system": "m365",
                "source_ref": "edge-document",
                "assurance": "extracted",
            }
        ]
    }
    return json.dumps(
        {
            "kind": "knowledge_graph_path",
            "path": path,
            "hop_source_stamps": [[_stamp().model_dump(mode="json")]],
            "evidence_assurance": "inferred",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _verified_t3_locator() -> bytes:
    stamp = _stamp()
    receipt = VerifiedRowContributorReceipt(
        template_id="template-b1",
        template_version=1,
        canonical_row_hash="3" * 64,
        contributor_schema_version=1,
        contributors=(stamp,),
    )
    row_anchor = _hash(
        b"ne.verified-evidence-row-anchor.v1\x00" + receipt.receipt_hash.encode("ascii")
    )
    return json.dumps(
        {
            "kind": "verified_t3",
            "row_anchor": row_anchor,
            "receipt": receipt.model_dump(mode="json"),
            "contributor_index": 0,
            "contributor": stamp.model_dump(mode="json"),
            "evidence_assurance": "direct",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _verified_t3_empty_locator() -> bytes:
    receipt = VerifiedEmptyResultReceipt(
        template_id="template-b1",
        template_version=1,
        canonical_args_hash="4" * 64,
        dataset_graphs_hash="5" * 64,
        contributor_schema_version=1,
        contributors=(_stamp(),),
    )
    return json.dumps(
        {
            "kind": "verified_t3_empty",
            "receipt": receipt.model_dump(mode="json"),
            "evidence_assurance": "direct",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _evidence(evidence_id: str, locator: bytes, ordinal: int) -> PersistedEvidence:
    return PersistedEvidence(
        evidence_id=evidence_id,
        run_id="run-1",
        owner_principal_id="owner-1",
        attempt_id="attempt-1",
        step_id=f"step-{ordinal}",
        tool_call_id=f"call-{ordinal}",
        read_ordinal=ordinal,
        locator_status="located",
        locator_id=f"locator-{ordinal}",
        locator_hash=_hash(locator),
        pending_result_id=f"pending-{ordinal}",
        producing_event_id=f"event-evidence-{ordinal}",
        content_hash=H,
        assurance="direct",
        created_at=NOW,
    )


def _association(claim: str, evidence: PersistedEvidence, receipt: str) -> PersistedAssociation:
    return PersistedAssociation(
        association_id=f"association-{claim}",
        run_id="run-1",
        claim_id=claim,
        evidence_id=evidence.evidence_id,
        locator_hash=evidence.locator_hash or H,
        served_citation_ref_hash="c" * 64,
        producing_event_id=evidence.producing_event_id,
        delivery_receipt_id=receipt,
        association_hash=("d" if claim == "claim-a" else "e") * 64,
    )


def _claim(
    claim_id: str,
    index: int,
    start: int,
    end: int,
    association: PersistedAssociation | None,
) -> PersistedClaim:
    return PersistedClaim(
        claim_id=claim_id,
        run_id="run-1",
        claim_key=f"key-{index}",
        claim_index=index,
        claim_verdict="grounded" if association is not None else "unavailable",
        display_state="protected_source_slice",
        protected_ref="output-1",
        served_output_hash=_hash(b"same same"),
        span_start_byte=start,
        span_end_byte=end,
        claim_content_hash=_hash(b"same"),
        association_status="associated" if association is not None else "association_unavailable",
        association=association,
        verifier=None,
    )


def _run() -> tuple[PersistedRunEvidence, dict[str, bytes]]:
    locator_a, locator_b = _locator("Source A"), _locator("Source B")
    evidence_a = _evidence("evidence-a", locator_a, 0)
    evidence_b = _evidence("evidence-b", locator_b, 1)
    association_a = _association("claim-a", evidence_a, "delivery-a")
    association_b = _association("claim-b", evidence_b, "delivery-b")
    receipt = PersistedAuditReceipt(
        run_audit_receipt_id="audit-receipt-1",
        run_id="run-1",
        run_event_id="event-terminal",
        attempt_id="attempt-1",
        decision_kind="terminal",
        step_id=None,
        tool_call_id=None,
        approval_id=None,
        audit_event_id="audit-event-1",
        audit_sequence=7,
        audit_record_hash="f" * 64,
        config_hash=H,
        created_at=NOW,
    )
    return (
        PersistedRunEvidence(
            run_id="run-1",
            owner_principal_id="owner-1",
            agent_id="agent-1",
            agent_version=3,
            status="completed",
            config_hash=H,
            capability_snapshot_hash="b" * 64,
            output_verdict="grounded",
            mapping_state="complete",
            protected_output_kind="protected_source_output",
            protected_output_ref="output-1",
            protected_output_hash=_hash(b"same same"),
            event_ids=frozenset({"event-evidence-0", "event-evidence-1", "event-terminal"}),
            delivery_receipt_ids=frozenset({"delivery-a", "delivery-b"}),
            # Reverse durable row order so order cannot masquerade as association.
            evidence=(evidence_b, evidence_a),
            claims=(
                _claim("claim-b", 1, 5, 9, association_b),
                _claim("claim-a", 0, 0, 4, association_a),
            ),
            audit_receipts=(receipt,),
        ),
        {"evidence-a": locator_a, "evidence-b": locator_b},
    )


def _projector(
    run: PersistedRunEvidence | None,
    locators: dict[str, bytes],
    *,
    visible: bool = True,
    denied: frozenset[str] = frozenset(),
) -> tuple[EvidenceProjector, _Reader, _Materializer]:
    reader = _Reader(run)
    materializer = _Materializer(
        locators={
            evidence_id: MaterializedDisclosure(
                "withheld" if evidence_id in denied else "authorized",
                None if evidence_id in denied else payload,
            )
            for evidence_id, payload in locators.items()
        },
        output=MaterializedDisclosure("authorized", b"same same"),
    )
    return (
        EvidenceProjector(
            visible_agent=lambda agent_id, principal: visible,
            reader=reader,
            materializer=materializer,
        ),
        reader,
        materializer,
    )


def _project(projector: EvidenceProjector):
    return asyncio.run(
        projector.project(
            agent_id="agent-1",
            run_id="run-1",
            principal=Principal(id="owner-1"),
            access_context_hash=H,
            projection_attempt_id="projection-1",
        )
    )


def test_identical_spans_follow_exact_association_ids_not_row_or_text_order() -> None:
    run, locators = _run()
    claim_b, claim_a = run.claims
    run = replace(run, claims=(replace(claim_b, span_start_byte=0, span_end_byte=4), claim_a))
    projector, _, _ = _projector(run, locators)

    result = _project(projector)

    assert [claim.claim_id for claim in result.claims] == ["claim-a", "claim-b"]
    assert [claim.display for claim in result.claims] == ["same", "same"]
    assert [claim.evidence_id for claim in result.claims] == ["evidence-a", "evidence-b"]
    assert [item.locator.title for item in result.evidence if item.locator] == [
        "Source A",
        "Source B",
    ]


def test_canonical_graph_locator_projects_exact_path_and_truthful_absent_review_state() -> None:
    run, locators = _run()
    locator = _graph_locator()
    evidence_b, evidence_a = run.evidence
    graph_evidence = replace(
        evidence_a,
        locator_hash=_hash(locator),
        assurance="inferred",
    )
    claim_b, claim_a = run.claims
    assert claim_a.association is not None
    graph_association = replace(claim_a.association, locator_hash=_hash(locator))
    run = replace(
        run,
        evidence=(evidence_b, graph_evidence),
        claims=(claim_b, replace(claim_a, association=graph_association)),
    )
    locators["evidence-a"] = locator
    projector, _, _ = _projector(run, locators)

    result = _project(projector)

    item = {candidate.evidence_id: candidate for candidate in result.evidence}["evidence-a"]
    assert item.state == "available"
    assert item.assurance == "inferred"
    assert item.locator is not None
    assert item.locator.entity_id is None
    assert len(item.locator.graph_path) == 1
    hop = item.locator.graph_path[0]
    assert hop.subject.entity_key == "acme"
    assert hop.edge_kind == "services"
    assert hop.obj.entity_key == "meridian"
    assert hop.source_system == "m365"
    assert hop.source_ref == "edge-document"
    assert hop.assurance == "extracted"
    assert hop.review_state is None
    assert '"review_state":null' in item.model_dump_json()


@pytest.mark.parametrize(
    ("locator_factory", "expected_kind", "has_row_anchor"),
    (
        (_verified_t3_locator, "verified_t3", True),
        (_verified_t3_empty_locator, "verified_t3_empty", False),
    ),
)
def test_canonical_verified_t3_locator_projects_only_content_free_receipt_metadata(
    locator_factory, expected_kind: str, has_row_anchor: bool
) -> None:
    run, locators = _run()
    locator = locator_factory()
    raw = json.loads(locator)
    evidence_b, evidence_a = run.evidence
    run = replace(
        run,
        evidence=(evidence_b, replace(evidence_a, locator_hash=_hash(locator))),
    )
    locators["evidence-a"] = locator
    projector, _, _ = _projector(run, locators)

    item = {candidate.evidence_id: candidate for candidate in _project(projector).evidence}[
        "evidence-a"
    ]

    assert item.state == "available"
    assert item.assurance == "direct"
    assert item.locator_hash == _hash(locator)
    assert item.locator is not None
    assert item.locator.kind == expected_kind
    assert item.locator.receipt_hash == raw["receipt"]["receipt_hash"]
    assert (item.locator.row_anchor is not None) is has_row_anchor
    if has_row_anchor:
        assert item.locator.row_anchor == raw["row_anchor"]
    disclosed = item.locator.model_dump(mode="json", exclude_none=True)
    # `graph_path`, `chunk_ids` and `served_document_versions` are tuple-typed
    # and serialize as empty lists rather than nulls; all must stay EMPTY on the
    # verified arms.
    base_fields = {"kind", "receipt_hash", "graph_path", "chunk_ids", "served_document_versions"}
    assert set(disclosed) == (base_fields | {"row_anchor"} if has_row_anchor else base_fields)
    assert disclosed["graph_path"] == []
    assert disclosed["chunk_ids"] == []
    assert disclosed["served_document_versions"] == []
    assert "receipt" not in disclosed
    assert "contributor" not in disclosed


def _remove(raw: dict[str, object], key: str) -> None:
    raw.pop(key)


def _mutate_receipt(raw: dict[str, object], key: str, value: object) -> None:
    receipt = raw["receipt"]
    assert isinstance(receipt, dict)
    receipt[key] = value


def _mutate_contributor_identity(raw: dict[str, object]) -> None:
    contributor = raw["contributor"]
    assert isinstance(contributor, dict)
    contributor["authority_receipt_id"] = "foreign-receipt"


@pytest.mark.parametrize(
    "mutate",
    (
        lambda raw: raw.update({"foreign": "field"}),
        lambda raw: _remove(raw, "receipt"),
        lambda raw: raw.update({"evidence_assurance": "inferred"}),
        lambda raw: raw.update({"row_anchor": "0" * 64}),
        lambda raw: raw.update({"contributor_index": True}),
        lambda raw: raw.update({"contributor_index": 1}),
        _mutate_contributor_identity,
        lambda raw: _mutate_receipt(raw, "receipt_hash", "0" * 64),
        lambda raw: _mutate_receipt(raw, "protected_source", "sentinel"),
    ),
    ids=(
        "foreign-field",
        "missing-receipt",
        "foreign-assurance",
        "wrong-row-anchor",
        "boolean-contributor-index",
        "out-of-range-contributor-index",
        "mismatched-contributor",
        "wrong-receipt-hash",
        "receipt-extra-field",
    ),
)
def test_malformed_verified_t3_locator_fails_closed_without_receipt_metadata(mutate) -> None:
    run, locators = _run()
    raw = json.loads(_verified_t3_locator())
    mutate(raw)
    locator = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    evidence_b, evidence_a = run.evidence
    run = replace(
        run,
        evidence=(evidence_b, replace(evidence_a, locator_hash=_hash(locator))),
    )
    locators["evidence-a"] = locator
    projector, _, _ = _projector(run, locators)

    item = {candidate.evidence_id: candidate for candidate in _project(projector).evidence}[
        "evidence-a"
    ]

    assert item.state == "locator_unavailable"
    assert item.locator is None
    assert item.locator_hash is None
    assert item.assurance == "unavailable"


@pytest.mark.parametrize(
    "mutate",
    (
        lambda raw: raw.update({"foreign": "field"}),
        lambda raw: _remove(raw, "receipt"),
        lambda raw: raw.update({"evidence_assurance": "inferred"}),
        lambda raw: raw.update({"row_anchor": "0" * 64}),
        lambda raw: _mutate_receipt(raw, "receipt_hash", "0" * 64),
        lambda raw: _mutate_receipt(raw, "canonical_args_hash", "0" * 64),
        lambda raw: _mutate_receipt(raw, "protected_source", "sentinel"),
    ),
    ids=(
        "foreign-field",
        "missing-receipt",
        "foreign-assurance",
        "invented-row-anchor",
        "wrong-receipt-hash",
        "receipt-preimage-substitution",
        "receipt-extra-field",
    ),
)
def test_malformed_verified_t3_empty_locator_fails_closed_without_receipt_metadata(
    mutate,
) -> None:
    run, locators = _run()
    raw = json.loads(_verified_t3_empty_locator())
    mutate(raw)
    locator = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    evidence_b, evidence_a = run.evidence
    run = replace(
        run,
        evidence=(evidence_b, replace(evidence_a, locator_hash=_hash(locator))),
    )
    locators["evidence-a"] = locator
    projector, _, _ = _projector(run, locators)

    item = {candidate.evidence_id: candidate for candidate in _project(projector).evidence}[
        "evidence-a"
    ]

    assert item.state == "locator_unavailable"
    assert item.locator is None
    assert item.locator_hash is None
    assert item.assurance == "unavailable"


@pytest.mark.parametrize("locator_factory", (_verified_t3_locator, _verified_t3_empty_locator))
def test_current_authority_denial_withholds_verified_t3_receipt_metadata(locator_factory) -> None:
    run, locators = _run()
    locator = locator_factory()
    evidence_b, evidence_a = run.evidence
    run = replace(
        run,
        evidence=(evidence_b, replace(evidence_a, locator_hash=_hash(locator))),
    )
    locators["evidence-a"] = locator
    projector, _, _ = _projector(run, locators, denied=frozenset({"evidence-a"}))

    item = {candidate.evidence_id: candidate for candidate in _project(projector).evidence}[
        "evidence-a"
    ]

    assert item.state == "withheld"
    assert item.locator is None
    assert item.locator_hash == _hash(locator)
    assert "receipt_hash" not in item.model_dump_json(exclude_none=True)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda raw: raw["path"].update({"hops": []}),
        lambda raw: raw.update({"hop_source_stamps": []}),
        lambda raw: raw.update({"hop_source_stamps": [{"foreign": "stamp"}]}),
        lambda raw: raw["hop_source_stamps"][0][0].update({"foreign": "stamp"}),
        lambda raw: raw.update({"evidence_assurance": "direct"}),
        lambda raw: raw.update({"unexpected": "field"}),
    ),
    ids=(
        "empty-path",
        "missing-hop-stamps",
        "malformed-stamp-group",
        "noncanonical-stamp",
        "foreign-assurance",
        "foreign-field",
    ),
)
def test_malformed_graph_locator_is_unavailable_not_an_empty_available_locator(mutate) -> None:
    run, locators = _run()
    raw = json.loads(_graph_locator())
    mutate(raw)
    locator = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    evidence_b, evidence_a = run.evidence
    run = replace(
        run,
        evidence=(
            evidence_b,
            replace(evidence_a, locator_hash=_hash(locator), assurance="inferred"),
        ),
    )
    locators["evidence-a"] = locator
    projector, _, _ = _projector(run, locators)

    item = {candidate.evidence_id: candidate for candidate in _project(projector).evidence}[
        "evidence-a"
    ]

    assert item.state == "locator_unavailable"
    assert item.locator is None
    assert item.locator_hash is None


def test_cross_run_evidence_mapping_is_unavailable_without_materializing_locator() -> None:
    run, locators = _run()
    evidence_b, evidence_a = run.evidence
    run = replace(run, evidence=(evidence_b, replace(evidence_a, run_id="run-foreign")))
    projector, _, materializer = _projector(run, locators)

    result = _project(projector)

    item = {candidate.evidence_id: candidate for candidate in result.evidence}["evidence-a"]
    claim = {candidate.claim_id: candidate for candidate in result.claims}["claim-a"]
    assert item.state == "locator_unavailable"
    assert item.locator is None
    assert claim.state == "unavailable"
    assert claim.evidence_id is None
    assert ("locator", "evidence-a") not in materializer.calls


def test_current_evidence_authority_denial_withholds_claim_and_locator_content() -> None:
    run, locators = _run()
    projector, _, _ = _projector(run, locators, denied=frozenset({"evidence-a"}))

    result = _project(projector)

    evidence = {item.evidence_id: item for item in result.evidence}
    claims = {item.claim_id: item for item in result.claims}
    assert evidence["evidence-a"].state == "withheld"
    assert evidence["evidence-a"].locator is None
    assert claims["claim-a"].state == "withheld"
    assert claims["claim-a"].display is None
    assert claims["claim-b"].display == "same"


def test_missing_exact_delivery_receipt_never_correlates_or_infers_a_claim_check() -> None:
    run, locators = _run()
    run = replace(run, delivery_receipt_ids=frozenset({"delivery-b"}))
    projector, _, _ = _projector(run, locators)

    result = _project(projector)

    first = {item.claim_id: item for item in result.claims}["claim-a"]
    assert first.state == "unavailable"
    # An association row without its exact persisted delivery receipt is NOT a
    # valid association, and the claim says so -- the same sentence as no row at
    # all, because an unproven binding must never read differently from an
    # absent one.
    assert first.message == "Claim not associated with evidence"
    assert first.display is None
    assert first.evidence_id is None


def test_absent_association_stays_unavailable_despite_identical_output_prose() -> None:
    run, locators = _run()
    orphan = _claim("claim-a", 0, 0, 4, None)
    run = replace(run, claims=(orphan, run.claims[0]), mapping_state="partial")
    projector, _, _ = _projector(run, locators)

    result = _project(projector)

    first = result.claims[0]
    assert first.state == "unavailable"
    assert first.message == "Claim not associated with evidence"
    assert first.display is None
    assert first.evidence_id is None
    assert result.mapping_state == "partial"


def test_hidden_agent_and_random_run_are_one_owner_safe_outcome() -> None:
    run, locators = _run()
    hidden, hidden_reader, _ = _projector(run, locators, visible=False)
    random, random_reader, _ = _projector(None, locators)

    with pytest.raises(EvidenceResourceNotFound) as hidden_error:
        _project(hidden)
    with pytest.raises(EvidenceResourceNotFound) as random_error:
        _project(random)

    assert str(hidden_error.value) == str(random_error.value) == "evidence resource not found"
    assert hidden_reader.calls == []
    assert random_reader.calls == [("agent-1", "run-1", "owner-1")]


def test_locator_hash_substitution_yields_a_content_free_shell() -> None:
    run, locators = _run()
    corrupt = dict(locators)
    corrupt["evidence-a"] = _locator("substituted current title")
    projector, _, _ = _projector(run, corrupt)

    result = _project(projector)

    item = {item.evidence_id: item for item in result.evidence}["evidence-a"]
    assert item.state == "locator_unavailable"
    assert item.locator is None
    assert item.locator_hash is None
    assert "substituted" not in result.model_dump_json()


class _ProjectionStore:
    def __init__(self, *, locator: bytes, output: bytes) -> None:
        stamp = _stamp()
        self.locator = StoredSourcePayload(locator, (stamp,), _hash(locator))
        self.output = StoredSourcePayload(output, (stamp,), _hash(output))
        self.calls: list[tuple[str, str, str]] = []

    async def load_evidence_projection_locator(self, **kwargs: object) -> StoredSourcePayload:
        self.calls.append(("locator", str(kwargs["run_id"]), str(kwargs["locator_ref"])))
        return self.locator

    async def load_evidence_projection_output(self, **kwargs: object) -> StoredSourcePayload:
        self.calls.append(("output", str(kwargs["run_id"]), str(kwargs["protected_ref"])))
        return self.output

    async def load_evidence_projection_claim_slice(self, **kwargs: object) -> StoredSourcePayload:
        start = cast(int, kwargs["span_start_byte"])
        end = cast(int, kwargs["span_end_byte"])
        payload = self.output.plaintext[start:end]
        self.calls.append(("claim", str(kwargs["run_id"]), str(kwargs["protected_ref"])))
        return StoredSourcePayload(payload, self.output.stamps, _hash(payload))


def test_run_store_materializer_reauthorizes_every_disclosure_in_exact_request_scope() -> None:
    run, locators = _run()
    locator = locators["evidence-a"]
    store = _ProjectionStore(locator=locator, output=b"same same")
    decisions: list[SourceAuthorityDecision] = []

    async def authorize(
        stamp: SourceAuthorityStamp, context: AuthenticatedRequestContext
    ) -> SourceAuthorityDecision:
        del stamp, context
        return decisions[-1]

    materializer = RunStoreCurrentEvidenceMaterializer(
        cast(AgentRunStore, cast(object, store)), authorize
    )
    context = AuthenticatedRequestContext(
        principal_id="owner-1",
        authorization_groups=(),
        issuer="https://issuer.example.test",
        expires_at=4_102_444_800,
    )

    async def scenario() -> None:
        missing = await materializer.resolve_locator(
            run.evidence[1],
            principal_id="owner-1",
            access_context_hash=context.context_hash,
            projection_attempt_id="projection-missing",
        )
        assert missing == MaterializedDisclosure("unknown")

        decisions.append(SourceAuthorityDecision.AUTHORIZED)
        with authenticated_request_scope(context):
            located = await materializer.resolve_locator(
                run.evidence[1],
                principal_id="owner-1",
                access_context_hash=context.context_hash,
                projection_attempt_id="projection-locator",
            )
            output = await materializer.resolve_output(
                run,
                principal_id="owner-1",
                access_context_hash=context.context_hash,
                projection_attempt_id="projection-output",
            )
            claim = await materializer.resolve_claim(
                run,
                run.claims[1],
                principal_id="owner-1",
                access_context_hash=context.context_hash,
                projection_attempt_id="projection-claim",
            )
        assert located == MaterializedDisclosure("authorized", locator)
        assert output == MaterializedDisclosure("authorized", b"same same")
        assert claim == MaterializedDisclosure("authorized", b"same")

        decisions.append(SourceAuthorityDecision.PERMANENT_WITHHELD)
        with authenticated_request_scope(context):
            withheld = await materializer.resolve_output(
                run,
                principal_id="owner-1",
                access_context_hash=context.context_hash,
                projection_attempt_id="projection-withheld",
            )
            withheld_locator = await materializer.resolve_locator(
                run.evidence[1],
                principal_id="owner-1",
                access_context_hash=context.context_hash,
                projection_attempt_id="projection-locator-withheld",
            )
        assert withheld == MaterializedDisclosure("withheld")
        assert withheld_locator == MaterializedDisclosure("withheld")

    asyncio.run(scenario())
    assert [kind for kind, _, _ in store.calls] == [
        "locator",
        "locator",
        "output",
        "claim",
        "output",
        "locator",
    ]


def test_sql_reader_filters_owner_and_agent_in_the_first_run_query() -> None:
    engine = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    sessions = SqlAlchemySessionStore(engine)
    runs = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="evidence-k1", keys={"evidence-k1": b"k" * 32})
        ),
    )

    async def scenario() -> None:
        conversation = await sessions.create_conversation("agent-1", "finance", "owner-1")
        created = await runs.create_run(
            conversation_id=conversation.conversation_id,
            owner_principal_id="owner-1",
            department="finance",
            conversation_agent_name="agent-1",
            agent_id="agent-1",
            agent_version=1,
            strategy="agentic",
            config_hash=H,
            capability_snapshot_hash="b" * 64,
            capability_snapshot_receipt_id="capability-receipt-1",
            input_plaintext=b"private-input-sentinel",
        )
        reader = SqlAlchemyEvidenceProjectionReader(engine)
        owned = await reader.load_exact(
            agent_id="agent-1", run_id=created.run_id, owner_principal_id="owner-1"
        )
        assert owned is not None
        assert owned.run_id == created.run_id
        assert owned.mapping_state == "unavailable"
        assert (
            await reader.load_exact(
                agent_id="agent-1", run_id=created.run_id, owner_principal_id="owner-foreign"
            )
            is None
        )
        assert (
            await reader.load_exact(
                agent_id="agent-hidden", run_id=created.run_id, owner_principal_id="owner-1"
            )
            is None
        )

    asyncio.run(scenario())
    engine.dispose()


# ---------------------------------------------------------------------------
# THE SEARCH-READ LOCATOR ARM, and one message per claim-failure cause.
#
# Measured live on run aaea4860 (and every verified run in the demo store):
# both claims were verified, associated, verifier-receipted, hash- and
# span-consistent -- every SQL-visible gate passed -- and still rendered
#     state=unavailable  message="Claim mapping unavailable"
# because the ONE evidence record they anchored to was a knowledge SEARCH read,
# whose persisted locator `{"kind": "knowledge_search", "contributors": [...],
# "chunk_ids": [...]}` had no parser arm: the generic fallthrough extracted
# nothing, `EvidenceLocatorView` refused the empty view, the record projected
# `locator_unavailable`, and the claim collapsed into the conflated default.
# These tests pin the arm and the per-cause sentences that replace the default.


def _search_locator(chunks: tuple[str, ...] = ("chunk-1", "chunk-2")) -> bytes:
    return json.dumps(
        {
            "kind": "knowledge_search",
            "contributors": [_stamp().model_dump(mode="json")],
            "chunk_ids": list(chunks),
            "evidence_assurance": "direct",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _verifier(claim: PersistedClaim) -> PersistedVerifier:
    return PersistedVerifier(
        run_id="run-1",
        claim_key=claim.claim_key,
        claim_content_hash=claim.claim_content_hash,
        served_output_hash=claim.served_output_hash,
        span_start_byte=claim.span_start_byte,
        span_end_byte=claim.span_end_byte,
        event_id="event-verifier",
        receipt_hash="9" * 64,
    )


def _verified_search_run() -> tuple[PersistedRunEvidence, dict[str, bytes]]:
    """One VERIFIED claim anchored to one search-read evidence record."""

    run, _ = _run()
    locator = _search_locator()
    evidence_a = _evidence("evidence-a", locator, 0)
    association_a = _association("claim-a", evidence_a, "delivery-a")
    base = _claim("claim-a", 0, 0, 4, association_a)
    claim_a = replace(base, claim_verdict="verified", verifier=_verifier(base))
    run = replace(
        run,
        output_verdict="verified",
        event_ids=run.event_ids | {"event-verifier"},
        evidence=(evidence_a,),
        claims=(claim_a,),
    )
    return run, {"evidence-a": locator}


def test_search_read_locator_projects_chunk_set_and_verified_claim_renders() -> None:
    run, locators = _verified_search_run()
    projector, _, _ = _projector(run, locators)

    result = _project(projector)

    item = result.evidence[0]
    assert item.state == "available"
    assert item.locator is not None
    assert item.locator.kind == "knowledge_search"
    assert item.locator.chunk_ids == ("chunk-1", "chunk-2")
    # The contributor stamps are validated but NEVER projected (C12).
    disclosed = item.locator.model_dump(mode="json", exclude_none=True)
    assert set(disclosed) == {"kind", "chunk_ids", "graph_path", "served_document_versions"}
    # No resolver was composed in this projector; absence projects as absence.
    assert disclosed["served_document_versions"] == []
    claim = result.claims[0]
    assert claim.claim_verdict == "verified"
    assert claim.state == "available"
    assert claim.message == "Claim available"
    assert claim.display == "same"
    assert claim.evidence_id == "evidence-a"
    assert claim.verifier_event_id == "event-verifier"


def test_search_read_locator_projects_stamp_bound_document_versions() -> None:
    """The composed resolver binds each contributor stamp to its exact version.

    The projection asks the seam with the retained artifact identities — NOT the
    receipt ids, which stop resolving the moment the same document version is
    re-admitted — and renders ONLY the resolved (document, version, hash, name)
    views: the binding a later corpus supersession cannot re-attribute.  A
    resolver that fails, or resolves nothing, degrades to the honest empty tuple
    without disturbing the chunk-set projection.
    """

    run, locators = _verified_search_run()
    reader = _Reader(run)
    materializer = _Materializer(
        locators={
            evidence_id: MaterializedDisclosure("authorized", payload)
            for evidence_id, payload in locators.items()
        },
        output=MaterializedDisclosure("authorized", b"same same"),
    )
    asked: list[tuple[ServedVersionKey, ...]] = []

    async def resolver(
        keys: tuple[ServedVersionKey, ...],
    ) -> tuple[ServedDocumentVersionView, ...]:
        asked.append(keys)
        assert keys == (ServedVersionKey("governed_local_t3", "2" * 64, "4" * 64),)
        return (
            ServedDocumentVersionView(
                document_id="doc-1",
                version_seq=2,
                content_hash="a" * 64,
                display_name="Debit Card Dispute Handling SOP.docx",
            ),
        )

    projector = EvidenceProjector(
        visible_agent=lambda agent_id, principal: True,
        reader=reader,
        materializer=materializer,
        served_version_resolver=resolver,
    )

    result = _project(projector)

    item = result.evidence[0]
    assert item.state == "available"
    assert item.locator is not None
    assert item.locator.chunk_ids == ("chunk-1", "chunk-2")
    assert item.locator.served_document_versions == (
        ServedDocumentVersionView(
            document_id="doc-1",
            version_seq=2,
            content_hash="a" * 64,
            display_name="Debit Card Dispute Handling SOP.docx",
        ),
    )
    assert asked == [(ServedVersionKey("governed_local_t3", "2" * 64, "4" * 64),)]

    # A failing resolver degrades to absence, never to a failed projection.
    async def broken(
        keys: tuple[ServedVersionKey, ...],
    ) -> tuple[ServedDocumentVersionView, ...]:
        raise RuntimeError("lineage unavailable")

    degraded = EvidenceProjector(
        visible_agent=lambda agent_id, principal: True,
        reader=_Reader(run),
        materializer=materializer,
        served_version_resolver=broken,
    )
    fallback = _project(degraded)
    assert fallback.evidence[0].state == "available"
    assert fallback.evidence[0].locator is not None
    assert fallback.evidence[0].locator.served_document_versions == ()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.pop("chunk_ids"),
        lambda raw: raw.update(chunk_ids=[]),
        lambda raw: raw.update(chunk_ids=[""]),
        lambda raw: raw.update(chunk_ids="chunk-1"),
        lambda raw: raw.update(contributors=[]),
        lambda raw: raw.update(contributors=[{"forged": "stamp"}]),
        lambda raw: raw.update(evidence_assurance="inferred"),
        lambda raw: raw.update(extra_field="x"),
    ],
)
def test_malformed_search_locator_fails_closed_and_names_the_locator_cause(mutate) -> None:
    run, _ = _verified_search_run()
    raw = json.loads(_search_locator())
    mutate(raw)
    broken = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    association = run.claims[0].association
    assert association is not None
    run = replace(
        run,
        evidence=(replace(run.evidence[0], locator_hash=_hash(broken)),),
        claims=(
            replace(
                run.claims[0],
                association=replace(association, locator_hash=_hash(broken)),
            ),
        ),
    )
    projector, _, _ = _projector(run, {"evidence-a": broken})

    result = _project(projector)

    assert result.evidence[0].state == "locator_unavailable"
    assert result.evidence[0].locator is None
    claim = result.claims[0]
    assert claim.state == "unavailable"
    assert claim.message == "Claim evidence locator unresolved"
    assert claim.display is None


def test_verified_claim_without_verifier_receipt_names_that_cause() -> None:
    run, locators = _verified_search_run()
    run = replace(run, claims=(replace(run.claims[0], verifier=None),))
    projector, _, materializer = _projector(run, locators)

    result = _project(projector)

    claim = result.claims[0]
    assert claim.state == "unavailable"
    assert claim.message == "Claim verifier receipt unavailable"
    assert claim.display is None
    assert claim.verifier_event_id is None
    # The materializer is never asked about a claim that cannot display.
    assert ("claim", "claim-a") not in materializer.calls


def test_undisclosed_answer_names_the_answer_not_the_claim_mapping() -> None:
    run, locators = _verified_search_run()
    reader = _Reader(run)
    materializer = _Materializer(
        locators={"evidence-a": MaterializedDisclosure("authorized", locators["evidence-a"])},
        output=MaterializedDisclosure("unknown"),
    )
    projector = EvidenceProjector(
        visible_agent=lambda agent_id, principal: True, reader=reader, materializer=materializer
    )

    result = _project(projector)

    assert result.output.state == "unavailable"
    claim = result.claims[0]
    assert claim.state == "unavailable"
    assert claim.message == "Claim unavailable — answer not disclosed"
    assert claim.display is None


class _ClaimDisclosureMaterializer(_Materializer):
    """The shared fake, with the per-claim resolution pinned by the test."""

    def __init__(self, *, claim_result: MaterializedDisclosure, **kwargs) -> None:
        super().__init__(**kwargs)
        self.claim_result = claim_result

    async def resolve_claim(self, run: PersistedRunEvidence, claim: PersistedClaim, **kwargs):
        self.calls.append(("claim", claim.claim_id))
        return self.claim_result


@pytest.mark.parametrize(
    ("claim_result", "state", "message"),
    [
        # Payload hashing to something other than claim_content_hash.
        (
            MaterializedDisclosure("authorized", b"forged"),
            "unavailable",
            "Claim content hash mismatch",
        ),
        # Payload hashes correctly but is not the served output's span bytes:
        # claim-a's span is [0, 4) of "same same" and the hash is over "same",
        # so a shifted span cannot happen here without the bytes differing --
        # simulate with the correct hash preimage placed at the wrong offset by
        # widening the claim span below.
        (
            MaterializedDisclosure("withheld"),
            "withheld",
            "Claim withheld — access changed",
        ),
        (
            MaterializedDisclosure("unknown"),
            "unavailable",
            "Claim mapping unavailable",
        ),
    ],
)
def test_claim_materializer_outcomes_each_name_their_own_cause(
    claim_result, state, message
) -> None:
    run, locators = _verified_search_run()
    reader = _Reader(run)
    materializer = _ClaimDisclosureMaterializer(
        claim_result=claim_result,
        locators={"evidence-a": MaterializedDisclosure("authorized", locators["evidence-a"])},
        output=MaterializedDisclosure("authorized", b"same same"),
    )
    projector = EvidenceProjector(
        visible_agent=lambda agent_id, principal: True, reader=reader, materializer=materializer
    )

    result = _project(projector)

    claim = result.claims[0]
    assert claim.state == state
    assert claim.message == message
    assert claim.display is None


def test_authorized_payload_at_the_wrong_span_is_a_span_mismatch() -> None:
    run, locators = _verified_search_run()
    # The materializer discloses bytes whose hash matches claim_content_hash
    # ("same"), but the claim's span points at output bytes [5, 9) ("same" of
    # the SECOND word -- byte-identical) shifted to [4, 8) (" sam"): the
    # equality against the served output's exact span must fail.
    shifted = replace(run.claims[0], span_start_byte=4, span_end_byte=8)
    shifted = replace(shifted, verifier=_verifier(shifted))
    run = replace(run, claims=(shifted,))
    reader = _Reader(run)
    materializer = _ClaimDisclosureMaterializer(
        claim_result=MaterializedDisclosure("authorized", b"same"),
        locators={"evidence-a": MaterializedDisclosure("authorized", locators["evidence-a"])},
        output=MaterializedDisclosure("authorized", b"same same"),
    )
    projector = EvidenceProjector(
        visible_agent=lambda agent_id, principal: True, reader=reader, materializer=materializer
    )

    result = _project(projector)

    claim = result.claims[0]
    assert claim.state == "unavailable"
    assert claim.message == "Claim span mismatch"
    assert claim.display is None


# ---------------------------------------------------------------------------
# THE GRAPH AND VERIFIED-ROW VERSION STAMPS (POST_DEMO #13).
#
# Before this, `served_document_versions` was projected ONLY for a
# knowledge_search locator. Measured on the live box across the whole train-63
# deploy: 62 knowledge.graph_read, 21 knowledge.verified_read, ZERO
# knowledge.search -- so in practice no served answer carried a version at all.
# Both of those locators DO retain full contributor stamps (a graph locator one
# group per hop, a verified-row locator the row receipt's own contributors), so
# both resolve the same way the search arm does.


def _projector_with_resolver(
    run: PersistedRunEvidence,
    locators: dict[str, bytes],
    resolver: ServedVersionResolver,
) -> EvidenceProjector:
    return EvidenceProjector(
        visible_agent=lambda agent_id, principal: True,
        reader=_Reader(run),
        materializer=_Materializer(
            locators={
                evidence_id: MaterializedDisclosure("authorized", payload)
                for evidence_id, payload in locators.items()
            },
            output=MaterializedDisclosure("authorized", b"same same"),
        ),
        served_version_resolver=resolver,
    )


def _single_evidence_run(
    locator: bytes, assurance: Literal["direct", "inferred"]
) -> PersistedRunEvidence:
    run, _ = _run()
    evidence = replace(_evidence("evidence-a", locator, 0), assurance=assurance)
    return replace(run, evidence=(evidence,), claims=(), mapping_state="unavailable")


_KEY = ServedVersionKey("governed_local_t3", "2" * 64, "4" * 64)
_VERSION = ServedDocumentVersionView(
    document_id="doc-1",
    version_seq=3,
    content_hash="c" * 64,
    display_name="Consumer Auto Collections Policy.md",
)


def test_graph_path_locator_projects_hop_stamp_bound_document_versions() -> None:
    """A graph hop's retained stamps name the versions the hop rests on."""

    locator = _graph_locator()
    run = _single_evidence_run(locator, "inferred")
    asked: list[tuple[ServedVersionKey, ...]] = []

    async def resolver(
        keys: tuple[ServedVersionKey, ...],
    ) -> tuple[ServedDocumentVersionView, ...]:
        asked.append(keys)
        return (_VERSION,)

    item = _project(_projector_with_resolver(run, {"evidence-a": locator}, resolver)).evidence[0]

    assert item.state == "available"
    assert item.locator is not None
    # The hop projection is untouched: the version stamp is additive.
    assert len(item.locator.graph_path) == 1
    assert item.locator.served_document_versions == (_VERSION,)
    assert asked == [(_KEY,)]
    # No stamp material crosses the wire -- only the resolved version views.
    disclosed = item.locator.model_dump(mode="json", exclude_none=True)
    assert "hop_source_stamps" not in disclosed
    assert set(disclosed["served_document_versions"][0]) == {
        "document_id",
        "version_seq",
        "content_hash",
        "display_name",
    }


def test_verified_row_locator_projects_receipt_contributor_document_versions() -> None:
    """A verified row's own contributor stamps resolve the same way."""

    locator = _verified_t3_locator()
    run = _single_evidence_run(locator, "direct")

    async def resolver(
        keys: tuple[ServedVersionKey, ...],
    ) -> tuple[ServedDocumentVersionView, ...]:
        assert keys == (_KEY,)
        return (_VERSION,)

    item = _project(_projector_with_resolver(run, {"evidence-a": locator}, resolver)).evidence[0]

    assert item.state == "available"
    assert item.locator is not None
    assert item.locator.kind == "verified_t3"
    assert item.locator.row_anchor is not None
    assert item.locator.served_document_versions == (_VERSION,)


def test_verified_empty_locator_never_carries_a_version() -> None:
    """A zero-row read served no document, so it must name no version."""

    locator = _verified_t3_empty_locator()
    run = _single_evidence_run(locator, "direct")
    asked: list[tuple[ServedVersionKey, ...]] = []

    async def resolver(
        keys: tuple[ServedVersionKey, ...],
    ) -> tuple[ServedDocumentVersionView, ...]:
        asked.append(keys)
        return (_VERSION,)

    item = _project(_projector_with_resolver(run, {"evidence-a": locator}, resolver)).evidence[0]

    assert item.state == "available"
    assert item.locator is not None
    assert item.locator.kind == "verified_t3_empty"
    assert item.locator.served_document_versions == ()
    assert asked == []


def test_locator_artifact_keys_reads_every_source_bearing_arm() -> None:
    """The one extractor, and the exact reason a bad payload resolves nothing."""

    assert _locator_artifact_keys(_search_locator()) == (_KEY,)
    assert _locator_artifact_keys(_graph_locator()) == (_KEY,)
    assert _locator_artifact_keys(_verified_t3_locator()) == (_KEY,)
    assert _locator_artifact_keys(_verified_t3_empty_locator()) == ()
    assert _locator_artifact_keys(b"not json") == ()
    assert _locator_artifact_keys(b'{"kind": "knowledge_search"}') == ()
    # A stamp missing its artifact identity resolves NOTHING rather than part of
    # the set: a partial version list would understate what the read rested on.
    raw = json.loads(_graph_locator())
    raw["hop_source_stamps"][0][0].pop("artifact_identity")
    assert (
        _locator_artifact_keys(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode())
        == ()
    )
    # Duplicate stamps across hops collapse to one key.
    raw = json.loads(_graph_locator())
    raw["hop_source_stamps"] = [raw["hop_source_stamps"][0], raw["hop_source_stamps"][0]]
    assert _locator_artifact_keys(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ) == (_KEY,)
