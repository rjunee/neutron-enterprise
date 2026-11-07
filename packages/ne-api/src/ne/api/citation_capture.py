# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Per-turn capture of citations served by governed knowledge search.

The capture is populated only by the production ``read.knowledge.search``
binding, after its principal-scoped retriever returned and the handler finished
building its successful result.  Consumers therefore relay source-of-record
identities that survived ACL trimming; they never infer citations from model
text, tool arguments, or chunk rendering.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from ne.api.wire import citation_payloads
from ne.contracts.types import RetrievalResult

CitationPayload = dict[str, str]
_CitationKey = tuple[str, str, str, str]


@dataclass
class CitationCapture:
    """Stable ordered citation identities with optional server enrichment."""

    _payloads: list[CitationPayload] = field(default_factory=list)
    _seen: dict[_CitationKey, int] = field(default_factory=dict)

    def record(self, result: RetrievalResult) -> None:
        """Append previously unseen citations without changing served order."""
        for payload in citation_payloads(result):
            key = (
                payload["chunk_id"],
                payload["document_id"],
                payload["source_system"],
                payload["source_ref"],
            )
            previous_index = self._seen.get(key)
            if previous_index is not None:
                previous = self._payloads[previous_index]
                if previous.get("source_url") != payload.get("source_url"):
                    # The same citation identity cannot truthfully open two
                    # different records. A link gained/lost/changed across
                    # calls in one turn therefore fails closed to no link.
                    previous.pop("source_url", None)
                continue
            self._seen[key] = len(self._payloads)
            self._payloads.append(payload)

    def snapshot(self) -> list[CitationPayload]:
        """Return an isolated wire-ready copy of the accumulated payloads."""
        return [dict(payload) for payload in self._payloads]

    def restore(self, payloads: tuple[CitationPayload, ...]) -> None:
        """Restore only a previously published, structurally valid replay envelope."""
        required = {"chunk_id", "document_id", "source_system", "source_ref"}
        allowed = required | {"source_url"}
        for payload in payloads:
            if (
                set(payload) - allowed
                or not required.issubset(payload)
                or any(not payload[key].strip() for key in payload)
            ):
                raise RuntimeError("stored conversation replay citation is malformed")
            key: _CitationKey = (
                payload["chunk_id"],
                payload["document_id"],
                payload["source_system"],
                payload["source_ref"],
            )
            if key in self._seen:
                continue
            self._seen[key] = len(self._payloads)
            self._payloads.append(dict(payload))


_TURN_CITATIONS: ContextVar[CitationCapture | None] = ContextVar(
    "ne_api_turn_citations", default=None
)


@contextmanager
def citation_capture_scope() -> Iterator[CitationCapture]:
    """Bind one fresh accumulator for exactly one chat turn."""
    capture = CitationCapture()
    token = _TURN_CITATIONS.set(capture)
    try:
        yield capture
    finally:
        _TURN_CITATIONS.reset(token)


def record_served_citations(result: RetrievalResult) -> None:
    """Record a successful governed search result when a turn scope is active."""
    capture = _TURN_CITATIONS.get()
    if capture is not None:
        capture.record(result)


def current_citations() -> list[CitationPayload]:
    """Snapshot the active turn's citations, or the default-empty legacy shape."""
    capture = _TURN_CITATIONS.get()
    return capture.snapshot() if capture is not None else []
