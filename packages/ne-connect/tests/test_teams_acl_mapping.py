# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Teams channel ACL mapping (NE-080) — fail-closed membershipType resolution."""

from __future__ import annotations

from ne.connect.teams.acl import CHANNEL_PREFIX, TEAM_PREFIX, map_channel_acl


def test_standard_channel_maps_to_team_label() -> None:
    outcome = map_channel_acl("T0001", {"id": "C0STD", "membershipType": "standard"})
    assert outcome.principals == ("teams-team:T0001",)
    assert not outcome.tainted
    assert outcome.dropped == ()


def test_private_channel_maps_to_channel_label() -> None:
    outcome = map_channel_acl("T0001", {"id": "C0PRIV", "membershipType": "private"})
    assert outcome.principals == ("teams-channel:T0001/C0PRIV",)
    assert not outcome.tainted


def test_shared_channel_quarantined() -> None:
    outcome = map_channel_acl("T0001", {"id": "C0SH", "membershipType": "shared"})
    assert outcome.principals == ()
    assert not outcome.tainted
    assert outcome.dropped and "shared" in outcome.dropped[0]


def test_unknown_membership_type_quarantined_fail_closed() -> None:
    outcome = map_channel_acl("T0001", {"id": "C0X", "membershipType": "unknownFutureValue"})
    assert outcome.principals == ()
    assert not outcome.tainted
    assert outcome.dropped


def test_missing_membership_type_quarantined() -> None:
    outcome = map_channel_acl("T0001", {"id": "C0X"})
    assert outcome.principals == ()
    assert outcome.dropped


def test_reserved_prefix_channel_id_tainted() -> None:
    outcome = map_channel_acl("T0001", {"id": "role:forged", "membershipType": "standard"})
    assert outcome.tainted
    assert outcome.principals == ()


def test_reserved_prefix_team_id_tainted() -> None:
    outcome = map_channel_acl("acl:tenant-all", {"id": "C0STD", "membershipType": "standard"})
    assert outcome.tainted
    assert outcome.principals == ()


def test_label_constants_byte_equal() -> None:
    assert TEAM_PREFIX == "teams-team:"
    assert CHANNEL_PREFIX == "teams-channel:"


def test_no_tenant_all_ever_minted() -> None:
    for membership in ("standard", "private", "shared", "unknownFutureValue"):
        outcome = map_channel_acl("T0001", {"id": "C0", "membershipType": membership})
        assert all(not p.startswith("acl:") for p in outcome.principals)
