# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Surface-origin audit + fail-closed role pre-checks (spec §4-5 / §4-8).

``ne-api`` layers its OWN surface-origin audit records on top of the governed
libraries' events — exactly as ``ne-chat`` layers ``chat.approval.submitted`` on
top of the gate's events (ne-chat/approvals.py). These records are C12-REDACTED:
their ``detail`` carries ids / decisions / reasons / error CLASSES only — never
prompts, payloads, chunk text, or tokens.

:func:`require_role` is the ne-chat/approvals.py:112-119 pattern VERBATIM: an
EMPTY configured role tuple ⇒ 403 for EVERYONE (fail closed), a principal whose
groups are disjoint from the roles ⇒ 403, and the gate/store is NEVER reached on
failure. Every unauthorized attempt is AUDITED (the authenticated-but-unauthorized
asymmetry).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from fastapi import HTTPException

from ne.contracts.governance import AuditSink
from ne.contracts.types import Principal
from ne.events import new_audit_event
from ne.schema import AuditOutcome


async def emit_surface_audit(
    sink: AuditSink,
    *,
    action: str,
    resource: str,
    outcome: AuditOutcome,
    actor: str,
    detail: Mapping[str, str],
    surface: str = "console",
) -> None:
    """Append a redacted ne-api surface-origin audit record (fail closed).

    NO try/except: a sink append failure propagates → 500, exactly the gate /
    ne-chat discipline (an unrecorded governed action is withheld, not silently
    served). ``detail`` MUST carry ids/decisions/error-classes only (C12).

    ``surface`` names the origin surface stamped into ``provenance`` (default
    ``"console"`` preserves every existing event byte-for-byte; the Flowise add-on
    (NE-088) passes ``surface="flowise"`` to distinguish flow-initiated records).
    """
    event = new_audit_event(
        action=action,
        resource=resource,
        outcome=outcome,
        actor=actor,
        detail=json.dumps(dict(detail)),
        provenance={
            "source": "ne-api",
            "component": resource.split(":")[1],
            "surface": surface,
        },
    )
    await sink.append(event)


async def require_role(
    principal: Principal,
    roles: tuple[str, ...],
    *,
    sink: AuditSink,
    audit_action: str,
    resource: str,
    http_detail: str,
    request_id: str | None = None,
) -> None:
    """Fail-closed role pre-check (spec §4-5). 403 + audit when unauthorized.

    Empty ``roles`` ⇒ 403 for EVERYONE; disjoint groups ⇒ 403. The guarded
    gate/store is NEVER called when this raises.
    """
    if not roles or set(principal.groups).isdisjoint(roles):
        detail: dict[str, str] = {"reason": "not-authorized"}
        if request_id is not None:
            detail["request_id"] = request_id
        await emit_surface_audit(
            sink,
            action=audit_action,
            resource=resource,
            outcome=AuditOutcome.denied,
            actor=principal.id,
            detail=detail,
        )
        raise HTTPException(status_code=403, detail=http_detail)
