# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Below-the-seam unit tests for S3ObjectStore — NO server required.

Everything that does not need a live S3-compatible endpoint: entry-point
registration, ``from_settings`` construction, the fail-loud/AIR_GAP/C2 boot
checks, and the pure helpers (retention normalization, key prefix join/strip).
The real transport is covered, skip-gated, by test_s3_conformance.py /
test_s3_worm_live.py against a live MinIO instance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from ne.contracts.plugins import PluginRegistry
from ne.net.airgap import AirGapViolationError
from ne.net.ssrf import UnsafeEndpointError
from ne.storage.config import S3ObjectStoreSettings
from ne.storage.s3 import (
    S3ObjectStore,
    _is_not_found,
    _join_key,
    _normalize_retain_until,
    _strip_key,
)


def _settings(**over: object) -> S3ObjectStoreSettings:
    base: dict[str, object] = {"bucket": "ne-test-bucket"}
    base.update(over)
    return S3ObjectStoreSettings(**base)  # type: ignore[arg-type]


def test_entry_point_resolves_via_plugin_registry() -> None:
    resolved = PluginRegistry().resolve("object_store", "s3")
    assert resolved is S3ObjectStore


def test_from_settings_constructs_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_STORAGE_S3_BUCKET", "env-bucket")
    monkeypatch.setenv("NE_STORAGE_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("NE_STORAGE_S3_ACCESS_KEY_ID", "ne-test")
    monkeypatch.setenv("NE_STORAGE_S3_SECRET_ACCESS_KEY", "ne-test-secret")

    # A positional settings object of an UNRELATED type is accepted and ignored
    # (the worker builder calls cls.from_settings(platform.audit_settings)).
    store = S3ObjectStore.from_settings(object())

    assert isinstance(store, S3ObjectStore)
    assert store._settings.bucket == "env-bucket"


def test_from_settings_present_for_worker_builder() -> None:
    # The exact check build_audit_checkpoint_loop performs before calling
    # cls.from_settings(...) instead of cls().
    assert hasattr(S3ObjectStore, "from_settings")


def test_missing_bucket_fails_loud() -> None:
    with pytest.raises(ValueError, match="NE_STORAGE_S3_BUCKET"):
        S3ObjectStore(S3ObjectStoreSettings(bucket=""))


@pytest.mark.parametrize(
    ("access_key_id", "secret_access_key"),
    [("ne-test", ""), ("", "ne-test-secret")],
)
def test_partial_explicit_credentials_fail_loud_at_boot(
    access_key_id: str, secret_access_key: str
) -> None:
    # Codex round-4 finding: aws_access_key_id was added to the client kwargs
    # INDEPENDENTLY of the secret, so key-id-set + empty-secret handed botocore
    # an incomplete explicit credential. That surfaces as PartialCredentialsError
    # at FIRST USE — a cryptic runtime failure for a boot-time misconfiguration,
    # and it does NOT fall through to the default chain as the docstring implies.
    with pytest.raises(ValueError, match="must be set together or both left empty"):
        S3ObjectStore(_settings(access_key_id=access_key_id, secret_access_key=secret_access_key))


def test_both_empty_credentials_fall_through_to_the_default_chain() -> None:
    # The RECOMMENDED production posture (IAM instance role / IRSA) must remain
    # constructible — the both-or-neither guard must not catch it.
    store = S3ObjectStore(_settings(access_key_id="", secret_access_key=""))
    kwargs = store._client_kwargs()
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs


def test_airgap_blocks_default_aws_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(AirGapViolationError):
        S3ObjectStore(_settings(endpoint_url=""))


def test_airgap_off_allows_default_aws_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIR_GAP", raising=False)
    store = S3ObjectStore(_settings(endpoint_url=""))
    assert store is not None


def test_public_endpoint_rejected_without_allowlist() -> None:
    with pytest.raises(UnsafeEndpointError):
        S3ObjectStore(_settings(endpoint_url="http://8.8.8.8:9000"))


def test_allowed_hosts_accepts_private_dns() -> None:
    store = S3ObjectStore(
        _settings(endpoint_url="http://s3.corp.internal:9000", allowed_hosts="s3.corp.internal")
    )
    assert store is not None


def test_loopback_endpoint_accepted_without_allowlist() -> None:
    # localhost/loopback is accepted by validate_in_vpc_endpoint unconditionally.
    store = S3ObjectStore(_settings(endpoint_url="http://localhost:9000"))
    assert store is not None


def test_airgap_rejects_public_endpoint_despite_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2 fail-closed: an operator allowlist must NOT punch through the air gap.

    Pre-fix, ``__init__`` read ``if endpoint_url: ... elif air_gap_engaged():``,
    so a non-empty endpoint_url SKIPPED the air-gap branch entirely and
    delegated to ``validate_in_vpc_endpoint``, which accepts anything the
    operator listed in ``NE_STORAGE_S3_ALLOWED_HOSTS``. AIR_GAP=1 plus a public
    AWS endpoint therefore constructed cleanly and egressed to the public
    internet, while config.py and DEPLOYMENT.md both advertise AIR_GAP as
    fail-closed here.
    """
    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(AirGapViolationError):
        S3ObjectStore(
            _settings(
                endpoint_url="https://s3.amazonaws.com",
                allowed_hosts="s3.amazonaws.com",
            )
        )


def test_airgap_allows_loopback_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    # Under AIR_GAP the allowlist is ignored, but a PROVABLY in-VPC host
    # (loopback) still constructs — the air gap blocks egress, not storage.
    monkeypatch.setenv("AIR_GAP", "1")
    assert S3ObjectStore(_settings(endpoint_url="http://localhost:9000")) is not None


def test_airgap_allows_in_vpc_dns_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    store = S3ObjectStore(_settings(endpoint_url="http://s3.corp.internal:9000", allowed_hosts=""))
    assert store is not None


def _client_error(code: str, status: int = 404) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},  # type: ignore[arg-type]
        "HeadObject",
    )


def test_is_not_found_matches_codes_and_lets_nosuchbucket_propagate() -> None:
    """A missing BUCKET is a broken deployment, not a missing object.

    Pre-fix ``_is_not_found`` returned True for ANY HTTP 404, so ``NoSuchBucket``
    was swallowed: ``delete()`` returned success having done nothing.
    """
    for code in ("404", "NoSuchKey", "NotFound"):
        assert _is_not_found(_client_error(code)) is True, code
    assert _is_not_found(_client_error("NoSuchBucket")) is False


def test_naive_retain_until_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _normalize_retain_until(datetime(2027, 1, 1))


def test_retention_normalization_whole_second_passes_through() -> None:
    dt = datetime(2027, 1, 1, tzinfo=UTC)
    assert _normalize_retain_until(dt) == dt


def test_retention_normalization_ceils_sub_second_to_next_whole_second() -> None:
    dt = datetime(2027, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
    normalized = _normalize_retain_until(dt)
    assert normalized == datetime(2027, 1, 1, 0, 0, 1, tzinfo=UTC)
    assert normalized > dt  # never shortens the effective retention


def test_retention_normalization_never_produces_earlier_instant() -> None:
    for micros in (1, 1000, 500_000, 999_999):
        dt = datetime(2027, 6, 1, 12, 0, 0, micros, tzinfo=UTC)
        assert _normalize_retain_until(dt) >= dt


def test_key_prefix_join_and_strip() -> None:
    assert _join_key("", "a/b") == "a/b"
    assert _join_key("tenant-1/", "a/b") == "tenant-1/a/b"
    assert _strip_key("", "a/b") == "a/b"
    assert _strip_key("tenant-1/", "tenant-1/a/b") == "a/b"
    # A physical key that does NOT start with the prefix is returned unchanged
    # (defensive: never raises on an unexpected key shape).
    assert _strip_key("tenant-1/", "other/a/b") == "other/a/b"


def test_key_prefix_round_trip() -> None:
    prefix = "audit/checkpoints/"
    physical = _join_key(prefix, "000001.json")
    assert _strip_key(prefix, physical) == "000001.json"


def test_settings_env_prefix_and_defaults() -> None:
    s = _settings()
    assert s.region == "us-east-1"
    assert s.force_path_style is True
    assert s.endpoint_url == ""
    assert s.access_key_id == ""
    assert s.secret_access_key.get_secret_value() == ""
    assert s.session_token.get_secret_value() == ""
    assert s.connect_timeout_s == 5.0
    assert s.read_timeout_s == 60.0


def test_settings_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        S3ObjectStoreSettings(bucket="b", not_a_real_field="x")  # type: ignore[call-arg]


def test_retain_until_normalization_used_by_far_future_kit_style_value() -> None:
    # Mirrors the shape of values the ObjectStoreContractKit constructs
    # (datetime.now(UTC) + timedelta(days=N)) — proves normalization is a no-op
    # for realistic archive() callers once ceiled.
    now = datetime(2026, 7, 18, 9, 30, 0, 250_000, tzinfo=UTC)
    retain = now + timedelta(days=365)
    normalized = _normalize_retain_until(retain)
    assert normalized.microsecond == 0
    assert normalized >= retain


# --- Credentials must not render in a repr ----------------------------------


def test_credential_fields_are_redacted_in_repr() -> None:
    """A settings object in a traceback frame or a log line must not leak creds.

    ``secret_access_key`` and ``session_token`` are ``SecretStr``, so pydantic
    renders them as ``**********``. An STS session token is a bearer credential
    in its own right — anyone holding it can act as the assumed role until it
    expires — so it needs the same treatment as the long-lived secret key.
    ``access_key_id`` is deliberately NOT redacted: it is an identifier, and
    keeping it visible is what makes a "which credential was this?" triage
    possible at all.
    """
    s = _settings(
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="super-secret-key",
        session_token="super-secret-token",
    )
    rendered = f"{s!r} {s!s}"
    assert "super-secret-key" not in rendered
    assert "super-secret-token" not in rendered
    # The identifier stays legible, and the values are still retrievable by
    # code that explicitly asks for them.
    assert "AKIAIOSFODNN7EXAMPLE" in rendered
    assert s.secret_access_key.get_secret_value() == "super-secret-key"
    assert s.session_token.get_secret_value() == "super-secret-token"


def test_empty_secretstr_credentials_do_not_reach_botocore() -> None:
    """Empty creds must fall through to the default credential chain.

    The trap this pins: ``SecretStr`` defines no ``__bool__``, so a naive
    ``if settings.secret_access_key:`` is TRUE even for ``SecretStr("")`` — it
    would hand botocore an EMPTY secret key instead of letting the IAM
    role / IRSA chain supply one, breaking the recommended production posture.
    ``_client_kwargs`` unwraps before testing truthiness; this proves it.
    """
    store = S3ObjectStore(_settings(endpoint_url="http://s3.internal:9000"))
    kwargs = store._client_kwargs()
    assert "aws_secret_access_key" not in kwargs
    assert "aws_session_token" not in kwargs
    assert "aws_access_key_id" not in kwargs

    # ...and a populated credential IS passed through, unwrapped.
    configured = S3ObjectStore(
        _settings(
            endpoint_url="http://s3.internal:9000",
            access_key_id="AKIA",
            secret_access_key="sk",
            session_token="tok",
        )
    )._client_kwargs()
    assert configured["aws_access_key_id"] == "AKIA"
    assert configured["aws_secret_access_key"] == "sk"
    assert configured["aws_session_token"] == "tok"


# --- The documented IAM list must cover the PERMISSION-GATED header reads ----


_IAM_DOC_FILES = (
    Path(__file__).resolve().parents[1] / "README.md",
    Path(__file__).resolve().parents[3] / "docs" / "DEPLOYMENT.md",
)


@pytest.mark.parametrize("action", ["s3:GetObjectLegalHold", "s3:GetObjectRetention"])
@pytest.mark.parametrize("doc", _IAM_DOC_FILES, ids=lambda p: p.name)
def test_documented_iam_list_covers_permission_gated_lock_header_reads(
    doc: Path, action: str
) -> None:
    """Both published minimal IAM lists must name every gated Object Lock read.

    Round-4 BLOCKING, and the reason it is a DOCS test rather than a behaviour
    test: on real AWS, ``HeadObject`` returns
    ``x-amz-object-lock-legal-hold`` only to a caller holding
    ``s3:GetObjectLegalHold``, and ``x-amz-object-lock-mode`` /
    ``x-amz-object-lock-retain-until-date`` only to one holding
    ``s3:GetObjectRetention``. **Lacking the permission does not fail the
    call** — ``HeadObject`` returns 200 and OMITS the header. So a protected
    version reads as unprotected, ``_locked_version`` misses it, ``delete()``
    lays a delete marker over it, and the archived checkpoint vanishes from
    ``list_objects``: the round-1 C10 hole, reopened by a missing permission.

    The docs listed ``s3:GetObjectRetention`` but NOT
    ``s3:GetObjectLegalHold``, so the legal-hold handling was inert on real
    AWS while every MinIO test stayed green — MinIO returns these headers
    regardless of permission, which is precisely why no live test against the
    test server can catch this and why the ACTION LIST is asserted directly.
    """
    text = doc.read_text(encoding="utf-8")
    assert "Minimal IAM actions for the checkpoint role" in text, f"{doc.name}: list moved"
    assert f"`{action}`" in text, (
        f"{doc.name} does not list {action}. Without it AWS silently omits the corresponding "
        "HeadObject Object Lock header, making the protection invisible rather than erroring."
    )
