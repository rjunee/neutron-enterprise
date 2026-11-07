# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Read-time connector-health filtering shared by the T1 and T2 read paths.

The provider returns connector stamps whose latest server-owned sync fact is
``degraded``.  It is evaluated once per read; callers deliberately do not cache
the result because a stale passing decision would continue serving content after
the audit chain recorded a failed permissions check.

T1 connector writes carry an explicit ``metadata['connector']`` stamp.  An
unstamped chunk is curated/non-connector content and remains available.  A
present but malformed stamp is unprovable connector provenance and is withheld.

Tier-2 callers use the same provider snapshot but own the interpretation of
their graph provenance labels.  This module deliberately contains no graph
writer/source constants: it is the connector-generic T1 seam, not a second
source-system authority.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

CONNECTOR_METADATA_KEY = "connector"


@dataclass(frozen=True, slots=True)
class SourceHealthSnapshot:
    """One read's split view of unhealthy connector stamps.

    ``unavailable`` stamps are withheld in full (the fail-closed leg: a denied
    ACL resolution, a never-verified lane, or a persisted failure run).
    ``stale`` stamps are SERVED — their latest sync attempt failed but the
    corpus is still the last verified sync's admission — and the retriever
    counts what it served from them into the ambient withholding observation so
    the renderer can state the freshness caveat instead of withholding silently.
    """

    unavailable: frozenset[str] = frozenset()
    stale: frozenset[str] = frozenset()


#: A provider may return the full two-way snapshot or (legacy shape) the bare
#: unavailable set; :func:`evaluate_source_health` normalizes both.
type SourceHealthResult = SourceHealthSnapshot | frozenset[str]
type DegradedSourcesProvider = Callable[[], SourceHealthResult | Awaitable[SourceHealthResult]]


async def evaluate_source_health(
    provider: DegradedSourcesProvider | None,
) -> SourceHealthSnapshot:
    """Evaluate one read's health snapshot; provider failures propagate closed."""
    if provider is None:
        return SourceHealthSnapshot()
    raw = provider()
    resolved = await raw if inspect.isawaitable(raw) else raw
    if isinstance(resolved, SourceHealthSnapshot):
        return resolved
    return SourceHealthSnapshot(unavailable=frozenset(resolved))


async def evaluate_degraded_sources(
    provider: DegradedSourcesProvider | None,
) -> frozenset[str]:
    """The withhold-only view of :func:`evaluate_source_health`.

    Tier-2 graph reads consume exactly the unavailable set: graph rows carry
    provenance labels rather than served prose, so the stale leg's caveat
    carrier (a rendered-chunk count) has nothing to attach to there.
    """
    return (await evaluate_source_health(provider)).unavailable


def chunk_source_is_healthy(metadata: Mapping[str, Any], degraded_sources: frozenset[str]) -> bool:
    """Return whether a chunk's explicit connector provenance may be served.

    Absence means the chunk is not connector-owned.  Presence must be a non-empty
    string and must not name a currently degraded source.  This distinction keeps
    manually curated content available without treating malformed connector
    provenance as curated content.
    """
    if CONNECTOR_METADATA_KEY not in metadata:
        return True
    stamp = metadata[CONNECTOR_METADATA_KEY]
    return isinstance(stamp, str) and bool(stamp.strip()) and stamp not in degraded_sources


def chunk_connector_stamp(metadata: Mapping[str, Any]) -> str | None:
    """The chunk's well-formed connector stamp, or ``None`` for curated content.

    Used AFTER :func:`chunk_source_is_healthy` admitted the chunk, to ask the
    second-order question — is this served chunk's source currently stale — so
    a malformed stamp (already withheld) never reaches this classifier.
    """
    stamp = metadata.get(CONNECTOR_METADATA_KEY)
    return stamp if isinstance(stamp, str) and stamp.strip() else None


__all__ = [
    "CONNECTOR_METADATA_KEY",
    "DegradedSourcesProvider",
    "SourceHealthResult",
    "SourceHealthSnapshot",
    "chunk_connector_stamp",
    "chunk_source_is_healthy",
    "evaluate_degraded_sources",
    "evaluate_source_health",
]
