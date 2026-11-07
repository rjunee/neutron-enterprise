# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Steward-gated skill library list (NE-SKILL-1a, D-5): GET /v1/steward/skills.

Reuses the steward router's fail-closed ``_require_steward`` role pre-check (403 +
audited) so a non-steward is refused-and-audited BEFORE the 503-when-unconfigured
check runs — 403 wins over 503 (no store-existence oracle). Read-only: the folded
``library_rows`` view (name/source/status/version/labels/fired-count/tampered),
INCLUDING quarantined + pending-forge names, over one ``anyio.to_thread`` hop.
"""

from __future__ import annotations

import functools
from typing import Annotated

import anyio.to_thread
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException

from ne.api.deps import ApiState, api_state, current_principal
from ne.api.routers.steward import _require_steward
from ne.contracts.types import Principal
from ne.skills.registry import LibraryRow, SqlAlchemySkillRegistry

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]


def _read_rows(engine: sa.Engine) -> tuple[LibraryRow, ...]:
    return SqlAlchemySkillRegistry(engine).library_rows()


def _row_view(row: LibraryRow) -> dict[str, object]:
    return {
        "name": row.name,
        "source": row.source,
        "status": row.status,
        "current_version": row.current_version,
        "description": row.description,
        "acl_labels": list(row.acl_labels),
        "content_hash": row.content_hash,
        "fired_count": row.fired_count,
        "last_fired_at": row.last_fired_at.isoformat() if row.last_fired_at else None,
        "tampered": row.tampered,
    }


@router.get("/v1/steward/skills")
async def steward_skills(principal: _Principal, state: _State) -> dict[str, object]:
    # Role check FIRST (403 audited); a non-steward never learns whether skills are
    # configured (403 > 503).
    await _require_steward(principal, state, None)
    if state.skills_engine is None:
        raise HTTPException(status_code=503, detail="skills registry not configured")
    rows = await anyio.to_thread.run_sync(functools.partial(_read_rows, state.skills_engine))
    return {"skills": [_row_view(r) for r in rows]}
