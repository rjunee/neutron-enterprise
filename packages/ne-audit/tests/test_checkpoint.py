# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Signed checkpoints: round-trip, WORM, and the tamper-vs-signed-head proof."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from ne.audit import (
    Checkpoint,
    HmacSigner,
    append_record,
    compute_record_hash,
    maybe_checkpoint,
    verify_chain,
    verify_checkpoints,
    write_checkpoint,
)
from ne.audit.checkpoint import repair_unarchived
from ne.audit.store import ne_audit_head, ne_audit_log
from ne.contracts.errors import WormViolationError
from ne.contracts.testing.memory import MemoryObjectStore, MemorySecretStore
from ne.schema import AuditEvent

_FIXED = datetime(2026, 3, 10, tzinfo=UTC)


def _clock() -> datetime:
    return _FIXED


def _seed(engine: sa.Engine, audit: Callable[..., AuditEvent], n: int = 5) -> None:
    with engine.begin() as conn:
        for i in range(n):
            append_record(conn, audit(event_id=f"e{i}"))


def _drop_triggers(conn: sa.Connection) -> None:
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_delete")


def test_signed_checkpoint_round_trip(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    mem_object_store: MemoryObjectStore,
) -> None:
    _seed(engine, audit, n=5)
    signer = HmacSigner(b"k" * 16, key_version=2)
    cp = asyncio.run(write_checkpoint(engine, mem_object_store, signer))
    assert cp.seq == 5
    assert cp.key_version == 2

    stored = asyncio.run(mem_object_store.list_objects("audit/checkpoints/"))
    assert len(stored) == 1
    raw = asyncio.run(mem_object_store.get(stored[0].key))
    fetched = Checkpoint.model_validate_json(raw)
    unsigned = fetched.model_copy(update={"signature": ""})
    assert signer.verify(unsigned.signing_preimage(), fetched.signature)
    # Wrong key fails.
    assert not HmacSigner(b"x" * 16).verify(unsigned.signing_preimage(), fetched.signature)
    # Any field mutation fails.
    mutated = fetched.model_copy(update={"seq": 99, "signature": ""})
    assert not signer.verify(mutated.signing_preimage(), fetched.signature)


def test_worm_and_retention(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit, n=3)
    store = MemoryObjectStore(now=_clock)
    signer = HmacSigner(b"k" * 16)
    asyncio.run(write_checkpoint(engine, store, signer, retention_days=30, now=_clock))
    stats = asyncio.run(store.list_objects("audit/checkpoints/"))
    assert stats[0].worm_retain_until == _FIXED + timedelta(days=30)
    # A second put to the WORM-locked key is refused.
    with pytest.raises(WormViolationError):
        asyncio.run(store.put(stats[0].key, b"overwrite"))


def test_verify_checkpoints_intact(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    mem_object_store: MemoryObjectStore,
) -> None:
    _seed(engine, audit, n=5)
    signer = HmacSigner(b"k" * 16)
    asyncio.run(write_checkpoint(engine, mem_object_store, signer))
    cv = asyncio.run(verify_checkpoints(engine, mem_object_store, signer))
    assert cv.valid
    assert cv.checked == 1


def test_consistent_rewrite_caught_by_checkpoints(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    mem_object_store: MemoryObjectStore,
) -> None:
    _seed(engine, audit, n=5)
    signer = HmacSigner(b"k" * 16)
    asyncio.run(write_checkpoint(engine, mem_object_store, signer))

    # Rewrite the chain from seq 3, recomputing 3..5 consistently AND the head —
    # so verify_chain is FOOLED (the chain is internally consistent) ...
    with engine.begin() as conn:
        _drop_triggers(conn)
        prev = conn.execute(
            sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == 2)
        ).scalar_one()
        for seq in (3, 4, 5):
            row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == seq)).one()
            payload = dict(row.payload)
            if seq == 3:
                payload["tampered"] = True
            new_hash = compute_record_hash(prev, payload)
            conn.execute(
                ne_audit_log.update()
                .where(ne_audit_log.c.seq == seq)
                .values(payload=payload, prev_hash=prev, record_hash=new_hash)
            )
            prev = new_hash
        conn.execute(ne_audit_head.update().where(ne_audit_head.c.id == 1).values(record_hash=prev))

    assert verify_chain(engine).valid  # the rewrite fools the internal chain
    # ... but the signed checkpoint catches it.
    cv = asyncio.run(verify_checkpoints(engine, mem_object_store, signer))
    assert not cv.valid
    assert "rewritten" in cv.detail


def test_truncation_below_checkpoint_caught(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    mem_object_store: MemoryObjectStore,
) -> None:
    _seed(engine, audit, n=5)
    signer = HmacSigner(b"k" * 16)
    asyncio.run(write_checkpoint(engine, mem_object_store, signer))
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq.in_([4, 5])))
        prev = conn.execute(
            sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == 3)
        ).scalar_one()
        conn.execute(
            ne_audit_head.update().where(ne_audit_head.c.id == 1).values(seq=3, record_hash=prev)
        )
    cv = asyncio.run(verify_checkpoints(engine, mem_object_store, signer))
    assert not cv.valid
    assert "truncated" in cv.detail


def test_maybe_checkpoint_threshold(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    mem_object_store: MemoryObjectStore,
) -> None:
    _seed(engine, audit, n=3)
    signer = HmacSigner(b"k" * 16)
    # Below the threshold: no checkpoint written.
    first = asyncio.run(maybe_checkpoint(engine, mem_object_store, signer, every_n_records=5))
    assert first is None
    _seed_more = audit
    with engine.begin() as conn:
        for i in range(3, 6):
            append_record(conn, _seed_more(event_id=f"e{i}"))
    # Now head=6 >= 0 + 5 → writes.
    second = asyncio.run(maybe_checkpoint(engine, mem_object_store, signer, every_n_records=5))
    assert second is not None and second.seq == 6
    # Idempotent between thresholds: 6 < 6 + 5 → None.
    third = asyncio.run(maybe_checkpoint(engine, mem_object_store, signer, every_n_records=5))
    assert third is None


def test_verify_checkpoints_malformed_object_fails_closed(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    mem_object_store: MemoryObjectStore,
) -> None:
    _seed(engine, audit, n=5)
    signer = HmacSigner(b"k" * 16)
    asyncio.run(write_checkpoint(engine, mem_object_store, signer))
    # A corrupt object under the same prefix must not crash verification.
    asyncio.run(
        mem_object_store.put(
            "audit/checkpoints/garbage.json", b"not json", content_type="application/json"
        )
    )
    cv = asyncio.run(verify_checkpoints(engine, mem_object_store, signer))
    assert not cv.valid
    assert "garbage.json" in cv.detail
    assert "malformed" in cv.detail


def test_verify_checkpoints_detects_head_rewind(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    mem_object_store: MemoryObjectStore,
) -> None:
    _seed(engine, audit, n=6)
    signer = HmacSigner(b"k" * 16)
    cp = asyncio.run(write_checkpoint(engine, mem_object_store, signer))
    assert cp.seq == 6
    # Rewind the head below the signed checkpoint, leaving the signed rows intact.
    with engine.begin() as conn:
        row3 = conn.execute(
            sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == 3)
        ).scalar_one()
        conn.execute(
            ne_audit_head.update().where(ne_audit_head.c.id == 1).values(seq=3, record_hash=row3)
        )
    cv = asyncio.run(verify_checkpoints(engine, mem_object_store, signer))
    assert not cv.valid
    assert "head rewound" in cv.detail


def test_verify_checkpoints_forged_document_signature_invalid(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    mem_object_store: MemoryObjectStore,
) -> None:
    _seed(engine, audit, n=5)
    signer = HmacSigner(b"k" * 16)
    # An attacker plants a well-formed checkpoint document WITHOUT the signing key.
    forged = Checkpoint(
        seq=5,
        record_hash="d" * 64,
        created_at=_FIXED,
        key_path="ne/audit/checkpoint-signing",
        key_version=1,
        signature="0" * 64,
    )
    asyncio.run(
        mem_object_store.put(
            "audit/checkpoints/" + f"{5:020d}.json",
            forged.model_dump_json().encode("utf-8"),
            content_type="application/json",
        )
    )
    cv = asyncio.run(verify_checkpoints(engine, mem_object_store, signer))
    assert not cv.valid
    assert "signature invalid" in cv.detail


def test_verify_checkpoints_zero_checkpoints_reports_no_anchor(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    mem_object_store: MemoryObjectStore,
) -> None:
    _seed(engine, audit, n=5)
    signer = HmacSigner(b"k" * 16)
    cv = asyncio.run(verify_checkpoints(engine, mem_object_store, signer))
    assert cv.valid
    assert cv.checked == 0
    assert "no anchor" in cv.detail


_KEY_PATH = "ne/audit/checkpoint-signing"


def _seed_more(engine: sa.Engine, audit: Callable[..., AuditEvent], start: int, n: int) -> None:
    with engine.begin() as conn:
        for i in range(start, start + n):
            append_record(conn, audit(event_id=f"e{i}"))


def test_verify_checkpoints_survives_key_rotation(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    mem_object_store: MemoryObjectStore,
    mem_secret_store: MemorySecretStore,
) -> None:
    # Sign checkpoint #1 under key v1 ...
    asyncio.run(mem_secret_store.write(_KEY_PATH, {"key": "11" * 16}))
    signer_v1 = asyncio.run(HmacSigner.from_secret_store(mem_secret_store, _KEY_PATH))
    assert signer_v1.key_version == 1
    _seed(engine, audit, n=3)
    asyncio.run(write_checkpoint(engine, mem_object_store, signer_v1))

    # ... ROTATE the signing key to v2, sign checkpoint #2 under it.
    asyncio.run(mem_secret_store.write(_KEY_PATH, {"key": "22" * 16}))
    signer_v2 = asyncio.run(HmacSigner.from_secret_store(mem_secret_store, _KEY_PATH))
    assert signer_v2.key_version == 2
    _seed_more(engine, audit, start=3, n=3)
    asyncio.run(write_checkpoint(engine, mem_object_store, signer_v2))

    # SecretStore mode resolves the key PER checkpoint version → both verify.
    cv = asyncio.run(verify_checkpoints(engine, mem_object_store, mem_secret_store))
    assert cv.valid
    assert cv.checked == 2

    # Legacy single-signer mode is exactly what P2-10 breaks: verifying every
    # checkpoint with the latest (v2) signer flags the pre-rotation v1 checkpoint.
    legacy = asyncio.run(verify_checkpoints(engine, mem_object_store, signer_v2))
    assert not legacy.valid
    assert "signature invalid at seq 3" in legacy.detail


def test_verify_checkpoints_missing_key_version_fails_closed(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    mem_object_store: MemoryObjectStore,
    mem_secret_store: MemorySecretStore,
) -> None:
    asyncio.run(mem_secret_store.write(_KEY_PATH, {"key": "11" * 16}))
    signer_v1 = asyncio.run(HmacSigner.from_secret_store(mem_secret_store, _KEY_PATH))
    _seed(engine, audit, n=3)
    asyncio.run(write_checkpoint(engine, mem_object_store, signer_v1))

    # Destroy the version the checkpoint was signed under → verification cannot
    # resolve a key. It must fail CLOSED with a named detail (no traceback).
    asyncio.run(mem_secret_store.destroy(_KEY_PATH, [1]))
    cv = asyncio.run(verify_checkpoints(engine, mem_object_store, mem_secret_store))
    assert not cv.valid
    assert _KEY_PATH in cv.detail
    assert "v1" in cv.detail
    assert "seq 3" in cv.detail


def test_signer_from_secret_store(
    mem_secret_store: MemorySecretStore,
) -> None:
    key_hex = "ab" * 16
    asyncio.run(mem_secret_store.write("ne/audit/checkpoint-signing", {"key": key_hex}))
    signer = asyncio.run(
        HmacSigner.from_secret_store(mem_secret_store, "ne/audit/checkpoint-signing")
    )
    assert signer.key_version == 1
    # Signs identically to a raw signer built from the same bytes.
    raw = HmacSigner(bytes.fromhex(key_hex))
    assert signer.sign(b"hello") == raw.sign(b"hello")


# -- P2-2: repair failure must DEGRADE, not BLOCK -----------------------------
# repair_unarchived runs FIRST inside maybe_checkpoint. Before this fix a single
# object the store would never archive (per-key ACL/retention-mode misconfig, a
# legal hold in a different mode) made repair raise BEFORE _last_archived_seq and
# BEFORE write_checkpoint — so EVERY later tick raised identically and the audit
# chain grew with ZERO new tamper anchors, indefinitely.
#
# The failure direction is now inverted: a new checkpoint at a higher seq is
# strictly MORE protection than none. Continuing loses no guarantee, because
# _last_archived_seq already refuses to count an un-WORM'd orphan as progress and
# verify_checkpoints already reports it red — the orphan stays visible either way.


class _PoisonArchiveStore(MemoryObjectStore):
    """MemoryObjectStore where ``archive`` permanently fails for chosen keys.

    Models the real-world shape this guards: not a transient outage (already
    covered by the P1-A recovery test) but a PERMANENT per-key defect that no
    number of retries will clear.
    """

    def __init__(self, poison: set[str], **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.poison = poison
        self.archive_attempts: list[str] = []

    async def archive(self, key: str, *, retain_until: datetime):  # type: ignore[no-untyped-def]
        self.archive_attempts.append(key)
        if key in self.poison:
            raise PermissionError(f"access denied archiving '{key}' (object lock not configured)")
        return await super().archive(key, retain_until=retain_until)


def _orphan(store: MemoryObjectStore, seq: int) -> str:
    """Put a checkpoint-shaped object at ``seq`` WITHOUT archiving it."""
    key = f"audit/checkpoints/{seq:020d}.json"
    asyncio.run(store.put(key, b"{}", content_type="application/json"))
    return key


def test_poison_orphan_does_not_block_new_checkpoints(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
) -> None:
    """(a) An un-archivable object must NOT prevent a NEW checkpoint at a higher seq."""
    poison_key = "audit/checkpoints/" + f"{1:020d}.json"
    store = _PoisonArchiveStore({poison_key}, now=_clock)
    _orphan(store, 1)
    _seed(engine, audit, n=5)
    signer = HmacSigner(b"k" * 16)

    checkpoint = asyncio.run(maybe_checkpoint(engine, store, signer, every_n_records=5))

    # Pre-fix this raised PermissionError out of maybe_checkpoint and returned nothing.
    assert checkpoint is not None, "repair failure blocked the new checkpoint entirely"
    assert checkpoint.seq == 5
    stats = {s.key: s for s in asyncio.run(store.list_objects("audit/checkpoints/"))}
    assert stats[f"audit/checkpoints/{checkpoint.seq:020d}.json"].worm_retain_until is not None, (
        "the NEW checkpoint must still be WORM-archived"
    )
    # The poison orphan stays visible as un-WORM'd — degraded, not hidden.
    assert stats[poison_key].worm_retain_until is None


def test_one_poison_orphan_does_not_block_repair_of_others(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
) -> None:
    """(b) Each orphan is attempted INDEPENDENTLY.

    list_objects returns sorted keys, so the poison object at the LOWEST seq used
    to abort the loop before the healthy orphans at higher seqs were ever reached.
    """
    poison_key = "audit/checkpoints/" + f"{1:020d}.json"
    store = _PoisonArchiveStore({poison_key}, now=_clock)
    _orphan(store, 1)
    healthy_a = _orphan(store, 2)
    healthy_b = _orphan(store, 3)

    repaired = asyncio.run(
        repair_unarchived(store, "audit/checkpoints/", retain_until=_FIXED + timedelta(days=365))
    )

    assert set(repaired) == {healthy_a, healthy_b}, (
        "a failure on the first orphan aborted repair of every later one"
    )
    stats = {s.key: s for s in asyncio.run(store.list_objects("audit/checkpoints/"))}
    assert stats[healthy_a].worm_retain_until is not None
    assert stats[healthy_b].worm_retain_until is not None
    assert stats[poison_key].worm_retain_until is None
    assert poison_key in store.archive_attempts


def test_repair_failure_is_logged_with_actionable_detail(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(c) Degrading silently would be worse than blocking — the failure must be LOUD."""
    poison_key = "audit/checkpoints/" + f"{1:020d}.json"
    store = _PoisonArchiveStore({poison_key}, now=_clock)
    _orphan(store, 1)
    _seed(engine, audit, n=5)
    signer = HmacSigner(b"k" * 16)

    with caplog.at_level("WARNING", logger="ne.audit.checkpoint"):
        asyncio.run(maybe_checkpoint(engine, store, signer, every_n_records=5))

    records = [r for r in caplog.records if r.name == "ne.audit.checkpoint"]
    assert records, "repair failure was swallowed with no log at all"
    message = "\n".join(r.getMessage() for r in records)
    assert poison_key in message, "log must name the key that could not be archived"
    assert "PermissionError" in message, "log must name the exception type"
    assert "object lock not configured" in message, "log must carry the driver's detail"
