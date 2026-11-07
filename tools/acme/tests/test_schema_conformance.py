# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The generated corpus conforms to the frozen ne-schema (NE-004) models."""

from __future__ import annotations

import json
from pathlib import Path

from ne.schema import OperationalRecordSet

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "acme-v1"


def _load_record_set() -> OperationalRecordSet:
    obj = json.loads((FIXTURES / "records.json").read_bytes())
    return OperationalRecordSet.model_validate(obj)


def test_record_set_validates() -> None:
    record_set = _load_record_set()
    assert record_set.people is not None and len(record_set.people) == 40
    assert record_set.accounts is not None and len(record_set.accounts) == 120
    assert record_set.documents is not None and len(record_set.documents) == 500
    assert record_set.tickets is not None and len(record_set.tickets) == 50
    assert record_set.orders is not None and len(record_set.orders) == 80


def test_ids_unique_per_collection() -> None:
    record_set = _load_record_set()
    for collection in (
        record_set.accounts,
        record_set.people,
        record_set.documents,
        record_set.tickets,
        record_set.orders,
    ):
        assert collection is not None
        ids = [entity.id for entity in collection]
        assert len(ids) == len(set(ids)), "duplicate id found in collection"
