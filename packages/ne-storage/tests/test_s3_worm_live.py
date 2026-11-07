# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The core WORM proofs — SERVER-side enforcement, not adapter courtesy.

Every test here bypasses S3ObjectStore with a RAW aiobotocore client wherever
the point is to prove the SERVER (not our code) refuses a mutation. This is
the entire reason NE-S3-OBJECTSTORE exists: a mock that returns what you tell
it proves nothing about WORM.

Skips cleanly (see the shared probe in test_s3_conformance.py's docstring)
when the test server is unreachable; CI wires a real MinIO into the
``live-postgres`` job under the must-run-not-skip gate (see
.github/workflows/ci.yml) so an outage FAILS the build rather than silently
passing.

NE-WORM-LIVE (2026-07-19): this same file is ALSO the AWS leg of the
owner-gated ``worm-live-proof`` workflow, pointed at real AWS S3 rather than
MinIO. It reads a handful of ``NE_TEST_S3_*`` env knobs (session token,
allowed hosts, STS role ARN, and the FAR/NEAR retention windows) that each
default to exactly the previous hard-coded value, so the MinIO CI lane is
unchanged. The retentions are shortened to minutes on the live lane because a
COMPLIANCE-locked object cannot be deleted until its retention expires — and
neither, therefore, can its bucket — so a year-scale lock would make teardown
impossible.

"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import ClientError
from pydantic import SecretStr

from ne.audit import ensure_audit_schema
from ne.audit.checkpoint import HmacSigner, verify_checkpoints, write_checkpoint
from ne.audit.store import append_record
from ne.contracts.errors import WormViolationError
from ne.schema import AuditEvent, AuditOutcome
from ne.storage.config import S3ObjectStoreSettings
from ne.storage.s3 import (
    GovernanceUpgradeRefusedError,
    ObjectLockNotEnabledError,
    ObjectLockPermissionError,
    S3ObjectStore,
    TooManyObjectVersionsError,
    _normalize_retain_until,
)

_ENDPOINT = os.environ.get("NE_TEST_S3_ENDPOINT", "http://localhost:9000")
_ACCESS_KEY = os.environ.get("NE_TEST_S3_ACCESS_KEY", "ne-test")
_SECRET_KEY = os.environ.get("NE_TEST_S3_SECRET_KEY", "ne-test-secret")
# --- NE-WORM-LIVE env knobs (2026-07-19) ------------------------------------
# Every knob below defaults to EXACTLY what this file did before, so the MinIO
# CI lane is byte-identical. They exist for the owner-gated `worm-live-proof`
# workflow, which points this same suite at REAL AWS S3: that needs a session
# token (OIDC role assumption), a non-localhost allowed_hosts (the C2 gate
# refuses an unlisted endpoint at construction), a real role ARN for the
# documented-IAM role-chain tests, and — critically — SHORT retentions. A
# COMPLIANCE-locked object cannot be deleted until its retention expires, so
# its bucket cannot be emptied or deleted either; year-scale locks would leave
# an undeletable bucket behind on every live run. Durations only — no
# assertion semantics change with these.
_SESSION_TOKEN = os.environ.get("NE_TEST_S3_SESSION_TOKEN", "")
_ALLOWED_HOSTS = os.environ.get("NE_TEST_S3_ALLOWED_HOSTS", "")
_STS_ROLE_ARN = os.environ.get(
    "NE_TEST_S3_STS_ROLE_ARN", "arn:aws:iam::000000000000:role/ne-storage-doc-iam-test"
)
# NE-WORM-LIVE: STS is a SEPARATE service on real AWS (sts.<region>.amazonaws.com),
# but MinIO serves AssumeRole on its ONE S3 endpoint. The role-chain tests build
# an sts client; pointing it at the S3 endpoint makes real S3 answer AssumeRole
# with an unparseable body → botocore raises the opaque `(Unknown)` error. Default
# to _ENDPOINT so the MinIO lane is byte-identical; the worm-live-proof AWS leg
# overrides it to the real STS endpoint. (Verified against real AWS 2026-07-22:
# S3 endpoint → Unknown; STS endpoint → a real, parseable AssumeRole response.)
_STS_ENDPOINT = os.environ.get("NE_TEST_S3_STS_ENDPOINT", _ENDPOINT)
# NE-WORM-LIVE: a server-side WORM-violating delete / retention-shorten is denied
# with HTTP 403 AccessDenied on real AWS but 400 InvalidRequest on MinIO — both
# are valid server denials. The guarantee each test proves is the raise + the
# error Code + the byte-survival re-read, NOT the incidental status number; the
# status check only guards against a false-green from an unrelated malformed
# request. (Verified against real AWS 2026-07-22: delete-locked-version and
# shorten-COMPLIANCE-retention both return 403 AccessDenied.)
_WORM_DENY_STATUSES = {400, 403}
_FAR_DELTA = timedelta(
    seconds=float(os.environ.get("NE_TEST_S3_FAR_RETENTION_SECONDS", str(365 * 86400)))
)
_NEAR_DELTA = timedelta(
    seconds=float(os.environ.get("NE_TEST_S3_NEAR_RETENTION_SECONDS", str(86400)))
)
# Several tests assert that a NEAR timestamp is refused as a shortening of a
# FAR one; that only means anything while the ordering holds.
assert _NEAR_DELTA < _FAR_DELTA, "NE_TEST_S3_NEAR_RETENTION_SECONDS must be < FAR"

_HEX_HASH = "a" * 64
_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _s3_available() -> bool:
    try:
        urllib.request.urlopen(f"{_ENDPOINT}/minio/health/live", timeout=1.0)
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _s3_available(),
    reason=(
        "S3 test server not reachable "
        "(docker compose -f deploy/compose/dev/compose.yaml --profile worm up -d minio; "
        "see packages/ne-storage/README.md)"
    ),
)


def _make_bucket_and_store(**overrides: Any) -> tuple[S3ObjectStore, str]:
    bucket = f"ne-worm-{uuid4().hex[:12]}"

    async def _create() -> None:
        async with _raw_client() as client:
            await client.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)

    asyncio.run(_create())

    settings = S3ObjectStoreSettings(
        endpoint_url=_ENDPOINT,
        bucket=bucket,
        access_key_id=_ACCESS_KEY,
        secret_access_key=SecretStr(_SECRET_KEY),
        session_token=SecretStr(_SESSION_TOKEN),
        allowed_hosts=_ALLOWED_HOSTS,
        **overrides,
    )
    return S3ObjectStore(settings), bucket


def _raw_client() -> Any:
    # Untyped return (aiobotocore ships no py.typed / stubs): without this,
    # pyright resolves dynamically-generated client methods (create_bucket,
    # put_object_retention, delete_object, ...) through
    # AioBaseClient.__getattr__, whose sole unconditional `raise
    # AttributeError` infers as NoReturn — making every
    # `await client.<method>(...)` call falsely fail "NoReturn is not
    # awaitable". Declaring this helper's return type Any is the standard
    # workaround for unstubbed boto-family clients.
    session = get_session()
    return session.create_client(
        "s3",
        endpoint_url=_ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY,
        # NE-WORM-LIVE: temporary OIDC creds (ASIA…) are invalid without their
        # session token — real AWS answers InvalidAccessKeyId. Default "" → None
        # (not sent), so the MinIO lane stays byte-identical.
        aws_session_token=_SESSION_TOKEN or None,
        config=AioConfig(s3={"addressing_style": "path"}),
    )


def test_raw_client_delete_version_denied() -> None:
    """The core proof: the SERVER enforces WORM, not the adapter.

    A raw client — not S3ObjectStore — attempts to delete the exact locked
    VERSION. The server must refuse regardless of who is asking.
    """
    store, bucket = _make_bucket_and_store()

    async def scenario() -> None:
        await store.put("k", b"payload")
        await store.archive("k", retain_until=datetime.now(UTC) + _NEAR_DELTA)

        async with _raw_client() as client:
            versions = (await client.list_object_versions(Bucket=bucket, Prefix="k"))["Versions"]
            assert len(versions) == 1
            version_id = versions[0]["VersionId"]

            with pytest.raises(ClientError) as exc_info:
                await client.delete_object(Bucket=bucket, Key="k", VersionId=version_id)
            assert (
                exc_info.value.response["ResponseMetadata"]["HTTPStatusCode"] in _WORM_DENY_STATUSES
            )
            # A bare 400 is satisfied by ANY malformed request (a typo'd
            # VersionId would keep this green while proving nothing), so pin
            # the actual denial code AND re-read the bytes to prove the locked
            # version physically survived the delete attempt.
            assert exc_info.value.response["Error"]["Code"] in {"AccessDenied", "InvalidRequest"}
            resp = await client.get_object(Bucket=bucket, Key="k", VersionId=version_id)
            async with resp["Body"] as stream:
                assert await stream.read() == b"payload"

    asyncio.run(scenario())


def test_raw_client_shorten_retention_denied() -> None:
    """COMPLIANCE shorten is impossible even bypassing our code entirely."""
    store, bucket = _make_bucket_and_store()

    async def scenario() -> None:
        await store.put("k", b"payload")
        original = datetime.now(UTC) + _FAR_DELTA
        await store.archive("k", retain_until=original)

        async with _raw_client() as client:
            versions = (await client.list_object_versions(Bucket=bucket, Prefix="k"))["Versions"]
            version_id = versions[0]["VersionId"]
            shorter = _normalize_retain_until(datetime.now(UTC) + _NEAR_DELTA)

            with pytest.raises(ClientError) as exc_info:
                await client.put_object_retention(
                    Bucket=bucket,
                    Key="k",
                    VersionId=version_id,
                    Retention={"Mode": "COMPLIANCE", "RetainUntilDate": shorter},
                )
            assert (
                exc_info.value.response["ResponseMetadata"]["HTTPStatusCode"] in _WORM_DENY_STATUSES
            )
            assert exc_info.value.response["Error"]["Code"] in {"AccessDenied", "InvalidRequest"}
            resp = await client.get_object(Bucket=bucket, Key="k", VersionId=version_id)
            async with resp["Body"] as stream:
                assert await stream.read() == b"payload"

    asyncio.run(scenario())


def test_worm_expiry_realtime() -> None:
    """Real-time expiry proof — covers what the kit's injectable-clock
    test_worm_expires would have proven, honestly, with an actual wait against
    a real server (no fake clock is possible against a live S3 endpoint)."""
    store, _bucket = _make_bucket_and_store()

    async def scenario() -> None:
        await store.put("k", b"v1")
        # OBSERVED FLAKE (2026-07-19), fixed here rather than re-run away: this
        # was `seconds=3`, and on a loaded machine the whole suite ran 189s
        # instead of 79s (~2.4x). Under that slowdown the archive() call landed
        # AFTER its own retain_until had already passed, and the server
        # rejected it — MalformedXML, "the retain until date must be in the
        # future". Not a code defect: the margin has to exceed the worst-case
        # latency of the very call that consumes it, and 3s does not on a busy
        # CI runner. This test is inside the must-run WORM gate with a pass
        # floor, so a flake here fails the entire PR.
        #
        # 15s is chosen to survive the ~2.4x slowdown actually measured with
        # room to spare. The cost is bounded and NOT 15s: the expiry poll below
        # exits as soon as the key becomes writable, so the added wall-clock is
        # only the extra retention itself.
        retain_until = datetime.now(UTC) + timedelta(seconds=15)
        await store.archive("k", retain_until=retain_until)

        with pytest.raises(WormViolationError):
            await store.put("k", b"v2")

        # Widened 30 -> 60 alongside the retention above: the poll has to
        # outlast the retention plus whatever load-induced latency delayed it,
        # and 30s left too little headroom over a 15s retention. Costs nothing
        # on the happy path — the loop breaks the moment the key is writable,
        # so this bound is only ever reached when the test is genuinely failing.
        deadline = time.monotonic() + 60
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                await store.put("k", b"v2")
                break
            except WormViolationError as exc:
                last_error = exc
                await asyncio.sleep(0.5)
        else:
            raise AssertionError(f"key never became writable after expiry: {last_error}")

        assert await store.get("k") == b"v2"

        # Re-archiving after expiry succeeds freely (contract: "after expiry
        # the key may be re-archived freely").
        receipt = await store.archive("k", retain_until=datetime.now(UTC) + _NEAR_DELTA)
        assert receipt.key == "k"

    asyncio.run(scenario())


def test_retain_until_precision_roundtrip() -> None:
    """Permanent regression pin for the STEP-0 precision decision (Branch B):
    a sub-second retain_until is ceiled to the next whole second BEFORE it
    reaches the server, so the archived receipt is never earlier than what the
    caller asked for, and the server-stored value matches the ceiled instant
    exactly (verified against MinIO's actual millisecond-truncating
    behaviour — see s3.py's module docstring)."""
    store, _bucket = _make_bucket_and_store()

    async def scenario() -> None:
        await store.put("k", b"v")
        sub_second = datetime.now(UTC).replace(microsecond=654_321) + _NEAR_DELTA
        receipt = await store.archive("k", retain_until=sub_second)

        expected = _normalize_retain_until(sub_second)
        assert receipt.retain_until == expected
        assert receipt.retain_until >= sub_second

        stat = await store.stat("k")
        assert stat.worm_retain_until == expected

    asyncio.run(scenario())


def _make_engine() -> sa.Engine:
    """A minimal serialized SQLite audit engine — copied (not imported) from
    packages/ne-audit/tests/conftest.py's ``engine`` fixture, per the
    #249-independence note in this unit's brief (this file must not import
    ne-audit's conftest)."""
    eng = sa.create_engine("sqlite://")
    ensure_audit_schema(eng)
    return eng


def _make_audit_event(event_id: str) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        event_type="audit",
        config_hash=_HEX_HASH,
        actor="sys",
        provenance={},
        occurred_at=_TS,
        action="tool.invoke",
        resource="urn:tool:search",
        outcome=AuditOutcome.success,
    )


def test_signed_checkpoint_lands_in_worm() -> None:
    """The #249-independent integration proof.

    NE-AUDIT-CRON's checkpoint loop (PR #249, not yet merged in this base —
    ``ne.worker.audit_checkpoint`` is absent) calls
    ``ne.audit.checkpoint.write_checkpoint`` with a resolved ObjectStore. This
    test calls that EXACT primitive against a real S3ObjectStore, proving the
    seam the loop depends on works end to end: a real signed checkpoint lands
    in real WORM storage, verifies, and the archived object is genuinely
    immutable — both through the adapter's own contract semantics AND via a
    raw-client server-side bypass probe.
    """
    store, bucket = _make_bucket_and_store()
    engine = _make_engine()
    try:
        with engine.begin() as conn:
            for i in range(3):
                append_record(conn, _make_audit_event(f"e{i}"))

        signer = HmacSigner(b"k" * 16)

        async def scenario() -> None:
            checkpoint = await write_checkpoint(engine, store, signer, retention_days=1)
            assert checkpoint.seq == 3

            verification = await verify_checkpoints(engine, store, signer)
            assert verification.valid is True
            assert verification.checked == 1

            key = f"audit/checkpoints/{checkpoint.seq:020d}.json"
            signed_bytes = await store.get(key)

            # Immutable via the adapter's own contract semantics.
            with pytest.raises(WormViolationError):
                await store.put(key, b"tampered")
            with pytest.raises(WormViolationError):
                await store.delete(key)

            # Immutable via a raw-client server-side bypass — the archived
            # checkpoint's locked VERSION cannot be deleted by ANY caller,
            # adapter or not.
            async with _raw_client() as client:
                versions = (await client.list_object_versions(Bucket=bucket, Prefix=key))[
                    "Versions"
                ]
                assert len(versions) == 1
                with pytest.raises(ClientError) as exc_info:
                    await client.delete_object(
                        Bucket=bucket, Key=key, VersionId=versions[0]["VersionId"]
                    )
                assert (
                    exc_info.value.response["ResponseMetadata"]["HTTPStatusCode"]
                    in _WORM_DENY_STATUSES
                )
                assert exc_info.value.response["Error"]["Code"] in {
                    "AccessDenied",
                    "InvalidRequest",
                }
                # The signed checkpoint bytes physically survived the bypass.
                resp = await client.get_object(
                    Bucket=bucket, Key=key, VersionId=versions[0]["VersionId"]
                )
                async with resp["Body"] as stream:
                    assert await stream.read() == signed_bytes

        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_delete_marker_does_not_hide_archived_checkpoint() -> None:
    """C10 regression: a versionless DeleteObject must NOT hide the tamper anchor.

    S3 Object Lock protects VERSIONS, not KEYS. ``DeleteObject`` WITHOUT a
    ``VersionId`` is ALWAYS permitted — it layers a delete marker over the
    locked version rather than removing it. Before the archived-version fix,
    that single call (using ``s3:DeleteObject``, a permission DEPLOYMENT.md
    tells operators to grant) made ``list_objects`` return ``[]`` and
    ``verify_checkpoints`` report ``valid=True, checked=0`` — a clean GREEN
    after the anchor had been made invisible. That is exactly the silent
    destruction C10 promises is impossible.

    The old raw-denial tests all passed ``VersionId=...``, i.e. they exercised
    only the path S3 denies; this one exercises the path S3 permits.
    """
    store, bucket = _make_bucket_and_store()
    engine = _make_engine()
    try:
        with engine.begin() as conn:
            for i in range(3):
                append_record(conn, _make_audit_event(f"e{i}"))

        signer = HmacSigner(b"k" * 16)

        async def scenario() -> None:
            checkpoint = await write_checkpoint(engine, store, signer, retention_days=1)
            assert checkpoint.seq == 3
            key = f"audit/checkpoints/{checkpoint.seq:020d}.json"
            original = await store.get(key)

            # The threat: a versionless delete. The SERVER permits this.
            async with _raw_client() as client:
                resp = await client.delete_object(Bucket=bucket, Key=key)
                assert resp.get("DeleteMarker") is True
                # ...and the current version is now a delete marker, so the
                # key has vanished from a plain list_objects_v2 listing.
                assert (await client.list_objects_v2(Bucket=bucket, Prefix=key)).get(
                    "Contents", []
                ) == []

            # ...but the adapter must see THROUGH the marker to the still-locked
            # archived version: listing, reads and stat all survive.
            assert key in [s.key for s in await store.list_objects("audit/checkpoints/")]
            assert await store.get(key) == original
            stat = await store.stat(key)
            assert stat.worm_retain_until is not None
            assert stat.size == len(original)

            # The anchor still verifies against the intact chain.
            intact = await verify_checkpoints(engine, store, signer)
            assert intact.valid is True
            assert intact.checked == 1

            # And the anchor still BITES: against a wiped DB (whole-database
            # destruction — ensure_audit_schema seeds head seq=0, below the
            # signed seq=3) verification must go RED. Pre-fix this returned
            # valid=True/checked=0 because the marker hid the checkpoint.
            # NB: asserting valid=False on the INTACT chain would be wrong —
            # an untampered chain legitimately verifies green.
            fresh = _make_engine()
            try:
                wiped = await verify_checkpoints(fresh, store, signer)
                assert wiped.valid is False
                assert wiped.checked >= 0
            finally:
                fresh.dispose()

        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_layered_unlocked_version_does_not_disable_worm_checks() -> None:
    """A layered unlocked version must not permanently switch the WORM check off.

    Pre-fix, ``put``/``delete`` tested the retention of the LATEST version
    (``HeadObject``). One out-of-band ``PutObject`` layered an unlocked version
    on top of the locked one, so from that moment EVERY subsequent adapter
    ``put`` and ``delete`` succeeded silently, forever, for all callers — not a
    transient race. Chained with the delete-marker hole that is: raw put once,
    then adapter delete, and the key is gone from the listing.

    Also pins P1-3: the WORM-protected bytes stay reachable through the
    ObjectStore API (``get`` resolves the LOCKED version, not the attacker's).
    """
    store, bucket = _make_bucket_and_store()

    async def scenario() -> None:
        await store.put("k", b"original")
        await store.archive("k", retain_until=datetime.now(UTC) + _NEAR_DELTA)

        # Out-of-band unlocked version layered on top — the server permits it
        # (a new version never touches the locked one).
        async with _raw_client() as client:
            await client.put_object(Bucket=bucket, Key="k", Body=b"layered")

        # The client-side WORM check must still fire — permanently.
        with pytest.raises(WormViolationError):
            await store.put("k", b"x")
        with pytest.raises(WormViolationError):
            await store.delete("k")

        # Reads resolve the ARCHIVED version, not the layered attacker bytes.
        assert await store.get("k") == b"original"
        stat = await store.stat("k")
        assert stat.worm_retain_until is not None
        assert stat.size == len(b"original")

        listed = {s.key: s for s in await store.list_objects()}
        assert "k" in listed
        assert listed["k"].worm_retain_until is not None

    asyncio.run(scenario())


def test_bucket_without_object_lock_fails_loud() -> None:
    """A bucket without Object Lock must fail with the REAL reason.

    Object Lock can only be enabled at bucket creation and cannot be
    retrofitted, so this misconfiguration is both likely and PERMANENT. Pre-fix
    it surfaced as a raw ``KeyError('VersionId')`` (no VersionId on an
    unversioned bucket) or — once past that — as a FABRICATED
    ``WormViolationError('... lost a race against a concurrent archive()')``,
    a lying diagnosis on the exact path C10 depends on.
    """
    bucket = f"ne-nolock-{uuid4().hex[:12]}"

    async def _create() -> None:
        async with _raw_client() as client:
            # Deliberately NO ObjectLockEnabledForBucket.
            await client.create_bucket(Bucket=bucket)

    asyncio.run(_create())

    store = S3ObjectStore(
        S3ObjectStoreSettings(
            endpoint_url=_ENDPOINT,
            bucket=bucket,
            access_key_id=_ACCESS_KEY,
            secret_access_key=SecretStr(_SECRET_KEY),
            session_token=SecretStr(_SESSION_TOKEN),
            allowed_hosts=_ALLOWED_HOSTS,
        )
    )

    async def scenario() -> None:
        await store.put("k", b"v")  # plain writes work fine
        with pytest.raises(ObjectLockNotEnabledError, match="Object Lock"):
            await store.archive("k", retain_until=datetime.now(UTC) + _NEAR_DELTA)

    asyncio.run(scenario())


def test_missing_bucket_surfaces_nosuchbucket() -> None:
    """A typo'd bucket must fail loud, not look like an empty audit store.

    Pre-fix ``_is_not_found`` keyed on HTTP 404 ALONE, which swallowed
    ``NoSuchBucket``: ``stat``/``get`` raised ``ObjectNotFoundError`` and —
    worse — ``delete()`` returned SUCCESS having done nothing. A broken
    deployment was indistinguishable from an empty audit store.
    """
    store = S3ObjectStore(
        S3ObjectStoreSettings(
            endpoint_url=_ENDPOINT,
            bucket=f"ne-absent-{uuid4().hex[:12]}",
            access_key_id=_ACCESS_KEY,
            secret_access_key=SecretStr(_SECRET_KEY),
            session_token=SecretStr(_SESSION_TOKEN),
            allowed_hosts=_ALLOWED_HOSTS,
        )
    )

    async def scenario() -> None:
        # Built lazily: an eagerly-constructed coroutine that a earlier failure
        # never awaits emits a RuntimeWarning and muddies the report.
        calls: tuple[tuple[str, Callable[[], Awaitable[object]]], ...] = (
            ("stat", lambda: store.stat("k")),
            ("get", lambda: store.get("k")),
            ("delete", lambda: store.delete("k")),
            ("put", lambda: store.put("k", b"v")),
        )
        for label, make_call in calls:
            with pytest.raises(ClientError) as exc_info:
                await make_call()
            assert exc_info.value.response["Error"]["Code"] == "NoSuchBucket", label

    asyncio.run(scenario())


# --- GOVERNANCE mode is NOT WORM -------------------------------------------
#
# S3 Object Lock has two modes and only COMPLIANCE is write-once. A principal
# holding s3:BypassGovernanceRetention can delete a GOVERNANCE-locked version
# and can shorten its retention — proven from a raw client in
# test_governance_lock_is_bypassable below, which is the empirical premise the
# whole group rests on. The adapter therefore refuses to launder a GOVERNANCE
# retention into a WORM claim (`ObjectStat.worm_retain_until`) or into a
# `WormViolationError`, and `archive()` upgrades such a lock to COMPLIANCE.
#
# Objects archived by S3ObjectStore are ALWAYS COMPLIANCE, so a GOVERNANCE lock
# reaches this adapter only from another tool, a bucket default, or a prior
# deployment — a narrow but real exposure, since under C10 the product would
# otherwise report WORM protection the bytes do not have.


def _put_governance_locked(bucket: str, key: str, body: bytes, retain_until: datetime) -> str:
    """Write ``key`` with a GOVERNANCE lock via a RAW client; return its VersionId.

    Raw on purpose: S3ObjectStore can only ever write COMPLIANCE, so the
    externally-written lock these tests are about is unreachable through the
    adapter's own API.
    """

    async def _write() -> str:
        async with _raw_client() as client:
            resp = await client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ObjectLockMode="GOVERNANCE",
                ObjectLockRetainUntilDate=retain_until,
            )
            head = await client.head_object(Bucket=bucket, Key=key, VersionId=resp["VersionId"])
            # Pin the premise: if MinIO ever silently stored something other
            # than a GOVERNANCE lock, every test below would be vacuous.
            assert head["ObjectLockMode"] == "GOVERNANCE"
            return str(resp["VersionId"])

    return asyncio.run(_write())


def test_governance_lock_is_bypassable() -> None:
    """The premise: a GOVERNANCE lock is NOT write-once.

    With ``BypassGovernanceRetention`` the retention shortens and the version
    deletes — both from a raw client, both while the retention is nominally in
    force. This is why the adapter must not report it as WORM. Contrast
    test_raw_client_delete_version_denied / test_raw_client_shorten_retention_denied,
    where the identical calls against a COMPLIANCE lock are refused.
    """
    _store, bucket = _make_bucket_and_store()
    far = _normalize_retain_until(datetime.now(UTC) + _FAR_DELTA)
    version_id = _put_governance_locked(bucket, "k", b"payload", far)

    async def scenario() -> None:
        async with _raw_client() as client:
            nearer = _normalize_retain_until(datetime.now(UTC) + _NEAR_DELTA)
            await client.put_object_retention(
                Bucket=bucket,
                Key="k",
                VersionId=version_id,
                Retention={"Mode": "GOVERNANCE", "RetainUntilDate": nearer},
                BypassGovernanceRetention=True,
            )
            head = await client.head_object(Bucket=bucket, Key="k", VersionId=version_id)
            assert head["ObjectLockRetainUntilDate"] == nearer, "GOVERNANCE retention shortened"

            await client.delete_object(
                Bucket=bucket, Key="k", VersionId=version_id, BypassGovernanceRetention=True
            )
            with pytest.raises(ClientError):
                await client.head_object(Bucket=bucket, Key="k", VersionId=version_id)

    asyncio.run(scenario())


def test_governance_lock_not_reported_as_worm_retain_until() -> None:
    """``stat()`` must not present a bypass-able lock as a WORM guarantee."""
    store, bucket = _make_bucket_and_store()
    far = _normalize_retain_until(datetime.now(UTC) + _FAR_DELTA)
    _put_governance_locked(bucket, "k", b"payload", far)

    async def scenario() -> None:
        stat = await store.stat("k")
        assert stat.worm_retain_until is None
        assert stat.size == len(b"payload")
        listed = {s.key: s for s in await store.list_objects()}
        assert listed["k"].worm_retain_until is None

    asyncio.run(scenario())


def test_put_and_delete_allowed_over_governance_lock() -> None:
    """No spurious ``WormViolationError``: a GOVERNANCE lock is not a WORM lock.

    The server permits both calls (a ``put`` writes a new version, a versionless
    ``delete`` lays a delete marker), so a client-side refusal would be the
    adapter inventing a restriction on the strength of a guarantee that does
    not exist.
    """
    store, bucket = _make_bucket_and_store()
    far = _normalize_retain_until(datetime.now(UTC) + _FAR_DELTA)
    _put_governance_locked(bucket, "k", b"payload", far)

    async def scenario() -> None:
        stat = await store.put("k", b"replacement")
        assert stat.worm_retain_until is None
        assert await store.get("k") == b"replacement"
        await store.delete("k")

    asyncio.run(scenario())


def test_archive_over_governance_lock_upgrades_to_compliance() -> None:
    """``archive()`` STRENGTHENS a GOVERNANCE lock instead of refusing it.

    Upgrading is not the "shortening" that "retention may only be extended"
    exists to prevent — it converts a bypass-able guardrail into real WORM. The
    proof is not the mode string: the same shorten that succeeded with bypass
    in test_governance_lock_is_bypassable must now be REFUSED by the server.

    Round 3: the upgrade is now OPT-IN (``allow_governance_upgrade``), so this
    test enables it explicitly. Every assertion below is unchanged — the
    behaviour under the opt-in is exactly what it was; only the default moved.
    The default's refusal is pinned by
    test_archive_over_governance_lock_refused_by_default.
    """
    store, bucket = _make_bucket_and_store(allow_governance_upgrade=True)
    near = _normalize_retain_until(datetime.now(UTC) + _NEAR_DELTA)
    version_id = _put_governance_locked(bucket, "k", b"payload", near)

    async def scenario() -> None:
        far = _normalize_retain_until(datetime.now(UTC) + _FAR_DELTA)
        receipt = await store.archive("k", retain_until=far)
        assert receipt.retain_until == far

        stat = await store.stat("k")
        assert stat.worm_retain_until == far

        async with _raw_client() as client:
            head = await client.head_object(Bucket=bucket, Key="k", VersionId=version_id)
            assert head["ObjectLockMode"] == "COMPLIANCE"
            with pytest.raises(ClientError):
                await client.put_object_retention(
                    Bucket=bucket,
                    Key="k",
                    VersionId=version_id,
                    Retention={"Mode": "GOVERNANCE", "RetainUntilDate": near},
                    BypassGovernanceRetention=True,
                )

        # And the WORM courtesy error is back on, keyed on the new COMPLIANCE lock.
        with pytest.raises(WormViolationError):
            await store.put("k", b"v2")

    asyncio.run(scenario())


def test_archive_over_governance_lock_never_shortens() -> None:
    """The upgrade must not trade a long weak lock for a short strong one.

    MinIO accepts an EARLIER COMPLIANCE date over a standing GOVERNANCE lock
    when the bypass flag is set (verified), so the adapter clamps the effective
    retention to the later of the two: stronger in mode, never shorter in
    duration. The receipt reports what was actually written.

    Round 3: run under the ``allow_governance_upgrade`` opt-in; assertions
    unchanged. The clamp is exactly the read-then-write that made the upgrade
    opt-in in the first place — it is correct against a single writer and
    loses a concurrent extension, which is why it is no longer the default.
    """
    store, bucket = _make_bucket_and_store(allow_governance_upgrade=True)
    far = _normalize_retain_until(datetime.now(UTC) + _FAR_DELTA)
    version_id = _put_governance_locked(bucket, "k", b"payload", far)

    async def scenario() -> None:
        near = _normalize_retain_until(datetime.now(UTC) + _NEAR_DELTA)
        receipt = await store.archive("k", retain_until=near)
        assert receipt.retain_until == far, "archive() shortened a standing retention"

        stat = await store.stat("k")
        assert stat.worm_retain_until == far

        async with _raw_client() as client:
            head = await client.head_object(Bucket=bucket, Key="k", VersionId=version_id)
            assert head["ObjectLockMode"] == "COMPLIANCE"
            assert head["ObjectLockRetainUntilDate"] == far

    asyncio.run(scenario())


def test_archive_over_governance_lock_refused_by_default() -> None:
    """DEFAULT: archive() must REFUSE to upgrade a standing GOVERNANCE lock.

    Codex round-3 finding. The upgrade clamps to ``max(requested, governance)``,
    which is a READ-then-WRITE, and S3 Object Lock has no compare-and-set:

      1. version carries GOVERNANCE until day 30
      2. caller A heads it, computes effective = day 30
      3. caller B extends the GOVERNANCE retention to day 365
      4. caller A writes COMPLIANCE until day 30 — B's day-365 retention is
         gone, permanently, since COMPLIANCE cannot be shortened

    The race cannot be closed, only avoided or disclosed. So the default
    refuses and never touches the existing lock — the default path is
    incapable of weakening anything. This asserts the refusal AND that the
    standing GOVERNANCE lock is left exactly as it was.
    """
    store, bucket = _make_bucket_and_store()
    far = _normalize_retain_until(datetime.now(UTC) + _FAR_DELTA)
    version_id = _put_governance_locked(bucket, "k", b"payload", far)

    async def scenario() -> None:
        with pytest.raises(GovernanceUpgradeRefusedError, match="ALLOW_GOVERNANCE_UPGRADE"):
            await store.archive("k", retain_until=datetime.now(UTC) + 2 * _FAR_DELTA)

        # Untouched: same mode, same date. Refusing must not be a partial write.
        async with _raw_client() as client:
            head = await client.head_object(Bucket=bucket, Key="k", VersionId=version_id)
            assert head["ObjectLockMode"] == "GOVERNANCE"
            assert head["ObjectLockRetainUntilDate"] == far

    asyncio.run(scenario())


# --- Object Lock LEGAL HOLD is a real, dateless, server-enforced lock --------
#
# Round-3 P2-1. A legal hold has no retain-until date and is cleared only by an
# explicit PutObjectLegalHold Status=OFF. Unlike GOVERNANCE there is no bypass
# permission — the server refuses to delete a held version outright (pinned by
# test_legal_hold_version_delete_denied_by_server below, the empirical premise
# the rest of the group rests on). Before round 3 the adapter did not know the
# field existed: put/delete were both ALLOWED over a held version, the
# adapter's own delete() laid a marker over genuinely protected bytes, and
# list_objects then reported the key ABSENT — the round-1 C10 hole reopened for
# a protection mode the delete-marker fix did not consider.


async def _aput_legal_held(bucket: str, key: str, body: bytes) -> str:
    """Async twin of :func:`_put_legal_held`, for callers already inside a loop.

    Same premise assertions; the sync wrapper below just drives this one.
    """
    async with _raw_client() as client:
        resp = await client.put_object(
            Bucket=bucket, Key=key, Body=body, ObjectLockLegalHoldStatus="ON"
        )
        head = await client.head_object(Bucket=bucket, Key=key, VersionId=resp["VersionId"])
        # Pin the premise: verified field name/casing against MinIO
        # RELEASE.2025-09-07T16-13-09Z. Two earlier rounds found MinIO
        # diverging from the AWS docs, so this is asserted, not assumed.
        assert head["ObjectLockLegalHoldStatus"] == "ON"
        assert "ObjectLockRetainUntilDate" not in head, "premise: hold-only, no retention"
        return str(resp["VersionId"])


def _put_legal_held(bucket: str, key: str, body: bytes) -> str:
    """Write ``key`` under a LEGAL HOLD and NO retention date; return its VersionId.

    Raw on purpose: S3ObjectStore never writes legal holds, so this lock can
    only arrive from another tool, a bucket default, or a compliance team's
    manual action — which is exactly the case the adapter has to survive.
    """

    return asyncio.run(_aput_legal_held(bucket, key, body))


def test_legal_hold_version_delete_denied_by_server() -> None:
    """The premise: a legal hold IS server-enforced, with no bypass flag.

    This is what separates a legal hold from GOVERNANCE — permitting writes
    over a GOVERNANCE lock is defensible because it genuinely is not WORM;
    permitting them over a legal hold is not.
    """
    _store, bucket = _make_bucket_and_store()
    version_id = _put_legal_held(bucket, "k", b"payload")

    async def scenario() -> None:
        async with _raw_client() as client:
            with pytest.raises(ClientError) as exc_info:
                await client.delete_object(Bucket=bucket, Key="k", VersionId=version_id)
            assert exc_info.value.response["Error"]["Code"] in {"AccessDenied", "InvalidRequest"}
            # There is no BypassGovernanceRetention equivalent: the same call
            # with the governance bypass flag is refused too.
            with pytest.raises(ClientError):
                await client.delete_object(
                    Bucket=bucket,
                    Key="k",
                    VersionId=version_id,
                    BypassGovernanceRetention=True,
                )
            resp = await client.get_object(Bucket=bucket, Key="k", VersionId=version_id)
            async with resp["Body"] as stream:
                assert await stream.read() == b"payload"

    asyncio.run(scenario())


def test_legal_hold_blocks_put_and_delete_and_keeps_key_listed() -> None:
    """C10 regression (round-3 P2-1): a legal hold must be RECOGNIZED as a lock.

    Pre-fix, verbatim against MinIO:

        stat worm_retain_until: None
        adapter put:    ALLOWED
        adapter delete: ALLOWED (delete marker laid)
        list_objects sees key: []

    i.e. the adapter itself destroyed the visibility of bytes the server was
    protecting indefinitely. Post-fix ``put``/``delete`` raise and the key
    stays listed. ``worm_retain_until`` remains ``None`` deliberately — that
    field is an INSTANT and a hold has none; ``None`` there means "no dated
    retention", not "unprotected".
    """
    store, bucket = _make_bucket_and_store()
    _put_legal_held(bucket, "k", b"payload")

    async def scenario() -> None:
        stat = await store.stat("k")
        assert stat.worm_retain_until is None  # no date exists to report
        assert stat.size == len(b"payload")

        with pytest.raises(WormViolationError, match="LEGAL HOLD"):
            await store.put("k", b"tampered")
        with pytest.raises(WormViolationError, match="LEGAL HOLD"):
            await store.delete("k")

        # No delete marker was laid, so the key is still listed and still reads.
        listed = {s.key for s in await store.list_objects()}
        assert "k" in listed
        assert await store.get("k") == b"payload"

    asyncio.run(scenario())


def test_legal_hold_survives_a_delete_marker_in_listing() -> None:
    """A held key stays LISTED even behind an out-of-band delete marker.

    The delete-marker fix resolved keys to the locked version by COMPLIANCE
    retention only, so a hold-only key vanished from ``list_objects`` exactly
    as an archived key used to — the same C10 shape, one protection mode over.
    """
    store, bucket = _make_bucket_and_store()
    _put_legal_held(bucket, "k", b"payload")

    async def scenario() -> None:
        async with _raw_client() as client:
            resp = await client.delete_object(Bucket=bucket, Key="k")
            assert resp.get("DeleteMarker") is True

        assert "k" in {s.key for s in await store.list_objects()}
        assert await store.get("k") == b"payload"
        assert (await store.stat("k")).size == len(b"payload")

    asyncio.run(scenario())


def test_archive_over_legal_hold_applies_retention() -> None:
    """``archive()`` over a hold-only version applies the requested retention.

    A hold carries no date, so there is nothing to extend or shorten — the
    requested COMPLIANCE retention is strictly ADDITIVE protection, and the
    hold stays in force independently. Also a regression guard: the pre-fix
    code read ``locked_head["ObjectLockRetainUntilDate"]`` unconditionally,
    which is a bare ``KeyError`` on a hold-only version.
    """
    store, bucket = _make_bucket_and_store()
    version_id = _put_legal_held(bucket, "k", b"payload")

    async def scenario() -> None:
        far = _normalize_retain_until(datetime.now(UTC) + _FAR_DELTA)
        receipt = await store.archive("k", retain_until=far)
        assert receipt.retain_until == far
        assert (await store.stat("k")).worm_retain_until == far

        async with _raw_client() as client:
            head = await client.head_object(Bucket=bucket, Key="k", VersionId=version_id)
            assert head["ObjectLockMode"] == "COMPLIANCE"
            assert head["ObjectLockRetainUntilDate"] == far
            assert head["ObjectLockLegalHoldStatus"] == "ON", "the hold must survive the archive"

    asyncio.run(scenario())


# --- The documented IAM action list must actually be sufficient --------------


def _restricted_store(bucket: str, actions: Sequence[str]) -> S3ObjectStore:
    """An S3ObjectStore whose credential holds EXACTLY ``actions`` and nothing else.

    Uses MinIO's STS ``AssumeRole`` with an INLINE session policy, which
    intersects with the (root) parent policy — so the session's effective
    permissions are precisely ``actions``. Chosen over provisioning a MinIO
    user because it needs no ``mc`` binary, no ``docker exec`` and no admin
    API: it is a plain in-process ``sts`` client call, so it runs identically
    here and in CI.
    """

    def _sts_client() -> Any:
        # Untyped return for the same reason as _raw_client above: aiobotocore
        # ships no stubs, so pyright resolves assume_role through
        # AioBaseClient.__getattr__ (inferred NoReturn) and falsely reports
        # "NoReturn is not awaitable".
        return get_session().create_client(
            "sts",
            endpoint_url=_STS_ENDPOINT,
            region_name="us-east-1",
            aws_access_key_id=_ACCESS_KEY,
            aws_secret_access_key=_SECRET_KEY,
            aws_session_token=_SESSION_TOKEN or None,
        )

    async def _assume() -> tuple[str, str, str]:
        async with _sts_client() as sts:
            resp = await sts.assume_role(
                RoleArn=_STS_ROLE_ARN,
                RoleSessionName="nestoragedociam",
                Policy=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": list(actions),
                                "Resource": ["arn:aws:s3:::*", "arn:aws:s3:::*/*"],
                            }
                        ],
                    }
                ),
                DurationSeconds=900,
            )
        creds = resp["Credentials"]
        return (
            str(creds["AccessKeyId"]),
            str(creds["SecretAccessKey"]),
            str(creds["SessionToken"]),
        )

    access_key, secret_key, token = asyncio.run(_assume())
    return S3ObjectStore(
        S3ObjectStoreSettings(
            endpoint_url=_ENDPOINT,
            bucket=bucket,
            access_key_id=access_key,
            secret_access_key=SecretStr(secret_key),
            session_token=SecretStr(token),
            allowed_hosts=_ALLOWED_HOSTS,
        )
    )


# EXACTLY the minimal IAM action list published in packages/ne-storage/README.md
# and docs/DEPLOYMENT.md. Kept as a literal so that dropping an action from the
# docs without dropping it here (or vice versa) shows up as a test change in
# review rather than passing unnoticed.
_DOCUMENTED_IAM_ACTIONS = (
    "s3:PutObject",
    "s3:GetObject",
    "s3:ListBucket",
    "s3:GetObjectVersion",
    "s3:ListBucketVersions",
    "s3:GetObjectRetention",
    # Round-4 BLOCKING. On real AWS, HeadObject returns
    # x-amz-object-lock-legal-hold ONLY to a caller holding this action;
    # without it the call still returns 200 and OMITS the header, so a legal
    # hold reads as absent and _locked_version misses a hold-only version —
    # delete() then lays a delete marker over a protected checkpoint and
    # list_objects reports it gone. MinIO returns the header regardless of
    # permission, so its ABSENCE cannot be reproduced against this test server;
    # the guard for that is
    # test_s3_unit.py::test_documented_iam_list_covers_permission_gated_lock_header_reads,
    # which asserts the published docs list it. What the live suite CAN prove
    # is that this list is SUFFICIENT to run the legal-hold path end to end —
    # see test_documented_iam_actions_suffice_for_the_checkpoint_write_path.
    "s3:GetObjectLegalHold",
    "s3:PutObjectRetention",
    "s3:GetBucketLocation",
    "s3:GetBucketObjectLockConfiguration",
)


def test_documented_iam_actions_suffice_for_the_checkpoint_write_path() -> None:
    """The DOCUMENTED minimal IAM list must actually run write_checkpoint's path.

    Round-3 P1-1, found empirically: the docs omitted
    ``s3:GetBucketObjectLockConfiguration``, but ``archive()`` calls
    ``GetObjectLockConfiguration`` unconditionally at the top of every
    invocation. A credential provisioned with exactly the documented list got:

        put: OK
        archive: FAILED AccessDenied ... calling the GetObjectLockConfiguration operation

    ``write_checkpoint`` does ``put`` THEN ``archive``, so an operator
    following the docs verbatim had every checkpoint object land in the bucket
    UNLOCKED before the loop died — a C10 downgrade on the documented happy
    path. This test drives the real adapter with a credential scoped to exactly
    the published list, so the docs cannot drift away from the code again
    without CI going red.

    Round-4 BLOCKING extended it beyond ORDINARY RETENTION to a LEGAL HOLD.
    That gap is why this docs-drift guard missed a second IAM defect: the list
    omitted ``s3:GetObjectLegalHold``, which on real AWS gates the
    ``x-amz-object-lock-legal-hold`` HeadObject header — omitted rather than
    denied when the permission is absent, so the whole legal-hold protection
    was inert in production while every MinIO test stayed green. Exercising
    only the retention path could never have surfaced it.

    Scope note, stated plainly: MinIO returns that header regardless of
    permission, so the legal-hold leg below is a SUFFICIENCY proof (the
    published list runs the path without an AccessDenied), NOT a reproduction
    of the AWS fail-open. The reproduction lives in
    test_s3_unit.py::test_documented_iam_list_covers_permission_gated_lock_header_reads.
    """
    _root_store, bucket = _make_bucket_and_store()
    store = _restricted_store(bucket, _DOCUMENTED_IAM_ACTIONS)

    async def scenario() -> None:
        await store.put("k", b"payload")
        far = _normalize_retain_until(datetime.now(UTC) + _NEAR_DELTA)
        receipt = await store.archive("k", retain_until=far)
        assert receipt.retain_until == far
        assert (await store.stat("k")).worm_retain_until == far
        assert await store.get("k") == b"payload"
        assert "k" in {s.key for s in await store.list_objects()}

        # --- and the LEGAL HOLD path, with the same restricted credential ---
        # A hold arrives from another tool / a bucket default / a compliance
        # team, so it is placed with the root client; every subsequent read is
        # driven through the documented-IAM store.
        await _aput_legal_held(bucket, "held", b"held-payload")

        # The hold must be VISIBLE to this credential: it is what makes the
        # key count as locked. If s3:GetObjectLegalHold were missing on real
        # AWS these four assertions are exactly what would silently invert.
        assert await store.get("held") == b"held-payload"
        assert "held" in {s.key for s in await store.list_objects()}
        with pytest.raises(WormViolationError):
            await store.put("held", b"overwrite")
        with pytest.raises(WormViolationError):
            await store.delete("held")

        # A hold-only version has no date to compare, so archive() applies the
        # requested retention as-is — and the documented list must suffice for
        # that PutObjectRetention too.
        held_receipt = await store.archive("held", retain_until=far)
        assert held_receipt.retain_until == far

        # The key survived the whole sequence: still listed, still readable.
        assert "held" in {s.key for s in await store.list_objects()}
        assert await store.get("held") == b"held-payload"

    asyncio.run(scenario())


def test_missing_lock_config_permission_fails_typed_naming_the_action() -> None:
    """Without ``s3:GetBucketObjectLockConfiguration``, archive() must fail TYPED.

    ``AccessDenied`` is not in ``_OBJECT_LOCK_MISSING_CODES``, so pre-fix this
    re-raised a raw ``botocore.ClientError`` from the top of every ``archive()``
    — the one config fault on the C10 path that did NOT get the typed,
    named-remediation treatment the others do. The error must name the missing
    permission.
    """
    _root_store, bucket = _make_bucket_and_store()
    without = [a for a in _DOCUMENTED_IAM_ACTIONS if a != "s3:GetBucketObjectLockConfiguration"]
    store = _restricted_store(bucket, without)

    async def scenario() -> None:
        await store.put("k", b"payload")  # put still succeeds — this is the trap
        with pytest.raises(ObjectLockPermissionError, match="s3:GetBucketObjectLockConfiguration"):
            await store.archive("k", retain_until=datetime.now(UTC) + _NEAR_DELTA)

    asyncio.run(scenario())


# --- The per-key version walk is CAPPED, and a breach fails LOUD -------------


def test_version_pile_fails_loud_instead_of_stalling_verification() -> None:
    """Round-3 P2-2: an unbounded per-key version walk is a DoS on the C10 path.

    Every ``put``/``get``/``stat``/``delete``, and every key in
    ``list_objects``, issues one ``HeadObject`` PER VERSION (~2ms measured).
    ``s3:PutObject`` is a permission the deployment MUST grant, and layering
    versions is NOT blocked by the required deny-``s3:DeleteObject`` policy,
    which only covers deletes. Uncapped, 100k layered versions on one
    checkpoint key cost ~200s on that key alone and ``verify_checkpoints``
    stops completing: the anchor is not falsified, it becomes UNVERIFIABLE,
    silently.

    So the walk is capped and a breach RAISES. Truncating instead would be
    worse — a truncated scan can miss the locked version and report an
    archived key as unprotected. The cap is set low here so the test stays
    fast; the mechanism is identical at the 1000 default.
    """
    store, bucket = _make_bucket_and_store(max_versions_per_key=3)

    async def scenario() -> None:
        await store.put("k", b"original")
        await store.archive("k", retain_until=datetime.now(UTC) + _NEAR_DELTA)

        async with _raw_client() as client:
            for i in range(5):
                await client.put_object(Bucket=bucket, Key="k", Body=f"layer{i}".encode())

        # Every path that resolves the locked version must fail LOUD, and the
        # message must name the key and the tuning knob.
        for label, make_call in (
            ("stat", lambda: store.stat("k")),
            ("get", lambda: store.get("k")),
            ("put", lambda: store.put("k", b"x")),
            ("delete", lambda: store.delete("k")),
            ("list_objects", lambda: store.list_objects()),
            ("archive", lambda: store.archive("k", retain_until=datetime.now(UTC))),
        ):
            with pytest.raises(TooManyObjectVersionsError, match="MAX_VERSIONS_PER_KEY") as info:
                await make_call()
            assert "'k'" in str(info.value), label

        # Single-key operations on a DIFFERENT key are unaffected: the walk in
        # _key_versions is scoped to one key, so `other` never sees `k`'s pile.
        #
        # `list_objects` is the deliberate exception, and the comment here used
        # to claim the opposite ("the cap is per key, not a bucket-wide kill
        # switch") while the loop 25 lines above already asserted list_objects
        # RAISES. The check is per key; the FAILURE is bucket-wide, because
        # list_objects raises for the whole call. That is a fail-closed choice
        # — a partial listing that silently omits a key is the C10 hole itself
        # — and it is documented as a tradeoff in packages/ne-storage/README.md
        # and docs/DEPLOYMENT.md. See
        # test_one_poisoned_key_halts_listing_and_the_error_names_it.
        assert await store.put("other", b"v") is not None

    asyncio.run(scenario())


def test_compliance_lock_behaviour_unchanged_by_mode_check() -> None:
    """Regression guard for the mode check itself.

    Everything the COMPLIANCE path did before must still hold: ``stat`` reports
    the retention, ``list_objects`` reports it, ``put``/``delete`` raise, and a
    shortening ``archive`` is refused. If a future refactor of the mode
    predicates ever inverted or dropped the COMPLIANCE case, this goes red
    rather than the guarantee going quietly missing.
    """
    store, _bucket = _make_bucket_and_store()

    async def scenario() -> None:
        await store.put("k", b"payload")
        far = _normalize_retain_until(datetime.now(UTC) + _FAR_DELTA)
        await store.archive("k", retain_until=far)

        assert (await store.stat("k")).worm_retain_until == far
        listed = {s.key: s for s in await store.list_objects()}
        assert listed["k"].worm_retain_until == far

        with pytest.raises(WormViolationError):
            await store.put("k", b"v2")
        with pytest.raises(WormViolationError):
            await store.delete("k")
        with pytest.raises(WormViolationError):
            await store.archive("k", retain_until=datetime.now(UTC) + _NEAR_DELTA)

        assert await store.get("k") == b"payload"

    asyncio.run(scenario())


# --- GOVERNANCE + LEGAL HOLD must route through the SAME opt-in gate ---------


def _put_governance_locked_and_held(
    bucket: str, key: str, body: bytes, retain_until: datetime
) -> str:
    """Write ``key`` with BOTH an active GOVERNANCE retention AND a legal hold.

    The combination round-4 P2-1 is about. Raw on purpose: S3ObjectStore writes
    neither GOVERNANCE locks nor legal holds, so this state can only arrive
    from another tool, a bucket default, or a compliance team's manual action.
    """

    async def _write() -> str:
        async with _raw_client() as client:
            resp = await client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ObjectLockMode="GOVERNANCE",
                ObjectLockRetainUntilDate=retain_until,
                ObjectLockLegalHoldStatus="ON",
            )
            head = await client.head_object(Bucket=bucket, Key=key, VersionId=resp["VersionId"])
            # Pin BOTH halves of the premise. If MinIO silently dropped either,
            # every assertion below would be testing a different scenario.
            assert head["ObjectLockMode"] == "GOVERNANCE"
            assert head["ObjectLockLegalHoldStatus"] == "ON"
            return str(resp["VersionId"])

    return asyncio.run(_write())


async def _head_version(bucket: str, key: str, version_id: str) -> dict[str, Any]:
    """HeadObject a specific version with a RAW client. Async: every caller is
    already inside an ``asyncio.run(scenario())``, so a nested ``asyncio.run``
    here would raise "cannot be called from a running event loop"."""
    async with _raw_client() as client:
        return dict(await client.head_object(Bucket=bucket, Key=key, VersionId=version_id))


def test_governance_plus_legal_hold_is_refused_by_the_opt_in_gate_by_default() -> None:
    """GOVERNANCE + legal hold must hit the SAME gate as a plain GOVERNANCE lock.

    Round-4 P2-1, a genuine repro. The legal hold makes the version count as
    locked, so ``archive()`` took the locked-version branch, where
    ``_compliance_retain_until`` is ``None`` (the mode is GOVERNANCE) — so the
    code read it as a bare hold-only lock and fell through intending to write a
    COMPLIANCE retention that may be SHORTER than the standing GOVERNANCE one,
    having skipped the opt-in gate, the ``max(requested, governance)`` clamp
    and ``BypassGovernanceRetention`` alike.

    Two defects, both fixed by hoisting the GOVERNANCE check ahead of the
    COMPLIANCE comparison:

    (a) The safety came entirely from the SERVER refusing the shortening — the
        code's intent on that path was to write a shorter COMPLIANCE date over
        a longer GOVERNANCE one. Now the refusal is OURS, before any write.
    (b) The server's refusal surfaced as ``WormViolationError`` reading
        "possible causes include a concurrent archive() race" for a PERMANENT,
        deterministic configuration state — the exact fabricated-race
        diagnosis a prior round declared fixed, reintroduced on a new path.
        Now it is a typed ``GovernanceUpgradeRefusedError`` naming the real
        cause and the real remedy.
    """
    store, bucket = _make_bucket_and_store()
    governance_until = _normalize_retain_until(datetime.now(UTC) + _FAR_DELTA)
    version_id = _put_governance_locked_and_held(bucket, "k", b"payload", governance_until)

    async def scenario() -> None:
        # A SHORTER retention than the standing GOVERNANCE one — the write the
        # broken path would have attempted.
        with pytest.raises(GovernanceUpgradeRefusedError) as info:
            await store.archive("k", retain_until=datetime.now(UTC) + _NEAR_DELTA)

        message = str(info.value)
        assert "GOVERNANCE" in message
        assert "NE_STORAGE_S3_ALLOW_GOVERNANCE_UPGRADE" in message
        # The fabricated diagnosis must NOT appear: nothing here is a race.
        assert "race" not in message.lower()

        # Nothing was touched — the default path cannot weaken an existing lock.
        head = await _head_version(bucket, "k", version_id)
        assert head["ObjectLockMode"] == "GOVERNANCE"
        assert head["ObjectLockRetainUntilDate"] == governance_until
        assert head["ObjectLockLegalHoldStatus"] == "ON"

    asyncio.run(scenario())


def test_governance_plus_legal_hold_upgrade_is_reachable_when_opted_in() -> None:
    """With the opt-in set, the GOVERNANCE + hold combination must be ARCHIVABLE.

    The other half of round-4 P2-1, and the more surprising one: the upgrade
    path was UNREACHABLE for this combination, so an operator who had
    deliberately enabled ``allow_governance_upgrade`` could not archive such a
    key AT ALL — every attempt died on the server's refusal of the
    un-bypassed, un-clamped write.

    Also pins the CLAMP, which the broken path skipped entirely: a requested
    retention SHORTER than the standing GOVERNANCE one must come back out as
    ``max(requested, governance)``, never the shorter requested date. Trading a
    longer weak lock for a shorter strong one would be a real downgrade.
    """
    store, bucket = _make_bucket_and_store(allow_governance_upgrade=True)
    governance_until = _normalize_retain_until(datetime.now(UTC) + _FAR_DELTA)
    version_id = _put_governance_locked_and_held(bucket, "k", b"payload", governance_until)

    async def scenario() -> None:
        short = _normalize_retain_until(datetime.now(UTC) + _NEAR_DELTA)
        receipt = await store.archive("k", retain_until=short)

        # Clamped UP to the standing GOVERNANCE date, not written at `short`.
        assert receipt.retain_until == governance_until

        head = await _head_version(bucket, "k", version_id)
        assert head["ObjectLockMode"] == "COMPLIANCE", "the upgrade must have happened"
        assert head["ObjectLockRetainUntilDate"] == governance_until
        assert head["ObjectLockLegalHoldStatus"] == "ON", "the hold must survive the upgrade"

        # And the key behaves as a genuinely WORM-locked one afterwards.
        assert (await store.stat("k")).worm_retain_until == governance_until
        with pytest.raises(WormViolationError):
            await store.delete("k")

    asyncio.run(scenario())


# --- The version cap fails CLOSED for the whole listing, and says which key --


def test_one_poisoned_key_halts_listing_and_the_error_names_it() -> None:
    """Round-4 P2-2, ACCEPTED as a tradeoff — but it must be ACTIONABLE.

    The cap is checked per key; the FAILURE is bucket-wide, because
    ``list_objects`` raises for the entire call. So five raw puts to an
    unrelated junk key make ``list_objects()`` raise and a legitimate, locked
    checkpoint nowhere near the cap is NOT listed. The vector is
    ``s3:PutObject`` — which the deployment must grant and which the required
    deny-``s3:DeleteObject`` policy does not cover — and the poisoned key need
    not be a checkpoint key at all.

    **Failing closed is the DECIDED behaviour and this test pins it, not a
    partial listing.** A listing that silently omits a key is the C10 hole
    itself: nothing downstream can distinguish "absent because deleted" from
    "absent because the scan gave up".

    What the fix owes the operator is ATTRIBUTION. Aborting the whole listing
    without saying WHICH key is unactionable, so the error must name the
    offending key — and, critically, must name the POISONED key rather than
    the innocent one the caller was looking for.
    """
    store, bucket = _make_bucket_and_store(max_versions_per_key=3)
    poison = "zzz-poison"
    legit = "aaa-checkpoint"

    async def scenario() -> None:
        await store.put(legit, b"checkpoint-bytes")
        far = _normalize_retain_until(datetime.now(UTC) + _FAR_DELTA)
        await store.archive(legit, retain_until=far)

        # The healthy baseline: the locked checkpoint IS listed.
        assert legit in {s.key for s in await store.list_objects()}

        async with _raw_client() as client:
            for i in range(5):
                await client.put_object(Bucket=bucket, Key=poison, Body=f"junk{i}".encode())

        with pytest.raises(TooManyObjectVersionsError) as info:
            await store.list_objects()

        message = str(info.value)
        # ATTRIBUTION is the whole point of accepting the abort: the operator
        # must learn which key to remediate. Naming the innocent key, or
        # naming none, would leave them with an unactionable hard stop.
        assert f"'{poison}'" in message, message
        assert legit not in message, "the innocent key must not be blamed"
        assert "MAX_VERSIONS_PER_KEY" in message, "the remediation knob must be named"

        # The locked checkpoint itself is untouched and still individually
        # reachable — the halt is a listing-availability fault, not data loss.
        assert await store.get(legit) == b"checkpoint-bytes"
        assert (await store.stat(legit)).worm_retain_until == far

    asyncio.run(scenario())


# --- Strongest-protection-first: a legal hold DOMINATES a dated retention ----
#
# NE-S3-HOLD-ORDER. The pre-fix rule in `_locked_version` (and the twin loop in
# `list_objects`) preferred a DATED lock over a hold-only one, documented as
# intended because a date "carries strictly more information to report". That
# is backwards: a legal hold is INDEFINITE, so it outlasts every finite
# retention. The same defect was found and fixed first on the Azure adapter.
#
# Severity is P2, not P1, and these tests deliberately do NOT claim a hole:
# `best` is non-None whenever ANY version is protected, so put/delete stayed
# blocked under either ranking, and once the dated policy expired the loop fell
# through and re-selected the hold. Nothing was ever unprotected. What WAS
# wrong is which version got SELECTED, and selection is load-bearing: `get()`
# returns the selected version's BYTES and `stat()` reports the selected
# version's expiry. Ranking a finite date above an indefinite hold therefore
# handed back the wrong archived payload to a verifier reading through `get()`,
# and advertised a `worm_retain_until` implying protection ENDS on that date
# while another version was held with no end at all.

# Distinct LENGTHS, not just distinct bytes, so `stat().size` identifies the
# selected version as independently as `get()` does.
_HOLD_BODY = b"held-payload"  # 12 — hold only, no date
_DATED_BODY = b"dated-payload"  # 13 — dated only, no hold
_HELD_DATED_BODY = b"held-and-dated-payload"  # 22 — both


async def _aput_dated(
    bucket: str, key: str, body: bytes, retain_until: datetime, *, legal_hold: bool = False
) -> str:
    """Write ``key`` under a COMPLIANCE retention (optionally + a hold); return its VersionId.

    Raw on purpose, same reasoning as :func:`_put_legal_held`: the adapter
    never writes a legal hold itself, so a held-AND-dated version can only
    arrive from another tool or a compliance team's manual action.
    """
    extra: dict[str, Any] = {"ObjectLockLegalHoldStatus": "ON"} if legal_hold else {}
    async with _raw_client() as client:
        resp = await client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=retain_until,
            **extra,
        )
        head = await client.head_object(Bucket=bucket, Key=key, VersionId=resp["VersionId"])
        # Premise assertions: two earlier rounds found MinIO diverging from the
        # AWS docs on field name/casing, so this is asserted, not assumed.
        assert head["ObjectLockMode"] == "COMPLIANCE"
        assert head["ObjectLockRetainUntilDate"] == retain_until
        expected_hold = "ON" if legal_hold else "OFF"
        assert head.get("ObjectLockLegalHoldStatus", "OFF") == expected_hold
        return str(resp["VersionId"])


# (body, retain_until or None for hold-only, legal_hold)
_VersionSpec = tuple[bytes, "datetime | None", bool]


def _key_with_versions(specs: Sequence[_VersionSpec], *, reverse: bool) -> S3ObjectStore:
    """Build ONE key carrying ``specs`` as separate protected versions.

    ORDER-INDEPENDENCE is the property under test, so every call site
    parametrizes over ``reverse``. S3 lists versions newest-first, so flipping
    the WRITE order genuinely flips the order the selection loop sees — both
    orders it can ever encounter, exercised end-to-end with no monkeypatching.
    """
    store, bucket = _make_bucket_and_store()
    ordered = list(reversed(specs)) if reverse else list(specs)

    async def build() -> None:
        for body, retain_until, legal_hold in ordered:
            if retain_until is None:
                assert legal_hold, "an unprotected version is not a case these tests cover"
                await _aput_legal_held(bucket, "k", body)
            else:
                await _aput_dated(bucket, "k", body, retain_until, legal_hold=legal_hold)

    asyncio.run(build())
    return store


@pytest.mark.parametrize("reverse", [False, True], ids=["hold-oldest", "hold-newest"])
def test_legal_hold_outranks_an_active_dated_lock(reverse: bool) -> None:
    """GENUINE REPRO (both orders). Hold-only vs dated: the HELD version wins.

    Pre-fix, verbatim against MinIO, identically in both listing orders:

        get():                   b'dated-payload'   (wrong version's bytes)
        stat().worm_retain_until <a date ~24h out>  (implies protection ENDS)
        list_objects()[k].size   13                 (the dated version)

    Post-fix all three resolve to the indefinitely-held version.
    ``worm_retain_until`` is ``None`` there deliberately — that field is an
    INSTANT and a hold has none; ``None`` means "no dated retention", NOT
    "unprotected", exactly as in
    ``test_legal_hold_blocks_put_and_delete_and_keeps_key_listed``.
    """
    retain = _normalize_retain_until(datetime.now(UTC) + timedelta(days=1))
    store = _key_with_versions(
        [(_HOLD_BODY, None, True), (_DATED_BODY, retain, False)], reverse=reverse
    )

    async def scenario() -> None:
        assert await store.get("k") == _HOLD_BODY

        stat = await store.stat("k")
        assert stat.size == len(_HOLD_BODY)
        assert stat.worm_retain_until is None, (
            "reporting the dated version's finite expiry advertises an end date "
            "for a key another version holds indefinitely"
        )

        listed = {s.key: s for s in await store.list_objects()}
        assert listed["k"].size == len(_HOLD_BODY)
        assert listed["k"].worm_retain_until is None

        # P2, not P1: the key was NEVER unprotected under either ranking.
        with pytest.raises(WormViolationError, match="LEGAL HOLD"):
            await store.put("k", b"tampered")
        with pytest.raises(WormViolationError, match="LEGAL HOLD"):
            await store.delete("k")

    asyncio.run(scenario())


@pytest.mark.parametrize("reverse", [False, True], ids=["held-oldest", "held-newest"])
def test_held_and_dated_outranks_date_only_at_equal_expiry(reverse: bool) -> None:
    """Held-and-dated beats date-only at the SAME expiry, and the message leads with the hold.

    Mixed labelling, stated plainly:

    * ``held-oldest`` is a GENUINE REPRO of the selection defect. Both versions
      are dated with an identical expiry, so the pre-fix strict-``>`` compare
      resolved to the first version SEEN — the newest, i.e. the date-only one —
      and `get()` returned the wrong payload.
    * ``held-newest`` is a REGRESSION GUARD for selection: the pre-fix rule
      already happened to pick the held version there, for the unrelated reason
      that it was newest. It pins that the new rank tuple does not regress the
      case the old rule got right by accident.
    * The ERROR-MESSAGE assertion below is a GENUINE REPRO in BOTH orders:
      ``_lock_reason`` composed the retention clause first unconditionally, so
      a legally-held version's refusal led with a finite end date that does not
      exist for it.
    """
    retain = _normalize_retain_until(datetime.now(UTC) + timedelta(days=1))
    store = _key_with_versions(
        [(_HELD_DATED_BODY, retain, True), (_DATED_BODY, retain, False)], reverse=reverse
    )

    async def scenario() -> None:
        assert await store.get("k") == _HELD_DATED_BODY
        stat = await store.stat("k")
        assert stat.size == len(_HELD_DATED_BODY)
        # Both versions carry the SAME expiry, so the date is honestly
        # reportable here — unlike the hold-only case above.
        assert stat.worm_retain_until == retain

        with pytest.raises(WormViolationError) as info:
            await store.put("k", b"tampered")
        message = str(info.value)
        assert "LEGAL HOLD" in message
        assert message.index("LEGAL HOLD") < message.index("under WORM retention until"), (
            "leading with a finite retention on a legally-held version claims an "
            f"end date the version does not have: {message}"
        )

    asyncio.run(scenario())
