# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE PERMANENT C6 NO-BYPASS GUARDIAN (build-orchestration plan §2.1).

This file is a DURABLE, UN-SKIPPABLE structural test of the C6 invariant: every
agent tool call passes the PDP AND emits an AuditEvent, and NO public code path
executes a tool without the middleware. If a future change opens a bypass — a
handler reachable off the executor, a sink-less constructor, a provider-SDK
import — one of these tests fails. Do not weaken them.

MARKER POLICY (self-checked by :func:`test_guardian_has_no_skip_markers`, an AST
scan, not a substring search): ``pytest.mark.skip`` / ``pytest.mark.skipif`` are
FORBIDDEN anywhere in this file, with no exception. ``pytest.mark.xfail`` is
forbidden EXCEPT on a ``test_declared_residual_*`` function, and even there ONLY
when it carries an explicit ``strict=False`` keyword. Those two functions are
DECLARED RESIDUAL PROBES (design §3-V3 / T2X-3 discipline): they assert the
POSITIVE claim that a structural scan catches a planted offender, which it does
not (yet) — the XFAIL keeps that gap visible in every run, and an XPASS (should
a future scan hardening catch the offender) SIGNALS closure instead of blocking
the suite. Reporting a residual is a visibility job; it must never gain the
power to enforce or to silently suppress.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

import ne.runtime
from ne.contracts.runtime import SubAgentSpec, ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.govern import create_guardrail_engine
from ne.govern.config import (
    ArgSpec,
    GovernSettings,
    GuardrailSettings,
    RateLimitRule,
    RateLimitSettings,
    RecipientSettings,
    ToolArgKind,
    ToolArgPolicy,
)
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.dispatch import AgentDefinition, GovernedSubAgentDispatcher
from ne.runtime.errors import ToolDeniedError, ToolNotRegisteredError, ToolsetViolationError
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.model import GatewayModel
from ne.runtime.processes import ProcessRegistry
from ne.runtime.runner import build_governed_tool
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.runtime.tools import RuntimeToolRegistry, ToolBinding
from ne.runtime.toolset import ToolsetScopedExecutor
from ne.schema import AuditOutcome

from .conftest import (
    ALLOWED_RECIPIENT,
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    UnusedGate,
    make_executor,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
    text_completion,
    tool_call_completion,
)

_SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "ne" / "runtime"
_PROVIDER_TOKENS = (
    "pydantic_ai.models.openai",
    "pydantic_ai.models.anthropic",
    "pydantic_ai.models.google",
    "pydantic_ai.models.groq",
    "pydantic_ai.models.mistral",
    "pydantic_ai.models.cohere",
    "pydantic_ai.models.bedrock",
    "pydantic_ai.models.huggingface",
)

# A model-emitted argument dict crafted to shadow EVERY carrier/binding-adjacent
# name the tool closure could conceivably use — the internal ToolSpec carrier
# (``_spec`` / ``spec``), the executor, the principal, the tool name, ``kwargs``
# itself, ``self``, and a non-identifier key. With the closure fix (**kwargs the
# ONLY parameter, tool name bound by closure) none of these can rebind anything:
# they arrive as ordinary governed DATA. ``q`` is the one legitimately-declared arg.
_SHADOW_ARGS: dict[str, Any] = {
    "q": "probe",
    "_spec": "shadow",
    "spec": "shadow",
    "executor": "shadow",
    "principal": "shadow",
    "tool_name": "shadow",
    "kwargs": {"n": 1},
    "self": "shadow",
    "bad-key": "shadow",
}


class _ExecuteSpy(GovernedToolExecutor):
    """A GovernedToolExecutor that records every (tool, args) reaching ``execute``.

    Proves the model-emitted call actually reached the governed chokepoint with the
    INTACT internal tool name (not attacker-supplied data) — i.e. the crafted keys
    never rebound the carrier. It still delegates to the real ``execute`` so the
    full guardrail/PDP/audit governance runs unchanged.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        self.seen.append((tool, dict(args)))
        return await super().execute(tool, args, principal=principal)


def test_executor_requires_all_governance_args() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    bindings = standard_bindings(log)
    from .conftest import make_guardrails, make_pdp

    full = {
        "pdp": make_pdp(sink),
        "guardrails": make_guardrails(sink),
        "approval_gate": NamedApproverGate(),
        "sink": sink,
    }
    # Every governance kwarg is required (no default): omitting any => TypeError.
    # (Python's own keyword-only-argument enforcement — the message names the
    # single omitted kwarg.)
    for omit in full:
        kwargs = {k: v for k, v in full.items() if k != omit}
        with pytest.raises(TypeError, match="missing 1 required keyword-only argument"):
            GovernedToolExecutor(bindings, **kwargs)  # type: ignore[arg-type]
    # A None value for any governance arg is rejected (no sink-less/pdp-less path).
    # (``GovernedToolExecutor.__init__``'s own explicit guard at middleware.py:153-160.)
    for none_key in full:
        kwargs = dict(full)
        kwargs[none_key] = None  # type: ignore[assignment]
        with pytest.raises((TypeError, ValueError), match="requires a non-None"):
            GovernedToolExecutor(bindings, **kwargs)  # type: ignore[arg-type]


def test_alias_constructed_executor_still_enforces_required_args() -> None:
    """CONTROL (not a residual): the design names 'alias-constructed executor' as
    a candidate evasion — constructing the SAME class through a module alias or
    a re-exported name, hoping the required-kwarg enforcement lives in a
    call-site check rather than ``__init__`` itself. It does not: the guard is
    on the constructor, so no import alias can route around it.

    ``ne.runtime.middleware.GovernedToolExecutor`` and the re-exported
    ``ne.runtime.GovernedToolExecutor`` are the SAME object through two names
    (the re-export is an identity, not a copy) — this loop therefore exercises
    ONE constructor twice, not two independent implementations. That identity
    IS the non-evasion guarantee this test states: there is no second,
    differently-guarded ``__init__`` reachable through the alternate name.
    """
    import ne.runtime.middleware as _mw_alias
    from ne.runtime import GovernedToolExecutor as _reexported

    assert _mw_alias.GovernedToolExecutor is _reexported, (
        "the re-export must be the identical object, not a wrapper/copy — "
        "this is what makes the loop below a single-object proof"
    )

    log = ToolLog()
    sink = RecordingAuditSink()
    bindings = standard_bindings(log)
    for aliased_cls in (_mw_alias.GovernedToolExecutor, _reexported):
        with pytest.raises(TypeError, match="missing 3 required keyword-only arguments"):
            aliased_cls(bindings, pdp=make_pdp(sink))  # type: ignore[call-arg]
    # Positive control: the SAME alias, fully governed, constructs fine.
    ex = _mw_alias.GovernedToolExecutor(
        bindings,
        pdp=make_pdp(sink),
        guardrails=make_guardrails(sink),
        approval_gate=NamedApproverGate(),
        sink=sink,
    )
    assert isinstance(ex, GovernedToolExecutor)


def test_factory_requires_sink_and_gate() -> None:
    from ne.runtime import create_tool_executor

    with pytest.raises(TypeError):
        create_tool_executor([])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        create_tool_executor([], sink=RecordingAuditSink())  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        create_tool_executor([], approval_gate=NamedApproverGate())  # type: ignore[call-arg]


def test_execute_is_the_only_public_callable() -> None:
    public_callables = {
        name
        for name, member in inspect.getmembers(GovernedToolExecutor)
        if not name.startswith("_")
        and (inspect.isfunction(member) or inspect.iscoroutinefunction(member))
    }
    assert public_callables == {"execute"}
    # The only public non-callable is the specs-only view (never a handler).
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=NamedApproverGate())
    specs = executor.tool_specs
    from ne.contracts.runtime import ToolSpec

    assert all(isinstance(s, ToolSpec) for s in specs)
    assert all(not hasattr(s, "handler") for s in specs)


def test_no_public_handler_surface() -> None:
    # The registry stores/returns ToolSpec only; the exported surface exposes no
    # object that can reach a ToolBinding.handler.
    registry = RuntimeToolRegistry()
    assert not hasattr(registry, "handler")
    for name in ne.runtime.__all__:
        obj = getattr(ne.runtime, name)
        # No exported symbol IS a ToolBinding (the only handler holder besides the executor).
        assert not isinstance(obj, ToolBinding)


# --- DECLARED RESIDUALS (NE-VG-2, design §3-V3 / T2X-3 discipline) ---------------
#
# Both structural scans below are ENUMERATION / literal-substring checks (V3):
# sound against every form observed so far, but — by the taxonomy's own
# definition — not decidable-exhaustive against every future form. Declaring
# the residual explicitly (rather than letting the scan's clean pass imply
# totality) is the T2X-3 discipline this design mandates.
#
# Each residual below is an XFAIL(strict=False) RESIDUAL PROBE, not a
# "PLANTED-RED" one (the original 4d873be wording was wrong — V10 in the SPEC's
# vacuous-guard-shape catalogue, commit efdbc47: these asserted the scan does
# NOT fire, so they were planted-GREEN, and would have gone RED the moment
# someone HARDENED the scanner — "a ratchet that fires on improvement"). The
# fixed shape asserts the POSITIVE claim ("the scan DOES catch this offender")
# under ``@pytest.mark.xfail(strict=False)``: it XFAILs today (the gap is
# real and stays visible in every test run, never silently green), and it
# XPASSes — a visible SIGNAL, never a suite failure — the day the scan is
# hardened enough to catch the planted offender. Reporting a residual must
# never be able to BLOCK a merge; only enforcing an invariant may do that.
# ``test_guardian_has_no_skip_markers`` below enforces, by AST policy, that
# ``xfail`` appears ONLY on a ``test_declared_residual_*`` function and ONLY
# with an explicit ``strict=False`` — so this discipline cannot silently
# regress into a real skip/xfail-as-suppression elsewhere in the file.
#
# RESIDUAL 1 — ``_PROVIDER_TOKENS`` (8-path enumeration of ``pydantic_ai.models.*``
# provider wrappers). A provider wrapper path NOT in this tuple (a new
# pydantic-ai backend added upstream, or one deliberately misspelled/obfuscated
# to evade literal-substring match, e.g. built via ``importlib.import_module``
# string concatenation) is INVISIBLE to this scan.
#   Backstop: PARTIAL. The repo's ``.importlinter`` ``c3-provider-sdk-chokepoint``
#   contract independently forbids ``ne.runtime`` from importing the RAW SDK
#   packages (litellm/openai/anthropic/mistralai/cohere/groq/ollama/together/
#   replicate) by import-graph analysis (not substring, so it survives string
#   concatenation) — but that list is ALSO an enumeration, and its raw-package
#   names do not 1:1 cover every ``pydantic_ai.models.<x>`` wrapper path
#   (verified by direct read of ``.importlinter`` this pass: e.g. a
#   pydantic-ai backend for a provider outside that 9-name list — no such
#   backstop exists for it). A NEW provider unlisted in BOTH places is a live
#   residual until one of the two enumerations is extended (ratchet rule,
#   design §5.1).
#
# RESIDUAL 2 — ``test_source_scan_handler_call_sites`` (literal substring
# ``".handler("`` outside ``middleware.py``). A call site that reaches the SAME
# handler without that literal substring — ``h = binding.handler; h(args)``,
# ``getattr(binding, "handler")(args)``, or an attribute-chain split across
# lines — evades the grep while reaching the exact bypass this test exists to
# forbid.
#   Backstop: NONE currently (no AST-based call-site scan exists in this repo
#   for handler invocation; filed for NE-VG-1's ``ne check test-shapes`` lint,
#   which is the natural home for an AST-based successor — out of this unit's
#   scope per the SPEC row).


def _handler_call_site_offenders(paths: list[Path]) -> list[str]:
    """Extracted check core (testable against synthetic text, not just real files)."""
    offenders: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if ".handler(" in text and path.name != "middleware.py":
            offenders.append(path.name)
    return offenders


def test_source_scan_handler_call_sites() -> None:
    # A handler is invoked ('.handler(') in exactly one module: middleware.py.
    offenders = _handler_call_site_offenders(list(_SRC_DIR.glob("*.py")))
    assert offenders == [], f"handler call sites outside middleware.py: {offenders}"


def test_handler_call_site_scan_positive_control() -> None:
    """P1-1 (Argus): every existing assertion on ``_handler_call_site_offenders``
    is ``== []`` — neutering the helper to ``return []`` left the whole file
    ``20 passed`` because nothing pins what the scan DOES catch. This test does:
    a real offending call site in a real (synthetic) file IS returned.

    Mutation proof (recorded, not left in the diff): neutering
    ``_handler_call_site_offenders`` to ``return []`` turns THIS test RED while
    ``test_source_scan_handler_call_sites`` (an ``== []`` scan of the real,
    clean tree) stays GREEN beside it — that pairing is the P1-1 demonstration.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        sneaky = Path(tmp) / "sneaky.py"
        sneaky.write_text("result = binding.handler(args)\n", encoding="utf-8")
        assert _handler_call_site_offenders([sneaky]) == ["sneaky.py"]

        # The middleware.py exclusion is DELIBERATE (that's the one legitimate
        # call site) — same content under that filename is NOT flagged, and
        # that exclusion is itself now pinned rather than merely relied upon.
        middleware_like = Path(tmp) / "middleware.py"
        middleware_like.write_text("result = binding.handler(args)\n", encoding="utf-8")
        assert _handler_call_site_offenders([middleware_like]) == []

        # A clean file (no handler call site at all) is not flagged.
        clean = Path(tmp) / "clean.py"
        clean.write_text("result = 1 + 1\n", encoding="utf-8")
        assert _handler_call_site_offenders([clean]) == []


@pytest.mark.xfail(
    strict=False,
    reason=(
        "RESIDUAL 2 open — XPASS signals closure; promote by deleting this "
        "marker and keeping the positive assertion"
    ),
)
def test_declared_residual_alias_constructed_handler_call_evades_the_scan() -> None:
    """XFAIL residual probe for RESIDUAL 2. A synthetic offending module (never
    written to ``_SRC_DIR`` — this is a controlled in-memory fixture, not a
    real bypass) reaches ``binding.handler(...)`` via a local alias. This
    asserts the POSITIVE claim that the scan CATCHES it — which it does not
    today (the literal-substring scan is blind to the alias form), so this
    test XFAILs on the current tree. That is the declared residual: visible in
    every run via the XFAIL line, never silently green. If a future scan
    hardening makes this XPASS, that is the closure signal — delete the
    marker and keep the assertion as an ordinary enforcing test."""
    import tempfile

    offending_source = (
        "aliased = binding.handler\nresult = aliased(args)  # local-alias call site\n"
    )
    assert ".handler(" not in offending_source, "fixture must actually evade the substring"
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "sneaky_bypass.py"
        fake.write_text(offending_source, encoding="utf-8")
        offenders = _handler_call_site_offenders([fake])
    assert offenders != [], "the scan should catch the aliased handler call site"


def _provider_token_violations(text: str, tokens: tuple[str, ...]) -> list[str]:
    """Extracted check core (testable against synthetic text)."""
    return [token for token in tokens if token in text]


def test_source_scan_no_provider_model_imports() -> None:
    for path in _SRC_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        violations = _provider_token_violations(text, _PROVIDER_TOKENS)
        assert violations == [], f"{path.name} imports provider model {violations} (C3 violation)"


def test_provider_token_scan_positive_control() -> None:
    """P1-1 (Argus): every existing assertion on ``_provider_token_violations``
    is ``== []`` — neutering the helper to ``return []`` left the whole file
    ``20 passed`` because nothing pins what the scan DOES catch. This test
    does: a listed provider token IS returned.

    Mutation proof (recorded, not left in the diff): neutering
    ``_provider_token_violations`` to ``return []`` turns THIS test RED while
    ``test_source_scan_no_provider_model_imports`` (an ``== []`` scan of the
    real, clean tree) stays GREEN beside it — that pairing is the P1-1
    demonstration.
    """
    assert _provider_token_violations(
        "from pydantic_ai.models.openai import OpenAIModel\n", _PROVIDER_TOKENS
    ) == ["pydantic_ai.models.openai"]

    # Two listed tokens are BOTH returned, in tuple order (not text-appearance
    # order — the text mentions anthropic first, openai second).
    two_token_text = (
        "from pydantic_ai.models.anthropic import AnthropicModel\n"
        "from pydantic_ai.models.openai import OpenAIModel\n"
    )
    assert _provider_token_violations(two_token_text, _PROVIDER_TOKENS) == [
        "pydantic_ai.models.openai",
        "pydantic_ai.models.anthropic",
    ]

    # A clean text (no provider token substring) returns nothing.
    assert _provider_token_violations("import httpx\n", _PROVIDER_TOKENS) == []


@pytest.mark.xfail(
    strict=False,
    reason=(
        "RESIDUAL 1 open — XPASS signals closure; promote by deleting this "
        "marker and keeping the positive assertion"
    ),
)
def test_declared_residual_unlisted_provider_token_evades_the_scan() -> None:
    """XFAIL residual probe for RESIDUAL 1. A synthetic import line for a
    pydantic-ai provider backend NOT in ``_PROVIDER_TOKENS`` (picked to be
    structurally identical to the eight real entries, so this is not a
    strawman) asserts the POSITIVE claim that the scan CATCHES it — which it
    does not today, by construction of an enumeration (see RESIDUAL 1's
    backstop note above), so this test XFAILs on the current tree. If
    ``_PROVIDER_TOKENS`` (or its enumeration-independent backstop) is
    extended to catch this, the test XPASSes — promote by deleting the
    marker."""
    unlisted_provider = "pydantic_ai.models.fireworks"
    assert unlisted_provider not in _PROVIDER_TOKENS, "fixture must actually be unlisted"
    offending_source = f"from {unlisted_provider} import FireworksModel\n"
    violations = _provider_token_violations(offending_source, _PROVIDER_TOKENS)
    assert violations != [], "the scan should catch the unlisted provider token"


def test_every_executed_call_has_a_pdp_decision() -> None:
    """Drive allow + approval + deny + guardrail-block paths through one executor;
    the count of handler invocations equals the count of allow/approved pdp.decide
    events, and every executed call was preceded by a pdp.decide event."""
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = NamedApproverGate("alice")
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=gate,
        pdp_overrides={
            "write.internal.notes.append": {
                "effect": "deny",
                "tier": 1,
                "reason": "denied for test",
                "obligations": {},
                "policy_version": "1.0.0",
            }
        },
    )
    principal = make_principal()

    async def scenario() -> None:
        # allow (tier 0)
        await executor.execute("read.docs.search", {"q": "x"}, principal=principal)
        # approved (tier 2)
        await executor.execute(
            "write.external.email.send",
            {"to": ALLOWED_RECIPIENT, "body": "hi"},
            principal=principal,
        )
        # deny (tier 1, forced)
        with pytest.raises(ToolDeniedError):
            await executor.execute(
                "write.internal.notes.append", {"note": "x"}, principal=principal
            )
        # guardrail block (never reaches the PDP)
        from ne.govern.errors import GuardrailViolation

        with pytest.raises(GuardrailViolation):
            await executor.execute(
                "write.external.email.send",
                {"to": "attacker@evil.example", "body": "x"},
                principal=principal,
            )

    asyncio.run(scenario())

    handler_calls = len(log.calls)
    executes = [e for e in sink.events if getattr(e, "action", "") == "tool.execute"]
    execute_successes = [e for e in executes if e.outcome == AuditOutcome.success.value]
    # Two handlers ran (allow + approved); each has a tool.execute[success] event,
    # and each was preceded by a pdp.decide (the deny + block never ran a handler).
    assert handler_calls == 2
    assert len(execute_successes) == 2
    pdp_events = [e for e in sink.events if getattr(e, "action", "") == "pdp.decide"]
    # allow + approved + deny each produced a pdp.decide; the guardrail block did not.
    assert len(pdp_events) == 3


def test_unknown_tool_call_is_governed_and_never_executes() -> None:
    """Executor-level: an unregistered tool name is audited (tool.unknown), guardrail-
    blocked, PDP-denied, and terminally refused — no handler ever runs."""
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())

    with pytest.raises(ToolNotRegisteredError):
        asyncio.run(executor.execute("nope.unknown", {"x": "y"}, principal=make_principal()))

    assert log.calls == []
    unknown = [e for e in sink.events if getattr(e, "action", "") == "tool.unknown"]
    blocks = [e for e in sink.events if getattr(e, "action", "") == "guardrail.block"]
    pdp = [e for e in sink.events if getattr(e, "action", "") == "pdp.decide"]
    executes = [e for e in sink.events if getattr(e, "action", "") == "tool.execute"]
    assert len(unknown) == 1
    assert unknown[0].outcome == AuditOutcome.denied.value
    assert len(blocks) == 1
    assert len(pdp) == 1
    assert pdp[0].outcome == AuditOutcome.denied.value
    assert pdp[0].resource == "tool:nope.unknown"
    assert executes == []


def test_unknown_tool_call_through_agent_loop_is_governed() -> None:
    """Full-loop: a model-EMITTED unregistered tool call is PDP'd + audited and never
    executes; the run still recovers to the scripted final answer."""
    log = ToolLog()
    sink = RecordingAuditSink()
    engine = sa.create_engine("sqlite://")
    try:
        ensure_runtime_schema(engine)
        store = SqlAlchemySessionStore(engine)
        executor = make_executor(
            standard_bindings(log), sink, approval_gate=NamedApproverGate("alice")
        )
        gateway = ScriptedGateway(
            [tool_call_completion("totally.unregistered", {}), text_completion("recovered")]
        )
        session = DepartmentAgentSession(
            session_id=asyncio.run(
                store.create_conversation("assistant", "eng", "user:ada")
            ).conversation_id,
            agent_name="assistant",
            department="eng",
            owner_principal_id="user:ada",
            store=store,
            executor=executor,
            gateway=gateway,
            settings=RuntimeSettings(),
        )

        result = asyncio.run(session.run("go", principal=make_principal(department="eng")))

        assert result.output == "recovered"
        assert log.names() == []
        unknown = [
            e
            for e in sink.events
            if getattr(e, "action", "") == "tool.unknown"
            and e.resource == "tool:totally.unregistered"
        ]
        pdp = [
            e
            for e in sink.events
            if getattr(e, "action", "") == "pdp.decide"
            and e.resource == "tool:totally.unregistered"
        ]
        assert unknown
        assert pdp and pdp[0].outcome == AuditOutcome.denied.value
        assert all(getattr(e, "action", "") != "tool.execute" for e in sink.events)
    finally:
        engine.dispose()


def test_gateway_model_has_no_hookless_constructor() -> None:
    """Structural C6: GatewayModel cannot be built without on_unknown_tool."""
    with pytest.raises(TypeError):
        GatewayModel(ScriptedGateway([]), RuntimeSettings())  # type: ignore[call-arg]


def test_known_tool_spec_shadow_args_are_governed_not_crash() -> None:
    """Full loop, KNOWN tool: a model-emitted call whose argument keys are crafted to
    shadow the ToolSpec carrier (``_spec`` et al.) must NOT crash the run pre-governance.

    On pre-fix code the ``_spec: ToolSpec = spec`` default parameter is rebound by the
    model's ``_spec`` key to the string ``"shadow"``, and ``_spec.name`` raises
    AttributeError BEFORE ``executor.execute`` — a governance bypass (DoS). With the
    closure fix the crafted keys arrive as ordinary DATA: execute() IS reached with the
    intact internal tool name, and the undeclared keys produce a normal AUDITED
    guardrail block (rule ``toolargs.unknown-arg``), not a crash and not a handler run.
    """
    log = ToolLog()
    sink = RecordingAuditSink()
    engine = sa.create_engine("sqlite://")
    try:
        ensure_runtime_schema(engine)
        store = SqlAlchemySessionStore(engine)
        spy = _ExecuteSpy(
            standard_bindings(log),
            pdp=make_pdp(sink),
            guardrails=make_guardrails(sink),
            approval_gate=NamedApproverGate("alice"),
            sink=sink,
        )
        gateway = ScriptedGateway(
            [
                tool_call_completion("read.docs.search", dict(_SHADOW_ARGS)),
                text_completion("recovered"),
            ]
        )
        session = DepartmentAgentSession(
            session_id=asyncio.run(
                store.create_conversation("assistant", "eng", "user:ada")
            ).conversation_id,
            agent_name="assistant",
            department="eng",
            owner_principal_id="user:ada",
            store=store,
            executor=spy,
            gateway=gateway,
            settings=RuntimeSettings(),
        )

        result = asyncio.run(session.run("go", principal=make_principal(department="eng")))

        # (1) No crash — the run recovers to the scripted final answer.
        assert result.output == "recovered"
        # (2) execute() WAS reached with the INTACT internal name and every adversarial
        # key present as an ordinary arg (the carrier was never rebound).
        assert spy.seen == [("read.docs.search", dict(_SHADOW_ARGS))]
        # (3) A normal AUDITED governance block: guardrails run first, so the undeclared
        # keys yield exactly one guardrail.block (rule toolargs.unknown-arg) and the call
        # never reaches the PDP or a tool.execute.
        blocks = [e for e in sink.events if getattr(e, "action", "") == "guardrail.block"]
        assert len(blocks) == 1
        assert "toolargs.unknown-arg" in json.loads(blocks[0].detail)["rules"]
        assert sink.with_action("pdp.decide") == []
        assert sink.with_action("tool.execute") == []
        # (4) The handler never ran — blocked, not bypassed.
        assert log.calls == []
    finally:
        engine.dispose()


def test_declared_spec_named_field_reaches_handler_as_data() -> None:
    """Allow path: a tool whose schema + arg-policy LEGITIMATELY declare a field named
    ``_spec`` — the crafted-name value flows the full governed path (pdp.decide +
    tool.execute[success]) and reaches the handler as ordinary DATA, proving carrier
    names are never special-cased and the internal ToolSpec stays intact.

    On pre-fix code this ALSO crashes (the ``_spec`` key rebinds the carrier before
    execute), so it is RED until the closure fix lands.
    """
    log = ToolLog()
    sink = RecordingAuditSink()
    engine = sa.create_engine("sqlite://")
    try:
        ensure_runtime_schema(engine)
        store = SqlAlchemySessionStore(engine)
        binding = ToolBinding(
            spec=ToolSpec(
                name="read.probe.echo",
                description="Echo probe.",
                tier=ApprovalTier.TIER_0,
                input_schema={
                    "type": "object",
                    "properties": {"q": {"type": "string"}, "_spec": {"type": "string"}},
                },
            ),
            handler=log.handler("read.probe.echo", "echoed"),
        )
        guardrails = create_guardrail_engine(
            GovernSettings(
                guardrails=GuardrailSettings(
                    recipients=RecipientSettings(allowed_recipients=(ALLOWED_RECIPIENT,)),
                    rate=RateLimitSettings(default=RateLimitRule(max_calls=1000, window_s=3600.0)),
                    tools={
                        "read.probe.echo": ToolArgPolicy(
                            args={
                                "q": ArgSpec(kind=ToolArgKind.text),
                                "_spec": ArgSpec(kind=ToolArgKind.text),
                            }
                        )
                    },
                )
            ),
            sink=sink,
        )
        executor = GovernedToolExecutor(
            [binding],
            pdp=make_pdp(sink),
            guardrails=guardrails,
            approval_gate=UnusedGate(),
            sink=sink,
        )
        gateway = ScriptedGateway(
            [
                tool_call_completion("read.probe.echo", {"q": "x", "_spec": "attacker-data"}),
                text_completion("done"),
            ]
        )
        session = DepartmentAgentSession(
            session_id=asyncio.run(
                store.create_conversation("assistant", "eng", "user:ada")
            ).conversation_id,
            agent_name="assistant",
            department="eng",
            owner_principal_id="user:ada",
            store=store,
            executor=executor,
            gateway=gateway,
            settings=RuntimeSettings(),
        )

        result = asyncio.run(session.run("go", principal=make_principal(department="eng")))

        assert result.output == "done"
        # The `_spec` VALUE reached the handler as plain data via the governed path.
        assert log.calls == [("read.probe.echo", {"q": "x", "_spec": "attacker-data"})]
        pdp = [
            e
            for e in sink.events
            if getattr(e, "action", "") == "pdp.decide" and e.resource == "tool:read.probe.echo"
        ]
        assert pdp
        executes = [e for e in sink.events if getattr(e, "action", "") == "tool.execute"]
        assert len(executes) == 1
        assert executes[0].outcome == AuditOutcome.success.value
        assert sink.with_action("guardrail.block") == []
    finally:
        engine.dispose()


def test_bound_tool_functions_are_structurally_unshadowable() -> None:
    """Durable ratchet (plan §2.1(2)): every parameter of every session-built tool
    function is VAR_KEYWORD — no named parameter and no default carrier — so no
    model-emitted argument key can ever shadow an internal binding. This makes the
    crafted-argument bypass class impossible to silently reintroduce.

    RED on pre-fix code: the ``_spec: ToolSpec = spec`` parameter is POSITIONAL_OR_KEYWORD.
    """
    executor = make_executor(
        standard_bindings(ToolLog()), RecordingAuditSink(), approval_gate=UnusedGate()
    )
    store = SqlAlchemySessionStore(sa.create_engine("sqlite://"))
    session = DepartmentAgentSession(
        session_id="structural",
        agent_name="assistant",
        department="eng",
        owner_principal_id="user:ada",
        store=store,
        executor=executor,
        gateway=ScriptedGateway([]),
        settings=RuntimeSettings(),
    )
    principal = make_principal(department="eng")
    for spec in executor.tool_specs:
        tool = session._build_tool(spec, principal)  # pyright: ignore[reportPrivateUsage]
        params = list(inspect.signature(tool.function).parameters.values())
        assert params, f"tool {spec.name} has no parameters at all"
        assert all(p.kind is inspect.Parameter.VAR_KEYWORD for p in params), (
            f"tool {spec.name} has a shadowable parameter"
        )


def test_scoped_executor_public_surface_is_execute_only() -> None:
    """NE-034 ratchet: the toolset-gated wrapper exposes ONLY ``execute`` as a public
    callable (plus the specs-only ``tool_specs`` property) — it holds no handler."""
    public_callables = {
        name
        for name, member in inspect.getmembers(ToolsetScopedExecutor)
        if not name.startswith("_")
        and (inspect.isfunction(member) or inspect.iscoroutinefunction(member))
    }
    assert public_callables == {"execute"}

    log = ToolLog()
    sink = RecordingAuditSink()
    pdp = make_pdp(sink)
    inner = GovernedToolExecutor(
        standard_bindings(log),
        pdp=pdp,
        guardrails=make_guardrails(sink),
        approval_gate=NamedApproverGate("alice"),
        sink=sink,
    )
    scoped = ToolsetScopedExecutor(
        inner, granted=("read.docs.search",), sink=sink, scope="guardian", pdp=pdp
    )
    for spec in scoped.tool_specs:
        assert isinstance(spec, ToolSpec)
        assert not hasattr(spec, "handler")


def test_runner_built_tools_are_structurally_unshadowable() -> None:
    """NE-034 ratchet: every runner-built governed tool is all-VAR_KEYWORD (no named
    parameter and no default carrier can be shadowed by a model-emitted key)."""
    executor = make_executor(
        standard_bindings(ToolLog()), RecordingAuditSink(), approval_gate=UnusedGate()
    )
    principal = make_principal(department="eng")
    for spec in executor.tool_specs:
        tool = build_governed_tool(executor, spec, principal)
        params = list(inspect.signature(tool.function).parameters.values())
        assert params, f"tool {spec.name} has no parameters at all"
        assert all(p.kind is inspect.Parameter.VAR_KEYWORD for p in params), (
            f"tool {spec.name} has a shadowable parameter"
        )


def test_subagent_and_cron_tool_calls_each_have_pdp_decision() -> None:
    """NE-034: a dispatched sub-agent's in-set tool call produces exactly one
    pdp.decide per tool.execute; an out-of-set call is toolset_denied + PDP-recorded
    and never touches the handler log. (NE-169: the dispatch ALSO makes its own
    ROUTE AUTHORITY pdp.decide call for the ``subagent.route`` decision — a
    SEPARATE, independently-governed decision from the tool-call one this test
    is about, so the tool-scoped assertion below filters on the tool's own
    requested_action rather than counting every pdp.decide on the sink.)"""
    definitions = {
        "researcher": AgentDefinition(
            name="researcher", instructions="Research.", toolset=("read.docs.search",)
        )
    }

    def _dispatcher(sink: RecordingAuditSink, log: ToolLog, gateway: ScriptedGateway):
        pdp = make_pdp(sink)
        executor = GovernedToolExecutor(
            standard_bindings(log),
            pdp=pdp,
            guardrails=make_guardrails(sink),
            approval_gate=NamedApproverGate("alice"),
            sink=sink,
        )
        return GovernedSubAgentDispatcher(
            definitions=definitions,
            executor=executor,
            gateway=gateway,
            registry=ProcessRegistry(),
            sink=sink,
            settings=RuntimeSettings(),
            principal=make_principal(),
            pdp=pdp,
        )

    # In-set call: one pdp.decide, one tool.execute[success], handler ran.
    log = ToolLog()
    sink = RecordingAuditSink()
    dispatcher = _dispatcher(
        sink,
        log,
        ScriptedGateway(
            [tool_call_completion("read.docs.search", {"q": "x"}), text_completion("done")]
        ),
    )
    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="t")))
    assert len(sink.with_action("tool.execute")) == 1
    tool_pdp_decisions = [
        e
        for e in sink.with_action("pdp.decide")
        if e.provenance.get("requested_action") == "read.docs.search"
    ]
    assert len(tool_pdp_decisions) == 1
    assert log.names() == ["read.docs.search"]

    # Out-of-set call: toolset_denied + a pdp.decide record, handler untouched.
    log2 = ToolLog()
    sink2 = RecordingAuditSink()
    dispatcher2 = _dispatcher(
        sink2,
        log2,
        ScriptedGateway(
            [
                tool_call_completion("write.internal.notes.append", {"note": "x"}),
                text_completion("recovered"),
            ]
        ),
    )
    asyncio.run(dispatcher2.dispatch(SubAgentSpec(agent="researcher", task="t")))
    assert sink2.with_action("tool.toolset_denied")
    assert sink2.with_action("pdp.decide")
    assert log2.calls == []
    # ToolsetViolationError is a governance block, not a crash — surfaced as recovery.
    assert issubclass(ToolsetViolationError, Exception)


def _decorator_dotted_name(node: ast.AST) -> str | None:
    """Resolve a decorator expression's dotted name, e.g. ``pytest.mark.xfail``
    for both the bare-attribute form (``@pytest.mark.xfail``) and the call form
    (``@pytest.mark.xfail(strict=False)`` — resolved from ``node.func``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _decorator_dotted_name(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _skip_marker_violations(tree: ast.AST) -> list[str]:
    """AST policy core (testable against synthetic source, not just this file):

    * ``pytest.mark.skip`` / ``pytest.mark.skipif`` are ALWAYS violations,
      anywhere in the file.
    * ``pytest.mark.xfail`` is a violation UNLESS the decorated function's name
      starts with ``test_declared_residual_`` AND the decorator is a CALL
      carrying an explicit keyword ``strict=False`` (a Constant ``False``, not
      merely present) — a bare ``@pytest.mark.xfail`` (no call, no
      ``strict=False``) is a violation even on a residual-probe function.
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            call = deco if isinstance(deco, ast.Call) else None
            target = call.func if call is not None else deco
            dotted = _decorator_dotted_name(target)
            if dotted in ("pytest.mark.skip", "pytest.mark.skipif"):
                violations.append(f"{node.name}: {dotted} is forbidden")
            elif dotted == "pytest.mark.xfail":
                if not node.name.startswith("test_declared_residual_"):
                    violations.append(
                        f"{node.name}: {dotted} is only allowed on test_declared_residual_*"
                    )
                    continue
                strict_kw = (
                    None
                    if call is None
                    else next((kw for kw in call.keywords if kw.arg == "strict"), None)
                )
                is_strict_false = (
                    strict_kw is not None
                    and isinstance(strict_kw.value, ast.Constant)
                    and strict_kw.value.value is False
                )
                if not is_strict_false:
                    violations.append(f"{node.name}: {dotted} must carry strict=False")
    return violations


def test_guardian_has_no_skip_markers() -> None:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    assert _skip_marker_violations(tree) == []


def test_skip_marker_policy_controls() -> None:
    """Four synthetic-source controls pinning both directions of the AST
    policy above (not just the real-file scan, which only ever proves ONE
    point on this file's current tree)."""
    # (1) skip / skipif anywhere is ALWAYS a violation.
    skip_src = "import pytest\n\n\n@pytest.mark.skip\ndef test_x():\n    pass\n"
    assert _skip_marker_violations(ast.parse(skip_src)) != []
    skipif_src = "import pytest\n\n\n@pytest.mark.skipif(True)\ndef test_x():\n    pass\n"
    assert _skip_marker_violations(ast.parse(skipif_src)) != []

    # (2) xfail on a function that is NOT a declared-residual probe is a
    # violation, even with strict=False.
    non_residual_xfail = (
        "import pytest\n\n\n@pytest.mark.xfail(strict=False)\ndef test_enforcing():\n    pass\n"
    )
    assert _skip_marker_violations(ast.parse(non_residual_xfail)) != []

    # (3) xfail(strict=False) on a test_declared_residual_* function is CLEAN —
    # this is the one allowed shape.
    allowed = (
        "import pytest\n\n\n@pytest.mark.xfail(strict=False)\n"
        "def test_declared_residual_x():\n    pass\n"
    )
    assert _skip_marker_violations(ast.parse(allowed)) == []

    # (4) bare xfail (no strict=False) on a test_declared_residual_* function
    # IS flagged — the marker alone is not enough, strict=False is mandatory.
    bare_xfail = (
        "import pytest\n\n\n@pytest.mark.xfail\ndef test_declared_residual_x():\n    pass\n"
    )
    assert _skip_marker_violations(ast.parse(bare_xfail)) != []
    strict_true = (
        "import pytest\n\n\n@pytest.mark.xfail(strict=True)\n"
        "def test_declared_residual_x():\n    pass\n"
    )
    assert _skip_marker_violations(ast.parse(strict_true)) != []
