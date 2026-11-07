# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The heart of NE-075: the Acme permission-trap suite, driven through the REAL
production ACL logic (``ne.brain.trimming.acl_match_labels`` + ``PrincipalAclFilter``).

The ACL match rule is NEVER re-implemented in eval code (SPEC §11 derive-not-
transliterate). Instead a fixture-built :class:`GroupExpander` (satisfying the
production structural Protocol verbatim) feeds the production
:func:`acl_match_labels`, and the trap matrix is read entirely from the golden
set's tags — so the golden set, not a second copy here, is the single source of
the matrix. A 2000-cell cross-check ties the production match rule to the
fixture's INDEPENDENTLY-authored ``reader_ids`` ground truth.

Importing ``ne.brain.trimming`` from an ne-eval test is allowed and precedented:
``tests/test_retrieval_baseline_public.py`` imports ne-brain heavily and is green
in CI (ne-brain is ne-eval's declared dev/test dependency; import-linter contracts
lint src packages, not tests). No network, no Docker — plain ``uv run pytest``.
"""

from __future__ import annotations

import json
from pathlib import Path

from ne.brain.trimming import GroupExpander, PrincipalAclFilter, acl_match_labels
from ne.contracts.types import Principal
from ne.eval.golden import filter_by_tag, load_golden_set

_ACME = Path(__file__).resolve().parent.parent / "goldensets" / "acme"
#: The permission-trap set — the single source of the trap matrix (its tags). Kept
#: separate from the retrieval set so denied docs are never scored by recall_at_k.
TRAPS_PATH = _ACME / "acme_permission_traps.jsonl"

_FIXTURES = Path(__file__).resolve().parents[3] / "tools" / "acme" / "fixtures" / "acme-v1"

_CANONICAL_PERSONAS = ("per-0000", "per-0006", "per-0016", "per-0038")


def _load(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


_ACL_BY_DOC: dict[str, dict[str, object]] = {
    entry["doc_id"]: entry
    for entry in _load("acl.json")  # type: ignore[union-attr]
}
_GT_BY_DOC: dict[str, dict[str, object]] = {
    doc["doc_id"]: doc
    for doc in _load("ground_truth.json")["docs"]  # type: ignore[call-overload,index]
}
_ORG_GROUPS: list[dict[str, object]] = list(_load("org.json")["groups"])  # type: ignore[call-overload,index]


class FixtureGroupExpander:
    """A :class:`GroupExpander` built from the fixture's org group memberships.

    Structurally satisfies the production Protocol: expands a principal to the set
    of its own id plus every org group it is a member of. This is exactly the
    label algebra the seeded-tenant ne-permissions expander computes at query time
    (NE-042), reproduced here from the committed fixture so the suite is CI-runnable.
    """

    def expand(self, principal: Principal | str) -> frozenset[str]:
        pid = principal if isinstance(principal, str) else principal.id
        groups = {
            str(group["name"])
            for group in _ORG_GROUPS
            if pid in group["member_ids"]  # type: ignore[operator]
        }
        return frozenset({pid} | groups)


_EXPANDER = FixtureGroupExpander()


def _doc_labels(doc_id: str) -> set[str]:
    entry = _ACL_BY_DOC[doc_id]
    return set(entry["allowed_groups"]) | set(entry["allowed_people"])  # type: ignore[arg-type]


def _match(pid: str) -> set[str]:
    return set(acl_match_labels(Principal(id=pid), _EXPANDER))


class _TrapCase:
    __slots__ = ("allowed", "denied", "item_id", "persona", "visible")

    def __init__(self, persona: str, denied: str, visible: str, allowed: str, item_id: str) -> None:
        self.persona = persona
        self.denied = denied
        self.visible = visible
        self.allowed = allowed
        self.item_id = item_id


def _trap_cases() -> list[_TrapCase]:
    """Parse the trap matrix from the golden set's tags (the single source)."""
    traps = filter_by_tag(load_golden_set(TRAPS_PATH), "permission-trap")
    cases: list[_TrapCase] = []
    for item in traps.items:
        fields: dict[str, str] = {}
        for tag in item.tags:
            for key in ("persona", "denied", "visible", "allowed"):
                if tag.startswith(f"{key}:"):
                    fields[key] = tag.split(":", 1)[1]
        cases.append(
            _TrapCase(
                persona=fields["persona"],
                denied=fields["denied"],
                visible=fields["visible"],
                allowed=fields["allowed"],
                item_id=item.id,
            )
        )
    return cases


def test_fixture_expander_satisfies_group_expander_protocol() -> None:
    assert isinstance(FixtureGroupExpander(), GroupExpander)


def test_every_trap_denied_persona_has_no_label_intersection() -> None:
    for case in _trap_cases():
        leak = _match(case.persona) & _doc_labels(case.denied)
        assert not leak, (
            f"LEAK: persona {case.persona} can see denied doc {case.denied} "
            f"({case.item_id}) via labels {sorted(leak)}"
        )


def test_every_trap_visible_control_intersects_for_same_persona() -> None:
    for case in _trap_cases():
        assert _match(case.persona) & _doc_labels(case.visible), (
            f"control doc {case.visible} not visible to {case.persona} ({case.item_id}); "
            "deny must be specific, not blanket"
        )


def test_every_trap_denied_doc_visible_to_its_allowed_persona() -> None:
    for case in _trap_cases():
        assert _match(case.allowed) & _doc_labels(case.denied), (
            f"denied doc {case.denied} is not reachable by its allowed persona "
            f"{case.allowed} ({case.item_id}) — the trap would be vacuous"
        )


def test_principal_acl_filter_parity_with_acl_match_labels() -> None:
    hook = PrincipalAclFilter(_EXPANDER)
    for pid in _CANONICAL_PERSONAS:
        principal = Principal(id=pid)
        mapping = hook(principal)  # no shared_docs -> plain sync mapping
        assert mapping == {"acl": list(acl_match_labels(principal, _EXPANDER))}
        labels = mapping["acl"]
        assert labels, "fail-closed: the hook mapping is never empty"
        assert labels == sorted(labels), "labels must be deterministically sorted"
        assert pid in labels, "acl_match_labels always contains principal.id"


def test_acl_match_labels_agrees_with_ground_truth_reader_ids() -> None:
    # The production match rule independently reproduces the fixture's authored
    # reader_ids ground truth across ALL 500 docs x 4 canonical personas.
    checked = 0
    for doc_id, doc in _GT_BY_DOC.items():
        readers = set(doc["reader_ids"])  # type: ignore[arg-type]
        labels = _doc_labels(doc_id)
        for pid in _CANONICAL_PERSONAS:
            got = bool(_match(pid) & labels)
            assert got == (pid in readers), (
                f"match rule disagrees with reader_ids for {pid} x {doc_id}"
            )
            checked += 1
    assert checked == 2000


def test_permission_trap_pass_rate_is_one_and_hard_gated() -> None:
    cases = _trap_cases()
    assert len(cases) == 12
    passed = sum(
        1
        for case in cases
        if _match(case.persona).isdisjoint(_doc_labels(case.denied))
        and (_match(case.persona) & _doc_labels(case.visible))
        and (_match(case.allowed) & _doc_labels(case.denied))
    )
    rate = passed / len(cases)
    assert rate == 1.0

    from ne.eval.thresholds import MetricStatus, ThresholdSpec, evaluate_thresholds

    raw = json.loads((_ACME / "thresholds.acme.json").read_text(encoding="utf-8"))
    trap_spec = next(
        ThresholdSpec.model_validate(entry)
        for entry in raw
        if entry["metric"] == "permission_trap_pass_rate"
    )
    assert evaluate_thresholds({"permission_trap_pass_rate": rate}, [trap_spec]) == {
        "permission_trap_pass_rate": MetricStatus.PASS
    }
    # One leak fails the suite (hard gate, zero tolerance).
    assert (
        evaluate_thresholds({"permission_trap_pass_rate": 11 / 12}, [trap_spec])[
            "permission_trap_pass_rate"
        ]
        is MetricStatus.FAIL
    )
