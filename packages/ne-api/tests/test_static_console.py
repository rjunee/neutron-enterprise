# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-103b static console serving — the mount is OUTSIDE the OIDC wrap; /api and
/healthz keep precedence."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .conftest import AppBuilder, run


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html><body><div id="root">NE-CONSOLE-TEST-INDEX</div></body></html>',
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return dist


def test_root_serves_spa_index_unauthenticated(make_app: AppBuilder, tmp_path: Path) -> None:
    h = make_app(serve_console_dist=str(_dist(tmp_path)))

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/")
            assert r.status_code == 200
            assert "NE-CONSOLE-TEST-INDEX" in r.text
            assert r.headers["content-type"].startswith("text/html")

    run(_t())


def test_spa_fallback_serves_index_for_client_routes(make_app: AppBuilder, tmp_path: Path) -> None:
    h = make_app(serve_console_dist=str(_dist(tmp_path)))

    async def _t() -> None:
        async with h.client() as client:
            for path in ("/approvals", "/auth/callback"):
                r = await client.get(path)
                assert r.status_code == 200, path
                assert "NE-CONSOLE-TEST-INDEX" in r.text, path

    run(_t())


def test_static_asset_served(make_app: AppBuilder, tmp_path: Path) -> None:
    h = make_app(serve_console_dist=str(_dist(tmp_path)))

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/assets/app.js")
            assert r.status_code == 200
            assert "console.log(1)" in r.text
            assert "javascript" in r.headers["content-type"]

    run(_t())


def test_api_me_still_401_unauth_with_static_mounted(make_app: AppBuilder, tmp_path: Path) -> None:
    h = make_app(serve_console_dist=str(_dist(tmp_path)))

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/me")
            assert r.status_code == 401
            assert "NE-CONSOLE-TEST-INDEX" not in r.text
            r2 = await client.post("/api/v1/chat/sessions", json={"agent_name": "console-chat"})
            assert r2.status_code == 401

    run(_t())


def test_api_still_works_authenticated(make_app: AppBuilder, tmp_path: Path) -> None:
    h = make_app(serve_console_dist=str(_dist(tmp_path)))

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/me", headers=h.bearer("user:ada", groups=["eng"]))
            assert r.status_code == 200
            body = r.json()
            assert body["id"] == "user:ada"

    run(_t())


def test_healthz_precedence(make_app: AppBuilder, tmp_path: Path) -> None:
    h = make_app(serve_console_dist=str(_dist(tmp_path)))

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/healthz")
            assert r.status_code == 200
            assert r.json() == {"status": "ok"}
            assert "NE-CONSOLE-TEST-INDEX" not in r.text

    run(_t())


def test_api_unknown_path_is_not_swallowed_by_spa_fallback(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    h = make_app(serve_console_dist=str(_dist(tmp_path)))

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/nonexistent", headers=h.bearer("user:ada"))
            assert r.status_code == 404
            assert "NE-CONSOLE-TEST-INDEX" not in r.text

    run(_t())


def test_no_dist_configured_means_no_root_mount(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/")
            assert r.status_code == 404

    run(_t())


def test_missing_dist_fails_loud_at_factory(make_app: AppBuilder, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="console dist is missing"):
        make_app(serve_console_dist=str(tmp_path / "nope"))


def test_static_mount_is_outside_the_oidc_wrap(make_app: AppBuilder, tmp_path: Path) -> None:
    h = make_app(serve_console_dist=str(_dist(tmp_path)))
    routes = h.app.routes

    # Exactly one /api Mount; its app is the OIDC middleware (carries ``_app``).
    api_mounts = [i for i, r in enumerate(routes) if isinstance(r, Mount) and r.path == "/api"]
    assert len(api_mounts) == 1
    api_idx = api_mounts[0]
    assert hasattr(routes[api_idx].app, "_app"), "the /api mount must be the OIDC-wrapped app"

    # /healthz is an exact-match Route, ordered before /api.
    healthz = [i for i, r in enumerate(routes) if isinstance(r, Route) and r.path == "/healthz"]
    assert healthz and healthz[0] < api_idx

    # The LAST route is the console mount: a bare StaticFiles (NOT wrapped), and it
    # is ordered AFTER the /api mount.
    last = routes[-1]
    assert isinstance(last, Mount) and last.path in ("", "/")
    assert isinstance(last.app, StaticFiles)
    assert not hasattr(last.app, "_app")
    static_idx = len(routes) - 1
    assert api_idx < static_idx
