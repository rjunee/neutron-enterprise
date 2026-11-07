# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-7 Layer-3 COUNT aggregates — LOAD + RENDER matrix.

The soundness re-derivation (design §5.4), in one sentence a reviewer can check:
a ``COUNT(DISTINCT ?count_over)`` over a TRIPLE-BOUND ``count_over`` grouped by
TRIPLE-BOUND keys, on ONE conjunctive path (UNION/MINUS/EXISTS/subquery banned in
the body), cannot FABRICATE a cell — every group key and every counted binding was
matched against the C7-trimmed graph, so the only trusted-computed scalar is the
tally itself. Three fabrication routes are proven DEAD by
``test_count_soundness_rederivation`` (a BIND-fabricated ``count_over``, a UNION arm,
and a nested subquery are each refused at load). The hard-coupling of the relaxation
to the UNION/MINUS/EXISTS bans is proven by
``test_count_relaxation_hard_coupled_to_bans``.
"""

from __future__ import annotations

import pytest
from rdflib.plugins.sparql import prepareQuery

from ne.brain_ontology.verified import templates
from ne.brain_ontology.verified.errors import TemplateRegistryError
from ne.brain_ontology.verified.templates import (
    QueryTemplate,
    _projected_vars,
    load_template_registry,
    parse_template_toml,
    render_count_query,
    render_query,
)

from .verified_helpers import COUNT_DIR, FIXTURES_DIR

_SCHEMA = "https://neutron.enterprise/schema/operational"

# The canonical, VALID count body: a plain single-path conjunctive SELECT projecting
# exactly the group_by keys (?account ?name) + count_over (?order).
_STD_BODY = (
    "SELECT ?account ?name ?order WHERE {\n"
    "  #BINDINGS#\n"
    f"  ?account a <{_SCHEMA}/Account> .\n"
    f"  ?account <{_SCHEMA}/name> ?name .\n"
    f"  ?order a <{_SCHEMA}/Order> .\n"
    f"  ?order <{_SCHEMA}/account> ?account .\n"
    "}"
)

_STD_ANSWER = (
    'kind = "count"\n'
    'group_by = ["account", "name"]\n'
    'count_over = "order"\n'
    'as = "n_orders"\n'
    'citation_var = "account"'
)


def _toml(body: str, answer: str) -> str:
    return (
        'template_id = "x-count"\n'
        "version = 1\n"
        'description = "d"\n'
        'sparql = """\n'
        f"{body}\n"
        '"""\n\n'
        "[scope]\n"
        'graph_classes = ["alpha", "beta"]\n'
        'result_shape = "enumerable"\n\n'
        "[answer]\n"
        f"{answer}\n"
    )


# --------------------------------------------------------------------------- load


def test_count_fixture_loads_with_derived_columns() -> None:
    reg = load_template_registry(COUNT_DIR)
    by = {t.template_id: t for t in reg.templates}
    tpl = by["orders-per-account-count"]
    assert tpl.answer.kind == "count"
    # Columns are DERIVED as [*group_by, <as>] — never author-declared.
    assert tpl.answer.columns == ("account", "name", "n_orders")
    assert tpl.answer.group_by == ("account", "name")
    assert tpl.answer.count_over == "order"
    assert tpl.answer.count_as == "n_orders"
    assert tpl.answer.count_total_rows is True
    assert tpl.tool_name == "read.kg.orders_per_account_count"
    # Steward propose/apply seam parity: the SAME TOML validates through
    # parse_template_toml (the shared load gate), not just the boot loader.
    raw = (COUNT_DIR / "orders_per_account_count.toml").read_text(encoding="utf-8")
    parsed = parse_template_toml(raw)
    assert parsed.answer.columns == ("account", "name", "n_orders")


_REFUSALS: list[tuple[str, str, str, str]] = [
    # (id, body, answer_block, match-substring)
    (
        "count_over_bind_bound",
        (
            "SELECT ?account ?name ?order WHERE {\n"
            "  #BINDINGS#\n"
            f"  ?account a <{_SCHEMA}/Account> .\n"
            f"  ?account <{_SCHEMA}/name> ?name .\n"
            "  BIND(<urn:ne:entity:x> AS ?order)\n"
            "}"
        ),
        _STD_ANSWER,
        "count over non-triple-bound var",
    ),
    (
        "group_by_bind_bound",
        (
            "SELECT ?account ?name ?order WHERE {\n"
            "  #BINDINGS#\n"
            f"  ?account a <{_SCHEMA}/Account> .\n"
            '  BIND("Nom" AS ?name)\n'
            f"  ?order a <{_SCHEMA}/Order> .\n"
            f"  ?order <{_SCHEMA}/account> ?account .\n"
            "}"
        ),
        _STD_ANSWER,
        "non-triple-bound group_by",
    ),
    (
        "citation_not_in_group_by",
        _STD_BODY,
        (
            'kind = "count"\n'
            'group_by = ["account", "name"]\n'
            'count_over = "order"\n'
            'as = "n_orders"\n'
            'citation_var = "n_orders"'
        ),
        "must be one of the group_by keys",
    ),
    (
        "explicit_columns",
        _STD_BODY,
        (
            'kind = "count"\n'
            'columns = ["account", "name", "n_orders"]\n'
            'group_by = ["account", "name"]\n'
            'count_over = "order"\n'
            'as = "n_orders"\n'
            'citation_var = "account"'
        ),
        "columns must NOT be declared",
    ),
    (
        "empty_group_by",
        _STD_BODY,
        (
            'kind = "count"\n'
            "group_by = []\n"
            'count_over = "order"\n'
            'as = "n_orders"\n'
            'citation_var = "account"'
        ),
        "non-empty group_by",
    ),
    (
        "duplicate_group_by",
        _STD_BODY,
        (
            'kind = "count"\n'
            'group_by = ["account", "account"]\n'
            'count_over = "order"\n'
            'as = "n_orders"\n'
            'citation_var = "account"'
        ),
        "duplicate keys",
    ),
    (
        "missing_count_over",
        _STD_BODY,
        (
            'kind = "count"\n'
            'group_by = ["account", "name"]\n'
            'as = "n_orders"\n'
            'citation_var = "account"'
        ),
        "requires count_over",
    ),
    (
        "missing_as",
        _STD_BODY,
        (
            'kind = "count"\n'
            'group_by = ["account", "name"]\n'
            'count_over = "order"\n'
            'citation_var = "account"'
        ),
        "requires as",
    ),
    (
        "count_as_in_group_by",
        _STD_BODY,
        (
            'kind = "count"\n'
            'group_by = ["account", "name"]\n'
            'count_over = "order"\n'
            'as = "name"\n'
            'citation_var = "account"'
        ),
        "collides with a group_by key",
    ),
    (
        "count_as_eq_count_over",
        _STD_BODY,
        (
            'kind = "count"\n'
            'group_by = ["account", "name"]\n'
            'count_over = "order"\n'
            'as = "order"\n'
            'citation_var = "account"'
        ),
        "must differ from count_over",
    ),
    (
        "count_as_alias_collision_in_body",
        (
            "SELECT ?account ?name ?order WHERE {\n"
            "  #BINDINGS#\n"
            f"  ?account a <{_SCHEMA}/Account> .\n"
            f"  ?account <{_SCHEMA}/name> ?name .\n"
            f"  ?order a <{_SCHEMA}/Order> .\n"
            f"  ?order <{_SCHEMA}/account> ?account .\n"
            f"  ?order <{_SCHEMA}/order_number> ?n_orders .\n"
            "}"
        ),
        _STD_ANSWER,
        "already appears as a variable",
    ),
    (
        "count_over_listed_in_group_by",
        _STD_BODY,
        (
            'kind = "count"\n'
            'group_by = ["account", "name", "order"]\n'
            'count_over = "order"\n'
            'as = "n_orders"\n'
            'citation_var = "account"'
        ),
        "degenerate",
    ),
    (
        "aggregate_keys_on_rows",
        "SELECT ?account ?name WHERE {\n"
        "  #BINDINGS#\n"
        f"  ?account a <{_SCHEMA}/Account> .\n"
        f"  ?account <{_SCHEMA}/name> ?name .\n"
        "}",
        ('columns = ["account", "name"]\ngroup_by = ["account"]\ncitation_var = "account"'),
        "only valid on a kind='count'",
    ),
    (
        "body_lacks_literal_where",
        (
            "SELECT ?account ?name ?order {\n"
            "  #BINDINGS#\n"
            f"  ?account a <{_SCHEMA}/Account> .\n"
            f"  ?account <{_SCHEMA}/name> ?name .\n"
            f"  ?order a <{_SCHEMA}/Order> .\n"
            f"  ?order <{_SCHEMA}/account> ?account .\n"
            "}"
        ),
        _STD_ANSWER,
        "literal WHERE",
    ),
    (
        "ne_total_reserved_in_body",
        (
            "SELECT ?account ?name ?order WHERE {\n"
            "  #BINDINGS#\n"
            f"  ?account a <{_SCHEMA}/Account> .\n"
            f"  ?account <{_SCHEMA}/name> ?name .\n"
            f"  ?account <{_SCHEMA}/marker> ?ne_total .\n"
            f"  ?order a <{_SCHEMA}/Order> .\n"
            f"  ?order <{_SCHEMA}/account> ?account .\n"
            "}"
        ),
        _STD_ANSWER + "\ncount_total_rows = true",
        "uses the reserved variable",
    ),
]


_REFUSAL_IDS = [c[0] for c in _REFUSALS]


@pytest.mark.parametrize(("case_id", "body", "answer", "match"), _REFUSALS, ids=_REFUSAL_IDS)
def test_count_validator_refusal_matrix(case_id: str, body: str, answer: str, match: str) -> None:
    with pytest.raises(TemplateRegistryError, match=match):
        parse_template_toml(_toml(body, answer))


def test_count_body_keyword_bans_still_fire() -> None:
    # The relaxation did NOT widen the body language: every existing ban fires on a
    # count body exactly as it does on a rows body.
    union = (
        "SELECT ?account ?name ?order WHERE {\n"
        "  #BINDINGS#\n"
        f"  {{ ?account a <{_SCHEMA}/Account> }} UNION {{ ?account a <{_SCHEMA}/Org> }}\n"
        f"  ?account <{_SCHEMA}/name> ?name .\n"
        f"  ?order a <{_SCHEMA}/Order> .\n"
        f"  ?order <{_SCHEMA}/account> ?account .\n"
        "}"
    )
    with pytest.raises(TemplateRegistryError, match="UNION"):
        parse_template_toml(_toml(union, _STD_ANSWER))

    minus = (
        "SELECT ?account ?name ?order WHERE {\n"
        "  #BINDINGS#\n"
        f"  ?account a <{_SCHEMA}/Account> .\n"
        f"  ?account <{_SCHEMA}/name> ?name .\n"
        f"  ?order a <{_SCHEMA}/Order> .\n"
        f"  ?order <{_SCHEMA}/account> ?account .\n"
        f"  MINUS {{ ?account a <{_SCHEMA}/Hidden> }}\n"
        "}"
    )
    with pytest.raises(TemplateRegistryError, match="MINUS"):
        parse_template_toml(_toml(minus, _STD_ANSWER))

    exists = (
        "SELECT ?account ?name ?order WHERE {\n"
        "  #BINDINGS#\n"
        f"  ?account a <{_SCHEMA}/Account> .\n"
        f"  ?account <{_SCHEMA}/name> ?name .\n"
        f"  ?order a <{_SCHEMA}/Order> .\n"
        f"  ?order <{_SCHEMA}/account> ?account .\n"
        f"  FILTER EXISTS {{ ?account <{_SCHEMA}/flag> ?f }}\n"
        "}"
    )
    with pytest.raises(TemplateRegistryError, match="EXISTS"):
        parse_template_toml(_toml(exists, _STD_ANSWER))

    subquery = (
        "SELECT ?account ?name ?order WHERE {\n"
        "  #BINDINGS#\n"
        f"  ?account a <{_SCHEMA}/Account> .\n"
        f"  ?account <{_SCHEMA}/name> ?name .\n"
        f"  {{ SELECT ?order WHERE {{ ?order a <{_SCHEMA}/Order> }} }}\n"
        f"  ?order <{_SCHEMA}/account> ?account .\n"
        "}"
    )
    with pytest.raises(TemplateRegistryError, match="nested subquery"):
        parse_template_toml(_toml(subquery, _STD_ANSWER))


def test_count_relaxation_hard_coupled_to_bans(monkeypatch: pytest.MonkeyPatch) -> None:
    # GUARDIAN (DoD): weaken _FORBIDDEN_KEYWORDS by removing UNION and the count path
    # REFUSES to load — the relaxation's soundness precondition (a single conjunctive
    # path, _triple_pattern_vars) can never be silently disarmed. This IS the
    # re-derivation anchor of that precondition for the relaxed count column.
    weakened = frozenset(templates._FORBIDDEN_KEYWORDS - {"UNION"})
    monkeypatch.setattr(templates, "_FORBIDDEN_KEYWORDS", weakened)
    with pytest.raises(TemplateRegistryError, match="precondition is violated"):
        parse_template_toml(_toml(_STD_BODY, _STD_ANSWER))
    # The coupling is count-SPECIFIC: a rows template still loads with UNION removed
    # from the ban set (its projection hygiene never relied on the relaxation).
    rows_toml = (FIXTURES_DIR / "accounts_directory.toml").read_text(encoding="utf-8")
    assert parse_template_toml(rows_toml).answer.kind == "rows"


def test_count_soundness_rederivation() -> None:
    # The three fabrication routes the design §5.4 argument rules out are each DEAD:
    bind_over = (
        "SELECT ?account ?name ?order WHERE {\n"
        "  #BINDINGS#\n"
        f"  ?account a <{_SCHEMA}/Account> .\n"
        f"  ?account <{_SCHEMA}/name> ?name .\n"
        "  BIND(<urn:ne:entity:x> AS ?order)\n"
        "}"
    )
    with pytest.raises(TemplateRegistryError, match="non-triple-bound"):
        parse_template_toml(_toml(bind_over, _STD_ANSWER))
    union = (
        "SELECT ?account ?name ?order WHERE {\n"
        "  #BINDINGS#\n"
        f"  {{ ?account a <{_SCHEMA}/Account> }} UNION {{ ?order a <{_SCHEMA}/Order> }}\n"
        f"  ?account <{_SCHEMA}/name> ?name .\n"
        f"  ?order <{_SCHEMA}/account> ?account .\n"
        "}"
    )
    with pytest.raises(TemplateRegistryError, match="UNION"):
        parse_template_toml(_toml(union, _STD_ANSWER))
    subquery = (
        "SELECT ?account ?name ?order WHERE {\n"
        "  #BINDINGS#\n"
        f"  ?account a <{_SCHEMA}/Account> .\n"
        f"  ?account <{_SCHEMA}/name> ?name .\n"
        f"  {{ SELECT ?order WHERE {{ ?order a <{_SCHEMA}/Order> }} }}\n"
        f"  ?order <{_SCHEMA}/account> ?account .\n"
        "}"
    )
    with pytest.raises(TemplateRegistryError, match="nested subquery"):
        parse_template_toml(_toml(subquery, _STD_ANSWER))


# ------------------------------------------------------------------------- render


_COUNT_GOLDEN = (
    "SELECT ?account ?name (COUNT(DISTINCT ?order) AS ?n_orders) WHERE {\n"
    "\n"
    "  ?account a <https://neutron.enterprise/schema/operational/Account> .\n"
    "  ?account <https://neutron.enterprise/schema/operational/name> ?name .\n"
    "  ?order a <https://neutron.enterprise/schema/operational/Order> .\n"
    "  ?order <https://neutron.enterprise/schema/operational/account> ?account .\n"
    "}\n"
    "GROUP BY ?account ?name\n"
    "ORDER BY ?account ?name ?n_orders\n"
    "LIMIT 501"
)


def _count_fixture() -> QueryTemplate:
    reg = load_template_registry(COUNT_DIR)
    return {t.template_id: t for t in reg.templates}["orders-per-account-count"]


def test_count_rendered_query_golden_and_projection() -> None:
    tpl = _count_fixture()
    rendered = render_query(tpl, {}, 500)
    assert "SELECT ?account ?name (COUNT(DISTINCT ?order) AS ?n_orders)" in rendered
    assert "\nGROUP BY ?account ?name" in rendered
    order_idx = rendered.index("ORDER BY ?account ?name ?n_orders")
    limit_idx = rendered.index("LIMIT 501")
    assert order_idx < limit_idx  # ORDER BY precedes LIMIT
    assert rendered == _COUNT_GOLDEN  # exact frozen golden
    # DoD: the rendered aggregate re-parses AND projects ONLY the declared columns.
    prepared = prepareQuery(rendered)
    assert _projected_vars(prepared) == {"account", "name", "n_orders"}


def test_count_template_with_arg_binds_values() -> None:
    body = (
        "SELECT ?account ?name ?order WHERE {\n"
        "  #BINDINGS#\n"
        f"  ?account a <{_SCHEMA}/Account> .\n"
        f"  ?account <{_SCHEMA}/name> ?name .\n"
        f"  ?order a <{_SCHEMA}/Order> .\n"
        f"  ?order <{_SCHEMA}/account> ?account .\n"
        f"  ?order <{_SCHEMA}/status_ref> ?status_iri .\n"
        "}"
    )
    toml = (
        'template_id = "x-count-arg"\n'
        "version = 1\n"
        'description = "d"\n'
        'sparql = """\n'
        f"{body}\n"
        '"""\n\n'
        "[args.status_iri]\n"
        'type = "iri"\n\n'
        "[scope]\n"
        'graph_classes = ["alpha", "beta"]\n'
        'result_shape = "enumerable"\n\n'
        "[answer]\n"
        f"{_STD_ANSWER}\n"
    )
    tpl = parse_template_toml(toml)
    rendered = render_query(tpl, {"status_iri": "urn:ne:entity:st"}, 500)
    assert "VALUES (?status_iri) { (<urn:ne:entity:st>) }" in rendered
    assert "GROUP BY ?account ?name" in rendered
    prepareQuery(rendered)  # VALUES + aggregate interaction re-parses (fail closed)


def test_render_count_query_goldens() -> None:
    reg = load_template_registry(COUNT_DIR)
    by = {t.template_id: t for t in reg.templates}
    rows_total = render_count_query(by["accounts-directory-total"], {})
    assert rows_total == (
        "SELECT (COUNT(*) AS ?ne_total) WHERE {\n"
        "\n"
        "  ?account a <https://neutron.enterprise/schema/operational/Account> .\n"
        "  ?account <https://neutron.enterprise/schema/operational/name> ?name .\n"
        "}"
    )
    assert "ORDER BY" not in rows_total and "LIMIT" not in rows_total
    count_total = render_count_query(by["orders-per-account-count"], {})
    assert count_total.startswith(
        "SELECT (COUNT(*) AS ?ne_total) WHERE { { SELECT DISTINCT ?account ?name WHERE {"
    )
    prepareQuery(rows_total)
    prepareQuery(count_total)


def test_rows_kind_render_byte_identical_goldens() -> None:
    # DoD back-compat: the three pre-change rendered-query strings are byte-identical.
    reg = load_template_registry(FIXTURES_DIR)
    by = {t.template_id: t for t in reg.templates}
    assert render_query(by["accounts-directory"], {}, 500) == (
        "SELECT ?account ?name WHERE {\n"
        "\n"
        "  ?account a <https://neutron.enterprise/schema/operational/Account> .\n"
        "  ?account <https://neutron.enterprise/schema/operational/name> ?name .\n"
        "}\n"
        "ORDER BY ?account ?name\n"
        "LIMIT 501"
    )
    assert render_query(by["accounts-by-name"], {"q_name": "Acme Corp"}, 500) == (
        "SELECT ?account ?name WHERE {\n"
        'VALUES (?q_name) { ("Acme Corp") }\n'
        "  ?account a <https://neutron.enterprise/schema/operational/Account> .\n"
        "  ?account <https://neutron.enterprise/schema/operational/name> ?name .\n"
        "  FILTER(?name = ?q_name)\n"
        "}\n"
        "ORDER BY ?account ?name\n"
        "LIMIT 501"
    )
    assert render_query(by["entity-by-iri"], {"account": "urn:ne:entity:acc_a"}, 500) == (
        "SELECT ?account ?name WHERE {\n"
        "VALUES (?account) { (<urn:ne:entity:acc_a>) }\n"
        "  ?account <https://neutron.enterprise/schema/operational/name> ?name .\n"
        "}\n"
        "ORDER BY ?account ?name\n"
        "LIMIT 501"
    )
