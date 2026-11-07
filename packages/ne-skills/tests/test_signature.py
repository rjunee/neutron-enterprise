# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Workflow-signature tests (NE-SKILL-1b): order-sensitive, stable, names-only."""

from __future__ import annotations

from datetime import UTC, datetime

from ne.contracts.skills import WorkflowSnapshot, WorkflowStep
from ne.skills.signature import workflow_signature


def _snap(actions: tuple[str, ...]) -> WorkflowSnapshot:
    now = datetime.now(UTC)
    return WorkflowSnapshot(
        snapshot_id="s",
        substrate_id="pydantic-ai",
        intent="subagent:x",
        steps=tuple(WorkflowStep(action=a, outcome="ok") for a in actions),
        succeeded=True,
        principal_id="user:a",
        session_id="sess",
        started_at=now,
        finished_at=now,
    )


def test_signature_is_order_sensitive() -> None:
    assert workflow_signature(_snap(("a", "b"))) != workflow_signature(_snap(("b", "a")))


def test_signature_is_stable_across_runs() -> None:
    snap = _snap(("a", "b", "c"))
    assert workflow_signature(snap) == workflow_signature(snap)


def test_signature_distinct_from_empty() -> None:
    assert workflow_signature(_snap(("a",))) != workflow_signature(_snap(()))


def test_signature_ignores_outcome_and_names_only() -> None:
    # Two snapshots with the same actions but different outcomes hash identically:
    # only the ordered action NAMES enter the signature.
    now = datetime.now(UTC)

    def _with_outcomes(outcomes: tuple[str, ...]) -> WorkflowSnapshot:
        return WorkflowSnapshot(
            snapshot_id="s",
            substrate_id="pydantic-ai",
            intent="subagent:x",
            steps=tuple(WorkflowStep(action="a", outcome=o) for o in outcomes),
            succeeded=True,
            principal_id="user:a",
            session_id="sess",
            started_at=now,
            finished_at=now,
        )

    assert workflow_signature(_with_outcomes(("ok", "ok"))) == workflow_signature(
        _with_outcomes(("fail", "ok"))
    )
