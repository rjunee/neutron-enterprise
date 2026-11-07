# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``llm_facade`` — the frontier-lane loopback OpenAI-compat surface.

A small ASGI app exposing ``POST /v1/chat/completions``, backed by the in-process
gateway-governed ``ModelGateway`` (feasibility memo §3). This is what the Graphiti
sidecar points at in the FRONTIER lane; the AIR_GAP lane points Graphiti directly
at ne-serve's in-VPC vLLM instead — ONE ``base_url`` config value switches lanes.

Every extraction call therefore stays gateway-governed (C2 egress allowlist, C12
caps, audit); no bypass is constructible because the sidecar has no other route
out. This module types ONLY against the frozen ``ne.contracts.models`` Protocol —
no provider SDK, no litellm (C3). The CLI wires a real ``LiteLLMGateway`` (the
ne-api ``_default_gateway`` recipe).

R1 cost capture: every request logs a structured line (logger
``ne.memory_graph.llm_facade``) with ``model_id`` / ``prompt_tokens`` /
``completion_tokens`` / ``cost_usd`` so per-event extraction spend is measurable.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ne.contracts.frozen import FrozenDict
from ne.contracts.models import CompletionRequest, ModelGateway
from ne.contracts.types import ChatMessage

__all__ = ["create_llm_facade_app"]

_LOGGER = logging.getLogger("ne.memory_graph.llm_facade")

_PASSTHROUGH_PARAMS = ("temperature", "top_p", "max_tokens", "stop", "seed", "response_format")


def create_llm_facade_app(
    gateway: ModelGateway,
    *,
    service_token: str,
    task: str = "memory-extraction",
) -> FastAPI:
    """Build the frontier-lane OpenAI-compat facade over ``gateway``.

    ``service_token`` MUST be non-empty (the sidecar authenticates with it); an
    empty token refuses to build the app (fail-closed).
    """
    if not service_token:
        raise ValueError("llm_facade requires a non-empty service_token (fail-closed)")

    app = FastAPI(title="ne-memory-graph llm_facade", version="0.1.0")
    expected = f"Bearer {service_token}"

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:
        if request.headers.get("authorization") != expected:
            return JSONResponse(
                {"error": {"message": "invalid bearer token", "type": "unauthorized"}},
                status_code=401,
            )
        body: dict[str, Any] = await request.json()
        if body.get("stream"):
            return JSONResponse(
                {
                    "error": {
                        "message": "streaming is not supported by the extraction facade",
                        "type": "invalid_request_error",
                    }
                },
                status_code=400,
            )
        messages = tuple(
            ChatMessage(role=m["role"], content=m.get("content") or "")
            for m in body.get("messages", [])
        )
        params: dict[str, Any] = {key: body[key] for key in _PASSTHROUGH_PARAMS if key in body}
        completion = await gateway.complete(
            CompletionRequest(messages=messages, task=task, params=FrozenDict(params))
        )
        usage = completion.usage
        total = usage.prompt_tokens + usage.completion_tokens
        _LOGGER.info(
            "extraction cost model_id=%s prompt_tokens=%d completion_tokens=%d cost_usd=%s",
            completion.model_id,
            usage.prompt_tokens,
            usage.completion_tokens,
            completion.cost_usd,
        )
        return JSONResponse(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "model": completion.model_id,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": completion.text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": total,
                },
            }
        )

    return app
