# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-T3: the governed local T3 release descriptor, contract and contributors.

These controls own the PRODUCER half of the unit: what the real production Meridian
loader derives from the checked-in generator bytes, and what a ``governed_local_t3``
contributor identity/artifact is allowed to be.  The selected-release store, its
no-overwrite publication and the Fuseki negative capability are owned by
``test_selected_oxigraph_release.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import DeclaredLiteral
from ne.brain_ontology.verified.meridian import (
    MERIDIAN_CORPUS_VERSION,
    MERIDIAN_GRAPH_LABELS,
    MERIDIAN_LOADER_SCHEMA_VERSION,
    build_meridian_release,
    load_meridian_verified_fixture,
)
from ne.brain_ontology.verified.selected_release import (
    GOVERNED_LOCAL_T3_SOURCE_KIND,
    SELECTED_RELEASE_ALGORITHM_VERSION,
    GovernedLocalT3Instance,
    MeridianReleaseDescriptor,
    contributor_artifact_identity,
    contributor_source_identity,
    meridian_source_contract_identity,
    release_content_hash,
)
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    GovernedLocalT3SourcePoint,
    SourceAuthorityState,
    SourceContractIdentity,
)

from .verified_helpers import make_gateway

_FIXTURE = Path(__file__).resolve().parents[3] / "tools" / "meridian" / "fixtures" / "meridian-v1"

_INSTANCE = GovernedLocalT3Instance(
    deployment_id="deployment-alpha",
    dataset_instance_id="meridian-instance-0001",
    generation=1,
)


def _descriptor(**overrides: Any) -> MeridianReleaseDescriptor:
    base: dict[str, Any] = {
        "dataset_name": MERIDIAN_CORPUS_VERSION,
        "corpus_version": MERIDIAN_CORPUS_VERSION,
        "manifest_hash": "1" * 64,
        "loader_schema_version": MERIDIAN_LOADER_SCHEMA_VERSION,
        "data_model_hash": "2" * 64,
        "ontology_shapes_version": "3" * 64,
        "acl_graph_assignment_hash": "4" * 64,
    }
    base.update(overrides)
    return MeridianReleaseDescriptor(**base)


def _revalued_fixture(tmp_path: Path, *, county: str = "Marion") -> Path:
    """A copy of the real fixture with ONE customer's county changed, manifest re-signed.

    This is a VALUE CHANGE to the generator's own bytes, not a substitute test seed:
    the loader still verifies every manifest sha256 and still refuses a fixture whose
    manifest disagrees with its content.
    """
    target = tmp_path / "revalued"
    shutil.copytree(_FIXTURE, target)
    banking = json.loads((target / "banking.json").read_bytes())
    banking["customers"][0]["address"]["county"] = county
    raw = json.dumps(banking, indent=2, sort_keys=False).encode("utf-8")
    (target / "banking.json").write_bytes(raw)
    manifest = json.loads((target / "manifest.json").read_bytes())
    manifest["sha256"]["banking.json"] = hashlib.sha256(raw).hexdigest()
    (target / "manifest.json").write_bytes(json.dumps(manifest, indent=2).encode("utf-8"))
    return target


# --- descriptor + contract identity -------------------------------------------


def test_release_descriptor_hash_moves_for_every_credential_free_member() -> None:
    """Every descriptor member is load-bearing, and only those members are.

    Rebuilding the identical descriptor is the control that must PASS; changing any
    one member — including the fixed publication-algorithm version — must move the
    hash, so a fixture, data-model, ontology/shapes, ACL-assignment, loader-schema or
    algorithm change can never be absorbed into an existing release.
    """
    baseline = _descriptor()
    assert _descriptor().descriptor_hash == baseline.descriptor_hash

    moved = {
        "descriptor_schema_version": 2,
        "dataset_name": "meridian-v2",
        "corpus_version": "meridian-v2",
        "manifest_hash": "a" * 64,
        "loader_schema_version": MERIDIAN_LOADER_SCHEMA_VERSION + 1,
        "data_model_hash": "b" * 64,
        "ontology_shapes_version": "c" * 64,
        "acl_graph_assignment_hash": "d" * 64,
        "selected_release_algorithm_version": SELECTED_RELEASE_ALGORITHM_VERSION + 1,
    }
    assert set(moved) == set(baseline.model_dump()) - {"descriptor_hash"}
    for member, value in moved.items():
        assert _descriptor(**{member: value}).descriptor_hash != baseline.descriptor_hash, member


@pytest.mark.parametrize("separator", ["", "|", ":", "\x00", "-", "/"])
def test_release_descriptor_encoding_preserves_field_boundaries(separator: str) -> None:
    """No delimiter joining at all: re-splitting adjacent members cannot collide.

    Length prefixing is what makes this hold for EVERY candidate separator. A
    concatenating encoding collides on the empty separator; an encoding that joins on
    a fixed delimiter collides as soon as a field value contains that delimiter, which
    is exactly what each parametrized case re-splits across.  ``dataset_name`` and
    ``corpus_version`` are adjacent in the canonical field order, so the two
    descriptors below have the same joined preimage under the matching encoding and
    must still hash differently.
    """
    left = _descriptor(dataset_name=f"meridian{separator}x", corpus_version="v1")
    right = _descriptor(dataset_name="meridian", corpus_version=f"x{separator}v1")
    assert left != right
    assert left.descriptor_hash != right.descriptor_hash


def test_release_descriptor_rejects_a_supplied_hash_that_is_not_its_canonical_bytes() -> None:
    with pytest.raises(ValidationError, match="canonical release-descriptor bytes"):
        _descriptor(descriptor_hash="f" * 64)
    assert _descriptor(descriptor_hash=_descriptor().descriptor_hash) == _descriptor()


def test_release_content_hash_distinguishes_term_kind_and_literal_datatype() -> None:
    """Two graphs that serialize to different RDF terms never share one content hash.

    A plain ``str`` object and a ``DeclaredLiteral`` carrying the same text do NOT
    become the same RDF term — the gateway's IRI-vs-literal shape heuristic applies to
    the first and never to the second — and a typed literal is different content again.
    Collapsing either distinction would let a changed release keep its old release id.
    """
    subject = "urn:ne:meridian:customer:mer-cus-001"
    predicate = "https://neutron.enterprise/schema/meridian/county"
    plain_term = release_content_hash("d", ((subject, predicate, "1"),))
    declared = release_content_hash("d", ((subject, predicate, DeclaredLiteral("1")),))
    typed = release_content_hash(
        "d",
        ((subject, predicate, DeclaredLiteral("1", "http://www.w3.org/2001/XMLSchema#integer")),),
    )
    assert len({plain_term, declared, typed}) == 3
    # The control that must PASS: identical content in a different order is one hash.
    pair = (
        (subject, predicate, DeclaredLiteral("1")),
        (subject, predicate, DeclaredLiteral("2")),
    )
    assert release_content_hash("d", pair) == release_content_hash("d", tuple(reversed(pair)))


def test_source_contract_identity_is_governed_local_t3_over_the_descriptor_hash() -> None:
    """T3's factory hashes the credential-free release descriptor and nothing else."""
    descriptor = _descriptor()
    contract = meridian_source_contract_identity(descriptor)
    assert contract == SourceContractIdentity(
        source_kind=GOVERNED_LOCAL_T3_SOURCE_KIND,
        descriptor_schema_version=descriptor.descriptor_schema_version,
        descriptor_hash=descriptor.descriptor_hash,
    )
    assert contract.source_kind == "governed_local_t3"


def test_source_contract_identity_moves_with_the_publication_algorithm_version() -> None:
    """The fixed selected-release/no-overwrite algorithm version is a contract member."""
    base = meridian_source_contract_identity(_descriptor())
    bumped = meridian_source_contract_identity(
        _descriptor(selected_release_algorithm_version=SELECTED_RELEASE_ALGORITHM_VERSION + 1)
    )
    assert bumped.identity_hash != base.identity_hash


# --- contributor identities ----------------------------------------------------


def test_contributor_identity_is_governed_local_t3_over_the_manifest_dataset_name() -> None:
    """Every contributor point is the manifest dataset plus its own row/entity key.

    No contributor may carry an M365 point, and the dataset name comes from the
    manifest-declared corpus version rather than any caller-supplied label.
    """
    build = build_meridian_release(_FIXTURE)
    assert build.contributors
    for contributor in build.contributors:
        identity = contributor_source_identity(
            _INSTANCE, build.descriptor, contributor.contributor_key
        )
        assert identity.source_kind == "governed_local_t3"
        assert identity.connector_instance_id == _INSTANCE.dataset_instance_id
        assert identity.generation == _INSTANCE.generation
        assert identity.point == GovernedLocalT3SourcePoint(
            dataset_name=MERIDIAN_CORPUS_VERSION,
            contributor_key=contributor.contributor_key,
        )


def test_contributor_identity_cannot_collide_across_deployments_or_instances() -> None:
    """The same row under another deployment or dataset instance is a different identity."""
    build = build_meridian_release(_FIXTURE)
    key = build.contributors[0].contributor_key
    mine = contributor_source_identity(_INSTANCE, build.descriptor, key)
    others = (
        _INSTANCE.model_copy(update={"deployment_id": "deployment-beta"}),
        _INSTANCE.model_copy(update={"dataset_instance_id": "meridian-instance-0002"}),
        _INSTANCE.model_copy(update={"generation": 2}),
    )
    hashes = {contributor_source_identity(o, build.descriptor, key).identity_hash for o in others}
    assert mine.identity_hash not in hashes
    assert len(hashes) == len(others)


def test_contributor_artifact_binds_its_identity_contract_and_acl_snapshot() -> None:
    build = build_meridian_release(_FIXTURE)
    contributor = build.contributors[0]
    artifact = contributor_artifact_identity(
        _INSTANCE,
        build.descriptor,
        contributor.contributor_key,
        content_snapshot_hash=contributor.content_hash,
        acl_snapshot_hash=build.descriptor.acl_graph_assignment_hash,
    )
    assert artifact.source_identity == contributor_source_identity(
        _INSTANCE, build.descriptor, contributor.contributor_key
    )
    assert artifact.source_contract == meridian_source_contract_identity(build.descriptor)
    assert artifact.source_snapshot_hash == build.descriptor.manifest_hash
    assert artifact.content_snapshot_hash == contributor.content_hash
    # A different contributor's content under the same identity is a different artifact.
    other = contributor_artifact_identity(
        _INSTANCE,
        build.descriptor,
        contributor.contributor_key,
        content_snapshot_hash=build.contributors[1].content_hash,
        acl_snapshot_hash=build.descriptor.acl_graph_assignment_hash,
    )
    assert other.artifact_id != artifact.artifact_id


def test_governed_local_t3_receipt_cannot_carry_a_connector_freshness_sla() -> None:
    """Governed local T3 freshness is the selected head, never a connector SLA.

    The frozen contract refuses an SLA on this source kind; the control that must
    PASS is the same receipt without one.
    """
    build = build_meridian_release(_FIXTURE)
    identity = contributor_source_identity(
        _INSTANCE, build.descriptor, build.contributors[0].contributor_key
    )
    moment = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    common: dict[str, Any] = {
        "receipt_id": "receipt-1",
        "source_identity": identity,
        "source_revision": "release-1",
        "source_contract": meridian_source_contract_identity(build.descriptor),
        "contract_epoch": 1,
        "acl_snapshot_hash": build.descriptor.acl_graph_assignment_hash,
        "state": SourceAuthorityState.UNKNOWN,
        "admitted_at": moment,
        "authority_as_of": moment,
        "audit_event_id": "audit-1",
        "lineage_id": "lineage-1",
    }
    assert CurrentSourceAuthorityReceipt(**common).freshness_sla_seconds is None
    with pytest.raises(ValidationError, match="cannot use a connector freshness SLA"):
        CurrentSourceAuthorityReceipt(**common, freshness_sla_seconds=Decimal(3600))


# --- contributor partitioning ---------------------------------------------------


def test_every_release_triple_belongs_to_exactly_one_contributor() -> None:
    """No unattributed remainder: contributor keys ARE the release's subject set.

    A row is withheld when any one of its contributors fails to resolve, which is
    only sound if every triple has a contributor.
    """
    build = build_meridian_release(_FIXTURE)
    expected: set[tuple[str, str]] = {
        (label, triple[0]) for label, triples in build.graph_triples.items() for triple in triples
    }
    assert {(c.graph_label, c.contributor_key) for c in build.contributors} == expected
    assert sum(c.triple_count for c in build.contributors) == sum(
        len(triples) for triples in build.graph_triples.values()
    )
    assert set(build.graph_triples) == set(MERIDIAN_GRAPH_LABELS)


def test_one_row_value_change_moves_only_that_rows_contributor_artifact(
    tmp_path: Path,
) -> None:
    """A value change creates ONE new immutable artifact, not a whole new set."""
    base = build_meridian_release(_FIXTURE)
    changed = build_meridian_release(_revalued_fixture(tmp_path))
    before = {c.contributor_key: c.content_hash for c in base.contributors}
    after = {c.contributor_key: c.content_hash for c in changed.contributors}
    assert set(before) == set(after)
    differing = {key for key in before if before[key] != after[key]}
    assert len(differing) == 1
    assert differing.pop().startswith("urn:ne:meridian:customer:")


def test_release_descriptor_moves_when_the_generator_bytes_change(tmp_path: Path) -> None:
    base = build_meridian_release(_FIXTURE)
    changed = build_meridian_release(_revalued_fixture(tmp_path))
    assert changed.descriptor.manifest_hash != base.descriptor.manifest_hash
    assert changed.descriptor.descriptor_hash != base.descriptor.descriptor_hash
    assert (
        meridian_source_contract_identity(changed.descriptor).identity_hash
        != meridian_source_contract_identity(base.descriptor).identity_hash
    )


def test_release_build_is_byte_deterministic_for_identical_generator_bytes() -> None:
    """Stable bytes do not age: the same fixture always yields the same descriptor."""
    first = build_meridian_release(_FIXTURE)
    second = build_meridian_release(_FIXTURE)
    assert first.descriptor == second.descriptor
    assert first.contributors == second.contributors


def test_loader_refuses_a_fixture_whose_manifest_disagrees_with_its_bytes(
    tmp_path: Path,
) -> None:
    corrupt = tmp_path / "meridian-v1"
    shutil.copytree(_FIXTURE, corrupt)
    (corrupt / "banking.json").write_bytes((corrupt / "banking.json").read_bytes() + b" ")
    with pytest.raises(ValueError, match="manifest sha256"):
        build_meridian_release(corrupt)


def test_production_load_report_carries_the_builders_exact_release_descriptor() -> None:
    """The stamped path is the only path: a load report always describes a release."""

    async def scenario() -> None:
        report = await load_meridian_verified_fixture(make_gateway(), _FIXTURE)
        assert report.release == build_meridian_release(_FIXTURE).descriptor
        assert report.fixture_hash == report.release.manifest_hash
        assert report.corpus_version == MERIDIAN_CORPUS_VERSION

    asyncio.run(scenario())


# --- configuration --------------------------------------------------------------


def test_config_refuses_governed_local_t3_on_a_backend_without_snapshot_capability() -> None:
    """Enabling governed local T3 on Fuseki is a fail-loud boot error.

    The control that must PASS is the same flag on oxigraph, and the control that
    must remain unaffected is Fuseki with the flag off.
    """
    with pytest.raises(ValidationError, match="cannot prove the immutable selected-release"):
        OntologySettings(
            backend="fuseki", fuseki_url="http://fuseki:3030/ne", governed_local_t3_enabled=True
        )
    assert OntologySettings(governed_local_t3_enabled=True).backend == "oxigraph"
    assert (
        OntologySettings(backend="fuseki", fuseki_url="http://fuseki:3030/ne").backend == "fuseki"
    )


def test_governed_local_t3_is_default_off_and_has_no_cadence_knob() -> None:
    """There is exactly one governed local T3 setting and it is off by default.

    Freshness is the selected release head plus backend health, so a schedule,
    cadence, interval or heartbeat setting must not exist on ``OntologySettings``.
    """
    settings = OntologySettings()
    assert settings.governed_local_t3_enabled is False
    t3_fields = {name for name in type(settings).model_fields if "governed_local_t3" in name}
    assert t3_fields == {"governed_local_t3_enabled"}
