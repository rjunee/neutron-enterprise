# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-M365: the producer-side admission coordinator's own rows.

The exclusive claim, its lease and the receipt store are AUTHORITY-CORE's and are
proven there. What this file proves is the part CORE deliberately does not own:
the connector-scope contract head and its epoch, the descendant ledger that makes
"absence is not deletion" enforceable, the durable deletion stage machine, the
persisted connector-instance binding, and the sub-SLA sweep arithmetic.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from ne.connect.authority_admission import (
    AdmissionStage,
    AdmissionStageError,
    AuthorityAdmissionError,
    ConnectorInstanceRebindError,
    ContractEpochUnresolvedError,
    OverlappingDriveScopeError,
    PartialPermissionReadError,
    PermissionReadEvidence,
    SqlAlchemyAdmissionAttemptStore,
    SqlAlchemyHierarchicalAdmission,
    SqlAlchemySourceContractHeadStore,
    SqlAlchemySweepCursor,
    SweepCadenceError,
    SweepCadencePlan,
    UnreconciledDescendantError,
    UnsweptPointError,
    claim_or_load_connector_instance,
    connector_instance_scope,
    drive_scope,
    ensure_admission_schema,
    expected_scope_head_revision,
    ne_source_contract_heads,
    observed_permissions,
    reject_overlapping_drive_scopes,
    site_scope,
)
from ne.connect.salesforce.authority import salesforce_connector_scope, salesforce_object_scope
from ne.contracts.source_authority import SourceContractIdentity, SourceReconciliationScope

DEPLOYMENT = "deployment-alpha"
INSTANCE = "conn-instance-1"
TENANT = "tenant-guid"
ORGANIZATION = "00D000000000001AAA"


def memory_engine() -> sa.Engine:
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    ensure_admission_schema(engine)
    return engine


def contract(
    descriptor_hash: str = "a" * 64,
    *,
    version: int = 1,
    source_kind: str = "m365-sharepoint",
) -> SourceContractIdentity:
    return SourceContractIdentity(
        source_kind=source_kind,
        descriptor_schema_version=version,
        descriptor_hash=descriptor_hash,
    )


def scope() -> SourceReconciliationScope:
    return connector_instance_scope(
        deployment_id=DEPLOYMENT, connector_instance_id=INSTANCE, graph_tenant_id=TENANT
    )


# -- the connector-scope contract head ---------------------------------------


def test_first_claim_starts_at_epoch_one_and_is_unresolved() -> None:
    store = SqlAlchemySourceContractHeadStore(memory_engine())
    head = store.claim_or_load(scope(), source_generation=1, contract=contract())
    assert head.contract_epoch == 1
    assert head.source_contract.identity_hash == contract().identity_hash
    with pytest.raises(ContractEpochUnresolvedError):
        store.require_resolved(scope())


def test_reloading_an_unchanged_contract_hash_keeps_the_same_epoch() -> None:
    store = SqlAlchemySourceContractHeadStore(memory_engine())
    first = store.claim_or_load(scope(), source_generation=1, contract=contract())
    second = store.claim_or_load(scope(), source_generation=1, contract=contract())
    assert (second.contract_epoch, second.head_revision) == (
        first.contract_epoch,
        first.head_revision,
    )


def test_a_changed_contract_hash_advances_the_epoch_and_clears_resolved() -> None:
    store = SqlAlchemySourceContractHeadStore(memory_engine())
    first = store.claim_or_load(scope(), source_generation=1, contract=contract())
    assert store.mark_resolved(first) is True
    assert store.is_resolved(scope()) is True

    second = store.claim_or_load(scope(), source_generation=1, contract=contract("b" * 64))
    assert second.contract_epoch == first.contract_epoch + 1
    assert second.head_revision != first.head_revision
    assert store.is_resolved(scope()) is False


def test_a_superseded_epoch_cannot_resolve_the_epoch_that_replaced_it() -> None:
    store = SqlAlchemySourceContractHeadStore(memory_engine())
    stale = store.claim_or_load(scope(), source_generation=1, contract=contract())
    store.claim_or_load(scope(), source_generation=1, contract=contract("b" * 64))
    assert store.mark_resolved(stale) is False
    assert store.is_resolved(scope()) is False


def test_a_descriptor_schema_version_change_alone_advances_the_epoch() -> None:
    store = SqlAlchemySourceContractHeadStore(memory_engine())
    first = store.claim_or_load(scope(), source_generation=1, contract=contract())
    second = store.claim_or_load(scope(), source_generation=1, contract=contract(version=2))
    assert second.contract_epoch == first.contract_epoch + 1
    assert second.source_contract.descriptor_schema_version == 2


def test_changing_the_admitted_generation_under_one_scope_is_refused() -> None:
    store = SqlAlchemySourceContractHeadStore(memory_engine())
    store.claim_or_load(scope(), source_generation=1, contract=contract())
    with pytest.raises(AuthorityAdmissionError):
        store.claim_or_load(scope(), source_generation=2, contract=contract())


def test_require_resolved_returns_the_head_once_an_admission_succeeded() -> None:
    store = SqlAlchemySourceContractHeadStore(memory_engine())
    head = store.claim_or_load(scope(), source_generation=1, contract=contract())
    store.mark_resolved(head)
    assert store.require_resolved(scope()).head_revision == head.head_revision


def test_two_stores_over_one_database_load_the_identical_head() -> None:
    engine = memory_engine()
    first = SqlAlchemySourceContractHeadStore(engine).claim_or_load(
        scope(), source_generation=1, contract=contract()
    )
    second = SqlAlchemySourceContractHeadStore(engine).claim_or_load(
        scope(), source_generation=1, contract=contract()
    )
    assert first == second


def test_insert_if_absent_reports_the_loser_rather_than_a_psycopg_rowcount() -> None:
    """The claim-or-load primitive must distinguish inserting from loading.

    Every claim-or-load above collapses to "the row exists"; this locks the
    underlying insert-if-absent primitive on the engine under test, because a
    version that always reports success would still satisfy them.
    """
    from ne.connect.authority_admission import _db_now, _insert_absent

    engine = memory_engine()
    with engine.begin() as conn:
        values = {
            "scope_hash": "f" * 64,
            "scope_path": ["m365-sharepoint"],
            "source_kind": "m365-sharepoint",
            "source_generation": 1,
            "contract_epoch": 1,
            "contract_identity_hash": "a" * 64,
            "descriptor_schema_version": 1,
            "descriptor_hash": "a" * 64,
            "head_revision": "chead-x",
            "resolved": False,
            "updated_at": _db_now(conn),
        }
        assert _insert_absent(conn, ne_source_contract_heads, ("scope_hash",), values) is True
        assert _insert_absent(conn, ne_source_contract_heads, ("scope_hash",), values) is False


# -- scopes and the overlapping-drive refusal --------------------------------


def test_one_drive_has_one_scope_whichever_route_reached_it() -> None:
    direct = drive_scope(
        deployment_id=DEPLOYMENT,
        connector_instance_id=INSTANCE,
        graph_tenant_id=TENANT,
        drive_id="drive-1",
    )
    assert (
        direct.scope_hash
        == drive_scope(
            deployment_id=DEPLOYMENT,
            connector_instance_id=INSTANCE,
            graph_tenant_id=TENANT,
            drive_id="drive-1",
        ).scope_hash
    )
    other = drive_scope(
        deployment_id=DEPLOYMENT,
        connector_instance_id=INSTANCE,
        graph_tenant_id=TENANT,
        drive_id="drive-2",
    )
    assert direct.scope_hash != other.scope_hash


def test_a_site_scope_is_not_a_drive_scope_of_the_same_id() -> None:
    site = site_scope(
        deployment_id=DEPLOYMENT,
        connector_instance_id=INSTANCE,
        graph_tenant_id=TENANT,
        site_id="shared",
    )
    drive = drive_scope(
        deployment_id=DEPLOYMENT,
        connector_instance_id=INSTANCE,
        graph_tenant_id=TENANT,
        drive_id="shared",
    )
    assert site.scope_hash != drive.scope_hash


def test_a_drive_reached_directly_and_by_discovery_is_refused() -> None:
    with pytest.raises(OverlappingDriveScopeError):
        reject_overlapping_drive_scopes(
            configured_drive_ids=["drive-1"], discovered_drive_ids=["drive-1", "drive-2"]
        )


def test_disjoint_direct_and_discovered_drives_are_accepted() -> None:
    reject_overlapping_drive_scopes(
        configured_drive_ids=["drive-1"], discovered_drive_ids=["drive-2"]
    )


def test_overlap_is_exact_identity_not_a_prefix_or_substring_match() -> None:
    reject_overlapping_drive_scopes(
        configured_drive_ids=["drive-1"], discovered_drive_ids=["drive-10", "1"]
    )


# -- the descendant ledger ---------------------------------------------------


def _site() -> SourceReconciliationScope:
    return site_scope(
        deployment_id=DEPLOYMENT,
        connector_instance_id=INSTANCE,
        graph_tenant_id=TENANT,
        site_id="site-eng",
    )


def _drive(drive_id: str) -> SourceReconciliationScope:
    return drive_scope(
        deployment_id=DEPLOYMENT,
        connector_instance_id=INSTANCE,
        graph_tenant_id=TENANT,
        drive_id=drive_id,
    )


def test_the_union_is_known_descendants_plus_newly_discovered_ones() -> None:
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    hierarchy.record_descendants(_site(), [("drive-1", _drive("drive-1"))])
    union = hierarchy.require_reconciled_union(_site(), discovered_keys=["drive-1", "drive-2"])
    assert union == ("drive-1", "drive-2")


def test_an_omitted_previously_admitted_descendant_blocks_the_parent() -> None:
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    hierarchy.record_descendants(
        _site(), [("drive-1", _drive("drive-1")), ("drive-2", _drive("drive-2"))]
    )
    with pytest.raises(UnreconciledDescendantError):
        hierarchy.require_reconciled_union(_site(), discovered_keys=["drive-1"])


def test_only_an_identity_bearing_retirement_unblocks_the_parent() -> None:
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    hierarchy.record_descendants(
        _site(), [("drive-1", _drive("drive-1")), ("drive-2", _drive("drive-2"))]
    )
    assert (
        hierarchy.retire_descendant(_site(), _drive("drive-2"), retirement_revision="etag-9")
        is True
    )
    assert hierarchy.require_reconciled_union(_site(), discovered_keys=["drive-1"]) == ("drive-1",)


def test_a_retirement_without_a_revision_is_unrepresentable() -> None:
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    hierarchy.record_descendants(_site(), [("drive-1", _drive("drive-1"))])
    with pytest.raises(ValueError):
        hierarchy.retire_descendant(_site(), _drive("drive-1"), retirement_revision="")


def test_a_later_discovery_listing_does_not_revive_a_retired_descendant() -> None:
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    hierarchy.record_descendants(_site(), [("drive-1", _drive("drive-1"))])
    hierarchy.retire_descendant(_site(), _drive("drive-1"), retirement_revision="etag-9")
    hierarchy.record_descendants(_site(), [("drive-1", _drive("drive-1"))])
    assert hierarchy.known_descendant_keys(_site()) == ()


def test_descendants_are_scoped_to_their_own_parent() -> None:
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    other = site_scope(
        deployment_id=DEPLOYMENT,
        connector_instance_id=INSTANCE,
        graph_tenant_id=TENANT,
        site_id="site-hr",
    )
    hierarchy.record_descendants(_site(), [("drive-1", _drive("drive-1"))])
    assert hierarchy.known_descendant_keys(other) == ()
    hierarchy.require_reconciled_union(other, discovered_keys=[])


# -- the per-point sweep ledger ----------------------------------------------


def _read(item_id: str, *, drive_id: str = "drive-1") -> PermissionReadEvidence:
    return observed_permissions(
        drive_id=drive_id,
        item_id=item_id,
        principals=("u-ada",),
        pages_read=1,
        collection_complete=True,
    )


def test_a_registered_point_is_unswept_until_this_epoch_re_reads_it() -> None:
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    hierarchy.register_points(_drive("drive-1"), [("a" * 64, "item-a"), ("b" * 64, "item-b")])
    assert hierarchy.unswept_point_hashes(_drive("drive-1"), epoch=1) == ("a" * 64, "b" * 64)
    hierarchy.mark_point_swept(
        _drive("drive-1"), "a" * 64, epoch=1, graph_item_id="item-a", evidence=_read("item-a")
    )
    assert hierarchy.unswept_point_hashes(_drive("drive-1"), epoch=1) == ("b" * 64,)


def test_a_new_epoch_makes_every_point_unswept_again() -> None:
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    hierarchy.register_points(_drive("drive-1"), [("a" * 64, "item-a")])
    hierarchy.mark_point_swept(
        _drive("drive-1"), "a" * 64, epoch=1, graph_item_id="item-a", evidence=_read("item-a")
    )
    assert hierarchy.unswept_point_hashes(_drive("drive-1"), epoch=1) == ()
    assert hierarchy.unswept_point_hashes(_drive("drive-1"), epoch=2) == ("a" * 64,)


def test_a_retired_point_no_longer_blocks_a_sweep() -> None:
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    hierarchy.register_points(_drive("drive-1"), [("a" * 64, "item-a")])
    assert hierarchy.retire_point(_drive("drive-1"), "a" * 64) is True
    assert hierarchy.unswept_point_hashes(_drive("drive-1"), epoch=9) == ()
    assert hierarchy.sweepable_points(_drive("drive-1")) == ()


def test_a_stale_epoch_cannot_lower_a_points_swept_watermark() -> None:
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    hierarchy.mark_point_swept(
        _drive("drive-1"), "a" * 64, epoch=5, graph_item_id="item-a", evidence=_read("item-a")
    )
    hierarchy.mark_point_swept(
        _drive("drive-1"), "a" * 64, epoch=2, graph_item_id="item-a", evidence=_read("item-a")
    )
    assert hierarchy.unswept_point_hashes(_drive("drive-1"), epoch=5) == ()


def test_a_point_is_addressable_by_its_graph_item_id_in_deterministic_order() -> None:
    """A stale point the sweep cannot address is a point it can never refresh."""
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    hierarchy.register_points(_drive("drive-1"), [("b" * 64, "item-b"), ("a" * 64, "item-a")])
    assert hierarchy.sweepable_points(_drive("drive-1")) == (
        ("a" * 64, "item-a"),
        ("b" * 64, "item-b"),
    )


def test_a_point_swept_for_another_item_is_refused() -> None:
    """Evidence read for one item cannot mark a different item swept."""
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    with pytest.raises(AuthorityAdmissionError):
        hierarchy.mark_point_swept(
            _drive("drive-1"),
            "a" * 64,
            epoch=1,
            graph_item_id="item-a",
            evidence=_read("item-other"),
        )


# -- the durable deletion stage machine --------------------------------------


def _attempt_store() -> SqlAlchemyAdmissionAttemptStore:
    return SqlAlchemyAdmissionAttemptStore(memory_engine())


def _claim(store: SqlAlchemyAdmissionAttemptStore, attempt_id: str = "adm-1") -> None:
    store.claim(
        attempt_id=attempt_id,
        scope=_drive("drive-1"),
        identity_hash="a" * 64,
        source_revision="etag-1",
        contract_epoch=1,
        prior_artifact_id="c" * 64,
    )


def test_a_claimed_attempt_starts_at_the_claimed_stage() -> None:
    store = _attempt_store()
    _claim(store)
    attempt = store.inspect("adm-1")
    assert attempt is not None
    assert attempt.stage is AdmissionStage.CLAIMED
    assert attempt.prior_artifact_id == "c" * 64


def test_re_claiming_loads_the_same_attempt_rather_than_restarting_it() -> None:
    store = _attempt_store()
    _claim(store)
    store.advance("adm-1", AdmissionStage.DELETION_VALIDATED)
    _claim(store)
    attempt = store.inspect("adm-1")
    assert attempt is not None
    assert attempt.stage is AdmissionStage.DELETION_VALIDATED


def test_an_attempt_id_bound_to_another_identity_is_refused() -> None:
    store = _attempt_store()
    _claim(store)
    with pytest.raises(AdmissionStageError):
        store.claim(
            attempt_id="adm-1",
            scope=_drive("drive-1"),
            identity_hash="b" * 64,
            source_revision="etag-1",
            contract_epoch=1,
            prior_artifact_id=None,
        )


def test_the_stages_advance_in_exactly_one_order() -> None:
    store = _attempt_store()
    _claim(store)
    assert store.advance("adm-1", AdmissionStage.DELETION_VALIDATED).stage is (
        AdmissionStage.DELETION_VALIDATED
    )
    assert (
        store.advance(
            "adm-1", AdmissionStage.AUTHORITY_TOMBSTONED, receipt_id="r-1", receipt_hash="d" * 64
        ).stage
        is AdmissionStage.AUTHORITY_TOMBSTONED
    )
    assert store.advance("adm-1", AdmissionStage.CURSOR_COMMITTABLE).stage is (
        AdmissionStage.CURSOR_COMMITTABLE
    )


def test_skipping_the_deletion_validation_stage_is_refused() -> None:
    store = _attempt_store()
    _claim(store)
    with pytest.raises(AdmissionStageError):
        store.advance(
            "adm-1",
            AdmissionStage.AUTHORITY_TOMBSTONED,
            receipt_id="r-1",
            receipt_hash="d" * 64,
        )


def test_a_cursor_can_never_become_committable_without_a_tombstone() -> None:
    store = _attempt_store()
    _claim(store)
    store.advance("adm-1", AdmissionStage.DELETION_VALIDATED)
    with pytest.raises(AdmissionStageError):
        store.advance("adm-1", AdmissionStage.CURSOR_COMMITTABLE)


def test_moving_backwards_through_the_stages_is_refused() -> None:
    store = _attempt_store()
    _claim(store)
    store.advance("adm-1", AdmissionStage.DELETION_VALIDATED)
    with pytest.raises(AdmissionStageError):
        store.advance("adm-1", AdmissionStage.CLAIMED)


def test_re_advancing_to_the_held_stage_is_an_idempotent_no_op() -> None:
    store = _attempt_store()
    _claim(store)
    first = store.advance("adm-1", AdmissionStage.DELETION_VALIDATED)
    assert store.advance("adm-1", AdmissionStage.DELETION_VALIDATED) == first


def test_the_tombstone_stage_requires_the_exact_receipt_id_and_hash() -> None:
    store = _attempt_store()
    _claim(store)
    store.advance("adm-1", AdmissionStage.DELETION_VALIDATED)
    with pytest.raises(AdmissionStageError):
        store.advance("adm-1", AdmissionStage.AUTHORITY_TOMBSTONED, receipt_id="r-1")
    with pytest.raises(AdmissionStageError):
        store.advance("adm-1", AdmissionStage.AUTHORITY_TOMBSTONED, receipt_hash="d" * 64)


def test_the_tombstone_receipt_is_retained_on_the_attempt() -> None:
    store = _attempt_store()
    _claim(store)
    store.advance("adm-1", AdmissionStage.DELETION_VALIDATED)
    store.advance(
        "adm-1", AdmissionStage.AUTHORITY_TOMBSTONED, receipt_id="r-1", receipt_hash="d" * 64
    )
    attempt = store.inspect("adm-1")
    assert attempt is not None
    assert (attempt.receipt_id, attempt.receipt_hash) == ("r-1", "d" * 64)


def test_retry_is_offered_until_the_cursor_is_committable_and_not_after() -> None:
    store = _attempt_store()
    _claim(store)
    assert store.retryable("adm-1") is True
    store.advance("adm-1", AdmissionStage.DELETION_VALIDATED)
    store.advance(
        "adm-1", AdmissionStage.AUTHORITY_TOMBSTONED, receipt_id="r-1", receipt_hash="d" * 64
    )
    assert store.retryable("adm-1") is True
    store.advance("adm-1", AdmissionStage.CURSOR_COMMITTABLE)
    assert store.retryable("adm-1") is False


def test_an_unknown_attempt_is_neither_inspectable_nor_retryable() -> None:
    store = _attempt_store()
    assert store.inspect("adm-missing") is None
    assert store.retryable("adm-missing") is False


def _current_attempt_store() -> tuple[
    SqlAlchemyAdmissionAttemptStore, SqlAlchemySourceContractHeadStore
]:
    engine = memory_engine()
    return SqlAlchemyAdmissionAttemptStore(engine), SqlAlchemySourceContractHeadStore(engine)


@pytest.mark.parametrize(
    ("parent", "requested", "other", "head_contract"),
    (
        (scope(), _drive("drive-1"), _drive("drive-10"), contract()),
        (
            salesforce_connector_scope(
                deployment_id=DEPLOYMENT,
                connector_instance_id=INSTANCE,
                organization_id=ORGANIZATION,
            ),
            salesforce_object_scope(
                deployment_id=DEPLOYMENT,
                connector_instance_id=INSTANCE,
                organization_id=ORGANIZATION,
                object_name="Account",
            ),
            salesforce_object_scope(
                deployment_id=DEPLOYMENT,
                connector_instance_id=INSTANCE,
                organization_id=ORGANIZATION,
                object_name="AccountHistory",
            ),
            contract(source_kind="salesforce"),
        ),
    ),
    ids=("m365-drive", "salesforce-object"),
)
def test_pending_attempts_use_parent_head_but_match_exact_requested_child_scope(
    parent: SourceReconciliationScope,
    requested: SourceReconciliationScope,
    other: SourceReconciliationScope,
    head_contract: SourceContractIdentity,
) -> None:
    attempts, heads = _current_attempt_store()
    head = heads.claim_or_load(parent, source_generation=1, contract=head_contract)
    for attempt_id, attempt_scope in (("adm-z", requested), ("adm-a", requested), ("adm-x", other)):
        attempts.claim(
            attempt_id=attempt_id,
            scope=attempt_scope,
            identity_hash="a" * 64,
            source_revision="etag-1",
            contract_epoch=head.contract_epoch,
            prior_artifact_id=None,
        )

    pending = attempts.pending_for_current_scopes([requested, requested])

    assert tuple(attempt.attempt_id for attempt in pending) == ("adm-a", "adm-z")


@pytest.mark.parametrize(
    ("parent", "requested", "source_kind"),
    (
        (scope(), _drive("drive-1"), "m365-sharepoint"),
        (
            salesforce_connector_scope(
                deployment_id=DEPLOYMENT,
                connector_instance_id=INSTANCE,
                organization_id=ORGANIZATION,
            ),
            salesforce_object_scope(
                deployment_id=DEPLOYMENT,
                connector_instance_id=INSTANCE,
                organization_id=ORGANIZATION,
                object_name="Account",
            ),
            "salesforce",
        ),
    ),
    ids=("m365-drive", "salesforce-object"),
)
def test_pending_attempts_exclude_superseded_parent_contract_epochs(
    parent: SourceReconciliationScope,
    requested: SourceReconciliationScope,
    source_kind: str,
) -> None:
    attempts, heads = _current_attempt_store()
    heads.claim_or_load(parent, source_generation=1, contract=contract(source_kind=source_kind))
    attempts.claim(
        attempt_id="adm-old",
        scope=requested,
        identity_hash="a" * 64,
        source_revision="etag-1",
        contract_epoch=1,
        prior_artifact_id=None,
    )
    heads.claim_or_load(
        parent,
        source_generation=1,
        contract=contract("b" * 64, source_kind=source_kind),
    )

    assert attempts.pending_for_current_scopes([requested]) == ()


def test_pending_attempts_exclude_cursor_committable() -> None:
    attempts, heads = _current_attempt_store()
    requested = _drive("drive-1")
    heads.claim_or_load(scope(), source_generation=1, contract=contract())
    _claim(attempts)
    attempts.advance("adm-1", AdmissionStage.DELETION_VALIDATED)
    attempts.advance(
        "adm-1", AdmissionStage.AUTHORITY_TOMBSTONED, receipt_id="r-1", receipt_hash="d" * 64
    )
    attempts.advance("adm-1", AdmissionStage.CURSOR_COMMITTABLE)

    assert attempts.pending_for_current_scopes([requested]) == ()


def test_pending_attempts_empty_input_performs_no_query() -> None:
    engine = memory_engine()
    attempts = SqlAlchemyAdmissionAttemptStore(engine)
    statements = 0

    def count_statement(*_args: object) -> None:
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        assert attempts.pending_for_current_scopes([]) == ()
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
    assert statements == 0


def test_the_operator_projection_exposes_only_ids_hashes_and_stages() -> None:
    """The inspect surface must have no field that could carry protected data."""
    from dataclasses import fields

    from ne.connect.authority_admission import AdmissionAttempt

    assert {field.name for field in fields(AdmissionAttempt)} == {
        "attempt_id",
        "scope_hash",
        "identity_hash",
        "stage",
        "source_revision",
        "contract_epoch",
        "prior_artifact_id",
        "receipt_id",
        "receipt_hash",
    }


# -- the persisted connector instance ----------------------------------------


def test_a_credential_rotation_loads_the_same_connector_instance() -> None:
    engine = memory_engine()
    first = claim_or_load_connector_instance(
        engine,
        deployment_id=DEPLOYMENT,
        source_name="m365-sharepoint",
        admitted_target_digest="a" * 64,
    )
    second = claim_or_load_connector_instance(
        engine,
        deployment_id=DEPLOYMENT,
        source_name="m365-sharepoint",
        admitted_target_digest="a" * 64,
    )
    assert first == second
    assert first.source_generation == 1


def test_a_changed_admitted_target_cannot_reuse_the_connector_instance() -> None:
    engine = memory_engine()
    claim_or_load_connector_instance(
        engine,
        deployment_id=DEPLOYMENT,
        source_name="m365-sharepoint",
        admitted_target_digest="a" * 64,
    )
    with pytest.raises(ConnectorInstanceRebindError):
        claim_or_load_connector_instance(
            engine,
            deployment_id=DEPLOYMENT,
            source_name="m365-sharepoint",
            admitted_target_digest="b" * 64,
        )


def test_two_deployments_mint_distinct_connector_instances() -> None:
    engine = memory_engine()
    first = claim_or_load_connector_instance(
        engine,
        deployment_id=DEPLOYMENT,
        source_name="m365-sharepoint",
        admitted_target_digest="a" * 64,
    )
    second = claim_or_load_connector_instance(
        engine,
        deployment_id="deployment-beta",
        source_name="m365-sharepoint",
        admitted_target_digest="a" * 64,
    )
    assert first.connector_instance_id != second.connector_instance_id


def test_a_malformed_admitted_target_digest_is_refused() -> None:
    with pytest.raises(ValueError):
        claim_or_load_connector_instance(
            memory_engine(),
            deployment_id=DEPLOYMENT,
            source_name="m365-sharepoint",
            admitted_target_digest="short",
        )


# -- the sub-SLA sweep arithmetic --------------------------------------------


def plan(
    *,
    cadence: str = "120",
    batch_size: int = 50,
    max_batch: str = "60",
    max_points: int = 500,
) -> SweepCadencePlan:
    return SweepCadencePlan(
        cadence_seconds=Decimal(cadence),
        batch_size=batch_size,
        max_batch_duration_seconds=Decimal(max_batch),
        max_points_per_scope=max_points,
    )


def test_the_batch_count_rounds_a_partial_batch_up() -> None:
    assert plan(batch_size=50, max_points=500).batches == 10
    assert plan(batch_size=50, max_points=501).batches == 11
    assert plan(batch_size=50, max_points=1).batches == 1


def test_the_worst_case_is_batches_times_cadence_plus_batch_duration() -> None:
    assert plan().worst_case_seconds() == Decimal(1800)
    assert plan(max_points=501).worst_case_seconds() == Decimal(1980)


def test_a_rotation_that_exactly_equals_the_sla_is_refused() -> None:
    """The rule is STRICTLY less than the SLA, not at most it.

    A rotation that finishes at the instant the SLA elapses has re-read the tail
    point exactly when its receipt stopped being fresh.
    """
    exact = plan(cadence="300", max_batch="60", batch_size=50, max_points=500)
    assert exact.worst_case_seconds() == Decimal(3600)
    assert exact.satisfies(Decimal(3600)) is False
    with pytest.raises(SweepCadenceError):
        exact.require_sub_sla(Decimal(3600))


def test_a_sub_sla_rotation_is_accepted() -> None:
    plan().require_sub_sla(Decimal(3600))
    assert plan().satisfies(Decimal(3600)) is True


def test_the_generic_hourly_cadence_cannot_satisfy_a_pt1h_sla() -> None:
    hourly = plan(cadence="3600", max_batch="60", batch_size=50, max_points=500)
    assert hourly.satisfies(Decimal(3600)) is False


def test_a_non_positive_or_non_decimal_sweep_member_is_unrepresentable() -> None:
    with pytest.raises(ValueError):
        plan(batch_size=0)
    with pytest.raises(ValueError):
        plan(max_points=0)
    with pytest.raises(ValueError):
        plan(cadence="0")
    with pytest.raises(ValueError):
        SweepCadencePlan(
            cadence_seconds=Decimal("NaN"),
            batch_size=1,
            max_batch_duration_seconds=Decimal(1),
            max_points_per_scope=1,
        )


def test_the_sla_argument_itself_must_be_a_positive_decimal() -> None:
    with pytest.raises(ValueError):
        plan().require_sub_sla(Decimal(0))


# -- the connector source-id split and observed revisions --------------------


def test_a_drive_item_id_containing_a_colon_stays_in_the_item_id() -> None:
    """Graph driveItem ids may contain ``:``; splitting on the LAST one moves
    characters out of the item id and silently addresses a different point."""
    from ne.connect.authority_admission import _split_m365_source_id

    assert _split_m365_source_id("drive-1:item:with:colons") == (
        "drive-1",
        "item:with:colons",
    )
    with pytest.raises(AuthorityAdmissionError):
        _split_m365_source_id("no-separator")
    with pytest.raises(AuthorityAdmissionError):
        _split_m365_source_id(":item-1")
    with pytest.raises(AuthorityAdmissionError):
        _split_m365_source_id("drive-1:")


def test_two_observed_states_of_one_item_never_share_a_source_revision() -> None:
    """A revision that could not distinguish two states would let a changed item
    reuse the previous receipt id and leave the head pinned to stale content."""
    from ne.connect.authority_admission import _source_revision
    from ne.contracts.integration import Record

    def _record(**payload: object) -> Record:
        return Record(record_id="item-1", source_id="drive-1:item-1", payload=payload)

    assert _source_revision(_record(etag="etag-v1")) == "etag-v1"
    assert _source_revision(_record(etag="etag-v1")) != _source_revision(_record(etag="etag-v2"))
    first = _source_revision(_record(name="a"))
    second = _source_revision(_record(name="b"))
    assert first != second
    assert _source_revision(_record(name="a")) == first
    assert first.startswith("snap-")


# -- permission-read evidence ------------------------------------------------


def test_a_partial_permission_collection_mints_no_evidence() -> None:
    """A truncated collection is indistinguishable from a contraction of access.

    Admitting it would record a narrower ACL than the source actually holds, so
    the incomplete read is refused rather than downgraded.
    """
    with pytest.raises(PartialPermissionReadError):
        observed_permissions(
            drive_id="drive-1",
            item_id="item-a",
            principals=("u-ada",),
            pages_read=1,
            collection_complete=False,
        )


def test_a_read_that_fetched_no_page_mints_no_evidence() -> None:
    """Zero pages is the shape a timer, an empty delta or a cursor reset has."""
    with pytest.raises(PartialPermissionReadError):
        observed_permissions(
            drive_id="drive-1",
            item_id="item-a",
            principals=(),
            pages_read=0,
            collection_complete=True,
        )


def test_evidence_is_bound_to_the_exact_point_it_was_read_for() -> None:
    evidence = observed_permissions(
        drive_id="drive-1",
        item_id="item-a",
        principals=("u-ada",),
        pages_read=2,
        collection_complete=True,
    )
    assert evidence.matches("drive-1:item-a") is True
    assert evidence.matches("drive-1:item-b") is False
    assert evidence.matches("drive-2:item-a") is False
    assert evidence.pages_read == 2


# -- the bounded rotating sweep cursor ---------------------------------------


def _points(count: int) -> list[tuple[str, str]]:
    return [(f"{index:064d}", f"item-{index}") for index in range(count)]


def test_a_bounded_batch_takes_the_points_after_the_persisted_cursor() -> None:
    cursor = SqlAlchemySweepCursor(memory_engine())
    scope = _drive("drive-1")
    candidates = _points(5)
    first = cursor.plan(scope, candidates=candidates, batch_size=2)
    assert [item for _, item in first.points] == ["item-0", "item-1"]
    for identity_hash, _ in first.points:
        cursor.complete_point(scope, identity_hash=identity_hash)
    second = cursor.plan(scope, candidates=candidates, batch_size=2)
    assert [item for _, item in second.points] == ["item-2", "item-3"]


def test_the_rotation_reaches_the_tail_before_it_restarts() -> None:
    """A bounded batch must not starve the tail, and must not stall at the end.

    Three batches of two over five points visit every point exactly once. The
    fourth batch is the one that would previously have been empty forever: the
    cursor sits past the last point, so the rotation restarts and the next epoch
    begins at the head of the same deterministic order.
    """
    cursor = SqlAlchemySweepCursor(memory_engine())
    scope = _drive("drive-1")
    candidates = _points(5)
    visited: list[str] = []
    epochs: list[int] = []
    for _ in range(4):
        batch = cursor.plan(scope, candidates=candidates, batch_size=2)
        epochs.append(batch.sweep_epoch)
        visited.extend(item for _, item in batch.points)
        for identity_hash, _ in batch.points:
            cursor.complete_point(scope, identity_hash=identity_hash)
    assert visited == [
        "item-0",
        "item-1",
        "item-2",
        "item-3",
        "item-4",
        "item-0",
        "item-1",
    ]
    assert epochs == [1, 1, 1, 2]


def test_a_scope_with_no_candidates_does_not_burn_a_rotation() -> None:
    """An empty candidate set is not the end of a rotation, it is no work."""
    cursor = SqlAlchemySweepCursor(memory_engine())
    scope = _drive("drive-1")
    first = cursor.plan(scope, candidates=[], batch_size=2)
    second = cursor.plan(scope, candidates=[], batch_size=2)
    assert first.points == () and second.points == ()
    assert first.sweep_epoch == second.sweep_epoch == 1


def test_a_restart_resumes_the_exact_page_and_the_principals_already_read() -> None:
    """Restarting a part-read collection must not restart the collection.

    Re-reading from page one would re-fetch pages already paid for and, worse,
    would make a bounded batch unable to finish a long collection at all.
    """
    engine = memory_engine()
    scope = _drive("drive-1")
    candidates = _points(3)
    cursor = SqlAlchemySweepCursor(engine)
    identity_hash = candidates[0][0]
    cursor.record_page(
        scope,
        identity_hash=identity_hash,
        next_page_url="https://graph.microsoft.com/v1.0/next?$skiptoken=PAGE2",
        principals=["u-ada"],
    )
    resumed = SqlAlchemySweepCursor(engine).plan(scope, candidates=candidates, batch_size=1)
    assert resumed.resume_point == identity_hash
    assert resumed.resume_page_url == ("https://graph.microsoft.com/v1.0/next?$skiptoken=PAGE2")
    assert resumed.resume_principals == ("u-ada",)
    assert [item for _, item in resumed.points] == ["item-0"]


def test_completing_a_point_clears_its_in_flight_page() -> None:
    engine = memory_engine()
    scope = _drive("drive-1")
    candidates = _points(2)
    cursor = SqlAlchemySweepCursor(engine)
    identity_hash = candidates[0][0]
    cursor.record_page(
        scope, identity_hash=identity_hash, next_page_url="https://x/next", principals=["u"]
    )
    cursor.complete_point(scope, identity_hash=identity_hash)
    plan = cursor.plan(scope, candidates=candidates, batch_size=1)
    assert plan.resume_point is None
    assert plan.resume_page_url is None
    assert plan.resume_principals == ()


def test_a_batch_size_below_one_is_unrepresentable() -> None:
    with pytest.raises(ValueError):
        SqlAlchemySweepCursor(memory_engine()).plan(
            _drive("drive-1"), candidates=_points(1), batch_size=0
        )


# -- the freshness gate ------------------------------------------------------


def test_a_never_swept_point_is_stale_and_refuses_its_parent() -> None:
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    hierarchy.register_points(_drive("drive-1"), [("a" * 64, "item-a")])
    assert hierarchy.stale_point_hashes(_drive("drive-1"), sla_seconds=Decimal(3600)) == ("a" * 64,)
    with pytest.raises(UnsweptPointError):
        hierarchy.require_points_swept_within_sla(_drive("drive-1"), sla_seconds=Decimal(3600))


def test_a_point_re_read_inside_the_sla_lets_its_parent_succeed() -> None:
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    hierarchy.register_points(_drive("drive-1"), [("a" * 64, "item-a")])
    hierarchy.mark_point_swept(
        _drive("drive-1"), "a" * 64, epoch=1, graph_item_id="item-a", evidence=_read("item-a")
    )
    assert hierarchy.stale_point_hashes(_drive("drive-1"), sla_seconds=Decimal(3600)) == ()
    hierarchy.require_points_swept_within_sla(_drive("drive-1"), sla_seconds=Decimal(3600))


def test_a_point_re_read_longer_ago_than_the_sla_is_stale() -> None:
    """The gate is decided on the recorded INSTANT, not on 'a sweep ran'.

    The row is backdated rather than the SLA shrunk to nothing, so this exercises
    the real cutoff comparison instead of a degenerate zero-second window.
    """
    from ne.connect.authority_admission import ne_source_scope_points

    engine = memory_engine()
    hierarchy = SqlAlchemyHierarchicalAdmission(engine)
    hierarchy.register_points(_drive("drive-1"), [("a" * 64, "item-a")])
    hierarchy.mark_point_swept(
        _drive("drive-1"), "a" * 64, epoch=1, graph_item_id="item-a", evidence=_read("item-a")
    )
    hierarchy.require_points_swept_within_sla(_drive("drive-1"), sla_seconds=Decimal(3600))
    with engine.begin() as conn:
        conn.execute(
            sa.update(ne_source_scope_points).values(last_swept_at=datetime(2020, 1, 1, tzinfo=UTC))
        )
    assert hierarchy.stale_point_hashes(_drive("drive-1"), sla_seconds=Decimal(3600)) == ("a" * 64,)
    with pytest.raises(UnsweptPointError):
        hierarchy.require_points_swept_within_sla(_drive("drive-1"), sla_seconds=Decimal(3600))


def test_a_retired_point_is_never_stale() -> None:
    hierarchy = SqlAlchemyHierarchicalAdmission(memory_engine())
    hierarchy.register_points(_drive("drive-1"), [("a" * 64, "item-a")])
    hierarchy.retire_point(_drive("drive-1"), "a" * 64)
    hierarchy.require_points_swept_within_sla(_drive("drive-1"), sla_seconds=Decimal(3600))


# -- the contract epoch that governs a read-time authority decision ----------
#
# The producer-side head above is only half of a contract-epoch advance. CORE's
# ``CurrentSourceAuthorityReader`` cannot import this module — ne-connect depends
# on ne-brain, not the reverse — so without a durable binding it answers from the
# source-authority head alone, and an old-epoch stamp keeps authorizing until a
# re-admission happens to move that head. These proofs drive the real head store,
# the real CORE Writer and the real CORE Reader over one database.


def _authority_engine() -> sa.Engine:
    from ne.brain.current_source_authority import ensure_source_authority_schema

    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    ensure_source_authority_schema(engine)
    ensure_admission_schema(engine)
    return engine


def _m365_identity(item_id: str, *, tenant: str = TENANT, instance: str = INSTANCE) -> Any:
    from ne.contracts.source_authority import M365SourcePoint, SourceIdentity

    return SourceIdentity(
        deployment_id=DEPLOYMENT,
        source_kind="m365-sharepoint",
        connector_instance_id=instance,
        point=M365SourcePoint(
            graph_tenant_id=tenant, site_id="site-1", drive_id="drive-1", item_id=item_id
        ),
        generation=1,
    )


def _admit_under_head(engine: sa.Engine, head: Any, identity: Any, *, content: bytes) -> Any:
    """Admit one point exactly as ``M365AuthorityAdmission.finalize`` does.

    The governing contract epoch is published BEFORE the Writer runs, which is
    the ordering the producer uses and the reason a published receipt always has
    a binding.
    """
    from ne.brain.current_source_authority import (
        SqlAlchemyCurrentSourceAuthorityWriter,
        claim_or_load_deployment_construction_id,
        publish_governing_contract_epoch,
    )
    from ne.contracts.source_authority import (
        SourceAdmissionFence,
        SourceAuthorityCandidate,
        SourceAuthorityState,
        SourceContentArtifactIdentity,
    )

    writer = SqlAlchemyCurrentSourceAuthorityWriter(
        engine,
        claim_or_load_deployment_construction_id(engine),
        freshness_sla_seconds=Decimal(3600),
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=identity,
        source_snapshot_hash=hashlib.sha256(content).hexdigest(),
        content_snapshot_hash=hashlib.sha256(content).hexdigest(),
        acl_snapshot_hash="a" * 64,
        source_contract=head.source_contract,
        artifact_schema_version=1,
    )
    revision = f"rev-{head.contract_epoch}-{identity.point.item_id}"
    child = drive_scope(
        deployment_id=DEPLOYMENT,
        connector_instance_id=identity.connector_instance_id,
        graph_tenant_id=identity.point.graph_tenant_id,
        drive_id="drive-1",
    )

    async def scenario() -> Any:
        claim = await writer.claim(
            child,
            expected_head_revision=expected_scope_head_revision(engine, child),
            lease_seconds=60,
        )
        publish_governing_contract_epoch(
            engine,
            identity_hash=identity.identity_hash,
            contract_scope_hash=head.connector_scope.scope_hash,
            contract_epoch=head.contract_epoch,
        )
        receipt = await writer.finalize(
            claim,
            SourceAdmissionFence(
                deployment_id=identity.deployment_id,
                connector_instance_id=identity.connector_instance_id,
                source_generation=identity.generation,
                external_source_revision=revision,
                contract_epoch=head.contract_epoch,
                reconciliation_scope=claim.scope,
                reconciliation_epoch=claim.reconciliation_epoch,
                fencing_token=claim.fencing_token,
            ),
            SourceAuthorityCandidate(
                source_identity=identity,
                artifact_identity=artifact,
                source_revision=revision,
                source_contract=head.source_contract,
                contract_epoch=head.contract_epoch,
                acl_snapshot_hash="a" * 64,
                state=SourceAuthorityState.PRESENT,
                authority_as_of=datetime.now(UTC) - timedelta(minutes=5),
            ),
        )
        await writer.release(claim)
        return receipt

    return asyncio.run(scenario())


def _stamp(receipt: Any) -> Any:
    from ne.contracts.source_authority import SourceAuthorityStamp

    assert receipt.artifact_identity is not None
    return SourceAuthorityStamp(
        source_identity=receipt.source_identity,
        artifact_identity=receipt.artifact_identity,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=receipt.source_contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )


def _decide(engine: sa.Engine, stamp: Any) -> Any:
    from ne.brain.current_source_authority import (
        SqlAlchemyCurrentSourceAuthorityReader,
        claim_or_load_deployment_construction_id,
    )

    reader = SqlAlchemyCurrentSourceAuthorityReader(
        engine, claim_or_load_deployment_construction_id(engine)
    )
    return asyncio.run(reader.resolve(stamp, principal_id="p-1", access_context_hash="a" * 64))


def test_an_unresolved_contract_epoch_advance_makes_the_prior_stamp_retryable_unknown() -> None:
    """Admit under epoch 1, advance to an unresolved epoch 2, re-admit under it.

    Step three is the whole point: between the advance and the re-admission the
    epoch-1 stamp is neither the current admitted truth nor a withdrawal, so the
    only truthful answer is ``retryable_unknown``. The two flanking assertions are
    the positive controls — a Reader that answered Unknown unconditionally would
    fail both of them.
    """
    from ne.contracts.source_authority import SourceAuthorityDecision

    engine = _authority_engine()
    store = SqlAlchemySourceContractHeadStore(engine)
    connector = scope()
    identity = _m365_identity("item-1")

    first = store.claim_or_load(connector, source_generation=1, contract=contract())
    epoch_one = _admit_under_head(engine, first, identity, content=b"epoch one bytes")
    assert store.mark_resolved(first)
    authorized = _decide(engine, _stamp(epoch_one))
    assert authorized.decision is SourceAuthorityDecision.AUTHORIZED
    assert authorized.current_receipt == epoch_one

    second = store.claim_or_load(
        connector, source_generation=1, contract=contract(descriptor_hash="b" * 64)
    )
    assert (second.contract_epoch, store.is_resolved(connector)) == (2, False)

    unresolved = _decide(engine, _stamp(epoch_one))
    assert unresolved.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN
    assert unresolved.decision is not SourceAuthorityDecision.AUTHORIZED
    assert unresolved.decision is not SourceAuthorityDecision.PERMANENT_WITHHELD
    # An unknown resolution discloses no current receipt at all, so the stale
    # epoch-1 receipt does not leak out through the answer either.
    assert unresolved.current_receipt is None
    assert unresolved.safe_reason is None

    epoch_two = _admit_under_head(engine, second, identity, content=b"epoch two bytes")
    assert store.mark_resolved(second)
    assert epoch_two.contract_epoch == 2
    current = _decide(engine, _stamp(epoch_two))
    assert current.decision is SourceAuthorityDecision.AUTHORIZED
    assert current.current_receipt == epoch_two
    # And the epoch-1 stamp does not come back with the completion.
    assert _decide(engine, _stamp(epoch_one)).decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN


def test_a_late_publish_from_the_superseded_epoch_cannot_restore_authorization() -> None:
    """A producer still finishing epoch-1 work must not walk the binding back.

    The identity here is deliberately NOT re-admitted under epoch 2, so its
    source-authority head still names an epoch-1 receipt and the receipt/stamp
    epoch comparison agrees with the stamp. The governing binding is the only
    thing standing between that stamp and an authorization.
    """
    from ne.brain.current_source_authority import publish_governing_contract_epoch
    from ne.contracts.source_authority import SourceAuthorityDecision

    engine = _authority_engine()
    store = SqlAlchemySourceContractHeadStore(engine)
    connector = scope()
    identity = _m365_identity("item-1")
    first = store.claim_or_load(connector, source_generation=1, contract=contract())
    receipt = _admit_under_head(engine, first, identity, content=b"epoch one bytes")
    store.mark_resolved(first)
    store.claim_or_load(connector, source_generation=1, contract=contract(descriptor_hash="b" * 64))

    stored = publish_governing_contract_epoch(
        engine,
        identity_hash=identity.identity_hash,
        contract_scope_hash=connector.scope_hash,
        contract_epoch=1,
    )

    assert stored == 2
    assert receipt.contract_epoch == _stamp(receipt).contract_epoch == 1
    assert _decide(engine, _stamp(receipt)).decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN


def test_a_contract_epoch_advance_is_confined_to_the_scope_that_advanced() -> None:
    """Two connector scopes sharing one contract must not strand each other.

    ``SourceContractIdentity`` is a hash of the source kind, descriptor version
    and descriptor hash — nothing tenant-specific — so two connector instances on
    the same connector version carry byte-identical contract identities. Keying
    the binding on the contract rather than the connector scope would leave the
    untouched tenant permanently Unknown with no admission able to clear it.
    """
    from ne.contracts.source_authority import SourceAuthorityDecision

    engine = _authority_engine()
    store = SqlAlchemySourceContractHeadStore(engine)
    left = scope()
    right = connector_instance_scope(
        deployment_id=DEPLOYMENT, connector_instance_id="conn-instance-2", graph_tenant_id="other"
    )
    left_head = store.claim_or_load(left, source_generation=1, contract=contract())
    right_head = store.claim_or_load(right, source_generation=1, contract=contract())
    assert left_head.source_contract == right_head.source_contract

    left_identity = _m365_identity("item-1")
    right_identity = _m365_identity("item-1", tenant="other", instance="conn-instance-2")
    left_receipt = _admit_under_head(engine, left_head, left_identity, content=b"left bytes")
    right_receipt = _admit_under_head(engine, right_head, right_identity, content=b"right bytes")

    store.claim_or_load(right, source_generation=1, contract=contract(descriptor_hash="b" * 64))

    assert _decide(engine, _stamp(right_receipt)).decision is (
        SourceAuthorityDecision.RETRYABLE_UNKNOWN
    )
    assert _decide(engine, _stamp(left_receipt)).decision is SourceAuthorityDecision.AUTHORIZED


def test_a_foreign_receipt_substitution_is_withheld_under_the_governing_epoch() -> None:
    """The epoch gate does not stand in for the identity gate, or vice versa.

    Both stamps here carry the CURRENT governing epoch, so the new binding lets
    them through to the receipt comparison — where naming another point's receipt
    is a refusal, not a retry.
    """
    from ne.contracts.source_authority import SourceAuthorityDecision

    engine = _authority_engine()
    store = SqlAlchemySourceContractHeadStore(engine)
    connector = scope()
    head = store.claim_or_load(connector, source_generation=1, contract=contract())
    mine = _admit_under_head(engine, head, _m365_identity("item-1"), content=b"mine")
    theirs = _admit_under_head(engine, head, _m365_identity("item-2"), content=b"theirs")

    substituted = _stamp(mine).model_copy(
        update={
            "authority_receipt_id": theirs.receipt_id,
            "authority_receipt_hash": theirs.receipt_hash,
        }
    )
    resolution = _decide(engine, substituted)
    assert resolution.decision is SourceAuthorityDecision.PERMANENT_WITHHELD
    assert resolution.current_receipt == mine
    # Positive control: the unsubstituted stamp of the same point authorizes.
    assert _decide(engine, _stamp(mine)).decision is SourceAuthorityDecision.AUTHORIZED


def test_the_governing_binding_row_carries_only_digests_an_epoch_and_an_instant() -> None:
    """No locator, label or source byte can reach a read-time decision this way.

    Asserted over the stored VALUES, not the column names: a check on names alone
    passes a row whose ``contract_scope_hash`` was filled with a drive id.
    """
    from ne.brain.current_source_authority import ne_source_authority_contract_epochs

    engine = _authority_engine()
    store = SqlAlchemySourceContractHeadStore(engine)
    head = store.claim_or_load(scope(), source_generation=1, contract=contract())
    identity = _m365_identity("item-1")
    _admit_under_head(engine, head, identity, content=b"content bytes")

    with engine.connect() as conn:
        row = conn.execute(sa.select(ne_source_authority_contract_epochs)).one()
    assert set(row._mapping) == {
        "identity_hash",
        "contract_scope_hash",
        "contract_epoch",
        "updated_at",
    }
    assert row.identity_hash == identity.identity_hash
    assert row.contract_scope_hash == scope().scope_hash
    assert row.contract_epoch == 1
    # Every stored string is a sha256 digest, so none of them IS a locator, a
    # label or a content byte — the values are checked, not the column headings.
    for value in (row.identity_hash, row.contract_scope_hash):
        assert re.fullmatch(r"[0-9a-f]{64}", str(value))
    leakable = {
        identity.point.item_id,
        identity.point.drive_id,
        identity.point.site_id,
        identity.point.graph_tenant_id,
        identity.connector_instance_id,
        identity.deployment_id,
        "content bytes",
    }
    assert {str(value) for value in row}.isdisjoint(leakable)


def test_an_identity_with_no_published_binding_resolves_as_it_did_before() -> None:
    """Named to the evidence: this is the CORE-only path, not a general claim.

    A receipt admitted straight through the Writer — every ne-brain suite, and any
    source with no connector contract head — has no binding to consult, and the
    Reader must keep answering from the head and the retained receipt alone. It
    is not a proof that an unbound identity is safe from a contract-epoch advance:
    nothing has advanced an epoch for it.
    """
    from ne.brain.current_source_authority import ne_source_authority_contract_epochs
    from ne.contracts.source_authority import SourceAuthorityDecision

    engine = _authority_engine()
    store = SqlAlchemySourceContractHeadStore(engine)
    head = store.claim_or_load(scope(), source_generation=1, contract=contract())
    identity = _m365_identity("item-1")
    receipt = _admit_under_head(engine, head, identity, content=b"bound")
    with engine.begin() as conn:
        conn.execute(sa.delete(ne_source_authority_contract_epochs))
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_source_authority_contract_epochs)
            ).scalar_one()
            == 0
        )

    assert _decide(engine, _stamp(receipt)).decision is SourceAuthorityDecision.AUTHORIZED
