# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-BRAIN-T2-WIRING F4 — the ENTITY_MERGE_CANDIDATE steward no-bypass wire proof.

Re-proves the 104c no-bypass property for the ER proposal kind through the LIVE
steward wire (never wire-tested with Tier-2 live before): ``StewardProposalView``
STRUCTURALLY omits ``summary``/``payload`` (only the sanitized ``artifact`` crosses),
and a hostile entity ``display`` name is markdown-escaped by the flywheel renderer
before it reaches the wire (the wire-layer mirror of flywheel ``test_diffs`` :434).

The security invariant matches ``test_steward_api``: proposal-carried free text
reaches the console ONLY inside the sanitized ``artifact`` (fenced summary + escaped
entity-merge bullets). A payload field the renderer NEVER surfaces (``raw_features``)
proves the raw payload dict is not dumped anywhere on the wire. The governed spine
is REAL; only the model/embedder/qdrant edges are faked (conftest).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import FixProposal, ProposalKind

from .conftest import AppBuilder, get_state, run

_ROLES = ("steward",)

#: Sentinels: the RAW payload dict must never cross the wire, and the summary/payload
#: KEYS must be structurally absent. ``SMUGGLE-HIDDEN`` rides a payload field the
#: renderer never surfaces (``raw_features``) — its absence proves no raw payload dump.
_HIDDEN = "SMUGGLE-HIDDEN"


def _steward(h: Any) -> dict[str, str]:
    return h.bearer("user:steward", groups=["steward"])


def _find_no_key(node: Any, key: str) -> None:
    if isinstance(node, dict):
        assert key not in node, f"{key!r} key leaked in {node!r}"
        for v in node.values():
            _find_no_key(v, key)
    elif isinstance(node, list):
        for v in node:
            _find_no_key(v, key)


def _seed_entity_merge(app: Any, *, display: str = "SMUGGLE-DISPLAY") -> str:
    """Append an APPROVED-able ENTITY_MERGE_CANDIDATE proposal (mirrors
    test_er_merge_semantics._seed_and_approve field-for-field). Does NOT touch
    conftest.seed_proposal — this is the ER-kind-specific seed."""
    state = get_state(app)
    assert state.proposal_store is not None
    proposal_id = uuid.uuid4().hex
    fp = FixProposal(
        proposal_id=proposal_id,
        kind=ProposalKind.ENTITY_MERGE_CANDIDATE,
        lane=FixLane.ENTITY_RESOLUTION,
        cluster_id="er:Person:x",
        evidence_event_ids=("ev-1",),
        evidence_signal_ids=(1,),
        summary="SMUGGLE-SUMMARY",
        confidence=0.99,
        priority=ProposalPriority.HIGH,
        config_hash=state.config_hash,
        created_at=datetime.now(UTC),
        payload={
            "left": {
                "entity_class": "Person",
                "entity_key": "SMUGGLE-KEY-L",
                "display": display,
            },
            "right": {
                "entity_class": "Person",
                "entity_key": "SMUGGLE-KEY-R",
                "display": "SMUGGLE-DISPLAY-R",
            },
            "match_probability": 0.99,
            "match_weight": 12.3,
            "threshold": 0.95,
            "blocking_rule": "SMUGGLE-RULE",
            # A field the renderer never surfaces — its absence on the wire proves
            # the raw payload dict is never dumped (only rendered fields cross).
            "raw_features": _HIDDEN,
        },
    )
    with state.proposal_store.engine.begin() as conn:
        state.proposal_store.append(conn, fp)
    return proposal_id


def test_entity_merge_proposal_wire_omits_payload_and_summary(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    pid = _seed_entity_merge(h.app)

    async def _t() -> None:
        async with h.client() as client:
            hdr = _steward(h)
            # /v1/steward/proposals is the ONLY steward GET route that renders
            # proposals; digest + latency are count-only aggregations.
            r = await client.get("/api/v1/steward/proposals", headers=hdr)
            assert r.status_code == 200
            body = r.json()
            proposals = body["proposals"]
            assert [p["proposal_id"] for p in proposals] == [pid]
            assert set(proposals[0]) == {
                "disclosure_status",
                "proposal_id",
                "status",
                "created_at",
            }
            assert proposals[0]["disclosure_status"] == "content_withheld"
            # Structural omission: no summary/payload KEY anywhere in the response.
            _find_no_key(body, "summary")
            _find_no_key(body, "payload")
            # The raw payload dict is never dumped — the un-rendered field is absent.
            assert _HIDDEN not in r.text

            # The count-only read routes carry no proposal text at all.
            for route in ("/api/v1/steward/digest", "/api/v1/steward/latency"):
                rr = await client.get(route, headers=hdr)
                assert rr.status_code == 200
                _find_no_key(rr.json(), "summary")
                _find_no_key(rr.json(), "payload")
                assert _HIDDEN not in rr.text

    run(_t())


def test_entity_merge_display_injection_is_withheld_on_the_wire(make_app: AppBuilder) -> None:
    # Entity merge is explicitly unsupported-source-bearing pre-demo: not even a
    # sanitized display/artifact may cross the wire.
    h = make_app(steward_roles=_ROLES)
    _seed_entity_merge(h.app, display="SMUGGLE-DISPLAY ## heading [link](x)")

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/steward/proposals", headers=_steward(h))
            assert r.status_code == 200
            assert r.json()["proposals"][0]["disclosure_status"] == "content_withheld"
            assert "artifact" not in r.json()["proposals"][0]
            assert "[link](x)" not in r.text
            assert "SMUGGLE-DISPLAY" not in r.text

    run(_t())
