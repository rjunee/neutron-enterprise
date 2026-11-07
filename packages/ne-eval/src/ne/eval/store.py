# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Append-only eval-run store (SQLAlchemy Core; SQLite in tests, Postgres live).

Every eval run is recorded with its ``config_hash`` (SPEC §4 replayability) plus
the ``golden_set_hash`` and per-rubric content hashes, so any run is fully
reproducible. The store is append-only *by construction*: :meth:`EvalRunStore.append`
is the only write surface — there is no update or delete API. Mirrors the
SQLAlchemy-Core pattern of ``ne.audit.store``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData()

eval_runs = sa.Table(
    "ne_eval_runs",
    metadata,
    sa.Column("run_id", sa.String, primary_key=True),
    sa.Column("suite", sa.String, nullable=False, index=True),
    sa.Column("config_hash", sa.String, nullable=False, index=True),
    sa.Column("golden_set_hash", sa.String, nullable=False),
    sa.Column("judge_model_id", sa.String, nullable=True),
    sa.Column("judge_family", sa.String, nullable=True),
    sa.Column("rubric_versions", sa.Text, nullable=False),
    sa.Column("metrics", sa.Text, nullable=False),
    sa.Column("thresholds", sa.Text, nullable=False),
    sa.Column("statuses", sa.Text, nullable=False),
    sa.Column("judge_error_rate", sa.Float, nullable=False),
    sa.Column("passed", sa.Boolean, nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True).with_variant(postgresql.TIMESTAMP(timezone=True), "postgresql"),
        nullable=False,
    ),
)


class EvalRunRecord(BaseModel):
    """A frozen mirror of one ``ne_eval_runs`` row."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    suite: str
    config_hash: str
    golden_set_hash: str
    judge_model_id: str | None
    judge_family: str | None
    rubric_versions: dict[str, Any]
    metrics: dict[str, float]
    thresholds: dict[str, float]
    statuses: dict[str, str]
    judge_error_rate: float
    passed: bool
    created_at: datetime


def ensure_eval_schema(engine: sa.Engine) -> None:
    """Create the eval-run table. Idempotent (``create_all`` skips existing tables)."""
    metadata.create_all(engine)


def _record_from_row(row: sa.Row[Any]) -> EvalRunRecord:
    return EvalRunRecord(
        run_id=row.run_id,
        suite=row.suite,
        config_hash=row.config_hash,
        golden_set_hash=row.golden_set_hash,
        judge_model_id=row.judge_model_id,
        judge_family=row.judge_family,
        rubric_versions=json.loads(row.rubric_versions),
        metrics=json.loads(row.metrics),
        thresholds=json.loads(row.thresholds),
        statuses=json.loads(row.statuses),
        judge_error_rate=row.judge_error_rate,
        passed=bool(row.passed),
        created_at=row.created_at,
    )


class EvalRunStore:
    """Append-only store for eval runs. No update/delete surface exists."""

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    def append(self, record: EvalRunRecord) -> EvalRunRecord:
        """Insert ``record`` and return it."""
        with self._engine.begin() as conn:
            conn.execute(
                eval_runs.insert().values(
                    run_id=record.run_id,
                    suite=record.suite,
                    config_hash=record.config_hash,
                    golden_set_hash=record.golden_set_hash,
                    judge_model_id=record.judge_model_id,
                    judge_family=record.judge_family,
                    rubric_versions=json.dumps(record.rubric_versions, sort_keys=True),
                    metrics=json.dumps(record.metrics, sort_keys=True),
                    thresholds=json.dumps(record.thresholds, sort_keys=True),
                    statuses=json.dumps(record.statuses, sort_keys=True),
                    judge_error_rate=record.judge_error_rate,
                    passed=record.passed,
                    created_at=record.created_at,
                )
            )
        return record

    def get_run(self, run_id: str) -> EvalRunRecord | None:
        """Return the single run with ``run_id`` (the PK), or ``None`` if absent.

        A direct one-row lookup on the ``run_id`` primary key — the store row is
        the tamper-resistant anchor a fail-closed eval gate re-verifies against a
        payload-carried eval ref (a forged ref pointing at no stored run fails).
        """
        stmt = sa.select(eval_runs).where(eval_runs.c.run_id == run_id)
        with self._engine.begin() as conn:
            row = conn.execute(stmt).one_or_none()
        return _record_from_row(cast(sa.Row[Any], row)) if row is not None else None

    def list_runs(self, *, suite: str | None = None, limit: int = 100) -> list[EvalRunRecord]:
        """Return runs newest-first, optionally filtered by ``suite``."""
        stmt = sa.select(eval_runs)
        if suite is not None:
            stmt = stmt.where(eval_runs.c.suite == suite)
        stmt = stmt.order_by(eval_runs.c.created_at.desc(), eval_runs.c.run_id.desc()).limit(limit)
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).all()
        return [_record_from_row(cast(sa.Row[Any], row)) for row in rows]
