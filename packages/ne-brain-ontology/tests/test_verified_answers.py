# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085c: the VerifiedAnswer artifact — grounded rows, T2 citations, truncation, replay."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ne.brain_ontology import load_template_registry
from ne.brain_ontology.lift.graph_to_rdf import entity_iri
from ne.brain_ontology.verified import answers_equal, execute_template, render_answer_text
from ne.brain_ontology.verified.answers import VerifiedAnswer
from ne.contracts.tokens import ByteTokenEstimator

from .verified_helpers import (
    FIXTURES_DIR,
    StubExpander,
    account,
    make_gateway,
    principal,
    seed_accounts,
)

_TPL = {t.template_id: t for t in load_template_registry(FIXTURES_DIR).templates}


def _answer(**overrides: object) -> VerifiedAnswer:
    """Construct a VerifiedAnswer with sane envelope defaults, for validator tests."""
    base: dict[str, object] = {
        "template_id": "t",
        "template_version": 1,
        "args": {},
        "columns": ("account", "name"),
        "rows": ((entity_iri("acc_a"), "acc_a Corp"),),
        "citations": (entity_iri("acc_a"),),
        "truncated": False,
        "row_limit": 500,
        "returned_rows": 1,
        "total_rows": 1,
        "truncation_reason": None,
        "config_hash": "a" * 64,
        "ontology_version": "b" * 64,
        "executed_at": datetime.now(UTC),
    }
    base.update(overrides)
    return VerifiedAnswer(**base)  # type: ignore[arg-type]


def test_envelope_returned_rows_pinned_to_rows() -> None:
    # returned_rows must equal len(rows) — a validator, not a convention.
    _answer()  # the consistent baseline constructs fine
    with pytest.raises(ValueError, match="returned_rows"):
        _answer(returned_rows=2)
    # citations are packed in lockstep with rows.
    with pytest.raises(ValueError, match="citations"):
        _answer(citations=(entity_iri("acc_a"), entity_iri("acc_b")))


async def _directory(gw: object, p: object, **kw: object) -> VerifiedAnswer:
    return await execute_template(
        _TPL["accounts-directory"],
        {},
        store=gw,  # type: ignore[arg-type]
        expander=StubExpander(),
        principal=p,  # type: ignore[arg-type]
        config_hash="a" * 64,
        **kw,  # type: ignore[arg-type]
    )


def test_untruncated_total_rows_exact_and_truncated_total_unknown() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(
            gw,
            [account("acc_a", ["alpha"]), account("acc_b", ["alpha"]), account("acc_c", ["alpha"])],
        )
        p = principal("user:a", ("alpha",))
        full = await _directory(gw, p, max_rows=500)
        assert not full.truncated
        assert full.truncation_reason is None
        assert full.returned_rows == len(full.rows) == 3
        assert full.total_rows == 3  # complete enumeration == exact trusted count
        capped = await _directory(gw, p, max_rows=2)
        assert capped.truncated
        assert capped.truncation_reason == "rows"
        assert capped.returned_rows == 2
        assert capped.total_rows is None  # unknown under truncation, never fabricated

    asyncio.run(scenario())


def test_token_packing_packs_in_query_order_with_lockstep_citations() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(
            gw,
            [account("acc_a", ["alpha"]), account("acc_b", ["alpha"]), account("acc_c", ["alpha"])],
        )
        p = principal("user:a", ("alpha",))
        est = ByteTokenEstimator()
        # The budget bounds the FULL model-facing render (frame + rows), not just the
        # row values (C1): the fixed frame estimates ~60 tokens and each demo row ~30,
        # so a 130-token budget keeps a 2-row prefix (frame 60 + 30 + 30 = 120 <= 130;
        # a third row, 150, overruns).
        budget = 130
        packed = await _directory(gw, p, max_rows=500, max_result_tokens=budget)
        assert packed.truncated
        assert packed.truncation_reason == "tokens"
        assert packed.returned_rows == len(packed.rows) == 2
        assert len(packed.citations) == len(packed.rows)  # lockstep
        assert packed.total_rows is None
        # Query order = the trusted ORDER BY (citation var ascending): acc_a, acc_b.
        assert packed.rows == (
            (entity_iri("acc_a"), "acc_a Corp"),
            (entity_iri("acc_b"), "acc_b Corp"),
        )
        assert packed.citations == (entity_iri("acc_a"), entity_iri("acc_b"))
        # §7.2 packing invariant (C1): the FULL model-facing rendering — header +
        # column header + separator + every kept row — fits within the budget plus at
        # most one always-kept row of slack. The single-string estimate is <= the sum
        # of the per-call estimates the packer charged, so honoring the whole render
        # (not just row values) is what keeps the model payload under the cap.
        full_render_est = est.estimate(render_answer_text(packed))
        one_row_slack = max(est.estimate("\n" + " | ".join(r)) for r in packed.rows)
        assert full_render_est <= budget + one_row_slack

    asyncio.run(scenario())


def test_packing_default_off_is_byte_identical() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(
            gw,
            [account("acc_a", ["alpha"]), account("acc_b", ["alpha"]), account("acc_c", ["alpha"])],
        )
        p = principal("user:a", ("alpha",))
        off = await _directory(gw, p, max_rows=2)  # None max_result_tokens => packing off
        explicit_none = await _directory(gw, p, max_rows=2, max_result_tokens=None)
        assert off.rows == explicit_none.rows
        assert off.citations == explicit_none.citations
        assert off.truncated == explicit_none.truncated is True
        assert off.truncation_reason == explicit_none.truncation_reason == "rows"

    asyncio.run(scenario())


def test_render_answer_text_envelope_header() -> None:
    untruncated = _answer(
        rows=((entity_iri("acc_a"), "A"), (entity_iri("acc_b"), "B")),
        citations=(entity_iri("acc_a"), entity_iri("acc_b")),
        returned_rows=2,
        total_rows=2,
        truncated=False,
        truncation_reason=None,
    )
    assert render_answer_text(untruncated).splitlines()[0] == "[showing 2 of 2 rows]"
    rows_cut = _answer(
        rows=((entity_iri("acc_a"), "A"), (entity_iri("acc_b"), "B")),
        citations=(entity_iri("acc_a"), entity_iri("acc_b")),
        returned_rows=2,
        total_rows=None,
        truncated=True,
        truncation_reason="rows",
    )
    assert (
        render_answer_text(rows_cut).splitlines()[0]
        == "[showing 2 of unknown rows, truncated (rows)]"
    )
    tokens_cut = _answer(
        returned_rows=1,
        total_rows=None,
        truncated=True,
        truncation_reason="tokens",
    )
    header = render_answer_text(tokens_cut).splitlines()[0]
    assert header == "[showing 1 of unknown rows, truncated (tokens)]"
    # the old trailing "[truncated at N rows]" line is gone.
    assert "[truncated at" not in render_answer_text(rows_cut)


def test_artifact_rows_and_citations_join_back_to_t2() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_alpha", ["alpha"])])
        ans = await execute_template(
            _TPL["accounts-directory"],
            {},
            store=gw,
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash="a" * 64,
            max_rows=500,
        )
        assert ans.columns == ("account", "name")
        assert ans.rows == ((entity_iri("acc_alpha"), "acc_alpha Corp"),)
        # Citations equal the deterministic urn:ne:entity:<key> T2 join (lift.entity_iri).
        assert ans.citations == (entity_iri("acc_alpha"),)
        assert ans.template_id == "accounts-directory"
        assert ans.template_version == 1
        assert not ans.truncated

    asyncio.run(scenario())


def test_truncation_flag_set_when_rows_exceed_limit() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(
            gw,
            [account("acc_a", ["alpha"]), account("acc_b", ["alpha"]), account("acc_c", ["alpha"])],
        )
        ans = await execute_template(
            _TPL["accounts-directory"],
            {},
            store=gw,
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash="a" * 64,
            max_rows=1,
        )
        assert ans.truncated
        assert len(ans.rows) == 1
        assert ans.row_limit == 1

    asyncio.run(scenario())


def test_render_answer_text_is_deterministic() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_alpha", ["alpha"])])
        ans = await execute_template(
            _TPL["accounts-directory"],
            {},
            store=gw,
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash="a" * 64,
            max_rows=500,
        )
        text = render_answer_text(ans)
        assert render_answer_text(ans) == text  # stable
        assert "account | name" in text
        assert entity_iri("acc_alpha") in text

    asyncio.run(scenario())


# --- NE-TEMPLATE-ENUM (SPEC §12 item 3): refusal vs empty result -------------

_ENUM_TOML = """
template_id = "accounts-by-status"
version = 1
description = "Accounts whose name matches one closed-vocabulary value."
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  ?account a <https://neutron.enterprise/schema/operational/Account> .
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
  FILTER(?name = ?status)
}
\"\"\"

[args]
status = { type = "str", enum = ["acc_a Corp", "ghost"] }

[scope]
graph_classes = ["alpha"]

[answer]
columns = ["account", "name"]
citation_var = "account"
"""


def _enum_template() -> object:
    from ne.brain_ontology.verified.templates import parse_template_toml

    return parse_template_toml(_ENUM_TOML)


def test_out_of_vocabulary_arg_is_a_loud_refusal_never_a_silent_zero_rows() -> None:
    """SPEC §12 item 3 decisive test: an out-of-vocabulary argument is REFUSED, the
    refusal names the field + the permitted values (published schema), never echoes
    the rejected value (C12), and executes NOTHING — provably distinguishable from
    an empty result both by type (an exception, not a VerifiedAnswer) and by the
    store never being touched."""
    from ne.brain_ontology.verified.errors import VerifiedAnswerArgsError

    from .verified_helpers import RecordingStore

    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_a", ["alpha"])])
        recording = RecordingStore(gw)
        with pytest.raises(VerifiedAnswerArgsError) as excinfo:
            await execute_template(
                _enum_template(),  # type: ignore[arg-type]
                {"status": "unresolved"},  # the exact live-walkthrough guess
                store=recording,  # type: ignore[arg-type]
                expander=StubExpander(),
                principal=principal("user:a", ("alpha",)),
                config_hash="a" * 64,
                max_rows=500,
            )
        message = str(excinfo.value)
        assert "'status'" in message  # names the FIELD
        assert "must be one of: acc_a Corp, ghost" in message  # names the vocabulary
        assert "unresolved" not in message  # never echoes the rejected value (C12)
        assert "refusal, not an empty result" in message
        assert recording.scoped_calls == 0  # nothing executed — refusal precedes the read

    asyncio.run(scenario())


def test_in_vocabulary_arg_matching_nothing_is_an_empty_result_not_a_refusal() -> None:
    """The CONTROL that proves the fix didn't just make everything fail: a permitted
    value that legitimately matches nothing still EXECUTES and returns an empty
    VerifiedAnswer (0 rows, store touched) — no exception."""
    from .verified_helpers import RecordingStore

    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_a", ["alpha"])])
        recording = RecordingStore(gw)
        ans = await execute_template(
            _enum_template(),  # type: ignore[arg-type]
            {"status": "ghost"},  # in-vocabulary, matches no account name
            store=recording,  # type: ignore[arg-type]
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash="a" * 64,
            max_rows=500,
        )
        assert ans.returned_rows == 0
        assert ans.rows == ()
        assert recording.scoped_calls >= 1  # the query genuinely ran

    asyncio.run(scenario())


def test_zero_row_render_carries_the_query_scoped_note() -> None:
    # SPEC §12 item 4 (the model-facing surface): a zero-row rendering states the
    # claim at query scope — never "the store holds no such data".
    from ne.brain_ontology.verified.answers import ZERO_ROWS_NOTE

    empty = _answer(rows=(), citations=(), returned_rows=0, total_rows=0)
    text = render_answer_text(empty)
    assert text.splitlines()[0] == "[showing 0 of 0 rows]"
    assert ZERO_ROWS_NOTE in text
    assert "not evidence that the data does not exist" in ZERO_ROWS_NOTE
    # CONTROL (bidirectional): a non-empty answer carries NO note — the note is
    # driven by the zero-row condition, not appended unconditionally.
    assert ZERO_ROWS_NOTE not in render_answer_text(_answer())


def test_refusal_formatted_exception_chain_never_carries_the_rejected_value(
    tmp_path: Path,
) -> None:
    """C12 chain sweep over BOTH package-level refusal raise sites: the rejected
    value must appear nowhere in the FORMATTED exception chain —
    ``traceback.format_exception`` over the raised error, not just ``str(exc)`` —
    because pydantic's chained ``ValidationError`` renders it as
    ``input_value=...``. That formatted chain is exactly the text any future
    ``logger.exception`` / Sentry / OTel ``record_exception`` would ship, so a
    surviving cause is a leak sitting one ordinary logging change away.
    ``raise ... from None`` at the raise sites is the mechanism; this sweep is
    the pin, and the ``__cause__``/``__suppress_context__`` asserts make each
    site's severed link individually mutation-visible.

    CONTROL (should-fire): the raw pydantic error's own formatted chain DOES
    carry the sentinel — the identical assertion shape catches a leak where one
    exists, so a green sweep is evidence of absence, not a blind sweep.
    """
    import traceback

    from pydantic import ValidationError

    from ne.brain_ontology.config import OntologySettings
    from ne.brain_ontology.verified import build_verified_answer_tools
    from ne.brain_ontology.verified.errors import VerifiedAnswerArgsError

    sentinel = "SENTINEL-c12-rejected-value-4f9d"
    template = _enum_template()

    def formatted_chain(exc: BaseException) -> str:
        return "".join(traceback.format_exception(exc))

    # CONTROL: plant the sentinel where it SHOULD appear (pydantic's own error).
    with pytest.raises(ValidationError) as control:
        template.args_model.model_validate({"status": sentinel})  # type: ignore[attr-defined]
    assert sentinel in formatted_chain(control.value)  # the sweep can see a leak

    # Site 1 — ``execute_template``'s defense-in-depth re-validation.
    async def site1() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_a", ["alpha"])])
        with pytest.raises(VerifiedAnswerArgsError) as excinfo:
            await execute_template(
                template,  # type: ignore[arg-type]
                {"status": sentinel},
                store=gw,
                expander=StubExpander(),
                principal=principal("user:a", ("alpha",)),
                config_hash="a" * 64,
                max_rows=500,
            )
        assert excinfo.value.__cause__ is None  # from None: the chain is severed
        assert excinfo.value.__suppress_context__
        assert sentinel not in formatted_chain(excinfo.value)

    asyncio.run(site1())

    # Site 2 — the pre-gate ``validate_args`` built by ``build_verified_answer_tools``.
    (tmp_path / "accounts_by_status.toml").write_text(_ENUM_TOML, encoding="utf-8")
    registry = load_template_registry(tmp_path)
    (tool_def,) = build_verified_answer_tools(
        registry=registry,
        store=make_gateway(),
        expander=StubExpander(),
        principal_provider=lambda: principal("user:a", ("alpha",)),
        settings=OntologySettings(verified_max_rows=500),
        config_hash="a" * 64,
    )
    with pytest.raises(VerifiedAnswerArgsError) as excinfo2:
        tool_def.validate_args({"status": sentinel})
    assert excinfo2.value.__cause__ is None  # from None: the chain is severed
    assert excinfo2.value.__suppress_context__
    assert sentinel not in formatted_chain(excinfo2.value)


def test_replay_procedural_identity_and_mutation_detected() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_alpha", ["alpha"])])
        p = principal("user:a", ("alpha",))
        first = await execute_template(
            _TPL["accounts-directory"],
            {},
            store=gw,
            expander=StubExpander(),
            principal=p,
            config_hash="a" * 64,
            max_rows=500,
        )
        second = await execute_template(
            _TPL["accounts-directory"],
            {},
            store=gw,
            expander=StubExpander(),
            principal=p,
            config_hash="a" * 64,
            max_rows=500,
        )
        # Procedural identity: equal ignoring executed_at (touched graphs unchanged).
        assert answers_equal(first, second)
        assert first.executed_at is not None
        # After a store write the diff is detected (rows change).
        await seed_accounts(gw, [account("acc_two", ["alpha"])])
        third = await execute_template(
            _TPL["accounts-directory"],
            {},
            store=gw,
            expander=StubExpander(),
            principal=p,
            config_hash="a" * 64,
            max_rows=500,
        )
        assert not answers_equal(first, third)

    asyncio.run(scenario())
