# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Coverage for the REAL ``scan_filtered`` implementations (NE-S5-INTERNAL-CRM).

``scan_filtered`` is the read the internal CRM lookup resolves a contact with —
the record whose ``email`` an outreach draft then copies. Before this file the
only ``scan_filtered`` any test executed was the in-memory double in
``packages/ne-api/tests/conftest.py``; NEITHER shipped implementation
(:meth:`ne.brain.qdrant.QdrantChunkStore.scan_filtered`'s scroll-pagination loop,
:meth:`ne.brain.pgvector.store.PgvectorChunkStore.scan_filtered`'s SQL assembly)
ran anywhere. The box runs one of THOSE. A filter-grammar or scroll-offset defect
there mis-resolves a contact and serves the wrong email while CI stays green.

What is pinned here:

* Qdrant against ``AsyncQdrantClient(":memory:")`` — the vendor's real in-process
  engine, so the scroll loop, the offset chase, the ``limit`` cap and the frozen
  filter grammar all execute for real.
* pgvector with the ``psycopg`` chokepoint (``ne.brain.pgvector._pg.execute``)
  intercepted — everything above the wire is the shipped method: the fail-closed
  refusals, ``compile_filters``, the exact SQL text, the parameter ORDER, the
  bound ``LIMIT``, the ``ORDER BY chunk_id`` determinism and the
  ``Chunk.model_validate`` of returned payloads.
* ACL (the negative case the review plan demands): ``scan_filtered`` applies NO
  ACL of its own, and the internal CRM caller passes no ACL term — deliberate,
  because the Salesforce client it replaced authenticated as ONE integration
  identity and was never scoped to the calling principal. That exposure is
  pinned here in BOTH directions so it can never become an accident: omitting
  the terms returns rows the principal cannot see, AND passing them refuses
  those rows. A caller that omits ACL terms is therefore refusable — it is
  choosing the exposure, not discovering that the store cannot enforce.
* The one real divergence between the two compilers of the one frozen grammar:
  a non-scalar element inside a list value. pgvector raised ``ValueError``;
  Qdrant handed it to ``MatchAny`` un-vetted. They now agree (both fail closed),
  and this pins it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient

from ne.brain import pgvector as _pgvector_pkg
from ne.brain.pgvector.filters import compile_filters
from ne.brain.pgvector.store import PgvectorChunkStore
from ne.brain.qdrant import QdrantChunkStore, _field_condition
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.contracts.types import Chunk

# The demo shape the internal CRM lookup scans by: the connector mapper stamps
# ``connector`` + ``object`` + ``record_id`` into chunk metadata.
_SALESFORCE = "salesforce"


def _chunk(
    chunk_id: str,
    *,
    connector: str = _SALESFORCE,
    obj: str = "Contact",
    text: str = "Avery\n\nArbor\n\navery@example.test",
    acl: tuple[str, ...] = ("ops",),
    extra: Mapping[str, Any] | None = None,
) -> Chunk:
    metadata: dict[str, Any] = {
        "connector": connector,
        "object": obj,
        "record_id": f"003{chunk_id}",
        "source_system": "crm",
        "source_ref": f"ref-{chunk_id}",
        "source_status": "no_source",
    }
    metadata.update(extra or {})
    return Chunk(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        text=text,
        metadata=metadata,
        acl=acl,
    )


# ---------------------------------------------------------------------------
# Qdrant — the REAL store against the vendor's in-process engine
# ---------------------------------------------------------------------------


def _qdrant_store() -> QdrantChunkStore:
    store = QdrantChunkStore(
        AsyncQdrantClient(":memory:"),
        f"scan_{uuid.uuid4().hex}",
        HashEmbedder(32),
        HashSparseEncoder(),
    )
    asyncio.run(store.ensure_collection())
    return store


def _seed(store: QdrantChunkStore, chunks: Sequence[Chunk]) -> None:
    asyncio.run(store.upsert_chunks(list(chunks)))


def _ids(chunks: Sequence[Chunk]) -> set[str]:
    return {c.chunk_id for c in chunks}


def test_qdrant_scan_filtered_serves_exactly_the_stamped_metadata_match() -> None:
    store = _qdrant_store()
    _seed(
        store,
        [
            _chunk("c1"),
            _chunk("c2", obj="Account", text="Arbor Household"),
            _chunk("c3", connector="sharepoint"),
        ],
    )
    got = asyncio.run(
        store.scan_filtered(
            filters={"metadata.connector": _SALESFORCE, "metadata.object": "Contact"},
            limit=64,
        )
    )
    assert _ids(got) == {"c1"}
    # Every returned payload really did round-trip through Chunk validation.
    assert got[0].metadata["record_id"] == "003c1"


def test_qdrant_scan_filtered_and_combines_every_term() -> None:
    """Two terms are AND, not OR — the pin is only worth anything if it narrows."""
    store = _qdrant_store()
    _seed(store, [_chunk("c1"), _chunk("c2", connector="sql:crm_export")])
    got = asyncio.run(
        store.scan_filtered(
            filters={"metadata.connector": _SALESFORCE, "metadata.object": "Contact"},
            limit=64,
        )
    )
    assert _ids(got) == {"c1"}


def test_qdrant_scan_filtered_list_value_is_any_of() -> None:
    store = _qdrant_store()
    _seed(
        store,
        [
            _chunk("c1", obj="Contact"),
            _chunk("c2", obj="Account"),
            _chunk("c3", obj="Case"),
        ],
    )
    got = asyncio.run(
        store.scan_filtered(filters={"metadata.object": ["Contact", "Account"]}, limit=64)
    )
    assert _ids(got) == {"c1", "c2"}


def test_qdrant_scan_filtered_matches_an_array_valued_payload_field() -> None:
    """A scalar term against an ARRAY field is contains, not whole-value equality.

    (The in-memory double in ne-api compares the whole value and would miss
    this — which is exactly why the real store needs its own coverage.)
    """
    store = _qdrant_store()
    _seed(store, [_chunk("c1", acl=("ops", "sales")), _chunk("c2", acl=("legal",))])
    got = asyncio.run(store.scan_filtered(filters={"acl": "sales"}, limit=64))
    assert _ids(got) == {"c1"}


def test_qdrant_scan_filtered_chases_the_scroll_offset_past_one_page() -> None:
    """The pagination loop: 300 matches > the 256-row page the store asks for."""
    store = _qdrant_store()
    _seed(store, [_chunk(f"c{i:04d}", text=f"Person {i}") for i in range(300)])

    client = store._c()
    offsets: list[Any] = []
    original = client.scroll

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        offsets.append(kwargs.get("offset"))
        return await original(*args, **kwargs)

    client.scroll = _spy  # type: ignore[method-assign]
    try:
        got = asyncio.run(store.scan_filtered(filters={"metadata.object": "Contact"}, limit=1024))
    finally:
        client.scroll = original  # type: ignore[method-assign]

    # The loop really ran a second time, with the server-issued offset.
    assert len(offsets) > 1
    assert offsets[0] is None and offsets[1] is not None
    assert len(got) == 300
    assert len(_ids(got)) == 300  # no page re-served, no row dropped


def test_qdrant_scan_filtered_caps_at_limit_so_truncation_is_detectable() -> None:
    store = _qdrant_store()
    _seed(store, [_chunk(f"c{i:04d}", text=f"Person {i}") for i in range(300)])
    got = asyncio.run(store.scan_filtered(filters={"metadata.object": "Contact"}, limit=260))
    # Exactly `limit` back is the caller's truncation signal (the internal CRM
    # lookup refuses on it rather than resolving against a partial record set).
    assert len(got) == 260


def test_qdrant_scan_filtered_refuses_an_unfiltered_scan() -> None:
    store = _qdrant_store()
    with pytest.raises(ValueError, match="non-empty filter"):
        asyncio.run(store.scan_filtered(filters={}, limit=64))


def test_qdrant_scan_filtered_refuses_a_non_positive_limit() -> None:
    store = _qdrant_store()
    with pytest.raises(ValueError, match="limit >= 1"):
        asyncio.run(store.scan_filtered(filters={"metadata.object": "Contact"}, limit=0))


def test_qdrant_scan_filtered_refuses_an_unknown_reserved_key() -> None:
    """Anti-smuggling: only ``$any`` is a recognised ``$``-prefixed token."""
    store = _qdrant_store()
    with pytest.raises(ValueError, match="unknown reserved filter key"):
        asyncio.run(store.scan_filtered(filters={"$not": {"metadata.object": "x"}}, limit=64))


def test_qdrant_scan_filtered_supports_the_any_or_group() -> None:
    store = _qdrant_store()
    _seed(store, [_chunk("c1", acl=("ops",)), _chunk("c2", acl=("legal",))])
    got = asyncio.run(
        store.scan_filtered(filters={"$any": [{"acl": ["ops"]}, {"acl": ["hr"]}]}, limit=64)
    )
    assert _ids(got) == {"c1"}


def test_qdrant_scan_filtered_omitting_acl_terms_is_untrimmed_but_refusable() -> None:
    """The negative case: no ACL term means NO trim — and the trim IS available.

    The internal CRM lookup deliberately passes no ACL term (the Salesforce
    client it mirrors authenticated as one integration identity and was never
    scoped to the caller). This pins BOTH halves so the exposure stays a choice:
    omitting the terms serves rows the principal cannot see, and passing them
    refuses those same rows.
    """
    store = _qdrant_store()
    _seed(store, [_chunk("mine", acl=("u-ada",)), _chunk("theirs", acl=("u-bob",))])
    base = {"metadata.connector": _SALESFORCE, "metadata.object": "Contact"}

    untrimmed = asyncio.run(store.scan_filtered(filters=dict(base), limit=64))
    assert _ids(untrimmed) == {"mine", "theirs"}

    trimmed = asyncio.run(store.scan_filtered(filters={**base, "acl": ["u-ada"]}, limit=64))
    assert _ids(trimmed) == {"mine"}


# ---------------------------------------------------------------------------
# pgvector — the REAL method above an intercepted psycopg chokepoint
# ---------------------------------------------------------------------------

_DEAD_DSN = "postgresql://nobody@127.0.0.1:1/none"


class _RecordingPg:
    """Stands in for ``ne.brain.pgvector._pg`` — records the statement it is given."""

    def __init__(self, rows: Sequence[tuple[Any, ...]] = ()) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(
        self, conn: Any, statement: str, params: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.calls.append((statement, tuple(params or ())))
        return list(self.rows)


def _pgvector_store(
    monkeypatch: pytest.MonkeyPatch, *, rows: Sequence[tuple[Any, ...]] = ()
) -> tuple[PgvectorChunkStore, _RecordingPg]:
    """A real store whose ONLY stub is the psycopg wire call (no connect, no DB)."""
    store = PgvectorChunkStore(_DEAD_DSN, "ne_chunks", HashEmbedder(32), HashSparseEncoder())
    recorder = _RecordingPg(rows)
    monkeypatch.setattr(_pgvector_pkg, "_pg", recorder, raising=False)

    @asynccontextmanager
    async def _acquire(self: PgvectorChunkStore) -> AsyncIterator[Any]:
        yield object()  # a connection sentinel; the recorder never touches it

    monkeypatch.setattr(PgvectorChunkStore, "_acquire", _acquire)
    return store, recorder


def test_pgvector_scan_filtered_assembles_bounded_ordered_parameterized_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, recorder = _pgvector_store(monkeypatch)
    filters = {"metadata.connector": _SALESFORCE, "metadata.object": "Contact"}
    asyncio.run(store.scan_filtered(filters=filters, limit=1024))

    ((sql, params),) = recorder.calls
    expected = compile_filters(filters)
    assert expected is not None
    assert sql == (
        "SELECT chunk_id, payload FROM ne_chunks WHERE "
        + expected.sql
        + " ORDER BY chunk_id LIMIT %s"
    )
    # Filter params first, IN ORDER, then the bound limit — never interpolated.
    assert params == (*expected.params, 1024)
    # Every value travelled as a bound json.dumps string; no literal in the text.
    assert _SALESFORCE not in sql
    assert json.dumps({"metadata": {"connector": _SALESFORCE}}) in params


def test_pgvector_scan_filtered_returns_payload_rows_as_validated_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = _chunk("c1")
    store, _ = _pgvector_store(monkeypatch, rows=[("c1", json.loads(chunk.model_dump_json()))])
    got = asyncio.run(store.scan_filtered(filters={"metadata.object": "Contact"}, limit=8))
    assert [c.chunk_id for c in got] == ["c1"]
    assert got[0].metadata["connector"] == _SALESFORCE


def test_pgvector_scan_filtered_refuses_an_unfiltered_scan_before_any_connection() -> None:
    store = PgvectorChunkStore(_DEAD_DSN, "ne_chunks", HashEmbedder(32), HashSparseEncoder())
    with pytest.raises(ValueError, match="non-empty filter"):
        asyncio.run(store.scan_filtered(filters={}, limit=8))
    assert store._conn is None  # the refusal short-circuits _acquire


def test_pgvector_scan_filtered_refuses_a_non_positive_limit_before_any_connection() -> None:
    store = PgvectorChunkStore(_DEAD_DSN, "ne_chunks", HashEmbedder(32), HashSparseEncoder())
    with pytest.raises(ValueError, match="limit >= 1"):
        asyncio.run(store.scan_filtered(filters={"metadata.object": "Contact"}, limit=0))
    assert store._conn is None


def test_pgvector_scan_filtered_refuses_an_unknown_reserved_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, recorder = _pgvector_store(monkeypatch)
    with pytest.raises(ValueError, match="unknown reserved filter key"):
        asyncio.run(store.scan_filtered(filters={"$not": {"metadata.object": "x"}}, limit=8))
    assert recorder.calls == []  # nothing reached the wire


def test_pgvector_scan_filtered_omitting_acl_terms_is_untrimmed_but_refusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pgvector half of the negative ACL case — see the Qdrant twin above.

    Without an ``acl`` term the WHERE fragment carries no ACL predicate at all
    (nothing trims); with one it does, so the refusal is available to any caller
    that wants it.
    """
    store, recorder = _pgvector_store(monkeypatch)
    base = {"metadata.connector": _SALESFORCE, "metadata.object": "Contact"}

    asyncio.run(store.scan_filtered(filters=dict(base), limit=8))
    untrimmed_sql, untrimmed_params = recorder.calls[-1]
    assert not any("acl" in str(p) for p in untrimmed_params)

    asyncio.run(store.scan_filtered(filters={**base, "acl": ["u-ada"]}, limit=8))
    trimmed_sql, trimmed_params = recorder.calls[-1]
    assert json.dumps({"acl": "u-ada"}) in trimmed_params
    assert json.dumps({"acl": ["u-ada"]}) in trimmed_params
    assert trimmed_sql.count("payload @>") > untrimmed_sql.count("payload @>")


# ---------------------------------------------------------------------------
# The two compilers of the ONE frozen grammar must refuse the same things
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [{"nested": "dict"}, ["deeper"], None, 1.5])
def test_both_compilers_refuse_a_non_scalar_element_inside_a_list_value(bad: Any) -> None:
    """Divergence found by review: pgvector failed closed, Qdrant did not.

    ``compile_filters`` vets EVERY element of a list value through
    ``_scalar_or_raise``; ``ne.brain.qdrant._field_condition`` used to pass the
    whole list to ``MatchAny`` un-vetted. Unreachable from today's only
    ``scan_filtered`` caller (it passes fixed string literals) — pinned so it
    stays that way, with both compilers refusing identically.
    """
    with pytest.raises(ValueError, match="unsupported filter value"):
        _field_condition("metadata.object", ["Contact", bad])
    with pytest.raises(ValueError, match="unsupported filter value"):
        compile_filters({"metadata.object": ["Contact", bad]})


@pytest.mark.parametrize(
    "filters",
    [
        {"metadata.object": {"nested": "dict"}},
        {"$not": [{"acl": ["ops"]}]},
        {"$any": []},
        {"$any": [{}]},
        {"$any": [{"$any": [{"acl": ["ops"]}]}]},
    ],
)
def test_both_compilers_refuse_the_same_malformed_filters(filters: Mapping[str, Any]) -> None:
    with pytest.raises(ValueError):
        from ne.brain.qdrant import _build_filter

        _build_filter(filters)
    with pytest.raises(ValueError):
        compile_filters(filters)
