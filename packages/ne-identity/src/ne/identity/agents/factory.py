# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Construction seam for the agent-credential service.

``store`` and ``sink`` are REQUIRED keyword-only arguments — there is no path
that omits the frozen SecretStore (C1) or the frozen AuditSink (C6). ``settings``
defaults to ``AgentIdentitySettings()`` (constructible with no args).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ne.contracts.governance import AuditSink
from ne.contracts.secrets import SecretStore
from ne.identity.agents.config import AgentIdentitySettings
from ne.identity.agents.service import AgentCredentialService


def create_agent_credential_service(
    settings: AgentIdentitySettings | None = None,
    *,
    store: SecretStore,
    sink: AuditSink,
    clock: Callable[[], datetime] | None = None,
) -> AgentCredentialService:
    """Build an :class:`AgentCredentialService` from settings + required seams."""
    settings = settings if settings is not None else AgentIdentitySettings()
    return AgentCredentialService(store, sink, settings.credentials, clock=clock)
