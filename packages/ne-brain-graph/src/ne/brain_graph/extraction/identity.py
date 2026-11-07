# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic identity for LLM-extracted entities (design §3.3).

``llm_entity_key`` is an ADDITIVE helper over the EXISTING
:func:`ne.brain_graph.model.entity_key_for` — that function's semantics are
UNCHANGED by this unit; extraction simply calls it with the fixed
``source_system="ne:llm"`` / ``source_id="extracted"`` pair, folded through
:func:`canonical_form` for the ``record_id`` argument. This namespaces every
extracted key permanently away from every connector's backbone namespace
(``entity_key_for`` folds ``source_system`` into the UUIDv5 name string), so an
extracted key can NEVER collide with or overwrite a backbone key (design
A-P1-4) — pinned by ``test_extraction_identity.py``.

**Fuzzy keying is deliberately rejected** (design §3.3, stated plainly so a
later unit is not tempted to "fix" fragmentation here): surface-form variance
("Acme" vs "Acme Corp.") that survives :func:`canonical_form`'s casefold /
whitespace-and-punctuation collapse produces TWO distinct keys, and this
package makes NO attempt to reconcile them via similarity matching.
Nondeterministic identity — the same extracted mention resolving to a
different key on a different run, or two DIFFERENT mentions silently
colliding under a loose similarity threshold — is worse than fragmentation:
it breaks the E-P1-1 content-addressed idempotency this whole pathway leans
on, and it can silently merge two distinct real-world entities with no audit
trail. The EXISTING Splink ER lane (``ne.brain_graph.er``) is the healer:
extracted entity classes join ``er_classes`` like any backbone class, and
fragmentation surfaces as an ordinary, review-gated ``ENTITY_MERGE_CANDIDATE``
proposal — the two strictness features compose, they do not duplicate effort.
"""

from __future__ import annotations

import unicodedata

from ne.brain_graph.extraction import LLM_SOURCE_SYSTEM
from ne.brain_graph.model import entity_key_for

__all__ = ["canonical_form", "llm_entity_key"]

_EXTRACTED_SOURCE_ID = "extracted"


def canonical_form(display: str) -> str:
    """A deterministic normalization of an extracted display name.

    Algorithm (pinned by ``test_extraction_identity.py`` — this exact recipe,
    not "something casefold-ish"): NFKC-normalize (``unicodedata.normalize``),
    casefold, then collapse every RUN of non-alphanumeric characters to a
    single space and strip. Pure function of its input — no locale, no clock,
    no external state, so the same display name always folds to the same
    canonical form on every run, on every process.
    """
    normalized = unicodedata.normalize("NFKC", display).casefold()
    runs: list[str] = []
    current: list[str] = []
    for ch in normalized:
        if ch.isalnum():
            current.append(ch)
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return " ".join(runs)


def llm_entity_key(entity_class: str, display: str) -> str:
    """The deterministic UUIDv5 key for an LLM-extracted entity (design §3.3).

    ``entity_key_for("ne:llm", entity_class, "extracted", canonical_form(display))``
    — namespace-separated from every connector's backbone keys by construction
    (``entity_key_for`` folds ``source_system`` into its UUIDv5 name string), and
    stable across processes/runs for the SAME (class, canonical display) pair.
    """
    return entity_key_for(
        LLM_SOURCE_SYSTEM, entity_class, _EXTRACTED_SOURCE_ID, canonical_form(display)
    )
