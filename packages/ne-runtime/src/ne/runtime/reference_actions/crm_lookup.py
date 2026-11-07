# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Reference READ tool: resolve a CRM account record id by name (Tier-0).

The read leg that makes :mod:`ne.runtime.reference_actions.crm_note` self-sufficient.
A governed write needs a target record id; resolving that id by READING the system of
record is what lets every field of the resulting write trace back to a served value
instead of a local lookup table.

``read.external.crm.account`` classifies Tier-0 under the SPEC §4.2 Rego taxonomy
(``read.*`` prefix) — autonomous and logged, no approval. That is correct: it reads
one id and one name, performs no mutation, and returns nothing the caller was not
already entitled to ask the CRM for. The WRITE it enables remains Tier-2.

Like every other module here, the connector seam is a NARROW structural Protocol
(:class:`CrmLookupClient`, matched by
``ne.connect.salesforce.lookup.SalesforceLookupClient``) — this module imports NO
httpx and NO ``ne.connect`` (C3/C5 src boundary).

This is a plain :class:`~ne.runtime.tools.ToolBinding`, NOT an
:class:`~ne.runtime.action_tools.ActionDefinition`: the action machinery exists for
idempotent WRITES (dedup keys, exactly-once claims, ``ActionResult``). A read has no
write to deduplicate, so borrowing that machinery would add a dedup row per lookup and
misreport a re-read as ``deduplicated``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.tools import ToolBinding

__all__ = [
    "CRM_ACCOUNT_LOOKUP_ARG_POLICY",
    "CRM_ACCOUNT_LOOKUP_TOOL",
    "CRM_CONTACT_LOOKUP_ARG_POLICY",
    "CRM_CONTACT_LOOKUP_TOOL",
    "CrmContactLookupClient",
    "CrmLookupClient",
    "crm_account_lookup_binding",
    "crm_contact_lookup_binding",
]

CRM_ACCOUNT_LOOKUP_TOOL = "read.external.crm.account"
CRM_CONTACT_LOOKUP_TOOL = "read.external.crm.contact"


class CrmLookupClient(Protocol):
    """The narrow read seam: resolve an sObject record id by exact name."""

    async def find_id_by_name(self, sobject: str, name: str) -> str | None: ...


def crm_account_lookup_binding(
    client: CrmLookupClient, *, source_system: str = "salesforce"
) -> ToolBinding:
    """Build the governed CRM account-lookup :class:`ToolBinding` over ``client``.

    ``source_system`` names the system the injected client actually reads, for
    the audit detail — ``"salesforce"`` for the live SOQL client, ``"internal"``
    for the synced-mirror client (NE-S5-INTERNAL-CRM): the audit chain must say
    where the record truly came from.
    """

    async def _handler(args: Mapping[str, object]) -> str:
        name = str(args.get("name", "")).strip()
        if not name:
            raise ValueError("name must not be empty")
        record_id = await client.find_id_by_name("Account", name)
        # A miss is reported, never raised: the caller's correct response is to
        # withhold the dependent write, and it needs to be able to say WHY.
        return json.dumps({"name": name, "record_id": record_id, "found": record_id is not None})

    def _extract_audit_detail(args: Mapping[str, object], result_str: str) -> Mapping[str, object]:
        # Identifiers and a bool — never the account name, which is customer data.
        parsed = json.loads(result_str)
        return {
            "action_kind": "read",
            "source_system": source_system,
            "sobject": "Account",
            "found": bool(parsed.get("found")),
            "target": parsed.get("record_id") or "",
        }

    return ToolBinding(
        spec=ToolSpec(
            name=CRM_ACCOUNT_LOOKUP_TOOL,
            description="Resolve a CRM Account record id by its exact name.",
            tier=ApprovalTier.TIER_0,
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        handler=_handler,
        extract_audit_detail=_extract_audit_detail,
    )


#: The allowlisted argument shape (merge into ``GovernSettings.guardrails.tools``).
CRM_ACCOUNT_LOOKUP_ARG_POLICY = ToolArgPolicy(
    args={"name": ArgSpec(kind=ToolArgKind.text, required=True, max_length=255)}
)


class CrmContactLookupClient(Protocol):
    """The narrow contact-read seam: resolve one Contact by exact full name.

    Returns ``None`` on no match; a mapping with ``record_id``, ``email`` and
    ``account_id`` string values on exactly one match. More than one match must
    raise in the client (an ambiguous person is never silently resolved).
    """

    async def find_contact_by_name(self, name: str) -> Mapping[str, str] | None: ...


def crm_contact_lookup_binding(
    client: CrmContactLookupClient, *, source_system: str = "salesforce"
) -> ToolBinding:
    """Build the governed CRM contact-lookup :class:`ToolBinding` over ``client``.

    ``source_system`` follows :func:`crm_account_lookup_binding`'s contract: it
    names the system the injected client actually reads (NE-S5-INTERNAL-CRM).

    NE-CS-LOOKUP (S5 outreach): the READ that turns a customer NAME served by
    governed graph evidence into the two values a governed outreach write needs
    — the contact's recorded email address (the draft recipient comes from the
    system of record, never from the prompt) and the parent Account record id
    (the target of the Tier-1 CRM contact note). ``read.*`` classifies Tier-0
    under the SPEC §4.2 Rego taxonomy: one bounded read of fields the caller
    is entitled to ask the CRM for; the writes it feeds keep their own tiers.
    Like :func:`crm_account_lookup_binding` this is a plain ToolBinding, not an
    ActionDefinition — a read has no write to deduplicate.
    """

    async def _handler(args: Mapping[str, object]) -> str:
        name = str(args.get("name", "")).strip()
        if not name:
            raise ValueError("name must not be empty")
        record = await client.find_contact_by_name(name)
        # A miss is reported, never raised: the caller's correct response is to
        # withhold the dependent draft/write, and it needs to be able to say WHY.
        if record is None:
            return json.dumps({"name": name, "found": False})
        return json.dumps(
            {
                "name": name,
                "found": True,
                "record_id": record["record_id"],
                "email": record["email"],
                "account_id": record["account_id"],
            }
        )

    def _extract_audit_detail(args: Mapping[str, object], result_str: str) -> Mapping[str, object]:
        # Identifiers and a bool — never the contact name or email, which are
        # customer data (C12).
        parsed = json.loads(result_str)
        return {
            "action_kind": "read",
            "source_system": source_system,
            "sobject": "Contact",
            "found": bool(parsed.get("found")),
            "target": parsed.get("record_id") or "",
        }

    return ToolBinding(
        spec=ToolSpec(
            name=CRM_CONTACT_LOOKUP_TOOL,
            description=(
                "Resolve a CRM Contact by exact full name: returns the contact "
                "record id, recorded email address, and parent account record id."
            ),
            tier=ApprovalTier.TIER_0,
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        handler=_handler,
        extract_audit_detail=_extract_audit_detail,
    )


#: The allowlisted argument shape (merge into ``GovernSettings.guardrails.tools``).
CRM_CONTACT_LOOKUP_ARG_POLICY = ToolArgPolicy(
    args={"name": ArgSpec(kind=ToolArgKind.text, required=True, max_length=255)}
)
