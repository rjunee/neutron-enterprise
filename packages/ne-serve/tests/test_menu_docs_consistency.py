# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Three-way consistency: docs/model-menu.md <-> ne.gateway.menus <-> MODEL_SOURCES.

ne.gateway is a TEST-ONLY (dev-group) dependency here — imported only to assert
the doc/table/registry stay in lockstep. ne.serve runtime never imports it.
"""

from __future__ import annotations

from pathlib import Path

from ne.gateway.menus import DEFAULT_MENUS
from ne.serve.models import MODEL_SOURCES, TINY_CI_SERVED_NAME, TINY_CI_SOURCE

REPO_ROOT = Path(__file__).resolve().parents[3]
MENU_DOC = REPO_ROOT / "docs" / "model-menu.md"

# Column order of the machine-parsed main menu table.
_COLUMNS = (
    "model_id",
    "menu",
    "provider",
    "hf_repo",
    "license",
    "origin",
    "context_window",
    "hardware_tier",
)


def _parse_menu_rows() -> list[dict[str, str]]:
    """Parse the main menu table: rows whose first cell is a known model_id."""
    known = set(MODEL_SOURCES)
    rows: list[dict[str, str]] = []
    for line in MENU_DOC.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) != len(_COLUMNS):
            continue
        if cells[0] not in known:
            continue
        rows.append(dict(zip(_COLUMNS, cells, strict=True)))
    return rows


def _menu_entries() -> dict[str, tuple[str, object]]:
    """Map model_id -> (menu_name, ModelEntry) across all default menus."""
    out: dict[str, tuple[str, object]] = {}
    for menu_name, entries in DEFAULT_MENUS.items():
        for entry in entries:
            out[entry.model_id] = (menu_name, entry)
    return out


def test_model_id_sets_are_identical_three_ways() -> None:
    doc_ids = {row["model_id"] for row in _parse_menu_rows()}
    menu_ids = set(_menu_entries())
    source_ids = set(MODEL_SOURCES)
    assert doc_ids == menu_ids == source_ids


def test_doc_rows_match_registry_and_sources() -> None:
    entries = _menu_entries()
    for row in _parse_menu_rows():
        mid = row["model_id"]
        menu_name, entry = entries[mid]
        source = MODEL_SOURCES[mid]

        # Doc row <-> ModelEntry.
        assert row["menu"] == menu_name, mid
        assert row["provider"] == entry.provider, mid  # type: ignore[attr-defined]
        assert row["license"] == entry.license, mid  # type: ignore[attr-defined]
        assert row["origin"] == entry.origin, mid  # type: ignore[attr-defined]
        assert int(row["context_window"]) == entry.context_window, mid  # type: ignore[attr-defined]

        # Doc row <-> MODEL_SOURCES.
        assert row["hf_repo"] == source.repo_id, mid
        assert row["license"] == source.license, mid
        assert row["origin"] == source.origin, mid


def test_sources_match_registry_license_and_origin() -> None:
    entries = _menu_entries()
    for mid, source in MODEL_SOURCES.items():
        _, entry = entries[mid]
        assert source.license == entry.license, mid  # type: ignore[attr-defined]
        assert source.origin == entry.origin, mid  # type: ignore[attr-defined]


def test_tiny_ci_model_is_not_in_menu_or_sources() -> None:
    assert TINY_CI_SERVED_NAME not in MODEL_SOURCES
    assert TINY_CI_SERVED_NAME not in _menu_entries()
    assert TINY_CI_SOURCE.repo_id not in {s.repo_id for s in MODEL_SOURCES.values()}
    # And it does not appear as a row in the main (machine-parsed) menu table.
    assert TINY_CI_SERVED_NAME not in {row["model_id"] for row in _parse_menu_rows()}
