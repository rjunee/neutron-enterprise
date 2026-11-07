# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Package-local errors for the connector framework (NE-040).

Endpoint/egress errors are deliberately NOT defined here: the C2 source-URL
validator in :mod:`ne.connect.endpoints` REUSES
``ne.brain.sovereignty.UnsafeEndpointError`` / ``AirGapViolationError`` rather
than minting a third mirror (aids the NE-CKC10 sovereignty consolidation).
"""

from __future__ import annotations


class NeConnectError(Exception):
    """Base for all package-local ne-connect errors."""


class AclResolutionError(NeConnectError):
    """An ACL could not be resolved to a non-empty principal set — fail-closed.

    Raised by :meth:`ne.connect.acl.AclMapper.resolve` whenever the resolution
    would produce an empty ACL. The record must NOT be written: an unlabeled
    record entering the brain is the NE-CKC2 fail-open lesson this framework
    makes structurally impossible.
    """


class InvalidRecordError(NeConnectError):
    """A source row is missing its declared record-id key (fail loud, never skip)."""


class GraphSyncError(NeConnectError):
    """A Microsoft Graph request failed (NE-044).

    C12 discipline: the message carries the HTTP STATUS and (optionally) the
    request host ONLY — never a response body, a token, a client secret, or the
    full URL (which may embed query-string tokens). Constructed by
    :class:`ne.connect.m365.graph.GraphClient` on token failures and on
    non-retryable / retry-exhausted Graph responses.

    ``status`` carries the HTTP status when the failure was an HTTP response (None
    for guard/transport failures) so callers can distinguish a per-item 4xx from a
    run-level fault.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GraphUnauthorizedError(GraphSyncError):
    """Microsoft Graph returned HTTP 401."""


class GraphForbiddenError(GraphSyncError):
    """Microsoft Graph returned HTTP 403."""


class GraphRateLimitedError(GraphSyncError):
    """Microsoft Graph rate limiting (HTTP 429) exhausted the retry budget."""


class GraphUpstreamError(GraphSyncError):
    """Microsoft Graph returned a server-side HTTP 5xx response."""


class GraphRedirectError(GraphSyncError):
    """A Graph-supplied link or content redirect was refused."""


class GraphContentError(GraphSyncError):
    """Graph content metadata or bytes violated the bounded content contract."""


class GraphParseError(GraphSyncError):
    """Base category for safe Microsoft Graph parsing failures."""


class GraphResponseParseError(GraphParseError):
    """A successful Graph or token response violated its JSON/envelope contract."""


class GraphDocumentParseError(GraphParseError):
    """Fetched document bytes could not be safely parsed or validated."""


class GraphDocumentConversionError(GraphDocumentParseError):
    """Fetched document bytes were rejected during parser conversion."""


class GraphDocumentOutputError(GraphDocumentParseError):
    """Parsed document output violated the admitted M365 body contract."""


class GraphTransportError(GraphSyncError):
    """A Microsoft Graph request transport failed without an HTTP response."""


class DeltaTokenExpiredError(NeConnectError):
    """A Graph ``deltaLink`` token has expired (HTTP 410 Gone — NE-044).

    Signals the caller to re-derive the full-crawl URL from the VALIDATED base and
    resync that drive from scratch (the Graph-documented ``resync`` flow); the
    ``Location`` header on the 410 is deliberately NOT followed — an attacker-
    controlled response must not redirect the crawl off-host.
    """


class SqlSyncError(NeConnectError):
    """A SQL connector read/connection failed (NE-046).

    C12 discipline: the message carries ONLY the exception CLASS name and the
    ``table_ref`` (or ``drivername`` / parsed hostname, for connection-level
    failures) — NEVER the driver's own exception args, which can embed the host,
    user, or password from the connection string. Constructed by
    :mod:`ne.connect.sql.engine` on connection-string / dialect / host-policy
    failures and by :mod:`ne.connect.sql.connector` on a per-table SQLAlchemy read
    failure (which aborts the run — no cursor commits, per the durability
    contract).
    """


class SlackSyncError(NeConnectError):
    """A Slack Web API request failed (NE-045).

    C12 discipline: the message carries the HTTP status, a host, or the short
    Slack error CODE (e.g. ``'not_in_channel'``) ONLY — never the bot token,
    request params, the response body, or the full URL. Constructed by
    :class:`ne.connect.slack.client.SlackClient` on non-retryable / retry-
    exhausted responses and on Slack's ``ok: false`` envelope.

    ``status`` carries the HTTP status when the failure was an HTTP response
    (``None`` for guard/transport failures; ``200`` for an ``ok: false``
    envelope) so callers can distinguish a per-item failure from a run-level
    fault. ``error_code`` carries Slack's short error string when present.
    """

    def __init__(
        self, message: str, *, status: int | None = None, error_code: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code


class SlackRateLimitedError(SlackSyncError):
    """Slack rate limiting persisted through the client's full retry budget
    (NE-CLEAN3 item 2 / NE-CLEAN1 P2 supersession).

    Raised ONLY for HTTP 429 exhaustion, or the ``ok: false error: 'ratelimited'``
    envelope (at any HTTP status) exhausting retries. It subclasses
    :class:`SlackSyncError` so every existing ``except SlackSyncError`` contract
    is preserved unchanged; the Slack connector specifically catches this
    subtype and treats it as a per-channel RETRYABLE SKIP — the channel is
    excluded from the current run (no ``Record``, no ``AclEntry``, no cursor
    advance) and retried on the next run — instead of aborting the whole sync.
    This deliberately supersedes NE-CLEAN1's prior "ratelimited exhaustion
    aborts the run" policy: genuine auth/permission failures (``invalid_auth``,
    ``not_authed``, ``account_inactive``, ``missing_scope``) and 5xx/503/504
    exhaustion remain plain :class:`SlackSyncError` and still abort the run.
    """


class JiraSyncError(NeConnectError):
    """A Jira Cloud REST/Agile API request failed (NE-093).

    C12 discipline: the message carries the HTTP status, a host, or a short
    code ONLY — never the Basic-auth ``email:api_token``, the JQL query text, a
    response body, or the full URL (which may embed a ``nextPageToken`` or JQL).
    Constructed by :class:`ne.connect.jira.client.JiraClient` on API failures, a
    refused path, retry exhaustion, and by :mod:`ne.connect.jira.connector` on a
    run-level fetch fault (all of which ABORT the run — no cursor commits).

    ``status`` carries the HTTP status when the failure was an HTTP response
    (``None`` for guard/transport failures) so callers can distinguish a
    per-item failure from a run-level fault.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ZendeskSyncError(NeConnectError):
    """A Zendesk REST API request failed (NE-093).

    C12 discipline: the message carries the HTTP status, a host, or a short
    code ONLY — never the Basic-auth ``email/token:api_token``, the response
    body, or the full URL (which may embed an ``after_cursor``). Constructed by
    :class:`ne.connect.zendesk.client.ZendeskClient` on API failures, a refused
    path/cursor, retry exhaustion, and by :mod:`ne.connect.zendesk.connector` on
    a run-level fetch fault (all of which ABORT the run — no cursor commits).

    ``status`` carries the HTTP status when the failure was an HTTP response
    (``None`` for guard/transport failures) so callers can distinguish a
    per-item failure from a run-level fault.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CsvSyncError(NeConnectError):
    """The CSV-file connector could not read its configured artifacts (NE-CONNECT-CSV).

    Raised when the polled CSV file or the contract's JSON-Schema file is
    absent/unreadable — a missing source must FAIL the run loudly, never
    surface as a clean empty sync. C12 discipline: the message carries the
    configured PATH and the failure class only — never file contents.
    """


class SalesforceSyncError(NeConnectError):
    """A Salesforce API request failed (NE-047).

    C12 discipline: the message carries the HTTP status, a host, or the short
    Salesforce error CODE ONLY — never the OAuth token, the SOQL query text, a
    response body, or the full URL (which may embed a locator/cursor). Constructed
    by :class:`ne.connect.salesforce.client.SalesforceClient` on token/API
    failures, retry exhaustion, a refused path/job-id, and by
    :mod:`ne.connect.salesforce.connector` on a Bulk job ``Failed``/``Aborted``
    state or poll exhaustion (all of which ABORT the run — no cursor commits).

    ``status`` carries the HTTP status when the failure was an HTTP response
    (``None`` for guard/transport/job-state failures) so callers can distinguish a
    per-item failure from a run-level fault. ``error_code`` carries Salesforce's
    short error string when present.
    """

    def __init__(
        self, message: str, *, status: int | None = None, error_code: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
