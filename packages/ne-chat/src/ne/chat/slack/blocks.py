# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pure Slack Block Kit builders (no I/O).

Redaction discipline: approval blocks render ONLY ids, tiers, action names, and
statuses — never the approval payload VALUES (tool args, recipients, amounts).
An actioned or rejected mirror message is REPLACED with an outcome block that
carries NO live buttons, so a decided approval can never be re-actioned from a
stale Slack message.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from ne.contracts.types import Citation

__all__ = [
    "APPROVE_ACTION_ID",
    "DENY_ACTION_ID",
    "approval_blocks",
    "approval_outcome_blocks",
    "message_blocks",
    "placeholder_blocks",
]

APPROVE_ACTION_ID = "ne_approve"
DENY_ACTION_ID = "ne_deny"

type Block = dict[str, object]


def _section(text: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def message_blocks(text: str, citations: Sequence[Citation] = ()) -> list[Block]:
    """A section block with the answer text, plus (if any) a citations context block.

    Citations render as ``source_system:source_ref`` — provenance pointers only,
    never the underlying chunk text.
    """
    blocks: list[Block] = [_section(text or " ")]
    if citations:
        refs = "  •  ".join(f"{c.source_system}:{c.source_ref}" for c in citations)
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Sources: {refs}"}]}
        )
    return blocks


def placeholder_blocks() -> list[Block]:
    """The initial streaming placeholder (posted immediately, updated in place)."""
    return [_section("_Thinking…_")]


def approval_blocks(
    *,
    request_id: str,
    action: str,
    tier: int,
    principal_id: str,
    sla_deadline: datetime | None = None,
    escalated: bool = False,
) -> list[Block]:
    """An ACTIONABLE mirrored-approval message: a summary section + approve/deny.

    Only ids / tier / action / requesting-principal / SLA are rendered — never the
    approval payload values. The two buttons carry ``request_id`` as their value so
    the interaction handler can route the decision onto the durable gate.
    """
    badge = f"Tier {tier}" + (" · ESCALATED" if escalated else "")
    lines = [
        f"*Approval required* ({badge})",
        f"*Action:* `{action}`",
        f"*Requested by:* `{principal_id}`",
    ]
    if sla_deadline is not None:
        lines.append(f"*SLA:* {sla_deadline.isoformat()}")
    return [
        _section("\n".join(lines)),
        {
            "type": "actions",
            "block_id": "ne_approval_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": APPROVE_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "value": request_id,
                },
                {
                    "type": "button",
                    "action_id": DENY_ACTION_ID,
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "value": request_id,
                },
            ],
        },
    ]


def approval_outcome_blocks(
    request_id: str,
    status: str,
    approver_id: str | None = None,
    note: str = "",
) -> list[Block]:
    """The REPLACEMENT (button-less) block posted after any action or rejection."""
    lines = [f"*Approval {status}*", f"*Request:* `{request_id}`"]
    if approver_id:
        lines.append(f"*Approver:* `{approver_id}`")
    if note:
        lines.append(note)
    return [_section("\n".join(lines))]
