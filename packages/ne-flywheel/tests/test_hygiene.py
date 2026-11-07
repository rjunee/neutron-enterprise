# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C12 secret-hygiene scrub tests (NE-064)."""

from __future__ import annotations

from ne.flywheel.hygiene import scrub_text


def test_scrubs_aws_access_key() -> None:
    text = "here is my key AKIAABCDEFGHIJKLMNOP for the demo"
    scrubbed = scrub_text(text)
    assert "AKIAABCDEFGHIJKLMNOP" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_scrubs_github_token() -> None:
    text = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    scrubbed = scrub_text(text)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_scrubs_slack_token() -> None:
    text = "slack token xoxb-1234567890-abcdefghijklmnop is embedded here"
    scrubbed = scrub_text(text)
    assert "xoxb-1234567890-abcdefghijklmnop" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_scrubs_bearer_token() -> None:
    text = "Authorization: Bearer abcdef0123456789.ABCDEF-ghijkl"
    scrubbed = scrub_text(text)
    assert "abcdef0123456789.ABCDEF-ghijkl" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_scrubs_jwt() -> None:
    text = (
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQmRZ1234abc "
        "is in the comment"
    )
    scrubbed = scrub_text(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_scrubs_pem_private_key_block() -> None:
    text = (
        "before\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAK...\n"
        "-----END RSA PRIVATE KEY-----\n"
        "after"
    )
    scrubbed = scrub_text(text)
    assert "MIIBOgIBAAJBAK" not in scrubbed
    assert "[REDACTED]" in scrubbed
    assert "before" in scrubbed
    assert "after" in scrubbed


def test_scrubs_dangling_pem_begin_line() -> None:
    text = "oops -----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBAK...\n(no end marker)"
    scrubbed = scrub_text(text)
    assert "MIIBOgIBAAJBAK" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_scrubs_password_and_api_key_assignments() -> None:
    assert "[REDACTED]" in scrub_text("password=hunter2plus")
    assert "[REDACTED]" in scrub_text("api_key: sk-abcdef1234567890")
    assert "[REDACTED]" in scrub_text("Secret = topsecretvalue")
    assert "[REDACTED]" in scrub_text("TOKEN=abc123def456")
    assert "hunter2plus" not in scrub_text("password=hunter2plus")


def test_plain_prose_unchanged() -> None:
    text = "The user asked about invoice status and the answer cited the wrong document."
    assert scrub_text(text) == text


def test_truncates_after_redaction_to_max_chars() -> None:
    text = "a" * 100
    scrubbed = scrub_text(text, max_chars=10)
    assert scrubbed == "a" * 10

    with_secret = "AKIAABCDEFGHIJKLMNOP " + ("b" * 100)
    scrubbed_secret = scrub_text(with_secret, max_chars=20)
    assert len(scrubbed_secret) == 20
    assert scrubbed_secret.startswith("[REDACTED]")
