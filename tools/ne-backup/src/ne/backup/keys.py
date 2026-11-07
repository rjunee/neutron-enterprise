# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Customer-managed KEK resolution from the bundle SecretStore (C12).

The KEK lives ONLY at ``pilot/backup`` field ``key_b64`` in the bundle's
SecretStore — the SAME store ``ne up`` reads for the pilot passwords. There is no
environment-variable or flag path for raw key material: the SecretStore is the
only source, and :func:`keygen` returns the fingerprint, never the key.
"""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

from ne.backup.crypto import KEK_LEN, decode_kek, kek_fingerprint
from ne.backup.errors import KeyExistsError, KeyResolutionError
from ne.contracts.errors import SecretNotFoundError
from ne.contracts.secrets import SecretStore
from ne.secrets import SecretsSettings, create_secret_store

KEK_PATH = "pilot/backup"
KEK_FIELD = "key_b64"


def store_from_bundle(bundle: Path) -> SecretStore:
    """Construct the bundle's SecretStore exactly as ``ne up`` does — from
    ``<bundle>/config/ne-secrets.toml``."""
    settings = SecretsSettings.from_config_file(bundle / "config" / "ne-secrets.toml")
    return create_secret_store(settings)


def resolve_kek(bundle: Path, store: SecretStore | None = None) -> bytes:
    """Read and validate the 32-byte customer KEK from the bundle SecretStore.

    ``store`` may be injected in tests; production resolves it from the bundle.
    """
    resolved = store or store_from_bundle(bundle)

    async def _read() -> str:
        try:
            version = await resolved.read(KEK_PATH)
        except SecretNotFoundError as error:
            raise KeyResolutionError(
                f"no backup KEK at '{KEK_PATH}' — run `ne-backup keygen --bundle {bundle}`"
            ) from error
        key_b64 = version.data.get(KEK_FIELD)
        if not key_b64:
            raise KeyResolutionError(f"secret '{KEK_PATH}' has no '{KEK_FIELD}' field")
        return key_b64

    return decode_kek(asyncio.run(_read()))


async def _kek_present(store: SecretStore) -> bool:
    """True if a usable KEK already lives at ``pilot/backup`` in this store."""
    try:
        version = await store.read(KEK_PATH)
    except SecretNotFoundError:
        return False
    return bool(version.data.get(KEK_FIELD))


def keygen(bundle: Path, store: SecretStore | None = None, *, rotate: bool = False) -> str:
    """Generate a fresh 32-byte KEK, write it to the bundle SecretStore, and
    return ONLY its fingerprint. The key bytes never leave this function.

    Refuses to clobber an existing KEK unless ``rotate`` is set. Provisioning a
    new key is a once-per-bundle act (see the runbook); silently replacing the
    key would orphan every prior backup — they were fingerprinted under the old
    key and ``resolve_kek`` only ever reads the latest, so they would fail the
    fingerprint check and become unrestorable through the tool. Rotation must be
    an explicit, deliberate choice.
    """
    resolved = store or store_from_bundle(bundle)

    async def _run() -> bytes:
        if not rotate and await _kek_present(resolved):
            raise KeyExistsError(
                f"a backup KEK already exists at '{KEK_PATH}' in this bundle. "
                "Replacing it orphans every existing backup (they become "
                "unrestorable). To deliberately rotate, re-run with --rotate."
            )
        kek = os.urandom(KEK_LEN)
        await resolved.write(KEK_PATH, {KEK_FIELD: base64.b64encode(kek).decode("ascii")})
        return kek

    return kek_fingerprint(asyncio.run(_run()))
