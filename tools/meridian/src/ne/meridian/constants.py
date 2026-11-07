# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Single authority block for the real FEMA declaration used by Meridian B1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class DeclarationSpec:
    declaration_id: str
    declaration_date: date
    incident: str
    counties: tuple[str, str, str]
    adjacent_county: str
    scope_description: str
    source_url: str


MERIDIAN_DECLARATION = DeclarationSpec(
    declaration_id="DR-4830",
    declaration_date=date(2024, 9, 30),
    incident="Hurricane Helene",
    counties=("Columbia", "Richmond", "Jefferson"),
    adjacent_county="McDuffie",
    scope_description=(
        "Three FEMA-designated Georgia counties selected for the Meridian pilot; "
        "not the exhaustive DR-4830 designated-area list."
    ),
    source_url="https://www.fema.gov/disaster/4830",
)
