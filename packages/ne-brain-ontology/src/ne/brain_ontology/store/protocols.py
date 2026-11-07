# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Package-local rich backend protocol for the T3 triple store (NE-085a).

The FROZEN ``ne.contracts.knowledge.TripleStore`` surface is deliberately minimal
(``sparql`` + ``insert_triples``). The richer operational surface a real backend
needs — named graphs, N-Triples I/O, dataset-scoped reads, graph-store CRUD —
lives here as a package-local structural :class:`TripleStoreBackend`, mirroring
the ``ne.brain.trimming`` package-local-Protocol pattern (the frozen contract is
too narrow, the fix is a local Protocol + composition-root injection).

``TripleStoreBackend`` is PRIVATE to ``ne.brain_ontology``: it is never exported
from the package ``__init__``, never registered as a plugin, and only ever held
inside the :class:`~ne.brain_ontology.gateway.ValidatingTripleStore` — exactly
the handler-privacy rule of ``ne.runtime`` ``tools.py`` (the raw writer exists
only inside the governed facade). Concrete surface decided per brain-tier3-spec
§4.2 (adapted: N-Triples/Turtle over the abstract ``RdfFormat`` sketch).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class DatasetSpec(BaseModel):
    """The FROM / FROM NAMED graph set a query may see.

    Constructed ONLY by trusted code (the gateway's full-union read surface here;
    C7 principal scoping is 085c). An EMPTY ``default_graphs`` means "union of all
    graphs" — the dev/steward default; a non-empty set restricts visibility.
    """

    model_config = ConfigDict(frozen=True)

    default_graphs: tuple[str, ...] = ()
    named_graphs: tuple[str, ...] = ()


@runtime_checkable
class ImmutableReleaseReadBackend(Protocol):
    """The READ-ONLY capability a governed local-T3 source-bearing read may use.

    Structurally this is :class:`TripleStoreBackend` MINUS every mutation method.
    A backend that satisfies it can still be a full read/write backend — the
    capability that matters for 015A0-AUTHORITY-T3 is
    :attr:`supports_immutable_release_snapshot`: it is ``True`` only for a backend
    that can hand a governed read one Store object whose bytes cannot change for
    the lifetime of that object (the embedded oxigraph Store). An out-of-process
    HTTP dataset cannot prove it, so ``FusekiBackend`` declares ``False`` and
    source-bearing governed local T3 is UNAVAILABLE on it *before* any HTTP I/O —
    matching hashes observed before and after an unfenced query are not proof.
    """

    supports_immutable_release_snapshot: ClassVar[bool]

    async def select(self, query: str, *, dataset: DatasetSpec) -> Sequence[Mapping[str, str]]: ...

    async def ask(self, query: str, *, dataset: DatasetSpec) -> bool: ...

    async def construct_nt(self, query: str, *, dataset: DatasetSpec) -> bytes: ...

    async def graphs(self) -> Sequence[str]: ...

    async def health(self) -> bool: ...


#: Every mutation method name on :class:`TripleStoreBackend`. A read-only release
#: wrapper must expose NONE of them; the no-overwrite tests assert against this
#: exact set rather than a hand-copied subset.
BACKEND_MUTATION_METHODS: Final[frozenset[str]] = frozenset(
    {
        "insert_nt",
        "load_turtle",
        "replace_graph_turtle",
        "delete_graph",
    }
)


@runtime_checkable
class TripleStoreBackend(Protocol):
    """Raw triple-store backend. PRIVATE — never plugin-registered, never in __all__.

    Every I/O-crossing method is async (repo convention). Reads are dataset-scoped
    via :class:`DatasetSpec`; writes target one named graph. ``insert_nt`` and
    ``load_turtle`` both add data; ``replace_graph_turtle`` is destructive (PUT
    semantics) for idempotent hot-reload.
    """

    async def select(self, query: str, *, dataset: DatasetSpec) -> Sequence[Mapping[str, str]]: ...

    async def ask(self, query: str, *, dataset: DatasetSpec) -> bool: ...

    async def construct_nt(self, query: str, *, dataset: DatasetSpec) -> bytes: ...

    async def insert_nt(self, data: bytes, *, graph_iri: str) -> None: ...

    async def load_turtle(self, data: bytes, *, graph_iri: str) -> None: ...

    async def replace_graph_turtle(self, data: bytes, *, graph_iri: str) -> None: ...

    async def delete_graph(self, graph_iri: str) -> None: ...

    async def graphs(self) -> Sequence[str]: ...

    async def health(self) -> bool: ...
