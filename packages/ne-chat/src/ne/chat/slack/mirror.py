# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SlackApprovalMirror — the Slack-specific VIEW of a pending NE-035 approval.

Mirroring is a VIEW plus a signed action-submission onto the existing durable
gate — it holds NO approval state of its own. The composition root (or a worker)
polls ``DurableApprovalGate.list_pending()`` and calls :meth:`mirror_pending` to
surface an actionable card in Slack; the actual decision is routed by
``ne.chat.approvals.ApprovalActionService`` onto the SAME gate. This class only
renders and posts Block Kit — never a policy decision.

Slack-specific by design (it builds Block Kit and posts via the Slack client), so
it lives under ``ne.chat.slack`` per the C5 "all Slack-specifics under
ne/chat/slack/" invariant; the channel-agnostic crux is
``ne.chat.approvals.ApprovalActionService``.
"""

from __future__ import annotations

from ne.chat.slack.blocks import approval_blocks, approval_outcome_blocks, message_blocks
from ne.chat.slack.client import SlackChatClient
from ne.runtime import PendingApproval

__all__ = ["SlackApprovalMirror"]


class SlackApprovalMirror:
    """Post / replace the Slack Block Kit VIEW of a durable pending approval."""

    def __init__(self, client: SlackChatClient) -> None:
        self._client = client

    async def mirror_pending(self, pending: PendingApproval, *, channel: str) -> str:
        """Post an ACTIONABLE mirror of ``pending`` into ``channel``; return its ts.

        Only ids / tier / action / requesting principal / SLA are rendered — never
        the approval payload values (redaction discipline).
        """
        posted = await self._client.call(
            "chat.postMessage",
            {
                "channel": channel,
                "blocks": approval_blocks(
                    request_id=pending.request_id,
                    action=pending.action,
                    tier=pending.tier,
                    principal_id=pending.principal_id,
                    sla_deadline=pending.sla_deadline,
                    escalated=pending.escalated,
                ),
            },
        )
        return str(posted["ts"])

    async def ephemeral(self, *, channel: str, user: str, text: str) -> None:
        """Send an ephemeral notice visible only to ``user`` (unauthorized-click notices).

        Used when a click did NOT decide the approval (unauthorized / stale): the
        mirrored message keeps its buttons for a legitimate approver, and only the
        clicker sees the notice.
        """
        await self._client.call(
            "chat.postEphemeral",
            {"channel": channel, "user": user, "blocks": message_blocks(text)},
        )

    async def replace_with_outcome(
        self,
        *,
        channel: str,
        ts: str,
        request_id: str,
        status: str,
        approver_id: str | None = None,
        note: str = "",
    ) -> None:
        """Replace a mirrored message with its (button-less) outcome block.

        An actioned or rejected mirror never keeps live buttons, so a decided
        approval can never be re-actioned from a stale Slack message.
        """
        await self._client.call(
            "chat.update",
            {
                "channel": channel,
                "ts": ts,
                "blocks": approval_outcome_blocks(request_id, status, approver_id, note),
            },
        )
