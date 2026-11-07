# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The corpus respects the SPEC row's bounded counts exactly (this is a demo
fixture, not a load test)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from ne.schema import OperationalRecordSet

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "acme-v1"

_EXPECTED_DOC_TYPE_COUNTS = {
    "policy": 60,
    "report": 150,
    "contract": 120,
    "correspondence": 120,
    "presentation": 50,
}


def _load_record_set() -> OperationalRecordSet:
    obj = json.loads((FIXTURES / "records.json").read_bytes())
    return OperationalRecordSet.model_validate(obj)


def test_counts_within_spec() -> None:
    rs = _load_record_set()
    assert rs.documents is not None
    assert rs.tickets is not None
    assert rs.people is not None
    assert rs.accounts is not None
    assert rs.orders is not None

    assert len(rs.documents) == 500
    doc_type_counts = Counter(doc.doc_type for doc in rs.documents)
    assert dict(doc_type_counts) == _EXPECTED_DOC_TYPE_COUNTS
    assert len(doc_type_counts) == 5

    assert len(rs.tickets) == 50
    assert len(rs.people) == 40
    assert len(rs.accounts) + len(rs.orders) == 200
    assert len(rs.accounts) == 120
    assert len(rs.orders) == 80

    manifest = json.loads((FIXTURES / "manifest.json").read_bytes())
    assert manifest["counts"] == {
        "people": 40,
        "accounts": 120,
        "orders": 80,
        "documents": 500,
        "tickets": 50,
    }
