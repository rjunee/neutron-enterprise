# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Platform-side driver for ne-schema codegen.

This module imports NOTHING from linkml. linkml (and its GPL/CC0/W3C-licensed
transitive closure) is confined to an ISOLATED, hash-pinned virtualenv at
``packages/ne-schema/.codegen-venv`` so it never enters the platform lock/SBOM
(SPEC §2 C4). Codegen therefore runs the in-venv ``codegen/generate.py`` as a
subprocess; this module only manages that venv and compares artifacts.

Public API:
  * ``ensure_env(root)``     — build/refresh the isolated codegen venv.
  * ``generate(root)``       — regenerate the committed artifacts in place.
  * ``check_freshness(root)``— list committed artifacts that are stale vs source.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ne.envbuild import ensure_sidecar_venv, marker_path, requirements_sha256, venv_python
from ne.net.airgap import AirGapViolationError, air_gap_engaged

# Paths relative to the repository root.
_PKG = Path("packages") / "ne-schema"
_SCHEMAS_DIR = _PKG / "src" / "ne" / "schema" / "schemas"
_GENERATED_DIR = _PKG / "src" / "ne" / "schema" / "generated"
_CODEGEN_DIR = _PKG / "codegen"
_VENV_DIR = _PKG / ".codegen-venv"


@dataclass(frozen=True)
class SchemaSpec:
    """One LinkML source in the codegen registry.

    ``name`` is the source stem (``schemas/<name>.yaml``) and the generated
    ``<name>.py`` / ``<name>.schema.json`` stem; ``top_class`` is the JSON Schema
    tree root; ``manifest_filename`` is the per-schema version manifest module.
    """

    name: str
    top_class: str
    manifest_filename: str
    graph_filename: str | None = None
    shacl_filename: str | None = None


# The schema registry. Each entry generates a Pydantic module, a JSON Schema, and
# a version manifest. Adding a schema here is all that is needed to enrol it in the
# codegen + freshness pipeline (its artifacts flow into ARTIFACTS automatically).
# ``graph_filename`` (NE-081) enrols the additive Tier-2 entity-graph contracts
# artifact for a schema; ``None`` (events) leaves that schema graph-free.
# ``shacl_filename`` (NE-085a) enrols the additive Tier-3 SHACL shapes artifact
# for a schema; ``None`` (events) leaves that schema shape-free. It is additive:
# the pre-existing operational artifacts (Pydantic/JSON Schema/manifest/graph) are
# byte-unchanged — only a new committed ``operational.shapes.ttl`` is produced.
SCHEMAS: tuple[SchemaSpec, ...] = (
    SchemaSpec(
        "operational",
        "OperationalRecordSet",
        "_manifest.py",
        "operational_graph.py",
        shacl_filename="operational.shapes.ttl",
    ),
    SchemaSpec("events", "EventRecordSet", "_events_manifest.py"),
)


def _artifacts() -> tuple[str, ...]:
    names: list[str] = []
    for spec in SCHEMAS:
        names.extend((f"{spec.name}.py", f"{spec.name}.schema.json", spec.manifest_filename))
        if spec.graph_filename is not None:
            names.append(spec.graph_filename)
        if spec.shacl_filename is not None:
            names.append(spec.shacl_filename)
    return tuple(names)


# Generated artifact filenames (the reproducible output set across all schemas).
ARTIFACTS: tuple[str, ...] = _artifacts()

_REQUIREMENTS = _CODEGEN_DIR / "requirements.txt"
_OVERRIDES = _CODEGEN_DIR / "overrides.txt"
_GENERATE_SCRIPT = _CODEGEN_DIR / "generate.py"
# Marker recording the requirements.txt sha256 the venv was built from.
_ENV_MARKER = ".requirements.sha256"


def schema_source(root: Path, name: str = "operational") -> Path:
    """Absolute path to a registered LinkML schema source."""
    return root / _SCHEMAS_DIR / f"{name}.yaml"


def generated_dir(root: Path) -> Path:
    """Absolute path to the committed generated-artifacts directory."""
    return root / _GENERATED_DIR


def _venv_python(root: Path) -> Path:
    return venv_python(root / _VENV_DIR)


def _marker_path(root: Path) -> Path:
    return marker_path(root / _VENV_DIR)


def _requirements_sha256(root: Path) -> str:
    return requirements_sha256(root / _CODEGEN_DIR)


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(cmd, check=True, env=env)


def _air_gap_check(venv_dir: Path, name: str) -> Callable[[], None]:
    """Build the fail-closed AIR_GAP hook for :func:`ensure_sidecar_venv`.

    The returned closure is passed as ``pre_lock_check`` so it fires AFTER the
    unlocked up-to-date fast-path but BEFORE any mkdir/lock/rmtree/subprocess.
    Reaching it means the codegen venv is absent or stale, which would trigger a
    ``uv pip install`` egress to pypi.org/files.pythonhosted.org — forbidden under
    AIR_GAP. ``air_gap_engaged()`` is read at CALL TIME (inside the closure) so
    tests can toggle it via ``monkeypatch.setenv``.
    """

    def _check() -> None:
        if air_gap_engaged():
            raise AirGapViolationError(
                f"AIR_GAP is engaged and {name} at {venv_dir} is missing or stale; "
                "runtime uv pip install egress (pypi.org/files.pythonhosted.org) is "
                "forbidden — pre-build the venv at image build/pre-staging time."
            )

    return _check


def ensure_env(root: Path) -> None:
    """Create/refresh the isolated hash-pinned codegen venv if needed — fail-closed
    under AIR_GAP.

    The venv is (re)built when it is missing or when a stored marker of
    requirements.txt no longer matches the committed file, so a requirements
    change forces a clean reinstall. Installation is ``--require-hashes`` with
    the GPL-eliminating override applied (SPEC §2 C4/C11).

    NE-CKC8: the probe/lock/rebuild flow now lives in
    :func:`ne.envbuild.ensure_sidecar_venv`, shared with the docling and presidio
    builders. This is a BEHAVIOR UPGRADE for codegen — it gains the cross-process/
    thread filelock serialisation and atomic-marker write it never had (two
    concurrent ``ne.schema.codegen`` runs, e.g. ``generate`` + ``check_freshness``
    in parallel CI shards, previously raced the rmtree/install). ``run=_run``
    resolves the module global at call time so monkeypatching still works.

    NE-CKC10 (P2-23): an ABSENT or STALE venv under AIR_GAP is refused with a typed
    :class:`ne.net.airgap.AirGapViolationError` BEFORE any lock/mkdir/rmtree/
    subprocess (the ``pre_lock_check`` hook) — runtime PyPI egress from inside a
    customer VPC is forbidden. A CURRENT venv stays a silent fast-path no-op, and
    with AIR_GAP unset the builder behaves exactly as before (the guard only ever
    ADDS restriction).
    """
    ensure_sidecar_venv(
        env_dir=root / _CODEGEN_DIR,
        venv_dir=root / _VENV_DIR,
        install_args=("--overrides", str(root / _OVERRIDES)),
        pre_lock_check=_air_gap_check(root / _VENV_DIR, "the isolated linkml codegen venv"),
        name="the isolated linkml codegen venv",
        run=_run,
    )


def _run_generator(root: Path, out_dir: Path) -> None:
    ensure_env(root)
    for spec in SCHEMAS:
        cmd = [
            str(_venv_python(root)),
            str(root / _GENERATE_SCRIPT),
            "--schema",
            str(schema_source(root, spec.name)),
            "--out-dir",
            str(out_dir),
            "--top-class",
            spec.top_class,
            "--manifest-name",
            spec.manifest_filename,
        ]
        if spec.graph_filename is not None:
            cmd.extend(("--graph-name", spec.graph_filename))
        if spec.shacl_filename is not None:
            cmd.extend(("--shacl-name", spec.shacl_filename))
        _run(cmd)


def generate(root: Path, out_dir: Path | None = None) -> Path:
    """Regenerate artifacts. With no out_dir, writes the committed generated dir."""
    target = out_dir if out_dir is not None else generated_dir(root)
    _run_generator(root, target)
    return target


def check_freshness(root: Path) -> list[str]:
    """Return committed artifact names that differ from a fresh generation.

    Generates into a temp dir and byte-compares each artifact against the
    committed copy. An empty list means the committed tree is up to date.
    """
    committed = generated_dir(root)
    stale: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp)
        _run_generator(root, fresh)
        for name in ARTIFACTS:
            committed_path = committed / name
            fresh_path = fresh / name
            if not committed_path.is_file():
                stale.append(name)
                continue
            if committed_path.read_bytes() != fresh_path.read_bytes():
                stale.append(name)
    return stale


def main(argv: list[str] | None = None) -> int:
    """Tiny CLI shim so codegen can also run as ``python -m ne.schema.codegen``."""
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path.cwd()
    if args and args[0] == "check":
        stale = check_freshness(root)
        if stale:
            for name in stale:
                print(f"stale: {name}")
            return 1
        return 0
    generate(root)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
