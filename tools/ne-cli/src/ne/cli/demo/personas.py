# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Narrow, fail-closed reconciliation for existing demo personas."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import click

_APPROVER_GROUPS = (
    "approver",
    "meridian-approver",
    "bank.customer.read",
    "bank.servicing.read",
    "bank.dispute.read",
    "bank.approval.read",
    "sharepoint.restricted.read",
)
_STEWARD_GROUPS = ("steward",)
# Mirrors ``live._MERIDIAN_RESTRICTED_GROUPS``.  The Branch Service Representative
# holds ``bank.customer.read`` and NOT ``bank.servicing.read`` / ``bank.dispute.read``,
# so of the six Meridian templates it reaches only ``customers-in-counties`` and is
# refused the loan/payment/servicing-note/dispute reads by the C7 scope intersection.
_FRONTLINE_GROUPS = (
    "all-employees",
    "meridian-frontline",
    "bank.customer.read",
    "bank.account-summary.read",
    "sharepoint.frontline.read",
)
#: username -> the EXACT group set this reconciler owns for it.  A persona that can
#: authenticate but holds the wrong entitlement groups is a governance defect, not a
#: login fix: every member here is both reconciled exactly AND proven exactly below.
_RECONCILED_GROUPS: dict[str, tuple[str, ...]] = {
    "approver": _APPROVER_GROUPS,
    "steward": _STEWARD_GROUPS,
    "meridian-frontline": _FRONTLINE_GROUPS,
}
_PERSONA_PASSWORDS = {
    "per-0005": "per-0005-demo",
    "per-0006": "per-0006-demo",
    "ops": "ops-demo",
    "approver": "approver-demo",
    "steward": "steward-demo",
    "meridian-frontline": "meridian-frontline-demo",
}
_CREDENTIAL_KEYS = {
    "per-0005": "per-0005",
    "per-0006": "per-0006",
    "ops": "ops",
    "approver": "approver-demo",
    "steward": "steward",
    "meridian-frontline": "meridian-frontline",
}
_SAFE_REMOTE_ID = re.compile(r"^[A-Za-z0-9._-]{1,256}$")
_SAFE_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$"
)


@dataclass(frozen=True, slots=True)
class _ExistingDemo:
    workdir: Path
    bundle: Path
    credentials_path: Path
    credentials_identity: tuple[int, int, int, int]
    credentials_raw: bytes
    credentials: dict[str, Any]
    hostname: str
    https_port: int
    external_url: str | None


def _fail(message: str) -> click.ClickException:
    return click.ClickException(f"demo persona reconciliation refused: {message}")


class PersonaRecoveryUnproven(click.ClickException):
    """A remote persona mutation could not be proven fully compensated."""

    exit_code = 78

    def __init__(self, failures: list[str]) -> None:
        joined = ", ".join(failures)
        super().__init__(
            "demo persona reconciliation recovery is unproven; "
            f"manual Keycloak/credential recovery is required ({joined})"
        )


class PersonaReconciliationSafelyRefused(click.ClickException):
    """A persona operation refused before mutation or after exact compensation."""

    exit_code = 75

    def __init__(self) -> None:
        super().__init__(
            "demo persona reconciliation refused before mutation or after fully proven compensation"
        )


def _canonical_directory(path: Path, *, label: str, private: bool = False) -> None:
    if not path.is_absolute():
        raise _fail(f"{label} path is not absolute")
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise _fail(f"{label} path is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or resolved != path:
        raise _fail(f"{label} path is symlinked or noncanonical")
    if info.st_uid != os.geteuid() or info.st_mode & 0o002:
        raise _fail(f"{label} ownership or mode is unsafe")
    if private and stat.S_IMODE(info.st_mode) != 0o700:
        raise _fail(f"{label} must have mode 0700")


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


def _canonical_file(
    path: Path, *, label: str, mode: int | None = None
) -> tuple[bytes, tuple[int, int, int, int]]:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise _fail(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or resolved != path
    ):
        raise _fail(f"{label} is symlinked, hard-linked, foreign-owned, or noncanonical")
    if mode is not None:
        if stat.S_IMODE(info.st_mode) != mode:
            raise _fail(f"{label} must have mode {mode:04o}")
    elif info.st_mode & 0o002:
        raise _fail(f"{label} is world writable")
    try:
        payload = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise _fail(f"{label} is unreadable") from exc
    if _file_identity(after) != _file_identity(info):
        raise _fail(f"{label} changed while it was being read")
    return payload, _file_identity(info)


def _json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _fail(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise _fail(f"{label} must contain one JSON object")
    return value


def _validate_external_url(value: object, *, hostname: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise _fail("credentials external_url is invalid")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise _fail("credentials external_url is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
        or value.endswith("/")
    ):
        raise _fail("credentials external_url is noncanonical")
    return value


def _load_existing_demo(workdir: Path) -> _ExistingDemo:
    _canonical_directory(workdir, label="workdir", private=True)
    bundle = workdir / "bundle"
    credentials_path = workdir / "demo-credentials.json"
    _canonical_directory(bundle, label="bundle")
    manifest_path = bundle / "manifest.json"
    manifest_raw, _manifest_identity = _canonical_file(manifest_path, label="bundle manifest")
    credentials_raw, credentials_identity = _canonical_file(
        credentials_path, label="credentials file", mode=0o600
    )
    manifest = _json_object(manifest_raw, label="bundle manifest")
    credentials = _json_object(credentials_raw, label="credentials file")

    expected_manifest = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()
    if manifest_raw != expected_manifest:
        raise _fail("bundle manifest JSON is not canonical")
    from ne.cli.pilotcfg import verify_bundle

    verified = verify_bundle(bundle)
    hostname = verified.get("hostname")
    if (
        not isinstance(hostname, str)
        or not _SAFE_HOSTNAME.fullmatch(hostname)
        or credentials.get("hostname") != hostname
    ):
        raise _fail("bundle and credentials hostname metadata is invalid or mismatched")
    if set(credentials) not in (
        {"hostname", "https_port", "principals"},
        {"external_url", "hostname", "https_port", "principals"},
    ):
        raise _fail("credentials object has a noncanonical field set")
    https_port = credentials.get("https_port")
    if (
        isinstance(https_port, bool)
        or not isinstance(https_port, int)
        or not 1 <= https_port <= 65535
        or https_port == 8443
    ):
        raise _fail("credentials HTTPS port is invalid")
    principals = credentials.get("principals")
    if not isinstance(principals, dict) or any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, dict)
        or set(value) != {"password", "username"}
        or not isinstance(value.get("username"), str)
        or not value["username"]
        or not isinstance(value.get("password"), str)
        or not value["password"]
        for key, value in principals.items()
    ):
        raise _fail("credentials principals object is noncanonical")
    usernames = [value["username"] for value in principals.values()]
    if len(usernames) != len(set(usernames)):
        raise _fail("credentials principals contain duplicate usernames")
    for username, key in _CREDENTIAL_KEYS.items():
        current = principals.get(key)
        if current is not None and current["username"] != username:
            raise _fail(f"credentials principal key {key!r} targets another username")
        if any(
            candidate_key != key and value["username"] == username
            for candidate_key, value in principals.items()
        ):
            raise _fail(f"credentials username {username!r} uses a noncanonical key")
    external_url = _validate_external_url(credentials.get("external_url"), hostname=hostname)
    return _ExistingDemo(
        workdir=workdir,
        bundle=bundle,
        credentials_path=credentials_path,
        credentials_identity=credentials_identity,
        credentials_raw=credentials_raw,
        credentials=credentials,
        hostname=hostname,
        https_port=https_port,
        external_url=external_url,
    )


def _response_rows(response: Any, *, label: str) -> list[dict[str, object]]:
    response.raise_for_status()
    try:
        value = response.json()
    except (ValueError, TypeError) as exc:
        raise _fail(f"Keycloak {label} response is invalid") from exc
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise _fail(f"Keycloak {label} response is malformed")
    return value


def _safe_remote_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_REMOTE_ID.fullmatch(value):
        raise _fail(f"Keycloak returned an unsafe {label} id")
    return value


def _exact_user_id(client: Any, *, base: str, headers: dict[str, str], username: str) -> str:
    rows = _response_rows(
        client.get(
            f"{base}/admin/realms/ne-pilot/users",
            params={"username": username, "exact": "true"},
            headers=headers,
        ),
        label=f"user {username}",
    )
    exact = [row for row in rows if row.get("username") == username]
    if len(exact) != 1:
        raise _fail(f"Keycloak user {username!r} was not found uniquely")
    return _safe_remote_id(exact[0].get("id"), label="user")


def _find_exact_group_ids(
    client: Any, *, base: str, headers: dict[str, str], names: tuple[str, ...]
) -> tuple[dict[str, str], tuple[str, ...]]:
    result: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        rows = _response_rows(
            client.get(
                f"{base}/admin/realms/ne-pilot/groups",
                params={"search": name, "exact": "true"},
                headers=headers,
            ),
            label=f"group {name}",
        )
        exact = [row for row in rows if row.get("name") == name]
        if not exact:
            missing.append(name)
            continue
        if len(exact) != 1:
            raise _fail(f"Keycloak group {name!r} was not found uniquely")
        result[name] = _safe_remote_id(exact[0].get("id"), label="group")
    if len(set(result.values())) != len(result):
        raise _fail("Keycloak returned one id for multiple authoritative groups")
    return result, tuple(missing)


def _exact_group_ids(
    client: Any, *, base: str, headers: dict[str, str], names: tuple[str, ...]
) -> dict[str, str]:
    result, missing = _find_exact_group_ids(client, base=base, headers=headers, names=names)
    if missing:
        raise _fail(f"Keycloak group {missing[0]!r} is absent")
    return result


def _delete_created_groups(
    client: Any,
    *,
    base: str,
    headers: dict[str, str],
    groups: dict[str, str],
) -> None:
    for name, group_id in reversed(tuple(groups.items())):
        _prove_group_identity(
            client,
            base=base,
            headers=headers,
            name=name,
            group_id=group_id,
        )
        members = _response_rows(
            client.get(
                f"{base}/admin/realms/ne-pilot/groups/{group_id}/members",
                headers=headers,
            ),
            label="created group members",
        )
        children = _response_rows(
            client.get(
                f"{base}/admin/realms/ne-pilot/groups/{group_id}/children",
                headers=headers,
            ),
            label="created group children",
        )
        if members or children:
            raise _fail(f"created Keycloak group {name!r} gained foreign descendants")
        client.delete(
            f"{base}/admin/realms/ne-pilot/groups/{group_id}", headers=headers
        ).raise_for_status()
        response = client.get(f"{base}/admin/realms/ne-pilot/groups/{group_id}", headers=headers)
        if response.status_code != 404:
            raise _fail(f"created Keycloak group {name!r} was not deleted by exact id")


def _created_group_id(
    response: Any,
    *,
    name: str,
    base: str,
    host_headers: dict[str, str],
) -> str:
    location = response.headers.get("location")
    if not isinstance(location, str) or not location or location != location.strip():
        raise _fail(f"created Keycloak group {name!r} has no canonical Location")
    parsed = urlparse(location)
    expected_authority = host_headers.get("Host", urlparse(base).netloc)
    if (
        parsed.scheme not in ("", "https")
        or (parsed.scheme and parsed.netloc != expected_authority)
        or (not parsed.scheme and parsed.netloc)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise _fail(f"created Keycloak group {name!r} has a foreign Location")
    prefix = "/admin/realms/ne-pilot/groups/"
    if not parsed.path.startswith(prefix) or parsed.path.count("/") != prefix.count("/"):
        raise _fail(f"created Keycloak group {name!r} has a malformed Location")
    return _safe_remote_id(parsed.path.removeprefix(prefix), label="created group")


def _prove_group_identity(
    client: Any,
    *,
    base: str,
    headers: dict[str, str],
    name: str,
    group_id: str,
) -> None:
    response = client.get(f"{base}/admin/realms/ne-pilot/groups/{group_id}", headers=headers)
    response.raise_for_status()
    try:
        document = response.json()
    except (TypeError, ValueError) as exc:
        raise _fail(f"created Keycloak group {name!r} identity response is invalid") from exc
    if (
        not isinstance(document, dict)
        or document.get("id") != group_id
        or document.get("name") != name
    ):
        raise _fail(f"created Keycloak group {name!r} identity was replaced")


def _current_groups(
    client: Any, *, base: str, headers: dict[str, str], user_id: str
) -> dict[str, str]:
    rows = _response_rows(
        client.get(f"{base}/admin/realms/ne-pilot/users/{user_id}/groups", headers=headers),
        label="user groups",
    )
    current: dict[str, str] = {}
    for row in rows:
        group_id = _safe_remote_id(row.get("id"), label="group")
        name = row.get("name")
        if not isinstance(name, str) or not name or group_id in current or name in current.values():
            raise _fail("Keycloak user groups response is ambiguous")
        current[group_id] = name
    return current


def _reconcile_exact_groups(
    client: Any,
    *,
    base: str,
    headers: dict[str, str],
    user_id: str,
    wanted: dict[str, str],
) -> None:
    current = _current_groups(client, base=base, headers=headers, user_id=user_id)
    wanted_ids = set(wanted.values())
    for group_id in sorted(set(current) - wanted_ids):
        client.delete(
            f"{base}/admin/realms/ne-pilot/users/{user_id}/groups/{group_id}",
            headers=headers,
        ).raise_for_status()
    for group_id in sorted(wanted_ids - set(current)):
        client.put(
            f"{base}/admin/realms/ne-pilot/users/{user_id}/groups/{group_id}",
            headers=headers,
        ).raise_for_status()
    verified = _current_groups(client, base=base, headers=headers, user_id=user_id)
    if verified != {group_id: name for name, group_id in wanted.items()}:
        raise _fail("Keycloak persona group reconciliation did not converge exactly")


def _credential_payload(existing: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(existing))
    principals = payload["principals"]
    for username, password in _PERSONA_PASSWORDS.items():
        principals[_CREDENTIAL_KEYS[username]] = {
            "username": username,
            "password": password,
        }
    return payload


def _persona_passwords(credentials: dict[str, Any]) -> dict[str, str]:
    principals = credentials["principals"]
    result: dict[str, str] = {}
    for username, key in _CREDENTIAL_KEYS.items():
        principal = principals.get(key)
        if (
            not isinstance(principal, dict)
            or principal.get("username") != username
            or not isinstance(principal.get("password"), str)
            or not principal["password"]
        ):
            # BOOTSTRAP PRECONDITION.  This reconciler is deliberately narrow: it can
            # only reconcile a persona that ALREADY authenticates, because its
            # compensation guarantee is "restore the exact prior password", which does
            # not exist for a persona that never had one.  A persona seeded by the
            # realm JSON with an empty ``credentials`` array (``meridian-frontline``,
            # by design since the credential rotation) must therefore be provisioned
            # once by ``ne demo up`` -> ``live._provision_keycloak``, which creates it
            # with a complete profile, its exact groups and a password.  Never widen
            # this into a bootstrap: doing so would silently mint authority for a
            # principal whose prior state cannot be restored on failure.
            raise _fail(
                f"credentials have no exact prior password for {username!r}; "
                "provision this persona once with `ne demo up` before reconciling "
                "(this command reconciles existing logins, it never bootstraps one)"
            )
        result[username] = principal["password"]
    return result


def _credential_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True).encode()


def _same_credentials_file(path: Path, expected_identity: tuple[int, int, int, int]) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and _file_identity(info) == expected_identity
    )


def _publish_credentials_bytes(
    path: Path,
    raw: bytes,
    *,
    expected_identity: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    fd = -1
    temporary = ""
    try:
        if not _same_credentials_file(path, expected_identity):
            raise _fail("credentials file changed before publication")
        fd, temporary = tempfile.mkstemp(prefix=".demo-credentials.", dir=path.parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if not _same_credentials_file(path, expected_identity):
            raise _fail("credentials file changed during publication")
        os.replace(temporary, path)
        temporary = ""
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        published, identity = _canonical_file(path, label="credentials file", mode=0o600)
        if published != raw:
            raise _fail("credentials publication readback mismatch")
        return identity
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def _reset_password(
    client: Any,
    *,
    base: str,
    headers: dict[str, str],
    user_id: str,
    password: str,
) -> None:
    client.put(
        f"{base}/admin/realms/ne-pilot/users/{user_id}/reset-password",
        headers=headers,
        json={"type": "password", "value": password, "temporary": False},
    ).raise_for_status()


def _prove_passwords(
    client: Any,
    *,
    base: str,
    host_headers: dict[str, str],
    passwords: dict[str, str],
    failures: list[str] | None = None,
) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for username in _PERSONA_PASSWORDS:
        try:
            response = client.post(
                f"{base}/realms/ne-pilot/protocol/openid-connect/token",
                headers=host_headers,
                data={
                    "grant_type": "password",
                    "client_id": "ne-console",
                    "scope": "openid",
                    "username": username,
                    "password": passwords[username],
                },
            )
            response.raise_for_status()
            token = response.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise ValueError("token response has no bearer")
            tokens[username] = token
        except Exception as exc:
            # The broad Exception member is deliberate: httpx transports and
            # response implementations expose several failure subclasses.  We
            # never disclose a password or bearer in the resulting diagnostic.
            if failures is None:
                raise _fail(f"password grant failed for {username!r}") from exc
            failures.append(f"password grant {username}")
    return tokens


def _prove_effective_authority(
    client: Any,
    *,
    base: str,
    host_headers: dict[str, str],
    tokens: dict[str, str],
) -> None:
    # ``meridian-frontline`` satisfies no coarse console surface, so its exact
    # entitlement list is EMPTY.  Proving that explicitly is the point: it fails the
    # gate on a frontline that authenticates while holding approver/ops authority.
    expected: dict[str, tuple[set[str], list[str]]] = {
        "approver": (set(_APPROVER_GROUPS), ["approvals"]),
        "steward": (set(_STEWARD_GROUPS), ["steward"]),
        "meridian-frontline": (set(_FRONTLINE_GROUPS), []),
    }
    for username, (expected_groups, expected_entitlements) in expected.items():
        response = client.get(
            f"{base}/api/v1/me",
            headers={"Authorization": f"Bearer {tokens[username]}", **host_headers},
        )
        response.raise_for_status()
        try:
            authority = response.json()
        except (TypeError, ValueError) as exc:
            raise _fail(f"effective authority response is invalid for {username!r}") from exc
        groups = authority.get("groups") if isinstance(authority, dict) else None
        if (
            not isinstance(authority, dict)
            or set(authority) != {"id", "groups", "entitlements"}
            or not isinstance(authority.get("id"), str)
            or not authority["id"]
            or not isinstance(groups, list)
            or any(not isinstance(group, str) or not group for group in groups)
            or len(groups) != len(set(groups))
            or set(groups) != expected_groups
            or authority.get("entitlements") != expected_entitlements
        ):
            raise _fail(f"effective authority is not exact for {username!r}")


def _recover_prior_state(
    *,
    client: Any,
    base: str,
    host_headers: dict[str, str],
    headers: dict[str, str],
    existing: _ExistingDemo,
    user_ids: dict[str, str],
    prior_passwords: dict[str, str],
    prior_groups: dict[str, dict[str, str]],
    desired_credentials_raw: bytes,
    created_groups: dict[str, str],
    initially_missing_groups: tuple[str, ...],
) -> None:
    failures: list[str] = []

    for username in _PERSONA_PASSWORDS:
        try:
            _reset_password(
                client,
                base=base,
                headers=headers,
                user_id=user_ids[username],
                password=prior_passwords[username],
            )
        except Exception:
            failures.append(f"password reset {username}")

    for username in _RECONCILED_GROUPS:
        prior = prior_groups[username]
        try:
            _reconcile_exact_groups(
                client,
                base=base,
                headers=headers,
                user_id=user_ids[username],
                wanted={name: group_id for group_id, name in prior.items()},
            )
        except Exception:
            failures.append(f"group recovery {username}")

    try:
        current_raw, current_identity = _canonical_file(
            existing.credentials_path, label="credentials file", mode=0o600
        )
        if current_raw == existing.credentials_raw:
            pass
        elif current_raw == desired_credentials_raw:
            _publish_credentials_bytes(
                existing.credentials_path,
                existing.credentials_raw,
                expected_identity=current_identity,
            )
        else:
            raise _fail("credentials changed to an unknown state during recovery")
        recovered_raw, _identity = _canonical_file(
            existing.credentials_path, label="credentials file", mode=0o600
        )
        if recovered_raw != existing.credentials_raw:
            raise _fail("prior credentials were not restored exactly")
    except Exception:
        failures.append("credentials file recovery")

    _prove_passwords(
        client,
        base=base,
        host_headers=host_headers,
        passwords=prior_passwords,
        failures=failures,
    )
    try:
        _delete_created_groups(
            client,
            base=base,
            headers=headers,
            groups=created_groups,
        )
    except Exception:
        failures.append("created group recovery")
    try:
        _unexpected, absent = _find_exact_group_ids(
            client,
            base=base,
            headers=headers,
            names=initially_missing_groups,
        )
        if absent != initially_missing_groups:
            raise _fail("an initially absent authoritative group remains after recovery")
    except Exception:
        failures.append("initially absent group recovery")
    if failures:
        raise PersonaRecoveryUnproven(failures)


def reconcile_demo_personas(workdir: Path) -> Path:
    """Reconcile only existing Keycloak personas and publish matching credentials."""

    existing = _load_existing_demo(workdir)
    prior_passwords = _persona_passwords(existing.credentials)
    desired_credentials = _credential_payload(existing.credentials)
    desired_credentials_raw = _credential_bytes(desired_credentials)
    from ne.cli.demo.live import _onbox_dial, _onbox_transport
    from ne.cli.pilotcfg import resolve_required_secrets

    resolved = resolve_required_secrets(existing.bundle)
    admin_password = resolved["NE_PILOT_KEYCLOAK_ADMIN_PASSWORD"]
    base, host_headers = _onbox_dial(existing.hostname, 8443, existing.external_url)
    app_base, app_headers = _onbox_dial(
        existing.hostname, existing.https_port, existing.external_url
    )

    import httpx

    with httpx.Client(
        verify=False,
        timeout=30.0,
        transport=_onbox_transport(existing.hostname, existing.external_url),
    ) as client:
        token_response = client.post(
            f"{base}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "admin",
                "password": admin_password,
            },
            headers=host_headers,
        )
        token_response.raise_for_status()
        try:
            token = token_response.json()["access_token"]
        except (KeyError, TypeError, ValueError) as exc:
            raise _fail("Keycloak admin token response is invalid") from exc
        if not isinstance(token, str) or not token:
            raise _fail("Keycloak admin token response has no bearer")
        headers = {"Authorization": f"Bearer {token}", **host_headers}

        user_ids = {
            username: _exact_user_id(client, base=base, headers=headers, username=username)
            for username in _PERSONA_PASSWORDS
        }
        _prove_passwords(
            client,
            base=base,
            host_headers=host_headers,
            passwords=prior_passwords,
        )
        prior_groups = {
            username: _current_groups(
                client,
                base=base,
                headers=headers,
                user_id=user_ids[username],
            )
            for username in _RECONCILED_GROUPS
        }
        authoritative_groups = tuple(
            dict.fromkeys(name for names in _RECONCILED_GROUPS.values() for name in names)
        )
        _existing_groups, missing_groups = _find_exact_group_ids(
            client, base=base, headers=headers, names=authoritative_groups
        )
        created_groups: dict[str, str] = {}
        try:
            for name in missing_groups:
                response = client.post(
                    f"{base}/admin/realms/ne-pilot/groups",
                    headers=headers,
                    json={"name": name},
                )
                if response.status_code == 201:
                    group_id = _created_group_id(
                        response,
                        name=name,
                        base=base,
                        host_headers=host_headers,
                    )
                    created_groups[name] = group_id
                    _prove_group_identity(
                        client,
                        base=base,
                        headers=headers,
                        name=name,
                        group_id=group_id,
                    )
                elif response.status_code == 409:
                    raise _fail(f"initially absent Keycloak group {name!r} appeared concurrently")
                else:
                    response.raise_for_status()
            group_ids = _exact_group_ids(
                client,
                base=base,
                headers=headers,
                names=authoritative_groups,
            )
            for username, wanted_names in _RECONCILED_GROUPS.items():
                _reconcile_exact_groups(
                    client,
                    base=base,
                    headers=headers,
                    user_id=user_ids[username],
                    wanted={name: group_ids[name] for name in wanted_names},
                )
            for username, password in _PERSONA_PASSWORDS.items():
                _reset_password(
                    client,
                    base=base,
                    headers=headers,
                    user_id=user_ids[username],
                    password=password,
                )
            _publish_credentials_bytes(
                existing.credentials_path,
                desired_credentials_raw,
                expected_identity=existing.credentials_identity,
            )
            desired_tokens = _prove_passwords(
                client,
                base=base,
                host_headers=host_headers,
                passwords=_PERSONA_PASSWORDS,
            )
            _prove_effective_authority(
                client,
                base=app_base,
                host_headers=app_headers,
                tokens=desired_tokens,
            )
        except Exception as exc:
            try:
                _recover_prior_state(
                    client=client,
                    base=base,
                    host_headers=host_headers,
                    headers=headers,
                    existing=existing,
                    user_ids=user_ids,
                    prior_passwords=prior_passwords,
                    prior_groups=prior_groups,
                    desired_credentials_raw=desired_credentials_raw,
                    created_groups=created_groups,
                    initially_missing_groups=missing_groups,
                )
            except PersonaRecoveryUnproven:
                raise
            raise PersonaReconciliationSafelyRefused() from exc
    return existing.credentials_path
