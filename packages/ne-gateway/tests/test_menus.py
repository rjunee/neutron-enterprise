# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Named default model menus (capability-first + Western alternate)."""

from __future__ import annotations

import pytest

from ne.contracts import Lane, ModelEntry
from ne.gateway import DEFAULT_MENUS, MENU_CAPABILITY_FIRST, MENU_WESTERN, load_menu
from ne.gateway.errors import RegistryValidationError
from ne.gateway.registry import WESTERN_ORIGINS


def test_both_default_menus_load_and_validate() -> None:
    cap_first = load_menu(MENU_CAPABILITY_FIRST)
    western = load_menu(MENU_WESTERN)
    assert len(cap_first.list_models()) == 4
    assert len(western.list_models()) == 2


def test_capability_first_menu_contents() -> None:
    reg = load_menu(MENU_CAPABILITY_FIRST)
    entries = reg.list_models()
    ids = {e.model_id for e in entries}
    assert ids == {
        "glm-5.2",
        "qwen3.5-397b-a17b",
        "qwen3.6-35b-a3b",
        "deepseek-v4-flash",
    }
    for entry in entries:
        assert entry.license in {"MIT", "Apache-2.0"}
        assert entry.lanes == (Lane.PRIVATE_VLLM,)


def test_western_menu_has_no_cn_origin() -> None:
    reg = load_menu(MENU_WESTERN)
    entries = reg.list_models()
    ids = {e.model_id for e in entries}
    assert ids == {"mistral-large-3", "gpt-oss-120b"}
    for entry in entries:
        assert entry.origin in WESTERN_ORIGINS
        assert entry.origin != "CN"


def test_unknown_menu_name_lists_available() -> None:
    with pytest.raises(RegistryValidationError) as exc_info:
        load_menu("nope")
    msg = str(exc_info.value)
    assert "['capability-first', 'western']" in msg


def test_menu_entries_are_frozen_contract_type() -> None:
    for entries in DEFAULT_MENUS.values():
        for entry in entries:
            assert isinstance(entry, ModelEntry)
