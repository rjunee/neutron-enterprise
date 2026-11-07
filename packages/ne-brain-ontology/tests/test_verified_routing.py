# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-VAF-U1: RoutingSpec load validation + the pure deterministic matcher.

Two halves: (1) matcher-only tests build :class:`RoutingRow` literals directly
(pure functions — no registry, no I/O, no TOML); (2) load-validation tests
reuse the ``test_verified_templates.py`` ``_toml()``/``TemplateRegistryError``
pattern with a local, routing-flavored builder — never touching the shared
``tests/data/verified_templates`` fixtures (NE-VAF-U5's job).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ne.brain_ontology import load_template_registry
from ne.brain_ontology.verified.errors import TemplateRegistryError
from ne.brain_ontology.verified.routing import (
    RouteOutcome,
    RoutingRow,
    build_routing_table,
    match_selection,
    match_template,
    normalize,
)

_ACCOUNT = "<https://neutron.enterprise/schema/operational/Account>"
_NAME = "<https://neutron.enterprise/schema/operational/name>"

# ---------------------------------------------------------------------------
# Matcher-only fixtures (pure RoutingRow literals — no registry involved).
# ---------------------------------------------------------------------------

ROW_ACCOUNTS = RoutingRow(
    domain="accounts-directory",
    template_id="accounts-directory",
    tool_name="read.kg.accounts_directory",
    description="d",
    triggers=("list every account i am cleared to see", "list the accounts i can see"),
    guard_terms=("accounts directory", "cleared to see"),
    clarify_max=2,
)

ROW_INVOICES = RoutingRow(
    domain="invoices",
    template_id="unpaid-invoices",
    tool_name="read.kg.unpaid_invoices",
    description="d",
    triggers=("list every unpaid invoice",),
    guard_terms=("unpaid invoices",),
    clarify_max=2,
)

_ROWS = (ROW_ACCOUNTS, ROW_INVOICES)


# ---------------------------------------------------------------------------
# match_template
# ---------------------------------------------------------------------------


def test_trigger_exact_match_routes() -> None:
    decision = match_template("List every account I am cleared to see.", _ROWS)
    assert decision.outcome == RouteOutcome.ROUTE
    assert decision.template_id == "accounts-directory"
    assert decision.domain == "accounts-directory"


def test_trigger_in_order_subsequence_routes() -> None:
    decision = match_template(
        "please list every single account that i am now cleared to see today", _ROWS
    )
    assert decision.outcome == RouteOutcome.ROUTE
    assert decision.template_id == "accounts-directory"


def test_out_of_order_trigger_tokens_do_not_route() -> None:
    decision = match_template("i am cleared to see every account list", _ROWS)
    assert decision.outcome == RouteOutcome.CLARIFY
    assert decision.template_id is None


def test_guard_hit_without_trigger_clarifies() -> None:
    decision = match_template("what is in the accounts directory", _ROWS)
    assert decision.outcome == RouteOutcome.CLARIFY
    assert decision.domain == "accounts-directory"
    assert decision.domains == ("accounts-directory",)


def test_negation_before_guard_passes() -> None:
    decision = match_template(
        "don't list the accounts, tell me what the accounts directory is for", _ROWS
    )
    assert decision.outcome == RouteOutcome.PASS


def test_negation_before_trigger_does_not_route() -> None:
    decision = match_template("never list every account i am cleared to see", _ROWS)
    assert decision.outcome == RouteOutcome.PASS


def test_compound_and_verb_clarifies_not_routes() -> None:
    decision = match_template("list every account i am cleared to see and email it to bob", _ROWS)
    assert decision.outcome == RouteOutcome.CLARIFY
    assert decision.outcome != RouteOutcome.ROUTE


def test_compound_then_clarifies_not_routes() -> None:
    decision = match_template("list every account i am cleared to see then delete them", _ROWS)
    assert decision.outcome == RouteOutcome.CLARIFY


def test_two_domain_guard_hits_clarify_lists_both_sorted() -> None:
    decision = match_template("how do the accounts directory relate to the unpaid invoices", _ROWS)
    assert decision.outcome == RouteOutcome.CLARIFY
    assert decision.domains == ("accounts-directory", "invoices")
    assert decision.domain == "accounts-directory"


def test_non_domain_prompt_passes() -> None:
    decision = match_template("what's the weather in paris", _ROWS)
    assert decision.outcome == RouteOutcome.PASS


@pytest.mark.parametrize("prompt", ["", "   !!!"])
def test_empty_prompt_passes(prompt: str) -> None:
    decision = match_template(prompt, _ROWS)
    assert decision.outcome == RouteOutcome.PASS


@pytest.mark.parametrize(
    "prompt",
    [
        "List every account I am cleared to see.",
        "how do the accounts directory relate to the unpaid invoices",
        "what's the weather in paris",
    ],
)
def test_match_template_row_order_invariant(prompt: str) -> None:
    forward = match_template(prompt, _ROWS)
    reversed_rows = tuple(reversed(_ROWS))
    backward = match_template(prompt, reversed_rows)
    assert forward == backward


# ---------------------------------------------------------------------------
# match_selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("1", "accounts-directory"),
        ("(1)", "accounts-directory"),
        ("option 1", "accounts-directory"),
        ("#1", "accounts-directory"),
        ("a", "accounts-directory"),
        ("(a)", "accounts-directory"),
        ("2", "unpaid-invoices"),
        ("b", "unpaid-invoices"),
        ("list every unpaid invoice", "unpaid-invoices"),
    ],
)
def test_selection_accepts_ordinal_letter_and_echo(reply: str, expected: str) -> None:
    # domain_options sorted by template_id: "accounts-directory" < "unpaid-invoices".
    assert match_selection(reply, _ROWS) == expected


@pytest.mark.parametrize(
    "reply",
    ["yes please", "", "option", "9", "z", "the first one"],
)
def test_selection_rejects_garbage_and_out_of_range(reply: str) -> None:
    assert match_selection(reply, _ROWS) is None


def test_selection_ambiguous_echo_returns_none() -> None:
    option_a = RoutingRow(
        domain="accounts-directory",
        template_id="show-accounts",
        tool_name="read.kg.show_accounts",
        description="d",
        triggers=("show all accounts",),
        guard_terms=("accounts directory",),
        clarify_max=2,
    )
    option_b = RoutingRow(
        domain="accounts-directory",
        template_id="show-accounts-today",
        tool_name="read.kg.show_accounts_today",
        description="d",
        triggers=("show all accounts today",),
        guard_terms=("accounts directory",),
        clarify_max=2,
    )
    assert match_selection("show all accounts today", (option_a, option_b)) is None


def test_selection_negated_echo_returns_none() -> None:
    # A clarify-turn reply that negates the echoed canonical trigger must not
    # resolve to that option — the same negation veto match_template applies
    # to trigger spans applies to the selection-grammar echo path too.
    assert match_selection("don't list every unpaid invoice", _ROWS) is None


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "List every account",
        "ＬＩＳＴ  every account!!!",  # noqa: RUF001 - full-width "LIST", NFKC-folds to ascii
        "list\tEVERY   account",
        "  list, every; account.  ",
    ],
)
def test_normalize_deterministic_across_variants(text: str) -> None:
    expected = ("list", "every", "account")
    assert normalize(text) == expected
    assert normalize(text) == normalize(text)  # repeated calls agree


def test_normalize_apostrophe_deleted() -> None:
    assert normalize("don't") == ("dont",)
    assert normalize("don’t") == ("dont",)  # noqa: RUF001 - curly apostrophe


def test_normalize_nfkc_ligature() -> None:
    assert normalize("ﬁle") == ("file",)


# ---------------------------------------------------------------------------
# Load validation: RoutingSpec on QueryTemplate / load_template_registry.
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, toml: str, name: str = "t.toml") -> Path:
    (tmp_path / name).write_text(toml, encoding="utf-8")
    return tmp_path


def _toml(
    *,
    template_id: str = "ok-tpl",
    version: int = 1,
    sparql: str | None = None,
    args_block: str = "",
    columns: str = '["account", "name"]',
    citation_var: str = "account",
    routing_block: str = "",
) -> str:
    if sparql is None:
        sparql = (
            "\nSELECT ?account ?name WHERE {\n"
            "  #BINDINGS#\n"
            f"  ?account a {_ACCOUNT} .\n"
            f"  ?account {_NAME} ?name .\n"
            "}\n"
        )
    return (
        f'template_id = "{template_id}"\n'
        f"version = {version}\n"
        'description = "d"\n'
        f'sparql = """{sparql}"""\n'
        f"{args_block}"
        "[scope]\n"
        'graph_classes = ["alpha"]\n'
        'result_shape = "enumerable"\n'
        "[answer]\n"
        f"columns = {columns}\n"
        f'citation_var = "{citation_var}"\n'
        f"{routing_block}"
    )


_ROUTING_BLOCK = (
    "[routing]\n"
    'domain = "accounts-directory"\n'
    'triggers = ["list every account i am cleared to see"]\n'
    'guard_terms = ["accounts directory", "cleared to see"]\n'
    "clarify_max = 2\n"
)


def test_routing_block_loads(tmp_path: Path) -> None:
    _write(tmp_path, _toml(template_id="accounts-directory", routing_block=_ROUTING_BLOCK))
    reg = load_template_registry(tmp_path)
    (template,) = reg.templates
    assert template.routing is not None
    assert template.routing.domain == "accounts-directory"
    assert template.routing.triggers == ("list every account i am cleared to see",)
    assert template.routing.guard_terms == ("accounts directory", "cleared to see")
    assert template.routing.clarify_max == 2


def test_routing_absent_is_back_compat(tmp_path: Path) -> None:
    _write(tmp_path, _toml())
    reg = load_template_registry(tmp_path)
    (template,) = reg.templates
    assert template.routing is None


def test_load_fails_routing_with_args(tmp_path: Path) -> None:
    args_block = '[args.q_name]\ntype = "str"\npattern = "^[A-Za-z0-9_ ]{1,64}$"\n'
    sparql = (
        "\nSELECT ?account ?name WHERE {\n"
        "  #BINDINGS#\n"
        f"  ?account a {_ACCOUNT} .\n"
        f"  ?account {_NAME} ?name .\n"
        "  FILTER(?name = ?q_name)\n"
        "}\n"
    )
    _write(
        tmp_path,
        _toml(args_block=args_block, sparql=sparql, routing_block=_ROUTING_BLOCK),
    )
    with pytest.raises(TemplateRegistryError, match="zero-arg"):
        load_template_registry(tmp_path)


def test_load_fails_single_token_guard_term(tmp_path: Path) -> None:
    routing_block = (
        "[routing]\n"
        'domain = "accounts-directory"\n'
        'triggers = ["list every account i am cleared to see"]\n'
        'guard_terms = ["account"]\n'
    )
    _write(tmp_path, _toml(routing_block=routing_block))
    with pytest.raises(TemplateRegistryError, match="multi-word"):
        load_template_registry(tmp_path)


@pytest.mark.parametrize(
    "routing_block",
    [
        (
            "[routing]\n"
            'domain = "accounts-directory"\n'
            "triggers = []\n"
            'guard_terms = ["accounts directory"]\n'
        ),
        (
            "[routing]\n"
            'domain = "accounts-directory"\n'
            'triggers = ["list every account i am cleared to see"]\n'
            "guard_terms = []\n"
        ),
    ],
)
def test_load_fails_empty_triggers_or_guard_terms(tmp_path: Path, routing_block: str) -> None:
    _write(tmp_path, _toml(routing_block=routing_block))
    with pytest.raises(TemplateRegistryError):
        load_template_registry(tmp_path)


def test_load_fails_trigger_normalizing_to_empty(tmp_path: Path) -> None:
    routing_block = (
        "[routing]\n"
        'domain = "accounts-directory"\n'
        'triggers = ["!!!"]\n'
        'guard_terms = ["accounts directory"]\n'
    )
    _write(tmp_path, _toml(routing_block=routing_block))
    with pytest.raises(TemplateRegistryError, match="zero tokens"):
        load_template_registry(tmp_path)


def test_load_fails_unknown_key_in_routing(tmp_path: Path) -> None:
    routing_block = (
        "[routing]\n"
        'domain = "accounts-directory"\n'
        'triggers = ["list every account i am cleared to see"]\n'
        'guard_terms = ["accounts directory"]\n'
        "foo = 1\n"
    )
    _write(tmp_path, _toml(routing_block=routing_block))
    with pytest.raises(TemplateRegistryError):
        load_template_registry(tmp_path)


def test_routing_block_moves_registry_hash_and_absence_is_byte_identical(tmp_path: Path) -> None:
    original = _toml(template_id="t1")
    _write(tmp_path, original, name="t1.toml")
    hash_without = load_template_registry(tmp_path).fingerprint.registry_hash

    (tmp_path / "t1.toml").write_text(
        _toml(template_id="t1", routing_block=_ROUTING_BLOCK), encoding="utf-8"
    )
    hash_with = load_template_registry(tmp_path).fingerprint.registry_hash
    assert hash_with != hash_without

    (tmp_path / "t1.toml").write_text(original, encoding="utf-8")
    hash_restored = load_template_registry(tmp_path).fingerprint.registry_hash
    assert hash_restored == hash_without


def test_projection_strings_only_frozen_sorted(tmp_path: Path) -> None:
    routing_block_b = (
        "[routing]\n"
        'domain = "invoices"\n'
        'triggers = ["list every unpaid invoice"]\n'
        'guard_terms = ["unpaid invoices"]\n'
    )
    _write(tmp_path, _toml(template_id="b-routed", routing_block=routing_block_b), name="b.toml")
    _write(
        tmp_path,
        _toml(template_id="a-routed", routing_block=_ROUTING_BLOCK),
        name="a.toml",
    )
    _write(tmp_path, _toml(template_id="c-unrouted"), name="c.toml")

    reg = load_template_registry(tmp_path)
    rows = build_routing_table(reg)

    assert [row.template_id for row in rows] == ["a-routed", "b-routed"]
    for row in rows:
        assert isinstance(row.domain, str)
        assert isinstance(row.template_id, str)
        assert isinstance(row.tool_name, str)
        assert isinstance(row.description, str)
        assert isinstance(row.triggers, tuple)
        assert all(isinstance(t, str) for t in row.triggers)
        assert isinstance(row.guard_terms, tuple)
        assert all(isinstance(g, str) for g in row.guard_terms)
        assert isinstance(row.clarify_max, int)
        assert row.tool_name == f"read.kg.{row.template_id.replace('-', '_')}"

    with pytest.raises(dataclasses.FrozenInstanceError):
        rows[0].domain = "changed"  # type: ignore[misc]
