# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Recorded-fixture Jira Cloud REST/Agile transport for the connector suites (NE-093).

``build_jira_transport`` returns an :class:`httpx.MockTransport` that replays the
hand-recorded (sanitized-fake) Jira JSON under ``fixtures/jira`` with ZERO
network sockets — the CI deliverable (no live instances; live seeding needs owner
dev instances per NE-000, out of scope). It asserts every request carries a Basic
``Authorization`` header, optionally records requests, exposes fault-injection
knobs (fail-after-N; a one-shot 429 + ``Retry-After``; a one-shot 401), and
ASSERTS that no issue/comment/board/sprint call is ever made for a quarantined
project (SECRET / FORGED) — the fail-closed "quarantine fetches nothing" proof.

Incremental routing is derived from the JQL text itself (a query containing
``'updated >='`` routes to the incremental fixture), not a separate flag; the
CONTENT served for that incremental query depends on ``incremental``:
``incremental=False`` (the default — used by the conformance kit, whose contract
requires a STRICT SUBSET of the backfill) serves ``search_ACME_incremental_subset``
(only ACME-3, a real subset); ``incremental=True`` (the e2e "genuine second run")
serves ``search_ACME_incremental`` (the new ACME-4).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

from ne.connect.jira.config import JiraConnectorSettings, JiraProjectConfig

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "jira"

BASE_URL = "https://acme.atlassian.net"
PROJECT_ACME = "ACME"
#: No BROWSE_PROJECTS grant -> zero principals -> quarantine.
PROJECT_SECRET = "SECRET"
#: A forged reserved-namespace groupId -> taint -> quarantine.
PROJECT_FORGED = "FORGED"

ACME_PROJECT_ID = "10001"
GROUP_ID = "g-eng-001"
ROLE_ID = "10100"
USER_ID = "u-alice-001"

_QUARANTINED_PROJECTS = frozenset({PROJECT_SECRET, PROJECT_FORGED})

_PROJECT_RE = re.compile(r"^/rest/api/3/project/([A-Z0-9_]+)$")
_PERMSCHEME_RE = re.compile(r"^/rest/api/3/project/([A-Z0-9_]+)/permissionscheme$")
_COMMENT_RE = re.compile(r"^/rest/api/3/issue/([A-Z0-9_]+-\d+)/comment$")
_SPRINT_RE = re.compile(r"^/rest/agile/1\.0/board/(\d+)/sprint$")
_JQL_PROJECT_RE = re.compile(r"project = (\w+)")


def _load(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def jira_settings(**overrides: object) -> JiraConnectorSettings:
    """A valid settings object pointed at the fixture site (fast throttle)."""
    kwargs: dict[str, object] = {
        "base_url": BASE_URL,
        "email": "svc@acme.test",
        "api_token": "fake-jira-api-token-not-real",
        "owner": "data-eng@acme.test",
        "projects": (
            JiraProjectConfig(key=PROJECT_ACME),
            JiraProjectConfig(key=PROJECT_SECRET),
            JiraProjectConfig(key=PROJECT_FORGED),
        ),
        "requests_per_second": 6000.0,
    }
    kwargs.update(overrides)
    return JiraConnectorSettings(**kwargs)  # type: ignore[arg-type]


def build_jira_transport(
    *,
    recorder: list[httpx.Request] | None = None,
    fail_after: int | None = None,
    one_shot_429: bool = False,
    respond_401_once: bool = False,
    incremental: bool = False,
    incremental_fixture: str | None = None,
) -> httpx.MockTransport:
    """Build the fixture-replay transport (see module docstring for the knobs).

    ``incremental_fixture``, when set, overrides which JSON file is served for
    the ``'updated >='`` incremental JQL query (e.g. to serve a batch containing
    a write-quarantine-triggering blank record); the default (``None``) preserves
    the existing ``incremental``-flag-driven selection.
    """
    state = {"calls": 0, "ratelimited": False, "unauthorized": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)

        auth = request.headers.get("Authorization", "")
        assert auth.startswith("Basic "), f"missing Basic auth on {request.url}"

        state["calls"] += 1
        if fail_after is not None and state["calls"] > fail_after:
            return httpx.Response(500)
        if one_shot_429 and not state["ratelimited"]:
            state["ratelimited"] = True
            return httpx.Response(429, headers={"Retry-After": "1"})
        if respond_401_once and not state["unauthorized"]:
            state["unauthorized"] = True
            return httpx.Response(401)

        path = request.url.path
        params = dict(request.url.params)

        perm = _PERMSCHEME_RE.match(path)
        if perm:
            return httpx.Response(200, json=_load(f"permscheme_{perm.group(1)}.json"))

        proj = _PROJECT_RE.match(path)
        if proj:
            return httpx.Response(200, json=_load(f"project_{proj.group(1)}.json"))

        if path == "/rest/api/3/search/jql":
            jql = params.get("jql", "")
            match = _JQL_PROJECT_RE.search(jql)
            project = match.group(1) if match else ""
            assert project not in _QUARANTINED_PROJECTS, (
                f"a JQL search was issued for quarantined project {project!r}"
            )
            if "updated >=" in jql:
                fixture = incremental_fixture or (
                    "search_ACME_incremental.json"
                    if incremental
                    else "search_ACME_incremental_subset.json"
                )
                return httpx.Response(200, json=_load(fixture))
            if params.get("nextPageToken") == "pg2tok":
                return httpx.Response(200, json=_load("search_ACME_page2.json"))
            return httpx.Response(200, json=_load("search_ACME_page1.json"))

        comment = _COMMENT_RE.match(path)
        if comment:
            issue_key = comment.group(1)
            project = issue_key.split("-", 1)[0]
            assert project not in _QUARANTINED_PROJECTS, (
                f"comments were fetched for quarantined project {project!r}"
            )
            return httpx.Response(200, json=_load(f"comments_{issue_key}.json"))

        if path == "/rest/agile/1.0/board":
            project = params.get("projectKeyOrId", "")
            assert project not in _QUARANTINED_PROJECTS, (
                f"boards were fetched for quarantined project {project!r}"
            )
            return httpx.Response(200, json=_load("boards_ACME.json"))

        sprint = _SPRINT_RE.match(path)
        if sprint:
            return httpx.Response(200, json=_load(f"sprints_board{sprint.group(1)}.json"))

        return httpx.Response(404)

    return httpx.MockTransport(handler)


def recording_transport(recorder: list[httpx.Request]):
    """A bare handler that records and 200s — proves zero requests on rejection."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={})

    return handler
