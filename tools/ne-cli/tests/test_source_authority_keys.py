# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused controls for ``ne source-authority-keys``."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from ne.cli import main
from ne.cli.source_authority_keys import (
    HMAC_KEY_FIELD,
    KEY_BYTES,
    KEY_ID,
    KEY_ID_FIELD,
    SECRET_PATH,
    decode_hmac_key,
    key_fingerprint,
)
from ne.secrets import FileSecretStore

source_authority_keys_module = importlib.import_module("ne.cli.source_authority_keys")


class CountingStore:
    """Small transparent write counter around the real versioned store."""

    def __init__(self, store: FileSecretStore) -> None:
        self.store = store
        self.write_count = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    async def write(self, path: str, data: dict[str, str]):
        self.write_count += 1
        return await self.store.write(path, data)


def _invoke(*args: str):
    return CliRunner().invoke(main, list(args))


def _store(tmp_path: Path) -> CountingStore:
    store = FileSecretStore(tmp_path / "secrets.ne.json")
    asyncio.run(store.list_secrets(""))
    return CountingStore(store)


def _bind_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: CountingStore) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setattr(source_authority_keys_module, "_store_from_bundle", lambda _bundle: store)
    return bundle


def _read(store: CountingStore):
    return asyncio.run(store.read(SECRET_PATH))


def test_provision_writes_one_canonical_key_and_emits_closed_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(tmp_path)
    bundle = _bind_store(monkeypatch, tmp_path, store)

    result = _invoke("source-authority-keys", "provision", "--bundle", str(bundle))

    assert result.exit_code == 0, result.output
    assert store.write_count == 1
    version = _read(store)
    assert dict(version.data).keys() == {KEY_ID_FIELD, HMAC_KEY_FIELD}
    assert version.data[KEY_ID_FIELD] == KEY_ID
    raw = decode_hmac_key(version.data[HMAC_KEY_FIELD])
    assert len(raw) == KEY_BYTES
    receipt = json.loads(result.output)
    assert receipt == {
        "fingerprint": key_fingerprint(raw),
        "key_id": KEY_ID,
        "path": SECRET_PATH,
        "secret_version": 1,
        "status": "provisioned",
    }
    assert version.data[HMAC_KEY_FIELD] not in result.output
    assert raw.hex() not in result.output


def test_provision_uses_the_bundle_secret_store_factory(tmp_path: Path) -> None:
    """The advertised CLI works through the real Pilot bundle configuration."""
    secrets_path = tmp_path / "secrets.ne.json"
    bundle = tmp_path / "bundle"
    (bundle / "config").mkdir(parents=True)
    (bundle / "config" / "ne-secrets.toml").write_text(
        f'store = "sops-file"\n\n[file]\npath = "{secrets_path}"\nmode = "plain"\n',
        encoding="utf-8",
    )

    result = _invoke("source-authority-keys", "provision", "--bundle", str(bundle))

    assert result.exit_code == 0, result.output
    version = asyncio.run(FileSecretStore(secrets_path).read(SECRET_PATH))
    assert version.version == 1
    assert version.data[KEY_ID_FIELD] == KEY_ID
    assert len(decode_hmac_key(version.data[HMAC_KEY_FIELD])) == KEY_BYTES


def test_second_provision_refuses_without_a_second_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(tmp_path)
    bundle = _bind_store(monkeypatch, tmp_path, store)
    assert _invoke("source-authority-keys", "provision", "--bundle", str(bundle)).exit_code == 0
    before = dict(_read(store).data)

    result = _invoke("source-authority-keys", "provision", "--bundle", str(bundle))

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert store.write_count == 1
    assert dict(_read(store).data) == before


@pytest.mark.parametrize(
    "data",
    [
        {KEY_ID_FIELD: KEY_ID},
        {HMAC_KEY_FIELD: "A" * 43},
        {KEY_ID_FIELD: "bad id", HMAC_KEY_FIELD: "A" * 43},
        {KEY_ID_FIELD: KEY_ID, HMAC_KEY_FIELD: "not-base64!"},
        {KEY_ID_FIELD: KEY_ID, HMAC_KEY_FIELD: "A" * 43, "extra": "field"},
    ],
)
def test_malformed_existing_secret_is_never_repaired_or_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: dict[str, str]
) -> None:
    store = _store(tmp_path)
    asyncio.run(store.write(SECRET_PATH, data))
    before_writes = store.write_count
    bundle = _bind_store(monkeypatch, tmp_path, store)

    result = _invoke("source-authority-keys", "provision", "--bundle", str(bundle))

    assert result.exit_code != 0
    assert "malformed" in result.output
    assert "refusing to replace" in result.output
    assert store.write_count == before_writes
    assert dict(_read(store).data) == data


def test_tombstoned_history_is_not_resurrected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(tmp_path)
    encoded = base64.urlsafe_b64encode(b"x" * KEY_BYTES).decode().rstrip("=")
    asyncio.run(store.write(SECRET_PATH, {KEY_ID_FIELD: KEY_ID, HMAC_KEY_FIELD: encoded}))
    asyncio.run(store.delete(SECRET_PATH))
    before_writes = store.write_count
    bundle = _bind_store(monkeypatch, tmp_path, store)

    result = _invoke("source-authority-keys", "provision", "--bundle", str(bundle))

    assert result.exit_code != 0
    assert "refusing to resurrect" in result.output
    assert store.write_count == before_writes


def test_status_is_read_only_and_binds_fingerprint_to_secret_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(tmp_path)
    bundle = _bind_store(monkeypatch, tmp_path, store)
    assert _invoke("source-authority-keys", "provision", "--bundle", str(bundle)).exit_code == 0
    first = _read(store)
    # Status binds the latest SecretVersion receipt rather than assuming every
    # readable store is version 1. (Provision itself is still one-shot.)
    asyncio.run(store.write(SECRET_PATH, dict(first.data)))
    version = _read(store)
    assert version.version == 2
    raw = decode_hmac_key(version.data[HMAC_KEY_FIELD])
    before_writes = store.write_count

    result = _invoke("source-authority-keys", "status", "--bundle", str(bundle))

    assert result.exit_code == 0, result.output
    assert store.write_count == before_writes
    assert json.loads(result.output) == {
        "fingerprint": key_fingerprint(raw),
        "key_id": KEY_ID,
        "path": SECRET_PATH,
        "secret_version": int(version.version),
        "status": "ready",
    }
    assert version.data[HMAC_KEY_FIELD] not in result.output
    assert raw.hex() not in result.output


def test_status_fails_closed_when_absent_or_malformed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(tmp_path)
    bundle = _bind_store(monkeypatch, tmp_path, store)
    absent = _invoke("source-authority-keys", "status", "--bundle", str(bundle))
    assert absent.exit_code != 0
    assert "no readable continuation key" in absent.output

    asyncio.run(store.write(SECRET_PATH, {KEY_ID_FIELD: KEY_ID, HMAC_KEY_FIELD: "short"}))
    malformed = _invoke("source-authority-keys", "status", "--bundle", str(bundle))
    assert malformed.exit_code != 0
    assert "malformed key material" in malformed.output
    assert "short" not in malformed.output
