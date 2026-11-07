# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from ne.brain.answer_ledger import (
    AnswerQuestionAlreadyPinnedError,
    CorpusChunkStamp,
    SqlAlchemyAnswerLedgerStore,
    classify_drift,
)
from ne.brain.current_source_filter import CurrentAclSnapshot, CurrentSourceDisclosureEvaluator
from ne.contracts.types import Principal

from .test_current_source_filter import ACCESS, ACL_A, _admit, _authority


def _engine() -> sa.Engine:
    return sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _store() -> SqlAlchemyAnswerLedgerStore:
    ticks = iter(datetime(2026, 8, 18, tzinfo=UTC) + timedelta(seconds=i) for i in range(20))
    return SqlAlchemyAnswerLedgerStore(_engine(), clock=lambda: next(ticks))


def _stamp(*, hash_: str = "chunk-a", version: int = 1) -> CorpusChunkStamp:
    return CorpusChunkStamp("chunk-1", "doc-1", hash_, version)


def _record(
    store: SqlAlchemyAnswerLedgerStore,
    owner: str,
    *,
    answer: str,
    stamp: CorpusChunkStamp | None = None,
    config: str = "config-a",
):
    question = store.question(owner)
    assert question is not None
    return store.record(
        question=question,
        answer=answer,
        corpus_stamp=[stamp or _stamp()],
        config_hash=config,
        served_model_id="private-model",
        trace_id=f"trace-{answer}",
    )


def test_pin_is_one_per_owner_and_timelines_are_principal_isolated() -> None:
    store = _store()
    ada = store.pin("user:ada", "What changed?")
    grace = store.pin("user:grace", "What is at risk?")

    with pytest.raises(AnswerQuestionAlreadyPinnedError):
        store.pin("user:ada", "A second question")

    _record(store, ada.owner_id, answer="Ada answer")
    _record(store, grace.owner_id, answer="Grace answer")

    assert store.question("user:ada") == ada
    assert [row.answer for row in store.timeline("user:ada")] == ["Ada answer"]
    assert [row.answer for row in store.timeline("user:grace")] == ["Grace answer"]
    assert store.question("user:unknown") is None


def test_event_round_trip_preserves_verbatim_answer_and_canonical_corpus_stamp() -> None:
    store = _store()
    store.pin("user:ada", "What changed?")
    answer = "Line one.\n\n  Line two stays verbatim.  "
    event = _record(
        store,
        "user:ada",
        answer=answer,
        stamp=CorpusChunkStamp("chunk-z", "doc-z", "f" * 64, 7),
        config="c" * 64,
    )

    loaded = store.timeline("user:ada")
    assert loaded == (event,)
    assert loaded[0].answer == answer
    assert loaded[0].corpus_stamp == (CorpusChunkStamp("chunk-z", "doc-z", "f" * 64, 7),)


@pytest.mark.parametrize(
    ("answer", "stamp", "config", "expected"),
    [
        ("before", _stamp(hash_="changed", version=2), "config-b", "SAME"),
        ("after", _stamp(hash_="changed", version=2), "config-b", "CHANGED-corpus"),
        ("after", _stamp(), "config-b", "CHANGED-config"),
        ("after", _stamp(), "config-a", "CHANGED-unexplained"),
    ],
)
def test_read_time_drift_classification_is_value_discriminating(
    answer: str,
    stamp: CorpusChunkStamp,
    config: str,
    expected: str,
) -> None:
    store = _store()
    store.pin("user:ada", "What changed?")
    previous = _record(store, "user:ada", answer="before")
    current = _record(store, "user:ada", answer=answer, stamp=stamp, config=config)

    assert classify_drift(previous, current) == expected


def test_compensation_helpers_never_remove_another_owners_rows() -> None:
    store = _store()
    ada = store.pin("user:ada", "What changed?")
    grace = store.pin("user:grace", "What is at risk?")
    grace_event = _record(store, grace.owner_id, answer="Private to Grace")

    store.unpin(ada.owner_id, grace.question_id)
    store.remove_event(ada.owner_id, grace_event.event_id)

    assert store.question(grace.owner_id) == grace
    assert store.timeline(grace.owner_id) == (grace_event,)


def test_reask_schedule_is_opt_in_bounded_and_due_on_the_exact_server_boundary() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    engine = _engine()
    store = SqlAlchemyAnswerLedgerStore(engine, clock=lambda: now)
    store.pin("user:ada", "What changed?")
    store.pin("user:grace", "What is at risk?")
    store.set_reask_schedule("user:ada", "hourly")

    now += timedelta(seconds=3599)
    assert store.due_questions(limit=1) == ()
    now += timedelta(seconds=1)
    assert [item.owner_id for item in store.due_questions(limit=1)] == ["user:ada"]

    _record(store, "user:ada", answer="Scheduled answer")
    assert store.due_questions(limit=10) == ()
    now += timedelta(hours=1)
    assert [item.owner_id for item in store.due_questions(limit=10)] == ["user:ada"]

    store.set_reask_schedule("user:grace", "hourly")
    now += timedelta(hours=1)
    assert len(store.due_questions(limit=1)) == 1
    store.set_reask_schedule("user:ada", None)
    assert [item.owner_id for item in store.due_questions(limit=10)] == ["user:grace"]


def test_existing_answer_ledger_schema_is_additively_upgraded_for_schedule() -> None:
    engine = _engine()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE ne_answer_ledger_questions ("
            "owner_id VARCHAR PRIMARY KEY, question_id VARCHAR NOT NULL UNIQUE, "
            "question TEXT NOT NULL, created_at TIMESTAMP NOT NULL)"
        )
    store = SqlAlchemyAnswerLedgerStore(engine)
    pinned = store.pin("user:ada", "What changed?")
    scheduled = store.set_reask_schedule("user:ada", "daily")

    assert scheduled.question_id == pinned.question_id
    assert scheduled.reask_schedule == "daily"
    assert scheduled.reask_scheduled_at is not None


def test_current_authorized_timeline_withholds_answer_after_acl_changes() -> None:
    writer, reader, _continuation = _authority()
    stamp = _admit(writer, "answer-source")

    class _Acl:
        snapshot = CurrentAclSnapshot(
            artifact_id=stamp.artifact_identity.artifact_id,
            snapshot_hash=ACL_A,
            labels=("analysts",),
        )

        def resolve_current_acl(self, stamp, *, current_receipt):
            assert current_receipt.artifact_identity == stamp.artifact_identity
            return self.snapshot

    acl = _Acl()
    evaluator = CurrentSourceDisclosureEvaluator(reader, acl)
    store = _store()
    store.pin("user:ada", "What changed?")
    _record(
        store,
        "user:ada",
        answer="historical answer",
        stamp=CorpusChunkStamp("chunk-1", "doc-1", "f" * 64, 1, stamp),
    )
    principal = Principal(id="user:ada", groups=("analysts",))

    visible = asyncio.run(
        store.timeline_current_authorized(
            "user:ada",
            principal=principal,
            access_context_hash=ACCESS,
            evaluator=evaluator,
        )
    )
    assert visible[0].answer == "historical answer"
    assert visible[0].content_withheld is False

    acl.snapshot = CurrentAclSnapshot(
        artifact_id=stamp.artifact_identity.artifact_id,
        snapshot_hash=ACL_A,
        labels=("another-team",),
    )
    withheld = asyncio.run(
        store.timeline_current_authorized(
            "user:ada",
            principal=principal,
            access_context_hash=ACCESS,
            evaluator=evaluator,
        )
    )
    assert withheld[0].answer == ""
    assert withheld[0].content_withheld is True
    assert withheld[0].corpus_stamp == ()
