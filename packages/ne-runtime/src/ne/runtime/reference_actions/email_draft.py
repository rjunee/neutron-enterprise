# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Reference write-back action: create an M365 email DRAFT (Tier-2).

Creating a message via Graph ``POST /users/{mailbox}/messages`` produces a DRAFT
(``isDraft`` true) — it is NEVER sent by this action. NE-CS-SEND (owner decision
2026-08-27): the draft is Tier-0, exactly as its ``draft.*`` name classifies in
``tiers.rego`` — autonomous, logged, no human gate — because an unsent draft in an
allowlisted mailbox is reviewable and reversible. The irreversible half, the REAL
send, is the separate ``write.external.email.send`` action (:mod:`email_send`),
whose ``write.external.`` class earns Tier-2 per-item human approval and cannot be
declared below it.

``extract_recipients`` surfaces the ``to`` address to the C12 recipient allowlist so it
runs BEFORE any judgment. The connector seam is the narrow :class:`DraftMailClient`
structural Protocol (matched by ``ne.connect.m365.write.GraphWriteClient``); this module
imports NO httpx / sqlalchemy / ``ne.connect``.

v1 cut: a single ``to`` recipient (multiple recipients is a documented follow-up — it
keeps the guardrail recipient ArgSpec exact).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import Field

from ne.contracts.integration import ActionResult
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.action_tools import ActionDefinition, ActionInput

__all__ = [
    "EMAIL_DRAFT_ARG_POLICY",
    "DraftMailClient",
    "EmailDraftInput",
    "email_draft_definition",
]

#: A mailbox is either a UPN-style email OR a 36-char Entra object GUID.
_MAILBOX_PATTERN = (
    r"^(?:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+"
    r"|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$"
)


class EmailDraftInput(ActionInput):
    """Typed input for the email-draft write (frozen, reject-by-default)."""

    #: The owning mailbox — a UPN email or an Entra object GUID. Optional:
    #: omitted, the deployment's sole configured allowlisted mailbox is used
    #: (NE-162 keeps mailbox addresses out of frontier-visible text, so a model
    #: cannot reliably quote one back; the mailbox is deployment configuration,
    #: not a model decision).
    mailbox: str | None = Field(default=None, pattern=_MAILBOX_PATTERN)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)
    #: A single recipient email (multiple recipients is a documented v1 cut).
    to: str = Field(pattern=r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$")


class DraftMailClient(Protocol):
    """The narrow write seam: create a draft message, returning its Graph id."""

    async def create_draft(self, *, mailbox: str, subject: str, body: str, to: str) -> str: ...


def email_draft_definition(
    client: DraftMailClient, *, default_mailbox: str | None = None
) -> ActionDefinition:
    """Build the governed email-draft :class:`ActionDefinition` over ``client``.

    ``default_mailbox`` is the deployment's configured mailbox used when a call
    omits ``mailbox`` (the composition root passes the SOLE allowlisted entry,
    or ``None`` when zero or several are configured). With no default and no
    explicit mailbox the call fails closed — a mailbox is never guessed.
    """

    async def _perform(i: EmailDraftInput) -> ActionResult:
        mailbox = i.mailbox or default_mailbox
        if mailbox is None:
            # C12: name the rule, never a value.
            raise ValueError("no mailbox: none supplied and no single configured default mailbox")
        message_id = await client.create_draft(
            mailbox=mailbox, subject=i.subject, body=i.body, to=i.to
        )
        return ActionResult(succeeded=True, detail="draft created", source_ref=message_id)

    def _extract_recipients(args: Mapping[str, object]) -> tuple[str, ...]:
        to = args.get("to")
        return (str(to),) if to else ()

    return ActionDefinition(
        spec=ToolSpec(
            name="draft.email.create",
            description=(
                "Create an unsent M365 email draft. Nothing is sent; sending is a "
                "separate approval-gated action."
            ),
            tier=ApprovalTier.TIER_0,
            input_schema={
                "type": "object",
                "properties": {
                    "mailbox": {
                        "type": "string",
                        "description": (
                            "Sending mailbox address; must be the configured allowlisted"
                            " mailbox. Omit to use the deployment's configured mailbox."
                        ),
                    },
                    "subject": {"type": "string", "description": "Draft subject line"},
                    "body": {"type": "string", "description": "Draft body text"},
                    "to": {
                        "type": "string",
                        "description": (
                            "Recipient email address; the draft is created for"
                            " human review, nothing is sent"
                        ),
                    },
                },
            },
        ),
        source_system="m365",
        input_model=EmailDraftInput,
        perform=_perform,
        extract_recipients=_extract_recipients,
    )


#: The allowlisted argument shape (merge into ``GovernSettings.guardrails.tools``).
EMAIL_DRAFT_ARG_POLICY = ToolArgPolicy(
    args={
        "request_id": ArgSpec(kind=ToolArgKind.text),
        "mailbox": ArgSpec(kind=ToolArgKind.text),
        "subject": ArgSpec(kind=ToolArgKind.text, max_length=200),
        "body": ArgSpec(kind=ToolArgKind.text, max_length=10000),
        "to": ArgSpec(kind=ToolArgKind.email),
    }
)
