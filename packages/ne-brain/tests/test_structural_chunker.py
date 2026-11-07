# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Structure-aware chunking tests (NE-020) against the committed REAL-docling fixture."""

from __future__ import annotations

from pathlib import Path

from ne.brain.ingest.chunker import StructuralChunker
from ne.brain.ingest.contracts import BlockKind, DocBlock, ParsedDocument
from ne.brain.ingest.docling_parser import parsed_document_from_json
from ne.contracts.types import Chunk

_FIXTURES = Path(__file__).parent / "fixtures" / "ingest"


def _load(name: str) -> ParsedDocument:
    return parsed_document_from_json((_FIXTURES / name).read_bytes())


def _report_doc() -> ParsedDocument:
    return _load("sample_report.parsed.json")


def test_every_emitted_chunk_is_an_ne_contracts_chunk() -> None:
    chunks = StructuralChunker().chunk(_report_doc(), acl=("grp:a",))
    assert chunks
    for chunk in chunks:
        assert isinstance(chunk, Chunk)


def test_heading_path_in_metadata_and_text_prefix() -> None:
    chunks = StructuralChunker().chunk(_report_doc())
    exec_summary = next(c for c in chunks if c.metadata["headings"] == ("Executive Summary",))
    assert exec_summary.text.startswith("Executive Summary\n\n")

    nested = next(
        c for c in chunks if c.metadata["headings"] == ("Traffic and Latency", "Regional Breakdown")
    )
    assert nested.text.startswith("Traffic and Latency > Regional Breakdown\n\n")


def test_table_is_its_own_atomic_chunk() -> None:
    chunks = StructuralChunker().chunk(_report_doc())
    table_chunks = [c for c in chunks if c.metadata["content_type"] == "table"]
    assert len(table_chunks) == 1
    table = table_chunks[0]
    assert "Region" in table.text
    assert "Avg Latency (ms)" in table.text
    assert "Uptime (%)" in table.text
    # Never split even if it were to exceed max_chars.
    assert table.text.count("|") > 0


def test_oversized_section_splits_at_sentence_boundaries_same_headings() -> None:
    chunks = StructuralChunker(max_chars=1800).chunk(_report_doc())
    traffic_text_chunks = [
        c
        for c in chunks
        if c.metadata["headings"] == ("Traffic and Latency",)
        and c.metadata["content_type"] == "text"
    ]
    assert len(traffic_text_chunks) >= 2
    for c in traffic_text_chunks:
        # Body (after the heading prefix) never exceeds max_chars.
        body = c.text.split("\n\n", 1)[1]
        assert len(body) <= 1800
        # Splits happen at sentence boundaries: body doesn't start mid-sentence
        # (i.e. it starts with an upper-case letter or quote, not a lowercase
        # continuation), and doesn't chop a sentence in half at a hard offset.
        assert body[0].isupper() or body[0] in "\"'"


def test_chunk_id_format_and_determinism() -> None:
    doc = _report_doc()
    chunker = StructuralChunker()
    first = chunker.chunk(doc)
    second = chunker.chunk(doc)
    assert first == second
    for i, c in enumerate(first):
        assert c.chunk_id == f"sample-report:{i:04d}"
        assert c.document_id == "sample-report"


def test_acl_stamped_on_every_chunk() -> None:
    chunks = StructuralChunker().chunk(_report_doc(), acl=("grp:a",))
    assert chunks
    for c in chunks:
        assert c.acl == ("grp:a",)


def test_include_headings_in_text_false_omits_prefix() -> None:
    chunker = StructuralChunker(include_headings_in_text=False)
    chunks = chunker.chunk(_report_doc())
    exec_summary = next(c for c in chunks if c.metadata["headings"] == ("Executive Summary",))
    assert not exec_summary.text.startswith("Executive Summary\n\n")
    assert exec_summary.text.startswith("This report summarizes")


def test_min_chars_trailing_fragment_does_not_merge_across_a_heading_change() -> None:
    # A short trailing fragment under a NEW heading has no earlier chunk with
    # the SAME heading path to merge into, so it must stand on its own rather
    # than silently disappearing or merging across a heading boundary.
    big_paragraph = ("Sentence number one is here. " * 40)[:900]
    small_trailing = "Tiny tail."
    doc = ParsedDocument(
        document_id="doc-merge",
        source_name="doc-merge.md",
        source_format="md",
        blocks=(
            DocBlock(kind=BlockKind.HEADING, text="Only Section", level=1),
            DocBlock(kind=BlockKind.PARAGRAPH, text=big_paragraph),
            DocBlock(kind=BlockKind.HEADING, text="Next Section", level=1),
            DocBlock(kind=BlockKind.PARAGRAPH, text=small_trailing),
        ),
    )
    chunks = StructuralChunker(max_chars=1800, min_chars=200).chunk(doc)
    next_section = [c for c in chunks if c.metadata["headings"] == ("Next Section",)]
    assert len(next_section) == 1
    assert next_section[0].text.endswith("Tiny tail.")


def test_min_chars_trailing_fragment_merges_into_previous_same_heading_chunk() -> None:
    # A single heading section whose content is emitted in two flushes (the
    # first forced by max_chars, the second the terminal end-of-document
    # flush): the small trailing fragment must merge back into the immediately
    # preceding chunk of the SAME heading path rather than standing alone.
    sentence = "Alpha beta gamma delta epsilon zeta eta theta. "
    big_paragraph = (sentence * 30)[:990]  # <= max_chars on its own.
    small_trailing = "Tiny tail."  # << min_chars.
    max_chars = 1000
    min_chars = 200
    assert len(big_paragraph) <= max_chars
    assert len(big_paragraph) + 2 + len(small_trailing) > max_chars
    assert len(small_trailing) < min_chars

    doc = ParsedDocument(
        document_id="doc-merge-3",
        source_name="doc-merge-3.md",
        source_format="md",
        blocks=(
            DocBlock(kind=BlockKind.HEADING, text="Section A", level=1),
            DocBlock(kind=BlockKind.PARAGRAPH, text=big_paragraph),
            DocBlock(kind=BlockKind.PARAGRAPH, text=small_trailing),
        ),
    )
    chunks = StructuralChunker(max_chars=max_chars, min_chars=min_chars).chunk(doc)
    text_chunks = [c for c in chunks if c.metadata["content_type"] == "text"]
    # The two flushes merge back into a single chunk for "Section A".
    assert len(text_chunks) == 1
    assert text_chunks[0].metadata["headings"] == ("Section A",)
    assert text_chunks[0].text.rstrip().endswith("Tiny tail.")
    assert big_paragraph in text_chunks[0].text


def test_paged_fixture_carries_page_metadata() -> None:
    doc = _load("sample_paged.parsed.json")
    chunks = StructuralChunker().chunk(doc)
    by_heading = {c.metadata["headings"]: c for c in chunks if c.metadata["content_type"] == "text"}
    assert by_heading[("Alpha Section",)].metadata["pages"] == (1,)
    assert by_heading[("Beta Section",)].metadata["pages"] == (2,)
    assert by_heading[("Gamma Section",)].metadata["pages"] == (1, 2)
    table_chunk = next(c for c in chunks if c.metadata["content_type"] == "table")
    assert table_chunk.metadata["pages"] == (2,)
    assert table_chunk.metadata["headings"] == ("Gamma Section",)


def test_html_fixture_has_no_page_metadata() -> None:
    chunks = StructuralChunker().chunk(_report_doc())
    for c in chunks:
        assert c.metadata["pages"] == ()


# --- NE-020 P2b fold: body-less headings emit their own chunk ------------------
# A HEADING that never contributes to an emitted chunk's heading context (a
# title-only doc, a trailing heading, or a sibling heading immediately replaced
# by another at the same level) was previously dropped, losing its text to
# retrieval. It must now emit its own ``text`` chunk.


def test_title_only_document_emits_heading_chunk() -> None:
    doc = ParsedDocument(
        document_id="title-only",
        source_name="title-only.md",
        source_format="md",
        blocks=(DocBlock(kind=BlockKind.HEADING, text="Title", level=1),),
    )
    chunks = StructuralChunker().chunk(doc)
    assert len(chunks) == 1
    only = chunks[0]
    # No ancestor path -> no heading prefix, body is exactly the title text.
    assert only.text == "Title"
    assert only.metadata["headings"] == ()
    assert only.metadata["content_type"] == "text"
    # Stable, deterministic ids (idempotency depends on this).
    assert only.chunk_id == "title-only:0000"
    assert only.document_id == "title-only"


def test_trailing_heading_emits_chunk() -> None:
    doc = ParsedDocument(
        document_id="trailing",
        source_name="trailing.md",
        source_format="md",
        blocks=(
            DocBlock(kind=BlockKind.HEADING, text="Intro", level=1),
            DocBlock(kind=BlockKind.PARAGRAPH, text="Body paragraph under the intro heading."),
            DocBlock(kind=BlockKind.HEADING, text="Appendix", level=1),
        ),
    )
    chunks = StructuralChunker().chunk(doc)
    # The trailing "Appendix" heading is body-less at end of document; its text
    # must survive in an emitted chunk rather than being dropped.
    assert any("Appendix" in c.text for c in chunks)
    appendix = next(c for c in chunks if c.metadata["headings"] == () and "Appendix" in c.text)
    assert appendix.text == "Appendix"


def test_sibling_heading_replacement_emits_bodyless_heading_chunk() -> None:
    doc = ParsedDocument(
        document_id="siblings",
        source_name="siblings.md",
        source_format="md",
        blocks=(
            DocBlock(kind=BlockKind.HEADING, text="A", level=1),
            DocBlock(kind=BlockKind.HEADING, text="B", level=1),
            DocBlock(kind=BlockKind.PARAGRAPH, text="Body under section B only."),
        ),
    )
    chunks = StructuralChunker().chunk(doc)
    # "A" (a mid-document body-less sibling) is emitted in order, BEFORE B's section.
    assert chunks[0].text == "A"
    assert chunks[0].metadata["headings"] == ()
    b_section = next(c for c in chunks if c.metadata["headings"] == ("B",))
    assert "Body under section B only." in b_section.text
    # Sequence order preserved: the "A" chunk precedes B's section chunk.
    assert chunks.index(chunks[0]) < chunks.index(b_section)


def test_headings_with_bodies_emit_no_extra_chunks() -> None:
    # Every heading here precedes a body, so every heading is "covered": the P2b
    # fold must add NO extra chunks and NO chunk whose body is a bare heading.
    doc = ParsedDocument(
        document_id="covered",
        source_name="covered.md",
        source_format="md",
        blocks=(
            DocBlock(kind=BlockKind.HEADING, text="First", level=1),
            DocBlock(kind=BlockKind.PARAGRAPH, text="First section body content here."),
            DocBlock(kind=BlockKind.HEADING, text="Second", level=1),
            DocBlock(kind=BlockKind.PARAGRAPH, text="Second section body content here."),
        ),
    )
    chunks = StructuralChunker().chunk(doc)
    assert len(chunks) == 2
    assert chunks[0].metadata["headings"] == ("First",)
    assert chunks[1].metadata["headings"] == ("Second",)
    # No chunk is a bare heading (body == heading text with empty ancestor path).
    assert not any(c.metadata["headings"] == () for c in chunks)
