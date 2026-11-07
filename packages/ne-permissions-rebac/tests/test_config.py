# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""RebacSettings defaults + fail-closed enabled-requires-pins validation."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from ne.permissions_rebac.config import RebacSettings


def test_defaults() -> None:
    s = RebacSettings()
    assert s.enabled is False
    assert s.unavailable_mode == "degrade"
    assert s.max_shared_documents == 10_000
    assert s.timeout_s == 3.0
    assert isinstance(s.api_token, SecretStr)
    # 8080 is the HTTP API port the SDK speaks (8081 is gRPC). Wiring MUST use it.
    assert s.api_url == "http://openfga:8080"


def test_env_prefix_drives_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # NE-CLEAN4 regression: RebacSettings overrides the base NE_ env prefix, so
    # NE_REBAC_* env drives the model (without the override the O-4 compose wiring
    # would be silently ignored — the FIX-1 latent bug this unit closes).
    monkeypatch.setenv("NE_REBAC_ENABLED", "true")
    monkeypatch.setenv("NE_REBAC_STORE_ID", "01STORE")
    monkeypatch.setenv("NE_REBAC_AUTHORIZATION_MODEL_ID", "01MODEL")
    monkeypatch.setenv("NE_REBAC_API_URL", "http://openfga:8080")
    monkeypatch.setenv("NE_REBAC_API_TOKEN", "shhh")
    s = RebacSettings()
    assert s.enabled is True
    assert s.store_id == "01STORE"
    assert s.authorization_model_id == "01MODEL"
    assert s.api_url == "http://openfga:8080"
    assert s.api_token.get_secret_value() == "shhh"


def test_enabled_requires_store_and_model_id() -> None:
    with pytest.raises(ValidationError):
        RebacSettings(enabled=True)  # empty store_id + model id
    with pytest.raises(ValidationError):
        RebacSettings(enabled=True, store_id="s1")  # empty model id
    with pytest.raises(ValidationError):
        RebacSettings(enabled=True, authorization_model_id="m1")  # empty store id
    # both present -> ok
    s = RebacSettings(enabled=True, store_id="s1", authorization_model_id="m1")
    assert s.enabled is True


def test_api_token_is_masked() -> None:
    s = RebacSettings(api_token=SecretStr("super-secret"))
    assert "super-secret" not in repr(s)
    assert "super-secret" not in str(s.model_dump())
    assert s.api_token.get_secret_value() == "super-secret"
