# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Current source-authority head, immutable receipt store and continuation rows.

This is 015A0-AUTHORITY-CORE's half of ADR-0022: the durable rows behind
``CurrentSourceAuthorityReader``/``Writer`` and ``AuthorizedContinuationTokenCodec``.
It deliberately implements no selector — bounded authorized selection, its page
assembly and its ACL filter belong to 015A0-ENFORCEMENT, which consumes the
claim/load primitive below.

Three properties are structural rather than documented:

* **The receipt store is append-only.** Supersession moves ``ne_source_authority_heads``
  and never touches ``ne_source_authority_receipts``, so a receipt named by an
  EvidenceLocator survives every later admission and can be revalidated from its
  own row without reading the mutable head.
* **One opaque deployment construction id.** It is claim-or-loaded once; a replica
  or a restore of the same history loads the same value, an independently
  initialized deployment mints a different one, and a database carrying receipts
  from *another* construction fails composition rather than joining histories.
* **The continuation rows carry no key bytes, presented token, decrypted payload
  or source label.** Their column sets are exactly the non-secret canonical state
  and the replay integrity record; the MAC is the lookup key, and it is
  re-derived from the loaded row *after* that row is claim/loaded.

ne-brain owns no migration directory: every table here is created in-module under
the same Postgres advisory lock as :mod:`ne.brain.lineage`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from ne.contracts.source_authority import (
    AuthorizedContinuationReplay,
    AuthorizedContinuationState,
    AuthorizedContinuationTokenLocator,
    CurrentSourceAuthorityReceipt,
    SourceAdmissionFence,
    SourceAuthorityCandidate,
    SourceAuthorityDecision,
    SourceAuthorityResolution,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceDeletionObservation,
    SourceReconciliationClaim,
    SourceReconciliationScope,
)

__all__ = [
    "CONTINUATION_CODEC_VERSION",
    "SOURCE_AUTHORITY_SCHEMA_VERSION",
    "ContinuationUnavailableError",
    "ForeignConstructionIdentityError",
    "HmacAuthorizedContinuationTokenCodec",
    "SourceAuthorityClaimConflictError",
    "SourceAuthorityError",
    "SourceAuthorityFenceError",
    "SourceReconciliationScopeBusyError",
    "SqlAlchemyAuthorizedContinuationStore",
    "SqlAlchemyCurrentSourceAuthorityReader",
    "SqlAlchemyCurrentSourceAuthorityWriter",
    "bind_deployment_construction_id",
    "claim_or_load_deployment_construction_id",
    "ensure_source_authority_schema",
    "ne_authorized_continuation_replays",
    "ne_authorized_continuation_states",
    "ne_source_authority_acl_snapshots",
    "ne_source_authority_contract_epochs",
    "ne_source_authority_deployment",
    "ne_source_authority_heads",
    "ne_source_authority_receipts",
    "ne_source_reconciliation_claims",
    "persist_current_acl_snapshot",
    "publish_governing_contract_epoch",
    "source_authority_metadata",
]

#: The frozen non-secret codec version. It is bound into every token preimage, so
#: advancing it invalidates outstanding tokens deployment-wide by construction
#: rather than by a documented promise. Key material is deliberately NOT here and
#: never enters the frozen product config hash.
CONTINUATION_CODEC_VERSION = 1

#: The frozen non-secret schema version for the rows in this module.
SOURCE_AUTHORITY_SCHEMA_VERSION = 1

# The same advisory-lock discipline as ne.brain.lineage: multiple API/worker
# replicas may boot against one runtime database, so package-owned create_all is
# serialized in a single Postgres transaction.
_POSTGRES_SCHEMA_LOCK_KEY = 0x4E45_5341_5554

_EMPTY_ACL_HASH = hashlib.sha256(b"").hexdigest()
_KEY_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
_MAC_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{43}\Z")
_MINIMUM_CONTINUATION_KEY_BYTES = 32

source_authority_metadata = sa.MetaData()


class SourceAuthorityError(RuntimeError):
    """A source-authority admission or read could not be completed."""


class ForeignConstructionIdentityError(SourceAuthorityError):
    """Retained authority history belongs to another deployment construction."""


class SourceAuthorityClaimConflictError(SourceAuthorityError):
    """Another reconciler holds this scope, or the expected head moved."""


class SourceReconciliationScopeBusyError(SourceAuthorityClaimConflictError):
    """Another reconciler currently HOLDS this scope under a live lease.

    This is the one conflict shape that carries no disagreement about authority:
    nothing has moved, nothing is stale, and the holder is expected to finish and
    release. It is still a refusal — the claim is not granted and no admission
    follows — but a scheduled reconciler that fires while its own predecessor is
    still sweeping should SKIP, not report a failure, and only this subclass tells
    it the two apart. Every other conflict (an expected head that moved, a racing
    inserter) stays a bare :class:`SourceAuthorityClaimConflictError` and must
    still surface as an error: those mean the caller's view of authority is wrong.
    """


class SourceAuthorityFenceError(SourceAuthorityError):
    """The presented admission fence does not authorize this claim."""


class ContinuationUnavailableError(RuntimeError):
    """The one opaque outcome shared by every unusable continuation token.

    Malformed syntax, a foreign key id, an absent row, a tampered row and an
    expired row all raise this with the same message: a caller must not be able
    to tell them apart, and a per-cause message is exactly the oracle ADR-0022 §5
    exists to close.
    """


_CONTINUATION_UNAVAILABLE = "continuation is unavailable"


ne_source_authority_deployment = sa.Table(
    "ne_source_authority_deployment",
    source_authority_metadata,
    # A single row, pinned by a constant primary key: two construction ids in one
    # database is precisely the joined-history state that must be unreachable.
    sa.Column("singleton", sa.Integer, primary_key=True),
    sa.Column("construction_id", sa.String(64), nullable=False),
    sa.Column("schema_version", sa.Integer, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("singleton = 1", name="ck_ne_source_authority_deployment_singleton"),
)

ne_source_authority_receipts = sa.Table(
    "ne_source_authority_receipts",
    source_authority_metadata,
    # APPEND-ONLY. Nothing in this module issues an UPDATE or DELETE against it.
    sa.Column("receipt_id", sa.String(512), primary_key=True),
    sa.Column("construction_id", sa.String(64), nullable=False),
    sa.Column("identity_hash", sa.String(64), nullable=False),
    sa.Column("artifact_id", sa.String(64), nullable=True),
    sa.Column("receipt_hash", sa.String(64), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("source_revision", sa.String(512), nullable=False),
    sa.Column("contract_epoch", sa.Integer, nullable=False),
    sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Index("ix_ne_source_authority_receipts_identity", "identity_hash"),
)

ne_source_authority_heads = sa.Table(
    "ne_source_authority_heads",
    source_authority_metadata,
    sa.Column("identity_hash", sa.String(64), primary_key=True),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("contract_epoch", sa.Integer, nullable=False),
    sa.Column("head_revision", sa.String(512), nullable=False),
    sa.Column("current_receipt_id", sa.String(512), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

# Immutable producer-authored ACL labels for one exact logical artifact.  The
# source-authority head remains the only authorization fact: an orphan row is
# inert, while a selected receipt without this exact row fails closed in the
# ENFORCEMENT resolver.  Labels are identifiers, never credentials or content.
ne_source_authority_acl_snapshots = sa.Table(
    "ne_source_authority_acl_snapshots",
    source_authority_metadata,
    sa.Column("artifact_id", sa.String(64), primary_key=True),
    sa.Column("identity_hash", sa.String(64), nullable=False),
    sa.Column("acl_snapshot_hash", sa.String(64), nullable=False),
    sa.Column("labels", sa.JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint(
        "identity_hash",
        "artifact_id",
        "acl_snapshot_hash",
        name="uq_ne_source_authority_acl_snapshot_binding",
    ),
)

#: The connector contract epoch that currently GOVERNS one source identity.
#:
#: The producer's ``CurrentSourceContractHead`` lives in ne-connect and cannot be
#: read from here — ne-connect depends on ne-brain, so the import only runs in
#: that direction. This row is the binding that carries the head's answer across
#: the seam: the producer publishes it before an admission can move the head, and
#: the same transaction that CAS-advances the contract epoch advances every row
#: bound to that connector scope. It is content-free by column set — two sha256
#: digests, an epoch and an instant — so no locator, label or source byte can
#: reach a read-time decision through it.
ne_source_authority_contract_epochs = sa.Table(
    "ne_source_authority_contract_epochs",
    source_authority_metadata,
    sa.Column("identity_hash", sa.String(64), primary_key=True),
    # The CONNECTOR scope, not the reconciliation scope of one admission: two
    # connector scopes may configure byte-identical contracts, and keying this on
    # the shared contract identity would let either one strand the other's
    # sources in a permanent Unknown no admission could clear.
    sa.Column("contract_scope_hash", sa.String(64), nullable=False),
    sa.Column("contract_epoch", sa.Integer, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_ne_source_authority_contract_epochs_scope", "contract_scope_hash"),
)

ne_source_reconciliation_claims = sa.Table(
    "ne_source_reconciliation_claims",
    source_authority_metadata,
    sa.Column("scope_hash", sa.String(64), primary_key=True),
    sa.Column("reconciliation_epoch", sa.Integer, nullable=False),
    sa.Column("fencing_token", sa.String(512), nullable=False),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("head_revision", sa.String(512), nullable=True),
)

#: The private canonical continuation state. Its column set is EXACTLY
#: ``AuthorizedContinuationState`` plus the (key id, MAC) lookup key: no key
#: bytes, no presented token, no decrypted payload, no source label.
ne_authorized_continuation_states = sa.Table(
    "ne_authorized_continuation_states",
    source_authority_metadata,
    sa.Column("key_id", sa.String(128), primary_key=True),
    sa.Column("token_mac", sa.String(64), primary_key=True),
    sa.Column("state_version", sa.Integer, nullable=False),
    sa.Column("principal_access_hash", sa.String(64), nullable=False),
    sa.Column("request_hash", sa.String(64), nullable=False),
    sa.Column("normalizer_version", sa.Integer, nullable=False),
    sa.Column("encoding_version", sa.Integer, nullable=False),
    sa.Column("requested_top_k", sa.Integer, nullable=False),
    sa.Column("scan_limit", sa.Integer, nullable=False),
    sa.Column("config_hash", sa.String(64), nullable=False),
    sa.Column("contract_head_hash", sa.String(64), nullable=False),
    sa.Column("cursor_score", sa.String(128), nullable=False),
    sa.Column("cursor_artifact_id", sa.String(64), nullable=False),
    sa.Column("prior_page_set_hash", sa.String(64), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
)

#: The immutable first-resolution INTEGRITY record, keyed by the token MAC. It
#: stores the replay hash and its bounded counts, never the disclosed items,
#: contributor stamps or ACL fingerprints: a replay is re-derived and checked
#: against this row, not served out of it.
ne_authorized_continuation_replays = sa.Table(
    "ne_authorized_continuation_replays",
    source_authority_metadata,
    sa.Column("token_mac", sa.String(64), primary_key=True),
    sa.Column("replay_hash", sa.String(64), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("scanned_count", sa.Integer, nullable=False),
    sa.Column("returned_count", sa.Integer, nullable=False),
    sa.Column("scan_limit", sa.Integer, nullable=False),
    sa.Column("scope_health_epoch", sa.Integer, nullable=False),
    sa.Column("next_state_hash", sa.String(64), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


def ensure_source_authority_schema(engine: sa.Engine) -> None:
    """Create the CORE authority tables, serialized across booting replicas."""
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(
                sa.text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _POSTGRES_SCHEMA_LOCK_KEY},
            )
        source_authority_metadata.create_all(conn)


def _canonical_acl_snapshot(labels: Sequence[str]) -> tuple[tuple[str, ...], str]:
    canonical = tuple(sorted(set(labels)))
    if not canonical or any(not label or len(label) > 2048 for label in canonical):
        raise ValueError("current ACL labels must be non-empty and bounded")
    payload = json.dumps(
        {"acl": list(canonical)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return canonical, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def persist_current_acl_snapshot(
    engine: sa.Engine,
    *,
    identity_hash: str,
    artifact_id: str,
    acl_snapshot_hash: str,
    labels: Sequence[str],
) -> None:
    """Claim-or-verify the canonical ACL sidecar before authority publication.

    The caller writes this before finalizing the head.  A crash can therefore
    leave only an inert orphan; the reverse (a newly selected head with no ACL
    truth) is not produced by the supported admission path.  Reuse is accepted
    only when every binding and canonical label is byte-equivalent.
    """
    canonical, computed = _canonical_acl_snapshot(labels)
    if computed != acl_snapshot_hash:
        raise ValueError("current ACL labels do not match the admitted snapshot hash")
    if not re.fullmatch(r"[0-9a-f]{64}", identity_hash) or not re.fullmatch(
        r"[0-9a-f]{64}", artifact_id
    ):
        raise ValueError("current ACL snapshot identities must be sha256 digests")
    values = {
        "artifact_id": artifact_id,
        "identity_hash": identity_hash,
        "acl_snapshot_hash": acl_snapshot_hash,
        "labels": list(canonical),
        "created_at": datetime.now(UTC),
    }
    with engine.begin() as conn:
        _insert_absent(
            conn,
            ne_source_authority_acl_snapshots,
            ("artifact_id",),
            values,
        )
        row = conn.execute(
            sa.select(ne_source_authority_acl_snapshots).where(
                ne_source_authority_acl_snapshots.c.artifact_id == artifact_id
            )
        ).one()
    stored_labels = tuple(row.labels) if isinstance(row.labels, list) else ()
    if (
        str(row.identity_hash) != identity_hash
        or str(row.acl_snapshot_hash) != acl_snapshot_hash
        or stored_labels != canonical
    ):
        raise ValueError("current ACL snapshot was replayed under a different binding")


def publish_governing_contract_epoch(
    engine: sa.Engine,
    *,
    identity_hash: str,
    contract_scope_hash: str,
    contract_epoch: int,
) -> int:
    """Bind one source identity to the contract epoch governing its connector scope.

    The producer calls this BEFORE the admission that would move the head, so an
    interrupted admission leaves only an inert orphan binding while a published
    receipt always has one. Returns the governing epoch now stored, which is not
    necessarily the one presented: the write is MONOTONIC, so a late publish from
    an epoch that has already been superseded cannot lower the governing epoch
    back onto its own stale receipts.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", identity_hash) or not re.fullmatch(
        r"[0-9a-f]{64}", contract_scope_hash
    ):
        raise ValueError("a governing contract-epoch binding is keyed by sha256 digests")
    if contract_epoch < 1:
        raise ValueError("a contract epoch is positive")
    with engine.begin() as conn:
        now = _db_now(conn)
        _insert_absent(
            conn,
            ne_source_authority_contract_epochs,
            ("identity_hash",),
            {
                "identity_hash": identity_hash,
                "contract_scope_hash": contract_scope_hash,
                "contract_epoch": contract_epoch,
                "updated_at": now,
            },
        )
        conn.execute(
            sa.update(ne_source_authority_contract_epochs)
            .where(
                ne_source_authority_contract_epochs.c.identity_hash == identity_hash,
                ne_source_authority_contract_epochs.c.contract_epoch <= contract_epoch,
            )
            .values(
                contract_scope_hash=contract_scope_hash,
                contract_epoch=contract_epoch,
                updated_at=now,
            )
        )
        stored = conn.execute(
            sa.select(ne_source_authority_contract_epochs.c.contract_epoch).where(
                ne_source_authority_contract_epochs.c.identity_hash == identity_hash
            )
        ).scalar_one()
    return int(stored)


def _db_now(conn: sa.Connection) -> datetime:
    """The DB-authoritative UTC clock; freshness never rides a replica's clock."""
    value = conn.execute(sa.select(sa.func.now())).scalar_one()
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        # SQLite CURRENT_TIMESTAMP is naive UTC. Stamping it UTC before any
        # conversion is load-bearing: `astimezone` on a naive value assumes LOCAL
        # time, which silently shifts every lease and freshness check by the
        # runner's offset.
        value = value.replace(tzinfo=UTC)
    # PostgreSQL now() is aware but in the SESSION time zone; the contract types
    # require UTC, and a local-offset value is the right instant under a label the
    # frozen models reject.
    return value.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    """Normalize a stored timestamp to UTC (SQLite reads back naive)."""
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(UTC)


def _digest(domain: str, values: Sequence[str]) -> str:
    """A domain-separated, length-prefixed digest for module-internal ids.

    Deliberately NOT ne-contracts' canonical identity hash: these are opaque
    server-minted row ids, and reusing the contract's domain strings would make
    an internal id indistinguishable from a contract identity.
    """
    material = bytearray(domain.encode("utf-8"))
    material.append(0x1E)
    for value in values:
        encoded = value.encode("utf-8")
        material.extend(len(encoded).to_bytes(8, "big"))
        material.extend(encoded)
    return hashlib.sha256(bytes(material)).hexdigest()


def claim_or_load_deployment_construction_id(engine: sa.Engine) -> str:
    """Claim the deployment's one opaque construction id, or load the existing one.

    Replicas and a restore of the same history load the same backed-up value; an
    independently initialized deployment mints a different one. There is no
    import-as-new operation pre-demo, so this never merges two values.
    """
    minted = secrets.token_hex(32)
    with engine.begin() as conn:
        _insert_absent(
            conn,
            ne_source_authority_deployment,
            ("singleton",),
            {
                "singleton": 1,
                "construction_id": minted,
                "schema_version": SOURCE_AUTHORITY_SCHEMA_VERSION,
                "created_at": _db_now(conn),
            },
        )
        stored = conn.execute(
            sa.select(ne_source_authority_deployment.c.construction_id)
        ).scalar_one()
    return str(stored)


def bind_deployment_construction_id(engine: sa.Engine) -> str:
    """Claim/load the construction id and refuse a database of foreign history.

    Presenting foreign history is not a recoverable read-time condition: a
    receipt minted under another construction cannot be revalidated here, so
    composition fails instead of silently joining two histories. The Reader
    independently leaves such an authority Unknown, because a root that somehow
    started must still not disclose the foreign row.
    """
    construction_id = claim_or_load_deployment_construction_id(engine)
    with engine.connect() as conn:
        foreign = conn.execute(
            sa.select(sa.func.count())
            .select_from(ne_source_authority_receipts)
            .where(ne_source_authority_receipts.c.construction_id != construction_id)
        ).scalar_one()
    if foreign:
        raise ForeignConstructionIdentityError(
            f"{foreign} retained authority receipt(s) were admitted under a different "
            "deployment construction id. Pre-demo has no import-as-new operation; "
            "restore the matching history or initialize a separate deployment."
        )
    return construction_id


def _receipt_from_payload(payload: Any) -> CurrentSourceAuthorityReceipt:
    """Rebuild a receipt from its stored JSON row.

    ``freshness_sla_seconds`` is a STRICT ``Decimal`` on the frozen contract, and
    ``model_dump(mode="json")`` renders it as a string — so the stored payload is
    not directly re-validatable and an M365 receipt (the only kind that carries an
    SLA) could not be read back at all. Restoring the exact Decimal from its
    canonical text is loss-free: the dump is the decimal's own ``str``.
    """
    if isinstance(payload, Mapping) and payload.get("freshness_sla_seconds") is not None:
        payload = {
            **payload,
            "freshness_sla_seconds": _decimal(payload["freshness_sla_seconds"]),
        }
    return CurrentSourceAuthorityReceipt.model_validate(payload)


def _receipt_id(
    *,
    identity_hash: str,
    source_revision: str,
    contract_epoch: int,
    state: SourceAuthorityState,
    contract_identity_hash: str,
    artifact_id: str,
    acl_snapshot_hash: str,
    authority_as_of: datetime,
) -> str:
    """A deterministic receipt id derived from the OBSERVATION, not the attempt.

    ``authority_as_of`` participates because a receipt id that ignored it would
    make freshness unrefreshable: re-admitting an unchanged point would collapse
    onto the existing row and silently keep its original as-of, so a genuinely
    re-read point would still age out. Idempotency is preserved because the
    producer binds one persisted as-of to one admission attempt — the SAME
    attempt retried presents the SAME instant and therefore the same receipt
    bytes, while a LATER successful re-read presents a later instant, mints a new
    immutable receipt and advances the head.
    """
    return "rcpt-" + _digest(
        "ne.brain.source-authority-receipt-id.v2",
        (
            identity_hash,
            source_revision,
            str(contract_epoch),
            state.value,
            contract_identity_hash,
            artifact_id,
            acl_snapshot_hash,
            authority_as_of.isoformat(),
        ),
    )


def _receipt_matches_stamp(
    receipt: CurrentSourceAuthorityReceipt, stamp: SourceAuthorityStamp
) -> bool:
    """Prove that a retained stamp came from this exact immutable receipt."""
    return (
        receipt.receipt_id == stamp.authority_receipt_id
        and receipt.receipt_hash == stamp.authority_receipt_hash
        and receipt.source_identity == stamp.source_identity
        and receipt.artifact_identity == stamp.artifact_identity
        and receipt.source_revision == stamp.selected_head_revision
        and receipt.source_contract == stamp.source_contract
        and receipt.contract_epoch == stamp.contract_epoch
        and receipt.acl_snapshot_hash == stamp.acl_snapshot_hash
        and receipt.state is SourceAuthorityState.PRESENT
    )


def _same_admitted_truth(
    historical: CurrentSourceAuthorityReceipt,
    current: CurrentSourceAuthorityReceipt,
) -> bool:
    """Compare authority truth while excluding observation/evidence rotation."""
    return (
        historical.receipt_schema_version == current.receipt_schema_version
        and historical.source_identity == current.source_identity
        and historical.artifact_identity == current.artifact_identity
        and historical.source_revision == current.source_revision
        and historical.source_contract == current.source_contract
        and historical.contract_epoch == current.contract_epoch
        and historical.acl_snapshot_hash == current.acl_snapshot_hash
        and historical.state is current.state
        and historical.freshness_sla_seconds == current.freshness_sla_seconds
    )


class SqlAlchemyCurrentSourceAuthorityReader:
    """Read-only current-source evaluator; request composition receives only this.

    It answers *does this authentic historical stamp still name the current
    admitted truth*.  A later permission sweep may mint a new observation receipt
    for byte-identical source/artifact/revision/ACL/contract truth.  That rotates
    freshness evidence, not authority truth, so the older authentic stamp remains
    authorized against the new current receipt.  Any truth-bearing difference
    still withholds it.

    It performs no ACL/ReBAC evaluation and no bounded selection: both are
    015A0-ENFORCEMENT's, composed above this seam, and pretending to do them here
    would be the fail-open shape ADR-0022 §3 rejects.
    """

    def __init__(self, engine: sa.Engine, construction_id: str) -> None:
        self._engine = engine
        self._construction_id = construction_id

    @property
    def construction_id(self) -> str:
        return self._construction_id

    def load_receipt(self, receipt_id: str) -> CurrentSourceAuthorityReceipt | None:
        """Load and revalidate one retained receipt WITHOUT reading the head.

        This is the restart path for an EvidenceLocator: the receipt's id and
        hash are checkable from its own immutable row, so a moved head can never
        make a retained receipt unverifiable.
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    ne_source_authority_receipts.c.payload,
                    ne_source_authority_receipts.c.receipt_hash,
                    ne_source_authority_receipts.c.construction_id,
                ).where(ne_source_authority_receipts.c.receipt_id == receipt_id)
            ).one_or_none()
        if row is None or row.construction_id != self._construction_id:
            return None
        receipt = _receipt_from_payload(row.payload)
        if receipt.receipt_id != receipt_id or receipt.receipt_hash != row.receipt_hash:
            return None
        return receipt

    async def resolve(
        self,
        stamp: SourceAuthorityStamp,
        *,
        principal_id: str,
        access_context_hash: str,
    ) -> SourceAuthorityResolution:
        if not principal_id:
            raise ValueError("principal_id is required")
        if len(access_context_hash) != 64 or not re.fullmatch(r"[0-9a-f]{64}", access_context_hash):
            raise ValueError("access_context_hash must be a sha256 digest")
        return self._resolve(stamp)

    def _resolve(self, stamp: SourceAuthorityStamp) -> SourceAuthorityResolution:
        unknown = SourceAuthorityResolution(decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN)
        with self._engine.connect() as conn:
            head = conn.execute(
                sa.select(ne_source_authority_heads.c.current_receipt_id).where(
                    ne_source_authority_heads.c.identity_hash == stamp.source_identity.identity_hash
                )
            ).one_or_none()
            # Read in the SAME connection as the head: taken apart, the pair could
            # straddle a contract-epoch advance and answer from one side of it.
            governing = conn.execute(
                sa.select(ne_source_authority_contract_epochs.c.contract_epoch).where(
                    ne_source_authority_contract_epochs.c.identity_hash
                    == stamp.source_identity.identity_hash
                )
            ).scalar_one_or_none()
        if head is None:
            return unknown
        if governing is not None and int(governing) != stamp.contract_epoch:
            # The head store can advance a connector scope to a new, not yet
            # re-admitted contract epoch, which the source-authority head alone
            # cannot see: until that advance lands here the head still names an
            # old-epoch receipt and this stamp would still match it. An epoch
            # nothing has been admitted under is UNKNOWN, not a refusal — the
            # source has not been withdrawn, it has not yet been re-read.
            return unknown
        receipt = self.load_receipt(str(head.current_receipt_id))
        if receipt is None:
            return unknown
        if receipt.contract_epoch != stamp.contract_epoch:
            # A contract-epoch advance makes every older receipt Unknown until a
            # successful re-admission, so this is retryable, not a refusal.
            return unknown
        if receipt.state is not SourceAuthorityState.PRESENT:
            return SourceAuthorityResolution(
                decision=SourceAuthorityDecision.PERMANENT_WITHHELD,
                current_receipt=receipt,
                safe_reason="the source is no longer present",
            )
        withheld = SourceAuthorityResolution(
            decision=SourceAuthorityDecision.PERMANENT_WITHHELD,
            current_receipt=receipt,
            safe_reason="the stamp is not the current admitted truth",
        )
        # Validate the presented historical receipt before comparing its truth
        # with the current head.  Merely ignoring receipt-id rotation here would
        # authorize a fabricated stamp that copied visible truth fields but never
        # existed in this deployment's immutable receipt ledger.
        presented = self.load_receipt(stamp.authority_receipt_id)
        if presented is None or not _receipt_matches_stamp(presented, stamp):
            return withheld
        if not _same_admitted_truth(presented, receipt):
            return withheld
        return SourceAuthorityResolution(
            decision=SourceAuthorityDecision.AUTHORIZED, current_receipt=receipt
        )


class SqlAlchemyCurrentSourceAuthorityWriter:
    """Write-only reconciliation authority; never available to API projectors."""

    def __init__(
        self,
        engine: sa.Engine,
        construction_id: str,
        *,
        freshness_sla_seconds: Decimal | None = None,
    ) -> None:
        self._engine = engine
        self._construction_id = construction_id
        self._freshness_sla_seconds = freshness_sla_seconds

    async def claim(
        self,
        scope: SourceReconciliationScope,
        *,
        expected_head_revision: str | None,
        lease_seconds: int,
    ) -> SourceReconciliationClaim:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        token = uuid.uuid4().hex
        with self._engine.begin() as conn:
            now = _db_now(conn)
            expires = now + timedelta(seconds=lease_seconds)
            existing = conn.execute(
                sa.select(ne_source_reconciliation_claims).where(
                    ne_source_reconciliation_claims.c.scope_hash == scope.scope_hash
                )
            ).one_or_none()
            if existing is None:
                inserted = _insert_absent(
                    conn,
                    ne_source_reconciliation_claims,
                    ("scope_hash",),
                    {
                        "scope_hash": scope.scope_hash,
                        "reconciliation_epoch": 1,
                        "fencing_token": token,
                        "lease_expires_at": expires,
                        "head_revision": expected_head_revision,
                    },
                )
                if not inserted:
                    raise SourceAuthorityClaimConflictError(
                        "another reconciler claimed this scope first"
                    )
                epoch = 1
            else:
                if _utc(existing.lease_expires_at) > now:
                    raise SourceReconciliationScopeBusyError(
                        "the reconciliation scope is held by a live lease"
                    )
                if existing.head_revision != expected_head_revision:
                    raise SourceAuthorityClaimConflictError(
                        "the expected head revision moved under this claim"
                    )
                epoch = int(existing.reconciliation_epoch) + 1
                updated = conn.execute(
                    sa.update(ne_source_reconciliation_claims)
                    .where(
                        ne_source_reconciliation_claims.c.scope_hash == scope.scope_hash,
                        ne_source_reconciliation_claims.c.reconciliation_epoch
                        == existing.reconciliation_epoch,
                        ne_source_reconciliation_claims.c.fencing_token == existing.fencing_token,
                    )
                    .values(
                        reconciliation_epoch=epoch,
                        fencing_token=token,
                        lease_expires_at=expires,
                    )
                ).rowcount
                if updated != 1:
                    raise SourceAuthorityClaimConflictError(
                        "another reconciler claimed this scope first"
                    )
        return SourceReconciliationClaim(
            scope=scope,
            reconciliation_epoch=epoch,
            expected_head_revision=expected_head_revision,
            lease_expires_at=expires,
            fencing_token=token,
        )

    async def renew(
        self, claim: SourceReconciliationClaim, *, lease_seconds: int
    ) -> SourceReconciliationClaim:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._engine.begin() as conn:
            expires = _db_now(conn) + timedelta(seconds=lease_seconds)
            if expires < claim.lease_expires_at:
                expires = claim.lease_expires_at
            updated = conn.execute(
                sa.update(ne_source_reconciliation_claims)
                .where(
                    ne_source_reconciliation_claims.c.scope_hash == claim.scope.scope_hash,
                    ne_source_reconciliation_claims.c.reconciliation_epoch
                    == claim.reconciliation_epoch,
                    ne_source_reconciliation_claims.c.fencing_token == claim.fencing_token,
                )
                .values(lease_expires_at=expires)
            ).rowcount
        if updated != 1:
            raise SourceAuthorityClaimConflictError("this claim no longer holds the scope")
        return claim.model_copy(update={"lease_expires_at": expires})

    async def release(self, claim: SourceReconciliationClaim) -> bool:
        """End this claim's lease so the scope is immediately re-claimable.

        Without it a finished reconciliation would hold its scope for the whole
        remaining lease. It is CAS'd on the exact epoch and fencing token, so a
        superseded holder cannot release the scope out from under its successor.
        """
        with self._engine.begin() as conn:
            released = conn.execute(
                sa.update(ne_source_reconciliation_claims)
                .where(
                    ne_source_reconciliation_claims.c.scope_hash == claim.scope.scope_hash,
                    ne_source_reconciliation_claims.c.reconciliation_epoch
                    == claim.reconciliation_epoch,
                    ne_source_reconciliation_claims.c.fencing_token == claim.fencing_token,
                )
                .values(lease_expires_at=_db_now(conn))
            ).rowcount
        return released == 1

    async def finalize(
        self,
        claim: SourceReconciliationClaim,
        fence: SourceAdmissionFence,
        candidate: SourceAuthorityCandidate | SourceDeletionObservation,
    ) -> CurrentSourceAuthorityReceipt:
        self._check_fence(claim, fence, candidate)
        with self._engine.begin() as conn:
            self._require_live_claim(conn, claim)
            now = _db_now(conn)
            authority_as_of = (
                candidate.authority_as_of
                if isinstance(candidate, SourceAuthorityCandidate)
                else candidate.observed_at
            )
            if authority_as_of > now:
                raise SourceAuthorityFenceError(
                    "a future-skewed authority observation cannot be admitted"
                )
            previous = self._current_receipt(conn, candidate.source_identity.identity_hash)
            receipt = self._build_receipt(candidate, previous=previous, admitted_at=now)
            stored = self._append_receipt(conn, receipt)
            self._advance_head(conn, stored, previous=previous, now=now)
            # The claim row remembers the head this scope now sits at, so the NEXT
            # claim must present it. Without this the expected-head CAS could only
            # ever hold the value it was first inserted with.
            conn.execute(
                sa.update(ne_source_reconciliation_claims)
                .where(
                    ne_source_reconciliation_claims.c.scope_hash == claim.scope.scope_hash,
                    ne_source_reconciliation_claims.c.reconciliation_epoch
                    == claim.reconciliation_epoch,
                    ne_source_reconciliation_claims.c.fencing_token == claim.fencing_token,
                )
                .values(head_revision=stored.source_revision)
            )
        return stored

    # -- internals ---------------------------------------------------------

    def _check_fence(
        self,
        claim: SourceReconciliationClaim,
        fence: SourceAdmissionFence,
        candidate: SourceAuthorityCandidate | SourceDeletionObservation,
    ) -> None:
        if fence.reconciliation_scope != claim.scope:
            raise SourceAuthorityFenceError("the fence authorizes another reconciliation scope")
        if fence.reconciliation_epoch != claim.reconciliation_epoch:
            raise SourceAuthorityFenceError("the fence carries a stale reconciliation epoch")
        if fence.fencing_token != claim.fencing_token:
            raise SourceAuthorityFenceError("the fence carries another claim's fencing token")
        identity = candidate.source_identity
        if fence.deployment_id != identity.deployment_id:
            raise SourceAuthorityFenceError("the fence authorizes another deployment")
        if fence.connector_instance_id != identity.connector_instance_id:
            raise SourceAuthorityFenceError("the fence authorizes another connector instance")
        if fence.source_generation != identity.generation:
            raise SourceAuthorityFenceError("the fence authorizes another source generation")
        if fence.contract_epoch != candidate.contract_epoch:
            raise SourceAuthorityFenceError("the fence authorizes another contract epoch")

    def _require_live_claim(self, conn: sa.Connection, claim: SourceReconciliationClaim) -> None:
        row = conn.execute(
            sa.select(
                ne_source_reconciliation_claims.c.reconciliation_epoch,
                ne_source_reconciliation_claims.c.fencing_token,
                ne_source_reconciliation_claims.c.lease_expires_at,
            ).where(ne_source_reconciliation_claims.c.scope_hash == claim.scope.scope_hash)
        ).one_or_none()
        if (
            row is None
            or row.reconciliation_epoch != claim.reconciliation_epoch
            or row.fencing_token != claim.fencing_token
        ):
            raise SourceAuthorityFenceError("this claim no longer holds the reconciliation scope")
        if _utc(row.lease_expires_at) <= _db_now(conn):
            raise SourceAuthorityFenceError("the reconciliation lease has expired")

    def _current_receipt(
        self, conn: sa.Connection, identity_hash: str
    ) -> CurrentSourceAuthorityReceipt | None:
        row = conn.execute(
            sa.select(ne_source_authority_receipts.c.payload)
            .select_from(
                ne_source_authority_heads.join(
                    ne_source_authority_receipts,
                    ne_source_authority_heads.c.current_receipt_id
                    == ne_source_authority_receipts.c.receipt_id,
                )
            )
            .where(ne_source_authority_heads.c.identity_hash == identity_hash)
        ).one_or_none()
        if row is None:
            return None
        return _receipt_from_payload(row.payload)

    def _build_receipt(
        self,
        candidate: SourceAuthorityCandidate | SourceDeletionObservation,
        *,
        previous: CurrentSourceAuthorityReceipt | None,
        admitted_at: datetime,
    ) -> CurrentSourceAuthorityReceipt:
        if isinstance(candidate, SourceAuthorityCandidate):
            state = candidate.state
            artifact = candidate.artifact_identity
            acl_snapshot_hash = candidate.acl_snapshot_hash
            authority_as_of = candidate.authority_as_of
        else:
            state = SourceAuthorityState.DELETED
            artifact = None
            # An identity-bearing deletion carries the last successfully admitted
            # ACL envelope forward: a deletion is not an ACL change, and minting a
            # blank one here would silently widen the recorded snapshot.
            acl_snapshot_hash = previous.acl_snapshot_hash if previous else _EMPTY_ACL_HASH
            authority_as_of = candidate.observed_at
        identity = candidate.source_identity
        sla = self._freshness_sla_seconds if identity.source_kind == "m365-sharepoint" else None
        if identity.source_kind == "m365-sharepoint" and sla is None:
            raise SourceAuthorityError(
                "an M365 authority admission requires a fixed freshness SLA on the writer"
            )
        receipt_id = _receipt_id(
            identity_hash=identity.identity_hash,
            source_revision=candidate.source_revision,
            contract_epoch=candidate.contract_epoch,
            state=state,
            contract_identity_hash=candidate.source_contract.identity_hash,
            artifact_id="" if artifact is None else artifact.artifact_id,
            acl_snapshot_hash=acl_snapshot_hash,
            authority_as_of=authority_as_of,
        )
        return CurrentSourceAuthorityReceipt(
            receipt_id=receipt_id,
            source_identity=identity,
            artifact_identity=artifact,
            source_revision=candidate.source_revision,
            source_contract=candidate.source_contract,
            contract_epoch=candidate.contract_epoch,
            acl_snapshot_hash=acl_snapshot_hash,
            state=state,
            admitted_at=admitted_at,
            authority_as_of=authority_as_of,
            audit_event_id="evt-"
            + _digest("ne.brain.source-authority-audit-event.v1", (receipt_id,)),
            lineage_id="lin-" + _digest("ne.brain.source-authority-lineage.v1", (receipt_id,)),
            freshness_sla_seconds=sla,
        )

    def _append_receipt(
        self, conn: sa.Connection, receipt: CurrentSourceAuthorityReceipt
    ) -> CurrentSourceAuthorityReceipt:
        """Insert-if-absent; a retry loads the identical bytes instead of rewriting."""
        _insert_absent(
            conn,
            ne_source_authority_receipts,
            ("receipt_id",),
            {
                "receipt_id": receipt.receipt_id,
                "construction_id": self._construction_id,
                "identity_hash": receipt.source_identity.identity_hash,
                "artifact_id": None
                if receipt.artifact_identity is None
                else receipt.artifact_identity.artifact_id,
                "receipt_hash": receipt.receipt_hash,
                "state": receipt.state.value,
                "source_revision": receipt.source_revision,
                "contract_epoch": receipt.contract_epoch,
                "admitted_at": receipt.admitted_at,
                "payload": receipt.model_dump(mode="json"),
            },
        )
        payload = conn.execute(
            sa.select(ne_source_authority_receipts.c.payload).where(
                ne_source_authority_receipts.c.receipt_id == receipt.receipt_id
            )
        ).scalar_one()
        return _receipt_from_payload(payload)

    def _advance_head(
        self,
        conn: sa.Connection,
        receipt: CurrentSourceAuthorityReceipt,
        *,
        previous: CurrentSourceAuthorityReceipt | None,
        now: datetime,
    ) -> None:
        identity_hash = receipt.source_identity.identity_hash
        values = {
            "identity_hash": identity_hash,
            "generation": receipt.source_identity.generation,
            "contract_epoch": receipt.contract_epoch,
            "head_revision": receipt.source_revision,
            "current_receipt_id": receipt.receipt_id,
            "updated_at": now,
        }
        if previous is None:
            inserted = _insert_absent(conn, ne_source_authority_heads, ("identity_hash",), values)
            if not inserted:
                raise SourceAuthorityClaimConflictError("another admission moved this head first")
            return
        if receipt.receipt_id == previous.receipt_id:
            return
        if receipt.contract_epoch < previous.contract_epoch:
            raise SourceAuthorityError("an out-of-order authority admission cannot move the head")
        # Source GENERATION is deliberately not compared here: it is part of
        # `identity_hash`, so a generation change is a different head row entirely
        # and this branch could only ever compare a value to itself. A guard that
        # can only take one value reads as coverage and provides none.
        moved = conn.execute(
            sa.update(ne_source_authority_heads)
            .where(
                ne_source_authority_heads.c.identity_hash == identity_hash,
                ne_source_authority_heads.c.current_receipt_id == previous.receipt_id,
            )
            .values({k: v for k, v in values.items() if k != "identity_hash"})
        ).rowcount
        if moved != 1:
            raise SourceAuthorityClaimConflictError("another admission moved this head first")


def _insert_absent(
    conn: sa.Connection,
    table: sa.Table,
    conflict_columns: Sequence[str],
    values: dict[str, Any],
) -> bool:
    """Dialect-correct INSERT ... ON CONFLICT DO NOTHING; True iff a row was inserted.

    ``RETURNING`` rather than ``rowcount``: psycopg reports -1 for an
    ON CONFLICT DO NOTHING insert, so a rowcount test silently reads every insert
    as a loss on PostgreSQL — the exact database the arbitration proofs run on.
    """
    first = conflict_columns[0]
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
    return conn.execute(statement.returning(table.c[first])).first() is not None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class ContinuationTokenSyntaxError(ValueError):
    """A presented token is not a canonical ``<key-id>.<43-char base64url MAC>``."""


class HmacAuthorizedContinuationTokenCodec:
    """Deterministic HMAC token codec; ``decode`` yields only an opaque locator.

    The canonical continuation state never travels in the token: the token IS the
    authenticated lookup key for its private durable row. ``decode`` therefore
    performs no authentication at all — it parses syntax and returns the key
    id/MAC locator, and the store below claim/loads the row before re-deriving
    the MAC from it.
    """

    def __init__(self, key_id: str, key: bytes) -> None:
        if not _KEY_ID_PATTERN.fullmatch(key_id):
            raise ValueError("continuation key id must match [A-Za-z0-9_-]{1,128}")
        if len(key) < _MINIMUM_CONTINUATION_KEY_BYTES:
            raise ValueError("continuation key must be at least 256 bits")
        self._key_id = key_id
        self._key = bytes(key)

    @property
    def key_id(self) -> str:
        return self._key_id

    def encode(self, state: AuthorizedContinuationState) -> str:
        return f"{self._key_id}.{_b64url(self._mac(state))}"

    def token_mac(self, state: AuthorizedContinuationState) -> str:
        """The hex MAC this replica derives for ``state`` under its current key."""
        return self._mac(state).hex()

    def decode(self, token: str) -> AuthorizedContinuationTokenLocator:
        key_id, separator, mac = token.partition(".")
        if not separator or "." in mac:
            raise ContinuationTokenSyntaxError("continuation token is not canonical")
        if not _KEY_ID_PATTERN.fullmatch(key_id) or not _MAC_PATTERN.fullmatch(mac):
            raise ContinuationTokenSyntaxError("continuation token is not canonical")
        raw = base64.urlsafe_b64decode(mac + "=")
        if _b64url(raw) != mac:
            # A non-canonical base64 alias decodes to the same bytes; accepting it
            # would give one row two distinct presented tokens.
            raise ContinuationTokenSyntaxError("continuation token is not canonical")
        return AuthorizedContinuationTokenLocator(key_id=key_id, token_mac=raw.hex())

    def _mac(self, state: AuthorizedContinuationState) -> bytes:
        return hmac.new(self._key, _state_preimage(state), hashlib.sha256).digest()


def _state_preimage(state: AuthorizedContinuationState) -> bytes:
    fields = (
        str(CONTINUATION_CODEC_VERSION),
        str(state.state_version),
        state.principal_access_hash,
        state.request_hash,
        str(state.normalizer_version),
        str(state.encoding_version),
        str(state.requested_top_k),
        str(state.scan_limit),
        state.config_hash,
        state.contract_head_hash,
        format(state.cursor_score, "f"),
        state.cursor_artifact_id,
        state.prior_page_set_hash,
        state.expires_at.isoformat(),
    )
    material = bytearray(b"ne.brain.authorized-continuation.v1")
    material.append(0x1E)
    for field in fields:
        encoded = field.encode("utf-8")
        material.extend(len(encoded).to_bytes(8, "big"))
        material.extend(encoded)
    return bytes(material)


def _constant_time_equals(left: str, right: str) -> bool:
    """Module-level so the claim/load ordering is observable to its own proof."""
    return hmac.compare_digest(left, right)


class SqlAlchemyAuthorizedContinuationStore:
    """Private continuation/replay rows plus the claim/load primitive above them.

    ``CurrentAuthorizedArtifactSelector`` is 015A0-ENFORCEMENT's; this ships only
    the rows and the ordered claim/load it calls. The ordering is load-bearing:
    the MAC is re-derived from the row that was actually loaded, so a row whose
    state columns were edited underneath the deployment fails authentication
    instead of being served.
    """

    def __init__(self, engine: sa.Engine, codec: HmacAuthorizedContinuationTokenCodec) -> None:
        self._engine = engine
        self._codec = codec
        self._row_lookups = 0

    @property
    def codec(self) -> HmacAuthorizedContinuationTokenCodec:
        return self._codec

    @property
    def row_lookup_count(self) -> int:
        """Private-row lookups performed by this instance (the kit's counter)."""
        return self._row_lookups

    def persist(self, state: AuthorizedContinuationState) -> str:
        """Store the private canonical state and return its opaque token."""
        token = self._codec.encode(state)
        mac = self._codec.token_mac(state)
        with self._engine.begin() as conn:
            _insert_absent(
                conn,
                ne_authorized_continuation_states,
                ("key_id", "token_mac"),
                {
                    "key_id": self._codec.key_id,
                    "token_mac": mac,
                    "state_version": state.state_version,
                    "principal_access_hash": state.principal_access_hash,
                    "request_hash": state.request_hash,
                    "normalizer_version": state.normalizer_version,
                    "encoding_version": state.encoding_version,
                    "requested_top_k": state.requested_top_k,
                    "scan_limit": state.scan_limit,
                    "config_hash": state.config_hash,
                    "contract_head_hash": state.contract_head_hash,
                    "cursor_score": format(state.cursor_score, "f"),
                    "cursor_artifact_id": state.cursor_artifact_id,
                    "prior_page_set_hash": state.prior_page_set_hash,
                    "expires_at": state.expires_at,
                },
            )
        return token

    def claim_load(self, token: str) -> AuthorizedContinuationState:
        """Claim/load the exact private row, THEN authenticate its MAC.

        A token whose key id is not this replica's fails before any lookup — the
        rotation boundary is a drain, not a per-row grace period. Every other
        unusable token performs exactly one lookup and raises the one shared
        opaque outcome.
        """
        try:
            locator = self._codec.decode(token)
        except ContinuationTokenSyntaxError as exc:
            raise ContinuationUnavailableError(_CONTINUATION_UNAVAILABLE) from exc
        if locator.key_id != self._codec.key_id:
            raise ContinuationUnavailableError(_CONTINUATION_UNAVAILABLE)
        self._row_lookups += 1
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_authorized_continuation_states).where(
                    ne_authorized_continuation_states.c.key_id == locator.key_id,
                    ne_authorized_continuation_states.c.token_mac == locator.token_mac,
                )
            ).one_or_none()
            if row is None:
                raise ContinuationUnavailableError(_CONTINUATION_UNAVAILABLE)
            now = _db_now(conn)
        state = AuthorizedContinuationState(
            state_version=row.state_version,
            principal_access_hash=row.principal_access_hash,
            request_hash=row.request_hash,
            normalizer_version=row.normalizer_version,
            encoding_version=row.encoding_version,
            requested_top_k=row.requested_top_k,
            scan_limit=row.scan_limit,
            config_hash=row.config_hash,
            contract_head_hash=row.contract_head_hash,
            cursor_score=_decimal(row.cursor_score),
            cursor_artifact_id=row.cursor_artifact_id,
            prior_page_set_hash=row.prior_page_set_hash,
            expires_at=_utc(row.expires_at),
        )
        if not _constant_time_equals(self._codec.token_mac(state), locator.token_mac):
            raise ContinuationUnavailableError(_CONTINUATION_UNAVAILABLE)
        if state.expires_at <= now:
            raise ContinuationUnavailableError(_CONTINUATION_UNAVAILABLE)
        return state

    def persist_replay(self, replay: AuthorizedContinuationReplay) -> str:
        """Record the immutable first-resolution integrity row; return its hash.

        A second resolution of the same token does NOT overwrite it: the stored
        hash is the first resolution's, and a later divergent one is detected by
        :meth:`replay_matches` rather than silently replacing history.
        """
        with self._engine.begin() as conn:
            _insert_absent(
                conn,
                ne_authorized_continuation_replays,
                ("token_mac",),
                {
                    "token_mac": replay.token_mac,
                    "replay_hash": replay.replay_hash,
                    "status": replay.status.value,
                    "scanned_count": replay.scanned_count,
                    "returned_count": replay.returned_count,
                    "scan_limit": replay.scan_limit,
                    "scope_health_epoch": replay.scope_health_epoch,
                    "next_state_hash": replay.next_state_hash,
                    "created_at": _db_now(conn),
                },
            )
            stored = conn.execute(
                sa.select(ne_authorized_continuation_replays.c.replay_hash).where(
                    ne_authorized_continuation_replays.c.token_mac == replay.token_mac
                )
            ).scalar_one()
        return str(stored)

    def replay_matches(self, replay: AuthorizedContinuationReplay) -> bool:
        """True iff a first-resolution row exists and this replay reproduces it."""
        with self._engine.connect() as conn:
            stored = conn.execute(
                sa.select(ne_authorized_continuation_replays.c.replay_hash).where(
                    ne_authorized_continuation_replays.c.token_mac == replay.token_mac
                )
            ).scalar_one_or_none()
        return stored is not None and _constant_time_equals(str(stored), replay.replay_hash)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))
