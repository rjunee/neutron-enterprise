# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED: the SPEC-row AGE round-trip via ``conformance.py --backend age``.

Gate (NE-081 ``test_graph_store_conformance_server.py`` wording): skip unless
``NE_AGE_DSN`` is set AND the sidecar venv is built. When live, this runs the
conformance harness against ``apache/age:release_PG17_1.6.0`` on a uniquely-named
graph and asserts the scenario outcomes (episodic append → AGE graph facts →
temporal/traversal/fulltext/vector recall + group-isolation) plus the recorded probe
rungs. Container/DSN::

    docker run -d -p 5455:5432 -e POSTGRES_PASSWORD=age apache/age:release_PG17_1.6.0
    export NE_AGE_DSN='postgresql://postgres:age@127.0.0.1:5455/postgres'
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid

import pytest

from ne.envbuild import venv_python
from ne.memory_graph.sidecar import GraphitiSidecar

from ._sidecar_loader import load_sidecar_module

_sidecar = GraphitiSidecar()
# conformance.py's module-level imports are stdlib-only, so it loads without the venv;
# used here only to pull the shared TOKEN_TEXT constant (FIX-1 round-trip expectation).
_conformance = load_sidecar_module("conformance.py", "_ne083c_conformance")
_HAVE_DSN = bool(os.environ.get("NE_AGE_DSN"))

pytestmark = pytest.mark.skipif(
    not (_HAVE_DSN and _sidecar.available()),
    reason=(
        "NE_AGE_DSN unset or sidecar venv not built — AGE driver round-trip LIVE-PARKED "
        "(run apache/age:release_PG17_1.6.0 and set NE_AGE_DSN; build the venv via "
        "GraphitiSidecar().ensure())"
    ),
)


def test_conformance_age_round_trip() -> None:  # pragma: no cover - live-parked
    graph = f"ne_conf_{uuid.uuid4().hex[:8]}"
    env = {
        **os.environ,
        "NE_MEMORY_GRAPH_AGE_DSN": os.environ["NE_AGE_DSN"],
        "NE_MEMORY_GRAPH_AGE_GRAPH": graph,
    }
    python = venv_python(_sidecar.venv_dir)
    proc = subprocess.run(
        [str(python), "conformance.py", "--backend", "age"],
        cwd=str(_sidecar.env_dir),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["backend"] == "age"
    # uuid lookup + group-filtered recall (rows 1/12/16)
    assert report["entity_by_uuid_name"] == "Alice"
    assert report["entities_in_group_a"] == ["Acme", "Alice", "Lead"]
    assert report["episodes_in_group_a"] == ["ep-a0", "ep-a1"]
    assert report["entity_count_group_a"] == 3
    # temporal as-of before/after edge invalidation at T2 (rows 5/6)
    assert report["alice_targets_asof_T1"] == ["Acme", "Lead"]
    assert report["alice_targets_asof_T2"] == ["Acme"]
    # traversal (rows 7/8)
    assert report["alice_1hop_targets"] == ["Acme", "Lead"]
    assert report["alice_2hop_names"] == ["Acme", "Lead"]
    # content-token recall + group isolation (rows 9/12): agent-b's "tea" never in a
    assert report["content_token_promoted_group_a"] == ["ep-a1"]
    assert report["content_token_tea_group_a"] == []
    assert report["content_token_tea_group_b"] == ["ep-b0"]
    assert report["entities_in_group_b"] == []
    # AGE side-table shims: tsvector fulltext + vector top-k (rows 9/10)
    side = report["age_side_table_recall"]
    assert side["ft_promoted_group_a"] == ["a1"]
    assert side["vec_top2_for_alice_vector"]
    assert side["vector_backend"] in ("pgvector", "client")
    # recorded probe rungs
    assert report["probe_rungs"]["predicate"]
    assert report["probe_rungs"]["vector_backend"]
    # FIX-2: fresh-graph search returns empty (never UndefinedTable/AgeQueryError)
    fresh = report["fresh_graph"]
    assert fresh["node_similarity"] == []
    assert fresh["edge_similarity"] == []
    assert fresh["node_fulltext"] == []
    # FIX-1: agtype-token-bearing property VALUES round-trip byte-identical
    assert report["token_node_summary"] == _conformance.TOKEN_TEXT
    assert report["token_edge_fact"] == _conformance.TOKEN_TEXT
    # FIX-3 / FIX-3b: source/target-filtered edge similarity (no unfiltered superset)
    esim = report["edge_similarity"]
    assert esim["filtered_alice_acme"] == ["works"]
    assert esim["wrong_pair_acme_lead"] == []
    assert esim["unfiltered"] == ["role", "works"]
    # Codex P1: BULK /episodes ingestion works on AGE (add_episode → UNWIND bulk saves).
    bulk = report["bulk_recall"]
    assert bulk["entities_in_group_d"] == ["Dana", "Feature"]
    assert bulk["episodes_in_group_d"] == ["bulk-ep0"]
    assert bulk["edge_facts_in_group_d"] == ["Dana shipped the feature"]
    assert bulk["dana_1hop_targets"] == ["Feature"]
    assert bulk["dana_mentioned_by_episode"] == ["bulk-ep0"]
    # Codex P1 finding-2: the bulk rows populated the ft/vec side tables (else /search
    # would be blind to episode-ingested data).
    assert side["bulk_ft_node_populated"] is True
    assert side["bulk_ft_edge_populated"] is True
    assert side["bulk_vec_node_populated"] is True
    assert side["bulk_vec_edge_populated"] is True
