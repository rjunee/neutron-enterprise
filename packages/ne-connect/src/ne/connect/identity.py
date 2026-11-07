# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Canonical, content-safe connector source-snapshot identity.

The digest binds a raw connector record to its source-native identity without
persisting or exposing its payload.  T1 lineage and the independent T2 graph
consumer both use this function; neither guesses from mutable current state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from ne.contracts.integration import Record

__all__ = [
    "SOURCE_ACL_METADATA_KEY",
    "SOURCE_SNAPSHOT_METADATA_KEY",
    "source_acl_hash",
    "source_ref_for",
    "source_snapshot_hash",
]

SOURCE_ACL_METADATA_KEY = "source_acl_hash"
SOURCE_SNAPSHOT_METADATA_KEY = "source_snapshot_hash"


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    return value


def source_ref_for(record: Record) -> str:
    """Return the graph/citation-stable source-native record reference."""
    return f"{record.source_id}:{record.record_id}"


def source_acl_hash(acl: tuple[str, ...]) -> str:
    """Hash the resolved source ACL envelope without exposing its labels."""
    canonical = json.dumps(
        {"acl": sorted(set(acl))},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_snapshot_hash(source_system: str, record: Record) -> str:
    """Hash the exact raw source record plus its identity deterministically.

    Identity is part of the envelope so two records with byte-identical payloads
    cannot share a receipt.  ``default=str`` matches the connector bridge's
    canonical payload handling for JSON-shaped provider values.
    """
    envelope = {
        "payload": _plain(record.payload),
        "record_id": record.record_id,
        "source_id": record.source_id,
        "source_system": source_system,
    }
    canonical = json.dumps(
        envelope,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
