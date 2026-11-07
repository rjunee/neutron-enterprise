# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Vendored-ontology corpus loader with C2 no-egress guards (NE-085-spike).

The BFO-2020 + CCO-2.1 corpus is vendored in-repo under ``assets/ontologies/``
and described by ``MANIFEST.toml`` (C11 pins: upstream repo/commit, per-file
sha256, exact triple count). This module parses that manifest (stdlib
``tomllib`` — no third-party TOML), verifies the vendored bytes against the
pinned digests, and yields the corpus as ``(graph_iri, path, bytes)`` for the
in-process store.

C2 hard guards:
  * every file path is resolved and asserted to be WITHIN the package
    ``assets/`` dir before it is opened (path-traversal / symlink escape → raise);
  * this module never fetches over the network and never calls rdflib
    ``parse(location=...)`` / URL parsing — it reads local bytes only.

Asset resolution (NE-085a, tracked Codex P2): the corpus lives INSIDE the
package (``ne/brain_ontology/assets/ontologies/``) and is resolved via
:func:`importlib.resources.files` — never a ``__file__``-parents path that
breaks under a wheel install (the cedar-evaluator / acme / sidecar precedent).
zipimport is unsupported by uv installs, so the resolved traversable is a real
filesystem :class:`~pathlib.Path` and is asserted to be a directory at import.
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict


def _assets_root() -> Path:
    """Resolve ``ne/brain_ontology/assets/ontologies`` as package data.

    Uses :func:`importlib.resources.files` so it works from an installed wheel as
    well as an editable/source checkout (never a ``__file__``-parents path). uv
    installs are directory-based (not zipimport), so the traversable coerces to a
    real :class:`~pathlib.Path`; a non-directory result is a fail-fast packaging
    error, not a silent empty corpus.
    """
    resource = files("ne.brain_ontology").joinpath("assets", "ontologies")
    root = Path(str(resource))
    if not root.is_dir():
        raise RuntimeError(
            f"ne-brain-ontology corpus not resolvable as package data: {root} "
            "(is_dir=False) — the vendored ontologies must ship inside the package"
        )
    return root


ASSETS_DIR = _assets_root().resolve()
DEFAULT_MANIFEST = ASSETS_DIR / "MANIFEST.toml"

# The corpus source keys, in load order (upper ontology first, then the mid
# CCO layer, then the self-authored operational alignment; NE-085b).
_SOURCE_KEYS = ("bfo", "cco", "ne")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class OntologyFile(_Frozen):
    """One vendored TTL artifact, pinned by sha256 + exact triple count."""

    upstream_path: str
    local_path: str
    sha256: str
    triples: int


class OntologySource(_Frozen):
    """A vendored ontology (BFO or CCO): upstream pin + its file set."""

    upstream_repo: str
    pin: str
    license: str
    role: str
    files: tuple[OntologyFile, ...]
    pin_commit: str | None = None
    license_evidence: str | None = None


class OntologyManifest(_Frozen):
    """Parsed ``assets/ontologies/MANIFEST.toml`` + its resolved assets root."""

    assets_root: Path
    sources: dict[str, OntologySource]


class OntologyVersion(_Frozen):
    """In-package ontology version registry (brain-tier3-spec §7.4).

    Surfaced for health checks / ``config_hash`` inputs: the pinned commit (or
    ``pin`` for the self-authored alignment) of every corpus source, the per-file
    sha256s, and the compiled ``shapes_hash`` — one frozen value that changes iff
    the loaded ontology or the shapes change.
    """

    sources: dict[str, str]
    file_sha256: dict[str, str]
    shapes_hash: str


def ontology_version_for(manifest: OntologyManifest, shapes_hash: str) -> OntologyVersion:
    """Build the :class:`OntologyVersion` registry from a manifest + shapes hash."""
    sources: dict[str, str] = {}
    file_sha256: dict[str, str] = {}
    for name, source in manifest.sources.items():
        sources[name] = source.pin_commit or source.pin
        for entry in source.files:
            file_sha256[entry.local_path] = entry.sha256
    return OntologyVersion(sources=sources, file_sha256=file_sha256, shapes_hash=shapes_hash)


def _resolve_within(assets_root: Path, local_path: str) -> Path:
    """Resolve ``local_path`` under ``assets_root``, C2-asserting containment.

    Rejects absolute paths, ``..`` traversal, and symlink escapes: the resolved
    real path MUST live inside the (resolved real) assets root.
    """
    root = assets_root.resolve()
    candidate = (root / local_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"C2 guard: ontology path {local_path!r} escapes the assets root {root}")
    return candidate


def load_manifest(path: Path = DEFAULT_MANIFEST) -> OntologyManifest:
    """Parse the vendored-ontology manifest (stdlib ``tomllib``)."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    sources: dict[str, OntologySource] = {}
    for key in _SOURCE_KEYS:
        if key not in data:
            continue
        sources[key] = OntologySource.model_validate(data[key])
    return OntologyManifest(assets_root=path.parent.resolve(), sources=sources)


def verify_assets(manifest: OntologyManifest) -> None:
    """Verify every vendored file exists and matches its pinned sha256.

    Raises ``ValueError`` on a missing file or a digest mismatch (C11): this is
    the fail-closed integrity gate the walking skeleton runs before loading.
    """
    for source in manifest.sources.values():
        for entry in source.files:
            resolved = _resolve_within(manifest.assets_root, entry.local_path)
            if not resolved.is_file():
                raise ValueError(f"vendored ontology missing: {entry.local_path}")
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            if digest != entry.sha256:
                raise ValueError(
                    f"sha256 mismatch for {entry.local_path}: "
                    f"manifest={entry.sha256} actual={digest}"
                )


def graph_iri_for(source: str, local_path: str) -> str:
    """Deterministic named-graph IRI for a vendored artifact."""
    stem = Path(local_path).stem
    return f"urn:ne:ontology:{source}:{stem}"


def iter_corpus(manifest: OntologyManifest) -> Iterator[tuple[str, Path, bytes]]:
    """Yield ``(graph_iri, path, bytes)`` for every vendored artifact.

    Each path is C2-validated (within the assets root) before it is read; the
    bytes are handed to the store's ``load_turtle_bytes`` — the caller never
    parses from a URL/location.
    """
    for name, source in manifest.sources.items():
        for entry in source.files:
            resolved = _resolve_within(manifest.assets_root, entry.local_path)
            yield graph_iri_for(name, entry.local_path), resolved, resolved.read_bytes()
