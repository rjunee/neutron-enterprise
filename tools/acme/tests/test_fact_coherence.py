# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Ground-truth coherence: account facts only on account-linked docs; every
question has exactly one answer corpus-wide (the invariant NE-075's golden
set + permission-trap eval depends on)."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "acme-v1"


def _load() -> tuple[dict, dict, dict]:
    records = json.loads((FIXTURES / "records.json").read_bytes())
    ground_truth = json.loads((FIXTURES / "ground_truth.json").read_bytes())
    content = json.loads((FIXTURES / "content.json").read_bytes())
    return records, ground_truth, content


def test_no_account_fact_on_non_account_doc() -> None:
    records, ground_truth, content = _load()
    docs_by_id = {d["id"]: d for d in records["documents"]}
    customer_names = {a["name"] for a in records["accounts"] if a["id"] != "acct-0000"}

    for doc_gt in ground_truth["docs"]:
        doc_id = doc_gt["doc_id"]
        doc = docs_by_id[doc_id]
        if doc.get("about"):
            continue
        body = content[doc_id]
        for name in customer_names:
            assert name not in body, f"{doc_id} body unexpectedly mentions {name!r}"
        for fact in doc_gt["facts"]:
            for field in ("statement", "question", "answer"):
                text = fact[field]
                for name in customer_names:
                    assert name not in text, (
                        f"fact {fact['fact_id']} {field} mentions {name!r} on "
                        f"non-account-linked doc {doc_id}"
                    )


def test_account_facts_cite_only_their_account() -> None:
    records, ground_truth, _content = _load()
    docs_by_id = {d["id"]: d for d in records["documents"]}
    customer_names = {a["name"] for a in records["accounts"] if a["id"] != "acct-0000"}
    name_by_id = {a["id"]: a["name"] for a in records["accounts"]}

    for doc_gt in ground_truth["docs"]:
        doc_id = doc_gt["doc_id"]
        doc = docs_by_id[doc_id]
        about = doc.get("about")
        if not about:
            continue
        linked_name = name_by_id[about[0]]
        for fact in doc_gt["facts"]:
            for field in ("statement", "question", "answer"):
                text = fact[field]
                for name in customer_names:
                    if name in text:
                        assert name == linked_name, (
                            f"fact {fact['fact_id']} {field} cites {name!r} but doc "
                            f"{doc_id} is linked to {linked_name!r}"
                        )


def test_questions_have_single_answer_corpus_wide() -> None:
    _records, ground_truth, _content = _load()
    answers: dict[str, set[str]] = {}
    for doc_gt in ground_truth["docs"]:
        for fact in doc_gt["facts"]:
            answers.setdefault(fact["question"], set()).add(fact["answer"])

    for question, answer_set in answers.items():
        assert len(answer_set) == 1, (
            f"question {question!r} has conflicting answers corpus-wide: {answer_set!r}"
        )
