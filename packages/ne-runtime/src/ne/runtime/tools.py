# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tool bindings + the specs-only registry (structural no-bypass property #1).

A :class:`ToolBinding` couples a frozen ``ne.contracts.runtime.ToolSpec`` to its
executable ``handler`` PLUS the binding-owned extractors that feed the C12
guardrails (recipients, egress URLs, estimated cost). Those extractors live on
the BINDING, not on the model's arguments — so a prompt-injected model can never
suppress a guardrail input by omitting a field: the trusted binding derives them
from the args.

Handlers NEVER enter the :class:`RuntimeToolRegistry`. The registry holds
``ToolSpec`` objects ONLY (for catalog rendering); the executable ``handler``
exists solely inside a :class:`ToolBinding` handed to the
:class:`~ne.runtime.middleware.GovernedToolExecutor`. There is no public
registry path from a tool name to a callable — the sole way to run a handler is
``GovernedToolExecutor.execute`` (C6 no-bypass).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ne.contracts.runtime import ToolSpec
from ne.runtime.actions import tier_for_action
from ne.runtime.errors import ToolNotRegisteredError, ToolRegistrationError

#: A tool handler: async, takes the (already guardrail/PDP-cleared) args, returns text.
ToolHandler = Callable[[Mapping[str, object]], Awaitable[str]]

#: Binding-owned extractor deriving a sequence of strings (recipients / egress URLs).
_SeqExtractor = Callable[[Mapping[str, object]], Sequence[str]]
#: Binding-owned extractor deriving an optional cost estimate string.
_CostExtractor = Callable[[Mapping[str, object]], str | None]
#: Binding-owned extractor deriving REDACTED audit detail from (args, handler-result).
#: Values MUST be names / ids / shapes only — never payload VALUES (C12/C7-write).
_AuditDetailExtractor = Callable[[Mapping[str, object], str], Mapping[str, Any]]


@dataclass(frozen=True)
class ToolBinding:
    """A tool spec + its handler + the binding-owned guardrail-input extractors."""

    spec: ToolSpec
    handler: ToolHandler
    #: Derives outbound recipients (emails) from the args, for the C12 recipient
    #: allowlist. ``None`` => the tool has no outbound recipients.
    extract_recipients: _SeqExtractor | None = None
    #: Derives outbound egress URLs from the args, for the C12 egress allowlist.
    extract_egress_urls: _SeqExtractor | None = None
    #: Derives a pre-call cost estimate (USD, Decimal-as-string) for the spend cap.
    estimate_cost_usd: _CostExtractor | None = None
    #: Derives REDACTED audit detail from ``(args, handler_result_str)`` on the SUCCESS
    #: path only — a BINDING-OWNED (trusted, like :attr:`extract_recipients`) derivation
    #: of the ``tool.execute[success]`` event's ``detail``. Its values MUST be field
    #: NAMES / record ids / payload SHAPES only, never payload VALUES (C7-write audit
    #: metadata, not content). ``None`` => the success event carries no extra detail
    #: (the pre-NE-050 behaviour). NE-050 write-back actions set this so every executed
    #: write records what-action / what-target / payload-shape.
    extract_audit_detail: _AuditDetailExtractor | None = None
    #: NE-ACTION-VALIDATE-BEFORE-GATE: a binding-OWNED (trusted) typed-input preflight,
    #: DERIVED from the same input model the handler validates against (never a second,
    #: divergent implementation — PR #377's lesson). The executor
    #: (:class:`~ne.runtime.middleware.GovernedToolExecutor`) runs this AFTER
    #: guardrails + PDP and BEFORE the human approval gate; any raise refuses the call
    #: PRE-gate — audited (``tool.validate[denied]``), fail closed — so an invalid call
    #: never consumes a human decision. ``None`` => no preflight (read tools, and any
    #: binding with no typed-input concept).
    validate_args: Callable[[Mapping[str, object]], None] | None = None


def validate_binding(binding: ToolBinding) -> None:
    """Reject an unclassifiable or under-tiered binding (fail loud at registration).

    * ``tier_for_action(spec.name)`` must classify the action (else the raised
      :class:`ToolRegistrationError` names it) — deny-by-default mirror of the Rego.
    * The declared ``spec.tier`` may only be >= the action's class tier. A declared
      tier *below* the class tier understates control and is a misconfiguration; it
      is rejected here (the Rego takes ``max`` anyway, but a tool must never SHIP
      declaring a laxer tier than its action class implies).
    """
    class_tier = tier_for_action(binding.spec.name)
    if int(binding.spec.tier) < int(class_tier):
        raise ToolRegistrationError(
            f"tool {binding.spec.name!r} declares tier {int(binding.spec.tier)} but its "
            f"action class requires at least tier {int(class_tier)} (declared tier may "
            "only escalate, never relax, the class tier)"
        )


class RuntimeToolRegistry:
    """Specs-only tool registry (``ne.contracts.runtime.ToolRegistry`` Protocol).

    Holds ``ToolSpec`` objects for catalog rendering. It NEVER stores or returns a
    handler — a handler lives only inside a :class:`ToolBinding` the executor
    holds privately (structural no-bypass property #1).
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> None:
        self._specs[tool.name] = tool

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ToolNotRegisteredError(f"no tool spec registered under {name!r}") from exc

    def list_tools(self) -> Sequence[ToolSpec]:
        return tuple(self._specs.values())
