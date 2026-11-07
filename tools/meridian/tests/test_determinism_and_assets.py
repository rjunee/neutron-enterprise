# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Byte identity, structure, hashes, and freshness for every Meridian artifact."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from zipfile import ZIP_STORED, ZipFile

from ne.meridian.constants import MERIDIAN_DECLARATION
from ne.meridian.generator import DEFAULT_SEED, check_fixture, generate, serialize

from .conftest import FIXTURES


def _files(seed: int = DEFAULT_SEED) -> dict[str, bytes]:
    records, corpus, truth, assets = generate(MERIDIAN_DECLARATION, seed=seed)
    return serialize(records, corpus, truth, assets, seed=seed)


def test_two_runs_are_byte_identical_including_binary_assets() -> None:
    first = _files()
    second = _files()
    assert first.keys() == second.keys()
    assert all(first[name] == second[name] for name in first)
    assert (
        first["assets/debit-card-dispute-handling-sop-v3.docx"]
        == second["assets/debit-card-dispute-handling-sop-v3.docx"]
    )
    assert first["assets/reg-dd-disclosure-2026.pdf"] == second["assets/reg-dd-disclosure-2026.pdf"]


def test_seed_changes_structured_source_but_not_policy_binary() -> None:
    default = _files(DEFAULT_SEED)
    alternate = _files(DEFAULT_SEED + 1)
    assert default["banking.json"] != alternate["banking.json"]
    assert (
        default["assets/debit-card-dispute-handling-sop-v4.docx"]
        == alternate["assets/debit-card-dispute-handling-sop-v4.docx"]
    )


def test_docx_is_fixed_order_stored_ooxml_with_load_bearing_version_diff() -> None:
    expected_members = [
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
        "docProps/core.xml",
        "docProps/app.xml",
    ]
    bodies: dict[str, str] = {}
    for version in ("v3", "v4"):
        path = FIXTURES / "assets" / f"debit-card-dispute-handling-sop-{version}.docx"
        with ZipFile(path) as archive:
            assert archive.namelist() == expected_members
            assert all(info.compress_type == ZIP_STORED for info in archive.infolist())
            assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
            for member in expected_members:
                ET.fromstring(archive.read(member))
            bodies[version] = archive.read("word/document.xml").decode()

    assert "within 5 business days" not in bodies["v3"]
    assert "within 5 business days" in bodies["v4"]
    assert "at least $500" in bodies["v4"]
    assert "signed written statement before beginning" in bodies["v3"]
    assert "oral notice starts the investigation" in bodies["v4"]


def test_pdf_xref_offsets_resolve_to_the_declared_objects() -> None:
    body = (FIXTURES / "assets" / "reg-dd-disclosure-2026.pdf").read_bytes()
    assert body.startswith(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    startxref = int(re.search(rb"startxref\n(\d+)\n%%EOF\n$", body).group(1))  # type: ignore[union-attr]
    assert body[startxref:].startswith(b"xref\n")
    xref_lines = body[startxref:].splitlines()
    assert xref_lines[1] == b"0 7"
    for object_number, entry in enumerate(xref_lines[3:9], start=1):
        offset = int(entry[:10])
        assert body[offset:].startswith(f"{object_number} 0 obj\n".encode())


def test_manifest_hashes_every_source_and_asset_and_fixture_is_fresh(
    manifest: dict[str, object],
) -> None:
    hashes = manifest["sha256"]
    assert isinstance(hashes, dict)
    expected = {
        path.relative_to(FIXTURES).as_posix()
        for path in FIXTURES.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    assert set(hashes) == expected
    for name, expected_hash in hashes.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == expected_hash
    assert check_fixture(_files(), FIXTURES) == []
