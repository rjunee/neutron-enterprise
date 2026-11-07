# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The NE-SKILL-3 near-dup detector — anti-pollution DUPLICATION, stdlib-clean.

Run at PROPOSAL time over the WHOLE verified registry (all sources, not just
forged). On a duplicate hit the forge proposes an UPDATE to the existing skill
(``author(existing=…)`` ⇒ a ``SKILL_UPDATE_CANDIDATE`` at ``current_version+1``),
NEVER a ``-2`` sibling — the registry's unique ``(name, version)`` + append-only
versions make siblings structurally impossible, so this module adds NO suffix
logic anywhere.

Two paths, both honouring SPEC section 3.1 keyword-default degradation:

- **Default install (no embedder) — stdlib only, C4-clean:** a shared exact
  trigger phrase (casefold+strip match between trigger lists) OR token-set Jaccard
  ``>=`` :attr:`NearDupConfig.jaccard_threshold` over the candidate text
  (``name + description + triggers``). Fully functional with no embedder wired.
- **Embedder opt-in (``ne.plugins.embedder`` slot configured, in-VPC TEI,
  C2-clean):** ONE batched ``embed`` over the candidate + the non-same-name
  library, pure-python cosine (no numpy); ``>=`` :attr:`cosine_duplicate` ⇒
  ``duplicate``, ``[cosine_overlap, cosine_duplicate)`` ⇒ ``overlap`` (warn-only).

The candidate's OWN name is always skipped (no self-dup). Thresholds are config,
tuned on the real library. This module imports NOTHING from ``ne.runtime`` /
``pydantic_ai`` / ``ne.brain`` — the embedder arrives ONLY as the frozen
:class:`ne.contracts.knowledge.Embedder` Protocol, injected at the composition
root (the D-10 boundary; ``tests/test_import_boundary.py`` guards it).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ne.contracts.knowledge import Embedder
from ne.contracts.skills import SkillPack


class NearDupConfig(BaseModel):
    """Frozen near-dup thresholds (tuned on the real library, config-overridable)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jaccard_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    cosine_duplicate: float = Field(default=0.85, ge=0.0, le=1.0)
    cosine_overlap: float = Field(default=0.70, ge=0.0, le=1.0)


class NearDupHit(BaseModel):
    """One near-dup match against an EXISTING registry pack (never self)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: int
    method: Literal["jaccard", "trigger_phrase", "embedder"]
    score: float
    verdict: Literal["duplicate", "overlap"]


def _tokens(text: str) -> list[str]:
    """The SAME tokenizer ``ne.skills.tools`` / ``ne.skills.evals`` use."""
    return re.findall(r"[a-z0-9]+", text.lower())


def candidate_text(pack: SkillPack) -> str:
    """The near-dup comparison text: name + description + triggers (names-only)."""
    return " ".join((pack.name, pack.description, *pack.triggers))


def _shared_trigger_phrase(candidate: SkillPack, other: SkillPack) -> bool:
    """True when the two packs share an exact (casefold+strip) trigger phrase."""
    cand = {t.strip().casefold() for t in candidate.triggers if t.strip()}
    lib = {t.strip().casefold() for t in other.triggers if t.strip()}
    return bool(cand & lib)


def _jaccard(candidate: SkillPack, other: SkillPack) -> float:
    """Token-SET Jaccard of the two candidate texts (empty union ⇒ 0.0)."""
    a = set(_tokens(candidate_text(candidate)))
    b = set(_tokens(candidate_text(other)))
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Pure-python cosine similarity (no numpy). Zero-norm ⇒ 0.0."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def find_near_dup(
    candidate: SkillPack,
    library: Sequence[SkillPack],
    *,
    config: NearDupConfig,
) -> NearDupHit | None:
    """Default (keyword) near-dup: shared trigger phrase OR Jaccard >= threshold.

    Scans the WHOLE library (any source), skipping the candidate's own name. A
    shared exact trigger phrase is a ``duplicate`` at score ``1.0`` (method
    ``trigger_phrase``); otherwise a token-set Jaccard ``>=``
    ``config.jaccard_threshold`` is a ``duplicate`` (method ``jaccard``). Returns
    the BEST-scoring hit, or ``None`` when nothing crosses a threshold.
    """
    best: NearDupHit | None = None
    for other in library:
        if other.name == candidate.name:
            continue
        if _shared_trigger_phrase(candidate, other):
            hit = NearDupHit(
                name=other.name,
                version=other.version,
                method="trigger_phrase",
                score=1.0,
                verdict="duplicate",
            )
        else:
            score = _jaccard(candidate, other)
            if score < config.jaccard_threshold:
                continue
            hit = NearDupHit(
                name=other.name,
                version=other.version,
                method="jaccard",
                score=score,
                verdict="duplicate",
            )
        if best is None or hit.score > best.score:
            best = hit
    return best


async def find_near_dup_embedded(
    candidate: SkillPack,
    library: Sequence[SkillPack],
    *,
    embedder: Embedder,
    config: NearDupConfig,
) -> NearDupHit | None:
    """Embedder near-dup: cosine >= duplicate ⇒ ``duplicate``, [overlap, duplicate) ⇒ ``overlap``.

    ONE batched ``embed`` over the candidate + the non-same-name library, then a
    pure-python cosine per pair. Returns the BEST-scoring hit (or ``None``); an
    ``overlap`` verdict is a warn-only annotation the forge keeps as a
    ``SKILL_CANDIDATE`` (not an update).
    """
    others = [p for p in library if p.name != candidate.name]
    if not others:
        return None
    texts = [candidate_text(candidate), *[candidate_text(p) for p in others]]
    vectors = await embedder.embed(texts)
    cand_vec = vectors[0]
    best: NearDupHit | None = None
    for other, vec in zip(others, vectors[1:], strict=False):
        sim = _cosine(cand_vec, vec)
        if sim >= config.cosine_duplicate:
            verdict: Literal["duplicate", "overlap"] = "duplicate"
        elif sim >= config.cosine_overlap:
            verdict = "overlap"
        else:
            continue
        hit = NearDupHit(
            name=other.name,
            version=other.version,
            method="embedder",
            score=sim,
            verdict=verdict,
        )
        if best is None or hit.score > best.score:
            best = hit
    return best


__all__ = [
    "NearDupConfig",
    "NearDupHit",
    "candidate_text",
    "find_near_dup",
    "find_near_dup_embedded",
]
