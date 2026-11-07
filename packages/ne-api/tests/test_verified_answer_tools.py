# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085c: the C6 governed-path proof — verified answers run ONLY via build_platform's executor."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.settings import ApiSettings
from ne.api.tools import TURN_PRINCIPAL
from ne.audit.config import AuditSettings
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.brain_ontology import PropertyGraphLift
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import ValidatingTripleStore
from ne.brain_ontology.lift.graph_to_rdf import entity_iri
from ne.brain_ontology.quarantine import SqliteTripleQuarantine
from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import ApprovalTier, Lane, Principal
from ne.gateway.config import GatewaySettings, ModelDef
from ne.govern.errors import GuardrailViolation
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings

from .conftest import (
    ISSUER,
    FakeTokenVerifier,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
    run,
)

_DIRECTORY = "read.kg.accounts_directory"
_ENTITY_BY_IRI = "read.kg.entity_by_iri"

_TEMPLATES = {
    "accounts_directory.toml": """
template_id = "accounts-directory"
version = 1
description = "Every account visible to the caller, with its name."
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  ?account a <https://neutron.enterprise/schema/operational/Account> .
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
}
\"\"\"

[scope]
graph_classes = ["alpha", "beta"]
result_shape = "enumerable"

[answer]
columns = ["account", "name"]
citation_var = "account"
""",
    "entity_by_iri.toml": """
template_id = "entity-by-iri"
version = 1
description = "Fetch one entity's name by its exact IRI."
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
}
\"\"\"

[args.account]
type = "iri"

[scope]
graph_classes = ["alpha", "beta"]
result_shape = "enumerable"

[answer]
columns = ["account", "name"]
citation_var = "account"
""",
}


def _write_templates(directory: Path, templates: dict[str, str] = _TEMPLATES) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in templates.items():
        (directory / name).write_text(body, encoding="utf-8")
    return directory


def _account(key: str, acl: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "entity_class": "Account",
        "display_name": "Acct",
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": f"{key}-id",
        "name": f"{key} Corp",
    }


class _NullSink:
    def emit(self, event: Any) -> None:
        return None


def _seed_store(config_hash: str) -> ValidatingTripleStore:
    settings = OntologySettings(include_ontology_graph=False)
    store = ValidatingTripleStore(
        OxigraphBackend(),
        build_validator(settings),
        SqliteTripleQuarantine(),
        _NullSink(),
        settings=settings,
        config_hash=config_hash,
        compiled_shapes=load_compiled_shapes(),
    )

    async def _seed() -> None:
        mem = InMemoryGraphStore()
        await mem.upsert_nodes(
            [
                _account("acc_alpha", ["alpha"]),
                _account("acc_beta", ["beta"]),
                _account("acc_both", ["alpha", "beta"]),
            ]
        )
        await PropertyGraphLift(mem, store).run()

    run(_seed())
    return store


@dataclass
class PlatformHarness:
    platform: Platform
    audit_url: str

    def audit_rows(self) -> list[tuple[str, str, str | None]]:
        engine = sa.create_engine(self.audit_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    sa.text("SELECT action, config_hash, payload FROM ne_audit_log")
                ).fetchall()
        finally:
            engine.dispose()
        out: list[tuple[str, str, str | None]] = []
        for action, config_hash, payload in rows:
            data = payload if isinstance(payload, dict) else json.loads(payload)
            out.append((action, config_hash, data.get("detail")))
        return out


AppBuilder = Callable[..., PlatformHarness]


@pytest.fixture
def make_platform(tmp_path: Path) -> Iterator[AppBuilder]:
    engines: list[sa.Engine] = []

    def _make(
        *,
        ontology_settings: OntologySettings | None = None,
        seed_store: bool = False,
        url_key: str | None = None,
    ) -> PlatformHarness:
        reset_config_hash_for_testing()
        registry = PluginRegistry()
        verifier = FakeTokenVerifier()
        registry.register("token_verifier", "fake", lambda oidc: verifier)
        registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
        if seed_store:
            # Pre-seed a store and hand it to the 085a triple_store entry point slot.
            store = _seed_store("s" * 64)
            registry.register("triple_store", "oxigraph", lambda **_kw: store)

        # A stable url_key reuses the same DB URLs across builds, so a config_hash
        # comparison isolates the ontology difference (the DB URLs are hash inputs).
        key = url_key or uuid.uuid4().hex
        runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
        audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
        perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"

        seams = AppSeams(
            identity_settings=IdentitySettings(
                verifier="fake",
                oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
                principal_claims=("email", "department"),
            ),
            runtime_settings=RuntimeSettings(session_database_url=runtime_url),
            audit_settings=AuditSettings(database_url=audit_url),
            permissions_settings=PermissionsSettings(database_url=perms_url),
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
        platform = build_platform(
            ApiSettings(
                qdrant_url="http://qdrant.test",
                qdrant_collection="t",
                tei_url="http://tei.test",
            ),
            seams,
        )
        engines.extend(platform.engines)
        return PlatformHarness(platform=platform, audit_url=audit_url)

    yield _make
    for engine in engines:
        engine.dispose()


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


def _enabled_settings(tmp_path: Path, subdir: str = "verified") -> OntologySettings:
    directory = _write_templates(tmp_path / subdir)
    return OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
    )


# --- (a) registration + tier ------------------------------------------------


def test_verified_tools_registered_at_tier0(make_platform: AppBuilder, tmp_path: Path) -> None:
    h = make_platform(ontology_settings=_enabled_settings(tmp_path), seed_store=True)
    specs = {s.name: s for s in h.platform.executor.tool_specs}
    assert "read.knowledge.search" in specs  # the pre-085c tool is still there
    assert _DIRECTORY in specs
    assert specs[_DIRECTORY].tier is ApprovalTier.TIER_0


def test_evidence_binding_requires_contributor_receipts_from_factory() -> None:
    from ne.api.verified_tools import build_verified_answer_bindings

    captured: list[bool] = []

    def factory(**kwargs: Any) -> tuple[Any, ...]:
        captured.append(kwargs["require_contributor_receipts"])
        return ()

    registry = PluginRegistry()
    registry.register("verified_answer_tools", "default", factory)
    common = {
        "plugin_registry": registry,
        "ontology_settings": OntologySettings(),
        "store": object(),
        "expander": object(),
        "config_hash": "a" * 64,
        "audit_sink": object(),
    }

    build_verified_answer_bindings(SimpleNamespace(registry=object()), **common)  # type: ignore[arg-type]
    build_verified_answer_bindings(
        SimpleNamespace(registry=object()),  # type: ignore[arg-type]
        **common,
        evidence=object(),  # type: ignore[arg-type]
    )

    assert captured == [False, True]


# --- (b) governed execution + audit -----------------------------------------


def test_execute_verified_answer_through_governed_executor(
    make_platform: AppBuilder, tmp_path: Path
) -> None:
    h = make_platform(ontology_settings=_enabled_settings(tmp_path), seed_store=True)
    principal = Principal(id="user:a", groups=("alpha",))

    async def _run() -> str:
        token = TURN_PRINCIPAL.set(principal)
        try:
            return await h.platform.executor.execute(_DIRECTORY, {}, principal=principal)
        finally:
            TURN_PRINCIPAL.reset(token)

    result = run(_run())
    # NE-CTXB-5: the governed handler now returns the COMPACT rendering — an envelope
    # header + a col|col table — NEVER model_dump_json(). No citations array / echoed
    # args / hashes reach the model; the row VALUES the caller is cleared to see do.
    assert result.splitlines()[0].startswith("[showing ")
    assert "account | name" in result
    assert entity_iri("acc_alpha") in result
    assert '"citations"' not in result  # no duplicated citations array
    assert "config_hash" not in result  # no echoed hash key
    assert "ontology_version" not in result

    rows = h.audit_rows()
    actions = [a for a, _c, _d in rows]
    assert "pdp.decide" in actions
    assert "tool.execute" in actions
    # config_hash stamped on every event.
    assert all(c == h.platform.config_hash for _a, c, _d in rows)
    # tool.execute[success] detail carries template ids/shapes, NEVER row values;
    # result_count is now parsed from the compact envelope header.
    exec_details = [json.loads(d) for a, _c, d in rows if a == "tool.execute" and d is not None]
    detail = next(x for x in exec_details if x.get("template_id") == "accounts-directory")
    assert detail["template_version"] == 1
    assert detail["result_count"] >= 1
    assert "acc_alpha Corp" not in json.dumps(detail)  # no value leakage


# --- (c) guardrail blocks a malformed call BEFORE the handler ----------------


def test_guardrail_blocks_unknown_arg_pre_handler(
    make_platform: AppBuilder, tmp_path: Path
) -> None:
    h = make_platform(ontology_settings=_enabled_settings(tmp_path), seed_store=True)
    principal = Principal(id="user:a", groups=("alpha",))

    async def _run() -> None:
        token = TURN_PRINCIPAL.set(principal)
        try:
            await h.platform.executor.execute(_DIRECTORY, {"surprise": "x"}, principal=principal)
        finally:
            TURN_PRINCIPAL.reset(token)

    with pytest.raises(GuardrailViolation):
        run(_run())


# --- (d) C7 trim + hidden-entity non-leak through the FULL stack -------------


def test_c7_hidden_entity_not_leaked_through_full_stack(
    make_platform: AppBuilder, tmp_path: Path
) -> None:
    h = make_platform(ontology_settings=_enabled_settings(tmp_path), seed_store=True)
    alpha = Principal(id="user:a", groups=("alpha",))

    async def _directory() -> str:
        token = TURN_PRINCIPAL.set(alpha)
        try:
            return await h.platform.executor.execute(_DIRECTORY, {}, principal=alpha)
        finally:
            TURN_PRINCIPAL.reset(token)

    async def _fetch_hidden() -> str:
        token = TURN_PRINCIPAL.set(alpha)
        try:
            return await h.platform.executor.execute(
                _ENTITY_BY_IRI, {"account": entity_iri("acc_beta")}, principal=alpha
            )
        finally:
            TURN_PRINCIPAL.reset(token)

    # The compact rendering the model sees: the hidden entity's IRI is absent from
    # the alpha caller's directory, and unfetchable even by its known IRI (0 rows).
    directory = run(_directory())
    assert entity_iri("acc_beta") not in directory  # hidden entity absent
    hidden = run(_fetch_hidden())
    assert hidden.splitlines()[0] == "[showing 0 of 0 rows]"  # unfetchable
    assert entity_iri("acc_beta") not in hidden


# --- (e) default-off byte identity ------------------------------------------


def test_default_off_is_byte_identical(make_platform: AppBuilder, tmp_path: Path) -> None:
    # Same DB URLs (url_key) across all three builds so the config_hash comparison
    # isolates the ONLY difference — whether verified answers are enabled.
    off_first = make_platform(url_key="ident")
    off_menu = {s.name for s in off_first.platform.executor.tool_specs}
    off_hash = off_first.platform.config_hash
    # A verified tool is absent from the default menu.
    assert not any(n.startswith("read.kg.") for n in off_menu)

    off_second = make_platform(url_key="ident")
    assert off_second.platform.config_hash == off_hash  # default is byte-identical
    assert {s.name for s in off_second.platform.executor.tool_specs} == off_menu

    on = make_platform(
        ontology_settings=_enabled_settings(tmp_path), seed_store=True, url_key="ident"
    )
    assert on.platform.config_hash != off_hash  # enabling stamps the fingerprint
    on_menu = {s.name for s in on.platform.executor.tool_specs}
    assert on_menu - off_menu  # the enabled build adds tools


# --- (f) a template edit changes the enabled config_hash --------------------


def test_template_edit_changes_enabled_config_hash(
    make_platform: AppBuilder, tmp_path: Path
) -> None:
    first = make_platform(
        ontology_settings=_enabled_settings(tmp_path, subdir="v1"),
        seed_store=True,
        url_key="edit",
    )
    edited = dict(_TEMPLATES)
    edited["accounts_directory.toml"] = _TEMPLATES["accounts_directory.toml"].replace(
        'description = "Every account visible to the caller, with its name."',
        'description = "Every account visible to the caller (edited)."',
    )
    directory = _write_templates(tmp_path / "v2", edited)
    second = make_platform(
        ontology_settings=OntologySettings(
            verified_answers_enabled=True,
            verified_templates_dir=directory,
            include_ontology_graph=False,
            backend="oxigraph",
        ),
        seed_store=True,
        url_key="edit",
    )
    # Only the template bytes differ (same DB URLs) → the fingerprint moved the hash.
    assert first.platform.config_hash != second.platform.config_hash


# --- (g) NE-TEMPLATE-ENUM: pre-gate enum refusal vs empty result -------------

_BY_STATUS = "read.kg.accounts_by_status"

_ENUM_TEMPLATES = {
    **_TEMPLATES,
    "accounts_by_status.toml": """
template_id = "accounts-by-status"
version = 1
description = "Accounts whose name matches one closed-vocabulary value."
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  ?account a <https://neutron.enterprise/schema/operational/Account> .
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
  FILTER(?name = ?status)
}
\"\"\"

[args]
status = { type = "str", enum = ["acc_alpha Corp", "ghost"] }

[scope]
graph_classes = ["alpha", "beta"]

[answer]
columns = ["account", "name"]
citation_var = "account"
""",
}


def _enum_settings(tmp_path: Path) -> OntologySettings:
    directory = _write_templates(tmp_path / "verified-enum", _ENUM_TEMPLATES)
    return OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
    )


def test_enum_advertised_in_registered_tool_spec(make_platform: AppBuilder, tmp_path: Path) -> None:
    # The agent-visible ToolSpec input_schema carries the declared vocabulary.
    h = make_platform(ontology_settings=_enum_settings(tmp_path), seed_store=True)
    spec = next(s for s in h.platform.executor.tool_specs if s.name == _BY_STATUS)
    prop = spec.input_schema["properties"]["status"]  # type: ignore[index]
    # ToolSpec deep-freezes JSON arrays to tuples; the wire serialization is a list.
    assert list(prop["enum"]) == ["acc_alpha Corp", "ghost"]
    # CONTROL: the enum-less directory template advertises no enum anywhere.
    directory = next(s for s in h.platform.executor.tool_specs if s.name == _DIRECTORY)
    assert "enum" not in repr(directory.input_schema)


def test_out_of_vocabulary_arg_refused_pre_gate_with_audited_validate_denied(
    make_platform: AppBuilder, tmp_path: Path
) -> None:
    """SPEC §12 item 3 at the governed-executor surface: an out-of-vocabulary arg
    raises the PRE-GATE ActionValidationError (audited tool.validate[denied]) —
    never a ToolExecutionError from inside the handler, and never a zero-row
    result. The message names the field + permitted values.

    C12 is pinned by SENTINEL SWEEP, not string spot-checks (the
    ``test_run_response_and_audit_rows_carry_no_customer_content`` shape): the
    rejected value must appear nowhere in the refusal message, nowhere in the
    FORMATTED exception chain (``traceback.format_exception`` — the text any
    future ``logger.exception`` / Sentry / OTel ``record_exception`` would ship;
    a chained pydantic ``ValidationError`` renders it as ``input_value=...``),
    and in NO column of NO audit row (``SELECT *`` — no column list to go
    stale). CONTROLS prove each sweep would catch a leak: the raw pydantic
    error's formatted chain DOES carry the sentinel under the identical
    assertion shape, and the audit blob DOES carry the ``tool.validate``
    refusal row it swept.
    """
    import traceback

    from pydantic import ValidationError

    from ne.brain_ontology.verified import load_template_registry
    from ne.runtime.errors import ActionValidationError

    sentinel = "SENTINEL-c12-rejected-arg-b7e2"
    h = make_platform(ontology_settings=_enum_settings(tmp_path), seed_store=True)
    principal = Principal(id="user:a", groups=("alpha",))

    async def _run() -> str:
        token = TURN_PRINCIPAL.set(principal)
        try:
            return await h.platform.executor.execute(
                _BY_STATUS, {"status": sentinel}, principal=principal
            )
        finally:
            TURN_PRINCIPAL.reset(token)

    with pytest.raises(ActionValidationError) as excinfo:
        run(_run())
    message = str(excinfo.value)
    assert "'status'" in message
    assert "must be one of: acc_alpha Corp, ghost" in message
    assert sentinel not in message  # the rejected value is never echoed (C12)

    # The formatted CHAIN, not just the message: ``from None`` at every refusal
    # raise site severs the causes whose rendering embeds the rejected value.
    assert excinfo.value.__cause__ is None  # the ne-api seam's from None
    assert excinfo.value.__suppress_context__
    formatted = "".join(traceback.format_exception(excinfo.value))
    assert sentinel not in formatted, "exception chain leaks the rejected value"

    # CONTROL (should-fire): the raw pydantic error's own formatted chain DOES
    # carry the sentinel — the same assertion shape catches a leak where one
    # exists, so the green sweep above is evidence, not a blind spot.
    registry = load_template_registry(tmp_path / "verified-enum")
    by_status = next(t for t in registry.templates if t.template_id == "accounts-by-status")
    with pytest.raises(ValidationError) as control:
        by_status.args_model.model_validate({"status": sentinel})
    assert sentinel in "".join(traceback.format_exception(control.value))

    # Every column of every audit row, serialized and swept (SELECT * so the
    # claim needs no column enumeration that can go stale).
    engine = sa.create_engine(h.audit_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT * FROM ne_audit_log")).fetchall()
    finally:
        engine.dispose()
    blob = json.dumps([list(r) for r in rows], default=str)
    assert "tool.validate" in blob  # presence control: the refusal IS audited here
    assert sentinel not in blob, "audit rows leak the rejected value"
    actions = [a for a, _c, _d in h.audit_rows()]
    assert "tool.execute" not in actions  # the handler never ran


def test_in_vocabulary_no_match_returns_zero_rows_with_scoped_note(
    make_platform: AppBuilder, tmp_path: Path
) -> None:
    # The CONTROL at the model-visible surface: a permitted value matching nothing
    # still EXECUTES and renders "[showing 0 of 0 rows]" plus the query-scoped note
    # — visibly a result, not a refusal.
    from ne.brain_ontology.verified.answers import ZERO_ROWS_NOTE

    h = make_platform(ontology_settings=_enum_settings(tmp_path), seed_store=True)
    principal = Principal(id="user:a", groups=("alpha",))

    async def _run() -> str:
        token = TURN_PRINCIPAL.set(principal)
        try:
            return await h.platform.executor.execute(
                _BY_STATUS, {"status": "ghost"}, principal=principal
            )
        finally:
            TURN_PRINCIPAL.reset(token)

    result = run(_run())
    assert result.splitlines()[0] == "[showing 0 of 0 rows]"
    assert ZERO_ROWS_NOTE in result
    actions = [a for a, _c, _d in h.audit_rows()]
    assert "tool.execute" in actions  # the query genuinely ran


def test_verified_evidence_preserves_contributor_receipt_and_opaque_output() -> None:
    import ne.api.verified_tools as module
    from ne.api.knowledge_source_evidence import (
        ClaimedKnowledgeResultSet,
        ExistingKnowledgeRead,
    )
    from ne.api.verified_tools import VerifiedKnowledgeEvidence
    from ne.contracts.source_authority import VerifiedRowContributorReceipt
    from ne.runtime.factory import ExistingRunEvidenceContext
    from ne.runtime.run_store import EvidenceRecord, EvidenceSetRecord

    from .test_knowledge_source_evidence import _stamp

    receipt = VerifiedRowContributorReceipt(
        template_id="template-1",
        template_version=1,
        canonical_row_hash="a" * 64,
        contributor_schema_version=1,
        contributors=(_stamp(),),
    )

    class _Author:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def claim_verified_set(self, **kwargs: Any) -> ClaimedKnowledgeResultSet:
            self.calls.append(kwargs)
            record = EvidenceRecord(
                evidence_id="evidence-1",
                invocation_key="invocation-1",
                locator_status="located",
                locator_id="locator-1",
                pending_result_id="pending-1",
                producing_event_id="event-1",
            )
            return ClaimedKnowledgeResultSet(
                protected_ref="ne-protected:verified",
                evidence=EvidenceSetRecord(
                    evidence_set_id="set-1",
                    set_key="set-key",
                    pending_result_id="pending-1",
                    evidence=(record,),
                    row_event_ids=("row-event-1",),
                ),
            )

    async def _handler(_args: Mapping[str, object]) -> str:
        module._CAPTURED_VERIFIED_ANSWER.set(  # pyright: ignore[reportPrivateUsage]
            {"contributor_receipts": [receipt.model_dump(mode="json")]}
        )
        return "protected verified row"

    principal = Principal(id="principal-1", groups=("analyst",))
    existing = ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=0
        ),
        owner_principal_id=principal.id,
        attempt_id="attempt-1",
        turn_id="turn-1",
    )
    author = _Author()
    wrapped = module._verified_evidence_handler(  # pyright: ignore[reportPrivateUsage]
        _handler,
        VerifiedKnowledgeEvidence(
            author=author,  # type: ignore[arg-type]
            context_provider=lambda: existing,
        ),
    )
    token = TURN_PRINCIPAL.set(principal)
    try:
        output = run(wrapped({}))
    finally:
        TURN_PRINCIPAL.reset(token)

    assert output == "ne-protected:verified"
    [claim] = author.calls
    assert claim["protected_result"] == b"protected verified row"
    assert claim["receipts"] == (receipt,)


def test_verified_evidence_preserves_authoritative_empty_result() -> None:
    import ne.api.verified_tools as module
    from ne.api.knowledge_source_evidence import ClaimedKnowledgeResult, ExistingKnowledgeRead
    from ne.api.verified_tools import VerifiedKnowledgeEvidence
    from ne.contracts.source_authority import VerifiedEmptyResultReceipt
    from ne.runtime.factory import ExistingRunEvidenceContext
    from ne.runtime.run_store import EvidenceRecord

    from .test_knowledge_source_evidence import _stamp

    receipt = VerifiedEmptyResultReceipt(
        template_id="template-1",
        template_version=1,
        canonical_args_hash="a" * 64,
        dataset_graphs_hash="b" * 64,
        contributor_schema_version=1,
        contributors=(_stamp(),),
    )

    class _Author:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def claim_verified_empty(self, **kwargs: Any) -> ClaimedKnowledgeResult:
            self.calls.append(kwargs)
            return ClaimedKnowledgeResult(
                protected_ref="ne-protected:verified-empty",
                evidence=EvidenceRecord(
                    evidence_id="evidence-1",
                    invocation_key="invocation-1",
                    locator_status="located",
                    locator_id="locator-1",
                    pending_result_id="pending-1",
                    producing_event_id="event-1",
                ),
            )

    async def _handler(_args: Mapping[str, object]) -> str:
        module._CAPTURED_VERIFIED_ANSWER.set(  # pyright: ignore[reportPrivateUsage]
            {"contributor_receipts": [], "empty_result_receipt": receipt.model_dump(mode="json")}
        )
        return "[showing 0 of 0 rows]"

    principal = Principal(id="principal-1", groups=("analyst",))
    existing = ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=0
        ),
        owner_principal_id=principal.id,
        attempt_id="attempt-1",
        turn_id="turn-1",
    )
    author = _Author()
    wrapped = module._verified_evidence_handler(  # pyright: ignore[reportPrivateUsage]
        _handler,
        VerifiedKnowledgeEvidence(
            author=author,  # type: ignore[arg-type]
            context_provider=lambda: existing,
        ),
    )
    token = TURN_PRINCIPAL.set(principal)
    try:
        output = run(wrapped({}))
    finally:
        TURN_PRINCIPAL.reset(token)

    assert output == "ne-protected:verified-empty"
    [claim] = author.calls
    assert claim["protected_result"] == b"[showing 0 of 0 rows]"
    assert claim["receipt"] == receipt


def test_claim_verifier_reparses_the_exact_rendering_a_verified_read_serves() -> None:
    """NE-CLAIM-VERIFIED-SET: the two ends of the verified lane must agree.

    ``render_answer_text`` is the ONLY content-bearing artifact a verified (T3) read
    persists — the structured answer is not stored — so the claim verifier rebuilds
    its grounding corpus by parsing that rendering back and re-hashing each row with
    ``ne.brain_ontology.verified.answers``'s own ``canonical_row_hash`` formula.

    That parity is what makes the parse self-verifying instead of a guess, and it is
    a CROSS-PACKAGE contract: a change to either the rendering or the row hash
    breaks claim binding for every verified read, which is precisely how the lane
    started publishing ``Recorded failure class: LookupError``. Pinned here.
    """

    import hashlib
    import json
    from datetime import UTC, datetime

    from ne.brain_ontology.verified.answers import VerifiedAnswer, render_answer_text
    from ne.runtime.run_claim_verifier import _VERIFIED_HEADER

    columns = ("customer", "provisional_credit_amount", "status")
    rows = (
        ("Meridian Bank", "412.55", "posted"),
        ("Northwind Trust", "88.20", "pending"),
        # A cell carrying the column separator: the row hash — not the split — is
        # what decides, so an ambiguous parse fails closed instead of binding.
        ("Third | Party", "5.00", "posted"),
    )

    def answer(**changes: object) -> VerifiedAnswer:
        fields: dict[str, object] = {
            "template_id": "reg_e_provisional_credits",
            "template_version": 1,
            "args": {},
            "columns": columns,
            "rows": rows,
            "citations": ("urn:x:1", "urn:x:2", "urn:x:3"),
            "contributor_receipts": (),
            "empty_result_receipt": None,
            "truncated": False,
            "row_limit": 50,
            "returned_rows": len(rows),
            "total_rows": len(rows),
            "truncation_reason": None,
            "config_hash": "a" * 64,
            "ontology_version": "v1",
            "executed_at": datetime.now(UTC),
        }
        fields.update(changes)
        return VerifiedAnswer.model_validate(fields)

    lines = render_answer_text(answer()).split("\n")
    match = _VERIFIED_HEADER.fullmatch(lines[0])
    assert match is not None
    assert int(match.group(1)) == len(rows)
    parsed_columns = lines[1].split(" | ")
    assert parsed_columns == list(columns)
    assert lines[2] == "-+-".join("-" * len(column) for column in parsed_columns)
    parsed_rows = [line.split(" | ") for line in lines[3:]]
    assert len(parsed_rows) == len(rows)

    for parsed, original in zip(parsed_rows, rows, strict=True):
        reparsed_hash = hashlib.sha256(
            json.dumps([parsed_columns, parsed], separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        minted_hash = hashlib.sha256(
            json.dumps([list(columns), list(original)], separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if "|" in "".join(original):
            # The separator-bearing row does NOT round-trip, and that is the point:
            # it is refused, never silently bound to a mis-split row.
            assert reparsed_hash != minted_hash
        else:
            assert reparsed_hash == minted_hash

    # Both other header shapes the renderer can emit still parse.
    for changes in (
        {"truncated": True, "truncation_reason": "rows", "total_rows": 99},
        {"total_rows": None},
    ):
        header = render_answer_text(answer(**changes)).split("\n")[0]
        assert _VERIFIED_HEADER.fullmatch(header) is not None


def test_verified_evidence_handler_allows_runless_console_invoke() -> None:
    """The console ``/v1/verified/invoke`` path is run-less BY DESIGN.

    Before CONSOLE_VERIFIED_READ existed, ``_verified_evidence_handler`` raised
    ``LookupError("existing canonical Run evidence context is unavailable")`` for
    EVERY console invoke on an evidence-wired deployment — the page's Run button
    could never succeed (surfaced live as a 502 with detail ``LookupError``). With
    the route's marker set and no Run context, the wrapper executes the governed
    handler verbatim and claims NOTHING (there is no Run evidence trail to claim
    into); the structured answer still reaches the route's recorder through the
    record_answer_provider seam.
    """
    import ne.api.verified_tools as module
    from ne.api.verified_tools import CONSOLE_VERIFIED_READ, VerifiedKnowledgeEvidence

    class _Author:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def claim_verified_set(self, **kwargs: Any) -> Any:
            self.calls.append("set")
            raise AssertionError("a run-less console read must claim no Run evidence")

        async def claim_verified_empty(self, **kwargs: Any) -> Any:
            self.calls.append("empty")
            raise AssertionError("a run-less console read must claim no Run evidence")

    async def _handler(_args: Mapping[str, object]) -> str:
        return "protected verified row"

    author = _Author()
    wrapped = module._verified_evidence_handler(  # pyright: ignore[reportPrivateUsage]
        _handler,
        VerifiedKnowledgeEvidence(
            author=author,  # type: ignore[arg-type]
            context_provider=lambda: None,
        ),
    )
    principal = Principal(id="principal-1", groups=("analyst",))
    principal_token = TURN_PRINCIPAL.set(principal)
    console_token = CONSOLE_VERIFIED_READ.set(True)
    try:
        output = run(wrapped({}))
    finally:
        CONSOLE_VERIFIED_READ.reset(console_token)
        TURN_PRINCIPAL.reset(principal_token)
    assert output == "protected verified row"
    assert author.calls == []


def test_verified_evidence_handler_still_fails_closed_off_the_console_route() -> None:
    """The agent-lane guarantee is untouched by the console marker.

    (a) No Run context AND no console marker — the pre-existing hard failure stands:
    an agent-lane read whose evidence context vanished must refuse, never silently
    skip its claim. (b) A PRESENT Run context owned by another principal refuses
    even with the marker set — the marker only excuses the genuinely run-less case.
    """
    import ne.api.verified_tools as module
    from ne.api.knowledge_source_evidence import ExistingKnowledgeRead
    from ne.api.verified_tools import CONSOLE_VERIFIED_READ, VerifiedKnowledgeEvidence
    from ne.runtime.factory import ExistingRunEvidenceContext

    async def _handler(_args: Mapping[str, object]) -> str:
        raise AssertionError("the governed handler must not run on a refused read")

    principal = Principal(id="principal-1", groups=("analyst",))

    wrapped_no_context = module._verified_evidence_handler(  # pyright: ignore[reportPrivateUsage]
        _handler,
        VerifiedKnowledgeEvidence(author=object(), context_provider=lambda: None),  # type: ignore[arg-type]
    )
    principal_token = TURN_PRINCIPAL.set(principal)
    try:
        with pytest.raises(LookupError):
            run(wrapped_no_context({}))
    finally:
        TURN_PRINCIPAL.reset(principal_token)

    other_owner = ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=0
        ),
        owner_principal_id="someone-else",
        attempt_id="attempt-1",
        turn_id="turn-1",
    )
    wrapped_mismatch = module._verified_evidence_handler(  # pyright: ignore[reportPrivateUsage]
        _handler,
        VerifiedKnowledgeEvidence(author=object(), context_provider=lambda: other_owner),  # type: ignore[arg-type]
    )
    principal_token = TURN_PRINCIPAL.set(principal)
    console_token = CONSOLE_VERIFIED_READ.set(True)
    try:
        with pytest.raises(LookupError):
            run(wrapped_mismatch({}))
    finally:
        CONSOLE_VERIFIED_READ.reset(console_token)
        TURN_PRINCIPAL.reset(principal_token)
