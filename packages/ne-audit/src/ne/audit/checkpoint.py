# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Periodic signed checkpoints archived to WORM — the tamper-evidence anchor.

The hash chain proves internal consistency, but a sufficiently privileged
attacker who can rewrite the whole ``ne_audit_log`` table can also recompute a
consistent chain from any point. Signed checkpoints close that gap: we
periodically SIGN the chain head (``seq`` + ``record_hash``) with a key held in
the :class:`~ne.contracts.secrets.SecretStore`, then archive the signed document
to WORM through the frozen :class:`~ne.contracts.storage.ObjectStore` (put +
``archive`` with a ``retain_until``). Even a fully-rewritten DB cannot forge a
checkpoint without the signing key, and the WORM retention means the archived
checkpoints cannot be deleted — so :func:`verify_checkpoints` catches a rewrite.

ARCHIVED-OR-NOTHING. A checkpoint is written as ``put`` then ``archive``; if the
``archive`` half fails, the object exists but is NOT under WORM retention, so it
is deletable and overwritable by precisely the privileged attacker this control
exists to catch. That state is never accepted as done: the write path counts only
ARCHIVED objects toward the threshold (:func:`_last_archived_seq`) so the loop
returns to that seq instead of stalling on a non-WORM orphan, re-archives any
orphan it finds (:func:`repair_unarchived`), and :func:`verify_checkpoints`
reports an unarchived checkpoint as INVALID rather than green.

Signing is HMAC-SHA256 (stdlib ``hmac``) — zero third-party crypto deps by
design. The ``algorithm`` field on the checkpoint document keeps an Ed25519
upgrade path open without a format change.

The periodic scheduler loop lives in ne-worker (``ne.worker.audit_checkpoint``,
NE-AUDIT-CRON, default-off via ``NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE``); this
module ships the primitive (:func:`write_checkpoint` / :func:`maybe_checkpoint`)
and the ``ne audit checkpoint`` CLI triggers it on demand.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, ValidationError

from ne.audit.store import canonical_bytes, ne_audit_head, ne_audit_log, read_head
from ne.contracts.errors import SecretNotFoundError
from ne.contracts.secrets import SecretStore
from ne.contracts.storage import ObjectStat, ObjectStore
from ne.net.ssrf import redact_url_for_logs

#: The default SecretStore path holding the checkpoint signing key.
DEFAULT_KEY_PATH = "ne/audit/checkpoint-signing"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Checkpoint(BaseModel):
    """A signed pointer at the chain head, archived to WORM."""

    model_config = ConfigDict(frozen=True)

    version: int = 1
    algorithm: str = "hmac-sha256"
    seq: int
    record_hash: str
    created_at: datetime
    key_path: str
    key_version: int
    signature: str

    def signing_preimage(self) -> bytes:
        """Canonical bytes over every field except the signature."""
        return canonical_bytes(self.model_dump(mode="json", exclude={"signature"}))


class HmacSigner:
    """HMAC-SHA256 signer over the checkpoint preimage.

    Constructed EITHER from raw key bytes (the clearly-stubbed CLI/test seam) or,
    in production, from a :class:`~ne.contracts.secrets.SecretStore` via
    :meth:`from_secret_store`, which reads a hex-encoded ``key`` and the secret's
    version.
    """

    def __init__(self, key: bytes, *, key_version: int = 1) -> None:
        self._key = key
        self.key_version = key_version

    @classmethod
    async def from_secret_store(
        cls, store: SecretStore, path: str = DEFAULT_KEY_PATH
    ) -> HmacSigner:
        secret = await store.read(path)
        key_hex = secret.data["key"]
        return cls(bytes.fromhex(key_hex), key_version=secret.version)

    def sign(self, preimage: bytes) -> str:
        return hmac.new(self._key, preimage, "sha256").hexdigest()

    def verify(self, preimage: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign(preimage), signature)


@dataclass(frozen=True)
class CheckpointVerification:
    """Result of verifying stored checkpoints against the live chain."""

    valid: bool
    checked: int
    detail: str


_LOG = logging.getLogger("ne.audit.checkpoint")


def _checkpoint_key(prefix: str, seq: int) -> str:
    return f"{prefix}{seq:020d}.json"


def _seq_from_key(prefix: str, key: str) -> int | None:
    """Recover the ``seq`` :func:`_checkpoint_key` encoded, or ``None``.

    ``_checkpoint_key`` zero-pads ``seq`` to a fixed width, so the seq is
    recoverable from the KEY ALONE — no ``get()`` of the object body required
    (P3-G). Returns ``None`` for any key that is not one of ours, so a foreign
    object sharing the prefix is ignored rather than crashing the loop.
    """
    if not key.startswith(prefix) or not key.endswith(".json"):
        return None
    stem = key[len(prefix) : -len(".json")]
    return int(stem) if stem.isdigit() else None


async def repair_unarchived(
    object_store: ObjectStore,
    prefix: str,
    *,
    retain_until: datetime,
    skip_key: str | None = None,
) -> list[str]:
    """Re-``archive`` any stored checkpoint that is NOT under WORM retention.

    A checkpoint is written as ``put`` THEN ``archive``. If the ``archive`` half
    fails (transient outage, throttling, credential blip) the object is left in
    the store WITHOUT WORM retention — deletable and overwritable by exactly the
    privileged attacker this control exists to catch. Nothing else in the system
    ever revisits that object, so the gap is PERMANENT.

    This is the repair half of that fix: on every tick, any stored checkpoint
    missing ``worm_retain_until`` is re-archived. It pairs with
    :func:`_last_archived_seq`, which refuses to count an unarchived object as
    progress — so the loop keeps returning to the seq until it is really WORM.
    Returns the keys repaired (for logging/tests).

    DEGRADE, DO NOT BLOCK (P2-2). Every object is attempted INDEPENDENTLY inside
    its own ``try``; a failure is logged and the sweep continues. Two distinct
    bugs are fixed by that:

    * **Repair failure used to block all future checkpoints.** This function runs
      FIRST in :func:`maybe_checkpoint`, before ``_last_archived_seq`` and before
      ``write_checkpoint``. One object the store will never archive — a per-key
      ACL or retention-mode misconfig, a legal hold applied in a different mode —
      made this raise on EVERY tick forever, so the audit chain kept growing with
      ZERO new tamper anchors. That is the wrong failure direction: a new
      checkpoint at a higher seq is strictly MORE protection than none. Letting
      the caller proceed costs no guarantee, because :func:`_last_archived_seq`
      still refuses to count the orphan as progress and :func:`verify_checkpoints`
      still reports it red — the defect stays just as visible, and the chain gains
      an anchor it would otherwise never get.
    * **One failure aborted repair of every LATER orphan.** ``list_objects``
      returns sorted keys, so a poison object at a low seq shadowed healthy
      orphans at higher seqs that would have repaired fine.

    Silent degradation would be worse than blocking, so each failure is logged at
    WARNING naming the key, the exception type and the driver's message. The
    message is passed through :func:`ne.net.ssrf.redact_url_for_logs` (the repo's
    redaction discipline, same as the worker loop) so an endpoint URL embedded in
    a storage driver's error text cannot leak userinfo into the log.
    """
    repaired: list[str] = []
    for stat in await object_store.list_objects(prefix):
        if stat.key == skip_key or _seq_from_key(prefix, stat.key) is None:
            continue
        if stat.worm_retain_until is None:
            try:
                await object_store.archive(stat.key, retain_until=retain_until)
            except Exception as exc:
                _LOG.warning(
                    "audit checkpoint orphan repair FAILED for key=%s: %s: %s — this object is "
                    "NOT under WORM retention and is deletable/overwritable; checkpointing "
                    "continues (a new checkpoint is strictly more protection than none) and "
                    "verify_checkpoints will keep reporting this object invalid until it is "
                    "archived or removed by an operator",
                    stat.key,
                    type(exc).__name__,
                    redact_url_for_logs(str(exc)),
                )
                continue
            repaired.append(stat.key)
    return repaired


async def _last_archived_seq(object_store: ObjectStore, prefix: str) -> int:
    """Highest seq among stored checkpoints that are ACTUALLY under WORM retention.

    Two deliberate properties:

    * **Archived-only.** An object that was ``put`` but never ``archive``d does
      NOT count as progress, so :func:`maybe_checkpoint`'s threshold keeps
      returning to that seq instead of treating a non-WORM orphan as a
      satisfied checkpoint forever.
    * **O(1) reads (P3-G).** ``list_objects`` already returns
      :class:`~ne.contracts.storage.ObjectStat` (key + ``worm_retain_until``),
      and :func:`_checkpoint_key` zero-pads the seq into the key — so BOTH
      inputs come from the single list call. No per-object ``get()``; at a 900s
      tick with 365-day retention the old full-download-per-tick grew without
      bound.
    """
    return max(
        (
            seq
            for stat in await object_store.list_objects(prefix)
            if stat.worm_retain_until is not None
            and (seq := _seq_from_key(prefix, stat.key)) is not None
        ),
        default=0,
    )


async def write_checkpoint(
    engine: sa.Engine,
    object_store: ObjectStore,
    signer: HmacSigner,
    *,
    key_path: str = DEFAULT_KEY_PATH,
    retention_days: int = 365,
    prefix: str = "audit/checkpoints/",
    now: Callable[[], datetime] = utcnow,
) -> Checkpoint:
    """Sign the current chain head and archive the signed checkpoint to WORM."""
    with engine.connect() as conn:
        seq, record_hash = read_head(conn)

    unsigned = Checkpoint(
        seq=seq,
        record_hash=record_hash,
        created_at=now(),
        key_path=key_path,
        key_version=signer.key_version,
        signature="",
    )
    signature = signer.sign(unsigned.signing_preimage())
    checkpoint = unsigned.model_copy(update={"signature": signature})

    key = _checkpoint_key(prefix, seq)
    retain_until = now() + timedelta(days=retention_days)

    # Repair FIRST: any earlier checkpoint left un-WORM'd by a failed archive is
    # re-archived before we add another object. ``skip_key`` excludes the key we
    # are about to (re)write — archiving it here would WORM-lock it and make our
    # own ``put`` below raise WormViolationError.
    await repair_unarchived(object_store, prefix, retain_until=retain_until, skip_key=key)

    body = canonical_bytes(checkpoint.model_dump(mode="json"))
    await object_store.put(key, body, content_type="application/json")
    await object_store.archive(key, retain_until=retain_until)
    return checkpoint


async def _stored_checkpoints(
    object_store: ObjectStore, prefix: str
) -> list[tuple[ObjectStat, Checkpoint]]:
    """Load every stored checkpoint as ``(stat, Checkpoint)`` pairs.

    The :class:`~ne.contracts.storage.ObjectStat` is carried alongside the parsed
    document so the verify path can assert ``worm_retain_until`` — a checkpoint's
    tamper-evidence value comes from being UNDELETABLE, not merely from parsing.

    This full-body read is the VERIFY path only. The write path
    (:func:`maybe_checkpoint`) deliberately does not use it — see
    :func:`_last_archived_seq` (P3-G).

    Raises :class:`ValueError` (naming the offending key) on a malformed object so
    the caller can decide whether to fail loud (write path) or fail closed with a
    named invalid result (verify path) — never an unhandled traceback.
    """
    stats = await object_store.list_objects(prefix)
    out: list[tuple[ObjectStat, Checkpoint]] = []
    for stat in stats:
        raw = await object_store.get(stat.key)
        try:
            out.append((stat, Checkpoint.model_validate_json(raw)))
        except ValidationError as exc:
            raise ValueError(f"malformed checkpoint object at {stat.key}: {exc}") from exc
    return out


async def maybe_checkpoint(
    engine: sa.Engine,
    object_store: ObjectStore,
    signer: HmacSigner,
    *,
    key_path: str = DEFAULT_KEY_PATH,
    retention_days: int = 365,
    prefix: str = "audit/checkpoints/",
    every_n_records: int = 1000,
    now: Callable[[], datetime] = utcnow,
) -> Checkpoint | None:
    """Write a checkpoint only once the head has advanced ``every_n_records`` past
    the last ARCHIVED checkpoint; otherwise return ``None`` (idempotent between
    thresholds).

    "Archived" is load-bearing. A checkpoint that was ``put`` but whose
    ``archive`` failed is NOT under WORM retention and therefore is not a
    checkpoint at all for our purposes — counting it toward ``last_seq`` would
    park the threshold permanently past a seq that has no WORM protection, and
    the loop would never revisit it even after the store recovered. So we
    (1) :func:`repair_unarchived` such orphans and (2) compute ``last_seq`` from
    :func:`_last_archived_seq`, which ignores anything not really WORM.
    """
    await repair_unarchived(
        object_store,
        prefix,
        retain_until=now() + timedelta(days=retention_days),
    )
    last_seq = await _last_archived_seq(object_store, prefix)
    with engine.connect() as conn:
        head_seq, _ = read_head(conn)
    if head_seq < last_seq + every_n_records:
        return None
    return await write_checkpoint(
        engine,
        object_store,
        signer,
        key_path=key_path,
        retention_days=retention_days,
        prefix=prefix,
        now=now,
    )


async def _resolve_verification_signers(
    key_source: SecretStore,
    checkpoints: Sequence[tuple[ObjectStat, Checkpoint]],
) -> dict[tuple[str, int], HmacSigner | None]:
    """Pre-resolve one verification signer per ``(key_path, key_version)`` (P2-10).

    In SecretStore mode this reads each checkpoint's OWN signing-key VERSION via
    ``SecretStore.read(cp.key_path, version=cp.key_version)`` so a checkpoint signed
    under a since-ROTATED key still verifies (a single latest-secret signer would
    raise a PERMANENT false 'signature invalid' on every pre-rotation WORM
    checkpoint). A missing/destroyed version resolves to ``None`` — the caller then
    fails CLOSED with a named detail rather than a traceback. All ``await``s happen
    HERE, before the DB connection is opened, so nothing awaits inside the connect
    block. In HmacSigner (legacy single-key) mode this is not called.
    """
    signers: dict[tuple[str, int], HmacSigner | None] = {}
    for _stat, cp in _sorted(checkpoints):
        cache_key = (cp.key_path, cp.key_version)
        if cache_key in signers:
            continue
        try:
            secret = await key_source.read(cp.key_path, version=cp.key_version)
        except SecretNotFoundError:
            signers[cache_key] = None
            continue
        signers[cache_key] = HmacSigner(
            bytes.fromhex(secret.data["key"]), key_version=cp.key_version
        )
    return signers


async def verify_checkpoints(
    engine: sa.Engine,
    object_store: ObjectStore,
    key_source: SecretStore | HmacSigner,
    *,
    prefix: str = "audit/checkpoints/",
) -> CheckpointVerification:
    """Verify every stored checkpoint against the live chain — fail closed.

    For each checkpoint: (1) its signature must verify; (2) a DB row must exist at
    ``checkpoint.seq``; (3) that row's ``record_hash`` must equal the signed
    ``record_hash``; (4) the live head must not have been rewound BELOW the
    checkpoint's seq — catching a consistent chain-rewrite (which fools
    :func:`ne.audit.verify.verify_chain`), a truncation below a signed head, and a
    head rewind that leaves the signed rows in place but moves the head backwards.

    Key resolution (P2-10): pass a :class:`~ne.contracts.secrets.SecretStore` and
    each checkpoint is verified with the key VERSION it recorded
    (``SecretStore.read(cp.key_path, version=cp.key_version)``), so rotating the
    checkpoint signing key does NOT turn every pre-rotation WORM checkpoint into a
    permanent false tamper alarm. A raw :class:`HmacSigner` is still accepted for
    the single-key legacy/test path (a rotated store MUST use SecretStore mode); a
    missing key version fails CLOSED with a named detail (no traceback).

    WORM retention is CHECKED, not assumed. A stored checkpoint whose
    ``ObjectStat.worm_retain_until`` is ``None`` (its ``archive`` never landed)
    is a verification FAILURE, not a warning — the deliberate choice, because
    such an object is deletable and overwritable by the privileged attacker the
    control exists to catch, so it provides none of the guarantee the C10
    tamper-evidence claim advertises. Reporting it as merely-degraded-but-valid
    would let ``ne audit verify`` print GREEN over exactly the hole. The write
    path repairs these automatically (:func:`repair_unarchived`), so a persistent
    failure here means the object store is genuinely refusing to WORM-lock.

    Fails closed: a malformed stored checkpoint returns an invalid result naming
    the offending key (never an exception), and a missing ``ne_audit_head`` row
    returns an invalid result.

    IMPORTANT — this function is only as strong as the stored checkpoints. With
    ZERO stored checkpoints it returns ``valid=True, checked=0`` but provides NO
    tamper anchor (the ``detail`` says so), and the window AFTER the newest
    checkpoint is unprotected until the next checkpoint is written. It is WORM
    retention on the object store — not this function — that prevents an attacker
    from deleting the checkpoints themselves.
    """
    try:
        checkpoints = await _stored_checkpoints(object_store, prefix)
    except ValueError as exc:
        return CheckpointVerification(valid=False, checked=0, detail=str(exc))

    # Resolve every per-checkpoint verification key BEFORE opening the DB
    # connection — the SecretStore reads are async and must not await inside the
    # connect block (which holds a transaction open across the loop).
    resolved_signers: dict[tuple[str, int], HmacSigner | None] = {}
    if isinstance(key_source, HmacSigner):
        legacy_signer: HmacSigner | None = key_source
    else:
        legacy_signer = None
        resolved_signers = await _resolve_verification_signers(key_source, checkpoints)

    checked = 0
    with engine.connect() as conn:
        head = conn.execute(sa.select(ne_audit_head.c.seq).where(ne_audit_head.c.id == 1)).first()
        if head is None:
            return CheckpointVerification(
                valid=False, checked=0, detail="audit head missing (ne_audit_head row absent)"
            )
        head_seq = int(head.seq)
        for stat, cp in _sorted(checkpoints):
            if legacy_signer is not None:
                signer = legacy_signer
            else:
                signer = resolved_signers.get((cp.key_path, cp.key_version))
                if signer is None:
                    return CheckpointVerification(
                        valid=False,
                        checked=checked,
                        detail=(
                            f"signing key {cp.key_path} v{cp.key_version} unavailable "
                            f"for checkpoint at seq {cp.seq}"
                        ),
                    )
            unsigned = cp.model_copy(update={"signature": ""})
            if not signer.verify(unsigned.signing_preimage(), cp.signature):
                return CheckpointVerification(
                    valid=False,
                    checked=checked,
                    detail=f"checkpoint signature invalid at seq {cp.seq}",
                )
            if stat.worm_retain_until is None:
                return CheckpointVerification(
                    valid=False,
                    checked=checked,
                    detail=(
                        f"checkpoint at seq {cp.seq} is NOT under WORM retention "
                        f"({stat.key}) — it is deletable and overwritable"
                    ),
                )
            row = conn.execute(
                sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == cp.seq)
            ).first()
            if row is None:
                return CheckpointVerification(
                    valid=False,
                    checked=checked,
                    detail=f"chain truncated below signed checkpoint at seq {cp.seq}",
                )
            if row.record_hash != cp.record_hash:
                return CheckpointVerification(
                    valid=False,
                    checked=checked,
                    detail=f"chain rewritten vs signed checkpoint at seq {cp.seq}",
                )
            if head_seq < cp.seq:
                return CheckpointVerification(
                    valid=False,
                    checked=checked,
                    detail=f"head rewound below signed checkpoint at seq {cp.seq}",
                )
            checked += 1
    if checked == 0:
        return CheckpointVerification(
            valid=True, checked=0, detail="0 checkpoint(s) verified (no anchor)"
        )
    return CheckpointVerification(
        valid=True, checked=checked, detail=f"{checked} checkpoint(s) verified"
    )


def _sorted(
    checkpoints: Sequence[tuple[ObjectStat, Checkpoint]],
) -> list[tuple[ObjectStat, Checkpoint]]:
    return sorted(checkpoints, key=lambda sc: sc[1].seq)
