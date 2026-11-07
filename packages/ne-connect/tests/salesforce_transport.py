# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Recorded-fixture Salesforce REST/Bulk API 2.0 transport for the connector suites (NE-047).

``build_salesforce_transport`` returns an :class:`httpx.MockTransport` that
replays the hand-recorded (sanitized-fake) Salesforce JSON/CSV under
``fixtures/salesforce`` with ZERO network sockets — the CI deliverable per
NE-000. It asserts every non-token call carries a Bearer ``Authorization``
header, optionally records requests, and exposes fault-injection knobs
(fail-after-N calls; a one-shot 429+``Retry-After``; a one-shot 401 to prove
the token-refresh-and-retry-once path; a named object whose Bulk job always
reports ``Failed``) used by the durability/rate-limit/quarantine tests.

Incremental routing is derived from the SOQL text itself (a query containing
``'WHERE SystemModstamp >'`` routes to the object's incremental job/results),
not a separate flag — mirroring how the real connector actually drives the
API.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from ne.connect.salesforce.config import SalesforceConnectorSettings, SalesforceObjectConfig

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "salesforce"

API_VERSION = "v65.0"
INSTANCE_URL = "https://acme.my.salesforce.com"
LOGIN_URL = "https://login.salesforce.com"

OBJ_ACCOUNT = "Account"
OBJ_OPP = "Opportunity"
#: Zero ObjectPermissions rows ever name this object -> empty outcome -> quarantine.
OBJ_NOPERM = "Secretplan__c"
#: A permission row whose Parent id is a forged reserved-namespace value -> taint -> quarantine.
OBJ_FORGED = "Forged__c"

PROFILE_ID = "00e000000000001AAA"
PERMSET_ID = "0PS000000000001AAA"
ORGANIZATION_ID = "00D000000000001EAA"

_QUERY_PATH = f"/services/data/{API_VERSION}/query"
_OBJECTPERMS_NEXT_PATH = f"/services/data/{API_VERSION}/query/01g0FAKE-2000"
_JOBS_PATH = f"/services/data/{API_VERSION}/jobs/query"
_TOKEN_PATH = "/services/oauth2/token"

#: (object, incremental) -> the fake Bulk job id our fixtures use.
_JOB_IDS: dict[tuple[str, bool], str] = {
    (OBJ_ACCOUNT, False): "750FAKEACC",
    (OBJ_ACCOUNT, True): "750FAKEACC2",
    (OBJ_OPP, False): "750FAKEOPP",
    (OBJ_OPP, True): "750FAKEOPP2",
}
_JOB_ID_TO_OBJECT = {job_id: obj for (obj, _incremental), job_id in _JOB_IDS.items()}

#: Static (job-id-only) results fixtures — "750FAKEACC2" is handled specially
#: (its content depends on the ``incremental`` construction flag; see below).
_RESULTS_FIXTURE: dict[str, str] = {
    "750FAKEOPP": "results_Opportunity.csv",
    "750FAKEOPP2": "results_Opportunity_incremental.csv",
}


def _load_json(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _load_csv(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def salesforce_settings(**overrides: object) -> SalesforceConnectorSettings:
    """A valid settings object pointed at the fixture org (client_credentials auth).

    The throttle rate is overridden to a fast-but-still-real token bucket and
    ``bulk_poll_interval_seconds=0.0`` so the fixture-replay suites exercise real
    ``AdaptiveThrottle``/poll pacing without real wall-clock waits blowing up CI
    run time (production defaults are the conservative values on
    :class:`SalesforceConnectorSettings` — see ``test_salesforce_config.py`` for
    those).
    """
    kwargs: dict[str, object] = {
        "auth_method": "client_credentials",
        "client_id": "3MVFAKE0000000000CLIENTID",
        "client_secret": "fake-client-secret-not-real",
        "instance_url": INSTANCE_URL,
        "owner": "data-eng@acme.test",
        "objects": (
            SalesforceObjectConfig(
                name=OBJ_ACCOUNT,
                content_fields=("Name", "Description"),
                metadata_fields=("Industry",),
            ),
            SalesforceObjectConfig(
                name=OBJ_OPP,
                content_fields=("Name",),
                metadata_fields=("StageName", "Amount"),
            ),
            SalesforceObjectConfig(name=OBJ_NOPERM, content_fields=("Name",)),
            SalesforceObjectConfig(name=OBJ_FORGED, content_fields=("Name",)),
        ),
        "bulk_poll_interval_seconds": 0.0,
        "requests_per_second": 6000.0,
    }
    kwargs.update(overrides)
    return SalesforceConnectorSettings(**kwargs)  # type: ignore[arg-type]


def salesforce_jwt_settings(tmp_path: Path, **overrides: object) -> SalesforceConnectorSettings:
    """A valid settings object using ``jwt_bearer`` auth, with a throwaway RSA key.

    Generates a fresh (never persisted beyond ``tmp_path``) RSA-2048 PEM per call
    — a real cryptographic key is required because the client signs a genuine
    RS256 assertion, but its origin is entirely local/fake (never a real org's
    key material).
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "sf_test_key.pem"
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    kwargs: dict[str, object] = {
        "auth_method": "jwt_bearer",
        "username": "integration-user@acme.test",
        "private_key_pem_path": str(key_path),
    }
    kwargs.update(overrides)
    return salesforce_settings(**kwargs)


def build_salesforce_transport(
    *,
    recorder: list[httpx.Request] | None = None,
    fail_after: int | None = None,
    one_shot_429: bool = False,
    respond_401_once: bool = False,
    fail_job_for: str | None = None,
    assert_no_bulk_job_for: frozenset[str] = frozenset({OBJ_NOPERM, OBJ_FORGED}),
    incremental: bool = False,
    processed_count_overrides: Mapping[str, int] | None = None,
    repeat_locator_for: str | None = None,
    organization_id: str = ORGANIZATION_ID,
    request_observer: Callable[[httpx.Request], None] | None = None,
) -> httpx.MockTransport:
    """Build the fixture-replay transport.

    ``recorder`` collects every request. ``fail_after`` returns HTTP 500 beyond
    the Nth call (drives the mid-run-abort/no-cursor-commit tests).
    ``one_shot_429`` serves ONE HTTP 429 (with ``Retry-After: 1``) on the first
    authenticated call, then the normal fixture. ``respond_401_once`` serves ONE
    HTTP 401 on the first authenticated call (token-refresh-and-retry-once path).
    ``fail_job_for`` names an object whose Bulk job status ALWAYS reports
    ``Failed`` (run-abort test). ``assert_no_bulk_job_for`` raises
    ``AssertionError`` if a ``jobs/query`` POST ever names one of these objects —
    the fail-closed "never even create a job for a quarantined object" proof.

    A SOQL containing ``'WHERE SystemModstamp >'`` always routes Account/
    Opportunity queries to their "incremental" Bulk job id, but the CONTENT
    served for that job depends on ``incremental``: ``incremental=False`` (the
    default — used by the conformance kit, whose contract requires a STRICT
    SUBSET of the full backfill) serves ``results_Account_subset.csv`` (a real
    subset of the backfill rows, still a "later page" — never new content, the
    same discipline ``slack_transport.py`` uses); ``incremental=True`` (used by
    the e2e "genuine second run" tests) serves the truly-new-content fixtures.
    """
    state = {"calls": 0, "ratelimited": False, "unauthorized": False}
    job_status_calls: dict[str, int] = {}
    processed_count_overrides = dict(processed_count_overrides or {})

    def processed_count(object_name: str, default: int) -> int:
        return processed_count_overrides.get(object_name, default)

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        if request_observer is not None:
            request_observer(request)

        state["calls"] += 1
        if fail_after is not None and state["calls"] > fail_after:
            return httpx.Response(500)

        path = request.url.path

        if path == _TOKEN_PATH:
            payload = _load_json("token.json")
            payload["organization_id"] = organization_id
            payload["id"] = f"https://login.salesforce.com/id/{organization_id}/005000000000001AAA"
            return httpx.Response(200, json=payload)

        auth = request.headers.get("Authorization", "")
        assert auth.startswith("Bearer "), f"missing bearer on {request.url}"

        if one_shot_429 and not state["ratelimited"]:
            state["ratelimited"] = True
            return httpx.Response(429, headers={"Retry-After": "1"})

        if respond_401_once and not state["unauthorized"]:
            state["unauthorized"] = True
            return httpx.Response(401)

        if path == _QUERY_PATH and request.method == "GET":
            soql = str(request.url.params.get("q", ""))
            point_match = re.search(
                r"FROM (\w+) WHERE \w+ = '([A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?)' LIMIT 2$",
                soql,
            )
            if point_match is not None:
                object_name, record_id = point_match.groups()
                fixtures = {
                    OBJ_ACCOUNT: ("results_Account_page1.csv", "results_Account_page2.csv"),
                    OBJ_OPP: ("results_Opportunity.csv",),
                }
                rows = [
                    row
                    for fixture in fixtures.get(object_name, ())
                    for row in csv.DictReader(io.StringIO(_load_csv(fixture)))
                    if row.get("Id") == record_id
                ]
                return httpx.Response(
                    200,
                    json={"totalSize": len(rows), "done": True, "records": rows},
                )
            payload = _load_json("objectperms_page1.json")
            payload["totalSize"] = 5
            return httpx.Response(200, json=payload)
        if path == _OBJECTPERMS_NEXT_PATH and request.method == "GET":
            payload = _load_json("objectperms_page2.json")
            payload["totalSize"] = 5
            return httpx.Response(200, json=payload)

        if path == _JOBS_PATH and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            soql = str(body.get("query", ""))
            match = re.search(r"FROM (\w+)", soql)
            obj = match.group(1) if match else ""
            if obj in assert_no_bulk_job_for:
                raise AssertionError(f"a Bulk query job was created for quarantined object {obj!r}")
            has_where_clause = "WHERE SystemModstamp >" in soql
            job_id = _JOB_IDS.get((obj, has_where_clause))
            if job_id is None:
                return httpx.Response(404)
            suffix = "_incremental" if has_where_clause else ""
            return httpx.Response(200, json=_load_json(f"job_create_{obj}{suffix}.json"))

        job_status_match = re.fullmatch(rf"{re.escape(_JOBS_PATH)}/(\w+)", path)
        if job_status_match and request.method == "GET":
            job_id = job_status_match.group(1)
            obj = _JOB_ID_TO_OBJECT.get(job_id)
            if fail_job_for is not None and obj == fail_job_for:
                return httpx.Response(200, json=_load_json("job_status_failed.json"))
            if job_id == "750FAKEACC":
                calls = job_status_calls.get(job_id, 0) + 1
                job_status_calls[job_id] = calls
                fixture = (
                    "job_status_Account_inprogress.json"
                    if calls == 1
                    else "job_status_Account_complete.json"
                )
                payload = _load_json(fixture)
                if payload.get("state") == "JobComplete":
                    payload["numberRecordsProcessed"] = processed_count(OBJ_ACCOUNT, 3)
                return httpx.Response(200, json=payload)
            if job_id == "750FAKEOPP":
                payload = _load_json("job_status_Opportunity_complete.json")
                payload["numberRecordsProcessed"] = processed_count(OBJ_OPP, 2)
                return httpx.Response(200, json=payload)
            if job_id == "750FAKEACC2":
                payload = _load_json("job_status_Account_incremental_complete.json")
                payload["numberRecordsProcessed"] = processed_count(
                    OBJ_ACCOUNT, 1 if incremental else 2
                )
                return httpx.Response(200, json=payload)
            if job_id == "750FAKEOPP2":
                payload = _load_json("job_status_Opportunity_incremental_complete.json")
                payload["numberRecordsProcessed"] = processed_count(OBJ_OPP, 0)
                return httpx.Response(200, json=payload)
            return httpx.Response(404)

        results_match = re.fullmatch(rf"{re.escape(_JOBS_PATH)}/(\w+)/results", path)
        if results_match and request.method == "GET":
            job_id = results_match.group(1)
            locator = dict(request.url.params).get("locator")
            if job_id == "750FAKEACC":
                if not locator:
                    return httpx.Response(
                        200,
                        text=_load_csv("results_Account_page1.csv"),
                        headers={"Sforce-Locator": "p2"},
                    )
                return httpx.Response(
                    200,
                    text=_load_csv("results_Account_page2.csv"),
                    headers={
                        "Sforce-Locator": "p2" if repeat_locator_for == OBJ_ACCOUNT else "null"
                    },
                )
            if job_id == "750FAKEACC2":
                fixture = (
                    "results_Account_incremental.csv"
                    if incremental
                    else "results_Account_subset.csv"
                )
                return httpx.Response(
                    200, text=_load_csv(fixture), headers={"Sforce-Locator": "null"}
                )
            fixture = _RESULTS_FIXTURE.get(job_id)
            if fixture is not None:
                return httpx.Response(
                    200, text=_load_csv(fixture), headers={"Sforce-Locator": "null"}
                )
            return httpx.Response(404)

        return httpx.Response(404)

    return httpx.MockTransport(handler)


def recording_transport(recorder: list[httpx.Request]):
    """A bare handler that records and 200s — proves zero requests on rejection."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json=_load_json("token.json"))

    return handler
