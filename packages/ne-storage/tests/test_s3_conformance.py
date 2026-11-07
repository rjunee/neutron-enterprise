# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ObjectStoreContractKit run against a REAL S3-compatible server — LIVE.

Skips cleanly when the test server is unreachable. No HTTP mocking here,
ever: this proves the real S3ObjectStore transport against a real Object
Lock-capable server. MinIO is the CHOSEN test server (see
packages/ne-storage/README.md "Test server" / NOTICE): SeaweedFS's S3 gateway
was evaluated and DISQUALIFIED — seaweedfs/seaweedfs#8350 and #7194 confirm its
COMPLIANCE-mode Object Lock accepts a retention write but does not actually
block deletes, which would make this whole conformance suite vacuous.

Start it with: ``docker compose -f deploy/compose/dev/compose.yaml --profile
worm up -d minio`` (or the bare ``docker run`` in packages/ne-storage/README.md
"Test server").

Each test gets a FRESH, Object-Lock-enabled bucket (``create_store``). There is
no cleanup: COMPLIANCE-locked objects are undeletable by design, and the test
server itself is ephemeral (a fresh container / fresh named volumes wipes
everything) — deleting objects here would be both wrong (defeats the WORM
proof) and pointless (nothing persists past the container's lifetime anyway).

``create_store_at`` deliberately raises ``NotImplementedError``: there is no
way to inject a fake clock into a real S3 server's retention comparisons, so
the kit's ``test_worm_expires`` self-skips for this adapter. Real-time expiry
(the same semantics, proven honestly with an actual 3-second wait) is covered
by ``test_s3_worm_live.py::test_worm_expiry_realtime`` instead — CI deselects
the kit-skipped test explicitly so "must run, not skip" stays strict for every
OTHER promoted test in this file.
"""

from __future__ import annotations

import asyncio
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from pydantic import SecretStr

from ne.contracts.storage import ObjectStore
from ne.contracts.testing.kits.object_store import ObjectStoreContractKit
from ne.storage.config import S3ObjectStoreSettings
from ne.storage.s3 import S3ObjectStore

_ENDPOINT = os.environ.get("NE_TEST_S3_ENDPOINT", "http://localhost:9000")
_ACCESS_KEY = os.environ.get("NE_TEST_S3_ACCESS_KEY", "ne-test")
_SECRET_KEY = os.environ.get("NE_TEST_S3_SECRET_KEY", "ne-test-secret")
# NE-WORM-LIVE: the owner-gated AWS leg authenticates with OIDC-federated
# TEMPORARY credentials (an `ASIA…` key). A temporary key is only valid when
# its session token accompanies every request; omit the token and real AWS
# rejects it with `InvalidAccessKeyId: does not exist in our records` — which
# is exactly how this suite failed against real S3 while passing on MinIO,
# whose permanent creds need no token. Defaults to "" so the MinIO CI lane is
# byte-identical (an empty token is passed as None below, i.e. not sent).
_SESSION_TOKEN = os.environ.get("NE_TEST_S3_SESSION_TOKEN", "")
# NE-WORM-LIVE: default is 365 days — byte-identical to the literal the kit
# used before the `worm_retention` hook, so the MinIO CI lane is unchanged.
# The owner-gated `worm-live-proof` AWS leg shortens it to minutes: a real
# COMPLIANCE lock cannot be deleted until it expires, so a year-scale kit
# retention would leave an undeletable bucket behind on every run.
_KIT_RETENTION = timedelta(
    seconds=float(os.environ.get("NE_TEST_S3_KIT_RETENTION_SECONDS", str(365 * 86400)))
)
# Real AWS is not `localhost`; the C2 allowed-hosts gate would refuse the
# endpoint at construction time (README "C2 / AIR_GAP behavior").
_ALLOWED_HOSTS = os.environ.get("NE_TEST_S3_ALLOWED_HOSTS", "localhost")


def _s3_available() -> bool:
    try:
        urllib.request.urlopen(f"{_ENDPOINT}/minio/health/live", timeout=1.0)
        return True
    except urllib.error.HTTPError:
        # Any HTTP response (even non-200) proves the server is reachable.
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


def _raw_client() -> Any:
    # Untyped return (aiobotocore ships no py.typed / stubs): without this,
    # pyright resolves dynamically-generated client methods (create_bucket,
    # put_object, ...) through AioBaseClient.__getattr__, whose sole
    # unconditional `raise AttributeError` infers as NoReturn — making every
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
        aws_session_token=_SESSION_TOKEN or None,
        config=AioConfig(s3={"addressing_style": "path"}),
    )


def _make_bucket_and_store() -> S3ObjectStore:
    bucket = f"ne-kit-{uuid4().hex[:12]}"

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
    )
    return S3ObjectStore(settings)


class TestS3ObjectStoreConformance(ObjectStoreContractKit):
    worm_retention = _KIT_RETENTION

    def create_store(self) -> ObjectStore:
        return _make_bucket_and_store()

    def create_store_at(self, now: Callable[[], datetime]) -> ObjectStore:
        raise NotImplementedError(
            "no injectable clock for a real S3 server — WORM expiry is proven "
            "in test_s3_worm_live.py::test_worm_expiry_realtime instead"
        )
