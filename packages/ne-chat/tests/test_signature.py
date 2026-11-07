# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Slack v0 signature verification — acceptance, forgery, anti-replay, C12."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from ne.chat.errors import SignatureVerificationError, StaleTimestampError
from ne.chat.slack.signature import verify_slack_signature

SECRET = "top-secret-signing-key"
NOW = 1_720_000_000.0


def _sig(body: bytes, ts: int, secret: str = SECRET) -> str:
    basestring = f"v0:{ts}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()


def _verify(**over: object) -> None:
    body = over.pop("raw_body", b'{"ok":true}')
    ts = over.pop("ts", int(NOW))
    kwargs: dict[str, object] = {
        "raw_body": body,
        "timestamp_header": str(ts),
        "signature_header": _sig(body, ts),  # type: ignore[arg-type]
        "signing_secret": SECRET,
        "now": NOW,
        "tolerance_s": 300.0,
    }
    kwargs.update(over)
    verify_slack_signature(**kwargs)  # type: ignore[arg-type]


def test_valid_v0_signature_accepts() -> None:
    _verify()  # does not raise


def test_forged_signature_rejects() -> None:
    with pytest.raises(SignatureVerificationError):
        _verify(signature_header="v0=" + "0" * 64)


def test_signature_from_wrong_secret_rejects() -> None:
    body = b'{"ok":true}'
    with pytest.raises(SignatureVerificationError):
        _verify(signature_header=_sig(body, int(NOW), "attacker-secret"))


def test_stale_past_timestamp_rejects() -> None:
    with pytest.raises(StaleTimestampError):
        _verify(ts=int(NOW - 10_000))


def test_future_timestamp_rejects() -> None:
    with pytest.raises(StaleTimestampError):
        _verify(ts=int(NOW + 10_000))


@pytest.mark.parametrize("missing", ["timestamp_header", "signature_header"])
def test_missing_headers_reject(missing: str) -> None:
    with pytest.raises(SignatureVerificationError):
        _verify(**{missing: None})


def test_empty_headers_reject() -> None:
    with pytest.raises(SignatureVerificationError):
        _verify(signature_header="")


def test_non_numeric_timestamp_rejects() -> None:
    body = b'{"ok":true}'
    with pytest.raises(SignatureVerificationError):
        verify_slack_signature(
            raw_body=body,
            timestamp_header="not-a-number",
            signature_header=_sig(body, int(NOW)),
            signing_secret=SECRET,
            now=NOW,
            tolerance_s=300.0,
        )


@pytest.mark.parametrize("bad_ts", ["nan", "inf", "-inf", "NaN", "Infinity"])
def test_non_finite_timestamp_rejects(bad_ts: str) -> None:
    # float("nan"/"inf") parses, but must not slip past the anti-replay window and
    # blow up at int(ts). Rejected status-only, never a 500 (P3 hardening).
    body = b'{"ok":true}'
    with pytest.raises(SignatureVerificationError):
        verify_slack_signature(
            raw_body=body,
            timestamp_header=bad_ts,
            signature_header=_sig(body, int(NOW)),
            signing_secret=SECRET,
            now=NOW,
            tolerance_s=300.0,
        )


def test_wrong_version_prefix_rejects() -> None:
    body = b'{"ok":true}'
    digest = hmac.new(
        SECRET.encode(), f"v0:{int(NOW)}:".encode() + body, hashlib.sha256
    ).hexdigest()
    with pytest.raises(SignatureVerificationError):
        _verify(signature_header=f"v1={digest}")


def test_errors_never_contain_secret_or_digest() -> None:
    body = b'{"payload":"sensitive"}'
    ts = int(NOW)
    good = _sig(body, ts)
    digest = good.removeprefix("v0=")
    cases: list[dict[str, object]] = [
        {"signature_header": None},  # missing
        {"signature_header": "v0=" + "0" * 64},  # forged
        {"ts": int(NOW - 10_000)},  # stale
        {"signature_header": f"v9={digest}"},  # wrong version
    ]
    for over in cases:
        try:
            _verify(raw_body=body, **over)  # type: ignore[arg-type]
        except (SignatureVerificationError, StaleTimestampError) as exc:
            text = str(exc)
            assert SECRET not in text
            assert digest not in text
            assert "sensitive" not in text
        else:  # pragma: no cover
            raise AssertionError("expected a rejection")
