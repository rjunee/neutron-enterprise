# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKABLE — full issue->rotate->grace->revoke lifecycle over REAL OpenBao.

Skips cleanly when OpenBao is unreachable (mirrors
``packages/ne-secrets/tests/test_openbao_conformance.py``). Start it with
``ne dev up`` (KV-v2 mount ``secret``, dev root token ``ne-dev-root``). Proves the
credential service against the real ``OpenBaoSecretStore`` KV-v2 transport:
version increments and revoked-version soft-delete happen for real.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from uuid import uuid4

import pytest

from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.errors import SecretNotFoundError
from ne.identity.agents import (
    AgentCredentialConfig,
    AgentCredentialService,
    AgentIdentitySettings,
    AgentRef,
    CredentialScope,
    CredentialState,
)
from ne.identity.errors import CredentialRevokedError
from ne.secrets.openbao import OpenBaoSecretStore

from .conftest import ListSink, MutableClock, run

_OPENBAO_URL = os.environ.get("NE_TEST_OPENBAO_URL", "http://localhost:8200")
_OPENBAO_TOKEN = os.environ.get("NE_TEST_OPENBAO_TOKEN", "ne-dev-root")


def _openbao_available() -> bool:
    try:
        with urllib.request.urlopen(f"{_OPENBAO_URL}/v1/sys/health", timeout=1.0):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _openbao_available(),
    reason="OpenBao not reachable (LIVE-PARKED; start deploy/compose/dev: ne dev up)",
)


def test_issue_rotate_revoke_over_real_openbao() -> None:
    reset_config_hash_for_testing()
    freeze_config_hash(AgentIdentitySettings())
    try:
        clock = MutableClock()
        prefix = f"ne-identity-agents-kit/{uuid4().hex}"
        store = OpenBaoSecretStore(
            url=_OPENBAO_URL,
            token=_OPENBAO_TOKEN,
            mount="secret",
            base_path=prefix,
        )
        svc = AgentCredentialService(
            store,
            ListSink(),
            AgentCredentialConfig(grace_seconds=300.0, rotate_after_seconds=86400.0),
            clock=clock,
        )
        agent = AgentRef(department="sales", agent_name="assistant")

        # issue -> real KV-v2 version 1.
        cred = run(svc.issue(agent, CredentialScope(tools=("read.notes",)), actor="op"))
        assert cred.secret_version == 1
        token1 = run(svc.resolve_secret(cred.credential_id)).get_secret_value()
        assert token1

        # rotate -> real version increment to 2; old lingers in GRACE.
        new = run(svc.rotate(agent, actor="op"))
        assert new.secret_version == 2
        assert svc.credential_state(cred.credential_id) is CredentialState.GRACE
        token2 = run(svc.resolve_secret(new.credential_id)).get_secret_value()
        assert token2 != token1

        # revoke the new credential -> its version is soft-deleted in OpenBao.
        run(svc.revoke(new.credential_id, actor="op"))
        with pytest.raises(CredentialRevokedError):
            run(svc.resolve_secret(new.credential_id))
        with pytest.raises(SecretNotFoundError):
            run(store.read(new.secret_path, version=new.secret_version))
    finally:
        reset_config_hash_for_testing()
