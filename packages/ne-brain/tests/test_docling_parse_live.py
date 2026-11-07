# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""DoclingParser LIVE acceptance test against the real isolated parser venv.

Skipped (LIVE-PARKED) unless the isolated parser venv already exists at
packages/ne-brain/.parser-venv, or NE_BUILD_PARSER_ENV=1 is set (in which case
this module builds it). To run for real:

    NE_BUILD_PARSER_ENV=1 uv run pytest packages/ne-brain/tests/test_docling_parse_live.py -q

Proves the committed sample_report.parsed.json fixture is an honest recording
of a real docling 2.110.0 run: this test re-parses sample_report.html with the
REAL DoclingParser and asserts stable fields (block kinds sequence, heading
texts, table content) match the committed fixture.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from ne.brain.ingest.docling_parser import DoclingParser, ensure_env, parsed_document_from_json

_PKG_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[3]
_VENV_PYTHON = _PKG_ROOT / ".parser-venv" / "bin" / "python"
_FIXTURES = Path(__file__).parent / "fixtures" / "ingest"

_BUILD_ENV = os.environ.get("NE_BUILD_PARSER_ENV") == "1"

if not _VENV_PYTHON.is_file() and not _BUILD_ENV:
    pytest.skip(
        "LIVE-PARKED: real-docling parse pending — build the isolated parser venv and "
        "re-run `NE_BUILD_PARSER_ENV=1 uv run pytest "
        "packages/ne-brain/tests/test_docling_parse_live.py -q` "
        f"({_VENV_PYTHON} missing)",
        allow_module_level=True,
    )

if _BUILD_ENV and not _VENV_PYTHON.is_file():
    ensure_env(_REPO_ROOT)


def test_real_docling_parse_matches_committed_fixture() -> None:
    parser = DoclingParser(_REPO_ROOT)
    html_path = _FIXTURES / "sample_report.html"

    live_doc = asyncio.run(parser.parse(html_path, document_id="sample-report"))
    committed_doc = parsed_document_from_json(
        (_FIXTURES / "sample_report.parsed.json").read_bytes()
    )

    assert [b.kind for b in live_doc.blocks] == [b.kind for b in committed_doc.blocks]
    assert [b.text for b in live_doc.blocks if b.kind == "heading"] == [
        b.text for b in committed_doc.blocks if b.kind == "heading"
    ]

    heading_blocks = [b for b in live_doc.blocks if b.kind == "heading"]
    assert len(heading_blocks) >= 4  # title + Executive Summary + Traffic... + Regional... + ...

    table_blocks = [b for b in live_doc.blocks if b.kind == "table"]
    assert len(table_blocks) == 1
    table_text = table_blocks[0].text
    assert "Region" in table_text
    assert "Avg Latency (ms)" in table_text
    assert "Uptime (%)" in table_text

    paragraph_blocks = [b for b in live_doc.blocks if b.kind == "paragraph"]
    assert len(paragraph_blocks) >= 1

    # HTML has no page provenance.
    assert all(b.page is None for b in live_doc.blocks)
