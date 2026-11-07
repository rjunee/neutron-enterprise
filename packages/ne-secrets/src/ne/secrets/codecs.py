# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""On-disk codecs for :class:`~ne.secrets.file_store.FileSecretStore`.

``PlainJsonCodec`` is a dev/CI codec with no encryption — it runs fully in CI.
``SopsCodec`` shells out to the ``sops`` binary, which supplies the
customer-KMS envelope (age / AWS KMS / Azure Key Vault / GCP KMS) via a
``.sops.yaml`` creation-rules file. Both write atomically (tempfile + chmod
0o600 + os.replace) so a crash never leaves a partially-written document.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from ne.secrets.errors import SecretsBackendError


def _empty_doc() -> dict[str, Any]:
    return {"format": "ne-secrets/kv2", "format_version": 1, "secrets": {}}


def _atomic_write(path: Path, raw: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


class SecretFileCodec(Protocol):
    """Reads/writes the ``ne-secrets/kv2`` JSON document to/from disk."""

    def load(self, path: Path) -> dict[str, Any]: ...

    def dump(self, doc: dict[str, Any], path: Path) -> None: ...


class PlainJsonCodec:
    """No encryption — dev/CI codec. Runs unconditionally in CI."""

    def load(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return _empty_doc()
        return json.loads(path.read_text(encoding="utf-8"))

    def dump(self, doc: dict[str, Any], path: Path) -> None:
        raw = json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")
        _atomic_write(path, raw)


class SopsCodec:
    """Encrypts/decrypts the document via the ``sops`` binary.

    The customer-KMS envelope (age / AWS KMS / Azure Key Vault / GCP KMS) is
    supplied entirely by ``sops`` through its ``.sops.yaml`` creation rules —
    this codec does not implement any cryptography itself.
    """

    def __init__(
        self,
        binary: str = "sops",
        config: Path | None = None,
        extra_env: Mapping[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._binary = binary
        self._config = config
        self._extra_env = dict(extra_env or {})
        self._timeout = timeout

    def _env(self) -> dict[str, str]:
        return {**os.environ, **self._extra_env}

    def load(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return _empty_doc()
        try:
            result = subprocess.run(
                [
                    self._binary,
                    "--decrypt",
                    "--input-type",
                    "json",
                    "--output-type",
                    "json",
                    str(path),
                ],
                capture_output=True,
                env=self._env(),
                # stdin=DEVNULL: an age/PGP backend that would prompt on inherited
                # stdin gets EOF instead of blocking the worker forever.
                stdin=subprocess.DEVNULL,
                timeout=self._timeout,
            )
        except FileNotFoundError as exc:
            raise SecretsBackendError(f"sops binary '{self._binary}' not found") from exc
        except subprocess.TimeoutExpired as exc:
            # Message must NOT include stdout/stderr (may carry plaintext).
            raise SecretsBackendError(f"sops --decrypt timed out after {self._timeout}s") from exc
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-2000:]
            raise SecretsBackendError(f"sops --decrypt failed: {stderr_tail}")
        return json.loads(result.stdout.decode("utf-8"))

    def dump(self, doc: dict[str, Any], path: Path) -> None:
        fd, tmp_plain_name = tempfile.mkstemp(dir=path.parent)
        tmp_plain = Path(tmp_plain_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(json.dumps(doc, indent=2, sort_keys=True).encode("utf-8"))
            os.chmod(tmp_plain, 0o600)

            cmd = [
                self._binary,
                "--encrypt",
                "--input-type",
                "json",
                "--output-type",
                "json",
                # Match creation rules against the DESTINATION filename, not the
                # random tempfile name — a path-specific `creation_rules.path_regex`
                # (e.g. `secrets\.ne\.json$`) must still select the right rule.
                # Supported since sops v3.9.0; this package requires >= 3.10.
                "--filename-override",
                str(path),
                *(["--config", str(self._config)] if self._config else []),
                str(tmp_plain),
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    env=self._env(),
                    stdin=subprocess.DEVNULL,
                    timeout=self._timeout,
                )
            except FileNotFoundError as exc:
                raise SecretsBackendError(f"sops binary '{self._binary}' not found") from exc
            except subprocess.TimeoutExpired as exc:
                raise SecretsBackendError(
                    f"sops --encrypt timed out after {self._timeout}s"
                ) from exc
            if result.returncode != 0:
                stderr_tail = result.stderr.decode("utf-8", errors="replace")[-2000:]
                raise SecretsBackendError(f"sops --encrypt failed: {stderr_tail}")
            _atomic_write(path, result.stdout)
        finally:
            # Plaintext must never survive on disk.
            tmp_plain.unlink(missing_ok=True)
