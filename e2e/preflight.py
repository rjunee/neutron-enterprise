#!/usr/bin/env python3
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Preflight for the demo-scenario E2E suite: is a run worth paying for?

Runs BEFORE any model dollar is spent (first step of the e2e-scenarios
workflow; also called by e2e/run.sh). It answers exactly two questions, both
owner-mandated (2026-08-29):

1. **Is the demo up?** ``https://<host>/`` must return 200 with a non-empty
   body. An EMPTY 200 is the known Caddy wrong-Host-header signature and
   counts as down.
2. **Is an external GPU lane connected?** The owner does not keep the GPU up
   ("it costs money"), and its address is NOT stable — so nothing here names
   an IP. Instead the deployment's own governed surface answers:
   ``GET /api/v1/models/serving`` (ops-gated) reports, for each probeable
   private lane, the endpoint it is ROUTED to and what that endpoint says it
   serves. The lane counts as an external GPU only when ALL hold:

   * ``state == "ok"`` — the deployment's own probe reached it;
   * ``serves_configured_model`` — IDENTITY, not address, is the signal: the
     endpoint's served model id matches the configured ``litellm_model``. A
     fallback to the local ``vllm-cpu`` container serves a different (small)
     model and fails this;
   * the endpoint host is not loopback and not an in-compose service name (a
     dotless hostname like ``vllm``/``vllm-cpu``): a local address for the
     private lane is itself sufficient evidence there is no external GPU.
     A routable VPN/VPC IP or DNS name passes — no specific address is
     assumed.

Exit codes — three distinct outcomes, never collapsed:
  0  proceed (both checks passed; evidence printed)
  3  SKIP, with the deciding reason on stdout as ``SKIP: ...`` (demo down /
     GPU absent / env not configured). The caller renders this as a skipped
     run with the reason — never as failure, never as a vacuous pass.
  1  the preflight itself is broken (e.g. credentials present but rejected)
     — a real failure someone must look at, NOT a skip.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

_LOOPBACK_HOSTS = {"localhost", "::1", "host.docker.internal"}


def _apply_resolve_override() -> None:
    """``NE_E2E_RESOLVE_TO``: resolve the deployment hostnames to a fixed IP.

    On the demo box itself the API port (9443) binds to 127.0.0.1 ONLY while
    the public DNS name resolves to the public interface — the same reason the
    S9-trials driver carried a getaddrinfo override. Box-side invocations set
    ``NE_E2E_RESOLVE_TO=127.0.0.1``; the hostname (and therefore TLS SNI,
    which Caddy requires) is preserved, only the address is pinned.
    """
    target = os.environ.get("NE_E2E_RESOLVE_TO", "").strip()
    if not target:
        return
    hosts = {
        urllib.parse.urlsplit(os.environ.get(name, "")).hostname
        for name in ("NE_E2E_API_BASE", "NE_E2E_KEYCLOAK_BASE", "NE_E2E_CONSOLE_BASE")
    }
    hosts.discard(None)
    original = socket.getaddrinfo

    def patched(host: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if host in hosts:
            host = target
        return original(host, *args, **kwargs)  # type: ignore[arg-type]

    socket.getaddrinfo = patched  # type: ignore[assignment]


def _skip(reason: str) -> None:
    print(f"SKIP: {reason}")
    sys.exit(3)


def _fail(reason: str) -> None:
    print(f"PREFLIGHT ERROR: {reason}", file=sys.stderr)
    sys.exit(1)


def _context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if os.environ.get("NE_E2E_INSECURE_TLS") == "1":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 20.0,
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, context=_context(), timeout=timeout) as response:
        return response.status, response.read()


def _console_origin(api_base: str) -> str:
    configured = os.environ.get("NE_E2E_CONSOLE_BASE", "").strip()
    if configured:
        return configured.rstrip("/")
    host = urllib.parse.urlsplit(api_base).hostname or ""
    return f"https://{host}"


def _check_demo_up(api_base: str) -> None:
    origin = _console_origin(api_base)
    try:
        status, body = _get(origin + "/")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        _skip(f"demo is DOWN: {origin}/ unreachable ({type(exc).__name__})")
        raise AssertionError from exc  # unreachable
    if status != 200:
        _skip(f"demo is DOWN: {origin}/ returned HTTP {status}")
    if not body.strip():
        _skip(
            f"demo looks DOWN: {origin}/ returned an EMPTY 200 — the known Caddy "
            "wrong-Host-header signature, not a served console"
        )
    print(f"demo up: {origin}/ -> 200, {len(body)} bytes")


def _ops_token(keycloak_base: str, realm: str, user: str, password: str) -> str:
    url = f"{keycloak_base}/realms/{realm}/protocol/openid-connect/token"
    payload = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": "ne-console",
            "scope": "openid",
            "username": user,
            "password": password,
        }
    ).encode()
    try:
        _, body = _get(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except urllib.error.HTTPError as exc:
        # Credentials present but rejected is a CONFIG BREAK, not a down box.
        _fail(f"Keycloak refused the ops credential (HTTP {exc.code}) — fix the secrets")
        raise AssertionError from exc  # unreachable
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        _skip(f"demo is DOWN: Keycloak unreachable ({type(exc).__name__})")
        raise AssertionError from exc  # unreachable
    return str(json.loads(body)["access_token"])


def _host_is_local(endpoint: str) -> str | None:
    """Non-None = why this endpoint host cannot be an external GPU."""
    host = urllib.parse.urlsplit(endpoint).hostname or ""
    if host in _LOOPBACK_HOSTS or host.startswith("127."):
        return f"loopback host {host!r}"
    if "." not in host and ":" not in host:
        return f"in-compose service name {host!r} (dotless hostname = local container)"
    return None


def _check_external_gpu(api_base: str, token: str) -> None:
    try:
        _, body = _get(
            f"{api_base}/api/v1/models/serving",
            headers={"Authorization": f"Bearer {token}"},
            timeout=45.0,
        )
    except urllib.error.HTTPError as exc:
        _fail(f"/v1/models/serving refused (HTTP {exc.code}) with a valid ops token")
        raise AssertionError from exc  # unreachable
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        _skip(
            f"the API is unreachable from this network ({type(exc).__name__}) — the box "
            "may be down, or this network simply cannot reach the API port (on the demo "
            "box, 9443 binds to 127.0.0.1 only: box-side runs need "
            "NE_E2E_RESOLVE_TO=127.0.0.1, and GitHub-hosted runners cannot reach it at "
            "all until the owner exposes it or adds a tunnel/self-hosted runner)"
        )
        raise AssertionError from exc  # unreachable
    lanes = json.loads(body).get("lanes", [])
    private = [
        lane
        for lane in lanes
        if str(lane.get("lane", "")).startswith("private")
        or str(lane.get("model_id", "")) == "private-default"
    ]
    if not private:
        _skip(
            "no external GPU: the deployment reports NO probeable private lane at all "
            "(GET /v1/models/serving returned no private rows)"
        )
    verdicts: list[str] = []
    for lane in private:
        model_id = lane.get("model_id")
        endpoint = str(lane.get("endpoint", ""))
        state = lane.get("state")
        if state != "ok":
            verdicts.append(f"{model_id}: probe state {state!r} (endpoint {endpoint})")
            continue
        if lane.get("serves_configured_model") is not True:
            verdicts.append(
                f"{model_id}: IDENTITY MISMATCH — endpoint {endpoint} serves "
                f"{lane.get('served_models')} but the config expects "
                f"{lane.get('configured_model')!r} (cpu-vllm fallback signature)"
            )
            continue
        local = _host_is_local(endpoint)
        if local is not None:
            verdicts.append(
                f"{model_id}: {local} — a local private lane means no external GPU "
                "(cpu-vllm is far too slow for the suite)"
            )
            continue
        print(
            f"external GPU lane connected: {model_id} @ {endpoint} serving "
            f"{lane.get('configured_model')!r} "
            f"(observed context window {lane.get('observed_context_window')}, "
            f"latency {lane.get('latency_ms')} ms, engine {lane.get('engine_version')})"
        )
        return
    _skip("no external GPU lane is connected: " + "; ".join(verdicts))


def main() -> None:
    api_base = os.environ.get("NE_E2E_API_BASE", "").strip().rstrip("/")
    keycloak_base = os.environ.get("NE_E2E_KEYCLOAK_BASE", "").strip().rstrip("/")
    user = os.environ.get("NE_E2E_OPS_USER", "").strip()
    password = os.environ.get("NE_E2E_OPS_PASSWORD", "").strip()
    if not api_base or not keycloak_base or not user or not password:
        _skip(
            "NE_E2E_* target/credentials are not configured — the suite did not run "
            "(configure the secrets, or run e2e/run.sh on the deployment box)"
        )
    realm = os.environ.get("NE_E2E_REALM", "ne-pilot")
    _apply_resolve_override()
    _check_demo_up(api_base)
    token = _ops_token(keycloak_base, realm, user, password)
    _check_external_gpu(api_base, token)
    print("preflight: PROCEED")


if __name__ == "__main__":
    main()
