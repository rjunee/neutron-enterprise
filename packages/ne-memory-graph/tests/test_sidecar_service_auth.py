# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral FIX-1 + FIX-3 regression through the REAL sidecar FastAPI app.

FIX-1 (auth enforcement): with ``NE_MEMORY_GRAPH_SERVICE_TOKEN`` set, an
unauthenticated (or wrong-bearer) POST to ``/episodes`` AND ``/search`` is
REJECTED with 401; the correct bearer is accepted. Proven by real round-trips
through ``service.app`` — not string inspection.

FIX-3 (one-secret llm-facade bearer): ``_resolve_llm_api_key()`` prefers an
explicit ``NE_MEMORY_GRAPH_LLM_API_KEY`` override, then falls back to the
``NE_MEMORY_GRAPH_SERVICE_TOKEN`` (the bearer the frontier-lane llm_facade
requires), then ``'sk-none'`` — so one documented secret wires the frontier lane.

The sidecar's ``service.py`` module-level imports are stdlib + fastapi ONLY
(graphiti_core / neo4j imports live inside ``_build_graphiti`` and ``post_episode``
and are never triggered here), so the module loads and serves platform-side.

FLAG-4 technique note: we construct ``TestClient(module.app)`` WITHOUT the
context manager so starlette never runs the lifespan (and thus never calls
``_build_graphiti`` / imports graphiti_core). ``_check_auth`` reads the token from
the environment per-request, so ``monkeypatch.setenv`` after import still takes
effect. If a future starlette runs lifespan even outside the context manager,
the fallback is to monkeypatch ``module.app.router.lifespan_context`` to a no-op.
"""

from __future__ import annotations

import importlib.util
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from ne.memory_graph.sidecar import default_graphiti_env_dir


def _load_service_module() -> ModuleType:
    """Load ``graphiti_env/service.py`` as a standalone module (stdlib+fastapi only)."""
    spec = importlib.util.spec_from_file_location(
        "_ne083_service_under_test", default_graphiti_env_dir() / "service.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeDriver:
    """Truthy /episodes lookup → idempotent 'already-ingested' branch (no graphiti import)."""

    async def execute_query(self, query: str, **params: object) -> tuple[object, None, None]:
        return ([{"e": {}}], None, None)


class _FakeGraphiti:
    async def search(
        self, query: str, group_ids: object = None, num_results: int = 0
    ) -> list[object]:
        return []


@pytest.fixture
def service() -> ModuleType:
    return _load_service_module()


def _inject_fakes(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module._state, "driver", _FakeDriver())
    monkeypatch.setattr(module._state, "graphiti", _FakeGraphiti())


def test_episodes_without_token_is_401_when_token_set(
    service: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NE_MEMORY_GRAPH_SERVICE_TOKEN", "sekrit")
    client = TestClient(service.app)
    resp = client.post("/episodes", json={"group_id": "a", "name": "n", "content": "c"})
    assert resp.status_code == 401


def test_search_without_token_is_401_when_token_set(
    service: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NE_MEMORY_GRAPH_SERVICE_TOKEN", "sekrit")
    client = TestClient(service.app)
    resp = client.post("/search", json={"query": "q"})
    assert resp.status_code == 401


def test_wrong_bearer_is_401(service: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_MEMORY_GRAPH_SERVICE_TOKEN", "sekrit")
    client = TestClient(service.app)
    headers = {"Authorization": "Bearer wrong"}
    assert (
        client.post(
            "/episodes",
            json={"group_id": "a", "name": "n", "content": "c"},
            headers=headers,
        ).status_code
        == 401
    )
    assert client.post("/search", json={"query": "q"}, headers=headers).status_code == 401


def test_correct_bearer_is_accepted_on_episodes_and_search(
    service: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NE_MEMORY_GRAPH_SERVICE_TOKEN", "sekrit")
    _inject_fakes(service, monkeypatch)
    client = TestClient(service.app)
    headers = {"Authorization": "Bearer sekrit"}

    ep = client.post(
        "/episodes",
        json={"group_id": "a", "name": "n", "content": "c"},
        headers=headers,
    )
    assert ep.status_code == 200
    assert ep.json()["status"] == "already-ingested"

    se = client.post("/search", json={"query": "q"}, headers=headers)
    assert se.status_code == 200
    assert se.json() == {"hits": []}


_INSECURE_FLAG = "NE_MEMORY_GRAPH_ALLOW_INSECURE_NO_TOKEN"


def test_no_token_configured_allows_local_dev(
    service: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Local-dev mode is now OPT-IN (fail-closed by default): an empty token is
    # only accepted when the explicit dev escape hatch is set.
    monkeypatch.delenv("NE_MEMORY_GRAPH_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv(_INSECURE_FLAG, "1")
    _inject_fakes(service, monkeypatch)
    client = TestClient(service.app)
    resp = client.post("/search", json={"query": "q"})
    assert resp.status_code == 200
    assert resp.json() == {"hits": []}


def test_empty_token_returns_401_on_all_routes(
    service: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail-closed default: no token AND no insecure flag → every route 401s.
    # TestClient WITHOUT the context manager so the lifespan (startup refusal)
    # never runs; we exercise the per-request _check_auth guard directly.
    monkeypatch.delenv("NE_MEMORY_GRAPH_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv(_INSECURE_FLAG, raising=False)
    _inject_fakes(service, monkeypatch)
    client = TestClient(service.app)

    assert (
        client.post("/episodes", json={"group_id": "a", "name": "n", "content": "c"}).status_code
        == 401
    )
    assert client.post("/search", json={"query": "q"}).status_code == 401
    assert client.get("/healthz").status_code == 401


def test_empty_token_startup_refused(service: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # The lifespan refuses to start (naming the env var) BEFORE any graphiti_core
    # import — proven by entering the TestClient context manager, which runs it.
    monkeypatch.delenv("NE_MEMORY_GRAPH_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv(_INSECURE_FLAG, raising=False)
    with pytest.raises(Exception, match="NE_MEMORY_GRAPH_SERVICE_TOKEN"), TestClient(service.app):
        pass


def test_insecure_flag_allows_empty_token(
    service: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_INSECURE_FLAG, "1")
    monkeypatch.delenv("NE_MEMORY_GRAPH_SERVICE_TOKEN", raising=False)
    _inject_fakes(service, monkeypatch)
    client = TestClient(service.app)
    assert client.get("/healthz").status_code == 200


def test_wrong_token_401_via_constant_time_compare(
    service: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NE_MEMORY_GRAPH_SERVICE_TOKEN", "sekrit")
    real_compare = service.hmac.compare_digest
    calls: list[int] = []

    def _recording_compare(a: object, b: object) -> bool:
        calls.append(1)
        return real_compare(a, b)

    monkeypatch.setattr(service.hmac, "compare_digest", _recording_compare)
    _inject_fakes(service, monkeypatch)
    client = TestClient(service.app)

    resp = client.post("/search", json={"query": "q"}, headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    # The constant-time compare was on the path — fails if reverted to `!=`.
    assert calls, "hmac.compare_digest must be used for the bearer comparison"


def test_llm_api_key_resolution_prefers_override_then_service_token(
    service: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Explicit override wins.
    monkeypatch.setenv("NE_MEMORY_GRAPH_LLM_API_KEY", "abc")
    monkeypatch.setenv("NE_MEMORY_GRAPH_SERVICE_TOKEN", "tok")
    assert service._resolve_llm_api_key() == "abc"

    # No override → the SERVICE_TOKEN (the llm_facade bearer) is used.
    monkeypatch.delenv("NE_MEMORY_GRAPH_LLM_API_KEY", raising=False)
    assert service._resolve_llm_api_key() == "tok"

    # Neither set → the inert local fallback.
    monkeypatch.delenv("NE_MEMORY_GRAPH_SERVICE_TOKEN", raising=False)
    assert service._resolve_llm_api_key() == "sk-none"
