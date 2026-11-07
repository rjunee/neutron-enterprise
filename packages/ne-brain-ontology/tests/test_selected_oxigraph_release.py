# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-T3: immutable selected-release staging, publication and pinning.

These controls own the STORE half of the unit: the distinct-Store release writer, the
create-or-verify-identical no-overwrite rule, the read-only selected release, CAS head
selection with a mid-invocation head recheck, and the exact Fuseki
source-bearing-unavailable / no-HTTP guard.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from ne.brain_ontology.acl import graph_iri_for_label
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import ValidatingTripleStore, build_release_staging_store
from ne.brain_ontology.quarantine import SqliteTripleQuarantine
from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
from ne.brain_ontology.store.fuseki import FusekiBackend
from ne.brain_ontology.store.oxigraph import OxigraphBackend, ReadOnlyOxigraphBackend
from ne.brain_ontology.store.protocols import (
    BACKEND_MUTATION_METHODS,
    DatasetSpec,
    ImmutableReleaseReadBackend,
    TripleStoreBackend,
)
from ne.brain_ontology.verified.errors import VerifiedAnswerScopeError
from ne.brain_ontology.verified.meridian import (
    MERIDIAN_GRAPH_LABELS,
    MeridianReleaseBuild,
    build_meridian_release,
    publish_meridian_release,
)
from ne.brain_ontology.verified.scope import release_scoped_dataset
from ne.brain_ontology.verified.selected_release import (
    GovernedLocalT3Instance,
    GovernedLocalT3UnavailableError,
    IncompleteReleaseError,
    MeridianReleaseStaging,
    ReleaseHeadDriftError,
    ReleaseOverwriteRefusedError,
    ReleaseScopeError,
    ReleaseSealedError,
    SelectedOxigraphRelease,
    SelectedReleaseRegistry,
    governed_local_t3_scope,
    pinned_release,
    require_source_bearing_backend,
)
from ne.brain_ontology.verified.templates import load_template_registry
from ne.contracts.source_authority import SourceAuthorityState

from .verified_helpers import FIXTURES_DIR, NullSink, StubExpander, principal

_FIXTURE = Path(__file__).resolve().parents[3] / "tools" / "meridian" / "fixtures" / "meridian-v1"

_INSTANCE = GovernedLocalT3Instance(
    deployment_id="deployment-alpha",
    dataset_instance_id="meridian-instance-0001",
    generation=1,
)
_SETTINGS = OntologySettings(include_ontology_graph=False)
_ALL_ROWS = "SELECT ?s ?p ?o WHERE { ?s ?p ?o }"


def _registry(build: MeridianReleaseBuild) -> SelectedReleaseRegistry:
    return SelectedReleaseRegistry(
        governed_local_t3_scope(_INSTANCE, build.descriptor.dataset_name)
    )


def _revalued_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "revalued"
    shutil.copytree(_FIXTURE, target)
    banking = json.loads((target / "banking.json").read_bytes())
    banking["customers"][0]["address"]["county"] = "Marion"
    raw = json.dumps(banking, indent=2).encode("utf-8")
    (target / "banking.json").write_bytes(raw)
    manifest = json.loads((target / "manifest.json").read_bytes())
    manifest["sha256"]["banking.json"] = hashlib.sha256(raw).hexdigest()
    (target / "manifest.json").write_bytes(json.dumps(manifest, indent=2).encode("utf-8"))
    return target


def _fuseki_gateway(recorded: list[httpx.Request]) -> ValidatingTripleStore:
    """A Fuseki-backed gateway whose transport FAILS the test if it is ever called."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        raise AssertionError(f"no HTTP request may be issued: {request.method} {request.url}")

    backend = FusekiBackend(
        "http://fuseki.invalid:3030/ne",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_attempts=0,
    )
    return ValidatingTripleStore(
        backend,
        build_validator(_SETTINGS),
        SqliteTripleQuarantine(),
        NullSink(),
        settings=_SETTINGS,
        config_hash="e" * 64,
        compiled_shapes=load_compiled_shapes(),
    )


# --- the read-only selected release --------------------------------------------


def test_selected_release_exposes_no_backend_mutation_method() -> None:
    """The mutation surface is structurally absent, not refused at call time.

    A product writer cannot be handed a selected release or its backend by accident:
    neither satisfies ``TripleStoreBackend``, and neither carries ANY name in the
    backend's mutation method set.
    """

    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        release = await publish_meridian_release(_registry(build), build, settings=_SETTINGS)
        backend = ReadOnlyOxigraphBackend(OxigraphBackend())
        assert BACKEND_MUTATION_METHODS
        for name in BACKEND_MUTATION_METHODS:
            assert not hasattr(release, name), name
            assert not hasattr(backend, name), name
        assert not isinstance(backend, TripleStoreBackend)
        assert isinstance(backend, ImmutableReleaseReadBackend)
        # The control that must PASS: a full backend still satisfies both.
        assert isinstance(OxigraphBackend(), TripleStoreBackend)

    asyncio.run(scenario())


def test_selected_release_refuses_a_read_outside_its_own_graph_set() -> None:
    """A pinned release can only address its own contributor and schema graphs."""

    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        release = await publish_meridian_release(_registry(build), build, settings=_SETTINGS)
        rows = await release.select(_ALL_ROWS, dataset=release.dataset)
        assert len(rows) == sum(len(t) for t in build.graph_triples.values())
        with pytest.raises(ReleaseScopeError, match="outside selected release"):
            await release.select(
                _ALL_ROWS, dataset=DatasetSpec(default_graphs=("urn:ne:graph:acl:other",))
            )

    asyncio.run(scenario())


def test_selected_release_refuses_the_unscoped_full_union_dataset() -> None:
    """An empty ``DatasetSpec`` means "everything the backend holds" and is fail-closed."""

    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        release = await publish_meridian_release(_registry(build), build, settings=_SETTINGS)
        with pytest.raises(ReleaseScopeError, match="unscoped full-union"):
            await release.select(_ALL_ROWS, dataset=DatasetSpec())

    asyncio.run(scenario())


def test_selected_release_binds_the_exact_contributor_graph_set_and_hashes() -> None:
    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        release = await publish_meridian_release(_registry(build), build, settings=_SETTINGS)
        assert set(release.graph_hashes) == {
            graph_iri_for_label(label) for label in MERIDIAN_GRAPH_LABELS
        }
        assert release.dataset == DatasetSpec(default_graphs=tuple(sorted(release.graph_hashes)))
        assert release.schema_graphs == ()
        assert release.descriptor == build.descriptor

    asyncio.run(scenario())


# --- no-overwrite staging -------------------------------------------------------


def _staging() -> MeridianReleaseStaging:
    gateway, backend = build_release_staging_store(_SETTINGS)
    return MeridianReleaseStaging(gateway, backend)


_ONE: tuple[tuple[str, str, str], ...] = (
    (
        "urn:ne:meridian:customer:mer-cus-001",
        "https://neutron.enterprise/schema/meridian/county",
        "Alpha",
    ),
)
_OTHER: tuple[tuple[str, str, str], ...] = (
    (
        "urn:ne:meridian:customer:mer-cus-001",
        "https://neutron.enterprise/schema/meridian/county",
        "Beta",
    ),
)


def test_staging_the_same_release_graph_with_identical_bytes_is_idempotent() -> None:
    """The exact-retry control that must PASS: a resumed stage re-verifies, not re-writes."""

    async def scenario() -> None:
        staging = _staging()
        label = MERIDIAN_GRAPH_LABELS[0]
        first = await staging.stage_graph(
            label=label, graph_iri=graph_iri_for_label(label), triples=_ONE
        )
        second = await staging.stage_graph(
            label=label, graph_iri=graph_iri_for_label(label), triples=tuple(reversed(_ONE))
        )
        assert second == first
        assert staging.staged_labels == (label,)

    asyncio.run(scenario())


def test_staging_a_release_graph_with_different_bytes_is_refused() -> None:
    async def scenario() -> None:
        staging = _staging()
        label = MERIDIAN_GRAPH_LABELS[0]
        await staging.stage_graph(label=label, graph_iri=graph_iri_for_label(label), triples=_ONE)
        with pytest.raises(ReleaseOverwriteRefusedError, match="refusing to overwrite"):
            await staging.stage_graph(
                label=label, graph_iri=graph_iri_for_label(label), triples=_OTHER
            )

    asyncio.run(scenario())


def test_a_sealed_release_cannot_be_staged_into() -> None:
    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        staging = _staging()
        for label in MERIDIAN_GRAPH_LABELS:
            await staging.stage_graph(
                label=label,
                graph_iri=graph_iri_for_label(label),
                triples=build.graph_triples[label],
            )
        await staging.seal(descriptor=build.descriptor, required_labels=MERIDIAN_GRAPH_LABELS)
        with pytest.raises(ReleaseSealedError, match="already sealed"):
            await staging.stage_graph(
                label=MERIDIAN_GRAPH_LABELS[0],
                graph_iri=graph_iri_for_label(MERIDIAN_GRAPH_LABELS[0]),
                triples=_ONE,
            )

    asyncio.run(scenario())


def test_an_incomplete_release_is_refused_and_selects_no_head() -> None:
    """A partial release never becomes a selectable head, and the scope stays UNKNOWN."""

    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        registry = _registry(build)
        claim = registry.claim()
        staging = _staging()
        for label in MERIDIAN_GRAPH_LABELS[:-1]:
            await staging.stage_graph(
                label=label,
                graph_iri=graph_iri_for_label(label),
                triples=build.graph_triples[label],
            )
        with pytest.raises(IncompleteReleaseError, match="no head is selected"):
            await staging.seal(descriptor=build.descriptor, required_labels=MERIDIAN_GRAPH_LABELS)
        registry.abandon(claim)
        assert registry.head_revision is None
        assert registry.state is SourceAuthorityState.UNKNOWN

    asyncio.run(scenario())


# --- CAS head selection ----------------------------------------------------------


def test_scope_is_unknown_until_a_complete_release_is_published() -> None:
    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        registry = _registry(build)
        assert registry.state is SourceAuthorityState.UNKNOWN
        with pytest.raises(GovernedLocalT3UnavailableError, match="no complete release head"):
            registry.pin()
        release = await publish_meridian_release(registry, build, settings=_SETTINGS)
        assert registry.state is SourceAuthorityState.PRESENT
        assert registry.pin() is release
        assert registry.head_revision == release.release_id

    asyncio.run(scenario())


def test_scope_is_unknown_while_a_release_claim_is_open() -> None:
    """A reload makes the scope UNKNOWN before it touches anything, and pins refuse."""

    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        registry = _registry(build)
        await publish_meridian_release(registry, build, settings=_SETTINGS)
        assert registry.state is SourceAuthorityState.PRESENT
        claim = registry.claim()
        assert registry.state is SourceAuthorityState.UNKNOWN
        with pytest.raises(GovernedLocalT3UnavailableError):
            registry.pin()
        registry.abandon(claim)
        assert registry.state is SourceAuthorityState.UNKNOWN
        assert registry.head_revision is None

    asyncio.run(scenario())


def test_a_failed_publication_leaves_the_scope_unknown_and_an_exact_retry_publishes_one_release(
    tmp_path: Path,
) -> None:
    """Crash before head selection: the old head and the partial new triples both go away."""

    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        registry = _registry(build)
        await publish_meridian_release(registry, build, settings=_SETTINGS)
        broken = MeridianReleaseBuild(
            descriptor=build.descriptor,
            graph_triples={MERIDIAN_GRAPH_LABELS[0]: build.graph_triples[MERIDIAN_GRAPH_LABELS[0]]},
            contributors=build.contributors,
        )
        with pytest.raises(IncompleteReleaseError):
            await publish_meridian_release(registry, broken, settings=_SETTINGS)
        assert registry.state is SourceAuthorityState.UNKNOWN
        assert registry.head_revision is None
        retried = await publish_meridian_release(registry, build, settings=_SETTINGS)
        assert registry.state is SourceAuthorityState.PRESENT
        assert registry.head_revision == retried.release_id

    asyncio.run(scenario())


def test_a_rebuilt_identical_release_selects_the_same_head_and_is_not_a_new_release() -> None:
    """A restart that rebuilds from retained bytes is the SAME release, new object or not."""

    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        registry = _registry(build)
        first = await publish_meridian_release(registry, build, settings=_SETTINGS)
        rows = await first.select(_ALL_ROWS, dataset=first.dataset)
        rebuilt = build_meridian_release(_FIXTURE)
        claim = registry.claim()
        gateway, backend = build_release_staging_store(_SETTINGS)
        staging = MeridianReleaseStaging(gateway, backend)
        for label in MERIDIAN_GRAPH_LABELS:
            await staging.stage_graph(
                label=label,
                graph_iri=graph_iri_for_label(label),
                triples=rebuilt.graph_triples[label],
            )
        candidate = await staging.seal(
            descriptor=rebuilt.descriptor, required_labels=MERIDIAN_GRAPH_LABELS
        )
        assert candidate is not first
        assert candidate.release_id == first.release_id
        selected = registry.publish(claim, candidate)
        assert selected is first
        assert registry.head_revision == first.release_id
        assert await first.select(_ALL_ROWS, dataset=first.dataset) == rows

    asyncio.run(scenario())


def test_a_pinned_release_stays_byte_identical_after_another_release_is_published(
    tmp_path: Path,
) -> None:
    """The concurrency control: A is pinned, B is built and published separately.

    Every A query returns identical bytes afterwards, A's Store is a different object
    from B's, and A carries no writer through which B's publication could reach it.
    """

    async def scenario() -> None:
        build_a = build_meridian_release(_FIXTURE)
        registry = _registry(build_a)
        release_a = await publish_meridian_release(registry, build_a, settings=_SETTINGS)
        before = await release_a.select(_ALL_ROWS, dataset=release_a.dataset)

        build_b = build_meridian_release(_revalued_fixture(tmp_path))
        release_b = await publish_meridian_release(registry, build_b, settings=_SETTINGS)

        assert release_b.release_id != release_a.release_id
        assert registry.head_revision == release_b.release_id
        assert await release_a.select(_ALL_ROWS, dataset=release_a.dataset) == before
        assert await release_b.select(_ALL_ROWS, dataset=release_b.dataset) != before
        for name in BACKEND_MUTATION_METHODS:
            assert not hasattr(release_a, name)

    asyncio.run(scenario())


def test_publishing_with_a_claim_that_is_not_the_live_claim_is_refused() -> None:
    """A stale/duplicated release claim can never select a head."""

    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        registry = _registry(build)
        stale = registry.claim()
        registry.abandon(stale)
        gateway, backend = build_release_staging_store(_SETTINGS)
        staging = MeridianReleaseStaging(gateway, backend)
        for label in MERIDIAN_GRAPH_LABELS:
            await staging.stage_graph(
                label=label,
                graph_iri=graph_iri_for_label(label),
                triples=build.graph_triples[label],
            )
        candidate = await staging.seal(
            descriptor=build.descriptor, required_labels=MERIDIAN_GRAPH_LABELS
        )
        with pytest.raises(ReleaseHeadDriftError, match="not the live claim"):
            registry.publish(stale, candidate)
        assert registry.head_revision is None
        assert registry.state is SourceAuthorityState.UNKNOWN

    asyncio.run(scenario())


def test_a_head_that_moves_mid_invocation_withholds_the_pinned_result(tmp_path: Path) -> None:
    """Final publication requires the SAME head the invocation pinned.

    The control that must PASS is the same block with no publication in it.
    """

    async def scenario() -> None:
        build_a = build_meridian_release(_FIXTURE)
        registry = _registry(build_a)
        await publish_meridian_release(registry, build_a, settings=_SETTINGS)
        build_b = build_meridian_release(_revalued_fixture(tmp_path))

        with pinned_release(registry) as pinned:
            assert await pinned.select(_ALL_ROWS, dataset=pinned.dataset)

        with (
            pytest.raises(ReleaseHeadDriftError, match="the result is withheld"),
            pinned_release(registry) as pinned,
        ):
            await pinned.select(_ALL_ROWS, dataset=pinned.dataset)
            await publish_meridian_release(registry, build_b, settings=_SETTINGS)

    asyncio.run(scenario())


def test_the_release_registry_holds_no_schedule_cadence_or_validation_timestamp() -> None:
    """Freshness is the head: the registry's whole state is scope, head, claim and epoch.

    A cached validation timestamp, a next-run time or a heartbeat would have to live
    here, so the exact attribute set is the claim.
    """

    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        registry = _registry(build)
        await publish_meridian_release(registry, build, settings=_SETTINGS)
        assert set(vars(registry)) == {"_scope", "_selected", "_claim", "_epoch"}

    asyncio.run(scenario())


# --- the Fuseki negative capability ----------------------------------------------


def test_fuseki_source_bearing_governed_local_t3_is_unavailable_before_any_http_request() -> None:
    """Fuseki cannot prove an immutable snapshot, so it is refused with zero I/O.

    The transport fails the test if it is touched at all: the refusal is a declared
    capability, never a probe, because matching hashes either side of an unfenced
    query would not be proof.
    """
    recorded: list[httpx.Request] = []
    gateway = _fuseki_gateway(recorded)
    assert gateway.supports_immutable_release_snapshot is False
    with pytest.raises(GovernedLocalT3UnavailableError, match="cannot prove an immutable"):
        require_source_bearing_backend(gateway)
    with pytest.raises(GovernedLocalT3UnavailableError):
        MeridianReleaseStaging(gateway, OxigraphBackend())
    assert recorded == []


def test_an_oxigraph_backed_gateway_can_serve_a_source_bearing_release() -> None:
    """The control that must PASS: the real serving backend declares the capability."""
    gateway, backend = build_release_staging_store(_SETTINGS)
    assert gateway.supports_immutable_release_snapshot is True
    assert backend.supports_immutable_release_snapshot is True
    require_source_bearing_backend(gateway)


def test_a_backend_that_declares_no_snapshot_capability_is_refused() -> None:
    """The guard is a capability check, not a Fuseki class check."""

    class Unknown:
        pass

    with pytest.raises(GovernedLocalT3UnavailableError, match="Unknown"):
        require_source_bearing_backend(Unknown())


def test_release_type_is_the_only_object_a_governed_read_receives() -> None:
    """``publish`` hands back a ``SelectedOxigraphRelease``, never a gateway or backend."""

    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        release: Any = await publish_meridian_release(_registry(build), build, settings=_SETTINGS)
        assert type(release) is SelectedOxigraphRelease

    asyncio.run(scenario())


# --- release-first dataset construction ------------------------------------------


def _meridian_template(template_id: str) -> Any:
    from ne.brain_ontology.verified.meridian import load_meridian_template_registry

    return next(
        t for t in load_meridian_template_registry().templates if t.template_id == template_id
    )


def test_release_scoped_dataset_confines_the_principal_trim_to_the_pinned_release() -> None:
    """A dataset for a source-bearing read is built FROM an already-pinned release.

    The principal trim is unchanged, and it is then confined to the release's own
    graph set — the release is a parameter, so no dataset can exist before a head is
    pinned.
    """

    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        registry = _registry(build)
        release = await publish_meridian_release(registry, build, settings=_SETTINGS)
        template = _meridian_template("customers-in-counties")
        who = principal("user:ops", ("bank.customer.read",))
        dataset = release_scoped_dataset(release, who, StubExpander(), template)
        assert dataset == DatasetSpec(default_graphs=(graph_iri_for_label("bank.customer.read"),))
        rows = await release.select(_ALL_ROWS, dataset=dataset)
        assert len(rows) == len(build.graph_triples["bank.customer.read"])

    asyncio.run(scenario())


def test_release_scoped_dataset_requires_at_least_one_current_contributor_graph() -> None:
    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        release = await publish_meridian_release(_registry(build), build, settings=_SETTINGS)
        template = _meridian_template("customers-in-counties")
        who = principal("user:ops", ("bank.customer.read",))
        with pytest.raises(VerifiedAnswerScopeError, match="source_authority_unavailable"):
            release_scoped_dataset(
                release,
                who,
                StubExpander(),
                template,
                authorized_contributor_graphs=(),
            )
        graph = graph_iri_for_label("bank.customer.read")
        dataset = release_scoped_dataset(
            release,
            who,
            StubExpander(),
            template,
            authorized_contributor_graphs=(graph,),
        )
        assert dataset.default_graphs == (graph,)

    asyncio.run(scenario())


def test_release_scoped_dataset_stays_fail_closed_for_a_principal_with_no_matching_label() -> None:
    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        release = await publish_meridian_release(_registry(build), build, settings=_SETTINGS)
        template = _meridian_template("customers-in-counties")
        who = principal("user:nobody", ())
        with pytest.raises(VerifiedAnswerScopeError):
            release_scoped_dataset(release, who, StubExpander(), template)

    asyncio.run(scenario())


def test_release_scoped_dataset_refuses_a_graph_this_release_does_not_carry() -> None:
    """A label the principal legitimately holds, whose graph is not in THIS release."""

    async def scenario() -> None:
        build = build_meridian_release(_FIXTURE)
        release = await publish_meridian_release(_registry(build), build, settings=_SETTINGS)
        foreign = load_template_registry(FIXTURES_DIR).templates[0]
        assert not set(foreign.graph_classes) & set(MERIDIAN_GRAPH_LABELS)
        who = principal("user:ops", tuple(foreign.graph_classes))
        with pytest.raises(ReleaseScopeError, match="outside selected release"):
            release_scoped_dataset(release, who, StubExpander(), foreign)

    asyncio.run(scenario())
