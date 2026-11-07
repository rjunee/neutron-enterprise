# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE MANIFEST'S BUDGET UNIT IS NOT THE ENFORCEMENT'S BUDGET UNIT.

WHY THIS FILE EXISTS. Every token figure in ``deploy/agents/registry.toml`` --
the Analyst's, Customer Support's, the Overdue Credit Review's, the Grounded
Analyst's -- is derived in MODEL tokens, as ``characters / 2.93``. Every place
that actually ENFORCES a context budget uses a different estimator entirely:
:class:`ne.contracts.tokens.ByteTokenEstimator`, ``bytes / 1.5 + 8`` (the
ADR-0012 oracle-selected default, overridden nowhere in ``live.py``, the pilot
compose or the chart), applied to the SERIALIZED request -- in
``ne.gateway.gateway``'s pre-flight and in ``ne.runtime.model``'s trim.

The two disagree by roughly a factor of two, and a lane-affordability ceiling
derived from the manifest figures was proposed, built and REVERTED for exactly
that reason: the manifest number is the SUBTRAHEND in such a derivation
(``(input_budget - fixed_prompt) // per_call``), so understating it yields a
ceiling that is too GENEROUS -- a governed number that under-protects while
claiming to protect.

So this is not a test of a feature. It is a MEASURED pin on a known limitation,
here so that:
  * the discrepancy cannot drift silently, and in particular cannot be closed by
    quietly changing one side to match the other without a deliberate decision;
  * anyone who next tries to derive a serving-window ceiling from a manifest
    budget finds the reason it does not work before they ship it; and
  * the registry's own prose (the Customer Support LANE HEADROOM paragraph) has
    an executable witness for the numbers it cites, rather than asserting them.

It asserts a RELATIONSHIP and a DIRECTION, never a frozen literal, so it fails on
a real change of behaviour rather than on any edit to a template description.
"""

from __future__ import annotations

from typing import Any, cast

from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.ontology import OntologyVersion
from ne.brain_ontology.verified import build_verified_answer_tools
from ne.brain_ontology.verified.meridian import load_meridian_template_registry
from ne.contracts.tokens import DEFAULT_BYTES_PER_TOKEN, DEFAULT_PER_CALL_OVERHEAD
from ne.contracts.types import Lane
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute
from ne.runtime.model import catalog_text_for_tool_specs

#: The divisor every budget derivation in ``deploy/agents/registry.toml`` is
#: written in. It appears in this repository ONLY in that file's prose and here.
_REGISTRY_MODEL_TOKEN_DIVISOR = 2.93

#: The five hardship verified-answer templates the Meridian Customer Support
#: Agent grants -- the set whose 5680 characters the registry measures.
_SUPPORT_TEMPLATE_TOOLS = frozenset(
    {
        "read.kg.disaster_declarations",
        "read.kg.customers_in_counties",
        "read.kg.autoloans_for_customer",
        "read.kg.payments_due_window",
        "read.kg.servicing_notes_for_loan",
    }
)


class _SpecOnlyStore:
    """The builder asks a store only for its ontology stamp; handlers never run."""

    def ontology_version(self) -> OntologyVersion:
        return OntologyVersion(sources={}, file_sha256={}, shapes_hash="0" * 64)


def _granted_specs() -> tuple[Any, ...]:
    defs = build_verified_answer_tools(
        registry=load_meridian_template_registry(),
        store=cast(Any, _SpecOnlyStore()),
        expander=cast(Any, None),
        principal_provider=lambda: None,
        settings=OntologySettings(),
        config_hash="a" * 64,
    )
    return tuple(d.spec for d in defs if d.spec.name in _SUPPORT_TEMPLATE_TOOLS)


def _enforcing_estimator() -> Any:
    """The estimator a REAL deployment composes, from the REAL settings type."""
    settings = GatewaySettings(
        models=[
            ModelDef(
                name="private-default",
                litellm_model="hosted_vllm/qwen",
                provider="hosted_vllm",
                lane=Lane.PRIVATE_VLLM,
                context_window=32_768,
            )
        ],
        tasks={"agent": TaskRoute(model="private-default")},
    )
    # Nothing in this tree overrides these, so the default IS the deployed value.
    assert settings.bytes_per_token == DEFAULT_BYTES_PER_TOKEN == 1.5
    assert settings.per_call_overhead == DEFAULT_PER_CALL_OVERHEAD == 8
    return settings.estimator()


def test_the_enforcing_estimator_reads_the_catalog_about_twice_the_manifest_unit() -> None:
    """The measured discrepancy, in both directions of the comparison.

    The registry states 5680 characters for these five entries and converts that
    to 1939 model tokens. Both halves are reproduced here from the REAL renderer
    rather than retyped, and then the SAME text is put through the estimator the
    gateway and the runtime trim actually enforce with.
    """
    specs = _granted_specs()
    assert len(specs) == 5, "the Customer Support grant set moved; re-read the registry"

    header = catalog_text_for_tool_specs(())
    entry_chars = sum(len(catalog_text_for_tool_specs((spec,))) - len(header) for spec in specs)
    # The registry's own character measurement, reproduced.
    assert entry_chars == 5680
    registry_tokens = round(entry_chars / _REGISTRY_MODEL_TOKEN_DIVISOR)
    assert registry_tokens == 1939

    estimator = _enforcing_estimator()
    enforced_tokens = estimator.estimate(catalog_text_for_tool_specs(specs))

    # THE FINDING: the unit the budget is written in is not the unit that is
    # enforced, and the enforced one is the larger -- so a ceiling derived from
    # the manifest figure would be too generous, i.e. wrong in the UNSAFE
    # direction. A range, not a literal: template prose may legitimately move.
    ratio = enforced_tokens / registry_tokens
    assert 1.8 < ratio < 2.2, (
        f"manifest unit and enforcement unit now differ by {ratio:.2f}x "
        f"(registry {registry_tokens} model tokens vs enforced {enforced_tokens}); "
        "if this converged, the registry derivations may finally be usable for a "
        "serving-window ceiling -- decide that deliberately, do not assume it"
    )


def test_five_of_thirteen_catalog_entries_already_exceed_the_whole_declared_prompt() -> None:
    """The sharpest statement of why no ceiling was derived from ``S``.

    The Customer Support derivation computes ``S = 3871`` model tokens for its
    ENTIRE fixed prompt -- system text, all thirteen tool entries, the Skill
    index and the question allowance. In enforcement units, just FIVE of those
    thirteen entries already cost more than that, before the structured-output
    schema, the transport tail or the manifest instructions are counted.

    ``S`` is not a slightly stale number that could be nudged; it is a number in
    a different currency.
    """
    declared_whole_fixed_prompt = 3871  # registry.toml, Customer Support budgets
    enforced_five_entries = _enforcing_estimator().estimate(
        catalog_text_for_tool_specs(_granted_specs())
    )
    assert enforced_five_entries > declared_whole_fixed_prompt, (
        f"five catalog entries estimate to {enforced_five_entries} enforced tokens "
        f"against a declared whole-prompt {declared_whole_fixed_prompt}"
    )
