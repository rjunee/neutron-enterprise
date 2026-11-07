# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-ENFORCEMENT current-head/health/ACL selector controls."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from ne.brain.current_source_authority import (
    HmacAuthorizedContinuationTokenCodec,
    SqlAlchemyAuthorizedContinuationStore,
    SqlAlchemyCurrentSourceAuthorityReader,
    SqlAlchemyCurrentSourceAuthorityWriter,
    claim_or_load_deployment_construction_id,
    ne_source_authority_acl_snapshots,
    persist_current_acl_snapshot,
    publish_governing_contract_epoch,
)
from ne.brain.current_source_filter import (
    CurrentAclSnapshot,
    CurrentAuthorizedArtifactSelectorImpl,
    CurrentSourceDisclosureEvaluator,
    HistoricalSourceDisclosureEvaluator,
    RankedArtifactBatch,
    RankedArtifactCandidate,
    SqlAlchemyCurrentAclSnapshotResolver,
)
from ne.contracts.source_authority import AuthorizedArtifactPageStatus, SourceAuthorityDecision
from ne.contracts.types import Principal

from .test_current_source_authority import (
    ACL_A,
    ACL_B,
    admit,
    memory_engine,
    stamp_for,
    t3_artifact,
    t3_contract,
    t3_identity,
)

ACCESS = "1" * 64
REQUEST = "2" * 64
CONFIG = "3" * 64
CONTRACT_HEAD = "4" * 64
NOW = datetime(2099, 8, 22, tzinfo=UTC)
PRINCIPAL = Principal(id="alice", groups=("analysts",))


class _AclSnapshots:
    def __init__(self) -> None:
        self.snapshots: dict[str, CurrentAclSnapshot] = {}
        self.calls = 0

    def resolve_current_acl(
        self, stamp, *, current_receipt
    ):  # type annotation would only repeat the Protocol
        assert current_receipt.artifact_identity == stamp.artifact_identity
        self.calls += 1
        return self.snapshots.get(stamp.artifact_identity.artifact_id)


class _CountingAuthority:
    def __init__(self, authority) -> None:
        self._authority = authority
        self.calls: list[str] = []

    async def resolve(self, stamp, *, principal_id: str, access_context_hash: str):
        self.calls.append(stamp.artifact_identity.artifact_id)
        return await self._authority.resolve(
            stamp,
            principal_id=principal_id,
            access_context_hash=access_context_hash,
        )


class _Candidates:
    def __init__(self, candidates: tuple[RankedArtifactCandidate, ...], *, end: bool) -> None:
        self.candidates = tuple(sorted(candidates, key=lambda item: (-item.score, item.cursor_id)))
        self.end = end
        self.calls = 0

    async def scan(self, *, after_score, after_cursor_id, limit: int) -> RankedArtifactBatch:
        self.calls += 1
        items = self.candidates
        if after_score is not None:
            assert after_cursor_id is not None
            cursor = (-after_score, after_cursor_id)
            items = tuple(item for item in items if (-item.score, item.cursor_id) > cursor)
        page = items[:limit]
        return RankedArtifactBatch(
            items=page,
            end_reached=self.end and len(items) <= limit,
            scope_health_epoch=7,
        )


def _authority():
    engine = memory_engine()
    construction = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction)
    reader = SqlAlchemyCurrentSourceAuthorityReader(engine, construction)
    continuation = SqlAlchemyAuthorizedContinuationStore(
        engine,
        HmacAuthorizedContinuationTokenCodec("filter_test", b"k" * 32),
    )
    return writer, reader, continuation


def _admit(writer, key: str, *, acl_hash: str = ACL_A, revision: str = "rev-1"):
    identity = t3_identity(key)
    contract = t3_contract()
    artifact = t3_artifact(
        identity,
        contract,
        content=f"content:{key}".encode(),
        acl_snapshot_hash=acl_hash,
    )
    receipt = admit(
        writer,
        identity,
        contract,
        artifact,
        scope_path=("ledger", key),
        source_revision=revision,
        acl_snapshot_hash=acl_hash,
    )
    return stamp_for(receipt)


def _candidate(stamp, score: str) -> RankedArtifactCandidate:
    return RankedArtifactCandidate(
        cursor_id=stamp.artifact_identity.artifact_id,
        score=Decimal(score),
        artifact_identity=stamp.artifact_identity,
        authority_stamp=stamp,
    )


def _selector(candidates: _Candidates, evaluator, continuation):
    return CurrentAuthorizedArtifactSelectorImpl(
        candidates,
        evaluator,
        continuation,
        principal=PRINCIPAL,
        config_hash=CONFIG,
        contract_head_hash=CONTRACT_HEAD,
        clock=lambda: NOW,
    )


def test_evaluator_requires_current_head_and_current_acl_membership() -> None:
    writer, reader, _continuation = _authority()
    stamp = _admit(writer, "row-a")
    acl = _AclSnapshots()
    acl.snapshots[stamp.artifact_identity.artifact_id] = CurrentAclSnapshot(
        artifact_id=stamp.artifact_identity.artifact_id,
        snapshot_hash=ACL_A,
        labels=("analysts",),
    )
    evaluator = CurrentSourceDisclosureEvaluator(reader, acl)

    allowed = asyncio.run(
        evaluator.evaluate(stamp, principal=PRINCIPAL, access_context_hash=ACCESS)
    )
    assert allowed.decision is SourceAuthorityDecision.AUTHORIZED

    acl.snapshots[stamp.artifact_identity.artifact_id] = CurrentAclSnapshot(
        artifact_id=stamp.artifact_identity.artifact_id,
        snapshot_hash=ACL_A,
        labels=("another-team",),
    )
    denied = asyncio.run(evaluator.evaluate(stamp, principal=PRINCIPAL, access_context_hash=ACCESS))
    assert denied.decision is SourceAuthorityDecision.PERMANENT_WITHHELD

    # A newer current artifact makes the old stamp unavailable before the ACL
    # provider is consulted; its historically granting labels cannot revive it.
    replacement = t3_artifact(
        stamp.source_identity,
        stamp.source_contract,
        content=b"replacement",
        acl_snapshot_hash=ACL_B,
    )
    admit(
        writer,
        stamp.source_identity,
        stamp.source_contract,
        replacement,
        scope_path=("ledger", "row-a"),
        source_revision="rev-2",
        acl_snapshot_hash=ACL_B,
        expected_head_revision="rev-1",
    )
    calls = acl.calls
    stale = asyncio.run(evaluator.evaluate(stamp, principal=PRINCIPAL, access_context_hash=ACCESS))
    assert stale.decision is not SourceAuthorityDecision.AUTHORIZED
    assert acl.calls == calls


def test_sql_acl_snapshot_is_exact_hash_bound_and_missing_or_tampered_fails_closed() -> None:
    labels = ("analysts",)
    canonical = json.dumps(
        {"acl": list(labels)}, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    acl_hash = hashlib.sha256(canonical.encode()).hexdigest()
    engine = memory_engine()
    construction = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction)
    reader = SqlAlchemyCurrentSourceAuthorityReader(engine, construction)
    stamp = _admit(writer, "sql-acl", acl_hash=acl_hash)
    evaluator = CurrentSourceDisclosureEvaluator(
        reader, SqlAlchemyCurrentAclSnapshotResolver(engine)
    )

    absent = asyncio.run(evaluator.evaluate(stamp, principal=PRINCIPAL, access_context_hash=ACCESS))
    assert absent.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN

    persist_current_acl_snapshot(
        engine,
        identity_hash=stamp.source_identity.identity_hash,
        artifact_id=stamp.artifact_identity.artifact_id,
        acl_snapshot_hash=acl_hash,
        labels=labels,
    )
    allowed = asyncio.run(
        evaluator.evaluate(stamp, principal=PRINCIPAL, access_context_hash=ACCESS)
    )
    assert allowed.decision is SourceAuthorityDecision.AUTHORIZED

    with engine.begin() as conn:
        conn.execute(
            sa.update(ne_source_authority_acl_snapshots)
            .where(
                ne_source_authority_acl_snapshots.c.artifact_id
                == stamp.artifact_identity.artifact_id
            )
            .values(labels=["other-team"])
        )
    tampered = asyncio.run(
        evaluator.evaluate(stamp, principal=PRINCIPAL, access_context_hash=ACCESS)
    )
    assert tampered.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN


def test_sql_acl_snapshot_accepts_authorized_observation_only_receipt_rotation() -> None:
    """A newer observation receipt for unchanged truth reuses the same ACL sidecar."""
    labels = ("analysts",)
    canonical = json.dumps(
        {"acl": list(labels)}, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    acl_hash = hashlib.sha256(canonical.encode()).hexdigest()
    engine = memory_engine()
    construction = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction)
    reader = SqlAlchemyCurrentSourceAuthorityReader(engine, construction)
    identity = t3_identity("sql-acl-observation-refresh")
    contract = t3_contract()
    artifact = t3_artifact(
        identity,
        contract,
        content=b"unchanged truth",
        acl_snapshot_hash=acl_hash,
    )
    first_observation = datetime.now(UTC) - timedelta(minutes=5)
    first = admit(
        writer,
        identity,
        contract,
        artifact,
        scope_path=("ledger", "sql-acl-observation-refresh"),
        acl_snapshot_hash=acl_hash,
        as_of=first_observation,
    )
    refreshed = admit(
        writer,
        identity,
        contract,
        artifact,
        scope_path=("ledger", "sql-acl-observation-refresh"),
        acl_snapshot_hash=acl_hash,
        expected_head_revision="rev-1",
        as_of=first_observation + timedelta(minutes=1),
    )
    assert first.receipt_id != refreshed.receipt_id
    persist_current_acl_snapshot(
        engine,
        identity_hash=identity.identity_hash,
        artifact_id=artifact.artifact_id,
        acl_snapshot_hash=acl_hash,
        labels=labels,
    )

    result = asyncio.run(
        CurrentSourceDisclosureEvaluator(
            reader, SqlAlchemyCurrentAclSnapshotResolver(engine)
        ).evaluate(stamp_for(first), principal=PRINCIPAL, access_context_hash=ACCESS)
    )

    assert result.decision is SourceAuthorityDecision.AUTHORIZED


def test_acl_snapshot_hash_mismatch_is_unknown_not_historical_acl_fallback() -> None:
    writer, reader, _continuation = _authority()
    stamp = _admit(writer, "row-a")
    acl = _AclSnapshots()
    acl.snapshots[stamp.artifact_identity.artifact_id] = CurrentAclSnapshot(
        artifact_id=stamp.artifact_identity.artifact_id,
        snapshot_hash=ACL_B,
        labels=("analysts",),
    )
    result = asyncio.run(
        CurrentSourceDisclosureEvaluator(reader, acl).evaluate(
            stamp, principal=PRINCIPAL, access_context_hash=ACCESS
        )
    )
    assert result.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN


def test_progressive_selector_scans_past_stale_crowding_to_current_result() -> None:
    writer, reader, continuation = _authority()
    stale = _admit(writer, "stale")
    current = _admit(writer, "current")
    replacement = t3_artifact(
        stale.source_identity,
        stale.source_contract,
        content=b"new stale head",
        acl_snapshot_hash=ACL_A,
    )
    admit(
        writer,
        stale.source_identity,
        stale.source_contract,
        replacement,
        scope_path=("ledger", "stale"),
        source_revision="rev-2",
        expected_head_revision="rev-1",
    )
    acl = _AclSnapshots()
    for stamp in (stale, current):
        acl.snapshots[stamp.artifact_identity.artifact_id] = CurrentAclSnapshot(
            artifact_id=stamp.artifact_identity.artifact_id,
            snapshot_hash=ACL_A,
            labels=("analysts",),
        )
    candidates = _Candidates((_candidate(stale, "10"), _candidate(current, "1")), end=True)
    page = asyncio.run(
        _selector(candidates, CurrentSourceDisclosureEvaluator(reader, acl), continuation).select(
            principal_id=PRINCIPAL.id,
            access_context_hash=ACCESS,
            request_hash=REQUEST,
            top_k=1,
            scan_limit=2,
        )
    )
    assert [item.artifact_identity.artifact_id for item in page.items] == [
        current.artifact_identity.artifact_id
    ]
    assert page.status is AuthorizedArtifactPageStatus.COMPLETE
    assert page.scanned_count == 2
    assert page.returned_count == 1


def test_bound_is_partial_with_some_results_and_exhausted_with_none() -> None:
    writer, reader, continuation = _authority()
    allowed = _admit(writer, "allowed")
    denied = _admit(writer, "denied")
    acl = _AclSnapshots()
    acl.snapshots[allowed.artifact_identity.artifact_id] = CurrentAclSnapshot(
        artifact_id=allowed.artifact_identity.artifact_id,
        snapshot_hash=ACL_A,
        labels=("analysts",),
    )
    acl.snapshots[denied.artifact_identity.artifact_id] = CurrentAclSnapshot(
        artifact_id=denied.artifact_identity.artifact_id,
        snapshot_hash=ACL_A,
        labels=("other",),
    )
    evaluator = CurrentSourceDisclosureEvaluator(reader, acl)

    partial = asyncio.run(
        _selector(
            _Candidates((_candidate(allowed, "2"), _candidate(denied, "1")), end=False),
            evaluator,
            continuation,
        ).select(
            principal_id=PRINCIPAL.id,
            access_context_hash=ACCESS,
            request_hash=REQUEST,
            top_k=2,
            scan_limit=1,
        )
    )
    assert partial.status is AuthorizedArtifactPageStatus.PARTIAL
    assert partial.returned_count == 1
    assert partial.continuation_token is not None

    exhausted = asyncio.run(
        _selector(
            _Candidates((_candidate(denied, "2"), _candidate(allowed, "1")), end=False),
            evaluator,
            continuation,
        ).select(
            principal_id=PRINCIPAL.id,
            access_context_hash=ACCESS,
            request_hash="9" * 64,
            top_k=1,
            scan_limit=1,
        )
    )
    assert exhausted.status is AuthorizedArtifactPageStatus.EXHAUSTED
    assert exhausted.items == ()
    assert exhausted.continuation_token is not None


def test_proven_backend_end_with_no_authorized_result_is_complete_empty() -> None:
    writer, reader, continuation = _authority()
    denied = _admit(writer, "denied")
    acl = _AclSnapshots()
    acl.snapshots[denied.artifact_identity.artifact_id] = CurrentAclSnapshot(
        artifact_id=denied.artifact_identity.artifact_id,
        snapshot_hash=ACL_A,
        labels=("other",),
    )
    page = asyncio.run(
        _selector(
            _Candidates((_candidate(denied, "1"),), end=True),
            CurrentSourceDisclosureEvaluator(reader, acl),
            continuation,
        ).select(
            principal_id=PRINCIPAL.id,
            access_context_hash=ACCESS,
            request_hash=REQUEST,
            top_k=1,
            scan_limit=2,
        )
    )
    assert page.status is AuthorizedArtifactPageStatus.COMPLETE
    assert page.items == ()
    assert page.continuation_token is None


def test_continuation_is_bound_before_candidate_access_and_has_no_duplicates() -> None:
    writer, reader, continuation = _authority()
    first = _admit(writer, "first")
    second = _admit(writer, "second")
    acl = _AclSnapshots()
    for stamp in (first, second):
        acl.snapshots[stamp.artifact_identity.artifact_id] = CurrentAclSnapshot(
            artifact_id=stamp.artifact_identity.artifact_id,
            snapshot_hash=ACL_A,
            labels=("analysts",),
        )
    candidates = _Candidates((_candidate(first, "2"), _candidate(second, "1")), end=True)
    selector = _selector(candidates, CurrentSourceDisclosureEvaluator(reader, acl), continuation)
    first_page = asyncio.run(
        selector.select(
            principal_id=PRINCIPAL.id,
            access_context_hash=ACCESS,
            request_hash=REQUEST,
            top_k=2,
            scan_limit=1,
        )
    )
    assert first_page.status is AuthorizedArtifactPageStatus.PARTIAL
    calls = candidates.calls
    try:
        asyncio.run(
            selector.select(
                principal_id=PRINCIPAL.id,
                access_context_hash=ACCESS,
                request_hash="8" * 64,
                top_k=2,
                scan_limit=1,
                continuation_token=first_page.continuation_token,
            )
        )
    except Exception as exc:  # public continuation error is deliberately opaque
        assert "continuation is unavailable" in str(exc)
    else:
        raise AssertionError("cross-query continuation must fail")
    assert candidates.calls == calls

    second_page = asyncio.run(
        selector.select(
            principal_id=PRINCIPAL.id,
            access_context_hash=ACCESS,
            request_hash=REQUEST,
            top_k=2,
            scan_limit=1,
            continuation_token=first_page.continuation_token,
        )
    )
    assert first_page.items[0].artifact_identity != second_page.items[0].artifact_identity
    assert second_page.status is AuthorizedArtifactPageStatus.COMPLETE


def test_identical_first_page_retry_is_byte_stable_without_rescanning() -> None:
    writer, reader, continuation = _authority()
    first_stamp = _admit(writer, "row-a")
    second_stamp = _admit(writer, "row-b")
    acl = _AclSnapshots()
    for stamp in (first_stamp, second_stamp):
        acl.snapshots[stamp.artifact_identity.artifact_id] = CurrentAclSnapshot(
            artifact_id=stamp.artifact_identity.artifact_id,
            snapshot_hash=ACL_A,
            labels=("analysts",),
        )
    candidates = _Candidates(
        (_candidate(first_stamp, "2"), _candidate(second_stamp, "1")),
        end=False,
    )
    counting_authority = _CountingAuthority(reader)
    selector = _selector(
        candidates,
        CurrentSourceDisclosureEvaluator(counting_authority, acl),
        continuation,
    )
    first = asyncio.run(
        selector.select(
            principal_id=PRINCIPAL.id,
            access_context_hash=ACCESS,
            request_hash=REQUEST,
            top_k=3,
            scan_limit=2,
        )
    )
    second = asyncio.run(
        selector.select(
            principal_id=PRINCIPAL.id,
            access_context_hash=ACCESS,
            request_hash=REQUEST,
            top_k=3,
            scan_limit=2,
        )
    )
    assert second is first
    assert second == first
    assert second.model_dump_json().encode() == first.model_dump_json().encode()
    assert candidates.calls == 1
    assert counting_authority.calls == [
        first_stamp.artifact_identity.artifact_id,
        second_stamp.artifact_identity.artifact_id,
        first_stamp.artifact_identity.artifact_id,
        second_stamp.artifact_identity.artifact_id,
    ]


def test_identical_first_page_retry_rechecks_authority_and_evicts_stale_disclosure() -> None:
    engine = memory_engine()
    construction = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction)
    reader = _CountingAuthority(SqlAlchemyCurrentSourceAuthorityReader(engine, construction))
    continuation = SqlAlchemyAuthorizedContinuationStore(
        engine,
        HmacAuthorizedContinuationTokenCodec("filter_test", b"k" * 32),
    )
    stamp = _admit(writer, "epoch-row")
    publish_governing_contract_epoch(
        engine,
        identity_hash=stamp.source_identity.identity_hash,
        contract_scope_hash="f" * 64,
        contract_epoch=1,
    )
    acl = _AclSnapshots()
    acl.snapshots[stamp.artifact_identity.artifact_id] = CurrentAclSnapshot(
        artifact_id=stamp.artifact_identity.artifact_id,
        snapshot_hash=ACL_A,
        labels=("analysts",),
    )
    candidates = _Candidates((_candidate(stamp, "1"),), end=False)
    selector = _selector(
        candidates,
        CurrentSourceDisclosureEvaluator(reader, acl),
        continuation,
    )

    first = asyncio.run(
        selector.select(
            principal_id=PRINCIPAL.id,
            access_context_hash=ACCESS,
            request_hash=REQUEST,
            top_k=2,
            scan_limit=1,
        )
    )
    assert [item.artifact_identity.artifact_id for item in first.items] == [
        stamp.artifact_identity.artifact_id
    ]
    assert first.continuation_token is not None

    publish_governing_contract_epoch(
        engine,
        identity_hash=stamp.source_identity.identity_hash,
        contract_scope_hash="f" * 64,
        contract_epoch=2,
    )
    candidates.end = True
    retry = asyncio.run(
        selector.select(
            principal_id=PRINCIPAL.id,
            access_context_hash=ACCESS,
            request_hash=REQUEST,
            top_k=2,
            scan_limit=1,
        )
    )

    assert retry.items == ()
    assert retry.continuation_token is None
    assert retry.status is AuthorizedArtifactPageStatus.COMPLETE
    assert candidates.calls == 2
    assert reader.calls == [
        stamp.artifact_identity.artifact_id,
        stamp.artifact_identity.artifact_id,
        stamp.artifact_identity.artifact_id,
    ]
    assert acl.calls == 1


def _canonical_acl(labels: tuple[str, ...]) -> str:
    canonical = json.dumps(
        {"acl": list(sorted(set(labels)))},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_historical_evaluator_discloses_superseded_stamps_but_never_fabrication_or_revoked() -> (
    None
):
    """Temporal provenance: supersession is history's normal state.

    The CURRENT evaluator must keep withholding a superseded stamp (never serve
    stale content as current); the HISTORICAL evaluator must keep disclosing the
    retained record of how a past answer was derived — while still refusing a
    stamp the receipt ledger never minted and a principal outside the CURRENT
    ACL snapshot.
    """

    engine = memory_engine()
    construction = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction)
    reader = SqlAlchemyCurrentSourceAuthorityReader(engine, construction)

    labels = ("analysts",)
    acl_hash = _canonical_acl(labels)
    identity = t3_identity("sop")
    contract = t3_contract()

    art_v1 = t3_artifact(identity, contract, content=b"v1 body", acl_snapshot_hash=acl_hash)
    first = admit(
        writer,
        identity,
        contract,
        art_v1,
        scope_path=("ledger", "sop"),
        source_revision="rev-1",
        acl_snapshot_hash=acl_hash,
    )
    old_stamp = stamp_for(first)
    persist_current_acl_snapshot(
        engine,
        identity_hash=identity.identity_hash,
        artifact_id=art_v1.artifact_id,
        acl_snapshot_hash=acl_hash,
        labels=labels,
    )

    # The live edit: the same source identity re-admits with new content and a
    # new revision — the head moves on, the old stamp becomes historical.
    art_v2 = t3_artifact(identity, contract, content=b"v2 body", acl_snapshot_hash=acl_hash)
    second = admit(
        writer,
        identity,
        contract,
        art_v2,
        scope_path=("ledger", "sop"),
        source_revision="rev-2",
        acl_snapshot_hash=acl_hash,
        expected_head_revision="rev-1",
    )
    persist_current_acl_snapshot(
        engine,
        identity_hash=identity.identity_hash,
        artifact_id=art_v2.artifact_id,
        acl_snapshot_hash=acl_hash,
        labels=labels,
    )
    member = Principal(id="alice", groups=("analysts",))
    outsider = Principal(id="mallory", groups=("interns",))
    current_evaluator = CurrentSourceDisclosureEvaluator(
        reader, SqlAlchemyCurrentAclSnapshotResolver(engine)
    )
    historical = HistoricalSourceDisclosureEvaluator(reader, engine)

    # Control: serving-as-current still refuses the superseded stamp.
    as_current = asyncio.run(
        current_evaluator.evaluate(old_stamp, principal=member, access_context_hash=ACCESS)
    )
    assert as_current.decision is SourceAuthorityDecision.PERMANENT_WITHHELD

    # The historical view keeps the retained record readable for a principal the
    # CURRENT ACL admits.
    as_history = asyncio.run(
        historical.evaluate(old_stamp, principal=member, access_context_hash=ACCESS)
    )
    assert as_history.decision is SourceAuthorityDecision.AUTHORIZED

    # The current-truth stamp also stays readable historically (sanity).
    fresh = asyncio.run(
        historical.evaluate(stamp_for(second), principal=member, access_context_hash=ACCESS)
    )
    assert fresh.decision is SourceAuthorityDecision.AUTHORIZED

    # Revocation still withholds: the CURRENT ACL snapshot governs.
    revoked = asyncio.run(
        historical.evaluate(old_stamp, principal=outsider, access_context_hash=ACCESS)
    )
    assert revoked.decision is SourceAuthorityDecision.PERMANENT_WITHHELD

    # Fabrication still withholds: a receipt id the ledger never minted.
    forged = old_stamp.model_copy(update={"authority_receipt_id": "rcpt-forged"})
    fake = asyncio.run(historical.evaluate(forged, principal=member, access_context_hash=ACCESS))
    assert fake.decision is SourceAuthorityDecision.PERMANENT_WITHHELD
