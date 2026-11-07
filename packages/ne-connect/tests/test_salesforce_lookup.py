# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SalesforceLookupClient — the previously-untested read leg of the CRM write.

Every normative claim the module makes is asserted HERE against the actual query
the client sends (exact string comparison, never substring — a substring check on a
query is monotone under injection and proves nothing):

* the exact SOQL shape, including ``LIMIT 2`` (the ambiguity-detection mechanism);
* apostrophes/backslashes are ESCAPED, not refused — "O'Brien Logistics" resolves;
* an injection-shaped name stays entirely inside the string literal;
* the escape ORDERING (backslash first) — the swapped order emits a bare quote;
* empty / control-character names are refused before any request (no token spent);
* an ambiguous name is refused without echoing the name (C12);
* a miss returns ``None`` (fail closed: caller withholds the write).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ne.connect.errors import SalesforceSyncError
from ne.connect.salesforce.client import SalesforceClient
from ne.connect.salesforce.lookup import SalesforceLookupClient, soql_escape

from .salesforce_transport import API_VERSION, salesforce_settings

_QUERY_PATH = f"/services/data/{API_VERSION}/query"
_TOKEN_PATH = "/services/oauth2/token"
_ACCOUNT_ID = "001000000000001AAA"
_SECRET = "top-secret-value-not-real"


def _lookup_client(transport: httpx.MockTransport) -> SalesforceLookupClient:
    client = SalesforceClient(
        salesforce_settings(auth_method="client_credentials", client_secret=_SECRET),
        transport=transport,
    )
    return SalesforceLookupClient(client)


def _transport(
    *,
    records: list[dict[str, str]] | None = None,
    recorder: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    rows = records if records is not None else [{"Id": _ACCOUNT_ID, "Name": "whatever"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        if request.url.path == _TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "fake-token", "token_type": "Bearer"})
        if request.url.path == _QUERY_PATH and request.method == "GET":
            return httpx.Response(
                200,
                json={"totalSize": len(rows), "done": True, "records": rows},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _sent_query(recorder: list[httpx.Request]) -> str:
    queries = [r for r in recorder if r.url.path == _QUERY_PATH]
    assert len(queries) == 1
    return queries[0].url.params["q"]


def test_exact_query_shape_and_happy_path() -> None:
    recorder: list[httpx.Request] = []
    client = _lookup_client(_transport(recorder=recorder))

    resolved = asyncio.run(client.find_id_by_name("Account", "Acme Ltd"))

    assert resolved == _ACCOUNT_ID
    # EXACT compare: LIMIT 2 is the ambiguity-detection mechanism, not decoration.
    assert _sent_query(recorder) == "SELECT Id, Name FROM Account WHERE Name = 'Acme Ltd' LIMIT 2"


def test_apostrophe_name_is_escaped_and_resolves() -> None:
    """ "O'Brien Logistics" is an ordinary company name — it must RESOLVE, not refuse."""
    recorder: list[httpx.Request] = []
    client = _lookup_client(_transport(recorder=recorder))

    resolved = asyncio.run(client.find_id_by_name("Account", "O'Brien Logistics"))

    assert resolved == _ACCOUNT_ID
    assert (
        _sent_query(recorder)
        == "SELECT Id, Name FROM Account WHERE Name = 'O\\'Brien Logistics' LIMIT 2"
    )


def test_injection_attempt_cannot_alter_the_query_meaning() -> None:
    """The whole hostile name lands INSIDE one string literal — proven by exact compare.

    ``x' OR Name != '`` would, unescaped, terminate the literal and widen the WHERE
    clause to every account. Escaped, it is an (unmatchable) literal name and the
    org's answer is a plain miss.
    """
    recorder: list[httpx.Request] = []
    client = _lookup_client(_transport(records=[], recorder=recorder))

    resolved = asyncio.run(client.find_id_by_name("Account", "x' OR Name != '"))

    assert resolved is None  # a miss, never a widened match
    assert (
        _sent_query(recorder)
        == "SELECT Id, Name FROM Account WHERE Name = 'x\\' OR Name != \\'' LIMIT 2"
    )


def test_soql_escape_ordering_backslash_first() -> None:
    """Exact outputs pin the claimed ordering; the swapped order emits a BARE quote.

    Swapped (quote first, then backslash): ``'`` -> ``\\'`` -> ``\\\\'`` — an escaped
    backslash followed by an UN-escaped quote, i.e. a literal terminator. These
    exact-string compares fail under that mutation.
    """
    assert soql_escape("'") == "\\'"
    assert soql_escape("\\") == "\\\\"
    # Combined: backslash-then-quote input must become escaped-backslash then
    # escaped-quote — 4 characters, ending in the ESCAPED quote pair.
    assert soql_escape("\\'") == "\\\\\\'"
    # Control: a name with nothing to escape passes through byte-identical.
    assert soql_escape("O Brien Logistics") == "O Brien Logistics"


@pytest.mark.parametrize("name", ["", "   "])
def test_empty_name_refused_before_any_request(name: str) -> None:
    recorder: list[httpx.Request] = []
    client = _lookup_client(_transport(recorder=recorder))

    with pytest.raises(SalesforceSyncError, match="empty record name"):
        asyncio.run(client.find_id_by_name("Account", name))

    assert recorder == []  # rejected at the boundary — not even a token POST


@pytest.mark.parametrize(
    "name",
    [
        "Acme\nLtd",
        "Acme\rLtd",
        "Acme\tLtd",
        "Acme\x00Ltd",
        # The module claims ALL raw control characters are refused — so the
        # rest of C0 (vertical tab, form feed, backspace, escape) and DEL must
        # refuse too, or the claim is an enumeration that under-samples its own
        # set (the V3 shape). The apostrophe/passthrough tests above are the
        # over-broad control: a guard that refused more than control characters
        # fails them.
        "Acme\x0bLtd",
        "Acme\x0cLtd",
        "Acme\x08Ltd",
        "Acme\x1bLtd",
        "Acme\x7fLtd",
    ],
)
def test_control_character_name_refused_without_echoing_it(name: str) -> None:
    recorder: list[httpx.Request] = []
    client = _lookup_client(_transport(recorder=recorder))

    with pytest.raises(SalesforceSyncError, match="control characters") as exc:
        asyncio.run(client.find_id_by_name("Account", name))

    assert "Acme" not in str(exc.value)  # never echo the value (C12)
    assert recorder == []


def test_ambiguous_name_refused_without_echoing_it() -> None:
    two = [{"Id": _ACCOUNT_ID, "Name": "n"}, {"Id": "001000000000002AAA", "Name": "n"}]
    client = _lookup_client(_transport(records=two))

    with pytest.raises(SalesforceSyncError, match="ambiguous") as exc:
        asyncio.run(client.find_id_by_name("Account", "Duplicated Name Ltd"))

    assert "Duplicated" not in str(exc.value)  # C12: the name never rides the error


def test_miss_returns_none() -> None:
    client = _lookup_client(_transport(records=[]))
    assert asyncio.run(client.find_id_by_name("Account", "Nobody Here Ltd")) is None


def test_injection_shaped_sobject_refused_before_any_request() -> None:
    recorder: list[httpx.Request] = []
    client = _lookup_client(_transport(recorder=recorder))

    with pytest.raises(ValueError):
        asyncio.run(client.find_id_by_name("Account WHERE 1=1 --", "Acme Ltd"))

    assert recorder == []


# --- find_contact_by_name (NE-CS-LOOKUP) --------------------------------------

_CONTACT_ROW = {
    "Id": "003000000000001AAA",
    "Email": "owner@example.com",
    "AccountId": _ACCOUNT_ID,
}


def test_contact_exact_query_shape_and_happy_path() -> None:
    recorder: list[httpx.Request] = []
    client = _lookup_client(_transport(records=[dict(_CONTACT_ROW)], recorder=recorder))

    resolved = asyncio.run(client.find_contact_by_name("Avery Arbor"))

    assert resolved == {
        "record_id": "003000000000001AAA",
        "email": "owner@example.com",
        "account_id": _ACCOUNT_ID,
    }
    # EXACT compare: LIMIT 2 is the ambiguity-detection mechanism, not decoration.
    assert (
        _sent_query(recorder)
        == "SELECT Id, Email, AccountId FROM Contact WHERE Name = 'Avery Arbor' LIMIT 2"
    )


def test_contact_apostrophe_name_is_escaped_and_resolves() -> None:
    recorder: list[httpx.Request] = []
    client = _lookup_client(_transport(records=[dict(_CONTACT_ROW)], recorder=recorder))

    resolved = asyncio.run(client.find_contact_by_name("Sinead O'Brien"))

    assert resolved is not None and resolved["email"] == "owner@example.com"
    assert (
        _sent_query(recorder)
        == "SELECT Id, Email, AccountId FROM Contact WHERE Name = 'Sinead O\\'Brien' LIMIT 2"
    )


def test_contact_miss_returns_none() -> None:
    client = _lookup_client(_transport(records=[]))
    assert asyncio.run(client.find_contact_by_name("Nobody Here")) is None


def test_contact_ambiguous_name_refused_without_echoing_it() -> None:
    client = _lookup_client(
        _transport(records=[dict(_CONTACT_ROW), dict(_CONTACT_ROW, Id="003000000000002AAA")])
    )
    with pytest.raises(SalesforceSyncError) as excinfo:
        asyncio.run(client.find_contact_by_name("Avery Arbor"))
    assert "ambiguous" in str(excinfo.value)
    assert "Avery" not in str(excinfo.value)


@pytest.mark.parametrize("field", ["Id", "Email", "AccountId"])
def test_contact_missing_field_fails_closed_not_invented(field: str) -> None:
    row = dict(_CONTACT_ROW)
    row[field] = ""
    client = _lookup_client(_transport(records=[row]))
    with pytest.raises(SalesforceSyncError) as excinfo:
        asyncio.run(client.find_contact_by_name("Avery Arbor"))
    assert "refusing to serve" in str(excinfo.value)


@pytest.mark.parametrize("name", ["", "   "])
def test_contact_empty_name_refused_before_any_request(name: str) -> None:
    recorder: list[httpx.Request] = []
    client = _lookup_client(_transport(recorder=recorder))
    with pytest.raises(SalesforceSyncError):
        asyncio.run(client.find_contact_by_name(name))
    assert recorder == []


def test_contact_control_character_name_refused_without_echoing_it() -> None:
    recorder: list[httpx.Request] = []
    client = _lookup_client(_transport(recorder=recorder))
    with pytest.raises(SalesforceSyncError) as excinfo:
        asyncio.run(client.find_contact_by_name("Avery\x00Arbor"))
    assert "Avery" not in str(excinfo.value)
    assert recorder == []
