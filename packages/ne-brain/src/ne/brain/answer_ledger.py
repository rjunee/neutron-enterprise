# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Durable, principal-owned answer history and read-time drift classification.

The ledger stores the answer exactly as it was served and a canonical stamp for
the exact chunks used to produce it.  Classification is deliberately computed
when the timeline is read: no historical row is rewritten when a newer answer
arrives, and changing classifier code cannot silently mutate retained evidence.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError

from ne.brain.current_source_filter import CurrentSourceDisclosureEvaluator
from ne.contracts.source_authority import SourceAuthorityStamp
from ne.contracts.types import Principal

__all__ = [
    "AnswerEvent",
    "AnswerQuestion",
    "AnswerQuestionAlreadyPinnedError",
    "AnswerQuestionNotFoundError",
    "CorpusChunkStamp",
    "DriftClassification",
    "ReaskSchedule",
    "SqlAlchemyAnswerLedgerStore",
    "classify_drift",
    "ensure_answer_ledger_schema",
]

DriftClassification = Literal["SAME", "CHANGED-corpus", "CHANGED-config", "CHANGED-unexplained"]
ReaskSchedule = Literal["hourly", "daily", "weekly"]

_SCHEDULE_DELTA = {
    "hourly": 60 * 60,
    "daily": 24 * 60 * 60,
    "weekly": 7 * 24 * 60 * 60,
}

_metadata = sa.MetaData()

_questions = sa.Table(
    "ne_answer_ledger_questions",
    _metadata,
    sa.Column("owner_id", sa.String, primary_key=True),
    sa.Column("question_id", sa.String, nullable=False, unique=True),
    sa.Column("question", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("reask_schedule", sa.String, nullable=True),
    sa.Column("reask_scheduled_at", sa.DateTime(timezone=True), nullable=True),
)

_events = sa.Table(
    "ne_answer_ledger_events",
    _metadata,
    sa.Column("event_id", sa.String, primary_key=True),
    sa.Column("question_id", sa.String, nullable=False),
    sa.Column("owner_id", sa.String, nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("answer", sa.Text, nullable=False),
    sa.Column("corpus_stamp", sa.JSON, nullable=False),
    sa.Column("config_hash", sa.String, nullable=False),
    sa.Column("served_model_id", sa.String, nullable=False),
    sa.Column("trace_id", sa.String, nullable=False),
    sa.Index("ix_ne_answer_ledger_owner_time", "owner_id", "occurred_at", "event_id"),
)


def ensure_answer_ledger_schema(engine: sa.Engine) -> None:
    """Create or additively upgrade the two Answer Ledger tables."""

    _metadata.create_all(engine)
    columns = {item["name"] for item in sa.inspect(engine).get_columns(_questions.name)}
    additions = {
        "reask_schedule": "VARCHAR NULL",
        "reask_scheduled_at": "TIMESTAMP NULL",
    }
    with engine.begin() as conn:
        for name, ddl in additions.items():
            if engine.dialect.name == "postgresql":
                # Multiple api/worker pods may boot the additive upgrade together.
                conn.exec_driver_sql(
                    f"ALTER TABLE {_questions.name} ADD COLUMN IF NOT EXISTS {name} {ddl}"
                )
            elif name not in columns:
                conn.exec_driver_sql(f"ALTER TABLE {_questions.name} ADD COLUMN {name} {ddl}")


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class CorpusChunkStamp:
    chunk_id: str
    document_id: str
    content_hash: str
    document_version: int
    authority_stamp: SourceAuthorityStamp | None = None


@dataclass(frozen=True)
class AnswerQuestion:
    question_id: str
    owner_id: str
    question: str
    created_at: datetime
    reask_schedule: ReaskSchedule | None = None
    reask_scheduled_at: datetime | None = None


@dataclass(frozen=True)
class AnswerEvent:
    event_id: str
    question_id: str
    owner_id: str
    occurred_at: datetime
    answer: str
    corpus_stamp: tuple[CorpusChunkStamp, ...]
    config_hash: str
    served_model_id: str
    trace_id: str
    content_withheld: bool = False


class AnswerQuestionAlreadyPinnedError(RuntimeError):
    """The principal already owns the one supported pinned question."""


class AnswerQuestionNotFoundError(RuntimeError):
    """The principal has not pinned a question, or does not own the supplied id."""


def classify_drift(previous: AnswerEvent, current: AnswerEvent) -> DriftClassification:
    """Classify answer drift with deterministic corpus-before-config precedence.

    SAME describes the served answer, even if its inputs changed.  When the
    answer differs and both inputs changed, corpus wins as the more immediate
    evidence cause; callers still receive independent corpus/config booleans so
    the second change is never hidden by the single classification label.
    """

    if current.answer == previous.answer:
        return "SAME"
    if current.corpus_stamp != previous.corpus_stamp:
        return "CHANGED-corpus"
    if current.config_hash != previous.config_hash:
        return "CHANGED-config"
    return "CHANGED-unexplained"


class SqlAlchemyAnswerLedgerStore:
    """Principal-isolated Answer Ledger over an injected SQLAlchemy engine."""

    def __init__(self, engine: sa.Engine, *, clock: Callable[[], datetime] | None = None) -> None:
        self.engine = engine
        self._clock = clock or (lambda: datetime.now(UTC))
        ensure_answer_ledger_schema(engine)

    def pin(self, owner_id: str, question: str) -> AnswerQuestion:
        normalized = question.strip()
        if not normalized:
            raise ValueError("question must not be blank")
        row = AnswerQuestion(uuid.uuid4().hex, owner_id, normalized, _utc(self._clock()))
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    _questions.insert().values(
                        owner_id=row.owner_id,
                        question_id=row.question_id,
                        question=row.question,
                        created_at=row.created_at,
                    )
                )
        except IntegrityError as exc:
            raise AnswerQuestionAlreadyPinnedError("one question is already pinned") from exc
        return row

    def unpin(self, owner_id: str, question_id: str) -> None:
        """Compensate a failed audit append; never deletes another owner's row."""

        with self.engine.begin() as conn:
            conn.execute(
                _questions.delete().where(
                    _questions.c.owner_id == owner_id,
                    _questions.c.question_id == question_id,
                )
            )

    def question(self, owner_id: str) -> AnswerQuestion | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(sa.select(_questions).where(_questions.c.owner_id == owner_id))
                .mappings()
                .first()
            )
        if row is None:
            return None
        return self._question_from_row(row)

    @staticmethod
    def _question_from_row(row: RowMapping) -> AnswerQuestion:
        scheduled_at = cast(datetime | None, row["reask_scheduled_at"])
        return AnswerQuestion(
            question_id=str(row["question_id"]),
            owner_id=str(row["owner_id"]),
            question=str(row["question"]),
            created_at=_utc(cast(datetime, row["created_at"])),
            reask_schedule=cast(ReaskSchedule | None, row["reask_schedule"]),
            reask_scheduled_at=_utc(scheduled_at) if scheduled_at is not None else None,
        )

    def set_reask_schedule(self, owner_id: str, schedule: ReaskSchedule | None) -> AnswerQuestion:
        """Set or clear one owner's opt-in schedule using authoritative store time."""

        if schedule is not None and schedule not in _SCHEDULE_DELTA:
            raise ValueError(f"unsupported re-ask schedule {schedule!r}")
        scheduled_at = _utc(self._clock()) if schedule is not None else None
        with self.engine.begin() as conn:
            updated = conn.execute(
                _questions.update()
                .where(_questions.c.owner_id == owner_id)
                .values(reask_schedule=schedule, reask_scheduled_at=scheduled_at)
            )
            if updated.rowcount != 1:
                raise AnswerQuestionNotFoundError("pinned question not found")
        question = self.question(owner_id)
        assert question is not None
        return question

    def restore_reask_schedule(self, question: AnswerQuestion) -> None:
        """Restore exact prior schedule state after a failed audit append."""

        with self.engine.begin() as conn:
            conn.execute(
                _questions.update()
                .where(
                    _questions.c.owner_id == question.owner_id,
                    _questions.c.question_id == question.question_id,
                )
                .values(
                    reask_schedule=question.reask_schedule,
                    reask_scheduled_at=question.reask_scheduled_at,
                )
            )

    def due_questions(self, *, limit: int) -> tuple[AnswerQuestion, ...]:
        """Return at most ``limit`` opted-in questions due at authoritative store time."""

        if limit < 1:
            raise ValueError("limit must be >= 1")
        now = _utc(self._clock())
        last_event = (
            sa.select(
                _events.c.owner_id,
                sa.func.max(_events.c.occurred_at).label("last_occurred_at"),
            )
            .group_by(_events.c.owner_id)
            .subquery()
        )
        stmt = (
            sa.select(_questions, last_event.c.last_occurred_at)
            .outerjoin(last_event, last_event.c.owner_id == _questions.c.owner_id)
            .where(_questions.c.reask_schedule.is_not(None))
            .order_by(_questions.c.reask_scheduled_at, _questions.c.owner_id)
        )
        with self.engine.connect() as conn:
            rows = list(conn.execute(stmt).mappings())
        due: list[AnswerQuestion] = []
        for row in rows:
            question = self._question_from_row(row)
            schedule = question.reask_schedule
            scheduled_at = question.reask_scheduled_at
            if schedule is None or scheduled_at is None:
                continue
            last = cast(datetime | None, row["last_occurred_at"])
            anchor = max(scheduled_at, _utc(last)) if last is not None else scheduled_at
            if (now - anchor).total_seconds() >= _SCHEDULE_DELTA[schedule]:
                due.append(question)
                if len(due) == limit:
                    break
        return tuple(due)

    def record(
        self,
        *,
        question: AnswerQuestion,
        answer: str,
        corpus_stamp: Sequence[CorpusChunkStamp],
        config_hash: str,
        served_model_id: str,
        trace_id: str,
    ) -> AnswerEvent:
        stamp = tuple(
            sorted(
                corpus_stamp,
                key=lambda item: (item.document_id, item.document_version, item.chunk_id),
            )
        )
        if not stamp:
            raise ValueError("corpus stamp must not be empty")
        event = AnswerEvent(
            event_id=uuid.uuid4().hex,
            question_id=question.question_id,
            owner_id=question.owner_id,
            occurred_at=_utc(self._clock()),
            answer=answer,
            corpus_stamp=stamp,
            config_hash=config_hash,
            served_model_id=served_model_id,
            trace_id=trace_id,
        )
        with self.engine.begin() as conn:
            owner = conn.scalar(
                sa.select(_questions.c.owner_id).where(
                    _questions.c.question_id == question.question_id,
                    _questions.c.owner_id == question.owner_id,
                )
            )
            if owner is None:
                raise AnswerQuestionNotFoundError("pinned question not found")
            conn.execute(
                _events.insert().values(
                    event_id=event.event_id,
                    question_id=event.question_id,
                    owner_id=event.owner_id,
                    occurred_at=event.occurred_at,
                    answer=event.answer,
                    corpus_stamp=[
                        {
                            "chunk_id": item.chunk_id,
                            "document_id": item.document_id,
                            "content_hash": item.content_hash,
                            "document_version": item.document_version,
                            "authority_stamp": (
                                None
                                if item.authority_stamp is None
                                else item.authority_stamp.model_dump(mode="json")
                            ),
                        }
                        for item in stamp
                    ],
                    config_hash=event.config_hash,
                    served_model_id=event.served_model_id,
                    trace_id=event.trace_id,
                )
            )
        return event

    def remove_event(self, owner_id: str, event_id: str) -> None:
        """Compensate a failed audit append; owner predicate prevents cross-delete."""

        with self.engine.begin() as conn:
            conn.execute(
                _events.delete().where(
                    _events.c.owner_id == owner_id, _events.c.event_id == event_id
                )
            )

    def timeline(self, owner_id: str) -> tuple[AnswerEvent, ...]:
        with self.engine.connect() as conn:
            rows = list(
                conn.execute(
                    sa.select(_events)
                    .where(_events.c.owner_id == owner_id)
                    .order_by(_events.c.occurred_at, _events.c.event_id)
                ).mappings()
            )
        return tuple(self._event(row) for row in rows)

    async def timeline_current_authorized(
        self,
        owner_id: str,
        *,
        principal: Principal,
        access_context_hash: str,
        evaluator: CurrentSourceDisclosureEvaluator,
    ) -> tuple[AnswerEvent, ...]:
        """Withhold an entire historical answer unless every contributor is current.

        Missing legacy stamps are not source-free proof. They fail closed on this
        explicit current-authorized surface while the ordinary evidence timeline
        remains available to trusted internal callers.
        """

        result: list[AnswerEvent] = []
        for event in self.timeline(owner_id):
            authorized = bool(event.corpus_stamp)
            for contributor in event.corpus_stamp:
                stamp = contributor.authority_stamp
                if stamp is None:
                    authorized = False
                    break
                decision = await evaluator.evaluate(
                    stamp,
                    principal=principal,
                    access_context_hash=access_context_hash,
                )
                if not decision.authorized:
                    authorized = False
                    break
            if authorized:
                result.append(event)
            else:
                result.append(
                    AnswerEvent(
                        event_id=event.event_id,
                        question_id=event.question_id,
                        owner_id=event.owner_id,
                        occurred_at=event.occurred_at,
                        answer="",
                        corpus_stamp=(),
                        config_hash=event.config_hash,
                        served_model_id=event.served_model_id,
                        trace_id=event.trace_id,
                        content_withheld=True,
                    )
                )
        return tuple(result)

    @staticmethod
    def _event(row: sa.RowMapping) -> AnswerEvent:
        stamp = tuple(
            CorpusChunkStamp(
                chunk_id=str(item["chunk_id"]),
                document_id=str(item["document_id"]),
                content_hash=str(item["content_hash"]),
                document_version=int(item["document_version"]),
                authority_stamp=(
                    None
                    if item.get("authority_stamp") is None
                    else SourceAuthorityStamp.model_validate(item["authority_stamp"])
                ),
            )
            for item in (row["corpus_stamp"] or ())
        )
        return AnswerEvent(
            event_id=str(row["event_id"]),
            question_id=str(row["question_id"]),
            owner_id=str(row["owner_id"]),
            occurred_at=_utc(row["occurred_at"]),
            answer=str(row["answer"]),
            corpus_stamp=stamp,
            config_hash=str(row["config_hash"]),
            served_model_id=str(row["served_model_id"]),
            trace_id=str(row["trace_id"]),
        )
