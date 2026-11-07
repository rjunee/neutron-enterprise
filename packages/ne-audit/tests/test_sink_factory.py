# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AuditSink Protocol conformance + config-driven factory.

AuditSink is not runtime_checkable, so conformance is proven STRUCTURALLY: the
sink is used through an ``AuditSink``-typed variable (append then verify). This
package suite is the conformance evidence — no official AuditSink kit exists and
none may be added under the ne-contracts freeze.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
import sqlalchemy as sa

from ne.audit import AuditSettings, PostgresAuditSink, create_audit_sink
from ne.contracts.governance import AuditSink, AuditVerification
from ne.contracts.plugins import PluginRegistry
from ne.schema import AuditEvent


def test_sink_conforms_to_protocol(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    sink: AuditSink = PostgresAuditSink(engine)
    asyncio.run(sink.append(audit(event_id="s1")))
    asyncio.run(sink.append(audit(event_id="s2")))
    result = asyncio.run(sink.verify())
    assert isinstance(result, AuditVerification)
    assert result.valid
    assert result.checked == 2


def test_entry_point_resolves() -> None:
    cls = PluginRegistry().resolve("audit_sink", "postgres")
    assert cls is PostgresAuditSink


def test_create_audit_sink_with_registry_override() -> None:
    class StubSink:
        instantiated = False

        @classmethod
        def from_settings(cls, settings: AuditSettings) -> StubSink:
            inst = cls()
            StubSink.instantiated = True
            return inst

    registry = PluginRegistry()
    registry.register("audit_sink", "stub", StubSink)
    settings = AuditSettings(sink="stub")
    sink = create_audit_sink(settings, registry)
    assert isinstance(sink, StubSink)
    assert StubSink.instantiated


def test_audit_settings_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_AUDIT_SINK", "postgres")
    monkeypatch.setenv("NE_AUDIT_DATABASE_URL", "postgresql://example/db")
    monkeypatch.setenv("NE_AUDIT_CHECKPOINT__EVERY_N_RECORDS", "50")
    settings = AuditSettings()
    assert settings.sink == "postgres"
    assert settings.database_url == "postgresql://example/db"
    assert settings.checkpoint.every_n_records == 50


def test_from_settings_empty_database_url_raises() -> None:
    with pytest.raises(ValueError, match="database_url is empty"):
        PostgresAuditSink.from_settings(AuditSettings(database_url=""))


def test_sink_from_settings_applies_engine_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Capture the create_engine kwargs at the ne.audit.engine seam through the
    # sink factory: the busy-timeout connect_args must be applied (P2-16).
    captured: dict[str, object] = {}
    real = sa.create_engine

    def fake(url: str, **kwargs: object) -> sa.Engine:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return real(url, **kwargs)

    monkeypatch.setattr("ne.audit.engine.sa.create_engine", fake)
    sink = PostgresAuditSink.from_settings(AuditSettings(database_url="sqlite://"))
    try:
        assert captured["url"] == "sqlite://"
        assert captured["kwargs"] == {"connect_args": {"timeout": 10}}
    finally:
        sink.engine.dispose()
