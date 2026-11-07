# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ObjectStore conformance kit — put/get/stat/delete/list + WORM semantics.

Async contract methods are exercised via ``asyncio.run`` (pytest-asyncio is not
a dependency and must not be added).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from ne.contracts.errors import ObjectNotFoundError, WormViolationError
from ne.contracts.storage import ObjectStore


class ObjectStoreContractKit:
    """Subclass and override :meth:`create_store` to prove ObjectStore conformance."""

    #: How far in the future the kit's WORM retention timestamps are set.
    #:
    #: NE-WORM-LIVE (2026-07-19): ADDITIVE, default-preserving hook. The default
    #: is 365 days — byte-identical to the literal it replaces, so every
    #: existing adapter run (memory, S3-against-MinIO) is unchanged. It exists
    #: because a live lane against REAL AWS S3 Object Lock cannot afford
    #: year-scale COMPLIANCE locks: a COMPLIANCE-locked object cannot be
    #: deleted until its retention expires, so its bucket can never be emptied
    #: or deleted either. The owner-gated `worm-live-proof` workflow overrides
    #: this to minutes so teardown can actually succeed. It changes DURATIONS
    #: only, never assertion semantics. Per build-orchestration-plan §2 rule 4
    #: ("changing a kit requires an explicit PR-body note"): this is the note.
    worm_retention: ClassVar[timedelta] = timedelta(days=365)

    def _worm_far(self) -> datetime:
        """A FAR future retention timestamp at WHOLE-SECOND precision.

        NE-S3-OBJECTSTORE (2026-07-18): every retention timestamp the kit builds
        goes through this one helper rather than a bare ``datetime.now(UTC) +
        timedelta(...)``. Reason: verified empirically against MinIO
        RELEASE.2025-09-07T16-13-09Z, S3 Object Lock's
        ``ObjectLockRetainUntilDate`` does not round-trip sub-second precision —
        the server silently truncates to millisecond resolution. A kit timestamp
        with microsecond precision would then fail equality assertions
        (``receipt.retain_until == retain`` / ``stat.worm_retain_until ==
        retain``) against ANY adapter that talks to a real S3-compatible server,
        even though the adapter is behaving correctly. Zeroing microseconds here
        keeps every kit assertion meaningful across BOTH adapter classes:
        in-memory (exact by construction) and S3-compatible (ceils sub-second
        inputs to the next whole second via its own ``_normalize_retain_until`` —
        see packages/ne-storage/src/ne/storage/s3.py). Semantics are UNCHANGED
        (still "now + delta", still strictly in the future); MemoryObjectStore is
        unaffected (it never had a precision issue). Per build-orchestration-plan
        §2 rule 4 ("changing a kit requires an explicit PR-body note"): this is
        the note — flagged prominently in the NE-S3-OBJECTSTORE PR body.
        """
        return (datetime.now(UTC) + self.worm_retention).replace(microsecond=0)

    def _worm_near(self) -> datetime:
        """A NEARER future retention timestamp — strictly earlier than :meth:`_worm_far`.

        Same whole-second truncation rationale as :meth:`_worm_far`. Used by the
        shorten-refusal and extend-allowed cases, which need two distinct future
        timestamps whose ordering holds at ANY configured ``worm_retention``.
        """
        return (datetime.now(UTC) + self.worm_retention / 2).replace(microsecond=0)

    def create_store(self) -> ObjectStore:
        """Return a fresh, empty ObjectStore adapter under test."""
        raise NotImplementedError

    def create_store_at(self, now: Callable[[], datetime]) -> ObjectStore:
        """Return an adapter with an injectable clock, or raise NotImplementedError.

        Adapters that support a deterministic clock override this so the
        WORM-expiry test can advance time; otherwise that test is skipped.
        """
        raise NotImplementedError

    def test_put_get_roundtrip(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.put("a/b", b"hello", content_type="text/plain")
            assert await store.get("a/b") == b"hello"

        asyncio.run(scenario())

    def test_stat_reports_size_content_type_metadata(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.put("k", b"12345", content_type="text/plain", metadata={"team": "x"})
            stat = await store.stat("k")
            assert stat.key == "k"
            assert stat.size == 5
            assert stat.content_type == "text/plain"
            assert stat.metadata["team"] == "x"

        asyncio.run(scenario())

    def test_get_missing_raises_object_not_found(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            with pytest.raises(ObjectNotFoundError):
                await store.get("nope")

        asyncio.run(scenario())

    def test_delete_removes(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.put("k", b"v")
            await store.delete("k")
            with pytest.raises(ObjectNotFoundError):
                await store.get("k")

        asyncio.run(scenario())

    def test_list_objects_prefix_filter(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.put("docs/a", b"1")
            await store.put("docs/b", b"2")
            await store.put("other/c", b"3")
            keys = {s.key for s in await store.list_objects("docs/")}
            assert keys == {"docs/a", "docs/b"}

        asyncio.run(scenario())

    def test_archive_returns_receipt(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.put("k", b"v")
            retain = self._worm_far()
            receipt = await store.archive("k", retain_until=retain)
            assert receipt.key == "k"
            assert receipt.retain_until == retain

        asyncio.run(scenario())

    def test_archived_key_stays_readable_and_listed(self) -> None:
        """An archived key must not disappear from ``get``/``list_objects``.

        NE-S3-OBJECTSTORE (2026-07-19): ADDITIVE case, no existing assertion
        touched. Motivated by a real S3 defect — a versionless ``DeleteObject``
        lays a delete marker that becomes the current version, after which
        ``GetObject``/``ListObjectsV2`` report the key as absent even though the
        COMPLIANCE-locked version is still there and still immutable. An
        archive that the operator can no longer READ is not a usable archive.

        Stated at the contract level (the archived bytes remain retrievable and
        the key remains listed) rather than in S3's version vocabulary, so it
        holds for any adapter: in-memory, S3, and Azure Blob immutability
        policies alike. It asserts only that archiving does not REMOVE
        visibility — it does not constrain versioning, delete markers, or how
        an adapter resolves which bytes are current.

        SCOPE — read before counting this as C10 coverage. This case involves
        NO delete marker and NO layered version, so it passes against the
        PRE-FIX S3 adapter too: it is a floor, not the pin. The contract layer
        cannot express the real defect without importing S3's version
        vocabulary, which would break the kit for `memory` and for Azure Blob
        immutability policies — so it deliberately does not try.

        The actual pins for the C10 hole are LIVE, in
        `packages/ne-storage/tests/test_s3_worm_live.py`:
        `test_delete_marker_does_not_hide_archived_checkpoint`,
        `test_layered_unlocked_version_does_not_disable_worm_checks`, and
        `test_legal_hold_survives_a_delete_marker_in_listing`. Those are the
        tests that went red before the fix and that CI's must-run-not-skip
        WORM gate exists to keep honest. Do not treat this method as
        substituting for any of them.
        """
        store = self.create_store()

        async def scenario() -> None:
            await store.put("arch/k", b"v")
            await store.archive("arch/k", retain_until=self._worm_far())
            assert await store.get("arch/k") == b"v"
            assert {s.key for s in await store.list_objects("arch/")} == {"arch/k"}

        asyncio.run(scenario())

    def test_worm_put_blocked_until_retention(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.put("k", b"v")
            retain = self._worm_far()
            await store.archive("k", retain_until=retain)
            with pytest.raises(WormViolationError):
                await store.put("k", b"v2")

        asyncio.run(scenario())

    def test_worm_delete_blocked_until_retention(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.put("k", b"v")
            retain = self._worm_far()
            await store.archive("k", retain_until=retain)
            with pytest.raises(WormViolationError):
                await store.delete("k")

        asyncio.run(scenario())

    def test_worm_expires(self) -> None:
        clock = {"t": datetime(2026, 1, 1, tzinfo=UTC)}
        try:
            store = self.create_store_at(lambda: clock["t"])
        except NotImplementedError:
            pytest.skip("adapter has no injectable clock for WORM-expiry testing")

        async def scenario() -> None:
            await store.put("k", b"v")
            retain = clock["t"] + timedelta(hours=1)
            await store.archive("k", retain_until=retain)
            with pytest.raises(WormViolationError):
                await store.put("k", b"v2")
            # Advance the clock past the retention window.
            clock["t"] = retain + timedelta(seconds=1)
            await store.put("k", b"v2")
            assert await store.get("k") == b"v2"
            await store.delete("k")
            with pytest.raises(ObjectNotFoundError):
                await store.get("k")

        asyncio.run(scenario())

    def test_worm_retention_cannot_be_shortened(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.put("k", b"v")
            original = self._worm_far()
            await store.archive("k", retain_until=original)
            # Shortening to an earlier (still-future) timestamp is refused...
            with pytest.raises(WormViolationError):
                await store.archive("k", retain_until=self._worm_near())
            # ...as is the past-timestamp exploit that would expire the window now.
            with pytest.raises(WormViolationError):
                await store.archive("k", retain_until=datetime.now(UTC) - timedelta(hours=1))
            # The original window survives the rejected attempts.
            assert (await store.stat("k")).worm_retain_until == original
            # And the key stays locked against mutation.
            with pytest.raises(WormViolationError):
                await store.put("k", b"v2")
            with pytest.raises(WormViolationError):
                await store.delete("k")

        asyncio.run(scenario())

    def test_worm_retention_can_be_extended(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.put("k", b"v")
            await store.archive("k", retain_until=self._worm_near())
            extended = self._worm_far()
            receipt = await store.archive("k", retain_until=extended)
            assert receipt.retain_until == extended
            # Re-archiving at the SAME timestamp is idempotent, not a violation.
            await store.archive("k", retain_until=extended)
            assert (await store.stat("k")).worm_retain_until == extended
            with pytest.raises(WormViolationError):
                await store.put("k", b"v2")

        asyncio.run(scenario())
