# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Bespoke, zero-dependency CycloneDX 1.6 SBOM generator driven by ``uv.lock``.

Same rationale as the bespoke license scanner (``ne.cli.licenses``): the briefed
``cyclonedx-bom`` pulls ``chardet`` (LGPL-2.1+), which our own SPEC §2 C4 gate
rejects — so adopting it would fail the very policy this unit enforces. Instead we
emit CycloneDX from the lock with stdlib ``tomllib``/``json`` only.

We deliberately emit ``specVersion`` **1.6** (not the current 1.7): 1.6 is
tool-free-stable and universally consumed, and CycloneDX 1.7 is documented as
backward-compatible with 1.4-1.6. Output is deterministic (no timestamp, no
dependency graph) **except** the top-level ``serialNumber``, which is a fresh
``urn:uuid`` per generation exactly as CycloneDX intends (it uniquely identifies
*this* BOM document, not the artifact set). See ``docs/security/supply-chain.md``
for the recorded decision.

Reproducible-bundle mode (NE-069): ``ne release bundle`` needs a BYTE-IDENTICAL
SBOM across repeated builds of the same tree (the whole point of a "release
fingerprint"), which a fresh ``uuid4`` breaks by construction. ``build_sbom``
therefore accepts an opt-in ``deterministic_serial`` keyword: when set, the
document is built WITHOUT a ``serialNumber``, canonicalized (sorted keys, tight
separators, ASCII), and a stable ``uuid5`` derived from that canonical form (plus
the target name, for domain separation between members) is used as the
``serialNumber`` instead. This is a distinct namespace from a real CycloneDX BOM
identity, but it makes the document byte-reproducible, which is exactly what a
release bundle's fingerprint requires. The default (no flag) still emits a fresh
``uuid4`` every run — this is what the per-PR ``sbom`` job in
``supply-chain.yml`` uses, where a unique serial per document is exactly what
CycloneDX intends. NE-069R added ``ne release sbom --deterministic``, which
release.yml uses so the uploaded standalone SBOMs are BYTE-IDENTICAL to the
copies inside the release bundle and their sha256 digests match the
manifest-recorded digests — SBOM digest verification is consistent end-to-end by
construction (no digest/signature check is weakened; the two paths simply agree
because they build the same bytes).

License rendering: a component license that is a valid SPDX *expression* is
emitted as ``{"expression": lic}``; anything else (a free-text name like "BSD
License") is emitted as ``{"license": {"name": lic}}`` so the document stays
schema-valid.

Component scope: ``components`` is the TARGET MEMBER's transitive runtime
dependency closure walked from ``uv.lock`` (``[[package]].dependencies`` plus
extras-gated ``[package.optional-dependencies]`` edges), not the whole
workspace's third-party set — ``[package.dev-dependencies]`` is excluded
because dev deps are never shipped. See ``_dependency_closure``.
"""

from __future__ import annotations

import json
import re
import tomllib
import uuid
from pathlib import Path

import click

from ne.cli.checks import _find_repo_root
from ne.cli.licenses import (
    _FIRST_PARTY_LICENSE,
    Policy,
    default_policy_path,
    resolve_license,
)

_FIRST_PARTY_SOURCE_KEYS = ("editable", "virtual", "directory")

_SPDX_OPERAND = re.compile(r"^[A-Za-z0-9.+-]+$")
_SPDX_OPERATORS = frozenset({"AND", "OR", "WITH"})


def _is_spdx_expression(text: str) -> bool:
    """Syntactic-only check that ``text`` is a well-formed SPDX license expression.

    This is NOT a license *validity* check (it does not verify identifiers against
    the official SPDX list) — it only distinguishes an SPDX expression (e.g.
    ``MIT``, ``MIT OR Apache-2.0``, ``GPL-2.0-only WITH Classpath-exception-2.0``,
    ``Apache-2.0``) from a free-text license *name* (e.g. ``BSD
    License``), so the SBOM can pick the right CycloneDX shape. Operands must
    match ``^[A-Za-z0-9.+-]+$``, operators are exactly ``AND``/``OR``/``WITH``
    (uppercase), parentheses must balance, and the expression must end on a
    completed operand.
    """
    if not text or not text.strip():
        return False
    tokens = text.replace("(", " ( ").replace(")", " ) ").split()
    depth = 0
    expect_operand = True
    for tok in tokens:
        if tok == "(":
            if not expect_operand:
                return False
            depth += 1
        elif tok == ")":
            if expect_operand or depth == 0:
                return False
            depth -= 1
            expect_operand = False
        elif expect_operand:
            if not _SPDX_OPERAND.match(tok):
                return False
            expect_operand = False
        else:
            if tok not in _SPDX_OPERATORS:
                return False
            expect_operand = True
    return depth == 0 and not expect_operand


def _is_first_party(pkg: dict) -> bool:
    src = pkg.get("source", {})
    return any(k in src for k in _FIRST_PARTY_SOURCE_KEYS)


def _is_member(pkg: dict) -> bool:
    """A buildable workspace member dist (editable/directory source).

    Excludes ``virtual`` sources — the workspace root ``neutron-enterprise`` and
    any other virtual (non-installable) package are not distributable artifacts.
    """
    src = pkg.get("source", {})
    return "editable" in src or "directory" in src


def _dependency_closure(packages: list[dict], target: str) -> set[str]:
    """Walk uv.lock's dependency graph and return the target's transitive runtime closure.

    Starts at ``target`` (with no extras activated) and follows two edge kinds:

    * ``[[package]].dependencies`` — the package's unconditional runtime deps
      (including marker-conditional ones, e.g. colorama's ``sys_platform ==
      'win32'`` — the shipped artifact can still install them, so they stay
      IN the closure).
    * ``[package.optional-dependencies]`` — only followed for extras that were
      requested by an incoming edge (a dependency dict's own ``extra`` list
      names extras of *that dependency* to activate, feeding that
      dependency's ``optional-dependencies`` table).

    ``[package.dev-dependencies]`` is never consulted — dev deps are not
    shipped and must not appear in a member's SBOM.

    First-party workspace members encountered mid-walk ARE traversed (their
    own deps count toward the closure); it is the caller's component filter
    that excludes first-party names from the emitted component list.
    """
    by_name = {p["name"]: p for p in packages}
    seed: tuple[str, tuple[str, ...]] = (target, ())
    seen: set[tuple[str, tuple[str, ...]]] = {seed}
    worklist: list[tuple[str, tuple[str, ...]]] = [seed]
    closure: set[str] = set()

    while worklist:
        name, extras = worklist.pop()
        pkg = by_name.get(name)
        if pkg is None:
            continue
        edges: list[dict] = list(pkg.get("dependencies", []))
        optional = pkg.get("optional-dependencies", {})
        for extra in extras:
            edges.extend(optional.get(extra, []))
        for dep in edges:
            dep_name = dep["name"]
            dep_extras = tuple(dep.get("extra", []))
            key = (dep_name, dep_extras)
            if key in seen:
                continue
            seen.add(key)
            closure.add(dep_name)
            worklist.append(key)

    return closure


def _sha256s(pkg: dict) -> list[str]:
    """All sha256 hashes on a lock entry (sdist + every wheel), prefix-stripped."""
    hashes: list[str] = []
    sdist = pkg.get("sdist")
    if isinstance(sdist, dict):
        h = sdist.get("hash")
        if isinstance(h, str):
            hashes.append(h)
    for wheel in pkg.get("wheels", []):
        h = wheel.get("hash")
        if isinstance(h, str):
            hashes.append(h)
    stripped = [h[len("sha256:") :] for h in hashes if h.startswith("sha256:")]
    return sorted(set(stripped))


def build_sbom(
    lock_path: Path,
    policy: Policy,
    target: str,
    *,
    deterministic_serial: bool = False,
) -> dict:
    """Build a CycloneDX 1.6 SBOM document for one workspace member. Deterministic.

    ``deterministic_serial=True`` (opt-in) replaces the default fresh-``uuid4``
    ``serialNumber`` with a stable ``uuid5`` derived from the document's own
    canonical content plus ``target`` — see the module docstring's
    "Reproducible-bundle mode" section. The default (``False``) preserves the
    existing ``ne release sbom`` behaviour unchanged.
    """
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages: list[dict] = data.get("package", [])

    member = next(
        (p for p in packages if _is_member(p) and p["name"] == target),
        None,
    )
    if member is None:
        members = sorted(p["name"] for p in packages if _is_member(p))
        raise ValueError(f"unknown package {target!r}; workspace members: " + ", ".join(members))
    tver = member.get("version", "")

    ne_cli = next((p for p in packages if p["name"] == "ne-cli"), None)
    ne_cli_version = ne_cli.get("version", "0") if ne_cli is not None else "0"

    closure = _dependency_closure(packages, target)

    components: list[dict] = []
    for pkg in sorted(
        (p for p in packages if not _is_first_party(p) and p["name"] in closure),
        key=lambda p: p["name"],
    ):
        name = pkg["name"]
        version = pkg.get("version", "")
        purl = f"pkg:pypi/{name}@{version}"
        comp: dict = {
            "type": "library",
            "bom-ref": purl,
            "name": name,
            "version": version,
            "purl": purl,
        }
        lic = resolve_license(name, policy)
        if lic is not None:
            comp["licenses"] = (
                [{"expression": lic}] if _is_spdx_expression(lic) else [{"license": {"name": lic}}]
            )
        hashes = _sha256s(pkg)
        if hashes:
            comp["hashes"] = [{"alg": "SHA-256", "content": h} for h in hashes]
        components.append(comp)

    doc: dict = {
        "$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "tools": {
                "components": [{"type": "application", "name": "ne-cli", "version": ne_cli_version}]
            },
            "component": {
                "type": "application",
                "bom-ref": f"{target}@{tver}",
                "name": target,
                "version": tver,
                "licenses": [{"expression": _FIRST_PARTY_LICENSE}],
            },
        },
        "components": components,
    }

    if deterministic_serial:
        canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        serial = uuid.uuid5(uuid.NAMESPACE_URL, "ne-sbom:" + target + ":" + canonical)
        doc["serialNumber"] = f"urn:uuid:{serial}"
    else:
        doc["serialNumber"] = f"urn:uuid:{uuid.uuid4()}"

    # Re-order so serialNumber renders where CycloneDX documents conventionally
    # place it (cosmetic only — JSON key order carries no semantic meaning, but
    # it keeps a hand-diff of an emitted document readable).
    ordered = {
        "$schema": doc["$schema"],
        "bomFormat": doc["bomFormat"],
        "specVersion": doc["specVersion"],
        "serialNumber": doc["serialNumber"],
        "version": doc["version"],
        "metadata": doc["metadata"],
        "components": doc["components"],
    }
    return ordered


@click.command("sbom")
@click.option("--package", "package", default=None, help="Workspace member dist name.")
@click.option("--all", "all_", is_flag=True, help="Generate an SBOM for every workspace member.")
@click.option(
    "--out",
    "out",
    type=click.Path(path_type=Path),
    default=Path("dist/sbom"),
    show_default=True,
    help="Output directory for the CycloneDX SBOM files.",
)
@click.option(
    "--deterministic",
    "deterministic",
    is_flag=True,
    default=False,
    help=(
        "Use the deterministic content-derived uuid5 serialNumber "
        "(byte-reproducible; matches the release bundle's SBOMs) instead of a "
        "fresh uuid4."
    ),
)
def sbom(package: str | None, all_: bool, out: Path, deterministic: bool) -> None:
    """Generate bespoke stdlib CycloneDX 1.6 SBOM(s) from uv.lock."""
    if (package is None) == (not all_):
        raise click.UsageError("pass exactly one of --package NAME or --all")

    root = _find_repo_root(Path.cwd())
    lock_path = root / "uv.lock"
    policy = Policy.load(default_policy_path(root))

    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages: list[dict] = data.get("package", [])
    if all_:
        targets = sorted(p["name"] for p in packages if _is_member(p))
    else:
        assert package is not None
        targets = [package]

    out_resolved = out if out.is_absolute() else (Path.cwd() / out)
    out_resolved.mkdir(parents=True, exist_ok=True)

    for target in targets:
        try:
            doc = build_sbom(lock_path, policy, target, deterministic_serial=deterministic)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        version = doc["metadata"]["component"]["version"]
        path = out_resolved / f"{target}-{version}.cdx.json"
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    click.echo(f"wrote {len(targets)} CycloneDX 1.6 SBOM(s) to {out_resolved}")
