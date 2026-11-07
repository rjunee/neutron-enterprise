# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-ONTOLOGY-FLYWHEEL: the governed HITL verified-answer-template curation lane.

The governed spine is NEVER mocked: propose/approve/apply ride the REAL NE-066
review gate + the tamper-evident audit chain over SQLite, and the template is
validated by the REAL ``QueryTemplate`` load gate. Only the model/embedder/qdrant
edges + the token verifier are faked (conftest helpers).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from ne.api.app import create_app
from ne.api.platform import AppSeams
from ne.api.settings import ApiSettings
from ne.audit.config import AuditSettings
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.verified.store import read_verified_templates
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Lane
from ne.flywheel.config import FlywheelSettings
from ne.flywheel.proposals import ProposalKind, ne_fix_proposals
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

# --- template fixtures ------------------------------------------------------

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


def _template(template_id: str, version: int, description: str = "d") -> str:
    return f'''template_id = "{template_id}"
version = {version}
description = "{description}"
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


#: A template that FAILS the load gate: a non-SELECT / banned-keyword body.
_INVALID_TEMPLATE = """template_id = "bad-one"
version = 1
description = "names a banned keyword"
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  SERVICE <https://evil/sparql> { ?account <https://x/name> ?name . }
}
\"\"\"
[scope]
graph_classes = ["alpha"]
result_shape = "enumerable"
[answer]
columns = ["account", "name"]
citation_var = "account"
"""


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


@dataclass
class CurationHarness:
    app: Any
    verifier: FakeTokenVerifier

    def bearer(
        self, subject: str = "user:steward", *, groups: tuple[str, ...] = ("steward",)
    ) -> dict[str, str]:
        token = self.verifier.mint(subject, groups=groups, sid="sid-1")
        return {"Authorization": f"Bearer {token}"}

    def client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app)
        return httpx.AsyncClient(transport=transport, base_url="http://ne.test")

    def audit_actions(self) -> list[tuple[str, dict[str, Any]]]:
        # Surface audits (emit_surface_audit) land on the audit engine; the
        # governed dual-append chain events (verified_template.propose/apply,
        # proposal.*) land on the FLYWHEEL engine (same-txn append_record). Read
        # BOTH so the test sees the whole record set.
        state = get_state(self.app)
        engines = [state.audit_engine]
        if state.proposal_store is not None:
            engines.append(state.proposal_store.engine)
        out: list[tuple[str, dict[str, Any]]] = []
        for engine in engines:
            with engine.connect() as conn:
                rows = conn.execute(
                    sa.text("SELECT event_type, action, payload FROM ne_audit_log")
                ).fetchall()
            for event_type, action, payload in rows:
                data = payload if isinstance(payload, dict) else json.loads(payload)
                # Surface audits store the action in the 'action' column; chain
                # events store the AuditEvent action inside the json payload.
                key = str(action) if action else str(event_type)
                out.append((key, data))
        return out

    def stored_templates(self) -> list[Any]:
        state = get_state(self.app)
        assert state.proposal_store is not None
        return read_verified_templates(state.proposal_store.engine)

    def proposal_count(self) -> int:
        state = get_state(self.app)
        assert state.proposal_store is not None
        with state.proposal_store.engine.connect() as conn:
            return int(
                conn.execute(sa.select(sa.func.count()).select_from(ne_fix_proposals)).scalar_one()
            )


CurationBuilder = Callable[..., CurationHarness]


@pytest.fixture
def make_curation_app(tmp_path: Path) -> Iterator[CurationBuilder]:
    built: list[CurationHarness] = []

    def _make(
        *,
        steward_roles: tuple[str, ...] = ("steward",),
        verified_enabled: bool = True,
        flywheel: bool = True,
    ) -> CurationHarness:
        registry = PluginRegistry()
        verifier = FakeTokenVerifier()
        registry.register("token_verifier", "fake", lambda oidc: verifier)
        registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
        registry.register(
            "triple_store", "oxigraph", lambda **kw: _empty_triple_store(kw["config_hash"])
        )

        key = uuid.uuid4().hex
        runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
        audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
        perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"
        signal_url = f"sqlite:///{tmp_path}/signals-{key}.db" if flywheel else ""

        ontology_settings: OntologySettings | None = None
        if verified_enabled:
            boot = tmp_path / f"boot-{key}"
            boot.mkdir()
            (boot / "boot.toml").write_text(_BOOT_TEMPLATE)
            ontology_settings = OntologySettings(
                verified_answers_enabled=True,
                verified_templates_dir=boot,
                include_ontology_graph=False,
                backend="oxigraph",
            )

        seams = AppSeams(
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
            plugin_registry=registry,
            ontology_settings=ontology_settings,
        )
        app = create_app(
            ApiSettings(
                qdrant_url="http://qdrant.test",
                qdrant_collection="t",
                tei_url="http://tei.test",
                steward_roles=steward_roles,
                eval_database_url="",
            ),
            seams=seams,
        )
        harness = CurationHarness(app=app, verifier=verifier)
        built.append(harness)
        return harness

    yield _make
    for h in built:
        for engine in get_state(h.app).engines:
            engine.dispose()


_PROPOSE = "/api/v1/steward/proposals/verified-template"


def _apply_url(pid: str) -> str:
    return f"/api/v1/steward/proposals/{pid}/apply-verified-template"


# --- SPEC-row test 1: fail-closed propose -----------------------------------


def test_propose_invalid_template_is_422_and_writes_nothing(
    make_curation_app: CurationBuilder,
) -> None:
    h = make_curation_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                _PROPOSE,
                json={"template_toml": _INVALID_TEMPLATE, "summary": "please add"},
                headers=h.bearer(),
            )
            assert r.status_code == 422

    run(_t())
    # No proposal row, no template row (fail-closed propose).
    assert h.proposal_count() == 0
    assert h.stored_templates() == []
    # A denied propose-invalid audit event exists (error class only, never the TOML).
    actions = h.audit_actions()
    denied = [d for a, d in actions if a == "api.steward.verified_propose_invalid"]
    assert denied, actions
    # C12: the raw TOML (its distinctive evil endpoint) never enters any audit.
    assert "evil/sparql" not in json.dumps(actions)


# --- SPEC-row test 2: persist + re-fingerprint + config_hash change + audit --


def test_propose_approve_apply_end_to_end(make_curation_app: CurationBuilder) -> None:
    h = make_curation_app()
    toml_text = _template("orders-by-date", 1, "a new governed template")

    async def _t() -> dict[str, Any]:
        async with h.client() as client:
            hdr = h.bearer()
            r = await client.post(
                _PROPOSE, json={"template_toml": toml_text, "summary": "add orders"}, headers=hdr
            )
            assert r.status_code == 200, r.text
            proposal = r.json()
            assert proposal["kind"] == "verified_answer_candidate"
            assert proposal["lane"] == "ontology"
            pid = proposal["proposal_id"]

            approve = await client.post(
                f"/api/v1/steward/proposals/{pid}/approve", json={"note": ""}, headers=hdr
            )
            assert approve.status_code == 200

            apply = await client.post(_apply_url(pid), json={"note": "ship it"}, headers=hdr)
            assert apply.status_code == 200, apply.text
            return apply.json()

    body = run(_t())
    assert body["restart_required"] is True
    assert body["old_config_hash"] != body["new_config_hash"]
    assert body["old_registry_hash"] != body["new_registry_hash"]
    assert body["review"]["to_status"] == "applied"
    # old_config_hash is the running (frozen) process hash.
    assert body["old_config_hash"] == get_state(h.app).config_hash

    # The template is persisted.
    stored = h.stored_templates()
    assert [(s.template_id, s.version) for s in stored] == [("orders-by-date", 1)]

    # The verified_template.apply chain event records the ids + hash deltas. Each
    # tuple is (action, payload); the json detail sits under payload['detail'].
    actions = h.audit_actions()
    apply_details = [
        json.loads(d["detail"])
        for a, d in actions
        if a == "verified_template.apply" and isinstance(d.get("detail"), str)
    ]
    assert apply_details, actions
    detail = apply_details[0]
    assert detail["template_id"] == "orders-by-date"
    assert detail["version"] == 1
    assert detail["old_config_hash"] != detail["new_config_hash"]
    assert detail["new_config_hash"] == body["new_config_hash"]
    assert detail["restart_required"] is True
    # A verified_template.propose event is also on the chain.
    assert any(a == "verified_template.propose" for a, _d in actions)
    # The raw TOML never enters the audit detail (C12).
    assert "SELECT ?account" not in json.dumps(actions)


# --- SPEC-row test 3: rejected never touches the store ----------------------


def test_rejected_proposal_never_touches_store(make_curation_app: CurationBuilder) -> None:
    h = make_curation_app()
    toml_text = _template("orders-by-date", 1)

    async def _t() -> None:
        async with h.client() as client:
            hdr = h.bearer()
            r = await client.post(
                _PROPOSE, json={"template_toml": toml_text, "summary": "add orders"}, headers=hdr
            )
            pid = r.json()["proposal_id"]
            reject = await client.post(
                f"/api/v1/steward/proposals/{pid}/reject",
                json={"code": "incorrect", "note": ""},
                headers=hdr,
            )
            assert reject.status_code == 200
            # rejected is terminal: a subsequent apply is 409, store stays empty.
            apply = await client.post(_apply_url(pid), json={"note": ""}, headers=hdr)
            assert apply.status_code == 409

    run(_t())
    assert h.stored_templates() == []


# --- SPEC-row test 4 (partial): gate-first apply refusals -------------------


def test_apply_without_approval_is_409_fail_closed(make_curation_app: CurationBuilder) -> None:
    h = make_curation_app()
    toml_text = _template("orders-by-date", 1)

    async def _t() -> None:
        async with h.client() as client:
            hdr = h.bearer()
            r = await client.post(
                _PROPOSE, json={"template_toml": toml_text, "summary": "add"}, headers=hdr
            )
            pid = r.json()["proposal_id"]
            # apply straight from 'proposed' (never approved) → 409, store untouched.
            apply = await client.post(_apply_url(pid), json={"note": ""}, headers=hdr)
            assert apply.status_code == 409

    run(_t())
    assert h.stored_templates() == []


def test_apply_wrong_kind_is_409(make_curation_app: CurationBuilder) -> None:
    from .conftest import seed_proposal

    h = make_curation_app()
    # A non-verified (fix) proposal id; approve it, then try the verified apply.
    pid = seed_proposal(h.app, lane="content", kind=ProposalKind.FIX)

    async def _t() -> None:
        async with h.client() as client:
            hdr = h.bearer()
            await client.post(
                f"/api/v1/steward/proposals/{pid}/approve", json={"note": ""}, headers=hdr
            )
            apply = await client.post(_apply_url(pid), json={"note": ""}, headers=hdr)
            assert apply.status_code == 409

    run(_t())
    assert h.stored_templates() == []


def test_apply_unknown_proposal_is_404(make_curation_app: CurationBuilder) -> None:
    h = make_curation_app()

    async def _t() -> None:
        async with h.client() as client:
            apply = await client.post(
                _apply_url("sha256:does-not-exist"), json={}, headers=h.bearer()
            )
            assert apply.status_code == 404

    run(_t())


# --- double apply: second is 409, exactly one row ---------------------------


def test_double_apply_second_is_409_single_row(make_curation_app: CurationBuilder) -> None:
    h = make_curation_app()
    toml_text = _template("orders-by-date", 1)

    async def _t() -> None:
        async with h.client() as client:
            hdr = h.bearer()
            pid = (
                await client.post(
                    _PROPOSE, json={"template_toml": toml_text, "summary": "add"}, headers=hdr
                )
            ).json()["proposal_id"]
            await client.post(
                f"/api/v1/steward/proposals/{pid}/approve", json={"note": ""}, headers=hdr
            )
            first = await client.post(_apply_url(pid), json={"note": ""}, headers=hdr)
            assert first.status_code == 200
            second = await client.post(_apply_url(pid), json={"note": ""}, headers=hdr)
            assert second.status_code == 409

    run(_t())
    assert len(h.stored_templates()) == 1


def test_crash_between_persist_and_record_apply_recovers(
    make_curation_app: CurationBuilder,
) -> None:
    """A crash AFTER the persist/audit commit but BEFORE record_apply must recover.

    Retrying the still-``approved`` proposal finds this EXACT (template_id, version,
    sha) already persisted; it must treat that as an idempotent retry (skip version
    discipline) and fold to 'applied' — NOT strand the proposal by rejecting its own
    committed persist as "not the next version" (P1 crash-recovery).
    """
    import hashlib
    from datetime import UTC, datetime

    from ne.brain_ontology.verified.store import (
        VerifiedTemplateRecord,
        append_verified_template,
    )

    h = make_curation_app()
    toml_text = _template("orders-by-date", 1)
    sha = hashlib.sha256(toml_text.encode("utf-8")).hexdigest()

    async def _t() -> tuple[int, dict[str, Any]]:
        async with h.client() as client:
            hdr = h.bearer()
            pid = (
                await client.post(
                    _PROPOSE, json={"template_toml": toml_text, "summary": "add"}, headers=hdr
                )
            ).json()["proposal_id"]
            await client.post(
                f"/api/v1/steward/proposals/{pid}/approve", json={"note": ""}, headers=hdr
            )
            # Simulate the crash: step (4)'s governed persist committed, but
            # record_apply never ran — the proposal is still 'approved'.
            state = get_state(h.app)
            assert state.proposal_store is not None
            with state.proposal_store.engine.begin() as conn:
                append_verified_template(
                    conn,
                    VerifiedTemplateRecord(
                        template_id="orders-by-date",
                        version=1,
                        template_toml=toml_text,
                        toml_sha256=sha,
                        proposal_id=pid,
                        actor="user:steward",
                        config_hash=state.config_hash,
                        created_at=datetime.now(UTC),
                    ),
                )
            # Retry the apply — must RECOVER (200 + fold), not 422 version-discipline.
            apply = await client.post(_apply_url(pid), json={"note": ""}, headers=hdr)
            return apply.status_code, apply.json()

    code, body = run(_t())
    assert code == 200, body
    assert body["review"]["to_status"] == "applied"
    # Idempotent: exactly ONE persisted row despite the pre-seeded persist + retry.
    assert len(h.stored_templates()) == 1


# --- guard ladder: 403 > 503 > 409 ------------------------------------------


def test_unauthorized_is_403_before_body_validation(make_curation_app: CurationBuilder) -> None:
    h = make_curation_app(steward_roles=("steward",))

    async def _t() -> None:
        async with h.client() as client:
            # A non-steward with a MALFORMED body is still 403 (dependency runs first).
            r = await client.post(
                _PROPOSE, json={"bogus": 1}, headers=h.bearer("user:nobody", groups=("user",))
            )
            assert r.status_code == 403

    run(_t())


def test_verified_disabled_is_409(make_curation_app: CurationBuilder) -> None:
    # Flywheel configured but verified answers OFF ⇒ state.verified_curation is None.
    h = make_curation_app(verified_enabled=False)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                _PROPOSE,
                json={"template_toml": _template("x", 1), "summary": "x"},
                headers=h.bearer(),
            )
            assert r.status_code == 409
            assert "not enabled" in r.json()["detail"]

    run(_t())


def test_flywheel_unconfigured_is_503(make_curation_app: CurationBuilder) -> None:
    # No flywheel store ⇒ review_service/proposal_store None ⇒ 503 (after 403).
    h = make_curation_app(verified_enabled=False, flywheel=False)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                _PROPOSE,
                json={"template_toml": _template("x", 1), "summary": "x"},
                headers=h.bearer(),
            )
            assert r.status_code == 503

    run(_t())


# --- version discipline -----------------------------------------------------


def test_new_template_must_be_v1(make_curation_app: CurationBuilder) -> None:
    h = make_curation_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                _PROPOSE,
                json={"template_toml": _template("orders-by-date", 3), "summary": "x"},
                headers=h.bearer(),
            )
            assert r.status_code == 422

    run(_t())
    assert h.proposal_count() == 0


def test_no_raw_write_path_exists() -> None:
    """Structural: ``ne_verified_templates`` is written ONLY by the governed apply.

    The append primitive is referenced ONLY in the store module (its definition)
    and the ne-api curation orchestration; schema-ensure appears additionally in
    the boot seams. No other module can write the governed template store.
    """
    root = Path(__file__).resolve().parents[3] / "packages"
    src_files = list(root.glob("*/src/**/*.py"))
    append_refs: set[str] = set()
    write_refs: set[str] = set()
    for path in src_files:
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(root))
        if "append_verified_template" in text:
            append_refs.add(rel)
        if "ne_verified_templates.insert" in text:
            write_refs.add(rel)
    # The append primitive is DEFINED in the store, RE-EXPORTED by the package
    # __init__s, and CALLED only from the ne-api curation orchestration. No other
    # module references it.
    assert any("verified/store.py" in r for r in append_refs)
    allowed = (
        "verified/store.py",
        "verified_curation.py",
        "verified/__init__.py",
        "brain_ontology/__init__.py",
    )
    for ref in append_refs:
        assert any(a in ref for a in allowed), (
            f"unexpected append_verified_template reference: {ref}"
        )
    # The raw INSERT into ne_verified_templates lives ONLY in the store module.
    for ref in write_refs:
        assert "verified/store.py" in ref, (
            f"raw ne_verified_templates write outside the store: {ref}"
        )
