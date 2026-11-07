# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Teams conversation-reference cache + serviceUrl validation (NE-080).

Bot Framework outbound replies POST to ``{serviceUrl}/v3/conversations/...`` where
``serviceUrl`` is supplied by the INBOUND activity. :func:`validate_service_url`
gates that URL through the SSRF policy (https, no private/loopback) and pins it to
the configured Bot Connector allowlist; :class:`ConversationRefs` is a bounded LRU
mapping a conversation id to its LAST validated serviceUrl.

Cache-poisoning defense: only a VERIFIED + allowlisted activity's serviceUrl is
ever recorded (the ingress records after auth + validation), so a forged activity
can never redirect a later reply. Proactive messaging to a conversation not seen
since process start is a documented v1 limitation (in-memory refs).
"""

from __future__ import annotations

from collections import OrderedDict
from urllib.parse import urlparse

from ne.chat.errors import TeamsApiError
from ne.chat.teams.config import TeamsChatSettings
from ne.net.ssrf import normalize_host, validate_endpoint_url

__all__ = ["ConversationRefs", "validate_service_url"]


def validate_service_url(settings: TeamsChatSettings, url: str) -> str:
    """Validate an activity ``serviceUrl`` and pin it to the Bot Connector allowlist.

    Raises :class:`TeamsApiError` (status/host-only) on any unsafe or off-allowlist
    URL. Returns the URL with any trailing ``/`` stripped.
    """
    validated = validate_endpoint_url(
        url,
        require_https=True,
        allow_private=False,
        error_cls=TeamsApiError,
    )
    host = normalize_host(urlparse(validated).hostname or "")
    allowed = {normalize_host(h) for h in settings.service_url_allowed_hosts}
    if host not in allowed:
        raise TeamsApiError(f"serviceUrl host {host!r} is not in the configured allowlist")
    return validated


class ConversationRefs:
    """A bounded LRU of ``conversation_id -> validated serviceUrl``."""

    def __init__(self, *, max_size: int) -> None:
        self._max = max_size
        self._refs: OrderedDict[str, str] = OrderedDict()

    def record(self, conversation_id: str, service_url: str) -> None:
        """Store an ALREADY-VALIDATED serviceUrl for a conversation (LRU-bounded)."""
        self._refs[conversation_id] = service_url
        self._refs.move_to_end(conversation_id)
        while len(self._refs) > self._max:
            self._refs.popitem(last=False)

    def lookup(self, conversation_id: str) -> str | None:
        """Return the last validated serviceUrl for ``conversation_id`` (or None)."""
        url = self._refs.get(conversation_id)
        if url is not None:
            self._refs.move_to_end(conversation_id)
        return url
