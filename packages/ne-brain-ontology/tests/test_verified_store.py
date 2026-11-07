# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-ONTOLOGY-FLYWHEEL: the governed, append-only ne_verified_templates store."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.audit import ensure_transactional_sqlite
from ne.brain_ontology.verified.errors import VerifiedTemplateConflictError
from ne.brain_ontology.verified.store import (
    VerifiedTemplateRecord,
    append_verified_template,
    ensure_verified_template_schema,
    read_verified_templates,
)


def _record(template_id: str, version: int, body: str) -> VerifiedTemplateRecord:
    return VerifiedTemplateRecord(
        template_id=template_id,
        version=version,
        template_toml=body,
        toml_sha256=hashlib.sha256(body.encode()).hexdigest(),
        proposal_id=f"sha256:{template_id}-v{version}",
        actor="user:steward",
        config_hash="c" * 64,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[sa.Engine]:
    eng = sa.create_engine(f"sqlite:///{tmp_path}/store.db")
    ensure_transactional_sqlite(eng)
    ensure_verified_template_schema(eng)
    # Idempotent re-ensure is a no-op.
    ensure_verified_template_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def test_append_and_read_roundtrip(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        rid_a = append_verified_template(conn, _record("accounts-directory", 1, "body-a"))
        rid_b = append_verified_template(conn, _record("orders-by-date", 1, "body-b"))
    assert rid_a != rid_b
    rows = read_verified_templates(engine)
    assert [(r.template_id, r.version) for r in rows] == [
        ("accounts-directory", 1),
        ("orders-by-date", 1),
    ]
    # created_at is tz-normalized to aware UTC on read-back (SQLite reads naive).
    assert all(r.created_at.tzinfo is not None for r in rows)


def test_savepoint_idempotency_returns_existing_id(engine: sa.Engine) -> None:
    rec = _record("accounts-directory", 1, "body-a")
    with engine.begin() as conn:
        first = append_verified_template(conn, rec)
        # A re-append of the SAME (id, version, sha) inside the SAME live
        # transaction returns the existing row id (crash-retry-safe).
        second = append_verified_template(conn, rec)
        assert first == second
    assert len(read_verified_templates(engine)) == 1


def test_different_sha_same_slot_raises_conflict(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        append_verified_template(conn, _record("accounts-directory", 1, "body-a"))
    with engine.begin() as conn:  # noqa: SIM117 - separate txn on purpose
        with pytest.raises(VerifiedTemplateConflictError, match="different body"):
            append_verified_template(conn, _record("accounts-directory", 1, "DIFFERENT"))
    # The store still holds exactly the first row (no partial write).
    rows = read_verified_templates(engine)
    assert len(rows) == 1
    assert rows[0].template_toml == "body-a"


def test_read_accepts_connection_inside_txn(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        append_verified_template(conn, _record("accounts-directory", 1, "body-a"))
        # A read on the SAME live connection sees the just-appended row.
        rows = read_verified_templates(conn)
    assert [r.template_id for r in rows] == ["accounts-directory"]
