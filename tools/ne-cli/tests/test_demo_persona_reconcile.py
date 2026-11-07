# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused proof for the narrow existing-demo persona reconciler."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, unquote

import click
import httpx
import pytest
from click.testing import CliRunner

from ne.cli import main
from ne.cli.demo import corpus, live, personas


def _canonical_credentials(value: dict[str, object]) -> bytes:
    # Current authoritative live writer shape: ordering is structural insertion
    # order and a trailing newline is allowed on read. Reconciliation publishes
    # the product writer's sorted/no-newline shape atomically.
    return (json.dumps(value, indent=2, sort_keys=False, ensure_ascii=True) + "\n").encode()


def _existing_demo(tmp_path: Path) -> tuple[Path, Path]:
    layout = live._prepare_workdir(tmp_path / "workdir")
    secrets_toml = live._seed_secret_store(layout["store"])
    live._promote_bundle(layout["bundle"], secrets_toml, "demo.example.test")
    credentials_path = layout["root"] / "demo-credentials.json"
    credentials_path.write_bytes(
        _canonical_credentials(
            {
                "hostname": "demo.example.test",
                "https_port": 443,
                "principals": {
                    "approver-demo": {"username": "approver", "password": "old"},
                    "meridian-frontline": {
                        "username": "meridian-frontline",
                        "password": "old",
                    },
                    "ops": {"username": "ops", "password": "old"},
                    "per-0005": {"username": "per-0005", "password": "old"},
                    "per-0006": {"username": "per-0006", "password": "old"},
                    "steward": {"username": "steward", "password": "old"},
                    "unrelated-demo": {
                        "username": "unrelated-demo",
                        "password": "preserve-me",
                    },
                },
            }
        )
    )
    credentials_path.chmod(0o600)
    return layout["root"], credentials_path


def _mock_keycloak(
    monkeypatch: pytest.MonkeyPatch,
    records: list[dict[str, object]],
    *,
    fail_reset: str | None = None,
    fail_recovery_reset: str | None = None,
    fail_desired_token: str | None = None,
    fail_recovery_token: str | None = None,
    fail_recovery_group: str | None = None,
    fail_authority: str | None = None,
    frontline_authority: tuple[list[str], list[str]] | None = None,
    missing_groups: tuple[str, ...] = (),
    ambiguous_create: str | None = None,
    created_location_failure: str | None = None,
) -> tuple[dict[str, set[str]], dict[str, str]]:
    real_client = httpx.Client
    user_ids = {
        "per-0005": "user-reader",
        "per-0006": "user-outsider",
        "ops": "user-ops",
        "approver": "user-approver",
        "steward": "user-steward",
        "meridian-frontline": "user-frontline",
    }
    memberships = {
        "user-reader": {"dept-engineering"},
        "user-outsider": {"all-employees"},
        "user-ops": {"ops", "meridian-ops"},
        "user-approver": {
            "approver",
            "all-employees",
            "sharepoint.operations.read",
            "stale-approver",
        },
        "user-steward": {"steward", "ops"},
        "user-frontline": {"meridian-frontline", "bank.customer.read"},
    }
    groups = {
        name: f"group-{name}"
        for name in (
            "ops",
            "dept-engineering",
            "meridian-ops",
            "all-employees",
            "sharepoint.operations.read",
            "stale-approver",
            "steward",
            *personas._APPROVER_GROUPS,
            *personas._STEWARD_GROUPS,
            *personas._FRONTLINE_GROUPS,
        )
        if name not in missing_groups
    }
    passwords = {username: "old" for username in user_ids}
    failed_reset_once = False
    desired_token_failed_once = False
    recovery_group_failed_once = False
    old_token_grants: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        body = None
        if request.content and request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            body = json.loads(request.content)
        records.append(
            {
                "method": request.method,
                "path": path,
                "params": dict(request.url.params),
                "body": body,
            }
        )
        if path.endswith("/realms/master/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "admin-bearer"})
        if path.endswith("/realms/ne-pilot/protocol/openid-connect/token"):
            form = parse_qs(request.content.decode("ascii"), strict_parsing=True)
            username = form.get("username", [""])[0]
            password = form.get("password", [""])[0]
            nonlocal desired_token_failed_once
            if (
                username == fail_desired_token
                and password == personas._PERSONA_PASSWORDS.get(username)
                and not desired_token_failed_once
            ):
                desired_token_failed_once = True
                return httpx.Response(401)
            prior_grants = old_token_grants.get(username, 0)
            if username == fail_recovery_token and password == "old" and prior_grants >= 1:
                return httpx.Response(401)
            if passwords.get(username) != password:
                return httpx.Response(401)
            if password == "old":
                old_token_grants[username] = prior_grants + 1
            return httpx.Response(200, json={"access_token": f"bearer-{username}"})
        if request.method == "GET" and path == "/api/v1/me":
            username = request.headers.get("authorization", "").removeprefix("Bearer bearer-")
            if username == fail_authority:
                return httpx.Response(
                    200,
                    json={"id": f"principal-{username}", "groups": [], "entitlements": []},
                )
            expected = {
                "approver": (sorted(personas._APPROVER_GROUPS), ["approvals"]),
                "steward": (sorted(personas._STEWARD_GROUPS), ["steward"]),
                "meridian-frontline": (
                    frontline_authority
                    if frontline_authority is not None
                    else (sorted(personas._FRONTLINE_GROUPS), [])
                ),
            }
            groups_for_user, entitlements = expected[username]
            return httpx.Response(
                200,
                json={
                    "id": f"principal-{username}",
                    "groups": groups_for_user,
                    "entitlements": entitlements,
                },
            )
        if request.method == "GET" and path.endswith("/admin/realms/ne-pilot/users"):
            username = request.url.params.get("username")
            user_id = user_ids.get(str(username))
            return httpx.Response(
                200,
                json=[] if user_id is None else [{"id": user_id, "username": username}],
            )
        if request.method == "POST" and path.endswith("/admin/realms/ne-pilot/groups"):
            assert isinstance(body, dict)
            name = str(body["name"])
            status = 409 if name in groups or name == ambiguous_create else 201
            groups.setdefault(name, f"group-{name}")
            headers = {"Location": f"/admin/realms/ne-pilot/groups/{groups[name]}"}
            if status == 201 and created_location_failure == "missing":
                headers = {}
            elif status == 201 and created_location_failure == "malformed":
                headers = {"Location": "/admin/realms/ne-pilot/groups/not/safe"}
            elif status == 201 and created_location_failure == "foreign":
                headers = {
                    "Location": (
                        f"https://foreign.test/admin/realms/ne-pilot/groups/{groups[name]}"
                    )
                }
            return httpx.Response(status, headers=headers)
        if request.method == "GET" and path.endswith("/admin/realms/ne-pilot/groups"):
            name = str(request.url.params.get("search"))
            group_id = groups.get(name)
            return httpx.Response(
                200,
                json=[] if group_id is None else [{"id": group_id, "name": name}],
            )
        if (
            request.method == "GET"
            and "/admin/realms/ne-pilot/groups/" in path
            and "/" not in path.rsplit("/groups/", 1)[1]
        ):
            group_id = path.rsplit("/groups/", 1)[1]
            name = next(
                (candidate for candidate, value in groups.items() if value == group_id), None
            )
            if name is None:
                return httpx.Response(404)
            if created_location_failure == "replacement":
                name = "foreign-replacement"
            return httpx.Response(200, json={"id": group_id, "name": name})
        if request.method == "GET" and path.endswith(("/members", "/children")):
            return httpx.Response(200, json=[])
        if (
            request.method == "DELETE"
            and "/admin/realms/ne-pilot/groups/" in path
            and "/users/" not in path
        ):
            group_id = path.rsplit("/groups/", 1)[1]
            name = next(name for name, candidate in groups.items() if candidate == group_id)
            del groups[name]
            return httpx.Response(204)
        if request.method == "GET" and path.endswith("/groups") and "/users/" in path:
            user_id = path.split("/users/", 1)[1].split("/", 1)[0]
            return httpx.Response(
                200,
                json=[{"id": groups[name], "name": name} for name in sorted(memberships[user_id])],
            )
        if request.method in {"PUT", "DELETE"} and "/groups/" in path:
            user_id = path.split("/users/", 1)[1].split("/", 1)[0]
            username = next(name for name, candidate in user_ids.items() if candidate == user_id)
            group_id = path.rsplit("/groups/", 1)[1]
            name = next(name for name, candidate in groups.items() if candidate == group_id)
            nonlocal recovery_group_failed_once
            if (
                fail_recovery_group == username
                and not recovery_group_failed_once
                and (
                    (request.method == "PUT" and name in {"all-employees", "stale-approver", "ops"})
                    or (request.method == "DELETE" and name in personas._APPROVER_GROUPS)
                )
            ):
                recovery_group_failed_once = True
                return httpx.Response(500)
            if request.method == "PUT":
                memberships[user_id].add(name)
            else:
                memberships[user_id].discard(name)
            return httpx.Response(204)
        if request.method == "PUT" and path.endswith("/reset-password"):
            user_id = path.split("/users/", 1)[1].split("/", 1)[0]
            username = next(name for name, candidate in user_ids.items() if candidate == user_id)
            assert isinstance(body, dict)
            password = str(body["value"])
            nonlocal failed_reset_once
            if username == fail_reset and not failed_reset_once:
                failed_reset_once = True
                return httpx.Response(500)
            if username == fail_recovery_reset and password == "old":
                return httpx.Response(500)
            passwords[username] = password
            return httpx.Response(204)
        return httpx.Response(599)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: real_client(transport=httpx.MockTransport(handler)),
    )
    return memberships, passwords


def test_authoritative_groups_are_pinned_to_fixture_and_realm() -> None:
    scenario = corpus.build_scenario()
    assert (scenario.reader_id, scenario.outsider_id) == ("per-0005", "per-0006")
    assert personas._PERSONA_PASSWORDS[scenario.reader_id] == "per-0005-demo"
    assert personas._PERSONA_PASSWORDS[scenario.outsider_id] == "per-0006-demo"
    root = Path(__file__).resolve().parents[3]
    banking = json.loads(
        (root / "tools/meridian/fixtures/meridian-v1/banking.json").read_text(encoding="utf-8")
    )
    approver = next(row for row in banking["personas"] if row["persona_id"] == "approver")
    assert (
        "approver",
        "meridian-approver",
        *approver["entitlements"],
    ) == personas._APPROVER_GROUPS
    assert "all-employees" not in personas._APPROVER_GROUPS
    assert "sharepoint.operations.read" not in personas._APPROVER_GROUPS

    realm = json.loads(
        (root / "deploy/compose/pilot/keycloak/ne-pilot-realm.json").read_text(encoding="utf-8")
    )
    steward = next(row for row in realm["users"] if row["username"] == "steward")
    assert tuple(group.removeprefix("/") for group in steward["groups"]) == personas._STEWARD_GROUPS

    # The Branch Service Representative's reconciled groups are exactly the corpus
    # persona's entitlements plus its two identity groups, and are byte-identical to
    # what the `ne demo up` provisioner grants.  Frontline must NEVER gain
    # bank.servicing.read or bank.dispute.read: those are the labels that make the
    # loan/payment/servicing-note/dispute templates reachable.
    frontline = next(row for row in banking["personas"] if row["persona_id"] == "frontline")
    assert (
        "all-employees",
        "meridian-frontline",
        *frontline["entitlements"],
    ) == personas._FRONTLINE_GROUPS
    assert personas._FRONTLINE_GROUPS == live._MERIDIAN_RESTRICTED_GROUPS
    assert "bank.servicing.read" not in personas._FRONTLINE_GROUPS
    assert "bank.dispute.read" not in personas._FRONTLINE_GROUPS
    assert "bank.approval.read" not in personas._FRONTLINE_GROUPS
    realm_frontline = next(row for row in realm["users"] if row["username"] == "meridian-frontline")
    assert (
        tuple(group.removeprefix("/") for group in realm_frontline["groups"])
        == personas._FRONTLINE_GROUPS
    )
    # Frontline's reach is a STRICT subset of approver's graph authority.
    assert set(personas._FRONTLINE_GROUPS) - {"all-employees", "meridian-frontline"} < set(
        personas._APPROVER_GROUPS
    ) | {"bank.account-summary.read", "sharepoint.frontline.read"}


def test_reconcile_personas_is_narrow_exact_and_atomically_publishes_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir, credentials_path = _existing_demo(tmp_path)
    original_inode = credentials_path.stat().st_ino
    records: list[dict[str, object]] = []
    memberships, _passwords = _mock_keycloak(monkeypatch, records)

    result = personas.reconcile_demo_personas(workdir)

    assert result == credentials_path
    assert credentials_path.stat().st_ino != original_inode
    assert credentials_path.stat().st_mode & 0o777 == 0o600
    assert list(workdir.glob(".demo-credentials.*")) == []
    assert memberships["user-approver"] == set(personas._APPROVER_GROUPS)
    assert memberships["user-steward"] == set(personas._STEWARD_GROUPS)
    assert memberships["user-ops"] == {"ops", "meridian-ops"}
    assert memberships["user-frontline"] == set(personas._FRONTLINE_GROUPS)
    assert memberships["user-reader"] == {"dept-engineering"}
    assert memberships["user-outsider"] == {"all-employees"}

    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    assert (
        credentials_path.read_bytes()
        == json.dumps(credentials, indent=2, sort_keys=True, ensure_ascii=True).encode()
    )
    assert credentials["principals"]["unrelated-demo"] == {
        "username": "unrelated-demo",
        "password": "preserve-me",
    }
    assert {key: credentials["principals"][key] for key in personas._CREDENTIAL_KEYS.values()} == {
        "ops": {"username": "ops", "password": "ops-demo"},
        "per-0005": {"username": "per-0005", "password": "per-0005-demo"},
        "per-0006": {"username": "per-0006", "password": "per-0006-demo"},
        "approver-demo": {"username": "approver", "password": "approver-demo"},
        "steward": {"username": "steward", "password": "steward-demo"},
        "meridian-frontline": {
            "username": "meridian-frontline",
            "password": "meridian-frontline-demo",
        },
    }
    resets = [row for row in records if str(row["path"]).endswith("/reset-password")]
    assert {cast(dict[str, object], row["body"])["value"] for row in resets} == set(
        personas._PERSONA_PASSWORDS.values()
    )
    assert not any("/clients" in str(row["path"]) for row in records)
    assert all(
        str(row["path"]) == "/api/v1/me"
        or "openid-connect/token" in str(row["path"])
        or "/admin/realms/ne-pilot/groups" in str(row["path"])
        or "/admin/realms/ne-pilot/users" in str(row["path"])
        for row in records
    )


@pytest.mark.parametrize("failed_username", tuple(personas._PERSONA_PASSWORDS))
def test_each_password_mutation_failure_fully_recovers_prior_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failed_username: str
) -> None:
    workdir, credentials_path = _existing_demo(tmp_path)
    original = credentials_path.read_bytes()
    original_inode = credentials_path.stat().st_ino
    memberships, passwords = _mock_keycloak(monkeypatch, [], fail_reset=failed_username)

    with pytest.raises(personas.PersonaReconciliationSafelyRefused):
        personas.reconcile_demo_personas(workdir)

    assert credentials_path.read_bytes() == original
    assert credentials_path.stat().st_ino == original_inode
    assert list(workdir.glob(".demo-credentials.*")) == []
    assert set(passwords.values()) == {"old"}
    assert memberships["user-approver"] == {
        "approver",
        "all-employees",
        "sharepoint.operations.read",
        "stale-approver",
    }
    assert memberships["user-steward"] == {"steward", "ops"}


@pytest.mark.parametrize("fail_on_call", (1, 2, 3))
def test_group_mutation_failure_fully_recovers_prior_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail_on_call: int
) -> None:
    workdir, credentials_path = _existing_demo(tmp_path)
    original = credentials_path.read_bytes()
    records: list[dict[str, object]] = []
    memberships, passwords = _mock_keycloak(monkeypatch, records)
    real = personas._reconcile_exact_groups
    calls = 0

    def fail_after_approver(*args, **kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == fail_on_call:
            raise RuntimeError("injected group reconciliation failure")
        real(*args, **kwargs)

    monkeypatch.setattr(personas, "_reconcile_exact_groups", fail_after_approver)

    with pytest.raises(personas.PersonaReconciliationSafelyRefused):
        personas.reconcile_demo_personas(workdir)

    assert credentials_path.read_bytes() == original
    assert set(passwords.values()) == {"old"}
    assert memberships["user-approver"] == {
        "approver",
        "all-employees",
        "sharepoint.operations.read",
        "stale-approver",
    }
    assert memberships["user-steward"] == {"steward", "ops"}
    assert memberships["user-frontline"] == {"meridian-frontline", "bank.customer.read"}


def test_post_replace_publication_failure_restores_exact_prior_bytes_and_remote_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir, credentials_path = _existing_demo(tmp_path)
    original = credentials_path.read_bytes()
    memberships, passwords = _mock_keycloak(monkeypatch, [])
    real = personas._publish_credentials_bytes
    first = True

    def fail_after_replace(path: Path, raw: bytes, *, expected_identity):
        nonlocal first
        result = real(path, raw, expected_identity=expected_identity)
        if first:
            first = False
            raise OSError("injected post-replace directory fsync failure")
        return result

    monkeypatch.setattr(personas, "_publish_credentials_bytes", fail_after_replace)

    with pytest.raises(personas.PersonaReconciliationSafelyRefused):
        personas.reconcile_demo_personas(workdir)

    assert credentials_path.read_bytes() == original
    assert set(passwords.values()) == {"old"}
    assert memberships["user-approver"] == {
        "approver",
        "all-employees",
        "sharepoint.operations.read",
        "stale-approver",
    }


def test_desired_token_smoke_failure_compensates_file_passwords_and_groups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir, credentials_path = _existing_demo(tmp_path)
    original = credentials_path.read_bytes()
    memberships, passwords = _mock_keycloak(monkeypatch, [], fail_desired_token="approver")

    with pytest.raises(personas.PersonaReconciliationSafelyRefused):
        personas.reconcile_demo_personas(workdir)

    assert credentials_path.read_bytes() == original
    assert set(passwords.values()) == {"old"}
    assert memberships["user-steward"] == {"steward", "ops"}


@pytest.mark.parametrize("username", ("approver", "steward", "meridian-frontline"))
def test_effective_authority_failure_compensates_file_passwords_and_groups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, username: str
) -> None:
    workdir, credentials_path = _existing_demo(tmp_path)
    original = credentials_path.read_bytes()
    memberships, passwords = _mock_keycloak(monkeypatch, [], fail_authority=username)

    with pytest.raises(personas.PersonaReconciliationSafelyRefused) as captured:
        personas.reconcile_demo_personas(workdir)

    assert captured.value.exit_code == 75
    assert credentials_path.read_bytes() == original
    assert set(passwords.values()) == {"old"}
    assert memberships["user-approver"] == {
        "approver",
        "all-employees",
        "sharepoint.operations.read",
        "stale-approver",
    }
    assert memberships["user-steward"] == {"steward", "ops"}


def test_missing_authoritative_groups_are_created_and_retained_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir, _credentials_path = _existing_demo(tmp_path)
    missing = ("meridian-approver", "bank.approval.read", "sharepoint.restricted.read")
    records: list[dict[str, object]] = []
    memberships, _passwords = _mock_keycloak(monkeypatch, records, missing_groups=missing)

    personas.reconcile_demo_personas(workdir)

    created = [
        cast(dict[str, object], row["body"])["name"]
        for row in records
        if row["method"] == "POST" and str(row["path"]).endswith("/admin/realms/ne-pilot/groups")
    ]
    assert created == list(missing)
    assert memberships["user-approver"] == set(personas._APPROVER_GROUPS)
    assert not any(
        row["method"] == "DELETE"
        and "/admin/realms/ne-pilot/groups/" in str(row["path"])
        and "/users/" not in str(row["path"])
        for row in records
    )


@pytest.mark.parametrize("failure_phase", ("group", "password", "publication", "token"))
def test_each_post_create_failure_deletes_only_invocation_created_groups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_phase: str
) -> None:
    workdir, credentials_path = _existing_demo(tmp_path)
    original = credentials_path.read_bytes()
    missing = ("meridian-approver", "bank.approval.read", "sharepoint.restricted.read")
    records: list[dict[str, object]] = []
    if failure_phase == "password":
        memberships, passwords = _mock_keycloak(
            monkeypatch, records, missing_groups=missing, fail_reset="steward"
        )
    elif failure_phase == "token":
        memberships, passwords = _mock_keycloak(
            monkeypatch, records, missing_groups=missing, fail_desired_token="approver"
        )
    else:
        memberships, passwords = _mock_keycloak(monkeypatch, records, missing_groups=missing)
    if failure_phase == "group":
        real_groups = personas._reconcile_exact_groups
        calls = 0

        def fail_steward(*args, **kwargs) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected post-create group failure")
            real_groups(*args, **kwargs)

        monkeypatch.setattr(personas, "_reconcile_exact_groups", fail_steward)
    elif failure_phase == "publication":
        real_publish = personas._publish_credentials_bytes
        first = True

        def fail_after_publish(path: Path, raw: bytes, *, expected_identity):
            nonlocal first
            result = real_publish(path, raw, expected_identity=expected_identity)
            if first:
                first = False
                raise OSError("injected post-create publication failure")
            return result

        monkeypatch.setattr(personas, "_publish_credentials_bytes", fail_after_publish)

    with pytest.raises(Exception) as captured:
        personas.reconcile_demo_personas(workdir)

    assert isinstance(captured.value, personas.PersonaReconciliationSafelyRefused)
    assert credentials_path.read_bytes() == original
    assert set(passwords.values()) == {"old"}
    assert memberships["user-approver"] == {
        "approver",
        "all-employees",
        "sharepoint.operations.read",
        "stale-approver",
    }
    deleted = [
        str(row["path"]).rsplit("/groups/", 1)[1]
        for row in records
        if row["method"] == "DELETE"
        and "/admin/realms/ne-pilot/groups/" in str(row["path"])
        and "/users/" not in str(row["path"])
    ]
    assert len(deleted) == len(missing)


def test_ambiguous_post_create_conflict_is_unproven_and_deletes_only_owned_groups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir, _credentials_path = _existing_demo(tmp_path)
    missing = ("meridian-approver", "bank.approval.read", "sharepoint.restricted.read")
    records: list[dict[str, object]] = []
    _mock_keycloak(
        monkeypatch,
        records,
        missing_groups=missing,
        ambiguous_create="bank.approval.read",
    )

    with pytest.raises(personas.PersonaRecoveryUnproven) as captured:
        personas.reconcile_demo_personas(workdir)

    assert captured.value.exit_code == 78
    deleted = [
        str(row["path"]).rsplit("/groups/", 1)[1]
        for row in records
        if row["method"] == "DELETE"
        and "/admin/realms/ne-pilot/groups/" in str(row["path"])
        and "/users/" not in str(row["path"])
    ]
    assert deleted == ["group-meridian-approver"]


@pytest.mark.parametrize(
    "created_location_failure",
    ("missing", "malformed", "foreign", "replacement"),
)
def test_created_group_requires_location_bound_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    created_location_failure: str,
) -> None:
    workdir, _credentials_path = _existing_demo(tmp_path)
    records: list[dict[str, object]] = []
    _mock_keycloak(
        monkeypatch,
        records,
        missing_groups=("meridian-approver",),
        created_location_failure=created_location_failure,
    )

    with pytest.raises(personas.PersonaRecoveryUnproven) as captured:
        personas.reconcile_demo_personas(workdir)

    assert captured.value.exit_code == 78
    assert not any(
        row["method"] == "DELETE"
        and "/admin/realms/ne-pilot/groups/" in str(row["path"])
        and "/users/" not in str(row["path"])
        for row in records
    )


@pytest.mark.parametrize(
    "recovery_failure",
    ("password", "group", "credentials", "token"),
)
def test_incomplete_compensation_uses_distinct_exit_78(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recovery_failure: str
) -> None:
    workdir, credentials_path = _existing_demo(tmp_path)
    original = credentials_path.read_bytes()
    if recovery_failure == "password":
        _mock_keycloak(monkeypatch, [], fail_reset="steward", fail_recovery_reset="ops")
    elif recovery_failure == "group":
        _mock_keycloak(monkeypatch, [], fail_reset="steward", fail_recovery_group="approver")
    elif recovery_failure == "token":
        _mock_keycloak(monkeypatch, [], fail_reset="steward", fail_recovery_token="ops")
    else:
        _mock_keycloak(monkeypatch, [], fail_reset="steward")
    if recovery_failure == "credentials":
        real = personas._publish_credentials_bytes

        def refuse_prior_bytes(path: Path, raw: bytes, *, expected_identity):
            if raw == original:
                raise OSError("injected credential recovery failure")
            return real(path, raw, expected_identity=expected_identity)

        monkeypatch.setattr(personas, "_publish_credentials_bytes", refuse_prior_bytes)
        # Reach publication and then fail desired proof so file recovery is required.
        monkeypatch.undo()
        _mock_keycloak(monkeypatch, [], fail_desired_token="approver")
        monkeypatch.setattr(personas, "_publish_credentials_bytes", refuse_prior_bytes)

    with pytest.raises(personas.PersonaRecoveryUnproven) as captured:
        personas.reconcile_demo_personas(workdir)

    assert captured.value.exit_code == 78


def test_cli_preserves_distinct_unproven_recovery_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir = (tmp_path / "existing-demo").resolve()
    monkeypatch.setattr(
        personas,
        "reconcile_demo_personas",
        lambda _path: (_ for _ in ()).throw(
            personas.PersonaRecoveryUnproven(["password grant ops"])
        ),
    )

    result = CliRunner().invoke(
        main,
        ["demo", "--workdir", str(workdir), "reconcile-personas"],
    )

    assert result.exit_code == 78
    assert "recovery is unproven" in result.output


def test_cli_uses_distinct_safe_refusal_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir = (tmp_path / "existing-demo").resolve()
    monkeypatch.setattr(
        personas,
        "reconcile_demo_personas",
        lambda _path: (_ for _ in ()).throw(click.ClickException("safe preflight")),
    )

    result = CliRunner().invoke(
        main,
        ["demo", "--workdir", str(workdir), "reconcile-personas"],
    )

    assert result.exit_code == 75
    assert "fully proven compensation" in result.output


@pytest.mark.parametrize(
    "damage",
    (
        "credentials-symlink",
        "credentials-hardlink",
        "credentials-mode",
        "credentials-noncanonical-json",
        "manifest-symlink",
        "manifest-mode",
        "manifest-noncanonical-json",
    ),
)
def test_unsafe_or_noncanonical_inputs_fail_before_any_keycloak_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, damage: str
) -> None:
    workdir, credentials_path = _existing_demo(tmp_path)
    manifest_path = workdir / "bundle/manifest.json"
    if damage == "credentials-symlink":
        target = workdir / "credentials-target"
        credentials_path.replace(target)
        credentials_path.symlink_to(target)
    elif damage == "credentials-hardlink":
        os.link(credentials_path, workdir / "credentials-hardlink")
    elif damage == "credentials-mode":
        credentials_path.chmod(0o640)
    elif damage == "credentials-noncanonical-json":
        text = credentials_path.read_text(encoding="utf-8")
        text = text.replace(
            '"hostname": "demo.example.test",',
            '"hostname": "demo.example.test",\n  "hostname": "demo.example.test",',
            1,
        )
        credentials_path.write_text(text, encoding="utf-8")
    elif damage == "manifest-symlink":
        target = workdir / "manifest-target"
        manifest_path.replace(target)
        manifest_path.symlink_to(target)
    elif damage == "manifest-mode":
        manifest_path.chmod(0o666)
    elif damage == "manifest-noncanonical-json":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    records: list[dict[str, object]] = []
    _mock_keycloak(monkeypatch, records)

    with pytest.raises(click.ClickException, match="reconciliation refused"):
        personas.reconcile_demo_personas(workdir)

    assert records == []


def test_cli_controller_exposes_only_the_narrow_reconciliation_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir = (tmp_path / "existing-demo").resolve()
    calls: list[Path] = []
    monkeypatch.setattr(
        personas,
        "reconcile_demo_personas",
        lambda path: calls.append(path) or path / "demo-credentials.json",
    )

    result = CliRunner().invoke(
        main,
        ["demo", "--workdir", str(workdir), "reconcile-personas"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [workdir]
    assert "Demo personas reconciled" in result.output


@pytest.mark.parametrize(
    "over_entitled",
    (
        pytest.param((sorted(personas._APPROVER_GROUPS), []), id="approver-groups"),
        pytest.param(
            (sorted({*personas._FRONTLINE_GROUPS, "bank.servicing.read"}), []),
            id="plus-servicing",
        ),
        pytest.param(
            (sorted({*personas._FRONTLINE_GROUPS, "bank.dispute.read"}), []),
            id="plus-dispute",
        ),
        pytest.param((sorted(personas._FRONTLINE_GROUPS), ["ops"]), id="ops-entitlement"),
        pytest.param((sorted(personas._FRONTLINE_GROUPS), ["approvals"]), id="approver-surface"),
    ),
)
def test_over_entitled_frontline_is_refused_not_admitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, over_entitled: tuple[list[str], list[str]]
) -> None:
    """A frontline that can log in but reaches too far must FAIL the gate.

    This is the arm that matters: a persona repair which merely lets everybody in has
    made the governance demo worse, not better.  Each variant below is a login that
    succeeds while the effective authority is wider than the Branch Service
    Representative's, and each must refuse before publication and compensate exactly.
    """
    workdir, credentials_path = _existing_demo(tmp_path)
    original = credentials_path.read_bytes()
    memberships, passwords = _mock_keycloak(monkeypatch, [], frontline_authority=over_entitled)

    with pytest.raises(personas.PersonaReconciliationSafelyRefused) as captured:
        personas.reconcile_demo_personas(workdir)

    assert captured.value.exit_code == 75
    assert credentials_path.read_bytes() == original
    assert set(passwords.values()) == {"old"}
    assert memberships["user-frontline"] == {"meridian-frontline", "bank.customer.read"}
    assert memberships["user-approver"] == {
        "approver",
        "all-employees",
        "sharepoint.operations.read",
        "stale-approver",
    }
    assert memberships["user-ops"] == {"ops", "meridian-ops"}
