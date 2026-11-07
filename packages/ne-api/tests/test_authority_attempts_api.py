# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Ops-only metadata API for current source-authority attempts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import sqlalchemy as sa

from ne.api.connect_sync import sync_request_tool_name
from ne.connect.authority_admission import (
    AdmissionStage,
    SqlAlchemyAdmissionAttemptStore,
    SqlAlchemySourceContractHeadStore,
    connector_instance_scope,
    drive_scope,
    ensure_admission_schema,
)
from ne.connect.authority_attempts import AuthorityAttemptRead, AuthorityAttemptReader
from ne.contracts.source_authority import SourceContractIdentity

from .conftest import AppBuilder, Harness, get_state, run

_DEPLOYMENT = "deployment-alpha"
_INSTANCE = "connector-instance-1"
_TENANT = "tenant-guid"


def _contract(descriptor_hash: str = "a" * 64) -> SourceContractIdentity:
    return SourceContractIdentity(
        source_kind="m365-sharepoint",
        descriptor_schema_version=1,
        descriptor_hash=descriptor_hash,
    )


def _seed_attempts(harness: Harness) -> AuthorityAttemptReader:
    engine = sa.create_engine(harness.runtime_url)
    ensure_admission_schema(engine)
    attempts = SqlAlchemyAdmissionAttemptStore(engine)
    heads = SqlAlchemySourceContractHeadStore(engine)
    parent = connector_instance_scope(
        deployment_id=_DEPLOYMENT,
        connector_instance_id=_INSTANCE,
        graph_tenant_id=_TENANT,
    )
    child = drive_scope(
        deployment_id=_DEPLOYMENT,
        connector_instance_id=_INSTANCE,
        graph_tenant_id=_TENANT,
        drive_id="drive-current",
    )
    foreign = drive_scope(
        deployment_id=_DEPLOYMENT,
        connector_instance_id=_INSTANCE,
        graph_tenant_id=_TENANT,
        drive_id="drive-foreign",
    )
    first = heads.claim_or_load(parent, source_generation=1, contract=_contract())
    attempts.claim(
        attempt_id="attempt-stale",
        scope=child,
        identity_hash="1" * 64,
        source_revision="revision-stale",
        contract_epoch=first.contract_epoch,
        prior_artifact_id=None,
    )
    current = heads.claim_or_load(parent, source_generation=1, contract=_contract("b" * 64))
    attempts.claim(
        attempt_id="attempt-current",
        scope=child,
        identity_hash="2" * 64,
        source_revision="revision-current",
        contract_epoch=current.contract_epoch,
        prior_artifact_id="3" * 64,
    )
    attempts.claim(
        attempt_id="attempt-complete",
        scope=child,
        identity_hash="4" * 64,
        source_revision="revision-complete",
        contract_epoch=current.contract_epoch,
        prior_artifact_id=None,
    )
    attempts.advance("attempt-complete", AdmissionStage.DELETION_VALIDATED)
    attempts.advance(
        "attempt-complete",
        AdmissionStage.AUTHORITY_TOMBSTONED,
        receipt_id="receipt-complete",
        receipt_hash="5" * 64,
    )
    attempts.advance("attempt-complete", AdmissionStage.CURSOR_COMMITTABLE)
    attempts.claim(
        attempt_id="attempt-foreign",
        scope=foreign,
        identity_hash="6" * 64,
        source_revision="revision-foreign",
        contract_epoch=current.contract_epoch,
        prior_artifact_id=None,
    )
    return AuthorityAttemptReader(attempts, (child, child))


def test_list_is_ops_only_private_stable_current_and_closed(
    make_app: AppBuilder,
) -> None:
    harness = make_app(ops_roles=("ops",))
    state = get_state(harness.app)
    state.authority_attempts = _seed_attempts(harness)

    async def _test() -> None:
        async with harness.client() as client:
            refused = await client.get(
                "/api/v1/connect/authority/attempts",
                headers=harness.bearer("user:reader", groups=[]),
            )
            assert refused.status_code == 403

            response = await client.get(
                "/api/v1/connect/authority/attempts",
                headers=harness.bearer("user:operator", groups=["ops"]),
            )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        attempts = response.json()["attempts"]
        assert [item["attempt_id"] for item in attempts] == ["attempt-current"]
        assert set(attempts[0]) == {
            "attempt_id",
            "source_kind",
            "source_binding",
            "scope_hash",
            "identity_hash",
            "stage",
            "source_revision",
            "contract_epoch",
            "prior_artifact_id",
            "receipt_id",
            "receipt_hash",
        }
        assert attempts[0]["source_kind"] == "m365-sharepoint"
        assert attempts[0]["source_binding"] == "m365"

    run(_test())


def test_unknown_foreign_stale_completed_and_random_ids_are_indistinguishable(
    make_app: AppBuilder,
) -> None:
    harness = make_app(ops_roles=("ops",))
    state = get_state(harness.app)
    state.authority_attempts = _seed_attempts(harness)

    async def _test() -> None:
        outcomes: list[tuple[int, bytes, str]] = []
        async with harness.client() as client:
            headers = harness.bearer("user:operator", groups=["ops"])
            for attempt_id in (
                "attempt-stale",
                "attempt-complete",
                "attempt-foreign",
                "random-not-present",
            ):
                response = await client.get(
                    f"/api/v1/connect/authority/attempts/{attempt_id}", headers=headers
                )
                outcomes.append(
                    (response.status_code, response.content, response.headers["cache-control"])
                )
        assert (
            outcomes
            == [(404, b'{"detail":"authority attempt not found"}', "private, no-store")] * 4
        )

    run(_test())


class _OrderedReader:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def read(self, attempt_id: str) -> AuthorityAttemptRead:
        self._events.append(f"read:{attempt_id}")
        return AuthorityAttemptRead(
            attempt_id=attempt_id,
            source_kind="m365-sharepoint",
            source_binding="m365",
            scope_hash="a" * 64,
            identity_hash="b" * 64,
            stage=AdmissionStage.DELETION_VALIDATED,
            source_revision="etag-1",
            contract_epoch=7,
            prior_artifact_id=None,
            receipt_id=None,
            receipt_hash=None,
        )


class _OrderedExecutor:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.tool_specs = (SimpleNamespace(name=sync_request_tool_name("m365")),)

    async def execute(self, action: str, args: dict[str, Any], *, principal: Any) -> str:
        self._events.append(f"queue:{action}:{principal.id}")
        assert args == {}
        return '{"created":true,"request_id":"opaque-server-id"}'


def test_retry_is_zero_body_ops_gated_server_routed_and_queues_before_final_read(
    make_app: AppBuilder,
) -> None:
    harness = make_app(ops_roles=("ops",))
    state = get_state(harness.app)
    events: list[str] = []
    state.authority_attempts = _OrderedReader(events)  # type: ignore[assignment]
    state.executor = _OrderedExecutor(events)  # type: ignore[assignment]

    async def _test() -> None:
        async with harness.client() as client:
            refused = await client.post(
                "/api/v1/connect/authority/attempts/attempt-7/retry",
                headers=harness.bearer("user:reader", groups=[]),
            )
            assert refused.status_code == 403
            assert events == []

            smuggled = await client.post(
                "/api/v1/connect/authority/attempts/attempt-7/retry",
                headers=harness.bearer("user:operator", groups=["ops"]),
                json={"source": "salesforce", "receipt_id": "caller-receipt"},
            )
            assert smuggled.status_code == 422
            assert events == []

            accepted = await client.post(
                "/api/v1/connect/authority/attempts/attempt-7/retry",
                headers=harness.bearer("user:operator", groups=["ops"]),
            )
        assert accepted.status_code == 202
        assert accepted.headers["cache-control"] == "private, no-store"
        assert accepted.json()["source_binding"] == "m365"
        assert "opaque-server-id" not in accepted.text
        assert events == [
            "read:attempt-7",
            "queue:write.internal.connect.request_sync_m365:user:operator",
            "read:attempt-7",
        ]

    run(_test())
