# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-ONTOLOGY-FLYWHEEL: the re-fingerprint PROVABILITY + byte-parity guardians.

The flagship proof of the restart-to-activate design: after a governed apply the
audit records a PROJECTED ``config_hash`` delta, and a FRESH process (simulated by
``reset_config_hash_for_testing`` + a second ``build_platform`` over the SAME
governed store) freezes to EXACTLY that audited hash — and the approved template
is now served as a live ``read.kg.*`` governed tool. No config bundle changed, no
image, no redeploy: an operator ``compose restart`` activated it.

The parity guardian side proves the default-off (verified disabled) hash is
untouched and the enabled-but-empty-governed-store overlay is a fingerprint no-op.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from ne.api.app import create_app
from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.settings import ApiSettings
from ne.audit.config import AuditSettings
from ne.brain_ontology.config import OntologySettings
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Lane
from ne.flywheel.config import FlywheelSettings
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings

from .conftest import (
    AUDIENCE,
    ISSUER,
    FakeTokenVerifier,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
    get_state,
    run,
)

_BOOT_TEMPLATE = """template_id = "boot-directory"
version = 1
description = "A package-shipped boot template."
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
}
\"\"\"
[scope]
graph_classes = ["alpha"]
result_shape = "enumerable"
[answer]
columns = ["account", "name"]
citation_var = "account"
"""


def _template(template_id: str, version: int) -> str:
    return f'''template_id = "{template_id}"
version = {version}
description = "a governed steward template"
sparql = """
SELECT ?account ?name WHERE {{
  #BINDINGS#
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
}}
"""
[scope]
graph_classes = ["alpha"]
result_shape = "enumerable"
[answer]
columns = ["account", "name"]
citation_var = "account"
'''


class _EmptyChunkStore:
    async def query(self, **_kw: Any) -> list[Any]:
        return []

    async def get(self, chunk_ids: Any) -> list[Any]:
        return []

    async def get_filtered(self, chunk_ids: Any, **_kw: Any) -> list[Any]:
        return []

    async def upsert_chunks(self, chunks: Any) -> None:
        return None

    async def delete(self, chunk_ids: Any) -> None:
        return None


def _empty_triple_store(config_hash: str) -> Any:
    from ne.brain_ontology.gateway import ValidatingTripleStore
    from ne.brain_ontology.quarantine import SqliteTripleQuarantine
    from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
    from ne.brain_ontology.store.oxigraph import OxigraphBackend

    settings = OntologySettings(include_ontology_graph=False)

    class _NullSink:
        def emit(self, event: Any) -> None:
            return None

    return ValidatingTripleStore(
        OxigraphBackend(),
        build_validator(settings),
        SqliteTripleQuarantine(),
        _NullSink(),
        settings=settings,
        config_hash=config_hash,
        compiled_shapes=load_compiled_shapes(),
    )


def _registry(verifier: FakeTokenVerifier) -> PluginRegistry:
    registry = PluginRegistry()
    registry.register("token_verifier", "fake", lambda oidc: verifier)
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    registry.register(
        "triple_store", "oxigraph", lambda **kw: _empty_triple_store(kw["config_hash"])
    )
    return registry


def _api_settings() -> ApiSettings:
    # Constructed identically on every build so the ONLY hash input that varies
    # across the create_app lifecycle and the fresh-process rebuild is the
    # governed template store's contents (the isolation the provability test needs).
    return ApiSettings(
        qdrant_url="http://qdrant.test",
        qdrant_collection="t",
        tei_url="http://tei.test",
        steward_roles=("steward",),
        eval_database_url="",
    )


def _seams(
    *,
    key: str,
    tmp_path: Path,
    ontology_settings: OntologySettings | None,
    signal_url: str,
    verifier: FakeTokenVerifier | None = None,
) -> AppSeams:
    runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
    audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
    perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"
    return AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience=AUDIENCE),
            principal_claims=("email", "department"),
        ),
        runtime_settings=RuntimeSettings(session_database_url=runtime_url),
        audit_settings=AuditSettings(database_url=audit_url),
        permissions_settings=PermissionsSettings(database_url=perms_url),
        flywheel_settings=FlywheelSettings(signal_database_url=signal_url),
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
        chunk_store=_EmptyChunkStore(),
        plugin_registry=_registry(verifier or FakeTokenVerifier()),
        ontology_settings=ontology_settings,
    )


def _enabled_settings(tmp_path: Path, key: str) -> OntologySettings:
    boot = tmp_path / f"boot-{key}"
    boot.mkdir()
    (boot / "boot.toml").write_text(_BOOT_TEMPLATE)
    return OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=boot,
        include_ontology_graph=False,
        backend="oxigraph",
    )


@pytest.fixture
def disposer() -> Iterator[Callable[[Platform], None]]:
    built: list[Platform] = []

    def _keep(p: Platform) -> None:
        built.append(p)

    yield _keep
    for p in built:
        for engine in p.engines:
            engine.dispose()


# --- THE FLAGSHIP: re-fingerprint provability -------------------------------


def test_refingerprint_provability(tmp_path: Path, disposer: Callable[[Platform], None]) -> None:
    """A governed apply's PROJECTED config_hash == a fresh restart's FROZEN hash.

    And the applied template becomes a live ``read.kg.*`` governed tool at restart.
    """
    key = uuid.uuid4().hex
    signal_url = f"sqlite:///{tmp_path}/signals-{key}.db"
    ontology_settings = _enabled_settings(tmp_path, key)
    toml_text = _template("orders-by-date", 1)

    # --- process #1: the running platform performs the governed lifecycle ----
    reset_config_hash_for_testing()
    verifier = FakeTokenVerifier()  # the SAME instance the app installs (mint↔verify shared state).
    app = create_app(
        _api_settings(),
        seams=_seams(
            key=key,
            tmp_path=tmp_path,
            ontology_settings=ontology_settings,
            signal_url=signal_url,
            verifier=verifier,
        ),
    )
    hdr = {"Authorization": f"Bearer {verifier.mint('user:steward', groups=('steward',), sid='s')}"}

    async def _lifecycle() -> dict[str, Any]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ne.test") as client:
            r = await client.post(
                "/api/v1/steward/proposals/verified-template",
                json={"template_toml": toml_text, "summary": "add orders"},
                headers=hdr,
            )
            assert r.status_code == 200, r.text
            pid = r.json()["proposal_id"]
            approve = await client.post(
                f"/api/v1/steward/proposals/{pid}/approve", json={"note": ""}, headers=hdr
            )
            assert approve.status_code == 200, approve.text
            apply = await client.post(
                f"/api/v1/steward/proposals/{pid}/apply-verified-template",
                json={"note": "ship it"},
                headers=hdr,
            )
            assert apply.status_code == 200, apply.text
            return apply.json()

    body = run(_lifecycle())
    audited_new_config_hash = body["new_config_hash"]
    running_hash = get_state(app).config_hash
    assert body["old_config_hash"] == running_hash
    assert audited_new_config_hash != running_hash
    for engine in get_state(app).engines:
        engine.dispose()

    # --- process #2: a fresh boot over the SAME governed store ----------------
    # No config bundle changed; only the governed ne_verified_templates store now
    # holds the applied row. The fresh freeze must land on the AUDITED hash exactly.
    reset_config_hash_for_testing()
    platform = build_platform(
        _api_settings(),
        _seams(
            key=key, tmp_path=tmp_path, ontology_settings=ontology_settings, signal_url=signal_url
        ),
    )
    disposer(platform)

    # PROVABILITY: the fresh frozen hash equals the audited projection EXACTLY.
    assert platform.config_hash == audited_new_config_hash

    # And the approved template is now a live governed tool (read.kg.orders_by_date).
    tool_names = {spec.name for spec in platform.executor.tool_specs}
    assert "read.kg.orders_by_date" in tool_names
    # The boot template still resolves too (overlay is additive, not a replacement).
    assert "read.kg.boot_directory" in tool_names


# --- byte-parity guardians --------------------------------------------------


def test_default_off_config_hash_stable(
    tmp_path: Path, disposer: Callable[[Platform], None]
) -> None:
    """Verified DISABLED ⇒ two builds over identical inputs freeze the same hash.

    And no verified-curation seam is attached (the default-off surface is inert).
    """
    key = "shared"
    hashes: list[str] = []
    for _ in range(2):
        reset_config_hash_for_testing()
        platform = build_platform(
            _api_settings(),
            _seams(key=key, tmp_path=tmp_path, ontology_settings=None, signal_url=""),
        )
        disposer(platform)
        assert platform.verified_curation is None
        hashes.append(platform.config_hash)
    assert hashes[0] == hashes[1]


def test_enabled_empty_store_overlay_is_noop(
    tmp_path: Path, disposer: Callable[[Platform], None]
) -> None:
    """Enabled + EMPTY governed store: the overlay is a fingerprint no-op.

    The overlaid (boot + empty-store) fingerprint is byte-identical to the
    boot-dir-only load, and the projector is self-consistent with the frozen hash.
    """
    from ne.api.verified_tools import load_verified_answer_setup
    from ne.brain_ontology.verified.templates import load_template_registry

    key = uuid.uuid4().hex
    signal_url = f"sqlite:///{tmp_path}/signals-{key}.db"
    ontology_settings = _enabled_settings(tmp_path, key)

    reset_config_hash_for_testing()
    platform = build_platform(
        _api_settings(),
        _seams(
            key=key, tmp_path=tmp_path, ontology_settings=ontology_settings, signal_url=signal_url
        ),
    )
    disposer(platform)
    assert platform.verified_curation is not None

    # Overlay-with-empty-store fingerprint == boot-dir-only fingerprint (parity anchor).
    boot_dir = ontology_settings.verified_templates_dir
    assert boot_dir is not None
    boot_only = load_template_registry(boot_dir).fingerprint
    overlaid = load_verified_answer_setup(ontology_settings, governed_store_url=signal_url)
    assert overlaid is not None
    assert overlaid.fingerprint == boot_only

    # Projector self-consistency: projecting the CURRENT registry reproduces the
    # frozen platform hash (the boot-time drift tripwire, re-asserted explicitly).
    projected = platform.verified_curation.project_config_hash(overlaid.registry)
    assert projected == platform.config_hash
