# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fixture-based tests for the image-scoped OpenVEX gate (SPEC 6.1.1, 2026-09-20).

No Docker and no scanner run here: the gate's whole matching surface is pure,
so the export tars and Grype documents are built in-process. The real-image
runs (a `docker export` of the built ne-postgres-age image, the raw
`grype -o json` report it was written against, and the `--vex`'d scan the
statement must suppress) are the BUILDER-side evidence recorded in the PR body;
they are inherently a CI/local-image concern, the same split
`test_vex_dualkey_gate.py` uses.

Every failure test asserts BOTH the exit code by equality AND the message
prefix of the check it names, so a different check failing for its own reasons
cannot satisfy it (vacuous-guards V5/V7). Each failure family has a green
control -- inline, or `test_control_real_shaped_inputs_pass_end_to_end`, which
every fixture here is a single-field mutation of.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

_GATE_PATH = Path(__file__).resolve().parents[1] / "image_vex_gate.py"
_spec = importlib.util.spec_from_file_location("image_vex_gate", _GATE_PATH)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass introspection (cls.__module__ lookup) works.
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)

_VEX_DIR = Path(__file__).resolve().parents[1] / "vex"
REAL_IMAGE_VEX_PATH = _VEX_DIR / "ne-postgres-age.openvex.json"
REAL_REACHABILITY_PATH = _VEX_DIR / "ne-postgres-age.reachability.json"
REAL_DIR_VEX_PATH = _VEX_DIR / "openvex.json"

# The VERBATIM purl grype 0.115.0 reports for glibc on the built image
# (`matches[].artifact.purl`), copied from a real `grype <image> -o json` run.
PURL = "pkg:apk/wolfi/glibc-2.44@2.44-r6?arch=x86_64&distro=wolfi-20230201"
CVE = "CVE-2026-19499"
LIBC = "usr/lib/libc.so.6"
AGE_SO = "usr/lib/postgresql18/age.so"

# The REAL shape grype emits for a Wolfi package with no fix: an EMPTY STRING
# state, not "not-fixed". A fixture that assumed otherwise would be a V8
# fixture-cannot-reach-the-failure defect.
NO_FIX = {"versions": [], "state": ""}
# A finding Wolfi HAS shipped a fix for: the only kind that fails the gate when
# it is unledgered (policy: fail on HIGH/CRITICAL only when a fix exists).
FIXED = {"versions": ["2.44-r7"], "state": "fixed"}

_ABSENT = object()

GOOD_FILES: dict[str, bytes] = {
    LIBC: b"\x7fELF\x00strfmon\x00strfmon_l\x00",
    AGE_SO: b"\x7fELF\x00age\x00",
    "usr/bin/postgres": b"\x7fELF",
}
GOOD_SYMLINKS: dict[str, str] = {"lib64/libc.so.6": "../usr/lib/libc.so.6"}
GOOD_DIRECTORIES: tuple[str, ...] = ("usr", "usr/lib", "usr/lib/postgresql18", "usr/bin")

DEFAULT_REACH: dict[str, object] = {
    CVE: {"needles": ["strfmon"], "allowed_files": [LIBC]},
}


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def write_export(
    path: Path,
    files: dict[str, bytes],
    symlinks: dict[str, str] | None = None,
    hardlinks: dict[str, str] | None = None,
    directories: tuple[str, ...] = GOOD_DIRECTORIES,
) -> Path:
    """Write an uncompressed tar shaped like `docker export` output."""
    with tarfile.open(path, "w") as archive:
        for name in directories:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, blob in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(blob))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
        for name, target in (hardlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.LNKTYPE
            info.linkname = target
            archive.addfile(info)
    return path


def grype_match(
    vuln_id: str = CVE,
    severity: str = "High",
    *,
    purl: object = PURL,
    fix: object = _ABSENT,
) -> dict:
    vulnerability: dict = {"id": vuln_id, "severity": severity}
    vulnerability["fix"] = dict(NO_FIX) if fix is _ABSENT else fix
    if vulnerability["fix"] is None:
        del vulnerability["fix"]
    artifact: dict = {"name": "glibc-2.44", "version": "2.44-r6"}
    if purl is not None:
        artifact["purl"] = purl
    return {"vulnerability": vulnerability, "artifact": artifact}


def grype_doc(matches: list[dict]) -> dict:
    return {"matches": matches}


def vex_statement(
    name: str = CVE,
    products: tuple[str, ...] = (PURL,),
    *,
    status: str = "not_affected",
    justification: str = "vulnerable_code_not_in_execute_path",
    impact_statement: str = "no caller of the symbol exists in the image",
    aliases: list[str] | None = None,
) -> dict:
    vulnerability: dict = {"name": name}
    if aliases is not None:
        vulnerability["aliases"] = aliases
    return {
        "vulnerability": vulnerability,
        "products": [{"@id": product} for product in products],
        "status": status,
        "justification": justification,
        "impact_statement": impact_statement,
    }


def vex_doc(statements: list[dict]) -> dict:
    return {"@context": "https://openvex.dev/ns/v0.2.0", "statements": statements}


def write_json(path: Path, doc: object) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def default_inputs(
    tmp_path: Path,
    *,
    files: dict[str, bytes] | None = None,
    symlinks: dict[str, str] | None = None,
    hardlinks: dict[str, str] | None = None,
    matches: list[dict] | None = None,
    statements: list[dict] | None = None,
    reachability: dict[str, object] | None = None,
) -> dict[str, Path]:
    paths = {
        "export": tmp_path / "export.tar",
        "grype": tmp_path / "grype.json",
        "vex": tmp_path / "vex.json",
        "reach": tmp_path / "reach.json",
    }
    write_export(
        paths["export"],
        GOOD_FILES if files is None else files,
        symlinks=GOOD_SYMLINKS if symlinks is None else symlinks,
        hardlinks=hardlinks,
    )
    write_json(paths["grype"], grype_doc([grype_match()] if matches is None else matches))
    write_json(paths["vex"], vex_doc([vex_statement()] if statements is None else statements))
    write_json(paths["reach"], DEFAULT_REACH if reachability is None else reachability)
    return paths


def argv(paths: dict[str, Path], **override: Path) -> list[str]:
    merged = {**paths, **override}
    return [
        "--export",
        str(merged["export"]),
        "--grype",
        str(merged["grype"]),
        "--vex",
        str(merged["vex"]),
        "--reachability",
        str(merged["reach"]),
    ]


def run(tmp_path: Path, **kwargs) -> int:
    return gate.main(argv(default_inputs(tmp_path, **kwargs)))


# ---------------------------------------------------------------------------
# The control every failure fixture below is a single-field mutation of
# ---------------------------------------------------------------------------
def test_control_real_shaped_inputs_pass_end_to_end(tmp_path: Path, capsys) -> None:
    assert run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "1 statement(s) exactly cover 1 HIGH+ finding(s)" in out
    assert "IMAGE-VEX" not in out


# ---------------------------------------------------------------------------
# (a) reachability probe
# ---------------------------------------------------------------------------
def test_probe_needle_in_a_second_file_fails(tmp_path: Path, capsys) -> None:
    """The ruling's mutation 1: `strfmon` bytes in a second file -> RED."""
    files = dict(GOOD_FILES)
    files[AGE_SO] = b"\x7fELF\x00age\x00strfmon\x00"
    assert run(tmp_path, files=files) == 1
    assert (
        f"IMAGE-VEX probe: {CVE}: needle found outside allowed_files: {AGE_SO}"
        in capsys.readouterr().out
    )


def test_probe_missing_libc_fails(tmp_path: Path, capsys) -> None:
    """The ruling's mutation 2, and the probe-can-see control.

    A probe that cannot find the symbol where it is known to live is broken,
    not clean -- so the absent direction must fail as loudly as the extra one.
    Second sub-case: libc present but without the needle is the same violation,
    which is what a needle rename or a stripped libc would look like.
    """
    files = {name: blob for name, blob in GOOD_FILES.items() if name != LIBC}
    assert run(tmp_path, files=files) == 1
    expected = (
        f"IMAGE-VEX probe: {CVE}: allowed file absent from the export or without any needle: {LIBC}"
    )
    assert expected in capsys.readouterr().out

    present_but_clean = dict(GOOD_FILES)
    present_but_clean[LIBC] = b"\x7fELF\x00printf\x00"
    assert run(tmp_path, files=present_but_clean) == 1
    assert expected in capsys.readouterr().out


def test_probe_needle_at_a_different_path_fails_in_both_directions(tmp_path: Path, capsys) -> None:
    files = {name: blob for name, blob in GOOD_FILES.items() if name != LIBC}
    files["lib/libc.so.6"] = GOOD_FILES[LIBC]
    assert run(tmp_path, files=files) == 1
    out = capsys.readouterr().out
    assert f"IMAGE-VEX probe: {CVE}: needle found outside allowed_files: lib/libc.so.6" in out
    assert (
        f"IMAGE-VEX probe: {CVE}: allowed file absent from the export or without any needle: {LIBC}"
    ) in out


def test_probe_skips_symlinks_and_directories_but_counts_hard_links(tmp_path: Path, capsys) -> None:
    """A symlink is a pointer, not a second copy; a hard link IS the bytes."""
    assert run(tmp_path, symlinks={"lib64/libc.so.6": "../usr/lib/libc.so.6"}) == 0
    assert "IMAGE-VEX" not in capsys.readouterr().out

    assert run(tmp_path, hardlinks={"usr/lib/libc-alias.so.6": LIBC}) == 1
    assert (
        f"IMAGE-VEX probe: {CVE}: needle found outside allowed_files: usr/lib/libc-alias.so.6"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize("member_name", [f"./{LIBC}", f"/{LIBC}"])
def test_probe_normalises_leading_dot_slash_and_slash_member_names(
    tmp_path: Path, member_name: str, capsys
) -> None:
    files = {name: blob for name, blob in GOOD_FILES.items() if name != LIBC}
    files[member_name] = GOOD_FILES[LIBC]
    assert run(tmp_path, files=files) == 0
    assert "IMAGE-VEX" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# (b) ledger exactness
# ---------------------------------------------------------------------------
def test_stale_statement_with_no_matching_finding_fails(tmp_path: Path, capsys) -> None:
    """The ruling's mutation 3: a statement with no matching finding -> RED."""
    other = "pkg:apk/wolfi/zlib@1.3.2-r4?arch=x86_64&distro=wolfi-20230201"
    statements = [vex_statement(), vex_statement("CVE-2099-0001", (other,))]
    reachability = {
        **DEFAULT_REACH,
        "CVE-2099-0001": {"needles": ["strfmon"], "allowed_files": [LIBC]},
    }
    assert run(tmp_path, statements=statements, reachability=reachability) == 1
    assert f"IMAGE-VEX exactness: stale statement CVE-2099-0001 {other}" in capsys.readouterr().out


@pytest.mark.parametrize("severity", ["High", "high", "Critical", "CRITICAL", "critical"])
def test_high_finding_without_a_statement_fails(tmp_path: Path, severity: str, capsys) -> None:
    other = "pkg:apk/wolfi/zlib@1.3.2-r4?arch=x86_64&distro=wolfi-20230201"
    matches = [grype_match(), grype_match("CVE-2099-0002", severity, purl=other, fix=FIXED)]
    assert run(tmp_path, matches=matches) == 1
    assert (
        f"IMAGE-VEX exactness: unledgered HIGH+ finding CVE-2099-0002 {other} (fix available)"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize("state", ["", "not-fixed", "wont-fix", "unknown"])
@pytest.mark.parametrize("severity", ["High", "Critical"])
def test_unfixed_unledgered_finding_is_report_only(
    tmp_path: Path, severity: str, state: str, capsys
) -> None:
    """No fix available -> printed as report-only, never a violation."""
    other = "pkg:apk/wolfi/zlib@1.3.2-r4?arch=x86_64&distro=wolfi-20230201"
    fix = {"versions": [], "state": state}
    matches = [grype_match(), grype_match("CVE-2099-0002", severity, purl=other, fix=fix)]
    assert run(tmp_path, matches=matches) == 0
    out = capsys.readouterr().out
    assert (
        f"IMAGE-VEX report-only: unfixed HIGH+ finding CVE-2099-0002 {other} (no fix available)"
        in out
    )
    assert "IMAGE-VEX exactness:" not in out
    assert "(1 without a fix)" not in out and "(2 without a fix)" in out


def test_report_only_and_fixable_findings_are_reported_side_by_side(tmp_path: Path, capsys) -> None:
    """A fixable finding still fails while an unfixed one beside it stays listed."""
    zlib = "pkg:apk/wolfi/zlib@1.3.2-r4?arch=x86_64&distro=wolfi-20230201"
    sqlite = "pkg:apk/wolfi/sqlite-libs@3.54.1-r0?arch=x86_64&distro=wolfi-20230201"
    matches = [
        grype_match(),
        grype_match("CVE-2099-0002", "High", purl=zlib, fix=FIXED),
        grype_match("CVE-2099-0003", "Critical", purl=sqlite),
    ]
    assert run(tmp_path, matches=matches) == 1
    out = capsys.readouterr().out
    assert f"IMAGE-VEX exactness: unledgered HIGH+ finding CVE-2099-0002 {zlib}" in out
    assert f"IMAGE-VEX report-only: unfixed HIGH+ finding CVE-2099-0003 {sqlite}" in out
    assert "CVE-2099-0003" not in out.split("IMAGE-VEX report-only:")[0]


def test_same_id_with_a_different_purl_version_is_not_a_match(tmp_path: Path, capsys) -> None:
    """The purl is load-bearing: a version bump must not inherit the statement."""
    bumped = PURL.replace("@2.44-r6", "@2.44-r7")
    assert run(tmp_path, matches=[grype_match(purl=bumped, fix=FIXED)]) == 1
    out = capsys.readouterr().out
    assert f"IMAGE-VEX exactness: unledgered HIGH+ finding {CVE} {bumped}" in out
    assert f"IMAGE-VEX exactness: stale statement {CVE} {PURL}" in out


def test_aliases_never_satisfy_exactness(tmp_path: Path, capsys) -> None:
    statements = [vex_statement("GHSA-xxxx-yyyy-zzzz", aliases=[CVE])]
    reachability = {"GHSA-xxxx-yyyy-zzzz": {"needles": ["strfmon"], "allowed_files": [LIBC]}}
    matches = [grype_match(fix=FIXED)]
    assert run(tmp_path, matches=matches, statements=statements, reachability=reachability) == 1
    out = capsys.readouterr().out
    assert f"IMAGE-VEX exactness: unledgered HIGH+ finding {CVE} {PURL}" in out
    assert f"IMAGE-VEX exactness: stale statement GHSA-xxxx-yyyy-zzzz {PURL}" in out


def test_below_high_findings_are_outside_the_ledger(tmp_path: Path, capsys) -> None:
    """Medium/Low need no statement -- and cannot keep one alive either."""
    other = "pkg:apk/wolfi/zlib@1.3.2-r4?arch=x86_64&distro=wolfi-20230201"
    matches = [
        grype_match(),
        grype_match("CVE-2026-19542", "Medium", purl=other),
        grype_match("CVE-2026-89092", "Low", purl=other),
    ]
    assert run(tmp_path, matches=matches) == 0
    assert "IMAGE-VEX" not in capsys.readouterr().out

    assert run(tmp_path, matches=[grype_match(severity="Medium")]) == 1
    assert f"IMAGE-VEX exactness: stale statement {CVE} {PURL}" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# (c) fix check
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("state", ["fixed", "Fixed", "FIXED"])
def test_fix_state_fixed_on_a_vexed_finding_fails(tmp_path: Path, state: str, capsys) -> None:
    """The ruling's mutation 4: the VEX dies the day Wolfi ships a fix."""
    fix = {"versions": ["2.44-r7"], "state": state}
    assert run(tmp_path, matches=[grype_match(fix=fix)]) == 1
    assert f"IMAGE-VEX fix: {CVE} {PURL} has fix.state=fixed" in capsys.readouterr().out


@pytest.mark.parametrize("state", ["", "not-fixed", "wont-fix", "unknown"])
def test_unfixed_states_are_the_green_control(tmp_path: Path, state: str, capsys) -> None:
    fix = {"versions": [], "state": state}
    assert run(tmp_path, matches=[grype_match(fix=fix)]) == 0
    assert "IMAGE-VEX" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# (d) ledger <-> reachability bijection
# ---------------------------------------------------------------------------
def test_ledger_reachability_bijection_fails_in_both_directions(tmp_path: Path, capsys) -> None:
    extra_entry = {
        **DEFAULT_REACH,
        "CVE-2099-0003": {"needles": ["strfmon"], "allowed_files": [LIBC]},
    }
    assert run(tmp_path, reachability=extra_entry) == 1
    assert "IMAGE-VEX bijection: no ledger statement for CVE-2099-0003" in capsys.readouterr().out

    other = "pkg:apk/wolfi/zlib@1.3.2-r4?arch=x86_64&distro=wolfi-20230201"
    statements = [vex_statement(), vex_statement("CVE-2099-0004", (other,))]
    assert run(tmp_path, statements=statements) == 1
    assert "IMAGE-VEX bijection: no reachability entry for CVE-2099-0004" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# (e) statement shape
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("case", "statements"),
    [
        ("status-affected", [vex_statement(status="affected")]),
        ("status-fixed", [vex_statement(status="fixed")]),
        ("status-under-investigation", [vex_statement(status="under_investigation")]),
        ("status-false-positive", [vex_statement(status="false_positive")]),
        ("product-not-apk", [vex_statement(products=("pkg:pypi/x@1",))]),
        ("product-unversioned", [vex_statement(products=("pkg:apk/wolfi/glibc-2.44",))]),
        (
            "product-unversioned-with-qualifiers",
            [vex_statement(products=("pkg:apk/wolfi/glibc-2.44?arch=x86_64",))],
        ),
        (
            "product-at-only-inside-qualifiers",
            [vex_statement(products=("pkg:apk/wolfi/glibc-2.44?maintainer=a@b",))],
        ),
        (
            "two-products",
            [vex_statement(products=(PURL, "pkg:apk/wolfi/zlib@1.3.2-r4"))],
        ),
        ("zero-products", [vex_statement(products=())]),
        ("other-justification", [vex_statement(justification="component_not_present")]),
        ("blank-impact-statement", [vex_statement(impact_statement="   ")]),
        ("duplicate-name-product-pair", [vex_statement(), vex_statement()]),
    ],
)
def test_statement_shape_rejections(
    tmp_path: Path, case: str, statements: list[dict], capsys
) -> None:
    assert run(tmp_path, statements=statements) == 1
    assert "IMAGE-VEX shape: statement " in capsys.readouterr().out, case


# ---------------------------------------------------------------------------
# Malformed inputs fail closed with exit 2, never 0 and never 1
# ---------------------------------------------------------------------------
def _unlink(key: str):
    def mutate(paths: dict[str, Path]) -> None:
        paths[key].unlink()

    return mutate


def _write_text(key: str, text: str):
    def mutate(paths: dict[str, Path]) -> None:
        paths[key].write_text(text, encoding="utf-8")

    return mutate


def _write(key: str, doc: object):
    def mutate(paths: dict[str, Path]) -> None:
        write_json(paths[key], doc)

    return mutate


def _reach(entry: object):
    return _write("reach", {CVE: entry})


_ABS_PATH_REACH = {"needles": ["strfmon"], "allowed_files": [f"/{LIBC}"]}
_DOT_PATH_REACH = {"needles": ["strfmon"], "allowed_files": [f"./{LIBC}"]}
_DOTDOT_PATH_REACH = {"needles": ["strfmon"], "allowed_files": ["usr/../usr/lib/libc.so.6"]}

_MALFORMED = [
    ("missing-export", _unlink("export")),
    ("non-tar-export", _write_text("export", "definitely not a tar archive")),
    ("garbage-grype-json", _write_text("grype", "{not json")),
    ("grype-without-matches", _write("grype", {"SchemaVersion": 2})),
    ("grype-matches-not-a-list", _write("grype", {"matches": {}})),
    ("high-match-empty-purl", _write("grype", grype_doc([grype_match(purl="")]))),
    ("high-match-missing-purl", _write("grype", grype_doc([grype_match(purl=None)]))),
    ("high-match-without-fix", _write("grype", grype_doc([grype_match(fix=None)]))),
    ("fix-state-non-string", _write("grype", grype_doc([grype_match(fix={"state": 1})]))),
    (
        "fix-versions-non-list",
        _write("grype", grype_doc([grype_match(fix={"state": "", "versions": "none"})])),
    ),
    ("missing-ledger", _unlink("vex")),
    ("zero-size-ledger", _write_text("vex", "")),
    ("ledger-without-statements-list", _write("vex", {"@context": "x"})),
    (
        "statement-without-vulnerability-name",
        _write("vex", vex_doc([{"vulnerability": {}, "products": [], "status": "not_affected"}])),
    ),
    (
        "statement-product-without-id",
        _write("vex", vex_doc([{"vulnerability": {"name": CVE}, "products": [{}]}])),
    ),
    ("missing-reachability", _unlink("reach")),
    ("reachability-not-an-object", _write("reach", [])),
    (
        "reachability-entry-extra-key",
        _reach({"needles": ["strfmon"], "allowed_files": [LIBC], "note": "x"}),
    ),
    ("reachability-entry-missing-key", _reach({"needles": ["strfmon"]})),
    ("reachability-empty-needles", _reach({"needles": [], "allowed_files": [LIBC]})),
    ("reachability-empty-allowed-files", _reach({"needles": ["strfmon"], "allowed_files": []})),
    ("reachability-absolute-allowed-path", _reach(_ABS_PATH_REACH)),
    ("reachability-dot-slash-allowed-path", _reach(_DOT_PATH_REACH)),
    ("reachability-dotdot-allowed-path", _reach(_DOTDOT_PATH_REACH)),
    ("reachability-non-string-needle", _reach({"needles": [1], "allowed_files": [LIBC]})),
    (
        "reachability-duplicate-allowed-path",
        _reach({"needles": ["strfmon"], "allowed_files": [LIBC, LIBC]}),
    ),
]


@pytest.mark.parametrize(("case", "mutate"), _MALFORMED, ids=[c for c, _ in _MALFORMED])
def test_malformed_inputs_exit_2(tmp_path: Path, case: str, mutate, capsys) -> None:
    paths = default_inputs(tmp_path)
    mutate(paths)
    assert gate.main(argv(paths)) == 2, case
    captured = capsys.readouterr()
    assert captured.err.startswith("IMAGE-VEX ERROR: "), case
    assert "IMAGE-VEX probe:" not in captured.out, case


# ---------------------------------------------------------------------------
# The COMMITTED files, bound to the gate rather than to a test-local ledger
# ---------------------------------------------------------------------------
def test_committed_image_ledger_and_reachability_map_are_both_empty() -> None:
    """Binds the SHIPPED files through the gate's own parsers, not only raw JSON."""
    doc = json.loads(REAL_IMAGE_VEX_PATH.read_text(encoding="utf-8"))
    assert doc["@context"] == "https://openvex.dev/ns/v0.2.0"
    assert doc["statements"] == []
    assert gate.parse_ledger(doc) == []

    reach = json.loads(REAL_REACHABILITY_PATH.read_text(encoding="utf-8"))
    assert reach == {}
    assert gate.parse_reachability(reach) == {}


def _shared_vulnerability_names(image_doc: dict, dir_doc: dict) -> set[str]:
    image_names = {s["vulnerability"]["name"] for s in image_doc["statements"]}
    dir_names = {s["vulnerability"]["name"] for s in dir_doc["statements"]}
    return image_names & dir_names


def test_committed_image_and_dir_ledgers_share_no_vulnerability_name() -> None:
    """Separate ledgers: neither can silence the other scan's findings. The
    overlap check's ability to see a collision is proven by the fixture test
    below, so an empty image ledger does not make this vacuous."""
    image_doc = json.loads(REAL_IMAGE_VEX_PATH.read_text(encoding="utf-8"))
    dir_doc = json.loads(REAL_DIR_VEX_PATH.read_text(encoding="utf-8"))
    assert dir_doc["statements"]
    assert _shared_vulnerability_names(image_doc, dir_doc) == set()


def test_ledger_name_overlap_check_reports_a_fixture_collision_with_the_committed_dir_ledger() -> (
    None
):
    dir_doc = json.loads(REAL_DIR_VEX_PATH.read_text(encoding="utf-8"))
    colliding = vex_doc([vex_statement("GHSA-frch-4w6v-q5xx")])
    assert _shared_vulnerability_names(colliding, dir_doc) == {"GHSA-frch-4w6v-q5xx"}
    unrelated = vex_doc([vex_statement("CVE-2099-0009")])
    assert _shared_vulnerability_names(unrelated, dir_doc) == set()


def _committed_ledger_argv(export: Path, grype: Path) -> list[str]:
    return [
        "--export",
        str(export),
        "--grype",
        str(grype),
        "--vex",
        str(REAL_IMAGE_VEX_PATH),
        "--reachability",
        str(REAL_REACHABILITY_PATH),
    ]


def test_committed_empty_ledger_passes_the_gate_against_a_zero_high_grype_report(
    tmp_path: Path, capsys
) -> None:
    """Binds the SHIPPED ledger + reachability map to the gate. The grype report
    carries only CVE-2026-89092 at Medium: the shape the registry scan of the
    pinned base reports on 2026-09-23."""
    export = write_export(tmp_path / "export.tar", GOOD_FILES, symlinks=GOOD_SYMLINKS)
    grype = write_json(
        tmp_path / "grype.json", grype_doc([grype_match("CVE-2026-89092", "Medium")])
    )
    assert gate.main(_committed_ledger_argv(export, grype)) == 0
    out = capsys.readouterr().out
    assert "0 statement(s) exactly cover 0 HIGH+ finding(s)" in out
    assert "IMAGE-VEX" not in out


def test_fixable_glibc_high_finding_against_the_committed_empty_ledger_is_unledgered(
    tmp_path: Path, capsys
) -> None:
    """The empty committed ledger silences nothing: a fixable glibc HIGH row fails."""
    export = write_export(tmp_path / "export.tar", GOOD_FILES, symlinks=GOOD_SYMLINKS)
    grype = write_json(tmp_path / "grype.json", grype_doc([grype_match(fix=FIXED)]))
    assert gate.main(_committed_ledger_argv(export, grype)) == 1
    out = capsys.readouterr().out
    assert f"IMAGE-VEX exactness: unledgered HIGH+ finding {CVE} {PURL} (fix available)" in out


def test_unfixed_glibc_high_finding_against_the_committed_empty_ledger_is_report_only(
    tmp_path: Path, capsys
) -> None:
    """The same row with no fix passes the gate but is still printed."""
    export = write_export(tmp_path / "export.tar", GOOD_FILES, symlinks=GOOD_SYMLINKS)
    grype = write_json(tmp_path / "grype.json", grype_doc([grype_match()]))
    assert gate.main(_committed_ledger_argv(export, grype)) == 0
    out = capsys.readouterr().out
    assert f"IMAGE-VEX report-only: unfixed HIGH+ finding {CVE} {PURL} (no fix available)" in out
    assert "IMAGE-VEX exactness:" not in out
