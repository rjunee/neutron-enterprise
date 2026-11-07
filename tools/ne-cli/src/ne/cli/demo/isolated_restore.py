# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Restore one encrypted Pilot backup into disposable, isolated targets.

This module is intentionally not registered on ``ne demo``.  Operators invoke it
as ``python -m ne.cli.demo.isolated_restore`` only after supplying the immutable
backup and PRE-009C receipt identities.  The authoritative backup directory is
never passed to ``ne-backup restore``: a byte-exact clone is verified and restored
into cryptographically named Docker resources on an internal network.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_SCHEMA = "neutron.pilot.isolated-restore-receipt.v1"
_CUTOVER_SCHEMA = "neutron.agent-transport.restart-receipt.v1"
_UPGRADE_LOCK = Path("/run/lock/neutron-live-upgrade.lock")
_DRILL_LOCK = Path("/run/lock/neutron-backup-restore-drill.lock")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_IMAGE = re.compile(r"(?:[A-Za-z0-9._/:-]+@)?sha256:[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_LIVE_ROOTS = ("ne-pilot-ne-api-1", "ne-pilot-ne-worker-1")
_LABEL_OWNER = "neutron.dev/isolated-restore-owner"
_LABEL_KIND = "neutron.dev/isolated-restore-kind"
_HELPER_IMAGE = (
    "alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce"
)
_JOURNAL_SCHEMA = "neutron.pilot.isolated-restore-journal.v1"
_GENERATION = "agent-transport-v1"
_MAX_RESTORE_SECONDS = 1800.0


class IsolatedRestoreError(RuntimeError):
    """A content-closed refusal from the isolated restore controller."""


class CommandPort(Protocol):
    def run(self, argv: Sequence[str], *, capture: bool = False) -> str: ...


class SubprocessPort:
    """Minimal argv-only subprocess boundary; shell expansion is never used."""

    def run(self, argv: Sequence[str], *, capture: bool = False) -> str:
        completed = subprocess.run(
            list(argv),
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return completed.stdout if capture else ""


@dataclass(frozen=True)
class RestoreInputs:
    backup_id: str
    source_dest: Path
    bundle: Path
    cutover_receipt: Path
    output_receipt: Path
    compose_file: Path
    postgres_image: str
    qdrant_image: str
    helper_image: str
    upgrade_lock: Path = _UPGRADE_LOCK
    drill_lock: Path = _DRILL_LOCK
    live_roots: tuple[str, ...] = _LIVE_ROOTS


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_identity(root: Path) -> tuple[str, int, int]:
    """Return a content identity without returning filenames or file contents."""
    digest = hashlib.sha256(b"neutron.isolated-restore.tree.v1\0")
    files = 0
    total = 0
    if not root.is_dir() or root.is_symlink():
        raise IsolatedRestoreError("restore input tree is missing or unsafe")
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise IsolatedRestoreError("restore input tree contains an unsafe entry")
        digest.update(("d" if path.is_dir() else "f").encode())
        digest.update(b"\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            size = path.stat().st_size
            files += 1
            total += size
            digest.update(str(size).encode("ascii"))
            digest.update(b"\0")
            digest.update(_sha256_file(path).encode("ascii"))
    return digest.hexdigest(), files, total


def _load_json(path: Path) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise IsolatedRestoreError("required receipt is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IsolatedRestoreError("required receipt is malformed") from error
    if not isinstance(value, dict):
        raise IsolatedRestoreError("required receipt is malformed")
    return value


def _extract_final_json(raw: str, *, operation: str) -> Mapping[str, Any]:
    """Extract exactly one final JSON object after optional content-closed progress lines."""
    decoder = json.JSONDecoder()
    starts = [index for index, char in enumerate(raw) if char == "{"]
    candidates: list[tuple[int, Any]] = []
    for start in starts:
        try:
            value, end = decoder.raw_decode(raw, start)
        except json.JSONDecodeError:
            continue
        if raw[end:].strip() == "":
            candidates.append((start, value))
    if len(candidates) != 1:
        raise IsolatedRestoreError(f"{operation} returned ambiguous JSON")
    start, value = candidates[0]
    prefix = raw[:start]
    if "{" in prefix or "}" in prefix or "[" in prefix or "]" in prefix:
        raise IsolatedRestoreError(f"{operation} returned multiple JSON documents")
    if not isinstance(value, dict):
        raise IsolatedRestoreError(f"{operation} returned a non-object JSON document")
    return value


def _manifest(inputs: RestoreInputs) -> tuple[Mapping[str, Any], str, str, int, int]:
    if _SAFE_ID.fullmatch(inputs.backup_id) is None:
        raise IsolatedRestoreError("backup identity is unsafe")
    backup = inputs.source_dest / inputs.backup_id
    manifest_path = backup / "manifest.json"
    manifest = _load_json(manifest_path)
    if manifest.get("id") != inputs.backup_id:
        raise IsolatedRestoreError("backup manifest identity is foreign")
    raw_hash = _sha256_file(manifest_path)
    identity, files, total = _tree_identity(backup)
    return manifest, raw_hash, identity, files, total


def _require_cutover_receipt(
    document: Mapping[str, Any], *, backup_id: str, manifest_sha256: str, kek: str
) -> str:
    from ne.runtime.legacy_transport_migration import Phase

    if set(document) != {"schema", "historical_cutover", "pre_roots", "post_roots", "receipt_hash"}:
        raise IsolatedRestoreError("PRE-009C receipt shape is non-canonical")
    if document.get("schema") != _CUTOVER_SCHEMA:
        raise IsolatedRestoreError("PRE-009C receipt schema is foreign")
    historical = document.get("historical_cutover")
    if not isinstance(historical, dict):
        raise IsolatedRestoreError("PRE-009C receipt is malformed")
    historical_keys = {
        "backup_id_sha256",
        "generation",
        "cutover_image_digest",
        "inventory_sha256",
        "journal_path",
        "journal_version",
        "kek_fingerprint",
        "manifest_sha256",
        "phase",
        "retirement_fence",
    }
    if set(historical) != historical_keys:
        raise IsolatedRestoreError("PRE-009C historical receipt is non-canonical")
    expected_backup_hash = hashlib.sha256(backup_id.encode("utf-8")).hexdigest()
    if (
        historical.get("phase") != "complete"
        or historical.get("generation") != _GENERATION
        or historical.get("journal_path") != "pilot/agent-transport-cutover"
        or historical.get("journal_version") != len(Phase)
        or historical.get("backup_id_sha256") != expected_backup_hash
        or historical.get("manifest_sha256") != manifest_sha256
        or historical.get("kek_fingerprint") != kek
    ):
        raise IsolatedRestoreError("PRE-009C receipt does not bind the selected backup")
    if (
        _IMAGE.fullmatch(str(historical.get("cutover_image_digest"))) is None
        or _SHA256.fullmatch(str(historical.get("inventory_sha256"))) is None
        or _SHA256.fullmatch(str(historical.get("retirement_fence"))) is None
    ):
        raise IsolatedRestoreError("PRE-009C historical receipt identity is malformed")
    root_keys = {
        "compose_config_hash",
        "container_id",
        "generation",
        "image_digest",
        "process_epoch",
    }
    normalized_roots: dict[str, dict[str, Mapping[str, Any]]] = {}
    restart_image_digest: str | None = None
    for side_name in ("pre_roots", "post_roots"):
        side = document.get(side_name)
        if not isinstance(side, dict) or set(side) != {"ne-api", "ne-worker"}:
            raise IsolatedRestoreError("PRE-009C root receipt is non-canonical")
        for identity in side.values():
            if not isinstance(identity, dict) or set(identity) != root_keys:
                raise IsolatedRestoreError("PRE-009C root identity is non-canonical")
            root_image_digest = identity.get("image_digest")
            if (
                identity.get("generation") != _GENERATION
                or _IMAGE.fullmatch(str(root_image_digest)) is None
                or any(
                    _SHA256.fullmatch(str(identity.get(field))) is None
                    for field in ("compose_config_hash", "container_id", "process_epoch")
                )
            ):
                raise IsolatedRestoreError("PRE-009C root identity is foreign")
            if restart_image_digest is None:
                restart_image_digest = str(root_image_digest)
            elif root_image_digest != restart_image_digest:
                raise IsolatedRestoreError("PRE-009C root image identity is inconsistent")
        normalized_roots[side_name] = side
    for root in ("ne-api", "ne-worker"):
        before = normalized_roots["pre_roots"][root]
        after = normalized_roots["post_roots"][root]
        if (
            before["container_id"] == after["container_id"]
            or before["process_epoch"] == after["process_epoch"]
            or before["compose_config_hash"] != after["compose_config_hash"]
            or before["image_digest"] != after["image_digest"]
        ):
            raise IsolatedRestoreError("PRE-009C restart identity did not advance exactly")
    receipt_hash = document.get("receipt_hash")
    body = {key: value for key, value in document.items() if key != "receipt_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    if receipt_hash != hashlib.sha256(canonical).hexdigest():
        raise IsolatedRestoreError("PRE-009C receipt hash is invalid")
    return str(receipt_hash)


def _compose_admission(inputs: RestoreInputs) -> str:
    if inputs.compose_file.is_symlink() or not inputs.compose_file.is_file():
        raise IsolatedRestoreError("Pilot Compose admission file is missing or unsafe")
    text = inputs.compose_file.read_text(encoding="utf-8")
    postgres = re.findall(r"^x-ne-postgres-age-image:\s*&\S+\s+(\S+)\s*$", text, re.MULTILINE)
    revision = re.findall(
        r"^x-ne-postgres-age-source-revision:\s*&\S+\s+([0-9a-f]{40})\s*$",
        text,
        re.MULTILINE,
    )
    qdrant = re.findall(
        r"^\s{4}image:\s+(qdrant/qdrant:\S+@sha256:[0-9a-f]{64})\s*$", text, re.MULTILINE
    )
    if len(postgres) != 1 or len(revision) != 1 or len(qdrant) != 1:
        raise IsolatedRestoreError("Pilot Compose image admission is ambiguous")
    if (
        inputs.postgres_image != postgres[0]
        or inputs.qdrant_image != qdrant[0]
        or inputs.helper_image != _HELPER_IMAGE
    ):
        raise IsolatedRestoreError("restore image differs from admitted Pilot image")
    return revision[0]


def _require_inputs(
    inputs: RestoreInputs,
) -> tuple[Mapping[str, Any], str, str, int, int, str, str]:
    if inputs.output_receipt.exists() or inputs.output_receipt.is_symlink():
        raise IsolatedRestoreError("isolated restore receipt already exists")
    for image in (inputs.postgres_image, inputs.qdrant_image, inputs.helper_image):
        if _IMAGE.fullmatch(image) is None:
            raise IsolatedRestoreError("restore image is not digest pinned")
    source_revision = _compose_admission(inputs)
    if inputs.bundle.is_symlink() or not inputs.bundle.is_dir():
        raise IsolatedRestoreError("backup KEK bundle is missing or unsafe")
    manifest, manifest_hash, tree_hash, file_count, byte_count = _manifest(inputs)
    kek = manifest.get("kek_fingerprint")
    if not isinstance(kek, str) or re.fullmatch(r"[0-9a-f]{16}", kek) is None:
        raise IsolatedRestoreError("backup KEK fingerprint is malformed")
    cutover_hash = _require_cutover_receipt(
        _load_json(inputs.cutover_receipt),
        backup_id=inputs.backup_id,
        manifest_sha256=manifest_hash,
        kek=kek,
    )
    databases = manifest.get("databases")
    volumes = manifest.get("volumes")
    if databases != ["ne_dbos", "neutron"] or volumes != ["caddy-data"]:
        raise IsolatedRestoreError("backup does not contain the exact Pilot restore topology")
    collections = manifest.get("qdrant_collections")
    artifacts = manifest.get("artifacts")
    if not isinstance(collections, list) or not isinstance(artifacts, list):
        raise IsolatedRestoreError("backup manifest artifact inventory is malformed")
    try:
        artifact_names = {row["name"] for row in artifacts if isinstance(row, dict)}
        expected_artifacts = {
            "pg-globals",
            "pgdb-ne_dbos",
            "pgdb-neutron",
            "bundle",
            "volume-caddy-data",
            *(f"qdrant-{row['name']}" for row in collections if isinstance(row, dict)),
        }
    except (KeyError, TypeError) as error:
        raise IsolatedRestoreError("backup manifest artifact inventory is malformed") from error
    if len(artifact_names) != len(artifacts) or artifact_names != expected_artifacts:
        raise IsolatedRestoreError("backup artifact inventory is incomplete or foreign")
    return manifest, manifest_hash, tree_hash, file_count, byte_count, cutover_hash, source_revision


@contextmanager
def _exclusive_lock(path: Path):
    if not path.is_absolute() or path.is_symlink():
        raise IsolatedRestoreError("restore lock path is unsafe")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise IsolatedRestoreError("restore lock ownership is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise IsolatedRestoreError("restore lock is already held") from error
        yield
    finally:
        os.close(descriptor)


def _inspect_roots(port: CommandPort, roots: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for root in roots:
        if not root.startswith("ne-pilot-") or not root.endswith("-1"):
            raise IsolatedRestoreError("live root name is outside the exact Pilot allowlist")
        raw = port.run(
            ["docker", "inspect", "--format", "{{.Id}}|{{.Image}}|{{.State.StartedAt}}", root],
            capture=True,
        ).strip()
        result[root] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return result


def _digest(image: str) -> str:
    return image.rsplit("@", 1)[-1]


def _repo_digest(image: str) -> str | None:
    if "@" not in image:
        return None
    repository, digest = image.rsplit("@", 1)
    if repository.rfind(":") > repository.rfind("/"):
        repository = repository[: repository.rfind(":")]
    return f"{repository}@{digest}"


def _inspect_admitted_images(
    port: CommandPort, inputs: RestoreInputs, source_revision: str
) -> None:
    for image, kind in (
        (inputs.postgres_image, "Postgres+AGE"),
        (inputs.qdrant_image, "Qdrant"),
        (inputs.helper_image, "helper"),
    ):
        raw = port.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}|{{.Id}}|{{index .Config.Labels "
                '"org.neutron.ne-postgres-age.source-rev"}}',
                image,
            ],
            capture=True,
        ).strip()
        try:
            repo_digests_raw, image_id, label = raw.split("|", 2)
            repo_digests = json.loads(repo_digests_raw)
        except (ValueError, json.JSONDecodeError) as error:
            raise IsolatedRestoreError(f"local {kind} image identity is malformed") from error
        expected_repo_digest = _repo_digest(image)
        if expected_repo_digest is None:
            admitted = image_id == _digest(image)
        else:
            admitted = (
                isinstance(repo_digests, list)
                and all(isinstance(value, str) for value in repo_digests)
                and expected_repo_digest in repo_digests
            )
        if not admitted:
            raise IsolatedRestoreError(f"local {kind} image admission is foreign")
        if kind == "Postgres+AGE" and label != source_revision:
            raise IsolatedRestoreError("local Postgres+AGE source revision is foreign")


def _docker_create(port: CommandPort, owner: str, inputs: RestoreInputs) -> tuple[str, ...]:
    network = f"ne-restore-{owner}"
    postgres = f"ne-restore-{owner}-postgres"
    qdrant = f"ne-restore-{owner}-qdrant"
    caddy = f"ne-restore-{owner}_caddy-data"
    labels = ["--label", f"{_LABEL_OWNER}={owner}"]
    port.run(["docker", "network", "create", "--internal", *labels, network])
    port.run(["docker", "volume", "create", *labels, "--label", f"{_LABEL_KIND}=caddy", caddy])
    port.run(
        [
            "docker",
            "run",
            "--pull",
            "never",
            "--detach",
            "--name",
            postgres,
            "--network",
            network,
            *labels,
            "--label",
            f"{_LABEL_KIND}=postgres-age",
            "-e",
            "POSTGRES_USER=ne",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            inputs.postgres_image,
        ]
    )
    port.run(
        [
            "docker",
            "run",
            "--pull",
            "never",
            "--detach",
            "--name",
            qdrant,
            "--network",
            network,
            *labels,
            "--label",
            f"{_LABEL_KIND}=qdrant",
            "--publish",
            "127.0.0.1::6333",
            inputs.qdrant_image,
        ]
    )
    return network, postgres, qdrant, caddy


def _wait_ready(port: CommandPort, postgres: str, qdrant_port: int) -> None:
    deadline = time.monotonic() + 90.0
    while True:
        try:
            port.run(
                [
                    "docker",
                    "exec",
                    postgres,
                    "pg_isready",
                    "--host",
                    "127.0.0.1",
                    "--username",
                    "ne",
                    "--dbname",
                    "postgres",
                ]
            )
            terminal = port.run(
                [
                    "docker",
                    "exec",
                    postgres,
                    "psql",
                    "--host",
                    "127.0.0.1",
                    "--username",
                    "ne",
                    "--dbname",
                    "postgres",
                    "--no-psqlrc",
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    "SELECT CASE WHEN NOT pg_is_in_recovery() AND "
                    "current_setting('transaction_read_only') = 'off' THEN 1 ELSE 0 END",
                ],
                capture=True,
            ).strip()
            if terminal != "1":
                raise subprocess.CalledProcessError(1, "isolated-postgres-terminal-readiness")
            break
        except (OSError, subprocess.SubprocessError):
            if time.monotonic() >= deadline:
                raise IsolatedRestoreError("isolated Postgres+AGE did not become ready") from None
            time.sleep(0.25)
    while True:
        try:
            with socket.create_connection(("127.0.0.1", qdrant_port), timeout=0.5):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise IsolatedRestoreError("isolated Qdrant did not become ready") from None
            time.sleep(0.25)


def _owned(port: CommandPort, owner: str, kind: str) -> list[str]:
    command = ["docker", kind, "ls"]
    if kind == "container":
        command.append("--all")
    return port.run(
        [*command, "--quiet", "--filter", f"label={_LABEL_OWNER}={owner}"],
        capture=True,
    ).split()


def _cleanup(port: CommandPort, owner: str) -> bool:
    """Best-effort exact-owner cleanup; success means a second complete zero census."""
    failed = False
    for kind in ("container", "volume", "network"):
        try:
            resources = _owned(port, owner, kind)
        except (OSError, subprocess.SubprocessError):
            failed = True
            continue
        for resource in resources:
            labels = ".Config.Labels" if kind == "container" else ".Labels"
            try:
                inspected = port.run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        f'{{{{index {labels} "{_LABEL_OWNER}"}}}}',
                        resource,
                    ],
                    capture=True,
                ).strip()
                if inspected != owner:
                    failed = True
                    continue
                command = ["docker", "rm", "--force", resource]
                if kind == "volume":
                    command = ["docker", "volume", "rm", "--force", resource]
                elif kind == "network":
                    command = ["docker", "network", "rm", resource]
                port.run(command)
            except (OSError, subprocess.SubprocessError):
                failed = True
    try:
        residue = any(_owned(port, owner, kind) for kind in ("container", "volume", "network"))
    except (OSError, subprocess.SubprocessError):
        return False
    return not failed and not residue


def _atomic_receipt(path: Path, document: Mapping[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise IsolatedRestoreError("isolated restore receipt already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".isolated-restore-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as output:
            json.dump(document, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _journal_path(output_receipt: Path) -> Path:
    return output_receipt.with_name(f".{output_receipt.name}.journal")


def _prepare_journal(port: CommandPort, inputs: RestoreInputs, owner: str) -> Path:
    journal = _journal_path(inputs.output_receipt)
    if journal.exists() or journal.is_symlink():
        document = _load_json(journal)
        if document != {"schema": _JOURNAL_SCHEMA, "owner": owner}:
            raise IsolatedRestoreError("isolated restore orphan journal is foreign")
        if not _cleanup(port, owner):
            raise IsolatedRestoreError("isolated restore orphan cleanup is incomplete")
        journal.unlink()
    _atomic_receipt(journal, {"schema": _JOURNAL_SCHEMA, "owner": owner})
    return journal


def _remove_journal(journal: Path) -> None:
    journal.unlink()
    directory = os.open(journal.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_config(
    path: Path,
    *,
    postgres: str,
    qdrant_port: int,
    owner: str,
    staging: Path,
    helper_image: str,
) -> None:
    path.write_text(
        f'postgres_container = "{postgres}"\n'
        'postgres_user = "ne"\n'
        f'qdrant_url = "http://127.0.0.1:{qdrant_port}"\n'
        'volumes = ["caddy-data"]\n'
        "stop_containers = []\n"
        f'compose_project = "ne-restore-{owner}"\n'
        f'staging_dir = "{staging}"\n'
        f'helper_image = "{helper_image}"\n',
        encoding="utf-8",
    )
    path.chmod(0o600)


def _docker_wrapper(workspace: Path, helper_image: str) -> Path:
    real_docker = shutil.which("docker")
    if real_docker is None:
        raise IsolatedRestoreError("Docker is unavailable")
    wrapper_dir = workspace / "closed-bin"
    wrapper_dir.mkdir(mode=0o700)
    wrapper = wrapper_dir / "docker"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"real={real_docker!r}\n"
        f"helper={helper_image!r}\n"
        "args=sys.argv[1:]\n"
        "if args and args[0] == 'run':\n"
        "    if helper not in args:\n"
        "        raise SystemExit(125)\n"
        "    args[1:1] = ['--pull', 'never', '--network', 'none']\n"
        "os.execv(real, [real, *args])\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    return wrapper


def _safe_archive_identity(archive: Path, destination: Path) -> tuple[str, int, int]:
    from ne.backup.tarsafe import safe_extract

    destination.mkdir(mode=0o700)
    with tarfile.open(archive, "r") as handle:
        safe_extract(handle, destination)
    return _tree_identity(destination)


def _authenticated_archive_identities(
    clone: Path,
    bundle: Path,
    workspace: Path,
) -> dict[str, tuple[str, int, int]]:
    """Independently decrypt and safely extract expected filesystem artifacts."""
    from ne.backup.crypto import decrypt_file
    from ne.backup.keys import resolve_kek
    from ne.backup.manifest import BackupManifest
    from ne.backup.runtime import _unwrap_backup_dek

    try:
        manifest_raw = (clone / "manifest.json").read_bytes()
        manifest = BackupManifest.from_json(manifest_raw)
        dek = _unwrap_backup_dek(
            clone,
            manifest,
            manifest_raw,
            resolve_kek(bundle),
            require_manifest_binding=True,
        )
        refs = {artifact.name: artifact for artifact in manifest.artifacts}
        identities: dict[str, tuple[str, int, int]] = {}
        for name in ("bundle", "volume-caddy-data"):
            reference = refs[name]
            plaintext = workspace / f"expected-{name}.tar"
            decrypt_file(dek, name, clone / reference.relpath, plaintext)
            if _sha256_file(plaintext) != reference.plaintext_sha256:
                raise IsolatedRestoreError("authenticated archive plaintext identity drifted")
            identities[name] = _safe_archive_identity(plaintext, workspace / f"expected-{name}")
        return identities
    except Exception as error:
        raise IsolatedRestoreError(
            "authenticated archive parity could not be established"
        ) from error


_AGE_SQL = r"""
CREATE TEMP TABLE ne_isolated_counts(kind "char", count bigint);
DO $ne$
DECLARE row record; amount bigint;
BEGIN
  FOR row IN SELECT kind, relation FROM ag_catalog.ag_label WHERE id > 2 ORDER BY graph, id LOOP
    EXECUTE format('SELECT count(*) FROM ONLY %s', row.relation::regclass) INTO amount;
    INSERT INTO ne_isolated_counts VALUES (row.kind, amount);
  END LOOP;
END $ne$;
SELECT json_build_object(
  'age_version', (SELECT extversion FROM pg_extension WHERE extname='age'),
  'graph_count', (SELECT count(*) FROM ag_catalog.ag_graph),
  'vertex_count', coalesce((SELECT sum(count) FROM ne_isolated_counts WHERE kind='v'), 0),
  'edge_count', coalesce((SELECT sum(count) FROM ne_isolated_counts WHERE kind='e'), 0),
  'catalog_rows', (SELECT json_agg(row_to_json(catalog) ORDER BY catalog.kind, catalog.graph,
      catalog.id, catalog.name) FROM (SELECT kind::text, graph::text, id::text, name::text,
      relation::text, seq_name::text FROM ag_catalog.ag_label) AS catalog)
)::text;
"""


def _age_identity(
    port: CommandPort,
    postgres: str,
    *,
    image: str,
    source_revision: str,
) -> dict[str, Any]:
    raw = port.run(
        [
            "docker",
            "exec",
            postgres,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "ne",
            "-d",
            "neutron",
            "-At",
            "-c",
            _AGE_SQL,
        ],
        capture=True,
    )
    document = _extract_final_json(raw, operation="AGE parity probe")
    catalog_rows = document.get("catalog_rows")
    counts = {field: document.get(field) for field in ("graph_count", "vertex_count", "edge_count")}
    if (
        document.get("age_version") != "1.7.0"
        or not isinstance(catalog_rows, list)
        or any(not isinstance(value, int) or value < 1 for value in counts.values())
    ):
        raise IsolatedRestoreError("isolated AGE parity is incomplete")
    catalog = json.dumps(catalog_rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "catalog_sha256": hashlib.sha256(catalog).hexdigest(),
        **counts,
        "image_digest": _digest(image),
        "source_revision": source_revision,
        "version": "1.7.0",
    }


def _caddy_identity(
    port: CommandPort,
    owner: str,
    helper_image: str,
    workspace: Path,
) -> tuple[str, int, int]:
    volume = f"ne-restore-{owner}_caddy-data"
    export = workspace / "observed-caddy.tar"
    port.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--label",
            f"{_LABEL_OWNER}={owner}",
            "--mount",
            f"type=volume,source={volume},destination=/from,readonly",
            "--mount",
            f"type=bind,source={workspace},destination=/to",
            helper_image,
            "tar",
            "cf",
            "/to/observed-caddy.tar",
            "-C",
            "/from",
            ".",
        ]
    )
    return _safe_archive_identity(export, workspace / "observed-caddy")


def _restore_parity(
    restore: Mapping[str, Any], manifest: Mapping[str, Any]
) -> tuple[dict[str, int], dict[str, int], float]:
    expected_databases = set(manifest["databases"])
    expected_qdrant: dict[str, int] = {}
    for row in manifest.get("qdrant_collections", []):
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("points_count"), int)
        ):
            raise IsolatedRestoreError("backup Qdrant parity manifest is invalid")
        expected_qdrant[row["name"]] = row["points_count"]
    databases = restore.get("databases")
    qdrant = restore.get("qdrant_collections")
    seconds = restore.get("restore_seconds")
    if (
        restore.get("verified") is not True
        or restore.get("id") != manifest["id"]
        or restore.get("safety_id") is not None
        or not isinstance(databases, dict)
        or set(databases) != expected_databases
        or any(not isinstance(value, int) or value < 0 for value in databases.values())
        or not isinstance(qdrant, dict)
        or qdrant != expected_qdrant
        or not isinstance(seconds, (int, float))
        or isinstance(seconds, bool)
        or not math.isfinite(float(seconds))
        or float(seconds) <= 0
        or float(seconds) > _MAX_RESTORE_SECONDS
    ):
        raise IsolatedRestoreError("isolated restore parity verification failed")
    database_counts = {str(key): int(value) for key, value in databases.items()}
    qdrant_counts = {str(key): int(value) for key, value in qdrant.items()}
    return database_counts, qdrant_counts, float(seconds)


def execute(inputs: RestoreInputs, *, port: CommandPort | None = None) -> dict[str, Any]:
    """Run one isolated restore and return a strict metadata-only receipt."""
    command = port or SubprocessPort()
    (
        manifest,
        manifest_hash,
        source_hash,
        file_count,
        byte_count,
        cutover_hash,
        source_revision,
    ) = _require_inputs(inputs)
    owner = hashlib.sha256(
        b"neutron.isolated-restore.owner.v1\0"
        + manifest_hash.encode("ascii")
        + b"\0"
        + cutover_hash.encode("ascii")
    ).hexdigest()[:32]
    previous_handlers: dict[int, Any] = {}
    cleaned = False
    journal: Path | None = None

    def interrupted(signum: int, _frame: Any) -> None:
        raise IsolatedRestoreError(f"isolated restore interrupted by signal {signum}")

    with ExitStack() as stack:
        stack.enter_context(_exclusive_lock(inputs.upgrade_lock))
        stack.enter_context(_exclusive_lock(inputs.drill_lock))
        _inspect_admitted_images(command, inputs, source_revision)
        journal = _prepare_journal(command, inputs, owner)
        pre_roots = _inspect_roots(command, inputs.live_roots)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        try:
            with tempfile.TemporaryDirectory(prefix="ne-isolated-restore-") as temporary:
                workspace = Path(temporary)
                clone_dest = workspace / "encrypted-backup"
                clone_backup = clone_dest / inputs.backup_id
                clone_dest.mkdir(mode=0o700)
                shutil.copytree(inputs.source_dest / inputs.backup_id, clone_backup, symlinks=False)
                clone_hash, clone_files, clone_bytes = _tree_identity(clone_backup)
                if (clone_hash, clone_files, clone_bytes) != (source_hash, file_count, byte_count):
                    raise IsolatedRestoreError("encrypted backup clone integrity mismatch")
                config = workspace / "restore.toml"
                bundle_target = workspace / f"bundle-target-{owner}"
                staging = workspace / "staging"
                bundle_target.mkdir(mode=0o700)
                staging.mkdir(mode=0o700)
                wrapper = _docker_wrapper(workspace, inputs.helper_image)
                _write_config(
                    config,
                    postgres=f"ne-restore-{owner}-postgres",
                    qdrant_port=1,
                    owner=owner,
                    staging=staging,
                    helper_image=inputs.helper_image,
                )
                base = [
                    "env",
                    f"PATH={wrapper.parent}:{os.environ.get('PATH', '')}",
                    "uv",
                    "run",
                    "--all-packages",
                    "ne-backup",
                ]
                verify_raw = command.run(
                    [
                        *base,
                        "verify",
                        inputs.backup_id,
                        "--dest",
                        str(clone_dest),
                        "--bundle",
                        str(inputs.bundle),
                        "--config",
                        str(config),
                    ],
                    capture=True,
                )
                verify = _extract_final_json(verify_raw, operation="backup verification")
                if (
                    set(verify)
                    != {"id", "kind", "artifacts", "ciphertext_verified", "plaintext_verified"}
                    or verify.get("ciphertext_verified") is not True
                    or verify.get("plaintext_verified") is not True
                    or verify.get("id") != inputs.backup_id
                    or verify.get("artifacts") != len(manifest["artifacts"])
                ):
                    raise IsolatedRestoreError("encrypted clone did not decrypt-verify exactly")
                expected_archives = _authenticated_archive_identities(
                    clone_backup,
                    inputs.bundle,
                    workspace,
                )
                _network, postgres, qdrant, _caddy = _docker_create(command, owner, inputs)
                qdrant_port_raw = (
                    command.run(["docker", "port", qdrant, "6333/tcp"], capture=True)
                    .strip()
                    .rsplit(":", 1)[-1]
                )
                if not qdrant_port_raw.isdigit():
                    raise IsolatedRestoreError("isolated Qdrant loopback port is malformed")
                qdrant_port = int(qdrant_port_raw)
                _wait_ready(command, postgres, qdrant_port)
                _write_config(
                    config,
                    postgres=postgres,
                    qdrant_port=qdrant_port,
                    owner=owner,
                    staging=staging,
                    helper_image=inputs.helper_image,
                )
                restore_raw = command.run(
                    [
                        *base,
                        "restore",
                        inputs.backup_id,
                        "--bundle",
                        str(inputs.bundle),
                        "--dest",
                        str(clone_dest),
                        "--config",
                        str(config),
                        "--restore-bundle-to",
                        str(bundle_target),
                        "--no-safety",
                        "--yes",
                    ],
                    capture=True,
                )
                restore = _extract_final_json(restore_raw, operation="backup restore")
                database_counts, qdrant_counts, restore_seconds = _restore_parity(restore, manifest)
                if not bundle_target.is_dir():
                    raise IsolatedRestoreError("isolated bundle restore target is missing")
                bundle_hash, bundle_files, bundle_bytes = _tree_identity(bundle_target)
                if bundle_files < 1 or bundle_bytes < 1:
                    raise IsolatedRestoreError("isolated bundle parity is incomplete")
                if (bundle_hash, bundle_files, bundle_bytes) != expected_archives["bundle"]:
                    raise IsolatedRestoreError("isolated bundle parity differs from backup")
                age = _age_identity(
                    command,
                    postgres,
                    image=inputs.postgres_image,
                    source_revision=source_revision,
                )
                caddy_identity = _caddy_identity(
                    command,
                    owner,
                    inputs.helper_image,
                    workspace,
                )
                if caddy_identity != expected_archives["volume-caddy-data"]:
                    raise IsolatedRestoreError("isolated caddy parity differs from backup")
                caddy_hash, caddy_files, caddy_bytes = caddy_identity
                post_roots = _inspect_roots(command, inputs.live_roots)
                if post_roots != pre_roots:
                    raise IsolatedRestoreError(
                        "live Pilot root identity changed during isolated restore"
                    )
                artifact_hashes = {
                    row["name"]: row["plaintext_sha256"] for row in manifest["artifacts"]
                }
                body: dict[str, Any] = {
                    "age": age,
                    "artifact_bytes": byte_count,
                    "artifact_count": file_count,
                    "backup_id_sha256": hashlib.sha256(inputs.backup_id.encode()).hexdigest(),
                    "bundle_artifact_sha256": artifact_hashes["bundle"],
                    "bundle_bytes": bundle_bytes,
                    "bundle_files": bundle_files,
                    "bundle_tree_sha256": bundle_hash,
                    "caddy": {
                        "byte_count": caddy_bytes,
                        "file_count": caddy_files,
                        "tree_sha256": caddy_hash,
                    },
                    "caddy_artifact_sha256": artifact_hashes["volume-caddy-data"],
                    "cleanup_complete": True,
                    "cutover_receipt_sha256": cutover_hash,
                    "database_table_counts": database_counts,
                    "kek_fingerprint": manifest["kek_fingerprint"],
                    "live_root_identity_sha256": hashlib.sha256(
                        json.dumps(pre_roots, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "manifest_sha256": manifest_hash,
                    "qdrant_point_counts": qdrant_counts,
                    "restore_seconds": restore_seconds,
                    "schema": _SCHEMA,
                    "source_clone_sha256": source_hash,
                    "verified": True,
                }
                canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
                receipt = {**body, "receipt_hash": hashlib.sha256(canonical).hexdigest()}
                cleaned = _cleanup(command, owner)
                if not cleaned:
                    raise IsolatedRestoreError("isolated restore cleanup left owned residue")
                source_after = _tree_identity(inputs.source_dest / inputs.backup_id)
                if source_after != (source_hash, file_count, byte_count):
                    raise IsolatedRestoreError(
                        "authoritative encrypted backup changed during drill"
                    )
                _atomic_receipt(inputs.output_receipt, receipt)
                if journal is None:
                    raise IsolatedRestoreError("isolated restore journal was not established")
                _remove_journal(journal)
                return receipt
        finally:
            if not cleaned:
                _cleanup(command, owner)
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-id", required=True)
    parser.add_argument("--source-dest", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--cutover-receipt", required=True, type=Path)
    parser.add_argument("--output-receipt", required=True, type=Path)
    parser.add_argument("--compose-file", required=True, type=Path)
    parser.add_argument("--postgres-image", required=True)
    parser.add_argument("--qdrant-image", required=True)
    parser.add_argument("--helper-image", required=True)
    parser.add_argument("--upgrade-lock", type=Path, default=_UPGRADE_LOCK)
    parser.add_argument("--drill-lock", type=Path, default=_DRILL_LOCK)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = execute(
            RestoreInputs(
                backup_id=args.backup_id,
                source_dest=args.source_dest,
                bundle=args.bundle,
                cutover_receipt=args.cutover_receipt,
                output_receipt=args.output_receipt,
                compose_file=args.compose_file,
                postgres_image=args.postgres_image,
                qdrant_image=args.qdrant_image,
                helper_image=args.helper_image,
                upgrade_lock=args.upgrade_lock,
                drill_lock=args.drill_lock,
            )
        )
    except (
        IsolatedRestoreError,
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ):
        print("isolated restore refused", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
