# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Below-the-seam unit tests for AzureBlobObjectStore — NO Azure account required.

Two kinds of coverage live here:

1. Boot-time / pure-helper coverage, mirroring test_s3_unit.py: entry-point
   registration, ``from_settings``, the fail-loud + C2/AIR_GAP construction
   checks, retention normalization, key-prefix arithmetic.

2. The full WORM STATE MACHINE, exercised against a FAKE blob client that
   raises real ``azure.core`` exceptions.

Be precise about what (2) proves and what it does not. It proves that THIS
ADAPTER implements the contract's retention rules correctly — extend-only,
equal-is-idempotent-with-no-server-call, earlier-raises, Locked mode, ceiled
expiry, error translation. It proves NOTHING about whether the Azure SERVICE
actually enforces immutability, because a fake returns whatever it is told to.

Unlike the S3 lane (where MinIO gives a real COMPLIANCE-mode server), there is
NO local emulator that can stand in: **Azurite does not implement immutability
policies at all**. So the server-side guarantee is deliberately NOT tested
here — a test that appeared to prove WORM against Azurite would be vacuous,
which is worse than no test. That proof lives in the LIVE-PARKED
``test_azure_blob_worm_live.py``, run by the owner-gated NE-WORM-LIVE lane
against a real storage account.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.storage.blob import BlobImmutabilityPolicyMode
from pydantic import ValidationError

from ne.contracts.errors import ObjectNotFoundError, WormViolationError
from ne.contracts.plugins import PluginRegistry
from ne.net.airgap import AirGapViolationError
from ne.net.ssrf import UnsafeEndpointError
from ne.storage.azure_blob import AzureBlobObjectStore
from ne.storage.config import AzureBlobObjectStoreSettings
from ne.storage.s3 import _join_key, _normalize_retain_until, _strip_key

_AZURITE_URL = "http://localhost:10000/devstoreaccount1"
_PUBLIC_URL = "https://acct.blob.core.windows.net"


def _settings(**over: object) -> AzureBlobObjectStoreSettings:
    base: dict[str, object] = {"account_url": _AZURITE_URL, "container": "ne-test"}
    base.update(over)
    return AzureBlobObjectStoreSettings(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The fake blob client. Deliberately dumb: it records what the adapter asked
# for and returns what it is told. Its ONLY job is to let us assert the
# adapter's own decisions (which calls it makes, with what arguments).
# --------------------------------------------------------------------------


@dataclass
class _FakePolicy:
    expiry_time: datetime | None = None
    policy_mode: str | None = None


@dataclass
class _FakeContentSettings:
    content_type: str | None = None


@dataclass
class _FakeBlob:
    data: bytes = b""
    content_type: str = "application/octet-stream"
    metadata: dict[str, str] = field(default_factory=dict)
    policy_expiry: datetime | None = None
    has_legal_hold: bool = False
    # ``None`` means "whatever a policy-bearing blob defaults to here", i.e.
    # Locked — so every pre-existing test keeps its original meaning. Tests
    # that care about the Unlocked case pass it explicitly.
    policy_mode: str | None = None
    # Opaque per-version id, the shape Azure uses. ``None`` on the CURRENT
    # version of a container whose account has versioning disabled.
    version_id: str | None = None


@dataclass
class _FakeContainerProps:
    immutable_storage_with_versioning_enabled: bool = True


@dataclass
class _FakeProps:
    name: str
    size: int
    content_settings: _FakeContentSettings
    metadata: dict[str, str]
    immutability_policy: _FakePolicy
    has_legal_hold: bool
    version_id: str | None = None
    is_current_version: bool | None = None


def _props_of(name: str, blob: _FakeBlob, *, is_current: bool | None) -> _FakeProps:
    """Deserialize a fake blob version the way the real SDK deserializes one.

    Matches azure-storage-blob 12.30.0's
    ``_deserialize.get_blob_properties_from_generated_code``, which populates
    ``version_id``, ``is_current_version``, ``immutability_policy``,
    ``has_legal_hold``, ``content_settings``, ``size`` and ``metadata`` on
    EVERY listed item when the matching ``include=`` values are passed.
    """
    return _FakeProps(
        name=name,
        size=len(blob.data),
        content_settings=_FakeContentSettings(content_type=blob.content_type),
        metadata=dict(blob.metadata),
        immutability_policy=_FakePolicy(
            expiry_time=blob.policy_expiry,
            policy_mode=(blob.policy_mode or "Locked") if blob.policy_expiry else None,
        ),
        has_legal_hold=blob.has_legal_hold,
        version_id=blob.version_id,
        is_current_version=is_current,
    )


class _FakeDownload:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def readall(self) -> bytes:
        return self._data


class _FakeBlobClient:
    def __init__(
        self, container: _FakeContainerClient, name: str, *, version_id: str | None = None
    ) -> None:
        self._c = container
        self._name = name
        # Mirrors the real BlobClient: a version-scoped client routes
        # get_blob_properties / download_blob / delete_blob /
        # set_immutability_policy to THAT version
        # (``get_version_id(self.version_id, kwargs)`` in the SDK).
        self._version_id = version_id

    def _target(self) -> _FakeBlob | None:
        if self._version_id is None:
            return self._c.blobs.get(self._name)
        for blob in self._c.old_versions.get(self._name, []):
            if blob.version_id == self._version_id:
                return blob
        current = self._c.blobs.get(self._name)
        if current is not None and current.version_id == self._version_id:
            return current
        return None

    async def get_blob_properties(self) -> _FakeProps:
        blob = self._target()
        if blob is None:
            raise ResourceNotFoundError(f"blob {self._name} not found")
        return _props_of(self._name, blob, is_current=blob is self._c.blobs.get(self._name))

    async def upload_blob(
        self,
        data: bytes,
        *,
        overwrite: bool = False,
        content_settings: Any = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        # Azure semantics, now modelled properly: an overwrite creates a NEW
        # CURRENT version and the previous current version is RETAINED as a
        # non-current version, keeping its own immutability policy and legal
        # hold. The new version carries neither (a container-level default VLW
        # policy would stamp it Unlocked, which tests set explicitly). The old
        # fake copied the policy forward and discarded the old version, which
        # made the very defect under repair unrepresentable.
        existing = self._c.blobs.pop(self._name, None)
        if existing is not None:
            self._c.old_versions.setdefault(self._name, []).append(existing)
        self._c.blobs[self._name] = _FakeBlob(
            data=data,
            content_type=getattr(content_settings, "content_type", None)
            or "application/octet-stream",
            metadata=dict(metadata or {}),
            version_id=self._c.next_version_id(),
        )

    async def download_blob(self) -> _FakeDownload:
        blob = self._target()
        if blob is None:
            raise ResourceNotFoundError(f"blob {self._name} not found")
        return _FakeDownload(blob.data)

    async def delete_blob(self) -> None:
        if self._version_id is None:
            # Azure semantics, modelled properly. An UNSCOPED delete on a
            # versioning-enabled container does NOT destroy the blob: it
            # removes the CURRENT version, RETAINING it as a non-current
            # version that keeps its own immutability policy and legal hold.
            # The key then has no current version — the Azure analogue of an
            # S3 delete marker — which is what must keep it out of
            # list_objects while leaving a protected version reachable.
            # Popping the blob outright (what this fake used to do) meant
            # "delete hides the key" was only ever proven by a hand-built
            # fixture, never by the fake's own delete, so a regression in
            # _pick_current's residual-version handling could not be caught.
            existing = self._c.blobs.pop(self._name, None)
            if existing is not None:
                self._c.old_versions.setdefault(self._name, []).append(existing)
            return
        blob = self._target()
        if blob is not None:
            versions = self._c.old_versions.get(self._name, [])
            if blob in versions:
                versions.remove(blob)
            elif self._c.blobs.get(self._name) is blob:
                self._c.blobs.pop(self._name, None)

    async def set_immutability_policy(self, immutability_policy: Any) -> dict[str, str]:
        self._c.policy_calls.append((self._name, immutability_policy, self._version_id))
        if self._c.set_policy_error is not None:
            raise self._c.set_policy_error
        blob = self._target()
        assert blob is not None, f"set_immutability_policy on missing {self._name}"
        blob.policy_expiry = immutability_policy.expiry_time
        # The service records the mode it was asked for; a successful
        # set_immutability_policy(Locked) is what actually makes a blob WORM.
        # NB ``.value``: BlobImmutabilityPolicyMode is a str-Enum whose
        # ``str()`` is "BlobImmutabilityPolicyMode.LOCKED", not "Locked".
        # The wire (and therefore a read-back) carries the VALUE.
        mode = immutability_policy.policy_mode
        blob.policy_mode = getattr(mode, "value", mode)
        return {}


class _FakeContainerClient:
    """A versioned fake container.

    ``blobs`` holds the CURRENT version of each name; ``old_versions`` holds
    the non-current versions, newest last. That split is what lets a test
    express the defect under repair — an unlocked version layered OVER a
    Locked one — which a current-version-only fake could not represent at all.
    """

    def __init__(
        self,
        blobs: dict[str, _FakeBlob] | None = None,
        *,
        old_versions: dict[str, list[_FakeBlob]] | None = None,
        vlw_enabled: bool = True,
    ) -> None:
        self.blobs: dict[str, _FakeBlob] = blobs or {}
        self.old_versions: dict[str, list[_FakeBlob]] = old_versions or {}
        self.policy_calls: list[tuple[str, Any, str | None]] = []
        self.set_policy_error: Exception | None = None
        self.vlw_enabled = vlw_enabled
        # Set to make get_container_properties() fail, the way a credential
        # lacking .../containers/read makes the real call fail.
        self.container_props_error: Exception | None = None
        self._version_seq = 0
        # Names to silently drop right after list_blobs yields them, to
        # simulate a delete racing between LIST and GET-PROPERTIES.
        self.drop_after_list: set[str] = set()
        for name, blob in self.blobs.items():
            if blob.version_id is None:
                blob.version_id = f"{name}#v0"

    def next_version_id(self) -> str:
        self._version_seq += 1
        return f"#new{self._version_seq}"

    async def get_container_properties(self) -> _FakeContainerProps:
        if self.container_props_error is not None:
            raise self.container_props_error
        return _FakeContainerProps(immutable_storage_with_versioning_enabled=self.vlw_enabled)

    def get_blob_client(self, name: str, *, version_id: str | None = None) -> _FakeBlobClient:
        return _FakeBlobClient(self, name, version_id=version_id)

    async def list_blobs(
        self, *, name_starts_with: str = "", include: list[str] | None = None
    ) -> AsyncIterator[Any]:
        # The adapter MUST ask for versions; without 'versions' the real
        # service enumerates current blobs only, which is precisely the
        # defect. Asserting it here keeps a regression from passing silently.
        assert include is not None and "versions" in include, (
            "list_blobs must be called with include=['versions', ...]; without it Azure "
            "enumerates CURRENT blobs only and a layered version hides a protected one"
        )
        names = sorted({*self.blobs, *self.old_versions})
        for name in names:
            if not name.startswith(name_starts_with):
                continue
            for old in self.old_versions.get(name, []):
                yield _props_of(name, old, is_current=False)
            current = self.blobs.get(name)
            if current is not None:
                yield _props_of(name, current, is_current=True)
        for name in self.drop_after_list:
            self.blobs.pop(name, None)


def _store_with_fake(
    fake: _FakeContainerClient,
    *,
    now: Callable[[], datetime] | None = None,
    **over: object,
) -> AzureBlobObjectStore:
    """Build a real AzureBlobObjectStore whose only faked part is the transport.

    ``_container`` is the adapter's single client seam (see azure_blob.py) —
    every Protocol method goes through it, so replacing it swaps the whole
    transport and nothing else.
    """
    kwargs: dict[str, Any] = {}
    if now is not None:
        kwargs["now"] = now
    store = AzureBlobObjectStore(_settings(**over), **kwargs)

    @asynccontextmanager
    async def _fake_container() -> AsyncIterator[Any]:
        yield fake

    store._container = _fake_container  # type: ignore[method-assign]
    return store


class _FakeHttpResponseError(HttpResponseError):
    """A real ``HttpResponseError`` carrying the status/error_code pair the adapter keys on.

    Subclassed rather than attribute-assigned because ``error_code`` is not a
    declared attribute on the SDK's exception type (pyright rejects the
    assignment), while the adapter reads it defensively via ``getattr`` —
    exactly how a real service response populates it.
    """

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"{code} ({status})")
        self.status_code = status
        self.error_code = code


def _http_error(status: int, code: str) -> HttpResponseError:
    return _FakeHttpResponseError(status, code)


# --------------------------------------------------------------------------
# Registration / construction / config
# --------------------------------------------------------------------------


def test_entry_point_resolves_via_plugin_registry() -> None:
    resolved = PluginRegistry().resolve("object_store", "azure_blob")
    assert resolved is AzureBlobObjectStore


def test_from_settings_constructs_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_STORAGE_AZURE_BLOB_ACCOUNT_URL", _AZURITE_URL)
    monkeypatch.setenv("NE_STORAGE_AZURE_BLOB_CONTAINER", "env-container")
    monkeypatch.setenv("NE_STORAGE_AZURE_BLOB_ACCOUNT_KEY", "ne-test-key")

    # A positional settings object of an UNRELATED type is accepted and ignored
    # (the worker builder calls cls.from_settings(platform.audit_settings)).
    store = AzureBlobObjectStore.from_settings(object())

    assert isinstance(store, AzureBlobObjectStore)
    assert store._settings.container == "env-container"


def test_from_settings_present_for_worker_builder() -> None:
    assert hasattr(AzureBlobObjectStore, "from_settings")


def test_missing_container_fails_loud() -> None:
    with pytest.raises(ValueError, match="NE_STORAGE_AZURE_BLOB_CONTAINER"):
        AzureBlobObjectStore(_settings(container=""))


def test_missing_account_url_fails_loud() -> None:
    with pytest.raises(ValueError, match="NE_STORAGE_AZURE_BLOB_ACCOUNT_URL"):
        AzureBlobObjectStore(_settings(account_url=""))


def test_airgap_blocks_public_azure_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(AirGapViolationError):
        AzureBlobObjectStore(_settings(account_url=_PUBLIC_URL))


def test_airgap_allows_public_fqdn_when_allowlisted(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Private-Endpoint posture: Azure Private Endpoints keep the PUBLIC
    # FQDN and resolve it to a private in-VNet address, so allowlisting the
    # exact FQDN is the operator's assertion that this egress stays in-VNet.
    monkeypatch.setenv("AIR_GAP", "1")
    store = AzureBlobObjectStore(
        _settings(account_url=_PUBLIC_URL, allowed_hosts="acct.blob.core.windows.net")
    )
    assert store is not None


def test_airgap_off_allows_public_azure_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIR_GAP", raising=False)
    store = AzureBlobObjectStore(_settings(account_url=_PUBLIC_URL))
    assert store is not None


def test_non_azure_external_host_rejected_without_allowlist() -> None:
    with pytest.raises(UnsafeEndpointError):
        AzureBlobObjectStore(_settings(account_url="http://8.8.8.8:10000"))


def test_loopback_account_url_accepted() -> None:
    # The Azurite-shaped URL: loopback is accepted by validate_in_vpc_endpoint
    # unconditionally. (Azurite is fine for put/get/list — it just cannot
    # prove anything about immutability policies.)
    store = AzureBlobObjectStore(_settings(account_url=_AZURITE_URL))
    assert store is not None


def test_allowed_hosts_accepts_private_dns() -> None:
    store = AzureBlobObjectStore(
        _settings(account_url="http://blob.corp.internal:10000", allowed_hosts="blob.corp.internal")
    )
    assert store is not None


def test_normalization_shared_with_s3() -> None:
    """The Azure adapter uses the S3 lane's normalizer verbatim — same rule.

    The ceiling RATIONALE differs (Azure: RFC1123 whole-second wire format;
    S3: MinIO millisecond truncation) but the RULE is identical and the two
    adapters must never drift: ceil only, so effective retention is
    equal-or-later than requested and NEVER earlier.
    """
    from ne.storage import azure_blob

    # vars() rather than attribute access: the name is module-private, so a
    # direct `azure_blob._normalize_retain_until` trips pyright's
    # reportPrivateImportUsage. Identity is the assertion that matters — the
    # two adapters share ONE normalizer and cannot drift apart.
    assert vars(azure_blob)["_normalize_retain_until"] is _normalize_retain_until

    with pytest.raises(ValueError, match="timezone-aware"):
        _normalize_retain_until(datetime(2027, 1, 1))

    whole = datetime(2027, 1, 1, tzinfo=UTC)
    assert _normalize_retain_until(whole) == whole

    sub = datetime(2027, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
    assert _normalize_retain_until(sub) == datetime(2027, 1, 1, 0, 0, 1, tzinfo=UTC)

    for micros in (1, 1000, 500_000, 999_999):
        dt = datetime(2027, 6, 1, 12, 0, 0, micros, tzinfo=UTC)
        assert _normalize_retain_until(dt) >= dt


def test_key_prefix_round_trip() -> None:
    prefix = "audit/checkpoints/"
    physical = _join_key(prefix, "000001.json")
    assert physical == "audit/checkpoints/000001.json"
    assert _strip_key(prefix, physical) == "000001.json"


def test_settings_env_prefix_and_defaults() -> None:
    s = AzureBlobObjectStoreSettings()
    assert s.model_config.get("env_prefix") == "NE_STORAGE_AZURE_BLOB_"
    assert s.account_url == ""
    assert s.container == ""
    assert s.account_key.get_secret_value() == ""
    assert s.key_prefix == ""
    assert s.allowed_hosts == ""
    assert s.connect_timeout_s == 5.0
    assert s.read_timeout_s == 60.0
    assert s.max_versions_per_key == 1000


def test_settings_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AzureBlobObjectStoreSettings(container="c", not_a_real_field="x")  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# Mocked-transport: the WORM state machine and error translation
# --------------------------------------------------------------------------


def test_put_get_stat_roundtrip_via_fake() -> None:
    fake = _FakeContainerClient()
    store = _store_with_fake(fake)

    async def scenario() -> None:
        stat = await store.put("k", b"hello", content_type="text/plain", metadata={"team": "x"})
        assert stat.key == "k"
        assert stat.size == 5
        assert stat.content_type == "text/plain"
        assert stat.metadata["team"] == "x"
        assert await store.get("k") == b"hello"
        assert (await store.stat("k")).size == 5

    asyncio.run(scenario())


def test_get_missing_raises_object_not_found() -> None:
    store = _store_with_fake(_FakeContainerClient())

    async def scenario() -> None:
        with pytest.raises(ObjectNotFoundError):
            await store.get("nope")

    asyncio.run(scenario())


def test_stat_missing_raises_object_not_found() -> None:
    store = _store_with_fake(_FakeContainerClient())

    async def scenario() -> None:
        with pytest.raises(ObjectNotFoundError):
            await store.stat("nope")

    asyncio.run(scenario())


def test_delete_missing_is_silent() -> None:
    store = _store_with_fake(_FakeContainerClient())

    async def scenario() -> None:
        await store.delete("nope")  # matches MemoryObjectStore's dict.pop(default)

    asyncio.run(scenario())


def test_delete_leaves_a_residual_version_and_key_stays_hidden() -> None:
    """REGRESSION GUARD for ``_pick_current``'s residual-version handling.

    Driven by the fake's OWN ``delete_blob``, not a hand-built fixture. On a
    versioning-enabled container an unscoped delete does not destroy the blob:
    it removes the CURRENT version and RETAINS it as a non-current one, so the
    key still appears in a ``include=['versions', ...]`` listing with no
    current version — the Azure analogue of an S3 delete marker. The adapter
    must read that as "deleted" (``_pick_current`` returning ``None``) rather
    than resurrecting the residual version.

    Until the fake modelled this, "delete → key stays hidden" was provable
    only from a fixture built to look post-delete, so a defect in exactly this
    handling would have gone uncaught.
    """
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"v", version_id="k#v1")})
    store = _store_with_fake(fake)

    async def scenario() -> None:
        assert [s.key for s in await store.list_objects("")] == ["k"]

        await store.delete("k")

        # The version really is still there — the fake models Azure, not a pop.
        assert [b.data for b in fake.old_versions["k"]] == [b"v"]
        assert "k" not in fake.blobs

        # ...and the adapter nonetheless treats the key as gone.
        assert await store.list_objects("") == []
        with pytest.raises(ObjectNotFoundError):
            await store.stat("k")
        with pytest.raises(ObjectNotFoundError):
            await store.get("k")

    asyncio.run(scenario())


def test_put_blocked_while_retention_active() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"v", policy_expiry=now + timedelta(days=1))})
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        with pytest.raises(WormViolationError, match="WORM retention"):
            await store.put("k", b"tampered")
        assert fake.blobs["k"].data == b"v"

    asyncio.run(scenario())


def test_delete_blocked_while_retention_active() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"v", policy_expiry=now + timedelta(days=1))})
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        with pytest.raises(WormViolationError, match="WORM retention"):
            await store.delete("k")
        assert "k" in fake.blobs

    asyncio.run(scenario())


def test_put_and_delete_blocked_under_legal_hold() -> None:
    # The adapter never SETS a legal hold (the frozen Protocol has no method)
    # but it must RESPECT one an operator set out-of-band.
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"v", has_legal_hold=True)})
    store = _store_with_fake(fake)

    async def scenario() -> None:
        with pytest.raises(WormViolationError, match="legal hold"):
            await store.put("k", b"tampered")
        with pytest.raises(WormViolationError, match="legal hold"):
            await store.delete("k")

    asyncio.run(scenario())


def test_legal_hold_alone_keeps_key_listed_and_reports_no_expiry() -> None:
    """A legally-held blob with NO time-based policy stays VISIBLE.

    Regression guard for the round-1 P0 shape (an object the SERVER protects
    made invisible to verification). A legal hold is indefinite and has no
    expiry, so ``worm_retain_until`` is correctly ``None`` — but that ``None``
    must never be read as "unprotected and therefore absent". ``stat()`` and
    ``list_objects()`` both keep reporting the key; only ``put``/``delete``
    are refused (see ``test_put_and_delete_blocked_under_legal_hold``).

    NB the hold surfaces on the LISTING itself — ``include=['legalhold', ...]``
    populates ``has_legal_hold`` on every item — so ``list_objects`` decides
    from the page entries with no per-key property re-read. (Azure needs no
    N+1 fan-out here at all, unlike the S3 lane's ``HeadObject`` walk.)
    """
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"v", has_legal_hold=True)})
    store = _store_with_fake(fake)

    async def scenario() -> None:
        stat = await store.stat("k")
        assert stat.key == "k"
        assert stat.worm_retain_until is None

        listed = await store.list_objects()
        assert [s.key for s in listed] == ["k"]
        assert listed[0].worm_retain_until is None

        # ...and the bytes are still readable, i.e. nothing was tombstoned.
        assert await store.get("k") == b"v"

    asyncio.run(scenario())


def test_put_allowed_after_retention_expiry() -> None:
    expiry = datetime(2026, 1, 2, tzinfo=UTC)
    clock = {"t": datetime(2026, 1, 1, tzinfo=UTC)}
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"v", policy_expiry=expiry)})
    store = _store_with_fake(fake, now=lambda: clock["t"])

    async def scenario() -> None:
        with pytest.raises(WormViolationError):
            await store.put("k", b"v2")
        clock["t"] = expiry + timedelta(seconds=1)
        await store.put("k", b"v2")
        assert await store.get("k") == b"v2"

    asyncio.run(scenario())


def test_archive_missing_key_raises_object_not_found() -> None:
    store = _store_with_fake(_FakeContainerClient())

    async def scenario() -> None:
        with pytest.raises(ObjectNotFoundError):
            await store.archive("nope", retain_until=datetime(2027, 1, 1, tzinfo=UTC))

    asyncio.run(scenario())


def test_archive_sets_locked_policy_with_normalized_expiry() -> None:
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"v")})
    store = _store_with_fake(fake)

    async def scenario() -> None:
        requested = datetime(2027, 1, 1, 0, 0, 0, 654_321, tzinfo=UTC)
        receipt = await store.archive("k", retain_until=requested)

        expected = datetime(2027, 1, 1, 0, 0, 1, tzinfo=UTC)
        assert receipt.retain_until == expected
        assert receipt.retain_until >= requested  # ceiled, never shortened

        assert len(fake.policy_calls) == 1
        name, policy, _version = fake.policy_calls[0]
        assert name == "k"
        assert policy.expiry_time == expected
        assert policy.policy_mode == BlobImmutabilityPolicyMode.LOCKED

    asyncio.run(scenario())


def test_archive_earlier_than_existing_raises_worm_violation() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    existing = now + timedelta(days=365)
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"v", policy_expiry=existing)})
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        with pytest.raises(WormViolationError, match="only be extended"):
            await store.archive("k", retain_until=now + timedelta(days=1))
        # Refused CLIENT-side: the server was never asked to shorten.
        assert fake.policy_calls == []
        assert fake.blobs["k"].policy_expiry == existing

    asyncio.run(scenario())


def test_archive_equal_is_idempotent_no_server_call() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    existing = now + timedelta(days=365)
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"v", policy_expiry=existing)})
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        receipt = await store.archive("k", retain_until=existing)
        assert receipt.retain_until == existing
        assert fake.policy_calls == []  # idempotent — zero server calls

    asyncio.run(scenario())


def test_archive_later_extends() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    existing = now + timedelta(days=1)
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"v", policy_expiry=existing)})
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        extended = now + timedelta(days=365)
        receipt = await store.archive("k", retain_until=extended)
        assert receipt.retain_until == extended
        assert len(fake.policy_calls) == 1
        assert fake.blobs["k"].policy_expiry == extended
        assert (await store.stat("k")).worm_retain_until == extended

    asyncio.run(scenario())


def test_archive_server_denial_translates_to_worm_violation() -> None:
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"v")})
    fake.set_policy_error = _http_error(409, "BlobImmutableDueToPolicy")
    store = _store_with_fake(fake)

    async def scenario() -> None:
        # The defensive TOCTOU path: the adapter's own check passed (no local
        # policy) but the SERVICE refused. Surfaced as the contract's named
        # error, not a raw SDK exception.
        with pytest.raises(WormViolationError, match="lost a race"):
            await store.archive("k", retain_until=datetime(2027, 1, 1, tzinfo=UTC))

    asyncio.run(scenario())


def test_archive_unrelated_server_error_reraised() -> None:
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"v")})
    fake.set_policy_error = _http_error(409, "ContainerNotFound")
    store = _store_with_fake(fake)

    async def scenario() -> None:
        # Not a WORM denial — must NOT be laundered into WormViolationError.
        with pytest.raises(HttpResponseError):
            await store.archive("k", retain_until=datetime(2027, 1, 1, tzinfo=UTC))

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Unlocked vs Locked. Only a ``Locked`` immutability policy is WORM; an
# ``Unlocked`` one is freely deletable and shortenable, so it guarantees
# nothing and must be treated as ABSENT.
#
# This is reachable through this repo's OWN provisioning: the container-level
# default VLW policy in deploy/terraform-azure/modules/storage/main.tf stamps
# every new blob Unlocked, so this is the state the audit checkpoint loop
# actually meets in production, not a hypothetical.
#
# NOTE none of these prove anything about the SERVICE — see the module
# docstring. They prove the ADAPTER's decisions: what it reports and which
# calls it makes. Azurite cannot stand in (it does not implement immutability
# policies at all); the server-side proof is the live-parked lane.
# --------------------------------------------------------------------------


def _unlocked(expiry: datetime) -> _FakeBlob:
    return _FakeBlob(data=b"v", policy_expiry=expiry, policy_mode="Unlocked")


def test_stat_does_not_report_unlocked_policy_as_worm() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fake = _FakeContainerClient({"k": _unlocked(now + timedelta(days=30))})
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        # An unlocked policy is not a retention guarantee, so claiming one
        # would overstate the guarantee to every audit caller downstream.
        assert (await store.stat("k")).worm_retain_until is None

    asyncio.run(scenario())


def test_put_over_unlocked_policy_is_allowed() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fake = _FakeContainerClient({"k": _unlocked(now + timedelta(days=30))})
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        # No spurious WormViolationError: the blob is not actually protected.
        # Blocking here is what breaks the audit checkpoint loop.
        await store.put("k", b"v2")
        assert await store.get("k") == b"v2"

    asyncio.run(scenario())


def test_delete_over_unlocked_policy_is_allowed() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fake = _FakeContainerClient({"k": _unlocked(now + timedelta(days=30))})
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        await store.delete("k")
        assert "k" not in fake.blobs

    asyncio.run(scenario())


def test_archive_over_unlocked_policy_actually_locks() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fake = _FakeContainerClient({"k": _unlocked(now + timedelta(days=30))})
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        # Requested retention is LATER than the unlocked default, so no
        # flooring is involved — this isolates "unlocked must not
        # short-circuit; it must reach the server and Lock".
        requested = now + timedelta(days=365)
        receipt = await store.archive("k", retain_until=requested)

        assert receipt.retain_until == requested
        assert len(fake.policy_calls) == 1
        _, policy, _version = fake.policy_calls[0]
        assert policy.policy_mode == BlobImmutabilityPolicyMode.LOCKED
        assert policy.expiry_time == requested
        # And it is now genuinely WORM, so it reports as such.
        assert fake.blobs["k"].policy_mode == "Locked"
        assert (await store.stat("k")).worm_retain_until == requested

    asyncio.run(scenario())


def test_archive_shorter_than_unlocked_default_still_locks_without_shortening() -> None:
    """Case (b): the exact shape the audit checkpoint loop produces.

    A 30-day container-default Unlocked policy meets a ``retention_days=1``
    archive. The old ``requested < existing`` branch refused outright, so
    ``set_immutability_policy`` was never called and the blob stayed UNLOCKED
    forever. It must instead be LOCKED — and locked at the 30-day expiry, not
    the 1-day one, because silently shortening an audit retention from 30 days
    to 1 is a P0-class defect.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)
    container_default = now + timedelta(days=30)
    fake = _FakeContainerClient({"k": _unlocked(container_default)})
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        requested = now + timedelta(days=1)
        receipt = await store.archive("k", retain_until=requested)

        assert len(fake.policy_calls) == 1, "must reach the server, not short-circuit"
        _, policy, _version = fake.policy_calls[0]
        assert policy.policy_mode == BlobImmutabilityPolicyMode.LOCKED
        # Floored at the retention the blob effectively already had.
        assert policy.expiry_time == container_default
        assert receipt.retain_until == container_default
        assert receipt.retain_until >= requested, "never shortened"
        assert fake.blobs["k"].policy_mode == "Locked"

    asyncio.run(scenario())


def test_archive_over_expired_unlocked_policy_uses_requested_expiry() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    # Already lapsed: it is not a retention the blob still has, so it must
    # NOT float the new expiry.
    fake = _FakeContainerClient({"k": _unlocked(now - timedelta(days=1))})
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        requested = now + timedelta(days=1)
        receipt = await store.archive("k", retain_until=requested)
        assert receipt.retain_until == requested
        _, policy, _version = fake.policy_calls[0]
        assert policy.expiry_time == requested

    asyncio.run(scenario())


def test_archive_equal_to_unlocked_policy_is_not_a_silent_noop() -> None:
    """The narrow C10 shape: exact equality must not return a success receipt
    while leaving the blob unlocked and freely deletable."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = now + timedelta(days=30)
    fake = _FakeContainerClient({"k": _unlocked(expiry)})
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        receipt = await store.archive("k", retain_until=expiry)
        assert receipt.retain_until == expiry
        assert len(fake.policy_calls) == 1, "a success receipt must mean a real Lock"
        assert fake.blobs["k"].policy_mode == "Locked"

    asyncio.run(scenario())


def test_locked_policy_mode_is_matched_case_insensitively() -> None:
    """``policy_mode`` is deserialized from the response header as a bare
    ``str`` with no enum coercion and no case normalization (verified against
    installed azure-storage-blob 12.30.0). A case-sensitive check would read a
    genuinely Locked blob as unlocked — the C10 downgrade direction."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = now + timedelta(days=30)
    fake = _FakeContainerClient(
        {"k": _FakeBlob(data=b"v", policy_expiry=expiry, policy_mode="locked")}
    )
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        assert (await store.stat("k")).worm_retain_until == expiry
        with pytest.raises(WormViolationError):
            await store.delete("k")

    asyncio.run(scenario())


def test_locked_policy_still_blocks_and_reports_unchanged() -> None:
    """Regression guard: the Locked path must behave exactly as before."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = now + timedelta(days=30)
    fake = _FakeContainerClient(
        {"k": _FakeBlob(data=b"v", policy_expiry=expiry, policy_mode="Locked")}
    )
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        assert (await store.stat("k")).worm_retain_until == expiry
        with pytest.raises(WormViolationError, match="under WORM retention"):
            await store.put("k", b"v2")
        with pytest.raises(WormViolationError, match="under WORM retention"):
            await store.delete("k")
        with pytest.raises(WormViolationError, match="only be extended"):
            await store.archive("k", retain_until=now + timedelta(days=1))
        assert fake.policy_calls == []

    asyncio.run(scenario())


def test_list_objects_strips_prefix_and_sorts() -> None:
    fake = _FakeContainerClient(
        {
            "tenant-1/docs/b": _FakeBlob(data=b"2"),
            "tenant-1/docs/a": _FakeBlob(data=b"1"),
            "tenant-1/other/c": _FakeBlob(data=b"3"),
        }
    )
    store = _store_with_fake(fake, key_prefix="tenant-1/")

    async def scenario() -> None:
        stats = await store.list_objects("docs/")
        assert [s.key for s in stats] == ["docs/a", "docs/b"]

    asyncio.run(scenario())


def test_list_objects_has_no_list_then_get_race_window() -> None:
    """A delete racing the listing can no longer perturb list_objects at all.

    This test USED to assert that a key deleted between LIST and the follow-up
    GET-PROPERTIES was skipped. That N+1 window is gone: ``list_objects`` now
    derives size, content-type, metadata, immutability policy and legal hold
    from a SINGLE versions listing (``include=`` carries all of them), so
    there is no second round trip to lose the race in. The blob dropped right
    after the listing yields it is therefore still reported — from the
    snapshot the service already returned, which is the correct and honest
    result for a point-in-time listing.
    """
    fake = _FakeContainerClient({"a": _FakeBlob(data=b"1"), "b": _FakeBlob(data=b"2")})
    fake.drop_after_list = {"b"}
    store = _store_with_fake(fake)

    async def scenario() -> None:
        stats = await store.list_objects("")
        assert [s.key for s in stats] == ["a", "b"]
        assert fake.blobs.keys() == {"a"}  # the drop really did happen

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# THE LAYERED-VERSION DEFECT (P1). Every test in this block is a GENUINE
# REPRO: each one FAILS against the pre-fix adapter, which resolved retention
# from the CURRENT version only (`get_blob_properties()` with no version_id,
# and `list_blobs()` with no include=['versions']).
#
# The scenario throughout is the one the finding describes: a Locked version
# exists, and an UNLOCKED version has been layered over it out-of-band — by a
# raw client, or by any writer that is not this adapter. Pre-fix, the adapter
# read the unlocked current version, saw no policy, and permitted put/delete
# from then on FOR ALL CALLERS while stat() reported no retention at all.
# --------------------------------------------------------------------------


def _layered(expiry: datetime) -> _FakeContainerClient:
    """A key whose Locked version is buried under an unlocked current version."""
    return _FakeContainerClient(
        {"k": _FakeBlob(data=b"layered", version_id="k#v2")},
        old_versions={
            "k": [
                _FakeBlob(
                    data=b"archived",
                    policy_expiry=expiry,
                    policy_mode="Locked",
                    version_id="k#v1",
                )
            ]
        },
    )


def test_put_blocked_by_locked_version_buried_under_layered_current() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = now + timedelta(days=30)
    fake = _layered(expiry)
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        with pytest.raises(WormViolationError, match="under WORM retention"):
            await store.put("k", b"tamper")

    asyncio.run(scenario())
    assert fake.blobs["k"].data == b"layered", "the put must not have gone through"


def test_delete_blocked_by_locked_version_buried_under_layered_current() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fake = _layered(now + timedelta(days=30))
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        with pytest.raises(WormViolationError, match="under WORM retention"):
            await store.delete("k")

    asyncio.run(scenario())
    assert "k" in fake.blobs


def test_stat_reports_retention_from_protected_version_not_current() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = now + timedelta(days=30)
    store = _store_with_fake(_layered(expiry), now=lambda: now)

    async def scenario() -> None:
        st = await store.stat("k")
        # Pre-fix this was None: a key that genuinely has 30 days of
        # server-enforced retention reported as having none.
        assert st.worm_retain_until == expiry
        assert st.size == len(b"archived")

    asyncio.run(scenario())


def test_get_returns_archived_bytes_not_the_layered_payload() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = _store_with_fake(_layered(now + timedelta(days=30)), now=lambda: now)

    async def scenario() -> None:
        # "Archived to WORM" has to imply RECOVERABLE, not merely
        # detectable-as-tampered. Pre-fix this returned b"layered".
        assert await store.get("k") == b"archived"

    asyncio.run(scenario())


def test_list_objects_reports_protected_version_over_layered_current() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = now + timedelta(days=30)
    store = _store_with_fake(_layered(expiry), now=lambda: now)

    async def scenario() -> None:
        stats = await store.list_objects("")
        assert [s.key for s in stats] == ["k"]
        assert stats[0].worm_retain_until == expiry
        assert stats[0].size == len(b"archived")

    asyncio.run(scenario())


def test_list_objects_keeps_key_listed_when_only_a_protected_version_remains() -> None:
    """A deleted current version must not hide a still-Locked version.

    The Azure analogue of the S3 delete-marker hole that closed the round-1
    P0: `list_blobs()` without include=['versions'] enumerates CURRENT blobs
    only, so deleting the current version made an archived audit checkpoint
    vanish from `verify_checkpoints`' listing entirely — a clean green over an
    anchor that is still sitting in WORM.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = now + timedelta(days=30)
    fake = _FakeContainerClient(
        {},
        old_versions={
            "k": [
                _FakeBlob(
                    data=b"archived",
                    policy_expiry=expiry,
                    policy_mode="Locked",
                    version_id="k#v1",
                )
            ]
        },
    )
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        stats = await store.list_objects("")
        assert [s.key for s in stats] == ["k"]
        assert stats[0].worm_retain_until == expiry
        assert await store.get("k") == b"archived"
        assert (await store.stat("k")).worm_retain_until == expiry

    asyncio.run(scenario())


def test_list_objects_hides_a_key_with_no_current_and_no_protection() -> None:
    """The other direction: a legitimately deleted key stays hidden.

    Non-current versions alone must NOT resurrect a key into the listing —
    only PROTECTION does. Mirrors s3.py's delete-marker branch and the
    contract kit's test_deleted_key_absent_from_listing.
    """
    fake = _FakeContainerClient({}, old_versions={"k": [_FakeBlob(data=b"old", version_id="k#v1")]})
    store = _store_with_fake(fake)

    async def scenario() -> None:
        assert await store.list_objects("") == []
        with pytest.raises(ObjectNotFoundError):
            await store.stat("k")

    asyncio.run(scenario())


def test_legal_hold_on_a_buried_version_still_blocks_and_lists() -> None:
    """A hold-only lock on a non-current version is a real, dateless lock."""
    fake = _FakeContainerClient(
        {"k": _FakeBlob(data=b"layered", version_id="k#v2")},
        old_versions={"k": [_FakeBlob(data=b"held", has_legal_hold=True, version_id="k#v1")]},
    )
    store = _store_with_fake(fake)

    async def scenario() -> None:
        with pytest.raises(WormViolationError, match="legal hold"):
            await store.put("k", b"tamper")
        with pytest.raises(WormViolationError, match="legal hold"):
            await store.delete("k")
        stats = await store.list_objects("")
        assert [s.key for s in stats] == ["k"]
        # A hold carries no date — reporting one would be a fabrication.
        assert stats[0].worm_retain_until is None
        assert await store.get("k") == b"held"

    asyncio.run(scenario())


def test_latest_expiry_wins_among_dated_versions() -> None:
    """Among DATED policies an extended retention beats a stale one."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    near, far = now + timedelta(days=1), now + timedelta(days=90)
    fake = _FakeContainerClient(
        {"k": _FakeBlob(data=b"layered", version_id="k#v3")},
        old_versions={
            "k": [
                _FakeBlob(
                    data=b"near", policy_expiry=near, policy_mode="Locked", version_id="k#v1"
                ),
                _FakeBlob(data=b"far", policy_expiry=far, policy_mode="Locked", version_id="k#v2"),
            ]
        },
    )
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        st = await store.stat("k")
        assert st.worm_retain_until == far, "an extended retention must beat a stale one"
        assert await store.get("k") == b"far"

    asyncio.run(scenario())


def _hold_vs_dated_fake(hold_first: bool, now: datetime) -> _FakeContainerClient:
    """One key: a hold-only version and a dated version, in the given order."""
    held = _FakeBlob(data=b"held", has_legal_hold=True, version_id="k#hold")
    dated = _FakeBlob(
        data=b"dated",
        policy_expiry=now + timedelta(days=90),
        policy_mode="Locked",
        version_id="k#dated",
    )
    ordered = [held, dated] if hold_first else [dated, held]
    return _FakeContainerClient(
        {"k": _FakeBlob(data=b"layered", version_id="k#current")},
        old_versions={"k": ordered},
    )


@pytest.mark.parametrize("hold_first", [True, False])
def test_legal_hold_outranks_a_dated_policy(hold_first: bool) -> None:
    """GENUINE REPRO. A legal hold DOMINATES any finite retention.

    The adapter used to rank any active dated policy above a hold-only
    version (``elif ... and best is None`` — the hold was kept only until a
    dated policy turned up). ``put``/``delete`` stayed blocked either way, so
    this was never a WORM hole, but two real consequences remained:

    * ``get()`` returned the DATED version's bytes while a DIFFERENT version
      sat under an indefinite hold. For an audit-checkpoint store whose
      verifier reads bytes through ``get()``, handing back a different
      version's payload than the one actually held is a correctness defect.
    * ``stat()`` reported a finite ``worm_retain_until``, implying the
      protection ENDS on that date, when the held version has no end at all —
      an under-report of the guarantee.

    Parametrized over BOTH listing orders because selection must be
    order-INDEPENDENT: the previous rule was already order-independent and
    that property must survive the reordering.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = _store_with_fake(_hold_vs_dated_fake(hold_first, now), now=lambda: now)

    async def scenario() -> None:
        assert await store.get("k") == b"held", "get() must return the HELD version's bytes"

        st = await store.stat("k")
        # None here means "no dated WORM retention", never "unprotected" —
        # the key staying listed and put/delete raising is the protection.
        assert st.worm_retain_until is None, (
            "a finite expiry would imply protection ends on that date, but the "
            "selected version is held indefinitely"
        )

        listed = await store.list_objects("")
        assert [s.key for s in listed] == ["k"]
        assert listed[0].worm_retain_until is None

        # Blocking is unchanged — it was correct under both rules — but the
        # REASON reported is now the stronger, indefinite one.
        with pytest.raises(WormViolationError, match="legal hold"):
            await store.put("k", b"tamper")
        with pytest.raises(WormViolationError, match="legal hold"):
            await store.delete("k")

    asyncio.run(scenario())


def test_hold_and_date_on_one_version_outranks_date_only_at_same_expiry() -> None:
    """A version carrying BOTH beats a date-only version at the same expiry."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = now + timedelta(days=90)
    fake = _FakeContainerClient(
        {"k": _FakeBlob(data=b"layered", version_id="k#v3")},
        old_versions={
            "k": [
                _FakeBlob(
                    data=b"date-only",
                    policy_expiry=expiry,
                    policy_mode="Locked",
                    version_id="k#v1",
                ),
                _FakeBlob(
                    data=b"held-and-dated",
                    policy_expiry=expiry,
                    policy_mode="Locked",
                    has_legal_hold=True,
                    version_id="k#v2",
                ),
            ]
        },
    )
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        assert await store.get("k") == b"held-and-dated"

    asyncio.run(scenario())


def test_expired_locked_version_no_longer_blocks() -> None:
    """Protection is a live-retention question, not a policy-exists question."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    fake = _FakeContainerClient(
        {"k": _FakeBlob(data=b"current", version_id="k#v2")},
        old_versions={
            "k": [
                _FakeBlob(
                    data=b"old",
                    policy_expiry=now - timedelta(days=1),
                    policy_mode="Locked",
                    version_id="k#v1",
                )
            ]
        },
    )
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        await store.put("k", b"new")
        assert (await store.stat("k")).worm_retain_until is None

    asyncio.run(scenario())


def test_unlocked_policy_on_a_buried_version_is_still_not_worm() -> None:
    """The Locked-only rule survives the version walk.

    An Unlocked policy can be deleted or shortened at will, so it guarantees
    nothing — regardless of WHICH version carries it.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fake = _FakeContainerClient(
        {"k": _FakeBlob(data=b"current", version_id="k#v2")},
        old_versions={
            "k": [
                _FakeBlob(
                    data=b"old",
                    policy_expiry=now + timedelta(days=30),
                    policy_mode="Unlocked",
                    version_id="k#v1",
                )
            ]
        },
    )
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        await store.put("k", b"new")  # must NOT raise
        assert (await store.stat("k")).worm_retain_until is None

    asyncio.run(scenario())


def test_prefix_sharing_key_versions_do_not_leak_into_the_decision() -> None:
    """`name_starts_with` is a PREFIX: listing 'k' also returns 'k2'.

    A Locked version of a DIFFERENT key must never block writes to this one.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fake = _FakeContainerClient(
        {
            "k": _FakeBlob(data=b"free", version_id="k#v1"),
            "k2": _FakeBlob(
                data=b"locked",
                policy_expiry=now + timedelta(days=30),
                policy_mode="Locked",
                version_id="k2#v1",
            ),
        }
    )
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        await store.put("k", b"ok")  # must NOT raise
        with pytest.raises(WormViolationError):
            await store.put("k2", b"nope")

    asyncio.run(scenario())


def test_archive_extension_lands_on_the_protected_version() -> None:
    """An extension must target the ARCHIVED version, never a layered current one.

    Pre-fix, archive() called set_immutability_policy on an UNSCOPED blob
    client — i.e. on whatever version was current — so an extension could
    write a Locked policy onto the attacker's layered payload while leaving
    the real archived version's retention untouched.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = now + timedelta(days=30)
    longer = now + timedelta(days=365)
    fake = _layered(expiry)
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        receipt = await store.archive("k", retain_until=longer)
        assert receipt.retain_until == longer

    asyncio.run(scenario())
    assert len(fake.policy_calls) == 1
    _name, _policy, version_id = fake.policy_calls[0]
    assert version_id == "k#v1", "the extension must land on the Locked version"
    assert fake.old_versions["k"][0].policy_expiry == longer
    assert fake.blobs["k"].policy_expiry is None, "the layered version must be untouched"


def test_archive_shortening_is_refused_from_the_protected_version() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fake = _layered(now + timedelta(days=30))
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        with pytest.raises(WormViolationError, match="only be extended"):
            await store.archive("k", retain_until=now + timedelta(days=1))

    asyncio.run(scenario())
    assert fake.policy_calls == []


def test_archive_of_a_fresh_key_targets_the_current_version() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"x", version_id="k#v1")})
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        await store.archive("k", retain_until=now + timedelta(days=7))

    asyncio.run(scenario())
    assert [c[2] for c in fake.policy_calls] == ["k#v1"]


# --------------------------------------------------------------------------
# Version-walk cap — regression guards (these do NOT repro the P1; they guard
# the DoS / truncation hazard the fix introduces along with the version walk).
# --------------------------------------------------------------------------


def _many_versions(count: int) -> _FakeContainerClient:
    return _FakeContainerClient(
        {"k": _FakeBlob(data=b"cur", version_id="k#cur")},
        old_versions={"k": [_FakeBlob(data=b"v", version_id=f"k#v{i}") for i in range(count)]},
    )


def test_version_cap_breach_fails_loud_rather_than_truncating() -> None:
    from ne.storage.azure_blob import TooManyBlobVersionsError

    store = _store_with_fake(_many_versions(12), max_versions_per_key=10)

    async def scenario() -> None:
        # Every read/write path walks versions, so every one must fail LOUD.
        # Truncating instead could miss the Locked version and report an
        # archived key as unprotected — a silent WORM downgrade.
        for call in (
            store.stat("k"),
            store.get("k"),
            store.put("k", b"x"),
            store.delete("k"),
            store.list_objects(""),
            store.archive("k", retain_until=datetime(2030, 1, 1, tzinfo=UTC)),
        ):
            with pytest.raises(TooManyBlobVersionsError, match="more than 10 blob versions"):
                await call

    asyncio.run(scenario())


def test_version_cap_message_names_the_logical_key_and_the_env_knob() -> None:
    from ne.storage.azure_blob import TooManyBlobVersionsError

    store = _store_with_fake(
        _FakeContainerClient(
            {"pre/k": _FakeBlob(data=b"c", version_id="c")},
            old_versions={"pre/k": [_FakeBlob(data=b"v", version_id=f"v{i}") for i in range(5)]},
        ),
        key_prefix="pre/",
        max_versions_per_key=2,
    )

    async def scenario() -> None:
        with pytest.raises(TooManyBlobVersionsError) as exc:
            await store.stat("k")
        assert "key 'k'" in str(exc.value)  # logical, not physical
        assert "NE_STORAGE_AZURE_BLOB_MAX_VERSIONS_PER_KEY" in str(exc.value)

    asyncio.run(scenario())


def test_version_count_at_the_cap_is_accepted() -> None:
    store = _store_with_fake(_many_versions(9), max_versions_per_key=10)

    async def scenario() -> None:
        assert (await store.stat("k")).size == len(b"cur")

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# VLW preflight — regression guards.
# --------------------------------------------------------------------------


def test_archive_without_vlw_fails_loud_and_typed() -> None:
    from ne.storage.azure_blob import VersionLevelImmutabilityNotEnabledError

    fake = _FakeContainerClient({"k": _FakeBlob(data=b"x", version_id="v1")}, vlw_enabled=False)
    store = _store_with_fake(fake)

    async def scenario() -> None:
        with pytest.raises(VersionLevelImmutabilityNotEnabledError) as exc:
            await store.archive("k", retain_until=datetime(2030, 1, 1, tzinfo=UTC))
        assert "version-level immutability" in str(exc.value)
        assert "land UNLOCKED" in str(exc.value)

    asyncio.run(scenario())
    assert fake.policy_calls == [], "must never reach the server"


@pytest.mark.parametrize("status", [401, 403])
def test_archive_translates_a_container_properties_authz_failure(status: int) -> None:
    """A PERMISSIONS fault on the preflight is typed, and names the action.

    ``archive()`` gained ``get_container_properties()`` as a precondition it
    did not have before the VLW fix, so a credential that can write blobs but
    cannot read container properties now fails on a call it never used to
    make — and would surface a bare ``HttpResponseError``. The S3 lane's
    equivalent gap blocked that PR twice, so this one is typed and names the
    required RBAC action rather than leaving the operator to decode a 403.
    """
    from ne.storage.azure_blob import ContainerPropertiesPermissionError

    fake = _FakeContainerClient({"k": _FakeBlob(data=b"x", version_id="v1")})
    fake.container_props_error = _http_error(status, "AuthorizationPermissionMismatch")
    store = _store_with_fake(fake)

    async def scenario() -> None:
        with pytest.raises(ContainerPropertiesPermissionError) as exc:
            await store.archive("k", retain_until=datetime(2030, 1, 1, tzinfo=UTC))
        assert "containers/read" in str(exc.value)
        assert "land UNLOCKED" in str(exc.value)

    asyncio.run(scenario())
    assert fake.policy_calls == [], "must never reach the server"


def test_archive_does_not_swallow_a_non_authz_container_error() -> None:
    """Only 401/403 translate; anything else must propagate unchanged."""
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"x", version_id="v1")})
    fake.container_props_error = _http_error(503, "ServerBusy")
    store = _store_with_fake(fake)

    async def scenario() -> None:
        with pytest.raises(HttpResponseError) as exc:
            await store.archive("k", retain_until=datetime(2030, 1, 1, tzinfo=UTC))
        assert exc.value.status_code == 503

    asyncio.run(scenario())


def test_vlw_preflight_does_not_gate_the_read_paths() -> None:
    """Reads must keep working on a non-VLW container — only archive() fails.

    Without versioning there are no versions: every key has a single
    version_id=None entry, which _pick_current treats as the blob itself.
    Degrading gracefully here matters because a misprovisioned container must
    not take out the whole store, only the WORM claim.
    """
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"x")}, vlw_enabled=False)
    fake.blobs["k"].version_id = None
    store = _store_with_fake(fake)

    async def scenario() -> None:
        assert (await store.stat("k")).size == 1
        assert await store.get("k") == b"x"
        assert [s.key for s in await store.list_objects("")] == ["k"]
        await store.put("k", b"yy")
        await store.delete("k")

    asyncio.run(scenario())


def test_vlw_preflight_is_cached_per_instance() -> None:
    fake = _FakeContainerClient({"k": _FakeBlob(data=b"x", version_id="v1")})
    calls = 0
    original = fake.get_container_properties

    async def counting() -> Any:
        nonlocal calls
        calls += 1
        return await original()

    fake.get_container_properties = counting  # type: ignore[method-assign]
    store = _store_with_fake(fake)

    async def scenario() -> None:
        await store.archive("k", retain_until=datetime(2030, 1, 1, tzinfo=UTC))
        await store.archive("k", retain_until=datetime(2031, 1, 1, tzinfo=UTC))

    asyncio.run(scenario())
    assert calls == 1, "VLW is a provisioning-time property; one check per instance"


# --------------------------------------------------------------------------
# account_key as SecretStr — regression guards for the P2.
# --------------------------------------------------------------------------


def test_account_key_is_redacted_in_settings_repr() -> None:
    s = _settings(account_key="super-secret-shared-key")
    assert "super-secret-shared-key" not in repr(s)
    assert "super-secret-shared-key" not in str(s)
    assert "super-secret-shared-key" not in repr(s.account_key)
    assert s.account_key.get_secret_value() == "super-secret-shared-key"


def test_empty_account_key_falls_through_to_default_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SecretStr("") must NOT be handed to the SDK as an empty credential.

    SecretStr defines no __bool__, so `if settings.account_key:` is True even
    when empty. Unwrapping BEFORE the truthiness test is what keeps the
    recommended DefaultAzureCredential production posture working.
    """
    seen: list[Any] = []

    class _FakeCredential:
        async def close(self) -> None:
            return None

    def _fake_container_client(**kwargs: Any) -> Any:
        seen.append(kwargs["credential"])

        class _Ctx:
            async def __aenter__(self) -> Any:
                return object()

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Ctx()

    monkeypatch.setattr("ne.storage.azure_blob.ContainerClient", _fake_container_client)
    monkeypatch.setattr("ne.storage.azure_blob.DefaultAzureCredential", _FakeCredential)

    async def scenario() -> None:
        async with AzureBlobObjectStore(_settings())._container():
            pass
        async with AzureBlobObjectStore(_settings(account_key="k1"))._container():
            pass

    asyncio.run(scenario())
    assert isinstance(seen[0], _FakeCredential), "empty key must use DefaultAzureCredential"
    assert seen[1] == "k1", "a set key must be passed UNWRAPPED, not as a SecretStr"


@pytest.mark.parametrize("reverse", [False, True])
def test_equal_rank_versions_resolve_deterministically(reverse: bool) -> None:
    """GENUINE REPRO (Codex). Equal protection does NOT imply equal payload.

    The rank tuple was ``(held, expiry)`` — a total PREORDER, not a total
    order. Two versions with identical ``(held, expiry)`` compared equal, and
    the strict ``>`` then kept whichever the listing yielded FIRST. So two
    hold-only versions (or two held versions sharing an expiry) made
    ``get()`` return DIFFERENT BYTES for the same key depending only on
    listing order — a key that does not resolve to the same bytes every time
    is worthless as an audit anchor.

    ``version_id`` is now the final tiebreak, making the ordering total.
    Azure version IDs are timestamp-based, so ``max`` means newest-among-
    equals; both orderings below must agree on that same version.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)
    older = _FakeBlob(data=b"older-held", has_legal_hold=True, version_id="k#2026-01-01T00:00:00")
    newer = _FakeBlob(data=b"newer-held", has_legal_hold=True, version_id="k#2026-01-02T00:00:00")
    ordered = [newer, older] if reverse else [older, newer]
    fake = _FakeContainerClient(
        {"k": _FakeBlob(data=b"layered", version_id="k#current")},
        old_versions={"k": ordered},
    )
    store = _store_with_fake(fake, now=lambda: now)

    async def scenario() -> None:
        # The SAME version regardless of listing order — that is the property.
        assert await store.get("k") == b"newer-held"

    asyncio.run(scenario())
