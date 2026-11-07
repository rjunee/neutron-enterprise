# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Layered settings for ne-govern — the PDP adapter and the C12 guardrail layer.

The guardrail settings (spend caps, rate limits, recipient/egress allowlists,
per-tool arg policies) and the redactor selection are all declared here as
``extra='forbid'`` pydantic models so a typo in ``ne.toml`` fails loudly rather
than silently disabling a control. Monetary values are carried as **strings**
(parsed to :class:`~decimal.Decimal` at check time) so the frozen config hash
never depends on float representation.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings
from ne.govern.emails import is_valid_email
from ne.govern.errors import GuardrailConfigError


class _Strict(BaseModel):
    """Base for guardrail settings blocks: unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")


def _validate_cap_str(key: str, raw: str | None) -> None:
    """Reject a cap value that would silently become 'uncapped' (C12 fail closed).

    ``None`` (the key genuinely absent from config) is the ONLY value that means
    'no cap applies' — that is a legitimate, documented default. Anything else
    must parse as a finite Decimal: a typo'd cap string, or a NaN/Infinity value
    (which can never be exceeded and is therefore *effectively* uncapped too),
    is rejected HERE, at construction, naming the exact offending key — never
    silently dropped into 'no cap' at check time.
    """
    if raw is None:
        return
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise GuardrailConfigError(f"{key} = {raw!r} is not a valid decimal spend cap") from exc
    if not parsed.is_finite():
        raise GuardrailConfigError(
            f"{key} = {raw!r} is not a finite spend cap (NaN/Infinity can never be exceeded)"
        )


class SpendCapSettings(_Strict):
    """Per-principal / per-department spend ceilings (USD, Decimal-as-string)."""

    #: Fallback ceiling applied to every principal when no specific cap matches.
    default_cap_usd: str | None = None
    #: Explicit per-principal ceilings keyed by ``principal.id``.
    principal_caps_usd: dict[str, str] = {}
    #: Explicit per-department ceilings keyed by ``principal.claims['department']``.
    department_caps_usd: dict[str, str] = {}

    @model_validator(mode="after")
    def _validate_caps(self) -> SpendCapSettings:
        _validate_cap_str("guardrails.spend.default_cap_usd", self.default_cap_usd)
        for principal_id, raw in self.principal_caps_usd.items():
            _validate_cap_str(f"guardrails.spend.principal_caps_usd[{principal_id!r}]", raw)
        for department, raw in self.department_caps_usd.items():
            _validate_cap_str(f"guardrails.spend.department_caps_usd[{department!r}]", raw)
        return self


class RateLimitRule(_Strict):
    """A sliding-window rate rule: at most ``max_calls`` per ``window_s`` seconds.

    Fail-closed at construction (mirrors :class:`SpendCapSettings`): a malformed
    rate rule must never silently DISABLE the control it configures (C12
    reject-by-default). A non-positive or non-finite ``window_s`` would silently
    turn rate limiting off — with ``window_s <= 0`` the prune cutoff (``now -
    window_s``) is ``>= now`` so every recorded event is immediately pruned and
    the count is always 0, and ``inf`` never expires while ``NaN`` makes the
    prune comparisons vacuous — so both are rejected here, at construction,
    naming the offending key. ``max_calls == 0`` is DELIBERATELY legal: it blocks
    every call (``count >= 0`` is always true in ``rate.py``), a fail-closed kill
    switch; only a negative ``max_calls`` is rejected.
    """

    max_calls: int
    window_s: float

    @model_validator(mode="after")
    def _validate_rule(self) -> RateLimitRule:
        if self.max_calls < 0:
            raise GuardrailConfigError(
                f"guardrails.rate rule: max_calls = {self.max_calls} must be >= 0"
            )
        if not math.isfinite(self.window_s) or self.window_s <= 0:
            raise GuardrailConfigError(
                f"guardrails.rate rule: window_s = {self.window_s!r} must be a positive, "
                "finite number of seconds (a non-positive window would silently disable "
                "rate limiting)"
            )
        return self


class RateLimitSettings(_Strict):
    """Per-tool rate rules, with an optional default applied to unlisted tools."""

    per_tool: dict[str, RateLimitRule] = {}
    default: RateLimitRule | None = None


class RecipientSettings(_Strict):
    """Outbound action-recipient and egress-host allowlists (deny-by-default)."""

    #: Exact bare email addresses permitted as recipients. ENFORCED, not just
    #: documented — see :meth:`_validate_allowed_recipients`.
    allowed_recipients: tuple[str, ...] = ()
    #: Email domains (part after the final ``@``) permitted as recipients.
    allowed_recipient_domains: tuple[str, ...] = ()
    #: Egress hostnames permitted as outbound targets (exact or ``*.suffix``).
    allowed_egress_hosts: tuple[str, ...] = ()

    @field_validator("allowed_recipients")
    @classmethod
    def _validate_allowed_recipients(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject an entry that is not a FULL bare email address (fail loud at boot).

        The field sits directly beside ``allowed_recipient_domains``, so putting a
        bare domain (``example.com``) in the wrong one is a natural operator slip.
        Inside :class:`~ne.govern.guardrails.recipients.RecipientAllowlistGuardrail`
        such an entry is INERT — ``_check_recipient`` first requires
        ``is_valid_email(recipient)`` and then compares for exact string equality,
        and no valid address can ever equal a bare domain — so the operator gets no
        signal at all from the guardrail. Nothing legitimate is therefore rejected
        by this validator: an entry that fails it could never have matched.

        It is not inert everywhere. ``ne-api``'s composition root feeds this same
        tuple to the pre-egress redaction exemption
        (:class:`~ne.govern.redaction.exempt.AllowlistExemptRedactor`), where the
        entry becomes an ``re.escape``'d SUBSTRING pattern: a bare-domain entry
        would un-mask ``attacker@example.com`` at any token boundary, and a bare
        local-part entry would un-mask ``ryan`` anywhere. A control that is dead
        in one consumer and a wildcard in another must not boot (C12
        reject-by-default, name-the-exact-bad-key).
        """
        for entry in value:
            if not is_valid_email(entry):
                raise GuardrailConfigError(
                    "guardrails.recipients.allowed_recipients: "
                    f"{entry!r} is not a full bare email address (local@domain). "
                    "This field is EXACT-MATCH; a bare domain belongs in "
                    "guardrails.recipients.allowed_recipient_domains."
                )
        return value


class ToolArgKind(StrEnum):
    """The deterministic validation class applied to a single tool argument."""

    text = "text"
    identifier = "identifier"
    path = "path"
    email = "email"
    url = "url"
    number = "number"
    boolean = "boolean"


class ArgSpec(_Strict):
    """The validation contract for one named tool argument."""

    kind: ToolArgKind = ToolArgKind.text
    required: bool = False
    max_length: int = 8192
    #: NE-KG-MULTIKEY. ``None`` (the default, and every pre-existing spec) means the
    #: argument is SCALAR-ONLY: a list value is rejected exactly as it is today. An
    #: int N permits a JSON ARRAY of at most N members, EACH validated against this
    #: same spec's ``kind``/``max_length`` — the cardinality cap is the reason this is
    #: an int and not a bool: a permitted list must always be a BOUNDED list.
    list_max: int | None = None


class ToolArgPolicy(_Strict):
    """The allowlisted argument shape for one tool (reject-by-default)."""

    args: dict[str, ArgSpec] = {}
    max_total_bytes: int = 65536


class GuardrailSettings(_Strict):
    """The full C12 deterministic-guardrail configuration (all defaulted)."""

    spend: SpendCapSettings = SpendCapSettings()
    rate: RateLimitSettings = RateLimitSettings()
    recipients: RecipientSettings = RecipientSettings()
    #: Per-tool argument policies; a tool absent from this map is rejected.
    tools: dict[str, ToolArgPolicy] = {}


class GovernSettings(NeSettings):
    """Layered settings selecting the PDP adapter and configuring guardrails."""

    model_config = SettingsConfigDict(
        env_prefix="NE_GOVERN_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    #: Name of the ``ne.plugins.policy_decision_point`` adapter to construct.
    pdp: str = "opa"
    #: Base URL of the OPA sidecar (ADR-001 "PDP calls (OPA HTTP)").
    opa_url: str = "http://localhost:8181"
    #: OPA data-document path evaluated for each decision (POST /v1/data/<path>).
    decision_path: str = "ne/govern/decision"
    #: Per-request OPA HTTP timeout, seconds.
    timeout_s: float = 5.0

    #: C12 deterministic guardrail configuration (spend/rate/recipients/tools).
    guardrails: GuardrailSettings = GuardrailSettings()
    #: Name of the ``ne.plugins.redactor`` adapter (adopt-first default: Presidio).
    redactor: str = "presidio"
