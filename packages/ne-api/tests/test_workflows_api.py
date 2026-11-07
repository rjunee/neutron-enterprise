# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-WORKFLOWS — the governed multi-step workflow run, at the composed level.

The governed spine is REAL (``build_platform``, the ONE executor, the real Rego-faithful
PDP, the real DurableApprovalGate over SQLite): only the network edges are faked — the
model gateway, the CRM connector, and the T3 store's contents.

What each test is FOR:

* default-off parity — the flag off leaves the config_hash, the tool menu and the route
  set unchanged, and the routes 404. An additive feature that silently perturbs a
  flag-off deployment is not additive.
* the pure helpers — ranking, sentence assembly and proposal validation are asserted
  DIRECTLY on served-shaped inputs, so a claim about them cannot be satisfied merely by
  the end-to-end path happening to pass.
* the whole run — a live run reads the verified ticket table, ranks accounts, resolves
  each CRM id through the governed lookup, and parks ONE Tier-2 approval per account,
  each carrying all three required fields. This is the assertion the demo rests on.
* the no-bypass property — every tool the run touched appears in the executor's own
  menu, and the write's approval row exists BEFORE any CRM record was created.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ne.api import create_app
from ne.api.model_selection import (
    AuthorizedModelSelection,
    ModelChoice,
    ModelChoiceCatalog,
    ModelSelectionPolicyRefused,
)
from ne.api.platform import build_platform
from ne.api.workflows import (
    AccountLoad,
    Proposal,
    WorkflowContext,
    WorkflowRunView,
    _Run,
    parse_retrieved_text,
    parse_verified_rows,
)
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.types import Lane, Principal

from .conftest import ScriptedGateway, get_state, run, text_completion, tool_call_completion
from .test_demo_seams import _api_settings, _seams


@pytest.fixture
def engines() -> Any:
    started: list[Any] = []
    yield started
    for engine in started:
        engine.dispose()


def _mint_approver(verifier: Any) -> dict[str, str]:
    """The APPROVER reads the queue and the disclosure — a different person, by design."""
    token = verifier.mint("user:approver-demo", groups=("approver",), department="operations")
    return {"Authorization": f"Bearer {token}"}


def _mint_ops(verifier: Any, subject: str = "user:ops-demo") -> dict[str, str]:
    """An operator who is ALSO an employee.

    ``ops`` is the ROLE that may start a run; ``all-employees`` is the DATA label the
    verified templates are scoped to. They are different things, and for THESE
    templates the outcome is BINARY: ``scoped_dataset`` intersects the caller's
    labels with the single ``graph_classes=["all-employees"]``, so a caller holding
    the label gets the full table and a caller without it gets a fail-closed
    ``VerifiedAnswerScopeError`` — surfaced by the run route as an honest 403
    (``test_operator_without_data_label_gets_403_not_500``). There is no per-row
    trimming gradient in this template to demonstrate.
    """
    token = verifier.mint(subject, groups=("ops", "all-employees"), department="operations")
    return {"Authorization": f"Bearer {token}"}


# --- (1) the pure helpers, asserted directly ---------------------------------


def test_verified_rows_parse_and_bad_shape_fails_loud() -> None:
    """Parse the tool's OWN rendering, including a separator inside the free-text tail."""
    served = "\n".join(
        [
            "[showing 2 of 2 rows]",
            "ticket | priority | account | account_name | subject",
            "-------+---------+---------+--------------+--------",
            "urn:t:1 | high | urn:a:1 | Acme Ltd | Widget broke",
            # A subject CONTAINING the separator: because subject is the last column,
            # the bounded split keeps it whole instead of shifting the table.
            "urn:t:2 | low | urn:a:1 | Acme Ltd | Ship A | B kit late",
        ]
    )
    table = parse_verified_rows(served)
    assert table.declared_rows == 2
    assert table.unparsed == 0
    assert table.dicts() == [
        {
            "ticket": "urn:t:1",
            "priority": "high",
            "account": "urn:a:1",
            "account_name": "Acme Ltd",
            "subject": "Widget broke",
        },
        {
            "ticket": "urn:t:2",
            "priority": "low",
            "account": "urn:a:1",
            "account_name": "Acme Ltd",
            "subject": "Ship A | B kit late",
        },
    ]
    # A row with too FEW cells is counted and dropped, never guessed at — a mangled
    # row must not be silently attributed to the wrong account.
    short = "\n".join(
        [
            "[showing 1 of 1 rows]",
            "ticket | priority | account | account_name | subject",
            "-------+---------+---------+--------------+--------",
            "urn:t:3 | high",
        ]
    )
    dropped = parse_verified_rows(short)
    assert (dropped.rows, dropped.unparsed, dropped.declared_rows) == ((), 1, 1)
    # A shape it does not recognise must RAISE. Returning an empty table here would
    # let a parsing bug read as "no accounts are at risk" — the worst wrong answer.
    with pytest.raises(ValueError, match="columns/rows"):
        parse_verified_rows(json.dumps({"answer": {"note": "not a table"}}))


def test_zero_row_render_round_trips_with_note_skipped() -> None:
    """NE-TEMPLATE-ENUM: ``render_answer_text`` appends a ``[note: ...]`` scoping
    line to every ZERO-row table. The parser must read that render as 0 rows /
    0 unparsed — never as a phantom under-read ("served-partial")."""
    from datetime import UTC, datetime

    from ne.brain_ontology.verified.answers import (
        ZERO_ROWS_NOTE,
        VerifiedAnswer,
        render_answer_text,
    )

    empty = VerifiedAnswer(
        template_id="tickets-by-status",
        template_version=1,
        args={"status": "closed"},
        columns=("ticket", "priority", "account", "account_name", "subject"),
        rows=(),
        citations=(),
        truncated=False,
        row_limit=500,
        returned_rows=0,
        total_rows=0,
        truncation_reason=None,
        config_hash="a" * 64,
        ontology_version="b" * 64,
        executed_at=datetime.now(UTC),
    )
    rendered = render_answer_text(empty)
    assert ZERO_ROWS_NOTE in rendered  # the real render carries the note
    table = parse_verified_rows(rendered)
    assert (table.rows, table.unparsed, table.declared_rows) == ((), 0, 0)


def test_workflow_call_ephemerally_materializes_a_protected_tool_result() -> None:
    """Canonical Runs compute from authorized bytes while durable state keeps the ref."""

    class _Executor:
        async def execute(self, tool: str, args: Any, *, principal: Principal) -> str:
            assert (tool, args, principal.id) == ("read.kg.demo", {"q": "current"}, "owner-1")
            return "opaque-protected-reference"

    class _Guard:
        async def materialize_direct_text(self, value: object) -> object:
            assert value == "opaque-protected-reference"
            return SimpleNamespace(
                retryable=False,
                authorized=True,
                text="[showing 0 of 0 rows]\nid\n--\n[note: no rows matched]",
            )

    context = WorkflowContext(
        executor=_Executor(),  # type: ignore[arg-type]
        dispatcher=None,
        gate=object(),  # type: ignore[arg-type]
        principal=Principal(id="owner-1"),
        run=_Run(view=WorkflowRunView(workflow_id="demo", trace_id="run-1")),
        max_subjects=1,
        source_guard=_Guard(),  # type: ignore[arg-type]
    )
    assert run(context.call("read.kg.demo", {"q": "current"})).startswith("[showing 0")


def test_meridian_enumeration_rejects_partial_prefix_and_b2_rejects_two_current_policies() -> None:
    """Negative controls: search-luck prefixes and ambiguous supersession both fail loud."""
    from ne.api.workflows import (
        WorkflowContext,
        WorkflowRunView,
        _read_complete_rows,
        _Run,
        run_workflow,
    )
    from ne.contracts.types import Principal

    class _ScriptedExecutor:
        def __init__(self, outputs: dict[str, str]) -> None:
            self.outputs = outputs
            self.calls: list[str] = []

        async def execute(self, tool: str, args: Any, *, principal: Any) -> str:
            self.calls.append(tool)
            return self.outputs[tool]

    partial = "\n".join(
        [
            "[showing 1 of 2 rows, truncated (rows)]",
            "dispute | dispute_id",
            "--------+-----------",
            "urn:ne:meridian:dispute:mer-dsp-001 | mer-dsp-001",
        ]
    )
    unknown_total = partial.replace("1 of 2", "1 of unknown")

    async def scenario() -> None:
        principal = Principal(id="user:ops", groups=("bank.dispute.read",))
        executor = _ScriptedExecutor({"read.kg.disputes_directory": partial})
        ctx = WorkflowContext(
            executor=executor,  # type: ignore[arg-type]
            dispatcher=None,
            gate=object(),  # type: ignore[arg-type]
            principal=principal,
            run=_Run(view=WorkflowRunView(workflow_id="test", trace_id="trace")),
            max_subjects=5,
        )
        with pytest.raises(ValueError, match="partial"):
            await _read_complete_rows(ctx, "read.kg.disputes_directory", {})
        executor.outputs["read.kg.disputes_directory"] = unknown_total
        with pytest.raises(ValueError, match="partial"):
            await _read_complete_rows(ctx, "read.kg.disputes_directory", {})

        directory = "\n".join(
            [
                "[showing 1 of 1 rows]",
                (
                    "dispute | dispute_id | customer_id | status | notice_date | amount_cents | "
                    "credit_amount_recorded | credit_amount_cents | credit_issue_date"
                ),
                "--------+-----------+------------+--------+-------------+--------------+-----------------------+---------------------+------------------",
                (
                    "urn:ne:meridian:dispute:mer-dsp-001 | mer-dsp-001 | mer-cus-001 | "
                    "open | 2026-08-10 | 65000 | false |  | "
                ),
            ]
        )
        open_rows = "\n".join(
            [
                "[showing 2 of 2 rows]",
                (
                    "dispute | dispute_id | customer_id | status | notice_date | amount_cents | "
                    "credit_amount_recorded | current_policy | policy_artifact_id | "
                    "policy_version | expedited_threshold_cents | "
                    "expedited_deadline_business_days | standard_deadline_business_days | "
                    "as_of_date"
                ),
                "--------+-----------+------------+--------+-------------+--------------+-----------------------+----------------+--------------------+---------------+----------------------------+------------------------------------+---------------------------------+-----------",
                (
                    "urn:ne:meridian:dispute:mer-dsp-001 | mer-dsp-001 | mer-cus-001 | open | "
                    "2026-08-10 | 65000 | false | urn:ne:meridian:policy:p4 | p4 | 4.0 | "
                    "50000 | 5 | 10 | 2026-08-18"
                ),
                (
                    "urn:ne:meridian:dispute:mer-dsp-001 | mer-dsp-001 | mer-cus-001 | open | "
                    "2026-08-10 | 65000 | false | urn:ne:meridian:policy:p5 | p5 | 5.0 | "
                    "50000 | 5 | 10 | 2026-08-18"
                ),
            ]
        )
        ambiguous = _ScriptedExecutor(
            {
                "read.kg.disputes_directory": directory,
                "read.kg.open_disputes_by_status": open_rows,
            }
        )
        view = await run_workflow(
            "meridian-b2-current-sop",
            executor=ambiguous,  # type: ignore[arg-type]
            dispatcher=None,
            gate=object(),  # type: ignore[arg-type]
            principal=principal,
            trace_id="trace-b2",
            max_subjects=5,
        )
        assert view.result is not None
        assert view.result.outcome == "refused"
        assert "exactly one current" in view.result.summary
        assert view.result.findings == []

    run(scenario())


def test_note_skip_is_exact_match_gated_on_zero_declared_rows() -> None:
    """The note skip is EXACTLY ``ZERO_ROWS_NOTE``, and only inside the
    zero-declared-rows envelope where ``render_answer_text`` emits it. Anything
    broader silently drops data: a prefix skip would eat any row whose first
    cell begins ``[note:``, and an ungated skip would eat the note string were
    it ever served as row content. Each branch below discriminates the fixed
    form from the prefix-skip mutant (all three fail under it)."""
    from ne.brain_ontology.verified.answers import ZERO_ROWS_NOTE

    # (a) A data row whose first cell legitimately begins "[note:" is a ROW.
    served = "\n".join(
        [
            "[showing 1 of 1 rows]",
            "subject | account",
            "--------+--------",
            "[note: customer asked for a callback] | acc-1",
        ]
    )
    table = parse_verified_rows(served)
    assert table.rows == (("[note: customer asked for a callback]", "acc-1"),)
    assert table.unparsed == 0

    # (b) Zero-declared envelope, but NOT byte-exactly the renderer's note:
    # counted unparsed (fail-visible), never silently skipped.
    near_miss = "\n".join(
        [
            "[showing 0 of 0 rows]",
            "subject | account",
            "--------+--------",
            "[note: some future different note]",
        ]
    )
    assert parse_verified_rows(near_miss).unparsed == 1

    # (c) The exact note OUTSIDE a zero-SHOWN envelope is not envelope —
    # it is counted unparsed alongside the real row, not dropped.
    wrong_envelope = "\n".join(
        [
            "[showing 1 of 1 rows]",
            "subject | account",
            "--------+--------",
            "ticket-1 | acc-1",
            ZERO_ROWS_NOTE,
        ]
    )
    table = parse_verified_rows(wrong_envelope)
    assert table.rows == (("ticket-1", "acc-1"),)
    assert table.unparsed == 1


def test_workflow_status_constants_match_the_served_ticket_vocabulary() -> None:
    """SPEC §12 item 3's claimed pin: the workflows' hardcoded status strings are the
    SAME closed vocabulary the ``tickets-by-status`` template declares as its ``enum``.

    ``_TICKET_STATUSES`` / ``_UNRESOLVED_STATUSES`` are fed straight into
    ``ctx.call(TICKETS_TOOL, {"status": status})``. Since the template's arg became a
    declared enum, drift here is no longer a silent zero-row read — it is a run-level
    ``ActionValidationError``, and the workflows route has no branch for that class, so
    it would surface as a generic 500. Pin the agreement so drift breaks HERE, loudly,
    at the one place that can name what diverged."""
    from ne.api.workflows import _TICKET_STATUSES, _UNRESOLVED_STATUSES
    from ne.schema import TicketStatus

    assert tuple(s.value for s in TicketStatus) == _TICKET_STATUSES

    # A genuine two-sided SPLIT of that vocabulary. ``assert _UNRESOLVED_STATUSES`` on
    # the tuple constant would be vacuous — pyright folds it to always-true — and an
    # empty set is a proper subset of anything, so the subset check alone does not
    # carry the claim. Both sides are asserted non-empty through a computed set: an
    # EMPTY unresolved side makes every run a silent no-op, and an unresolved side
    # equal to the whole vocabulary makes the word meaningless.
    unresolved = set(_UNRESOLVED_STATUSES)
    vocabulary = set(_TICKET_STATUSES)
    assert unresolved < vocabulary
    assert len(unresolved) > 0
    assert len(vocabulary - unresolved) > 0


def test_note_skip_keys_on_the_shown_count_not_the_declared_total() -> None:
    """The skip gate is the RENDERER's own condition. ``render_answer_text`` emits
    ``ZERO_ROWS_NOTE`` on ``returned_rows == 0`` — the SHOWN count, header group 1 —
    so the parser must gate on shown, NOT on the declared total (group 2).

    The two diverge on a ``[showing 0 of N rows, truncated (...)]`` envelope. Gating on
    the declared total there fails to recognise the renderer's own note as envelope,
    and the note is then served as a PHANTOM single-column DATA ROW (a caller reading
    ``rows`` gets a sentence where it expects a record), or, on a multi-column
    projection, raises a false "partial read" alarm. Both branches below fail under a
    declared-gated parser and pass under a shown-gated one; the (c) case above is the
    control proving the gate still refuses to eat the note outside a zero-SHOWN
    envelope, so this is not simply a loosened skip."""
    from ne.brain_ontology.verified.answers import ZERO_ROWS_NOTE

    # Single column: the note must NOT become a row.
    single = "\n".join(
        ["[showing 0 of 18 rows, truncated (rows)]", "citation", "--------", ZERO_ROWS_NOTE]
    )
    table = parse_verified_rows(single)
    assert table.rows == ()
    assert table.unparsed == 0
    assert table.declared_rows == 18  # the declared total is still reported honestly

    # Multi column: the note must NOT count as unparsed.
    multi = "\n".join(
        [
            "[showing 0 of 18 rows, truncated (rows)]",
            "subject | account",
            "--------+--------",
            ZERO_ROWS_NOTE,
        ]
    )
    table = parse_verified_rows(multi)
    assert table.rows == ()
    assert table.unparsed == 0


def test_ranking_orders_by_escalated_then_volume() -> None:
    rows = [
        {"account": "a", "account_name": "A Ltd", "priority": "low", "subject": "s1"},
        {"account": "a", "account_name": "A Ltd", "priority": "low", "subject": "s2"},
        {"account": "a", "account_name": "A Ltd", "priority": "low", "subject": "s3"},
        {"account": "b", "account_name": "B Ltd", "priority": "urgent", "subject": "s4"},
        {"account": "c", "account_name": "C Ltd", "priority": "normal", "subject": "s5"},
    ]
    from ne.api.workflows import _rank_accounts

    ranked = _rank_accounts(rows)
    # One ESCALATED ticket outranks three low-priority ones — the ordering claim.
    assert [e.account for e in ranked] == ["b", "a", "c"]
    assert ranked[0].escalated == 1
    assert ranked[1].escalated == 0
    # Volume breaks the tie among the un-escalated.
    assert len(ranked[1].tickets) == 3


def test_facts_sentence_states_only_served_values() -> None:
    entry = AccountLoad(
        account="urn:a:1",
        account_name="Acme Ltd",
        tickets=(
            {"subject": "Widget broke", "priority": "high"},
            {"subject": "Late delivery", "priority": "low"},
        ),
        escalated=1,
    )
    facts = entry.facts
    assert "2 unresolved tickets" in facts
    assert "1 of them filed at high or urgent priority" in facts
    assert "Widget broke" in facts and "Late delivery" in facts
    # No invented quantity, and specifically no renewal claim — the fixture carries
    # no renewal date, so a workflow must never imply one.
    assert "renewal" not in facts.lower()


def test_proposal_body_reserves_served_facts_before_clipping_draft() -> None:
    from ne.api.workflows import _BODY_MAX, _proposal_body_with_facts

    heading = "On file at time of review"
    facts = "2 unresolved tickets, 1 of them filed at high or urgent priority."
    trailer = f"\n\n{heading}: {facts}"
    exact_draft = "D" * (_BODY_MAX - len(trailer))

    exact = _proposal_body_with_facts(exact_draft, heading=heading, facts=facts)
    assert exact == f"{exact_draft}{trailer}"
    assert len(exact) == _BODY_MAX

    oversized = _proposal_body_with_facts(f"{exact_draft}X", heading=heading, facts=facts)
    assert oversized == exact
    assert oversized.endswith(trailer)

    with pytest.raises(ValueError, match="served facts exceed the CRM note body limit"):
        _proposal_body_with_facts("ignored", heading=heading, facts="F" * _BODY_MAX)


def test_proposal_refuses_a_missing_required_field() -> None:
    ok = Proposal(
        subject_ref="urn:a:1",
        account_name="Acme Ltd",
        title="Support review",
        body="Two unresolved tickets.",
        evidence_refs=("urn:t:1",),
        record_id="001dL00002MiTaaAAB",
    )
    assert ok.validated() is ok
    for broken, expected in (
        (ok.__class__(**{**vars(ok), "body": ""}), "body"),
        (ok.__class__(**{**vars(ok), "title": "   "}), "title"),
        (ok.__class__(**{**vars(ok), "record_id": None}), "record_id"),
        (ok.__class__(**{**vars(ok), "record_id": "001SHORT"}), "legal Salesforce id"),
    ):
        with pytest.raises(ValueError, match=expected):
            broken.validated()
    with pytest.raises(ValueError, match="single line"):
        ok.__class__(**{**vars(ok), "title": "two\nlines"}).validated()
    with pytest.raises(ValueError, match="field limits"):
        ok.__class__(**{**vars(ok), "body": "x" * 4001}).validated()


def test_retrieved_text_reports_what_the_search_actually_served() -> None:
    """The counts are read from the tool's own header, never assumed."""
    served = "\n".join(
        [
            "[showing 2 of 7 retrieved chunks]",
            "c1: Master Services Agreement between Acme Corporation and Other Ltd",
            "c2: A status report about Acme Ltd",
        ]
    )
    retrieved = parse_retrieved_text(served)
    assert (retrieved.shown, retrieved.candidates) == (2, 7)
    assert len(retrieved.texts) == 2
    assert "Master Services Agreement" in retrieved.joined


def test_retrieved_text_envelope_strict_body_tolerant() -> None:
    """The sibling-parser contrast, pinned as DELIBERATE (not an oversight).

    Envelope: strict like ``parse_verified_rows`` — output that is not the knowledge
    tool's rendering RAISES, so a malformed read surfaces as a failure instead of
    being recorded as ``outcome="served", chunks_shown=0``. Body: tolerant, because
    chunk text is free customer prose the workflow derives no figure from — the
    counts stay the header's, whatever the body looks like.
    """
    # (a) No envelope — the shapes that previously parsed as an "honest" empty serve.
    for garbage in (
        json.dumps({"error": "not a rendering"}),
        "chunk-1: text without any envelope header",
        "",
    ):
        with pytest.raises(ValueError, match="retrieved-chunks envelope"):
            parse_retrieved_text(garbage)
    # (b) A genuinely-empty serve IS honest: header present, zero chunks.
    empty = parse_retrieved_text("[showing 0 of 0 retrieved chunks]")
    assert (empty.shown, empty.candidates, empty.texts) == (0, 0, ())
    # (c) Body tolerance: a continuation line that is not a ``<id>: <text>`` chunk
    #     boundary is not a reason to refuse the serve; the header count is
    #     authoritative and is NOT re-derived from the body.
    ragged = parse_retrieved_text(
        "\n".join(
            [
                "[showing 1 of 3 retrieved chunks]",
                "c1: first line of a chunk",
                "a continuation line without a separator",
            ]
        )
    )
    assert (ragged.shown, ragged.candidates) == (1, 3)
    assert ragged.texts == ("first line of a chunk",)


def test_envelope_reads_the_withheld_clause_and_separates_it_from_an_empty_corpus() -> None:
    """A source-health blackout and an empty corpus are BOTH ``shown == 0`` — and different.

    The knowledge tool appends ``; K withheld …`` when connector health removed
    matches from a read. The envelope reader must take K from the header rather
    than infer it, because the two zeroes lead to opposite conclusions: an empty
    corpus means the question has no answer here, a blackout means the answer
    exists and is currently undisclosable.
    """
    blackout = parse_retrieved_text(
        "[showing 0 of 52 retrieved chunks; 52 withheld — their governed source is "
        "currently degraded; this is a source-health outage, not an empty corpus, so "
        "rephrasing or broadening the query cannot surface them]"
    )
    assert (blackout.shown, blackout.candidates, blackout.withheld) == (0, 52, 52)
    assert blackout.source_health_blackout is True

    empty = parse_retrieved_text("[showing 0 of 0 retrieved chunks]")
    assert empty.withheld == 0
    assert empty.source_health_blackout is False

    partial = parse_retrieved_text(
        "\n".join(
            [
                "[showing 1 of 3 retrieved chunks; 2 withheld — some prose here]",
                "c1: served text",
            ]
        )
    )
    assert (partial.shown, partial.candidates, partial.withheld) == (1, 3, 2)
    assert partial.source_health_blackout is False
    assert partial.texts == ("served text",)

    # The envelope stays STRICT: a withheld clause does not license a loose header.
    with pytest.raises(ValueError, match="retrieved-chunks envelope"):
        parse_retrieved_text("[showing 0 of 52 retrieved chunks; lots withheld]")


def test_recurring_issues_need_two_distinct_accounts() -> None:
    """The signal is an IDENTICAL subject on DIFFERENT accounts — nothing weaker.

    Two tickets with the same subject on ONE account is a duplicate, not a pattern; a
    near-miss subject is a different subject, because this workflow deliberately owns
    no product taxonomy that could decide otherwise.
    """
    from ne.api.workflows import _find_recurring_issues

    rows = [
        {
            "ticket": "t1",
            "subject": "Widget will not power on",
            "account": "a",
            "account_name": "A",
        },
        {
            "ticket": "t2",
            "subject": "Widget will not power on",
            "account": "b",
            "account_name": "B",
        },
        {
            "ticket": "t3",
            "subject": "Widget will not power on",
            "account": "c",
            "account_name": "C",
        },
        # Same account twice — a duplicate, not a cross-account pattern.
        {"ticket": "t4", "subject": "Late delivery", "account": "a", "account_name": "A"},
        {"ticket": "t5", "subject": "Late delivery", "account": "a", "account_name": "A"},
        # A near-miss subject must NOT be folded in with the first cluster.
        {
            "ticket": "t6",
            "subject": "Widget will not power up",
            "account": "d",
            "account_name": "D",
        },
    ]
    issues = _find_recurring_issues(rows, unresolved_refs={"t1", "t6"})
    assert [i.subject for i in issues] == ["Widget will not power on"]
    issue = issues[0]
    assert set(issue.accounts) == {"a", "b", "c"}
    # The unresolved count is a SEPARATE served figure, so a long-closed cluster is
    # never presented as if it were live.
    assert issue.unresolved == 1
    assert len(issue.tickets) == 3


# --- (2) default-off parity ---------------------------------------------------


def test_default_off_leaves_hash_menu_and_routes_unchanged(
    tmp_path: Path, engines: list[Any]
) -> None:
    reset_config_hash_for_testing()
    seams_a, _ = _seams(tmp_path, key="wf-a")
    default = build_platform(_api_settings(), seams_a)
    engines.extend(default.engines)

    reset_config_hash_for_testing()
    seams_b, _ = _seams(tmp_path, key="wf-a")
    explicit_off = build_platform(_api_settings(workflows_enabled=False), seams_b)
    engines.extend(explicit_off.engines)

    assert explicit_off.config_hash == default.config_hash
    assert default.workflow_dispatcher is None
    assert {s.name for s in default.executor.tool_specs} == {
        s.name for s in explicit_off.executor.tool_specs
    }

    reset_config_hash_for_testing()
    seams_c, verifier = _seams(tmp_path, key="wf-c")
    app = create_app(_api_settings(ops_roles=("ops",)), seams=seams_c)
    engines.extend(get_state(app).engines)

    # The routes are always mounted; the feature being off is an honest 404, never a
    # missing route the caller cannot distinguish from a typo. BOTH verbs: the POST
    # is the one that would park approvals, so its gate matters more than the GET's.
    async def _check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            headers = _mint_ops(verifier)
            resp = await client.get("/api/v1/workflows", headers=headers)
            assert resp.status_code == 404, resp.text
            posted = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run", headers=headers
            )
            assert posted.status_code == 404, posted.text

    run(_check())


def test_workflows_without_write_actions_builds_read_only_dispatcher(
    tmp_path: Path, engines: list[Any]
) -> None:
    reset_config_hash_for_testing()
    seams, _ = _seams(tmp_path, key="wf-boot")
    platform = build_platform(_api_settings(workflows_enabled=True), seams)
    engines.extend(platform.engines)

    menu = {spec.name for spec in platform.executor.tool_specs}
    assert platform.workflow_dispatcher is not None
    assert platform.workflow_dispatcher._source_guard is platform.source_guard
    assert platform.gate._source_guard is platform.source_guard
    assert "write.internal.crm.note" not in menu
    assert "write.external.ticket.update" not in menu
    assert "draft.email.create" not in menu
    assert "read.external.crm.account" not in menu


# --- (3) the whole run, end to end -------------------------------------------


class _FakeCrm:
    """Records what the governed write actually asked Salesforce to create."""

    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, str]]] = []

    async def create_record(self, sobject: str, fields: Any) -> str:
        self.created.append((sobject, dict(fields)))
        return f"00Q{len(self.created):015d}"


class _FakeLookup:
    """Resolves exactly the names the seeded graph serves; everything else misses."""

    def __init__(self, known: dict[str, str]) -> None:
        self.known = known
        self.calls: list[tuple[str, str]] = []

    async def find_id_by_name(self, sobject: str, name: str) -> str | None:
        self.calls.append((sobject, name))
        return self.known.get(name)


class _FakeTicketWrite:
    # The method name has to be the PROTOCOL's (`TicketWriteClient.update_record`),
    # not a near-miss: a stand-in that does not implement the seam it stands in for
    # cannot refuse a call on it. Named `update_row`, this guard would never fire —
    # a workflow reaching for the write leg would die on AttributeError instead,
    # failing the test by accident and saying nothing about which leg it touched.
    async def update_record(self, *args: Any, **kwargs: Any) -> int:  # pragma: no cover - unused
        raise AssertionError("the workflow must not touch the ticket write leg")


class _FakeDraftMail:
    async def create_draft(self, *args: Any, **kwargs: Any) -> str:  # pragma: no cover - unused
        raise AssertionError("the workflow must not touch the mail draft leg")


def _ticket(key: str, *, subject: str, priority: str, status: str, account: str) -> dict[str, Any]:
    return {
        "key": key,
        "entity_class": "Ticket",
        "display_name": subject,
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": ["all-employees"],
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": f"{key}-id",
        "subject": subject,
        "status": status,
        "priority": priority,
        "opened_at": "2026-01-01T00:00:00Z",
    }


def _belongs_to(key: str, ticket_key: str, account_key: str) -> dict[str, Any]:
    return {
        "key": key,
        "edge_kind": "BELONGS_TO",
        "from_key": ticket_key,
        "to_key": account_key,
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": ["all-employees"],
        "config_hash": "c",
    }


def _seeded_store(tmp_path: Path) -> Any:
    """A REAL oxigraph-backed T3 store carrying two accounts and four tickets.

    The shape mirrors the shipped Acme graph exactly (Ticket nodes with status /
    priority / subject slots, BELONGS_TO edges to Account nodes), so the template
    under test is exercised against the same triple layout the demo lifts.
    """
    from ne.brain_ontology import PropertyGraphLift
    from ne.brain_ontology.config import OntologySettings
    from ne.brain_ontology.gateway import ValidatingTripleStore
    from ne.brain_ontology.quarantine import SqliteTripleQuarantine
    from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
    from ne.brain_ontology.store.oxigraph import OxigraphBackend

    from .test_ontology_api import _NullSink

    settings = OntologySettings(include_ontology_graph=False)
    store = ValidatingTripleStore(
        OxigraphBackend(),
        build_validator(settings),
        SqliteTripleQuarantine(),
        _NullSink(),
        settings=settings,
        config_hash="s" * 64,
        compiled_shapes=load_compiled_shapes(),
    )
    graph_store = _seeded_graph_store()

    async def _seed() -> None:
        await PropertyGraphLift(graph_store, store).run()

    run(_seed())
    return store


def _seeded_graph_store() -> Any:
    """The Acme T2 snapshot used by both the direct seed and production boot lift."""
    from ne.brain_graph.testing.memory import InMemoryGraphStore

    from .test_ontology_api import _account

    mem = InMemoryGraphStore()

    async def _seed() -> None:
        await mem.upsert_nodes(
            [
                _account("acc_hot", ["all-employees"]) | {"source_status": "no_source"},
                _account("acc_cold", ["all-employees"]) | {"source_status": "no_source"},
                # Two escalated open tickets on acc_hot; one low open ticket on
                # acc_cold; one RESOLVED ticket that must never be read.
                _ticket(
                    "tkt_a",
                    subject="Widget will not power on",
                    priority="urgent",
                    status="open",
                    account="acc_hot",
                )
                | {"source_status": "no_source"},
                _ticket(
                    "tkt_b",
                    subject="Widget shipment delayed",
                    priority="high",
                    status="pending",
                    account="acc_hot",
                )
                | {"source_status": "no_source"},
                _ticket(
                    "tkt_c",
                    subject="Billing question",
                    priority="low",
                    status="open",
                    account="acc_cold",
                )
                | {"source_status": "no_source"},
                # The SAME subject as tkt_a, on a DIFFERENT account and in a
                # DIFFERENT status: the cross-account signal the sweep exists for,
                # and invisible to a read restricted to the unresolved statuses.
                _ticket(
                    "tkt_done",
                    subject="Widget will not power on",
                    priority="urgent",
                    status="resolved",
                    account="acc_cold",
                )
                | {"source_status": "no_source"},
            ]
        )
        await mem.upsert_edges(
            [
                _belongs_to("e_a", "tkt_a", "acc_hot") | {"source_status": "no_source"},
                _belongs_to("e_b", "tkt_b", "acc_hot") | {"source_status": "no_source"},
                _belongs_to("e_c", "tkt_c", "acc_cold") | {"source_status": "no_source"},
                _belongs_to("e_done", "tkt_done", "acc_cold") | {"source_status": "no_source"},
            ]
        )

    run(_seed())
    return mem


def _templates_dir(tmp_path: Path) -> Path:
    """The SHIPPED template bundle — written by the one function the demo also calls."""
    from ne.cli.demo.corpus import write_verified_templates

    return write_verified_templates(tmp_path / "verified-templates")


def _meridian_templates_dir(tmp_path: Path) -> Path:
    """Acme's existing bundle plus BD-5's packaged Meridian templates."""
    from ne.brain_ontology import meridian_template_dir

    destination = _templates_dir(tmp_path)
    for source in meridian_template_dir().glob("*.toml"):
        shutil.copy2(source, destination / source.name)
    return destination


_HOT_ID = "001dL00002MiTaaAAB"
_COLD_ID = "001dL00002MiTbbAAB"


def _meridian_fixture_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "tools" / "meridian" / "fixtures" / "meridian-v1"


def _workflow_app(
    tmp_path: Path,
    *,
    key: str,
    crm: Any | None = None,
    lookup: Any | None = None,
    store: Any | None = None,
    gateway: Any | None = None,
    write_actions_enabled: bool = True,
    verified_templates_dir: Path | None = None,
    demo_meridian_fixture_path: str = "",
    graph_store: Any | None = None,
    demo_ontology_lift: bool = False,
) -> tuple[Any, Any, Any]:
    """One composed workflow app over the standard 2-account seeded graph.

    Returns ``(app, verifier, crm)`` — the governed spine is real; only the model
    gateway, the CRM edges and the T3 store contents are faked (module docstring).
    """
    from ne.api.platform import WriteActionClients
    from ne.brain_graph.config import BrainGraphSettings
    from ne.brain_ontology.config import OntologySettings

    write_crm: Any = None
    write_lookup: Any = None
    if write_actions_enabled:
        write_crm = crm if crm is not None else _FakeCrm()
        write_lookup = (
            lookup
            if lookup is not None
            else _FakeLookup({"acc_hot Corp": _HOT_ID, "acc_cold Corp": _COLD_ID})
        )
        crm = write_crm
    reset_config_hash_for_testing()
    seams, verifier = _seams(
        tmp_path,
        key=key,
        ontology_settings=OntologySettings(
            verified_answers_enabled=True,
            verified_templates_dir=verified_templates_dir or _templates_dir(tmp_path),
            include_ontology_graph=False,
            backend="oxigraph",
        ),
        gateway=gateway,
        brain_graph_settings=(
            BrainGraphSettings(enabled=True) if graph_store is not None else None
        ),
        graph_store=graph_store,
    )
    if write_actions_enabled:
        seams = type(seams)(
            **{
                **{f.name: getattr(seams, f.name) for f in seams.__dataclass_fields__.values()},
                "write_action_clients": WriteActionClients(
                    crm=write_crm,
                    ticket=_FakeTicketWrite(),
                    draft=_FakeDraftMail(),
                    lookup=write_lookup,
                ),
            }
        )
    seeded = store if store is not None else _seeded_store(tmp_path)
    seams.plugin_registry.register("triple_store", "oxigraph", lambda **_kw: seeded)  # type: ignore[union-attr]
    app = create_app(
        _api_settings(
            ops_roles=("ops",),
            approver_roles=("approver",),
            write_actions_enabled=write_actions_enabled,
            workflows_enabled=True,
            demo_meridian_fixture_path=demo_meridian_fixture_path,
            demo_ontology_lift=demo_ontology_lift,
        ),
        seams=seams,
    )
    return app, verifier, crm


def _meridian_workflow_app(
    tmp_path: Path, *, key: str, gateway: Any | None = None
) -> tuple[Any, Any, Any]:
    """Production-compose the real governed engine over six templates and BD-1 data."""
    templates_dir = _meridian_templates_dir(tmp_path)
    return _workflow_app(
        tmp_path,
        key=key,
        store=_seeded_store(tmp_path),
        gateway=gateway,
        write_actions_enabled=False,
        verified_templates_dir=templates_dir,
        demo_meridian_fixture_path=str(_meridian_fixture_dir()),
    )


def _mint_meridian_ops(verifier: Any, *, full: bool = True) -> dict[str, str]:
    labels = (
        "ops",
        "all-employees",
        "bank.customer.read",
        *(("bank.servicing.read", "bank.dispute.read") if full else ()),
    )
    token = verifier.mint("user:meridian-ops", groups=labels, department="operations")
    return {"Authorization": f"Bearer {token}"}


class _WorkflowSelection:
    """Server-authority double; the gateway remains the real runtime carrier consumer."""

    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.choice_calls: list[str] = []
        self.authorize_calls: list[dict[str, Any]] = []

    async def choices(self, task: str) -> ModelChoiceCatalog:
        self.choice_calls.append(task)
        return ModelChoiceCatalog(
            task=task,
            choices=(
                ModelChoice(
                    model="stub",
                    provider="stub",
                    lane=Lane.FRONTIER_API,
                    evidence="frontier-provider-attempt",
                    observed_at=datetime.now(UTC),
                ),
            ),
        )

    async def configured_choices(self, task: str) -> ModelChoiceCatalog:
        """The DISPLAY catalog: configuration only, so no evidence is claimed."""
        self.choice_calls.append(task)
        return ModelChoiceCatalog(
            task=task,
            choices=(ModelChoice(model="stub", provider="stub", lane=Lane.FRONTIER_API),),
        )

    async def authorize(self, **kwargs: Any) -> AuthorizedModelSelection:
        self.authorize_calls.append(dict(kwargs))
        if self.deny:
            raise ModelSelectionPolicyRefused("model selection was not allowed by policy")
        return AuthorizedModelSelection(
            model="stub",
            lane=Lane.FRONTIER_API,
            observed_at=datetime.now(UTC),
            policy_effect="allow",
            policy_version="workflow-test-policy",
        )


def test_meridian_b1_b2_b3_run_on_governed_deterministic_engine(
    tmp_path: Path, engines: list[Any]
) -> None:
    """The three deterministic definitions stay first-class beside agentic mode."""
    fixture = _meridian_fixture_dir()
    truth = json.loads((fixture / "ground_truth.json").read_bytes())
    gateway = ScriptedGateway()
    app, verifier, crm = _meridian_workflow_app(tmp_path, key="wf-meridian", gateway=gateway)
    state = get_state(app)
    engines.extend(state.engines)
    expected_tools = {
        "read.kg.customers_in_counties",
        "read.kg.autoloans_for_customer",
        "read.kg.payments_due_window",
        "read.kg.servicing_notes_for_loan",
        "read.kg.open_disputes_by_status",
        "read.kg.disputes_directory",
    }
    meridian_tool_specs = [
        spec.name for spec in state.executor.tool_specs if spec.name in expected_tools
    ]
    assert len(meridian_tool_specs) == 6
    assert set(meridian_tool_specs) == expected_tools

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            headers = _mint_meridian_ops(verifier)
            listed = await client.get("/api/v1/workflows", headers=headers)
            assert listed.status_code == 200, listed.text
            meridian = {row["workflow_id"]: row for row in listed.json()}
            assert set(meridian) == {
                "meridian-b1-disaster-hardship",
                "meridian-b2-current-sop",
                "meridian-b3-fabrication-refusal",
                "meridian-s9-overdue-provisional-credit",
            }
            assert {workflow_id: row["writes"] for workflow_id, row in meridian.items()} == {
                "meridian-b1-disaster-hardship": [],
                "meridian-b2-current-sop": [],
                "meridian-b3-fabrication-refusal": [],
                "meridian-s9-overdue-provisional-credit": [],
            }
            for unavailable_id in ("unresolved-ticket-review", "recurring-issue-sweep"):
                unavailable = await client.post(
                    f"/api/v1/workflows/{unavailable_id}/run", headers=headers
                )
                assert unavailable.status_code == 404
                assert unavailable.json()["detail"] == ("workflow not available in this deployment")
            for row in meridian.values():
                assert row["modes"][0]["mode"] == "deterministic"
                assert row["modes"][0]["status"] == "available"
                assert row["modes"][0]["endpoint"].startswith("/v1/workflows/")
                if row["workflow_id"] == "meridian-s9-overdue-provisional-credit":
                    # S9 is deterministic-only BY DESIGN: its agentic contrast is
                    # the Meridian Analyst itself, not an agentic workflow twin.
                    assert row["modes"][1]["mode"] == "agentic"
                    assert row["modes"][1]["status"] == "pending"
                    continue
                assert row["modes"][1] == {
                    "mode": "agentic",
                    "status": "available",
                    "endpoint": f"/v1/agentic-workflows/{row['workflow_id']}/run",
                    "reason": "",
                }

            b1 = await client.post(
                "/api/v1/workflows/meridian-b1-disaster-hardship/run?max_subjects=5",
                headers=headers,
            )
            assert b1.status_code == 200, b1.text
            b1_view = b1.json()
            assert b1_view["proposals"] == []
            assert b1_view["result"]["outcome"] == "complete"
            assert [row["subject_ref"] for row in b1_view["result"]["findings"]] == [
                row["loan_id"] for row in truth["b1"]["actionable_rows"]
            ]
            distractors = {
                row["loan_id"] for row in truth["b1"]["distractor_rows"] if row["loan_id"]
            }
            assert not distractors & {row["subject_ref"] for row in b1_view["result"]["findings"]}
            b1_tools = {step["tool"] for step in b1_view["steps"] if step["tool"]}
            assert b1_tools == {
                "read.kg.customers_in_counties",
                "read.kg.autoloans_for_customer",
                "read.kg.payments_due_window",
                "read.kg.servicing_notes_for_loan",
            }
            assert all(
                step["outcome"] == "served-complete"
                for step in b1_view["steps"]
                if step["kind"] == "read"
            )

            b2 = await client.post(
                "/api/v1/workflows/meridian-b2-current-sop/run?max_subjects=5",
                headers=headers,
            )
            assert b2.status_code == 200, b2.text
            b2_result = b2.json()["result"]
            assert b2_result["outcome"] == "complete"
            assert b2_result["policy_artifact_id"] == truth["b2"]["current_artifact_id"]
            assert b2_result["policy_version"] == "4.0"
            assert [row["subject_ref"] for row in b2_result["findings"]] == truth["b2"][
                "current_answer_dispute_ids"
            ]
            assert b2_result["findings"][0]["detail"] == {
                "amount_cents": 65000,
                "business_days_elapsed": 6,
                "deadline_business_days": 5,
            }

            b3 = await client.post(
                "/api/v1/workflows/meridian-b3-fabrication-refusal/run?max_subjects=5",
                headers=headers,
            )
            assert b3.status_code == 200, b3.text
            b3_result = b3.json()["result"]
            assert b3_result["outcome"] == "refused"
            assert b3_result["summary"] == truth["b3"]["expected_response"]
            assert b3_result["missing_fields"] == truth["b3"]["missing_fields"]
            assert b3_result["control_outcome"] == "complete"
            assert b3_result["control_amount_cents"] == truth["b3"]["control_answer_cents"]
            assert sorted(ref.rsplit(":", 1)[-1] for ref in b3_result["control_evidence_refs"]) == [
                "mer-dsp-003",
                "mer-dsp-005",
            ]

            # S9: the deterministic overdue-provisional-credit read — one
            # argument-free verified call, the served-cell comparison only, and
            # BYTE-IDENTICAL across two runs (trace ids aside).
            s9_views = []
            for _ in range(2):
                s9 = await client.post(
                    "/api/v1/workflows/meridian-s9-overdue-provisional-credit/run?max_subjects=5",
                    headers=headers,
                )
                assert s9.status_code == 200, s9.text
                s9_views.append(s9.json())
            s9_view = s9_views[0]
            assert s9_view["result"]["outcome"] == "complete"
            assert [row["subject_ref"] for row in s9_view["result"]["findings"]] == truth["b2"][
                "current_answer_dispute_ids"
            ]
            s9_finding = s9_view["result"]["findings"][0]
            assert s9_finding["outcome"] == "overdue-provisional-credit"
            assert s9_finding["detail"]["deadline_policy"] == truth["b2"]["current_artifact_id"]
            assert s9_finding["detail"]["as_of_date"] > s9_finding["detail"]["deadline_date"]
            s9_tools = {step["tool"] for step in s9_view["steps"] if step["tool"]}
            assert s9_tools == {"read.kg.overdue_provisional_credit_disputes"}
            stripped = [dict(view, trace_id="") for view in s9_views]
            assert stripped[0] == stripped[1]

            # A caller may hold the ops ROLE while lacking dispute DATA labels.
            # The run is denied at the verified read; it never sees a cross-role row.
            denied = await client.post(
                "/api/v1/workflows/meridian-b2-current-sop/run?max_subjects=5",
                headers=_mint_meridian_ops(verifier, full=False),
            )
            assert denied.status_code == 403
            assert denied.json()["detail"] == "read-scope-denied"

            # Read-only Meridian composition has no write clients or provider call.
            assert crm is None
            assert gateway.requests == []

            # Every read traversed the governed executor: tool execution is audited
            # under the caller and the PDP emitted its independent decision facts.
            from ne.audit.replay import replay

            tool_event_count = 0
            for view in (b1_view, b2.json(), b3.json()):
                events = replay(state.audit_engine, trace_id=view["trace_id"]).events
                tool_events = [event for event in events if event.action == "tool.execute"]
                assert tool_events
                tool_event_count += len(tool_events)
                assert {event.actor for event in tool_events} == {"user:meridian-ops"}
                assert all((event.resource or "").startswith("tool:read.") for event in tool_events)

            import sqlalchemy as sa

            with state.audit_engine.connect() as conn:
                pdp_rows = conn.execute(
                    sa.text(
                        "SELECT actor FROM ne_audit_log "
                        "WHERE action = 'pdp.decide' AND actor = :actor"
                    ),
                    {"actor": "user:meridian-ops"},
                ).fetchall()
            assert len(pdp_rows) >= tool_event_count

    run(_drive())


def test_combined_acme_lift_then_meridian_fixture_preserves_both_projections(
    tmp_path: Path, engines: list[Any]
) -> None:
    """The complete Acme reconcile runs before the independent Meridian projection."""
    from ne.brain_ontology import graph_iri_for_label, graphs_for_labels

    fixture = _meridian_fixture_dir()
    truth = json.loads((fixture / "ground_truth.json").read_bytes())
    app, verifier, _crm = _workflow_app(
        tmp_path,
        key="wf-combined-acme-meridian",
        store=_seeded_store(tmp_path),
        graph_store=_seeded_graph_store(),
        write_actions_enabled=False,
        verified_templates_dir=_meridian_templates_dir(tmp_path),
        demo_meridian_fixture_path=str(fixture),
        demo_ontology_lift=True,
    )
    state = get_state(app)
    engines.extend(state.engines)
    assert state.ontology is not None
    store = state.ontology.store

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            headers = _mint_meridian_ops(verifier)

            # The existing Acme projection remains queryable after the generic lift.
            acme = await client.post(
                "/api/v1/verified/invoke",
                json={"template_id": "accounts-directory", "args": {}},
                headers=headers,
            )
            assert acme.status_code == 200, acme.text
            answer = acme.json()["answer"]
            acme_rows = [dict(zip(answer["columns"], row, strict=True)) for row in answer["rows"]]
            assert {row["name"] for row in acme_rows} == {"acc_hot Corp", "acc_cold Corp"}

            bank_labels = (
                "bank.customer.read",
                "bank.servicing.read",
                "bank.dispute.read",
            )
            expected_graphs = {
                graph_iri_for_label("all-employees"),
                *(graph_iri_for_label(label) for label in bank_labels),
            }
            assert set(await store.acl_graphs()) == expected_graphs
            for label in bank_labels:
                rows = await store.sparql_scoped(
                    "SELECT ?s ?p ?o WHERE { ?s ?p ?o }",
                    dataset=graphs_for_labels((label,)),
                )
                assert rows, f"expected non-empty Meridian ACL graph for {label}"

            # B1 consumes all three retained bank projections and returns the
            # generator-bound actionable loans, never an empty success.
            b1 = await client.post(
                "/api/v1/workflows/meridian-b1-disaster-hardship/run?max_subjects=5",
                headers=headers,
            )
            assert b1.status_code == 200, b1.text
            assert [row["subject_ref"] for row in b1.json()["result"]["findings"]] == [
                row["loan_id"] for row in truth["b1"]["actionable_rows"]
            ]

    run(_drive())


def test_meridian_fixture_path_requires_verified_answers(tmp_path: Path) -> None:
    """A configured banking projection never silently loads without its tools."""
    reset_config_hash_for_testing()
    seams, _ = _seams(tmp_path, key="wf-meridian-verified-off")
    with pytest.raises(ValueError, match="requires verified answers enabled"):
        build_platform(
            _api_settings(demo_meridian_fixture_path=str(_meridian_fixture_dir())), seams
        )


def test_meridian_fixture_path_requires_all_six_templates(tmp_path: Path) -> None:
    templates_dir = _meridian_templates_dir(tmp_path)
    omitted = templates_dir / "servicing_notes_for_loan.toml"
    assert omitted.is_file()
    omitted.unlink()

    with pytest.raises(ValueError, match="servicing-notes-for-loan"):
        _workflow_app(
            tmp_path,
            key="wf-meridian-missing-template",
            store=_seeded_store(tmp_path),
            write_actions_enabled=False,
            verified_templates_dir=templates_dir,
            demo_meridian_fixture_path=str(_meridian_fixture_dir()),
        )


@pytest.mark.parametrize("case", ["missing", "manifest-mismatch"])
def test_meridian_fixture_path_missing_or_malformed_fails_boot(tmp_path: Path, case: str) -> None:
    fixture = tmp_path / case / "meridian-v1"
    if case == "manifest-mismatch":
        shutil.copytree(_meridian_fixture_dir(), fixture)
        banking = fixture / "banking.json"
        banking.write_bytes(banking.read_bytes() + b" ")

    templates_dir = _meridian_templates_dir(tmp_path)
    expected = FileNotFoundError if case == "missing" else ValueError
    match = "manifest.json" if case == "missing" else "disagrees with manifest sha256"
    with pytest.raises(expected, match=match):
        _workflow_app(
            tmp_path,
            key=f"wf-meridian-{case}",
            store=_seeded_store(tmp_path),
            write_actions_enabled=False,
            verified_templates_dir=templates_dir,
            demo_meridian_fixture_path=str(fixture),
        )


def test_meridian_agentic_run_is_model_planned_caller_pinned_and_persisted(
    tmp_path: Path, engines: list[Any]
) -> None:
    """A real model-chosen B3 read traverses the frozen executor and records evidence."""
    gateway = ScriptedGateway(
        [
            tool_call_completion(
                "read.kg.disputes_directory",
                {"step_intent": "Establish the complete dispute evidence directory."},
            ),
            text_completion(
                json.dumps(
                    {
                        "final_text": (
                            "Refuse the late-credit total because issue dates were not served; "
                            "the recorded-credit control is supported by the served dispute "
                            "identities."
                        ),
                        "claims": [],
                    }
                )
            ),
        ]
    )
    app, verifier, _crm = _meridian_workflow_app(
        tmp_path, key="wf-meridian-agentic", gateway=gateway
    )
    state = get_state(app)
    engines.extend(state.engines)

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.post(
                "/api/v1/agentic-workflows/meridian-b3-fabrication-refusal/run",
                headers=_mint_meridian_ops(verifier),
            )
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["model_planned"] is True
        assert view["outcome"] == "complete"
        assert view["answer"].startswith("Refuse the late-credit total")
        assert view["steps"] == [
            {
                "step": 1,
                "tool": "read.kg.disputes_directory",
                "stated_intent": "Establish the complete dispute evidence directory.",
                "outcome": "served",
                "served_row_ids": view["steps"][0]["served_row_ids"],
                "evidence_refs": [],
                "principal_id": "user:meridian-ops",
            }
        ]
        assert len(view["steps"][0]["served_row_ids"]) >= 6
        assert set(view["steps"][0]["served_row_ids"]) <= set(view["served_row_ids"])
        assert view["budgets"]["tool_calls_used"] == 1

        import sqlalchemy as sa

        with state.audit_engine.connect() as conn:
            tool_rows = conn.execute(
                sa.text(
                    "SELECT action, actor, resource FROM ne_audit_log "
                    "WHERE trace_id = :trace_id AND action = 'tool.execute'"
                ),
                {"trace_id": view["trace_id"]},
            ).fetchall()
        assert tool_rows == [
            (
                "tool.execute",
                "user:meridian-ops",
                "tool:read.kg.disputes_directory",
            )
        ]

        from ne.api.agentic_workflows import read_agentic_run

        persisted = read_agentic_run(state.audit_engine, view["run_id"])
        assert persisted is not None
        assert persisted.model_dump(mode="json") == view

    run(_drive())


def test_agentic_start_returns_opaque_ticket_and_live_trace_before_terminal(
    tmp_path: Path, engines: list[Any]
) -> None:
    """The async door exposes the existing trace while the final model call waits."""

    class PausingGateway(ScriptedGateway):
        def __init__(self) -> None:
            super().__init__(
                [
                    tool_call_completion(
                        "read.kg.disputes_directory",
                        {"step_intent": "Establish the complete dispute evidence directory."},
                    ),
                    text_completion(
                        json.dumps(
                            {
                                "final_text": "The served directory supports the control answer.",
                                "claims": [],
                            }
                        )
                    ),
                ]
            )
            self.final_started = asyncio.Event()
            self.release_final = asyncio.Event()

        async def complete(self, request: Any) -> Any:
            if len(self.requests) == 1:
                self.final_started.set()
                await self.release_final.wait()
            return await super().complete(request)

    gateway = PausingGateway()
    app, verifier, _crm = _meridian_workflow_app(
        tmp_path, key="wf-meridian-agentic-live", gateway=gateway
    )
    state = get_state(app)
    engines.extend(state.engines)

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            started = await client.post(
                "/api/v1/agentic-workflows/meridian-b3-fabrication-refusal/start",
                headers=_mint_meridian_ops(verifier),
            )
            assert started.status_code == 202, started.text
            ticket = started.json()
            assert ticket["status"] == "running"
            assert ticket["result"] is None
            assert re.fullmatch(r"[0-9a-f]{32}", ticket["run_id"])
            assert ticket["run_id"] != ticket["trace_id"]

            await asyncio.wait_for(gateway.final_started.wait(), timeout=2)
            pending = await client.get(
                f"/api/v1/agentic-workflows/runs/{ticket['run_id']}",
                headers=_mint_meridian_ops(verifier),
            )
            assert pending.status_code == 200
            assert pending.json()["status"] == "running"
            assert pending.json()["result"] is None

            # This is the EXISTING redacted trace assembler, not a parallel
            # live-event payload. It is already populated before the run ends.
            trace = await client.get(
                f"/api/v1/trace/{ticket['trace_id']}",
                headers=_mint_meridian_ops(verifier),
            )
            assert trace.status_code == 200, trace.text
            assert trace.json()["trace_id"] == ticket["trace_id"]
            assert any(node["action"] == "tool.execute" for node in trace.json()["nodes"])

            gateway.release_final.set()
            terminal = pending
            for _ in range(100):
                terminal = await client.get(
                    f"/api/v1/agentic-workflows/runs/{ticket['run_id']}",
                    headers=_mint_meridian_ops(verifier),
                )
                if terminal.json()["status"] != "running":
                    break
                await asyncio.sleep(0)
            assert terminal.json()["status"] == "complete"
            assert terminal.json()["result"]["run_id"] == ticket["run_id"]
            assert terminal.json()["result"]["trace_id"] == ticket["trace_id"]

    run(_drive())


def test_agentic_choice_surface_maps_to_reason_task_and_background_carries_selection(
    tmp_path: Path, engines: list[Any]
) -> None:
    """One authorized alias/lane/trace survives the HTTP-to-background boundary."""
    gateway = ScriptedGateway(
        [
            tool_call_completion(
                "read.kg.disputes_directory",
                {"step_intent": "Establish the complete dispute evidence directory."},
            ),
            text_completion(
                json.dumps(
                    {
                        "final_text": "The served directory supports the control answer.",
                        "claims": [],
                    }
                )
            ),
        ]
    )
    app, verifier, _crm = _meridian_workflow_app(
        tmp_path, key="wf-agentic-selection", gateway=gateway
    )
    state = get_state(app)
    engines.extend(state.engines)
    selection = _WorkflowSelection()
    state.model_selection = selection  # type: ignore[assignment]

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            headers = _mint_meridian_ops(verifier)
            choices = await client.get("/api/v1/models/choices/agentic-workflow", headers=headers)
            assert choices.status_code == 200, choices.text
            assert choices.json()["surface"] == "agentic-workflow"
            assert choices.json()["task"] == "reason"
            assert [row["model"] for row in choices.json()["choices"]] == ["stub"]

            started = await client.post(
                "/api/v1/agentic-workflows/meridian-b3-fabrication-refusal/start",
                json={"requested_model": "stub"},
                headers=headers,
            )
            assert started.status_code == 202, started.text
            ticket = started.json()
            terminal = started
            for _ in range(100):
                terminal = await client.get(
                    f"/api/v1/agentic-workflows/runs/{ticket['run_id']}", headers=headers
                )
                if terminal.json()["status"] != "running":
                    break
                await asyncio.sleep(0)
            assert terminal.json()["status"] == "complete", terminal.text

        assert selection.choice_calls == ["reason"]
        assert len(selection.authorize_calls) == 1
        decision_call = selection.authorize_calls[0]
        assert decision_call["task"] == "reason"
        assert decision_call["requested_model"] == "stub"
        assert decision_call["trace_id"] == ticket["trace_id"]
        assert [request.requested_model for request in gateway.requests] == ["stub", "stub"]
        assert [request.lane for request in gateway.requests] == [
            Lane.FRONTIER_API,
            Lane.FRONTIER_API,
        ]
        assert [request.trace_id for request in gateway.requests] == [
            ticket["trace_id"],
            ticket["trace_id"],
        ]

    run(_drive())


def test_agentic_selection_refusal_precedes_ticket_and_background_work(
    tmp_path: Path, engines: list[Any]
) -> None:
    gateway = ScriptedGateway()
    app, verifier, _crm = _meridian_workflow_app(
        tmp_path, key="wf-agentic-selection-denied", gateway=gateway
    )
    state = get_state(app)
    engines.extend(state.engines)
    selection = _WorkflowSelection(deny=True)
    state.model_selection = selection  # type: ignore[assignment]

    async def _drive() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            return await client.post(
                "/api/v1/agentic-workflows/meridian-b3-fabrication-refusal/start",
                json={"requested_model": "stub"},
                headers=_mint_meridian_ops(verifier),
            )

    response = run(_drive())
    assert response.status_code == 403
    assert response.json() == {"detail": "model selection was not allowed by policy"}
    assert len(selection.authorize_calls) == 1
    assert gateway.requests == []

    import sqlalchemy as sa

    with state.audit_engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM ne_agentic_workflow_runs")).scalar_one()
    assert count == 0


def test_run_reads_ranks_resolves_and_parks_one_approval_per_account(
    tmp_path: Path, engines: list[Any]
) -> None:
    """The assertion the demo rests on: a real multi-step governed run."""
    from ne.api.platform import WriteActionClients
    from ne.brain_ontology.config import OntologySettings

    crm, lookup = (
        _FakeCrm(),
        _FakeLookup({"acc_hot Corp": "001dL00002MiTaaAAB", "acc_cold Corp": "001dL00002MiTbbAAB"}),
    )
    reset_config_hash_for_testing()
    seams, verifier = _seams(
        tmp_path,
        key="wf-run",
        ontology_settings=OntologySettings(
            verified_answers_enabled=True,
            verified_templates_dir=_templates_dir(tmp_path),
            include_ontology_graph=False,
            backend="oxigraph",
        ),
    )
    seams = type(seams)(
        **{
            **{f.name: getattr(seams, f.name) for f in seams.__dataclass_fields__.values()},
            "write_action_clients": WriteActionClients(
                crm=crm, ticket=_FakeTicketWrite(), draft=_FakeDraftMail(), lookup=lookup
            ),
        }
    )
    # Hand the pre-seeded gateway to the 085a ``triple_store`` entry-point slot, so the
    # platform composes ITS verified-answer tools over the SAME store the tickets live
    # in (``resolve_triple_store`` returns it verbatim — no second oxigraph open).
    seeded = _seeded_store(tmp_path)
    seams.plugin_registry.register("triple_store", "oxigraph", lambda **_kw: seeded)  # type: ignore[union-attr]
    app = create_app(
        _api_settings(
            ops_roles=("ops",),
            approver_roles=("approver",),
            write_actions_enabled=True,
            workflows_enabled=True,
        ),
        seams=seams,
    )
    state = get_state(app)
    engines.extend(state.engines)

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            headers = _mint_ops(verifier)
            listed = await client.get("/api/v1/workflows", headers=headers)
            assert listed.status_code == 200, listed.text
            assert {w["workflow_id"] for w in listed.json()} == {
                "unresolved-ticket-review",
                "recurring-issue-sweep",
            }
            unavailable = await client.post(
                "/api/v1/workflows/meridian-b1-disaster-hardship/run", headers=headers
            )
            assert unavailable.status_code == 404
            assert unavailable.json()["detail"] == "workflow not available in this deployment"
            unknown = await client.post("/api/v1/workflows/does-not-exist/run", headers=headers)
            assert unknown.status_code == 404
            assert unknown.json()["detail"] == "unknown workflow"

            resp = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run?max_subjects=2",
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            view = resp.json()

            # (a) The READ is real: 3 unresolved tickets (open + pending) — never the
            #     resolved one — served by the verified template, not a constant.
            assert view["read_row_count"] == 3
            read_steps = [s for s in view["steps"] if s["tool"] == "read.kg.tickets_by_status"]
            assert {s["detail"]["status"] for s in read_steps} == {"open", "pending"}
            assert sum(int(s["detail"]["rows"]) for s in read_steps) == 3
            assert all(int(s["detail"]["unparsed_rows"]) == 0 for s in read_steps)

            # (b) The RANKING put the escalated account first.
            assert view["subjects_considered"] == 2
            assert view["subjects_selected"] == 2

            # (c) Each write resolved its target through the governed CRM READ.
            assert [name for _, name in lookup.calls] == ["acc_hot Corp", "acc_cold Corp"]

            # (d) Each proposal parked its OWN Tier-2 approval — one human decision
            #     per write, never one decision covering two.
            ids = [p["approval_request_id"] for p in view["proposals"]]
            assert all(i for i in ids), view["proposals"]
            assert len(set(ids)) == 2
            assert {p["outcome"] for p in view["proposals"]} == {"awaiting-approval"}

            # (e) NOTHING was written to the CRM yet: the gate is holding every one.
            assert crm.created == []

            approver = _mint_approver(verifier)
            pending = await client.get("/api/v1/approvals/pending", headers=approver)
            assert pending.status_code == 200
            rows = {r["request_id"]: r for r in pending.json()["approvals"]}
            assert set(ids) <= set(rows)
            assert all(rows[i]["action"] == "write.internal.crm.note" for i in ids)
            assert all(rows[i]["tier"] == 2 for i in ids)

            # (f) Every parked call carries ALL THREE required fields. This is the
            #     defect class the engine exists to make impossible: a field-missing
            #     call that only fails AFTER a human has spent a decision on it.
            for request_id in ids:
                payload = await client.get(
                    f"/api/v1/approvals/{request_id}/payload", headers=approver
                )
                assert payload.status_code == 200, payload.text
                params = payload.json()["material_parameters"]
                assert set(params) == {"record_id", "title", "body"}
                assert all(str(params[k]).strip() for k in params)

    run(_drive())


@pytest.mark.parametrize(
    ("workflow_id", "heading", "facts"),
    [
        (
            "unresolved-ticket-review",
            "On file at time of review",
            "2 unresolved tickets, 2 of them filed at high or urgent priority. "
            "Subjects on file: Widget shipment delayed, Widget will not power on.",
        ),
        (
            "recurring-issue-sweep",
            "Observed at time of review",
            '"Widget will not power on" has been filed by 2 different accounts; '
            "1 of those tickets is still unresolved. acc_hot Corp filed 1 of them.",
        ),
    ],
)
def test_long_model_draft_keeps_served_facts_in_parked_payload(
    tmp_path: Path,
    engines: list[Any],
    workflow_id: str,
    heading: str,
    facts: str,
) -> None:
    """The real composed write receives the bounded body, not the raw model tail-slice."""
    from ne.api.workflows import _BODY_MAX

    gateway = ScriptedGateway([text_completion("D" * (_BODY_MAX * 2))])
    app, verifier, _crm = _workflow_app(
        tmp_path, key=f"wf-long-draft-{workflow_id}", gateway=gateway
    )
    state = get_state(app)
    engines.extend(state.engines)

    async def _drive() -> str:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.post(
                f"/api/v1/workflows/{workflow_id}/run?max_subjects=1",
                headers=_mint_ops(verifier),
            )
            assert response.status_code == 200, response.text
            proposal = response.json()["proposals"][0]
            assert proposal["outcome"] == "awaiting-approval"
            payload = await client.get(
                f"/api/v1/approvals/{proposal['approval_request_id']}/payload",
                headers=_mint_approver(verifier),
            )
            assert payload.status_code == 200, payload.text
            return str(payload.json()["material_parameters"]["body"])

    body = run(_drive())
    trailer = f"\n\n{heading}: {facts}"
    assert len(body) == _BODY_MAX
    assert body == f"{'D' * (_BODY_MAX - len(trailer))}{trailer}"
    assert len(gateway.requests) == 1


def test_every_tool_the_run_uses_is_in_the_one_executor_menu(
    tmp_path: Path, engines: list[Any]
) -> None:
    """No workflow-only tool exists: the engine can only reach the governed menu."""
    from ne.api.platform import WriteActionClients, build_platform
    from ne.api.workflows import CRM_LOOKUP_TOOL, CRM_NOTE_TOOL, KNOWLEDGE_TOOL, TICKETS_TOOL
    from ne.brain_ontology.config import OntologySettings

    reset_config_hash_for_testing()
    seams, _ = _seams(
        tmp_path,
        key="wf-menu",
        ontology_settings=OntologySettings(
            verified_answers_enabled=True,
            verified_templates_dir=_templates_dir(tmp_path),
            include_ontology_graph=False,
            backend="oxigraph",
        ),
    )
    seams = type(seams)(
        **{
            **{f.name: getattr(seams, f.name) for f in seams.__dataclass_fields__.values()},
            "write_action_clients": WriteActionClients(
                crm=_FakeCrm(),
                ticket=_FakeTicketWrite(),
                draft=_FakeDraftMail(),
                lookup=_FakeLookup({}),
            ),
        }
    )
    seeded = _seeded_store(tmp_path)
    seams.plugin_registry.register("triple_store", "oxigraph", lambda **_kw: seeded)  # type: ignore[union-attr]
    platform = build_platform(
        _api_settings(write_actions_enabled=True, workflows_enabled=True), seams
    )
    engines.extend(platform.engines)
    menu = {s.name for s in platform.executor.tool_specs}
    assert {TICKETS_TOOL, KNOWLEDGE_TOOL, CRM_LOOKUP_TOOL, CRM_NOTE_TOOL} <= menu
    assert platform.workflow_dispatcher is not None


def test_recurring_issue_sweep_notifies_every_affected_account(
    tmp_path: Path, engines: list[Any]
) -> None:
    """The cross-account signal, end to end: one fault, two accounts, two approvals.

    The seeded graph gives ``acc_hot`` and ``acc_cold`` a byte-identical ticket subject
    (``Widget will not power on``) under DIFFERENT statuses, so a run that only read the
    unresolved statuses would miss it — which is precisely the read this workflow widens.
    """
    from ne.api.platform import WriteActionClients
    from ne.brain_ontology.config import OntologySettings

    crm = _FakeCrm()
    lookup = _FakeLookup(
        {"acc_hot Corp": "001dL00002MiTaaAAB", "acc_cold Corp": "001dL00002MiTbbAAB"}
    )
    reset_config_hash_for_testing()
    seams, verifier = _seams(
        tmp_path,
        key="wf-recur",
        ontology_settings=OntologySettings(
            verified_answers_enabled=True,
            verified_templates_dir=_templates_dir(tmp_path),
            include_ontology_graph=False,
            backend="oxigraph",
        ),
    )
    seams = type(seams)(
        **{
            **{f.name: getattr(seams, f.name) for f in seams.__dataclass_fields__.values()},
            "write_action_clients": WriteActionClients(
                crm=crm, ticket=_FakeTicketWrite(), draft=_FakeDraftMail(), lookup=lookup
            ),
        }
    )
    seeded = _seeded_store(tmp_path)
    seams.plugin_registry.register("triple_store", "oxigraph", lambda **_kw: seeded)  # type: ignore[union-attr]
    app = create_app(
        _api_settings(
            ops_roles=("ops",),
            approver_roles=("approver",),
            write_actions_enabled=True,
            workflows_enabled=True,
        ),
        seams=seams,
    )
    engines.extend(get_state(app).engines)

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            headers = _mint_ops(verifier)
            resp = await client.post(
                "/api/v1/workflows/recurring-issue-sweep/run?max_subjects=2", headers=headers
            )
            assert resp.status_code == 200, resp.text
            view = resp.json()

            # (a) EVERY status was read — the widened read is what surfaces a fault
            #     whose tickets have since been closed on some accounts.
            read_steps = [s for s in view["steps"] if s["tool"] == "read.kg.tickets_by_status"]
            assert {s["detail"]["status"] for s in read_steps} == {
                "open",
                "pending",
                "resolved",
                "closed",
            }

            # (b) Exactly one subject spans two accounts, and both were notified.
            assert view["subjects_considered"] == 1
            assert view["subjects_selected"] == 2
            assert len(view["proposals"]) == 2
            assert {p["outcome"] for p in view["proposals"]} == {"awaiting-approval"}
            assert len({p["approval_request_id"] for p in view["proposals"]}) == 2

            # (c) Still nothing written: two independent human decisions are owed.
            assert crm.created == []

            approver = _mint_approver(verifier)
            for proposal in view["proposals"]:
                payload = await client.get(
                    f"/api/v1/approvals/{proposal['approval_request_id']}/payload",
                    headers=approver,
                )
                assert payload.status_code == 200, payload.text
                params = payload.json()["material_parameters"]
                assert set(params) == {"record_id", "title", "body"}
                # (d) The served figures reach the approver VERBATIM, whatever prose
                #     the model produced: the cluster size and the unresolved count are
                #     reported SEPARATELY, so a mostly-closed cluster is never shown as
                #     if it were live. Here 2 accounts filed it and only 1 is still open.
                assert "filed by 2 different accounts" in params["body"]
                assert "1 of those tickets is still unresolved" in params["body"]
                assert '"Widget will not power on"' in params["body"]

    run(_drive())


# --- (4) failure honesty ------------------------------------------------------


class _FailingLookup(_FakeLookup):
    """Resolves like the fake, except the named subjects raise (the O'Brien case:
    one subject's lookup dying mid-run)."""

    def __init__(self, known: dict[str, str], fail: set[str]) -> None:
        super().__init__(known)
        self.fail = fail

    async def find_id_by_name(self, sobject: str, name: str) -> str | None:
        if name in self.fail:
            raise RuntimeError("lookup exploded")
        return await super().find_id_by_name(sobject, name)


def test_one_failing_subject_does_not_strand_the_others(tmp_path: Path, engines: list[Any]) -> None:
    """Subject 2's lookup dies; subject 1's parked approval MUST still reach the caller.

    The pre-fix behaviour (proven by the review's execution): the raise unwound the
    run into a bare 500 AFTER subject 1's Tier-2 approval was durably parked — a
    human left holding a decision for a run that reported total failure, and a
    caller with no request id to correlate it. Now the failing subject degrades to
    an honest per-proposal outcome naming the error CLASS, and the run returns 200
    with every parked id.
    """
    lookup = _FailingLookup({"acc_hot Corp": _HOT_ID}, fail={"acc_cold Corp"})
    app, verifier, crm = _workflow_app(tmp_path, key="wf-partial", lookup=lookup)
    state = get_state(app)
    engines.extend(state.engines)

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run?max_subjects=2",
                headers=_mint_ops(verifier),
            )
            assert resp.status_code == 200, resp.text
            view = resp.json()
            assert len(view["proposals"]) == 2

            # Subject 1 (the escalated account) parked normally and the caller HAS
            # its request id.
            first = view["proposals"][0]
            assert first["outcome"] == "awaiting-approval"
            assert first["approval_request_id"]

            # Subject 2 is an honest error outcome carrying the CLASSES (the
            # handler error wrapped by the executor), never a bare 500.
            second = view["proposals"][1]
            assert second["outcome"] == "failed"
            assert "ToolExecutionError: RuntimeError" in second["reason"]
            failed_steps = [s for s in view["steps"] if s["outcome"] == "failed"]
            assert [s["detail"]["error_class"] for s in failed_steps] == [
                "ToolExecutionError: RuntimeError"
            ]

            # The parked approval is REAL and visible to the approver.
            pending = await client.get(
                "/api/v1/approvals/pending", headers=_mint_approver(verifier)
            )
            assert first["approval_request_id"] in {
                r["request_id"] for r in pending.json()["approvals"]
            }
            assert crm.created == []

    run(_drive())


def test_gate_failure_is_reported_as_failure_not_timeout(
    tmp_path: Path, engines: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate DB failure surfaces immediately with its class — never a 100s stall.

    Pre-fix (proven by the review's execution): every proposal reported
    ``outcome="unconfirmed", reason="the approval row did not become visible in
    time"`` after stalling the FULL poll window per subject — a gate failure
    indistinguishable from a slow INSERT.
    """
    import time

    app, verifier, _crm = _workflow_app(tmp_path, key="wf-gatefail")
    state = get_state(app)
    engines.extend(state.engines)

    async def _boom(request: Any, *, args_hash: Any = None) -> Any:
        raise RuntimeError("gate database is down")

    monkeypatch.setattr(state.gate, "request_approval", _boom)

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            started = time.monotonic()
            resp = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run?max_subjects=2",
                headers=_mint_ops(verifier),
            )
            elapsed = time.monotonic() - started
            assert resp.status_code == 200, resp.text
            view = resp.json()
            outcomes = {p["outcome"] for p in view["proposals"]}
            assert outcomes == {"failed"}, view["proposals"]
            for proposal in view["proposals"]:
                assert "RuntimeError" in proposal["reason"]
                assert "did not become visible" not in proposal["reason"]
            # Immediate, not timeout-shaped: the pre-fix path stalled >= 40s here
            # (2 subjects x the 20s window); one window is a generous ceiling even
            # under CI load.
            assert elapsed < 20, f"stalled {elapsed:.1f}s — refusal misreported as timeout"

    run(_drive())


def test_pdp_deny_is_reported_as_a_refusal(
    tmp_path: Path, engines: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A policy deny on the write is the platform refusing — named as such, immediately."""
    from ne.contracts.governance import DecisionEffect, PolicyDecision

    app, verifier, crm = _workflow_app(tmp_path, key="wf-pdpdeny")
    state = get_state(app)
    engines.extend(state.engines)

    real_pdp = state.executor._pdp  # type: ignore[attr-defined]

    class _DenyCrmWrites:
        async def decide(self, request: Any) -> PolicyDecision:
            if request.action == "write.internal.crm.note":
                return PolicyDecision(
                    effect=DecisionEffect.DENY, policy_version="test", reason="denied by test"
                )
            return await real_pdp.decide(request)

    monkeypatch.setattr(state.executor, "_pdp", _DenyCrmWrites())

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run?max_subjects=2",
                headers=_mint_ops(verifier),
            )
            assert resp.status_code == 200, resp.text
            view = resp.json()
            assert {p["outcome"] for p in view["proposals"]} == {"refused"}, view["proposals"]
            for proposal in view["proposals"]:
                assert "ToolDeniedError" in proposal["reason"]
                # A PDP deny raises before the gate submit, so "before it was
                # parked" is TRUE here — pinned as the control for the classes
                # (reject/expiry) whose reasons must NOT say it.
                assert "before it was parked" in proposal["reason"]
            assert crm.created == []

    run(_drive())


def test_task_death_after_park_reports_the_parked_row_id(
    tmp_path: Path, engines: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task that dies AFTER its approval row is durable must report the row's id.

    The window: the gate WRITE lands the pending row, then the parked task dies
    inside the same poll gap (here: the decision-wait raising after a successful
    submit — the mid-decision-wait DB error / post-insert audit failure shape).
    Pre-fix, ``new_request_id`` checked ``parked.done()`` BEFORE the fresh-row
    read, so the run reported ``failed`` with a "before it was parked" reason
    while a pending row a human can see sat in the queue with no id delivered to
    the caller. The fresh-row read now runs first: a TRUE refusal produces no
    row at all (every pre-gate refusal class raises before the gate submit), so
    the refusal fast path loses nothing — see
    ``test_gate_failure_is_reported_as_failure_not_timeout``, which stays green
    and fast as this test's control.
    """
    app, verifier, crm = _workflow_app(tmp_path, key="wf-parkdie")
    state = get_state(app)
    engines.extend(state.engines)

    real_submit = state.gate.submit

    async def _park_then_die(request: Any, *, args_hash: Any = None) -> Any:
        await real_submit(request, args_hash=args_hash)
        raise RuntimeError("gate database died mid-decision-wait")

    monkeypatch.setattr(state.gate, "request_approval", _park_then_die)

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run?max_subjects=2",
                headers=_mint_ops(verifier),
            )
            assert resp.status_code == 200, resp.text
            view = resp.json()
            assert len(view["proposals"]) == 2

            # The rows are REAL and pending; the caller must hold both ids.
            pending = await client.get(
                "/api/v1/approvals/pending", headers=_mint_approver(verifier)
            )
            pending_ids = {r["request_id"] for r in pending.json()["approvals"]}
            for proposal in view["proposals"]:
                assert proposal["outcome"] == "awaiting-approval", view["proposals"]
                assert proposal["approval_request_id"], view["proposals"]
                assert proposal["approval_request_id"] in pending_ids
                assert "before it was parked" not in proposal["reason"]
            assert len({p["approval_request_id"] for p in view["proposals"]}) == 2
            assert crm.created == []

    run(_drive())


def test_same_instant_reject_reads_as_a_human_decision(
    tmp_path: Path, engines: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reject decided inside the poll gap is reported as a HUMAN decision.

    A reject (or expiry) happens AFTER parking, by definition — only a parked
    row can be decided — so the old blanket reason ("the platform refused this
    write before it was parked") was false every time this class surfaced, and
    mislabelled a human decision as a platform refusal. Only the reject's TIMING
    is injected here: the row is really submitted, really rejected through the
    gate, and the middleware's own decision-wait raises the real
    ``ApprovalNotGrantedError``.
    """
    app, verifier, crm = _workflow_app(tmp_path, key="wf-instantrej")
    state = get_state(app)
    engines.extend(state.engines)

    real_submit = state.gate.submit
    real_await = state.gate.await_outcome

    async def _reject_before_observed(request: Any, *, args_hash: Any = None) -> Any:
        request_id = await real_submit(request, args_hash=args_hash)
        await state.gate.reject(
            request_id, approver="user:approver-demo", reason="same-instant reject"
        )
        return await real_await(request_id)

    monkeypatch.setattr(state.gate, "request_approval", _reject_before_observed)

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run?max_subjects=2",
                headers=_mint_ops(verifier),
            )
            assert resp.status_code == 200, resp.text
            view = resp.json()
            assert {p["outcome"] for p in view["proposals"]} == {"refused"}, view["proposals"]
            for proposal in view["proposals"]:
                assert "ApprovalNotGrantedError" in proposal["reason"]
                # The two claims that were categorically false pre-fix:
                assert "before it was parked" not in proposal["reason"]
                assert "the platform refused" not in proposal["reason"]
                # The claim that is true: a decision, after parking.
                assert "rejected by a human approver" in proposal["reason"]
                assert "after it was parked" in proposal["reason"]
            # The rejected rows are terminal — nothing pending, nothing written.
            pending = await client.get(
                "/api/v1/approvals/pending", headers=_mint_approver(verifier)
            )
            assert pending.json()["approvals"] == []
            assert crm.created == []

    run(_drive())


def test_subject_leg_refusal_is_reported_as_refused(
    tmp_path: Path, engines: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal is a refusal WHICHEVER leg it lands on — reads included.

    Pre-fix the refused/failed split lived only in ``submit_proposal``, so the
    same ``ToolDeniedError`` was ``refused`` on the write leg but ``failed`` on
    a subject's knowledge READ — a position-dependent taxonomy. Now
    ``_subject_failure_view`` applies the same ``_GOVERNANCE_REFUSAL_CLASSES``
    classification. Control (stays ``failed``):
    ``test_one_failing_subject_does_not_strand_the_others`` — a WRAPPED handler
    error (``ToolExecutionError: RuntimeError``) is infrastructure, not
    governance, and an over-broad classifier that called every subject failure
    a refusal would turn it red.
    """
    from ne.contracts.governance import DecisionEffect, PolicyDecision

    app, verifier, crm = _workflow_app(tmp_path, key="wf-subjrefuse")
    state = get_state(app)
    engines.extend(state.engines)

    real_pdp = state.executor._pdp  # type: ignore[attr-defined]

    class _DenyKnowledgeReads:
        async def decide(self, request: Any) -> PolicyDecision:
            if request.action == "read.knowledge.search":
                return PolicyDecision(
                    effect=DecisionEffect.DENY, policy_version="test", reason="denied by test"
                )
            return await real_pdp.decide(request)

    monkeypatch.setattr(state.executor, "_pdp", _DenyKnowledgeReads())

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run?max_subjects=2",
                headers=_mint_ops(verifier),
            )
            assert resp.status_code == 200, resp.text
            view = resp.json()
            assert {p["outcome"] for p in view["proposals"]} == {"refused"}, view["proposals"]
            for proposal in view["proposals"]:
                assert "ToolDeniedError" in proposal["reason"]
                assert "refused this subject's leg" in proposal["reason"]
            refused_steps = [s for s in view["steps"] if s["kind"] == "subject"]
            assert refused_steps
            assert {s["outcome"] for s in refused_steps} == {"refused"}
            # No write leg was reached: nothing parked, nothing written.
            pending = await client.get(
                "/api/v1/approvals/pending", headers=_mint_approver(verifier)
            )
            assert pending.json()["approvals"] == []
            assert crm.created == []

    run(_drive())


def test_refusal_set_decides_every_runtime_error_class() -> None:
    """The refusal/failure boundary is a DECIDED set, not a default.

    Every exception class ne-runtime exports must sit in exactly one bucket:
    ``_GOVERNANCE_REFUSAL_CLASSES`` (the platform/gate refusing, loudly) or this
    test's justified non-refusal ledger (degrades to the equally-honest
    ``failed``). A new sibling class added to ``ne.runtime.errors`` fails HERE
    until its bucket is decided — it can no longer silently fall through to
    ``"failed"``. Both directions are pinned: an UNDECIDED class fails the first
    assert; a class claimed by BOTH buckets fails the overlap assert.
    """
    import inspect

    from ne.api.workflows import _GOVERNANCE_REFUSAL_CLASSES
    from ne.runtime import errors as rt_errors

    hierarchy = {
        name
        for name, obj in vars(rt_errors).items()
        if inspect.isclass(obj) and issubclass(obj, rt_errors.NeRuntimeError)
    }
    non_refusal = {
        # Class -> why "failed" (not "refused") is the honest bucket for it.
        "NeRuntimeError": "the abstract base names no decision at all",
        "ToolRegistrationError": "a boot-time misconfiguration, not a per-call refusal",
        "ToolExecutionError": "the handler failed — infrastructure, whatever the cause",
        "RuntimeAuditError": "the audit append failed — fail-closed infrastructure",
        "RunApprovalAuthorityRetryableError": (
            "the authority floor could not be ESTABLISHED — a missing head "
            "receipt, an ACL snapshot that has not landed, an invocation that "
            "no longer parses. Nothing refused anything and nothing was "
            "decided: the approval stays PENDING and the caller retries. "
            "'refused' would tell a reader the platform declined the write when "
            "it only failed to get an answer, and the class cannot reach _park "
            "anyway (the standing-successor seam catches it and parks)"
        ),
        "ApprovalRequestNotFoundError": "an approvals-API lookup miss, not a write refusal",
        "ApprovalAlreadyDecidedError": "an approvals-API conflict, not a write refusal",
        "ApprovalSourceWithheldFromApproverError": (
            "the approver-disclosure surface (U2): CURRENT source authority does "
            "not admit ONE reading/deciding principal, so disclosure/decision is "
            "refused for that principal ALONE and the approval row STAYS PENDING "
            "for another approver — nothing decided the subject's write, and the "
            "class never reaches a workflow leg (the waiter keeps waiting on the "
            "still-pending row). Calling it 'refused' would tell a reader the "
            "WRITE was declined when only one reader was"
        ),
        "SessionDepartmentMismatchError": "session scoping, upstream of any tool call",
        "DurabilityUnavailableError": "infrastructure availability, not governance",
        "SubAgentDispatchError": "drafting-leg failures fall back deterministically",
        "UnknownAgentTypeError": "drafting-leg (see SubAgentDispatchError)",
        "DispatchCapExceededError": "drafting-leg (see SubAgentDispatchError)",
        "SubAgentRoutePolicyDeniedError": "drafting-leg (see SubAgentDispatchError)",
        "ToolsetViolationError": (
            "toolset gating refuses a DISPATCHED agent's out-of-set call; the "
            "workflow's own legs run on the unscoped executor, and a drafting-leg "
            "surface falls back deterministically"
        ),
        "ToolsetConfigurationError": "a boot-time misconfiguration, not a per-call refusal",
        "AgenticNoProgressError": (
            "the model-planned loop stopping because its reads found nothing; "
            "nothing refused anything, and the deterministic workflow legs do "
            "not run that loop at all. It publishes its own stated reason on "
            "the Run record (safe_reason -> failure_reason), which is where a "
            "reader is told WHY - the bucket here only decides the word"
        ),
        "AgentOutputContractError": (
            "NOTHING REFUSED ANYTHING. The model exhausted its bounded output-"
            "retry budget without ever producing a reply that satisfies the run's "
            "answer contract, so the run has no usable output — that is an "
            "unusable ANSWER, not a gate, a guardrail, a PDP decision or a "
            "validation refusal, and calling it 'refused' would tell a reader the "
            "platform declined to act when in fact it tried and the reply came "
            "back unvalidatable. Like AgenticNoProgressError it publishes its own "
            "server-authored safe_reason on the Run record (-> failure_reason), "
            "which is where the reader is told WHY; the bucket here only decides "
            "the word, and 'failed' is the honest one"
        ),
        "RunActivationError": (
            "a pre-flight precondition on the WORKER's run-start path — the "
            "catalog re-check, the composed-binding re-check, the Skill-surface "
            "activation — raised before the model is called and upstream of any "
            "workflow write leg, so it can never reach _park and is not a "
            "governed-write refusal. Like AgenticNoProgressError it publishes "
            "its own server-authored safe_reason on the Run record "
            "(-> failure_reason), which is where the reader is told WHY; the "
            "bucket here only decides the word, and 'failed' is the honest one "
            "for a run that could not start"
        ),
        "RunSkillActivationError": (
            "the pre-flight Skill-exactness refusal, a RunActivationError "
            "sibling raised on the same worker run-start path (see "
            "RunActivationError): activation/pre-flight, upstream of any "
            "workflow write leg, with its own safe_reason on the Run record"
        ),
        "SchedulingError": "the scheduler surface — never reaches a workflow leg",
        "AgentMemoryError": "the memory surface — never reaches a workflow leg",
        "MemoryEventNotFoundError": "the memory surface — never reaches a workflow leg",
        "MemoryPromotionError": "the memory surface — never reaches a workflow leg",
        "MemoryPromotionAuditError": "the memory surface — never reaches a workflow leg",
    }
    undecided = hierarchy - _GOVERNANCE_REFUSAL_CLASSES - set(non_refusal)
    assert undecided == set(), (
        f"undecided ne-runtime error class(es) {sorted(undecided)}: add each to "
        "_GOVERNANCE_REFUSAL_CLASSES or to this test's non-refusal ledger, with the reason"
    )
    overlap = _GOVERNANCE_REFUSAL_CLASSES & set(non_refusal)
    assert overlap == set(), f"class(es) claimed by both buckets: {sorted(overlap)}"
    # The ledger may not quietly carry entries the hierarchy no longer has (a
    # rename would otherwise leave a stale justification pinning nothing).
    assert set(non_refusal) <= hierarchy
    # Exactly one refusal-set member lives outside ne-runtime, and it is the
    # guardrail engine's (ne-govern). Pins that the set stays import-derived:
    # a misspelled string literal could never satisfy this.
    assert _GOVERNANCE_REFUSAL_CLASSES - hierarchy == {"GuardrailViolation"}


def test_operator_without_data_label_gets_403_not_500(tmp_path: Path, engines: list[Any]) -> None:
    """C7 fail-closed scope refusal maps to an honest 403 (was: unhandled -> 500).

    ``groups=("ops",)`` holds the ROLE but not the ``all-employees`` DATA label the
    verified template is scoped to, so ``scoped_dataset`` raises — the outcome for
    this template is binary (full table or refusal), and the refusal is an
    authorization outcome, not a server fault.
    """
    app, verifier, _crm = _workflow_app(tmp_path, key="wf-scope")
    state = get_state(app)
    engines.extend(state.engines)

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            token = verifier.mint("user:ops-only", groups=("ops",), department="operations")
            resp = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run?max_subjects=2",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 403, resp.text
            assert resp.json()["detail"] == "read-scope-denied"
            # Nothing was parked for the refused run.
            pending = await client.get(
                "/api/v1/approvals/pending", headers=_mint_approver(verifier)
            )
            assert pending.json()["approvals"] == []

    run(_drive())


@pytest.mark.parametrize(
    ("mode", "status", "detail"),
    [
        ("pdp-deny", 403, "tool-denied"),
        ("guardrail", 422, "guardrail-violation"),
        ("handler-error", 502, "RuntimeError"),
    ],
)
def test_run_level_refusals_map_to_honest_statuses(
    mode: str,
    status: int,
    detail: str,
    tmp_path: Path,
    engines: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router docstring's run-level mapping, pinned class by class.

    A failure on the run-level ticket READ — before any subject leg, before any
    approval could be parked — must surface as the documented status, never a
    generic 500: PDP deny -> 403 ``tool-denied``, guardrail block -> 422
    ``guardrail-violation``, a tool handler failing -> 502 naming the cause
    class. (The fourth mapped class, the C7 scope refusal, is pinned by
    ``test_operator_without_data_label_gets_403_not_500``.)
    """
    import dataclasses

    from ne.api.workflows import TICKETS_TOOL
    from ne.contracts.governance import DecisionEffect, GuardrailVerdict, PolicyDecision
    from ne.govern.errors import GuardrailViolation

    app, verifier, _crm = _workflow_app(tmp_path, key=f"wf-runlvl-{mode}")
    state = get_state(app)
    engines.extend(state.engines)

    if mode == "pdp-deny":
        real_pdp = state.executor._pdp  # type: ignore[attr-defined]

        class _DenyTicketReads:
            async def decide(self, request: Any) -> PolicyDecision:
                if request.action == TICKETS_TOOL:
                    return PolicyDecision(
                        effect=DecisionEffect.DENY, policy_version="test", reason="denied by test"
                    )
                return await real_pdp.decide(request)

        monkeypatch.setattr(state.executor, "_pdp", _DenyTicketReads())
    elif mode == "guardrail":
        real_guardrails = state.executor._guardrails  # type: ignore[attr-defined]

        class _BlockTicketReads:
            async def enforce(self, payload: Any) -> Any:
                # The guardrail payload is the KEY_TOOL-keyed dict the executor
                # assembles via ``build_tool_call_payload``.
                if payload.get("tool") == TICKETS_TOOL:
                    raise GuardrailViolation(
                        (GuardrailVerdict(passed=False, rule="test.block", detail="test"),)
                    )
                return await real_guardrails.enforce(payload)

        monkeypatch.setattr(state.executor, "_guardrails", _BlockTicketReads())
    else:

        async def _boom(args: Any) -> str:
            raise RuntimeError("ticket store is down")

        bindings = state.executor._bindings  # type: ignore[attr-defined]
        monkeypatch.setitem(
            bindings, TICKETS_TOOL, dataclasses.replace(bindings[TICKETS_TOOL], handler=_boom)
        )

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run?max_subjects=2",
                headers=_mint_ops(verifier),
            )
            assert resp.status_code == status, resp.text
            assert resp.json()["detail"] == detail
            # Run-level means BEFORE any approval: nothing was parked.
            pending = await client.get(
                "/api/v1/approvals/pending", headers=_mint_approver(verifier)
            )
            assert pending.json()["approvals"] == []

    run(_drive())


# --- (5) the flood ceiling ----------------------------------------------------


def test_route_rejects_over_ceiling_and_accepts_the_boundary(
    tmp_path: Path, engines: list[Any]
) -> None:
    """422 at ceiling+1; 200 AT the ceiling (the control an over-broad guard fails)."""
    from ne.api.workflows import MAX_SUBJECTS_CEILING

    app, verifier, _crm = _workflow_app(tmp_path, key="wf-flood")
    state = get_state(app)
    engines.extend(state.engines)

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            headers = _mint_ops(verifier)
            over = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run"
                f"?max_subjects={MAX_SUBJECTS_CEILING + 1}",
                headers=headers,
            )
            assert over.status_code == 422, over.text
            # No run started, so no approval was parked by the refused request.
            pending = await client.get(
                "/api/v1/approvals/pending", headers=_mint_approver(verifier)
            )
            assert pending.json()["approvals"] == []

            at = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run"
                f"?max_subjects={MAX_SUBJECTS_CEILING}",
                headers=headers,
            )
            assert at.status_code == 200, at.text

    run(_drive())


def _seeded_store_wide(tmp_path: Path, accounts: int) -> Any:
    """A seeded T3 store with ``accounts`` accounts, one open urgent ticket each."""
    from ne.brain_graph.testing.memory import InMemoryGraphStore
    from ne.brain_ontology import PropertyGraphLift
    from ne.brain_ontology.config import OntologySettings
    from ne.brain_ontology.gateway import ValidatingTripleStore
    from ne.brain_ontology.quarantine import SqliteTripleQuarantine
    from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
    from ne.brain_ontology.store.oxigraph import OxigraphBackend

    from .test_ontology_api import _account, _NullSink

    settings = OntologySettings(include_ontology_graph=False)
    store = ValidatingTripleStore(
        OxigraphBackend(),
        build_validator(settings),
        SqliteTripleQuarantine(),
        _NullSink(),
        settings=settings,
        config_hash="s" * 64,
        compiled_shapes=load_compiled_shapes(),
    )

    async def _seed() -> None:
        mem = InMemoryGraphStore()
        await mem.upsert_nodes(
            [_account(f"acc{i}", ["all-employees"]) for i in range(accounts)]
            + [
                _ticket(
                    f"tkt{i}",
                    subject=f"Fault {i}",
                    priority="urgent",
                    status="open",
                    account=f"acc{i}",
                )
                for i in range(accounts)
            ]
        )
        await mem.upsert_edges(
            [_belongs_to(f"e{i}", f"tkt{i}", f"acc{i}") for i in range(accounts)]
        )
        await PropertyGraphLift(mem, store).run()

    run(_seed())
    return store


def test_engine_clamps_subjects_to_the_ceiling(tmp_path: Path, engines: list[Any]) -> None:
    """``run_workflow`` itself enforces the ceiling — a future non-route caller
    inherits it. The count that matters is APPROVALS ACTUALLY PARKED, not the
    requested subject count: 7 eligible accounts, ``max_subjects=50``, exactly 5
    approvals in the queue.
    """
    from ne.api.tools import TURN_PRINCIPAL
    from ne.api.workflows import MAX_SUBJECTS_CEILING, run_workflow
    from ne.contracts.types import Principal
    from ne.runtime.correlation import run_trace_scope

    wide = 7
    lookup = _FakeLookup({f"acc{i} Corp": f"001dL00002MiT{i:02d}AAB" for i in range(wide)})
    app, _verifier, _crm = _workflow_app(
        tmp_path, key="wf-clamp", lookup=lookup, store=_seeded_store_wide(tmp_path, wide)
    )
    state = get_state(app)
    engines.extend(state.engines)

    principal = Principal(id="user:ops-demo", groups=("ops", "all-employees"))

    async def _drive() -> None:
        token = TURN_PRINCIPAL.set(principal)
        try:
            with run_trace_scope() as trace_id:
                view = await run_workflow(
                    "unresolved-ticket-review",
                    executor=state.executor,
                    dispatcher=state.workflow_dispatcher,
                    gate=state.gate,
                    principal=principal,
                    trace_id=trace_id,
                    max_subjects=50,
                )
        finally:
            TURN_PRINCIPAL.reset(token)
        assert view.subjects_considered == wide
        assert view.subjects_selected == MAX_SUBJECTS_CEILING
        assert len(view.proposals) == MAX_SUBJECTS_CEILING
        assert {p.outcome for p in view.proposals} == {"awaiting-approval"}
        pending = await state.gate.list_pending()
        parked = [r for r in pending if r.action == "write.internal.crm.note"]
        assert len(parked) == MAX_SUBJECTS_CEILING

    run(_drive())


# --- (6) the human decision fires — or withholds — the write ------------------


def test_approve_fires_the_write_and_reject_withholds_it(
    tmp_path: Path, engines: list[Any]
) -> None:
    """The headline demo beat, pinned: approve -> the note lands in CRM; reject ->
    it never does. Before this test, no test in the suite ever DECIDED an approval.
    """
    import asyncio as _asyncio

    app, verifier, crm = _workflow_app(tmp_path, key="wf-decide")
    state = get_state(app)
    engines.extend(state.engines)

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run?max_subjects=2",
                headers=_mint_ops(verifier),
            )
            assert resp.status_code == 200, resp.text
            proposals = resp.json()["proposals"]
            assert [p["outcome"] for p in proposals] == ["awaiting-approval"] * 2
            hot = next(p for p in proposals if p["crm_record_id"] == _HOT_ID)
            cold = next(p for p in proposals if p["crm_record_id"] == _COLD_ID)
            assert crm.created == []  # parked, not written

            approver = _mint_approver(verifier)
            approved = await client.post(
                f"/api/v1/approvals/{hot['approval_request_id']}/approve",
                json={},
                headers=approver,
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["status"] == "approved"

            # The parked task wakes and performs the governed write.
            for _ in range(200):
                if crm.created:
                    break
                await _asyncio.sleep(0.05)
            assert len(crm.created) == 1, "approval did not fire the CRM write"
            sobject, fields = crm.created[0]
            assert sobject == "Note"
            assert fields["ParentId"] == _HOT_ID
            assert fields["Title"].startswith("Support review:")
            assert "2 unresolved tickets" in fields["Body"]

            rejected = await client.post(
                f"/api/v1/approvals/{cold['approval_request_id']}/reject",
                json={"reason": "not now"},
                headers=approver,
            )
            assert rejected.status_code == 200, rejected.text
            assert rejected.json()["status"] == "rejected"

            # Wait for the rejected task to settle, then prove it wrote NOTHING.
            from ne.api.workflows import _INFLIGHT

            for _ in range(200):
                if not any(not t.done() for t in _INFLIGHT):
                    break
                await _asyncio.sleep(0.05)
            assert len(crm.created) == 1, "a rejected write reached the CRM"
            pending = await client.get("/api/v1/approvals/pending", headers=approver)
            assert pending.json()["approvals"] == []

    run(_drive())


# --- (7) C12: sentinels, not field lists --------------------------------------


def test_run_response_and_audit_rows_carry_no_customer_content(
    tmp_path: Path, engines: list[Any]
) -> None:
    """Sentinel sweep over the SERIALIZED response and the run's audit rows.

    This — not the disclosure field-list check — is the C12 pin: a field list is
    V4 presence-without-structure (a new ``StepView.detail`` string carrying a note
    body would ship green under it). The sentinels are the seeded account names and
    ticket subjects; the CONTROLS below prove EVERY sentinel is, verbatim, content
    this run really carried — each on the surface it genuinely flows through — so
    seeded-naming drift turns a control RED instead of leaving the sweep silently
    vacuous for that sentinel.
    """
    import sqlalchemy as sa

    lookup = _FakeLookup({"acc_hot Corp": _HOT_ID, "acc_cold Corp": _COLD_ID})
    app, verifier, _crm = _workflow_app(tmp_path, key="wf-c12", lookup=lookup)
    state = get_state(app)
    engines.extend(state.engines)

    sentinels = (
        "acc_hot Corp",
        "acc_cold Corp",
        "Widget will not power on",
        "Widget shipment delayed",
        "Billing question",
    )

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run?max_subjects=2",
                headers=_mint_ops(verifier),
            )
            assert resp.status_code == 200, resp.text
            for sentinel in sentinels:
                assert sentinel not in resp.text, f"response leaks {sentinel!r}"

            # The run's audit rows: every column of every row, serialized —
            # SELECT * so the claim needs no column enumeration that can go
            # stale (the previous 4-column SELECT made this comment false; the
            # payload column is the hash-covered event and the rest are derived
            # copies and chain columns, but the sweep asserts that by sweeping,
            # not by arguing it). Proven live by an audit-only leak mutant: a
            # sentinel planted into the gate's ``approval.requested`` detail —
            # which never reaches the HTTP response — turns THIS sweep red.
            with state.audit_engine.connect() as conn:
                rows = conn.execute(sa.text("SELECT * FROM ne_audit_log")).fetchall()
            blob = json.dumps([list(r) for r in rows], default=str)
            assert "tool.execute" in blob  # the sweep read the right table
            for sentinel in sentinels:
                assert sentinel not in blob, f"audit rows leak {sentinel!r}"

            # CONTROLS (should-pass): every sentinel IS real content this run
            # carried, each pinned on the surface it genuinely flows through. A
            # misspelled sentinel, a drifted seed display name, or a sweep over
            # the wrong store fails HERE, not silently in the sweep above.
            #
            # (a) The account-NAME sentinels flow through the governed CRM
            #     lookup (the run resolves each write target by name); in this
            #     composition the drafting model's output does not carry them,
            #     so the lookup's recorded calls are their presence surface.
            assert [name for _, name in lookup.calls] == ["acc_hot Corp", "acc_cold Corp"]
            # (b) The ticket-SUBJECT sentinels flow into the audited approver
            #     disclosure bodies (the facts sentence lists subjects on file).
            approver = _mint_approver(verifier)
            disclosed = ""
            for proposal in resp.json()["proposals"]:
                payload = await client.get(
                    f"/api/v1/approvals/{proposal['approval_request_id']}/payload",
                    headers=approver,
                )
                assert payload.status_code == 200
                disclosed += payload.json()["material_parameters"]["body"]
            for subject_sentinel in (
                "Widget will not power on",
                "Widget shipment delayed",
                "Billing question",
            ):
                assert subject_sentinel in disclosed, (
                    f"presence control lost {subject_sentinel!r} — "
                    "the sweep above would be vacuous for it"
                )

    run(_drive())


# --- (8) who each leg executes as, pinned -------------------------------------


def test_drafting_leg_is_audited_as_the_service_identity(
    tmp_path: Path, engines: list[Any]
) -> None:
    """The honest principal split is a tested FACT, not a slogan.

    The reads (and the parked write's approval request) execute and audit as the
    CALLING principal; the drafting leg — the one leg whose task text carries
    customer content — executes and audits as ``agent:workflow-drafter``. One
    trace id links them.
    """
    from ne.audit.replay import replay

    app, verifier, _crm = _workflow_app(tmp_path, key="wf-actors")
    state = get_state(app)
    engines.extend(state.engines)

    async def _drive() -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/unresolved-ticket-review/run?max_subjects=2",
                headers=_mint_ops(verifier),
            )
            assert resp.status_code == 200, resp.text
            return resp.json()

    view = run(_drive())
    trace_id = view["trace_id"]
    events = replay(state.audit_engine, trace_id=trace_id).events
    assert events and all(e.trace_id == trace_id for e in events)

    # Every governed TOOL leg (reads + the write's approval request) is the caller.
    tool_events = [e for e in events if e.action in ("tool.execute", "approval.requested")]
    assert tool_events
    assert {e.actor for e in tool_events} == {"user:ops-demo"}

    # Every DRAFTING leg event is the service identity — and it exists (2 subjects
    # were drafted through the dispatcher in this run).
    subagent_events = [e for e in events if (e.action or "").startswith("subagent.")]
    assert subagent_events
    assert {e.actor for e in subagent_events} == {"agent:workflow-drafter"}
    assert any(e.action == "subagent.route" for e in subagent_events)

    # The PDP saw BOTH identities: the caller for the tools, the drafter for its
    # route consult. (``pdp.decide`` events do not carry a trace_id — ne-govern's
    # emitter predates run correlation — so this reads the whole table, not the
    # trace replay.)
    import sqlalchemy as sa

    with state.audit_engine.connect() as conn:
        pdp_actors = {
            row[0]
            for row in conn.execute(
                sa.text("SELECT actor FROM ne_audit_log WHERE action = 'pdp.decide'")
            )
        }
    assert {"user:ops-demo", "agent:workflow-drafter"} <= pdp_actors
