# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""Graphiti sidecar HTTP service — runs INSIDE the isolated graphiti_env venv.

Loopback-bound FastAPI surface over Graphiti (getzep/graphiti, Apache-2.0). This
script is stdlib + fastapi + graphiti-core ONLY; NOTHING here is importable
platform-side (C4 — the openai SDK / neo4j driver / posthog closure is confined to
this venv, never entering uv.lock).

C1 (no phone-home): ``GRAPHITI_TELEMETRY_ENABLED`` is FORCED to ``'false'`` — an
OVERRIDE, not a setdefault — as the FIRST executable statement, BEFORE any graphiti
import, neutralizing graphiti-core's posthog telemetry (default ON).

The LLM lane is ONE ``base_url`` env value (``NE_MEMORY_GRAPH_LLM_BASE_URL``):
frontier ⇒ the loopback llm_facade; air_gap ⇒ ne-serve's in-VPC vLLM /v1. Both are
validated loopback-or-allowlisted at startup (fail-closed, C2) — the sidecar has no
other egress route.

Usage (launched by ``ne.memory_graph.sidecar.GraphitiSidecar`` via subprocess)::

    python -m uvicorn --app-dir graphiti_env service:app --host 127.0.0.1 --port 8093
"""

from __future__ import annotations

import os

# C1 — force telemetry OFF unconditionally, BEFORE any graphiti import.
os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"

import hmac
import ipaddress
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request

# Dev-only escape hatch: set to "1" to run the sidecar WITHOUT a service token.
# Any other value (incl. unset) keeps the fail-closed default — an empty token
# refuses startup and 401s every route.
_INSECURE_FLAG = "NE_MEMORY_GRAPH_ALLOW_INSECURE_NO_TOKEN"


def _require_loopback(url: str, allowed: frozenset[str]) -> str:
    """Fail-closed C2 guard: base_url host must be loopback or explicitly allowlisted."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        raise RuntimeError(f"LLM/embed base_url has no host: {url!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and ip.is_loopback:
        return url
    if host in {"localhost", *allowed}:
        return url
    raise RuntimeError(
        f"LLM/embed base_url host {host!r} is not loopback/allowlisted (C2 fail-closed)"
    )


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _resolve_backend(value: str | None) -> str:
    """Resolve the NE_MEMORY_GRAPH_BACKEND env value → 'neo4j' | 'age' (fail-loud).

    Neo4j-CE stays the DEV default (empty/unset/'neo4j'); 'age' selects the Apache-AGE
    driver on the platform Postgres (the ship path, NE-083c). Anything else is a loud
    RuntimeError naming the env var — never a silent fallback to a surprise backend.
    """
    normalized = (value or "").strip().lower()
    if normalized in ("", "neo4j"):
        return "neo4j"
    if normalized == "age":
        return "age"
    raise RuntimeError(f"NE_MEMORY_GRAPH_BACKEND must be 'neo4j' or 'age', got {value!r}")


def _resolve_llm_api_key() -> str:
    # Frontier lane: the llm_facade requires the shared service token as its
    # bearer, so default the api_key to it — ONE documented secret wires the
    # lane end-to-end. vLLM (air_gap) ignores the key; 'sk-none' is the final
    # fallback so the OpenAI client never rejects an empty key locally.
    return _env("NE_MEMORY_GRAPH_LLM_API_KEY") or _env("NE_MEMORY_GRAPH_SERVICE_TOKEN") or "sk-none"


class _State:
    graphiti: Any = None
    driver: Any = None


_state = _State()


def _build_driver() -> Any:
    """Construct the graph driver selected by NE_MEMORY_GRAPH_BACKEND (fail-loud).

    'neo4j' (dev default) → the Neo4j-CE driver; 'age' (ship path) → the vendored
    ``graphiti_age.AgeDriver`` on the platform Postgres. The ``graphiti_age`` import
    stays INSIDE this function so ``service.py``'s module-level imports remain stdlib +
    fastapi only (the platform-side load pattern depends on it). The sidecar app-dir is
    on ``sys.path`` via ``uvicorn --app-dir``.
    """
    backend = _resolve_backend(_env("NE_MEMORY_GRAPH_BACKEND"))
    if backend == "age":
        dsn = _env("NE_MEMORY_GRAPH_AGE_DSN")
        if not dsn:
            raise RuntimeError("backend 'age' requires NE_MEMORY_GRAPH_AGE_DSN to be set")
        from graphiti_age import AgeDriver

        return AgeDriver(dsn=dsn, graph_name=_env("NE_MEMORY_GRAPH_AGE_GRAPH", "ne_memory"))

    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    return Neo4jDriver(
        uri=_env("NE_MEMORY_GRAPH_NEO4J_URI", "bolt://127.0.0.1:7687"),
        user=_env("NE_MEMORY_GRAPH_NEO4J_USER", "neo4j"),
        password=_env("NE_MEMORY_GRAPH_NEO4J_PASSWORD", "neo4j"),
    )


async def _build_graphiti() -> Any:
    from graphiti_core import Graphiti
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    allowed = frozenset(
        h.strip().lower() for h in _env("NE_MEMORY_GRAPH_ALLOWED_LLM_HOSTS").split(",") if h.strip()
    )
    llm_base = _require_loopback(_env("NE_MEMORY_GRAPH_LLM_BASE_URL"), allowed)
    embed_base = _require_loopback(_env("NE_MEMORY_GRAPH_EMBED_BASE_URL"), allowed)

    driver = _build_driver()
    llm_config = LLMConfig(
        api_key=_resolve_llm_api_key(),
        base_url=llm_base,
        model=_env("NE_MEMORY_GRAPH_LLM_MODEL", "gpt-4o-mini"),
    )
    llm_client = OpenAIGenericClient(config=llm_config)
    embedder = OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            api_key=_env("NE_MEMORY_GRAPH_EMBED_API_KEY", "sk-none"),
            base_url=embed_base,
            embedding_model=_env("NE_MEMORY_GRAPH_EMBED_MODEL", "text-embedding-3-small"),
        )
    )
    graphiti = Graphiti(graph_driver=driver, llm_client=llm_client, embedder=embedder)
    await graphiti.build_indices_and_constraints()
    _state.driver = driver
    return graphiti


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Fail-closed at startup (mirror the llm_facade refusal in cli.py): an empty
    # NE_MEMORY_GRAPH_SERVICE_TOKEN leaves /episodes,/search,/healthz open to any
    # caller. Refuse to start BEFORE _build_graphiti() (so this fires before any
    # graphiti_core import) unless the dev-only insecure flag is explicitly set.
    if not _env("NE_MEMORY_GRAPH_SERVICE_TOKEN") and _env(_INSECURE_FLAG) != "1":
        raise RuntimeError(
            "refusing to start: NE_MEMORY_GRAPH_SERVICE_TOKEN is empty — set it "
            "(mirror llm_facade fail-closed), or set "
            "NE_MEMORY_GRAPH_ALLOW_INSECURE_NO_TOKEN=1 for local dev only"
        )
    _state.graphiti = await _build_graphiti()
    try:
        yield
    finally:
        if _state.graphiti is not None:
            await _state.graphiti.close()


app = FastAPI(title="ne-memory-graph graphiti sidecar", version="0.1.0", lifespan=_lifespan)


def _check_auth(authorization: str | None) -> None:
    # The SAME env var the host-side MemoryGraphSettings.service_token reads
    # (NE_MEMORY_GRAPH_ prefix + service_token) and the README documents —
    # one secret secures the whole loopback seam.
    token = _env("NE_MEMORY_GRAPH_SERVICE_TOKEN")
    if not token:
        # Fail-closed: no token → refuse every request UNLESS the dev-only
        # insecure flag is explicitly set (the startup guard already refuses to
        # boot in this state outside dev, so this is defence-in-depth).
        if _env(_INSECURE_FLAG) == "1":
            return
        raise HTTPException(status_code=401, detail="service token not configured")
    # Constant-time compare so a wrong bearer can't be recovered by timing.
    if not hmac.compare_digest((authorization or "").encode("utf-8"), f"Bearer {token}".encode()):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@app.get("/healthz")
async def healthz(authorization: str | None = Header(default=None)) -> dict[str, str]:
    _check_auth(authorization)
    driver = _state.driver
    if driver is None:
        raise HTTPException(status_code=503, detail="driver not ready")
    await driver.execute_query("RETURN 1 AS ok")
    return {"status": "ok"}


@app.post("/episodes")
async def post_episode(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _check_auth(authorization)
    body: dict[str, Any] = await request.json()
    group_id = str(body["group_id"])
    name = str(body["name"])
    content = str(body["content"])
    kind = str(body.get("kind", ""))
    provenance = str(body.get("provenance", ""))
    reference_time = _parse_iso(body.get("occurred_at")) or datetime.now(UTC)

    # Idempotent by (group_id, name): skip if the episode already exists.
    existing, _, _ = await _state.driver.execute_query(
        "MATCH (e:Episodic {name: $name, group_id: $gid}) RETURN e LIMIT 1",
        name=name,
        gid=group_id,
    )
    if existing:
        return {"status": "already-ingested", "name": name}

    from graphiti_core.nodes import EpisodeType

    await _state.graphiti.add_episode(
        name=name,
        episode_body=content,
        source=EpisodeType.text,
        source_description=f"{kind} {provenance}".strip(),
        reference_time=reference_time,
        group_id=group_id,
    )
    return {"status": "ingested", "name": name}


def _within(instant: datetime | None, since: datetime | None, until: datetime | None) -> bool:
    if instant is None:
        return since is None and until is None
    if since is not None and instant < since:
        return False
    return not (until is not None and instant > until)


@app.post("/search")
async def search(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _check_auth(authorization)
    body: dict[str, Any] = await request.json()
    query = str(body["query"])
    group_ids = [str(g) for g in body.get("group_ids", [])]
    since = _parse_iso(body.get("since"))
    until = _parse_iso(body.get("until"))
    as_of = _parse_iso(body.get("as_of"))
    limit = int(body.get("limit", 20))

    results = await _state.graphiti.search(query, group_ids=group_ids, num_results=limit * 2)

    hits: list[dict[str, Any]] = []
    for edge in results:
        created_at = getattr(edge, "created_at", None)
        valid_at = getattr(edge, "valid_at", None)
        expired_at = getattr(edge, "expired_at", None)
        invalid_at = getattr(edge, "invalid_at", None)
        instant = valid_at or created_at
        if as_of is not None:
            if created_at is not None and created_at > as_of:
                continue
            if expired_at is not None and expired_at <= as_of:
                continue
            if invalid_at is not None and invalid_at <= as_of:
                continue
        else:
            if invalid_at is not None:
                continue
            if not _within(instant, since, until):
                continue
        event_ids = await _resolve_episode_names(getattr(edge, "episodes", []) or [])
        hits.append(
            {
                "content": getattr(edge, "fact", "") or "",
                "valid_at": valid_at.isoformat() if valid_at else None,
                "created_at": (created_at or datetime.now(UTC)).isoformat(),
                "group_id": getattr(edge, "group_id", group_ids[0] if group_ids else ""),
                "event_ids": event_ids,
                "edge_uuid": getattr(edge, "uuid", ""),
            }
        )
        if len(hits) >= limit:
            break
    return {"hits": hits}


async def _resolve_episode_names(episode_uuids: list[str]) -> list[str]:
    """Resolve episode UUIDs → episode NAMES (= NE-037 event_ids) for provenance."""
    if not episode_uuids:
        return []
    rows, _, _ = await _state.driver.execute_query(
        "MATCH (e:Episodic) WHERE e.uuid IN $uuids RETURN e.name AS name",
        uuids=list(episode_uuids),
    )
    return [str(r["name"]) for r in rows if r.get("name")]
