# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ODCS data contract for CSV-file connector records (NE-CONNECT-CSV).

The row schema is LOADED from the operator-owned JSON-Schema artifact named by
``settings.schema_path`` — the contract travels WITH the sheet (the data-contract
owner maintains both), never hardcoded in connector code. Construction is
fail-loud twice over: an unreadable/unparseable schema file raises
:class:`~ne.connect.errors.CsvSyncError`, and a well-formed-but-invalid
JSON-Schema is rejected by ``OdcsDataContract``'s own ``check_schema`` validator.

The contract source is the FIXED string ``"csv"`` — it MUST equal the
:data:`ne.connect.runner.SYNC_SOURCES` registry key, because the /connect status
surface attributes quarantine rows to a lane by the ``connector:<source>:``
provenance prefix (``count_with_prefix``); a configurable source name here would
silently break that attribution.

Permission model = ``PUBLIC_WITHIN_TENANT`` over the operator-declared
``default_acl``: a spreadsheet has no source ACL system to mirror, so the
audience is an explicit operator statement, and an empty one is unrepresentable
(``PermissionModelSpec``'s NE-CKC2 validator).
"""

from __future__ import annotations

import json
from pathlib import Path

from ne.connect.contracts import OdcsDataContract, PermissionMode, PermissionModelSpec
from ne.connect.csvfile.config import CsvConnectorSettings
from ne.connect.errors import CsvSyncError

__all__ = ["CSV_SOURCE", "csv_data_contract"]

#: The fixed contract source == the SYNC_SOURCES registry key (see module doc).
CSV_SOURCE = "csv"


def csv_data_contract(settings: CsvConnectorSettings) -> OdcsDataContract:
    """Build the ODCS contract from the operator-owned schema artifact."""
    schema_file = Path(settings.schema_path)
    try:
        schema_def = json.loads(schema_file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CsvSyncError(
            f"csv contract schema {schema_file} is not readable ({type(exc).__name__})"
        ) from exc
    except json.JSONDecodeError as exc:
        raise CsvSyncError(f"csv contract schema {schema_file} is not valid JSON: {exc}") from exc
    if not isinstance(schema_def, dict):
        raise CsvSyncError(
            f"csv contract schema {schema_file} must be a JSON object (a JSON-Schema), "
            f"got {type(schema_def).__name__}"
        )
    return OdcsDataContract(
        source=CSV_SOURCE,
        owner=settings.owner,
        schema_def=schema_def,
        freshness_sla=settings.freshness_sla,
        pii=settings.pii,
        permissions=PermissionModelSpec(
            mode=PermissionMode.PUBLIC_WITHIN_TENANT,
            default_acl=settings.default_acl,
            principal_namespace=settings.principal_namespace,
        ),
    )
