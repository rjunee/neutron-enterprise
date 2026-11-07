# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FD-bound, output-silent execution of closed adapter programs."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import signal
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ne.contracts.inference import InferenceOperatorCode, InferenceTargetOperation
from ne.inference_operator.manifest import AdapterPolicy, VerifiedFile
from ne.inference_operator.provider_broker import ProviderBrokerClient, ProviderBrokerRefused


@dataclass(frozen=True)
class RunResult:
    code: InferenceOperatorCode
    returncode: int | None


class PinnedRunner:
    """Verify all closed files, then execute one fixed program without a shell."""

    def __init__(
        self,
        *,
        owner_uid: int = 0,
        upgrade_lock: Path = Path("/run/lock/neutron-live-upgrade.lock"),
        provider_grant_dir: Path = Path("/var/lib/neutron/inference-operator/provider-grants"),
        provider_state: Path = Path("/var/lib/neutron/vast/instance.json"),
        provider_client: ProviderBrokerClient | None = None,
    ) -> None:
        self._owner_uid = owner_uid
        self._upgrade_lock = upgrade_lock
        self._provider_grant_dir = provider_grant_dir
        self._provider_state = provider_state
        self._provider_client = provider_client or ProviderBrokerClient()

    @staticmethod
    def _process_start_ticks(pid: int) -> str:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        # comm is parenthesized and may contain spaces, so split only after its
        # final ')'. starttime is field 22, or index 19 in the remaining tail.
        tail = raw.rsplit(")", 1)[1].strip().split()
        if len(tail) <= 19 or not tail[19].isdigit():
            raise PermissionError("operator process identity is unavailable")
        return tail[19]

    def _provider_grant_paths(self, transition_id: str) -> tuple[Path, Path]:
        name = hashlib.sha256(transition_id.encode()).hexdigest()
        return (
            self._provider_grant_dir / f"{name}.json",
            self._provider_grant_dir / f"{name}.consumed",
        )

    def _authorize_provider_grant(
        self,
        operation: InferenceTargetOperation,
        transition_id: str,
        *,
        deadline_at: float,
    ) -> tuple[Path, Path] | None:
        if operation not in {InferenceTargetOperation.STATUS, InferenceTargetOperation.START}:
            return None
        remaining = deadline_at - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise PermissionError("provider grant deadline has expired")
        directory = self._provider_grant_dir
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = directory.lstat()
        if (
            directory.is_symlink()
            or directory.resolve(strict=True) != directory
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != self._owner_uid
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise PermissionError("provider grant directory is unsafe")
        active, consumed = self._provider_grant_paths(transition_id)
        if active.exists() or consumed.exists():
            raise PermissionError("provider transition grant already exists")
        raw_state = b""
        before = self._provider_state.lstat()
        state_descriptor = os.open(
            self._provider_state,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            info = os.fstat(state_descriptor)
            if (
                not self._provider_state.is_absolute()
                or self._provider_state.is_symlink()
                or self._provider_state.resolve(strict=True) != self._provider_state
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != self._owner_uid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
            ):
                raise PermissionError("provider instance state is unsafe")
            while chunk := os.read(state_descriptor, 4096):
                raw_state += chunk
                if len(raw_state) > 4096:
                    raise PermissionError("provider instance state is too large")
        finally:
            os.close(state_descriptor)
        try:
            state = json.loads(raw_state)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermissionError("provider instance state is invalid") from exc
        if not isinstance(state, dict) or set(state) not in (
            {"id"},
            {"host", "id", "map", "ssh_port", "status"},
        ):
            raise PermissionError("provider instance state has an invalid shape")
        instance_id = state["id"]
        if not isinstance(instance_id, int) or isinstance(instance_id, bool) or instance_id <= 0:
            raise PermissionError("provider instance state is not grantable")
        expires_at = datetime.now(UTC) + timedelta(seconds=min(900.0, remaining))
        document = {
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            "instance_id": instance_id,
            "operation": operation.value,
            "owner_pid": os.getpid(),
            "owner_start_ticks": self._process_start_ticks(os.getpid()),
            "policy_sha256": "8f06a75d0c58f058d6b12293c4233bf2d05a10f009377278bbc6af4bcb7ca06d",
            "schema": "ne-inference-provider-grant-authority-v1",
            "target_id": "private-vast-gpu",
            "transition_id": transition_id,
        }
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        descriptor, temporary = tempfile.mkstemp(prefix=".grant-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, active)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
        return active, consumed

    @staticmethod
    def _remove_provider_grant(paths: tuple[Path, Path] | None) -> None:
        if paths is None:
            return
        for path in paths:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def _open_verified(self, expected: VerifiedFile) -> int:
        path = expected.path
        if not path.is_absolute():
            raise PermissionError("adapter executable path is not absolute")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != self._owner_uid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o022
                or path.resolve(strict=True) != path
            ):
                raise PermissionError("adapter executable integrity metadata is unsafe")
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 65536):
                digest.update(chunk)
            if digest.hexdigest() != expected.sha256:
                raise PermissionError("adapter executable digest is not admitted")
            os.lseek(descriptor, 0, os.SEEK_SET)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_upgrade_lock(self) -> int:
        path = self._upgrade_lock
        if not path.is_absolute():
            raise PermissionError("upgrade lock path is not absolute")
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != self._owner_uid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o022
                or path.resolve(strict=True) != path
            ):
                raise PermissionError("upgrade lock integrity metadata is unsafe")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    async def _acquire_upgrade_lock(
        self,
        *,
        deadline_at: float,
    ) -> int | None:
        descriptor = self._open_upgrade_lock()
        try:
            while True:
                try:
                    # Every target operation is a shared reader of the deployed
                    # host configuration. The deployment transaction alone is
                    # the exclusive writer; independent targets must not consume
                    # one another's operation deadline while no deploy runs.
                    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    return descriptor
                except BlockingIOError:
                    remaining = deadline_at - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        os.close(descriptor)
                        return None
                    await asyncio.sleep(min(0.05, remaining))
        except BaseException:
            os.close(descriptor)
            raise

    async def run(
        self,
        policy: AdapterPolicy,
        operation: InferenceTargetOperation,
        *,
        deadline_s: float,
        transition_id: str,
        _direct_provider: bool = False,
    ) -> RunResult:
        deadline_at = asyncio.get_running_loop().time() + deadline_s
        if operation not in policy.operations:
            return RunResult(InferenceOperatorCode.OPERATION_UNSUPPORTED, None)
        upgrade_lock: int | None = None
        if not _direct_provider:
            try:
                # The deployment lock is the generation boundary. Acquire it
                # before opening or hashing any executable or policy file, and
                # retain it through the complete target operation. The
                # provider-side direct runner is already nested inside the
                # root broker invocation holding this lock.
                upgrade_lock = await self._acquire_upgrade_lock(deadline_at=deadline_at)
            except (OSError, PermissionError):
                return RunResult(InferenceOperatorCode.SCRIPT_REFUSED, None)
            if upgrade_lock is None:
                return RunResult(InferenceOperatorCode.DEADLINE_EXCEEDED, None)
        descriptors: list[int] = []
        try:
            descriptors.append(self._open_verified(policy.executable))
            for expected in policy.verified_files:
                descriptors.append(self._open_verified(expected))
        except (OSError, PermissionError):
            for descriptor in descriptors:
                os.close(descriptor)
            if upgrade_lock is not None:
                os.close(upgrade_lock)
            return RunResult(InferenceOperatorCode.SCRIPT_REFUSED, None)

        if policy.provider_egress and not _direct_provider:
            grant_paths: tuple[Path, Path] | None = None
            try:
                grant_paths = self._authorize_provider_grant(
                    operation,
                    transition_id,
                    deadline_at=deadline_at,
                )
                result = await self._provider_client.invoke(
                    operation,
                    transition_id=transition_id,
                    deadline_s=max(0.0, deadline_at - asyncio.get_running_loop().time()),
                )
            except (OSError, PermissionError, ProviderBrokerRefused):
                return RunResult(InferenceOperatorCode.SCRIPT_REFUSED, None)
            finally:
                self._remove_provider_grant(grant_paths)
                if upgrade_lock is not None:
                    os.close(upgrade_lock)
                for descriptor in descriptors:
                    os.close(descriptor)
            return RunResult(result.code, result.returncode)

        executable_fd = descriptors[0]
        fd_root = "/proc/self/fd" if os.path.exists("/proc/self/fd") else "/dev/fd"
        executable = f"{fd_root}/{executable_fd}"
        if not os.path.exists(fd_root):
            if upgrade_lock is not None:
                os.close(upgrade_lock)
            for descriptor in descriptors:
                os.close(descriptor)
            return RunResult(InferenceOperatorCode.SCRIPT_REFUSED, None)
        argv = (executable, *policy.operations[operation])
        environment = dict(policy.environment)
        environment["NE_INFERENCE_OPERATOR"] = "1"
        if policy.adapter_id == "vast-private-rig-v1":
            if len(descriptors) != 2:
                if upgrade_lock is not None:
                    os.close(upgrade_lock)
                for descriptor in descriptors:
                    os.close(descriptor)
                return RunResult(InferenceOperatorCode.SCRIPT_REFUSED, None)
            environment["NE_VAST_OPERATOR_CLI_FD"] = f"{fd_root}/{descriptors[1]}"
            environment["NE_PROVIDER_TRANSITION_ID"] = transition_id
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
                pass_fds=tuple(descriptors),
                start_new_session=True,
            )
            try:
                remaining = deadline_at - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                returncode = await asyncio.wait_for(process.wait(), timeout=remaining)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
                return RunResult(InferenceOperatorCode.DEADLINE_EXCEEDED, None)
            if returncode in {0, 3}:
                return RunResult(InferenceOperatorCode.OBSERVED, returncode)
            return RunResult(InferenceOperatorCode.READINESS_FAILED, returncode)
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), timeout=2.0)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await asyncio.shield(process.wait())
            raise
        except OSError:
            return RunResult(InferenceOperatorCode.SCRIPT_REFUSED, None)
        finally:
            if upgrade_lock is not None:
                os.close(upgrade_lock)
            for descriptor in descriptors:
                os.close(descriptor)


__all__ = ["PinnedRunner", "RunResult"]
