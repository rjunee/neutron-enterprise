# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""N -> N-1 upgrade proof (fixture-first, sqlite-shaped stores).

This is the executable proof behind the ``upgrade`` CI job (see
``.github/workflows/ci.yml``): given a store shaped like the PREVIOUS release
(N-1 — every migration in ``REGISTRY`` except the newest, plus pre-existing
user data that predates the migration machinery entirely), applying the
pending forward migration(s) reaches the CURRENT release's (N) schema with
data intact, and reversible migrations roll back cleanly to the N-1 shape.

``REGISTRY`` currently holds exactly one real migration (the v1 baseline), so
"N-1" here is the empty-registry prefix — a legitimate edge case: a brand-new
platform's very first migration IS the N-1 -> N step being proven. The fixture
table (``legacy_widgets``) stands in for schema created by a package's own
``ensure_*_schema()`` helper BEFORE the cross-module migration runner existed —
exactly the kind of pre-existing data an upgrade must never disturb.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Engine

from ne.cli.migrate import applied_versions, downgrade, upgrade
from ne.cli.migrations import REGISTRY

N_MINUS_1 = REGISTRY[:-1]
N = REGISTRY


def _seed_legacy_data(engine: Engine) -> None:
    """Pre-existing user data that predates the migration machinery (N-1 shape)."""
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE legacy_widgets (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        )
        conn.execute(
            text("INSERT INTO legacy_widgets (id, name) VALUES (:id, :name)"),
            [{"id": 1, "name": "sprocket"}, {"id": 2, "name": "cog"}],
        )


def _legacy_rows(engine: Engine) -> list[tuple[int, str]]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, name FROM legacy_widgets ORDER BY id")).fetchall()
        return [(row[0], row[1]) for row in rows]


def test_n_minus_1_store_upgrades_to_n_with_data_intact(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'n_minus_1.db'}")

    # Build the N-1-shaped store: N-1's migrations applied + pre-existing data.
    upgrade(engine, N_MINUS_1)
    _seed_legacy_data(engine)
    assert applied_versions(engine) == [m.version for m in N_MINUS_1]

    # Upgrade N-1 -> N.
    applied_now = upgrade(engine, N)
    assert applied_now == [m.version for m in N][len(N_MINUS_1) :]
    assert applied_versions(engine) == [m.version for m in N]

    # Full N schema present.
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert "ne_platform_meta" in tables

    # Pre-existing data untouched.
    assert _legacy_rows(engine) == [(1, "sprocket"), (2, "cog")]

    # ne_migrations records are complete.
    assert applied_versions(engine) == [m.version for m in N]


def test_reversible_migrations_roll_back_cleanly_to_n_minus_1(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'roundtrip.db'}")

    upgrade(engine, N_MINUS_1)
    _seed_legacy_data(engine)
    snapshot = _legacy_rows(engine)
    with engine.connect() as conn:
        tables_before = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }

    upgrade(engine, N)
    to_version = N_MINUS_1[-1].version if N_MINUS_1 else 0
    reverted = downgrade(engine, N, to=to_version)
    assert reverted == [m.version for m in N][len(N_MINUS_1) :][::-1]

    assert applied_versions(engine) == [m.version for m in N_MINUS_1]
    with engine.connect() as conn:
        tables_after = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert tables_after == tables_before
    assert _legacy_rows(engine) == snapshot


def test_upgrade_is_idempotent_on_already_n_store(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'already_n.db'}")
    upgrade(engine, N)
    assert applied_versions(engine) == [m.version for m in N]

    # Re-running upgrade on an already-N store is a no-op.
    assert upgrade(engine, N) == []
    assert applied_versions(engine) == [m.version for m in N]
