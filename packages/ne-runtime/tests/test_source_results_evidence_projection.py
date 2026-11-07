# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused proof for the sixth protected-result resolver target."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest

import ne.runtime.source_results as source_results_module
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    ProtectedClaimSliceRef,
    ProtectedSourceOutputRef,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceBoundEvidenceProjectionResolver,
    SourceBoundModelSendResolution,
    SourceBoundToolResultRef,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.runtime.run_store import AgentRunStore, StoredSourcePayload
from ne.runtime.source_results import (
    EvidenceProjectionPayloadReader,
    MalformedProtectedReferenceError,
    ProtectedSourceResultGuard,
    RunStoreSourceBoundResultResolver,
    UnavailableSourceBoundResultResolver,
    authenticated_request_scope,
    current_protected_tool_evidence_refs,
    encode_protected_reference,
    model_call_scope,
    protected_tool_evidence_scope,
    run_delivery_observation_scope,
)


def _stamp() -> SourceAuthorityStamp:
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant-a",
            site_id="",
            drive_id="drive-a",
            item_id="item-a",
        ),
        generation=3,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint",
        descriptor_schema_version=1,
        descriptor_hash="a" * 64,
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    receipt = CurrentSourceAuthorityReceipt(
        receipt_id="receipt-a",
        source_identity=source,
        artifact_identity=artifact,
        source_revision="etag-a",
        source_contract=contract,
        contract_epoch=4,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 21, 11, 59, tzinfo=UTC),
        audit_event_id="audit-a",
        lineage_id="lineage-a",
        freshness_sla_seconds=Decimal("900"),
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )


def _context() -> AuthenticatedRequestContext:
    return AuthenticatedRequestContext(
        principal_id="principal-a",
        authorization_groups=("analysts",),
        issuer="https://issuer.example",
        expires_at=4_102_444_800,
    )


class _ExactProjectionReader:
    def __init__(
        self,
        *,
        expected_reference: ProtectedSourceOutputRef | ProtectedClaimSliceRef,
        payload: bytes,
    ) -> None:
        self.expected_reference = expected_reference
        self.payload = payload
        self.calls: list[tuple[ProtectedSourceOutputRef | ProtectedClaimSliceRef, str, str]] = []

    async def load_for_evidence_projection(
        self,
        reference: ProtectedSourceOutputRef | ProtectedClaimSliceRef,
        *,
        owner_principal_id: str,
        run_id: str,
    ) -> StoredSourcePayload:
        self.calls.append((reference, owner_principal_id, run_id))
        if (
            type(reference) is not type(self.expected_reference)
            or reference.reference_id != self.expected_reference.reference_id
            or owner_principal_id != "principal-a"
            or run_id != "run-a"
        ):
            raise LookupError("projection payload unavailable")
        return StoredSourcePayload(
            plaintext=self.payload,
            plaintext_hash=hashlib.sha256(self.payload).hexdigest(),
            stamps=self.expected_reference.stamps,
        )


def _resolver(
    reader: EvidenceProjectionPayloadReader | None,
    *,
    decision: SourceAuthorityDecision = SourceAuthorityDecision.AUTHORIZED,
) -> tuple[RunStoreSourceBoundResultResolver, list[AuthenticatedRequestContext]]:
    evaluated: list[AuthenticatedRequestContext] = []

    async def evaluate(
        stamp: SourceAuthorityStamp, context: AuthenticatedRequestContext
    ) -> SourceAuthorityDecision:
        assert stamp == _stamp()
        evaluated.append(context)
        return decision

    resolver = RunStoreSourceBoundResultResolver(
        cast(AgentRunStore, object()),
        evaluate,
        evidence_projection_reader=reader,
    )
    return resolver, evaluated


def test_evidence_projection_binds_exact_reference_owner_run_and_read_attempt() -> None:
    payload = b"current protected answer"
    reference = ProtectedSourceOutputRef(
        reference_id="output-a",
        output_hash=hashlib.sha256(payload).hexdigest(),
        stamps=(_stamp(),),
    )
    reader = _ExactProjectionReader(expected_reference=reference, payload=payload)
    resolver, evaluated = _resolver(reader)
    context = _context()

    async def scenario() -> None:
        with authenticated_request_scope(context):
            result = await resolver.for_evidence_projection(
                reference,
                principal_id=context.principal_id,
                access_context_hash=context.context_hash,
                run_id="run-a",
                evidence_projection_attempt_id="read-a",
            )
        assert result.decision is SourceAuthorityDecision.AUTHORIZED
        assert result.materialized_text == payload.decode()
        assert result.stamps == reference.stamps
        assert result.run_id == "run-a"
        assert result.evidence_projection_attempt_id == "read-a"

    asyncio.run(scenario())
    assert reader.calls == [(reference, "principal-a", "run-a")]
    assert evaluated == [context]
    assert isinstance(resolver, SourceBoundEvidenceProjectionResolver)


def test_evidence_projection_reauthorizes_claim_slice_and_refuses_hash_or_authority_drift() -> None:
    payload = b"exact claim"
    reference = ProtectedClaimSliceRef(
        reference_id="claim-a",
        slice_hash=hashlib.sha256(payload).hexdigest(),
        stamps=(_stamp(),),
    )
    reader = _ExactProjectionReader(expected_reference=reference, payload=payload)
    context = _context()

    async def resolve(
        resolver: RunStoreSourceBoundResultResolver,
        candidate: ProtectedClaimSliceRef,
    ):
        with authenticated_request_scope(context):
            return await resolver.for_evidence_projection(
                candidate,
                principal_id=context.principal_id,
                access_context_hash=context.context_hash,
                run_id="run-a",
                evidence_projection_attempt_id="read-claim-a",
            )

    authorized, evaluated = _resolver(reader)
    allowed = asyncio.run(resolve(authorized, reference))
    assert allowed.decision is SourceAuthorityDecision.AUTHORIZED
    assert allowed.materialized_text == "exact claim"
    assert evaluated == [context]

    wrong_hash = reference.model_copy(update={"slice_hash": "f" * 64})
    malformed = asyncio.run(resolve(authorized, wrong_hash))
    assert malformed.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN
    assert malformed.materialized_text is None
    assert malformed.stamps == ()

    withheld, withheld_evaluations = _resolver(
        reader, decision=SourceAuthorityDecision.PERMANENT_WITHHELD
    )
    denied = asyncio.run(resolve(withheld, reference))
    assert denied.decision is SourceAuthorityDecision.PERMANENT_WITHHELD
    assert denied.materialized_text is None
    assert denied.stamps == ()
    assert withheld_evaluations == [context]


def test_evidence_projection_missing_foreign_or_unbound_state_is_one_unknown_shape() -> None:
    payload = b"protected"
    reference = ProtectedSourceOutputRef(
        reference_id="output-a",
        output_hash=hashlib.sha256(payload).hexdigest(),
        stamps=(_stamp(),),
    )
    reader = _ExactProjectionReader(expected_reference=reference, payload=payload)
    resolver, evaluated = _resolver(reader)
    context = _context()

    async def scenario():
        missing_context = await resolver.for_evidence_projection(
            reference,
            principal_id=context.principal_id,
            access_context_hash=context.context_hash,
            run_id="run-a",
            evidence_projection_attempt_id="read-a",
        )
        with authenticated_request_scope(context):
            foreign_target = await resolver.for_evidence_projection(
                reference,
                principal_id=context.principal_id,
                access_context_hash=context.context_hash,
                run_id="run-b",
                evidence_projection_attempt_id="read-a",
            )
        unavailable, _ = _resolver(None)
        with authenticated_request_scope(context):
            no_reader = await unavailable.for_evidence_projection(
                reference,
                principal_id=context.principal_id,
                access_context_hash=context.context_hash,
                run_id="run-a",
                evidence_projection_attempt_id="read-a",
            )
        return missing_context, foreign_target, no_reader

    results = asyncio.run(scenario())
    for result in results:
        assert result.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN
        assert result.materialized_text is None
        assert result.stamps == ()
        assert result.run_id in {"run-a", "run-b"}
        assert result.evidence_projection_attempt_id == "read-a"
    assert evaluated == []


def test_unavailable_resolver_implements_sixth_target_without_disclosure() -> None:
    reference = ProtectedSourceOutputRef(
        reference_id="output-a",
        output_hash="e" * 64,
        stamps=(_stamp(),),
    )
    resolver = UnavailableSourceBoundResultResolver()
    result = asyncio.run(
        resolver.for_evidence_projection(
            reference,
            principal_id="principal-a",
            access_context_hash="f" * 64,
            run_id="run-a",
            evidence_projection_attempt_id="read-a",
        )
    )
    assert result.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN
    assert result.materialized_text is None
    assert result.stamps == ()
    assert isinstance(resolver, SourceBoundEvidenceProjectionResolver)


def test_protected_tool_evidence_carrier_accepts_only_exact_stamped_tool_refs_and_resets() -> None:
    tool_ref = encode_protected_reference(
        SourceBoundToolResultRef(
            reference_id="tool-result-a",
            result_hash="e" * 64,
            stamps=(_stamp(),),
        )
    )
    output_ref = encode_protected_reference(
        ProtectedSourceOutputRef(
            reference_id="output-a",
            output_hash="f" * 64,
            stamps=(_stamp(),),
        )
    )

    assert current_protected_tool_evidence_refs() == ()
    with protected_tool_evidence_scope((tool_ref,)) as outer:
        assert outer == (tool_ref,)
        assert current_protected_tool_evidence_refs() == (tool_ref,)
        with (
            pytest.raises(MalformedProtectedReferenceError, match="Tool-result references"),
            protected_tool_evidence_scope((output_ref,)),
        ):
            pass
        assert current_protected_tool_evidence_refs() == (tool_ref,)
    assert current_protected_tool_evidence_refs() == ()


def test_effect_reauthorization_preserves_three_values_and_mints_no_delivery_receipt() -> None:
    reference = SourceBoundToolResultRef(
        reference_id="tool-result-a",
        result_hash="e" * 64,
        stamps=(_stamp(),),
    )

    class _Resolver:
        def __init__(self) -> None:
            self.decisions = [
                SourceAuthorityDecision.AUTHORIZED,
                SourceAuthorityDecision.PERMANENT_WITHHELD,
                SourceAuthorityDecision.RETRYABLE_UNKNOWN,
            ]

        async def for_model_send(self, candidate: Any, **kwargs: Any):
            assert candidate == reference
            assert source_results_module._current_delivery_observer() is None
            decision = self.decisions.pop(0)
            return SourceBoundModelSendResolution(
                decision=decision,
                materialized_text=(
                    "protected" if decision is SourceAuthorityDecision.AUTHORIZED else None
                ),
                stamps=reference.stamps if decision is SourceAuthorityDecision.AUTHORIZED else (),
                run_id=kwargs["run_id"],
                model_call_attempt_id=kwargs["model_call_attempt_id"],
            )

    resolver = _Resolver()
    guard = ProtectedSourceResultGuard(resolver)  # type: ignore[arg-type]
    context = _context()

    async def scenario() -> tuple[SourceAuthorityDecision, ...]:
        with (
            authenticated_request_scope(context),
            model_call_scope(run_id="run-a", model_call_attempt_id="attempt-a"),
            run_delivery_observation_scope(run_id="run-a", attempt_id="attempt-a") as observer,
        ):
            decisions = tuple([await guard.reauthorize_for_effect((reference,)) for _ in range(3)])
            assert observer.snapshot().delivery_receipt_ids == ()
            return decisions

    assert asyncio.run(scenario()) == (
        SourceAuthorityDecision.AUTHORIZED,
        SourceAuthorityDecision.PERMANENT_WITHHELD,
        SourceAuthorityDecision.RETRYABLE_UNKNOWN,
    )


def test_effect_reauthorization_withholds_on_any_union_member_not_just_the_first() -> None:
    """U3/F3: revoking authority on a NON-FIRST union member still withholds the effect.

    A Tier-2 effect that followed a multi-read ladder rests on the UNION of every
    admitted reference's stamps, and ``reauthorize_for_effect`` re-checks all of
    them immediately before the effect. The guarantee held, but nothing pinned
    it: every existing test passed a ONE-element tuple, so an edit narrowing the
    aggregate to ``references[0]`` — the shape this code replaced — would have
    kept CI green while silently dropping reads 2..N from the re-check. An
    effect could then land on evidence whose authority had been revoked.

    This walks the revocation across EVERY position of a four-read ladder, in
    both non-authorized values, so no single-position implementation survives.
    """

    references = tuple(
        SourceBoundToolResultRef(
            reference_id=f"tool-result-{name}",
            result_hash=hashlib.sha256(name.encode()).hexdigest(),
            stamps=(_stamp(),),
        )
        for name in ("graph-path", "loan-row", "servicing-note", "disaster-decl")
    )

    class _Resolver:
        """Authorizes every reference except ``revoked``, which gets ``decision``."""

        def __init__(self, revoked: str, decision: SourceAuthorityDecision) -> None:
            self.revoked = revoked
            self.decision = decision
            self.seen: list[str] = []

        async def for_model_send(self, candidate: Any, **kwargs: Any):
            self.seen.append(candidate.reference_id)
            decision = (
                self.decision
                if candidate.reference_id == self.revoked
                else SourceAuthorityDecision.AUTHORIZED
            )
            authorized = decision is SourceAuthorityDecision.AUTHORIZED
            return SourceBoundModelSendResolution(
                decision=decision,
                materialized_text="protected" if authorized else None,
                stamps=candidate.stamps if authorized else (),
                run_id=kwargs["run_id"],
                model_call_attempt_id=kwargs["model_call_attempt_id"],
            )

    async def revoke(position: int, decision: SourceAuthorityDecision) -> SourceAuthorityDecision:
        resolver = _Resolver(references[position].reference_id, decision)
        guard = ProtectedSourceResultGuard(resolver)  # type: ignore[arg-type]
        with (
            authenticated_request_scope(_context()),
            model_call_scope(run_id="run-a", model_call_attempt_id="attempt-a"),
        ):
            result = await guard.reauthorize_for_effect(references)
        # The revoked member was actually consulted -- a narrowed aggregate that
        # never asked about reads 2..N is caught here even if it guessed right.
        assert references[position].reference_id in resolver.seen
        return result

    async def scenario() -> None:
        for position in range(len(references)):
            assert (
                await revoke(position, SourceAuthorityDecision.PERMANENT_WITHHELD)
            ) is SourceAuthorityDecision.PERMANENT_WITHHELD, (
                f"revoking union member {position} did not withhold the effect"
            )
            assert (
                await revoke(position, SourceAuthorityDecision.RETRYABLE_UNKNOWN)
            ) is SourceAuthorityDecision.RETRYABLE_UNKNOWN, (
                f"unknown authority on union member {position} did not stop the effect"
            )

    asyncio.run(scenario())


# --- NE-REF-VOCAB: exactly one new thing crosses the delivery seam ------------


def _tool_ref(*, reference_id: str = "pending-result-1") -> SourceBoundToolResultRef:
    return SourceBoundToolResultRef(
        reference_id=reference_id, result_hash="e" * 64, stamps=(_stamp(),)
    )


def _authorized(text: str) -> Any:
    return source_results_module.MaterializedSourceResult(
        decision=SourceAuthorityDecision.AUTHORIZED, text=text, stamps=(_stamp(),)
    )


def test_an_authorized_tool_result_is_delivered_under_a_citable_id() -> None:
    """The id the claim contract resolves, beside the text it names.

    `reference_id` is `uuid.uuid4()` in production (`pending_result_id`): a random
    surrogate that encodes no content, no source identity, no ACL and nothing
    withheld. It is delivered ONLY alongside material the model is already
    authorized to read, so it discloses nothing the delivery had not already
    disclosed — it makes that same material nameable.
    """
    delivered = source_results_module._delivered_text(
        _tool_ref(), _authorized("the authorized chunk text")
    )

    assert delivered == "[source-id: pending-result-1]\nthe authorized chunk text"


def test_a_withheld_or_unknown_decision_is_delivered_with_no_id_at_all() -> None:
    """Nothing was delivered, so there is nothing to cite.

    Naming a reference whose material was refused would invite a claim over
    content the model never received — a citation to an absence.
    """
    for result in (source_results_module._WITHHELD, source_results_module._UNAVAILABLE):
        delivered = source_results_module._delivered_text(_tool_ref(), result)

        assert delivered == result.text
        assert "source-id" not in delivered


def test_a_non_tool_result_reference_crosses_exactly_as_it_did_before() -> None:
    """Only a Tool result is claimable, so only a Tool result is named.

    This seam decides what reaches a model and it stays boring: one line, for one
    reference kind, on the authorized path, and byte-identical behaviour for
    everything else.
    """
    output_ref = ProtectedSourceOutputRef(
        reference_id="protected-output-1", output_hash="a" * 64, stamps=(_stamp(),)
    )

    assert source_results_module._delivered_text(output_ref, _authorized("text")) == "text"
    # And a part that carried no reference at all is never routed here.
    assert source_results_module._delivered_text(None, _authorized("text")) == "text"


# ---------------------------------------------------------------------------
# THE ANSWER THAT COULD NEVER BE READ.
#
# `run_finalizer._protect_output` packs the SET of evidence records an answer is
# bound to into the protected source output's own encrypted payload:
#
#     evidence_ids = tuple(dict.fromkeys(... candidate.observations.evidence))
#     storage_plaintext = _pack_source_bound_payload(output, stamps, evidence_ids=evidence_ids)
#
# so any source-derived answer whose Run produced evidence is stored under the
# `...payload.v2` schema WITH those ids (run_store `_pack_source_bound_payload`
# selects v2 on exactly that condition, and `_unpack...` REQUIRES them for v2).
#
# PRE-011C (#731) then taught this resolver to bind an evidence set to a
# `SourceBoundToolResultSetRef`, and closed the door behind it: any OTHER
# reference type carrying stored evidence ids is refused. The run finalizer
# landed after that commit and was never taught about the door.
#
# The result is not a refusal and not a stale source. It is a permanent,
# deterministic RETRYABLE_UNKNOWN for every grounded answer the product exists
# to produce -- and because `evidence_projection` collapses `unknown` into
# "Answer withheld — access changed", it presented as an access decision that
# had never been made.


def _output_reference(payload: bytes) -> ProtectedSourceOutputRef:
    return ProtectedSourceOutputRef(
        reference_id="output-a",
        output_hash=hashlib.sha256(payload).hexdigest(),
        stamps=(_stamp(),),
    )


class _PackedProjectionReader:
    """Serves the payload the FINALIZER actually writes, evidence ids and all."""

    def __init__(self, payload: bytes, evidence_ids: tuple[str, ...]) -> None:
        self.payload = payload
        self.evidence_ids = evidence_ids

    async def load_for_evidence_projection(
        self,
        reference: ProtectedSourceOutputRef | ProtectedClaimSliceRef,
        *,
        owner_principal_id: str,
        run_id: str,
    ) -> StoredSourcePayload:
        return StoredSourcePayload(
            plaintext=self.payload,
            plaintext_hash=hashlib.sha256(self.payload).hexdigest(),
            stamps=reference.stamps,
            evidence_ids=self.evidence_ids,
        )


def _resolve_output(
    evidence_ids: tuple[str, ...],
    *,
    decision: SourceAuthorityDecision = SourceAuthorityDecision.AUTHORIZED,
    evaluated: list[AuthenticatedRequestContext] | None = None,
) -> tuple[SourceAuthorityDecision, str | None]:
    payload = b"Meridian holds 4 subjects, per the 2026-06 portfolio record."
    reference = _output_reference(payload)
    reader = _PackedProjectionReader(payload, evidence_ids)
    resolver, seen = _resolver(cast(EvidenceProjectionPayloadReader, reader), decision=decision)
    context = _context()

    async def scenario() -> SourceBoundModelSendResolution:
        with authenticated_request_scope(context):
            return await resolver.for_evidence_projection(  # type: ignore[return-value]
                reference,
                principal_id=context.principal_id,
                access_context_hash=context.context_hash,
                run_id="run-a",
                evidence_projection_attempt_id="read-a",
            )

    result = asyncio.run(scenario())
    if evaluated is not None:
        evaluated.extend(seen)
    return result.decision, result.materialized_text


def test_a_grounded_answer_bound_to_evidence_records_still_resolves() -> None:
    """THE REGRESSION, stated as the behaviour it broke.

    A Run that read source records and produced evidence is the ONLY kind of Run
    that reaches this resolver at all -- source-free answers never get here. So
    refusing every payload that names its evidence set made the sixth target
    unreachable in production while every test of it kept passing, because each
    one hand-built a `StoredSourcePayload` with the default empty `evidence_ids`
    instead of the packed payload the finalizer writes.
    """

    evaluated: list[AuthenticatedRequestContext] = []
    decision, text = _resolve_output(("evidence-1", "evidence-2"), evaluated=evaluated)
    assert decision is SourceAuthorityDecision.AUTHORIZED
    assert text == "Meridian holds 4 subjects, per the 2026-06 portfolio record."
    # AND THE AUTHORITY CHECK ACTUALLY RAN. This is the half that matters for
    # review: the guard used to reject the payload BEFORE any stamp was
    # evaluated, so the source-authority decision was never reached at all --
    # which is why the live authority tables looked irrelevant to the outage
    # (they were) and why no amount of fixing them would have helped.
    assert evaluated == [_context()]


def test_the_evidence_bound_answer_still_obeys_current_source_authority() -> None:
    """Accepting the ids relaxes NOTHING about the authority decision itself."""

    withheld, text = _resolve_output(
        ("evidence-1",), decision=SourceAuthorityDecision.PERMANENT_WITHHELD
    )
    assert withheld is SourceAuthorityDecision.PERMANENT_WITHHELD
    assert text is None
    unknown, text = _resolve_output(
        ("evidence-1",), decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN
    )
    assert unknown is SourceAuthorityDecision.RETRYABLE_UNKNOWN
    assert text is None


def test_an_answer_naming_no_evidence_set_is_still_fine() -> None:
    """The v1 payload -- a served delivery that produced no evidence record."""

    decision, text = _resolve_output(())
    assert decision is SourceAuthorityDecision.AUTHORIZED
    assert text is not None


def test_a_reference_whose_writer_never_packs_evidence_ids_still_refuses_them() -> None:
    """PRE-011C's DOOR STAYS SHUT on every target that has no evidence set.

    Only two writers pack ids: the pending tool result for an evidence SET, and
    the run finalizer's protected source OUTPUT. A claim slice is packed v1
    (`run_store` line 7157, no `evidence_ids=`), so ids appearing on one are a
    payload that does not match its target and must not resolve.
    """

    payload = b"exact claim"
    reference = ProtectedClaimSliceRef(
        reference_id="claim-a",
        slice_hash=hashlib.sha256(payload).hexdigest(),
        stamps=(_stamp(),),
    )
    reader = _PackedProjectionReader(payload, ("evidence-1",))
    resolver, _ = _resolver(cast(EvidenceProjectionPayloadReader, reader))
    context = _context()

    async def scenario() -> Any:
        with authenticated_request_scope(context):
            return await resolver.for_evidence_projection(
                reference,
                principal_id=context.principal_id,
                access_context_hash=context.context_hash,
                run_id="run-a",
                evidence_projection_attempt_id="read-a",
            )

    assert asyncio.run(scenario()).decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN
