# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Reference write-back action: update a support ticket over SQL (Tier-3).

A ticket status change with a ``public_reply`` is CUSTOMER-VISIBLE / destructive
(``write.external.ticket.update``, Rego class Tier-2) declared at Tier-3 — so it
blocks on a NAMED approver plus a distinct second check via the real
:class:`~ne.runtime.approvals.DurableApprovalGate` (never auto-approve). The connector
seam is the narrow :class:`TicketWriteClient` structural Protocol (matched by
``ne.connect.sql.write.SqlWriteClient``); this module imports NO httpx / sqlalchemy /
``ne.connect`` — the client is injected, reachable only from inside the governed
handler (C7-write no-bypass).

An absolute UPDATE is naturally idempotent at the source (re-applying the same row
update is a no-op change), but the framework's idempotency dedup store still applies
uniformly so a governed retry does not re-enter the source write path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

from pydantic import Field

from ne.contracts.integration import ActionResult
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.action_tools import ActionDefinition, ActionInput

__all__ = [
    "TICKET_UPDATE_ARG_POLICY",
    "TicketUpdateInput",
    "TicketWriteClient",
    "ticket_update_definition",
]


class TicketUpdateInput(ActionInput):
    """Typed input for the customer-visible ticket update (frozen, reject-by-default)."""

    ticket_id: int = Field(ge=1)
    status: Literal["open", "pending", "resolved", "closed"]
    public_reply: str = Field(default="", max_length=2000)


class TicketWriteClient(Protocol):
    """The narrow write seam: update rows by key, returning the affected rowcount."""

    async def update_record(
        self,
        *,
        table: str,
        key_column: str,
        key_value: int | str,
        fields: Mapping[str, str],
    ) -> int: ...


def ticket_update_definition(client: TicketWriteClient) -> ActionDefinition:
    """Build the governed ticket-update :class:`ActionDefinition` over ``client``."""

    async def _perform(i: TicketUpdateInput) -> ActionResult:
        rows = await client.update_record(
            table="tickets",
            key_column="id",
            key_value=i.ticket_id,
            fields={"status": i.status, "public_reply": i.public_reply},
        )
        if rows == 0:
            return ActionResult(succeeded=False, detail="no matching ticket")
        return ActionResult(succeeded=True, detail="updated", source_ref=f"tickets/{i.ticket_id}")

    return ActionDefinition(
        spec=ToolSpec(
            name="write.external.ticket.update",
            description="Update a customer-visible support ticket.",
            tier=ApprovalTier.TIER_3,
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer"},
                    "status": {"type": "string"},
                    "public_reply": {"type": "string"},
                },
            },
        ),
        source_system="sql",
        input_model=TicketUpdateInput,
        perform=_perform,
    )


#: The allowlisted argument shape (merge into ``GovernSettings.guardrails.tools``).
TICKET_UPDATE_ARG_POLICY = ToolArgPolicy(
    args={
        "request_id": ArgSpec(kind=ToolArgKind.text),
        "ticket_id": ArgSpec(kind=ToolArgKind.number),
        "status": ArgSpec(kind=ToolArgKind.text),
        "public_reply": ArgSpec(kind=ToolArgKind.text, max_length=2000),
    }
)
