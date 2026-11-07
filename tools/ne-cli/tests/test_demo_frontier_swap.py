# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-BUNDLE-GATEWAY-CONFIG: the demoable frontier<->frontier (OpenAI<->
Anthropic) and frontier->open-weight model swap.

Proven at TWO levels:

* env-generation (``live._gateway_lane_env``): exact JSON shape, byte-identical
  default, exact-value (never superset) egress allowlist.
* gateway routing: the REAL litellm ``Router`` (via ``mock_response``), the REAL
  ``EgressPolicy``/``build_egress_guard`` built from the EXACT emitted allowlist,
  and the REAL ``freeze_config_hash`` — the anti-vacuous core is asserting the
  SERVED ``model_id``, never menu contents alone.

Mirrors ``packages/ne-gateway/tests/conftest.py`` patterns INLINE (this package
does not depend on ne-gateway's test package, so nothing is imported from it).
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
from collections.abc import Sequence
from typing import Any

import pytest

from ne.cli.demo import live
from ne.contracts import (
    ChatMessage,
    CompletionRequest,
    freeze_config_hash,
    reset_config_hash_for_testing,
)
from ne.contracts.config import NeSettings
from ne.contracts.governance import RedactionResult
from ne.gateway import (
    AirGapViolationError,
    EgressBlockedError,
    EgressPolicy,
    EgressSettings,
    GatewaySettings,
    LiteLLMGateway,
    ModelDef,
    TaskRoute,
    build_egress_guard,
)
from ne.govern.config import GovernSettings
from ne.govern.factory import create_redactor
from ne.runtime.factory import freeze_platform_config

_MSGS = (ChatMessage(role="user", content="hi"),)

_MODELS_LINE = re.compile(r"NE_GATEWAY_MODELS:\s*'(.+?)'\n")
_TASKS_LINE = re.compile(r"NE_GATEWAY_TASKS:\s*'(.+?)'\n")
_ALLOWLIST_LINE = re.compile(r"NE_EGRESS_ALLOWED_FRONTIER_HOSTS:\s*'(.+?)'\n")


# -- inline test doubles (the ne-gateway conftest.py shapes, not imported) ---


class _RecordingCostSink:
    """A real CostSink implementation that appends emitted events to a list."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


class _PassthroughRedactor:
    """A ``Redactor`` that never redacts anything — the mechanical stub used to
    satisfy the fail-closed ``redactor is not None`` requirement; these tests
    exercise ROUTING, not redaction content."""

    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        return RedactionResult(text=text)


PASSTHROUGH_REDACTOR = _PassthroughRedactor()


class _DemoSettings(NeSettings):
    demo: int = 11


@pytest.fixture(autouse=True)
def _frozen_config() -> Any:
    """Every gateway ``complete()`` call stamps the frozen process config_hash
    onto its CostEvent/AuditEvent (design §6.2/Q5) — freeze a throwaway config
    once per test (mirrors ``packages/ne-gateway/tests/conftest.py``'s
    ``frozen_config`` fixture; not imported, since this package must not depend
    on ne-gateway's test package)."""
    reset_config_hash_for_testing()
    freeze_config_hash(_DemoSettings())
    yield
    reset_config_hash_for_testing()


# -- helpers: parse live._gateway_lane_env's emitted env into real settings --


def _parse_lane_env(
    frontier: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Parse ``live._gateway_lane_env(None, frontier=...)`` back into plain
    Python — the SAME env string ``_write_demo_overlay`` embeds in the overlay,
    so a test built from this is proving the actual demo-emitted config, not a
    hand-rolled proxy of it.

    ``hosts`` is the FULL parsed allowlist list (``[]`` when the line is
    absent) — never just the first element — so callers can assert exact
    cardinality (``== [host]``), not merely that the first entry matches a
    superset."""
    env = live._gateway_lane_env(None, frontier=frontier)
    models_match = _MODELS_LINE.search(env)
    tasks_match = _TASKS_LINE.search(env)
    assert models_match and tasks_match, env
    models = json.loads(models_match.group(1))
    tasks = json.loads(tasks_match.group(1))
    allow_match = _ALLOWLIST_LINE.search(env)
    hosts: list[str] = json.loads(allow_match.group(1)) if allow_match else []
    return models, tasks, hosts


def _settings(
    frontier: str | None, mock_by_name: dict[str, str]
) -> tuple[GatewaySettings, list[str]]:
    """Real ``GatewaySettings`` built from the exact demo-emitted menu, with
    ``mock_response``/``api_key`` injected via ``model_copy`` ONLY for the
    models this test actually calls — the rest of the parsed menu (and its
    JSON key order) stays otherwise exact (NOT a hand-rolled ``make_model``
    fixture)."""
    raw_models, raw_tasks, hosts = _parse_lane_env(frontier)
    models: list[ModelDef] = []
    for raw in raw_models:
        model = ModelDef.model_validate(raw)
        if model.name in mock_by_name:
            model = model.model_copy(
                update={
                    "extra_params": {"api_key": "test", "mock_response": mock_by_name[model.name]}
                }
            )
        models.append(model)
    tasks = {k: TaskRoute(**v) for k, v in raw_tasks.items()}
    return GatewaySettings(models=models, tasks=tasks), hosts


def _request(task: str = "agent") -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task)


# -- env-generation: default byte-parity + exact frontier shape --------------


def test_default_lane_env_unchanged_without_frontier() -> None:
    assert live._gateway_lane_env(None) == live._gateway_lane_env(None, frontier=None)
    env = live._gateway_lane_env(None)
    assert "frontier-default" not in env
    assert "NE_EGRESS_ALLOWED_FRONTIER_HOSTS" not in env


@pytest.mark.parametrize(
    "provider,litellm_model,host,extra_params",
    [
        # NE-FRONTIER-THINKING: anthropic/claude-sonnet-5 runs ADAPTIVE THINKING
        # when the request omits `thinking` and defaults thinking `display` to
        # "omitted" — which returned a thinking block with an EMPTY thinking
        # field and, intermittently, no text block at all (live audit row,
        # 2026-08-26: outcome=error, finish_reason=stop, output_tokens=144,
        # empty_completion_tokens=144). Disabling it explicitly is the only way
        # off, since NOT asking is what selects it.
        #
        # `allowed_openai_params` is REQUIRED alongside, not decoration: litellm
        # gates `thinking` on the model's supports_reasoning, read from the
        # packaged cost map the C3 chokepoint forces, and this id is not in that
        # map (see _FRONTIER_LITELLM_MODEL's own COST-MAP STATUS note). Without
        # it litellm raises UnsupportedParamsError before building a request and
        # EVERY call on this lane fails. Proven end-to-end against the real
        # request body in packages/ne-gateway/tests/test_extra_params_reach_wire.py.
        #
        # Pinned as a LITERAL, not read from live._FRONTIER_MODEL_EXTRA_PARAMS,
        # for the same reason max_output_tokens below is: a silent edit of the
        # table must be a RED test, never a self-fulfilling one.
        (
            "anthropic",
            "anthropic/claude-sonnet-5",
            "api.anthropic.com",
            {
                "extra_params": {
                    "thinking": {"type": "disabled"},
                    "allowed_openai_params": ["thinking"],
                }
            },
        ),
        # openai/gpt-5.5 gets NOTHING: `thinking` is an Anthropic request field,
        # and the table is keyed by MODEL so this row never acquires one.
        ("openai", "openai/gpt-5.5", "api.openai.com", {}),
    ],
)
def test_frontier_menu_appends_and_reroutes_agent_only(
    provider: str, litellm_model: str, host: str, extra_params: dict[str, object]
) -> None:
    default_models, default_tasks, default_hosts = _parse_lane_env(None)
    assert default_hosts == []

    models, tasks, emitted_hosts = _parse_lane_env(provider)

    # The first two entries are EXACT-dict-equal to the default emission.
    assert models[:2] == default_models
    assert len(models) == 3
    # NE-CTX-IN-FORCE (owner-directed 2026-09-01): the declared window is the
    # MODEL'S OWN (`_FRONTIER_CONTEXT_WINDOW`), never the private lane's. It was
    # capped at 32768 here because `refusal_for_selection` used to refuse a model
    # whose input budget was smaller than the task PRIMARY's, and `--frontier`
    # makes frontier-default the primary of `agent`/`agent.meridian-analyst.v2` —
    # so an uncapped declaration made `private-default` unselectable for exactly
    # the tasks whose declared fallback it is. That floor is gone: the runtime
    # trim guard re-derives its budget from the model in force, so a roll onto
    # the private lane is trimmed to the private lane's window.
    assert models[2] == {
        "name": "frontier-default",
        "litellm_model": litellm_model,
        "provider": provider,
        "lane": "frontier-api",
        "context_window": live._FRONTIER_CONTEXT_WINDOW[provider],
        # NE-FRONTIER-OUTPUT: the LIVE answering lane declares its own output
        # reserve. Absent, ModelDef filled min(4096, cw//8) = 4096 — a sixth of
        # what the private lane declares for the SAME task, and the exact budget
        # the private lane's comment records as reproducibly truncating grounded
        # synthesis turns. Pinned as a LITERAL, not as
        # live._FRONTIER_MAX_OUTPUT_TOKENS, so a silent edit of the constant is
        # a RED test rather than a self-fulfilling one.
        "max_output_tokens": 12288,
        **extra_params,
    }
    # THE regression this line now guards: the frontier lane must NOT inherit the
    # private lane's window. On the sealed emission that cap made a 1M model
    # declare 32768 and — after its 12288 reserve — serve 18841 input tokens
    # against the private lane's 27033, i.e. the frontier lane was the NARROWEST
    # lane in the deployment. A real agentic run died `context_exhausted` at
    # 18928 estimated tokens, 87 over.
    assert models[2]["context_window"] > default_models[0]["context_window"]
    # The reserve is still legal against the window ACTUALLY emitted (ModelDef
    # requires max_output_tokens < context_window // 2). This bound used to be
    # the binding one at the capped 32768 (ceiling 16384, which is why the
    # private lane's 24576 could not simply be inherited); at the model's own
    # window it is slack, and it is asserted here so a future re-cap cannot
    # silently produce an unconstructable deployment.
    assert models[2]["max_output_tokens"] < models[2]["context_window"] // 2

    # `agent` keeps NO fallback: it is the swap beat's own task, and a fallback
    # would let a blocked frontier egress serve the private lane while the
    # narration claimed the frontier one.
    assert tasks["agent"] == {"model": "frontier-default"}
    # NE-DEMO-NO-CPU-FALLBACK: the analyst keeps its private fallback (the
    # air-gap beat needs a sovereign lane to land on) and LOSES the cpu tier.
    assert tasks["agent.meridian-analyst.v2"] == {
        "model": "frontier-default",
        "fallbacks": ["private-default"],
    }
    # NE-DEMO-NO-CPU-FALLBACK: these tasks are NO LONGER "unchanged by the
    # swap" — they were, when their second lane was the 0.6B CPU lane, which
    # existed in both branches. Now the second lane is `frontier-default`, which is only
    # CONFIGURED when --frontier is passed, so the swap necessarily changes
    # them. Asserting `== default_tasks[key]` here would be asserting the
    # opposite of the intended behaviour.
    #
    # What is still true, and is the property that mattered all along: they stay
    # PRIVATE-PRIMARY. Customer text is structurally routed to the sovereign
    # lane first; the frontier lane is only ever the degrade.
    frontier_backed = (
        "agent.meridian-hardship-review.v3",
        "triage",
        "reason",
        "skill-authoring",
        "grounding-assist",
        "graph-extract",
    )
    for key in frontier_backed:
        assert tasks[key] == {
            "model": "private-default",
            "fallbacks": ["frontier-default"],
        }, key
        # The primary is what the sealed branch already routed them at — the
        # swap ADDS a degrade lane, it never re-primaries a customer-text task.
        assert tasks[key]["model"] == default_tasks[key]["model"], key
    # `adjudicate` is the one route the swap leaves byte-identical: it is the
    # governed-DENY exhibit at `frontier-best`, and giving it a fallback would
    # turn a demonstrated refusal into a demonstrated bypass.
    assert tasks["adjudicate"] == default_tasks["adjudicate"] == {"model": "frontier-best"}
    # The 0.6B CPU lane is GONE OUTRIGHT (owner-directed): no route names it
    # and the menu no longer defines it, in either branch.
    for key, route in tasks.items():
        assert route["model"] != "cpu-model", key
        assert "cpu-model" not in route.get("fallbacks", []), key
    assert "cpu-model" not in {m["name"] for m in models}

    # Exact value AND exact cardinality (1) — asserting the full parsed list,
    # never just its first element, so an emission of
    # `["api.anthropic.com", "evil.example.com"]` would fail this too.
    assert emitted_hosts == [host]


@pytest.mark.parametrize("gpu_vllm", [None, "http://10.0.0.9:8000/v1"])
def test_frontier_lane_out_budgets_its_private_fallback_and_the_fallback_stays_selectable(
    gpu_vllm: str | None,
) -> None:
    """The INVERSE of the invariant the retired window cap existed to hold.

    Before NE-CTX-IN-FORCE, ``refusal_for_selection`` compared
    ``context_budget_for(...).input_budget`` against the task PRIMARY's, so a
    frontier primary with a bigger budget made ``private-default`` unselectable
    for exactly the tasks that declare it as their fallback — the air-gap beat
    with nothing left to run on. The cap (and the reserve inheritance beside it)
    were the workaround, and their price was a 1M model declaring 32768.

    Both halves are asserted together, because either alone would be
    uninformative: the frontier lane DOES now out-budget its own private
    fallback on both shapes, AND that fallback remains selectable. Nothing is
    unprotected — a roll onto the narrower lane is trimmed to THAT lane's window
    by ``GatewayModel._turn_budget``, and the gateway's per-candidate pre-flight
    still skips a candidate the estimate cannot fit.
    """
    env = live._gateway_lane_env(gpu_vllm, frontier="anthropic")
    models_match = _MODELS_LINE.search(env)
    assert models_match, env
    models = [ModelDef.model_validate(m) for m in json.loads(models_match.group(1))]
    settings = GatewaySettings(
        models=models,
        tasks={"agent": TaskRoute(model="frontier-default", fallbacks=("private-default",))},
    )
    by_name = {m.name: m for m in models}
    private_budget = settings.context_budget_for(by_name["private-default"]).input_budget
    frontier_budget = settings.context_budget_for(by_name["frontier-default"]).input_budget
    assert frontier_budget > private_budget, (frontier_budget, private_budget)

    from ne.api.model_routing import refusal_for_selection

    assert refusal_for_selection(settings, "agent", "private-default") is None


def test_frontier_window_is_the_models_own_on_the_gpu_lane() -> None:
    """The uncapping BINDS on the GPU lane too: 1M is the model's window, and it
    is emitted even though the private lane beside it serves 262144."""
    env = live._gateway_lane_env("http://10.0.0.9:8000/v1", frontier="anthropic")
    models_match = _MODELS_LINE.search(env)
    assert models_match, env
    models = json.loads(models_match.group(1))
    private = next(m for m in models if m["name"] == "private-default")
    frontier = next(m for m in models if m["name"] == "frontier-default")
    assert private["context_window"] == 262144
    assert frontier["context_window"] == 1_000_000
    assert live._FRONTIER_CONTEXT_WINDOW["anthropic"] == 1_000_000
    # The output reserve is UNCHANGED by the uncapping (see the live.py note):
    # its truncation justification stands on its own.
    assert frontier["max_output_tokens"] == private["max_output_tokens"] == 24576


def test_the_two_frontier_lanes_name_different_models() -> None:
    """``frontier-best`` vs ``frontier-default`` is only an exhibit while the
    two lanes resolve to DIFFERENT models. Pinning both at one id (the defect
    this unit fixed) makes "best" claim nothing — and this goes RED."""
    env = live._gateway_lane_env(None, frontier="anthropic")
    models_match = _MODELS_LINE.search(env)
    assert models_match, env
    models = {m["name"]: m for m in json.loads(models_match.group(1))}
    best = models["frontier-best"]
    default = models["frontier-default"]
    assert best["litellm_model"] == "anthropic/claude-fable-5"
    assert default["litellm_model"] == "anthropic/claude-sonnet-5"
    assert best["litellm_model"] != default["litellm_model"]
    # The deny exhibit is a MODEL-IDENTITY change only: it still carries no
    # credential and no api_base of its own, on both emissions.
    for emitted in (live._gateway_lane_env(None), env):
        line = _MODELS_LINE.search(emitted)
        assert line
        exhibit = next(m for m in json.loads(line.group(1)) if m["name"] == "frontier-best")
        assert "api_base" not in exhibit
        assert "extra_params" not in exhibit
        assert "api_key" not in json.dumps(exhibit)


@pytest.mark.parametrize("gpu", [None, "http://10.0.0.9:8000/v1"])
@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_frontier_output_reserve_is_declared_and_legal_on_both_branches(
    gpu: str | None, provider: str
) -> None:
    """NE-FRONTIER-OUTPUT: the LIVE answering lane declares a real output budget,
    and it is legal against the window it ACTUALLY emits — on BOTH branches.

    The bug: ``frontier-default`` declared no ``max_output_tokens``, so
    ``ModelDef`` filled ``min(4096, cw // 8)`` = 4096, while ``private-default``
    — the lane ``--frontier`` REPLACES for the ``agent`` task — declares 24576
    precisely because 4096 reproducibly truncated grounded synthesis turns into
    the ``Exceeded maximum output retries`` death. The demo's primary answering
    lane was running on the configuration that failure is documented against.

    The trap this pins: the fix is NOT inheriting 24576. The declared frontier
    window is CAPPED at the private lane's, which is 32768 on the sealed CPU
    branch, and ``ModelDef`` requires ``max_output_tokens < context_window // 2``
    — so 24576 would fail the deployment AT BOOT there. The reserve must clear
    the bound on the branch with the SMALLER window, and it must still be there
    on the branch with the larger one. Both directions here.

    Mutations: dropping the key -> RED (back at 4096); setting it to the private
    lane's 24576 -> RED on the CPU branch's bound; the ``model_validate`` below
    is the same rule the deployment applies at boot, so a value that would
    refuse to start cannot pass this test either.
    """
    env = live._gateway_lane_env(gpu, frontier=provider)
    models_match = _MODELS_LINE.search(env)
    assert models_match, env
    raw_models = json.loads(models_match.group(1))
    raw = next(m for m in raw_models if m["name"] == "frontier-default")

    # DECLARED, not inherited from ModelDef's fallback, and never BELOW the
    # measured floor. Asserted as `>=` rather than `== 12288` because the emitter
    # keeps taking the max with the private lane's reserve, which raises it to
    # 24576 on the GPU branch. That max no longer has an ordering justification
    # (NE-CTX-IN-FORCE retired the selection floor); it is kept because the value
    # it produces is measured and unchanged. The literal 12288 stays below as the
    # truncation floor, which is the part this unit owns.
    assert raw["max_output_tokens"] >= 12288
    assert raw["max_output_tokens"] != 4096, (
        "the ModelDef fallback is the value this unit exists to stop using"
    )

    # LEGAL: the same ModelDef validation the deployment runs at boot, against
    # the window this branch actually emits (the model's own, since
    # NE-CTX-IN-FORCE).
    model = ModelDef.model_validate(raw)
    assert model.max_output_tokens < model.context_window // 2

    # And it still leaves a positive input budget (ContextBudget's own rule,
    # which GatewaySettings raises as a GatewayConfigError at boot).
    settings = GatewaySettings(
        models=[ModelDef.model_validate(m) for m in raw_models],
        tasks={k: TaskRoute(**v) for k, v in json.loads(_TASKS_LINE.search(env).group(1)).items()},  # type: ignore[union-attr]
    )
    assert settings.context_budget_for(model).input_budget > 0

    # Every declared fallback of the re-primaried task stays selectable, and
    # since NE-CTX-IN-FORCE that holds UNCONDITIONALLY rather than only for
    # fallbacks that clear a budget floor — `refusal_for_selection` has no budget
    # arm left, so the output reserve chosen above can no longer make a lane
    # unselectable in either direction.
    from ne.api.model_routing import refusal_for_selection

    for fallback in settings.tasks["agent.meridian-analyst.v2"].fallbacks:
        assert refusal_for_selection(settings, "agent.meridian-analyst.v2", fallback) is None, (
            fallback
        )


def test_frontier_staging_enables_the_governed_warm_call() -> None:
    """A staged ``--frontier`` MUST also turn the model-selection warm on.

    Without ``NE_API_FRONTIER_WARM_ENABLED`` the appended frontier model can
    never earn the <60s ``ne.gateway.model.observed`` record
    ``ModelSelectionService`` requires, so the swap is config-only: absent from
    the Ask picker, and silently demoted onto the private fallbacks on
    ``agent.meridian-analyst.v2``. Deleting the emission -> RED.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        workdir = pathlib.Path(tmp)
        seed = workdir / "seed"
        seed.mkdir()
        default_overlay = live._write_demo_overlay(workdir, seed).read_text(encoding="utf-8")
        assert "NE_API_FRONTIER_WARM_ENABLED" not in default_overlay
        assert "NE_GOVERN_REDACTOR" not in default_overlay

        staging = live._FrontierStaging(
            provider="anthropic", env_file=(workdir / "overlay" / "frontier.env").resolve()
        )
        staged_overlay = live._write_demo_overlay(workdir, seed, frontier=staging).read_text(
            encoding="utf-8"
        )
    assert 'NE_API_FRONTIER_WARM_ENABLED: "true"' in staged_overlay
    # The mandatory pre-egress redaction override must survive alongside it.
    assert 'NE_GOVERN_REDACTOR: "regex"' in staged_overlay


# -- gateway routing: the anti-vacuous core -----------------------------------


def test_agent_turn_routes_to_A_then_B() -> None:
    for provider, host in (("anthropic", "api.anthropic.com"), ("openai", "api.openai.com")):
        default_settings, _ = _settings(None, {"private-default": "hello-private"})
        default_gw = LiteLLMGateway(
            default_settings,
            _RecordingCostSink(),
            egress_guard=EgressPolicy(EgressSettings(), host_resolver=lambda _host: ("10.77.0.2",)),
            redactor=PASSTHROUGH_REDACTOR,
        )
        pre = asyncio.run(default_gw.complete(_request()))
        assert pre.model_id == "private-default"

        swapped_settings, emitted_hosts = _settings(
            provider, {"frontier-default": "hello-frontier"}
        )
        assert emitted_hosts == [host]
        swapped_gw = LiteLLMGateway(
            swapped_settings,
            _RecordingCostSink(),
            egress_guard=build_egress_guard(
                EgressSettings(allowed_frontier_hosts=tuple(emitted_hosts))
            ),
            redactor=PASSTHROUGH_REDACTOR,
        )
        post = asyncio.run(swapped_gw.complete(_request()))
        assert post.model_id == "frontier-default"


def test_air_gap_structurally_kills_frontier_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    swapped_settings, hosts = _settings("anthropic", {"frontier-default": "hello-frontier"})
    assert hosts == ["api.anthropic.com"]
    swapped_gw = LiteLLMGateway(
        swapped_settings,
        _RecordingCostSink(),
        egress_guard=build_egress_guard(EgressSettings(allowed_frontier_hosts=tuple(hosts))),
        redactor=PASSTHROUGH_REDACTOR,
    )
    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(AirGapViolationError):
        asyncio.run(swapped_gw.complete(_request()))

    # CONTROL: the identical AIR_GAP env with the frontier=None (open-weight)
    # settings still completes on the private lane — AIR_GAP kills ONLY the
    # frontier lane, the open-weight lane survives it.
    default_settings, _ = _settings(None, {"private-default": "hello-private"})
    default_gw = LiteLLMGateway(
        default_settings,
        _RecordingCostSink(),
        egress_guard=EgressPolicy(EgressSettings(), host_resolver=lambda _host: ("10.77.0.2",)),
        redactor=PASSTHROUGH_REDACTOR,
    )
    result = asyncio.run(default_gw.complete(_request()))
    assert result.model_id == "private-default"


def test_missing_allowlist_blocks_frontier() -> None:
    """MUTATION control: dropping the emitted allowlist line (here, simply never
    honoring it — an empty EgressSettings) must still block — proves the
    overlay's NE_EGRESS_ALLOWED_FRONTIER_HOSTS line is load-bearing, not
    decoration."""
    swapped_settings, hosts = _settings("anthropic", {"frontier-default": "hello-frontier"})
    assert hosts == ["api.anthropic.com"]
    swapped_gw = LiteLLMGateway(
        swapped_settings,
        _RecordingCostSink(),
        egress_guard=build_egress_guard(EgressSettings()),  # allowlist NOT applied
        redactor=PASSTHROUGH_REDACTOR,
    )
    with pytest.raises(EgressBlockedError) as excinfo:
        asyncio.run(swapped_gw.complete(_request()))
    assert hosts[0] in str(excinfo.value)


def test_swap_changes_config_hash() -> None:
    """Argus round-2 P3: proves the ARTIFACT ``create_app`` actually stamps on
    every ``AuditEvent`` — the COMPOSED platform hash
    (``ne.runtime.factory.freeze_platform_config``, ``extra_modules={"ne-gateway":
    ...}`` — the EXACT composition ``packages/ne-api/src/ne/api/platform.py``
    performs — see platform.py:790-857), not the NE-033 single-module subset
    (``freeze_config_hash``). A differing ``ne-gateway`` module serialization
    forces a differing composed digest, so this makes the docs' 'a swap changes
    config_hash' claim self-proving rather than a proxy assertion."""
    default_settings, _ = _settings(None, {})
    swapped_settings, _ = _settings("anthropic", {})

    reset_config_hash_for_testing()
    hash_default = freeze_platform_config(extra_modules={"ne-gateway": default_settings})
    reset_config_hash_for_testing()
    hash_swapped = freeze_platform_config(extra_modules={"ne-gateway": swapped_settings})
    reset_config_hash_for_testing()
    assert hash_default != hash_swapped

    # Control: identical settings hash identically.
    default_settings_2, _ = _settings(None, {})
    reset_config_hash_for_testing()
    hash_default_again = freeze_platform_config(extra_modules={"ne-gateway": default_settings_2})
    reset_config_hash_for_testing()
    assert hash_default == hash_default_again


def test_swap_changes_composed_config_hash_including_redactor() -> None:
    """The frontier swap ALSO changes ``NE_GOVERN_REDACTOR`` (regex, while
    ``--frontier`` is staged) — that choice rides ``GovernSettings.redactor``,
    which ``freeze_platform_config`` freezes directly (not via
    ``extra_modules``), so the composed hash moves on the redactor choice too,
    independent of the gateway menu."""
    reset_config_hash_for_testing()
    hash_presidio = freeze_platform_config(govern_settings=GovernSettings(redactor="presidio"))
    reset_config_hash_for_testing()
    hash_regex = freeze_platform_config(govern_settings=GovernSettings(redactor="regex"))
    reset_config_hash_for_testing()
    assert hash_presidio != hash_regex

    # Control: identical GovernSettings hash identically.
    reset_config_hash_for_testing()
    hash_regex_again = freeze_platform_config(govern_settings=GovernSettings(redactor="regex"))
    reset_config_hash_for_testing()
    assert hash_regex == hash_regex_again


# -- redaction backend swap (Argus round-2 P1) --------------------------------


def test_regex_redactor_unblocks_frontier_lane_without_presidio_sidecar() -> None:
    """Argus round-2 P1: the shipped ``ne-app`` runtime image carries no isolated
    Presidio sidecar venv (``packages/ne-govern/presidio_env`` lives outside
    ``src/``), so the frontier lane's mandatory pre-egress redaction
    (``gateway._prepare_egress_body``, NE-162) would fail the first ``agent``
    turn CLOSED under the default ``presidio`` backend. ``_write_demo_overlay``
    now emits ``NE_GOVERN_REDACTOR: "regex"`` while ``--frontier`` is staged —
    this proves the EXACT construction that selection drives
    (``ne.govern.factory.create_redactor``, resolving the ``regex``
    ``ne.plugins.redactor`` entry point) builds and successfully redacts with
    NO Presidio/sidecar-venv dependency, unlike the default backend."""
    redactor = create_redactor(GovernSettings(redactor="regex"))
    result = redactor.redact("contact me at demo@example.com about acct-123")
    assert "demo@example.com" not in result.text
    assert "<REDACTED:EMAIL_ADDRESS>" in result.text

    # The overlay-level gating (NE_GOVERN_REDACTOR present ONLY while --frontier
    # is staged, absent by default) is proven where it is actually emitted —
    # test_demo_live_seams.py::test_frontier_overlay_env_and_env_file /
    # test_overlay_env_parity_and_exact_values — not here (this test proves the
    # BACKEND CONSTRUCTION the overlay selects works without Presidio, which is
    # `_gateway_lane_env`'s output has no knowledge of).


# -- _frontier_posture_note ---------------------------------------------------


def test_frontier_posture_note(tmp_path: Any) -> None:
    workdir = tmp_path / "wd"
    workdir.mkdir()
    assert live._frontier_posture_note(workdir) is None

    (workdir / live._FRONTIER_MARKER).write_text(
        json.dumps({"provider": "anthropic"}), encoding="utf-8"
    )
    note = live._frontier_posture_note(workdir)
    assert note is not None
    assert "anthropic" in note
    assert "api.anthropic.com" in note


def test_adjudicate_still_denied_even_when_its_host_is_allowlisted() -> None:
    """Argus P3: `--frontier anthropic` also un-blocks the deny exhibit at C2.

    `frontier-best` shares provider ``anthropic`` and carries no
    ``api_base``, so ``EgressPolicy._resolve_frontier_host`` derives the SAME
    host that ``--frontier anthropic`` stages into
    ``NE_EGRESS_ALLOWED_FRONTIER_HOSTS``. The C2 layer therefore stops being
    what blocks it — asymmetric with ``--frontier openai``, where adjudicate
    still egress-denies.

    That is SAFE, but only because two upstream gates deny first, and the
    previous state of this unit rested that claim on PROSE plus tests of a
    different layer. This pins it at the demo layer: the emitted overlay must
    keep routing ``adjudicate`` at the deny exhibit (never at the live
    ``frontier-default`` lane), so the C2 allowlist widening cannot silently
    become the thing that would have stopped a real adjudicate call.

    The defence-in-depth behind it (unchanged, verified, not re-asserted here):
    ``allow_frontier_subagent_routing`` defaults False
    (``ne/runtime/dispatch.py:182``) and is never passed True by
    ``platform.py``, so frontier sub-agent routing is refused before egress is
    ever consulted.
    """
    for provider in ("anthropic", "openai"):
        model_list, tasks, _hosts = _parse_lane_env(provider)
        models = {m["name"]: m for m in model_list}

        assert tasks["adjudicate"] == {"model": "frontier-best"}, (
            f"[{provider}] adjudicate must stay routed at the deny exhibit — "
            "routing it at the live frontier lane would turn a governed-DENY "
            "demo beat into a real outbound call"
        )
        # The exhibit must remain key-less, so it cannot service a real call
        # even if something upstream ever let it through.
        assert "api_key" not in models["frontier-best"], (
            f"[{provider}] the deny exhibit must never carry a key"
        )
        # Control: the LIVE lane is present and distinct, so this test is not
        # passing merely because no frontier model exists at all.
        assert "frontier-default" in models, f"[{provider}] live lane missing"
