# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exact-owner Ask APIs, model-context isolation, and legacy quarantine."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from ne.api.routers.chat import _mint_turn_id, _trace_id_for_turn
from ne.audit import list_records
from ne.contracts.models import Completion, CompletionRequest
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.store import (
    SqlAlchemySessionStore,
    conversation_request_fingerprint,
    ne_agent_conversation_turns,
)

from .conftest import ISSUER, AppBuilder, ScriptedGateway, get_state, run, text_completion


def _observe_two_successful_renewals(
    store: SqlAlchemySessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[asyncio.Event, asyncio.Event]:
    original_renew = store.renew_conversation_attachment_lease
    observing = asyncio.Event()
    observed_twice = asyncio.Event()
    successful_renewals = 0
    observed_attempt_id: str | None = None

    async def observed_renew(
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
    ) -> bool:
        nonlocal observed_attempt_id, successful_renewals
        renewed = await original_renew(
            conversation_id,
            owner_principal_id,
            attachment_id,
            attempt_id,
        )
        if renewed:
            if observed_attempt_id is None:
                observed_attempt_id = attempt_id
            if observing.is_set() and attempt_id == observed_attempt_id:
                successful_renewals += 1
                if successful_renewals >= 2:
                    observed_twice.set()
        return renewed

    monkeypatch.setattr(store, "renew_conversation_attachment_lease", observed_renew)
    return observing, observed_twice


def test_same_department_principals_get_distinct_contexts_and_multiple_new_ids(
    make_app: AppBuilder,
) -> None:
    gateway = ScriptedGateway(
        [
            text_completion("ada answer"),
            text_completion("ada followup answer"),
            text_completion("bob answer"),
        ]
    )
    harness = make_app(gateway=gateway)
    ada = harness.bearer("user:ada", department="engineering")
    bob = harness.bearer("user:bob", department="engineering")

    async def scenario() -> None:
        async with harness.client() as client:
            ada_one = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=ada,
            )
            ada_two = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=ada,
            )
            bob_one = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=bob,
            )
            ids = {
                ada_one.json()["session_id"],
                ada_two.json()["session_id"],
                bob_one.json()["session_id"],
            }
            assert len(ids) == 3

            sentinel = "ADA-PRIVATE-SENTINEL-007"
            sent = await client.post(
                f"/api/v1/chat/sessions/{ada_one.json()['session_id']}/messages",
                json={"prompt": sentinel},
                headers=ada,
            )
            assert sent.status_code == 200
            ada_followup = await client.post(
                f"/api/v1/chat/sessions/{ada_one.json()['session_id']}/messages",
                json={"prompt": "ada followup"},
                headers=ada,
            )
            assert ada_followup.status_code == 200
            bob_sent = await client.post(
                f"/api/v1/chat/sessions/{bob_one.json()['session_id']}/messages",
                json={"prompt": "bob question"},
                headers=bob,
            )
            assert bob_sent.status_code == 200

            listed = await client.get("/api/v1/chat/sessions", headers=ada)
            assert listed.status_code == 200
            assert listed.headers["cache-control"] == "private, no-store"
            assert {row["session_id"] for row in listed.json()["sessions"]} == {
                ada_one.json()["session_id"],
                ada_two.json()["session_id"],
            }
            fetched = await client.get(
                f"/api/v1/chat/sessions/{ada_one.json()['session_id']}", headers=ada
            )
            assert fetched.status_code == 200
            assert fetched.headers["cache-control"] == "private, no-store"

        assert len(gateway.requests) == 3
        assert sentinel in str(gateway.requests[0].messages)
        assert sentinel in str(gateway.requests[1].messages)
        assert sentinel not in str(gateway.requests[2].messages)

    run(scenario())


def test_same_principal_department_change_lists_empty_and_cannot_run_foreign_context(
    make_app: AppBuilder,
) -> None:
    gateway = ScriptedGateway()
    harness = make_app(gateway=gateway)
    engineering = harness.bearer("user:ada", department="engineering")
    finance = harness.bearer("user:ada", department="finance")

    async def scenario() -> None:
        async with harness.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=engineering,
            )
            session_id = created.json()["session_id"]
            listed = await client.get("/api/v1/chat/sessions", headers=finance)
            assert listed.status_code == 200
            assert listed.json() == {"sessions": []}
            unknown = await client.get(f"/api/v1/chat/sessions/{session_id}", headers=finance)
            send = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "must not run"},
                headers=finance,
            )
            assert unknown.status_code == send.status_code == 404
            assert unknown.content == send.content
        assert gateway.requests == []

    run(scenario())


def test_same_idempotency_key_replays_one_published_attachment_without_model_rerun(
    make_app: AppBuilder,
) -> None:
    gateway = ScriptedGateway([text_completion("ONE-SERVED-ANSWER")])
    harness = make_app(gateway=gateway)
    state = get_state(harness.app)
    auth = harness.bearer("user:ada", department="engineering")

    async def scenario() -> None:
        async with harness.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=auth,
            )
            session_id = created.json()["session_id"]
            bodies = []
            for _ in range(2):
                response = await client.post(
                    f"/api/v1/chat/sessions/{session_id}/messages",
                    json={"prompt": "same retried prompt"},
                    headers={**auth, "Idempotency-Key": "retry-007"},
                )
                assert response.status_code == 200
                bodies.append(response.content)
            assert b"ONE-SERVED-ANSWER" in bodies[0]
            assert b"ONE-SERVED-ANSWER" in bodies[1]
        assert len(gateway.requests) == 1
        with state.session_store.engine.connect() as conn:
            turns = conn.execute(
                sa.select(ne_agent_conversation_turns).where(
                    ne_agent_conversation_turns.c.conversation_id == session_id
                )
            ).all()
        assert len(turns) == 1
        assert turns[0].publication_state == "published"
        assert len(list_records(state.audit_engine, action="session.run", limit=10)) == 1
        assert len(list_records(state.audit_engine, action="api.chat.turn_replayed", limit=10)) == 1

    run(scenario())


def test_concurrent_same_request_claims_once_then_replays_exact_terminal(
    make_app: AppBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingGateway(ScriptedGateway):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, request: CompletionRequest) -> Completion:
            self.requests.append(request)
            self.started.set()
            await self.release.wait()
            return text_completion("ONE-CONCURRENT-ANSWER")

    gateway = BlockingGateway()
    harness = make_app(gateway=gateway)
    state = get_state(harness.app)
    # Keep the lease tolerant of CI scheduler/offload delays while observing the
    # real store-instance renew calls. Removing the heartbeat makes the bounded
    # wait below time out instead of relying on an artificial sub-100ms TTL race.
    state.session_store._conversation_lease_seconds = 2.0
    state.session_store._conversation_heartbeat_seconds = 0.05
    observing_renewals, observed_twice = _observe_two_successful_renewals(
        state.session_store, monkeypatch
    )
    auth = harness.bearer("user:ada", department="engineering")

    async def scenario() -> None:
        async with harness.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=auth,
            )
            session_id = created.json()["session_id"]
            endpoint = f"/api/v1/chat/sessions/{session_id}/messages"
            headers = {**auth, "Idempotency-Key": "concurrent-retry-007"}
            first = asyncio.create_task(
                client.post(endpoint, json={"prompt": "same concurrent prompt"}, headers=headers)
            )
            await asyncio.wait_for(gateway.started.wait(), timeout=1.0)
            observing_renewals.set()
            second = asyncio.create_task(
                client.post(endpoint, json={"prompt": "same concurrent prompt"}, headers=headers)
            )
            await asyncio.wait_for(observed_twice.wait(), timeout=5.0)
            gateway.release.set()
            first_response, second_response = await asyncio.gather(first, second)
            assert first_response.status_code == second_response.status_code == 200
            assert first_response.content == second_response.content
            assert b"ONE-CONCURRENT-ANSWER" in first_response.content

        assert len(gateway.requests) == 1
        with state.session_store.engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_agent_conversation_turns).where(
                    ne_agent_conversation_turns.c.conversation_id == session_id
                )
            ).all()
        assert len(rows) == 1
        assert rows[0].publication_state == "published"

    run(scenario())


def test_terminal_audit_block_keeps_lease_and_same_key_joins_exact_replay(
    make_app: AppBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = ScriptedGateway([text_completion("AUDIT-BLOCK-ANSWER")])
    harness = make_app(gateway=gateway)
    state = get_state(harness.app)
    state.session_store._conversation_lease_seconds = 2.0
    state.session_store._conversation_heartbeat_seconds = 0.05
    observing_renewals, observed_twice = _observe_two_successful_renewals(
        state.session_store, monkeypatch
    )
    underlying = state.sink
    underlying_append = underlying.append

    class BlockingSink:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def append(self, event) -> None:  # type: ignore[no-untyped-def]
            if event.action == "session.run":
                self.started.set()
                await self.release.wait()
            await underlying_append(event)

    blocking = BlockingSink()
    underlying.append = blocking.append  # type: ignore[method-assign]
    auth = harness.bearer("user:ada", department="engineering")

    async def scenario() -> None:
        async with harness.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=auth,
            )
            endpoint = f"/api/v1/chat/sessions/{created.json()['session_id']}/messages"
            headers = {**auth, "Idempotency-Key": "audit-block-007"}
            first = asyncio.create_task(
                client.post(endpoint, json={"prompt": "same prompt"}, headers=headers)
            )
            await asyncio.wait_for(blocking.started.wait(), timeout=1)
            observing_renewals.set()
            second = asyncio.create_task(
                client.post(endpoint, json={"prompt": "same prompt"}, headers=headers)
            )
            await asyncio.wait_for(observed_twice.wait(), timeout=5.0)
            blocking.release.set()
            first_response, second_response = await asyncio.gather(first, second)
        assert first_response.content == second_response.content
        assert b"AUDIT-BLOCK-ANSWER" in first_response.content
        assert len(gateway.requests) == 1

    run(scenario())


@pytest.mark.parametrize("crash_phase", ["before-audit", "after-audit"])
def test_crash_around_terminal_audit_recovers_staged_envelope_without_reexecution(
    make_app: AppBuilder,
    monkeypatch: pytest.MonkeyPatch,
    crash_phase: str,
) -> None:
    gateway = ScriptedGateway([text_completion("AUDITED-STAGED-ANSWER")])
    harness = make_app(gateway=gateway)
    state = get_state(harness.app)
    state.session_store._conversation_lease_seconds = 0.04
    state.session_store._conversation_heartbeat_seconds = 0.01
    crashed = False
    if crash_phase == "before-audit":
        original_emit = DepartmentAgentSession._emit_session_run

        async def crash_emit_once(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal crashed
            if not crashed:
                crashed = True
                raise RuntimeError("crash before terminal audit")
            return await original_emit(self, *args, **kwargs)

        monkeypatch.setattr(DepartmentAgentSession, "_emit_session_run", crash_emit_once)
    else:
        original_prepare = state.session_store.prepare_conversation_turn

        async def crash_prepare_once(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal crashed
            if not crashed:
                crashed = True
                raise RuntimeError("crash after terminal audit")
            return await original_prepare(*args, **kwargs)

        monkeypatch.setattr(state.session_store, "prepare_conversation_turn", crash_prepare_once)
    auth = harness.bearer("user:ada", department="engineering")

    async def scenario() -> None:
        async with harness.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=auth,
            )
            session_id = created.json()["session_id"]
            endpoint = f"/api/v1/chat/sessions/{session_id}/messages"
            key = f"audit-{crash_phase}-crash-007"
            headers = {**auth, "Idempotency-Key": key}
            turn_id = _mint_turn_id(session_id, "user:ada", key)
            trace_id = _trace_id_for_turn(turn_id)
            failed = await client.post(endpoint, json={"prompt": "same prompt"}, headers=headers)
            assert b'"type":"error"' in failed.content
            with state.session_store.engine.connect() as conn:
                staged = conn.execute(
                    sa.select(ne_agent_conversation_turns.c.publication_state).where(
                        ne_agent_conversation_turns.c.conversation_id == session_id
                    )
                ).scalar_one()
            assert staged == "staged"
            await asyncio.sleep(0.06)
            recovered = await client.post(endpoint, json={"prompt": "same prompt"}, headers=headers)
            assert b"AUDITED-STAGED-ANSWER" in recovered.content
            with state.session_store.engine.connect() as conn:
                stored_trace = conn.execute(
                    sa.select(ne_agent_conversation_turns.c.trace_id).where(
                        ne_agent_conversation_turns.c.attachment_id == turn_id
                    )
                ).scalar_one()
            assert stored_trace == trace_id
            records = list_records(state.audit_engine, action="session.run", limit=10)
            assert len(records) == 1
            assert records[0].trace_id == trace_id
            assert gateway.requests[0].trace_id == trace_id
        assert len(gateway.requests) == 1

    run(scenario())


def test_expired_prepared_attempt_is_adopted_and_published_without_reexecution(
    make_app: AppBuilder,
) -> None:
    gateway = ScriptedGateway([text_completion("MUST-NOT-RERUN")])
    harness = make_app(gateway=gateway)
    state = get_state(harness.app)
    auth = harness.bearer("user:ada", department="engineering")

    async def scenario() -> None:
        async with harness.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=auth,
            )
            session_id = created.json()["session_id"]
            key = "prepared-adoption-007"
            turn_id = _mint_turn_id(session_id, "user:ada", key)
            trace_id = _trace_id_for_turn(turn_id)
            fingerprint = conversation_request_fingerprint(
                session_id,
                "user:ada",
                "engineering",
                "console-chat",
                "same prompt",
                None,
                None,
                trace_id,
                (),
                {
                    "department": "engineering",
                    "iss": ISSUER,
                },
            )
            old_attempt = "attempt-before-process-restart"
            await state.session_store.append_conversation_turn(
                session_id,
                "user:ada",
                "engineering",
                "console-chat",
                [
                    ModelRequest(parts=[UserPromptPart(content="same prompt")]),
                    ModelResponse(
                        parts=[TextPart(content="PREPARED-ADOPTION-ANSWER")],
                        model_name="prepared",
                    ),
                ],
                served_output="PREPARED-ADOPTION-ANSWER",
                trace_id=trace_id,
                attachment_id=turn_id,
                attempt_id=old_attempt,
                request_fingerprint=fingerprint,
                idempotency_fingerprint=fingerprint,
                terminal_audited=True,
            )
            with state.session_store.engine.begin() as conn:
                conn.execute(
                    ne_agent_conversation_turns.update()
                    .where(ne_agent_conversation_turns.c.attachment_id == turn_id)
                    .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
            recovered = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "same prompt"},
                headers={**auth, "Idempotency-Key": key},
            )
            assert recovered.status_code == 200, recovered.text
            assert b"PREPARED-ADOPTION-ANSWER" in recovered.content
        with state.session_store.engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    ne_agent_conversation_turns.c.attempt_id,
                    ne_agent_conversation_turns.c.publication_state,
                ).where(ne_agent_conversation_turns.c.attachment_id == turn_id)
            ).one()
        assert row.attempt_id != old_attempt
        assert row.publication_state == "published"
        assert gateway.requests == []

    run(scenario())


def test_same_key_different_prompt_or_model_is_non_oracular_and_never_executes(
    make_app: AppBuilder,
) -> None:
    gateway = ScriptedGateway([text_completion("ORIGINAL-ANSWER")])
    harness = make_app(gateway=gateway)
    auth = harness.bearer("user:ada", department="engineering")

    async def scenario() -> None:
        async with harness.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=auth,
            )
            endpoint = f"/api/v1/chat/sessions/{created.json()['session_id']}/messages"
            headers = {**auth, "Idempotency-Key": "fingerprint-007"}
            original = await client.post(
                endpoint,
                json={"prompt": "original prompt"},
                headers=headers,
            )
            assert original.status_code == 200
            changed_scope = harness.bearer(
                "user:ada", groups=("new-sensitive-group",), department="engineering"
            )
            conflicts = [
                await client.post(endpoint, json={"prompt": "changed prompt"}, headers=headers),
                await client.post(
                    endpoint,
                    json={"prompt": "original prompt", "requested_model": "unknown-model"},
                    headers=headers,
                ),
                await client.post(
                    endpoint,
                    json={"prompt": "original prompt"},
                    headers={**changed_scope, "Idempotency-Key": "fingerprint-007"},
                ),
            ]
            assert {response.status_code for response in conflicts} == {409}
            assert len({response.content for response in conflicts}) == 1
            assert conflicts[0].json() == {
                "detail": "idempotency key conflicts with the original request"
            }
        assert len(gateway.requests) == 1

    run(scenario())


def test_foreign_random_and_legacy_ids_are_byte_equivalent_404_without_side_effects(
    make_app: AppBuilder,
) -> None:
    gateway = ScriptedGateway()
    harness = make_app(gateway=gateway)
    state = get_state(harness.app)
    ada = harness.bearer("user:ada", department="engineering")
    bob = harness.bearer("user:bob", department="engineering")

    async def scenario() -> None:
        legacy = await state.session_store.open_session("console-chat", "engineering")
        async with harness.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=ada,
            )
            foreign = created.json()["session_id"]
            bodies: list[bytes] = []
            for session_id in (foreign, "random-opaque-id", legacy):
                response = await client.get(f"/api/v1/chat/sessions/{session_id}", headers=bob)
                assert response.status_code == 404
                bodies.append(response.content)
                message = await client.post(
                    f"/api/v1/chat/sessions/{session_id}/messages",
                    json={"prompt": "must not run"},
                    headers=bob,
                )
                assert message.status_code == 404
                assert message.content == response.content
                ledger = await client.get(
                    f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=bob
                )
                assert ledger.status_code == 404
                assert ledger.content == response.content
                signal = await client.post(
                    f"/api/v1/chat/sessions/{session_id}/signals/thumbs",
                    json={"up": True, "target_ref": "chat:unknown:m1"},
                    headers=bob,
                )
                assert signal.status_code == 404
                assert signal.content == response.content
            assert len(set(bodies)) == 1
        assert gateway.requests == []
        capture = state.signal_capture
        assert capture is not None
        assert capture.store.read_signals() == []

    run(scenario())
