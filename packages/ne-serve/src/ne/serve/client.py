# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin async client for a vLLM OpenAI-compatible server (the private-vllm lane).

``ServeClient`` speaks ONLY the OpenAI-compatible HTTP surface (``/health``,
``/v1/models``, ``/v1/chat/completions``) so the engine is swappable (SGLang /
TGI drop-in — C5). It imports no provider SDK / litellm / openai (C3 covers
``ne.serve``); the transport is plain :mod:`httpx`, already a platform dep.

Structured output uses the CURRENT vLLM surface: ``response_format`` of type
``json_schema`` with ``strict: true``. vLLM removed the legacy
``guided_json`` / ``guided_*`` request fields in v0.12.0 — this client MUST
NEVER emit them (a below-seam and a live test both lock that in).

The config-supplied ``base_url`` is C2-validated at CONSTRUCTION time
(:func:`ne.serve.sovereignty.validate_in_vpc_endpoint`: SSRF shape + in-VPC /
allowlist, fail-closed :class:`~ne.serve.errors.UnsafeEndpointError`) — a
rejected endpoint never receives a request. This is the ne-serve half of the
audit corpus-exfil finding.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from ne.serve.config import ServeSettings
from ne.serve.errors import (
    ServeConnectionError,
    ServeHealthError,
    StructuredOutputViolationError,
)
from ne.serve.sovereignty import validate_in_vpc_endpoint


class ServeClient:
    """Async OpenAI-compatible client for an in-VPC vLLM server."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        settings: ServeSettings | None = None,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
    ) -> None:
        settings = settings or ServeSettings()
        self._base_url = validate_in_vpc_endpoint(
            base_url or settings.base_url, allowed_hosts=settings.allowed_endpoint_hosts
        )
        self._model = model or settings.model
        self._timeout = timeout if timeout is not None else settings.timeout_s
        self._owns_client = client is None
        self._client = client

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def model(self) -> str:
        return self._model

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def health(self) -> None:
        """GET ``/health``; raise :class:`ServeHealthError` unless it returns 200."""
        client = self._get_client()
        try:
            response = await client.get(f"{self._base_url}/health")
        except httpx.HTTPError as exc:
            raise ServeConnectionError(f"vLLM server unreachable at {self._base_url}") from exc
        if response.status_code != 200:
            raise ServeHealthError(
                f"vLLM /health returned {response.status_code} (server not ready)"
            )

    async def models(self) -> list[str]:
        """GET ``/v1/models``; return the list of served model ids (``data[].id``)."""
        client = self._get_client()
        try:
            response = await client.get(f"{self._base_url}/v1/models")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ServeConnectionError(f"vLLM /v1/models failed at {self._base_url}") from exc
        data = response.json().get("data", [])
        return [entry["id"] for entry in data]

    async def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str | None = None,
        max_tokens: int = 256,
        temperature: float = 0.0,
    ) -> str:
        """POST ``/v1/chat/completions``; return the first choice's message content."""
        body: dict[str, Any] = {
            "model": model or self._model,
            "messages": [dict(m) for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        return await self._chat(body)

    async def generate_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str = "response",
        model: str | None = None,
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        """Structured output: request ``response_format`` json_schema, parse the
        returned content as JSON. Raises :class:`StructuredOutputViolationError`
        when the content is not parseable JSON.

        Emits the CURRENT vLLM surface only — never the removed ``guided_json``
        / ``guided_*`` fields.
        """
        body: dict[str, Any] = {
            "model": model or self._model,
            "messages": [dict(m) for m in messages],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": dict(schema),
                    "strict": True,
                },
            },
        }
        content = await self._chat(body)
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise StructuredOutputViolationError(
                "structured-output response content was not valid JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise StructuredOutputViolationError(
                f"structured-output response was not a JSON object (got {type(parsed).__name__})"
            )
        return parsed

    async def _chat(self, body: Mapping[str, Any]) -> str:
        client = self._get_client()
        try:
            response = await client.post(
                f"{self._base_url}/v1/chat/completions",
                json=dict(body),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ServeConnectionError(
                f"vLLM /v1/chat/completions failed at {self._base_url}"
            ) from exc
        payload = response.json()
        return payload["choices"][0]["message"]["content"]

    async def aclose(self) -> None:
        """Close the owned client. A no-op when the client was injected."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
