# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed protected-transport cutover coordinator.

Side effects are supplied as a fixed port by the Pilot operator adapter.  The
coordinator owns ordering and never accepts SQL, commands, or service names from
the caller.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ne.contracts.errors import SecretNotFoundError
from ne.contracts.secrets import SecretStore

GENERATION = "agent-transport-v1"
DATABASES = frozenset({"neutron", "ne_dbos"})


class CutoverRefused(RuntimeError):
    pass


class Phase(StrEnum):
    QUIESCED = "quiesced"
    BACKED_UP = "backed_up"
    RETIRED = "retired"
    MIGRATED = "migrated"
    SCANNED = "scanned"
    FENCED = "fenced"
    WORKER_READY = "worker_ready"
    API_READY = "api_ready"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    backup_id: str
    manifest_sha256: str
    kek_fingerprint: str
    databases: frozenset[str]
    verified: bool

    def __post_init__(self) -> None:
        _hash(self.manifest_sha256, "manifest_sha256")
        if not self.backup_id or not self.kek_fingerprint:
            raise ValueError("backup identity is incomplete")


@dataclass(frozen=True, slots=True)
class Inventory:
    database: str
    schema_fingerprint: str
    legacy_rows: int
    plaintext_by_surface: Mapping[str, int]
    unknown_wire_rows: int

    def __post_init__(self) -> None:
        if not self.database or not self.schema_fingerprint:
            raise ValueError("inventory identity is incomplete")
        if self.legacy_rows < 0 or self.unknown_wire_rows < 0:
            raise ValueError("inventory counts cannot be negative")
        if not self.plaintext_by_surface or any(
            value < 0 for value in self.plaintext_by_surface.values()
        ):
            raise ValueError("every enumerated legacy surface needs a non-negative count")

    @property
    def plaintext_rows(self) -> int:
        return sum(self.plaintext_by_surface.values())


class CutoverPort(Protocol):
    async def lock(self) -> None: ...
    async def unlock(self) -> None: ...
    async def scale_zero(self) -> None: ...
    async def roots_zero(self) -> bool: ...
    async def backup(self) -> BackupReceipt: ...
    async def inventory(self) -> Mapping[str, Inventory]: ...
    async def retire(self, receipt: BackupReceipt) -> str: ...
    async def migrate(self, retirement_fence: str) -> None: ...
    async def fence(self, generation: str, image_digest: str) -> None: ...
    async def start(self, root: str, generation: str, image_digest: str) -> None: ...
    async def ready(self, root: str, generation: str, image_digest: str) -> bool: ...


def _hash(value: str, name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be lowercase sha256")


def _image_digest(value: str) -> None:
    if not value.startswith("sha256:"):
        raise CutoverRefused("image must be pinned by sha256 digest")
    try:
        _hash(value.removeprefix("sha256:"), "image digest")
    except ValueError as exc:
        raise CutoverRefused("image must be pinned by sha256 digest") from exc


def _inventory_digest(inventories: Mapping[str, Inventory]) -> str:
    body = {
        name: {
            "schema": item.schema_fingerprint,
            "legacy": item.legacy_rows,
            "plaintext": dict(sorted(item.plaintext_by_surface.items())),
            "unknown": item.unknown_wire_rows,
        }
        for name, item in sorted(inventories.items())
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class SecretStoreJournal:
    """Versioned content-free cutover checkpoint at one fixed SecretStore path."""

    PATH = "pilot/agent-transport-cutover"
    _ORDER = tuple(Phase)

    _KEYS = frozenset(
        {
            "phase",
            "backup_id",
            "manifest_sha256",
            "kek_fingerprint",
            "fence",
            "generation",
            "image_digest",
            "inventory_sha256",
        }
    )

    def __init__(self, store: SecretStore) -> None:
        self._store = store

    async def latest(self) -> tuple[Phase | None, Mapping[str, str], int]:
        try:
            version = await self._store.read(self.PATH)
        except SecretNotFoundError:
            return None, {}, 0
        latest_data: dict[str, str] = {}
        for expected_version in range(1, version.version + 1):
            try:
                item = await self._store.read(self.PATH, version=expected_version)
            except SecretNotFoundError as exc:
                raise CutoverRefused("protected cutover journal history is incomplete") from exc
            data = dict(item.data)
            if item.path != self.PATH or item.version != expected_version:
                raise CutoverRefused("protected cutover journal version is not monotonic")
            if set(data) - self._KEYS or "phase" not in data:
                raise CutoverRefused("protected cutover journal is malformed")
            try:
                phase = Phase(data["phase"])
            except ValueError as exc:
                raise CutoverRefused("protected cutover journal is malformed") from exc
            if (
                expected_version > len(self._ORDER)
                or self._ORDER[expected_version - 1] is not phase
            ):
                raise CutoverRefused("protected cutover journal transition is out of order")
            prior = {key: value for key, value in latest_data.items() if key != "phase"}
            current = {key: value for key, value in data.items() if key != "phase"}
            if any(current.get(key) != value for key, value in prior.items()):
                raise CutoverRefused("protected cutover journal metadata was rewritten")
            latest_data = data
        return Phase(latest_data["phase"]), latest_data, version.version

    async def append(self, phase: Phase, **metadata: str) -> int:
        if set(metadata) - (self._KEYS - {"phase"}):
            raise ValueError("journal metadata may not carry content")
        latest, prior, prior_version = await self.latest()
        expected_index = 0 if latest is None else self._ORDER.index(latest) + 1
        if expected_index >= len(self._ORDER) or self._ORDER[expected_index] is not phase:
            raise CutoverRefused("protected cutover journal transition is out of order")
        cumulative = {key: value for key, value in prior.items() if key != "phase"}
        for key, value in metadata.items():
            if key in cumulative and cumulative[key] != value:
                raise CutoverRefused("protected cutover journal metadata cannot be changed")
            cumulative[key] = value
        written = await self._store.write(self.PATH, {"phase": phase.value, **cumulative})
        if written.path != self.PATH or written.version != prior_version + 1:
            raise CutoverRefused("protected cutover journal version is not monotonic")
        return written.version


def validate_inventory(items: Mapping[str, Inventory], schemas: Mapping[str, str]) -> None:
    if set(items) != DATABASES or set(schemas) != DATABASES:
        raise CutoverRefused("both exact databases must be inventoried")
    for name in DATABASES:
        if items[name].database != name or items[name].schema_fingerprint != schemas[name]:
            raise CutoverRefused(f"unsupported {name} schema fingerprint")


def admit_empty_install(items: Mapping[str, Inventory], schemas: Mapping[str, str]) -> None:
    validate_inventory(items, schemas)
    if any(
        item.legacy_rows or item.plaintext_rows or item.unknown_wire_rows for item in items.values()
    ):
        raise CutoverRefused("protected generation requires exact empty inventories")


def classify_legacy(*, has_authenticated_source_proof: bool) -> str:
    """Absence of old citation metadata never implies source-free content."""
    return "source_bound_container" if has_authenticated_source_proof else "legacy_unavailable"


async def cutover(
    port: CutoverPort,
    *,
    image_digest: str,
    schemas: Mapping[str, str],
    journal: SecretStoreJournal,
) -> BackupReceipt:
    _image_digest(image_digest)
    phase, journal_data, _ = await journal.latest()
    if phase is not None and "image_digest" in journal_data:
        if journal_data["image_digest"] != image_digest:
            raise CutoverRefused("cutover journal names a different image digest")
        if journal_data.get("generation") != GENERATION:
            raise CutoverRefused("cutover journal names a different generation")

    def recorded_receipt() -> BackupReceipt:
        try:
            return BackupReceipt(
                backup_id=journal_data["backup_id"],
                manifest_sha256=journal_data["manifest_sha256"],
                kek_fingerprint=journal_data["kek_fingerprint"],
                databases=DATABASES,
                verified=True,
            )
        except KeyError as exc:
            raise CutoverRefused("cutover journal has no verified backup receipt") from exc

    if phase is Phase.COMPLETE:
        return recorded_receipt()

    await port.lock()
    retirement_started = phase is not None and _phase_index(phase) >= _phase_index(Phase.RETIRED)
    try:
        if phase is None or _phase_index(phase) < _phase_index(Phase.WORKER_READY):
            await port.scale_zero()
            if not await port.roots_zero():
                raise CutoverRefused("API and worker must both be zero")
        if phase is None:
            await journal.append(Phase.QUIESCED)
            phase = Phase.QUIESCED
        if phase is Phase.QUIESCED:
            receipt = await port.backup()
            if (
                not receipt.verified
                or receipt.databases != DATABASES
                or not await port.roots_zero()
            ):
                raise CutoverRefused("backup must be verified with both quiesced databases")
            await journal.append(
                Phase.BACKED_UP,
                backup_id=receipt.backup_id,
                manifest_sha256=receipt.manifest_sha256,
                kek_fingerprint=receipt.kek_fingerprint,
            )
            journal_data = {
                **journal_data,
                "backup_id": receipt.backup_id,
                "manifest_sha256": receipt.manifest_sha256,
                "kek_fingerprint": receipt.kek_fingerprint,
            }
            phase = Phase.BACKED_UP
        receipt = recorded_receipt()
        if phase is Phase.BACKED_UP:
            validate_inventory(await port.inventory(), schemas)
            retirement_started = True
            fence = await port.retire(receipt)
            _hash(fence, "retirement fence")
            await journal.append(Phase.RETIRED, fence=fence)
            journal_data = {**journal_data, "fence": fence}
            phase = Phase.RETIRED
        try:
            fence = journal_data["fence"]
        except KeyError as exc:
            raise CutoverRefused("cutover journal has no retirement fence") from exc
        _hash(fence, "retirement fence")
        if phase is Phase.RETIRED:
            await port.migrate(fence)
            await journal.append(Phase.MIGRATED, fence=fence)
            phase = Phase.MIGRATED
        if phase is Phase.MIGRATED:
            if not await port.roots_zero():
                raise CutoverRefused("root restarted before fences")
            final = await port.inventory()
            validate_inventory(final, schemas)
            if any(
                item.legacy_rows or item.plaintext_rows or item.unknown_wire_rows
                for item in final.values()
            ):
                raise CutoverRefused("legacy plaintext or unknown wire remains")
            await journal.append(Phase.SCANNED, inventory_sha256=_inventory_digest(final))
            phase = Phase.SCANNED
        if phase is Phase.SCANNED:
            await port.fence(GENERATION, image_digest)
            await journal.append(Phase.FENCED, generation=GENERATION, image_digest=image_digest)
            phase = Phase.FENCED
        for root, ready_phase in (("ne-worker", Phase.WORKER_READY), ("ne-api", Phase.API_READY)):
            if _phase_index(phase) < _phase_index(ready_phase):
                await port.start(root, GENERATION, image_digest)
                if not await port.ready(root, GENERATION, image_digest):
                    raise CutoverRefused(f"{root} did not attest the protected fence")
                await journal.append(ready_phase, generation=GENERATION, image_digest=image_digest)
                phase = ready_phase
        await journal.append(Phase.COMPLETE, generation=GENERATION, image_digest=image_digest)
        return receipt
    except Exception:
        if retirement_started:
            await port.scale_zero()
        raise
    finally:
        await port.unlock()


def _phase_index(phase: Phase) -> int:
    """Stable phase ordinal used only for resume comparisons."""
    return SecretStoreJournal._ORDER.index(phase)
