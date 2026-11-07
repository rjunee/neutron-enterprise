# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AllowlistExemptRedactor — deployment-owned addresses survive pre-egress masking.

NE-162 pre-egress redaction walks EVERY string leaf of a frontier-bound body, so
an email address served by a governed tool read reaches the frontier model only
as ``<REDACTED:EMAIL_ADDRESS>`` — a placeholder the model can neither use nor
un-mask. For customer PII that is exactly right. But two address classes in the
outbound body are DEPLOYMENT CONFIGURATION, not customer PII:

* the exact addresses on the send/recipient allowlist
  (``guardrails.recipients.allowed_recipients``) — every one an explicit,
  owner-authorized target of REAL outbound email; and
* the exact configured draft mailboxes (``NE_API_EMAIL_DRAFT_ALLOWED_MAILBOXES``)
  — the deployment's own sending identity.

Masking those breaks the governed outreach loop by design: the model must copy
the recorded recipient from a governed CRM read into ``draft.email.create``, and
a placeholder is (correctly) refused by the recipient guardrail. This wrapper
exempts EXACTLY those configured addresses from redaction — nothing else. Every
other address, in every other field, still redacts.

Exposure surface (stated, not hidden) — READ THIS AS THE ACTUAL SCOPE, it is
wider than "the outreach draft". This wrapper is installed as the SINGLE
pre-egress redactor on the gateway built by ``ne.api.platform._default_gateway``,
and ``LiteLLMGateway._prepare_egress_body`` applies that one redactor to EVERY
``FRONTIER_API`` completion — in ne-api AND in ne-worker, for every principal,
every agent, every field of every body, on every call path. It has no principal,
agent, field, or call-path awareness and cannot acquire any: it sees a string,
not a caller.

So the honest statement is: an exempted address is visible to the frontier
provider whenever ANY frontier-bound body, from ANY principal, happens to
contain it — not only when the governed outreach draft is being composed. What
IS narrow is the SET, not the caller: exactly the configured addresses, and
nothing else, are ever un-masked. Every other address, from every principal,
still redacts. That set is bounded by the same deployment configuration that
already authorizes real email egress to (or from) those addresses, which is why
a global un-mask of it is an acceptable trade — but it is a global one.

Mechanism: exact, boundary-guarded, case-insensitive matches of an exempted
address are swapped for private-use-area sentinels before the inner redactor
runs, then swapped back afterwards. The inner redactor stays the fail-closed
:class:`~ne.govern.redaction.stage.RedactionStage`; if it raises, this wrapper
propagates and no text escapes. If the incoming text already contains a
sentinel codepoint (adversarial input), the exemption is SKIPPED and the text
is redacted normally — fail closed toward masking, never toward leaking.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ne.contracts.governance import RedactionResult, Redactor
from ne.govern.emails import is_valid_email
from ne.govern.errors import RedactionConfigError

__all__ = ["AllowlistExemptRedactor"]

# Private-use-area sentinels: never produced by the regex backend, survive the
# Presidio isolation round-trip, and vanishingly unlikely in real text. Their
# presence in INPUT text disables the exemption for that call (fail closed).
_SENTINEL_OPEN = "\ue000"
_SENTINEL_CLOSE = "\ue001"
_SENTINEL_RE = re.compile(f"{_SENTINEL_OPEN}(\\d+){_SENTINEL_CLOSE}")

# Boundary guards mirroring the email-atom grammar: an exempted address embedded
# in a LONGER address (``xowner@example.com``, ``owner@example.com.evil.com``) is a
# DIFFERENT address and must keep redacting. A trailing dot followed by
# non-domain text (end of sentence) does not block the exemption.
_LEFT_GUARD = r"(?<![A-Za-z0-9._%+\-!#$&'*/=?^`{|}~])"
_RIGHT_GUARD = r"(?!(?:[A-Za-z0-9-]|\.[A-Za-z0-9]))"


class AllowlistExemptRedactor:
    """Redactor wrapper: exact configured addresses are never masked.

    Every entry MUST be a full bare email address. An entry that is not one —
    a bare domain (``example.com``), a bare local part (``ryan``), a Graph mailbox
    object id — is NOT an address here, it is a SUBSTRING: ``re.escape(entry)``
    plus the token-boundary guards below would un-mask that substring wherever
    it appears at a boundary, so ``example.com`` would carry ``attacker@example.com``
    to the frontier provider. There is no reading of the config under which that
    is what an operator meant, so it is a construction-time
    :class:`~ne.govern.errors.RedactionConfigError` — the composition root wires
    this redactor at boot, so a malformed entry refuses the boot instead of
    silently widening the exemption (fail closed, and loud).
    """

    def __init__(self, inner: Redactor, exempt_addresses: Sequence[str]) -> None:
        self._inner = inner
        cleaned = sorted({a.strip() for a in exempt_addresses if a.strip()}, key=str.casefold)
        malformed = [address for address in cleaned if not is_valid_email(address)]
        if malformed:
            raise RedactionConfigError(
                "redaction exemption entries must each be a full bare email address "
                f"(local@domain); refusing to build the exempt redactor for {malformed!r}. "
                "A bare domain or local part is not an address here — it is a substring "
                "wildcard that would un-mask every address containing it."
            )
        self._patterns = tuple(
            re.compile(_LEFT_GUARD + re.escape(address) + _RIGHT_GUARD, re.IGNORECASE)
            for address in cleaned
        )

    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        if not self._patterns or _SENTINEL_OPEN in text or _SENTINEL_CLOSE in text:
            # No exemptions configured, or sentinel collision: redact normally.
            return self._inner.redact(text, categories=categories)

        held: list[str] = []

        def _protect(match: re.Match[str]) -> str:
            held.append(match.group(0))
            return f"{_SENTINEL_OPEN}{len(held) - 1}{_SENTINEL_CLOSE}"

        protected = text
        for pattern in self._patterns:
            protected = pattern.sub(_protect, protected)
        result = self._inner.redact(protected, categories=categories)
        restored = _SENTINEL_RE.sub(lambda m: held[int(m.group(1))], result.text)
        return RedactionResult(text=restored, redactions=result.redactions)
