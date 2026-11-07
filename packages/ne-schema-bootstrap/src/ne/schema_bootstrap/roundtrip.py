# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The round-trip validator: PROVES an emitted draft generates through the
REAL isolated codegen venv — not a YAML-well-formedness check.

``validate_draft_generates`` writes ``yaml_text`` to a temp directory and
subprocesses ``packages/ne-schema/codegen/generate.py`` INSIDE the isolated,
hash-pinned ``.codegen-venv`` (``ne.schema.codegen.ensure_env`` — this
inherits that module's AIR_GAP fail-closed posture unchanged: under
``AIR_GAP=1`` an absent/stale venv raises
:class:`ne.net.airgap.AirGapViolationError` BEFORE this module ever
subprocesses anything, see ``tests/test_roundtrip.py::test_air_gap_fail_closed_before_any_egress``).
It then PROVES the draft actually generates — not merely that generation
exited 0 — by loading the generated Pydantic module in a FRESH platform
Python subprocess (no ``linkml_runtime`` on that interpreter's path,
mirroring ``generate.py``'s own ``_generate_pydantic`` guard) and
instantiating the tree-root class with no arguments.

**Known residual, filed rather than worked around out-of-lane.**
``draft_graph.py``'s enum import is hardwired to
``ne.schema.generated.operational`` (``generate.py:250-257``, outside this
unit's ``mayTouch``), so an enum-bearing draft's generated graph module is
not importable as a Python module — this validator therefore only proves the
graph module's ``EDGE_DEFS`` textually (a regex over the generated source),
never imports it. U7 (or a future ``ne-schema`` unit) needs a
``--graph-enum-module`` argument added to ``generate.py`` to lift this.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ne.envbuild import venv_python
from ne.schema import codegen as ne_codegen

from .errors import DraftGenerationError
from .model import CandidateEdge

# Paths relative to the repository root — drift-guarded against
# ``ne.schema.codegen``'s own module-level constants by
# ``tests/test_roundtrip.py::test_codegen_layout_constants_match_ne_schema``,
# so an ``ne-schema`` layout move fails loudly HERE instead of mysteriously.
_CODEGEN_VENV_DIR = Path("packages") / "ne-schema" / ".codegen-venv"
_GENERATE_SCRIPT = Path("packages") / "ne-schema" / "codegen" / "generate.py"

_MANIFEST_NAME = "_draft_manifest.py"
_GRAPH_NAME = "draft_graph.py"
_SCHEMA_STEM = "schema_draft"

_EDGE_DEF_PATTERN = r'EdgeDef\(\s*"([^"]+)",\s*"([^"]+)",\s*"([^"]+)",\s*"([^"]+)"'


@dataclass(frozen=True)
class RoundTripArtifacts:
    """The generated artifact TEXTS proven by :func:`validate_draft_generates`."""

    pydantic_module: str
    json_schema: str
    manifest: str
    graph_module: str


_IMPORT_CHECK_SCRIPT = """
import importlib.util
import json
import sys

module_path, payload_json = sys.argv[1], sys.argv[2]
payload = json.loads(payload_json)
expected_names = payload["expected_names"]
top_class = payload["top_class"]

spec = importlib.util.spec_from_file_location("_ne_schema_bootstrap_draft_module", module_path)
if spec is None or spec.loader is None:
    raise ImportError(f"cannot load module spec from {module_path!r}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

for name in expected_names:
    getattr(module, name)

cls = getattr(module, top_class)
cls()
"""


def _check_generated_module_imports(
    module_path: Path, *, expected_names: Sequence[str], top_class: str
) -> None:
    """Import ``module_path`` in a FRESH platform-python subprocess and prove it runs.

    Uses ``sys.executable`` (the PLATFORM interpreter, never the codegen
    venv's) — proving the generated module has no ``linkml_runtime`` (or any
    other codegen-venv-only) dependency, exactly what
    ``generate.py:73-77``'s own in-venv guard checks, but proven a second
    time from the consuming side. Raises :class:`DraftGenerationError` on any
    failure (syntax error, missing name, instantiation error) — the
    subprocess's stderr is the only diagnostic carried, truncated by the
    error class itself.
    """
    payload = json.dumps({"expected_names": list(expected_names), "top_class": top_class})
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_CHECK_SCRIPT, str(module_path), payload],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise DraftGenerationError(proc.returncode, proc.stderr)


def _verify_edge_defs(graph_text: str, expected_edges: Sequence[CandidateEdge]) -> None:
    found = set(re.findall(_EDGE_DEF_PATTERN, graph_text))
    expected = {(e.name.upper(), e.source_class, e.name, e.target_class) for e in expected_edges}
    if found != expected:
        missing = sorted(expected - found)
        extra = sorted(found - expected)
        raise DraftGenerationError(
            -1,
            f"draft_graph.py EDGE_DEFS mismatch: missing={missing!r} extra={extra!r}",
        )


def validate_draft_generates(
    yaml_text: str,
    *,
    repo_root: Path,
    top_class: str,
    expected_classes: Sequence[str],
    expected_enums: Sequence[str],
    expected_edges: Sequence[CandidateEdge] | None = None,
) -> RoundTripArtifacts:
    """PROVE ``yaml_text`` generates through the REAL isolated codegen venv.

    Inherits ``ne.schema.codegen.ensure_env``'s AIR_GAP fail-closed posture
    unchanged (called first, before any subprocess/tempfile work below — see
    the module docstring). Beyond a zero exit code, this function proves the
    draft ACTUALLY generates by: (1) confirming all four artifacts exist;
    (2) importing the generated Pydantic module in a fresh platform-python
    subprocess and instantiating ``top_class`` with no arguments; (3) parsing
    the JSON Schema and confirming ``top_class`` is a defined class; (4) when
    ``expected_edges`` is given, textually verifying ``draft_graph.py``'s
    ``EDGE_DEFS`` match exactly (see the module docstring for why the graph
    module itself is never imported).
    """
    ne_codegen.ensure_env(repo_root)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        schema_path = tmp_path / f"{_SCHEMA_STEM}.yaml"
        schema_path.write_text(yaml_text, encoding="utf-8")
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        cmd = [
            str(venv_python(repo_root / _CODEGEN_VENV_DIR)),
            str(repo_root / _GENERATE_SCRIPT),
            "--schema",
            str(schema_path),
            "--out-dir",
            str(out_dir),
            "--top-class",
            top_class,
            "--manifest-name",
            _MANIFEST_NAME,
            "--graph-name",
            _GRAPH_NAME,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        if proc.returncode != 0:
            raise DraftGenerationError(proc.returncode, proc.stderr)

        module_path = out_dir / f"{_SCHEMA_STEM}.py"
        json_schema_path = out_dir / f"{_SCHEMA_STEM}.schema.json"
        manifest_path = out_dir / _MANIFEST_NAME
        graph_path = out_dir / _GRAPH_NAME

        expected_artifacts = (module_path, json_schema_path, manifest_path, graph_path)
        missing = [p.name for p in expected_artifacts if not p.is_file()]
        if missing:
            raise DraftGenerationError(
                -1, f"generation exited 0 but artifacts missing: {missing!r}"
            )

        _check_generated_module_imports(
            module_path,
            expected_names=[*expected_classes, *expected_enums, "OperationalEntity"],
            top_class=top_class,
        )

        json_schema_text = json_schema_path.read_text(encoding="utf-8")
        parsed_schema = json.loads(json_schema_text)
        defs = parsed_schema.get("$defs", {})
        if top_class not in defs:
            raise DraftGenerationError(
                -1, f"top_class {top_class!r} not found in generated JSON Schema $defs"
            )

        graph_text = graph_path.read_text(encoding="utf-8")
        if expected_edges is not None:
            _verify_edge_defs(graph_text, expected_edges)

        return RoundTripArtifacts(
            pydantic_module=module_path.read_text(encoding="utf-8"),
            json_schema=json_schema_text,
            manifest=manifest_path.read_text(encoding="utf-8"),
            graph_module=graph_text,
        )


__all__ = ["RoundTripArtifacts", "validate_draft_generates"]
