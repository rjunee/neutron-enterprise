# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``collect_spend`` against a REAL sqlite audit chain (C8 KPI attribution).

The whitepaper's C8 claim is that AI spend is traceable to NAMED BUSINESS
OUTCOMES. These tests pin the two halves of that claim that can be got wrong in
a way a buyer would not notice:

* spend that CARRIES a declared tag is bucketed under that tag, and
* spend that carries NO tag stays in a VISIBLY SEPARATE unattributed bucket —
  never folded into a named outcome, never imputed, never defaulted.

The read seam is ``ne.audit.replay`` (payload-authoritative, hash-covered), NOT
the ne-events outbox: outbox rows are DELETED by ``prune_acked`` retention, so
the outbox cannot answer a question about last month.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from ne.obs.collect import collect_spend
from ne.obs.kpi import ReportPeriod

from .conftest import seed_audit_event

JUNE = ReportPeriod(year=2026, month=6)


def _gateway_call(
    engine: sa.Engine,
    *,
    cost: str | None,
    kpi_tag: str | None,
    day: int = 5,
    action: str = "ne.gateway.complete",
) -> None:
    """Seed one gateway cost-bearing audit row, the shape the gateway emits."""
    seed_audit_event(
        engine,
        action=action,
        actor="user:ada@acme.com",
        occurred_at=datetime(2026, 6, day, tzinfo=UTC),
        provenance=None if cost is None else {"cost_usd": cost},
        kpi_tag=kpi_tag,
    )


def test_spend_is_bucketed_under_the_declared_tag_it_carried(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    _gateway_call(audit_engine, cost="0.25", kpi_tag="renewal-retention", day=5)
    _gateway_call(audit_engine, cost="0.75", kpi_tag="renewal-retention", day=6)
    _gateway_call(audit_engine, cost="0.5", kpi_tag="quote-turnaround", day=7)

    spend = collect_spend(audit_engine, JUNE)

    by_tag = {o.kpi_tag: o for o in spend.outcomes}
    assert by_tag["renewal-retention"].cost_usd == 1.0
    assert by_tag["renewal-retention"].calls == 2
    assert by_tag["quote-turnaround"].cost_usd == 0.5
    assert by_tag["quote-turnaround"].calls == 1


def test_untagged_spend_stays_unattributed_and_enters_no_named_outcome(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    """THE defect this surface exists to prevent.

    An untagged call must never be imputed into a named outcome — not into the
    only outcome present, not into the largest, not into a default. A KPI screen
    that shows "renewal-retention $3.00" when $2.00 of that was never tagged is
    a false spend claim made to a buyer.
    """
    _gateway_call(audit_engine, cost="1.0", kpi_tag="renewal-retention", day=5)
    _gateway_call(audit_engine, cost="2.0", kpi_tag=None, day=6)

    spend = collect_spend(audit_engine, JUNE)

    assert [o.kpi_tag for o in spend.outcomes] == ["renewal-retention"]
    assert spend.outcomes[0].cost_usd == 1.0
    assert spend.outcomes[0].calls == 1
    assert spend.unattributed_usd == 2.0
    assert spend.unattributed_calls == 1


def test_empty_string_tag_names_no_outcome_and_counts_as_unattributed(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    """An empty tag is not an outcome named ``""`` — it names nobody."""
    _gateway_call(audit_engine, cost="1.5", kpi_tag="", day=5)

    spend = collect_spend(audit_engine, JUNE)

    assert spend.outcomes == ()
    assert spend.unattributed_usd == 1.5
    assert spend.unattributed_calls == 1


def test_call_without_a_recorded_cost_is_a_gap_not_a_zero_dollar_call(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    """A missing ``cost_usd`` means the price was NOT MEASURED.

    Summing it as $0.00 would understate spend while the totals looked complete.
    It is counted separately so the screen can say the total is short.
    """
    _gateway_call(audit_engine, cost="1.0", kpi_tag="renewal-retention", day=5)
    _gateway_call(audit_engine, cost=None, kpi_tag="renewal-retention", day=6)

    spend = collect_spend(audit_engine, JUNE)

    assert spend.calls_missing_cost == 1
    assert spend.outcomes[0].cost_usd == 1.0
    assert spend.outcomes[0].priced_calls == 1
    assert spend.total_usd == 1.0


def test_an_unpriced_call_keeps_the_outcome_it_declared(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    """Attribution and price are SEPARATE facts.

    A call declared against an outcome was declared against it whether or not the
    provider priced it. Dropping unpriced calls from the outcome would erase real
    attributed activity from the only screen that reports it — and on a
    self-hosted lane, where prices are frequently absent, that is most of it.
    """
    _gateway_call(audit_engine, cost="1.0", kpi_tag="renewal-retention", day=5)
    _gateway_call(audit_engine, cost=None, kpi_tag="renewal-retention", day=6)
    _gateway_call(audit_engine, cost=None, kpi_tag="renewal-retention", day=7)

    spend = collect_spend(audit_engine, JUNE)

    outcome = spend.outcomes[0]
    assert outcome.calls == 3
    assert outcome.priced_calls == 1
    assert outcome.unpriced_calls == 2
    # ...and the money still covers only the one call that had a price.
    assert outcome.cost_usd == 1.0


def test_an_outcome_of_only_unpriced_calls_still_gets_a_row(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    """The private-lane case. If the provider prices nothing, the outcome must
    still appear with its call count — otherwise the whole surface vanishes on
    exactly the deployment the product sells (self-hosted)."""
    _gateway_call(audit_engine, cost=None, kpi_tag="renewal-retention", day=5)

    spend = collect_spend(audit_engine, JUNE)

    assert [o.kpi_tag for o in spend.outcomes] == ["renewal-retention"]
    assert spend.outcomes[0].calls == 1
    assert spend.outcomes[0].cost_usd == 0.0
    assert spend.outcomes[0].unpriced_calls == 1


def test_call_share_reports_coverage_the_dollar_share_flatters(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    """THE NUMBER THAT STOPS THE SCREEN LYING.

    One priced frontier call under a declared outcome, beside three untagged
    self-hosted calls priced at $0.00. Dollar-weighted, attribution is 100%.
    By CALLS — what actually ran — it is 25%. Serving only the dollar share
    would tell a buyer every model call is attributed while three quarters of
    them are not.
    """
    _gateway_call(audit_engine, cost="1.0", kpi_tag="renewal-retention", day=5)
    for day in (6, 7, 8):
        _gateway_call(audit_engine, cost="0.0", kpi_tag=None, day=day)

    spend = collect_spend(audit_engine, JUNE)

    assert spend.attributed_share == 1.0
    assert spend.attributed_call_share == 0.25
    assert spend.total_calls == 4
    assert spend.attributed_calls == 1


def test_call_share_is_none_when_no_call_was_recorded(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    spend = collect_spend(audit_engine, JUNE)
    assert spend.attributed_call_share is None
    assert spend.total_calls == 0


def test_a_non_finite_cost_is_unrecorded_not_a_poisoned_total(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    """``float('nan')`` and ``float('inf')`` parse fine and would propagate into
    every sum, the shares, and the JSON response. They are not prices."""
    _gateway_call(audit_engine, cost="1.0", kpi_tag="renewal-retention", day=5)
    _gateway_call(audit_engine, cost="nan", kpi_tag="renewal-retention", day=6)
    _gateway_call(audit_engine, cost="Infinity", kpi_tag="renewal-retention", day=7)

    spend = collect_spend(audit_engine, JUNE)

    assert spend.total_usd == 1.0
    assert spend.attributed_usd == 1.0
    assert spend.calls_missing_cost == 2
    assert spend.attributed_share == 1.0


def test_recorded_zero_cost_is_a_measurement_and_is_not_counted_as_missing(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    """A private-lane call priced at $0.00 IS a measurement (gateway ``extract_cost``
    documents 0.0 as legitimate). Absent means unknown; zero means zero."""
    _gateway_call(audit_engine, cost="0.0", kpi_tag="renewal-retention", day=5)

    spend = collect_spend(audit_engine, JUNE)

    assert spend.calls_missing_cost == 0
    assert spend.outcomes[0].calls == 1
    assert spend.outcomes[0].cost_usd == 0.0


def test_attributed_share_is_served_and_excludes_unattributed_from_the_numerator(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    """The server computes the share so the console never divides (C7)."""
    _gateway_call(audit_engine, cost="3.0", kpi_tag="renewal-retention", day=5)
    _gateway_call(audit_engine, cost="1.0", kpi_tag=None, day=6)

    spend = collect_spend(audit_engine, JUNE)

    assert spend.attributed_usd == 3.0
    assert spend.unattributed_usd == 1.0
    assert spend.total_usd == 4.0
    assert spend.attributed_share == 0.75


def test_attributed_share_is_none_when_total_spend_is_zero(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    """No denominator is not 100% attributed. A private-lane-only window prices
    every call at $0.00, and reporting "100% attributed" off a 0/0 would be a
    fabricated verdict."""
    _gateway_call(audit_engine, cost="0.0", kpi_tag="renewal-retention", day=5)
    _gateway_call(audit_engine, cost="0.0", kpi_tag=None, day=6)

    spend = collect_spend(audit_engine, JUNE)

    assert spend.total_usd == 0.0
    assert spend.attributed_share is None


def test_outcomes_are_ordered_by_tag_so_the_report_is_deterministic(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    _gateway_call(audit_engine, cost="1.0", kpi_tag="zeta-outcome", day=5)
    _gateway_call(audit_engine, cost="1.0", kpi_tag="alpha-outcome", day=6)
    _gateway_call(audit_engine, cost="1.0", kpi_tag="mid-outcome", day=7)

    spend = collect_spend(audit_engine, JUNE)

    assert [o.kpi_tag for o in spend.outcomes] == [
        "alpha-outcome",
        "mid-outcome",
        "zeta-outcome",
    ]


def test_streaming_calls_are_metered_alongside_completions(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    _gateway_call(
        audit_engine, cost="1.0", kpi_tag="renewal-retention", day=5, action="ne.gateway.stream"
    )
    _gateway_call(
        audit_engine, cost="1.0", kpi_tag="renewal-retention", day=6, action="ne.gateway.complete"
    )

    spend = collect_spend(audit_engine, JUNE)

    assert spend.outcomes[0].calls == 2
    assert spend.outcomes[0].cost_usd == 2.0


def test_non_gateway_audit_rows_contribute_no_spend(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    """A tool execution carries no model cost; counting it would invent spend."""
    seed_audit_event(
        audit_engine,
        action="tool.execute",
        actor="user:ada@acme.com",
        occurred_at=datetime(2026, 6, 5, tzinfo=UTC),
        provenance={"cost_usd": "99.0"},
        kpi_tag="renewal-retention",
    )

    spend = collect_spend(audit_engine, JUNE)

    assert spend.outcomes == ()
    assert spend.total_usd == 0.0
    assert spend.attributed_calls == 0


def test_calls_outside_the_window_are_not_counted(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    _gateway_call(audit_engine, cost="1.0", kpi_tag="renewal-retention", day=5)
    seed_audit_event(
        audit_engine,
        action="ne.gateway.complete",
        actor="user:ada@acme.com",
        occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
        provenance={"cost_usd": "50.0"},
        kpi_tag="renewal-retention",
    )

    spend = collect_spend(audit_engine, JUNE)

    assert spend.total_usd == 1.0


def test_an_empty_window_reports_zeros_and_no_outcomes(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    spend = collect_spend(audit_engine, JUNE)

    assert spend.outcomes == ()
    assert spend.total_usd == 0.0
    assert spend.unattributed_usd == 0.0
    assert spend.unattributed_calls == 0
    assert spend.attributed_share is None
