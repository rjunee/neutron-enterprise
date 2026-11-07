# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Recipient + egress allowlist guardrail (C12) — deny-by-default exfil control.

Blocks any outbound action whose recipient (email) or egress target (URL) is not
explicitly allowlisted. This is *action-recipient* allowlisting — the last
code-enforced line stopping a hijacked agent from mailing/exfiltrating data to an
attacker-controlled address — and complements NE-012's network-level egress
policy. With empty allowlists, ANY outbound recipient is denied (deny-by-default).

A PRESENT-but-malformed ``recipients``/``egress_urls`` shape (anything other than
a string or a sequence of strings — a mapping, a number, a set, bytes, or a
sequence with a non-str item) is itself a BLOCK (``recipients.malformed-shape`` /
``egress.malformed-shape``); only a genuinely absent/None field is treated as
'no-outbound'. Malformed shapes are never coerced or skipped (C12
reject-by-default).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ne.contracts.governance import GuardrailVerdict
from ne.govern.config import RecipientSettings
from ne.govern.emails import email_domain, is_valid_email
from ne.govern.guardrails._common import (
    failed,
    passed,
    validate_egress_url,
)


class RecipientAllowlistGuardrail:
    """Blocks outbound recipients/egress targets that are not allowlisted."""

    def __init__(self, settings: RecipientSettings) -> None:
        self._settings = settings

    def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict:
        recipients = _as_str_seq(payload.get("recipients"))
        if recipients is None:
            return failed(
                "recipients.malformed-shape",
                "recipients is present but not a sequence of strings (reject-by-default)",
            )
        egress_urls = _as_str_seq(payload.get("egress_urls"))
        if egress_urls is None:
            return failed(
                "egress.malformed-shape",
                "egress_urls is present but not a sequence of strings (reject-by-default)",
            )
        if not recipients and not egress_urls:
            return passed("recipients.no-outbound")

        for recipient in recipients:
            verdict = self._check_recipient(recipient)
            if not verdict.passed:
                return verdict

        for url in egress_urls:
            verdict = self._check_egress(url)
            if not verdict.passed:
                return verdict

        return passed("recipients.allowlisted")

    def _check_recipient(self, recipient: str) -> GuardrailVerdict:
        if not is_valid_email(recipient):
            return failed("recipients.malformed", "recipient is not a bare email address")
        allowed = recipient.lower() in {
            a.lower() for a in self._settings.allowed_recipients
        } or email_domain(recipient) in {
            d.lower() for d in self._settings.allowed_recipient_domains
        }
        if not allowed:
            return failed("recipients.not-allowlisted", "recipient not on the allowlist")
        return passed("recipients.recipient-ok")

    def _check_egress(self, url: str) -> GuardrailVerdict:
        problem = validate_egress_url(url, self._settings.allowed_egress_hosts)
        if problem is not None:
            return failed(f"egress.{problem}", f"egress URL rejected: {problem}")
        return passed("egress.host-ok")


def _as_str_seq(value: object) -> tuple[str, ...] | None:
    """Normalize an outbound field. Absent (None) -> (); a bare str -> one-item
    tuple; a sequence of str -> tuple. Anything else — a mapping, a number, a
    bytes value, a set, or a sequence containing a non-str item — returns None:
    PRESENT-BUT-MALFORMED, which the caller MUST turn into a blocking verdict
    (C12 reject-by-default, never silently 'no outbound')."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        items: list[str] = []
        for item in value:
            if not isinstance(item, str):
                return None
            items.append(item)
        return tuple(items)
    return None
