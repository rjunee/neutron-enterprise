# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Conformance kit run against a REAL OpenBao instance — LIVE-PARKABLE.

Skips cleanly when OpenBao is unreachable (verified down on the build host at
plan time — no docker daemon). No HTTP mocking here, ever: this proves the
real ``_request_sync`` transport against the NE-006 dev compose OpenBao
(``openbao/openbao:2.5.5``, KV-v2 mount ``secret``, dev root token
``ne-dev-root``).

Start it with: ``ne dev up`` (or
``docker compose -f deploy/compose/dev/compose.yaml up -d openbao``).
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import cast
from uuid import uuid4

import pytest

from ne.contracts.plugins import PluginRegistry
from ne.contracts.secrets import SecretStore
from ne.contracts.testing.kits.secret_store import SecretStoreContractKit
from ne.secrets.openbao import OpenBaoSecretStore

_OPENBAO_URL = os.environ.get("NE_TEST_OPENBAO_URL", "http://localhost:8200")
_OPENBAO_TOKEN = os.environ.get("NE_TEST_OPENBAO_TOKEN", "ne-dev-root")


def _openbao_available() -> bool:
    try:
        with urllib.request.urlopen(f"{_OPENBAO_URL}/v1/sys/health", timeout=1.0):
            return True
    except urllib.error.HTTPError:
        # OpenBao's /sys/health can return non-200 (e.g. 429/473) while still up.
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _openbao_available(),
    reason="OpenBao not reachable (start deploy/compose/dev: ne dev up)",
)


class TestOpenBaoSecretStoreConformance(SecretStoreContractKit):
    def create_store(self) -> SecretStore:
        resolved = PluginRegistry().resolve("secret_store", "openbao")
        adapter_cls = cast("type[OpenBaoSecretStore]", resolved)
        return adapter_cls(
            url=_OPENBAO_URL,
            token=_OPENBAO_TOKEN,
            mount="secret",
            base_path=f"ne-secrets-kit/{uuid4().hex}",
        )
