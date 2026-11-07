# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE EXECUTABLE restore DRILL: back up -> wipe -> restore -> verify, end to end.

Module-gated on ``NE_BACKUP_DRILL=1``. Unset (the local default) → skip with a
reason naming the CI workflow. Set → docker MUST work or the drill FAILS (it never
silently skips when asked to run). Boots the pinned pilot postgres + qdrant images
(digests read from deploy/compose/pilot/compose.yaml so they can never drift from
NE-070's provenance), seeds a canary, and proves: NEB1 encryption (canary bytes
absent from every artifact), canary survival, byte-equivalent row set, qdrant point
count, bundle-file restore, the safety-snapshot path, a measured RTO bound, a
tamper-aborts-before-wipe sub-case, and a pinned-helper volume roundtrip.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

if os.environ.get("NE_BACKUP_DRILL") != "1":
    pytest.skip(
        "restore drill is gated on NE_BACKUP_DRILL=1 — it runs in "
        ".github/workflows/backup-restore-drill.yml (backup->wipe->restore->verify)",
        allow_module_level=True,
    )

import httpx

from ne.backup import keys
from ne.backup.config import BackupConfig
from ne.backup.errors import RestoreError
from ne.backup.postgres import Postgres
from ne.backup.qdrant import Qdrant
from ne.backup.runner import DockerRunner
from ne.backup.runtime import Services, create_backup, restore_backup
from ne.backup.volumes import VolumeArchiver

_CANARY = "ne-drill-canary-8f3a"
_COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "compose" / "pilot" / "compose.yaml"
_RTO_SECONDS = float(os.environ.get("NE_BACKUP_DRILL_RTO_SECONDS", "900"))
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_REVISION = re.compile(r"^[0-9a-f]{40}$")


def _image(pattern: str) -> str:
    text = _COMPOSE.read_text(encoding="utf-8")
    match = re.search(rf"image:\s*({re.escape(pattern)}\S*@sha256:[0-9a-f]{{64}})", text)
    assert match, f"could not find {pattern} image in {_COMPOSE}"
    return match.group(1)


def _postgres_age_image() -> str:
    document = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    pilot_image = document["x-ne-postgres-age-image"]
    source_revision = document["x-ne-postgres-age-source-revision"]
    assert isinstance(pilot_image, str)
    assert isinstance(source_revision, str)
    ci_image = os.environ.get("NE_BACKUP_DRILL_POSTGRES_IMAGE")
    image = pilot_image if ci_image is None else ci_image
    assert _IMAGE_ID.fullmatch(image), "Pilot AGE image must be one exact local image ID"
    assert _SOURCE_REVISION.fullmatch(source_revision), (
        "Pilot AGE source revision must be one exact commit"
    )
    assert document["services"]["postgres"]["image"] == pilot_image
    assert document["services"]["age-admission"]["image"] == pilot_image
    assert (
        document["services"]["age-admission"]["environment"]["NE_POSTGRES_AGE_SOURCE_REVISION"]
        == source_revision
    )

    inspect = _docker(
        "image",
        "inspect",
        '--format={{.Id}}|{{index .Config.Labels "org.neutron.ne-postgres-age.source-rev"}}',
        image,
    )
    assert inspect.stdout.decode().strip() == f"{image}|{source_revision}"
    return image


def _docker(
    *args: str, stdin: bytes | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], input=stdin, capture_output=True, check=check)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(predicate, *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:
            last = exc
        time.sleep(1.0)
    raise AssertionError(f"timed out waiting for {what}: {last}")


def _postgres_initialized(container: str) -> bool:
    logs = _docker("logs", container, check=False)
    output = (logs.stdout or b"") + (logs.stderr or b"")
    if b"PostgreSQL init process complete; ready for start up." not in output:
        return False
    return (
        _docker(
            "exec",
            container,
            "psql",
            "-U",
            "ne",
            "-d",
            "neutron",
            "-At",
            "-c",
            "SELECT 1",
            check=False,
        ).returncode
        == 0
    )


class _Stack:
    def __init__(self, tmp: Path) -> None:
        self.suffix = uuid.uuid4().hex[:8]
        self.project = f"nedrill{self.suffix}"
        self.pg_container = f"ne-drill-pg-{self.suffix}"
        self.qdrant_container = f"ne-drill-qdrant-{self.suffix}"
        self.volume = f"{self.project}_caddy-data"
        self.qdrant_port = _free_port()
        self.tmp = tmp
        self.helper = BackupConfig().helper_image

    def config(self) -> BackupConfig:
        return BackupConfig(
            postgres_container=self.pg_container,
            postgres_user="ne",
            qdrant_url=f"http://127.0.0.1:{self.qdrant_port}",
            volumes=["caddy-data"],
            stop_containers=[],  # drill: nothing external to stop
            compose_project=self.project,
            helper_image=self.helper,
        )


def _pg_exec(stack: _Stack, sql: str, db: str = "neutron") -> str:
    out = _docker("exec", stack.pg_container, "psql", "-U", "ne", "-d", db, "-At", "-c", sql)
    return out.stdout.decode().strip()


def _rowset_digest(stack: _Stack) -> str:
    return _pg_exec(stack, "SELECT md5(string_agg(id || ':' || body, ',' ORDER BY id)) FROM docs")


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[_Stack]:
    if _docker("info", check=False).returncode != 0:
        pytest.fail("NE_BACKUP_DRILL=1 but the docker daemon is not available")
    st = _Stack(tmp_path)
    pg_image = _postgres_age_image()
    qdrant_image = _image("qdrant/qdrant:v1.18.2")
    try:
        _docker("volume", "create", st.volume)
        _docker(
            "run",
            "-d",
            "--name",
            st.pg_container,
            "-e",
            "POSTGRES_USER=ne",
            "-e",
            "POSTGRES_PASSWORD=drill",
            "-e",
            "POSTGRES_DB=neutron",
            pg_image,
        )
        _docker(
            "run",
            "-d",
            "--name",
            st.qdrant_container,
            "-p",
            f"127.0.0.1:{st.qdrant_port}:6333",
            qdrant_image,
        )
        _wait(
            lambda: _postgres_initialized(st.pg_container),
            timeout=90,
            what="postgres ready",
        )
        _wait(
            lambda: (
                httpx.get(f"http://127.0.0.1:{st.qdrant_port}/readyz", timeout=5).status_code == 200
            ),
            timeout=90,
            what="qdrant ready",
        )
        yield st
    finally:
        for name in (st.pg_container, st.qdrant_container):
            _docker("rm", "-f", name, check=False)
        _docker("volume", "rm", "-f", st.volume, check=False)


def _seed(stack: _Stack, bundle: Path) -> tuple[bytes, str]:
    # postgres: 100 rows including the canary at id=100.
    _pg_exec(stack, "CREATE EXTENSION age")
    assert _pg_exec(stack, "SELECT extversion FROM pg_extension WHERE extname = 'age'") == "1.7.0"
    _pg_exec(
        stack,
        "LOAD 'age'; SET search_path = ag_catalog, public; SELECT create_graph('drill_graph')",
    )
    created = _pg_exec(
        stack,
        "LOAD 'age'; SET search_path = ag_catalog, public; "
        "SELECT * FROM cypher('drill_graph', $$ "
        "CREATE (n:Continuity {key: 'age-canary'}) RETURN n.key "
        "$$) AS (key agtype)",
    )
    assert created.splitlines()[-1] == '"age-canary"'
    _pg_exec(stack, "CREATE TABLE docs (id int PRIMARY KEY, body text)")
    _pg_exec(stack, "INSERT INTO docs SELECT g, 'row-' || g FROM generate_series(1, 99) g")
    _pg_exec(stack, f"INSERT INTO docs VALUES (100, '{_CANARY}')")

    # qdrant: collection 'drill' with 50 points.
    base = f"http://127.0.0.1:{stack.qdrant_port}"
    httpx.put(
        f"{base}/collections/drill", json={"vectors": {"size": 4, "distance": "Cosine"}}, timeout=30
    ).raise_for_status()
    points = [
        {"id": i, "vector": [0.1 * i, 0.2, 0.3, 0.4], "payload": {"n": i}} for i in range(1, 51)
    ]
    httpx.put(
        f"{base}/collections/drill/points?wait=true", json={"points": points}, timeout=60
    ).raise_for_status()

    # volume marker.
    _docker(
        "run",
        "--rm",
        "--mount",
        f"type=volume,source={stack.volume},destination=/data",
        stack.helper,
        "sh",
        "-c",
        "echo caddy-marker-original > /data/marker.txt",
    )

    # bundle canary + KEK.
    (bundle / "config" / "bundle-canary.txt").write_text(
        "bundle-canary-content\n", encoding="utf-8"
    )
    fingerprint = keys.keygen(bundle)
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint)
    return b"seeded", _rowset_digest(stack)


def _services(stack: _Stack) -> Services:
    runner = DockerRunner()
    cfg = stack.config()
    return Services(
        postgres=Postgres(runner, cfg.postgres_container, cfg.postgres_user),
        volumes=VolumeArchiver(runner, cfg.compose_project, cfg.helper_image),
        runner=runner,
        qdrant=Qdrant(cfg.qdrant_url),
        echo=lambda m: print(m),
    )


def _make_bundle(root: Path) -> Path:
    bundle = root / "bundle"
    (bundle / "config").mkdir(parents=True)
    secrets_file = bundle / "secrets.ne.json"
    (bundle / "config" / "ne-secrets.toml").write_text(
        f'store = "sops-file"\n[file]\npath = "{secrets_file.as_posix()}"\nmode = "plain"\n',
        encoding="utf-8",
    )
    (bundle / "manifest.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
    (bundle / "runtime").mkdir()
    (bundle / "runtime" / "pilot.env").write_text("SECRET=leaky\n", encoding="utf-8")
    return bundle


def test_restore_drill(stack: _Stack, tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    dest = tmp_path / "backups"
    dest.mkdir()
    _, pre_digest = _seed(stack, bundle)

    cfg = stack.config()
    kek = keys.resolve_kek(bundle)

    # ---- BACK UP ----
    manifest = create_backup(
        config=cfg, services=_services(stack), dest=dest, kek=kek, kind="on-demand", bundle=bundle
    )
    snap_dir = dest / manifest.id

    # Encryption proof: every artifact is NEB1 and the plaintext canary is nowhere.
    canary_bytes = _CANARY.encode()
    for art in manifest.artifacts:
        blob = (snap_dir / art.relpath).read_bytes()
        assert blob[:4] == b"NEB1", f"{art.name} is not NEB1-framed"
        assert canary_bytes not in blob, f"canary leaked into ciphertext {art.name}"
        assert b"leaky" not in blob  # the excluded runtime/ secret never entered a backup
    assert "neutron" in manifest.databases
    assert any(c.name == "drill" and c.points_count == 50 for c in manifest.qdrant_collections)

    # ---- WIPE ----
    _pg_exec(stack, "DROP TABLE docs")
    _pg_exec(
        stack,
        "LOAD 'age'; SET search_path = ag_catalog, public; SELECT drop_graph('drill_graph', true)",
    )
    base = f"http://127.0.0.1:{stack.qdrant_port}"
    httpx.delete(f"{base}/collections/drill", timeout=30).raise_for_status()
    _docker(
        "run",
        "--rm",
        "--mount",
        f"type=volume,source={stack.volume},destination=/data",
        stack.helper,
        "sh",
        "-c",
        "echo WIPED > /data/marker.txt",
    )
    restored_bundle = tmp_path / "restored-bundle"

    # ---- RESTORE (safety path exercised) ----
    report = restore_backup(
        config=cfg,
        services=_services(stack),
        dest=dest,
        backup_id=manifest.id,
        kek=kek,
        bundle=bundle,
        restore_bundle_to=restored_bundle,
        no_safety=False,
    )
    assert report["safety_id"] is not None  # the safety snapshot ran
    assert report["verified"] is True

    # ---- VERIFY ----
    assert _pg_exec(stack, "SELECT count(*) FROM docs") == "100"
    assert _pg_exec(stack, "SELECT body FROM docs WHERE id = 100") == _CANARY
    assert _rowset_digest(stack) == pre_digest
    assert _pg_exec(stack, "SELECT extversion FROM pg_extension WHERE extname = 'age'") == "1.7.0"
    graph_value = _pg_exec(
        stack,
        "LOAD 'age'; SET search_path = ag_catalog, public; "
        "SELECT * FROM cypher('drill_graph', $$ "
        "MATCH (n:Continuity {key: 'age-canary'}) RETURN n.key "
        "$$) AS (key agtype)",
    )
    assert graph_value.splitlines()[-1] == '"age-canary"'
    catalog_identity = _pg_exec(
        stack,
        "SELECT graphid::text || ':' || namespace::oid::text || ':' || name::text "
        "FROM ag_catalog.ag_graph ORDER BY name",
    )
    # The same production reconciliation on an already-consistent catalog is
    # an exact no-op, followed by another fresh-backend structural/Cypher canary.
    Postgres(DockerRunner(), stack.pg_container, user="ne")._reconcile_age_graph_oids("neutron")
    assert (
        _pg_exec(
            stack,
            "SELECT graphid::text || ':' || namespace::oid::text || ':' || name::text "
            "FROM ag_catalog.ag_graph ORDER BY name",
        )
        == catalog_identity
    )
    points = httpx.get(f"{base}/collections/drill", timeout=30).json()["result"]["points_count"]
    assert points == 50
    assert (restored_bundle / "bundle" / "config" / "bundle-canary.txt").read_text().strip() == (
        "bundle-canary-content"
    )
    marker = (
        _docker(
            "run",
            "--rm",
            "--mount",
            f"type=volume,source={stack.volume},destination=/data",
            stack.helper,
            "cat",
            "/data/marker.txt",
        )
        .stdout.decode()
        .strip()
    )
    assert marker == "caddy-marker-original"

    # ---- RTO ----
    rto = float(report["restore_seconds"])
    print(f"[drill] measured RTO = {rto:.1f}s (threshold {_RTO_SECONDS:.0f}s)")
    assert rto < _RTO_SECONDS

    # ---- TAMPER sub-case: a flipped ciphertext byte aborts BEFORE any wipe ----
    tamper = create_backup(
        config=cfg, services=_services(stack), dest=dest, kek=kek, kind="on-demand", bundle=bundle
    )
    art = tamper.artifacts[0]
    target = dest / tamper.id / art.relpath
    raw = bytearray(target.read_bytes())
    raw[-1] ^= 0x01
    target.write_bytes(bytes(raw))
    with pytest.raises(RestoreError):
        restore_backup(
            config=cfg,
            services=_services(stack),
            dest=dest,
            backup_id=tamper.id,
            kek=kek,
            bundle=bundle,
            restore_bundle_to=tmp_path / "never",
            no_safety=True,
        )
    assert _pg_exec(stack, "SELECT count(*) FROM docs") == "100"  # live data intact

    # ---- Volume roundtrip via the pinned helper ----
    archiver = VolumeArchiver(DockerRunner(), stack.project, stack.helper)
    vol_tar = tmp_path / "vol.tar"
    archiver.archive("caddy-data", vol_tar)
    archiver.reset("caddy-data")
    archiver.extract("caddy-data", vol_tar)
    roundtrip = (
        _docker(
            "run",
            "--rm",
            "--mount",
            f"type=volume,source={stack.volume},destination=/data",
            stack.helper,
            "cat",
            "/data/marker.txt",
        )
        .stdout.decode()
        .strip()
    )
    assert roundtrip == "caddy-marker-original"
