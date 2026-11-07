# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Report-only vulnerability summary for the ``vuln-scan`` job (never fails a build).

Gate policy: the fail-closed scans fail only on HIGH/CRITICAL findings that
HAVE a fix available (``trivy --ignore-unfixed``, ``grype --only-fixed``, and
the same predicate in ``image_vex_gate.py`` / ``vex_dualkey_gate.py``). Unfixed
findings never fail the build, so this script is where they stay visible: it
reads the RAW (un-VEX'd, unfiltered) Trivy and Grype JSON reports and writes a
Markdown summary listing every HIGH/CRITICAL finding with its fix state, plus
per-severity counts. The workflow appends it to ``$GITHUB_STEP_SUMMARY`` and
uploads the raw JSON reports as the ``vuln-reports`` artifact.

"Fix available" uses each scanner's own predicate: a non-empty Trivy
``FixedVersion`` and Grype ``vulnerability.fix.state == "fixed"``.

Usage::

    python3 tools/supply-chain/vuln_report.py \\
      --trivy "image=$RUNNER_TEMP/ne-postgres-age-trivy-raw.json" \\
      --grype "image=$RUNNER_TEMP/ne-postgres-age-grype-raw.json" \\
      --out "$GITHUB_STEP_SUMMARY"

Exit code is always 0: a missing or unreadable report is written into the
summary as such. The gates, not this script, decide pass/fail.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_SEVERITY_ORDER = ("critical", "high", "medium", "low", "negligible", "unknown")
_GATED = {"critical", "high"}


@dataclass(frozen=True)
class Row:
    scanner: str
    target: str
    vuln_id: str
    severity: str
    package: str
    fixed: bool
    fixed_in: str


def _trivy_rows(target: str, doc: object) -> list[Row]:
    rows: list[Row] = []
    results = doc.get("Results") if isinstance(doc, dict) else None
    for result in results or []:
        if not isinstance(result, dict):
            continue
        for vuln in result.get("Vulnerabilities") or []:
            if not isinstance(vuln, dict):
                continue
            fixed_in = vuln.get("FixedVersion")
            fixed_in = fixed_in.strip() if isinstance(fixed_in, str) else ""
            package = f"{vuln.get('PkgName', '?')}@{vuln.get('InstalledVersion', '?')}"
            rows.append(
                Row(
                    scanner="trivy",
                    target=target,
                    vuln_id=str(vuln.get("VulnerabilityID", "?")),
                    severity=str(vuln.get("Severity", "unknown")).casefold(),
                    package=package,
                    fixed=bool(fixed_in),
                    fixed_in=fixed_in,
                )
            )
    return rows


def _grype_rows(target: str, doc: object) -> list[Row]:
    rows: list[Row] = []
    matches = doc.get("matches") if isinstance(doc, dict) else None
    for match in matches or []:
        if not isinstance(match, dict):
            continue
        vulnerability = match.get("vulnerability") or {}
        artifact = match.get("artifact") or {}
        fix = vulnerability.get("fix") or {}
        state = fix.get("state") if isinstance(fix, dict) else ""
        versions = fix.get("versions") if isinstance(fix, dict) else []
        fixed = isinstance(state, str) and state.casefold() == "fixed"
        rows.append(
            Row(
                scanner="grype",
                target=target,
                vuln_id=str(vulnerability.get("id", "?")),
                severity=str(vulnerability.get("severity", "unknown")).casefold(),
                package=f"{artifact.get('name', '?')}@{artifact.get('version', '?')}",
                fixed=fixed,
                fixed_in=", ".join(str(v) for v in versions or []) if fixed else "",
            )
        )
    return rows


def _load(spec: str) -> tuple[str, Path]:
    label, sep, path = spec.partition("=")
    if not sep or not label or not path:
        raise SystemExit(f"report spec must be LABEL=PATH, got {spec!r}")
    return label, Path(path)


def render(trivy: list[str], grype: list[str]) -> str:
    lines = [
        "## Vulnerability scan report (report-only)",
        "",
        "Gate policy: HIGH/CRITICAL findings **with a fix available** fail the build. "
        "Findings with **no fix** are listed here and in the `vuln-reports` artifact "
        "and do not fail the build.",
        "",
    ]
    rows: list[Row] = []
    for scanner, specs, parse in (("trivy", trivy, _trivy_rows), ("grype", grype, _grype_rows)):
        for spec in specs:
            label, path = _load(spec)
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                lines.append(f"- {scanner} `{label}`: report not produced or unreadable ({exc})")
                continue
            found = parse(label, doc)
            counts = Counter(row.severity for row in found)
            ordered = [s for s in _SEVERITY_ORDER if counts.get(s)] + sorted(
                s for s in counts if s not in _SEVERITY_ORDER
            )
            summary = ", ".join(f"{s}: {counts[s]}" for s in ordered) or "no findings"
            lines.append(f"- {scanner} `{label}`: {summary}")
            rows.extend(found)

    gated = sorted(
        {row for row in rows if row.severity in _GATED},
        key=lambda r: (r.fixed is False, r.target, r.scanner, r.vuln_id, r.package),
    )
    lines.append("")
    if not gated:
        lines.append("No HIGH/CRITICAL findings.")
        return "\n".join(lines) + "\n"
    unfixed = sum(1 for row in gated if not row.fixed)
    lines.append(
        f"HIGH/CRITICAL findings: {len(gated)} ({len(gated) - unfixed} with a fix, "
        f"{unfixed} without a fix, report-only). Fixable ones fail the build unless a "
        "reviewed VEX statement covers them."
    )
    lines.append("")
    lines.append("| Target | Scanner | ID | Severity | Package | Fix available |")
    lines.append("|---|---|---|---|---|---|")
    for row in gated:
        fix = (
            f"yes ({row.fixed_in})"
            if row.fixed and row.fixed_in
            else ("yes" if row.fixed else "no (report-only)")
        )
        lines.append(
            f"| {row.target} | {row.scanner} | {row.vuln_id} | {row.severity.upper()} | "
            f"{row.package} | {fix} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report-only HIGH/CRITICAL vulnerability summary (never fails a build)."
    )
    parser.add_argument("--trivy", action="append", default=[], help="LABEL=PATH (repeatable)")
    parser.add_argument("--grype", action="append", default=[], help="LABEL=PATH (repeatable)")
    parser.add_argument("--out", type=Path, help="Append the summary here (default: stdout).")
    args = parser.parse_args(argv)
    text = render(args.trivy, args.grype)
    if args.out:
        with args.out.open("a", encoding="utf-8") as handle:
            handle.write(text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
