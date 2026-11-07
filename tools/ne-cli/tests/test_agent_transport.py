# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused controls for the fixed Pilot protected-transport adapter."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import click
import pytest

from ne.cli.agent_transport import (
    _PLAINTEXT_SURFACES,
    FixedPilotCutoverPort,
    _controlled_restart_receipt,
    _create_verified_pilot_topology_backup,
    _ProtectedTransportPostgresView,
    _restart_receipt_command_async,
    agent_transport,
)
from ne.contracts.testing.memory import MemorySecretStore
from ne.runtime.legacy_transport_migration import (
    GENERATION,
    BackupReceipt,
    CutoverRefused,
    Phase,
    SecretStoreJournal,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE = REPO_ROOT / "tools/ne-cli/src/ne/cli/agent_transport.py"
IMAGE = "sha256:" + "a" * 64
CUTOVER_IMAGE = "sha256:" + "f" * 64


async def _journal(
    *,
    stop: Phase = Phase.COMPLETE,
    image: str = IMAGE,
    generation: str = GENERATION,
) -> SecretStoreJournal:
    journal = SecretStoreJournal(MemorySecretStore())
    phases = (
        (Phase.QUIESCED, {}),
        (
            Phase.BACKED_UP,
            {
                "backup_id": "backup-secret-identity",
                "manifest_sha256": "b" * 64,
                "kek_fingerprint": "c" * 16,
            },
        ),
        (Phase.RETIRED, {"fence": "d" * 64}),
        (Phase.MIGRATED, {"fence": "d" * 64}),
        (Phase.SCANNED, {"inventory_sha256": "e" * 64}),
        (Phase.FENCED, {"generation": generation, "image_digest": image}),
        (Phase.WORKER_READY, {"generation": generation, "image_digest": image}),
        (Phase.API_READY, {"generation": generation, "image_digest": image}),
        (Phase.COMPLETE, {"generation": generation, "image_digest": image}),
    )
    for phase, metadata in phases:
        await journal.append(phase, **metadata)
        if phase is stop:
            break
    return journal


class _RestartPort:
    def __init__(
        self,
        *,
        unchanged_root: str | None = None,
        unchanged_container_root: str | None = None,
        not_ready_root: str | None = None,
        drift_after_restart: bool = False,
        extra_identity_field: bool = False,
        foreign_post_field: tuple[str, str, str] | None = None,
    ) -> None:
        self.events: list[str] = []
        self.restarted = {"ne-worker": False, "ne-api": False}
        self.unchanged_root = unchanged_root
        self.unchanged_container_root = unchanged_container_root
        self.not_ready_root = not_ready_root
        self.drift_after_restart = drift_after_restart
        self.extra_identity_field = extra_identity_field
        self.foreign_post_field = foreign_post_field
        self.shape = {"ne-worker": "1" * 64, "ne-api": "2" * 64}

    async def lock(self) -> None:
        self.events.append("lock")

    async def unlock(self) -> None:
        self.events.append("unlock")

    def compose_shape(self) -> dict[str, str]:
        self.events.append("shape")
        if self.drift_after_restart and any(self.restarted.values()):
            return {**self.shape, "ne-api": "9" * 64}
        return dict(self.shape)

    def root_identity(self, root: str, **_kwargs: str) -> dict[str, str]:
        self.events.append(f"identity:{root}:{'post' if self.restarted[root] else 'pre'}")
        pre_containers = {"ne-worker": "3" * 64, "ne-api": "4" * 64}
        post_containers = {"ne-worker": "5" * 64, "ne-api": "6" * 64}
        pre_epochs = {"ne-worker": "7" * 64, "ne-api": "8" * 64}
        post_epochs = {"ne-worker": "a" * 64, "ne-api": "b" * 64}
        post = self.restarted[root]
        identity = {
            "compose_config_hash": self.shape[root],
            "container_id": (post_containers if post else pre_containers)[root],
            "generation": GENERATION,
            "image_digest": IMAGE,
            "process_epoch": (post_epochs if post else pre_epochs)[root],
        }
        if post and root == self.unchanged_root:
            identity["process_epoch"] = pre_epochs[root]
        if post and root == self.unchanged_container_root:
            identity["container_id"] = pre_containers[root]
        if post and self.foreign_post_field is not None and root == self.foreign_post_field[0]:
            identity[self.foreign_post_field[1]] = self.foreign_post_field[2]
        if self.extra_identity_field:
            identity["database_password"] = "secret-value"
        return identity

    async def restart_root(self, root: str, generation: str, image_digest: str) -> None:
        assert generation == GENERATION
        assert image_digest == IMAGE
        self.events.append(f"restart:{root}")
        self.restarted[root] = True

    async def ready(self, root: str, generation: str, image_digest: str) -> bool:
        assert generation == GENERATION
        assert image_digest == IMAGE
        self.events.append(f"ready:{root}")
        return root != self.not_ready_root

    async def scale_zero(self) -> None:
        self.events.append("scale-zero")


def _port(tmp_path: Path) -> FixedPilotCutoverPort:
    bundle = tmp_path / "bundle"
    stage = tmp_path / "stage"
    compose = stage / "deploy/compose/pilot/compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services:\n  ne-api: {}\n  ne-worker: {}\n", encoding="utf-8")
    registry = stage / "deploy/agents/registry.toml"
    registry.parent.mkdir(parents=True)
    registry.write_text("schema_version = 1\ngeneration = 2\n", encoding="utf-8")
    skill = stage / "deploy/skills/meridian-hardship-draft/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# fixed skill\n", encoding="utf-8")
    runtime = bundle / "runtime"
    runtime.mkdir(parents=True)
    pilot_env = runtime / "pilot.env"
    pilot_env.write_text("NE_PILOT_POSTGRES_PASSWORD=secret\n", encoding="utf-8")
    pilot_env.chmod(0o600)
    return FixedPilotCutoverPort(
        bundle=bundle,
        backup_dest=tmp_path / "backups",
        compose_files=(compose,),
        compose_working_dir=compose.parent,
    )


def test_fixed_adapter_binds_retirement_migration_and_fence_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port(tmp_path)
    calls: list[tuple[str, str]] = []

    def psql(database: str, sql: str) -> str:
        calls.append((database, sql))
        if "SELECT jsonb_build_object('columns'" in sql:
            if "ne_agent_transport_retirement" in sql:
                columns = [
                    ["fence", "character varying(64)", True, None],
                    ["generation", "character varying", True, None],
                    ["backup_id_hash", "character varying(64)", True, None],
                    ["manifest_sha256", "character varying(64)", True, None],
                    ["kek_fingerprint", "character varying", True, None],
                ]
                constraints = [
                    ["n", "NOT NULL backup_id_hash"],
                    ["n", "NOT NULL fence"],
                    ["n", "NOT NULL generation"],
                    ["n", "NOT NULL kek_fingerprint"],
                    ["n", "NOT NULL manifest_sha256"],
                    ["p", "PRIMARY KEY (fence)"],
                ]
            else:
                columns = [
                    ["singleton", "boolean", True, "true"],
                    ["generation", "character varying", True, None],
                    ["image_digest", "character varying(71)", True, None],
                ]
                constraints = [
                    ["c", "CHECK (singleton)"],
                    ["n", "NOT NULL generation"],
                    ["n", "NOT NULL image_digest"],
                    ["n", "NOT NULL singleton"],
                    ["p", "PRIMARY KEY (singleton)"],
                ]
            return json.dumps(
                {
                    "columns": columns,
                    "constraints": constraints,
                    "owner": "ne",
                    "acl": None,
                    "column_acl": [],
                    "triggers": 0,
                    "policies": 0,
                    "rules": 0,
                    "rls": False,
                    "force_rls": False,
                    "options": None,
                    "current_user": "ne",
                }
            )
        if "SELECT fence FROM" in sql:
            material = "\x00".join(("backup-1", "b" * 64, "fingerprint", GENERATION))
            return hashlib.sha256(material.encode()).hexdigest()
        if "SELECT generation||'|'||image_digest" in sql:
            return f"{GENERATION}|sha256:{'a' * 64}"
        return "ok"

    monkeypatch.setattr(port, "_psql_scalar", psql)
    migrated_conversations: list[bool] = []

    async def migrate_conversations() -> None:
        migrated_conversations.append(True)

    monkeypatch.setattr(port, "_migrate_conversations", migrate_conversations)
    receipt = BackupReceipt(
        backup_id="backup-1",
        manifest_sha256="b" * 64,
        kek_fingerprint="fingerprint",
        databases=frozenset({"neutron", "ne_dbos"}),
        verified=True,
    )
    fence = asyncio.run(port.retire(receipt))
    asyncio.run(port.migrate(fence))
    asyncio.run(port.fence(GENERATION, "sha256:" + "a" * 64))

    assert len(fence) == 64
    assert migrated_conversations == [True]
    assert sum("INSERT INTO ne_agent_transport_retirement" in sql for _, sql in calls) == 1
    assert sum("'policies'" in sql and "transport_retirement" in sql for _, sql in calls) >= 3
    dbos_sql = next(sql for database, sql in calls if database == "ne_dbos" and "BEGIN;" in sql)
    for surface in (
        "workflow_status",
        "operation_outputs",
        "authenticated_user",
        "authenticated_roles",
        "assumed_role",
        "notifications",
        "streams",
        "workflow_events",
        "workflow_events_history",
    ):
        assert surface in dbos_sql
    assert "legacy_transport_retired" in dbos_sql
    for surface in _PLAINTEXT_SURFACES["ne_dbos"]:
        table, column = surface.split(".", 1)
        update = re.search(rf"UPDATE dbos\.{table}\b[^;]*", dbos_sql)
        assert update is not None, surface
        assert re.search(rf"\b{column}\s*=", update.group()), surface
    assert any("legacy_transport_retired=TRUE" in sql for _, sql in calls)
    fenced = [
        database for database, sql in calls if "INSERT INTO ne_agent_transport_generation" in sql
    ]
    assert sorted(fenced) == ["ne_dbos", "neutron"]
    contract_databases = [
        database
        for database, sql in calls
        if "'policies'" in sql and "ne_agent_transport_generation" in sql
    ]
    assert contract_databases.count("neutron") >= 2
    assert contract_databases.count("ne_dbos") >= 2


def test_fixed_adapter_starts_canonical_roots_and_parses_exact_ready_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port(tmp_path)
    image = "sha256:" + "a" * 64
    compose_calls: list[tuple[str, ...]] = []
    inspect_calls: list[list[str]] = []

    def compose(*args: str, capture: bool = False) -> str:
        compose_calls.append(args)
        if args[:2] == ("ps", "--quiet"):
            return "container-" + args[-1]
        return ""

    def docker(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        inspect_calls.append(argv)
        template = argv[argv.index("--format") + 1]
        if template == "{{.Image}}":
            return SimpleNamespace(stdout=image + "\n")
        assert "println .State.Health.Status" in template
        return SimpleNamespace(
            stdout=(
                "healthy\n"
                + image
                + "\nNE_AGENT_TRANSPORT_GENERATION="
                + GENERATION
                + "\nUNRELATED=value\n"
            )
        )

    monkeypatch.setattr(port, "_run", compose)
    agent_transport_module = importlib.import_module("ne.cli.agent_transport")
    monkeypatch.setattr(agent_transport_module.subprocess, "run", docker)

    for root in ("ne-worker", "ne-api"):
        asyncio.run(port.start(root, GENERATION, image))
        assert asyncio.run(port.ready(root, GENERATION, image)) is True
    assert (
        "up",
        "--detach",
        "--no-deps",
        "--wait",
        "--pull",
        "never",
        "ne-worker",
    ) in compose_calls
    assert (
        "up",
        "--detach",
        "--no-deps",
        "--wait",
        "--pull",
        "never",
        "ne-api",
    ) in compose_calls
    assert all(call[:3] == ["docker", "inspect", "--format"] for call in inspect_calls)

    assert asyncio.run(port.ready("ne-api", "wrong-generation", image)) is False
    assert asyncio.run(port.ready("ne-api", GENERATION, "sha256:" + "b" * 64)) is False
    with pytest.raises(ValueError, match="root is invalid"):
        asyncio.run(port.start("api", GENERATION, image))


@pytest.mark.parametrize(
    ("health", "generation", "expected_image"),
    (
        ("starting", GENERATION, IMAGE),
        ("healthy", "wrong-generation", IMAGE),
        ("healthy", GENERATION, "sha256:" + "b" * 64),
    ),
)
def test_fixed_adapter_ready_refuses_unhealthy_or_foreign_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    health: str,
    generation: str,
    expected_image: str,
) -> None:
    port = _port(tmp_path)
    monkeypatch.setattr(port, "_container_id", lambda root: "container-" + root)

    def docker(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        template = argv[argv.index("--format") + 1]
        assert "println .State.Health.Status" in template
        return SimpleNamespace(
            stdout=(health + "\n" + IMAGE + "\nNE_AGENT_TRANSPORT_GENERATION=" + GENERATION + "\n")
        )

    agent_transport_module = importlib.import_module("ne.cli.agent_transport")
    monkeypatch.setattr(agent_transport_module.subprocess, "run", docker)

    assert asyncio.run(port.ready("ne-worker", generation, expected_image)) is False


def test_fixed_adapter_force_recreates_one_root_with_closed_compose_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(port, "_run", lambda *args, **_kwargs: calls.append(args) or "")

    asyncio.run(port.restart_root("ne-worker", GENERATION, IMAGE))

    assert calls == [
        (
            "up",
            "--detach",
            "--no-deps",
            "--wait",
            "--pull",
            "never",
            "--force-recreate",
            "ne-worker",
        )
    ]


def test_fixed_adapter_captures_closed_exact_ready_root_identity_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port(tmp_path)
    container = "1" * 64
    compose_hash = "2" * 64
    started_at = "2026-08-23T12:34:56.123456789Z"
    monkeypatch.setattr(port, "_container_id", lambda root: container)

    def docker(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        template = argv[argv.index("--format") + 1]
        assert "NE_AGENT_TRANSPORT_GENERATION=agent-transport-v1" in template
        assert "NE_PILOT_POSTGRES_PASSWORD" not in template
        assert "{{json .Config.Env}}" not in template
        return SimpleNamespace(
            stdout="\n".join(
                (
                    container,
                    "true",
                    "healthy",
                    "4321",
                    started_at,
                    IMAGE,
                    compose_hash,
                    str(port.compose_files[0]),
                    str(port.compose_working_dir),
                    f"NE_AGENT_TRANSPORT_GENERATION={GENERATION}",
                )
            )
            + "\n"
        )

    agent_transport_module = importlib.import_module("ne.cli.agent_transport")
    monkeypatch.setattr(agent_transport_module.subprocess, "run", docker)
    identity = port.root_identity(
        "ne-worker",
        generation=GENERATION,
        image_digest=IMAGE,
        compose_hash=compose_hash,
    )
    expected_epoch = hashlib.sha256(
        b"ne.agent-transport.process-epoch.v1\x004321\x00" + started_at.encode()
    ).hexdigest()
    assert identity == {
        "compose_config_hash": compose_hash,
        "container_id": container,
        "generation": GENERATION,
        "image_digest": IMAGE,
        "process_epoch": expected_epoch,
    }


def test_controlled_restart_is_worker_first_and_emits_canonical_bound_receipt() -> None:
    async def scenario() -> tuple[_RestartPort, dict[str, object]]:
        port = _RestartPort()
        receipt = await _controlled_restart_receipt(
            cast(FixedPilotCutoverPort, port),
            journal=await _journal(image=CUTOVER_IMAGE),
            expected_image_digest=IMAGE,
        )
        return port, receipt

    port, receipt = asyncio.run(scenario())
    worker_restart = port.events.index("restart:ne-worker")
    worker_ready = port.events.index("ready:ne-worker")
    api_restart = port.events.index("restart:ne-api")
    assert worker_restart < worker_ready < api_restart
    assert port.events[-1] == "unlock"
    assert "scale-zero" not in port.events
    assert set(cast(dict[str, object], receipt["pre_roots"])) == {"ne-worker", "ne-api"}
    assert set(cast(dict[str, object], receipt["post_roots"])) == {"ne-worker", "ne-api"}
    historical = cast(dict[str, object], receipt["historical_cutover"])
    assert historical["cutover_image_digest"] == CUTOVER_IMAGE
    assert "image_digest" not in historical
    for roots_key in ("pre_roots", "post_roots"):
        roots = cast(dict[str, dict[str, str]], receipt[roots_key])
        assert {identity["image_digest"] for identity in roots.values()} == {IMAGE}
    body = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    canonical_body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    assert receipt["receipt_hash"] == hashlib.sha256(canonical_body).hexdigest()
    wire = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    assert wire == json.dumps(json.loads(wire), sort_keys=True, separators=(",", ":"))
    assert "backup-secret-identity" not in wire
    assert "password" not in wire.lower()
    assert "secret-value" not in wire


@pytest.mark.parametrize(
    ("stop", "image", "generation", "message"),
    [
        (Phase.API_READY, IMAGE, GENERATION, "complete historical"),
        (Phase.COMPLETE, "sha256:" + "f" * 63, GENERATION, "malformed"),
        (Phase.COMPLETE, IMAGE, "agent-transport-v0", "foreign generation"),
    ],
)
def test_controlled_restart_refuses_incomplete_or_foreign_cutover_journal(
    stop: Phase, image: str, generation: str, message: str
) -> None:
    async def scenario() -> _RestartPort:
        port = _RestartPort()
        with pytest.raises(CutoverRefused, match=message):
            await _controlled_restart_receipt(
                cast(FixedPilotCutoverPort, port),
                journal=await _journal(stop=stop, image=image, generation=generation),
                expected_image_digest=IMAGE,
            )
        return port

    port = asyncio.run(scenario())
    assert not any(event.startswith("restart:") for event in port.events)
    assert port.events == ["lock", "unlock"]


@pytest.mark.parametrize("unchanged", ["container", "process"])
def test_controlled_restart_refuses_unchanged_root_epoch_and_scales_zero(
    unchanged: str,
) -> None:
    async def scenario() -> _RestartPort:
        port = _RestartPort(
            unchanged_root="ne-worker" if unchanged == "process" else None,
            unchanged_container_root="ne-worker" if unchanged == "container" else None,
        )
        with pytest.raises(CutoverRefused, match="new container and process epoch"):
            await _controlled_restart_receipt(
                cast(FixedPilotCutoverPort, port),
                journal=await _journal(),
                expected_image_digest=IMAGE,
            )
        return port

    port = asyncio.run(scenario())
    assert "restart:ne-worker" in port.events
    assert "restart:ne-api" not in port.events
    assert port.events[-2:] == ["scale-zero", "unlock"]


def test_controlled_restart_refuses_readiness_failure_before_api_and_scales_zero() -> None:
    async def scenario() -> _RestartPort:
        port = _RestartPort(not_ready_root="ne-worker")
        with pytest.raises(CutoverRefused, match="ne-worker did not attest"):
            await _controlled_restart_receipt(
                cast(FixedPilotCutoverPort, port),
                journal=await _journal(),
                expected_image_digest=IMAGE,
            )
        return port

    port = asyncio.run(scenario())
    assert "restart:ne-api" not in port.events
    assert port.events[-2:] == ["scale-zero", "unlock"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_digest", "sha256:" + "f" * 64),
        ("generation", "agent-transport-v0"),
    ],
)
def test_controlled_restart_refuses_wrong_current_image_or_generation(
    field: str, value: str
) -> None:
    async def scenario() -> _RestartPort:
        port = _RestartPort(foreign_post_field=("ne-worker", field, value))
        with pytest.raises(click.ClickException, match="foreign root identity"):
            await _controlled_restart_receipt(
                cast(FixedPilotCutoverPort, port),
                journal=await _journal(),
                expected_image_digest=IMAGE,
            )
        return port

    port = asyncio.run(scenario())
    assert "restart:ne-api" not in port.events
    assert port.events[-2:] == ["scale-zero", "unlock"]


def test_controlled_restart_refuses_compose_drift_and_noncanonical_secret_field() -> None:
    async def drift_scenario() -> _RestartPort:
        port = _RestartPort(drift_after_restart=True)
        with pytest.raises(CutoverRefused, match="Compose shape drifted"):
            await _controlled_restart_receipt(
                cast(FixedPilotCutoverPort, port),
                journal=await _journal(),
                expected_image_digest=IMAGE,
            )
        return port

    drifted = asyncio.run(drift_scenario())
    assert drifted.events[-2:] == ["scale-zero", "unlock"]

    async def secret_scenario() -> _RestartPort:
        port = _RestartPort(extra_identity_field=True)
        with pytest.raises(click.ClickException, match="non-canonical root identity"):
            await _controlled_restart_receipt(
                cast(FixedPilotCutoverPort, port),
                journal=await _journal(),
                expected_image_digest=IMAGE,
            )
        return port

    foreign = asyncio.run(secret_scenario())
    assert foreign.events == ["lock", "shape", "identity:ne-worker:pre", "unlock"]


def test_restart_receipt_command_is_registered_and_passes_the_inherited_upgrade_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "restart-receipt" in agent_transport.commands
    captured: dict[str, object] = {}
    sentinel_port = object()
    module = importlib.import_module("ne.cli.agent_transport")

    def port_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel_port

    async def controlled(port: object, **kwargs: object) -> dict[str, object]:
        assert port is sentinel_port
        captured.update(kwargs)
        return {"receipt_hash": "f" * 64}

    monkeypatch.setattr(module, "_store_from_bundle", lambda bundle: object())
    monkeypatch.setattr(module, "FixedPilotCutoverPort", port_factory)
    monkeypatch.setattr(module, "_controlled_restart_receipt", controlled)
    result = asyncio.run(
        _restart_receipt_command_async(
            bundle=tmp_path / "bundle",
            backup_dest=tmp_path / "backup",
            expected_image_digest=IMAGE,
            _inherited_lock_fd=17,
        )
    )
    assert result == {"receipt_hash": "f" * 64}
    assert captured["_inherited_lock_fd"] == 17


def test_fixed_adapter_preserves_exact_compose_file_set_and_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    runtime = bundle / "runtime"
    runtime.mkdir(parents=True)
    pilot_env = runtime / "pilot.env"
    pilot_env.write_text("NE_PILOT_POSTGRES_PASSWORD=secret\n", encoding="utf-8")
    pilot_env.chmod(0o600)
    stage = tmp_path / "candidate"
    working = stage / "deploy/compose/pilot"
    working.mkdir(parents=True)
    base = working / "compose.yaml"
    overlay = working / "overlay.yaml"
    base.write_text("services: {}\n", encoding="utf-8")
    overlay.write_text("services: {}\n", encoding="utf-8")
    registry = stage / "deploy/agents/registry.toml"
    registry.parent.mkdir(parents=True)
    registry.write_text("registry\n", encoding="utf-8")
    skill = stage / "deploy/skills/meridian-hardship-draft/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill\n", encoding="utf-8")
    port = FixedPilotCutoverPort(
        bundle=bundle,
        backup_dest=tmp_path / "backups",
        compose_files=(base, overlay),
        compose_working_dir=working,
    )
    observed: list[tuple[list[str], Path, dict[str, str]]] = []

    def run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        observed.append((argv, kwargs["cwd"], kwargs["env"]))
        return SimpleNamespace(stdout="", returncode=0)

    agent_transport_module = importlib.import_module("ne.cli.agent_transport")
    monkeypatch.setattr(agent_transport_module.subprocess, "run", run)
    port._run("up", "--detach", "ne-api")
    assert observed == [
        (
            [
                "docker",
                "compose",
                "--project-name",
                "ne-pilot",
                "-f",
                str(base),
                "-f",
                str(overlay),
                "--env-file",
                str(pilot_env),
                "--profile",
                "core",
                "up",
                "--detach",
                "ne-api",
            ],
            working,
            {
                "PATH": str(port._compose_env["PATH"]),
                "NE_AGENT_REGISTRY_RAW_SHA256": hashlib.sha256(registry.read_bytes()).hexdigest(),
                "NE_SKILL_BUNDLE_RAW_SHA256": hashlib.sha256(skill.read_bytes()).hexdigest(),
            },
        )
    ]


def test_fixed_adapter_preflight_resolves_roots_with_exact_source_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port(tmp_path)
    calls: list[tuple[str, ...]] = []

    def compose(*args: str, capture: bool = False) -> str:
        assert capture is True
        calls.append(args)
        if args == ("config", "--services"):
            return "postgres\nne-api\nne-worker\n"
        assert args[:2] == ("config", "--hash")
        return f"{args[-1]} {'a' * 64}\n"

    monkeypatch.setattr(port, "_run", compose)
    port.preflight()
    assert calls == [
        ("config", "--services"),
        ("config", "--hash", "ne-api"),
        ("config", "--hash", "ne-worker"),
    ]
    assert set(port._compose_env) == {
        "PATH",
        "NE_AGENT_REGISTRY_RAW_SHA256",
        "NE_SKILL_BUNDLE_RAW_SHA256",
    }

    monkeypatch.setattr(port, "_run", lambda *args, **kwargs: "ne-api z\n")
    with pytest.raises(click.ClickException, match="Compose graph omits"):
        port.preflight()


def test_fixed_adapter_refuses_foreign_runtime_env_and_nested_public_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port(tmp_path)
    port.pilot_env.chmod(0o644)
    with pytest.raises(click.ClickException, match="mode-0600"):
        FixedPilotCutoverPort(
            bundle=port.bundle,
            backup_dest=tmp_path / "backups-2",
            compose_files=port.compose_files,
            compose_working_dir=port.compose_working_dir,
        )

    port.pilot_env.chmod(0o600)
    lock = tmp_path / "upgrade.lock"
    agent_transport_module = importlib.import_module("ne.cli.agent_transport")
    monkeypatch.setattr(agent_transport_module, "_LOCK_PATH", lock)
    first = _port(tmp_path / "first")
    second = _port(tmp_path / "second")
    asyncio.run(first.lock())
    try:
        with pytest.raises(BlockingIOError):
            asyncio.run(second.lock())
        internal = FixedPilotCutoverPort(
            bundle=second.bundle,
            backup_dest=tmp_path / "internal-backups",
            compose_files=second.compose_files,
            compose_working_dir=second.compose_working_dir,
            _inherited_lock_fd=first._lock_fd,
        )
        asyncio.run(internal.lock())
        asyncio.run(internal.unlock())
        with (
            second.pilot_env.open() as foreign_lock,
            pytest.raises(click.ClickException, match="inherited upgrade lock"),
        ):
            FixedPilotCutoverPort(
                bundle=second.bundle,
                backup_dest=tmp_path / "foreign-internal-backups",
                compose_files=second.compose_files,
                compose_working_dir=second.compose_working_dir,
                _inherited_lock_fd=foreign_lock.fileno(),
            )
    finally:
        asyncio.run(first.unlock())


def test_fixed_adapter_refuses_symlinked_or_noncanonical_source_inputs(tmp_path: Path) -> None:
    port = _port(tmp_path)
    env_payload = port.pilot_env.with_suffix(".payload")
    port.pilot_env.rename(env_payload)
    port.pilot_env.symlink_to(env_payload)
    with pytest.raises(click.ClickException, match="mode-0600"):
        FixedPilotCutoverPort(
            bundle=port.bundle,
            backup_dest=tmp_path / "backups-env",
            compose_files=port.compose_files,
            compose_working_dir=port.compose_working_dir,
        )

    port.pilot_env.unlink()
    env_payload.rename(port.pilot_env)
    compose_link = tmp_path / "compose-link.yaml"
    compose_link.symlink_to(port.compose_files[0])
    with pytest.raises(click.ClickException, match="canonical regular file"):
        FixedPilotCutoverPort(
            bundle=port.bundle,
            backup_dest=tmp_path / "backups-compose",
            compose_files=(compose_link,),
            compose_working_dir=port.compose_working_dir,
        )


def test_cutover_runbook_does_not_claim_unimplemented_port_capabilities() -> None:
    from ne.runtime.legacy_transport_migration import CutoverPort

    methods = {name for name in dir(CutoverPort) if not name.startswith("_")}
    runbook = (
        (REPO_ROOT / "docs/ops/agent-transport-cutover.md").read_text(encoding="utf-8").lower()
    )
    for phrase, method in {
        "disable ingress": "disable_ingress",
        "worker workflows drain": "drain",
        "restore the decrypt-verified": "restore",
    }.items():
        assert phrase not in runbook or method in methods


@pytest.mark.parametrize(
    ("databases", "plaintext_verified", "accepted"),
    [
        (("ne_dbos", "neutron"), True, True),
        (("foreign", "ne_dbos", "neutron"), True, False),
        (("ne_dbos", "neutron"), False, False),
    ],
)
def test_backup_consumes_exact_decrypt_verified_manifest_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    databases: tuple[str, ...],
    plaintext_verified: bool,
    accepted: bool,
) -> None:
    from ne.backup.runtime import Services

    port = _port(tmp_path)
    raw = '{"id":"backup-1"}'
    manifest = SimpleNamespace(
        id="backup-1",
        databases=list(databases),
        kek_fingerprint="fingerprint",
        to_json=lambda: raw,
    )

    async def roots_zero() -> bool:
        return True

    def create_backup(**kwargs: Any) -> Any:
        target = kwargs["dest"] / "backup-1"
        target.mkdir(parents=True)
        (target / "manifest.json").write_text(raw, encoding="utf-8")
        return manifest

    class FullPilotPostgres:
        def list_databases(self) -> list[str]:
            return ["keycloak", "ne_dbos", "neutron", "openfga"]

        def dump_globals(self, _dest: Path) -> None:
            return None

        def dump_database(self, _database: str, _dest: Path) -> None:
            return None

    services = Services(
        postgres=cast(Any, FullPilotPostgres()),
        volumes=cast(Any, SimpleNamespace()),
        runner=cast(Any, SimpleNamespace()),
    )
    monkeypatch.setattr(port, "roots_zero", roots_zero)
    monkeypatch.setattr("ne.backup.cli._build_services", lambda config: services)
    monkeypatch.setattr("ne.backup.keys.resolve_kek", lambda bundle: b"k" * 32)
    monkeypatch.setattr("ne.backup.crypto.kek_fingerprint", lambda kek: "fingerprint")

    def create_selected_backup(**kwargs: Any) -> Any:
        selected = kwargs["services"].postgres
        assert isinstance(selected, _ProtectedTransportPostgresView)
        assert selected.list_databases() == ["ne_dbos", "neutron"]
        return create_backup(**kwargs)

    monkeypatch.setattr("ne.backup.runtime.create_backup", create_selected_backup)
    monkeypatch.setattr(
        "ne.backup.runtime.verify_backup",
        lambda *args, **kwargs: {
            "id": "backup-1",
            "ciphertext_verified": True,
            "plaintext_verified": plaintext_verified,
        },
    )
    if accepted:
        receipt = asyncio.run(port.backup())
        assert receipt.databases == frozenset({"neutron", "ne_dbos"})
        assert receipt.manifest_sha256 == hashlib.sha256(raw.encode()).hexdigest()
    else:
        with pytest.raises(click.ClickException, match="exactly neutron and ne_dbos"):
            asyncio.run(port.backup())


def test_full_topology_backup_uses_unwrapped_exact_four_database_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ne.backup.errors import ManifestError
    from ne.backup.runtime import Services

    class FullPilotPostgres:
        def list_databases(self) -> list[str]:
            return ["keycloak", "ne_dbos", "neutron", "openfga"]

    services = Services(
        postgres=cast(Any, FullPilotPostgres()),
        volumes=cast(Any, SimpleNamespace()),
        runner=cast(Any, SimpleNamespace()),
    )
    raw = '{"id":"topology-1"}'
    manifest = SimpleNamespace(
        id="topology-1",
        databases=["keycloak", "ne_dbos", "neutron", "openfga"],
        kek_fingerprint="fingerprint",
        to_json=lambda: raw,
    )

    def create_backup(**kwargs: Any) -> Any:
        assert kwargs["services"] is services
        target = kwargs["dest"] / manifest.id
        target.mkdir(parents=True)
        (target / "manifest.json").write_text(raw, encoding="utf-8")
        return manifest

    monkeypatch.setattr("ne.backup.cli._build_services", lambda config: services)
    monkeypatch.setattr("ne.backup.keys.resolve_kek", lambda bundle: b"k" * 32)
    monkeypatch.setattr("ne.backup.crypto.kek_fingerprint", lambda kek: "fingerprint")
    monkeypatch.setattr("ne.backup.runtime.create_backup", create_backup)
    monkeypatch.setattr(
        "ne.backup.runtime.verify_backup",
        lambda *args, **kwargs: {
            "id": manifest.id,
            "ciphertext_verified": True,
            "plaintext_verified": True,
        },
    )
    result = _create_verified_pilot_topology_backup(
        bundle=tmp_path / "bundle", dest=tmp_path / "backups"
    )
    assert result == {
        "backup_id": "topology-1",
        "manifest_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "kek_fingerprint": "fingerprint",
    }

    services.postgres = cast(Any, SimpleNamespace(list_databases=lambda: ["neutron"]))
    with pytest.raises(ManifestError, match="exactly the four admitted databases"):
        _create_verified_pilot_topology_backup(
            bundle=tmp_path / "bundle", dest=tmp_path / "backups-foreign"
        )


def test_click_boundaries_own_the_only_asyncio_runs_and_have_no_install_hook() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    run_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
        and node.func.attr == "run"
    ]
    assert len(run_calls) == 2
    enclosing = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(run_call in set(ast.walk(node)) for run_call in run_calls)
    }
    assert enclosing == {"cutover_command", "restart_receipt_command"}
    source = MODULE.read_text(encoding="utf-8")
    assert "_pilot_factory" not in source
    assert "install_hook" not in source
