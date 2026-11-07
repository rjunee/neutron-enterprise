# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""VerifiedRouter — the deterministic verified-or-clarify-or-refuse turn PRE-STEP.

Implements Layer 1 (routing) + Layer 2 (the audited refusal channel) of
verified-answer-first: composed together with Layer 0
(``ne.contracts.runtime.VerifiedProvenance``), this is the "keystone"
NE-VAF-U3 unit.

C3 injection contract: this module imports NOTHING from ``ne.brain_ontology``.
The U1 deterministic matcher (``ne.brain_ontology.verified.routing``) and the
U2 output guardrail (``ne.brain_ontology`` consumers wire
``VerifiedDomainGuardrail`` from ``ne.govern``) are consumed ONLY through the
structural Protocols below (:class:`RoutingRowLike`, :class:`RouteDecisionLike`,
:class:`GuardrailEnforcerLike`) plus plain ``Callable`` injection points — the
matcher/rows/renderer are handed in by the composition root (U4), never
imported here. This is enforced three ways: (1) no import anywhere in this
file, (2) the ``.importlinter`` ``runtime-verified-lane`` contract (``ne.runtime``
forbidden from importing ``ne.brain_ontology``), and (3) an AST-based guardian
test. ``RouteOutcome`` (a ``StrEnum`` in the injected matcher) is therefore
compared by its plain STRING value ('route'/'clarify'/'refuse'/'pass') only —
no enum import is needed or wanted.

Fail-closed policy (H6): every exception on the ROUTE execution path converts
to the Layer-2 AUDITED refusal — never free text, never a raised error frame —
with the sole exception of ``RuntimeAuditError``/``AuditEmissionError``: an
un-auditable call must kill the run (mirrors
``ne.runtime.middleware``/``ne.runtime.runner``'s own fail-closed posture). A
``GuardrailViolation`` raised BY the governed executor's own engine has
ALREADY been audited (exactly one ``guardrail.block``); this module renders
that refusal directly and does NOT call its own ``guardrail_engine.enforce``
a second time (no double-audit). When this module's OWN
``guardrail_engine.enforce`` call (in ``_audited_refusal``) raises
``GuardrailViolation`` or ``GuardrailEvaluationError``, both are already
audited (``guardrail.block``/``guardrail.error``) and are suppressed so the
Layer-2 refusal still renders; only ``AuditEmissionError`` propagates from
that call. The model is NEVER instantiated on
ROUTE/CLARIFY/REFUSE — every menu and refusal string here is a pure,
deterministic render of injected config data (descriptions + canonical
triggers), zero model tokens.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from pydantic_ai.messages import ModelMessage, ModelResponse

from ne.contracts.runtime import VerifiedProvenance
from ne.contracts.types import Principal
from ne.govern.errors import AuditEmissionError, GuardrailEvaluationError, GuardrailViolation
from ne.runtime.actions import tier_for_action
from ne.runtime.errors import RuntimeAuditError
from ne.runtime.runner import ToolExecutorLike
from ne.runtime.source_results import ProtectedSourceResultGuard

__all__ = [
    "ROUTER_PASS",
    "GuardrailEnforcerLike",
    "RouteDecisionLike",
    "RoutedTurn",
    "RouterOutcome",
    "RouterPass",
    "RoutingRowLike",
    "VerifiedRouter",
]

# Marker namespace (H-unforgeable): these strings only ever reach
# ``ModelResponse.model_name`` through THIS module's own return values — a
# user prompt containing the literal text lands as ``UserPromptPart`` content
# (inert; see :meth:`VerifiedRouter._clarify_count`), and every other agent
# turn stamps ``ne-gateway:<task>`` (see ``ne.runtime.model``).
_CLARIFY_PREFIX: Final = "ne-verified-router:clarify"
_CLARIFY_DOMAIN_PREFIX: Final = "ne-verified-router:clarify:"
_ROUTE_PREFIX: Final = "ne-verified-router:route:"
_REFUSE_PREFIX: Final = "ne-verified-router:refuse:"


class RoutingRowLike(Protocol):
    """The plain-data routing-row surface this module needs (mirrors
    ``ne.brain_ontology.verified.routing.RoutingRow`` structurally, WITHOUT
    importing it — C3)."""

    @property
    def domain(self) -> str: ...

    @property
    def template_id(self) -> str: ...

    @property
    def tool_name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def triggers(self) -> tuple[str, ...]: ...

    @property
    def guard_terms(self) -> tuple[str, ...]: ...

    @property
    def clarify_max(self) -> int: ...


class RouteDecisionLike(Protocol):
    """The plain-data matcher-verdict surface (mirrors
    ``ne.brain_ontology.verified.routing.RouteDecision`` structurally).

    ``outcome`` is a plain ``str`` here (the real ``RouteOutcome`` is a
    ``StrEnum`` and satisfies ``str`` at runtime) — compare against the
    literals ``'route'``/``'clarify'``/``'refuse'``/``'pass'`` only.
    """

    @property
    def outcome(self) -> str: ...

    @property
    def template_id(self) -> str | None: ...

    @property
    def domain(self) -> str | None: ...

    @property
    def domains(self) -> tuple[str, ...]: ...


class GuardrailEnforcerLike(Protocol):
    """The narrow ``GuardrailEngine`` surface this module needs. The real
    ``ne.govern.guardrails.engine.GuardrailEngine`` satisfies this; test
    fakes are trivial."""

    async def enforce(self, payload: Mapping[str, Any]) -> object: ...


@dataclass(frozen=True, slots=True)
class RoutedTurn:
    """A terminal, model-bypassing turn outcome: ROUTE, CLARIFY, or REFUSE."""

    output: str
    marker: str
    verified: VerifiedProvenance | None


class RouterPass:
    """Sentinel: no verified-domain signal fired — fall through to the model loop."""

    __slots__ = ()


#: The single, shared PASS sentinel — compare with ``is``, never ``==``.
ROUTER_PASS: Final[RouterPass] = RouterPass()

RouterOutcome = RoutedTurn | RouterPass


class VerifiedRouter[RowT: RoutingRowLike]:
    """Deterministic verified-or-clarify-or-refuse turn PRE-STEP (NE-VAF-U3).

    Generic over the injected row type (PEP 695): the real
    ``ne.brain_ontology.verified.routing.match_template``/``match_selection``
    take ``Sequence[RoutingRow]`` (narrower than ``Sequence[RoutingRowLike]``),
    so a non-generic ``Callable[[str, Sequence[RoutingRowLike]], ...]`` param
    would REJECT them under standard contravariance; the generic makes the
    injected callables type-sound for the real matcher.

    DECIDED DEVIATION: there is no separate ``audit_sink`` constructor
    parameter. ``guardrail_engine``'s OWN constructor-required sink is the
    single audit path for every refusal this router emits; a second sink
    handle would invite a divergent, unaudited path and is deliberately not
    offered.
    """

    def __init__(
        self,
        *,
        rows: Sequence[RowT],
        match: Callable[[str, Sequence[RowT]], RouteDecisionLike],
        match_selection: Callable[[str, Sequence[RowT]], str | None],
        render_answer: Callable[[str], str],
        executor: ToolExecutorLike,
        guardrail_engine: GuardrailEnforcerLike,
        source_guard: ProtectedSourceResultGuard | None = None,
    ) -> None:
        self._rows: tuple[RowT, ...] = tuple(rows)
        self._match = match
        self._match_selection = match_selection
        self._render_answer = render_answer
        self._executor = executor
        self._guardrail_engine = guardrail_engine
        # PRE-015A0-ENFORCEMENT: a routed verified row is a DIRECT publication —
        # it reaches the user without a model send — so a source-bearing row is
        # re-authorized here under the request's own snapshot. None ⇒ byte-identical
        # legacy routing (the T3/verified rows that carry no source reference).
        self._source_guard = source_guard

    async def route(
        self,
        prompt: str,
        *,
        principal: Principal,
        history: Sequence[ModelMessage],
    ) -> RouterOutcome:
        """Run the CLARIFY-state-machine pre-step; PASS falls through to the model."""
        if not self._rows:
            return ROUTER_PASS

        # (1) SELECTION FOLLOW-UP: the immediately preceding turn was a clarify
        # menu for a domain — try to resolve THIS prompt as an ordinal/echo
        # selection over that SAME domain's rows before running the general
        # matcher. v1's routing table is single-domain, so ordinals always
        # match the shown menu; a multi-domain future table would still be
        # safe here (worst case: a re-clarify), never a mis-route — the
        # rebuild is deliberately scoped to the marker's single domain (the
        # brief's literal instruction).
        last_response = self._last_response(history)
        if (
            last_response is not None
            and isinstance(last_response.model_name, str)
            and last_response.model_name.startswith(_CLARIFY_DOMAIN_PREFIX)
        ):
            domain = last_response.model_name[len(_CLARIFY_DOMAIN_PREFIX) :]
            if domain:
                domain_rows = self._menu_rows_for(domain)
                selected = self._match_selection(prompt, domain_rows)
                if selected is not None:
                    row = next((r for r in domain_rows if r.template_id == selected), None)
                    if row is not None:
                        return await self._execute_route(row, principal)

        # (2) General matcher.
        decision = self._match(prompt, self._rows)

        if decision.outcome == "route":
            row = next((r for r in self._rows if r.template_id == decision.template_id), None)
            if row is None:
                # Defensive: the matcher/table disagree on a template id. Never
                # execute an unresolved tool name — audited fail-closed refusal.
                return await self._audited_refusal(
                    principal=principal,
                    tool="verified.router",
                    domain=decision.domain or "unknown",
                    clarify_exhausted=False,
                )
            return await self._execute_route(row, principal)

        if decision.outcome == "clarify":
            domain = decision.domain
            if domain is None:
                # Defensive: the matcher never emits a domain-less CLARIFY in
                # v1; fall through to the model rather than guess.
                return ROUTER_PASS
            hit_domains = decision.domains or (domain,)
            menu_rows = tuple(
                sorted(
                    (r for r in self._rows if r.domain in hit_domains),
                    key=lambda r: r.template_id,
                )
            )
            if not menu_rows:
                menu_rows = self._menu_rows_for(domain)
            bound = min((r.clarify_max for r in menu_rows), default=0)
            counter = self._clarify_count(history)
            if counter >= bound:
                return await self._refuse_exhausted(principal, domain, menu_rows)
            return RoutedTurn(
                output=self._clarify_text(menu_rows),
                marker=f"{_CLARIFY_DOMAIN_PREFIX}{domain}",
                verified=None,
            )

        if decision.outcome == "refuse":
            # Defensive: the matcher never emits REFUSE in v1 (it is reserved
            # for the stateful clarify-bound-exhausted transition this router
            # owns) — handled identically to the bound-exhausted case, but
            # without treating THIS as a clarify-bound exhaustion.
            domain = decision.domain
            menu_rows = self._menu_rows_for(domain)
            return await self._audited_refusal(
                principal=principal,
                tool="verified.router",
                domain=domain or "unknown",
                clarify_exhausted=False,
                menu_rows=menu_rows,
            )

        # 'pass' or any unrecognized outcome: fall through to the model.
        return ROUTER_PASS

    # -- history-derived state (no new DB column) --------------------------

    @staticmethod
    def _last_response(history: Sequence[ModelMessage]) -> ModelResponse | None:
        for message in reversed(history):
            if isinstance(message, ModelResponse):
                return message
        return None

    @staticmethod
    def _clarify_count(history: Sequence[ModelMessage]) -> int:
        """Count CONSECUTIVE TRAILING real clarify-marker ``ModelResponse``s.

        Only ``ModelResponse.model_name`` markers count — a user prompt
        containing the literal marker text lands as ``UserPromptPart``
        content (a non-``ModelResponse`` message) and is SKIPPED, never
        counted and never breaking the run. A real, non-clarify
        ``ModelResponse`` (e.g. the gateway's own ``ne-gateway:*`` marker)
        stops the count.
        """
        count = 0
        for message in reversed(history):
            if isinstance(message, ModelResponse):
                if isinstance(message.model_name, str) and message.model_name.startswith(
                    _CLARIFY_PREFIX
                ):
                    count += 1
                    continue
                break
        return count

    # -- ROUTE execution -----------------------------------------------------

    async def _execute_route(self, row: RowT, principal: Principal) -> RouterOutcome:
        # HITL DEFENSE (non-blocking): a chat turn must never hang on a human
        # approval gate. If the tool's STATIC tier (spec tier or the Rego
        # mirror) is already >= 2, refuse up front rather than await a gate
        # inside executor.execute. accounts-directory is TIER_0, so the happy
        # path never reaches this branch; a PDP-time escalation is still
        # covered by the exception arm below (defense-in-depth).
        spec = next((s for s in self._executor.tool_specs if s.name == row.tool_name), None)
        if spec is not None and max(int(spec.tier), int(tier_for_action(spec.name))) >= 2:
            return await self._audited_refusal(
                principal=principal,
                tool=row.tool_name,
                domain=row.domain,
                clarify_exhausted=False,
            )

        # LAZY import (grounding.py top-imports THIS module — a top-level import
        # here would be a cycle). The recorder wraps the SAME governed executor and
        # captures the STRUCTURED verified answer the handler records DIRECTLY
        # (§4.2): the handler now returns the COMPACT rendering, so json.loads(raw)
        # is dead — the structured artifact arrives via recorder.answers, not the
        # model-visible string. No new tool-execution surface (C6): the recorder
        # only delegates to self._executor.
        from ne.runtime.grounding import GroundingRecorder

        recorder = GroundingRecorder(self._executor)
        try:
            raw = await recorder.execute(row.tool_name, {}, principal=principal)
            if self._source_guard is not None:
                # A contributor-stamped verified row is genuinely source-bearing:
                # resolve its opaque reference under the request's own unexpired
                # authorization before it can be rendered. Anything but AUTHORIZED
                # (including an absent context or target binding, which raise into
                # the audited-refusal arm below) refuses the whole turn rather than
                # rendering a safe placeholder as a verified answer.
                materialized = await self._source_guard.materialize_direct_text(raw)
                if materialized is not None:
                    if not materialized.authorized:
                        return await self._audited_refusal(
                            principal=principal,
                            tool=row.tool_name,
                            domain=row.domain,
                            clarify_exhausted=False,
                        )
                    raw = materialized.text
            if not recorder.answers:
                # The governed handler ran but recorded no structured answer (a
                # non-verified tool bound to this route, or a compact string with
                # no direct record) — never certify provenance from an unstructured
                # return; fail closed to the audited Layer-2 refusal.
                return await self._audited_refusal(
                    principal=principal,
                    tool=row.tool_name,
                    domain=row.domain,
                    clarify_exhausted=False,
                )
            parsed = recorder.answers[0]
            # Provenance integrity: the governed handler bound to this route's
            # tool must return an artifact stamped with the SAME template_id as
            # the deterministically-selected row. A mismatch means a miswired
            # binding (tool_name -> the wrong template's handler); never emit a
            # foreign template_id under this route's marker — fail closed to an
            # audited Layer-2 refusal rather than certify unverified provenance.
            if str(parsed["template_id"]) != row.template_id:
                return await self._audited_refusal(
                    principal=principal,
                    tool=row.tool_name,
                    domain=row.domain,
                    clarify_exhausted=False,
                )
            provenance = VerifiedProvenance(
                template_id=str(parsed["template_id"]),
                template_version=int(parsed["template_version"]),
                config_hash=str(parsed["config_hash"]),
                row_count=len(parsed["rows"]),
                truncated=bool(parsed["truncated"]),
            )
            output = self._render_answer(raw)
            return RoutedTurn(
                output=output, marker=f"{_ROUTE_PREFIX}{row.domain}", verified=provenance
            )
        except (RuntimeAuditError, AuditEmissionError):
            # An un-auditable call must kill the run — never converted to a
            # refusal (mirrors agent.py / ne.runtime.runner._BLOCKING_ERRORS).
            raise
        except GuardrailViolation:
            # The EXECUTOR's own engine already appended exactly ONE
            # guardrail.block before raising — render the refusal directly,
            # do NOT enforce a second time (no double-audit).
            return RoutedTurn(
                output=self._refusal_text(self._menu_rows_for(row.domain)),
                marker=f"{_REFUSE_PREFIX}{row.domain}",
                verified=None,
            )
        except Exception:
            # Covers ToolDeniedError (PDP DENY), ToolExecutionError (the
            # wrapper every handler exception arrives as — including a
            # wrapped VerifiedAnswerScopeError or an unbound-TURN_PRINCIPAL
            # RuntimeError; C3 forbids naming the ontology class, and the
            # wrapper makes that unnecessary), ApprovalNotGrantedError /
            # ApprovalIntegrityError (PDP-escalated HITL), ToolNotRegisteredError,
            # GuardrailEvaluationError, and any json/KeyError parse failure or
            # render_answer failure — ALL become the Layer-2 audited refusal.
            return await self._audited_refusal(
                principal=principal,
                tool=row.tool_name,
                domain=row.domain,
                clarify_exhausted=False,
            )

    # -- Layer-2 refusal helpers ---------------------------------------------

    async def _audited_refusal(
        self,
        *,
        principal: Principal,
        tool: str,
        domain: str,
        clarify_exhausted: bool,
        menu_rows: tuple[RowT, ...] | None = None,
    ) -> RoutedTurn:
        payload: dict[str, Any] = {
            "principal": principal,
            "tool": tool,
            "verified_domain": True,
            "routed": False,
            "clarify_exhausted": clarify_exhausted,
        }
        # Both GuardrailViolation (a check-time block) and GuardrailEvaluationError
        # (a guardrail that raised internally) have ALREADY been audited (exactly
        # one guardrail.block / guardrail.error event) by the time enforce() raises
        # either — this render is the documented Layer-2 refusal, not a second
        # audit. AuditEmissionError is deliberately NOT suppressed: an un-auditable
        # call must still kill the run (see module docstring, H6).
        with contextlib.suppress(GuardrailViolation, GuardrailEvaluationError):
            await self._guardrail_engine.enforce(payload)
        rows = menu_rows if menu_rows is not None else self._menu_rows_for(domain)
        return RoutedTurn(
            output=self._refusal_text(rows), marker=f"{_REFUSE_PREFIX}{domain}", verified=None
        )

    async def _refuse_exhausted(
        self, principal: Principal, domain: str, menu_rows: tuple[RowT, ...]
    ) -> RoutedTurn:
        return await self._audited_refusal(
            principal=principal,
            tool="verified.router",
            domain=domain,
            clarify_exhausted=True,
            menu_rows=menu_rows,
        )

    # -- deterministic, zero-model-token rendering ---------------------------

    def _menu_rows_for(self, domain: str | None) -> tuple[RowT, ...]:
        if domain is None:
            return self._rows_sorted()
        matched = tuple(sorted((r for r in self._rows if r.domain == domain), key=_by_template_id))
        return matched or self._rows_sorted()

    def _rows_sorted(self) -> tuple[RowT, ...]:
        return tuple(sorted(self._rows, key=_by_template_id))

    @staticmethod
    def _menu_lines(rows: Sequence[RoutingRowLike]) -> list[str]:
        lines: list[str] = []
        for i, row in enumerate(rows, 1):
            example = row.triggers[0] if row.triggers else row.template_id
            lines.append(f'{i}. {row.description} (e.g. "{example}")')
        return lines

    def _clarify_text(self, rows: Sequence[RoutingRowLike]) -> str:
        lines = "\n".join(self._menu_lines(rows))
        return (
            "Which verified question did you mean?\n"
            f"{lines}\n"
            "Reply with the number, or rephrase using one of the examples."
        )

    def _refusal_text(self, rows: Sequence[RoutingRowLike]) -> str:
        lines = "\n".join(self._menu_lines(rows))
        return (
            "None of the verified questions I can answer matched your request, "
            "and I cannot guess. I can answer:\n"
            f"{lines}\n"
            "Rephrase using one of the examples to get a verified answer."
        )


def _by_template_id(row: RoutingRowLike) -> str:
    return row.template_id
