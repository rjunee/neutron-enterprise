# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The Acme Corp 40-person org chart — the permission structure's backbone.

Fixed department sizes and manager chains (see module-level ``_DEPARTMENTS``);
names/emails/phones are deterministically drawn from :mod:`ne.acme.pools` via
:class:`~ne.acme.rand.DetRandom`. Every person also gets an :class:`OrgMember`
sidecar record (department/role/manager/grade) and is placed into the fixed
groups (``all-employees``, ``executive``, ``dept-<slug>``) that ACLs reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from ne.acme.models import Group, OrgChart, OrgMember
from ne.acme.pools import ACME_DOMAIN, BASE_TIME, FAMILY_NAMES, GIVEN_NAMES
from ne.acme.rand import DetRandom
from ne.schema import Person

ACME_ACCOUNT_ID = "acct-0000"

Grade = Literal["exec", "manager", "ic"]


@dataclass(frozen=True)
class _Role:
    """One person-slot in the org chart build order."""

    department: str
    role: str
    grade: Grade


# Fixed department name order (never derived from set iteration, to keep
# every deterministic-ordering guarantee explicit rather than incidental).
_DEPARTMENT_ORDER: tuple[str, ...] = (
    "Executive",
    "Engineering",
    "Sales",
    "Support",
    "Finance",
    "HR",
    "Legal",
)


def dept_slug(name: str) -> str:
    """The ``dept-<slug>`` group-name slug for a department display name."""
    return name.lower().replace(" ", "-")


# Fixed build order: Executive (5) -> Engineering (10) -> Sales (8) -> Support (6)
# -> Finance (5) -> HR (3) -> Legal (3) = 40. Order is part of the determinism
# contract (person ids are assigned in this exact sequence) — never reorder.
def _build_roles() -> list[_Role]:
    roles: list[_Role] = []
    # Executive: CEO + 4 direct C-suite/VP reports, all grade "exec".
    roles.append(_Role("Executive", "Chief Executive Officer", "exec"))
    roles.append(_Role("Executive", "Chief Financial Officer", "exec"))
    roles.append(_Role("Executive", "Chief Technology Officer", "exec"))
    roles.append(_Role("Executive", "VP Sales", "exec"))
    roles.append(_Role("Executive", "VP Support", "exec"))
    # Engineering: 1 manager (reports to CTO) + 9 ICs.
    roles.append(_Role("Engineering", "Engineering Manager", "manager"))
    roles.extend(_Role("Engineering", "Software Engineer", "ic") for _ in range(9))
    # Sales: 1 manager (reports to VP Sales) + 7 ICs.
    roles.append(_Role("Sales", "Sales Manager", "manager"))
    roles.extend(_Role("Sales", "Sales Representative", "ic") for _ in range(7))
    # Support: 1 manager (reports to VP Support) + 5 ICs.
    roles.append(_Role("Support", "Support Manager", "manager"))
    roles.extend(_Role("Support", "Support Engineer", "ic") for _ in range(5))
    # Finance: 1 manager (reports to CFO) + 4 ICs.
    roles.append(_Role("Finance", "Finance Manager", "manager"))
    roles.extend(_Role("Finance", "Financial Analyst", "ic") for _ in range(4))
    # HR: 1 manager (reports to CFO) + 2 ICs.
    roles.append(_Role("HR", "HR Manager", "manager"))
    roles.extend(_Role("HR", "HR Generalist", "ic") for _ in range(2))
    # Legal: 1 manager (reports to CEO) + 2 ICs.
    roles.append(_Role("Legal", "Legal Manager", "manager"))
    roles.extend(_Role("Legal", "Legal Counsel", "ic") for _ in range(2))
    return roles


def build_org(rng: DetRandom) -> tuple[list[Person], OrgChart]:
    """Build the fixed 40-person org chart deterministically from ``rng``."""
    roles = _build_roles()
    assert len(roles) == 40

    people: list[Person] = []
    members: list[OrgMember] = []
    used_emails: dict[str, int] = {}

    ceo_id = "per-0000"
    cfo_id = "per-0001"
    cto_id = "per-0002"
    vp_sales_id = "per-0003"
    vp_support_id = "per-0004"

    # Department -> manager person id, filled in as each department's manager
    # (or, for Executive, the CEO/execs themselves) is created.
    dept_manager_id: dict[str, str] = {}
    dept_report_manager: dict[str, str] = {
        "Engineering": cto_id,
        "Sales": vp_sales_id,
        "Support": vp_support_id,
        "Finance": cfo_id,
        "HR": cfo_id,
        "Legal": ceo_id,
    }

    for i, role in enumerate(roles):
        person_id = f"per-{i:04d}"
        given = rng.choice(GIVEN_NAMES)
        family = rng.choice(FAMILY_NAMES)
        base = f"{given.lower()}.{family.lower()}"
        n = used_emails.get(base, 0)
        used_emails[base] = n + 1
        email = f"{base}@{ACME_DOMAIN}" if n == 0 else f"{base}-{n + 1}@{ACME_DOMAIN}"

        if role.department == "Executive":
            manager_id = None if person_id == ceo_id else ceo_id
        elif role.grade == "manager":
            manager_id = dept_report_manager[role.department]
            dept_manager_id[role.department] = person_id
        else:
            manager_id = dept_manager_id[role.department]

        created_days = rng.rand_int(30, 900)
        updated_days = rng.rand_int(0, created_days)
        created_at = BASE_TIME - timedelta(days=created_days)
        updated_at = BASE_TIME - timedelta(days=updated_days)

        people.append(
            Person(
                id=person_id,
                given_name=given,
                family_name=family,
                email=email,
                title=role.role,
                phone=f"+1-555-01{rng.rand_int(0, 99):02d}",
                works_at=ACME_ACCOUNT_ID,
                is_active=True,
                source_system="acme-hris",
                source_record_id=f"HR-{i:04d}",
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        members.append(
            OrgMember(
                person_id=person_id,
                department=role.department,
                role=role.role,
                manager_id=manager_id,
                grade=role.grade,
            )
        )

    all_ids = [m.person_id for m in members]
    executive_ids = [m.person_id for m in members if m.department == "Executive"]

    dept_names = _DEPARTMENT_ORDER
    dept_groups = [
        Group(
            name=f"dept-{dept_slug(dept)}",
            member_ids=[m.person_id for m in members if m.department == dept],
        )
        for dept in dept_names
    ]

    groups = [
        Group(name="all-employees", member_ids=all_ids),
        Group(name="executive", member_ids=executive_ids),
        *dept_groups,
    ]

    return people, OrgChart(members=members, groups=groups)
