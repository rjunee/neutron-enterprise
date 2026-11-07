# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

from ne.api.steward_disclosure import (
    ProposalDisclosure,
    ProposalDisposition,
    disclose_proposal,
    proposal_disposition,
)
from ne.contracts.types import Principal
from ne.flywheel.proposals import ProposalKind, StoredFixProposal

from .conftest import AppBuilder, run, seed_proposal


def _proposal(kind: ProposalKind, payload: dict) -> StoredFixProposal:
    return StoredFixProposal(
        id=1,
        proposal_id="proposal-1",
        kind=kind.value,
        lane="ontology",
        cluster_id="cluster-1",
        status="proposed",
        config_hash="f" * 64,
        summary="private",
        confidence=1,
        priority="medium",
        evidence_event_ids=(),
        evidence_signal_ids=(),
        payload=payload,
        created_at=datetime(2026, 8, 22, tzinfo=UTC),
    )


def test_registry_is_exhaustive_and_unknown_never_becomes_source_free() -> None:
    assert {proposal_disposition(kind.value) for kind in ProposalKind} == {
        ProposalDisposition.UNKNOWN,
        ProposalDisposition.UNSUPPORTED_SOURCE_BEARING,
        ProposalDisposition.SOURCE_FREE_DEFINITION,
        ProposalDisposition.SUPPORTED_SOURCE_BEARING,
    }
    assert proposal_disposition("future-kind") is ProposalDisposition.UNKNOWN


def test_unsupported_and_malformed_proposals_fail_closed_without_evaluation() -> None:
    principal = Principal(id="steward", groups=("steward",))
    unsupported = asyncio.run(
        disclose_proposal(
            _proposal(ProposalKind.ENTITY_MERGE_CANDIDATE, {"display": "secret"}),
            evaluator=None,
            principal=principal,
            access_context_hash=None,
        )
    )
    malformed = asyncio.run(
        disclose_proposal(
            _proposal(ProposalKind.GRAPH_EXTRACTION_CANDIDATE, {"assertions": []}),
            evaluator=None,
            principal=principal,
            access_context_hash=None,
        )
    )
    assert unsupported is ProposalDisclosure.CONTENT_WITHHELD
    assert malformed is ProposalDisclosure.RETRYABLE_UNKNOWN


def test_verified_definition_requires_the_exact_producer_envelope_and_hash() -> None:
    principal = Principal(id="steward", groups=("steward",))
    body = "template_id = 'safe'\nversion = 1\n"
    payload = {
        "template_id": "safe",
        "version": 1,
        "toml_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "template_toml": body,
    }
    available = asyncio.run(
        disclose_proposal(
            _proposal(ProposalKind.VERIFIED_ANSWER_CANDIDATE, payload),
            evaluator=None,
            principal=principal,
            access_context_hash=None,
        )
    )
    forged = asyncio.run(
        disclose_proposal(
            _proposal(
                ProposalKind.VERIFIED_ANSWER_CANDIDATE,
                {**payload, "source_evidence": "private"},
            ),
            evaluator=None,
            principal=principal,
            access_context_hash=None,
        )
    )
    assert available is ProposalDisclosure.AVAILABLE
    assert forged is ProposalDisclosure.RETRYABLE_UNKNOWN


def test_queue_and_action_use_the_same_server_authored_withheld_decision(
    make_app: AppBuilder,
) -> None:
    harness = make_app(steward_roles=("steward",))
    proposal_id = seed_proposal(harness.app, payload={"private": "never-serve"})

    async def _drive() -> None:
        headers = harness.bearer("user:steward", groups=["steward"])
        async with harness.client() as client:
            queue = await client.get("/api/v1/steward/proposals", headers=headers)
            assert queue.status_code == 200
            assert queue.headers["cache-control"] == "private, no-store, max-age=0"
            assert queue.json() == {
                "proposals": [
                    {
                        "disclosure_status": "retryable_unknown",
                        "proposal_id": proposal_id,
                        "status": "proposed",
                        "created_at": queue.json()["proposals"][0]["created_at"],
                    }
                ]
            }
            assert "private" not in queue.text
            action = await client.post(
                f"/api/v1/steward/proposals/{proposal_id}/approve",
                json={"note": ""},
                headers=headers,
            )
            assert action.status_code == 409

    run(_drive())
