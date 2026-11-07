# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Coordinator controls for the quiesced protected-transport migration.

The fixed Pilot adapter's SQL runs in its own isolated real-Postgres test. This
file exercises the async coordinator and the real versioned SecretStore contract;
the historical filename is retained because the promoted lane already names it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from ne.contracts.testing.memory import MemorySecretStore
from ne.runtime.legacy_transport_migration import (
    DATABASES,
    BackupReceipt,
    CutoverRefused,
    Inventory,
    Phase,
    SecretStoreJournal,
    classify_legacy,
    cutover,
)

SCHEMAS = {"neutron": "platform-v1", "ne_dbos": "dbos-2.26.0"}
DIGEST = "sha256:" + "a" * 64


def inventory(name: str, *, legacy: int, plaintext: int = 0) -> Inventory:
    return Inventory(name, SCHEMAS[name], legacy, {"legacy.payload": plaintext}, 0)


@dataclass
class Port:
    final_plaintext: int = 0
    extra_backup_database: bool = False
    migrate_failures: int = 0
    events: list[str] = field(default_factory=list)
    scans: int = 0
    zero: bool = True

    async def lock(self) -> None:
        self.events.append("lock")

    async def unlock(self) -> None:
        self.events.append("unlock")

    async def scale_zero(self) -> None:
        self.zero = True
        self.events.append("zero")

    async def roots_zero(self) -> bool:
        return self.zero

    async def backup(self) -> BackupReceipt:
        self.events.append("backup")
        databases = DATABASES | ({"foreign"} if self.extra_backup_database else set())
        return BackupReceipt("b1", "b" * 64, "fp", frozenset(databases), True)

    async def inventory(self) -> dict[str, Inventory]:
        self.scans += 1
        self.events.append("scan")
        before = self.scans == 1
        return {
            name: inventory(
                name,
                legacy=1 if before else 0,
                plaintext=1 if before else self.final_plaintext,
            )
            for name in SCHEMAS
        }

    async def retire(self, receipt: BackupReceipt) -> str:
        assert receipt.backup_id == "b1"
        self.events.append("retire")
        return "f" * 64

    async def migrate(self, retirement_fence: str) -> None:
        assert retirement_fence == "f" * 64
        self.events.append("migrate")
        if self.migrate_failures:
            self.migrate_failures -= 1
            raise RuntimeError("injected migration interruption")

    async def fence(self, generation: str, image_digest: str) -> None:
        self.events.append("fence")

    async def start(self, root: str, generation: str, image_digest: str) -> None:
        self.zero = False
        self.events.append(f"start:{root}")

    async def ready(self, root: str, generation: str, image_digest: str) -> bool:
        return True


def test_both_roots_stay_zero_through_verified_backup_migration_and_fences() -> None:
    port = Port()
    store = MemorySecretStore()
    receipt = asyncio.run(
        cutover(
            port,
            image_digest=DIGEST,
            schemas=SCHEMAS,
            journal=SecretStoreJournal(store),
        )
    )
    assert receipt.backup_id == "b1"
    assert port.events == [
        "lock",
        "zero",
        "backup",
        "scan",
        "retire",
        "migrate",
        "scan",
        "fence",
        "start:ne-worker",
        "start:ne-api",
        "unlock",
    ]
    latest = asyncio.run(store.read(SecretStoreJournal.PATH))
    assert latest.version == len(Phase)
    assert latest.data["phase"] == Phase.COMPLETE.value
    assert "prompt" not in repr(latest.data).lower()


def test_interrupted_retirement_resumes_without_repeating_backup_or_retire() -> None:
    port = Port(migrate_failures=1)
    journal = SecretStoreJournal(MemorySecretStore())
    with pytest.raises(RuntimeError, match="injected migration interruption"):
        asyncio.run(cutover(port, image_digest=DIGEST, schemas=SCHEMAS, journal=journal))
    phase, data, version = asyncio.run(journal.latest())
    assert (phase, version, data["fence"]) == (Phase.RETIRED, 3, "f" * 64)
    assert port.events.count("backup") == 1
    assert port.events.count("retire") == 1

    asyncio.run(cutover(port, image_digest=DIGEST, schemas=SCHEMAS, journal=journal))
    assert port.events.count("backup") == 1
    assert port.events.count("retire") == 1
    assert port.events.count("migrate") == 2
    assert asyncio.run(journal.latest())[0] is Phase.COMPLETE


def test_journal_refuses_out_of_order_history_and_metadata_mutation() -> None:
    store = MemorySecretStore()
    journal = SecretStoreJournal(store)
    asyncio.run(store.write(journal.PATH, {"phase": Phase.BACKED_UP.value}))
    with pytest.raises(CutoverRefused, match="out of order"):
        asyncio.run(journal.latest())

    clean = SecretStoreJournal(MemorySecretStore())
    asyncio.run(clean.append(Phase.QUIESCED))
    asyncio.run(
        clean.append(
            Phase.BACKED_UP,
            backup_id="b1",
            manifest_sha256="b" * 64,
            kek_fingerprint="fp",
        )
    )
    asyncio.run(clean.append(Phase.RETIRED, fence="f" * 64))
    with pytest.raises(CutoverRefused, match="cannot be changed"):
        asyncio.run(clean.append(Phase.MIGRATED, fence="e" * 64))


def test_backup_receipt_must_name_exactly_both_databases() -> None:
    port = Port(extra_backup_database=True)
    with pytest.raises(CutoverRefused, match="both quiesced databases"):
        asyncio.run(
            cutover(
                port,
                image_digest=DIGEST,
                schemas=SCHEMAS,
                journal=SecretStoreJournal(MemorySecretStore()),
            )
        )
    assert "retire" not in port.events


def test_remaining_plaintext_refuses_restart_and_stays_zero() -> None:
    port = Port(final_plaintext=1)
    with pytest.raises(CutoverRefused, match="plaintext"):
        asyncio.run(
            cutover(
                port,
                image_digest=DIGEST,
                schemas=SCHEMAS,
                journal=SecretStoreJournal(MemorySecretStore()),
            )
        )
    assert "fence" not in port.events
    assert not any(event.startswith("start:") for event in port.events)
    assert port.events[-2:] == ["zero", "unlock"]


def test_ambiguous_legacy_is_unavailable_not_source_free() -> None:
    assert classify_legacy(has_authenticated_source_proof=False) == "legacy_unavailable"
