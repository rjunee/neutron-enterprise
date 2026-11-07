# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for requiredness + identifier selection (``ne.schema_bootstrap.infer``)."""

from __future__ import annotations

from pathlib import Path

from ne.schema_bootstrap.infer import infer_candidate_model
from ne.schema_bootstrap.profiles import profile_samples


def _class(model, name: str):
    (cls,) = [c for c in model.classes if c.name == name]
    return cls


def _slot(cls, name: str):
    (slot,) = [s for s in cls.slots if s.name == name]
    return slot


def test_zero_null_column_is_required_and_sample_derived(tmp_path: Path) -> None:
    (tmp_path / "widgets.csv").write_text("id,code\nw1,c1\nw2,c2\nw3,c3\n")
    model = infer_candidate_model(profile_samples(tmp_path))

    slot = _slot(_class(model, "WidgetsRecord"), "code")
    assert slot.required is True
    assert slot.required_is_sample_derived is True


def test_nullable_column_is_not_required(tmp_path: Path) -> None:
    (tmp_path / "widgets.csv").write_text("id,code\nw1,c1\nw2,\nw3,c3\n")
    model = infer_candidate_model(profile_samples(tmp_path))

    slot = _slot(_class(model, "WidgetsRecord"), "code")
    assert slot.required is False
    assert slot.required_is_sample_derived is False


def test_identifier_positive_unique_id_column(tmp_path: Path) -> None:
    (tmp_path / "widgets.csv").write_text("id,name\nw1,alpha\nw2,beta\nw3,gamma\n")
    model = infer_candidate_model(profile_samples(tmp_path))

    slot = _slot(_class(model, "WidgetsRecord"), "id")
    assert slot.identifier is True


def test_identifier_negative_unique_but_non_matching_name(tmp_path: Path) -> None:
    # "notes" is unique in sample but matches no identifier-name heuristic
    # and no other column qualifies either -> no identifier at all.
    (tmp_path / "widgets.csv").write_text("notes,name\nfirst,alpha\nsecond,beta\nthird,gamma\n")
    model = infer_candidate_model(profile_samples(tmp_path))

    cls = _class(model, "WidgetsRecord")
    slot = _slot(cls, "notes")
    assert slot.identifier is False
    assert all(not s.identifier for s in cls.slots)


def test_identifier_negative_id_column_with_duplicate_not_selected(tmp_path: Path) -> None:
    # "id" is name-eligible but has a duplicate value, so it fails
    # unique_in_sample and must not be selected as the identifier.
    (tmp_path / "widgets.csv").write_text("id,name\nw1,alpha\nw1,beta\nw3,gamma\n")
    model = infer_candidate_model(profile_samples(tmp_path))

    cls = _class(model, "WidgetsRecord")
    slot = _slot(cls, "id")
    assert slot.identifier is False
    assert all(not s.identifier for s in cls.slots)


def test_identifier_priority_id_over_uuid_when_both_qualify(tmp_path: Path) -> None:
    (tmp_path / "widgets.csv").write_text(
        "id,uuid,name\nw1,u-aaaa,alpha\nw2,u-bbbb,beta\nw3,u-cccc,gamma\n"
    )
    model = infer_candidate_model(profile_samples(tmp_path))

    cls = _class(model, "WidgetsRecord")
    assert _slot(cls, "id").identifier is True
    assert _slot(cls, "uuid").identifier is False


def test_self_referential_identifier_with_ies_to_y_singularization(tmp_path: Path) -> None:
    # table "companies" -> singular "company" -> self-ref id name "company_id".
    (tmp_path / "companies.csv").write_text("company_id,name\nco1,Acme\nco2,Globex\nco3,Initech\n")
    model = infer_candidate_model(profile_samples(tmp_path))

    cls = _class(model, "CompaniesRecord")
    assert _slot(cls, "company_id").identifier is True
