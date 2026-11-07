# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-086 — prove a built Zarf package tarball carries what C2/C11 demand.

Fail-loud assertions run against a `zarf package create` output tarball:

  (a) `zarf package inspect definition <pkg>` parses, and its
      components[0].images set EXACTLY equals the --expect-image set. Every
      entry must be a resolved digest pin (repo:tag@sha256:<64hex>) — a
      create-time ### template that survived into the built package is a hard
      failure (the first-party --set token MUST have been resolved at create).

  (b) The built definition retains the exact three Pilot protected-generation
      payload targets: ne-cli (the `ne agent-transport cutover` command),
      ne-backup, and the Pilot Compose contract.

  (c) The raw tar listing contains Zarf's vendored OCI image store
      (images/index.json + an images/blobs/sha256/ prefix) AND the vendored
      Helm chart archive (charts/*neutron-enterprise*<version>*.tgz).

  (d) An OK summary (image count + Pilot payloads + chart entry) is printed.

Exit 1 on any failure. Stdlib + PyYAML only (PyYAML is already an ne-cli test
dep); runs via `uv run python`. Zarf itself is a C4 customer-side/CI tool.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import NoReturn

_RESOLVED_PIN = re.compile(r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$")
_CHART_ENTRY = re.compile(r"charts/.*neutron-enterprise.*0\.1\.0.*\.tgz")
_REQUIRED_PILOT_CUTOVER_TARGETS = {
    "/opt/neutron/pilot-cutover/tools/ne-cli",
    "/opt/neutron/pilot-cutover/tools/ne-backup",
    "/opt/neutron/pilot-cutover/deploy/compose/pilot/compose.yaml",
}
#: Strip ANSI/VT100 control sequences (CSI + a few 2-char forms). `zarf` colorizes
#: its output even to a pipe under some builds, and PyYAML rejects the ESC (0x1b)
#: byte ("unacceptable character #x001b") — so we scrub before parsing AND pass
#: NO_COLOR/TERM=dumb to the process (belt-and-suspenders; either alone suffices).
_ANSI = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assert a built Zarf package's contents (NE-086).")
    parser.add_argument("--package", required=True, help="Path to the zarf-package-*.tar.zst.")
    parser.add_argument("--zarf-bin", default="zarf", help="Zarf binary (default: zarf on PATH).")
    parser.add_argument(
        "--expect-image",
        dest="expect_images",
        action="append",
        required=True,
        help="A digest-pinned image expected in the package (repeatable).",
    )
    parser.add_argument("--chart-name", default="neutron-enterprise")
    parser.add_argument("--chart-version", default="0.1.0")
    return parser.parse_args(argv)


def _fail(msg: str) -> NoReturn:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _inspect_definition(zarf_bin: str, package: str) -> dict:
    import yaml

    result = subprocess.run(
        [zarf_bin, "package", "inspect", "definition", package],
        capture_output=True,
        text=True,
        check=False,
        # Suppress zarf's ANSI colorization so the YAML on stdout is clean.
        env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
    )
    if result.returncode != 0:
        _fail(f"`zarf package inspect definition` exited {result.returncode}: {result.stderr}")
    # Scrub any residual control sequences before parsing (PyYAML rejects ESC).
    cleaned = _ANSI.sub("", result.stdout)
    try:
        doc = yaml.safe_load(cleaned)
    except yaml.YAMLError as exc:
        _fail(f"could not parse zarf inspect YAML: {exc}")
    if not isinstance(doc, dict):
        _fail("zarf inspect definition did not yield a mapping")
    return doc


def _package_images(definition: dict) -> list[str]:
    components = definition.get("components") or []
    if not components:
        _fail("built package definition has no components")
    return list(components[0].get("images") or [])


def _package_file_targets(definition: dict) -> set[str]:
    components = definition.get("components") or []
    targets: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            _fail("built package definition has a malformed component")
        files = component.get("files") or []
        if not isinstance(files, list):
            _fail("built package definition has a malformed files list")
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("target"), str):
                _fail("built package definition has a malformed file payload")
            targets.add(item["target"])
    return targets


def _tar_listing(package: str) -> list[str]:
    result = subprocess.run(
        ["tar", "--use-compress-program=unzstd", "-tf", package],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _fail(f"`tar -tf` on the package exited {result.returncode}: {result.stderr}")
    return result.stdout.splitlines()


def _nested_tar_listing(package: str, member: str) -> list[str]:
    """List the entries of a component tar nested inside the .tar.zst package.

    Zarf vendors each component as its own `components/<name>.tar` member, so a
    helm component's chart archive (charts/<name>-<version>.tgz) is INSIDE that
    nested tar, not at the outer package root — extract the member to stdout and
    pipe it through a second `tar -tf -`.
    """
    outer = subprocess.Popen(
        ["tar", "--use-compress-program=unzstd", "-xOf", package, member],
        stdout=subprocess.PIPE,
    )
    inner = subprocess.run(
        ["tar", "-tf", "-"], stdin=outer.stdout, capture_output=True, text=True, check=False
    )
    if outer.stdout is not None:
        outer.stdout.close()
    outer.wait()
    if inner.returncode != 0:
        return []
    return inner.stdout.splitlines()


def _find_chart(package: str, listing: list[str]) -> str | None:
    """Locate the vendored chart archive at the package root OR inside any nested
    `components/*.tar` member (Zarf's actual layout). Returns the matching path
    (prefixed with the component member for nested hits) or None."""
    direct = [e for e in listing if _CHART_ENTRY.search(e)]
    if direct:
        return direct[0]
    for member in (e for e in listing if re.fullmatch(r"components/.*\.tar", e)):
        for inner in _nested_tar_listing(package, member):
            if _CHART_ENTRY.search(inner):
                return f"{member}!{inner}"
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    expected = set(args.expect_images)

    # (a) resolved image set == expected set, every entry digest-pinned.
    definition = _inspect_definition(args.zarf_bin, args.package)
    images = _package_images(definition)
    for image in images:
        if "###" in image:
            _fail(f"unresolved create-time template in built package: {image!r}")
        if not _RESOLVED_PIN.match(image):
            _fail(f"image not a resolved digest pin: {image!r}")
    actual = set(images)
    if actual != expected:
        _fail(
            "package image set != expected\n"
            f"  missing: {sorted(expected - actual)}\n"
            f"  extra:   {sorted(actual - expected)}"
        )

    # (b) the same disconnected package carries the inert Pilot cutover kit.
    file_targets = _package_file_targets(definition)
    missing_targets = _REQUIRED_PILOT_CUTOVER_TARGETS - file_targets
    if missing_targets:
        _fail(f"built package is missing Pilot cutover payload(s): {sorted(missing_targets)}")

    # (c) tar listing carries the OCI image store + the vendored chart tgz.
    listing = _tar_listing(args.package)
    if not any(entry.endswith("images/index.json") for entry in listing):
        _fail("no images/index.json in the package tar (OCI image store missing)")
    if not any("images/blobs/sha256/" in entry for entry in listing):
        _fail("no images/blobs/sha256/ prefix in the package tar (image blobs missing)")
    chart_entry = _find_chart(args.package, listing)
    if chart_entry is None:
        # Dump the actual package layout so a Zarf-layout change is diagnosable in
        # one CI round-trip rather than a blind guess.
        listing_dump = "\n".join(f"    {e}" for e in listing)
        _fail(
            "no vendored chart archive matching "
            f"charts/.*{args.chart_name}.*{args.chart_version}.*.tgz at the package root "
            "or inside any components/*.tar\n"
            f"  package tar listing:\n{listing_dump}"
        )

    # (d) OK summary.
    print(f"OK: {len(actual)} digest-pinned image(s) present:")
    for image in sorted(actual):
        print(f"  - {image}")
    print(f"OK: {len(_REQUIRED_PILOT_CUTOVER_TARGETS)} Pilot cutover payload target(s) present")
    print(f"OK: vendored chart archive present: {chart_entry}")
    print("OK: images/index.json + images/blobs/sha256/ present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
