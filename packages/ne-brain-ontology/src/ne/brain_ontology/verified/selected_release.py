# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Immutable selected-release staging, publication and pinning for governed local T3.

015A0-AUTHORITY-T3's freshness is NOT a connector heartbeat and NOT a schedule: a
``governed_local_t3`` source-bearing answer is fresh exactly when it reads the
atomically selected, complete, immutable release head over a backend that can prove
an immutable snapshot.  This module owns that machinery:

* :class:`MeridianReleaseDescriptor` — the credential-free release descriptor
  (manifest corpus version, manifest/fixture hash, loader-schema version, declared
  data-model hash, ontology/shapes version, ACL-graph assignment hash and the FIXED
  selected-release/no-overwrite algorithm version) and
  :func:`meridian_source_contract_identity`, T3's own manifest-backed
  ``SourceContractIdentity`` factory.  Value-changing any member changes the hash.
* :class:`MeridianReleaseStaging` — a release writer over a DISTINCT Store.  Staged
  graphs are create-or-verify-identical: a byte-identical restage is idempotent, a
  different-bytes restage is refused, and nothing may be staged after the seal.
* :class:`SelectedOxigraphRelease` — the sealed, READ-ONLY release.  It exposes no
  mutation method at all (structurally, not by raising), pins one backend object
  and one head revision, and refuses to read outside its own graph set.
* :class:`SelectedReleaseRegistry` — CAS head selection.  The dataset scope is
  ``UNKNOWN`` before the first publication and from the moment a release claim opens
  until that claim publishes a COMPLETE release; an incomplete or drifted publish
  selects nothing and leaves the scope ``UNKNOWN``.
* :func:`pinned_release` — one governed invocation pins ONE release instance/head
  before any ``DatasetSpec`` is built and rechecks the same head at publication.
* :func:`require_source_bearing_backend` — the exact Fuseki control: a backend that
  cannot prove the immutable snapshot makes source-bearing governed local T3
  unavailable BEFORE any HTTP I/O.

Import discipline (C3): ``ne.contracts`` only — the authority Writer, the durable
Postgres claim and the deployment construction id all live outside this package and
are supplied by the composition root as plain values.  Nothing here invents a
deployment id, a dataset-instance id or M365 lineage.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Annotated, Final
from typing import Any as _Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ne.brain_ontology.gateway import (
    DeclaredLiteral,
    ValidatingTripleStore,
    assert_read_only_sparql,
)
from ne.brain_ontology.ontology import OntologyVersion
from ne.brain_ontology.store.oxigraph import OxigraphBackend, ReadOnlyOxigraphBackend
from ne.brain_ontology.store.protocols import DatasetSpec, ImmutableReleaseReadBackend
from ne.contracts.source_authority import (
    GovernedLocalT3SourcePoint,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
    SourceReconciliationScope,
)

__all__ = [
    "GOVERNED_LOCAL_T3_SOURCE_KIND",
    "RELEASE_DESCRIPTOR_SCHEMA_VERSION",
    "SELECTED_RELEASE_ALGORITHM_VERSION",
    "GovernedLocalT3Instance",
    "GovernedLocalT3UnavailableError",
    "IncompleteReleaseError",
    "MeridianReleaseDescriptor",
    "MeridianReleaseStaging",
    "ReleaseClaim",
    "ReleaseGraph",
    "ReleaseHeadDriftError",
    "ReleaseOverwriteRefusedError",
    "ReleaseScopeError",
    "ReleaseSealedError",
    "SelectedOxigraphRelease",
    "SelectedReleaseRegistry",
    "SelectedReleaseVerifiedReader",
    "contributor_artifact_identity",
    "contributor_source_identity",
    "governed_local_t3_admitted_target_digest",
    "governed_local_t3_scope",
    "meridian_source_contract_identity",
    "pinned_release",
    "release_content_hash",
    "require_source_bearing_backend",
]

#: The only ``source_kind`` this producer may ever mint.  A ``m365-sharepoint``
#: identity is a DIFFERENT producer's output and is never fabricated here.
GOVERNED_LOCAL_T3_SOURCE_KIND: Final = "governed_local_t3"

#: The FIXED, non-configurable immutable-publication / no-overwrite algorithm version.
#: It is a member of the release descriptor and therefore of the SourceContract hash:
#: changing how a release is staged, sealed or CAS-selected moves the contract.
SELECTED_RELEASE_ALGORITHM_VERSION: Final = 1

#: Schema version of :class:`MeridianReleaseDescriptor` itself.
RELEASE_DESCRIPTOR_SCHEMA_VERSION: Final = 1

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Bounded = Annotated[str, Field(min_length=1, max_length=2048)]


def _length_prefixed(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(8, "big") + encoded


def _canonical_hash(domain: str, values: Sequence[str]) -> str:
    """Domain-tagged sha256 over individually length-prefixed raw UTF-8 fields.

    No delimiter joining, case folding or Unicode normalization: field boundaries
    survive the encoding, so two distinct field tuples can never share a preimage
    (``("ab", "c")`` and ``("a", "bc")`` hash differently) and no field value can
    smuggle a separator.  This mirrors the encoding rule the frozen
    ``ne.contracts.source_authority`` identities are specified with; the bytes are
    domain-separated from those identities by ``domain`` and are never presented as
    a contract identity — they are only ever fed into one as a ``descriptor_hash``.
    """
    material = b"".join((_length_prefixed(domain), *(_length_prefixed(v) for v in values)))
    return hashlib.sha256(material).hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- errors ------------------------------------------------------------------


class GovernedLocalT3UnavailableError(RuntimeError):
    """Source-bearing governed local T3 is unavailable on this backend/head."""


class ReleaseOverwriteRefusedError(RuntimeError):
    """A staged release graph may not be replaced with different bytes."""


class ReleaseSealedError(RuntimeError):
    """A sealed release is immutable: no further staging is possible."""


class IncompleteReleaseError(RuntimeError):
    """A release was sealed or published without its complete required graph set."""


class ReleaseHeadDriftError(RuntimeError):
    """The current selected head is not the head this operation was bound to."""


class ReleaseScopeError(RuntimeError):
    """A read addressed a graph outside the pinned release's own graph set."""


# --- descriptor + identities --------------------------------------------------


class GovernedLocalT3Instance(_Frozen):
    """Deployment context for one governed local dataset instance.

    Every member is issued OUTSIDE this package — the deployment construction id and
    the server-issued dataset-instance id come from the durable authority store, and
    the generation advances there when the configured dataset is deleted/recreated or
    replaced.  This package never mints or defaults one: a caller that has no
    instance has no authority to stamp contributors.
    """

    deployment_id: Bounded
    dataset_instance_id: Bounded
    generation: Annotated[int, Field(ge=1)]


class MeridianReleaseDescriptor(_Frozen):
    """The credential-free descriptor of ONE governed local T3 release.

    Safe to project into API composition: it carries hashes and versions only, never
    fixture bytes, credentials or a wall-clock/as-of value.  ``descriptor_hash`` is
    bound by construction over every other member, so value-changing any one of them
    changes the hash and therefore the ``SourceContractIdentity``.
    """

    descriptor_schema_version: Annotated[int, Field(ge=1)] = RELEASE_DESCRIPTOR_SCHEMA_VERSION
    dataset_name: Bounded
    corpus_version: Bounded
    manifest_hash: Sha256
    loader_schema_version: Annotated[int, Field(ge=1)]
    data_model_hash: Sha256
    ontology_shapes_version: Sha256
    acl_graph_assignment_hash: Sha256
    selected_release_algorithm_version: Annotated[int, Field(ge=1)] = (
        SELECTED_RELEASE_ALGORITHM_VERSION
    )
    descriptor_hash: Sha256 | Annotated[str, Field(max_length=0)] = ""

    @model_validator(mode="after")
    def _bind_descriptor_hash(self) -> MeridianReleaseDescriptor:
        expected = _canonical_hash(
            "ne.governed-local-t3.release-descriptor.v1",
            (
                str(self.descriptor_schema_version),
                self.dataset_name,
                self.corpus_version,
                self.manifest_hash,
                str(self.loader_schema_version),
                self.data_model_hash,
                self.ontology_shapes_version,
                self.acl_graph_assignment_hash,
                str(self.selected_release_algorithm_version),
            ),
        )
        if self.descriptor_hash and self.descriptor_hash != expected:
            raise ValueError("descriptor_hash does not match canonical release-descriptor bytes")
        object.__setattr__(self, "descriptor_hash", expected)
        return self


def meridian_source_contract_identity(
    descriptor: MeridianReleaseDescriptor,
) -> SourceContractIdentity:
    """T3's manifest-backed ``SourceContractIdentity`` factory.

    Separate from M365's ne-connect factory and consuming the same frozen contract
    type: the contract is the credential-free release descriptor, so a fixture,
    data-model, ontology/shapes, ACL-assignment, loader-schema or publication-algorithm
    change moves the contract hash and advances the contract epoch.
    """
    return SourceContractIdentity(
        source_kind=GOVERNED_LOCAL_T3_SOURCE_KIND,
        descriptor_schema_version=descriptor.descriptor_schema_version,
        descriptor_hash=descriptor.descriptor_hash,
    )


def contributor_source_identity(
    instance: GovernedLocalT3Instance,
    descriptor: MeridianReleaseDescriptor,
    contributor_key: str,
) -> SourceIdentity:
    """The ``governed_local_t3`` identity of ONE row/entity contributor.

    The typed point is the manifest-declared dataset name plus the contributor key,
    beneath the server-issued dataset-instance id and generation.  The release
    version, hashes and ACL assignment are release/artifact/contract fields — they
    are deliberately NOT folded into this identity.
    """
    return SourceIdentity(
        deployment_id=instance.deployment_id,
        source_kind=GOVERNED_LOCAL_T3_SOURCE_KIND,
        connector_instance_id=instance.dataset_instance_id,
        point=GovernedLocalT3SourcePoint(
            dataset_name=descriptor.dataset_name,
            contributor_key=contributor_key,
        ),
        generation=instance.generation,
    )


def contributor_artifact_identity(
    instance: GovernedLocalT3Instance,
    descriptor: MeridianReleaseDescriptor,
    contributor_key: str,
    *,
    content_snapshot_hash: str,
    acl_snapshot_hash: str,
    artifact_schema_version: int = 1,
) -> SourceContentArtifactIdentity:
    """The immutable content artifact for ONE contributor of this release."""
    return SourceContentArtifactIdentity(
        source_identity=contributor_source_identity(instance, descriptor, contributor_key),
        source_snapshot_hash=descriptor.manifest_hash,
        content_snapshot_hash=content_snapshot_hash,
        acl_snapshot_hash=acl_snapshot_hash,
        source_contract=meridian_source_contract_identity(descriptor),
        artifact_schema_version=artifact_schema_version,
    )


def governed_local_t3_admitted_target_digest(dataset_name: str) -> str:
    """The credential-free digest of the CONFIGURED governed dataset target.

    It covers the source kind and the manifest-declared dataset name and nothing
    else, which is what makes the dataset-instance claim behave the way the source
    model requires: a reload, a fixture VALUE change, a data-model/ontology/ACL
    change and a loader-schema change all keep this digest — and therefore the same
    server-issued instance id — while re-pointing the deployment at a DIFFERENT
    governed dataset changes it and cannot reuse the old instance's admitted
    history.  The fixture directory is deliberately absent: moving the same dataset
    to another mount is not a new dataset.
    """
    return _canonical_hash(
        "ne.governed-local-t3.admitted-target.v1",
        (GOVERNED_LOCAL_T3_SOURCE_KIND, dataset_name),
    )


def governed_local_t3_scope(
    instance: GovernedLocalT3Instance, dataset_name: str
) -> SourceReconciliationScope:
    """The reconciliation scope one release claim/CAS is exclusive over."""
    return SourceReconciliationScope(
        path=(
            GOVERNED_LOCAL_T3_SOURCE_KIND,
            instance.deployment_id,
            instance.dataset_instance_id,
            dataset_name,
        )
    )


# --- release content hashing --------------------------------------------------

Triple = tuple[str, str, "str | DeclaredLiteral"]


def release_content_hash(domain: str, triples: Sequence[Triple]) -> str:
    """Deterministic content hash over a triple set, independent of input order.

    Each object is hashed as a term KIND plus its lexical form plus its datatype, in
    separate fields.  The kind matters because a plain ``str`` object and a
    :class:`DeclaredLiteral` with the same text do not serialize to the same RDF term
    — the gateway applies an IRI-vs-literal shape heuristic to plain strings and never
    to a declared literal — so conflating them would let two different graphs share one
    content hash.  The datatype matters for the same reason: an ``xsd:integer`` ``"1"``
    and an untyped ``"1"`` are different content.  Sorting makes the hash a pure
    function of the SET, which is what makes a restart rebuild from retained bytes
    produce the identical release id rather than a new release.
    """
    fields: list[str] = []
    rows: list[tuple[str, str, str, str, str]] = []
    for subject, predicate, obj in triples:
        if isinstance(obj, DeclaredLiteral):
            rows.append(("declared-literal", subject, predicate, obj.lexical, obj.datatype or ""))
        else:
            rows.append(("plain-term", subject, predicate, obj, ""))
    for row in sorted(set(rows)):
        fields.extend(row)
    return _canonical_hash(domain, (str(len(rows)), *fields))


class ReleaseGraph(_Frozen):
    """One immutable, ACL-labelled named graph of a release."""

    label: Bounded
    graph_iri: Bounded
    content_hash: Sha256
    triple_count: Annotated[int, Field(ge=0)]


# --- the sealed, read-only selected release -----------------------------------


class SelectedOxigraphRelease:
    """A sealed, read-only, immutable Oxigraph release pinned by one head revision.

    There is no write method on this object and none on the backend it holds: the
    mutation surface is structurally absent, not refused at call time, so no product
    writer can be handed one by accident.  Reads are confined to the release's own
    contributor graphs plus its explicit, source-free schema graph set.
    """

    def __init__(
        self,
        *,
        descriptor: MeridianReleaseDescriptor,
        contributor_graphs: Sequence[ReleaseGraph],
        schema_graphs: Sequence[str],
        backend: ImmutableReleaseReadBackend,
    ) -> None:
        self._descriptor = descriptor
        self._contract = meridian_source_contract_identity(descriptor)
        self._contributor_graphs = tuple(sorted(contributor_graphs, key=lambda g: g.graph_iri))
        self._schema_graphs = tuple(sorted(set(schema_graphs)))
        self._backend = backend
        self._release_id = _canonical_hash(
            "ne.governed-local-t3.selected-release.v1",
            (
                str(SELECTED_RELEASE_ALGORITHM_VERSION),
                descriptor.descriptor_hash,
                self._contract.identity_hash,
                str(len(self._contributor_graphs)),
                *(
                    field
                    for graph in self._contributor_graphs
                    for field in (
                        graph.label,
                        graph.graph_iri,
                        graph.content_hash,
                        str(graph.triple_count),
                    )
                ),
                str(len(self._schema_graphs)),
                *self._schema_graphs,
            ),
        )

    @property
    def release_id(self) -> str:
        """The selected head revision: a pure function of the release's own bytes."""
        return self._release_id

    @property
    def descriptor(self) -> MeridianReleaseDescriptor:
        return self._descriptor

    @property
    def source_contract(self) -> SourceContractIdentity:
        return self._contract

    @property
    def contributor_graphs(self) -> tuple[ReleaseGraph, ...]:
        return self._contributor_graphs

    @property
    def schema_graphs(self) -> tuple[str, ...]:
        """The explicit, source-free schema graph set bound to this head."""
        return self._schema_graphs

    @property
    def graph_hashes(self) -> Mapping[str, str]:
        """``graph_iri -> content_hash`` for the complete immutable graph set."""
        return {graph.graph_iri: graph.content_hash for graph in self._contributor_graphs}

    @property
    def dataset(self) -> DatasetSpec:
        """The complete contributor-graph dataset of this release."""
        return DatasetSpec(default_graphs=tuple(g.graph_iri for g in self._contributor_graphs))

    def confine(self, dataset: DatasetSpec) -> DatasetSpec:
        """Return ``dataset`` unchanged, or refuse it if it leaves this release.

        Fail-closed both ways: an empty spec would mean "union of everything the
        backend holds" and is refused, and any graph outside the release's own
        contributor/schema graph set is refused.
        """
        addressed = tuple(dataset.default_graphs) + tuple(dataset.named_graphs)
        if not addressed:
            raise ReleaseScopeError(
                "a source-bearing governed local T3 read may not use the unscoped "
                "full-union dataset; it must address this release's graphs explicitly"
            )
        allowed = set(self.graph_hashes) | set(self._schema_graphs)
        outside = sorted(graph for graph in addressed if graph not in allowed)
        if outside:
            raise ReleaseScopeError(
                f"graphs {outside!r} are outside selected release {self._release_id}"
            )
        return dataset

    async def select(self, query: str, *, dataset: DatasetSpec) -> Sequence[Mapping[str, str]]:
        return await self._backend.select(query, dataset=self.confine(dataset))

    async def ask(self, query: str, *, dataset: DatasetSpec) -> bool:
        return await self._backend.ask(query, dataset=self.confine(dataset))

    async def construct_nt(self, query: str, *, dataset: DatasetSpec) -> bytes:
        return await self._backend.construct_nt(query, dataset=self.confine(dataset))


# --- the release writer -------------------------------------------------------


class MeridianReleaseStaging:
    """Builds ONE release into its own distinct Store, then seals it read-only.

    A release writer never touches the Store a selected release pins: the caller
    obtains a fresh gateway + backend pair from
    :func:`~ne.brain_ontology.gateway.build_release_staging_store`, stages the
    complete graph set through the SAME fail-closed SHACL gateway every other write
    uses, and seals.  Sealing hands the read-only wrapper out and drops the writable
    backend, so the staged Store has no reachable mutation path afterwards.
    """

    def __init__(self, gateway: ValidatingTripleStore, backend: OxigraphBackend) -> None:
        require_source_bearing_backend(gateway)
        self._gateway = gateway
        self._backend: OxigraphBackend | None = backend
        self._staged: dict[str, ReleaseGraph] = {}

    @property
    def staged_labels(self) -> tuple[str, ...]:
        return tuple(sorted(self._staged))

    async def stage_graph(
        self, *, label: str, graph_iri: str, triples: Sequence[Triple]
    ) -> ReleaseGraph:
        """Stage ONE immutable release graph: create, or verify identical.

        A byte-identical restage is idempotent and does NOT re-write the backend (an
        exact retry after a crash resumes rather than duplicating work).  A restage
        of the same graph with different content is refused — a selected release
        graph can never be overwritten.
        """
        if self._backend is None:
            raise ReleaseSealedError(
                f"release graph {label!r} cannot be staged: this release is already sealed"
            )
        content_hash = release_content_hash("ne.governed-local-t3.release-graph.v1", triples)
        existing = self._staged.get(label)
        if existing is not None:
            if existing.content_hash != content_hash or existing.graph_iri != graph_iri:
                raise ReleaseOverwriteRefusedError(
                    f"release graph {label!r} is already staged with content "
                    f"{existing.content_hash}; refusing to overwrite it with {content_hash}"
                )
            return existing
        await self._gateway.replace_acl_graph(triples, graph_iri=graph_iri)
        staged = ReleaseGraph(
            label=label,
            graph_iri=graph_iri,
            content_hash=content_hash,
            triple_count=len(set(triples)),
        )
        self._staged[label] = staged
        return staged

    async def seal(
        self,
        *,
        descriptor: MeridianReleaseDescriptor,
        required_labels: Sequence[str],
        schema_graphs: Sequence[str] = (),
    ) -> SelectedOxigraphRelease:
        """Seal the complete staged set into a read-only :class:`SelectedOxigraphRelease`.

        An incomplete release is refused and nothing is sealed — a partial release
        can never become a selectable head.
        """
        backend = self._backend
        if backend is None:
            raise ReleaseSealedError("this release is already sealed")
        required = tuple(sorted(set(required_labels)))
        missing = tuple(label for label in required if label not in self._staged)
        extra = tuple(label for label in self.staged_labels if label not in required)
        if missing or extra:
            raise IncompleteReleaseError(
                f"release {descriptor.dataset_name!r} staged {self.staged_labels!r} but requires "
                f"exactly {required!r} (missing={missing!r} unexpected={extra!r}); "
                "no head is selected"
            )
        self._backend = None
        return SelectedOxigraphRelease(
            descriptor=descriptor,
            contributor_graphs=tuple(self._staged.values()),
            schema_graphs=schema_graphs,
            backend=ReadOnlyOxigraphBackend(backend),
        )


# --- CAS head selection -------------------------------------------------------


class ReleaseClaim(_Frozen):
    """An open exclusive claim over one dataset scope; the scope is ``UNKNOWN`` while it lives."""

    scope: SourceReconciliationScope
    reconciliation_epoch: Annotated[int, Field(ge=1)]
    expected_head_revision: str | None = None


class SelectedReleaseRegistry:
    """The atomically selected release head for ONE governed local dataset scope.

    Freshness here is the head, not a timer: there is no scheduled task, no
    configurable cadence and no cached validation timestamp anywhere in this class.
    """

    def __init__(self, scope: SourceReconciliationScope) -> None:
        self._scope = scope
        self._selected: SelectedOxigraphRelease | None = None
        self._claim: ReleaseClaim | None = None
        self._epoch = 0

    @property
    def scope(self) -> SourceReconciliationScope:
        return self._scope

    @property
    def state(self) -> SourceAuthorityState:
        """``PRESENT`` only with a selected head and no open claim; else ``UNKNOWN``."""
        if self._selected is None or self._claim is not None:
            return SourceAuthorityState.UNKNOWN
        return SourceAuthorityState.PRESENT

    @property
    def head_revision(self) -> str | None:
        return None if self._selected is None else self._selected.release_id

    def claim(self) -> ReleaseClaim:
        """Open the exclusive release claim.  The scope becomes ``UNKNOWN`` at once."""
        if self._claim is not None:
            raise ReleaseHeadDriftError(
                f"scope {self._scope.scope_hash} already has an open release claim"
            )
        self._epoch += 1
        self._claim = ReleaseClaim(
            scope=self._scope,
            reconciliation_epoch=self._epoch,
            expected_head_revision=self.head_revision,
        )
        return self._claim

    def abandon(self, claim: ReleaseClaim) -> None:
        """Release a claim WITHOUT selecting a head.

        The old head is deliberately not restored: a load that failed before head
        selection leaves the old release and the partial new triples both
        unavailable until an exact retry publishes one complete release.
        """
        self._require_live_claim(claim)
        self._claim = None
        self._selected = None

    def publish(
        self, claim: ReleaseClaim, release: SelectedOxigraphRelease
    ) -> SelectedOxigraphRelease:
        """CAS-select ``release`` as the head, closing ``claim``.

        Publishing a release whose id equals the current head returns the ALREADY
        SELECTED instance: a restart that rebuilds the identical release from
        retained bytes is the same release, not a new one, and its new object
        identity is never mistaken for a new head.
        """
        self._require_live_claim(claim)
        if release.source_contract.source_kind != GOVERNED_LOCAL_T3_SOURCE_KIND:
            raise GovernedLocalT3UnavailableError(
                "refusing to select a release whose contract is not governed_local_t3"
            )
        if self.head_revision != claim.expected_head_revision:
            raise ReleaseHeadDriftError(
                f"head moved from {claim.expected_head_revision} to {self.head_revision} "
                "while this release was staged; nothing is selected"
            )
        if self._selected is not None and self._selected.release_id == release.release_id:
            self._claim = None
            return self._selected
        self._selected = release
        self._claim = None
        return release

    def pin(self) -> SelectedOxigraphRelease:
        """Pin the current head for one governed invocation, or refuse.

        Refusing is the ``UNKNOWN`` outcome: no head yet, or a release claim is open.
        """
        selected = self._selected
        if selected is None or self._claim is not None:
            raise GovernedLocalT3UnavailableError(
                f"governed local T3 scope {self._scope.scope_hash} is "
                f"{SourceAuthorityState.UNKNOWN.value}: no complete release head is selected"
            )
        return selected

    def _require_live_claim(self, claim: ReleaseClaim) -> None:
        if self._claim is None or self._claim != claim:
            raise ReleaseHeadDriftError("this release claim is not the live claim for its scope")


def require_source_bearing_backend(store: object) -> None:
    """Refuse a store that cannot prove an immutable release snapshot, before any I/O.

    This runs on the store OBJECT and reads one boolean capability, so a Fuseki-backed
    gateway is refused without a single HTTP request being issued.  Matching hashes
    observed before and after an unfenced query would not be proof, so there is no
    probe to make here — the capability is declared by the backend class.
    """
    if not getattr(store, "supports_immutable_release_snapshot", False):
        raise GovernedLocalT3UnavailableError(
            f"{type(store).__name__} cannot prove an immutable selected-release snapshot: "
            "source-bearing governed local T3 is unavailable on this backend"
        )


@contextmanager
def pinned_release(registry: SelectedReleaseRegistry) -> Iterator[SelectedOxigraphRelease]:
    """Pin ONE release instance/head for the whole of one governed invocation.

    The pin is taken BEFORE any ``DatasetSpec`` is constructed and the same head is
    rechecked on exit, so a head that drifts mid-invocation — between the primary,
    count and citation queries — fails the invocation instead of publishing a result
    assembled across two releases.
    """
    release = registry.pin()
    yield release
    if registry.head_revision != release.release_id:
        raise ReleaseHeadDriftError(
            f"selected head moved from {release.release_id} to {registry.head_revision} "
            "during this invocation; the result is withheld"
        )


class SelectedReleaseVerifiedReader:
    """The ONLY store a source-bearing governed local T3 verified answer may read.

    It satisfies the package-local ``VerifiedReadStore`` surface — ``sparql_scoped``
    plus ``ontology_version`` — and nothing else.  It holds ONE
    :class:`SelectedOxigraphRelease` for its whole life, so the release instance and
    head are inseparable from every read reachable through it: a direct
    ``/v1/verified/invoke`` and a governed Tool invocation are the same object's
    reads, and neither can fall back to a mutable serving store, a seeded store or a
    request-local fake.  Every query passes the IDENTICAL C12 read gate the gateway
    applies (:func:`~ne.brain_ontology.gateway.assert_read_only_sparql`, called, not
    restated), and every dataset is confined to the release's own graph set.

    There is no write method here and none on the release it holds.
    """

    def __init__(self, release: SelectedOxigraphRelease, ontology_version: OntologyVersion) -> None:
        self._release = release
        self._ontology_version = ontology_version

    @property
    def release(self) -> SelectedOxigraphRelease:
        return self._release

    @property
    def release_id(self) -> str:
        """The pinned head revision every read through this object addresses."""
        return self._release.release_id

    async def sparql_scoped(
        self, query: str, *, dataset: DatasetSpec
    ) -> Sequence[Mapping[str, _Any]]:
        form = assert_read_only_sparql(query)
        if form == "SELECT":
            return await self._release.select(query, dataset=dataset)
        return [{"ask": await self._release.ask(query, dataset=dataset)}]

    def ontology_version(self) -> OntologyVersion:
        return self._ontology_version
