# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""`ne audit` — chain verification, on-demand checkpointing, and replay.

Follows schema.py's lazy-import discipline: ``ne.audit`` and ``sqlalchemy`` are
imported INSIDE the command bodies so bare ``ne --help`` stays light and does not
pull SQLAlchemy on unrelated invocations.
"""

from __future__ import annotations

import json as _json
import os
from datetime import datetime

import click


@click.group("audit")
def audit() -> None:
    """Tamper-evident audit chain: verify, checkpoint, replay."""


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


@audit.command("verify")
@click.option(
    "--database-url",
    envvar="NE_AUDIT_DATABASE_URL",
    required=True,
    help="SQLAlchemy database URL of the audit store.",
)
@click.option("--start", default=None, help="ISO datetime lower bound (occurred_at).")
@click.option("--end", default=None, help="ISO datetime upper bound (occurred_at).")
@click.option(
    "--checkpoint",
    "checkpoints",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Local signed checkpoint JSON file(s) to cross-check (repeatable). Each is "
    "verified with the key VERSION it recorded: set NE_AUDIT_SIGNING_KEY_V{n} (hex) "
    "per version to survive a signing-key rotation, or NE_AUDIT_SIGNING_KEY as the "
    "fallback for all versions.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def verify(
    database_url: str,
    start: str | None,
    end: str | None,
    checkpoints: tuple[str, ...],
    as_json: bool,
) -> None:
    """Walk the chain, recompute hashes, and report any break (exit 1 on break)."""
    import sqlalchemy as sa

    from ne.audit import Checkpoint, HmacSigner, verify_chain
    from ne.audit.store import ne_audit_log

    engine = sa.create_engine(database_url)
    cv = verify_chain(engine, start=_parse_dt(start), end=_parse_dt(end))

    checkpoint_detail: str | None = None
    checkpoint_ok = True
    if checkpoints:
        with engine.connect() as conn:
            for path in checkpoints:
                with open(path, "rb") as fh:
                    cp = Checkpoint.model_validate_json(fh.read())
                # Per-checkpoint key resolution (P2-10): resolve THIS checkpoint's own
                # key version so a rotation does not turn every archived checkpoint
                # into a permanent false tamper alarm. Version-specific env key first,
                # then the generic fallback.
                key_hex = os.environ.get(
                    f"NE_AUDIT_SIGNING_KEY_V{cp.key_version}"
                ) or os.environ.get("NE_AUDIT_SIGNING_KEY")
                if not key_hex:
                    raise click.ClickException(
                        f"--checkpoint at seq {cp.seq} needs signing key version "
                        f"{cp.key_version}: set NE_AUDIT_SIGNING_KEY_V{cp.key_version} "
                        "(or NE_AUDIT_SIGNING_KEY as a fallback) to the hex-encoded key"
                    )
                signer = HmacSigner(bytes.fromhex(key_hex))
                unsigned = cp.model_copy(update={"signature": ""})
                if not signer.verify(unsigned.signing_preimage(), cp.signature):
                    checkpoint_ok = False
                    checkpoint_detail = f"checkpoint signature invalid at seq {cp.seq}"
                    break
                row = conn.execute(
                    sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == cp.seq)
                ).first()
                if row is None:
                    checkpoint_ok = False
                    checkpoint_detail = f"chain truncated below signed checkpoint at seq {cp.seq}"
                    break
                if row.record_hash != cp.record_hash:
                    checkpoint_ok = False
                    checkpoint_detail = f"chain rewritten vs signed checkpoint at seq {cp.seq}"
                    break

    valid = cv.valid and checkpoint_ok
    if as_json:
        click.echo(
            _json.dumps(
                {
                    "valid": valid,
                    "checked": cv.checked,
                    "chain_detail": cv.detail,
                    "first_break_seq": cv.first_break_seq,
                    "first_break_event_id": cv.first_break_event_id,
                    "checkpoint_detail": checkpoint_detail,
                }
            )
        )
    else:
        click.echo(cv.detail)
        if checkpoint_detail is not None:
            click.echo(checkpoint_detail)
        elif checkpoints:
            click.echo(f"{len(checkpoints)} checkpoint(s) verified")

    if not valid:
        raise click.exceptions.Exit(1)


@audit.command("checkpoint")
@click.option(
    "--database-url",
    envvar="NE_AUDIT_DATABASE_URL",
    required=True,
    help="SQLAlchemy database URL of the audit store.",
)
@click.option(
    "--out",
    required=True,
    type=click.Path(dir_okay=False),
    help="Path to write the signed checkpoint JSON. NOTE: the production WORM/"
    "ObjectStore route is the library API (write_checkpoint) using ne-storage's "
    "'s3'/'azure_blob' adapters; this CLI command always writes a local file, "
    "not WORM storage.",
)
@click.option(
    "--key-version",
    type=int,
    default=1,
    envvar="NE_AUDIT_SIGNING_KEY_VERSION",
    help="Signing-key version to RECORD on the checkpoint so a later rotation can "
    "resolve the right key per checkpoint (default 1). The key bytes come from "
    "NE_AUDIT_SIGNING_KEY_V{n} (falling back to NE_AUDIT_SIGNING_KEY).",
)
def checkpoint(database_url: str, out: str, key_version: int) -> None:
    """Sign the current chain head and write a checkpoint JSON (NE_AUDIT_SIGNING_KEY)."""
    import sqlalchemy as sa

    from ne.audit import Checkpoint, HmacSigner
    from ne.audit.checkpoint import utcnow
    from ne.audit.store import read_head

    key_hex = os.environ.get(f"NE_AUDIT_SIGNING_KEY_V{key_version}") or os.environ.get(
        "NE_AUDIT_SIGNING_KEY"
    )
    if not key_hex:
        raise click.ClickException(
            f"checkpoint requires the NE_AUDIT_SIGNING_KEY_V{key_version} (or "
            "NE_AUDIT_SIGNING_KEY) environment variable (hex-encoded signing key)"
        )
    signer = HmacSigner(bytes.fromhex(key_hex), key_version=key_version)

    engine = sa.create_engine(database_url)
    with engine.connect() as conn:
        seq, record_hash = read_head(conn)

    unsigned = Checkpoint(
        seq=seq,
        record_hash=record_hash,
        created_at=utcnow(),
        key_path="ne/audit/checkpoint-signing",
        key_version=signer.key_version,
        signature="",
    )
    signature = signer.sign(unsigned.signing_preimage())
    cp = unsigned.model_copy(update={"signature": signature})
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(cp.model_dump_json())
    click.echo(f"wrote checkpoint at seq {seq} -> {out}")


@audit.command("replay")
@click.option(
    "--database-url",
    envvar="NE_AUDIT_DATABASE_URL",
    required=True,
    help="SQLAlchemy database URL of the audit store.",
)
@click.option("--trace-id", default=None, help="Replay every record on this trace.")
@click.option("--event-id", default=None, help="Replay this record's whole trace.")
@click.option("--resource", default=None, help="Replay records on this resource.")
@click.option("--start", default=None, help="ISO datetime lower bound (with --resource).")
@click.option("--end", default=None, help="ISO datetime upper bound (with --resource).")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def replay(
    database_url: str,
    trace_id: str | None,
    event_id: str | None,
    resource: str | None,
    start: str | None,
    end: str | None,
    as_json: bool,
) -> None:
    """Answer "why did the system say X on date D?" — config_hash + context + events."""
    import sqlalchemy as sa

    from ne.audit import replay as run_replay

    selectors = [s for s in (trace_id, event_id, resource) if s is not None]
    if len(selectors) != 1:
        raise click.ClickException("provide exactly one of --trace-id / --event-id / --resource")

    engine = sa.create_engine(database_url)
    result = run_replay(
        engine,
        trace_id=trace_id,
        event_id=event_id,
        resource=resource,
        start=_parse_dt(start),
        end=_parse_dt(end),
    )

    if as_json:
        click.echo(
            _json.dumps(
                {
                    "config_hashes": list(result.config_hashes),
                    "context_ids": list(result.context_ids),
                    "events": [
                        {
                            "seq": e.seq,
                            "event_id": e.event_id,
                            "event_type": e.event_type,
                            "action": e.action,
                            "resource": e.resource,
                            "outcome": e.outcome,
                            "occurred_at": e.occurred_at.isoformat(),
                        }
                        for e in result.events
                    ],
                }
            )
        )
        return

    click.echo(f"config_hash(es): {', '.join(result.config_hashes) or '(none)'}")
    click.echo(f"context_ids: {', '.join(result.context_ids) or '(none)'}")
    click.echo(f"events ({len(result.events)}):")
    for e in result.events:
        click.echo(
            f"  seq={e.seq} {e.event_type} action={e.action} resource={e.resource} "
            f"outcome={e.outcome} at={e.occurred_at.isoformat()}"
        )
