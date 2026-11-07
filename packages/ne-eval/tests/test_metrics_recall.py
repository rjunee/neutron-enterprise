# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic recall@k tests (hand-computed)."""

from __future__ import annotations

from ne.eval.metrics import mean_score, recall_at_k
from ne.eval.testing import make_golden_item, make_retrieval_result


def test_all_found() -> None:
    golden = make_golden_item("i1", "q", relevant_document_ids=("docA", "docB"))
    rr = make_retrieval_result([("c1", "docA", "t", 0.9), ("c2", "docB", "t", 0.8)])
    assert recall_at_k(golden, rr).score == 1.0


def test_one_of_two_found() -> None:
    golden = make_golden_item("i1", "q", relevant_document_ids=("docA", "docB"))
    rr = make_retrieval_result([("c1", "docA", "t", 0.9), ("c2", "docZ", "t", 0.8)])
    assert recall_at_k(golden, rr).score == 0.5


def test_none_found() -> None:
    golden = make_golden_item("i1", "q", relevant_document_ids=("docA",))
    rr = make_retrieval_result([("c2", "docZ", "t", 0.8)])
    assert recall_at_k(golden, rr).score == 0.0


def test_relevant_at_rank_11_not_counted() -> None:
    golden = make_golden_item("i1", "q", relevant_document_ids=("docX",))
    chunks = [(f"c{i}", f"doc{i}", "t", 1.0 - i * 0.01) for i in range(10)]
    chunks.append(("c-x", "docX", "t", 0.5))  # lowest score -> rank 11
    rr = make_retrieval_result(chunks)
    assert recall_at_k(golden, rr, k=10).score == 0.0


def test_duplicate_chunks_same_doc_counted_once() -> None:
    golden = make_golden_item("i1", "q", relevant_document_ids=("docA",))
    rr = make_retrieval_result([("c1", "docA", "t1", 0.9), ("c2", "docA", "t2", 0.8)])
    score = recall_at_k(golden, rr)
    assert score.score == 1.0
    assert score.detail["found"] == ["docA"]


def test_empty_relevant_excluded_from_mean() -> None:
    included = make_golden_item("i1", "q", relevant_document_ids=("docA",))
    excluded = make_golden_item("i2", "q2", relevant_document_ids=())
    rr = make_retrieval_result([("c1", "docA", "t", 0.9)])
    s_inc = recall_at_k(included, rr)
    s_exc = recall_at_k(excluded, rr)
    assert s_exc.detail["excluded"] is True
    assert mean_score([s_inc, s_exc]) == 1.0
    assert mean_score([s_exc]) is None
