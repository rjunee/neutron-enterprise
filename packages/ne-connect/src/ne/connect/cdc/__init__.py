# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CDC sink (NE-084) — the governed, fail-closed Debezium change-event lane.

Import EXPLICITLY (``from ne.connect.cdc import CdcSinkService``) — this
subpackage is deliberately NOT re-exported from ``ne.connect`` (the sql/jira
explicit-import precedent). It is ``dlt``-free and ``httpx``-free by
construction (the import-hygiene guardian locks it) so ne-api may consume it at
runtime without dragging the connector-framework egress/ETL surface.
"""

from __future__ import annotations

from ne.connect.cdc.auth import decode_webhook_secret, verify_standard_webhook
from ne.connect.cdc.config import CdcSinkConfig, CdcTableAclConfig, CdcTableConfig
from ne.connect.cdc.envelope import (
    CdcChange,
    CdcEnvelopeError,
    parse_change_events,
    table_identity,
)
from ne.connect.cdc.sink import (
    CdcApplyReport,
    CdcSinkService,
    ingest_detail,
    written_from_ingest_detail,
)

__all__ = [
    "CdcApplyReport",
    "CdcChange",
    "CdcEnvelopeError",
    "CdcSinkConfig",
    "CdcSinkService",
    "CdcTableAclConfig",
    "CdcTableConfig",
    "decode_webhook_secret",
    "ingest_detail",
    "parse_change_events",
    "table_identity",
    "verify_standard_webhook",
    "written_from_ingest_detail",
]
