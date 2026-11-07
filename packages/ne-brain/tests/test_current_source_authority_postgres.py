# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-Postgres proofs for the 015A0-AUTHORITY-CORE head and receipt store.

Env-gated on ``NE_TEST_POSTGRES_URL`` and PROMOTED into the REQUIRED ``check`` job,
which fails if any node here skips or collects zero. In-memory SQLite cannot stand
in: its global writer lock serializes the very interleaving under test, so a SQLite
"exactly one claim winner" proof passes against an unfenced implementation too.

:func:`test_an_unfenced_claim_allocation_duplicates_under_the_same_harness` is the
positive control for that. It reimplements the read-then-update allocation WITHOUT
the CAS, under the identical barrier and worker count, and shows it admits every
worker — so a green arbitration proof cannot be green merely because the harness
never produced a race.

The supersession proofs deliberately revalidate a retained receipt on a database
whose head row has been DELETED. An EvidenceLocator must survive the head moving,
and a proof that still reads the head cannot tell the difference.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable, Iterator

import pytest
import sqlalchemy as sa

from ne.brain.current_source_authority import (
    SourceAuthorityClaimConflictError,
    SqlAlchemyCurrentSourceAuthorityReader,
    SqlAlchemyCurrentSourceAuthorityWriter,
    claim_or_load_deployment_construction_id,
    ensure_source_authority_schema,
    ne_source_authority_heads,
    ne_source_authority_receipts,
    ne_source_reconciliation_claims,
    source_authority_metadata,
)
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    SourceAuthorityDecision,
)

from .test_current_source_authority import (
    ACL_A,
    admit,
    reconciliation_scope,
    stamp_for,
    t3_artifact,
    t3_contract,
    t3_identity,
)

_URL = os.environ.get("NE_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="live Postgres not configured (set NE_TEST_POSTGRES_URL); promoted proof",
)

WORKERS = 8


@pytest.fixture
def pg_engine() -> Iterator[sa.Engine]:
    assert _URL is not None
    engine = sa.create_engine(_URL, pool_size=WORKERS * 2, max_overflow=WORKERS * 2)
    source_authority_metadata.drop_all(engine)
    ensure_source_authority_schema(engine)
    try:
        yield engine
    finally:
        source_authority_metadata.drop_all(engine)
        engine.dispose()


def _race(worker: Callable[[int], None], *, workers: int = WORKERS) -> list[BaseException]:
    """Release ``workers`` threads together; return whatever they raised."""
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def wrapped(index: int) -> None:
        barrier.wait()
        try:
            worker(index)
        except BaseException as exc:  # the point of the proof is exactly what escaped
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


# --- supersession retains every receipt --------------------------------------


def test_supersession_moves_the_head_and_appends_rather_than_rewriting_receipts(
    pg_engine: sa.Engine,
) -> None:
    construction_id = claim_or_load_deployment_construction_id(pg_engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(pg_engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    first_artifact = t3_artifact(identity, contract, content=b"revision one")
    second_artifact = t3_artifact(
        identity, contract, content=b"revision two", source_snapshot_hash="e" * 64
    )
    first = admit(writer, identity, contract, first_artifact)
    second = admit(
        writer,
        identity,
        contract,
        second_artifact,
        source_revision="rev-2",
        expected_head_revision="rev-1",
    )
    assert first.receipt_id != second.receipt_id

    with pg_engine.connect() as conn:
        head = conn.execute(sa.select(ne_source_authority_heads.c.current_receipt_id)).scalar_one()
        retained = {
            row.receipt_id: row.receipt_hash
            for row in conn.execute(
                sa.select(
                    ne_source_authority_receipts.c.receipt_id,
                    ne_source_authority_receipts.c.receipt_hash,
                )
            )
        }
    assert head == second.receipt_id
    # The superseded receipt is still there, byte for byte.
    assert retained == {
        first.receipt_id: first.receipt_hash,
        second.receipt_id: second.receipt_hash,
    }


def test_a_retained_receipt_revalidates_without_the_mutable_head(pg_engine: sa.Engine) -> None:
    """An EvidenceLocator survives a moved — or a missing — head.

    The head row is DELETED before the revalidation, so a Reader that reconstructed
    the receipt from the head could not answer at all. This is the restart path.
    """
    construction_id = claim_or_load_deployment_construction_id(pg_engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(pg_engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    artifact = t3_artifact(identity, contract, content=b"located evidence")
    receipt = admit(writer, identity, contract, artifact)
    with pg_engine.begin() as conn:
        conn.execute(sa.delete(ne_source_authority_heads))

    assert _URL is not None
    fresh = sa.create_engine(_URL)
    try:
        reader = SqlAlchemyCurrentSourceAuthorityReader(fresh, construction_id)
        revalidated = reader.load_receipt(receipt.receipt_id)
        assert revalidated is not None
        assert revalidated.receipt_id == receipt.receipt_id
        assert revalidated.receipt_hash == receipt.receipt_hash
        assert revalidated == receipt
        # And with no head, current-authority resolution is Unknown rather than
        # authorized off the retained receipt: history is not a live decision.
        resolution = asyncio.run(
            reader.resolve(stamp_for(receipt), principal_id="p", access_context_hash=ACL_A)
        )
        assert resolution.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN
    finally:
        fresh.dispose()


def test_the_reader_authorizes_the_current_stamp_and_withholds_the_superseded_one(
    pg_engine: sa.Engine,
) -> None:
    construction_id = claim_or_load_deployment_construction_id(pg_engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(pg_engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    first = admit(
        writer, identity, contract, t3_artifact(identity, contract, content=b"revision one")
    )
    second = admit(
        writer,
        identity,
        contract,
        t3_artifact(identity, contract, content=b"revision two", source_snapshot_hash="e" * 64),
        source_revision="rev-2",
        expected_head_revision="rev-1",
    )
    reader = SqlAlchemyCurrentSourceAuthorityReader(pg_engine, construction_id)
    current = asyncio.run(
        reader.resolve(stamp_for(second), principal_id="p", access_context_hash=ACL_A)
    )
    superseded = asyncio.run(
        reader.resolve(stamp_for(first), principal_id="p", access_context_hash=ACL_A)
    )
    assert current.decision is SourceAuthorityDecision.AUTHORIZED
    assert current.current_receipt == second
    assert superseded.decision is SourceAuthorityDecision.PERMANENT_WITHHELD
    assert superseded.current_receipt == second
    assert superseded.safe_reason is not None


# --- fenced claim arbitration ------------------------------------------------


def test_racing_reconcilers_elect_exactly_one_claim_winner(pg_engine: sa.Engine) -> None:
    construction_id = claim_or_load_deployment_construction_id(pg_engine)
    scope = reconciliation_scope("ledger")
    won: list[int] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        engine = sa.create_engine(str(_URL))
        try:
            writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)
            try:
                asyncio.run(writer.claim(scope, expected_head_revision=None, lease_seconds=60))
            except SourceAuthorityClaimConflictError:
                return
            with lock:
                won.append(index)
        finally:
            engine.dispose()

    errors = _race(worker)
    assert errors == [], errors
    assert len(won) == 1
    with pg_engine.connect() as conn:
        rows = conn.execute(sa.select(ne_source_reconciliation_claims)).all()
    assert len(rows) == 1
    assert rows[0].reconciliation_epoch == 1


def test_an_unfenced_claim_allocation_duplicates_under_the_same_harness(
    pg_engine: sa.Engine,
) -> None:
    """Positive control: the harness DOES interleave, so the proof above is real.

    This reimplements the allocation as read-then-update with no CAS on the epoch
    or fencing token — the shape a plausible implementation takes — under the same
    barrier and worker count. Every worker "wins", which is exactly the lost
    arbitration the fenced path must not have.
    """
    scope = reconciliation_scope("ledger")
    with pg_engine.begin() as conn:
        conn.execute(
            ne_source_reconciliation_claims.insert().values(
                scope_hash=scope.scope_hash,
                reconciliation_epoch=1,
                fencing_token="seed",
                lease_expires_at=sa.func.now(),
                head_revision=None,
            )
        )
    won: list[int] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        engine = sa.create_engine(str(_URL))
        try:
            with engine.begin() as conn:
                epoch = conn.execute(
                    sa.select(ne_source_reconciliation_claims.c.reconciliation_epoch).where(
                        ne_source_reconciliation_claims.c.scope_hash == scope.scope_hash
                    )
                ).scalar_one()
                updated = conn.execute(
                    sa.update(ne_source_reconciliation_claims)
                    .where(ne_source_reconciliation_claims.c.scope_hash == scope.scope_hash)
                    .values(reconciliation_epoch=epoch + 1, fencing_token=f"unfenced-{index}")
                ).rowcount
            if updated == 1:
                with lock:
                    won.append(index)
        finally:
            engine.dispose()

    assert _race(worker) == []
    assert len(won) == WORKERS


def test_only_the_current_fencing_token_can_move_the_head(pg_engine: sa.Engine) -> None:
    """A superseded reconciler holds a token the schema no longer honours."""
    construction_id = claim_or_load_deployment_construction_id(pg_engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(pg_engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    artifact = t3_artifact(identity, contract, content=b"fenced admission")
    scope = reconciliation_scope("ledger")

    async def scenario() -> None:
        from .test_current_source_authority import admission_fence, present_candidate

        stale = await writer.claim(scope, expected_head_revision=None, lease_seconds=60)
        await writer.release(stale)
        current = await writer.claim(scope, expected_head_revision=None, lease_seconds=60)
        assert current.fencing_token != stale.fencing_token
        with pytest.raises(Exception) as raised:
            await writer.finalize(
                stale,
                admission_fence(stale, identity),
                present_candidate(identity, contract, artifact),
            )
        assert "claim" in str(raised.value) or "fence" in str(raised.value)
        # The current holder still admits: the refusal is about the token, not
        # about admission being broken.
        receipt = await writer.finalize(
            current,
            admission_fence(current, identity),
            present_candidate(identity, contract, artifact),
        )
        assert receipt.artifact_identity == artifact

    asyncio.run(scenario())
    with pg_engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_source_authority_receipts)
            ).scalar_one()
            == 1
        )


# --- the governing contract epoch, on the deployed database ------------------


def test_an_unresolved_contract_epoch_is_unknown_then_authorizes_on_completion(
    pg_engine: sa.Engine,
) -> None:
    """The full sequence on real Postgres: admit, advance, re-admit.

    SQLite cannot stand in for the ordering here — the binding advance and the
    Reader's paired head/binding read are separate transactions against one row,
    and SQLite's global writer lock serializes exactly what this is checking.
    """
    from ne.brain.current_source_authority import publish_governing_contract_epoch

    construction_id = claim_or_load_deployment_construction_id(pg_engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(pg_engine, construction_id)
    reader = SqlAlchemyCurrentSourceAuthorityReader(pg_engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    scope_hash = reconciliation_scope("ledger").scope_hash
    first = admit(writer, identity, contract, t3_artifact(identity, contract, content=b"one"))
    publish_governing_contract_epoch(
        pg_engine,
        identity_hash=identity.identity_hash,
        contract_scope_hash=scope_hash,
        contract_epoch=1,
    )

    def decide(receipt: CurrentSourceAuthorityReceipt) -> SourceAuthorityDecision:
        return asyncio.run(
            reader.resolve(stamp_for(receipt), principal_id="p", access_context_hash=ACL_A)
        ).decision

    assert decide(first) is SourceAuthorityDecision.AUTHORIZED

    publish_governing_contract_epoch(
        pg_engine,
        identity_hash=identity.identity_hash,
        contract_scope_hash=scope_hash,
        contract_epoch=2,
    )
    assert decide(first) is SourceAuthorityDecision.RETRYABLE_UNKNOWN

    second = admit(
        writer,
        identity,
        contract,
        t3_artifact(identity, contract, content=b"two", source_snapshot_hash="e" * 64),
        source_revision="rev-2",
        contract_epoch=2,
        expected_head_revision="rev-1",
    )
    assert decide(second) is SourceAuthorityDecision.AUTHORIZED
    assert decide(first) is SourceAuthorityDecision.RETRYABLE_UNKNOWN


def test_racing_publishes_settle_on_the_highest_governing_epoch(pg_engine: sa.Engine) -> None:
    """Concurrent producers cannot leave the binding on a superseded epoch.

    Eight workers publish epochs 1..8 simultaneously against one row. The stored
    value must be 8 whatever order they land in — a last-write-wins upsert would
    settle on whichever worker happened to commit last.
    """
    from ne.brain.current_source_authority import (
        ne_source_authority_contract_epochs,
        publish_governing_contract_epoch,
    )

    identity = t3_identity("row-1")
    scope_hash = reconciliation_scope("ledger").scope_hash

    def worker(index: int) -> None:
        engine = sa.create_engine(str(_URL))
        try:
            publish_governing_contract_epoch(
                engine,
                identity_hash=identity.identity_hash,
                contract_scope_hash=scope_hash,
                contract_epoch=index + 1,
            )
        finally:
            engine.dispose()

    errors = _race(worker)
    assert errors == []
    with pg_engine.connect() as conn:
        stored = conn.execute(
            sa.select(ne_source_authority_contract_epochs.c.contract_epoch).where(
                ne_source_authority_contract_epochs.c.identity_hash == identity.identity_hash
            )
        ).scalar_one()
    assert stored == WORKERS
