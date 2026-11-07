# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Read-only host checks for the final demo deploy and rehearsal.

This module deliberately has no repair path. It never tags, builds, pulls, starts,
stops, or recreates anything; it only measures the host and inspects artifacts the
operator staged explicitly. The live item-53 vLLM memory ceiling still requires a
measured peak RSS and is outside this preflight.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import click

_GIB = 1024**3
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PROJECT = "ne-pilot"
_SERVICE = "ne-api"
_OVERLAY = Path("overlay/demo-overlay.yaml")
_WRITEBACK_MARKER = Path("demo-writeback.json")
_WRITEBACK_ENV = Path("overlay/writeback.env")
_DOSSIER = Path("DEMO-DOSSIER.md")
_LISTENER_OWNER = re.compile(r'\("(?P<name>[^"]+)",pid=(?P<pid>[0-9]+),fd=[0-9]+\)')


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one read-only Docker query (tests replace this seam completely)."""
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def _host(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one read-only host query (tests replace this seam completely)."""
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def _listener_inventory() -> subprocess.CompletedProcess[str]:
    """Read process-attributed listeners, using non-interactive sudo when required."""
    if os.geteuid() == 0:
        return _host("ss", "-H", "-ltnp")
    sudo = _host("sudo", "-n", "true")
    if sudo.returncode != 0:
        raise click.ClickException(
            "passwordless sudo is required to attribute host listeners; "
            "dedicated-host isolation is unproven"
        )
    return _host("sudo", "-n", "ss", "-H", "-ltnp")


def _dedicated_listener_owners(line: str) -> bool:
    """Require sshd, optionally co-owned only by PID-1 systemd."""
    marker = "users:("
    if marker not in line:
        return False
    payload = line.rsplit(marker, 1)[1].rstrip()
    if not payload.endswith(")"):
        return False
    entries = payload[:-1]
    matches = list(_LISTENER_OWNER.finditer(entries))
    if not matches:
        return False
    cursor = 0
    names: set[str] = set()
    for index, match in enumerate(matches):
        if entries[cursor : match.start()] != ("" if index == 0 else ","):
            return False
        name, pid = match.group("name"), int(match.group("pid"))
        if name not in {"sshd", "systemd"} or (name == "systemd" and pid != 1):
            return False
        names.add(name)
        cursor = match.end()
    return cursor == len(entries) and "sshd" in names


def _fresh_host_isolation() -> None:
    """Fail unless the host is empty of containers and unrelated public listeners."""
    containers = _docker("ps", "--all", "--format", "{{ .ID }}")
    if containers.returncode != 0:
        raise click.ClickException(
            "docker container inventory is unreadable; dedicated-host isolation is unproven"
        )
    if containers.stdout.strip():
        raise click.ClickException(
            "host already contains Docker containers; refusing to provision a shared host"
        )

    listeners = _listener_inventory()
    if listeners.returncode != 0:
        raise click.ClickException(
            "TCP listener inventory is unreadable; dedicated-host isolation is unproven"
        )
    unrelated: list[str] = []
    for raw_line in listeners.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 4:
            raise click.ClickException(
                "TCP listener inventory is malformed; dedicated-host isolation is unproven"
            )
        local = fields[3]
        loopback = (
            local.startswith("127.") or local.startswith("[::1]:") or local.startswith("::1:")
        )
        if not loopback and (not local.endswith(":22") or not _dedicated_listener_owners(line)):
            unrelated.append(local)
    if unrelated:
        raise click.ClickException(
            "host exposes unrelated public TCP listeners: "
            + ", ".join(sorted(set(unrelated)))
            + "; refusing to provision a shared host"
        )


def _docker_data_root() -> Path:
    result = _docker("info", "--format", "{{ .DockerRootDir }}")
    if result.returncode != 0:
        raise click.ClickException("docker daemon is not readable; no host state was changed")
    raw = result.stdout.strip()
    if not raw.startswith("/"):
        raise click.ClickException(
            "docker reported no absolute data-root; refusing a guessed disk check"
        )
    path = Path(raw)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _disk_headroom(minimum_bytes: int) -> tuple[Path, int]:
    target = _docker_data_root()
    try:
        free = shutil.disk_usage(target).free
    except OSError as exc:
        raise click.ClickException(
            f"cannot measure docker data-root filesystem at {target}; refusing to guess"
        ) from exc
    if free < minimum_bytes:
        raise click.ClickException(
            f"insufficient disk at {target} (docker data-root filesystem): "
            f"{free / _GIB:.1f} GiB free, need >= {minimum_bytes / _GIB:.1f} GiB"
        )
    return target, free


def _read_meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(
            f"cannot read Linux memory counters from {path}; refusing a guessed headroom check"
        ) from exc

    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([A-Za-z_()]+):\s+([0-9]+)\s+kB", line.strip())
        if match is not None:
            values[match.group(1)] = int(match.group(2)) * 1024
    required = ("MemAvailable", "SwapTotal", "SwapFree")
    missing = [key for key in required if key not in values]
    if missing:
        raise click.ClickException(
            "memory counters are incomplete (missing " + ", ".join(missing) + "); refusing to guess"
        )
    if values["SwapFree"] > values["SwapTotal"]:
        raise click.ClickException(
            "memory counters are inconsistent; refusing to guess swap pressure"
        )
    return values


def _memory_headroom(
    minimum_available_bytes: int, maximum_swap_used_percent: float
) -> tuple[int, int, int, float]:
    values = _read_meminfo()
    available = values["MemAvailable"]
    swap_total = values["SwapTotal"]
    swap_free = values["SwapFree"]
    swap_used_percent = 0.0 if swap_total == 0 else ((swap_total - swap_free) / swap_total) * 100.0
    if available < minimum_available_bytes:
        raise click.ClickException(
            f"insufficient available memory: {available / _GIB:.1f} GiB, "
            f"need >= {minimum_available_bytes / _GIB:.1f} GiB"
        )
    if swap_used_percent > maximum_swap_used_percent:
        raise click.ClickException(
            f"swap pressure too high: {swap_used_percent:.1f}% used, "
            f"maximum is {maximum_swap_used_percent:.1f}%"
        )
    return available, swap_total, swap_free, swap_used_percent


def _validate_rollback_tag(image: str) -> None:
    from ne.cli.up import NE_APP_IMAGE

    final_component = image.rsplit("/", 1)[-1]
    if "@" in image or ":" not in final_component:
        raise click.UsageError("--rollback-image must be an explicit local image tag, not a digest")
    tag = final_component.rsplit(":", 1)[1]
    if not tag or tag == "latest" or image == NE_APP_IMAGE:
        raise click.UsageError(
            "--rollback-image must be a dedicated non-latest tag distinct from the live image tag"
        )


def _image_id(image: str) -> str:
    result = _docker("image", "inspect", image, "--format", "{{ .Id }}")
    image_id = result.stdout.strip()
    if result.returncode != 0 or _IMAGE_ID.fullmatch(image_id) is None:
        raise click.ClickException(f"required local image tag {image!r} is absent or unreadable")
    return image_id


def _service_block(overlay: str, service: str) -> str:
    blocks = re.findall(
        rf"(?ms)^  {re.escape(service)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", overlay
    )
    if len(blocks) != 1:
        raise click.ClickException(f"demo overlay does not define exactly one {service} service")
    return blocks[0]


def _require_line(block: str, line: str, *, present: bool, message: str) -> None:
    count = block.splitlines().count(line)
    expected = 1 if present else 0
    if count != expected:
        raise click.ClickException(message)


def _validated_rerank_endpoint(endpoint: str) -> tuple[str, str]:
    from urllib.parse import urlparse

    from ne.net.ssrf import UnsafeEndpointError, normalize_host, validate_in_vpc_endpoint

    base = endpoint.rstrip("/")
    host = normalize_host(urlparse(base).hostname or "")
    try:
        validate_in_vpc_endpoint(base, allowed_hosts=(host,))
    except (UnsafeEndpointError, ValueError):
        raise click.UsageError(
            "--gpu-rerank is not a valid credential-free rerank endpoint"
        ) from None
    return base, host


def _check_writeback(workdir: Path, blocks: tuple[str, str], *, mode: str) -> list[str]:
    marker_path = workdir / _WRITEBACK_MARKER
    env_path = workdir / _WRITEBACK_ENV
    enabled_line = '      NE_API_WRITE_ACTIONS_ENABLED: "true"'
    inert_line = '      NE_API_DEMO_WRITEBACK_TOOL: "true"'
    preserve: list[str] = []

    if mode == "inert":
        if marker_path.exists() or env_path.exists():
            raise click.ClickException(
                "write-back expectation is inert but live-writeback artifacts remain staged"
            )
        for block in blocks:
            _require_line(
                block,
                inert_line,
                present=True,
                message="demo overlay does not preserve inert write-back on both app services",
            )
            _require_line(
                block,
                enabled_line,
                present=False,
                message="demo overlay enables real writes despite inert write-back expectation",
            )
        return preserve

    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException("live write-back marker is missing or unreadable") from exc
    manifest_raw = marker.get("manifest_dir")
    if marker.get("mode") != "live-crm" or not isinstance(manifest_raw, str) or not manifest_raw:
        raise click.ClickException("live write-back marker does not authoritatively name live-crm")
    manifest_dir = Path(manifest_raw).expanduser()
    if not manifest_dir.is_dir() or not (manifest_dir / "salesforce.json").is_file():
        raise click.ClickException(
            "live write-back marker points at no readable Salesforce manifest"
        )
    try:
        env_mode = stat.S_IMODE(env_path.stat().st_mode)
    except OSError as exc:
        raise click.ClickException("live write-back env file is missing or unreadable") from exc
    if not env_path.is_file() or env_mode != 0o600:
        raise click.ClickException("live write-back env file must be a regular 0600 file")

    env_line = f'      - "{env_path.resolve()}"'
    for block in blocks:
        _require_line(
            block,
            enabled_line,
            present=True,
            message="demo overlay does not preserve live write-back on both app services",
        )
        # NE-ITEM1-INERT-PARK: the inert tool is KEPT in live mode now (see
        # ``_write_demo_overlay``). Beat 4's swap onto the real governed write is
        # enforced by the agent's Tool grants, never by this binding's absence;
        # dropping it only made the approval-lifecycle self-test unreachable.
        _require_line(
            block,
            inert_line,
            present=True,
            message="demo overlay does not preserve the inert write-back tool in live mode",
        )
        _require_line(
            block,
            env_line,
            present=True,
            message=(
                "demo overlay does not reference the staged write-back env on both app services"
            ),
        )
    preserve.extend(("--live-writeback", "--seed-manifest-dir", str(manifest_dir)))
    return preserve


def _check_rerank(blocks: tuple[str, str], *, mode: str, gpu_rerank: str | None) -> list[str]:
    names = (
        "NE_API_RERANK_BACKEND",
        "NE_API_TEI_RERANK_URL",
        "NE_API_RERANK_ALLOWED_HOSTS",
    )
    if mode == "cpu":
        if gpu_rerank is not None:
            raise click.UsageError("--gpu-rerank is only valid with --rerank-mode gpu")
        if any(
            any(name in line for name in names) for block in blocks for line in block.splitlines()
        ):
            raise click.ClickException("demo overlay still carries GPU rerank state in CPU mode")
        return []

    if gpu_rerank is None:
        raise click.UsageError("--gpu-rerank is required with --rerank-mode gpu")
    endpoint, host = _validated_rerank_endpoint(gpu_rerank)
    lines = (
        '      NE_API_RERANK_BACKEND: "vllm"',
        f"      NE_API_TEI_RERANK_URL: {json.dumps(endpoint)}",
        f"      NE_API_RERANK_ALLOWED_HOSTS: '{json.dumps([host]).replace(chr(39), chr(39) * 2)}'",
    )
    for block in blocks:
        for line in lines:
            _require_line(
                block,
                line,
                present=True,
                message="demo overlay does not preserve the exact GPU rerank lane on both services",
            )
    return ["--gpu-rerank", endpoint]


def _overlay_preservation(
    workdir: Path, *, writeback_mode: str, rerank_mode: str, gpu_rerank: str | None
) -> list[str]:
    path = workdir / _OVERLAY
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(f"demo overlay is missing at {path}") from exc
    blocks = (_service_block(text, "ne-api"), _service_block(text, "ne-worker"))
    return [
        *_check_writeback(workdir, blocks, mode=writeback_mode),
        *_check_rerank(blocks, mode=rerank_mode, gpu_rerank=gpu_rerank),
    ]


def _parse_datetime(value: str, *, what: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise click.ClickException(f"{what} carries an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise click.ClickException(f"{what} timestamp has no timezone")
    return parsed.astimezone(UTC)


def _runtime_env_expectations(
    *, writeback_mode: str, rerank_mode: str, gpu_rerank: str | None
) -> tuple[dict[str, str], set[str]]:
    expected: dict[str, str] = {}
    absent: set[str] = set()
    # NE-ITEM1-INERT-PARK: the inert write-back binding is KEPT in live mode (see
    # ``_check_writeback`` above and ``live._write_demo_overlay``), so the running
    # container carries BOTH keys there. This half MUST mirror the overlay half —
    # ``test_runtime_env_expectations_agree_with_the_deployed_overlay`` fails if it
    # drifts again.
    expected["NE_API_DEMO_WRITEBACK_TOOL"] = "true"
    if writeback_mode == "live-crm":
        expected["NE_API_WRITE_ACTIONS_ENABLED"] = "true"
    else:
        absent.add("NE_API_WRITE_ACTIONS_ENABLED")

    rerank_names = {
        "NE_API_RERANK_BACKEND",
        "NE_API_TEI_RERANK_URL",
        "NE_API_RERANK_ALLOWED_HOSTS",
    }
    if rerank_mode == "gpu":
        if gpu_rerank is None:  # already validated by _overlay_preservation
            raise click.UsageError("--gpu-rerank is required with --rerank-mode gpu")
        endpoint, host = _validated_rerank_endpoint(gpu_rerank)
        expected.update(
            {
                "NE_API_RERANK_BACKEND": "vllm",
                "NE_API_TEI_RERANK_URL": endpoint,
                "NE_API_RERANK_ALLOWED_HOSTS": json.dumps([host]),
            }
        )
    else:
        absent.update(rerank_names)
    return expected, absent


def _container_env_matches(container_id: str, key: str, value: str) -> bool:
    """Check one non-secret env value without reading the container's other env."""
    expected = json.dumps(f"{key}={value}")
    template = f"{{{{range .Config.Env}}}}{{{{if eq . {expected}}}}}match{{{{end}}}}{{{{end}}}}"
    result = _docker("inspect", container_id, "--format", template)
    if result.returncode != 0:
        raise click.ClickException("running ne-api environment metadata is unreadable")
    return result.stdout.strip() == "match"


def _container_env_has_key(container_id: str, key: str) -> bool:
    """Check key presence with a value-suppressing Docker template (C12)."""
    quoted_key = json.dumps(key)
    template = (
        '{{range .Config.Env}}{{if eq (index (split . "=") 0) '
        f"{quoted_key}" + "}}present{{end}}{{end}}"
    )
    result = _docker("inspect", container_id, "--format", template)
    if result.returncode != 0:
        raise click.ClickException("running ne-api environment metadata is unreadable")
    return result.stdout.strip() == "present"


def _running_api_container(
    current_image_id: str, *, expected_env: dict[str, str], absent_env: set[str]
) -> tuple[str, datetime]:
    result = _docker(
        "ps",
        "--filter",
        f"label=com.docker.compose.project={_PROJECT}",
        "--filter",
        f"label=com.docker.compose.service={_SERVICE}",
        "--format",
        "{{ .ID }}",
    )
    containers = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(containers) != 1:
        raise click.ClickException("expected exactly one running ne-pilot ne-api container")
    container_id = containers[0]
    inspected = _docker(
        "inspect",
        container_id,
        "--format",
        "{{ .State.Running }}\n{{ .State.StartedAt }}\n{{ .Image }}",
    )
    metadata = inspected.stdout.splitlines()
    if inspected.returncode != 0 or len(metadata) != 3:
        raise click.ClickException("running ne-api container metadata is unreadable")
    running, started, image_id = metadata
    if running != "true":
        raise click.ClickException("the selected ne-api container is not running")
    if image_id != current_image_id:
        raise click.ClickException("running ne-api does not use the current ne-app image tag")
    for key, expected_value in expected_env.items():
        if not _container_env_matches(container_id, key, expected_value):
            raise click.ClickException(f"running ne-api does not preserve required {key} state")
    for key in absent_env:
        if _container_env_has_key(container_id, key):
            raise click.ClickException(f"running ne-api unexpectedly carries {key} state")
    return container_id, _parse_datetime(started, what="running ne-api container")


def _current_source_matches_image() -> bool:
    from ne.cli.checks import _find_repo_root
    from ne.cli.demo.live import _image_up_to_date

    return _image_up_to_date(_find_repo_root(Path.cwd()))


def _rehearsal(workdir: Path, *, container_started: datetime) -> datetime:
    path = workdir / _DOSSIER
    try:
        text = path.read_text(encoding="utf-8")
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError as exc:
        raise click.ClickException("post-deploy DEMO-DOSSIER.md is missing or unreadable") from exc
    generated = re.findall(r"(?m)^- Generated: .+ → (.+)$", text)
    if len(generated) != 1:
        raise click.ClickException("demo dossier does not carry exactly one generated interval")
    finished = _parse_datetime(generated[0], what="demo dossier")
    if modified < container_started or finished < container_started:
        raise click.ClickException(
            "demo dossier predates the running ne-api; rehearse after deploy"
        )
    if "✅ OK" not in text or "❌ FAILED" in text:
        raise click.ClickException(
            "post-deploy rehearsal dossier has no success or contains a failure"
        )
    audit_match = re.search(r"(?ms)^## Audit-chain verification\s+```json\s+(\{.*?\})\s+```", text)
    if audit_match is None:
        raise click.ClickException("post-deploy rehearsal dossier has no audit-chain result")
    try:
        audit = json.loads(audit_match.group(1))
    except json.JSONDecodeError as exc:
        raise click.ClickException("post-deploy rehearsal audit result is unreadable") from exc
    if audit.get("valid") is not True or type(audit.get("checked")) is not int:
        raise click.ClickException(
            "post-deploy rehearsal did not prove a readable valid audit chain"
        )
    if audit["checked"] <= 0:
        raise click.ClickException("post-deploy rehearsal audit chain checked zero records")
    return finished


def run_host_preflight(
    workdir: Path,
    *,
    phase: Literal["fresh-host", "pre-deploy", "post-deploy"],
    min_disk_free_gib: float,
    min_memory_available_gib: float,
    max_swap_used_percent: float,
    rollback_image: str | None,
    expected_rollback_id: str | None,
    writeback_mode: Literal["inert", "live-crm"],
    rerank_mode: Literal["cpu", "gpu"],
    gpu_rerank: str | None,
) -> None:
    """Run the complete non-mutating host gate and print evidence only on success."""
    minimum_disk = int(min_disk_free_gib * _GIB)
    minimum_memory = int(min_memory_available_gib * _GIB)
    data_root, disk_free = _disk_headroom(minimum_disk)
    memory_available, swap_total, swap_free, swap_used = _memory_headroom(
        minimum_memory, max_swap_used_percent
    )

    if phase == "fresh-host":
        if rollback_image is not None or expected_rollback_id is not None:
            raise click.UsageError("fresh-host phase does not accept rollback image state")
        _fresh_host_isolation()
        click.echo("demo-host preflight: PASS (read-only)")
        click.echo("dedicated isolation: no containers or unrelated public TCP listeners")
        click.echo(f"docker data-root: {data_root} ({disk_free / _GIB:.1f} GiB free)")
        click.echo(f"memory available: {memory_available / _GIB:.1f} GiB")
        click.echo(
            f"swap: {(swap_total - swap_free) / _GIB:.1f}/{swap_total / _GIB:.1f} GiB "
            f"used ({swap_used:.1f}%)"
        )
        return

    if rollback_image is None:
        raise click.UsageError("pre-deploy and post-deploy phases require --rollback-image")
    _validate_rollback_tag(rollback_image)

    from ne.cli.up import NE_APP_IMAGE

    current_id = _image_id(NE_APP_IMAGE)
    rollback_id = _image_id(rollback_image)
    if phase == "pre-deploy":
        if expected_rollback_id is not None:
            raise click.UsageError("--expected-rollback-id is only valid in post-deploy phase")
        if rollback_id != current_id:
            raise click.ClickException(
                "pre-deploy rollback tag does not point at the current ne-app image tag"
            )
    else:
        if expected_rollback_id is None or _IMAGE_ID.fullmatch(expected_rollback_id) is None:
            raise click.UsageError(
                "post-deploy phase requires the exact --expected-rollback-id printed pre-deploy"
            )
        if rollback_id != expected_rollback_id:
            raise click.ClickException("rollback tag moved since the pre-deploy check")

    preserve_args = _overlay_preservation(
        workdir,
        writeback_mode=writeback_mode,
        rerank_mode=rerank_mode,
        gpu_rerank=gpu_rerank,
    )

    post_lines: list[str] = []
    if phase == "post-deploy":
        if not _current_source_matches_image():
            raise click.ClickException(
                "current ne-app image source revision does not match this checkout; "
                "freeze is unproven"
            )
        expected_env, absent_env = _runtime_env_expectations(
            writeback_mode=writeback_mode,
            rerank_mode=rerank_mode,
            gpu_rerank=gpu_rerank,
        )
        container_id, started = _running_api_container(
            current_id, expected_env=expected_env, absent_env=absent_env
        )
        rehearsal_finished = _rehearsal(workdir, container_started=started)
        post_lines = [
            f"running ne-api: {container_id} ({current_id})",
            f"post-deploy rehearsal finished: {rehearsal_finished.isoformat()}",
        ]

    preserve_fragment = (
        shlex.join(preserve_args)
        if preserve_args
        else "(none: inert write-back and CPU rerank are expected)"
    )
    click.echo("demo-host preflight: PASS (read-only)")
    click.echo(f"docker data-root: {data_root} ({disk_free / _GIB:.1f} GiB free)")
    click.echo(f"memory available: {memory_available / _GIB:.1f} GiB")
    click.echo(
        f"swap: {(swap_total - swap_free) / _GIB:.1f}/{swap_total / _GIB:.1f} GiB "
        f"used ({swap_used:.1f}%)"
    )
    click.echo(f"rollback image: {rollback_image} ({rollback_id})")
    click.echo(f"required re-up preservation args: {preserve_fragment}")
    for line in post_lines:
        click.echo(line)


__all__ = ["run_host_preflight"]
