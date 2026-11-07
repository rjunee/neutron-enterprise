# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Persona constants are locked to the corpus; principals fail closed."""

from __future__ import annotations

import json

import pytest

from ne.acme.cli import default_fixtures_dir
from ne.acme_seed.errors import SeedPreflightError
from ne.acme_seed.personas import PERSONA_GROUP, PrincipalsMap

from .conftest import full_principals


def _org() -> dict:
    return json.loads((default_fixtures_dir() / "org.json").read_text(encoding="utf-8"))


def test_personas_match_org_chart() -> None:
    org = _org()
    members = {m["person_id"]: m for m in org["members"]}
    groups = {g["name"]: set(g["member_ids"]) for g in org["groups"]}

    assert members["per-0000"]["grade"] == "exec"
    assert "per-0000" in groups["executive"]
    assert "per-0006" in groups["dept-engineering"]
    assert "per-0016" in groups["dept-sales"]
    assert "per-0038" in groups["dept-legal"]

    # PERSONA_GROUP matches the org chart membership.
    for persona, group in PERSONA_GROUP.items():
        assert persona in groups[group]


def test_principals_file_roundtrip_and_missing_group_fails_closed() -> None:
    principals = full_principals()
    assert principals.m365_group_id("dept-legal") == "g-legal-id"
    assert principals.m365_user_id("per-0006") == "u-eng@acme.example"
    assert principals.slack_user_id("per-0016") == "U0016SALES"
    principals.require_m365()  # complete map: no raise.

    # A map missing an M365 group raises BEFORE any client construction.
    partial = PrincipalsMap.model_validate(
        {
            "m365": {
                "users": {
                    "per-0000": "u0",
                    "per-0006": "u6",
                    "per-0016": "u16",
                    "per-0038": "u38",
                },
                "groups": {"executive": "g-exec", "dept-engineering": "g-eng"},
            }
        }
    )
    with pytest.raises(SeedPreflightError):
        partial.require_m365()
