# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused, network-free proofs for the Appendix E measured capture."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from ne.cli import main
from ne.cli.demo.appendix_e import (
    MERIDIAN_MULTI_HOP_QUESTION,
    EvidenceError,
    capture_appendix_e,
    compare_captures,
    normalize_api_base,
    read_token_file,
    write_capture,
)

_NOW = datetime(2026, 8, 18, 23, 30, tzinfo=UTC)


def _verification() -> dict[str, Any]:
    return {
        "valid": True,
        "checked": 1,
        "first_break_seq": None,
        "first_break_event_id": None,
        "reason": None,
        "detail": "chain intact",
    }


def _trace_index(*trace_ids: str) -> dict[str, Any]:
    return {
        "traces": [{"trace_id": trace_id} for trace_id in trace_ids],
        "verification": _verification(),
        "truncated": False,
    }


def _trace(
    trace_id: str,
    *,
    requested_model: str = "cheap-alias",
    served_model: str = "served-cheap-model",
    input_tokens: int | None = 17,
    output_tokens: int | None = 0,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "rollup": {},
        "models": [],
        "nodes": [
            {
                "kind": "event",
                "outcome": "success",
                "children": [
                    {
                        "kind": "model_call",
                        "outcome": "success",
                        "model_call": {
                            "model_name": requested_model,
                            "response_model": served_model,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                        },
                        "children": [],
                    },
                    {
                        "kind": "tool",
                        "action": "tool.execute",
                        "outcome": "success",
                        "resource": "tool:brain.search",
                        "children": [],
                    },
                ],
            }
        ],
        "verification": _verification(),
    }


def _sse(*, answer: str = "Measured answer from this run") -> str:
    return (
        'event: tool\ndata: {"type":"tool","name":"brain.search","decision":"allow"}\n\n'
        f"event: token\ndata: {json.dumps({'type': 'token', 'delta': answer})}\n\n"
        f"event: done\ndata: {json.dumps({'type': 'done', 'output': answer})}\n\n"
    )


def _client(
    *,
    phase: str,
    trace_id: str,
    extra_trace_ids: tuple[str, ...] = (),
    served_model: str = "served-cheap-model",
    input_tokens: int | None = 17,
    output_tokens: int | None = 0,
    graph_paths: list[Any] | None = None,
) -> httpx.AsyncClient:
    trace_index_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal trace_index_calls
        path = request.url.path
        expected_token = "ops-token" if "/v1/trace" in path else "reader-token"
        assert request.headers["Authorization"] == f"Bearer {expected_token}"
        if path.endswith("/v1/graph/neighborhood"):
            assert request.url.params["entity"] == "appendix-e-preflight-missing"
            if phase == "t1-only":
                return httpx.Response(404, json={"detail": "graph tier not enabled"})
            return httpx.Response(200, json={"nodes": [], "edges": [], "truncated": False})
        if path.endswith("/v1/brain/search"):
            assert request.read()
            body = json.loads(request.content)
            assert body == {"query": MERIDIAN_MULTI_HOP_QUESTION, "top_k": 10}
            return httpx.Response(
                200,
                json={
                    "chunks": [],
                    "citations": [
                        {
                            "chunk_id": "mer-chunk-1",
                            "document_id": "mer-doc-1",
                            "source_system": "salesforce",
                            "source_ref": "loan/mer-loan-001",
                        }
                    ],
                    "graph_paths": graph_paths
                    if graph_paths is not None
                    else ([{"hops": []}] if phase == "t1-t2" else []),
                    "provisional_source_count": 0,
                },
            )
        if path.endswith("/v1/traces"):
            trace_index_calls += 1
            if trace_index_calls == 1:
                return httpx.Response(200, json=_trace_index("trace-old"))
            return httpx.Response(200, json=_trace_index("trace-old", trace_id, *extra_trace_ids))
        if path.endswith("/v1/chat/sessions"):
            return httpx.Response(
                200, json={"session_id": "session/one", "agent_name": "console-chat"}
            )
        if path.endswith("/v1/chat/sessions/session/one/messages"):
            assert request.url.raw_path.endswith(b"/v1/chat/sessions/session%2Fone/messages")
            return httpx.Response(200, text=_sse())
        if path.endswith(f"/v1/trace/{trace_id}"):
            return httpx.Response(
                200,
                json=_trace(
                    trace_id,
                    served_model=served_model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://ne.test/api/"
    )


def _capture(
    phase: str,
    trace_id: str,
    *,
    rehearsal_id: str = "room-run-1",
    now: datetime = _NOW,
    **client_kwargs: Any,
) -> dict[str, Any]:
    async def drive() -> dict[str, Any]:
        async with _client(phase=phase, trace_id=trace_id, **client_kwargs) as client:
            return await capture_appendix_e(
                client,
                phase=phase,
                rehearsal_id=rehearsal_id,
                expected_model="cheap-alias",
                reader_token="reader-token",
                ops_token="ops-token",
                trace_wait_seconds=0,
                now=now,
            )

    return asyncio.run(drive())


def test_canonical_question_is_the_generated_meridian_b1_question() -> None:
    root = Path(__file__).resolve().parents[3]
    ground_truth = json.loads(
        (root / "tools/meridian/fixtures/meridian-v1/ground_truth.json").read_text()
    )
    assert ground_truth["b1"]["question"] == MERIDIAN_MULTI_HOP_QUESTION


@pytest.mark.parametrize("phase", ["t1-only", "t1-t2"])
def test_capture_records_only_server_evidence_and_preserves_measured_zero(phase: str) -> None:
    captured = _capture(phase, f"trace-{phase}")

    assert captured["question"] == MERIDIAN_MULTI_HOP_QUESTION
    assert captured["requested_model"] == "cheap-alias"
    assert captured["served_model"] == "served-cheap-model"
    assert captured["input_tokens"] == 17
    assert captured["output_tokens"] == 0
    assert captured["model_calls"] == 1
    assert captured["tool_calls"] == ["brain.search"]
    assert captured["tool_transits"] == [{"name": "brain.search", "decision": "allow"}]
    assert captured["answer"] == "Measured answer from this run"
    assert captured["citations"][0]["document_id"] == "mer-doc-1"
    assert captured["graph_path_count"] == (0 if phase == "t1-only" else 1)
    assert captured["evidence_sha256"]


def test_capture_fails_when_trace_delta_is_ambiguous() -> None:
    with pytest.raises(EvidenceError, match="ambiguous: 2 new traces"):
        _capture("t1-only", "trace-new", extra_trace_ids=("trace-unrelated",))


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "missing_field"),
    [(None, 0, "input_tokens"), (0, None, "output_tokens")],
)
def test_capture_fails_when_per_call_token_evidence_is_absent(
    input_tokens: int | None, output_tokens: int | None, missing_field: str
) -> None:
    with pytest.raises(EvidenceError, match=missing_field):
        _capture(
            "t1-only",
            "trace-missing-usage",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def test_capture_fails_when_graph_profile_and_search_evidence_disagree() -> None:
    with pytest.raises(EvidenceError, match="graph-disabled search"):
        _capture("t1-only", "trace-graph-drift", graph_paths=[{"hops": []}])


def test_compare_renders_captured_values_and_never_claims_improvement() -> None:
    t1 = _capture("t1-only", "trace-t1")
    t2 = _capture("t1-t2", "trace-t2")

    rendered = compare_captures(t1, t2, now=_NOW + timedelta(minutes=5))

    assert "| Input tokens | 17 | 17 |" in rendered
    assert "| Output tokens | 0 | 0 |" in rendered
    assert "`trace-t1`" in rendered and "`trace-t2`" in rendered
    assert "Measured answer from this run" in rendered
    assert "does not claim that T2 improved" in rendered


def test_compare_rejects_different_served_models() -> None:
    t1 = _capture("t1-only", "trace-t1")
    t2 = _capture("t1-t2", "trace-t2", served_model="fallback-model")

    with pytest.raises(EvidenceError, match="served_model"):
        compare_captures(t1, t2, now=_NOW + timedelta(minutes=5))


def test_compare_rejects_same_trace_for_both_phases() -> None:
    t1 = _capture("t1-only", "trace-reused")
    t2 = _capture("t1-t2", "trace-reused")

    with pytest.raises(EvidenceError, match="two real runs"):
        compare_captures(t1, t2, now=_NOW + timedelta(minutes=5))


def test_compare_rejects_stale_or_tampered_captures() -> None:
    t1 = _capture("t1-only", "trace-t1")
    t2 = _capture("t1-t2", "trace-t2")
    with pytest.raises(EvidenceError, match="not fresh"):
        compare_captures(t1, t2, now=_NOW + timedelta(hours=2))

    t2["output_tokens"] = 999_999
    with pytest.raises(EvidenceError, match="evidence hash"):
        compare_captures(t1, t2, now=_NOW + timedelta(minutes=5))


def test_https_and_owner_only_token_guards_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(EvidenceError, match="must use https"):
        normalize_api_base("http://demo.test/api")
    assert normalize_api_base("https://demo.test/api") == "https://demo.test/api/"

    token_file = tmp_path / "reader.token"
    token_file.write_text("secret-token\n")
    os.chmod(token_file, 0o644)
    with pytest.raises(EvidenceError, match="chmod 600"):
        read_token_file(token_file)
    os.chmod(token_file, 0o600)
    assert read_token_file(token_file) == "secret-token"


def test_capture_artifact_is_owner_only_and_never_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / "capture.json"
    payload = _capture("t1-only", "trace-t1")
    write_capture(destination, payload)

    assert destination.stat().st_mode & 0o777 == 0o600
    with pytest.raises(EvidenceError, match="refusing to overwrite"):
        write_capture(destination, payload)


def test_cli_exposes_capture_and_compare_without_importing_live_runner() -> None:
    runner = CliRunner()
    group = runner.invoke(main, ["demo", "appendix-e", "--help"])
    capture = runner.invoke(main, ["demo", "appendix-e", "capture", "--help"])
    compare = runner.invoke(main, ["demo", "appendix-e", "compare", "--help"])

    assert group.exit_code == 0, group.output
    assert capture.exit_code == 0, capture.output
    assert compare.exit_code == 0, compare.output
    assert "capture" in group.output and "compare" in group.output
