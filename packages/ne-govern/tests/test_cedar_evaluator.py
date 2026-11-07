# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CedarEvaluator: bundle parsing, fail-closed validation, declared-tier marshalling.

``cedarpy`` is imported DIRECTLY (it is a dev dependency of ne-govern) — a
silent ``importorskip`` would silently disable the parity gate this unit exists
to provide.
"""

from __future__ import annotations

from pathlib import Path

import cedarpy  # noqa: F401 - imported directly on purpose, see module docstring
import pytest

from ne.govern.bundle import read_policy_version
from ne.govern.cedar.evaluator import CedarEvaluator
from ne.govern.errors import PolicyBackendError

from .conftest import load_vectors, oracle_doc_for

_POLICIES_DIR = Path(__file__).parent.parent / "policies"
_VECTORS = load_vectors()


def _rule(
    *,
    rule_id: str,
    ne_effect: str,
    tier: str,
    reason: str,
    obligations: str,
    policy_version: str = "1.0.0",
    keyword: str = "permit",
    cond: str = "true",
) -> str:
    return (
        f'@id("{rule_id}")\n'
        f'@ne_effect("{ne_effect}")\n'
        f'@ne_tier("{tier}")\n'
        f'@ne_reason("{reason}")\n'
        f'@ne_obligations("{obligations}")\n'
        f'@policy_version("{policy_version}")\n'
        f"{keyword} (principal, action, resource) when {{\n\t{cond}\n}};\n"
    )


def _bundle(*rules: str) -> str:
    return "// test bundle\n\n" + "\n".join(rules)


def _valid_eight_rules() -> list[str]:
    return [
        _rule(
            rule_id="a",
            ne_effect="deny",
            tier="null",
            reason="a",
            obligations="{}",
            keyword="forbid",
            cond='context.principal_id == ""',
        ),
        _rule(
            rule_id="b",
            ne_effect="deny",
            tier="null",
            reason="b",
            obligations="{}",
            keyword="forbid",
            cond="false",
        ),
        _rule(
            rule_id="c",
            ne_effect="allow",
            tier="0",
            reason="c",
            obligations="{}",
            keyword="permit",
            cond='context.principal_id != "" && context.ne_action like "read.*"',
        ),
        _rule(
            rule_id="d",
            ne_effect="allow",
            tier="1",
            reason="d",
            obligations='{\\"daily_digest\\": true}',
            keyword="permit",
            cond='context.ne_action like "write.internal.*"',
        ),
        _rule(
            rule_id="e",
            ne_effect="require-approval",
            tier="2",
            reason="e",
            obligations='{\\"per_item_human_approval\\": true}',
            keyword="forbid",
            cond='context.ne_action like "write.external.*"',
        ),
        _rule(
            rule_id="f",
            ne_effect="require-approval",
            tier="3",
            reason="f",
            obligations='{\\"named_approver\\": true}',
            keyword="forbid",
            cond='context.ne_action like "finance.*"',
        ),
        _rule(
            rule_id="e2",
            ne_effect="require-approval",
            tier="2",
            reason="e2",
            obligations='{\\"per_item_human_approval\\": true, \\"provisional_context\\": true}',
            keyword="forbid",
            cond="false",
        ),
        _rule(
            rule_id="f2",
            ne_effect="require-approval",
            tier="3",
            reason="f2",
            obligations=(
                '{\\"named_approver\\": true, \\"second_check\\": true, '
                '\\"provisional_context\\": true}'
            ),
            keyword="forbid",
            cond="false",
        ),
    ]


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "bundle.cedar"
    path.write_text(text, encoding="utf-8")
    return path


# --- packaged bundle -------------------------------------------------------


def test_packaged_bundle_loads_and_policy_version_matches_rego() -> None:
    evaluator = CedarEvaluator()
    assert evaluator.policy_version == read_policy_version(_POLICIES_DIR)
    assert evaluator.policy_version == "1.5.0"


def test_exactly_eight_rules_with_unique_ids() -> None:
    evaluator = CedarEvaluator()
    ids = [rule.rule_id for rule in evaluator._rules]
    assert len(ids) == 8
    assert len(set(ids)) == 8


# --- fail-closed on every malformed-bundle variant --------------------------


def test_malformed_bundle_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyBackendError):
        CedarEvaluator(policy_path=tmp_path / "does-not-exist.cedar")


def test_malformed_bundle_seven_rules_fails_closed(tmp_path: Path) -> None:
    path = _write(tmp_path, _bundle(*_valid_eight_rules()[:7]))
    with pytest.raises(PolicyBackendError, match="8 rules"):
        CedarEvaluator(policy_path=path)


def test_malformed_bundle_unknown_effect_fails_closed(tmp_path: Path) -> None:
    rules = _valid_eight_rules()
    rules[0] = _rule(
        rule_id="a",
        ne_effect="maybe",
        tier="null",
        reason="a",
        obligations="{}",
        keyword="forbid",
        cond='context.principal_id == ""',
    )
    path = _write(tmp_path, _bundle(*rules))
    with pytest.raises(PolicyBackendError, match="ne_effect"):
        CedarEvaluator(policy_path=path)


def test_malformed_bundle_non_json_obligations_fails_closed(tmp_path: Path) -> None:
    rules = _valid_eight_rules()
    rules[2] = _rule(
        rule_id="c",
        ne_effect="allow",
        tier="0",
        reason="c",
        obligations="not json",
        keyword="permit",
        cond='context.ne_action like "read.*"',
    )
    path = _write(tmp_path, _bundle(*rules))
    with pytest.raises(PolicyBackendError, match="ne_obligations"):
        CedarEvaluator(policy_path=path)


def test_malformed_bundle_mixed_policy_version_fails_closed(tmp_path: Path) -> None:
    rules = _valid_eight_rules()
    rules[5] = _rule(
        rule_id="f",
        ne_effect="require-approval",
        tier="3",
        reason="f",
        obligations='{\\"named_approver\\": true}',
        policy_version="9.9.9",
        keyword="forbid",
        cond='context.ne_action like "finance.*"',
    )
    path = _write(tmp_path, _bundle(*rules))
    with pytest.raises(PolicyBackendError, match="policy_version"):
        CedarEvaluator(policy_path=path)


def test_malformed_bundle_permit_conservatism_guardian_fails_closed(tmp_path: Path) -> None:
    # A require-approval rule MUST be a Cedar `forbid`; making it `permit`
    # trips the conservatism guardian even though every other field is valid.
    rules = _valid_eight_rules()
    rules[4] = _rule(
        rule_id="e",
        ne_effect="require-approval",
        tier="2",
        reason="e",
        obligations='{\\"per_item_human_approval\\": true}',
        keyword="permit",
        cond='context.ne_action like "write.external.*"',
    )
    path = _write(tmp_path, _bundle(*rules))
    with pytest.raises(PolicyBackendError, match="conservatism guardian"):
        CedarEvaluator(policy_path=path)


def test_malformed_bundle_tier3_allow_c12_backstop_fails_closed(tmp_path: Path) -> None:
    # A tier-3 rule carrying ne_effect "allow" (with a matching `permit` keyword
    # so the conservatism guardian alone would NOT catch it) must still be
    # rejected by the dedicated C12 backstop.
    rules = _valid_eight_rules()
    rules[5] = _rule(
        rule_id="f",
        ne_effect="allow",
        tier="3",
        reason="f",
        obligations="{}",
        keyword="permit",
        cond='context.ne_action like "finance.*"',
    )
    path = _write(tmp_path, _bundle(*rules))
    with pytest.raises(PolicyBackendError, match="C12"):
        CedarEvaluator(policy_path=path)


def test_overlapping_rules_fail_closed(tmp_path: Path) -> None:
    rules = _valid_eight_rules()
    # Make TWO rules always match (both `true`) so any input has >=2 matches.
    rules[0] = _rule(
        rule_id="a",
        ne_effect="deny",
        tier="null",
        reason="a",
        obligations="{}",
        keyword="forbid",
        cond="true",
    )
    rules[1] = _rule(
        rule_id="b",
        ne_effect="deny",
        tier="null",
        reason="b",
        obligations="{}",
        keyword="forbid",
        cond="true",
    )
    path = _write(tmp_path, _bundle(*rules))
    evaluator = CedarEvaluator(policy_path=path)
    with pytest.raises(PolicyBackendError, match="mutually-exclusive"):
        evaluator.evaluate({"principal": {"id": "u1"}, "action": "read.document", "context": {}})


def test_no_match_fails_closed(tmp_path: Path) -> None:
    rules = [
        _rule(
            rule_id=name,
            ne_effect="deny",
            tier="null",
            reason=name,
            obligations="{}",
            keyword="forbid",
            cond="false",
        )
        for name in ("a", "b", "c", "d", "e", "f", "e2", "f2")
    ]
    path = _write(tmp_path, _bundle(*rules))
    evaluator = CedarEvaluator(policy_path=path)
    with pytest.raises(PolicyBackendError, match="mutually-exclusive"):
        evaluator.evaluate({"principal": {"id": "u1"}, "action": "read.document", "context": {}})


# --- declared-tier marshalling matrix ---------------------------------------


@pytest.mark.parametrize(
    "vector_name",
    [
        "declared-bool-invalid",
        "declared-float-integral-valid",
        "declared-float-fractional-invalid",
        "declared-string-invalid",
        "declared-negative-invalid",
    ],
)
def test_declared_tier_marshalling(vector_name: str) -> None:
    vector = next(v for v in _VECTORS if v["name"] == vector_name)
    evaluator = CedarEvaluator()
    raw = evaluator.evaluate(vector["input"])
    doc = {
        "effect": raw.effect,
        "tier": raw.tier,
        "reason": raw.reason,
        "obligations": raw.obligations,
        "approval_window_seconds": raw.approval_window_seconds,
        "approval_window_declared": raw.approval_window_declared,
        "standing_approval_seconds": raw.standing_approval_seconds,
        "policy_version": raw.policy_version,
    }
    assert doc == oracle_doc_for(vector)


def test_declared_tier_marshalling_out_of_i64_range_omitted() -> None:
    evaluator = CedarEvaluator()
    raw = evaluator.evaluate(
        {
            "principal": {"id": "u1"},
            "action": "read.document",
            "context": {"tier": 10**20},
        }
    )
    # Out-of-i64-range values are OMITTED entirely -> falls back to the
    # unescalated class tier (0, read.document).
    assert raw.effect == "allow"
    assert raw.tier == 0


# --- context_provisional / extraction_mode marshalling (NE-T2X-5, §6.3) ----


@pytest.mark.parametrize(
    "vector_name",
    [
        "provisional-write-internal-escalates",
        "provisional-yolo-threads-but-does-not-escalate",
        "provisional-absent-control",
        "provisional-write-external-escalates-to-3",
        "provisional-read-does-not-escalate",
        "provisional-finance-clamps-at-3",
        "provisional-mode-absent-fails-toward-escalation",
        "provisional-nonboolean-does-not-escalate",
        "provisional-declared-2-on-internal-write-escalates-to-3",
    ],
)
def test_provisional_context_vectors_match_oracle(vector_name: str) -> None:
    vector = next(v for v in _VECTORS if v["name"] == vector_name)
    evaluator = CedarEvaluator()
    raw = evaluator.evaluate(vector["input"])
    doc = {
        "effect": raw.effect,
        "tier": raw.tier,
        "reason": raw.reason,
        "obligations": raw.obligations,
        "approval_window_seconds": raw.approval_window_seconds,
        "approval_window_declared": raw.approval_window_declared,
        "standing_approval_seconds": raw.standing_approval_seconds,
        "policy_version": raw.policy_version,
    }
    assert doc == oracle_doc_for(vector)


@pytest.mark.parametrize("value", [True])
def test_context_provisional_true_is_admitted_and_escalates(value: bool) -> None:
    # Bidirectional positive: the JSON boolean True is admitted into the Cedar
    # context and DOES change the outcome (an escalation actually fires) —
    # proves this is a value-discriminating assertion, not field-presence.
    evaluator = CedarEvaluator()
    raw = evaluator.evaluate(
        {
            "principal": {"id": "u1"},
            "action": "write.internal.notes.append",
            "context": {"context_provisional": value, "extraction_mode": "read_flagged"},
        }
    )
    assert raw.effect == "require-approval"
    assert raw.tier == 2
    assert raw.obligations.get("provisional_context") is True


@pytest.mark.parametrize("value", ["true", 1, 1.0, None, "True", 0])
def test_context_provisional_non_true_values_are_omitted_and_never_escalate(
    value: object,
) -> None:
    # Bidirectional negative: every non-`True` value is OMITTED from the Cedar
    # context entirely (never merely "falsy inside Cedar") — the SAME
    # write.internal action with the SAME extraction_mode stays at tier 1.
    evaluator = CedarEvaluator()
    context: dict[str, object] = {"extraction_mode": "read_flagged"}
    if value is not None:
        context["context_provisional"] = value
    raw = evaluator.evaluate(
        {
            "principal": {"id": "u1"},
            "action": "write.internal.notes.append",
            "context": context,
        }
    )
    assert raw.effect == "allow"
    assert raw.tier == 1


def test_extraction_mode_string_is_admitted() -> None:
    evaluator = CedarEvaluator()
    raw = evaluator.evaluate(
        {
            "principal": {"id": "u1"},
            "action": "write.internal.notes.append",
            "context": {"context_provisional": True, "extraction_mode": "autonomous"},
        }
    )
    # "autonomous" is admitted (it is a string) and its VALUE is what suppresses
    # escalation (YOLO) — a non-"autonomous" string would still escalate.
    assert raw.effect == "allow"
    assert raw.tier == 1


@pytest.mark.parametrize("value", [123, 1.5, True, ["read_flagged"], {"mode": "x"}])
def test_extraction_mode_non_string_values_are_omitted(value: object) -> None:
    # A non-string extraction_mode is OMITTED entirely (never coerced to a
    # string, never treated as a match for "autonomous") — absence fails
    # TOWARD escalation, exactly like a missing key.
    evaluator = CedarEvaluator()
    raw = evaluator.evaluate(
        {
            "principal": {"id": "u1"},
            "action": "write.internal.notes.append",
            "context": {"context_provisional": True, "extraction_mode": value},
        }
    )
    assert raw.effect == "require-approval"
    assert raw.tier == 2


# --- laziness / non-export ---------------------------------------------------


def test_cedar_evaluator_not_exported() -> None:
    import ne.govern

    assert "CedarEvaluator" not in ne.govern.__all__
    assert not hasattr(ne.govern, "CedarEvaluator")


# --- tier-3/legal red-team never-allow sweep --------------------------------


@pytest.mark.parametrize("action", ["finance.wire", "legal.filing"])
@pytest.mark.parametrize("declared", [None, 0, 1, 2, 3])
def test_tier3_never_allows_c12(action: str, declared: int | None) -> None:
    evaluator = CedarEvaluator()
    context: dict[str, object] = {} if declared is None else {"tier": declared}
    raw = evaluator.evaluate({"principal": {"id": "u1"}, "action": action, "context": context})
    assert raw.effect == "require-approval"
    assert raw.effect != "allow"
    assert raw.tier == 3
