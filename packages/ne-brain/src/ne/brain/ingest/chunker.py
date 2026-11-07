# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Structure-aware chunker (NE-020). Implements ``ne.brain.ingest.contracts.Chunker``.

Algorithm (deterministic — NE-026 idempotency depends on stable ``chunk_id``s):

* A heading stack tracks the current heading path. A ``HEADING`` block of level
  ``L`` pops every stacked heading with level ``>= L``, then pushes itself.
  A heading whose section produced at least one emitted chunk is "covered" and
  updates context only. A heading that is popped (or reached at end of document)
  while still UNCOVERED — a title-only, trailing, or otherwise body-less heading
  — is emitted as its own ``text`` chunk (body = the heading text, heading-path
  metadata = its ANCESTOR path only) so its text is never lost to retrieval
  (NE-020 P2b fold). Nested body-less headings collapse into a single ancestor-
  prefixed chunk rather than one chunk per level.
* ``TABLE`` blocks are ATOMIC: the running text buffer is always flushed first,
  then the table is emitted as its own chunk (``content_type='table'``) —
  never split, even past ``max_chars``.
* ``PARAGRAPH``/``LIST_ITEM``/``CODE``/``CAPTION``/``OTHER`` text accumulates
  into a section buffer, flushed when the heading path changes or appending
  would exceed ``max_chars``.
* A single block longer than ``max_chars`` is split at sentence boundaries
  (falling back to a hard character split for any single sentence that is
  itself oversized).
* After a flush, a resulting fragment shorter than ``min_chars`` merges
  backward into the previous emitted chunk if that chunk shares the same
  heading path and is itself a text chunk (never a table).

Every emitted chunk is a frozen ``ne.contracts.types.Chunk`` carrying
heading-path, page-number (ABI LIFT-PATTERN teardown §3.3), and sequence
metadata for the NE-026 pipeline spine.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from ne.brain.ingest.contracts import BlockKind, ParsedDocument
from ne.contracts.types import Chunk


@dataclass
class _Heading:
    """One entry on the chunker's heading stack.

    ``covered`` flips to ``True`` once any chunk is emitted while this heading is
    on the stack; an uncovered heading that is popped (or survives to end of
    document) is a body-less heading and gets its own emitted chunk (NE-020 P2b).
    """

    level: int
    text: str
    page: int | None
    covered: bool = False


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_oversized(text: str, max_chars: int) -> list[str]:
    """Split ``text`` into pieces <= ``max_chars``, preferring sentence boundaries."""
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s]
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = sentence if not current else f"{current} {sentence}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            pieces.append(current)
            current = ""
        if len(sentence) > max_chars:
            for start in range(0, len(sentence), max_chars):
                pieces.append(sentence[start : start + max_chars])
        else:
            current = sentence
    if current:
        pieces.append(current)
    return pieces


class StructuralChunker:
    """Chunks a :class:`ParsedDocument` into frozen ``Chunk`` objects."""

    EVIDENCE_REVISION = "ne.structural_chunker/default-v1"
    DEFAULT_MAX_CHARS = 1800
    DEFAULT_MIN_CHARS = 200
    DEFAULT_INCLUDE_HEADINGS_IN_TEXT = True

    def __init__(
        self,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
        min_chars: int = DEFAULT_MIN_CHARS,
        include_headings_in_text: bool = DEFAULT_INCLUDE_HEADINGS_IN_TEXT,
    ) -> None:
        self._max_chars = max_chars
        self._min_chars = min_chars
        self._include_headings_in_text = include_headings_in_text

    def chunk(self, doc: ParsedDocument, *, acl: Sequence[str] = ()) -> tuple[Chunk, ...]:
        chunks: list[Chunk] = []
        emission_index = 0
        heading_stack: list[_Heading] = []

        buffer_texts: list[str] = []
        buffer_pages: set[int] = set()
        buffer_headings: tuple[str, ...] = ()

        acl_tuple = tuple(acl)
        base_metadata = dict(doc.metadata)

        def current_headings() -> tuple[str, ...]:
            return tuple(h.text for h in heading_stack)

        def buffer_len() -> int:
            if not buffer_texts:
                return 0
            return sum(len(t) for t in buffer_texts) + 2 * (len(buffer_texts) - 1)

        def make_chunk(
            body: str,
            headings: tuple[str, ...],
            pages: tuple[int, ...],
            content_type: str,
            index: int,
        ) -> Chunk:
            prefix = ""
            if headings and self._include_headings_in_text:
                prefix = " > ".join(headings) + "\n\n"
            metadata = dict(base_metadata)
            metadata.update(
                {
                    "headings": headings,
                    "pages": pages,
                    "content_type": content_type,
                    "sequence": index,
                    "source_name": doc.source_name,
                    "source_format": doc.source_format,
                }
            )
            return Chunk(
                chunk_id=f"{doc.document_id}:{index:04d}",
                document_id=doc.document_id,
                text=prefix + body,
                metadata=metadata,
                acl=acl_tuple,
            )

        def emit(body: str, headings: tuple[str, ...], pages: set[int], content_type: str) -> None:
            nonlocal emission_index
            if not body:
                return
            # Any real emission (fresh append OR back-merge) covers every heading
            # currently on the stack: their section now has representation in the
            # index, so they are not body-less (NE-020 P2b coverage tracking).
            for heading in heading_stack:
                heading.covered = True
            sorted_pages = tuple(sorted(pages))
            if (
                content_type == "text"
                and len(body) < self._min_chars
                and chunks
                and chunks[-1].metadata.get("content_type") == "text"
                and chunks[-1].metadata.get("headings") == headings
            ):
                prev = chunks[-1]
                prev_pages = set(prev.metadata.get("pages", ()))
                merged_pages = tuple(sorted(prev_pages | pages))
                prefix = ""
                if headings and self._include_headings_in_text:
                    prefix = " > ".join(headings) + "\n\n"
                prev_body = (
                    prev.text[len(prefix) :]
                    if prefix and prev.text.startswith(prefix)
                    else prev.text
                )
                new_body = prev_body + "\n\n" + body
                new_metadata = dict(prev.metadata)
                new_metadata["pages"] = merged_pages
                chunks[-1] = Chunk(
                    chunk_id=prev.chunk_id,
                    document_id=prev.document_id,
                    text=prefix + new_body,
                    metadata=new_metadata,
                    acl=prev.acl,
                )
                return
            chunk = make_chunk(body, headings, sorted_pages, content_type, emission_index)
            chunks.append(chunk)
            emission_index += 1

        def flush_buffer() -> None:
            nonlocal buffer_texts, buffer_pages
            if not buffer_texts:
                return
            body = "\n\n".join(buffer_texts)
            emit(body, buffer_headings, buffer_pages, "text")
            buffer_texts = []
            buffer_pages = set()

        def emit_bare_heading(popped: _Heading) -> None:
            # A body-less heading emits its own text chunk. It is popped BEFORE
            # this call, so ``current_headings()`` is its ancestor path (excluding
            # itself); the emit then covers those ancestors, so nested body-less
            # headings collapse into a single ancestor-prefixed chunk instead of
            # one chunk per level (NE-020 P2b).
            pages = {popped.page} if popped.page is not None else set()
            emit(popped.text, current_headings(), pages, "text")

        for block in doc.blocks:
            if block.kind == BlockKind.HEADING:
                flush_buffer()
                level = block.level if block.level is not None else 1
                while heading_stack and heading_stack[-1].level >= level:
                    popped = heading_stack.pop()
                    if not popped.covered:
                        emit_bare_heading(popped)
                heading_stack.append(_Heading(level=level, text=block.text, page=block.page))
                continue

            if block.kind == BlockKind.TABLE:
                flush_buffer()
                table_pages = {block.page} if block.page is not None else set()
                emit(block.text, current_headings(), table_pages, "table")
                continue

            # PARAGRAPH / LIST_ITEM / CODE / CAPTION / OTHER
            hp = current_headings()
            if buffer_texts and buffer_headings != hp:
                flush_buffer()
            if not buffer_texts:
                buffer_headings = hp

            text = block.text
            if len(text) > self._max_chars:
                if buffer_texts:
                    flush_buffer()
                    buffer_headings = hp
                block_pages = {block.page} if block.page is not None else set()
                for piece in _split_oversized(text, self._max_chars):
                    emit(piece, hp, block_pages, "text")
                continue

            candidate_len = buffer_len() + (2 if buffer_texts else 0) + len(text)
            if buffer_texts and candidate_len > self._max_chars:
                flush_buffer()
                buffer_headings = hp

            buffer_texts.append(text)
            if block.page is not None:
                buffer_pages.add(block.page)

        flush_buffer()
        # Trailing body-less headings (e.g. a title-only document, or a heading
        # with no following body at end of document): pop deepest-first, emitting
        # the deepest uncovered heading — which covers its still-stacked ancestors
        # so a nested bare tail yields ONE ancestor-prefixed chunk (NE-020 P2b).
        while heading_stack:
            popped = heading_stack.pop()
            if not popped.covered:
                emit_bare_heading(popped)
        return tuple(chunks)
