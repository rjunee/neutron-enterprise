# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""pgvector ``ChunkStore`` alternate (NE-091) — OPTIONAL; never imported by ne.brain.

A drop-in alternate to :class:`ne.brain.qdrant.QdrantChunkStore` backed by a
Postgres ``pgvector`` table, so a Postgres-first customer can run retrieval on the
platform Postgres with NO separate Qdrant service. Selected by config behind the
EXISTING ``ne.plugins.vector_store`` entry-point slot (name ``"pgvector"``);
Qdrant stays the byte-unchanged default.

Install the optional extra (``ne-brain[pgvector]``) to pull ``psycopg``. Nothing
in :mod:`ne.brain` imports this subpackage — importing ``ne.brain`` alone never
pulls in ``psycopg`` (enforced by ``test_pgvector_is_lazy_and_unexported``).
"""

from __future__ import annotations

from ne.brain.pgvector.store import PgvectorChunkStore

__all__ = ["PgvectorChunkStore"]
