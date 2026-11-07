# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared committed-fixture loaders for Meridian corpus tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ne.meridian.models import GroundTruth, MeridianCorpus
from ne.schema import OperationalRecordSet

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "meridian-v1"


@pytest.fixture(scope="session")
def corpus() -> MeridianCorpus:
    return MeridianCorpus.model_validate_json((FIXTURES / "banking.json").read_bytes())


@pytest.fixture(scope="session")
def ground_truth() -> GroundTruth:
    return GroundTruth.model_validate_json((FIXTURES / "ground_truth.json").read_bytes())


@pytest.fixture(scope="session")
def records() -> OperationalRecordSet:
    return OperationalRecordSet.model_validate_json((FIXTURES / "records.json").read_bytes())


@pytest.fixture(scope="session")
def manifest() -> dict[str, object]:
    return json.loads((FIXTURES / "manifest.json").read_bytes())
