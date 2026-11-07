# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NEB1 envelope: roundtrip, and every tamper/truncation/reorder/swap path fails."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from ne.backup import crypto
from ne.backup.errors import BackupCryptoError, KeyResolutionError


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_roundtrip_multichunk(tmp_path: Path) -> None:
    dek = crypto.new_dek()
    plaintext = os.urandom(crypto.CHUNK_SIZE * 2 + 1234)  # >2 full chunks
    src = _write(tmp_path / "src", plaintext)
    enc = tmp_path / "src.neb"
    crypto.encrypt_file(dek, "artifact-a", src, enc)
    assert enc.read_bytes()[:4] == b"NEB1"
    out = tmp_path / "out"
    crypto.decrypt_file(dek, "artifact-a", enc, out)
    assert out.read_bytes() == plaintext


def test_empty_file_roundtrips(tmp_path: Path) -> None:
    dek = crypto.new_dek()
    src = _write(tmp_path / "empty", b"")
    enc = tmp_path / "empty.neb"
    crypto.encrypt_file(dek, "empty", src, enc)
    out = tmp_path / "out"
    crypto.decrypt_file(dek, "empty", enc, out)
    assert out.read_bytes() == b""


def test_wrong_dek_fails_before_output(tmp_path: Path) -> None:
    dek = crypto.new_dek()
    src = _write(tmp_path / "src", b"hello world" * 1000)
    enc = tmp_path / "src.neb"
    crypto.encrypt_file(dek, "a", src, enc)
    out = tmp_path / "out"
    with pytest.raises(InvalidTag):
        crypto.decrypt_file(crypto.new_dek(), "a", enc, out)


def test_wrong_kek_unwrap_raises_invalidtag() -> None:
    kek = os.urandom(32)
    dek = crypto.new_dek()
    wrapped, nonce = crypto.wrap_dek(kek, dek)
    assert crypto.unwrap_dek(kek, wrapped, nonce) == dek
    with pytest.raises(InvalidTag):
        crypto.unwrap_dek(os.urandom(32), wrapped, nonce)


def test_single_byte_tamper_fails(tmp_path: Path) -> None:
    dek = crypto.new_dek()
    src = _write(tmp_path / "src", os.urandom(5000))
    enc = tmp_path / "src.neb"
    crypto.encrypt_file(dek, "a", src, enc)
    raw = bytearray(enc.read_bytes())
    raw[-1] ^= 0x01  # flip a ciphertext byte
    tampered = _write(tmp_path / "t.neb", bytes(raw))
    with pytest.raises(InvalidTag):
        crypto.decrypt_file(dek, "a", tampered, tmp_path / "out")


def test_truncation_drop_last_chunk_fails(tmp_path: Path) -> None:
    dek = crypto.new_dek()
    plaintext = os.urandom(crypto.CHUNK_SIZE * 2 + 10)
    src = _write(tmp_path / "src", plaintext)
    enc = tmp_path / "src.neb"
    crypto.encrypt_file(dek, "a", src, enc)
    # Strip the trailing frame (len prefix + ciphertext) of the final chunk by
    # re-encoding: parse frames and drop the last.
    raw = enc.read_bytes()
    body = raw[4 + crypto.NONCE_PREFIX_LEN :]
    frames: list[bytes] = []
    i = 0
    while i < len(body):
        clen = int.from_bytes(body[i : i + 4], "big")
        frames.append(body[i : i + 4 + clen])
        i += 4 + clen
    truncated = raw[: 4 + crypto.NONCE_PREFIX_LEN] + b"".join(frames[:-1])
    bad = _write(tmp_path / "trunc.neb", truncated)
    # The new last chunk was encrypted without the :final marker → decrypting it
    # as final fails the AEAD tag.
    with pytest.raises(InvalidTag):
        crypto.decrypt_file(dek, "a", bad, tmp_path / "out")


def test_chunk_reorder_fails(tmp_path: Path) -> None:
    dek = crypto.new_dek()
    plaintext = os.urandom(crypto.CHUNK_SIZE * 2 + 7)
    src = _write(tmp_path / "src", plaintext)
    enc = tmp_path / "src.neb"
    crypto.encrypt_file(dek, "a", src, enc)
    raw = enc.read_bytes()
    header = raw[: 4 + crypto.NONCE_PREFIX_LEN]
    body = raw[4 + crypto.NONCE_PREFIX_LEN :]
    frames: list[bytes] = []
    i = 0
    while i < len(body):
        clen = int.from_bytes(body[i : i + 4], "big")
        frames.append(body[i : i + 4 + clen])
        i += 4 + clen
    frames[0], frames[1] = frames[1], frames[0]  # swap the first two full chunks
    bad = _write(tmp_path / "reorder.neb", header + b"".join(frames))
    with pytest.raises(InvalidTag):
        crypto.decrypt_file(dek, "a", bad, tmp_path / "out")


def test_ciphertext_swap_between_artifacts_fails(tmp_path: Path) -> None:
    dek = crypto.new_dek()
    src = _write(tmp_path / "src", os.urandom(4000))
    enc = tmp_path / "src.neb"
    crypto.encrypt_file(dek, "artifact-a", src, enc)
    # Decrypting the same ciphertext under a DIFFERENT artifact name fails because
    # the name is in the AAD.
    with pytest.raises(InvalidTag):
        crypto.decrypt_file(dek, "artifact-b", enc, tmp_path / "out")


def test_bad_magic_raises_structural(tmp_path: Path) -> None:
    dek = crypto.new_dek()
    bad = _write(tmp_path / "x.neb", b"XXXX" + os.urandom(20))
    with pytest.raises(BackupCryptoError):
        crypto.decrypt_file(dek, "a", bad, tmp_path / "out")


def test_dek_wrap_unwrap_and_fingerprint_stability() -> None:
    kek = os.urandom(32)
    assert crypto.kek_fingerprint(kek) == crypto.kek_fingerprint(kek)
    assert crypto.kek_fingerprint(kek) != crypto.kek_fingerprint(os.urandom(32))
    assert len(crypto.kek_fingerprint(kek)) == 16


def test_decode_kek_enforces_length() -> None:
    import base64

    good = base64.b64encode(os.urandom(32)).decode()
    assert len(crypto.decode_kek(good)) == 32
    with pytest.raises(KeyResolutionError):
        crypto.decode_kek(base64.b64encode(os.urandom(16)).decode())
    with pytest.raises(KeyResolutionError):
        crypto.decode_kek("not-valid-base64!!!")
