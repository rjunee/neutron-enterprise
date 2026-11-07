# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Which candidate actually served — recorded, not inferred.

The gateway routes per task down a declared chain (``TaskRoute.model`` then
``TaskRoute.fallbacks``). Until this unit the SERVED record said only which
model answered; nothing said whether that model was the one the route asked
for FIRST or the one that answered after an earlier candidate did not. A
reader of the chain could not tell "the frontier model answered" from "the
frontier model was unreachable and the on-box model answered instead" — which
is a governance fact, not a footnote.

Two provenance keys carry it, on the same success record that already carries
task/lane/model_name:

* ``route_position`` — ``"primary"`` when NO earlier candidate was attempted
  for this request, ``"fallback"`` when at least one was and did not serve.
  This is a statement about THIS request's attempt sequence, never about the
  configured chain: a lane-pinned request whose only eligible candidate served
  is ``"primary"``, because nothing fell back.
* ``fallback_from`` — the declared NAMES of the candidates that were attempted
  and did not serve, in attempt order. Present IFF ``route_position`` is
  ``"fallback"``. Names only (C12): never a provider error string, never an
  ``api_base``, never a key.

C12 IS CLAIMED OVER THE KEY, SO IT IS SWEPT AT EVERY SITE THAT COULD BREAK IT
— and BOTH halves of that sentence are executable, because a claim of totality
that no test can falsify is worth less than no claim at all.

* The sweep itself is ONE parametrized test over ALL THREE call sites of
  ``_routing_provenance``
  (``test_every_routing_provenance_site_carries_names_only_over_a_credentialed_private_chain``),
  driven on ``PRIVATE_VLLM`` — the lane where the hazard is live, because the
  frontier lane's ``FrontierProviderError`` reduction (``gateway.py``, the
  establish-phase ``except``) destroys the exception before it can reach
  ``attempts`` and a sweep over a frontier fixture is therefore unfalsifiable
  (V8). Every failing candidate in that fixture carries its OWN credentialed
  ``api_base``, and there are TWO of them before the one that serves, so a
  mutant that leaks any single predecessor — not merely the first — is RED.
* The word EVERY is pinned by a source census
  (``test_the_swept_sites_are_every_routing_provenance_call_site_in_the_package``),
  which parses the shipped ``ne.gateway`` package and asserts the call sites of
  ``_routing_provenance`` are exactly the three the sweep drives. A fourth site
  added later turns that census RED, so the sweep has to grow with the code
  instead of quietly under-approximating it.

``Sequence[str]`` at ``_finalize_stream`` is NOT the closure for the stream
site and is not cited as one: a credential is a string, so that annotation
forbids the shape that was never the hazard while admitting the one that is.
The stream site is closed by the sweep, like the other two.

Every assertion is against a real ``AuditEvent`` recorded by
``RecordingAuditSink``, driven at the sanctioned litellm boundary
(``mock_response`` / a ``Router.acompletion`` monkeypatch), following
test_cost_provenance.py.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ne.gateway as gateway_package
from ne.contracts import ChatMessage, CompletionChunk, CompletionRequest, Lane
from ne.gateway import LiteLLMGateway, ModelCallFailedError, ModelDef, TaskRoute
from ne.gateway._litellm import litellm
from ne.schema import AuditEvent, AuditOutcome

from .conftest import (
    PASSTHROUGH_REDACTOR,
    RecordingAuditSink,
    RecordingCostSink,
    make_model,
    make_settings,
)

_MSGS = (ChatMessage(role="user", content="hi"),)


def _request(task: str = "draft") -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task)


def _find_event(events: Sequence[object], action: str) -> AuditEvent:
    for event in events:
        if isinstance(event, AuditEvent) and event.action == action:
            return event
    raise AssertionError(f"no AuditEvent with action={action!r} in {events!r}")


def _success_event(events: Sequence[object], action: str) -> AuditEvent:
    for event in events:
        if (
            isinstance(event, AuditEvent)
            and event.action == action
            and event.outcome == AuditOutcome.success
        ):
            return event
    raise AssertionError(f"no successful AuditEvent with action={action!r} in {events!r}")


def _error_event_matching(events: Sequence[object], action: str, needle: str) -> AuditEvent:
    """The ONE error-outcome record whose detail names ``needle``.

    A single request can produce several error records on the same action; a
    positional pick would silently drift onto a sibling. Raises when the match
    is not unique, so a fixture change can never quietly re-point this at a
    different record.
    """
    matches = [
        event
        for event in events
        if isinstance(event, AuditEvent)
        and event.action == action
        and event.outcome == AuditOutcome.error
        and needle in (event.detail or "")
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly 1 error AuditEvent with action={action!r} whose detail "
            f"contains {needle!r}; found {len(matches)} in {events!r}"
        )
    return matches[0]


async def _drain(gen: object) -> list[CompletionChunk]:
    out: list[CompletionChunk] = []
    async for chunk in gen:  # type: ignore[attr-defined]
        out.append(chunk)
    return out


def _stream_gen() -> Any:
    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        async def gen() -> AsyncIterator[object]:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))])

        return gen()

    return fake_acompletion


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


def test_complete_first_candidate_records_route_position_primary_without_fallback_from(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary"), make_model("backup")],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    result = asyncio.run(gw.complete(_request()))
    assert result.model_id == "primary"

    provenance = _success_event(audit_sink.events, "ne.gateway.complete").provenance
    assert provenance["route_position"] == "primary"
    # No candidate failed, so there is nothing to have fallen back FROM. The
    # key is absent rather than empty: an empty string would read as a name.
    assert "fallback_from" not in provenance


def test_complete_after_primary_error_records_fallback_and_names_the_candidate_that_failed(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("boom")),
            make_model("backup", mock_response="recovered"),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    result = asyncio.run(gw.complete(_request()))
    assert result.model_id == "backup"

    event = _success_event(audit_sink.events, "ne.gateway.complete")
    provenance = event.provenance
    # The record names BOTH facts: which model served, and that it served only
    # because the declared first choice did not.
    assert provenance["model_name"] == "backup"
    assert provenance["route_position"] == "fallback"
    assert provenance["fallback_from"] == "primary"


@pytest.mark.parametrize("passed_over_count", [1, 2, 3, 5, 8])
def test_complete_records_each_earlier_candidate_in_attempt_order_across_chain_lengths(
    passed_over_count: int,
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
) -> None:
    """``fallback_from`` is the WHOLE passed-over prefix, exactly and in order.

    Over a GENERATED chain rather than a hand-written pair, because the
    property is a statement about ``TaskRoute.fallbacks``, which is uncapped:
    a fixture with two predecessors cannot tell "records every earlier
    candidate" from "records the first two", and the truncating mutant
    ``passed_over[:2]`` survives it. Sweeping the lengths kills every fixed
    truncation and every off-by-one below the top of the range.

    RESIDUAL, stated rather than implied by the name: this is a sample, not an
    induction. A truncation at a bound ABOVE 8 (``passed_over[:9]``) survives,
    which is why the name says "across chain lengths" instead of "every".
    """
    names = [f"cand-{index}" for index in range(passed_over_count)]
    settings = make_settings(
        [make_model(name, mock_response=Exception(f"boom-{name}")) for name in names]
        + [make_model("server", mock_response="recovered")],
        {"draft": TaskRoute(model=names[0], fallbacks=(*names[1:], "server"))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    assert asyncio.run(gw.complete(_request())).model_id == "server"

    provenance = _success_event(audit_sink.events, "ne.gateway.complete").provenance
    assert provenance["route_position"] == "fallback"
    assert provenance["fallback_from"] == ",".join(names)


def test_complete_after_budget_preflight_skip_records_fallback(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """A candidate passed over by the budget pre-flight did not serve either.

    The pre-flight skip never reaches a provider, so it emits no record of its
    own; without this the chain would show the second model answering with no
    trace that the first was passed over at all.
    """
    settings = make_settings(
        [
            make_model("small-window", context_window=2048),
            make_model("big-window", context_window=32768, mock_response="recovered"),
        ],
        {"draft": TaskRoute(model="small-window", fallbacks=("big-window",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    # ~830 estimated tokens: over the 2048-window candidate's input budget
    # (2048 - max_output - 1024 margin), comfortably inside the 32768 one.
    long_request = CompletionRequest(
        messages=(ChatMessage(role="user", content="x" * 1200),), task="draft"
    )
    assert asyncio.run(gw.complete(long_request)).model_id == "big-window"

    provenance = _success_event(audit_sink.events, "ne.gateway.complete").provenance
    assert provenance["route_position"] == "fallback"
    assert provenance["fallback_from"] == "small-window"


def test_complete_lane_pinned_single_candidate_records_primary_not_fallback(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """A lane pin narrows the chain; it does not make the survivor a fallback.

    ``route_position`` states what happened to THIS request — nothing was
    attempted before the model that served — so claiming "fallback" here
    would report a failure that never occurred.
    """
    settings = make_settings(
        [
            make_model("frontier-primary"),
            make_model("on-box", lane=Lane.PRIVATE_VLLM, api_base="http://vllm.internal:8000"),
        ],
        {"draft": TaskRoute(model="frontier-primary", fallbacks=("on-box",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    pinned = CompletionRequest(messages=_MSGS, task="draft", lane=Lane.PRIVATE_VLLM)
    assert asyncio.run(gw.complete(pinned)).model_id == "on-box"

    provenance = _success_event(audit_sink.events, "ne.gateway.complete").provenance
    assert provenance["model_name"] == "on-box"
    assert provenance["route_position"] == "primary"
    assert "fallback_from" not in provenance


def test_private_lane_fallback_records_the_failed_candidate_name_not_its_error_or_endpoint(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """C12 on the lane where the hazard is LIVE: the failed candidate is named,
    its failure text and endpoint credential are not quoted.

    THE LANE IS THE POINT. On ``FRONTIER_API`` the gateway already replaces
    every attempt exception with ``FrontierProviderError(type(exc).__name__)``
    *before* it reaches ``attempts`` (gateway.py, the establish-phase
    ``except``; pinned independently by
    ``test_redaction_seam.py::test_frontier_provider_error_reduced_to_class_name``,
    which is why this file does not re-assert it). A
    credential planted on a frontier candidate is therefore destroyed by
    pre-existing code, and a sweep over that fixture is unfalsifiable — it
    passes whether or not ``_routing_provenance`` is careful (V8: a fixture
    shaped to miss the failing case). ``PRIVATE_VLLM`` keeps the ORIGINAL
    exception object in ``attempts``, so its text — and any ``api_base``
    credential the provider echoed into it — is genuinely reachable from the
    success record's provenance. This fixture puts it there.

    PRESENCE CONTROL (catalogue rows #11/#13): the swept string must contain
    the failed candidate's declared NAME. That proves the sweep is reading the
    right event's provenance AND that the failed candidate's data reaches this
    window at all — without it, a mis-selected event or an empty join would
    make every absence assertion below silently vacuous.
    """
    leaky_endpoint = "http://user:sk-SECRET@vllm.internal:8000"
    settings = make_settings(
        [
            make_model(
                "onbox-primary",
                lane=Lane.PRIVATE_VLLM,
                api_base=leaky_endpoint,
                mock_response=Exception(f"connect to {leaky_endpoint} failed"),
            ),
            make_model(
                "onbox-backup",
                lane=Lane.PRIVATE_VLLM,
                api_base="http://vllm-2.internal:8000",
                mock_response="recovered",
            ),
        ],
        {"draft": TaskRoute(model="onbox-primary", fallbacks=("onbox-backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    assert asyncio.run(gw.complete(_request())).model_id == "onbox-backup"

    provenance = _success_event(audit_sink.events, "ne.gateway.complete").provenance
    joined = " ".join(str(value) for value in provenance.values())

    # CONTROL — the failed candidate's data demonstrably reaches this window.
    assert "onbox-primary" in joined, (
        "presence control failed: the failed candidate's name is not in the swept "
        "provenance, so the absence assertions below prove nothing"
    )
    # ...and what reaches it is the NAME and nothing else off that candidate.
    assert "sk-SECRET" not in joined
    assert "vllm.internal" not in joined
    assert "connect to" not in joined
    assert "Exception" not in joined


# ---------------------------------------------------------------------------
# The reasoning-exhausted record.
#
# A candidate that burns its whole output budget inside the reasoning channel
# DID serve tokens and then failed to answer, so it gets its own error-outcome
# record — and that record's placement in the attempt sequence is a real fact
# about the request, exactly as the success record's is. Without this the
# routing stamp reads "on every record that names a served candidate" while
# one of the three site classes is unpinned.
# ---------------------------------------------------------------------------


def test_reasoning_exhausted_record_stamps_the_position_that_candidate_occupied(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chain is primary (unreachable) → reasoner (burns its budget) →
    backup (answers). The reasoner's OWN record must say it sat second, and
    name what it was routed past — not report itself as the first choice."""
    settings = make_settings(
        [make_model("primary"), make_model("reasoner"), make_model("backup")],
        {"draft": TaskRoute(model="primary", fallbacks=("reasoner", "backup"))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    def _reply(content: str | None, reasoning: str | None) -> Any:
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning is not None:
            message["reasoning_content"] = reasoning
        return litellm.ModelResponse(
            choices=[{"message": message, "finish_reason": "length" if reasoning else "stop"}],
            usage={"prompt_tokens": 10, "completion_tokens": 4096, "total_tokens": 4106},
        )

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        if model == "primary":
            raise RuntimeError("primary is unreachable")
        if model == "reasoner":
            return _reply(None, "thought at length and never answered")
        return _reply("recovered", None)

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    assert asyncio.run(gw.complete(_request())).model_id == "backup"

    exhausted = _error_event_matching(
        audit_sink.events, "ne.gateway.complete", "reasoning exhausted"
    )
    assert exhausted.provenance["model_name"] == "reasoner"
    assert exhausted.provenance["route_position"] == "fallback"
    assert exhausted.provenance["fallback_from"] == "primary"


def test_private_lane_reasoning_exhausted_record_names_the_failed_candidate_not_its_error(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C12 on the reasoning-exhausted record, on the lane where it is LIVE.

    THE SWEEP MUST COVER EVERY SITE THAT READS ``attempts``, not the one that
    happened to get a fixture. ``complete()``'s success record and this
    error-outcome record BOTH build their routing keys from
    ``[name for name, _ in attempts]``, and on ``PRIVATE_VLLM`` the second
    element of each pair is the ORIGINAL provider exception — whose text can
    carry the candidate's ``api_base`` credential (the frontier lane's
    ``FrontierProviderError`` reduction does not apply here). A mutant that
    sourced either record's routing detail from the exceptions instead of the
    names would leak that credential onto a key the console renders; swept at
    the success site alone, such a mutant survives here.

    Chain: onbox-primary (unreachable, and its failure quotes a credentialed
    endpoint) → onbox-reasoner (burns its whole budget in the reasoning
    channel) → onbox-backup (answers).

    PRESENCE CONTROL (catalogue rows #11/#13): the swept string must contain
    the failed candidate's declared NAME, so a mis-selected event or an empty
    join cannot make the absence assertions below silently vacuous.
    """
    leaky_endpoint = "http://user:sk-SECRET@vllm.internal:8000"
    settings = make_settings(
        [
            make_model("onbox-primary", lane=Lane.PRIVATE_VLLM, api_base=leaky_endpoint),
            make_model(
                "onbox-reasoner", lane=Lane.PRIVATE_VLLM, api_base="http://vllm-2.internal:8000"
            ),
            make_model(
                "onbox-backup", lane=Lane.PRIVATE_VLLM, api_base="http://vllm-3.internal:8000"
            ),
        ],
        {"draft": TaskRoute(model="onbox-primary", fallbacks=("onbox-reasoner", "onbox-backup"))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    def _reply(content: str | None, reasoning: str | None) -> Any:
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning is not None:
            message["reasoning_content"] = reasoning
        return litellm.ModelResponse(
            choices=[{"message": message, "finish_reason": "length" if reasoning else "stop"}],
            usage={"prompt_tokens": 10, "completion_tokens": 4096, "total_tokens": 4106},
        )

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        if model == "onbox-primary":
            raise Exception(f"connect to {leaky_endpoint} failed")
        if model == "onbox-reasoner":
            return _reply(None, "thought at length and never answered")
        return _reply("recovered", None)

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    assert asyncio.run(gw.complete(_request())).model_id == "onbox-backup"

    exhausted = _error_event_matching(
        audit_sink.events, "ne.gateway.complete", "reasoning exhausted"
    )
    joined = " ".join(str(value) for value in exhausted.provenance.values())

    # CONTROL — the failed candidate's data demonstrably reaches this window.
    assert "onbox-primary" in joined, (
        "presence control failed: the failed candidate's name is not in the swept "
        "provenance, so the absence assertions below prove nothing"
    )
    # ...and what reaches it is the NAME and nothing else off that candidate.
    assert "sk-SECRET" not in joined
    assert "vllm.internal" not in joined
    assert "connect to" not in joined
    assert "Exception" not in joined


# ---------------------------------------------------------------------------
# stream()
#
# The stream site's C12 sweep is NOT here — it is the three-site parametrized
# sweep at the bottom of this file, which drives this transport on
# ``PRIVATE_VLLM`` with two credentialed failures ahead of the candidate that
# serves. An earlier revision of this comment claimed the site was closed BY
# CONSTRUCTION because ``_finalize_stream`` takes ``Sequence[str]``; that is
# wrong and is recorded here rather than deleted. ``Sequence[str]`` forbids a
# non-string, and the hazard was never a non-string: an ``api_base``
# credential is itself a ``str``, so ``passed_over`` could be built from
# ``str(exc)`` and satisfy the annotation, pyright, and every test in this
# file. The tests below pin the ROUTING facts on this transport; C12 on it is
# pinned by the sweep.
# ---------------------------------------------------------------------------


def test_stream_first_candidate_records_route_position_primary_without_fallback_from(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        [make_model("primary"), make_model("backup")],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    monkeypatch.setattr(gw._router, "acompletion", _stream_gen())

    asyncio.run(_drain(gw.stream(_request())))

    provenance = _success_event(audit_sink.events, "ne.gateway.stream").provenance
    assert provenance["route_position"] == "primary"
    assert "fallback_from" not in provenance


def test_stream_after_primary_error_records_fallback_and_names_the_candidate_that_failed(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("boom")),
            make_model("backup", mock_response="recovered"),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    asyncio.run(_drain(gw.stream(_request())))

    provenance = _success_event(audit_sink.events, "ne.gateway.stream").provenance
    assert provenance["model_name"] == "backup"
    assert provenance["route_position"] == "fallback"
    assert provenance["fallback_from"] == "primary"


def test_stream_after_budget_preflight_skip_records_fallback(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """The pre-flight skip counts on the stream transport too.

    ``complete()`` has its own pin above; the stream path reaches
    ``_routing_provenance`` through a DIFFERENT binding (``passed_over``,
    frozen at the winning attempt) rather than through ``attempts``, so the
    skip's placement there is a separate fact and needs its own record.
    """
    settings = make_settings(
        [
            make_model("small-window", context_window=2048),
            make_model("big-window", context_window=32768, mock_response="streamed"),
        ],
        {"draft": TaskRoute(model="small-window", fallbacks=("big-window",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    long_request = CompletionRequest(
        messages=(ChatMessage(role="user", content="x" * 1200),), task="draft"
    )
    asyncio.run(_drain(gw.stream(long_request)))

    provenance = _success_event(audit_sink.events, "ne.gateway.stream").provenance
    assert provenance["model_name"] == "big-window"
    assert provenance["route_position"] == "fallback"
    assert provenance["fallback_from"] == "small-window"


# ---------------------------------------------------------------------------
# The two ABNORMAL stream terminations.
#
# ``stream()`` finalizes on three paths — clean exhaustion, consumer abandon
# (GeneratorExit), and mid-drain failure (BaseException) — and all three run
# through the SAME ``_finalize_stream`` call with their own ``passed_over=``
# argument. The clean path is pinned above. These two pin the abnormal arms,
# because a routing downgrade is not less true for the stream having been
# abandoned or having failed: the customer's text still went to a model the
# route did not ask for first, and that arm's record is the only place it is
# written down.
# ---------------------------------------------------------------------------


def test_stream_abandoned_by_its_consumer_still_records_the_candidate_it_fell_back_from(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("boom")),
            make_model("backup", mock_response="hello world"),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def consume_one_then_abandon() -> None:
        agen = gw.stream(_request())
        await agen.__anext__()  # type: ignore[attr-defined]
        await agen.aclose()  # type: ignore[attr-defined]

    asyncio.run(consume_one_then_abandon())

    event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert event.outcome == AuditOutcome.error  # abandoned...
    # ...and still says which candidate served, and that it was not the first.
    assert event.provenance["model_name"] == "backup"
    assert event.provenance["route_position"] == "fallback"
    assert event.provenance["fallback_from"] == "primary"


def test_stream_failing_mid_drain_still_records_the_candidate_it_fell_back_from(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        [
            make_model("primary"),
            make_model("backup"),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        if model == "primary":
            raise RuntimeError("primary is unreachable")

        async def gen() -> AsyncIterator[object]:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))])
            raise RuntimeError("provider exploded mid-stream")

        return gen()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    with pytest.raises(RuntimeError):
        asyncio.run(_drain(gw.stream(_request())))

    event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert event.outcome == AuditOutcome.error
    assert event.provenance["model_name"] == "backup"
    assert event.provenance["route_position"] == "fallback"
    assert event.provenance["fallback_from"] == "primary"


def test_all_candidates_failed_event_claims_no_route_position(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """Nothing served, so no candidate holds a position. The terminal
    all-failed record names the task and stays silent about routing rather
    than reporting a placement no model occupied.

    PRESENCE CONTROL (catalogue rows #11/#13, as this file's other two
    absence-shaped tests already carry): the record must still NAME THE TASK.
    Both halves of the docstring are asserted, because only one of them is
    load-bearing against the natural mutant. ``provenance={}`` at the
    all-failed site satisfies both absence assertions perfectly while deleting
    the record's entire provenance — the governance fact the record exists to
    carry — so without this control the two assertions below are green on a
    record that says nothing at all.
    """
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("boom-1")),
            make_model("backup", mock_response=Exception("boom-2")),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    with pytest.raises(ModelCallFailedError):
        asyncio.run(gw.complete(_request()))

    event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert event.outcome == AuditOutcome.error
    # CONTROL — the record is not empty: it names the task it failed on.
    assert event.provenance["task"] == "draft", (
        "presence control failed: the all-failed record carries no task, so the "
        "absence assertions below are satisfied by an empty provenance"
    )
    # ...and it stays silent about a placement no candidate occupied.
    assert "route_position" not in event.provenance
    assert "fallback_from" not in event.provenance


# ---------------------------------------------------------------------------
# C12, over EVERY ``_routing_provenance`` call site, on the lane where the
# hazard is live — one guard for the SHAPE, not one guard per instance.
#
# The two site-specific sweeps above (the ``complete()`` success record and the
# reasoning-exhausted record) each drive a chain with a SINGLE credentialed
# predecessor, which is enough to prove those two records do not quote the
# first failure — and not enough to prove anything about the second, or about
# the third site. The sweep below closes that: three sites, two credentialed
# failures each, exact-equality on the whole passed-over prefix, and a census
# that turns RED if a fourth call site is ever added.
# ---------------------------------------------------------------------------

_LEAKY_FIRST = "http://user:sk-SECRET-ONE@vllm-1.internal:8000"
_LEAKY_SECOND = "http://user:sk-SECRET-TWO@vllm-2.internal:8000"

#: The candidates that fail ahead of the one that serves, in attempt order.
#: TWO of them, each with its OWN credential, so a leak of the SECOND
#: predecessor — the shape a one-predecessor fixture is blind to — is named.
_PASSED_OVER = ("onbox-first", "onbox-second")


def _failing_private_pair() -> list[ModelDef]:
    """The two credentialed ``PRIVATE_VLLM`` candidates that never serve.

    ``PRIVATE_VLLM`` IS THE POINT: on ``FRONTIER_API`` the gateway replaces
    every attempt exception with ``FrontierProviderError(type(exc).__name__)``
    before it reaches ``attempts``/``passed_over``, so a planted credential is
    destroyed by pre-existing code and the sweep becomes unfalsifiable (V8 — a
    fixture shaped to miss the failing case). Here the ORIGINAL exception
    survives, and its text quotes the candidate's own credentialed endpoint,
    exactly as a provider connection error does in production.
    """
    return [
        make_model(
            "onbox-first",
            lane=Lane.PRIVATE_VLLM,
            api_base=_LEAKY_FIRST,
            mock_response=Exception(f"connect to {_LEAKY_FIRST} failed"),
        ),
        make_model(
            "onbox-second",
            lane=Lane.PRIVATE_VLLM,
            api_base=_LEAKY_SECOND,
            mock_response=Exception(f"connect to {_LEAKY_SECOND} failed"),
        ),
    ]


def _private_route(*after: str) -> dict[str, TaskRoute]:
    return {"draft": TaskRoute(model=_PASSED_OVER[0], fallbacks=(_PASSED_OVER[1], *after))}


def _complete_success_provenance(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, monkeypatch: pytest.MonkeyPatch
) -> Mapping[str, object]:
    """Site 1 — ``complete()``'s success record (``gateway.py``, success branch)."""
    settings = make_settings(
        [
            *_failing_private_pair(),
            make_model(
                "onbox-server",
                lane=Lane.PRIVATE_VLLM,
                api_base="http://vllm-3.internal:8000",
                mock_response="recovered",
            ),
        ],
        _private_route("onbox-server"),
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    assert asyncio.run(gw.complete(_request())).model_id == "onbox-server"
    return _success_event(audit_sink.events, "ne.gateway.complete").provenance


def _reasoning_exhausted_provenance(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, monkeypatch: pytest.MonkeyPatch
) -> Mapping[str, object]:
    """Site 2 — the reasoning-exhausted error record, with TWO failures ahead of it."""
    settings = make_settings(
        [
            *_failing_private_pair(),
            make_model(
                "onbox-reasoner", lane=Lane.PRIVATE_VLLM, api_base="http://vllm-3.internal:8000"
            ),
            make_model(
                "onbox-server", lane=Lane.PRIVATE_VLLM, api_base="http://vllm-4.internal:8000"
            ),
        ],
        _private_route("onbox-reasoner", "onbox-server"),
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    def _reply(content: str | None, reasoning: str | None) -> Any:
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning is not None:
            message["reasoning_content"] = reasoning
        return litellm.ModelResponse(
            choices=[{"message": message, "finish_reason": "length" if reasoning else "stop"}],
            usage={"prompt_tokens": 10, "completion_tokens": 4096, "total_tokens": 4106},
        )

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        if model == "onbox-first":
            raise Exception(f"connect to {_LEAKY_FIRST} failed")
        if model == "onbox-second":
            raise Exception(f"connect to {_LEAKY_SECOND} failed")
        if model == "onbox-reasoner":
            return _reply(None, "thought at length and never answered")
        return _reply("recovered", None)

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    assert asyncio.run(gw.complete(_request())).model_id == "onbox-server"
    return _error_event_matching(
        audit_sink.events, "ne.gateway.complete", "reasoning exhausted"
    ).provenance


def _empty_completion_provenance(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, monkeypatch: pytest.MonkeyPatch
) -> Mapping[str, object]:
    """Site 3 — the empty-completion error record, with TWO failures ahead of it.

    Same credentialed private chain as the reasoning-exhausted driver, and for
    the same reason: this record is written while two provider exceptions
    carrying ``api_base`` URLs sit in ``attempts``, so it is exactly where a
    routing key could pick one up.
    """
    settings = make_settings(
        [
            *_failing_private_pair(),
            make_model(
                "onbox-silent", lane=Lane.PRIVATE_VLLM, api_base="http://vllm-3.internal:8000"
            ),
            make_model(
                "onbox-server", lane=Lane.PRIVATE_VLLM, api_base="http://vllm-4.internal:8000"
            ),
        ],
        _private_route("onbox-silent", "onbox-server"),
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    def _reply(content: str | None) -> Any:
        return litellm.ModelResponse(
            choices=[
                {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 182, "total_tokens": 192},
        )

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        if model == "onbox-first":
            raise Exception(f"connect to {_LEAKY_FIRST} failed")
        if model == "onbox-second":
            raise Exception(f"connect to {_LEAKY_SECOND} failed")
        if model == "onbox-silent":
            return _reply(None)
        return _reply("recovered")

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    assert asyncio.run(gw.complete(_request())).model_id == "onbox-server"
    return _error_event_matching(
        audit_sink.events, "ne.gateway.complete", "returned an EMPTY completion"
    ).provenance


def _stream_success_provenance(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, monkeypatch: pytest.MonkeyPatch
) -> Mapping[str, object]:
    """Site 4 — ``_finalize_stream``.

    The site an earlier revision of this file exempted, on the ground that
    ``passed_over: Sequence[str]`` puts no exception object in scope. That
    annotation forbids a NON-STRING; a credential is a string, so it forbids
    nothing that matters here. This driver is the actual closure.
    """
    settings = make_settings(
        [
            *_failing_private_pair(),
            make_model(
                "onbox-server",
                lane=Lane.PRIVATE_VLLM,
                api_base="http://vllm-3.internal:8000",
                mock_response="streamed",
            ),
        ],
        _private_route("onbox-server"),
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    asyncio.run(_drain(gw.stream(_request())))
    return _success_event(audit_sink.events, "ne.gateway.stream").provenance


_C12_SITES: dict[
    str,
    Callable[[RecordingCostSink, RecordingAuditSink, pytest.MonkeyPatch], Mapping[str, object]],
] = {
    "complete-success": _complete_success_provenance,
    "complete-reasoning-exhausted": _reasoning_exhausted_provenance,
    "complete-empty": _empty_completion_provenance,
    "stream-finalize": _stream_success_provenance,
}

#: The enclosing function of each ``_routing_provenance`` call site in the
#: shipped package, and how many calls it makes — the census's expected value.
#: ``complete`` holds three (the success record, the reasoning-exhausted
#: record, and the empty-completion record); ``_finalize_stream`` holds the
#: fourth.
_EXPECTED_CALL_SITES = {"complete": 3, "_finalize_stream": 1}


@pytest.mark.parametrize("site", sorted(_C12_SITES), ids=sorted(_C12_SITES))
def test_every_routing_provenance_site_carries_names_only_over_a_credentialed_private_chain(
    site: str,
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every record that stamps a routing position names DECLARED NAMES only.

    The failure this forbids is concrete, not hypothetical. A later "why did it
    fall back?" enrichment that appended each candidate's failure text to
    ``passed_over`` would land provider error strings on ``fallback_from`` — a
    key ``SpanPane.tsx`` renders — and on ``PRIVATE_VLLM`` those strings are
    the ORIGINAL provider exceptions, which quote the candidate's
    ``api_base``. A one-predecessor fixture cannot see an enrichment that only
    fires from the second candidate onward; this one can.
    """
    provenance = _C12_SITES[site](sink, audit_sink, monkeypatch)
    joined = " ".join(str(value) for value in provenance.values())

    # CONTROL — both passed-over candidates reach this record, by NAME, in
    # order, and nothing else is in the list. Exact equality rather than
    # containment: a mutant that appends failure text alongside the names would
    # satisfy "both names present" while leaking.
    assert provenance["route_position"] == "fallback"
    assert provenance["fallback_from"] == ",".join(_PASSED_OVER), (
        "presence control failed: the passed-over candidates are not recorded as "
        "their declared names, so the absence assertions below prove nothing"
    )

    # ...and NOTHING off either failed candidate rides along.
    assert "sk-SECRET-ONE" not in joined
    assert "sk-SECRET-TWO" not in joined
    assert "vllm-1.internal" not in joined
    assert "vllm-2.internal" not in joined
    assert "connect to" not in joined
    assert "Exception" not in joined


def test_the_swept_sites_are_every_routing_provenance_call_site_in_the_package() -> None:
    """The word EVERY in this file's C12 claim, made falsifiable.

    A sweep over three sites is a statement about the code only while the code
    has three sites. This parses the SHIPPED ``ne.gateway`` package — not a
    hand-kept list — and pins where ``_routing_provenance`` is called. Add a
    fourth call site and this goes RED, which is the point: the sweep above
    has to grow with the code rather than silently under-approximate it.

    Residual, named: this pins the call sites of ``_routing_provenance``, not
    every possible way a future record could stamp a routing key. A record
    that hand-built ``{"route_position": ...}`` without calling the helper is
    invisible here — and would also be outside the helper's own C12 docstring,
    which is the boundary this file's claim is scoped to.
    """
    package_root = Path(str(gateway_package.__file__)).parent
    found: dict[str, int] = {}
    for path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = sum(
                1
                for child in ast.walk(node)
                if isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "_routing_provenance"
            )
            if calls:
                found[node.name] = found.get(node.name, 0) + calls

    assert found == _EXPECTED_CALL_SITES, (
        "the set of _routing_provenance call sites changed; extend "
        "_C12_SITES with a driver for the new site before updating this pin"
    )
    assert sum(found.values()) == len(_C12_SITES)
