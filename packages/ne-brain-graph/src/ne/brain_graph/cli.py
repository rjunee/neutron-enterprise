# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne-brain-graph`` CLI — stdlib argparse (the ``ne.eval.cli`` house style).

Verbs: ``sync --mode backfill|incremental --source <name>``, ``backfill``
(alias), ``verify`` (connect, ensure_graph, print the probed predicate rung +
node/edge counts). Composition is from :class:`BrainGraphSettings`; the CLI
REFUSES to run when the tier is disabled.

The store/runner composition is behind module-level hooks so tests can drive the
verbs over the in-memory store + a fixture connector without a live Postgres.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Protocol

from ne.brain_graph.config import BrainGraphSettings

__all__ = ["main"]


class _VerifiableStore(Protocol):
    async def ensure_graph(self) -> None: ...
    async def stats(self) -> dict[str, int]: ...


def load_settings() -> BrainGraphSettings:
    """Load settings (overridable in tests)."""
    return BrainGraphSettings()


def store_for(settings: BrainGraphSettings) -> _VerifiableStore:
    """Build the graph store for the CLI (overridable in tests)."""
    from ne.brain_graph.age.store import AgeGraphStore

    return AgeGraphStore(settings.database_url, settings.graph_name)


def compose_runner(settings: BrainGraphSettings, source: str) -> Any:
    """Build a GraphSyncRunner for ``source`` (host/test-provided).

    The default has no way to construct a source connector — that is a
    composition-root responsibility (ne-ingest). Tests override this hook.
    """
    raise RuntimeError(
        "connector composition is host-provided (ne-ingest); no default runner for "
        f"source {source!r}"
    )


def compose_er_job(settings: BrainGraphSettings, entity_class: str | None) -> Any:
    """Build a SplinkResolutionJob (host/test-provided; needs the sidecar + store)."""
    raise RuntimeError("ER job composition is host-provided (ne-worker); no default")


def compose_merge_context(settings: BrainGraphSettings) -> Any:
    """Return ``(engine, review_service, store, audit_sink)`` (host/test-provided)."""
    raise RuntimeError("merge composition is host-provided (ne-worker/ne-ingest); no default")


def compose_extraction_job(settings: BrainGraphSettings) -> Any:
    """Build an ExtractionJob (host/test-provided; needs the gateway + chunk source)."""
    raise RuntimeError("extraction job composition is host-provided (ne-worker/ne-api); no default")


def compose_extraction_purge_context(settings: BrainGraphSettings) -> Any:
    """Return ``(store, audit_sink)`` for the purge verb (host/test-provided)."""
    raise RuntimeError(
        "extraction purge composition is host-provided (ne-worker/ne-api); no default"
    )


async def compose_lightrag_rebuild(settings: BrainGraphSettings) -> None:
    """Rebuild the LightRAG projection: scan the graph → project → sidecar insert.

    Overridable in tests. The default composes the in-repo store + the hash-pinned
    LightRAG sidecar; the projection is a derived, rebuildable index.
    """
    from ne.brain_graph.age.store import AgeGraphStore
    from ne.brain_graph.lightrag import LightRagProjector, LightRagSidecar

    store = AgeGraphStore(settings.database_url, settings.graph_name)
    payload = await LightRagProjector(store).build_payload()
    LightRagSidecar(env_dir=settings.lightrag_env_dir).project(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ne-brain-graph", description="Tier-2 entity graph.")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="run a graph sync")
    sync.add_argument("--mode", choices=("backfill", "incremental"), default="incremental")
    sync.add_argument("--source", required=True)

    backfill = sub.add_parser("backfill", help="alias for `sync --mode backfill`")
    backfill.add_argument("--source", required=True)

    sub.add_parser("verify", help="connect, ensure graph, print probed rung + counts")

    er_run = sub.add_parser("er-run", help="run Splink entity resolution -> proposals")
    er_run.add_argument("--class", dest="entity_class", default=None)

    apply_merge = sub.add_parser("apply-merge", help="apply an APPROVED entity-merge proposal")
    apply_merge.add_argument("proposal_id")
    apply_merge.add_argument("--actor", default="cli")

    unmerge = sub.add_parser("unmerge", help="reverse a merge (delete the ne:er SAME_AS edge)")
    unmerge.add_argument("edge_key")
    unmerge.add_argument("--node", dest="non_canonical_key", required=True)
    unmerge.add_argument("--actor", default="cli")
    unmerge.add_argument("--confirm", action="store_true")

    sub.add_parser("rebuild-lightrag", help="rebuild the LightRAG projection from the graph")

    extraction = sub.add_parser("extraction", help="LLM entity extraction (NE-T2X, opt-in)")
    extraction_sub = extraction.add_subparsers(dest="extraction_command", required=True)
    ext_run = extraction_sub.add_parser("run", help="run mode-1 extraction over eligible chunks")
    ext_run.add_argument("--source", default=None)
    ext_purge = extraction_sub.add_parser(
        "purge",
        help=(
            "delete every ne:llm graph element (DETACH: also removes any edge attached to a "
            "purged node, including ER SAME_AS anchorings)"
        ),
    )
    ext_purge.add_argument("--actor", default="cli")
    ext_purge.add_argument("--confirm", action="store_true")

    return parser


async def _verify(settings: BrainGraphSettings) -> int:
    store = store_for(settings)
    await store.ensure_graph()
    counts = await store.stats()
    rung = getattr(store, "predicate_style", "unknown")
    print(f"graph={settings.graph_name} predicate_rung={rung}")
    print(f"nodes={counts.get('nodes', 0)} edges={counts.get('edges', 0)}")
    return 0


async def _sync(settings: BrainGraphSettings, source: str, mode: str) -> int:
    runner = compose_runner(settings, source)
    report = await runner.run(mode=mode)
    print(
        f"source={source} mode={mode} seen={report.seen} upserted={report.upserted} "
        f"unchanged={report.unchanged} acl_healed={report.acl_healed} "
        f"quarantined={report.quarantined} unmapped={report.unmapped} "
        f"pruned={report.pruned} pending_edges={report.pending_edges}"
    )
    return 0


async def _er_run(settings: BrainGraphSettings, entity_class: str | None) -> int:
    del settings, entity_class
    print(
        "er-run refused: entity resolution is unsupported before current-source authority",
        file=sys.stderr,
    )
    return 3


async def _apply_merge(settings: BrainGraphSettings, proposal_id: str, actor: str) -> int:
    del settings, proposal_id, actor
    print(
        "apply-merge refused: entity resolution is unsupported before current-source authority",
        file=sys.stderr,
    )
    return 3


async def _unmerge(
    settings: BrainGraphSettings, edge_key: str, non_canonical_key: str, actor: str
) -> int:
    del settings, edge_key, non_canonical_key, actor
    print(
        "unmerge refused: retained ER state has no current-source authority stamp",
        file=sys.stderr,
    )
    return 3


async def _extraction_run(settings: BrainGraphSettings, source: str | None) -> int:
    from ne.brain_graph.extraction.job import ExtractionModeNotImplementedError

    if not settings.extraction_enabled:
        print(
            "ne-brain-graph: LLM extraction is disabled (set extraction_enabled=true to run)",
            file=sys.stderr,
        )
        return 2
    job = compose_extraction_job(settings)
    try:
        report = await job.run(source=source)
    except ExtractionModeNotImplementedError as exc:
        # Codex C1 (round-4): the job itself is the source of truth for this
        # refusal (public API, not only CLI-reachable) — this catch is UX
        # only, converting the typed refusal into the same clean
        # stderr+exit-code shape as `apply-merge`'s `MergeGateError` handling.
        print(f"extraction-run refused: {exc}", file=sys.stderr)
        return 3
    print(
        f"extraction-run run_id={report.run_id} chunks_scanned={report.chunks_scanned} "
        f"batches_built={report.batches_built} proposals_appended={report.proposals_appended} "
        f"mentions={report.mentions} "
        f"discarded_below_threshold={report.discarded_below_threshold} "
        f"quarantined_invalid={report.quarantined_invalid}"
    )
    return 0


async def _extraction_purge(settings: BrainGraphSettings, actor: str, confirm: bool) -> int:
    if not settings.extraction_enabled:
        print(
            "ne-brain-graph: LLM extraction is disabled (set extraction_enabled=true to run)",
            file=sys.stderr,
        )
        return 2
    if not confirm:
        print(
            "extraction purge: pass --confirm to delete every ne:llm element (DETACH: purging "
            "extracted nodes also removes any incident edge of ANY source, including ER SAME_AS "
            "edges that anchored them)",
            file=sys.stderr,
        )
        return 2

    import json

    from ne.contracts import get_config_hash
    from ne.events import new_audit_event
    from ne.schema import AuditOutcome

    store, audit_sink = compose_extraction_purge_context(settings)
    counts = await store.purge_llm_extracted()
    await audit_sink.append(
        new_audit_event(
            action="graph.extraction_purge",
            resource="graph://extraction/purge",
            outcome=AuditOutcome.success,
            actor=actor,
            detail=json.dumps(dict(counts)),
            config_hash=get_config_hash(),
            provenance={"source": "ne-brain-graph", "component": "extraction.cli"},
        )
    )
    print(f"extraction-purge nodes={counts.get('nodes', 0)} edges={counts.get('edges', 0)}")
    return 0


async def _rebuild_lightrag(settings: BrainGraphSettings) -> int:
    if not settings.lightrag_enabled:
        print(
            "ne-brain-graph: LightRAG is disabled (set lightrag_enabled=true to rebuild)",
            file=sys.stderr,
        )
        return 2
    await compose_lightrag_rebuild(settings)
    print("rebuild-lightrag ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = load_settings()
    if not settings.enabled:
        print(
            "ne-brain-graph: the Tier-2 graph is disabled "
            "(set NE_BRAIN_GRAPH_ENABLED / enabled=true to run)",
            file=sys.stderr,
        )
        return 2

    if args.command == "verify":
        return asyncio.run(_verify(settings))
    if args.command == "backfill":
        return asyncio.run(_sync(settings, args.source, "backfill"))
    if args.command == "sync":
        return asyncio.run(_sync(settings, args.source, args.mode))
    if args.command == "er-run":
        return asyncio.run(_er_run(settings, args.entity_class))
    if args.command == "apply-merge":
        return asyncio.run(_apply_merge(settings, args.proposal_id, args.actor))
    if args.command == "unmerge":
        if not args.confirm:
            print("unmerge: pass --confirm to reverse a merge", file=sys.stderr)
            return 2
        return asyncio.run(_unmerge(settings, args.edge_key, args.non_canonical_key, args.actor))
    if args.command == "rebuild-lightrag":
        return asyncio.run(_rebuild_lightrag(settings))
    if args.command == "extraction":
        if args.extraction_command == "run":
            return asyncio.run(_extraction_run(settings, args.source))
        if args.extraction_command == "purge":
            return asyncio.run(_extraction_purge(settings, args.actor, args.confirm))
    return 1  # pragma: no cover - argparse requires a subcommand


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
