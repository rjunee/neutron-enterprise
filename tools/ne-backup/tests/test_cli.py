# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Click surfaces: keygen prints the fingerprint (never the key); create/list/
verify/prune work against a fake runner; restore needs confirmation."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from ne.backup import cli
from ne.backup.runtime import verify_backup as real_verify
from ne.secrets import FileSecretStore, PlainJsonCodec

from .conftest import Harness, make_harness


@pytest.fixture
def harness() -> Harness:
    return make_harness(databases=["neutron"], collections={"drill": 3})


@pytest.fixture(autouse=True)
def _patch_services(monkeypatch: pytest.MonkeyPatch, harness: Harness) -> None:
    monkeypatch.setattr(cli, "_build_services", lambda config, **_: harness.services())


def _stored_key_b64(bundle: Path) -> str:
    store = FileSecretStore(bundle / "secrets.ne.json", PlainJsonCodec())
    version = asyncio.run(store.read("pilot/backup"))
    return version.data["key_b64"]


def test_keygen_prints_fingerprint_not_key(bundle: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli.main, ["keygen", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    assert "fingerprint:" in result.output
    key_b64 = _stored_key_b64(bundle)
    key_raw = base64.b64decode(key_b64)
    assert key_b64 not in result.output
    assert key_raw.hex() not in result.output


def _keygen(bundle: Path) -> None:
    assert CliRunner().invoke(cli.main, ["keygen", "--bundle", str(bundle)]).exit_code == 0


def test_keygen_refuses_to_overwrite_existing_kek(bundle: Path) -> None:
    runner = CliRunner()
    first = runner.invoke(cli.main, ["keygen", "--bundle", str(bundle)])
    assert first.exit_code == 0, first.output
    original_key = _stored_key_b64(bundle)

    # A second keygen without --rotate must fail and leave the key untouched.
    second = runner.invoke(cli.main, ["keygen", "--bundle", str(bundle)])
    assert second.exit_code != 0
    assert "already exists" in second.output
    assert _stored_key_b64(bundle) == original_key


def test_keygen_rotate_replaces_key_with_explicit_flag(bundle: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(cli.main, ["keygen", "--bundle", str(bundle)]).exit_code == 0
    original_key = _stored_key_b64(bundle)

    rotated = runner.invoke(cli.main, ["keygen", "--bundle", str(bundle), "--rotate"])
    assert rotated.exit_code == 0, rotated.output
    assert "rotated" in rotated.output
    assert "WARNING" in rotated.output
    new_key = _stored_key_b64(bundle)
    assert new_key != original_key
    # The rotated key is not leaked in output either.
    assert new_key not in rotated.output


def test_create_list_verify_prune(bundle: Path, dest: Path) -> None:
    _keygen(bundle)
    runner = CliRunner()
    created = runner.invoke(cli.main, ["create", "--bundle", str(bundle), "--dest", str(dest)])
    assert created.exit_code == 0, created.output
    assert "Backup complete" in created.output

    listed = runner.invoke(cli.main, ["list", "--dest", str(dest)])
    assert listed.exit_code == 0
    backup_id = listed.output.split()[0]

    verified = runner.invoke(
        cli.main, ["verify", backup_id, "--dest", str(dest), "--bundle", str(bundle)]
    )
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.output)["plaintext_verified"] is True

    pruned = runner.invoke(cli.main, ["prune", "--dest", str(dest), "--keep", "1"])
    assert pruned.exit_code == 0


def test_restore_requires_confirmation(bundle: Path, dest: Path) -> None:
    _keygen(bundle)
    runner = CliRunner()
    runner.invoke(cli.main, ["create", "--bundle", str(bundle), "--dest", str(dest)])
    backup_id = runner.invoke(cli.main, ["list", "--dest", str(dest)]).output.split()[0]

    # Declining the confirmation aborts (non-zero exit, nothing restored).
    declined = runner.invoke(
        cli.main,
        ["restore", backup_id, "--bundle", str(bundle), "--dest", str(dest)],
        input="n\n",
    )
    assert declined.exit_code != 0

    # --yes proceeds without a prompt.
    accepted = runner.invoke(
        cli.main,
        ["restore", backup_id, "--bundle", str(bundle), "--dest", str(dest), "--yes"],
    )
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.output)["verified"] is True


def test_verify_threads_staging_dir_from_config(
    bundle: Path, dest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`verify --config` applies the documented staging_dir override, so multi-GB
    plaintext verification can be pointed at disk-backed storage like create/restore."""
    _keygen(bundle)
    runner = CliRunner()
    runner.invoke(cli.main, ["create", "--bundle", str(bundle), "--dest", str(dest)])
    backup_id = runner.invoke(cli.main, ["list", "--dest", str(dest)]).output.split()[0]

    stage = tmp_path / "stage-root"  # deliberately nonexistent: created on demand
    config_file = tmp_path / "backup.toml"
    config_file.write_text(f'staging_dir = "{stage}"\n', encoding="utf-8")

    seen: dict[str, Path | None] = {}

    def recording_verify(
        dest_arg: Path,
        backup_id_arg: str,
        kek: bytes | None = None,
        *,
        staging_root: Path | None = None,
    ) -> dict[str, object]:
        seen["staging_root"] = staging_root
        return real_verify(dest_arg, backup_id_arg, kek, staging_root=staging_root)

    monkeypatch.setattr(cli, "verify_backup", recording_verify)
    result = runner.invoke(
        cli.main,
        [
            "verify",
            backup_id,
            "--dest",
            str(dest),
            "--bundle",
            str(bundle),
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["staging_root"] == stage
    assert json.loads(result.output)["plaintext_verified"] is True
