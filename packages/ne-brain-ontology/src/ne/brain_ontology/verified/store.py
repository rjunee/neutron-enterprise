# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The governed, append-only verified-answer template store (NE-ONTOLOGY-FLYWHEEL).

The HITL continuous-improvement flywheel curates NEW/MODIFIED T3 verified-answer
templates through the NE-066 gate. On a steward APPLY, the approved template TOML
is persisted HERE — an append-only, ``config_hash``-tracked store beyond the
read-only boot directory — so a process restart re-loads it into the effective
:class:`~ne.brain_ontology.verified.templates.TemplateRegistry` and the T3 layer
improves WITHOUT a redeploy.

Import discipline (C3, brain-ontology-lane): this module imports ONLY
sqlalchemy + pydantic + stdlib + package-local errors — NEVER ``ne.flywheel`` /
``ne.audit`` / ``ne.runtime`` / ``ne.govern``. The governed apply ORCHESTRATION
(the NE-066 gate + the audit chain + the config_hash projection) lives in the
ne-api composition seam (``ne.api.verified_curation``), mirroring the
``ne.api.verified_tools`` precedent — because the merged ``.importlinter``
brain-ontology-lane contract forbids ``ne.brain_ontology`` importing those.

APPEND-ONLY is a hard invariant: the complete public mutation surface is a single
idempotent INSERT (:func:`append_verified_template`). No update/delete/edit
exists. A duplicate ``(template_id, version)`` with an IDENTICAL ``toml_sha256``
is idempotent (returns the existing row id); with a DIFFERENT sha it raises
:class:`~ne.brain_ontology.verified.errors.VerifiedTemplateConflictError`
(fail-loud — one body per slot).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from ne.brain_ontology.verified.errors import VerifiedTemplateConflictError

__all__ = [
    "StoredVerifiedTemplate",
    "VerifiedTemplateRecord",
    "append_verified_template",
    "ensure_verified_template_schema",
    "ne_verified_templates",
    "read_verified_templates",
    "verified_template_metadata",
]

# Its OWN MetaData — independent of every other package's, so
# ``ensure_verified_template_schema`` stays composable on ONE engine.
verified_template_metadata = sa.MetaData()

ne_verified_templates = sa.Table(
    "ne_verified_templates",
    verified_template_metadata,
    # BigInteger on Postgres (Identity); Integer on SQLite so it aliases the rowid
    # and autoincrements (SQLite only autoincrements INTEGER PRIMARY KEY).
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.Identity(),
        primary_key=True,
    ),
    sa.Column("template_id", sa.String, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("template_toml", sa.Text, nullable=False),
    sa.Column("toml_sha256", sa.String, nullable=False),
    sa.Column("proposal_id", sa.String, nullable=False),
    sa.Column("actor", sa.String, nullable=False),
    sa.Column("config_hash", sa.String, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index(
        "ux_ne_verified_templates_id_version",
        "template_id",
        "version",
        unique=True,
    ),
    sa.Index("ix_ne_verified_templates_template_id", "template_id"),
)


def ensure_verified_template_schema(engine: sa.Engine) -> None:
    """Create the verified-template store table if absent. Idempotent (``create_all``)."""
    verified_template_metadata.create_all(engine)


def _as_utc(dt: datetime) -> datetime:
    """Normalize a datetime to tz-aware UTC (SQLite reads back naive)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class VerifiedTemplateRecord(BaseModel):
    """The write-shape for one governed template row (append input)."""

    model_config = ConfigDict(frozen=True)

    template_id: str
    version: int
    template_toml: str
    toml_sha256: str
    proposal_id: str
    actor: str
    config_hash: str
    created_at: datetime


class StoredVerifiedTemplate(BaseModel):
    """A frozen, fully-materialized governed template row as stored + read back."""

    model_config = ConfigDict(frozen=True)

    id: int
    template_id: str
    version: int
    template_toml: str
    toml_sha256: str
    proposal_id: str
    actor: str
    config_hash: str
    created_at: datetime


def _row_to_stored(row: sa.Row[Any]) -> StoredVerifiedTemplate:
    return StoredVerifiedTemplate(
        id=int(row.id),
        template_id=str(row.template_id),
        version=int(row.version),
        template_toml=str(row.template_toml),
        toml_sha256=str(row.toml_sha256),
        proposal_id=str(row.proposal_id),
        actor=str(row.actor),
        config_hash=str(row.config_hash),
        created_at=_as_utc(row.created_at),
    )


def append_verified_template(conn: sa.Connection, record: VerifiedTemplateRecord) -> int:
    """Append ONE governed template row on the CALLER's connection; return its id.

    Runs on ``conn`` (not its own engine) so the ne-api apply orchestration
    composes the persist with the dual-appended audit event in ONE transaction.

    SAVEPOINT-idempotent on a duplicate ``(template_id, version)``: the INSERT
    runs in a nested transaction (mirroring ``SqlAlchemyProposalStore.append``);
    a unique-violation rolls back just the SAVEPOINT and the existing row is
    consulted — if its ``toml_sha256`` is IDENTICAL the existing id is returned
    (a crash-retry-safe re-apply), if DIFFERENT a
    :class:`VerifiedTemplateConflictError` is raised (one body per slot).
    """
    stmt = ne_verified_templates.insert().values(
        template_id=record.template_id,
        version=record.version,
        template_toml=record.template_toml,
        toml_sha256=record.toml_sha256,
        proposal_id=record.proposal_id,
        actor=record.actor,
        config_hash=record.config_hash,
        created_at=_as_utc(record.created_at),
    )
    try:
        with conn.begin_nested():
            result = conn.execute(stmt)
        pk = result.inserted_primary_key
        if pk is None:  # pragma: no cover - a single-row insert always yields a pk
            raise RuntimeError("verified-template insert returned no primary key")
        return int(pk[0])
    except IntegrityError:
        existing = conn.execute(
            sa.select(ne_verified_templates.c.id, ne_verified_templates.c.toml_sha256).where(
                sa.and_(
                    ne_verified_templates.c.template_id == record.template_id,
                    ne_verified_templates.c.version == record.version,
                )
            )
        ).one()
        if str(existing.toml_sha256) != record.toml_sha256:
            raise VerifiedTemplateConflictError(
                f"template {record.template_id!r}@v{record.version} already exists with a "
                f"different body (stored sha {existing.toml_sha256!r} != {record.toml_sha256!r}); "
                "re-propose at the next version (one body per (id, version) slot)"
            ) from None
        return int(existing.id)


def read_verified_templates(
    conn_or_engine: sa.Connection | sa.Engine,
) -> list[StoredVerifiedTemplate]:
    """Read every governed template row, ordered ``(template_id, version)``.

    Accepts either a live connection (inside the apply transaction, so the read
    sees the just-appended row) or an engine (boot-time load).
    """
    stmt = sa.select(ne_verified_templates).order_by(
        ne_verified_templates.c.template_id.asc(), ne_verified_templates.c.version.asc()
    )
    if isinstance(conn_or_engine, sa.Engine):
        with conn_or_engine.connect() as conn:
            rows = conn.execute(stmt).all()
    else:
        rows = conn_or_engine.execute(stmt).all()
    return [_row_to_stored(row) for row in rows]
