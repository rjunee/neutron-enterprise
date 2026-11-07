# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-162b composition-root proof: ``compose_gateway`` wires the REAL fail-closed
``RedactionStage`` (via ``ne.govern.create_redactor``) into the llm-facade's
``LiteLLMGateway`` — the second frontier-gateway construction site closed by
this unit (mirrors ``packages/ne-api/tests/test_gateway_redaction_wiring.py``).

Construction of ``RedactionStage`` (and its ``PresidioRedactor`` backend) is
boot-safe: BOTH the Presidio isolation venv build AND the env-dir resolution
(the ``uv.lock`` repo-root walk-up / ``NE_GOVERN_PRESIDIO_*`` env var reads,
``presidio_backend.py``) are deferred to the first ``redact()`` call, so this
test never touches the venv, needs no live Presidio, needs no repo checkout,
and never calls ``redact()``.
"""

from __future__ import annotations

import sqlalchemy as sa
from pytest import MonkeyPatch

from ne.govern.redaction.stage import RedactionStage
from ne.memory_graph.cli import compose_gateway

_STUB_MODELS = (
    '[{"name":"stub","litellm_model":"openai/stub","provider":"stub",'
    '"lane":"frontier-api","context_window":32768}]'
)


def test_compose_gateway_wires_fail_closed_redaction_stage(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("NE_GATEWAY_MODELS", _STUB_MODELS)

    gateway = compose_gateway(sa.create_engine("sqlite://"))

    assert isinstance(gateway._redactor, RedactionStage)  # type: ignore[attr-defined]


def test_compose_gateway_constructs_without_repo_checkout(monkeypatch: MonkeyPatch) -> None:
    """The literal composition-root regression: ``compose_gateway`` must
    construct without raising even in a production/air-gap install with no
    ``uv.lock`` to walk up to (the NE-162 air-gap boot regression precedent)."""
    import ne.govern.redaction.presidio_backend as presidio_backend_mod

    monkeypatch.setenv("NE_GATEWAY_MODELS", _STUB_MODELS)
    monkeypatch.delenv("NE_GOVERN_PRESIDIO_ENV_DIR", raising=False)
    monkeypatch.delenv("NE_GOVERN_PRESIDIO_VENV_DIR", raising=False)

    def _never_found(start: object) -> object:
        raise RuntimeError(f"could not locate repo root (uv.lock) above {start}")

    monkeypatch.setattr(presidio_backend_mod, "_find_repo_root", _never_found)

    gateway = compose_gateway(sa.create_engine("sqlite://"))

    assert isinstance(gateway._redactor, RedactionStage)  # type: ignore[attr-defined]
