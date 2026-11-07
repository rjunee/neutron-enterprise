# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared, side-effect-free helpers for the C12 guardrails.

Payload accessors + verdict builders + the deterministic email/egress-URL
validators reused by both the recipient-allowlist and tool-arg guardrails, so
the "what counts as a safe egress target" rule has a single definition. The
sibling "what counts as a well-formed bare email address" rule lives one level
down in :mod:`ne.govern.emails` — a leaf module that imports nothing from
``ne.govern``, so ``ne.govern.config`` (which this package imports) and
``ne.govern.redaction.exempt`` can share the SAME grammar without a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ne.contracts.governance import GuardrailVerdict
from ne.contracts.types import Principal


def passed(rule: str, detail: str = "") -> GuardrailVerdict:
    """Build a passing verdict."""
    return GuardrailVerdict(passed=True, rule=rule, detail=detail)


def failed(rule: str, detail: str = "") -> GuardrailVerdict:
    """Build a failing verdict. ``detail`` must NEVER carry raw payload values."""
    return GuardrailVerdict(passed=False, rule=rule, detail=detail)


def get_principal(payload: Mapping[str, Any]) -> Principal | None:
    """Return the payload principal, or ``None`` when missing/wrong type."""
    principal = payload.get("principal")
    return principal if isinstance(principal, Principal) else None


def get_tool(payload: Mapping[str, Any]) -> str | None:
    """Return the payload tool name, or ``None`` when missing/wrong type."""
    tool = payload.get("tool")
    return tool if isinstance(tool, str) and tool else None


def host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    """Exact or ``*.suffix`` wildcard match of ``host`` against the allowlist."""
    host = host.rstrip(".").lower()
    for pattern in allowed_hosts:
        pat = pattern.rstrip(".").lower()
        if pat.startswith("*."):
            if host == pat[2:] or host.endswith("." + pat[2:]):
                return True
        elif host == pat:
            return True
    return False


def validate_egress_url(value: str, allowed_hosts: tuple[str, ...]) -> str | None:
    """Return a failure-rule suffix for a bad egress URL, or ``None`` if allowed.

    Deterministic checks (used by both the recipient/egress guardrail and the
    ``url``-kind tool-arg check):

      * scheme must be http/https               -> 'bad-scheme'
      * userinfo present (user:pass@host)       -> 'userinfo-forbidden'
      * empty host, or host not on the allowlist -> 'not-allowlisted'
    """
    try:
        parts = urlsplit(value)
    except ValueError:
        return "bad-scheme"
    if parts.scheme not in ("http", "https"):
        return "bad-scheme"
    if parts.username is not None or parts.password is not None:
        return "userinfo-forbidden"
    host = parts.hostname
    if not host:
        return "not-allowlisted"
    if not host_allowed(host, allowed_hosts):
        return "not-allowlisted"
    return None
