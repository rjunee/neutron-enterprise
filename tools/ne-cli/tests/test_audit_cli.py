# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the `ne audit` CLI group (verify / checkpoint / replay)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from click.testing import CliRunner

from ne.audit import (
    Checkpoint,
    HmacSigner,
    append_record,
    compute_record_hash,
    ensure_audit_schema,
)
from ne.audit.store import ne_audit_head, ne_audit_log
from ne.cli import main
from ne.schema import AuditEvent, AuditOutcome

SIGNING_KEY = "ab" * 16
KEY_V1 = "11" * 16
KEY_V2 = "22" * 16


def _mk(event_id: str, **over: object) -> AuditEvent:
    fields: dict[str, Any] = {
        "event_id": event_id,
        "event_type": "audit",
        "config_hash": "a" * 64,
        "actor": "sys",
        "provenance": {},
        "occurred_at": datetime(2026, 3, 3, tzinfo=UTC),
        "action": "tool.invoke",
        "resource": "urn:r",
        "outcome": AuditOutcome.success,
    }
    fields.update(over)
    return AuditEvent(**fields)


def _seed_db(tmp_path: Path, n: int = 4) -> tuple[str, sa.Engine]:
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    engine = sa.create_engine(url)
    ensure_audit_schema(engine)
    with engine.begin() as conn:
        for i in range(n):
            append_record(
                conn,
                _mk(
                    f"e{i}",
                    trace_id="tr-1",
                    provenance={"context_ids": "chunk-1,chunk-2"} if i == 1 else {},
                ),
            )
    return url, engine


def test_help_lists_audit_and_existing_groups() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for group in ("audit", "check", "release", "dev", "schema"):
        assert group in result.output


def test_verify_intact_exits_zero(tmp_path: Path) -> None:
    url, _ = _seed_db(tmp_path)
    result = CliRunner().invoke(main, ["audit", "verify", "--database-url", url])
    assert result.exit_code == 0, result.output
    assert "intact" in result.output


def test_verify_tamper_exits_one_and_names_record(tmp_path: Path) -> None:
    url, engine = _seed_db(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 2)
            .values(payload={"event_id": "e1", "tampered": True})
        )
    result = CliRunner().invoke(main, ["audit", "verify", "--database-url", url])
    assert result.exit_code == 1
    assert "seq 2" in result.output
    assert "e1" in result.output


def test_checkpoint_writes_valid_file_then_verify(tmp_path: Path) -> None:
    url, engine = _seed_db(tmp_path)
    out = tmp_path / "cp.json"
    runner = CliRunner()
    env = {"NE_AUDIT_SIGNING_KEY": SIGNING_KEY}
    res = runner.invoke(
        main,
        ["audit", "checkpoint", "--database-url", url, "--out", str(out)],
        env=env,
    )
    assert res.exit_code == 0, res.output
    assert out.exists()

    # verify --checkpoint passes on the intact chain ...
    ok = runner.invoke(
        main,
        ["audit", "verify", "--database-url", url, "--checkpoint", str(out)],
        env=env,
    )
    assert ok.exit_code == 0, ok.output
    assert "checkpoint(s) verified" in ok.output

    # ... and fails after a consistent chain rewrite.
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
        prev = conn.execute(
            sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == 3)
        ).scalar_one()
        row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == 4)).one()
        payload = dict(row.payload)
        payload["tampered"] = True
        new_hash = compute_record_hash(prev, payload)
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 4)
            .values(payload=payload, prev_hash=prev, record_hash=new_hash)
        )
        conn.execute(
            ne_audit_head.update().where(ne_audit_head.c.id == 1).values(record_hash=new_hash)
        )
    bad = runner.invoke(
        main,
        ["audit", "verify", "--database-url", url, "--checkpoint", str(out)],
        env=env,
    )
    assert bad.exit_code == 1
    assert "rewritten" in bad.output


def test_checkpoint_missing_key_with_flag_is_clean_error(tmp_path: Path) -> None:
    url, _ = _seed_db(tmp_path)
    out = tmp_path / "cp.json"
    # First make a checkpoint file (with a key) so --checkpoint has a file to read.
    CliRunner().invoke(
        main,
        ["audit", "checkpoint", "--database-url", url, "--out", str(out)],
        env={"NE_AUDIT_SIGNING_KEY": SIGNING_KEY},
    )
    res = CliRunner().invoke(
        main,
        ["audit", "verify", "--database-url", url, "--checkpoint", str(out)],
        env={"NE_AUDIT_SIGNING_KEY": ""},
    )
    assert res.exit_code != 0
    assert "NE_AUDIT_SIGNING_KEY" in res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)


def test_replay_prints_config_and_context(tmp_path: Path) -> None:
    url, _ = _seed_db(tmp_path)
    result = CliRunner().invoke(
        main, ["audit", "replay", "--database-url", url, "--trace-id", "tr-1"]
    )
    assert result.exit_code == 0, result.output
    assert "a" * 64 in result.output
    assert "chunk-1" in result.output
    assert "chunk-2" in result.output


def test_replay_json_is_parseable(tmp_path: Path) -> None:
    url, _ = _seed_db(tmp_path)
    result = CliRunner().invoke(
        main,
        ["audit", "replay", "--database-url", url, "--trace-id", "tr-1", "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["config_hashes"] == ["a" * 64]
    assert data["context_ids"] == ["chunk-1", "chunk-2"]
    assert len(data["events"]) == 4


def test_verify_json_is_parseable(tmp_path: Path) -> None:
    url, _ = _seed_db(tmp_path)
    result = CliRunner().invoke(main, ["audit", "verify", "--database-url", url, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["valid"] is True
    assert data["checked"] == 4


def _record_hash_at(engine: sa.Engine, seq: int) -> str:
    with engine.connect() as conn:
        return conn.execute(
            sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == seq)
        ).scalar_one()


def _sign_cp(*, seq: int, record_hash: str, key_version: int, key_hex: str) -> Checkpoint:
    signer = HmacSigner(bytes.fromhex(key_hex), key_version=key_version)
    unsigned = Checkpoint(
        seq=seq,
        record_hash=record_hash,
        created_at=datetime(2026, 3, 3, tzinfo=UTC),
        key_path="ne/audit/checkpoint-signing",
        key_version=key_version,
        signature="",
    )
    return unsigned.model_copy(update={"signature": signer.sign(unsigned.signing_preimage())})


def test_verify_checkpoint_key_rotation_via_versioned_env_keys(tmp_path: Path) -> None:
    url, engine = _seed_db(tmp_path)  # 4 records
    cp1 = _sign_cp(seq=2, record_hash=_record_hash_at(engine, 2), key_version=1, key_hex=KEY_V1)
    cp2 = _sign_cp(seq=4, record_hash=_record_hash_at(engine, 4), key_version=2, key_hex=KEY_V2)
    f1 = tmp_path / "cp1.json"
    f2 = tmp_path / "cp2.json"
    f1.write_text(cp1.model_dump_json())
    f2.write_text(cp2.model_dump_json())

    runner = CliRunner()
    # Per-version env keys → both checkpoints verify across the rotation.
    ok = runner.invoke(
        main,
        [
            "audit",
            "verify",
            "--database-url",
            url,
            "--checkpoint",
            str(f1),
            "--checkpoint",
            str(f2),
        ],
        env={"NE_AUDIT_SIGNING_KEY_V1": KEY_V1, "NE_AUDIT_SIGNING_KEY_V2": KEY_V2},
    )
    assert ok.exit_code == 0, ok.output
    assert "checkpoint(s) verified" in ok.output

    # Only the v2 key as the generic fallback → the v1 checkpoint fails, naming seq 2.
    bad = runner.invoke(
        main,
        [
            "audit",
            "verify",
            "--database-url",
            url,
            "--checkpoint",
            str(f1),
            "--checkpoint",
            str(f2),
        ],
        env={"NE_AUDIT_SIGNING_KEY": KEY_V2},
    )
    assert bad.exit_code == 1
    assert "seq 2" in bad.output


def test_checkpoint_cli_records_key_version(tmp_path: Path) -> None:
    url, _ = _seed_db(tmp_path)
    out = tmp_path / "cp.json"
    res = CliRunner().invoke(
        main,
        ["audit", "checkpoint", "--database-url", url, "--out", str(out), "--key-version", "2"],
        env={"NE_AUDIT_SIGNING_KEY": SIGNING_KEY},
    )
    assert res.exit_code == 0, res.output
    doc = json.loads(out.read_text())
    assert doc["key_version"] == 2
