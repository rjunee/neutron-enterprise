# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SCIM mount gating (NE-089) — fail-closed default, bearer auth, no-bypass."""

from __future__ import annotations

import pytest

from .conftest import AppBuilder, run

_TOKEN = "a" * 32


def test_scim_mount_absent_when_disabled(make_app: AppBuilder) -> None:
    h = make_app()  # scim_bearer_token default ""

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/scim/v2/ServiceProviderConfig")
            assert r.status_code == 404

    run(_t())


def test_missing_and_wrong_bearer_401_no_oracle(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)

    async def _t() -> None:
        async with h.client() as client:
            no_auth = await client.get("/scim/v2/ServiceProviderConfig")
            assert no_auth.status_code == 401
            assert no_auth.headers["www-authenticate"] == "Bearer"
            assert no_auth.json()["detail"] == "unauthorized"

            wrong = await client.get(
                "/scim/v2/ServiceProviderConfig", headers={"Authorization": "Bearer nope"}
            )
            assert wrong.status_code == 401
            assert wrong.json()["detail"] == "unauthorized"
            # No oracle: the missing-header and wrong-token bodies are identical.
            assert wrong.json() == no_auth.json()

    run(_t())


def test_correct_bearer_ok(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/scim/v2/ServiceProviderConfig",
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("application/scim+json")
            assert r.json()["patch"]["supported"] is True

    run(_t())


def test_scim_token_never_authenticates_api(make_app: AppBuilder) -> None:
    h = make_app(scim_bearer_token=_TOKEN)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {_TOKEN}"})
            assert r.status_code == 401

    run(_t())


def test_short_token_rejected_at_factory(make_app: AppBuilder) -> None:
    with pytest.raises(ValueError, match="16 characters"):
        make_app(scim_bearer_token="short")
