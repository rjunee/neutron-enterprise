# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ONE server-side ``SignalProvenance`` constructor for ne-api (spec §7-R4).

Every flywheel-intake surface (steward reviews AND the NE-137 chat thumbs /
wrong-source signals) builds provenance HERE — grep-provably the single
``SignalProvenance`` construction in ne-api src (guarded by
``test_no_bypass.test_steward_source_scan_guardian``). Identity is NEVER read from a
request body / query / header: the ``principal`` is the verified bearer and
``surface`` is hard-coded to :attr:`SignalSurface.CONSOLE`.

The ``session_id`` is the caller-supplied chat ``session_id`` when the surface has one
(the NE-137 signal routes, whose session is verified via the chat R3 lookup BEFORE
this is called), else the verified token's ``sid`` claim, else a fresh ``uuid4`` — a
server correlation id, NOT client-trusted identity.
"""

from __future__ import annotations

import uuid

from ne.contracts.types import Principal
from ne.flywheel.signals import SignalProvenance, SignalSurface


def console_provenance(
    principal: Principal,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    message_id: str | None = None,
) -> SignalProvenance:
    """Build the server-side-ONLY console signal provenance (spec §7-R4).

    ``session_id`` None ⇒ the verified token's ``sid`` claim when present, else a
    fresh ``uuid4`` (the steward precedent, moved here verbatim). When the caller
    passes a ``session_id`` (a chat session already verified via the R3 lookup) it
    is used directly. ``surface`` is ALWAYS :attr:`SignalSurface.CONSOLE`.
    """
    if session_id is None:
        sid = principal.claims.get("sid")
        session_id = sid if isinstance(sid, str) and sid.strip() else str(uuid.uuid4())
    return SignalProvenance(
        principal=principal,
        surface=SignalSurface.CONSOLE,
        session_id=session_id,
        turn_id=turn_id,
        message_id=message_id,
    )
