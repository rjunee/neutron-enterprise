# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The grounded agent's surface is bounded by the writer it executes on.

The grounded analyst runs on the LEGACY session path — the recorder-bound
verified-read toolset, the whole-answer gate in
``ne.runtime.grounding.run_grounding_gate``, the withheld-notice fallback and the
``GroundedProvenance`` on the ``done`` frame all live in
``DepartmentAgentSession.run``. The canonical Run path composes no
``GroundingPolicy``, has no grounded Agent in the catalog and no grounded
``ExecutionStrategy``.

PRE-010G retired the legacy Conversation attachment writer *in the database*:
``ne_block_legacy_turn_when_default_ask_active`` (PostgreSQL) and its SQLite twin,
installed by ``ne.runtime.run_store._install_default_ask_fence``, are BEFORE INSERT
triggers on ``ne_agent_conversation_turns`` with no exemptions. NE-GA-G8 read that
retirement as an API-level policy the grounded name could be exempted from and
routed grounded turns back onto the legacy writer; every one of them then died on
the trigger with a 500.

So the boundary these tests pin is the trigger's own: the grounded surface exists
exactly while the legacy writer does. Under canonical activation it is advertised
to nobody and admitted from nobody. It is NOT aliased onto canonical Ask — serving
an ungrounded answer under a control labelled "Grounding on" is the one outcome
worse than the capability being absent.
"""

from __future__ import annotations

import asyncio
from dataclasses import fields
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from ne.api.deps import ApiState
from ne.api.routers import chat
from ne.api.wire import ChatMessageRequest, CreateSessionRequest
from ne.contracts.types import Principal
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_store import AgentRunStore, ensure_run_schema
from ne.runtime.store import (
    PrincipalConversation,
    SqlAlchemySessionStore,
    ensure_runtime_schema,
    ne_agent_conversation_turns,
)

from .test_default_ask_submission import (  # reuse the exact canonical-Ask doubles
    AGENT_ID,
    CONFIG_HASH,
    OWNER,
    _CanonicalAskConversationStore,
    _Catalog,
    _ChatPreflight,
    _RunEnqueuer,
    _service,
)

GROUNDED = "grounded-analyst"
ORDINARY = "console-chat"
WIRE_GENERATION = "default-ask/canonical-run/v1"
RETIRED = "legacy Conversation attachment writer is retired"


class _Grounding:
    """Stand-in for the composed GroundingPolicy. Presence is the only signal the
    chat boundary reads; the policy itself is exercised by the runtime suites."""


def _state(
    *,
    service: Any,
    store: Any,
    grounding: object | None,
    grounded_agent_name: str | None,
    chat_agents: tuple[str, ...] = (ORDINARY, GROUNDED),
    enqueuer: Any | None = None,
) -> Any:
    return SimpleNamespace(
        default_ask_submission=service,
        session_store=store,
        grounding=grounding,
        grounded_agent_name=grounded_agent_name,
        settings=SimpleNamespace(
            chat_agents=chat_agents,
            sse_keepalive_s=15.0,
            turn_timeout_s=120.0,
        ),
        agent_catalog=_Catalog(),
        agent_preflight=_ChatPreflight(),
        config_hash=CONFIG_HASH,
        agent_run_enqueuer=enqueuer if enqueuer is not None else _RunEnqueuer(),
    )


class _GroundedConversationStore(_CanonicalAskConversationStore):
    """The stored Conversation is owned by the grounded agent, not canonical Ask."""

    def __init__(self) -> None:
        super().__init__(agent_name=GROUNDED)


def _fenced_stores(tmp_path: Any, *, agent_name: str) -> tuple[SqlAlchemySessionStore, Any, Any]:
    """A REAL runtime+Run schema with the activation fence installed and armed.

    No double stands in for the trigger here: this is the same
    ``_install_default_ask_fence`` DDL the deployment runs, on the same tables, so
    a legacy write attempted through it fails exactly the way production failed.
    """
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'fenced.db'}")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    sessions = SqlAlchemySessionStore(engine)
    conversation = asyncio.run(sessions.create_conversation(agent_name, "operations", OWNER.id))
    runs = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
        ),
    )
    asyncio.run(runs.activate_default_ask(WIRE_GENERATION))
    return sessions, conversation, engine


def test_api_state_declares_the_two_fields_the_boundary_reads() -> None:
    """The boundary reads composed config, never a getattr default: if either
    field is renamed away, this fails instead of silently disabling grounding."""

    declared = {field.name for field in fields(ApiState)}
    assert {"grounding", "grounded_agent_name"} <= declared


def test_a_grounded_submission_never_reaches_the_retired_legacy_writer(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE regression. A grounded Conversation submitted while canonical default
    Ask is active must never attempt a legacy turn write.

    ``resolve_session`` is replaced by the legacy path's FIRST durable act —
    ``claim_conversation_attachment`` against the real fenced store — so if the
    boundary diverts a grounded Conversation onto the legacy writer the trigger
    raises exactly the production error instead of a tidy assertion. With the
    diversion removed the grounded Conversation is simply not the canonical one:
    an unknown session, and not one row on ``ne_agent_conversation_turns``.
    """

    sessions, conversation, engine = _fenced_stores(tmp_path, agent_name=GROUNDED)
    service, transactions = _service()
    enqueuer = _RunEnqueuer()

    async def _resolve_session(
        session_id: str, principal_id: str, department: str, _state: Any
    ) -> Any:
        await sessions.claim_conversation_attachment(
            session_id, principal_id, department, GROUNDED, "turn-legacy", "a" * 64, "trace-legacy"
        )
        raise AssertionError("unreachable: the activation fence must have raised")

    monkeypatch.setattr(chat, "resolve_session", _resolve_session)
    state = _state(
        service=service,
        store=sessions,
        grounding=_Grounding(),
        grounded_agent_name=GROUNDED,
        enqueuer=enqueuer,
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            chat.send_message(
                conversation.conversation_id,
                ChatMessageRequest(prompt="how many disputes are open?"),
                OWNER,
                cast(Any, state),
                "ask-once",
            )
        )

    assert raised.value.status_code == 404
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_conversation_turns)) == 0
    # Nor was it quietly served as an ordinary canonical Ask turn.
    assert transactions.calls == []
    assert enqueuer.requests == []


def test_the_retired_legacy_writer_still_refuses_a_genuine_grounded_write(
    tmp_path: Any,
) -> None:
    """Paired mutation for the test above: its 404 must be the boundary refusing,
    not the fence having gone missing. Attempt the identical legacy claim directly
    and the trigger must still abort it."""

    sessions, conversation, engine = _fenced_stores(tmp_path, agent_name=GROUNDED)

    with pytest.raises(Exception, match=RETIRED):
        asyncio.run(
            sessions.claim_conversation_attachment(
                conversation.conversation_id,
                OWNER.id,
                "operations",
                GROUNDED,
                "turn-legacy",
                "a" * 64,
                "trace-legacy",
            )
        )

    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_conversation_turns)) == 0


def test_capabilities_stop_advertising_grounding_once_canonical_ask_is_active() -> None:
    """Composed policy + admitted name are no longer sufficient: the writer the
    mode executes on is retired, so the console must be offered nothing."""

    service, _ = _service()
    state = _state(
        service=service,
        store=_CanonicalAskConversationStore(),
        grounding=_Grounding(),
        grounded_agent_name=GROUNDED,
    )

    view = asyncio.run(chat.read_chat_capabilities(OWNER, cast(Any, state)))

    assert view.grounded is None


def test_capabilities_still_advertise_grounding_where_the_legacy_writer_lives() -> None:
    """Paired mutation: grounding is bounded by the fence, not deleted. A
    deployment that has NOT activated canonical default Ask still runs the legacy
    writer, so the grounded mode is still executable and still advertised."""

    state = _state(
        service=None,
        store=_CanonicalAskConversationStore(),
        grounding=_Grounding(),
        grounded_agent_name=GROUNDED,
    )

    view = asyncio.run(chat.read_chat_capabilities(OWNER, cast(Any, state)))

    assert view.grounded is not None
    assert view.grounded.agent_name == GROUNDED


@pytest.mark.parametrize(
    ("grounding", "grounded_agent_name", "chat_agents"),
    [
        (None, GROUNDED, (ORDINARY, GROUNDED)),
        (_Grounding(), None, (ORDINARY, GROUNDED)),
        (_Grounding(), GROUNDED, (ORDINARY,)),
    ],
    ids=["policy-not-composed", "no-configured-name", "name-not-admitted"],
)
def test_capabilities_stay_silent_on_every_partial_grounded_composition(
    grounding: object | None,
    grounded_agent_name: str | None,
    chat_agents: tuple[str, ...],
) -> None:
    """Advertising a mode ``create_session`` would refuse is the failure this
    endpoint exists to prevent; it must hold on the un-fenced deployment too."""

    state = _state(
        service=None,
        store=_CanonicalAskConversationStore(),
        grounding=grounding,
        grounded_agent_name=grounded_agent_name,
        chat_agents=chat_agents,
    )

    view = asyncio.run(chat.read_chat_capabilities(OWNER, cast(Any, state)))

    assert view.grounded is None


def test_a_grounded_agent_name_is_refused_rather_than_aliased_onto_canonical_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anti-downgrade guard. Under canonical activation the grounded name must
    422 — it must NOT be quietly minted as a canonical Ask Conversation, which
    would serve an ungrounded answer to a caller that asked for a grounded one."""

    service, transactions = _service()
    store = _CanonicalAskConversationStore()

    async def _open(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the legacy grounded session must not be opened")

    monkeypatch.setattr(chat, "_open", _open)
    state = _state(
        service=service,
        store=store,
        grounding=_Grounding(),
        grounded_agent_name=GROUNDED,
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            chat.create_session(CreateSessionRequest(agent_name=GROUNDED), OWNER, cast(Any, state))
        )

    assert raised.value.status_code == 422
    assert store.created == []
    assert transactions.calls == []


def test_a_grounded_session_still_opens_where_the_legacy_writer_lives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paired mutation for the refusal above: the grounded session path itself is
    intact and still threads the composed policy — only the fenced deployment
    withholds it."""

    store = _CanonicalAskConversationStore()
    opened: list[tuple[str, str, str]] = []

    async def _open(agent_name: str, department: str, owner_principal_id: str, _state: Any) -> Any:
        opened.append((agent_name, department, owner_principal_id))
        return SimpleNamespace(session_id="grounded-session", agent_name=agent_name)

    monkeypatch.setattr(chat, "_open", _open)
    state = _state(
        service=None,
        store=store,
        grounding=_Grounding(),
        grounded_agent_name=GROUNDED,
    )

    view = asyncio.run(
        chat.create_session(CreateSessionRequest(agent_name=GROUNDED), OWNER, cast(Any, state))
    )

    assert opened == [(GROUNDED, "operations", OWNER.id)]
    assert view.agent_name == GROUNDED
    assert store.created == []


def test_ordinary_ask_still_mints_only_the_canonical_conversation() -> None:
    """Removing the grounded diversion must not move canonical Ask one byte."""

    service, _ = _service()
    store = _CanonicalAskConversationStore(agent_name=ORDINARY)
    state = _state(
        service=service,
        store=store,
        grounding=_Grounding(),
        grounded_agent_name=GROUNDED,
    )

    view = asyncio.run(
        chat.create_session(CreateSessionRequest(agent_name=ORDINARY), OWNER, cast(Any, state))
    )

    assert store.created == [(AGENT_ID, "operations", OWNER.id)]
    assert view.agent_name == AGENT_ID


def test_a_third_agent_name_is_still_refused_under_canonical_ask() -> None:
    service, _ = _service()
    state = _state(
        service=service,
        store=_CanonicalAskConversationStore(),
        grounding=_Grounding(),
        grounded_agent_name=GROUNDED,
        chat_agents=(ORDINARY, GROUNDED, "some-other-agent"),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            chat.create_session(
                CreateSessionRequest(agent_name="some-other-agent"), OWNER, cast(Any, state)
            )
        )

    assert raised.value.status_code == 422


def test_a_retained_grounded_conversation_is_an_unknown_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Conversation minted under the grounded name BEFORE activation must not
    fall through to the legacy writer once the fence is armed — it is an unknown
    session, whether or not the policy is still composed."""

    service, transactions = _service()

    async def _resolve_session(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the legacy path must not be reached")

    monkeypatch.setattr(chat, "resolve_session", _resolve_session)
    state = _state(
        service=service,
        store=_GroundedConversationStore(),
        grounding=None,
        grounded_agent_name=GROUNDED,
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            chat.send_message(
                "conversation-owned",
                ChatMessageRequest(prompt="how many disputes are open?"),
                OWNER,
                cast(Any, state),
                "ask-once",
            )
        )

    assert raised.value.status_code == 404
    assert transactions.calls == []


def test_unknown_principal_conversations_are_still_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing row must never be read as "not canonical, therefore grounded"."""

    service, transactions = _service()

    async def _resolve_session(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the legacy path must not be reached")

    monkeypatch.setattr(chat, "resolve_session", _resolve_session)
    state = _state(
        service=service,
        store=_CanonicalAskConversationStore(),
        grounding=_Grounding(),
        grounded_agent_name=GROUNDED,
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            chat.send_message(
                "someone-elses-conversation",
                ChatMessageRequest(prompt="how many disputes are open?"),
                OWNER,
                cast(Any, state),
                "ask-once",
            )
        )

    assert raised.value.status_code == 404
    assert transactions.calls == []


def test_principal_conversation_row_shape_is_the_one_the_boundary_reads() -> None:
    """Pin the exact attribute the canonical branch keys off, so a store-model
    rename cannot silently turn every Conversation into a canonical-Ask turn."""

    assert "agent_name" in {field.name for field in fields(PrincipalConversation)}
    assert isinstance(OWNER, Principal)
