# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Gate-checked, non-destructive entity merge/unmerge (NE-066 apply step).

``apply_merge`` refuses unless the NE-066 gate reports ``APPROVED`` (BEFORE any
read/write), then writes a NON-destructive ``SAME_AS`` edge under the INTERSECTION
of the two nodes' ACLs (a principal who cannot see BOTH sides cannot learn they
are the same entity — the resolution is itself information), stamps
``source_system='ne:er'`` (so a connector re-sync's ``delete_edges_from_source``
never removes it), sets ``canonical_key`` on the non-canonical node, records the
apply on the gate, and emits a ``graph.entity_merge`` audit event. ``unmerge`` is
the reverse (``delete_edge`` is itself guarded to 'ne:er' edges).

NE-BRAIN-T2-WIRING (audit F5): ``apply_merge`` guards on the proposal KIND before
any payload parse — an APPROVED but non-``entity_merge_candidate`` proposal (or a
malformed ER payload missing left/right ``entity_key``) raises a clean
:class:`MergeGateError` instead of a bare ``KeyError`` (fail-closed, graceful).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa

from ne.brain_graph.errors import GraphAclError
from ne.brain_graph.model import edge_key_for
from ne.contracts.governance import AuditSink
from ne.contracts.types import Principal
from ne.events import new_audit_event
from ne.flywheel.proposals import ProposalKind, ne_fix_proposals
from ne.flywheel.review import ProposalReviewService, ProposalStatus
from ne.flywheel.signals import SignalProvenance, SignalSurface
from ne.schema import AuditOutcome

__all__ = ["ER_SOURCE_SYSTEM", "SAME_AS_EDGE_KIND", "MergeGateError", "apply_merge", "unmerge"]

SAME_AS_EDGE_KIND = "SAME_AS"
ER_SOURCE_SYSTEM = "ne:er"


class MergeGateError(Exception):
    """The merge was refused because the proposal is not APPROVED (fail-closed)."""


class ErMergeStore(Protocol):
    """The store surface the merge needs (fetch + non-destructive writes + unmerge)."""

    async def fetch_nodes(self, keys: Sequence[str]) -> Sequence[Mapping[str, Any]]: ...
    async def upsert_nodes(self, nodes: Sequence[Mapping[str, Any]]) -> None: ...
    async def upsert_edges(self, edges: Sequence[Mapping[str, Any]]) -> None: ...
    async def delete_edge(self, key: str) -> None: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _load_proposal(engine: sa.Engine, proposal_id: str) -> tuple[str, Mapping[str, Any]]:
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_fix_proposals.c.kind, ne_fix_proposals.c.payload).where(
                ne_fix_proposals.c.proposal_id == proposal_id
            )
        ).first()
    if row is None:
        raise MergeGateError(f"proposal {proposal_id!r} is not in ne_fix_proposals")
    return str(row.kind), dict(row.payload)


async def apply_merge(
    engine: sa.Engine,
    review_service: ProposalReviewService,
    store: ErMergeStore,
    proposal_id: str,
    *,
    actor: str,
    audit_sink: AuditSink,
    config_hash: str,
) -> str:
    """Apply an APPROVED entity-merge proposal → a ``SAME_AS`` edge. Returns edge key.

    Fail-closed (F5): refuses unless the gate reports APPROVED AND the proposal kind
    is ``entity_merge_candidate`` with a well-formed left/right payload — a non-ER
    kind or malformed payload raises :class:`MergeGateError` before any store write.
    """
    # (1) Gate FIRST — refuse unless APPROVED, before any read/write.
    status = review_service.status_of(proposal_id)
    if status is not ProposalStatus.APPROVED:
        raise MergeGateError(
            f"cannot apply merge for {proposal_id!r}: status is {status.value!r}, not "
            "'approved' (the NE-066 gate must approve first; fail-closed)"
        )

    # (2) Load the proposal kind + payload. F5 (fail-closed BEFORE any parse/write):
    # an APPROVED but NON-ER proposal kind must raise a clean MergeGateError, not a
    # bare KeyError on a payload that never had left/right entity keys.
    kind, payload = _load_proposal(engine, proposal_id)
    if kind != ProposalKind.ENTITY_MERGE_CANDIDATE.value:
        raise MergeGateError(
            f"cannot apply merge for {proposal_id!r}: proposal kind {kind!r} is not "
            "'entity_merge_candidate' (fail-closed; nothing written)"
        )
    # Validate the ER payload shape BEFORE any parse: left/right must each be a
    # Mapping carrying 'entity_key', else a graceful MergeGateError (not a KeyError).
    left = payload.get("left")
    right = payload.get("right")
    if (
        not isinstance(left, Mapping)
        or not isinstance(right, Mapping)
        or "entity_key" not in left
        or "entity_key" not in right
    ):
        raise MergeGateError(
            f"cannot apply merge for {proposal_id!r}: malformed entity-merge payload "
            "(missing left/right entity_key); nothing written"
        )
    # (3) fetch both endpoints.
    left_key = str(left["entity_key"])
    right_key = str(right["entity_key"])
    fetched = {str(n["key"]): dict(n) for n in await store.fetch_nodes([left_key, right_key])}
    if left_key not in fetched or right_key not in fetched:
        raise MergeGateError(
            f"merge {proposal_id!r}: one or both endpoints are absent from the graph"
        )

    # (4) Intersection ACL — empty means nobody could see the edge; refuse.
    intersection = sorted(set(fetched[left_key]["acl"]) & set(fetched[right_key]["acl"]))
    if not intersection:
        raise GraphAclError(
            f"merge {proposal_id!r}: empty ACL intersection — a SAME_AS edge nobody "
            "could see is unrepresentable; nothing written (fail-closed)"
        )

    # (5) Non-destructive write: SAME_AS edge (canonical->other) + canonical_key.
    canonical, other = sorted([left_key, right_key])
    source_ref = f"er:{proposal_id}"
    edge_key = edge_key_for(SAME_AS_EDGE_KIND, canonical, other, source_ref)
    synced_at = _now()
    await store.upsert_edges(
        [
            {
                "key": edge_key,
                "edge_kind": SAME_AS_EDGE_KIND,
                "from_key": canonical,
                "to_key": other,
                "source_system": ER_SOURCE_SYSTEM,
                "source_ref": source_ref,
                "acl": intersection,
                "config_hash": config_hash,
                "synced_at": synced_at,
            }
        ]
    )
    non_canonical = dict(fetched[other])
    non_canonical["canonical_key"] = canonical
    non_canonical["synced_at"] = synced_at
    await store.upsert_nodes([non_canonical])

    # (6) Record the apply on the gate; (7) emit the audit event.
    provenance = SignalProvenance(
        principal=Principal(id=actor), surface=SignalSurface.API, session_id=source_ref
    )
    review_service.record_apply(proposal_id, provenance, applied_ref=edge_key)
    await _emit_merge_audit(
        audit_sink,
        action_detail="merge",
        proposal_id=proposal_id,
        edge_key=edge_key,
        canonical=canonical,
        other=other,
        actor=actor,
        config_hash=config_hash,
    )
    return edge_key


async def unmerge(
    store: ErMergeStore,
    edge_key: str,
    non_canonical_key: str,
    *,
    actor: str,
    audit_sink: AuditSink,
    config_hash: str,
) -> None:
    """Reverse a merge: delete the ('ne:er'-guarded) SAME_AS edge + clear canonical_key."""
    await store.delete_edge(edge_key)  # guarded to source_system 'ne:er'
    fetched = list(await store.fetch_nodes([non_canonical_key]))
    if fetched:
        props = dict(fetched[0])
        props.pop("canonical_key", None)
        props["synced_at"] = _now()
        await store.upsert_nodes([props])
    await _emit_merge_audit(
        audit_sink,
        action_detail="unmerge",
        proposal_id="",
        edge_key=edge_key,
        canonical="",
        other=non_canonical_key,
        actor=actor,
        config_hash=config_hash,
    )


async def _emit_merge_audit(
    audit_sink: AuditSink,
    *,
    action_detail: str,
    proposal_id: str,
    edge_key: str,
    canonical: str,
    other: str,
    actor: str,
    config_hash: str,
) -> None:
    await audit_sink.append(
        new_audit_event(
            action="graph.entity_merge",
            resource=f"graph://edge/{edge_key}",
            outcome=AuditOutcome.success,
            actor=actor,
            detail=json.dumps(
                {
                    "op": action_detail,
                    "proposal_id": proposal_id,
                    "edge_key": edge_key,
                    "canonical_key": canonical,
                    "other_key": other,
                }
            ),
            config_hash=config_hash or None,
            provenance={"source": "ne-brain-graph", "component": "er.merge"},
        )
    )
