# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Below-the-seam unit tests for OpenBaoSecretStore.

Only ``_request_sync`` is monkeypatched here — everything above that seam
(response mapping, metadata pre-checks, timestamp parsing, path joining) is
exercised for real. The real HTTP seam is covered, skip-gated, by
test_openbao_conformance.py against a live OpenBao instance.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ne.contracts.errors import SecretNotFoundError
from ne.secrets.errors import SecretsBackendError
from ne.secrets.openbao import OpenBaoSecretStore, _parse_rfc3339


def _run(coro):
    return asyncio.run(coro)


def test_constructor_performs_zero_io() -> None:
    # An unroutable URL must not raise at construction time.
    store = OpenBaoSecretStore(url="http://192.0.2.1:1", token="t")
    assert store is not None


def test_parse_rfc3339_nine_digit_fraction() -> None:
    dt = _parse_rfc3339("2026-07-03T10:20:30.123456789Z")
    assert dt.isoformat() == "2026-07-03T10:20:30.123456+00:00"


def test_parse_rfc3339_six_digit_fraction() -> None:
    dt = _parse_rfc3339("2026-07-03T10:20:30.123456Z")
    assert dt.isoformat() == "2026-07-03T10:20:30.123456+00:00"


def test_parse_rfc3339_no_fraction() -> None:
    dt = _parse_rfc3339("2026-07-03T10:20:30Z")
    assert dt.isoformat() == "2026-07-03T10:20:30+00:00"


def test_full_path_joining_with_and_without_base_path() -> None:
    bare = OpenBaoSecretStore(url="http://x", token="t")
    assert bare._full("app/db") == "app/db"

    scoped = OpenBaoSecretStore(url="http://x", token="t", base_path="ne-secrets-kit/abc")
    assert scoped._full("app/db") == "ne-secrets-kit/abc/app/db"


def test_read_maps_404_to_secret_not_found() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t")
    store._request_sync = lambda method, api_path, body=None, query="": (404, None)  # type: ignore[method-assign]

    with pytest.raises(SecretNotFoundError):
        _run(store.read("app/db"))


@pytest.mark.parametrize("status", [500, 503, 403, 429, 400, 412])
def test_read_maps_all_non_200_non_404_to_backend_error(status: int) -> None:
    # Fail-closed: ONLY 404 means "not found". Every other non-200 (incl. the
    # transient 429 standby/rate-limit, 400, 412 stale-index) is a backend error
    # — reverting this to the old fail-open mapping makes 429/400/412 fail here
    # because they would raise SecretNotFoundError instead.
    store = OpenBaoSecretStore(url="http://x", token="t")
    store._request_sync = lambda method, api_path, body=None, query="", s=status: (s, None)  # type: ignore[method-assign]

    with pytest.raises(SecretsBackendError):
        _run(store.read("app/db"))


def test_read_maps_200_to_secret_version() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t")

    def fake(method: str, api_path: str, body: dict[str, Any] | None = None, query: str = ""):
        assert method == "GET"
        assert api_path == "/v1/secret/data/app/db"
        return 200, {
            "data": {
                "data": {"password": "s3cr3t"},
                "metadata": {"version": 4, "created_time": "2026-07-03T10:20:30.123456789Z"},
            }
        }

    store._request_sync = fake  # type: ignore[method-assign]
    v = _run(store.read("app/db"))
    assert v.version == 4
    assert v.data["password"] == "s3cr3t"


def test_write_maps_200_to_secret_version() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t")

    def fake(method: str, api_path: str, body: dict[str, Any] | None = None, query: str = ""):
        assert method == "POST"
        assert body == {"data": {"password": "s3cr3t"}}
        return 200, {"data": {"version": 1, "created_time": "2026-07-03T10:20:30Z"}}

    store._request_sync = fake  # type: ignore[method-assign]
    v = _run(store.write("app/db", {"password": "s3cr3t"}))
    assert v.version == 1
    assert v.data["password"] == "s3cr3t"


def test_write_non_200_raises_backend_error() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t")
    store._request_sync = lambda method, api_path, body=None, query="": (500, None)  # type: ignore[method-assign]
    with pytest.raises(SecretsBackendError):
        _run(store.write("app/db", {"k": "v"}))


def test_delete_none_resolves_current_version_and_deletes_latest() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t")
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake(method: str, api_path: str, body: dict[str, Any] | None = None, query: str = ""):
        calls.append((method, api_path, body))
        if method == "GET":
            return 200, {"data": {"current_version": 3, "versions": {"3": {"destroyed": False}}}}
        return 204, None

    store._request_sync = fake  # type: ignore[method-assign]
    _run(store.delete("app/db"))

    assert calls[0] == ("GET", "/v1/secret/metadata/app/db", None)
    assert calls[1] == ("DELETE", "/v1/secret/data/app/db", None)


def test_delete_specific_versions_uses_delete_endpoint() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t")
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake(method: str, api_path: str, body: dict[str, Any] | None = None, query: str = ""):
        calls.append((method, api_path, body))
        if method == "GET":
            return 200, {
                "data": {
                    "current_version": 3,
                    "versions": {"1": {"destroyed": False}, "2": {"destroyed": False}},
                }
            }
        return 204, None

    store._request_sync = fake  # type: ignore[method-assign]
    _run(store.delete("app/db", versions=[1, 2]))

    assert calls[1] == ("POST", "/v1/secret/delete/app/db", {"versions": [1, 2]})


def test_delete_missing_path_raises_not_found() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t")
    store._request_sync = lambda method, api_path, body=None, query="": (404, None)  # type: ignore[method-assign]
    with pytest.raises(SecretNotFoundError):
        _run(store.delete("app/nope"))


def test_undelete_prechecks_metadata_and_raises_for_destroyed_version() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t")

    def fake(method: str, api_path: str, body: dict[str, Any] | None = None, query: str = ""):
        if method == "GET":
            return 200, {
                "data": {
                    "current_version": 2,
                    "versions": {"1": {"destroyed": True}, "2": {"destroyed": False}},
                }
            }
        raise AssertionError("undelete POST must not be reached for a destroyed version")

    store._request_sync = fake  # type: ignore[method-assign]
    with pytest.raises(SecretNotFoundError):
        _run(store.undelete("app/db", versions=[1]))


def test_undelete_missing_version_raises_not_found() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t")
    store._request_sync = lambda method, api_path, body=None, query="": (  # type: ignore[method-assign]
        200,
        {"data": {"current_version": 1, "versions": {"1": {"destroyed": False}}}},
    )
    with pytest.raises(SecretNotFoundError):
        _run(store.undelete("app/db", versions=[9]))


def test_undelete_succeeds_for_non_destroyed_version() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t")
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake(method: str, api_path: str, body: dict[str, Any] | None = None, query: str = ""):
        calls.append((method, api_path, body))
        if method == "GET":
            return 200, {"data": {"current_version": 1, "versions": {"1": {"destroyed": False}}}}
        return 200, None

    store._request_sync = fake  # type: ignore[method-assign]
    _run(store.undelete("app/db", versions=[1]))
    assert calls[1] == ("POST", "/v1/secret/undelete/app/db", {"versions": [1]})


def test_destroy_uses_destroy_endpoint() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t")
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake(method: str, api_path: str, body: dict[str, Any] | None = None, query: str = ""):
        calls.append((method, api_path, body))
        if method == "GET":
            return 200, {"data": {"current_version": 1, "versions": {"1": {"destroyed": False}}}}
        return 204, None

    store._request_sync = fake  # type: ignore[method-assign]
    _run(store.destroy("app/db", versions=[1]))
    assert calls[1] == ("POST", "/v1/secret/destroy/app/db", {"versions": [1]})


def test_list_secrets_joins_recurses_and_returns_404_as_empty() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t")

    def fake(method: str, api_path: str, body: dict[str, Any] | None = None, query: str = ""):
        if api_path == "/v1/secret/metadata/app" and query == "list=true":
            return 200, {"data": {"keys": ["db", "sub/"]}}
        if api_path == "/v1/secret/metadata/app/sub" and query == "list=true":
            return 200, {"data": {"keys": ["cache"]}}
        return 404, None

    store._request_sync = fake  # type: ignore[method-assign]
    names = _run(store.list_secrets("app/"))
    assert set(names) == {"app/db", "app/sub/cache"}


def test_list_secrets_strips_base_path() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t", base_path="ne-secrets-kit/abc")

    def fake(method: str, api_path: str, body: dict[str, Any] | None = None, query: str = ""):
        if api_path == "/v1/secret/metadata/ne-secrets-kit/abc/app" and query == "list=true":
            return 200, {"data": {"keys": ["db", "cache"]}}
        return 404, None

    store._request_sync = fake  # type: ignore[method-assign]
    names = _run(store.list_secrets("app/"))
    assert set(names) == {"app/db", "app/cache"}


def test_list_secrets_404_returns_empty_list() -> None:
    store = OpenBaoSecretStore(url="http://x", token="t")
    store._request_sync = lambda method, api_path, body=None, query="": (404, None)  # type: ignore[method-assign]
    assert _run(store.list_secrets("nope/")) == []
