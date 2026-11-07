# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The closed-loop prompt<->wall guard (NE-T2X-PROMPT-SCHEMA).

Every OTHER extraction test feeds ``ExtractionJob._validate_wall`` a
PRE-SHAPED fixture — the prompt<->wall contract itself was never exercised
end to end, which is exactly how the shipped prompt could claim a schema it
never stated (see ``ne.brain_graph.extraction.prompt``'s module docstring)
and still pass every existing test. This module closes that loop: a
scripted gateway that ECHOES BACK the schema example parsed straight out of
the real, rendered system prompt, run through the real
:class:`~ne.brain_graph.extraction.job.ExtractionJob`. A drift on EITHER
side of the contract — the prompt's example stops matching what it claims,
or the wall's requirements change — turns the round-trip test red
(docs/testing/vacuous-guards.md's mutation-bidirectional property).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pydantic
import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from ne.audit import ensure_transactional_sqlite
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.extraction import EXTRACTION_TASK
from ne.brain_graph.extraction.job import ExtractionChunk, ExtractionJob
from ne.brain_graph.extraction.prompt import (
    _CONFIDENCE_FIELD,
    _KIND_FIELD,
    _LINK_REQUIRED,
    _NEW_EDGE_ENDPOINT_HINTS,
    _NEW_EDGE_REQUIRED,
    _NEW_ENTITY_REQUIRED,
    _OBJECT_CLASS_HINT,
    _UNADVERTISED_OPTIONAL,
    EXAMPLE_EXISTING_KEY,
    example_properties,
    example_triple,
    render_extraction_system_prompt,
    schema_example_entries,
)
from ne.brain_graph.extraction.writer import _PLATFORM_OWNED_KEYS, validate_extracted_properties
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.flywheel.proposals import GraphExtractionAssertionKind, SqlAlchemyProposalStore
from ne.schema import EDGE_DEFS, ENTITY_CONTRACTS
from ne.schema.generated.operational import DocumentType

_CFG = "e" * 64


def _prompt_without_example_block() -> str:
    """The rendered prompt with the worked JSON example block STRIPPED OUT --
    same technique ``_EchoPromptExampleGateway`` uses to find the block
    (``rindex`` + ``raw_decode``), applied to delete rather than parse it.

    WHY: Argus's round-3 mutation renamed the per-kind field names in the
    hand-written PROSE sentences only (``entity_key`` -> ``entity_id``,
    ``display`` -> ``label``), leaving the DERIVED generator and the wall
    untouched. Every containment pin below was, at the time, run against the
    FULL prompt -- and every one of those names ALSO appears, unmutated, in
    the example block's own JSON text, so the pins stayed green even though
    the prose no longer told the truth. Stripping the example block first
    means these pins can only be satisfied by the prose itself.
    """
    prompt = render_extraction_system_prompt()
    idx = prompt.rindex('{"assertions"')
    _envelope, end = json.JSONDecoder().raw_decode(prompt[idx:])
    return prompt[:idx] + prompt[idx + end :]


@pytest.fixture(autouse=True)
def _frozen_config() -> Any:
    reset_config_hash_for_testing()
    digest = freeze_config_hash(BrainGraphSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


class _StaticScanStore:
    """Scan-only (T-ER-1 mirror): a single, fixed candidate vocabulary."""

    def __init__(self, nodes: Sequence[Mapping[str, Any]]) -> None:
        self._nodes = nodes

    async def scan_nodes(self, entity_class: str | None = None) -> Sequence[Mapping[str, Any]]:
        return self._nodes


class _StaticChunkSource:
    def __init__(self, by_source: dict[str, list[ExtractionChunk]]) -> None:
        self._by_source = by_source

    async def chunks(self, source_system: str) -> Sequence[ExtractionChunk]:
        return self._by_source.get(source_system, [])


class _NullAudit:
    async def append(self, event: Any) -> None:
        return None

    async def verify(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class _EchoPromptExampleGateway:
    """The load-bearing trick: parses the schema example block straight out
    of the INCOMING request's own system prompt (``messages[0].content``)
    and echoes it back verbatim as the model's completion. A model that
    follows the prompt's own worked example EXACTLY."""

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []
        self.parsed_envelopes: list[dict[str, Any]] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        content = request.messages[0].content
        # rfind, not find: the role sentence illustrates the envelope SHAPE
        # with a literal ``{"assertions": [...]}`` (not valid JSON — "..." is
        # not a value); the LAST occurrence is the real, fully-populated
        # example block this test round-trips through the wall.
        idx = content.rindex('{"assertions"')
        envelope, _end = json.JSONDecoder().raw_decode(content[idx:])
        self.parsed_envelopes.append(envelope)
        return Completion(
            text=json.dumps(envelope),
            model_id="test",
            usage=Usage(prompt_tokens=0, completion_tokens=0),
        )

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError


class _ScriptedGateway:
    def __init__(self, text: str) -> None:
        self._text = text
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        return Completion(
            text=self._text, model_id="test", usage=Usage(prompt_tokens=0, completion_tokens=0)
        )

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError


def _engine() -> sa.Engine:
    from ne.flywheel.review import ensure_review_schema

    eng = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    ensure_transactional_sqlite(eng)
    ensure_review_schema(eng)
    return eng


def _settings() -> BrainGraphSettings:
    return BrainGraphSettings(
        enabled=True,
        extraction_enabled=True,
        extraction_sources=("docs",),
        extraction_min_confidence=0.1,
        extraction_auto_link_threshold=1.0,
    )


def _candidate_node() -> dict[str, Any]:
    return {
        "key": EXAMPLE_EXISTING_KEY,
        "entity_class": example_triple().object_class,
        "display_name": "Existing Example",
    }


def _chunk() -> ExtractionChunk:
    return ExtractionChunk(
        chunk_ref="c1",
        document_ref="d1",
        source_system="docs",
        text="chunk text",
        acl=("eng",),
    )


def _run(gateway: Any) -> Any:
    engine = _engine()
    job = ExtractionJob(
        _StaticScanStore([_candidate_node()]),
        engine,
        _settings(),
        gateway,
        _StaticChunkSource({"docs": [_chunk()]}),
        audit_sink=_NullAudit(),
        config_hash=_CFG,
        clock=lambda: datetime(2026, 7, 27, tzinfo=UTC),
    )
    report = asyncio.run(job.run())
    return report, engine


# -- the load-bearing guard ---------------------------------------------------


def test_prompt_example_block_round_trips_through_the_wall() -> None:
    gateway = _EchoPromptExampleGateway()
    report, engine = _run(gateway)

    # Parser control: the raw_decode grabbed a real, non-trivial envelope, not
    # something vacuous.
    assert len(gateway.parsed_envelopes) == 1
    parsed_assertions = gateway.parsed_envelopes[0]["assertions"]
    assert len(parsed_assertions) == 3
    assert {e["kind"] for e in parsed_assertions} == {
        GraphExtractionAssertionKind.LINK.value,
        GraphExtractionAssertionKind.NEW_ENTITY.value,
        GraphExtractionAssertionKind.NEW_EDGE.value,
    }

    assert report.quarantined_invalid == 0
    assert report.mentions == 3
    assert report.discarded_below_threshold == 0
    assert report.links_auto == 0
    assert report.batches_built == 1

    proposals = SqlAlchemyProposalStore(engine).read_proposals()
    assert len(proposals) == 1
    stored_assertions = proposals[0].payload["assertions"]
    assert len(stored_assertions) == 3
    assert {a["kind"] for a in stored_assertions} == {
        GraphExtractionAssertionKind.LINK.value,
        GraphExtractionAssertionKind.NEW_ENTITY.value,
        GraphExtractionAssertionKind.NEW_EDGE.value,
    }


def test_rig_observed_shape_still_quarantined() -> None:
    """Regression pin of the LIVE rig-observed defect (2026-07-29) AND proof
    the wall was NOT weakened to accommodate it: the model's invented shape
    (no ``kind``, no ``confidence``, a hallucinated ``WORKS_FOR``/``Organization``
    vocabulary) still quarantines wholesale."""
    text = json.dumps(
        {
            "assertions": [
                {
                    "edge_kind": "WORKS_FOR",
                    "from_class": "Person",
                    "from_key": "p1",
                    "to_class": "Organization",
                    "to_key": "o1",
                    "properties": {"role": "head of analytics"},
                }
            ]
        }
    )
    report, _engine = _run(_ScriptedGateway(text))
    assert report.mentions == 1
    assert report.quarantined_invalid == 1
    assert report.batches_built == 0


def test_example_missing_confidence_is_quarantined() -> None:
    """The no-silent-default governance DECISION: the wall REQUIRES a numeric
    ``confidence`` on every assertion and quarantines without one.
    ``extraction_min_confidence``/``extraction_auto_link_threshold`` both
    route on this value — if the job (or a future edit) silently synthesized
    a default confidence, both thresholds would become meaningless. This
    test pins that no such default exists: the prompt's own three examples,
    stripped of ``confidence``, quarantine every single one."""
    entries = list(schema_example_entries())
    no_confidence = [{k: v for k, v in e.items() if k != "confidence"} for e in entries]
    text = json.dumps({"assertions": no_confidence})
    report, _engine = _run(_ScriptedGateway(text))
    assert report.mentions == 3
    assert report.quarantined_invalid == 3
    assert report.batches_built == 0

    # Second arm: rename "kind" -> "assertion_type" (confidence intact) --
    # every entry loses its dispatchable kind and is quarantined too.
    renamed = []
    for e in entries:
        entry = dict(e)
        entry["assertion_type"] = entry.pop("kind")
        renamed.append(entry)
    text2 = json.dumps({"assertions": renamed})
    report2, _engine2 = _run(_ScriptedGateway(text2))
    assert report2.mentions == 3
    assert report2.quarantined_invalid == 3
    assert report2.batches_built == 0


def test_prompt_states_every_wall_vocabulary_item() -> None:
    """Secondary substring pins (kept secondary per the vacuous-guard
    catalogue; the round-trip test above is the load-bearing guard).

    RE-SCOPED (Argus round 3, NE-T2X-PROMPT-SCHEMA REVISE): every vocabulary
    containment assert below runs against the prompt with its worked JSON
    EXAMPLE BLOCK STRIPPED (see :func:`_prompt_without_example_block`), not
    the full prompt. Argus proved that renaming the per-kind field names in
    the hand-written PROSE sentences ONLY (``entity_key`` -> ``entity_id``,
    ``display`` -> ``label``) -- leaving the DERIVED generator and the wall
    untouched -- left every pin here green when it ran against the full
    prompt, because the SAME names also appear, unmutated, in the example
    block's own JSON text. Stripping the example first means these pins can
    only be satisfied by the prose independently stating every field name;
    the P2 fix (deriving the prose from the same constants the example uses)
    makes that structurally impossible to violate going forward.
    """
    prose = _prompt_without_example_block()
    prompt = render_extraction_system_prompt()

    for entity_class in ENTITY_CONTRACTS:
        assert entity_class in prose

    for triple in EDGE_DEFS:
        assert triple.edge_kind in prose

    assert '"kind"' in prose
    assert '"confidence"' in prose
    for kind in GraphExtractionAssertionKind:
        assert f'"{kind.value}"' in prose

    for member in DocumentType:
        assert f'"{member.value}"' in prose

    # NEW (Argus P2 close): the per-kind field vocabulary -- the exact names
    # the old hand-written prose could silently drift on -- pinned directly
    # against the derived-and-stripped prose.
    all_field_names = (
        *_LINK_REQUIRED,
        *_NEW_ENTITY_REQUIRED,
        *_NEW_EDGE_REQUIRED,
        *_NEW_EDGE_ENDPOINT_HINTS,
    )
    for name in all_field_names:
        assert f'"{name}"' in prose

    # Negative controls -- kept on the FULL prompt (unaffected by the example
    # block either way: neither string appears there).
    assert "schema you were given" not in prompt
    assert "Organization" not in prompt

    # A-P0-2 minimization pins (Argus P3, prior round) -- kept on the FULL
    # prompt as today: the writer's own strip-list and the
    # deliberately-unadvertised provenance fields must never appear as
    # advertised entity-class properties. Field names are rendered as
    # "name (type)" (see ``_format_fields``); no legitimate contract field
    # name collides with this substring for any of these keys (verified
    # against ``ENTITY_CONTRACTS`` at authoring time).
    for platform_key in _PLATFORM_OWNED_KEYS:
        assert f"{platform_key} (" not in prompt
    for unadvertised_key in _UNADVERTISED_OPTIONAL:
        assert f"{unadvertised_key} (" not in prompt


def test_new_edge_object_class_hint_resolves_same_response_display() -> None:
    """The one advertised field NAME no prior test exercised against the real
    wall: ``object_class`` (:data:`_OBJECT_CLASS_HINT`). Three arms, all
    against the real :class:`~ne.brain_graph.extraction.job.ExtractionJob`:
    the hint present resolves the edge; absent or misnamed, it does not --
    proving the name is load-bearing in BOTH directions, not just advertised.
    """
    triple = example_triple()
    candidate_node = {
        "key": "subj-1",
        "entity_class": triple.subject_class,
        "display_name": "Existing Subject",
    }
    display = f"Example {triple.object_class}"
    new_entity_entry = {
        _KIND_FIELD: GraphExtractionAssertionKind.NEW_ENTITY.value,
        **dict(
            zip(
                _NEW_ENTITY_REQUIRED,
                (triple.object_class, display, example_properties(triple.object_class)),
                strict=True,
            )
        ),
        _CONFIDENCE_FIELD: 0.5,
    }

    def _edge_entry(*, hint_key: str | None) -> dict[str, Any]:
        edge_kind_field, subject_key_field, object_key_field = _NEW_EDGE_REQUIRED
        entry: dict[str, Any] = {
            _KIND_FIELD: GraphExtractionAssertionKind.NEW_EDGE.value,
            edge_kind_field: triple.edge_kind,
            subject_key_field: "subj-1",
            object_key_field: display,
            _CONFIDENCE_FIELD: 0.5,
        }
        if hint_key is not None:
            entry[hint_key] = triple.object_class
        return entry

    def _run_with_candidate(assertions: list[dict[str, Any]]) -> Any:
        engine = _engine()
        job = ExtractionJob(
            _StaticScanStore([candidate_node]),
            engine,
            _settings(),
            _ScriptedGateway(json.dumps({"assertions": assertions})),
            _StaticChunkSource({"docs": [_chunk()]}),
            audit_sink=_NullAudit(),
            config_hash=_CFG,
            clock=lambda: datetime(2026, 7, 27, tzinfo=UTC),
        )
        report = asyncio.run(job.run())
        return report

    # Arm 1: hint present -- both mentions resolve, one batch, 0 quarantined.
    report_hint = _run_with_candidate([new_entity_entry, _edge_entry(hint_key=_OBJECT_CLASS_HINT)])
    assert report_hint.quarantined_invalid == 0
    assert report_hint.mentions == 2
    assert report_hint.batches_built == 1

    # Arm 2: hint absent -- the edge's object endpoint cannot resolve (it is
    # not a candidate key and there is no class hint to look it up in
    # local_entities), so it is quarantined; the new_entity mention itself
    # still lands.
    report_absent = _run_with_candidate([new_entity_entry, _edge_entry(hint_key=None)])
    assert report_absent.quarantined_invalid == 1
    assert report_absent.mentions == 2

    # Arm 3: hint present but under the WRONG key name -- same failure mode
    # as absent; proves the field NAME "object_class" itself is load-bearing,
    # not just "some hint key".
    misnamed = _edge_entry(hint_key=None)
    misnamed["object_klass"] = triple.object_class
    report_misnamed = _run_with_candidate([new_entity_entry, misnamed])
    assert report_misnamed.quarantined_invalid == 1
    assert report_misnamed.mentions == 2


def test_every_entity_class_example_properties_pass_the_wall() -> None:
    """The correct-by-derivation-not-by-sort-order pin (Argus P3 close): every
    entity class's :func:`example_properties`, required-only AND
    required+optional, must pass the real
    ``validate_extracted_properties`` wall -- not just ``Document`` (the
    class :func:`example_triple`'s ``EDGE_DEFS`` sort order happens to pick
    for the rendered prompt's worked example). Fails on the PRE-fix
    ``_sample_value`` for ``Order`` (``order_number``/``currency`` patterns)
    and ``Person`` (``email`` pattern) -- see the module docstring and PR
    body for the before/after evidence.
    """
    for entity_class in sorted(ENTITY_CONTRACTS):
        validate_extracted_properties(entity_class, example_properties(entity_class), "example-key")
        validate_extracted_properties(
            entity_class,
            example_properties(entity_class, include_optional=True),
            "example-key",
        )

    # Control arm: the oracle bites. A deliberately pattern-violating
    # ``order_number`` must still raise -- proves the pass loop above is not
    # vacuously satisfied by a validator that accepts anything.
    with pytest.raises(pydantic.ValidationError):
        validate_extracted_properties(
            "Order",
            {**example_properties("Order"), "order_number": "example"},
            "example-key",
        )


def test_build_request_omits_response_format_pending_gateway_support() -> None:
    """NE-T2X-PROMPT-SCHEMA PRE-DECIDED FALLBACK, taken: the build brief's Step
    2 called for ``params={"response_format": {"type": "json_object"}}`` on
    ``CompletionRequest`` IF the gateway/rig support it. The rig's vLLM DOES
    accept the param over raw HTTP (verified: a direct
    ``/v1/chat/completions`` POST with it returns 200) -- the rejection is NOT
    a provider/rig capability gap. It reproduces with zero network:
    ``CompletionRequest.params`` is an ``AnyMap``, so pydantic freezes the
    nested ``{"type": "json_object"}`` dict into a ``FrozenDict``; ``ne.
    gateway.gateway``'s ``_call_params`` only thaws the OUTER layer
    (``dict(request.params)``), so litellm's
    ``type_to_response_format_param`` receives a ``FrozenDict`` where it
    requires ``isinstance(x, dict)`` and raises ``TypeError: Unsupported
    response_format type - FrozenDict(...)``. That is a genuine gateway gap
    (no request param carrying a nested object survives ``_call_params``
    today, not just ``response_format``) -- filed in the PR body as a
    follow-up for the gateway owner; fixing ``gateway.py`` is out of this
    unit's ``mayTouch``. (Gateway gap since CLOSED by NE-GATEWAY-FROZENDICT:
    ``_call_params`` now thaws nested params at the egress boundary. This
    test still pins the DROP deliberately -- the prompt fix is verified
    sufficient standalone, C3 -- until a follow-up unit wires
    ``response_format`` and flips this pin to presence.) Per the
    pre-decided fallback the param is DROPPED entirely; the prompt (not a
    response-format constraint) is the fix, and is verified sufficient
    standalone against the live rig. This test pins the ABSENCE of the
    param (the fallback actually taken), not its presence."""
    engine = _engine()
    job = ExtractionJob(
        _StaticScanStore([_candidate_node()]),
        engine,
        _settings(),
        _ScriptedGateway('{"assertions": []}'),
        _StaticChunkSource({"docs": [_chunk()]}),
        audit_sink=_NullAudit(),
        config_hash=_CFG,
        clock=lambda: datetime(2026, 7, 27, tzinfo=UTC),
    )
    request = job._build_request("dummy|line", _chunk())  # private seam, exercised directly here
    assert "response_format" not in dict(request.params)
    assert request.task == EXTRACTION_TASK
    assert "chunk text" in request.messages[-1].content
