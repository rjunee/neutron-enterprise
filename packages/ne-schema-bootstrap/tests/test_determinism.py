# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The epic's defining property: byte-for-byte pipeline determinism.

Property-style WITHOUT ``hypothesis`` (not a workspace dependency): seeded
generation loops build a randomized-but-reproducible sample corpus per seed,
write it twice (identical content, different file-creation order), run the
full profile+infer pipeline twice per directory, and assert all four results
are byte-identical JSON. A sensitivity control (append one row) proves the
comparison has teeth — it is not comparing an object to itself.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path

from ne.schema_bootstrap.infer import infer_candidate_model
from ne.schema_bootstrap.profiles import profile_samples

_STATUSES = ("active", "inactive", "pending")


def _build_corpus(
    rng: random.Random,
) -> list[tuple[str, list[str], list[list[str]]]]:
    """Build ``[(table_name, columns, rows), ...]`` — 2-4 tables, 50-500 rows,
    at least one exceeding the default row cap (200), with nullable, enum-ish,
    FK-ish, and PII-ish columns."""
    n_tables = rng.randint(2, 4)
    tables: list[tuple[str, list[str], list[list[str]]]] = []
    id_pools: dict[str, list[str]] = {}

    for i in range(n_tables):
        name = f"entity{i}"
        n_rows = rng.randint(220, 500) if i == 0 else rng.randint(50, 500)
        ids = [f"{name[0]}{j:04d}" for j in range(n_rows)]
        id_pools[name] = ids

        columns = ["id", "label", "email", "status", "note", "score"]
        rows: list[list[str]] = []
        for j in range(n_rows):
            label = f"Label{rng.randint(0, 9999)}"
            email = f"user{rng.randint(0, 9999)}@example{rng.randint(0, 5)}.com"
            status = _STATUSES[rng.randint(0, 2)]
            note = "" if rng.random() < 0.2 else f"note{rng.randint(0, 999)}"
            score = str(rng.randint(0, 1000))
            rows.append([ids[j], label, email, status, note, score])

        if i > 0:
            target_ids = id_pools["entity0"]
            columns = [*columns, "entity0_id"]
            for j in range(n_rows):
                if rng.random() < 0.9 and target_ids:
                    fk_value = rng.choice(target_ids)
                else:
                    fk_value = f"missing{rng.randint(0, 9999)}"
                rows[j].append(fk_value)

        tables.append((name, columns, rows))
    return tables


def _write_table(
    table_dir: Path, name: str, columns: list[str], rows: list[list[str]], *, as_jsonl: bool
) -> None:
    if as_jsonl:
        lines = [
            json.dumps(
                {col: (v or None) for col, v in zip(columns, row, strict=True)}, sort_keys=True
            )
            for row in rows
        ]
        (table_dir / f"{name}.jsonl").write_text("\n".join(lines) + "\n")
    else:
        lines = [",".join(columns)] + [",".join(row) for row in rows]
        (table_dir / f"{name}.csv").write_text("\n".join(lines) + "\n")


def _append_row(
    table_dir: Path, name: str, columns: list[str], row: list[str], *, as_jsonl: bool
) -> None:
    suffix = "jsonl" if as_jsonl else "csv"
    path = table_dir / f"{name}.{suffix}"
    with path.open("a", encoding="utf-8") as handle:
        if as_jsonl:
            obj = {col: (v or None) for col, v in zip(columns, row, strict=True)}
            handle.write(json.dumps(obj, sort_keys=True) + "\n")
        else:
            handle.write(",".join(row) + "\n")


def test_pipeline_byte_determinism(tmp_path: Path) -> None:
    for seed in range(5):
        corpus = _build_corpus(random.Random(seed))
        format_rng = random.Random(seed * 7919 + 1)
        as_jsonl_flags = [format_rng.random() < 0.5 for _ in corpus]

        dir_a = tmp_path / f"a{seed}"
        dir_b = tmp_path / f"b{seed}"
        dir_a.mkdir()
        dir_b.mkdir()

        # dir_a: written in forward table order.
        for (name, columns, rows), as_jsonl in zip(corpus, as_jsonl_flags, strict=True):
            _write_table(dir_a, name, columns, rows, as_jsonl=as_jsonl)

        # dir_b: IDENTICAL content, files created in REVERSED order.
        for (name, columns, rows), as_jsonl in zip(
            reversed(corpus), reversed(as_jsonl_flags), strict=True
        ):
            _write_table(dir_b, name, columns, rows, as_jsonl=as_jsonl)

        bundle_a1 = profile_samples(dir_a)
        bundle_a2 = profile_samples(dir_a)
        bundle_b1 = profile_samples(dir_b)
        bundle_b2 = profile_samples(dir_b)

        profile_jsons = {
            bundle_a1.profile.model_dump_json(),
            bundle_a2.profile.model_dump_json(),
            bundle_b1.profile.model_dump_json(),
            bundle_b2.profile.model_dump_json(),
        }
        assert len(profile_jsons) == 1, f"seed {seed}: profile JSON diverged across runs/dirs"

        model_jsons = {
            infer_candidate_model(bundle_a1).model_dump_json(),
            infer_candidate_model(bundle_a2).model_dump_json(),
            infer_candidate_model(bundle_b1).model_dump_json(),
            infer_candidate_model(bundle_b2).model_dump_json(),
        }
        assert len(model_jsons) == 1, f"seed {seed}: candidate-model JSON diverged across runs/dirs"

        # Sensitivity control: mutate dir_b (append one row to the first
        # table) and prove the profile JSON actually changes — otherwise the
        # equality assertions above could pass vacuously.
        first_name, first_columns, first_rows = corpus[0]
        mutated_row = list(first_rows[0])
        mutated_row[0] = f"{mutated_row[0]}-extra"
        _append_row(dir_b, first_name, first_columns, mutated_row, as_jsonl=as_jsonl_flags[0])

        mutated_profile_json = profile_samples(dir_b).profile.model_dump_json()
        assert mutated_profile_json != bundle_b1.profile.model_dump_json(), (
            f"seed {seed}: appending a row did not change the profile — comparison has no teeth"
        )


_SUBPROCESS_SCRIPT = """
import sys
from pathlib import Path
from ne.schema_bootstrap.infer import infer_candidate_model
from ne.schema_bootstrap.profiles import profile_samples

bundle = profile_samples(Path(sys.argv[1]))
model = infer_candidate_model(bundle)
sys.stdout.write(bundle.profile.model_dump_json())
sys.stdout.write("\\n")
sys.stdout.write(model.model_dump_json())
sys.stdout.write("\\n")
"""


def test_cross_process_hashseed_determinism(tmp_path: Path) -> None:
    """The existing 4-way in-process comparison (``test_pipeline_byte_determinism``)
    runs all four pipeline invocations under ONE Python process, hence one
    ``PYTHONHASHSEED`` — so any nondeterminism that comes from
    str-hash-dependent set/dict iteration order reaching the output (e.g. an
    unsorted ``set`` -> ``list`` conversion) is structurally INVISIBLE to it:
    every comparison would share the identical (accidental) ordering. This
    test is the only one that can catch that class of bug: it runs the
    pipeline in two separate subprocesses with PYTHONHASHSEED explicitly
    pinned to two different values and asserts the JSON outputs are
    byte-identical. Expected GREEN, because every set-derived collection in
    this package is sorted before it lands on a model (design invariant) —
    if this test goes RED, the fix is to sort the offending collection AT
    THE SOURCE, never to loosen this assertion.
    """
    corpus = _build_corpus(random.Random(0))
    sample_dir = tmp_path / "corpus"
    sample_dir.mkdir()
    for name, columns, rows in corpus:
        _write_table(sample_dir, name, columns, rows, as_jsonl=False)

    outputs: list[str] = []
    for hashseed in ("0", "1"):
        result = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS_SCRIPT, str(sample_dir)],
            env={**os.environ, "PYTHONHASHSEED": hashseed},
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.append(result.stdout)

    assert outputs[0] == outputs[1], (
        "profile/model JSON diverged across PYTHONHASHSEED=0 vs PYTHONHASHSEED=1 "
        "— a set/dict-derived collection is reaching output unsorted"
    )
    # Non-vacuity control: the subprocess actually produced non-trivial output
    # (a profile line and a candidate-model line, the latter containing the
    # inferred classes), not an empty/crashed run that trivially compares equal.
    profile_line, model_line = outputs[0].splitlines()[:2]
    assert '"tables":[' in profile_line
    assert '"classes":[' in model_line
