# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Composed API proof for the server-authoritative BD-15 model catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ne.api.model_selection import MODEL_OBSERVATION_ACTION
from ne.contracts.types import Lane
from ne.events import new_audit_event
from ne.gateway.config import ModelDef, TaskRoute
from ne.schema import AuditOutcome

from .conftest import AppBuilder, ScriptedGateway, get_state, run, text_completion

PRIVATE_LIVE = ModelDef(
    name="private-live",
    litellm_model="hosted_vllm/acme/private-live",
    provider="vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://private-live.test/v1",
    context_window=32768,
)
PRIVATE_CONFIGURED_ONLY = ModelDef(
    name="private-configured-only",
    litellm_model="hosted_vllm/acme/private-configured-only",
    provider="vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://private-configured-only.test/v1",
    context_window=32768,
)
FRONTIER = ModelDef(
    name="frontier-live",
    litellm_model="anthropic/frontier-live",
    provider="anthropic",
    lane=Lane.FRONTIER_API,
    context_window=32768,
)
TASKS = {
    "agent": TaskRoute(model=PRIVATE_LIVE.name),
    "reason": TaskRoute(model=PRIVATE_LIVE.name),
}


@dataclass(frozen=True)
class _ServingRow:
    model_id: str
    lane: str
    state: str
    checked_at: datetime | None
    serves_configured_model: bool | None


class _ServingProbe:
    def __init__(self, rows: list[_ServingRow] | None = None, *, failure: Exception | None = None):
        self.rows = list(rows or [])
        self.failure = failure
        self.seen: list[tuple[str, ...]] = []

    async def observe(
        self,
        models: Any,
        *,
        force_refresh: bool = False,
    ) -> list[_ServingRow]:
        del force_refresh
        self.seen.append(tuple(model.name for model in models))
        if self.failure is not None:
            raise self.failure
        return list(self.rows)


def _fresh_private_row() -> _ServingRow:
    return _ServingRow(
        model_id=PRIVATE_LIVE.name,
        lane=PRIVATE_LIVE.lane.value,
        state="ok",
        checked_at=datetime.now(UTC),
        serves_configured_model=True,
    )


def test_choices_are_server_mapped_and_list_configured_models_without_probing(
    make_app: AppBuilder,
) -> None:
    """The picker's list is CONFIGURATION; it is not a claim about liveness.

    This test deliberately REPLACES the former rule it grew from — that a
    configured model with no current live evidence was withheld from the list.
    That rule put a vLLM probe and a full audit-chain verification on the Ask
    page's critical path (seconds per load, re-polled every 30s) and expressed
    unavailability as a silently shorter dropdown, which told the user nothing
    about WHICH model was missing or why.

    The governance property was moved, not dropped. Its replacement half is
    ``test_configured_but_unevidenced_model_is_refused_by_name_at_submit``:
    the very model this test proves is LISTED is proven there to be refused by
    name at submit, with no substitution. Read the two together — neither is
    the whole contract.
    """
    probe = _ServingProbe([_fresh_private_row()])
    h = make_app(
        gateway_models=[PRIVATE_LIVE, PRIVATE_CONFIGURED_ONLY],
        gateway_tasks=TASKS,
        serving_probe=probe,
    )
    headers = h.bearer(department="engineering")

    async def _request() -> Any:
        async with h.client() as client:
            return await client.get("/api/v1/models/choices/ask", headers=headers)

    response = run(_request())
    assert response.status_code == 200, response.text
    # Both configured models are offered, and NEITHER carries an evidence claim:
    # nothing was measured, so nothing is reported as observed.
    assert response.json() == {
        "surface": "ask",
        "task": "agent",
        "effective_default": PRIVATE_LIVE.name,
        "choices": [
            {
                "model": PRIVATE_LIVE.name,
                "provider": "vllm",
                "lane": "private-vllm",
                "evidence": None,
                "observed_at": None,
            },
            {
                "model": PRIVATE_CONFIGURED_ONLY.name,
                "provider": "vllm",
                "lane": "private-vllm",
                "evidence": None,
                "observed_at": None,
            },
        ],
    }
    # The point of the change: the read costs NO liveness probe at all. This is
    # the assertion that fails if evidence gathering creeps back onto the path.
    assert probe.seen == []


def test_configured_but_unevidenced_model_is_refused_by_name_at_submit(
    make_app: AppBuilder,
) -> None:
    """The other half of the contract above: the run is where honesty lands.

    A model the picker offered but that is not actually serving must fail the
    RUN loudly and by name, and must never be quietly swapped for one that is
    serving — a run that claimed a model it did not use would be worse than a
    slow dropdown.
    """
    gateway = ScriptedGateway([text_completion("EVIDENCED-ANSWER")])
    h = make_app(
        gateway=gateway,
        gateway_models=[PRIVATE_LIVE, PRIVATE_CONFIGURED_ONLY],
        gateway_tasks=TASKS,
        serving_probe=_ServingProbe([_fresh_private_row()]),
    )
    headers = h.bearer("user:ada", department="engineering")

    async def _drive() -> tuple[Any, Any, Any, list[str | None]]:
        async with h.client() as client:
            listed = await client.get("/api/v1/models/choices/ask", headers=headers)
            opened = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=headers,
            )
            assert opened.status_code == 200, opened.text
            endpoint = f"/api/v1/chat/sessions/{opened.json()['session_id']}/messages"
            refused = await client.post(
                endpoint,
                json={"prompt": "must not run", "requested_model": PRIVATE_CONFIGURED_ONLY.name},
                headers=headers,
            )
            # Snapshot BEFORE the second turn: taken afterwards, the successful
            # turn's own request would mask a substitution by the refused one.
            after_refusal = [request.requested_model for request in gateway.requests]
            accepted = await client.post(
                endpoint,
                json={"prompt": "may run", "requested_model": PRIVATE_LIVE.name},
                headers=headers,
            )
        return listed, refused, accepted, after_refusal

    listed, refused, accepted, after_refusal = run(_drive())

    # It really was on offer — this test and the listing test share one model.
    assert PRIVATE_CONFIGURED_ONLY.name in [row["model"] for row in listed.json()["choices"]]
    # Loud, and it NAMES what was unavailable rather than refusing anonymously.
    assert refused.status_code == 409, refused.text
    assert refused.json() == {
        "detail": f"requested model is unavailable: {PRIVATE_CONFIGURED_ONLY.name}"
    }
    # No substitution: the refused turn reached no model at all.
    assert after_refusal == []
    # An evidenced model still runs, on exactly the model that was asked for.
    assert accepted.status_code == 200, accepted.text
    assert b"EVIDENCED-ANSWER" in accepted.content
    assert [request.requested_model for request in gateway.requests] == [PRIVATE_LIVE.name]


def test_bare_global_air_gap_removes_frontier_but_keeps_current_private(
    make_app: AppBuilder, monkeypatch: Any
) -> None:
    monkeypatch.delenv("AIR_GAP", raising=False)
    probe = _ServingProbe([_fresh_private_row()])
    h = make_app(
        gateway_models=[PRIVATE_LIVE, FRONTIER],
        gateway_tasks=TASKS,
        serving_probe=probe,
    )
    state = get_state(h.app)
    observed_at = datetime.now(UTC)
    run(
        state.sink.append(
            new_audit_event(
                action=MODEL_OBSERVATION_ACTION,
                resource=FRONTIER.litellm_model,
                outcome=AuditOutcome.success,
                actor="ne-gateway",
                trace_id="frontier-warmup-trace",
                config_hash=state.config_hash,
                occurred_at=observed_at,
                provenance={
                    "task": "agent",
                    "model_name": FRONTIER.name,
                    "lane": FRONTIER.lane.value,
                    "operation": "complete",
                },
            )
        )
    )
    headers = h.bearer(department="engineering")

    async def _request() -> tuple[Any, Any]:
        async with h.client() as client:
            before = await client.get("/api/v1/models/choices/ask", headers=headers)
            monkeypatch.setenv("AIR_GAP", "true")
            after = await client.get("/api/v1/models/choices/ask", headers=headers)
            return before, after

    before, after = run(_request())
    assert before.status_code == 200, before.text
    assert [row["model"] for row in before.json()["choices"]] == [
        PRIVATE_LIVE.name,
        FRONTIER.name,
    ]
    assert after.status_code == 200, after.text
    assert [row["model"] for row in after.json()["choices"]] == [PRIVATE_LIVE.name]


def test_evidence_failure_is_a_typed_submit_503_and_leaves_the_listing_alone(
    make_app: AppBuilder,
) -> None:
    """A broken serving probe now fails the RUN, not the list.

    This too is a deliberate contract move. The listing used to 503 when the
    probe failed, because the listing WAS the evidence claim; it no longer makes
    one, so a probe fault cannot make a configuration list untrue and must not
    blank the picker. What the fault must still do — fail the run closed, with a
    typed status and no backend text — is unchanged, and is pinned here at the
    submit boundary where the claim now lives.
    """
    gateway = ScriptedGateway()
    h = make_app(
        gateway=gateway,
        gateway_models=[PRIVATE_LIVE],
        gateway_tasks=TASKS,
        serving_probe=_ServingProbe(failure=RuntimeError("SECRET serving host failed")),
    )
    headers = h.bearer("user:ada", department="engineering")

    async def _drive() -> tuple[Any, Any]:
        async with h.client() as client:
            listed = await client.get("/api/v1/models/choices/ask", headers=headers)
            opened = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=headers,
            )
            assert opened.status_code == 200, opened.text
            submitted = await client.post(
                f"/api/v1/chat/sessions/{opened.json()['session_id']}/messages",
                json={"prompt": "must not run", "requested_model": PRIVATE_LIVE.name},
                headers=headers,
            )
        return listed, submitted

    listed, submitted = run(_drive())
    # The configuration list is intact: it never depended on the probe.
    assert listed.status_code == 200, listed.text
    assert [row["model"] for row in listed.json()["choices"]] == [PRIVATE_LIVE.name]
    # The run fails closed, typed, and without a word of the backend's error.
    assert submitted.status_code == 503, submitted.text
    assert submitted.json() == {"detail": "model selection evidence is unavailable"}
    assert "SECRET" not in submitted.text
    assert gateway.requests == []


def test_agentic_choices_are_server_mapped_and_disabled_workflows_stay_404(
    make_app: AppBuilder,
) -> None:
    h = make_app(
        gateway_models=[PRIVATE_LIVE],
        gateway_tasks=TASKS,
        serving_probe=_ServingProbe([_fresh_private_row()]),
    )
    headers = h.bearer(department="engineering")

    async def _request() -> Any:
        async with h.client() as client:
            return await client.get("/api/v1/models/choices/agentic-workflow", headers=headers)

    response = run(_request())
    assert response.status_code == 404
    assert response.json() == {"detail": "workflows not enabled"}
