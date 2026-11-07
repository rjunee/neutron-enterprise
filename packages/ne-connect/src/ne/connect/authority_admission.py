# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Durable source-authority admission coordination for the supported M365 pull path.

015A0-AUTHORITY-CORE owns the receipt store, the current head and the fenced
reconciliation claim. This module owns everything a *producer* needs on top of
that, and nothing a producer may fake:

* :class:`SqlAlchemySourceContractHeadStore` — the connector-scope
  ``CurrentSourceContractHead``. A changed configured contract hash CAS-advances
  ``contract_epoch`` and leaves the scope *unresolved*, so an old receipt is
  Unknown even inside its former SLA until a successful admission under the new
  epoch resolves it again.
* :class:`SqlAlchemyHierarchicalAdmission` — the claim-before-I/O hierarchy. A
  configured site claims its parent BEFORE ``/sites/{site}/drives`` discovery;
  each discovered drive claims a child under the same epoch before delta, item
  or permission I/O; and the site finalizes only against the union of newly
  discovered drives and the durable previously known descendants. Absence is not
  deletion: an omitted descendant blocks parent success until an
  identity-bearing retirement is recorded for that exact drive.
* :class:`SqlAlchemyAdmissionAttemptStore` — the durable
  ``claimed -> deletion_validated -> authority_tombstoned -> cursor_committable``
  state machine behind identity-bearing deletion, plus the safe read-only
  ``inspect``/``retry`` projection ``ne connect authority`` exposes. It has no
  force-allow and no destructive delete: the only thing ``retry`` does is return
  an attempt to its previous stage boundary so the same idempotent work runs
  again.
* :class:`SweepCadencePlan` — the sub-SLA permission sweep feasibility rule
  ``ceil(max_points / batch_size) * (cadence + max_batch_duration) < SLA``. A
  plan that cannot prove it fails; it never rounds, never widens the SLA and
  never silently inherits a generic cron.

Every table here is created in-module under the same Postgres advisory-lock
discipline ``ne.brain.current_source_authority`` uses, because API and worker
replicas boot concurrently against one runtime database.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import sqlalchemy as sa

from ne.connect.identity import source_acl_hash, source_snapshot_hash
from ne.contracts.integration import Record
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    CurrentSourceAuthorityWriter,
    CurrentSourceContractHead,
    ImmutableArtifactCreateReceipt,
    ImmutableKnowledgeArtifactWrite,
    M365SourcePoint,
    SourceAdmissionFence,
    SourceAuthorityCandidate,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceDeletionObservation,
    SourceIdentity,
    SourceReconciliationClaim,
    SourceReconciliationScope,
)

__all__ = [
    "ADMISSION_SCHEMA_VERSION",
    "AdmissionAttempt",
    "AdmissionPreparation",
    "AdmissionStage",
    "AdmissionStageError",
    "AuthorityAdmissionError",
    "ConnectorInstance",
    "ConnectorInstanceRebindError",
    "ContractEpochUnresolvedError",
    "M365AuthorityAdmission",
    "M365DeletionObservation",
    "OverlappingDriveScopeError",
    "PartialPermissionReadError",
    "PermissionReadEvidence",
    "PullAuthorityAdmission",
    "SourceAuthorityLineageSink",
    "SqlAlchemyAdmissionAttemptStore",
    "SqlAlchemyHierarchicalAdmission",
    "SqlAlchemySourceContractHeadStore",
    "SqlAlchemySweepCursor",
    "SweepBatch",
    "SweepCadenceError",
    "SweepCadencePlan",
    "UnclaimedScopeError",
    "UnreconciledDescendantError",
    "UnsweptPointError",
    "admission_metadata",
    "claim_or_load_connector_instance",
    "connector_instance_scope",
    "drive_scope",
    "ensure_admission_schema",
    "expected_scope_head_revision",
    "ne_source_admission_attempts",
    "ne_source_admission_observations",
    "ne_source_connector_instances",
    "ne_source_contract_heads",
    "ne_source_scope_descendants",
    "ne_source_scope_points",
    "ne_source_sweep_cursors",
    "observed_permissions",
    "reject_overlapping_drive_scopes",
    "site_scope",
]

#: The frozen non-secret schema version for the rows in this module.
ADMISSION_SCHEMA_VERSION = 1

_POSTGRES_SCHEMA_LOCK_KEY = 0x4E45_5341_444D

#: The scope-path tag for every M365 reconciliation scope. It is a path
#: COMPONENT rather than a delimiter-folded prefix, so a tenant, site or drive id
#: containing the tag's own text cannot impersonate another level.
M365_SCOPE_KIND = "m365-sharepoint"

_SITE_TAG = "site"
_DRIVE_TAG = "drive"
_SALESFORCE_SCOPE_KIND = "salesforce"
_SALESFORCE_OBJECT_TAG = "object"

admission_metadata = sa.MetaData()


class AuthorityAdmissionError(RuntimeError):
    """A producer-side admission precondition was not met."""


class ContractEpochUnresolvedError(AuthorityAdmissionError):
    """The connector scope has not been re-admitted under its current epoch."""


class OverlappingDriveScopeError(AuthorityAdmissionError):
    """One drive is both directly configured and site-discovered."""


class UnreconciledDescendantError(AuthorityAdmissionError):
    """A previously admitted descendant was omitted without a retirement."""


class AdmissionStageError(AuthorityAdmissionError):
    """A deletion attempt cannot move to the requested durable stage."""


class SweepCadenceError(AuthorityAdmissionError):
    """The configured permission sweep cannot re-read every point inside the SLA."""


class ConnectorInstanceRebindError(AuthorityAdmissionError):
    """A connector instance was pointed at another tenant or admitted scope."""


class UnclaimedScopeError(AuthorityAdmissionError):
    """Authority was asked to advance for a scope no live claim covers."""


class PartialPermissionReadError(AuthorityAdmissionError):
    """A permission collection was not read to completion, so it proves nothing."""


class UnsweptPointError(AuthorityAdmissionError):
    """An admitted point was not actually re-read inside its freshness SLA."""


class AdmissionStage(StrEnum):
    """The durable stages of one identity-bearing deletion attempt."""

    CLAIMED = "claimed"
    DELETION_VALIDATED = "deletion_validated"
    AUTHORITY_TOMBSTONED = "authority_tombstoned"
    CURSOR_COMMITTABLE = "cursor_committable"


#: Stage order is data, not a chain of ``if`` branches: a new stage inserted here
#: is automatically ordered, and no comparison anywhere else needs updating.
_STAGE_ORDER: tuple[AdmissionStage, ...] = (
    AdmissionStage.CLAIMED,
    AdmissionStage.DELETION_VALIDATED,
    AdmissionStage.AUTHORITY_TOMBSTONED,
    AdmissionStage.CURSOR_COMMITTABLE,
)


ne_source_contract_heads = sa.Table(
    "ne_source_contract_heads",
    admission_metadata,
    sa.Column("scope_hash", sa.String(64), primary_key=True),
    sa.Column("scope_path", sa.JSON, nullable=False),
    sa.Column("source_kind", sa.String(128), nullable=False),
    sa.Column("source_generation", sa.Integer, nullable=False),
    sa.Column("contract_epoch", sa.Integer, nullable=False),
    sa.Column("contract_identity_hash", sa.String(64), nullable=False),
    sa.Column("descriptor_schema_version", sa.Integer, nullable=False),
    sa.Column("descriptor_hash", sa.String(64), nullable=False),
    sa.Column("head_revision", sa.String(512), nullable=False),
    # An epoch is UNRESOLVED until an admission actually succeeds under it. The
    # column is not a cache of "did the hash change": it is the durable answer to
    # "may a source-bearing read be served for this scope yet".
    sa.Column("resolved", sa.Boolean, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

ne_source_scope_descendants = sa.Table(
    "ne_source_scope_descendants",
    admission_metadata,
    sa.Column("parent_scope_hash", sa.String(64), primary_key=True),
    sa.Column("child_scope_hash", sa.String(64), primary_key=True),
    sa.Column("child_key", sa.String(512), nullable=False),
    sa.Column("retired", sa.Boolean, nullable=False),
    sa.Column("retirement_revision", sa.String(512), nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

ne_source_scope_points = sa.Table(
    "ne_source_scope_points",
    admission_metadata,
    sa.Column("scope_hash", sa.String(64), primary_key=True),
    sa.Column("identity_hash", sa.String(64), primary_key=True),
    # The Graph item id the sweep must actually re-read. Without it the ledger
    # would know a point is stale and have no way to address it.
    sa.Column("graph_item_id", sa.String(1024), nullable=False),
    sa.Column("last_swept_epoch", sa.Integer, nullable=False),
    # DB-authoritative instant of the last genuine permission re-read. The
    # freshness gate is decided against this column, never against wall time on
    # a replica and never against "we ran a sweep recently".
    sa.Column("last_swept_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("retired", sa.Boolean, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

#: The bounded rotating sweep's durable position. One row per reconciliation
#: scope: the deterministic POINT cursor plus the Graph permission PAGE cursor
#: of the point currently in flight, so a restart resumes the exact point and the
#: exact page rather than restarting the collection.
ne_source_sweep_cursors = sa.Table(
    "ne_source_sweep_cursors",
    admission_metadata,
    sa.Column("scope_hash", sa.String(64), primary_key=True),
    sa.Column("sweep_epoch", sa.Integer, nullable=False),
    # The highest identity hash this rotation has COMPLETED. Empty means the
    # rotation has not started; it only ever advances, and resets exactly once
    # per rotation, which is what stops a bounded batch from starving the tail.
    sa.Column("last_completed_point", sa.String(64), nullable=False),
    sa.Column("inflight_point", sa.String(64), nullable=True),
    sa.Column("inflight_page_url", sa.String(4096), nullable=True),
    sa.Column("inflight_principals", sa.JSON, nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

#: One persisted observation instant per admission attempt. This is what makes
#: ``authority_as_of`` both idempotent and refreshable: the SAME attempt retried
#: loads the SAME instant (identical receipt bytes), while the next sweep is a
#: different attempt and therefore a genuinely later observation.
ne_source_admission_observations = sa.Table(
    "ne_source_admission_observations",
    admission_metadata,
    sa.Column("observation_id", sa.String(512), primary_key=True),
    sa.Column("scope_hash", sa.String(64), nullable=False),
    sa.Column("reconciliation_epoch", sa.Integer, nullable=False),
    sa.Column("identity_hash", sa.String(64), nullable=False),
    sa.Column("authority_as_of", sa.DateTime(timezone=True), nullable=False),
)

ne_source_connector_instances = sa.Table(
    "ne_source_connector_instances",
    admission_metadata,
    sa.Column("registry_key", sa.String(64), primary_key=True),
    sa.Column("connector_instance_id", sa.String(512), nullable=False),
    sa.Column("source_generation", sa.Integer, nullable=False),
    # The CREDENTIAL-FREE admitted target. Credential material is structurally
    # absent from the digest, which is what makes a secret/certificate rotation
    # keep the instance id while a tenant or site/drive change cannot.
    sa.Column("admitted_target_digest", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

ne_source_admission_attempts = sa.Table(
    "ne_source_admission_attempts",
    admission_metadata,
    sa.Column("attempt_id", sa.String(512), primary_key=True),
    sa.Column("scope_hash", sa.String(64), nullable=False),
    sa.Column("identity_hash", sa.String(64), nullable=False),
    sa.Column("stage", sa.String(32), nullable=False),
    sa.Column("source_revision", sa.String(512), nullable=False),
    sa.Column("contract_epoch", sa.Integer, nullable=False),
    sa.Column("prior_artifact_id", sa.String(64), nullable=True),
    sa.Column("receipt_id", sa.String(512), nullable=True),
    sa.Column("receipt_hash", sa.String(64), nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def ensure_admission_schema(engine: sa.Engine) -> None:
    """Create the producer-side admission tables, serialized across replicas.

    The CORE-owned governing contract-epoch binding is created here too. It is
    ne-brain's row, but this module is what WRITES it, and a contract-epoch
    advance must never be the statement that discovers the table is missing.
    """
    from ne.brain.current_source_authority import ne_source_authority_contract_epochs

    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(
                sa.text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _POSTGRES_SCHEMA_LOCK_KEY},
            )
        admission_metadata.create_all(conn)
        ne_source_authority_contract_epochs.create(conn, checkfirst=True)


def _utc(value: datetime) -> datetime:
    """Normalize a stored timestamp to UTC (SQLite reads back naive)."""
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(UTC)


def _db_now(conn: sa.Connection) -> datetime:
    """The DB-authoritative UTC clock; a lease never rides a replica's clock."""
    value = conn.execute(sa.select(sa.func.now())).scalar_one()
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _insert_absent(
    conn: sa.Connection,
    table: sa.Table,
    conflict_columns: Sequence[str],
    values: Mapping[str, Any],
) -> bool:
    """Dialect-correct insert-if-absent; ``True`` iff this call inserted the row.

    The result is read from ``RETURNING`` rather than ``rowcount`` because
    psycopg reports ``-1`` for an ``ON CONFLICT DO NOTHING`` insert; a rowcount
    test therefore reads *every* insert as a loss on the deployed database. The
    dedicated round-trip test locks this on the engine under test rather than on
    this sentence.
    """
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        statement = (
            pg_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=list(conflict_columns))
        )
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        statement = (
            sqlite_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=list(conflict_columns))
        )
    return conn.execute(statement.returning(table.c[conflict_columns[0]])).first() is not None


# -- canonical scopes --------------------------------------------------------


def connector_instance_scope(
    *, deployment_id: str, connector_instance_id: str, graph_tenant_id: str
) -> SourceReconciliationScope:
    """The connector-instance/tenant scope every M365 contract head is keyed by."""
    return SourceReconciliationScope(
        path=(M365_SCOPE_KIND, deployment_id, connector_instance_id, graph_tenant_id)
    )


def site_scope(
    *, deployment_id: str, connector_instance_id: str, graph_tenant_id: str, site_id: str
) -> SourceReconciliationScope:
    """The parent scope a configured site claims BEFORE drive discovery."""
    return SourceReconciliationScope(
        path=(
            M365_SCOPE_KIND,
            deployment_id,
            connector_instance_id,
            graph_tenant_id,
            _SITE_TAG,
            site_id,
        )
    )


def drive_scope(
    *, deployment_id: str, connector_instance_id: str, graph_tenant_id: str, drive_id: str
) -> SourceReconciliationScope:
    """The scope one drive claims, identical whether direct or site-discovered.

    A drive's reconciliation scope deliberately does NOT carry the site it was
    reached through. Reaching one drive by two routes must be the SAME scope, so
    the double-claim is structurally visible instead of being two independent
    claims that each believe they hold the drive exclusively.
    """
    return SourceReconciliationScope(
        path=(
            M365_SCOPE_KIND,
            deployment_id,
            connector_instance_id,
            graph_tenant_id,
            _DRIVE_TAG,
            drive_id,
        )
    )


def _attempt_contract_head_scope(scope: SourceReconciliationScope) -> SourceReconciliationScope:
    """Return the exact connector parent that owns a child attempt's epoch."""
    path = scope.path
    if len(path) == 6 and (path[0], path[4]) in {
        (M365_SCOPE_KIND, _DRIVE_TAG),
        (_SALESFORCE_SCOPE_KIND, _SALESFORCE_OBJECT_TAG),
    }:
        return SourceReconciliationScope(path=path[:4])
    return scope


def reject_overlapping_drive_scopes(
    *, configured_drive_ids: Sequence[str], discovered_drive_ids: Sequence[str]
) -> None:
    """Fail closed when one drive is both directly configured and discovered.

    Canonical identity comparison, not a substring or prefix test: the overlap is
    computed on the exact drive ids, so a drive whose id merely contains
    another's is not an overlap and a genuine duplicate cannot be spelled around.
    """
    overlap = sorted(set(configured_drive_ids) & set(discovered_drive_ids))
    if overlap:
        raise OverlappingDriveScopeError(
            f"drive(s) {overlap!r} are both directly configured and site-discovered; "
            "one drive is never reconciled twice"
        )


# -- the connector-scope contract head ---------------------------------------


class SqlAlchemySourceContractHeadStore:
    """Claim-or-load the connector scope's ``CurrentSourceContractHead``.

    The head is claim-or-loaded BEFORE API readiness or any worker/CLI sync, so
    API and worker independently compute the same configured hash and read the
    same epoch. A value-changed contract advances the epoch and clears
    ``resolved``; only a successful admission under the new epoch sets it again.
    """

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    def claim_or_load(
        self,
        scope: SourceReconciliationScope,
        *,
        source_generation: int,
        contract: SourceContractIdentity,
    ) -> CurrentSourceContractHead:
        """Return this scope's current head, advancing the epoch on a changed hash."""
        from ne.brain.current_source_authority import ne_source_authority_contract_epochs

        if source_generation < 1:
            raise ValueError("source_generation must be positive")
        with self._engine.begin() as conn:
            now = _db_now(conn)
            _insert_absent(
                conn,
                ne_source_contract_heads,
                ("scope_hash",),
                {
                    "scope_hash": scope.scope_hash,
                    "scope_path": list(scope.path),
                    "source_kind": contract.source_kind,
                    "source_generation": source_generation,
                    "contract_epoch": 1,
                    "contract_identity_hash": contract.identity_hash,
                    "descriptor_schema_version": contract.descriptor_schema_version,
                    "descriptor_hash": contract.descriptor_hash,
                    "head_revision": _head_revision(1, contract.identity_hash),
                    "resolved": False,
                    "updated_at": now,
                },
            )
            row = self._require_row(conn, scope)
            if row.source_kind != contract.source_kind:
                raise AuthorityAdmissionError(
                    "this connector scope is registered under another source kind"
                )
            if row.source_generation != source_generation:
                raise AuthorityAdmissionError(
                    "changing the admitted source generation requires a new connector instance"
                )
            if row.contract_identity_hash == contract.identity_hash:
                return _head_from_row(row, scope)
            epoch = int(row.contract_epoch) + 1
            advanced = conn.execute(
                sa.update(ne_source_contract_heads)
                .where(
                    ne_source_contract_heads.c.scope_hash == scope.scope_hash,
                    ne_source_contract_heads.c.contract_epoch == row.contract_epoch,
                    ne_source_contract_heads.c.contract_identity_hash == row.contract_identity_hash,
                )
                .values(
                    contract_epoch=epoch,
                    contract_identity_hash=contract.identity_hash,
                    descriptor_schema_version=contract.descriptor_schema_version,
                    descriptor_hash=contract.descriptor_hash,
                    head_revision=_head_revision(epoch, contract.identity_hash),
                    resolved=False,
                    updated_at=now,
                )
            ).rowcount
            if advanced != 1:
                raise AuthorityAdmissionError(
                    "another composition advanced this contract epoch first"
                )
            # Inside the SAME transaction as the CAS: the CAS above stays the one
            # authority, and CORE's read-time binding can never be left naming an
            # epoch this scope has already advanced past. Bounded by `<` so a
            # concurrent advance that already went further is not walked back.
            conn.execute(
                sa.update(ne_source_authority_contract_epochs)
                .where(
                    ne_source_authority_contract_epochs.c.contract_scope_hash == scope.scope_hash,
                    ne_source_authority_contract_epochs.c.contract_epoch < epoch,
                )
                .values(contract_epoch=epoch, updated_at=now)
            )
            return _head_from_row(self._require_row(conn, scope), scope)

    def mark_resolved(self, head: CurrentSourceContractHead) -> bool:
        """Record that an admission succeeded under this exact epoch.

        CAS'd on the epoch and contract hash: a late success from a superseded
        epoch cannot resolve the epoch that replaced it.
        """
        with self._engine.begin() as conn:
            resolved = conn.execute(
                sa.update(ne_source_contract_heads)
                .where(
                    ne_source_contract_heads.c.scope_hash == head.connector_scope.scope_hash,
                    ne_source_contract_heads.c.contract_epoch == head.contract_epoch,
                    ne_source_contract_heads.c.contract_identity_hash
                    == head.source_contract.identity_hash,
                )
                .values(resolved=True, updated_at=_db_now(conn))
            ).rowcount
        return resolved == 1

    def require_resolved(self, scope: SourceReconciliationScope) -> CurrentSourceContractHead:
        """Return the head only when its current epoch has been admitted under."""
        with self._engine.begin() as conn:
            row = self._require_row(conn, scope)
        if not row.resolved:
            raise ContractEpochUnresolvedError(
                "this connector scope has not been re-admitted under its current contract "
                "epoch; every receipt is Unknown until it is"
            )
        return _head_from_row(row, scope)

    def load(self, scope: SourceReconciliationScope) -> CurrentSourceContractHead | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(ne_source_contract_heads).where(
                    ne_source_contract_heads.c.scope_hash == scope.scope_hash
                )
            ).one_or_none()
        return None if row is None else _head_from_row(row, scope)

    def is_resolved(self, scope: SourceReconciliationScope) -> bool:
        with self._engine.begin() as conn:
            value = conn.execute(
                sa.select(ne_source_contract_heads.c.resolved).where(
                    ne_source_contract_heads.c.scope_hash == scope.scope_hash
                )
            ).scalar_one_or_none()
        return bool(value)

    def _require_row(self, conn: sa.Connection, scope: SourceReconciliationScope) -> Any:
        row = conn.execute(
            sa.select(ne_source_contract_heads).where(
                ne_source_contract_heads.c.scope_hash == scope.scope_hash
            )
        ).one_or_none()
        if row is None:
            raise AuthorityAdmissionError("no contract head is registered for this connector scope")
        return row


def _head_revision(epoch: int, contract_identity_hash: str) -> str:
    """A deterministic head revision, so two replicas mint the identical value."""
    material = json.dumps(
        {"epoch": epoch, "contract": contract_identity_hash, "wire": ADMISSION_SCHEMA_VERSION},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "chead-" + hashlib.sha256(material).hexdigest()


def _head_from_row(row: Any, scope: SourceReconciliationScope) -> CurrentSourceContractHead:
    return CurrentSourceContractHead(
        connector_scope=scope,
        source_generation=int(row.source_generation),
        contract_epoch=int(row.contract_epoch),
        source_contract=SourceContractIdentity(
            source_kind=str(row.source_kind),
            descriptor_schema_version=int(row.descriptor_schema_version),
            descriptor_hash=str(row.descriptor_hash),
        ),
        head_revision=str(row.head_revision),
    )


# -- the hierarchical, claim-before-I/O descendant ledger ---------------------


class SqlAlchemyHierarchicalAdmission:
    """Durable parent/child descendant bookkeeping for M365 reconciliation.

    The exclusive claim, its lease, its epoch and its fencing token belong to
    ``CurrentSourceAuthorityWriter``; this store never mints one. What it owns is
    the part a lease cannot answer: which descendants a parent is *known* to have
    from previous epochs, so a discovery response that silently omits one cannot
    be read as a deletion.
    """

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    def record_descendants(
        self,
        parent: SourceReconciliationScope,
        children: Iterable[tuple[str, SourceReconciliationScope]],
    ) -> None:
        """Durably remember each discovered child of ``parent``.

        Re-recording an already-retired child deliberately does NOT revive it:
        a retirement is an authoritative identity-bearing statement, and a later
        discovery listing has no authority to overturn it. Only a new admission
        under a new generation does that, and that is a different child key.
        """
        with self._engine.begin() as conn:
            now = _db_now(conn)
            for child_key, child in children:
                _insert_absent(
                    conn,
                    ne_source_scope_descendants,
                    ("parent_scope_hash", "child_scope_hash"),
                    {
                        "parent_scope_hash": parent.scope_hash,
                        "child_scope_hash": child.scope_hash,
                        "child_key": child_key,
                        "retired": False,
                        "retirement_revision": None,
                        "updated_at": now,
                    },
                )

    def known_descendant_keys(self, parent: SourceReconciliationScope) -> tuple[str, ...]:
        """Every non-retired descendant key this parent has previously admitted."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.select(ne_source_scope_descendants.c.child_key).where(
                    ne_source_scope_descendants.c.parent_scope_hash == parent.scope_hash,
                    ne_source_scope_descendants.c.retired.is_(False),
                )
            ).all()
        return tuple(sorted(str(row.child_key) for row in rows))

    def retire_descendant(
        self,
        parent: SourceReconciliationScope,
        child: SourceReconciliationScope,
        *,
        retirement_revision: str,
    ) -> bool:
        """Record an authoritative identity-bearing retirement for one descendant.

        ``retirement_revision`` is the exact source revision the retirement was
        observed at. A retirement without one is not representable, because
        "it stopped appearing" is the very thing this method must not encode.
        """
        if not retirement_revision:
            raise ValueError("a descendant retirement requires an identity-bearing revision")
        with self._engine.begin() as conn:
            retired = conn.execute(
                sa.update(ne_source_scope_descendants)
                .where(
                    ne_source_scope_descendants.c.parent_scope_hash == parent.scope_hash,
                    ne_source_scope_descendants.c.child_scope_hash == child.scope_hash,
                )
                .values(
                    retired=True,
                    retirement_revision=retirement_revision,
                    updated_at=_db_now(conn),
                )
            ).rowcount
        return retired == 1

    def require_reconciled_union(
        self, parent: SourceReconciliationScope, *, discovered_keys: Sequence[str]
    ) -> tuple[str, ...]:
        """Return the reconciled union, or refuse the parent's success.

        A previously admitted, non-retired descendant that discovery omitted
        leaves that descendant Unknown and blocks the parent. Absence is not
        deletion.
        """
        known = set(self.known_descendant_keys(parent))
        omitted = sorted(known - set(discovered_keys))
        if omitted:
            raise UnreconciledDescendantError(
                f"previously admitted descendant(s) {omitted!r} were omitted by discovery "
                "without an identity-bearing retirement; absence is not deletion"
            )
        return tuple(sorted(known | set(discovered_keys)))

    # -- per-point sweep bookkeeping ---------------------------------------

    def register_points(
        self,
        scope: SourceReconciliationScope,
        points: Iterable[tuple[str, str]],
    ) -> None:
        """Remember the ``(identity_hash, graph_item_id)`` points a sweep must re-read."""
        with self._engine.begin() as conn:
            now = _db_now(conn)
            for identity_hash, graph_item_id in points:
                _insert_absent(
                    conn,
                    ne_source_scope_points,
                    ("scope_hash", "identity_hash"),
                    {
                        "scope_hash": scope.scope_hash,
                        "identity_hash": identity_hash,
                        "graph_item_id": graph_item_id,
                        "last_swept_epoch": 0,
                        "last_swept_at": None,
                        "retired": False,
                        "updated_at": now,
                    },
                )

    def mark_point_swept(
        self,
        scope: SourceReconciliationScope,
        identity_hash: str,
        *,
        epoch: int,
        graph_item_id: str,
        evidence: PermissionReadEvidence,
    ) -> None:
        """Record that this point's permission resource was ACTUALLY re-fetched.

        ``evidence`` is required and unforgeable-by-omission: nothing marks a
        point swept on a timer, an empty delta, a cursor reset or a blind head
        touch, because none of those can produce a completed permission read.
        """
        if not evidence.matches(f"{evidence.drive_id}:{graph_item_id}"):
            raise AuthorityAdmissionError(
                "the presented permission read was performed for another item"
            )
        with self._engine.begin() as conn:
            now = _db_now(conn)
            _insert_absent(
                conn,
                ne_source_scope_points,
                ("scope_hash", "identity_hash"),
                {
                    "scope_hash": scope.scope_hash,
                    "identity_hash": identity_hash,
                    "graph_item_id": graph_item_id,
                    "last_swept_epoch": epoch,
                    "last_swept_at": now,
                    "retired": False,
                    "updated_at": now,
                },
            )
            conn.execute(
                sa.update(ne_source_scope_points)
                .where(
                    ne_source_scope_points.c.scope_hash == scope.scope_hash,
                    ne_source_scope_points.c.identity_hash == identity_hash,
                    ne_source_scope_points.c.last_swept_epoch < epoch,
                )
                .values(
                    last_swept_epoch=epoch,
                    last_swept_at=now,
                    graph_item_id=graph_item_id,
                    updated_at=now,
                )
            )

    def retire_point(self, scope: SourceReconciliationScope, identity_hash: str) -> bool:
        """Remove a tombstoned point from the set a sweep must still re-read."""
        with self._engine.begin() as conn:
            retired = conn.execute(
                sa.update(ne_source_scope_points)
                .where(
                    ne_source_scope_points.c.scope_hash == scope.scope_hash,
                    ne_source_scope_points.c.identity_hash == identity_hash,
                )
                .values(retired=True, updated_at=_db_now(conn))
            ).rowcount
        return retired == 1

    def sweepable_points(self, scope: SourceReconciliationScope) -> tuple[tuple[str, str], ...]:
        """Every non-retired ``(identity_hash, graph_item_id)`` in deterministic order.

        Ordered by identity hash, which is what makes the rotation cursor
        deterministic: two processes and two restarts enumerate the same
        sequence, so a bounded batch resumes rather than re-picking.
        """
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.select(
                    ne_source_scope_points.c.identity_hash,
                    ne_source_scope_points.c.graph_item_id,
                )
                .where(
                    ne_source_scope_points.c.scope_hash == scope.scope_hash,
                    ne_source_scope_points.c.retired.is_(False),
                )
                .order_by(ne_source_scope_points.c.identity_hash)
            ).all()
        return tuple((str(row.identity_hash), str(row.graph_item_id)) for row in rows)

    def unswept_points(
        self, scope: SourceReconciliationScope, *, epoch: int
    ) -> tuple[tuple[str, str], ...]:
        """The addressable points this reconciliation epoch has NOT re-read yet.

        The delta path already re-read every point it admitted this run, so the
        rotation covers exactly the remainder — the quiet points. Sweeping a
        point twice in one epoch would spend the bounded batch on work that
        cannot change any answer.
        """
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.select(
                    ne_source_scope_points.c.identity_hash,
                    ne_source_scope_points.c.graph_item_id,
                )
                .where(
                    ne_source_scope_points.c.scope_hash == scope.scope_hash,
                    ne_source_scope_points.c.retired.is_(False),
                    ne_source_scope_points.c.last_swept_epoch < epoch,
                )
                .order_by(ne_source_scope_points.c.identity_hash)
            ).all()
        return tuple((str(row.identity_hash), str(row.graph_item_id)) for row in rows)

    def stale_point_hashes(
        self, scope: SourceReconciliationScope, *, sla_seconds: Decimal
    ) -> tuple[str, ...]:
        """Non-retired points whose last genuine re-read is not inside the SLA.

        A point that has never been swept is stale by construction (``NULL``
        compares out), and the cutoff is computed from the DB clock so a skewed
        replica cannot declare a stale point fresh.
        """
        with self._engine.begin() as conn:
            cutoff = _db_now(conn) - timedelta(seconds=float(sla_seconds))
            rows = conn.execute(
                sa.select(ne_source_scope_points.c.identity_hash).where(
                    ne_source_scope_points.c.scope_hash == scope.scope_hash,
                    ne_source_scope_points.c.retired.is_(False),
                    sa.or_(
                        ne_source_scope_points.c.last_swept_at.is_(None),
                        ne_source_scope_points.c.last_swept_at <= cutoff,
                    ),
                )
            ).all()
        return tuple(sorted(str(row.identity_hash) for row in rows))

    def require_points_swept_within_sla(
        self, scope: SourceReconciliationScope, *, sla_seconds: Decimal
    ) -> None:
        """Refuse the parent while any admitted point is outside its SLA."""
        stale = self.stale_point_hashes(scope, sla_seconds=sla_seconds)
        if stale:
            raise UnsweptPointError(
                f"{len(stale)} admitted point(s) in this scope were not re-read inside the "
                "freshness SLA; the scope stays Unknown and is not advertised"
            )

    def unswept_point_hashes(
        self, scope: SourceReconciliationScope, *, epoch: int
    ) -> tuple[str, ...]:
        """Points this epoch has not actually re-read yet.

        A point is marked swept only when its permission resource was genuinely
        resolved and its admission succeeded, so this is a record of re-reads
        rather than of elapsed time. The SLA gate is
        :meth:`require_points_swept_within_sla`, which decides on the recorded
        INSTANT rather than on the epoch counter.
        """
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.select(ne_source_scope_points.c.identity_hash).where(
                    ne_source_scope_points.c.scope_hash == scope.scope_hash,
                    ne_source_scope_points.c.retired.is_(False),
                    ne_source_scope_points.c.last_swept_epoch < epoch,
                )
            ).all()
        return tuple(sorted(str(row.identity_hash) for row in rows))


# -- the durable bounded rotating sweep cursor -------------------------------


@dataclass(frozen=True)
class SweepBatch:
    """One bounded batch of points to re-read, plus where to resume inside it."""

    sweep_epoch: int
    points: tuple[tuple[str, str], ...]
    #: The point that was in flight when the process last stopped, and the exact
    #: Graph page plus already-collected principals it stopped at. A restart
    #: resumes THAT page rather than restarting the collection, which is the
    #: difference between resuming and silently re-reading from the top.
    resume_point: str | None
    resume_page_url: str | None
    resume_principals: tuple[str, ...]
    #: True when this batch reaches the end of the rotation, so completing it
    #: closes the epoch and the next batch starts a fresh rotation.
    completes_rotation: bool


class SqlAlchemySweepCursor:
    """The durable position of one scope's bounded rotating permission sweep.

    The cursor only ever advances within a rotation and resets exactly once at
    the end of one, so every non-retired point is visited once per rotation and
    a bounded batch cannot starve the tail: the points after the cursor are
    always the next ones taken.
    """

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    def _row(self, conn: sa.Connection, scope: SourceReconciliationScope) -> Any:
        _insert_absent(
            conn,
            ne_source_sweep_cursors,
            ("scope_hash",),
            {
                "scope_hash": scope.scope_hash,
                "sweep_epoch": 1,
                "last_completed_point": "",
                "inflight_point": None,
                "inflight_page_url": None,
                "inflight_principals": None,
                "updated_at": _db_now(conn),
            },
        )
        return conn.execute(
            sa.select(ne_source_sweep_cursors).where(
                ne_source_sweep_cursors.c.scope_hash == scope.scope_hash
            )
        ).one()

    def plan(
        self,
        scope: SourceReconciliationScope,
        *,
        candidates: Sequence[tuple[str, str]],
        batch_size: int,
    ) -> SweepBatch:
        """The next bounded batch, resuming — or restarting — from the cursor.

        When the cursor has no candidates left after it the rotation is over, and
        this RESTARTS it in the same call rather than returning an empty batch.
        Returning empty would be a silent stall: the cursor would sit past the
        last point forever and every point would starve from then on, which is
        the exact failure a bounded rotation exists to avoid.
        """
        if batch_size < 1:
            raise ValueError("a sweep batch must contain at least one point")
        with self._engine.begin() as conn:
            row = self._row(conn, scope)
            cursor = str(row.last_completed_point)
            epoch = int(row.sweep_epoch)
            remaining = [point for point in candidates if point[0] > cursor]
            resume_point = None if row.inflight_point is None else str(row.inflight_point)
            resume_page = None if row.inflight_page_url is None else str(row.inflight_page_url)
            principals = tuple(str(value) for value in (row.inflight_principals or []))
            if not remaining and candidates:
                epoch += 1
                remaining = list(candidates)
                resume_point, resume_page, principals = None, None, ()
                conn.execute(
                    sa.update(ne_source_sweep_cursors)
                    .where(
                        ne_source_sweep_cursors.c.scope_hash == scope.scope_hash,
                        ne_source_sweep_cursors.c.sweep_epoch == row.sweep_epoch,
                    )
                    .values(
                        sweep_epoch=epoch,
                        last_completed_point="",
                        inflight_point=None,
                        inflight_page_url=None,
                        inflight_principals=None,
                        updated_at=_db_now(conn),
                    )
                )
        batch = tuple(remaining[:batch_size])
        return SweepBatch(
            sweep_epoch=epoch,
            points=batch,
            resume_point=resume_point,
            resume_page_url=resume_page,
            resume_principals=principals,
            completes_rotation=len(batch) == len(remaining),
        )

    def record_page(
        self,
        scope: SourceReconciliationScope,
        *,
        identity_hash: str,
        next_page_url: str | None,
        principals: Sequence[str],
    ) -> None:
        """Persist the in-flight point's exact next page and collected principals."""
        with self._engine.begin() as conn:
            self._row(conn, scope)
            conn.execute(
                sa.update(ne_source_sweep_cursors)
                .where(ne_source_sweep_cursors.c.scope_hash == scope.scope_hash)
                .values(
                    inflight_point=identity_hash,
                    inflight_page_url=next_page_url,
                    inflight_principals=list(principals),
                    updated_at=_db_now(conn),
                )
            )

    def complete_point(self, scope: SourceReconciliationScope, *, identity_hash: str) -> None:
        """Advance the point cursor past a fully re-read point."""
        with self._engine.begin() as conn:
            self._row(conn, scope)
            conn.execute(
                sa.update(ne_source_sweep_cursors)
                .where(
                    ne_source_sweep_cursors.c.scope_hash == scope.scope_hash,
                    ne_source_sweep_cursors.c.last_completed_point < identity_hash,
                )
                .values(last_completed_point=identity_hash, updated_at=_db_now(conn))
            )
            conn.execute(
                sa.update(ne_source_sweep_cursors)
                .where(ne_source_sweep_cursors.c.scope_hash == scope.scope_hash)
                .values(
                    inflight_point=None,
                    inflight_page_url=None,
                    inflight_principals=None,
                )
            )


# -- the durable deletion attempt state machine ------------------------------


@dataclass(frozen=True)
class AdmissionAttempt:
    """The safe, operator-visible projection of one durable deletion attempt.

    Every member is an id, a hash, an integer or a stage name. There is
    deliberately no source label, no ACL principal, no content and no
    credential: this is exactly what ``ne connect authority inspect`` prints.
    """

    attempt_id: str
    scope_hash: str
    identity_hash: str
    stage: AdmissionStage
    source_revision: str
    contract_epoch: int
    prior_artifact_id: str | None
    receipt_id: str | None
    receipt_hash: str | None


class SqlAlchemyAdmissionAttemptStore:
    """The ``claimed -> ... -> cursor_committable`` durable deletion coordinator.

    It is an idempotent state machine, not a cross-store exactly-once claim. A
    crash after the tombstone CAS re-loads the same attempt at the same stage and
    resumes; a crash before it leaves the attempt at an earlier stage, which is
    Unknown for the affected scope and a retryable connector cursor.
    """

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    def claim(
        self,
        *,
        attempt_id: str,
        scope: SourceReconciliationScope,
        identity_hash: str,
        source_revision: str,
        contract_epoch: int,
        prior_artifact_id: str | None,
    ) -> AdmissionAttempt:
        """Claim-or-load this attempt at :attr:`AdmissionStage.CLAIMED`."""
        if not attempt_id:
            raise ValueError("an admission attempt requires a non-empty attempt id")
        with self._engine.begin() as conn:
            _insert_absent(
                conn,
                ne_source_admission_attempts,
                ("attempt_id",),
                {
                    "attempt_id": attempt_id,
                    "scope_hash": scope.scope_hash,
                    "identity_hash": identity_hash,
                    "stage": AdmissionStage.CLAIMED.value,
                    "source_revision": source_revision,
                    "contract_epoch": contract_epoch,
                    "prior_artifact_id": prior_artifact_id,
                    "receipt_id": None,
                    "receipt_hash": None,
                    "updated_at": _db_now(conn),
                },
            )
            attempt = self._load(conn, attempt_id)
        if attempt is None:  # pragma: no cover - the insert-or-load above guarantees a row
            raise AdmissionStageError("the claimed admission attempt vanished")
        if attempt.identity_hash != identity_hash or attempt.source_revision != source_revision:
            raise AdmissionStageError(
                "this attempt id is already bound to another source identity/revision"
            )
        return attempt

    def advance(
        self,
        attempt_id: str,
        stage: AdmissionStage,
        *,
        receipt_id: str | None = None,
        receipt_hash: str | None = None,
    ) -> AdmissionAttempt:
        """Move one attempt forward exactly one stage, idempotently.

        Re-advancing to the stage an attempt already holds is a no-op that
        returns the same row: a crash between the durable stage write and the
        caller's own bookkeeping must not become a second, different attempt.
        Skipping a stage, or moving backwards, is refused.
        """
        if stage is AdmissionStage.AUTHORITY_TOMBSTONED and not (receipt_id and receipt_hash):
            raise AdmissionStageError(
                "the authority_tombstoned stage requires the exact tombstone receipt id/hash"
            )
        with self._engine.begin() as conn:
            current = self._load(conn, attempt_id)
            if current is None:
                raise AdmissionStageError(f"no admission attempt {attempt_id!r} exists")
            if current.stage is stage:
                return current
            if _STAGE_ORDER.index(stage) != _STAGE_ORDER.index(current.stage) + 1:
                raise AdmissionStageError(
                    f"an admission attempt at stage {current.stage.value!r} cannot move to "
                    f"{stage.value!r}"
                )
            values: dict[str, Any] = {"stage": stage.value, "updated_at": _db_now(conn)}
            if receipt_id is not None:
                values["receipt_id"] = receipt_id
            if receipt_hash is not None:
                values["receipt_hash"] = receipt_hash
            moved = conn.execute(
                sa.update(ne_source_admission_attempts)
                .where(
                    ne_source_admission_attempts.c.attempt_id == attempt_id,
                    ne_source_admission_attempts.c.stage == current.stage.value,
                )
                .values(values)
            ).rowcount
            if moved != 1:
                raise AdmissionStageError("another worker advanced this attempt first")
            advanced = self._load(conn, attempt_id)
        if advanced is None:  # pragma: no cover - the CAS above just updated this row
            raise AdmissionStageError("the advanced admission attempt vanished")
        return advanced

    def inspect(self, attempt_id: str) -> AdmissionAttempt | None:
        """The read-only ops projection; it can neither allow nor delete anything."""
        with self._engine.begin() as conn:
            return self._load(conn, attempt_id)

    def retryable(self, attempt_id: str) -> bool:
        """Whether ``ne connect authority retry`` has anything left to do.

        An attempt that already reached ``cursor_committable`` is complete: retry
        reports it as such rather than re-running a stage. There is no stage at
        which retry can force an allow or destroy an artifact — the only work it
        can cause is the same idempotent admission that stalled.
        """
        attempt = self.inspect(attempt_id)
        return attempt is not None and attempt.stage is not AdmissionStage.CURSOR_COMMITTABLE

    def pending_for_current_scopes(
        self, scopes: Iterable[SourceReconciliationScope]
    ) -> tuple[AdmissionAttempt, ...]:
        """Return incomplete attempts for the exact scopes' current contract epochs.

        Attempts belong to child drive/object scopes, while the contract epoch is
        owned by their connector-parent scope. Membership is still exact: child
        hashes are compared for equality and never inferred from path prefixes.
        The projection is metadata-only and the empty-input fast path deliberately
        performs no database query.
        """
        requested = {
            scope.scope_hash: _attempt_contract_head_scope(scope).scope_hash for scope in scopes
        }
        if not requested:
            return ()
        with self._engine.begin() as conn:
            head_rows = conn.execute(
                sa.select(
                    ne_source_contract_heads.c.scope_hash,
                    ne_source_contract_heads.c.contract_epoch,
                ).where(
                    ne_source_contract_heads.c.scope_hash.in_(
                        tuple(sorted(set(requested.values())))
                    )
                )
            ).all()
            current_epochs = {str(row.scope_hash): int(row.contract_epoch) for row in head_rows}
            exact_scope_epochs = tuple(
                (scope_hash, current_epochs[parent_hash])
                for scope_hash, parent_hash in sorted(requested.items())
                if parent_hash in current_epochs
            )
            if not exact_scope_epochs:
                return ()
            rows = conn.execute(
                sa.select(ne_source_admission_attempts)
                .where(
                    sa.or_(
                        *(
                            sa.and_(
                                ne_source_admission_attempts.c.scope_hash == scope_hash,
                                ne_source_admission_attempts.c.contract_epoch == contract_epoch,
                            )
                            for scope_hash, contract_epoch in exact_scope_epochs
                        )
                    ),
                    ne_source_admission_attempts.c.stage != AdmissionStage.CURSOR_COMMITTABLE.value,
                )
                .order_by(ne_source_admission_attempts.c.attempt_id)
            ).all()
        return tuple(self._from_row(row) for row in rows)

    def _load(self, conn: sa.Connection, attempt_id: str) -> AdmissionAttempt | None:
        row = conn.execute(
            sa.select(ne_source_admission_attempts).where(
                ne_source_admission_attempts.c.attempt_id == attempt_id
            )
        ).one_or_none()
        if row is None:
            return None
        return self._from_row(row)

    @staticmethod
    def _from_row(row: sa.Row[Any]) -> AdmissionAttempt:
        return AdmissionAttempt(
            attempt_id=str(row.attempt_id),
            scope_hash=str(row.scope_hash),
            identity_hash=str(row.identity_hash),
            stage=AdmissionStage(str(row.stage)),
            source_revision=str(row.source_revision),
            contract_epoch=int(row.contract_epoch),
            prior_artifact_id=None if row.prior_artifact_id is None else str(row.prior_artifact_id),
            receipt_id=None if row.receipt_id is None else str(row.receipt_id),
            receipt_hash=None if row.receipt_hash is None else str(row.receipt_hash),
        )


# -- the sub-SLA permission sweep feasibility rule ---------------------------


@dataclass(frozen=True)
class SweepCadencePlan:
    """An explicit sub-SLA permission-sweep plan for one connector scope.

    With ``batches = ceil(max_points / batch_size)``, a full rotation costs
    ``batches * (cadence + max_batch_duration)`` in the worst case, and that must
    finish STRICTLY inside the contract SLA.

    It is both the feasibility precondition and the batch bound the rotation
    actually runs at. An authority-enabled connector whose numbers cannot beat
    its SLA composes no producer at all, so the scope stays Unknown and is not
    advertised; a connector that can beat it sweeps ``batch_size`` points per
    run from :class:`SqlAlchemySweepCursor`'s persisted position.
    """

    cadence_seconds: Decimal
    batch_size: int
    max_batch_duration_seconds: Decimal
    max_points_per_scope: int

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError("sweep batch_size must be at least 1")
        if self.max_points_per_scope < 1:
            raise ValueError("sweep max_points_per_scope must be at least 1")
        for name in ("cadence_seconds", "max_batch_duration_seconds"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"sweep {name} must be a finite positive Decimal")

    @property
    def batches(self) -> int:
        """Whole batches needed to rotate through the admitted point ceiling."""
        return -(-self.max_points_per_scope // self.batch_size)

    def worst_case_seconds(self) -> Decimal:
        """The exact worst-case seconds one full rotation may take."""
        return self.batches * (self.cadence_seconds + self.max_batch_duration_seconds)

    def satisfies(self, freshness_sla_seconds: Decimal) -> bool:
        """Whether a full rotation completes STRICTLY inside the SLA."""
        return self.worst_case_seconds() < freshness_sla_seconds

    def require_sub_sla(self, freshness_sla_seconds: Decimal) -> None:
        """Fail closed when the configured sweep cannot beat the SLA."""
        if not isinstance(freshness_sla_seconds, Decimal) or freshness_sla_seconds <= 0:
            raise ValueError("freshness_sla_seconds must be a positive Decimal")
        if not self.satisfies(freshness_sla_seconds):
            raise SweepCadenceError(
                f"the configured permission sweep needs {self.worst_case_seconds()}s to "
                f"re-read {self.max_points_per_scope} point(s) in batches of "
                f"{self.batch_size}, which does not fit inside the {freshness_sla_seconds}s "
                "contract SLA; the scope stays Unknown and is not advertised"
            )


# -- the persisted connector instance ----------------------------------------


@dataclass(frozen=True)
class ConnectorInstance:
    """The persisted M365 connector-instance id and its source generation."""

    connector_instance_id: str
    source_generation: int


def _registry_key(deployment_id: str, source_name: str) -> str:
    material = json.dumps(
        {"deployment": deployment_id, "source": source_name},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def claim_or_load_connector_instance(
    engine: sa.Engine,
    *,
    deployment_id: str,
    source_name: str,
    admitted_target_digest: str,
) -> ConnectorInstance:
    """Issue this connector's one instance id, or load the persisted one.

    ``admitted_target_digest`` must be computed over the credential-free target
    only. Because no credential contributes, rotating the client secret or the
    signing certificate loads the SAME instance id; changing the Graph tenant or
    the admitted site/drive scope changes the digest and is REJECTED, which is
    what forces a new instance/generation rather than silently re-pointing an
    admitted history at different data.
    """
    if not deployment_id or not source_name:
        raise ValueError("a connector instance requires a deployment id and source name")
    if len(admitted_target_digest) != 64:
        raise ValueError("admitted_target_digest must be a sha256 hex digest")
    key = _registry_key(deployment_id, source_name)
    minted = "conn-" + hashlib.sha256(f"{key}\n{admitted_target_digest}".encode()).hexdigest()
    with engine.begin() as conn:
        _insert_absent(
            conn,
            ne_source_connector_instances,
            ("registry_key",),
            {
                "registry_key": key,
                "connector_instance_id": minted,
                "source_generation": 1,
                "admitted_target_digest": admitted_target_digest,
                "created_at": _db_now(conn),
            },
        )
        row = conn.execute(
            sa.select(ne_source_connector_instances).where(
                ne_source_connector_instances.c.registry_key == key
            )
        ).one()
    if str(row.admitted_target_digest) != admitted_target_digest:
        raise ConnectorInstanceRebindError(
            "this connector instance is admitted for another Graph tenant or site/drive "
            "scope; changing it requires a new connector instance and generation"
        )
    return ConnectorInstance(
        connector_instance_id=str(row.connector_instance_id),
        source_generation=int(row.source_generation),
    )


# -- the pull-path admission chokepoint --------------------------------------


@dataclass(frozen=True)
class PermissionReadEvidence:
    """Proof that an ACTUAL Graph permission resource was read to completion.

    Freshness may only be refreshed by a real re-read, so the re-read is a VALUE
    the admission requires rather than a step a caller is trusted to have taken.
    A timer, an empty delta page, a page-cursor reset, a blind head touch and a
    failed or partially-paged read all fail to produce one: the only constructor
    is :func:`observed_permissions`, and it refuses an incomplete collection.
    """

    drive_id: str
    item_id: str
    principals: tuple[str, ...]
    pages_read: int

    def matches(self, source_id: str) -> bool:
        """Whether this evidence was read for exactly ``<drive>:<item>``."""
        return f"{self.drive_id}:{self.item_id}" == source_id


def observed_permissions(
    *,
    drive_id: str,
    item_id: str,
    principals: Sequence[str],
    pages_read: int,
    collection_complete: bool,
) -> PermissionReadEvidence:
    """Mint read evidence, refusing anything short of a complete collection.

    ``collection_complete`` is the caller's report that Graph returned no further
    ``@odata.nextLink``. A partial read is refused rather than downgraded,
    because a permission collection truncated halfway is indistinguishable from a
    contraction of access and admitting it would widen the recorded ACL.
    """
    if not collection_complete:
        raise PartialPermissionReadError(
            f"the permission collection for {drive_id}:{item_id} was not read to "
            "completion; a truncated collection cannot refresh authority"
        )
    if pages_read < 1:
        raise PartialPermissionReadError(f"no permission page was read for {drive_id}:{item_id}")
    if not drive_id or not item_id:
        raise PartialPermissionReadError("permission evidence requires a drive and item id")
    return PermissionReadEvidence(
        drive_id=drive_id,
        item_id=item_id,
        principals=tuple(principals),
        pages_read=pages_read,
    )


@dataclass(frozen=True)
class AdmissionPreparation:
    """One record's immutable artifact, created or verified, not yet authoritative.

    Holding this is deliberately NOT authority: the head has not moved, no
    receipt exists, and the Reader still answers with whatever it answered
    before. Only :meth:`PullAuthorityAdmission.finalize` changes that, and only
    after the governed Knowledge write for the same item was accepted.
    """

    source_identity: SourceIdentity
    artifact_identity: SourceContentArtifactIdentity
    create_receipt: ImmutableArtifactCreateReceipt
    acl_snapshot_hash: str
    source_revision: str
    authority_as_of: datetime
    item_id: str
    drive_id: str
    graph_item_id: str
    #: The completed permission read this admission rests on. Carrying it through
    #: to ``finalize`` is what lets the sweep ledger record a re-read that
    #: actually happened rather than one the caller asserts.
    evidence: PermissionReadEvidence
    scope: SourceReconciliationScope


@runtime_checkable
class PullAuthorityAdmission(Protocol):
    """The only authority-bearing seam the shared pull bridge may hold."""

    async def prepare(
        self,
        record: Record,
        *,
        acl: tuple[str, ...],
        canonical_bytes: bytes,
        item_id: str,
        evidence: PermissionReadEvidence,
    ) -> AdmissionPreparation: ...

    async def finalize(
        self, preparation: AdmissionPreparation
    ) -> CurrentSourceAuthorityReceipt: ...


class SourceAuthorityLineageSink(Protocol):
    """The narrow T1 lineage seam that retains an admitted artifact/receipt."""

    def bind_source_authority(
        self, document_id: str, *, receipt: CurrentSourceAuthorityReceipt
    ) -> bool: ...


@dataclass(frozen=True)
class M365DeletionObservation:
    """An identity-bearing Graph delta deletion, never a bare count."""

    drive_id: str
    item_id: str
    source_revision: str


class M365AuthorityAdmission:
    """The production authority producer for admitted M365/SharePoint points.

    It is constructed only by ``build_ingest_stack``. A source wrapper, the CLI
    and API request code cannot reach the Writer through it: the Writer is a
    private attribute, every entry point requires a live claim this object was
    handed, and no method exposes an unfenced ``finalize``.
    """

    def __init__(
        self,
        *,
        engine: sa.Engine,
        writer: CurrentSourceAuthorityWriter,
        artifacts: ImmutableKnowledgeArtifactWrite,
        hierarchy: SqlAlchemyHierarchicalAdmission,
        attempts: SqlAlchemyAdmissionAttemptStore,
        contract_heads: SqlAlchemySourceContractHeadStore,
        contract_head: CurrentSourceContractHead,
        instance: ConnectorInstance,
        deployment_id: str,
        graph_tenant_id: str,
        source_name: str,
        artifact_schema_version: int = 1,
    ) -> None:
        self._engine = engine
        self._writer = writer
        self._artifacts = artifacts
        self._hierarchy = hierarchy
        self._attempts = attempts
        self._contract_heads = contract_heads
        self._head = contract_head
        self._instance = instance
        self._deployment_id = deployment_id
        self._graph_tenant_id = graph_tenant_id
        self._source_name = source_name
        self._artifact_schema_version = artifact_schema_version
        self._claims: dict[str, SourceReconciliationClaim] = {}
        self._drive_sites: dict[str, str] = {}
        self._lineage: SourceAuthorityLineageSink | None = None

    # -- driver-side wiring ------------------------------------------------

    def bind_lineage(self, lineage: SourceAuthorityLineageSink | None) -> None:
        """Attach the T1 lineage sink that retains admitted artifact/receipt ids."""
        self._lineage = lineage

    def bind_drive_claim(
        self, drive_id: str, claim: SourceReconciliationClaim, *, site_id: str = ""
    ) -> None:
        """Record the live claim covering one drive, BEFORE any I/O against it."""
        self._claims[drive_id] = claim
        self._drive_sites[drive_id] = site_id

    def release_drive_claim(self, drive_id: str) -> None:
        """Drop a claim so a later record from that drive cannot ride an expired lease."""
        self._claims.pop(drive_id, None)
        self._drive_sites.pop(drive_id, None)

    @property
    def contract_head(self) -> CurrentSourceContractHead:
        return self._head

    @property
    def engine(self) -> sa.Engine:
        return self._engine

    @property
    def deployment_id(self) -> str:
        return self._deployment_id

    @property
    def connector_instance_id(self) -> str:
        return self._instance.connector_instance_id

    async def claim_scope(
        self, scope: SourceReconciliationScope, *, lease_seconds: int
    ) -> SourceReconciliationClaim:
        """Take the exclusive reconciliation claim for one scope.

        The Writer itself is never handed out. A caller can sequence claims and
        releases through this object, but it cannot reach ``finalize`` with a
        candidate of its own choosing, so a source wrapper still cannot advance
        authority.
        """
        return await self._writer.claim(
            scope,
            expected_head_revision=expected_scope_head_revision(self._engine, scope),
            lease_seconds=lease_seconds,
        )

    async def release_scope(self, claim: SourceReconciliationClaim) -> bool:
        """End a claim's lease so the scope is immediately re-claimable."""
        release = getattr(self._writer, "release", None)
        if release is None:
            return False
        return bool(await release(claim))

    async def renew_scope(
        self, claim: SourceReconciliationClaim, *, lease_seconds: int
    ) -> SourceReconciliationClaim:
        """Extend a live claim's lease WITHOUT advancing its epoch or token.

        This is what lets a crawl hold a SHORT lease. The lease's other job is to
        be a dead-man switch — a worker killed mid-crawl never releases, so the
        scope stays blocked for the remainder of whatever window the lease bought
        — and that argues for the shortest lease the work can live inside.
        Renewal separates the two: the dead-man window shrinks to one lease, while
        a crawl that outruns it keeps the scope by proving it is still alive.

        The underlying CAS is on epoch AND fencing token, so a superseded holder
        cannot renew a scope out from under its successor: it raises instead, and
        the caller learns its claim is gone.
        """
        renew = getattr(self._writer, "renew", None)
        if renew is None:
            return claim
        renewed: SourceReconciliationClaim = await renew(claim, lease_seconds=lease_seconds)
        return renewed

    # -- PullAuthorityAdmission -------------------------------------------

    async def prepare(
        self,
        record: Record,
        *,
        acl: tuple[str, ...],
        canonical_bytes: bytes,
        item_id: str,
        evidence: PermissionReadEvidence,
    ) -> AdmissionPreparation:
        """Create or verify this record's immutable artifact under its live claim.

        ``evidence`` is required and is cross-checked against BOTH the record's
        own identity and the ACL being admitted, so an admission cannot ride a
        permission read performed for a different point or a different result.
        """
        drive_id, graph_item_id = _split_m365_source_id(record.source_id)
        claim = self._require_claim(drive_id)
        if not evidence.matches(record.source_id):
            raise AuthorityAdmissionError(
                "the presented permission read was performed for another point"
            )
        acl_hash = source_acl_hash(acl)
        if source_acl_hash(evidence.principals) != acl_hash:
            raise AuthorityAdmissionError(
                "the admitted ACL is not the one the permission read resolved"
            )
        identity = self._source_identity(drive_id, graph_item_id)
        source_revision = _source_revision(record)
        artifact = SourceContentArtifactIdentity(
            schema_version=1,
            source_identity=identity,
            source_snapshot_hash=source_snapshot_hash(self._source_name, record),
            content_snapshot_hash=hashlib.sha256(canonical_bytes).hexdigest(),
            acl_snapshot_hash=acl_hash,
            source_contract=self._head.source_contract,
            artifact_schema_version=self._artifact_schema_version,
        )
        create_receipt = await self._artifacts.create_if_absent(artifact, canonical_bytes)
        return AdmissionPreparation(
            source_identity=identity,
            artifact_identity=artifact,
            create_receipt=create_receipt,
            acl_snapshot_hash=acl_hash,
            source_revision=source_revision,
            authority_as_of=self._observation_instant(
                claim,
                identity_hash=identity.identity_hash,
                source_revision=source_revision,
                acl_snapshot_hash=acl_hash,
                artifact_id=artifact.artifact_id,
            ),
            item_id=item_id,
            drive_id=drive_id,
            graph_item_id=graph_item_id,
            evidence=evidence,
            scope=claim.scope,
        )

    def _observation_instant(
        self,
        claim: SourceReconciliationClaim,
        *,
        identity_hash: str,
        source_revision: str,
        acl_snapshot_hash: str,
        artifact_id: str,
    ) -> datetime:
        """Claim-or-load the ONE observation instant bound to this attempt.

        The key includes the reconciliation epoch, so retrying inside the same
        claim loads the same instant and reproduces the receipt byte for byte,
        while the NEXT sweep claims a new epoch and therefore genuinely observes
        the point again.
        """
        observation_id = (
            "obs-"
            + hashlib.sha256(
                json.dumps(
                    {
                        "acl": acl_snapshot_hash,
                        "artifact": artifact_id,
                        "contract_epoch": self._head.contract_epoch,
                        "epoch": claim.reconciliation_epoch,
                        "identity": identity_hash,
                        "revision": source_revision,
                        "scope": claim.scope.scope_hash,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        with self._engine.begin() as conn:
            _insert_absent(
                conn,
                ne_source_admission_observations,
                ("observation_id",),
                {
                    "observation_id": observation_id,
                    "scope_hash": claim.scope.scope_hash,
                    "reconciliation_epoch": claim.reconciliation_epoch,
                    "identity_hash": identity_hash,
                    "authority_as_of": _db_now(conn),
                },
            )
            stored = conn.execute(
                sa.select(ne_source_admission_observations.c.authority_as_of).where(
                    ne_source_admission_observations.c.observation_id == observation_id
                )
            ).scalar_one()
        return _utc(stored)

    async def finalize(self, preparation: AdmissionPreparation) -> CurrentSourceAuthorityReceipt:
        """Advance authority for one prepared point and retain it in T1 lineage."""
        claim = self._require_claim(preparation.drive_id)
        candidate = SourceAuthorityCandidate(
            source_identity=preparation.source_identity,
            artifact_identity=preparation.artifact_identity,
            source_revision=preparation.source_revision,
            source_contract=self._head.source_contract,
            contract_epoch=self._head.contract_epoch,
            acl_snapshot_hash=preparation.acl_snapshot_hash,
            state=SourceAuthorityState.PRESENT,
            authority_as_of=preparation.authority_as_of,
        )
        # ENFORCEMENT: persist the exact completed permission result BEFORE the
        # head can move.  An orphan is inert; a supported successful admission can
        # therefore never publish without its canonical ACL snapshot.  The helper
        # independently recomputes the hash, so this is not trust in caller labels.
        from ne.brain.current_source_authority import (
            persist_current_acl_snapshot,
            publish_governing_contract_epoch,
        )

        persist_current_acl_snapshot(
            self._engine,
            identity_hash=preparation.source_identity.identity_hash,
            artifact_id=preparation.artifact_identity.artifact_id,
            acl_snapshot_hash=preparation.acl_snapshot_hash,
            labels=preparation.evidence.principals,
        )
        # Same ordering, same reason: the Reader cannot see the connector contract
        # head, so the epoch governing this identity is published BEFORE the head
        # can move. An interrupted admission leaves an inert orphan binding; the
        # reverse — a published receipt whose governing epoch is unknown to the
        # Reader — is not produced by the supported admission path.
        publish_governing_contract_epoch(
            self._engine,
            identity_hash=preparation.source_identity.identity_hash,
            contract_scope_hash=self._head.connector_scope.scope_hash,
            contract_epoch=self._head.contract_epoch,
        )
        receipt = await self._writer.finalize(claim, self._fence(claim, candidate), candidate)
        if self._lineage is not None:
            self._lineage.bind_source_authority(preparation.item_id, receipt=receipt)
        identity_hash = preparation.source_identity.identity_hash
        self._hierarchy.register_points(claim.scope, ((identity_hash, preparation.graph_item_id),))
        self._hierarchy.mark_point_swept(
            claim.scope,
            identity_hash,
            epoch=claim.reconciliation_epoch,
            graph_item_id=preparation.graph_item_id,
            evidence=preparation.evidence,
        )
        self._contract_heads.mark_resolved(self._head)
        return receipt

    # -- identity-bearing deletion ----------------------------------------

    async def admit_deletion(
        self, observation: M365DeletionObservation
    ) -> CurrentSourceAuthorityReceipt:
        """Run the durable deletion stages for one identity-bearing observation.

        The stages are durable BEFORE the connector's delta cursor may commit, so
        a crash resumes the same idempotent tombstone rather than skipping past a
        deletion the cursor already claimed to have processed.
        """
        claim = self._require_claim(observation.drive_id)
        identity = self._source_identity(observation.drive_id, observation.item_id)
        attempt_id = _attempt_id(identity.identity_hash, observation.source_revision)
        self._attempts.claim(
            attempt_id=attempt_id,
            scope=claim.scope,
            identity_hash=identity.identity_hash,
            source_revision=observation.source_revision,
            contract_epoch=self._head.contract_epoch,
            prior_artifact_id=None,
        )
        self._attempts.advance(attempt_id, AdmissionStage.DELETION_VALIDATED)
        deletion = SourceDeletionObservation(
            source_identity=identity,
            source_revision=observation.source_revision,
            observed_at=self._now(),
            source_contract=self._head.source_contract,
            contract_epoch=self._head.contract_epoch,
        )
        from ne.brain.current_source_authority import publish_governing_contract_epoch

        publish_governing_contract_epoch(
            self._engine,
            identity_hash=identity.identity_hash,
            contract_scope_hash=self._head.connector_scope.scope_hash,
            contract_epoch=self._head.contract_epoch,
        )
        receipt = await self._writer.finalize(claim, self._fence(claim, deletion), deletion)
        self._attempts.advance(
            attempt_id,
            AdmissionStage.AUTHORITY_TOMBSTONED,
            receipt_id=receipt.receipt_id,
            receipt_hash=receipt.receipt_hash,
        )
        self._hierarchy.retire_point(claim.scope, identity.identity_hash)
        self._attempts.advance(attempt_id, AdmissionStage.CURSOR_COMMITTABLE)
        return receipt

    def cursor_committable(self, observation: M365DeletionObservation) -> bool:
        """Whether this deletion's durable stages permit a delta cursor commit."""
        claim = self._require_claim(observation.drive_id)
        identity = self._source_identity(observation.drive_id, observation.item_id)
        attempt = self._attempts.inspect(
            _attempt_id(identity.identity_hash, observation.source_revision)
        )
        del claim
        return attempt is not None and attempt.stage is AdmissionStage.CURSOR_COMMITTABLE

    # -- internals ---------------------------------------------------------

    def _require_claim(self, drive_id: str) -> SourceReconciliationClaim:
        claim = self._claims.get(drive_id)
        if claim is None:
            raise UnclaimedScopeError(
                f"drive {drive_id!r} has no live reconciliation claim; authority is never "
                "advanced for a scope this process did not claim before I/O"
            )
        return claim

    def _source_identity(self, drive_id: str, item_id: str) -> SourceIdentity:
        return SourceIdentity(
            deployment_id=self._deployment_id,
            source_kind=M365_SCOPE_KIND,
            connector_instance_id=self._instance.connector_instance_id,
            point=M365SourcePoint(
                graph_tenant_id=self._graph_tenant_id,
                site_id=self._drive_sites.get(drive_id, ""),
                drive_id=drive_id,
                item_id=item_id,
            ),
            generation=self._instance.source_generation,
        )

    def _fence(
        self,
        claim: SourceReconciliationClaim,
        candidate: SourceAuthorityCandidate | SourceDeletionObservation,
    ) -> SourceAdmissionFence:
        return SourceAdmissionFence(
            deployment_id=self._deployment_id,
            connector_instance_id=self._instance.connector_instance_id,
            source_generation=self._instance.source_generation,
            external_source_revision=candidate.source_revision,
            contract_epoch=self._head.contract_epoch,
            reconciliation_scope=claim.scope,
            reconciliation_epoch=claim.reconciliation_epoch,
            fencing_token=claim.fencing_token,
        )

    def _now(self) -> datetime:
        with self._engine.begin() as conn:
            return _db_now(conn)


def _split_m365_source_id(source_id: str) -> tuple[str, str]:
    """Split the connector's ``<drive>:<item>`` source id into its components.

    ``partition`` on the FIRST separator: a Graph driveItem id may itself contain
    a colon, and splitting on the last one would move characters out of the item
    id and into the drive id, silently addressing a different point.
    """
    drive_id, separator, item_id = source_id.partition(":")
    if not separator or not drive_id or not item_id:
        raise AuthorityAdmissionError(
            "an M365 authority candidate requires a '<drive-id>:<item-id>' source id"
        )
    return drive_id, item_id


def _source_revision(record: Record) -> str:
    """The exact external revision this record was observed at.

    Graph's ``eTag`` is the source's own revision when present. When it is
    absent the revision falls back to the canonical snapshot of the record
    itself, so two different observed states can never share one revision — and
    a revision is never invented from wall time or an attempt id.
    """
    etag = record.payload.get("etag")
    if isinstance(etag, str) and etag:
        return etag
    return "snap-" + source_snapshot_hash("ne.m365.revision", record)


def _attempt_id(identity_hash: str, source_revision: str) -> str:
    """A deterministic attempt id, so a retry resumes instead of forking."""
    material = json.dumps(
        {"identity": identity_hash, "revision": source_revision},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "adm-" + hashlib.sha256(material).hexdigest()


def expected_scope_head_revision(engine: sa.Engine, scope: SourceReconciliationScope) -> str | None:
    """Read the head revision AUTHORITY-CORE's claim row currently pins for a scope.

    ``CurrentSourceAuthorityWriter.claim`` CAS-checks the caller's expected head
    against this exact value, so a producer that re-claims a scope across runs
    must present what the previous run's last successful admission left behind.
    It is read from CORE's own exported table rather than mirrored into a second
    producer-side column, because two copies of a CAS precondition can disagree.
    """
    from ne.brain.current_source_authority import ne_source_reconciliation_claims

    with engine.begin() as conn:
        value = conn.execute(
            sa.select(ne_source_reconciliation_claims.c.head_revision).where(
                ne_source_reconciliation_claims.c.scope_hash == scope.scope_hash
            )
        ).scalar_one_or_none()
    return None if value is None else str(value)
