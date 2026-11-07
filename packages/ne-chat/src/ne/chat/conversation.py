# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ConversationService — a Slack message becomes a fully-GOVERNED agent turn.

Channel-agnostic (Teams reuses it). A chat message can ONLY become an agent turn
via the injected ``session_factory`` that yields a frozen
``ne.contracts.runtime.AgentSession`` whose tools are closures over the real
``GovernedToolExecutor`` the composition root wired. ne-chat itself holds NO tool
handlers, NO PDP, NO approval gate, and NO model gateway: every tool call inside
the session flows through the guardrails→PDP→HITL→audit chokepoint. C6 is
therefore satisfied BY CONSTRUCTION — there is no ungoverned agent path from Slack.

No turn-level audit event is emitted HERE: the governed layers
(session→executor→PDP→gate→sink) already own the C6 audit record for the turn.
Emitting another one would double-count and risk diverging from the authoritative
record.

Failure discipline (fail closed): an UNAUDITABLE turn must never look successful,
so ``ChatAuditError`` / ``RuntimeAuditError`` / ``AuditEmissionError`` PROPAGATE
(after the placeholder is replaced with a generic failure notice). Any other
exception is turned into a generic failure notice and re-raised wrapped as a
:class:`~ne.chat.errors.ChatError` carrying only the error CLASS name (C12).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Protocol

from ne.chat.errors import ChatError, UnmappedChatUserError
from ne.chat.identity import ChatIdentityLinker
from ne.contracts.governance import AuditSink
from ne.contracts.runtime import AgentSession, InboundMessage
from ne.contracts.types import Citation, Principal
from ne.govern.errors import AuditEmissionError
from ne.runtime.errors import RuntimeAuditError

__all__ = ["ConversationService", "SessionFactory", "Streamer"]

#: A composition-root-supplied factory that builds a governed AgentSession bound
#: to a principal (the C6 seam: the session's tools close over the real executor).
type SessionFactory = Callable[[Principal], Awaitable[AgentSession]]

_UNAUDITABLE = (RuntimeAuditError, AuditEmissionError)
_DENIAL_TEXT = "You are not linked to a platform identity; contact your admin."


class Streamer(Protocol):
    """The streaming seam the ConversationService drives (SlackStreamer duck-types this)."""

    async def start(self, channel: str) -> str: ...

    async def stream_into(
        self,
        channel: str,
        ts: str,
        chunks: AsyncIterator[str],
        *,
        citations: Sequence[Citation] = (),
    ) -> None: ...

    async def fail(self, channel: str, ts: str) -> None: ...

    async def ephemeral(self, channel: str, user: str, text: str) -> None: ...


async def _single(text: str) -> AsyncIterator[str]:
    """Yield ``text`` as one chunk (today's runtime is not token-streaming)."""
    yield text


class ConversationService:
    """Route a Slack message through the governed runtime and stream the reply."""

    def __init__(
        self,
        linker: ChatIdentityLinker,
        session_factory: SessionFactory,
        streamer: Streamer,
        sink: AuditSink,
    ) -> None:
        self._linker = linker
        self._session_factory = session_factory
        self._streamer = streamer
        self._sink = sink

    async def handle(self, message: InboundMessage) -> None:
        """Handle one inbound chat message end-to-end (identity → governed turn → stream)."""
        try:
            principal = await self._linker.resolve(message.sender)
        except UnmappedChatUserError:
            # No agent access for an unmapped user — already audited by the linker.
            await self._streamer.ephemeral(message.channel, message.sender, _DENIAL_TEXT)
            return

        ts = await self._streamer.start(message.channel)
        try:
            session = await self._session_factory(principal)
            result = await session.run(message.text, principal=principal)
        except _UNAUDITABLE:
            # An unauditable turn must not look successful: notify generically, then
            # PROPAGATE so the failure is not swallowed (fail closed).
            await self._streamer.fail(message.channel, ts)
            raise
        except Exception as exc:
            await self._streamer.fail(message.channel, ts)
            raise ChatError(f"chat agent turn failed ({type(exc).__name__})") from exc

        await self._streamer.stream_into(message.channel, ts, _single(result.output))
