# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Production-root fixture for the PRE-016 integrated Agent vertical.

Only network and external-effect Protocol edges are local.  Identity uses a
real RSA/JWKS verifier, storage uses a UUID-named PostgreSQL database, secrets
use the Pilot file adapter, and both process roots use ``build_platform`` /
``create_app``.  The governed executor, Run store/workflow/finalizer, approval
gate, Skill registry/substrate, authority reader and HTTP serializers are never
replaced here.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import jwt
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.routing import Mount

from ne.api.app import create_app
from ne.api.deps import ApiState
from ne.api.platform import AppSeams, Platform, WriteActionClients, build_platform
from ne.api.settings import ApiSettings
from ne.api.skills_delivery import reconcile_required_agent_skills
from ne.audit import create_engine_with_timeouts
from ne.audit.config import AuditSettings
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.construct.meridian import meridian_m365_graph_mapping
from ne.brain_ontology import meridian_template_dir
from ne.brain_ontology.config import OntologySettings
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.operational_graph import OperationalGraphItemBinding
from ne.connect.m365.sync import run_m365_sync
from ne.connect.runner import build_ingest_stack
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Lane, Principal
from ne.flywheel.config import FlywheelSettings
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute
from ne.identity.config import IdentitySettings, OidcConfig, RoleMappingConfig
from ne.identity.jwks import StaticJwksSource
from ne.identity.verifier import OidcTokenVerifier
from ne.meridian_seed.slice import slice_corpus
from ne.permissions.config import PermissionsSettings
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.runtime.config import RuntimeSettings
from ne.secrets.config import FileStoreConfig, SecretsSettings
from ne.secrets.file_store import FileSecretStore
from ne.skills.config import SkillsSettings
from ne.skills.registry import ensure_skills_schema
from ne.worker.testing import (
    RegoFaithfulPdpFactory,
    StubChunkStore,
    StubEmbedder,
    StubSparseEncoder,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_MANIFEST = REPO_ROOT / "deploy" / "agents" / "registry.toml"
SKILL_BUNDLE = REPO_ROOT / "deploy" / "skills"
MERIDIAN_FIXTURE = REPO_ROOT / "tools" / "meridian" / "fixtures" / "meridian-v1"
SKILL_RAW_HASH = "cdcf8cc87d6042d06a56afb6ff4b5494c192387c5b9abca4d99a9fae4854221d"
ANALYST_ID = "agt_01k3meridiananalyst000000000"
#: NE-CS-SPLIT: the Skill-pinned agent — activation beats target this one now.
SUPPORT_ID = "agt_01k3customersupport000000000"
HARDSHIP_ID = "agt_01k3hardshipreview0000000000"
OWNER_ID = "user:pre016-owner"
RESTRICTED_ID = "user:pre016-restricted"
DEPARTMENT = "engineering"
M365_DRIVE_ID = "drive-meridian"
ISSUER = "https://idp.ne.test/realms/pre016"
AUDIENCE = "ne-api"
_KID = "pre016-rsa-1"


def _operational_documents() -> tuple[Any, ...]:
    return slice_corpus().operational_graph


def _operational_bindings() -> tuple[OperationalGraphItemBinding, ...]:
    return tuple(
        OperationalGraphItemBinding(
            record_id=document.record_id,
            entity_class=document.entity_class,
            item_id=f"item-{document.entity_class.lower()}-{document.record_id}",
            path=document.filename,
            acl=("group-ops", "group-approver"),
            edges=document.edges,
        )
        for document in _operational_documents()
    )


def _m365_settings() -> M365ConnectorSettings:
    return M365ConnectorSettings()


def _m365_transport() -> httpx.MockTransport:
    settings = _m365_settings()
    token_url = f"{settings.login_base_url}/{settings.tenant_id}/oauth2/v2.0/token"
    delta_url = f"{settings.graph_base_url}/drives/{M365_DRIVE_ID}/root/delta"
    documents = {
        f"item-{document.entity_class.lower()}-{document.record_id}": document
        for document in _operational_documents()
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == token_url:
            return httpx.Response(
                200,
                json={"access_token": "pre016-token", "token_type": "Bearer", "expires_in": 3600},
            )
        if url == delta_url:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": item_id,
                            "name": document.filename,
                            "webUrl": f"https://contoso.invalid/{document.filename}",
                            "lastModifiedDateTime": "2026-08-23T12:00:00Z",
                            "createdDateTime": "2026-08-23T11:00:00Z",
                            "size": len(document.content),
                            "eTag": f'"etag-{item_id}"',
                            "file": {"mimeType": document.media_type},
                            "parentReference": {
                                "path": f"/drives/{M365_DRIVE_ID}/root:/Meridian/Operational"
                            },
                        }
                        for item_id, document in documents.items()
                    ],
                    "@odata.deltaLink": f"{delta_url}?$deltatoken=complete",
                },
            )
        prefix = f"{settings.graph_base_url}/drives/{M365_DRIVE_ID}/items/"
        if url.startswith(prefix):
            item_id, suffix = url.removeprefix(prefix).split("/", 1)
            document = documents[item_id]
            if suffix == "permissions":
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": f"perm-{index}",
                                "roles": ["read"],
                                "grantedToV2": {"group": {"id": group}},
                            }
                            for index, group in enumerate(("group-ops", "group-approver"), start=1)
                        ]
                    },
                )
            if suffix == "content":
                return httpx.Response(200, content=document.content)
        return httpx.Response(404, json={"error": {"code": "fixtureMiss"}})

    return httpx.MockTransport(handler)


def run(coro: Any) -> Any:
    """Drive one coroutine without adding a second async test runtime."""
    return asyncio.run(coro)


class CapturingGateway:
    """Deterministic local model edge; all production routing remains intact."""

    def __init__(self, responses: tuple[Completion, ...] = ()) -> None:
        self._responses = list(responses)
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return Completion(
            text="PRE016 deterministic local completion",
            model_id="pre016-local-a",
            usage=Usage(prompt_tokens=7, completion_tokens=5),
        )

    def stream(self, request: CompletionRequest) -> Any:
        del request
        raise NotImplementedError("PRE-016 exercises the durable complete() boundary")


class LocalCrmClient:
    async def create_record(self, sobject: str, fields: Mapping[str, str]) -> str:
        del sobject, fields
        return "pre016-crm-record"


class LocalTicketClient:
    async def update_record(
        self,
        *,
        table: str,
        key_column: str,
        key_value: int | str,
        fields: Mapping[str, str],
    ) -> int:
        del table, key_column, key_value, fields
        return 1


class LocalDraftClient:
    async def create_draft(self, *, mailbox: str, subject: str, body: str, to: str) -> str:
        del mailbox, subject, body, to
        return "pre016-draft"


class ArtifactChunkStore(StubChunkStore):
    """Deterministic retrieval edge implementing the immutable-artifact Protocol."""

    def __init__(self) -> None:
        self._artifact_candidates: dict[str, str] = {}

    async def put_immutable_artifact_candidate(
        self, *, candidate_id: str, artifact_id: str, dense: Any
    ) -> None:
        del dense
        self._artifact_candidates[candidate_id] = artifact_id

    async def immutable_artifact_candidate_ids(self, artifact_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                candidate_id
                for candidate_id, stored_artifact_id in self._artifact_candidates.items()
                if stored_artifact_id == artifact_id
            )
        )


class LocalOidcAuthority:
    """Trusted local signer plus the production ``OidcTokenVerifier`` factory."""

    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(self._key.public_key()))
        jwk.update({"kid": _KID, "use": "sig", "alg": "RS256"})
        self._jwks = {"keys": [jwk]}
        self.config = OidcConfig(issuer=ISSUER, audience=AUDIENCE)

    def register(self, registry: PluginRegistry) -> None:
        jwks = self._jwks
        registry.register(
            "token_verifier",
            "pre016-local",
            lambda oidc: OidcTokenVerifier(oidc, key_source=StaticJwksSource(jwks)),
        )

    def mint(
        self,
        subject: str,
        *,
        groups: tuple[str, ...],
        department: str = DEPARTMENT,
    ) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": subject,
                "iat": now,
                "exp": now + 600,
                "groups": list(groups),
                "department": department,
                "typ": "Bearer",
            },
            self._key,
            algorithm="RS256",
            headers={"kid": _KID},
        )


@dataclass
class IsolatedPostgres:
    """A UUID-named database, including DBOS's fixed ``dbos`` schema."""

    admin_dsn: str
    name: str = ""
    dsn: str = ""
    _admin: sa.Engine | None = None

    def create(self) -> IsolatedPostgres:
        url = sa.make_url(self.admin_dsn)
        self.name = f"ne_pre016_{uuid.uuid4().hex}"
        self._admin = create_engine_with_timeouts(url.render_as_string(hide_password=False))
        with self._admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{self.name}"'))
        self.dsn = url.set(database=self.name).render_as_string(hide_password=False)
        database = create_engine_with_timeouts(self.dsn)
        try:
            with database.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS age"))
        finally:
            database.dispose()
        return self

    def close(self) -> None:
        if self._admin is None or not self.name:
            return
        with self._admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": self.name},
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{self.name}"'))
        self._admin.dispose()
        self._admin = None


class Pre016VerticalHarness:
    """Restartable API/worker composition over one isolated durable root."""

    def __init__(self, lane: IsolatedPostgres, tmp_path: Path, monkeypatch: Any) -> None:
        self.lane = lane
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.oidc = LocalOidcAuthority()
        self.gateway = CapturingGateway()
        self.embedder = StubEmbedder()
        self.chunk_store = ArtifactChunkStore()
        self.secret_path = tmp_path / "pre016-secrets.ne.json"
        self._platforms: list[Platform] = []
        self._prepare_environment()

    def _prepare_environment(self) -> None:
        private_key = base64.urlsafe_b64encode(b"p" * 32).rstrip(b"=").decode("ascii")
        continuation_key = base64.urlsafe_b64encode(b"c" * 32).rstrip(b"=").decode("ascii")

        async def seed() -> None:
            store = FileSecretStore(self.secret_path)
            await store.write(
                "pilot/agent-run-private-payload-kek",
                {"key_id": "pre016-private-k1", "kek": private_key},
            )
            await store.write(
                "pilot/source-authority-continuation",
                {"key_id": "pre016-continuation-k1", "hmac_key_b64": continuation_key},
            )

        run(seed())
        # The production composition consumes these resolved secret values.  A
        # second FileSecretStore instance below proves the Pilot adapter persisted
        # exactly what both roots receive.
        persisted = FileSecretStore(self.secret_path)
        private = run(persisted.read("pilot/agent-run-private-payload-kek"))
        continuation = run(persisted.read("pilot/source-authority-continuation"))
        self.monkeypatch.setenv("NE_AGENT_PRIVATE_PAYLOAD_KEY_ID", private.data["key_id"])
        self.monkeypatch.setenv("NE_AGENT_PRIVATE_PAYLOAD_KEK_B64", private.data["kek"])
        self.monkeypatch.setenv("NE_AGENT_TRANSPORT_GENERATION", "agent-transport-v1")
        self.monkeypatch.setenv(
            "NE_SOURCE_AUTHORITY_CONTINUATION_HMAC_KEY_ID",
            continuation.data["key_id"],
        )
        self.monkeypatch.setenv(
            "NE_SOURCE_AUTHORITY_CONTINUATION_HMAC_KEY_B64",
            continuation.data["hmac_key_b64"],
        )
        self.monkeypatch.setenv("NE_SKILLS_BUNDLE_DIR", str(SKILL_BUNDLE))
        self.monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["m365"]')
        self.monkeypatch.setenv(
            "NE_CONNECT_SYNC_STATE_PATH", str(self.tmp_path / "pre016-connect-state.db")
        )
        self.monkeypatch.setenv(
            "NE_CONNECT_SYNC_QUARANTINE_PATH",
            str(self.tmp_path / "pre016-connect-quarantine.db"),
        )
        for name, value in {
            "NE_M365_TENANT_ID": "tenant-meridian",
            "NE_M365_CLIENT_ID": "client-meridian",
            "NE_M365_AUTH_METHOD": "secret",
            "NE_M365_CLIENT_SECRET": "fixture-secret-not-real",
            "NE_M365_DRIVE_IDS": f'["{M365_DRIVE_ID}"]',
            "NE_M365_OWNER": "acceptance@neutron.invalid",
            "NE_M365_AUTHORITY_ENABLED": "true",
            "NE_M365_AUTHORITY_SWEEP_CADENCE_SECONDS": "240",
            "NE_M365_AUTHORITY_SWEEP_BATCH_SIZE": "100",
            "NE_M365_AUTHORITY_SWEEP_MAX_BATCH_DURATION_SECONDS": "60",
            "NE_M365_AUTHORITY_SWEEP_MAX_POINTS_PER_SCOPE": "1000",
        }.items():
            self.monkeypatch.setenv(name, value)

        engine = create_engine_with_timeouts(self.lane.dsn)
        try:
            ensure_skills_schema(engine)
            reconcile_required_agent_skills(
                engine,
                SkillsSettings(bundle_dir=str(SKILL_BUNDLE)),
                agent_manifest_path=str(AGENT_MANIFEST),
                expected_raw_hash=SKILL_RAW_HASH,
            )
        finally:
            engine.dispose()

    def settings(self, *, default_ask: bool = False) -> tuple[ApiSettings, AppSeams]:
        registry = PluginRegistry()
        self.oidc.register(registry)
        registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
        models = [
            ModelDef(
                name="pre016-local-a",
                litellm_model="openai/pre016-local-a",
                provider="local",
                lane=Lane.PRIVATE_VLLM,
                context_window=32768,
            ),
            ModelDef(
                name="pre016-local-b",
                litellm_model="openai/pre016-local-b",
                provider="local",
                lane=Lane.PRIVATE_VLLM,
                context_window=32768,
            ),
        ]
        routes = {
            "agent": TaskRoute(model="pre016-local-a"),
            "agent.meridian-analyst.v2": TaskRoute(model="pre016-local-a"),
            "agent.meridian-hardship-review.v3": TaskRoute(model="pre016-local-a"),
        }
        identity = IdentitySettings(
            verifier="pre016-local",
            oidc=self.oidc.config,
            roles=RoleMappingConfig(mapping={"ops": ("ops",)}),
            principal_claims=("email", "preferred_username", "name", "department"),
        )
        secrets = SecretsSettings(
            store="sops-file",
            file=FileStoreConfig(path=self.secret_path, mode="plain"),
        )
        ontology = OntologySettings(
            verified_answers_enabled=True,
            verified_templates_dir=meridian_template_dir(),
            include_ontology_graph=False,
            backend="oxigraph",
            oxigraph_path=None,
            quarantine_db=self.tmp_path / "pre016-quarantine.db",
            governed_local_t3_enabled=True,
        )
        graph_mapping = meridian_m365_graph_mapping(
            source_system="m365-sharepoint",
            drive_id=M365_DRIVE_ID,
            bindings=_operational_bindings(),
        )
        seams = AppSeams(
            agent_private_payload_configured=True,
            identity_settings=identity,
            runtime_settings=RuntimeSettings(
                durability="dbos",
                session_database_url=self.lane.dsn,
            ),
            audit_settings=AuditSettings(database_url=self.lane.dsn),
            permissions_settings=PermissionsSettings(database_url=self.lane.dsn),
            flywheel_settings=FlywheelSettings(signal_database_url=self.lane.dsn),
            secrets_settings=secrets,
            gateway_settings=GatewaySettings(models=models, tasks=routes),
            gateway=self.gateway,
            embedder=self.embedder,
            sparse_encoder=StubSparseEncoder(),
            chunk_store=self.chunk_store,
            plugin_registry=registry,
            ontology_settings=ontology,
            brain_graph_settings=BrainGraphSettings(
                enabled=True,
                database_url=self.lane.dsn.replace("postgresql+psycopg://", "postgresql://"),
                ledger_url=self.lane.dsn,
                sources=(graph_mapping,),
            ),
            write_action_clients=WriteActionClients(
                crm=LocalCrmClient(),
                ticket=LocalTicketClient(),
                draft=LocalDraftClient(),
            ),
        )
        api = ApiSettings(
            qdrant_url="http://qdrant.test",
            qdrant_collection="pre016",
            tei_url="http://tei.test",
            tei_rerank_url="",
            agent_manifest_path=str(AGENT_MANIFEST),
            demo_meridian_fixture_path=str(MERIDIAN_FIXTURE),
            default_ask_generation=("default-ask/canonical-run/v1" if default_ask else ""),
            dbos_system_database_url=self.lane.dsn,
            write_actions_enabled=True,
        )
        return api, seams

    def build_worker_root(self, *, default_ask: bool = False) -> Platform:
        settings, seams = self.settings(default_ask=default_ask)
        platform = build_platform(settings, seams)
        self._platforms.append(platform)
        self.seed_principals(platform)
        return platform

    def build_api_root(self, *, default_ask: bool = False) -> tuple[Any, ApiState]:
        settings, seams = self.settings(default_ask=default_ask)
        app = create_app(settings, seams=seams)
        state = api_state(app)
        self.seed_principals(state)
        return app, state

    def populate_m365(self, platform: Platform) -> None:
        """Run the real governed T1 and M365GraphSyncHost paths over local Graph I/O."""
        stack = build_ingest_stack(
            chunk_store=platform.chunk_store,
            runtime_engine=platform.runtime_engine,
            sink=platform.sink,
            settings=platform.connect_sync_settings,
            embedder=self.embedder,
            document_lineage=platform.document_lineage,
        )
        assert stack.authority is not None
        assert stack.authority_unavailable_reason is None
        settings = _m365_settings()
        transport = _m365_transport()

        async def populate() -> None:
            outcome = await run_m365_sync(
                settings,
                writer=stack.writer,
                state_store=stack.state_store,
                audit_sink=platform.sink,
                actor=OWNER_ID,
                transport=transport,
                quarantine=stack.quarantine,
                authority=stack.authority,
            )
            assert (outcome.report.records_seen, outcome.report.written) == (45, 45)
            assert platform.m365_graph_sync_host is not None
            await cast(Any, platform.m365_graph_sync_host._store).ensure_graph()
            graph = await platform.m365_graph_sync_host.run(
                settings,
                state_store=stack.state_store,
                actor=OWNER_ID,
                transport=transport,
            )
            with platform.m365_graph_sync_host._ledger._engine.connect() as conn:
                reasons = (
                    conn.execute(
                        sa.text(
                            "SELECT DISTINCT reason FROM ne_graph_sync_ledger "
                            "WHERE status = 'quarantined'"
                        )
                    )
                    .scalars()
                    .all()
                )
            assert (graph.seen, graph.upserted, graph.pending_edges) == (45, 45, 0), (
                graph,
                reasons,
            )

        try:
            run(populate())
        finally:
            stack.state_store.close()

    @staticmethod
    def seed_principals(state: Platform | ApiState) -> None:
        now = datetime.now(UTC)
        state.perm_store.upsert_principals(
            (
                SyncedPrincipal(principal_id=OWNER_ID, kind=PrincipalKind.USER),
                SyncedPrincipal(principal_id=RESTRICTED_ID, kind=PrincipalKind.USER),
            ),
            source="pre016-oidc",
            now=now,
        )
        # The shipped Skill ACL is servicing while the verified JWT department
        # remains engineering.  Only the owner receives the explicit membership.
        state.perm_store.set_member_groups(
            OWNER_ID,
            ("department:servicing",),
            source="pre016-oidc",
            now=now,
        )
        state.perm_store.set_member_groups(
            RESTRICTED_ID,
            (),
            source="pre016-oidc",
            now=now,
        )

    def owner(self) -> Principal:
        return Principal(id=OWNER_ID, groups=("ops",), claims={"department": DEPARTMENT})

    def restricted(self) -> Principal:
        return Principal(id=RESTRICTED_ID, claims={"department": DEPARTMENT})

    def bearer(self, *, restricted: bool = False) -> dict[str, str]:
        token = self.oidc.mint(
            RESTRICTED_ID if restricted else OWNER_ID,
            groups=() if restricted else ("ops",),
        )
        return {"Authorization": f"Bearer {token}"}

    def close(self) -> None:
        for platform in self._platforms:
            for engine in platform.engines:
                engine.dispose()
        self._platforms.clear()


def api_state(app: Any) -> ApiState:
    """Read the real ApiState retained behind the OIDC middleware mount."""
    for route in app.routes:
        if isinstance(route, Mount) and route.path == "/api":
            inner = cast(Any, route.app)._app
            state = inner.state.ne_api
            if isinstance(state, ApiState):
                return state
    raise AssertionError("production create_app did not mount /api with ApiState")
