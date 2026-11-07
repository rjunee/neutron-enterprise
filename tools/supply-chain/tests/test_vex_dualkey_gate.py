# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fast, fixture-based tests for the VEX dual-key gate (invariant-audit Finding E).

No live scanner invocation happens here — these exercise the gate's pure
parsing/matching functions against realistic fixture JSON, mirroring how
test_npm_license_gate.py isolates the parsing seam from the live npm/CI seam.
The gate's own live-scanner integration is verified separately (BUILDER-side,
locally, at the exact pinned scanner versions; see the PR body / STEP 7 of
NE-INV-VEX-DUALKEY-TEST) since it is inherently a CI-only concern.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_GATE_PATH = Path(__file__).resolve().parents[1] / "vex_dualkey_gate.py"
_spec = importlib.util.spec_from_file_location("vex_dualkey_gate", _GATE_PATH)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass introspection (cls.__module__ lookup) works.
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)

REAL_VEX_PATH = Path(__file__).resolve().parents[1] / "vex" / "openvex.json"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def trivy_doc(vulns: list[dict]) -> dict:
    return {
        "SchemaVersion": 2,
        "Results": [{"Target": "requirements.txt", "Vulnerabilities": vulns}],
    }


def trivy_vuln(
    vuln_id: str,
    severity: str = "HIGH",
    *,
    pkg_name: str = "transformers",
    installed_version: str = "4.57.6",
    purl: str | None = "pkg:pypi/transformers@4.57.6",
    fixed_version: str | None = "4.58.0",
) -> dict:
    entry: dict = {
        "VulnerabilityID": vuln_id,
        "Severity": severity,
        "PkgName": pkg_name,
        "InstalledVersion": installed_version,
    }
    if fixed_version is not None:
        entry["FixedVersion"] = fixed_version
    if purl is not None:
        entry["PkgIdentifier"] = {"PURL": purl}
    return entry


def grype_doc(matches: list[dict]) -> dict:
    return {"matches": matches}


def grype_match(
    vuln_id: str,
    severity: str = "High",
    *,
    name: str = "transformers",
    version: str = "4.57.6",
    purl: str | None = "pkg:pypi/transformers@4.57.6",
    fix_state: str = "fixed",
) -> dict:
    artifact: dict = {"name": name, "version": version}
    if purl is not None:
        artifact["purl"] = purl
    fix = {"versions": ["4.58.0"] if fix_state == "fixed" else [], "state": fix_state}
    return {
        "vulnerability": {"id": vuln_id, "severity": severity, "fix": fix},
        "artifact": artifact,
    }


def vex_doc(statements: list[dict]) -> dict:
    return {"@context": "https://openvex.dev/ns/v0.2.0", "statements": statements}


def vex_statement(name: str, aliases: list[str] | None = None) -> dict:
    return {
        "vulnerability": {"name": name, "aliases": aliases or []},
        "products": [{"@id": "pkg:pypi/example@1.0.0"}],
        "status": "not_affected",
    }


def write_json(path: Path, doc: object) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# test_parse_trivy_extracts_critical_and_high_findings
# ---------------------------------------------------------------------------
def test_parse_trivy_extracts_critical_and_high_findings() -> None:
    doc = trivy_doc(
        [
            trivy_vuln("CVE-2026-0001", "CRITICAL"),
            trivy_vuln("CVE-2026-0002", "HIGH"),
        ]
    )
    findings = gate.parse_trivy(doc)
    assert len(findings) == 2
    assert {f.vuln_id for f in findings} == {"CVE-2026-0001", "CVE-2026-0002"}
    for f in findings:
        assert f.scanner == "trivy"
        assert f.package == "pkg:pypi/transformers@4.57.6"


# ---------------------------------------------------------------------------
# test_parse_trivy_skips_below_high_and_missing_vulnerability_lists
# ---------------------------------------------------------------------------
def test_parse_trivy_skips_below_high_and_missing_vulnerability_lists() -> None:
    doc = trivy_doc([trivy_vuln("CVE-2026-0003", "MEDIUM"), trivy_vuln("CVE-2026-0004", "LOW")])
    assert gate.parse_trivy(doc) == []

    # A Results entry with no 'Vulnerabilities' key -> skip, no error.
    doc_no_vulns = {"SchemaVersion": 2, "Results": [{"Target": "requirements.txt"}]}
    assert gate.parse_trivy(doc_no_vulns) == []

    # No 'Results' at all (clean scan) -> zero findings, no error.
    doc_no_results = {"SchemaVersion": 2}
    assert gate.parse_trivy(doc_no_results) == []


# ---------------------------------------------------------------------------
# test_parse_trivy_rejects_non_report_shape
# ---------------------------------------------------------------------------
def test_parse_trivy_rejects_non_report_shape() -> None:
    with pytest.raises(gate.GateError):
        gate.parse_trivy([])
    with pytest.raises(gate.GateError):
        gate.parse_trivy({})
    with pytest.raises(gate.GateError):
        gate.parse_trivy("not a report")


# ---------------------------------------------------------------------------
# test_parse_trivy_rejects_vulnerability_missing_id_or_severity
# ---------------------------------------------------------------------------
def test_parse_trivy_rejects_vulnerability_missing_id_or_severity() -> None:
    bad_no_id = trivy_vuln("CVE-2026-0005")
    del bad_no_id["VulnerabilityID"]
    with pytest.raises(gate.GateError):
        gate.parse_trivy(trivy_doc([bad_no_id]))

    bad_empty_severity = trivy_vuln("CVE-2026-0006", severity="")
    with pytest.raises(gate.GateError):
        gate.parse_trivy(trivy_doc([bad_empty_severity]))


# ---------------------------------------------------------------------------
# test_parse_grype_extracts_high_and_critical_matches
# ---------------------------------------------------------------------------
def test_parse_grype_extracts_high_and_critical_matches() -> None:
    doc = grype_doc(
        [
            grype_match("GHSA-aaaa-bbbb-cccc", "Critical"),
            grype_match("GHSA-dddd-eeee-ffff", "High"),
        ]
    )
    findings = gate.parse_grype(doc)
    assert len(findings) == 2
    assert {f.vuln_id for f in findings} == {"GHSA-aaaa-bbbb-cccc", "GHSA-dddd-eeee-ffff"}
    for f in findings:
        assert f.scanner == "grype"


# ---------------------------------------------------------------------------
# test_parse_grype_filters_below_high
# ---------------------------------------------------------------------------
def test_parse_grype_filters_below_high() -> None:
    doc = grype_doc(
        [
            grype_match("GHSA-medium", "Medium"),
            grype_match("GHSA-low", "Low"),
            grype_match("GHSA-negligible", "Negligible"),
            grype_match("GHSA-unknown", "Unknown"),
        ]
    )
    assert gate.parse_grype(doc) == []

    # Casefold comparison: HIGH / high / High all kept.
    doc_cased = grype_doc(
        [
            grype_match("GHSA-upper", "HIGH"),
            grype_match("GHSA-lower", "high"),
            grype_match("GHSA-title", "High"),
        ]
    )
    findings = gate.parse_grype(doc_cased)
    assert {f.vuln_id for f in findings} == {"GHSA-upper", "GHSA-lower", "GHSA-title"}


# ---------------------------------------------------------------------------
# test_parse_grype_rejects_missing_matches_or_ids
# ---------------------------------------------------------------------------
def test_parse_grype_rejects_missing_matches_or_ids() -> None:
    with pytest.raises(gate.GateError):
        gate.parse_grype({})
    with pytest.raises(gate.GateError):
        gate.parse_grype({"matches": "not-a-list"})

    bad_no_id = grype_match("GHSA-zzzz")
    del bad_no_id["vulnerability"]["id"]
    with pytest.raises(gate.GateError):
        gate.parse_grype(grype_doc([bad_no_id]))

    bad_no_severity = grype_match("GHSA-yyyy")
    del bad_no_severity["vulnerability"]["severity"]
    with pytest.raises(gate.GateError):
        gate.parse_grype(grype_doc([bad_no_severity]))


# ---------------------------------------------------------------------------
# test_vex_statement_names_collects_names_never_aliases
# ---------------------------------------------------------------------------
def test_vex_statement_names_collects_names_never_aliases() -> None:
    doc = vex_doc([vex_statement("CVE-A", aliases=["GHSA-B"])])
    names = gate.vex_statement_names(doc)
    assert names == {"CVE-A"}
    assert "GHSA-B" not in names


# ---------------------------------------------------------------------------
# test_vex_statement_missing_name_fails_closed
# ---------------------------------------------------------------------------
def test_vex_statement_missing_name_fails_closed() -> None:
    bad = vex_statement("CVE-A")
    bad["vulnerability"]["name"] = ""
    with pytest.raises(gate.GateError):
        gate.vex_statement_names(vex_doc([bad]))

    bad_missing = vex_statement("CVE-A")
    del bad_missing["vulnerability"]["name"]
    with pytest.raises(gate.GateError):
        gate.vex_statement_names(vex_doc([bad_missing]))


# ---------------------------------------------------------------------------
# test_alias_only_statement_fails_the_gate
# INCIDENT REPLAY (chromadb/json-repair class): ledger has ONE statement named
# CVE-X aliasing GHSA-Y; trivy reports CVE-X, grype reports GHSA-Y.
# ---------------------------------------------------------------------------
def test_alias_only_statement_fails_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    names = gate.vex_statement_names(vex_doc([vex_statement("CVE-X", aliases=["GHSA-Y"])]))
    trivy_findings = gate.parse_trivy(trivy_doc([trivy_vuln("CVE-X")]))
    grype_findings = gate.parse_grype(grype_doc([grype_match("GHSA-Y")]))

    missing = gate.missing_statements(trivy_findings + grype_findings, names)
    assert len(missing) == 1
    assert missing[0].vuln_id == "GHSA-Y"
    assert missing[0].scanner == "grype"

    trivy_path = write_json(tmp_path / "trivy.json", trivy_doc([trivy_vuln("CVE-X")]))
    grype_path = write_json(tmp_path / "grype.json", grype_doc([grype_match("GHSA-Y")]))
    vex_path = write_json(
        tmp_path / "vex.json", vex_doc([vex_statement("CVE-X", aliases=["GHSA-Y"])])
    )

    rc = gate.main(["--trivy", str(trivy_path), "--grype", str(grype_path), "--vex", str(vex_path)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "GHSA-Y" in captured.out


# ---------------------------------------------------------------------------
# test_dual_keyed_ledger_passes
# ---------------------------------------------------------------------------
def test_dual_keyed_ledger_passes(tmp_path: Path) -> None:
    names = gate.vex_statement_names(
        vex_doc(
            [
                vex_statement("CVE-X", aliases=["GHSA-Y"]),
                vex_statement("GHSA-Y", aliases=["CVE-X"]),
            ]
        )
    )
    trivy_findings = gate.parse_trivy(trivy_doc([trivy_vuln("CVE-X")]))
    grype_findings = gate.parse_grype(grype_doc([grype_match("GHSA-Y")]))
    assert gate.missing_statements(trivy_findings + grype_findings, names) == []

    trivy_path = write_json(tmp_path / "trivy.json", trivy_doc([trivy_vuln("CVE-X")]))
    grype_path = write_json(tmp_path / "grype.json", grype_doc([grype_match("GHSA-Y")]))
    vex_path = write_json(
        tmp_path / "vex.json",
        vex_doc(
            [
                vex_statement("CVE-X", aliases=["GHSA-Y"]),
                vex_statement("GHSA-Y", aliases=["CVE-X"]),
            ]
        ),
    )
    rc = gate.main(["--trivy", str(trivy_path), "--grype", str(grype_path), "--vex", str(vex_path)])
    assert rc == 0


# ---------------------------------------------------------------------------
# test_missing_or_empty_vex_file_semantics
# ---------------------------------------------------------------------------
def test_missing_or_empty_vex_file_semantics(tmp_path: Path) -> None:
    trivy_path = write_json(tmp_path / "trivy.json", trivy_doc([]))
    grype_path = write_json(tmp_path / "grype.json", grype_doc([]))
    nonexistent_vex = tmp_path / "does-not-exist.json"

    # Zero findings + nonexistent --vex path -> 0 (template-only repo state).
    rc = gate.main(
        ["--trivy", str(trivy_path), "--grype", str(grype_path), "--vex", str(nonexistent_vex)]
    )
    assert rc == 0

    # Zero-size --vex file behaves the same as nonexistent.
    empty_vex = tmp_path / "empty-vex.json"
    empty_vex.write_text("", encoding="utf-8")
    rc = gate.main(
        ["--trivy", str(trivy_path), "--grype", str(grype_path), "--vex", str(empty_vex)]
    )
    assert rc == 0

    # One finding + nonexistent vex -> fail closed (empty ledger suppresses nothing).
    trivy_with_finding = write_json(
        tmp_path / "trivy2.json", trivy_doc([trivy_vuln("CVE-2026-0099")])
    )
    rc = gate.main(
        [
            "--trivy",
            str(trivy_with_finding),
            "--grype",
            str(grype_path),
            "--vex",
            str(nonexistent_vex),
        ]
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# test_garbage_or_missing_scanner_input_returns_2
# ---------------------------------------------------------------------------
def test_garbage_or_missing_scanner_input_returns_2(tmp_path: Path) -> None:
    grype_path = write_json(tmp_path / "grype.json", grype_doc([]))
    vex_path = write_json(tmp_path / "vex.json", vex_doc([]))
    nonexistent_trivy = tmp_path / "does-not-exist.json"

    rc = gate.main(
        ["--trivy", str(nonexistent_trivy), "--grype", str(grype_path), "--vex", str(vex_path)]
    )
    assert rc == 2

    trivy_path = write_json(tmp_path / "trivy.json", trivy_doc([]))
    garbage_grype = tmp_path / "garbage-grype.json"
    garbage_grype.write_text("{not json", encoding="utf-8")
    rc = gate.main(
        ["--trivy", str(trivy_path), "--grype", str(garbage_grype), "--vex", str(vex_path)]
    )
    assert rc == 2


# ---------------------------------------------------------------------------
# test_committed_ledger_is_dual_keyed
# ---------------------------------------------------------------------------
def test_committed_ledger_is_dual_keyed() -> None:
    doc = json.loads(REAL_VEX_PATH.read_text(encoding="utf-8"))
    names = gate.vex_statement_names(doc)
    expected = {
        "CVE-2026-4372",
        "GHSA-29pf-2h5f-8g72",
        "CVE-2026-5241",
        "GHSA-fgcw-684q-jj6r",
        "CVE-2026-1839",
        "GHSA-69w3-r845-3855",
        "CVE-2025-14929",
        "PYSEC-2025-217",
        "CVE-2026-6587",
        "GHSA-95ww-475f-pr4f",
        "CVE-2025-69872",
        "GHSA-w8v5-vhqr-4h9v",
        "CVE-2026-45829",
        "GHSA-f4j7-r4q5-qw2c",
        "CVE-2026-45830",
        "GHSA-2wm9-hf6c-p5cr",
        "CVE-2026-45831",
        "GHSA-xph7-9rjv-w5fr",
        "CVE-2026-45833",
        "GHSA-36p7-vc44-83pf",
        "GHSA-xf7x-x43h-rpqh",
    }
    assert expected <= names


# ---------------------------------------------------------------------------
# Fix-available policy: only fixable CRITICAL/HIGH findings need a statement;
# unfixed ones are listed report-only and never fail the gate.
# ---------------------------------------------------------------------------
def test_fix_state_is_read_with_each_scanners_own_predicate() -> None:
    trivy = gate.parse_trivy(
        trivy_doc(
            [
                trivy_vuln("CVE-FIXED"),
                trivy_vuln("CVE-EMPTY", fixed_version=""),
                trivy_vuln("CVE-ABSENT", fixed_version=None),
            ]
        )
    )
    assert [(f.vuln_id, f.fixable) for f in trivy] == [
        ("CVE-FIXED", True),
        ("CVE-EMPTY", False),
        ("CVE-ABSENT", False),
    ]
    grype = gate.parse_grype(
        grype_doc(
            [
                grype_match("GHSA-FIXED"),
                grype_match("GHSA-UPPER", fix_state="Fixed"),
                grype_match("GHSA-EMPTY", fix_state=""),
                grype_match("GHSA-NOT", fix_state="not-fixed"),
                grype_match("GHSA-WONT", fix_state="wont-fix"),
                grype_match("GHSA-UNKNOWN", fix_state="unknown"),
            ]
        )
    )
    assert [(f.vuln_id, f.fixable) for f in grype] == [
        ("GHSA-FIXED", True),
        ("GHSA-UPPER", True),
        ("GHSA-EMPTY", False),
        ("GHSA-NOT", False),
        ("GHSA-WONT", False),
        ("GHSA-UNKNOWN", False),
    ]


def test_unfixed_findings_without_statements_are_report_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trivy_path = write_json(
        tmp_path / "trivy.json", trivy_doc([trivy_vuln("CVE-UNFIXED", fixed_version="")])
    )
    grype_path = write_json(
        tmp_path / "grype.json", grype_doc([grype_match("GHSA-UNFIXED", fix_state="")])
    )
    vex_path = write_json(tmp_path / "vex.json", vex_doc([vex_statement("CVE-OTHER")]))

    rc = gate.main(["--trivy", str(trivy_path), "--grype", str(grype_path), "--vex", str(vex_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "VEX-DUALKEY report-only: trivy finding CVE-UNFIXED" in out
    assert "VEX-DUALKEY report-only: grype finding GHSA-UNFIXED" in out
    assert "(2 without a fix)" in out


def test_fixable_finding_still_fails_beside_a_report_only_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trivy_path = write_json(
        tmp_path / "trivy.json",
        trivy_doc([trivy_vuln("CVE-UNFIXED", fixed_version=""), trivy_vuln("CVE-FIXABLE")]),
    )
    grype_path = write_json(tmp_path / "grype.json", grype_doc([]))
    vex_path = write_json(tmp_path / "vex.json", vex_doc([vex_statement("CVE-OTHER")]))

    rc = gate.main(["--trivy", str(trivy_path), "--grype", str(grype_path), "--vex", str(vex_path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "VEX-DUALKEY report-only: trivy finding CVE-UNFIXED" in out
    assert "VEX-DUALKEY: trivy finding CVE-FIXABLE" in out
    assert "VEX-DUALKEY: 1 finding(s) missing a same-scanner-keyed VEX statement." in out


def test_grype_high_match_without_a_fix_object_fails_closed(tmp_path: Path) -> None:
    match = grype_match("GHSA-X")
    del match["vulnerability"]["fix"]
    with pytest.raises(gate.GateError):
        gate.parse_grype(grype_doc([match]))

    bad_state = grype_match("GHSA-X")
    bad_state["vulnerability"]["fix"]["state"] = None
    with pytest.raises(gate.GateError):
        gate.parse_grype(grype_doc([bad_state]))

    trivy_bad = trivy_vuln("CVE-X")
    trivy_bad["FixedVersion"] = 7
    with pytest.raises(gate.GateError):
        gate.parse_trivy(trivy_doc([trivy_bad]))

    trivy_path = write_json(tmp_path / "trivy.json", trivy_doc([]))
    grype_path = write_json(tmp_path / "grype.json", grype_doc([match]))
    vex_path = write_json(tmp_path / "vex.json", vex_doc([vex_statement("CVE-OTHER")]))
    rc = gate.main(["--trivy", str(trivy_path), "--grype", str(grype_path), "--vex", str(vex_path)])
    assert rc == 2
