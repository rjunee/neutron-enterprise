# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ChatIdentityLinker — fail-closed Slack-user → real Principal mapping.

Channel-agnostic (Teams reuses it): given an explicit admin-configured
``{external_user_id: platform_subject}`` map plus the FROZEN
``ne.contracts.identity.PrincipalResolver``, resolve an external chat user to a
real ``Principal``. There is NO code path that fabricates an anonymous or ambient
principal:

* an unmapped external user ⇒ ``chat.identity.denied`` audit + ``UnmappedChatUserError``;
* a mapped-but-unresolvable subject (resolver raises for any reason) ⇒ the same
  denial audit (reason ``unresolvable``, error CLASS name only) + ``UnmappedChatUserError``.

Audit appends here are fail-closed: an append failure raises
:class:`~ne.chat.errors.ChatAuditError` (the result is withheld). Actor
namespacing: unmapped/unresolved actors audit as ``"<surface>:<external_id>"``
(an explicit non-principal namespace); mapped flows audit ``actor=principal.id``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from ne.chat.errors import ChatAuditError, UnmappedChatUserError
from ne.contracts.governance import AuditSink
from ne.contracts.identity import PrincipalResolver
from ne.contracts.types import Principal
from ne.events import new_audit_event
from ne.schema import AuditOutcome

__all__ = ["ChatIdentityLinker"]


class ChatIdentityLinker:
    """Resolve an external chat user id to a real Principal, fail closed."""

    def __init__(
        self,
        links: Mapping[str, str],
        resolver: PrincipalResolver,
        sink: AuditSink,
        *,
        surface: str = "slack",
    ) -> None:
        self._links = dict(links)
        self._resolver = resolver
        self._sink = sink
        self._surface = surface

    @property
    def surface(self) -> str:
        return self._surface

    async def resolve(self, external_user_id: str) -> Principal:
        """Map ``external_user_id`` to a Principal, or deny+audit and raise."""
        subject = self._links.get(external_user_id)
        if subject is None:
            await self._deny(external_user_id, reason="unmapped")
            raise UnmappedChatUserError(
                f"{self._surface} user is not linked to a platform identity"
            )
        try:
            return await self._resolver.resolve(subject)
        except Exception as exc:
            await self._deny(
                external_user_id, reason="unresolvable", error_class=type(exc).__name__
            )
            raise UnmappedChatUserError(
                f"{self._surface} user maps to an unresolvable subject"
            ) from exc

    async def _deny(
        self, external_user_id: str, *, reason: str, error_class: str | None = None
    ) -> None:
        detail: dict[str, str] = {"reason": reason}
        if error_class is not None:
            detail["error_class"] = error_class
        event = new_audit_event(
            action="chat.identity.denied",
            resource=f"chat:{self._surface}",
            outcome=AuditOutcome.denied,
            actor=f"{self._surface}:{external_user_id}",
            detail=json.dumps(detail),
            provenance={
                "source": "ne-chat",
                "component": "identity",
                "surface": self._surface,
            },
        )
        try:
            await self._sink.append(event)
        except Exception as exc:
            raise ChatAuditError(
                "audit append failed for chat.identity.denied; result withheld (fail closed)"
            ) from exc
