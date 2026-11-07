# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Measured Appendix E capture/compare helpers for the Meridian rehearsal.

This module deliberately does not change model routing or the T2 deployment
toggle.  An operator captures one already-running T1-only profile and one
already-running T1+T2 profile.  Comparison is fail-closed: only fresh,
untampered server captures from the same rehearsal and served model render.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import ssl
import stat
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from ne.cli.demo.arc import parse_sse

MERIDIAN_MULTI_HOP_QUESTION = (
    "For DR-4830, which affected Meridian auto loans have an unpaid scheduled payment "
    "due within 14 days and remain eligible for a new hardship offer?"
)

_SCHEMA = "ne.appendix-e.capture.v1"
_PHASES = ("t1-only", "t1-t2")
_GRAPH_PROBE_ENTITY = "appendix-e-preflight-missing"
_MAX_CAPTURE_AGE = timedelta(hours=1)
_ARTIFACT_FIELDS = {
    "schema",
    "rehearsal_id",
    "phase",
    "captured_at",
    "question",
    "expected_model",
    "requested_model",
    "served_model",
    "trace_id",
    "model_calls",
    "input_tokens",
    "output_tokens",
    "tool_calls",
    "tool_transits",
    "answer",
    "citations",
    "graph_path_count",
    "evidence_sha256",
}


class EvidenceError(RuntimeError):
    """The server evidence is incomplete, ambiguous, stale, or inconsistent."""


def normalize_api_base(value: str) -> str:
    """Require a credential-safe HTTPS API base and normalize its trailing slash."""
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https":
        raise EvidenceError("--api-base must use https; bearer tokens are never sent over HTTP")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise EvidenceError("--api-base must be an HTTPS origin/path without userinfo")
    if parsed.query or parsed.fragment:
        raise EvidenceError("--api-base must not contain a query or fragment")
    path = parsed.path.rstrip("/") + "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def read_token_file(path: Path) -> str:
    """Read one bearer token from an owner-only regular file."""
    try:
        info = path.stat()
    except OSError as exc:  # pragma: no cover - platform error text is not stable
        raise EvidenceError(f"cannot stat token file {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise EvidenceError(f"token path is not a regular file: {path}")
    if info.st_mode & 0o077:
        raise EvidenceError(f"token file must be owner-only (chmod 600): {path}")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:  # pragma: no cover - platform error text is not stable
        raise EvidenceError(f"cannot read token file {path}") from exc
    if not token or any(char.isspace() for char in token):
        raise EvidenceError(f"token file must contain exactly one non-whitespace token: {path}")
    return token


def tls_verifier(*, ca_file: Path | None, insecure: bool) -> ssl.SSLContext | bool:
    """Build the explicit TLS verifier used by the later operator-run capture."""
    if ca_file is not None and insecure:
        raise EvidenceError("--ca-file and --insecure are mutually exclusive")
    if insecure:
        return False
    try:
        return ssl.create_default_context(cafile=str(ca_file) if ca_file is not None else None)
    except (OSError, ssl.SSLError) as exc:
        raise EvidenceError("could not load the requested CA file") from exc


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _safe_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "non-JSON response"
    if isinstance(body, Mapping) and isinstance(body.get("detail"), str):
        return str(body["detail"])
    return "unexpected response"


def _expect_json(response: httpx.Response, *, operation: str) -> dict[str, Any]:
    if response.status_code != 200:
        raise EvidenceError(
            f"{operation} returned HTTP {response.status_code}: {_safe_detail(response)}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise EvidenceError(f"{operation} returned non-JSON evidence") from exc
    if not isinstance(body, dict):
        raise EvidenceError(f"{operation} returned a non-object JSON value")
    return body


def _require_list(body: Mapping[str, Any], key: str, *, operation: str) -> list[Any]:
    value = body.get(key)
    if not isinstance(value, list):
        raise EvidenceError(f"{operation} omitted list field {key!r}")
    return value


def _require_nonempty_str(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"server evidence omitted non-empty {field}")
    return value


def _require_count(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceError(f"server evidence omitted non-negative integer {field}")
    return value


def _verification_is_valid(body: Mapping[str, Any], *, operation: str) -> None:
    verification = body.get("verification")
    if not isinstance(verification, Mapping) or verification.get("valid") is not True:
        raise EvidenceError(f"{operation} chain verification is absent or invalid")


def _trace_ids(body: Mapping[str, Any]) -> set[str]:
    _verification_is_valid(body, operation="trace index")
    rows = _require_list(body, "traces", operation="trace index")
    result: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise EvidenceError("trace index contains a non-object row")
        result.add(_require_nonempty_str(row.get("trace_id"), field="trace_id"))
    return result


def _walk_nodes(nodes: Iterable[Any]) -> Iterable[Mapping[str, Any]]:
    for node in nodes:
        if not isinstance(node, Mapping):
            raise EvidenceError("trace contains a non-object node")
        yield node
        children = node.get("children", [])
        if not isinstance(children, list):
            raise EvidenceError("trace node children is not a list")
        yield from _walk_nodes(children)


def _trace_measurements(
    trace: Mapping[str, Any], *, expected_model: str
) -> tuple[str, str, int, int, int, list[str]]:
    _verification_is_valid(trace, operation="captured trace")
    nodes = list(_walk_nodes(_require_list(trace, "nodes", operation="captured trace")))
    model_nodes = [
        node
        for node in nodes
        if node.get("kind") == "model_call" and node.get("outcome") == "success"
    ]
    if not model_nodes:
        raise EvidenceError("captured trace has no successful model call")

    requested: set[str] = set()
    served: set[str] = set()
    input_tokens = 0
    output_tokens = 0
    for node in model_nodes:
        call = node.get("model_call")
        if not isinstance(call, Mapping):
            raise EvidenceError("successful model-call node omitted model_call evidence")
        requested.add(_require_nonempty_str(call.get("model_name"), field="model_name"))
        served.add(_require_nonempty_str(call.get("response_model"), field="response_model"))
        input_tokens += _require_count(call.get("input_tokens"), field="input_tokens")
        output_tokens += _require_count(call.get("output_tokens"), field="output_tokens")

    if requested != {expected_model}:
        raise EvidenceError(
            "captured trace did not exclusively request the pinned model "
            f"{expected_model!r}; observed {sorted(requested)!r}"
        )
    if len(served) != 1:
        raise EvidenceError(f"captured trace served multiple models: {sorted(served)!r}")

    tool_calls: list[str] = []
    for node in nodes:
        if (
            node.get("kind") == "tool"
            and node.get("action") == "tool.execute"
            and node.get("outcome") == "success"
        ):
            resource = _require_nonempty_str(node.get("resource"), field="tool resource")
            tool_calls.append(resource.removeprefix("tool:"))
    return (
        next(iter(requested)),
        next(iter(served)),
        len(model_nodes),
        input_tokens,
        output_tokens,
        tool_calls,
    )


def _parse_chat(body: str) -> tuple[str, list[dict[str, Any]]]:
    parsed = parse_sse(body)
    errors = parsed.of_type("error")
    if errors:
        detail = errors[-1].data.get("detail") if errors[-1].data else None
        raise EvidenceError(f"chat run ended in an error frame: {detail or 'unspecified'}")
    token_frames = parsed.of_type("token")
    done_frames = parsed.of_type("done")
    if len(token_frames) != 1 or len(done_frames) != 1:
        raise EvidenceError("chat run must contain exactly one token and one done frame")
    token_data = token_frames[0].data
    done_data = done_frames[0].data
    if not isinstance(token_data, Mapping) or not isinstance(done_data, Mapping):
        raise EvidenceError("chat terminal frames omitted object payloads")
    token_output = _require_nonempty_str(token_data.get("delta"), field="token output")
    answer = _require_nonempty_str(done_data.get("output"), field="done output")
    if token_output != answer:
        raise EvidenceError("chat token and done outputs disagree")

    transits: list[dict[str, Any]] = []
    for frame in parsed.of_type("tool"):
        if not isinstance(frame.data, Mapping):
            raise EvidenceError("tool transit frame omitted an object payload")
        transits.append(
            {
                "name": _require_nonempty_str(frame.data.get("name"), field="tool name"),
                "decision": _require_nonempty_str(
                    frame.data.get("decision"), field="tool decision"
                ),
            }
        )
    return answer, transits


def _normalize_citations(search: Mapping[str, Any]) -> tuple[list[dict[str, str]], int]:
    citations: list[dict[str, str]] = []
    for raw in _require_list(search, "citations", operation="brain search"):
        if not isinstance(raw, Mapping):
            raise EvidenceError("brain search returned a non-object citation")
        citations.append(
            {
                key: _require_nonempty_str(raw.get(key), field=f"citation {key}")
                for key in ("chunk_id", "document_id", "source_system", "source_ref")
            }
        )
    graph_paths = _require_list(search, "graph_paths", operation="brain search")
    return citations, len(graph_paths)


async def _new_trace_id(
    client: httpx.AsyncClient,
    *,
    ops_headers: Mapping[str, str],
    before: set[str],
    wait_seconds: float,
) -> str:
    deadline = asyncio.get_running_loop().time() + max(wait_seconds, 0.0)
    while True:
        after_body = _expect_json(
            await client.get("v1/traces", params={"limit": 200}, headers=ops_headers),
            operation="post-run trace index",
        )
        delta = _trace_ids(after_body) - before
        if len(delta) == 1:
            return next(iter(delta))
        if len(delta) > 1:
            raise EvidenceError(
                f"trace correlation is ambiguous: {len(delta)} new traces appeared during the run"
            )
        if asyncio.get_running_loop().time() >= deadline:
            raise EvidenceError("no new trace appeared before the capture deadline")
        await asyncio.sleep(min(0.25, max(deadline - asyncio.get_running_loop().time(), 0.0)))


def _digest(payload: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "evidence_sha256"}
    encoded = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def capture_appendix_e(
    client: httpx.AsyncClient,
    *,
    phase: str,
    rehearsal_id: str,
    expected_model: str,
    reader_token: str,
    ops_token: str,
    trace_wait_seconds: float = 15.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Capture one profile using only responses served during this invocation."""
    if phase not in _PHASES:
        raise EvidenceError(f"phase must be one of {_PHASES!r}")
    rehearsal_id = _require_nonempty_str(rehearsal_id, field="rehearsal_id")
    expected_model = _require_nonempty_str(expected_model, field="expected_model")
    reader_headers = _auth(reader_token)
    ops_headers = _auth(ops_token)

    graph_response = await client.get(
        "v1/graph/neighborhood",
        params={"entity": _GRAPH_PROBE_ENTITY, "hops": 1},
        headers=reader_headers,
    )
    if phase == "t1-only":
        if (
            graph_response.status_code != 404
            or _safe_detail(graph_response) != "graph tier not enabled"
        ):
            raise EvidenceError("t1-only capture requires the server's exact graph-disabled 404")
    else:
        graph = _expect_json(graph_response, operation="T1+T2 graph probe")
        _require_list(graph, "nodes", operation="T1+T2 graph probe")
        _require_list(graph, "edges", operation="T1+T2 graph probe")

    search = _expect_json(
        await client.post(
            "v1/brain/search",
            json={"query": MERIDIAN_MULTI_HOP_QUESTION, "top_k": 10},
            headers=reader_headers,
        ),
        operation="Meridian brain search",
    )
    citations, graph_path_count = _normalize_citations(search)
    if phase == "t1-only" and graph_path_count != 0:
        raise EvidenceError("graph-disabled search unexpectedly served graph paths")

    before_body = _expect_json(
        await client.get("v1/traces", params={"limit": 200}, headers=ops_headers),
        operation="pre-run trace index",
    )
    before = _trace_ids(before_body)

    session = _expect_json(
        await client.post(
            "v1/chat/sessions",
            json={"agent_name": "console-chat"},
            headers=reader_headers,
        ),
        operation="chat session creation",
    )
    session_id = _require_nonempty_str(session.get("session_id"), field="session_id")
    chat_response = await client.post(
        f"v1/chat/sessions/{quote(session_id, safe='')}/messages",
        json={"prompt": MERIDIAN_MULTI_HOP_QUESTION},
        headers=reader_headers,
    )
    if chat_response.status_code != 200:
        raise EvidenceError(
            f"chat run returned HTTP {chat_response.status_code}: {_safe_detail(chat_response)}"
        )
    answer, tool_transits = _parse_chat(chat_response.text)

    trace_id = await _new_trace_id(
        client,
        ops_headers=ops_headers,
        before=before,
        wait_seconds=trace_wait_seconds,
    )
    trace = _expect_json(
        await client.get(f"v1/trace/{quote(trace_id, safe='')}", headers=ops_headers),
        operation="captured trace",
    )
    if trace.get("trace_id") != trace_id:
        raise EvidenceError("captured trace body does not match the correlated trace id")
    requested, served, model_calls, input_tokens, output_tokens, tool_calls = _trace_measurements(
        trace, expected_model=expected_model
    )

    captured_at = now or datetime.now(UTC)
    if captured_at.tzinfo is None:
        raise EvidenceError("capture timestamp must be timezone-aware")
    payload: dict[str, Any] = {
        "schema": _SCHEMA,
        "rehearsal_id": rehearsal_id,
        "phase": phase,
        "captured_at": captured_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "question": MERIDIAN_MULTI_HOP_QUESTION,
        "expected_model": expected_model,
        "requested_model": requested,
        "served_model": served,
        "trace_id": trace_id,
        "model_calls": model_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tool_calls": tool_calls,
        "tool_transits": tool_transits,
        "answer": answer,
        "citations": citations,
        "graph_path_count": graph_path_count,
    }
    payload["evidence_sha256"] = _digest(payload)
    return payload


def write_capture(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a new owner-only artifact; never overwrite earlier evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise EvidenceError(f"refusing to overwrite existing capture: {path}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def load_capture(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"could not read capture artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise EvidenceError(f"capture artifact is not a JSON object: {path}")
    _validate_capture(payload)
    return payload


def _parse_timestamp(value: Any) -> datetime:
    raw = _require_nonempty_str(value, field="captured_at")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("capture timestamp is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise EvidenceError("capture timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _validate_capture(payload: Mapping[str, Any]) -> None:
    if set(payload) != _ARTIFACT_FIELDS:
        raise EvidenceError("capture artifact fields do not match the Appendix E schema")
    if payload.get("schema") != _SCHEMA:
        raise EvidenceError("capture artifact schema is not supported")
    if payload.get("phase") not in _PHASES:
        raise EvidenceError("capture artifact phase is invalid")
    if payload.get("question") != MERIDIAN_MULTI_HOP_QUESTION:
        raise EvidenceError("capture artifact question is not the canonical Meridian B1 question")
    for field in (
        "rehearsal_id",
        "expected_model",
        "requested_model",
        "served_model",
        "trace_id",
        "answer",
    ):
        _require_nonempty_str(payload.get(field), field=field)
    _parse_timestamp(payload.get("captured_at"))
    for field in ("model_calls", "input_tokens", "output_tokens", "graph_path_count"):
        _require_count(payload.get(field), field=field)
    if payload.get("model_calls") == 0:
        raise EvidenceError("capture artifact has no measured model calls")
    if not isinstance(payload.get("tool_calls"), list) or not all(
        isinstance(item, str) and item for item in payload["tool_calls"]
    ):
        raise EvidenceError("capture artifact tool_calls is malformed")
    if not isinstance(payload.get("tool_transits"), list) or not all(
        isinstance(item, dict)
        and set(item) == {"name", "decision"}
        and all(isinstance(item[key], str) and item[key] for key in item)
        for item in payload["tool_transits"]
    ):
        raise EvidenceError("capture artifact tool_transits is malformed")
    if not isinstance(payload.get("citations"), list) or not all(
        isinstance(item, dict)
        and set(item) == {"chunk_id", "document_id", "source_system", "source_ref"}
        and all(isinstance(item[key], str) and item[key] for key in item)
        for item in payload["citations"]
    ):
        raise EvidenceError("capture artifact citations is malformed")
    digest = payload.get("evidence_sha256")
    if not isinstance(digest, str) or digest != _digest(payload):
        raise EvidenceError("capture artifact evidence hash does not match its contents")


def _fresh_timestamp(payload: Mapping[str, Any], *, now: datetime) -> datetime:
    captured = _parse_timestamp(payload["captured_at"])
    age = now - captured
    if age < timedelta(minutes=-1) or age > _MAX_CAPTURE_AGE:
        raise EvidenceError("capture is not fresh enough for the in-room comparison")
    return captured


def _table_text(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _render_sources(citations: list[dict[str, str]]) -> list[str]:
    if not citations:
        return ["- None were served by the search call."]
    return [
        f"- `{item['document_id']}` / `{item['source_system']}` / `{item['source_ref']}`"
        for item in citations
    ]


def compare_captures(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    """Render captured evidence only after every cross-run invariant passes."""
    _validate_capture(first)
    _validate_capture(second)
    by_phase = {str(first["phase"]): first, str(second["phase"]): second}
    if set(by_phase) != set(_PHASES):
        raise EvidenceError("comparison requires exactly one t1-only and one t1-t2 capture")
    t1 = by_phase["t1-only"]
    t2 = by_phase["t1-t2"]

    compared_at = now or datetime.now(UTC)
    if compared_at.tzinfo is None:
        raise EvidenceError("comparison timestamp must be timezone-aware")
    compared_at = compared_at.astimezone(UTC)
    t1_time = _fresh_timestamp(t1, now=compared_at)
    t2_time = _fresh_timestamp(t2, now=compared_at)
    if abs(t1_time - t2_time) > _MAX_CAPTURE_AGE:
        raise EvidenceError("the two captures are not from the same in-room rehearsal window")

    for field in (
        "rehearsal_id",
        "question",
        "expected_model",
        "requested_model",
        "served_model",
    ):
        if t1[field] != t2[field]:
            raise EvidenceError(f"captures disagree on {field}; comparison is not controlled")
    if t1["expected_model"] != t1["requested_model"]:
        raise EvidenceError("captured requested model does not match the operator's pinned model")
    if t1["trace_id"] == t2["trace_id"]:
        raise EvidenceError("the two phases reused one trace; two real runs are required")

    lines = [
        "# Appendix E — measured T1 versus T1+T2 evidence",
        "",
        f"Rehearsal: `{t1['rehearsal_id']}`  ",
        f"Question: {_table_text(str(t1['question']))}  ",
        f"Requested model: `{t1['requested_model']}`  ",
        f"Served model: `{t1['served_model']}`",
        "",
        "| Measured field | T1 only | T1 + T2 |",
        "| --- | ---: | ---: |",
        f"| Successful model calls | {t1['model_calls']} | {t2['model_calls']} |",
        f"| Input tokens | {t1['input_tokens']} | {t2['input_tokens']} |",
        f"| Output tokens | {t1['output_tokens']} | {t2['output_tokens']} |",
        f"| Successful tool executions | {len(t1['tool_calls'])} | {len(t2['tool_calls'])} |",
        f"| Tool transit frames | {len(t1['tool_transits'])} | {len(t2['tool_transits'])} |",
        f"| Search citations | {len(t1['citations'])} | {len(t2['citations'])} |",
        f"| Search graph paths | {t1['graph_path_count']} | {t2['graph_path_count']} |",
        "",
    ]
    for label, payload in (("T1 only", t1), ("T1 + T2", t2)):
        tool_calls = ", ".join(f"`{tool}`" for tool in payload["tool_calls"]) or "None"
        tool_transits = (
            ", ".join(f"`{item['name']}` ({item['decision']})" for item in payload["tool_transits"])
            or "None"
        )
        lines.extend(
            [
                f"## {label}",
                "",
                f"Trace: `{payload['trace_id']}`  ",
                f"Captured: `{payload['captured_at']}`  ",
                f"Tool executions: {tool_calls}  ",
                f"Tool transits: {tool_transits}",
                "",
                "Answer:",
                "",
                str(payload["answer"]),
                "",
                "Citations the direct search served for the same question:",
                "",
                *_render_sources(payload["citations"]),
                "",
            ]
        )
    lines.extend(
        [
            "The command does not claim that T2 improved the answer. Make that claim only if the",
            "captured evidence demonstrates it; otherwise say that this run did not show an",
            "improvement (or skip the beat if either capture is unavailable).",
        ]
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "MERIDIAN_MULTI_HOP_QUESTION",
    "EvidenceError",
    "capture_appendix_e",
    "compare_captures",
    "load_capture",
    "normalize_api_base",
    "read_token_file",
    "tls_verifier",
    "write_capture",
]
