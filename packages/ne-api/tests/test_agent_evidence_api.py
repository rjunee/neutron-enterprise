# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""HTTP contract for the shared evidence read/export serializer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ne.api.deps import current_authenticated_context, current_principal
from ne.api.evidence_projection import EvidenceResourceNotFound
from ne.api.evidence_wire import (
    EvidenceEnvelope,
    EvidenceItemView,
    EvidenceLocatorView,
    GovernanceView,
    RunOutputView,
)
from ne.api.routers.evidence import attach_evidence_service, router
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.types import Principal

H = "a" * 64
PRINCIPAL = Principal(id="owner-1")
CONTEXT = AuthenticatedRequestContext(
    principal_id="owner-1",
    authorization_groups=(),
    department="finance",
    issuer="https://issuer.example.test",
    expires_at=2_000_000_000,
)


def _envelope() -> EvidenceEnvelope:
    return EvidenceEnvelope(
        agent_id="agent-1",
        run_id="run-1",
        run_status="completed",
        mapping_state="unavailable",
        mapping_message="Claim mapping unavailable",
        output=RunOutputView(
            verdict="unavailable",
            state="unavailable",
            message="Answer unavailable",
            display=None,
        ),
        claims=(),
        evidence=(),
        governance=GovernanceView(
            status="unavailable",
            acting_for_principal_id="owner-1",
            agent_id="agent-1",
            agent_version=1,
            config_hash=H,
            capability_snapshot_hash="b" * 64,
            delegated_user_label=None,
            agent_service_identity=None,
            connector_credential_owner_label=None,
            effective_scopes=(),
            policy_environment=None,
            tool_tier=None,
            tool_decision=None,
            receipts=(),
            unavailable_reasons=(
                "delegation_join_unavailable",
                "service_identity_join_unavailable",
                "credential_owner_join_unavailable",
                "scope_join_unavailable",
                "policy_join_unavailable",
                "tool_decision_join_unavailable",
                "audit_receipt_join_unavailable",
            ),
        ),
    )


class _Service:
    def __init__(self, result: EvidenceEnvelope | None) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def project(self, **kwargs: object) -> EvidenceEnvelope:
        self.calls.append(kwargs)
        if self.result is None:
            raise EvidenceResourceNotFound
        return self.result


def _client(service: _Service) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_principal] = lambda: PRINCIPAL
    app.dependency_overrides[current_authenticated_context] = lambda: CONTEXT
    attach_evidence_service(app.state, service)
    return TestClient(app)


def test_read_and_export_share_exact_serializer_and_no_store_headers() -> None:
    envelope = _envelope().model_copy(
        update={
            "evidence": (
                EvidenceItemView(
                    evidence_id="evidence-opaque",
                    state="available",
                    message="Evidence available",
                    assurance="direct",
                    producing_event_id="event-1",
                    produced_at=datetime(2026, 8, 23, tzinfo=UTC),
                    locator_hash="c" * 64,
                    locator=EvidenceLocatorView(
                        kind="verified_t3",
                        row_anchor="d" * 64,
                        receipt_hash="e" * 64,
                    ),
                ),
            )
        }
    )
    service = _Service(envelope)
    client = _client(service)

    read = client.get("/v1/agents/agent-1/runs/run-1/evidence")
    export = client.get("/v1/agents/agent-1/runs/run-1/evidence/export")

    assert read.status_code == export.status_code == 200
    assert read.content == export.content
    assert read.json()["agent_id"] == "agent-1"
    assert read.json()["run_id"] == "run-1"
    assert read.json()["evidence"][0]["evidence_id"] == "evidence-opaque"
    assert read.json()["evidence"][0]["locator"] == {
        "kind": "verified_t3",
        "row_anchor": "d" * 64,
        "receipt_hash": "e" * 64,
        "source_system": None,
        "title": None,
        "source_revision": None,
        "as_of": None,
        "content_hash": None,
        "chunk_id": None,
        "chunk_ids": [],
        "served_document_versions": [],
        "page": None,
        "row_id": None,
        "entity_id": None,
        "graph_path": [],
    }
    for response in (read, export):
        assert response.headers["cache-control"] == "private, no-store, max-age=0"
        assert response.headers["pragma"] == "no-cache"
    assert export.headers["content-disposition"] == 'attachment; filename="run-evidence.json"'
    assert "content-disposition" not in read.headers
    assert service.calls[0]["principal"] == PRINCIPAL
    assert service.calls[0]["access_context_hash"] == CONTEXT.context_hash


@pytest.mark.parametrize(
    "fields",
    (
        {"kind": "verified_t3", "receipt_hash": "e" * 64},
        {
            "kind": "verified_t3_empty",
            "row_anchor": "d" * 64,
            "receipt_hash": "e" * 64,
        },
        {"row_anchor": "d" * 64, "receipt_hash": "e" * 64},
        {
            "kind": "verified_t3",
            "row_anchor": "d" * 64,
            "receipt_hash": "e" * 64,
            "title": "fabricated source title",
        },
    ),
    ids=("row-missing-anchor", "empty-invented-row", "missing-kind", "mixed-source-fields"),
)
def test_verified_locator_wire_refuses_ambiguous_or_fabricated_shapes(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        EvidenceLocatorView.model_validate(fields)


def test_known_hidden_and_random_ids_are_byte_equivalent_404s() -> None:
    client = _client(_Service(None))

    known = client.get("/v1/agents/agent-hidden/runs/run-known/evidence")
    random = client.get("/v1/agents/agent-random/runs/run-random/evidence")

    assert known.status_code == random.status_code == 404
    assert known.content == random.content
    assert known.headers["cache-control"] == random.headers["cache-control"]
    assert known.headers["pragma"] == random.headers["pragma"] == "no-cache"


def test_unmounted_core_fails_closed_with_non_cacheable_503() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_principal] = lambda: PRINCIPAL
    app.dependency_overrides[current_authenticated_context] = lambda: CONTEXT

    response = TestClient(app).get("/v1/agents/agent-1/runs/run-1/evidence")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
