# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pluggable judge seam (SPEC §5 NE-024 judge policy).

The :class:`Judge` Protocol is a *structural* seam declared here in ne-eval (NOT
in ne-contracts, which is frozen). The only model seam a judge may use is
``ne.contracts.models.ModelGateway`` — :class:`GatewayJudge` routes every call
through it (C2/C3: gateway-only egress, model independence; ne-eval imports no
provider SDK).

Judge-output robustness (the flakiest seam) is handled here: strip whitespace,
strip a single leading/trailing markdown fence, ``json.loads``, validate against
the caller's pydantic ``out_model``; on any failure retry ONCE with a corrective
message; a second failure raises :class:`JudgeOutputError`. Cross-family judging
is mandatory — :func:`enforce_cross_family` refuses a same-family judge.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from ne.contracts.models import CompletionRequest, ModelGateway
from ne.contracts.types import ChatMessage
from ne.eval.errors import CrossFamilyError, EvalConfigError, JudgeOutputError
from ne.eval.rubrics import Rubric, render

_CORRECTIVE = (
    "Your previous output was not valid JSON matching the required schema. "
    "Return ONLY the JSON object."
)


class _JudgeModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class StatementsOut(_JudgeModel):
    """Output of the faithfulness_statements rubric."""

    statements: tuple[str, ...]


class VerdictItem(_JudgeModel):
    """One verdict in the faithfulness_verdicts output."""

    statement: str
    verdict: int
    reason: str | None = None


class VerdictsOut(_JudgeModel):
    """Output of the faithfulness_verdicts rubric."""

    verdicts: tuple[VerdictItem, ...]


class PrecisionOut(_JudgeModel):
    """Output of the context_precision rubric (single context)."""

    verdict: int
    reason: str | None = None


class RecallClassificationItem(_JudgeModel):
    """One classification in the context_recall output."""

    statement: str
    attributed: int
    reason: str | None = None


class RecallClassificationsOut(_JudgeModel):
    """Output of the context_recall rubric."""

    classifications: tuple[RecallClassificationItem, ...]


@runtime_checkable
class Judge(Protocol):
    """A pluggable LLM judge. The only model seam is the platform gateway."""

    @property
    def judge_model_id(self) -> str: ...

    @property
    def judge_family(self) -> str: ...

    async def judge(
        self,
        rubric: Rubric,
        variables: Mapping[str, str],
        out_model: type[BaseModel],
    ) -> BaseModel: ...


def _strip_fence(text: str) -> str:
    """Strip a single leading/trailing ```json / ``` fence pair if present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    # Drop the opening fence line (``` or ```json).
    lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse(text: str, out_model: type[BaseModel]) -> BaseModel:
    payload = json.loads(_strip_fence(text))
    return out_model.model_validate(payload)


class GatewayJudge:
    """A :class:`Judge` that routes every call through a :class:`ModelGateway`."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        task: str = "eval-judge",
        judge_model_id: str,
        judge_family: str,
        max_output_retries: int = 1,
    ) -> None:
        self._gateway = gateway
        self._task = task
        self._judge_model_id = judge_model_id
        self._judge_family = judge_family
        self._max_output_retries = max_output_retries

    @property
    def judge_model_id(self) -> str:
        return self._judge_model_id

    @property
    def judge_family(self) -> str:
        return self._judge_family

    async def judge(
        self,
        rubric: Rubric,
        variables: Mapping[str, str],
        out_model: type[BaseModel],
    ) -> BaseModel:
        rendered = render(rubric, **dict(variables))
        messages: list[ChatMessage] = [ChatMessage(role="user", content=rendered)]
        last_error: Exception | None = None
        attempts = self._max_output_retries + 1
        for attempt in range(attempts):
            request = CompletionRequest(messages=tuple(messages), task=self._task)
            completion = await self._gateway.complete(request)
            try:
                return _parse(completion.text, out_model)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_error = exc
                if attempt < attempts - 1:
                    messages.append(ChatMessage(role="assistant", content=completion.text))
                    messages.append(ChatMessage(role="user", content=_CORRECTIVE))
        raise JudgeOutputError(
            f"judge output for rubric {rubric.id!r} failed to parse after "
            f"{attempts} attempt(s): {last_error}"
        )


#: The pydantic output model each rubric's judge call is validated against.
RUBRIC_OUT_MODELS: dict[str, type[BaseModel]] = {
    "faithfulness_statements": StatementsOut,
    "faithfulness_verdicts": VerdictsOut,
    "context_precision": PrecisionOut,
    "context_recall": RecallClassificationsOut,
}


def out_model_for(rubric_id: str) -> type[BaseModel]:
    """Return the pydantic output model for ``rubric_id``.

    Raises :class:`EvalConfigError` for a rubric with no registered output model.
    """
    model = RUBRIC_OUT_MODELS.get(rubric_id)
    if model is None:
        raise EvalConfigError(f"no output model registered for rubric {rubric_id!r}")
    return model


def enforce_cross_family(judge_family: str, evaluated_family: str) -> None:
    """Refuse a same-family judge (cross-family judging is mandatory, no v1 override).

    Raises :class:`EvalConfigError` when either family is empty, and
    :class:`CrossFamilyError` when they are equal case-insensitively after strip.
    """
    jf = judge_family.strip()
    ef = evaluated_family.strip()
    if not jf or not ef:
        raise EvalConfigError(
            "cross-family judging requires both judge_family and evaluated_family "
            f"to be set; got judge_family={judge_family!r}, evaluated_family={evaluated_family!r}"
        )
    if jf.casefold() == ef.casefold():
        raise CrossFamilyError(
            f"judge_family {judge_family!r} equals evaluated_family {evaluated_family!r}; "
            "cross-family judging is mandatory"
        )
