# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy for ne-chat (Slack chat adapter).

C12 secret-hygiene rule (BINDING for every raise in this package): no exception
message may EVER contain the signing secret, the bot token, message text, or raw
Slack payload values. Ingress-rejection and Slack-API errors carry only the
failure CATEGORY, an HTTP status, a short Slack error code, or a host — never a
computed digest, a provided signature, request params, or a response body.
"""

from __future__ import annotations

__all__ = [
    "ActivityAuthError",
    "ApproverNotAuthorizedError",
    "ChatAuditError",
    "ChatError",
    "IngressRejectedError",
    "SignatureVerificationError",
    "SlackApiError",
    "StaleTimestampError",
    "TeamsApiError",
    "UnmappedChatUserError",
]


class ChatError(Exception):
    """Base class for every ne-chat error."""


class IngressRejectedError(ChatError):
    """An inbound Slack request was rejected BEFORE any processing.

    The two concrete subclasses (:class:`SignatureVerificationError`,
    :class:`StaleTimestampError`) both surface as a status-only HTTP 401 at the
    ingress — the response never echoes the secret, the digest, or the payload.
    """


class SignatureVerificationError(IngressRejectedError):
    """The Slack v0 request signature was missing, malformed, or forged."""


class StaleTimestampError(IngressRejectedError):
    """The request timestamp is outside the anti-replay tolerance window."""


class ActivityAuthError(IngressRejectedError):
    """A Bot Framework activity's authorization was missing, malformed, or forged.

    The SINGLE opaque rejection the Teams ingress surfaces as a status-only HTTP
    401 — no branch of the JWT validation leaks WHY (no oracle; the
    OidcTokenVerifier posture). Like the Slack signature rejection it is
    DELIBERATELY NOT audited: an unauthenticated forgery must not be able to flood
    the tamper-evident audit log.
    """


class UnmappedChatUserError(ChatError):
    """A Slack user is not linked to (or does not resolve to) a real Principal.

    Fail-closed: an unmapped/unresolvable Slack user gets NO agent access and NO
    approval power — never an anonymous or ambient principal.
    """


class ApproverNotAuthorizedError(ChatError):
    """A mapped principal is not authorized to action the referenced approval."""


class SlackApiError(ChatError):
    """A Slack Web API call failed.

    C12: carries ONLY the HTTP status, a short Slack error code, and/or the host
    — never the bot token, request params, or the response body.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_code: str | None = None,
        host: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.host = host


class TeamsApiError(ChatError):
    """A Teams / Bot Framework outbound (Connector API or token) call failed.

    C12: carries ONLY the HTTP status and/or the host — never the app secret, the
    bearer token, request params, or the response body.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        host: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.host = host


class ChatAuditError(ChatError):
    """An audit append failed, so the result is withheld (fail closed).

    Mirrors ``ne.runtime.errors.RuntimeAuditError`` semantics: a decision that
    cannot be recorded in the tamper-evident audit log must never look successful.
    """
