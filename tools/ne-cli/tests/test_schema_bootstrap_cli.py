# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for `ne schema bootstrap` (NE-ESB-CLI, U3).

Several tests genuinely subprocess the REAL isolated codegen venv (through
`ne.schema_bootstrap.run_bootstrap` -> `validate_draft_generates`) — this is
the actual, delivery-facing CLI surface, so a mocked round-trip would prove
nothing about whether the advertised command works.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import ne.schema_bootstrap as sb
from ne.cli import main

_ENGAGEMENT = "acme-co"

#: A sentinel customer value that must never leak into CLI output or any
#: written artifact — planted in an `ssn`-named (and SSN-shaped) column, both
#: the column-NAME PII heuristic and the value-SHAPE heuristic flag it.
_SENTINEL_SSN = "123-45-6789"


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(header)] + [",".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n")


def _pii_fixture(samples_dir: Path) -> None:
    n = 20
    _write_csv(
        samples_dir / "customers.csv",
        ["id", "name", "ssn", "status"],
        [
            [
                f"c{i}",
                f"Cust {i}",
                _SENTINEL_SSN if i == 0 else f"123-45-{6790 + i:04d}",
                "active" if i % 2 == 0 else "churned",
            ]
            for i in range(n)
        ],
    )


def test_bootstrap_end_to_end_real(tmp_path: Path) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _pii_fixture(samples_dir)
    workspace_dir = tmp_path / "workspace"

    result = CliRunner().invoke(
        main,
        [
            "schema",
            "bootstrap",
            "--engagement",
            _ENGAGEMENT,
            "--workspace",
            str(workspace_dir),
            "--samples",
            str(samples_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert _SENTINEL_SSN not in result.output

    written = list(workspace_dir.glob("*"))
    assert written, "expected the workspace to contain written artifacts"
    for path in written:
        if path.is_file():
            assert _SENTINEL_SSN.encode() not in path.read_bytes(), (
                f"sentinel leaked into {path.name}"
            )

    # Non-vacuity control: the report still names the (mechanical, not
    # value-bearing) column header "ssn" — suppression is of the VALUE, not
    # the column's existence.
    report_text = (workspace_dir / "inference-report.md").read_text(encoding="utf-8")
    assert "ssn" in report_text

    # STRONGER non-vacuity control (2026-07-29 Argus P3): a genuine
    # non-PII column's EXEMPLAR VALUE (not just a column name substring)
    # is present in the report, proving suppression operates at the value
    # level rather than the report simply never rendering exemplars at all.
    assert "active" in report_text


def test_bootstrap_refine_end_to_end_prints_merge_summary_and_tombstones(
    tmp_path: Path,
) -> None:
    """CLI-level coverage for `--refine` (2026-07-30, NE-ESB-CLI
    continuation, Argus P2).

    Every OTHER `--refine` exercise in this file passes `refine=True`
    straight through to a `_fake_run_bootstrap` stub that always returns
    `merge=None` — so `ne/cli/schema.py`'s `if result.merge is not None:`
    block (the merge-summary line and the tombstone lines, the operator-
    facing surface for this unit's headline feature) was never executed by
    any test in this suite. The library side already has real end-to-end
    `--refine` coverage in `tests/test_workspace.py`; this drives the SAME
    shape through the actual CLI entrypoint: a real fresh bootstrap, a
    hand-tombstoned class in the landed draft, then a real `--refine`
    re-invocation, asserting the terminal output a human operator actually
    sees.
    """
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    _write_csv(
        samples_dir / "customers.csv",
        ["id", "name", "status"],
        [[f"c{i}", f"Cust {i}", "active" if i % 2 == 0 else "churned"] for i in range(20)],
    )
    _write_csv(
        samples_dir / "products.csv",
        ["id", "title", "category"],
        [[f"p{i}", f"Product {i}", "widgets" if i % 2 == 0 else "gadgets"] for i in range(20)],
    )
    workspace_dir = tmp_path / "workspace"

    result = CliRunner().invoke(
        main,
        [
            "schema",
            "bootstrap",
            "--engagement",
            _ENGAGEMENT,
            "--workspace",
            str(workspace_dir),
            "--samples",
            str(samples_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "refined: False" in result.output
    # Non-vacuity control: on a FRESH run the labelled ("not landing count")
    # wording is absent — it only applies once `--refine` is in play.
    assert "not landing count" not in result.output

    draft_path = workspace_dir / "schema-draft.yaml"
    data = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    assert "ProductsRecord" in data["classes"], sorted(data["classes"])
    del data["classes"]["ProductsRecord"]
    record_set_name = next(
        name for name, entry in data["classes"].items() if entry.get("tree_root") is True
    )
    del data["classes"][record_set_name]["attributes"]["products"]
    draft_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    result2 = CliRunner().invoke(
        main,
        [
            "schema",
            "bootstrap",
            "--engagement",
            _ENGAGEMENT,
            "--workspace",
            str(workspace_dir),
            "--samples",
            str(samples_dir),
            "--refine",
        ],
    )
    assert result2.exit_code == 0, result2.output
    assert "refined: True" in result2.output
    assert "not landing count" in result2.output
    assert "merge: human_kept=" in result2.output
    assert "machine_updated=" in result2.output
    assert "blocked_resurrections=" in result2.output
    assert "human_readded=" in result2.output
    assert "tombstoned classes: ProductsRecord" in result2.output

    merged = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    assert "ProductsRecord" not in merged["classes"], sorted(merged["classes"])


def test_bootstrap_llm_fails_closed(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    samples_dir = tmp_path / "samples"

    result = CliRunner().invoke(
        main,
        [
            "schema",
            "bootstrap",
            "--engagement",
            _ENGAGEMENT,
            "--workspace",
            str(workspace_dir),
            "--samples",
            str(samples_dir),
            "--llm",
        ],
    )

    assert result.exit_code == 2
    assert "U5" in result.output
    assert "NE-ESB-LABEL" in result.output
    assert not workspace_dir.exists()


def test_bootstrap_surfaces_invalid_class_name_filename(tmp_path: Path) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    # A digit-leading stem mechanically derives an invalid Python identifier
    # class name ("2023Record" -> starts with a digit) — see
    # ne.schema_bootstrap.errors.InvalidClassNameError's docstring.
    _write_csv(samples_dir / "2023.csv", ["id", "name"], [["1", "a"], ["2", "b"], ["3", "c"]])
    workspace_dir = tmp_path / "workspace"

    result = CliRunner().invoke(
        main,
        [
            "schema",
            "bootstrap",
            "--engagement",
            _ENGAGEMENT,
            "--workspace",
            str(workspace_dir),
            "--samples",
            str(samples_dir),
        ],
    )

    assert result.exit_code == 1
    assert "2023" in result.output


def test_bootstrap_missing_samples_directory_names_path(tmp_path: Path) -> None:
    # 2026-07-29 Argus P2 live repro: `ne schema bootstrap --engagement
    # acme-co` with no samples directory yet — the exact shape the CLI's own
    # `--samples` help text advertises as the default (`<workspace>/samples`)
    # — previously exited with a bare Python traceback and EMPTY
    # operator-facing output, bypassing the CLI's reviewed error surface.
    workspace_dir = tmp_path / "workspace"
    samples_dir = tmp_path / "workspace" / "samples"  # never created
    assert not samples_dir.exists()

    result = CliRunner().invoke(
        main,
        [
            "schema",
            "bootstrap",
            "--engagement",
            _ENGAGEMENT,
            "--workspace",
            str(workspace_dir),
            "--samples",
            str(samples_dir),
        ],
    )

    assert result.exit_code == 1
    assert "error:" in result.output
    assert str(samples_dir) in result.output
    # Not a bare traceback: click's own `Exit(1)` never dumps a stack trace
    # to stdout/stderr the way an unhandled `FileNotFoundError` would.
    assert "Traceback" not in result.output

    # Control: an EXISTING but empty samples directory hits the SAME
    # reviewed error surface, not a different one.
    workspace_dir2 = tmp_path / "workspace2"
    empty_samples_dir = tmp_path / "empty_samples"
    empty_samples_dir.mkdir()
    result2 = CliRunner().invoke(
        main,
        [
            "schema",
            "bootstrap",
            "--engagement",
            _ENGAGEMENT,
            "--workspace",
            str(workspace_dir2),
            "--samples",
            str(empty_samples_dir),
        ],
    )
    assert result2.exit_code == 1
    assert "error:" in result2.output
    assert str(empty_samples_dir) in result2.output


def test_bootstrap_prints_orphaned_class_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fake_run_bootstrap(samples_dir, workspace_dir, *, engagement, repo_root, config, refine):
        return sb.BootstrapRunResult(
            workspace=str(workspace_dir),
            refined=refine,
            files_written=[],
            table_count=1,
            class_count=1,
            enum_count=0,
            edge_count=0,
            merge=None,
            orphaned_classes=["OrphanedRecord"],
        )

    monkeypatch.setattr(sb, "run_bootstrap", _fake_run_bootstrap)

    result = CliRunner().invoke(
        main,
        [
            "schema",
            "bootstrap",
            "--engagement",
            _ENGAGEMENT,
            "--workspace",
            str(tmp_path / "ws"),
            "--samples",
            str(tmp_path / "samples"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output
    assert "OrphanedRecord" in result.output

    # Control: no orphaned classes -> no warning line at all.
    def _fake_run_bootstrap_no_orphans(
        samples_dir, workspace_dir, *, engagement, repo_root, config, refine
    ):
        return sb.BootstrapRunResult(
            workspace=str(workspace_dir),
            refined=refine,
            files_written=[],
            table_count=1,
            class_count=1,
            enum_count=0,
            edge_count=0,
            merge=None,
            orphaned_classes=[],
        )

    monkeypatch.setattr(sb, "run_bootstrap", _fake_run_bootstrap_no_orphans)
    result2 = CliRunner().invoke(
        main,
        [
            "schema",
            "bootstrap",
            "--engagement",
            _ENGAGEMENT,
            "--workspace",
            str(tmp_path / "ws2"),
            "--samples",
            str(tmp_path / "samples2"),
        ],
    )
    assert result2.exit_code == 0, result2.output
    assert "WARNING" not in result2.output


def test_bootstrap_threshold_flags_reach_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def _fake_run_bootstrap(samples_dir, workspace_dir, *, engagement, repo_root, config, refine):
        captured["config"] = config
        return sb.BootstrapRunResult(
            workspace=str(workspace_dir),
            refined=refine,
            files_written=[],
            table_count=0,
            class_count=0,
            enum_count=0,
            edge_count=0,
            merge=None,
            orphaned_classes=[],
        )

    monkeypatch.setattr(sb, "run_bootstrap", _fake_run_bootstrap)

    result = CliRunner().invoke(
        main,
        [
            "schema",
            "bootstrap",
            "--engagement",
            _ENGAGEMENT,
            "--workspace",
            str(tmp_path / "ws"),
            "--samples",
            str(tmp_path / "samples"),
            "--rows-per-table",
            "7",
        ],
    )
    assert result.exit_code == 0, result.output
    config = captured["config"]
    assert isinstance(config, sb.BootstrapConfig)
    assert config.rows_per_table == 7
    # Every UNSET threshold flag keeps DEFAULT_CONFIG's value.
    assert config.type_vote_threshold == sb.DEFAULT_CONFIG.type_vote_threshold
    assert config.pattern_threshold == sb.DEFAULT_CONFIG.pattern_threshold
    assert config.containment_threshold == sb.DEFAULT_CONFIG.containment_threshold
    assert config.enum_max_distinct == sb.DEFAULT_CONFIG.enum_max_distinct
    assert config.enum_max_distinct_ratio == sb.DEFAULT_CONFIG.enum_max_distinct_ratio

    # Bidirectional control: setting every remaining threshold flag actually
    # changes every one of them away from the default.
    result2 = CliRunner().invoke(
        main,
        [
            "schema",
            "bootstrap",
            "--engagement",
            _ENGAGEMENT,
            "--workspace",
            str(tmp_path / "ws2"),
            "--samples",
            str(tmp_path / "samples2"),
            "--type-vote-threshold",
            "0.42",
            "--pattern-threshold",
            "0.43",
            "--containment-threshold",
            "0.44",
            "--enum-max-distinct",
            "3",
            "--enum-max-distinct-ratio",
            "0.05",
        ],
    )
    assert result2.exit_code == 0, result2.output
    config2 = captured["config"]
    assert isinstance(config2, sb.BootstrapConfig)
    assert config2.type_vote_threshold == 0.42
    assert config2.pattern_threshold == 0.43
    assert config2.containment_threshold == 0.44
    assert config2.enum_max_distinct == 3
    assert config2.enum_max_distinct_ratio == 0.05
    # An unset flag on THIS invocation (rows_per_table) reverts to default,
    # proving no state leaks across invocations.
    assert config2.rows_per_table == sb.DEFAULT_CONFIG.rows_per_table


def test_bootstrap_defaults_workspace_and_samples_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def _fake_run_bootstrap(samples_dir, workspace_dir, *, engagement, repo_root, config, refine):
        captured["samples_dir"] = samples_dir
        captured["workspace_dir"] = workspace_dir
        return sb.BootstrapRunResult(
            workspace=str(workspace_dir),
            refined=refine,
            files_written=[],
            table_count=0,
            class_count=0,
            enum_count=0,
            edge_count=0,
            merge=None,
            orphaned_classes=[],
        )

    monkeypatch.setattr(sb, "run_bootstrap", _fake_run_bootstrap)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["schema", "bootstrap", "--engagement", _ENGAGEMENT])

    assert result.exit_code == 0, result.output
    assert captured["workspace_dir"] == Path("discovery") / _ENGAGEMENT
    assert captured["samples_dir"] == Path("discovery") / _ENGAGEMENT / "samples"


def test_cli_import_does_not_import_schema_bootstrap() -> None:
    # `import ne.cli` must not drag ne.schema_bootstrap (the lazy-import
    # discipline) — run in a fresh interpreter so a co-resident test's
    # earlier import can't mask it (test_demo_cli.py's own pattern).
    code = "import ne.cli, sys; assert 'ne.schema_bootstrap' not in sys.modules, 'leaked'"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
