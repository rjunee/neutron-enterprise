# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Durable, append-only Agent definitions and deny-default catalog projection.

Only immutable declarative definitions are stored here.  The mutable active row
records whether the latest definition is present in the current boot manifest;
removal never deletes history.  This module has deliberately no launch method,
runtime AgentDefinition adapter, executor, gateway, or workflow runner.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
from dataclasses import dataclass
from typing import Any, Final

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from ne.api.agent_manifest import (
    AgentManifestDefinition,
    AvailabilityDependency,
    LoadedAgentManifest,
)
from ne.contracts.types import Principal

_LOCK_KEY: Final[int] = 0x4E454147454E5438  # "NEAGENT8", stable signed-int64-safe key.
_SQLITE_CATALOG_LOCK = threading.Lock()
_UNCONFIGURED_RAW_HASH: Final[str] = (
    "de1c24cc7125bdbbce80003320c18175ac839bf08517302c094eb946833a56db"
)
_UNCONFIGURED_SEMANTIC_HASH: Final[str] = (
    "20e8d0efb05832941c56304c24c1036ca3907fa6db6ee67f4234b9eb9987f95c"
)


def _lower_hex_shape_sql(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND {remainder} = ''"


_metadata = sa.MetaData()

ne_agent_registry_head = sa.Table(
    "ne_agent_registry_head",
    _metadata,
    sa.Column("singleton_key", sa.Integer, primary_key=True),
    sa.Column("configured", sa.Boolean, nullable=False),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("manifest_raw_hash", sa.String(64), nullable=False),
    sa.Column("manifest_semantic_hash", sa.String(64), nullable=False),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.CheckConstraint("singleton_key = 1", name="ck_agent_registry_head_singleton"),
    sa.CheckConstraint("generation >= 0", name="ck_agent_registry_head_generation"),
    sa.CheckConstraint(
        _lower_hex_shape_sql("manifest_raw_hash"), name="ck_agent_registry_head_raw_hash_shape"
    ),
    sa.CheckConstraint(
        _lower_hex_shape_sql("manifest_semantic_hash"),
        name="ck_agent_registry_head_semantic_hash_shape",
    ),
)

ne_agent_definition_versions = sa.Table(
    "ne_agent_definition_versions",
    _metadata,
    sa.Column("agent_id", sa.String(64), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("definition_hash", sa.String(64), nullable=False),
    sa.Column("definition_json", sa.Text, nullable=False),
    sa.Column("first_manifest_semantic_hash", sa.String(64), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.CheckConstraint("version > 0", name="ck_agent_definition_version_positive"),
    sa.CheckConstraint(
        _lower_hex_shape_sql("definition_hash"), name="ck_agent_definition_hash_shape"
    ),
    sa.CheckConstraint(
        _lower_hex_shape_sql("first_manifest_semantic_hash"),
        name="ck_agent_definition_manifest_hash_shape",
    ),
    sa.CheckConstraint(
        "length(trim(agent_id)) > 0",
        name="ck_agent_definition_id_nonempty",
    ),
    sa.CheckConstraint(
        "length(trim(definition_json)) > 0",
        name="ck_agent_definition_json_nonempty",
    ),
    sa.UniqueConstraint(
        "agent_id",
        "version",
        "definition_hash",
        name="uq_agent_definition_hash_identity",
    ),
)

ne_agent_active_definitions = sa.Table(
    "ne_agent_active_definitions",
    _metadata,
    sa.Column("agent_id", sa.String(64), primary_key=True),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("definition_hash", sa.String(64), nullable=False),
    sa.Column("present", sa.Boolean, nullable=False),
    sa.Column("manifest_raw_hash", sa.String(64), nullable=False),
    sa.Column("manifest_semantic_hash", sa.String(64), nullable=False),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.ForeignKeyConstraint(
        ["agent_id", "version", "definition_hash"],
        [
            "ne_agent_definition_versions.agent_id",
            "ne_agent_definition_versions.version",
            "ne_agent_definition_versions.definition_hash",
        ],
        name="fk_agent_active_definition",
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
    sa.CheckConstraint("version > 0", name="ck_agent_active_version_positive"),
    sa.CheckConstraint(
        _lower_hex_shape_sql("definition_hash"), name="ck_agent_active_definition_hash_shape"
    ),
    sa.CheckConstraint(
        _lower_hex_shape_sql("manifest_raw_hash"), name="ck_agent_active_raw_hash_shape"
    ),
    sa.CheckConstraint(
        _lower_hex_shape_sql("manifest_semantic_hash"),
        name="ck_agent_active_semantic_hash_shape",
    ),
    sa.CheckConstraint(
        "length(trim(agent_id)) > 0",
        name="ck_agent_active_id_nonempty",
    ),
)


class AgentCatalogError(RuntimeError):
    """Boot registry history or schema violates immutable catalog rules."""


@dataclass(frozen=True)
class AgentDependencySnapshot:
    """Exact boot-composed dependency identities; absence means unavailable."""

    tools: frozenset[str]
    knowledge_sources: frozenset[tuple[str, str]]
    skills: frozenset[tuple[str, int, str]]
    workflow_profiles: frozenset[tuple[str, int, str]]
    model_policies: frozenset[str]


@dataclass(frozen=True)
class CatalogAgent:
    definition: AgentManifestDefinition
    available: bool
    availability_reasons: tuple[str, ...]
    present: bool


def _definition_json(definition: AgentManifestDefinition) -> str:
    return json.dumps(
        definition.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _canonical_expression(value: object) -> str:
    """Normalize reflected SQL outside literals while preserving literal bytes."""
    source = str(value)
    pieces: list[str] = []
    unquoted: list[str] = []

    def flush() -> None:
        if unquoted:
            segment = "".join(unquoted).lower().replace('"', "")
            segment = re.sub(
                r"\(([a-z_][a-z0-9_]*)\)\s*::(?:character\s+varying|varchar|text)",
                r"\1",
                segment,
            )
            segment = re.sub(r"::(?:character\s+varying|varchar|text)", "", segment)
            segment = re.sub(r"\s+", "", segment)
            segment = segment.replace("btrim(", "trim(").replace("trim(bothfrom", "trim(")
            pieces.append(segment)
            unquoted.clear()

    index = 0
    while index < len(source):
        if source[index] != "'":
            unquoted.append(source[index])
            index += 1
            continue
        flush()
        literal = ["'"]
        index += 1
        while index < len(source):
            literal.append(source[index])
            if source[index] == "'":
                index += 1
                if index < len(source) and source[index] == "'":
                    literal.append("'")
                    index += 1
                    continue
                break
            index += 1
        pieces.append("".join(literal))
    flush()
    normalized = "".join(pieces)
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1]
    return normalized


def _assert_schema_shape(conn: sa.Connection) -> None:
    inspector = sa.inspect(conn)
    expected_columns = {
        "ne_agent_registry_head": {
            "singleton_key",
            "configured",
            "generation",
            "manifest_raw_hash",
            "manifest_semantic_hash",
            "updated_at",
        },
        "ne_agent_definition_versions": {
            "agent_id",
            "version",
            "definition_hash",
            "definition_json",
            "first_manifest_semantic_hash",
            "created_at",
        },
        "ne_agent_active_definitions": {
            "agent_id",
            "version",
            "definition_hash",
            "present",
            "manifest_raw_hash",
            "manifest_semantic_hash",
            "updated_at",
        },
    }
    expected_pk = {
        "ne_agent_registry_head": ["singleton_key"],
        "ne_agent_definition_versions": ["agent_id", "version"],
        "ne_agent_active_definitions": ["agent_id"],
    }
    expected_checks = {
        "ne_agent_registry_head": {
            "ck_agent_registry_head_singleton",
            "ck_agent_registry_head_generation",
            "ck_agent_registry_head_raw_hash_shape",
            "ck_agent_registry_head_semantic_hash_shape",
        },
        "ne_agent_definition_versions": {
            "ck_agent_definition_version_positive",
            "ck_agent_definition_hash_shape",
            "ck_agent_definition_manifest_hash_shape",
            "ck_agent_definition_id_nonempty",
            "ck_agent_definition_json_nonempty",
        },
        "ne_agent_active_definitions": {
            "ck_agent_active_version_positive",
            "ck_agent_active_definition_hash_shape",
            "ck_agent_active_raw_hash_shape",
            "ck_agent_active_semantic_hash_shape",
            "ck_agent_active_id_nonempty",
        },
    }
    expected_check_expressions = {
        "ne_agent_registry_head": {
            "ck_agent_registry_head_singleton": _canonical_expression("singleton_key = 1"),
            "ck_agent_registry_head_generation": _canonical_expression("generation >= 0"),
            "ck_agent_registry_head_raw_hash_shape": _canonical_expression(
                _lower_hex_shape_sql("manifest_raw_hash")
            ),
            "ck_agent_registry_head_semantic_hash_shape": _canonical_expression(
                _lower_hex_shape_sql("manifest_semantic_hash")
            ),
        },
        "ne_agent_definition_versions": {
            "ck_agent_definition_version_positive": _canonical_expression("version > 0"),
            "ck_agent_definition_hash_shape": _canonical_expression(
                _lower_hex_shape_sql("definition_hash")
            ),
            "ck_agent_definition_manifest_hash_shape": _canonical_expression(
                _lower_hex_shape_sql("first_manifest_semantic_hash")
            ),
            "ck_agent_definition_id_nonempty": _canonical_expression("length(trim(agent_id)) > 0"),
            "ck_agent_definition_json_nonempty": _canonical_expression(
                "length(trim(definition_json)) > 0"
            ),
        },
        "ne_agent_active_definitions": {
            "ck_agent_active_version_positive": _canonical_expression("version > 0"),
            "ck_agent_active_definition_hash_shape": _canonical_expression(
                _lower_hex_shape_sql("definition_hash")
            ),
            "ck_agent_active_raw_hash_shape": _canonical_expression(
                _lower_hex_shape_sql("manifest_raw_hash")
            ),
            "ck_agent_active_semantic_hash_shape": _canonical_expression(
                _lower_hex_shape_sql("manifest_semantic_hash")
            ),
            "ck_agent_active_id_nonempty": _canonical_expression("length(trim(agent_id)) > 0"),
        },
    }
    expected_shapes: dict[
        str, dict[str, tuple[bool, tuple[type[sa.types.TypeEngine[Any]], ...]]]
    ] = {
        "ne_agent_registry_head": {
            "singleton_key": (False, (sa.Integer,)),
            "configured": (False, (sa.Boolean,)),
            "generation": (False, (sa.Integer,)),
            "manifest_raw_hash": (False, (sa.String,)),
            "manifest_semantic_hash": (False, (sa.String,)),
            "updated_at": (False, (sa.DateTime,)),
        },
        "ne_agent_definition_versions": {
            "agent_id": (False, (sa.String,)),
            "version": (False, (sa.Integer,)),
            "definition_hash": (False, (sa.String,)),
            "definition_json": (False, (sa.Text,)),
            "first_manifest_semantic_hash": (False, (sa.String,)),
            "created_at": (False, (sa.DateTime,)),
        },
        "ne_agent_active_definitions": {
            "agent_id": (False, (sa.String,)),
            "version": (False, (sa.Integer,)),
            "definition_hash": (False, (sa.String,)),
            "present": (False, (sa.Boolean,)),
            "manifest_raw_hash": (False, (sa.String,)),
            "manifest_semantic_hash": (False, (sa.String,)),
            "updated_at": (False, (sa.DateTime,)),
        },
    }
    for table, columns in expected_columns.items():
        actual = {str(column["name"]): column for column in inspector.get_columns(table)}
        if set(actual) != columns:
            raise AgentCatalogError(f"Agent catalog schema has incompatible {table} columns")
        for name, (nullable, types) in expected_shapes[table].items():
            if bool(actual[name]["nullable"]) is not nullable or not isinstance(
                actual[name]["type"], types
            ):
                raise AgentCatalogError(
                    f"Agent catalog schema has incompatible {table}.{name} shape"
                )
        pk = list(inspector.get_pk_constraint(table).get("constrained_columns") or ())
        if pk != expected_pk[table]:
            raise AgentCatalogError(f"Agent catalog schema has incompatible {table} primary key")
        checks = {
            str(check.get("name")): _canonical_expression(check.get("sqltext"))
            for check in inspector.get_check_constraints(table)
        }
        expected_expressions = expected_check_expressions[table]
        if set(checks) != expected_checks[table] or checks != expected_expressions:
            raise AgentCatalogError(f"Agent catalog schema has incompatible {table} checks")

    uniques = {
        str(item.get("name")): tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints("ne_agent_definition_versions")
    }
    if uniques != {"uq_agent_definition_hash_identity": ("agent_id", "version", "definition_hash")}:
        raise AgentCatalogError("Agent catalog definition uniques are incompatible")

    fks = inspector.get_foreign_keys("ne_agent_active_definitions")
    if len(fks) != 1:
        raise AgentCatalogError("Agent catalog active definition foreign key is incompatible")
    fk = fks[0]
    options = {str(k).lower(): str(v).upper() for k, v in (fk.get("options") or {}).items()}
    if (
        fk.get("name") != "fk_agent_active_definition"
        or list(fk.get("constrained_columns") or ()) != ["agent_id", "version", "definition_hash"]
        or fk.get("referred_schema") not in (None, "", inspector.default_schema_name)
        or fk.get("referred_table") != "ne_agent_definition_versions"
        or list(fk.get("referred_columns") or ()) != ["agent_id", "version", "definition_hash"]
        or options.get("ondelete", "RESTRICT") != "RESTRICT"
        or options.get("onupdate", "RESTRICT") != "RESTRICT"
    ):
        raise AgentCatalogError("Agent catalog active definition foreign key is incompatible")


def _install_append_only_guards(conn: sa.Connection) -> None:
    if conn.dialect.name == "sqlite":
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_agent_definitions_no_update")
        conn.exec_driver_sql(
            """
            CREATE TRIGGER trg_agent_definitions_no_update
            BEFORE UPDATE ON ne_agent_definition_versions
            BEGIN SELECT RAISE(ABORT, 'Agent definition history is append-only'); END
            """
        )
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_agent_definitions_no_delete")
        conn.exec_driver_sql(
            """
            CREATE TRIGGER trg_agent_definitions_no_delete
            BEFORE DELETE ON ne_agent_definition_versions
            BEGIN SELECT RAISE(ABORT, 'Agent definition history is append-only'); END
            """
        )
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_agent_definitions_json_valid")
        conn.exec_driver_sql(
            """
            CREATE TRIGGER trg_agent_definitions_json_valid
            BEFORE INSERT ON ne_agent_definition_versions
            WHEN json_valid(NEW.definition_json) = 0
              OR json_type(NEW.definition_json) != 'object'
            BEGIN SELECT RAISE(ABORT, 'Agent definition JSON is invalid'); END
            """
        )
    elif conn.dialect.name == "postgresql":
        conn.exec_driver_sql(
            """
            CREATE OR REPLACE FUNCTION ne_agent_definitions_append_only()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Agent definition history is append-only'; END $$
            """
        )
        for operation in ("UPDATE", "DELETE"):
            name = f"trg_agent_definitions_no_{operation.lower()}"
            conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name} ON ne_agent_definition_versions")
            conn.exec_driver_sql(
                f"CREATE TRIGGER {name} BEFORE {operation} ON ne_agent_definition_versions "
                "FOR EACH ROW EXECUTE FUNCTION ne_agent_definitions_append_only()"
            )
        conn.exec_driver_sql(
            """
            CREATE OR REPLACE FUNCTION ne_agent_definition_json_valid()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF jsonb_typeof(NEW.definition_json::jsonb) != 'object' THEN
                RAISE EXCEPTION 'Agent definition JSON is invalid';
              END IF;
              RETURN NEW;
            END $$
            """
        )
        conn.exec_driver_sql(
            "DROP TRIGGER IF EXISTS trg_agent_definitions_json_valid "
            "ON ne_agent_definition_versions"
        )
        conn.exec_driver_sql(
            "CREATE TRIGGER trg_agent_definitions_json_valid BEFORE INSERT "
            "ON ne_agent_definition_versions FOR EACH ROW "
            "EXECUTE FUNCTION ne_agent_definition_json_valid()"
        )


def ensure_agent_catalog_schema(engine: sa.Engine) -> None:
    lock = _SQLITE_CATALOG_LOCK if engine.dialect.name == "sqlite" else threading.Lock()
    with lock, engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        else:
            _acquire_lock(conn)
        _metadata.create_all(conn)
        _assert_schema_shape(conn)
        _install_append_only_guards(conn)


def _acquire_lock(conn: sa.Connection) -> None:
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})


def _dependency_reasons(
    definition: AgentManifestDefinition,
    present: bool,
    dependencies: AgentDependencySnapshot,
) -> tuple[str, ...]:
    if not present:
        return ("definition-removed",)
    reasons: set[str] = set()
    if definition.lifecycle == "retired":
        reasons.add("lifecycle-retired")
    if definition.model_policy_id not in dependencies.model_policies:
        reasons.add("model-policy-unavailable")
    required_tools = set(definition.tool_grants) - set(definition.optional_tool_grants)
    if not required_tools <= dependencies.tools:
        reasons.add("tool-unavailable")
    optional_knowledge = {
        (grant.source_id, tier)
        for grant in definition.optional_knowledge_sources
        for tier in grant.tiers
    }
    if any(
        (grant.source_id, tier) not in dependencies.knowledge_sources
        for grant in definition.knowledge_sources
        for tier in grant.tiers
        if (grant.source_id, tier) not in optional_knowledge
    ):
        reasons.add("knowledge-unavailable")
    if any(
        (skill.skill_id, skill.version, skill.content_hash) not in dependencies.skills
        for skill in definition.skills
    ):
        reasons.add("skill-unavailable")
    if (
        definition.workflow is not None
        and (
            definition.workflow.profile_id,
            definition.workflow.version,
            definition.workflow.config_hash,
        )
        not in dependencies.workflow_profiles
    ):
        reasons.add("workflow-profile-unavailable")
    for dependency in definition.availability_dependencies:
        reason = _explicit_dependency_reason(dependency, dependencies)
        if reason is not None:
            reasons.add(reason)
    return tuple(sorted(reasons))


def effective_agent_tool_grants(
    definition: AgentManifestDefinition,
    dependencies: AgentDependencySnapshot,
) -> tuple[str, ...]:
    """Return the exact boot-admitted grants for one immutable Agent.

    THE COARSENESS THIS ADMITS, RECORDED WHERE THE MECHANISM IS.  The optional
    families ride ONE conjunction: ``optional_tool_grants`` are admitted only
    when EVERY entry of ``optional_knowledge_sources`` is ready, and they are
    dropped SILENTLY when it is not.  Availability is a separate question --
    only ``availability_dependencies`` can make an Agent unavailable -- so an
    Agent whose optional half is missing stays AVAILABLE with a quietly smaller
    grant set.

    The concrete case on this tree is the Meridian Customer Support Agent: a
    deployment with the Salesforce legs staged but no Tier-3 release admits
    NEITHER family, so ``read.external.crm.contact`` is absent and the run
    correctly refuses to draft for want of a governed recipient -- an honest
    refusal, but reached from a catalog row that said "available".  Accepted as
    scoped for a single-deployment demo box where both tiers stage together, and
    stated again beside the grants in ``deploy/agents/registry.toml``; a
    per-family gate is the fix if a deployment ever stages them apart.
    """
    required = set(definition.tool_grants) - set(definition.optional_tool_grants)
    optional_knowledge = {
        (grant.source_id, tier)
        for grant in definition.optional_knowledge_sources
        for tier in grant.tiers
    }
    optional_ready = optional_knowledge <= dependencies.knowledge_sources
    admitted_optional = (
        set(definition.optional_tool_grants) & dependencies.tools if optional_ready else set()
    )
    return tuple(sorted(required | admitted_optional))


def _explicit_dependency_reason(
    dependency: AvailabilityDependency,
    dependencies: AgentDependencySnapshot,
) -> str | None:
    if dependency.kind == "tool":
        return None if dependency.dependency_id in dependencies.tools else "tool-unavailable"
    if dependency.kind == "knowledge":
        return (
            None
            if any(
                source_id == dependency.dependency_id
                for source_id, _tier in dependencies.knowledge_sources
            )
            else "knowledge-unavailable"
        )
    if dependency.kind == "model-policy":
        return (
            None
            if dependency.dependency_id in dependencies.model_policies
            else "model-policy-unavailable"
        )
    if dependency.kind == "skill":
        key = (dependency.dependency_id, dependency.version, dependency.content_hash)
        return None if key in dependencies.skills else "skill-unavailable"
    key = (dependency.dependency_id, dependency.version, dependency.content_hash)
    return None if key in dependencies.workflow_profiles else "workflow-profile-unavailable"


def _visible(definition: AgentManifestDefinition, principal: Principal) -> bool:
    visibility = definition.visibility
    if visibility.all_authenticated:
        return True
    if principal.id in visibility.principals:
        return True
    groups = set(principal.groups)
    if groups.intersection(visibility.groups):
        return True
    if groups.intersection({f"role:{role}" for role in visibility.roles}):
        return True
    department = principal.claims.get("department")
    return isinstance(department, str) and department in visibility.departments


class AgentCatalog:
    """Read-only boot snapshot with visibility applied before every lookup/page."""

    def __init__(self, agents: tuple[CatalogAgent, ...]) -> None:
        self._agents = tuple(sorted(agents, key=lambda row: row.definition.agent_id))
        self._by_id = {row.definition.agent_id: row for row in self._agents}

    @classmethod
    def empty(cls) -> AgentCatalog:
        return cls(())

    @property
    def agents(self) -> tuple[CatalogAgent, ...]:
        return self._agents

    def get_visible(self, agent_id: str, principal: Principal) -> CatalogAgent | None:
        row = self._by_id.get(agent_id)
        if row is None or not _visible(row.definition, principal):
            return None
        return row

    def list_visible(
        self, principal: Principal, *, after: str | None, limit: int
    ) -> tuple[tuple[CatalogAgent, ...], str | None]:
        visible = [row for row in self._agents if _visible(row.definition, principal)]
        if after is not None:
            positions = [
                index for index, row in enumerate(visible) if row.definition.agent_id == after
            ]
            if not positions:
                return (), None
            visible = visible[positions[0] + 1 :]
        page = tuple(visible[:limit])
        next_after = page[-1].definition.agent_id if len(visible) > limit and page else None
        return page, next_after


def encode_agent_cursor(agent_id: str) -> str:
    payload = json.dumps({"after": agent_id}, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_agent_cursor(cursor: str) -> str | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"after"}:
        return None
    after = payload.get("after")
    return after if isinstance(after, str) else None


def _read_catalog(conn: sa.Connection, dependencies: AgentDependencySnapshot) -> AgentCatalog:
    rows = conn.execute(
        sa.select(
            ne_agent_active_definitions.c.agent_id,
            ne_agent_active_definitions.c.version,
            ne_agent_active_definitions.c.definition_hash,
            ne_agent_active_definitions.c.present,
            ne_agent_definition_versions.c.agent_id.label("history_agent_id"),
            ne_agent_definition_versions.c.version.label("history_version"),
            ne_agent_definition_versions.c.definition_hash.label("history_definition_hash"),
            ne_agent_definition_versions.c.definition_json,
        ).select_from(
            ne_agent_active_definitions.outerjoin(
                ne_agent_definition_versions,
                sa.and_(
                    ne_agent_active_definitions.c.agent_id
                    == ne_agent_definition_versions.c.agent_id,
                    ne_agent_active_definitions.c.version == ne_agent_definition_versions.c.version,
                    ne_agent_active_definitions.c.definition_hash
                    == ne_agent_definition_versions.c.definition_hash,
                ),
            )
        )
    ).all()
    agents: list[CatalogAgent] = []
    for row in rows:
        if row.definition_json is None:
            raise AgentCatalogError("Agent catalog active definition has no exact history row")
        try:
            definition = AgentManifestDefinition.model_validate_json(row.definition_json)
        except ValidationError as exc:
            raise AgentCatalogError("Agent catalog stored definition is invalid") from exc
        identity = (definition.agent_id, definition.version, definition.definition_hash)
        active_identity = (str(row.agent_id), int(row.version), str(row.definition_hash))
        history_identity = (
            str(row.history_agent_id),
            int(row.history_version),
            str(row.history_definition_hash),
        )
        if identity != active_identity or identity != history_identity:
            raise AgentCatalogError("Agent catalog stored definition identity mismatch")
        reasons = _dependency_reasons(definition, bool(row.present), dependencies)
        agents.append(
            CatalogAgent(
                definition=definition,
                available=not reasons,
                availability_reasons=reasons,
                present=bool(row.present),
            )
        )
    return AgentCatalog(tuple(agents))


def reconcile_agent_catalog(
    engine: sa.Engine,
    loaded: LoadedAgentManifest,
    dependencies: AgentDependencySnapshot,
) -> AgentCatalog:
    """Atomically reconcile one boot manifest and return its immutable read view."""
    if loaded.configured and (loaded.raw_hash is None or loaded.semantic_hash is None):
        raise AgentCatalogError("configured Agent manifest has no fingerprint")
    raw_hash = loaded.raw_hash or _UNCONFIGURED_RAW_HASH
    semantic_hash = loaded.semantic_hash or _UNCONFIGURED_SEMANTIC_HASH
    generation = loaded.manifest.generation if loaded.configured else 0
    ensure_agent_catalog_schema(engine)
    try:
        lock = _SQLITE_CATALOG_LOCK if engine.dialect.name == "sqlite" else threading.Lock()
        with lock, engine.begin() as conn:
            if conn.dialect.name == "sqlite":
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            _acquire_lock(conn)
            existing_active = {
                str(row.agent_id): row
                for row in conn.execute(sa.select(ne_agent_active_definitions)).all()
            }
            head = conn.execute(
                sa.select(ne_agent_registry_head).where(ne_agent_registry_head.c.singleton_key == 1)
            ).one_or_none()
            if head is None and existing_active:
                raise AgentCatalogError("Agent registry head is missing")
            if head is not None:
                head_raw_hash = str(head.manifest_raw_hash)
                head_semantic_hash = str(head.manifest_semantic_hash)
                enabling_configured_registry = (
                    not bool(head.configured)
                    and int(head.generation) == 0
                    and loaded.configured
                    and generation >= 1
                )
                if (
                    bool(head.configured) is not loaded.configured
                    and not enabling_configured_registry
                ):
                    raise AgentCatalogError("Agent registry configured-state mismatch")
                active_fingerprints = {
                    (str(row.manifest_raw_hash), str(row.manifest_semantic_hash))
                    for row in existing_active.values()
                }
                if active_fingerprints - {(head_raw_hash, head_semantic_hash)}:
                    raise AgentCatalogError("stored Agent manifest fingerprints are inconsistent")
                if generation < int(head.generation):
                    raise AgentCatalogError("stale Agent registry generation")
                if generation == int(head.generation):
                    semantic_matches = head_semantic_hash == semantic_hash
                    if not semantic_matches:
                        raise AgentCatalogError("immutable Agent registry generation drift")
                    if semantic_matches and head_raw_hash != raw_hash:
                        raise AgentCatalogError("Agent manifest byte fingerprint mismatch")
                    expected_active = {
                        definition.agent_id: (
                            definition.version,
                            definition.definition_hash,
                            True,
                        )
                        for definition in loaded.manifest.agents
                    }
                    latest_history: dict[str, tuple[int, str]] = {}
                    for row in conn.execute(
                        sa.select(
                            ne_agent_definition_versions.c.agent_id,
                            ne_agent_definition_versions.c.version,
                            ne_agent_definition_versions.c.definition_hash,
                        ).order_by(
                            ne_agent_definition_versions.c.agent_id,
                            ne_agent_definition_versions.c.version.desc(),
                        )
                    ).all():
                        latest_history.setdefault(
                            str(row.agent_id),
                            (int(row.version), str(row.definition_hash)),
                        )
                    for agent_id, identity in latest_history.items():
                        expected_active.setdefault(agent_id, (*identity, False))
                    actual_active = {
                        agent_id: (
                            int(row.version),
                            str(row.definition_hash),
                            bool(row.present),
                        )
                        for agent_id, row in existing_active.items()
                    }
                    if actual_active != expected_active:
                        raise AgentCatalogError("stored Agent active/absent state is inconsistent")
                    advance_head = False
                else:
                    if head_semantic_hash == semantic_hash:
                        raise AgentCatalogError(
                            "Agent registry generation has no semantic transition"
                        )
                    advance_head = True
            else:
                advance_head = True
            current_ids = {definition.agent_id for definition in loaded.manifest.agents}
            for definition in loaded.manifest.agents:
                rows = conn.execute(
                    sa.select(
                        ne_agent_definition_versions.c.version,
                        ne_agent_definition_versions.c.definition_hash,
                    )
                    .where(ne_agent_definition_versions.c.agent_id == definition.agent_id)
                    .order_by(ne_agent_definition_versions.c.version.desc())
                ).all()
                if rows:
                    latest_version = int(rows[0].version)
                    if definition.version < latest_version:
                        raise AgentCatalogError("stale Agent definition version")
                    same = next(
                        (row for row in rows if int(row.version) == definition.version), None
                    )
                    if same is not None and str(same.definition_hash) != definition.definition_hash:
                        raise AgentCatalogError("immutable Agent definition drift")
                else:
                    same = None
                active = existing_active.get(definition.agent_id)
                if (
                    advance_head
                    and active is not None
                    and not bool(active.present)
                    and definition.version <= int(active.version)
                ):
                    raise AgentCatalogError("stale Agent definition reactivation")
                if same is None:
                    conn.execute(
                        ne_agent_definition_versions.insert().values(
                            agent_id=definition.agent_id,
                            version=definition.version,
                            definition_hash=definition.definition_hash,
                            definition_json=_definition_json(definition),
                            first_manifest_semantic_hash=semantic_hash,
                        )
                    )
                values = {
                    "version": definition.version,
                    "definition_hash": definition.definition_hash,
                    "present": True,
                    "manifest_raw_hash": raw_hash,
                    "manifest_semantic_hash": semantic_hash,
                    "updated_at": sa.func.now(),
                }
                if not advance_head:
                    continue
                if definition.agent_id in existing_active:
                    conn.execute(
                        ne_agent_active_definitions.update()
                        .where(ne_agent_active_definitions.c.agent_id == definition.agent_id)
                        .values(**values)
                    )
                else:
                    conn.execute(
                        ne_agent_active_definitions.insert().values(
                            agent_id=definition.agent_id,
                            **values,
                        )
                    )
            if existing_active:
                conn.execute(
                    ne_agent_active_definitions.update()
                    .where(ne_agent_active_definitions.c.agent_id.not_in(current_ids))
                    .values(
                        present=False,
                        manifest_raw_hash=raw_hash,
                        manifest_semantic_hash=semantic_hash,
                        updated_at=sa.func.now(),
                    )
                )
            if head is None:
                conn.execute(
                    ne_agent_registry_head.insert().values(
                        singleton_key=1,
                        configured=loaded.configured,
                        generation=generation,
                        manifest_raw_hash=raw_hash,
                        manifest_semantic_hash=semantic_hash,
                    )
                )
            elif advance_head:
                conn.execute(
                    ne_agent_registry_head.update()
                    .where(ne_agent_registry_head.c.singleton_key == 1)
                    .values(
                        configured=loaded.configured,
                        generation=generation,
                        manifest_raw_hash=raw_hash,
                        manifest_semantic_hash=semantic_hash,
                        updated_at=sa.func.now(),
                    )
                )
            return _read_catalog(conn, dependencies)
    except IntegrityError as exc:
        raise AgentCatalogError("concurrent Agent catalog reconciliation conflict") from exc
