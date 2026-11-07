# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Cross-module migration runner + the fail-closed reversible-migration policy (NE-069).

``ne migrate`` applies ordered, versioned, idempotent cross-module schema/data
migrations on top of SQLAlchemy Core, against ANY SQLAlchemy-supported engine
(sqlite in v1 fixtures/tests; the SQL is ANSI-portable so Postgres rides the
same runner in production — see ``tools/ne-cli/README.md``).

**Reversible-migration policy (fail-closed).** Every :class:`Migration` must
declare EITHER a ``down`` step (truly reversible) OR a non-blank
``irreversible_justification`` (an explicit, human-authored reason it cannot be
reversed) — never both, never neither. :func:`validate_registry` enforces this
BEFORE any migration ever touches a database: an unmarked irreversible
migration, a blank justification, or a migration carrying both a ``down`` AND a
justification all raise :class:`MigrationPolicyError`. ``downgrade`` also
refuses, up front (nothing partially reverted), the moment any migration in the
requested revert range lacks a ``down`` step.

**Ordering + idempotency + version tracking.** Versions must be strictly
increasing positive integers, unique, non-empty-named. A ``ne_migrations``
tracking table (created via ``MetaData().create_all(..., checkfirst=True)``)
records ``(version, name, applied_at)`` for every applied migration, each
inside the SAME transaction as its ``up`` step — a failing ``up`` rolls back
both the schema change and the tracking row (atomic). Re-running ``upgrade``
after it has already reached the target is therefore a no-op. Any tracked
version absent from the current registry is **drift** and every command
refuses, fail-closed, before touching the database.
"""

from __future__ import annotations

import datetime as _dt
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import click
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

_METADATA = sa.MetaData()

ne_migrations_table = sa.Table(
    "ne_migrations",
    _METADATA,
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("applied_at", sa.String, nullable=False),
)

# Engines already patched by `_ensure_transactional_ddl` (a WeakSet so we never
# keep a test/CLI engine alive past its natural lifetime).
_ddl_fixed_engines: weakref.WeakSet[Engine] = weakref.WeakSet()


def _ensure_transactional_ddl(engine: Engine) -> None:
    """Make DDL participate in the SAME transaction as its ``engine.begin()`` block.

    SQLAlchemy's pysqlite driver has a well-known quirk (documented in the
    SQLAlchemy docs, "Serializable isolation / Savepoints / Transactional DDL"):
    by default it silently COMMITS before every DDL statement, which would
    break this runner's core atomicity guarantee (a failing migration must roll
    back its DDL *and* its ``ne_migrations`` row together). This installs the
    documented fix — disable pysqlite's own implicit transaction handling and
    have SQLAlchemy issue ``BEGIN`` explicitly.

    This DELIBERATELY uses the ``"checkout"`` pool event instead of the docs'
    ``"connect"`` event, because ``"connect"`` fires only for DBAPI connections
    created AFTER the listener exists and silently misses connections already
    pooled from earlier engine use — those kept pysqlite's legacy isolation
    behavior, leaving the atomic-rollback guarantee resting on the
    ``"begin"`` listener alone (Codex P2-A, NE-069 continuation).
    ``"checkout"`` fires on EVERY pool checkout, pre-existing connections
    included. At checkout the connection is idle (pool reset-on-return has
    already rolled back any open transaction), so flipping
    ``isolation_level`` is safe — the pysqlite setter would otherwise
    implicitly commit an open transaction, but there is none at that point.

    This is the reference implementation NE-CLEAN2 (SPEC §11) applies
    cross-cuttingly wherever else the codebase needs transactional pysqlite
    DDL. Installed for sqlite engines only, and only once per engine
    (idempotent; a WeakSet tracks which engines are already patched so
    re-running this on every command is safe). Postgres (and every other real
    transactional-DDL dialect) needs no such patch.
    """
    if engine.dialect.name != "sqlite" or engine in _ddl_fixed_engines:
        return
    _ddl_fixed_engines.add(engine)

    @event.listens_for(engine, "checkout")
    def _do_checkout(dbapi_connection: Any, connection_record: Any, connection_proxy: Any) -> None:
        if dbapi_connection.isolation_level is not None:
            dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _do_begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN")


class MigrationPolicyError(Exception):
    """Raised when the reversible-migration policy is violated (fail-closed)."""


class MigrationDriftError(Exception):
    """Raised when the tracking table records a version absent from the registry."""


@dataclass(frozen=True)
class Migration:
    """One versioned, ordered, idempotent cross-module migration step.

    Exactly one of ``down`` / ``irreversible_justification`` must be set — see
    the module docstring's "Reversible-migration policy" section.
    """

    version: int
    name: str
    up: Callable[[Connection], None]
    down: Callable[[Connection], None] | None = None
    irreversible_justification: str | None = None


def validate_registry(migrations: Sequence[Migration]) -> tuple[Migration, ...]:
    """Validate ordering + the reversible-migration policy. Fail-closed.

    Raises :class:`MigrationPolicyError` on the first violation found:
    non-positive/duplicate/non-increasing versions, a blank ``name``, an
    unmarked irreversible migration (``down`` is ``None`` and
    ``irreversible_justification`` is ``None``/blank), or a contradictory
    migration (BOTH ``down`` and a justification set).
    """
    ordered = tuple(migrations)
    seen_versions: set[int] = set()
    last_version = 0
    for mig in ordered:
        if mig.version <= 0:
            raise MigrationPolicyError(
                f"migration {mig.name!r} has non-positive version {mig.version}"
            )
        if mig.version in seen_versions:
            raise MigrationPolicyError(f"duplicate migration version {mig.version}")
        if mig.version <= last_version:
            raise MigrationPolicyError(
                f"migration versions must be strictly increasing: {mig.version} "
                f"does not follow {last_version}"
            )
        if not mig.name or not mig.name.strip():
            raise MigrationPolicyError(f"migration version {mig.version} has a blank name")

        justification = (mig.irreversible_justification or "").strip()
        has_down = mig.down is not None
        has_justification = bool(justification)

        if has_down and has_justification:
            raise MigrationPolicyError(
                f"migration {mig.version} ({mig.name!r}) is contradictory: it declares "
                "BOTH a down step AND an irreversible_justification — a migration is "
                "either reversible (down) or irreversible-with-justification, not both"
            )
        if not has_down and not has_justification:
            raise MigrationPolicyError(
                f"migration {mig.version} ({mig.name!r}) is an unmarked irreversible "
                "migration (no down step and no irreversible_justification) — refused "
                "(reversible-migration policy)"
            )

        seen_versions.add(mig.version)
        last_version = mig.version
    return ordered


def ensure_migrations_schema(engine: Engine) -> None:
    """Create the ``ne_migrations`` tracking table if absent (idempotent)."""
    _ensure_transactional_ddl(engine)
    _METADATA.create_all(engine, tables=[ne_migrations_table], checkfirst=True)


def applied_versions(engine: Engine) -> list[int]:
    """The sorted list of migration versions recorded as applied."""
    ensure_migrations_schema(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(ne_migrations_table.c.version).order_by(ne_migrations_table.c.version)
        )
        return [row[0] for row in rows]


def _check_drift(registry: Sequence[Migration], engine: Engine) -> None:
    registered = {mig.version for mig in registry}
    applied = applied_versions(engine)
    unknown = [v for v in applied if v not in registered]
    if unknown:
        raise MigrationDriftError(
            "database has applied migration version(s) unknown to the current "
            f"registry: {unknown} — refusing to proceed (registry/database drift)"
        )


def _by_version(registry: Sequence[Migration]) -> dict[int, Migration]:
    return {mig.version: mig for mig in registry}


def upgrade(engine: Engine, registry: Sequence[Migration], to: int | None = None) -> list[int]:
    """Apply every pending migration in order, up to (and including) ``to``.

    Idempotent: re-running with the same or a lower target after a prior
    upgrade applies nothing (already-applied versions are skipped). Each
    migration's ``up`` step and its ``ne_migrations`` tracking row are written
    inside ONE transaction (``engine.begin()``) — a raised exception rolls
    back both, so a failed migration never leaves a partial schema change or a
    dangling tracking row. Returns the list of newly-applied versions.
    """
    validated = validate_registry(registry)
    ensure_migrations_schema(engine)
    _check_drift(validated, engine)

    already = set(applied_versions(engine))
    target = to if to is not None else (validated[-1].version if validated else 0)
    if to is not None and to not in _by_version(validated) and to != 0:
        raise click.ClickException(f"--to version {to} does not exist in the registry")

    applied_now: list[int] = []
    for mig in validated:
        if mig.version > target:
            break
        if mig.version in already:
            continue
        with engine.begin() as conn:
            mig.up(conn)
            conn.execute(
                ne_migrations_table.insert().values(
                    version=mig.version,
                    name=mig.name,
                    applied_at=_dt.datetime.now(_dt.UTC).isoformat(),
                )
            )
        applied_now.append(mig.version)
    return applied_now


def downgrade(engine: Engine, registry: Sequence[Migration], to: int) -> list[int]:
    """Revert applied migrations in reverse order, down to (and excluding) ``to``.

    Refuses UP FRONT — before reverting anything — if ANY migration in the
    revert range lacks a ``down`` step, naming the offending migration and its
    ``irreversible_justification`` (fail-closed: nothing is partially
    reverted). Each ``down`` step + tracking-row delete runs inside its own
    transaction. Returns the list of reverted versions (in the order they were
    reverted).
    """
    validated = validate_registry(registry)
    ensure_migrations_schema(engine)
    _check_drift(validated, engine)
    by_version = _by_version(validated)

    if to != 0 and to not in by_version:
        raise click.ClickException(f"--to version {to} does not exist in the registry")

    applied = applied_versions(engine)
    targets = sorted((v for v in applied if v > to), reverse=True)

    blocking = [v for v in targets if by_version[v].down is None]
    if blocking:
        offender = by_version[blocking[0]]
        raise MigrationPolicyError(
            f"cannot downgrade past irreversible migration {offender.version} "
            f"({offender.name!r}): {offender.irreversible_justification} — refusing "
            "(nothing has been reverted)"
        )

    reverted: list[int] = []
    for version in targets:
        mig = by_version[version]
        assert mig.down is not None  # guaranteed by the blocking check above
        with engine.begin() as conn:
            mig.down(conn)
            conn.execute(
                ne_migrations_table.delete().where(ne_migrations_table.c.version == version)
            )
        reverted.append(version)
    return reverted


# --- CLI -------------------------------------------------------------------


def _registry() -> tuple[Migration, ...]:
    from ne.cli.migrations import REGISTRY

    return REGISTRY


def _validated_registry() -> tuple[Migration, ...]:
    """``validate_registry`` wrapped for the CLI: a policy violation becomes a
    ``ClickException`` (clean, non-zero exit, no traceback) BEFORE any command
    creates an engine or touches a database."""
    try:
        return validate_registry(_registry())
    except MigrationPolicyError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group("migrate")
def migrate() -> None:
    """Cross-module migration runner (ordered, versioned, transactional, fail-closed)."""


@migrate.command("status")
@click.option("--db-url", "db_url", required=True, envvar="NE_MIGRATE_DB_URL")
def status_cmd(db_url: str) -> None:
    """Print applied vs. pending migrations."""
    registry = _validated_registry()
    engine = sa.create_engine(db_url)
    _check_drift(registry, engine)
    applied = set(applied_versions(engine))
    for mig in registry:
        state = "applied" if mig.version in applied else "pending"
        click.echo(f"{mig.version:>4}  {state:<8}  {mig.name}")


@migrate.command("up")
@click.option("--db-url", "db_url", required=True, envvar="NE_MIGRATE_DB_URL")
@click.option("--to", "to", type=int, default=None, help="Target version (default: latest).")
def up_cmd(db_url: str, to: int | None) -> None:
    """Apply pending migrations, in order, up to --to (default: latest)."""
    registry = _validated_registry()
    engine = sa.create_engine(db_url)
    applied_now = upgrade(engine, registry, to=to)
    if applied_now:
        click.echo(f"applied: {applied_now}")
    else:
        click.echo("no pending migrations (up to date)")


@migrate.command("down")
@click.option("--db-url", "db_url", required=True, envvar="NE_MIGRATE_DB_URL")
@click.option(
    "--to", "to", type=int, required=True, help="Revert down to (excluding) this version."
)
def down_cmd(db_url: str, to: int) -> None:
    """Revert applied migrations, in reverse order, down to --to."""
    registry = _validated_registry()
    engine = sa.create_engine(db_url)
    try:
        reverted = downgrade(engine, registry, to=to)
    except MigrationPolicyError as exc:
        raise click.ClickException(str(exc)) from exc
    if reverted:
        click.echo(f"reverted: {reverted}")
    else:
        click.echo("nothing to revert")
