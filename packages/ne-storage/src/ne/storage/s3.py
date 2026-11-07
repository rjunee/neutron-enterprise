# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""S3ObjectStore — the production :class:`~ne.contracts.storage.ObjectStore` adapter.

Speaks S3 (or an S3-compatible endpoint) via ``aiobotocore`` — a genuinely
async client (not sync-boto3-in-a-threadpool), matching the contract's async
Protocol. ``archive()`` is backed by real S3 Object Lock in COMPLIANCE mode.

WORM honesty (read this before trusting the guarantee — see also
packages/ne-storage/README.md "WORM honesty" section):

* The archived object **VERSION**'s bytes are made immutable by the SERVER
  itself: ``PutObjectRetention`` in COMPLIANCE mode is enforced by S3/MinIO —
  a raw client bypassing this adapter entirely still gets ``DeleteObject
  VersionId=<locked>`` and any retention-shortening ``PutObjectRetention``
  denied (proven in ``tests/test_s3_worm_live.py``). Nobody, including a root
  credential, can delete or shorten a COMPLIANCE-locked version before
  ``retain_until``.
* The guarantee is specifically **COMPLIANCE-mode** Object Lock. S3's other
  mode, ``GOVERNANCE``, is deliberately NOT treated as WORM anywhere in this
  adapter: a principal holding ``s3:BypassGovernanceRetention`` can delete a
  GOVERNANCE-locked version and can shorten its retention (both verified
  against MinIO), so it is a guardrail, not write-once storage. Consequently a
  version carrying only a GOVERNANCE retention is never reported as
  ``ObjectStat.worm_retain_until`` and never raises ``WormViolationError`` from
  ``put``/``delete`` — reporting it would overclaim a guarantee the bytes do
  not have, and blocking on it would refuse legitimate writes on that same
  false premise. ``archive()`` over an active GOVERNANCE lock is REFUSED BY
  DEFAULT (``GovernanceUpgradeRefusedError``); the upgrade-to-COMPLIANCE
  behaviour is opt-in via ``NE_STORAGE_S3_ALLOW_GOVERNANCE_UPGRADE`` because
  its ``max(requested, governance)`` clamp is a read-then-write that a
  concurrent retention extension can defeat (see
  :meth:`S3ObjectStore.archive`). Objects archived by this adapter are always
  COMPLIANCE, so a GOVERNANCE lock can only arrive from another tool, a bucket
  default, or a prior deployment.
* An Object Lock LEGAL HOLD **is** treated as a lock. Unlike GOVERNANCE it is
  genuinely server-enforced with no bypass permission — it simply has no date.
  So a held version keeps the key LISTED and makes ``put``/``delete`` raise
  ``WormViolationError``, while ``ObjectStat.worm_retain_until`` stays ``None``
  (there is no instant to report). See :func:`_legal_hold_on`.
* The per-key version walk is CAPPED (``NE_STORAGE_S3_MAX_VERSIONS_PER_KEY``)
  and a breach raises :class:`TooManyObjectVersionsError` rather than stalling
  or truncating — see :meth:`S3ObjectStore._check_version_cap`.
* Every retention decision is keyed on the ARCHIVED VERSION, never on the
  current one. Object Lock protects a VERSION, not a KEY, so ``put``,
  ``delete``, ``get``, ``stat``, ``list_objects`` and ``archive`` all resolve
  the version that actually carries an active ``RetainUntilDate``
  (:meth:`S3ObjectStore._locked_version`) before acting. Reasoning about the
  current version — what this adapter used to do via ``HeadObject`` — had two
  fatal consequences, both now covered by regression tests:

  - One out-of-band ``PutObject`` layered an unlocked version on top of the
    locked one, and from that moment the client-side check was off
    PERMANENTLY, for every caller, not transiently for a racing one.
  - S3 NEVER blocks a versionless ``DeleteObject``, not even in COMPLIANCE
    mode: it layers a DELETE MARKER over the locked version. Listing current
    versions therefore made an archived audit checkpoint invisible, and
    ``ne.audit.checkpoint.verify_checkpoints`` reported ``valid=True,
    checked=0`` — a clean green over a destroyed tamper anchor.

* The KEY-level ``put``/``delete`` blocking the frozen contract requires is
  still CLIENT-SIDE and therefore BEST-EFFORT: a read followed by the mutating
  call, with a TOCTOU window between them. It is a courtesy error, not the
  guarantee. The DURABLE guarantee is the server-locked VERSION, which a
  concurrent ``put`` cannot touch (S3 always writes a NEW version) and which
  remains reachable by ``VersionId`` — and which ``get``/``stat`` now resolve
  for you, so archived bytes stay readable through the ObjectStore API rather
  than being shadowed by an attacker's layered version.

* Seeing through delete markers is DEFENCE-IN-DEPTH, not the control. Because
  ``s3:DeleteObject`` without a ``VersionId`` is always permitted, the
  REQUIRED bucket policy denying it on the checkpoint prefix (see
  packages/ne-storage/README.md and docs/DEPLOYMENT.md) is what actually stops
  the marker from being created.

Empirical precision note (verified against MinIO RELEASE.2025-09-07T16-13-09Z,
STEP 0 of NE-S3-OBJECTSTORE): ``ObjectLockRetainUntilDate`` does not round-trip
sub-second precision unchanged — the server truncates to millisecond
resolution. :func:`_normalize_retain_until` ceils any sub-second
``retain_until`` up to the next whole second before it is ever sent to the
server, so the *effective* stored retention is never earlier than what the
caller asked for (never a silent shortening).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, TypeGuard

from aiobotocore.config import AioConfig
from aiobotocore.session import AioSession, get_session
from botocore.exceptions import ClientError

from ne.contracts.errors import ObjectNotFoundError, WormViolationError
from ne.contracts.storage import ArchiveReceipt, ObjectStat
from ne.net.airgap import AirGapViolationError, air_gap_engaged
from ne.net.ssrf import UnsafeEndpointError, validate_in_vpc_endpoint
from ne.storage.config import S3ObjectStoreSettings


class ObjectLockNotEnabledError(RuntimeError):
    """The configured bucket does not have S3 Object Lock enabled.

    A PERMANENT configuration fault, not a transient one: Object Lock can only
    be turned on at bucket creation (``ObjectLockEnabledForBucket=True``) and
    cannot be retrofitted onto an existing bucket. Raised instead of the raw
    ``KeyError('VersionId')`` (unversioned buckets return no VersionId) or the
    misleading "lost a race against a concurrent archive()"
    ``WormViolationError`` the server's InvalidRequest used to be translated
    into.
    """


class ObjectLockPermissionError(RuntimeError):
    """The credential may not read the bucket's Object Lock configuration.

    A PERMISSIONS fault, distinct from :class:`ObjectLockNotEnabledError` (a
    bucket that genuinely has no Object Lock). Both are permanent
    configuration faults on the exact path C10 depends on, and both must fail
    LOUD and TYPED rather than as a raw ``botocore.ClientError``.

    Empirically verified (MinIO RELEASE.2025-09-07T16-13-09Z, round-3 review):
    a credential provisioned with EXACTLY the previously-documented minimal IAM
    action list got ``put: OK`` followed by
    ``archive: AccessDenied ... calling the GetObjectLockConfiguration
    operation``. ``write_checkpoint`` does ``put`` THEN ``archive``, so an
    operator following the docs verbatim had every checkpoint object landing in
    the bucket UNLOCKED and the loop then dying on an untyped error — a C10
    downgrade on the documented happy path. The docs now list
    ``s3:GetBucketObjectLockConfiguration``; this error names it when they are
    not followed.
    """


class TooManyObjectVersionsError(RuntimeError):
    """A single key carries more versions than the adapter will scan.

    Resolving which VERSION holds the Object Lock costs one ``HeadObject`` per
    version, and ``s3:PutObject`` — a permission the deployment MUST grant —
    lets any holder layer unlimited unlocked versions onto one checkpoint key.
    Raised instead of stalling, because an unbounded scan makes the tamper
    anchor UNVERIFIABLE (``verify_checkpoints`` never completing) with nothing
    to make that visible. Silently truncating the scan would be worse still: a
    truncated walk can miss the locked version and report an archived key as
    unprotected.
    """


class GovernanceUpgradeRefusedError(RuntimeError):
    """``archive()`` refused to upgrade a standing GOVERNANCE lock to COMPLIANCE.

    The upgrade is a read-then-write over someone else's retention with no
    compare-and-set available (S3 Object Lock has none), so it is OPT-IN and
    refused by default. See :meth:`S3ObjectStore.archive` and
    packages/ne-storage/README.md "GOVERNANCE → COMPLIANCE upgrade".
    """


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize_retain_until(retain_until: datetime) -> datetime:
    """Ceil a sub-second ``retain_until`` to the next whole second.

    Raises ``ValueError`` for a timezone-naive datetime (an S3 retention
    timestamp must be an unambiguous instant). Whole-second timestamps
    (``microsecond == 0``) pass through unchanged. See the module docstring
    "Empirical precision note" for why ceiling (never truncating) is the only
    safe direction: it can only ever make a retention EQUAL-or-LATER than
    requested, never earlier.
    """
    if retain_until.tzinfo is None:
        raise ValueError("retain_until must be timezone-aware")
    if retain_until.microsecond == 0:
        return retain_until
    return retain_until.replace(microsecond=0) + timedelta(seconds=1)


def _is_retention_active(retain_until: datetime | None, now: datetime) -> TypeGuard[datetime]:
    """True if ``retain_until`` is a real, still-in-force retention timestamp.

    A ``TypeGuard`` rather than a plain ``bool`` so callers that go on to
    compare retention instants narrow to ``datetime`` without a redundant
    ``is not None`` re-check.
    """
    return retain_until is not None and retain_until > now


COMPLIANCE_MODE = "COMPLIANCE"
GOVERNANCE_MODE = "GOVERNANCE"


def _retain_until_in_mode(head: Mapping[str, Any], mode: str) -> datetime | None:
    """``ObjectLockRetainUntilDate`` only when the lock is in ``mode``.

    S3 Object Lock has TWO modes and only one of them is WORM:

    * ``COMPLIANCE`` — nobody, not even a root credential, can delete the
      version or shorten its retention before ``RetainUntilDate``. This is the
      guarantee C10 (audit-log integrity) rests on, and the only mode
      :meth:`S3ObjectStore.archive` ever writes.
    * ``GOVERNANCE`` — a principal holding ``s3:BypassGovernanceRetention``
      CAN delete the version and CAN shorten the retention (verified against
      MinIO RELEASE.2025-09-07T16-13-09Z: both operations succeed with
      ``BypassGovernanceRetention=True``). It is a guardrail, not WORM.

    Reading ``ObjectLockRetainUntilDate`` without inspecting ``ObjectLockMode``
    — what this adapter used to do at every site — reported a GOVERNANCE lock
    as a WORM guarantee it does not provide, and blocked legitimate
    ``put``/``delete`` on the strength of it. Both directions are wrong, so
    every retention decision in this module goes through this helper.
    """
    if head.get("ObjectLockMode") != mode:
        return None
    retain_until = head.get("ObjectLockRetainUntilDate")
    return retain_until if isinstance(retain_until, datetime) else None


def _compliance_retain_until(head: Mapping[str, Any]) -> datetime | None:
    """The COMPLIANCE-mode (i.e. genuinely WORM) retention on a head, or ``None``."""
    return _retain_until_in_mode(head, COMPLIANCE_MODE)


def _governance_retain_until(head: Mapping[str, Any]) -> datetime | None:
    """The GOVERNANCE-mode (bypass-able, NOT WORM) retention on a head, or ``None``."""
    return _retain_until_in_mode(head, GOVERNANCE_MODE)


def _legal_hold_on(head: Mapping[str, Any]) -> bool:
    """True if this version carries an ACTIVE Object Lock legal hold.

    A legal hold is a SECOND, independent Object Lock protection: no date, in
    force until an explicit ``PutObjectLegalHold Status=OFF``, and — unlike
    GOVERNANCE — genuinely server-enforced with no bypass permission. Verified
    against MinIO RELEASE.2025-09-07T16-13-09Z (round-3 review): a version put
    with ``ObjectLockLegalHoldStatus=ON`` and NO retention date answers
    ``HeadObject`` with ``ObjectLockLegalHoldStatus: 'ON'`` (that exact field
    name and casing — the value is compared case-insensitively anyway), and
    ``DeleteObject VersionId=<held>`` is REFUSED
    (``InvalidRequest``/400, "Object is WORM protected and cannot be
    overwritten"), exactly as for a COMPLIANCE retention.

    Before this was handled, a legal hold was invisible to the adapter: ``put``
    and ``delete`` were both ALLOWED, the adapter's own ``delete()`` laid a
    delete marker over genuinely protected bytes, and ``list_objects`` then
    reported the key ABSENT — the same shape as the round-1 C10 hole, reopened
    for a protection mode the delete-marker fix did not consider. So a legal
    hold counts as a lock in :meth:`S3ObjectStore._locked_version`.

    It is deliberately NOT reported as ``ObjectStat.worm_retain_until``: that
    field is an INSTANT, and a legal hold has none. ``None`` there means "no
    dated WORM retention", not "unprotected" — the key staying LISTED and
    ``put``/``delete`` raising is how the protection is surfaced.

    **This read is PERMISSION-GATED on real AWS, and fails OPEN when the
    permission is missing.** Per the S3 API reference for ``HeadObject``, the
    ``x-amz-object-lock-legal-hold`` response header "is only returned if the
    requester has the ``s3:GetObjectLegalHold`` permission" — without it the
    ``HeadObject`` still returns ``200`` and simply OMITS the header, so this
    function returns ``False`` for a genuinely held version and the hold is
    INVISIBLE rather than erroring. The consequence is precisely the round-1
    C10 hole: :meth:`S3ObjectStore._locked_version` misses the hold-only
    version, ``delete()`` lays a delete marker over it, and the archived
    checkpoint drops out of ``list_objects``. So ``s3:GetObjectLegalHold`` is
    in the documented minimal IAM list and is load-bearing for CORRECTNESS,
    not merely for access.

    MinIO returns the header regardless of permission, so the MinIO test server
    CANNOT catch its absence by behaviour alone — which is why
    ``test_documented_iam_actions_suffice_for_the_checkpoint_write_path``
    exercises a legal hold through a credential scoped to exactly the published
    list, pinning the ACTION LIST rather than the server's leniency.

    The sibling reads are covered: ``x-amz-object-lock-mode`` and
    ``x-amz-object-lock-retain-until-date`` (:func:`_retain_until_in_mode`,
    hence both ``_compliance_retain_until`` and ``_governance_retain_until``)
    are gated on ``s3:GetObjectRetention``, which the documented list has
    always carried. Those two headers plus this one are the only permission-
    gated protection-mode reads the adapter performs — checked against the
    HeadObject response-header reference, not assumed.
    """
    return str(head.get("ObjectLockLegalHoldStatus", "")).upper() == "ON"


# Sorts below every real ``RetainUntilDate``, so a hold-only version ranks
# under a held-AND-dated one while still dominating every date-only version.
_NO_EXPIRY = datetime.min.replace(tzinfo=UTC)


def _protection_rank(head: Mapping[str, Any], now: datetime) -> tuple[int, datetime] | None:
    """How STRONG the server-enforced protection on ``head`` is, or ``None`` if there is none.

    The single ranking rule for this adapter, shared by
    :meth:`S3ObjectStore._locked_version` and the twin walk inside
    :meth:`S3ObjectStore.list_objects` so the two cannot drift apart.

    A version is protected if it carries EITHER an active COMPLIANCE-mode
    ``RetainUntilDate`` OR an Object Lock LEGAL HOLD (:func:`_legal_hold_on`).
    A GOVERNANCE retention is deliberately NOT protection here: it is
    bypass-able and therefore not a WORM guarantee at all
    (:func:`_retain_until_in_mode`).

    Ordering is STRONGEST-PROTECTION-FIRST, and the tuple encodes exactly two
    rules:

    1. A legal hold **DOMINATES** any dated retention. A hold is INDEFINITE,
       so it outlasts every finite expiry — there is no instant at which a
       held version stops being protected. A version carrying both a hold and
       a date therefore also outranks a date-only version at the same expiry.
    2. Among versions of equal hold status, the LATEST active expiry wins, so
       an extended retention always beats a stale one.

    Comparing the returned tuples with ``>`` is a total PREORDER, not a total
    ORDER: two versions with identical ``(held, expiry)`` compare equal, so the
    strict ``>`` at the call sites keeps whichever was seen FIRST. Stated
    precisely because an earlier draft of this docstring claimed "total order",
    and a docstring asserting a stronger property than the code has is the
    exact failure this whole change exists to correct — a reviewer leans on the
    claim and skips the tie analysis.

    What that tie CANNOT do: change whether the key is protected, or until
    when. Every protected version outranks every unprotected one, so ``best``
    is non-``None`` for exactly the same version set regardless of order, and
    ``put``/``delete`` block identically.

    What it COULD do in principle is decide which equally-protected version's
    bytes ``get()`` returns. On S3 that is **empirically deterministic** rather
    than merely argued: ``ListObjectVersions`` is a server-side sort (key
    ascending, then the version stack newest-first), so ``_key_versions``
    yields a stable order across calls and processes, and the exact-key filter
    and pagination walk preserve it. A reviewer probed all three tie classes
    live (both-held-and-dated at equal expiry, both-date-only at equal expiry,
    both-hold-only), 12 ``get()`` calls each on fresh client sessions, and got
    one distinct payload every time. Ties resolve to the NEWEST version.

    The sibling Azure adapter needs an explicit ``version_id`` final tiebreak
    for this same guarantee, because its ordering is not similarly pinned.
    Adding one here would make the invariant self-evident instead of dependent
    on an S3 ordering guarantee — worth doing, but it is a hardening, not a
    fix: this change introduces no new tie class (equal-expiry dated versions
    and multiple hold-only versions tied identically before it).
    """
    retain_until = _compliance_retain_until(head)
    dated = _is_retention_active(retain_until, now)
    held = _legal_hold_on(head)
    if not dated and not held:
        return None
    return (1 if held else 0, retain_until if dated else _NO_EXPIRY)


def _lock_reason(head: Mapping[str, Any]) -> str:
    """Human-readable description of why a version is locked, for error messages.

    A locked version may carry a dated COMPLIANCE retention, a legal hold, or
    both, so the message is composed rather than assuming a ``retain_until``
    exists (it does not for a hold-only lock).

    The LEGAL HOLD clause comes FIRST when both apply, mirroring the ranking in
    :func:`_protection_rank`. Leading with the retention named a finite end
    date on a version that is in fact held INDEFINITELY, so the refusal
    understated its own reason — an operator reading "under WORM retention
    until <date>" would reasonably conclude the write succeeds after that date,
    when the hold means it does not.
    """
    parts: list[str] = []
    if _legal_hold_on(head):
        parts.append(
            "under an Object Lock LEGAL HOLD (indefinite — no retain-until date; cleared only "
            "by an explicit PutObjectLegalHold Status=OFF)"
        )
    retain_until = _compliance_retain_until(head)
    if retain_until is not None:
        parts.append(f"under WORM retention until {retain_until}")
    return " and ".join(parts) if parts else "under an Object Lock"


def _join_key(prefix: str, key: str) -> str:
    return f"{prefix}{key}" if prefix else key


def _strip_key(prefix: str, physical_key: str) -> str:
    if prefix and physical_key.startswith(prefix):
        return physical_key[len(prefix) :]
    return physical_key


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _status_code(exc: ClientError) -> int | None:
    meta = exc.response.get("ResponseMetadata", {})
    code = meta.get("HTTPStatusCode")
    return int(code) if code is not None else None


_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


def _is_not_found(exc: ClientError) -> bool:
    """True only for a missing OBJECT — never for a missing BUCKET.

    MinIO's HeadObject 404 carries ``Error.Code == "404"`` (a bare status
    string, because HEAD has no response body to parse), while GetObject's
    404 carries ``NoSuchKey``; AWS uses ``NotFound`` for HeadObject. Keying on
    the HTTP status ALONE (the old behavior) also swallowed ``NoSuchBucket``,
    so a typo'd ``NE_STORAGE_S3_BUCKET`` made ``stat``/``get`` raise
    ``ObjectNotFoundError`` and — worse — made ``delete()`` return SUCCESS
    having done nothing: a broken deployment was indistinguishable from an
    empty audit store.

    Caveat (empirical, MinIO RELEASE.2025-09-07T16-13-09Z): HeadObject against
    an ABSENT BUCKET also answers a bodyless ``404``, so this predicate alone
    cannot separate the two on that one call. The separation is structural
    instead: every mutating/reading path runs :meth:`S3ObjectStore._locked_version`
    (a ``ListObjectVersions`` page-walk, which DOES answer ``NoSuchBucket``)
    BEFORE any HeadObject, so the bucket fault surfaces first. See
    ``tests/test_s3_worm_live.py::test_missing_bucket_surfaces_nosuchbucket``.
    """
    return _error_code(exc) in _NOT_FOUND_CODES


_OBJECT_LOCK_MISSING_CODES = {
    # AWS S3's documented code for GetObjectLockConfiguration on a bucket
    # without Object Lock.
    "ObjectLockConfigurationNotFoundError",
    # Empirically identical on MinIO RELEASE.2025-09-07T16-13-09Z (verified in
    # STEP 0 of this fix round: code ObjectLockConfigurationNotFoundError,
    # HTTP 404). Kept as an explicit set so an S3 appliance that answers with
    # a different spelling can be added without reworking the control flow.
    "ObjectLockConfigurationNotFound",
    "InvalidRequest",
}


def _is_worm_denial(exc: ClientError) -> bool:
    """True if the server rejected a retention mutation because of a WORM lock.

    Empirically (MinIO RELEASE.2025-09-07T16-13-09Z), a COMPLIANCE-mode
    shorten attempt returns ``InvalidRequest`` / HTTP 400 ("Object is WORM
    protected and cannot be overwritten"). AWS S3's documented behavior for
    the same class of denial is ``AccessDenied`` — which it serves as HTTP
    403, a status the old 400-only guard could never have matched.

    Deliberately NARROW. ``InvalidRequest``/400 is a catch-all that also covers
    a past-dated ``RetainUntilDate``, absent lock configuration and malformed
    retention payloads; labelling those "lost a race against a concurrent
    archive()" fabricated a diagnosis for a permanent config fault. Missing
    lock configuration is now excluded LOCALLY before this path is reachable
    (:meth:`S3ObjectStore._ensure_object_lock_enabled`). A past-dated
    ``RetainUntilDate`` is deliberately NOT excluded locally — see the decision
    note in :meth:`S3ObjectStore.archive` — so the raised message no longer
    asserts a race as THE diagnosis, it offers it as one possible cause.
    """
    code = _error_code(exc)
    status = _status_code(exc)
    if code == "InvalidRequest":
        return status == 400
    if code == "AccessDenied":
        return status in {400, 403}
    return False


class S3ObjectStore:
    """Production S3-compatible :class:`~ne.contracts.storage.ObjectStore` adapter.

    No network I/O happens in ``__init__`` (mirroring
    ``OpenBaoSecretStore``'s no-I/O-at-construction posture) — a fresh
    aiobotocore client is opened for the duration of each Protocol method
    call. The clock is injectable (``now=lambda: ...``) for deterministic
    client-side retention comparisons, matching ``MemoryObjectStore``.
    """

    def __init__(
        self,
        settings: S3ObjectStoreSettings | None = None,
        *,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings if settings is not None else S3ObjectStoreSettings()
        self._now = now

        if not self._settings.bucket:
            raise ValueError(
                "NE_STORAGE_S3_BUCKET is not set; the 's3' ObjectStore adapter requires an "
                "explicit bucket (fail loud at boot)"
            )

        # Explicit credentials are BOTH-or-NEITHER. Setting only one leaks a
        # partial credential into botocore, which raises PartialCredentialsError
        # at first use rather than falling through to the default chain — a
        # cryptic runtime failure for what is a boot-time misconfiguration.
        # Both-empty is the RECOMMENDED production posture (IAM role / IRSA via
        # the botocore chain), so only the mismatched case is an error.
        if bool(self._settings.access_key_id) != bool(
            self._settings.secret_access_key.get_secret_value()
        ):
            raise ValueError(
                "NE_STORAGE_S3_ACCESS_KEY_ID and NE_STORAGE_S3_SECRET_ACCESS_KEY must be set "
                "together or both left empty; exactly one was set. Leave BOTH empty to use "
                "the default credential chain (IAM instance role / IRSA — the recommended "
                "production posture), or set BOTH for a fixed key pair (dev/CI)."
            )

        # AIR_GAP is evaluated UNCONDITIONALLY. The previous
        # `if endpoint_url: ... elif air_gap_engaged():` shape meant a non-empty
        # endpoint_url skipped the air-gap branch entirely and fell through to
        # validate_in_vpc_endpoint, which honours the operator's allowed_hosts —
        # so AIR_GAP=1 with endpoint_url=https://s3.amazonaws.com and that host
        # allowlisted constructed cleanly and egressed to the public internet.
        engaged = air_gap_engaged()

        if self._settings.endpoint_url:
            # Under AIR_GAP the operator allowlist is IGNORED: an env var must
            # not be able to punch a hole through the air gap. Only PROVABLY
            # in-VPC hosts (loopback / private IP / dotless service name /
            # .internal / .local / .localhost) pass.
            allowed = (
                []
                if engaged
                else [h.strip() for h in self._settings.allowed_hosts.split(",") if h.strip()]
            )
            # `external_hint` is passed POSITIONALLY-BY-NAME rather than through a
            # `**{...}` unpack: a `dict[str, str]` unpack tells the type checker
            # only "some str keyword", so it matches against whichever keyword-only
            # parameter it likes — which broke the moment `validate_in_vpc_endpoint`
            # grew a non-str keyword (`air_gap: bool`). The default below is the
            # validator's own default hint, so the not-engaged branch is unchanged.
            hint = (
                "AIR_GAP is engaged, so NE_STORAGE_S3_ALLOWED_HOSTS is ignored; "
                "point NE_STORAGE_S3_ENDPOINT_URL at an in-VPC endpoint or "
                "disable AIR_GAP"
                if engaged
                else "pass allowed_hosts=(...,) if it is private DNS reachable only inside the VPC"
            )
            validate_in_vpc_endpoint(
                self._settings.endpoint_url,
                allowed_hosts=allowed,
                error_cls=AirGapViolationError if engaged else UnsafeEndpointError,
                external_hint=hint,
            )
        elif engaged:
            raise AirGapViolationError(
                "NE_STORAGE_S3_ENDPOINT_URL is empty (default AWS public endpoints) while "
                "AIR_GAP is engaged; set an in-VPC/allowlisted endpoint_url or disable AIR_GAP"
            )

        self._session: AioSession = get_session()
        # Set once per adapter instance after a successful
        # GetObjectLockConfiguration — the bucket's lock config is immutable
        # (it can only be set at creation), so one verification per process is
        # sufficient and keeps archive() to a single extra call on first use.
        self._object_lock_verified = False

    @classmethod
    def from_settings(cls, settings: object | None = None) -> S3ObjectStore:
        """Construct purely from ``NE_STORAGE_S3_*`` env — the worker builder's seam.

        Accepts (and IGNORES) a positional settings object of ANY type. The
        NE-AUDIT-CRON checkpoint-loop builder resolves an ``ObjectStore``
        plugin and calls ``cls.from_settings(platform.audit_settings)``
        uniformly across whichever adapter is configured — but
        ``AuditSettings`` carries no notion of an S3 bucket/credentials; those
        live in this adapter's OWN env-driven ``S3ObjectStoreSettings``. This
        mirrors the Cedar PDP's ``from_settings`` ignore-precedent: same seam
        (a generically-typed caller resolving a plugin-specific settings
        model), same resolution (construct from the adapter's own settings
        source, ignore the argument).
        """
        return cls()

    def _client_kwargs(self) -> dict[str, Any]:
        s = self._settings
        kwargs: dict[str, Any] = {
            "region_name": s.region,
            "config": AioConfig(
                s3={"addressing_style": "path" if s.force_path_style else "auto"},
                connect_timeout=s.connect_timeout_s,
                read_timeout=s.read_timeout_s,
            ),
        }
        if s.endpoint_url:
            kwargs["endpoint_url"] = s.endpoint_url
        if s.access_key_id:
            kwargs["aws_access_key_id"] = s.access_key_id
        # .get_secret_value() FIRST, then test truthiness. A pydantic SecretStr
        # defines no __bool__, so `if s.secret_access_key:` is True even for
        # SecretStr("") — which would pass an EMPTY key to botocore instead of
        # falling through to the default credential chain, breaking the
        # recommended IAM-role posture. The unwrapped values are used here and
        # nowhere else; everything upstream keeps the redacting wrapper.
        secret_access_key = s.secret_access_key.get_secret_value()
        session_token = s.session_token.get_secret_value()
        if secret_access_key:
            kwargs["aws_secret_access_key"] = secret_access_key
        if session_token:
            kwargs["aws_session_token"] = session_token
        return kwargs

    def _client(self) -> Any:
        return self._session.create_client("s3", **self._client_kwargs())

    def _physical(self, key: str) -> str:
        return _join_key(self._settings.key_prefix, key)

    def _logical(self, physical_key: str) -> str:
        return _strip_key(self._settings.key_prefix, physical_key)

    async def _head_or_none(
        self, client: Any, physical: str, *, version_id: str | None = None
    ) -> dict[str, Any] | None:
        extra = {"VersionId": version_id} if version_id is not None else {}
        try:
            head = await client.head_object(Bucket=self._settings.bucket, Key=physical, **extra)
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            raise
        return dict(head)

    async def _key_versions(self, client: Any, physical: str) -> list[dict[str, Any]]:
        """Every non-delete-marker VERSION of exactly ``physical``, newest first.

        ``Prefix`` is a PREFIX, not an exact match — listing prefix ``k`` also
        returns ``k2`` — so entries are filtered to an exact key equality.
        Versions of one key can straddle pagination boundaries, hence the full
        page-walk before filtering.

        ``NoSuchBucket`` propagates: this is the first server call on every
        read/write path, which is what makes a typo'd bucket fail loud instead
        of masquerading as an empty store.

        Capped at ``max_versions_per_key`` and raising
        :class:`TooManyObjectVersionsError` on breach — see
        :meth:`_check_version_cap`. The check is INSIDE the page loop, so the
        walk stops as soon as MATCHING versions breach the cap rather than
        collecting the whole pile first.

        What the cap bounds is the ``HeadObject`` fan-out, NOT the
        ``ListObjectVersions`` paging. The counter only counts versions of
        exactly ``physical``, while pagination runs over ``Prefix=physical``
        with no delimiter — so unrelated keys that merely SHARE the prefix are
        paged through in full without moving the counter. Bounding that would
        need a separate page/key budget, which is deliberately not attempted
        here: the expensive part (~2ms per version) is the per-version head.
        """
        paginator = client.get_paginator("list_object_versions")
        versions: list[dict[str, Any]] = []
        async for page in paginator.paginate(Bucket=self._settings.bucket, Prefix=physical):
            versions.extend(v for v in page.get("Versions", []) if v.get("Key") == physical)
            self._check_version_cap(physical, len(versions))
        return versions

    def _check_version_cap(self, physical: str, count: int) -> None:
        """Fail LOUD when one key carries more versions than we will scan.

        Resolving the locked version costs one ``HeadObject`` per version
        (~2ms measured). ``s3:PutObject`` is a permission the deployment MUST
        grant and is NOT covered by the required deny-``s3:DeleteObject``
        bucket policy, so a holder can cheaply layer 100k unlocked versions
        onto a single checkpoint key. Uncapped, ``list_objects`` then spends
        minutes on that one key and ``verify_checkpoints`` stops completing:
        the tamper anchor is not falsified, it becomes UNVERIFIABLE, with
        nothing to make that fail visibly.

        Raising — never truncating — is the requirement. A truncated scan could
        skip the version that actually carries the lock and report an archived
        key as unprotected, which is a silent C10 downgrade rather than a loud
        availability fault.
        """
        cap = self._settings.max_versions_per_key
        if count <= cap:
            return
        raise TooManyObjectVersionsError(
            f"key '{self._logical(physical)}' has more than {cap} object versions; the adapter "
            "must HeadObject every version to find the one carrying the Object Lock, so an "
            "unbounded walk here would stall list_objects and stop verify_checkpoints "
            "completing. Refusing to scan rather than truncating (a truncated scan can miss "
            "the locked version and report an archived key as unprotected). Investigate who is "
            "layering versions onto this key; raise NE_STORAGE_S3_MAX_VERSIONS_PER_KEY only if "
            "the volume is legitimate."
        )

    async def _locked_version(
        self, client: Any, physical: str
    ) -> tuple[str, dict[str, Any]] | None:
        """The ARCHIVED version of ``physical``, or ``None`` if nothing is locked.

        This is the heart of the adapter's WORM correctness. S3 Object Lock
        protects a specific VERSION, never a key, so every retention decision
        must be made about the version that actually carries the lock — NOT
        about whatever version happens to be current. Reasoning about the
        current version (the old ``HeadObject`` approach) had two fatal
        consequences: a single out-of-band ``PutObject`` layered an unlocked
        version on top and permanently disabled the check for ALL callers, and
        a versionless ``DeleteObject`` layered a delete marker that hid the key
        from listings entirely.

        Which version counts as locked, and how locked versions are RANKED
        against each other, is :func:`_protection_rank` — shared with the twin
        walk in :meth:`list_objects` so the two cannot drift. In short: a legal
        hold DOMINATES any dated retention (a hold is indefinite and outlasts
        every finite expiry), and among versions of equal hold status the
        LATEST active ``RetainUntilDate`` wins.

        That ordering is load-bearing beyond blocking. ``put``/``delete`` are
        refused whenever ANY version is locked, so they are correct under any
        ranking — but ``get()`` returns the SELECTED version's BYTES and
        ``stat()`` reports the SELECTED version's expiry. This code previously
        ranked a dated lock ABOVE a hold-only one, and documented that as
        intended on the grounds that a date "carries strictly more information
        to report". It is backwards, and it made ``get()`` hand back a
        different version's payload than the one under indefinite hold (wrong
        bytes for an audit-checkpoint verifier reading through ``get()``) and
        made ``stat()`` advertise a finite ``worm_retain_until`` — implying
        protection ENDS on that date — while another version was held with no
        end at all. Both UNDER-report the guarantee.

        Ties resolve to the FIRST version seen, and S3 lists versions
        newest-first, so an exact rank tie resolves to the NEWEST version. See
        :func:`_protection_rank` for why that is deterministic here (it was
        probed live, not assumed) and for the tie-break the Azure adapter needs
        instead. That comparison is deliberately NOT restated here — an earlier
        draft asserted a cross-reference to a sibling module that does not
        exist in this repo, which is an unverifiable claim of exactly the kind
        this change was written to stop shipping.
        """
        now = self._now()
        best: tuple[str, dict[str, Any]] | None = None
        best_rank: tuple[int, datetime] | None = None
        for version in await self._key_versions(client, physical):
            version_id = version.get("VersionId")
            if not version_id:
                continue
            head = await self._head_or_none(client, physical, version_id=version_id)
            if head is None:  # raced a concurrent version delete
                continue
            rank = _protection_rank(head, now)
            if rank is None:
                continue
            if best_rank is None or rank > best_rank:
                best, best_rank = (str(version_id), head), rank
        return best

    async def _ensure_object_lock_enabled(self, client: Any) -> None:
        """Fail loud, with the REAL reason, if the bucket has no Object Lock.

        Without this precondition the failure mode on the exact path C10
        depends on was either a raw ``KeyError('VersionId')`` (an unversioned
        bucket returns no VersionId) or a FABRICATED
        ``WormViolationError('... lost a race against a concurrent archive()')``
        — a lying diagnosis for a permanent, unfixable-in-place misconfiguration.
        """
        if self._object_lock_verified:
            return
        bucket = self._settings.bucket
        try:
            resp = await client.get_object_lock_configuration(Bucket=bucket)
        except ClientError as exc:
            if _error_code(exc) == "AccessDenied":
                # The credential may not READ the lock configuration. Distinct
                # from "the bucket has no Object Lock", and previously
                # re-raised as a raw ClientError from the top of EVERY
                # archive() — see ObjectLockPermissionError for the verbatim
                # repro against the documented IAM list.
                raise ObjectLockPermissionError(
                    f"AccessDenied reading the Object Lock configuration of bucket '{bucket}'; "
                    "archive() calls GetObjectLockConfiguration on every invocation, so the "
                    "checkpoint role MUST hold 's3:GetBucketObjectLockConfiguration' (bucket-"
                    "scoped). Without it put() still succeeds and archive() always fails, so "
                    "checkpoint objects land UNLOCKED. Add the action to the role policy — see "
                    "packages/ne-storage/README.md and docs/DEPLOYMENT.md."
                ) from exc
            if _error_code(exc) in _OBJECT_LOCK_MISSING_CODES:
                raise ObjectLockNotEnabledError(
                    f"bucket '{bucket}' does not have S3 Object Lock enabled; Object Lock can "
                    "only be enabled at bucket creation (ObjectLockEnabledForBucket=True) and "
                    "cannot be retrofitted — recreate the bucket (see "
                    "packages/ne-storage/README.md)"
                ) from exc
            raise
        enabled = (resp.get("ObjectLockConfiguration") or {}).get("ObjectLockEnabled")
        if enabled != "Enabled":
            raise ObjectLockNotEnabledError(
                f"bucket '{bucket}' reports ObjectLockEnabled={enabled!r} (expected 'Enabled'); "
                "S3 Object Lock is required for archive() and cannot be retrofitted onto an "
                "existing bucket — recreate the bucket (see packages/ne-storage/README.md)"
            )
        self._object_lock_verified = True

    async def _stat_from_head(self, key: str, physical: str, head: dict[str, Any]) -> ObjectStat:
        return ObjectStat(
            key=key,
            size=int(head["ContentLength"]),
            content_type=head.get("ContentType") or "application/octet-stream",
            metadata=dict(head.get("Metadata") or {}),
            # COMPLIANCE only: `worm_retain_until` is a claim that these bytes
            # cannot be destroyed before that instant, and a GOVERNANCE lock
            # does not support that claim. Expiry is deliberately NOT filtered
            # here — MemoryObjectStore keeps reporting an elapsed
            # `worm_retain_until` too, and matching the reference adapter
            # matters more than tidiness.
            worm_retain_until=_compliance_retain_until(head),
        )

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectStat:
        physical = self._physical(key)
        async with self._client() as client:
            # Keyed on the ARCHIVED version, not the current one: otherwise a
            # single layered unlocked version disables this check forever.
            locked = await self._locked_version(client, physical)
            if locked is not None:
                raise WormViolationError(f"key '{key}' is {_lock_reason(locked[1])}")
            await client.put_object(
                Bucket=self._settings.bucket,
                Key=physical,
                Body=data,
                ContentType=content_type,
                Metadata=dict(metadata or {}),
            )
            new_head = await self._head_or_none(client, physical)
            if new_head is None:  # pragma: no cover - just written, must exist
                raise ObjectNotFoundError(f"no object at key '{key}' immediately after put")
            return await self._stat_from_head(key, physical, new_head)

    async def get(self, key: str) -> bytes:
        physical = self._physical(key)
        async with self._client() as client:
            # An archived key reads back the ARCHIVED bytes. Reading the current
            # version would return an attacker's layered payload (or 404 behind
            # a delete marker) while the WORM-protected original stayed
            # unreachable through this API — "archived to WORM" has to imply
            # recoverable, not merely detectable-as-tampered.
            locked = await self._locked_version(client, physical)
            extra = {"VersionId": locked[0]} if locked is not None else {}
            try:
                resp = await client.get_object(Bucket=self._settings.bucket, Key=physical, **extra)
            except ClientError as exc:
                if _is_not_found(exc):
                    raise ObjectNotFoundError(f"no object at key '{key}'") from exc
                raise
            async with resp["Body"] as stream:
                return await stream.read()

    async def stat(self, key: str) -> ObjectStat:
        physical = self._physical(key)
        async with self._client() as client:
            locked = await self._locked_version(client, physical)
            if locked is not None:
                return await self._stat_from_head(key, physical, locked[1])
            head = await self._head_or_none(client, physical)
            if head is None:
                raise ObjectNotFoundError(f"no object at key '{key}'")
            return await self._stat_from_head(key, physical, head)

    async def delete(self, key: str) -> None:
        physical = self._physical(key)
        async with self._client() as client:
            locked = await self._locked_version(client, physical)
            if locked is not None:
                raise WormViolationError(f"key '{key}' is {_lock_reason(locked[1])}")
            head = await self._head_or_none(client, physical)
            if head is None:
                return  # already absent — matches MemoryObjectStore's dict.pop(default)
            await client.delete_object(Bucket=self._settings.bucket, Key=physical)

    async def list_objects(self, prefix: str = "") -> Sequence[ObjectStat]:
        full_prefix = self._physical(prefix)
        async with self._client() as client:
            # ListObjectVersions, NOT ListObjectsV2. V2 enumerates CURRENT
            # versions only, so any key whose current version is a delete
            # marker vanishes from the listing — and S3 NEVER blocks a
            # versionless DeleteObject, not even in COMPLIANCE mode. That made
            # one `s3:DeleteObject` call enough to hide an archived audit
            # checkpoint from verify_checkpoints, which then reported
            # valid=True/checked=0: a clean green over a destroyed anchor.
            # Seeing through delete markers is defence-in-depth for the
            # deny-s3:DeleteObject bucket policy documented in README.md.
            paginator = client.get_paginator("list_object_versions")
            versions_by_key: dict[str, list[dict[str, Any]]] = {}
            latest_is_marker: dict[str, bool] = {}
            async for page in paginator.paginate(Bucket=self._settings.bucket, Prefix=full_prefix):
                for version in page.get("Versions", []):
                    physical_key = str(version["Key"])
                    versions_by_key.setdefault(physical_key, []).append(version)
                    latest_is_marker.setdefault(physical_key, False)
                    if version.get("IsLatest"):
                        latest_is_marker[physical_key] = False
                for marker in page.get("DeleteMarkers", []):
                    physical_key = str(marker["Key"])
                    versions_by_key.setdefault(physical_key, [])
                    if marker.get("IsLatest"):
                        latest_is_marker[physical_key] = True
                    else:
                        latest_is_marker.setdefault(physical_key, False)

            now = self._now()
            out: list[ObjectStat] = []
            for physical in sorted(versions_by_key):
                logical = self._logical(physical)
                locked: dict[str, Any] | None = None
                locked_rank: tuple[int, datetime] | None = None
                # N+1 HeadObject: ListObjectVersions returns neither Lock
                # retention nor legal-hold status nor content-type, so each
                # VERSION is head-ed. Capped per key (see _check_version_cap):
                # uncapped, one key with a large layered version pile stalls
                # the whole verification listing.
                self._check_version_cap(physical, len(versions_by_key[physical]))
                for version in versions_by_key[physical]:
                    version_id = version.get("VersionId")
                    if not version_id:
                        continue
                    head = await self._head_or_none(client, physical, version_id=str(version_id))
                    if head is None:
                        continue
                    # Same ranking as _locked_version, via the same helper: a
                    # legal hold DOMINATES a dated retention (it is indefinite
                    # and outlasts every finite expiry), then latest expiry
                    # wins. A held key must stay LISTED even if a delete marker
                    # is on top, and the stat it reports must come from the
                    # MOST protected version, not merely a protected one.
                    rank = _protection_rank(head, now)
                    if rank is None:
                        continue
                    if locked_rank is None or rank > locked_rank:
                        locked, locked_rank = head, rank

                if locked is not None:
                    # A locked key is ALWAYS listed, and its stat comes from the
                    # locked version — never from a layered current version.
                    out.append(await self._stat_from_head(logical, physical, locked))
                    continue
                if latest_is_marker.get(physical, False):
                    # Legitimately deleted (delete marker on top, no retention
                    # anywhere): stays hidden. Pinned by the contract kit's
                    # test_deleted_key_absent_from_listing.
                    continue
                head = await self._head_or_none(client, physical)
                if head is None:
                    continue  # raced a concurrent delete between LIST and HEAD
                out.append(await self._stat_from_head(logical, physical, head))
            return out

    async def archive(self, key: str, *, retain_until: datetime) -> ArchiveReceipt:
        requested = _normalize_retain_until(retain_until)
        physical = self._physical(key)
        now = self._now()
        # DECIDED (NE-S3-OBJECTSTORE, 2026-07-19): archive() deliberately does
        # NOT reject a non-future retain_until locally. The frozen contract kit's
        # ObjectStoreContractKit.test_worm_retention_cannot_be_shortened requires
        # archive(<locked key>, retain_until=<past>) to raise WormViolationError,
        # which does not subclass ValueError — a local ValueError would break it,
        # and narrowing a frozen-contract test to suit one adapter is not on the
        # table. MemoryObjectStore performs no futurity validation either, so
        # having none here MATCHES the reference adapter rather than diverging
        # from it. The real defect in that finding — a fabricated "lost a race"
        # diagnosis reported for a permanent config fault — is fixed in the
        # except-clause below and in _is_worm_denial.
        effective = requested
        bypass_governance = False
        async with self._client() as client:
            await self._ensure_object_lock_enabled(client)

            # Resolve WHICH version this archive() will write to, first. The
            # locked version wins when there is one (an extension must land on
            # the already-archived version, never on whatever version happens
            # to be current); otherwise it is the current version.
            locked = await self._locked_version(client, physical)
            if locked is not None:
                version_id, target_head = locked
            else:
                head = await self._head_or_none(client, physical)
                if head is None:
                    raise ObjectNotFoundError(f"no object at key '{key}'")
                raw_version_id = head.get("VersionId")
                if raw_version_id is None:
                    # Belt-and-braces: _ensure_object_lock_enabled should already
                    # have caught this, but an unversioned bucket returning no
                    # VersionId must never surface as a raw KeyError.
                    raise ObjectLockNotEnabledError(
                        f"bucket '{self._settings.bucket}' returned no VersionId for key "
                        f"'{key}'; the bucket is not versioned, so S3 Object Lock cannot be "
                        "applied — Object Lock must be enabled at bucket creation and cannot "
                        "be retrofitted (see packages/ne-storage/README.md)"
                    )
                version_id = str(raw_version_id)
                target_head = head

            # The GOVERNANCE gate runs on the TARGET VERSION regardless of which
            # branch above chose it, and BEFORE the COMPLIANCE comparison below.
            #
            # Round-4 P2-1: it used to live inside the `else` (unlocked) branch
            # only, which made it unreachable for a version carrying BOTH an
            # active GOVERNANCE retention AND a legal hold. The hold makes
            # _locked_version return that version, so archive() took the locked
            # branch, found _compliance_retain_until(...) is None (the mode is
            # GOVERNANCE, not COMPLIANCE), treated it as a bare hold-only lock,
            # and fell through intending to write a COMPLIANCE retention SHORTER
            # than the standing GOVERNANCE one — with no opt-in gate, no
            # max(requested, governance) clamp and no BypassGovernanceRetention.
            # Nothing but the server refusing that write stopped it, and the
            # refusal was then reported as "possible causes include a concurrent
            # archive() race" for what is a permanent, deterministic
            # configuration state. Hoisting the gate routes the combination
            # through the same clamp and the same typed error as a plain
            # GOVERNANCE lock, and makes the opt-in upgrade REACHABLE for it
            # (before, an opt-in operator could not archive such a key at all).
            #
            # Ordering is safe because ObjectLockMode is a single-valued field:
            # a version is GOVERNANCE or COMPLIANCE, never both, so an active
            # `governance` here implies `existing` below is None and vice versa.
            #
            # DECIDED (NE-S3-OBJECTSTORE round 3): archive() over an ACTIVE
            # GOVERNANCE lock is OPT-IN and REFUSED BY DEFAULT.
            #
            # The upgrade itself is a strengthening — a GOVERNANCE lock is
            # not a WORM guarantee at all (any principal holding
            # s3:BypassGovernanceRetention deletes or shortens it), and the
            # effective date is clamped to max(requested, governance) so it
            # can never trade a longer weak lock for a shorter strong one.
            #
            # But that clamp is a READ-then-WRITE and S3 Object Lock has NO
            # compare-and-set, so it loses a concurrent retention
            # extension outright:
            #
            #   1. version carries GOVERNANCE until day 30
            #   2. caller A heads it, computes effective = day 30
            #   3. caller B extends the GOVERNANCE retention to day 365
            #   4. caller A writes COMPLIANCE until day 30 — B's day-365
            #      retention is GONE, and COMPLIANCE means nobody can put
            #      it back before day 30
            #
            # The race cannot be closed, only avoided or disclosed. So the
            # DEFAULT refuses and never touches the existing lock, which
            # makes the default path incapable of weakening anything; the
            # opt-in restores the upgrade for the deliberate migration
            # case, documented as unsafe under concurrent retention
            # mutation. See README.md "GOVERNANCE → COMPLIANCE upgrade".
            governance = _governance_retain_until(target_head)
            if _is_retention_active(governance, now):
                if not self._settings.allow_governance_upgrade:
                    raise GovernanceUpgradeRefusedError(
                        f"key '{key}' already carries a GOVERNANCE-mode Object Lock until "
                        f"{governance}; archive() will not upgrade it to COMPLIANCE by "
                        "default. The upgrade reads that date and writes back "
                        "max(requested, governance), and Object Lock has no compare-and-set, "
                        "so a retention extension racing between the read and the write is "
                        "silently lost — permanently, since COMPLIANCE cannot be shortened. "
                        "Set NE_STORAGE_S3_ALLOW_GOVERNANCE_UPGRADE=true to opt in once no "
                        "other writer is mutating retention on this bucket, or clear the "
                        "GOVERNANCE lock first and re-archive."
                    )
                # Empirical (MinIO RELEASE.2025-09-07T16-13-09Z): a
                # GOVERNANCE -> COMPLIANCE PutObjectRetention is DENIED
                # (InvalidRequest/400) without BypassGovernanceRetention,
                # even when the new date is LATER, so the flag is required
                # for the upgrade.
                #
                # The guarantee about that flag is NARROW, and an earlier
                # version of this comment overstated it: it claimed the
                # flag is sent only to a GOVERNANCE-locked object. It is
                # sent only on this CODE PATH, which is a static-code
                # argument that concurrency defeats — between the
                # HeadObject above and the PutObjectRetention below,
                # another actor can upgrade this very version to
                # COMPLIANCE, and the flag then lands on a
                # COMPLIANCE-locked object. What actually holds: the flag
                # is NOT a shortening primitive. S3 refuses to shorten a
                # COMPLIANCE retention with or without it, so the worst
                # case is a denied call, not a weakened lock. The
                # permission is still not granted by default — see
                # docs/DEPLOYMENT.md.
                bypass_governance = True
                effective = max(requested, governance)

            # The standing COMPLIANCE retention on the target version, if any.
            # NOT target_head["ObjectLockRetainUntilDate"]: a version locked
            # ONLY by a legal hold carries no retention date at all, and a bare
            # subscript there was a KeyError waiting on that path. `None` here
            # means "no COMPLIANCE retention to extend or shorten" — either an
            # unlocked version, or a hold-only lock, in which case the requested
            # retention is applied as-is (strictly additive protection: the hold
            # stays in force independently of the retention).
            existing = _compliance_retain_until(target_head)
            if existing is not None:
                if requested < existing:
                    raise WormViolationError(
                        f"key '{key}' is under WORM retention until {existing}; retention may "
                        f"only be extended, not shortened to {requested}"
                    )
                if requested == existing:
                    # Idempotent — no server call, matching MemoryObjectStore.
                    return ArchiveReceipt(key=key, retain_until=existing, archived_at=now)
                # An EXTENSION applies to the already-archived version, never to
                # whatever version happens to be current.

            try:
                await client.put_object_retention(
                    Bucket=self._settings.bucket,
                    Key=physical,
                    VersionId=version_id,
                    Retention={"Mode": COMPLIANCE_MODE, "RetainUntilDate": effective},
                    **({"BypassGovernanceRetention": True} if bypass_governance else {}),
                )
            except ClientError as exc:
                if _is_worm_denial(exc):
                    cause = (
                        "the credential may lack s3:BypassGovernanceRetention, which is "
                        "required to upgrade an existing GOVERNANCE lock to COMPLIANCE"
                        if bypass_governance
                        else "possible causes include a concurrent archive() race"
                    )
                    raise WormViolationError(
                        f"key '{key}' retention update to {effective} was denied by the "
                        f"server on the locked version (COMPLIANCE WORM); {cause}"
                    ) from exc
                raise
            return ArchiveReceipt(key=key, retain_until=effective, archived_at=now)
