# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The 4 canonical personas + 4 groups, and the owner-staged principals map.

The seeder maps 4 corpus personas onto 4 REAL tenant principals (+ 4 groups per
tenant), NOT the full 40 people / 9 groups — this is what keeps the seeded slice
small while still exercising every ACL-trap class the READ connectors assert
against (NE-075/NE-077):

* P1 Exec     — corpus ``per-0000`` (group ``executive``)
* P2 Engineer — corpus ``per-0006`` (group ``dept-engineering``)
* P3 Sales    — corpus ``per-0016`` (group ``dept-sales``)
* P4 Legal    — corpus ``per-0038`` (group ``dept-legal``)

Corpus rule (verified against the committed fixture): ``department`` docs ALSO
grant ``executive`` (so P1 sees dept docs); only ``named`` visibility can exclude
execs (the sharpest trap).

The real tenant principal IDs live in an owner-staged ``principals.json`` OUTSIDE
the repo (never committed). :class:`PrincipalsMap` loads/validates it; a missing
M365 group or a missing named-doc user is a HARD ERROR (fail closed — the ACL
trap IS the point), while a missing Slack user is a warn-and-continue.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ne.acme_seed.errors import SeedPreflightError

__all__ = [
    "GROUPS",
    "PERSONAS",
    "PERSONA_GROUP",
    "M365Principals",
    "PrincipalsMap",
    "SlackPrincipals",
    "default_principals_path",
]

#: corpus person-id -> stable persona label.
PERSONAS: dict[str, str] = {
    "per-0000": "P1-exec",
    "per-0006": "P2-engineer",
    "per-0016": "P3-sales",
    "per-0038": "P4-legal",
}

#: The 4 canonical groups seeded per tenant (subset of the corpus's 9 groups).
GROUPS: tuple[str, ...] = ("executive", "dept-engineering", "dept-sales", "dept-legal")

#: persona person-id -> its home group (the group the persona anchors).
PERSONA_GROUP: dict[str, str] = {
    "per-0000": "executive",
    "per-0006": "dept-engineering",
    "per-0016": "dept-sales",
    "per-0038": "dept-legal",
}


def default_principals_path() -> Path:
    """The owner-staged principals map (OUTSIDE the repo — never committed)."""
    return Path.home() / "ne-owner-secrets" / "acme-seed" / "principals.json"


class M365Principals(BaseModel):
    """Entra user + group object-ids (or UPNs) for the 4 personas / 4 groups."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: persona person-id -> Entra user object-id or UPN.
    users: dict[str, str] = {}
    #: group name -> Entra group object-id.
    groups: dict[str, str] = {}


class SlackPrincipals(BaseModel):
    """Slack user-ids for the personas (channel membership is by user-id)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: persona person-id -> Slack user-id (e.g. ``U0123``).
    users: dict[str, str] = {}


class PrincipalsMap(BaseModel):
    """The owner-staged principal mapping for all tenants.

    Salesforce needs no principals here — the two permission sets ARE the
    principals (object-level trap; see ``salesforce.py``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    m365: M365Principals = M365Principals()
    slack: SlackPrincipals = SlackPrincipals()

    @classmethod
    def load(cls, path: Path) -> PrincipalsMap:
        """Load + validate the principals JSON; a bad shape raises loudly."""
        if not path.is_file():
            raise SeedPreflightError(
                f"principals file not found at {path} — create it per the README "
                "M365/Slack owner add-lists (never commit it)"
            )
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    # -- M365 accessors: fail CLOSED on a missing group / named user ----------
    def m365_group_id(self, group: str) -> str:
        """Entra group object-id for ``group``; missing -> HARD ERROR (fail closed)."""
        try:
            return self.m365.groups[group]
        except KeyError:
            raise SeedPreflightError(
                f"M365 principals is missing group {group!r} — the ACL trap requires "
                f"all of {list(GROUPS)}; create the 4 groups and fill principals.json"
            ) from None

    def m365_user_id(self, persona: str) -> str:
        """Entra user id/UPN for ``persona``; missing -> HARD ERROR (fail closed)."""
        try:
            return self.m365.users[persona]
        except KeyError:
            raise SeedPreflightError(
                f"M365 principals is missing user for persona {persona!r} "
                f"({PERSONAS.get(persona, '?')}) — a named-doc grant cannot be made; "
                "create the user and fill principals.json"
            ) from None

    def require_m365(self) -> None:
        """Assert every M365 group + persona user is present BEFORE any write."""
        for group in GROUPS:
            self.m365_group_id(group)
        for persona in PERSONAS:
            self.m365_user_id(persona)

    def slack_user_id(self, persona: str) -> str | None:
        """Slack user-id for ``persona`` or ``None`` (warn-and-continue upstream)."""
        return self.slack.users.get(persona)
