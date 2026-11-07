# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GUARDIAN #3 (NE-CTXB-2a): every NE_GATEWAY_MODELS producer emits entries that
validate as a ``ModelDef`` (keys ⊆ ModelDef.model_fields, extra='forbid'
respected) and carry the now-REQUIRED ``context_window``.

Sources swept: pilot compose, helm values (ne-api AND ne-worker via the YAML
anchor), ``live._gateway_lane_env`` (both the no-GPU and GPU branches), and
``live._host_sync_gateway_models``. If a NEW producer appears, add it here — the
required-field change stays enforced permanently, not just at merge time.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ne.cli.demo import live
from ne.gateway import ModelDef

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "pilot" / "compose.yaml"
VALUES_FILE = REPO_ROOT / "deploy" / "helm" / "neutron-enterprise" / "values.yaml"

_MODEL_FIELDS = set(ModelDef.model_fields)
_ENV_VAR = re.compile(r"\$\{([^:}]+):-([^}]+)\}")
_MODELS_LINE = re.compile(r"NE_GATEWAY_MODELS:\s*'(.+?)'")


def _assert_entries_valid(entries: list[dict[str, Any]], source: str) -> None:
    assert entries, f"{source}: no model entries found"
    for entry in entries:
        assert set(entry.keys()) <= _MODEL_FIELDS, (
            f"{source}: entry {entry.get('name')!r} has keys outside ModelDef: "
            f"{set(entry.keys()) - _MODEL_FIELDS}"
        )
        assert "context_window" in entry, (
            f"{source}: entry {entry.get('name')!r} is missing the required context_window"
        )
        # extra='forbid' + required-field validation both exercised here.
        ModelDef.model_validate(entry)


def _find_gateway_models(node: Any) -> list[str]:
    """Recursively collect every NE_GATEWAY_MODELS value in a parsed YAML tree."""
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "NE_GATEWAY_MODELS" and isinstance(v, str):
                found.append(v)
            else:
                found.extend(_find_gateway_models(v))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_gateway_models(item))
    return found


def test_pilot_compose_gateway_models_parity() -> None:
    import yaml

    tree = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    raws = _find_gateway_models(tree)
    assert raws, "pilot compose: NE_GATEWAY_MODELS not found"
    for raw in raws:
        substituted = _ENV_VAR.sub(lambda m: m.group(2), raw)
        entries = json.loads(substituted)
        _assert_entries_valid(entries, "pilot compose")


def test_helm_values_gateway_models_parity() -> None:
    import yaml

    values = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))
    for chart in ("ne-api", "ne-worker"):
        entries = values[chart]["gatewayModels"]
        _assert_entries_valid(entries, f"helm values[{chart}]")


def test_live_gateway_lane_env_parity() -> None:
    # NE-BUNDLE-GATEWAY-CONFIG: sweep every (gpu, frontier) combination — a new
    # producer variant (the appended frontier-default entry) must register here
    # too, or a future required-field change could silently skip it.
    for gpu in (None, "https://pod-8000.proxy.runpod.net/v1"):
        for frontier in (None, "anthropic", "openai"):
            env = live._gateway_lane_env(gpu, frontier=frontier)
            match = _MODELS_LINE.search(env)
            assert match, (
                f"live._gateway_lane_env({gpu!r}, frontier={frontier!r}): "
                "NE_GATEWAY_MODELS not emitted"
            )
            entries = json.loads(match.group(1))
            _assert_entries_valid(
                entries, f"live._gateway_lane_env({gpu!r}, frontier={frontier!r})"
            )


def test_live_host_sync_gateway_models_parity() -> None:
    entries = live._host_sync_gateway_models()
    _assert_entries_valid(entries, "live._host_sync_gateway_models")
