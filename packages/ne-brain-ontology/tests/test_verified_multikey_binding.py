# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-KG-MULTIKEY: bounded multi-key (``VALUES``-list) argument binding.

The feature collapses N single-key verified reads into ONE governed call. These tests
pin the five properties that make that collapse safe rather than merely fast:

* per-MEMBER validation is the SAME validation the scalar form gets (no member is
  waved through, and one bad member refuses the whole call);
* the CARDINALITY CAP is real (a list can never become an unbounded scan);
* the EMPTY-MEMBER case survives the collapse — "this key matched nothing" is still
  establishable, which is the fact N separate empty reads used to carry;
* ordering is deterministic (the query is a function of the member SET);
* the SCALAR path is byte-identical to its pre-feature self — query text, rendering,
  rows, citations, and the receipt row hash the claim verifier re-derives.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ne.brain_ontology import (
    load_meridian_template_registry,
    load_meridian_verified_fixture,
)
from ne.brain_ontology.verified import VerifiedAnswerArgsError, execute_template
from ne.brain_ontology.verified.answers import (
    ZERO_ROWS_NOTE,
    empty_members_note,
    render_answer_text,
)
from ne.brain_ontology.verified.errors import TemplateRegistryError
from ne.brain_ontology.verified.templates import (
    LIST_MAX_CEILING,
    list_members,
    parse_template_toml,
    render_query,
)

from .verified_helpers import StubExpander, make_gateway, principal

_FIXTURE = Path(__file__).resolve().parents[3] / "tools" / "meridian" / "fixtures" / "meridian-v1"
_CONFIG_HASH = "b" * 64

_CUSTOMER = ("bank.customer.read",)
_SERVICING = ("bank.servicing.read",)

#: The three DR-4830 counties the packaged declaration selects.
_COUNTIES = ["Columbia", "Jefferson", "Richmond"]
#: The 11 affected customers those counties yield (ground_truth.json b1.affected_rows).
_AFFECTED = [
    "mer-cus-001",
    "mer-cus-002",
    "mer-cus-003",
    "mer-cus-004",
    "mer-cus-005",
    "mer-cus-007",
    "mer-cus-008",
    "mer-cus-010",
    "mer-cus-011",
    "mer-cus-013",
    "mer-cus-015",
]
#: The three of them holding NO auto loan — the load-bearing empty members.
_NO_LOAN = ("mer-cus-011", "mer-cus-013", "mer-cus-015")


def _templates() -> dict[str, Any]:
    return {row.template_id: row for row in load_meridian_template_registry().templates}


def _typed(template: Any, args: Mapping[str, object]) -> dict[str, object]:
    validated = template.args_model.model_validate(dict(args))
    return {name: getattr(validated, name) for name in template.args}


async def _loaded_store() -> Any:
    store = make_gateway()
    await load_meridian_verified_fixture(store, _FIXTURE)
    return store


async def _execute(
    template: Any, args: Mapping[str, object], *, store: Any, groups: tuple[str, ...]
) -> Any:
    return await execute_template(
        template,
        args,
        store=store,
        expander=StubExpander(),
        principal=principal("user:meridian", groups),
        config_hash=_CONFIG_HASH,
        max_rows=500,
    )


# --- the packaged declarations -------------------------------------------------


def test_exactly_the_four_fanout_templates_declare_a_bounded_multi_key_arg() -> None:
    """The opt-in is explicit per template, and every cap is under the code ceiling."""
    declared = {
        template.template_id: (template.list_arg, template.args[template.list_arg].list_max)
        for template in load_meridian_template_registry().templates
        if template.list_arg is not None
    }
    assert declared == {
        "customers-in-counties": ("q_county", 4),
        "autoloans-for-customer": ("q_customer_id", 24),
        "payments-due-window": ("q_loan_id", 16),
        "servicing-notes-for-loan": ("q_loan_id", 16),
    }
    assert all(cap is not None and cap <= LIST_MAX_CEILING for _, cap in declared.values())
    # The county cap IS the closed vocabulary: that list is bounded by construction.
    counties = _templates()["customers-in-counties"].args["q_county"]
    assert counties.list_max == len(counties.enum or ())


# --- scalar parity -------------------------------------------------------------


def test_scalar_call_renders_the_identical_single_row_values_block() -> None:
    """A single value binds ONE ``VALUES`` row — the pre-feature text, byte for byte."""
    template = _templates()["autoloans-for-customer"]
    query = render_query(template, _typed(template, {"q_customer_id": "mer-cus-001"}), 500)
    assert 'VALUES (?q_customer_id) { ("mer-cus-001") }' in query
    assert query.count("VALUES") == 1
    assert list_members(template, _typed(template, {"q_customer_id": "mer-cus-001"})) is None


def test_scalar_call_answer_is_inert_for_every_multi_key_field() -> None:
    """No array in ⇒ nothing changes: the rendering and the new fields stay untouched."""

    async def scenario() -> None:
        store = await _loaded_store()
        answer = await _execute(
            _templates()["autoloans-for-customer"],
            {"q_customer_id": "mer-cus-001"},
            store=store,
            groups=_SERVICING,
        )
        assert answer.list_arg is None
        assert answer.empty_members == ()
        assert answer.empty_members_verified is False
        assert answer.empty_member_receipt is None
        assert empty_members_note(answer) is None
        rendered = render_answer_text(answer)
        assert "[note:" not in rendered
        # header + column row + rule + one line per row, and nothing else.
        assert len(rendered.split("\n")) == 3 + answer.returned_rows

    asyncio.run(scenario())


def test_scalar_empty_read_still_renders_only_the_zero_rows_note() -> None:
    """The pre-feature empty single-key read is unchanged (mer-cus-011 holds no loan)."""

    async def scenario() -> None:
        store = await _loaded_store()
        answer = await _execute(
            _templates()["autoloans-for-customer"],
            {"q_customer_id": "mer-cus-011"},
            store=store,
            groups=_SERVICING,
        )
        assert answer.rows == ()
        assert answer.empty_members_verified is False
        assert render_answer_text(answer).split("\n")[-1] == ZERO_ROWS_NOTE

    asyncio.run(scenario())


def test_scalar_and_single_member_list_serve_the_same_rows_and_row_hashes() -> None:
    """A 1-member list is a LIST call, but the DATA (and its receipt material) is equal."""

    async def scenario() -> None:
        store = await _loaded_store()
        template = _templates()["autoloans-for-customer"]
        scalar = await _execute(
            template, {"q_customer_id": "mer-cus-001"}, store=store, groups=_SERVICING
        )
        listed = await _execute(
            template, {"q_customer_id": ["mer-cus-001"]}, store=store, groups=_SERVICING
        )
        assert scalar.rows == listed.rows
        assert scalar.citations == listed.citations
        assert scalar.columns == listed.columns
        assert [_row_hash(scalar.columns, row) for row in scalar.rows] == [
            _row_hash(listed.columns, row) for row in listed.rows
        ]
        # ...but the ARGS differ, honestly: the artifact records what was asked.
        assert scalar.args["q_customer_id"] == "mer-cus-001"
        assert listed.args["q_customer_id"] == ["mer-cus-001"]

    asyncio.run(scenario())


# --- per-member validation -----------------------------------------------------


def test_one_invalid_member_refuses_the_whole_call_and_never_echoes_it() -> None:
    """Fail closed: a bad member is a refusal with a reason, never a dropped row."""

    async def scenario() -> None:
        store = await _loaded_store()
        with pytest.raises(VerifiedAnswerArgsError) as excinfo:
            await _execute(
                _templates()["autoloans-for-customer"],
                {"q_customer_id": ["mer-cus-001", "mer-cus-9x9", "mer-cus-002"]},
                store=store,
                groups=_SERVICING,
            )
        message = str(excinfo.value)
        assert "'q_customer_id' member #1 does not match the declared pattern" in message
        assert "mer-cus-9x9" not in message
        assert "No query was executed" in message

    asyncio.run(scenario())


def test_an_out_of_vocabulary_member_names_the_enum_never_the_value() -> None:
    template = _templates()["customers-in-counties"]
    with pytest.raises(VerifiedAnswerArgsError) as excinfo:
        _validate(template, {"q_county": ["Columbia", "Atlantis"]})
    message = str(excinfo.value)
    assert "must be one of: Columbia, Jefferson, McDuffie, Richmond" in message
    assert "Atlantis" not in message


def test_an_oversized_member_is_refused_by_the_same_max_length_as_a_scalar() -> None:
    """One constraint object, used by both branches — so both report the same finding."""
    template = _templates()["autoloans-for-customer"]
    scalar = _refusal(template, {"q_customer_id": "mer-cus-0001"})
    listed = _refusal(template, {"q_customer_id": ["mer-cus-001", "mer-cus-0001"]})
    assert "'q_customer_id' exceeds max_length=11" in scalar
    assert "'q_customer_id' member #1 exceeds max_length=11" in listed
    assert "mer-cus-0001" not in scalar and "mer-cus-0001" not in listed


# --- the cardinality cap -------------------------------------------------------


def test_a_list_over_the_cap_is_refused_before_any_query_runs() -> None:
    template = _templates()["payments-due-window"]
    over = [f"mer-loan-{index:03d}" for index in range(17)]
    message = _refusal(
        template,
        {"q_loan_id": over, "window_start": "2026-08-18", "window_end": "2026-09-01"},
    )
    assert "'q_loan_id' supplies more than the declared cardinality cap of 16 values" in message
    assert "No query was executed" in message


def test_a_duplicated_member_is_refused_not_silently_collapsed() -> None:
    template = _templates()["autoloans-for-customer"]
    message = _refusal(template, {"q_customer_id": ["mer-cus-001", "mer-cus-001"]})
    assert "'q_customer_id' repeats a value (list members must be distinct)" in message


def test_an_empty_list_is_refused() -> None:
    template = _templates()["autoloans-for-customer"]
    message = _refusal(template, {"q_customer_id": []})
    assert "'q_customer_id' is an empty list" in message


def test_a_wrongly_typed_argument_names_the_permitted_shape() -> None:
    template = _templates()["autoloans-for-customer"]
    message = _refusal(template, {"q_customer_id": 7})
    assert "must be a single value or a list of at most 24 values" in message


# --- ordering determinism ------------------------------------------------------


def test_the_rendered_query_is_a_function_of_the_member_set_not_its_order() -> None:
    template = _templates()["autoloans-for-customer"]
    forwards = render_query(
        template, _typed(template, {"q_customer_id": ["mer-cus-001", "mer-cus-002"]}), 500
    )
    backwards = render_query(
        template, _typed(template, {"q_customer_id": ["mer-cus-002", "mer-cus-001"]}), 500
    )
    assert forwards == backwards
    assert 'VALUES (?q_customer_id) { ("mer-cus-001") ("mer-cus-002") }' in forwards


def test_a_permuted_member_list_serves_identical_rows_and_empty_members() -> None:
    async def scenario() -> None:
        store = await _loaded_store()
        template = _templates()["autoloans-for-customer"]
        forwards = await _execute(
            template, {"q_customer_id": _AFFECTED}, store=store, groups=_SERVICING
        )
        backwards = await _execute(
            template,
            {"q_customer_id": list(reversed(_AFFECTED))},
            store=store,
            groups=_SERVICING,
        )
        assert forwards.rows == backwards.rows
        assert forwards.citations == backwards.citations
        assert forwards.empty_members == backwards.empty_members == _NO_LOAN

    asyncio.run(scenario())


def test_multi_key_rows_keep_the_trusted_citation_first_ordering() -> None:
    async def scenario() -> None:
        store = await _loaded_store()
        answer = await _execute(
            _templates()["autoloans-for-customer"],
            {"q_customer_id": _AFFECTED},
            store=store,
            groups=_SERVICING,
        )
        citation = answer.columns.index("loan")
        served = [row[citation] for row in answer.rows]
        assert served == sorted(served)

    asyncio.run(scenario())


# --- the empty-member case (the load-bearing one) ------------------------------


def test_the_three_customers_with_no_auto_loan_survive_the_collapse() -> None:
    """The fact 11 single-key reads used to carry, carried by ONE multi-key read."""

    async def scenario() -> None:
        store = await _loaded_store()
        answer = await _execute(
            _templates()["autoloans-for-customer"],
            {"q_customer_id": _AFFECTED},
            store=store,
            groups=_SERVICING,
        )
        assert answer.list_arg == "q_customer_id"
        assert answer.empty_members_verified is True
        assert answer.empty_members == _NO_LOAN
        assert answer.returned_rows == 8
        note = render_answer_text(answer).split("\n")[-1]
        assert note == (
            "[note: 3 of 11 requested q_customer_id values returned no rows: "
            "mer-cus-011, mer-cus-013, mer-cus-015 — a fact about this query's "
            "result only, not evidence that the data does not exist]"
        )
        # NO fabricated row: the payload stays rectangular over REAL rows only.
        assert all(len(row) == len(answer.columns) for row in answer.rows)
        assert not any(member in row for member in _NO_LOAN for row in answer.rows)

    asyncio.run(scenario())


def test_every_member_matching_leaves_no_note_at_all() -> None:
    async def scenario() -> None:
        store = await _loaded_store()
        answer = await _execute(
            _templates()["autoloans-for-customer"],
            {"q_customer_id": ["mer-cus-001", "mer-cus-002"]},
            store=store,
            groups=_SERVICING,
        )
        assert answer.empty_members_verified is True
        assert answer.empty_members == ()
        assert "[note:" not in render_answer_text(answer)

    asyncio.run(scenario())


def test_a_wholly_empty_multi_key_read_reports_every_member_empty() -> None:
    async def scenario() -> None:
        store = await _loaded_store()
        answer = await _execute(
            _templates()["autoloans-for-customer"],
            {"q_customer_id": list(_NO_LOAN)},
            store=store,
            groups=_SERVICING,
        )
        assert answer.rows == ()
        assert answer.empty_members == _NO_LOAN
        rendered = render_answer_text(answer).split("\n")
        assert rendered[-2] == ZERO_ROWS_NOTE
        assert rendered[-1].startswith("[note: 3 of 3 requested q_customer_id")

    asyncio.run(scenario())


def test_truncation_voids_the_empty_member_claim_rather_than_guessing() -> None:
    """A member absent from a capped prefix may hold rows past the cap — so: no claim."""

    async def scenario() -> None:
        store = await _loaded_store()
        answer = await _execute(
            _templates()["autoloans-for-customer"],
            {"q_customer_id": _AFFECTED},
            store=store,
            groups=_SERVICING,
        )
        assert answer.truncated is False and answer.empty_members_verified is True

        capped = await execute_template(
            _templates()["autoloans-for-customer"],
            {"q_customer_id": _AFFECTED},
            store=store,
            expander=StubExpander(),
            principal=principal("user:meridian", _SERVICING),
            config_hash=_CONFIG_HASH,
            max_rows=3,
        )
        assert capped.truncated is True
        assert capped.empty_members_verified is False
        assert capped.empty_members == ()
        assert "[note:" not in render_answer_text(capped)

    asyncio.run(scenario())


def test_the_empty_member_mechanism_generalises_to_the_date_window_template() -> None:
    """mer-loan-008's only scheduled payment falls OUTSIDE the B1 14-day window."""

    async def scenario() -> None:
        store = await _loaded_store()
        loan_ids = [f"mer-loan-{index:03d}" for index in (1, 2, 3, 4, 5, 7, 8, 10)]
        answer = await _execute(
            _templates()["payments-due-window"],
            {
                "q_loan_id": loan_ids,
                "window_start": "2026-08-18",
                "window_end": "2026-09-01",
            },
            store=store,
            groups=_SERVICING,
        )
        assert answer.empty_members == ("mer-loan-008",)
        assert answer.returned_rows == 7
        # The scalar read of that same loan agrees: zero rows, and no note.
        scalar = await _execute(
            _templates()["payments-due-window"],
            {
                "q_loan_id": "mer-loan-008",
                "window_start": "2026-08-18",
                "window_end": "2026-09-01",
            },
            store=store,
            groups=_SERVICING,
        )
        assert scalar.rows == ()
        assert scalar.empty_members_verified is False

    asyncio.run(scenario())


def test_a_full_vocabulary_county_sweep_serves_every_customer_in_one_call() -> None:
    """All four enum counties in ONE call — the cap IS the vocabulary size."""

    async def scenario() -> None:
        store = await _loaded_store()
        answer = await _execute(
            _templates()["customers-in-counties"],
            {"q_county": [*_COUNTIES, "McDuffie"]},
            store=store,
            groups=_CUSTOMER,
        )
        assert answer.empty_members_verified is True
        assert answer.empty_members == ()
        assert len({row[answer.columns.index("customer_id")] for row in answer.rows}) == 15
        assert {row[answer.columns.index("county")] for row in answer.rows} == {
            *_COUNTIES,
            "McDuffie",
        }

    asyncio.run(scenario())


def test_token_packing_charges_the_note_and_still_bounds_the_rendering() -> None:
    """The empty-member note is part of the pre-charged frame, not free budget."""

    async def scenario() -> None:
        store = await _loaded_store()
        budget = 4000
        answer = await execute_template(
            _templates()["autoloans-for-customer"],
            {"q_customer_id": _AFFECTED},
            store=store,
            expander=StubExpander(),
            principal=principal("user:meridian", _SERVICING),
            config_hash=_CONFIG_HASH,
            max_rows=500,
            max_result_tokens=budget,
        )
        from ne.contracts.tokens import ByteTokenEstimator

        assert ByteTokenEstimator().estimate(render_answer_text(answer)) <= budget
        # A budget this generous packs nothing away, so the claim still stands.
        assert answer.truncation_reason is None
        assert answer.empty_members == _NO_LOAN

    asyncio.run(scenario())


# --- the receipt / row-hash round trip -----------------------------------------


def _row_hash(columns: tuple[str, ...], row: tuple[str, ...]) -> str:
    """The claim verifier's own formula, re-typed here on purpose (drift tripwire)."""
    return hashlib.sha256(
        json.dumps([list(columns), list(row)], separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_multi_key_rows_round_trip_through_the_claim_verifiers_row_hash() -> None:
    """Re-derive each row FROM THE RENDERING, exactly as run_claim_verifier does."""

    async def scenario() -> None:
        store = await _loaded_store()
        answer = await _execute(
            _templates()["autoloans-for-customer"],
            {"q_customer_id": _AFFECTED},
            store=store,
            groups=_SERVICING,
        )
        lines = render_answer_text(answer).split("\n")
        columns = lines[1].split(" | ")
        assert lines[2] == "-+-".join("-" * len(column) for column in columns)
        row_lines = lines[3:]
        # The verifier drops trailing envelope notes ONLY while the count exceeds
        # ``returned`` — mirrored here so a rendering change breaks THIS test first.
        while (
            len(row_lines) > answer.returned_rows
            and row_lines[-1].startswith("[note: ")
            and row_lines[-1].endswith("]")
        ):
            row_lines.pop()
        assert len(row_lines) == answer.returned_rows
        rederived = [tuple(line.split(" | ")) for line in row_lines]
        assert rederived == list(answer.rows)
        assert [_row_hash(tuple(columns), row) for row in rederived] == [
            _row_hash(answer.columns, row) for row in answer.rows
        ]

    asyncio.run(scenario())


# --- load-time guardians -------------------------------------------------------


_BASE_TOML = """
template_id = "probe-multikey"
version = 1
description = "probe"
max_rows = 20
sparql = \"\"\"
SELECT ?loan ?loan_id ?customer_id WHERE {
  #BINDINGS#
  ?loan a <https://example.test/AutoLoan> .
  ?loan <https://example.test/loan_id> ?loan_id .
  ?loan <https://example.test/customer_id> ?customer_id .
  FILTER(?customer_id = ?q_customer_id)
}
\"\"\"

[args.q_customer_id]
type = "str"
pattern = "^mer-cus-[0-9]{3}$"
max_length = 11
list_max = 8
key_column = "customer_id"

[scope]
graph_classes = ["bank.servicing.read"]
result_shape = "enumerable"

[answer]
columns = ["loan", "loan_id", "customer_id"]
citation_var = "loan"
contributor_vars = ["loan"]
source_status = "source_bearing"
"""


def test_the_probe_template_loads_as_written() -> None:
    template = parse_template_toml(_BASE_TOML)
    assert template.list_arg == "q_customer_id"


@pytest.mark.parametrize(
    ("replace", "with_", "expected"),
    [
        pytest.param(
            'key_column = "customer_id"',
            "",
            "list_max without key_column",
            id="key_column-required",
        ),
        pytest.param(
            'key_column = "customer_id"',
            'key_column = "not_a_column"',
            "not one of the declared answer columns",
            id="key_column-must-be-a-column",
        ),
        pytest.param(
            "FILTER(?customer_id = ?q_customer_id)",
            "FILTER(?customer_id != ?q_customer_id)",
            "carries no FILTER(?customer_id = ?q_customer_id) equality join",
            id="equality-join-required",
        ),
        pytest.param(
            "list_max = 8",
            "list_max = 33",
            "less than or equal to 32",
            id="ceiling-enforced",
        ),
        pytest.param(
            'pattern = "^mer-cus-[0-9]{3}$"',
            "",
            "neither enum nor pattern",
            id="vocabulary-bounded-required",
        ),
        pytest.param(
            'key_column = "customer_id"',
            'key_column = "q_customer_id"',
            "not one of the declared answer columns",
            id="key_column-is-not-an-argument",
        ),
        pytest.param(
            'columns = ["loan", "loan_id", "customer_id"]',
            'columns = ["loan", "loan_id", "customer_id", "q_customer_id"]',
            "is not in the SELECT projection",
            id="arg-echo-column-refused",
        ),
    ],
)
def test_a_malformed_multi_key_declaration_fails_at_load(
    replace: str, with_: str, expected: str
) -> None:
    with pytest.raises(TemplateRegistryError) as excinfo:
        parse_template_toml(_BASE_TOML.replace(replace, with_))
    assert expected in str(excinfo.value)


def test_a_second_multi_key_argument_is_refused_as_a_cross_product() -> None:
    toml = (
        _BASE_TOML.replace(
            "  FILTER(?customer_id = ?q_customer_id)",
            "  FILTER(?customer_id = ?q_customer_id)\n  FILTER(?loan_id = ?q_loan_id)",
        )
        + """
[args.q_loan_id]
type = "str"
pattern = "^mer-loan-[0-9]{3}$"
max_length = 12
list_max = 8
key_column = "loan_id"
"""
    )
    with pytest.raises(TemplateRegistryError) as excinfo:
        parse_template_toml(toml)
    assert "list_max on more than one arg" in str(excinfo.value)


def test_key_column_without_list_max_is_refused() -> None:
    with pytest.raises(TemplateRegistryError) as excinfo:
        parse_template_toml(_BASE_TOML.replace("list_max = 8\n", ""))
    assert "key_column without list_max" in str(excinfo.value)


def test_a_list_max_above_its_own_enum_size_is_refused() -> None:
    toml = _BASE_TOML.replace(
        'pattern = "^mer-cus-[0-9]{3}$"', 'enum = ["mer-cus-001", "mer-cus-002"]'
    ).replace("list_max = 8", "list_max = 3")
    with pytest.raises(TemplateRegistryError) as excinfo:
        parse_template_toml(toml)
    assert "exceeds its own closed enum size" in str(excinfo.value)


def test_a_non_string_argument_may_not_declare_list_max() -> None:
    toml = _BASE_TOML.replace('type = "str"', 'type = "int"').replace(
        'pattern = "^mer-cus-[0-9]{3}$"\n', ""
    )
    with pytest.raises(TemplateRegistryError) as excinfo:
        parse_template_toml(toml)
    assert "may not declare list_max" in str(excinfo.value)


# --- the collapsed B1 chain ----------------------------------------------------


def test_the_b1_chain_collapses_to_five_governed_reads_and_keeps_ground_truth() -> None:
    """32 governed reads → 5 (plus the one prose SOP search), same three loans."""

    calls: list[str] = []

    async def scenario() -> None:
        store = await _loaded_store()
        templates = _templates()

        async def read(template_id: str, args: Mapping[str, object], groups: tuple[str, ...]):
            calls.append(template_id)
            return await _execute(templates[template_id], args, store=store, groups=groups)

        declarations = await read("disaster-declarations", {}, _CUSTOMER)
        counties = sorted({row[declarations.columns.index("county")] for row in declarations.rows})
        assert counties == _COUNTIES

        customers = await read("customers-in-counties", {"q_county": counties}, _CUSTOMER)
        customer_ids = sorted(
            {row[customers.columns.index("customer_id")] for row in customers.rows}
        )
        assert customer_ids == _AFFECTED

        loans = await read("autoloans-for-customer", {"q_customer_id": customer_ids}, _SERVICING)
        assert loans.empty_members == _NO_LOAN
        loan_ids = sorted({row[loans.columns.index("loan_id")] for row in loans.rows})
        assert len(loan_ids) == 8

        payments = await read(
            "payments-due-window",
            {"q_loan_id": loan_ids, "window_start": "2026-08-18", "window_end": "2026-09-01"},
            _SERVICING,
        )
        notes = await read("servicing-notes-for-loan", {"q_loan_id": loan_ids}, _SERVICING)

        scheduled: dict[str, list[str]] = {}
        for row in payments.rows:
            if row[payments.columns.index("status")] == "scheduled":
                key = row[payments.columns.index("loan_id")]
                scheduled.setdefault(key, []).append(row[payments.columns.index("due_date")])
        note_types: dict[str, set[str]] = {}
        for row in notes.rows:
            key = row[notes.columns.index("loan_id")]
            note_types.setdefault(key, set()).add(row[notes.columns.index("note_type")])

        actionable = sorted(
            row[loans.columns.index("loan_id")]
            for row in loans.rows
            if row[loans.columns.index("status")] == "active"
            and scheduled.get(row[loans.columns.index("loan_id")])
            and not (
                {"hardship_offered", "repossession"}
                & note_types.get(row[loans.columns.index("loan_id")], set())
            )
        )
        assert actionable == ["mer-loan-001", "mer-loan-002", "mer-loan-003"]

    asyncio.run(scenario())
    assert len(calls) == 5


# --- helpers -------------------------------------------------------------------


def _validate(template: Any, args: Mapping[str, object]) -> None:
    from pydantic import ValidationError

    from ne.brain_ontology.verified.templates import describe_invalid_args

    try:
        template.args_model.model_validate(dict(args))
    except ValidationError as exc:
        raise VerifiedAnswerArgsError(describe_invalid_args(template, exc)) from None


def _refusal(template: Any, args: Mapping[str, object]) -> str:
    with pytest.raises(VerifiedAnswerArgsError) as excinfo:
        _validate(template, args)
    return str(excinfo.value)


# --- window-relative emptiness says WHOSE window it was measured under ---------
#
# The live failure this pins: a model with no served as-of anchor invented a
# payments window ending before 2026-09-01, saw mer-loan-003 come back as an empty
# member, and concluded "no payment due" — an answer a verifier then stamped
# VERIFIED, because every quoted sentence was grounded. The absence was real, but
# it was an absence UNDER A CALLER-CHOSEN WINDOW, and the rendering never said so.
# For a template that declares a ``date_window``, the note now names the window.


def test_a_windowed_empty_member_note_names_the_caller_chosen_window() -> None:
    async def scenario() -> None:
        store = await _loaded_store()
        answer = await _execute(
            _templates()["payments-due-window"],
            {
                "q_loan_id": ["mer-loan-001", "mer-loan-002", "mer-loan-003"],
                "window_start": "2026-07-25",
                "window_end": "2026-08-25",
            },
            store=store,
            groups=_SERVICING,
        )
        assert answer.window == ("2026-07-25", "2026-08-25")
        assert answer.empty_members_verified is True
        assert answer.empty_members == ("mer-loan-003",)
        note = render_answer_text(answer).split("\n")[-1]
        assert note == (
            "[note: 1 of 3 requested q_loan_id values returned no rows in the "
            "caller-chosen window 2026-07-25..2026-08-25: mer-loan-003 — a fact "
            "about this query's result only, not evidence that the data does not exist]"
        )

    asyncio.run(scenario())


def test_a_windowed_read_with_every_member_matching_is_note_free() -> None:
    """A genuinely complete windowed answer is unaffected: no note, no new bytes."""

    async def scenario() -> None:
        store = await _loaded_store()
        answer = await _execute(
            _templates()["payments-due-window"],
            {
                "q_loan_id": ["mer-loan-001", "mer-loan-002", "mer-loan-003"],
                "window_start": "2026-08-18",
                "window_end": "2026-09-01",
            },
            store=store,
            groups=_SERVICING,
        )
        assert answer.window == ("2026-08-18", "2026-09-01")
        assert answer.empty_members_verified is True
        assert answer.empty_members == ()
        assert answer.empty_member_receipt is None
        assert "[note:" not in render_answer_text(answer)

    asyncio.run(scenario())


def test_a_windowless_template_note_is_byte_identical_to_before() -> None:
    """No ``date_window`` declared ⇒ ``window`` is None and the note text is
    exactly the pre-change spelling (pinned above in
    ``test_the_three_customers_with_no_auto_loan_survive_the_collapse``)."""

    async def scenario() -> None:
        store = await _loaded_store()
        answer = await _execute(
            _templates()["autoloans-for-customer"],
            {"q_customer_id": _AFFECTED},
            store=store,
            groups=_SERVICING,
        )
        assert answer.window is None
        assert " in the caller-chosen window " not in render_answer_text(answer)

    asyncio.run(scenario())
