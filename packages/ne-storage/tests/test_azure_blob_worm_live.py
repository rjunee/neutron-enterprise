# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED Azure WORM proofs — SERVER-side enforcement, not adapter courtesy.

This file is the ONLY place the Azure WORM guarantee is actually proven, and it
runs ONLY against a REAL Azure storage account. It skips cleanly otherwise.

Why there is no emulator variant: **Azurite does not implement immutability
policies.** The S3 lane can point test_s3_conformance.py at a local MinIO and
get a genuine COMPLIANCE-mode server; Azure has no such local stand-in. Rather
than write a test that appears to prove WORM against an emulator that silently
ignores the policy — a vacuous proof, worse than no proof — the server-side
guarantee is parked here for the owner-gated NE-WORM-LIVE lane. The adapter's
own retention STATE MACHINE (extend/equal/earlier, error translation, ceiling)
is proven against a fake in test_azure_blob_unit.py; what is proven HERE is the
part only a real service can answer: that the SERVICE itself refuses.

Required env (all three, or every test skips):

* ``NE_TEST_AZURE_BLOB_ACCOUNT_URL`` — e.g. https://acct.blob.core.windows.net
* ``NE_TEST_AZURE_BLOB_ACCOUNT_KEY``
* ``NE_TEST_AZURE_BLOB_CONTAINER`` — a container with blob versioning AND
  version-level immutability (VLW) ENABLED. Both are provisioning-time
  properties (cf. deploy/terraform-azure/modules/storage/main.tf) and cannot
  be turned on from the data plane, which is why the container is supplied by
  env rather than created per test.

Isolation and cleanup: every test namespaces itself under a unique
``key_prefix`` inside that ONE container. Locked retention defaults to a SHORT
window (10 minutes, override with ``NE_TEST_AZURE_BLOB_RETENTION_MINUTES``)
because a real account PERSISTS — unlike MinIO's ephemeral container, nothing
here disappears when the test process exits. Locked blobs are undeletable by
design (that is the point), so expired blobs are reaped by the account's
lifecycle-management policy, never by these tests. The shared conformance kit
now uses that SAME short window (NE-WORM-LIVE set ``worm_retention`` on
``TestAzureBlobConformance``), so it no longer leaves day-locked blobs behind.
The one deliberate exception is ``write_checkpoint(retention_days=1)`` — that
API is day-granular, so its blob stays locked for a day and is reaped by the
account lifecycle policy, not by teardown.

Async is driven with ``asyncio.run`` (pytest-asyncio is not a dependency and
must not be added), matching test_s3_worm_live.py.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from azure.core.exceptions import HttpResponseError
from azure.storage.blob import BlobImmutabilityPolicyMode, ImmutabilityPolicy
from azure.storage.blob.aio import ContainerClient
from pydantic import SecretStr

from ne.audit import ensure_audit_schema
from ne.audit.checkpoint import HmacSigner, verify_checkpoints, write_checkpoint
from ne.audit.store import append_record
from ne.contracts.errors import WormViolationError
from ne.contracts.storage import ObjectStore
from ne.contracts.testing.kits.object_store import ObjectStoreContractKit
from ne.schema import AuditEvent, AuditOutcome
from ne.storage.azure_blob import AzureBlobObjectStore
from ne.storage.config import AzureBlobObjectStoreSettings
from ne.storage.s3 import _normalize_retain_until

_ACCOUNT_URL = os.environ.get("NE_TEST_AZURE_BLOB_ACCOUNT_URL", "")
_ACCOUNT_KEY = os.environ.get("NE_TEST_AZURE_BLOB_ACCOUNT_KEY", "")
_CONTAINER = os.environ.get("NE_TEST_AZURE_BLOB_CONTAINER", "")
_RETENTION_MINUTES = float(os.environ.get("NE_TEST_AZURE_BLOB_RETENTION_MINUTES", "10"))

_HEX_HASH = "a" * 64
_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

pytestmark = pytest.mark.skipif(
    not (_ACCOUNT_URL and _ACCOUNT_KEY and _CONTAINER),
    reason=(
        "real Azure storage account not configured (set NE_TEST_AZURE_BLOB_ACCOUNT_URL / "
        "_ACCOUNT_KEY / _CONTAINER, where the container has blob versioning AND "
        "version-level immutability enabled). There is deliberately NO emulator-backed "
        "variant: Azurite does not implement immutability policies, so an emulator run "
        "would prove nothing. This file is the owner-gated NE-WORM-LIVE lane's target."
    ),
)


def _retain_until() -> datetime:
    """A SHORT locked-retention instant — a real account persists (see module docstring)."""
    return datetime.now(UTC) + timedelta(minutes=_RETENTION_MINUTES)


def _fresh_prefix() -> str:
    return f"ne-worm-{uuid4().hex[:12]}/"


def _make_store(prefix: str | None = None) -> tuple[AzureBlobObjectStore, str]:
    """A store namespaced under a fresh key_prefix inside the ONE env container."""
    key_prefix = prefix if prefix is not None else _fresh_prefix()
    settings = AzureBlobObjectStoreSettings(
        account_url=_ACCOUNT_URL,
        container=_CONTAINER,
        account_key=SecretStr(_ACCOUNT_KEY),
        key_prefix=key_prefix,
    )
    return AzureBlobObjectStore(settings), key_prefix


def _raw_container() -> Any:
    """A RAW ContainerClient built straight from env — bypasses the adapter entirely.

    Untyped return for the same reason test_s3_worm_live.py's ``_raw_client``
    is untyped: keeps pyright from chasing SDK client internals in a test
    whose whole point is to poke the service directly.
    """
    return ContainerClient(
        account_url=_ACCOUNT_URL,
        container_name=_CONTAINER,
        credential=_ACCOUNT_KEY,
    )


class TestAzureBlobConformance(ObjectStoreContractKit):
    """The shared ObjectStore conformance kit against REAL Azure Blob.

    ``create_store_at`` deliberately raises ``NotImplementedError``: there is
    no way to inject a fake clock into a real Azure service's retention
    comparisons, so the kit's ``test_worm_expires`` self-skips — the same
    honesty the S3 conformance suite applies. The kit's WORM retention window
    is pinned to the same SHORT ``_RETENTION_MINUTES`` the rest of this file
    uses (NE-WORM-LIVE, 2026-07-19) — a real account PERSISTS, and a Locked
    blob is undeletable until its expiry, so day-scale kit locks would leave
    day-scale litter in the owner's evidence container on every run.
    """

    worm_retention = timedelta(minutes=_RETENTION_MINUTES)

    def create_store(self) -> ObjectStore:
        store, _prefix = _make_store()
        return store

    def create_store_at(self, now: Callable[[], datetime]) -> ObjectStore:
        raise NotImplementedError(
            "no injectable clock for a real Azure Blob service — WORM expiry cannot be "
            "faked against a live account"
        )


def test_raw_client_delete_locked_blob_denied() -> None:
    """The core proof: the SERVICE enforces WORM, not the adapter.

    A raw ContainerClient — not AzureBlobObjectStore — attempts to delete the
    Locked blob. Azure must refuse regardless of who is asking.
    """
    store, prefix = _make_store()

    async def scenario() -> None:
        await store.put("k", b"payload")
        await store.archive("k", retain_until=_retain_until())

        async with _raw_container() as container:
            blob = container.get_blob_client(f"{prefix}k")
            with pytest.raises(HttpResponseError) as exc_info:
                await blob.delete_blob()
            assert exc_info.value.status_code == 409

    asyncio.run(scenario())


def test_raw_client_shorten_retention_denied() -> None:
    """A Locked policy's expiry can only be EXTENDED — proven bypassing our code."""
    store, prefix = _make_store()

    async def scenario() -> None:
        await store.put("k", b"payload")
        # NE-WORM-LIVE: requires NE_TEST_AZURE_BLOB_RETENTION_MINUTES >= 2 so
        # that `shorter` (now + 1 minute) is genuinely shorter than `original`.
        original = _retain_until()
        await store.archive("k", retain_until=original)

        async with _raw_container() as container:
            blob = container.get_blob_client(f"{prefix}k")
            shorter = _normalize_retain_until(datetime.now(UTC) + timedelta(minutes=1))
            with pytest.raises(HttpResponseError) as exc_info:
                await blob.set_immutability_policy(
                    ImmutabilityPolicy(
                        expiry_time=shorter,
                        policy_mode=BlobImmutabilityPolicyMode.LOCKED,
                    )
                )
            # NE-WORM-LIVE (real-Azure divergence, proven live 2026-07-22): the
            # DENIAL is the guarantee — the ``pytest.raises`` above IS the proof
            # that shortening a Locked policy is server-refused. Real Azure
            # rejects "reducing UntilDate on a locked policy" with HTTP **400**
            # ("...is not allowed"), NOT 409; Azurite implements no immutability
            # so it can never exercise this path, which is why the original 409
            # was an unverified guess. Accept either server-side client-error
            # denial code. Do NOT relax the raises-at-all assertion (that is the
            # WORM guarantee); only the specific status code is corrected.
            assert exc_info.value.status_code in (400, 409)

    asyncio.run(scenario())


def test_legal_hold_is_server_enforced_and_stays_visible() -> None:
    """A legal hold is indefinite, server-enforced protection with NO expiry.

    Parked here because ONLY a real account can settle it: Azurite implements
    neither immutability policies nor legal holds, so an emulator run would
    prove nothing. The adapter's own handling of a hold is proven against the
    fake in test_azure_blob_unit.py; what is proven HERE is the three things
    only the SERVICE can answer:

    1. ``get_blob_properties`` really does surface ``has_legal_hold=True``
       (the unit fake asserts the adapter reads that attribute; this asserts
       the SDK actually populates it from ``x-ms-legal-hold``),
    2. the SERVICE refuses a raw delete of a legally-held blob even with no
       time-based policy in force, and
    3. the adapter reports ``worm_retain_until=None`` (a hold has no expiry)
       while STILL listing the key — "no retention date" must never be read
       as "unprotected and therefore absent".

    Cleanup is mandatory and unusual for this file: a held blob is
    undeletable indefinitely and lifecycle management cannot reap it, so the
    hold is released in a ``finally`` and the blob then deleted. No
    immutability policy is set, so the delete succeeds.
    """
    store, prefix = _make_store()

    async def scenario() -> None:
        await store.put("k", b"payload")

        async with _raw_container() as container:
            blob = container.get_blob_client(f"{prefix}k")
            await blob.set_legal_hold(True)
            try:
                props = await blob.get_blob_properties()
                assert props.has_legal_hold is True
                # No time-based policy was ever set on this blob.
                assert props.immutability_policy.expiry_time is None

                # (2) the SERVICE refuses, bypassing our adapter entirely.
                with pytest.raises(HttpResponseError) as exc_info:
                    await blob.delete_blob()
                assert exc_info.value.status_code == 409

                # (3) the adapter refuses too, reports no expiry, stays listed.
                with pytest.raises(WormViolationError, match="legal hold"):
                    await store.delete("k")
                with pytest.raises(WormViolationError, match="legal hold"):
                    await store.put("k", b"tampered")

                stat = await store.stat("k")
                assert stat.worm_retain_until is None
                assert [s.key for s in await store.list_objects()] == ["k"]
                assert await store.get("k") == b"payload"
            finally:
                await blob.set_legal_hold(False)
                await blob.delete_blob()

    asyncio.run(scenario())


def test_retain_until_precision_roundtrip() -> None:
    """Empirical pin for the RFC1123 whole-second claim in azure_blob.py's docstring.

    A sub-second ``retain_until`` is ceiled to the next whole second BEFORE it
    reaches Azure, so the receipt is never earlier than what the caller asked
    for, and the value the SERVICE stores matches the ceiled instant exactly
    after a real round-trip.
    """
    store, _prefix = _make_store()

    async def scenario() -> None:
        await store.put("k", b"v")
        sub_second = datetime.now(UTC).replace(microsecond=654_321) + timedelta(
            minutes=_RETENTION_MINUTES
        )
        receipt = await store.archive("k", retain_until=sub_second)

        expected = _normalize_retain_until(sub_second)
        assert receipt.retain_until == expected
        assert receipt.retain_until >= sub_second

        stat = await store.stat("k")
        assert stat.worm_retain_until == expected

    asyncio.run(scenario())


def test_service_reports_locked_mode_and_container_default_is_unlocked() -> None:
    """The two things ONLY a real account can settle about ``policy_mode``.

    The adapter treats an ``Unlocked`` policy as ABSENT (not WORM) and matches
    ``Locked`` case-INSENSITIVELY. Both choices rest on facts a fake cannot
    establish, because a fake returns whatever it is told:

    1. **What casing the SERVICE puts on the wire.** ``policy_mode`` is
       deserialized from the ``x-ms-immutability-policy-mode`` response header
       with the bare type ``"str"`` — the generated client does no enum
       coercion and no case normalization (verified by introspection against
       azure-storage-blob 12.30.0). So the read-back casing is the service's
       choice, and a case-sensitive check would be a latent C10 downgrade.
       This test records what the service actually returns.

    2. **That a container default-VLW policy really does arrive Unlocked**,
       which is the premise of the whole Unlocked-handling path and the state
       the audit checkpoint loop meets in production.

    Deliberately assert-light on (2): a container may or may not carry a
    default policy, so its absence is reported, not failed.
    """
    store, prefix = _make_store()

    async def scenario() -> None:
        await store.put("k", b"v")

        # (2) Whatever policy the CONTAINER stamped on a freshly-written blob,
        # before this adapter has locked anything.
        async with _raw_container() as container:
            props = await container.get_blob_client(f"{prefix}k").get_blob_properties()
        stamped = getattr(props.immutability_policy, "policy_mode", None)
        if stamped is not None:
            unlocked = BlobImmutabilityPolicyMode.UNLOCKED.value.casefold()
            assert str(stamped).casefold() == unlocked, (
                f"container default-VLW policy arrived as {stamped!r}, expected Unlocked; "
                "the adapter's Unlocked-is-not-WORM path assumes this"
            )
            # An unlocked policy must not be reported as a WORM guarantee.
            assert (await store.stat("k")).worm_retain_until is None

        # (1) Lock it, then read back the mode the SERVICE reports.
        retain = _retain_until()
        await store.archive("k", retain_until=retain)
        async with _raw_container() as container:
            locked = await container.get_blob_client(f"{prefix}k").get_blob_properties()
        mode = locked.immutability_policy.policy_mode
        assert mode is not None
        assert str(mode).casefold() == BlobImmutabilityPolicyMode.LOCKED.value.casefold(), (
            f"service reported policy_mode={mode!r} after a Locked archive()"
        )
        # And the adapter now reports it as genuine WORM.
        assert (await store.stat("k")).worm_retain_until is not None

    asyncio.run(scenario())


def test_archive_shorter_than_container_default_still_locks() -> None:
    """Case (b) against the real service: a retention SHORTER than the
    container default must still end up LOCKED, and must not shorten.

    This is the audit checkpoint loop's exact shape (``retention_days=1``
    against a longer container default). The pre-fix adapter took the
    "retention may only be extended" branch and never called
    ``set_immutability_policy`` at all, leaving the blob unlocked forever.
    """
    store, prefix = _make_store()

    async def scenario() -> None:
        await store.put("k", b"v")

        async with _raw_container() as container:
            before = await container.get_blob_client(f"{prefix}k").get_blob_properties()
        default_expiry = getattr(before.immutability_policy, "expiry_time", None)
        if default_expiry is None:
            pytest.skip(
                "container has no default immutability policy, so the "
                "shorter-than-default case cannot arise here"
            )

        # Deliberately EARLIER than the container default.
        requested = datetime.now(UTC) + timedelta(minutes=_RETENTION_MINUTES)
        receipt = await store.archive("k", retain_until=requested)

        async with _raw_container() as container:
            after = await container.get_blob_client(f"{prefix}k").get_blob_properties()
        mode = after.immutability_policy.policy_mode
        assert mode is not None
        assert str(mode).casefold() == BlobImmutabilityPolicyMode.LOCKED.value.casefold()
        # Never shortened: locked at the later of the two.
        assert receipt.retain_until >= requested
        assert after.immutability_policy.expiry_time >= default_expiry

    asyncio.run(scenario())


def _make_engine() -> sa.Engine:
    """A minimal SQLite audit engine — copied (not imported) from ne-audit's conftest,
    matching test_s3_worm_live.py's ``_make_engine`` (this file must not import
    another package's conftest)."""
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
    """C10 on Azure: a real signed audit checkpoint lands in real immutable storage.

    NE-AUDIT-CRON's checkpoint loop calls ``ne.audit.checkpoint.write_checkpoint``
    with a resolved ObjectStore. This calls that EXACT primitive against a real
    AzureBlobObjectStore, then proves the checkpoint is genuinely immutable —
    both through the adapter's contract semantics AND via a raw-client
    server-side bypass.
    """
    store, prefix = _make_store()
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

            # Immutable via the adapter's own contract semantics.
            with pytest.raises(WormViolationError):
                await store.put(key, b"tampered")
            with pytest.raises(WormViolationError):
                await store.delete(key)

            # Immutable via a raw-client server-side bypass — the Locked blob
            # cannot be deleted by ANY caller, adapter or not.
            async with _raw_container() as container:
                blob = container.get_blob_client(f"{prefix}{key}")
                with pytest.raises(HttpResponseError) as exc_info:
                    await blob.delete_blob()
                assert exc_info.value.status_code == 409

        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_immutability_is_per_version_and_survives_a_layered_overwrite() -> None:
    """The crux of the P1, settled against the real service.

    Only a real account can answer whether Azure's immutability is
    VERSION-level or BLOB-level, and the whole layered-version fix rests on
    the answer. Azurite implements no immutability at all, so a fake can
    only assert what it was told.

    What is proven HERE:

    1. After ``archive()``, a raw out-of-band ``upload_blob(overwrite=True)``
       SUCCEEDS — Azure does not block the write, it creates a NEW CURRENT
       version. (If it were refused, the defect would be narrower than
       described and this assertion is the thing that would catch that.)
    2. The Locked policy stays on the OLD version and is ABSENT from the new
       current version — i.e. immutability is genuinely per-version.
    3. The adapter nevertheless still sees the key as protected: ``stat()``
       reports the retention, ``put``/``delete`` still raise, and ``get()``
       returns the ARCHIVED bytes rather than the layered payload.
    """
    store, prefix = _make_store()
    retain = _retain_until()

    async def scenario() -> None:
        await store.put("k", b"archived")
        await store.archive("k", retain_until=retain)

        # (1) Layer an unlocked version over the Locked one, out of band.
        async with _raw_container() as container:
            await container.get_blob_client(f"{prefix}k").upload_blob(b"layered", overwrite=True)
            current = await container.get_blob_client(f"{prefix}k").get_blob_properties()

        # (2) The new CURRENT version carries no Locked policy...
        current_mode = getattr(current.immutability_policy, "policy_mode", None)
        locked_mode = BlobImmutabilityPolicyMode.LOCKED.value.casefold()
        assert current_mode is None or str(current_mode).casefold() != locked_mode, (
            f"current version reports policy_mode={current_mode!r}; if Azure really did "
            "carry the Lock forward onto the new version, the layered-version defect would "
            "be narrower than described and this fix over-engineered"
        )
        # ...while a versions listing still shows a Locked one.
        async with _raw_container() as container:
            modes = [
                str(getattr(b.immutability_policy, "policy_mode", "") or "").casefold()
                async for b in container.list_blobs(
                    name_starts_with=f"{prefix}k",
                    include=["versions", "immutabilitypolicy"],
                )
                if b.name == f"{prefix}k"
            ]
        assert locked_mode in modes, f"no Locked version survived the overwrite: {modes}"

        # (3) The adapter's key-level WORM blocking is NOT defeated.
        assert (await store.stat("k")).worm_retain_until is not None
        with pytest.raises(WormViolationError):
            await store.put("k", b"tamper")
        with pytest.raises(WormViolationError):
            await store.delete("k")
        assert await store.get("k") == b"archived", (
            "get() must return the ARCHIVED bytes, not the layered payload"
        )

    asyncio.run(scenario())


def test_container_reports_version_level_immutability_enabled() -> None:
    """``archive()``'s VLW preflight must not false-alarm on a real VLW container.

    The preflight reads ``ContainerProperties.immutable_storage_with_versioning_enabled``
    (deserialized from ``x-ms-immutable-storage-with-versioning-enabled``).
    That the header is actually PRESENT and true on a correctly-provisioned
    container is doc-and-SDK-grounded only until a real account confirms it —
    and if it were absent, the preflight would break every archive() on a
    healthy deployment. That risk is exactly why this test is here.
    """
    store, _prefix = _make_store()

    async def scenario() -> None:
        async with _raw_container() as container:
            props = await container.get_container_properties()
        enabled = getattr(props, "immutable_storage_with_versioning_enabled", None)
        assert enabled, (
            f"container reports immutable_storage_with_versioning_enabled={enabled!r}; "
            "either the test container is not provisioned with VLW, or the service does "
            "not return the header — in which case archive()'s preflight "
            "(VersionLevelImmutabilityNotEnabledError) is a FALSE ALARM and must be "
            "relaxed to warn-and-proceed"
        )
        # And the preflight consequently lets archive() through.
        await store.put("k", b"v")
        await store.archive("k", retain_until=_retain_until())

    asyncio.run(scenario())
