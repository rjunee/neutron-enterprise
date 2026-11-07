# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Identity pins for ``ne.brain_graph.extraction.identity`` (design §3.3)."""

from __future__ import annotations

from ne.brain_graph.extraction.identity import canonical_form, llm_entity_key
from ne.brain_graph.model import entity_key_for


def test_canonical_form_folds_case_punctuation_and_whitespace() -> None:
    assert canonical_form("Acme Corp.") == "acme corp"
    assert canonical_form("acme corp") == "acme corp"
    assert canonical_form(" ACME   CORP") == "acme corp"
    assert canonical_form("Acme Corp.") == canonical_form(" ACME   CORP")


def test_canonical_form_nfkc_normalizes() -> None:
    # U+FB01 LATIN SMALL LIGATURE FI -> NFKC-decomposes to "fi".
    assert canonical_form("ﬁle Corp") == canonical_form("file Corp")


def test_canonical_form_is_deliberate_fragmentation_not_fuzzy_matching() -> None:
    # "Acme" and "Acme Corp." are DIFFERENT canonical forms — no fuzzy healing
    # happens here (design §3.3: the Splink ER lane is the healer, not this).
    assert canonical_form("Acme") != canonical_form("Acme Corp.")
    assert llm_entity_key("Account", "Acme") != llm_entity_key("Account", "Acme Corp.")


def test_llm_entity_key_disjoint_from_backbone_namespaces() -> None:
    llm_key = llm_entity_key("Account", "Acme Corp")
    for source_system in ("salesforce", "public.accounts", "ne:er", "ne:llm"):
        backbone_key = entity_key_for(source_system, "Account", "Account", "A1")
        assert llm_key != backbone_key


def test_llm_entity_key_is_stable_across_calls() -> None:
    a = llm_entity_key("Account", "Acme Corp")
    b = llm_entity_key("Account", "Acme Corp")
    assert a == b


def test_llm_entity_key_differs_by_class() -> None:
    assert llm_entity_key("Account", "Acme") != llm_entity_key("Person", "Acme")
