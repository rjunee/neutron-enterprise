# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed gate for the image-scoped OpenVEX ledger (SPEC 6.1.1 ruling, 2026-09-20).

The required ``vuln-scan`` job scans the BUILT ``ne-postgres-age`` image with
Grype. Any ``not_affected`` / ``vulnerable_code_not_in_execute_path`` statement
in ``tools/supply-chain/vex/ne-postgres-age.openvex.json`` (empty today) must be
an ENFORCED claim instead of an assertion, and this gate is what enforces it: it
runs in the same job, BEFORE the ``--vex``'d scan, over the raw un-VEX'd Grype
JSON and a ``docker export`` of the same image.

Five checks, each with its own message prefix and its own failing test in
``tools/supply-chain/tests/test_image_vex_gate.py``:

(a) ``IMAGE-VEX probe:``     the set of regular files in the export whose bytes
    contain one of a vulnerability's reachability needles must EQUAL that
    vulnerability's ``allowed_files``. An extra file fails
    (``test_probe_needle_in_a_second_file_fails``) and a missing one fails
    (``test_probe_missing_libc_fails``) -- the second direction is the
    probe-can-see control: a probe that cannot find the symbol where it is
    known to live is broken, not clean.
(b) ``IMAGE-VEX exactness:`` every raw HIGH-and-above Grype finding that HAS a
    fix (``fix.state == "fixed"``, the predicate Grype's own ``--only-fixed``
    uses) must be in the ledger's ``(vulnerability.name, product @id)`` set, so
    an unledgered fixable finding fails
    (``test_high_finding_without_a_statement_fails``). A HIGH-and-above finding
    with NO fix is report-only: it is printed with the ``IMAGE-VEX report-only:``
    prefix and does not fail the gate
    (``test_unfixed_unledgered_finding_is_report_only``). In the other direction
    every ledger pair must match a raw finding, fixed or not, so a stale
    statement fails (``test_stale_statement_with_no_matching_finding_fails``).
    Aliases are never read (``test_aliases_never_satisfy_exactness``).
(c) ``IMAGE-VEX fix:``       any VEX'd finding Grype reports with
    ``fix.state == "fixed"`` fails, so the statement dies the day Wolfi ships a
    fix (``test_fix_state_fixed_on_a_vexed_finding_fails``).
(d) ``IMAGE-VEX bijection:`` ledger statement names and reachability keys match
    one to one (``test_ledger_reachability_bijection_fails_in_both_directions``).
(e) ``IMAGE-VEX shape:``     every statement is ``not_affected`` /
    ``vulnerable_code_not_in_execute_path``, carries a non-blank
    ``impact_statement``, and names exactly one version-pinned ``pkg:apk/``
    product (``test_statement_shape_rejections``).

Policy (2026-11-06): the gate fails only on HIGH/CRITICAL findings that HAVE a
fix available. Unfixed ones stay visible: this gate prints them, and the
workflow's report-only pass writes them to the job summary and the
``vuln-reports`` artifact.

Exit codes:
  0 -- all five checks pass (report-only lines may still be printed).
  1 -- at least one check reports a violation (every violation is printed).
  2 -- an input is missing, empty, unparseable, or malformed (fail closed).
       Unlike ``vex_dualkey_gate.py`` there is NO lenient empty-ledger mode:
       this gate only ever runs in a job that HAS an image ledger, so an absent
       one is a defect, not a repo state (``test_malformed_inputs_exit_2``).

Residuals this gate does NOT close, stated rather than implied:
  * A stripped, STATICALLY LINKED caller of the vulnerable symbol carries no
    symbol name, so the byte probe cannot see it. The mitigation is upstream
    source evidence, recorded in the statement's ``impact_statement``, not this
    code; ``test_committed_impact_statement_mentions_the_static_link_residual``
    only checks that the wording is present.
  * Needle bytes inside a COMPRESSED member of the export are not searched --
    the probe reads member bytes verbatim and never decompresses them.
  * Symlinks, directories and device nodes are skipped; hard links are read
    (``test_probe_skips_symlinks_and_directories_but_counts_hard_links``).

Stdlib only (argparse, json, sys, tarfile, dataclasses, pathlib) so the whole
matching surface is exercised by fast fixture tests without Docker or a scanner.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

# Grype severities are title-case in its JSON ("High", "Critical"); compared via
# .casefold() so any casing the scanner emits is handled, mirroring the
# workflow's own `--fail-on high` = high-and-above semantics.
_HIGH_AND_ABOVE = {"high", "critical"}

_REQUIRED_STATUS = "not_affected"
_REQUIRED_JUSTIFICATION = "vulnerable_code_not_in_execute_path"
_REQUIRED_PRODUCT_PREFIX = "pkg:apk/"

_REACH_KEYS = frozenset({"needles", "allowed_files"})


class GateError(Exception):
    """An input has an unexpected/invalid shape and cannot be trusted.

    Raised for missing files, unparseable JSON, wrong top-level types, and
    missing required fields. Always fail closed: a malformed input is never
    silently read as "nothing to check".
    """


@dataclass(frozen=True)
class Statement:
    """One OpenVEX statement, flattened to what the five checks read."""

    index: int
    name: str
    products: tuple[str, ...]
    status: str
    justification: str
    impact_statement: str


@dataclass(frozen=True)
class Finding:
    """One HIGH-and-above Grype match on the built image.

    ``purl`` is load-bearing here, unlike in ``vex_dualkey_gate.py``: the image
    ledger keys on ``(id, purl)`` so a statement pinned to one package version
    cannot silence the same id on another.
    """

    vuln_id: str
    purl: str
    fix_state: str
    fix_versions: tuple[str, ...]


@dataclass(frozen=True)
class Reach:
    """The reachability claim for one vulnerability id."""

    needles: tuple[bytes, ...]
    allowed_files: frozenset[str]


def _load_json(path: Path, what: str) -> object:
    """Read and parse a JSON file, or raise :class:`GateError` naming it.

    A zero-size file is a GateError, not an empty document: every input this
    gate takes is produced by a step that must have run.
    """
    if not path.is_file():
        raise GateError(f"{what} file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateError(f"{what} file unreadable ({path}): {exc}") from exc
    if not text.strip():
        raise GateError(f"{what} file is empty: {path}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GateError(f"{what} file is not valid JSON ({path}): {exc}") from exc


def parse_ledger(doc: object) -> list[Statement]:
    """Flatten an OpenVEX document into :class:`Statement` records.

    ``aliases`` is deliberately never read: exactness keys on the exact
    scanner-reported id, the same discipline ``vex_dualkey_gate.py`` enforces
    for the directory-scan ledger.
    """
    if not isinstance(doc, dict):
        raise GateError("VEX ledger is not a JSON object")
    statements = doc.get("statements")
    if not isinstance(statements, list):
        raise GateError("VEX ledger 'statements' is missing or not a list")

    parsed: list[Statement] = []
    for index, statement in enumerate(statements):
        if not isinstance(statement, dict):
            raise GateError(f"VEX ledger statement {index} is not an object")
        vulnerability = statement.get("vulnerability")
        if not isinstance(vulnerability, dict):
            raise GateError(f"VEX ledger statement {index} missing 'vulnerability' object")
        name = vulnerability.get("name")
        if not isinstance(name, str) or not name:
            raise GateError(f"VEX ledger statement {index} has no non-empty vulnerability.name")
        products_raw = statement.get("products")
        if not isinstance(products_raw, list):
            raise GateError(f"VEX ledger statement {index} ({name}) 'products' is not a list")
        products: list[str] = []
        for product in products_raw:
            if not isinstance(product, dict):
                raise GateError(f"VEX ledger statement {index} ({name}) has a non-object product")
            product_id = product.get("@id")
            if not isinstance(product_id, str) or not product_id:
                raise GateError(
                    f"VEX ledger statement {index} ({name}) has a product with no string '@id'"
                )
            products.append(product_id)
        parsed.append(
            Statement(
                index=index,
                name=name,
                products=tuple(products),
                status=_optional_str(statement.get("status")),
                justification=_optional_str(statement.get("justification")),
                impact_statement=_optional_str(statement.get("impact_statement")),
            )
        )
    return parsed


def _optional_str(value: object) -> str:
    """Read a shape-checked field as text; anything non-string reads as absent.

    Absent and wrong-typed both become ``""`` so check (e) reports them as a
    shape violation (exit 1) rather than a parse failure (exit 2) -- a ledger
    whose statement says ``status: 42`` is a bad claim, not an unreadable file.
    """
    return value if isinstance(value, str) else ""


def parse_grype_high(doc: object) -> list[Finding]:
    """Extract the HIGH-and-above matches from a raw (un-VEX'd) Grype report.

    Every kept match must carry a non-empty ``artifact.purl`` and a ``fix``
    object. ``fix.state`` may be the EMPTY STRING -- that is what Grype 0.115.0
    actually emits for a Wolfi package with no fix (``{"versions": [], "state":
    ""}``), so treating it as malformed would make the gate unable to run on its
    own real input.
    """
    if not isinstance(doc, dict):
        raise GateError("grype report is not a JSON object")
    matches = doc.get("matches")
    if not isinstance(matches, list):
        raise GateError("grype report 'matches' is missing or not a list")

    findings: list[Finding] = []
    for match in matches:
        if not isinstance(match, dict):
            raise GateError("grype report contains a non-object match entry")
        vulnerability = match.get("vulnerability")
        if not isinstance(vulnerability, dict):
            raise GateError("grype match missing 'vulnerability' object")
        vuln_id = vulnerability.get("id")
        severity = vulnerability.get("severity")
        if not isinstance(vuln_id, str) or not vuln_id:
            raise GateError("grype match missing non-empty vulnerability.id")
        if not isinstance(severity, str) or not severity:
            raise GateError(f"grype match {vuln_id} missing non-empty vulnerability.severity")
        if severity.casefold() not in _HIGH_AND_ABOVE:
            continue

        artifact = match.get("artifact")
        purl = artifact.get("purl") if isinstance(artifact, dict) else None
        if not isinstance(purl, str) or not purl:
            raise GateError(f"grype match {vuln_id} missing non-empty artifact.purl")
        fix = vulnerability.get("fix")
        if not isinstance(fix, dict):
            raise GateError(f"grype match {vuln_id} missing 'vulnerability.fix' object")
        fix_state = fix.get("state")
        if not isinstance(fix_state, str):
            raise GateError(f"grype match {vuln_id} has a non-string vulnerability.fix.state")
        fix_versions = fix.get("versions")
        if not isinstance(fix_versions, list):
            raise GateError(f"grype match {vuln_id} has a non-list vulnerability.fix.versions")
        findings.append(
            Finding(
                vuln_id=vuln_id,
                purl=purl,
                fix_state=fix_state,
                fix_versions=tuple(str(version) for version in fix_versions),
            )
        )
    return findings


def parse_reachability(doc: object) -> dict[str, Reach]:
    """Parse the reachability map: vulnerability id -> ``{needles, allowed_files}``.

    The key set of each entry must be EXACTLY ``{needles, allowed_files}`` so a
    typo'd or invented key (``allowed_file``, ``exclude``) fails closed instead
    of being ignored. ``allowed_files`` entries are export-relative: an absolute
    path, a ``./`` prefix or a ``..`` segment can never match a normalised
    member name and is rejected rather than left silently unmatchable.
    """
    if not isinstance(doc, dict):
        raise GateError("reachability map is not a JSON object")

    parsed: dict[str, Reach] = {}
    for key, entry in doc.items():
        if not isinstance(key, str) or not key:
            raise GateError("reachability map has a non-string or empty vulnerability id")
        if not isinstance(entry, dict):
            raise GateError(f"reachability entry {key} is not an object")
        if set(entry) != _REACH_KEYS:
            raise GateError(
                f"reachability entry {key} must have exactly the keys "
                f"{sorted(_REACH_KEYS)}, got {sorted(entry)}"
            )
        needles = _string_list(entry.get("needles"), key, "needles")
        allowed = _string_list(entry.get("allowed_files"), key, "allowed_files")
        for path in allowed:
            if path.startswith("/") or path.startswith("./") or ".." in path.split("/"):
                raise GateError(
                    f"reachability entry {key} allowed_files must be export-relative "
                    f"member names without '..' segments: {path}"
                )
        parsed[key] = Reach(
            needles=tuple(needle.encode("utf-8") for needle in needles),
            allowed_files=frozenset(allowed),
        )
    return parsed


def _string_list(value: object, key: str, field: str) -> list[str]:
    """Validate a non-empty list of unique, non-empty strings."""
    if not isinstance(value, list) or not value:
        raise GateError(f"reachability entry {key} '{field}' is not a non-empty list")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise GateError(f"reachability entry {key} '{field}' has a non-string or empty item")
        items.append(item)
    if len(set(items)) != len(items):
        raise GateError(f"reachability entry {key} '{field}' has duplicate items")
    return items


def shape_violations(statements: list[Statement]) -> list[str]:
    """Check (e): every statement is a version-pinned apk ``not_affected`` claim."""
    violations: list[str] = []
    seen: set[tuple[str, str]] = set()
    for statement in statements:
        reasons: list[str] = []
        if statement.status != _REQUIRED_STATUS:
            reasons.append(f"status must be {_REQUIRED_STATUS!r}, got {statement.status!r}")
        if statement.justification != _REQUIRED_JUSTIFICATION:
            reasons.append(
                f"justification must be {_REQUIRED_JUSTIFICATION!r}, "
                f"got {statement.justification!r}"
            )
        if not statement.impact_statement.strip():
            reasons.append("impact_statement is missing or blank")
        if len(statement.products) != 1:
            reasons.append(f"expected exactly one product, got {len(statement.products)}")
        for product in statement.products:
            if not product.startswith(_REQUIRED_PRODUCT_PREFIX):
                reasons.append(
                    f"product must be an apk package purl starting {_REQUIRED_PRODUCT_PREFIX!r}: "
                    f"{product}"
                )
            elif not _is_version_pinned(product):
                reasons.append(f"product must be version-pinned (name@version): {product}")
            if (statement.name, product) in seen:
                reasons.append(f"duplicate (vulnerability, product) pair: {product}")
            seen.add((statement.name, product))
        for reason in reasons:
            violations.append(
                f"IMAGE-VEX shape: statement {statement.index} ({statement.name}): {reason}"
            )
    return violations


def _is_version_pinned(product: str) -> bool:
    """True when the purl carries a non-empty ``@version`` outside qualifiers.

    The version lives before the ``?qualifiers`` and ``#subpath`` segments, so
    an ``@`` that only appears inside a qualifier value does not count.
    """
    core = product.split("#", 1)[0].split("?", 1)[0]
    name, separator, version = core.partition("@")
    return bool(separator) and bool(name) and bool(version)


def has_fix(finding: Finding) -> bool:
    """True when Grype reports a fix: the same predicate as ``grype --only-fixed``."""
    return finding.fix_state.casefold() == "fixed"


def _ledgered(statements: list[Statement]) -> set[tuple[str, str]]:
    return {(statement.name, product) for statement in statements for product in statement.products}


def report_only_findings(findings: list[Finding], statements: list[Statement]) -> list[str]:
    """Unledgered HIGH+ findings with NO fix: listed in the log, never a violation."""
    ledgered = _ledgered(statements)
    pairs = {
        (finding.vuln_id, finding.purl)
        for finding in findings
        if not has_fix(finding) and (finding.vuln_id, finding.purl) not in ledgered
    }
    return [
        f"IMAGE-VEX report-only: unfixed HIGH+ finding {vuln_id} {purl} (no fix available)"
        for vuln_id, purl in sorted(pairs)
    ]


def exactness_violations(findings: list[Finding], statements: list[Statement]) -> list[str]:
    """Check (b): every fixable raw HIGH+ ``(id, purl)`` is ledgered; no ledger pair is stale.

    Unfixed findings are exempt from the first direction (they are report-only,
    see :func:`report_only_findings`) but still keep a statement alive in the
    second.
    """
    found = {(finding.vuln_id, finding.purl) for finding in findings}
    fixable = {(finding.vuln_id, finding.purl) for finding in findings if has_fix(finding)}
    ledgered = _ledgered(statements)
    violations = [
        f"IMAGE-VEX exactness: unledgered HIGH+ finding {vuln_id} {purl} (fix available)"
        for vuln_id, purl in sorted(fixable - ledgered)
    ]
    violations.extend(
        f"IMAGE-VEX exactness: stale statement {name} {product}"
        for name, product in sorted(ledgered - found)
    )
    return violations


def fix_violations(findings: list[Finding], statements: list[Statement]) -> list[str]:
    """Check (c): a VEX'd finding Grype reports as fixed must break the build."""
    ledgered = _ledgered(statements)
    return [
        f"IMAGE-VEX fix: {finding.vuln_id} {finding.purl} has fix.state=fixed"
        for finding in findings
        if (finding.vuln_id, finding.purl) in ledgered and has_fix(finding)
    ]


def bijection_violations(statements: list[Statement], reach: dict[str, Reach]) -> list[str]:
    """Check (d): ledger names and reachability keys match one to one."""
    names = {statement.name for statement in statements}
    keys = set(reach)
    violations = [
        f"IMAGE-VEX bijection: no reachability entry for {name}" for name in sorted(names - keys)
    ]
    violations.extend(
        f"IMAGE-VEX bijection: no ledger statement for {key}" for key in sorted(keys - names)
    )
    return violations


def scan_export(path: Path, needles: set[bytes]) -> dict[bytes, set[str]]:
    """Single pass over a ``docker export`` tar: needle -> set of member names.

    Only regular files and hard links are read; symlinks, directories and device
    nodes are skipped, so an alias that is merely a pointer never counts as a
    second copy of the symbol. Member names are normalised by stripping repeated
    leading ``./`` and any leading ``/``, because ``docker export`` has emitted
    both bare and ``./``-prefixed names across versions.
    """
    hits: dict[bytes, set[str]] = {needle: set() for needle in needles}
    try:
        with tarfile.open(path, mode="r:") as archive:
            for member in archive:
                if not (member.isreg() or member.islnk()):
                    continue
                try:
                    handle = archive.extractfile(member)
                except KeyError as exc:  # hard link whose target is not in the archive
                    raise GateError(f"export member {member.name} is unreadable: {exc}") from exc
                if handle is None:
                    raise GateError(f"export member {member.name} has no readable content")
                with handle:
                    blob = handle.read()
                name = _normalise_member(member.name)
                for needle in needles:
                    if needle in blob:
                        hits[needle].add(name)
    except tarfile.TarError as exc:
        raise GateError(f"image export is not a readable tar ({path}): {exc}") from exc
    except OSError as exc:
        raise GateError(f"image export unreadable ({path}): {exc}") from exc
    return hits


def _normalise_member(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name.lstrip("/")


def probe_violations(export: Path, reach: dict[str, Reach]) -> list[str]:
    """Check (a): hit set must EQUAL ``allowed_files``, in both directions."""
    needles: set[bytes] = set()
    for entry in reach.values():
        needles.update(entry.needles)
    hits_by_needle = scan_export(export, needles)

    violations: list[str] = []
    for vuln_id in sorted(reach):
        entry = reach[vuln_id]
        hits: set[str] = set()
        for needle in entry.needles:
            hits |= hits_by_needle.get(needle, set())
        violations.extend(
            f"IMAGE-VEX probe: {vuln_id}: needle found outside allowed_files: {path}"
            for path in sorted(hits - entry.allowed_files)
        )
        violations.extend(
            f"IMAGE-VEX probe: {vuln_id}: allowed file absent from the export or "
            f"without any needle: {path}"
            for path in sorted(entry.allowed_files - hits)
        )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed gate for the image-scoped OpenVEX ledger: proves the "
            "reachability claim against a docker export of the built image, and "
            "proves the ledger covers every fixable raw HIGH+ Grype finding."
        )
    )
    parser.add_argument(
        "--export", type=Path, required=True, help="Path to `docker export` tar of the image."
    )
    parser.add_argument(
        "--grype", type=Path, required=True, help="Path to the RAW (un-VEX'd) Grype JSON report."
    )
    parser.add_argument(
        "--vex",
        type=Path,
        required=True,
        help="Path to tools/supply-chain/vex/ne-postgres-age.openvex.json.",
    )
    parser.add_argument(
        "--reachability",
        type=Path,
        required=True,
        help="Path to tools/supply-chain/vex/ne-postgres-age.reachability.json.",
    )
    args = parser.parse_args(argv)

    try:
        statements = parse_ledger(_load_json(args.vex, "VEX ledger"))
        reach = parse_reachability(_load_json(args.reachability, "reachability map"))
        findings = parse_grype_high(_load_json(args.grype, "grype report"))
        violations = probe_violations(args.export, reach)
    except GateError as exc:
        print(f"IMAGE-VEX ERROR: {exc}", file=sys.stderr)
        return 2

    for line in report_only_findings(findings, statements):
        print(line)
    violations.extend(exactness_violations(findings, statements))
    violations.extend(fix_violations(findings, statements))
    violations.extend(bijection_violations(statements, reach))
    violations.extend(shape_violations(statements))

    if violations:
        for violation in violations:
            print(violation)
        print(f"IMAGE-VEX: {len(violations)} violation(s); the image VEX ledger is not honest.")
        return 1

    unfixed = sum(1 for finding in findings if not has_fix(finding))
    print(
        f"image VEX gate: {len(statements)} statement(s) exactly cover "
        f"{len(findings)} HIGH+ finding(s) ({unfixed} without a fix); reachability probe, "
        "fix state, bijection and statement shape all clean."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
