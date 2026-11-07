# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Principal-owned Ask conversation storage and legacy quarantine."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy.exc import IntegrityError

from ne.contracts.runtime import GroundedProvenance, VerifiedProvenance
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.factory import create_department_session
from ne.runtime.store import (
    AttachmentRequestMismatchError,
    ProtectedConversationContentBinding,
    ProtectedConversationTurn,
    SqlAlchemySessionStore,
    _canonical_schema_expression,
    conversation_request_fingerprint,
    ensure_runtime_schema,
    ne_agent_conversation_turns,
    ne_agent_conversations,
    ne_agent_session_messages,
)


def _store(url: str = "sqlite://") -> SqlAlchemySessionStore:
    engine = sa.create_engine(url)
    ensure_runtime_schema(engine)
    return SqlAlchemySessionStore(engine)


def _private(prompt: str, draft: str) -> list[ModelRequest | ModelResponse]:
    return [
        ModelRequest(parts=[UserPromptPart(content=prompt)]),
        ModelResponse(parts=[TextPart(content=draft)], model_name="private"),
    ]


def test_request_fingerprint_distinguishes_every_direct_run_choice() -> None:
    values = {
        conversation_request_fingerprint("c", "o", "d", "a", "prompt", None),
        conversation_request_fingerprint("c", "o", "d", "a", "prompt", ""),
        conversation_request_fingerprint("c", "o", "d", "a", "prompt", "m", "private-vllm"),
        conversation_request_fingerprint("c", "o", "d", "a", "prompt", "m", "frontier-api"),
        conversation_request_fingerprint("c", "o", "d", "a", "different", "m", "frontier-api"),
    }
    assert len(values) == 5


@pytest.mark.parametrize(
    ("candidate", "matches"),
    [("a" * 64, True), ("b" + "a" * 63, False), ("a" * 63 + "b", False)],
)
def test_claim_fingerprint_comparison_uses_constant_time_exact_match(
    monkeypatch: pytest.MonkeyPatch,
    candidate: str,
    matches: bool,
) -> None:
    store = _store()
    calls: list[tuple[str, str]] = []
    original_compare = hmac.compare_digest

    def recording_compare(left: str, right: str) -> bool:
        calls.append((left, right))
        return original_compare(left, right)

    monkeypatch.setattr(hmac, "compare_digest", recording_compare)

    async def scenario() -> None:
        conversation = await store.create_conversation("console-chat", "eng", "user:ada")
        exact = "a" * 64
        first = await store.claim_conversation_attachment(
            conversation.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "fingerprint-attachment",
            exact,
            "fingerprint-trace",
        )
        assert first.disposition == "owned"
        if matches:
            replay = await store.claim_conversation_attachment(
                conversation.conversation_id,
                "user:ada",
                "eng",
                "console-chat",
                "fingerprint-attachment",
                candidate,
                "fingerprint-trace",
            )
            assert replay.disposition == "inflight"
        else:
            with pytest.raises(AttachmentRequestMismatchError):
                await store.claim_conversation_attachment(
                    conversation.conversation_id,
                    "user:ada",
                    "eng",
                    "console-chat",
                    "fingerprint-attachment",
                    candidate,
                    "fingerprint-trace",
                )

    asyncio.run(scenario())
    assert calls == [("a" * 64, candidate)]


def test_runnable_session_factories_require_owner_and_have_no_legacy_fallback() -> None:
    for callable_ in (create_department_session, DepartmentAgentSession):
        owner = inspect.signature(callable_).parameters["owner_principal_id"]
        assert owner.default is inspect.Parameter.empty
        assert owner.annotation in ("str", str)
    factory_source = inspect.getsource(create_department_session)
    session_source = inspect.getsource(DepartmentAgentSession)
    assert ".open_session(" not in factory_source
    assert ".load_history(" not in session_source
    assert ".append_turn(" not in session_source
    with pytest.raises(TypeError, match="owner_principal_id"):
        create_department_session(  # type: ignore[call-arg]
            "assistant", "eng", store=None, gateway=None, executor=None
        )
    with pytest.raises(TypeError, match="owner_principal_id"):
        DepartmentAgentSession(  # type: ignore[call-arg]
            session_id="legacy",
            agent_name="assistant",
            department="eng",
            store=None,
            executor=None,
            gateway=None,
            settings=None,
        )


def test_multiple_owned_conversations_never_collapse_and_foreign_owner_is_absent() -> None:
    store = _store()

    async def scenario() -> None:
        ada_one = await store.create_conversation("console-chat", "eng", "user:ada")
        ada_two = await store.create_conversation("console-chat", "eng", "user:ada")
        bob = await store.create_conversation("console-chat", "eng", "user:bob")
        assert len({ada_one.conversation_id, ada_two.conversation_id, bob.conversation_id}) == 3
        assert await store.get_conversation(ada_one.conversation_id, "user:bob", "eng") is None
        assert [
            row.conversation_id for row in await store.list_conversations("user:ada", "eng")
        ] == [
            ada_two.conversation_id,
            ada_one.conversation_id,
        ]

    asyncio.run(scenario())


def test_published_replay_preserves_original_citation_membership() -> None:
    store = _store()
    verified = VerifiedProvenance(
        template_id="accounts-directory",
        template_version=1,
        config_hash="config-hash",
        row_count=2,
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

    async def scenario() -> None:
        row = await store.create_conversation("console-chat", "eng", "user:ada")
        await store.append_conversation_turn(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            _private("question", "private"),
            served_output="served",
            trace_id="trace-replay",
            attachment_id="turn-replay",
            attempt_id="attempt-replay",
            verified=verified,
            grounded=grounded,
            trace_linked=True,
        )
        await store.publish_conversation_turn(
            row.conversation_id,
            "user:ada",
            "turn-replay",
            "attempt-replay",
            ["source-owned"],
            citation_payloads=[
                {
                    "chunk_id": "source-owned",
                    "document_id": "doc-owned",
                    "source_system": "sharepoint",
                    "source_ref": "ref-owned",
                }
            ],
        )
        published_claim = await store.claim_conversation_attachment(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "turn-replay",
            hashlib.sha256(b"store-direct:attempt-replay").hexdigest(),
            "trace-replay",
        )
        assert (published_claim.disposition, published_claim.attempt_id) == (
            "published",
            "attempt-replay",
        )
        await store.publish_conversation_turn(
            row.conversation_id, "user:ada", "turn-replay", "attempt-replay", []
        )
        assert await store.conversation_has_attachment(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "turn-replay",
            "source-owned",
        )
        replay = await store.published_conversation_replay(
            row.conversation_id, "user:ada", "eng", "console-chat", "turn-replay"
        )
        assert replay is not None
        assert replay.citations[0]["document_id"] == "doc-owned"
        assert replay.verified == verified
        assert replay.grounded == grounded
        assert replay.trace_linked is True

    asyncio.run(scenario())


def test_protected_conversation_turn_returns_only_exact_owner_scoped_bindings() -> None:
    store = _store()

    async def scenario() -> None:
        conversation = await store.create_conversation("console-chat", "eng", "user:ada")
        await store.append_conversation_turn(
            conversation.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            _private("PRIVATE-INPUT-SENTINEL", "PRIVATE-DRAFT-SENTINEL"),
            served_output="SERVED-OUTPUT-SENTINEL",
            trace_id="trace-protected",
            attachment_id="attachment-protected",
            attempt_id="attempt-protected",
        )
        await store.publish_conversation_turn(
            conversation.conversation_id,
            "user:ada",
            "attachment-protected",
            "attempt-protected",
            [],
        )
        with store.engine.begin() as conn:
            conn.execute(
                ne_agent_conversation_turns.update()
                .where(
                    ne_agent_conversation_turns.c.conversation_id == conversation.conversation_id
                )
                .values(
                    private_context="",
                    served_output="",
                    citation_refs="[]",
                    served_citations="[]",
                    served_verified="",
                    served_grounded="",
                    input_status="source_free",
                    protected_input_ref="protected-input-row",
                    protected_input_hash="a" * 64,
                    terminal_status="source_free",
                    protected_terminal_ref="protected-terminal-row",
                    protected_terminal_hash="b" * 64,
                )
            )
        protected = await store.protected_conversation_turn(
            conversation.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "attachment-protected",
            "attempt-protected",
        )
        assert protected == ProtectedConversationTurn(
            conversation_id=conversation.conversation_id,
            owner_principal_id="user:ada",
            turn_sequence=1,
            attachment_id="attachment-protected",
            attempt_id="attempt-protected",
            input=ProtectedConversationContentBinding(
                status="source_free",
                protected_ref="protected-input-row",
                plaintext_hash="a" * 64,
            ),
            terminal=ProtectedConversationContentBinding(
                status="source_free",
                protected_ref="protected-terminal-row",
                plaintext_hash="b" * 64,
            ),
        )
        rendered = repr(protected)
        for secret in (
            "PRIVATE-INPUT-SENTINEL",
            "PRIVATE-DRAFT-SENTINEL",
            "SERVED-OUTPUT-SENTINEL",
        ):
            assert secret not in rendered

    asyncio.run(scenario())


def test_protected_conversation_turn_withholds_foreign_partial_and_invalid_shapes() -> None:
    store = _store()

    async def scenario() -> None:
        conversation = await store.create_conversation("console-chat", "eng", "user:ada")
        await store.append_conversation_turn(
            conversation.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            _private("private", "draft"),
            served_output="served",
            trace_id="trace-protected",
            attachment_id="attachment-protected",
            attempt_id="attempt-protected",
        )
        await store.publish_conversation_turn(
            conversation.conversation_id,
            "user:ada",
            "attachment-protected",
            "attempt-protected",
            [],
        )
        where = ne_agent_conversation_turns.c.conversation_id == conversation.conversation_id
        valid = {
            "private_context": "",
            "served_output": "",
            "input_status": "source_free",
            "protected_input_ref": "protected-input-row",
            "protected_input_hash": "a" * 64,
            "terminal_status": "source_free",
            "protected_terminal_ref": "protected-terminal-row",
            "protected_terminal_hash": "b" * 64,
        }
        with store.engine.begin() as conn:
            conn.execute(ne_agent_conversation_turns.update().where(where).values(**valid))

        async def read(owner: str = "user:ada", attempt: str = "attempt-protected"):
            return await store.protected_conversation_turn(
                conversation.conversation_id,
                owner,
                "eng",
                "console-chat",
                "attachment-protected",
                attempt,
            )

        assert await read("user:bob") is None
        assert await read(attempt="foreign-attempt") is None
        for mutation in (
            {"terminal_status": "legacy_unavailable"},
            {"protected_terminal_hash": None},
            {"protected_input_hash": "A" * 64},
            {"protected_input_ref": ""},
            {"publication_state": "prepared"},
        ):
            with store.engine.begin() as conn:
                conn.execute(
                    ne_agent_conversation_turns.update()
                    .where(where)
                    .values(**{**valid, **mutation})
                )
            assert await read() is None

    asyncio.run(scenario())


def test_protected_conversation_ledger_page_is_owner_scoped_bounded_and_content_free() -> None:
    store = _store()

    async def scenario() -> None:
        conversation = await store.create_conversation("console-chat", "eng", "user:ada")
        statuses = ("source_free", "source_bound_container", "legacy_unavailable")
        for sequence, status in enumerate(statuses, start=1):
            attachment_id = f"ledger-attachment-{sequence}"
            attempt_id = f"ledger-attempt-{sequence}"
            await store.append_conversation_turn(
                conversation.conversation_id,
                "user:ada",
                "eng",
                "console-chat",
                _private(f"PRIVATE-LEDGER-INPUT-{sequence}", "PRIVATE-LEDGER-DRAFT"),
                served_output=f"PRIVATE-LEDGER-OUTPUT-{sequence}",
                trace_id=f"ledger-trace-{sequence}",
                attachment_id=attachment_id,
                attempt_id=attempt_id,
            )
            await store.publish_conversation_turn(
                conversation.conversation_id,
                "user:ada",
                attachment_id,
                attempt_id,
                [],
            )
            with store.engine.begin() as conn:
                conn.execute(
                    ne_agent_conversation_turns.update()
                    .where(
                        ne_agent_conversation_turns.c.conversation_id
                        == conversation.conversation_id,
                        ne_agent_conversation_turns.c.sequence == sequence,
                    )
                    .values(
                        input_status=status,
                        protected_input_ref=f"protected-ledger-input-{sequence}",
                        protected_input_hash=f"{sequence}" * 64,
                        terminal_status=status,
                        protected_terminal_ref=f"protected-ledger-terminal-{sequence}",
                        protected_terminal_hash=f"{sequence + 3}" * 64,
                    )
                )

        page = await store.protected_conversation_ledger_page(
            conversation.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            limit=2,
        )
        assert [
            (
                turn.turn_sequence,
                turn.attachment_id,
                turn.attempt_id,
                turn.input.status,
                turn.terminal.status,
            )
            for turn in page.turns
        ] == [
            (
                3,
                "ledger-attachment-3",
                "ledger-attempt-3",
                "legacy_unavailable",
                "legacy_unavailable",
            ),
            (
                2,
                "ledger-attachment-2",
                "ledger-attempt-2",
                "source_bound_container",
                "source_bound_container",
            ),
        ]
        assert page.next_before_sequence == 2
        assert "PRIVATE-LEDGER" not in repr(page)

        tail = await store.protected_conversation_ledger_page(
            conversation.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            limit=2,
            before_sequence=page.next_before_sequence,
        )
        assert [turn.turn_sequence for turn in tail.turns] == [1]
        assert tail.next_before_sequence is None

        for owner, department in (("user:bob", "eng"), ("user:ada", "finance")):
            foreign = await store.protected_conversation_ledger_page(
                conversation.conversation_id,
                owner,
                department,
                "console-chat",
                limit=2,
            )
            assert foreign.turns == () and foreign.next_before_sequence is None

        with store.engine.begin() as conn:
            conn.execute(
                ne_agent_conversation_turns.update()
                .where(
                    ne_agent_conversation_turns.c.conversation_id == conversation.conversation_id,
                    ne_agent_conversation_turns.c.sequence == 3,
                )
                .values(protected_terminal_hash="NOT-A-HASH")
            )
        invalid = await store.protected_conversation_ledger_page(
            conversation.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            limit=2,
        )
        assert invalid.turns == () and invalid.next_before_sequence is None

    asyncio.run(scenario())


def test_foreign_retry_attempt_cannot_abort_another_requests_prepared_turn() -> None:
    store = _store()

    async def scenario() -> None:
        row = await store.create_conversation("console-chat", "eng", "user:ada")
        await store.append_conversation_turn(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            _private("question", "private"),
            served_output="served",
            trace_id="reused-trace",
            attachment_id="shared-idempotency-key",
            attempt_id="winning-attempt",
        )
        with pytest.raises(LookupError):
            await store.abort_conversation_turn(
                row.conversation_id,
                "user:ada",
                "shared-idempotency-key",
                "losing-attempt",
            )
        await store.publish_conversation_turn(
            row.conversation_id,
            "user:ada",
            "shared-idempotency-key",
            "winning-attempt",
            [],
        )
        assert (
            await store.published_conversation_output(
                row.conversation_id,
                "user:ada",
                "eng",
                "console-chat",
                "shared-idempotency-key",
            )
            == "served"
        )

    asyncio.run(scenario())


def test_reclaimed_attachment_rejects_the_delayed_worker_attempt() -> None:
    store = _store()

    async def scenario() -> None:
        row = await store.create_conversation("console-chat", "eng", "user:ada")
        fingerprint = conversation_request_fingerprint(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "same prompt",
            None,
        )
        old = await store.claim_conversation_attachment(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "shared-attachment",
            fingerprint,
            "old-trace",
        )
        assert old.disposition == "owned" and old.attempt_id is not None
        # Simulate process loss: no heartbeat or abort reaches the store, and
        # only expiry makes the orphan reclaimable.
        with store.engine.begin() as conn:
            conn.execute(
                ne_agent_conversation_turns.update()
                .where(
                    ne_agent_conversation_turns.c.conversation_id == row.conversation_id,
                    ne_agent_conversation_turns.c.attachment_id == "shared-attachment",
                )
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        current = await store.claim_conversation_attachment(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "shared-attachment",
            fingerprint,
            "old-trace",
        )
        assert current.disposition == "owned" and current.attempt_id is not None
        assert current.attempt_id != old.attempt_id

        with pytest.raises(LookupError, match="stale"):
            await store.claim_conversation_attachment(
                row.conversation_id,
                "user:ada",
                "eng",
                "console-chat",
                "shared-attachment",
                fingerprint,
                "old-trace",
                preferred_attempt_id=old.attempt_id,
            )
        with pytest.raises(LookupError, match=r"stale|not owned"):
            await store.append_conversation_turn(
                row.conversation_id,
                "user:ada",
                "eng",
                "console-chat",
                _private("same prompt", "old answer"),
                served_output="old answer",
                trace_id="old-trace",
                attachment_id="shared-attachment",
                attempt_id=old.attempt_id,
                request_fingerprint=fingerprint,
            )
        await store.append_conversation_turn(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            _private("same prompt", "current answer"),
            served_output="current answer",
            trace_id="old-trace",
            attachment_id="shared-attachment",
            attempt_id=current.attempt_id,
            request_fingerprint=fingerprint,
        )
        await store.publish_conversation_turn(
            row.conversation_id,
            "user:ada",
            "shared-attachment",
            current.attempt_id,
            [],
        )
        replay = await store.published_conversation_replay(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "shared-attachment",
            fingerprint,
        )
        assert replay is not None and replay.output == "current answer"

    asyncio.run(scenario())


def test_prepared_takeover_rotates_epoch_and_stale_attempts_are_powerless() -> None:
    store = _store()

    async def scenario() -> None:
        row = await store.create_conversation("console-chat", "eng", "user:ada")
        fingerprint = conversation_request_fingerprint(
            row.conversation_id, "user:ada", "eng", "console-chat", "same prompt", None
        )
        first = await store.claim_conversation_attachment(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "epoch-attachment",
            fingerprint,
            "epoch-trace",
        )
        assert first.attempt_id is not None
        first_attempt = first.attempt_id
        await store.append_conversation_turn(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            _private("same prompt", "immutable answer"),
            served_output="immutable answer",
            trace_id="epoch-trace",
            attachment_id="epoch-attachment",
            attempt_id=first_attempt,
            request_fingerprint=fingerprint,
            terminal_audited=False,
        )
        await store.prepare_conversation_turn(
            row.conversation_id, "user:ada", "epoch-attachment", first_attempt
        )
        with store.engine.begin() as conn:
            conn.execute(
                ne_agent_conversation_turns.update()
                .where(ne_agent_conversation_turns.c.attachment_id == "epoch-attachment")
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        winner = await store.claim_conversation_attachment(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "epoch-attachment",
            fingerprint,
            "epoch-trace",
        )
        assert winner.disposition == "prepared"
        assert winner.attempt_id is not None and winner.attempt_id != first_attempt
        assert not await store.renew_conversation_attachment_lease(
            row.conversation_id, "user:ada", "epoch-attachment", first_attempt
        )
        for action in (
            lambda: store.prepare_conversation_turn(
                row.conversation_id, "user:ada", "epoch-attachment", first_attempt
            ),
            lambda: store.abort_conversation_turn(
                row.conversation_id, "user:ada", "epoch-attachment", first_attempt
            ),
            lambda: store.publish_conversation_turn(
                row.conversation_id, "user:ada", "epoch-attachment", first_attempt, []
            ),
        ):
            with pytest.raises(LookupError):
                await action()
        replay = await store.prepared_conversation_replay(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "epoch-attachment",
            fingerprint,
        )
        assert replay is not None and replay.output == "immutable answer"

    asyncio.run(scenario())


def test_aborted_attempt_is_never_revived_by_its_old_preferred_token() -> None:
    store = _store()

    async def scenario() -> None:
        row = await store.create_conversation("console-chat", "eng", "user:ada")
        fingerprint = conversation_request_fingerprint(
            row.conversation_id, "user:ada", "eng", "console-chat", "prompt", None
        )
        old = await store.claim_conversation_attachment(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "aborted-attachment",
            fingerprint,
            "trace",
        )
        assert old.attempt_id is not None
        await store.abort_conversation_turn(
            row.conversation_id, "user:ada", "aborted-attachment", old.attempt_id
        )
        with pytest.raises(LookupError, match="stale"):
            await store.claim_conversation_attachment(
                row.conversation_id,
                "user:ada",
                "eng",
                "console-chat",
                "aborted-attachment",
                fingerprint,
                "trace",
                preferred_attempt_id=old.attempt_id,
            )
        replacement = await store.claim_conversation_attachment(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "aborted-attachment",
            fingerprint,
            "trace",
        )
        assert replacement.disposition == "owned"
        assert replacement.attempt_id is not None and replacement.attempt_id != old.attempt_id

    asyncio.run(scenario())


def test_legacy_session_is_never_resolved_or_assigned_to_first_attributed_principal() -> None:
    store = _store()

    async def scenario() -> None:
        legacy = await store.open_session("console-chat", "eng")
        await store.append_turn(
            legacy,
            _private("legacy", "legacy-answer"),
            principal_id="user:ada",
            trace_id="legacy-trace",
        )
        assert await store.get_conversation(legacy, "user:ada", "eng") is None
        assert await store.list_conversations("user:ada", "eng") == ()

    asyncio.run(scenario())


def test_mixed_legacy_rows_survive_repeated_schema_ensure_without_assignment() -> None:
    store = _store()

    async def seed() -> tuple[str, list[tuple[object, ...]]]:
        legacy = await store.open_session("console-chat", "eng")
        for index, principal in enumerate((None, "user:ada", "user:bob"), start=1):
            await store.append_turn(
                legacy,
                _private(f"legacy-{index}", f"answer-{index}"),
                principal_id=principal,
                trace_id=None if principal is None else f"legacy-trace-{index}",
            )
        with store.engine.connect() as conn:
            before = [tuple(row) for row in conn.execute(sa.select(ne_agent_session_messages))]
        return legacy, before

    legacy, before = asyncio.run(seed())
    ensure_runtime_schema(store.engine)
    ensure_runtime_schema(store.engine)
    with store.engine.connect() as conn:
        after = [tuple(row) for row in conn.execute(sa.select(ne_agent_session_messages))]
        conversations = conn.execute(sa.select(sa.func.count()).select_from(ne_agent_conversations))
        assert conversations.scalar_one() == 0
    assert after == before
    page = asyncio.run(store.list_conversation_turns(limit=10))
    assert len(page.turns) == 3
    assert {turn.principal_id for turn in page.turns} == {None, "user:ada", "user:bob"}
    assert asyncio.run(store.get_conversation(legacy, "user:ada", "eng")) is None


def test_private_model_context_and_exact_served_output_are_distinct() -> None:
    store = _store()

    async def scenario() -> None:
        row = await store.create_conversation("console-chat", "eng", "user:ada")
        await store.append_conversation_turn(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            _private("question", "PRIVATE-DRAFT"),
            served_output="EXACT-SERVED-OUTPUT",
            trace_id="trace-1",
            attachment_id="turn-1",
            attempt_id="attempt-1",
        )
        await store.publish_conversation_turn(
            row.conversation_id, "user:ada", "turn-1", "attempt-1", []
        )
        history = await store.load_conversation_history(
            row.conversation_id, "user:ada", "eng", "console-chat"
        )
        assert "PRIVATE-DRAFT" in str(history)
        assert "EXACT-SERVED-OUTPUT" not in str(history)
        with store.engine.connect() as conn:
            turn = conn.execute(sa.select(ne_agent_conversation_turns)).one()
        assert turn.served_output == "EXACT-SERVED-OUTPUT"
        assert "PRIVATE-DRAFT" in turn.private_context

    asyncio.run(scenario())


def test_restart_preserves_owner_and_gapless_sequence(tmp_path) -> None:  # type: ignore[no-untyped-def]
    url = f"sqlite:///{tmp_path}/conversation.db"
    first = _store(url)

    async def write() -> str:
        row = await first.create_conversation("console-chat", "eng", "user:ada")
        for index in range(1, 4):
            sequence = await first.append_conversation_turn(
                row.conversation_id,
                "user:ada",
                "eng",
                "console-chat",
                _private(f"q{index}", f"a{index}"),
                served_output=f"a{index}",
                trace_id=f"trace-{index}",
                attachment_id=f"turn-{index}",
                attempt_id=f"attempt-{index}",
            )
            await first.publish_conversation_turn(
                row.conversation_id,
                "user:ada",
                f"turn-{index}",
                f"attempt-{index}",
                [],
            )
            assert sequence == index
        return row.conversation_id

    conversation_id = asyncio.run(write())
    first.engine.dispose()
    restarted = _store(url)
    owned = asyncio.run(restarted.get_conversation(conversation_id, "user:ada", "eng"))
    assert owned is not None
    assert owned.last_sequence == 3
    assert asyncio.run(restarted.get_conversation(conversation_id, "user:bob", "eng")) is None


def test_owner_scope_fk_and_read_predicates_reject_detached_turns() -> None:
    engine = sa.create_engine("sqlite://")
    # The enforcement hook must cover a connection that existed before schema
    # startup and is later reused from SQLite's pool.
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1")
    ensure_runtime_schema(engine)
    store = SqlAlchemySessionStore(engine)

    async def scenario() -> str:
        row = await store.create_conversation("console-chat", "eng", "user:ada")
        with pytest.raises(IntegrityError), store.engine.begin() as conn:
            conn.execute(
                ne_agent_conversation_turns.insert().values(
                    conversation_id=row.conversation_id,
                    sequence=1,
                    owner_principal_id="user:bob",
                    department="eng",
                    agent_name="console-chat",
                    trace_id="trace-detached",
                    attachment_id="turn-detached",
                    attempt_id="attempt-detached",
                    request_fingerprint="a" * 64,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                    private_context=json.dumps([]),
                    served_output="must-not-serve",
                    citation_refs="[]",
                    served_citations="[]",
                    served_verified="null",
                    served_grounded="null",
                    served_trace_link=False,
                    publication_state="published",
                    created_at=row.created_at,
                )
            )
        return row.conversation_id

    conversation_id = asyncio.run(scenario())
    assert (
        asyncio.run(
            store.load_conversation_history(conversation_id, "user:ada", "eng", "console-chat")
        )
        == []
    )


def test_schema_startup_rejects_lookalike_missing_named_constraints() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE ne_agent_conversation_turns")
        conn.exec_driver_sql(
            """
            CREATE TABLE ne_agent_conversation_turns (
              id INTEGER PRIMARY KEY,
              conversation_id VARCHAR NOT NULL,
              sequence INTEGER NOT NULL,
              owner_principal_id VARCHAR NOT NULL,
              department VARCHAR NOT NULL,
              agent_name VARCHAR NOT NULL,
              trace_id VARCHAR NOT NULL,
              attachment_id VARCHAR NOT NULL,
              attempt_id VARCHAR NOT NULL,
              request_fingerprint VARCHAR NOT NULL,
              lease_expires_at DATETIME NOT NULL,
              private_context TEXT NOT NULL,
              served_output TEXT NOT NULL,
              citation_refs TEXT NOT NULL,
              served_citations TEXT NOT NULL,
              served_verified TEXT NOT NULL,
              served_grounded TEXT NOT NULL,
              served_trace_link BOOLEAN NOT NULL,
              publication_state VARCHAR NOT NULL,
              created_at DATETIME NOT NULL
            )
            """
        )
    with pytest.raises(RuntimeError, match="incompatible"):
        ensure_runtime_schema(engine)


def _recreate_conversation_table(engine: sa.Engine, transform) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        create_sql = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ne_agent_conversations'"
        ).scalar_one()
        conn.exec_driver_sql("DROP TABLE ne_agent_conversation_turns")
        conn.exec_driver_sql("DROP TABLE ne_agent_conversations")
        conn.exec_driver_sql(transform(str(create_sql)))
        conn.exec_driver_sql(
            "CREATE INDEX ix_ne_agent_conversations_owner_principal_id "
            "ON ne_agent_conversations (owner_principal_id)"
        )


def test_schema_startup_rejects_forbidden_owner_tuple_singleton_unique() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)

    def add_forbidden_unique(create_sql: str) -> str:
        assert create_sql.rstrip().endswith(")")
        return (
            create_sql.rstrip()[:-1] + ", CONSTRAINT uq_forbidden_singleton UNIQUE "
            "(agent_name, department, owner_principal_id))"
        )

    _recreate_conversation_table(engine, add_forbidden_unique)
    with pytest.raises(RuntimeError, match="unique constraints"):
        ensure_runtime_schema(engine)


def test_schema_startup_rejects_same_named_weakened_check_expression() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)

    def weaken_check(create_sql: str) -> str:
        assert "last_sequence >= 0" in create_sql
        return create_sql.replace("last_sequence >= 0", "1 = 1")

    _recreate_conversation_table(engine, weaken_check)
    with pytest.raises(RuntimeError, match="checks"):
        ensure_runtime_schema(engine)


def test_schema_startup_rejects_same_named_weakened_partial_index() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX uq_agent_conversation_turn_active_attachment")
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_agent_conversation_turn_active_attachment "
            "ON ne_agent_conversation_turns (conversation_id, attachment_id) "
            "WHERE publication_state = 'published'"
        )
    with pytest.raises(RuntimeError, match="indexes"):
        ensure_runtime_schema(engine)


def test_schema_startup_rejects_uppercase_partial_index_literals() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX uq_agent_conversation_turn_active_attachment")
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_agent_conversation_turn_active_attachment "
            "ON ne_agent_conversation_turns (conversation_id, attachment_id) "
            "WHERE publication_state IN ('CLAIMED', 'PREPARED', 'PUBLISHED')"
        )
    with pytest.raises(RuntimeError, match="indexes"):
        ensure_runtime_schema(engine)


@pytest.mark.parametrize("changed_literal", ["prepared::text", "pre pared", 'pre"pared'])
def test_schema_startup_rejects_partial_index_literal_bytes(changed_literal: str) -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX uq_agent_conversation_turn_active_attachment")
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_agent_conversation_turn_active_attachment "
            "ON ne_agent_conversation_turns (conversation_id, attachment_id) "
            f"WHERE publication_state IN ('claimed', '{changed_literal}', 'published')"
        )
    with pytest.raises(RuntimeError, match="indexes"):
        ensure_runtime_schema(engine)


def _recreate_turn_table(engine: sa.Engine, transform) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        create_sql = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='ne_agent_conversation_turns'"
        ).scalar_one()
        conn.exec_driver_sql("DROP TABLE ne_agent_conversation_turns")
        conn.exec_driver_sql(transform(str(create_sql)))
        conn.exec_driver_sql(
            "CREATE INDEX ix_ne_agent_conversation_turns_conversation_id "
            "ON ne_agent_conversation_turns (conversation_id)"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_agent_conversation_turn_active_attachment "
            "ON ne_agent_conversation_turns (conversation_id, attachment_id) "
            "WHERE publication_state IN ('claimed', 'staged', 'prepared', 'published')"
        )


def test_schema_startup_rejects_uppercase_publication_check_literals() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)

    def uppercase_publication_literals(create_sql: str) -> str:
        expected = "publication_state IN ('claimed', 'staged', 'prepared', 'published', 'aborted')"
        assert expected in create_sql
        return create_sql.replace(
            expected,
            "publication_state IN ('CLAIMED', 'STAGED', 'PREPARED', 'PUBLISHED', 'ABORTED')",
        )

    _recreate_turn_table(engine, uppercase_publication_literals)
    with pytest.raises(RuntimeError, match="checks"):
        ensure_runtime_schema(engine)


@pytest.mark.parametrize("changed_literal", ["prepared::text", "pre pared", 'pre"pared'])
def test_schema_startup_rejects_publication_check_literal_bytes(changed_literal: str) -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)

    def change_publication_literal(create_sql: str) -> str:
        expected = "publication_state IN ('claimed', 'staged', 'prepared', 'published', 'aborted')"
        assert expected in create_sql
        return create_sql.replace(expected, expected.replace("prepared", changed_literal))

    _recreate_turn_table(engine, change_publication_literal)
    with pytest.raises(RuntimeError, match="checks"):
        ensure_runtime_schema(engine)


class _ForeignKeyInspectorOverride:
    def __init__(self, delegate: sa.Inspector, transform) -> None:  # type: ignore[no-untyped-def]
        self._delegate = delegate
        self._transform = transform

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._delegate, name)

    def get_foreign_keys(self, table_name: str, **kwargs):  # type: ignore[no-untyped-def]
        rows = [dict(row) for row in self._delegate.get_foreign_keys(table_name, **kwargs)]
        return self._transform(table_name, rows)


def test_schema_startup_rejects_owner_fk_targeting_same_table_name_in_wrong_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    real_inspect = sa.inspect

    def wrong_schema(table_name: str, rows: list[dict[str, object]]):
        if table_name == ne_agent_conversation_turns.name:
            assert len(rows) == 1
            rows[0]["referred_schema"] = "other_schema"
        return rows

    monkeypatch.setattr(
        sa,
        "inspect",
        lambda bind: _ForeignKeyInspectorOverride(real_inspect(bind), wrong_schema),
    )
    with pytest.raises(RuntimeError, match="foreign key"):
        ensure_runtime_schema(engine)


def test_schema_startup_rejects_unexpected_extra_foreign_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    real_inspect = sa.inspect

    def add_extra(table_name: str, rows: list[dict[str, object]]):
        if table_name == ne_agent_conversation_turns.name:
            rows.append(
                {
                    "name": "fk_unexpected_extra",
                    "constrained_columns": ["conversation_id"],
                    "referred_schema": None,
                    "referred_table": ne_agent_conversations.name,
                    "referred_columns": ["conversation_id"],
                }
            )
        return rows

    monkeypatch.setattr(
        sa,
        "inspect",
        lambda bind: _ForeignKeyInspectorOverride(real_inspect(bind), add_extra),
    )
    with pytest.raises(RuntimeError, match="foreign key"):
        ensure_runtime_schema(engine)


@pytest.mark.parametrize(("option", "value"), [("ondelete", "CASCADE"), ("onupdate", "CASCADE")])
def test_schema_startup_rejects_owner_fk_mutation_actions(
    monkeypatch: pytest.MonkeyPatch, option: str, value: str
) -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    real_inspect = sa.inspect

    def mutate_action(table_name: str, rows: list[dict[str, object]]):
        if table_name == ne_agent_conversation_turns.name:
            assert len(rows) == 1
            rows[0]["options"] = {option: value}
        return rows

    monkeypatch.setattr(
        sa,
        "inspect",
        lambda bind: _ForeignKeyInspectorOverride(real_inspect(bind), mutate_action),
    )
    with pytest.raises(RuntimeError, match="foreign key"):
        ensure_runtime_schema(engine)


@pytest.mark.parametrize(
    ("reflected", "declared"),
    [
        (
            "length(btrim((owner_principal_id)::text)) > 0",
            "length(trim(owner_principal_id)) > 0",
        ),
        (
            "length(TRIM(BOTH FROM department)) > 0",
            "length(trim(department)) > 0",
        ),
        (
            "(publication_state)::text = ANY "
            "((ARRAY['claimed'::character varying, 'prepared'::character varying, "
            "'published'::character varying, 'aborted'::character varying])::text[])",
            "publication_state IN ('claimed', 'prepared', 'published', 'aborted')",
        ),
        (
            "(publication_state)::text = ANY "
            "((ARRAY['claimed'::character varying, 'prepared'::character varying, "
            "'published'::character varying])::text[])",
            "publication_state IN ('claimed', 'prepared', 'published')",
        ),
    ],
)
def test_schema_expression_canonicalizer_accepts_postgres_deparser_equivalents(
    reflected: str, declared: str
) -> None:
    assert _canonical_schema_expression(reflected) == _canonical_schema_expression(declared)


def test_schema_expression_canonicalizer_keeps_weakened_postgres_predicates_distinct() -> None:
    declared = "publication_state IN ('claimed', 'prepared', 'published')"
    weakened = "(publication_state)::text = ANY ((ARRAY['published'::character varying])::text[])"
    assert _canonical_schema_expression(weakened) != _canonical_schema_expression(declared)


@pytest.mark.parametrize(
    ("uppercase", "declared"),
    [
        (
            "publication_state IN ('CLAIMED', 'PREPARED', 'PUBLISHED', 'ABORTED')",
            "publication_state IN ('claimed', 'prepared', 'published', 'aborted')",
        ),
        (
            "(publication_state)::text = ANY "
            "((ARRAY['CLAIMED'::character varying, 'PREPARED'::character varying, "
            "'PUBLISHED'::character varying])::text[])",
            "publication_state IN ('claimed', 'prepared', 'published')",
        ),
    ],
)
def test_schema_expression_canonicalizer_preserves_literal_case(
    uppercase: str, declared: str
) -> None:
    assert _canonical_schema_expression(uppercase) != _canonical_schema_expression(declared)


@pytest.mark.parametrize("changed_literal", ["prepared::text", "pre pared", 'pre"pared'])
def test_schema_expression_canonicalizer_preserves_every_literal_byte(
    changed_literal: str,
) -> None:
    declared = "publication_state IN ('claimed', 'prepared', 'published')"
    changed = declared.replace("prepared", changed_literal)
    assert _canonical_schema_expression(changed) != _canonical_schema_expression(declared)


@pytest.mark.parametrize(
    "declared",
    [
        "length(trim(owner_principal_id)) > 0",
        "length(trim(request_fingerprint)) = 64",
    ],
)
def test_schema_expression_canonicalizer_preserves_function_identity_and_nesting(
    declared: str,
) -> None:
    malformed = declared.replace("length(trim(", "lengthtrim(").replace("))", ")")
    assert _canonical_schema_expression(malformed) != _canonical_schema_expression(declared)


def test_cancel_during_blocking_publish_is_compensated_before_history_visibility(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    engine = sa.create_engine(
        f"sqlite:///{tmp_path}/publish-race.db", connect_args={"check_same_thread": False}
    )
    ensure_runtime_schema(engine)
    store = SqlAlchemySessionStore(engine, offload_db_io=True)
    entered = threading.Event()
    release = threading.Event()
    original = store._set_conversation_turn_state_sync

    def blocked_publish(
        conversation_id: str,
        owner: str,
        attachment: str,
        attempt: str,
        state: str,
        citation_refs: str | None,
        citation_payloads: str | None,
        publish_cancelled: threading.Event | None,
    ) -> None:
        if state == "published":
            entered.set()
            assert release.wait(timeout=5)
        original(
            conversation_id,
            owner,
            attachment,
            attempt,
            state,
            citation_refs,
            citation_payloads,
            publish_cancelled,
        )

    monkeypatch.setattr(store, "_set_conversation_turn_state_sync", blocked_publish)

    async def scenario() -> None:
        row = await store.create_conversation("console-chat", "eng", "user:ada")
        await store.append_conversation_turn(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            _private("question", "private"),
            served_output="served",
            trace_id="trace-race",
            attachment_id="turn-race",
            attempt_id="attempt-race",
        )
        publish = asyncio.create_task(
            store.publish_conversation_turn(
                row.conversation_id, "user:ada", "turn-race", "attempt-race", []
            )
        )
        assert await asyncio.to_thread(entered.wait, 5)
        publish.cancel()
        with pytest.raises(asyncio.CancelledError):
            await publish
        abort = asyncio.create_task(
            store.abort_conversation_turn(
                row.conversation_id, "user:ada", "turn-race", "attempt-race"
            )
        )
        release.set()
        await abort
        history = await store.load_conversation_history(
            row.conversation_id, "user:ada", "eng", "console-chat"
        )
        assert history == []
        assert not await store.conversation_has_attachment(
            row.conversation_id,
            "user:ada",
            "eng",
            "console-chat",
            "turn-race",
        )

    asyncio.run(scenario())
