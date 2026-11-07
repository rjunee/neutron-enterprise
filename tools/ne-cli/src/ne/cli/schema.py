# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""`ne schema` — LinkML codegen for packages/ne-schema, plus `ne schema bootstrap`
(NE-ESB-CLI, U3): the delivery-facing engagement schema bootstrap entrypoint.

One LinkML source (operational.yaml) generates the committed Pydantic models and
JSON Schema. `generate` regenerates them; `check` fails if the committed
artifacts have drifted from the source (the CI staleness gate).

codegen itself runs linkml inside an isolated hash-pinned venv (SPEC §2 C4) — see
ne.schema.codegen. That module is imported lazily inside each command so the CLI
stays light and does not import ne-schema on unrelated invocations.

`bootstrap` follows the SAME lazy-import discipline: ``ne.schema_bootstrap`` (a
runtime dependency of `ne-cli`, declared in `pyproject.toml` with its own
rationale comment, so the advertised command WORKS on a plain install) is
imported only inside the command body — never at module import time — so bare
`ne --help`/`ne schema --help` never pulls in pydantic/pyyaml's transitive
closure or the codegen-venv machinery. Proven, not just claimed: see
`tools/ne-cli/tests/test_schema_bootstrap_cli.py::test_cli_import_does_not_import_schema_bootstrap`.
"""

from __future__ import annotations

from pathlib import Path

import click

from ne.cli.checks import _find_repo_root


@click.group("schema")
def schema() -> None:
    """LinkML codegen for packages/ne-schema."""


@schema.command("generate")
def generate() -> None:
    """Regenerate the committed Pydantic models + JSON Schema from the LinkML source."""
    # Lazy import: ne-schema is a workspace member co-installed in the dev venv.
    from ne.schema import codegen

    root = _find_repo_root(Path.cwd())
    codegen.generate(root)
    click.echo("schema artifacts regenerated")


@schema.command("check")
def check() -> None:
    """Fail if the committed artifacts are stale vs the LinkML source."""
    from ne.schema import codegen

    root = _find_repo_root(Path.cwd())
    stale = codegen.check_freshness(root)
    if stale:
        for name in stale:
            click.echo(f"stale generated artifact: {name}")
        click.echo("run: uv run ne schema generate")
        raise click.exceptions.Exit(1)
    click.echo("schema artifacts up to date")


@schema.command("bootstrap")
@click.option("--engagement", required=True, help="Engagement slug (^[a-z][a-z0-9-]{0,62}$).")
@click.option(
    "--workspace",
    type=click.Path(path_type=Path),
    default=None,
    help="Workspace directory. Default: ./discovery/<engagement>/",
)
@click.option(
    "--samples",
    type=click.Path(path_type=Path),
    default=None,
    help="Sample directory (.csv/.tsv/.jsonl/.json). Default: <workspace>/samples",
)
@click.option(
    "--rows-per-table", type=int, default=None, help="Override BootstrapConfig.rows_per_table."
)
@click.option(
    "--type-vote-threshold",
    type=float,
    default=None,
    help="Override BootstrapConfig.type_vote_threshold.",
)
@click.option(
    "--pattern-threshold",
    type=float,
    default=None,
    help="Override BootstrapConfig.pattern_threshold.",
)
@click.option(
    "--containment-threshold",
    type=float,
    default=None,
    help="Override BootstrapConfig.containment_threshold.",
)
@click.option(
    "--enum-max-distinct",
    type=int,
    default=None,
    help="Override BootstrapConfig.enum_max_distinct.",
)
@click.option(
    "--enum-max-distinct-ratio",
    type=float,
    default=None,
    help="Override BootstrapConfig.enum_max_distinct_ratio.",
)
@click.option(
    "--llm/--no-llm",
    "llm",
    default=False,
    help="LLM-assisted semantic labeling (U5, NE-ESB-LABEL — not built yet; always fails closed).",
)
@click.option(
    "--refine",
    is_flag=True,
    default=False,
    help="Three-way-merge a new machine draft into an existing workspace (design §4.4).",
)
def bootstrap(
    engagement: str,
    workspace: Path | None,
    samples: Path | None,
    rows_per_table: int | None,
    type_vote_threshold: float | None,
    pattern_threshold: float | None,
    containment_threshold: float | None,
    enum_max_distinct: int | None,
    enum_max_distinct_ratio: float | None,
    llm: bool,
    refine: bool,
) -> None:
    """Profile a sample directory into a reviewable schema-draft workspace.

    Drives the U1 profiler + U2 emitter/round-trip/renderers end to end
    (`ne.schema_bootstrap.workspace.run_bootstrap`): the real-codegen-venv
    round-trip gate is proven to pass BEFORE any run artifact lands on disk.
    `--refine` re-runs inference over (possibly updated) samples and performs
    a deterministic three-way merge into the existing human-reviewed draft —
    the human side wins every conflict, and a human-deleted class/enum stays
    deleted (tracked in `tombstones.json`).
    """
    if llm:
        # Fail closed BEFORE any filesystem or library work (AIR_GAP posture,
        # zero egress): U5 (NE-ESB-LABEL) is not built yet.
        raise click.UsageError(
            "--llm is not available yet (U5 NE-ESB-LABEL is not built); rerun with --no-llm"
        )

    # Lazy import: ne-schema-bootstrap is a workspace member co-installed in
    # the dev venv, but this import must never happen on `ne --help`/`ne
    # schema --help` (see the module docstring's startup discipline).
    from ne.schema_bootstrap import BootstrapConfig, SchemaBootstrapError, run_bootstrap

    workspace_dir = workspace if workspace is not None else Path("discovery") / engagement
    samples_dir = samples if samples is not None else workspace_dir / "samples"

    overrides = {
        "rows_per_table": rows_per_table,
        "type_vote_threshold": type_vote_threshold,
        "pattern_threshold": pattern_threshold,
        "containment_threshold": containment_threshold,
        "enum_max_distinct": enum_max_distinct,
        "enum_max_distinct_ratio": enum_max_distinct_ratio,
    }
    config = BootstrapConfig(**{k: v for k, v in overrides.items() if v is not None})

    repo_root = _find_repo_root(Path.cwd())

    try:
        result = run_bootstrap(
            samples_dir,
            workspace_dir,
            engagement=engagement,
            repo_root=repo_root,
            config=config,
            refine=refine,
        )
    except SchemaBootstrapError as exc:
        # The library's own error messages are the reviewed disclosure
        # surface (paths + mechanical class/table/enum names only — never a
        # sample value; see ne.schema_bootstrap.errors' module docstring).
        # This CLI relays `str(exc)` verbatim and adds no column/value of
        # its own to the message.
        click.echo(f"error: {exc}", err=True)
        raise click.exceptions.Exit(1) from None

    click.echo(f"workspace: {result.workspace}")
    click.echo(f"refined: {result.refined}")
    if result.refined:
        # (2026-07-30, NE-ESB-CLI continuation, Argus P3): on --refine these
        # four counts describe THIS RUN's fresh machine draft, NOT the
        # merged draft that actually landed as schema-draft.yaml (see
        # BootstrapRunResult's docstring) — the round-4 landing-view defect
        # class, previously disclosed on-disk (inference-report.md, the
        # pr-body) but not on the terminal. Label it here rather than
        # leaving an operator to infer the mismatch from a stale count.
        click.echo(
            f"tables: {result.table_count}  classes: {result.class_count}  "
            f"enums: {result.enum_count}  edges: {result.edge_count}   "
            "(classes/enums/edges: fresh machine draft, not landing count "
            "— see the merge line below)"
        )
    else:
        click.echo(
            f"tables: {result.table_count}  classes: {result.class_count}  "
            f"enums: {result.enum_count}  edges: {result.edge_count}"
        )
    for name in result.files_written:
        click.echo(f"wrote: {name}")

    if result.merge is not None:
        merge = result.merge
        click.echo(
            f"merge: human_kept={len(merge.human_kept)} "
            f"machine_updated={len(merge.machine_updated)} "
            f"blocked_resurrections={len(merge.blocked_resurrections)} "
            f"human_readded={len(merge.human_readded)}"
        )
        if merge.tombstoned_classes:
            click.echo(f"tombstoned classes: {', '.join(merge.tombstoned_classes)}")
        if merge.tombstoned_enums:
            click.echo(f"tombstoned enums: {', '.join(merge.tombstoned_enums)}")

    if result.orphaned_classes:
        # Mechanical class names only (see ne.schema_bootstrap.workspace's
        # BootstrapRunResult.orphaned_classes docstring) — a class present
        # in the landing draft but not wired into the tree-root container's
        # attributes; the draft still generates, but needs re-wiring by
        # hand (design §4.4, NE-ESB-CLI U3 residual, 2026-07-29 Argus P1).
        click.echo(
            "WARNING: orphaned classes (not reachable from the tree-root "
            f"container): {', '.join(result.orphaned_classes)}"
        )
