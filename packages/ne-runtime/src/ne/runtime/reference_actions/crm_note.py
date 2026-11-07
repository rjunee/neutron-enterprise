# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Reference write-back action: add a CRM Note to a Salesforce record (Tier-1).

A ``Note`` attached to a record is an INTERNAL write (``write.internal.crm.note``,
Rego class Tier-1) declared AT its class tier — NE-CS-SEND (owner decision
2026-08-27) dropped the old Tier-2 over-declaration along with the absolute
ActionTool write floor: an internal, not customer-visible write runs autonomously,
audit-logged and surfaced in the daily digest, exactly as SPEC §4.2's ladder says.
The provenance stamp below still lands whenever an approval WAS consumed (a
deployment may escalate via PDP obligations); with no consumed approval the body
is byte-identical to the input. The connector seam is
the narrow :class:`CrmNoteClient` structural Protocol (matched by
``ne.connect.salesforce.write.SalesforceWriteClient``); this module imports NO
httpx / sqlalchemy / ``ne.connect`` — the client is injected, reachable only from
inside the governed handler (C3/C5 src boundary; C7-write no-bypass).

Provenance stamp (NE-CRM-NOTE-PROVENANCE): every governed integration
authenticates to Salesforce as ONE service identity, so the CRM's own
``CreatedBy`` names the integration user on every write and Salesforce alone
cannot say which human authorised it. When the composition root injects the
executor's carriers (``approval_provider=current_tool_approval``,
``principal_provider=current_tool_principal`` — see
``ne.runtime.middleware``), the written ``Note.Body`` therefore carries a
trailing self-describing stamp::

    <body>

    [Governed by Neutron] Approved by: <approver> | Approval ID: <id> | Requested by: <principal>

Stamp semantics, in order of precedence:

* **Truthfulness over completeness.** ``Approved by`` is the GATE-RECORDED named
  approver (who said yes) — never the requesting principal; ``Requested by`` is
  the verified requesting principal. A segment whose value is unavailable is
  OMITTED whole — never a placeholder, never ``None``, never an empty label.
  With no consumed approval at all (an ungoverned/test-double invocation, or a
  definition built without providers) the body is BYTE-IDENTICAL to the input.
* **C12.** The stamp carries ONLY identifiers already stamped on the audit
  chain (approver id, durable approval id, principal id) — no display name,
  email, department, group, or claim material.
* **The Salesforce cap.** ``Note.Body`` caps at 4000 chars. The stamp is a
  SUFFIX and assembly is truncation-aware: when body+stamp would exceed the
  cap, the BODY tail is truncated (marked ``…``) and the stamp lands intact —
  a truncated stamp could misattribute, a truncated body is visibly elided and
  the full text survives in the approval's disclosure payload.

Honest residual: the body text itself is requester/model-authored, so a body
could IMITATE a stamp. The stamp is a convenience projection; the
tamper-evident audit chain (where the approval id resolves) remains the
authority for who approved what.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from pydantic import Field

from ne.contracts.integration import ActionResult
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.action_tools import ActionDefinition, ActionInput

__all__ = [
    "CRM_NOTE_ARG_POLICY",
    "ApprovalProvenance",
    "CrmNoteClient",
    "CrmNoteInput",
    "crm_note_definition",
]

#: Salesforce's ``Note.Body`` character cap (mirrors ``CrmNoteInput.body``'s
#: ``max_length`` — the stamped body must respect the same limit the input does).
_BODY_CAP = 4000
#: Marks a body tail elided to make room for the intact provenance stamp.
_TRUNCATION_MARKER = "…"


class ApprovalProvenance(Protocol):
    """Structural view of a consumed approval — matched by
    ``ne.runtime.middleware.ConsumedApproval``. A Protocol (not an import) so this
    module stays spec-only per the reference-action import guardian
    (``test_action_write_guardian.py``): a reference action never imports the
    executor machinery, the composition root injects a provider over it."""

    @property
    def approver(self) -> str: ...

    @property
    def approval_id(self) -> str | None: ...


#: Injected read seams for the executor's ambient carriers (middleware plan §2.9:
#: carriers are read through injected providers, never imported by handlers).
ApprovalProvider = Callable[[], ApprovalProvenance | None]
PrincipalProvider = Callable[[], Principal | None]


class CrmNoteInput(ActionInput):
    """Typed input for the CRM-note write (frozen, reject-by-default)."""

    #: A Salesforce record id (15- or 18-char case-safe alphanumeric).
    record_id: str = Field(pattern=r"^[a-zA-Z0-9]{15,18}$")
    #: Note title — single line (no CR/LF), Salesforce caps Title at 80 chars.
    title: str = Field(min_length=1, max_length=80, pattern=r"^[^\r\n]+$")
    #: Note body — Salesforce caps Note.Body at 4000 chars.
    body: str = Field(min_length=1, max_length=4000)


class CrmNoteClient(Protocol):
    """The narrow write seam: create an sObject record, returning its new id."""

    async def create_record(self, sobject: str, fields: Mapping[str, str]) -> str: ...


def _provenance_stamp(approval: ApprovalProvenance | None, requester_id: str | None) -> str | None:
    """The stamp for this write, or ``None`` when there is nothing to stamp.

    No approval ⇒ no stamp AT ALL — a ``Requested by``-only stamp is forbidden
    because a reader skimming attribution lines could take the only named human
    for the authoriser, which the requester, by Tier-2 design, is not.
    """
    if approval is None:
        return None
    segments = [f"Approved by: {approval.approver}"]
    if approval.approval_id is not None:
        segments.append(f"Approval ID: {approval.approval_id}")
    if requester_id:
        segments.append(f"Requested by: {requester_id}")
    return "\n\n[Governed by Neutron] " + " | ".join(segments)


def _stamped_body(body: str, stamp: str | None) -> str:
    """``body`` with ``stamp`` suffixed, truncation-aware within ``_BODY_CAP``.

    The stamp always lands INTACT; when it does not fit, the BODY tail is
    replaced by ``_TRUNCATION_MARKER`` so the elision is visible. With no stamp
    the body is returned byte-identical.
    """
    if stamp is None:
        return body
    if len(body) + len(stamp) <= _BODY_CAP:
        return body + stamp
    budget = _BODY_CAP - len(stamp) - len(_TRUNCATION_MARKER)
    if budget < 1:
        # Pathological identifier lengths only; refuse rather than write a
        # stampless or stamp-truncated note (C12-safe: no values named).
        raise ValueError(
            "the provenance stamp leaves no room for any note body; "
            "refusing to write an unattributable or mis-attributable note"
        )
    return body[:budget] + _TRUNCATION_MARKER + stamp


def crm_note_definition(
    client: CrmNoteClient,
    *,
    approval_provider: ApprovalProvider | None = None,
    principal_provider: PrincipalProvider | None = None,
    source_system: str = "salesforce",
) -> ActionDefinition:
    """Build the governed CRM-note :class:`ActionDefinition` over ``client``.

    Composition roots SHOULD inject ``approval_provider=current_tool_approval``
    and ``principal_provider=current_tool_principal`` (the executor's verified
    ambient carriers) so the written note names who approved it and under which
    approval — see the module docstring. Without providers (the default, and
    every pre-existing caller) the write is byte-identical to the raw input.
    """

    async def _perform(i: CrmNoteInput) -> ActionResult:
        approval = approval_provider() if approval_provider is not None else None
        requester = principal_provider() if principal_provider is not None else None
        body = _stamped_body(
            i.body,
            _provenance_stamp(approval, requester.id if requester is not None else None),
        )
        note_id = await client.create_record(
            "Note", {"ParentId": i.record_id, "Title": i.title, "Body": body}
        )
        return ActionResult(succeeded=True, detail="created", source_ref=note_id)

    return ActionDefinition(
        spec=ToolSpec(
            name="write.internal.crm.note",
            description=(
                "Attach a Note to a Salesforce record. An internal write: runs "
                "autonomously (tier 1), audit-logged and reported in the daily digest."
            ),
            tier=ApprovalTier.TIER_1,
            input_schema={
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
            },
        ),
        source_system=source_system,
        input_model=CrmNoteInput,
        perform=_perform,
    )


#: The allowlisted argument shape (merge into ``GovernSettings.guardrails.tools``).
CRM_NOTE_ARG_POLICY = ToolArgPolicy(
    args={
        "request_id": ArgSpec(kind=ToolArgKind.text),
        "record_id": ArgSpec(kind=ToolArgKind.identifier),
        "title": ArgSpec(kind=ToolArgKind.text, max_length=80),
        "body": ArgSpec(kind=ToolArgKind.text, max_length=4000),
    }
)
