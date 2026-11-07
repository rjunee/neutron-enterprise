# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Venv-gated canaries pinning graphiti-core==0.29.2 + the AgeDriver seam.

Skips cleanly when the sidecar venv is absent. When present, shells short scripts
through the venv python asserting: the pin (0.29.2); the R3.i 'ON CREATE SET' canary
absence in the pinned node/edge query modules (loud failure on upstream adoption);
that ``GraphDriver`` still exposes the seam names ``AgeDriver`` implements; and that
``from graphiti_age import AgeDriver`` succeeds inside the venv, subclasses
``GraphDriver``, and constructs without connecting.
"""

from __future__ import annotations

import subprocess

import pytest

from ne.envbuild import venv_python
from ne.memory_graph.sidecar import GraphitiSidecar

_sidecar = GraphitiSidecar()

pytestmark = pytest.mark.skipif(
    not _sidecar.available(),
    reason="sidecar venv not built; run GraphitiSidecar().ensure()",
)


def _run_in_venv(script: str) -> str:
    python = venv_python(_sidecar.venv_dir)
    proc = subprocess.run(
        [str(python), "-c", script],
        cwd=str(_sidecar.env_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"venv script failed: {proc.stderr}\n{proc.stdout}"
    return proc.stdout.strip()


def test_graphiti_core_pin_is_0_29_2() -> None:
    out = _run_in_venv("import importlib.metadata as m; print(m.version('graphiti-core'))")
    assert out == "0.29.2"


def test_on_create_set_canary_absent() -> None:
    # R3.i: the pinned save queries use SET (overwrite), not ON CREATE SET. If a future
    # graphiti adopts ON CREATE SET semantics this canary fails loud (re-review needed).
    script = (
        "import graphiti_core, pathlib\n"
        "root = pathlib.Path(graphiti_core.__file__).parent\n"
        "srcs = ["
        "  root/'models'/'nodes'/'node_db_queries.py',"
        "  root/'models'/'edges'/'edge_db_queries.py',"
        "]\n"
        "hits = [str(p) for p in srcs if 'ON CREATE SET' in p.read_text()]\n"
        "print('HITS:' + ','.join(hits))"
    )
    out = _run_in_venv(script)
    assert out == "HITS:"


def test_graph_driver_seam_names_present() -> None:
    script = (
        "from graphiti_core.driver.driver import GraphDriver\n"
        "names = ['execute_query','session','close','delete_all_indexes',"
        "'build_indices_and_constraints','build_fulltext_query','search_interface',"
        "'graph_operations_interface']\n"
        "missing = [n for n in names if not hasattr(GraphDriver, n)]\n"
        "print('MISSING:' + ','.join(missing))"
    )
    out = _run_in_venv(script)
    assert out == "MISSING:"


def test_age_driver_imports_and_subclasses_and_constructs() -> None:
    script = (
        "from graphiti_core.driver.driver import GraphDriver\n"
        "from graphiti_age import AgeDriver\n"
        "assert issubclass(AgeDriver, GraphDriver)\n"
        "d = AgeDriver(dsn='postgresql://noconnect', graph_name='ne_memory')\n"
        "assert d.provider is not None\n"
        "assert d.search_interface is not None\n"
        "print('OK')"
    )
    assert _run_in_venv(script) == "OK"
