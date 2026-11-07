# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Isolated live-Postgres proof for the Fixed Pilot cutover adapter."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from ne.api.turn_wire import ProtectedTerminalPayload
from ne.audit.engine import create_engine_with_timeouts
from ne.cli.agent_transport import (
    _PLAINTEXT_SURFACES,
    _TARGET_RUNTIME_SCHEMA_COMMAND,
    FixedPilotCutoverPort,
)
from ne.runtime.legacy_transport_migration import BackupReceipt
from ne.runtime.private_payloads import (
    CONVERSATION_TURN_TARGET_KIND,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    RunPrivatePayloadCipher,
    StaticPrivatePayloadKeys,
    conversation_turn_content_binding,
    protected_payload_aad_binding,
)
from ne.runtime.run_store import AgentRunStore, ensure_run_schema
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema


def _required_postgres_url() -> str:
    value = os.environ.get("NE_TEST_POSTGRES_URL")
    if not value:
        pytest.skip("NE_TEST_POSTGRES_URL is required for the isolated adapter proof")
    return value


def test_historical_runtime_schema_is_replayed_before_conversation_migration_on_postgres(
    tmp_path: Path,
) -> None:
    admin_url = sa.make_url(_required_postgres_url())
    database = "ne_cutover_runtime_first_" + uuid.uuid4().hex
    admin = create_engine_with_timeouts(admin_url.render_as_string(hide_password=False))
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    isolated_url = admin_url.set(database=database).render_as_string(hide_password=False)
    engine = create_engine_with_timeouts(isolated_url)
    protected_columns = (
        "input_status",
        "protected_input_ref",
        "protected_input_hash",
        "terminal_status",
        "protected_terminal_ref",
        "protected_terminal_hash",
    )
    try:
        ensure_runtime_schema(engine)
        with engine.begin() as conn:
            for column in protected_columns:
                conn.exec_driver_sql(
                    f'ALTER TABLE ne_agent_conversation_turns DROP COLUMN "{column}"'
                )

        bundle = tmp_path / "bundle"
        runtime = bundle / "runtime"
        runtime.mkdir(parents=True)
        pilot_env = runtime / "pilot.env"
        pilot_env.write_text("NE_PILOT_POSTGRES_PASSWORD=secret\n", encoding="utf-8")
        pilot_env.chmod(0o600)
        stage = tmp_path / "stage"
        compose = stage / "deploy/compose/pilot/compose.yaml"
        compose.parent.mkdir(parents=True)
        compose.write_text("services: {}\n", encoding="utf-8")
        registry = stage / "deploy/agents/registry.toml"
        registry.parent.mkdir(parents=True)
        registry.write_text("schema_version = 1\ngeneration = 2\n", encoding="utf-8")
        skill = stage / "deploy/skills/meridian-hardship-draft/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# fixed skill\n", encoding="utf-8")
        port = FixedPilotCutoverPort(
            bundle=bundle,
            backup_dest=tmp_path / "backups",
            compose_files=(compose,),
            platform_database_url=isolated_url,
            private_payload_key=("k-test", b"k" * 32),
            database_urls={"neutron": isolated_url},
        )

        with pytest.raises(Exception, match="input_status"):
            asyncio.run(port._migrate_conversations())

        environment = dict(os.environ)
        environment["NE_RUNTIME_SESSION_DATABASE_URL"] = isolated_url
        environment["NE_BOOTSTRAP_SCHEMA_DATABASE"] = database
        replay = subprocess.run(
            [sys.executable, "-c", _TARGET_RUNTIME_SCHEMA_COMMAND],
            cwd=Path(__file__).parents[3],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert replay.returncode == 0, replay.stderr
        asyncio.run(port._migrate_conversations())
        assert {
            str(column["name"])
            for column in sa.inspect(engine).get_columns("ne_agent_conversation_turns")
        }.issuperset(protected_columns)
    finally:
        engine.dispose()
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:database AND pid <> pg_backend_pid()"
                ),
                {"database": database},
            )
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database}"')
        admin.dispose()


def test_schema_object_capture_refuses_a_conflicting_full_identity_duplicate() -> None:
    identity = "ag_catalog.age_vle(" + "ag_catalog.agtype," * 8 + "OUT edges ag_catalog.agtype)"
    objects: dict[tuple[str, str, str], str] = {}
    with pytest.raises(Exception, match="schema object is duplicated"):
        FixedPilotCutoverPort._record_schema_objects(
            database="neutron",
            object_rows=[
                json.dumps(["function", identity, {"source": "first"}]),
                json.dumps(["function", identity, {"source": "conflicting"}]),
            ],
            objects=objects,
        )


def test_replay_projection_admits_only_exact_historical_candidate_absence_on_postgres(
    tmp_path: Path,
) -> None:
    admin_url = sa.make_url(_required_postgres_url())
    database = "ne_cutover_historical_" + uuid.uuid4().hex
    dbos_database = database + "_dbos"
    admin = create_engine_with_timeouts(admin_url.render_as_string(hide_password=False))
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
        conn.exec_driver_sql(f'CREATE DATABASE "{dbos_database}"')
    isolated_url = admin_url.set(database=database).render_as_string(hide_password=False)
    dbos_url = admin_url.set(database=dbos_database).render_as_string(hide_password=False)
    engine = create_engine_with_timeouts(isolated_url)
    dbos_engine = create_engine_with_timeouts(dbos_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE ne_agent_session_messages (id BIGINT PRIMARY KEY, "
                "session_id TEXT NOT NULL, seq INTEGER NOT NULL, principal_id TEXT, "
                "trace_id TEXT, payload TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)"
            )
            conn.exec_driver_sql(
                "CREATE TABLE ne_agent_conversation_turns (id BIGINT PRIMARY KEY, "
                "conversation_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
                "owner_principal_id TEXT NOT NULL, department TEXT NOT NULL, "
                "agent_name TEXT NOT NULL, trace_id TEXT NOT NULL, attachment_id TEXT NOT NULL, "
                "attempt_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL, "
                "private_context TEXT NOT NULL, served_output TEXT NOT NULL, "
                "citation_refs TEXT NOT NULL, served_citations TEXT NOT NULL, "
                "served_verified TEXT NOT NULL, served_grounded TEXT NOT NULL, "
                "publication_state TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)"
            )
        with dbos_engine.begin() as conn:
            conn.exec_driver_sql("CREATE SCHEMA dbos")
            conn.exec_driver_sql(
                "CREATE TABLE dbos.workflow_status (workflow_uuid TEXT PRIMARY KEY, name TEXT)"
            )
            conn.exec_driver_sql(
                "CREATE TABLE dbos.operation_outputs (workflow_uuid TEXT, function_id INTEGER)"
            )
            conn.exec_driver_sql(
                "CREATE TABLE dbos.notifications (message_uuid TEXT, destination_uuid TEXT)"
            )
            conn.exec_driver_sql(
                'CREATE TABLE dbos.streams (workflow_uuid TEXT, "key" TEXT, "offset" INTEGER)'
            )
            conn.exec_driver_sql(
                'CREATE TABLE dbos.workflow_events (workflow_uuid TEXT, "key" TEXT)'
            )
            conn.exec_driver_sql(
                "CREATE TABLE dbos.workflow_events_history "
                '(workflow_uuid TEXT, function_id INTEGER, "key" TEXT)'
            )
        bundle = tmp_path / "bundle"
        runtime = bundle / "runtime"
        runtime.mkdir(parents=True)
        pilot_env = runtime / "pilot.env"
        pilot_env.write_text("NE_PILOT_POSTGRES_PASSWORD=secret\n", encoding="utf-8")
        pilot_env.chmod(0o600)
        stage = tmp_path / "stage"
        compose = stage / "deploy/compose/pilot/compose.yaml"
        compose.parent.mkdir(parents=True)
        compose.write_text("services: {}\n", encoding="utf-8")
        registry = stage / "deploy/agents/registry.toml"
        registry.parent.mkdir(parents=True)
        registry.write_text("schema_version = 1\ngeneration = 2\n", encoding="utf-8")
        skill = stage / "deploy/skills/meridian-hardship-draft/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# fixed skill\n", encoding="utf-8")
        port = FixedPilotCutoverPort(
            bundle=bundle,
            backup_dest=tmp_path / "backups",
            compose_files=(compose,),
            platform_database_url=isolated_url,
            private_payload_key=("k-test", b"k" * 32),
            database_urls={"neutron": isolated_url, "ne_dbos": dbos_url},
        )
        port._run = lambda *_args, capture=False: "closed-schema\n"  # type: ignore[method-assign]

        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE SCHEMA ag_catalog")
            conn.exec_driver_sql("CREATE TYPE ag_catalog.agtype AS (value text)")
            for input_count in (7, 8):
                arguments = ",".join(["ag_catalog.agtype"] * input_count)
                conn.exec_driver_sql(
                    "CREATE FUNCTION ag_catalog.age_vle("
                    f"{arguments},OUT edges ag_catalog.agtype) LANGUAGE sql IMMUTABLE "
                    "AS 'SELECT NULL::ag_catalog.agtype'"
                )

        snapshot = port._database_snapshot()
        assert ("neutron", "public", "ne_agent_session_messages") in snapshot.relations
        assert ("neutron", "schema", "public") in snapshot.objects
        assert ("ne_dbos", "schema", "dbos") in snapshot.objects
        assert ("neutron", "extension", "plpgsql") in snapshot.objects
        age_vle_identities = {
            identity
            for database_name, kind, identity in snapshot.objects
            if database_name == "neutron"
            and kind == "function"
            and identity.startswith("ag_catalog.age_vle(")
        }
        assert age_vle_identities == {
            "ag_catalog.age_vle(ag_catalog.agtype, ag_catalog.agtype, "
            "ag_catalog.agtype, ag_catalog.agtype, ag_catalog.agtype, "
            "ag_catalog.agtype, ag_catalog.agtype, OUT edges ag_catalog.agtype)",
            "ag_catalog.age_vle(ag_catalog.agtype, ag_catalog.agtype, "
            "ag_catalog.agtype, ag_catalog.agtype, ag_catalog.agtype, "
            "ag_catalog.agtype, ag_catalog.agtype, ag_catalog.agtype, "
            "OUT edges ag_catalog.agtype)",
        }
        assert all(len(identity) > 63 for identity in age_vle_identities)

        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE ne_transport_snapshot_probe ("
                "id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, value text)"
            )
            conn.exec_driver_sql("SELECT setval('ne_transport_snapshot_probe_id_seq',41,false)")
        sequence_before = port._database_snapshot()
        sequence_key = (
            "neutron",
            "public",
            "ne_transport_snapshot_probe_id_seq",
            "public.ne_transport_snapshot_probe",
        )
        assert sequence_key in sequence_before.sequences
        with engine.begin() as conn:
            conn.exec_driver_sql("SELECT setval('ne_transport_snapshot_probe_id_seq',41,true)")
        called = port._database_snapshot()
        assert called.sequences[sequence_key] != sequence_before.sequences[sequence_key]
        with engine.begin() as conn:
            conn.exec_driver_sql("GRANT SELECT ON ne_transport_snapshot_probe TO PUBLIC")
        authority = port._database_snapshot()
        relation_key = ("neutron", "public", "ne_transport_snapshot_probe")
        assert (
            authority.relations[relation_key].contract_sha256
            != called.relations[relation_key].contract_sha256
        )
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE ne_transport_snapshot_probe")
        assert port._database_snapshot() == snapshot

        historical = asyncio.run(port._replay_projection())
        members = cast(dict[str, object], historical["members"])
        state = cast(dict[str, object], historical["state"])
        assert members["protected"] == {}
        assert all(value == {"pre": 0, "post": 0} for value in state.values())
        baseline = {key: value for key, value in historical.items() if key != "state"}
        port._run = lambda *_args, capture=False: "closed-schema-reordered\n"  # type: ignore[method-assign]
        with pytest.raises(Exception, match="replay invariant changed"):
            asyncio.run(port._replay_inspection(baseline=baseline))
        semantic_predecessor = asyncio.run(
            port._replay_inspection(
                baseline=baseline,
                capture_successor_candidate=True,
            )
        )
        assert semantic_predecessor.projection["invariant_sha256"] != baseline["invariant_sha256"]
        assert semantic_predecessor.projection["members"] == baseline["members"]
        assert semantic_predecessor.projection["state"] == state
        assert port._database_snapshot() == snapshot

        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE ne_agent_session_messages ADD COLUMN content_status TEXT"
            )
        with pytest.raises(Exception, match="candidate schema is foreign"):
            asyncio.run(port._replay_projection())
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE ne_agent_session_messages DROP COLUMN content_status")
            for definition in (
                "content_status TEXT",
                "legacy_transport_fence VARCHAR(64)",
                "legacy_transport_generation TEXT",
                "legacy_transport_reason TEXT",
                "legacy_transport_retired BOOLEAN",
                "protected_content_hash VARCHAR(64)",
                "protected_content_ref TEXT",
            ):
                conn.exec_driver_sql(
                    f"ALTER TABLE ne_agent_session_messages ADD COLUMN {definition}"
                )
            for definition in (
                "input_status TEXT",
                "protected_input_hash VARCHAR(64)",
                "protected_input_ref TEXT",
                "protected_terminal_hash VARCHAR(64)",
                "protected_terminal_ref TEXT",
                "terminal_status TEXT",
            ):
                conn.exec_driver_sql(
                    f"ALTER TABLE ne_agent_conversation_turns ADD COLUMN {definition}"
                )
            conn.exec_driver_sql(
                "CREATE TABLE ne_agent_protected_turn_contents ("
                "turn_content_id TEXT PRIMARY KEY, run_id TEXT, owner_principal_id TEXT, "
                "target_kind TEXT, target_hash VARCHAR(64), target_binding JSON, "
                "conversation_id TEXT, turn_id TEXT, turn_sequence INTEGER, attachment_id TEXT, "
                "attempt_id TEXT, content_status TEXT, stamp_set_hash VARCHAR(64), "
                "created_at TIMESTAMPTZ, cipher_version INTEGER, plaintext_hash VARCHAR(64), "
                "binding_hash VARCHAR(64), key_id TEXT, payload_nonce TEXT, "
                "payload_ciphertext TEXT, key_nonce TEXT, encrypted_dek TEXT)"
            )

        current_with_state = asyncio.run(port._replay_projection())
        current = {key: value for key, value in current_with_state.items() if key != "state"}
        replay = asyncio.run(port._replay_projection(baseline=current))
        assert {key: value for key, value in replay.items() if key != "state"} == current
    finally:
        engine.dispose()
        dbos_engine.dispose()
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname IN (:database,:dbos_database) AND pid <> pg_backend_pid()"
                ),
                {"database": database, "dbos_database": dbos_database},
            )
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database}"')
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{dbos_database}"')
        admin.dispose()


def test_fixed_pilot_migrates_conversations_and_every_derived_surface_on_postgres(
    tmp_path: Path,
) -> None:
    admin_url = sa.make_url(_required_postgres_url())
    database = "ne_cutover_" + uuid.uuid4().hex
    dbos_database = database + "_dbos"
    admin = create_engine_with_timeouts(admin_url.render_as_string(hide_password=False))
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
        conn.exec_driver_sql(f'CREATE DATABASE "{dbos_database}"')
    isolated_url = admin_url.set(database=database).render_as_string(hide_password=False)
    dbos_url = admin_url.set(database=dbos_database).render_as_string(hide_password=False)
    engine = create_engine_with_timeouts(isolated_url)
    dbos_engine = create_engine_with_timeouts(dbos_url)
    try:
        ensure_runtime_schema(engine)
        ensure_run_schema(engine)
        with dbos_engine.begin() as conn:
            conn.exec_driver_sql("CREATE SCHEMA dbos")
            conn.exec_driver_sql(
                "CREATE TABLE dbos.workflow_status (workflow_uuid TEXT PRIMARY KEY, "
                "name TEXT, status TEXT, inputs JSONB, output JSONB, error TEXT, "
                "authenticated_user TEXT, authenticated_roles JSONB, assumed_role TEXT, "
                "attributes JSONB, serialization TEXT)"
            )
            conn.exec_driver_sql(
                "CREATE TABLE dbos.operation_outputs (workflow_uuid TEXT, output JSONB, "
                "error TEXT, serialization TEXT)"
            )
            conn.exec_driver_sql(
                "CREATE TABLE dbos.notifications (destination_uuid TEXT, message TEXT, "
                "serialization TEXT)"
            )
            for table in ("streams", "workflow_events", "workflow_events_history"):
                conn.exec_driver_sql(
                    f"CREATE TABLE dbos.{table} (workflow_uuid TEXT, value TEXT, "
                    "serialization TEXT)"
                )
            conn.exec_driver_sql(
                "INSERT INTO dbos.workflow_status VALUES "
                "('workflow-1','chat.turn','SUCCESS','{\"prompt\":\"PRIVATE\"}',"
                "'{\"output\":\"PRIVATE\"}','PRIVATE','user:ada','[\"eng\"]','eng',"
                "'{\"kind\":\"legacy\"}','json')"
            )
            conn.exec_driver_sql(
                "INSERT INTO dbos.operation_outputs VALUES "
                "('workflow-1','{\"output\":\"PRIVATE\"}','PRIVATE','json')"
            )
            conn.exec_driver_sql(
                "INSERT INTO dbos.notifications VALUES ('workflow-1','PRIVATE','json')"
            )
            for table in ("streams", "workflow_events", "workflow_events_history"):
                conn.exec_driver_sql(
                    f"INSERT INTO dbos.{table} VALUES ('workflow-1','PRIVATE','json')"
                )

        sessions = SqlAlchemySessionStore(engine)

        async def seed() -> tuple[str, str, str]:
            conversation = await sessions.create_conversation(
                "console-chat", "engineering", "user:ada"
            )
            attachment_id = "turn-live-pg"
            attempt_id = "attempt-live-pg"
            await sessions.append_conversation_turn(
                conversation.conversation_id,
                "user:ada",
                "engineering",
                "console-chat",
                [
                    ModelRequest(parts=[UserPromptPart(content="PRIVATE-PROMPT")]),
                    ModelResponse(parts=[TextPart(content="PRIVATE-ANSWER")], model_name="legacy"),
                ],
                served_output="PRIVATE-ANSWER",
                trace_id="a" * 32,
                attachment_id=attachment_id,
                attempt_id=attempt_id,
                citation_payloads=[
                    {
                        "chunk_id": "c1",
                        "document_id": "d1",
                        "source_system": "wiki",
                        "source_ref": "r1",
                    }
                ],
            )
            await sessions.publish_conversation_turn(
                conversation.conversation_id,
                "user:ada",
                attachment_id,
                attempt_id,
                ["c1"],
                citation_payloads=[
                    {
                        "chunk_id": "c1",
                        "document_id": "d1",
                        "source_system": "wiki",
                        "source_ref": "r1",
                    }
                ],
            )
            return conversation.conversation_id, attachment_id, attempt_id

        conversation_id, attachment_id, attempt_id = asyncio.run(seed())
        bundle = tmp_path / "bundle"
        runtime = bundle / "runtime"
        runtime.mkdir(parents=True)
        pilot_env = runtime / "pilot.env"
        pilot_env.write_text("NE_PILOT_POSTGRES_PASSWORD=secret\n", encoding="utf-8")
        pilot_env.chmod(0o600)
        stage = tmp_path / "stage"
        compose = stage / "deploy/compose/pilot/compose.yaml"
        compose.parent.mkdir(parents=True)
        compose.write_text("services: {}\n", encoding="utf-8")
        registry = stage / "deploy/agents/registry.toml"
        registry.parent.mkdir(parents=True)
        registry.write_text("schema_version = 1\ngeneration = 2\n", encoding="utf-8")
        skill = stage / "deploy/skills/meridian-hardship-draft/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# fixed skill\n", encoding="utf-8")
        port = FixedPilotCutoverPort(
            bundle=bundle,
            backup_dest=tmp_path / "backups",
            compose_files=(compose,),
            platform_database_url=isolated_url,
            private_payload_key=("k-test", b"k" * 32),
            database_urls={"neutron": isolated_url, "ne_dbos": dbos_url},
        )
        before = asyncio.run(port.inventory())
        assert set(before) == {"neutron", "ne_dbos"}
        assert set(before["neutron"].plaintext_by_surface) == set(_PLAINTEXT_SURFACES["neutron"])
        assert set(before["ne_dbos"].plaintext_by_surface) == set(_PLAINTEXT_SURFACES["ne_dbos"])
        assert before["neutron"].legacy_rows == 1
        assert before["ne_dbos"].legacy_rows == 1
        assert all(value > 0 for value in before["ne_dbos"].plaintext_by_surface.values())

        # Exercise the real adapter projection SQL against Postgres.  Only the
        # pg_dump transport is replaced because this isolated test uses direct
        # database URLs rather than a Compose-owned postgres service.
        port._run = lambda *_args, capture=False: "closed-schema\n"  # type: ignore[method-assign]
        baseline_with_state = asyncio.run(port._replay_projection())
        baseline = {key: value for key, value in baseline_with_state.items() if key != "state"}
        receipt = BackupReceipt(
            backup_id="backup-live-pg",
            manifest_sha256="b" * 64,
            kek_fingerprint="0123456789abcdef",
            databases=frozenset({"neutron", "ne_dbos"}),
            verified=True,
        )
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE ne_agent_protected_turn_contents DISABLE TRIGGER ALL")
            conn.exec_driver_sql(
                "CREATE TABLE ne_agent_transport_retirement ("
                "fence VARCHAR(64) PRIMARY KEY, generation VARCHAR NOT NULL, "
                "backup_id_hash VARCHAR(64) NOT NULL, manifest_sha256 VARCHAR(64) NOT NULL, "
                "kek_fingerprint VARCHAR NOT NULL)"
            )
        empty_effect = asyncio.run(port._replay_projection(baseline=baseline))
        assert empty_effect["invariant_sha256"] == baseline["invariant_sha256"]
        fence = asyncio.run(port.retire(receipt))
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE ne_agent_transport_retirement ADD COLUMN foreign_value TEXT"
            )
        with pytest.raises(Exception, match="table contract is foreign"):
            asyncio.run(port._replay_projection(baseline=baseline, retirement_fence=fence))
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE ne_agent_transport_retirement DROP COLUMN foreign_value"
            )
            conn.exec_driver_sql("GRANT SELECT ON ne_agent_transport_retirement TO PUBLIC")
        with pytest.raises(Exception, match="table contract is foreign"):
            asyncio.run(port._replay_projection(baseline=baseline, retirement_fence=fence))
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE ne_agent_transport_retirement")
        assert asyncio.run(port.retire(receipt)) == fence
        asyncio.run(port.migrate(fence))
        replay = asyncio.run(port._replay_projection(baseline=baseline, retirement_fence=fence))
        state = replay["state"]
        assert isinstance(state, dict)
        assert all(value["pre"] == 0 for value in state.values())
        after = asyncio.run(port.inventory())
        assert all(item.legacy_rows == 0 for item in after.values())
        assert all(item.unknown_wire_rows == 0 for item in after.values())
        assert all(
            count == 0
            for inventory in after.values()
            for count in inventory.plaintext_by_surface.values()
        )

        runs = AgentRunStore(
            engine,
            RunPrivatePayloadCipher(
                StaticPrivatePayloadKeys(current_key_id="k-test", keys={"k-test": b"k" * 32})
            ),
        )
        protected = asyncio.run(
            sessions.protected_conversation_turn(
                conversation_id,
                "user:ada",
                "engineering",
                "console-chat",
                attachment_id,
                attempt_id,
            )
        )
        assert protected is not None
        terminal = asyncio.run(
            runs.materialize_legacy_conversation_turn(
                owner_principal_id="user:ada",
                conversation_id=conversation_id,
                attachment_id=attachment_id,
                attempt_id=attempt_id,
                slot="terminal",
                protected_ref=protected.terminal.protected_ref,
                plaintext_hash=protected.terminal.plaintext_hash,
            )
        )
        assert ProtectedTerminalPayload.model_validate_json(terminal).output == "PRIVATE-ANSWER"

        # A fully authentic re-encryption of the wrong bytes, with every
        # mutable row/turn hash adjusted to match it, must still refuse against
        # the domain-separated pre-cutover slot membership.
        wrong = RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="k-test", keys={"k-test": b"k" * 32})
        ).encrypt_for_target(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            row_id=protected.terminal.protected_ref,
            owner_principal_id="user:ada",
            binding=protected_payload_aad_binding(
                target_kind=CONVERSATION_TURN_TARGET_KIND,
                target_binding=conversation_turn_content_binding(
                    conversation_id=conversation_id,
                    attachment_id=attachment_id,
                    attempt_id=attempt_id,
                    slot="terminal",
                ),
                content_status=ProtectedContentStatus.SOURCE_FREE,
            ),
            plaintext=b'{"output":"AUTHENTIC-BUT-WRONG"}',
        )
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE ne_agent_protected_turn_contents SET binding_hash=%s,plaintext_hash=%s,"
                "key_id=%s,payload_nonce=%s,payload_ciphertext=%s,key_nonce=%s,encrypted_dek=%s "
                "WHERE turn_content_id=%s",
                (
                    wrong.binding_hash,
                    wrong.plaintext_hash,
                    wrong.key_id,
                    wrong.payload_nonce,
                    wrong.payload_ciphertext,
                    wrong.key_nonce,
                    wrong.encrypted_dek,
                    protected.terminal.protected_ref,
                ),
            )
            conn.exec_driver_sql("ALTER TABLE ne_agent_protected_turn_contents ENABLE TRIGGER ALL")
            conn.exec_driver_sql(
                "UPDATE ne_agent_conversation_turns SET protected_terminal_hash=%s "
                "WHERE conversation_id=%s AND attachment_id=%s AND attempt_id=%s",
                (wrong.plaintext_hash, conversation_id, attachment_id, attempt_id),
            )
        with pytest.raises(Exception, match="not authentic"):
            asyncio.run(port._replay_projection(baseline=baseline, retirement_fence=fence))
    finally:
        engine.dispose()
        dbos_engine.dispose()
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname IN (:database,:dbos_database) AND pid <> pg_backend_pid()"
                ),
                {"database": database, "dbos_database": dbos_database},
            )
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database}"')
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{dbos_database}"')
        admin.dispose()
