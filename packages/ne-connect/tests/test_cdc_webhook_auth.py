# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Standard-Webhooks HMAC verification for the CDC sink (NE-084).

Includes a HARDCODED known-answer vector computed per Debezium's
``StandardWebhooksAuthenticator.java`` scheme so the Python verifier and the
Java signer provably agree on the wire format.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from ne.connect.cdc import decode_webhook_secret, verify_standard_webhook

# --- Known-answer vector (computed offline; DO NOT regenerate casually) ------
_KA_SECRET = "whsec_AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
_KA_RAW = bytes(range(32))
_KA_MSG_ID = "msg_2wUangdCeLufkQxKXaJT7DP"
_KA_TS = "1720000000"
_KA_BODY = b'{"op":"c","source":{"schema":"public","table":"customers"}}'
_KA_SIG = "v1,7TgfdebIlCYjQJFk4smzFWbTX+w2BUJ+GycGO+nWIoE="


def _sign(raw: bytes, msg_id: str, ts: str, body: bytes) -> str:
    digest = hmac.new(raw, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def test_known_answer_vector_matches_debezium_scheme() -> None:
    raw = decode_webhook_secret(_KA_SECRET)
    assert raw == _KA_RAW
    # The verifier accepts the hardcoded signature at the vector's timestamp.
    assert verify_standard_webhook(
        secret=raw,
        msg_id=_KA_MSG_ID,
        timestamp=_KA_TS,
        signature_header=_KA_SIG,
        body=_KA_BODY,
        now=int(_KA_TS),
    )
    # And the offline-computed constant equals a fresh local computation.
    assert _sign(raw, _KA_MSG_ID, _KA_TS, _KA_BODY) == _KA_SIG


def test_valid_signature_accepts() -> None:
    raw = decode_webhook_secret(_KA_SECRET)
    sig = _sign(raw, "msg_1", "1000", b"payload")
    assert verify_standard_webhook(
        secret=raw,
        msg_id="msg_1",
        timestamp="1000",
        signature_header=sig,
        body=b"payload",
        now=1000,
    )


def test_wrong_secret_rejects() -> None:
    good = decode_webhook_secret(_KA_SECRET)
    bad = bytes([1]) + good[1:]
    sig = _sign(good, "msg_1", "1000", b"payload")
    assert not verify_standard_webhook(
        secret=bad,
        msg_id="msg_1",
        timestamp="1000",
        signature_header=sig,
        body=b"payload",
        now=1000,
    )


def test_tampered_body_rejects() -> None:
    raw = decode_webhook_secret(_KA_SECRET)
    sig = _sign(raw, "msg_1", "1000", b"payload")
    assert not verify_standard_webhook(
        secret=raw,
        msg_id="msg_1",
        timestamp="1000",
        signature_header=sig,
        body=b"tampered",
        now=1000,
    )


def test_stale_timestamp_rejects() -> None:
    raw = decode_webhook_secret(_KA_SECRET)
    sig = _sign(raw, "msg_1", "1000", b"payload")
    assert not verify_standard_webhook(
        secret=raw,
        msg_id="msg_1",
        timestamp="1000",
        signature_header=sig,
        body=b"payload",
        now=1000 + 301,
    )


def test_future_timestamp_rejects() -> None:
    raw = decode_webhook_secret(_KA_SECRET)
    sig = _sign(raw, "msg_1", "1000", b"payload")
    assert not verify_standard_webhook(
        secret=raw,
        msg_id="msg_1",
        timestamp="1000",
        signature_header=sig,
        body=b"payload",
        now=1000 - 301,
    )


def test_multiple_signatures_any_valid_accepts() -> None:
    raw = decode_webhook_secret(_KA_SECRET)
    good = _sign(raw, "msg_1", "1000", b"payload")
    header = f"v1,deadbeefdeadbeefdeadbeefdeadbeef= {good}"
    assert verify_standard_webhook(
        secret=raw,
        msg_id="msg_1",
        timestamp="1000",
        signature_header=header,
        body=b"payload",
        now=1000,
    )


def test_missing_headers_reject() -> None:
    raw = decode_webhook_secret(_KA_SECRET)
    sig = _sign(raw, "msg_1", "1000", b"payload")
    for msg_id, ts, header in [
        (None, "1000", sig),
        ("msg_1", None, sig),
        ("msg_1", "1000", None),
        ("msg_1", "not-an-int", sig),
    ]:
        assert not verify_standard_webhook(
            secret=raw,
            msg_id=msg_id,
            timestamp=ts,
            signature_header=header,
            body=b"payload",
            now=1000,
        )


def test_secret_length_bounds() -> None:
    # 23 bytes: too short.
    with pytest.raises(ValueError):
        decode_webhook_secret("whsec_" + base64.b64encode(b"a" * 23).decode())
    # 65 bytes: too long.
    with pytest.raises(ValueError):
        decode_webhook_secret("whsec_" + base64.b64encode(b"a" * 65).decode())
    # 32 bytes without the prefix decodes fine.
    assert len(decode_webhook_secret(base64.b64encode(b"a" * 32).decode())) == 32


def test_bad_base64_secret_raises() -> None:
    with pytest.raises(ValueError):
        decode_webhook_secret("whsec_not valid base64!!!")
