# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-AUDIT-CRON — the periodic signed-WORM audit-checkpoint loop.

THE verification-bar test is :func:`test_loop_fires_and_writes_signed_archived_checkpoint`:
it proves the RUNNING loop task itself invokes ``maybe_checkpoint`` and produces a
checkpoint that is present in the object store, HMAC-signature-valid, at the live
chain head, and WORM-archived — not merely that a job row exists somewhere. Its
signature assertion computes the expected digest with stdlib ``hmac`` DIRECTLY
rather than round-tripping through ``HmacSigner.verify`` (which recomputes via
``HmacSigner.sign``, so both sides of the comparison move together and no signing
defect is detectable). The rest cover threshold idempotency across fires,
fail-closed-and-recoverable behavior on a missing signing key, the benign
multi-replica WORM race, recovery from a FAILED ``archive`` (the checkpoint must
not be left permanently non-WORM, and the loop must not wedge on it), the
actionable/redacted generic-error branch, and the builder's default-safe /
boot-loud / seam-wiring contracts.

No pytest-asyncio (repo policy): async is driven via ``asyncio.run`` (mirroring the
``run`` helper in ``conftest.py``).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from ne.audit import append_record, ensure_audit_schema
from ne.audit.checkpoint import Checkpoint, HmacSigner, verify_checkpoints
from ne.audit.config import CheckpointConfig
from ne.audit.store import read_head
from ne.contracts.errors import PluginNotFoundError, WormViolationError
from ne.contracts.storage import ObjectStat
from ne.contracts.testing.memory import MemoryObjectStore, MemorySecretStore
from ne.schema import AuditEvent, AuditOutcome
from ne.worker.audit_checkpoint import (
    AuditCheckpointLoop,
    build_audit_checkpoint_loop,
    checkpoint_once,
    run_audit_checkpoint_loop,
)
from ne.worker.settings import WorkerSettings

from .conftest import build_sqlite_platform, run

_SIGNING_KEY_HEX = "ab" * 32
_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _audit(action: str = "tool.invoke", event_id: str | None = None) -> AuditEvent:
    return AuditEvent(
        event_id=event_id if event_id is not None else str(uuid.uuid4()),
        event_type="audit",
        config_hash="a" * 64,
        actor="sys",
        provenance={},
        occurred_at=_TS,
        action=action,
        resource="urn:tool:search",
        outcome=AuditOutcome.success,
    )


def _engine() -> sa.Engine:
    eng = sa.create_engine("sqlite://")
    ensure_audit_schema(eng)
    return eng


def _seed(engine: sa.Engine, n: int) -> None:
    with engine.begin() as conn:
        for i in range(n):
            append_record(conn, _audit(event_id=f"e{i}-{uuid.uuid4()}"))


async def _seeded_key_store(key_hex: str = _SIGNING_KEY_HEX) -> MemorySecretStore:
    store = MemorySecretStore()
    await store.write("ne/audit/checkpoint-signing", {"key": key_hex})
    return store


@dataclass
class _WormOnPutObjectStore:
    """Delegates to a real :class:`MemoryObjectStore`, but ``put`` ALWAYS raises
    :class:`WormViolationError` — simulating a peer replica that already archived
    the exact same WORM key an instant earlier (the benign convergent race)."""

    inner: MemoryObjectStore

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: Any = None,
    ) -> ObjectStat:
        raise WormViolationError(f"peer replica already archived key '{key}'")

    async def get(self, key: str) -> bytes:
        return await self.inner.get(key)

    async def stat(self, key: str) -> ObjectStat:
        return await self.inner.stat(key)

    async def delete(self, key: str) -> None:
        await self.inner.delete(key)

    async def list_objects(self, prefix: str = "") -> Any:
        return await self.inner.list_objects(prefix)

    async def archive(self, key: str, *, retain_until: datetime) -> Any:
        return await self.inner.archive(key, retain_until=retain_until)


async def _poll_until_archived(
    object_store: MemoryObjectStore, prefix: str, *, deadline_s: float = 5.0
) -> list[ObjectStat]:
    """Poll ``list_objects`` until a checkpoint is present AND WORM-archived (i.e.
    both ``put`` and ``archive`` completed — proves the FULL write, not a partial
    one caught mid-flight by cooperative interleaving)."""
    deadline = time.monotonic() + deadline_s
    stats: list[ObjectStat] = []
    while time.monotonic() < deadline:
        stats = list(await object_store.list_objects(prefix))
        if stats and all(s.worm_retain_until is not None for s in stats):
            return stats
        await asyncio.sleep(0.02)
    return stats


# --- (1) THE verification-bar test -------------------------------------------


def test_loop_fires_and_writes_signed_archived_checkpoint() -> None:
    engine = _engine()
    _seed(engine, n=3)
    secret_store = run(_seeded_key_store())
    object_store = MemoryObjectStore()
    config = CheckpointConfig(every_n_records=1)
    loop = AuditCheckpointLoop(
        engine=engine,
        secret_store=secret_store,
        object_store=object_store,
        config=config,
        interval_s=0.01,
    )

    async def _drive() -> list[ObjectStat]:
        stop = asyncio.Event()
        task = asyncio.create_task(run_audit_checkpoint_loop(loop, stop))
        stats = await _poll_until_archived(object_store, config.prefix)
        stop.set()
        await task
        return stats

    stats = run(_drive())
    assert stats, "the running loop task never wrote a checkpoint within the deadline"

    raw = run(object_store.get(stats[0].key))
    cp = Checkpoint.model_validate_json(raw)

    # Compute the expected digest with stdlib ``hmac`` DIRECTLY — do NOT round-trip
    # through HmacSigner.verify. ``verify`` recomputes via ``HmacSigner.sign``, so
    # both sides of that comparison move together: mutating ``sign`` to return a
    # constant, or to key off the wrong secret, left the old assertion passing. An
    # independent oracle is the only thing that can detect a signing defect.
    unsigned = cp.model_copy(update={"signature": ""})
    expected = hmac.new(
        bytes.fromhex(_SIGNING_KEY_HEX), unsigned.signing_preimage(), "sha256"
    ).hexdigest()
    assert cp.signature == expected, "signature is not HMAC-SHA256 over the preimage"

    with engine.connect() as conn:
        head_seq, _ = read_head(conn)
    assert cp.seq == head_seq

    assert stats[0].worm_retain_until is not None, "checkpoint was never WORM-archived"


# --- (2) threshold idempotency across repeated fires --------------------------


def test_repeated_fires_below_threshold_write_once() -> None:
    engine = _engine()
    _seed(engine, n=1)
    secret_store = run(_seeded_key_store())
    object_store = MemoryObjectStore()
    config = CheckpointConfig(every_n_records=1)
    loop = AuditCheckpointLoop(
        engine=engine,
        secret_store=secret_store,
        object_store=object_store,
        config=config,
        interval_s=999.0,
    )

    first = run(checkpoint_once(loop))
    assert first is not None
    with engine.connect() as conn:
        head_seq, _ = read_head(conn)
    assert first.seq == head_seq

    # A second fire (e.g. a worker restart) with the head unchanged writes nothing.
    second = run(checkpoint_once(loop))
    assert second is None

    stats = run(object_store.list_objects(config.prefix))
    assert len(stats) == 1


# --- (3) missing signing key: fail closed, visible, recoverable ---------------


def test_missing_signing_key_fails_closed_then_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = _engine()
    _seed(engine, n=1)
    secret_store = MemorySecretStore()  # EMPTY — no signing key.
    object_store = MemoryObjectStore()
    config = CheckpointConfig(every_n_records=1)
    loop = AuditCheckpointLoop(
        engine=engine,
        secret_store=secret_store,
        object_store=object_store,
        config=config,
        interval_s=0.01,
    )

    async def _drive() -> tuple[list[ObjectStat], list[ObjectStat]]:
        stop = asyncio.Event()
        with caplog.at_level(logging.WARNING, logger="ne.worker.audit_checkpoint"):
            task = asyncio.create_task(run_audit_checkpoint_loop(loop, stop))
            # Give the loop several ticks (interval 0.01s) to prove it survives.
            await asyncio.sleep(0.1)
            assert not task.done(), "the worker must survive a missing signing key"
            before = list(await object_store.list_objects(config.prefix))

            # Recover: write the key, then the NEXT tick should checkpoint.
            await secret_store.write(config.key_path, {"key": _SIGNING_KEY_HEX})
            after = await _poll_until_archived(object_store, config.prefix)

            stop.set()
            await task
        return before, after

    before, after = run(_drive())
    assert before == [], "no checkpoint may be written while the signing key is absent"
    assert any("ne/audit/checkpoint-signing" in rec.message for rec in caplog.records), (
        "the throttled warning must name the missing key path"
    )
    assert after, "the loop must recover and checkpoint once the key appears"


# --- (4) benign multi-replica WORM race ----------------------------------------


def test_worm_race_is_benign_skip(caplog: pytest.LogCaptureFixture) -> None:
    engine = _engine()
    _seed(engine, n=1)
    secret_store = run(_seeded_key_store())
    object_store = _WormOnPutObjectStore(MemoryObjectStore())
    config = CheckpointConfig(every_n_records=1)
    loop = AuditCheckpointLoop(
        engine=engine,
        secret_store=secret_store,
        object_store=object_store,
        config=config,
        interval_s=0.01,
    )

    async def _drive() -> None:
        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="ne.worker.audit_checkpoint"):
            task = asyncio.create_task(run_audit_checkpoint_loop(loop, stop))
            await asyncio.sleep(0.1)
            assert not task.done(), "a peer-replica WORM race must not crash the loop"
            stop.set()
            await task

    run(_drive())
    assert any("benign convergent race" in rec.message for rec in caplog.records)


# --- (5) P1-A: a transient archive() failure must NOT permanently orphan a
#         non-WORM checkpoint, and must not wedge the loop forever ---------------


@dataclass
class _FlakyArchiveObjectStore:
    """A real :class:`MemoryObjectStore` whose ``archive`` fails the FIRST N times.

    Reproduces the reported scenario exactly: the object store accepts the ``put``
    but rejects the ``archive`` with a TRANSIENT error, then fully recovers. Before
    the fix the ``put`` object survived with ``worm_retain_until = None``, counted
    toward ``last_seq``, and the threshold check then returned ``None`` on every
    later tick — so the seq was never retried even though the store was healthy.
    """

    inner: MemoryObjectStore
    fail_first: int = 1
    archive_calls: int = 0

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: Any = None,
    ) -> ObjectStat:
        return await self.inner.put(key, data, content_type=content_type, metadata=metadata)

    async def get(self, key: str) -> bytes:
        return await self.inner.get(key)

    async def stat(self, key: str) -> ObjectStat:
        return await self.inner.stat(key)

    async def delete(self, key: str) -> None:
        await self.inner.delete(key)

    async def list_objects(self, prefix: str = "") -> Any:
        return await self.inner.list_objects(prefix)

    async def archive(self, key: str, *, retain_until: datetime) -> Any:
        self.archive_calls += 1
        if self.archive_calls <= self.fail_first:
            raise ConnectionError(f"transient archive outage for key '{key}'")
        return await self.inner.archive(key, retain_until=retain_until)


def test_failed_archive_is_repaired_and_loop_recovers() -> None:
    engine = _engine()
    _seed(engine, n=3)
    secret_store = run(_seeded_key_store())
    inner = MemoryObjectStore()
    object_store = _FlakyArchiveObjectStore(inner, fail_first=1)
    config = CheckpointConfig(every_n_records=1)
    loop = AuditCheckpointLoop(
        engine=engine,
        secret_store=secret_store,
        object_store=object_store,
        config=config,
        interval_s=0.01,
    )

    # Tick 1: put succeeds, archive raises -> a non-WORM orphan is left behind.
    with pytest.raises(ConnectionError):
        run(checkpoint_once(loop))
    orphans = run(inner.list_objects(config.prefix))
    assert len(orphans) == 1
    assert orphans[0].worm_retain_until is None, "precondition: the orphan is not WORM"

    # An operator running `ne audit verify` MUST NOT be told this is fine.
    signer = HmacSigner(bytes.fromhex(_SIGNING_KEY_HEX))
    unarchived_result = run(verify_checkpoints(engine, inner, signer, prefix=config.prefix))
    assert not unarchived_result.valid, "verify must not report a non-WORM checkpoint as valid"
    assert "NOT under WORM retention" in unarchived_result.detail

    # The store has now fully recovered. The RUNNING loop must repair the orphan.
    async def _drive() -> list[ObjectStat]:
        stop = asyncio.Event()
        task = asyncio.create_task(run_audit_checkpoint_loop(loop, stop))
        stats = await _poll_until_archived(inner, config.prefix)
        stop.set()
        await task
        return stats

    stats = run(_drive())
    assert stats, "the loop produced no checkpoint after the store recovered"
    for stat in stats:
        assert stat.worm_retain_until is not None, (
            f"{stat.key} is still not WORM-archived — a failed archive() left a "
            "permanently deletable checkpoint and the loop never retried it"
        )

    # And verification is green again.
    repaired = run(verify_checkpoints(engine, inner, signer, prefix=config.prefix))
    assert repaired.valid, repaired.detail
    assert repaired.checked >= 1


# --- (6) P2-E: an unexpected error is throttled AND actionable ------------------


@dataclass
class _BrokenObjectStore:
    """Every ``list_objects`` fails with a PERMANENT, diagnosable error."""

    async def put(self, key: str, data: bytes, **kwargs: Any) -> Any:
        raise AssertionError("unreachable")

    async def get(self, key: str) -> bytes:
        raise AssertionError("unreachable")

    async def stat(self, key: str) -> Any:
        raise AssertionError("unreachable")

    async def delete(self, key: str) -> None:
        raise AssertionError("unreachable")

    async def list_objects(self, prefix: str = "") -> Any:
        raise RuntimeError("NoSuchBucket: bucket 'ne-audit-typo' at https://k:s3cret@s3.local")

    async def archive(self, key: str, *, retain_until: datetime) -> Any:
        raise AssertionError("unreachable")


def test_generic_error_is_actionable_and_redacted(caplog: pytest.LogCaptureFixture) -> None:
    engine = _engine()
    _seed(engine, n=1)
    secret_store = run(_seeded_key_store())
    config = CheckpointConfig(every_n_records=1)
    loop = AuditCheckpointLoop(
        engine=engine,
        secret_store=secret_store,
        object_store=_BrokenObjectStore(),  # type: ignore[arg-type]
        config=config,
        interval_s=0.01,
    )

    async def _drive() -> None:
        stop = asyncio.Event()
        with caplog.at_level(logging.WARNING, logger="ne.worker.audit_checkpoint"):
            task = asyncio.create_task(run_audit_checkpoint_loop(loop, stop))
            await asyncio.sleep(0.1)
            assert not task.done(), "a broken object store must not crash the worker"
            stop.set()
            await task

    run(_drive())
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, "the 60s throttle must collapse repeated failures to one line"
    message = warnings[0].getMessage()
    # Actionable: the type ALONE ("RuntimeError") is not enough to tell a permanent
    # misconfiguration apart from a transient outage.
    assert "RuntimeError" in message
    assert "NoSuchBucket" in message
    assert "ne-audit-typo" in message
    assert config.prefix in message
    assert config.key_path in message
    # ...but an embedded credential in that message must never reach the log.
    assert "s3cret" not in message
    assert "***" in message
    # A full traceback accompanies the FIRST occurrence only.
    assert warnings[0].exc_info is not None


# --- (7)-(9) the builder: default-safe / boot-loud / seam-wiring --------------


def test_build_default_off_returns_none(tmp_path: Path) -> None:
    platform = build_sqlite_platform(tmp_path)
    loop = build_audit_checkpoint_loop(platform, WorkerSettings())  # type: ignore[call-arg]
    assert loop is None


def test_build_unknown_plugin_fails_boot(tmp_path: Path) -> None:
    platform = build_sqlite_platform(tmp_path)
    settings = WorkerSettings(audit_checkpoint_object_store="nope")  # type: ignore[call-arg]
    with pytest.raises(PluginNotFoundError):
        build_audit_checkpoint_loop(platform, settings)


def test_build_memory_plugin_wires_platform_seams(tmp_path: Path) -> None:
    platform = build_sqlite_platform(tmp_path)
    settings = WorkerSettings(  # type: ignore[call-arg]
        audit_checkpoint_object_store="memory",
        audit_checkpoint_interval_s=1.0,
    )
    loop = build_audit_checkpoint_loop(platform, settings)
    assert loop is not None
    assert isinstance(loop.object_store, MemoryObjectStore)
    assert loop.engine is platform.audit_engine
    assert loop.secret_store is platform.secret_store
    assert loop.config is platform.audit_settings.checkpoint
    assert loop.interval_s == 1.0
