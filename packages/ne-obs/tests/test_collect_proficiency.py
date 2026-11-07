# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""collect_proficiency against real EvalRunStore + SqlAlchemyCorrectionSignalStore (NE-068)."""

from __future__ import annotations

from datetime import UTC, datetime

from ne.eval.store import EvalRunStore
from ne.flywheel import SqlAlchemyCorrectionSignalStore
from ne.obs.collect import collect_proficiency
from ne.obs.kpi import ReportPeriod
from ne.schema import CorrectionKind

from .conftest import seed_eval_run, seed_signal

JUNE = ReportPeriod(year=2026, month=6)


def test_eval_pass_rate_only_over_runs_in_window(
    eval_store: EvalRunStore, signal_store: SqlAlchemyCorrectionSignalStore, frozen_config: str
) -> None:
    seed_eval_run(
        eval_store, run_id="in-1", created_at=datetime(2026, 6, 5, tzinfo=UTC), passed=True
    )
    seed_eval_run(
        eval_store, run_id="in-2", created_at=datetime(2026, 6, 6, tzinfo=UTC), passed=False
    )
    # Out-of-window runs, must be excluded.
    seed_eval_run(
        eval_store, run_id="out-1", created_at=datetime(2026, 5, 31, tzinfo=UTC), passed=True
    )
    seed_eval_run(
        eval_store, run_id="out-2", created_at=datetime(2026, 7, 1, tzinfo=UTC), passed=True
    )

    metrics = collect_proficiency(eval_store, signal_store, JUNE, agent_turns=10)

    assert metrics.eval_runs == 2
    assert metrics.eval_pass_rate == 0.5


def test_eval_pass_rate_none_when_zero_runs(
    eval_store: EvalRunStore, signal_store: SqlAlchemyCorrectionSignalStore, frozen_config: str
) -> None:
    metrics = collect_proficiency(eval_store, signal_store, JUNE, agent_turns=10)

    assert metrics.eval_runs == 0
    assert metrics.eval_pass_rate is None


def test_cursor_pagination_counts_every_signal_exactly_once(
    eval_store: EvalRunStore, signal_store: SqlAlchemyCorrectionSignalStore, frozen_config: str
) -> None:
    for i in range(7):
        seed_signal(
            signal_store,
            kind=CorrectionKind.thumbs_down,
            occurred_at=datetime(2026, 6, 1 + i, tzinfo=UTC),
        )

    metrics = collect_proficiency(eval_store, signal_store, JUNE, agent_turns=100, page_size=3)

    assert metrics.correction_signals_total == 7
    assert metrics.negative_signals == 7


def test_kind_classification(
    eval_store: EvalRunStore, signal_store: SqlAlchemyCorrectionSignalStore, frozen_config: str
) -> None:
    seed_signal(
        signal_store, kind=CorrectionKind.thumbs_up, occurred_at=datetime(2026, 6, 1, tzinfo=UTC)
    )
    seed_signal(
        signal_store, kind=CorrectionKind.thumbs_down, occurred_at=datetime(2026, 6, 2, tzinfo=UTC)
    )
    seed_signal(
        signal_store, kind=CorrectionKind.wrong_source, occurred_at=datetime(2026, 6, 3, tzinfo=UTC)
    )
    seed_signal(
        signal_store, kind=CorrectionKind.re_query, occurred_at=datetime(2026, 6, 4, tzinfo=UTC)
    )
    seed_signal(
        signal_store,
        kind=CorrectionKind.approval_rejection,
        occurred_at=datetime(2026, 6, 5, tzinfo=UTC),
    )
    seed_signal(
        signal_store,
        kind=CorrectionKind.edit_distance,
        occurred_at=datetime(2026, 6, 6, tzinfo=UTC),
        value=0.3,
    )

    metrics = collect_proficiency(eval_store, signal_store, JUNE, agent_turns=100)

    assert metrics.positive_signals == 1
    assert metrics.negative_signals == 4
    assert metrics.correction_signals_total == 6


def test_negative_signal_rate_none_when_zero_agent_turns(
    eval_store: EvalRunStore, signal_store: SqlAlchemyCorrectionSignalStore, frozen_config: str
) -> None:
    seed_signal(
        signal_store, kind=CorrectionKind.thumbs_down, occurred_at=datetime(2026, 6, 1, tzinfo=UTC)
    )

    metrics = collect_proficiency(eval_store, signal_store, JUNE, agent_turns=0)

    assert metrics.negative_signal_rate is None


def test_negative_signal_rate_computed(
    eval_store: EvalRunStore, signal_store: SqlAlchemyCorrectionSignalStore, frozen_config: str
) -> None:
    seed_signal(
        signal_store, kind=CorrectionKind.thumbs_down, occurred_at=datetime(2026, 6, 1, tzinfo=UTC)
    )
    seed_signal(
        signal_store, kind=CorrectionKind.wrong_source, occurred_at=datetime(2026, 6, 2, tzinfo=UTC)
    )

    metrics = collect_proficiency(eval_store, signal_store, JUNE, agent_turns=10)

    assert metrics.negative_signal_rate == 0.2
