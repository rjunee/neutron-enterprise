# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C7 principal-scoped dataset construction for verified answers (NE-085c).

The query text carries NO ``FROM`` (banned at load); the graphs a request may see
are built HERE, by trusted code, from the principal's expanded ACL labels
INTERSECTED with the template's declared ``graph_classes``. The label algebra
mirrors ``ne.brain.trimming`` VERBATIM WITHOUT importing it (the brain-ontology
lane forbids ``ne.brain``, C3): the label set ALWAYS contains ``principal.id`` and
is never empty, and an expander exception PROPAGATES (deny-by-erroring).

Fail-closed inheritance: an empty class∩label intersection raises
:class:`VerifiedAnswerScopeError` — it NEVER widens to the full union
(``graphs_for_labels`` also refuses an empty label set). ``named_graphs`` stays
``()`` so a ``GRAPH`` pattern would match nothing — a belt over the load-time
``GRAPH`` keyword ban.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ne.brain_ontology.acl import graphs_for_labels
from ne.brain_ontology.store.protocols import DatasetSpec
from ne.brain_ontology.verified.errors import VerifiedAnswerScopeError
from ne.brain_ontology.verified.selected_release import SelectedOxigraphRelease
from ne.brain_ontology.verified.templates import QueryTemplate
from ne.contracts.types import Principal

__all__ = [
    "GroupExpander",
    "principal_labels",
    "release_scoped_dataset",
    "scoped_dataset",
    "source_authorized_dataset",
]


@runtime_checkable
class GroupExpander(Protocol):
    """Principal → its full ACL match-set (self + transitive groups + well-known labels).

    The package-local structural twin of ``ne.brain.trimming.GroupExpander`` /
    ``ne.permissions.expand.PrincipalExpander`` — injected at the composition root,
    NEVER imported here (C3). ``expand`` is SYNC (the expander is pure indexed SQL).
    """

    def expand(self, principal: Principal | str) -> frozenset[str]: ...


def principal_labels(principal: Principal, expander: GroupExpander) -> frozenset[str]:
    """The ACL label set for ``principal`` — always includes ``principal.id``, never empty.

    An expander exception PROPAGATES (deny-by-erroring, the trimming discipline); this
    never catches-and-falls-back to an unscoped result.
    """
    return frozenset(expander.expand(principal)) | {principal.id}


def scoped_dataset(
    principal: Principal, expander: GroupExpander, template: QueryTemplate
) -> DatasetSpec:
    """Build the C7 :class:`DatasetSpec` for ``principal`` under ``template``.

    The dataset unions ONLY the per-ACL named graphs for the principal's labels that
    are EXACTLY one of the template's declared ``graph_classes`` — the set
    intersection ``principal labels ∩ template graph_classes``. Matching is exact
    string equality (NEVER a prefix): the underlying lift partitions triples into one
    named graph per EXACT ACL label (``ne.brain_ontology.acl``), and ACL labels carry
    no hierarchical separator, so a prefix match (e.g. ``alphabet`` for ``alpha``)
    would over-return an unrelated graph the intersection semantics never grant. An
    empty intersection is a fail-closed denial (never the full union).
    """
    labels = principal_labels(principal, expander)
    graph_classes = frozenset(template.graph_classes)
    matched = [label for label in labels if label in graph_classes]
    if not matched:
        raise VerifiedAnswerScopeError(
            f"principal {principal.id!r} holds no label matching template "
            f"{template.template_id!r} graph_classes {template.graph_classes!r} — "
            "fail-closed under-return (never the full union)"
        )
    # named_graphs stays () — GRAPH patterns match nothing (belt over the keyword ban).
    return graphs_for_labels(matched)


def source_authorized_dataset(
    principal: Principal,
    expander: GroupExpander,
    template: QueryTemplate,
    *,
    authorized_contributor_graphs: Sequence[str],
) -> DatasetSpec:
    """Conjoin a template capability with source-native row ACL graphs.

    ``template.graph_classes`` are product capabilities (for example
    ``bank.customer.read``), while a governed connector retains the source's own
    ACL labels on each row (for example an Entra group object id).  The injected
    expander is the governed bridge between those vocabularies: it carries both
    the verified capability and every source-native grant reachable from it.

    The capability remains an exact, mandatory gate.  Only after it succeeds do
    we intersect the principal's complete expanded graph set with the lift's
    admitted contributor graphs.  Neither side can authorize a graph alone, and
    an empty intersection fails closed.
    """
    labels = principal_labels(principal, expander)
    graph_classes = frozenset(template.graph_classes)
    if not any(label in graph_classes for label in labels):
        raise VerifiedAnswerScopeError(
            f"principal {principal.id!r} holds no label matching template "
            f"{template.template_id!r} graph_classes {template.graph_classes!r} — "
            "fail-closed under-return (never the full union)"
        )
    authorized = frozenset(authorized_contributor_graphs)
    if not authorized:
        raise VerifiedAnswerScopeError("source_authority_unavailable")
    expanded = graphs_for_labels(labels)
    selected = tuple(graph for graph in expanded.default_graphs if graph in authorized)
    if not selected:
        raise VerifiedAnswerScopeError("source_authority_unavailable")
    return DatasetSpec(default_graphs=selected, named_graphs=())


def release_scoped_dataset(
    release: SelectedOxigraphRelease,
    principal: Principal,
    expander: GroupExpander,
    template: QueryTemplate,
    *,
    authorized_contributor_graphs: Sequence[str] | None = None,
) -> DatasetSpec:
    """The C7 dataset for a SOURCE-BEARING governed local T3 read.

    ``release`` is the FIRST parameter because the ordering is the invariant: one
    read-only :class:`SelectedOxigraphRelease` instance/head must already be pinned
    before any ``DatasetSpec`` exists, so a caller cannot build a dataset first and
    look for a head afterwards.  The principal trim is computed exactly as
    :func:`scoped_dataset` computes it, then confined to the pinned release's own
    graph set — a label the principal legitimately holds whose graph is not part of
    THIS release is a fail-closed denial, never a read that silently crosses a
    release boundary.
    """
    confined = release.confine(scoped_dataset(principal, expander, template))
    if authorized_contributor_graphs is None:
        return confined
    authorized = frozenset(authorized_contributor_graphs)
    if not authorized:
        raise VerifiedAnswerScopeError("source_authority_unavailable")
    selected = tuple(graph for graph in confined.default_graphs if graph in authorized)
    if not selected:
        raise VerifiedAnswerScopeError("source_authority_unavailable")
    return DatasetSpec(default_graphs=selected, named_graphs=())
