# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-5 §4.5: the single token-budget resolver + cross-seam envelope parity.

``resolve_verified_token_budget`` is the ONLY place the effective verified-read
token budget is computed (grep: no other reader of ``verified_max_result_tokens``
outside config/declaration). At merge BOTH seams pass ``task_budget=None`` (the
configured-cap-only, packing-default-OFF posture), so the executor-path structured
record and the HTTP-invoke wire view carry byte-identical envelope fields.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ne.api.verified_tools import resolve_verified_token_budget
from ne.api.wire import VerifiedAnswerView
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.brain_ontology import PropertyGraphLift, load_template_registry
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import ValidatingTripleStore
from ne.brain_ontology.quarantine import SqliteTripleQuarantine
from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.brain_ontology.verified import execute_template
from ne.contracts.types import Principal

from .test_verified_answer_tools import _account  # seeded-node helper


class _NullSink:
    def emit(self, event: Any) -> None:
        return None


def _seed() -> ValidatingTripleStore:
    settings = OntologySettings(include_ontology_graph=False)
    store = ValidatingTripleStore(
        OxigraphBackend(),
        build_validator(settings),
        SqliteTripleQuarantine(),
        _NullSink(),
        settings=settings,
        config_hash="s" * 64,
        compiled_shapes=load_compiled_shapes(),
    )

    async def _do() -> None:
        mem = InMemoryGraphStore()
        await mem.upsert_nodes([_account("acc_a", ["alpha"]), _account("acc_b", ["alpha"])])
        await PropertyGraphLift(mem, store).run()

    asyncio.run(_do())
    return store


_FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "ne-brain-ontology"
    / "tests"
    / "data"
    / "verified_templates"
)


# --- resolve_verified_token_budget unit cases -------------------------------


def test_resolve_none_config_returns_none_regardless_of_task_budget() -> None:
    settings = OntologySettings(verified_max_result_tokens=None)
    assert resolve_verified_token_budget(settings) is None
    assert resolve_verified_token_budget(settings, 16000) is None  # master switch off


def test_resolve_configured_only_returns_configured() -> None:
    settings = OntologySettings(verified_max_result_tokens=4000)
    assert resolve_verified_token_budget(settings) == 4000  # HTTP/human seam: cap only


def test_resolve_task_budget_min_applies() -> None:
    settings = OntologySettings(verified_max_result_tokens=4000)
    # floor(0.5 * 16000 / 8) = 1000 < 4000 -> the derived share wins.
    assert resolve_verified_token_budget(settings, 16000) == 1000


def test_resolve_clamps_to_floor_256() -> None:
    settings = OntologySettings(verified_max_result_tokens=4000)
    # floor(0.5 * 1000 / 8) = 62 -> clamped up to the 256 floor.
    assert resolve_verified_token_budget(settings, 1000) == 256


# --- cross-seam envelope parity (§7.2): default None on both seams -----------


def test_seam_parity_default_budget_byte_identical_envelope() -> None:
    settings = OntologySettings(verified_max_result_tokens=None)
    # Both seams compute the SAME budget from the ONE resolver: None (packing off).
    budget = resolve_verified_token_budget(settings)
    assert budget is None

    store = _seed()
    template = next(
        t
        for t in load_template_registry(_FIXTURES).templates
        if t.template_id == "accounts-directory"
    )

    class _StubExpander:
        def expand(self, principal: Principal | str) -> frozenset[str]:
            if isinstance(principal, Principal):
                return frozenset({principal.id, *principal.groups})
            return frozenset({principal})

    async def _run() -> Any:
        return await execute_template(
            template,
            {},
            store=store,
            expander=_StubExpander(),
            principal=Principal(id="user:a", groups=("alpha",)),
            config_hash="s" * 64,
            max_rows=500,
            max_result_tokens=budget,
        )

    # Executor-path structured record (what the chat binding records) ...
    answer = asyncio.run(_run())
    # ... and the HTTP-invoke wire view (built field-by-field from the same answer).
    view = VerifiedAnswerView(
        template_id=answer.template_id,
        template_version=answer.template_version,
        args=answer.args,
        columns=answer.columns,
        rows=answer.rows,
        citations=answer.citations,
        truncated=answer.truncated,
        row_limit=answer.row_limit,
        returned_rows=answer.returned_rows,
        total_rows=answer.total_rows,
        truncation_reason=answer.truncation_reason,
        config_hash=answer.config_hash,
        ontology_version=answer.ontology_version,
        executed_at=answer.executed_at,
    )

    # Byte-identical envelope fields across both seams (all except executed_at).
    assert view.returned_rows == answer.returned_rows == len(answer.rows)
    assert view.total_rows == answer.total_rows
    assert view.truncation_reason == answer.truncation_reason is None
    assert view.rows == answer.rows
    assert view.citations == answer.citations
    assert view.truncated == answer.truncated is False
