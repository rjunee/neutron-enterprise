# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTX-IN-FORCE, closed over the EMITTER: no lane is capped at another's window.

``packages/ne-api/tests/test_context_budget_in_force.py`` proves the mechanism
(the trim guard sizes itself to the model in force). This module proves the
DEPLOYMENT the owner actually ran is fixed, by deriving the model set from
:func:`ne.cli.demo.live._gateway_lane_env` — the one function that writes
``NE_GATEWAY_MODELS`` into the demo overlay — rather than restating it.

The measured defect, verbatim from the live box::

    context_exhausted  estimated 18928 · input_budget 18841 · window 32768

``claude-sonnet-5`` serves 1M. It declared 32768 because the emitter capped every
frontier lane at the private lane's window, and with a 12288 output reserve sized
against a 262144 GPU window that left 18841 input tokens — LESS than the CPU
lane's 27033. The frontier lane was the narrowest lane in the deployment.
"""

from __future__ import annotations

import json
import re

import pytest

from ne.cli.demo.live import _gateway_lane_env
from ne.contracts.types import Lane
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute

GPU_ENDPOINT = "http://10.77.0.2:10200/v1"

_MODELS = re.compile(r"NE_GATEWAY_MODELS: '(.*)'")
_TASKS = re.compile(r"NE_GATEWAY_TASKS: '(.*)'")

#: The estimate of the turn that died. Anything at or below the emitted frontier
#: input budget means that same turn now fits.
OWNER_TURN_TOKENS = 18_928


def boot_settings(gpu: str | None, frontier: str | None) -> GatewaySettings:
    env = _gateway_lane_env(gpu, frontier)
    models = _MODELS.search(env)
    tasks = _TASKS.search(env)
    assert models and tasks, env
    return GatewaySettings(
        models=[ModelDef(**m) for m in json.loads(models.group(1))],
        tasks={k: TaskRoute(**v) for k, v in json.loads(tasks.group(1)).items()},
    )


@pytest.mark.parametrize("gpu", [None, GPU_ENDPOINT])
@pytest.mark.parametrize("frontier", [None, "anthropic"])
def test_no_lane_is_capped_at_another_lanes_window(gpu: str | None, frontier: str | None) -> None:
    """The owner's rule, as a property of every emitted shape: "every model should
    always use its maximum available context, no matter if its external or
    internal".

    Asserted as "no frontier lane shares the private lane's window", which is the
    only way the cap could express itself, and it goes RED the moment a ``min(...,
    private_default["context_window"])`` returns to either emission.
    """
    boot = boot_settings(gpu, frontier)
    by_lane = {m.name: m for m in boot.models}
    private = by_lane["private-default"]
    frontier_models = [m for m in boot.models if m.lane is Lane.FRONTIER_API]
    assert frontier_models, "every shape stages at least the frontier-best exhibit"
    for model in frontier_models:
        assert model.context_window == 1_000_000, model.name
        assert model.context_window != private.context_window, model.name


@pytest.mark.parametrize("gpu", [None, GPU_ENDPOINT])
def test_the_owners_turn_fits_every_frontier_lane(gpu: str | None) -> None:
    """THE regression, pinned at the exact number it failed at, on both shapes."""
    boot = boot_settings(gpu, "anthropic")
    for model in (m for m in boot.models if m.lane is Lane.FRONTIER_API):
        assert boot.context_budget_for(model).input_budget > OWNER_TURN_TOKENS, model.name


def test_the_retired_cap_would_still_refuse_that_turn_on_the_sealed_shape() -> None:
    """The CONTROL that keeps the test above from being vacuous.

    Recomputed on the emitter's own arithmetic rather than a remembered literal:
    give each sealed-shape frontier lane the private lane's window back (32768),
    keep its reserve, and the margin that window implies — and the turn no longer
    fits. So the test above was RED before this change and is GREEN after it.

    SEALED SHAPE ONLY, and that is not a convenience: the owner's failure was
    measured on the sealed CPU box, where the private lane serves 32768. On the
    GPU shape the private window is 262144 and the cap never produced a budget
    this small, so asserting it there would be asserting a fiction.
    """
    boot = boot_settings(None, "anthropic")
    private = next(m for m in boot.models if m.name == "private-default")
    assert private.context_window == 32768
    frontier_models = [m for m in boot.models if m.lane is Lane.FRONTIER_API]
    assert frontier_models
    for model in frontier_models:
        capped = model.model_copy(update={"context_window": private.context_window})
        assert boot.context_budget_for(capped).input_budget < OWNER_TURN_TOKENS, model.name


def test_the_frontier_lane_is_no_longer_the_narrowest_lane() -> None:
    """The inversion itself, on the sealed shape where it was measured: 18841
    frontier input tokens against 27033 private ones, on a 1M model."""
    boot = boot_settings(None, "anthropic")
    private = next(m for m in boot.models if m.name == "private-default")
    live_lane = next(m for m in boot.models if m.name == "frontier-default")
    assert boot.context_budget_for(private).input_budget == 27_033
    assert boot.context_budget_for(live_lane).input_budget > (
        boot.context_budget_for(private).input_budget
    )
    # And the output RESERVE is unchanged by the uncapping — only the window
    # moved (see the `_FRONTIER_MAX_OUTPUT_TOKENS` note in live.py).
    assert live_lane.max_output_tokens == 12288
