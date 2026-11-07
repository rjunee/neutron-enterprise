# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Meridian M365 persona ACL mapping generation and fail-closed validation."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from ne.permissions.mapping import AclMappingSource

permissions = importlib.import_module("ne.cli.permissions").permissions

GROUPS = {
    "meridian-ops": "306773cc-b05a-43b4-bbb2-7858bfdb67dc",
    "meridian-approver": "ab98c77c-1310-4e50-9427-a91d6bbfc3e3",
    "meridian-frontline": "d9ec7a2f-41c3-48d7-b310-a13ce806c339",
}


def _write_principals(path: Path, groups: object = GROUPS) -> None:
    path.write_text(
        json.dumps({"m365": {"users": {"ignored": "private"}, "groups": groups}}),
        encoding="utf-8",
    )


def _invoke(principals: Path, *extra: str):
    return CliRunner().invoke(
        permissions,
        ["mapping", "generate-meridian-m365", "--principals", str(principals), *extra],
    )


def test_emits_exact_three_direction_marked_persona_grants(tmp_path: Path) -> None:
    principals = tmp_path / "principals.json"
    output = tmp_path / "mapping.json"
    _write_principals(principals)

    result = _invoke(principals, "--out", str(output))

    assert result.exit_code == 0, result.output
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document == {
        "name": "meridian-m365-demo-acl-mapping",
        "mappings": [
            {
                "principal": "meridian-ops",
                "kind": "group",
                "grants": [f"m365-group:{GROUPS['meridian-ops']}"],
            },
            {
                "principal": "meridian-approver",
                "kind": "group",
                "grants": [f"m365-group:{GROUPS['meridian-approver']}"],
            },
            {
                "principal": "meridian-frontline",
                "kind": "group",
                "grants": [f"m365-group:{GROUPS['meridian-frontline']}"],
            },
        ],
    }
    snapshot = asyncio.run(AclMappingSource.from_data(document).fetch({}))
    assert {edge.group_id for edge in snapshot.edges} == set(GROUPS.values())
    assert "generated 3 Meridian M365 persona grants" in result.stderr


@pytest.mark.parametrize(
    "groups",
    [
        {"meridian-ops": GROUPS["meridian-ops"]},
        {**GROUPS, "extra": "11111111-1111-1111-1111-111111111111"},
        {**GROUPS, "meridian-ops": "not-a-uuid"},
        {**GROUPS, "meridian-ops": GROUPS["meridian-approver"]},
    ],
)
def test_unattested_principals_write_nothing(tmp_path: Path, groups: object) -> None:
    principals = tmp_path / "principals.json"
    output = tmp_path / "mapping.json"
    _write_principals(principals, groups)

    result = _invoke(principals, "--out", str(output))

    assert result.exit_code == 1
    assert "Mapping is unattested; M365 content remains invisible" in result.output
    assert not output.exists()


def test_help_names_only_owner_input_and_atomic_output_controls() -> None:
    result = CliRunner().invoke(permissions, ["mapping", "generate-meridian-m365", "--help"])

    assert result.exit_code == 0, result.output
    assert "--principals" in result.output
    assert "--out" in result.output
    assert "--force" in result.output
    assert "--manifest" not in result.output
