# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""bootstrap + verify_model_pin — fail-closed on server-side model drift."""

from __future__ import annotations

import asyncio
import copy
import io
from typing import Any

import pytest

from ne.permissions_rebac.bootstrap import (
    _without_unset_defaults,
    bootstrap,
    load_model,
    main,
    verify_model_pin,
)
from ne.permissions_rebac.config import RebacSettings
from ne.permissions_rebac.errors import ModelPinError
from ne.permissions_rebac.testing.fakes import InMemoryOpenFga


def _settings() -> RebacSettings:
    return RebacSettings(enabled=True, store_id="s1", authorization_model_id="m1")


def _sdk_roundtrip(model: dict[str, Any]) -> dict[str, Any]:
    """model dict -> SDK AuthorizationModel -> to_dict(serialize=True).

    Exactly the deserializer the live client's ``read_authorization_model``
    response passes through — used here to lock the fix's behavior against
    the REAL SDK shape without needing a live server for every run.
    """
    from openfga_sdk.api_client import ApiClient
    from openfga_sdk.configuration import Configuration

    client = ApiClient.__new__(ApiClient)  # skip network setup; only the deserializer is used
    client.configuration = Configuration(api_url="http://127.0.0.1:8080")
    deserialize = getattr(client, "_ApiClient__deserialize")  # noqa: B009 — name-mangled private; the exact deserializer read_authorization_model responses pass through
    m = deserialize({**model, "id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}, "AuthorizationModel")
    return dict(m.to_dict(serialize=True))


def _none_to_empty_string(obj: Any) -> Any:
    """Mirror the live server's observed proto3 "" defaults instead of SDK-local None."""
    if isinstance(obj, dict):
        return {k: _none_to_empty_string(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_none_to_empty_string(v) for v in obj]
    if obj is None:
        return ""
    return obj


def test_verify_passes_on_verbatim_model() -> None:
    client = InMemoryOpenFga(model=load_model())
    asyncio.run(verify_model_pin(client, _settings()))  # no raise


def test_verify_passes_when_type_and_relation_order_differs() -> None:
    model = load_model()
    model["type_definitions"] = list(reversed(model["type_definitions"]))
    client = InMemoryOpenFga(model=model)
    asyncio.run(verify_model_pin(client, _settings()))  # normalization -> no raise


def test_verify_raises_on_extra_relation_drift() -> None:
    model = copy.deepcopy(load_model())
    document = next(td for td in model["type_definitions"] if td["type"] == "document")
    document["relations"]["editor"] = {"this": {}}  # drift
    client = InMemoryOpenFga(model=model)
    with pytest.raises(ModelPinError):
        asyncio.run(verify_model_pin(client, _settings()))


def test_verify_raises_on_rewritten_viewer() -> None:
    model = copy.deepcopy(load_model())
    document = next(td for td in model["type_definitions"] if td["type"] == "document")
    document["relations"]["viewer"] = {"this": {}}  # dropped the union
    client = InMemoryOpenFga(model=model)
    with pytest.raises(ModelPinError):
        asyncio.run(verify_model_pin(client, _settings()))


def test_verify_passes_on_sdk_default_injected_model() -> None:
    """D2 fix, fast lock: SDK-round-tripped (null-default-injected) undrifted model passes."""
    client = InMemoryOpenFga(model=_sdk_roundtrip(load_model()))
    asyncio.run(verify_model_pin(client, _settings()))  # no raise


def test_verify_passes_when_sdk_defaults_are_empty_strings() -> None:
    """Same, under the live server's observed proto3 shape (nulls appear as "")."""
    model = _none_to_empty_string(_sdk_roundtrip(load_model()))
    client = InMemoryOpenFga(model=model)
    asyncio.run(verify_model_pin(client, _settings()))  # no raise


def test_verify_passes_when_sdk_map_defaults_are_empty_dicts() -> None:
    """Mirrors the REAL server's observed shape for empty map-typed fields.

    OBSERVED (2026-07-24, live docker-compose ``openfga/openfga:v1.18.1``): a
    type with no relations round-trips as ``"relations": {}`` (not ``null``), and
    a model with no conditions round-trips as top-level ``"conditions": {}`` (not
    ``null``) — diverging from the LOCAL ``ApiClient.__deserialize`` round-trip
    used by ``_sdk_roundtrip`` below, which gives ``null`` for both (this exact
    divergence broke ``verify_model_pin`` against the real server during this
    unit's own build, caught only by the live suite, not by the fake-lane tests
    above — this test locks the fix against that real shape without needing a
    live server for every run). ``_without_unset_defaults`` must strip these
    non-marker raw ``{}`` fields exactly like it strips ``null``.
    """
    model = _sdk_roundtrip(load_model())
    model["conditions"] = {}
    user_td = next(td for td in model["type_definitions"] if td["type"] == "user")
    user_td["relations"] = {}
    client = InMemoryOpenFga(model=model)
    asyncio.run(verify_model_pin(client, _settings()))  # no raise


def test_verify_raises_on_drift_even_after_sdk_round_trip() -> None:
    """Unit-level non-vacuity: a drifted model still raises after SDK default injection."""
    model = copy.deepcopy(load_model())
    document = next(td for td in model["type_definitions"] if td["type"] == "document")
    document["relations"]["editor"] = {"this": {}}
    document["metadata"]["relations"]["editor"] = {
        "directly_related_user_types": [{"type": "user"}]
    }
    client = InMemoryOpenFga(model=_sdk_roundtrip(model))
    with pytest.raises(ModelPinError):
        asyncio.run(verify_model_pin(client, _settings()))


def test_strip_drops_raw_empty_containers_except_presence_markers() -> None:
    """Locks the "unset default" strip rule vs. the presence-marker keep rule.

    Raw ``None``/``""``/``[]`` are ALWAYS stripped (unambiguous SDK/proto3
    unset-default shapes). Raw ``{}`` is stripped for EVERY key EXCEPT the two
    OpenFGA presence-marker field names, where an earlier version of this fix was
    found (by review) to be vacuous: ``"this"`` (``Userset`` direct rewrite) and
    ``"wildcard"`` (``RelationReference`` ``user:*`` / PUBLIC). Every other raw
    ``{}`` — e.g. ``"relations": {}`` or top-level ``"conditions": {}``, both
    OBSERVED on the real server for a model with zero relations/conditions — means
    "empty container", semantically identical to the field being absent, and
    strips like any other unset default. ``False``/``0`` survive either way.
    """
    result = _without_unset_defaults(
        {
            "a": None,
            "b": "",
            "c": [],
            "relations": {},  # non-marker raw {} -> stripped (empty map == absent)
            "conditions": {},  # non-marker raw {} -> stripped
            "this": {},  # presence marker -> kept
            "wildcard": {},  # presence marker -> kept
            "f": 0,
            "g": False,
        }
    )
    assert result == {"this": {}, "wildcard": {}, "f": 0, "g": False}


def test_verify_raises_on_wildcard_widening_even_after_sdk_round_trip() -> None:
    """Non-vacuity: presence-semantic `wildcard: {}` widening (added public access) still raises.

    ``RelationReference.wildcard`` is, like ``Userset.this``, a zero-field OpenFGA
    object whose mere PRESENCE (not content) is the signal —
    ``{"type": "user", "wildcard": {}}`` means ``user:*`` (anyone) is directly
    related, vs ``{"type": "user"}`` (only named users). Unlike ``this``, ``wildcard``
    sits as a SIBLING key inside its own dict (alongside ``type``/``relation``/
    ``condition``), so no "parent survives via an added key" shortcut applies to it:
    this test locks that ``_without_unset_defaults`` never treats a raw ``{}`` as
    strippable, so an added wildcard cannot silently compare equal to its absence.
    """
    model = copy.deepcopy(load_model())
    document = next(td for td in model["type_definitions"] if td["type"] == "document")
    refs = document["metadata"]["relations"]["viewer"]["directly_related_user_types"]
    refs[0] = {**refs[0], "wildcard": {}}  # widen user -> user:* (PUBLIC)
    client = InMemoryOpenFga(model=_sdk_roundtrip(model))
    with pytest.raises(ModelPinError):
        asyncio.run(verify_model_pin(client, _settings()))


def test_verify_raises_on_empty_pin_while_enabled() -> None:
    client = InMemoryOpenFga(model=load_model())
    settings = RebacSettings()  # enabled=False, empty model id
    with pytest.raises(ModelPinError):
        asyncio.run(verify_model_pin(client, settings))


def test_bootstrap_returns_store_and_model_ids() -> None:
    client = InMemoryOpenFga()
    store_id, model_id = asyncio.run(bootstrap(client))
    assert store_id == "store-neutron"
    assert model_id == "model-fake-1"
    assert client.model == load_model()


def test_bootstrap_cli_main_prints_ids_and_toml_snippet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = InMemoryOpenFga()
    seen: dict[str, str] = {}

    def _factory(api_url: str, api_token: str) -> InMemoryOpenFga:
        seen["api_url"] = api_url
        seen["api_token"] = api_token
        return fake

    # Token comes ONLY from env (never argv — C12).
    monkeypatch.setenv("NE_REBAC_API_TOKEN", "op-token")
    buf = io.StringIO()
    rc = main(
        ["--api-url", "http://127.0.0.1:8080", "--store-name", "neutron"],
        client_factory=_factory,
        out=buf,
    )
    assert rc == 0
    out = buf.getvalue()
    assert "store_id=store-neutron" in out
    assert "authorization_model_id=model-fake-1" in out
    # A ready-to-paste ne-rebac.toml snippet with the resolved ids.
    assert "enabled = true" in out
    assert 'store_id = "store-neutron"' in out
    assert 'authorization_model_id = "model-fake-1"' in out
    assert 'api_url = "http://127.0.0.1:8080"' in out
    # The injected client received the env token, not an argv one.
    assert seen["api_token"] == "op-token"
    assert fake.closed is True  # the CLI closes the client in a finally
    assert fake.model == load_model()
