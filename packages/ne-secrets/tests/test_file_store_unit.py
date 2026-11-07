# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Below-the-seam unit tests for FileSecretStore: durability, destroy, atomicity."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from ne.contracts.errors import SecretNotFoundError
from ne.secrets.file_store import FileSecretStore


def _run(coro):
    return asyncio.run(coro)


def test_second_instance_reads_versions_written_by_first(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"

    async def scenario() -> None:
        store_a = FileSecretStore(path)
        await store_a.write("app/db", {"password": "one"})
        await store_a.write("app/db", {"password": "two"})

        store_b = FileSecretStore(path)
        v1 = await store_b.read("app/db", version=1)
        v2 = await store_b.read("app/db", version=2)
        assert v1.data["password"] == "one"
        assert v2.data["password"] == "two"

    _run(scenario())


def test_destroy_removes_data_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"

    async def scenario() -> None:
        store = FileSecretStore(path)
        await store.write("app/db", {"password": "top-secret-value"})
        await store.destroy("app/db", versions=[1])

    _run(scenario())

    raw = path.read_text(encoding="utf-8")
    assert "top-secret-value" not in raw
    doc = json.loads(raw)
    entry = doc["secrets"]["app/db"][0]
    assert entry["destroyed"] is True
    assert "data" not in entry


def test_soft_deleted_survives_reload_then_undelete(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"

    async def scenario() -> None:
        store_a = FileSecretStore(path)
        await store_a.write("app/db", {"password": "one"})
        await store_a.delete("app/db")

        store_b = FileSecretStore(path)
        with pytest.raises(SecretNotFoundError):
            await store_b.read("app/db")

        await store_b.undelete("app/db", versions=[1])
        restored = await store_b.read("app/db")
        assert restored.data["password"] == "one"

    _run(scenario())


def test_atomic_write_leaves_valid_file_with_0600_mode(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"

    async def scenario() -> None:
        store = FileSecretStore(path)
        await store.write("app/db", {"password": "one"})

    _run(scenario())

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    # Valid JSON, and no leftover temp files.
    json.loads(path.read_text(encoding="utf-8"))
    leftovers = [p for p in path.parent.iterdir() if p != path]
    assert leftovers == []


def test_unknown_path_on_delete_raises_not_found(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "secrets.json")

    async def scenario() -> None:
        with pytest.raises(SecretNotFoundError):
            await store.delete("no/such/path")

    _run(scenario())


def test_unknown_version_on_undelete_raises_not_found(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "secrets.json")

    async def scenario() -> None:
        await store.write("app/db", {"password": "one"})
        with pytest.raises(SecretNotFoundError):
            await store.undelete("app/db", versions=[99])

    _run(scenario())


def test_unknown_version_on_destroy_raises_not_found(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "secrets.json")

    async def scenario() -> None:
        await store.write("app/db", {"password": "one"})
        with pytest.raises(SecretNotFoundError):
            await store.destroy("app/db", versions=[99])

    _run(scenario())


def test_default_codec_is_plain_json(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    store = FileSecretStore(path)

    async def scenario() -> None:
        await store.write("app/db", {"password": "one"})

    _run(scenario())

    # PlainJsonCodec writes readable JSON (no encryption).
    raw = path.read_text(encoding="utf-8")
    assert "one" in raw
    assert json.loads(raw)["format"] == "ne-secrets/kv2"


def test_missing_file_reads_as_empty_document(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "does-not-exist.json")

    async def scenario() -> None:
        names = await store.list_secrets("")
        assert names == []

    _run(scenario())
