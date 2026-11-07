# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fixed Pilot-only protected Agent transport cutover command."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import click

from ne.cli.agent_private_keys import _read_version, _require_fields, _store_from_bundle

if TYPE_CHECKING:
    from ne.runtime.legacy_transport_migration import BackupReceipt, Inventory
    from ne.runtime.private_payloads import ProtectedContentStatus

_LOCK_PATH = Path("/run/lock/neutron-live-upgrade.lock")
_PROJECT = "ne-pilot"
_ROOTS = frozenset({"ne-api", "ne-worker"})
_DATABASES = frozenset({"neutron", "ne_dbos"})
_PILOT_TOPOLOGY_DATABASES = frozenset({"keycloak", "ne_dbos", "neutron", "openfga"})
_GENERATION = "agent-transport-v1"
_RESTART_ORDER = ("ne-worker", "ne-api")
_RESTART_RECEIPT_SCHEMA = "neutron.agent-transport.restart-receipt.v1"
_TARGET_RUNTIME_SCHEMA_COMMAND = r'''exec("""import os
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from ne.audit.engine import create_engine_with_timeouts
from ne.runtime.config import RuntimeSettings
from ne.runtime.store import (
    _active_conversation_attachment_index,
    _assert_principal_conversation_shape,
    _canonical_schema_expression,
    ensure_runtime_schema,
    ne_agent_conversation_turns,
)

settings = RuntimeSettings()
url = make_url(settings.session_database_url).set(
    database=os.environ["NE_BOOTSTRAP_SCHEMA_DATABASE"]
)
engine = create_engine_with_timeouts(
    url.render_as_string(hide_password=False), timeouts=settings.engine_timeouts
)
with engine.begin() as connection:
    rewrite_active_attachment_index = False
    inspector = sa.inspect(connection)
    conversations_present = inspector.has_table("ne_agent_conversations")
    turns_present = inspector.has_table("ne_agent_conversation_turns")
    if conversations_present is not turns_present:
        raise RuntimeError("scratch runtime conversation tables are foreign")
    if turns_present:
        protected_columns = (
            "input_status",
            "protected_input_ref",
            "protected_input_hash",
            "terminal_status",
            "protected_terminal_ref",
            "protected_terminal_hash",
        )

        def column_signature(column):
            identity = column.get("identity")
            return (
                str(column["name"]),
                str(column["type"].compile(dialect=connection.dialect)).upper(),
                bool(column["nullable"]),
                None if column.get("default") is None else str(column["default"]),
                None if identity is None else ("a" if identity.get("always") else "d"),
                column.get("computed") is not None,
            )

        def metadata_signature(column):
            identity = column.identity
            return (
                column.name,
                str(column.type.compile(dialect=connection.dialect)).upper(),
                bool(column.nullable),
                None,
                None if identity is None else ("a" if identity.always else "d"),
                column.computed is not None,
            )

        actual_columns = {
            signature[0]: signature
            for signature in (
                column_signature(column)
                for column in inspector.get_columns("ne_agent_conversation_turns")
            )
        }
        expected_columns = {
            signature[0]: signature
            for signature in (
                metadata_signature(column) for column in ne_agent_conversation_turns.c
            )
        }
        expected_historical_columns = {
            name: signature
            for name, signature in expected_columns.items()
            if name not in protected_columns
        }
        if actual_columns == expected_historical_columns:
            for name in protected_columns:
                column = ne_agent_conversation_turns.c[name]
                if not column.nullable or column.server_default is not None:
                    raise RuntimeError("target runtime protected column metadata is foreign")
                sql_type = str(column.type.compile(dialect=connection.dialect))
                connection.exec_driver_sql(
                    "ALTER TABLE ne_agent_conversation_turns ADD COLUMN {} {}".format(
                        name, sql_type
                    )
                )
            inspector.clear_cache()
            actual_columns = {
                signature[0]: signature
                for signature in (
                    column_signature(column)
                    for column in inspector.get_columns("ne_agent_conversation_turns")
                )
            }
        if actual_columns != expected_columns:
            raise RuntimeError("scratch runtime conversation columns are foreign")
        try:
            _assert_principal_conversation_shape(connection)
        except RuntimeError as exc:
            if (
                str(exc)
                != "runtime ne_agent_conversation_turns indexes have an incompatible shape"
            ):
                raise
            rewrite_active_attachment_index = True
    if rewrite_active_attachment_index:
        inspector.clear_cache()
        dialect_where_key = f"{connection.dialect.name}_where"
        actual_indexes = {}
        for table in ("ne_agent_conversations", "ne_agent_conversation_turns"):
            for item in inspector.get_indexes(table):
                if item.get("duplicates_constraint"):
                    continue
                where = (item.get("dialect_options") or {}).get(dialect_where_key)
                actual_indexes[(table, str(item.get("name")))] = (
                    tuple(item.get("column_names") or ()),
                    bool(item.get("unique")),
                    None if where is None else _canonical_schema_expression(where),
                )
        expected_restored_indexes = {
            ("ne_agent_conversations", "ix_ne_agent_conversations_owner_principal_id"): (
                ("owner_principal_id",),
                False,
                None,
            ),
            (
                "ne_agent_conversation_turns",
                "ix_ne_agent_conversation_turns_conversation_id",
            ): (("conversation_id",), False, None),
            (
                "ne_agent_conversation_turns",
                "uq_agent_conversation_turn_active_attachment",
            ): (
                ("conversation_id", "attachment_id"),
                True,
                "publication_state=any(array[({q}claimed{q}),({q}staged{q}),"
                "({q}prepared{q}),({q}published{q})])".format(q=chr(39)),
            ),
        }
        if actual_indexes != expected_restored_indexes:
            raise RuntimeError("scratch runtime indexes are foreign")
        _active_conversation_attachment_index.drop(connection, checkfirst=False)
        _active_conversation_attachment_index.create(connection)
        _assert_principal_conversation_shape(connection)
ensure_runtime_schema(engine)
engine.dispose()
""")'''
_OWNERLESS_RETIREMENT_REASON = "ownerless_principal_unrecoverable"
_RETIRED_WIRE = (
    '{"fence":"%s","generation":"agent-transport-v1",'
    '"kind":"legacy_transport_retired","reason":"ownerless_principal_unrecoverable"}'
)
_PLAINTEXT_SURFACES = {
    "neutron": {
        "ne_agent_session_messages.payload": (
            "SELECT count(*) FROM ne_agent_session_messages WHERE payload <> ''"
        ),
        "ne_agent_conversation_turns.private_context": (
            "SELECT count(*) FROM ne_agent_conversation_turns WHERE private_context <> ''"
        ),
        "ne_agent_conversation_turns.served_output": (
            "SELECT count(*) FROM ne_agent_conversation_turns WHERE served_output <> ''"
        ),
        "ne_agent_conversation_turns.served_citations": (
            "SELECT count(*) FROM ne_agent_conversation_turns "
            "WHERE served_citations NOT IN ('','[]')"
        ),
    },
    "ne_dbos": {
        "workflow_status.inputs": (
            "SELECT count(*) FROM dbos.workflow_status "
            "WHERE name='chat.turn' AND inputs IS NOT NULL"
        ),
        "workflow_status.output": (
            "SELECT count(*) FROM dbos.workflow_status "
            "WHERE name='chat.turn' AND output IS NOT NULL"
        ),
        "workflow_status.error": (
            "SELECT count(*) FROM dbos.workflow_status WHERE name='chat.turn' AND error IS NOT NULL"
        ),
        "workflow_status.authenticated_user": (
            "SELECT count(*) FROM dbos.workflow_status "
            "WHERE name='chat.turn' AND authenticated_user IS NOT NULL"
        ),
        "workflow_status.authenticated_roles": (
            "SELECT count(*) FROM dbos.workflow_status "
            "WHERE name='chat.turn' AND authenticated_roles IS NOT NULL"
        ),
        "workflow_status.assumed_role": (
            "SELECT count(*) FROM dbos.workflow_status "
            "WHERE name='chat.turn' AND assumed_role IS NOT NULL"
        ),
        "operation_outputs.output": (
            "SELECT count(*) FROM dbos.operation_outputs o JOIN dbos.workflow_status w "
            "USING (workflow_uuid) WHERE w.name='chat.turn' AND o.output IS NOT NULL"
        ),
        "operation_outputs.error": (
            "SELECT count(*) FROM dbos.operation_outputs o JOIN dbos.workflow_status w "
            "USING (workflow_uuid) WHERE w.name='chat.turn' AND o.error IS NOT NULL"
        ),
        "notifications.message": (
            "SELECT count(*) FROM dbos.notifications n JOIN dbos.workflow_status w "
            "ON w.workflow_uuid=n.destination_uuid WHERE w.name='chat.turn' "
            'AND n.message NOT LIKE \'%"kind":"legacy_transport_retired"%\''
        ),
        "streams.value": (
            "SELECT count(*) FROM dbos.streams s JOIN dbos.workflow_status w "
            "USING (workflow_uuid) WHERE w.name='chat.turn' "
            'AND s.value NOT LIKE \'%"kind":"legacy_transport_retired"%\''
        ),
        "workflow_events.value": (
            "SELECT count(*) FROM dbos.workflow_events e JOIN dbos.workflow_status w "
            "USING (workflow_uuid) WHERE w.name='chat.turn' "
            'AND e.value NOT LIKE \'%"kind":"legacy_transport_retired"%\''
        ),
        "workflow_events_history.value": (
            "SELECT count(*) FROM dbos.workflow_events_history e "
            "JOIN dbos.workflow_status w USING (workflow_uuid) "
            "WHERE w.name='chat.turn' "
            'AND e.value NOT LIKE \'%"kind":"legacy_transport_retired"%\''
        ),
    },
}


def _legacy_conversation_payloads(
    row: Mapping[str, object],
) -> tuple[tuple[ProtectedContentStatus, bytes], tuple[ProtectedContentStatus, bytes]]:
    """Classify the exact legacy Conversation bytes consumed by the cutover."""
    from pydantic import ValidationError
    from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, UserPromptPart

    from ne.api.turn_wire import ProtectedTerminalPayload
    from ne.contracts.runtime import GroundedProvenance, VerifiedProvenance
    from ne.runtime.private_payloads import ProtectedContentStatus

    prompt: str | None = None
    try:
        private_context = row["private_context"]
        if not isinstance(private_context, (str, bytes, bytearray)):
            raise TypeError
        messages = ModelMessagesTypeAdapter.validate_json(private_context)
        prompt = next(
            part.content
            for message in reversed(messages)
            if isinstance(message, ModelRequest)
            for part in reversed(message.parts)
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        )
    except (KeyError, StopIteration, TypeError, ValidationError):
        pass
    input_payload = (
        ProtectedContentStatus.SOURCE_FREE
        if prompt is not None
        else ProtectedContentStatus.LEGACY_UNAVAILABLE,
        (prompt or "").encode(),
    )
    try:
        if row["publication_state"] not in {"staged", "prepared", "published"}:
            raise ValueError("turn has no terminal envelope")
        raw_citations = json.loads(str(row["served_citations"]))
        if not isinstance(raw_citations, list):
            raise ValueError("malformed citations")
        terminal = ProtectedTerminalPayload(
            output=str(row["served_output"]),
            citations=tuple(raw_citations),
            verified=(
                None
                if row["served_verified"] in (None, "", "null")
                else VerifiedProvenance.model_validate_json(str(row["served_verified"]))
            ),
            grounded=(
                None
                if row["served_grounded"] in (None, "", "null")
                else GroundedProvenance.model_validate_json(str(row["served_grounded"]))
            ),
        )
        terminal_payload = ProtectedContentStatus.SOURCE_FREE, terminal.model_dump_json().encode()
    except (KeyError, TypeError, ValueError, ValidationError):
        terminal_payload = ProtectedContentStatus.LEGACY_UNAVAILABLE, b""
    return input_payload, terminal_payload


def _require_image_digest(image_digest: str) -> None:
    if (
        not image_digest.startswith("sha256:")
        or len(image_digest) != 71
        or any(char not in "0123456789abcdef" for char in image_digest[7:])
    ):
        raise ValueError("protected transport image digest is invalid")


def _require_sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be lowercase sha256")


@dataclass(frozen=True, slots=True)
class _ReplayInspection:
    """Private, process-local replay detail; row material is never serialized."""

    projection: dict[str, object]
    invariant_rows: tuple[object, ...]
    schemas: tuple[object, ...]
    sequences: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _RelationState:
    contract_sha256: str
    rows: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _DatabaseSnapshot:
    relations: Mapping[tuple[str, str, str], _RelationState]
    sequences: Mapping[tuple[str, str, str, str], str]
    objects: Mapping[tuple[str, str, str], str]


class _ProtectedTransportPostgresView:
    """Closed two-database view over the full Pilot Postgres backup port."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def list_databases(self) -> list[str]:
        return sorted(_DATABASES)

    def dump_globals(self, dest: Path) -> None:
        self._delegate.dump_globals(dest)

    def dump_database(self, database: str, dest: Path) -> None:
        if database not in _DATABASES:
            raise ValueError("protected-transport backup database is invalid")
        self._delegate.dump_database(database, dest)

    def restore_globals(self, src: Path) -> None:
        self._delegate.restore_globals(src)

    def restore_database(self, database: str, src: Path) -> None:
        if database not in _DATABASES:
            raise ValueError("protected-transport backup database is invalid")
        self._delegate.restore_database(database, src)

    def scalar(self, database: str, sql: str) -> str:
        if database not in _DATABASES:
            raise ValueError("protected-transport backup database is invalid")
        return str(self._delegate.scalar(database, sql))

    def user_table_count(self, database: str) -> int:
        if database not in _DATABASES:
            raise ValueError("protected-transport backup database is invalid")
        return int(self._delegate.user_table_count(database))


def _create_verified_pilot_topology_backup(*, bundle: Path, dest: Path) -> dict[str, str]:
    """Create the fixed full-Pilot topology backup used before Postgres takeover."""
    from ne.backup.cli import _build_services
    from ne.backup.config import BackupConfig
    from ne.backup.crypto import kek_fingerprint
    from ne.backup.errors import ManifestError, RestoreError
    from ne.backup.keys import resolve_kek
    from ne.backup.runtime import create_backup, verify_backup

    config = BackupConfig()
    services = _build_services(config)
    databases = services.postgres.list_databases()
    if len(databases) != len(set(databases)) or frozenset(databases) != _PILOT_TOPOLOGY_DATABASES:
        raise ManifestError("Pilot topology backup requires exactly the four admitted databases")
    kek = resolve_kek(bundle)
    manifest = create_backup(
        config=config,
        services=services,
        dest=dest,
        kek=kek,
        kind="on-demand",
        note="Pilot PostgreSQL/AGE topology pre-takeover",
        bundle=bundle,
        staging_root=Path(config.staging_dir) if config.staging_dir else None,
    )
    report = verify_backup(
        dest,
        manifest.id,
        kek,
        staging_root=Path(config.staging_dir) if config.staging_dir else None,
    )
    manifest_path = dest / manifest.id / "manifest.json"
    raw = manifest_path.read_bytes()
    if (
        raw != manifest.to_json().encode("utf-8")
        or manifest.databases != sorted(_PILOT_TOPOLOGY_DATABASES)
        or report.get("id") != manifest.id
        or report.get("ciphertext_verified") is not True
        or report.get("plaintext_verified") is not True
        or manifest.kek_fingerprint != kek_fingerprint(kek)
    ):
        raise RestoreError("full Pilot topology backup did not decrypt-verify exactly")
    return {
        "backup_id": manifest.id,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "kek_fingerprint": manifest.kek_fingerprint,
    }


class FixedPilotCutoverPort:
    """Host Pilot adapter with fixed service names and fixed Compose argv shape."""

    def __init__(
        self,
        *,
        bundle: Path,
        backup_dest: Path,
        compose_files: tuple[Path, ...] = (),
        compose_working_dir: Path | None = None,
        platform_database_url: str | None = None,
        private_payload_key: tuple[str, bytes] | None = None,
        database_urls: Mapping[str, str] | None = None,
        _database_names: Mapping[str, str] | None = None,
        _inherited_lock_fd: int | None = None,
    ) -> None:
        self.bundle = bundle.resolve()
        self.backup_dest = backup_dest.resolve()
        if not compose_files:
            from ne.cli.checks import _find_repo_root

            compose_files = (_find_repo_root(Path.cwd()) / "deploy/compose/pilot/compose.yaml",)
        if any(
            not path.is_absolute()
            or not path.is_file()
            or path.is_symlink()
            or path.resolve() != path
            for path in compose_files
        ):
            raise click.ClickException(
                "Pilot cutover Compose manifest must be an exact canonical regular file"
            )
        self.compose_files = compose_files
        raw_working_dir = compose_working_dir or self.compose_files[0].parent
        if (
            not raw_working_dir.is_absolute()
            or not raw_working_dir.is_dir()
            or raw_working_dir.is_symlink()
            or raw_working_dir.resolve() != raw_working_dir
        ):
            raise click.ClickException("Pilot cutover Compose working directory is unavailable")
        self.compose_working_dir = raw_working_dir
        runtime_dir = self.bundle / "runtime"
        if (
            not runtime_dir.is_dir()
            or runtime_dir.is_symlink()
            or runtime_dir.resolve() != runtime_dir
            or runtime_dir.stat().st_uid != os.geteuid()
        ):
            raise click.ClickException("Pilot cutover runtime directory is unavailable")
        self.pilot_env = runtime_dir / "pilot.env"
        try:
            env_info = self.pilot_env.lstat()
        except OSError as error:
            raise click.ClickException(
                "Pilot cutover runtime environment is unavailable"
            ) from error
        if (
            not stat.S_ISREG(env_info.st_mode)
            or self.pilot_env.is_symlink()
            or stat.S_IMODE(env_info.st_mode) != 0o600
            or env_info.st_nlink != 1
            or env_info.st_uid != os.geteuid()
        ):
            raise click.ClickException(
                "Pilot cutover runtime environment must be an owner-only mode-0600 file"
            )
        bases = [
            path
            for path in self.compose_files
            if path.as_posix().endswith("/deploy/compose/pilot/compose.yaml")
        ]
        if len(bases) != 1:
            raise click.ClickException("Pilot cutover requires one exact staged base manifest")
        base = bases[0]
        stage_root = base.parents[3]
        registry = stage_root / "deploy/agents/registry.toml"
        skill = stage_root / "deploy/skills/meridian-hardship-draft/SKILL.md"
        if any(
            not path.is_file()
            or path.is_symlink()
            or path.resolve() != path
            or path.stat().st_uid != stage_root.stat().st_uid
            or path.stat().st_nlink != 1
            or stat.S_IMODE(path.stat().st_mode) != 0o644
            for path in (registry, skill)
        ):
            raise click.ClickException(
                "Pilot cutover registry or Skill source binding is unavailable"
            )
        self._stage_root = stage_root
        self._agent_registry_path = registry
        self._skill_path = skill
        self._compose_env = {
            "PATH": os.environ.get("PATH", ""),
            "NE_AGENT_REGISTRY_RAW_SHA256": hashlib.sha256(registry.read_bytes()).hexdigest(),
            "NE_SKILL_BUNDLE_RAW_SHA256": hashlib.sha256(skill.read_bytes()).hexdigest(),
        }
        self._compose_prefix = [
            "docker",
            "compose",
            "--project-name",
            _PROJECT,
            *[part for path in self.compose_files for part in ("-f", str(path))],
            "--env-file",
            str(self.pilot_env),
            "--profile",
            "core",
        ]
        self._lock_fd: int | None = None
        self._inherited_lock = _inherited_lock_fd is not None
        if _inherited_lock_fd is not None:
            try:
                descriptor_info = os.fstat(_inherited_lock_fd)
                lock_info = _LOCK_PATH.stat()
                if (
                    not stat.S_ISREG(descriptor_info.st_mode)
                    or (descriptor_info.st_dev, descriptor_info.st_ino)
                    != (lock_info.st_dev, lock_info.st_ino)
                    or descriptor_info.st_uid != os.geteuid()
                ):
                    raise OSError("foreign inherited upgrade lock")
                fcntl.flock(_inherited_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, ValueError) as error:
                raise click.ClickException(
                    "Pilot cutover inherited upgrade lock is unavailable"
                ) from error
        self._platform_database_url = platform_database_url
        self._private_payload_key = private_payload_key
        self._database_urls = dict(database_urls or {})
        self._database_names = dict(_database_names or {})
        if set(self._database_names) - _DATABASES or any(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None
            for value in self._database_names.values()
        ):
            raise click.ClickException("protected transport replay database is foreign")

    def _run(self, *args: str, capture: bool = False) -> str:
        result = subprocess.run(
            [*self._compose_prefix, *args],
            cwd=self.compose_working_dir,
            env=self._compose_env,
            check=True,
            text=True,
            capture_output=capture,
        )
        return result.stdout.strip() if capture else ""

    async def lock(self) -> None:
        if self._inherited_lock:
            return

        def acquire() -> int:
            _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(_LOCK_PATH, os.O_WRONLY | os.O_CREAT, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor

        self._lock_fd = await asyncio.to_thread(acquire)

    async def unlock(self) -> None:
        if self._inherited_lock:
            return
        if self._lock_fd is None:
            return
        descriptor, self._lock_fd = self._lock_fd, None
        await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    async def scale_zero(self) -> None:
        await asyncio.to_thread(self._run, "scale", "ne-api=0", "ne-worker=0")

    async def roots_zero(self) -> bool:
        output = await asyncio.to_thread(
            self._run, "ps", "--status", "running", "--services", capture=True
        )
        return not ({"ne-api", "ne-worker"} & set(output.splitlines()))

    def compose_shape(self) -> dict[str, str]:
        """Resolve the exact bound Compose config hash for both application roots."""
        services = set(self._run("config", "--services", capture=True).splitlines())
        if not services >= _ROOTS:
            raise click.ClickException("Pilot cutover Compose graph omits an application root")
        shape: dict[str, str] = {}
        for root in sorted(_ROOTS):
            output = self._run("config", "--hash", root, capture=True)
            rows = output.splitlines()
            if (
                len(rows) != 1
                or len(rows[0].split()) != 2
                or rows[0].split()[0] != root
                or re.fullmatch(r"[0-9a-f]{64}", rows[0].split()[1]) is None
            ):
                raise click.ClickException(f"Pilot cutover {root} Compose hash is unavailable")
            shape[root] = rows[0].split()[1]
        return shape

    def preflight(self) -> None:
        """Resolve the exact bound Compose graph and both application roots."""
        self.compose_shape()

    async def backup(self) -> BackupReceipt:
        """Create and decrypt-verify one encrypted receipt while both roots are zero."""
        if not await self.roots_zero():
            raise click.ClickException("backup requires both application roots at zero")

        def create_verified() -> BackupReceipt:
            from ne.backup.cli import _build_services
            from ne.backup.config import BackupConfig
            from ne.backup.crypto import kek_fingerprint
            from ne.backup.keys import resolve_kek
            from ne.backup.runtime import create_backup, verify_backup
            from ne.runtime.legacy_transport_migration import BackupReceipt

            config = BackupConfig()
            kek = resolve_kek(self.bundle)
            services = _build_services(config)
            databases = services.postgres.list_databases()
            if (
                len(databases) != len(set(databases))
                or frozenset(databases) != _PILOT_TOPOLOGY_DATABASES
            ):
                raise click.ClickException(
                    "Pilot protected-transport backup requires the exact admitted topology"
                )
            protected_services = replace(
                services, postgres=_ProtectedTransportPostgresView(services.postgres)
            )
            manifest = create_backup(
                config=config,
                services=protected_services,
                dest=self.backup_dest,
                kek=kek,
                kind="on-demand",
                note="agent protected-transport pre-cutover",
                bundle=self.bundle,
                staging_root=Path(config.staging_dir) if config.staging_dir else None,
            )
            report = verify_backup(
                self.backup_dest,
                manifest.id,
                kek,
                staging_root=Path(config.staging_dir) if config.staging_dir else None,
            )
            databases = frozenset(manifest.databases)
            if (
                databases != _DATABASES
                or report.get("id") != manifest.id
                or report.get("ciphertext_verified") is not True
                or report.get("plaintext_verified") is not True
                or manifest.kek_fingerprint != kek_fingerprint(kek)
            ):
                raise click.ClickException(
                    "verified cutover backup must contain exactly neutron and ne_dbos"
                )
            manifest_path = self.backup_dest / manifest.id / "manifest.json"
            manifest_bytes = manifest_path.read_bytes()
            if manifest_bytes != manifest.to_json().encode("utf-8"):
                raise click.ClickException("cutover backup manifest bytes changed after creation")
            return BackupReceipt(
                backup_id=manifest.id,
                manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                kek_fingerprint=kek_fingerprint(kek),
                databases=databases,
                verified=True,
            )

        return await asyncio.to_thread(create_verified)

    def _psql_scalar(self, database: str, sql: str) -> str:
        if database not in _DATABASES:
            raise ValueError("cutover database is invalid")
        if database in self._database_urls:
            from ne.audit.engine import create_engine_with_timeouts

            engine = create_engine_with_timeouts(self._database_urls[database])
            try:
                raw = engine.raw_connection()
                try:
                    driver = raw.driver_connection
                    assert driver is not None
                    driver.autocommit = True
                    cursor = driver.cursor()
                    try:
                        cursor.execute(sql)
                        rows: list[tuple[object, ...]] = []
                        while True:
                            if cursor.description is not None:
                                rows = cursor.fetchall()
                            if not cursor.nextset():
                                break
                        return "\n".join(str(row[0]) for row in rows)
                    finally:
                        cursor.close()
                finally:
                    raw.close()
            finally:
                engine.dispose()
        result = subprocess.run(
            [
                *self._compose_prefix,
                "exec",
                "--no-TTY",
                "postgres",
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "ne",
                "-d",
                self._physical_database_name(database),
                "--tuples-only",
                "--no-align",
                "--quiet",
                "--command",
                sql,
            ],
            cwd=self.compose_working_dir,
            env=self._compose_env,
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip()

    def _physical_database_name(self, database: str) -> str:
        """Return the admitted physical DB name without placing a DSN in argv."""

        if database not in _DATABASES:
            raise ValueError("cutover database is invalid")
        database_names = getattr(self, "_database_names", {})
        if database in database_names:
            return database_names[database]
        database_urls = getattr(self, "_database_urls", {})
        if database not in database_urls:
            return database
        import sqlalchemy as sa

        value = sa.make_url(database_urls[database]).database
        if value is None or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
            raise click.ClickException("protected transport replay database is foreign")
        return value

    def _schema_dump(self, database: str) -> str:
        """Capture schema from the same physical database used for row inspection."""

        return self._run(
            "exec",
            "--no-TTY",
            "postgres",
            "pg_dump",
            "-U",
            "ne",
            "-d",
            self._physical_database_name(database),
            "--schema-only",
            "--no-owner",
            "--restrict-key=3c4f184de76e9a475cf91a1a63d554c8",
            "--exclude-table=public.ne_agent_transport_retirement",
            "--exclude-table=public.ne_agent_transport_generation",
            capture=True,
        )

    @staticmethod
    def _closed_hash(domain: str, value: object) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(domain.encode() + b"\0" + payload).hexdigest()

    @classmethod
    def _record_schema_objects(
        cls,
        *,
        database: str,
        object_rows: list[str],
        objects: dict[tuple[str, str, str], str],
    ) -> None:
        """Record full logical object identities while refusing real duplicates."""

        for line in object_rows:
            parts = json.loads(line)
            if (
                not isinstance(parts, list)
                or len(parts) != 3
                or any(not isinstance(part, str) for part in parts[:2])
                or not all(parts[:2])
            ):
                raise click.ClickException("protected transport successor schema object is foreign")
            key = (database, parts[0], parts[1])
            if key in objects:
                raise click.ClickException(
                    "protected transport successor schema object is duplicated"
                )
            objects[key] = cls._closed_hash("transport-worker-boot-schema-object-v1", parts[2])

    def _database_snapshot(self) -> _DatabaseSnapshot:
        """Capture complete relation/sequence state for private scratch comparison."""

        relations: dict[tuple[str, str, str], _RelationState] = {}
        sequences: dict[tuple[str, str, str, str], str] = {}
        objects: dict[tuple[str, str, str], str] = {}
        for database in sorted(_DATABASES):
            identities = self._psql_scalar(
                database,
                "SELECT n.nspname||'|'||c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind IN ('r','p','v','m','f') "
                "AND n.nspname NOT IN ('pg_catalog','information_schema','ag_catalog') "
                "AND n.nspname!~'^pg_(toast_)?temp_[0-9]+$' ORDER BY 1",
            ).splitlines()
            for identity in identities:
                parts = identity.split("|")
                if len(parts) != 2 or any(
                    re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) is None for part in parts
                ):
                    raise click.ClickException(
                        "protected transport successor relation identity is foreign"
                    )
                schema_name, table_name = parts
                relation = (database, schema_name, table_name)
                quoted = f'"{schema_name}"."{table_name}"'
                rows = tuple(
                    json.loads(line)
                    for line in self._psql_scalar(
                        database,
                        f"SELECT to_jsonb(t)::text FROM {quoted} t ORDER BY to_jsonb(t)::text",
                    ).splitlines()
                    if line
                )
                if any(not isinstance(row, dict) for row in rows):
                    raise click.ClickException(
                        "protected transport successor relation row is foreign"
                    )
                contract_rows = self._psql_scalar(
                    database,
                    "SELECT value::text FROM ("
                    "SELECT jsonb_build_array('column',a.attnum,a.attname,"
                    "pg_catalog.format_type(a.atttypid,a.atttypmod),a.attnotnull,"
                    "coalesce(pg_get_expr(d.adbin,d.adrelid),''),a.attidentity,"
                    "a.attgenerated,coalesce((SELECT jsonb_agg(x::text ORDER BY x::text) "
                    "FROM unnest(a.attacl) x),'[]'::jsonb)) AS value FROM pg_attribute a "
                    "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
                    f"WHERE a.attrelid='{schema_name}.{table_name}'::regclass "
                    "AND a.attnum>0 AND NOT a.attisdropped UNION ALL "
                    "SELECT jsonb_build_array('constraint',conname,contype,"
                    "pg_get_constraintdef(oid,true)) FROM pg_constraint "
                    f"WHERE conrelid='{schema_name}.{table_name}'::regclass UNION ALL "
                    "SELECT jsonb_build_array('index',indexname,indexdef) FROM pg_indexes "
                    f"WHERE schemaname='{schema_name}' AND tablename='{table_name}' UNION ALL "
                    "SELECT jsonb_build_array('trigger',tgname,pg_get_triggerdef(oid,true)) "
                    "FROM pg_trigger "
                    f"WHERE tgrelid='{schema_name}.{table_name}'::regclass AND NOT tgisinternal "
                    "UNION ALL SELECT jsonb_build_array('relation',relkind,relrowsecurity,"
                    "relforcerowsecurity,coalesce(to_jsonb(reloptions),'[]'::jsonb),"
                    "pg_get_userbyid(relowner),coalesce((SELECT jsonb_agg(x::text "
                    "ORDER BY x::text) FROM unnest(relacl) x),'[]'::jsonb)) "
                    f"FROM pg_class WHERE oid='{schema_name}.{table_name}'::regclass UNION ALL "
                    "SELECT jsonb_build_array('policy',polname,polpermissive,polroles::text,"
                    "polcmd,coalesce(pg_get_expr(polqual,polrelid),''),"
                    "coalesce(pg_get_expr(polwithcheck,polrelid),'')) FROM pg_policy "
                    f"WHERE polrelid='{schema_name}.{table_name}'::regclass"
                    ") contract ORDER BY value",
                ).splitlines()
                relations[relation] = _RelationState(
                    contract_sha256=self._closed_hash(
                        "transport-worker-boot-relation-contract-v1", contract_rows
                    ),
                    rows=rows,
                )
            for line in self._psql_scalar(
                database,
                "BEGIN;CREATE TEMP TABLE ne_transport_sequences ("
                "schema_name text,sequence_name text,data_type text,start_value numeric,"
                "minimum_value numeric,maximum_value numeric,increment_by numeric,cycle boolean,"
                "cache_size numeric,last_value numeric,is_called boolean,sequence_owner text,"
                "sequence_acl jsonb,owned_schema text,owned_relation text,"
                "owned_column_number integer,owned_column text,dependency_type text) "
                "ON COMMIT DROP;"
                "DO $block$ DECLARE item record; current_last numeric; current_called boolean;"
                "BEGIN FOR item IN SELECT n.nspname AS schema_name,c.relname AS sequence_name,"
                "format_type(s.seqtypid,NULL) AS data_type,s.seqstart,s.seqmin,s.seqmax,"
                "s.seqincrement,s.seqcycle,s.seqcache,pg_get_userbyid(c.relowner) "
                "AS sequence_owner,"
                "coalesce((SELECT jsonb_agg(x::text ORDER BY x::text) FROM unnest(c.relacl) x),"
                "'[]'::jsonb) AS sequence_acl,owned_ns.nspname AS owned_schema,"
                "owned_rel.relname AS owned_relation,owned.refobjsubid AS owned_column_number,"
                "owned_attr.attname AS owned_column,owned.deptype AS dependency_type "
                "FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN LATERAL ("
                "SELECT d.refobjid,d.refobjsubid,d.deptype FROM pg_depend d "
                "WHERE d.classid='pg_class'::regclass AND d.objid=c.oid "
                "AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i') "
                "ORDER BY d.refobjid,d.refobjsubid,d.deptype LIMIT 2) owned ON true "
                "LEFT JOIN pg_class owned_rel ON owned_rel.oid=owned.refobjid "
                "LEFT JOIN pg_namespace owned_ns ON owned_ns.oid=owned_rel.relnamespace "
                "LEFT JOIN pg_attribute owned_attr ON owned_attr.attrelid=owned.refobjid "
                "AND owned_attr.attnum=owned.refobjsubid "
                "WHERE n.nspname<>'pg_toast' AND n.nspname!~'^pg_(toast_)?temp_[0-9]+$' "
                "ORDER BY n.nspname,c.relname LOOP EXECUTE format("
                "'SELECT last_value,is_called FROM %I.%I',item.schema_name,item.sequence_name) "
                "INTO current_last,current_called;INSERT INTO ne_transport_sequences VALUES ("
                "item.schema_name,item.sequence_name,item.data_type,item.seqstart,item.seqmin,"
                "item.seqmax,item.seqincrement,item.seqcycle,item.seqcache,current_last,"
                "current_called,item.sequence_owner,item.sequence_acl,item.owned_schema,"
                "item.owned_relation,item.owned_column_number,item.owned_column,"
                "item.dependency_type);END LOOP;END $block$;"
                "SELECT jsonb_build_object('schema',schema_name,'name',sequence_name,"
                "'data_type',data_type,'start',start_value::text,'min',minimum_value::text,"
                "'max',maximum_value::text,'increment',increment_by::text,'cycle',cycle,"
                "'cache',cache_size::text,'last',last_value::text,'is_called',is_called,"
                "'owner',sequence_owner,'acl',sequence_acl,'owned_schema',owned_schema,"
                "'owned_relation',owned_relation,'owned_column_number',owned_column_number,"
                "'owned_column',owned_column,'dependency',dependency_type)::text "
                "FROM ne_transport_sequences ORDER BY schema_name,sequence_name;COMMIT;",
            ).splitlines():
                parts = json.loads(line)
                required = {
                    "schema",
                    "name",
                    "data_type",
                    "start",
                    "min",
                    "max",
                    "increment",
                    "cycle",
                    "cache",
                    "last",
                    "is_called",
                    "owner",
                    "acl",
                    "owned_schema",
                    "owned_relation",
                    "owned_column_number",
                    "owned_column",
                    "dependency",
                }
                if not isinstance(parts, dict) or set(parts) != required:
                    raise click.ClickException(
                        "protected transport successor sequence identity is foreign"
                    )
                schema_name = parts["schema"]
                sequence_name = parts["name"]
                owner_schema = parts["owned_schema"] or ""
                owner_table = parts["owned_relation"] or ""
                if any(
                    not isinstance(value, str)
                    or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None
                    for value in (schema_name, sequence_name)
                ):
                    raise click.ClickException(
                        "protected transport successor sequence identity is foreign"
                    )
                key = (database, schema_name, sequence_name, owner_schema + "." + owner_table)
                if key in sequences:
                    raise click.ClickException(
                        "protected transport successor sequence identity is duplicated"
                    )
                sequences[key] = self._closed_hash("transport-worker-boot-sequence-v2", parts)
            object_rows = self._psql_scalar(
                database,
                "SELECT jsonb_build_array(kind,identity,value)::text FROM ("
                "SELECT 'database'::text AS kind,'current'::text AS identity,"
                "jsonb_build_object('encoding',pg_encoding_to_char(encoding),'provider',"
                "datlocprovider::text,'collate',datcollate,'ctype',datctype,'locale',"
                "coalesce(datlocale,''),'icu_rules',coalesce(daticurules,''),'owner',"
                "pg_get_userbyid(datdba),'acl',CASE WHEN datacl IS NULL THEN NULL ELSE "
                "(SELECT jsonb_agg(x::text ORDER BY x::text) FROM unnest(datacl) x) END) "
                "AS value FROM pg_database WHERE datname=current_database() UNION ALL "
                "SELECT 'schema'::text AS kind,n.nspname::text AS identity,"
                "jsonb_build_object('owner',pg_get_userbyid(n.nspowner),'acl',"
                "coalesce((SELECT jsonb_agg(x::text ORDER BY x::text) "
                "FROM unnest(n.nspacl) x),'[]'::jsonb)) AS value "
                "FROM pg_namespace n WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname!~'^pg_(toast_)?temp_[0-9]+$' UNION ALL "
                "SELECT 'function'::text,(n.nspname||'.'||p.proname||'('||"
                "pg_get_function_identity_arguments(p.oid)||')')::text,"
                "jsonb_build_object('kind',p.prokind,'arguments',pg_get_function_arguments(p.oid),"
                "'result',pg_get_function_result(p.oid),'owner',pg_get_userbyid(p.proowner),"
                "'acl',coalesce((SELECT jsonb_agg(x::text ORDER BY x::text) "
                "FROM unnest(p.proacl) x),'[]'::jsonb),'volatile',p.provolatile,"
                "'parallel',p.proparallel,'security_definer',p.prosecdef,"
                "'leakproof',p.proleakproof,'strict',p.proisstrict,'set',p.proretset,"
                "'language',l.lanname,'config',coalesce(to_jsonb(p.proconfig),'[]'::jsonb),"
                "'source',p.prosrc,'binary',coalesce(p.probin,'')) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace "
                "JOIN pg_language l ON l.oid=p.prolang "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname!~'^pg_(toast_)?temp_[0-9]+$' UNION ALL "
                "SELECT 'type'::text,(n.nspname||'.'||t.typname)::text,jsonb_build_object("
                "'owner',pg_get_userbyid(t.typowner),'acl',"
                "coalesce((SELECT jsonb_agg(x::text ORDER BY x::text) "
                "FROM unnest(t.typacl) x),'[]'::jsonb),'kind',t.typtype,"
                "'category',t.typcategory,'relation',CASE WHEN t.typrelid=0 THEN '' "
                "ELSE t.typrelid::regclass::text END) FROM pg_type t "
                "JOIN pg_namespace n ON n.oid=t.typnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname!~'^pg_(toast_)?temp_[0-9]+$' UNION ALL "
                "SELECT 'extension'::text,e.extname::text,jsonb_build_object("
                "'version',e.extversion,"
                "'owner',pg_get_userbyid(e.extowner),'config',coalesce((SELECT jsonb_agg("
                "jsonb_build_array(config::regclass::text,e.extcondition[ordinality]) "
                "ORDER BY ordinality) FROM unnest(e.extconfig) WITH ORDINALITY AS item("
                "config,ordinality)),'[]'::jsonb)) "
                "FROM pg_extension e UNION ALL "
                "SELECT 'language'::text,l.lanname::text,jsonb_build_object('owner',"
                "pg_get_userbyid(l.lanowner),'acl',coalesce((SELECT jsonb_agg(x::text "
                "ORDER BY x::text) FROM unnest(l.lanacl) x),'[]'::jsonb),'trusted',"
                "l.lanpltrusted) FROM pg_language l UNION ALL "
                "SELECT 'default-acl'::text,(pg_get_userbyid(d.defaclrole)||'|'||"
                "coalesce(n.nspname,'')||'|'||d.defaclobjtype::text)::text,jsonb_build_object("
                "'acl',coalesce((SELECT jsonb_agg(x::text ORDER BY x::text) "
                "FROM unnest(d.defaclacl) x),'[]'::jsonb)) "
                "FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace "
                "UNION ALL SELECT 'column-acl'::text,"
                "(n.nspname||'.'||c.relname||'.'||a.attname)::text,"
                "jsonb_build_object('number',a.attnum,'acl',(SELECT jsonb_agg(x::text "
                "ORDER BY x::text) FROM unnest(a.attacl) x)) FROM pg_attribute a "
                "JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE a.attnum>0 AND NOT a.attisdropped AND a.attacl IS NOT NULL "
                "AND n.nspname!~'^pg_(toast_)?temp_[0-9]+$'"
                ") objects ORDER BY kind,identity",
            ).splitlines()
            if (
                self._psql_scalar(
                    database,
                    "SELECT count(*) FROM pg_extension WHERE extname='age'",
                )
                == "1"
            ):
                object_rows.extend(
                    self._psql_scalar(
                        database,
                        "SELECT jsonb_build_array(kind,identity,value)::text FROM ("
                        "SELECT 'age-graph'::text AS kind,g.name::text AS identity,"
                        "jsonb_build_object('name',g.name::text,'namespace',"
                        "coalesce(n.nspname,'!missing')) AS value FROM ag_catalog.ag_graph g "
                        "LEFT JOIN pg_namespace n ON n.oid=g.namespace::oid UNION ALL "
                        "SELECT 'age-label'::text,"
                        "(coalesce(g.name::text,'!missing')||'|'||l.name::text)::text,"
                        "jsonb_build_object('graph',coalesce(g.name::text,'!missing'),'name',"
                        "l.name::text,'kind',l.kind::text,'relation',coalesce(c.relname,'!missing'),"
                        "'sequence',coalesce(s.relname,'!missing')) FROM ag_catalog.ag_label l "
                        "LEFT JOIN ag_catalog.ag_graph g ON g.graphid=l.graph "
                        "LEFT JOIN pg_class c ON c.oid=l.relation::oid LEFT JOIN pg_class s "
                        "ON s.relnamespace=g.namespace::oid AND s.relname::text=l.seq_name::text "
                        "AND s.relkind='S') objects ORDER BY kind,identity",
                    ).splitlines()
                )
            self._record_schema_objects(
                database=database,
                object_rows=object_rows,
                objects=objects,
            )
        return _DatabaseSnapshot(relations=relations, sequences=sequences, objects=objects)

    async def inventory(self) -> dict[str, Inventory]:
        """Enumerate every known plaintext-bearing platform and DBOS 2.26 surface."""
        from ne.runtime.legacy_transport_migration import Inventory

        def inspect(database: str) -> Inventory:
            required = (
                {
                    "ne_agent_session_messages",
                    "ne_agent_conversation_turns",
                }
                if database == "neutron"
                else {
                    "workflow_status",
                    "operation_outputs",
                    "notifications",
                    "streams",
                    "workflow_events",
                    "workflow_events_history",
                }
            )
            table_schema = "public" if database == "neutron" else "dbos"
            shape = set(
                self._psql_scalar(
                    database,
                    "SELECT table_name FROM information_schema.tables "
                    f"WHERE table_schema='{table_schema}' ORDER BY table_name",
                ).splitlines()
            )
            if not required <= shape:
                raise click.ClickException(f"unsupported {database} schema shape")
            schema = "platform-v1" if database == "neutron" else "dbos-2.26.0"
            counts = {
                name: int(self._psql_scalar(database, sql))
                for name, sql in _PLAINTEXT_SURFACES[database].items()
            }
            if database == "ne_dbos":
                legacy = int(
                    self._psql_scalar(
                        database,
                        "SELECT count(*) FROM dbos.workflow_status WHERE name='chat.turn' "
                        "AND COALESCE(attributes->>'kind','') <> 'legacy_transport_retired'",
                    )
                )
                unknown = int(
                    self._psql_scalar(
                        database,
                        "SELECT count(*) FROM dbos.workflow_status WHERE name='chat.turn' "
                        "AND attributes IS NOT NULL "
                        "AND COALESCE(attributes->>'kind','') NOT IN "
                        "('legacy_transport_retired','protected_ref')",
                    )
                )
            else:
                legacy = int(
                    self._psql_scalar(
                        database,
                        "SELECT (SELECT count(*) FROM ne_agent_session_messages "
                        "WHERE legacy_transport_retired IS NULL "
                        "AND protected_content_ref IS NULL) + "
                        "(SELECT count(*) FROM ne_agent_conversation_turns "
                        "WHERE protected_input_ref IS NULL OR protected_terminal_ref IS NULL)",
                    )
                )
                unknown = int(
                    self._psql_scalar(
                        database,
                        "SELECT count(*) FROM ne_agent_session_messages "
                        "WHERE legacy_transport_retired IS NOT NULL AND NOT ("
                        "legacy_transport_retired IS TRUE "
                        f"AND legacy_transport_generation='{_GENERATION}' "
                        f"AND legacy_transport_reason='{_OWNERLESS_RETIREMENT_REASON}' "
                        "AND length(legacy_transport_fence)=64)",
                    )
                )
            return Inventory(database, schema, legacy, counts, unknown)

        neutron, dbos = await asyncio.gather(
            asyncio.to_thread(inspect, "neutron"), asyncio.to_thread(inspect, "ne_dbos")
        )
        return {"neutron": neutron, "ne_dbos": dbos}

    def _transport_table_contract(self, database: str, table_name: str) -> bool:
        expected_contracts: dict[str, dict[str, object]] = {
            "ne_agent_transport_retirement": {
                "columns": [
                    ["fence", "character varying(64)", True, None],
                    ["generation", "character varying", True, None],
                    ["backup_id_hash", "character varying(64)", True, None],
                    ["manifest_sha256", "character varying(64)", True, None],
                    ["kek_fingerprint", "character varying", True, None],
                ],
                "constraints": [
                    ["n", "NOT NULL backup_id_hash"],
                    ["n", "NOT NULL fence"],
                    ["n", "NOT NULL generation"],
                    ["n", "NOT NULL kek_fingerprint"],
                    ["n", "NOT NULL manifest_sha256"],
                    ["p", "PRIMARY KEY (fence)"],
                ],
            },
            "ne_agent_transport_generation": {
                "columns": [
                    ["singleton", "boolean", True, "true"],
                    ["generation", "character varying", True, None],
                    ["image_digest", "character varying(71)", True, None],
                ],
                "constraints": [
                    ["c", "CHECK (singleton)"],
                    ["n", "NOT NULL generation"],
                    ["n", "NOT NULL image_digest"],
                    ["n", "NOT NULL singleton"],
                    ["p", "PRIMARY KEY (singleton)"],
                ],
            },
        }
        expected = expected_contracts.get(table_name)
        if expected is None:
            raise ValueError("protected transport table contract is unknown")
        raw = self._psql_scalar(
            database,
            "SELECT jsonb_build_object("
            "'columns',(SELECT COALESCE(jsonb_agg(jsonb_build_array(a.attname,"
            "format_type(a.atttypid,a.atttypmod),a.attnotnull,"
            "pg_get_expr(d.adbin,d.adrelid)) ORDER BY a.attnum),'[]'::jsonb) "
            "FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid "
            "AND d.adnum=a.attnum WHERE a.attrelid=c.oid AND a.attnum>0 "
            "AND NOT a.attisdropped),"
            "'constraints',(SELECT COALESCE(jsonb_agg(jsonb_build_array(x.contype::text,"
            "pg_get_constraintdef(x.oid,true)) ORDER BY x.contype::text,"
            "pg_get_constraintdef(x.oid,true)),'[]'::jsonb) FROM pg_constraint x "
            "WHERE x.conrelid=c.oid),'owner',pg_get_userbyid(c.relowner),'acl',c.relacl,"
            "'column_acl',(SELECT COALESCE(jsonb_agg(a.attacl ORDER BY a.attnum) "
            "FILTER (WHERE a.attacl IS NOT NULL),'[]'::jsonb) FROM pg_attribute a "
            "WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped),"
            "'triggers',(SELECT count(*) FROM pg_trigger t WHERE t.tgrelid=c.oid "
            "AND NOT t.tgisinternal),'policies',(SELECT count(*) FROM pg_policy p "
            "WHERE p.polrelid=c.oid),'rules',(SELECT count(*) FROM pg_rewrite r "
            "WHERE r.ev_class=c.oid AND r.rulename<>'_RETURN'),'rls',c.relrowsecurity,"
            "'force_rls',c.relforcerowsecurity,'options',c.reloptions,"
            "'current_user',current_user)::text "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relname='"
            + table_name
            + "' AND c.relkind='r' AND c.relpersistence='p'",
        )
        if not raw:
            return False
        try:
            contract = json.loads(raw)
        except json.JSONDecodeError as error:
            raise click.ClickException("protected transport table contract is malformed") from error
        current_owner = contract.get("current_user")
        if not isinstance(current_owner, str) or not current_owner:
            raise click.ClickException("protected transport table contract is foreign")
        if contract != {
            **expected,
            "owner": current_owner,
            "acl": None,
            "column_acl": [],
            "triggers": 0,
            "policies": 0,
            "rules": 0,
            "rls": False,
            "force_rls": False,
            "options": None,
            "current_user": current_owner,
        }:
            raise click.ClickException("protected transport table contract is foreign")
        return True

    async def _replay_inspection(
        self,
        *,
        baseline: Mapping[str, object] | None = None,
        retirement_fence: str | None = None,
        successor_invariant_sha256: str | None = None,
        capture_successor_candidate: bool = False,
    ) -> _ReplayInspection:
        """Inspect exact cutover replay state, retaining private comparison detail.

        This is deliberately private to the fixed Pilot cutover.  The projection
        normalizes only rows selected by the production migration and only the
        columns that migration owns.  Every other row and every untouched column
        remains in the invariant digest.  Membership is carried only as
        domain-separated SHA-256 values, never as customer identifiers or bytes.
        """

        def canonical(value: object) -> bytes:
            return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

        def digest(domain: str, value: object) -> str:
            return hashlib.sha256(domain.encode() + b"\0" + canonical(value)).hexdigest()

        def inspect() -> _ReplayInspection:
            from ne.runtime.private_payloads import (
                CONVERSATION_TURN_TARGET_KIND,
                KeyEnvelopeError,
                PrivatePayloadDomain,
                PrivatePayloadEnvelope,
                ProtectedContentStatus,
                RunPrivatePayloadCipher,
                StaticPrivatePayloadKeys,
                conversation_turn_content_binding,
                protected_payload_aad_binding,
                protected_target_hash,
            )

            if baseline is not None and (
                set(baseline) != {"schema", "invariant_sha256", "members"}
                or baseline.get("schema") != "ne-pilot-transport-replay-projection-v1"
                or not isinstance(baseline.get("invariant_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", str(baseline["invariant_sha256"])) is None
                or not isinstance(baseline.get("members"), dict)
            ):
                raise click.ClickException("protected transport replay projection is foreign")
            prior_members: dict[str, object] = (
                {} if baseline is None else dict(cast(dict[str, object], baseline["members"]))
            )
            surface_specs = {
                "session": (
                    "neutron",
                    "public",
                    "ne_agent_session_messages",
                    ("id",),
                    {
                        "payload",
                        "principal_id",
                        "trace_id",
                        "content_status",
                        "protected_content_ref",
                        "protected_content_hash",
                        "legacy_transport_retired",
                        "legacy_transport_generation",
                        "legacy_transport_reason",
                        "legacy_transport_fence",
                    },
                ),
                "conversation": (
                    "neutron",
                    "public",
                    "ne_agent_conversation_turns",
                    ("id",),
                    {
                        "private_context",
                        "served_output",
                        "citation_refs",
                        "served_citations",
                        "served_verified",
                        "served_grounded",
                        "input_status",
                        "protected_input_ref",
                        "protected_input_hash",
                        "terminal_status",
                        "protected_terminal_ref",
                        "protected_terminal_hash",
                    },
                ),
                "workflow": (
                    "ne_dbos",
                    "dbos",
                    "workflow_status",
                    ("workflow_uuid",),
                    {
                        "status",
                        "inputs",
                        "output",
                        "error",
                        "authenticated_user",
                        "authenticated_roles",
                        "assumed_role",
                        "attributes",
                        "serialization",
                    },
                ),
                "operation": (
                    "ne_dbos",
                    "dbos",
                    "operation_outputs",
                    ("workflow_uuid", "function_id"),
                    {"output", "error", "serialization"},
                ),
                "notification": (
                    "ne_dbos",
                    "dbos",
                    "notifications",
                    ("message_uuid",),
                    {"message", "serialization"},
                ),
                "stream": (
                    "ne_dbos",
                    "dbos",
                    "streams",
                    ("workflow_uuid", "key", "offset"),
                    {"value", "serialization"},
                ),
                "event": (
                    "ne_dbos",
                    "dbos",
                    "workflow_events",
                    ("workflow_uuid", "key"),
                    {"value", "serialization"},
                ),
                "history": (
                    "ne_dbos",
                    "dbos",
                    "workflow_events_history",
                    ("workflow_uuid", "function_id", "key"),
                    {"value", "serialization"},
                ),
            }
            member_names = {*surface_specs, "protected"}
            if baseline is not None and set(prior_members) != member_names:
                raise click.ClickException("protected transport replay projection is foreign")
            for name in member_names:
                value = cast(object, prior_members.get(name, {}))
                required_member_fields = (
                    {
                        "immutable_sha256",
                        "pre_sha256",
                        "input_status_sha256",
                        "input_body_sha256",
                        "terminal_status_sha256",
                        "terminal_body_sha256",
                    }
                    if name == "conversation"
                    else {"immutable_sha256", "pre_sha256"}
                )
                if not isinstance(value, dict) or any(
                    not isinstance(key, str)
                    or re.fullmatch(r"[0-9a-f]{64}", key) is None
                    or not isinstance(item, dict)
                    or set(item) != required_member_fields
                    or any(
                        not isinstance(item[field], str)
                        or re.fullmatch(r"[0-9a-f]{64}", item[field]) is None
                        for field in item
                    )
                    for key, item in value.items()
                ):
                    raise click.ClickException("protected transport replay projection is foreign")
            transport_tables = {
                ("neutron", "public", "ne_agent_transport_retirement"),
                ("neutron", "public", "ne_agent_transport_generation"),
                ("ne_dbos", "public", "ne_agent_transport_generation"),
            }
            schemas = []
            invariant: list[object] = []
            members: dict[str, dict[str, object]] = {
                name: dict(cast(dict[str, object], prior_members.get(name, {})))
                for name in member_names
            }
            observed = {name: set() for name in member_names}
            state = {name: {"pre": 0, "post": 0} for name in surface_specs}

            protected_table_columns = {
                "attachment_id",
                "attempt_id",
                "binding_hash",
                "cipher_version",
                "content_status",
                "conversation_id",
                "created_at",
                "encrypted_dek",
                "key_id",
                "key_nonce",
                "owner_principal_id",
                "payload_ciphertext",
                "payload_nonce",
                "plaintext_hash",
                "run_id",
                "stamp_set_hash",
                "target_binding",
                "target_hash",
                "target_kind",
                "turn_content_id",
                "turn_id",
                "turn_sequence",
            }
            legacy_candidate_columns = {
                "ne_agent_session_messages": {
                    "content_status",
                    "legacy_transport_fence",
                    "legacy_transport_generation",
                    "legacy_transport_reason",
                    "legacy_transport_retired",
                    "protected_content_hash",
                    "protected_content_ref",
                },
                "ne_agent_conversation_turns": {
                    "input_status",
                    "protected_input_hash",
                    "protected_input_ref",
                    "protected_terminal_hash",
                    "protected_terminal_ref",
                    "terminal_status",
                },
            }

            def columns(table_name: str) -> set[str]:
                raw = self._psql_scalar(
                    "neutron",
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='"
                    + table_name
                    + "' ORDER BY ordinal_position",
                ).splitlines()
                if len(raw) != len(set(raw)) or any(
                    re.fullmatch(r"[a-z_][a-z0-9_]*", item) is None for item in raw
                ):
                    raise click.ClickException("protected transport candidate schema is foreign")
                return set(raw)

            protected_observed = columns("ne_agent_protected_turn_contents")
            legacy_observed = {
                table_name: columns(table_name) & expected
                for table_name, expected in legacy_candidate_columns.items()
            }
            historical_candidate_absence = not protected_observed and all(
                not observed for observed in legacy_observed.values()
            )
            current_candidate_schema = protected_observed == protected_table_columns and all(
                legacy_observed[table_name] == expected
                for table_name, expected in legacy_candidate_columns.items()
            )
            if not historical_candidate_absence and not current_candidate_schema:
                raise click.ClickException("protected transport candidate schema is foreign")
            invariant.append(
                [
                    "protected-transport-candidate-schema",
                    "historical-absent-v1" if historical_candidate_absence else "current-v1",
                ]
            )

            def rows(database: str, schema_name: str, table_name: str) -> list[dict[str, object]]:
                raw = self._psql_scalar(
                    database,
                    f'SELECT to_jsonb(t)::text FROM "{schema_name}"."{table_name}" t '
                    "ORDER BY to_jsonb(t)::text",
                ).splitlines()
                values = [json.loads(item) for item in raw if item]
                if any(not isinstance(item, dict) for item in values):
                    raise click.ClickException("protected transport replay row is malformed")
                return values

            loaded: dict[tuple[str, str, str], list[dict[str, object]]] = {}
            for _, (database, schema_name, table_name, _, _) in surface_specs.items():
                loaded.setdefault(
                    (database, schema_name, table_name),
                    rows(database, schema_name, table_name),
                )
            protected_rows = (
                []
                if historical_candidate_absence
                else rows("neutron", "public", "ne_agent_protected_turn_contents")
            )

            for database, _, table_name in sorted(transport_tables):
                self._transport_table_contract(database, table_name)
                invariant.append(["transport-contract", database, table_name])
            selected_workflows = {
                str(row.get("workflow_uuid"))
                for row in loaded[("ne_dbos", "dbos", "workflow_status")]
                if row.get("name") == "chat.turn"
            }

            def selected(name: str, row: Mapping[str, object]) -> bool:
                if name == "session":
                    return (
                        row.get("principal_id") is None
                        and row.get("legacy_transport_retired") is None
                        and row.get("content_status") is None
                        and row.get("protected_content_ref") is None
                        and row.get("protected_content_hash") is None
                    )
                if name == "conversation":
                    return (
                        row.get("protected_input_ref") is None
                        or row.get("protected_terminal_ref") is None
                        or row.get("private_context") != ""
                        or row.get("served_output") != ""
                        or row.get("served_citations") not in ("", "[]")
                    )
                if name == "workflow":
                    return row.get("name") == "chat.turn"
                workflow = row.get(
                    "destination_uuid" if name == "notification" else "workflow_uuid"
                )
                return str(workflow) in selected_workflows

            def post_valid(name: str, row: Mapping[str, object]) -> bool:
                wire = (
                    None
                    if retirement_fence is None
                    else json.loads(_RETIRED_WIRE % retirement_fence)
                )
                if name == "session":
                    return (
                        retirement_fence is not None
                        and row.get("payload") == ""
                        and all(
                            row.get(field) is None
                            for field in (
                                "principal_id",
                                "trace_id",
                                "content_status",
                                "protected_content_ref",
                                "protected_content_hash",
                            )
                        )
                        and row.get("legacy_transport_retired") is True
                        and row.get("legacy_transport_generation") == _GENERATION
                        and row.get("legacy_transport_reason") == _OWNERLESS_RETIREMENT_REASON
                        and row.get("legacy_transport_fence") == retirement_fence
                    )
                if name == "conversation":
                    return (
                        row.get("private_context") == ""
                        and row.get("served_output") == ""
                        and row.get("citation_refs") == "[]"
                        and row.get("served_citations") == "[]"
                        and row.get("served_verified") == ""
                        and row.get("served_grounded") == ""
                        and row.get("input_status") in {"source_free", "legacy_unavailable"}
                        and row.get("terminal_status") in {"source_free", "legacy_unavailable"}
                        and all(
                            isinstance(row.get(field), str) and bool(row.get(field))
                            for field in (
                                "protected_input_ref",
                                "protected_input_hash",
                                "protected_terminal_ref",
                                "protected_terminal_hash",
                            )
                        )
                    )
                if name == "workflow":
                    return (
                        retirement_fence is not None
                        and row.get("status") == "CANCELLED"
                        and all(
                            row.get(field) is None
                            for field in (
                                "inputs",
                                "output",
                                "error",
                                "authenticated_user",
                                "authenticated_roles",
                                "assumed_role",
                            )
                        )
                        and row.get("attributes") == wire
                        and row.get("serialization") == "legacy_transport_retired"
                    )
                if name == "operation":
                    return (
                        row.get("output") is None
                        and row.get("error") is None
                        and row.get("serialization") == "legacy_transport_retired"
                    )
                value_field = "message" if name == "notification" else "value"
                return (
                    wire is not None
                    and row.get(value_field) == json.dumps(wire, separators=(",", ":"))
                    and row.get("serialization") == "legacy_transport_retired"
                )

            conversation_rows: list[Mapping[str, object]] = []

            for database in ("neutron", "ne_dbos"):
                schema_dump = self._schema_dump(database)
                schemas.append([database, hashlib.sha256(schema_dump.encode()).hexdigest()])
                table_rows = self._psql_scalar(
                    database,
                    "SELECT n.nspname||'|'||c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r' "
                    "AND n.nspname NOT IN ('pg_catalog','information_schema') "
                    "AND n.nspname!~'^pg_(toast_)?temp_[0-9]+$' ORDER BY 1",
                ).splitlines()
                for identity in table_rows:
                    parts = identity.split("|")
                    if len(parts) != 2 or any(
                        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) is None for part in parts
                    ):
                        raise click.ClickException(
                            "protected transport replay relation identity is unsupported"
                        )
                    schema_name, table_name = parts
                    table_key = (database, schema_name, table_name)
                    if table_key in transport_tables:
                        continue
                    if table_key == (
                        "neutron",
                        "public",
                        "ne_agent_protected_turn_contents",
                    ):
                        continue
                    spec_name = next(
                        (name for name, spec in surface_specs.items() if spec[:3] == table_key),
                        None,
                    )
                    if spec_name is None:
                        for row in loaded.get(table_key, rows(database, schema_name, table_name)):
                            invariant.append([database, schema_name, table_name, row])
                        continue
                    _, _, _, key_fields, mutable_fields = surface_specs[spec_name]
                    for row in loaded[table_key]:
                        member_hash = digest(
                            f"transport-member:{spec_name}",
                            [row.get(field) for field in key_fields],
                        )
                        owns = (
                            selected(spec_name, row)
                            if baseline is None
                            else member_hash in members[spec_name]
                        )
                        if not owns:
                            invariant.append([database, schema_name, table_name, row])
                            continue
                        immutable = {
                            key: value for key, value in row.items() if key not in mutable_fields
                        }
                        mutable_value = {key: row.get(key) for key in sorted(mutable_fields)}
                        immutable_hash = digest(f"transport-immutable:{spec_name}", immutable)
                        pre_hash = digest(f"transport-pre:{spec_name}", mutable_value)
                        if baseline is None:
                            member: dict[str, str] = {
                                "immutable_sha256": immutable_hash,
                                "pre_sha256": pre_hash,
                            }
                            if spec_name == "conversation":
                                for slot, (status, payload) in zip(
                                    ("input", "terminal"),
                                    _legacy_conversation_payloads(row),
                                    strict=True,
                                ):
                                    member[f"{slot}_status_sha256"] = digest(
                                        f"transport-expected:{slot}:status", status.value
                                    )
                                    member[f"{slot}_body_sha256"] = digest(
                                        f"transport-expected:{slot}:body", payload.hex()
                                    )
                            members[spec_name][member_hash] = member
                            state[spec_name]["pre"] += 1
                        else:
                            expected = cast(dict[str, str], members[spec_name][member_hash])
                            if immutable_hash != expected["immutable_sha256"]:
                                raise click.ClickException(
                                    "protected transport replay invariant changed"
                                )
                            if pre_hash == expected["pre_sha256"]:
                                state[spec_name]["pre"] += 1
                            elif post_valid(spec_name, row):
                                state[spec_name]["post"] += 1
                            else:
                                raise click.ClickException(
                                    "protected transport replay state is foreign"
                                )
                        observed[spec_name].add(member_hash)
                        invariant.append(
                            [database, schema_name, table_name, member_hash, immutable_hash]
                        )
                        if spec_name == "conversation":
                            conversation_rows.append(row)

            if baseline is not None and any(
                observed[name] != set(members[name]) for name in surface_specs
            ):
                raise click.ClickException("protected transport replay membership changed")

            allowed_protected: dict[str, tuple[Mapping[str, object], str, dict[str, str]]] = {}
            protected_by_ref = {str(row.get("turn_content_id")): row for row in protected_rows}
            if len(protected_by_ref) != len(protected_rows):
                raise click.ClickException("protected transport replay protected row is foreign")
            for turn in conversation_rows:
                turn_member_hash = digest("transport-member:conversation", [turn.get("id")])
                expected_turn = cast(
                    dict[str, str] | None, members["conversation"].get(turn_member_hash)
                )
                if expected_turn is None:
                    raise click.ClickException(
                        "protected transport replay conversation membership is foreign"
                    )
                for slot in ("input", "terminal"):
                    binding = conversation_turn_content_binding(
                        conversation_id=str(turn.get("conversation_id")),
                        attachment_id=str(turn.get("attachment_id")),
                        attempt_id=str(turn.get("attempt_id")),
                        slot=slot,
                    )
                    target = protected_target_hash(
                        PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                        str(turn.get("owner_principal_id")),
                        CONVERSATION_TURN_TARGET_KIND,
                        binding,
                    )
                    if target in allowed_protected:
                        raise click.ClickException(
                            "protected transport replay protected target is duplicated"
                        )
                    allowed_protected[target] = (turn, slot, expected_turn)
            for row in protected_rows:
                key_hash = digest(
                    "transport-member:protected",
                    [row.get("target_kind"), row.get("target_hash")],
                )
                full_hash = digest("transport-protected-row", row)
                if baseline is None:
                    members["protected"][key_hash] = {
                        "immutable_sha256": full_hash,
                        "pre_sha256": full_hash,
                    }
                    continue
                expected = cast(dict[str, str] | None, members["protected"].get(key_hash))
                if expected is not None:
                    if full_hash != expected["immutable_sha256"]:
                        raise click.ClickException(
                            "protected transport replay protected row changed"
                        )
                    observed["protected"].add(key_hash)
                    continue
                target = str(row.get("target_hash"))
                admitted = allowed_protected.get(target)
                if admitted is None:
                    raise click.ClickException(
                        "protected transport replay added a foreign protected row"
                    )
                turn, slot, expected_turn = admitted
                binding = conversation_turn_content_binding(
                    conversation_id=str(turn.get("conversation_id")),
                    attachment_id=str(turn.get("attachment_id")),
                    attempt_id=str(turn.get("attempt_id")),
                    slot=slot,
                )
                ref_field = f"protected_{slot}_ref"
                hash_field = f"protected_{slot}_hash"
                status_field = f"{slot}_status"
                if (
                    row.get("target_kind") != CONVERSATION_TURN_TARGET_KIND
                    or row.get("target_binding") != list(binding)
                    or row.get("owner_principal_id") != turn.get("owner_principal_id")
                    or row.get("run_id") is not None
                    or any(
                        row.get(field) is not None
                        for field in (
                            "conversation_id",
                            "turn_id",
                            "turn_sequence",
                            "attachment_id",
                            "attempt_id",
                        )
                    )
                    or row.get("content_status") not in {"source_free", "legacy_unavailable"}
                    or not isinstance(row.get("plaintext_hash"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", str(row.get("plaintext_hash"))) is None
                    or (
                        turn.get(ref_field) is not None
                        and (
                            turn.get(ref_field) != row.get("turn_content_id")
                            or turn.get(hash_field) != row.get("plaintext_hash")
                            or turn.get(status_field) != row.get("content_status")
                        )
                    )
                ):
                    raise click.ClickException(
                        "protected transport replay protected row is foreign"
                    )
                if self._private_payload_key is None:
                    raise click.ClickException("protected transport replay key is unavailable")
                key_id, key = self._private_payload_key
                status = ProtectedContentStatus(str(row["content_status"]))
                cipher_version = row["cipher_version"]
                if not isinstance(cipher_version, int):
                    raise click.ClickException(
                        "protected transport replay protected row is foreign"
                    )
                try:
                    recovered = RunPrivatePayloadCipher(
                        StaticPrivatePayloadKeys(current_key_id=key_id, keys={key_id: key})
                    ).materialize_for_target(
                        PrivatePayloadEnvelope(
                            cipher_version=cipher_version,
                            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                            row_id=str(row["turn_content_id"]),
                            owner_principal_id=str(row["owner_principal_id"]),
                            binding_hash=str(row["binding_hash"]),
                            plaintext_hash=str(row["plaintext_hash"]),
                            key_id=str(row["key_id"]),
                            payload_nonce=str(row["payload_nonce"]),
                            payload_ciphertext=str(row["payload_ciphertext"]),
                            key_nonce=str(row["key_nonce"]),
                            encrypted_dek=str(row["encrypted_dek"]),
                        ),
                        domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                        row_id=str(row["turn_content_id"]),
                        owner_principal_id=str(row["owner_principal_id"]),
                        binding=protected_payload_aad_binding(
                            target_kind=CONVERSATION_TURN_TARGET_KIND,
                            target_binding=binding,
                            content_status=status,
                        ),
                    )
                except (KeyEnvelopeError, KeyError, TypeError, ValueError) as error:
                    raise click.ClickException(
                        "protected transport replay protected row is not authentic"
                    ) from error
                if not hmac.compare_digest(
                    hashlib.sha256(recovered).hexdigest(), str(row["plaintext_hash"])
                ):
                    raise click.ClickException(
                        "protected transport replay protected row is not authentic"
                    )
                if (
                    digest(f"transport-expected:{slot}:status", status.value)
                    != expected_turn[f"{slot}_status_sha256"]
                    or digest(f"transport-expected:{slot}:body", recovered.hex())
                    != expected_turn[f"{slot}_body_sha256"]
                ):
                    raise click.ClickException(
                        "protected transport replay protected row is not authentic"
                    )
            if baseline is not None and observed["protected"] != set(members["protected"]):
                raise click.ClickException("protected transport replay protected row disappeared")
            if baseline is not None:
                for turn in conversation_rows:
                    for slot in ("input", "terminal"):
                        reference = turn.get(f"protected_{slot}_ref")
                        if reference is None:
                            continue
                        protected = protected_by_ref.get(str(reference))
                        binding = conversation_turn_content_binding(
                            conversation_id=str(turn.get("conversation_id")),
                            attachment_id=str(turn.get("attachment_id")),
                            attempt_id=str(turn.get("attempt_id")),
                            slot=slot,
                        )
                        expected_target = protected_target_hash(
                            PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                            str(turn.get("owner_principal_id")),
                            CONVERSATION_TURN_TARGET_KIND,
                            binding,
                        )
                        if (
                            protected is None
                            or protected.get("target_hash") != expected_target
                            or protected.get("plaintext_hash") != turn.get(f"protected_{slot}_hash")
                            or protected.get("content_status") != turn.get(f"{slot}_status")
                        ):
                            raise click.ClickException(
                                "protected transport replay protected binding is foreign"
                            )
            sequence_projection = []
            for database in ("neutron", "ne_dbos"):
                sequence_projection.extend(
                    [database, row]
                    for row in self._psql_scalar(
                        database,
                        "SELECT n.nspname||'|'||c.relname||'|'||s.seqstart||'|'||s.seqmin||'|'||"
                        "s.seqmax||'|'||s.seqincrement||'|'||s.seqcycle||'|'||s.seqcache FROM "
                        "pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid JOIN pg_namespace n "
                        "ON n.oid=c.relnamespace WHERE n.nspname!~'^pg_(toast_)?temp_[0-9]+$' "
                        "ORDER BY 1",
                    ).splitlines()
                )
            invariant_hash = digest(
                "transport-replay-invariant-v1",
                [schemas, sorted(invariant, key=canonical), sequence_projection],
            )
            admitted_invariant = (
                baseline["invariant_sha256"]
                if successor_invariant_sha256 is None and baseline is not None
                else successor_invariant_sha256
            )
            if (
                not capture_successor_candidate
                and admitted_invariant is not None
                and invariant_hash != admitted_invariant
            ):
                raise click.ClickException("protected transport replay invariant changed")
            return _ReplayInspection(
                projection={
                    "schema": "ne-pilot-transport-replay-projection-v1",
                    "invariant_sha256": invariant_hash,
                    "members": members,
                    "state": state,
                },
                invariant_rows=tuple(invariant),
                schemas=tuple(schemas),
                sequences=tuple(sequence_projection),
            )

        return await asyncio.to_thread(inspect)

    async def _replay_projection(
        self,
        *,
        baseline: Mapping[str, object] | None = None,
        retirement_fence: str | None = None,
        _successor_invariant_sha256: str | None = None,
    ) -> dict[str, object]:
        """Return the byte-compatible v1 content-closed replay projection."""

        inspection = await self._replay_inspection(
            baseline=baseline,
            retirement_fence=retirement_fence,
            successor_invariant_sha256=_successor_invariant_sha256,
        )
        return inspection.projection

    async def retire(self, receipt: BackupReceipt) -> str:
        """Commit the content-free rollback/retirement fence before rewriting rows."""
        if not receipt.verified or receipt.databases != _DATABASES:
            raise click.ClickException("retirement requires the exact verified backup receipt")
        material = "\x00".join(
            (
                receipt.backup_id,
                receipt.manifest_sha256,
                receipt.kek_fingerprint,
                _GENERATION,
            )
        )
        fence = hashlib.sha256(material.encode()).hexdigest()
        backup_id_hash = hashlib.sha256(receipt.backup_id.encode()).hexdigest()
        contract = await asyncio.to_thread(
            self._transport_table_contract, "neutron", "ne_agent_transport_retirement"
        )
        create_sql = (
            "CREATE TABLE IF NOT EXISTS ne_agent_transport_retirement ("
            "fence VARCHAR(64) PRIMARY KEY, generation VARCHAR NOT NULL, "
            "backup_id_hash VARCHAR(64) NOT NULL, manifest_sha256 VARCHAR(64) NOT NULL, "
            "kek_fingerprint VARCHAR NOT NULL);SELECT 'created';"
        )
        if not contract:
            created = await asyncio.to_thread(self._psql_scalar, "neutron", create_sql)
            if created != "created":
                raise click.ClickException("retirement fence table was not created exactly")
        contract = await asyncio.to_thread(
            self._transport_table_contract, "neutron", "ne_agent_transport_retirement"
        )
        if not contract:
            raise click.ClickException("retirement fence table contract is unavailable")
        insert_sql = (
            "INSERT INTO ne_agent_transport_retirement "
            "(fence,generation,backup_id_hash,manifest_sha256,kek_fingerprint) VALUES "
            f"('{fence}','{_GENERATION}','{backup_id_hash}',"
            f"'{receipt.manifest_sha256}','{receipt.kek_fingerprint}') "
            "ON CONFLICT (fence) DO NOTHING;"
            "SELECT fence FROM ne_agent_transport_retirement "
            f"WHERE fence='{fence}' AND generation='{_GENERATION}' "
            f"AND backup_id_hash='{backup_id_hash}' "
            f"AND manifest_sha256='{receipt.manifest_sha256}' "
            f"AND kek_fingerprint='{receipt.kek_fingerprint}';"
        )
        observed = await asyncio.to_thread(self._psql_scalar, "neutron", insert_sql)
        contract = await asyncio.to_thread(
            self._transport_table_contract, "neutron", "ne_agent_transport_retirement"
        )
        if observed != fence or not contract:
            raise click.ClickException("retirement fence did not bind the verified backup")
        return fence

    async def migrate(self, retirement_fence: str) -> None:
        """Protect Conversation rows, then retire remaining legacy transport surfaces."""
        if len(retirement_fence) != 64 or any(
            char not in "0123456789abcdef" for char in retirement_fence
        ):
            raise ValueError("retirement fence must be lowercase sha256")
        await self._migrate_conversations()
        wire = _RETIRED_WIRE % retirement_fence
        platform_sql = (
            "UPDATE ne_agent_session_messages SET payload='', principal_id=NULL, trace_id=NULL, "
            "content_status=NULL, protected_content_ref=NULL, protected_content_hash=NULL, "
            "legacy_transport_retired=TRUE, "
            f"legacy_transport_generation='{_GENERATION}', "
            f"legacy_transport_reason='{_OWNERLESS_RETIREMENT_REASON}', "
            f"legacy_transport_fence='{retirement_fence}' "
            "WHERE principal_id IS NULL AND legacy_transport_retired IS NULL "
            "AND content_status IS NULL AND protected_content_ref IS NULL "
            "AND protected_content_hash IS NULL; SELECT 'ok';"
        )
        dbos_sql = (
            "BEGIN;"
            "WITH retired AS (SELECT workflow_uuid FROM dbos.workflow_status "
            "WHERE name='chat.turn') UPDATE dbos.operation_outputs o SET output=NULL,error=NULL,"
            "serialization='legacy_transport_retired' FROM retired r "
            "WHERE o.workflow_uuid=r.workflow_uuid;"
            "WITH retired AS (SELECT workflow_uuid FROM dbos.workflow_status "
            f"WHERE name='chat.turn') UPDATE dbos.streams s SET value='{wire}',"
            "serialization='legacy_transport_retired' FROM retired r "
            "WHERE s.workflow_uuid=r.workflow_uuid;"
            "WITH retired AS (SELECT workflow_uuid FROM dbos.workflow_status "
            f"WHERE name='chat.turn') UPDATE dbos.notifications n SET message='{wire}',"
            "serialization='legacy_transport_retired' FROM retired r "
            "WHERE n.destination_uuid=r.workflow_uuid;"
            "WITH retired AS (SELECT workflow_uuid FROM dbos.workflow_status "
            f"WHERE name='chat.turn') UPDATE dbos.workflow_events e SET value='{wire}',"
            "serialization='legacy_transport_retired' FROM retired r "
            "WHERE e.workflow_uuid=r.workflow_uuid;"
            "WITH retired AS (SELECT workflow_uuid FROM dbos.workflow_status "
            f"WHERE name='chat.turn') UPDATE dbos.workflow_events_history e SET value='{wire}',"
            "serialization='legacy_transport_retired' FROM retired r "
            "WHERE e.workflow_uuid=r.workflow_uuid;"
            "UPDATE dbos.workflow_status SET status='CANCELLED', inputs=NULL, output=NULL, "
            "error=NULL, authenticated_user=NULL, authenticated_roles=NULL, assumed_role=NULL, "
            f"attributes='{wire}'::json, serialization='legacy_transport_retired' "
            "WHERE name='chat.turn'; COMMIT; SELECT 'ok';"
        )
        platform, dbos = await asyncio.gather(
            asyncio.to_thread(self._psql_scalar, "neutron", platform_sql),
            asyncio.to_thread(self._psql_scalar, "ne_dbos", dbos_sql),
        )
        if platform != "ok" or dbos != "ok":
            raise click.ClickException("protected transport migration did not commit exactly")

    async def _migrate_conversations(self) -> None:
        """Create both protected payloads before erasing each Conversation envelope."""

        import sqlalchemy as sa

        from ne.audit.engine import create_engine_with_timeouts
        from ne.runtime.private_payloads import (
            CONVERSATION_TURN_TARGET_KIND,
            PrivatePayloadDomain,
            RunPrivatePayloadCipher,
            StaticPrivatePayloadKeys,
            conversation_turn_content_binding,
        )
        from ne.runtime.run_store import AgentRunStore
        from ne.runtime.store import ne_agent_conversation_turns

        database_url = self._platform_database_url
        if database_url is None:
            from ne.cli.pilotcfg import _resolve_required_secrets_async

            resolved = await _resolve_required_secrets_async(self.bundle)
            database_url = sa.URL.create(
                "postgresql+psycopg",
                username="ne",
                password=resolved["NE_PILOT_POSTGRES_PASSWORD"],
                host="127.0.0.1",
                port=5432,
                database=self._physical_database_name("neutron"),
            ).render_as_string(hide_password=False)
        key = self._private_payload_key
        if key is None:
            version = await _read_version(_store_from_bundle(self.bundle))
            if version is None:
                raise click.ClickException("protected payload key is unavailable")
            key = _require_fields(version)
        key_id, kek = key
        engine = create_engine_with_timeouts(database_url)
        cipher = RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id=key_id, keys={key_id: kek})
        )
        runs = AgentRunStore(engine, cipher)
        turn = ne_agent_conversation_turns

        async def protect(
            *,
            slot: str,
            status: ProtectedContentStatus,
            body: bytes,
            owner: str,
            conversation_id: str,
            attachment_id: str,
            attempt_id: str,
        ):
            return await runs.claim_protected_payload(
                domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                run_id=None,
                owner_principal_id=owner,
                target_kind=CONVERSATION_TURN_TARGET_KIND,
                target_binding=conversation_turn_content_binding(
                    conversation_id=conversation_id,
                    attachment_id=attachment_id,
                    attempt_id=attempt_id,
                    slot=slot,
                ),
                content_status=status,
                plaintext=body,
            )

        try:
            with engine.connect() as conn:
                rows = list(
                    conn.execute(
                        sa.select(turn).where(
                            sa.or_(
                                turn.c.protected_input_ref.is_(None),
                                turn.c.protected_terminal_ref.is_(None),
                                turn.c.private_context != "",
                                turn.c.served_output != "",
                                turn.c.served_citations.not_in(("", "[]")),
                            )
                        )
                    ).mappings()
                )
            for row in rows:
                (input_status, input_bytes), (terminal_status, terminal_bytes) = (
                    _legacy_conversation_payloads(dict(row))
                )

                binding = {
                    "owner": row["owner_principal_id"],
                    "conversation_id": row["conversation_id"],
                    "attachment_id": row["attachment_id"],
                    "attempt_id": row["attempt_id"],
                }
                input_ref = await protect(
                    slot="input", status=input_status, body=input_bytes, **binding
                )
                terminal_ref = await protect(
                    slot="terminal", status=terminal_status, body=terminal_bytes, **binding
                )
                await runs.migrate_conversation_turn_to_protected_refs(
                    conversation_id=row["conversation_id"],
                    owner_principal_id=row["owner_principal_id"],
                    run_id=None,
                    attachment_id=row["attachment_id"],
                    attempt_id=row["attempt_id"],
                    input_status=input_status.value,
                    protected_input_ref=input_ref.row_id,
                    protected_input_hash=input_ref.plaintext_hash,
                    terminal_status=terminal_status.value,
                    protected_terminal_ref=terminal_ref.row_id,
                    protected_terminal_hash=terminal_ref.plaintext_hash,
                )
        finally:
            engine.dispose()

    async def fence(self, generation: str, image_digest: str) -> None:
        """Install the exact admitted generation/image record in both databases."""
        if generation != _GENERATION:
            raise ValueError("protected transport generation is invalid")
        _require_image_digest(image_digest)
        schema_sql = (
            "CREATE TABLE IF NOT EXISTS ne_agent_transport_generation ("
            "singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton), "
            "generation VARCHAR NOT NULL, image_digest VARCHAR(71) NOT NULL);SELECT 'created';"
        )
        initial_contracts = await asyncio.gather(
            *(
                asyncio.to_thread(
                    self._transport_table_contract,
                    database,
                    "ne_agent_transport_generation",
                )
                for database in ("neutron", "ne_dbos")
            )
        )
        for database, exists in zip(("neutron", "ne_dbos"), initial_contracts, strict=True):
            if (
                not exists
                and await asyncio.to_thread(self._psql_scalar, database, schema_sql) != "created"
            ):
                raise click.ClickException("database generation table was not created exactly")
        contracts = await asyncio.gather(
            *(
                asyncio.to_thread(
                    self._transport_table_contract,
                    database,
                    "ne_agent_transport_generation",
                )
                for database in ("neutron", "ne_dbos")
            )
        )
        if contracts != [True, True]:
            raise click.ClickException("database generation table contract is unavailable")
        insert_sql = (
            "INSERT INTO ne_agent_transport_generation(singleton,generation,image_digest) "
            f"VALUES(TRUE,'{generation}','{image_digest}') ON CONFLICT (singleton) DO UPDATE "
            "SET generation=EXCLUDED.generation,image_digest=EXCLUDED.image_digest "
            "WHERE ne_agent_transport_generation.generation=EXCLUDED.generation "
            "AND ne_agent_transport_generation.image_digest=EXCLUDED.image_digest;"
            "SELECT generation||'|'||image_digest FROM ne_agent_transport_generation "
            "WHERE singleton IS TRUE;"
        )
        expected = f"{generation}|{image_digest}"
        values = await asyncio.gather(
            asyncio.to_thread(self._psql_scalar, "neutron", insert_sql),
            asyncio.to_thread(self._psql_scalar, "ne_dbos", insert_sql),
        )
        contracts = await asyncio.gather(
            *(
                asyncio.to_thread(
                    self._transport_table_contract,
                    database,
                    "ne_agent_transport_generation",
                )
                for database in ("neutron", "ne_dbos")
            )
        )
        if values != [expected, expected] or contracts != [True, True]:
            raise click.ClickException("database generation fence does not match the image")

    def _require_root(self, root: str) -> str:
        if root not in _ROOTS:
            raise ValueError("protected cutover root is invalid")
        return root

    def _container_id(self, root: str) -> str:
        output = self._run("ps", "--quiet", self._require_root(root), capture=True)
        if not output or "\n" in output:
            raise click.ClickException(f"expected exactly one running {root} container")
        return output

    async def start(self, root: str, generation: str, image_digest: str) -> None:
        self._require_root(root)
        if generation != _GENERATION:
            raise ValueError("protected transport generation is invalid")
        _require_image_digest(image_digest)
        await asyncio.to_thread(
            self._run,
            "up",
            "--detach",
            "--no-deps",
            "--wait",
            "--pull",
            "never",
            root,
        )
        container = await asyncio.to_thread(self._container_id, root)
        actual_image = await asyncio.to_thread(
            subprocess.run,
            ["docker", "inspect", "--format", "{{.Image}}", container],
            check=True,
            text=True,
            capture_output=True,
        )
        if actual_image.stdout.strip() != image_digest:
            await self.scale_zero()
            raise click.ClickException("started root does not use the admitted image digest")

    async def ready(self, root: str, generation: str, image_digest: str) -> bool:
        container = await asyncio.to_thread(self._container_id, self._require_root(root))
        generation_attestation = f"NE_AGENT_TRANSPORT_GENERATION={_GENERATION}"
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "docker",
                "inspect",
                "--format",
                "{{println .State.Health.Status}}{{println .Image}}"
                "{{range .Config.Env}}"
                f'{{{{if eq . "{generation_attestation}"}}}}{{{{println .}}}}{{{{end}}}}'
                "{{end}}",
                container,
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        lines = result.stdout.splitlines()
        return (
            len(lines) >= 2
            and lines[0].strip() == "healthy"
            and lines[1].strip() == image_digest
            and f"NE_AGENT_TRANSPORT_GENERATION={generation}" in lines[2:]
        )

    async def restart_root(self, root: str, generation: str, image_digest: str) -> None:
        """Force-recreate one exact root without permitting dependency replacement."""
        self._require_root(root)
        if generation != _GENERATION:
            raise ValueError("protected transport generation is invalid")
        _require_image_digest(image_digest)
        await asyncio.to_thread(
            self._run,
            "up",
            "--detach",
            "--no-deps",
            "--wait",
            "--pull",
            "never",
            "--force-recreate",
            root,
        )

    def root_identity(
        self,
        root: str,
        *,
        generation: str,
        image_digest: str,
        compose_hash: str,
    ) -> dict[str, str]:
        """Return one closed, ready, exact-Compose application-root identity."""
        self._require_root(root)
        if generation != _GENERATION:
            raise ValueError("protected transport generation is invalid")
        _require_image_digest(image_digest)
        _require_sha256(compose_hash, "Compose config hash")
        container = self._container_id(root)
        generation_attestation = f"NE_AGENT_TRANSPORT_GENERATION={_GENERATION}"
        template = (
            "{{println .Id}}{{println .State.Running}}{{println .State.Health.Status}}"
            "{{println .State.Pid}}{{println .State.StartedAt}}{{println .Image}}"
            '{{println (index .Config.Labels "com.docker.compose.config-hash")}}'
            '{{println (index .Config.Labels "com.docker.compose.project.config_files")}}'
            '{{println (index .Config.Labels "com.docker.compose.project.working_dir")}}'
            "{{range .Config.Env}}"
            f'{{{{if eq . "{generation_attestation}"}}}}{{{{println .}}}}{{{{end}}}}'
            "{{end}}"
        )
        result = subprocess.run(
            ["docker", "inspect", "--format", template, container],
            check=True,
            text=True,
            capture_output=True,
        )
        lines = result.stdout.splitlines()
        if len(lines) != 10:
            raise click.ClickException(f"{root} readiness identity is unavailable")
        (
            inspected_container,
            running,
            health,
            raw_pid,
            started_at,
            actual_image,
            actual_compose_hash,
            actual_compose_files,
            actual_working_dir,
            actual_generation,
        ) = lines
        expected_compose_files = ",".join(str(path) for path in self.compose_files)
        if (
            re.fullmatch(r"[0-9a-f]{64}", inspected_container) is None
            or inspected_container != container
            or running != "true"
            or health != "healthy"
            or not raw_pid.isascii()
            or not raw_pid.isdecimal()
            or int(raw_pid) < 1
            or not started_at
            or actual_image != image_digest
            or actual_compose_hash != compose_hash
            or actual_compose_files != expected_compose_files
            or actual_working_dir != str(self.compose_working_dir)
            or actual_generation != generation_attestation
            or self._container_id(root) != container
        ):
            raise click.ClickException(f"{root} does not match exact ready Compose identity")
        process_epoch = hashlib.sha256(
            b"ne.agent-transport.process-epoch.v1\x00"
            + raw_pid.encode("ascii")
            + b"\x00"
            + started_at.encode("utf-8")
        ).hexdigest()
        return {
            "compose_config_hash": compose_hash,
            "container_id": container,
            "generation": generation,
            "image_digest": image_digest,
            "process_epoch": process_epoch,
        }


def _historical_cutover_projection(
    *, phase: object, data: Mapping[str, str], version: int
) -> dict[str, str | int]:
    """Validate and close the historical cutover journal to receipt-safe metadata."""
    from ne.runtime.legacy_transport_migration import CutoverRefused, Phase

    required = {
        "phase",
        "backup_id",
        "manifest_sha256",
        "kek_fingerprint",
        "fence",
        "generation",
        "image_digest",
        "inventory_sha256",
    }
    if phase is not Phase.COMPLETE or version != len(Phase) or set(data) != required:
        raise CutoverRefused("restart requires one complete historical Pilot cutover journal")
    if data["phase"] != Phase.COMPLETE.value:
        raise CutoverRefused("restart requires one complete historical Pilot cutover journal")
    if data["generation"] != _GENERATION:
        raise CutoverRefused("historical Pilot cutover names a foreign generation")
    try:
        _require_image_digest(data["image_digest"])
        _require_sha256(data["manifest_sha256"], "cutover manifest hash")
        _require_sha256(data["fence"], "cutover retirement fence")
        _require_sha256(data["inventory_sha256"], "cutover inventory hash")
    except ValueError as exc:
        raise CutoverRefused("historical Pilot cutover receipt is malformed") from exc
    if not data["backup_id"] or re.fullmatch(r"[0-9a-f]{16}", data["kek_fingerprint"]) is None:
        raise CutoverRefused("historical Pilot cutover receipt is malformed")
    return {
        "backup_id_sha256": hashlib.sha256(data["backup_id"].encode()).hexdigest(),
        "generation": data["generation"],
        "cutover_image_digest": data["image_digest"],
        "inventory_sha256": data["inventory_sha256"],
        "journal_path": "pilot/agent-transport-cutover",
        "journal_version": version,
        "kek_fingerprint": data["kek_fingerprint"],
        "manifest_sha256": data["manifest_sha256"],
        "phase": data["phase"],
        "retirement_fence": data["fence"],
    }


def _canonical_restart_receipt(body: Mapping[str, object]) -> dict[str, object]:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return {**body, "receipt_hash": hashlib.sha256(canonical).hexdigest()}


def _validate_root_identity(
    identity: Mapping[str, str],
    *,
    root: str,
    generation: str,
    image_digest: str,
    compose_hash: str,
) -> dict[str, str]:
    keys = {
        "compose_config_hash",
        "container_id",
        "generation",
        "image_digest",
        "process_epoch",
    }
    if set(identity) != keys:
        raise click.ClickException(f"{root} returned a non-canonical root identity")
    if (
        identity["generation"] != generation
        or identity["image_digest"] != image_digest
        or identity["compose_config_hash"] != compose_hash
        or re.fullmatch(r"[0-9a-f]{64}", identity["container_id"]) is None
    ):
        raise click.ClickException(f"{root} returned a foreign root identity")
    try:
        _require_sha256(identity["process_epoch"], f"{root} process epoch")
    except ValueError as exc:
        raise click.ClickException(f"{root} returned a foreign root identity") from exc
    return dict(identity)


async def _controlled_restart_receipt(
    port: FixedPilotCutoverPort,
    *,
    journal: Any,
    expected_image_digest: str,
) -> dict[str, object]:
    """Force a worker-first Pilot restart and bind both new root epochs."""
    from ne.runtime.legacy_transport_migration import CutoverRefused

    _require_image_digest(expected_image_digest)
    restarted = False
    await port.lock()
    try:
        phase, journal_data, journal_version = await journal.latest()
        historical = _historical_cutover_projection(
            phase=phase,
            data=journal_data,
            version=journal_version,
        )
        expected_shape = await asyncio.to_thread(port.compose_shape)
        if set(expected_shape) != set(_RESTART_ORDER):
            raise CutoverRefused("restart Compose shape does not name both exact roots")
        pre_roots = {
            root: _validate_root_identity(
                await asyncio.to_thread(
                    port.root_identity,
                    root,
                    generation=_GENERATION,
                    image_digest=expected_image_digest,
                    compose_hash=expected_shape[root],
                ),
                root=root,
                generation=_GENERATION,
                image_digest=expected_image_digest,
                compose_hash=expected_shape[root],
            )
            for root in _RESTART_ORDER
        }
        post_roots: dict[str, dict[str, str]] = {}
        for root in _RESTART_ORDER:
            restarted = True
            await port.restart_root(root, _GENERATION, expected_image_digest)
            if await asyncio.to_thread(port.compose_shape) != expected_shape:
                raise CutoverRefused("restart Compose shape drifted during root replacement")
            if not await port.ready(root, _GENERATION, expected_image_digest):
                raise CutoverRefused(f"{root} did not attest restart readiness")
            post = _validate_root_identity(
                await asyncio.to_thread(
                    port.root_identity,
                    root,
                    generation=_GENERATION,
                    image_digest=expected_image_digest,
                    compose_hash=expected_shape[root],
                ),
                root=root,
                generation=_GENERATION,
                image_digest=expected_image_digest,
                compose_hash=expected_shape[root],
            )
            pre = pre_roots[root]
            if (
                post["container_id"] == pre["container_id"]
                or post["process_epoch"] == pre["process_epoch"]
            ):
                raise CutoverRefused(f"{root} did not enter a new container and process epoch")
            post_roots[root] = post
        if await asyncio.to_thread(port.compose_shape) != expected_shape:
            raise CutoverRefused("restart Compose shape drifted before receipt emission")
        for root in _RESTART_ORDER:
            final = _validate_root_identity(
                await asyncio.to_thread(
                    port.root_identity,
                    root,
                    generation=_GENERATION,
                    image_digest=expected_image_digest,
                    compose_hash=expected_shape[root],
                ),
                root=root,
                generation=_GENERATION,
                image_digest=expected_image_digest,
                compose_hash=expected_shape[root],
            )
            if final != post_roots[root]:
                raise CutoverRefused(f"{root} identity changed before restart receipt emission")
        body: dict[str, object] = {
            "historical_cutover": historical,
            "post_roots": post_roots,
            "pre_roots": pre_roots,
            "schema": _RESTART_RECEIPT_SCHEMA,
        }
        return _canonical_restart_receipt(body)
    except Exception:
        if restarted:
            await port.scale_zero()
        raise
    finally:
        await port.unlock()


@click.group(name="agent-transport")
def agent_transport() -> None:
    """Operate the protected Agent transport generation."""


def _fixed_port(
    *,
    bundle: Path,
    backup_dest: Path,
    compose_files: tuple[Path, ...],
    compose_working_dir: Path | None,
) -> FixedPilotCutoverPort:
    return FixedPilotCutoverPort(
        bundle=bundle.resolve(),
        backup_dest=backup_dest.resolve(),
        compose_files=compose_files,
        compose_working_dir=compose_working_dir,
    )


@agent_transport.command("preflight")
@click.option(
    "--bundle", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option("--target", required=True, type=click.Choice(["pilot"], case_sensitive=True))
@click.option("--backup-dest", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--compose-file",
    "compose_files",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--compose-working-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
def preflight_command(
    bundle: Path,
    target: str,
    backup_dest: Path,
    compose_files: tuple[Path, ...],
    compose_working_dir: Path | None,
) -> None:
    """Validate the fixed Pilot adapter before the rollback boundary moves."""
    del target
    port = _fixed_port(
        bundle=bundle,
        backup_dest=backup_dest,
        compose_files=compose_files,
        compose_working_dir=compose_working_dir,
    )
    port.preflight()
    click.echo("protected transport cutover preflight complete")


@agent_transport.command("cutover")
@click.option(
    "--bundle", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option("--target", required=True, type=click.Choice(["pilot"], case_sensitive=True))
@click.option("--expected-image-digest", required=True, type=str)
@click.option("--backup-dest", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--compose-file",
    "compose_files",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--compose-working-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
def cutover_command(
    bundle: Path,
    target: str,
    expected_image_digest: str,
    backup_dest: Path,
    compose_files: tuple[Path, ...],
    compose_working_dir: Path | None,
) -> None:
    """Quiesce, back up, migrate and fence one populated Pilot installation."""
    del target
    receipt = asyncio.run(
        _cutover_command_async(
            bundle=bundle.resolve(),
            backup_dest=backup_dest.resolve(),
            expected_image_digest=expected_image_digest,
            compose_files=compose_files,
            compose_working_dir=compose_working_dir,
        )
    )
    click.echo(
        f"protected transport cutover complete backup_id={receipt.backup_id} "
        f"manifest_sha256={receipt.manifest_sha256}"
    )


@agent_transport.command("restart-receipt")
@click.option(
    "--bundle", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option("--target", required=True, type=click.Choice(["pilot"], case_sensitive=True))
@click.option("--expected-image-digest", required=True, type=str)
@click.option("--backup-dest", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--compose-file",
    "compose_files",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--compose-working-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
def restart_receipt_command(
    bundle: Path,
    target: str,
    expected_image_digest: str,
    backup_dest: Path,
    compose_files: tuple[Path, ...],
    compose_working_dir: Path | None,
) -> None:
    """Force-recreate both protected Pilot roots and emit their bound receipt."""
    del target
    receipt = asyncio.run(
        _restart_receipt_command_async(
            bundle=bundle.resolve(),
            backup_dest=backup_dest.resolve(),
            expected_image_digest=expected_image_digest,
            compose_files=compose_files,
            compose_working_dir=compose_working_dir,
        )
    )
    click.echo(json.dumps(receipt, sort_keys=True, separators=(",", ":")))


async def _restart_receipt_command_async(
    *,
    bundle: Path,
    backup_dest: Path,
    expected_image_digest: str,
    compose_files: tuple[Path, ...] = (),
    compose_working_dir: Path | None = None,
    _inherited_lock_fd: int | None = None,
) -> dict[str, object]:
    from ne.runtime.legacy_transport_migration import SecretStoreJournal

    store = _store_from_bundle(bundle)
    port = FixedPilotCutoverPort(
        bundle=bundle,
        backup_dest=backup_dest,
        compose_files=compose_files,
        compose_working_dir=compose_working_dir,
        _inherited_lock_fd=_inherited_lock_fd,
    )
    return await _controlled_restart_receipt(
        port,
        journal=SecretStoreJournal(store),
        expected_image_digest=expected_image_digest,
    )


async def _cutover_command_async(
    *,
    bundle: Path,
    backup_dest: Path,
    expected_image_digest: str,
    compose_files: tuple[Path, ...] = (),
    compose_working_dir: Path | None = None,
    _inherited_lock_fd: int | None = None,
) -> BackupReceipt:
    from ne.runtime.legacy_transport_migration import SecretStoreJournal, cutover

    store = _store_from_bundle(bundle)
    port = FixedPilotCutoverPort(
        bundle=bundle,
        backup_dest=backup_dest,
        compose_files=compose_files,
        compose_working_dir=compose_working_dir,
        _inherited_lock_fd=_inherited_lock_fd,
    )
    await asyncio.to_thread(port.preflight)
    return await cutover(
        port,
        image_digest=expected_image_digest,
        schemas={"neutron": "platform-v1", "ne_dbos": "dbos-2.26.0"},
        journal=SecretStoreJournal(store),
    )
