# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The runner's blackout predicate is bound to the header this package PRODUCES.

`ne.runtime.runner` recognises exactly one tool-result grammar — the header
`ne.api.tools._result_header` writes above a governed search's chunks — and reads
two things off it: whether the result is a chunk rendering at all
(`_progress_digest_material`, which drops the header line and the reranker's
ordering before hashing) and whether the result disclosed nothing
(`_disclosed_nothing`, which is what stops a run looping over a 100%-withheld
corpus).

⚠️ WHY THIS FILE EXISTS. Those two live behind a regex COPY of the grammar, and
the runner's own tests assert against COPIED literals. So the producer and the
consumer could drift apart with every test in both packages still green — and the
drift is silent in the direction that matters: an unrecognised header makes
`_disclosed_nothing` return False and the digest hash the whole output, so a real
fully-withheld search stops matching the rule and can run to the tool-call
ceiling, publishing `budget-exhausted` for a stop that was never a budget.

The binding cannot live in an ne-runtime test: `ne-api` depends on `ne-runtime`,
so importing `ne.api` from there would invert the workspace dependency and the C3
import-linter boundary. It is free in THIS direction, which is why it is here.
"""

from __future__ import annotations

import pytest

from ne.api.tools import _result_header
from ne.brain.withholding import SourceHealthWithholding
from ne.runtime.runner import _CHUNK_RESULT_HEADER_RE, _disclosed_nothing


def _header(*, shown: int, total: int, withheld: int = 0, candidates: int = 0) -> str:
    return _result_header(
        shown=shown,
        total=total,
        withholding=SourceHealthWithholding(candidates=candidates, withheld=withheld),
    )


@pytest.mark.parametrize(
    ("shown", "total", "withheld", "candidates"),
    [
        (0, 0, 0, 0),  # an empty corpus: a true 0 of 0, nothing withheld
        (3, 19, 0, 0),  # the ordinary served read
        (0, 4, 4, 4),  # TOTAL BLACKOUT — the case the rule exists for
        (2, 7, 5, 7),  # a partial withholding: some disclosed, some not
        (1, 1, 0, 3),  # candidates inspected, none dropped
    ],
)
def test_every_header_this_package_emits_is_one_the_runner_recognises(
    shown: int, total: int, withheld: int, candidates: int
) -> None:
    """The consumer's regex must match the producer's output, not a copy of it."""
    line = _header(shown=shown, total=total, withheld=withheld, candidates=candidates)
    assert _CHUNK_RESULT_HEADER_RE.match(line.strip()) is not None, (
        f"the runner no longer recognises a header this package emits: {line!r} — "
        "_CHUNK_RESULT_HEADER_RE and _result_header have drifted"
    )


def test_a_total_blackout_rendered_by_the_producer_reads_as_disclosing_nothing() -> None:
    """`shown == 0 and withheld > 0` off the REAL header, not a hand-written one."""
    rendered = _header(shown=0, total=4, withheld=4, candidates=4)
    assert _disclosed_nothing(rendered) is True


def test_a_read_that_served_something_is_never_read_as_a_blackout() -> None:
    """The anti-overmute direction: a partial withholding still disclosed passages."""
    partial = f"{_header(shown=2, total=7, withheld=5, candidates=7)}\nabc:0001: one\nabc:0002: two"
    assert _disclosed_nothing(partial) is False

    clean = f"{_header(shown=3, total=19)}\nabc:0001: one\nabc:0002: two\nabc:0003: three"
    assert _disclosed_nothing(clean) is False


def test_an_empty_corpus_is_not_a_blackout() -> None:
    """A true 0 of 0 withheld nothing — the distinction the `; K withheld` clause exists to draw."""
    assert _disclosed_nothing(_header(shown=0, total=0)) is False
