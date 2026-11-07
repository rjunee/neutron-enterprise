# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Entry-point resolution for ne-eval's pluggable suite + judge (mirrors ne-brain)."""

from __future__ import annotations

from ne.contracts import PluginRegistry


def test_resolve_eval_suite_rag() -> None:
    from ne.eval.suite import RagEvalSuite

    assert PluginRegistry().resolve("eval_suite", "rag") is RagEvalSuite


def test_resolve_eval_judge_gateway() -> None:
    from ne.eval.judge import GatewayJudge

    assert PluginRegistry().resolve("eval_judge", "gateway") is GatewayJudge
