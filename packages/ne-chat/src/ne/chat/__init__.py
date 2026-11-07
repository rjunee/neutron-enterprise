# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-chat — chat channel adapters implementing the frozen ChatAdapter contract.

Slack is the v1 implementation; Teams implements the SAME
``ne.contracts.runtime.ChatAdapter`` Protocol in v1.1. The channel-AGNOSTIC
services (:class:`ConversationService`, :class:`ChatIdentityLinker`,
:class:`ApprovalActionService`) live at the package root; every Slack-specific
detail lives under :mod:`ne.chat.slack`, so a Teams adapter is an additive
:mod:`ne.chat.teams` package against the same seams.

Security spine (SPEC §2):

* Slack request signatures are verified on EVERY inbound request BEFORE any
  processing (C2/C12, anti-replay);
* a Slack message becomes an agent turn ONLY through the injected, governed
  ``AgentSession`` — there is no ungoverned agent path from Slack (C6);
* a Slack user is fail-closed-mapped to a real ``Principal`` (unmapped ⇒ no
  access);
* approval mirroring is a VIEW plus a signed action-submission onto the REAL
  NE-035 ``DurableApprovalGate`` — never a parallel approval path.
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.chat.approvals import ApprovalActionResult, ApprovalActionService
from ne.chat.conversation import ConversationService
from ne.chat.errors import (
    ApproverNotAuthorizedError,
    ChatAuditError,
    ChatError,
    IngressRejectedError,
    SignatureVerificationError,
    SlackApiError,
    StaleTimestampError,
    UnmappedChatUserError,
)
from ne.chat.identity import ChatIdentityLinker

__all__ = [
    "ApprovalActionResult",
    "ApprovalActionService",
    "ApproverNotAuthorizedError",
    "ChatAuditError",
    "ChatError",
    "ChatIdentityLinker",
    "ConversationService",
    "IngressRejectedError",
    "SignatureVerificationError",
    "SlackApiError",
    "StaleTimestampError",
    "UnmappedChatUserError",
    "__version__",
]
