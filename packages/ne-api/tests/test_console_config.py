# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CONSOLE-ISSUER — the public runtime console-config endpoint.

``GET /config.json`` is an additive, unauthenticated, exact-match Route that
serves the deployment's PUBLIC OIDC discovery values (issuer/client_id/audience/
api_base) sourced from the SAME ``IdentitySettings.oidc`` the OIDC verifier
trusts (ONE issuer source of truth). It sits beside ``/healthz`` OUTSIDE the OIDC
wrap; the guarded alias ``/api/config.json`` stays 401. It NEVER returns a secret
(C12).
"""

from __future__ import annotations

from pathlib import Path

from starlette.routing import Mount, Route

from ne.contracts import reset_config_hash_for_testing

from .conftest import AUDIENCE, ISSUER, AppBuilder, get_state, run


def _dist(tmp_path: Path, *, config_json: str | None = None) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html><body><div id="root">NE-CONSOLE-TEST-INDEX</div></body></html>',
        encoding="utf-8",
    )
    if config_json is not None:
        (dist / "config.json").write_text(config_json, encoding="utf-8")
    return dist


def test_config_json_reachable_without_auth(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/config.json")  # NO Authorization header
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("application/json")

    run(_t())


def test_config_issuer_matches_backend_verifier_trusted_issuer(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/config.json")
            body = r.json()
            oidc = get_state(h.app).identity_settings.oidc
            # ONE source of truth: the endpoint reads the SAME OidcConfig the
            # middleware verifies tokens against.
            assert body["issuer"] == oidc.issuer == ISSUER
            assert body["audience"] == oidc.audience == AUDIENCE

    run(_t())


def test_config_client_id_from_settings(make_app: AppBuilder) -> None:
    # Default build → the pilot public client. Each app freezes the config_hash at
    # factory time; a different console_client_id yields a different hash, so reset
    # the frozen hash before composing the override app (test-only).
    default = make_app()

    async def _t_default() -> None:
        async with default.client() as client:
            r = await client.get("/config.json")
            assert r.json()["client_id"] == "ne-console"

    run(_t_default())

    reset_config_hash_for_testing()
    override = make_app(console_client_id="acme-console")

    async def _t_override() -> None:
        async with override.client() as client:
            r = await client.get("/config.json")
            assert r.json()["client_id"] == "acme-console"

    run(_t_override())


def test_config_payload_is_public_only_no_secret(make_app: AppBuilder) -> None:
    # C12 payload-scan: a configured SCIM secret must NEVER appear in the body.
    h = make_app(scim_bearer_token="s3cr3t-token-16chars")

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/config.json")
            body = r.json()
            assert set(body.keys()) == {"issuer", "client_id", "audience", "api_base"}
            assert all(isinstance(v, str) for v in body.values())
            assert "s3cr3t-token-16chars" not in r.text
            assert "secret" not in r.text.lower()

    run(_t())


def test_config_cache_control_no_store(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/config.json")
            assert r.headers["cache-control"] == "no-store"

    run(_t())


def test_config_route_is_outside_the_oidc_wrap(make_app: AppBuilder, tmp_path: Path) -> None:
    h = make_app(serve_console_dist=str(_dist(tmp_path)))
    routes = h.app.routes

    # /config.json is an exact-match Route (NOT a Mount).
    config_idx = [
        i for i, r in enumerate(routes) if isinstance(r, Route) and r.path == "/config.json"
    ]
    assert len(config_idx) == 1
    cfg_i = config_idx[0]

    # Ordered AFTER /healthz.
    healthz = [i for i, r in enumerate(routes) if isinstance(r, Route) and r.path == "/healthz"]
    assert healthz and healthz[0] < cfg_i

    # Ordered BEFORE the OIDC-wrapped /api Mount (whose app carries ``_app``).
    api_mounts = [i for i, r in enumerate(routes) if isinstance(r, Mount) and r.path == "/api"]
    assert len(api_mounts) == 1
    assert cfg_i < api_mounts[0]
    assert hasattr(routes[api_mounts[0]].app, "_app"), "the /api mount must be OIDC-wrapped"


def test_api_config_json_is_still_guarded(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/config.json")  # no auth
            assert r.status_code == 401

    run(_t())


def test_config_not_shadowed_by_spa_dist(make_app: AppBuilder, tmp_path: Path) -> None:
    dist = _dist(tmp_path, config_json='{"issuer": "https://evil.example"}')
    h = make_app(serve_console_dist=str(dist))

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/config.json")
            assert r.status_code == 200
            body = r.json()
            # Route-before-StaticFiles precedence: the settings issuer wins, never
            # the static file's evil issuer.
            assert body["issuer"] == ISSUER
            assert "evil.example" not in r.text

    run(_t())


def test_config_served_when_no_dist_configured(make_app: AppBuilder) -> None:
    h = make_app()  # no serve_console_dist

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/config.json")
            assert r.status_code == 200
            assert r.json()["issuer"] == ISSUER

    run(_t())
