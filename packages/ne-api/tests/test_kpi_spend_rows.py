# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C8 spend attribution on the /v1/kpi/summary wire.

The whitepaper tells a buyer their AI spend is traceable to named business
outcomes. These tests pin what the wire must and must NOT say about that:

* named outcomes carry only the spend that DECLARED them,
* untagged spend appears as its own labelled row and is never folded in,
* the attribution SHARE is served (so the console never divides), and is
  OMITTED rather than fabricated when there is no denominator.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ne.events import new_audit_event
from ne.schema import AuditOutcome

from .conftest import AppBuilder, get_state, run


def _seed_calls(state: object, calls: list[tuple[str | None, str | None]]) -> None:
    """Seed gateway cost-bearing audit rows as ``(kpi_tag, cost_usd)`` pairs."""
    now = datetime.now(UTC)

    async def _run() -> None:
        for kpi_tag, cost in calls:
            await state.sink.append(  # type: ignore[attr-defined]
                new_audit_event(
                    action="ne.gateway.complete",
                    resource="gateway",
                    outcome=AuditOutcome.success,
                    actor="user:ada",
                    kpi_tag=kpi_tag,
                    provenance=None if cost is None else {"cost_usd": cost},
                    occurred_at=now,
                )
            )

    run(_run())


def _by_tag(h: object) -> dict[str, dict]:
    out: dict[str, dict] = {}

    async def _t() -> None:
        async with h.client() as client:  # type: ignore[attr-defined]
            r = await client.get(
                "/api/v1/kpi/summary",
                headers=h.bearer("u", groups=[]),  # type: ignore[attr-defined]
            )
            assert r.status_code == 200
            out.update({e["kpi_tag"]: e for e in r.json()["kpis"]})

    run(_t())
    return out


def test_named_outcome_row_carries_only_the_spend_that_declared_it(
    make_app: AppBuilder,
) -> None:
    h = make_app()
    _seed_calls(
        get_state(h.app),
        [("renewal-retention", "1.0"), ("renewal-retention", "0.5"), ("quote-speed", "0.25")],
    )

    by_tag = _by_tag(h)

    assert by_tag["spend.outcome.renewal-retention"]["value"] == 1.5
    assert by_tag["spend.outcome.renewal-retention"]["unit"] == "usd"
    assert by_tag["spend.outcome.quote-speed"]["value"] == 0.25


def test_untagged_spend_is_served_as_its_own_row_not_folded_into_an_outcome(
    make_app: AppBuilder,
) -> None:
    """The defect this surface exists to prevent, at the wire boundary."""
    h = make_app()
    _seed_calls(get_state(h.app), [("renewal-retention", "1.0"), (None, "3.0")])

    by_tag = _by_tag(h)

    assert by_tag["spend.outcome.renewal-retention"]["value"] == 1.0
    assert by_tag["spend.unattributed"]["value"] == 3.0
    assert by_tag["spend.total_usd"]["value"] == 4.0
    assert by_tag["spend.attributed_usd"]["value"] == 1.0


def test_unattributed_row_is_served_even_when_no_outcome_was_ever_declared(
    make_app: AppBuilder,
) -> None:
    """A deployment that tagged NOTHING must still see its spend — as
    unattributed. Suppressing the row would leave a blank screen implying there
    was no spend at all."""
    h = make_app()
    _seed_calls(get_state(h.app), [(None, "2.0")])

    by_tag = _by_tag(h)

    assert by_tag["spend.unattributed"]["value"] == 2.0
    assert not [t for t in by_tag if t.startswith("spend.outcome.")]


def test_call_count_rides_the_served_dimensions_so_the_console_counts_nothing(
    make_app: AppBuilder,
) -> None:
    h = make_app()
    _seed_calls(
        get_state(h.app),
        [("renewal-retention", "1.0"), ("renewal-retention", "1.0"), (None, "1.0")],
    )

    by_tag = _by_tag(h)

    assert by_tag["spend.outcome.renewal-retention"]["dimensions"]["calls"] == "2"
    assert by_tag["spend.unattributed"]["dimensions"]["calls"] == "1"


def test_attributed_share_is_served_by_the_server(make_app: AppBuilder) -> None:
    h = make_app()
    _seed_calls(get_state(h.app), [("renewal-retention", "3.0"), (None, "1.0")])

    by_tag = _by_tag(h)

    assert by_tag["spend.attributed_share"]["value"] == 0.75
    assert by_tag["spend.attributed_share"]["unit"] == "ratio"


def test_attributed_share_row_is_omitted_when_nothing_was_spent(
    make_app: AppBuilder,
) -> None:
    """A private-lane window prices every call at $0.00. Serving a share of 1.0
    off a 0/0 would tell a buyer their spend is fully attributed on the strength
    of no evidence at all."""
    h = make_app()
    _seed_calls(get_state(h.app), [("renewal-retention", "0.0"), (None, "0.0")])

    by_tag = _by_tag(h)

    assert "spend.attributed_share" not in by_tag
    assert by_tag["spend.total_usd"]["value"] == 0.0
    # ...but the calls are still visible, so the screen is not silently blank.
    assert by_tag["spend.outcome.renewal-retention"]["dimensions"]["calls"] == "1"


def test_unpriced_calls_are_reported_so_a_short_total_says_so(
    make_app: AppBuilder,
) -> None:
    h = make_app()
    _seed_calls(get_state(h.app), [("renewal-retention", "1.0"), ("renewal-retention", None)])

    by_tag = _by_tag(h)

    assert by_tag["spend.calls_missing_cost"]["value"] == 1.0
    assert by_tag["spend.calls_missing_cost"]["unit"] == "count"
    assert by_tag["spend.outcome.renewal-retention"]["value"] == 1.0


def test_call_coverage_share_is_served_beside_the_dollar_share(
    make_app: AppBuilder,
) -> None:
    """One priced tagged call beside three untagged $0.00 calls: dollar-weighted
    attribution is 100%, call-weighted coverage is 25%. Both go on the wire, so
    the screen cannot show the flattering one alone."""
    h = make_app()
    _seed_calls(
        get_state(h.app),
        [("renewal-retention", "1.0"), (None, "0.0"), (None, "0.0"), (None, "0.0")],
    )

    by_tag = _by_tag(h)

    assert by_tag["spend.attributed_share"]["value"] == 1.0
    assert by_tag["spend.attributed_call_share"]["value"] == 0.25
    assert by_tag["spend.total_usd"]["dimensions"]["calls"] == "4"
    assert by_tag["spend.attributed_usd"]["dimensions"]["calls"] == "1"


def test_outcome_row_reports_how_many_of_its_calls_are_unpriced(
    make_app: AppBuilder,
) -> None:
    """A row whose money figure does not cover all its calls says so on the row."""
    h = make_app()
    _seed_calls(get_state(h.app), [("renewal-retention", "1.0"), ("renewal-retention", None)])

    by_tag = _by_tag(h)

    row = by_tag["spend.outcome.renewal-retention"]
    assert row["dimensions"]["calls"] == "2"
    assert row["dimensions"]["unpriced"] == "1"
    assert row["value"] == 1.0


def test_no_missing_cost_row_when_every_call_was_priced(make_app: AppBuilder) -> None:
    """The control: the gap row appears because there IS a gap, not always."""
    h = make_app()
    _seed_calls(get_state(h.app), [("renewal-retention", "1.0")])

    by_tag = _by_tag(h)

    assert "spend.calls_missing_cost" not in by_tag
