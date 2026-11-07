# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE governance-parity swap suite: cedarpy vs OPA, zero-weakening proof.

Dual-backend battery over the full (expanded) vector battery:

  * cedar-vs-shared-oracle FULL-document equality (effect/tier/reason/
    obligations/policy_version) — ALWAYS executes, no skip.
  * cedar-vs-live ``opa eval`` full-document equality — executes wherever the
    pinned ``opa`` binary is on PATH (CI installs opa 1.18.2, so this runs on
    every PR; LIVE-PARKABLE only on a build host without the binary).
  * live-opa-vs-oracle consistency — proves the shared oracle itself.
  * dual-backend GovernedPdp/CedarGovernedPdp decision AND audit-event parity.

ANY divergence anywhere in this module is a test failure — never a weaker
approximation.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from ne.contracts.governance import PolicyDecision, PolicyRequest
from ne.contracts.types import Principal
from ne.govern.cedar.evaluator import CedarEvaluator
from ne.govern.cedar.pdp import CedarGovernedPdp
from ne.schema import AuditEvent

from .conftest import RecordingAuditSink, load_vectors, make_pdp, oracle_doc_for

_OPA = shutil.which("opa")
_POLICIES = str(Path(__file__).parents[1] / "policies")
_VECTORS = load_vectors()
_IDS = [v["name"] for v in _VECTORS]

_LIVE_OPA_SKIP = pytest.mark.skipif(
    _OPA is None,
    reason="LIVE-PARKABLE: opa binary not on PATH — CI runs this on every PR "
    "(install OPA 1.18.2 locally to execute it here too).",
)


def request_from(vec_input: dict[str, Any]) -> PolicyRequest:
    return PolicyRequest(
        principal=Principal(id=vec_input["principal"]["id"]),
        action=vec_input["action"],
        resource=vec_input["resource"],
        context=vec_input.get("context", {}),
    )


def _cedar_doc(vector: dict[str, Any]) -> dict[str, Any]:
    raw = CedarEvaluator().evaluate(vector["input"])
    return {
        "effect": raw.effect,
        "tier": raw.tier,
        "reason": raw.reason,
        "obligations": raw.obligations,
        "approval_window_seconds": raw.approval_window_seconds,
        "approval_window_declared": raw.approval_window_declared,
        "standing_approval_seconds": raw.standing_approval_seconds,
        "policy_version": raw.policy_version,
    }


def _opa_eval_doc(vector: dict[str, Any]) -> dict[str, Any]:
    assert _OPA is not None
    result = subprocess.run(
        [
            _OPA,
            "eval",
            "-d",
            _POLICIES,
            "--stdin-input",
            "--format=json",
            "data.ne.govern.decision",
        ],
        input=json.dumps(vector["input"]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)["result"][0]["expressions"][0]["value"]
    return {
        "effect": value["effect"],
        "tier": value["tier"],
        "reason": value["reason"],
        "obligations": value["obligations"],
        "approval_window_seconds": value["approval_window_seconds"],
        "approval_window_declared": value["approval_window_declared"],
        "standing_approval_seconds": value["standing_approval_seconds"],
        "policy_version": value["policy_version"],
    }


# --- (1) cedar vs shared oracle: ALWAYS executes ----------------------------


@pytest.mark.parametrize("vector", _VECTORS, ids=_IDS)
def test_cedar_equals_oracle_full_document(vector: dict[str, Any]) -> None:
    assert _cedar_doc(vector) == oracle_doc_for(vector)


# --- (2) cedar vs live opa eval: skipif no opa binary -----------------------


@_LIVE_OPA_SKIP
@pytest.mark.parametrize("vector", _VECTORS, ids=_IDS)
def test_cedar_equals_live_opa_full_document(vector: dict[str, Any]) -> None:
    assert _opa_eval_doc(vector) == _cedar_doc(vector)


# --- (3) live opa vs oracle: proves the shared oracle itself ----------------


@_LIVE_OPA_SKIP
@pytest.mark.parametrize("vector", _VECTORS, ids=_IDS)
def test_live_opa_equals_oracle_full_document(vector: dict[str, Any]) -> None:
    assert _opa_eval_doc(vector) == oracle_doc_for(vector)


# --- (4) dual-backend GovernedPdp/CedarGovernedPdp decision + audit parity --


@pytest.mark.parametrize("vector", _VECTORS, ids=_IDS)
def test_swap_suite_decision_and_audit_parity(vector: dict[str, Any]) -> None:
    oracle = oracle_doc_for(vector)

    opa_sink = RecordingAuditSink()
    opa_pdp = make_pdp(oracle, opa_sink)
    opa_decision = asyncio.run(opa_pdp.decide(request_from(vector["input"])))

    cedar_sink = RecordingAuditSink()
    cedar_pdp = CedarGovernedPdp(CedarEvaluator(), cedar_sink)
    cedar_decision = asyncio.run(cedar_pdp.decide(request_from(vector["input"])))

    assert isinstance(opa_decision, PolicyDecision)
    assert isinstance(cedar_decision, PolicyDecision)
    assert opa_decision.effect == cedar_decision.effect
    assert opa_decision.tier == cedar_decision.tier
    assert opa_decision.reason == cedar_decision.reason
    assert dict(opa_decision.obligations) == dict(cedar_decision.obligations)
    assert opa_decision.approval_window_seconds == cedar_decision.approval_window_seconds
    # The provenance bit is asserted at the PDP layer too, not only on the
    # documents above: `OpaClient` normalises it (`declared and window is not
    # None`) and the Cedar path has no equivalent line, so a DIVERGENCE
    # introduced at that layer is invisible to the document-level legs.
    #
    # ⚠ ONLY A DIVERGENCE, THOUGH, AND CLAIMING MORE IS WHAT THIS PARAGRAPH
    # REPLACES. All 36 vectors carry `approval_window_declared: False` — the
    # battery evaluates the policies with no `data.ne.config.approval_windows`
    # document (`_opa_eval_doc` passes only `-d _POLICIES`), so nothing these
    # vectors reach can be declared. Mutation-MEASURED, not reasoned: rewriting
    # `OpaClient`'s `declared and window is not None` to `not (declared and
    # window is not None)` reds all 36 of these; rewriting it to a constant
    # `False` leaves all 36 GREEN. So this line catches an INVERSION and cannot
    # catch a CONSTANT. The constant is caught by
    # `test_opa_client.test_declared_bit_is_parsed_and_defaults_to_not_declared`
    # and, at this same PDP layer, by `test_pdp_layer_parity_on_declared_windows`
    # in section (5) below — added rather than merely documented — which drives
    # the True branch through both PDPs and is the one test in THIS module that
    # the constant reds.
    assert opa_decision.approval_window_declared == cedar_decision.approval_window_declared
    assert opa_decision.policy_version == cedar_decision.policy_version

    assert len(opa_sink.events) == 1
    assert len(cedar_sink.events) == 1
    opa_event = cast(AuditEvent, opa_sink.events[0])
    cedar_event = cast(AuditEvent, cedar_sink.events[0])

    assert opa_event.action == cedar_event.action == "pdp.decide"
    assert opa_event.outcome == cedar_event.outcome
    assert json.loads(opa_event.detail or "{}") == json.loads(cedar_event.detail or "{}")
    assert dict(opa_event.provenance) == dict(cedar_event.provenance)


# --- (5) window-override parity: the two override mechanisms agree ----------
#
# ADR-0030: OPA resolves a deployment's approval-window overrides from
# `data.ne.config.approval_windows`; Cedar has no `data` mechanism, so its
# evaluator takes the SAME mapping as a constructor argument and mirrors
# tiers.rego's most-specific-first resolution in the marshaller. These prove
# the two mechanisms produce the identical decision document when handed the
# identical mapping — the deployment-consistency contract the ADR states.
#
# The PROVENANCE bit is carried through every case on purpose. The 36-vector
# battery above runs with NO data document (`_opa_eval_doc` passes only
# `-d _POLICIES`), so `approval_window_declared` is False in all 36 and those
# vectors cannot distinguish "declared" from "floor" at all. These cases and
# `test_pdp_layer_parity_on_declared_windows` are where the True branches are
# exercised, and the runtime's park-time coherence check refuses on the bit — a
# backend that reported a floor as declared would refuse parks the other
# backend lets through.

_WINDOW_OVERRIDES: dict[str, Any] = {
    "actions": {"write.external.email.send": 600},
    "tiers": {"3": 7200},
}

_WINDOW_CASES = [
    # (name, action, expected window under _WINDOW_OVERRIDES, expected provenance)
    ("exact-action-override", "write.external.email.send", 600, True),
    ("tier-override", "finance.wire.send", 7200, True),
    ("floor-when-nothing-matches", "write.external.post", 14400, False),
    ("allow-still-null", "read.document", None, False),
]


def _override_input(action: str) -> dict[str, Any]:
    return {
        "principal": {"id": "user:ops"},
        "action": action,
        "resource": "r:1",
        "context": {},
    }


@pytest.mark.parametrize(
    ("name", "action", "window", "declared"), _WINDOW_CASES, ids=lambda c: str(c)
)
def test_cedar_override_resolution_matches_the_declared_order(
    name: str, action: str, window: float | None, declared: bool
) -> None:
    raw = CedarEvaluator(approval_windows=_WINDOW_OVERRIDES).evaluate(_override_input(action))
    assert raw.approval_window_seconds == window
    assert raw.approval_window_declared is declared


@_LIVE_OPA_SKIP
@pytest.mark.parametrize(
    ("name", "action", "window", "declared"), _WINDOW_CASES, ids=lambda c: str(c)
)
def test_live_opa_and_cedar_agree_on_every_override_case(
    name: str, action: str, window: float | None, declared: bool, tmp_path: Path
) -> None:
    data_file = tmp_path / "approval_windows_data.json"
    data_file.write_text(
        json.dumps({"ne": {"config": {"approval_windows": _WINDOW_OVERRIDES}}}),
        encoding="utf-8",
    )
    assert _OPA is not None
    result = subprocess.run(
        [
            _OPA,
            "eval",
            "-d",
            _POLICIES,
            "-d",
            str(data_file),
            "--stdin-input",
            "--format=json",
            "data.ne.govern.decision",
        ],
        input=json.dumps(_override_input(action)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)["result"][0]["expressions"][0]["value"]
    raw = CedarEvaluator(approval_windows=_WINDOW_OVERRIDES).evaluate(_override_input(action))
    assert value["approval_window_seconds"] == raw.approval_window_seconds == window
    assert value["approval_window_declared"] == raw.approval_window_declared == declared


def test_pdp_layer_parity_on_declared_windows() -> None:
    """The PDP-layer provenance parity, driven through the TRUE branch as well.

    ⚠ WHY THIS EXISTS. `test_swap_suite_decision_and_audit_parity` compares
    `approval_window_declared` across the two PDPs on all 36 vectors, but every
    one of those carries False, so that equality is VACUOUS against a constant:
    replacing `OpaClient`'s `declared and window is not None` with `False`
    leaves the whole 36-vector battery green. This case is the same PDP-layer
    comparison on an OVERRIDE deployment, where two of the four
    `_WINDOW_CASES` are declared, so the constant reds here.

    ⛔ IT ADDS NO CLAIM ABOUT THE POLICY, and the document it feeds `OpaClient`
    is Cedar's own answer rather than a hand-written one. That is honest for the
    two fields asserted below and for nothing else:
    `test_live_opa_and_cedar_agree_on_every_override_case` above proves live OPA
    and Cedar agree on `approval_window_seconds` and `approval_window_declared`
    for exactly these four inputs under exactly this mapping, so those two
    fields are what live OPA would return; the rest of the document is carried
    across only to give `OpaClient` a well-formed decision to parse, and nothing
    here asserts anything about it. What is under test is the two PDPs' handling
    of the provenance bit — the layer the 36 vectors cannot reach — and that is
    why this can be hermetic (no `opa` binary) without weakening anything.
    """

    evaluator = CedarEvaluator(approval_windows=_WINDOW_OVERRIDES)
    for _name, action, window, declared in _WINDOW_CASES:
        vec_input = _override_input(action)
        raw = evaluator.evaluate(vec_input)
        opa_document = {
            "effect": raw.effect,
            "tier": raw.tier,
            "reason": raw.reason,
            "obligations": raw.obligations,
            "approval_window_seconds": raw.approval_window_seconds,
            "approval_window_declared": raw.approval_window_declared,
            "standing_approval_seconds": raw.standing_approval_seconds,
            "policy_version": raw.policy_version,
        }
        opa_decision = asyncio.run(
            make_pdp(opa_document, RecordingAuditSink()).decide(request_from(vec_input))
        )
        cedar_decision = asyncio.run(
            CedarGovernedPdp(evaluator, RecordingAuditSink()).decide(request_from(vec_input))
        )
        assert opa_decision.approval_window_seconds == window, action
        assert cedar_decision.approval_window_seconds == window, action
        assert opa_decision.approval_window_declared is declared, action
        assert cedar_decision.approval_window_declared is declared, action


# --- (6) standing-lifetime override parity: the SECOND clock agrees too -----
#
# F1 (bundle 1.5.0) adds `standing_approval_seconds` beside the window: how
# long an approver's DECISION outlives the attempt it was asked for. It is
# resolved from a SEPARATE OPA data document (`data.ne.config.standing_
# approvals`) and a SEPARATE Cedar constructor argument, so it needs its own
# parity proof — a mirror that silently read the window's document would pass
# every test above while giving the two clocks one value.

_STANDING_OVERRIDES: dict[str, Any] = {
    "actions": {"write.external.email.send": 900},
    "tiers": {"3": 3600},
}

_STANDING_CASES = [
    # (name, action, expected standing lifetime under _STANDING_OVERRIDES)
    ("exact-action-override", "write.external.email.send", 900),
    ("tier-override", "finance.wire.send", 3600),
    ("floor-when-nothing-matches", "write.external.post", 604800),
    ("allow-still-null", "read.document", None),
]


@pytest.mark.parametrize(("name", "action", "lifetime"), _STANDING_CASES, ids=lambda c: str(c))
def test_cedar_standing_override_resolution_matches_the_declared_order(
    name: str, action: str, lifetime: float | None
) -> None:
    raw = CedarEvaluator(standing_approvals=_STANDING_OVERRIDES).evaluate(_override_input(action))
    assert raw.standing_approval_seconds == lifetime


def test_the_two_clocks_are_configured_independently() -> None:
    """Overriding the WINDOW must not move the STANDING lifetime, or vice versa.

    They answer different questions (how long a human has to ANSWER vs. how
    long that answer OUTLIVES the attempt) and a deployment must be able to say
    "decide within the hour, but if the owner's session died, the decision is
    still good for a week".
    """
    raw = CedarEvaluator(
        approval_windows={"actions": {"write.external.email.send": 600}},
        standing_approvals={"actions": {"write.external.email.send": 900}},
    ).evaluate(_override_input("write.external.email.send"))
    assert raw.approval_window_seconds == 600
    assert raw.standing_approval_seconds == 900


@_LIVE_OPA_SKIP
@pytest.mark.parametrize(("name", "action", "lifetime"), _STANDING_CASES, ids=lambda c: str(c))
def test_live_opa_and_cedar_agree_on_every_standing_override_case(
    name: str, action: str, lifetime: float | None, tmp_path: Path
) -> None:
    data_file = tmp_path / "standing_approvals_data.json"
    data_file.write_text(
        json.dumps({"ne": {"config": {"standing_approvals": _STANDING_OVERRIDES}}}),
        encoding="utf-8",
    )
    assert _OPA is not None
    result = subprocess.run(
        [
            _OPA,
            "eval",
            "-d",
            _POLICIES,
            "-d",
            str(data_file),
            "--stdin-input",
            "--format=json",
            "data.ne.govern.decision",
        ],
        input=json.dumps(_override_input(action)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)["result"][0]["expressions"][0]["value"]
    raw = CedarEvaluator(standing_approvals=_STANDING_OVERRIDES).evaluate(_override_input(action))
    assert value["standing_approval_seconds"] == raw.standing_approval_seconds == lifetime
