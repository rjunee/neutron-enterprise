# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Repo-wide CVE remediation floors on every separately-resolved Python lockfile.

Trivy (the fail-closed ``vuln-scan`` CI job) is the DISCOVERY mechanism for new
advisories; this test is the REGRESSION guard that keeps an already-taken
remediation from silently backsliding. The sidecar-env ``requirements.txt``
files are resolved independently of the platform ``uv.lock``, so each one
carries its OWN copy of any vulnerable pin — a recompile of a single env (or a
``uv lock`` re-resolve) can quietly land below a known-fixed version and stay
green until Trivy's DB catches up. Precedent: the dev-compose image pins
asserted in ``tools/ne-cli/tests/test_dev_stack.py`` and the presidio-local
cryptography floor in ``packages/ne-govern/tests/test_presidio_env_policy.py``.

Floors are remediations that already shipped — NEVER lower one. Raise a floor
when a new advisory's fixed version lands in the lockfiles. The Vast CLI
offline closure (``vastai-offline-lock.json``) has its own table because it
is not a requirements lockfile.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_UV_LOCK = _REPO_ROOT / "uv.lock"
_VAST_LOCK = _REPO_ROOT / "deploy/inference-operator/vastai-offline-lock.json"

# package -> (minimum fixed version, advisory)
_FLOORS: dict[str, tuple[tuple[int, ...], str]] = {
    # OOB heap read in the C HTTP response parser's error path.
    "aiohttp": ((3, 14, 3), "CVE-2026-69244"),
    # PKCS#7 EnvelopedData Bleichenbacher padding oracle.
    "cryptography": ((50, 0, 0), "CVE-2026-69247"),
    # GitPython .gitmodules/git-config/clone/revision-option parsing on untrusted git input
    # (CVE-2026-78675, fixed in 3.1.59); GHSA-59cr-6r3x-644w .gitmodules `path` residual (3.1.62).
    # 3.2.0 closes six maintainer repository-level advisories of 2026-09-30 — none GitHub-reviewed,
    # so invisible to trivy/grype/osv: GHSA-f9j4-qggq-h239 HIGH 8.1 (Remote.pull() refspec bypasses
    # the --upload-pack unsafe-option guard -> command execution), GHSA-w8jc-g24h-crhw HIGH 7.5
    # (Git.ls_remote accepts ext:: URLs), GHSA-23mf-xhv8-69c2 HIGH (arbitrary file read via branch
    # name in Head.checkout()), GHSA-fx3j-rwgx-fr94 MEDIUM 6.5, GHSA-m64x-33q8-m5h7 MEDIUM,
    # GHSA-gq48-pqfc-9p58 MEDIUM. Transitive via dlt only; no sidecar pins it.
    "gitpython": ((3, 2, 0), "GHSA-f9j4-qggq-h239"),
    # TLSStream.wrap IDNA-2003 hostname handling (CRITICAL); 4.14.2 also fixes
    # CVE-2026-63349 (open_process extra_groups) and CVE-2026-64847 (to_process deadlock).
    "anyio": ((4, 14, 2), "GHSA-82r6-8w77-94w6 / CVE-2026-63374"),
    # PyJWT maintainer repository-level advisories (absent from GHSA-pip/osv/trivy/grype):
    # 13 of 2026-09-08/11 fixed in 2.14.0 (GHSA-8wjv-2p76-3863 deep-header RecursionError,
    # GHSA-hxm8-2xgr-2p9m, GHSA-r6x4-923q-g947, ...) and 2 of 2026-09-23 fixed in 2.15.0
    # (GHSA-42vr-xj54-vc7v deep-payload RecursionError, GHSA-x33g-cr3x-6449 OKP x/d check).
    "pyjwt": ((2, 15, 0), "GHSA-42vr-xj54-vc7v"),
}
# mcp is deliberately NOT in _FLOORS. GHSA-rwrf-2pqf-9j8j (ClientSession fetched
# server-chosen external ``$ref`` URLs while validating tool results against
# outputSchema; fixed in 1.30.0) is closed in uv.lock and langgraph_env, but crewai_env
# is walled at 1.28.1 by crewai's own ``mcp~=1.28.1`` pin, and a carrier exclusion is a
# floor with a hole. The mcp floor lands with the crewai_env recompile to >=1.30.0,
# together with the _KNOWN_CARRIERS entries.

# Files where the floored packages are KNOWN to resolve today. Discovery must
# find every one of these; if a package legitimately leaves a closure, update
# this table alongside the recompile that removed it.
_KNOWN_CARRIERS: dict[str, tuple[str, ...]] = {
    "packages/ne-brain-graph/lightrag_env/requirements.txt": ("aiohttp", "anyio", "cryptography"),
    "packages/ne-brain/parser_env/requirements.txt": ("anyio",),
    "packages/ne-eval/ragas_env/requirements.txt": ("aiohttp", "anyio"),
    "packages/ne-govern/presidio_env/requirements.txt": ("anyio", "cryptography"),
    "packages/ne-mcp/crewai_env/requirements.txt": ("aiohttp", "anyio", "cryptography", "pyjwt"),
    "packages/ne-mcp/langgraph_env/requirements.txt": ("anyio", "cryptography", "pyjwt"),
    "packages/ne-memory-graph/graphiti_env/requirements.txt": ("anyio",),
}

# Floors for the Vast CLI offline closure ONLY: standalone operator tooling whose
# wheel rows are resolved by hand into vastai-offline-lock.json, not by uv, so
# neither _env_lockfiles() nor uv.lock covers it. urllib3 is deliberately NOT in
# _FLOORS: uv.lock and the sidecar closures still pin 2.7.0 and that repo-wide
# floor lands with PR #1103 (ne/NE-SEC-URLLIB3-0921).
_VAST_LOCK_FLOORS: dict[str, tuple[tuple[int, ...], str]] = {
    # Unbounded chunk-size line buffered by HTTPResponse.stream()/read_chunked();
    # 2.8.0 also fixes GHSA-8988-9cw3-xx77 (HTTPS-proxy TLS override) and
    # GHSA-gh4c-6fx4-qh6g (chunked deflate spin).
    "urllib3": ((2, 8, 0), "GHSA-vxq7-64xx-v4gw"),
}


def _env_lockfiles() -> list[Path]:
    """Every separately-resolved, hash-pinned requirements lockfile."""
    found = (
        set(_REPO_ROOT.glob("packages/*/*_env/requirements.txt"))
        | set(_REPO_ROOT.glob("tools/*/*_env/requirements.txt"))
        | {_REPO_ROOT / "packages/ne-schema/codegen/requirements.txt"}
    )
    return sorted(path for path in found if path.is_file())


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.split("."):
        match = re.match(r"\d+", piece)
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def _vast_lock_pins() -> dict[str, str]:
    document = json.loads(_VAST_LOCK.read_text(encoding="utf-8"))
    return {row["name"]: row["version"] for row in document["wheels"]}


def test_lockfile_discovery_is_live() -> None:
    # Non-vacuity control: the glob must keep finding the files where the
    # floored packages actually resolve; a silent discovery miss would turn
    # the floor test into a no-op.
    discovered = {path.relative_to(_REPO_ROOT).as_posix() for path in _env_lockfiles()}
    missing = set(_KNOWN_CARRIERS) - discovered
    assert not missing, f"env-lockfile discovery lost known carriers: {sorted(missing)}"


def test_env_lockfile_pins_meet_cve_floors() -> None:
    violations: list[str] = []
    seen: dict[str, set[str]] = {name: set() for name in _FLOORS}
    for path in _env_lockfiles():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for name, (floor, advisory) in _FLOORS.items():
            match = re.search(rf"(?m)^{re.escape(name)}==([0-9][0-9A-Za-z.]*)", text)
            if match is None:
                continue
            seen[name].add(rel)
            pinned = match.group(1)
            if _version_tuple(pinned) < floor:
                floor_str = ".".join(map(str, floor))
                violations.append(
                    f"{rel}: {name}=={pinned} is below the {advisory} fix ({floor_str})"
                )
    assert not violations, "\n".join(violations)
    # The floor comparison must actually be exercised where we know the
    # packages resolve; a regex/format drift that stopped matching would
    # otherwise pass vacuously.
    for rel, names in _KNOWN_CARRIERS.items():
        for name in names:
            assert rel in seen[name], f"expected {name} pin in {rel}; regex found none"


def test_uv_lock_pins_meet_cve_floors() -> None:
    # The platform lock resolves the same packages for the shipped runtime
    # (cryptography via ne-identity/ne-connect/ne-chat/ne-backup, aiohttp via
    # litellm/aiobotocore/openfga-sdk, anyio directly via
    # ne-api/ne-worker/ne-skills/ne-skills-cc and via httpx/mcp/openai/
    # sse-starlette) and must hold the same floors.
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    by_name = {package["name"]: package for package in lock["package"]}
    for name, (floor, advisory) in _FLOORS.items():
        package = by_name.get(name)
        assert package is not None, (
            f"{name} left uv.lock entirely; update _FLOORS bookkeeping if intentional"
        )
        version = package["version"]
        floor_str = ".".join(map(str, floor))
        assert _version_tuple(version) >= floor, (
            f"uv.lock: {name}=={version} is below the {advisory} fix ({floor_str})"
        )


def test_vast_offline_lock_pins_meet_cve_floors() -> None:
    rel = _VAST_LOCK.relative_to(_REPO_ROOT).as_posix()
    pins = _vast_lock_pins()
    for name, (floor, advisory) in _VAST_LOCK_FLOORS.items():
        # Non-vacuity control: the floored package must still be a wheel row of the
        # closure; a renamed or dropped row would otherwise pass silently.
        assert name in pins, (
            f"{rel}: {name} left the Vast closure; update _VAST_LOCK_FLOORS if intentional"
        )
        floor_str = ".".join(map(str, floor))
        assert _version_tuple(pins[name]) >= floor, (
            f"{rel}: {name}=={pins[name]} is below the {advisory} fix ({floor_str})"
        )
