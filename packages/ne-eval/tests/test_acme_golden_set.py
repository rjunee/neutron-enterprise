# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Integrity guards for the committed Acme (NE-075) golden set + fixture corpus.

Mirrors ``tests/test_public_golden_set.py`` VERBATIM in spirit: it loads only
committed local files (no network, no Docker, no live tenant) and asserts the
set's shape, that every reference answer appears verbatim in its labeled corpus
doc, the SIGNED content-hash pin (editing the set without re-pinning fails CI),
the frozen fixture sha256 pins, and the pinned NE-077 ThresholdSpec set. Brain-
free on purpose (like the public guard); the production-ACL trap assertions live
in ``test_acme_permission_traps.py``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ne.eval.golden import filter_by_tag, golden_set_hash, load_golden_set
from ne.eval.thresholds import (
    DEFAULT_THRESHOLDS,
    MetricStatus,
    ThresholdSpec,
    evaluate_thresholds,
)

_ACME = Path(__file__).resolve().parent.parent / "goldensets" / "acme"
#: The general RETRIEVAL set (RagEvalSuite / recall_at_10) — 100 items, NO traps.
GOLDEN_PATH = _ACME / "acme_golden.jsonl"
#: The PERMISSION-TRAP set (persona-aware gate only) — 12 items, never scored by recall.
TRAPS_PATH = _ACME / "acme_permission_traps.jsonl"
THRESHOLDS_PATH = _ACME / "thresholds.acme.json"

_FIXTURES = Path(__file__).resolve().parents[3] / "tools" / "acme" / "fixtures" / "acme-v1"

#: The SIGNED integrity pins: golden_set_hash of each committed jsonl file.
#: Regenerate with ``uv run ne-eval hash-golden`` and update here on any set edit.
ACME_GOLDEN_SET_SHA256 = "435f4a4760c2ddfb981a890ee4f470de7e2813416d270468a42da16a8d0f3e91"
ACME_TRAP_SET_SHA256 = "446683b67870c2037a5bd9f1e0fc032ce99405680d58f32d720d832ef1dfc884"

#: sha256 of the frozen acme-v1 fixture inputs the set was authored against.
FIXTURE_SHA256: dict[str, str] = {
    "acl.json": "0129ba2f4058b4d6239e2662d888dad5fd1f527a2d5060bb203f7c547322f20c",
    "content.json": "ea8ba0ac3fb85f5bbdbb9e098132cab37578ef247f756b0434ad0d1b8aadbe48",
    "ground_truth.json": "185fcd856f32e0d0066a7f3142a3df9b77f8a844b686ae6d8396484e74ee5e89",
    "org.json": "26a87fadfd6370fd1966ea72d888be9577d0c286de423783ca60e37ad9795e99",
}

_CANONICAL_PERSONAS = frozenset({"per-0000", "per-0006", "per-0016", "per-0038"})


def _corpus() -> dict[str, str]:
    return json.loads((_FIXTURES / "content.json").read_text(encoding="utf-8"))


def _acl_by_doc() -> dict[str, dict[str, object]]:
    entries = json.loads((_FIXTURES / "acl.json").read_text(encoding="utf-8"))
    return {entry["doc_id"]: entry for entry in entries}


def _org_member_ids() -> set[str]:
    org = json.loads((_FIXTURES / "org.json").read_text(encoding="utf-8"))
    return {member["person_id"] for member in org["members"]}


def _load_thresholds() -> tuple[ThresholdSpec, ...]:
    raw = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    return tuple(ThresholdSpec.model_validate(entry) for entry in raw)


def test_retrieval_set_loads_exactly_100_items() -> None:
    golden = load_golden_set(GOLDEN_PATH)
    assert len(golden.items) == 100


def test_authored_question_count_is_at_least_100() -> None:
    # SPEC row: "100+ SME-style questions incl. permission-trap cases".
    total = len(load_golden_set(GOLDEN_PATH).items) + len(load_golden_set(TRAPS_PATH).items)
    assert total == 112
    assert total >= 100


def test_retrieval_set_excludes_permission_traps() -> None:
    # The heart of the NE-075 fix: the recall-scored file carries ZERO trap rows,
    # so a correctly-DENIED (secure) result can never be counted as a retrieval
    # miss. Trap rows live only in the persona-aware permission-trap set.
    golden = load_golden_set(GOLDEN_PATH)
    assert filter_by_tag(golden, "permission-trap").items == ()
    for item in golden.items:
        assert not item.id.startswith("acme-trap-"), item.id
        assert "permission-trap" not in item.tags, item.id


def test_labels_resolve_into_corpus_and_answers_present() -> None:
    # Runs over BOTH files: every authored reference answer (retrieval + trap) must
    # appear verbatim in its labeled corpus doc (replicates the public guard).
    corpus = _corpus()
    items = load_golden_set(GOLDEN_PATH).items + load_golden_set(TRAPS_PATH).items
    for item in items:
        assert item.relevant_document_ids, f"{item.id} has no relevant document"
        for doc_id in item.relevant_document_ids:
            assert doc_id in corpus, f"{item.id} references missing corpus doc {doc_id}"
        doc_text = corpus[item.relevant_document_ids[0]].casefold()
        assert item.reference_answer.casefold() in doc_text, (
            f"{item.id} reference answer not found verbatim in its labeled document"
        )


def test_corpus_spread() -> None:
    # Spread is measured over the whole authored set (retrieval + trap items).
    acl = _acl_by_doc()
    items = load_golden_set(GOLDEN_PATH).items + load_golden_set(TRAPS_PATH).items

    labeled = {item.relevant_document_ids[0] for item in items}
    assert len(labeled) >= 100, f"only {len(labeled)} distinct labeled docs; too concentrated"

    buckets: dict[str, int] = {}
    for item in items:
        visibility = str(acl[item.relevant_document_ids[0]]["visibility"])
        buckets[visibility] = buckets.get(visibility, 0) + 1
    assert set(buckets) == {"public", "department", "exec_only", "named"}
    for visibility, count in buckets.items():
        assert count >= 10, f"visibility class {visibility!r} underrepresented ({count} items)"


def test_files_sorted_and_single_trailing_newline() -> None:
    for path in (GOLDEN_PATH, TRAPS_PATH):
        raw = path.read_text(encoding="utf-8")
        assert raw.endswith("\n"), path.name
        assert not raw.endswith("\n\n"), path.name
        ids = [json.loads(line)["id"] for line in raw.splitlines()]
        assert ids == sorted(ids), f"{path.name} is not sorted by id"


def test_golden_set_hashes_pinned() -> None:
    # The 'signed' pins: editing either set without re-pinning fails CI.
    assert golden_set_hash(load_golden_set(GOLDEN_PATH)) == ACME_GOLDEN_SET_SHA256
    assert golden_set_hash(load_golden_set(TRAPS_PATH)) == ACME_TRAP_SET_SHA256


def test_fixture_files_match_pinned_sha256() -> None:
    manifest = json.loads((_FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    for name, want in FIXTURE_SHA256.items():
        got = hashlib.sha256((_FIXTURES / name).read_bytes()).hexdigest()
        assert got == want, f"{name} drifted from the test pin"
        assert got == manifest["sha256"][name], f"{name} drifted from manifest.json"


def test_thresholds_pinned_to_ne077_gate_numbers() -> None:
    specs = _load_thresholds()
    by_metric = {spec.metric: spec for spec in specs}

    # The 4 retrieval/judge specs must EQUAL the SPEC constant (derive-not-copy).
    for default in DEFAULT_THRESHOLDS:
        assert by_metric[default.metric] == default, (
            f"threshold for {default.metric} drifted from DEFAULT_THRESHOLDS"
        )

    # The hard, fail-closed permission-trap gate.
    trap_spec = by_metric["permission_trap_pass_rate"]
    assert trap_spec == ThresholdSpec(
        metric="permission_trap_pass_rate", threshold=1.0, tolerance=0.0
    )

    # Fail-closed semantics: one leak FAILs, a perfect rate PASSes, absent FAILs.
    trap_only = [trap_spec]
    assert (
        evaluate_thresholds({"permission_trap_pass_rate": 11 / 12}, trap_only)[
            "permission_trap_pass_rate"
        ]
        is MetricStatus.FAIL
    )
    assert (
        evaluate_thresholds({"permission_trap_pass_rate": 1.0}, trap_only)[
            "permission_trap_pass_rate"
        ]
        is MetricStatus.PASS
    )
    assert evaluate_thresholds({}, trap_only)["permission_trap_pass_rate"] is MetricStatus.FAIL


def test_permission_trap_items_selectable_and_well_formed() -> None:
    trap_set = load_golden_set(TRAPS_PATH)
    traps = filter_by_tag(trap_set, "permission-trap")
    assert len(traps.items) == 12
    assert len(trap_set.items) == 12  # the file is exclusively permission-trap rows

    acl = _acl_by_doc()
    members = _org_member_ids()
    for item in traps.items:
        parsed: dict[str, str] = {}
        for tag in item.tags:
            for key in ("persona", "denied", "visible", "allowed"):
                if tag.startswith(f"{key}:"):
                    assert key not in parsed, f"{item.id} has duplicate {key}: tag"
                    parsed[key] = tag.split(":", 1)[1]
        assert set(parsed) == {"persona", "denied", "visible", "allowed"}, item.id
        assert parsed["persona"] in _CANONICAL_PERSONAS, item.id
        assert parsed["denied"] in acl, item.id
        assert parsed["visible"] in acl, item.id
        assert parsed["allowed"] in members, item.id
        assert item.relevant_document_ids == (parsed["denied"],), item.id
