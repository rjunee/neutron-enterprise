# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The Skill Gardener — ONE run_once payload with every section populated."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa

from ne.contracts.skills import SkillInvocation, SkillSource
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import FixProposal, ProposalKind, SqlAlchemyProposalStore
from ne.skills.config import SkillsSettings
from ne.skills.gardener import SkillGardener
from ne.skills.governed_gardener import gardener_tool_handler
from ne.skills.reconcile import reconcile_bundle_dir
from ne.skills.registry import SqlAlchemySkillRegistry

from .conftest import GOOD_BODY, make_pack


def _fire(reg: SqlAlchemySkillRegistry, name: str, *, ok: bool | None, at: datetime) -> None:
    reg.append_invocation(
        SkillInvocation(
            skill_name=name,
            version=1,
            principal_id="u-1",
            session_id="s-1",
            fired_at=at,
            run_succeeded=ok,
        )
    )


def _write_bundle_pack(root: Path, name: str, *, description: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    md = (
        f"---\nname: {name}\nversion: 1\ndescription: {description}\n"
        f"source: bundled\ntriggers:\n  - use {name}\n---\n{GOOD_BODY}"
    )
    (d / "SKILL.md").write_text(md, encoding="utf-8")


def _seed_stale_proposal(engine: sa.Engine, *, real_now: datetime) -> str:
    store = SqlAlchemyProposalStore(engine)
    proposal = FixProposal(
        proposal_id="stale-1",
        kind=ProposalKind.SKILL_CANDIDATE,
        lane=FixLane.SKILLS,
        cluster_id="c-1",
        evidence_event_ids=(),
        evidence_signal_ids=(),
        summary="[skill-candidate] stale-cand from a workflow",
        confidence=0.5,
        priority=ProposalPriority.LOW,
        config_hash="test-hash",
        created_at=real_now - timedelta(days=30),  # older than the 14d stale window
        payload={"pack": {"name": "stale-cand"}},
    )
    with engine.begin() as conn:
        store.append(conn, proposal)
    return proposal.proposal_id


def test_gardener_one_run_populates_every_section(forge_engine: sa.Engine, tmp_path: Path) -> None:
    engine = forge_engine
    reg = SqlAlchemySkillRegistry(engine)
    real_now = datetime.now(UTC)
    clock_now = real_now + timedelta(days=200)  # everything registered now looks old

    # (retirement) an old, unfired pack.
    reg.register_pack(make_pack("stale-unfired", description="a lonely skill"), origin="bundle")

    # (failure flag) 2 success + 2 fail of 4 known fires ⇒ ratio 0.5 flag; 4 fires
    # keeps it OUT of the unfired-retirement set (>= min_fires).
    reg.register_pack(make_pack("flaky", description="a flaky skill"), origin="bundle")
    _fire(reg, "flaky", ok=True, at=real_now)
    _fire(reg, "flaky", ok=True, at=real_now)
    _fire(reg, "flaky", ok=False, at=real_now)
    _fire(reg, "flaky", ok=False, at=real_now)

    # (overlap + near-dup + superseded) two packs sharing a trigger phrase; the
    # forged one is lower-ranked ⇒ superseded. Both fired enough to NOT be
    # unfired-retirement candidates, so over-b keeps its 'superseded' reason.
    reg.register_pack(
        make_pack("over-a", description="alpha overlap", triggers=("shared trigger phrase",)),
        origin="bundle",
    )
    reg.register_pack(
        make_pack(
            "over-b",
            description="beta overlap",
            triggers=("shared trigger phrase",),
            source=SkillSource.FORGED,
        ),
        origin="apply",
    )
    for _ in range(3):
        _fire(reg, "over-a", ok=True, at=clock_now - timedelta(days=1))
        _fire(reg, "over-b", ok=True, at=clock_now - timedelta(days=1))

    # (unknown requires_tools) a pack declaring a tool absent from the catalogue.
    reg.register_pack(
        make_pack("needs-ghost", description="wants a ghost", requires_tools=("read.nonexistent",)),
        origin="bundle",
    )
    _fire(reg, "needs-ghost", ok=True, at=clock_now - timedelta(days=1))

    # (bundle hash drift) register from a bundle dir, then mutate the file.
    root = tmp_path / "bundle"
    _write_bundle_pack(root, "drifter", description="original")
    reconcile_bundle_dir(engine, SkillsSettings(bundle_dir=str(root)))
    _fire(reg, "drifter", ok=True, at=clock_now - timedelta(days=1))
    _write_bundle_pack(root, "drifter", description="mutated-out-of-band")

    # (stale proposal) an open skill_candidate older than the stale window.
    _seed_stale_proposal(engine, real_now=real_now)

    gardener = SkillGardener(
        engine,
        settings=SkillsSettings(bundle_dir=str(root)),
        audit_engine=None,
        known_tools=frozenset({"read.skills.view", "read.skills.search"}),
        clock=lambda: clock_now,
    )
    outcome = gardener.run_once()

    # --- the persisted report round-trips + every section is populated ---
    report = reg.latest_gardener_report()
    assert report is not None

    retire_names = {r["name"]: r["reason"] for r in report["retirement_candidates"]}
    assert "stale-unfired" in retire_names
    assert retire_names["stale-unfired"] == "unfired-90d"
    assert retire_names.get("over-b") == "superseded"

    failure_names = {f["name"] for f in report["failure_flags"]}
    assert "flaky" in failure_names
    flaky = next(f for f in report["failure_flags"] if f["name"] == "flaky")
    assert flaky["known_fires"] == 4 and flaky["failed"] == 2 and flaky["ratio"] == 0.5

    overlap_phrases = {o["phrase"] for o in report["trigger_overlaps"]}
    assert "shared trigger phrase" in overlap_phrases

    near_dup_pairs = {frozenset((d["a"], d["b"])) for d in report["near_dups"]}
    assert frozenset(("over-a", "over-b")) in near_dup_pairs

    unknown = {u["name"]: u["unknown"] for u in report["unknown_requires_tools"]}
    assert unknown.get("needs-ghost") == ["read.nonexistent"]

    drift_names = {d["name"] for d in report["bundle_hash_drift"]}
    assert "drifter" in drift_names

    stale_ids = {s["proposal_id"] for s in report["stale_proposals"]}
    assert "stale-1" in stale_ids
    stale = next(s for s in report["stale_proposals"] if s["proposal_id"] == "stale-1")
    assert stale["name"] == "stale-cand" and stale["age_days"] >= 14

    # --- the counts-only outcome mirrors the report and carries NO names ---
    assert outcome.retirement_candidates == len(report["retirement_candidates"])
    assert outcome.failure_flags == len(report["failure_flags"])
    assert outcome.near_dups == len(report["near_dups"])
    assert outcome.stale_proposals == len(report["stale_proposals"])
    dumped = outcome.model_dump()
    assert all(isinstance(v, int) for v in dumped.values())


def test_gardener_tool_handler_returns_counts_only_json(forge_engine: sa.Engine) -> None:
    """The governed tool handler ignores model args + returns counts-only JSON."""
    import asyncio

    engine = forge_engine
    SqlAlchemySkillRegistry(engine).register_pack(make_pack("solo"), origin="bundle")
    gardener = SkillGardener(engine, settings=SkillsSettings(), audit_engine=None)
    handler = gardener_tool_handler(gardener)

    # Hostile injected args are ignored (no injectable surface).
    async def _call() -> str:
        return await handler({"evil": "drop table", "force_retire": "solo"})

    result = asyncio.run(_call())
    payload = json.loads(result)
    assert set(payload) == {
        "rows_folded",
        "retirement_candidates",
        "failure_flags",
        "trigger_overlaps",
        "near_dups",
        "unknown_requires_tools",
        "bundle_hash_drift",
        "stale_proposals",
    }
    assert all(isinstance(v, int) for v in payload.values())
    # solo is untouched by the read-only sweep — still active.
    assert SqlAlchemySkillRegistry(engine).current_status("solo") == "active"
