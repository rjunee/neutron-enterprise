# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The NEB1 envelope format — chunked AES-256-GCM with a wrapped per-backup DEK.

Envelope model (C12: the KEK is never persisted here or anywhere on disk):

  * KEK — the 32-byte customer-managed key, held ONLY in the bundle SecretStore.
  * DEK — a per-backup random 32-byte data-encryption key. It is wrapped with the
    KEK (``AESGCM(kek).encrypt(...)``) and the *wrapped* form is stored in
    ``keyinfo.json``. Raw DEK/KEK bytes never touch disk or logs.

    As of the v2 wrap the DEK-wrap AAD authenticates the SHA-256 of the plaintext
    manifest bytes (``manifest_sha256``), so any post-hoc edit of ``manifest.json``
    — including silently dropping a database from the artifact SET — makes the DEK
    unwrap raise ``InvalidTag`` (fail-closed) instead of restoring a truncated set.
    Versioning is confined to ``keyinfo.json`` field presence: a keyinfo WITHOUT a
    ``manifest_sha256`` field is a legacy (pre-v2) backup and is unwrapped under the
    original AAD, so backups written before this change still restore.
  * Per-artifact stream — magic ``NEB1`` + an 8-byte random nonce prefix, then a
    sequence of length-framed AES-256-GCM chunks over 4 MiB plaintext blocks.

Per-chunk AEAD binds:
  * the counter into the 96-bit nonce (``nonce_prefix || counter``) so a chunk
    reorder fails,
  * the artifact name AND the counter (and ``:final`` on the last chunk) into the
    AAD, so a ciphertext swap between two artifacts fails, and dropping the last
    chunk (truncation) fails because the new last chunk lacks the ``:final`` tag.

Everything streams: neither encrypt nor decrypt holds a whole file in memory, and
every output file is created 0600.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ne.backup.errors import BackupCryptoError, KeyResolutionError

MAGIC = b"NEB1"
NONCE_PREFIX_LEN = 8
COUNTER_LEN = 4
CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB plaintext per chunk
KEK_LEN = 32
DEK_LEN = 32

_DEK_AAD = b"ne-backup/NEB1/dek"
_DEK_AAD_MANIFEST_PREFIX = b"ne-backup/NEB1/dek/manifest-sha256:"
_CHUNK_AAD_PREFIX = b"ne-backup/NEB1/chunk:"


def _dek_aad(manifest_sha256: str | None) -> bytes:
    if manifest_sha256 is None:
        return _DEK_AAD
    return _DEK_AAD_MANIFEST_PREFIX + manifest_sha256.encode("ascii")


def kek_fingerprint(kek: bytes) -> str:
    """A short, non-reversible fingerprint of the KEK for manifest/keyinfo binding.

    It is a truncated salted SHA-256 — enough to detect a wrong-key restore before
    any destructive step, while revealing nothing usable about the key material.
    """
    return hashlib.sha256(b"ne-backup-kek-v1:" + kek).hexdigest()[:16]


def decode_kek(key_b64: str) -> bytes:
    """Decode a base64 KEK and enforce the 32-byte length. Raises on any other
    length so a mis-provisioned key fails loudly rather than silently weakening
    the envelope."""
    try:
        kek = base64.b64decode(key_b64, validate=True)
    except (ValueError, TypeError) as error:
        raise KeyResolutionError("KEK is not valid base64") from error
    if len(kek) != KEK_LEN:
        raise KeyResolutionError(f"KEK must decode to {KEK_LEN} bytes, got {len(kek)}")
    return kek


def new_dek() -> bytes:
    """A fresh random 32-byte data-encryption key."""
    return os.urandom(DEK_LEN)


def wrap_dek(kek: bytes, dek: bytes, *, manifest_sha256: str | None = None) -> tuple[str, str]:
    """Wrap the DEK under the KEK. Returns ``(wrapped_dek_b64, nonce_b64)``.

    When ``manifest_sha256`` is given (v2), the manifest hash is folded into the
    AEAD AAD so the wrap authenticates the manifest; ``None`` reproduces the legacy
    AAD for backward compatibility.
    """
    nonce = os.urandom(12)
    wrapped = AESGCM(kek).encrypt(nonce, dek, _dek_aad(manifest_sha256))
    return (
        base64.b64encode(wrapped).decode("ascii"),
        base64.b64encode(nonce).decode("ascii"),
    )


def unwrap_dek(
    kek: bytes, wrapped_dek_b64: str, nonce_b64: str, *, manifest_sha256: str | None = None
) -> bytes:
    """Unwrap the DEK. Raises ``cryptography.exceptions.InvalidTag`` on a wrong
    KEK — or, for a v2 wrap, on any manifest edit — the exact failure a wrong-key
    or manifest-tamper restore must produce before touching data."""
    wrapped = base64.b64decode(wrapped_dek_b64)
    nonce = base64.b64decode(nonce_b64)
    return AESGCM(kek).decrypt(nonce, wrapped, _dek_aad(manifest_sha256))


def _chunk_aad(artifact_name: str, counter: int, *, final: bool) -> bytes:
    aad = _CHUNK_AAD_PREFIX + artifact_name.encode("utf-8") + b":" + str(counter).encode("ascii")
    if final:
        aad += b":final"
    return aad


def _open_0600(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)


def encrypt_file(dek: bytes, artifact_name: str, src: Path, dst: Path) -> None:
    """Encrypt ``src`` into the NEB1 stream ``dst`` (created 0600).

    Read-ahead by one chunk so the final chunk is tagged ``:final`` in its AAD. An
    empty input still produces exactly one (empty, final) chunk, so its integrity
    is authenticated too.
    """
    aes = AESGCM(dek)
    nonce_prefix = os.urandom(NONCE_PREFIX_LEN)
    fd = _open_0600(dst)
    with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
        out.write(MAGIC)
        out.write(nonce_prefix)
        counter = 0
        current = inp.read(CHUNK_SIZE)
        while True:
            nxt = inp.read(CHUNK_SIZE)
            final = nxt == b""
            nonce = nonce_prefix + counter.to_bytes(COUNTER_LEN, "big")
            aad = _chunk_aad(artifact_name, counter, final=final)
            ct = aes.encrypt(nonce, current, aad)
            out.write(len(ct).to_bytes(4, "big"))
            out.write(ct)
            counter += 1
            if final:
                break
            current = nxt


def decrypt_file(dek: bytes, artifact_name: str, src: Path, dst: Path) -> None:
    """Decrypt an NEB1 stream ``src`` into ``dst`` (created 0600).

    Enforces counter order (via the nonce), the artifact name (via AAD), and the
    ``:final`` terminator: a reorder, a cross-artifact ciphertext swap, or a
    dropped last chunk all raise ``InvalidTag``. A stream that never presents a
    final chunk raises :class:`BackupCryptoError` (truncation of the whole tail).
    """
    aes = AESGCM(dek)
    with src.open("rb") as inp:
        if inp.read(4) != MAGIC:
            raise BackupCryptoError(f"artifact {artifact_name!r}: bad NEB1 magic")
        nonce_prefix = inp.read(NONCE_PREFIX_LEN)
        if len(nonce_prefix) != NONCE_PREFIX_LEN:
            raise BackupCryptoError(f"artifact {artifact_name!r}: truncated header")
        fd = _open_0600(dst)
        counter = 0
        saw_final = False
        with os.fdopen(fd, "wb") as out:
            pending_len = inp.read(4)
            while pending_len:
                if len(pending_len) != 4:
                    raise BackupCryptoError(f"artifact {artifact_name!r}: truncated length frame")
                clen = int.from_bytes(pending_len, "big")
                ct = inp.read(clen)
                if len(ct) != clen:
                    raise BackupCryptoError(f"artifact {artifact_name!r}: truncated ciphertext")
                next_len = inp.read(4)
                final = next_len == b""
                nonce = nonce_prefix + counter.to_bytes(COUNTER_LEN, "big")
                aad = _chunk_aad(artifact_name, counter, final=final)
                out.write(aes.decrypt(nonce, ct, aad))  # InvalidTag on tamper/reorder/truncation
                counter += 1
                if final:
                    saw_final = True
                    break
                pending_len = next_len
        if not saw_final:
            raise BackupCryptoError(
                f"artifact {artifact_name!r}: stream ended without a final chunk"
            )


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 of a file (never loads it whole)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
