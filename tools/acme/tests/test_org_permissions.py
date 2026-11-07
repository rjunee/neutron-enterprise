# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The org chart + ACL permission structure is internally consistent — this is
the ground truth NE-075's permission-trap eval cases depend on.
"""

from __future__ import annotations

import json
from pathlib import Path

from ne.acme.models import AclEntry, GroundTruth, OrgChart
from ne.schema import OperationalRecordSet

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "acme-v1"

_EXPECTED_DEPARTMENT_SIZES = {
    "Executive": 5,
    "Engineering": 10,
    "Sales": 8,
    "Support": 6,
    "Finance": 5,
    "HR": 3,
    "Legal": 3,
}


def _load_org() -> OrgChart:
    return OrgChart.model_validate(json.loads((FIXTURES / "org.json").read_bytes()))


def _load_acls() -> list[AclEntry]:
    return [
        AclEntry.model_validate(entry) for entry in json.loads((FIXTURES / "acl.json").read_bytes())
    ]


def _load_ground_truth() -> GroundTruth:
    return GroundTruth.model_validate(json.loads((FIXTURES / "ground_truth.json").read_bytes()))


def _load_documents() -> list[str]:
    obj = json.loads((FIXTURES / "records.json").read_bytes())
    rs = OperationalRecordSet.model_validate(obj)
    assert rs.documents is not None
    return [doc.id for doc in rs.documents]


def test_org_chart_counts_and_departments() -> None:
    org = _load_org()
    rs = OperationalRecordSet.model_validate(json.loads((FIXTURES / "records.json").read_bytes()))
    assert rs.people is not None
    people_ids = {p.id for p in rs.people}

    assert len(org.members) == 40
    assert {m.person_id for m in org.members} == people_ids

    sizes: dict[str, int] = {}
    for member in org.members:
        sizes[member.department] = sizes.get(member.department, 0) + 1
    assert sizes == _EXPECTED_DEPARTMENT_SIZES


def test_manager_chains_terminate_at_ceo() -> None:
    org = _load_org()
    members = {m.person_id: m for m in org.members}
    ceo_ids = [m.person_id for m in org.members if m.manager_id is None]
    assert ceo_ids == ["per-0000"]

    for start in members:
        visited: set[str] = set()
        pid = start
        while True:
            manager_id = members[pid].manager_id
            if manager_id is None:
                break
            assert pid not in visited, f"manager cycle detected starting at {start}"
            visited.add(pid)
            pid = manager_id
        assert pid == "per-0000"


def test_acl_principals_exist() -> None:
    org = _load_org()
    acls = _load_acls()
    doc_ids = set(_load_documents())
    person_ids = {m.person_id for m in org.members}
    group_names = {g.name for g in org.groups}
    group_members = {g.name: set(g.member_ids) for g in org.groups}

    for group in org.groups:
        for member_id in group.member_ids:
            assert member_id in person_ids

    for entry in acls:
        assert entry.doc_id in doc_ids
        for person_id in entry.allowed_people:
            assert person_id in person_ids
        for group_name in entry.allowed_groups:
            assert group_name in group_names
            assert group_members[group_name] <= person_ids


def test_every_doc_has_exactly_one_acl() -> None:
    acls = _load_acls()
    doc_ids = _load_documents()
    acl_doc_ids = [entry.doc_id for entry in acls]
    assert len(acl_doc_ids) == len(doc_ids)
    assert set(acl_doc_ids) == set(doc_ids)
    assert len(acl_doc_ids) == len(set(acl_doc_ids))


def test_ground_truth_readers_match_acl_expansion() -> None:
    org = _load_org()
    acls = {entry.doc_id: entry for entry in _load_acls()}
    ground_truth = _load_ground_truth()
    group_members = {g.name: g.member_ids for g in org.groups}

    for doc_gt in ground_truth.docs:
        acl = acls[doc_gt.doc_id]
        expected: set[str] = set(acl.allowed_people)
        for group_name in acl.allowed_groups:
            expected.update(group_members[group_name])
        assert doc_gt.reader_ids == sorted(expected)


def test_visibility_classes_all_present_and_traps_exist() -> None:
    org = _load_org()
    ground_truth = _load_ground_truth()
    all_person_ids = {m.person_id for m in org.members}

    visibilities = {doc_gt.visibility for doc_gt in ground_truth.docs}
    assert visibilities == {"public", "department", "exec_only", "named"}

    exec_only_count = sum(1 for doc_gt in ground_truth.docs if doc_gt.visibility == "exec_only")
    assert exec_only_count >= 20

    trap_found = False
    for doc_gt in ground_truth.docs:
        excluded = all_person_ids - set(doc_gt.reader_ids)
        if excluded:
            trap_found = True
            break
    assert trap_found, "expected at least one (person, doc) permission-trap pair"
