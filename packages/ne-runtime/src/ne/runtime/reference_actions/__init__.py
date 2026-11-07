# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The reference governed write-back actions (NE-050 / NE-CS-SEND) — the tier ladder.

Each module defines a typed :class:`~ne.runtime.action_tools.ActionInput`, a NARROW
structural client Protocol (so ne-runtime src never imports ``ne.connect``), a
``*_definition(client)`` factory returning an
:class:`~ne.runtime.action_tools.ActionDefinition`, and an exported ``*_ARG_POLICY``
constant for composition roots / tests.

The tier ladder (0 / 1 / 2 / 3 — SPEC §4.2's own rungs, owner decision 2026-08-27):

* :mod:`email_draft` — M365 email DRAFT (never sent by it), Tier-0: autonomous,
  logged, reviewable in the mailbox before anything irreversible happens;
* :mod:`crm_note` — Salesforce Note, internal write, Tier-1: autonomous + digest;
* :mod:`email_send` — REAL M365 send of an existing draft, Tier-2: per-item human
  approval, double-allowlisted (mailbox + actual draft recipients);
* :mod:`ticket_update` — SQL customer-visible ticket update, Tier-3 (named + distinct
  second check).

:mod:`crm_lookup` joins them as the one READ here: a Tier-0 ``ToolBinding`` (not an
``ActionDefinition`` — a read has no write to deduplicate) that resolves the CRM
record id a governed write needs, so the target of a write is READ from the system of
record rather than carried in a lookup table.
"""

from __future__ import annotations

from ne.runtime.reference_actions.crm_lookup import (
    CRM_ACCOUNT_LOOKUP_ARG_POLICY,
    CRM_ACCOUNT_LOOKUP_TOOL,
    CRM_CONTACT_LOOKUP_ARG_POLICY,
    CRM_CONTACT_LOOKUP_TOOL,
    CrmContactLookupClient,
    CrmLookupClient,
    crm_account_lookup_binding,
    crm_contact_lookup_binding,
)
from ne.runtime.reference_actions.crm_note import (
    CRM_NOTE_ARG_POLICY,
    CrmNoteClient,
    CrmNoteInput,
    crm_note_definition,
)
from ne.runtime.reference_actions.email_draft import (
    EMAIL_DRAFT_ARG_POLICY,
    DraftMailClient,
    EmailDraftInput,
    email_draft_definition,
)
from ne.runtime.reference_actions.email_send import (
    EMAIL_SEND_ARG_POLICY,
    EMAIL_SEND_TOOL,
    EmailSendInput,
    SendMailClient,
    email_send_definition,
)
from ne.runtime.reference_actions.ticket_update import (
    TICKET_UPDATE_ARG_POLICY,
    TicketUpdateInput,
    TicketWriteClient,
    ticket_update_definition,
)

__all__ = [
    "CRM_ACCOUNT_LOOKUP_ARG_POLICY",
    "CRM_ACCOUNT_LOOKUP_TOOL",
    "CRM_CONTACT_LOOKUP_ARG_POLICY",
    "CRM_CONTACT_LOOKUP_TOOL",
    "CRM_NOTE_ARG_POLICY",
    "EMAIL_DRAFT_ARG_POLICY",
    "EMAIL_SEND_ARG_POLICY",
    "EMAIL_SEND_TOOL",
    "TICKET_UPDATE_ARG_POLICY",
    "CrmContactLookupClient",
    "CrmLookupClient",
    "CrmNoteClient",
    "CrmNoteInput",
    "DraftMailClient",
    "EmailDraftInput",
    "EmailSendInput",
    "SendMailClient",
    "TicketUpdateInput",
    "TicketWriteClient",
    "crm_account_lookup_binding",
    "crm_contact_lookup_binding",
    "crm_note_definition",
    "email_draft_definition",
    "email_send_definition",
    "ticket_update_definition",
]
