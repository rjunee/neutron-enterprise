# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GET /v1/me — reflect the middleware-attached principal (spec §1.1).

Returns ``{id, groups, entitlements}``. ``claims`` are NEVER echoed (they may
carry department/roles internals the console does not need). Principal from the
verified bearer only; the sole error is a middleware 401.

``entitlements`` names the coarse surfaces this principal's groups satisfy, using
the SAME fail-closed disjoint check every guarded route runs (empty configured
tuple ⇒ nobody). It is advisory for the UI — see ``wire.PrincipalView`` for why
it exists and why it authorizes nothing.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ne.api.deps import ApiState, api_state, current_principal
from ne.api.wire import PrincipalView
from ne.contracts.types import Principal

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]

#: surface name -> the ``ApiSettings`` attribute holding its configured roles.
#: One entry per coarse surface the console can be refused on.
_SURFACE_ROLES: tuple[tuple[str, str], ...] = (
    ("approvals", "approver_roles"),
    ("steward", "steward_roles"),
    ("ops", "ops_roles"),
)


def entitlements_for(groups: tuple[str, ...] | list[str], settings: object) -> list[str]:
    """The surfaces these groups satisfy, fail-closed and disjoint-checked.

    The check is IDENTICAL to ``authz.require_role``: an empty configured tuple
    admits nobody, and disjoint groups admit nobody. Pure, so the rule is
    testable without a request.
    """
    held = set(groups)
    out: list[str] = []
    for surface, attr in _SURFACE_ROLES:
        roles: tuple[str, ...] = getattr(settings, attr, ()) or ()
        if roles and not held.isdisjoint(roles):
            out.append(surface)
    return out


@router.get("/v1/me")
async def get_me(principal: _Principal, state: _State) -> PrincipalView:
    return PrincipalView(
        id=principal.id,
        groups=list(principal.groups),
        entitlements=entitlements_for(principal.groups, state.settings),
    )
