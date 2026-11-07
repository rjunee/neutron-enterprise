# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Production ``_default_gateway`` composition-root proofs.

NE-162 wires the REAL fail-closed ``RedactionStage`` (via
``ne.govern.create_redactor``) into every production ``LiteLLMGateway``,
unconditionally. NE-MODEL-ROUTING wires the governed per-call route provider
into that same gateway so a durable repoint takes effect without reconstruction.
This owns both assertions outside the broad ``test_composition.py`` surface.

Construction of ``RedactionStage`` (and its ``PresidioRedactor`` backend) is
boot-safe: BOTH the Presidio isolation venv build AND the env-dir resolution
(the ``uv.lock`` repo-root walk-up / ``NE_GOVERN_PRESIDIO_*`` env var reads,
``presidio_backend.py``) are deferred to the first ``redact()`` call, so this
test never touches the venv, needs no live Presidio, needs no repo checkout,
and never calls ``redact()``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from pytest import MonkeyPatch

from ne.api.platform import (
    AppSeams,
    _default_gateway,
    _redaction_exempt_addresses,
    build_platform,
)
from ne.api.settings import ApiSettings
from ne.audit.config import AuditSettings
from ne.contracts import (
    ChatMessage,
    CompletionRequest,
    Lane,
    freeze_config_hash,
    reset_config_hash_for_testing,
)
from ne.contracts.plugins import PluginRegistry
from ne.events import ensure_outbox_schema
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute
from ne.gateway.egress import EgressSettings
from ne.govern.config import GovernSettings, GuardrailSettings, RecipientSettings
from ne.govern.errors import RedactionConfigError
from ne.govern.redaction.exempt import AllowlistExemptRedactor
from ne.govern.redaction.regex_backend import RegexRedactor
from ne.govern.redaction.stage import RedactionStage
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings

from .conftest import (
    ISSUER,
    FakeTokenVerifier,
    InMemoryFilteredChunkStore,
    PassthroughReranker,
    RegoFaithfulPdpFactory,
    StaticEmbedder,
    StaticSparseEncoder,
    run,
)


def test_default_gateway_reads_live_route_provider_without_reconstruction(
    monkeypatch: MonkeyPatch,
) -> None:
    """The production constructor must not drop the governed route provider."""
    engine = sa.create_engine("sqlite://")
    ensure_outbox_schema(engine)
    settings = GatewaySettings(
        models=[
            ModelDef(
                name="private-a",
                litellm_model="hosted_vllm/a",
                provider="hosted_vllm",
                lane=Lane.PRIVATE_VLLM,
                api_base="http://vllm-a.test/v1",
                context_window=32768,
            ),
            ModelDef(
                name="private-b",
                litellm_model="hosted_vllm/b",
                provider="hosted_vllm",
                lane=Lane.PRIVATE_VLLM,
                api_base="http://vllm-b.test/v1",
                context_window=32768,
            ),
        ],
        tasks={"agent": TaskRoute(model="private-a")},
    )
    override: dict[str, TaskRoute] = {}

    gateway = _default_gateway(
        settings,
        engine,
        None,
        lambda: EgressSettings(),
        GovernSettings(),
        lambda: override,
    )
    # Fake only the network edge; route selection still runs through the real
    # gateway returned by the production composition root.
    gateway._egress_guard = None  # type: ignore[attr-defined]

    async def complete(*, model: str, **_kw: object) -> object:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=model))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            _hidden_params={"response_cost": 0.0},
            model=model,
        )

    monkeypatch.setattr(gateway._router, "acompletion", complete)  # type: ignore[attr-defined]
    request = CompletionRequest(
        messages=(ChatMessage(role="user", content="route me"),),
        task="agent",
    )

    reset_config_hash_for_testing()
    freeze_config_hash(settings)
    try:
        first = asyncio.run(gateway.complete(request))
        override["agent"] = TaskRoute(model="private-b")
        second = asyncio.run(gateway.complete(request))
    finally:
        reset_config_hash_for_testing()

    assert first.model_id == "private-a"
    assert second.model_id == "private-b"


def test_default_gateway_wires_fail_closed_redaction_stage() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_outbox_schema(engine)

    gateway_settings = GatewaySettings(
        models=[
            ModelDef(
                name="stub",
                litellm_model="openai/stub",
                provider="stub",
                lane=Lane.FRONTIER_API,
                context_window=32768,
            )
        ],
        tasks={"draft": TaskRoute(model="stub")},
    )

    boot_egress = EgressSettings()
    gateway = _default_gateway(
        gateway_settings,
        engine,
        None,  # sink: a trivial None audit sink is legal (LiteLLMGateway default)
        lambda: boot_egress,  # NE-AIRGAP-TOGGLE: the guard takes a per-call provider
        GovernSettings(),
    )

    assert isinstance(gateway._redactor, RedactionStage)  # type: ignore[attr-defined]


def test_default_gateway_constructs_without_repo_checkout(monkeypatch: MonkeyPatch) -> None:
    """The literal composition-root regression: ``_default_gateway`` must
    construct without raising even in a production/air-gap install with no
    ``uv.lock`` to walk up to (the CI air-gap boot regression this
    continuation fixes — ``container ne-pilot-ne-api-1 is unhealthy``)."""
    import ne.govern.redaction.presidio_backend as presidio_backend_mod

    monkeypatch.delenv("NE_GOVERN_PRESIDIO_ENV_DIR", raising=False)
    monkeypatch.delenv("NE_GOVERN_PRESIDIO_VENV_DIR", raising=False)

    def _never_found(start: object) -> object:
        raise RuntimeError(f"could not locate repo root (uv.lock) above {start}")

    monkeypatch.setattr(presidio_backend_mod, "_find_repo_root", _never_found)

    engine = sa.create_engine("sqlite://")
    ensure_outbox_schema(engine)

    gateway_settings = GatewaySettings(
        models=[
            ModelDef(
                name="stub",
                litellm_model="openai/stub",
                provider="stub",
                lane=Lane.FRONTIER_API,
                context_window=32768,
            )
        ],
        tasks={"draft": TaskRoute(model="stub")},
    )

    boot_egress = EgressSettings()
    gateway = _default_gateway(
        gateway_settings,
        engine,
        None,
        lambda: boot_egress,
        GovernSettings(),
    )

    assert isinstance(gateway._redactor, RedactionStage)  # type: ignore[attr-defined]


# --- NE-U8-F3/F1: the redaction-exemption composition root -------------------
#
# The exempt addresses come from TWO settings trees, are de-duplicated, and are
# handed to ``create_redactor`` inside ``_default_gateway``. Before this block
# that whole seam had no coverage at all (``git grep exempt`` over ne-api's tests
# returned nothing), so neither the threading nor the F1 shape filter was pinned.

_RECIPIENT = "owner@example.com"
_MAILBOX = "owner@contoso.onmicrosoft.com"
_MAILBOX_OBJECT_ID = "5973b0d2-d572-4bc2-a45f-b491b152e0aa"


def _api(**overrides: object) -> ApiSettings:
    base: dict[str, object] = {
        "qdrant_url": "http://qdrant.test",
        "qdrant_collection": "t",
        "tei_url": "http://tei.test",
    }
    base.update(overrides)
    return ApiSettings(**base)  # type: ignore[arg-type]


def _frontier_settings() -> GatewaySettings:
    return GatewaySettings(
        models=[
            ModelDef(
                name="stub",
                litellm_model="openai/stub",
                provider="stub",
                lane=Lane.FRONTIER_API,
                context_window=32768,
            )
        ],
        tasks={"draft": TaskRoute(model="stub")},
    )


def _govern(*recipients: str) -> GovernSettings:
    return GovernSettings(
        guardrails=GuardrailSettings(recipients=RecipientSettings(allowed_recipients=recipients))
    )


def test_exempt_addresses_thread_from_both_settings_trees() -> None:
    addresses = _redaction_exempt_addresses(
        _api(email_draft_allowed_mailboxes=(_MAILBOX,)),
        _govern(_RECIPIENT),
    )
    assert set(addresses) == {_RECIPIENT, _MAILBOX}


def test_overlapping_sources_are_deduplicated_case_insensitively() -> None:
    """The demo mails FROM and TO the same address; one pattern, not two."""
    addresses = _redaction_exempt_addresses(
        _api(email_draft_allowed_mailboxes=(_RECIPIENT.upper(),)),
        _govern(_RECIPIENT),
    )
    assert addresses == (_RECIPIENT,)


def test_empty_configuration_exempts_nothing() -> None:
    assert _redaction_exempt_addresses(_api(), _govern()) == ()


def test_a_non_address_mailbox_identity_is_dropped_not_exempted() -> None:
    """A Graph object id is a LEGAL mailbox and an ILLEGAL exemption.

    ``email_draft_allowed_mailboxes`` is a mailbox IDENTITY (Graph accepts a UPN
    or an object id, and ``_AllowedDraftMailClient`` compares it as an opaque
    casefolded string), so it cannot be tightened to address-shape at its origin
    the way ``allowed_recipients`` was. The split happens here: the object id is
    dropped, the real address is kept, and nothing becomes a substring wildcard.
    """
    addresses = _redaction_exempt_addresses(
        _api(email_draft_allowed_mailboxes=(_MAILBOX, _MAILBOX_OBJECT_ID)),
        _govern(_RECIPIENT),
    )
    assert set(addresses) == {_RECIPIENT, _MAILBOX}
    assert _MAILBOX_OBJECT_ID not in addresses


def test_the_object_id_deployment_still_builds_a_gateway() -> None:
    """The reason the drop is a drop and not a boot refusal: an object-id-only
    mailbox deployment is legitimate and must still start (with no exemption)."""
    engine = sa.create_engine("sqlite://")
    ensure_outbox_schema(engine)
    api_settings = _api(email_draft_allowed_mailboxes=(_MAILBOX_OBJECT_ID,))
    govern_settings = _govern()

    gateway = _default_gateway(
        _frontier_settings(),
        engine,
        None,
        lambda: EgressSettings(),
        govern_settings,
        redaction_exempt_addresses=_redaction_exempt_addresses(api_settings, govern_settings),
    )

    assert isinstance(gateway._redactor, RedactionStage)  # type: ignore[attr-defined]


def test_default_gateway_delivers_the_exempt_redactor_with_those_addresses() -> None:
    """End of the thread: the addresses reach the gateway's ONE pre-egress redactor."""
    engine = sa.create_engine("sqlite://")
    ensure_outbox_schema(engine)
    api_settings = _api(email_draft_allowed_mailboxes=(_MAILBOX,))
    govern_settings = _govern(_RECIPIENT)

    gateway = _default_gateway(
        _frontier_settings(),
        engine,
        None,
        lambda: EgressSettings(),
        govern_settings,
        redaction_exempt_addresses=_redaction_exempt_addresses(api_settings, govern_settings),
    )

    redactor = gateway._redactor  # type: ignore[attr-defined]
    assert isinstance(redactor, AllowlistExemptRedactor)
    reference = AllowlistExemptRedactor(RedactionStage(RegexRedactor()), (_RECIPIENT, _MAILBOX))
    assert {p.pattern for p in redactor._patterns} == {p.pattern for p in reference._patterns}


def test_a_bare_domain_recipient_cannot_reach_the_exemption() -> None:
    """F1 defence in depth, at the boundary rather than the origin.

    ``RecipientSettings`` now refuses a bare domain outright, so this constructs
    the settings through ``model_construct`` to model a pre-fix / hand-built tree
    and proves the exemption boundary ALSO refuses it — the platform fails to
    boot instead of un-masking ``attacker@example.com`` to the frontier provider.
    """
    engine = sa.create_engine("sqlite://")
    ensure_outbox_schema(engine)
    govern_settings = GovernSettings(
        guardrails=GuardrailSettings(
            recipients=RecipientSettings.model_construct(allowed_recipients=("example.com",))
        )
    )
    addresses = _redaction_exempt_addresses(_api(), govern_settings)
    assert addresses == ("example.com",)

    with pytest.raises(RedactionConfigError):
        _default_gateway(
            _frontier_settings(),
            engine,
            None,
            lambda: EgressSettings(),
            govern_settings,
            redaction_exempt_addresses=addresses,
        )


# --- NE-U8-P2: the PRODUCTION composition, not a hand-assembled one ----------
#
# Everything above either calls ``_redaction_exempt_addresses`` on its own or
# hands its result to ``_default_gateway`` BY HAND. None of it executes
# ``platform.py``'s own ``redaction_exempt_addresses=...`` argument, so replacing
# that argument with ``()`` would silently disable the exemption in production
# while every test above stayed green — the V6 shape in
# ``docs/testing/vacuous-guards.md`` ("in production composition" claimed, "a
# test-local composition" checked).
#
# This test closes that gap the only way it can be closed: it runs
# ``build_platform`` with ``seams.gateway=None`` — the branch production takes —
# and then asserts on the bytes that actually reach the litellm edge. It is an
# OUTCOME test, not an attribute assertion: the exempt address must survive into
# the frontier-bound body and a non-exempt one must not. Neutering the argument
# at ``platform.py`` turns it RED (mutation-verified; see the commit message).


def _platform_seams(tmp_path: Path, *, key: str, govern_settings: GovernSettings) -> AppSeams:
    """Production-shaped seams with the GATEWAY LEFT UNSET.

    ``gateway=None`` is the whole point: it is the only way ``build_platform``
    reaches the ``_default_gateway`` branch that carries the exemption wiring.
    Every other seam fakes a network edge only — the govern/redaction spine is
    real, with the regex backend selected so redaction stays in-process (the
    presidio default would launch a subprocess against a ~560 MB venv).
    """
    registry = PluginRegistry()
    verifier = FakeTokenVerifier()
    registry.register("token_verifier", "fake", lambda oidc: verifier)
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    return AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
            principal_claims=("email", "preferred_username", "name", "department"),
        ),
        runtime_settings=RuntimeSettings(session_database_url=f"sqlite:///{tmp_path}/rt-{key}.db"),
        audit_settings=AuditSettings(database_url=f"sqlite:///{tmp_path}/au-{key}.db"),
        permissions_settings=PermissionsSettings(database_url=f"sqlite:///{tmp_path}/pm-{key}.db"),
        govern_settings=govern_settings,
        gateway_settings=GatewaySettings(
            models=[
                ModelDef(
                    name="stub",
                    litellm_model="openai/stub",
                    provider="stub",
                    lane=Lane.FRONTIER_API,
                    context_window=32768,
                )
            ],
            tasks={"agent": TaskRoute(model="stub")},
        ),
        gateway=None,
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=InMemoryFilteredChunkStore(),
        reranker=PassthroughReranker(),
        plugin_registry=registry,
    )


def _frontier_body_through_production_platform(
    tmp_path: Path, monkeypatch: MonkeyPatch, key: str, prompt: str
) -> str:
    """Compose via ``build_platform``, drive one FRONTIER_API completion, return
    the JSON body handed to the litellm edge."""
    govern_settings = GovernSettings(
        redactor="regex",
        guardrails=GuardrailSettings(
            recipients=RecipientSettings(allowed_recipients=(_RECIPIENT,))
        ),
    )
    seams = _platform_seams(tmp_path, key=key, govern_settings=govern_settings)
    api_settings = _api(email_draft_allowed_mailboxes=(_MAILBOX,))

    reset_config_hash_for_testing()
    platform = build_platform(api_settings, seams)
    try:
        gateway = platform.gateway
        # Fake ONLY the network edge; the composed redactor is the real one.
        gateway._egress_guard = None  # type: ignore[attr-defined]
        captured: dict[str, object] = {}

        async def _acompletion(*, model: str, messages: object, **_kw: object) -> object:
            captured["messages"] = messages
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                _hidden_params={"response_cost": 0.0},
                model=model,
            )

        monkeypatch.setattr(gateway._router, "acompletion", _acompletion)  # type: ignore[attr-defined]
        run(
            gateway.complete(
                CompletionRequest(
                    messages=(ChatMessage(role="user", content=prompt),), task="agent"
                )
            )
        )
        assert "messages" in captured, "the completion never reached the litellm edge"
        return json.dumps(captured["messages"], default=str)
    finally:
        for engine in platform.engines:
            engine.dispose()
        reset_config_hash_for_testing()


def test_production_build_platform_exempts_the_configured_addresses_at_frontier_egress(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """The composed platform's OWN redactor, observed at the litellm boundary.

    RED CONDITION: replace ``redaction_exempt_addresses=_redaction_exempt_addresses(...)``
    at the ``_default_gateway`` call in ``platform.py`` with ``()`` and this fails —
    ``owner@example.com`` comes back as ``<REDACTED:EMAIL_ADDRESS>``, which is exactly
    the S5 breakage NE-CS-USABLE-ADDRESS existed to fix.
    """
    body = _frontier_body_through_production_platform(
        tmp_path,
        monkeypatch,
        "exempt",
        f"Draft from {_MAILBOX} to {_RECIPIENT}, copying victim@example.com.",
    )

    # Both configured addresses survive: the allowlisted recipient (ne-govern's
    # tree) and the configured draft mailbox (ne-api's tree). One source alone
    # going missing must not be mistakable for success.
    assert _RECIPIENT in body
    assert _MAILBOX in body
    # And the exemption is still exactly that narrow.
    assert "victim@example.com" not in body
    assert "<REDACTED:EMAIL_ADDRESS>" in body


def test_production_build_platform_still_masks_a_superstring_at_frontier_egress(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """The boundary guard survives the production composition too."""
    body = _frontier_body_through_production_platform(
        tmp_path, monkeypatch, "superstring", "Mail attacker.owner@example.com.evil.com now."
    )

    assert "attacker.owner@example.com.evil.com" not in body
    assert "<REDACTED:EMAIL_ADDRESS>" in body
