# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for FK-edge and junction-table inference (``ne.schema_bootstrap.infer``)."""

from __future__ import annotations

import csv
import random
import tracemalloc
from pathlib import Path

from ne.schema_bootstrap.infer import infer_candidate_model
from ne.schema_bootstrap.profiles import profile_samples


def _class(model, name: str):
    (cls,) = [c for c in model.classes if c.name == name]
    return cls


def _slot(cls, name: str):
    (slot,) = [s for s in cls.slots if s.name == name]
    return slot


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(header)] + [",".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n")


def test_fk_edge_full_containment_creates_edge_and_ranges_slot(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "customers.csv",
        ["id", "name"],
        [["c1", "Acme"], ["c2", "Globex"], ["c3", "Initech"]],
    )
    _write_csv(
        tmp_path / "orders.csv",
        ["id", "customer_id", "amount"],
        [["o1", "c1", "100"], ["o2", "c2", "50"], ["o3", "c1", "75"]],
    )

    model = infer_candidate_model(profile_samples(tmp_path))

    orders = _class(model, "OrdersRecord")
    (edge,) = orders.edges
    assert edge.name == "customer_id"
    assert edge.source_class == "OrdersRecord"
    assert edge.target_class == "CustomersRecord"
    assert edge.via_name_heuristic is True
    assert edge.containment == 1.0

    slot = _slot(orders, "customer_id")
    assert slot.range == "CustomersRecord"


def test_fk_containment_boundary_at_95_percent_creates_edge(tmp_path: Path) -> None:
    _write_csv(tmp_path / "customers.csv", ["id"], [[f"c{i}"] for i in range(20)])
    # 19 contained + 1 not = 0.95.
    fk_values = [f"c{i}" for i in range(19)] + ["unknown"]
    _write_csv(
        tmp_path / "orders.csv",
        ["id", "customer_id"],
        [[f"o{i}", v] for i, v in enumerate(fk_values)],
    )

    model = infer_candidate_model(profile_samples(tmp_path))

    orders = _class(model, "OrdersRecord")
    assert len(orders.edges) == 1
    assert orders.edges[0].containment == 0.95


def test_fk_containment_below_threshold_rejected(tmp_path: Path) -> None:
    _write_csv(tmp_path / "customers.csv", ["id"], [[f"c{i}"] for i in range(20)])
    # 18 contained + 2 not = 0.90, below the 0.95 threshold.
    fk_values = [f"c{i}" for i in range(18)] + ["unknown1", "unknown2"]
    _write_csv(
        tmp_path / "orders.csv",
        ["id", "customer_id"],
        [[f"o{i}", v] for i, v in enumerate(fk_values)],
    )

    model = infer_candidate_model(profile_samples(tmp_path))

    orders = _class(model, "OrdersRecord")
    assert orders.edges == []
    slot = _slot(orders, "customer_id")
    assert slot.range != "CustomersRecord"


def test_no_edge_when_name_heuristic_misses_despite_full_containment(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "customers.csv",
        ["id", "name"],
        [["c1", "Acme"], ["c2", "Globex"]],
    )
    # "ref" doesn't end with "_id" -> name heuristic never fires, even though
    # every value is contained in customers.id.
    _write_csv(
        tmp_path / "orders.csv",
        ["id", "ref"],
        [["o1", "c1"], ["o2", "c2"]],
    )

    model = infer_candidate_model(profile_samples(tmp_path))

    orders = _class(model, "OrdersRecord")
    assert orders.edges == []
    assert _slot(orders, "ref").range != "CustomersRecord"


def test_fk_full_containment_holds_when_target_table_exceeds_row_cap(tmp_path: Path) -> None:
    # 2026-07-28 Argus P1: containment was measured against the SAMPLED
    # (capped) target identifier values, so a referentially-perfect FK on a
    # target table above `rows_per_table` (default 200) was silently
    # rejected — the common case, not an edge case. 5,000-row customers
    # target, 300-row orders source, EVERY customer_id genuinely valid: this
    # must still produce an edge with containment 1.0, not a ~4% false
    # rejection (200 sampled / 5000 total).
    customer_ids = [f"c{i:05d}" for i in range(5000)]
    _write_csv(
        tmp_path / "customers.csv",
        ["id", "name"],
        [[cid, f"Name{i}"] for i, cid in enumerate(customer_ids)],
    )
    rng = random.Random(0)
    order_rows = [[f"o{i}", rng.choice(customer_ids)] for i in range(300)]
    _write_csv(tmp_path / "orders.csv", ["id", "customer_id"], order_rows)

    model = infer_candidate_model(profile_samples(tmp_path))

    orders = _class(model, "OrdersRecord")
    assert len(orders.edges) == 1
    assert orders.edges[0].containment == 1.0
    assert _slot(orders, "customer_id").range == "CustomersRecord"


def test_fk_containment_still_rejects_below_threshold_against_over_cap_target(
    tmp_path: Path,
) -> None:
    # Bidirectional control for the P1 fix: proves measuring against the
    # FULL target set didn't turn containment into a rubber stamp — an
    # over-cap target table with a genuinely low-containment FK column must
    # still be rejected.
    customer_ids = [f"c{i:05d}" for i in range(5000)]
    _write_csv(
        tmp_path / "customers.csv",
        ["id", "name"],
        [[cid, f"Name{i}"] for i, cid in enumerate(customer_ids)],
    )
    # Only 50% of order rows reference a real customer id.
    order_rows = [[f"o{i}", customer_ids[i] if i % 2 == 0 else f"missing{i}"] for i in range(300)]
    _write_csv(tmp_path / "orders.csv", ["id", "customer_id"], order_rows)

    model = infer_candidate_model(profile_samples(tmp_path))

    orders = _class(model, "OrdersRecord")
    assert orders.edges == []
    assert _slot(orders, "customer_id").range != "CustomersRecord"


def test_no_edge_when_target_table_lacks_an_identifier(tmp_path: Path) -> None:
    # customers.csv has no id/uuid/key/pk-shaped unique column.
    _write_csv(
        tmp_path / "customers.csv",
        ["name", "notes"],
        [["Acme", "n1"], ["Globex", "n2"]],
    )
    _write_csv(
        tmp_path / "orders.csv",
        ["id", "customer_id"],
        [["o1", "Acme"], ["o2", "Globex"]],
    )

    model = infer_candidate_model(profile_samples(tmp_path))

    orders = _class(model, "OrdersRecord")
    assert orders.edges == []


def test_junction_positive_two_fk_columns_plus_timestamp(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "persons.csv",
        ["id", "name"],
        [["p1", "Alice"], ["p2", "Bob"], ["p3", "Carol"]],
    )
    _write_csv(
        tmp_path / "accounts.csv",
        ["id", "name"],
        [["a1", "Acme"], ["a2", "Globex"]],
    )
    _write_csv(
        tmp_path / "memberships.csv",
        ["person_id", "account_id", "joined_at"],
        [
            ["p1", "a1", "2024-01-01"],
            ["p2", "a2", "2024-01-02"],
            ["p3", "a1", "2024-01-03"],
            ["p1", "a2", "2024-01-04"],
        ],
    )

    model = infer_candidate_model(profile_samples(tmp_path))

    memberships = _class(model, "MembershipsRecord")
    assert memberships.is_junction is True
    assert memberships.junction_note is not None
    assert len(memberships.edges) == 2
    target_classes = {e.target_class for e in memberships.edges}
    assert target_classes == {"PersonsRecord", "AccountsRecord"}


def test_junction_extra_payload_column_not_flagged(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "persons.csv",
        ["id", "name"],
        [["p1", "Alice"], ["p2", "Bob"]],
    )
    _write_csv(
        tmp_path / "accounts.csv",
        ["id", "name"],
        [["a1", "Acme"], ["a2", "Globex"]],
    )
    # A third non-temporal, non-FK payload column ("role") means the
    # remaining set is no longer EXACTLY two FK columns.
    _write_csv(
        tmp_path / "memberships.csv",
        ["person_id", "account_id", "role"],
        [["p1", "a1", "owner"], ["p2", "a2", "member"], ["p1", "a2", "viewer"]],
    )

    model = infer_candidate_model(profile_samples(tmp_path))

    memberships = _class(model, "MembershipsRecord")
    assert memberships.is_junction is False
    assert memberships.junction_note is None

    # Single-FK-column negative: exactly one FK column remains after removing
    # the identifier, never exactly two -> never a junction.
    _write_csv(
        tmp_path / "assignments.csv",
        ["id", "person_id"],
        [["as1", "p1"], ["as2", "p2"]],
    )
    model2 = infer_candidate_model(profile_samples(tmp_path))
    assignments = _class(model2, "AssignmentsRecord")
    assert assignments.is_junction is False


def test_fk_pipeline_memory_bounded_with_over_cap_identifier_target(tmp_path: Path) -> None:
    # 2026-07-28 SPEC §6.1.1 arbiter ruling (Seam 1): the proving fixture for
    # the whole two-phase FK-containment redesign. A 50,000-row `customers`
    # target table (identifier column included, unlike the OLD dodging
    # fixture this replaces) plus a 300-row `orders` source table where
    # EVERY `customer_id` genuinely references a real customer. The full
    # `profile_samples` + `infer_candidate_model` pipeline must be
    # SIMULTANEOUSLY bounded (phase 2 never holds the target's full value
    # set) AND exact (containment == 1.0 — not degraded by the bound).
    n = 50_000
    customer_lines = ["id,name"] + [f"cust-{i:08d},Name{i}" for i in range(n)]
    (tmp_path / "customers.csv").write_text("\n".join(customer_lines) + "\n")

    rng = random.Random(0)
    customer_ids = [f"cust-{i:08d}" for i in range(n)]
    order_rows = [[f"o{i}", rng.choice(customer_ids)] for i in range(300)]
    _write_csv(tmp_path / "orders.csv", ["id", "customer_id"], order_rows)

    tracemalloc.start()
    try:
        bundle = profile_samples(tmp_path)
        model = infer_candidate_model(bundle)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 3_000_000, f"peak traced memory {peak} bytes — phase 2 is not bounded"

    orders = _class(model, "OrdersRecord")
    assert len(orders.edges) == 1
    assert orders.edges[0].containment == 1.0
    assert orders.edges[0].target_class == "CustomersRecord"


def test_memory_bound_would_catch_unbounded_identifier_collection(tmp_path: Path) -> None:
    # Non-vacuity control per [[vacuous-guard-assertion-shapes]]: proves the
    # fixture above is actually large enough to distinguish bounded from
    # unbounded FK-containment evidence collection — a naive full-value-set
    # collection over the SAME `customers.csv` identifier column measures
    # comfortably OVER the 3 MB bound the sibling test asserts UNDER.
    n = 50_000
    customer_lines = ["id,name"] + [f"cust-{i:08d},Name{i}" for i in range(n)]
    (tmp_path / "customers.csv").write_text("\n".join(customer_lines) + "\n")

    tracemalloc.start()
    try:
        full_values: set[str] = set()
        with (tmp_path / "customers.csv").open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader)
            idx = header.index("id")
            for row in reader:
                full_values.add(row[idx])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(full_values) == n  # the naive collection actually did its job
    assert peak > 3_000_000, (
        f"peak traced memory {peak} bytes — fixture too small to distinguish "
        "bounded from unbounded collection"
    )
