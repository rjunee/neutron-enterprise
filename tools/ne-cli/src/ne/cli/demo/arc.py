# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO scripted-narrated arc engine — transport-agnostic, HTTP-only.

Drives the full governed brain ladder against a running ne-api over an injected
``httpx.AsyncClient`` (fixture-mode in CI, live https in ``ne demo run``). It speaks
ONLY HTTP to ne-api's governed surface (``/v1/brain`` + ``/v1/chat`` + ``/v1/approvals``)
— it NEVER composes an ungoverned executor, so every action goes THROUGH the frozen
PDP/C6 executor/HITL/audit (C6). Each step is try/except-wrapped: a step never
crashes the arc; an unexpected exception becomes a FAILED status with a class-name-
only detail (C12). Absence of a not-yet-wired tier (T2 graph, live T3, live tier-2
write-back) is DEGRADED with an exact lights-up label — NEVER FAILED.

The ~40-line SSE parser is vendored from packages/ne-api/tests/conftest.py's
``parse_sse`` (the sse.ts grammar): CRLF-normalize, blank-line frames, ':' comments
skipped, multi-line ``data:`` joined with '\\n', ``event:`` captured.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, NamedTuple

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ne.cli.demo.corpus import (
    T3_AMBIGUOUS_PROMPTS,
    T3_CANONICAL_TRIGGER,
    T3_CLARIFY_PROMPT,
    T3_TEMPLATE_DESCRIPTION,
    DemoScenario,
)

#: The ne-api mount prefix under the injected client's base_url. Both the fixture
#: harness (base http://ne.test) and the live stack (Caddy at https://<host>) expose
#: the governed API under ``/api`` — the arc builds every path from this.
DEFAULT_MOUNT = "/api"

_T2_DEGRADE_LABEL = (
    "graph tier unavailable in the stock demo — requires persistent AGE and "
    "governed source producers"
)
_T3_DEGRADE_LABEL = (
    "verified answers not enabled in this deployment — set "
    "NE_ONTOLOGY_VERIFIED_ANSWERS_ENABLED + the template bundle (the demo overlay wires it)"
)
_HITL_DEGRADE_LABEL = (
    "no tier-2 write-back tool registered in this deployment — set "
    "NE_API_DEMO_WRITEBACK_TOOL (the demo overlay wires it); full HITL arc is CI-proven"
)
_DELEGATION_DEGRADE_LABEL = (
    "delegation demo not enabled in this deployment — set NE_API_DEMO_DELEGATION "
    "(the demo overlay wires it)"
)
_GROUNDED_DEGRADE_LABEL = (
    "grounded agent not available in this deployment — either it is not enabled "
    "(NE_ONTOLOGY_GROUNDED_AGENT_ENABLED + NE_API_CHAT_AGENTS; the demo overlay "
    "wires it), or canonical default Ask is active (NE_API_DEFAULT_ASK_GENERATION), "
    "whose database fence retires the legacy writer the grounded session executes on"
)

_AGENT_NAME = "console-chat"
_AGENT_GROUNDED = "grounded-analyst"

#: NE-DEMO-WRITEBACK-LIVE: the real NE-050 governed CRM-note write action
#: (Tier-2), the live swap-target for Beat 4's write-back call. Duplicated
#: from ``packages/ne-runtime/src/ne/runtime/reference_actions/crm_note.py``'s
#: ``crm_note_definition(...).spec.name`` — ne-cli src must not import
#: ne-api/ne-runtime (the dependency points the other way); the two are kept
#: in sync by ``test_crm_action_name_matches_reference_action``.
CRM_WRITEBACK_ACTION = "write.internal.crm.note"

#: The scripted PASS-leg draft's per-account count line (see
#: ``tests/demo_fixture_harness.py::_grounded_draft``): ``"<name>: <n> open
#: orders."`` — parsed back here so the arc can independently verify every
#: reported count against its OWN recomputation (never trust the model's
#: number). NE-CTXB-8: the grounded beat now targets the server-side verified
#: COUNT aggregate, so the draft's line reports OPEN orders (the word "placed"
#: is now ungroundable — no status cells in the aggregate corpus). NE-GA-G7:
#: this regex is FIXTURE-STRICT and LIVE-OPTIONAL — fixture mode's deterministic
#: phrasing always matches it (the exact-count assertion is retained there), but
#: a live model's phrasing may vary; when it does not match, the grounded_pass
#: leg's SOLE pass/fail gate is the structural verdict (``grounded["fallback"]
#: is False`` and the aggregate fetch present, untruncated) — unparseable live
#: phrasing is honestly reported, never DEGRADED/FAILED solely because this
#: regex found nothing (see ``_grounded_counts_verdict``).
_GROUNDED_COUNT_RE = re.compile(r"^([^:\n]+): (\d+) open orders\.$", re.MULTILINE)

#: The class name ``ne.runtime.errors.ToolNotRegisteredError`` arrives VERBATIM
#: in the SSE error frame's ``detail`` via ne-api's safe_error_detail contract
#: (packages/ne-api/src/ne/api/sse.py:65-75 — class NAME only, for unknown
#: classes). Hardcoded string, no ne-runtime import (NE-DEMO-FIX1).
_UNKNOWN_TOOL_ERROR_CLASS = "ToolNotRegisteredError"


# --- SSE parser (vendored from ne-api tests conftest parse_sse) --------------


@dataclass
class SseFrame:
    event: str | None
    data: dict[str, Any] | None


@dataclass
class ParsedSse:
    frames: list[SseFrame]

    def of_type(self, event: str) -> list[SseFrame]:
        return [f for f in self.frames if f.event == event]


def parse_sse(body: str) -> ParsedSse:
    """Parse an SSE stream per the sse.ts grammar (vendored from ne-api conftest)."""
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    frames: list[SseFrame] = []
    for raw_block in normalized.split("\n\n"):
        block = raw_block.strip("\n")
        if not block:
            continue
        event: str | None = None
        data_parts: list[str] = []
        is_frame = False
        for line in block.split("\n"):
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
                is_frame = True
            elif line.startswith("data:"):
                data_parts.append(line[len("data:") :].lstrip())
                is_frame = True
        if not is_frame:
            continue
        data_str = "\n".join(data_parts)
        data = json.loads(data_str) if data_str else None
        frames.append(SseFrame(event=event, data=data))
    return ParsedSse(frames=frames)


# --- transcript models -------------------------------------------------------


class StepStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


def _t3_verified_status(invoke_ok: bool, done_frame_template_id: bool) -> StepStatus:
    """Honest status for the T3 verified-answer step (never over-claims — Codex P2).

    The governed ``POST /v1/verified/invoke`` leg is the authoritative T3 proof, so a
    failed invoke is a hard FAIL. But the step must NOT report ``OK`` while its own
    recorded evidence shows the conversational chat leg did not confirm
    (``done_frame_template_id`` false) — that would let the dossier imply the chat leg
    succeeded when it did not. When the governed invoke passed but the chat leg did not
    surface the template (the small CPU model may not emit the tool call — a labeled
    note, never a FAIL), the step is honestly ``DEGRADED``, not ``OK``.
    """
    if not invoke_ok:
        return StepStatus.FAILED
    return StepStatus.OK if done_frame_template_id else StepStatus.DEGRADED


def _grounded_counts_verdict(
    parsed_counts: Mapping[str, int], expected: Mapping[str, int]
) -> tuple[StepStatus, str, dict[str, Any]]:
    """Honest status for the grounded_pass leg's ADDITIONAL count recompute-match
    (NE-GA-G7 — never over-claims, the ``_t3_verified_status`` precedent).

    The leg's SOLE pass/fail gate is the model-agnostic STRUCTURAL verdict the
    caller already computed (``grounded["fallback"] is False`` and both template
    fetches present) — this helper only decides the label/evidence for the
    OPTIONAL per-account count check, which fixture mode's deterministic
    phrasing always parses and a live model's phrasing may not:

      * ``parsed_counts`` empty (the fixture-strict regex found nothing) ->
        ``OK`` + ``"counts not machine-checked (live phrasing)"`` — a passing
        structural verdict never FAILs solely because live phrasing didn't
        match; the arc's own recompute simply went unchecked this run.
      * a parsed count CONTRADICTING the arc's own recompute -> ``FAILED`` — a
        wrong CHECKABLE fact is a real failure, never forgiven.
      * every parsed count matches, but not every account with >=1 placed
        order was reported -> ``OK`` + ``"counts partially machine-checked
        (live phrasing)"`` — no FAIL solely from phrasing coverage.
      * every parsed count matches AND every expected account was reported ->
        ``OK``, no label (fixture mode's exact-count teeth, retained).
    """
    if not parsed_counts:
        return (
            StepStatus.OK,
            "counts not machine-checked (live phrasing)",
            {"counts_checked": 0, "counts_machine_checked": False},
        )
    every_parsed_matches = all(expected.get(name) == n for name, n in parsed_counts.items())
    every_expected_reported = all(name in parsed_counts for name, n in expected.items() if n >= 1)
    if not every_parsed_matches:
        return (
            StepStatus.FAILED,
            "",
            {
                "counts_checked": len(parsed_counts),
                "counts_machine_checked": True,
                "counts_matched": False,
            },
        )
    return (
        StepStatus.OK,
        "" if every_expected_reported else "counts partially machine-checked (live phrasing)",
        {
            "counts_checked": len(parsed_counts),
            "counts_machine_checked": True,
            "counts_matched": every_parsed_matches and every_expected_reported,
        },
    )


def _done_verified_template_id(done: list[SseFrame]) -> str | None:
    """The done frame's STRUCTURAL ``verified.template_id``, or ``None``.

    This is the H1-forgery-closure surface: ``verified`` is emitted ONLY by
    ``ne.api.sse`` from ``AgentRunResult.verified`` (:class:`VerifiedProvenance`),
    which only the deterministic ``VerifiedRouter`` populates on a ROUTE outcome —
    model output TEXT has no channel into this key, so a done frame whose
    ``output`` string merely CONTAINS ``"template_id=..."`` (the pre-U5 gateway
    echo) never satisfies this check.
    """
    if not done or not isinstance(done[0].data, dict):
        return None
    verified = done[0].data.get("verified")
    if not isinstance(verified, dict):
        return None
    tid = verified.get("template_id")
    return tid if isinstance(tid, str) else None


class StepResult(BaseModel):
    """One narrated step's governed evidence (frozen; no secret material)."""

    model_config = ConfigDict(frozen=True)

    name: str
    title: str
    narration: str
    status: StepStatus
    label: str = ""
    request_summary: str = ""
    response_excerpt: str = ""
    citations: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


ARC_STEP_COUNT = 12
ArcProgressCallback = Callable[[int, int, str, str, float, StepStatus | None, float | None], None]


@dataclass(frozen=True)
class _StepDeadline:
    """A cumulative wall-clock budget for one narrated step.

    httpx's timeout is an inactivity/phase timeout, so multiple requests, polls,
    or a response that keeps yielding bytes can otherwise run forever in total.
    Wrapping the step's complete async unit in :meth:`wait` makes the advertised
    budget an actual monotonic wall-clock bound.
    """

    started: float
    deadline_s: float

    async def wait(self, awaitable: Awaitable[Any]) -> Any:
        remaining = self.deadline_s - (time.monotonic() - self.started)
        if remaining <= 0:
            if isinstance(awaitable, Coroutine):
                awaitable.close()
            raise TimeoutError("narrated step wall-clock deadline exceeded")
        return await asyncio.wait_for(awaitable, timeout=remaining)


def _progress_start(
    progress: ArcProgressCallback | None,
    index: int,
    title: str,
    request_deadline_s: float,
) -> _StepDeadline:
    started = time.monotonic()
    if progress is not None:
        progress(index, ARC_STEP_COUNT, title, "start", request_deadline_s, None, None)
    return _StepDeadline(started=started, deadline_s=request_deadline_s)


def _progress_finish(
    progress: ArcProgressCallback | None,
    index: int,
    result: StepResult,
    request_deadline_s: float,
    started: _StepDeadline,
) -> StepResult:
    elapsed = time.monotonic() - started.started
    if elapsed > request_deadline_s:
        result = result.model_copy(
            update={
                "status": StepStatus.FAILED,
                "label": f"step exceeded {request_deadline_s:.0f}s wall-clock deadline",
            }
        )
    if progress is not None:
        progress(
            index,
            ARC_STEP_COUNT,
            result.title,
            "finish",
            request_deadline_s,
            result.status,
            elapsed,
        )
    return result


class DemoTranscript(BaseModel):
    """The whole arc's governed transcript (rendered into the dossier)."""

    model_config = ConfigDict(frozen=True)

    steps: list[StepResult]
    audit_verify: dict[str, Any] | None = None
    started: str
    finished: str

    def has_failure(self) -> bool:
        return any(s.status is StepStatus.FAILED for s in self.steps)


# --- HTTP helpers ------------------------------------------------------------


async def _open_session(
    client: httpx.AsyncClient, mount: str, headers: dict[str, str], *, agent_name: str = _AGENT_NAME
) -> str:
    resp = await client.post(
        f"{mount}/v1/chat/sessions", json={"agent_name": agent_name}, headers=headers
    )
    resp.raise_for_status()
    return resp.json()["session_id"]


def _excerpt(text: str, limit: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


async def _open_grounded_session(
    client: httpx.AsyncClient, mount: str, headers: dict[str, str]
) -> tuple[str | None, int]:
    """Open a ``grounded-analyst`` chat session; ``(None, 422)`` on an unknown
    ``agent_name`` (an honest DEGRADE signal, distinct from a hard failure)."""
    resp = await client.post(
        f"{mount}/v1/chat/sessions", json={"agent_name": _AGENT_GROUNDED}, headers=headers
    )
    if resp.status_code == 422:
        return None, 422
    resp.raise_for_status()
    return resp.json()["session_id"], resp.status_code


def _done_grounded(sse_body: str) -> tuple[dict[str, Any] | None, str]:
    """Parse an SSE turn body; return (STRUCTURAL ``grounded`` dict or ``None``, output).

    ``grounded`` is populated ONLY by the G3 runtime post-step's own
    ``GroundedProvenance`` (sse.py's additive ``done`` key) — never a substring
    of model output text (the same H1-forgery-closure discipline
    ``_done_verified_template_id`` establishes for the verified-answer path).
    """
    parsed = parse_sse(sse_body)
    done = parsed.of_type("done")
    data = done[0].data if (done and isinstance(done[0].data, dict)) else {}
    output = data.get("output", "") if isinstance(data, dict) else ""
    grounded = data.get("grounded") if isinstance(data, dict) else None
    return (grounded if isinstance(grounded, dict) else None), (
        output if isinstance(output, str) else ""
    )


def _sse_error_details(parsed: ParsedSse) -> list[str]:
    """Surface the ACTUAL error class(es) carried by a turn's ``error`` frame(s)
    (e.g. ``EgressBlockedError`` vs a real policy rejection vs a timeout), so the
    dossier can state WHY a leg degraded instead of guessing (NE-DEMO-FIX1)."""
    return [
        str(f.data.get("detail", "")) for f in parsed.of_type("error") if isinstance(f.data, dict)
    ]


def _fetch_template_ids(grounded: Mapping[str, Any]) -> set[str]:
    fetches = grounded.get("fetches", [])
    if not isinstance(fetches, list):
        return set()
    return {f["template_id"] for f in fetches if isinstance(f, dict) and "template_id" in f}


def _template_not_staged(response: httpx.Response) -> bool:
    """True ONLY for the governed API's audited "this deployment does not hold that
    template" refusal to ``POST /v1/verified/invoke``.

    That route raises 404 from TWO distinct causes (``ne/api/routers/ontology.py``)
    and ONLY THE FIRST is benign:

    * ``{"detail": "unknown-template"}`` — the fail-closed pre-check: the id is absent
      from the frozen registry. "This deployment does not stage that template", a
      configuration fact. THIS is what this predicate matches.
    * ``{"detail": "template-tool-unregistered"}`` — the template IS staged but its
      executor binding is missing. That is a PLATFORM REGRESSION and must FAIL the
      step. The two shared one body until the router was given a distinct one; while
      they did, a lost binding degraded the arc and ``ne demo run`` exited zero.

    Every OTHER 404 still FAILS the step too: an unmounted or mistyped route
    (Starlette's ``{"detail": "Not Found"}``), a sibling route's
    ``validation-issue-not-found``, a proxy's HTML error page. A blanket "404 is fine"
    would hide a dropped route or a wrong mount behind an honest-looking DEGRADE —
    strictly worse than the failure it would be papering over.
    """
    if response.status_code != 404:
        return False
    try:
        body = response.json()
    except ValueError:  # non-JSON body (a proxy error page) is NOT this refusal
        return False
    return isinstance(body, dict) and body.get("detail") == "unknown-template"


def _not_staged_label(template_ids: str) -> str:
    """The DEGRADE reason naming the verified template(s) this deployment lacks."""
    return (
        f"verified template(s) not staged in this deployment: {template_ids} — the "
        "governed API refused the invoke with an audited 404 unknown-template"
    )


class _GroundedRecompute(NamedTuple):
    """The two-leg grounded-count recompute result (NE-CTXB-8 §5.4).

    Exactly one of ``degrade_reason`` / ``failed_reason`` is non-empty on a
    non-OK result; both empty (with a populated ``expected``) is the clean OK.
    ``expected`` is the AGGREGATE cells (the shared artifact — closes the
    cap-divergence by construction); it is empty on any non-OK result.
    """

    expected: dict[str, int]
    degrade_reason: str
    failed_reason: str
    evidence: dict[str, Any]


def _compare_recount(aggregate_cells: Mapping[str, int], recount: Mapping[str, int]) -> str:
    """Compare the aggregate template's per-name cells against the arc's OWN
    independent Python row-join recount over the FULL directory reads.

    Returns ``""`` when they match EXACTLY, else a non-empty failure reason. This
    is the R6-P1e regression guard: leg (b)'s hand-rolled row-join is the SECOND
    implementation, so a broken trusted aggregate rewrite diverges from it and
    FAILS here — the arc never collapses into a tautology of re-invoking (and
    trivially agreeing with) the same aggregate. Pure function → unit-testable
    without HTTP (``test_grounded_recount_divergence_fails``)."""
    if dict(aggregate_cells) == dict(recount):
        return ""
    return "aggregate/recount divergence"


async def _grounded_expected_counts(
    client: httpx.AsyncClient, mount: str, reader: dict[str, str]
) -> _GroundedRecompute:
    """The arc's OWN two-leg recompute (never the model's own numbers; never
    raises — the caller maps ``failed_reason`` → FAILED and ``degrade_reason`` →
    DEGRADED). Rule 3 (§5.4): ``truncated`` is read on EVERY consumed answer; any
    truncation is a DEGRADE with reason, NEVER a silent partial-count comparison.

    LEG A (shared-artifact consistency): re-invoke the SAME
    ``orders-per-account-count`` aggregate and take its per-ACCOUNT cells as the
    comparison basis — closes the cap-divergence by construction (the chat beat
    and the arc consume the identical governed artifact). ``expected`` is exposed
    to the caller name-keyed for the fixture-strict per-name draft verdict.

    LEG B (independent regression guard — R6-P1e): an INDEPENDENT Python row-join
    over the FULL, untruncated ``accounts-directory`` + ``orders-directory`` reads
    (status=='placed' per account IRI), compared cell-for-cell against leg A via
    :func:`_compare_recount`. Both sides are keyed by ACCOUNT IRI (identity), not
    display name (Codex C1: shared names must not collapse distinct groups). A
    broken aggregate rewrite diverges here and FAILS — leg B is the SECOND
    implementation, never a re-invoke tautology."""
    # -- LEG A: the shared server-side verified COUNT aggregate ----------------
    agg_resp = await client.post(
        f"{mount}/v1/verified/invoke",
        json={"template_id": "orders-per-account-count", "args": {}},
        headers=reader,
    )
    if agg_resp.status_code != 200:
        # A template this deployment does not stage is an UNAVAILABLE tier, not a
        # broken one — DEGRADE with the id, exactly as the 503 path does. Any other
        # non-200 (including any other 404) still FAILS.
        if _template_not_staged(agg_resp):
            return _GroundedRecompute({}, _not_staged_label("orders-per-account-count"), "", {})
        return _GroundedRecompute(
            {}, "", f"aggregate invoke failed (HTTP {agg_resp.status_code})", {}
        )
    agg_body = agg_resp.json()
    agg_answer = agg_body.get("answer", {}) if isinstance(agg_body, dict) else {}
    if agg_answer.get("truncated") is True:
        reason = agg_answer.get("truncation_reason")
        return _GroundedRecompute(
            {},
            f"aggregate read truncated ({reason}) — count comparison skipped",
            "",
            {"aggregate_truncated": True, "aggregate_truncation_reason": reason},
        )
    try:
        agg_columns = list(agg_answer["columns"])
        agg_account_ix = agg_columns.index("account")
        agg_name_ix = agg_columns.index("name")
        agg_n_ix = agg_columns.index("n_orders")
        # Key the aggregate by ACCOUNT IRI (identity), NOT the display ``name`` —
        # two visible accounts can share a ``name``, and keying by name would
        # collapse their distinct groups (last-write-wins) while leg (b)'s
        # row-join SUMS both under that name, spuriously reporting a correct
        # aggregate as an ``aggregate/recount divergence`` (Codex C1). ``name`` is
        # retained per account for display/verdict only, never as the join key.
        aggregate_by_account: dict[str, int] = {}
        name_by_account: dict[str, str] = {}
        for row in agg_answer["rows"]:
            account = row[agg_account_ix]
            aggregate_by_account[account] = int(row[agg_n_ix])
            name_by_account[account] = row[agg_name_ix]
    except (KeyError, TypeError, ValueError):
        return _GroundedRecompute({}, "", "aggregate answer shape invalid", {})
    if not aggregate_by_account:
        # A silently-empty aggregate is a FAILURE — the demo data guarantees 28
        # groups; an empty read means the count machinery broke, not a valid zero.
        return _GroundedRecompute({}, "", "aggregate read returned zero groups", {})
    evidence: dict[str, Any] = {
        "aggregate_groups": len(aggregate_by_account),
        "replay_match": bool(agg_body.get("replay_match")),
    }

    # -- LEG B: the INDEPENDENT Python row-join over FULL directory reads -------
    accounts_resp = await client.post(
        f"{mount}/v1/verified/invoke",
        json={"template_id": "accounts-directory", "args": {}},
        headers=reader,
    )
    orders_resp = await client.post(
        f"{mount}/v1/verified/invoke",
        json={"template_id": "orders-directory", "args": {}},
        headers=reader,
    )
    if accounts_resp.status_code != 200 or orders_resp.status_code != 200:
        unread = [
            (tid, resp)
            for tid, resp in (
                ("accounts-directory", accounts_resp),
                ("orders-directory", orders_resp),
            )
            if resp.status_code != 200
        ]
        # DEGRADE only when EVERY read that failed to arrive was the audited
        # not-staged refusal — one genuine error alongside one absent template is
        # still a FAILURE.
        if all(_template_not_staged(resp) for _tid, resp in unread):
            return _GroundedRecompute(
                {}, _not_staged_label(" + ".join(tid for tid, _resp in unread)), "", evidence
            )
        return _GroundedRecompute({}, "", "directory recompute read failed", evidence)
    accounts_answer = accounts_resp.json().get("answer", {})
    orders_answer = orders_resp.json().get("answer", {})
    for label, ans in (
        ("accounts-directory", accounts_answer),
        ("orders-directory", orders_answer),
    ):
        if ans.get("truncated") is True:
            reason = ans.get("truncation_reason")
            return _GroundedRecompute(
                {},
                f"{label} recompute read truncated ({reason}) — count comparison skipped",
                "",
                evidence,
            )
    try:
        a_columns = list(accounts_answer["columns"])
        a_account_ix = a_columns.index("account")
        a_name_ix = a_columns.index("name")
        name_by_iri = {row[a_account_ix]: row[a_name_ix] for row in accounts_answer["rows"]}

        o_columns = list(orders_answer["columns"])
        o_account_ix = o_columns.index("account")
        o_status_ix = o_columns.index("status")
        # Recount keyed by ACCOUNT IRI (identity), the SAME key as leg (a) — never
        # by display name (Codex C1). Only accounts visible in the
        # accounts-directory read are counted.
        recount: dict[str, int] = {}
        for row in orders_answer["rows"]:
            if row[o_status_ix] == "placed":
                account = row[o_account_ix]
                if account in name_by_iri:
                    recount[account] = recount.get(account, 0) + 1
    except (KeyError, TypeError, ValueError):
        return _GroundedRecompute({}, "", "directory recompute answer shape invalid", evidence)

    # The R6-P1e divergence guard runs over ACCOUNT-IDENTITY keys on both sides.
    divergence = _compare_recount(aggregate_by_account, recount)
    if divergence:
        return _GroundedRecompute(
            {},
            "",
            divergence,
            {**evidence, "aggregate_size": len(aggregate_by_account), "recount_size": len(recount)},
        )
    # ``expected`` is name-keyed for the fixture-strict per-name draft verdict
    # (the demo's account names are distinct); identity-keyed divergence already
    # ran above, so this name view is display/verdict-only.
    expected_by_name = {name_by_account[account]: n for account, n in aggregate_by_account.items()}
    return _GroundedRecompute(
        expected_by_name,
        "",
        "",
        {**evidence, "recompute_legs_match": True, "aggregate_truncated": False},
    )


# --- the arc -----------------------------------------------------------------


async def run_demo_arc(
    client: httpx.AsyncClient,
    bearers: Mapping[str, dict[str, str]],
    scenario: DemoScenario,
    *,
    audit_verify: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    writeback_action: str = "write.external.demo.send",
    writeback_args: Mapping[str, Any] | None = None,
    poll_deadline_s: float = 30.0,
    mount: str = DEFAULT_MOUNT,
    progress: ArcProgressCallback | None = None,
    request_deadline_s: float = 60.0,
) -> DemoTranscript:
    """Execute the full governed ladder IN ORDER; return a :class:`DemoTranscript`.

    ``bearers`` maps ``reader``/``outsider``/``approver`` → HTTP auth header dicts.
    ``audit_verify`` (injected) returns the ``ne audit verify --json`` shape.
    """
    args = dict(writeback_args or {"to": "ops"})
    reader = dict(bearers["reader"])
    outsider = dict(bearers["outsider"])
    approver = dict(bearers["approver"])
    # NE-168: the delegation step is ops-gated; absent in older callers (pre-NE-168
    # bearers maps) it honestly DEGRADEs rather than KeyError-ing the whole arc.
    ops = dict(bearers["ops"]) if "ops" in bearers else None
    started = datetime.now(UTC).isoformat()
    steps: list[StepResult] = []

    def _failed(name: str, title: str, narration: str, exc: BaseException) -> StepResult:
        return StepResult(
            name=name,
            title=title,
            narration=narration,
            status=StepStatus.FAILED,
            response_excerpt=f"unexpected error: {type(exc).__name__}",
        )

    t1_raw: dict[str, Any] = {}

    # -- Step 1: T1 hybrid retrieval with citations ---------------------------
    title = "T1 — hybrid retrieval with citations"
    progress_started = _progress_start(progress, 1, title, request_deadline_s)
    narration = (
        f"The reader ({scenario.reader_id}) asks a governed question. ne-api runs "
        "vector+BM25 hybrid retrieval, ACL-trimmed to what the reader may see, and "
        "returns cited chunks."
    )
    try:
        resp = await progress_started.wait(
            client.post(
                f"{mount}/v1/brain/search",
                json={"query": scenario.t1_question, "top_k": 5},
                headers=reader,
            )
        )
        ok = resp.status_code == 200
        t1_raw = resp.json() if ok else {}
        chunks = t1_raw.get("chunks", []) if ok else []
        citations = t1_raw.get("citations", []) if ok else []
        answer_seen = any(
            scenario.t1_answer in (c.get("chunk", {}).get("text", "")) for c in chunks
        )
        status = StepStatus.OK if (ok and chunks and citations) else StepStatus.FAILED
        steps.append(
            StepResult(
                name="t1_search",
                title=title,
                narration=narration,
                status=status,
                request_summary=f"POST {mount}/v1/brain/search q={scenario.t1_question!r} top_k=5",
                response_excerpt=(
                    f"{len(chunks)} chunk(s), {len(citations)} citation(s); "
                    f"ground-truth answer {scenario.t1_answer!r} present={answer_seen}"
                ),
                citations=list(citations),
                evidence={
                    "status_code": resp.status_code,
                    "chunk_count": len(chunks),
                    "answer_seen": answer_seen,
                },
            )
        )
    except Exception as exc:
        steps.append(_failed("t1_search", title, narration, exc))
    steps[-1] = _progress_finish(progress, 1, steps[-1], request_deadline_s, progress_started)

    # -- Step 2: permission trap (deny-means-deny on read) --------------------
    title = "T1 permission trap — deny-means-deny on read"
    progress_started = _progress_start(progress, 2, title, request_deadline_s)
    narration = (
        f"The outsider ({scenario.outsider_id}) is denied the exec-only document "
        f"{scenario.trap_doc_id}. C7 trims it from BOTH search and retrieve-by-id — "
        "the outsider can never see it, by construction."
    )
    try:
        search = await progress_started.wait(
            client.post(
                f"{mount}/v1/brain/search",
                json={"query": scenario.t1_question, "top_k": 10},
                headers=outsider,
            )
        )
        s_chunks = search.json().get("chunks", []) if search.status_code == 200 else []
        trap_in_search = any(
            c.get("chunk", {}).get("document_id") == scenario.trap_document_id for c in s_chunks
        )
        retrieve = await progress_started.wait(
            client.post(
                f"{mount}/v1/brain/retrieve",
                json={"chunk_ids": [scenario.trap_document_id]},
                headers=outsider,
            )
        )
        r_chunks = retrieve.json().get("chunks", []) if retrieve.status_code == 200 else []
        trap_in_retrieve = len(r_chunks) > 0
        status = (
            StepStatus.OK if (not trap_in_search and not trap_in_retrieve) else StepStatus.FAILED
        )
        steps.append(
            StepResult(
                name="permission_trap",
                title=title,
                narration=narration,
                status=status,
                request_summary=(
                    f"POST {mount}/v1/brain/search + retrieve as outsider "
                    f"(trap doc {scenario.trap_doc_id})"
                ),
                response_excerpt=(
                    f"outsider saw {len(s_chunks)} chunk(s); trap in search={trap_in_search}, "
                    f"trap in retrieve-by-id={trap_in_retrieve}"
                ),
                evidence={
                    "trap_in_search": trap_in_search,
                    "trap_in_retrieve": trap_in_retrieve,
                    "outsider_visible_chunks": len(s_chunks),
                },
            )
        )
    except Exception as exc:
        steps.append(_failed("permission_trap", title, narration, exc))
    steps[-1] = _progress_finish(progress, 2, steps[-1], request_deadline_s, progress_started)

    # -- Step 3: T2 knowledge-graph (REAL governed neighborhood read) ---------
    title = "T2 — knowledge-graph multi-hop"
    progress_started = _progress_start(progress, 3, title, request_deadline_s)
    narration = (
        "The reader reads the governed graph neighborhood of the engineering runbook "
        "(GET /v1/graph/neighborhood). Then the OUTSIDER anchors on the exec-only trap "
        "entity — C7 trims it to EMPTY at source (dual-key honesty). The stock demo "
        "degrades this step until persistent AGE and governed sources are composed."
    )
    try:
        reader_resp = await progress_started.wait(
            client.get(
                f"{mount}/v1/graph/neighborhood",
                params={"entity": scenario.t2_anchor_key, "hops": 2},
                headers=reader,
            )
        )
        if reader_resp.status_code == 404:
            steps.append(
                StepResult(
                    name="t2_graph",
                    title=title,
                    narration=narration,
                    status=StepStatus.DEGRADED,
                    label=_T2_DEGRADE_LABEL,
                    request_summary=(
                        f"GET {mount}/v1/graph/neighborhood entity={scenario.t2_anchor_key} hops=2"
                    ),
                    response_excerpt="graph tier not enabled by ne-api in this deployment",
                    evidence={"graph_surface_present": False},
                )
            )
        else:
            reader_nodes = (
                len(reader_resp.json().get("nodes", [])) if reader_resp.status_code == 200 else 0
            )
            trap_resp = await progress_started.wait(
                client.get(
                    f"{mount}/v1/graph/neighborhood",
                    params={"entity": scenario.t2_trap_key, "hops": 2},
                    headers=outsider,
                )
            )
            outsider_trap_nodes = (
                len(trap_resp.json().get("nodes", [])) if trap_resp.status_code == 200 else 0
            )
            ok = reader_nodes > 0 and outsider_trap_nodes == 0
            steps.append(
                StepResult(
                    name="t2_graph",
                    title=title,
                    narration=narration,
                    status=StepStatus.OK if ok else StepStatus.FAILED,
                    request_summary=(
                        f"GET {mount}/v1/graph/neighborhood reader(anchor)+outsider(trap)"
                    ),
                    response_excerpt=(
                        f"reader saw {reader_nodes} node(s) around the anchor; "
                        f"outsider saw {outsider_trap_nodes} node(s) around the trap "
                        "(trimmed at source)"
                    ),
                    evidence={
                        "graph_surface_present": True,
                        "reader_nodes": reader_nodes,
                        "outsider_trap_nodes": outsider_trap_nodes,
                    },
                )
            )
    except Exception as exc:
        steps.append(_failed("t2_graph", title, narration, exc))
    steps[-1] = _progress_finish(progress, 3, steps[-1], request_deadline_s, progress_started)

    # -- Step 4: T3 verified answer (deterministic invoke + deterministically
    # ROUTED chat-turn leg) ----------------------------------------------------
    title = "T3 — ontology-backed verified answer"
    progress_started = _progress_start(progress, 4, title, request_deadline_s)
    narration = (
        "The reader asks in natural language; a governed deterministic router "
        "matches the query to the accounts-directory verified template and invokes "
        "it — returning a provable, replayable, entity-cited answer. It is "
        "architecturally incapable of guessing: off-template questions get a "
        "governed clarification or an audited refusal, never an unverified answer."
    )
    verified_surface_present = False
    # Set to the template id(s) this deployment does not stage, so the gated steps
    # below name the real reason instead of blaming a flag that IS set. DECIDED: the
    # arc still ATTEMPTS every verified leg on any composition — one code path, and
    # the transcript then carries the deployment's own audited refusal as evidence.
    # Pre-filtering against GET /v1/verified/templates would be a second source of
    # truth for what is servable (the exact split that caused this bug) and would
    # hide the refusal it is guessing about.
    missing_verified_template: str | None = None
    try:
        invoke = await progress_started.wait(
            client.post(
                f"{mount}/v1/verified/invoke",
                json={"template_id": "accounts-directory", "args": {}},
                headers=reader,
            )
        )
        if invoke.status_code == 503:
            steps.append(
                StepResult(
                    name="t3_verified",
                    title=title,
                    narration=narration,
                    status=StepStatus.DEGRADED,
                    label=_T3_DEGRADE_LABEL,
                    request_summary=f"POST {mount}/v1/verified/invoke template=accounts-directory",
                    response_excerpt="verified answers not enabled in this deployment",
                    evidence={"verified_surface_present": False},
                )
            )
        elif _template_not_staged(invoke):
            # The verified tier is ENABLED but does not hold this template — an
            # unavailable tier for the arc's purposes, degraded exactly like the 503
            # above (never a pass, never a FAIL). ``verified_surface_present`` stays
            # False because the surface is unusable FOR THIS ARC; the extra evidence
            # key records the narrower truth.
            missing_verified_template = "accounts-directory"
            steps.append(
                StepResult(
                    name="t3_verified",
                    title=title,
                    narration=narration,
                    status=StepStatus.DEGRADED,
                    label=_not_staged_label("accounts-directory"),
                    request_summary=f"POST {mount}/v1/verified/invoke template=accounts-directory",
                    response_excerpt=(
                        "audited 404 unknown-template — this deployment does not "
                        "stage 'accounts-directory'"
                    ),
                    evidence={
                        "verified_surface_present": False,
                        "verified_template_not_staged": "accounts-directory",
                    },
                )
            )
        elif (invoke.json() if invoke.status_code == 200 else {}).get("answer", {}).get(
            "truncated"
        ) is True:
            # Rule 3 (§5.4) plan-symmetry: the sibling t3 verified invoke leg reads
            # ``truncated`` too — a truncated verified read is DEGRADED with the
            # reason, never silently passed as a certified (partial) table. At the
            # shipped cap (8192 > the 6016-token accounts render) this branch is
            # never taken; it fires under the small-cap packing proof.
            verified_surface_present = True
            _t3_answer = invoke.json().get("answer", {})
            _t3_reason = _t3_answer.get("truncation_reason")
            steps.append(
                StepResult(
                    name="t3_verified",
                    title=title,
                    narration=narration,
                    status=StepStatus.DEGRADED,
                    label=f"verified read truncated ({_t3_reason})",
                    request_summary=f"POST {mount}/v1/verified/invoke template=accounts-directory",
                    response_excerpt="verified read truncated — partial table not certified",
                    evidence={
                        "verified_surface_present": True,
                        "t3_truncated": True,
                        "truncation_reason": _t3_reason,
                    },
                )
            )
        else:
            verified_surface_present = True
            body = invoke.json() if invoke.status_code == 200 else {}
            answer = body.get("answer", {}) if isinstance(body, dict) else {}
            rows = answer.get("rows", []) or []
            citations = answer.get("citations", []) or []
            config_hash = answer.get("config_hash", "") or ""
            ontology_version = answer.get("ontology_version", "") or ""
            invoke_template_id = str(answer.get("template_id", ""))
            replay_match = bool(body.get("replay_match")) if isinstance(body, dict) else False
            citations_ok = bool(citations) and all(
                str(c).startswith("urn:ne:entity") for c in citations
            )
            invoke_ok = (
                invoke.status_code == 200
                and len(rows) >= 1
                and citations_ok
                and bool(config_hash)
                and bool(ontology_version)
                and replay_match
            )
            # The chat-turn leg: deterministic routing (ne.runtime.verified_router)
            # bypasses the model entirely on a ROUTE — no prefix sentinel needed
            # (routing is deterministic now, the '[verified-answer]' prefix is
            # obsolete and dropped). Structural assertion only: the done frame's
            # ``verified.template_id`` (never a substring of model output text —
            # H1 forgery closed).
            routed_tid: str | None = None
            done_verified_config_hash: str | None = None
            try:
                sid = await progress_started.wait(_open_session(client, mount, reader))
                turn = await progress_started.wait(
                    client.post(
                        f"{mount}/v1/chat/sessions/{sid}/messages",
                        json={"prompt": scenario.t3_question},
                        headers=reader,
                    )
                )
                parsed = parse_sse(turn.text)
                done = parsed.of_type("done")
                routed_tid = _done_verified_template_id(done)
                if done and isinstance(done[0].data, dict):
                    verified_obj = done[0].data.get("verified")
                    if isinstance(verified_obj, dict):
                        done_verified_config_hash = verified_obj.get("config_hash")
            except Exception:
                routed_tid = None
                done_verified_config_hash = None
            done_frame_template_id = (
                routed_tid == "accounts-directory" and routed_tid == invoke_template_id
            )
            config_hash_match = bool(routed_tid) and done_verified_config_hash == config_hash
            # The chat leg only CONFIRMS when BOTH structural facts hold: the routed
            # template_id matches the invoke leg's AND the routed provenance carries
            # the SAME config_hash (Codex P2: the same template_id under a different
            # configuration must not be certified as provenance-verified).
            steps.append(
                StepResult(
                    name="t3_verified",
                    title=title,
                    narration=narration,
                    status=_t3_verified_status(
                        invoke_ok, done_frame_template_id and config_hash_match
                    ),
                    request_summary=f"POST {mount}/v1/verified/invoke template=accounts-directory",
                    response_excerpt=_excerpt(
                        f"{len(rows)} row(s); citations urn:ne:entity={citations_ok}; "
                        f"config_hash set={bool(config_hash)}; ontology_version set="
                        f"{bool(ontology_version)}; replay_match={replay_match}; "
                        f"done_frame_verified_template_id={routed_tid!r}"
                    ),
                    citations=[{"citation": str(c)} for c in citations],
                    evidence={
                        "verified_surface_present": True,
                        "rows": len(rows),
                        "citations_urn_ne_entity": citations_ok,
                        "config_hash_set": bool(config_hash),
                        "ontology_version_set": bool(ontology_version),
                        "replay_match": replay_match,
                        "done_frame_template_id": done_frame_template_id,
                        "done_frame_verified_template_id": routed_tid,
                        "config_hash_match": config_hash_match,
                    },
                )
            )
    except Exception as exc:
        steps.append(_failed("t3_verified", title, narration, exc))
    steps[-1] = _progress_finish(progress, 4, steps[-1], request_deadline_s, progress_started)
    # Every step below that needs the verified tier reports WHY it could not run: the
    # stock "not enabled" text when the tier is off, or the not-staged reason naming
    # the template when the tier is on but does not hold it.
    t3_gate_label = (
        _T3_DEGRADE_LABEL
        if missing_verified_template is None
        else _not_staged_label(missing_verified_template)
    )
    grounded_gate_label = (
        _GROUNDED_DEGRADE_LABEL
        if missing_verified_template is None
        else _not_staged_label(missing_verified_template)
    )

    # -- Step 5: T3 governed clarification (bounded) --------------------------
    title = "T3 — governed clarification (bounded)"
    progress_started = _progress_start(progress, 5, title, request_deadline_s)
    narration = (
        "The reader asks an in-domain but ambiguous question. The deterministic "
        "router does not guess: it returns a governed clarification menu (zero "
        "model tokens). The reader answers with an ordinal and the SAME session "
        "routes to the verified template — structural provenance in the done frame."
    )
    try:
        if not verified_surface_present:
            steps.append(
                StepResult(
                    name="t3_clarify",
                    title=title,
                    narration=narration,
                    status=StepStatus.DEGRADED,
                    label=t3_gate_label,
                    evidence={"verified_surface_present": False},
                )
            )
        else:
            sid = await progress_started.wait(_open_session(client, mount, reader))
            turn1 = await progress_started.wait(
                client.post(
                    f"{mount}/v1/chat/sessions/{sid}/messages",
                    json={"prompt": T3_CLARIFY_PROMPT},
                    headers=reader,
                )
            )
            parsed1 = parse_sse(turn1.text)
            done1 = parsed1.of_type("done")
            d1 = done1[0].data if (done1 and done1[0].data) else {}
            out1 = d1.get("output", "") if isinstance(d1, dict) else ""
            menu_shown = T3_TEMPLATE_DESCRIPTION in out1 and T3_CANONICAL_TRIGGER in out1
            clarify_no_verified = bool(done1) and "verified" not in d1

            turn2 = await progress_started.wait(
                client.post(
                    f"{mount}/v1/chat/sessions/{sid}/messages",
                    json={"prompt": "1"},
                    headers=reader,
                )
            )
            parsed2 = parse_sse(turn2.text)
            routed_tid = _done_verified_template_id(parsed2.of_type("done"))
            ordinal_routed = routed_tid == "accounts-directory"

            status = (
                StepStatus.OK
                if (menu_shown and clarify_no_verified and ordinal_routed)
                else StepStatus.FAILED
            )
            steps.append(
                StepResult(
                    name="t3_clarify",
                    title=title,
                    narration=narration,
                    status=status,
                    request_summary=(
                        f"POST {mount}/v1/chat/... ambiguous prompt {T3_CLARIFY_PROMPT!r} "
                        "→ clarify menu → '1' → routed"
                    ),
                    response_excerpt=(
                        f"menu_shown={menu_shown}; clarify_no_verified={clarify_no_verified}; "
                        f"ordinal_routed={ordinal_routed}"
                    ),
                    evidence={
                        "verified_surface_present": True,
                        "menu_shown": menu_shown,
                        "clarify_no_verified": clarify_no_verified,
                        "ordinal_routed": ordinal_routed,
                        "routed_template_id": routed_tid or "",
                    },
                )
            )
    except Exception as exc:
        steps.append(_failed("t3_clarify", title, narration, exc))
    steps[-1] = _progress_finish(progress, 5, steps[-1], request_deadline_s, progress_started)

    # -- Step 6: T3 audited refusal (fail-closed bound) -----------------------
    title = "T3 — audited refusal (fail-closed bound)"
    progress_started = _progress_start(progress, 6, title, request_deadline_s)
    narration = (
        "The reader stays ambiguous past the governed clarification bound "
        "(clarify_max=2). The router refuses honestly — restating what it CAN "
        "answer — and the refusal is audited (guardrail.block). It never "
        "free-texts an unverified answer."
    )
    try:
        if not verified_surface_present:
            steps.append(
                StepResult(
                    name="t3_refusal",
                    title=title,
                    narration=narration,
                    status=StepStatus.DEGRADED,
                    label=t3_gate_label,
                    evidence={"verified_surface_present": False},
                )
            )
        else:
            sid = await progress_started.wait(_open_session(client, mount, reader))
            outputs: list[str] = []
            has_verified: list[bool] = []
            for prompt in T3_AMBIGUOUS_PROMPTS:
                turn = await progress_started.wait(
                    client.post(
                        f"{mount}/v1/chat/sessions/{sid}/messages",
                        json={"prompt": prompt},
                        headers=reader,
                    )
                )
                parsed = parse_sse(turn.text)
                done = parsed.of_type("done")
                data = done[0].data if (done and done[0].data) else {}
                outputs.append(data.get("output", "") if isinstance(data, dict) else "")
                has_verified.append(isinstance(data, dict) and "verified" in data)

            clarify1_ok = not has_verified[0] and T3_TEMPLATE_DESCRIPTION in outputs[0]
            clarify2_ok = not has_verified[1] and T3_TEMPLATE_DESCRIPTION in outputs[1]
            refused = (
                not has_verified[2]
                and "cannot guess" in outputs[2]
                and T3_TEMPLATE_DESCRIPTION in outputs[2]
            )
            status = (
                StepStatus.OK if (clarify1_ok and clarify2_ok and refused) else StepStatus.FAILED
            )
            steps.append(
                StepResult(
                    name="t3_refusal",
                    title=title,
                    narration=narration,
                    status=status,
                    request_summary=(
                        f"POST {mount}/v1/chat/... 3 ambiguous turns in one session → "
                        "audited refusal"
                    ),
                    response_excerpt=_excerpt(outputs[2]),
                    evidence={
                        "verified_surface_present": True,
                        "clarify_turns": sum([clarify1_ok, clarify2_ok]),
                        "refused": refused,
                        "refusal_no_verified": not has_verified[2],
                        "refusal_menu_shown": T3_TEMPLATE_DESCRIPTION in outputs[2],
                    },
                )
            )
    except Exception as exc:
        steps.append(_failed("t3_refusal", title, narration, exc))
    steps[-1] = _progress_finish(progress, 6, steps[-1], request_deadline_s, progress_started)

    # -- Step 7: grounded analyst — reason + fetch ONE aggregate, verified counts
    title = "Grounded analyst — reason + fetch a verified COUNT, every fact verified"
    progress_started = _progress_start(progress, 7, title, request_deadline_s)
    narration = (
        "The grounded analyst reasons with the model — it plans and fetches ONE "
        "server-side verified COUNT aggregate (orders-per-account-count), reasoning "
        "over dozens of pre-grouped rows instead of two 500-row tables — yet it is "
        "architecturally incapable of stating a fact we did not verify this turn. "
        "Every count below was independently recomputed by this arc TWO ways: "
        "re-invoking the same governed aggregate (shared-artifact consistency) AND "
        "an independent Python row-join over the full directory reads."
    )
    try:
        if not verified_surface_present:
            steps.append(
                StepResult(
                    name="grounded_pass",
                    title=title,
                    narration=narration,
                    status=StepStatus.DEGRADED,
                    label=grounded_gate_label,
                    evidence={"verified_surface_present": False},
                )
            )
        else:
            # (i) ARC-OWN two-leg recompute: leg (a) re-invokes the SAME aggregate
            # (shared artifact), leg (b) is an INDEPENDENT Python row-join over the
            # FULL directory reads (R6-P1e regression guard). Any truncation on a
            # consumed answer → DEGRADE with reason, never a partial-count compare.
            recompute = await progress_started.wait(
                _grounded_expected_counts(client, mount, reader)
            )
            if recompute.failed_reason:
                steps.append(
                    StepResult(
                        name="grounded_pass",
                        title=title,
                        narration=narration,
                        status=StepStatus.FAILED,
                        response_excerpt=f"arc-own recompute failed: {recompute.failed_reason}",
                        evidence=recompute.evidence,
                    )
                )
            elif recompute.degrade_reason:
                steps.append(
                    StepResult(
                        name="grounded_pass",
                        title=title,
                        narration=narration,
                        status=StepStatus.DEGRADED,
                        label=recompute.degrade_reason,
                        evidence=recompute.evidence,
                    )
                )
            else:
                expected = recompute.expected
                session_id, sc = await progress_started.wait(
                    _open_grounded_session(client, mount, reader)
                )
                if session_id is None:
                    steps.append(
                        StepResult(
                            name="grounded_pass",
                            title=title,
                            narration=narration,
                            status=StepStatus.DEGRADED,
                            label=_GROUNDED_DEGRADE_LABEL,
                            evidence={"chat_session_status": sc},
                        )
                    )
                else:
                    turn = await progress_started.wait(
                        client.post(
                            f"{mount}/v1/chat/sessions/{session_id}/messages",
                            json={"prompt": scenario.g_question},
                            headers=reader,
                        )
                    )
                    grounded, output = _done_grounded(turn.text)
                    if grounded is None:
                        error_details = _sse_error_details(parse_sse(turn.text))
                        if error_details:
                            steps.append(
                                StepResult(
                                    name="grounded_pass",
                                    title=title,
                                    narration=narration,
                                    status=StepStatus.FAILED,
                                    response_excerpt=f"turn errored: {', '.join(error_details)}",
                                    evidence={
                                        "grounded_present": False,
                                        "error_details": error_details,
                                    },
                                )
                            )
                        else:
                            steps.append(
                                StepResult(
                                    name="grounded_pass",
                                    title=title,
                                    narration=narration,
                                    status=StepStatus.DEGRADED,
                                    label=_GROUNDED_DEGRADE_LABEL,
                                    evidence={"grounded_present": False},
                                )
                            )
                    else:
                        fetch_ids = _fetch_template_ids(grounded)
                        fallback = grounded.get("fallback")
                        assist_used = grounded.get("assist_used")
                        raw_fetches = grounded.get("fetches", [])
                        raw_fetches = raw_fetches if isinstance(raw_fetches, list) else []
                        # Rule 3 (§5.4) applied to the CHAT-consumed answer too: a
                        # truncated verified fetch the grounded turn read → DEGRADED
                        # with reason, never a FAILED-by-confusion or partial-count
                        # comparison (VerifiedProvenance carries truncated —
                        # contracts/runtime.py:113).
                        truncated_fetches = [
                            str(f.get("template_id"))
                            for f in raw_fetches
                            if isinstance(f, dict) and f.get("truncated") is True
                        ]
                        if truncated_fetches:
                            steps.append(
                                StepResult(
                                    name="grounded_pass",
                                    title=title,
                                    narration=narration,
                                    status=StepStatus.DEGRADED,
                                    label=(
                                        "grounded fetch truncated "
                                        f"({', '.join(truncated_fetches)}) — count "
                                        "comparison skipped"
                                    ),
                                    response_excerpt=_excerpt(output),
                                    evidence={
                                        "fetches": sorted(fetch_ids),
                                        "fallback": fallback,
                                        "assist_used": assist_used,
                                        "chat_fetch_truncated": True,
                                    },
                                )
                            )
                        else:
                            hard_ok = (
                                fetch_ids == {"orders-per-account-count"} and fallback is False
                            )
                            if not hard_ok:
                                steps.append(
                                    StepResult(
                                        name="grounded_pass",
                                        title=title,
                                        narration=narration,
                                        status=StepStatus.FAILED,
                                        response_excerpt=_excerpt(output),
                                        evidence={
                                            "fetches": sorted(fetch_ids),
                                            "fallback": fallback,
                                            "assist_used": assist_used,
                                        },
                                    )
                                )
                            else:
                                parsed_counts = {
                                    m.group(1): int(m.group(2))
                                    for m in _GROUNDED_COUNT_RE.finditer(output)
                                }
                                # The aggregate is an inner join (only accounts WITH
                                # >=1 open order), so the ACCOUNT-IDENTITY group count
                                # (Codex C1 — never len(expected), which is name-keyed
                                # and would undercount on a shared display name) is the
                                # accounts-with-placed count.
                                accounts_with_placed = recompute.evidence.get(
                                    "aggregate_groups", len(expected)
                                )
                                count_status, count_label, count_evidence = (
                                    _grounded_counts_verdict(parsed_counts, expected)
                                )
                                steps.append(
                                    StepResult(
                                        name="grounded_pass",
                                        title=title,
                                        narration=narration,
                                        status=count_status,
                                        label=count_label,
                                        request_summary=(
                                            f"POST {mount}/v1/chat/... grounded-analyst "
                                            f"g_question={scenario.g_question!r}"
                                        ),
                                        response_excerpt=_excerpt(output),
                                        evidence={
                                            "fetches": sorted(fetch_ids),
                                            "fallback": fallback,
                                            "assist_used": assist_used,
                                            # NE-HGATE-4 (additive): surface the
                                            # done-frame grounding posture so the
                                            # LIVE gate can assert strictness=standard
                                            # + gate=ng-2/… from `ne demo run`.
                                            "strictness": grounded.get("strictness"),
                                            "gate": grounded.get("gate"),
                                            "accounts_with_placed": accounts_with_placed,
                                            "aggregate_groups": accounts_with_placed,
                                            "recompute_legs_match": True,
                                            "aggregate_truncated": False,
                                            **count_evidence,
                                        },
                                    )
                                )
    except Exception as exc:
        steps.append(_failed("grounded_pass", title, narration, exc))
    steps[-1] = _progress_finish(progress, 7, steps[-1], request_deadline_s, progress_started)

    # -- Step 8: grounded analyst — refuse-to-hallucinate (audited) -----------
    title = "Grounded analyst — refuse-to-hallucinate (audited)"
    progress_started = _progress_start(progress, 8, title, request_deadline_s)
    narration = (
        "Watch us force it to hallucinate: a scripted draft invents an account "
        "and a number. The deterministic grounding gate refuses — audited — and "
        "hands back only the verified table."
    )
    try:
        if not verified_surface_present:
            steps.append(
                StepResult(
                    name="grounded_refuse",
                    title=title,
                    narration=narration,
                    status=StepStatus.DEGRADED,
                    label=grounded_gate_label,
                    evidence={"verified_surface_present": False},
                )
            )
        else:
            session_id, sc = await progress_started.wait(
                _open_grounded_session(client, mount, reader)
            )
            if session_id is None:
                steps.append(
                    StepResult(
                        name="grounded_refuse",
                        title=title,
                        narration=narration,
                        status=StepStatus.DEGRADED,
                        label=_GROUNDED_DEGRADE_LABEL,
                        evidence={"chat_session_status": sc},
                    )
                )
            else:
                turn = await progress_started.wait(
                    client.post(
                        f"{mount}/v1/chat/sessions/{session_id}/messages",
                        json={"prompt": scenario.g_refuse_prompt},
                        headers=reader,
                    )
                )
                grounded, output = _done_grounded(turn.text)
                if grounded is None:
                    error_details = _sse_error_details(parse_sse(turn.text))
                    if error_details:
                        steps.append(
                            StepResult(
                                name="grounded_refuse",
                                title=title,
                                narration=narration,
                                status=StepStatus.FAILED,
                                response_excerpt=f"turn errored: {', '.join(error_details)}",
                                evidence={
                                    "grounded_present": False,
                                    "error_details": error_details,
                                },
                            )
                        )
                    else:
                        steps.append(
                            StepResult(
                                name="grounded_refuse",
                                title=title,
                                narration=narration,
                                status=StepStatus.DEGRADED,
                                label=_GROUNDED_DEGRADE_LABEL,
                                evidence={"grounded_present": False},
                            )
                        )
                else:
                    fetch_ids = _fetch_template_ids(grounded)
                    fallback = grounded.get("fallback")
                    assist_used = grounded.get("assist_used")
                    initech_absent = "initech" not in output.lower()
                    table_shown = "withheld" in output.lower() and "accounts-directory@v1" in output
                    ok = (
                        fallback is True
                        and initech_absent
                        and table_shown
                        and "accounts-directory" in fetch_ids
                    )
                    steps.append(
                        StepResult(
                            name="grounded_refuse",
                            title=title,
                            narration=narration,
                            status=StepStatus.OK if ok else StepStatus.FAILED,
                            request_summary=(
                                f"POST {mount}/v1/chat/... grounded-analyst "
                                f"g_refuse_prompt={scenario.g_refuse_prompt!r}"
                            ),
                            response_excerpt=_excerpt(output),
                            evidence={
                                "fallback": fallback,
                                "assist_used": assist_used,
                                # NE-HGATE-4 (additive): the done-frame posture the
                                # LIVE gate reads on the refuse leg (strictness=
                                # standard + gate=ng-2/…) — beat 8 stays green at
                                # STANDARD because the numeral is a fact class.
                                "strictness": grounded.get("strictness"),
                                "gate": grounded.get("gate"),
                                "fetches": sorted(fetch_ids),
                                "initech_absent": initech_absent,
                                "table_shown": table_shown,
                            },
                        )
                    )
    except Exception as exc:
        steps.append(_failed("grounded_refuse", title, narration, exc))
    steps[-1] = _progress_finish(progress, 8, steps[-1], request_deadline_s, progress_started)

    # -- Steps 9 & 10: deny-means-deny + HITL approval (C6 write-back) --------
    deny_result, approve_result = await _run_writeback_pair(
        client,
        mount,
        reader=reader,
        approver=approver,
        scenario=scenario,
        writeback_action=writeback_action,
        args=args,
        poll_deadline_s=poll_deadline_s,
        progress=progress,
        request_deadline_s=request_deadline_s,
    )
    steps.append(deny_result)
    steps.append(approve_result)

    # -- Step 11: governed multi-lane agent-to-model delegation (NE-168) -----
    title = "Delegation — governed multi-lane agent-to-model dispatch"
    progress_started = _progress_start(progress, 11, title, request_deadline_s)
    narration = (
        "A long-running agent delegates three sub-tasks across governed lanes — "
        "two private legs (on a sealed CPU box these are different TASKS/lanes over "
        "the SAME CPU model; with --gpu-vllm the reason leg runs the GPU model) and "
        "one frontier leg that is governed-DENIED by policy (PDP-first; the runtime "
        "gate stays as defense-in-depth) with NO egress — all under one replayable trace."
    )
    try:
        if ops is None:
            steps.append(
                StepResult(
                    name="delegation",
                    title=title,
                    narration=narration,
                    status=StepStatus.DEGRADED,
                    label="no ops bearer available in this run",
                    response_excerpt="no ops bearer available in this run",
                )
            )
        else:
            delegate_resp = await progress_started.wait(
                client.post(f"{mount}/v1/demo/delegate", json={}, headers=ops)
            )
            if delegate_resp.status_code == 404:
                steps.append(
                    StepResult(
                        name="delegation",
                        title=title,
                        narration=narration,
                        status=StepStatus.DEGRADED,
                        label=_DELEGATION_DEGRADE_LABEL,
                        request_summary=f"POST {mount}/v1/demo/delegate",
                        response_excerpt="delegation demo not enabled in this deployment",
                        evidence={"delegation_surface_present": False},
                    )
                )
            else:
                delegate_body = delegate_resp.json() if delegate_resp.status_code == 200 else {}
                trace_id = delegate_body.get("trace_id", "")
                legs = delegate_body.get("legs", [])
                trace_resp = await progress_started.wait(
                    client.get(f"{mount}/v1/trace/{trace_id}", headers=ops)
                )
                trace_body = trace_resp.json() if trace_resp.status_code == 200 else {}

                def _walk(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
                    out: list[dict[str, Any]] = []
                    for node in nodes:
                        out.append(node)
                        out.extend(_walk(node.get("children", [])))
                    return out

                route_nodes = [
                    n
                    for n in _walk(trace_body.get("nodes", []))
                    if n.get("action") == "subagent.route"
                ]
                private_success = [
                    n
                    for n in route_nodes
                    if n.get("outcome") == "success"
                    and (n.get("route") or {}).get("tier") == "private"
                ]
                frontier_denied = [
                    n
                    for n in route_nodes
                    if n.get("outcome") == "denied"
                    and (n.get("route") or {}).get("tier") == "frontier"
                    # NE-169: the PDP authority consult fires FIRST, so the audited deny
                    # reason is 'route_policy_denied'; NE-161's runtime gate
                    # ('frontier_routing_disabled') stays as defense-in-depth — either
                    # blocker is a governed denial of the frontier leg.
                    and (n.get("route") or {}).get("reason")
                    in ("route_policy_denied", "frontier_routing_disabled")
                ]
                rollup_trace_id = trace_body.get("rollup", {}).get("trace_id")
                # A `subagent.route` event only proves the ROUTING decision succeeded
                # (the dispatcher emits it before the child model call ever runs) —
                # it says nothing about whether the private leg actually completed.
                # Cross-check against the delegate response's own per-leg
                # `succeeded` (derived from SubAgentResult.succeeded, set only when
                # run_isolated_agent finishes without raising), so a private leg
                # that routed fine but then failed its model call can't read as OK.
                private_leg_completed = sum(
                    1 for leg in legs if leg.get("tier") == "private" and bool(leg.get("succeeded"))
                )
                ok = (
                    delegate_resp.status_code == 200
                    and trace_resp.status_code == 200
                    and len(route_nodes) == 3
                    and len(private_success) == 2
                    and private_leg_completed == 2
                    and len(frontier_denied) == 1
                    and bool(trace_id)
                    and rollup_trace_id == trace_id
                )
                steps.append(
                    StepResult(
                        name="delegation",
                        title=title,
                        narration=narration,
                        status=StepStatus.OK if ok else StepStatus.FAILED,
                        request_summary=(
                            f"POST {mount}/v1/demo/delegate → GET {mount}/v1/trace/{{trace_id}}"
                        ),
                        response_excerpt=(
                            f"{len(route_nodes)} route leg(s); {private_leg_completed} private "
                            f"completed; frontier denied={bool(frontier_denied)}"
                        ),
                        evidence={
                            "trace_id": trace_id,
                            "route_legs": len(route_nodes),
                            "private_success": len(private_success),
                            "private_leg_completed": private_leg_completed,
                            "frontier_denied": bool(frontier_denied),
                            "leg_outcomes": [
                                {"agent": leg.get("agent"), "outcome": leg.get("outcome")}
                                for leg in legs
                            ],
                        },
                    )
                )
    except Exception as exc:
        steps.append(_failed("delegation", title, narration, exc))
    steps[-1] = _progress_finish(progress, 11, steps[-1], request_deadline_s, progress_started)

    # -- Step 12: audit-chain verify -------------------------------------------
    title = "Audit — tamper-evident chain verify"
    progress_started = _progress_start(progress, 12, title, request_deadline_s)
    narration = (
        "Finally the demo verifies the tamper-evident hash chain over the whole "
        "session: every governed decision, tool call, approval, and connector sync "
        "is covered by the audit chain."
    )
    audit_json: dict[str, Any] | None = None
    try:
        if audit_verify is not None:
            verified_audit: dict[str, Any] = await progress_started.wait(audit_verify())
            audit_json = verified_audit
            valid = bool(verified_audit.get("valid"))
            checked = int(verified_audit.get("checked", 0))
            status = StepStatus.OK if (valid and checked > 0) else StepStatus.FAILED
            steps.append(
                StepResult(
                    name="audit_verify",
                    title=title,
                    narration=narration,
                    status=status,
                    request_summary="ne audit verify --json",
                    response_excerpt=f"valid={valid} checked={checked}",
                    evidence={"valid": valid, "checked": checked},
                )
            )
        else:
            steps.append(
                StepResult(
                    name="audit_verify",
                    title=title,
                    narration=narration,
                    status=StepStatus.DEGRADED,
                    label="audit verify not wired in this run",
                    response_excerpt="no audit_verify callable injected",
                )
            )
    except Exception as exc:
        steps.append(_failed("audit_verify", title, narration, exc))
    steps[-1] = _progress_finish(progress, 12, steps[-1], request_deadline_s, progress_started)

    return DemoTranscript(
        steps=steps,
        audit_verify=audit_json,
        started=started,
        finished=datetime.now(UTC).isoformat(),
    )


def _bearer_sub(header: Mapping[str, str]) -> str | None:
    """Extract the OIDC ``sub`` claim from a Bearer auth header — the runtime
    ``Principal.id`` is ``claims.subject`` (ne-identity provisioning), i.e. the
    token ``sub``, NOT the corpus ``person_id``. Used to verify a pending
    approval belongs to the requester by their real principal id."""
    token = header.get("Authorization", "").removeprefix("Bearer ").strip()
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)  # base64url padding
    try:
        return json.loads(base64.urlsafe_b64decode(payload)).get("sub")
    except Exception:
        return None


async def _poll_pending(
    client: httpx.AsyncClient,
    mount: str,
    approver: dict[str, str],
    action: str,
    deadline_s: float,
) -> dict[str, Any] | None:
    """Poll GET /v1/approvals/pending until a row for ``action`` appears or the
    deadline passes. Returns the row (or None)."""
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    while loop.time() < deadline:
        resp = await client.get(f"{mount}/v1/approvals/pending", headers=approver)
        if resp.status_code == 200:
            match = [x for x in resp.json().get("approvals", []) if x.get("action") == action]
            if match:
                return match[0]
        await asyncio.sleep(0.05)
    return None


async def _drive_writeback_turn(
    client: httpx.AsyncClient,
    mount: str,
    *,
    reader: dict[str, str],
    approver: dict[str, str],
    action: str,
    prompt: str,
    decision: str,
    poll_deadline_s: float,
) -> tuple[dict[str, Any] | None, httpx.Response, str | None]:
    """Start a tier-2 write-back chat turn, poll for its pending approval, and
    ``decision`` it (``approve``/``reject``). Returns (pending row, SSE response,
    decision-status)."""
    import asyncio

    sid = await _open_session(client, mount, reader)
    task = asyncio.create_task(
        client.post(
            f"{mount}/v1/chat/sessions/{sid}/messages",
            json={"prompt": prompt},
            headers=reader,
        )
    )
    try:
        pending = await _poll_pending(client, mount, approver, action, poll_deadline_s)
        decision_status: str | None = None
        if pending is not None:
            rid = pending["request_id"]
            body = {} if decision == "approve" else {"reason": "demo deny-means-deny"}
            resp = await client.post(
                f"{mount}/v1/approvals/{rid}/{decision}", json=body, headers=approver
            )
            if resp.status_code == 200:
                decision_status = resp.json().get("status")
        sse = await task
    finally:
        if not task.done():
            task.cancel()
    return pending, sse, decision_status


def _writeback_prompt(action: str, args: Mapping[str, Any], *, again: bool) -> str:
    """The reader's write-back chat prompt for ``action`` (the deny leg when
    ``again`` is False, the HITL-approve leg when ``again`` is True).

    The DEFAULT demo action's two strings are BYTE-IDENTICAL to the pre-unit
    literals (fixture/live back-compat pin —
    ``test_writeback_prompt_default_action_byte_identical``). Any OTHER action
    (the real CRM write) renders its ``args`` sorted + single-quoted so
    ``demo_fixture_harness.PromptRoutedGateway``'s regex parser can recover
    them deterministically (``test_writeback_prompt_crm_renders_sorted_
    quoted_args`` pins this exact shape as the prompt<->parser contract).
    """
    if action == "write.external.demo.send":
        if again:
            return f"[writeback] please send an external note again ({action})"
        return f"[writeback] please send an external note ({action})"
    rendered = ", ".join(f"{k}={str(v)!r}" for k, v in sorted(args.items()))
    suffix = " again" if again else ""
    return f"[writeback] please call the {action} tool{suffix} with arguments " + rendered


async def _run_autonomous_writeback(
    client: httpx.AsyncClient,
    mount: str,
    *,
    reader: dict[str, str],
    approver: dict[str, str],
    writeback_action: str,
    args: Mapping[str, Any],
    progress: ArcProgressCallback | None = None,
    request_deadline_s: float = 60.0,
) -> tuple[StepResult, StepResult]:
    """NE-CS-SEND: Beat 4 for a TIER-1 internal write — autonomous, audited, no gate.

    The owner's tier story (2026-08-27): some writes are risky (external email)
    and require per-item human approval; an internal CRM note is not — it runs
    immediately, fully audited, surfaced in the daily digest. The old
    deny→approve pair asserted the OPPOSITE for this action, so for a
    ``write.internal.*`` Beat 4 the two steps become (9) the write executes
    autonomously, and (10) the CONTROL: no human approval was requested at all —
    the absence is asserted, not assumed. The Tier-2 approval beat lives on
    ``write.external.email.send`` (the Meridian Customer Support agent flow).
    """
    import asyncio

    auto_title = "C6 write-back — Tier-1 internal write executes autonomously"
    auto_started = _progress_start(progress, 9, auto_title, request_deadline_s)
    auto_narration = (
        "The reader instructs a tier-1 INTERNAL write-back (a CRM note). The PDP "
        "classifies write.internal.* as tier 1 — autonomous + daily digest — so the "
        "write executes immediately, fully audited, with no human gate."
    )
    pending_after: dict[str, Any] | None = None
    try:
        sid = await _open_session(client, mount, reader)
        task = asyncio.create_task(
            client.post(
                f"{mount}/v1/chat/sessions/{sid}/messages",
                json={"prompt": _writeback_prompt(writeback_action, args, again=False)},
                headers=reader,
            )
        )
        sse = await auto_started.wait(task)
        parsed = parse_sse(sse.text)
        done = parsed.of_type("done")
        error_details = _sse_error_details(parsed)
        # One post-completion poll: a pending row for this action would mean the
        # write was NOT autonomous.
        pending_after = await _poll_pending(client, mount, approver, writeback_action, 1.0)
        auto = StepResult(
            name="autonomous_write",
            title=auto_title,
            narration=auto_narration,
            status=StepStatus.OK if (done and not error_details) else StepStatus.FAILED,
            request_summary=f"tier-1 {writeback_action} → immediate governed execution",
            response_excerpt=(
                "the internal write executed without a human gate"
                if done and not error_details
                else f"turn errored: {', '.join(error_details) or 'no done frame'}"
            ),
            evidence={"done": bool(done), "error_details": error_details},
        )
    except Exception as exc:
        auto = StepResult(
            name="autonomous_write",
            title=auto_title,
            narration=auto_narration,
            status=StepStatus.FAILED,
            response_excerpt=f"unexpected error: {type(exc).__name__}",
        )
    auto = _progress_finish(progress, 9, auto, request_deadline_s, auto_started)

    control_title = "C6 write-back — tier 1 means NO human gate (the control)"
    control_started = _progress_start(progress, 10, control_title, request_deadline_s)
    control_narration = (
        "The control for the tier ladder: the tier-1 internal write queued NO approval "
        "for anyone to decide. The per-item human gate belongs to tier-2 external "
        "actions (the email send), where the run parks at waiting_approval instead."
    )
    control = StepResult(
        name="no_gate_control",
        title=control_title,
        narration=control_narration,
        status=(
            StepStatus.OK
            if auto.status is StepStatus.OK and pending_after is None
            else StepStatus.FAILED
        ),
        request_summary=f"approvals queue polled for {writeback_action}",
        response_excerpt=(
            "no pending approval existed for the tier-1 write"
            if pending_after is None
            else "a pending approval appeared for a tier-1 write — the ladder is broken"
        ),
        evidence={"pending_after": pending_after is not None},
    )
    control = _progress_finish(progress, 10, control, request_deadline_s, control_started)
    return auto, control


async def _run_writeback_pair(
    client: httpx.AsyncClient,
    mount: str,
    *,
    reader: dict[str, str],
    approver: dict[str, str],
    scenario: DemoScenario,
    writeback_action: str,
    args: Mapping[str, Any],
    poll_deadline_s: float,
    progress: ArcProgressCallback | None = None,
    request_deadline_s: float = 60.0,
) -> tuple[StepResult, StepResult]:
    """Run the deny-means-deny turn then the HITL-approval turn.

    NE-CS-SEND: a ``write.internal.*`` Beat 4 (the live CRM note, now genuinely
    Tier-1) takes the autonomous path instead — see
    :func:`_run_autonomous_writeback`. The inert default
    ``write.external.demo.send`` keeps the deny→approve pair (its class is
    Tier-2, where a human gate is the correct exhibit).
    """
    if writeback_action.startswith("write.internal."):
        return await _run_autonomous_writeback(
            client,
            mount,
            reader=reader,
            approver=approver,
            writeback_action=writeback_action,
            args=args,
            progress=progress,
            request_deadline_s=request_deadline_s,
        )
    # -- deny step --
    deny_title = "C6 write-back — deny-means-deny"
    deny_started = _progress_start(progress, 9, deny_title, request_deadline_s)
    deny_narration = (
        "The reader instructs a tier-2 external write-back. The PDP routes it to "
        "require-approval; the approver REJECTS it — the action never executes."
    )
    try:
        pending, sse, status = await deny_started.wait(
            _drive_writeback_turn(
                client,
                mount,
                reader=reader,
                approver=approver,
                action=writeback_action,
                prompt=_writeback_prompt(writeback_action, args, again=False),
                decision="reject",
                poll_deadline_s=poll_deadline_s,
            )
        )
        parsed = parse_sse(sse.text)
        error_details = _sse_error_details(parsed)
        governed_rejection = _UNKNOWN_TOOL_ERROR_CLASS in error_details
        if pending is not None and status == "rejected":
            deny = StepResult(
                name="deny_means_deny",
                title=deny_title,
                narration=deny_narration,
                status=StepStatus.OK,
                request_summary=f"tier-2 {writeback_action} → approver reject",
                response_excerpt=(
                    "approval rejected; the external write-back never executed "
                    f"(pending principal={pending.get('principal_id')})"
                ),
                evidence={
                    "rejected": True,
                    "principal_id": pending.get("principal_id"),
                    "error_details": error_details,
                },
            )
        elif governed_rejection:
            deny = StepResult(
                name="deny_means_deny",
                title=deny_title,
                narration=deny_narration,
                status=StepStatus.OK,
                label="no tier-2 tool registered — the governed unknown-tool rejection IS "
                "deny-means-deny (audited)",
                request_summary=f"tier-2 {writeback_action} → governed rejection",
                response_excerpt="ne-api governed-rejected the write-back (audited)",
                evidence={
                    "rejected": True,
                    "governed_rejection": True,
                    "error_details": error_details,
                },
            )
        else:
            deny = StepResult(
                name="deny_means_deny",
                title=deny_title,
                narration=deny_narration,
                status=StepStatus.FAILED,
                response_excerpt=(
                    f"no pending approval appeared; turn errored: {', '.join(error_details)}"
                    if error_details
                    else "no pending approval appeared and no governed rejection"
                ),
                evidence={"rejected": False, "error_details": error_details},
            )
    except Exception as exc:
        deny = StepResult(
            name="deny_means_deny",
            title=deny_title,
            narration=deny_narration,
            status=StepStatus.FAILED,
            response_excerpt=f"unexpected error: {type(exc).__name__}",
        )
    deny = _progress_finish(progress, 9, deny, request_deadline_s, deny_started)

    # -- HITL approval step --
    hitl_title = "C6 write-back — HITL approval releases the turn"
    hitl_started = _progress_start(progress, 10, hitl_title, request_deadline_s)
    hitl_narration = (
        "The reader re-issues the write-back. This time a DISTINCT approver APPROVES "
        "the pending request and the turn is released — the human is in the loop."
    )
    try:
        no_tool = deny.status is StepStatus.OK and bool(deny.evidence.get("governed_rejection"))
        if no_tool:
            hitl = StepResult(
                name="hitl_approval",
                title=hitl_title,
                narration=hitl_narration,
                status=StepStatus.DEGRADED,
                label=_HITL_DEGRADE_LABEL,
                request_summary=f"tier-2 {writeback_action} not registered in this deployment",
                response_excerpt="full HITL arc is proven in the CI fixture run",
            )
        else:
            pending, sse, status = await hitl_started.wait(
                _drive_writeback_turn(
                    client,
                    mount,
                    reader=reader,
                    approver=approver,
                    action=writeback_action,
                    prompt=_writeback_prompt(writeback_action, args, again=True),
                    decision="approve",
                    poll_deadline_s=poll_deadline_s,
                )
            )
            parsed = parse_sse(sse.text)
            done = parsed.of_type("done")
            # The pending belongs to the REQUESTER (reader). Its principal_id is the
            # runtime Principal.id, which is the reader token's OIDC ``sub`` under real
            # auth (ne-identity: Principal.id == claims.subject) but the corpus
            # ``person_id`` under the scripted fixture harness — accept EITHER so the
            # check is correct live AND in fixture mode (the prior code compared only
            # to person_id, which never matches the live sub UUID).
            pending_pid = pending.get("principal_id") if pending is not None else None
            principal_ok = pending_pid is not None and pending_pid in {
                _bearer_sub(reader),
                scenario.reader_id,
            }
            released = status == "approved" and bool(done)
            hitl = StepResult(
                name="hitl_approval",
                title=hitl_title,
                narration=hitl_narration,
                status=StepStatus.OK if (principal_ok and released) else StepStatus.FAILED,
                request_summary=f"tier-2 {writeback_action} → approver approve",
                response_excerpt=(
                    f"pending principal={pending.get('principal_id') if pending else None}; "
                    f"decision={status}; released={released}"
                ),
                evidence={
                    "principal_ok": principal_ok,
                    "released": released,
                    "decision": status,
                    "error_details": _sse_error_details(parsed),
                },
            )
    except Exception as exc:
        hitl = StepResult(
            name="hitl_approval",
            title=hitl_title,
            narration=hitl_narration,
            status=StepStatus.FAILED,
            response_excerpt=f"unexpected error: {type(exc).__name__}",
        )
    hitl = _progress_finish(progress, 10, hitl, request_deadline_s, hitl_started)

    return deny, hitl
