# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin Salesforce LOOKUP client — resolve an sObject record id by exact Name.

The mirror of :mod:`ne.connect.salesforce.write`: it wraps a CONSTRUCTED
:class:`~ne.connect.salesforce.client.SalesforceClient` so it inherits every C2 /
SSRF / air-gap / path-guard construction guarantee, adds NO egress surface of its own,
and imports NO httpx.

**Why this exists.** A governed write needs a TARGET. Reading that target out of the
system of record — rather than out of a local seed file — is what makes the write
traceable to a served field. This client is the read leg.

**SOQL injection defence.** The sObject name is identifier-validated through the
config module's ``_check_identifier`` (the established same-package idiom). The Name
VALUE cannot travel in a JSON body (SOQL is a query string), so it is escaped for a
SOQL string literal by :func:`soql_escape`. Apostrophes and backslashes are ORDINARY
characters in real account names ("O'Brien Logistics") and are escaped, never
refused. The only refused characters are raw control characters — ALL of C0
(``0x00``-``0x1F``, so ``\\x0b \\x0c \\x08`` alongside ``\\r \\n \\t \\x00``) and
DEL (``0x7f``). The refusal is not a grammar claim (a tab, for instance, is
ordinary whitespace inside a query string): it is that a control character is not
legitimate account-name content, and this client makes no attempt at
escaped-control-character matching — if such a name exists in the org, this client
cannot resolve it and says so. A caller must surface
that refusal as a per-subject outcome (no write, fail closed), not let it unwind a
whole run. Every claim in this paragraph is pinned by
``packages/ne-connect/tests/test_salesforce_lookup.py``, including an injection
attempt proven (by exact query comparison) unable to alter the query's meaning.

Failures raise a STATUS/flag-only :class:`SalesforceSyncError` — never a field value
or credential (C12).
"""

from __future__ import annotations

from ne.connect.errors import SalesforceSyncError
from ne.connect.salesforce.client import SalesforceClient
from ne.connect.salesforce.config import _check_identifier

__all__ = ["SalesforceLookupClient", "soql_escape"]

#: Every raw control character — all of C0 (``0x00``-``0x1F``) plus DEL
#: (``0x7f``) — so this set IS the module docstring's claim, not a sample of it.
#: A Name containing one is refused (see the module docstring for why this is a
#: refusal and not an escape). Quoting characters are NOT in this set — they are
#: handled by :func:`soql_escape`, because they occur in legitimate account names.
_SOQL_FORBIDDEN = frozenset(chr(code) for code in range(0x20)) | {"\x7f"}


def soql_escape(value: str) -> str:
    """Escape ``value`` for use inside a single-quoted SOQL string literal.

    Salesforce requires backslash and single-quote to be backslash-escaped. The order
    matters: backslash FIRST, or the escape character inserted for the quote would
    itself be re-escaped, leaving the quote bare — an injection, not a typo. The
    ordering is pinned by exact-output tests in ``test_salesforce_lookup.py``.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


class SalesforceLookupClient:
    """Resolve sObject record ids over a constructed :class:`SalesforceClient`."""

    def __init__(self, client: SalesforceClient) -> None:
        self._client = client

    async def find_id_by_name(self, sobject: str, name: str) -> str | None:
        """Return the id of the ``sobject`` record whose ``Name`` equals ``name``.

        Returns ``None`` when nothing matches — an unresolved target is a NORMAL
        outcome a caller must handle (fail closed: no write), not an error. Raises
        :class:`SalesforceSyncError` when the name is unusable or the org returns
        MORE than one match (an ambiguous target is never silently resolved to the
        first row).
        """
        _check_identifier(sobject, "sobject")
        cleaned = name.strip()
        if not cleaned:
            raise SalesforceSyncError("refusing to look up an empty record name")
        if _SOQL_FORBIDDEN & set(cleaned):
            # STATUS/flag only — never echo the offending value (C12).
            raise SalesforceSyncError("refusing to look up a name with raw control characters")
        soql = f"SELECT Id, Name FROM {sobject} WHERE Name = '{soql_escape(cleaned)}' LIMIT 2"
        async with self._client.session() as http:
            rows = await self._client.rest_query(http, soql)
        if not rows:
            return None
        if len(rows) > 1:
            raise SalesforceSyncError(f"{sobject} name is ambiguous; refusing to pick one")
        return str(rows[0]["Id"])

    async def find_contact_by_name(self, name: str) -> dict[str, str] | None:
        """Resolve one Contact by exact full ``Name`` (NE-CS-LOOKUP).

        Returns ``None`` when nothing matches, or a mapping of ``record_id``
        (the Contact's own id), ``email`` (the recorded email address — the
        governed outreach recipient is READ from the system of record, never
        supplied by a caller), and ``account_id`` (the parent Account a
        governed CRM note targets). The same defensive posture as
        :meth:`find_id_by_name`: control characters are refused, quoting is
        escaped, and MORE than one match raises rather than silently picking a
        row — an ambiguous person is never resolved to the first hit. A
        matched Contact whose Email or AccountId is empty raises too: an
        outreach flow must fail closed on an unaddressable or unparented
        contact, not invent a value.
        """
        cleaned = name.strip()
        if not cleaned:
            raise SalesforceSyncError("refusing to look up an empty contact name")
        if _SOQL_FORBIDDEN & set(cleaned):
            # STATUS/flag only — never echo the offending value (C12).
            raise SalesforceSyncError("refusing to look up a name with raw control characters")
        soql = (
            "SELECT Id, Email, AccountId FROM Contact "
            f"WHERE Name = '{soql_escape(cleaned)}' LIMIT 2"
        )
        async with self._client.session() as http:
            rows = await self._client.rest_query(http, soql)
        if not rows:
            return None
        if len(rows) > 1:
            raise SalesforceSyncError("Contact name is ambiguous; refusing to pick one")
        row = rows[0]
        record_id = str(row.get("Id") or "")
        email = str(row.get("Email") or "")
        account_id = str(row.get("AccountId") or "")
        if not record_id or not email or not account_id:
            raise SalesforceSyncError(
                "Contact record lacks an Id, Email, or AccountId; refusing to serve it"
            )
        return {"record_id": record_id, "email": email, "account_id": account_id}
