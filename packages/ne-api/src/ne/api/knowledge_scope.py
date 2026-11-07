# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The Agent-declared knowledge scope, resolved into a retrieval filter.

WHY THIS MODULE EXISTS. ``deploy/agents/registry.toml`` lets an Agent declare
``knowledge_sources = [{ source_id = "...", tiers = [...] }]``. Until this
module, that declaration was consumed ONLY by admission/readiness
(``ne.api.agent_catalog``, ``ne.api.platform``'s dependency snapshot) — it
decided whether the Agent was AVAILABLE and never once narrowed what the Agent
could READ. ``read.knowledge.search`` searched the entire chunk store, so an
Agent granted one governed source could retrieve from every other corpus in the
deployment. The declared boundary was decorative. This module makes it real.

WHAT A LOGICAL SOURCE ID IS. The registry names a source LOGICALLY
(``"meridian-servicing"``) — a governance-facing name an operator writes and
reads. Chunks carry the CONCRETE producer name in ``metadata["connector"]``,
authored by the connector bridge from its ODCS contract source
(``ne.connect.bridge.default_record_mapper``): the M365 connector writes
``"m365-sharepoint"`` (``M365ConnectorSettings.source_name``), a fixture
connector writes its own. :data:`KNOWLEDGE_SOURCE_CONNECTORS` is the ONE place
that resolution lives.

FAIL LOUDLY, NEVER WIDEN. This is the whole point, and the reason the bug hid
for as long as it did: a scope that resolves to nothing must REFUSE, never
degrade to an unfiltered search.  :meth:`KnowledgeScopeResolver.filters` raises
:class:`KnowledgeScopeUnresolved` when a bound scope names an unknown source id,
or when every id it names resolves to no chunk-store corpus at all. It never
returns ``None`` and never returns ``{}`` — both of which the store's filter
grammar reads as "no filter".

SCOPE IS NOT AUTHORIZATION. The resolved filter is AND-combined with the ACL
trimming hook by :func:`ne.brain.retriever.and_combine_filters`, which is
narrowing-only and rejects ``$``-prefixed caller keys outright. Scope rides ON
TOP of ``PrincipalAclFilter`` and the current-source/ACL re-resolution in
``ne.brain.current_source_filter`` — it can only ever remove candidates a
principal was already entitled to see, never add one they were not.

THE CARRIER. :data:`RUN_KNOWLEDGE_SCOPE` is an ambient carrier shaped exactly
like ``ne.api.agent_skills.RUN_SKILL_PINS`` (itself shaped like ne-api's
``TURN_PRINCIPAL``). ``ne.worker.run_execution`` binds it from the Agent
definition around the agentic run, in the same ``with`` block that binds the
Run's frozen Skill pins, and ``ne.api.tools``' ``read.knowledge.search`` handler
reads it per call. Three states, all distinct and all deliberate:

* ``None`` — NO Agent Run is in scope (an interactive chat turn). The search is
  unfiltered, exactly as before; the principal's own ACL is the only boundary,
  which is correct because a human searching their own governed corpus is an
  authorization question, not an Agent-scope question.
* a NON-EMPTY frozenset — an Agent Run declaring those logical sources. The
  search is filtered to their connectors, or REFUSED.
* an EMPTY frozenset — an Agent Run declaring NO knowledge source. The search is
  REFUSED. It is emphatically not "search everything".
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType

from ne.api.agent_manifest import AgentManifestDefinition

#: The chunk metadata key carrying the concrete producer name. Authored by
#: ``ne.connect.bridge.default_record_mapper`` from the ODCS contract source and
#: consumed as a payload path (``metadata.connector``) by the store's filter
#: grammar — the same key ``ne.brain.source_health.CONNECTOR_METADATA_KEY`` and
#: ``ne.brain.write.writer`` already read.
CONNECTOR_METADATA_KEY = "connector"

#: The store filter path for :data:`CONNECTOR_METADATA_KEY`. ``QdrantChunkStore``
#: writes ``chunk.model_dump(mode="json")`` as the point payload, so a chunk's
#: metadata lives under the ``metadata`` object and ``_build_filter`` uses the key
#: VERBATIM as the payload field path.
CONNECTOR_FILTER_KEY = f"metadata.{CONNECTOR_METADATA_KEY}"

#: Logical registry source id -> the concrete ``metadata["connector"]`` values it
#: names in the chunk store.
#:
#: An EMPTY tuple is a deliberate, meaningful entry: the source is known and
#: governed but serves NO chunk-store corpus, so it contributes nothing to a
#: ``read.knowledge.search`` filter. ``meridian-governed-local-t3`` is exactly
#: that — a Tier-3 Oxigraph release read by the deterministic verified-answer
#: ``read.kg.*`` tools, which never touch the chunk store. Declaring it alongside
#: a searchable source narrows to the searchable one; declaring it ALONE leaves
#: the union empty and the search is refused (see :meth:`KnowledgeScopeResolver.
#: filters`) rather than silently widened to every corpus.
#:
#: An id absent from this table is an ERROR, not an implicit allow-all: adding a
#: knowledge source to an Agent that searches means adding its resolution here.
KNOWLEDGE_SOURCE_CONNECTORS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        # ``ne.api.platform._MERIDIAN_SOURCE_ID`` ->
        # ``ne.connect.m365.config.M365ConnectorSettings.source_name``.
        "meridian-servicing": ("m365-sharepoint",),
        # ``ne.api.platform._MERIDIAN_LOCAL_T3_SOURCE_ID`` — graph-only, see above.
        "meridian-governed-local-t3": (),
    }
)


class KnowledgeScopeUnresolved(RuntimeError):
    """A bound Agent knowledge scope names no searchable corpus.

    Raised INSTEAD of searching. The handler that catches nothing lets this
    surface as an audited ``tool.execute`` error, which is the loud failure the
    scope contract requires — a silent widening to the whole store is the exact
    defect this exception exists to make impossible.
    """


@dataclass(frozen=True, slots=True)
class KnowledgeScopeResolver:
    """Resolve declared logical source ids into a narrowing store filter."""

    connectors: Mapping[str, tuple[str, ...]] = KNOWLEDGE_SOURCE_CONNECTORS

    def filters(self, source_ids: Iterable[str]) -> dict[str, list[str]]:
        """The AND-able store filter for ``source_ids``, or raise.

        Never returns ``None`` and never returns an empty mapping: both are read
        as "unfiltered" by ``ne.brain.qdrant._build_filter``, so returning either
        would reintroduce the silent widening. The returned connector list is
        sorted for a deterministic filter (and therefore a deterministic
        ``ne.brain.lineage`` cursor fingerprint).
        """
        declared = tuple(dict.fromkeys(source_ids))
        unknown = sorted(source_id for source_id in declared if source_id not in self.connectors)
        if unknown:
            raise KnowledgeScopeUnresolved(
                "agent knowledge scope names unresolvable source id(s) "
                f"{unknown!r}; refusing to search rather than widening scope"
            )
        resolved = sorted(
            {connector for source_id in declared for connector in self.connectors[source_id]}
        )
        if not resolved:
            raise KnowledgeScopeUnresolved(
                f"agent knowledge scope {list(declared)!r} resolves to no searchable "
                "corpus; refusing to search rather than widening scope"
            )
        return {CONNECTOR_FILTER_KEY: resolved}


#: The single shared resolver over :data:`KNOWLEDGE_SOURCE_CONNECTORS`. Frozen and
#: slotted, so it is safe as a function default; a test supplies its own table by
#: constructing a :class:`KnowledgeScopeResolver` directly.
DEFAULT_KNOWLEDGE_SCOPE_RESOLVER = KnowledgeScopeResolver()


#: The current Agent Run's declared logical knowledge source ids. ``None`` ⇒ no Run.
RUN_KNOWLEDGE_SCOPE: ContextVar[frozenset[str] | None] = ContextVar(
    "ne_api_run_knowledge_scope", default=None
)


def current_run_knowledge_scope() -> frozenset[str] | None:
    """The seam ``ne.api.tools``' ``read.knowledge.search`` handler reads per call."""
    return RUN_KNOWLEDGE_SCOPE.get()


@contextmanager
def run_knowledge_scope(source_ids: Iterable[str]) -> Iterator[None]:
    """Bind ``source_ids`` as the current Run's declared scope for the enclosed scope.

    An empty iterable binds an EMPTY frozenset — a Run that declared no knowledge
    source — which refuses at search time. It is never confused with ``None``.
    """
    token = RUN_KNOWLEDGE_SCOPE.set(frozenset(source_ids))
    try:
        yield
    finally:
        RUN_KNOWLEDGE_SCOPE.reset(token)


def declared_source_ids(definition: AgentManifestDefinition) -> frozenset[str]:
    """Every logical source id an Agent definition declares, required or optional.

    Tiers are deliberately NOT consulted. A tier says which READ SHAPE the
    deployment admitted for a source (T1 governed search, T2 graph paths, T3
    verified templates), not which corpus the chunks live in — so an Agent
    granted t1+t2 on one source is scoped to that one source's corpus either way.
    ``optional_knowledge_sources`` is included because an optional grant that IS
    admitted is a source the Agent may genuinely read.
    """
    return frozenset(
        grant.source_id
        for grant in (*definition.knowledge_sources, *definition.optional_knowledge_sources)
    )
