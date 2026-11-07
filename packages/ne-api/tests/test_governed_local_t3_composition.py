# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-T3 production composition: the governed local T3 release head.

These controls own ONLY the new ``build_platform`` seam — the durable dataset-instance
claim, the selected immutable-release head, the CAS-registered SourceContract head, the
credential-free descriptor, and the fact that the verified-answer path reads the PINNED
release rather than the mutable serving gateway. The producer itself and the release
store are proven package-locally in ne-brain-ontology and are not replayed here.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ne.api import create_app
from ne.api.platform import AppSeams, build_platform
from ne.api.settings import ApiSettings
from ne.audit.config import AuditSettings
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Lane, Principal
from ne.flywheel.config import FlywheelSettings
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.runtime.config import RuntimeSettings

from .conftest import (
    ISSUER,
    FakeTokenVerifier,
    InMemoryFilteredChunkStore,
    PassthroughReranker,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
    get_state,
    run,
)

_FIXTURE = Path(__file__).resolve().parents[3] / "tools" / "meridian" / "fixtures" / "meridian-v1"
_CUSTOMER_LABEL = "bank.customer.read"


def _ontology_settings(*, governed: bool, templates_dir: Path | None = None) -> Any:
    from ne.brain_ontology import meridian_template_dir
    from ne.brain_ontology.config import OntologySettings

    return OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=templates_dir or meridian_template_dir(),
        include_ontology_graph=False,
        backend="oxigraph",
        governed_local_t3_enabled=governed,
    )


def _api_settings(**overrides: object) -> ApiSettings:
    base: dict[str, object] = {
        "qdrant_url": "http://qdrant.test",
        "qdrant_collection": "t",
        "tei_url": "http://tei.test",
    }
    base.update(overrides)
    return ApiSettings(**base)  # type: ignore[arg-type]


def _seams(
    tmp_path: Path,
    *,
    key: str,
    governed: bool,
    templates_dir: Path | None = None,
    signal_url: str = "",
) -> tuple[AppSeams, FakeTokenVerifier]:
    registry = PluginRegistry()
    verifier = FakeTokenVerifier()
    registry.register("token_verifier", "fake", lambda oidc: verifier)
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
            principal_claims=("email", "preferred_username", "name", "department"),
        ),
        runtime_settings=RuntimeSettings(session_database_url=f"sqlite:///{tmp_path}/rt-{key}.db"),
        audit_settings=AuditSettings(database_url=f"sqlite:///{tmp_path}/au-{key}.db"),
        permissions_settings=PermissionsSettings(database_url=f"sqlite:///{tmp_path}/pm-{key}.db"),
        gateway_settings=GatewaySettings(
            models=[
                ModelDef(
                    name="stub",
                    litellm_model="openai/stub",
                    provider="stub",
                    lane=Lane.FRONTIER_API,
                    context_window=32768,
                )
            ]
        ),
        gateway=ScriptedGateway(),
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=InMemoryFilteredChunkStore(()),
        reranker=PassthroughReranker(),
        plugin_registry=registry,
        ontology_settings=_ontology_settings(governed=governed, templates_dir=templates_dir),
        # A signal database is what turns the verified registry into the boot bundle
        # OVERLAID with the governed steward store; left unset the boot dir IS the
        # registry (the pre-NE-ONTOLOGY-FLYWHEEL load), so the default stays byte-identical.
        flywheel_settings=FlywheelSettings(signal_database_url=signal_url) if signal_url else None,
    )
    return seams, verifier


def _revalued_fixture(tmp_path: Path) -> Path:
    """The real fixture with ONE customer's county changed, manifest re-signed."""
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


def _build(tmp_path: Path, *, key: str, governed: bool, fixture: Path | None = None) -> Any:
    reset_config_hash_for_testing()
    seams, verifier = _seams(tmp_path, key=key, governed=governed)
    platform = build_platform(
        _api_settings(demo_meridian_fixture_path=str(fixture or _FIXTURE)), seams
    )
    return platform, seams, verifier


# --- default-off parity ---------------------------------------------------------


def test_governed_local_t3_default_off_composes_no_release_and_no_descriptor(
    tmp_path: Path,
) -> None:
    """A deployment that did not ask for governed local T3 composes exactly as before.

    Both fields stay ``None``, so nothing downstream can read a release head that was
    never selected, and the verified bindings keep the gateway they always had.
    """
    platform, _, _ = _build(tmp_path, key=uuid.uuid4().hex, governed=False)
    assert platform.governed_local_t3_release is None
    assert platform.governed_local_t3_source_authority is None
    for engine in platform.engines:
        engine.dispose()


# --- the composed head ----------------------------------------------------------


def test_governed_local_t3_composition_selects_a_release_head_and_advertises_exactly_it(
    tmp_path: Path,
) -> None:
    """The advertised descriptor is the head that was actually selected, not a restatement."""
    from ne.brain_ontology import (
        SELECTED_RELEASE_ALGORITHM_VERSION,
        build_meridian_release,
        meridian_source_contract_identity,
    )

    platform, _, _ = _build(tmp_path, key=uuid.uuid4().hex, governed=True)
    release = platform.governed_local_t3_release
    descriptor = platform.governed_local_t3_source_authority
    assert release is not None and descriptor is not None

    expected = build_meridian_release(_FIXTURE)
    assert release.descriptor == expected.descriptor
    assert descriptor.selected_release_id == release.release_id
    assert descriptor.contract_identity_hash == (
        meridian_source_contract_identity(expected.descriptor).identity_hash
    )
    assert descriptor.descriptor_hash == expected.descriptor.descriptor_hash
    assert descriptor.dataset_name == expected.descriptor.dataset_name
    assert descriptor.source_kind == "governed_local_t3"
    assert descriptor.selected_release_algorithm_version == SELECTED_RELEASE_ALGORITHM_VERSION
    # Publication admits every contributor before exposing the resolved head.
    assert descriptor.contract_epoch == 1
    assert descriptor.resolved is True
    for engine in platform.engines:
        engine.dispose()


def test_the_composed_descriptor_carries_no_freshness_sla_cadence_or_schedule(
    tmp_path: Path,
) -> None:
    """Governed local T3 freshness is the selected head; no timer member may exist.

    The field set is pinned exactly, so adding an SLA, cadence, interval, schedule or
    heartbeat member to this descriptor fails here rather than at a reviewer's
    discretion.
    """
    platform, _, _ = _build(tmp_path, key=uuid.uuid4().hex, governed=True)
    descriptor = platform.governed_local_t3_source_authority
    assert descriptor is not None
    assert set(type(descriptor).model_fields) == {
        "source_kind",
        "dataset_name",
        "dataset_instance_id",
        "source_generation",
        "contract_epoch",
        "contract_identity_hash",
        "descriptor_schema_version",
        "descriptor_hash",
        "contract_head_revision",
        "selected_release_id",
        "selected_release_algorithm_version",
        "resolved",
    }
    # M365 is not configured here, so no sibling producer's descriptor is invented.
    assert platform.m365_source_authority is None
    for engine in platform.engines:
        engine.dispose()


def test_nothing_reachable_from_the_composed_release_can_write(tmp_path: Path) -> None:
    """Holding the release on ``Platform`` cannot become a write capability.

    The audit covers the release AND the backend it holds — deliberately reaching the
    private handle, because the claim is about everything reachable from the platform,
    not only about the wrapper's own attribute list. A release sealed over a writable
    backend would pass a wrapper-only check and still be a writer at one hop.
    """
    from ne.brain_ontology.store.protocols import BACKEND_MUTATION_METHODS, TripleStoreBackend

    platform, _, _ = _build(tmp_path, key=uuid.uuid4().hex, governed=True)
    release = platform.governed_local_t3_release
    assert release is not None
    assert BACKEND_MUTATION_METHODS
    held = release._backend
    for name in BACKEND_MUTATION_METHODS:
        assert not hasattr(release, name), name
        assert not hasattr(held, name), name
    assert not isinstance(held, TripleStoreBackend)
    for engine in platform.engines:
        engine.dispose()


def test_the_pinned_reader_refuses_a_dataset_outside_its_own_release(
    tmp_path: Path,
) -> None:
    """The composed reader delegates through the release, so its confinement holds.

    A reader that reached the release's backend directly would answer this query; the
    reader must go through the release, which is what keeps the pinned instance/head
    inseparable from every read the verified path can make.
    """
    from ne.brain_ontology.store.protocols import DatasetSpec
    from ne.brain_ontology.verified.selected_release import (
        ReleaseScopeError,
        SelectedReleaseVerifiedReader,
    )

    platform, _, _ = _build(tmp_path, key=uuid.uuid4().hex, governed=True)
    release = platform.governed_local_t3_release
    assert release is not None
    reader = SelectedReleaseVerifiedReader(release, platform.ontology.store.ontology_version())

    async def _scenario() -> None:
        # The control that must PASS: the release's own graphs answer.
        rows = await reader.sparql_scoped("SELECT ?s WHERE { ?s ?p ?o }", dataset=release.dataset)
        assert rows
        with pytest.raises(ReleaseScopeError, match="outside selected release"):
            await reader.sparql_scoped(
                "SELECT ?s WHERE { ?s ?p ?o }",
                dataset=DatasetSpec(default_graphs=("urn:ne:graph:acl:elsewhere",)),
            )
        # The gateway's own C12 gate is applied, not restated.
        with pytest.raises(ValueError, match="SELECT/ASK only"):
            await reader.sparql_scoped(
                "DESCRIBE <urn:ne:meridian:customer:mer-cus-001>", dataset=release.dataset
            )

    run(_scenario())
    assert reader.release_id == release.release_id
    for engine in platform.engines:
        engine.dispose()


# --- durable instance + contract epoch ------------------------------------------


def test_a_restart_on_the_same_dataset_reselects_the_identical_head_and_instance(
    tmp_path: Path,
) -> None:
    """Same bytes, same durable state ⇒ the same release, not a new one."""
    key = uuid.uuid4().hex
    first, _, _ = _build(tmp_path, key=key, governed=True)
    first_descriptor = first.governed_local_t3_source_authority
    for engine in first.engines:
        engine.dispose()

    second, _, _ = _build(tmp_path, key=key, governed=True)
    second_descriptor = second.governed_local_t3_source_authority
    assert first_descriptor is not None and second_descriptor is not None
    assert second_descriptor == first_descriptor
    for engine in second.engines:
        engine.dispose()


def test_a_fixture_value_change_advances_the_contract_epoch_and_keeps_the_instance(
    tmp_path: Path,
) -> None:
    """A reload of the SAME configured dataset retains its server-issued instance.

    The release and contract move — a changed release is a changed contract — and the
    epoch advances with ``resolved`` cleared, then the same boot publication admits
    every contributor under the replacement epoch before exposing the platform.
    """
    key = uuid.uuid4().hex
    first, _, _ = _build(tmp_path, key=key, governed=True)
    before = first.governed_local_t3_source_authority
    for engine in first.engines:
        engine.dispose()

    second, _, _ = _build(tmp_path, key=key, governed=True, fixture=_revalued_fixture(tmp_path))
    after = second.governed_local_t3_source_authority
    assert before is not None and after is not None
    assert after.dataset_instance_id == before.dataset_instance_id
    assert after.source_generation == before.source_generation
    assert after.contract_identity_hash != before.contract_identity_hash
    assert after.selected_release_id != before.selected_release_id
    assert after.contract_epoch == before.contract_epoch + 1
    assert after.contract_head_revision != before.contract_head_revision
    assert after.resolved is True
    for engine in second.engines:
        engine.dispose()


# --- fail-loud composition ------------------------------------------------------


def test_governed_local_t3_without_a_configured_fixture_is_a_loud_boot_error(
    tmp_path: Path,
) -> None:
    """There is no other source for this producer, so an unset path cannot default."""
    reset_config_hash_for_testing()
    seams, _ = _seams(tmp_path, key=uuid.uuid4().hex, governed=True)
    with pytest.raises(ValueError, match="NE_API_DEMO_MERIDIAN_FIXTURE_PATH"):
        build_platform(_api_settings(demo_meridian_fixture_path=""), seams)


#: A SURPLUS template the pinned release CAN serve: its ``[scope] graph_classes`` is a
#: label the Meridian release actually holds, so ``SelectedOxigraphRelease.confine``
#: admits its dataset. The surplus guard must let this one boot -- it checks
#: SERVABILITY against the release's graph set, not membership of the packaged set.
_SERVABLE_SURPLUS_TEMPLATE = """
template_id = "extra-servable"
version = 1
description = "A surplus template addressing a graph the pinned Meridian release holds."
max_rows = 50
sparql = \"\"\"
SELECT ?declaration ?declaration_id WHERE {
  #BINDINGS#
  ?declaration a <https://neutron.enterprise/schema/meridian/DisasterDeclaration> .
  ?declaration <https://neutron.enterprise/schema/meridian/declaration_id> ?declaration_id .
}
\"\"\"

[scope]
graph_classes = ["bank.customer.read"]
result_shape = "enumerable"

[answer]
columns = ["declaration", "declaration_id"]
citation_var = "declaration"
contributor_vars = ["declaration"]
source_status = "source_bearing"
count_total_rows = true
"""


def _bundle_with_extra(tmp_path: Path, *, name: str, body: str) -> Path:
    """The packaged Meridian bundle plus ONE extra template file."""
    from ne.brain_ontology import meridian_template_dir

    directory = tmp_path / f"templates-{name}"
    directory.mkdir()
    for source in sorted(meridian_template_dir().glob("*.toml")):
        shutil.copyfile(source, directory / source.name)
    (directory / f"{name}.toml").write_text(body, encoding="utf-8")
    return directory


def test_a_template_the_pinned_release_cannot_serve_is_a_loud_boot_error(
    tmp_path: Path,
) -> None:
    """The SURPLUS direction of the boot guard, which had been missing.

    This is the exact shipped regression: ``ne demo up`` staged the four acme corpus
    templates beside the packaged Meridian bundle, governed local T3 handed EVERY
    verified binding the pinned read-only release, and the release holds only the three
    ``bank.*`` graphs -- so ``POST /v1/verified/invoke`` answered HTTP 502
    ``ReleaseScopeError`` for a template the deployment had itself advertised. The
    guard's own comment already promised a boot failure rather than "advertising
    workflows whose tools cannot exist"; it only ever computed the MISSING direction.
    """
    from ne.cli.demo.corpus import ACCOUNTS_DIRECTORY_TEMPLATE

    reset_config_hash_for_testing()
    directory = _bundle_with_extra(
        tmp_path, name="accounts_directory", body=ACCOUNTS_DIRECTORY_TEMPLATE
    )
    seams, _ = _seams(tmp_path, key=uuid.uuid4().hex, governed=True, templates_dir=directory)
    with pytest.raises(ValueError, match="could only fail at invoke: \\['accounts-directory'\\]"):
        build_platform(_api_settings(demo_meridian_fixture_path=str(_FIXTURE)), seams)


def test_a_canonical_bundle_template_whose_scope_drifts_is_a_loud_boot_error(
    tmp_path: Path,
) -> None:
    """The guard used to EXEMPT every id in ``MERIDIAN_TEMPLATE_IDS``, so the
    templates we ship ourselves were the one thing it never checked.

    A canonical template that keeps its id but drifts its ``[scope] graph_classes``
    outside the release still satisfies the missing-id check above — the id is
    present — and is then advertised and 502s at invoke with ``ReleaseScopeError``:
    exactly the failure the guard exists to prevent, on exactly the content we
    control at build time. Membership of the packaged set was never the question;
    servability against the pinned release is. Here ``disaster-declarations`` keeps
    its id and moves to a label the Meridian release does not hold.
    """
    from ne.brain_ontology import meridian_template_dir

    reset_config_hash_for_testing()
    canonical = (meridian_template_dir() / "disaster_declarations.toml").read_text(encoding="utf-8")
    drifted = canonical.replace(
        'graph_classes = ["bank.customer.read"]', 'graph_classes = ["all-employees"]'
    )
    assert drifted != canonical, "the canonical fixture no longer has the scope this test drifts"
    assert 'template_id = "disaster-declarations"' in drifted
    directory = _bundle_with_extra(tmp_path, name="disaster_declarations", body=drifted)
    seams, _ = _seams(tmp_path, key=uuid.uuid4().hex, governed=True, templates_dir=directory)
    with pytest.raises(
        ValueError, match="could only fail at invoke: \\['disaster-declarations'\\]"
    ):
        build_platform(_api_settings(demo_meridian_fixture_path=str(_FIXTURE)), seams)


def test_the_packaged_bundle_as_shipped_still_boots_under_the_whole_bundle_check(
    tmp_path: Path,
) -> None:
    """CONTROL for the broadened predicate: checking canonical ids too must not red
    the bundle we actually ship. All eleven packaged Meridian templates are scoped
    inside ``MERIDIAN_GRAPH_LABELS``, so an unmodified bundle boots."""
    from ne.brain_ontology import meridian_template_dir

    reset_config_hash_for_testing()
    seams, _ = _seams(
        tmp_path, key=uuid.uuid4().hex, governed=True, templates_dir=meridian_template_dir()
    )
    platform = build_platform(_api_settings(demo_meridian_fixture_path=str(_FIXTURE)), seams)
    assert "read.kg.disaster_declarations" in {spec.name for spec in platform.executor.tool_specs}
    for engine in platform.engines:
        engine.dispose()


def test_a_surplus_template_inside_the_release_graphs_still_boots(tmp_path: Path) -> None:
    """The guard checks SERVABILITY, not membership of the packaged Meridian set."""
    reset_config_hash_for_testing()
    directory = _bundle_with_extra(tmp_path, name="extra_servable", body=_SERVABLE_SURPLUS_TEMPLATE)
    seams, _ = _seams(tmp_path, key=uuid.uuid4().hex, governed=True, templates_dir=directory)
    platform = build_platform(_api_settings(demo_meridian_fixture_path=str(_FIXTURE)), seams)
    assert "read.kg.extra_servable" in {spec.name for spec in platform.executor.tool_specs}
    for engine in platform.engines:
        engine.dispose()


def test_an_unservable_template_boots_fine_when_no_release_pins_the_reader(
    tmp_path: Path,
) -> None:
    """Governed local T3 OFF: nothing confines the dataset, so nothing is unservable.

    The surplus direction is a claim about the PINNED release, so it may only fire
    where a release is pinned. With ``governed_local_t3_enabled`` false the verified
    bindings read the mutable serving gateway, which has no release scope to leave.
    """
    from ne.cli.demo.corpus import ACCOUNTS_DIRECTORY_TEMPLATE

    reset_config_hash_for_testing()
    directory = _bundle_with_extra(
        tmp_path, name="accounts_directory", body=ACCOUNTS_DIRECTORY_TEMPLATE
    )
    seams, _ = _seams(tmp_path, key=uuid.uuid4().hex, governed=False, templates_dir=directory)
    platform = build_platform(_api_settings(demo_meridian_fixture_path=str(_FIXTURE)), seams)
    assert platform.governed_local_t3_source_authority is None
    for engine in platform.engines:
        engine.dispose()


#: A steward-authored template addressing a label the pinned Meridian release does NOT
#: hold. Identical in shape to the surplus fixture above; only the id and the scope
#: differ. This body only ever reaches the governed ``ne_verified_templates`` store --
#: it is never a file in the boot bundle.
_STEWARD_OUTSIDE_TEMPLATE = _SERVABLE_SURPLUS_TEMPLATE.replace(
    'template_id = "extra-servable"', 'template_id = "steward-outside"'
).replace('graph_classes = ["bank.customer.read"]', 'graph_classes = ["all-employees"]')

#: A bundle template scoped to a release label AND a label outside the release. Runtime
#: servability is an INTERSECTION predicate, so a principal holding only
#: ``bank.customer.read`` invokes this one successfully -- the boot guard refuses it
#: anyway, on purpose (see the PREDICATE note on the guard).
_PARTIALLY_SERVABLE_TEMPLATE = _SERVABLE_SURPLUS_TEMPLATE.replace(
    'template_id = "extra-servable"', 'template_id = "extra-partial"'
).replace(
    'graph_classes = ["bank.customer.read"]',
    'graph_classes = ["bank.customer.read", "all-employees"]',
)


def _store_verified_template(signal_url: str, toml_text: str, *, template_id: str) -> None:
    """Append ONE row to the governed HITL store, exactly as a steward apply would.

    The row is written BEFORE the boot under test, so the next ``build_platform``
    overlays it onto the boot bundle -- the runtime-added provenance the boot guard
    must never refuse to start over.
    """
    import sqlalchemy as sa

    from ne.brain_ontology.verified.store import (
        VerifiedTemplateRecord,
        append_verified_template,
        ensure_verified_template_schema,
    )

    engine = sa.create_engine(signal_url)
    try:
        ensure_verified_template_schema(engine)
        with engine.begin() as conn:
            append_verified_template(
                conn,
                VerifiedTemplateRecord(
                    template_id=template_id,
                    version=1,
                    template_toml=toml_text,
                    toml_sha256=hashlib.sha256(toml_text.encode("utf-8")).hexdigest(),
                    proposal_id=f"prop-{template_id}",
                    actor="user:steward",
                    config_hash="boot",
                    created_at=datetime.now(UTC),
                ),
            )
    finally:
        engine.dispose()


def test_a_steward_applied_template_outside_the_release_cannot_brick_boot(
    tmp_path: Path,
) -> None:
    """THE regression: a governed-store row may never make the box unbootable.

    ``verified_setup.registry`` is the boot bundle OVERLAID with the governed HITL
    store whenever a flywheel signal database is configured -- and the pilot compose
    configures one. A steward applies templates into that store at RUNTIME, so a boot
    guard that read the overlaid registry would let one approved-and-applied proposal
    take ne-api AND ne-worker down at the next restart, recoverable only by deleting
    the row from ``ne_verified_templates`` by hand. The guard therefore scopes its
    refusal to bundle content. The steward template stays advertised and keeps its
    PRE-EXISTING behaviour -- a 502 at invoke, not a dead deployment.
    """
    reset_config_hash_for_testing()
    key = uuid.uuid4().hex
    signal_url = f"sqlite:///{tmp_path}/signals-{key}.db"
    _store_verified_template(signal_url, _STEWARD_OUTSIDE_TEMPLATE, template_id="steward-outside")
    seams, _ = _seams(tmp_path, key=key, governed=True, signal_url=signal_url)

    platform = build_platform(_api_settings(demo_meridian_fixture_path=str(_FIXTURE)), seams)

    assert platform.governed_local_t3_release is not None
    # Still advertised (the pre-existing behaviour this test refuses to "improve" into
    # a boot failure): the tool exists, and invoking it is what fails.
    assert "read.kg.steward_outside" in {spec.name for spec in platform.executor.tool_specs}
    for engine in platform.engines:
        engine.dispose()


def test_the_bundle_guard_still_fires_under_a_governed_store_overlay(tmp_path: Path) -> None:
    """Scoping the guard to the bundle does not disarm it when an overlay is present.

    Bundle content is build-time content we package, so a packaging mistake must still
    stop the box even on a deployment that has a governed store -- and the refusal must
    name ONLY the bundle template, never the steward row sitting beside it.
    """
    from ne.cli.demo.corpus import ACCOUNTS_DIRECTORY_TEMPLATE

    reset_config_hash_for_testing()
    key = uuid.uuid4().hex
    signal_url = f"sqlite:///{tmp_path}/signals-{key}.db"
    _store_verified_template(signal_url, _STEWARD_OUTSIDE_TEMPLATE, template_id="steward-outside")
    directory = _bundle_with_extra(
        tmp_path, name="accounts_directory", body=ACCOUNTS_DIRECTORY_TEMPLATE
    )
    seams, _ = _seams(
        tmp_path, key=key, governed=True, templates_dir=directory, signal_url=signal_url
    )
    with pytest.raises(ValueError) as excinfo:
        build_platform(_api_settings(demo_meridian_fixture_path=str(_FIXTURE)), seams)
    assert "could only fail at invoke: ['accounts-directory']" in str(excinfo.value)
    assert "steward-outside" not in str(excinfo.value)


def test_the_bundle_guard_is_deliberately_stricter_than_the_runtime_confine(
    tmp_path: Path,
) -> None:
    """SUBSET, not disjointness -- and this pins the gap, so nobody closes it by accident.

    Runtime servability is an INTERSECTION predicate: ``scoped_dataset`` builds the
    dataset from ``principal labels INTERSECT template.graph_classes``, so a template
    scoped ``["bank.customer.read", "all-employees"]`` invoked by a principal holding
    only ``bank.customer.read`` produces a dataset the pinned release ACCEPTS (part 1).
    The boot guard refuses that template in a packaged bundle anyway (part 2): a bundle
    naming a label outside the release is a packaging mistake, and whoever DOES hold
    ``all-employees`` would get the 502 the guard exists to prevent shipping.
    """
    from ne.brain_ontology.verified.scope import release_scoped_dataset
    from ne.brain_ontology.verified.templates import parse_template_toml

    class _OnlyCustomerLabel:
        def expand(self, principal: Principal | str) -> frozenset[str]:
            return frozenset({_CUSTOMER_LABEL})

    # (1) confine ACCEPTS the intersection dataset for this very template.
    platform, _, _ = _build(tmp_path, key=uuid.uuid4().hex, governed=True)
    release = platform.governed_local_t3_release
    assert release is not None
    dataset = release_scoped_dataset(
        release,
        Principal(id="u-ops", groups=(_CUSTOMER_LABEL,)),
        _OnlyCustomerLabel(),
        parse_template_toml(_PARTIALLY_SERVABLE_TEMPLATE),
    )
    assert dataset.default_graphs, "the intersection is servable inside the release"
    for engine in platform.engines:
        engine.dispose()

    # (2) ...and the boot guard refuses it in the bundle regardless.
    reset_config_hash_for_testing()
    directory = _bundle_with_extra(
        tmp_path, name="extra_partial", body=_PARTIALLY_SERVABLE_TEMPLATE
    )
    seams, _ = _seams(tmp_path, key=uuid.uuid4().hex, governed=True, templates_dir=directory)
    with pytest.raises(ValueError, match="could only fail at invoke: \\['extra-partial'\\]"):
        build_platform(_api_settings(demo_meridian_fixture_path=str(_FIXTURE)), seams)


def test_partial_contributor_publication_keeps_the_head_unresolved_until_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One admitted row is not enough to expose a mixed release as current."""
    import sqlalchemy as sa

    from ne.brain.current_source_authority import SqlAlchemyCurrentSourceAuthorityWriter
    from ne.connect.authority_admission import ne_source_contract_heads

    key = uuid.uuid4().hex
    reset_config_hash_for_testing()
    seams, _ = _seams(tmp_path, key=key, governed=True)
    real_finalize = SqlAlchemyCurrentSourceAuthorityWriter.finalize
    calls = 0

    async def _fail_second(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected contributor publication failure")
        return await real_finalize(self, *args, **kwargs)

    monkeypatch.setattr(SqlAlchemyCurrentSourceAuthorityWriter, "finalize", _fail_second)
    with pytest.raises(RuntimeError, match="injected contributor publication failure"):
        build_platform(_api_settings(demo_meridian_fixture_path=str(_FIXTURE)), seams)

    engine = sa.create_engine(f"sqlite:///{tmp_path}/rt-{key}.db")
    with engine.connect() as conn:
        assert conn.execute(sa.select(ne_source_contract_heads.c.resolved)).scalars().all() == [
            False
        ]
    engine.dispose()

    # Exact retry resumes the durable claim and exposes only the fully admitted head.
    monkeypatch.setattr(SqlAlchemyCurrentSourceAuthorityWriter, "finalize", real_finalize)
    reset_config_hash_for_testing()
    platform = build_platform(_api_settings(demo_meridian_fixture_path=str(_FIXTURE)), seams)
    assert platform.governed_local_t3_source_authority is not None
    assert platform.governed_local_t3_source_authority.resolved is True
    for composed_engine in platform.engines:
        composed_engine.dispose()


def test_composition_refuses_to_advertise_a_descriptor_the_load_did_not_produce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The serving materialization and the selected head must be the same release.

    A divergence means one of the two loads read something else, which would leave the
    advertised contract describing a release nothing is serving.
    """
    import ne.api.demo_seams as demo_seams

    real = demo_seams.run_demo_meridian_fixture_load

    def _drifted(fixture_dir: Path, t3_store: Any) -> Any:
        report = real(fixture_dir, t3_store)
        drifted = report.release.model_copy(
            update={"loader_schema_version": 99, "descriptor_hash": ""}
        )
        return report.model_copy(update={"release": drifted})

    monkeypatch.setattr(demo_seams, "run_demo_meridian_fixture_load", _drifted)
    reset_config_hash_for_testing()
    seams, _ = _seams(tmp_path, key=uuid.uuid4().hex, governed=True)
    with pytest.raises(ValueError, match="is not the release selected as the governed"):
        build_platform(_api_settings(demo_meridian_fixture_path=str(_FIXTURE)), seams)


# --- the verified path reads the PINNED release ---------------------------------


def test_the_verified_answer_path_reads_the_pinned_release_not_the_serving_store(
    tmp_path: Path,
) -> None:
    """The strongest composition claim: emptying the serving store changes no answer.

    ``/v1/verified/invoke`` reaches the frozen executor, the executor reaches the
    verified binding, and the binding's store is the pinned read-only release. Vacating
    the SAME ACL graph in the mutable serving gateway therefore leaves the governed
    answer byte-identical — which a gateway-backed binding could not do.
    """
    import httpx

    from ne.brain_ontology import graph_iri_for_label

    reset_config_hash_for_testing()
    seams, verifier = _seams(tmp_path, key=uuid.uuid4().hex, governed=True)
    app = create_app(_api_settings(demo_meridian_fixture_path=str(_FIXTURE)), seams=seams)
    state = get_state(app)
    state.perm_store.upsert_principals(
        [SyncedPrincipal(principal_id="u-ops", kind=PrincipalKind.USER)],
        source="test",
        now=datetime.now(UTC),
    )
    state.perm_store.set_member_groups(
        "u-ops", [_CUSTOMER_LABEL], source="test", now=datetime.now(UTC)
    )
    assert state.ontology is not None
    serving_store = state.ontology.store

    async def _invoke(client: httpx.AsyncClient, token: str) -> Any:
        response = await client.post(
            "/api/v1/verified/invoke",
            json={"template_id": "customers-in-counties", "args": {"q_county": "Richmond"}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        return response.json()["answer"]

    async def _scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ne.test") as client:
            token = verifier.mint("u-ops", groups=[_CUSTOMER_LABEL], department="ops")
            before = await _invoke(client, token)
            assert before["rows"], "the pinned release must serve a real table"
            # Vacate the ACL graph in the MUTABLE serving gateway.
            await serving_store.replace_acl_graph(
                (), graph_iri=graph_iri_for_label(_CUSTOMER_LABEL)
            )
            assert (
                await serving_store.sparql_scoped(
                    "SELECT ?s WHERE { ?s ?p ?o }",
                    dataset=(await _empty_dataset(_CUSTOMER_LABEL)),
                )
                == []
            )
            after = await _invoke(client, token)
            # Everything except the per-execution timestamp must be byte-identical:
            # same columns, same rows in the same order, same citations, same counts.
            assert _without_executed_at(after) == _without_executed_at(before)

    def _without_executed_at(answer: dict[str, Any]) -> dict[str, Any]:
        assert "executed_at" in answer
        return {k: v for k, v in answer.items() if k != "executed_at"}

    async def _empty_dataset(label: str) -> Any:
        from ne.brain_ontology.store.protocols import DatasetSpec

        return DatasetSpec(default_graphs=(graph_iri_for_label(label),))

    run(_scenario())
    for engine in state.engines:
        engine.dispose()


# --- post-boot supersession is LOUD ---------------------------------------------


def test_a_superseding_composition_refuses_reads_and_names_why_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """2026-08-27 outage regression: another composition steals the contract head.

    A second composition of the SAME scope from different release bytes (live case:
    an `ne` CLI run from an older checkout whose loader schema differs) advances the
    durable contract epoch out from under an already-running process. That process's
    source-bearing reads must refuse (the honest ``source_authority_unavailable``
    scope error — never a silently unpinned read), and the refusal must be DIAGNOSED:
    one ERROR line naming the composed head, the durable head that displaced it, and
    the restart remedy — not an opaque per-request scope error and an empty log.
    """
    import logging

    import httpx

    from ne.api.platform import build_platform as _build_platform

    reset_config_hash_for_testing()
    key = uuid.uuid4().hex
    seams, verifier = _seams(tmp_path, key=key, governed=True)
    app = create_app(_api_settings(demo_meridian_fixture_path=str(_FIXTURE)), seams=seams)
    state = get_state(app)
    state.perm_store.upsert_principals(
        [SyncedPrincipal(principal_id="u-ops", kind=PrincipalKind.USER)],
        source="test",
        now=datetime.now(UTC),
    )
    state.perm_store.set_member_groups(
        "u-ops", [_CUSTOMER_LABEL], source="test", now=datetime.now(UTC)
    )

    async def _invoke(client: httpx.AsyncClient, token: str) -> httpx.Response:
        return await client.post(
            "/api/v1/verified/invoke",
            json={"template_id": "customers-in-counties", "args": {"q_county": "Richmond"}},
            headers={"Authorization": f"Bearer {token}"},
        )

    async def _scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ne.test") as client:
            token = verifier.mint("u-ops", groups=[_CUSTOMER_LABEL], department="ops")
            before = await _invoke(client, token)
            assert before.status_code == 200, before.text
            assert before.json()["answer"]["rows"]

            # The superseding composition: same durable state, DIFFERENT release
            # bytes — exactly what the stale-checkout CLI run did to the live box.
            reset_config_hash_for_testing()
            thief_seams, _ = _seams(tmp_path, key=key, governed=True)
            thief = _build_platform(
                _api_settings(demo_meridian_fixture_path=str(_revalued_fixture(tmp_path))),
                thief_seams,
            )
            for engine in thief.engines:
                engine.dispose()

            with caplog.at_level(logging.ERROR, logger="ne.api.platform"):
                after = await _invoke(client, token)
                assert after.status_code == 502, after.text
                # C12: the class NAME reaches the caller; the diagnosis goes to the log.
                assert after.json()["detail"] == "VerifiedAnswerScopeError"
                again = await _invoke(client, token)
                assert again.status_code == 502, again.text
            superseded = [r for r in caplog.records if "SUPERSEDED" in r.getMessage()]
            # Diagnosed LOUDLY, exactly once per distinct durable head — the state is
            # the news, not the request traffic.
            assert len(superseded) == 1
            message = superseded[0].getMessage()
            assert "source_authority_unavailable" in message
            assert "restart" in message

    run(_scenario())
    for engine in state.engines:
        engine.dispose()
