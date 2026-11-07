# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Structural safety-invariant suite for the propose-then-approve gate (NE-066).

Proves the crux invariant: the unit's ONLY writes are the review log, the
rejection correction-signal (+ its outbox mirror), and the audit chain — the
content/schema/policy/proposal stores are NEVER mutated, the renderer writes
nothing, and 'applied' is reachable only via an explicit human-driven
``record_apply`` call (no internal call site, no agent tool).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa

import ne.flywheel.review as review_module
from ne.flywheel.diffs import render_proposal_diff
from ne.flywheel.proposals import StoredFixProposal, ne_fix_proposals
from ne.flywheel.review import ProposalReviewService, RejectionCode
from ne.flywheel.signals import SignalProvenance

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "ne" / "flywheel"

#: The COMPLETE set of tables this unit is permitted to write.
_ALLOWED_WRITE_TABLES = {
    "ne_proposal_reviews",
    "ne_correction_signals",
    "ne_outbox_events",
    "ne_audit_log",
    "ne_audit_head",
}


def _table_counts(engine: sa.Engine) -> dict[str, int]:
    inspector = sa.inspect(engine)
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for name in inspector.get_table_names():
            counts[name] = int(
                conn.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar_one()
            )
    return counts


def _proposal_rows(engine: sa.Engine) -> list[tuple[object, ...]]:
    with engine.connect() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                sa.select(ne_fix_proposals).order_by(ne_fix_proposals.c.id)
            ).all()
        ]


def _pick(proposals: list[StoredFixProposal], lane: str) -> StoredFixProposal:
    return next(p for p in proposals if p.lane == lane and p.kind == "fix")


def test_review_flow_writes_only_review_signal_outbox_audit_tables(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    svc = ProposalReviewService(review_engine)
    prov = make_provenance()
    approved = _pick(seeded_proposals, "content")
    rejected = _pick(seeded_proposals, "policy")

    before_counts = _table_counts(review_engine)
    before_proposals = _proposal_rows(review_engine)

    # A full approve + reject + apply flow.
    svc.approve(approved.proposal_id, prov)
    svc.record_apply(approved.proposal_id, prov, applied_ref="https://git/pr/1")
    svc.reject(rejected.proposal_id, prov, code=RejectionCode.INCORRECT)

    after_counts = _table_counts(review_engine)
    after_proposals = _proposal_rows(review_engine)

    changed = {t for t in after_counts if after_counts[t] != before_counts.get(t)}
    assert changed <= _ALLOWED_WRITE_TABLES, f"unexpected mutated tables: {changed}"
    # The review + signal + outbox + audit tables demonstrably changed.
    assert {
        "ne_proposal_reviews",
        "ne_correction_signals",
        "ne_outbox_events",
        "ne_audit_log",
    } <= changed
    # NE-065's proposal store is byte-identical — never UPDATEd (status stays proposed).
    assert "ne_fix_proposals" not in changed
    assert before_proposals == after_proposals


def test_diff_rendering_writes_nothing(
    review_engine: sa.Engine,
    seeded_proposals: list[StoredFixProposal],
    frozen_config: str,
) -> None:
    before = _table_counts(review_engine)
    for proposal in seeded_proposals:
        render_proposal_diff(proposal)
    after = _table_counts(review_engine)
    assert before == after


def test_review_module_is_append_only_and_offline() -> None:
    source = inspect.getsource(review_module)
    assert ".update(" not in source
    assert ".delete(" not in source
    assert "httpx" not in source


def test_no_auto_apply_call_sites_in_src() -> None:
    # 'record_apply(' appears ONLY in review.py (its own def) — no internal call
    # site, so 'applied' is reachable only via an external human-driven caller.
    files_with_record_apply = []
    for path in _SRC_ROOT.glob("*.py"):
        if "record_apply(" in path.read_text():
            files_with_record_apply.append(path.name)
    assert files_with_record_apply == ["review.py"]

    # governed.py constructs EXACTLY one ToolSpec — no review/apply tool was
    # snuck into the agent surface.
    governed_src = (_SRC_ROOT / "governed.py").read_text()
    assert governed_src.count("ToolSpec(") == 1
