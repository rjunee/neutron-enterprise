# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""OpenBaoSecretStore — the prod-profile :class:`~ne.contracts.secrets.SecretStore`.

Speaks the KV-v2 REST API via stdlib ``urllib`` in ``asyncio.to_thread`` — ZERO
new Python dependencies. ``httpx`` was evaluated and rejected (see the README
"dependency intake" section): its stewardship moved to Pydantic as "httpx2"
mid-2026 while the original ``encode/httpx`` package's security-update path
became uncertain, so a fixed, six-call REST client over stdlib ``urllib`` wins
on supply-chain surface. All cryptography is delegated to OpenBao itself —
this module implements zero cryptography.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from ne.contracts.errors import SecretNotFoundError
from ne.contracts.secrets import SecretVersion
from ne.secrets.config import SecretsSettings
from ne.secrets.errors import SecretsBackendError


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse cross-host redirects so the X-Vault-Token is never forwarded away.

    OpenBao runs behind loopback/in-VPC; a redirect to another (scheme, host) is
    treated as hostile — following it would replay the ``X-Vault-Token`` header
    (and any secret payload) to an unsanctioned host (C2/C12). We raise a typed
    :class:`SecretsBackendError` instead. The target URL is NOT interpolated into
    the message (it could embed attacker-chosen data / a credential).
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        target = urllib.parse.urlparse(urllib.parse.urljoin(req.full_url, newurl))
        origin = urllib.parse.urlparse(req.full_url)
        if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
            raise SecretsBackendError(
                f"OpenBao redirect refused: cross-host redirect (status {code}) would "
                "forward X-Vault-Token to another host"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Built ONCE: ``ProxyHandler({})`` (an INSTANCE with an empty map) hard-disables
# env HTTP(S)_PROXY/ALL_PROXY so the token + secret payloads are never sent in
# cleartext to an unsanctioned proxy (C2/C12); the redirect handler refuses
# cross-host redirects so the token is never replayed to a foreign host.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SameHostRedirectHandler())


def _parse_rfc3339(ts: str) -> datetime:
    """Parse an OpenBao RFC3339 timestamp, which may carry nanosecond fractions.

    ``datetime.fromisoformat`` rejects >6-digit fractional seconds (e.g.
    ``2026-07-03T10:20:30.123456789Z``), so a fractional part is truncated to
    microseconds before parsing. Timestamps without a fractional part parse
    directly after swapping the ``Z`` suffix for an explicit UTC offset.
    """
    if "." in ts:
        base, rest = ts.split(".", 1)
        frac = rest.rstrip("Z")[:6]
        return datetime.fromisoformat(f"{base}.{frac}+00:00")
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class OpenBaoSecretStore:
    """KV-v2-semantics :class:`~ne.contracts.secrets.SecretStore` over OpenBao.

    The constructor performs NO network I/O — connection failures surface only
    when a Protocol method is actually called.
    """

    def __init__(
        self,
        url: str,
        token: str,
        mount: str = "secret",
        base_path: str = "",
        timeout: float = 5.0,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._mount = mount
        self._base_path = base_path
        self._timeout = timeout

    @classmethod
    def from_settings(cls, settings: SecretsSettings) -> OpenBaoSecretStore:
        cfg = settings.openbao
        token = os.environ.get(cfg.token_env_var)
        if not token:
            raise SecretsBackendError(f"environment variable {cfg.token_env_var} is not set")
        return cls(
            url=cfg.url,
            token=token,
            mount=cfg.mount,
            base_path=cfg.base_path,
            timeout=cfg.timeout_seconds,
        )

    def _full(self, path: str) -> str:
        return f"{self._base_path}/{path}" if self._base_path else path

    def _request_sync(
        self,
        method: str,
        api_path: str,
        body: dict[str, Any] | None = None,
        query: str = "",
    ) -> tuple[int, dict[str, Any] | None]:
        full_url = f"{self._url}{api_path}"
        if query:
            full_url = f"{full_url}?{query}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            full_url,
            data=data,
            headers={"X-Vault-Token": self._token, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with _OPENER.open(req, timeout=self._timeout) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise SecretsBackendError(f"OpenBao request failed: {exc}") from exc
        parsed = json.loads(raw) if raw else None
        return status, parsed

    def _get_metadata_sync(self, path: str) -> dict[str, Any]:
        full = self._full(path)
        status, body = self._request_sync("GET", f"/v1/{self._mount}/metadata/{full}")
        if status == 404:
            raise SecretNotFoundError(f"no secret at path '{path}'")
        if status != 200 or body is None:
            raise SecretsBackendError(f"OpenBao metadata fetch failed: status {status}")
        data: dict[str, Any] = body["data"]
        return data

    def _resolve_targets(
        self, md: dict[str, Any], path: str, versions: Sequence[int] | None
    ) -> list[int]:
        if versions is None:
            return [md["current_version"]]
        wanted = list(versions)
        vmap: dict[str, Any] = md.get("versions", {})
        missing = [v for v in wanted if str(v) not in vmap]
        if missing:
            raise SecretNotFoundError(f"no versions {sorted(missing)} at path '{path}'")
        return wanted

    def _check_not_destroyed(self, md: dict[str, Any], path: str, targets: list[int]) -> None:
        vmap: dict[str, Any] = md.get("versions", {})
        for v in targets:
            if vmap.get(str(v), {}).get("destroyed"):
                raise SecretNotFoundError(f"version {v} at path '{path}' is destroyed")

    def _list_sync(self, list_path: str) -> list[str]:
        clean = list_path.rstrip("/")
        api_path = f"/v1/{self._mount}/metadata/{clean}" if clean else f"/v1/{self._mount}/metadata"
        status, body = self._request_sync("GET", api_path, query="list=true")
        if status == 404:
            return []
        if status != 200 or body is None:
            raise SecretsBackendError(f"OpenBao list failed: status {status} for '{clean}'")
        keys: list[str] = body.get("data", {}).get("keys", [])
        results: list[str] = []
        for key in keys:
            full_key = f"{clean}/{key}" if clean else key
            if key.endswith("/"):
                results.extend(self._list_sync(full_key))
            else:
                results.append(full_key)
        return results

    async def read(self, path: str, *, version: int | None = None) -> SecretVersion:
        full = self._full(path)
        query = f"version={version}" if version is not None else ""
        status, body = await asyncio.to_thread(
            self._request_sync, "GET", f"/v1/{self._mount}/data/{full}", None, query
        )
        if status == 200 and body is not None:
            data = body["data"]
            meta = data["metadata"]
            return SecretVersion(
                path=path,
                version=meta["version"],
                data=data["data"],
                created_at=_parse_rfc3339(meta["created_time"]),
                deleted=False,
            )
        # Fail CLOSED: ONLY a 404 means confirmed absence. Every other non-200
        # (403, 429 standby/rate-limit, 412 stale-index, 400, 5xx, or a 200 with
        # an empty body) is a backend/transient error — reporting it as "not
        # found" would let existence-driven callers treat a live credential as
        # revoked (or overwrite a real secret once the backend recovers).
        if status == 404:
            raise SecretNotFoundError(f"no secret at path '{path}'")
        raise SecretsBackendError(f"OpenBao read failed: status {status}")

    async def write(self, path: str, data: Mapping[str, str]) -> SecretVersion:
        full = self._full(path)
        status, body = await asyncio.to_thread(
            self._request_sync,
            "POST",
            f"/v1/{self._mount}/data/{full}",
            {"data": dict(data)},
        )
        if status != 200 or body is None:
            raise SecretsBackendError(f"OpenBao write failed: status {status}")
        d = body["data"]
        return SecretVersion(
            path=path,
            version=d["version"],
            data=dict(data),
            created_at=_parse_rfc3339(d["created_time"]),
            deleted=False,
        )

    async def delete(self, path: str, *, versions: Sequence[int] | None = None) -> None:
        md = await asyncio.to_thread(self._get_metadata_sync, path)
        targets = self._resolve_targets(md, path, versions)
        full = self._full(path)
        if versions is None:
            status, _ = await asyncio.to_thread(
                self._request_sync, "DELETE", f"/v1/{self._mount}/data/{full}"
            )
        else:
            status, _ = await asyncio.to_thread(
                self._request_sync,
                "POST",
                f"/v1/{self._mount}/delete/{full}",
                {"versions": targets},
            )
        if status not in (200, 204):
            raise SecretsBackendError(f"OpenBao delete failed: status {status}")

    async def undelete(self, path: str, versions: Sequence[int]) -> None:
        md = await asyncio.to_thread(self._get_metadata_sync, path)
        targets = self._resolve_targets(md, path, versions)
        self._check_not_destroyed(md, path, targets)
        full = self._full(path)
        status, _ = await asyncio.to_thread(
            self._request_sync,
            "POST",
            f"/v1/{self._mount}/undelete/{full}",
            {"versions": targets},
        )
        if status not in (200, 204):
            raise SecretsBackendError(f"OpenBao undelete failed: status {status}")

    async def destroy(self, path: str, versions: Sequence[int]) -> None:
        md = await asyncio.to_thread(self._get_metadata_sync, path)
        targets = self._resolve_targets(md, path, versions)
        full = self._full(path)
        status, _ = await asyncio.to_thread(
            self._request_sync,
            "POST",
            f"/v1/{self._mount}/destroy/{full}",
            {"versions": targets},
        )
        if status not in (200, 204):
            raise SecretsBackendError(f"OpenBao destroy failed: status {status}")

    async def list_secrets(self, prefix: str) -> Sequence[str]:
        full_prefix = self._full(prefix)
        results = await asyncio.to_thread(self._list_sync, full_prefix)
        if self._base_path:
            strip = f"{self._base_path}/"
            results = [r[len(strip) :] if r.startswith(strip) else r for r in results]
        return sorted(results)
