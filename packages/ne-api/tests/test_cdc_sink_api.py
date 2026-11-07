# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CDC sink mount (NE-084) — fail-closed default, HMAC gate, governed ingest."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator

import pytest

from .conftest import AppBuilder, run

_SECRET = "whsec_" + base64.b64encode(b"cdc-webhook-key-32-bytes-long!!!!").decode()
_RAW = base64.b64decode(_SECRET[len("whsec_") :])
_TABLES = json.dumps(
    {
        "source_name": "debezium",
        "tables": {
            "public.customers": {
                "key_columns": ["id"],
                "content_columns": ["name", "email"],
                "acl": {"principal_column": "acl_group"},
            }
        },
    }
)


def _envelope(op: str, after: dict | None, before: dict | None = None) -> bytes:
    return json.dumps(
        {
            "before": before,
            "after": after,
            "source": {
                "connector": "postgresql",
                "db": "appdb",
                "schema": "public",
                "table": "customers",
            },
            "op": op,
            "ts_ms": 1,
        }
    ).encode()


def _sign(body: bytes, *, msg_id: str = "msg_1", ts: int | None = None) -> dict[str, str]:
    timestamp = str(int(time.time()) if ts is None else ts)
    sig = (
        "v1,"
        + base64.b64encode(
            hmac.new(_RAW, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
        ).decode()
    )
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": sig,
    }


def _enabled(make_app: AppBuilder):  # type: ignore[no-untyped-def]
    return make_app(cdc_webhook_secret=_SECRET, cdc_tables=_TABLES)


def test_cdc_mount_absent_by_default_404(make_app: AppBuilder) -> None:
    h = make_app()  # cdc off

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post("/cdc/v1/events", content=_envelope("c", {"id": 1}))
            assert r.status_code == 404

    run(_t())


def test_unauthenticated_post_401(make_app: AppBuilder) -> None:
    h = _enabled(make_app)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post("/cdc/v1/events", content=_envelope("c", {"id": 1}))
            assert r.status_code == 401
            assert r.json()["detail"] == "unauthorized"

    run(_t())


def test_bad_signature_401(make_app: AppBuilder) -> None:
    h = _enabled(make_app)

    async def _t() -> None:
        async with h.client() as client:
            body = _envelope("c", {"id": 1, "name": "Ada", "acl_group": "eng"})
            headers = _sign(body)
            headers["webhook-signature"] = "v1,deadbeef"
            r = await client.post("/cdc/v1/events", content=body, headers=headers)
            assert r.status_code == 401
            assert r.json()["detail"] == "unauthorized"

    run(_t())


def test_valid_signed_insert_ingests_acl_trimmed(make_app: AppBuilder) -> None:
    h = _enabled(make_app)

    async def _t() -> None:
        async with h.client() as client:
            body = _envelope(
                "c", {"id": 1, "name": "Ada Synthetic", "email": "a@x", "acl_group": "eng"}
            )
            r = await client.post("/cdc/v1/events", content=body, headers=_sign(body))
            assert r.status_code == 200, r.text
            assert r.json()["written"] == 1
            # It landed in the store with the row's derived ACL.
            assert len(h.chunk_store._chunks) == 1
            stored = next(iter(h.chunk_store._chunks.values()))
            assert set(stored.acl) == {"eng"}

            # CDC has not yet produced a current-source authority receipt. The
            # historical row ACL is retained in storage, but it cannot authorize
            # disclosure on the exact-current read surface.
            granted = await client.post(
                "/api/v1/brain/search",
                json={"query": "Ada", "top_k": 5},
                headers=h.bearer("user:ada", groups=["eng"]),
            )
            assert granted.status_code == 200
            assert granted.json()["citations"] == []

            forbidden = await client.post(
                "/api/v1/brain/search",
                json={"query": "Ada", "top_k": 5},
                headers=h.bearer("user:bob", groups=["sales"]),
            )
            assert forbidden.status_code == 200
            assert forbidden.json()["citations"] == []

    run(_t())


def test_signed_delete_removes_chunk(make_app: AppBuilder) -> None:
    h = _enabled(make_app)

    async def _t() -> None:
        async with h.client() as client:
            ins = _envelope("c", {"id": 1, "name": "Ada", "email": "a@x", "acl_group": "eng"})
            await client.post("/cdc/v1/events", content=ins, headers=_sign(ins))
            assert len(h.chunk_store._chunks) == 1
            dele = _envelope("d", None, before={"id": 1, "name": "Ada", "acl_group": "eng"})
            r = await client.post("/cdc/v1/events", content=dele, headers=_sign(dele))
            assert r.status_code == 200
            assert r.json()["removed"] == 1
            assert h.chunk_store._chunks == {}

    run(_t())


def test_unresolvable_acl_quarantined_non_leak(make_app: AppBuilder) -> None:
    h = _enabled(make_app)

    async def _t() -> None:
        async with h.client() as client:
            body = _envelope("c", {"id": 9, "name": "Cy", "email": "c@x", "acl_group": None})
            r = await client.post("/cdc/v1/events", content=body, headers=_sign(body))
            assert r.status_code == 200
            assert r.json()["written"] == 0
            assert r.json()["quarantined"] == 1
            # Nothing readable landed.
            assert h.chunk_store._chunks == {}

    run(_t())


def test_stale_timestamp_rejected_401(make_app: AppBuilder) -> None:
    h = _enabled(make_app)

    async def _t() -> None:
        async with h.client() as client:
            body = _envelope("c", {"id": 1, "name": "Ada", "acl_group": "eng"})
            r = await client.post(
                "/cdc/v1/events", content=body, headers=_sign(body, ts=int(time.time()) - 3600)
            )
            assert r.status_code == 401

    run(_t())


def test_oversize_body_413(make_app: AppBuilder) -> None:
    h = _enabled(make_app)

    async def _t() -> None:
        async with h.client() as client:
            oversize = b"x" * (8 * 1024 * 1024 + 1)
            r = await client.post("/cdc/v1/events", content=oversize, headers=_sign(oversize))
            assert r.status_code == 413

    run(_t())


def test_oversize_streamed_body_rejected_before_full_buffer(make_app: AppBuilder) -> None:
    # A chunked upload (no honest Content-Length) must still be rejected — the
    # running byte budget bounds memory even when the header cannot be trusted.
    h = _enabled(make_app)
    chunk = b"x" * (1024 * 1024)

    async def _stream() -> AsyncIterator[bytes]:
        for _ in range(9):  # 9 MiB > 8 MiB cap
            yield chunk

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/cdc/v1/events",
                content=_stream(),
                headers={
                    "webhook-id": "msg_1",
                    "webhook-timestamp": "1",
                    "webhook-signature": "v1,x",
                },
            )
            assert r.status_code == 413

    run(_t())


def test_signed_invalid_utf8_body_400_not_500(make_app: AppBuilder) -> None:
    # The HMAC is computed over raw bytes, so an authenticated-but-undecodable
    # body reaches the parser; it must be a 400 "malformed change event" (which
    # Debezium does retry, but the operator sees the poison event loudly) and
    # never an unhandled UnicodeDecodeError 500.
    h = _enabled(make_app)

    async def _t() -> None:
        async with h.client() as client:
            body = b'{"op": "c"\xff\xfe}'
            r = await client.post("/cdc/v1/events", content=body, headers=_sign(body))
            assert r.status_code == 400
            assert r.json()["detail"] == "malformed change event"

    run(_t())


def test_weak_or_malformed_secret_fails_loud_at_factory(make_app: AppBuilder) -> None:
    with pytest.raises(ValueError):
        make_app(cdc_webhook_secret="whsec_not valid base64!!!", cdc_tables=_TABLES)


def test_secret_without_tables_fails_loud(make_app: AppBuilder) -> None:
    with pytest.raises(ValueError):
        make_app(cdc_webhook_secret=_SECRET, cdc_tables="")


def test_secret_without_quarantine_path_fails_loud(make_app: AppBuilder) -> None:
    with pytest.raises(ValueError):
        make_app(cdc_webhook_secret=_SECRET, cdc_tables=_TABLES, cdc_quarantine_path="")
