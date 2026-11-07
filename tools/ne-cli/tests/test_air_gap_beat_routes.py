# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Which task routes block the AIR-GAP BEAT on the SERVING build, derived from
the emitter rather than restated.

``platform.py`` refuses to engage the air gap while ANY effective route's
PRIMARY sits on the frontier lane. The list of such routes is therefore the
list an operator must clear first — and it is not a matter of opinion: it falls
out of :func:`ne.cli.demo.live._gateway_lane_env`, the one function that writes
``NE_GATEWAY_MODELS`` / ``NE_GATEWAY_TASKS`` into the demo overlay.

This module computes that list for both bring-up shapes and asserts what the
product can and cannot do about it. The headline is
:func:`test_adjudicate_is_repointable_on_both_shapes`.

WHY IT PASSES CHANGED UNDER NE-CTX-IN-FORCE (owner-directed, 2026-09-01), and
the change is the whole point rather than a relaxation:

* BEFORE — ``refusal_for_target`` carried an input-budget FLOOR: a target whose
  input budget was below the boot primary's was refused, because the runtime
  trim guard had been derived from that primary once at boot and "could not be
  re-derived live". The only way to keep ``adjudicate`` repointable was to CAP
  ``frontier-best``'s declared window and reserve at the private lane's, which
  is how a 1M-window model came to declare 32768.
* NOW — the trim guard re-derives its budget per request from the model in
  force (``GatewayModel._turn_budget`` over the gateway's ``TurnContextBudget``
  capability), the floor is GONE, and each lane declares its own true window.
  Reachability of the beat therefore no longer rests on a window cap at all;
  it rests on there being an in-VPC model and no budget arm to refuse it.

:func:`test_frontier_best_declares_its_own_window_and_stays_repointable`
replaces the old ``..._is_capped_at_the_private_lane`` test and pins exactly
that: the frontier exhibit out-budgets the private lane by a wide margin AND the
in-VPC repoint is still permitted — the combination the old floor made
impossible.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

from ne.api.model_routing import action_for_lane, refusal_for_target
from ne.cli.demo.live import _gateway_lane_env
from ne.contracts.types import Lane
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute

#: An in-VPC GPU rig endpoint.
GPU_ENDPOINT = "http://10.77.0.2:10200/v1"

_ENV_LINE = re.compile(r"\s*([A-Z0-9_]+): '(.*)'\s*$")


def _overlay_env(gpu_vllm: str | None, frontier: str | None) -> dict[str, str]:
    """The overlay env block ``_gateway_lane_env`` emits, parsed back."""
    out: dict[str, str] = {}
    for line in _gateway_lane_env(gpu_vllm, frontier).splitlines():
        match = _ENV_LINE.match(line)
        if match is not None:
            out[match.group(1)] = match.group(2)
    return out


def boot_settings(gpu_vllm: str | None, frontier: str | None) -> GatewaySettings:
    """The ``GatewaySettings`` the API composes at boot from that env.

    Built the same way ``GatewaySettings()`` builds it in the container — the
    two env JSON blobs and nothing else — so the routes asserted below are the
    routes the running process holds, not a re-description of them.
    """
    env = _overlay_env(gpu_vllm, frontier)
    return GatewaySettings(
        models=[ModelDef(**m) for m in json.loads(env["NE_GATEWAY_MODELS"])],
        tasks={k: TaskRoute(**v) for k, v in json.loads(env["NE_GATEWAY_TASKS"]).items()},
    )


def frontier_primaries(boot: GatewaySettings) -> tuple[str, ...]:
    """The guard's own expression (``platform.py``'s air-gap lambda), applied to
    the boot routes: every task whose PRIMARY model is on the frontier lane."""
    by_name = {m.name: m for m in boot.models}
    return tuple(
        sorted(
            task
            for task, route in boot.tasks.items()
            if by_name[route.model].lane is Lane.FRONTIER_API
        )
    )


def in_vpc_models(boot: GatewaySettings) -> tuple[str, ...]:
    return tuple(sorted(m.name for m in boot.models if m.lane is Lane.PRIVATE_VLLM))


# --- the definitive repoint list ------------------------------------------------


def test_serving_shape_has_exactly_three_frontier_routed_tasks() -> None:
    """``--gpu-vllm <rig> --frontier anthropic``: the shape the customer demo runs."""
    boot = boot_settings(GPU_ENDPOINT, "anthropic")
    assert frontier_primaries(boot) == (
        "adjudicate",
        "agent",
        "agent.meridian-analyst.v2",
    )
    # CONTROL — the other six routed tasks are already in-VPC, so the tuple above
    # is the COMPLETE blocker set rather than a sample of it.
    by_name = {m.name: m for m in boot.models}
    rest = set(boot.tasks) - set(frontier_primaries(boot))
    assert len(rest) == 6
    assert all(by_name[boot.tasks[t].model].lane is Lane.PRIVATE_VLLM for t in rest)
    # And the private lane really is the rig, not a stand-in.
    assert by_name["private-default"].api_base == GPU_ENDPOINT


def test_sealed_shape_still_has_one_frontier_routed_task() -> None:
    """Without ``--frontier`` the swap routes go away — ``adjudicate`` does not.

    So the repoint list is NOT a consequence of staging the frontier lane: even
    the "sealed" bring-up must repoint ``adjudicate`` in-VPC before its air gap
    will engage (and with no budget floor left it legally can).
    """
    boot = boot_settings(GPU_ENDPOINT, None)
    assert frontier_primaries(boot) == ("adjudicate",)
    assert boot.tasks["adjudicate"].model == "frontier-best"


# --- what the product will and will not let you do about it ---------------------


@pytest.mark.parametrize("frontier", [None, "anthropic"])
def test_adjudicate_is_repointable_on_both_shapes(
    frontier: str | None,
) -> None:
    """THE FORMER BLOCKER, cleared — stated as a property of the shipped rule.

    ``refusal_for_target`` is the ONE judgement the console's ``selectable``
    flag, ``POST /v1/models/routing``'s 409 pre-flight and the governed
    binding's ``validate_args`` all read. Since NE-CTX-IN-FORCE it carries no
    input-budget arm at all, so the repoint the air-gap beat needs is legal on
    EVERY shape regardless of how wide a window ``adjudicate``'s primary
    declares.
    """
    boot = boot_settings(GPU_ENDPOINT, frontier)
    targets = in_vpc_models(boot)
    assert targets, "a deployment with no in-VPC model could not host the beat at all"
    for model in targets:
        refusal = refusal_for_target(
            boot, "adjudicate", model, current=boot.tasks["adjudicate"].model
        )
        assert refusal is None, (model, refusal)


@pytest.mark.parametrize("gpu", [None, GPU_ENDPOINT])
def test_frontier_best_declares_its_own_window_and_stays_repointable(gpu: str | None) -> None:
    """NE-CTX-IN-FORCE, stated as the pair of facts that could not coexist before.

    ``frontier-best`` declares the window its model actually serves (1M), so its
    input budget STRICTLY EXCEEDS the private lane's on both branches — the exact
    condition the retired budget floor turned into "no in-VPC target for
    ``adjudicate``, ever". And the repoint is nevertheless permitted, because the
    floor is gone: the trim guard sizes itself to whichever model is in force, so
    routing DOWN to the private lane is trimmed to the private lane's window
    instead of being refused at config time.

    This is the direct inverse of the assertion it replaces
    (``..._is_capped_at_the_private_lane``). If a future change re-introduces a
    config-time budget floor, the second half fails here rather than being
    discovered as a dead air-gap beat on stage.
    """
    boot = boot_settings(gpu, "anthropic")
    by_name = {m.name: m for m in boot.models}
    best = by_name["frontier-best"]
    private = by_name["private-default"]
    assert best.context_window == 1_000_000
    assert best.context_window > private.context_window
    assert (
        boot.context_budget_for(best).input_budget > boot.context_budget_for(private).input_budget
    )
    assert (
        refusal_for_target(
            boot, "adjudicate", "private-default", current=boot.tasks["adjudicate"].model
        )
        is None
    )
    # And the runtime-facing bound still holds (ModelDef would refuse to
    # construct otherwise; asserted for legibility).
    assert best.max_output_tokens < best.context_window // 2


def test_the_other_frontier_routes_are_repointable() -> None:
    """CONTROL for the test above: the rule is not simply refusing everything.

    ``agent`` and ``agent.meridian-analyst.v2`` DO have a permitted in-VPC
    target, and it rides the internal action name — so the beat's step (b) is
    blocked by ``adjudicate`` specifically, not by the repoint rule in general.
    """
    boot = boot_settings(GPU_ENDPOINT, "anthropic")
    by_name = {m.name: m for m in boot.models}
    for task in ("agent", "agent.meridian-analyst.v2"):
        assert (
            refusal_for_target(boot, task, "private-default", current=boot.tasks[task].model)
            is None
        )
    assert (
        action_for_lane(by_name["private-default"].lane) == "write.internal.model.routing.repoint"
    )


def test_engaging_the_gap_would_also_freeze_the_repoint_menu() -> None:
    """Ordering fact the runbook depends on: once the gap IS engaged, no route
    can be pointed BACK at a frontier model even by an approver — the same rule
    refuses a frontier target under an engaged gap. The reversal is therefore
    two governed changes, not one, and both need the Tier-2 gate.
    """
    boot = boot_settings(GPU_ENDPOINT, "anthropic")
    refusal = refusal_for_target(
        boot, "agent", "frontier-default", air_gap=True, current="private-default"
    )
    assert refusal is not None
    assert "air gap is engaged" in refusal
    # CONTROL — with the gap open the same repoint is permitted.
    assert (
        refusal_for_target(
            boot, "agent", "frontier-default", air_gap=False, current="private-default"
        )
        is None
    )


# --- the retired demo-only switch never rides the overlay again -----------------


def test_the_retired_demo_switch_never_reaches_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST_DEMO.md item 20: the NE_DEMO_UNAPPROVED_ROUTE_REPOINT passthrough
    (owner-authorized for the 2026-08-28 demo ONLY) is RETIRED. Even with the
    variable exported in the operator's shell — the exact posture the demo-day
    deploy script left behind — ``ne demo up`` emits an overlay byte-identical
    to the clean-environment one. The approval bypass cannot ride a stale
    deploy shell back into the app env."""
    from ne.cli.demo import live

    seed = live._stage_demo_seed(tmp_path)

    monkeypatch.delenv("NE_DEMO_UNAPPROVED_ROUTE_REPOINT", raising=False)
    clean = live._write_demo_overlay(tmp_path, seed).read_text(encoding="utf-8")
    assert "NE_DEMO_UNAPPROVED_ROUTE_REPOINT" not in clean

    monkeypatch.setenv("NE_DEMO_UNAPPROVED_ROUTE_REPOINT", "1")
    exported = live._write_demo_overlay(tmp_path, seed).read_text(encoding="utf-8")
    assert exported == clean


# --- POST_DEMO item 22: the beat's reachability, pinned as one property ---------


def test_air_gap_stays_reachable_every_frontier_primary_is_repointable() -> None:
    """THE regression this file exists to prevent (POST_DEMO.md item 22): the
    air gap silently becoming unreachable because every frontier primary has run
    out of legal in-VPC repoint targets — green tests and a dead beat. Pinned
    here as the end property on BOTH bring-up shapes: every frontier PRIMARY has
    at least one permitted in-VPC repoint target, so the engage guard's blocker
    set can always be cleared.

    NE-CTX-IN-FORCE changed the THREAT, not the property. The old danger was
    ``frontier-best`` declaring its raw 1M window and pushing ``adjudicate``'s
    input-budget floor to ~945k; that window is now exactly what it declares, and
    it is harmless because there is no floor. The property is asserted here
    directly against ``refusal_for_target`` — the one shipped rule — so it holds
    whatever the emitted windows are, which is strictly stronger than pinning a
    cap.

    The GPU shape additionally pins the numbers the 2026-08-28 rig actually
    served (``--max-model-len 262144``, reserve 24576) and the frontier lane's
    own declared window, so a change to either side is deliberate rather than
    discovered on stage."""
    for frontier in (None, "anthropic"):
        boot = boot_settings(GPU_ENDPOINT, frontier)
        primaries = frontier_primaries(boot)
        assert primaries, "no frontier primary would mean the beat proves nothing"
        for task in primaries:
            repointable = [
                model
                for model in in_vpc_models(boot)
                if refusal_for_target(boot, task, model, current=boot.tasks[task].model) is None
            ]
            assert repointable, (
                f"task {task!r} (frontier={frontier!r}) has NO legal in-VPC repoint "
                "target: the air gap can never be engaged on this shape. The usual "
                "cause is a config-time budget arm returning to refusal_for_target "
                "(POST_DEMO item 22 / NE-CTX-IN-FORCE)."
            )

    gpu_boot = boot_settings(GPU_ENDPOINT, "anthropic")
    best = {m.name: m for m in gpu_boot.models}["frontier-best"]
    private = {m.name: m for m in gpu_boot.models}["private-default"]
    assert (best.context_window, private.context_window) == (1_000_000, 262144)
    assert best.max_output_tokens >= private.max_output_tokens == 24576


# --- anti-drift: the ne-api beat tests copy this table --------------------------


def test_the_api_side_beat_fixture_matches_this_emitter() -> None:
    """``packages/ne-api/tests/test_air_gap_beat.py`` hard-codes the serving task
    table (it cannot import ne-cli). Pin the two together by reading that
    module's literal with ``ast`` — a copy nobody checks is a copy that drifts.
    """
    here = Path(__file__).resolve()
    api_test = here.parents[3] / "packages" / "ne-api" / "tests" / "test_air_gap_beat.py"
    assert api_test.is_file(), api_test
    tree = ast.parse(api_test.read_text())
    literal: Any = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if "SERVING_TASKS" in names and node.value is not None:
                literal = ast.literal_eval(node.value)
    assert literal is not None, "SERVING_TASKS not found in the ne-api beat test"

    emitted = json.loads(_overlay_env(GPU_ENDPOINT, "anthropic")["NE_GATEWAY_TASKS"])
    assert literal == emitted
