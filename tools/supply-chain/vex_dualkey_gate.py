# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed VEX dual-scanner-keying gate (invariant-audit Finding E, 2026-07-23).

Finding E: Trivy keys a vulnerability finding by its CVE id when one exists; Grype keys the SAME
vulnerability by its GHSA id (or PYSEC id when no GHSA exists). An OpenVEX
statement (``tools/supply-chain/vex/openvex.json``) suppresses a scanner
finding only when the statement's ``vulnerability.name`` — or one of its
``aliases`` — matches the id the scanner actually reported. Relying on aliases
to bridge that gap proved fragile in practice: the chromadb/json-repair
incident was exactly a case where a VEX statement was keyed by only one
scanner's id format while the two scanners keyed the same vulnerability
differently, and nothing caught it until a human noticed.

This repo's corrective discipline is ONE STATEMENT PER SCANNER-REPORTED ID:
whenever Trivy and Grype key the same vulnerability differently, the ledger
carries a paired statement for each reported id (see the CVE-2026-4372 /
GHSA-29pf-2h5f-8g72 pair in openvex.json as the template). Today that
discipline is applied everywhere, but by manual diligence, not by CI.

This gate makes it a CI-enforced invariant: it takes the RAW (un-VEX'd)
CRITICAL/HIGH findings from a Trivy JSON report and a Grype JSON report, and
asserts that every finding WITH A FIX AVAILABLE has its exact reported id as
some statement's ``vulnerability.name`` in the ledger. A CRITICAL/HIGH finding
with no fix is report-only (policy 2026-11-06: the scans fail only on fixable
HIGH/CRITICAL findings, ``trivy --ignore-unfixed`` / ``grype --only-fixed``): it
is printed with the ``VEX-DUALKEY report-only:`` prefix and never fails the
gate. "Has a fix" uses each scanner's own predicate: a non-empty Trivy
``FixedVersion`` (what ``--ignore-unfixed`` filters on) and Grype
``vulnerability.fix.state == "fixed"`` (what ``--only-fixed`` filters on).
ALIASES ARE DELIBERATELY NEVER COLLECTED as acceptable matches — that is the
entire point of the gate (see ``test_alias_only_statement_fails_the_gate``, the
incident replay, which is the actual lock on this claim, not this docstring).

Exit codes:
  0 — every fixable live finding has a same-scanner-keyed VEX statement.
  1 — at least one fixable live finding lacks a same-scanner-keyed statement.
  2 — a scanner report or the VEX ledger is unreadable/malformed (fail
      closed; distinct from a keying violation).

The parsing/matching logic here is pure and stdlib-only (json, argparse,
dataclasses, pathlib) so it is exercised by fast, fixture-based unit tests in
``tools/supply-chain/tests/test_vex_dualkey_gate.py`` without invoking either
scanner. The live-scanner invocation itself happens in three steps appended
to the ``vuln-scan`` job of ``.github/workflows/supply-chain.yml``, reusing
the vulnerability DBs the job's existing fail-closed ``--vex`` scans already
downloaded.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Trivy severities are upper-case in its JSON ("CRITICAL", "HIGH", ...).
_TRIVY_SEVERITIES = {"CRITICAL", "HIGH"}
# Grype severities are title-case in its JSON ("Critical", "High", ...); we
# compare via .casefold() so any casing scanner code emits is handled, mirroring
# the existing workflow's `--fail-on high` = high-and-above semantics.
_GRYPE_SEVERITIES = {"critical", "high"}


class GateError(Exception):
    """A scanner report or the VEX ledger has an unexpected/invalid shape.

    Raised for anything that means the gate cannot trust its inputs — missing
    required fields, wrong top-level types, unparseable JSON, missing files.
    Always fail closed: never silently treat a malformed input as "no findings".
    """


@dataclass(frozen=True)
class Finding:
    """A single CRITICAL/HIGH vulnerability finding reported by one scanner.

    ``package`` is display context only (a purl or ``name@version``) — it is
    never load-bearing for the matching logic, which keys purely on
    ``vuln_id`` against VEX statement names.
    """

    scanner: str
    vuln_id: str
    package: str
    fixable: bool = True


def _load_json(path: Path, what: str) -> object:
    """Read and parse a JSON file, or raise :class:`GateError` naming it."""
    if not path.is_file():
        raise GateError(f"{what} file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateError(f"{what} file unreadable ({path}): {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GateError(f"{what} file is not valid JSON ({path}): {exc}") from exc


def parse_trivy(doc: object) -> list[Finding]:
    """Extract CRITICAL/HIGH findings from a parsed Trivy JSON report.

    ``doc`` must be a dict carrying ``SchemaVersion`` (proves it is actually a
    Trivy report, not some other JSON blob). ``Results`` is absent/None on a
    clean scan — that yields zero findings, not an error. Each result's
    ``Vulnerabilities`` list is likewise optional (absent/None -> skip). Every
    vulnerability entry that IS present must carry non-empty
    ``VulnerabilityID`` and ``Severity`` strings; a malformed entry fails
    closed rather than being silently skipped.
    """
    if not isinstance(doc, dict) or "SchemaVersion" not in doc:
        raise GateError("trivy report is not a recognizable Trivy JSON document (no SchemaVersion)")

    findings: list[Finding] = []
    results = doc.get("Results")
    if results is None:
        return findings
    if not isinstance(results, list):
        raise GateError("trivy report 'Results' is present but not a list")

    for result in results:
        if not isinstance(result, dict):
            raise GateError("trivy report contains a non-object Results entry")
        vulns = result.get("Vulnerabilities")
        if vulns is None:
            continue
        if not isinstance(vulns, list):
            raise GateError("trivy report 'Vulnerabilities' is present but not a list")
        for vuln in vulns:
            if not isinstance(vuln, dict):
                raise GateError("trivy report contains a non-object vulnerability entry")
            vuln_id = vuln.get("VulnerabilityID")
            severity = vuln.get("Severity")
            if not isinstance(vuln_id, str) or not vuln_id:
                raise GateError("trivy vulnerability entry missing non-empty VulnerabilityID")
            if not isinstance(severity, str) or not severity:
                raise GateError(f"trivy vulnerability {vuln_id} missing non-empty Severity")
            if severity not in _TRIVY_SEVERITIES:
                continue
            fixed_version = vuln.get("FixedVersion")
            if fixed_version is not None and not isinstance(fixed_version, str):
                raise GateError(f"trivy vulnerability {vuln_id} has a non-string FixedVersion")
            findings.append(
                Finding(
                    scanner="trivy",
                    vuln_id=vuln_id,
                    package=_trivy_package(vuln),
                    fixable=bool(fixed_version and fixed_version.strip()),
                )
            )
    return findings


def _trivy_package(vuln: dict) -> str:
    identifier = vuln.get("PkgIdentifier")
    if isinstance(identifier, dict):
        purl = identifier.get("PURL")
        if isinstance(purl, str) and purl:
            return purl
    name = vuln.get("PkgName")
    version = vuln.get("InstalledVersion")
    if isinstance(name, str) and name and isinstance(version, str) and version:
        return f"{name}@{version}"
    return "?"


def parse_grype(doc: object) -> list[Finding]:
    """Extract CRITICAL/HIGH findings from a parsed Grype JSON report.

    ``doc`` must be a dict with a ``matches`` list (Grype always emits this
    key, even when empty, so its absence/wrong type is a shape violation, not
    a clean scan). Each match must carry non-empty
    ``vulnerability.id``/``vulnerability.severity`` strings, and every
    CRITICAL/HIGH match a ``vulnerability.fix`` object with a string ``state``
    (Grype emits ``""`` for a package with no fix, which reads as unfixed).
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
        if severity.casefold() not in _GRYPE_SEVERITIES:
            continue
        fix = vulnerability.get("fix")
        if not isinstance(fix, dict):
            raise GateError(f"grype match {vuln_id} missing 'vulnerability.fix' object")
        fix_state = fix.get("state")
        if not isinstance(fix_state, str):
            raise GateError(f"grype match {vuln_id} has a non-string vulnerability.fix.state")
        findings.append(
            Finding(
                scanner="grype",
                vuln_id=vuln_id,
                package=_grype_package(match),
                fixable=fix_state.casefold() == "fixed",
            )
        )
    return findings


def _grype_package(match: dict) -> str:
    artifact = match.get("artifact")
    if isinstance(artifact, dict):
        purl = artifact.get("purl")
        if isinstance(purl, str) and purl:
            return purl
        name = artifact.get("name")
        version = artifact.get("version")
        if isinstance(name, str) and name and isinstance(version, str) and version:
            return f"{name}@{version}"
    return "?"


def vex_statement_names(doc: object) -> set[str]:
    """Collect every VEX statement's exact ``vulnerability.name``.

    ``doc`` must be a dict with a ``statements`` list; every statement must
    have a non-empty string ``vulnerability.name`` (a name-less statement can
    never match a scanner finding and is a ledger defect, so it fails closed
    rather than being silently skipped). ALIASES ARE DELIBERATELY NOT
    COLLECTED here — only exact reported-id matches satisfy the gate.
    """
    if not isinstance(doc, dict):
        raise GateError("VEX ledger is not a JSON object")
    statements = doc.get("statements")
    if not isinstance(statements, list):
        raise GateError("VEX ledger 'statements' is missing or not a list")

    names: set[str] = set()
    for statement in statements:
        if not isinstance(statement, dict):
            raise GateError("VEX ledger contains a non-object statement entry")
        vulnerability = statement.get("vulnerability")
        if not isinstance(vulnerability, dict):
            raise GateError("VEX statement missing 'vulnerability' object")
        name = vulnerability.get("name")
        if not isinstance(name, str) or not name:
            raise GateError("VEX statement has no non-empty vulnerability.name")
        names.add(name)
    return names


def missing_statements(findings: list[Finding], names: set[str]) -> list[Finding]:
    """Return the FIXABLE ``findings`` whose ``vuln_id`` is not in ``names``.

    Preserves the input ordering. Unfixed findings are never returned here;
    see :func:`report_only`.
    """
    return [f for f in findings if f.fixable and f.vuln_id not in names]


def report_only(findings: list[Finding], names: set[str]) -> list[Finding]:
    """Return the UNFIXED ``findings`` with no exact-keyed statement (listed, never failed)."""
    return [f for f in findings if not f.fixable and f.vuln_id not in names]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed CI gate: every un-VEX'd CRITICAL/HIGH finding from Trivy or "
            "Grype that has a fix available must have a VEX statement keyed by that "
            "exact scanner-reported id (unfixed findings are listed, report-only) "
            "(invariant-audit Finding E, 2026-07-23). Aliases never satisfy the gate."
        )
    )
    parser.add_argument(
        "--trivy", type=Path, required=True, help="Path to Trivy JSON report (no --vex)."
    )
    parser.add_argument(
        "--grype", type=Path, required=True, help="Path to Grype JSON report (no --vex)."
    )
    parser.add_argument(
        "--vex",
        type=Path,
        required=True,
        help="Path to tools/supply-chain/vex/openvex.json (missing/zero-size = empty ledger).",
    )
    args = parser.parse_args(argv)

    try:
        trivy_doc = _load_json(args.trivy, "trivy report")
        grype_doc = _load_json(args.grype, "grype report")

        # Mirror the workflow's own `[ -s ... ]` assemble-step semantics: a
        # missing or zero-size VEX file means "no ledger yet" (template-only
        # repo state) -> empty name set, NOT an error. An existing non-empty
        # file must parse and validate strictly, same as the scanner reports.
        if args.vex.is_file() and args.vex.stat().st_size > 0:
            vex_doc = _load_json(args.vex, "VEX ledger")
            names = vex_statement_names(vex_doc)
        else:
            names = set()

        trivy_findings = parse_trivy(trivy_doc)
        grype_findings = parse_grype(grype_doc)
    except GateError as exc:
        print(f"VEX-DUALKEY ERROR: {exc}", file=sys.stderr)
        return 2

    all_findings = trivy_findings + grype_findings
    missing = missing_statements(all_findings, names)
    for finding in report_only(all_findings, names):
        print(
            f"VEX-DUALKEY report-only: {finding.scanner} finding {finding.vuln_id} "
            f"({finding.package}) has no fix available and no VEX statement"
        )

    if missing:
        for finding in missing:
            print(
                f"VEX-DUALKEY: {finding.scanner} finding {finding.vuln_id} ({finding.package}) "
                "has no VEX statement keyed by this exact id — aliases do not count; add a "
                "paired statement to tools/supply-chain/vex/openvex.json (see vex/README.md)"
            )
        print(f"VEX-DUALKEY: {len(missing)} finding(s) missing a same-scanner-keyed VEX statement.")
        return 1

    unfixed = sum(1 for finding in all_findings if not finding.fixable)
    print(
        f"VEX dual-key gate: {len(trivy_findings)} trivy + {len(grype_findings)} grype findings; "
        f"every fixable one has a same-scanner-keyed VEX statement ({unfixed} without a fix)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
