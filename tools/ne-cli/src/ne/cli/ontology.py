# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne ontology`` — READ-ONLY operator/steward inspection of the T3 ontology store.

NE-ONTOLOGY-INSPECT: the missing operator surface over the live T3 ontology +
verified-answer layer (today inspection is programmatic-only). Five subcommands:
``graphs`` (named graphs + per-graph triple counts + pinned MANIFEST versions),
``verified`` (the registered verified-answer templates), ``sparql`` (a governed
SELECT/ASK through the EXISTING gateway steward surface), ``conformance`` (the
existing BFO+SHACL conformance gate + quarantine summary), and ``lint`` (a
documented alias of ``conformance`` — the NE-085-deferred lint verb). Every
subcommand supports ``--json`` for scripting and a human table by default.

READ-ONLY GUARANTEE: this command group reuses the gateway's C12 steward read gate
(``ValidatingTripleStore.sparql`` — SELECT/ASK-only, banned-keyword refusal), the
``load_template_registry`` loader, and the ``run_corpus_conformance`` gate. It adds
NO raw-backend entry point, NO write/mutation path, and NO gate bypass — curation is
the SEPARATE NE-ONTOLOGY-FLYWHEEL unit. Quarantine access is ``.list()`` only.

STARTUP DISCIPLINE (the audit.py/schema.py/connect.py precedent): every
``ne.brain_ontology`` import is LAZY inside a command body so bare ``ne --help`` never
drags rdflib/pyoxigraph.
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Any

import click


def _settings() -> Any:
    """Build ``OntologySettings`` from the ambient env (the ne-api layering).

    Env-driven exactly how ne-api's platform.py resolves ``OntologySettings()``:
    NE_ONTOLOGY_BACKEND, NE_ONTOLOGY_OXIGRAPH_PATH, NE_ONTOLOGY_FUSEKI_URL,
    NE_ONTOLOGY_FUSEKI_USER/NE_ONTOLOGY_FUSEKI_PASSWORD, NE_ONTOLOGY_QUARANTINE_DB,
    NE_ONTOLOGY_VERIFIED_ANSWERS_ENABLED, NE_ONTOLOGY_VERIFIED_TEMPLATES_DIR.
    """
    from ne.brain_ontology.config import OntologySettings

    return OntologySettings()  # type: ignore[call-arg]


def _build_gateway(settings: Any) -> Any:
    """Build the gateway over the configured backend (THE DECIDED TEST SEAM).

    Lazy-imports the public gateway factories so ``ne --help`` never drags the store
    backends. Tests monkeypatch ``ne.cli.ontology._build_gateway`` to return a REAL,
    seeded ``ValidatingTripleStore`` so the C12 rejection tests stay non-vacuous.
    Returns the gateway ONLY (never a raw backend — the backend-privacy invariant).
    """
    from ne.brain_ontology import build_fuseki_store, build_oxigraph_store

    try:
        if settings.backend == "fuseki":
            return build_fuseki_store(settings)
        return build_oxigraph_store(settings)
    except ValueError as exc:  # e.g. fuseki backend without a fuseki_url
        raise click.ClickException(str(exc)) from exc


@click.group("ontology")
def ontology() -> None:
    """Read-only inspection of the T3 ontology store + verified answers (steward surface)."""


@ontology.command("graphs")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def graphs(as_json: bool) -> None:
    """List the loaded named graphs (+ triple counts) and the pinned ontology versions."""
    settings = _settings()
    store = _build_gateway(settings)
    rows = asyncio.run(
        store.sparql(
            "SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g ORDER BY ?g"
        )
    )
    graph_rows: list[dict[str, Any]] = [
        {"graph_iri": row["g"], "triples": int(row["n"])} for row in rows
    ]
    graph_rows.sort(key=lambda r: r["graph_iri"])
    total = sum(r["triples"] for r in graph_rows)

    version = store.ontology_version()
    sources = dict(version.sources)

    if as_json:
        payload = {
            "graphs": graph_rows,
            "total_triples": total,
            "ontology": {"sources": sources, "shapes_hash": version.shapes_hash},
        }
        click.echo(_json.dumps(payload, indent=2, sort_keys=True))
        return

    if not graph_rows:
        click.echo("no named graphs")
    else:
        for r in graph_rows:
            click.echo(f"{r['graph_iri']:<60}  {r['triples']:>10}")
        click.echo(f"{'TOTAL':<60}  {total:>10}")
    click.echo("")
    click.echo("pinned ontology versions:")
    for name in sorted(sources):
        click.echo(f"  {name}: {sources[name]}")
    click.echo(f"  shapes_hash: {version.shapes_hash}")


@ontology.command("verified")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option(
    "--templates-dir",
    type=click.Path(exists=False),
    default=None,
    help="Inspect a template bundle directly (overrides NE_ONTOLOGY_VERIFIED_TEMPLATES_DIR).",
)
def verified(as_json: bool, templates_dir: str | None) -> None:
    """List the registered verified-answer templates (id@version, args, citation)."""
    settings = _settings()

    resolved: Path | None = (
        Path(templates_dir) if templates_dir is not None else settings.verified_templates_dir
    )

    # DISABLED: no explicit dir AND verified answers are off → a clean exit-0 report.
    if templates_dir is None and not settings.verified_answers_enabled:
        if as_json:
            payload: dict[str, Any] = {
                "enabled": False,
                "templates_dir": None,
                "registry_hash": None,
                "templates": [],
            }
            click.echo(_json.dumps(payload, indent=2, sort_keys=True))
        else:
            click.echo("verified answers disabled (NE_ONTOLOGY_VERIFIED_ANSWERS_ENABLED is false)")
        return

    # ENABLED (or explicit dir requested) but no directory resolved → fail loud.
    if resolved is None:
        raise click.ClickException(
            "verified answers are enabled but verified_templates_dir is unset — "
            "set NE_ONTOLOGY_VERIFIED_TEMPLATES_DIR"
        )

    from ne.brain_ontology import TemplateRegistryError, load_template_registry

    try:
        registry = load_template_registry(resolved)
    except TemplateRegistryError as exc:
        raise click.ClickException(str(exc)) from exc

    templates: list[dict[str, Any]] = [
        {
            "id": t.template_id,
            "version": t.version,
            "tool_name": t.tool_name,
            "description": t.description,
            "args": t.args_json_schema(),
            "columns": list(t.answer.columns),
            "citation_var": t.answer.citation_var,
            "label_var": t.answer.label_var,
            "graph_classes": list(t.scope.graph_classes),
            "max_rows": t.max_rows,
        }
        for t in registry.templates
    ]

    if as_json:
        payload = {
            "enabled": True,
            "templates_dir": str(resolved),
            "registry_hash": registry.fingerprint.registry_hash,
            "templates": templates,
        }
        click.echo(_json.dumps(payload, indent=2, sort_keys=True))
        return

    for t in templates:
        click.echo(f"{t['id']}@v{t['version']}  {t['tool_name']}")
        click.echo(f"  {t['description']}")
        schema_props = t["args"].get("properties", {})
        for name in schema_props:
            arg_type = schema_props[name].get("type", "?")
            permitted = schema_props[name].get("enum")
            suffix = f" — permitted: {', '.join(permitted)}" if permitted is not None else ""
            click.echo(f"  arg {name}: {arg_type}{suffix}")
        click.echo(f"  citation: {t['citation_var']}  columns: {', '.join(t['columns'])}")
    click.echo(f"registry {registry.fingerprint.registry_hash}")


@ontology.command("sparql")
@click.argument("query")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def sparql(query: str, as_json: bool) -> None:
    """Run a governed read (SELECT/ASK only) through the gateway steward surface (C12)."""
    store = _build_gateway(_settings())
    try:
        rows = asyncio.run(store.sparql(query))
    except ValueError as exc:  # C12 refusal (banned keyword / non-SELECT-ASK form)
        raise click.ClickException(str(exc)) from exc

    dict_rows = [dict(r) for r in rows]

    if as_json:
        payload = {"rows": dict_rows, "row_count": len(dict_rows)}
        click.echo(_json.dumps(payload, indent=2, sort_keys=True))
        return

    if not dict_rows:
        click.echo("0 rows")
        return
    header = list(dict_rows[0].keys())
    widths = {col: max(len(col), *(len(str(r.get(col, ""))) for r in dict_rows)) for col in header}
    click.echo("  ".join(f"{col:<{widths[col]}}" for col in header))
    for r in dict_rows:
        click.echo("  ".join(f"{r.get(col, '')!s:<{widths[col]}}" for col in header))
    click.echo(f"{len(dict_rows)} row(s)")


@ontology.command("conformance")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def conformance(as_json: bool) -> None:
    """Run the offline BFO+SHACL conformance gate + summarize the quarantine lane.

    Exit 1 IFF the gate reports untriaged errors (after printing). Quarantine
    contents are informational and NEVER affect the exit code.
    """
    from ne.brain_ontology.lint.gate import TRIAGED_ERROR_ALLOWLIST, run_corpus_conformance

    report = run_corpus_conformance()

    # Quarantine summary WITHOUT building the gateway (keeps `conformance` runnable
    # offline even under backend=fuseki): read the inspectable lane's .list() only.
    settings = _settings()
    db_path: str | None
    entries: tuple[Any, ...]
    if settings.quarantine_db is None:
        db_path = None
        entries = ()
        quarantine_note = "quarantine lane unconfigured (NE_ONTOLOGY_QUARANTINE_DB unset)"
    else:
        from ne.brain_ontology.quarantine import SqliteTripleQuarantine

        db_path = str(settings.quarantine_db)
        entries = tuple(SqliteTripleQuarantine(settings.quarantine_db).list())
        quarantine_note = ""

    entry_payloads = [
        {
            "quarantine_id": e.quarantine_id,
            "created_at": e.created_at,
            "provenance": e.provenance,
            "graph_iri": e.graph_iri,
            "shapes_hash": e.shapes_hash,
            "violations": list(e.violations),
        }
        for e in entries
    ]

    if as_json:
        payload = {
            "gate": "failed" if report.errors else "passed",
            "untriaged_errors": list(report.errors),
            "warnings": list(report.warnings),
            "triaged_categories": sorted(TRIAGED_ERROR_ALLOWLIST),
            "quarantine": {
                "db_path": db_path,
                "count": len(entry_payloads),
                "entries": entry_payloads,
            },
        }
        click.echo(_json.dumps(payload, indent=2, sort_keys=True))
    else:
        for warning in report.warnings:
            click.echo(f"WARNING {warning}")
        if TRIAGED_ERROR_ALLOWLIST:
            click.echo(f"triaged ERROR categories (allowlisted): {sorted(TRIAGED_ERROR_ALLOWLIST)}")
        if report.errors:
            click.echo(f"GATE FAILED: {len(report.errors)} untriaged BFO-conformance ERROR(s):")
            for error in report.errors:
                click.echo(f"  ERROR {error}")
        else:
            click.echo("GATE PASSED: zero untriaged BFO-conformance errors over the full corpus.")
        if quarantine_note:
            click.echo(quarantine_note)
        click.echo(f"quarantine: {len(entry_payloads)} entry(ies)")
        for e in entry_payloads:
            click.echo(f"  {e['quarantine_id']}  {e['created_at']}  graph={e['graph_iri']}")

    if report.errors:
        raise click.exceptions.Exit(1)


@ontology.command("lint")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.pass_context
def lint(ctx: click.Context, as_json: bool) -> None:
    """Alias for ``ne ontology conformance`` — the NE-085-deferred lint verb.

    The ontology "lint" capability IS the offline BFO+SHACL conformance gate
    (``run_corpus_conformance``); this verb exists under the name the NE-085 docs
    promised and delegates verbatim: same output, same ``--json`` shape, same
    exit-1-on-untriaged-errors contract, same read-only guarantee.
    """
    ctx.invoke(conformance, as_json=as_json)
