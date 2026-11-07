# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin Salesforce WRITE client (NE-050) — create an sObject record (e.g. a Note).

:class:`SalesforceWriteClient` wraps a CONSTRUCTED
:class:`~ne.connect.salesforce.client.SalesforceClient` so it inherits every C2 /
SSRF / air-gap / path-guard construction guarantee — it adds NO egress surface of its
own and imports NO httpx (the C2 egress guardian allowlist is unchanged).

The sObject name AND every field NAME are validated through the config module's
``_check_identifier`` (the established same-package idiom) BEFORE any request — this
blocks injection-shaped names at the boundary. Field VALUES travel ONLY in the JSON
body (no SOQL string interpolation anywhere). The REST create path
``/services/data/{api_version}/sobjects/{sObject}`` passes the client's ``/services/``
path guard by construction. Failures raise a STATUS/flag-only
:class:`SalesforceSyncError` — never a field value or credential (C12).
"""

from __future__ import annotations

from collections.abc import Mapping

from ne.connect.errors import SalesforceSyncError
from ne.connect.salesforce.client import SalesforceClient
from ne.connect.salesforce.config import _check_identifier

__all__ = ["SalesforceWriteClient"]


class SalesforceWriteClient:
    """Create Salesforce sObject records over a constructed :class:`SalesforceClient`."""

    def __init__(self, client: SalesforceClient, *, api_version: str) -> None:
        self._client = client
        self._api_version = api_version

    async def create_record(self, sobject: str, fields: Mapping[str, str]) -> str:
        """Create an ``sobject`` record with ``fields`` and return its new record id.

        The sObject name and every field NAME are identifier-validated first (injection
        defense at the boundary); the VALUES go only in the JSON body.
        """
        _check_identifier(sobject, "sobject")
        for name in fields:
            _check_identifier(name, "field name")
        path = f"/services/data/{self._api_version}/sobjects/{sobject}"
        async with self._client.session() as http:
            payload = await self._client.post_json(http, path, dict(fields))
        if not payload.get("success", False):
            # STATUS/flag only — never echo field values (C12).
            raise SalesforceSyncError("Salesforce create was not successful")
        return str(payload["id"])
