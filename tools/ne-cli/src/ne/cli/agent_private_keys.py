# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Agent Run private-payload KEK lifecycle (`ne private-payload-keys`).

The KEK lives ONLY at ``pilot/agent-run-private-payload-kek`` in the bundle's
SecretStore -- the same store ``ne up`` reads for the pilot passwords. There is no
environment-variable or flag path for raw key material: the SecretStore is the only
source, and every command here returns a NON-REVERSIBLE fingerprint, never the key.
Modelled on ``ne.backup.keys`` (NE-BACKUP), with the same discipline: CSPRNG inside
the function, key bytes never returned, refuse to clobber without ``--rotate``.

ROTATION IS NOT A STORE OPERATION. This repository's ``SecretStore`` protocol has
no compare-and-swap -- ``read``/``write``/``list_secrets`` only -- so the
compare-and-swap that makes rotation safe is the DATABASE-side one over the
DEK-envelope rows: ``AgentRunStore.rewrap_private_payload_keys`` refuses unless
every stored row is under the exact expected old key id, rewraps them in one
transaction, and proves zero old references before committing. Do not read "CAS"
here as a Vault ``options.cas`` write; it is not, and inventing one on the protocol
would be a lie about what the store guarantees.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path
from typing import Any

import click

#: The one SecretStore location for the Agent Run private-payload KEK.
KEK_PATH = "pilot/agent-run-private-payload-kek"
KEY_ID_FIELD = "key_id"
KEK_FIELD = "kek"

#: Env-var NAMES delivered to the runtime by `ne up` / `ne k8s up`. NAMES ONLY --
#: a value never appears in this module, a manifest, a config hash or a log (C12).
ENV_KEY_ID = "NE_AGENT_PRIVATE_PAYLOAD_KEY_ID"
ENV_KEK_B64 = "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64"

KEK_LEN = 32


def encode_kek(raw: bytes) -> str:
    """Canonical UNPADDED base64url, the one on-the-wire form for this key."""
    if len(raw) != KEK_LEN:
        raise ValueError(f"private-payload KEK must be {KEK_LEN} bytes")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_kek(text: str) -> bytes:
    """Decode a canonical unpadded base64url KEK, rejecting every other spelling.

    Padding-REJECTING and round-trip checked, modelled on ne-brain-graph's cursor
    decoder rather than on ne-backup's padded-standard ``decode_kek``: a key that
    can be written two ways is a key whose id can disagree with its bytes across
    two deployments that both "work".
    """
    if not text or "=" in text:
        raise ValueError("private-payload KEK must be canonical unpadded base64url")
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("private-payload KEK is not valid base64url") from exc
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != text:
        raise ValueError("private-payload KEK is not canonical base64url")
    if len(raw) != KEK_LEN:
        raise ValueError(f"private-payload KEK must decode to {KEK_LEN} bytes")
    return raw


def kek_fingerprint(kek: bytes) -> str:
    """A short, NON-REVERSIBLE fingerprint for reporting and drift detection.

    Salted truncated SHA-256 -- enough to tell two keys apart in `status` output
    without revealing anything usable about the material.
    """
    return hashlib.sha256(b"ne-agent-private-payload-kek-v1:" + kek).hexdigest()[:16]


def _store_from_bundle(bundle: Path) -> Any:
    """The bundle's SecretStore, constructed exactly as `ne up` constructs it."""
    from ne.secrets import SecretsSettings, create_secret_store

    settings = SecretsSettings.from_config_file(bundle / "config" / "ne-secrets.toml")
    return create_secret_store(settings)


async def _read_version(store: Any) -> Any | None:
    """The current SecretVersion at KEK_PATH, or None when the path is absent."""
    from ne.contracts.errors import SecretNotFoundError

    try:
        return await store.read(KEK_PATH)
    except SecretNotFoundError:
        return None


def _require_fields(version: Any) -> tuple[str, bytes]:
    """The exact (key_id, kek) pair from a SecretVersion, or a loud failure."""
    key_id = (version.data or {}).get(KEY_ID_FIELD)
    kek_b64 = (version.data or {}).get(KEK_FIELD)
    if not key_id:
        raise click.ClickException(f"secret '{KEK_PATH}' has no '{KEY_ID_FIELD}' field")
    if not kek_b64:
        raise click.ClickException(f"secret '{KEK_PATH}' has no '{KEK_FIELD}' field")
    try:
        return str(key_id), decode_kek(str(kek_b64))
    except ValueError as exc:
        raise click.ClickException(f"secret '{KEK_PATH}' holds an invalid KEK: {exc}") from exc


def _new_key_id(existing: str | None) -> str:
    """A fresh, ordered key id. Ordering makes rotation history readable."""
    generation = 1
    if existing and existing.startswith("k") and existing[1:].isdigit():
        generation = int(existing[1:]) + 1
    return f"k{generation}"


@click.group(name="private-payload-keys")
def private_payload_keys() -> None:
    """Provision, inspect and rotate the Agent Run private-payload KEK."""


@private_payload_keys.command("provision")
@click.option(
    "--bundle",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Pilot bundle whose SecretStore holds the KEK.",
)
def provision(bundle: Path) -> None:
    """Generate the KEK and write it to the bundle SecretStore.

    Refuses to clobber an existing key: replacing it without rewrapping orphans
    every stored private payload. Use `rotate` for a deliberate replacement.
    """
    import asyncio

    store = _store_from_bundle(bundle)

    async def _run() -> tuple[str, str, int]:
        if await _read_version(store) is not None:
            raise click.ClickException(
                f"a private-payload KEK already exists at '{KEK_PATH}'. Replacing it "
                "without rewrapping orphans every stored private payload. To rotate "
                "deliberately, use `ne private-payload-keys rotate`."
            )
        kek = os.urandom(KEK_LEN)
        key_id = _new_key_id(None)
        written = await store.write(KEK_PATH, {KEY_ID_FIELD: key_id, KEK_FIELD: encode_kek(kek)})
        return key_id, kek_fingerprint(kek), int(written.version)

    key_id, fingerprint, version = asyncio.run(_run())
    click.echo(
        f"provisioned {KEK_PATH} key_id={key_id} fingerprint={fingerprint} version={version}"
    )
    click.echo(f"delivered to the runtime as {ENV_KEY_ID} / {ENV_KEK_B64}")


@private_payload_keys.command("status")
@click.option(
    "--bundle",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Pilot bundle whose SecretStore holds the KEK.",
)
@click.option(
    "--database-url",
    default=None,
    help="Optional runtime database URL; when given, reports stored-row key references.",
)
def status(bundle: Path, database_url: str | None) -> None:
    """Report the current key id, fingerprint and SecretVersion -- never the key.

    With ``--database-url`` it also reports the store's own readiness: how many
    private rows sit under the current key and how many under any other. That is
    the same synchronous seam a later composition root consumes.
    """
    import asyncio

    store = _store_from_bundle(bundle)
    version = asyncio.run(_read_version(store))
    if version is None:
        raise click.ClickException(
            f"no private-payload KEK at '{KEK_PATH}' -- run "
            f"`ne private-payload-keys provision --bundle {bundle}`"
        )
    key_id, kek = _require_fields(version)
    click.echo(
        f"{KEK_PATH} key_id={key_id} fingerprint={kek_fingerprint(kek)} "
        f"version={int(version.version)}"
    )
    if database_url is None:
        return
    readiness = _readiness(database_url, key_id, kek)
    click.echo(
        f"rows_under_current_key={readiness.rows_under_current_key} "
        f"rows_under_other_keys={readiness.rows_under_other_keys} ready={readiness.ready}"
    )


def _readiness(database_url: str, key_id: str, kek: bytes) -> Any:
    """The store's synchronous current-key readiness for this exact key."""
    import sqlalchemy as sa

    from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
    from ne.runtime.run_store import AgentRunStore

    engine = sa.create_engine(database_url)
    try:
        cipher = RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id=key_id, keys={key_id: kek})
        )
        return AgentRunStore(engine, cipher).current_key_readiness()
    finally:
        engine.dispose()


@private_payload_keys.command("rotate")
@click.option(
    "--bundle",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Pilot bundle whose SecretStore holds the KEK.",
)
@click.option(
    "--database-url",
    required=True,
    help="Runtime database URL holding the DEK-envelope rows to rewrap.",
)
@click.option(
    "--roots-stopped",
    is_flag=True,
    default=False,
    help="Confirm the API and worker roots are STOPPED. Required.",
)
def rotate(bundle: Path, database_url: str, roots_stopped: bool) -> None:
    """Rotate the KEK: rewrap every stored DEK, then publish the new key.

    Requires BOTH roots stopped. A running root holds the old key in memory and
    would keep minting rows under it while the rewrap walks the table, so the
    "zero old references" proof below would be true only for an instant.

    The compare-and-swap is the DATABASE one: the rewrap refuses unless every
    stored row is under the exact expected old key id, rewraps them in ONE
    transaction, and verifies zero remaining old references before committing.
    The SecretStore has no CAS, so ordering carries the safety instead -- the new
    secret is published only AFTER the rewrap succeeds, because a published key
    whose rows were never rewrapped is unrecoverable, while a successful rewrap
    with an unpublished key is simply re-runnable.
    """
    import asyncio

    if not roots_stopped:
        raise click.ClickException(
            "refusing to rotate: pass --roots-stopped to confirm the API and worker "
            "roots are stopped. Rotating under a live root leaves rows minted under "
            "the old key after the rewrap has already passed its zero-reference check."
        )
    store = _store_from_bundle(bundle)
    version = asyncio.run(_read_version(store))
    if version is None:
        raise click.ClickException(
            f"no private-payload KEK at '{KEK_PATH}' -- nothing to rotate; run "
            "`ne private-payload-keys provision` first"
        )
    old_key_id, old_kek = _require_fields(version)
    old_version = int(version.version)
    new_key_id = _new_key_id(old_key_id)
    if new_key_id == old_key_id:  # pragma: no cover - _new_key_id always advances
        raise click.ClickException("rotation would not advance the key id")
    new_kek = os.urandom(KEK_LEN)

    rewrapped = _rewrap(database_url, old_key_id, old_kek, new_key_id, new_kek)

    written = asyncio.run(
        store.write(KEK_PATH, {KEY_ID_FIELD: new_key_id, KEK_FIELD: encode_kek(new_kek)})
    )
    click.echo(
        f"rotated {KEK_PATH} {old_key_id}->{new_key_id} "
        f"fingerprint={kek_fingerprint(new_kek)} "
        f"version={old_version}->{int(written.version)} rewrapped={rewrapped} "
        "old_references=0"
    )
    click.echo("refresh the deployment secret and restart the API and worker roots")


def _rewrap(
    database_url: str, old_key_id: str, old_kek: bytes, new_key_id: str, new_kek: bytes
) -> int:
    """One database-side compare-and-swap rewrap; returns the row count."""
    import asyncio

    import sqlalchemy as sa

    from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
    from ne.runtime.run_store import AgentRunStore

    engine = sa.create_engine(database_url)
    try:
        # BOTH keys resolvable for the duration: the rewrap must open every row
        # under the old key and reseal it under the new one in one transaction.
        cipher = RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(
                current_key_id=new_key_id, keys={old_key_id: old_kek, new_key_id: new_kek}
            )
        )
        runs = AgentRunStore(engine, cipher)
        rewrapped = asyncio.run(
            runs.rewrap_private_payload_keys(
                expected_old_key_id=old_key_id, target_key_id=new_key_id
            )
        )
        readiness = runs.current_key_readiness()
        if readiness.rows_under_other_keys:  # pragma: no cover - the rewrap already asserts this
            raise click.ClickException(
                f"rotation left {readiness.rows_under_other_keys} rows under another key"
            )
        return rewrapped
    except ValueError as exc:
        raise click.ClickException(f"rotation refused: {exc}") from exc
    finally:
        engine.dispose()
