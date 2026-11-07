# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""One-way bridge from dev ``.env`` files into a versioned SecretStore.

This is the "env-file" half of the SPEC secrets row: developers keep a local
``.env`` for convenience, and :func:`import_env_file` imports it into the
versioned store as a single write.
"""

from __future__ import annotations

from pathlib import Path

from ne.contracts.secrets import SecretStore, SecretVersion


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file into a dict.

    Blank lines and ``#``-prefixed comment lines are skipped. An optional
    ``export `` prefix is stripped. A single layer of matching single or
    double quotes around the value is stripped. A malformed line (no ``=``)
    raises ``ValueError`` naming the line number — NEVER the line content,
    which may hold a secret.
    """
    result: dict[str, str] = {}
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            raise ValueError(f"malformed env file line {lineno}: missing '='")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


async def import_env_file(store: SecretStore, env_path: Path, secret_path: str) -> SecretVersion:
    """Parse ``env_path`` and write it as one new version at ``secret_path``."""
    data = parse_env_file(env_path)
    return await store.write(secret_path, data)
