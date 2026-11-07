# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Conformance kit run for the sops-codec FileSecretStore — the pilot default.

This exercises the REAL sops + age seam (SopsCodec flag construction, ``.sops.yaml``
rule matching, ciphertext-hides-plaintext). It RUNS IN CI: the ci.yml ``check`` job
installs pinned + sha256-verified sops v3.13.2 + age v1.3.1 and runs this file both
inside the full ``uv run pytest`` suite AND under a dedicated must-run-not-skip
executing gate ("SOPS conformance executing gate"), so the pilot's at-rest
encryption seam can never again silently skip in CI. The ``pytestmark`` skipif
below is a **local-dev convenience only** — it lets a developer without the
binaries skip cleanly; in CI the binaries are present and the gate fails the lane
on any skip. An ephemeral age keypair is generated per test — no committed key
material anywhere.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from ne.contracts.secrets import SecretStore
from ne.contracts.testing.kits.secret_store import SecretStoreContractKit
from ne.secrets.codecs import SopsCodec
from ne.secrets.file_store import FileSecretStore

pytestmark = pytest.mark.skipif(
    shutil.which("sops") is None or shutil.which("age-keygen") is None,
    reason="sops and/or age-keygen not installed",
)

_PUBLIC_KEY_RE = re.compile(r"# public key:\s*(age1\S+)")


def _make_sops_store() -> FileSecretStore:
    d = Path(tempfile.mkdtemp())
    key_file = d / "key.txt"
    result = subprocess.run(
        ["age-keygen", "-o", str(key_file)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    key_text = key_file.read_text() + result.stderr
    match = _PUBLIC_KEY_RE.search(key_text)
    assert match, f"could not find age public key in age-keygen output: {key_text!r}"
    pubkey = match.group(1)

    sops_config = d / ".sops.yaml"
    sops_config.write_text(f"creation_rules:\n  - path_regex: .*\n    age: {pubkey}\n")

    return FileSecretStore(
        d / "secrets.json",
        codec=SopsCodec(config=sops_config, extra_env={"SOPS_AGE_KEY_FILE": str(key_file)}),
    )


class TestFileSecretStoreSopsConformance(SecretStoreContractKit):
    def create_store(self) -> SecretStore:
        return _make_sops_store()

    def test_ciphertext_hides_plaintext(self) -> None:
        store = _make_sops_store()

        async def scenario() -> None:
            await store.write("app/db", {"password": "kit-plain-proof"})

        asyncio.run(scenario())

        raw = store._path.read_bytes()
        assert b"kit-plain-proof" not in raw
        assert b"sops" in raw
