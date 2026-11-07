# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Key-based cosign sign/verify roundtrip (offline, no Rekor).

Skips locally when cosign is absent; runs in CI where install-scanners.sh
provides a pinned cosign. This is a LIVE-PARKABLE proof: it exercises the exact
offline key-based flow customers use via verify-release.sh.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("cosign") is None, reason="cosign not installed")


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


def test_cosign_keybased_roundtrip(tmp_path: Path) -> None:
    env = {**os.environ, "COSIGN_PASSWORD": "test-password"}

    keygen = _run(["cosign", "generate-key-pair"], tmp_path, env)
    assert keygen.returncode == 0, keygen.stderr
    key = tmp_path / "cosign.key"
    pub = tmp_path / "cosign.pub"
    assert key.is_file() and pub.is_file()

    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"neutron enterprise release artifact")
    bundle = tmp_path / "artifact.bin.cosign.bundle"

    sign = _run(
        [
            "cosign",
            "sign-blob",
            "--yes",
            "--key",
            str(key),
            "--tlog-upload=false",
            "--bundle",
            str(bundle),
            str(artifact),
        ],
        tmp_path,
        env,
    )
    assert sign.returncode == 0, sign.stderr
    assert bundle.is_file()

    verify = _run(
        [
            "cosign",
            "verify-blob",
            "--key",
            str(pub),
            "--bundle",
            str(bundle),
            "--insecure-ignore-tlog=true",
            str(artifact),
        ],
        tmp_path,
        env,
    )
    assert verify.returncode == 0, verify.stderr

    # Tampering must fail verification.
    artifact.write_bytes(b"tampered payload")
    tampered = _run(
        [
            "cosign",
            "verify-blob",
            "--key",
            str(pub),
            "--bundle",
            str(bundle),
            "--insecure-ignore-tlog=true",
            str(artifact),
        ],
        tmp_path,
        env,
    )
    assert tampered.returncode != 0, "verification must fail on a tampered artifact"
