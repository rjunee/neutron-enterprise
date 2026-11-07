# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic generator for the public SQuAD-derived retrieval golden set.

Downloads the SQuAD v1.1 dev set (CC BY-SA 4.0; Rajpurkar et al.), selects a
FROZEN, hardcoded list of article titles, emits every paragraph of those
articles as a retrieval corpus document, and selects exactly 100 questions
(file order, max 2 per paragraph, answer span required verbatim in the
paragraph) as a golden set in NE-024's :class:`ne.eval.golden.GoldenSet` JSONL
format.

The output is a pure, deterministic function of the downloaded source file: the
selection order, ids, and sort order are all fixed, so re-running against the
recorded-sha256 source reproduces ``squad_corpus.jsonl`` and ``squad_golden.jsonl``
byte-for-byte (guarded by ``tests/test_public_golden_set.py``). Only stdlib +
``ne.eval.golden`` are used. See ``DATA-LICENSE.md`` for attribution + license.

Usage::

    python packages/ne-eval/goldensets/public/generate_squad_subset.py

The recorded source digest is asserted unless ``--allow-sha-drift`` is passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path

from ne.eval.golden import GoldenItem, GoldenSet, save_golden_set

#: The SQuAD v1.1 development set (CC BY-SA 4.0).
SOURCE_URL = "https://rajpurkar.github.io/SQuAD-explorer/dataset/dev-v1.1.json"

#: SHA-256 of the source file this fixture was generated from (byte-stability anchor).
SOURCE_SHA256 = "95aa6a52d5d6a735563366753ca50492a658031da74f301ac5238b03966972c9"

#: FROZEN article selection — diverse topics keep cross-article confusion low.
#: Paragraph counts (dev-v1.1): Normans 45, Oxygen 43, Steam_engine 46,
#: Victoria_(Australia) 25 -> 159 corpus documents (within the [120, 220] band).
ARTICLE_TITLES: tuple[str, ...] = (
    "Normans",
    "Oxygen",
    "Steam_engine",
    "Victoria_(Australia)",
)

#: Exactly this many golden questions are selected (file order).
TARGET_QUESTIONS = 100

#: At most this many questions are taken from any single paragraph.
MAX_QUESTIONS_PER_PARAGRAPH = 2

_SLUG_RE = re.compile(r"[^a-z0-9]+")

_HERE = Path(__file__).resolve().parent
CORPUS_PATH = _HERE / "squad_corpus.jsonl"
GOLDEN_PATH = _HERE / "squad_golden.jsonl"


def slugify(title: str) -> str:
    """Lowercase ``title`` and collapse non ``[a-z0-9]`` runs to single hyphens."""
    return _SLUG_RE.sub("-", title.lower()).strip("-")


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:
        return response.read()


def _load_articles(raw: bytes) -> dict[str, list[dict[str, object]]]:
    data = json.loads(raw)
    by_title: dict[str, list[dict[str, object]]] = {}
    for article in data["data"]:
        by_title[article["title"]] = article["paragraphs"]
    return by_title


def build(raw: bytes) -> tuple[list[dict[str, str]], GoldenSet]:
    """Return ``(corpus_rows, golden_set)`` derived purely from ``raw``."""
    by_title = _load_articles(raw)
    corpus_rows: list[dict[str, str]] = []
    items: list[GoldenItem] = []
    question_number = 0

    for title in ARTICLE_TITLES:
        if title not in by_title:
            raise SystemExit(f"article {title!r} not found in source SQuAD dev set")
        slug = slugify(title)
        for para_idx, paragraph in enumerate(by_title[title]):
            document_id = f"squad-{slug}-p{para_idx:02d}"
            context = str(paragraph["context"])
            corpus_rows.append({"document_id": document_id, "title": title, "text": context})
            if len(items) >= TARGET_QUESTIONS:
                continue
            haystack = context.casefold()
            taken = 0
            for qa in paragraph["qas"]:  # type: ignore[index]
                if len(items) >= TARGET_QUESTIONS or taken >= MAX_QUESTIONS_PER_PARAGRAPH:
                    break
                answers = qa["answers"]
                if not answers:
                    continue
                answer_text = str(answers[0]["text"])
                if answer_text.casefold() not in haystack:
                    continue
                question_number += 1
                items.append(
                    GoldenItem(
                        id=f"squad-{slug}-q{question_number:03d}",
                        question=str(qa["question"]),
                        reference_answer=answer_text,
                        relevant_document_ids=(document_id,),
                        tags=("squad", slug),
                    )
                )
                taken += 1

    if len(items) != TARGET_QUESTIONS:
        raise SystemExit(
            f"selected {len(items)} questions, expected exactly {TARGET_QUESTIONS}; "
            "widen the article selection"
        )
    corpus_rows.sort(key=lambda row: row["document_id"])
    return corpus_rows, GoldenSet(items=tuple(items))


def _write_corpus(rows: list[dict[str, str]], path: Path) -> None:
    lines = [
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True) for row in rows
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-sha-drift", action="store_true")
    args = parser.parse_args(argv)

    raw = _download(SOURCE_URL)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SOURCE_SHA256 and not args.allow_sha_drift:
        raise SystemExit(
            f"source SHA-256 drift: expected {SOURCE_SHA256}, got {digest}; "
            "pass --allow-sha-drift to regenerate against a changed source"
        )

    corpus_rows, golden = build(raw)
    _write_corpus(corpus_rows, CORPUS_PATH)
    save_golden_set(golden, GOLDEN_PATH)
    print(
        f"wrote {len(corpus_rows)} corpus docs -> {CORPUS_PATH.name} and "
        f"{len(golden.items)} golden questions -> {GOLDEN_PATH.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
