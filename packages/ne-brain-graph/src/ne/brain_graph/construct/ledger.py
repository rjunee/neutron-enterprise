# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``GraphSyncLedger`` — the durable 3-way idempotency + pending-edge ledger.

Over an injected SQLAlchemy engine (sqlite in tests, the graph's Postgres in
prod). Two tables, the ``IngestLedger`` discipline (spine.py:115):

* ``ne_graph_sync_ledger`` keyed ``(source_system, entity_class, key)`` with
  ``content_hash``, ``acl_hash``, ``status``, ``last_run_id``, ``reason``,
  ``updated_at`` — drives the spine's 3-way short-circuit (spine.py:66-89):
  same content + same acl + clean row → ``UNCHANGED``; same content + changed
  acl → ``ACL_STALE`` (cheap ``update_acl``, no property rewrite); else
  ``UPSERT``.
* ``ne_graph_pending_edges`` — an edge whose target node has no ledger row yet
  (a dangling FK) is parked here, NEVER a placeholder node; retried at end of run
  and on the next run.

Every SEEN mapped record touches ``last_run_id`` so a backfill prune can delete
exactly the source-deleted (untouched) rows in upsert→delete→ledger order.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

__all__ = ["GraphSyncLedger", "LedgerDecision"]


class LedgerDecision(StrEnum):
    UNCHANGED = "unchanged"
    ACL_STALE = "acl_stale"
    UPSERT = "upsert"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class GraphSyncLedger:
    """Durable sync ledger; idempotent ``create_all`` on construction."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._metadata = sa.MetaData()
        self._ledger = sa.Table(
            "ne_graph_sync_ledger",
            self._metadata,
            sa.Column("source_system", sa.String, primary_key=True),
            sa.Column("entity_class", sa.String, primary_key=True),
            sa.Column("key", sa.String, primary_key=True),
            sa.Column("content_hash", sa.String, nullable=False),
            sa.Column("acl_hash", sa.String, nullable=False),
            sa.Column("status", sa.String, nullable=False),
            sa.Column("last_run_id", sa.String, nullable=False),
            sa.Column("reason", sa.String, nullable=True),
            sa.Column("updated_at", sa.String, nullable=False),
        )
        self._pending = sa.Table(
            "ne_graph_pending_edges",
            self._metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("source_system", sa.String, nullable=False),
            sa.Column("key", sa.String, nullable=False),
            # DECISION-2: the parked edge's originating node key, so a changed FK
            # can purge the stale parked edge (``purge_pending_from``). The ledger
            # is a DERIVED, rebuildable index — this column needs no migration.
            sa.Column("from_key", sa.String, nullable=False),
            sa.Column("target_key", sa.String, nullable=False),
            sa.Column("origin_source_ref", sa.String, nullable=False),
            sa.Column("origin_snapshot_hash", sa.String(64), nullable=False),
            sa.Column("origin_entity", sa.JSON, nullable=False),
            sa.Column("props", sa.JSON, nullable=False),
            sa.Column("first_seen_run_id", sa.String, nullable=False),
            sa.UniqueConstraint("source_system", "key", name="uq_ne_graph_pending_edge"),
        )
        self._evidence_nodes = sa.Table(
            "ne_graph_evidence_nodes",
            self._metadata,
            sa.Column("source_system", sa.String, primary_key=True),
            sa.Column("key", sa.String, primary_key=True),
            sa.Column("source_ref", sa.String, nullable=False),
            sa.Column("source_snapshot_hash", sa.String(64), nullable=False),
            sa.Column("source_acl_hash", sa.String(64), nullable=False),
            sa.Column("entity_class", sa.String, nullable=False),
            sa.Column("display_name", sa.String, nullable=False),
            sa.Column("acl", sa.JSON, nullable=False),
            sa.Column("last_run_id", sa.String, nullable=False),
            sa.Column("updated_at", sa.String, nullable=False),
        )
        self._evidence_edges = sa.Table(
            "ne_graph_evidence_edges",
            self._metadata,
            sa.Column("source_system", sa.String, primary_key=True),
            sa.Column("key", sa.String, primary_key=True),
            sa.Column("edge_kind", sa.String, nullable=False),
            sa.Column("from_key", sa.String, nullable=False),
            sa.Column("to_key", sa.String, nullable=False),
            sa.Column("last_run_id", sa.String, nullable=False),
            sa.Column("updated_at", sa.String, nullable=False),
        )
        self._mutation_intents = sa.Table(
            "ne_graph_mutation_intents",
            self._metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.String, nullable=False, unique=True),
            sa.Column("source_system", sa.String, nullable=False),
            sa.Column("audit_event", sa.JSON, nullable=False),
            sa.Column("plan", sa.JSON, nullable=False),
            sa.Column("audited", sa.Boolean, nullable=False, default=False),
            sa.Column("applied", sa.Boolean, nullable=False, default=False),
            sa.Column("created_at", sa.String, nullable=False),
            sa.Column("applied_at", sa.String, nullable=True),
        )
        self._evidence_outbox = sa.Table(
            "ne_graph_evidence_outbox",
            self._metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.String, nullable=False, unique=True),
            sa.Column("audit_event", sa.JSON, nullable=False),
            sa.Column("evidence", sa.JSON, nullable=False),
            sa.Column("audited", sa.Boolean, nullable=False, default=False),
            sa.Column("delivered", sa.Boolean, nullable=False, default=False),
            sa.Column("created_at", sa.String, nullable=False),
            sa.Column("delivered_at", sa.String, nullable=True),
        )
        self._metadata.create_all(self._engine)
        self._upgrade_pending_edges()

    def _upgrade_pending_edges(self) -> None:
        """Self-heal pending edges that predate current origin evidence.

        ``create_all`` skips an already-existing table, so a DB created before
        DECISION-2/E2A keeps the old shape and every subsequent sync would fail on
        the current insert/delete. The pending table is a TRANSIENT, derived retry
        buffer (dangling FKs are re-parked from source on the next sync), so a
        legacy table missing any required origin column is migrated into the current
        shape and its originating ledger rows are dirtied for exact source replay.
        This preserves unresolved work without pretending missing immutable origin
        evidence can be reconstructed. The
        create-all seam owns package-local schema (outbox.py:190 precedent); the
        real migration runner is NE-069's. No-op on a freshly created table.
        """
        inspector = sa.inspect(self._engine)
        if not inspector.has_table(self._pending.name):
            return
        columns = {col["name"] for col in inspector.get_columns(self._pending.name)}
        required = {
            "from_key",
            "origin_source_ref",
            "origin_snapshot_hash",
            "origin_entity",
        }
        if required.issubset(columns) and any(
            constraint.get("name") == "uq_ne_graph_pending_edge"
            for constraint in inspector.get_unique_constraints(self._pending.name)
        ):
            return
        with self._engine.begin() as conn:
            rows = list(conn.execute(sa.text(f"SELECT * FROM {self._pending.name}")).mappings())
            migrated: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows:
                props = row.get("props", {})
                if isinstance(props, str):
                    try:
                        props = json.loads(props)
                    except (TypeError, ValueError):
                        props = {}
                if not isinstance(props, dict):
                    props = {}
                source_system = str(row.get("source_system", ""))
                key = str(row.get("key", props.get("key", "")))
                from_key = str(row.get("from_key", props.get("from_key", "")))
                if not source_system or not key or not from_key:
                    raise RuntimeError(
                        "legacy pending edge cannot be migrated without exact source/key/from_key"
                    )
                target_key = str(row.get("target_key", props.get("to_key", "")))
                if not target_key:
                    raise RuntimeError(
                        "legacy pending edge cannot be migrated without exact target_key"
                    )
                origin_entity = row.get("origin_entity", {}) or {}
                if isinstance(origin_entity, str):
                    try:
                        origin_entity = json.loads(origin_entity)
                    except (TypeError, ValueError):
                        origin_entity = {}
                if not isinstance(origin_entity, dict):
                    origin_entity = {}
                migrated[(source_system, key)] = {
                    "source_system": source_system,
                    "key": key,
                    "from_key": from_key,
                    "target_key": target_key,
                    "origin_source_ref": str(row.get("origin_source_ref", "")),
                    "origin_snapshot_hash": str(row.get("origin_snapshot_hash", "")),
                    "origin_entity": origin_entity,
                    "props": props,
                    "first_seen_run_id": str(row.get("first_seen_run_id", "legacy-migration")),
                }
                conn.execute(
                    self._ledger.update()
                    .where(
                        self._ledger.c.source_system == source_system,
                        self._ledger.c.key == from_key,
                    )
                    .values(
                        reason="pending edge requires exact source replay",
                        status="replay_required",
                        updated_at=_now(),
                    )
                )
            self._pending.drop(conn)
            self._pending.create(conn)
            if migrated:
                conn.execute(self._pending.insert(), list(migrated.values()))

    # -- 3-way decision -------------------------------------------------------

    def check(
        self, source_system: str, entity_class: str, key: str, content_hash: str, acl_hash: str
    ) -> LedgerDecision:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    self._ledger.c.content_hash,
                    self._ledger.c.acl_hash,
                    self._ledger.c.reason,
                ).where(
                    self._ledger.c.source_system == source_system,
                    self._ledger.c.entity_class == entity_class,
                    self._ledger.c.key == key,
                )
            ).one_or_none()
        if row is None:
            return LedgerDecision.UPSERT
        if row.reason is not None:
            return LedgerDecision.UPSERT
        if row.content_hash == content_hash:
            if row.acl_hash == acl_hash:
                return LedgerDecision.UNCHANGED
            return LedgerDecision.ACL_STALE
        return LedgerDecision.UPSERT

    def has_node(self, source_system: str, key: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(self._ledger.c.key).where(
                    self._ledger.c.source_system == source_system,
                    self._ledger.c.key == key,
                )
            ).one_or_none()
        return row is not None

    # -- state transitions ----------------------------------------------------

    def _upsert_row(
        self,
        source_system: str,
        entity_class: str,
        key: str,
        *,
        content_hash: str,
        acl_hash: str,
        status: str,
        run_id: str,
        reason: str | None,
    ) -> None:
        values = {
            "content_hash": content_hash,
            "acl_hash": acl_hash,
            "status": status,
            "last_run_id": run_id,
            "reason": reason,
            "updated_at": _now(),
        }
        with self._engine.begin() as conn:
            existing = conn.execute(
                sa.select(self._ledger.c.key).where(
                    self._ledger.c.source_system == source_system,
                    self._ledger.c.entity_class == entity_class,
                    self._ledger.c.key == key,
                )
            ).one_or_none()
            if existing is None:
                conn.execute(
                    self._ledger.insert().values(
                        source_system=source_system,
                        entity_class=entity_class,
                        key=key,
                        **values,
                    )
                )
            else:
                conn.execute(
                    self._ledger.update()
                    .where(
                        self._ledger.c.source_system == source_system,
                        self._ledger.c.entity_class == entity_class,
                        self._ledger.c.key == key,
                    )
                    .values(**values)
                )

    def mark_upserted(
        self,
        source_system: str,
        entity_class: str,
        key: str,
        *,
        content_hash: str,
        acl_hash: str,
        run_id: str,
    ) -> None:
        self._upsert_row(
            source_system,
            entity_class,
            key,
            content_hash=content_hash,
            acl_hash=acl_hash,
            status="upserted",
            run_id=run_id,
            reason=None,
        )

    def mark_acl_healed(
        self,
        source_system: str,
        entity_class: str,
        key: str,
        *,
        content_hash: str,
        acl_hash: str,
        run_id: str,
    ) -> None:
        self._upsert_row(
            source_system,
            entity_class,
            key,
            content_hash=content_hash,
            acl_hash=acl_hash,
            status="acl_healed",
            run_id=run_id,
            reason=None,
        )

    def touch(self, source_system: str, entity_class: str, key: str, *, run_id: str) -> None:
        """Mark a SEEN-but-UNCHANGED record as touched this run (prune safety)."""
        with self._engine.begin() as conn:
            conn.execute(
                self._ledger.update()
                .where(
                    self._ledger.c.source_system == source_system,
                    self._ledger.c.entity_class == entity_class,
                    self._ledger.c.key == key,
                )
                .values(last_run_id=run_id, updated_at=_now())
            )

    def mark_quarantined(
        self, source_system: str, entity_class: str, key: str, *, reason: str, run_id: str
    ) -> None:
        self._upsert_row(
            source_system,
            entity_class,
            key,
            content_hash="",
            acl_hash="",
            status="quarantined",
            run_id=run_id,
            reason=reason,
        )

    def record_node_evidence(
        self,
        source_system: str,
        key: str,
        *,
        source_ref: str,
        source_snapshot_hash: str,
        source_acl_hash: str,
        entity_class: str,
        display_name: str,
        acl: Sequence[str],
        run_id: str,
    ) -> None:
        """Upsert the exact source snapshot that most recently proved a node."""
        values = {
            "source_ref": source_ref,
            "source_snapshot_hash": source_snapshot_hash,
            "source_acl_hash": source_acl_hash,
            "entity_class": entity_class,
            "display_name": display_name,
            "acl": list(acl),
            "last_run_id": run_id,
            "updated_at": _now(),
        }
        with self._engine.begin() as conn:
            existing = conn.execute(
                sa.select(self._evidence_nodes.c.key).where(
                    self._evidence_nodes.c.source_system == source_system,
                    self._evidence_nodes.c.key == key,
                )
            ).one_or_none()
            if existing is None:
                conn.execute(
                    self._evidence_nodes.insert().values(
                        source_system=source_system,
                        key=key,
                        **values,
                    )
                )
            else:
                conn.execute(
                    self._evidence_nodes.update()
                    .where(
                        self._evidence_nodes.c.source_system == source_system,
                        self._evidence_nodes.c.key == key,
                    )
                    .values(**values)
                )

    def node_evidence(self, source_system: str, key: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(self._evidence_nodes).where(
                        self._evidence_nodes.c.source_system == source_system,
                        self._evidence_nodes.c.key == key,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else dict(row)

    def record_edge_evidence(
        self,
        source_system: str,
        *,
        key: str,
        edge_kind: str,
        from_key: str,
        to_key: str,
        run_id: str,
    ) -> None:
        """Record an edge only after the graph store accepted its exact upsert."""
        values = {
            "edge_kind": edge_kind,
            "from_key": from_key,
            "to_key": to_key,
            "last_run_id": run_id,
            "updated_at": _now(),
        }
        with self._engine.begin() as conn:
            existing = conn.execute(
                sa.select(self._evidence_edges.c.key).where(
                    self._evidence_edges.c.source_system == source_system,
                    self._evidence_edges.c.key == key,
                )
            ).one_or_none()
            if existing is None:
                conn.execute(
                    self._evidence_edges.insert().values(
                        source_system=source_system,
                        key=key,
                        **values,
                    )
                )
            else:
                conn.execute(
                    self._evidence_edges.update()
                    .where(
                        self._evidence_edges.c.source_system == source_system,
                        self._evidence_edges.c.key == key,
                    )
                    .values(**values)
                )

    def edge_evidence_for_nodes(
        self, source_system: str, keys: Sequence[str]
    ) -> list[dict[str, Any]]:
        if not keys:
            return []
        node_keys = list(keys)
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.select(self._evidence_edges).where(
                        self._evidence_edges.c.source_system == source_system,
                        sa.or_(
                            self._evidence_edges.c.from_key.in_(node_keys),
                            self._evidence_edges.c.to_key.in_(node_keys),
                        ),
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def purge_edge_evidence_from(
        self, source_system: str, from_key: str, keep_keys: Sequence[str]
    ) -> None:
        keep = list(keep_keys)
        with self._engine.begin() as conn:
            stmt = self._evidence_edges.delete().where(
                self._evidence_edges.c.source_system == source_system,
                self._evidence_edges.c.from_key == from_key,
            )
            if keep:
                stmt = stmt.where(self._evidence_edges.c.key.notin_(keep))
            conn.execute(stmt)

    # -- prune ----------------------------------------------------------------

    def untouched_keys(self, source_system: str, run_id: str) -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(self._ledger.c.key).where(
                    self._ledger.c.source_system == source_system,
                    self._ledger.c.last_run_id != run_id,
                    self._ledger.c.reason.is_(None),
                )
            ).all()
        return [r.key for r in rows]

    def delete_keys(self, source_system: str, keys: Sequence[str]) -> None:
        if not keys:
            return
        with self._engine.begin() as conn:
            conn.execute(
                self._ledger.delete().where(
                    self._ledger.c.source_system == source_system,
                    self._ledger.c.key.in_(list(keys)),
                )
            )
            conn.execute(
                self._evidence_nodes.delete().where(
                    self._evidence_nodes.c.source_system == source_system,
                    self._evidence_nodes.c.key.in_(list(keys)),
                )
            )
            conn.execute(
                self._evidence_edges.delete().where(
                    self._evidence_edges.c.source_system == source_system,
                    sa.or_(
                        self._evidence_edges.c.from_key.in_(list(keys)),
                        self._evidence_edges.c.to_key.in_(list(keys)),
                    ),
                )
            )
            conn.execute(
                self._pending.delete().where(
                    self._pending.c.source_system == source_system,
                    sa.or_(
                        self._pending.c.from_key.in_(list(keys)),
                        self._pending.c.target_key.in_(list(keys)),
                    ),
                )
            )

    # -- pending edges --------------------------------------------------------

    def add_pending_edge(
        self,
        source_system: str,
        props: Mapping[str, Any],
        target_key: str,
        run_id: str,
        *,
        origin_source_ref: str = "",
        origin_snapshot_hash: str = "",
        origin_entity: Mapping[str, Any] | None = None,
    ) -> None:
        values = {
            "from_key": str(props["from_key"]),
            "target_key": target_key,
            "origin_source_ref": origin_source_ref,
            "origin_snapshot_hash": origin_snapshot_hash,
            "origin_entity": dict(origin_entity or {}),
            "props": dict(props),
            "first_seen_run_id": run_id,
        }
        with self._engine.begin() as conn:
            key = str(props["key"])
            existing = conn.execute(
                sa.select(self._pending.c.id).where(
                    self._pending.c.source_system == source_system,
                    self._pending.c.key == key,
                )
            ).one_or_none()
            if existing is None:
                conn.execute(
                    self._pending.insert().values(
                        source_system=source_system,
                        key=key,
                        **values,
                    )
                )
            else:
                conn.execute(
                    self._pending.update().where(self._pending.c.id == existing.id).values(**values)
                )

    def purge_pending_from(
        self, source_system: str, from_key: str, keep_keys: Sequence[str]
    ) -> None:
        """Delete parked edges out of ``from_key`` whose key is not in ``keep_keys``.

        DECISION-2: when the FK that parked an edge changes (or is removed), the
        stale parked edge for that source node is purged so it can never be
        retried into the graph.
        """
        keep = list(keep_keys)
        with self._engine.begin() as conn:
            stmt = self._pending.delete().where(
                self._pending.c.source_system == source_system,
                self._pending.c.from_key == from_key,
            )
            if keep:
                stmt = stmt.where(self._pending.c.key.notin_(keep))
            conn.execute(stmt)

    def take_pending_edges(self, source_system: str) -> list[tuple[int, dict[str, Any], str]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    self._pending.c.id, self._pending.c.props, self._pending.c.target_key
                ).where(self._pending.c.source_system == source_system)
            ).all()
        return [(r.id, dict(r.props), r.target_key) for r in rows]

    def take_pending_edges_with_origin(
        self, source_system: str
    ) -> list[tuple[int, dict[str, Any], str, dict[str, Any]]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    self._pending.c.id,
                    self._pending.c.props,
                    self._pending.c.target_key,
                    self._pending.c.origin_entity,
                ).where(self._pending.c.source_system == source_system)
            ).all()
        return [
            (
                int(row.id),
                dict(row.props),
                str(row.target_key),
                dict(row.origin_entity),
            )
            for row in rows
        ]

    def delete_pending_edge(self, pending_id: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(self._pending.delete().where(self._pending.c.id == pending_id))

    def pending_edge_count(self, source_system: str) -> int:
        with self._engine.connect() as conn:
            return int(
                conn.scalar(
                    sa.select(sa.func.count())
                    .select_from(self._pending)
                    .where(self._pending.c.source_system == source_system)
                )
                or 0
            )

    # -- durable pre-write mutation intents ---------------------------------

    def enqueue_mutation_intent(
        self,
        event_id: str,
        *,
        source_system: str,
        audit_event: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> int:
        """Persist an exact replay plan before its first graph-store mutation."""
        with self._engine.begin() as conn:
            existing = conn.execute(
                sa.select(self._mutation_intents.c.id).where(
                    self._mutation_intents.c.event_id == event_id
                )
            ).one_or_none()
            if existing is not None:
                return int(existing.id)
            result = conn.execute(
                self._mutation_intents.insert().values(
                    event_id=event_id,
                    source_system=source_system,
                    audit_event=dict(audit_event),
                    plan=dict(plan),
                    audited=False,
                    applied=False,
                    created_at=_now(),
                )
            )
            inserted_primary_key = result.inserted_primary_key
            if inserted_primary_key is None:
                raise RuntimeError("graph mutation intent insert returned no identity")
            primary_key = inserted_primary_key[0]
            if primary_key is None:
                raise RuntimeError("graph mutation intent insert returned no identity")
            return int(primary_key)

    def pending_mutation_audits(self, source_system: str) -> list[tuple[int, dict[str, Any]]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(self._mutation_intents.c.id, self._mutation_intents.c.audit_event)
                .where(
                    self._mutation_intents.c.source_system == source_system,
                    self._mutation_intents.c.audited.is_(False),
                )
                .order_by(self._mutation_intents.c.id)
            ).all()
        return [(int(row.id), dict(row.audit_event)) for row in rows]

    def mark_mutation_audited(self, intent_id: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                self._mutation_intents.update()
                .where(self._mutation_intents.c.id == intent_id)
                .values(audited=True)
            )

    def pending_mutation_plans(self, source_system: str) -> list[tuple[int, dict[str, Any]]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(self._mutation_intents.c.id, self._mutation_intents.c.plan)
                .where(
                    self._mutation_intents.c.source_system == source_system,
                    self._mutation_intents.c.audited.is_(True),
                    self._mutation_intents.c.applied.is_(False),
                )
                .order_by(self._mutation_intents.c.id)
            ).all()
        return [(int(row.id), dict(row.plan)) for row in rows]

    def mark_mutation_applied(self, intent_id: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                self._mutation_intents.update()
                .where(
                    self._mutation_intents.c.id == intent_id,
                    self._mutation_intents.c.audited.is_(True),
                )
                .values(applied=True, applied_at=_now())
            )

    def pending_mutation_count(self) -> int:
        with self._engine.connect() as conn:
            return int(
                conn.scalar(
                    sa.select(sa.func.count())
                    .select_from(self._mutation_intents)
                    .where(self._mutation_intents.c.applied.is_(False))
                )
                or 0
            )

    # -- durable audited evidence outbox ------------------------------------

    def enqueue_evidence(
        self,
        event_id: str,
        *,
        audit_event: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> int:
        with self._engine.begin() as conn:
            existing = conn.execute(
                sa.select(self._evidence_outbox.c.id).where(
                    self._evidence_outbox.c.event_id == event_id
                )
            ).one_or_none()
            if existing is not None:
                return int(existing.id)
            result = conn.execute(
                self._evidence_outbox.insert().values(
                    event_id=event_id,
                    audit_event=dict(audit_event),
                    evidence=dict(evidence),
                    audited=False,
                    delivered=False,
                    created_at=_now(),
                )
            )
            inserted_primary_key = result.inserted_primary_key
            if inserted_primary_key is None:
                raise RuntimeError("graph evidence outbox insert returned no identity")
            primary_key = inserted_primary_key[0]
            if primary_key is None:
                raise RuntimeError("graph evidence outbox insert returned no identity")
            return int(primary_key)

    def pending_evidence_audits(self) -> list[tuple[int, dict[str, Any]]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(self._evidence_outbox.c.id, self._evidence_outbox.c.audit_event)
                .where(self._evidence_outbox.c.audited.is_(False))
                .order_by(self._evidence_outbox.c.id)
            ).all()
        return [(int(row.id), dict(row.audit_event)) for row in rows]

    def mark_evidence_audited(self, outbox_id: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                self._evidence_outbox.update()
                .where(self._evidence_outbox.c.id == outbox_id)
                .values(audited=True)
            )

    def pending_evidence_deliveries(self) -> list[tuple[int, dict[str, Any]]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(self._evidence_outbox.c.id, self._evidence_outbox.c.evidence)
                .where(
                    self._evidence_outbox.c.audited.is_(True),
                    self._evidence_outbox.c.delivered.is_(False),
                )
                .order_by(self._evidence_outbox.c.id)
            ).all()
        return [(int(row.id), dict(row.evidence)) for row in rows]

    def mark_evidence_delivered(self, outbox_id: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                self._evidence_outbox.update()
                .where(
                    self._evidence_outbox.c.id == outbox_id,
                    self._evidence_outbox.c.audited.is_(True),
                )
                .values(delivered=True, delivered_at=_now())
            )

    def pending_evidence_count(self) -> int:
        with self._engine.connect() as conn:
            return int(
                conn.scalar(
                    sa.select(sa.func.count())
                    .select_from(self._evidence_outbox)
                    .where(self._evidence_outbox.c.delivered.is_(False))
                )
                or 0
            )
