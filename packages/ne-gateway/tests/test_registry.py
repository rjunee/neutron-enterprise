# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Model registry: entry validation, registration, lookup, and filtering."""

from __future__ import annotations

import pytest

from ne.contracts import Lane, ModelEntry, ModelRegistry
from ne.gateway import (
    InMemoryModelRegistry,
    RegistryValidationError,
    UnknownModelIdError,
)
from ne.gateway.registry import KNOWN_CAPABILITIES, KNOWN_MODEL_LICENSES, KNOWN_ORIGINS


def make_entry(
    model_id: str = "m1",
    *,
    license: str = "MIT",
    origin: str = "US",
    lanes: tuple[Lane, ...] = (Lane.PRIVATE_VLLM,),
    capabilities: tuple[str, ...] = ("streaming",),
    context_window: int | None = 131_072,
    provider: str = "acme",
) -> ModelEntry:
    return ModelEntry(
        model_id=model_id,
        provider=provider,
        license=license,
        origin=origin,
        lanes=lanes,
        capabilities=capabilities,
        context_window=context_window,
    )


def test_registry_conforms_to_protocol() -> None:
    reg: ModelRegistry = InMemoryModelRegistry()
    entry = make_entry()
    reg.register(entry)
    assert reg.get("m1") == entry
    assert reg.list_models() == (entry,)


def test_register_and_get_roundtrip() -> None:
    reg = InMemoryModelRegistry()
    entry = make_entry("roundtrip")
    reg.register(entry)
    assert reg.get("roundtrip") == entry


def test_get_unknown_id_lists_registered() -> None:
    reg = InMemoryModelRegistry([make_entry("a"), make_entry("b")])
    with pytest.raises(UnknownModelIdError) as exc_info:
        reg.get("ghost")
    msg = str(exc_info.value)
    assert "ghost" in msg
    assert "'a'" in msg
    assert "'b'" in msg


def test_duplicate_model_id_rejected() -> None:
    reg = InMemoryModelRegistry()
    reg.register(make_entry("dup"))
    with pytest.raises(RegistryValidationError) as exc_info:
        reg.register(make_entry("dup"))
    msg = str(exc_info.value)
    assert "dup" in msg
    assert "already registered" in msg


def test_empty_license_rejected() -> None:
    reg = InMemoryModelRegistry()
    with pytest.raises(RegistryValidationError) as exc_info:
        reg.register(make_entry("bad-license", license=""))
    msg = str(exc_info.value)
    assert "bad-license" in msg
    assert "license" in msg


@pytest.mark.parametrize("license", ["CC-BY-NC", "cc-by-nc-4.0"])
def test_banned_license_rejected_cites_c4(license: str) -> None:
    reg = InMemoryModelRegistry()
    with pytest.raises(RegistryValidationError) as exc_info:
        reg.register(make_entry("banned", license=license))
    msg = str(exc_info.value)
    assert "banned" in msg
    assert "C4" in msg


def test_unknown_license_rejected_lists_known() -> None:
    reg = InMemoryModelRegistry()
    with pytest.raises(RegistryValidationError) as exc_info:
        reg.register(make_entry("wtfpl", license="WTFPL"))
    msg = str(exc_info.value)
    for lic in sorted(KNOWN_MODEL_LICENSES):
        assert lic in msg


def test_unknown_origin_rejected_lists_known() -> None:
    reg = InMemoryModelRegistry()
    with pytest.raises(RegistryValidationError) as exc_info:
        reg.register(make_entry("badorigin", origin="XX"))
    msg = str(exc_info.value)
    for origin in sorted(KNOWN_ORIGINS):
        assert origin in msg


def test_empty_lanes_rejected() -> None:
    reg = InMemoryModelRegistry()
    with pytest.raises(RegistryValidationError) as exc_info:
        reg.register(make_entry("nolanes", lanes=()))
    msg = str(exc_info.value)
    assert "nolanes" in msg
    assert "at least one lane" in msg


def test_proprietary_api_license_barred_from_private_vllm_lane() -> None:
    reg = InMemoryModelRegistry()
    with pytest.raises(RegistryValidationError):
        reg.register(
            make_entry(
                "frontier-only",
                license="proprietary-api",
                lanes=(Lane.PRIVATE_VLLM,),
            )
        )
    # Same license with a frontier-api lane is accepted.
    reg.register(
        make_entry(
            "frontier-ok",
            license="proprietary-api",
            lanes=(Lane.FRONTIER_API,),
        )
    )
    assert reg.get("frontier-ok").license == "proprietary-api"


def test_unknown_capability_rejected_lists_known() -> None:
    reg = InMemoryModelRegistry()
    with pytest.raises(RegistryValidationError) as exc_info:
        reg.register(make_entry("telepath", capabilities=("telepathy",)))
    msg = str(exc_info.value)
    assert "telepathy" in msg
    for cap in sorted(KNOWN_CAPABILITIES):
        assert cap in msg

    with pytest.raises(RegistryValidationError) as exc_info2:
        reg.register(make_entry("nocaps", capabilities=()))
    assert "capability" in str(exc_info2.value)


def test_missing_context_window_rejected() -> None:
    reg = InMemoryModelRegistry()
    with pytest.raises(RegistryValidationError) as exc_info:
        reg.register(make_entry("nowin", context_window=None))
    assert "context_window" in str(exc_info.value)

    with pytest.raises(RegistryValidationError) as exc_info2:
        reg.register(make_entry("zerowin", context_window=0))
    assert "context_window" in str(exc_info2.value)


def test_list_models_filters_by_lane() -> None:
    reg = InMemoryModelRegistry(
        [
            make_entry("private", lanes=(Lane.PRIVATE_VLLM,)),
            make_entry(
                "frontier",
                license="proprietary-api",
                lanes=(Lane.FRONTIER_API,),
            ),
        ]
    )
    private_only = reg.list_models(lane=Lane.PRIVATE_VLLM)
    assert [e.model_id for e in private_only] == ["private"]

    all_models = reg.list_models()
    assert [e.model_id for e in all_models] == ["private", "frontier"]


def test_list_models_filters_by_capability() -> None:
    reg = InMemoryModelRegistry(
        [
            make_entry("reasoner", capabilities=("streaming", "reasoning")),
            make_entry("plain", capabilities=("streaming",)),
        ]
    )
    reasoning_only = reg.list_models(capability="reasoning")
    assert [e.model_id for e in reasoning_only] == ["reasoner"]


def test_filter_models_by_origin_and_license() -> None:
    from ne.gateway.menus import DEFAULT_MENUS, MENU_CAPABILITY_FIRST, MENU_WESTERN

    reg = InMemoryModelRegistry(
        [*DEFAULT_MENUS[MENU_CAPABILITY_FIRST], *DEFAULT_MENUS[MENU_WESTERN]]
    )

    cn_ids = {e.model_id for e in reg.filter_models(origin="CN")}
    assert cn_ids == {e.model_id for e in DEFAULT_MENUS[MENU_CAPABILITY_FIRST]}

    mit_ids = {e.model_id for e in reg.filter_models(license="MIT")}
    assert mit_ids == {"glm-5.2", "deepseek-v4-flash"}

    combined = {e.model_id for e in reg.filter_models(origin="CN", license="Apache-2.0")}
    assert combined == {"qwen3.5-397b-a17b", "qwen3.6-35b-a3b"}
