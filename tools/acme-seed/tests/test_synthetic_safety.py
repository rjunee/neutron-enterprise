# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Belt-and-braces RFC-2606: every email that leaves the seeder is ``@*.example``.

Re-asserts the tools/acme corpus's synthetic guarantee over the ACTUAL outbound
request bodies of all three seeders — no real-looking domain can leave the tool.
"""

from __future__ import annotations

import asyncio
import re

import httpx

from ne.acme_seed.m365 import M365Seeder
from ne.acme_seed.personas import PrincipalsMap
from ne.acme_seed.salesforce import SalesforceSeeder
from ne.acme_seed.slack import SlackSeeder
from ne.acme_seed.slice import slice_corpus

from .conftest import (
    build_m365_transport,
    build_salesforce_transport,
    build_slack_transport,
    full_principals,
    m365_settings,
    salesforce_settings,
    slack_settings,
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")


def _record_all() -> list[httpx.Request]:
    principals: PrincipalsMap = full_principals()
    recorder: list[httpx.Request] = []

    asyncio.run(
        M365Seeder(
            m365_settings(), principals, transport=build_m365_transport(recorder=recorder)
        ).push(slice_corpus())
    )
    asyncio.run(
        SalesforceSeeder(
            salesforce_settings(), transport=build_salesforce_transport(recorder=recorder)
        ).push(slice_corpus())
    )
    asyncio.run(
        SlackSeeder(
            slack_settings(), principals, transport=build_slack_transport(recorder=recorder)
        ).push(slice_corpus())
    )
    return recorder


def test_all_outbound_payloads_are_rfc2606_safe() -> None:
    recorder = _record_all()
    checked = 0
    for request in recorder:
        if not request.content:
            continue
        body = request.content.decode("utf-8", errors="ignore")
        for raw in _EMAIL_RE.findall(body):
            match = raw.rstrip(".,;:)\"'")
            checked += 1
            assert match.endswith(".example"), f"non-synthetic email left the seeder: {match}"
    # Sanity: we actually inspected outbound bodies containing emails.
    assert checked > 0
