# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DELIVERY-PASS-MEMO: one delivery pass asks about one stamp once.

Self-contained per this package's test convention (fixtures mirror
``test_run_store.py``, not imported from it). The three pinned behaviours:

* OUTSIDE a pass scope, every resolution evaluates every stamp — byte-identical
  to the unmemoized resolver.
* INSIDE one pass scope, a stamp already decided for the same principal and
  access context is not re-asked, and the memoized decision is the one served.
* ACROSS scopes nothing is remembered: the next pass re-evaluates, so authority
  revoked between the materialize and reauthorize passes still withholds.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa

from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_store import AgentRunStore, ensure_run_schema
from ne.runtime.source_results import (
    RunStoreProtectedSourceOutputWriter,
    RunStoreSourceBoundResultResolver,
    _stamp_decision_memo_scope,
    authenticated_request_scope,
)
from ne.runtime.store import ensure_runtime_schema


def _stamp() -> SourceAuthorityStamp:
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant-a", site_id="", drive_id="drive-a", item_id="item-a"
        ),
        generation=3,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint", descriptor_schema_version=1, descriptor_hash="a" * 64
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


def _runs() -> tuple[sa.Engine, AgentRunStore]:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    cipher = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
    )
    return engine, AgentRunStore(engine, cipher)


def _context() -> AuthenticatedRequestContext:
    return AuthenticatedRequestContext(
        principal_id="alice",
        authorization_groups=("analysts",),
        issuer="https://issuer.example",
        expires_at=4_102_444_800,
    )


def test_one_pass_evaluates_one_stamp_once_and_the_next_pass_re_evaluates() -> None:
    engine, runs = _runs()
    context = _context()
    calls: list[SourceAuthorityDecision] = []
    decisions = iter(
        [
            SourceAuthorityDecision.AUTHORIZED,  # pass one, single evaluation
            SourceAuthorityDecision.PERMANENT_WITHHELD,  # pass two re-asks
        ]
    )

    async def evaluate(stamp, context):
        del stamp, context
        decision = next(decisions)
        calls.append(decision)
        return decision

    async def exercise() -> None:
        writer = RunStoreProtectedSourceOutputWriter(runs)
        resolver = RunStoreSourceBoundResultResolver(runs, evaluate)
        with authenticated_request_scope(context):
            references = []
            for index in range(2):
                payload = f"protected answer {index}".encode()
                references.append(
                    await writer.claim_output(
                        target_id="run-a",
                        attempt_id=f"attempt-{index}",
                        output=payload,
                        output_hash=hashlib.sha256(payload).hexdigest(),
                        stamps=(_stamp(),),
                    )
                )

            async def resolve(reference, attempt):
                return await resolver.for_model_send(
                    reference,
                    principal_id=context.principal_id,
                    access_context_hash=context.context_hash,
                    run_id="run-a",
                    model_call_attempt_id=attempt,
                )

            # One pass: two references, one shared stamp, ONE evaluation, and
            # the memoized decision is served to the second reference.
            with _stamp_decision_memo_scope():
                first = await resolve(references[0], "attempt-0")
                second = await resolve(references[1], "attempt-1")
            assert len(calls) == 1
            assert first.decision is SourceAuthorityDecision.AUTHORIZED
            assert second.decision is SourceAuthorityDecision.AUTHORIZED

            # The NEXT pass re-evaluates: revocation between passes withholds.
            with _stamp_decision_memo_scope():
                third = await resolve(references[0], "attempt-0")
            assert len(calls) == 2
            assert third.decision is SourceAuthorityDecision.PERMANENT_WITHHELD

    asyncio.run(exercise())
    engine.dispose()


def test_outside_a_pass_scope_every_resolution_evaluates() -> None:
    engine, runs = _runs()
    context = _context()
    evaluations = 0

    async def evaluate(stamp, context):
        nonlocal evaluations
        del stamp, context
        evaluations += 1
        return SourceAuthorityDecision.AUTHORIZED

    async def exercise() -> None:
        writer = RunStoreProtectedSourceOutputWriter(runs)
        resolver = RunStoreSourceBoundResultResolver(runs, evaluate)
        with authenticated_request_scope(context):
            payload = b"protected answer"
            reference = await writer.claim_output(
                target_id="run-a",
                attempt_id="attempt-a",
                output=payload,
                output_hash=hashlib.sha256(payload).hexdigest(),
                stamps=(_stamp(),),
            )
            for _ in range(2):
                resolved = await resolver.for_model_send(
                    reference,
                    principal_id=context.principal_id,
                    access_context_hash=context.context_hash,
                    run_id="run-a",
                    model_call_attempt_id="attempt-a",
                )
                assert resolved.decision is SourceAuthorityDecision.AUTHORIZED

    asyncio.run(exercise())
    assert evaluations == 2
    engine.dispose()
