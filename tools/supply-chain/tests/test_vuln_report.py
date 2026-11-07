# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The report-only pass lists unfixed HIGH/CRITICAL findings and never fails."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "vuln_report.py"
_spec = importlib.util.spec_from_file_location("vuln_report", _PATH)
assert _spec and _spec.loader
report = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = report
_spec.loader.exec_module(report)


def _write(path: Path, doc: object) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_unfixed_and_fixed_findings_are_both_listed(tmp_path: Path) -> None:
    grype = _write(
        tmp_path / "grype.json",
        {
            "matches": [
                {
                    "vulnerability": {
                        "id": "CVE-2099-0001",
                        "severity": "High",
                        "fix": {"versions": [], "state": ""},
                    },
                    "artifact": {"name": "glibc-2.44", "version": "2.44-r13"},
                },
                {
                    "vulnerability": {
                        "id": "CVE-2099-0002",
                        "severity": "Critical",
                        "fix": {"versions": ["3.55.0-r0"], "state": "fixed"},
                    },
                    "artifact": {"name": "sqlite-libs", "version": "3.54.1-r0"},
                },
                {
                    "vulnerability": {
                        "id": "CVE-2099-0003",
                        "severity": "Medium",
                        "fix": {"versions": [], "state": ""},
                    },
                    "artifact": {"name": "zlib", "version": "1.3.2-r4"},
                },
            ]
        },
    )
    trivy = _write(
        tmp_path / "trivy.json",
        {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2099-0004",
                            "Severity": "HIGH",
                            "PkgName": "transformers",
                            "InstalledVersion": "4.57.6",
                        }
                    ]
                }
            ],
        },
    )
    out = tmp_path / "summary.md"
    rc = report.main(["--grype", f"image={grype}", "--trivy", f"repo={trivy}", "--out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    rows = [
        "| image | grype | CVE-2099-0001 | HIGH | glibc-2.44@2.44-r13 | no (report-only) |",
        "| image | grype | CVE-2099-0002 | CRITICAL | sqlite-libs@3.54.1-r0 | yes (3.55.0-r0) |",
        "| repo | trivy | CVE-2099-0004 | HIGH | transformers@4.57.6 | no (report-only) |",
    ]
    for row in rows:
        assert row in text, row
    assert "CVE-2099-0003" not in text
    assert "grype `image`: critical: 1, high: 1, medium: 1" in text
    assert "3 (1 with a fix, 2 without a fix, report-only)" in text


def test_missing_report_is_written_into_the_summary_not_raised(tmp_path: Path) -> None:
    out = tmp_path / "summary.md"
    rc = report.main(["--grype", f"image={tmp_path / 'absent.json'}", "--out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "grype `image`: report not produced or unreadable" in text
    assert "No HIGH/CRITICAL findings." in text
