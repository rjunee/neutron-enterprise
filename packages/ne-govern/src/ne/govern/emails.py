# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The single definition of "a bare email address" for ne-govern.

Extracted from ``ne.govern.guardrails._common`` (which re-exports these names
unchanged) so that modules BELOW the guardrail package can share the same
grammar without an import cycle: ``ne.govern.config`` validates
``RecipientSettings.allowed_recipients`` with it, and
``ne.govern.redaction.exempt`` validates its exemption list with it, while
``ne.govern.guardrails.recipients`` imports ``ne.govern.config`` — so the
predicate cannot live inside ``ne.govern.guardrails``.

This module imports nothing from ``ne.govern``: it is the leaf.
"""

from __future__ import annotations

import re

__all__ = ["email_domain", "is_valid_email"]

# A single bare email address: local@domain, no display name, no whitespace,
# no angle brackets. Deliberately strict — display-name forms ("Bob <b@x>")
# and embedded whitespace are rejected as malformed, not parsed.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,}\Z"
)


def is_valid_email(value: str) -> bool:
    """True iff ``value`` is a single strictly-normalized bare email address."""
    if value != value.strip() or any(ch.isspace() for ch in value):
        return False
    return _EMAIL_RE.match(value) is not None


def email_domain(value: str) -> str:
    """Return the lowercased domain (part after the final ``@``)."""
    return value.rsplit("@", 1)[-1].lower()
