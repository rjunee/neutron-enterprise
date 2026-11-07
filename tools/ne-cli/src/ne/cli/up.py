# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""`ne up` / `ne down` — the hardened single-VM pilot installer (NE-070).

Mirrors the `ne dev` compose-driver pattern but for deploy/compose/pilot: it runs
real preflights, resolves per-customer secrets from the bundle's SecretStore into
an ephemeral 0600 env file (values NEVER logged), then `docker compose ... up
--detach --wait`. Idempotent up/down.

Serve mapping: the SPEC surface name ``serve-gpu`` is exposed as ``--serve-gpu``;
it selects the INCLUDED serve tier's compose-level ``gpu`` profile (``--serve-cpu``
→ ``cpu``). This keeps the CLI vocabulary aligned with SPEC while not fighting
compose ``include`` profile-name semantics — see deploy/compose/pilot/README.md.

NE-PILOT-OPA-BUNDLE: a `core` bring-up stages the real ne-govern rego bundle
into ``<bundle>/runtime/opa-policies`` and layers a generated compose overlay
re-mounting it at ``/policies:ro``, so a by-the-runbook pilot's OPA answers a
real ``ne.govern`` decision instead of the shipped ``ne.dev`` placeholder.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tarfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

import click

from ne.cli.checks import _find_repo_root
from ne.cli.envfile import dotenv_line
from ne.cli.opa_bundle import stage_govern_policies, write_compose_opa_overlay

_PROJECT_NAME = "ne-pilot"
# The first-party build-in-seal image (NE-103a). Single-sourced with
# deploy/compose/pilot/compose.yaml's ne-api service and
# deploy/airgap/scripts/build-ne-app.sh (guardian: test_pilot_stack.py). It is
# built locally (pull_policy: never) — never pulled — so `ne up --profile core`
# preflights that it is present before bringing the stack up.
NE_APP_IMAGE = "ne-app:0.1.0"
NE_POSTGRES_AGE_IMAGE = "sha256:6ba66e01b43cbfde0f0a03699f609fed1e218612aab7b2119052e0fd5aeb4f26"
NE_POSTGRES_AGE_SOURCE_REVISION = "7b7cede30d7af58ac3259b51cf59c76f1e95754c"
_NE_POSTGRES_AGE_LOCAL_TAG = "ne-postgres-age:18.6-1.7.0"
_CI_AGE_IIDFILE = Path("/mnt/ne-airgap/ne-postgres-age.iid")
# CI-only admission: the no-egress harness labels its locally built AGE image
# with the git tree hash of deploy/docker/ne-postgres-age (the same pin its
# build step asserts), so the admitted identity is the image source content.
_CI_NE_POSTGRES_AGE_SOURCE_REVISION = "8b60ae265b5d08a794a489492e7709f0da775734"
_OFF_BOX_PORTS = (443, 8443)
_MIN_DISK_BYTES = 10 * 1024**3  # 10 GiB
_MIN_COMPOSE = (2, 20)  # `include` support
# NE-087 (O-4): the null-env --env-file pass-through (FIX-1) is belt-and-braces
# gated to compose >= 2.24 when the rebac profile is selected (base floor stays
# 2.20 for a non-rebac bring-up). The mechanism was verified live on 2.36.2.
_MIN_COMPOSE_REBAC = (2, 24)
# NE-DEMO-HTTPS-PORT: publishing the API on a non-443 host port rides a runtime
# demo overlay whose caddy `ports: !override` block REPLACES the pinned compose's
# published list. The compose `!override` merge tag was introduced in v2.24.4
# (https://docs.docker.com/reference/compose-file/merge/); require it only when a
# non-443 --https-port is requested (the default 443 path keeps the 2.20 floor).
_MIN_COMPOSE_OVERRIDE = (2, 24, 4)
# Every compose profile `ne down` must sweep so nothing leaks, regardless of how
# the stack was started (mirrors `ne dev down` tearing down langfuse too).
_ALL_DOWN_PROFILES = ("core", "brain", "obs", "gpu", "cpu", "rebac")
_PILOT_EXPANSION = ("core", "brain", "obs")
# `ne down` never starts a container, so the ${NE_PILOT_*:?} vars in compose.yaml
# only need to be DEFINED non-empty for the env-file to parse — their VALUES are
# irrelevant. Teardown uses this fixed placeholder so it never reads the
# SecretStore (an operator whose secrets are rotated/unavailable must still be
# able to stop the stack).
_TEARDOWN_PLACEHOLDER = "ne-teardown-placeholder"
_AGENT_REGISTRY_HASH_ENV = "NE_AGENT_REGISTRY_RAW_SHA256"
_SKILL_BUNDLE_HASH_ENV = "NE_SKILL_BUNDLE_RAW_SHA256"
_DEFAULT_ASK_ACTIVATION_ENV = "NE_API_DEFAULT_ASK_GENERATION"


def _compose_file(root: Path) -> Path:
    return root / "deploy" / "compose" / "pilot" / "compose.yaml"


def _compose_base(
    root: Path,
    env_file: Path,
    profiles: list[str],
    overlays: Sequence[Path] = (),
) -> list[str]:
    cmd = [
        "docker",
        "compose",
        "--project-name",
        _PROJECT_NAME,
        "-f",
        str(_compose_file(root)),
    ]
    # NE-DEMO-LIVE-SEAMS: additive compose OVERLAY layering (the NE-084
    # cdc/pilot-override precedent). Each overlay `-f` lands AFTER the base compose
    # file so it merges over it (compose merge order: later wins). Default-empty
    # keeps the argv byte-identical to a plain bring-up.
    for overlay in overlays:
        cmd += ["-f", str(overlay)]
    cmd += ["--env-file", str(env_file)]
    for profile in profiles:
        cmd += ["--profile", profile]
    return cmd


# ---------------------------------------------------------------------------
# Preflights — each raises click.ClickException with a specific one-line message
# ---------------------------------------------------------------------------
def _check_docker() -> None:
    if shutil.which("docker") is None:
        raise click.ClickException(
            "docker CLI not found on PATH — install Docker Desktop or the docker "
            "CLI with the compose plugin"
        )


def _check_daemon() -> None:
    result = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise click.ClickException("docker daemon not reachable — start Docker Desktop / dockerd")


def _check_app_image() -> None:
    """Fail closed if the first-party ne-app image is not present locally.

    The image is build-in-seal + local-digest-pinned (owner dec ii) with
    ``pull_policy: never`` — compose will NOT pull it, so a `core` bring-up whose
    image is absent would fail deep inside compose with an opaque error. This
    preflight surfaces the fix up front. Never prints image internals.
    """
    result = subprocess.run(
        ["docker", "image", "inspect", NE_APP_IMAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"first-party image {NE_APP_IMAGE} not found — build it first: "
            "deploy/airgap/scripts/build-ne-app.sh (see deploy/docker/ne-app/README.md)"
        )


def _age_identity(image: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            image,
            "--format",
            '{{.Id}}|{{index .Config.Labels "org.neutron.ne-postgres-age.source-rev"}}|'
            "{{.Os}}|{{.Architecture}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _owner_only_carrier_path(variable: str) -> Path:
    raw = os.environ.get(variable, "")
    path = Path(raw)
    if not raw or not path.is_absolute():
        raise click.ClickException(
            "containerd PostgreSQL AGE admission requires canonical absolute archive paths"
        )
    try:
        info = path.lstat()
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise click.ClickException("PostgreSQL AGE carrier is unavailable") from exc
    if (
        canonical != path
        or not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
    ):
        raise click.ClickException("PostgreSQL AGE carrier is not owner-only regular data")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _oci_blob(bundle: tarfile.TarFile, members: dict[str, tarfile.TarInfo], name: str) -> bytes:
    member = members[name]
    if not member.isfile():
        raise ValueError
    handle = bundle.extractfile(member)
    if handle is None:
        raise ValueError
    payload = handle.read()
    if len(payload) != member.size:
        raise ValueError
    return payload


def _verify_oci_blob(
    bundle: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    *,
    digest: str,
    size: object,
) -> None:
    member = members[f"blobs/sha256/{digest.removeprefix('sha256:')}"]
    if not member.isfile() or size != member.size:
        raise ValueError
    handle = bundle.extractfile(member)
    if handle is None:
        raise ValueError
    observed = hashlib.sha256()
    observed_size = 0
    while chunk := handle.read(1024 * 1024):
        observed.update(chunk)
        observed_size += len(chunk)
    if observed_size != member.size or observed.hexdigest() != digest.removeprefix("sha256:"):
        raise ValueError


def _verify_age_carrier() -> str:
    """Return the OCI runtime digest bound by the sanctioned portable carrier."""
    archive = _owner_only_carrier_path("NE_DEMO_POSTGRES_AGE_ARCHIVE")
    receipt_path = _owner_only_carrier_path("NE_DEMO_POSTGRES_AGE_ARCHIVE_MANIFEST")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if set(receipt) != {"archive_sha256", "image_digest", "source_revision"}:
            raise ValueError
        if receipt != {
            "archive_sha256": _sha256_file(archive),
            "image_digest": NE_POSTGRES_AGE_IMAGE,
            "source_revision": NE_POSTGRES_AGE_SOURCE_REVISION,
        }:
            raise ValueError
        with tarfile.open(archive, "r:") as bundle:
            raw_members = bundle.getmembers()
            members = {member.name.removeprefix("./"): member for member in raw_members}
            if len(members) != len(raw_members):
                raise ValueError
            for name, member in members.items():
                path = PurePosixPath(name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError
            index = json.loads(_oci_blob(bundle, members, "index.json"))
            descriptors = index.get("manifests")
            if not isinstance(descriptors, list) or len(descriptors) != 1:
                raise ValueError
            descriptor = descriptors[0]
            runtime_digest = descriptor.get("digest")
            if (
                descriptor.get("mediaType") != "application/vnd.oci.image.manifest.v1+json"
                or not isinstance(runtime_digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", runtime_digest) is None
            ):
                raise ValueError
            manifest_payload = _oci_blob(
                bundle, members, f"blobs/sha256/{runtime_digest.removeprefix('sha256:')}"
            )
            if descriptor.get("size") != len(manifest_payload) or hashlib.sha256(
                manifest_payload
            ).hexdigest() != runtime_digest.removeprefix("sha256:"):
                raise ValueError
            manifest = json.loads(manifest_payload)
            config = manifest.get("config")
            if not isinstance(config, dict) or config.get("digest") != NE_POSTGRES_AGE_IMAGE:
                raise ValueError
            config_payload = _oci_blob(
                bundle, members, f"blobs/sha256/{NE_POSTGRES_AGE_IMAGE.removeprefix('sha256:')}"
            )
            if config.get("size") != len(config_payload) or hashlib.sha256(
                config_payload
            ).hexdigest() != NE_POSTGRES_AGE_IMAGE.removeprefix("sha256:"):
                raise ValueError
            layers = manifest.get("layers")
            if not isinstance(layers, list) or not layers:
                raise ValueError
            for layer in layers:
                layer_digest = layer.get("digest") if isinstance(layer, dict) else None
                if (
                    not isinstance(layer_digest, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", layer_digest) is None
                ):
                    raise ValueError
                _verify_oci_blob(bundle, members, digest=layer_digest, size=layer.get("size"))
            config_document = json.loads(config_payload)
            labels = config_document.get("config", {}).get("Labels", {})
            if (
                config_document.get("os") != "linux"
                or config_document.get("architecture") != "amd64"
                or labels.get("org.neutron.ne-postgres-age.source-rev")
                != NE_POSTGRES_AGE_SOURCE_REVISION
            ):
                raise ValueError
    except (
        KeyError,
        OSError,
        tarfile.TarError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        raise click.ClickException("PostgreSQL AGE carrier identity is not admitted") from None
    return runtime_digest


def _check_age_image() -> str:
    """Return the exact admitted local AGE reference for this image store."""
    expected_config_identity = (
        f"{NE_POSTGRES_AGE_IMAGE}|{NE_POSTGRES_AGE_SOURCE_REVISION}|linux|amd64"
    )
    result = _age_identity(NE_POSTGRES_AGE_IMAGE)
    if result.returncode == 0 and result.stdout.strip() == expected_config_identity:
        return NE_POSTGRES_AGE_IMAGE
    if result.returncode == 0:
        raise click.ClickException(
            "PostgreSQL AGE image source revision is not admitted or identity/platform differs"
        )

    runtime_digest = _verify_age_carrier()
    expected_runtime_identity = f"{runtime_digest}|{NE_POSTGRES_AGE_SOURCE_REVISION}|linux|amd64"
    tag_result = _age_identity(_NE_POSTGRES_AGE_LOCAL_TAG)
    runtime_result = _age_identity(runtime_digest)
    if (
        tag_result.returncode != 0
        or runtime_result.returncode != 0
        or tag_result.stdout.strip() != expected_runtime_identity
        or runtime_result.stdout.strip() != expected_runtime_identity
    ):
        raise click.ClickException(
            "PostgreSQL AGE local tag or runtime image identity is not admitted"
        )
    return runtime_digest


def _validate_ci_age_iidfile(iidfile: Path) -> str:
    """Admit one runner-local AGE image only inside the sealed CI harness."""
    required_markers = {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "NE_AIRGAP_CI": "1",
    }
    if any(os.environ.get(key) != value for key, value in required_markers.items()):
        raise click.ClickException("CI AGE image override is restricted to the no-egress harness")
    airgap_host = os.environ.get("NE_AIRGAP_DOCKER_HOST", "")
    if not airgap_host or os.environ.get("DOCKER_HOST") != airgap_host:
        raise click.ClickException("CI AGE image override requires the inner air-gap Docker host")
    if iidfile != _CI_AGE_IIDFILE:
        raise click.ClickException("CI AGE image override requires the fixed harness iidfile")

    try:
        raw_iid = iidfile.read_text(encoding="utf-8")
    except OSError as exc:
        raise click.ClickException("CI AGE image iidfile is unavailable") from exc
    lines = raw_iid.splitlines()
    if len(lines) != 1 or raw_iid not in {lines[0], f"{lines[0]}\n"}:
        raise click.ClickException("CI AGE image iidfile must contain exactly one image ID")
    image_id = lines[0]
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise click.ClickException("CI AGE image iidfile is not a canonical image ID")

    tag_result = subprocess.run(
        ["docker", "image", "inspect", _NE_POSTGRES_AGE_LOCAL_TAG, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if tag_result.returncode != 0 or tag_result.stdout.strip() != image_id:
        raise click.ClickException("CI AGE local tag does not resolve to the admitted image ID")

    identity_result = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            image_id,
            "--format",
            '{{.Id}}|{{index .Config.Labels "org.neutron.ne-postgres-age.source-rev"}}|'
            "{{.Os}}|{{.Architecture}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    expected = f"{image_id}|{_CI_NE_POSTGRES_AGE_SOURCE_REVISION}|linux|amd64"
    if identity_result.returncode != 0 or identity_result.stdout.strip() != expected:
        raise click.ClickException(
            "CI AGE image identity, source revision, or platform is not admitted"
        )
    return image_id


def _write_age_overlay(
    bundle: Path, *, filename: str, image_id: str, admission_digest: str
) -> Path:
    """Write a private, last-layer Compose pin for one admitted AGE image."""
    runtime_dir = bundle / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    overlay_path = runtime_dir / filename
    body = (
        "services:\n"
        "  postgres:\n"
        f"    image: {image_id}\n"
        "  age-admission:\n"
        f"    image: {image_id}\n"
        "    environment:\n"
        f"      NE_POSTGRES_AGE_IMAGE_DIGEST: {admission_digest}\n"
    )
    fd = os.open(overlay_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(overlay_path, 0o600)
    return overlay_path


def _write_ci_age_overlay(bundle: Path, image_id: str) -> Path:
    """Write the private, last-layer Compose pin for the no-egress CI image."""
    return _write_age_overlay(
        bundle,
        filename="postgres-age-ci.overlay.yaml",
        image_id=image_id,
        admission_digest=image_id,
    )


def _write_runtime_age_overlay(bundle: Path, image_id: str) -> Path:
    """Pin a carrier-derived runtime manifest while admission retains the config ID."""
    return _write_age_overlay(
        bundle,
        filename="postgres-age-runtime.overlay.yaml",
        image_id=image_id,
        admission_digest=NE_POSTGRES_AGE_IMAGE,
    )


def _check_compose_version(*, rebac: bool = False, ports_override: bool = False) -> None:
    result = subprocess.run(
        ["docker", "compose", "version", "--short"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise click.ClickException("`docker compose` plugin not available")
    match = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?", result.stdout.strip())
    if match is None:
        raise click.ClickException(
            f"could not parse docker compose version: {result.stdout.strip()!r}"
        )
    # A missing patch component parses as 0 (conservative: bare `2.24` is treated as
    # 2.24.0 and rejected for the `!override` tag that needs 2.24.4).
    version = (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
    if version[:2] < _MIN_COMPOSE:
        raise click.ClickException(
            f"docker compose {version[0]}.{version[1]} is too old — need "
            f">= {_MIN_COMPOSE[0]}.{_MIN_COMPOSE[1]} for compose `include` support"
        )
    # NE-087 (O-4): the rebac profile rides the null-env --env-file pass-through
    # (FIX-1); require >= 2.24 as belt-and-braces when it is selected.
    if rebac and version[:2] < _MIN_COMPOSE_REBAC:
        raise click.ClickException(
            f"docker compose {version[0]}.{version[1]} is too old for --profile rebac "
            f"— need >= {_MIN_COMPOSE_REBAC[0]}.{_MIN_COMPOSE_REBAC[1]} (null-env "
            "--env-file pass-through)"
        )
    # NE-DEMO-HTTPS-PORT: a non-443 --https-port needs the `!override` merge tag.
    if ports_override and version < _MIN_COMPOSE_OVERRIDE:
        raise click.ClickException(
            f"docker compose {version[0]}.{version[1]}.{version[2]} is too old for "
            f"--https-port != 443 — need >= {_MIN_COMPOSE_OVERRIDE[0]}."
            f"{_MIN_COMPOSE_OVERRIDE[1]}.{_MIN_COMPOSE_OVERRIDE[2]} (the compose "
            "`!override` merge tag that replaces the pinned 443 publish)"
        )


def _bind_probe(
    port: int,
    published: tuple[int, int] = _OFF_BOX_PORTS,
    *,
    bind_host: str = "0.0.0.0",
) -> None:
    """Bind-probe a single off-box port; raise ClickException iff it is in use.

    A PermissionError (binding a privileged port as non-root) is NOT a failure —
    the docker daemon binds ports as root at `up` time; this preflight only
    catches an already-listening conflict. ``published`` names the two host ports
    Caddy publishes (default 443/8443; NE-DEMO-HTTPS-PORT overrides the first).
    ``bind_host`` must match the address the generated Compose ports publish on:
    ordinary Pilot starts bind all interfaces, while an externally fronted demo
    binds only loopback so a listener on the host's public address is not a
    collision.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind_host, port))
    except PermissionError:
        return
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            endpoint = f"port {port}" if bind_host == "0.0.0.0" else f"{bind_host}:{port}"
            raise click.ClickException(
                f"{endpoint} is already in use — free it before `ne up` "
                f"(Caddy publishes {published[0]}/{published[1]})"
            ) from exc
        return
    finally:
        sock.close()


def _check_ports_free(
    ports: tuple[int, int] = _OFF_BOX_PORTS, *, bind_host: str = "0.0.0.0"
) -> None:
    for port in ports:
        if bind_host == "0.0.0.0":
            _bind_probe(port, ports)
        else:
            _bind_probe(port, ports, bind_host=bind_host)


def _current_project_publishes_ports(ports: tuple[int, int], *, bind_host: str) -> bool:
    """Whether the running Pilot Caddy owns both exact requested publishes.

    This is the narrow idempotent-demo re-up exception to the empty-port
    preflight.  It is not caller assertion: Docker must report exactly one
    running ``ne-pilot``/``caddy`` service container and that container must
    carry each expected host-address/port binding.  Any Docker error, malformed
    inspection, foreign label, missing binding, or ambiguity returns ``False``
    so the ordinary bind probe remains authoritative and fails closed.
    """
    listed = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={_PROJECT_NAME}",
            "--filter",
            "label=com.docker.compose.service=caddy",
            "--filter",
            "status=running",
            "--format",
            "{{.ID}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    container_ids = listed.stdout.split() if listed.returncode == 0 else []
    if len(container_ids) != 1:
        return False
    inspected = subprocess.run(
        ["docker", "inspect", container_ids[0]],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspected.returncode != 0:
        return False
    try:
        documents = json.loads(inspected.stdout)
        if not isinstance(documents, list) or len(documents) != 1:
            return False
        document = documents[0]
        labels = document["Config"]["Labels"]
        published = document["NetworkSettings"]["Ports"]
        if (
            labels.get("com.docker.compose.project") != _PROJECT_NAME
            or labels.get("com.docker.compose.service") != "caddy"
        ):
            return False
        for container_port, host_port in zip(("443/tcp", "8443/tcp"), ports, strict=True):
            bindings = published.get(container_port)
            if not isinstance(bindings, list) or not any(
                isinstance(binding, dict)
                and binding.get("HostIp") == bind_host
                and binding.get("HostPort") == str(host_port)
                for binding in bindings
            ):
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _docker_data_root() -> Path:
    """Filesystem the docker daemon actually stores images/volumes on.

    Docker's data-root is relocatable off `/` (the NE-073 no-egress harness
    points it at /mnt), so the disk preflight must stat IT, not `/`. Fail-open to
    `/` when it cannot be determined (docker unreachable, garbage/non-absolute
    output); on remote/VM daemons (Docker Desktop) the reported path may not exist
    on the client host, so walk up to the nearest existing ancestor.
    """
    result = subprocess.run(
        ["docker", "info", "--format", "{{ .DockerRootDir }}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return Path("/")
    raw = result.stdout.strip()
    if not raw.startswith("/"):
        return Path("/")
    path = Path(raw)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _check_disk() -> None:
    target = _docker_data_root()
    try:
        free = shutil.disk_usage(target).free
    except OSError:
        target = Path("/")
        free = shutil.disk_usage(target).free
    if free < _MIN_DISK_BYTES:
        raise click.ClickException(
            f"insufficient disk at {target} (docker data-root filesystem): "
            f"{free // 1024**3} GiB free, need >= {_MIN_DISK_BYTES // 1024**3} GiB"
        )


def _check_bundle(bundle: Path) -> None:
    from ne.cli.pilotcfg import verify

    manifest = bundle / "manifest.json"
    if not manifest.is_file():
        raise click.ClickException(
            f"bundle {bundle} has no manifest.json — run `ne pilot promote` first"
        )
    # Reuse the bundle verifier (file hashes + composed config_hash).
    ctx = click.Context(verify)
    try:
        ctx.invoke(verify, bundle_dir=bundle)
    except click.ClickException as exc:
        raise click.ClickException(f"bundle verify failed: {exc.message}") from exc


def _check_gpu() -> None:
    result = subprocess.run(
        ["docker", "info", "--format", "{{json .Runtimes}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or "nvidia" not in result.stdout:
        raise click.ClickException(
            "no NVIDIA runtime detected — use --serve-cpu (CPU-mode tiny-model "
            "vLLM, the CI/no-GPU path) or install nvidia-container-toolkit; the "
            "GPU host is Azure-owner-gated"
        )


# ---------------------------------------------------------------------------
# Secret injection
# ---------------------------------------------------------------------------
def _write_env_file(bundle: Path, values: dict[str, str]) -> Path:
    """Write `values` to the bundle's runtime/pilot.env at 0600, sorted keys.

    Owner-only, truncate+create with the restrictive mode atomically. Shared by
    the secret-resolving `ne up` path and the placeholder-only `ne down` path so
    both keep identical 0600 discipline.

    Every value is serialized by the single shared ``ne.cli.envfile.dotenv_line``
    (the single-quote idiom, NE-CLI-DOTENV-UNIFY) — the same serializer used by
    the demo live-writeback env_file, so both compose-go dotenv consumers in
    this repo share one wire-format implementation instead of two independently
    drifting ones. All lines are computed BEFORE the runtime dir is created or
    the file is opened, so a value that ``dotenv_line`` rejects raises before
    any byte is written and leaves no partial ``pilot.env`` behind.

    This file is the first place an externally-supplied, operator-pasted value
    can flow through it — the Entra client secret (NE-BEW-INJECT) — every
    prior value here was tool-generated (hex tokens) or a manifest hostname.
    ``dotenv_line`` fails loud, naming the offending KEY only (never the value —
    C12), on a newline/carriage-return, a control character, or an embedded
    single quote — none of which any value reachable today (Azure client-secret
    charset ``A-Za-z0-9._~-``, GUIDs, hex tokens, hostnames) can contain.

    Intentional behavior change from the prior ``$``-doubling escape: a literal
    ``$`` is now passed through UNCHANGED (single-quoted values are taken fully
    literally by compose-go's dotenv parser, so no escaping is needed at all).
    An embedded ``'`` (newly rejected — the deleted per-value loop never
    checked for it, so it would previously have been written raw and silently
    mangled by compose) and a trailing odd backslash run (ditto) now fail
    loud instead. Control characters were ALREADY rejected before this
    refactor (the deleted per-value loop had its own control-char check); that
    guard is carried over unchanged, not new. None of these branches is
    reachable by any value in use today (see the charset note above).

    ``dotenv_line`` also rejects a KEY that is not a plain env var name.
    That matters here specifically: this function's key set is
    ``{**REQUIRED_SECRETS, **from_bundle}``, and ``from_bundle`` is parsed
    from ``secrets/required.json``, so env-var NAMES are bundle-supplied
    rather than compile-time constants.

    Shared by ``ne up`` (resolved-secret values) and ``ne down`` (fixed
    ``_TEARDOWN_PLACEHOLDER`` values plus the manifest hostname).
    """
    lines = [dotenv_line(key, values[key]) for key in sorted(values)]
    runtime_dir = bundle / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    env_path = runtime_dir / "pilot.env"
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("".join(lines))
    os.chmod(env_path, 0o600)
    return env_path


def _rebac_config(bundle: Path) -> dict[str, str]:
    """Read config/ne-rebac.toml (NE-087). Empty ids ⇒ ReBAC not yet bootstrapped.

    Returns ``store_id`` / ``authorization_model_id`` / ``api_url`` (defaulting the
    URL to the in-VPC ``http://openfga:8080`` HTTP API — 8081 is gRPC).
    """
    import tomllib

    path = bundle / "config" / "ne-rebac.toml"
    data: dict[str, object] = {}
    if path.is_file():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        "store_id": str(data.get("store_id", "") or ""),
        "authorization_model_id": str(data.get("authorization_model_id", "") or ""),
        "api_url": str(data.get("api_url", "") or "http://openfga:8080"),
    }


def _rebac_env_values(bundle: Path, resolved: dict[str, str]) -> dict[str, str]:
    """The five NE_REBAC_* app-env values (FIX-1), or ``{}`` if not bootstrapped.

    ``{}`` when store_id/authorization_model_id are empty — the server can start
    (bootstrap phase) but ne-api/ne-worker receive NO NE_REBAC_* keys, so the
    resolver stays disabled until the ids are sealed into the bundle.
    """
    cfg = _rebac_config(bundle)
    if not cfg["store_id"] or not cfg["authorization_model_id"]:
        return {}
    return {
        "NE_REBAC_ENABLED": "true",
        "NE_REBAC_API_URL": cfg["api_url"],
        "NE_REBAC_STORE_ID": cfg["store_id"],
        "NE_REBAC_AUTHORIZATION_MODEL_ID": cfg["authorization_model_id"],
        "NE_REBAC_API_TOKEN": resolved["NE_PILOT_OPENFGA_API_TOKEN"],
    }


#: PRE-GRAPH-ON: the ONE Tier-2 activation key. Declared NULL-VALUED in the pilot
#: compose shared ``x-ne-app-env`` anchor, so compose omits it from BOTH container
#: envs unless this exact name is written into the ephemeral ``--env-file``.
_GRAPH_ACTIVATION_ENV = "NE_BRAIN_GRAPH_ENABLED"
#: The prefix scrubbed from the compose subprocess env. ``NE_BRAIN_GRAPH_DATABASE_URL``
#: and ``NE_BRAIN_GRAPH_GRAPH_NAME`` are compose LITERALS, not interpolations, so
#: scrubbing the whole prefix cannot change the graph a bring-up addresses; it can
#: only stop an ambient export from deciding whether the tier is on.
_GRAPH_ENV_PREFIX = "NE_BRAIN_GRAPH_"


def _write_runtime_env(bundle: Path, *, rebac: bool = False, graph: bool = False) -> Path:
    """Resolve required secrets from the bundle SecretStore into a 0600 env file.

    Values are NEVER echoed or logged. A missing secret fails loud listing the
    env-var NAMES only. Returns the path to the written env file. Secret
    resolution itself is shared with the K8s installer via
    ``ne.cli.pilotcfg.resolve_required_secrets`` (same SecretsSettings/
    create_secret_store code path, NE-071a) — this function's remaining job is
    just the pilot Compose tier's ephemeral 0600 env-file shape.

    PRE-GRAPH-ON: when ``graph`` is set, the ONE Tier-2 activation key
    ``NE_BRAIN_GRAPH_ENABLED=true`` is merged in. Without ``graph`` the file
    carries ZERO ``NE_BRAIN_GRAPH_*`` keys, so compose omits the null-valued
    anchor declaration and both roots freeze the byte-identical default
    ``config_hash`` — activation is a deployment decision, never a per-request one.

    NE-087 (O-4, FIX-1): when ``rebac`` is set, the five NE_REBAC_* app-env values
    are merged in (token from the resolved SecretStore, ids from
    config/ne-rebac.toml) so the resolver ACTUALLY activates. Without ``rebac`` the
    file carries ZERO NE_REBAC_* keys ⇒ compose omits them ⇒ default-off container
    env is byte-identical.

    NE-BEW-INJECT: the three optional Entra broker secrets
    (``ne.cli.pilotcfg.OPTIONAL_SECRETS``) are resolved best-effort and merged
    in. Absent ``pilot/entra-broker`` ⇒ zero ``NE_PILOT_ENTRA_*`` keys land in
    the env file ⇒ the keycloak service's null-value pass-through declarations
    for those same three names (``deploy/compose/pilot/compose.yaml``, the
    NE-087 NE_REBAC_* idiom) make compose OMIT the keys from the container env
    ENTIRELY — the same container-env shape the U1 realm merged with, proven
    at runtime by ``.github/workflows/no-egress.yml``'s real ``ne up`` against
    a bundle with no ``pilot/entra-broker`` path. Present ⇒ Keycloak's
    ``--import-realm`` env substitution receives the real tenant id / client
    id / client secret.
    """
    from ne.cli.pilotcfg import resolve_optional_secrets, resolve_required_secrets

    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    values = resolve_required_secrets(bundle)
    values["NE_PILOT_HOSTNAME"] = manifest["hostname"]
    values.update(resolve_optional_secrets(bundle))
    if rebac:
        values.update(_rebac_env_values(bundle, values))
    if graph:
        values[_GRAPH_ACTIVATION_ENV] = "true"
    return _write_env_file(bundle, values)


def _agent_registry_raw_sha256(root: Path) -> str:
    """Hash the exact repository Agent registry bytes mounted into both processes."""
    return hashlib.sha256((root / "deploy/agents/registry.toml").read_bytes()).hexdigest()


def _skill_bundle_raw_sha256(root: Path) -> str:
    """Hash the exact Skill bytes mounted into API and worker."""

    path = root / "deploy/skills/meridian-hardship-draft/SKILL.md"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scrubbed_child_env(root: Path) -> dict[str, str]:
    """Compose env with ambient optional config stripped and registry hash injected.

    Compose gives a shell-exported variable PRECEDENCE over the same name defined
    in ``--env-file`` when resolving ``${VAR}`` interpolations and null-valued
    pass-through keys in compose.yaml (this is core compose interpolation-source
    ordering, not a bug) — so an
    ambient host NE_REBAC_* (FIX-1) or NE_PILOT_ENTRA_* (NE-BEW-INJECT) could
    leak into / override the resolved value, or inject Entra credentials into a
    deployment whose bundle has no `pilot/entra-broker` path at all, silently
    breaking the "absence is a clean no-op" contract. Always hand compose a
    scrubbed env so ONLY the ephemeral --env-file drives these values. The
    non-secret Agent registry hash is computed from the exact mounted repository
    bytes and overwrites ambient state for every admitted invocation.
    """
    child_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("NE_REBAC_")
        and not k.startswith("NE_PILOT_ENTRA_")
        # PRE-010G: only the generated live-writeback demo overlay may activate
        # canonical Ask. A caller-exported value must not turn on either root or
        # install the worker's database fence during an ordinary `ne up`.
        and k != _DEFAULT_ASK_ACTIVATION_ENV
        # PRE-GRAPH-ON: rolling Tier-2 back is `ne up` WITHOUT --graph. That must
        # land the tier OFF even on an operator shell that still exports
        # NE_BRAIN_GRAPH_ENABLED=true from the activated run — compose gives a
        # shell-exported name precedence over the --env-file, so without this the
        # rollback would silently leave the graph on.
        and not k.startswith(_GRAPH_ENV_PREFIX)
        # PRE-009A (A2): an ambient KEK on the operator's host must never beat the
        # value resolved from the bundle SecretStore into the ephemeral --env-file.
        # Same failure the NE_PILOT_ENTRA_ prefix fixed, with worse consequences:
        # a stale ambient key silently makes every stored private row unopenable.
        and not k.startswith("NE_AGENT_PRIVATE_PAYLOAD_")
        and k != _AGENT_REGISTRY_HASH_ENV
        and k != _SKILL_BUNDLE_HASH_ENV
    }
    child_env[_AGENT_REGISTRY_HASH_ENV] = _agent_registry_raw_sha256(root)
    child_env[_SKILL_BUNDLE_HASH_ENV] = _skill_bundle_raw_sha256(root)
    return child_env


def _write_teardown_env(bundle: Path) -> Path:
    """Interpolation-only env for `ne down` — NEVER touches the SecretStore.

    compose.yaml uses ${NE_PILOT_*:?} so every var must be DEFINED non-empty for
    the file to parse — but teardown never starts a container, so the VALUES are
    irrelevant. An operator whose secrets are rotated/unavailable must still be
    able to stop the stack. Fallbacks keep teardown possible even on a bundle with
    a damaged required.json / manifest.json.

    NE-BEW-INJECT: the optional `NE_PILOT_ENTRA_*` vars are deliberately
    EXCLUDED here — they use compose's null-value pass-through form (never
    `:?`), so they parse fine when simply undefined and need no placeholder.
    """
    from ne.cli.pilotcfg import REQUIRED_SECRETS

    try:
        raw = (bundle / "secrets" / "required.json").read_text(encoding="utf-8")
        from_bundle: dict[str, dict[str, str]] = json.loads(raw)
    except (OSError, ValueError):
        # ValueError covers a present-but-corrupt required.json (JSONDecodeError)
        # — same fallback as a missing file; teardown must still work.
        from_bundle = {}
    # UNION of REQUIRED_SECRETS and the bundle's recorded names so an old bundle
    # (missing NE_PILOT_OPENFGA_API_TOKEN) still parses the ${...:?} vars at `down`.
    required = {**REQUIRED_SECRETS, **from_bundle}
    values = {env_var: _TEARDOWN_PLACEHOLDER for env_var in required}
    try:
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        values["NE_PILOT_HOSTNAME"] = manifest["hostname"]
    except (OSError, ValueError, KeyError):
        values["NE_PILOT_HOSTNAME"] = _TEARDOWN_PLACEHOLDER
    return _write_env_file(bundle, values)


def _remove_runtime_env(env_path: Path) -> None:
    """Delete the ephemeral runtime env so plaintext secrets never sit at rest.

    Called in a `finally` after every compose invocation: compose reads the
    env-file at `up`/`down` time to interpolate `${NE_PILOT_*}`, and running
    containers keep their resolved env, so the file has no further use. `down`
    regenerates a PLACEHOLDER-value env on demand when absent (never reading the
    SecretStore), keeping teardown fully idempotent.
    Best-effort — a leftover here is a bug, never a hard failure of the command.
    """
    with contextlib.suppress(OSError):
        env_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Profile expansion
# ---------------------------------------------------------------------------
def _expand_profiles(profiles: tuple[str, ...], *, serve_gpu: bool, serve_cpu: bool) -> list[str]:
    expanded: set[str] = set()
    for profile in profiles:
        if profile == "pilot":
            expanded.update(_PILOT_EXPANSION)
        elif profile == "agents":
            click.echo(
                "note: the `agents` profile ships with the first-party app images "
                "(post NE-067); nothing to start yet — skipping.",
                err=True,
            )
        else:
            expanded.add(profile)
    if serve_gpu:
        expanded.add("gpu")
    if serve_cpu:
        expanded.add("cpu")
    return sorted(expanded)


def _run(cmd: list[str], env: dict[str, str] | None = None) -> int:
    try:
        return subprocess.run(cmd, check=False, env=env).returncode
    except FileNotFoundError:
        click.echo("docker not found — install Docker Desktop or the docker CLI", err=True)
        return 1


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@click.command("up")
@click.option(
    "--profile",
    "profiles",
    multiple=True,
    type=click.Choice(["pilot", "core", "brain", "agents", "obs", "rebac"]),
    default=("pilot",),
    show_default=True,
    help="Profile(s) to start; 'pilot' = core+brain+obs. 'rebac' = OpenFGA (NE-087).",
)
@click.option(
    "--bundle",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Per-customer config bundle (from `ne pilot promote`).",
)
@click.option("--serve-gpu", is_flag=True, help="Also start vLLM on the serve `gpu` profile.")
@click.option("--serve-cpu", is_flag=True, help="Also start vLLM on the serve `cpu` profile.")
@click.option(
    "--graph",
    is_flag=True,
    help="Activate the Tier-2 graph over the admitted persistent AGE substrate "
    "(writes NE_BRAIN_GRAPH_ENABLED=true into the ephemeral env file). Requires "
    "the core profile. Omit it to roll Tier-2 back off.",
)
@click.option(
    "--overlay",
    "overlays",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=(),
    help="Additional compose overlay file(s) layered over the pilot compose "
    "(NE-DEMO-LIVE-SEAMS; the NE-084 pilot-override pattern). Repeatable.",
)
@click.option(
    "--https-port",
    type=click.IntRange(1, 65535),
    default=443,
    show_default=True,
    help="Host port the API (Caddy) is published on WHEN an overlay republishes it "
    "(NE-DEMO-HTTPS-PORT pattern). Adjusts the port preflight + endpoint summary only "
    "— the pinned pilot compose publishes 443 unless an --overlay overrides it. "
    "Keycloak stays on 8443.",
)
@click.option(
    "--ci-age-iidfile",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    hidden=True,
)
def up(
    profiles: tuple[str, ...],
    bundle: Path,
    serve_gpu: bool,
    serve_cpu: bool,
    graph: bool,
    overlays: tuple[Path, ...],
    https_port: int,
    ci_age_iidfile: Path | None,
    port_bind_host: str = "0.0.0.0",
    reuse_existing_project_ports: bool = False,
) -> None:
    """Bring up the pilot stack from a config bundle (hardened, --wait)."""
    if serve_gpu and serve_cpu:
        raise click.ClickException("pass at most one of --serve-gpu / --serve-cpu")
    if https_port == 8443:
        raise click.ClickException("--https-port must differ from the Keycloak publish port 8443")
    if port_bind_host not in {"0.0.0.0", "127.0.0.1"}:
        raise click.ClickException("internal port bind host must be wildcard or loopback")
    root = _find_repo_root(Path.cwd())
    expanded = _expand_profiles(profiles, serve_gpu=serve_gpu, serve_cpu=serve_cpu)
    if not expanded:
        raise click.ClickException("no startable profile selected")
    if ci_age_iidfile is not None and "core" not in expanded:
        raise click.ClickException("CI AGE image override requires the core profile")
    # PRE-GRAPH-ON: the graph tier addresses the postgres service the CORE profile
    # starts, behind the age-admission gate that same profile runs. Activating it on
    # a bring-up that starts neither would compose a graph store against nothing, so
    # this fails closed BEFORE the env file or any overlay is written.
    if graph and "core" not in expanded:
        raise click.ClickException(
            "--graph requires the core profile — the Tier-2 graph is composed over "
            "the admitted persistent AGE substrate that core starts and admits"
        )

    rebac = "rebac" in expanded
    # NE-087 (O-4): enabling rebac ALONGSIDE core requires a bootstrapped store/model
    # pin — otherwise ne-api would start the resolver against empty ids. A rebac-ONLY
    # bring-up is the bootstrap phase itself, so it proceeds with no NE_REBAC_* env.
    if rebac and "core" in expanded:
        cfg = _rebac_config(bundle)
        if not cfg["store_id"] or not cfg["authorization_model_id"]:
            raise click.ClickException(
                "config/ne-rebac.toml has no store_id/authorization_model_id — run the "
                "bootstrap flow first (see the 'ReBAC (rebac profile)' section in "
                "deploy/compose/pilot/README.md) before `ne up ... --profile rebac` with core"
            )

    # Preflights (fail-closed, specific messages).
    _check_docker()
    _check_daemon()
    _check_compose_version(rebac=rebac, ports_override=https_port != 443)
    admitted_age_image: str | None = None
    ci_age_image: str | None = None
    if "core" in expanded:
        # The core profile carries the first-party ne-app image (pull_policy:
        # never) — assert it exists before bring-up, and check the off-box ports.
        _check_app_image()
        if ci_age_iidfile is None:
            admitted_age_image = _check_age_image()
        else:
            ci_age_image = _validate_ci_age_iidfile(ci_age_iidfile)
        # Only the core profile publishes the off-box <https-port>/8443
        # (Caddy/Keycloak); a brain/obs/cpu-only bring-up must not be blocked by an
        # unrelated listener on those ports. NE-DEMO-HTTPS-PORT: probe the chosen
        # publish port (default 443) rather than a hard-coded 443.
        published_ports = (https_port, 8443)
        reusing_project_ports = reuse_existing_project_ports and _current_project_publishes_ports(
            published_ports, bind_host=port_bind_host
        )
        if not reusing_project_ports:
            if port_bind_host == "0.0.0.0":
                _check_ports_free(published_ports)
            else:
                _check_ports_free(published_ports, bind_host=port_bind_host)
    _check_disk()
    _check_bundle(bundle)
    if serve_gpu:
        _check_gpu()

    # NE-PILOT-OPA-BUNDLE: a `core` bring-up stages the real ne-govern rego bundle
    # and layers a GENERATED overlay re-mounting it at /policies:ro — FIRST, so
    # any operator/demo --overlay (e.g. `ne demo up`'s own opa-policies overlay,
    # ctx.invoke'ing this command) still layers AFTER it and wins on the same
    # compose merge target (later `-f` wins). A non-core bring-up (e.g.
    # --profile rebac/brain alone) stages nothing — argv stays byte-identical.
    if "core" in expanded:
        staged = stage_govern_policies(bundle / "runtime" / "opa-policies", root=root)
        opa_overlay = write_compose_opa_overlay(
            bundle / "runtime" / "opa-govern.overlay.yaml", staged
        )
        overlays = (opa_overlay, *overlays)
        if ci_age_image is not None:
            # This private harness overlay is always last. No caller-controlled
            # overlay may replace the two admitted AGE image pins after validation.
            overlays = (*overlays, _write_ci_age_overlay(bundle, ci_age_image))
        elif admitted_age_image is not None and admitted_age_image != NE_POSTGRES_AGE_IMAGE:
            # A containerd-backed store addresses the portable archive by its OCI
            # manifest digest, not the config digest pinned in the base Compose.
            # This validated runtime override is always last so a caller overlay
            # cannot substitute either AGE root after carrier verification.
            overlays = (*overlays, _write_runtime_age_overlay(bundle, admitted_age_image))

    env_file = _write_runtime_env(bundle, rebac=rebac, graph=graph)
    try:
        cmd = [
            *_compose_base(root, env_file, expanded, overlays),
            "up",
            "--detach",
            "--wait",
        ]
        # Always scrub ambient NE_REBAC_* from the compose subprocess env so ONLY the
        # ephemeral --env-file drives the pass-through values (FIX-1).
        rc = _run(cmd, env=_scrubbed_child_env(root))
    finally:
        _remove_runtime_env(env_file)
    if rc == 0:
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        host = manifest["hostname"]
        click.echo(_endpoint_summary(host, expanded, https_port))
    else:
        click.echo(
            f"pilot bring-up failed (rc {rc}); inspect with `docker compose -p {_PROJECT_NAME} ps`",
            err=True,
        )
        raise click.exceptions.Exit(rc)


def _endpoint_summary(host: str, profiles: list[str], https_port: int = 443) -> str:
    lines = [f"Pilot stack is up (project {_PROJECT_NAME}). Endpoints:"]
    if "core" in profiles:
        lines += [
            f"  console          https://{host}:{https_port} (SPA + /api, same-origin via ne-api)",
            f"  keycloak         https://{host}:8443",
            "  postgres         127.0.0.1:5432 (on-VM)",
            "  opa              127.0.0.1:8181 (on-VM)",
            # The in-container Caddy front door is always 443 (only the HOST publish
            # port changes via the NE-DEMO-HTTPS-PORT overlay) — leave byte-identical.
            "  ne-api           (in-network upstream behind Caddy 443)",
        ]
    if "brain" in profiles:
        lines += [
            "  qdrant           127.0.0.1:6333 (grpc 6334) (on-VM)",
            "  tei-embed        127.0.0.1:8085 (on-VM)",
            "  tei-rerank       127.0.0.1:8086 (on-VM)",
        ]
    if "obs" in profiles:
        lines.append("  otel-collector   127.0.0.1:4317 / 4318 (on-VM)")
    if "gpu" in profiles or "cpu" in profiles:
        lines.append("  vllm             127.0.0.1:8090/8091 (on-VM; private-vllm lane)")
    if "rebac" in profiles:
        lines.append("  openfga          127.0.0.1:8080 (on-VM; rebac profile)")
    lines.append(
        f"Tear down with `ne down --bundle <dir>`. Off-box: only https://{host}:{https_port}/8443."
    )
    return "\n".join(lines)


@click.command("down")
@click.option(
    "--bundle",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Per-customer config bundle used to bring the stack up.",
)
@click.option("--volumes", is_flag=True, help="Also remove named volumes.")
def down(bundle: Path, volumes: bool) -> None:
    """Tear down the pilot stack (all profiles, idempotent)."""
    root = _find_repo_root(Path.cwd())
    # An env file must exist for interpolation to resolve during `down`. `up`
    # leaves none behind (it is ephemeral now), so regenerate on demand. Teardown
    # only needs the ${NE_PILOT_*:?} vars DEFINED — it never starts a container —
    # so it uses PLACEHOLDER values and NEVER reads the SecretStore (rotated /
    # unavailable secrets must not block a stop). Only the file WE generate here
    # is swept afterward, never an operator-placed one.
    env_file = bundle / "runtime" / "pilot.env"
    generated = not env_file.is_file()
    if generated:
        env_file = _write_teardown_env(bundle)
    try:
        cmd = [*_compose_base(root, env_file, list(_ALL_DOWN_PROFILES)), "down"]
        if volumes:
            cmd.append("--volumes")
        rc = _run(cmd, env=_scrubbed_child_env(root))
    finally:
        if generated:
            _remove_runtime_env(env_file)
    if rc != 0:
        raise click.exceptions.Exit(rc)
