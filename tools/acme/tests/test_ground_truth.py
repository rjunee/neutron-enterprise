# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The Q&A ground truth is real: every fact's answer is literally findable in
its document's body, and every fact resolves to a real document."""

from __future__ import annotations

import json
from pathlib import Path

from ne.acme.models import GroundTruth

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "acme-v1"


def _load_ground_truth() -> GroundTruth:
    return GroundTruth.model_validate(json.loads((FIXTURES / "ground_truth.json").read_bytes()))


def test_fact_answers_appear_in_content() -> None:
    ground_truth = _load_ground_truth()
    content = json.loads((FIXTURES / "content.json").read_bytes())
    for doc_gt in ground_truth.docs:
        body = content[doc_gt.doc_id]
        for fact in doc_gt.facts:
            assert fact.answer in body, (
                f"fact {fact.fact_id} answer {fact.answer!r} not found in {doc_gt.doc_id} body"
            )


def test_fact_ids_unique_and_docs_resolve() -> None:
    ground_truth = _load_ground_truth()
    doc_ids = {doc_gt.doc_id for doc_gt in ground_truth.docs}

    all_fact_ids: list[str] = []
    for doc_gt in ground_truth.docs:
        assert 1 <= len(doc_gt.facts) <= 3
        for fact in doc_gt.facts:
            assert fact.doc_id in doc_ids
            all_fact_ids.append(fact.fact_id)

    assert len(all_fact_ids) == len(set(all_fact_ids))
