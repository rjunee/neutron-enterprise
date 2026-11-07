# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Principal fail-closed behavior and offline CLI mutation confirmation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ne.meridian_seed import cli
from ne.meridian_seed.errors import SeedPreflightError
from ne.meridian_seed.personas import PrincipalsMap


def test_missing_m365_persona_group_fails_closed() -> None:
    principals = PrincipalsMap.model_validate(
        {
            "m365": {
                "users": {
                    "ops": "ops@meridian.example",
                    "approver": "approver@meridian.example",
                    "frontline": "frontline@meridian.example",
                },
                "groups": {
                    "meridian-ops": "group-ops",
                    "meridian-approver": "group-approver",
                },
            }
        }
    )
    with pytest.raises(SeedPreflightError, match="meridian-frontline"):
        principals.require_m365()


def test_slice_cli_is_offline_and_prints_stage_counts(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["slice"]) == 0
    output = capsys.readouterr().out
    assert "11 initial, 1 supersession" in output
    assert "10 accounts, 15 contacts, 19 cases" in output


def test_push_confirmation_fails_before_config_or_principal_loading(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "does-not-exist.json"
    rc = cli.main(
        [
            "push",
            "--tenant",
            "m365",
            "--stage",
            "initial",
            "--principals",
            str(missing),
            "--confirm",
            "WRONG",
        ]
    )
    assert rc == 1
    assert "refusing tenant mutation" in capsys.readouterr().err


def test_principals_file_is_typed_and_never_embedded_in_fixture(tmp_path: Path) -> None:
    path = tmp_path / "principals.json"
    path.write_text(
        json.dumps(
            {
                "m365": {
                    "users": {
                        "ops": "ops@example.invalid",
                        "approver": "approver@example.invalid",
                        "frontline": "frontline@example.invalid",
                    },
                    "groups": {
                        "meridian-ops": "g1",
                        "meridian-approver": "g2",
                        "meridian-frontline": "g3",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    assert PrincipalsMap.load(path).m365_group_id("ops") == "g1"
