# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Governed write-back action: SEND an existing M365 email draft (Tier-2, REAL egress).

NE-CS-SEND (owner decision 2026-08-27): the demo's approval beat is a real send.
This action takes the ``draft_ref`` a prior governed ``draft.email.create`` call
returned and performs the real Microsoft Graph
``POST /users/{mailbox}/messages/{id}/send``. The action NAME is
``write.external.email.send``, so ``tiers.rego`` (and its Python mirror) classify
it Tier-2 from the ``write.external.`` prefix alone — every call parks on the
durable human-approval gate before the handler runs, and a rejected approval
never reaches the transport (the ``test_action_write_guardian`` discipline).

TWO fail-closed allowlists run INSIDE ``perform``, after approval, before egress:

* the sending ``mailbox`` must be one of ``allowed_mailboxes`` (the same
  deployment allowlist the draft action enforces);
* the draft's ACTUAL recipient set — read back from Graph itself via
  ``read_draft_recipients``, never taken from a caller claim — must be non-empty
  and a subset of ``allowed_recipients``. An empty configured allowlist refuses
  every send (deny-by-default).

Why the recipient check is here and not in the recipients guardrail: the
guardrail validates ARGUMENT-extracted recipients, and this action's only
argument is an opaque draft reference. The address Graph would deliver to lives
in the draft, so the enforcement reads it from the system of record.

The connector seam is the narrow :class:`SendMailClient` structural Protocol
(matched by ``ne.connect.m365.write.GraphWriteClient``); this module imports NO
httpx / sqlalchemy / ``ne.connect``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import Field

from ne.contracts.integration import ActionResult
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.action_tools import ActionDefinition, ActionInput

__all__ = [
    "EMAIL_SEND_ARG_POLICY",
    "EMAIL_SEND_TOOL",
    "EmailSendInput",
    "SendMailClient",
    "email_send_definition",
]

EMAIL_SEND_TOOL = "write.external.email.send"

#: A mailbox is either a UPN-style email OR a 36-char Entra object GUID
#: (mirrors ``email_draft``'s pattern exactly).
_MAILBOX_PATTERN = (
    r"^(?:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+"
    r"|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$"
)


class EmailSendInput(ActionInput):
    """Typed input for the governed send (frozen, reject-by-default)."""

    #: The mailbox holding the draft — a UPN email or an Entra object GUID.
    #: Optional for the same reason as the draft action's: NE-162 keeps mailbox
    #: addresses out of frontier-visible text, so the deployment default (the
    #: sole allowlisted mailbox) is used when omitted.
    mailbox: str | None = Field(default=None, pattern=_MAILBOX_PATTERN)
    #: The Graph message id ``draft.email.create`` returned for the draft.
    draft_ref: str = Field(min_length=1, max_length=512)


class SendMailClient(Protocol):
    """The narrow send seam: read a draft's real recipients, then send it."""

    async def read_draft_recipients(self, *, mailbox: str, message_id: str) -> tuple[str, ...]: ...

    async def send_draft(self, *, mailbox: str, message_id: str) -> None: ...


def email_send_definition(
    client: SendMailClient,
    *,
    allowed_mailboxes: Sequence[str],
    allowed_recipients: Sequence[str],
    default_mailbox: str | None = None,
) -> ActionDefinition:
    """Build the governed email-send :class:`ActionDefinition` over ``client``.

    Both allowlists are exact normalized addresses. An empty RECIPIENT allowlist
    refuses every call (the composition root additionally refuses to register the
    tool at all without one — deny-by-default is structural). An empty MAILBOX
    allowlist means the connector credential is the mailbox authority — the same
    enabled-mode semantics the draft action has; when non-empty it is enforced
    exactly.
    """
    mailbox_allowlist = frozenset(entry.strip().casefold() for entry in allowed_mailboxes)
    recipient_allowlist = frozenset(entry.strip().casefold() for entry in allowed_recipients)

    async def _perform(i: EmailSendInput) -> ActionResult:
        supplied = i.mailbox or default_mailbox
        if supplied is None:
            # C12: name the rule, never a value.
            raise ValueError("no mailbox: none supplied and no single configured default mailbox")
        mailbox = supplied.strip().casefold()
        if mailbox_allowlist and mailbox not in mailbox_allowlist:
            # C12: name the rule, never the value.
            raise ValueError("refusing to send from outside the configured mailbox allowlist")
        recipients = await client.read_draft_recipients(mailbox=mailbox, message_id=i.draft_ref)
        if not recipients:
            raise ValueError("refusing to send a draft with no recipients")
        if not set(recipients) <= recipient_allowlist:
            raise ValueError(
                "refusing to send: a draft recipient is outside the configured send allowlist"
            )
        await client.send_draft(mailbox=mailbox, message_id=i.draft_ref)
        return ActionResult(succeeded=True, detail="sent", source_ref=i.draft_ref)

    return ActionDefinition(
        spec=ToolSpec(
            name=EMAIL_SEND_TOOL,
            description=(
                "Send a previously created email draft by its draft reference. "
                "This REALLY sends the message; it requires per-item human "
                "approval, and only allowlisted recipients are reachable."
            ),
            tier=ApprovalTier.TIER_2,
            input_schema={
                "type": "object",
                "properties": {
                    "mailbox": {
                        "type": "string",
                        "description": (
                            "Mailbox holding the draft. Omit to use the deployment's"
                            " configured mailbox."
                        ),
                    },
                    "draft_ref": {
                        "type": "string",
                        "description": "the draft id returned by draft.email.create",
                    },
                },
                "required": ["draft_ref"],
            },
        ),
        source_system="m365",
        input_model=EmailSendInput,
        perform=_perform,
    )


#: The allowlisted argument shape (merge into ``GovernSettings.guardrails.tools``).
#: ``draft_ref`` is ``text`` rather than ``identifier`` deliberately: Graph message
#: ids are base64-ish and may carry ``=``/``+``, which the identifier grammar
#: refuses. Length-capped; it only ever addresses a message inside the
#: allowlisted mailbox.
EMAIL_SEND_ARG_POLICY = ToolArgPolicy(
    args={
        "request_id": ArgSpec(kind=ToolArgKind.text),
        "mailbox": ArgSpec(kind=ToolArgKind.text),
        "draft_ref": ArgSpec(kind=ToolArgKind.text, required=True, max_length=512),
    }
)
