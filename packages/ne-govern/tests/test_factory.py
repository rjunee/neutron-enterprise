# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""create_pdp: entry-point resolution, programmatic override, settings wiring."""

from __future__ import annotations

import asyncio
from typing import Any

from ne.contracts.plugins import PluginRegistry
from ne.govern import GovernedPdp, create_pdp
from ne.govern.config import GovernSettings

from .conftest import RecordingAuditSink


def test_create_pdp_resolves_the_opa_entry_point() -> None:
    pdp = create_pdp(GovernSettings(), sink=RecordingAuditSink())
    assert isinstance(pdp, GovernedPdp)
    asyncio.run(pdp.aclose())


def test_programmatic_registration_overrides_the_entry_point() -> None:
    sink = RecordingAuditSink()

    class SentinelPdp:
        def __init__(self, sink: Any) -> None:
            self.sink = sink

        @classmethod
        def from_settings(cls, settings: GovernSettings, *, sink: Any) -> SentinelPdp:
            return cls(sink)

    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", SentinelPdp)
    pdp = create_pdp(GovernSettings(), sink=sink, registry=registry)
    assert isinstance(pdp, SentinelPdp)
    assert pdp.sink is sink


def test_from_settings_honors_opa_url_and_decision_path_and_timeout() -> None:
    settings = GovernSettings(
        opa_url="http://opa.internal:9999",
        decision_path="ne/govern/decision",
        timeout_s=1.5,
    )
    pdp = GovernedPdp.from_settings(settings, sink=RecordingAuditSink())
    assert pdp._opa._decision_path == "ne/govern/decision"
    assert str(pdp._opa._client.base_url) == "http://opa.internal:9999"
    assert pdp._opa._client.timeout.read == 1.5
    asyncio.run(pdp.aclose())
