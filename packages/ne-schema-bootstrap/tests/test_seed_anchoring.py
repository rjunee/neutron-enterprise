# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for seed-class anchoring (``ne.schema_bootstrap.infer``)."""

from __future__ import annotations

from pathlib import Path

from ne.schema_bootstrap.infer import infer_candidate_model
from ne.schema_bootstrap.profiles import profile_samples


def _class(model, name: str):
    (cls,) = [c for c in model.classes if c.name == name]
    return cls


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(header)] + [",".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n")


def test_people_table_anchors_to_person_with_matching_evidence(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "people.csv",
        ["id", "given_name", "family_name", "email", "title"],
        [
            ["p1", "Ada", "Lovelace", "ada@example.com", "Engineer"],
            ["p2", "Grace", "Hopper", "grace@example.com", "Admiral"],
        ],
    )

    model = infer_candidate_model(profile_samples(tmp_path))

    cls = _class(model, "PeopleRecord")
    assert cls.seed_anchor == "Person"
    # Value assertion: evidence is EXACTLY the matching attribute names,
    # sorted, not merely present.
    assert cls.seed_anchor_evidence == ["email", "family_name", "given_name", "title"]


def test_weak_overlap_does_not_anchor(tmp_path: Path) -> None:
    # "status" alone appears in Account/Ticket/Order, but a single shared
    # attribute never meets anchor_min_matching_attrs(2) for any seed.
    _write_csv(
        tmp_path / "widgets.csv",
        ["id", "status", "foo", "bar"],
        [["w1", "active", "f1", "b1"], ["w2", "inactive", "f2", "b2"]],
    )

    model = infer_candidate_model(profile_samples(tmp_path))

    cls = _class(model, "WidgetsRecord")
    assert cls.seed_anchor is None
    assert cls.seed_anchor_evidence == []


def test_ratio_gate_rejects_two_matches_diluted_by_many_extra_columns(tmp_path: Path) -> None:
    # given_name + family_name match Person (count 2, meets the count floor),
    # but three unrelated extra columns push the overlap ratio to 2/5 = 0.4,
    # below anchor_min_overlap_ratio(0.5).
    _write_csv(
        tmp_path / "widgets.csv",
        ["id", "given_name", "family_name", "extra1", "extra2", "extra3"],
        [["w1", "Ada", "Lovelace", "x1", "y1", "z1"]],
    )

    model = infer_candidate_model(profile_samples(tmp_path))

    cls = _class(model, "WidgetsRecord")
    assert cls.seed_anchor is None


def test_tie_break_resolves_lexicographically_first_seed_deterministically(
    tmp_path: Path,
) -> None:
    # given_name/family_name match Person (count 2); name/domain match
    # Account (count 2) — an engineered exact tie. "Account" < "Person"
    # lexicographically, so Account must win, both times.
    _write_csv(
        tmp_path / "widgets.csv",
        ["id", "given_name", "family_name", "name", "domain"],
        [["w1", "Ada", "Lovelace", "Acme Corp", "acme.com"]],
    )

    model_a = infer_candidate_model(profile_samples(tmp_path))
    model_b = infer_candidate_model(profile_samples(tmp_path))

    cls_a = _class(model_a, "WidgetsRecord")
    cls_b = _class(model_b, "WidgetsRecord")
    assert cls_a.seed_anchor == "Account"
    assert cls_b.seed_anchor == "Account"
