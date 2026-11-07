# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Persistent Pilot PostgreSQL/AGE composition and admission controls."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import types
import uuid
from pathlib import Path

import pytest
import yaml

from ne.cli import main as _main  # noqa: F401

up_module = sys.modules["ne.cli.up"]

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "deploy/compose/pilot/compose.yaml"
ADMISSION = REPO_ROOT / "deploy/compose/pilot/postgres-age-admission.sh"
PACKAGE = REPO_ROOT / "deploy/airgap/scripts/package-ne-postgres-age.sh"
DIGEST = "sha256:6ba66e01b43cbfde0f0a03699f609fed1e218612aab7b2119052e0fd5aeb4f26"
REVISION = "7b7cede30d7af58ac3259b51cf59c76f1e95754c"
OLD_POSTGRES = (
    "postgres:18.4-alpine@sha256:ecafd34249b5c248f3cb6ebe339584ee6448b36fad3f0a827ae5e8efbae6afda"
)
# linux/amd64 child of the exact admitted multi-architecture index above.  The
# target AGE artifact is amd64-only, so both sides of the physical-volume proof
# must run on the same architecture even when the CI host is arm64.
OLD_POSTGRES_AMD64 = (
    "postgres@sha256:b6a16ed0eb96e2c362811f7eeb951eac8b459e7b40be4149ea5444aa7c65569b"
)
PILOT_DATABASES = ("keycloak", "ne_dbos", "neutron", "openfga")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_pilot_uses_one_exact_age_artifact_and_retained_identity_volumes() -> None:
    data = _compose()
    services = data["services"]
    assert data["x-ne-postgres-age-image"] == DIGEST
    assert data["x-ne-postgres-age-source-revision"] == REVISION
    assert services["postgres"]["image"] == services["age-admission"]["image"] == DIGEST
    assert (
        services["postgres"]["platform"] == services["age-admission"]["platform"] == ("linux/amd64")
    )
    assert services["postgres"]["pull_policy"] == "never"
    assert services["postgres"]["command"] == ["-c", "listen_addresses=*"]
    assert services["age-admission"]["pull_policy"] == "never"
    assert services["postgres"]["volumes"] == [
        "pg-data:/var/lib/postgresql",
        "./initdb:/var/lib/postgres/initdb:ro",
    ]
    assert "/docker-entrypoint-initdb.d" not in str(services["postgres"]["volumes"])
    assert services["age-admission"]["volumes"] == [
        "./postgres-age-admission.sh:/usr/local/share/neutron/postgres-age-admission.sh:ro",
        "age-admission-data:/var/lib/neutron-age-admission",
    ]
    assert {"pg-data", "age-admission-data"} <= set(data["volumes"])


def test_admission_is_exact_and_gates_both_production_consumers() -> None:
    services = _compose()["services"]
    admission = services["age-admission"]
    assert admission["environment"] == {
        "NE_PILOT_POSTGRES_PASSWORD": (
            "${NE_PILOT_POSTGRES_PASSWORD:?set via the config bundle (ne up --bundle)}"
        ),
        "NE_POSTGRES_AGE_IMAGE_DIGEST": DIGEST,
        "NE_POSTGRES_AGE_SOURCE_REVISION": REVISION,
    }
    assert admission["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert admission["restart"] == "no"
    assert admission["read_only"] is True
    assert admission["cap_drop"] == ["ALL"]
    for consumer in ("ne-api", "ne-worker"):
        assert services[consumer]["depends_on"]["age-admission"] == {
            "condition": "service_completed_successfully"
        }

    text = ADMISSION.read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS age" in text
    assert "create_graph('ne_graph')" in text
    assert "PostgreSQL AGE admission receipt mismatch" in text

    format_match = re.search(
        r"expected_receipt=\"\$\(printf \\\n  '([^']+)' \\\n",
        text,
    )
    assert format_match is not None
    receipt_format = format_match.group(1)
    assert receipt_format == (
        '{"image_digest":"%s","source_revision":"%s","system_identifier":"%s"}'
    )
    emitted = subprocess.run(
        [
            "/bin/sh",
            "-c",
            'printf "$1" "$2" "$3" "$4"',
            "admission-receipt",
            receipt_format,
            DIGEST,
            REVISION,
            "7623490816542367211",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert json.loads(emitted) == {
        "image_digest": DIGEST,
        "source_revision": REVISION,
        "system_identifier": "7623490816542367211",
    }


def test_graph_connection_is_shared_and_activation_is_declared_null_valued() -> None:
    """Both roots address one graph; activation is a declared operator decision.

    The #744 control asserted activation was HELD — the key was absent from the
    file, so no operator could reach the tier without editing governed Compose.
    Activation now exists as the NE-087 null-valued pass-through: still OFF for
    every bring-up that does not ask for it, but reachable through
    ``ne up --graph`` rather than through an edit here.
    """
    data = _compose()
    env = data["x-ne-app-env"]
    raw_dsn = env["NE_BRAIN_GRAPH_DATABASE_URL"]
    ledger_url = env["NE_BRAIN_GRAPH_LEDGER_URL"]
    assert raw_dsn == "postgresql://ne:${NE_PILOT_POSTGRES_PASSWORD}@postgres:5432/neutron"
    assert ledger_url == (
        "postgresql+psycopg://ne:${NE_PILOT_POSTGRES_PASSWORD}@postgres:5432/neutron"
    )
    # The graph store needs a raw libpq DSN; the SQLAlchemy ledger must select
    # the psycopg-3 dialect because the application image does not ship psycopg2.
    assert ledger_url.replace("postgresql+psycopg://", "postgresql://", 1) == raw_dsn
    assert env["NE_BRAIN_GRAPH_GRAPH_NAME"] == "ne_graph"
    assert env["NE_BRAIN_GRAPH_ENABLED"] is None
    # SHARED anchor, not a per-service copy: two roots that disagree on whether
    # the graph tier is composed freeze two different config_hashes over one graph.
    for service in ("ne-api", "ne-worker"):
        assert data["services"][service]["environment"]["NE_BRAIN_GRAPH_ENABLED"] is None
    # Population is NOT declared: this tier configures no connector-sync source,
    # so a mapping here would map nothing. Activation composes graph READ only.
    assert "NE_BRAIN_GRAPH_SOURCES" not in env


def test_airgap_export_binds_archive_to_the_admitted_artifact() -> None:
    text = PACKAGE.read_text(encoding="utf-8")
    assert f'expected_digest="{DIGEST}"' in text
    assert f'expected_revision="{REVISION}"' in text
    assert "docker image inspect" in text
    assert "docker image save" in text
    assert text.count('"archive_sha256"') == 1
    assert text.count('"image_digest"') == 1
    assert text.count('"source_revision"') == 1


def test_up_age_preflight_requires_carrier_after_config_digest_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""),
    )
    monkeypatch.delenv("NE_DEMO_POSTGRES_AGE_ARCHIVE", raising=False)
    monkeypatch.delenv("NE_DEMO_POSTGRES_AGE_ARCHIVE_MANIFEST", raising=False)
    with pytest.raises(up_module.click.ClickException, match="canonical absolute archive paths"):
        up_module._check_age_image()


def test_up_age_preflight_accepts_only_exact_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[list[str]] = []

    def _run(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
        observed.append(argv)
        return types.SimpleNamespace(returncode=0, stdout=f"{DIGEST}|{REVISION}|linux|amd64\n")

    monkeypatch.setattr(up_module.subprocess, "run", _run)
    up_module._check_age_image()
    assert observed == [
        [
            "docker",
            "image",
            "inspect",
            DIGEST,
            "--format",
            '{{.Id}}|{{index .Config.Labels "org.neutron.ne-postgres-age.source-rev"}}|'
            "{{.Os}}|{{.Architecture}}",
        ]
    ]


def _portable_age_carrier(tmp_path: Path) -> tuple[Path, Path, str, str]:
    config = json.dumps(
        {
            "architecture": "amd64",
            "config": {"Labels": {"org.neutron.ne-postgres-age.source-rev": REVISION}},
            "os": "linux",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    config_digest = hashlib.sha256(config).hexdigest()
    layer = b"admitted-age-layer\n"
    layer_digest = hashlib.sha256(layer).hexdigest()
    manifest = json.dumps(
        {
            "config": {"digest": f"sha256:{config_digest}", "size": len(config)},
            "layers": [{"digest": f"sha256:{layer_digest}", "size": len(layer)}],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    runtime_digest = hashlib.sha256(manifest).hexdigest()
    index = json.dumps(
        {
            "manifests": [
                {
                    "digest": f"sha256:{runtime_digest}",
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "size": len(manifest),
                }
            ],
            "schemaVersion": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    archive = tmp_path / "age.tar"
    with tarfile.open(archive, "w") as bundle:
        for name, payload in {
            "index.json": index,
            f"blobs/sha256/{runtime_digest}": manifest,
            f"blobs/sha256/{config_digest}": config,
            f"blobs/sha256/{layer_digest}": layer,
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    receipt = tmp_path / "age.json"
    receipt.write_text(
        json.dumps(
            {
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "image_digest": f"sha256:{config_digest}",
                "source_revision": REVISION,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    archive.chmod(0o600)
    receipt.chmod(0o600)
    return archive, receipt, f"sha256:{config_digest}", f"sha256:{runtime_digest}"


def test_up_age_preflight_derives_containerd_runtime_from_exact_carrier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive, receipt, config_digest, runtime_digest = _portable_age_carrier(tmp_path)
    monkeypatch.setattr(up_module, "NE_POSTGRES_AGE_IMAGE", config_digest)
    monkeypatch.setenv("NE_DEMO_POSTGRES_AGE_ARCHIVE", str(archive))
    monkeypatch.setenv("NE_DEMO_POSTGRES_AGE_ARCHIVE_MANIFEST", str(receipt))
    observed: list[str] = []

    def _run(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
        image = argv[3]
        observed.append(image)
        if image == config_digest:
            return types.SimpleNamespace(returncode=1, stdout="")
        return types.SimpleNamespace(
            returncode=0, stdout=f"{runtime_digest}|{REVISION}|linux|amd64\n"
        )

    monkeypatch.setattr(up_module.subprocess, "run", _run)
    assert up_module._check_age_image() == runtime_digest
    assert observed == [config_digest, up_module._NE_POSTGRES_AGE_LOCAL_TAG, runtime_digest]


@pytest.mark.parametrize("defect", ["archive", "config", "source", "runtime", "tag"])
def test_up_age_containerd_fallback_rejects_foreign_carrier_or_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, defect: str
) -> None:
    archive, receipt, config_digest, runtime_digest = _portable_age_carrier(tmp_path)
    monkeypatch.setattr(up_module, "NE_POSTGRES_AGE_IMAGE", config_digest)
    monkeypatch.setenv("NE_DEMO_POSTGRES_AGE_ARCHIVE", str(archive))
    monkeypatch.setenv("NE_DEMO_POSTGRES_AGE_ARCHIVE_MANIFEST", str(receipt))
    if defect in {"archive", "config", "source"}:
        data = json.loads(receipt.read_text(encoding="utf-8"))
        if defect == "archive":
            data["archive_sha256"] = "0" * 64
        elif defect == "config":
            data["image_digest"] = "sha256:" + "0" * 64
        else:
            data["source_revision"] = "0" * 40
        receipt.write_text(json.dumps(data) + "\n", encoding="utf-8")
        with pytest.raises(up_module.click.ClickException, match="carrier identity"):
            up_module._check_age_image()
        return

    def _run(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
        image = argv[3]
        if image == config_digest:
            return types.SimpleNamespace(returncode=1, stdout="")
        reported = (
            "sha256:" + "f" * 64
            if defect == "runtime"
            or (defect == "tag" and image == up_module._NE_POSTGRES_AGE_LOCAL_TAG)
            else runtime_digest
        )
        return types.SimpleNamespace(returncode=0, stdout=f"{reported}|{REVISION}|linux|amd64\n")

    monkeypatch.setattr(up_module.subprocess, "run", _run)
    with pytest.raises(up_module.click.ClickException, match="runtime image identity"):
        up_module._check_age_image()


def test_up_age_containerd_runtime_overlay_keeps_config_admission_digest(
    tmp_path: Path,
) -> None:
    runtime_digest = "sha256:" + "2" * 64
    overlay = up_module._write_runtime_age_overlay(tmp_path / "bundle", runtime_digest)
    assert stat.S_IMODE(overlay.stat().st_mode) == 0o600
    assert yaml.safe_load(overlay.read_text(encoding="utf-8")) == {
        "services": {
            "postgres": {"image": runtime_digest},
            "age-admission": {
                "image": runtime_digest,
                "environment": {"NE_POSTGRES_AGE_IMAGE_DIGEST": DIGEST},
            },
        }
    }


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=check)


#: The four states the admission script path can be in inside the sidecar. Each
#: carries the EXACT refusal it must produce, so a state that failed to
#: materialise (a bind-mount source the daemon could not share becomes a
#: DIRECTORY, which is itself one of the states) cannot pass as another one.
_SCRIPT_STATE_REFUSALS = {
    "directory": "missing or not a regular file",
    "empty": "is empty",
    "unreadable": "is not readable",
}


@pytest.mark.skipif(
    os.environ.get("NE_POSTGRES_AGE_PILOT_LIVE") != "1",
    reason="LIVE-PARKED: set NE_POSTGRES_AGE_PILOT_LIVE=1 for isolated Docker proof",
)
def test_admission_entrypoint_refuses_every_unusable_script_state(tmp_path: Path) -> None:
    """No unusable script state may report readiness through the admission gate.

    Docker materialises a MISSING bind-mount source as a DIRECTORY, and
    ``/bin/sh <directory>`` exits 0 — as does ``/bin/sh <empty file>``. Both states
    therefore satisfied ``service_completed_successfully`` while admission never
    ran, so the gate said ready and the graph did not exist. The refusal is asserted
    on the ADMISSION OUTCOME (no receipt, no graph), not on the exit code alone.

    The entrypoint is read FROM the compose file, so a guard deleted there is a
    guard deleted here.
    """
    entrypoint = _compose()["services"]["age-admission"]["entrypoint"]
    suffix = uuid.uuid4().hex[:10]
    network = f"ne-age-guard-{suffix}"
    container = f"ne-age-guard-pg-{suffix}"
    receipt_volume = f"ne-age-guard-receipt-{suffix}"
    password = "age-guard-proof-only"

    directory = tmp_path / "directory.sh"
    directory.mkdir()
    empty = tmp_path / "empty.sh"
    empty.touch()
    unreadable = tmp_path / "unreadable.sh"
    unreadable.write_text("echo MUST-NOT-RUN\n", encoding="utf-8")
    unreadable.chmod(0o000)
    states = {"directory": directory, "empty": empty, "unreadable": unreadable}

    def admit(script: Path) -> subprocess.CompletedProcess[str]:
        return _run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                "--cap-drop",
                "ALL",
                "--env",
                f"NE_PILOT_POSTGRES_PASSWORD={password}",
                "--env",
                f"NE_POSTGRES_AGE_IMAGE_DIGEST={DIGEST}",
                "--env",
                f"NE_POSTGRES_AGE_SOURCE_REVISION={REVISION}",
                "--volume",
                f"{script}:/usr/local/share/neutron/postgres-age-admission.sh:ro",
                "--volume",
                f"{receipt_volume}:/var/lib/neutron-age-admission",
                "--entrypoint",
                entrypoint[0],
                DIGEST,
                *entrypoint[1:],
            ],
            check=False,
        )

    def receipt_exists() -> bool:
        probe = _run(
            [
                "docker",
                "run",
                "--rm",
                "--volume",
                f"{receipt_volume}:/receipt",
                "--entrypoint",
                "/bin/sh",
                DIGEST,
                "-c",
                "test -f /receipt/admission.json && echo PRESENT || echo ABSENT",
            ]
        )
        return "PRESENT" in probe.stdout

    def graph_exists() -> bool:
        probe = _run(
            [
                "docker",
                "exec",
                "--env",
                f"PGPASSWORD={password}",
                container,
                "psql",
                "postgresql://ne@127.0.0.1:5432/neutron",
                "--tuples-only",
                "--no-align",
                "--command",
                "SELECT count(*) FROM ag_catalog.ag_graph WHERE name='ne_graph'",
            ],
            check=False,
        )
        return probe.returncode == 0 and probe.stdout.strip() == "1"

    try:
        _run(["docker", "network", "create", network])
        _run(["docker", "volume", "create", receipt_volume])
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                "--network",
                network,
                "--network-alias",
                "postgres",
                "--env",
                "POSTGRES_USER=ne",
                "--env",
                f"POSTGRES_PASSWORD={password}",
                "--env",
                "POSTGRES_DB=neutron",
                DIGEST,
                "-c",
                "listen_addresses=*",
            ]
        )
        probe_ready = ["docker", "exec", container, "pg_isready"]
        probe_ready += ["-h", "127.0.0.1", "-U", "ne", "-d", "neutron"]
        for _ in range(90):
            ready = _run(probe_ready, check=False)
            if ready.returncode == 0:
                break
        else:
            raise AssertionError("isolated PostgreSQL AGE container did not become ready")

        for state, script in states.items():
            outcome = admit(script)
            assert outcome.returncode != 0, f"{state}: the sidecar reported success"
            # The EXACT refusal, so a state that silently materialised as another
            # one cannot be credited to this row.
            assert _SCRIPT_STATE_REFUSALS[state] in outcome.stderr, f"{state}: {outcome.stderr!r}"
            # No false readiness: nothing downstream may find an admission receipt
            # or an admitted graph after a refusal.
            assert not receipt_exists(), f"{state}: a receipt was published"
            assert not graph_exists(), f"{state}: the graph was created"

        # POSITIVE CONTROL on the same gate, same container, same entrypoint: the
        # real admission script still admits, so the refusals above are a real
        # discrimination and not an entrypoint that can only ever fail.
        admitted = admit(ADMISSION)
        assert admitted.returncode == 0, admitted.stderr
        assert receipt_exists()
        assert graph_exists()
    finally:
        unreadable.chmod(0o600)
        _run(["docker", "rm", "--force", container], check=False)
        _run(["docker", "volume", "rm", "--force", receipt_volume], check=False)
        _run(["docker", "network", "rm", network], check=False)


@pytest.mark.skipif(
    os.environ.get("NE_POSTGRES_AGE_PILOT_LIVE") != "1",
    reason="LIVE-PARKED: set NE_POSTGRES_AGE_PILOT_LIVE=1 for isolated Docker proof",
)
def test_admitted_graph_and_system_identity_survive_container_replacement() -> None:
    suffix = uuid.uuid4().hex[:10]
    network = f"ne-age-admit-{suffix}"
    database_volume = f"ne-age-db-{suffix}"
    replacement_volume = f"ne-age-replacement-{suffix}"
    receipt_volume = f"ne-age-receipt-{suffix}"
    first = f"ne-age-first-{suffix}"
    second = f"ne-age-second-{suffix}"
    password = "age-live-proof-only"
    created_containers: list[str] = []
    created_volumes = (database_volume, replacement_volume, receipt_volume)

    def start(name: str, volume: str) -> None:
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--network",
                network,
                "--network-alias",
                "postgres",
                "--env",
                "POSTGRES_USER=ne",
                "--env",
                f"POSTGRES_PASSWORD={password}",
                "--env",
                "POSTGRES_DB=neutron",
                "--volume",
                f"{volume}:/var/lib/postgresql",
                DIGEST,
                "-c",
                "listen_addresses=*",
            ]
        )
        created_containers.append(name)
        for _ in range(60):
            ready = _run(
                [
                    "docker",
                    "exec",
                    name,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "ne",
                    "-d",
                    "neutron",
                ],
                check=False,
            )
            if ready.returncode == 0:
                return
        raise AssertionError("isolated PostgreSQL AGE container did not become ready")

    def admit(*, digest: str = DIGEST) -> subprocess.CompletedProcess[str]:
        return _run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                "--env",
                f"NE_PILOT_POSTGRES_PASSWORD={password}",
                "--env",
                f"NE_POSTGRES_AGE_IMAGE_DIGEST={digest}",
                "--env",
                f"NE_POSTGRES_AGE_SOURCE_REVISION={REVISION}",
                "--volume",
                f"{ADMISSION}:/usr/local/share/neutron/postgres-age-admission.sh:ro",
                "--volume",
                f"{receipt_volume}:/var/lib/neutron-age-admission",
                "--entrypoint",
                "/bin/sh",
                DIGEST,
                "/usr/local/share/neutron/postgres-age-admission.sh",
            ],
            check=False,
        )

    def scalar(container: str, sql: str) -> str:
        return _run(
            [
                "docker",
                "exec",
                "--env",
                f"PGPASSWORD={password}",
                container,
                "psql",
                "postgresql://ne@127.0.0.1:5432/neutron",
                "--tuples-only",
                "--no-align",
                "--quiet",
                "--command",
                sql,
            ]
        ).stdout.strip()

    try:
        _run(["docker", "network", "create", network])
        for volume in created_volumes:
            _run(["docker", "volume", "create", volume])
        start(first, database_volume)
        admitted = admit()
        assert admitted.returncode == 0, admitted.stderr
        first_system = scalar(first, "SELECT system_identifier FROM pg_control_system()")
        first_graph = scalar(first, "SELECT graphid FROM ag_catalog.ag_graph WHERE name='ne_graph'")
        scalar(
            first,
            "LOAD 'age'; SET search_path=ag_catalog,\"$user\",public; "
            "SELECT * FROM cypher('ne_graph', $$CREATE (:DeploymentProbe {key: 'persist'}) "
            "RETURN 1$$) AS (result agtype);",
        )

        wrong_digest = admit(digest="sha256:" + "0" * 64)
        assert wrong_digest.returncode != 0
        assert "receipt mismatch" in wrong_digest.stderr

        _run(["docker", "rm", "--force", first])
        created_containers.remove(first)
        start(second, database_volume)
        readmitted = admit()
        assert readmitted.returncode == 0, readmitted.stderr
        assert scalar(second, "SELECT system_identifier FROM pg_control_system()") == first_system
        assert (
            scalar(second, "SELECT graphid FROM ag_catalog.ag_graph WHERE name='ne_graph'")
            == first_graph
        )
        assert (
            scalar(
                second,
                "LOAD 'age'; SET search_path=ag_catalog,\"$user\",public; "
                "SELECT * FROM cypher('ne_graph', $$MATCH (n:DeploymentProbe {key: 'persist'}) "
                "RETURN count(n)$$) AS (result agtype);",
            )
            == "1"
        )

        _run(["docker", "rm", "--force", second])
        created_containers.remove(second)
        start(second, replacement_volume)
        replaced = admit()
        assert replaced.returncode != 0
        assert "receipt mismatch" in replaced.stderr
    finally:
        for container in created_containers:
            _run(["docker", "rm", "--force", container], check=False)
        _run(["docker", "network", "rm", network], check=False)
        for volume in created_volumes:
            _run(["docker", "volume", "rm", volume], check=False)


@pytest.mark.skipif(
    os.environ.get("NE_POSTGRES_AGE_PILOT_LIVE") != "1",
    reason="LIVE-PARKED: set NE_POSTGRES_AGE_PILOT_LIVE=1 for isolated Docker proof",
)
def test_exact_postgres_184_to_186_age_handoff_and_verified_topology_restore(
    tmp_path: Path,
) -> None:
    """Promoted Pilot proof: one volume survives the exact PostgreSQL/AGE takeover.

    This is deliberately a real database and backup-runtime proof.  It begins on
    the exact official PostgreSQL 18.4 artifact, takes a manifest-bound encrypted
    four-database backup, and replaces only the container with the admitted
    PostgreSQL 18.6 + AGE image while retaining the named data volume.  It then
    proves logical rollback from that decrypt-verified backup and exercises the
    admission receipt's foreign-image and foreign-cluster refusals.
    """
    from ne.backup.config import BackupConfig
    from ne.backup.postgres import Postgres
    from ne.backup.runner import DockerRunner
    from ne.backup.runtime import Services, create_backup, restore_backup, verify_backup

    suffix = uuid.uuid4().hex[:10]
    network = f"ne-bootstrap-pg-{suffix}"
    database_volume = f"ne-bootstrap-db-{suffix}"
    foreign_volume = f"ne-bootstrap-foreign-{suffix}"
    receipt_volume = f"ne-bootstrap-receipt-{suffix}"
    old_container = f"ne-bootstrap-old-{suffix}"
    target_container = f"ne-bootstrap-age-{suffix}"
    foreign_container = f"ne-bootstrap-foreign-{suffix}"
    password = "bootstrap-migration-live-proof-only"
    backup_dest = tmp_path / "backups"
    staging = tmp_path / "staging"
    backup_dest.mkdir()
    staging.mkdir()
    created_containers: list[str] = []
    created_volumes = (database_volume, foreign_volume, receipt_volume)

    def start(
        name: str,
        image: str,
        volume: str,
        *,
        platform: str | None = None,
        pgdata: str | None = None,
    ) -> None:
        argv = ["docker", "run", "--detach"]
        if platform is not None:
            argv += ["--platform", platform]
        argv += [
            "--name",
            name,
            "--network",
            network,
            "--network-alias",
            "postgres",
            "--env",
            "POSTGRES_USER=ne",
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--env",
            "POSTGRES_DB=neutron",
        ]
        if pgdata is not None:
            argv += ["--env", f"PGDATA={pgdata}"]
        argv += [
            "--volume",
            f"{volume}:/var/lib/postgresql",
            image,
            "-c",
            "listen_addresses=*",
        ]
        outcome = _run(argv, check=False)
        assert outcome.returncode == 0, outcome.stderr
        created_containers.append(name)
        for _ in range(120):
            ready = _run(
                [
                    "docker",
                    "exec",
                    name,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "ne",
                    "-d",
                    "neutron",
                ],
                check=False,
            )
            if ready.returncode == 0:
                return
        logs = _run(["docker", "logs", name], check=False)
        raise AssertionError(
            f"isolated PostgreSQL container {name} did not become ready: {logs.stderr}"
        )

    def remove(name: str) -> None:
        _run(["docker", "rm", "--force", name], check=False)
        if name in created_containers:
            created_containers.remove(name)

    def scalar(container: str, database: str, sql: str, *, check: bool = True) -> str:
        outcome = _run(
            [
                "docker",
                "exec",
                "--env",
                f"PGPASSWORD={password}",
                container,
                "psql",
                f"postgresql://ne@127.0.0.1:5432/{database}",
                "--set=ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--quiet",
                "--command",
                sql,
            ],
            check=check,
        )
        return outcome.stdout.strip()

    def admit(*, digest: str = DIGEST) -> subprocess.CompletedProcess[str]:
        return _run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "--network",
                network,
                "--env",
                f"NE_PILOT_POSTGRES_PASSWORD={password}",
                "--env",
                f"NE_POSTGRES_AGE_IMAGE_DIGEST={digest}",
                "--env",
                f"NE_POSTGRES_AGE_SOURCE_REVISION={REVISION}",
                "--volume",
                f"{ADMISSION}:/usr/local/share/neutron/postgres-age-admission.sh:ro",
                "--volume",
                f"{receipt_volume}:/var/lib/neutron-age-admission",
                "--entrypoint",
                "/bin/sh",
                DIGEST,
                "/usr/local/share/neutron/postgres-age-admission.sh",
            ],
            check=False,
        )

    def services(container: str) -> Services:
        class NoVolumes:
            def archive(self, key: str, dest_tar: Path) -> None:
                del key, dest_tar
                raise AssertionError("volume archive is outside the exact topology proof")

            def reset(self, key: str) -> None:
                del key
                raise AssertionError("volume reset is outside the exact topology proof")

            def extract(self, key: str, src_tar: Path) -> None:
                del key, src_tar
                raise AssertionError("volume extraction is outside the exact topology proof")

        runner = DockerRunner()
        return Services(
            postgres=Postgres(runner, container, "ne"),
            volumes=NoVolumes(),
            runner=runner,
            qdrant=None,
        )

    config = BackupConfig(volumes=[], stop_containers=[], staging_dir=str(staging))
    kek = bytes.fromhex("2a" * 32)
    expected_sentinels = {database: f"pilot-bootstrap::{database}" for database in PILOT_DATABASES}

    try:
        target_image = _run(
            [
                "docker",
                "image",
                "inspect",
                DIGEST,
                "--format",
                "{{.Id}}|{{.Architecture}}|{{index .Config.Labels "
                '"org.neutron.ne-postgres-age.source-rev"}}',
            ],
            check=False,
        )
        assert target_image.returncode == 0, "admitted PostgreSQL/AGE image is not installed"
        assert target_image.stdout.strip() == f"{DIGEST}|amd64|{REVISION}"
        old_image = _run(
            [
                "docker",
                "image",
                "inspect",
                OLD_POSTGRES_AMD64,
                "--format",
                "{{.Architecture}}",
            ],
            check=False,
        )
        assert old_image.returncode == 0, (
            f"exact official PostgreSQL 18.4 amd64 child of {OLD_POSTGRES} is not installed"
        )
        assert old_image.stdout.strip() == "amd64"

        _run(["docker", "network", "create", network])
        for volume in created_volumes:
            _run(["docker", "volume", "create", volume])

        start(old_container, OLD_POSTGRES_AMD64, database_volume, platform="linux/amd64")
        assert scalar(old_container, "postgres", "SHOW server_version") == "18.4"
        for database in PILOT_DATABASES:
            if database != "neutron":
                scalar(old_container, "postgres", f'CREATE DATABASE "{database}"')
            value = expected_sentinels[database].replace("'", "''")
            scalar(
                old_container,
                database,
                "CREATE TABLE pilot_bootstrap_sentinel (value text PRIMARY KEY); "
                f"INSERT INTO pilot_bootstrap_sentinel VALUES ('{value}')",
            )

        before_system_identifier = scalar(
            old_container, "postgres", "SELECT system_identifier FROM pg_control_system()"
        )
        before_sentinels = {
            database: scalar(
                old_container,
                database,
                "SELECT value FROM pilot_bootstrap_sentinel ORDER BY value",
            )
            for database in PILOT_DATABASES
        }
        assert before_sentinels == expected_sentinels

        manifest = create_backup(
            config=config,
            services=services(old_container),
            dest=backup_dest,
            kek=kek,
            kind="on-demand",
            note="exact PostgreSQL 18.4 pre-AGE takeover",
            _expected_databases=frozenset(PILOT_DATABASES),
        )
        assert tuple(manifest.databases) == PILOT_DATABASES
        verification = verify_backup(backup_dest, manifest.id, kek, staging_root=staging)
        assert verification == {
            "id": manifest.id,
            "kind": "on-demand",
            "artifacts": 5,
            "ciphertext_verified": True,
            "plaintext_verified": True,
        }

        remove(old_container)
        start(
            target_container,
            DIGEST,
            database_volume,
            platform="linux/amd64",
            pgdata="/var/lib/postgresql/18/docker",
        )
        assert scalar(target_container, "postgres", "SHOW server_version") == "18.6"
        assert (
            scalar(
                target_container,
                "postgres",
                "SELECT system_identifier FROM pg_control_system()",
            )
            == before_system_identifier
        )
        assert {
            database: scalar(
                target_container,
                database,
                "SELECT value FROM pilot_bootstrap_sentinel ORDER BY value",
            )
            for database in PILOT_DATABASES
        } == expected_sentinels

        admitted = admit()
        assert admitted.returncode == 0, admitted.stderr
        assert (
            scalar(
                target_container,
                "neutron",
                "SELECT extversion FROM pg_extension WHERE extname='age'",
            )
            == "1.7.0"
        )
        assert (
            scalar(
                target_container,
                "neutron",
                "SELECT count(*) FROM ag_catalog.ag_graph WHERE name='ne_graph'",
            )
            == "1"
        )

        wrong_image = admit(digest="sha256:" + "0" * 64)
        assert wrong_image.returncode != 0
        assert "receipt mismatch" in wrong_image.stderr

        # Destroy all four sentinel tables, then restore the manifest-bound backup.
        for database in PILOT_DATABASES:
            scalar(target_container, database, "DROP TABLE pilot_bootstrap_sentinel")
        restored = restore_backup(
            config=config,
            services=services(target_container),
            dest=backup_dest,
            backup_id=manifest.id,
            kek=kek,
            no_safety=True,
            staging_root=staging,
        )
        assert restored["verified"] is True
        assert set(restored["databases"]) == set(PILOT_DATABASES)
        assert {
            database: scalar(
                target_container,
                database,
                "SELECT value FROM pilot_bootstrap_sentinel ORDER BY value",
            )
            for database in PILOT_DATABASES
        } == expected_sentinels
        assert (
            scalar(
                target_container,
                "postgres",
                "SELECT system_identifier FROM pg_control_system()",
            )
            == before_system_identifier
        )
        readmitted = admit()
        assert readmitted.returncode == 0, readmitted.stderr
        assert (
            scalar(
                target_container,
                "neutron",
                "SELECT extversion FROM pg_extension WHERE extname='age'",
            )
            == "1.7.0"
        )
        assert (
            scalar(
                target_container,
                "neutron",
                "SELECT count(*) FROM ag_catalog.ag_graph WHERE name='ne_graph'",
            )
            == "1"
        )

        # A replacement cluster cannot reuse this cluster's authenticated receipt.
        remove(target_container)
        start(
            foreign_container,
            DIGEST,
            foreign_volume,
            platform="linux/amd64",
            pgdata="/var/lib/postgresql/18/docker",
        )
        foreign_refusal = admit()
        assert foreign_refusal.returncode != 0
        assert "receipt mismatch" in foreign_refusal.stderr
        remove(foreign_container)

        # Positive recovery control: the original named volume still owns the
        # admitted receipt identity and comes back with every sentinel intact.
        start(
            target_container,
            DIGEST,
            database_volume,
            platform="linux/amd64",
            pgdata="/var/lib/postgresql/18/docker",
        )
        recovered = admit()
        assert recovered.returncode == 0, recovered.stderr
        assert (
            scalar(
                target_container,
                "postgres",
                "SELECT system_identifier FROM pg_control_system()",
            )
            == before_system_identifier
        )
        assert {
            database: scalar(
                target_container,
                database,
                "SELECT value FROM pilot_bootstrap_sentinel ORDER BY value",
            )
            for database in PILOT_DATABASES
        } == expected_sentinels
    finally:
        for container in created_containers:
            _run(["docker", "rm", "--force", container], check=False)
        _run(["docker", "network", "rm", network], check=False)
        for volume in created_volumes:
            _run(["docker", "volume", "rm", "--force", volume], check=False)
