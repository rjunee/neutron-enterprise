# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AzureBlobObjectStore — the AZURE production :class:`~ne.contracts.storage.ObjectStore`.

Speaks Azure Blob Storage via the official ``azure-storage-blob`` ASYNC client
(``azure.storage.blob.aio``) — genuinely async, matching the contract's async
Protocol, for the same reason ``aiobotocore`` was chosen over ``boto3`` on the
S3 lane. ``archive()`` is backed by a real Azure **version-level immutability
policy** in ``Locked`` mode.

Azure Blob is NOT S3-compatible, so ``S3ObjectStore`` cannot serve an Azure
deployment at all — this adapter is what makes SPEC C9 ("deploy anywhere",
AWS and Azure as co-equal first-class targets) true rather than aspirational.

WORM honesty — Azure's truth, which is NOT S3's truth
-----------------------------------------------------
Read this before trusting the guarantee (see also packages/ne-storage/README.md
"Azure Blob adapter"):

* **SERVER-enforced.** Once a version-level immutability policy with
  ``policy_mode=Locked`` is set on a blob version, the SERVICE refuses to
  delete that version and refuses to SHORTEN its ``expiry_time`` — only
  extension is accepted — for ANY caller, this adapter or a raw client. That
  is Azure's documented contract. **It is empirically provable ONLY against a
  real storage account**: Azurite, the Azure Storage emulator, does NOT
  implement immutability policies at all. There is therefore deliberately no
  emulator-backed WORM test in this repo's CI — a test that appeared to prove
  WORM against Azurite would be vacuous, which is worse than no test. The
  live proof is ``tests/test_azure_blob_worm_live.py`` run by the owner-gated
  NE-WORM-LIVE lane against a real account, NOT this repo's CI.

* **Only ``Locked`` counts.** Azure immutability policies have a
  ``policy_mode``, and only ``Locked`` is WORM: an ``Unlocked`` policy can be
  deleted or SHORTENED at will, so it guarantees nothing. This adapter treats
  an Unlocked policy as ABSENT everywhere — ``stat()`` does not report it as
  ``worm_retain_until``, and ``put``/``delete`` are not blocked by it. This is
  not hypothetical: a container-level default VLW policy (which
  ``deploy/terraform-azure/modules/storage/main.tf`` provisions) stamps new
  blobs in **Unlocked** mode, so every blob the audit checkpoint loop writes
  arrives unlocked. ``archive()`` consequently does NOT short-circuit on an
  unlocked policy — it proceeds and actually Locks the blob — but it locks at
  the LATER of the requested expiry and any retention already in force, so
  locking can never shorten what the blob effectively had.

* **PER-VERSION, not per-blob — every decision is keyed on the PROTECTED
  version.** Azure's immutability is version-level (VLW): the policy and the
  legal-hold flag live on a specific blob VERSION, never on the key. Verified
  by introspection of the installed azure-storage-blob 12.30.0 —
  ``get_blob_properties``, ``download_blob``, ``delete_blob`` and
  ``set_immutability_policy`` all take a ``version_id`` keyword
  (``get_version_id(self.version_id, kwargs)``), and ``BlobProperties``
  carries ``version_id`` / ``is_current_version`` / ``immutability_policy`` /
  ``has_legal_hold`` per item. So this adapter resolves
  :meth:`AzureBlobObjectStore._protected_version` on EVERY path rather than
  reading whatever version happens to be CURRENT. Reading the current version
  was a real defect: one unlocked version layered over a Locked one — by a raw
  client, or by any writer that is not this adapter — made ``_policy_expiry``
  see no policy, so ``put``/``delete`` were permitted from then on for ALL
  callers and ``stat()`` reported no retention for a key that genuinely had
  one. The archived bytes stayed server-protected throughout, but the
  adapter's key-level WORM blocking was permanently off. This is the exact
  Azure analogue of the S3 ``_locked_version`` fix.

* **CLIENT-side courtesy, with a TOCTOU window.** The frozen contract's
  KEY-level ``put``/``delete`` blocking (raising ``WormViolationError`` while
  a key is archived) is implemented client-side: a versions read followed by
  the mutating call, exactly the shape s3.py uses. There is a window between
  the two. This does NOT weaken the actual WORM guarantee: with versioning
  enabled an overwrite creates a NEW current version while the Locked version
  stays immutable, so the archived bytes are untouched. The window can only
  affect whether a racing caller SEES the contract's courtesy error — never
  the archived data.

* **Precision.** The REST wire format for the immutability expiry
  (``x-ms-immutability-policy-until-date``) is RFC 1123, which carries
  WHOLE-SECOND precision only. A sub-second ``retain_until`` is therefore
  CEILED to the next whole second before it is ever sent
  (:func:`ne.storage.s3._normalize_retain_until`, shared with the S3 lane —
  the ceiling direction is what matters, and it is the same rule for a
  different underlying reason). Ceiling can only ever make the effective
  retention EQUAL-OR-LATER than requested, never earlier. A silent shortening
  of an audit retention is a P0 defect.

* **Provisioning prerequisite, checked LOUD.** The target container must have
  blob versioning AND version-level immutability (VLW) enabled. That is a
  provisioning-time property of the account/container and is NOT settable
  from the data plane — this adapter cannot turn it on, only read it.
  ``deploy/terraform-azure/modules/storage/main.tf`` provisions a dedicated
  VLW WORM container (``azapi_resource.worm``) for this: ``hashicorp/azurerm``
  exposes NO argument to enable VLW at any level (upstream
  ``hashicorp/terraform-provider-azurerm#21512`` / ``#31438``, both open), so
  the container is created via the ``azapi`` provider instead. The account's
  ``versioning_enabled = true`` plus the OPTIONAL time-based
  ``azurerm_storage_container_immutability_policy`` on the (separate) velero
  container do NOT satisfy this preflight — that is a DIFFERENT Azure
  feature. Asserted plan-time by
  ``deploy/terraform-azure/tests/storage_worm.tftest.hcl``.

  ``archive()`` preflights ``get_container_properties()`` and raises
  :class:`VersionLevelImmutabilityNotEnabledError` when
  ``immutable_storage_with_versioning_enabled`` is not true, rather than
  letting an untyped ``HttpResponseError`` surface from
  ``set_immutability_policy``. This mirrors ``S3ObjectStore``'s
  ``_ensure_object_lock_enabled``, which was itself a review finding: without
  it the operator sees a raw service error on the exact path C10 depends on.
  Verified by introspection: ``ContainerProperties`` exposes
  ``immutable_storage_with_versioning_enabled``, deserialized from the
  ``x-ms-immutable-storage-with-versioning-enabled`` response header. Note
  VLW *implies* versioning, so the one check covers both preconditions; if
  versioning is off, no versions exist and every key has exactly one
  ``version_id=None`` entry, which the read paths handle without crashing —
  only ``archive()`` fails, loudly.

* **Legal hold, and it DOMINATES a date.** This adapter never SETS a legal
  hold (the frozen Protocol has no method for it; setting one is an operator
  action via Azure tooling) but it does RESPECT one: ``put``/``delete``
  against a blob carrying ``has_legal_hold`` raise ``WormViolationError``.
  When several versions are protected, :meth:`AzureBlobObjectStore._pick_protected`
  ranks a HELD version above any dated one, because a hold is indefinite and
  outlasts every finite expiry. This is not merely cosmetic: ``get()`` returns
  the SELECTED version's bytes and ``stat()`` reports its expiry, so ranking a
  date first handed back the wrong archived payload and advertised an END DATE
  for a key that is actually held without one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob import BlobImmutabilityPolicyMode, ContentSettings, ImmutabilityPolicy
from azure.storage.blob.aio import ContainerClient

from ne.contracts.errors import ObjectNotFoundError, WormViolationError
from ne.contracts.storage import ArchiveReceipt, ObjectStat
from ne.net.airgap import AirGapViolationError, air_gap_engaged
from ne.net.ssrf import normalize_host, validate_in_vpc_endpoint

# Shared with the S3 lane on purpose: these are pure, adapter-agnostic helpers
# and the two WORM adapters MUST agree byte-for-byte on retention
# normalization and key-prefix arithmetic. Note the ceiling RATIONALE differs
# (S3: MinIO/S3 truncates ObjectLockRetainUntilDate to milliseconds; Azure:
# the RFC1123 wire format for x-ms-immutability-policy-until-date carries
# whole seconds only) — but the RULE, "ceil, never truncate, so effective
# retention is equal-or-later and never earlier", is identical and must stay
# that way. test_s3_unit.py already imports these same underscore names.
from ne.storage.config import AzureBlobObjectStoreSettings
from ne.storage.s3 import _is_retention_active, _join_key, _normalize_retain_until, _strip_key

_PUBLIC_AZURE_SUFFIXES = (
    ".blob.core.windows.net",  # Azure public cloud
    ".blob.core.usgovcloudapi.net",  # Azure Government
    ".blob.core.chinacloudapi.cn",  # Azure China (21Vianet)
)
"""Public Azure Blob service FQDN suffixes — public-internet egress."""

_WORM_DENIAL_CODES = {"BlobImmutableDueToPolicy", "ImmutabilityPolicyDeleteOnLockedPolicy"}
"""Azure Blob error codes meaning "refused because of an immutability policy".

From Azure's documented Blob Storage error-code table. The defensive TOCTOU
translation path that keys on these is doc-grounded only until the owner-gated
NE-WORM-LIVE lane confirms it empirically against a real account.
"""


_AUTHZ_STATUS_CODES = {401, 403}
"""HTTP statuses meaning "the credential is not permitted to do that".

Keyed on the STATUS rather than an Azure error-code string on purpose: the
service returns ``AuthorizationPermissionMismatch``,
``AuthorizationFailure``, ``InvalidAuthenticationInfo`` and others depending on
the credential type, and missing one of those strings would silently drop the
typed translation back to a raw ``HttpResponseError``.
"""


_VERSION_INCLUDE = ["versions", "immutabilitypolicy", "legalhold", "metadata"]
"""``include=`` for every listing this adapter makes.

Introspected against azure-storage-blob 12.30.0, not taken from docs:
``ListBlobsIncludeItem`` carries ``VERSIONS='versions'``,
``IMMUTABILITYPOLICY='immutabilitypolicy'``, ``LEGALHOLD='legalhold'`` and
``METADATA='metadata'``, and ``_deserialize.get_blob_properties_from_generated_code``
populates ``version_id``, ``is_current_version``, ``immutability_policy``,
``has_legal_hold``, ``content_settings``, ``size`` and ``metadata`` on EVERY
listed item. So one paged LIST carries everything a retention decision needs —
Azure needs no per-version property round trip at all, unlike the S3 lane's
N+1 ``HeadObject`` fan-out. Without ``'versions'`` the listing enumerates
CURRENT blobs only, which is what let a layered version hide a protected one.
"""


class TooManyBlobVersionsError(RuntimeError):
    """A single key carries more versions than the adapter will walk.

    The Azure analogue of :class:`ne.storage.s3.TooManyObjectVersionsError`,
    with a deliberately different cost story. Azure returns the immutability
    policy INLINE on the versions listing, so resolving the protected version
    costs no extra round trip per version — the S3 "~2ms per HeadObject"
    argument does NOT apply here. What does apply is identical: the listing
    itself is unbounded, any principal holding blob-write permission can layer
    arbitrarily many versions onto one checkpoint key, and an unbounded walk
    makes ``list_objects`` (and therefore ``verify_checkpoints``) stop
    completing — the tamper anchor becomes UNVERIFIABLE rather than falsified,
    silently.

    Raising — never truncating — is the requirement, and it is the stronger
    half of the reasoning: a truncated walk could skip the version carrying
    the Locked policy and report an archived key as unprotected, which is a
    silent C10 WORM downgrade rather than a loud availability fault.
    """


class VersionLevelImmutabilityNotEnabledError(RuntimeError):
    """The container does not have version-level immutability (VLW) enabled.

    A PERMANENT provisioning fault on the exact path C10 depends on, and not
    settable from the data plane: VLW is enabled at container creation (or
    inherited from an account-level default) and this adapter cannot turn it
    on. Raised from ``archive()``'s preflight instead of letting the raw
    ``HttpResponseError`` from ``set_immutability_policy`` surface, mirroring
    ``S3ObjectStore._ensure_object_lock_enabled`` — which existed for exactly
    this reason, having itself been a review finding.
    """


class ContainerPropertiesPermissionError(RuntimeError):
    """The credential may not READ the container's properties.

    A PERMISSIONS fault, distinct from
    :class:`VersionLevelImmutabilityNotEnabledError` (a container that
    genuinely has no VLW). Both are permanent configuration faults on the
    exact path C10 depends on, and both must fail LOUD and TYPED rather than
    as a raw ``azure.core.exceptions.HttpResponseError``.

    The Azure twin of :class:`ne.storage.s3.ObjectLockPermissionError`, and it
    exists because the S3 lane's equivalent gap blocked that PR TWICE:
    ``write_checkpoint`` does ``put`` THEN ``archive``, so a credential that
    can write but cannot read the container's properties leaves every
    checkpoint object in the container UNLOCKED while the loop dies on an
    untyped service error — a C10 downgrade on the documented happy path.

    ``archive()``'s preflight (:meth:`AzureBlobObjectStore._ensure_vlw_enabled`)
    calls ``get_container_properties()``, i.e. the **Get Container Properties**
    REST operation, which is authorized by the RBAC *control*-plane action
    ``Microsoft.Storage/storageAccounts/blobServices/containers/read`` — NOT by
    a ``.../containers/blobs/*`` data action. The documented minimal role
    (**Storage Blob Data Contributor**, see ``docs/DEPLOYMENT.md`` §6.6 and
    ``packages/ne-storage/README.md``) DOES carry that action in its ``Actions``
    list alongside ``containers/write`` and ``containers/delete``, so the
    documented posture is sufficient and this is NOT a repeat of the S3 IAM
    defect. It is nonetheless typed, because the blob-only roles an operator
    might reasonably reach for instead (a custom role scoped to
    ``containers/blobs/*`` alone) grant every other call this adapter makes and
    fail on exactly this one.
    """


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _is_locked_mode(mode: object) -> bool:
    """True iff an immutability policy's ``policy_mode`` is ``Locked``.

    Compared CASE-INSENSITIVELY on purpose. Verified against the INSTALLED
    azure-storage-blob 12.30.0 by introspection, not from docs:

    * ``BlobImmutabilityPolicyMode`` is a ``str``-Enum whose members are
      ``UNLOCKED='Unlocked'``, ``LOCKED='Locked'``, ``MUTABLE='Mutable'`` —
      TitleCase values.
    * But ``ImmutabilityPolicy.policy_mode`` is typed ``Optional[str]`` and is
      populated straight from the ``x-ms-immutability-policy-mode`` RESPONSE
      HEADER, deserialized with the bare type ``"str"`` — the generated client
      does NOT coerce it into the enum and does NOT normalize its case. So the
      value on a read-back is whatever casing the SERVICE put on the wire.

    Because the enum subclasses ``str``, ``BlobImmutabilityPolicyMode.LOCKED
    == "locked"`` is ``False``. A case-SENSITIVE check would therefore read a
    genuinely Locked blob as unlocked if the service ever answers lowercase —
    under-reporting ``worm_retain_until`` from ``stat()`` and skipping the
    client-side ``put``/``delete`` courtesy block. That is the C10 silent-
    downgrade direction, so the comparison is deliberately case-insensitive.
    """
    return isinstance(mode, str) and mode.strip().casefold() == _LOCKED_MODE.casefold()


_LOCKED_MODE: str = BlobImmutabilityPolicyMode.LOCKED.value
"""``'Locked'`` — read off the installed SDK enum rather than hardcoded."""


def _is_worm_denial(exc: HttpResponseError) -> bool:
    """True if the service rejected a mutation because of an immutability policy.

    Defensive: the normal ``archive()`` flow already compares the existing
    retention itself and raises ``WormViolationError`` BEFORE calling the
    service in the shorten case, so this only fires on a genuine TOCTOU race
    against a concurrent ``archive()``.
    """
    return exc.status_code == 409 and str(getattr(exc, "error_code", "")) in _WORM_DENIAL_CODES


class AzureBlobObjectStore:
    """Production Azure Blob :class:`~ne.contracts.storage.ObjectStore` adapter.

    No network I/O happens in ``__init__`` (mirroring ``S3ObjectStore`` and
    ``OpenBaoSecretStore``) — a fresh ``ContainerClient`` is opened for the
    duration of each Protocol method call. The clock is injectable
    (``now=lambda: ...``) for deterministic client-side retention comparisons,
    matching ``MemoryObjectStore``.
    """

    def __init__(
        self,
        settings: AzureBlobObjectStoreSettings | None = None,
        *,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings if settings is not None else AzureBlobObjectStoreSettings()
        self._now = now
        self._vlw_verified = False

        if not self._settings.container:
            raise ValueError(
                "NE_STORAGE_AZURE_BLOB_CONTAINER is not set; the 'azure_blob' ObjectStore "
                "adapter requires an explicit container (fail loud at boot)"
            )
        if not self._settings.account_url:
            raise ValueError(
                "NE_STORAGE_AZURE_BLOB_ACCOUNT_URL is not set; Azure has no account-less "
                "default endpoint, so the 'azure_blob' adapter requires an explicit "
                "account URL (fail loud at boot)"
            )

        self._validate_endpoint()

    def _allowed_hosts(self) -> list[str]:
        return [h.strip() for h in self._settings.allowed_hosts.split(",") if h.strip()]

    def _validate_endpoint(self) -> None:
        """C2 governed-egress check for ``account_url`` — no I/O, boot-time only.

        A PUBLIC Azure FQDN is ordinary governed egress (the deployment's
        network firewall holds the FQDN allowlist), so it is accepted while
        AIR_GAP is off and FAILS CLOSED under AIR_GAP — unless the operator
        explicitly allowlists that exact FQDN, which is how an Azure Private
        Endpoint is expressed (Private Endpoints keep the public FQDN and
        resolve it to a private in-VNet address, so the URL alone cannot tell
        the two apart). Anything else — a custom domain, Azurite — goes
        through the shared in-VPC validator exactly as ``S3ObjectStore``'s
        ``endpoint_url`` does.
        """
        host = normalize_host(urlsplit(self._settings.account_url).hostname or "")
        allowed = self._allowed_hosts()

        if host.endswith(_PUBLIC_AZURE_SUFFIXES):
            if air_gap_engaged() and host not in {normalize_host(h) for h in allowed}:
                raise AirGapViolationError(
                    f"NE_STORAGE_AZURE_BLOB_ACCOUNT_URL host {host!r} is a PUBLIC Azure Blob "
                    "endpoint while AIR_GAP is engaged; add it to "
                    "NE_STORAGE_AZURE_BLOB_ALLOWED_HOSTS to assert an Azure Private Endpoint "
                    "resolves it privately in-VNet, or disable AIR_GAP"
                )
            return

        validate_in_vpc_endpoint(self._settings.account_url, allowed_hosts=allowed)

    @classmethod
    def from_settings(cls, settings: object | None = None) -> AzureBlobObjectStore:
        """Construct purely from ``NE_STORAGE_AZURE_BLOB_*`` env — the worker builder's seam.

        Accepts (and IGNORES) a positional settings object of ANY type. The
        NE-AUDIT-CRON checkpoint-loop builder resolves an ``ObjectStore``
        plugin and calls ``cls.from_settings(platform.audit_settings)``
        uniformly across whichever adapter is configured — but
        ``AuditSettings`` carries no notion of an Azure account URL /
        container / credential; those live in this adapter's OWN env-driven
        ``AzureBlobObjectStoreSettings``. Same seam, same resolution, as
        ``S3ObjectStore.from_settings`` and the Cedar PDP precedent.
        """
        return cls()

    @asynccontextmanager
    async def _container(self) -> AsyncIterator[Any]:
        """Open a ``ContainerClient`` for the duration of one Protocol call.

        This is also the single seam the unit tests monkeypatch to substitute
        a fake blob client (Azurite cannot prove WORM, so the retention state
        machine is proven against a fake here and against a REAL account in
        the owner-gated live lane).
        """
        credential: Any
        owns_credential = False
        # .get_secret_value() FIRST, then test truthiness. A pydantic
        # SecretStr defines no __bool__, so `if self._settings.account_key:`
        # is True even for SecretStr("") — which would hand an EMPTY shared
        # key to the SDK instead of falling through to DefaultAzureCredential
        # (AKS workload identity / managed identity), the recommended
        # production posture. The S3 lane hit exactly this; see
        # S3ObjectStore._client_kwargs. The unwrapped value is used HERE and
        # nowhere else — everything upstream keeps the redacting wrapper, so
        # the key cannot surface in a traceback or a settings repr.
        #
        # Azure has NO partial-credential hazard equivalent to S3's
        # key-id/secret pair: there is a single credential field, and
        # account_url is already required-and-validated at construction. So
        # "account_url set, account_key empty" is a complete and VALID
        # configuration — the DefaultAzureCredential path — not a half-filled
        # one, and needs no both-set-or-both-empty validator.
        account_key = self._settings.account_key.get_secret_value()
        if account_key:
            credential = account_key
        else:
            credential = DefaultAzureCredential()
            owns_credential = True

        try:
            client = ContainerClient(
                account_url=self._settings.account_url,
                container_name=self._settings.container,
                credential=credential,
                connection_timeout=self._settings.connect_timeout_s,
                read_timeout=self._settings.read_timeout_s,
            )
            async with client:
                yield client
        finally:
            if owns_credential:
                await credential.close()

    def _physical(self, key: str) -> str:
        return _join_key(self._settings.key_prefix, key)

    def _logical(self, physical_key: str) -> str:
        return _strip_key(self._settings.key_prefix, physical_key)

    @staticmethod
    async def _props_or_none(blob: Any) -> Any | None:
        try:
            return await blob.get_blob_properties()
        except ResourceNotFoundError:
            return None

    def _check_version_cap(self, physical: str, count: int) -> None:
        """Fail LOUD when one key carries more versions than we will walk.

        See :class:`TooManyBlobVersionsError` for why this raises rather than
        truncating.
        """
        cap = self._settings.max_versions_per_key
        if count <= cap:
            return
        raise TooManyBlobVersionsError(
            f"key '{self._logical(physical)}' has more than {cap} blob versions; the adapter "
            "must inspect every version to find the one carrying the Locked immutability "
            "policy, so an unbounded walk here would stall list_objects and stop "
            "verify_checkpoints completing. Refusing to walk rather than truncating (a "
            "truncated walk can miss the Locked version and report an archived key as "
            "unprotected). Investigate who is layering versions onto this key; raise "
            "NE_STORAGE_AZURE_BLOB_MAX_VERSIONS_PER_KEY only if the volume is legitimate."
        )

    async def _key_versions(self, container: Any, physical: str) -> list[Any]:
        """Every VERSION of exactly ``physical`` — the protection-decision input.

        ``name_starts_with`` is a PREFIX, not an exact match (listing prefix
        ``k`` also returns ``k2``), so items are filtered to exact name
        equality. The cap check is INSIDE the loop so the walk stops as soon
        as MATCHING versions breach it, rather than collecting the whole pile
        first — and, as on the S3 lane, what the cap bounds is the per-key
        version count, not the paging over unrelated prefix-sharing keys.

        With versioning DISABLED there are no versions and the service
        returns a single item with ``version_id=None``; the callers below
        treat that as the current version, so read paths degrade gracefully
        rather than crashing. Only ``archive()`` fails, loudly, via
        :meth:`_ensure_vlw_enabled`.
        """
        versions: list[Any] = []
        async for item in container.list_blobs(name_starts_with=physical, include=_VERSION_INCLUDE):
            if getattr(item, "name", None) != physical:
                continue
            versions.append(item)
            self._check_version_cap(physical, len(versions))
        return versions

    def _pick_protected(self, versions: Sequence[Any]) -> Any | None:
        """The PROTECTED version among ``versions``, or ``None`` if none is.

        The heart of this adapter's WORM correctness, and the Azure analogue
        of ``S3ObjectStore._locked_version``. Azure immutability is
        VERSION-level, so every retention decision must be made about the
        version that actually carries the protection — NOT about whatever
        version happens to be current. Keying on the current version meant a
        single layered unlocked version permanently disabled the check for
        ALL callers.

        A version counts as PROTECTED if it carries EITHER an active
        **Locked** immutability policy (an Unlocked one guarantees nothing —
        see :meth:`_policy_expiry`) OR a legal hold.

        Ordering is STRONGEST-PROTECTION-FIRST:

        1. A **legal hold DOMINATES** any dated retention. A hold is
           INDEFINITE, so it outlasts every finite expiry — there is no date
           at which the held version stops being protected. A version
           carrying both a hold and a date therefore also outranks a
           date-only version at the same expiry.
        2. Among versions of equal hold status, the LATEST active expiry
           wins, so an extended retention always beats a stale one.

        This ordering is load-bearing beyond blocking. ``put``/``delete`` are
        refused whenever ANY version is protected, so they are correct under
        either ranking — but ``get()`` returns the SELECTED version's bytes
        and ``stat()`` reports the SELECTED version's expiry. Ranking a dated
        policy above a hold made ``get()`` hand back a different version's
        payload than the one under indefinite hold (wrong bytes for an
        audit-checkpoint verifier that reads through ``get()``), and made
        ``stat()`` advertise a finite ``worm_retain_until`` — implying
        protection ENDS on that date — while another version was held with no
        end at all. Both under-report the guarantee.

        3. ``version_id`` breaks any remaining tie, which is what makes this
           a total ORDER rather than a total preorder (Codex finding). Two
           versions with identical ``(held, expiry)`` — two hold-only
           versions, or two held versions sharing an expiry — otherwise
           resolved to whichever the listing yielded first, so ``get()``
           could hand back DIFFERENT BYTES for the same key on two calls.
           Equal protection does not imply equal payload, and a key must
           resolve to the same bytes every time for an audit store to be
           worth anything.

           What this tie-break guarantees is exactly DETERMINISM, and no
           more: version IDs are unique per version, so the maximum is
           well-defined and stable across calls. It is deliberately NOT
           documented as "newest among equals". Azure's REST contract calls
           the version ID an opaque ``DateTime`` value and does not promise a
           permanently fixed, lexicographically sortable representation —
           today's canonical fixed-width UTC form happens to sort
           chronologically, but relying on that would be asserting a property
           the service does not owe us. An earlier revision of this docstring
           claimed the chronological reading; it is corrected here rather
           than left, because a docstring claiming more than the code can
           guarantee is the specific failure mode this package has already
           shipped once (SPEC §11, the hold-vs-date ordering).
        """
        now = self._now()
        # Sorts below every real expiry, so a hold-only version ranks under a
        # held-and-dated one while still dominating every date-only version.
        floor = datetime.min.replace(tzinfo=UTC)
        best: Any | None = None
        best_rank: tuple[int, datetime, str] | None = None
        for props in versions:
            expiry = self._policy_expiry(props)
            dated = _is_retention_active(expiry, now)
            held = bool(getattr(props, "has_legal_hold", False))
            if not dated and not held:
                continue
            # version_id is the FINAL tiebreak, and it is what makes this a
            # total ORDER rather than a total preorder. Without it two versions
            # with identical (held, expiry) — two hold-only versions, or two
            # held versions sharing an expiry — resolve to whichever the
            # listing happened to yield first, so get() could return DIFFERENT
            # BYTES for the same key across two calls. Equal protection does
            # not imply equal payload, and an audit store must resolve a key to
            # the same bytes every time. Version IDs are unique per version, so
            # the maximum is well-defined and STABLE — determinism is the whole
            # claim here. See the docstring for why this is deliberately NOT
            # documented as picking the "newest": the version ID is an opaque
            # DateTime in Azure's REST contract, not a guaranteed sortable
            # representation.
            rank = (
                1 if held else 0,
                expiry if dated else floor,
                str(getattr(props, "version_id", "") or ""),
            )
            if best_rank is None or rank > best_rank:
                best, best_rank = props, rank
        return best

    @staticmethod
    def _pick_current(versions: Sequence[Any]) -> Any | None:
        """The CURRENT version among ``versions``, or ``None`` if the blob is deleted.

        ``is_current_version`` is populated per item on the versions listing.
        When versioning is off no item carries it and every item has
        ``version_id=None`` — that single item IS the blob, so it is treated
        as current. When versioning is on and the blob has been deleted, only
        non-current versions remain and this returns ``None``: the Azure
        analogue of an S3 delete marker sitting on top.
        """
        for props in versions:
            if getattr(props, "is_current_version", None):
                return props
        if len(versions) == 1 and getattr(versions[0], "version_id", None) is None:
            return versions[0]
        return None

    async def _protected_version(self, container: Any, physical: str) -> Any | None:
        return self._pick_protected(await self._key_versions(container, physical))

    @staticmethod
    def _version_id(props: Any | None) -> str | None:
        if props is None:
            return None
        version_id = getattr(props, "version_id", None)
        return str(version_id) if version_id else None

    async def _ensure_vlw_enabled(self, container: Any) -> None:
        """Fail loud, with the REAL reason, if the container has no VLW.

        Cached per adapter instance: VLW is a provisioning-time property that
        cannot change under a running process in any way that would make a
        previously-passing check start failing (disabling it requires
        recreating the container). Mirrors ``_object_lock_verified``.
        """
        if self._vlw_verified:
            return
        try:
            props = await container.get_container_properties()
        except HttpResponseError as exc:
            # 403 Forbidden / 401 Unauthorized here is a PERMISSIONS fault, not
            # a missing-VLW one, and the two need different operator actions.
            # See ContainerPropertiesPermissionError for why this is typed even
            # though the documented role does grant the action.
            if exc.status_code in _AUTHZ_STATUS_CODES:
                raise ContainerPropertiesPermissionError(
                    f"reading the properties of container '{self._settings.container}' was "
                    f"denied (HTTP {exc.status_code}); archive() preflights "
                    "get_container_properties() on every invocation to verify version-level "
                    "immutability, so the checkpoint identity MUST hold the RBAC action "
                    "'Microsoft.Storage/storageAccounts/blobServices/containers/read' — which "
                    "the documented 'Storage Blob Data Contributor' role grants, but a custom "
                    "role scoped only to '.../containers/blobs/*' data actions does NOT. "
                    "Without it put() still succeeds and archive() always fails, so checkpoint "
                    "objects land UNLOCKED. See docs/DEPLOYMENT.md §6.6 and "
                    "packages/ne-storage/README.md."
                ) from exc
            raise
        enabled = getattr(props, "immutable_storage_with_versioning_enabled", None)
        if not enabled:
            raise VersionLevelImmutabilityNotEnabledError(
                f"container '{self._settings.container}' reports "
                f"immutable_storage_with_versioning_enabled={enabled!r}; archive() needs "
                "version-level immutability (VLW), which implies blob versioning. Neither is "
                "settable from the data plane — VLW is enabled at container creation or "
                "inherited from an account-level default, so the container must be "
                "reprovisioned (see deploy/terraform-azure/modules/storage/main.tf and "
                "packages/ne-storage/README.md). Without it put() still succeeds and "
                "archive() always fails, so checkpoint objects land UNLOCKED."
            )
        self._vlw_verified = True

    @staticmethod
    def _policy_expiry(props: Any) -> datetime | None:
        """Expiry of a **Locked** immutability policy — the only real WORM.

        An **Unlocked** policy is deliberately reported as ABSENT. Unlocked is
        not a guarantee: it can be deleted or shortened by any caller with the
        right permission, so treating it as WORM would be a lie in both
        directions — spurious ``WormViolationError`` on ``put``/``delete``, and
        an ``archive()`` that short-circuits and leaves the blob unlocked
        forever (a C10 silent WORM downgrade). Use :meth:`_any_policy_expiry`
        when what is wanted is "any retention the blob effectively has".
        """
        policy = getattr(props, "immutability_policy", None)
        if policy is None:
            return None
        if not _is_locked_mode(getattr(policy, "policy_mode", None)):
            return None
        return getattr(policy, "expiry_time", None)

    @staticmethod
    def _any_policy_expiry(props: Any) -> datetime | None:
        """Expiry of ANY immutability policy — Locked, Unlocked or Mutable alike.

        Used ONLY as a floor when locking: whatever expiry this adapter writes
        must never be EARLIER than a retention the blob effectively already
        had, even an unlocked one. See :meth:`archive`.
        """
        policy = getattr(props, "immutability_policy", None)
        if policy is None:
            return None
        return getattr(policy, "expiry_time", None)

    def _stat_from_props(self, key: str, props: Any) -> ObjectStat:
        content_settings = getattr(props, "content_settings", None)
        content_type = getattr(content_settings, "content_type", None) if content_settings else None
        return ObjectStat(
            key=key,
            size=int(props.size),
            content_type=content_type or "application/octet-stream",
            metadata=dict(getattr(props, "metadata", None) or {}),
            worm_retain_until=self._policy_expiry(props),
        )

    def _raise_protected(self, key: str, protected: Any) -> None:
        """Report WHY the resolved protected version blocks a mutation.

        ``protected`` came from :meth:`_pick_protected`, so it carries an
        active Locked policy, a legal hold, or both. The HOLD is reported
        preferentially when present, matching the selection order: a hold is
        indefinite, so naming a finite expiry on a version that also carries
        a hold would tell the operator the protection ends on a date when it
        does not.
        """
        if getattr(protected, "has_legal_hold", False):
            raise WormViolationError(f"key '{key}' is under a legal hold")
        expiry = self._policy_expiry(protected)
        if _is_retention_active(expiry, self._now()):
            raise WormViolationError(f"key '{key}' is under WORM retention until {expiry}")
        raise WormViolationError(f"key '{key}' is under a legal hold")

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectStat:
        physical = self._physical(key)
        async with self._container() as container:
            # Keyed on the PROTECTED version, not the current one: otherwise a
            # single layered unlocked version disables this check forever.
            protected = await self._protected_version(container, physical)
            if protected is not None:
                self._raise_protected(key, protected)
            # Unscoped client on purpose — upload_blob must create a NEW
            # current version, never write through to the protected one.
            blob = container.get_blob_client(physical)
            await blob.upload_blob(
                data,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
                metadata=dict(metadata or {}),
            )
            new_props = await self._props_or_none(blob)
            if new_props is None:  # pragma: no cover - just written, must exist
                raise ObjectNotFoundError(f"no object at key '{key}' immediately after put")
            return self._stat_from_props(key, new_props)

    async def get(self, key: str) -> bytes:
        physical = self._physical(key)
        async with self._container() as container:
            # An archived key reads back the ARCHIVED bytes. Reading the
            # current version would return an attacker's layered payload while
            # the protected original stayed unreachable through this API —
            # "archived to WORM" has to imply recoverable, not merely
            # detectable-as-tampered.
            protected = await self._protected_version(container, physical)
            blob = container.get_blob_client(physical, version_id=self._version_id(protected))
            try:
                stream = await blob.download_blob()
                return await stream.readall()
            except ResourceNotFoundError as exc:
                raise ObjectNotFoundError(f"no object at key '{key}'") from exc

    async def stat(self, key: str) -> ObjectStat:
        physical = self._physical(key)
        async with self._container() as container:
            # The protected version's properties are the truth to report: a
            # layered current version carries no policy, and reporting IT
            # would say "no retention" about a key that genuinely has one.
            protected = await self._protected_version(container, physical)
            if protected is not None:
                return self._stat_from_props(key, protected)
            props = await self._props_or_none(container.get_blob_client(physical))
            if props is None:
                raise ObjectNotFoundError(f"no object at key '{key}'")
            return self._stat_from_props(key, props)

    async def delete(self, key: str) -> None:
        physical = self._physical(key)
        async with self._container() as container:
            protected = await self._protected_version(container, physical)
            if protected is not None:
                self._raise_protected(key, protected)
            # Unscoped client: this deletes the CURRENT blob, never a specific
            # version. Reaching here means nothing is protected anyway.
            blob = container.get_blob_client(physical)
            props = await self._props_or_none(blob)
            if props is None:
                return  # already absent — matches MemoryObjectStore's dict.pop(default)
            await blob.delete_blob()

    async def list_objects(self, prefix: str = "") -> Sequence[ObjectStat]:
        full_prefix = self._physical(prefix)
        async with self._container() as container:
            # ONE paged listing WITH versions — not a current-blobs listing
            # plus an N+1 property fan-out. `include=_VERSION_INCLUDE` carries
            # the immutability policy, legal hold, content-type, size and
            # metadata on every item (see _VERSION_INCLUDE), so Azure needs no
            # per-version round trip at all. Crucially, without 'versions' a
            # key whose current version was deleted vanishes from the listing
            # entirely even while a Locked version of it still exists — which
            # would hide an archived audit checkpoint from verify_checkpoints.
            #
            # SCOPE OF max_versions_per_key, precisely: the WHOLE listing is
            # accumulated here BEFORE any cap fires, so the cap does NOT bound
            # this method's memory — it bounds the per-key version FAN-OUT that
            # the protection walk below would otherwise do without limit. A
            # single key with a pathological version count is caught; a prefix
            # with pathologically many distinct keys is not, and is not meant
            # to be. Same shape, same accumulate-then-cap ordering, as the
            # already-reviewed S3 lane; the two must not diverge.
            versions_by_key: dict[str, list[Any]] = {}
            async for item in container.list_blobs(
                name_starts_with=full_prefix, include=_VERSION_INCLUDE
            ):
                versions_by_key.setdefault(str(item.name), []).append(item)

            out: list[ObjectStat] = []
            for physical in sorted(versions_by_key):
                versions = versions_by_key[physical]
                self._check_version_cap(physical, len(versions))
                logical = self._logical(physical)
                protected = self._pick_protected(versions)
                if protected is not None:
                    # A protected key is ALWAYS listed, and its stat comes from
                    # the protected version — never from a layered current one.
                    out.append(self._stat_from_props(logical, protected))
                    continue
                current = self._pick_current(versions)
                if current is None:
                    # Legitimately deleted (no current version, no protection
                    # anywhere): stays hidden, matching MemoryObjectStore.
                    continue
                out.append(self._stat_from_props(logical, current))
            return out

    async def archive(self, key: str, *, retain_until: datetime) -> ArchiveReceipt:
        requested = _normalize_retain_until(retain_until)
        physical = self._physical(key)
        async with self._container() as container:
            await self._ensure_vlw_enabled(container)

            # Resolve WHICH version this archive() will write to, first. The
            # protected version wins when there is one — an extension must
            # land on the already-archived version, never on whatever version
            # happens to be current — otherwise it is the current version.
            versions = await self._key_versions(container, physical)
            props = self._pick_protected(versions)
            if props is None:
                props = self._pick_current(versions)
            if props is None:
                raise ObjectNotFoundError(f"no object at key '{key}'")
            blob = container.get_blob_client(physical, version_id=self._version_id(props))

            # LOCKED-only. An Unlocked policy must NOT short-circuit any of
            # the three branches below: doing so would return a success
            # receipt (or a spurious refusal) while leaving the blob freely
            # deletable — the C10 silent-WORM-downgrade shape. Unlocked falls
            #
            # NB every retention comparison below — the shorten check, the
            # idempotent-equal check, and the `effective` floor — is scoped to
            # the SINGLE target version resolved above, deliberately. It is
            # NOT a max() across all versions of the key, and a reviewer WILL
            # raise that as "archiving to day 60 while another version holds
            # day 90 shortens the key's protection". It does not: Azure
            # immutability is PER-VERSION, so a set_immutability_policy scoped
            # to version A has no effect whatsoever on version B — version B
            # keeps its own policy, and the key's effective protection stays
            # max() across versions no matter what is written here. Nothing
            # can be shortened. The only real consequence is that the
            # ArchiveReceipt may UNDER-report the key's total protection
            # (reporting this version's expiry, not the key-wide maximum),
            # which is the safe direction. Widening the floor to scan every
            # version would be strictly WORSE: it would write a LONGER
            # retention than the caller asked for, derived from an unrelated
            # version, into an immutable and unshortenable lock.
            # through to set_immutability_policy so the blob actually gets
            # Locked, which is the whole point of archive().
            existing = self._policy_expiry(props)
            now = self._now()
            if existing is not None and _is_retention_active(existing, now):
                if requested < existing:
                    raise WormViolationError(
                        f"key '{key}' is under WORM retention until {existing}; retention may "
                        f"only be extended, not shortened to {requested}"
                    )
                if requested == existing:
                    # Idempotent — no server call, matching MemoryObjectStore.
                    return ArchiveReceipt(key=key, retain_until=existing, archived_at=now)

            # Never write an expiry EARLIER than a retention the blob already
            # effectively had, INCLUDING an unlocked one. A container-level
            # default VLW policy stamps new blobs Unlocked at (say) 30 days;
            # the audit checkpoint loop then archives with retain_until=+1day.
            # Locking at 1 day would silently SHORTEN an audit retention from
            # 30 days to 1 — a P0-class defect, and precisely what this
            # adapter exists to make impossible. So we lock at the LATER of
            # the two. Ceiling-only, exactly like _normalize_retain_until.
            target = requested
            effective = self._any_policy_expiry(props)
            if (
                effective is not None
                and effective > target
                and _is_retention_active(effective, now)
            ):
                target = effective

            try:
                await blob.set_immutability_policy(
                    ImmutabilityPolicy(
                        expiry_time=target,
                        policy_mode=BlobImmutabilityPolicyMode.LOCKED,
                    )
                )
            except HttpResponseError as exc:
                if _is_worm_denial(exc):
                    raise WormViolationError(
                        f"key '{key}' retention update to {target} was denied by the "
                        "server (lost a race against a concurrent archive())"
                    ) from exc
                raise
            return ArchiveReceipt(key=key, retain_until=target, archived_at=now)
