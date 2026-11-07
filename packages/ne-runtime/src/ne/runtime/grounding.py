# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The grounded-agent runtime POST-STEP (NE-GA-G3) — the symmetric mirror of
the NE-VAF-U3 :class:`~ne.runtime.verified_router.VerifiedRouter` PRE-step.

Where the router runs BEFORE the model and can skip it entirely, this module
runs AFTER the model has produced a draft and decides whether that draft may
ever reach the caller. The model reasons freely over this turn's fetched
:class:`~ne.contracts.runtime.VerifiedProvenance`-shaped tool returns; it
cannot emit an unverified fact into the final answer, because
:func:`run_grounding_gate` withholds any draft the deterministic G1 gate
(``ne.govern.grounding``) does not certify as fully grounded.

C3 injection contract: this module imports NOTHING from ``ne.brain_ontology``.
The G1 gate functions (``GroundingCorpus.from_answers``, ``check_grounding``)
are imported directly from ``ne.govern.grounding``, and the tier policy
(``StrictnessTier``/``apply_strictness``/``GateProfile``/``PROFILE_NG1_FROZEN``)
from the sibling ``ne.govern.grounding_policy`` — both ALLOWED lanes
(ne-runtime already depends on ne-govern; the
``.importlinter`` ``runtime-verified-lane`` contract only forbids
``ne.runtime -> ne.brain_ontology``). Everything that WOULD require a
``ne.brain_ontology`` import — the deterministic fallback renderer
(``render_answer_text`` wrapped over the fetched answers) and the sink-bound
guardrail engine built from G2's ``GroundingGuardrail`` — is INJECTED via
:class:`GroundingPolicy`, exactly the way :class:`GroundingPolicy` mirrors
:class:`~ne.runtime.verified_router.VerifiedRouter`'s own injection contract.
Enforced three ways: no import in this file, the existing ``.importlinter``
contract, and an AST guardian test
(``tests/test_grounding_runtime.py::test_grounding_module_imports_no_brain_ontology``).

Marker namespace (H-unforgeable): ``FALLBACK_MARKER`` (``ne-grounding-gate:
fallback``) only ever reaches ``ModelResponse.model_name`` through
:mod:`ne.runtime.agent`'s own return values, for the exact reason the
``ne-verified-router:*`` markers are unforgeable (see
``ne.runtime.verified_router``'s module docstring): a user prompt containing
the literal marker text lands as ``UserPromptPart`` content, never as a
``ModelResponse.model_name``.

DECIDED DEVIATION (per-run recorder construction): :class:`GroundingRecorder`
is constructed FRESH inside :meth:`~ne.runtime.agent.DepartmentAgentSession.run`
for EVERY call, wrapping that turn's (already toolset-scoped) executor — it is
never built once in the factory and held across turns. A factory-held
recorder would be cross-turn shared state: on a session object reused for a
second turn, a PRIOR turn's fetched answers would still be sitting in the
recorder and could ground a draft that fetched NOTHING this turn — violating
this unit's own binding invariant that a prior turn's fetches must never
ground the current one ("one recorder per run() invocation"). The composed stack —
``GroundingRecorder(ToolsetScopedExecutor(GovernedToolExecutor))`` — is
unchanged; only the recorder layer's construction moves from factory-time to
run()-time.

Fail-CLOSED everywhere (H6, design §7.6): an ungrounded draft is WITHHELD
ENTIRELY — it never reaches ``output``, never enters session history, and its
TEXT never leaves this module (only reason CLASSES and COUNTS reach the audit
detail, C12). A FAIL always emits exactly ONE audited ``guardrail.block`` via
the injected, sink-bound engine before rendering the deterministic fallback;
``GuardrailViolation``/``GuardrailEvaluationError`` raised by that ``enforce``
call are SUPPRESSED (they have already been audited) so the fallback still
renders, but ``AuditEmissionError`` (an un-auditable call) is NOT suppressed —
it propagates and kills the run, mirroring
``ne.runtime.verified_router``'s own H6 posture line-for-line. An EMPTY
corpus (zero fetches this turn) means every content token of the draft is
ungrounded, so a turn that never fetched anything always falls back.

G4 — bounded model-assist (design §4): :attr:`GroundingPolicy.assist`, when
injected (``None`` by default, disabling it entirely and reproducing G3
byte-for-byte), adds ONE additional model round-trip AFTER the deterministic
verdict is already known, on EITHER branch. PASS-polish: the assist may
re-word the ALREADY-PASSING draft; FAIL-re-draft: the assist gets the
rendered tables and the FAIL's reason CLASSES only — never the withheld
draft TEXT (C12 extended to the assist boundary). Either way the assist's
own output is re-certified by the SAME :func:`check_grounding` against the
SAME corpus+prompt before it may be emitted — there is no other channel to
``output``: this is the single emit point, and it is the ONLY invariant that
matters here (assist output reaches :class:`~ne.contracts.runtime.
GroundedProvenance`-carrying callers ONLY via a passing gate re-check).
``assist_used=True`` means the assist TEXT WAS EMITTED — a rejected
(re-check-failed) or erroring assist always leaves ``assist_used=False`` and
falls back to the pre-assist baseline (the original passing draft on PASS,
the deterministic table on FAIL): the assist can only ever IMPROVE the
outcome, never degrade it (H6 extended). ``prompt_echo_count`` always
describes the EMITTED text's OWN verdict when that text was gate-checked
(the draft's verdict, or the accepted candidate's re-check verdict); the
never-gate-checked deterministic table keeps the ORIGINAL verdict's count,
preserving G3 parity. Runs AT MOST ONCE per turn (structural: the PASS and
FAIL branches are mutually exclusive and each contains exactly one assist
call).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final

from ne.contracts.governance import AuditSink, PolicyDecisionPoint
from ne.contracts.runtime import GroundedProvenance, ToolSpec, VerifiedProvenance
from ne.contracts.types import Principal
from ne.events import new_audit_event
from ne.govern.errors import GuardrailEvaluationError, GuardrailViolation
from ne.govern.grounding import GroundingCorpus, GroundingFinding, check_grounding
from ne.govern.grounding_policy import (
    PROFILE_NG1_FROZEN,
    GateProfile,
    StrictnessTier,
    apply_strictness,
)
from ne.runtime.correlation import current_run_trace_id
from ne.runtime.errors import RuntimeAuditError
from ne.runtime.runner import ToolExecutorLike
from ne.runtime.verified_router import GuardrailEnforcerLike
from ne.schema import AuditOutcome

__all__ = [
    "FALLBACK_MARKER",
    "GateResult",
    "GroundingPolicy",
    "GroundingRecorder",
    "current_answer_recorder",
    "run_grounding_gate",
]

#: Marker namespace (H-unforgeable — see module docstring): stamped ONLY by
#: ``ne.runtime.agent`` onto the synthetic fallback ``ModelResponse`` it
#: persists on a FAIL turn.
FALLBACK_MARKER: Final = "ne-grounding-gate:fallback"

#: The ACTIVE per-run recorder, published for the duration of a governed
#: ``GroundingRecorder.execute`` call so the verified-read tool handler (running
#: DEEP inside that same await chain, in the SAME task context) can record its
#: structured :class:`VerifiedAnswer` DIRECTLY (§4.2), instead of the recorder
#: re-parsing the model-visible tool-return string. Same-task ContextVar
#: propagation covers the async call chain (the executor stack is plain awaits —
#: no task-group hop; pinned by the contextvar-window test). ``None`` outside any
#: recorder window (e.g. the router/invoke paths bind their own recorder).
_ACTIVE_RECORDER: ContextVar[GroundingRecorder | None] = ContextVar(
    "ne_runtime_active_grounding_recorder", default=None
)


def current_answer_recorder() -> Callable[[Mapping[str, Any]], None] | None:
    """The active recorder's structured-record sink, or ``None`` outside a window.

    The ne-api verified-read binding binds its ``record_answer_provider`` to this
    function (as DATA, never an import into ne.brain_ontology — C3): the tool
    handler calls it, and if a recorder is active, records the structured answer
    mapping via :meth:`GroundingRecorder.record_answer`."""
    recorder = _ACTIVE_RECORDER.get()
    return recorder.record_answer if recorder is not None else None


def _parse_answer_mapping(item: object) -> Mapping[str, Any] | None:
    """The FULL ``VerifiedAnswer`` shape gate a recorded answer must satisfy to
    be treated as a fetched answer (fail closed): a ``Mapping`` with
    ``template_id``/``template_version``/``config_hash``/``columns``/``rows``/
    ``citations``/``truncated``/``args`` of exactly the types G1's
    ``ne.govern.grounding._parse_verified_answer`` demands (including per-row
    length agreement with ``columns``; extra keys — including NE-CTXB-5's
    ``returned_rows``/``total_rows``/``truncation_reason`` envelope — are
    tolerated, matching G1, and NO new key is REQUIRED here).

    This DELIBERATELY mirrors G1's gate — keep the two aligned: everything the
    recorder captures must ALSO be accepted by ``GroundingCorpus.from_answers``
    downstream, so the injected ``render_fallback`` and the
    :class:`~ne.contracts.runtime.VerifiedProvenance` fetch list are only ever
    built from mappings that actually contributed verified facts to the
    corpus. A looser gate here would let a near-shaped mapping (e.g. one
    missing ``columns``/``citations``) be reported as a verified fetch — and
    be handed to the production fallback renderer, which may not tolerate it —
    despite grounding nothing. Stricter-than-G1 is safe (an unrecorded answer
    simply contributes nothing); looser is the bug.
    """
    if not isinstance(item, Mapping):
        return None
    parsed = item
    template_id = parsed.get("template_id")
    template_version = parsed.get("template_version")
    config_hash = parsed.get("config_hash")
    columns = parsed.get("columns")
    rows = parsed.get("rows")
    citations = parsed.get("citations")
    truncated = parsed.get("truncated")
    args = parsed.get("args")
    if not isinstance(template_id, str):
        return None
    if not isinstance(template_version, int) or isinstance(template_version, bool):
        return None
    if not isinstance(config_hash, str):
        return None
    if not isinstance(columns, Sequence) or isinstance(columns, (str, bytes)):
        return None
    if not all(isinstance(col, str) for col in columns):
        return None
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return None
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            return None
        if len(row) != len(columns):
            return None
        if not all(isinstance(cell, str) for cell in row):
            return None
    if not isinstance(citations, Sequence) or isinstance(citations, (str, bytes)):
        return None
    if not all(isinstance(cit, str) for cit in citations):
        return None
    if not isinstance(truncated, bool):
        return None
    if not isinstance(args, Mapping):
        return None
    if not all(isinstance(key, str) for key in args):
        return None
    return parsed


class GroundingRecorder:
    """Per-run capture of this turn's structured verified answers.

    A THIN wrapper around an inner :class:`~ne.runtime.runner.ToolExecutorLike`
    (in production, an already toolset-scoped executor): every ``execute`` call
    is delegated UNCHANGED to the inner executor, and — for the DURATION of that
    delegated call — this recorder is published on the ``_ACTIVE_RECORDER``
    ContextVar so the verified-read tool handler (running deep inside the same
    await chain) can record its structured :class:`VerifiedAnswer` DIRECTLY via
    :meth:`record_answer` / :func:`current_answer_recorder` (§4.2). The recorder
    no longer parses the model-visible tool-return STRING: the return may now be
    the compact rendering, and the structured artifact reaches the recorder via
    the direct API instead. A ``BLOCKED: ...`` return (an out-of-set or
    governance-denied call) never runs the handler, so it records nothing; an
    exception from the inner executor propagates and the ContextVar is reset in
    ``finally`` (a denied/errored call is never captured as a fetch).

    No-new-bypass argument (C6, extends ``ne.runtime.toolset.
    ToolsetScopedExecutor``'s argument verbatim): this class holds ZERO tool
    handlers and ZERO bindings. Its only route to running a tool is
    ``await self._inner.execute(...)`` — the SAME governed pipeline the inner
    executor already runs. There is no code path off this object that
    executes a tool without the full governance stack.

    MUST be constructed FRESH per ``run()`` invocation (see the module
    docstring's DECIDED DEVIATION) — a shared/reused instance would let a
    prior turn's fetches ground the current turn.
    """

    def __init__(self, inner: ToolExecutorLike) -> None:
        self._inner = inner
        self._answers: list[Mapping[str, Any]] = []

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return self._inner.tool_specs

    def record_answer(self, answer: Mapping[str, Any]) -> None:
        """Record ONE structured verified answer (the direct §4.2 channel).

        Gated through the SAME shape check the corpus parser demands
        (:func:`_parse_answer_mapping`) so a captured mapping always contributes
        facts downstream. The caller is TRUSTED first-party code (the verified-read
        handler recording ``VerifiedAnswer.model_dump``), so a shape miss is a LOUD
        bug (``ValueError``), never a silently-ungrounded turn."""
        parsed = _parse_answer_mapping(answer)
        if parsed is None:
            raise ValueError(
                "structured record is not VerifiedAnswer-shaped — the verified-read "
                "handler must record a full VerifiedAnswer.model_dump mapping (§4.2)"
            )
        self._answers.append(parsed)

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        token = _ACTIVE_RECORDER.set(self)
        try:
            return await self._inner.execute(tool, args, principal=principal)
        finally:
            _ACTIVE_RECORDER.reset(token)

    @property
    def answers(self) -> tuple[Mapping[str, Any], ...]:
        """This run's recorded, VerifiedAnswer-shaped answers, in record order."""
        return tuple(self._answers)


@dataclass(frozen=True, slots=True)
class GroundingPolicy:
    """Plain data + INJECTED callables the G3 post-step needs (C3).

    ``render_fallback`` and ``guardrail_engine`` are injected by the
    composition root (G5) — ``render_fallback`` wraps
    ``ne.brain_ontology.verified.answers.render_answer_text`` over the
    fetched answers, and ``guardrail_engine`` is built from G2's
    ``GroundingGuardrail`` — so ne-runtime never imports ``ne.brain_ontology``
    directly or transitively through this module. The G1 gate functions
    (``GroundingCorpus.from_answers``/``check_grounding``) are imported directly
    from ``ne.govern.grounding`` — an allowed lane; ``check_grounding`` is
    always called with ``profile=policy.profile``, so the emitted ``gate``
    string is ``policy.profile.version`` (NOT a hard-coded ``GATE_VERSION``).

    ``sink``/``pdp`` are REQUIRED because
    :class:`~ne.runtime.toolset.ToolsetScopedExecutor`'s constructor demands
    both (it raises ``TypeError`` on either being ``None``) and
    ``create_department_session`` has no other source for them — the
    composition root (G5) builds this policy in the same place it has both.

    ``assist`` (NE-GA-G4, design §4) is INJECTED by the composition root
    (G5) as a plain gateway completion wrapper, task-tagged
    ``grounding-assist``, given NO tool catalog (it is a bare completion, not
    an agent loop). ``None`` (the default) disables the assist entirely —
    ``run_grounding_gate`` then reproduces G3 behavior byte-identically,
    including never calling ``render_fallback`` on a PASS. The call
    convention is KEYWORD-ONLY and differs by branch:

    - PASS-polish: ``await assist(question=prompt, draft=passing_draft,
      tables=rendered_tables)``.
    - FAIL-re-draft: ``await assist(question=prompt, tables=rendered_tables,
      reasons=finding_reason_classes)`` — ``reasons`` is the tuple of
      ``FindingReason`` CLASS strings from the verdict that failed. The
      withheld draft is NEVER passed as a kwarg on this call (structural
      C12): a fabrication in the model's first attempt cannot ride along
      into the re-draft prompt.

    ``strictness`` (NE-HGATE-2, design §2/§3.1) + ``profile`` (design §3.2) are
    DEPLOYMENT config threaded as frozen plain data — NEVER caller/request-
    selectable (design B-P0-1: a per-request tier is a self-service assurance
    downgrade). They reach ``run_grounding_gate``, which decides withhold via
    :func:`ne.govern.grounding_policy.apply_strictness` (NOT ``verdict.passed``)
    and stamps both onto :class:`~ne.contracts.runtime.GroundedProvenance`.
    Their DEFAULTS (``StrictnessTier.STRICT`` + ``PROFILE_NG1_FROZEN``, whose
    ``version`` IS today's ``GATE_VERSION``) reproduce today's gate byte-for-
    byte — a default-constructed ``GroundingPolicy`` makes exactly the same
    withhold decisions, emits the same ``gate`` string, and dumps the same
    provenance (``strictness='strict'`` equals the contract default). Pinned by
    ``tests/test_grounding_runtime.py::test_default_policy_byte_parity_with_explicit_ng1_strict``.
    """

    granted_tools: tuple[str, ...]
    render_fallback: Callable[[Sequence[Mapping[str, Any]]], str]
    guardrail_engine: GuardrailEnforcerLike
    sink: AuditSink
    pdp: PolicyDecisionPoint
    assist: Callable[..., Awaitable[str]] | None = None
    strictness: StrictnessTier = StrictnessTier.STRICT
    profile: GateProfile = PROFILE_NG1_FROZEN


@dataclass(frozen=True, slots=True)
class GateResult:
    """The post-step's outcome: the (possibly-replaced) output text + provenance.

    ``withheld_reasons``/``withheld_count`` (NE-GA-RUN1) carry the ENFORCED
    finding CLASS strings + total finding count of a WITHHOLDING verdict —
    empty/zero on every non-withholding pass. They exist so the Run execution
    path can author its server-side refusal notice from classes and counts
    alone (C12: the withheld draft TEXT still never leaves this module; these
    are the same class strings the ``guardrail.block`` payload already
    carries). They are IN-MEMORY only — deliberately NOT part of
    :class:`~ne.contracts.runtime.GroundedProvenance`, so every serialized
    provenance byte is unchanged (byte-parity pinned by
    ``test_default_policy_byte_parity_with_explicit_ng1_strict``)."""

    output: str
    provenance: GroundedProvenance
    withheld_reasons: tuple[str, ...] = ()
    withheld_count: int = 0


async def _call_assist(
    assist: Callable[..., Awaitable[str]], **kwargs: Any
) -> tuple[str | None, str | None]:
    """Invoke the injected ``assist`` callable, degrading to
    ``(None, error_class)`` (never raising) on ANY failure — where
    ``error_class`` is the exception TYPE NAME (e.g. ``"UnknownTaskError"``),
    the counts-only signal the ``grounding.assist`` audit event carries
    (design §6.2-3) and NE-HGATE-3's negative routing test asserts. A
    success returns ``(candidate, None)``.

    Rationale (G4, design §4): the assist may only IMPROVE the outcome over
    the no-assist baseline — a raising or otherwise flaky assist must
    degrade to exactly that baseline (the original passing draft on PASS,
    the deterministic table on FAIL), never reduce availability below the
    pure hard-gate. The broad ``except Exception`` is deliberate: it is the
    mechanism that makes an assist failure equivalent to ``assist=None`` for
    THIS call, not a swallowed bug — ``BaseException`` (e.g.
    ``asyncio.CancelledError``) is NOT an ``Exception`` and still propagates,
    so task cancellation is never absorbed here.
    """
    try:
        return str(await assist(**kwargs)), None
    except Exception as exc:
        return None, type(exc).__name__


async def _emit_assist_event(
    policy: GroundingPolicy, *, phase: str, adopted: bool, error_class: str | None
) -> None:
    """Emit exactly ONE counts-only audited ``grounding.assist`` event per assist
    invocation (design §6.2-3), mirroring ``toolset._emit`` (toolset.py:129-157)
    fail-closed: event construction (which resolves the FROZEN config hash) is
    OUTSIDE the try so ``ConfigHashNotFrozenError`` propagates, and a sink-append
    failure becomes a terminal :class:`~ne.runtime.errors.RuntimeAuditError` (C10
    — an un-auditable assist invocation kills the run). ``detail`` carries
    EXACTLY ``{phase, adopted, error_class}`` — NO draft/candidate/question/
    tables text, NO assist kwargs (C12). ``outcome`` is ``error`` iff the assist
    raised (``error_class`` set), else ``success`` — a REJECTED (re-check-failed)
    candidate is a ``success`` with ``adopted=False``, not an error. This is a
    MODULE-LEVEL helper (never nested inside ``run_grounding_gate``) so the
    single-return AST guardian (test_grounding_runtime.py's
    ``test_gate_single_emit_point_ast``) walks a truthful function body. Pinned
    by ``test_assist_event_emitted_once_counts_only`` +
    ``test_assist_event_append_failure_fails_closed``.
    """
    event = new_audit_event(
        action="grounding.assist",
        resource="grounding.gate",
        outcome=AuditOutcome.error if error_class is not None else AuditOutcome.success,
        actor="ne-runtime",
        detail=json.dumps({"phase": phase, "adopted": adopted, "error_class": error_class}),
        trace_id=current_run_trace_id(),
    )
    try:
        await policy.sink.append(event)
    except Exception as exc:
        raise RuntimeAuditError(
            "audit append failed for 'grounding.assist' on 'grounding.gate'; "
            "result withheld (C10 fail closed)"
        ) from exc


async def _emit_advisory_event(
    policy: GroundingPolicy, *, advisory: Sequence[GroundingFinding]
) -> None:
    """Emit ONE counts-only audited ``grounding.advisory`` event for a
    NON-withholding pass-through that still carried advisory findings (design
    §2.5 line 23-25: "Advisory means recorded, never dropped" — the flywheel/
    steward curation signal). On the WITHHOLD branch the advisory split already
    rides the ``guardrail.block`` payload (``advisory_reasons``); this helper is
    the ONLY audit channel for advisory findings on the PASS path, where no block
    is emitted and (with ``assist=None``) no ``grounding.assist`` event fires —
    without it the advisory counts would be silently lost (Codex C1).

    ``detail`` carries EXACTLY ``{strictness, advisory_reasons, advisory_count}``
    — reason CLASS strings + counts only, no draft/candidate text (C12). Fires
    ONLY when ``decision.advisory`` is non-empty, so a DEFAULT (STRICT) policy —
    whose ``advisory`` tuple is always empty (``ENFORCEMENT[STRICT] ==
    _ALL_CLASSES``) — never emits it and stays byte-for-byte identical to today
    (pinned by ``test_default_policy_byte_parity_with_explicit_ng1_strict``).
    Fail-closed like :func:`_emit_assist_event` (C10): a sink-append failure
    becomes a terminal :class:`~ne.runtime.errors.RuntimeAuditError`. Module-level
    (never nested) so the single-return AST guardian walks a truthful body.
    Pinned by ``test_advisory_event_emitted_counts_only`` +
    ``test_advisory_event_append_failure_fails_closed`` +
    ``test_standard_tier_advisory_vocab_passes_and_stamps``.
    """
    event = new_audit_event(
        action="grounding.advisory",
        resource="grounding.gate",
        outcome=AuditOutcome.success,
        actor="ne-runtime",
        detail=json.dumps(
            {
                "strictness": policy.strictness.value,
                "advisory_reasons": [finding.reason for finding in advisory],
                "advisory_count": sum(finding.count for finding in advisory),
            }
        ),
        trace_id=current_run_trace_id(),
    )
    try:
        await policy.sink.append(event)
    except Exception as exc:
        raise RuntimeAuditError(
            "audit append failed for 'grounding.advisory' on 'grounding.gate'; "
            "result withheld (C10 fail closed)"
        ) from exc


async def run_grounding_gate(
    *,
    draft: str,
    prompt: str,
    principal: Principal,
    fetched: Sequence[Mapping[str, Any]],
    policy: GroundingPolicy,
) -> GateResult:
    """The G3 POST-step + G4 bounded model-assist: certify ``draft`` against
    this turn's ``fetched`` answers, fail closed to an audited deterministic
    fallback, then (if ``policy.assist`` is injected) run AT MOST ONE
    additional assist pass — re-certified by the SAME gate before it may
    replace the output. This function has exactly ONE ``return`` statement:
    every value ever assigned to ``output`` either already passed
    ``check_grounding`` (the original ``draft``) or is re-checked by it
    before assignment (an accepted assist candidate); the single terminal
    ``GateResult`` construction is the ONE emit point (design §4's binding
    invariant).

    PASS: the model's ``draft`` is the baseline, alongside a
    :class:`~ne.contracts.runtime.GroundedProvenance` built from ``fetched``
    (never from the draft's own text — the same H1-forgeability discipline
    ``VerifiedProvenance`` established for the verified-answer-first path).
    If ``policy.assist`` is injected, it is called ONCE with the question,
    the passing draft, and the rendered tables; its candidate output is
    re-checked by ``check_grounding`` against the SAME corpus+prompt — if
    THAT re-check passes, the candidate is emitted instead
    (``assist_used=True``); otherwise (re-check fails, or the assist raised)
    the ORIGINAL passing draft is emitted (``assist_used=False`` — the
    assist can only improve, never degrade).

    FAIL: exactly ONE audited ``guardrail.block`` is emitted via
    ``policy.guardrail_engine.enforce`` with GENUINE booleans
    (``grounded_gate=True``, ``grounded=False``) and reason CLASSES + a
    finding COUNT only (C12 — the ungrounded draft TEXT never leaves this
    function) — BEFORE any assist call, so an un-auditable FAIL still kills
    the run pre-assist. ``GuardrailViolation``/``GuardrailEvaluationError``
    raised by that call are SUPPRESSED (already audited); ``AuditEmissionError``
    is NOT suppressed and propagates, killing the run (H6). The draft is then
    WITHHELD ENTIRELY and the deterministic ``policy.render_fallback(fetched)``
    table becomes the baseline. If ``policy.assist`` is injected, it is
    called ONCE with the question, the rendered tables, and the FAIL's
    reason CLASSES ONLY — the withheld draft TEXT is NEVER passed to it
    (structural C12). Its candidate is re-checked the same way; on a passing
    re-check the candidate is emitted (``fallback=True``, ``assist_used=True``
    — still exactly one audited block for the ORIGINAL failure, since that
    was a true fact about the model's first attempt); otherwise the
    deterministic table is emitted (``assist_used=False``).

    TIER + PROFILE (NE-HGATE-2, design §3.1/§2.5): the branch condition is
    WITHHOLD, decided by :func:`ne.govern.grounding_policy.apply_strictness`
    (``not decision.withhold``), NOT ``verdict.passed`` (E-P1-1) — advisory
    findings (``vocab-ungrounded``/``vocab-extended`` at STANDARD) appear in the
    verdict and in the FAIL payload's ``advisory_reasons`` split but NEVER flip
    withhold (pinned by ``test_standard_tier_advisory_vocab_passes_and_stamps``
    + ``test_fail_payload_strictness_and_enforced_advisory_split``). On a
    NON-withholding PASS-through the advisory findings are instead recorded via a
    single counts-only ``grounding.advisory`` audit event
    (:func:`_emit_advisory_event`) — the FAIL payload's block is not emitted on a
    PASS, so this is the only audit channel keeping advisory "recorded, never
    dropped" (design §2.5; Codex C1). It fires only when advisory findings exist,
    so the DEFAULT STRICT policy never emits it (pinned by
    ``test_advisory_event_emitted_counts_only``). Both the
    original check and the G4 assist re-check use ``profile=policy.profile`` and
    the SAME ``policy.strictness`` tier — symmetric (pinned by
    ``test_recheck_applies_same_tier_symmetrically``). The provenance stamps
    ``gate=policy.profile.version`` + ``strictness=policy.strictness.value``.
    Each assist invocation emits ONE counts-only ``grounding.assist`` audit
    event (:func:`_emit_assist_event`) — ``{phase, adopted, error_class}``, no
    text (C12, pinned by ``test_assist_event_emitted_once_counts_only``). With
    the DEFAULT policy (STRICT + ``PROFILE_NG1_FROZEN``), ``apply_strictness(v,
    STRICT).withhold == (not v.passed)`` and ``profile.version == GATE_VERSION``,
    so this reproduces today's gate byte-for-byte (pinned by
    ``test_default_policy_byte_parity_with_explicit_ng1_strict``).
    """
    corpus = GroundingCorpus.from_answers(fetched)
    verdict = check_grounding(draft, corpus, prompt, profile=policy.profile)
    decision = apply_strictness(verdict, policy.strictness)

    # Safe to trust these types: _parse_answer_mapping (the recorder's own
    # shape gate) already required exactly these primitive types before a
    # mapping was ever appended to `fetched` — mirrors
    # ne.runtime.verified_router.py's own re-parse-after-pre-gate pattern.
    fetches = tuple(
        VerifiedProvenance(
            template_id=str(m["template_id"]),
            template_version=int(m["template_version"]),
            config_hash=str(m["config_hash"]),
            row_count=len(m["rows"]),
            truncated=bool(m["truncated"]),
        )
        for m in fetched
    )

    assist_used = False
    withheld_reasons: tuple[str, ...] = ()
    withheld_count = 0

    if not decision.withhold:
        output = draft
        echo = verdict.prompt_echo_count
        fallback = False
        # NE-HGATE-2 / Codex C1: a PASS-through can still carry advisory findings
        # (STANDARD non-fact-sentence vocab). No guardrail.block is emitted here,
        # and with assist=None no grounding.assist event fires either — so this is
        # the ONLY audit channel for those advisory counts (design §2.5: advisory
        # is RECORDED, never dropped). Empty under the DEFAULT STRICT policy, so
        # byte-parity is untouched.
        if decision.advisory:
            await _emit_advisory_event(policy, advisory=decision.advisory)
        if policy.assist is not None:
            tables = policy.render_fallback(fetched)
            candidate, error_class = await _call_assist(
                policy.assist, question=prompt, draft=draft, tables=tables
            )
            if candidate is not None:
                recheck = check_grounding(candidate, corpus, prompt, profile=policy.profile)
                if not apply_strictness(recheck, policy.strictness).withhold:
                    output = candidate
                    assist_used = True
                    echo = recheck.prompt_echo_count
                # else: re-check withheld -> keep the ORIGINAL passing draft.
            await _emit_assist_event(
                policy, phase="pass-polish", adopted=assist_used, error_class=error_class
            )
    else:
        # NE-GA-RUN1: the enforced classes + count the Run path's refusal
        # notice is authored from — the SAME facts the guardrail.block payload
        # below records (classes/counts only, C12; never draft text).
        withheld_reasons = tuple(finding.reason for finding in decision.enforced)
        withheld_count = sum(finding.count for finding in decision.enforced)
        payload: dict[str, Any] = {
            "principal": principal,
            "tool": "grounding.gate",
            "grounded_gate": True,
            "grounded": False,
            "reasons": [finding.reason for finding in verdict.findings],
            "finding_count": sum(finding.count for finding in verdict.findings),
            # NE-HGATE-2: the deployment posture + the enforced/advisory split
            # (design §2.5, E-P2-2). CLASS strings + counts only (C12) — advisory
            # findings are RECORDED here but never flipped `withhold` (they are
            # not in `decision.enforced`). Legacy `reasons`/`finding_count` stay
            # byte-identical, covering ALL findings.
            "strictness": policy.strictness.value,
            "enforced_reasons": [f.reason for f in decision.enforced],
            "advisory_reasons": [f.reason for f in decision.advisory],
        }
        # Both GuardrailViolation (a check-time block) and
        # GuardrailEvaluationError (a guardrail that raised internally) have
        # ALREADY been audited (exactly one guardrail.block / guardrail.error
        # event) by the time enforce() raises either — suppressing them here
        # renders the fallback, not a second audit. AuditEmissionError is
        # deliberately NOT suppressed: an un-auditable call must still kill
        # the run (module docstring, H6) — and it does so BEFORE any assist
        # call runs, per this branch's ordering.
        with contextlib.suppress(GuardrailViolation, GuardrailEvaluationError):
            await policy.guardrail_engine.enforce(payload)

        tables = policy.render_fallback(fetched)
        output = tables
        echo = verdict.prompt_echo_count
        fallback = True
        if policy.assist is not None:
            reasons = tuple(finding.reason for finding in verdict.findings)
            candidate, error_class = await _call_assist(
                policy.assist, question=prompt, tables=tables, reasons=reasons
            )
            if candidate is not None:
                recheck = check_grounding(candidate, corpus, prompt, profile=policy.profile)
                if not apply_strictness(recheck, policy.strictness).withhold:
                    output = candidate
                    assist_used = True
                    echo = recheck.prompt_echo_count
                # else: second withhold -> keep the deterministic table.
            await _emit_assist_event(
                policy, phase="fail-redraft", adopted=assist_used, error_class=error_class
            )

    return GateResult(
        output=output,
        withheld_reasons=withheld_reasons,
        withheld_count=withheld_count,
        provenance=GroundedProvenance(
            fetches=fetches,
            citation_count=corpus.citation_count,
            gate=policy.profile.version,
            strictness=policy.strictness.value,
            fallback=fallback,
            assist_used=assist_used,
            prompt_echo_count=echo,
        ),
    )
