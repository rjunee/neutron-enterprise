# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Owner-staged principal mapping for three distinct Meridian personas."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ne.meridian.models import PERSONA_IDS, PersonaId
from ne.meridian_seed.errors import SeedPreflightError

GROUP_BY_PERSONA: dict[PersonaId, str] = {
    "ops": "meridian-ops",
    "approver": "meridian-approver",
    "frontline": "meridian-frontline",
}
GROUPS: tuple[str, ...] = tuple(GROUP_BY_PERSONA.values())


def default_principals_path() -> Path:
    return Path.home() / "ne-owner-secrets" / "meridian-seed" / "principals.json"


class M365Principals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    users: dict[str, str] = Field(default_factory=dict)
    groups: dict[str, str] = Field(default_factory=dict)


class PrincipalsMap(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    m365: M365Principals = Field(default_factory=M365Principals)

    @classmethod
    def load(cls, path: Path) -> PrincipalsMap:
        if not path.is_file():
            raise SeedPreflightError(
                f"principals file not found at {path}; create it outside the repository per README"
            )
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def m365_group_id(self, persona: PersonaId) -> str:
        group = GROUP_BY_PERSONA[persona]
        try:
            return self.m365.groups[group]
        except KeyError:
            raise SeedPreflightError(
                f"M365 principals is missing group {group!r}; all three persona groups are required"
            ) from None

    def m365_user_id(self, persona: PersonaId) -> str:
        try:
            return self.m365.users[persona]
        except KeyError:
            raise SeedPreflightError(
                f"M365 principals is missing user for persona {persona!r}"
            ) from None

    def require_m365(self) -> None:
        for persona in PERSONA_IDS:
            self.m365_group_id(persona)
            self.m365_user_id(persona)
