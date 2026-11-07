# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Strict loader for the public registry and private digest receipt.

The registry is deliberately incapable of selecting executable policy.  Only
``adapter_id`` joins a target to closed adapter code; the separate receipt can
attest installed bytes but cannot select commands or paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ne.contracts.inference import InferenceTargetOperation
from ne.inference_operator.vast_bundle import (
    VAST_OFFLINE_LOCK_SHA256,
    VastBundleRefused,
    validate_vast_bundle,
)

_REGISTRY_KEYS = {"schema", "targets"}
_TARGET_KEYS = {
    "target_id",
    "display_name",
    "adapter_id",
    "resource_id",
    "served_models",
    "secret_refs",
    "capabilities",
    "deadlines_s",
    "cooldown_s",
    "reconcile_s",
}
_ADAPTER_MANIFEST_KEYS = {"schema", "digests"}
_CLOSED_ROOT = Path("/opt/neutron/inference-operator")
_SAFE_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._:-")


class ManifestRefused(ValueError):
    """The closed host policy is malformed, unsafe, or stale."""


@dataclass(frozen=True)
class RegistryTarget:
    target_id: str
    display_name: str
    adapter_id: str
    resource_id: str
    served_models: tuple[str, ...]
    secret_refs: tuple[str, ...]
    capabilities: tuple[InferenceTargetOperation, ...]
    deadlines_s: Mapping[InferenceTargetOperation, float]
    cooldown_s: float
    reconcile_s: float


@dataclass(frozen=True)
class VerifiedFile:
    path: Path
    sha256: str


@dataclass(frozen=True)
class AdapterPolicy:
    adapter_id: str
    executable: VerifiedFile
    verified_files: tuple[VerifiedFile, ...]
    operations: Mapping[InferenceTargetOperation, tuple[str, ...]]
    environment: Mapping[str, str]
    provider_egress: bool


@dataclass(frozen=True)
class PrivateManifest:
    manifest_hash: str
    targets: tuple[RegistryTarget, ...]
    adapters: Mapping[str, AdapterPolicy]


def _exact_dict(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ManifestRefused(f"{label} has an invalid closed shape")
    return value


def _safe_id(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
        or any(char not in _SAFE_ID_CHARS for char in value)
    ):
        raise ManifestRefused(f"{label} is not a safe identifier")
    return value


def _bounded_text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(ord(char) < 32 for char in value)
    ):
        raise ManifestRefused(f"{label} is invalid")
    return value


def _hash(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ManifestRefused(f"{label} is not a lowercase SHA-256")
    return value


def _positive_number(value: Any, label: str, *, maximum: float = 3600.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestRefused(f"{label} is not numeric")
    result = float(value)
    if not 0 < result <= maximum:
        raise ManifestRefused(f"{label} is outside its bound")
    return result


def _read_owned_file(
    path: Path,
    *,
    owner_uid: int,
    mode: int,
    reader_gid: int | None = None,
) -> bytes:
    if not path.is_absolute():
        raise ManifestRefused("manifest paths must be absolute")
    try:
        before = path.lstat()
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise ManifestRefused("manifest file is unavailable") from exc
    if (
        path.is_symlink()
        or canonical != path
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != owner_uid
        or (reader_gid is not None and before.st_gid != reader_gid)
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != mode
    ):
        raise ManifestRefused("manifest file ownership or mode is unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise ManifestRefused("manifest file changed during validation")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 65536):
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (after.st_size, after.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
                raise ManifestRefused("manifest file changed during validation")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ManifestRefused("manifest file cannot be read safely") from exc


def _load_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestRefused(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ManifestRefused(f"{label} must be an object")
    return value


def _closed_adapters(digests: Mapping[str, str], root: Path) -> dict[str, AdapterPolicy]:
    """Build the only admitted adapter programs from code-owned constants.

    The private receipt can attest bytes, but cannot select a path, command,
    argument, environment variable, endpoint, or secret.  Adding an adapter
    therefore requires a reviewed code change, not a configuration edit.
    """
    if not root.is_absolute():
        raise ManifestRefused("closed adapter root must be absolute")
    expected = {"cpu-script", "vast-script", "vast-cli"}
    if set(digests) != expected:
        raise ManifestRefused("adapter digest receipt has an invalid closed shape")
    admitted = {key: _hash(value, f"digests.{key}") for key, value in digests.items()}
    fixed_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    return {
        "compose-vllm-cpu-v1": AdapterPolicy(
            adapter_id="compose-vllm-cpu-v1",
            executable=VerifiedFile(root / "bin/cpu-vllm-target.sh", admitted["cpu-script"]),
            verified_files=(),
            operations=MappingProxyType(
                {
                    InferenceTargetOperation.STATUS: ("status",),
                    InferenceTargetOperation.START: ("start",),
                    InferenceTargetOperation.STOP: ("stop",),
                }
            ),
            environment=MappingProxyType(
                {
                    "PATH": fixed_path,
                    "NE_CPU_COMPOSE_RECEIPT": (
                        "/var/lib/neutron/inference-operator/cpu-compose-receipt.json"
                    ),
                    "NE_UPGRADE_LOCK": "/run/lock/neutron-live-upgrade.lock",
                    "NE_OPERATOR_PYTHON": "/usr/bin/python3",
                    "NE_OPERATOR_DOCKER": "/usr/bin/docker",
                    "NE_OPERATOR_OWNER_UID": "0",
                }
            ),
            provider_egress=False,
        ),
        "vast-private-rig-v1": AdapterPolicy(
            adapter_id="vast-private-rig-v1",
            executable=VerifiedFile(root / "bin/vast-rig.sh", admitted["vast-script"]),
            verified_files=(VerifiedFile(root / "vendor/vastai/bin/vastai", admitted["vast-cli"]),),
            operations=MappingProxyType(
                {
                    InferenceTargetOperation.STATUS: ("operator-status",),
                    InferenceTargetOperation.START: ("operator-start",),
                    InferenceTargetOperation.STOP: ("operator-stop",),
                }
            ),
            environment=MappingProxyType(
                {
                    "PATH": fixed_path,
                    "NE_VAST_OPERATOR_MODE": "1",
                    "NE_VAST_KEY_FILE": (
                        "/run/credentials/ne-inference-provider-broker.service/vast-api-key"
                    ),
                    "NE_VAST_SSH_KEY_FILE": (
                        "/run/credentials/ne-inference-provider-broker.service/vast-ssh-private-key"
                    ),
                    "NE_VAST_KNOWN_HOSTS_FILE": (
                        "/var/lib/neutron/inference-provider/vast-known-hosts"
                    ),
                    "NE_VAST_STATE_DIR": "/var/lib/neutron/inference-provider",
                    "NE_VAST_OPERATOR_TMP_ROOT": "/var/lib/neutron/inference-provider",
                    "NE_OPERATOR_PYTHON": "/usr/bin/python3",
                    "NE_OPERATOR_SSH": "/usr/bin/ssh",
                    "NE_OPERATOR_OWNER_UID": "10002",
                    "NE_PROVIDER_GRANT_CLIENT": (
                        "/opt/neutron/inference-operator/bin/provider-ssh-grant-client"
                    ),
                }
            ),
            provider_egress=True,
        ),
    }


_CLOSED_SECRET_REFS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "compose-vllm-cpu-v1": (),
        "vast-private-rig-v1": ("vast-api-key", "vast-ssh-private-key"),
    }
)


def load_private_manifest(
    registry_path: Path,
    adapter_manifest_path: Path,
    *,
    owner_uid: int = 0,
    _closed_root: Path = _CLOSED_ROOT,
    _accepted_vast_lock_sha256: str = VAST_OFFLINE_LOCK_SHA256,
    _document_mode: int = 0o600,
    _document_reader_gid: int | None = None,
    _vast_reader_gid: int | None = None,
) -> PrivateManifest:
    """Load two root-owned 0600 documents into an immutable closed policy."""
    registry_raw = _read_owned_file(
        registry_path,
        owner_uid=owner_uid,
        mode=_document_mode,
        reader_gid=_document_reader_gid,
    )
    adapters_raw = _read_owned_file(
        adapter_manifest_path,
        owner_uid=owner_uid,
        mode=_document_mode,
        reader_gid=_document_reader_gid,
    )
    registry = _exact_dict(_load_json(registry_raw, "registry"), _REGISTRY_KEYS, "registry")
    adapter_doc = _exact_dict(
        _load_json(adapters_raw, "adapter manifest"),
        _ADAPTER_MANIFEST_KEYS,
        "adapter manifest",
    )
    if registry["schema"] != "ne-inference-target-registry-v1":
        raise ManifestRefused("registry schema is unsupported")
    if adapter_doc["schema"] != "ne-inference-adapter-digests-v1":
        raise ManifestRefused("adapter manifest schema is unsupported")
    digests = adapter_doc["digests"]
    if not isinstance(digests, dict):
        raise ManifestRefused("adapter digest receipt must be an object")
    adapters = _closed_adapters(digests, _closed_root)
    try:
        admitted_vast_cli = validate_vast_bundle(
            _closed_root / "vendor/vastai",
            owner_uid=owner_uid,
            reader_gid=_vast_reader_gid,
            accepted_lock_sha256=_accepted_vast_lock_sha256,
        )
    except VastBundleRefused as exc:
        raise ManifestRefused("Vast offline bundle is not admitted") from exc
    if digests["vast-cli"] != admitted_vast_cli:
        raise ManifestRefused("Vast adapter receipt contradicts the reviewed bundle")

    target_rows = registry["targets"]
    if not isinstance(target_rows, list) or not target_rows:
        raise ManifestRefused("registry must contain targets")
    targets: list[RegistryTarget] = []
    seen_targets: set[str] = set()
    for index, value in enumerate(target_rows):
        row = _exact_dict(value, _TARGET_KEYS, f"target[{index}]")
        target_id = _safe_id(row["target_id"], "target_id")
        adapter_id = _safe_id(row["adapter_id"], "adapter_id")
        if target_id in seen_targets or adapter_id not in adapters:
            raise ManifestRefused("target id is duplicate or adapter is unknown")
        seen_targets.add(target_id)
        served_models_raw = row["served_models"]
        if not isinstance(served_models_raw, list) or not served_models_raw:
            raise ManifestRefused("served_models must be non-empty")
        served_models = tuple(_safe_id(item, "served model") for item in served_models_raw)
        if len(served_models) != len(set(served_models)):
            raise ManifestRefused("served_models must be unique")
        secret_refs_raw = row["secret_refs"]
        if not isinstance(secret_refs_raw, list):
            raise ManifestRefused("secret_refs must be a list")
        secret_refs = tuple(_safe_id(item, "secret reference") for item in secret_refs_raw)
        if secret_refs != _CLOSED_SECRET_REFS[adapter_id]:
            raise ManifestRefused("secret references are not admitted by the closed adapter")
        capabilities_raw = row["capabilities"]
        if not isinstance(capabilities_raw, list):
            raise ManifestRefused("capabilities must be a list")
        try:
            capabilities = tuple(InferenceTargetOperation(item) for item in capabilities_raw)
        except (TypeError, ValueError) as exc:
            raise ManifestRefused("capability is unknown") from exc
        if InferenceTargetOperation.STATUS not in capabilities or len(capabilities) != len(
            set(capabilities)
        ):
            raise ManifestRefused("capabilities must be unique and include status")
        if any(operation not in adapters[adapter_id].operations for operation in capabilities):
            raise ManifestRefused("target capability is absent from its closed adapter")
        deadlines_raw = row["deadlines_s"]
        if not isinstance(deadlines_raw, dict) or set(deadlines_raw) != {
            operation.value for operation in capabilities
        }:
            raise ManifestRefused("deadlines must exactly cover capabilities")
        deadlines = MappingProxyType(
            {
                operation: _positive_number(deadlines_raw[operation.value], "deadline")
                for operation in capabilities
            }
        )
        targets.append(
            RegistryTarget(
                target_id=target_id,
                display_name=_bounded_text(row["display_name"], "display_name"),
                adapter_id=adapter_id,
                resource_id=_safe_id(row["resource_id"], "resource_id"),
                served_models=served_models,
                secret_refs=secret_refs,
                capabilities=capabilities,
                deadlines_s=deadlines,
                cooldown_s=_positive_number(row["cooldown_s"], "cooldown", maximum=600),
                reconcile_s=_positive_number(row["reconcile_s"], "reconcile", maximum=600),
            )
        )
    digest = hashlib.sha256(registry_raw + b"\0" + adapters_raw).hexdigest()
    return PrivateManifest(digest, tuple(targets), MappingProxyType(adapters))


__all__ = [
    "AdapterPolicy",
    "ManifestRefused",
    "PrivateManifest",
    "RegistryTarget",
    "VerifiedFile",
    "load_private_manifest",
]
