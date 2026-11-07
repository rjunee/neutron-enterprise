# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The Skill Forge registry — append-only writes, folded reads, hash-pinned serve.

Schema style mirrors ``ne.flywheel.proposals`` (BigInteger/Identity PKs with a
SQLite Integer variant so the CI SQLite store autoincrements; ``sa.JSON`` with a
Postgres ``JSONB`` variant; tz-aware ``DateTime``). Everything mutable-looking is
a FOLD at read time (current version = max version row; status = last lifecycle
event; usage = a count over invocation rows) — the NE-066 review-log discipline.

SAME-ENGINE REQUIREMENT (plan §2.2, the review.py:74-77 lesson): the caller MUST
run ``ensure_skills_schema`` on the SAME engine/database as the flywheel review
service in ne-api (and the ne-worker reconcile), or NE-SKILL-1b's one-transaction
apply seam cannot hold. ne-api enables skills exactly when the flywheel signal DB
is configured, and builds this registry over THAT engine.

THE ONE HASH-CHECK CHOKEPOINT (R-28): :meth:`verified_current_packs`,
:meth:`get_current` and :meth:`verified_pinned_pack` reconstruct a ``SkillPack``
from the stored row columns and compare its computed ``content_hash`` against the
stored ``content_hash`` column BEFORE the pack's ``acl_labels`` are ever trusted.
A DB-tampered row (labels widened, body changed) fails the check and is EXCLUDED
(verified_current_packs) or returned as ``None`` (get_current /
verified_pinned_pack, indistinguishable from absent). Search, view, and the
active-set computer ALL read through these three methods, so a tampered row never
leaks a restricted pack's name/description on ANY read path.

EXACT PINS: :meth:`verified_pinned_pack` is the CURRENT-VERSION-FREE read. The two
older methods fold to ``max(version)``, so a rollover silently moves what they
serve; a Run frozen against an Agent's declared ``(skill_id, version,
content_hash)`` must keep serving the version it froze. It therefore selects the
EXACT ``(name, version)`` row, checks the DECLARED pin hash against the STORED
column (is this the artifact the Agent pinned?) and then runs the R-28 row
integrity check (is the stored row still self-consistent?) — two independent
refusals which, with retirement and absence, all collapse to the same ``None``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from ne.contracts.skills import InvocationPage, SkillInvocation, SkillPack, SkillSource

_LOG = logging.getLogger(__name__)

#: Lifecycle events whose LAST occurrence folds a name to a status.
_ACTIVE_EVENTS = frozenset({"registered", "restored"})
_QUARANTINED_EVENT = "quarantined"
_RETIRED_EVENT = "retired"


class SkillPin(NamedTuple):
    """An EXACT, Run-frozen Skill pin: name + version + DECLARED content hash.

    The value shape an Agent definition declares (``SkillGrant``) and a Run
    freezes. All three components are load-bearing on every resolution: a pin
    naming the right skill at the wrong version, or the right version under a
    stale hash, resolves to nothing.
    """

    name: str
    version: int
    content_hash: str


skills_metadata = sa.MetaData()


def _big_id() -> sa.Column[int]:
    return sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.Identity(),
        primary_key=True,
    )


def _json() -> sa.types.TypeEngine[Any]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


ne_skills = sa.Table(
    "ne_skills",
    skills_metadata,
    _big_id(),
    sa.Column("name", sa.String, nullable=False, unique=True),
    sa.Column("source", sa.String, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

ne_skill_versions = sa.Table(
    "ne_skill_versions",
    skills_metadata,
    _big_id(),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("triggers", _json(), nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("category", sa.String, nullable=False, default=""),
    sa.Column("tags", _json(), nullable=False),
    sa.Column("requires_tools", _json(), nullable=False),
    sa.Column("platforms", _json(), nullable=False),
    sa.Column("acl_labels", _json(), nullable=False),
    sa.Column("content_hash", sa.String, nullable=False),
    sa.Column("routing_hash", sa.String, nullable=False),
    sa.Column("provenance", _json(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("name", "version", name="uq_ne_skill_versions_name_version"),
    sa.Index("ix_ne_skill_versions_name", "name"),
)

ne_skill_lifecycle = sa.Table(
    "ne_skill_lifecycle",
    skills_metadata,
    _big_id(),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("event", sa.String, nullable=False),
    sa.Column("actor", sa.String, nullable=False),
    sa.Column("reason", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_ne_skill_lifecycle_name", "name"),
)

ne_skill_invocations = sa.Table(
    "ne_skill_invocations",
    skills_metadata,
    _big_id(),
    sa.Column("skill_name", sa.String, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("principal_id", sa.String, nullable=False),
    sa.Column("session_id", sa.String, nullable=False, default=""),
    sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("run_succeeded", sa.Boolean, nullable=True),
    sa.Index("ix_ne_skill_invocations_skill_name", "skill_name"),
)

ne_skill_surfaces = sa.Table(
    "ne_skill_surfaces",
    skills_metadata,
    _big_id(),
    sa.Column("surface_key", sa.String, nullable=False, unique=True),
    sa.Column("index_text", sa.Text, nullable=False),
    sa.Column("pack_names", _json(), nullable=False),
    sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
)

# -- NE-SKILL-4 rot layer -----------------------------------------------------
# The materialized usage rollup (§6.5): the telemetry fold folds
# ``ne_skill_invocations`` into per-skill counters here so rot/retirement read a
# bounded O(distinct-skills) table, never a live aggregate over the unbounded,
# append-only invocation log. (R-26: the view handler stays the SOLE writer of
# ``ne_skill_invocations``; this table is a fold of it, not a second source.)
ne_skill_usage = sa.Table(
    "ne_skill_usage",
    skills_metadata,
    _big_id(),
    sa.Column("skill_name", sa.String, nullable=False, unique=True),
    sa.Column("fired_count", sa.Integer, nullable=False, default=0),
    sa.Column("success_count", sa.Integer, nullable=False, default=0),
    sa.Column("fail_count", sa.Integer, nullable=False, default=0),
    sa.Column("unknown_count", sa.Integer, nullable=False, default=0),
    sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

#: Per-consumer fold cursor (opaque monotone id watermark over a source table).
ne_skill_cursors = sa.Table(
    "ne_skill_cursors",
    skills_metadata,
    _big_id(),
    sa.Column("consumer", sa.String, nullable=False, unique=True),
    sa.Column(
        "last_id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        nullable=False,
        default=0,
    ),
)

#: One persisted Skill Gardener run report (the full JSON the digest route reads).
ne_skill_gardener_runs = sa.Table(
    "ne_skill_gardener_runs",
    skills_metadata,
    _big_id(),
    sa.Column("report", _json(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_ne_skill_gardener_runs_created", "created_at"),
)


def ensure_skills_schema(engine: sa.Engine) -> None:
    """Create the skill tables if absent. Idempotent (``create_all``)."""
    skills_metadata.create_all(engine)


def _as_utc(value: datetime) -> datetime:
    """Normalize a datetime to tz-aware UTC (SQLite reads back naive)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


class LibraryRow:
    """A steward library-list row (INCLUDING quarantined + version-less names)."""

    __slots__ = (
        "acl_labels",
        "content_hash",
        "current_version",
        "description",
        "fired_count",
        "last_fired_at",
        "name",
        "source",
        "status",
        "tampered",
    )

    def __init__(
        self,
        *,
        name: str,
        source: str,
        status: str,
        current_version: int | None,
        description: str,
        acl_labels: tuple[str, ...],
        content_hash: str,
        fired_count: int,
        last_fired_at: datetime | None,
        tampered: bool,
    ) -> None:
        self.name = name
        self.source = source
        self.status = status
        self.current_version = current_version
        self.description = description
        self.acl_labels = acl_labels
        self.content_hash = content_hash
        self.fired_count = fired_count
        self.last_fired_at = last_fired_at
        self.tampered = tampered


def _consistent_index(index_text: str, pack_names: Sequence[str]) -> str:
    """Enforce the surface invariant: NO packs ⇒ NO index text.

    ONE DIRECTION ONLY, and deliberately so. ``ne_skill_surfaces`` stores one fact
    in two representations — the ordered ``pack_names`` and the rendered
    ``index_text``. This function enforces the EMPTY case of their agreement and
    nothing more: an empty ``pack_names`` forces ``index_text`` to ``""``. It does
    NOT check that a non-empty ``index_text`` renders exactly ``pack_names`` (that
    would require re-rendering here, which would need the packs themselves, not
    their names), so a caller that writes a stale or hand-built index alongside a
    correct name list is passed through unchanged. The non-empty case is instead
    the callers' obligation, asserted at the substrate seam by
    ``test_registry.py``'s ``index_text == render_index(...)`` equality controls.

    ``render_index`` already returns ``""`` for an empty set, but the two WITHDRAW
    paths (:meth:`SqlAlchemySkillRegistry.retire` and
    ``PydanticAiSkillSubstrate.withdraw``) do not re-render: they line-filter the
    stored text, dropping only ``- {name} (`` entries and never the header. Emptying
    a surface that way would leave a bare
    "Available skills (call read.skills.view …):" header behind — a DANGLING INDEX
    that advertises a loadable Skill which no longer exists, the exact shape plan
    §2.5 refuses, and a surface that no longer equals what a fresh composition
    renders.

    This lives at the STORE rather than in each withdrawer on purpose. Both
    substrate write paths (``materialize`` and ``withdraw``) funnel through
    :meth:`upsert_surface`, and ``retire`` is the only writer that bypasses it, so
    normalizing in those two places covers every writer — including the ones in
    other modules — instead of relying on each caller to remember. An invariant
    enforced in some writers and exempted in others passes its own tests and then
    fails at a distance.
    """
    return index_text if pack_names else ""


class SqlAlchemySkillRegistry:
    """SQL-core registry over the skills schema (ALL methods sync)."""

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    # -- writes (append-only) ---------------------------------------------

    def register_pack(self, pack: SkillPack, *, origin: str) -> None:
        """Insert the ``ne_skills`` row (if absent) + a version + a ``registered``
        lifecycle event."""
        now = _now()
        with self._engine.begin() as conn:
            self._ensure_skill_row(conn, pack.name, pack.source, now)
            self._insert_version(conn, pack, origin=origin, now=now)
            self._insert_lifecycle(
                conn, pack.name, "registered", actor="system:register", reason="", now=now
            )

    def append_version(self, pack: SkillPack, *, origin: str) -> None:
        """Append a new version row (edit-wins reconcile / 1b apply)."""
        now = _now()
        with self._engine.begin() as conn:
            self._ensure_skill_row(conn, pack.name, pack.source, now)
            self._insert_version(conn, pack, origin=origin, now=now)

    def append_lifecycle(self, name: str, event: str, *, actor: str, reason: str = "") -> None:
        now = _now()
        with self._engine.begin() as conn:
            self._insert_lifecycle(conn, name, event, actor=actor, reason=reason, now=now)

    def append_invocation(self, inv: SkillInvocation) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                ne_skill_invocations.insert().values(
                    skill_name=inv.skill_name,
                    version=inv.version,
                    principal_id=inv.principal_id,
                    session_id=inv.session_id,
                    fired_at=inv.fired_at,
                    run_succeeded=inv.run_succeeded,
                )
            )

    def register_quarantined(
        self, name: str, source: str, *, reason: str, actor: str = "system:reconcile"
    ) -> None:
        """Record a name (no version) as quarantined — invalid or pending-forge.

        Surfaced in the library list, never active. Idempotent on the name row.
        """
        now = _now()
        with self._engine.begin() as conn:
            self._ensure_skill_row(conn, name, source, now)
            self._insert_lifecycle(
                conn, name, _QUARANTINED_EVENT, actor=actor, reason=reason, now=now
            )

    # -- conn-level primitives (the NE-SKILL-1b one-transaction apply seam) ----

    def insert_parent_strict(self, conn: sa.Connection, name: str, source: str) -> None:
        """Insert the ``ne_skills`` parent row on ``conn``; RAISE if it exists.

        The strict (never ON-CONFLICT-ignore) variant the apply seam uses for a
        NEW ``skill_candidate``: the ``(name)`` uniqueness is the concurrent
        first-registration backstop. A duplicate name (a racer already inserted,
        or an update mis-routed as a candidate) raises so the apply transaction —
        including ``record_apply`` — rolls back. Runs in a SAVEPOINT so the
        caller's transaction survives to surface the error.
        """
        try:
            with conn.begin_nested():
                conn.execute(ne_skills.insert().values(name=name, source=source, created_at=_now()))
        except IntegrityError as exc:
            raise ValueError(f"skill {name!r} already has a parent row") from exc

    def assert_parent_exists(self, conn: sa.Connection, name: str) -> None:
        """Assert the ``ne_skills`` parent row exists on ``conn`` (update path)."""
        exists = conn.execute(sa.select(ne_skills.c.id).where(ne_skills.c.name == name)).first()
        if exists is None:
            raise ValueError(f"skill {name!r} has no parent row (cannot update a missing skill)")

    def insert_version_on(self, conn: sa.Connection, pack: SkillPack, *, origin: str) -> None:
        """Append a ``ne_skill_versions`` row on ``conn``.

        The ``(name, version)`` unique index is the concurrent-apply backstop: a
        losing racer's whole transaction (incl. ``record_apply``) rolls back on
        the IntegrityError.
        """
        self._insert_version(conn, pack, origin=origin, now=_now())

    def insert_lifecycle_on(
        self, conn: sa.Connection, name: str, event: str, *, actor: str, reason: str = ""
    ) -> None:
        """Append a ``ne_skill_lifecycle`` row on ``conn``."""
        self._insert_lifecycle(conn, name, event, actor=actor, reason=reason, now=_now())

    def name_exists(self, name: str) -> bool:
        """True if a ``ne_skills`` parent row exists for ``name``."""
        with self._engine.connect() as conn:
            return (
                conn.execute(sa.select(ne_skills.c.id).where(ne_skills.c.name == name)).first()
                is not None
            )

    def _ensure_skill_row(self, conn: sa.Connection, name: str, source: str, now: datetime) -> None:
        exists = conn.execute(sa.select(ne_skills.c.id).where(ne_skills.c.name == name)).first()
        if exists is None:
            conn.execute(ne_skills.insert().values(name=name, source=source, created_at=now))

    def _insert_version(
        self, conn: sa.Connection, pack: SkillPack, *, origin: str, now: datetime
    ) -> None:
        provenance = dict(pack.provenance) | {"origin": origin}
        conn.execute(
            ne_skill_versions.insert().values(
                name=pack.name,
                version=pack.version,
                description=pack.description,
                triggers=list(pack.triggers),
                body=pack.body,
                category=pack.category,
                tags=list(pack.tags),
                requires_tools=list(pack.requires_tools),
                platforms=list(pack.platforms),
                acl_labels=list(pack.acl_labels),
                content_hash=pack.content_hash,
                routing_hash=pack.routing_hash,
                provenance=provenance,
                created_at=now,
            )
        )

    def _insert_lifecycle(
        self,
        conn: sa.Connection,
        name: str,
        event: str,
        *,
        actor: str,
        reason: str,
        now: datetime,
    ) -> None:
        conn.execute(
            ne_skill_lifecycle.insert().values(
                name=name, event=event, actor=actor, reason=reason, created_at=now
            )
        )

    # -- folded reads -----------------------------------------------------

    def _status_map(self, conn: sa.Connection) -> dict[str, str]:
        """Fold each name to its LAST lifecycle event's status (default active)."""
        rows = conn.execute(
            sa.select(
                ne_skill_lifecycle.c.name,
                ne_skill_lifecycle.c.event,
                ne_skill_lifecycle.c.id,
            ).order_by(ne_skill_lifecycle.c.id)
        ).all()
        last_event: dict[str, str] = {}
        for name, event, _id in rows:
            last_event[name] = event
        status: dict[str, str] = {}
        for name, event in last_event.items():
            if event in _ACTIVE_EVENTS:
                status[name] = "active"
            elif event == _QUARANTINED_EVENT:
                status[name] = "quarantined"
            elif event == _RETIRED_EVENT:
                status[name] = "retired"
            else:
                status[name] = "active"
        return status

    def current_status(self, name: str) -> str:
        """The folded status of ``name`` (last lifecycle event; default ``active``).

        Used by the reconciler to detect a currently-``quarantined`` pack that a
        repaired bundle file must be ``restored`` back to active (Codex P1).
        """
        with self._engine.begin() as conn:
            return self._status_map(conn).get(name, "active")

    def _current_version_rows(self, conn: sa.Connection) -> dict[str, sa.Row[Any]]:
        """Return the MAX-version row per name (the current version)."""
        rows = conn.execute(
            sa.select(ne_skill_versions).order_by(
                ne_skill_versions.c.name, ne_skill_versions.c.version
            )
        ).all()
        current: dict[str, sa.Row[Any]] = {}
        for row in rows:
            current[row.name] = row  # last wins == max version (ordered asc)
        return current

    def verified_current_packs(self) -> tuple[tuple[SkillPack, ...], tuple[str, ...]]:
        """THE hash-check chokepoint (R-28): active packs whose stored content_hash
        matches the reconstructed pack; plus the names whose rows are tampered.

        A row is TAMPERED when the reconstructed ``SkillPack.content_hash`` differs
        from the stored ``content_hash`` column — such a row is EXCLUDED from the
        first tuple and reported in the second (a logged warning). Only ACTIVE-status
        names are candidates.
        """
        with self._engine.begin() as conn:
            status = self._status_map(conn)
            current = self._current_version_rows(conn)
            sources = self._source_map(conn)
        verified: list[SkillPack] = []
        tampered: list[str] = []
        for name, row in current.items():
            if status.get(name, "active") != "active":
                continue
            pack = self._reconstruct(name, row, sources)
            if pack.content_hash != row.content_hash:
                _LOG.warning(
                    "skill %r current version content_hash mismatch — refusing "
                    "(tampered row; excluded from ALL read paths)",
                    name,
                )
                tampered.append(name)
                continue
            verified.append(pack)
        return tuple(verified), tuple(tampered)

    def get_current(self, name: str) -> SkillPack | None:
        """The current version of ``name`` IFF active + hash-verified, else ``None``.

        A tampered row (hash mismatch) returns ``None`` + a logged warning —
        indistinguishable from absent (no oracle).
        """
        with self._engine.begin() as conn:
            status = self._status_map(conn)
            if status.get(name, "active") != "active":
                return None
            row = conn.execute(
                sa.select(ne_skill_versions)
                .where(ne_skill_versions.c.name == name)
                .order_by(ne_skill_versions.c.version.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            sources = self._source_map(conn)
        pack = self._reconstruct(name, row, sources)
        if pack.content_hash != row.content_hash:
            _LOG.warning("skill %r content_hash mismatch on get_current — refusing", name)
            return None
        return pack

    def verified_pinned_pack(self, pin: SkillPin) -> SkillPack | None:
        """The EXACT pinned pack IFF active, pin-matching AND hash-verified, else ``None``.

        Unlike :meth:`get_current` this NEVER folds to ``max(version)``: it reads the
        exact ``(name, version)`` row, so a later version appended by the reconciler
        or the forge cannot displace what a Run froze.

        Four independent refusals — retired/quarantined name, absent name/version,
        DECLARED-pin mismatch (``row.content_hash != pin.content_hash``: the stored
        artifact is not the one the Agent pinned), and the R-28 row-integrity
        mismatch (``pack.content_hash != row.content_hash``: the stored row was
        tampered with) — ALL return the same bare ``None`` with no distinguishing
        signal, so a caller cannot use this method as an existence oracle. The two
        hash comparisons are deliberately against DIFFERENT references and neither
        subsumes the other: a raw-SQL row edit that leaves the stored hash column
        alone passes the pin check and is caught only by the integrity check, while
        a stale/forged declared hash passes the integrity check and is caught only
        by the pin check.
        """
        with self._engine.begin() as conn:
            status = self._status_map(conn)
            if status.get(pin.name, "active") != "active":
                return None
            row = conn.execute(
                sa.select(ne_skill_versions).where(
                    ne_skill_versions.c.name == pin.name,
                    ne_skill_versions.c.version == pin.version,
                )
            ).first()
            if row is None:
                return None
            sources = self._source_map(conn)
        if row.content_hash != pin.content_hash:
            _LOG.warning(
                "skill %r v%s stored content_hash does not match the declared pin — refusing",
                pin.name,
                pin.version,
            )
            return None
        pack = self._reconstruct(pin.name, row, sources)
        if pack.content_hash != row.content_hash:
            _LOG.warning(
                "skill %r v%s content_hash mismatch on verified_pinned_pack — refusing "
                "(tampered row; excluded from ALL read paths)",
                pin.name,
                pin.version,
            )
            return None
        return pack

    def _source_map(self, conn: sa.Connection) -> dict[str, str]:
        rows = conn.execute(sa.select(ne_skills.c.name, ne_skills.c.source)).all()
        return {name: source for name, source in rows}

    def _reconstruct(self, name: str, row: sa.Row[Any], sources: Mapping[str, str]) -> SkillPack:
        return SkillPack(
            name=name,
            version=row.version,
            description=row.description,
            triggers=tuple(row.triggers),
            body=row.body,
            source=SkillSource(sources.get(name, "bundled")),
            category=row.category or "",
            tags=tuple(row.tags),
            requires_tools=tuple(row.requires_tools),
            platforms=tuple(row.platforms),
            acl_labels=tuple(row.acl_labels),
            provenance=dict(row.provenance),
        )

    def current_version_of(self, name: str) -> int | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(sa.func.max(ne_skill_versions.c.version)).where(
                    ne_skill_versions.c.name == name
                )
            ).first()
        return int(row[0]) if row is not None and row[0] is not None else None

    def current_version_meta(self, name: str) -> dict[str, Any] | None:
        """The current version's (content_hash, provenance, created_at) — or None.

        Used by the reconciler for the edit-wins decision (it reads the STORED
        content_hash regardless of tamper — the tamper refusal is a read-path
        concern, not a reconcile concern)."""
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(
                    ne_skill_versions.c.version,
                    ne_skill_versions.c.content_hash,
                    ne_skill_versions.c.provenance,
                    ne_skill_versions.c.created_at,
                )
                .where(ne_skill_versions.c.name == name)
                .order_by(ne_skill_versions.c.version.desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        return {
            "version": int(row.version),
            "content_hash": row.content_hash,
            "provenance": dict(row.provenance),
            "created_at": _as_utc(row.created_at),
        }

    def last_lifecycle_after(self, name: str, event: str, *, after: datetime) -> bool:
        """True if a lifecycle ``event`` for ``name`` postdates ``after``."""
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(sa.func.max(ne_skill_lifecycle.c.created_at)).where(
                    ne_skill_lifecycle.c.name == name,
                    ne_skill_lifecycle.c.event == event,
                )
            ).first()
        if row is None or row[0] is None:
            return False
        return _as_utc(row[0]) > _as_utc(after)

    def usage_counts(self) -> Mapping[str, int]:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.select(
                    ne_skill_invocations.c.skill_name,
                    sa.func.count().label("n"),
                ).group_by(ne_skill_invocations.c.skill_name)
            ).all()
        return {name: int(n) for name, n in rows}

    def library_rows(self) -> tuple[LibraryRow, ...]:
        """Steward list: ALL names (incl. quarantined + version-less)."""
        with self._engine.begin() as conn:
            names = conn.execute(
                sa.select(ne_skills.c.name, ne_skills.c.source).order_by(ne_skills.c.name)
            ).all()
            status = self._status_map(conn)
            current = self._current_version_rows(conn)
            fired = conn.execute(
                sa.select(
                    ne_skill_invocations.c.skill_name,
                    sa.func.count().label("n"),
                    sa.func.max(ne_skill_invocations.c.fired_at).label("last"),
                ).group_by(ne_skill_invocations.c.skill_name)
            ).all()
            sources = self._source_map(conn)
        fired_map = {name: (int(n), last) for name, n, last in fired}
        rows: list[LibraryRow] = []
        for name, source in names:
            row = current.get(name)
            version: int | None = None
            description = ""
            acl_labels: tuple[str, ...] = ()
            content_hash = ""
            tampered = False
            if row is not None:
                version = int(row.version)
                description = row.description
                acl_labels = tuple(row.acl_labels)
                content_hash = row.content_hash
                pack = self._reconstruct(name, row, sources)
                tampered = pack.content_hash != row.content_hash
            count, last = fired_map.get(name, (0, None))
            rows.append(
                LibraryRow(
                    name=name,
                    source=source,
                    status=status.get(name, "active"),
                    current_version=version,
                    description=description,
                    acl_labels=acl_labels,
                    content_hash=content_hash,
                    fired_count=count,
                    last_fired_at=_as_utc(last) if last is not None else None,
                    tampered=tampered,
                )
            )
        return tuple(rows)

    def invocations_page(self, cursor: str | None, limit: int = 500) -> InvocationPage:
        """A cursor-pull page of invocation rows (opaque cursor = str(last id))."""
        after = int(cursor) if cursor else 0
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.select(ne_skill_invocations)
                .where(ne_skill_invocations.c.id > after)
                .order_by(ne_skill_invocations.c.id)
                .limit(limit + 1)
            ).all()
        exhausted = len(rows) <= limit
        page_rows = rows[:limit]
        items = tuple(
            SkillInvocation(
                skill_name=row.skill_name,
                version=row.version,
                principal_id=row.principal_id,
                session_id=row.session_id,
                fired_at=_as_utc(row.fired_at),
                run_succeeded=row.run_succeeded,
            )
            for row in page_rows
        )
        next_cursor = str(page_rows[-1].id) if page_rows else (cursor or "0")
        return InvocationPage(items=items, cursor=next_cursor, exhausted=exhausted)

    # -- surfaces (materialize projection cache) --------------------------

    def upsert_surface(
        self, surface_key: str, index_text: str, pack_names: tuple[str, ...]
    ) -> None:
        # THE surface chokepoint: both PydanticAiSkillSubstrate.materialize and
        # .withdraw write through here, so the no-packs-no-index invariant is
        # enforced once for both rather than in each caller (see _consistent_index).
        index_text = _consistent_index(index_text, pack_names)
        now = _now()
        with self._engine.begin() as conn:
            exists = conn.execute(
                sa.select(ne_skill_surfaces.c.id).where(
                    ne_skill_surfaces.c.surface_key == surface_key
                )
            ).first()
            if exists is None:
                conn.execute(
                    ne_skill_surfaces.insert().values(
                        surface_key=surface_key,
                        index_text=index_text,
                        pack_names=list(pack_names),
                        computed_at=now,
                    )
                )
            else:
                conn.execute(
                    ne_skill_surfaces.update()
                    .where(ne_skill_surfaces.c.surface_key == surface_key)
                    .values(
                        index_text=index_text,
                        pack_names=list(pack_names),
                        computed_at=now,
                    )
                )

    def read_surface(self, surface_key: str) -> tuple[str, tuple[str, ...]] | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(ne_skill_surfaces.c.index_text, ne_skill_surfaces.c.pack_names).where(
                    ne_skill_surfaces.c.surface_key == surface_key
                )
            ).first()
        if row is None:
            return None
        return row.index_text, tuple(row.pack_names)

    def all_surface_keys(self) -> tuple[str, ...]:
        """Every materialized surface key (retirement withdraws from each)."""
        with self._engine.begin() as conn:
            rows = conn.execute(sa.select(ne_skill_surfaces.c.surface_key)).all()
        return tuple(str(r.surface_key) for r in rows)

    def retire(self, name: str, *, actor: str, reason: str) -> None:
        """ATOMICALLY append the ``retired`` lifecycle event AND withdraw ``name``
        from every materialized surface, in ONE transaction.

        The lifecycle append and the surface withdrawals MUST commit together
        (Codex P2): if they ran in separate transactions, a surface-update
        failure after the lifecycle commit would leave the pack folded-retired
        yet still ADVERTISED on a stale surface — worst of all for a pack
        retired BECAUSE it was failing. One ``begin()`` makes retirement
        all-or-nothing; a failure rolls back the lifecycle event too, so a
        retry is clean.
        """
        now = _now()
        prefix = f"- {name} ("
        with self._engine.begin() as conn:
            self._insert_lifecycle(conn, name, "retired", actor=actor, reason=reason, now=now)
            surface_keys = [
                str(r.surface_key)
                for r in conn.execute(sa.select(ne_skill_surfaces.c.surface_key)).all()
            ]
            for surface_key in surface_keys:
                row = conn.execute(
                    sa.select(ne_skill_surfaces.c.index_text, ne_skill_surfaces.c.pack_names).where(
                        ne_skill_surfaces.c.surface_key == surface_key
                    )
                ).first()
                if row is None:
                    continue
                pack_names = tuple(row.pack_names)
                if name not in pack_names:
                    continue
                remaining = [n for n in pack_names if n != name]
                kept = "\n".join(
                    line for line in row.index_text.splitlines() if not line.startswith(prefix)
                )
                conn.execute(
                    ne_skill_surfaces.update()
                    .where(ne_skill_surfaces.c.surface_key == surface_key)
                    .values(
                        # retire() is the ONE writer that bypasses upsert_surface,
                        # so it enforces the same invariant here.
                        index_text=_consistent_index(kept, remaining),
                        pack_names=remaining,
                        computed_at=now,
                    )
                )

    # -- NE-SKILL-4 rot reads --------------------------------------------------

    def status_map(self) -> dict[str, str]:
        """Public wrapper over the folded status map (name -> active/quarantined/retired)."""
        with self._engine.begin() as conn:
            return self._status_map(conn)

    def registered_at_map(self) -> dict[str, datetime]:
        """name -> ``ne_skills.created_at`` (UTC). The register instant a just-registered
        pack is measured against so it is never an instant retirement candidate."""
        with self._engine.begin() as conn:
            rows = conn.execute(sa.select(ne_skills.c.name, ne_skills.c.created_at)).all()
        return {str(name): _as_utc(created) for name, created in rows}

    def last_lifecycle_at(self, name: str, event: str) -> datetime | None:
        """The MAX ``created_at`` of a lifecycle ``event`` for ``name`` (UTC), or None."""
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(sa.func.max(ne_skill_lifecycle.c.created_at)).where(
                    ne_skill_lifecycle.c.name == name,
                    ne_skill_lifecycle.c.event == event,
                )
            ).first()
        if row is None or row[0] is None:
            return None
        return _as_utc(row[0])

    # -- NE-SKILL-4 gardener run persistence -----------------------------------

    def append_gardener_run(self, report: dict[str, Any]) -> None:
        """Persist ONE gardener run report (the digest route reads the latest)."""
        with self._engine.begin() as conn:
            conn.execute(ne_skill_gardener_runs.insert().values(report=report, created_at=_now()))

    def latest_gardener_report(self) -> dict[str, Any] | None:
        """The most-recent gardener run report (max id), or None when none exist."""
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(ne_skill_gardener_runs.c.report)
                .order_by(ne_skill_gardener_runs.c.id.desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        return dict(row.report)
