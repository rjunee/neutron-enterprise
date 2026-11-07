# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED: canonical-JSON equality of the conformance scenario across backends.

The binding AGE == Neo4j-CE behavioral proof. Skips unless BOTH an AGE DSN
(``NE_AGE_DSN``) AND a Neo4j URI (``NE_MEMORY_NEO4J_URI``) are reachable AND the
sidecar venv is built. When live, runs ``conformance.py`` under both backends and
asserts the canonical JSON (backend key excepted) is identical.

The compared core now also covers the three review-fix paths (they are ordinary
top-level keys, so the equality assertion picks them up by construction):

* ``fresh_graph`` — FIX-2: a similarity/fulltext search on a freshly-built, unseeded
  graph returns empty on both backends (AGE no longer raises ``UndefinedTable``).
* ``token_node_summary`` / ``token_edge_fact`` — FIX-1: a property VALUE containing
  literal ``::vertex``/``::edge``/``::path``/``::numeric`` tokens round-trips
  byte-identical (Neo4j is the ground truth, so equality == an AGE round-trip proof).
* ``edge_similarity`` — FIX-3/FIX-3b: source/target-filtered edge similarity returns
  the SAME filtered set on AGE as Neo4j (not the unfiltered superset), and min_score
  parity makes the default-threshold case agree too.
* ``bulk_recall`` — Codex P1: recall over data ingested through Graphiti's BULK save
  path (``add_episode`` → ``add_nodes_and_edges_bulk_tx`` → UNWIND bulk queries, the
  actual ``/episodes`` service path). AGE must translate the multi-var
  ``WITH n, node CALL …`` vector write + dynamic labels and populate the side tables;
  the graph-fact recall it yields must equal Neo4j's.

The test also asserts (before the equality) that on the shipping AGE 1.6.0 image the
predicate and traversal probes select the DIRECT rungs (``predicate == 'any'``,
``traversal == 'direct_vle'``) — the reason the canonical equality holds unchanged now
that all three R1 ladders (NE-083c-FOLLOWUP) are probed AND applied in the query path.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from typing import cast

import pytest

from ne.envbuild import venv_python
from ne.memory_graph.sidecar import GraphitiSidecar

_sidecar = GraphitiSidecar()
_HAVE_AGE = bool(os.environ.get("NE_AGE_DSN"))
_HAVE_NEO4J = bool(os.environ.get("NE_MEMORY_NEO4J_URI"))

pytestmark = pytest.mark.skipif(
    not (_HAVE_AGE and _HAVE_NEO4J and _sidecar.available()),
    reason=(
        "need NE_AGE_DSN + NE_MEMORY_NEO4J_URI + sidecar venv — dual-backend "
        "conformance LIVE-PARKED (the AGE==Neo4j-CE proof)"
    ),
)


def _run(backend: str, env: dict[str, str]) -> dict[str, object]:  # pragma: no cover
    python = venv_python(_sidecar.venv_dir)
    proc = subprocess.run(
        [str(python), "conformance.py", "--backend", backend],
        cwd=str(_sidecar.env_dir),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_age_equals_neo4j_canonical_json() -> None:  # pragma: no cover - live-parked
    graph = f"ne_conf_{uuid.uuid4().hex[:8]}"
    age_env = {
        **os.environ,
        "NE_MEMORY_GRAPH_AGE_DSN": os.environ["NE_AGE_DSN"],
        "NE_MEMORY_GRAPH_AGE_GRAPH": graph,
    }
    age = _run("age", age_env)
    neo4j = _run("neo4j", dict(os.environ))
    # DIRECT-rung selection on the shipping AGE 1.6.0 image: the predicate probe accepts
    # any() and the traversal probe accepts variable-length *1..n, so the driver stays on
    # the direct rungs — which is WHY the canonical AGE==Neo4j equality below holds. The
    # vector backend is image-dependent (the pgvector-less live image selects CLIENT).
    age_rungs = cast("dict[str, str]", age["probe_rungs"])
    assert age_rungs["predicate"] == "any"
    assert age_rungs["traversal"] == "direct_vle"
    # AGE-specific keys are excluded from the binding graph-recall equality (documented):
    # probe rungs + the tsvector/vector side-table shims have no byte-identical Neo4j
    # analogue (Neo4j uses native RANGE/FULLTEXT/vector; AGE uses ts_rank/client-cosine).
    # The graph-recall CORE (uuid/group/temporal/traversal/count/collect/content-token +
    # isolation) IS compared and must match exactly — the AGE==Neo4j-CE proof.
    for doc in (age, neo4j):
        for key in ("backend", "probe_rungs", "age_side_table_recall"):
            doc.pop(key, None)
    assert age == neo4j
