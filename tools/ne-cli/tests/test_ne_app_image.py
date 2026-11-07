# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Static guardian for the first-party ne-app image (NE-103a).

No docker daemon needed: the Dockerfile is parsed textually and asserted against
the packaging contract — every base FROM/COPY --from is tag@sha256 digest-pinned
(C11), the runtime is non-root, the CMD is the exact 103c command-switch seam
(exec-form, no ENTRYPOINT), the uv sync is frozen/prod-only, and every embedded
digest is cross-checked into the README provenance table (the canary.sh↔README
drift-guard pattern). The live build + boot smoke are exercised separately.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
NE_APP_DIR = REPO_ROOT / "deploy" / "docker" / "ne-app"
DOCKERFILE = NE_APP_DIR / "Dockerfile"
DOCKERIGNORE = NE_APP_DIR / "Dockerfile.dockerignore"
README = NE_APP_DIR / "README.md"
BUILD_SCRIPT = REPO_ROOT / "deploy" / "airgap" / "scripts" / "build-ne-app.sh"

_FROM = re.compile(r"^FROM\s+(?P<ref>\S+)(?:\s+AS\s+(?P<stage>\S+))?\s*$", re.MULTILINE)
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}")
_HEADER_SCAN_LINES = 5


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _instructions(prefix: str) -> list[str]:
    lines = []
    for raw in _dockerfile().splitlines():
        stripped = raw.strip()
        if stripped.startswith(prefix + " ") or stripped == prefix:
            lines.append(stripped)
    return lines


def test_dockerfile_exists() -> None:
    assert DOCKERFILE.is_file(), "deploy/docker/ne-app/Dockerfile missing"


def test_every_from_is_digest_pinned_with_named_stage() -> None:
    froms = list(_FROM.finditer(_dockerfile()))
    assert froms, "no FROM instructions parsed"
    for m in froms:
        ref = m.group("ref")
        assert _DIGEST.search(ref), f"FROM not digest-pinned: {ref}"
        # A stage referenced by a later COPY --from=<stage> must be named; every
        # FROM here carries an AS name for clarity/regression safety.
        assert m.group("stage"), f"FROM missing an AS stage name: {ref}"


def test_copy_from_uv_is_digest_pinned() -> None:
    text = _dockerfile()
    match = re.search(r"COPY\s+--from=(ghcr\.io/astral-sh/uv:\S+)", text)
    assert match, "expected a COPY --from=ghcr.io/astral-sh/uv:<ver> line"
    assert _DIGEST.search(match.group(1)), f"uv COPY --from not digest-pinned: {match.group(1)}"


def test_no_add_instruction() -> None:
    assert not _instructions("ADD"), "ADD is forbidden (use COPY — no implicit fetch/unpack)"


def test_exactly_one_numeric_nonroot_user_before_cmd() -> None:
    text = _dockerfile()
    users = _instructions("USER")
    assert len(users) == 1, f"expected exactly one USER directive, got {users}"
    user_arg = users[0].split(None, 1)[1].strip()
    uid = user_arg.split(":", 1)[0]
    assert uid.isdigit(), f"USER must be numeric (non-root by uid), got {user_arg!r}"
    assert uid == "10001", f"expected uid 10001, got {uid}"
    assert int(uid) != 0, "USER must not be root"
    # USER must appear before CMD.
    assert text.index("\nUSER ") < text.index("\nCMD "), "USER must precede CMD"


def test_cmd_is_exact_exec_form() -> None:
    cmds = _instructions("CMD")
    assert cmds == ['CMD ["python", "-m", "ne.api"]'], cmds


def test_no_entrypoint() -> None:
    # No ENTRYPOINT: the 103c ne-worker role is a compose `command:` override.
    assert not _instructions("ENTRYPOINT"), "ENTRYPOINT must be absent (command-switch seam)"


def test_expose_8000_only() -> None:
    exposes = _instructions("EXPOSE")
    assert exposes == ["EXPOSE 8000"], exposes


def test_uv_sync_is_frozen_prod_ne_api_and_ne_worker() -> None:
    text = _dockerfile()
    # The RUN line (flags start with `--`), not a prose comment mentioning uv sync.
    match = re.search(r"uv sync --[^\n]*", text)
    assert match, "no `uv sync --...` RUN line found"
    line = match.group(0)
    # NE-103c FIX-1: the image installs BOTH the ne-api and ne-worker closures
    # (one image, two roles). Without --package ne-worker, `python -m ne.worker`
    # fails `No module named ne.worker` in the pilot/air-gap stack.
    expected = ("--frozen", "--no-dev", "--no-editable", "--package ne-worker")
    for flag in expected:
        assert flag in line, f"uv sync missing {flag}: {line}"


def test_runtime_vendors_parser_builder_payload_but_not_docling_closure() -> None:
    """Installed M365 parsing can build its isolated, deployment-owned venv."""
    text = _dockerfile()
    assert "COPY --from=py-builder /bin/uv /bin/uv" in text
    assert "COPY --from=py-builder /src/packages/ne-brain/parser_env /opt/ne/parser_env" in text
    assert "NE_PARSER_ENV_DIR=/opt/ne/parser_env" in text
    assert "COPY --from=py-builder /src/packages/ne-brain/.parser-venv" not in text
    assert "COPY --from=py-builder /opt/ne/parser-venv" not in text


def test_every_digest_appears_in_readme_provenance() -> None:
    readme = README.read_text(encoding="utf-8")
    digests = set(_DIGEST.findall(_dockerfile()))
    assert digests, "no digests parsed from Dockerfile"
    for digest in digests:
        bare = digest.removeprefix("@")
        assert bare in readme, f"Dockerfile digest {bare} not in ne-app README provenance table"


def test_dockerignore_exists_and_excludes_git_and_node_modules() -> None:
    assert DOCKERIGNORE.is_file(), "Dockerfile.dockerignore missing"
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    lines = {line.strip() for line in text.splitlines()}
    assert ".git" in lines, ".git must be excluded from the build context"
    assert "**/node_modules" in lines, "**/node_modules must be excluded"
    assert "deploy" in lines, "operator Agent registry must stay mounted, never COPY into image"


def _has_license_header(path: Path) -> bool:
    seen_copyright = seen_owner = False
    with path.open(encoding="utf-8") as handle:
        for _, line in zip(range(_HEADER_SCAN_LINES), handle, strict=False):
            if "Copyright (c)" in line:
                seen_copyright = True
            if "Ryan Junee" in line:
                seen_owner = True
    return seen_copyright and seen_owner


def test_license_headers_present() -> None:
    for path in (DOCKERFILE, BUILD_SCRIPT):
        assert _has_license_header(path), f"missing source header: {path}"
