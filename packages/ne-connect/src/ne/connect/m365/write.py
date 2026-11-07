# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin Microsoft Graph WRITE client (NE-050 / NE-CS-SEND) — drafts, and governed sends.

:class:`GraphWriteClient` wraps a CONSTRUCTED :class:`~ne.connect.m365.graph.GraphClient`
so it inherits every C2 / SSRF / air-gap construction guarantee — it adds NO egress
surface of its own and imports NO httpx / socket (the C2 egress guardian's allowlist
stays ``{m365/graph.py, salesforce/client.py, slack/client.py}``; this module relies on
type inference from ``GraphClient.session()``).

Creating a message via ``POST /users/{mailbox}/messages`` produces a DRAFT
(``isDraft`` true) — it is NEVER sent by that call. NE-CS-SEND (owner decision
2026-08-27) adds the second half: :meth:`read_draft_recipients` reads back an
EXISTING draft's recipient set (so the governed send action can enforce its
recipient allowlist against what Graph will actually deliver to — never against a
caller-supplied claim), and :meth:`send_draft` performs the real
``POST /users/{mailbox}/messages/{id}/send``. The send is REAL egress and is only
ever reached through the governed Tier-2 ``write.external.email.send`` action —
this client never decides to send on its own. The mailbox is validated fail-closed
(a UPN email OR an Entra object GUID) BEFORE any URL is built; a
:class:`GraphSyncError` names only the FIELD on failure, never the value (C12).
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from ne.connect.errors import GraphSyncError
from ne.connect.m365.graph import GraphClient

__all__ = ["GraphWriteClient"]

_MAILBOX_RE = re.compile(
    r"^(?:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+"
    r"|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$"
)


class GraphWriteClient:
    """Create unsent M365 email drafts over a constructed :class:`GraphClient`."""

    def __init__(self, graph: GraphClient) -> None:
        self._graph = graph

    async def create_draft(self, *, mailbox: str, subject: str, body: str, to: str) -> str:
        """Create a draft message in ``mailbox`` and return its Graph message id.

        Creating a message this way IS a draft (``isDraft`` true) — it is never sent.
        The mailbox is validated fail-closed before any URL construction or token
        spend; the subject / body / recipient travel only in the JSON body.
        """
        if not _MAILBOX_RE.match(mailbox):
            # Name ONLY the field — never the value (C12 redaction).
            raise GraphSyncError("refusing to create a draft for a malformed mailbox")
        url = f"{self._graph.graph_base}/users/{urllib.parse.quote(mailbox, safe='')}/messages"
        payload: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        }
        async with self._graph.session() as http:
            data = await self._graph.post_json(http, url, payload)
        message_id = data.get("id")
        if data.get("isDraft") is not True or not isinstance(message_id, str) or not message_id:
            raise GraphSyncError("Graph did not attest creation of an unsent draft")
        return message_id

    def _message_url(self, mailbox: str, message_id: str) -> str:
        """The guarded per-message URL — mailbox validated fail-closed FIRST (C12)."""
        if not _MAILBOX_RE.match(mailbox):
            raise GraphSyncError("refusing to address a malformed mailbox")
        if not message_id or message_id != message_id.strip():
            raise GraphSyncError("refusing to address a malformed draft reference")
        return (
            f"{self._graph.graph_base}/users/{urllib.parse.quote(mailbox, safe='')}"
            f"/messages/{urllib.parse.quote(message_id, safe='')}"
        )

    async def read_draft_recipients(self, *, mailbox: str, message_id: str) -> tuple[str, ...]:
        """Read back an EXISTING message's recipient addresses, refusing non-drafts.

        The governed send action calls this FIRST so its recipient allowlist is
        enforced against what Graph itself says the message addresses — a
        caller-supplied recipient claim never enters the decision. A message that
        is not a draft (already sent, or arbitrary mailbox content) is refused
        here, before any send URL exists.
        """
        url = (
            self._message_url(mailbox, message_id)
            + "?$select=isDraft,toRecipients,ccRecipients,bccRecipients"
        )
        async with self._graph.session() as http:
            data = await self._graph.get_json(http, url)
        if data.get("isDraft") is not True:
            raise GraphSyncError("refusing to send a message that is not an unsent draft")
        recipients: list[str] = []
        for field in ("toRecipients", "ccRecipients", "bccRecipients"):
            entries = data.get(field) or []
            if not isinstance(entries, list):
                raise GraphSyncError("Graph returned a malformed recipient list")
            for entry in entries:
                address = (
                    entry.get("emailAddress", {}).get("address")
                    if isinstance(entry, dict)
                    else None
                )
                if not isinstance(address, str) or not address:
                    raise GraphSyncError("Graph returned a malformed recipient entry")
                recipients.append(address.strip().casefold())
        return tuple(recipients)

    async def send_draft(self, *, mailbox: str, message_id: str) -> None:
        """REAL egress: send an existing draft (``POST .../messages/{id}/send``).

        Only the governed Tier-2 ``write.external.email.send`` action reaches
        this, AFTER the durable human approval and AFTER
        :meth:`read_draft_recipients` verified the draft's actual recipient set
        against the deployment's send allowlist. Graph attests success with an
        empty ``202 Accepted``.
        """
        url = self._message_url(mailbox, message_id) + "/send"
        async with self._graph.session() as http:
            await self._graph.post_accepted(http, url, None)
