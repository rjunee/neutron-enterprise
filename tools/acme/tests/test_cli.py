# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne-acme`` CLI: `generate` writes the artifacts; `check` detects drift."""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

from ne.acme import cli
from ne.acme.generator import ARTIFACTS, generate, serialize

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "acme-v1"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_generate_writes_artifacts(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    exit_code = cli.main(["generate", "--seed", "74", "--out", str(out_dir)])
    assert exit_code == 0

    expected = serialize(generate(74), 74)
    for name in ARTIFACTS:
        written = out_dir / name
        assert written.is_file()
        assert written.read_bytes() == expected[name]


def test_check_detects_drift(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fixtures_copy = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures_copy)

    # Uncorrupted copy: clean.
    assert cli.main(["check", "--seed", "74", "--fixtures", str(fixtures_copy)]) == 0

    # Corrupt one byte of acl.json.
    acl_path = fixtures_copy / "acl.json"
    data = bytearray(acl_path.read_bytes())
    data[-2] ^= 0xFF
    acl_path.write_bytes(bytes(data))

    exit_code = cli.main(["check", "--seed", "74", "--fixtures", str(fixtures_copy)])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "acl.json" in out


def test_default_fixtures_dir_in_checkout_is_committed_fixtures() -> None:
    """NE-CLEAN3 item 3: the checkout-first default must still be the
    committed ``tools/acme/fixtures/acme-v1`` directory — no behavior change
    for the dev/CI default."""
    assert cli.default_fixtures_dir() == FIXTURES
    assert FIXTURES.is_dir()


def test_installed_fixtures_dir_resolves_without_repo_layout(tmp_path: Path) -> None:
    """``_installed_fixtures_dir`` resolves purely from the injected scheme-paths
    mapping — zero dependence on any repo/checkout layout."""
    (tmp_path / "acme-v1").mkdir()
    resolved = cli._installed_fixtures_dir({"data": str(tmp_path)})
    assert resolved == tmp_path / "acme-v1"


def test_default_falls_back_to_installed_when_checkout_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback ordering: checkout -> installed -> checkout (preserving the
    historical missing-dir failure mode when neither exists)."""
    nowhere = tmp_path / "nowhere"
    installed = tmp_path / "installed" / "acme-v1"
    installed.mkdir(parents=True)

    monkeypatch.setattr(cli, "_checkout_fixtures_dir", lambda: nowhere)
    monkeypatch.setattr(cli, "_installed_fixtures_dir", lambda *a, **k: installed)
    assert cli.default_fixtures_dir() == installed

    also_nowhere = tmp_path / "also-nowhere"
    monkeypatch.setattr(cli, "_installed_fixtures_dir", lambda *a, **k: also_nowhere)
    assert cli.default_fixtures_dir() == nowhere


def test_wheel_ships_fixtures_as_data() -> None:
    """Packaging regression lock: the fixtures corpus ships as wheel DATA
    (``$prefix/acme-v1``) via ``[tool.uv.build-backend] data`` — NE-CLEAN3
    item 3's whole design depends on this key staying present."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert config["tool"]["uv"]["build-backend"]["data"]["data"] == "fixtures"
