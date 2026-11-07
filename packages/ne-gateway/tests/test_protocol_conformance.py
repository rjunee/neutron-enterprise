# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LiteLLMGateway satisfies the frozen ModelGateway Protocol.

The annotated assignment below is the real assertion — pyright checks that
LiteLLMGateway is structurally a ``ModelGateway`` (async ``complete``, sync
``stream`` returning an ``AsyncIterator``). The runtime asserts back it up.
"""

from __future__ import annotations

import asyncio

from ne.contracts import ModelGateway
from ne.gateway import LiteLLMGateway, TaskRoute

from .conftest import PASSTHROUGH_REDACTOR, RecordingCostSink, make_model, make_settings


def test_litellm_gateway_conforms_to_protocol() -> None:
    settings = make_settings(
        [make_model("a")],
        {"draft": TaskRoute(model="a")},
    )
    gw: ModelGateway = LiteLLMGateway(settings, RecordingCostSink(), redactor=PASSTHROUGH_REDACTOR)
    assert asyncio.iscoroutinefunction(gw.complete)
    assert not asyncio.iscoroutinefunction(gw.stream)
