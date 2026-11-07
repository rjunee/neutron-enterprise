# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The frozen-shape ratchet for the Skill Forge contract surface (ADR-0011)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

import ne.contracts as contracts
from ne.contracts.skills import (
    _HASH_ACL,
    _HASH_EXCLUDED,
    _HASH_ORDERED,
    _HASH_SORTED_SET,
    AuthoringStandard,
    InvocationPage,
    SkillInvocation,
    SkillPack,
    SkillSource,
    SkillSubstrate,
    SnapshotPage,
    WorkflowSnapshot,
    WorkflowStep,
    skill_content_hash,
    skill_routing_hash,
)

_NEW_NAMES = (
    "AuthoringStandard",
    "InvocationPage",
    "SkillInvocation",
    "SkillPack",
    "SkillSource",
    "SkillSubstrate",
    "SnapshotPage",
    "WorkflowSnapshot",
    "WorkflowStep",
    "skill_content_hash",
    "skill_routing_hash",
)


def _pack(**overrides: object) -> SkillPack:
    base: dict[str, object] = {
        "name": "deploy-staging",
        "version": 1,
        "description": "Deploy to staging safely.",
        "triggers": ("deploy to staging", "push staging"),
        "body": "## When to Use\n## Procedure\n## Pitfalls\n## Verification\n",
        "source": SkillSource.FORGED,
    }
    base.update(overrides)
    return SkillPack(**base)  # type: ignore[arg-type]


def _snapshot(**overrides: object) -> WorkflowSnapshot:
    start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    base: dict[str, object] = {
        "snapshot_id": "snap-1",
        "substrate_id": "pydantic-ai",
        "intent": "ship the release",
        "steps": (WorkflowStep(action="write.deploy", outcome="ok"),),
        "succeeded": True,
        "principal_id": "p1",
        "session_id": "s1",
        "started_at": start,
        "finished_at": start + timedelta(minutes=5),
    }
    base.update(overrides)
    return WorkflowSnapshot(**base)  # type: ignore[arg-type]


def _invocation(**overrides: object) -> SkillInvocation:
    base: dict[str, object] = {
        "skill_name": "deploy-staging",
        "version": 1,
        "principal_id": "p1",
        "session_id": "s1",
        "fired_at": datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return SkillInvocation(**base)  # type: ignore[arg-type]


def test_skill_source_has_five_members() -> None:
    assert {s.value for s in SkillSource} == {
        "bundled",
        "engagement",
        "forged",
        "learned",
        "manual",
    }


def test_models_are_frozen() -> None:
    pack = _pack()
    snap = _snapshot()
    inv = _invocation()
    step = WorkflowStep(action="a", outcome="b")
    std = AuthoringStandard()
    with pytest.raises(ValidationError):
        pack.name = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        snap.intent = "x"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        inv.skill_name = "x"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        step.action = "x"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        std.body_max_chars = 1  # type: ignore[misc]


def test_models_reject_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _pack(surprise="x")
    with pytest.raises(ValidationError):
        _snapshot(surprise="x")
    with pytest.raises(ValidationError):
        _invocation(surprise="x")
    with pytest.raises(ValidationError):
        WorkflowStep(action="a", outcome="b", surprise="x")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AuthoringStandard(surprise="x")  # type: ignore[call-arg]


def test_name_slug_rejected() -> None:
    for bad in ("Bad_Name", "x", "-leading", "UPPER", "has space"):
        with pytest.raises(ValidationError):
            _pack(name=bad)


def test_version_below_one_rejected() -> None:
    with pytest.raises(ValidationError):
        _pack(version=0)
    with pytest.raises(ValidationError):
        _invocation(version=0)


def test_description_over_500_rejected() -> None:
    with pytest.raises(ValidationError):
        _pack(description="x" * 501)


def test_empty_triggers_rejected() -> None:
    with pytest.raises(ValidationError):
        _pack(triggers=())


def test_naive_datetime_rejected() -> None:
    naive = datetime(2026, 7, 20, 12, 0)
    with pytest.raises(ValidationError):
        _snapshot(started_at=naive)
    with pytest.raises(ValidationError):
        _invocation(fired_at=naive)


def test_negative_duration_snapshot_rejected() -> None:
    start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    with pytest.raises(ValidationError):
        _snapshot(started_at=start, finished_at=start - timedelta(minutes=1))


def test_set_semantic_fields_normalized() -> None:
    pack = _pack(
        tags=("z", "a", "a"),
        requires_tools=("t2", "t1", "t2"),
        platforms=("b", "a"),
        acl_labels=("dept:z", "dept:a", "dept:a"),
    )
    assert pack.tags == ("a", "z")
    assert pack.requires_tools == ("t1", "t2")
    assert pack.platforms == ("a", "b")
    assert pack.acl_labels == ("dept:a", "dept:z")


def test_content_hash_order_independent_over_set_fields() -> None:
    a = _pack(
        tags=("a", "z"),
        platforms=("a", "b"),
        requires_tools=("t1", "t2"),
        acl_labels=("dept:a", "dept:z"),
    )
    b = _pack(
        tags=("z", "a"),
        platforms=("b", "a"),
        requires_tools=("t2", "t1"),
        acl_labels=("dept:z", "dept:a"),
    )
    assert a.content_hash == b.content_hash
    assert a.routing_hash == b.routing_hash


def test_trigger_order_changes_content_hash() -> None:
    a = _pack(triggers=("one", "two"))
    b = _pack(triggers=("two", "one"))
    assert a.content_hash != b.content_hash
    assert a.routing_hash != b.routing_hash


def test_relabel_changes_content_hash_not_routing_hash() -> None:
    labeled = _pack(acl_labels=("dept:finance",))
    unlabeled = _pack(acl_labels=())
    assert labeled.content_hash != unlabeled.content_hash
    assert labeled.routing_hash == unlabeled.routing_hash


def test_provenance_excluded_from_both_hashes() -> None:
    a = _pack(provenance={"review_id": "r1"})
    b = _pack(provenance={"review_id": "r2"})
    assert a.content_hash == b.content_hash
    assert a.routing_hash == b.routing_hash


def test_hash_field_classification_tripwire() -> None:
    assert set(SkillPack.model_fields) == (
        _HASH_ORDERED | _HASH_SORTED_SET | _HASH_ACL | _HASH_EXCLUDED
    )
    # The four classes are disjoint.
    classes = [_HASH_ORDERED, _HASH_SORTED_SET, _HASH_ACL, _HASH_EXCLUDED]
    for i, first in enumerate(classes):
        for second in classes[i + 1 :]:
            assert first.isdisjoint(second)


def test_workflow_step_has_no_argument_field() -> None:
    assert set(WorkflowStep.model_fields) == {"action", "outcome", "detail_ref"}


def test_skill_pack_is_procedural_only() -> None:
    fields = set(SkillPack.model_fields)
    assert fields == {
        "name",
        "version",
        "description",
        "triggers",
        "body",
        "source",
        "category",
        "tags",
        "requires_tools",
        "platforms",
        "acl_labels",
        "provenance",
    }
    # No executable-artifact field exists (procedural-only, C6).
    for forbidden in ("scripts", "executables", "code", "artifacts", "commands"):
        assert forbidden not in fields


def test_hashes_serialize_in_model_dump() -> None:
    pack = _pack()
    dumped = pack.model_dump()
    assert dumped["content_hash"] == pack.content_hash
    assert dumped["routing_hash"] == pack.routing_hash
    assert '"content_hash"' in pack.model_dump_json()
    assert '"routing_hash"' in pack.model_dump_json()


def test_roundtrip_via_dump_excluding_hashes() -> None:
    pack = _pack(acl_labels=("dept:finance",), tags=("a", "b"))
    payload = pack.model_dump(exclude={"content_hash", "routing_hash"})
    rebuilt = SkillPack.model_validate(payload)
    assert rebuilt == pack
    assert rebuilt.content_hash == pack.content_hash
    assert rebuilt.routing_hash == pack.routing_hash
    # extra='forbid' rejects re-feeding the computed-field keys directly (the
    # actual pydantic 2.13.4 behavior, pinned here — this is WHY the normative
    # reconstruction excludes the hash keys).
    with pytest.raises(ValidationError):
        SkillPack.model_validate(pack.model_dump())


def test_functions_match_computed_fields() -> None:
    pack = _pack(acl_labels=("dept:a",), tags=("z", "a"))
    assert skill_content_hash(pack) == pack.content_hash
    assert skill_routing_hash(pack) == pack.routing_hash


def test_snapshot_and_invocation_pages_shape() -> None:
    snap = _snapshot()
    inv = _invocation()
    spage = SnapshotPage(items=[snap], cursor="mem:1", exhausted=False)
    ipage = InvocationPage(items=[inv], cursor="mem:1", exhausted=True)
    assert spage.items == [snap]
    assert spage.cursor == "mem:1"
    assert spage.exhausted is False
    # Positional unpacking works (NamedTuple).
    items, cursor, exhausted = ipage
    assert items == [inv]
    assert cursor == "mem:1"
    assert exhausted is True


def test_skill_substrate_is_protocol() -> None:
    assert getattr(SkillSubstrate, "_is_protocol", False)


def test_new_names_exported() -> None:
    for name in _NEW_NAMES:
        assert hasattr(contracts, name), f"{name} not importable from ne.contracts"
        assert name in contracts.__all__, f"{name} missing from __all__"
