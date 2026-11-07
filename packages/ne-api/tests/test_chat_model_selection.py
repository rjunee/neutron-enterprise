# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Composed Ask proof for governed per-turn model selection."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from ne.api.model_selection import (
    MODEL_SELECTION_EVIDENCE_ACTION,
    ModelSelectionEvidenceError,
    ModelSelectionPolicyRefused,
    ModelSelectionUnavailable,
)
from ne.api.routers.chat import _mint_turn_id, _trace_id_for_turn
from ne.api.turn_wire import (
    ProtectedTerminalPayload,
    ProtectedTurnRef,
    TurnFrame,
    TurnRequest,
    done_frame,
)
from ne.audit import list_records
from ne.contracts.runtime import GroundedProvenance, VerifiedProvenance
from ne.contracts.types import Lane
from ne.gateway.config import ModelDef, TaskRoute
from ne.runtime.private_payloads import (
    CONVERSATION_TURN_TARGET_KIND,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    conversation_turn_content_binding,
)
from ne.runtime.store import conversation_request_fingerprint

from .conftest import AppBuilder, ScriptedGateway, get_state, parse_sse, run, text_completion

PRIVATE_A = ModelDef(
    name="private-a",
    litellm_model="hosted_vllm/acme/private-a",
    provider="vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://private-a.test/v1",
    context_window=32768,
)
PRIVATE_B = ModelDef(
    name="private-b",
    litellm_model="hosted_vllm/acme/private-b",
    provider="vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://private-b.test/v1",
    context_window=32768,
)


#: Narrower than both privates: input budget 6144 (window 8192). Wide enough
#: for the harness's ~2227-token tool catalog + the 512-token floor, so it is
#: OFFERED and selectable for a fresh conversation — but the continuity
#: reserve alone (8192) means any conversation with published context
#: overflows it, which is exactly the round-8 snapshot gate under test.
NARROW = ModelDef(
    name="narrow-model",
    litellm_model="hosted_vllm/acme/narrow",
    provider="vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://narrow.test/v1",
    context_window=8192,
)


@dataclass(frozen=True)
class _ServingRow:
    model_id: str
    lane: str
    state: str = "ok"
    checked_at: datetime | None = None
    serves_configured_model: bool | None = True


class _ServingProbe:
    async def observe(
        self,
        models: Any,
        *,
        force_refresh: bool = False,
    ) -> list[_ServingRow]:
        del force_refresh
        now = datetime.now(UTC)
        return [
            _ServingRow(model_id=model.name, lane=model.lane.value, checked_at=now)
            for model in models
        ]


class _Enqueuer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def enqueue(self, turn_id: str, request_json: str) -> None:
        self.calls.append((turn_id, request_json))


class _RequestBackedReader:
    def __init__(self, enqueuer: _Enqueuer) -> None:
        self.enqueuer = enqueuer
        self._state = None

    def bind(self, h: object) -> None:
        self._state = get_state(h.app)  # type: ignore[attr-defined]

    async def frames(self, turn_id: str) -> AsyncIterator[TurnFrame]:
        assert self._state is not None
        queued_turn, payload = self.enqueuer.calls[-1]
        assert queued_turn == turn_id
        request = TurnRequest.model_validate_json(payload)
        store = self._state.agent_run_store
        assert store is not None
        row = await store.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            run_id=None,
            owner_principal_id=request.principal_id,
            target_kind=CONVERSATION_TURN_TARGET_KIND,
            target_binding=conversation_turn_content_binding(
                conversation_id=request.session_id,
                attachment_id=request.turn_id,
                attempt_id=request.claimed_attempt_id,
                slot="terminal",
            ),
            content_status=ProtectedContentStatus.SOURCE_FREE,
            # Production protected worker payload: content only. Correlation is
            # carried by the outer request/frame, never copied into this body.
            plaintext=ProtectedTerminalPayload(output="durable answer").model_dump_json().encode(),
        )
        yield done_frame(
            0,
            request.session_id,
            protected_terminal=ProtectedTurnRef(
                protected_ref=row.row_id,
                protected_hash=row.plaintext_hash,
                content_status="source_free",
            ),
            message_id=request.turn_id,
        )


class _DenySelection:
    async def authorize(self, **_kwargs: Any) -> Any:
        raise ModelSelectionPolicyRefused("model selection was not allowed by policy")


class _UnavailableSelection:
    async def authorize(self, **_kwargs: Any) -> Any:
        raise ModelSelectionUnavailable("unavailable")


class _EvidenceErrorSelection:
    async def authorize(self, **_kwargs: Any) -> Any:
        raise ModelSelectionEvidenceError("unavailable evidence")


def _make_harness(
    make_app: AppBuilder,
    *,
    gateway: ScriptedGateway | None = None,
    turn_enqueuer: Any | None = None,
    turn_reader: Any | None = None,
    protected_transport: bool = False,
) -> Any:
    return make_app(
        gateway=gateway,
        gateway_models=[PRIVATE_A, PRIVATE_B],
        gateway_tasks={"agent": TaskRoute(model=PRIVATE_A.name)},
        serving_probe=_ServingProbe(),
        turn_enqueuer=turn_enqueuer,
        turn_reader=turn_reader,
        protected_transport=protected_transport,
    )


def _session(h: Any, headers: dict[str, str]) -> str:
    async def _open() -> str:
        async with h.client() as client:
            response = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            return response.json()["session_id"]

    return run(_open())


def test_inline_switch_keeps_history_and_joins_selection_to_done_trace(
    make_app: AppBuilder,
) -> None:
    gateway = ScriptedGateway([text_completion("answer one"), text_completion("answer two")])
    h = _make_harness(make_app, gateway=gateway)
    headers = h.bearer("user:ada", department="engineering")
    session_id = _session(h, headers)

    async def _ask(model: str, prompt: str) -> Any:
        async with h.client() as client:
            return await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": prompt, "requested_model": model},
                headers=headers,
            )

    first = run(_ask(PRIVATE_B.name, "first question"))
    second = run(_ask(PRIVATE_A.name, "second question"))
    assert first.status_code == second.status_code == 200
    first_done = parse_sse(first.text).of_type("done")[0].data
    second_done = parse_sse(second.text).of_type("done")[0].data
    assert first_done is not None and second_done is not None
    assert re.fullmatch(r"[0-9a-f]{32}", first_done["trace_id"])
    assert first_done["trace_id"] != second_done["trace_id"]

    assert [request.requested_model for request in gateway.requests] == [
        PRIVATE_B.name,
        PRIVATE_A.name,
    ]
    assert [request.lane for request in gateway.requests] == [
        Lane.PRIVATE_VLLM,
        Lane.PRIVATE_VLLM,
    ]
    assert [request.trace_id for request in gateway.requests] == [
        first_done["trace_id"],
        second_done["trace_id"],
    ]
    second_contents = [message.content for message in gateway.requests[1].messages]
    assert "first question" in second_contents
    assert "answer one" in second_contents

    records = list_records(
        get_state(h.app).audit_engine,
        action=MODEL_SELECTION_EVIDENCE_ACTION,
        limit=100,
    )
    assert {record.trace_id for record in records if record.outcome == "success"} == {
        first_done["trace_id"],
        second_done["trace_id"],
    }


def test_legacy_switch_is_gated_against_the_conversation_snapshot(
    make_app: AppBuilder,
) -> None:
    """NE-CTX-IN-FORCE round 8, finding 2 — the snapshot gate covers legacy chat.

    Round 7 gated an explicit model switch against the current conversation
    snapshot (SPEC PRE-DEMO-014, no silent truncation) — but only on the
    canonical default-Ask branch. Legacy chat reached ``authorize_model_choice``
    with no ``snapshot_tokens`` at all, so a long wide-model session could
    authorize a switch to a model that cannot hold the conversation, and the
    per-turn trim would then silently drop older turns — the exact SPEC
    violation round 7 closed, through a different door.

    The distinction that must survive (both directions asserted here):

    * a FRESH conversation genuinely has no snapshot — no published turn, so
      the store reports ``None`` and no fit check runs (the demo's
      model-switch beat: the narrow model is selectable on turn one);
    * a CONTINUING conversation has a real snapshot — the stored bytes of the
      published turns the run will reload + this prompt + the continuity
      reserve — and a model that snapshot overflows refuses BY NAME with no
      gateway call, while a switch to a model that FITS still serves.
    """
    gateway = ScriptedGateway([text_completion("answer one"), text_completion("answer two")])
    h = make_app(
        gateway=gateway,
        gateway_models=[PRIVATE_A, PRIVATE_B, NARROW],
        gateway_tasks={"agent": TaskRoute(model=PRIVATE_A.name)},
        serving_probe=_ServingProbe(),
    )
    headers = h.bearer("user:ada", department="engineering")
    session_id = _session(h, headers)

    async def _ask(model: str, prompt: str) -> Any:
        async with h.client() as client:
            return await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": prompt, "requested_model": model},
                headers=headers,
            )

    # Fresh conversation: no published turn, no snapshot, no fit check — the
    # narrow model is a legitimate first-turn choice and really serves.
    first = run(_ask(NARROW.name, "first question"))
    assert first.status_code == 200, first.text

    # Continuing conversation: the published context + prompt + the 8192-token
    # continuity reserve overflow narrow-model's 6144-token input budget, so
    # the switch refuses BY NAME before any model call — not authorized and
    # then silently trimmed.
    refused = run(_ask(NARROW.name, "second question"))
    assert refused.status_code == 409, refused.text
    assert NARROW.name in refused.json()["detail"]

    # Control on the SAME conversation: a switch to a model the snapshot FITS
    # is still authorized and serves with the history intact (no truncation,
    # no new-conversation disguise).
    second = run(_ask(PRIVATE_B.name, "third question"))
    assert second.status_code == 200, second.text
    assert [request.requested_model for request in gateway.requests] == [
        NARROW.name,
        PRIVATE_B.name,
    ]
    served_contents = [message.content for message in gateway.requests[1].messages]
    assert "first question" in served_contents
    assert "answer one" in served_contents


def test_enqueue_carries_exact_authorized_model_lane_and_server_trace(
    make_app: AppBuilder,
) -> None:
    enqueuer = _Enqueuer()
    reader = _RequestBackedReader(enqueuer)
    gateway = ScriptedGateway()
    h = _make_harness(
        make_app,
        gateway=gateway,
        turn_enqueuer=enqueuer,
        turn_reader=reader,
        protected_transport=True,
    )
    reader.bind(h)
    headers = h.bearer("user:ada", department="engineering")
    session_id = _session(h, headers)

    async def _ask() -> Any:
        async with h.client() as client:
            return await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "durable question", "requested_model": PRIVATE_B.name},
                headers={**headers, "Idempotency-Key": "stable-turn"},
            )

    response = run(_ask())
    assert response.status_code == 200, response.text
    assert len(enqueuer.calls) == 1
    workflow_id, payload = enqueuer.calls[0]
    request = TurnRequest.model_validate_json(payload)
    assert workflow_id == f"{request.turn_id}:{request.claimed_attempt_id}"
    assert request.requested_model == PRIVATE_B.name
    assert request.requested_model_lane is Lane.PRIVATE_VLLM
    assert request.trace_id == _trace_id_for_turn(request.turn_id)
    assert gateway.requests == []  # durable work has not been run in the API process
    done = parse_sse(response.text).of_type("done")[0].data
    assert done is not None
    assert done["trace_id"] == request.trace_id
    assert done["message_id"] == request.turn_id


@pytest.mark.parametrize("enqueue_mode", [False, True], ids=["inline", "enqueue"])
@pytest.mark.parametrize(
    "selection_authority",
    [_DenySelection(), _UnavailableSelection(), _EvidenceErrorSelection()],
    ids=["pdp-deny", "catalog-unavailable", "evidence-error"],
)
def test_published_replay_precedes_model_authorization_and_enqueue(
    make_app: AppBuilder,
    enqueue_mode: bool,
    selection_authority: Any,
) -> None:
    enqueuer = _Enqueuer() if enqueue_mode else None
    reader = _RequestBackedReader(enqueuer) if enqueuer is not None else None
    gateway = ScriptedGateway()
    h = _make_harness(
        make_app,
        gateway=gateway,
        turn_enqueuer=enqueuer,
        turn_reader=reader,
    )
    headers = h.bearer("user:ada", department="engineering")
    session_id = _session(h, headers)
    state = get_state(h.app)
    turn_id = _mint_turn_id(session_id, "user:ada", "published-replay")
    verified = VerifiedProvenance(
        template_id="replay-template",
        template_version=1,
        config_hash="replay-config",
        row_count=1,
        truncated=False,
    )
    grounded = GroundedProvenance(
        fetches=(verified,),
        citation_count=1,
        gate="grounding-v1",
        fallback=False,
        assist_used=False,
        prompt_echo_count=0,
    )
    citation = {
        "chunk_id": "replay-chunk",
        "document_id": "replay-doc",
        "source_system": "sharepoint",
        "source_ref": "replay-ref",
    }

    async def _ask() -> Any:
        await state.session_store.append_conversation_turn(
            session_id,
            "user:ada",
            "engineering",
            "console-chat",
            [
                ModelRequest(parts=[UserPromptPart(content="private replay prompt")]),
                ModelResponse(
                    parts=[TextPart(content="private replay answer")],
                    model_name="private-replay-model",
                ),
            ],
            served_output="DURABLE-REPLAY-ANSWER",
            trace_id=_trace_id_for_turn(turn_id),
            attachment_id=turn_id,
            attempt_id="attempt-published-replay",
            verified=verified,
            grounded=grounded,
            trace_linked=True,
            request_fingerprint=conversation_request_fingerprint(
                session_id,
                "user:ada",
                "engineering",
                "console-chat",
                "retry",
                PRIVATE_B.name,
                None,
                _trace_id_for_turn(turn_id),
                (),
                {
                    "iss": "https://idp.ne.test/realms/ne-test",
                    "department": "engineering",
                },
            ),
        )
        await state.session_store.publish_conversation_turn(
            session_id,
            "user:ada",
            turn_id,
            "attempt-published-replay",
            [citation["chunk_id"]],
            citation_payloads=[citation],
        )
        state.model_selection = selection_authority  # type: ignore[assignment]
        async with h.client() as client:
            return await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "retry", "requested_model": PRIVATE_B.name},
                headers={**headers, "Idempotency-Key": "published-replay"},
            )

    response = run(_ask())
    assert response.status_code == 200, response.text
    parsed = parse_sse(response.text)
    assert parsed.of_type("citations")[0].data == {
        "type": "citations",
        "citations": [citation],
    }
    terminal = parsed.of_type("done")[0].data
    assert terminal is not None
    assert terminal["output"] == "DURABLE-REPLAY-ANSWER"
    assert terminal["message_id"] == turn_id
    assert terminal["trace_id"] == _trace_id_for_turn(turn_id)
    assert terminal["verified"] == verified.model_dump(mode="json")
    assert terminal["grounded"] == grounded.model_dump(mode="json")
    assert gateway.requests == []
    if enqueuer is not None:
        assert enqueuer.calls == []
    assert len(list_records(state.audit_engine, action="api.chat.turn_replayed", limit=10)) == 1
    assert list_records(state.audit_engine, action="session.run", limit=10) == ()


def test_policy_refused_chat_creates_no_gateway_or_durable_work(make_app: AppBuilder) -> None:
    enqueuer = _Enqueuer()
    gateway = ScriptedGateway()
    h = _make_harness(
        make_app,
        gateway=gateway,
        turn_enqueuer=enqueuer,
        turn_reader=_RequestBackedReader(enqueuer),
    )
    get_state(h.app).model_selection = _DenySelection()  # type: ignore[assignment]
    headers = h.bearer("user:ada", department="engineering")
    session_id = _session(h, headers)

    async def _ask() -> Any:
        async with h.client() as client:
            return await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "must not run", "requested_model": PRIVATE_B.name},
                headers=headers,
            )

    response = run(_ask())
    assert response.status_code == 403
    assert response.json() == {"detail": "model selection was not allowed by policy"}
    assert enqueuer.calls == []
    assert gateway.requests == []


def test_policy_refused_claim_is_retryable_with_same_exact_request(make_app: AppBuilder) -> None:
    gateway = ScriptedGateway([text_completion("RETRY-AFTER-AUTH-ANSWER")])
    h = _make_harness(make_app, gateway=gateway)
    state = get_state(h.app)
    allowed_selection = state.model_selection
    state.model_selection = _DenySelection()  # type: ignore[assignment]
    headers = h.bearer("user:ada", department="engineering")
    session_id = _session(h, headers)

    async def _drive() -> tuple[Any, Any]:
        endpoint = f"/api/v1/chat/sessions/{session_id}/messages"
        request_headers = {**headers, "Idempotency-Key": "retry-after-auth"}
        body = {"prompt": "same prompt", "requested_model": PRIVATE_B.name}
        async with h.client() as client:
            refused = await client.post(endpoint, json=body, headers=request_headers)
            state.model_selection = allowed_selection
            accepted = await client.post(endpoint, json=body, headers=request_headers)
        return refused, accepted

    refused, accepted = run(_drive())
    assert refused.status_code == 403
    assert accepted.status_code == 200
    assert b"RETRY-AFTER-AUTH-ANSWER" in accepted.content
    assert len(gateway.requests) == 1


def test_unavailable_and_missing_authority_are_safe_409_and_503(make_app: AppBuilder) -> None:
    gateway = ScriptedGateway()
    h = _make_harness(make_app, gateway=gateway)
    headers = h.bearer("user:ada", department="engineering")
    session_id = _session(h, headers)

    async def _ask(h: Any, session_id: str, headers: dict[str, str], model: str) -> Any:
        async with h.client() as client:
            return await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "must not run", "requested_model": model},
                headers=headers,
            )

    unknown = run(_ask(h, session_id, headers, "SECRET-UNREGISTERED-MODEL"))
    get_state(h.app).model_selection = None
    unavailable = run(_ask(h, session_id, headers, PRIVATE_B.name))
    assert unknown.status_code == 409
    assert unknown.json() == {"detail": "requested model is unavailable"}
    assert "SECRET-UNREGISTERED-MODEL" not in unknown.text
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "model selection is unavailable"}
    assert gateway.requests == []


def test_omitted_model_fails_closed_when_selection_authority_is_unavailable(
    make_app: AppBuilder,
) -> None:
    gateway = ScriptedGateway([text_completion("legacy answer")])
    h = _make_harness(make_app, gateway=gateway)
    # Omission still resolves through the server authority; otherwise a stopped
    # deployment default could bypass current availability evidence.
    get_state(h.app).model_selection = None
    headers = h.bearer("user:ada", department="engineering")
    session_id = _session(h, headers)

    async def _ask() -> Any:
        async with h.client() as client:
            return await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "legacy question"},
                headers=headers,
            )

    response = run(_ask())
    assert response.status_code == 503, response.text
    assert gateway.requests == []
