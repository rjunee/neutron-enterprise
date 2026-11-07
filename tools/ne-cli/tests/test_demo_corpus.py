# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO corpus adapter: acme fixtures → loadable, ACL-faithful, deterministic
FixtureConnector JSON + valid scenario selection against ground_truth.json."""

from __future__ import annotations

import json
from pathlib import Path

from ne.acme.cli import default_fixtures_dir
from ne.cli.demo.corpus import (
    SOURCE,
    T3_AMBIGUOUS_PROMPTS,
    T3_CANONICAL_TRIGGER,
    T3_CLARIFY_PROMPT,
    T3_QUESTION,
    T3_TEMPLATE_DESCRIPTION,
    build_graph_snapshot,
    build_scenario,
    demo_doc_subset,
    demo_document_id,
    export_connector_fixture,
    graph_document_key,
    write_verified_templates,
)
from ne.connect.testing.fixture_connector import FixtureConnector

FIXTURES = default_fixtures_dir()


def _content() -> dict[str, str]:
    return json.loads((FIXTURES / "content.json").read_text(encoding="utf-8"))


def _acl_by_id() -> dict[str, dict]:
    rows = json.loads((FIXTURES / "acl.json").read_text(encoding="utf-8"))
    return {r["doc_id"]: r for r in rows}


def test_export_is_fixtureconnector_loadable(tmp_path: Path) -> None:
    subset = demo_doc_subset(FIXTURES)
    out = tmp_path / "acme-fixture.json"
    count = export_connector_fixture(FIXTURES, out, doc_ids=subset)
    assert count == len(subset)

    connector = FixtureConnector.from_path(out)
    assert connector.source == SOURCE

    import asyncio

    async def _drain() -> tuple[int, int, int]:
        objects = await connector.discover()
        records = [r async for r in connector.backfill(None)]
        acl = [a async for a in connector.permissions()]
        return len(objects), len(records), len(acl)

    n_obj, n_rec, n_acl = asyncio.run(_drain())
    assert n_obj == n_rec == n_acl == len(subset)


def test_per_doc_acl_fidelity(tmp_path: Path) -> None:
    scenario = build_scenario(FIXTURES)
    subset = demo_doc_subset(FIXTURES)
    out = tmp_path / "acme-fixture.json"
    export_connector_fixture(FIXTURES, out, doc_ids=subset)
    data = json.loads(out.read_text(encoding="utf-8"))
    acl_by_resource = {a["resource_id"]: a["principals"] for a in data["acl"]}

    # The exec-only trap doc's principals are exactly ['executive'].
    trap_res = f"acme/docs/{scenario.trap_doc_id}"
    assert acl_by_resource[trap_res] == ["executive"]

    # A named doc's principals include its owned_by person id.
    acl_by_id = _acl_by_id()
    named = next(
        (d for d in subset if acl_by_id.get(d, {}).get("visibility") == "named"),
        None,
    )
    if named is not None:
        principals = acl_by_resource[f"acme/docs/{named}"]
        allowed_people = acl_by_id[named]["allowed_people"]
        assert set(allowed_people) <= set(principals)
        # The named doc must expose at least one person principal.
        assert allowed_people


def test_export_is_byte_deterministic(tmp_path: Path) -> None:
    subset = demo_doc_subset(FIXTURES)
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    export_connector_fixture(FIXTURES, a, doc_ids=subset)
    export_connector_fixture(FIXTURES, b, doc_ids=subset)
    assert a.read_bytes() == b.read_bytes()


def test_content_carries_the_body(tmp_path: Path) -> None:
    subset = demo_doc_subset(FIXTURES)
    out = tmp_path / "acme-fixture.json"
    export_connector_fixture(FIXTURES, out, doc_ids=subset)
    data = json.loads(out.read_text(encoding="utf-8"))
    by_record = {r["record_id"]: r for r in data["records"]}
    content = _content()
    for doc_id in subset:
        payload = by_record[doc_id]["payload"]
        assert payload["content"] == content[doc_id]
        assert payload["content"].strip()


def test_scenario_selection_is_valid() -> None:
    scenario = build_scenario(FIXTURES)
    gt = json.loads((FIXTURES / "ground_truth.json").read_text(encoding="utf-8"))
    gt_by_id = {d["doc_id"]: d for d in gt["docs"]}
    content = _content()

    # Reader/outsider are distinct people.
    assert scenario.reader_id != scenario.outsider_id
    assert "dept-engineering" in scenario.reader_groups

    # The outsider is NOT a reader of the trap doc (deny-on-read premise).
    assert scenario.outsider_id not in set(gt_by_id[scenario.trap_doc_id]["reader_ids"])

    # t1_answer is a substring of the T1 doc's content (so retrieval can surface it).
    assert scenario.t1_answer in content[scenario.t1_doc_id]

    # The computed trap document id matches the corpus helper.
    assert scenario.trap_document_id == demo_document_id(scenario.trap_doc_id)


# --- NE-DEMO-LIVE-SEAMS: the governed ACL-labeled T2 graph snapshot -----------
def _snapshot(doc_ids=None):
    return build_graph_snapshot(FIXTURES, doc_ids=doc_ids)


def test_graph_snapshot_is_byte_deterministic() -> None:
    import json as _json

    sub = demo_doc_subset(FIXTURES)
    a = _snapshot(sub)
    b = _snapshot(sub)
    assert _json.dumps(a, sort_keys=True) == _json.dumps(b, sort_keys=True)


def test_graph_snapshot_every_element_carries_real_acl() -> None:
    snap = _snapshot(demo_doc_subset(FIXTURES))
    assert snap["nodes"] and snap["edges"]
    for node in snap["nodes"]:
        assert node.get("acl"), node["key"]
    for edge in snap["edges"]:
        assert edge.get("acl"), edge["key"]


def test_graph_snapshot_trap_doc_carries_exec_acl() -> None:
    scenario = build_scenario(FIXTURES)
    subset = demo_doc_subset(FIXTURES)
    snap = _snapshot(subset)
    acl_by_id = _acl_by_id()
    row = acl_by_id[scenario.trap_doc_id]
    expected = [*row["allowed_groups"], *row["allowed_people"]]

    trap_node = next(n for n in snap["nodes"] if n["key"] == scenario.t2_trap_key)
    # Dual-key honesty: the trap document node carries acl.json's exec-only principals.
    assert list(trap_node["acl"]) == expected
    assert "executive" in trap_node["acl"]

    # Its OWNED_BY edge carries the SAME exec-only acl (never fabricated — C7).
    owned = [
        e
        for e in snap["edges"]
        if e["from_key"] == scenario.t2_trap_key and e["edge_kind"] == "OWNED_BY"
    ]
    assert owned
    for edge in owned:
        assert list(edge["acl"]) == expected


def test_graph_snapshot_entity_nodes_are_all_employees() -> None:
    snap = _snapshot(demo_doc_subset(FIXTURES))
    for node in snap["nodes"]:
        if node["entity_class"] in ("Account", "Person"):
            assert list(node["acl"]) == ["all-employees"], node["key"]


def test_graph_document_key_is_deterministic() -> None:
    scenario = build_scenario(FIXTURES)
    assert scenario.t2_anchor_key == graph_document_key(scenario.t1_doc_id)
    assert scenario.t2_trap_key == graph_document_key(scenario.trap_doc_id)
    # The graph node key is DISTINCT from the chunk document id (colon vs slash source).
    assert scenario.t2_trap_key != scenario.trap_document_id


def test_graph_snapshot_anchor_and_trap_present() -> None:
    scenario = build_scenario(FIXTURES)
    subset = demo_doc_subset(FIXTURES)
    snap = _snapshot(subset)
    keys = {n["key"] for n in snap["nodes"]}
    assert scenario.t2_anchor_key in keys
    assert scenario.t2_trap_key in keys


# --- NE-VAF-U5: deterministic routing ACTIVATED in the demo corpus -----------


def test_routing_block_loads_and_routes_t3_question(tmp_path: Path) -> None:
    """The corpus bundle loads through the REAL U1 registry (zero-arg + multi-word
    guard load gates pass) and the T3 question deterministically ROUTEs to
    accounts-directory — the routing-activation proof."""
    from ne.brain_ontology.verified.routing import build_routing_table, match_template
    from ne.brain_ontology.verified.templates import load_template_registry

    registry = load_template_registry(write_verified_templates(tmp_path / "vt"))
    rows = build_routing_table(registry)
    assert len(rows) == 1
    assert rows[0].template_id == "accounts-directory"
    assert rows[0].clarify_max == 2

    decision = match_template(T3_QUESTION, rows)
    assert decision.outcome == "route"
    assert decision.template_id == "accounts-directory"


def test_routing_clarify_and_selection(tmp_path: Path) -> None:
    """An in-domain but ambiguous prompt CLARIFYs (guard hit, no trigger); the
    clarify follow-up ordinal grammar resolves to accounts-directory."""
    from ne.brain_ontology.verified.routing import (
        build_routing_table,
        match_selection,
        match_template,
    )
    from ne.brain_ontology.verified.templates import load_template_registry

    registry = load_template_registry(write_verified_templates(tmp_path / "vt"))
    rows = build_routing_table(registry)

    assert match_template(T3_CLARIFY_PROMPT, rows).outcome == "clarify"
    for prompt in T3_AMBIGUOUS_PROMPTS:
        assert match_template(prompt, rows).outcome == "clarify", prompt

    assert match_selection("1", rows) == "accounts-directory"


def test_routing_constants_match_template_bytes(tmp_path: Path) -> None:
    """The arc's menu assertions (T3_TEMPLATE_DESCRIPTION / T3_CANONICAL_TRIGGER)
    byte-match the loaded template's actual description/triggers[0] — no drift
    between the corpus constants and the TOML bytes."""
    from ne.brain_ontology.verified.templates import load_template_registry

    registry = load_template_registry(write_verified_templates(tmp_path / "vt"))
    template = next(t for t in registry.templates if t.template_id == "accounts-directory")
    assert template.description == T3_TEMPLATE_DESCRIPTION
    assert template.routing is not None
    assert template.routing.triggers[0] == T3_CANONICAL_TRIGGER


def test_shipped_templates_declare_the_label_identifier_distinction(tmp_path: Path) -> None:
    """NE-LIBRARY-LEGIBILITY: every shipped template that projects a human-name
    column declares it as ``label_var`` (the console leads with it and demotes
    the identifier); ``orders-directory`` projects no name column, so it declares
    NONE — the shipped exercise of the identifier-led degrade path, never a
    fabricated label. Asserted per-template BY VALUE (not merely presence), so
    swapping a label to the identifier column, or quietly dropping one, goes RED.

    The COLUMN ORDER is pinned too (owner-directed): where a name column exists
    and the machine rendering permits it, it LEADS the declared order —
    ``accounts-directory`` and the count aggregate lead with ``name``.
    ``tickets-by-status`` deliberately does NOT lead with ``subject`` (the
    free-text column must stay LAST for the ``" | "`` separator invariant; its
    display leads via ``label_var``), and ``orders-directory`` has no
    human-readable column to lead with."""
    from ne.brain_ontology.verified.templates import load_template_registry

    registry = load_template_registry(write_verified_templates(tmp_path / "vt"))
    roles = {
        t.template_id: (t.answer.columns, t.answer.citation_var, t.answer.label_var)
        for t in registry.templates
    }
    assert roles == {
        "accounts-directory": (("name", "account"), "account", "name"),
        "orders-directory": (("order", "account", "status"), "order", None),
        "orders-per-account-count": (("name", "account", "n_orders"), "account", "name"),
        "tickets-by-status": (
            ("ticket", "priority", "account", "account_name", "subject"),
            "ticket",
            "subject",
        ),
    }


# --- NE-GA-G6: the orders-directory verified template (grounded-agent demo) --


def test_orders_template_loads_and_passes_projection_guard(tmp_path: Path) -> None:
    """``ORDERS_DIRECTORY_TEMPLATE`` loads through the REAL U1 registry — the
    NE-GA-TG projection-hygiene guard runs at load, so a violation (an
    arg-as-column, or a column not bound by a real triple pattern) would raise
    ``TemplateRegistryError`` right here. Zero args, NO ``[routing]`` block (the
    routing table must stay ``len == 1`` — see
    ``test_routing_block_loads_and_routes_t3_question`` — orders is reached only
    via the grounded agent's own tool-calling, never deterministic T3 routing)."""
    from ne.brain_ontology.verified.routing import build_routing_table
    from ne.brain_ontology.verified.templates import load_template_registry

    registry = load_template_registry(write_verified_templates(tmp_path / "vt"))
    ids = {t.template_id for t in registry.templates}
    # NE-CTXB-8: the bundle now ships the server-side verified COUNT aggregate too;
    # NE-WORKFLOWS adds the ticket read the governed workflows are built on.
    assert ids == {
        "accounts-directory",
        "orders-directory",
        "orders-per-account-count",
        "tickets-by-status",
    }

    orders = next(t for t in registry.templates if t.template_id == "orders-directory")
    assert orders.args == {}
    assert orders.routing is None
    assert orders.tool_name == "read.kg.orders_directory"
    assert orders.answer.columns == ("order", "account", "status")
    assert orders.answer.citation_var == "order"
    assert orders.scope.graph_classes == ("all-employees",)

    # NE-CTXB-8: both directory templates opt into the trusted COUNT(*) → total_rows
    # rewrite (count_total_rows) that this unit's packing composition enables.
    accounts = next(t for t in registry.templates if t.template_id == "accounts-directory")
    assert accounts.answer.count_total_rows is True
    assert orders.answer.count_total_rows is True

    # The orders template ships NO [routing] — the routing table stays len == 1
    # (accounts-directory only), so the pre-existing T3 route/clarify/refuse
    # legs are structurally undisturbed by this addition.
    rows = build_routing_table(registry)
    assert len(rows) == 1
    assert rows[0].template_id == "accounts-directory"


def test_tickets_by_status_template_loads_and_keeps_free_text_last(
    tmp_path: Path,
) -> None:
    """``TICKETS_BY_STATUS_TEMPLATE`` loads through the REAL registry, and its column
    ORDER is pinned.

    The order is load-bearing, not cosmetic: the governed tool renders its answer as a
    ``" | "``-joined text table, so a value containing that separator shifts every
    column after it. ``subject`` is the one free-text column and it must stay LAST, so
    a reader splitting on a fixed separator count keeps the subject whole. This test is
    what stops a later "tidier" column order from silently corrupting every row whose
    subject contains a pipe.

    NO ``[routing]`` block — the routing table must stay ``len == 1`` (this template is
    reached by a workflow's tool call, never by prose routing)."""
    from ne.brain_ontology.verified.routing import build_routing_table
    from ne.brain_ontology.verified.templates import load_template_registry, render_query

    registry = load_template_registry(write_verified_templates(tmp_path / "vt"))
    tickets = next(t for t in registry.templates if t.template_id == "tickets-by-status")

    assert tickets.tool_name == "read.kg.tickets_by_status"
    assert tickets.answer.columns == ("ticket", "priority", "account", "account_name", "subject")
    assert tickets.answer.columns[-1] == "subject", "the free-text column must be projected LAST"
    assert tickets.answer.citation_var == "ticket"
    assert tickets.scope.graph_classes == ("all-employees",)
    assert set(tickets.args) == {"status"}
    assert tickets.routing is None

    # The status arg is BOUND as an RDF term in a VALUES block by trusted code — it is
    # never interpolated into the query text, so a hostile status cannot reshape it.
    rendered = render_query(tickets, {"status": 'open" } # '}, 500)
    assert "VALUES (?status)" in rendered
    assert '"open\\" } # "' in rendered

    assert len(build_routing_table(registry)) == 1


def test_tickets_by_status_template_advertises_the_status_vocabulary(
    tmp_path: Path,
) -> None:
    """NE-TEMPLATE-ENUM (SPEC §12 item 3): the demo ticket template declares the
    CLOSED ``TicketStatus`` vocabulary, advertises it in the generated tool schema
    the agent sees, and refuses the live-walkthrough guess ``"unresolved"`` loudly
    — with a message naming the field and the permitted values, never the guess —
    instead of executing it into silent zero rows."""
    import pytest
    from pydantic import ValidationError

    from ne.brain_ontology.verified.templates import (
        describe_invalid_args,
        load_template_registry,
    )
    from ne.schema import TicketStatus

    registry = load_template_registry(write_verified_templates(tmp_path / "vt"))
    tickets = next(t for t in registry.templates if t.template_id == "tickets-by-status")

    vocabulary = ("open", "pending", "resolved", "closed")
    # One vocabulary, three surfaces that must agree: the schema enum, the ArgDef,
    # and the platform TicketStatus the acme generator + workflows read.
    assert tickets.args["status"].enum == vocabulary
    assert tuple(s.value for s in TicketStatus) == vocabulary
    schema = tickets.args_json_schema()
    assert schema["properties"]["status"]["enum"] == list(vocabulary)  # type: ignore[index]

    with pytest.raises(ValidationError) as excinfo:
        tickets.args_model.model_validate({"status": "unresolved"})
    message = describe_invalid_args(tickets, excinfo.value)
    assert "'status' must be one of: open, pending, resolved, closed" in message
    assert "unresolved" not in message  # the rejected value is never echoed (C12)
    # CONTROL: every permitted value still validates — the enum refuses only
    # out-of-vocabulary values, not the vocabulary itself.
    for status in vocabulary:
        tickets.args_model.model_validate({"status": status})


def test_orders_per_account_count_template_loads(tmp_path: Path) -> None:
    """``ORDERS_PER_ACCOUNT_COUNT_TEMPLATE`` loads through the REAL registry with the
    kind='count' validators + GUARD-2 count relaxation + projection rewrite probe.
    The exact contract fields are pinned, NO [routing] block (the routing table
    stays len == 1 — the deterministic T3 routing is undisturbed by this add)."""
    from ne.brain_ontology.verified.routing import build_routing_table
    from ne.brain_ontology.verified.templates import load_template_registry

    registry = load_template_registry(write_verified_templates(tmp_path / "vt"))
    agg = next(t for t in registry.templates if t.template_id == "orders-per-account-count")
    assert agg.answer.kind == "count"
    assert agg.tool_name == "read.kg.orders_per_account_count"
    # Derived [*group_by, <as>] — name leads (NE-LIBRARY-LEGIBILITY, owner-directed).
    assert agg.answer.columns == ("name", "account", "n_orders")
    assert agg.answer.citation_var == "account"
    assert agg.answer.count_over == "order"
    assert agg.answer.count_total_rows is True
    assert agg.args == {}
    assert agg.routing is None
    assert agg.scope.graph_classes == ("all-employees",)

    # Deterministic T3 routing is undisturbed: the routing table is still exactly
    # accounts-directory (the aggregate + orders directory carry no [routing]).
    rows = build_routing_table(registry)
    assert len(rows) == 1
    assert rows[0].template_id == "accounts-directory"


# --- NE-GA-G7: marker parity with the production grounded-hallucination seam --


def test_grounded_hallucination_marker_matches_seam() -> None:
    """Grep-provable single-marker parity: ``G_REFUSE_PROMPT`` carries the EXACT
    literal ``ne.api.demo_seams.DEMO_GROUNDED_HALLUCINATION_MARKER`` the production
    ``DemoGroundedHallucinationGateway`` seam keys off — proven without adding a
    src-level ne-cli -> ne-api import to ``corpus.py`` itself."""
    from ne.api.demo_seams import DEMO_GROUNDED_HALLUCINATION_MARKER
    from ne.cli.demo.corpus import G_REFUSE_PROMPT

    assert G_REFUSE_PROMPT.startswith(DEMO_GROUNDED_HALLUCINATION_MARKER)
