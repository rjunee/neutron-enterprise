# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-AUDIT-CRON — the periodic signed-WORM audit-checkpoint maintenance loop.

Wires ``ne.audit.checkpoint.maybe_checkpoint`` onto a recurring tick in ne-worker so
signed WORM checkpoints are written automatically on a schedule, not only on demand
via ``ne audit checkpoint`` — closing the gap the ``ne.audit.checkpoint`` module
docstring named.

DESIGN JUDGMENT (decided): this is a PLAIN maintenance loop — the same shape as
``_heartbeat_loop`` / ``_outbox_drain_loop`` in ``main.py`` — NOT a
``ScheduledJob`` on the shared ``CronScheduler`` (see ``cron_seed.py``). The
``ScheduledJob``/``CronScheduler`` surface is built for GOVERNED AGENT TOOL CALLS:
``scheduler.py``'s ``_run_claimed_job`` drives ``run_isolated_agent`` — a REAL LLM
agent turn through the model gateway that must CHOOSE to invoke its granted tool.
Anchoring the tamper-evidence chain's integrity primitive on a nondeterministic
model call (and requiring a live model lane to fire at all) is unacceptable: a
checkpoint must be written because a deterministic threshold was crossed, not
because a model decided to call a tool. Checkpointing is a platform-internal
maintenance operation — it reads the chain head and writes to the object store,
appending no ungoverned audit records — exactly like the outbox drain, so it
follows that precedent instead (no new scheduling mechanism, no governed
components constructed — see ``test_no_bypass_worker.py``).

DEFAULT-SAFE: :func:`build_audit_checkpoint_loop` returns ``None`` when
``WorkerSettings.audit_checkpoint_object_store`` is empty (the default) — no task
is created, mirroring the ``outbox_consumer`` / cron_seed non-destructive-empty-set
posture. A CONFIGURED-but-unknown adapter name fails worker BOOT loud
(``PluginNotFoundError``, the ``_build_outbox_handler`` precedent) — never a silent
no-op. A missing signing key at runtime is a throttled, VISIBLE warning (fail
closed, never a crash, never a silent no-op) — see :func:`run_audit_checkpoint_loop`.

CONCURRENCY (multi-replica, one DB + one object store): same-``seq`` races between
replicas converge on ONE WORM key (``f"{prefix}{seq:020d}.json"``, see
``ne.audit.checkpoint._checkpoint_key``) — the loser's ``put`` (pre-archive) or
``archive`` (post-put) call raises :class:`~ne.contracts.errors.WormViolationError`,
which this loop treats as a BENIGN logged skip: a peer already wrote (and, in the
archive race, already WORM-locked) the identical checkpoint for that seq. This is
DELIBERATELY NOT the ``read_head`` ``FOR UPDATE`` row lock ``append_record`` uses to
serialize appends — holding that lock across the checkpoint's object-store I/O
(a network round-trip) would stall the hot audit-append path for every writer in the
process, for the sake of an operation that already tolerates a benign race.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlalchemy as sa

from ne.audit.checkpoint import Checkpoint, HmacSigner, maybe_checkpoint
from ne.audit.config import CheckpointConfig
from ne.contracts.errors import SecretNotFoundError, WormViolationError
from ne.contracts.plugins import PluginRegistry
from ne.contracts.secrets import SecretStore
from ne.contracts.storage import ObjectStore
from ne.net.ssrf import redact_url_for_logs

if TYPE_CHECKING:
    from ne.api.platform import Platform
    from ne.worker.settings import WorkerSettings

_LOG = logging.getLogger("ne.worker.audit_checkpoint")

#: Minimum interval (seconds) between repeated throttled warnings for the SAME
#: failure kind — mirrors the outbox drain-loop's 60s throttle.
_WARN_THROTTLE_S = 60.0


@dataclass(frozen=True)
class AuditCheckpointLoop:
    """The wired seams the periodic checkpoint loop runs over."""

    engine: sa.Engine
    secret_store: SecretStore
    object_store: ObjectStore
    config: CheckpointConfig
    interval_s: float


def build_audit_checkpoint_loop(
    platform: Platform,
    worker_settings: WorkerSettings,
    *,
    registry: PluginRegistry | None = None,
) -> AuditCheckpointLoop | None:
    """Build the loop's seams, or return ``None`` when the loop is unconfigured.

    An EMPTY ``audit_checkpoint_object_store`` (the default) is default-safe: no
    loop is built and — per ``main.py`` — no task is created. A CONFIGURED but
    UNKNOWN ``ne.plugins.object_store`` adapter name raises
    :class:`~ne.contracts.errors.PluginNotFoundError` here, at worker BOOT, so a
    typo fails loud instead of silently disabling the loop.

    Wires ``platform.audit_engine`` (the authoritative chain the sink writes and
    ``ne audit verify`` targets — NOT ``platform.runtime_engine``),
    ``platform.secret_store``, and the FROZEN ``platform.audit_settings.checkpoint``
    policy (key path / prefix / retention / threshold) — so this loop and the
    on-demand ``ne audit checkpoint`` CLI always agree on policy.
    """
    name = worker_settings.audit_checkpoint_object_store
    if not name:
        _LOG.info(
            "audit checkpoint loop disabled (NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE is empty)"
        )
        return None

    registry = registry or PluginRegistry()
    cls = registry.resolve("object_store", name)
    if hasattr(cls, "from_settings"):
        object_store = cls.from_settings(platform.audit_settings)
    else:
        object_store = cls()

    return AuditCheckpointLoop(
        engine=platform.audit_engine,
        secret_store=platform.secret_store,
        object_store=object_store,
        config=platform.audit_settings.checkpoint,
        interval_s=worker_settings.audit_checkpoint_interval_s,
    )


async def checkpoint_once(loop: AuditCheckpointLoop) -> Checkpoint | None:
    """Sign (if the key resolves) and maybe-write one checkpoint.

    Raises :class:`~ne.contracts.errors.SecretNotFoundError` when the signing key
    is absent and :class:`~ne.contracts.errors.WormViolationError` on a benign
    peer-replica race — both are handled by the caller,
    :func:`run_audit_checkpoint_loop`.
    """
    signer = await HmacSigner.from_secret_store(loop.secret_store, loop.config.key_path)
    return await maybe_checkpoint(
        loop.engine,
        loop.object_store,
        signer,
        key_path=loop.config.key_path,
        retention_days=loop.config.retention_days,
        prefix=loop.config.prefix,
        every_n_records=loop.config.every_n_records,
    )


async def run_audit_checkpoint_loop(loop: AuditCheckpointLoop, stop: asyncio.Event) -> None:
    """Fire-first-then-wait (the outbox pattern): tick every ``loop.interval_s``.

    - A written :class:`~ne.audit.checkpoint.Checkpoint` (threshold crossed) is
      logged INFO with its seq + prefix.
    - :class:`~ne.contracts.errors.WormViolationError` (a peer replica already
      archived this seq) is logged INFO and skipped — a benign convergent race,
      not a failure.
    - :class:`~ne.contracts.errors.SecretNotFoundError` (no signing key configured)
      is a throttled (60s) WARNING naming the key path — checkpoints are NOT being
      written, fail closed and VISIBLE, never a crash, never a silent no-op.
    - Any other exception is a throttled (60s) WARNING carrying ACTIONABLE
      detail: the exception type, its message, the object-store adapter, the
      configured prefix and the signing-key path — plus a full traceback on the
      FIRST occurrence only. A permanent defect (wrong bucket, bad credentials,
      malformed JSON, invalid hex signing key) must not look identical to a
      transient outage forever while the worker reports healthy. The message is
      passed through ``redact_url_for_logs`` (the repo's credential-redaction
      precedent) so an endpoint URL embedded in a storage driver's error text
      cannot leak userinfo into the log. The 60s throttle is preserved, so this
      is at most one traceback plus one line per minute per failure kind.
    """
    last_warn: dict[str, float] = {}

    def _throttled_warn(key: str, message: str, *, exc_info: BaseException | None = None) -> None:
        now = time.monotonic()
        if now - last_warn.get(key, 0.0) >= _WARN_THROTTLE_S:
            last_warn[key] = now
            _LOG.warning(message, exc_info=exc_info)

    while not stop.is_set():
        try:
            checkpoint = await checkpoint_once(loop)
        except WormViolationError:
            _LOG.info(
                "audit checkpoint already archived by a peer replica (benign convergent race)"
            )
        except SecretNotFoundError:
            _throttled_warn(
                "secret",
                "audit checkpoint loop: no signing key at "
                f"{loop.config.key_path!r} — checkpoints are NOT being written "
                "(throttled 60s)",
            )
        except Exception as exc:  # deliberate broad catch, throttled (outbox-drain precedent)
            first = "other" not in last_warn
            _throttled_warn(
                "other",
                "audit checkpoint loop error (throttled 60s): "
                f"{type(exc).__name__}: {redact_url_for_logs(str(exc))} "
                f"[object_store={type(loop.object_store).__name__} "
                f"prefix={loop.config.prefix!r} key_path={loop.config.key_path!r}]",
                exc_info=exc if first else None,
            )
        else:
            if checkpoint is not None:
                _LOG.info(
                    "audit checkpoint written seq=%d prefix=%s",
                    checkpoint.seq,
                    loop.config.prefix,
                )

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=loop.interval_s)


__all__ = [
    "AuditCheckpointLoop",
    "build_audit_checkpoint_loop",
    "checkpoint_once",
    "run_audit_checkpoint_loop",
]
