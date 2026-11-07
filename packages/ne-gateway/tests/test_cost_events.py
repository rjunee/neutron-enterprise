# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Cost pipeline — OutboxCostSink end-to-end over the sqlite outbox, plus the
extract_* helper guarantees."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import sqlalchemy as sa

from ne.contracts import ChatMessage, CompletionRequest
from ne.events.outbox import ne_outbox_events
from ne.gateway import (
    LiteLLMGateway,
    OutboxCostSink,
    TaskRoute,
    extract_cost,
    extract_usage,
)

from .conftest import PASSTHROUGH_REDACTOR, RecordingCostSink, make_model, make_settings

_MSGS = (ChatMessage(role="user", content="hi"),)


def test_complete_emits_cost_event_to_outbox(engine: sa.Engine, frozen_config: str) -> None:
    settings = make_settings(
        [
            make_model(
                "primary", litellm_model="gpt-3.5-turbo", provider="openai", mock_response="hello"
            )
        ],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, OutboxCostSink(engine), redactor=PASSTHROUGH_REDACTOR)
    asyncio.run(gw.complete(CompletionRequest(messages=_MSGS, task="draft")))

    with engine.begin() as conn:
        rows = conn.execute(sa.select(ne_outbox_events)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "cost"
    assert row["config_hash"] == frozen_config
    assert row["actor"] == "ne-gateway"
    payload = row["payload"]
    assert payload["model"] == "gpt-3.5-turbo"
    assert payload["provider"] == "openai"
    assert payload["lane"] == "frontier-api"
    assert payload["task"] == "draft"
    assert payload["input_tokens"] == 10
    assert payload["output_tokens"] == 20


def test_kpi_tag_request_param_stamped_and_not_forwarded(
    sink: RecordingCostSink, frozen_config: str, monkeypatch: Any
) -> None:
    settings = make_settings(
        [make_model("a")],
        {"draft": TaskRoute(model="a")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)

    captured: dict[str, Any] = {}

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
            _hidden_params={"response_cost": 0.0},
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(
        gw.complete(CompletionRequest(messages=_MSGS, task="draft", params={"kpi_tag": "x"}))
    )
    assert len(sink.events) == 1
    assert sink.events[0].kpi_tag == "x"
    assert "kpi_tag" not in captured


def test_extract_usage_none_guards() -> None:
    assert extract_usage(object()) == (None, None)
    partial = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=5))
    assert extract_usage(partial) == (5, None)


def test_extract_cost_never_raises() -> None:
    # No _hidden_params and completion_cost will fail on a bare object -> None.
    assert extract_cost(object()) is None
    # 0.0 is a legitimate value and passes through as-is.
    zero = SimpleNamespace(_hidden_params={"response_cost": 0.0})
    assert extract_cost(zero) == 0.0


def test_kpi_tag_is_stamped_on_the_audit_event_too(
    sink: RecordingCostSink, audit_sink: Any, frozen_config: str, monkeypatch: Any
) -> None:
    """THE JOIN THE WHOLE C8 REPORTING PATH RESTS ON.

    ``ne.obs.collect_spend`` reads the durable AUDIT CHAIN, not the CostEvent
    outbox (whose rows ``prune_acked`` deletes), and groups spend by
    ``AuditRecord.kpi_tag``. That only reports the truth if the gateway stamps
    the SAME tag on the AuditEvent as on the sibling CostEvent — and the obs/api
    tests cannot prove it, because they seed audit rows by hand with the tag
    already set. This test is the one place the linkage is observed rather than
    constructed: one real gateway call, both sinks, byte-equal tags, and the
    ``cost_usd`` provenance the collector parses.
    """
    settings = make_settings([make_model("a")], {"draft": TaskRoute(model="a")})
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR, audit_sink=audit_sink)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
            _hidden_params={"response_cost": 0.25},
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(
        gw.complete(
            CompletionRequest(messages=_MSGS, task="draft", params={"kpi_tag": "renewal-retention"})
        )
    )

    (cost_event,) = sink.events
    assert cost_event.kpi_tag == "renewal-retention"

    audited = [e for e in audit_sink.events if getattr(e, "action", None) == "ne.gateway.complete"]
    assert len(audited) == 1
    # Byte-equal, not merely both-truthy: a differing tag would re-attribute the
    # spend on the reporting path while the CostEvent looked correct.
    assert audited[0].kpi_tag == cost_event.kpi_tag
    # And the price the collector parses rides the SAME event, as a string that
    # round-trips to the CostEvent's float.
    assert float(audited[0].provenance["cost_usd"]) == cost_event.cost_usd


def test_an_untagged_call_leaves_the_audit_event_untagged(
    sink: RecordingCostSink, audit_sink: Any, frozen_config: str, monkeypatch: Any
) -> None:
    """The control for the test above: the tag appears BECAUSE the request carried
    one, not on every call. An audit event tagged regardless would make every
    call look attributed to whatever the last tag was."""
    settings = make_settings([make_model("a")], {"draft": TaskRoute(model="a")})
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR, audit_sink=audit_sink)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
            _hidden_params={"response_cost": 0.25},
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(gw.complete(CompletionRequest(messages=_MSGS, task="draft")))

    assert sink.events[0].kpi_tag is None
    audited = [e for e in audit_sink.events if getattr(e, "action", None) == "ne.gateway.complete"]
    assert audited[0].kpi_tag is None
