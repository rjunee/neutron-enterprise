# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""M365ConnectorSettings validation — early, clear failures (FIX-C)."""

from __future__ import annotations

import pydantic
import pytest

from ne.connect.m365.contract import m365_data_contract

from .m365_transport import m365_settings


def test_empty_owner_rejected_at_settings_validation() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        m365_settings(owner="")
    assert "owner" in str(excinfo.value)


def test_whitespace_owner_rejected() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        m365_settings(owner="   ")
    assert "owner" in str(excinfo.value)


def test_valid_settings_accept() -> None:
    settings = m365_settings()
    # The early check matches the late consumer: the ODCS contract carries it.
    assert m365_data_contract(settings).owner == "data-eng@contoso.test"
