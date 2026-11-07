# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Provider execution behind the non-root broker and deny-by-default netns."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ne.contracts.inference import InferenceOperatorCode, InferenceTargetOperation
from ne.inference_operator.manifest import AdapterPolicy
from ne.inference_operator.provider_broker import BrokerResult, ProviderBrokerRefused
from ne.inference_operator.runner import PinnedRunner

PROVIDER_AUTHORITY = "console.vast.ai:443"
NFT_POLICY_SHA256 = "8f06a75d0c58f058d6b12293c4233bf2d05a10f009377278bbc6af4bcb7ca06d"
_RECEIPT_KEYS = {
    "addresses",
    "authority",
    "expires_at",
    "netns_inode",
    "policy_sha256",
    "schema",
}


def _exact_dict(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ProviderBrokerRefused(f"{label} has an invalid closed shape")
    return value


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProviderBrokerRefused("provider egress receipt expiry is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProviderBrokerRefused("provider egress receipt expiry is invalid") from exc
    if parsed.tzinfo is None:
        raise ProviderBrokerRefused("provider egress receipt expiry is invalid")
    return parsed.astimezone(UTC)


class ProviderEgressReceipt:
    """Validate activation-owned nft/DNS evidence inside the broker namespace."""

    def __init__(
        self,
        path: Path = Path("/run/neutron/inference-provider/egress-receipt.json"),
        *,
        owner_uid: int = 0,
        reader_gid: int = 10002,
        network_namespace: Path = Path("/proc/self/ns/net"),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = path
        self._owner_uid = owner_uid
        self._reader_gid = reader_gid
        self._network_namespace = network_namespace
        self._now = now or (lambda: datetime.now(UTC))

    def validate(self) -> None:
        path = self._path
        try:
            before = path.lstat()
            canonical = path.resolve(strict=True)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise ProviderBrokerRefused("provider egress receipt is unavailable") from exc
        try:
            current = os.fstat(descriptor)
            if (
                not path.is_absolute()
                or path.is_symlink()
                or canonical != path
                or not stat.S_ISREG(before.st_mode)
                or before.st_uid != self._owner_uid
                or before.st_gid != self._reader_gid
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o640
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise ProviderBrokerRefused("provider egress receipt metadata is unsafe")
            raw = b""
            while chunk := os.read(descriptor, 65536):
                raw += chunk
                if len(raw) > 8192:
                    raise ProviderBrokerRefused("provider egress receipt is too large")
        finally:
            os.close(descriptor)
        try:
            document = _exact_dict(json.loads(raw), _RECEIPT_KEYS, "provider egress receipt")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderBrokerRefused("provider egress receipt is not valid JSON") from exc
        if (
            document["schema"] != "ne-inference-provider-egress-v1"
            or document["authority"] != PROVIDER_AUTHORITY
            or document["policy_sha256"] != NFT_POLICY_SHA256
            or _utc(document["expires_at"]) <= self._now()
        ):
            raise ProviderBrokerRefused("provider egress receipt authority is refused")
        addresses = document["addresses"]
        if not isinstance(addresses, list) or not 1 <= len(addresses) <= 16:
            raise ProviderBrokerRefused("provider egress address set is refused")
        try:
            normalized = [str(ipaddress.ip_address(value)) for value in addresses]
        except (TypeError, ValueError) as exc:
            raise ProviderBrokerRefused("provider egress address set is refused") from exc
        if normalized != sorted(set(normalized)) or any(
            not ipaddress.ip_address(value).is_global for value in normalized
        ):
            raise ProviderBrokerRefused("provider egress address set is refused")
        netns_inode = document["netns_inode"]
        if isinstance(netns_inode, bool) or not isinstance(netns_inode, int) or netns_inode <= 0:
            raise ProviderBrokerRefused("provider egress namespace receipt is refused")
        try:
            current_inode = self._network_namespace.stat().st_ino
        except OSError as exc:
            raise ProviderBrokerRefused("provider network namespace is unavailable") from exc
        if current_inode != netns_inode:
            raise ProviderBrokerRefused("provider network namespace does not match its receipt")


class VastProviderBackend:
    """Run the admitted Vast adapter only after live sovereignty/egress checks."""

    def __init__(
        self,
        policy: AdapterPolicy,
        *,
        runner: PinnedRunner,
        air_gap: Callable[[], bool],
        egress_receipt: ProviderEgressReceipt,
    ) -> None:
        if policy.adapter_id != "vast-private-rig-v1" or not policy.provider_egress:
            raise ProviderBrokerRefused("provider backend policy is refused")
        self._policy = policy
        self._runner = runner
        self._air_gap = air_gap
        self._egress_receipt = egress_receipt

    async def invoke(
        self,
        operation: InferenceTargetOperation,
        *,
        transition_id: str,
        deadline_at: float,
    ) -> BrokerResult:
        try:
            air_gap = self._air_gap()
        except (Exception, SystemExit):
            return BrokerResult(InferenceOperatorCode.AIR_GAP_REFUSED, None)
        if air_gap:
            return BrokerResult(InferenceOperatorCode.AIR_GAP_REFUSED, None)
        try:
            self._egress_receipt.validate()
        except (Exception, SystemExit):
            return BrokerResult(InferenceOperatorCode.SCRIPT_REFUSED, None)
        remaining = deadline_at - asyncio.get_running_loop().time()
        if remaining <= 0:
            return BrokerResult(InferenceOperatorCode.DEADLINE_EXCEEDED, None)
        result = await self._runner.run(
            self._policy,
            operation,
            deadline_s=remaining,
            transition_id=transition_id,
            _direct_provider=True,
        )
        return BrokerResult(
            result.code,
            result.returncode if result.code is InferenceOperatorCode.OBSERVED else None,
        )


__all__ = [
    "NFT_POLICY_SHA256",
    "PROVIDER_AUTHORITY",
    "ProviderEgressReceipt",
    "VastProviderBackend",
]
