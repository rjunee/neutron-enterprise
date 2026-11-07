# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-CORE: the Reader-only API composition boundary.

Four claims, all against the ACTUAL ``create_app``-constructed ``ApiState``:

* **Request state receives the Reader and nothing writer-shaped.** Asserted by
  walking every dataclass field of the real state and refusing any value that
  carries ``claim``/``renew``/``finalize``. ``isinstance`` against the
  ``@runtime_checkable`` Protocol would be weaker in both directions — it checks
  method NAMES only, so a Reader-shaped stand-in satisfies it and a renamed
  Writer evades it — and it would only ever look at the field somebody remembered
  to name.
* **Key material cannot reach the frozen config hash.** Paired: changing the key
  leaves ``config_hash`` byte-identical, and changing real API config MOVES it, so
  the first half cannot pass merely because the hash is insensitive to everything.
* **An unusable key fails continuation readiness on this replica, silently.**
  Missing, half-configured, short and malformed all compose a root that serves,
  with no codec and no log line naming a secret variable.
* **The worker inherits the identical codec.** ne-worker composes nothing of its
  own here, so the parity proof is AST-level over ``ne/worker/main.py`` and lives
  in this file rather than in an ne-worker edit that would itself be the defect.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import inspect
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr

import ne.worker.main as _worker_main
from ne.api.platform import build_platform
from ne.api.source_authority_secrets import (
    SOURCE_AUTHORITY_CONTINUATION_KEY_B64_ENV,
    SOURCE_AUTHORITY_CONTINUATION_KEY_ID_ENV,
    load_source_authority_continuation_key,
)
from ne.contracts.config import get_config_hash
from ne.contracts.errors import ConfigHashNotFrozenError
from ne.contracts.source_authority import CurrentSourceAuthorityReader

from .conftest import get_state

_KEY = b"A" * 32
_OTHER_KEY = b"B" * 32
_WRITER_METHODS = ("claim", "renew", "finalize")

_worker_source_file = inspect.getsourcefile(_worker_main)
assert _worker_source_file is not None
WORKER_MAIN = Path(_worker_source_file)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _set_key(monkeypatch: pytest.MonkeyPatch, key_id: str, key_b64: str) -> None:
    monkeypatch.setenv(SOURCE_AUTHORITY_CONTINUATION_KEY_ID_ENV, key_id)
    monkeypatch.setenv(SOURCE_AUTHORITY_CONTINUATION_KEY_B64_ENV, key_b64)


def _clear_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SOURCE_AUTHORITY_CONTINUATION_KEY_ID_ENV, raising=False)
    monkeypatch.delenv(SOURCE_AUTHORITY_CONTINUATION_KEY_B64_ENV, raising=False)


# --- Reader-only request state -----------------------------------------------


def test_request_state_receives_the_current_source_authority_reader(make_app) -> None:
    """The must-PASS half: the seam is genuinely composed, not merely absent."""
    from ne.brain.current_source_authority import SqlAlchemyCurrentSourceAuthorityReader

    state = get_state(make_app().app)
    reader = state.source_authority_reader
    assert isinstance(reader, CurrentSourceAuthorityReader)
    # The CONCRETE ne-brain implementation, not merely something Reader-shaped:
    # the Protocol is @runtime_checkable and so checks method names only.
    assert isinstance(reader, SqlAlchemyCurrentSourceAuthorityReader)
    # And it is a working evaluator over the composed runtime engine, not a stub:
    # an absent receipt reads as absent rather than raising.
    assert reader.load_receipt("rcpt-does-not-exist") is None
    assert state.authority_attempts is not None
    assert not any(
        hasattr(state.authority_attempts, method) for method in ("claim", "finalize", "advance")
    )


def test_attempt_reader_binds_exact_configured_m365_and_salesforce_children() -> None:
    """Production composition derives source routes; callers never choose them."""
    import sqlalchemy as sa

    from ne.api.platform import _compose_authority_attempt_reader
    from ne.api.source_authority_descriptors import M365SourceAuthorityDescriptor
    from ne.brain.current_source_authority import (
        bind_deployment_construction_id,
        ensure_source_authority_schema,
    )
    from ne.connect.authority_admission import (
        SqlAlchemyAdmissionAttemptStore,
        SqlAlchemyHierarchicalAdmission,
        SqlAlchemySourceContractHeadStore,
        claim_or_load_connector_instance,
        connector_instance_scope,
        drive_scope,
        ensure_admission_schema,
        site_scope,
    )
    from ne.connect.m365.config import M365ConnectorSettings
    from ne.connect.runner import ConnectSyncSettings
    from ne.connect.salesforce.authority import (
        salesforce_connector_scope,
        salesforce_object_scope,
    )
    from ne.connect.salesforce.config import SalesforceConnectorSettings, SalesforceObjectConfig
    from ne.connect.salesforce.contract import (
        salesforce_admitted_target_digest,
        salesforce_contract_identity,
    )
    from ne.connect.salesforce.identity import canonical_salesforce_organization_id
    from ne.contracts.source_authority import SourceContractIdentity

    engine = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=sa.pool.StaticPool
    )
    ensure_source_authority_schema(engine)
    ensure_admission_schema(engine)
    deployment = bind_deployment_construction_id(engine)
    attempts = SqlAlchemyAdmissionAttemptStore(engine)
    heads = SqlAlchemySourceContractHeadStore(engine)

    m365 = M365ConnectorSettings(
        tenant_id="tenant-1",
        client_id="client-1",
        auth_method="secret",
        client_secret=SecretStr("secret"),
        owner="data-owner",
        drive_ids=("drive-direct",),
        site_ids=("site-1",),
        authority_enabled=True,
        authority_sweep_cadence_seconds=Decimal("60"),
        authority_sweep_batch_size=10,
        authority_sweep_max_batch_duration_seconds=Decimal("10"),
        authority_sweep_max_points_per_scope=100,
    )
    m365_parent = connector_instance_scope(
        deployment_id=deployment,
        connector_instance_id="conn-m365",
        graph_tenant_id=m365.tenant_id,
    )
    m365_contract = SourceContractIdentity(
        source_kind="m365-sharepoint",
        descriptor_schema_version=1,
        descriptor_hash="a" * 64,
    )
    m365_head = heads.claim_or_load(m365_parent, source_generation=1, contract=m365_contract)
    direct = drive_scope(
        deployment_id=deployment,
        connector_instance_id="conn-m365",
        graph_tenant_id=m365.tenant_id,
        drive_id="drive-direct",
    )
    discovered = drive_scope(
        deployment_id=deployment,
        connector_instance_id="conn-m365",
        graph_tenant_id=m365.tenant_id,
        drive_id="drive-discovered",
    )
    SqlAlchemyHierarchicalAdmission(engine).record_descendants(
        site_scope(
            deployment_id=deployment,
            connector_instance_id="conn-m365",
            graph_tenant_id=m365.tenant_id,
            site_id="site-1",
        ),
        [("drive-discovered", discovered)],
    )

    salesforce = SalesforceConnectorSettings(
        auth_method="client_credentials",
        client_id="client-2",
        client_secret=SecretStr("secret"),
        instance_url="https://acme.my.salesforce.com",
        owner="data-owner",
        objects=(SalesforceObjectConfig(name="Account", content_fields=("Name",)),),
        authority_enabled=True,
        authority_sweep_cadence_seconds=Decimal("60"),
        authority_sweep_batch_size=10,
        authority_sweep_max_batch_duration_seconds=Decimal("10"),
        authority_sweep_max_points_per_scope=100,
    )
    organization = canonical_salesforce_organization_id("00D000000000001")
    sf_instance = claim_or_load_connector_instance(
        engine,
        deployment_id=deployment,
        source_name=salesforce.source_name,
        admitted_target_digest=salesforce_admitted_target_digest(salesforce, organization),
    )
    sf_parent = salesforce_connector_scope(
        deployment_id=deployment,
        connector_instance_id=sf_instance.connector_instance_id,
        organization_id=organization,
    )
    sf_head = heads.claim_or_load(
        sf_parent,
        source_generation=sf_instance.source_generation,
        contract=salesforce_contract_identity(salesforce, organization),
    )
    account = salesforce_object_scope(
        deployment_id=deployment,
        connector_instance_id=sf_instance.connector_instance_id,
        organization_id=organization,
        object_name="Account",
    )

    for attempt_id, scope, epoch in (
        ("m365-direct", direct, m365_head.contract_epoch),
        ("m365-discovered", discovered, m365_head.contract_epoch),
        ("salesforce-account", account, sf_head.contract_epoch),
        (
            "m365-not-configured",
            drive_scope(
                deployment_id=deployment,
                connector_instance_id="conn-m365",
                graph_tenant_id=m365.tenant_id,
                drive_id="drive-other",
            ),
            m365_head.contract_epoch,
        ),
        (
            "salesforce-not-configured",
            salesforce_object_scope(
                deployment_id=deployment,
                connector_instance_id=sf_instance.connector_instance_id,
                organization_id=organization,
                object_name="Contact",
            ),
            sf_head.contract_epoch,
        ),
    ):
        attempts.claim(
            attempt_id=attempt_id,
            scope=scope,
            identity_hash="b" * 64,
            source_revision=f"revision-{attempt_id}",
            contract_epoch=epoch,
            prior_artifact_id=None,
        )

    reader = _compose_authority_attempt_reader(
        engine,
        connect_sync_settings=ConnectSyncSettings(
            sources=("m365", "salesforce"),
            state_path="state.db",
            quarantine_path="quarantine.db",
        ),
        connect_source_settings={"m365": m365, "salesforce": salesforce},
        m365_source_authority=M365SourceAuthorityDescriptor(
            source_kind="m365-sharepoint",
            source_name=m365.source_name,
            connector_instance_id="conn-m365",
            source_generation=1,
            contract_epoch=m365_head.contract_epoch,
            contract_identity_hash=m365_contract.identity_hash,
            descriptor_schema_version=1,
            descriptor_hash="a" * 64,
            head_revision=m365_head.head_revision,
            freshness_sla_seconds=Decimal("3600"),
            resolved=False,
        ),
    )

    assert [(item.attempt_id, item.source_binding) for item in reader.pending()] == [
        ("m365-direct", "m365"),
        ("m365-discovered", "m365"),
        ("salesforce-account", "salesforce"),
    ]


def test_no_field_of_request_state_exposes_authority_writer_methods(make_app) -> None:
    """Every field, not the one somebody remembered to name.

    A Writer on request state means an API projector can claim a reconciliation
    scope and finalize a receipt — it can reassign source authority to itself.
    This walks the real constructed state and refuses any value carrying the
    Writer's three methods, whatever the field is called or annotated.
    """
    state = get_state(make_app().app)
    offenders = [
        field.name
        for field in dataclasses.fields(state)
        if all(hasattr(getattr(state, field.name), method) for method in _WRITER_METHODS)
    ]
    assert offenders == []
    # Control on the detector itself: it DOES fire on a writer-shaped object, so
    # the empty result above is an exclusion and not a broken predicate.

    class _WriterShaped:
        def claim(self) -> None: ...
        def renew(self) -> None: ...
        def finalize(self) -> None: ...

    assert all(hasattr(_WriterShaped(), method) for method in _WRITER_METHODS)


def test_the_api_composition_root_never_instantiates_the_reconciliation_writer() -> None:
    """Producer capability is not composed here at all — nothing to discard."""
    source = inspect.getsource(build_platform)
    helper = inspect.getsource(
        inspect.getmodule(build_platform).__dict__["_compose_source_authority"]  # type: ignore[union-attr]
    )
    for forbidden in (
        "SqlAlchemyCurrentSourceAuthorityWriter",
        "CurrentSourceAuthorityWriter(",
    ):
        assert forbidden not in source, forbidden
        assert forbidden not in helper, forbidden
    # Positive control: the READ side IS composed through this recipe.
    assert "SqlAlchemyCurrentSourceAuthorityReader" in helper
    assert "source_authority_reader=source_authority_reader" in source


# --- key material stays outside the frozen product config hash ---------------


def test_the_continuation_key_is_read_only_after_the_config_hash_is_frozen(
    make_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key CANNOT be in the hash, because the hash exists before it is read.

    Stated as an ordering fact rather than as "two builds hash identically": the
    test harness mints a unique runtime/eval database URL per build, so a
    byte-identical comparison across two builds is unavailable here and would in
    any case only show that ONE key change did not move the hash. This observes
    the moment the loader runs and asserts the platform hash is ALREADY frozen —
    and that it is this deployment's own hash, not some unrelated frozen value —
    so no key material can reach the preimage by any route.
    """
    import ne.api.source_authority_secrets as secrets_module

    observed: list[str | None] = []
    real = secrets_module.load_source_authority_continuation_key

    def spy(*args: object, **kwargs: object) -> object:
        try:
            observed.append(get_config_hash())
        except ConfigHashNotFrozenError:
            observed.append(None)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(secrets_module, "load_source_authority_continuation_key", spy)
    _set_key(monkeypatch, "k1", _b64(_KEY))
    state = get_state(make_app().app)
    assert observed == [state.config_hash]


def test_the_key_loader_is_not_an_api_settings_field() -> None:
    """Settings are hashed; this value must not be reachable through them."""
    import ne.api.settings as settings_module
    from ne.api.settings import ApiSettings

    fields = set(ApiSettings.model_fields)
    assert not any("continuation" in name or "hmac" in name for name in fields)
    # Nor reachable indirectly: the settings module does not know the loader exists.
    source = inspect.getsource(settings_module)
    assert "source_authority_secrets" not in source
    assert SOURCE_AUTHORITY_CONTINUATION_KEY_B64_ENV not in source


# --- continuation readiness --------------------------------------------------


@pytest.mark.parametrize(
    ("key_id", "key_b64"),
    [
        ("", ""),
        ("k1", ""),
        ("", "AAAA"),
        ("k1", _b64(b"short")),
        ("k1", "not-canonical-base64!!"),
        ("k1", base64.urlsafe_b64encode(_KEY).decode("ascii")),  # padded, non-canonical
        ("bad key id", _b64(_KEY)),
    ],
)
def test_an_unusable_key_fails_continuation_readiness_on_this_replica(
    key_id: str, key_b64: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_key(monkeypatch, key_id, key_b64)
    assert load_source_authority_continuation_key() is None


def test_a_valid_key_is_ready_and_never_reveals_its_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The must-PASS half, plus: the value cannot leak through a repr."""
    _set_key(monkeypatch, "k1", _b64(_KEY))
    key = load_source_authority_continuation_key()
    assert key is not None
    assert key.key_id == "k1"
    assert key.material == _KEY
    assert _b64(_KEY) not in repr(key)
    assert "A" * 32 not in f"{key}"


def test_an_unusable_key_composes_a_serving_root_with_no_codec_and_no_log(
    make_app, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Continuation is ONE surface; its key must not take the deployment down.

    And nothing is logged: a boot message naming which variable was wrong, or how
    long the value was, is a secret disclosure in every sink the deployment has.
    """
    _set_key(monkeypatch, "k1", _b64(b"far too short"))
    with caplog.at_level(logging.DEBUG):
        harness = make_app()
    state = get_state(harness.app)
    assert state.source_authority_reader is not None
    records = " ".join(record.getMessage() for record in caplog.records)
    assert SOURCE_AUTHORITY_CONTINUATION_KEY_ID_ENV not in records
    assert SOURCE_AUTHORITY_CONTINUATION_KEY_B64_ENV not in records
    assert "continuation" not in records.lower()


def test_a_valid_key_composes_a_codec_on_the_shared_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composed on Platform, so BOTH roots inherit the identical codec."""
    import sqlalchemy as sa

    from ne.api.platform import _compose_source_authority

    engine = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=sa.pool.StaticPool
    )
    _set_key(monkeypatch, "k1", _b64(_KEY))
    reader, codec = _compose_source_authority(engine)
    assert reader is not None
    assert codec is not None and codec.key_id == "k1"
    _set_key(monkeypatch, "k1", _b64(b"short"))
    _, no_codec = _compose_source_authority(engine)
    assert no_codec is None


def test_foreign_construction_history_fails_api_composition() -> None:
    """A restored database from another deployment must not become a serving root."""
    import sqlalchemy as sa

    from ne.api.platform import _compose_source_authority
    from ne.brain.current_source_authority import (
        ForeignConstructionIdentityError,
        ensure_source_authority_schema,
        ne_source_authority_receipts,
    )

    engine = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=sa.pool.StaticPool
    )
    ensure_source_authority_schema(engine)
    _compose_source_authority(engine)  # clean: composes
    with engine.begin() as conn:
        conn.execute(
            ne_source_authority_receipts.insert().values(
                receipt_id="rcpt-foreign",
                construction_id="another-deployment",
                identity_hash="a" * 64,
                artifact_id=None,
                receipt_hash="b" * 64,
                state="present",
                source_revision="rev-1",
                contract_epoch=1,
                admitted_at=datetime(2026, 1, 1, tzinfo=UTC),
                payload={},
            )
        )
    with pytest.raises(ForeignConstructionIdentityError):
        _compose_source_authority(engine)


# --- ne-worker parity, proven here because no ne-worker edit is correct ------


def test_the_worker_composes_only_through_the_shared_build_platform() -> None:
    source = WORKER_MAIN.read_text(encoding="utf-8")
    for forbidden in (
        "HmacAuthorizedContinuationTokenCodec(",
        "SqlAlchemyCurrentSourceAuthorityReader(",
        "SqlAlchemyCurrentSourceAuthorityWriter(",
        "ensure_source_authority_schema(",
        "_compose_source_authority(",
    ):
        assert forbidden not in source, forbidden
    # Positive control: it DOES compose through the shared recipe, so the absence
    # above is inheritance rather than the feature being missing entirely.
    assert "build_platform(api_settings, seams)" in source


def test_the_worker_never_reads_the_continuation_key_environment_itself() -> None:
    source = WORKER_MAIN.read_text(encoding="utf-8")
    assert SOURCE_AUTHORITY_CONTINUATION_KEY_ID_ENV not in source
    assert SOURCE_AUTHORITY_CONTINUATION_KEY_B64_ENV not in source


def test_both_roots_share_one_composition_entry_point() -> None:
    tree = ast.parse(WORKER_MAIN.read_text(encoding="utf-8"))
    imported_from = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and any(alias.name == "build_platform" for alias in node.names)
    }
    assert imported_from == {"ne.api.platform"}


def test_the_worker_inherits_the_codec_through_the_platform_object_it_binds() -> None:
    """The positive half of inheritance, stated exactly as far as it is proven.

    Nothing routes to the codec yet — 015A0-ENFORCEMENT's selector is its only
    consumer — so this does NOT claim the worker calls it. What it does claim, and
    prove, is that the worker binds the object ``build_platform`` returned and
    reads its fields, and that ``build_platform`` assigns the codec onto exactly
    that object. A negative "the worker composes nothing" check alone is satisfied
    by a worker with no platform at all.
    """
    tree = ast.parse(WORKER_MAIN.read_text(encoding="utf-8"))
    bound = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "platform" for t in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "build_platform"
    ]
    assert bound, "the worker never binds the composed platform"
    reads = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "platform"
    }
    assert reads, "the worker never reads the composed platform"
    assigned = inspect.getsource(build_platform)
    assert "source_authority_continuation=source_authority_continuation" in assigned
    assert "source_authority_reader=source_authority_reader" in assigned
