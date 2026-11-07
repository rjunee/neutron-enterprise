# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T-12: the authorization model is minimal, additive, and deny-operator-free."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from ne.permissions_rebac.bootstrap import load_model


def _fga_text() -> str:
    return (
        resources.files("ne.permissions_rebac")
        .joinpath("model/neutron.fga")
        .read_text(encoding="utf-8")
    )


def test_model_json_parses_and_has_exact_types() -> None:
    model = load_model()
    assert model["schema_version"] == "1.1"
    types = {td["type"] for td in model["type_definitions"]}
    assert types == {"user", "group", "document"}


def test_relations_exact() -> None:
    model = load_model()
    by_type = {td["type"]: td for td in model["type_definitions"]}
    assert "relations" not in by_type["user"]  # user has no relations
    assert set(by_type["group"]["relations"]) == {"member"}
    assert set(by_type["document"]["relations"]) == {"owner", "viewer"}


def test_viewer_is_union_of_direct_and_computed_owner() -> None:
    model = load_model()
    document = next(td for td in model["type_definitions"] if td["type"] == "document")
    viewer = document["relations"]["viewer"]
    children = viewer["union"]["child"]
    assert {"this": {}} in children
    assert {"computedUserset": {"relation": "owner"}} in children


def test_no_deny_or_exclusion_operator_anywhere() -> None:
    model = load_model()
    serialized = json.dumps(model)
    assert "difference" not in serialized
    assert "intersection" not in serialized
    assert "exclusion" not in serialized


def test_fga_text_uses_or_owner_and_no_but_not() -> None:
    text = _fga_text()
    assert "or owner" in text
    assert "but not" not in text


def test_two_model_files_agree_on_types_and_relations() -> None:
    text = _fga_text()
    model = load_model()
    # every type/relation named in the JSON appears in the .fga mirror.
    for td in model["type_definitions"]:
        assert f"type {td['type']}" in text
        for relation in td.get("relations", {}):
            assert f"define {relation}" in text


def test_directly_related_user_types_include_group_member() -> None:
    model: dict[str, Any] = load_model()
    document = next(td for td in model["type_definitions"] if td["type"] == "document")
    drut = document["metadata"]["relations"]["viewer"]["directly_related_user_types"]
    assert {"type": "user"} in drut
    assert {"type": "group", "relation": "member"} in drut
