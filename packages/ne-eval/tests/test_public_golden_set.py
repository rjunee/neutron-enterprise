# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Integrity guards for the committed public SQuAD golden set + corpus fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from ne.eval.golden import load_golden_set

_PUBLIC = Path(__file__).resolve().parent.parent / "goldensets" / "public"
CORPUS_PATH = _PUBLIC / "squad_corpus.jsonl"
GOLDEN_PATH = _PUBLIC / "squad_golden.jsonl"


def _load_corpus() -> dict[str, str]:
    rows = [json.loads(line) for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines()]
    return {row["document_id"]: row["text"] for row in rows}


def test_golden_set_loads_exactly_100_items() -> None:
    golden = load_golden_set(GOLDEN_PATH)
    assert len(golden.items) == 100


def test_labels_resolve_into_corpus_and_answers_present() -> None:
    golden = load_golden_set(GOLDEN_PATH)
    corpus = _load_corpus()
    for item in golden.items:
        assert item.relevant_document_ids, f"{item.id} has no relevant document"
        for doc_id in item.relevant_document_ids:
            assert doc_id in corpus, f"{item.id} references missing corpus doc {doc_id}"
        doc_text = corpus[item.relevant_document_ids[0]].casefold()
        assert item.reference_answer.casefold() in doc_text, (
            f"{item.id} reference answer not found verbatim in its labeled document"
        )


def test_corpus_size_within_band() -> None:
    assert 120 <= len(_load_corpus()) <= 220


def test_files_sorted_and_single_trailing_newline() -> None:
    corpus_raw = CORPUS_PATH.read_text(encoding="utf-8")
    golden_raw = GOLDEN_PATH.read_text(encoding="utf-8")
    for raw in (corpus_raw, golden_raw):
        assert raw.endswith("\n")
        assert not raw.endswith("\n\n")

    corpus_ids = [json.loads(line)["document_id"] for line in corpus_raw.splitlines()]
    assert corpus_ids == sorted(corpus_ids), "corpus is not sorted by document_id"

    golden_ids = [json.loads(line)["id"] for line in golden_raw.splitlines()]
    assert golden_ids == sorted(golden_ids), "golden set is not sorted by id"
