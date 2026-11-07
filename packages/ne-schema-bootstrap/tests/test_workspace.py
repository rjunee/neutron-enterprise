# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end + atomicity + refine-invariant tests for
:mod:`ne.schema_bootstrap.workspace` — the validate-BEFORE-write ordering,
the ``.gitignore`` guard-first decision, and the ``--refine`` merge loop.

Several tests genuinely subprocess the REAL isolated codegen venv (the same
``_three_table_fixture`` pattern as ``tests/test_roundtrip.py`` — copied
locally per this unit's brief rather than imported, so this module has no
test-to-test coupling on ``test_roundtrip.py``'s own fixture evolving).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

import ne.schema_bootstrap.workspace as workspace_mod
from ne.schema_bootstrap.errors import (
    DraftGenerationError,
    EmptySampleError,
    InvalidEngagementNameError,
    RefineError,
    WorkspaceExistsError,
    WorkspaceIOError,
)
from ne.schema_bootstrap.roundtrip import validate_draft_generates as _real_validate_draft_generates
from ne.schema_bootstrap.workspace import run_bootstrap

# tests/ -> ne-schema-bootstrap -> packages -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]

_ENGAGEMENT = "acme-co"


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(header)] + [",".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n")


def _three_table_fixture(tmp_path: Path) -> None:
    """customers + orders (customer_id FK >= 95%) + a 2-FK junction table +
    a status enum + an email-pattern column — copied locally from
    ``tests/test_roundtrip.py::_three_table_fixture`` (see module docstring).
    """
    n_customers = 30
    _write_csv(
        tmp_path / "customers.csv",
        ["id", "name", "email", "status"],
        [
            [
                f"c{i}",
                f"Cust {i}",
                f"user{i}@example.com",
                "active" if i % 2 == 0 else "churned",
            ]
            for i in range(n_customers)
        ],
    )
    n_orders = 20
    _write_csv(
        tmp_path / "orders.csv",
        ["id", "customer_id", "status", "amount"],
        [
            [
                f"o{i}",
                f"c{i % n_customers}",
                "open" if i % 2 == 0 else "closed",
                str(10 + i),
            ]
            for i in range(n_orders)
        ],
    )
    n_reviews = 20
    _write_csv(
        tmp_path / "order_reviews.csv",
        ["id", "order_id", "customer_id"],
        [[f"r{i}", f"o{i % n_orders}", f"c{i % n_customers}"] for i in range(n_reviews)],
    )


def _run_artifact_paths(workspace_dir: Path) -> dict[str, Path]:
    return {name: workspace_dir / name for name in workspace_mod._RUN_ARTIFACTS}


def _workspace_snapshot(workspace_dir: Path) -> dict[str, bytes]:
    """Every file under ``workspace_dir``, by relative path, as raw bytes —
    the shared byte-identical-workspace check reused by the round-5
    filesystem-sweep tests (same shape as the pre-existing inline
    ``_snapshot`` closures in ``test_refine_failure_leaves_workspace_byte_identical``
    and ``test_refine_reserved_name_collision_one_probed_shape_fails_gate_and_writes_nothing``).
    """
    return {
        str(p.relative_to(workspace_dir)): p.read_bytes()
        for p in sorted(workspace_dir.rglob("*"))
        if p.is_file()
    }


def _report_section(report_text: str, heading: str) -> str:
    """Slice ``report_text`` from ``heading`` (a full ``### Class `Name` ``/
    ``### Enum `Name` `` heading line) up to (not including) the NEXT
    ``#``-prefixed heading line, so a landing-status assertion is scoped to
    the ONE section it names rather than the whole report.
    """
    start = report_text.index(heading)
    rest = report_text[start + len(heading) :]
    match = re.search(r"(?m)^#", rest)
    end = start + len(heading) + (match.start() if match else len(rest))
    return report_text[start:end]


def test_fresh_run_writes_all_artifacts_and_gitignore(tmp_path: Path) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    result = run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    assert result.refined is False
    assert result.merge is None
    assert result.files_written == sorted(workspace_mod._RUN_ARTIFACTS)
    # Non-vacuity: this fixture is built to have at least one FK edge and one
    # emitted enum — assert both, mirroring test_roundtrip.py's own guard.
    assert result.edge_count >= 1
    assert result.enum_count >= 1
    assert result.class_count == 3
    assert result.table_count == 3

    paths = _run_artifact_paths(workspace_dir)
    machine_text = paths[workspace_mod.MACHINE_DRAFT].read_text(encoding="utf-8")
    draft_text = paths[workspace_mod.DRAFT].read_text(encoding="utf-8")
    assert machine_text == draft_text

    report_text = paths[workspace_mod.REPORT].read_text(encoding="utf-8")
    pr_body_text = paths[workspace_mod.PR_BODY].read_text(encoding="utf-8")
    assert report_text.strip() != ""
    assert pr_body_text.strip() != ""
    assert "round-trip: VERIFIED" in pr_body_text

    tombstones = json.loads(paths[workspace_mod.TOMBSTONES].read_text(encoding="utf-8"))
    assert tombstones == {"classes": [], "enums": []}

    gitignore_path = workspace_dir / ".gitignore"
    assert gitignore_path.read_text(encoding="utf-8") == "samples/\n.bootstrap-staging/\n"

    # Non-vacuity: a fresh run's landing draft never orphans a class —
    # `emit_linkml` wires every class into the tree-root container
    # unconditionally (see `BootstrapRunResult.orphaned_classes`'s
    # docstring).
    assert result.orphaned_classes == []


def test_generation_failure_leaves_no_partial_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_dir = tmp_path / "workspace"
    samples_dir = workspace_dir / "samples"
    samples_dir.mkdir(parents=True)
    _three_table_fixture(samples_dir)

    def _forced_failure(*args: object, **kwargs: object) -> None:
        raise DraftGenerationError(
            1, "forced failure for test_generation_failure_leaves_no_partial_artifacts"
        )

    monkeypatch.setattr(workspace_mod, "validate_draft_generates", _forced_failure)

    with pytest.raises(DraftGenerationError):
        run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    # Guard-first: the workspace + its samples-excluding .gitignore exist...
    assert sorted(p.name for p in workspace_dir.iterdir()) == [".gitignore", "samples"]
    assert (workspace_dir / ".gitignore").read_text(
        encoding="utf-8"
    ) == "samples/\n.bootstrap-staging/\n"
    # ...but NONE of the five run artifacts do.
    for name in workspace_mod._RUN_ARTIFACTS:
        assert not (workspace_dir / name).exists(), (
            f"{name} should not exist after a forced failure"
        )

    monkeypatch.undo()

    # Control: the SAME call, unforced, succeeds and produces every artifact.
    result = run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)
    assert result.refined is False
    for name in workspace_mod._RUN_ARTIFACTS:
        assert (workspace_dir / name).is_file()


def test_fresh_run_errors_write_nothing_when_workspace_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    samples_dir = tmp_path / "external_samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "brand_new_workspace"
    assert not workspace_dir.exists()

    def _forced_failure(*args: object, **kwargs: object) -> None:
        raise DraftGenerationError(1, "forced failure")

    monkeypatch.setattr(workspace_mod, "validate_draft_generates", _forced_failure)

    with pytest.raises(DraftGenerationError):
        run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    assert workspace_dir.is_dir()
    assert sorted(p.name for p in workspace_dir.iterdir()) == [".gitignore"]
    for name in workspace_mod._RUN_ARTIFACTS:
        assert not (workspace_dir / name).exists()


def test_workspace_exists_error_without_refine(tmp_path: Path) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / workspace_mod.DRAFT).write_text("id: pre-existing\n", encoding="utf-8")

    with pytest.raises(WorkspaceExistsError) as exc_info:
        run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    assert str(workspace_dir) in str(exc_info.value)
    assert "--refine" in str(exc_info.value)


def test_refine_missing_state_file_raises_refine_error_naming_path(tmp_path: Path) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    # Nothing exists yet -> the FIRST required file (MACHINE_DRAFT) is named.
    with pytest.raises(RefineError) as exc_info:
        run_bootstrap(
            samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
        )
    assert workspace_mod.MACHINE_DRAFT in str(exc_info.value)

    # MACHINE_DRAFT + DRAFT exist, TOMBSTONES does not -> that path is named.
    (workspace_dir / workspace_mod.MACHINE_DRAFT).write_text("classes: {}\n", encoding="utf-8")
    (workspace_dir / workspace_mod.DRAFT).write_text("classes: {}\n", encoding="utf-8")
    with pytest.raises(RefineError) as exc_info2:
        run_bootstrap(
            samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
        )
    assert workspace_mod.TOMBSTONES in str(exc_info2.value)
    assert str(workspace_dir / workspace_mod.TOMBSTONES) in str(exc_info2.value)


def test_refine_failure_leaves_workspace_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    def _snapshot() -> dict[str, bytes]:
        return {
            str(p.relative_to(workspace_dir)): p.read_bytes()
            for p in sorted(workspace_dir.rglob("*"))
            if p.is_file()
        }

    before = _snapshot()

    call_count = {"n": 0}

    def _fail_on_second_call(*args: Any, **kwargs: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise DraftGenerationError(1, "forced failure on the merged-draft validation")
        return _real_validate_draft_generates(*args, **kwargs)

    monkeypatch.setattr(workspace_mod, "validate_draft_generates", _fail_on_second_call)

    with pytest.raises(DraftGenerationError):
        run_bootstrap(
            samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
        )

    assert call_count["n"] == 2  # both the machine-draft AND merged-draft calls ran
    after = _snapshot()
    assert after == before


def test_refine_human_edit_survives(tmp_path: Path) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    draft_path = workspace_dir / workspace_mod.DRAFT
    data = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    data["classes"]["CustomersRecord"]["description"] = "HUMAN EDITED DESCRIPTION MARKER"
    draft_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    # 2026-07-29 round 4, Argus P3-2: a STALE `.gitignore` (as if written by
    # an older revision, missing the staging-dir entry) — the refine path
    # must REPAIR it, not just leave it stale.
    gitignore_path = workspace_dir / ".gitignore"
    gitignore_path.write_text("samples/\n", encoding="utf-8")

    result = run_bootstrap(
        samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
    )

    assert result.refined is True
    assert result.merge is not None
    assert "classes.CustomersRecord" in result.merge.human_kept

    merged = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    assert merged["classes"]["CustomersRecord"]["description"] == "HUMAN EDITED DESCRIPTION MARKER"

    # Control: an untouched class (OrdersRecord) still reflects the machine
    # run, i.e. its mechanical description, not a stale human edit.
    assert "classes.OrdersRecord" not in result.merge.human_kept

    gitignore_lines = gitignore_path.read_text(encoding="utf-8").splitlines()
    assert "samples/" in gitignore_lines
    assert ".bootstrap-staging/" in gitignore_lines


def test_ensure_gitignore_repairs_stale_and_preserves_human_lines(tmp_path: Path) -> None:
    # 2026-07-29 round 4, Argus P3-2: `_ensure_gitignore` is CONTENT-AWARE,
    # not just absent-vs-present.
    absent_dir = tmp_path / "absent"
    workspace_mod._ensure_gitignore(absent_dir)
    assert (absent_dir / ".gitignore").read_text(encoding="utf-8") == workspace_mod.GITIGNORE

    # Stale (missing the staging-dir entry, human-added lines present) ->
    # exactly the missing entry is APPENDED; every existing line, including
    # the human ones, survives verbatim and in order.
    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    stale_text = "samples/\n# human comment\ncustom-entry/\n"
    (stale_dir / ".gitignore").write_text(stale_text, encoding="utf-8")
    workspace_mod._ensure_gitignore(stale_dir)
    repaired = (stale_dir / ".gitignore").read_text(encoding="utf-8")
    assert repaired == stale_text + ".bootstrap-staging/\n"

    # Already-correct -> byte-identical (no write, no reordering).
    correct_dir = tmp_path / "correct"
    correct_dir.mkdir()
    (correct_dir / ".gitignore").write_text(workspace_mod.GITIGNORE, encoding="utf-8")
    workspace_mod._ensure_gitignore(correct_dir)
    assert (correct_dir / ".gitignore").read_text(encoding="utf-8") == workspace_mod.GITIGNORE

    # No trailing newline -> repaired without gluing lines together.
    no_newline_dir = tmp_path / "no_newline"
    no_newline_dir.mkdir()
    (no_newline_dir / ".gitignore").write_text("samples/", encoding="utf-8")
    workspace_mod._ensure_gitignore(no_newline_dir)
    result_text = (no_newline_dir / ".gitignore").read_text(encoding="utf-8")
    assert "samples/\n.bootstrap-staging/\n" in result_text


def test_refine_reserved_name_collision_one_probed_shape_fails_gate_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """Locks Argus's live probe behind README "Residual #1": a human
    hand-renames a class key to the literal reserved container name
    ``OperationalEntity`` and repoints the tree-root container's attribute
    range at it. This proves the round-3 commit's phrasing ("a hand-edited
    collision would fail the round-trip gate and write nothing") for this
    ONE probed shape only — it does NOT prove the universal claim (every
    hand-edited collision fails the gate), which remains unproven and is not
    asserted here or anywhere else in this unit; see the README's residual
    #1 section, which this test's name and docstring deliberately mirror.
    """
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    # CONTROL FIRST: an unedited refine succeeds.
    control_result = run_bootstrap(
        samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
    )
    assert control_result.refined is True

    draft_path = workspace_dir / workspace_mod.DRAFT
    data = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    record_set_name = next(
        name for name, entry in data["classes"].items() if entry.get("tree_root") is True
    )
    container_attrs = data["classes"][record_set_name]["attributes"]
    customers_attr_key = next(
        key for key, attr in container_attrs.items() if attr.get("range") == "CustomersRecord"
    )
    data["classes"]["OperationalEntity"] = data["classes"].pop("CustomersRecord")
    container_attrs[customers_attr_key]["range"] = "OperationalEntity"
    draft_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    def _snapshot() -> dict[str, bytes]:
        return {
            str(p.relative_to(workspace_dir)): p.read_bytes()
            for p in sorted(workspace_dir.rglob("*"))
            if p.is_file()
        }

    before = _snapshot()

    with pytest.raises(DraftGenerationError):
        run_bootstrap(
            samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
        )

    after = _snapshot()
    assert after == before


def test_fresh_run_never_orphans_classes(tmp_path: Path) -> None:
    # Dedicated citation target for `BootstrapRunResult.orphaned_classes`'s
    # docstring claim ("always [] on a fresh run") — checked directly
    # against the written machine draft, not just the in-memory result.
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    result = run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    assert result.orphaned_classes == []
    from ne.schema_bootstrap.refine import orphaned_classes, parse_merged_yaml

    written = parse_merged_yaml(
        (workspace_dir / workspace_mod.MACHINE_DRAFT).read_text(encoding="utf-8")
    )
    top = next(name for name, entry in written["classes"].items() if entry.get("tree_root") is True)
    assert orphaned_classes(written, top) == []


def test_refine_container_freeze_orphans_a_later_machine_class(tmp_path: Path) -> None:
    # 2026-07-29 Argus P1 reproduction: editing the tree-root container
    # (here, deleting a rejected class's attribute — the same edit
    # `test_refine_deleted_class_stays_deleted_across_two_refines` makes)
    # freezes the container under rule 4, so a class the machine adds on a
    # LATER refine is never wired into it.
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    draft_path = workspace_dir / workspace_mod.DRAFT
    data = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    del data["classes"]["OrderReviewsRecord"]
    record_set_name = next(
        name for name, entry in data["classes"].items() if entry.get("tree_root") is True
    )
    del data["classes"][record_set_name]["attributes"]["order_reviews"]
    draft_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    run_bootstrap(
        samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
    )

    # A brand-new table appears on the NEXT refine — the machine proposes a
    # new class, but the container is already human-frozen.
    _write_csv(
        samples_dir / "new_widgets.csv",
        ["id", "name"],
        [[f"w{i}", f"Widget {i}"] for i in range(5)],
    )
    result = run_bootstrap(
        samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
    )

    assert "NewWidgetsRecord" in result.orphaned_classes
    merged = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    assert "NewWidgetsRecord" in merged["classes"]
    assert "new_widgets" not in merged["classes"][record_set_name]["attributes"]

    # Control: the pre-existing, still-wired classes are never reported as
    # orphaned.
    assert "CustomersRecord" not in result.orphaned_classes
    assert "OrdersRecord" not in result.orphaned_classes


def test_refine_pr_body_reflects_landing_schema_not_stale_machine_draft(tmp_path: Path) -> None:
    # 2026-07-29 Argus P1 reproduction: a human deletes a class whose sample
    # file is still present, so the machine proposes it again every refine.
    # pr-body.md must show the LANDING (merged) draft, which excludes it —
    # never the stale machine draft, which would still include it.
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    draft_path = workspace_dir / workspace_mod.DRAFT
    data = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    del data["classes"]["OrderReviewsRecord"]
    record_set_name = next(
        name for name, entry in data["classes"].items() if entry.get("tree_root") is True
    )
    del data["classes"][record_set_name]["attributes"]["order_reviews"]
    draft_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    result = run_bootstrap(
        samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
    )
    assert result.merge is not None
    assert "OrderReviewsRecord" in result.merge.tombstoned_classes

    pr_body_text = (workspace_dir / workspace_mod.PR_BODY).read_text(encoding="utf-8")

    # The fenced "## schema-draft.yaml" block (everything between that
    # heading and the NEXT "## " heading) must NOT contain the tombstoned
    # class name — it is genuinely absent from the landing draft embedded
    # there.
    schema_start = pr_body_text.index("## schema-draft.yaml")
    schema_end = pr_body_text.index("\n## ", schema_start + 1)
    schema_block = pr_body_text[schema_start:schema_end]
    assert "OrderReviewsRecord" not in schema_block

    # It DOES appear, explicitly labeled as removed, in the merge summary —
    # so a reviewer sees the deletion rather than silence.
    assert "## Refine merge summary" in pr_body_text
    summary_start = pr_body_text.index("## Refine merge summary")
    summary_block = pr_body_text[summary_start:]
    assert "OrderReviewsRecord" in summary_block
    assert "tombstoned" in summary_block

    # The embedded draft matches what actually landed on disk.
    landing_text = draft_path.read_text(encoding="utf-8")
    assert landing_text.strip() in pr_body_text

    # round-trip line still honestly claims VERIFIED — both the machine and
    # merged drafts were validated in this same call.
    assert "round-trip: VERIFIED" in pr_body_text


def test_refine_report_reflects_landing_draft_not_fresh_machine_draft(tmp_path: Path) -> None:
    # 2026-07-29 round 4, Argus P2 live repro, closed: a human tombstones
    # OrderReviewsRecord whose sample is still present (the machine
    # re-proposes it every run). Before the fix, inference-report.md
    # described THIS RUN'S fresh machine draft with no indication it had
    # diverged from the landing draft, which correctly omits the tombstoned
    # class — the report told a reviewer the deletion had not taken.
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    fresh_report = (workspace_dir / workspace_mod.REPORT).read_text(encoding="utf-8")
    assert "What landed" not in fresh_report
    assert "landing status" not in fresh_report

    draft_path = workspace_dir / workspace_mod.DRAFT
    data = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    del data["classes"]["OrderReviewsRecord"]
    record_set_name = next(
        name for name, entry in data["classes"].items() if entry.get("tree_root") is True
    )
    del data["classes"][record_set_name]["attributes"]["order_reviews"]
    data["classes"]["CustomersRecord"]["description"] = "HUMAN EDITED DESCRIPTION MARKER"
    draft_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    run_bootstrap(
        samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
    )

    report_text = (workspace_dir / workspace_mod.REPORT).read_text(encoding="utf-8")
    assert "## What landed (--refine)" in report_text

    # The machine still evidences OrderReviewsRecord this run (its sample
    # file is unchanged) — the section STAYS, but is labeled as not landed.
    reviews_section = _report_section(report_text, "### Class `OrderReviewsRecord`")
    assert "- landing status: did NOT land — tombstoned" in reviews_section

    customers_section = _report_section(report_text, "### Class `CustomersRecord`")
    assert "landed as the HUMAN'S version" in customers_section

    orders_section = _report_section(report_text, "### Class `OrdersRecord`")
    assert "landed (machine version)" in orders_section

    what_landed_start = report_text.index("## What landed (--refine)")
    what_landed_end = report_text.index("\n## ", what_landed_start + 1)
    what_landed_block = report_text[what_landed_start:what_landed_end]
    landing_line = next(
        ln for ln in what_landed_block.splitlines() if ln.startswith("- landing classes")
    )
    tombstoned_line = next(
        ln for ln in what_landed_block.splitlines() if ln.startswith("- tombstoned classes:")
    )
    assert '"OrderReviewsRecord"' in tombstoned_line
    assert '"OrderReviewsRecord"' not in landing_line
    assert '"OrdersRecord"' in landing_line

    # Artifact consistency: pr-body.md's existing merge summary still lists
    # OrderReviewsRecord (same tombstoning, described from its own artifact).
    pr_body_text = (workspace_dir / workspace_mod.PR_BODY).read_text(encoding="utf-8")
    assert "OrderReviewsRecord" in pr_body_text

    # 2026-07-29 round 5, P3 #2: the "- landing classes (N)" bullet must
    # state its non-abstract exclusion — see
    # tests/test_refine.py::test_merged_expected_names_excludes_abstract for
    # the exclusion at the source. Proven with a DISCRIMINATING assertion,
    # not just a substring match on the clause: OperationalEntity IS a key
    # of the landed draft's `classes:` mapping (the count-vs-list
    # discrepancy the P3 named) yet is absent from this bullet's quoted
    # list, and the bullet's own text explains why.
    assert "non-abstract only" in landing_line
    landed_classes = yaml.safe_load(draft_path.read_text(encoding="utf-8"))["classes"]
    assert "OperationalEntity" in landed_classes
    assert '"OperationalEntity"' not in landing_line


def test_refine_malformed_tombstones_json_raises_refine_error(tmp_path: Path) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    tombstones_path = workspace_dir / workspace_mod.TOMBSTONES
    tombstones_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(RefineError, match=str(tombstones_path)):
        run_bootstrap(
            samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
        )

    # Control: a syntactically valid JSON document that is not an object at
    # the top level is also rejected, distinctly from a plain parse error.
    tombstones_path.write_text("[]", encoding="utf-8")
    with pytest.raises(RefineError, match="JSON object"):
        run_bootstrap(
            samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
        )

    # Control: a well-formed tombstones.json still works.
    tombstones_path.write_text('{"classes": [], "enums": []}', encoding="utf-8")
    result = run_bootstrap(
        samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
    )
    assert result.refined is True


def test_refine_summary_enums_bullet_describes_landing_draft(tmp_path: Path) -> None:
    # 2026-07-29 Argus P3 (round 2): `pr-body.md`'s `enums emitted` bullet
    # already describes the LANDING (merged) draft, unlike `classes`/
    # `slots`/`edges` which describe THIS RUN'S fresh machine draft. Prove
    # the discrepancy actually exists (a human-rejected enum makes the two
    # counts differ) and that the rendered text is consistent with that.
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    draft_path = workspace_dir / workspace_mod.DRAFT
    data = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    assert "OrdersRecordStatusEnum" in data["enums"]
    # A human rejecting an inferred enum edits BOTH the enum entry away AND
    # the class attribute that referenced it (otherwise the landing draft
    # would carry a dangling `range:` and fail its own round-trip gate).
    del data["enums"]["OrdersRecordStatusEnum"]
    data["classes"]["OrdersRecord"]["attributes"]["status"]["range"] = "string"
    draft_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    result = run_bootstrap(
        samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
    )
    assert result.merge is not None
    assert "OrdersRecordStatusEnum" in result.merge.tombstoned_enums

    # THIS RUN'S freshly re-profiled machine draft still proposes BOTH
    # enums (same samples) -> BootstrapRunResult.enum_count is 2.
    assert result.enum_count == 2

    # The LANDING (merged) draft has only 1 enum — the tombstoned one is
    # excluded.
    landing = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    assert list(landing["enums"].keys()) == ["CustomersRecordStatusEnum"]

    pr_body_text = (workspace_dir / workspace_mod.PR_BODY).read_text(encoding="utf-8")

    # The `enums emitted` metadata bullet already reflects the LANDING
    # count (1), not the fresh machine count (2) — the very fact the
    # addendum's own prose (added below) describes.
    assert "- enums emitted: 1" in pr_body_text
    assert "- enums emitted: 2" not in pr_body_text

    # Control: `classes` DOES describe the fresh machine run (3, unchanged
    # by this refine — the human edit only touched an attribute + an enum,
    # not the class roster).
    assert "- classes: 3" in pr_body_text

    summary_start = pr_body_text.index("## Refine merge summary")
    summary_block = pr_body_text[summary_start:]
    assert "enums=1" in summary_block
    assert "enums emitted` above" in summary_block

    # 2026-07-29 round 4, Argus P2 fix: inference-report.md's enum sections
    # carry the same landing split — the tombstoned enum's section says it
    # did NOT land, the surviving enum's says it landed (machine version).
    report_text = (workspace_dir / workspace_mod.REPORT).read_text(encoding="utf-8")
    tombstoned_enum_section = _report_section(report_text, "### Enum `OrdersRecordStatusEnum`")
    assert "did NOT land — tombstoned" in tombstoned_enum_section

    landed_enum_section = _report_section(report_text, "### Enum `CustomersRecordStatusEnum`")
    assert "landed (machine version)" in landed_enum_section


def test_missing_samples_directory_raises_empty_sample_error(tmp_path: Path) -> None:
    # 2026-07-29 Argus P2 live repro: `ne schema bootstrap --engagement
    # acme-co` with no samples directory yet (the CLI's OWN advertised
    # default, `<workspace>/samples`) previously exited with a bare
    # `FileNotFoundError` traceback rather than a `SchemaBootstrapError`.
    workspace_dir = tmp_path / "workspace"
    samples_dir = tmp_path / "does_not_exist"
    assert not samples_dir.exists()

    with pytest.raises(EmptySampleError) as exc_info:
        run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)
    assert str(samples_dir) in str(exc_info.value)

    # Guard-first still applies even for this failure mode: the workspace
    # dir + its .gitignore exist, no run artifact does.
    assert workspace_dir.is_dir()
    assert sorted(p.name for p in workspace_dir.iterdir()) == [".gitignore"]

    # Control: a samples directory that EXISTS but is empty raises the SAME
    # error type, naming the same path shape (existing EmptySampleError
    # behavior, unaffected by this fix).
    workspace_dir2 = tmp_path / "workspace2"
    empty_samples_dir = tmp_path / "empty_samples"
    empty_samples_dir.mkdir()
    with pytest.raises(EmptySampleError) as exc_info2:
        run_bootstrap(
            empty_samples_dir, workspace_dir2, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT
        )
    assert str(empty_samples_dir) in str(exc_info2.value)

    # Control (refine path): a missing samples directory on `--refine` is
    # also caught, after the guard-first `.gitignore` (re)write.
    workspace_dir3 = tmp_path / "workspace3"
    samples_dir3 = tmp_path / "samples3"
    samples_dir3.mkdir()
    _three_table_fixture(samples_dir3)
    run_bootstrap(samples_dir3, workspace_dir3, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)
    missing_samples_dir3 = tmp_path / "moved_away_samples3"
    with pytest.raises(EmptySampleError) as exc_info3:
        run_bootstrap(
            missing_samples_dir3,
            workspace_dir3,
            engagement=_ENGAGEMENT,
            repo_root=_REPO_ROOT,
            refine=True,
        )
    assert str(missing_samples_dir3) in str(exc_info3.value)


def test_refine_deleted_class_stays_deleted_across_two_refines(tmp_path: Path) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    draft_path = workspace_dir / workspace_mod.DRAFT
    data = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    del data["classes"]["OrderReviewsRecord"]
    record_set_name = next(
        name for name, entry in data["classes"].items() if entry.get("tree_root") is True
    )
    del data["classes"][record_set_name]["attributes"]["order_reviews"]
    draft_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    result1 = run_bootstrap(
        samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
    )
    assert result1.merge is not None
    assert "OrderReviewsRecord" in result1.merge.tombstoned_classes

    merged1 = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    assert "OrderReviewsRecord" not in merged1["classes"]

    tombstones1 = json.loads((workspace_dir / workspace_mod.TOMBSTONES).read_text(encoding="utf-8"))
    assert "OrderReviewsRecord" in tombstones1["classes"]

    # A second refine over the SAME (unchanged) samples: the machine proposes
    # OrderReviewsRecord again, but the tombstone must block the resurrection.
    result2 = run_bootstrap(
        samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
    )
    assert result2.merge is not None
    assert "classes.OrderReviewsRecord" in result2.merge.blocked_resurrections
    assert "OrderReviewsRecord" in result2.merge.tombstoned_classes

    merged2 = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    assert "OrderReviewsRecord" not in merged2["classes"]

    tombstones2 = json.loads((workspace_dir / workspace_mod.TOMBSTONES).read_text(encoding="utf-8"))
    assert "OrderReviewsRecord" in tombstones2["classes"]


def test_refine_non_string_class_key_raises_refine_error_and_writes_nothing(
    tmp_path: Path,
) -> None:
    # 2026-07-29 Argus P2, round 5, live repro at the CLI layer: a human
    # hand-adds an UNQUOTED numeric class key to schema-draft.yaml (the
    # exact Argus shape — keys become
    # `['OperationalEntity', 'CustomersRecord', ..., 2024]`), then
    # `--refine` -> RefineError, not a raw TypeError, and the workspace is
    # left byte-identical (the failure precedes any write, same proof shape
    # as ``test_refine_failure_leaves_workspace_byte_identical``).
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    draft_path = workspace_dir / workspace_mod.DRAFT
    data = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    data["classes"][2024] = {"description": "hand-added numeric-keyed class"}
    draft_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    # Non-vacuity: confirm the fixture actually produced the unquoted shape,
    # not a quoted "2024" that would never trigger the guard.
    assert "2024:" in draft_path.read_text(encoding="utf-8")

    before = _workspace_snapshot(workspace_dir)

    with pytest.raises(RefineError, match="2024"):
        run_bootstrap(
            samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
        )

    assert _workspace_snapshot(workspace_dir) == before


def test_clear_staging_refuses_subdirectory_with_typed_error(tmp_path: Path) -> None:
    # Argus finding 4, round 5: `.bootstrap-staging/` is a FIXED,
    # operator-visible path this module's recovery path (`_clear_staging`)
    # runs on — a subdirectory left there by anything else must be REFUSED,
    # never recursively deleted.
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    staging = workspace_dir / workspace_mod._STAGING_DIRNAME
    nested = staging / "nested"
    nested.mkdir(parents=True)

    with pytest.raises(WorkspaceIOError) as exc_info:
        run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    assert str(nested) in str(exc_info.value)
    assert nested.is_dir()  # refused, NOT deleted
    # Discriminating (not just "some WorkspaceIOError fired"):
    # `leftover.unlink()` on a directory ALSO raises an OSError
    # (`IsADirectoryError`) that the generic per-entry `except OSError`
    # wrap below would independently convert to a WorkspaceIOError with a
    # GENERIC "cannot remove staged file" message — so asserting only
    # `WorkspaceIOError` fired would pass even if the explicit
    # `is_dir()`/`is_symlink()` refusal check were deleted entirely
    # ([[vacuous-guard-assertion-shapes]]). The message must specifically
    # name the REFUSAL reason, proving the dedicated check (not the
    # fallback OSError wrap) is what fired.
    assert "subdirectory this tool never creates" in str(exc_info.value)

    # Control (non-vacuity, [[vacuous-guard-assertion-shapes]]): the
    # crash-recovery PROPERTY this function exists for is preserved — a
    # PLAIN FILE leftover (the ordinary shape after a crashed commit) is
    # still silently cleared and the run succeeds.
    nested.rmdir()
    (staging / "leftover.txt").write_text("stale from a crashed commit", encoding="utf-8")
    result = run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)
    assert result.refined is False
    assert not staging.exists()


def test_staging_name_occupied_by_file_raises_typed_error(tmp_path: Path) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    staging = workspace_dir / workspace_mod._STAGING_DIRNAME
    staging.write_text("not a directory", encoding="utf-8")

    with pytest.raises(WorkspaceIOError) as exc_info:
        run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    assert str(staging) in str(exc_info.value)
    assert staging.is_file()  # left in place, not clobbered


def test_workspace_path_is_file_raises_typed_error(tmp_path: Path) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace_as_file"
    workspace_dir.write_text("not a directory", encoding="utf-8")

    with pytest.raises(WorkspaceIOError) as exc_info:
        run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    assert str(workspace_dir) in str(exc_info.value)
    assert workspace_dir.is_file()  # left in place, not clobbered by mkdir


def test_gitignore_directory_raises_typed_error(tmp_path: Path) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    gitignore_path = workspace_dir / ".gitignore"
    gitignore_path.mkdir()

    with pytest.raises(WorkspaceIOError) as exc_info:
        run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    assert str(gitignore_path) in str(exc_info.value)
    assert gitignore_path.is_dir()  # left in place


@pytest.mark.parametrize(
    "state_file_name",
    [workspace_mod.MACHINE_DRAFT, workspace_mod.DRAFT, workspace_mod.TOMBSTONES],
)
def test_state_file_not_utf8_raises_refine_error_naming_path(
    tmp_path: Path, state_file_name: str
) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    target_path = workspace_dir / state_file_name
    sentinel = b"sentinel-ok \xff\xfe garbage"
    target_path.write_bytes(sentinel)

    with pytest.raises(RefineError) as exc_info:
        run_bootstrap(
            samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT, refine=True
        )
    message = str(exc_info.value)
    assert str(target_path) in message
    assert "byte position" in message
    # Disclosure guard: the planted ASCII-readable sentinel text must NEVER
    # appear in the error message, even though everything before the bad
    # byte decodes as ordinary ASCII — proves `_io_detail` never touches
    # `UnicodeDecodeError.object`.
    assert "sentinel-ok" not in message


def test_commit_destination_directory_raises_typed_error(tmp_path: Path) -> None:
    # A DIRECTORY squatting a run artifact's landing name (e.g. an operator
    # accidentally `mkdir`-ing `inference-report.md`) makes
    # `staged_path.replace(dest)` raise `IsADirectoryError` — a fresh path,
    # so `schema-draft.yaml`'s own exists-check still passes.
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    report_path = workspace_dir / workspace_mod.REPORT
    report_path.mkdir()

    with pytest.raises(WorkspaceIOError) as exc_info:
        run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)

    assert str(report_path) in str(exc_info.value)
    assert report_path.is_dir()  # left in place, not clobbered


def test_engagement_validated_before_any_workspace_write(tmp_path: Path) -> None:
    # 2026-07-29 round 5: `_validate_engagement` now runs as the FIRST
    # statement of `run_bootstrap` — an invalid slug must raise
    # `InvalidEngagementNameError` with ZERO filesystem side effects, not
    # after `_ensure_gitignore` has already created the workspace directory
    # and written `.gitignore` (the side-effect-before-validation gap this
    # round closes for engagement validation specifically).
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _three_table_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"
    assert not workspace_dir.exists()

    with pytest.raises(InvalidEngagementNameError):
        run_bootstrap(samples_dir, workspace_dir, engagement="Bad_Slug", repo_root=_REPO_ROOT)

    assert not workspace_dir.exists()

    # Control (non-vacuity): a VALID slug against the SAME nonexistent
    # workspace path does create it — the assertion above is discriminating,
    # not vacuously true because `run_bootstrap` never creates anything.
    result = run_bootstrap(samples_dir, workspace_dir, engagement=_ENGAGEMENT, repo_root=_REPO_ROOT)
    assert workspace_dir.is_dir()
    assert result.refined is False
