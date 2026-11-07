# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Layered settings for the production ObjectStore adapters (``s3``, ``azure_blob``).

All fields are environment-driven (``NE_STORAGE_S3_*`` / ``NE_STORAGE_AZURE_BLOB_*``) so pointing
NE-AUDIT-CRON's checkpoint loop at real WORM storage is a **config change, not a
code change**: set ``NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE=s3`` plus this
block. Credentials are OPTIONAL by design — empty ``access_key_id`` /
``secret_access_key`` fall through to botocore's default credential chain (IAM
instance role / IRSA / environment / shared config), which is the recommended
production posture; setting both is only for dev/CI where a fixed key pair is
easier to reason about (e.g. the MinIO test server).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings


class S3ObjectStoreSettings(NeSettings):
    """Layered settings for :class:`~ne.storage.s3.S3ObjectStore`.

    ``endpoint_url`` empty means "use AWS's default regional endpoints" — the
    production posture for real AWS S3. A non-empty value is validated as an
    in-VPC endpoint (:func:`ne.net.ssrf.validate_in_vpc_endpoint`) at adapter
    construction time, and an EMPTY ``endpoint_url`` under ``AIR_GAP`` fails
    closed with :class:`~ne.net.airgap.AirGapViolationError` (the default AWS
    endpoint is public-internet egress).
    """

    model_config = SettingsConfigDict(
        env_prefix="NE_STORAGE_S3_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    endpoint_url: str = ""
    region: str = "us-east-1"
    bucket: str = ""
    access_key_id: str = ""
    # SecretStr, not str: pydantic renders it as ``**********`` in repr()/str(),
    # so a settings object landing in a traceback frame, a structlog event or a
    # `print(settings)` during triage cannot leak the credential. Unwrapped with
    # .get_secret_value() at exactly one place — the client construction in
    # ne.storage.s3.S3ObjectStore._client.
    secret_access_key: SecretStr = SecretStr("")
    # Optional STS session token, for a temporary credential (AssumeRole /
    # AssumeRoleWithWebIdentity). Only meaningful alongside an explicit
    # access_key_id/secret_access_key pair; the default credential chain
    # (IAM role / IRSA) sources and refreshes its own token and needs nothing
    # here. Also what the restricted-credential live test uses to drive the
    # adapter with EXACTLY the documented IAM action list.
    # SecretStr for the same reason as secret_access_key: an STS session token
    # is a bearer credential in its own right — anyone holding it can act as the
    # assumed role until it expires — so it must not render in a repr either.
    session_token: SecretStr = SecretStr("")
    key_prefix: str = ""
    force_path_style: bool = True
    # Comma-separated operator allowlist of non-obviously-in-VPC endpoint hosts
    # (e.g. a private S3-compatible appliance on corporate DNS). Passed through
    # to validate_in_vpc_endpoint(allowed_hosts=...).
    allowed_hosts: str = ""
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 60.0
    # Ceiling on how many VERSIONS of a single key the adapter will scan when
    # resolving which one carries the Object Lock. Every version costs one
    # HeadObject (~2ms), and `s3:PutObject` — a permission the deployment MUST
    # grant — lets any holder layer unlimited unlocked versions onto one
    # checkpoint key. Without a cap that turns list_objects into an unbounded
    # stall and makes verify_checkpoints stop completing: the tamper anchor
    # becomes UNVERIFIABLE rather than falsified, and silently so. Breaching
    # this cap raises TooManyObjectVersionsError — failing loud, never
    # truncating the scan (a truncated scan could miss the locked version and
    # report an archived key as unprotected, which is strictly worse).
    max_versions_per_key: int = 1000
    # Opt-in for archive() upgrading a pre-existing GOVERNANCE-mode lock to
    # COMPLIANCE. DEFAULT FALSE — the upgrade reads the standing GOVERNANCE
    # retention and then writes a COMPLIANCE retention clamped to
    # max(requested, governance), and Object Lock offers no compare-and-set, so
    # a concurrent extension of the GOVERNANCE retention between those two
    # calls is silently lost. Refusing by default means the default path can
    # never weaken an existing lock. See packages/ne-storage/README.md
    # "GOVERNANCE → COMPLIANCE upgrade".
    allow_governance_upgrade: bool = False


class AzureBlobObjectStoreSettings(NeSettings):
    """Layered settings for :class:`~ne.storage.azure_blob.AzureBlobObjectStore`.

    Unlike S3 (where an empty ``endpoint_url`` means "AWS's default regional
    endpoints"), Azure has NO account-less default endpoint: ``account_url``
    and ``container`` are both REQUIRED and the adapter fails loud at boot
    without them.

    C2 / AIR_GAP behavior, validated at adapter construction time:

    * A public Azure Blob FQDN (``*.blob.core.windows.net`` and the sovereign
      ``*.blob.core.usgovcloudapi.net`` / ``*.blob.core.chinacloudapi.cn``
      suffixes) is permitted when ``AIR_GAP`` is off — governed egress via the
      deployment's network-firewall FQDN allowlist, the same posture as S3's
      empty ``endpoint_url``.
    * Under ``AIR_GAP`` that same public FQDN FAILS CLOSED with
      :class:`~ne.net.airgap.AirGapViolationError` UNLESS the host is listed in
      ``allowed_hosts`` — the operator's explicit assertion that an Azure
      Private Endpoint resolves that FQDN privately in-VNet (Azure Private
      Endpoints keep the public FQDN, which is why an allowlist entry is the
      only way to express this).
    * Any NON-public-suffix host (custom domain, Azurite) goes through
      :func:`ne.net.ssrf.validate_in_vpc_endpoint` exactly as S3's
      ``endpoint_url`` does.

    ``account_key`` is OPTIONAL by design: empty falls through to
    ``azure.identity.aio.DefaultAzureCredential`` (AKS workload identity /
    managed identity / environment), the recommended production posture —
    the exact mirror of S3's empty-credentials-to-IAM-chain behavior. Set it
    only for dev or shared-key accounts.

    Pointing NE-AUDIT-CRON's checkpoint loop at Azure is then a **config
    change, not a code change**: set
    ``NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE=azure_blob`` plus this block.
    """

    model_config = SettingsConfigDict(
        env_prefix="NE_STORAGE_AZURE_BLOB_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    account_url: str = ""
    container: str = ""
    # SecretStr, not str, for the same reason as S3's ``secret_access_key``:
    # an Azure shared account key is a full-control credential and pydantic
    # renders a SecretStr as ``**********`` in repr()/str(), so it cannot leak
    # into a traceback, a settings dump or a structured log line. NB SecretStr
    # defines no ``__bool__`` — ``if settings.account_key:`` is True even for
    # SecretStr("") — so every consumer MUST call ``.get_secret_value()``
    # BEFORE testing truthiness or the empty-key fall-through to
    # DefaultAzureCredential silently breaks. See
    # AzureBlobObjectStore._container, which is the sole consumer.
    account_key: SecretStr = SecretStr("")
    key_prefix: str = ""
    # Bound on how many VERSIONS of a single blob the adapter will walk when
    # resolving which version actually carries the Locked immutability policy.
    # Azure returns the policy inline on the versions listing (no per-version
    # round trip, unlike S3's HeadObject fan-out), so the cost profile is
    # lighter — but the paging is still unbounded and any writer holding blob-
    # write permission can layer versions onto one checkpoint key. Breaching
    # this cap raises TooManyBlobVersionsError: failing loud, never truncating
    # (a truncated walk could miss the Locked version and report an archived
    # key as unprotected, which is strictly worse than an availability fault).
    max_versions_per_key: int = 1000
    # Comma-separated operator allowlist. Two distinct uses (see the class
    # docstring): asserting a public Azure FQDN is Private-Endpoint-backed
    # under AIR_GAP, and passing non-Azure hosts through
    # validate_in_vpc_endpoint(allowed_hosts=...).
    allowed_hosts: str = ""
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 60.0
