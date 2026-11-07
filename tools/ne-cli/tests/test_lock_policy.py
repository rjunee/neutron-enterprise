# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the uv.lock pin+hash policy (`ne check lock`)."""

from __future__ import annotations

from pathlib import Path

from ne.cli.lockcheck import find_unhashed

REPO_ROOT = Path(__file__).resolve().parents[3]

_HEX_A = "sha256:" + "a" * 64
_HEX_B = "sha256:" + "b" * 64


def test_real_uv_lock_all_hashes_present() -> None:
    missing = find_unhashed(REPO_ROOT / "uv.lock")
    assert missing == [], f"third-party lock entries without a sha256: {missing}"


def test_synthetic_lock_missing_hash_fails(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "\n".join(
            [
                "version = 1",
                "[[package]]",
                'name = "unhashed-dep"',
                'version = "2.0.0"',
                'source = { registry = "https://pypi.org/simple" }',
                "",
            ]
        ),
        encoding="utf-8",
    )
    assert find_unhashed(lock) == ["unhashed-dep 2.0.0"]


def test_mixed_hash_entry_fails(tmp_path: Path) -> None:
    """An entry where ANY artifact lacks sha256 is reported (all-artifacts rule)."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "\n".join(
            [
                "version = 1",
                "[[package]]",
                'name = "mixed-dep"',
                'version = "3.0.0"',
                'source = { registry = "https://pypi.org/simple" }',
                f'sdist = {{ url = "https://example/x.tar.gz", hash = "{_HEX_A}" }}',
                "wheels = [",
                f'  {{ url = "https://example/a.whl", hash = "{_HEX_B}" }},',
                '  { url = "https://example/b.whl" },',
                "]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    assert find_unhashed(lock) == ["mixed-dep 3.0.0"]


def test_sdist_missing_hash_fails(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "\n".join(
            [
                "version = 1",
                "[[package]]",
                'name = "bad-sdist"',
                'version = "1.0.0"',
                'source = { registry = "https://pypi.org/simple" }',
                'sdist = { url = "https://example/x.tar.gz" }',
                f'wheels = [{{ url = "https://example/a.whl", hash = "{_HEX_B}" }}]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    assert find_unhashed(lock) == ["bad-sdist 1.0.0"]


def _malformed_lock(tmp_path: Path, bad_hash: str) -> Path:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "\n".join(
            [
                "version = 1",
                "[[package]]",
                'name = "malformed-dep"',
                'version = "1.0.0"',
                'source = { registry = "https://pypi.org/simple" }',
                f'sdist = {{ url = "https://example/x.tar.gz", hash = "{bad_hash}" }}',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return lock


def test_malformed_sha256_substring_fails(tmp_path: Path) -> None:
    """A hash string that merely CONTAINS 'sha256' must not pass (fail-open regression)."""
    lock = _malformed_lock(tmp_path, "not-a-real-sha256")
    assert find_unhashed(lock) == ["malformed-dep 1.0.0"]


def test_md5_with_sha256_substring_fails(tmp_path: Path) -> None:
    lock = _malformed_lock(tmp_path, "md5:d41d8cd98f00b204e9800998ecf8427e-sha256")
    assert find_unhashed(lock) == ["malformed-dep 1.0.0"]


def test_sha256_wrong_length_fails(tmp_path: Path) -> None:
    lock = _malformed_lock(tmp_path, "sha256:abc")
    assert find_unhashed(lock) == ["malformed-dep 1.0.0"]


def test_sha256_non_hex_fails(tmp_path: Path) -> None:
    lock = _malformed_lock(tmp_path, "sha256:" + "z" * 64)
    assert find_unhashed(lock) == ["malformed-dep 1.0.0"]


def test_valid_sha256_digest_passes(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "\n".join(
            [
                "version = 1",
                "[[package]]",
                'name = "good-dep"',
                'version = "1.0.0"',
                'source = { registry = "https://pypi.org/simple" }',
                f'sdist = {{ url = "https://example/x.tar.gz", hash = "{_HEX_A}" }}',
                f'wheels = [{{ url = "https://example/a.whl", hash = "{_HEX_B}" }}]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    assert find_unhashed(lock) == []


def test_first_party_editable_exempt(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "\n".join(
            [
                "version = 1",
                "[[package]]",
                'name = "ne-thing"',
                'version = "0.1.0"',
                'source = { editable = "packages/ne-thing" }',
                "",
            ]
        ),
        encoding="utf-8",
    )
    assert find_unhashed(lock) == []
