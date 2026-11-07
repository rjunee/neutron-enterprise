# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-WINDOW-ENDPOINTS: a declared date window publishes what its bounds MEAN.

THE DEFECT. ``[date_window]`` bounds are MODEL-SUPPLIED, the generated SPARQL is
``>=`` / ``<=`` on both sides, and nothing the model read said how to compute the
far endpoint. Live, the flagship multi-hop question returned one subject too few
and said why in its own answer: a row due on day N of an N-day window was called
"outside window". The template was not at fault -- it is inclusive, and says so in
its prose description. What was missing is that "inclusive" describes the MATCH
and never described the ARITHMETIC, and an N-day window is exactly where D+N and
D+N-1 are both plausible readings.

WHY THE SCHEMA. An argument's meaning belongs in that argument's schema, which the
model reads on the turn it picks the value. Prose in the template description is
one retrieval away from not being read; a system-prompt sentence is paid for by
every run that has no window at all. This reaches precisely the templates that
declare a window, and it is arithmetic rather than an answer: no span, no
reference date, and no subject appears in it.

WHAT THIS SUITE PROVES. That the semantics ARE published (a template with a window
carries them; one without is untouched), that they say the thing the failure
turned on (D+N, not D+N-1), and that the SQL they describe is what the executed
query actually does -- a description that drifted from the FILTER would be worse
than none.
"""

from __future__ import annotations

from typing import Any, cast

from ne.brain_ontology.verified.meridian import load_meridian_template_registry
from ne.brain_ontology.verified.templates import QueryTemplate


def _properties(template: QueryTemplate) -> dict[str, dict[str, Any]]:
    """The generated schema's ``properties`` map, typed. ``args_json_schema`` is
    declared ``dict[str, object]``, which is right for its callers and useless for
    a test that reads one field out of it."""
    return cast(dict[str, dict[str, Any]], template.args_json_schema()["properties"])


def test_a_windowed_template_publishes_its_endpoint_arithmetic() -> None:
    """Both bounds are described, and the far one resolves the off-by-one."""
    windowed = [t for t in load_meridian_template_registry().templates if t.date_window]
    assert windowed, "no template declares [date_window]; this suite would prove nothing"

    for template in windowed:
        window = template.date_window
        assert window is not None
        properties = _properties(template)
        start = properties[window.start_arg]["description"]
        end = properties[window.end_arg]["description"]

        # Both endpoints match. Saying it on BOTH is deliberate: a model that reads
        # only the bound it is unsure of still gets the answer.
        assert "INCLUSIVE" in start and "INCLUSIVE" in end
        # The arithmetic, which is the half that was missing.
        assert "ENDS ON D+N, not D+N-1" in end
        assert "do not subtract one" in end
        # The declared cap travels with it, so the bound is not a separate lookup.
        assert f"at most {window.max_days} days" in end


def test_the_generated_clause_reaches_the_endpoints_and_nothing_else() -> None:
    """The GENERATED clause reaches the two endpoint args and no others.

    NE-CAPTAB added a second, author-written description channel (``description``
    under ``[args.<name>]`` in the template TOML), so "no arg outside a window
    carries a description" is no longer the invariant -- an author may now document
    any argument, and the console's Capabilities panel reads exactly that.

    The invariant this suite still holds, and the one it was written for, is about
    PROVENANCE: the window arithmetic is generated, it is derived from the declared
    ``max_days``, and it must not leak onto an argument that is not a declared
    endpoint. So every non-endpoint property is pinned to the author's declared
    text VERBATIM -- absent when the author declared none, byte-identical when they
    did -- and is separately proven free of the generated clause. A generator that
    started writing prose onto ordinary args would fail both halves.
    """
    for template in load_meridian_template_registry().templates:
        window = template.date_window
        endpoints = {window.start_arg, window.end_arg} if window is not None else set()
        for name, prop in _properties(template).items():
            if name in endpoints:
                continue
            declared = template.args[name].description
            # Verbatim, or no key at all when the author declared nothing.
            assert prop.get("description") == declared
            if declared is None:
                assert "description" not in prop
            text = declared or ""
            for generated in ("INCLUSIVE", "ENDS ON D+N", "do not subtract one"):
                assert generated not in text


def test_the_published_semantics_match_the_query_that_actually_runs() -> None:
    """The description is checked against the FILTER, not merely asserted.

    An inclusive-sounding description over an exclusive query would turn a prompt
    fix into a lie the model believes, so the two are pinned together: ``>=`` on
    the start bound and ``<=`` on the end bound is what makes "a row dated exactly
    this day is returned" true.
    """
    for template in load_meridian_template_registry().templates:
        window = template.date_window
        if window is None:
            continue
        body = " ".join(template.sparql.split())
        assert f">= ?{window.start_arg}" in body
        assert f"<= ?{window.end_arg}" in body


def test_the_clause_states_no_span_no_date_and_no_subject() -> None:
    """It is arithmetic. A concrete N, D or subject here would be the answer.

    The live failure was a 14-day window anchored on a fixture as-of date; naming
    either would fix that one question and teach the model nothing about the next.
    """
    for template in load_meridian_template_registry().templates:
        window = template.date_window
        if window is None:
            continue
        properties = _properties(template)
        text = (
            properties[window.start_arg]["description"] + properties[window.end_arg]["description"]
        )
        assert "14" not in text
        assert "2026" not in text
        for subject in ("loan", "payment", "hardship", "Meridian", "DR-"):
            assert subject not in text
