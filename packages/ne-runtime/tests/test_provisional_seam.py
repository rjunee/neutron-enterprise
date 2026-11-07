# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Turn-scoped provisional-context accumulator (NE-T2X-5, design §6.1/§6.3):
sum/dedup semantics, mode-conflict resolution, inherit-if-ambient scope
nesting, and the outside-any-scope no-op behavior."""

from __future__ import annotations

import pytest

from ne.runtime.provisional import (
    ProvisionalContext,
    current_provisional_context,
    current_provisional_recorder,
    provisional_context_scope,
)

# --- outside any scope: everything is None/absent --------------------------


def test_no_active_scope_recorder_and_context_are_none() -> None:
    assert current_provisional_recorder() is None
    assert current_provisional_context() is None


# --- count sums across recordings -------------------------------------------


def test_counts_sum_across_multiple_records() -> None:
    with provisional_context_scope() as acc:
        acc.record(provisional_source_count=2, extraction_mode="read_flagged")
        acc.record(provisional_source_count=3, extraction_mode="read_flagged")
        snap = current_provisional_context()
    assert snap is not None
    assert snap.provisional_source_count == 5
    assert snap.provisional is True


def test_negative_count_rejected() -> None:
    with (
        provisional_context_scope() as acc,
        pytest.raises(ValueError, match="provisional_source_count"),
    ):
        acc.record(provisional_source_count=-1, extraction_mode="read_flagged")


# --- refs: ordered, deduplicated --------------------------------------------


def test_refs_are_ordered_and_deduplicated() -> None:
    with provisional_context_scope() as acc:
        acc.record(
            provisional_source_count=1,
            provisional_source_refs=["llm:aaaaaaaaaaaaaaaa:e1", "llm:bbbbbbbbbbbbbbbb:e2"],
            extraction_mode="read_flagged",
        )
        acc.record(
            provisional_source_count=1,
            provisional_source_refs=["llm:aaaaaaaaaaaaaaaa:e1", "llm:cccccccccccccccc:e3"],
            extraction_mode="read_flagged",
        )
        snap = current_provisional_context()
    assert snap is not None
    assert snap.provisional_source_refs == (
        "llm:aaaaaaaaaaaaaaaa:e1",
        "llm:bbbbbbbbbbbbbbbb:e2",
        "llm:cccccccccccccccc:e3",
    )


def test_provisional_true_from_refs_alone_even_with_zero_count() -> None:
    with provisional_context_scope() as acc:
        acc.record(
            provisional_source_count=0,
            provisional_source_refs=["llm:aaaaaaaaaaaaaaaa:e1"],
            extraction_mode="read_flagged",
        )
        snap = current_provisional_context()
    assert snap is not None
    assert snap.provisional_source_count == 0
    assert snap.provisional is True


# --- mode-conflict rule: first non-"autonomous" wins ------------------------


def test_mode_is_recorded_when_only_one_call() -> None:
    with provisional_context_scope() as acc:
        acc.record(provisional_source_count=1, extraction_mode="read_flagged")
        snap = current_provisional_context()
    assert snap is not None
    assert snap.extraction_mode == "read_flagged"


def test_autonomous_only_stays_autonomous() -> None:
    with provisional_context_scope() as acc:
        acc.record(provisional_source_count=1, extraction_mode="autonomous")
        acc.record(provisional_source_count=1, extraction_mode="autonomous")
        snap = current_provisional_context()
    assert snap is not None
    assert snap.extraction_mode == "autonomous"


def test_non_autonomous_mode_overrides_earlier_autonomous() -> None:
    with provisional_context_scope() as acc:
        acc.record(provisional_source_count=1, extraction_mode="autonomous")
        acc.record(provisional_source_count=1, extraction_mode="read_flagged")
        snap = current_provisional_context()
    assert snap is not None
    assert snap.extraction_mode == "read_flagged"


def test_autonomous_after_non_autonomous_does_not_displace_it() -> None:
    with provisional_context_scope() as acc:
        acc.record(provisional_source_count=1, extraction_mode="read_flagged")
        acc.record(provisional_source_count=1, extraction_mode="autonomous")
        snap = current_provisional_context()
    assert snap is not None
    assert snap.extraction_mode == "read_flagged"


def test_no_records_leaves_mode_none() -> None:
    with provisional_context_scope():
        snap = current_provisional_context()
    assert snap is not None
    assert snap.extraction_mode is None
    assert snap.provisional is False


# --- inherit-if-ambient: nested scopes share ONE accumulator ----------------


def test_nested_scope_inherits_ambient_accumulator() -> None:
    with provisional_context_scope() as outer:
        outer.record(provisional_source_count=1, extraction_mode="read_flagged")
        with provisional_context_scope() as inner:
            assert inner is outer
            inner.record(provisional_source_count=1, extraction_mode="read_flagged")
        # The OUTER snapshot sees the inner recording too — poisoned context
        # follows the call tree (a sub-agent dispatched inside a provisional
        # turn shares the parent's accumulator).
        snap = current_provisional_context()
    assert snap is not None
    assert snap.provisional_source_count == 2


# --- contextvar reset on exit ------------------------------------------------


def test_scope_resets_to_none_on_exit() -> None:
    with provisional_context_scope() as acc:
        acc.record(provisional_source_count=1, extraction_mode="read_flagged")
    assert current_provisional_recorder() is None
    assert current_provisional_context() is None


def test_scope_resets_on_exception() -> None:
    with pytest.raises(RuntimeError), provisional_context_scope() as acc:
        acc.record(provisional_source_count=1, extraction_mode="read_flagged")
        raise RuntimeError("boom")
    assert current_provisional_recorder() is None
    assert current_provisional_context() is None


def test_sequential_scopes_do_not_leak_between_turns() -> None:
    with provisional_context_scope() as first:
        first.record(provisional_source_count=5, extraction_mode="read_flagged")
    with provisional_context_scope() as second:
        assert second is not first
        snap = current_provisional_context()
    assert snap is not None
    assert snap.provisional_source_count == 0
    assert snap.provisional is False


# --- ProvisionalContext.provisional property ---------------------------------


@pytest.mark.parametrize(
    ("count", "refs", "expected"),
    [
        (0, (), False),
        (1, (), True),
        (0, ("llm:aaaaaaaaaaaaaaaa:e1",), True),
        (3, ("llm:aaaaaaaaaaaaaaaa:e1",), True),
    ],
)
def test_provisional_context_provisional_property(
    count: int, refs: tuple[str, ...], expected: bool
) -> None:
    ctx = ProvisionalContext(
        provisional_source_count=count, provisional_source_refs=refs, extraction_mode=None
    )
    assert ctx.provisional is expected
