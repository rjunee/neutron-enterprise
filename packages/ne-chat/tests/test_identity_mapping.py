# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ChatIdentityLinker — fail-closed Slack-user → Principal mapping (real resolver)."""

from __future__ import annotations

import asyncio

import pytest

from ne.chat.errors import ChatAuditError, UnmappedChatUserError
from ne.chat.identity import ChatIdentityLinker
from ne.identity.provisioning import InMemoryPrincipalStore, JitPrincipalResolver

from .conftest import FailingAuditSink, RecordingAuditSink, make_linker, principal


def test_mapped_slack_user_resolves_real_principal() -> None:
    sink = RecordingAuditSink()

    async def scenario() -> None:
        linker = await make_linker(
            sink,
            links={"U0ADA00000": "user:ada"},
            principals=[principal("user:ada", groups=("eng",))],
        )
        resolved = await linker.resolve("U0ADA00000")
        assert resolved.id == "user:ada"
        assert resolved.groups == ("eng",)

    asyncio.run(scenario())
    assert sink.events == []  # a successful mapping is not a denial


def test_unmapped_slack_user_denied_and_audited() -> None:
    sink = RecordingAuditSink()

    async def scenario() -> None:
        linker = await make_linker(sink, links={})
        with pytest.raises(UnmappedChatUserError):
            await linker.resolve("U0INTRUDER")

    asyncio.run(scenario())
    denied = sink.with_action("chat.identity.denied")
    assert len(denied) == 1
    assert denied[0].actor == "slack:U0INTRUDER"
    assert denied[0].outcome == "denied"
    assert "unmapped" in (denied[0].detail or "")


def test_mapped_but_unresolvable_subject_fails_closed() -> None:
    sink = RecordingAuditSink()

    async def scenario() -> None:
        # Link present, but the subject was never provisioned into the store.
        linker = await make_linker(sink, links={"U0ADA00000": "user:ghost"})
        with pytest.raises(UnmappedChatUserError):
            await linker.resolve("U0ADA00000")

    asyncio.run(scenario())
    denied = sink.with_action("chat.identity.denied")
    assert len(denied) == 1
    assert "unresolvable" in (denied[0].detail or "")
    # error CLASS name only (no principal fabricated).
    assert "UnknownPrincipalError" in (denied[0].detail or "")


def test_denial_audit_append_failure_raises_chat_audit_error() -> None:
    sink = FailingAuditSink()

    async def scenario() -> None:
        linker = ChatIdentityLinker({}, JitPrincipalResolver(InMemoryPrincipalStore()), sink)
        with pytest.raises(ChatAuditError):
            await linker.resolve("U0INTRUDER")

    asyncio.run(scenario())
