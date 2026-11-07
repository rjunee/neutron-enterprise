# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""JWKS anti-amplification hardening (NE-FIX-IDENTITY P2-6).

Only the below-the-seam sync JSON fetch (``_fetch_json_sync``) is monkeypatched,
mirroring ``test_jwks_source.py``. These assert the forced-refresh cooldown,
negative kid cache, lock-free fresh-cache reads, and single-flight refresh — the
defenses against an unauthenticated unknown-kid spray stalling all auth.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from ne.identity.config import OidcConfig
from ne.identity.errors import JwksError
from ne.identity.jwks import HttpJwksSource

from .conftest import DEFAULT_KID, TEST_AUDIENCE, TEST_ISSUER, run


def _config(**overrides: Any) -> OidcConfig:
    base: dict[str, Any] = {
        "issuer": TEST_ISSUER,
        "audience": TEST_AUDIENCE,
        "jwks_uri": "https://idp.ne.test/certs",
    }
    base.update(overrides)
    return OidcConfig(**base)


def test_unknown_kid_spray_triggers_exactly_one_refresh_then_fails_closed(
    jwks: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = HttpJwksSource(_config())  # default 30s cooldown
    calls = {"n": 0}

    def fake_fetch(url: str) -> dict[str, Any]:
        calls["n"] += 1
        return jwks

    monkeypatch.setattr(source, "_fetch_json_sync", fake_fetch)

    # Warm the cache with a known kid: exactly one fetch.
    run(source.get_signing_key(DEFAULT_KID))
    assert calls["n"] == 1

    # Spray 10 distinct unknown kids: exactly ONE forced refresh for the whole
    # spray, then fail-closed for the rest of the cooldown window.
    for i in range(10):
        with pytest.raises(JwksError):
            run(source.get_signing_key(f"spray-{i}"))
    assert calls["n"] == 2


def test_negative_cached_kid_fails_without_second_refresh(
    jwks: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = HttpJwksSource(_config())
    calls = {"n": 0}

    def fake_fetch(url: str) -> dict[str, Any]:
        calls["n"] += 1
        return jwks

    monkeypatch.setattr(source, "_fetch_json_sync", fake_fetch)

    run(source.get_signing_key(DEFAULT_KID))  # warm (fetch 1)
    with pytest.raises(JwksError):
        run(source.get_signing_key("gone"))  # forced refresh (fetch 2), miss noted
    after_first_miss = calls["n"]
    assert after_first_miss == 2

    with pytest.raises(JwksError):
        run(source.get_signing_key("gone"))  # negative-cached: no fetch
    assert calls["n"] == after_first_miss


def test_cooldown_zero_disables_rate_limit(
    jwks: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = HttpJwksSource(_config(jwks_refresh_cooldown_seconds=0.0))
    calls = {"n": 0}

    def fake_fetch(url: str) -> dict[str, Any]:
        calls["n"] += 1
        return jwks

    monkeypatch.setattr(source, "_fetch_json_sync", fake_fetch)

    run(source.get_signing_key(DEFAULT_KID))  # warm (fetch 1)
    with pytest.raises(JwksError):
        run(source.get_signing_key("miss-a"))  # forces a refresh (fetch 2)
    with pytest.raises(JwksError):
        run(source.get_signing_key("miss-b"))  # forces another refresh (fetch 3)
    assert calls["n"] == 3


def test_fresh_cache_hit_never_touches_the_lock(
    jwks: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = HttpJwksSource(_config())

    def fake_fetch(url: str) -> dict[str, Any]:
        return jwks

    monkeypatch.setattr(source, "_fetch_json_sync", fake_fetch)

    async def scenario() -> None:
        await source.get_signing_key(DEFAULT_KID)  # populate the fresh cache
        # Hold the source lock EXTERNALLY: a fresh-cache hit must still resolve.
        # A reverted lock-everything implementation would deadlock here.
        async with source._lock:
            key = await asyncio.wait_for(source.get_signing_key(DEFAULT_KID), timeout=0.5)
        assert key.key_id == DEFAULT_KID

    run(scenario())


def test_stale_cache_refresh_is_single_flight(
    jwks: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    source = HttpJwksSource(config)
    calls = {"n": 0}

    def fake_fetch(url: str) -> dict[str, Any]:
        calls["n"] += 1
        time.sleep(0.05)  # runs in asyncio.to_thread; slow enough to overlap
        return jwks

    monkeypatch.setattr(source, "_fetch_json_sync", fake_fetch)

    async def scenario() -> None:
        await source.get_signing_key(DEFAULT_KID)  # warm (fetch 1)
        # Force the cache stale so concurrent lookups race the refresh.
        source._cached_at = time.monotonic() - config.jwks_cache_ttl_seconds - 1
        results = await asyncio.gather(*(source.get_signing_key(DEFAULT_KID) for _ in range(5)))
        assert all(k.key_id == DEFAULT_KID for k in results)

    run(scenario())
    assert calls["n"] == 2  # single-flight: exactly one refresh for the 5 lookups
