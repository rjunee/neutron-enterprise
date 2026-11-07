# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""BD-5 Meridian templates over BD-1's generator-derived ``meridian-v1`` truth."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ne.brain_ontology import (
    MERIDIAN_TEMPLATE_IDS,
    graphs_for_labels,
    load_meridian_template_registry,
    load_meridian_verified_fixture,
    meridian_template_dir,
)
from ne.brain_ontology.verified import (
    VerifiedAnswerArgsError,
    VerifiedAnswerScopeError,
    answers_equal,
    execute_template,
)
from ne.contracts.source_authority import (
    GovernedLocalT3SourcePoint,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)

from .verified_helpers import RecordingStore, StubExpander, make_gateway, principal

_FIXTURE = Path(__file__).resolve().parents[3] / "tools" / "meridian" / "fixtures" / "meridian-v1"
_CONFIG_HASH = "b" * 64


def _source_stamp() -> SourceAuthorityStamp:
    contract = SourceContractIdentity(
        source_kind="governed_local_t3",
        descriptor_schema_version=1,
        descriptor_hash="1" * 64,
    )
    source = SourceIdentity(
        deployment_id="deployment-1",
        source_kind="governed_local_t3",
        connector_instance_id="ontology-1",
        point=GovernedLocalT3SourcePoint(
            dataset_name="meridian",
            contributor_key="contributor-1",
        ),
        generation=1,
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="2" * 64,
        content_snapshot_hash="3" * 64,
        acl_snapshot_hash="4" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision="revision-1",
        acl_snapshot_hash="4" * 64,
        source_contract=contract,
        contract_epoch=1,
        authority_receipt_id="receipt-1",
        authority_receipt_hash="5" * 64,
    )


def _documents() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        json.loads((_FIXTURE / "banking.json").read_bytes()),
        json.loads((_FIXTURE / "ground_truth.json").read_bytes()),
    )


def _templates() -> dict[str, Any]:
    return {row.template_id: row for row in load_meridian_template_registry().templates}


async def _execute(
    template: Any,
    args: Mapping[str, object],
    *,
    store: Any,
    groups: tuple[str, ...],
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


def test_packaged_bundle_is_exact_and_fully_typed() -> None:
    registry = load_meridian_template_registry()
    assert meridian_template_dir().is_dir()
    assert registry.fingerprint.template_ids == MERIDIAN_TEMPLATE_IDS
    assert {template.tool_name for template in registry.templates} == {
        "read.kg.customers_in_counties",
        "read.kg.autoloans_for_customer",
        "read.kg.payments_due_window",
        "read.kg.servicing_notes_for_loan",
        "read.kg.open_disputes_by_status",
        "read.kg.disputes_directory",
        "read.kg.disaster_declarations",
        "read.kg.hardship_candidate_loans",
        "read.kg.overdue_provisional_credit_disputes",
        "read.kg.t3_records_in_class",
        "read.kg.t3_record",
    }
    assert all(template.answer.count_total_rows for template in registry.templates)
    assert all(template.answer.source_status == "source_bearing" for template in registry.templates)
    assert _templates()["open-disputes-by-status"].answer.contributor_vars == (
        "dispute",
        "current_policy",
    )
    payments = _templates()["payments-due-window"]
    assert payments.date_window is not None
    assert payments.date_window.max_days == 31


def test_loader_is_manifest_bound_and_byte_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = make_gateway()
        first = await load_meridian_verified_fixture(store, _FIXTURE)
        second = await load_meridian_verified_fixture(store, _FIXTURE)
        assert set(first.graph_outcomes.values()) == {"replaced"}
        assert set(second.graph_outcomes.values()) == {"unchanged"}
        assert first.fixture_hash == second.fixture_hash
        assert all(count > 0 for count in first.triple_counts.values())

        corrupt = tmp_path / "meridian-v1"
        shutil.copytree(_FIXTURE, corrupt)
        (corrupt / "banking.json").write_bytes((corrupt / "banking.json").read_bytes() + b" ")
        with pytest.raises(ValueError, match="manifest sha256"):
            await load_meridian_verified_fixture(make_gateway(), corrupt)

    asyncio.run(scenario())


def test_all_six_templates_match_generator_source_and_ground_truth() -> None:
    async def scenario() -> None:
        banking, truth = _documents()
        store = make_gateway()
        await load_meridian_verified_fixture(store, _FIXTURE)
        templates = _templates()

        # Complete B1 county enumeration: the declaration's finite county list is
        # used directly; no search or retrieval result decides which county to ask.
        customer_rows: list[dict[str, str]] = []
        customer_template = templates["customers-in-counties"]
        for county in banking["disaster_declarations"][0]["counties"]:
            answer = await _execute(
                customer_template,
                {"q_county": county},
                store=store,
                groups=("bank.customer.read",),
            )
            assert answer.total_rows == answer.returned_rows
            customer_rows.extend(dict(zip(answer.columns, row, strict=True)) for row in answer.rows)
        expected_affected = {row["customer_id"] for row in truth["b1"]["affected_rows"]}
        assert {row["customer_id"] for row in customer_rows} == expected_affected

        loans_by_customer: dict[str, list[dict[str, Any]]] = {}
        for row in banking["auto_loans"]:
            loans_by_customer.setdefault(row["customer_id"], []).append(row)
        loan_template = templates["autoloans-for-customer"]
        for customer_id in sorted(expected_affected):
            answer = await _execute(
                loan_template,
                {"q_customer_id": customer_id},
                store=store,
                groups=("bank.servicing.read",),
            )
            served_ids = {row[1] for row in answer.rows}
            assert served_ids == {row["loan_id"] for row in loans_by_customer.get(customer_id, [])}
            assert all(row[2] == customer_id for row in answer.rows)

        payment_template = templates["payments-due-window"]
        for source_loan in banking["auto_loans"]:
            loan_id = source_loan["loan_id"]
            answer = await _execute(
                payment_template,
                {
                    "q_loan_id": loan_id,
                    "window_start": truth["b1"]["window_start"],
                    "window_end": truth["b1"]["window_end"],
                },
                store=store,
                groups=("bank.servicing.read",),
            )
            expected = {
                row["payment_id"]
                for row in banking["payment_schedules"]
                if row["loan_id"] == loan_id
                and truth["b1"]["window_start"] <= row["due_date"] <= truth["b1"]["window_end"]
            }
            assert {row[1] for row in answer.rows} == expected
            assert all(row[2] == loan_id for row in answer.rows)

        notes = await _execute(
            templates["servicing-notes-for-loan"],
            {"q_loan_id": "mer-loan-001"},
            store=store,
            groups=("bank.servicing.read",),
        )
        assert {row[1] for row in notes.rows} == {
            row["note_id"] for row in banking["servicing_notes"] if row["loan_id"] == "mer-loan-001"
        }
        assert all(row[2] == "mer-loan-001" for row in notes.rows)

        open_disputes = await _execute(
            templates["open-disputes-by-status"],
            {"q_status": "open"},
            store=store,
            groups=("bank.dispute.read",),
        )
        assert {row[1] for row in open_disputes.rows} == {
            row["dispute_id"] for row in banking["reg_e_disputes"] if row["status"] == "open"
        }
        assert {row[8] for row in open_disputes.rows} == {truth["b2"]["current_artifact_id"]}

        directory = await _execute(
            templates["disputes-directory"],
            {},
            store=store,
            groups=("bank.dispute.read",),
        )
        assert [row[1] for row in directory.rows] == sorted(
            row["dispute_id"] for row in banking["reg_e_disputes"]
        )
        credited = {row[1]: row[7] for row in directory.rows if row[6] == "true"}
        assert credited == {"mer-dsp-003": "20000", "mer-dsp-005": "90000"}
        assert all(row[8] == "" for row in directory.rows)

        # Repeating an identical complete sweep is byte-equivalent except for its
        # execution timestamp; trusted ORDER BY, not backend luck, owns row order.
        repeated = await _execute(
            templates["disputes-directory"],
            {},
            store=store,
            groups=("bank.dispute.read",),
        )
        assert answers_equal(directory, repeated)

    asyncio.run(scenario())


def test_mutable_demo_store_emits_required_contributor_receipts() -> None:
    class _AuthorizedReader:
        async def resolve(self, *_args: Any, **_kwargs: Any) -> Any:
            return type("Resolution", (), {"decision": SourceAuthorityDecision.AUTHORIZED})()

    async def scenario() -> None:
        store = make_gateway()
        await load_meridian_verified_fixture(store, _FIXTURE)
        stamp = _source_stamp()
        contributors: list[str] = []

        async def stamp_for(contributor: str) -> SourceAuthorityStamp:
            contributors.append(contributor)
            return stamp

        answer = await execute_template(
            _templates()["customers-in-counties"],
            {"q_county": "Columbia"},
            store=store,
            expander=StubExpander(),
            principal=principal("user:meridian", ("bank.customer.read",)),
            config_hash=_CONFIG_HASH,
            max_rows=500,
            contributor_stamp_provider=stamp_for,
            source_authority=_AuthorizedReader(),  # type: ignore[arg-type]
            authorized_contributor_graphs=graphs_for_labels(("bank.customer.read",)).default_graphs,
            require_contributor_receipts=True,
        )

        assert answer.rows
        assert len(answer.contributor_receipts) == len(answer.rows)
        assert len(contributors) == len(answer.rows)
        assert all(receipt.contributors == (stamp,) for receipt in answer.contributor_receipts)

        with pytest.raises(VerifiedAnswerScopeError, match="source_authority_unavailable"):
            await execute_template(
                _templates()["customers-in-counties"],
                {"q_county": "Columbia"},
                store=store,
                expander=StubExpander(),
                principal=principal("user:meridian", ("bank.customer.read",)),
                config_hash=_CONFIG_HASH,
                max_rows=500,
                contributor_stamp_provider=stamp_for,
                source_authority=_AuthorizedReader(),  # type: ignore[arg-type]
                authorized_contributor_graphs=(),
                require_contributor_receipts=True,
            )

    asyncio.run(scenario())


def test_mutable_demo_conjoins_product_capability_with_source_native_acl_graph() -> None:
    class _AuthorizedReader:
        async def resolve(self, *_args: Any, **_kwargs: Any) -> Any:
            return type("Resolution", (), {"decision": SourceAuthorityDecision.AUTHORIZED})()

    class _DatasetRecordingStore:
        def __init__(self, inner: Any) -> None:
            self.inner = inner
            self.datasets: list[Any] = []

        async def sparql_scoped(self, query: str, *, dataset: Any) -> Any:
            self.datasets.append(dataset)
            return await self.inner.sparql_scoped(query, dataset=dataset)

        def ontology_version(self) -> Any:
            return self.inner.ontology_version()

    async def scenario() -> None:
        gateway = make_gateway()
        await load_meridian_verified_fixture(gateway, _FIXTURE)
        store = _DatasetRecordingStore(gateway)
        stamp = _source_stamp()
        source_acl = "306773cc-b05a-43b4-bbb2-7858bfdb67dc"
        source_graphs = graphs_for_labels((source_acl,)).default_graphs
        snapshots: list[tuple[str, ...]] = []

        async def stamp_for(_contributor: str) -> SourceAuthorityStamp:
            return stamp

        async def snapshot_for(graphs: tuple[str, ...]) -> tuple[SourceAuthorityStamp, ...]:
            snapshots.append(graphs)
            return (stamp,)

        answer = await execute_template(
            _templates()["customers-in-counties"],
            {"q_county": "Columbia"},
            store=store,
            expander=StubExpander(),
            principal=principal(
                "user:meridian",
                ("bank.customer.read", source_acl),
            ),
            config_hash=_CONFIG_HASH,
            max_rows=500,
            contributor_stamp_provider=stamp_for,
            contributor_snapshot_provider=snapshot_for,
            source_authority=_AuthorizedReader(),  # type: ignore[arg-type]
            authorized_contributor_graphs=source_graphs,
            require_contributor_receipts=True,
        )

        # The product capability admits the template, but data is read only from
        # the separately expanded, source-native graph admitted by the lift.
        assert answer.rows == ()
        assert [dataset.default_graphs for dataset in store.datasets] == [source_graphs]
        assert snapshots == [source_graphs]

        denied_store = _DatasetRecordingStore(gateway)
        with pytest.raises(VerifiedAnswerScopeError, match="holds no label matching template"):
            await execute_template(
                _templates()["customers-in-counties"],
                {"q_county": "Columbia"},
                store=denied_store,
                expander=StubExpander(),
                principal=principal("user:meridian", (source_acl,)),
                config_hash=_CONFIG_HASH,
                max_rows=500,
                contributor_stamp_provider=stamp_for,
                contributor_snapshot_provider=snapshot_for,
                source_authority=_AuthorizedReader(),  # type: ignore[arg-type]
                authorized_contributor_graphs=source_graphs,
                require_contributor_receipts=True,
            )
        assert denied_store.datasets == []

    asyncio.run(scenario())


def test_mutable_demo_store_proves_complete_zero_row_result_from_scoped_snapshot() -> None:
    class _AuthorizedReader:
        async def resolve(self, *_args: Any, **_kwargs: Any) -> Any:
            return type("Resolution", (), {"decision": SourceAuthorityDecision.AUTHORIZED})()

    async def scenario() -> None:
        store = make_gateway()
        await load_meridian_verified_fixture(store, _FIXTURE)
        stamp = _source_stamp()
        requested_graphs: list[tuple[str, ...]] = []

        async def stamp_for(_contributor: str) -> SourceAuthorityStamp:
            return stamp

        async def snapshot_for(graphs: tuple[str, ...]) -> tuple[SourceAuthorityStamp, ...]:
            requested_graphs.append(graphs)
            return (stamp,)

        graphs = graphs_for_labels(("bank.servicing.read",)).default_graphs
        answer = await execute_template(
            _templates()["autoloans-for-customer"],
            {"q_customer_id": "mer-cus-011"},
            store=store,
            expander=StubExpander(),
            principal=principal("user:meridian", ("bank.servicing.read",)),
            config_hash=_CONFIG_HASH,
            max_rows=500,
            contributor_stamp_provider=stamp_for,
            contributor_snapshot_provider=snapshot_for,
            source_authority=_AuthorizedReader(),  # type: ignore[arg-type]
            authorized_contributor_graphs=graphs,
            require_contributor_receipts=True,
        )

        assert answer.rows == ()
        assert answer.contributor_receipts == ()
        assert answer.empty_result_receipt is not None
        assert answer.empty_result_receipt.contributors == (stamp,)
        assert requested_graphs == [graphs]

        with pytest.raises(VerifiedAnswerScopeError, match="source_authority_unavailable"):
            await execute_template(
                _templates()["autoloans-for-customer"],
                {"q_customer_id": "mer-cus-011"},
                store=store,
                expander=StubExpander(),
                principal=principal("user:meridian", ("bank.servicing.read",)),
                config_hash=_CONFIG_HASH,
                max_rows=500,
                contributor_stamp_provider=stamp_for,
                source_authority=_AuthorizedReader(),  # type: ignore[arg-type]
                authorized_contributor_graphs=graphs,
                require_contributor_receipts=True,
            )

    asyncio.run(scenario())


def test_acl_subject_scope_and_input_bounds_fail_closed_before_query() -> None:
    async def scenario() -> None:
        gateway = make_gateway()
        await load_meridian_verified_fixture(gateway, _FIXTURE)
        templates = _templates()
        banking, truth = _documents()
        groups_by_persona = {
            row["persona_id"]: tuple(row["entitlements"]) for row in banking["personas"]
        }

        # BD-1's generated frontline persona sees the customer directory but has
        # neither servicing nor dispute data entitlement.
        customers = await _execute(
            templates["customers-in-counties"],
            {"q_county": "Columbia"},
            store=gateway,
            groups=groups_by_persona["frontline"],
        )
        assert customers.rows
        with pytest.raises(VerifiedAnswerScopeError):
            await _execute(
                templates["autoloans-for-customer"],
                {"q_customer_id": "mer-cus-001"},
                store=gateway,
                groups=groups_by_persona["frontline"],
            )
        with pytest.raises(VerifiedAnswerScopeError):
            await _execute(
                templates["disputes-directory"],
                {},
                store=gateway,
                groups=groups_by_persona["frontline"],
            )

        # The complete governed answer for the SAME question, per persona.  Frontline
        # reaches EXACTLY TWO of the seven Meridian templates: the customer directory
        # and the FEMA declaration that scopes it.  The declaration sits in the
        # customer-labelled graph deliberately — it is public FEMA information (a
        # declaration id, an incident, a date, a state, its designated counties and
        # the agency URL), it carries no customer, account, loan or dispute fact, and
        # it is the scope that makes the county sweep meaningful.  Every OTHER
        # template is a REFUSAL raised before the store is touched — never an empty
        # result set and never a fabricated zero.
        frontline_refused = {
            "autoloans-for-customer": {"q_customer_id": "mer-cus-001"},
            "payments-due-window": {
                "q_loan_id": "mer-loan-001",
                "window_start": truth["b1"]["window_start"],
                "window_end": truth["b1"]["window_end"],
            },
            "servicing-notes-for-loan": {"q_loan_id": "mer-loan-001"},
            "disputes-directory": {},
            "open-disputes-by-status": {"q_status": "open"},
            "overdue-provisional-credit-disputes": {},
        }
        # The two T3 record-browser templates declare ALL THREE ACL graph classes,
        # so frontline's held ``bank.customer.read`` satisfies the capability gate
        # and C7 trimming does the narrowing: frontline enumerates customer-graph
        # classes but sees ZERO rows for a dispute-graph class (asserted below).
        # ``hardship-candidate-loans`` declares customer + servicing classes for its
        # cross-graph join, so it follows the SAME trimming rule: frontline's held
        # customer label passes the gate and the servicing-graph triples the join
        # needs are simply not in its dataset — zero rows, with the zero-row scoping
        # note, never a partial answer (asserted below).
        assert set(frontline_refused) | {
            "customers-in-counties",
            "disaster-declarations",
            "hardship-candidate-loans",
            "t3-record",
            "t3-records-in-class",
        } == set(templates)

        frontline_candidates = await _execute(
            templates["hardship-candidate-loans"],
            {"q_declaration_id": "DR-4830", "q_as_of_date": banking["as_of_date"]},
            store=gateway,
            groups=groups_by_persona["frontline"],
        )
        assert frontline_candidates.rows == ()
        full_candidates = await _execute(
            templates["hardship-candidate-loans"],
            {"q_declaration_id": "DR-4830", "q_as_of_date": banking["as_of_date"]},
            store=gateway,
            groups=("bank.customer.read", "bank.servicing.read"),
        )
        assert len(full_candidates.rows) > 0

        frontline_disputes = await _execute(
            templates["t3-records-in-class"],
            {"q_class": "RegEDispute"},
            store=gateway,
            groups=groups_by_persona["frontline"],
        )
        assert frontline_disputes.rows == ()
        assert frontline_disputes.total_rows == 0
        full_disputes = await _execute(
            templates["t3-records-in-class"],
            {"q_class": "RegEDispute"},
            store=gateway,
            groups=("bank.customer.read", "bank.servicing.read", "bank.dispute.read"),
        )
        assert len(full_disputes.rows) > 0

        # Frontline reaches the declaration itself, and it is the SAME finite county
        # list the sweep above was run over — the scope is served, not assumed.
        declarations = await _execute(
            templates["disaster-declarations"],
            {},
            store=gateway,
            groups=groups_by_persona["frontline"],
        )
        assert {row[5] for row in declarations.rows} == set(
            banking["disaster_declarations"][0]["counties"]
        )
        for template_id, args in frontline_refused.items():
            recorder = RecordingStore(gateway)
            with pytest.raises(VerifiedAnswerScopeError):
                await _execute(
                    templates[template_id],
                    args,
                    store=recorder,
                    groups=groups_by_persona["frontline"],
                )
            # Refused, not empty: the scope intersection fails closed BEFORE any read,
            # so there is no query whose zero rows could be mistaken for an answer.
            assert recorder.scoped_calls == 0, template_id

        # Approver reaches a STRICTLY LARGER set over the same corpus: every template
        # frontline is refused, plus the one it shares.
        for template_id, args in frontline_refused.items():
            answer = await _execute(
                templates[template_id],
                args,
                store=gateway,
                groups=groups_by_persona["approver"],
            )
            assert answer is not None, template_id
        approver_customers = await _execute(
            templates["customers-in-counties"],
            {"q_county": "Columbia"},
            store=gateway,
            groups=groups_by_persona["approver"],
        )
        assert approver_customers.rows == customers.rows

        # A subject-specific read returns only the requested subject's rows.
        loans = await _execute(
            templates["autoloans-for-customer"],
            {"q_customer_id": "mer-cus-001"},
            store=gateway,
            groups=groups_by_persona["ops"],
        )
        assert loans.rows
        assert {row[2] for row in loans.rows} == {"mer-cus-001"}
        assert "mer-cus-002" not in repr(loans.rows)

        # Invalid vocabularies/identifiers, unknown keys, reversed windows and
        # over-wide windows are refusals with zero store calls—not empty answers.
        recording = RecordingStore(gateway)
        bad_calls = [
            (templates["customers-in-counties"], {"q_county": "Fulton"}),
            (templates["autoloans-for-customer"], {"q_customer_id": "mer-cus-001'"}),
            (templates["open-disputes-by-status"], {"q_status": "pending"}),
            (
                templates["payments-due-window"],
                {
                    "q_loan_id": "mer-loan-001",
                    "window_start": "08/18/2026",
                    "window_end": "2026-09-01",
                },
            ),
            (
                templates["payments-due-window"],
                {
                    "q_loan_id": "mer-loan-001",
                    "window_start": "2026-09-02",
                    "window_end": "2026-09-01",
                },
            ),
            (
                templates["payments-due-window"],
                {
                    "q_loan_id": "mer-loan-001",
                    "window_start": "2026-01-01",
                    "window_end": "2026-09-01",
                },
            ),
            (templates["disputes-directory"], {"status": "open"}),
        ]
        for template, args in bad_calls:
            with pytest.raises(VerifiedAnswerArgsError):
                await _execute(
                    template,
                    args,
                    store=recording,
                    groups=("bank.customer.read", "bank.servicing.read", "bank.dispute.read"),
                )
        assert recording.scoped_calls == 0

    asyncio.run(scenario())


def test_disaster_declaration_is_served_from_the_generator_table_alone() -> None:
    """The declaration the demo asks about is SERVED, not assumed by the caller.

    Before this template the FEMA table existed only in ``banking.json``: the county
    list reached the B1 sweep as a frozen workflow input, so an agent asked about
    DR-4830 had no governed row naming it and could not ground the case identifier it
    had just been handed.  Every value asserted here is read back out of the fixture
    in this test — nothing is written as a literal, so a fixture edit moves the
    expectation instead of silently disagreeing with it.
    """

    async def scenario() -> None:
        banking, _truth = _documents()
        store = make_gateway()
        await load_meridian_verified_fixture(store, _FIXTURE)

        answer = await _execute(
            _templates()["disaster-declarations"],
            {},
            store=store,
            groups=("bank.customer.read",),
        )
        source = banking["disaster_declarations"]
        assert answer.total_rows == answer.returned_rows
        # One row per designated county — the declaration's scope is enumerated, and
        # the whole finite list is present rather than a retrieved sample of it.
        assert answer.returned_rows == sum(len(row["counties"]) for row in source)

        rows = [dict(zip(answer.columns, row, strict=True)) for row in answer.rows]
        by_id: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            by_id.setdefault(row["declaration_id"], []).append(row)
        assert set(by_id) == {row["declaration_id"] for row in source}

        for declaration in source:
            served = by_id[declaration["declaration_id"]]
            assert {row["county"] for row in served} == set(declaration["counties"])
            for row in served:
                # The identifier and the facts asserted about it share a row, so a
                # cell boundary never separates DR-4830 from its own scope caveat.
                assert row["incident"] == declaration["incident"]
                assert row["declaration_date"] == declaration["declaration_date"]
                assert row["state"] == declaration["state"]
                assert row["scope_description"] == declaration["scope_description"]
                assert row["source_url"] == declaration["source_url"]
                assert row["declaration"] == (
                    f"urn:ne:meridian:disaster:{declaration['declaration_id']}"
                )

        # The served counties are EXACTLY the sweep the customer template is called
        # with, so the two templates compose into one complete population.
        counties = {row["county"] for row in rows}
        affected: set[str] = set()
        for county in sorted(counties):
            customers = await _execute(
                _templates()["customers-in-counties"],
                {"q_county": county},
                store=store,
                groups=("bank.customer.read",),
            )
            affected.update(row[1] for row in customers.rows)
        assert affected == {
            row["customer_id"]
            for row in banking["customers"]
            if row["address"]["county"] in counties
        }

        repeated = await _execute(
            _templates()["disaster-declarations"],
            {},
            store=store,
            groups=("bank.customer.read",),
        )
        assert answers_equal(answer, repeated)

    asyncio.run(scenario())


def test_declaration_without_counties_fails_loud_rather_than_losing_its_scope() -> None:
    """An empty county list must not yield a declaration whose scope quietly vanishes.

    The template joins the declaration to its counties, so a countyless row would be
    dropped by the inner join and the declaration would be absent from an answer that
    still reported itself complete.  The loader refuses it instead.
    """
    from ne.brain_ontology.verified.meridian import _disaster_triples

    banking, _truth = _documents()
    declaration = dict(banking["disaster_declarations"][0])

    # Control: the unmodified generator row projects, so the guard below is
    # discriminating between an empty county list and any county list at all.
    assert _disaster_triples({"disaster_declarations": [declaration]})

    for broken in ([], "Columbia", [""], ["Columbia", 4]):
        with pytest.raises(ValueError, match="non-empty list of strings"):
            _disaster_triples({"disaster_declarations": [{**declaration, "counties": broken}]})


def test_disaster_declaration_projects_only_fields_the_generator_actually_has() -> None:
    """No invented banking data: the projection's predicate set is fixture-derived.

    ``_dispute_triples`` already demonstrates the failure mode this guards — the
    dispute template OPTIONALs ``provisional_credit_issue_date``, a field
    ``banking.json`` does not contain, so that column is permanently blank.  The
    declaration projection is pinned to the generator's own keys so a future template
    cannot quietly acquire a column with nothing behind it.
    """
    from ne.brain_ontology.verified.meridian import _PRED, _disaster_triples

    banking, _truth = _documents()
    source_keys = set(banking["disaster_declarations"][0])
    predicates = {
        triple[1].removeprefix(_PRED)
        for triple in _disaster_triples(banking)
        if triple[1].startswith(_PRED)
    }
    # ``corpus_version`` is loader provenance, not a banking fact; ``county`` is the
    # singular projection of the fixture's ``counties`` list.
    assert predicates - {"corpus_version", "county"} <= source_keys
    assert "counties" not in predicates


def test_servicing_as_of_anchor_is_served_in_row_from_the_fixture_alone() -> None:
    """The snapshot's as-of date is SERVED evidence, not prose nobody published.

    The governing SOP measures its hardship window "from the as-of date" but never
    states the date, and the generator's ``as_of_date`` appeared nowhere in the
    servicing lane's served surface — the model had to invent the anchor, and live it
    anchored on the wrong day and dropped the day-N loan.  Both servicing templates
    now carry the snapshot's ``as_of_date`` on EVERY row, so the anchor arrives the
    same way every other fact does and sits in the same row as the loan/payment ids
    it is asserted with (the grounding gate's row-binding rule refuses a numeral
    quoted against a row that does not carry it).  Every expected value here is read
    back out of the fixture — nothing is a literal, never the system clock.
    """

    async def scenario() -> None:
        banking, truth = _documents()
        served_as_of = banking["as_of_date"]
        store = make_gateway()
        await load_meridian_verified_fixture(store, _FIXTURE)
        templates = _templates()

        loans = await _execute(
            templates["autoloans-for-customer"],
            {"q_customer_id": banking["auto_loans"][0]["customer_id"]},
            store=store,
            groups=("bank.servicing.read",),
        )
        assert "as_of_date" in loans.columns
        assert loans.returned_rows > 0
        for row in loans.rows:
            named = dict(zip(loans.columns, row, strict=True))
            # The anchor and the loan id share one row.
            assert named["as_of_date"] == served_as_of
            assert named["loan_id"]

        payments = await _execute(
            templates["payments-due-window"],
            {
                "q_loan_id": banking["payment_schedules"][0]["loan_id"],
                "window_start": truth["b1"]["window_start"],
                "window_end": truth["b1"]["window_end"],
            },
            store=store,
            groups=("bank.servicing.read",),
        )
        assert "as_of_date" in payments.columns
        assert payments.returned_rows > 0
        for row in payments.rows:
            named = dict(zip(payments.columns, row, strict=True))
            assert named["as_of_date"] == served_as_of
            assert named["loan_id"]

        # The ground truth's own window anchors on exactly the served value: a model
        # that reads the anchor from a served loan row starts the window on the
        # generator's day zero, not on a guess and not on the deployment clock.
        assert truth["b1"]["window_start"] == served_as_of

    asyncio.run(scenario())


def test_b1_chain_over_served_rows_alone_reproduces_the_actionable_set() -> None:
    """The flagship chain, driven ONLY by served evidence, yields the truth set.

    Live, the answer was one loan short: with no served anchor the model invented a
    window start and called the day-N payment "outside window".  This walks the same
    template chain the agent walks — declaration counties, affected customers, their
    loans, payments in the window, servicing notes — taking the window's start from
    the SERVED ``as_of_date`` cell and nothing else, and must reproduce the ground
    truth's actionable rows exactly, inclusive far-endpoint membership included.
    """

    async def scenario() -> None:
        _banking, truth = _documents()
        store = make_gateway()
        await load_meridian_verified_fixture(store, _FIXTURE)
        templates = _templates()

        declarations = await _execute(
            templates["disaster-declarations"],
            {},
            store=store,
            groups=("bank.customer.read",),
        )
        declaration_rows = [
            dict(zip(declarations.columns, row, strict=True)) for row in declarations.rows
        ]
        counties = sorted({row["county"] for row in declaration_rows})

        affected_customers: set[str] = set()
        for county in counties:
            customers = await _execute(
                templates["customers-in-counties"],
                {"q_county": county},
                store=store,
                groups=("bank.customer.read",),
            )
            affected_customers.update(
                dict(zip(customers.columns, row, strict=True))["customer_id"]
                for row in customers.rows
            )

        loan_rows: list[dict[str, str]] = []
        for customer_id in sorted(affected_customers):
            loans = await _execute(
                templates["autoloans-for-customer"],
                {"q_customer_id": customer_id},
                store=store,
                groups=("bank.servicing.read",),
            )
            loan_rows.extend(dict(zip(loans.columns, row, strict=True)) for row in loans.rows)

        # The window's anchor comes from the served rows, never from this test.
        served_anchors = {row["as_of_date"] for row in loan_rows}
        assert len(served_anchors) == 1
        window_start = served_anchors.pop()
        window_end = truth["b1"]["window_end"]
        assert window_start == truth["b1"]["window_start"]

        actionable: set[str] = set()
        far_endpoint_members: set[str] = set()
        for row in sorted(loan_rows, key=lambda r: r["loan_id"]):
            if row["status"] != "active":
                continue
            loan_id = row["loan_id"]
            payments = await _execute(
                templates["payments-due-window"],
                {
                    "q_loan_id": loan_id,
                    "window_start": window_start,
                    "window_end": window_end,
                },
                store=store,
                groups=("bank.servicing.read",),
            )
            scheduled = [
                named
                for named in (
                    dict(zip(payments.columns, prow, strict=True)) for prow in payments.rows
                )
                if named["status"] == "scheduled"
            ]
            if not scheduled:
                continue
            if any(named["due_date"] == window_end for named in scheduled):
                far_endpoint_members.add(loan_id)
            notes = await _execute(
                templates["servicing-notes-for-loan"],
                {"q_loan_id": loan_id},
                store=store,
                groups=("bank.servicing.read",),
            )
            note_types = {
                dict(zip(notes.columns, nrow, strict=True))["note_type"] for nrow in notes.rows
            }
            if note_types & {"hardship_offered", "repossession"}:
                continue
            actionable.add(loan_id)

        expected = {row["loan_id"] for row in truth["b1"]["actionable_rows"]}
        assert actionable == expected

        # The regression that motivated the anchor: a payment due ON the window's
        # far endpoint is INSIDE the inclusive window, and at least one actionable
        # loan sits exactly there — the loan the live run wrongly excluded.
        assert far_endpoint_members & expected

    asyncio.run(scenario())


def _exact_dollars(cents: object) -> str:
    """The one legal dollar rendering of an integer cents value — recomputed here
    INDEPENDENTLY of the loader (divmod on the integer, remainder zero-padded),
    so a loader that drifted to floats, rounding, or locale would fail."""
    units, remainder = divmod(int(str(cents)), 100)
    assert int(str(cents)) >= 0
    return f"${units}.{remainder:02d}"


def test_every_monetary_cents_cell_is_served_with_its_exact_dollar_sibling() -> None:
    """NE-MERIDIAN-DOLLARS: property-style over the fixture's REAL values.

    The grounding gate's F2 rule makes a currency symbol meaning-bearing and has
    (by design) NO currency recompute, so ``$200.00`` grounds only against a served
    cell — this test proves every monetary column on every monetary template serves
    that cell, and that the dollars cell is the exact fixed-point rendering of the
    cents cell it sits beside.  The cents column stays the system of record: a
    dollars cell that disagreed with its cents sibling would be a fabrication
    served as evidence, so equality is asserted per row, for every fixture row.
    New columns are APPENDED — the leading column order every positional consumer
    relies on is pinned unchanged.
    """

    async def scenario() -> None:
        banking, truth = _documents()
        store = make_gateway()
        await load_meridian_verified_fixture(store, _FIXTURE)
        templates = _templates()
        checked = 0

        # autoloans-for-customer: principal_balance_cents -> principal_balance_dollars.
        loans_template = templates["autoloans-for-customer"]
        assert list(loans_template.answer.columns)[:7] == [
            "loan",
            "loan_id",
            "customer_id",
            "status",
            "principal_balance_cents",
            "next_due_date",
            "as_of_date",
        ]
        for source in banking["auto_loans"]:
            answer = await _execute(
                loans_template,
                {"q_customer_id": source["customer_id"]},
                store=store,
                groups=("bank.servicing.read",),
            )
            for row in answer.rows:
                named = dict(zip(answer.columns, row, strict=True))
                if named["loan_id"] != source["loan_id"]:
                    continue
                assert named["principal_balance_cents"] == str(source["principal_balance_cents"])
                assert named["principal_balance_dollars"] == _exact_dollars(
                    named["principal_balance_cents"]
                )
                checked += 1

        # payments-due-window: amount_cents -> amount_dollars (whole population via
        # each payment's own due date as a one-day window).
        payments_template = templates["payments-due-window"]
        for source in banking["payment_schedules"]:
            answer = await _execute(
                payments_template,
                {
                    "q_loan_id": source["loan_id"],
                    "window_start": source["due_date"],
                    "window_end": source["due_date"],
                },
                store=store,
                groups=("bank.servicing.read",),
            )
            for row in answer.rows:
                named = dict(zip(answer.columns, row, strict=True))
                if named["payment_id"] != source["payment_id"]:
                    continue
                assert named["amount_cents"] == str(source["amount_cents"])
                assert named["amount_dollars"] == _exact_dollars(named["amount_cents"])
                checked += 1

        # disputes-directory: amount_cents -> amount_dollars and the OPTIONAL
        # credit pair — the dollars cell is blank EXACTLY when the cents cell is.
        directory = await _execute(
            templates["disputes-directory"],
            {},
            store=store,
            groups=("bank.dispute.read",),
        )
        source_disputes = {row["dispute_id"]: row for row in banking["reg_e_disputes"]}
        assert directory.returned_rows == len(source_disputes)
        for row in directory.rows:
            named = dict(zip(directory.columns, row, strict=True))
            source = source_disputes[named["dispute_id"]]
            assert named["amount_cents"] == str(source["amount_cents"])
            assert named["amount_dollars"] == _exact_dollars(named["amount_cents"])
            checked += 1
            if named["credit_amount_cents"] == "":
                assert named["credit_amount_dollars"] == ""
                assert source.get("provisional_credit_amount_cents") is None
            else:
                assert named["credit_amount_cents"] == str(
                    source["provisional_credit_amount_cents"]
                )
                assert named["credit_amount_dollars"] == _exact_dollars(
                    named["credit_amount_cents"]
                )
                checked += 1

        # open-disputes-by-status: amount_cents -> amount_dollars on the dispute
        # side, expedited_threshold_cents -> expedited_threshold_dollars on the
        # joined current SOP.
        threshold_cents = next(
            row["threshold_cents"]
            for row in truth["b2"]["answers_by_version"]
            if row.get("current") is True
        )
        for status in ("open", "closed", "withdrawn"):
            answer = await _execute(
                templates["open-disputes-by-status"],
                {"q_status": status},
                store=store,
                groups=("bank.dispute.read",),
            )
            for row in answer.rows:
                named = dict(zip(answer.columns, row, strict=True))
                source = source_disputes[named["dispute_id"]]
                assert named["amount_cents"] == str(source["amount_cents"])
                assert named["amount_dollars"] == _exact_dollars(named["amount_cents"])
                assert named["expedited_threshold_cents"] == str(threshold_cents)
                assert named["expedited_threshold_dollars"] == _exact_dollars(
                    named["expedited_threshold_cents"]
                )
                checked += 2

        # Non-vacuous: every fixture population above actually contributed.
        assert checked >= (
            len(banking["auto_loans"])
            + len(banking["payment_schedules"])
            + len(banking["reg_e_disputes"])
        )

    asyncio.run(scenario())


# --- S9: the materialised provisional-credit deadline + the argument-free overdue read


def _independent_business_day_deadline(notice: str, business_days: int) -> str:
    """Recompute the deadline INDEPENDENTLY of the loader: N week-days after notice."""
    from datetime import date, timedelta

    day = date.fromisoformat(notice)
    remaining = business_days
    while remaining:
        day += timedelta(days=1)
        if day.weekday() < 5:
            remaining -= 1
    return day.isoformat()


def test_every_dispute_carries_a_materialised_deadline_derived_from_served_facts() -> None:
    """NE-S9-DEADLINE: the deadline is a load-time fact with provenance, per dispute.

    Every expected value is recomputed HERE from the fixture's own rows — the
    current policy's tier selection (expedited iff amount_cents >= threshold) and
    an independent weekend-skipping walk — so a loader that drifted to calendar
    days, the wrong tier, or a stale policy fails this test on real values.
    """
    from ne.brain_ontology.gateway import DeclaredLiteral
    from ne.brain_ontology.verified.meridian import _PRED, _dispute_triples

    banking, truth = _documents()
    current = next(row for row in truth["b2"]["answers_by_version"] if row.get("current") is True)
    standard = next(
        row for row in truth["b2"]["answers_by_version"] if row.get("threshold_cents") is None
    )
    served: dict[str, dict[str, str]] = {}
    for subject, predicate, obj in _dispute_triples(banking, truth):
        name = predicate.removeprefix(_PRED)
        if (
            name
            in {
                "provisional_credit_deadline_date",
                "provisional_credit_deadline_business_days",
                "provisional_credit_deadline_policy",
                "as_of_date",
            }
            and ":dispute:" in subject
        ):
            assert isinstance(obj, DeclaredLiteral)
            served.setdefault(subject.rsplit(":", 1)[-1], {})[name] = obj.lexical

    disputes = {row["dispute_id"]: row for row in banking["reg_e_disputes"]}
    assert set(served) == set(disputes)
    for dispute_id, row in disputes.items():
        threshold = current.get("threshold_cents")
        tier = (
            int(current["deadline_business_days"])
            if threshold is not None and int(row["amount_cents"]) >= int(threshold)
            else int(standard["deadline_business_days"])
        )
        expected = _independent_business_day_deadline(row["notice_date"], tier)
        cells = served[dispute_id]
        assert cells["provisional_credit_deadline_date"] == expected, dispute_id
        assert cells["provisional_credit_deadline_business_days"] == str(tier)
        assert cells["provisional_credit_deadline_policy"] == current["artifact_id"]
        assert cells["as_of_date"] == banking["as_of_date"]


def test_overdue_provisional_credit_read_matches_ground_truth_and_is_deterministic() -> None:
    """The argument-free overdue read serves EXACTLY the b2 ground-truth set.

    The overdue rule is a served-cell comparison (as-of strictly past the
    materialised deadline) — no caller input, no run-time business-day
    arithmetic — and two independent executions over two independently loaded
    stores must be byte-identical, columns, rows and order alike.
    """

    async def scenario() -> None:
        banking, truth = _documents()
        templates = _templates()
        template = templates["overdue-provisional-credit-disputes"]
        assert template.args == {}

        results = []
        for _ in range(2):
            store = make_gateway()
            await load_meridian_verified_fixture(store, _FIXTURE)
            answer = await _execute(template, {}, store=store, groups=("bank.dispute.read",))
            results.append((answer.columns, answer.rows))

        assert results[0] == results[1]
        columns, rows = results[0]
        named = [dict(zip(columns, row, strict=True)) for row in rows]
        assert [row["dispute_id"] for row in named] == truth["b2"]["current_answer_dispute_ids"]
        for row in named:
            assert row["status"] == "open"
            assert row["credit_amount_recorded"] == "false"
            assert row["as_of_date"] > row["provisional_credit_deadline_date"]
        # The near-miss distractors are excluded for exactly the served-cell
        # reasons the demo narrates: mer-dsp-002's deadline is still ahead of the
        # as-of date, and mer-dsp-004's deadline IS the as-of date (deadline day,
        # not overdue) — both recomputed here from the fixture, not asserted as
        # literals.
        current = next(
            row for row in truth["b2"]["answers_by_version"] if row.get("current") is True
        )
        standard = next(
            row for row in truth["b2"]["answers_by_version"] if row.get("threshold_cents") is None
        )
        by_id = {row["dispute_id"]: row for row in banking["reg_e_disputes"]}
        deadline_002 = _independent_business_day_deadline(
            by_id["mer-dsp-002"]["notice_date"], int(current["deadline_business_days"])
        )
        deadline_004 = _independent_business_day_deadline(
            by_id["mer-dsp-004"]["notice_date"], int(standard["deadline_business_days"])
        )
        assert deadline_002 > banking["as_of_date"]
        assert deadline_004 == banking["as_of_date"]

    asyncio.run(scenario())


def test_hardship_candidate_read_serves_the_positive_criteria_set_in_one_call() -> None:
    """One call serves every ACTIVE loan with a scheduled payment in the derived window.

    The expected candidate set is recomputed HERE from the fixture alone (active
    loans of declared-county customers with a scheduled payment inside the
    inclusive 14-day window from the as-of date); the exclusion criteria stay a
    separate complete servicing-notes read by design — the verified lane refuses
    negation in a template body so every projected cell is provably graph-joined.
    Applying that exclusion over the served candidates must reproduce the b1
    ground truth exactly.
    """

    async def scenario() -> None:
        from datetime import date, timedelta

        banking, truth = _documents()
        store = make_gateway()
        await load_meridian_verified_fixture(store, _FIXTURE)
        templates = _templates()

        as_of = banking["as_of_date"]
        window_end = (date.fromisoformat(as_of) + timedelta(days=14)).isoformat()
        declared_counties = {
            county for row in banking["disaster_declarations"] for county in row["counties"]
        }
        county_of = {row["customer_id"]: row["address"]["county"] for row in banking["customers"]}
        expected_candidates = set()
        for loan in banking["auto_loans"]:
            if loan["status"] != "active":
                continue
            if county_of[loan["customer_id"]] not in declared_counties:
                continue
            if any(
                payment["loan_id"] == loan["loan_id"]
                and payment["status"] == "scheduled"
                and as_of <= payment["due_date"] <= window_end
                for payment in banking["payment_schedules"]
            ):
                expected_candidates.add(loan["loan_id"])

        answer = await _execute(
            templates["hardship-candidate-loans"],
            {"q_declaration_id": "DR-4830", "q_as_of_date": as_of},
            store=store,
            groups=("bank.customer.read", "bank.servicing.read"),
        )
        named = [dict(zip(answer.columns, row, strict=True)) for row in answer.rows]
        assert {row["loan_id"] for row in named} == expected_candidates
        for row in named:
            assert row["loan_status"] == "active"
            assert row["payment_status"] == "scheduled"
            assert as_of <= row["due_date"] <= window_end
            assert row["as_of_date"] == as_of

        # The far-endpoint membership the live regression turned on: a payment due
        # ON as_of + 14 days is INSIDE the derived inclusive window.
        assert any(row["due_date"] == window_end for row in named)

        # Applying the SOP's exclusion criteria over a complete servicing-notes
        # read of the candidates reproduces the b1 actionable ground truth.
        excluded_types = {"hardship_offered", "repossession"}
        note_types_by_loan: dict[str, set[str]] = {}
        for note in banking["servicing_notes"]:
            note_types_by_loan.setdefault(note["loan_id"], set()).add(note["note_type"])
        eligible = {
            loan_id
            for loan_id in expected_candidates
            if not (note_types_by_loan.get(loan_id, set()) & excluded_types)
        }
        assert eligible == {row["loan_id"] for row in truth["b1"]["actionable_rows"]}

    asyncio.run(scenario())
