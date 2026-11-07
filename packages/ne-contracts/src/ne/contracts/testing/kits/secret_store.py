# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SecretStore conformance kit — Vault KV v2 semantics.

Async contract methods are exercised via ``asyncio.run`` (pytest-asyncio is not
a dependency and must not be added).
"""

from __future__ import annotations

import asyncio

import pytest

from ne.contracts.errors import SecretNotFoundError
from ne.contracts.secrets import SecretStore


class SecretStoreContractKit:
    """Subclass and override :meth:`create_store` to prove SecretStore conformance."""

    def create_store(self) -> SecretStore:
        """Return a fresh, empty SecretStore adapter under test."""
        raise NotImplementedError

    def test_write_creates_version_1(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            v = await store.write("app/db", {"password": "s3cr3t"})
            assert v.version == 1
            assert v.data["password"] == "s3cr3t"

        asyncio.run(scenario())

    def test_second_write_increments_version(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.write("app/db", {"password": "one"})
            v2 = await store.write("app/db", {"password": "two"})
            assert v2.version == 2

        asyncio.run(scenario())

    def test_read_latest_returns_newest(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.write("app/db", {"password": "one"})
            await store.write("app/db", {"password": "two"})
            latest = await store.read("app/db")
            assert latest.version == 2
            assert latest.data["password"] == "two"

        asyncio.run(scenario())

    def test_read_pinned_version(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.write("app/db", {"password": "one"})
            await store.write("app/db", {"password": "two"})
            v1 = await store.read("app/db", version=1)
            assert v1.version == 1
            assert v1.data["password"] == "one"

        asyncio.run(scenario())

    def test_read_missing_raises(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            with pytest.raises(SecretNotFoundError):
                await store.read("app/nope")

        asyncio.run(scenario())

    def test_soft_delete_then_read_raises(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.write("app/db", {"password": "one"})
            await store.delete("app/db")
            with pytest.raises(SecretNotFoundError):
                await store.read("app/db")

        asyncio.run(scenario())

    def test_undelete_restores(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.write("app/db", {"password": "one"})
            await store.delete("app/db", versions=[1])
            await store.undelete("app/db", versions=[1])
            restored = await store.read("app/db", version=1)
            assert restored.data["password"] == "one"

        asyncio.run(scenario())

    def test_destroy_is_permanent(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.write("app/db", {"password": "one"})
            await store.destroy("app/db", versions=[1])
            with pytest.raises(SecretNotFoundError):
                await store.undelete("app/db", versions=[1])
            with pytest.raises(SecretNotFoundError):
                await store.read("app/db", version=1)

        asyncio.run(scenario())

    def test_list_secrets_prefix(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.write("app/db", {"k": "v"})
            await store.write("app/cache", {"k": "v"})
            await store.write("other/x", {"k": "v"})
            names = await store.list_secrets("app/")
            assert set(names) == {"app/db", "app/cache"}

        asyncio.run(scenario())
