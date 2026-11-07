# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused proof for non-destructive Pilot realm presentation reconciliation."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import click
import httpx
import pytest

from ne.cli.demo import live

_DESIRED_PRESENTATION = {
    "realm": "ne-pilot",
    "displayName": "Neutron Enterprise",
    "displayNameHtml": "<strong>Neutron Enterprise</strong>",
}


def _write_realm(root: Path, payload: object) -> None:
    path = root / live._PILOT_KEYCLOAK_REALM_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_existing_realm_updates_without_reimport_and_second_run_is_zero_write(
    tmp_path: Path,
) -> None:
    _write_realm(tmp_path, {**_DESIRED_PRESENTATION, "enabled": True})
    desired = live._load_pilot_realm_presentation(tmp_path)
    assert desired == _DESIRED_PRESENTATION

    current: dict[str, object] = {
        "realm": "ne-pilot",
        "enabled": True,
        "displayName": "Old name",
        "displayNameHtml": "<em>Old name</em>",
        "attributes": {"preserve": "yes"},
    }
    calls: list[tuple[str, str, object | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        assert request.url.path == "/admin/realms/ne-pilot"
        if request.method == "GET":
            return httpx.Response(200, json=current)
        assert request.method == "PUT"
        assert isinstance(body, dict)
        current.clear()
        current.update(body)
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        live._reconcile_keycloak_realm_presentation(
            client,
            base="https://keycloak.invalid",
            headers={"Authorization": "Bearer test"},
            desired=desired,
        )
        live._reconcile_keycloak_realm_presentation(
            client,
            base="https://keycloak.invalid",
            headers={"Authorization": "Bearer test"},
            desired=desired,
        )

    assert [method for method, _path, _body in calls] == ["GET", "PUT", "GET", "GET"]
    realm_puts = [body for method, _path, body in calls if method == "PUT"]
    assert len(realm_puts) == 1
    assert realm_puts[0] == {
        "realm": "ne-pilot",
        "enabled": True,
        "displayName": "Neutron Enterprise",
        "displayNameHtml": "<strong>Neutron Enterprise</strong>",
        "attributes": {"preserve": "yes"},
    }
    # The only mutation is the existing-realm Admin REST PUT: no startup import,
    # database/schema reset, volume action, or realm delete is reachable here.
    assert all(path == "/admin/realms/ne-pilot" for _method, path, _body in calls)


def test_absent_realm_presentation_is_a_zero_request_noop(tmp_path: Path) -> None:
    _write_realm(tmp_path, {"realm": "ne-pilot", "enabled": True})
    desired = live._load_pilot_realm_presentation(tmp_path)
    assert desired is None

    def unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("an unconfigured presentation must not call Keycloak")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        live._reconcile_keycloak_realm_presentation(
            client,
            base="https://keycloak.invalid",
            headers={"Authorization": "Bearer test"},
            desired=desired,
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"realm": "foreign"}, "does not identify realm"),
        (
            {"realm": "ne-pilot", "displayName": "Neutron Enterprise"},
            "must define displayName and displayNameHtml together",
        ),
        (
            {
                "realm": "ne-pilot",
                "displayName": "Neutron Enterprise",
                "displayNameHtml": " ",
            },
            "displayNameHtml must be a non-empty string",
        ),
    ],
)
def test_invalid_realm_presentation_fails_before_keycloak_request(
    tmp_path: Path, payload: object, message: str
) -> None:
    _write_realm(tmp_path, payload)
    with pytest.raises(click.ClickException, match=message):
        live._load_pilot_realm_presentation(tmp_path)


def test_realm_update_http_error_fails_closed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"realm": "ne-pilot", "enabled": True})
        return httpx.Response(500)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        live._reconcile_keycloak_realm_presentation(
            client,
            base="https://keycloak.invalid",
            headers={"Authorization": "Bearer test"},
            desired=_DESIRED_PRESENTATION,
        )

    assert calls == ["GET", "PUT"]


def test_realm_update_that_does_not_converge_fails_closed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "realm": "ne-pilot",
                    "displayName": "Old",
                    "displayNameHtml": "<em>Old</em>",
                },
            )
        return httpx.Response(204)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(click.ClickException, match="did not converge exactly"),
    ):
        live._reconcile_keycloak_realm_presentation(
            client,
            base="https://keycloak.invalid",
            headers={"Authorization": "Bearer test"},
            desired=_DESIRED_PRESENTATION,
        )

    assert calls == ["GET", "PUT", "GET"]


def test_demo_provisioning_aborts_before_principal_writes_when_realm_update_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ne.cli import pilotcfg

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path == "/realms/master/protocol/openid-connect/token"
        return httpx.Response(200, json={"access_token": "test"})

    real_client = httpx.Client

    def client_factory(*_args: object, **_kwargs: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler))

    def fail_reconciliation(*_args: object, **_kwargs: object) -> None:
        raise click.ClickException("realm presentation update failed")

    monkeypatch.setattr(httpx, "Client", client_factory)
    monkeypatch.setattr(
        pilotcfg,
        "resolve_required_secrets",
        lambda _bundle: {"NE_PILOT_KEYCLOAK_ADMIN_PASSWORD": "test"},
    )
    monkeypatch.setattr(live, "_load_pilot_realm_presentation", lambda _root: _DESIRED_PRESENTATION)
    monkeypatch.setattr(live, "_reconcile_keycloak_realm_presentation", fail_reconciliation)

    scenario = SimpleNamespace(
        reader_id="reader",
        reader_groups=(),
        outsider_id="outsider",
        outsider_groups=(),
        approver_id="approver-demo",
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with pytest.raises(click.ClickException, match="realm presentation update failed"):
        live._provision_keycloak(bundle, "localhost", scenario)

    assert calls == ["/realms/master/protocol/openid-connect/token"]
    assert not (tmp_path / "demo-credentials.json").exists()
