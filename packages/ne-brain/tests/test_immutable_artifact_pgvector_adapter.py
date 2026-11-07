# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-CORE: the Pgvector pole of ``ImmutableArtifactVectorSeam``.

The Qdrant pole is proven end to end against a live server in
``test_immutable_artifact_store_postgres_qdrant.py``. This file proves the
Postgres-backed adapter at the seam it actually owns — the SQL it emits — through
the existing ``ne.brain.pgvector._pg`` psycopg chokepoint, which is the one module
the store talks to and therefore the one place a fake belongs.

Every assertion here is value-discriminating, because a presence-only check is
worthless on this surface:

* the candidate payload is asserted as tuple EQUALITY over the bound parameters,
  so writing the artifact id where the candidate id belongs fails — that mutation
  collapses every candidate for one artifact onto a single row whose id is not the
  one the address row selected, which makes the elected winner unreachable;
* enumeration is asserted as tuple EQUALITY over the returned ids AND over the
  bound filter parameter, so dropping the ``artifact_id`` predicate fails even
  when the fake happens to hold rows for one artifact only;
* overwrite and delete capability are asserted as SET EQUALITY over the SQL verbs
  the adapter is capable of emitting, so an ``UPDATE``, ``DELETE``, ``TRUNCATE`` or
  ``DROP`` appearing anywhere in either method fails here rather than at a
  distance.

The fake records statements rather than executing them, so the overwrite claim is
about the statement the adapter EMITS (`ON CONFLICT (candidate_id) DO NOTHING`,
never `(artifact_id)`) — under a fake, two inserts are emitted either way, and only
the conflict target distinguishes them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from ne.brain.pgvector import _pg
from ne.brain.pgvector.store import PgvectorChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.write.immutable_artifacts import ImmutableArtifactVectorSeam

DSN = "postgresql://nobody@127.0.0.1:1/none"
TABLE = "ne_chunks"
CANDIDATE_TABLE = "ne_chunks_source_artifact_candidates"
ARTIFACT_ID = "a" * 64
DIMENSION = 4


@dataclass
class RecordedConnection:
    """Stands in for the psycopg connection the chokepoint hands back."""

    closed: bool = False

    async def close(self) -> None:
        self.closed = True


@dataclass
class Chokepoint:
    """A fake ``ne.brain.pgvector._pg`` recording exactly what the store emits."""

    rows: list[tuple[Any, ...]] = field(default_factory=list)
    statements: list[tuple[str, tuple[Any, ...] | None]] = field(default_factory=list)
    connection: RecordedConnection = field(default_factory=RecordedConnection)

    async def connect(self, dsn: str) -> RecordedConnection:
        assert dsn == DSN
        return self.connection

    async def execute(self, conn: Any, statement: str, params: Any = None) -> list[tuple[Any, ...]]:
        assert conn is self.connection
        self.statements.append((statement, None if params is None else tuple(params)))
        return list(self.rows) if statement.lstrip().upper().startswith("SELECT") else []

    # -- views over what was recorded ---------------------------------------

    def of_verb(self, verb: str) -> list[tuple[str, tuple[Any, ...] | None]]:
        return [item for item in self.statements if _verb(item[0]) == verb]

    def verbs(self) -> set[str]:
        return {_verb(statement) for statement, _ in self.statements}


def _verb(statement: str) -> str:
    tokens = statement.split()
    head = tokens[0].upper()
    if head in {"CREATE", "DROP", "ALTER"}:
        return f"{head} {tokens[1].upper()}"
    return head


@pytest.fixture
def chokepoint(monkeypatch: pytest.MonkeyPatch) -> Chokepoint:
    fake = Chokepoint()
    monkeypatch.setattr(_pg, "connect", fake.connect)
    monkeypatch.setattr(_pg, "execute", fake.execute)
    return fake


def store() -> PgvectorChunkStore:
    return PgvectorChunkStore(DSN, TABLE, HashEmbedder(DIMENSION), HashSparseEncoder())


def dense() -> list[float]:
    return [0.25, -0.5, 0.75, -1.0]


# --- the adapter is the seam -------------------------------------------------


def test_the_pgvector_store_satisfies_the_immutable_artifact_vector_seam() -> None:
    """Structural only, and paired below: the Protocol is ``@runtime_checkable``,
    so on its own it compares method NAMES and proves nothing about behaviour."""
    assert isinstance(store(), ImmutableArtifactVectorSeam)


# --- the exact candidate payload ---------------------------------------------


def test_the_candidate_row_binds_the_exact_candidate_id_artifact_id_and_vector(
    chokepoint: Chokepoint,
) -> None:
    """Tuple EQUALITY over the bound parameters, in order.

    Writing the artifact id into the candidate-id position is the failure this
    owns: it collapses every candidate for one artifact onto one row, and the id
    the address row selected then names no row at all.
    """
    subject = store()
    asyncio.run(
        subject.put_immutable_artifact_candidate(
            candidate_id="candidate-1", artifact_id=ARTIFACT_ID, dense=dense()
        )
    )
    inserts = chokepoint.of_verb("INSERT")
    assert len(inserts) == 1
    statement, params = inserts[0]
    assert params == ("candidate-1", ARTIFACT_ID, dense())
    assert params is not None and params[0] != params[1]
    assert f"INSERT INTO {CANDIDATE_TABLE} (candidate_id, artifact_id, dense)" in statement


def test_a_dense_vector_of_the_wrong_dimension_never_reaches_the_database(
    chokepoint: Chokepoint,
) -> None:
    """The seam validates before it writes, so a short vector is not persisted."""
    with pytest.raises(ValueError):
        asyncio.run(
            store().put_immutable_artifact_candidate(
                candidate_id="candidate-1", artifact_id=ARTIFACT_ID, dense=[0.1, 0.2]
            )
        )
    assert chokepoint.of_verb("INSERT") == []


# --- enumeration --------------------------------------------------------------


def test_enumeration_returns_this_artifacts_candidate_ids_sorted_and_filtered(
    chokepoint: Chokepoint,
) -> None:
    """EQUALITY on both the result and the bound filter parameter.

    Asserting the filter parameter is what makes this fail when the
    ``WHERE artifact_id = %s`` predicate is dropped — the fake would still return
    its rows, so a result-only assertion would pass a filterless query.
    """
    chokepoint.rows = [("candidate-2",), ("candidate-1",), ("candidate-3",)]
    found = asyncio.run(store().immutable_artifact_candidate_ids(ARTIFACT_ID))
    assert found == ("candidate-1", "candidate-2", "candidate-3")
    selects = chokepoint.of_verb("SELECT")
    assert len(selects) == 1
    statement, params = selects[0]
    assert params == (ARTIFACT_ID,)
    assert f"FROM {CANDIDATE_TABLE} WHERE artifact_id = %s" in statement


def test_enumeration_of_an_artifact_with_no_candidates_is_empty(
    chokepoint: Chokepoint,
) -> None:
    """The must-PASS pair for the control above: empty is empty, not an error."""
    chokepoint.rows = []
    assert asyncio.run(store().immutable_artifact_candidate_ids(ARTIFACT_ID)) == ()


# --- no overwrite, no delete --------------------------------------------------


def test_a_second_candidate_for_one_artifact_is_a_second_row_not_an_overwrite(
    chokepoint: Chokepoint,
) -> None:
    """The conflict target is the CANDIDATE id, never the artifact id.

    ``ON CONFLICT (artifact_id)`` would keep one row per artifact and silently
    discard every later candidate — including, half the time, the one the address
    CAS went on to select.
    """
    subject = store()

    async def scenario() -> None:
        for index in (1, 2):
            await subject.put_immutable_artifact_candidate(
                candidate_id=f"candidate-{index}", artifact_id=ARTIFACT_ID, dense=dense()
            )

    asyncio.run(scenario())
    inserts = chokepoint.of_verb("INSERT")
    assert [params for _, params in inserts] == [
        ("candidate-1", ARTIFACT_ID, dense()),
        ("candidate-2", ARTIFACT_ID, dense()),
    ]
    for statement, _ in inserts:
        assert "ON CONFLICT (candidate_id) DO NOTHING" in statement
        assert "ON CONFLICT (artifact_id)" not in statement


def test_the_seam_is_incapable_of_emitting_an_overwrite_or_a_delete(
    chokepoint: Chokepoint,
) -> None:
    """SET EQUALITY over every verb both methods can emit.

    Not "no DELETE appeared this time": the whole verb vocabulary of the seam is
    pinned, so adding an ``UPDATE``, ``DELETE``, ``TRUNCATE`` or ``DROP`` to either
    method fails here. The immutable-artifact store holds this adapter and must not
    be able to reach an unconditional write or a reclamation through it.
    """
    subject = store()

    async def scenario() -> None:
        await subject.put_immutable_artifact_candidate(
            candidate_id="candidate-1", artifact_id=ARTIFACT_ID, dense=dense()
        )
        await subject.immutable_artifact_candidate_ids(ARTIFACT_ID)

    asyncio.run(scenario())
    assert chokepoint.verbs() == {"CREATE TABLE", "CREATE INDEX", "INSERT", "SELECT"}


def test_the_candidate_path_never_creates_the_vector_extension_or_an_index_on_it(
    chokepoint: Chokepoint,
) -> None:
    """The seam is decoupled from the HNSW/GIN chunk path on purpose.

    A candidate is inert staging that nothing similarity-searches, so requiring the
    ``vector`` extension for it would couple an unqueried table to an extension it
    has no use for.
    """
    asyncio.run(
        store().put_immutable_artifact_candidate(
            candidate_id="candidate-1", artifact_id=ARTIFACT_ID, dense=dense()
        )
    )
    emitted = " ".join(statement for statement, _ in chokepoint.statements)
    assert "CREATE EXTENSION" not in emitted
    assert "hnsw" not in emitted
    assert "gin" not in emitted.lower()
    # Positive control: the candidate table itself IS created, so the exclusions
    # above are a real decoupling and not an empty recording.
    assert len(chokepoint.of_verb("CREATE TABLE")) == 1
