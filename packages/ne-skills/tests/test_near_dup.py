# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SKILL-3 near-dup detector: Jaccard/trigger boundaries + the embedder path."""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
from collections.abc import Mapping, Sequence

import pytest

from ne.contracts.skills import SkillSource
from ne.skills.near_dup import (
    NearDupConfig,
    candidate_text,
    find_near_dup,
    find_near_dup_embedded,
)

from .conftest import make_pack


def _boundary_packs(shared_n: int, a_n: int, b_n: int):
    """Two packs A, B whose candidate-text token SETS are exactly controllable.

    A.tokens = shared + a_only; B.tokens = shared + b_only (all disjoint groups).
    Name + trigger tokens are drawn from each pack's OWN token set so they add
    nothing new, and the two trigger PHRASES are disjoint (no trigger-phrase hit).
    Jaccard(A,B) = shared_n / (shared_n + a_n + b_n).
    """
    shared = [f"s{i}" for i in range(shared_n)]
    a_only = [f"a{i}" for i in range(a_n)]
    b_only = [f"b{i}" for i in range(b_n)]
    a = make_pack(
        a_only[0],  # name token ∈ a_only
        description=" ".join(shared + a_only),
        triggers=(a_only[0],),  # phrase "a0" — disjoint from B's "b0"
    )
    b = make_pack(
        b_only[0],
        description=" ".join(shared + b_only),
        triggers=(b_only[0],),
    )
    return a, b


def test_jaccard_boundary_below_threshold_no_flag() -> None:
    # shared 24, a 12, b 13 ⇒ union 49, intersection 24 ⇒ 24/49 ≈ 0.4898 < 0.5.
    cand, other = _boundary_packs(24, 12, 13)
    assert find_near_dup(cand, [other], config=NearDupConfig()) is None


def test_jaccard_boundary_at_threshold_flags() -> None:
    # shared 2, a 1, b 1 ⇒ union 4, intersection 2 ⇒ 2/4 = 0.5 (>= threshold).
    cand, other = _boundary_packs(2, 1, 1)
    hit = find_near_dup(cand, [other], config=NearDupConfig())
    assert hit is not None
    assert hit.method == "jaccard"
    assert hit.verdict == "duplicate"
    assert hit.score == pytest.approx(0.5)
    assert hit.name == other.name


def test_shared_trigger_phrase_flags_below_jaccard() -> None:
    # Disjoint description vocab (low Jaccard) but ONE exact shared trigger phrase.
    cand = make_pack(
        "alpha-skill",
        description="alpha beta gamma delta epsilon",
        triggers=("run the nightly batch",),
    )
    other = make_pack(
        "zeta-skill",
        description="zeta eta theta iota kappa lambda",
        triggers=("run the nightly batch",),
    )
    hit = find_near_dup(cand, [other], config=NearDupConfig())
    assert hit is not None
    assert hit.method == "trigger_phrase"
    assert hit.verdict == "duplicate"
    assert hit.score == pytest.approx(1.0)
    assert hit.name == "zeta-skill"


def test_same_name_pack_never_flags() -> None:
    # A library pack with the candidate's OWN name is skipped (no self-dup) even
    # though it is byte-identical (Jaccard 1.0).
    cand = make_pack("deploy-runbook", triggers=("deploy the service",))
    twin = make_pack("deploy-runbook", triggers=("deploy the service",))
    assert find_near_dup(cand, [twin], config=NearDupConfig()) is None


def test_detector_scans_all_sources() -> None:
    # Whole-registry: a MANUAL/ENGAGEMENT/BUNDLED pack is equally detectable.
    cand = make_pack("shipit", triggers=("cut a release",))
    library = [
        make_pack("bundled-x", source=SkillSource.BUNDLED, triggers=("unrelated one",)),
        make_pack("engagement-y", source=SkillSource.ENGAGEMENT, triggers=("unrelated two",)),
        make_pack("manual-z", source=SkillSource.MANUAL, triggers=("cut a release",)),
    ]
    hit = find_near_dup(cand, library, config=NearDupConfig())
    assert hit is not None
    assert hit.name == "manual-z"
    assert hit.method == "trigger_phrase"


# -- the embedder opt-in path ---------------------------------------------------


class _FakeEmbedder:
    """A LOCAL deterministic Embedder returning canned vectors per candidate text.

    Structurally implements ne.contracts.knowledge.Embedder (no I/O, no egress),
    so the REAL pure-python cosine branch is exercised, never mocked away.
    """

    model_id = "fake-embedder"
    dimension = 2

    def __init__(self, table: Mapping[str, Sequence[float]]) -> None:
        self._table = table
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.calls += 1
        return [self._table[t] for t in texts]


def test_embedder_cosine_thresholds() -> None:
    cand = make_pack("cand", triggers=("do the thing",))
    dup = make_pack("dup-pack", triggers=("almost the thing",))
    over = make_pack("over-pack", triggers=("kinda the thing",))
    clean = make_pack("clean-pack", triggers=("nothing alike",))
    # Candidate is the unit x-axis; each library vector's x-component IS its cosine
    # to the candidate (unit vectors), so cosine ≈ 0.9 / 0.75 / 0.5 respectively.
    table = {
        candidate_text(cand): [1.0, 0.0],
        candidate_text(dup): [0.9, 0.4358898943540674],
        candidate_text(over): [0.75, 0.6614378277661477],
        candidate_text(clean): [0.5, 0.8660254037844386],
    }
    cfg = NearDupConfig()

    # ONE batched call over the whole library ⇒ best hit is the duplicate.
    emb = _FakeEmbedder(table)
    hit = asyncio.run(find_near_dup_embedded(cand, [dup, over, clean], embedder=emb, config=cfg))
    assert emb.calls == 1  # a single batched embed
    assert hit is not None
    assert hit.method == "embedder"
    assert hit.verdict == "duplicate"
    assert hit.name == "dup-pack"
    assert hit.score == pytest.approx(0.9)

    # The 0.70-0.85 band is an overlap-warn (not an update).
    over_hit = asyncio.run(
        find_near_dup_embedded(cand, [over], embedder=_FakeEmbedder(table), config=cfg)
    )
    assert over_hit is not None
    assert over_hit.verdict == "overlap"
    assert over_hit.score == pytest.approx(0.75)

    # Below the overlap floor ⇒ no hit.
    clean_hit = asyncio.run(
        find_near_dup_embedded(cand, [clean], embedder=_FakeEmbedder(table), config=cfg)
    )
    assert clean_hit is None


def test_embedder_live_slot_or_skip() -> None:
    """LIVE-PARKABLE: a real in-VPC TEI round-trip when the slot is configured.

    MUST skip-with-reason when unconfigured — never vacuously green (highest-care
    A: a test that passes without exercising the thing proves nothing).
    """
    eps = importlib.metadata.entry_points(group="ne.plugins.embedder")
    url = os.environ.get("NE_SKILLS_TEST_EMBEDDER_URL")
    if not eps or not url:
        pytest.skip(
            "ne.plugins.embedder slot unconfigured (NE_SKILLS_TEST_EMBEDDER_URL unset) "
            "— embedder near-dup path not exercised"
        )
    # Live path (parked): resolve the injected embedder + assert a real duplicate
    # is detected against a near-identical pack via a TEI round-trip.
    (ep,) = tuple(eps)[:1]
    embedder = ep.load()()
    cand = make_pack("live-cand", triggers=("provision a new tenant",))
    other = make_pack("live-other", triggers=("provision a brand new tenant",))
    hit = asyncio.run(
        find_near_dup_embedded(cand, [other], embedder=embedder, config=NearDupConfig())
    )
    assert hit is not None
    assert hit.method == "embedder"
