# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic generator for the Acme (NE-075) retrieval + permission golden sets.

Reads the FROZEN, first-party synthetic ``acme-v1`` fixture corpus at
``tools/acme/fixtures/acme-v1/`` (500 docs; generated in-repo by ``ne-acme 0.1.0``,
seed 74, sha256-pinned by its ``manifest.json``) and emits TWO canonical JSONL
golden files that are consumed by DIFFERENT scorers and MUST NOT be mixed:

* ``acme_golden.jsonl`` — **100 general retrieval items** — one per ground-truth
  doc whose numeric id is divisible by 5 (a fixed 1-in-5 SPREAD across the corpus,
  never a handful of docs), taking that doc's FIRST fact. Each item is tagged with
  its doc's ``acl.json`` visibility class (``public`` / ``department`` /
  ``exec_only`` / ``named``). This is the set scored by ``RagEvalSuite`` /
  ``ne-eval baseline`` for ``recall_at_10`` etc.: every ``relevant_document_ids``
  entry is a doc the question's answer genuinely lives in, so it is legitimately
  retrieval-relevant. It carries ZERO permission-trap rows by construction.

* ``acme_permission_traps.jsonl`` — **12 permission-trap items** — a FROZEN matrix
  (embedded below, each row pre-verified against ``acl.json``/``org.json``) pinning
  one of the four canonical personas to an EXPECTED-DENIED doc plus an
  EXPECTED-VISIBLE control. The trap matrix is carried on the item TAGS
  (``persona:`` / ``denied:`` / ``visible:`` / ``allowed:``) so this file — not a
  second copy in the test — is the single source of the matrix (SPEC §11
  derive-not-transliterate). These rows are scored ONLY by the persona-aware
  permission gate (``test_acme_permission_traps.py`` +
  ``permission_trap_pass_rate``); their ``relevant_document_ids`` names the doc the
  persona must NOT retrieve, so they are DELIBERATELY kept out of the retrieval set
  — feeding them to ``recall_at_k`` would score a correctly-DENIED (secure) result
  as a retrieval miss and contaminate the retrieval/judge metrics.

The output is a pure, deterministic function of the pinned fixture bytes: the
selection order, ids, tags, and sort order are all fixed, so re-running against
the recorded-sha256 fixtures reproduces both files byte-for-byte (guarded by
``tests/test_acme_golden_set.py``). Only stdlib + ``ne.eval.golden`` are used. The
acme-v1 corpus is first-party synthetic data — no external attribution
(``NOTICE`` is untouched, unlike the public SQuAD set).

Usage::

    uv run python packages/ne-eval/goldensets/acme/generate_acme_golden.py

Both files are written; the recorded fixture digests are asserted unless
``--allow-sha-drift`` is passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ne.eval.golden import GoldenItem, GoldenSet, golden_set_hash, save_golden_set

#: Repo root, then the frozen acme-v1 fixture directory (READ-ONLY input).
_REPO_ROOT = Path(__file__).resolve().parents[4]
_FIXTURES = _REPO_ROOT / "tools" / "acme" / "fixtures" / "acme-v1"

_HERE = Path(__file__).resolve().parent
#: Retrieval baseline set (RagEvalSuite / recall_at_10) — general items only.
GOLDEN_PATH = _HERE / "acme_golden.jsonl"
#: Permission-trap set (persona-aware gate only) — NEVER fed to recall_at_k.
TRAPS_PATH = _HERE / "acme_permission_traps.jsonl"

#: SHA-256 of the frozen fixture files this set was authored against (byte anchor).
#: Identical to ``manifest.json['sha256']`` — asserted both here and in the test.
FIXTURE_SHA256: dict[str, str] = {
    "acl.json": "0129ba2f4058b4d6239e2662d888dad5fd1f527a2d5060bb203f7c547322f20c",
    "content.json": "ea8ba0ac3fb85f5bbdbb9e098132cab37578ef247f756b0434ad0d1b8aadbe48",
    "ground_truth.json": "185fcd856f32e0d0066a7f3142a3df9b77f8a844b686ae6d8396484e74ee5e89",
    "org.json": "26a87fadfd6370fd1966ea72d888be9577d0c286de423783ca60e37ad9795e99",
}

#: 1-in-5 spread selector: a doc is a general item iff its numeric id % this == 0.
GENERAL_STRIDE = 5

#: FROZEN permission-trap matrix. Each row pins the four canonical personas to an
#: EXPECTED-DENIED doc (+ fact index) and an EXPECTED-VISIBLE control, plus the
#: doc's canonical ``allowed`` persona. Every row is pre-verified against
#: ``acl.json``/``org.json``: the denied persona's label set is DISJOINT from the
#: denied doc's ACL labels, INTERSECTS the visible control, and the ``allowed``
#: persona INTERSECTS the denied doc (proving the doc is genuinely reachable, not
#: dead). Columns: (persona_slug, persona_id, denied_doc, denied_fact_index,
#: visible_doc, allowed_person).
TRAP_MATRIX: tuple[tuple[str, str, str, int, str, str], ...] = (
    ("p1-exec", "per-0000", "doc-0310", 0, "doc-0010", "per-0006"),
    ("p2-eng", "per-0006", "doc-0010", 0, "doc-0310", "per-0000"),
    ("p2-eng", "per-0006", "doc-0377", 0, "doc-0310", "per-0016"),
    ("p3-sales", "per-0016", "doc-0310", 1, "doc-0377", "per-0006"),
    ("p3-sales", "per-0016", "doc-0000", 0, "doc-0377", "per-0038"),
    ("p4-legal", "per-0038", "doc-0377", 1, "doc-0077", "per-0016"),
    ("p4-legal", "per-0038", "doc-0010", 1, "doc-0000", "per-0000"),
    ("p1-exec", "per-0000", "doc-0077", 0, "doc-0000", "per-0038"),
    ("p2-eng", "per-0006", "doc-0000", 1, "doc-0011", "per-0038"),
    ("p3-sales", "per-0016", "doc-0010", 2, "doc-0011", "per-0000"),
    ("p4-legal", "per-0038", "doc-0310", 2, "doc-0077", "per-0006"),
    ("p1-exec", "per-0000", "doc-0377", 2, "doc-0010", "per-0016"),
)


def _read_bytes(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _assert_fixtures(*, allow_drift: bool) -> None:
    for name, want in FIXTURE_SHA256.items():
        got = hashlib.sha256(_read_bytes(name)).hexdigest()
        if got != want and not allow_drift:
            raise SystemExit(
                f"fixture {name} SHA-256 drift: expected {want}, got {got}; "
                "pass --allow-sha-drift to regenerate against a changed corpus"
            )


def _load(name: str) -> object:
    return json.loads(_read_bytes(name))


def _acl_by_doc() -> dict[str, dict[str, object]]:
    return {
        entry["doc_id"]: entry
        for entry in _load("acl.json")  # type: ignore[union-attr]
    }


def _docs_by_id() -> dict[str, dict[str, object]]:
    return {
        doc["doc_id"]: doc
        for doc in _load("ground_truth.json")["docs"]  # type: ignore[call-overload,index]
    }


def build_retrieval_set() -> GoldenSet:
    """Return the 100-item general RETRIEVAL set (every 1-in-5 doc's first fact).

    This is the set scored by ``RagEvalSuite`` — it carries NO permission-trap rows,
    so ``recall_at_k`` never scores a denied doc as retrieval-relevant.
    """
    acl_by_doc = _acl_by_doc()
    docs_by_id = _docs_by_id()

    items: list[GoldenItem] = []
    for doc_id in sorted(docs_by_id):
        if int(doc_id[4:]) % GENERAL_STRIDE != 0:
            continue
        fact = docs_by_id[doc_id]["facts"][0]  # type: ignore[index]
        visibility = str(acl_by_doc[doc_id]["visibility"])
        items.append(
            GoldenItem(
                id=f"acme-{fact['fact_id']}",
                question=str(fact["question"]),
                reference_answer=str(fact["answer"]),
                relevant_document_ids=(doc_id,),
                tags=(visibility,),
                notes=None,
            )
        )
    return GoldenSet(items=tuple(items))


def build_trap_set() -> GoldenSet:
    """Return the 12-item PERMISSION-TRAP set (the frozen matrix, carried on tags).

    Scored ONLY by the persona-aware permission gate; deliberately kept out of the
    retrieval set because each ``relevant_document_ids`` names a doc the tagged
    persona must NOT retrieve.
    """
    acl_by_doc = _acl_by_doc()
    docs_by_id = _docs_by_id()

    items: list[GoldenItem] = []
    for slug, persona_id, denied_doc, fact_index, visible_doc, allowed_person in TRAP_MATRIX:
        fact = docs_by_id[denied_doc]["facts"][fact_index]  # type: ignore[index]
        visibility = str(acl_by_doc[denied_doc]["visibility"])
        items.append(
            GoldenItem(
                id=f"acme-trap-{slug}-{denied_doc}",
                question=str(fact["question"]),
                reference_answer=str(fact["answer"]),
                relevant_document_ids=(denied_doc,),
                tags=(
                    "permission-trap",
                    f"persona:{persona_id}",
                    f"denied:{denied_doc}",
                    f"visible:{visible_doc}",
                    f"allowed:{allowed_person}",
                    visibility,
                ),
                notes=(
                    f"{persona_id} must NOT see {denied_doc}; "
                    f"control: same persona CAN see {visible_doc}"
                ),
            )
        )
    return GoldenSet(items=tuple(items))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-sha-drift", action="store_true")
    args = parser.parse_args(argv)

    _assert_fixtures(allow_drift=args.allow_sha_drift)
    retrieval = build_retrieval_set()
    traps = build_trap_set()
    save_golden_set(retrieval, GOLDEN_PATH)
    save_golden_set(traps, TRAPS_PATH)
    print(
        f"wrote {len(retrieval.items)} retrieval items -> {GOLDEN_PATH.name}\n"
        f"  golden_set_hash = {golden_set_hash(retrieval)}\n"
        f"wrote {len(traps.items)} permission-trap items -> {TRAPS_PATH.name}\n"
        f"  golden_set_hash = {golden_set_hash(traps)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
