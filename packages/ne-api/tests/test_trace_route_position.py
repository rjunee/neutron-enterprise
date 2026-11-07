# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The trace surface carries WHICH candidate served, and after what.

The gateway records ``route_position`` (``primary``/``fallback``) and, when a
fallback served, ``fallback_from`` (the declared names of the candidates that
were passed over) on every per-call success record. This module proves the
assembler lifts BOTH onto the typed ``model_call`` span, and — the load-bearing
half — that a record written before those keys existed stays ABSENT rather than
being defaulted to ``primary``: "the first choice served" is a verdict, and the
assembler may only repeat one the emitter actually made.

It also proves the per-model aggregate names the TASKS each model served, so a
reader can see the run's task→model routing without opening every span.
"""

from __future__ import annotations

import uuid

from ne.events import new_audit_event
from ne.schema import AuditOutcome

from .conftest import AppBuilder, get_state, run


def _seed(h, tid: str) -> None:
    """Three model calls on one run: a primary, a fallback that served after
    the frontier candidate did not, and a PRE-KEY record (the rolling-upgrade
    row that predates route_position entirely)."""
    state = get_state(h.app)

    async def _append() -> None:
        await state.sink.append(
            new_audit_event(
                action="ne.gateway.complete",
                resource="cpu/qwen3-1.7b",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail="task=triage lane=private-vllm model=aux-model",
                trace_id=tid,
                provenance={
                    "task": "triage",
                    "lane": "private-vllm",
                    "model_name": "aux-model",
                    "route_position": "primary",
                },
            )
        )
        await state.sink.append(
            new_audit_event(
                action="ne.gateway.complete",
                resource="cpu/qwen3-1.7b",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail="task=reason lane=private-vllm model=aux-model",
                trace_id=tid,
                provenance={
                    "task": "reason",
                    "lane": "private-vllm",
                    "requested_model": "private-default",
                    "model_name": "aux-model",
                    "route_position": "fallback",
                    "fallback_from": "private-default",
                },
            )
        )
        await state.sink.append(
            new_audit_event(
                action="ne.gateway.complete",
                resource="anthropic/claude",
                outcome=AuditOutcome.success,
                actor="user:ada",
                detail="task=adjudicate lane=frontier-api model=frontier-best",
                trace_id=tid,
                provenance={
                    "task": "adjudicate",
                    "lane": "frontier-api",
                    "model_name": "frontier-best",
                },
            )
        )

    run(_append())


def _body(h, tid: str) -> dict:
    async def _t() -> dict:
        async with h.client() as client:
            r = await client.get(
                f"/api/v1/trace/{tid}", headers=h.bearer("user:ops", groups=["ops"])
            )
            assert r.status_code == 200
            return r.json()

    return run(_t())


def test_primary_call_serves_route_position_primary_and_no_fallback_from(
    make_app: AppBuilder,
) -> None:
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed(h, tid)

    call = _body(h, tid)["nodes"][0]["model_call"]
    assert call["task"] == "triage"
    assert call["model_name"] == "aux-model"
    assert call["route_position"] == "primary"
    assert call["fallback_from"] is None


def test_fallback_call_serves_the_position_and_the_candidate_that_was_passed_over(
    make_app: AppBuilder,
) -> None:
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed(h, tid)

    call = _body(h, tid)["nodes"][1]["model_call"]
    # The model that ANSWERED, and the one the route asked for first.
    assert call["model_name"] == "aux-model"
    assert call["requested_model"] == "private-default"
    assert call["route_position"] == "fallback"
    assert call["fallback_from"] == "private-default"


def test_pre_key_record_serves_null_route_position_never_primary(make_app: AppBuilder) -> None:
    """A record from before the keys existed says nothing about routing.

    Defaulting it to ``primary`` would manufacture the exact reassurance this
    field exists to withhold — that the route's first choice served.
    """
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed(h, tid)

    call = _body(h, tid)["nodes"][2]["model_call"]
    assert call["model_name"] == "frontier-best"
    assert call["requested_model"] is None
    assert call["route_position"] is None
    assert call["fallback_from"] is None


def test_model_aggregate_names_the_tasks_that_model_served(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    _seed(h, tid)

    models = {row["model"]: row for row in _body(h, tid)["models"]}
    # One model served two different tasks; the aggregate says which.
    assert models["aux-model"]["tasks"] == ["triage", "reason"]
    assert models["frontier-best"]["tasks"] == ["adjudicate"]


def test_model_aggregate_task_list_deduplicates_and_omits_untasked_calls(
    make_app: AppBuilder,
) -> None:
    h = make_app(ops_roles=("ops",))
    tid = uuid.uuid4().hex
    state = get_state(h.app)

    async def _append() -> None:
        for provenance in (
            {"task": "triage", "lane": "private-vllm", "model_name": "aux-model"},
            {"task": "triage", "lane": "private-vllm", "model_name": "aux-model"},
            {"lane": "private-vllm", "model_name": "aux-model"},
        ):
            await state.sink.append(
                new_audit_event(
                    action="ne.gateway.complete",
                    resource="cpu/qwen3-1.7b",
                    outcome=AuditOutcome.success,
                    actor="user:ada",
                    detail="call",
                    trace_id=tid,
                    provenance=provenance,
                )
            )

    run(_append())

    models = _body(h, tid)["models"]
    assert len(models) == 1
    # Three calls, one named task: the list is a SET of served tasks, and the
    # call whose task the record never stated contributes no name (never "").
    assert models[0]["calls"] == 3
    assert models[0]["tasks"] == ["triage"]
