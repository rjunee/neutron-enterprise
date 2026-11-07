# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Source-authority continuation key provisioning.

The continuation HMAC key has one fixed home in the Pilot bundle's
``SecretStore``: ``pilot/source-authority-continuation``.  This command is a
one-way provisioning surface, deliberately not a rotation surface.  Once the
path has any version history, provision refuses rather than overwriting,
repairing, undeleting, or resurrecting it.

Key material is generated inside the provisioning coroutine, written directly
to the store, and never returned or printed.  Command output is a closed JSON
metadata receipt containing only the key id, a domain-separated non-reversible
fingerprint, the fixed path, and the exact ``SecretVersion`` number.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Any

import click

SECRET_PATH = "pilot/source-authority-continuation"
KEY_ID_FIELD = "key_id"
HMAC_KEY_FIELD = "hmac_key_b64"
KEY_ID = "sa-k1"
KEY_BYTES = 32

_KEY_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
_EXPECTED_FIELDS = frozenset({KEY_ID_FIELD, HMAC_KEY_FIELD})
_FINGERPRINT_DOMAIN = b"ne-source-authority-continuation-hmac-key-v1:"


def encode_hmac_key(raw: bytes) -> str:
    """Return the one canonical unpadded base64url encoding of a 256-bit key."""
    if len(raw) != KEY_BYTES:
        raise ValueError(f"source-authority continuation key must be {KEY_BYTES} bytes")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_hmac_key(text: str) -> bytes:
    """Decode only the canonical unpadded base64url 256-bit key spelling."""
    if not text or "=" in text:
        raise ValueError("continuation key must be canonical unpadded base64url")
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("continuation key is not valid base64url") from exc
    if encode_hmac_key(raw) != text:
        raise ValueError("continuation key is not canonical base64url")
    return raw


def key_fingerprint(raw: bytes) -> str:
    """Return a domain-separated, non-reversible key fingerprint."""
    if len(raw) != KEY_BYTES:
        raise ValueError(f"source-authority continuation key must be {KEY_BYTES} bytes")
    return "sha256:" + hashlib.sha256(_FINGERPRINT_DOMAIN + raw).hexdigest()


def _store_from_bundle(bundle: Path) -> Any:
    """Construct the bundle SecretStore through the same factory as ``ne up``."""
    from ne.secrets import SecretsSettings, create_secret_store

    settings = SecretsSettings.from_config_file(bundle / "config" / "ne-secrets.toml")
    return create_secret_store(settings)


def _validated_key(version: Any) -> tuple[str, bytes]:
    """Validate the closed two-field secret without including values in errors."""
    data = dict(version.data or {})
    if set(data) != _EXPECTED_FIELDS:
        raise click.ClickException(
            f"secret '{SECRET_PATH}' is malformed; expected exactly the fields "
            f"'{KEY_ID_FIELD}' and '{HMAC_KEY_FIELD}'"
        )
    key_id = data.get(KEY_ID_FIELD)
    encoded = data.get(HMAC_KEY_FIELD)
    if not isinstance(key_id, str) or not _KEY_ID_PATTERN.fullmatch(key_id):
        raise click.ClickException(f"secret '{SECRET_PATH}' has a malformed key id")
    if not isinstance(encoded, str):
        raise click.ClickException(f"secret '{SECRET_PATH}' has malformed key material")
    try:
        raw = decode_hmac_key(encoded)
    except ValueError as exc:
        raise click.ClickException(f"secret '{SECRET_PATH}' has malformed key material") from exc
    return key_id, raw


def _receipt(status: str, version: Any, key_id: str, raw: bytes) -> str:
    """Serialize the closed metadata-only receipt."""
    if version.path != SECRET_PATH or int(version.version) < 1 or bool(version.deleted):
        raise click.ClickException("SecretStore returned an invalid continuation-key receipt")
    return json.dumps(
        {
            "fingerprint": key_fingerprint(raw),
            "key_id": key_id,
            "path": SECRET_PATH,
            "secret_version": int(version.version),
            "status": status,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


async def _path_has_history(store: Any) -> bool:
    """Return whether the exact fixed path exists, including tombstoned history."""
    paths = await store.list_secrets(SECRET_PATH)
    return SECRET_PATH in set(paths)


@click.group(name="source-authority-keys")
def source_authority_keys() -> None:
    """Provision or inspect the source-authority continuation HMAC key."""


@source_authority_keys.command("provision")
@click.option(
    "--bundle",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Pilot bundle whose SecretStore holds the continuation key.",
)
def provision(bundle: Path) -> None:
    """Provision the fixed continuation key once; never rotate or repair it."""
    import asyncio

    store = _store_from_bundle(bundle)

    async def _run() -> tuple[Any, str, bytes]:
        if await _path_has_history(store):
            # Validate an active version so an operator gets a closed malformed
            # diagnosis, but never treat malformed or tombstoned state as absent.
            from ne.contracts.errors import SecretNotFoundError

            try:
                existing = await store.read(SECRET_PATH)
            except SecretNotFoundError as exc:
                raise click.ClickException(
                    f"secret '{SECRET_PATH}' has version history but no readable current "
                    "version; refusing to resurrect or replace it"
                ) from exc
            try:
                _validated_key(existing)
            except click.ClickException as exc:
                raise click.ClickException(
                    f"existing secret '{SECRET_PATH}' is malformed; refusing to replace it"
                ) from exc
            raise click.ClickException(
                f"a continuation key already exists at '{SECRET_PATH}'; refusing to rotate "
                "or replace it"
            )

        raw = secrets.token_bytes(KEY_BYTES)
        encoded = encode_hmac_key(raw)
        written = await store.write(
            SECRET_PATH,
            {KEY_ID_FIELD: KEY_ID, HMAC_KEY_FIELD: encoded},
        )
        # An absent path must produce version 1. A different receipt signals a
        # concurrent/history conflict; never claim successful provisioning.
        if int(written.version) != 1:
            raise click.ClickException(
                "SecretStore did not return the initial continuation-key version; "
                "refusing to claim provisioning success"
            )
        written_key_id, written_raw = _validated_key(written)
        if written_key_id != KEY_ID or written_raw != raw:
            raise click.ClickException(
                "SecretStore returned a continuation-key receipt that does not bind "
                "the requested write"
            )
        return written, written_key_id, written_raw

    version, key_id, raw = asyncio.run(_run())
    click.echo(_receipt("provisioned", version, key_id, raw))


@source_authority_keys.command("status")
@click.option(
    "--bundle",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Pilot bundle whose SecretStore holds the continuation key.",
)
def status(bundle: Path) -> None:
    """Read and validate the current key, emitting metadata only."""
    import asyncio

    from ne.contracts.errors import SecretNotFoundError

    store = _store_from_bundle(bundle)
    try:
        version = asyncio.run(store.read(SECRET_PATH))
    except SecretNotFoundError as exc:
        raise click.ClickException(
            f"no readable continuation key at '{SECRET_PATH}'; run "
            f"`ne source-authority-keys provision --bundle {bundle}`"
        ) from exc
    key_id, raw = _validated_key(version)
    click.echo(_receipt("ready", version, key_id, raw))
