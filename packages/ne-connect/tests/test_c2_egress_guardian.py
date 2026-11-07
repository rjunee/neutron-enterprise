# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE C2 egress structural guardian for ne-connect (plan §2.1, NE-044/045).

Makes "a bare HTTP client to a config-supplied source URL that skips SSRF /
air-gap validation" impossible to reintroduce silently. Three layers, cloned
from the ne-brain guardian:

(a) IMPORT ALLOWLIST — under ``src/ne/connect`` only ``m365/graph.py`` and
    ``slack/client.py`` may import an outbound-HTTP client library. The
    ``testing/`` subtree (the conformance kit's zero-network replay
    legitimately references ``socket`` to DISABLE it) is CI infrastructure, not
    production egress surface, so it is out of scope.
(b) VALIDATED CONSTRUCTION — both clients' stored base URLs must flow through
    ``validate_source_url`` before any client can issue a request.
(c) BEHAVIORAL RED-TEAM — a hostile endpoint raises at construction and, with a
    live recording transport handed in, ZERO requests ever leave.

NE-045 consciously extends this guardian for the SECOND httpx site
(``slack/client.py``) per the unit's own §2.1 mandate: a new egress module must
keep the constraint airtight in the SAME PR that introduces it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from ne.connect.endpoints import UnsafeEndpointError
from ne.connect.jira.client import JiraClient
from ne.connect.m365.graph import GraphClient
from ne.connect.salesforce.client import SalesforceClient
from ne.connect.slack.client import SlackClient
from ne.connect.teams.graph import TeamsGraphClient
from ne.connect.zendesk.client import ZendeskClient

from .jira_transport import jira_settings
from .jira_transport import recording_transport as jira_recording_transport
from .m365_transport import m365_settings
from .m365_transport import recording_transport as m365_recording_transport
from .salesforce_transport import recording_transport as salesforce_recording_transport
from .salesforce_transport import salesforce_settings
from .slack_transport import recording_transport as slack_recording_transport
from .slack_transport import slack_settings
from .teams_transport import recording_transport as teams_recording_transport
from .teams_transport import teams_settings
from .zendesk_transport import recording_transport as zendesk_recording_transport
from .zendesk_transport import zendesk_settings

_SRC = Path(__file__).resolve().parents[1] / "src" / "ne" / "connect"

#: The ONLY ne.connect modules permitted to import an outbound-HTTP client.
ALLOWED_HTTP_MODULES = {
    "m365/graph.py",
    "salesforce/client.py",
    "slack/client.py",
    "jira/client.py",
    "zendesk/client.py",
    # NE-080 consciously extends this guardian for the Teams Graph client — a new
    # egress module keeps the single-egress-site constraint airtight in the SAME
    # PR that introduces it (quality-policy §2 item 4).
    "teams/graph.py",
}

_FORBIDDEN_TOP_LEVEL = {"httpx", "requests", "aiohttp", "urllib3", "socket"}
_FORBIDDEN_DOTTED_PREFIXES = ("urllib.request", "http.client")

_GUARD_MESSAGE = (
    "ne-connect outbound HTTP is C2-guarded: construct Graph clients only with "
    "base URLs validated by ne.connect.endpoints.validate_source_url plus the "
    "allowed-hosts membership check, then consciously extend ALLOWED_HTTP_MODULES."
)


def _imports_http_client(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _FORBIDDEN_TOP_LEVEL or alias.name.startswith(_FORBIDDEN_DOTTED_PREFIXES):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module is None:
                continue
            top = node.module.split(".")[0]
            if top in _FORBIDDEN_TOP_LEVEL or node.module.startswith(_FORBIDDEN_DOTTED_PREFIXES):
                return True
    return False


def test_a_only_graph_module_imports_an_http_client() -> None:
    offenders: set[str] = set()
    for path in _SRC.rglob("*.py"):
        rel = path.relative_to(_SRC).as_posix()
        if rel.startswith("testing/"):
            continue  # CI test infrastructure, not production egress surface
        if _imports_http_client(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.add(rel)
    assert offenders == ALLOWED_HTTP_MODULES, f"{_GUARD_MESSAGE}\n  offenders={sorted(offenders)}"


def test_b_base_urls_flow_through_validate_source_url() -> None:
    tree = ast.parse((_SRC / "m365" / "graph.py").read_text(encoding="utf-8"))
    validated: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _call_name(node.value) == "validate_source_url"
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    validated.add(target.id)
    assert {"graph_base", "login_base"} <= validated, (
        f"{_GUARD_MESSAGE}\n  both Graph and login base URLs must be assigned from "
        f"validate_source_url(...); validated={sorted(validated)}"
    )


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


_HOSTILE = [
    "http://graph.microsoft.com/v1.0",  # not https
    "https://169.254.169.254/v1.0",  # metadata IP literal
    "https://2130706433/v1.0",  # packed-integer IPv4
    "https://graph.evil.example.com/v1.0",  # https but off-allowlist
]


@pytest.mark.parametrize("hostile", _HOSTILE)
def test_c_construction_refuses_hostile_graph_endpoint(hostile: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        GraphClient(m365_settings(graph_base_url=hostile))


def test_c_rejected_endpoint_never_emits_a_request() -> None:
    recorder: list[httpx.Request] = []
    transport = httpx.MockTransport(m365_recording_transport(recorder))
    with pytest.raises(UnsafeEndpointError):
        GraphClient(
            m365_settings(graph_base_url="https://169.254.169.254/v1.0"), transport=transport
        )
    assert recorder == []  # construction fails first — the exfil-never-POSTs proof


# -- Slack (NE-045) -----------------------------------------------------------


def test_b_slack_base_url_flows_through_validate_source_url() -> None:
    tree = ast.parse((_SRC / "slack" / "client.py").read_text(encoding="utf-8"))
    validated: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _call_name(node.value) == "validate_source_url"
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    validated.add(target.id)
    assert "api_base" in validated, (
        f"{_GUARD_MESSAGE}\n  the Slack base URL must be assigned from "
        f"validate_source_url(...) into a variable named 'api_base'; validated={sorted(validated)}"
    )


_HOSTILE_SLACK = [
    "http://slack.com/api",  # not https
    "https://169.254.169.254/api",  # metadata IP literal
    "https://2130706433/api",  # packed-integer IPv4
    "https://slack.evil.example.com/api",  # https but off-allowlist
]


@pytest.mark.parametrize("hostile", _HOSTILE_SLACK)
def test_c_construction_refuses_hostile_slack_endpoint(hostile: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        SlackClient(slack_settings(api_base_url=hostile))


def test_c_rejected_slack_endpoint_never_emits_a_request() -> None:
    recorder: list[httpx.Request] = []
    transport = httpx.MockTransport(slack_recording_transport(recorder))
    with pytest.raises(UnsafeEndpointError):
        SlackClient(slack_settings(api_base_url="https://169.254.169.254/api"), transport=transport)
    assert recorder == []  # construction fails first — the exfil-never-POSTs proof


# -- Salesforce (NE-047) --------------------------------------------------------


def test_b_salesforce_base_urls_flow_through_validate_source_url() -> None:
    tree = ast.parse((_SRC / "salesforce" / "client.py").read_text(encoding="utf-8"))
    validated: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _call_name(node.value) == "validate_source_url"
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    validated.add(target.id)
    assert {"instance_base", "login_base"} <= validated, (
        f"{_GUARD_MESSAGE}\n  both the Salesforce instance and login base URLs must be "
        f"assigned from validate_source_url(...); validated={sorted(validated)}"
    )


_HOSTILE_SALESFORCE = [
    "http://acme.my.salesforce.com",  # not https
    "https://169.254.169.254",  # metadata IP literal
    "https://2130706433",  # packed-integer IPv4
    "https://acme.my.salesforce.com.evil.example.com",  # dot-suffix bypass attempt
    "https://evil.example.com",  # https but off-allowlist
]


@pytest.mark.parametrize("hostile", _HOSTILE_SALESFORCE)
def test_c_construction_refuses_hostile_salesforce_endpoint(hostile: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        SalesforceClient(salesforce_settings(instance_url=hostile))


def test_c_rejected_salesforce_endpoint_never_emits_a_request() -> None:
    recorder: list[httpx.Request] = []
    transport = httpx.MockTransport(salesforce_recording_transport(recorder))
    with pytest.raises(UnsafeEndpointError):
        SalesforceClient(
            salesforce_settings(instance_url="https://169.254.169.254"), transport=transport
        )
    assert recorder == []  # construction fails first — the exfil-never-POSTs proof


# -- Jira (NE-093) ------------------------------------------------------------


def test_b_jira_base_url_flows_through_validate_source_url() -> None:
    tree = ast.parse((_SRC / "jira" / "client.py").read_text(encoding="utf-8"))
    validated: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _call_name(node.value) == "validate_source_url"
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    validated.add(target.id)
    assert "base" in validated, (
        f"{_GUARD_MESSAGE}\n  the Jira base URL must be assigned from "
        f"validate_source_url(...) into a variable named 'base'; validated={sorted(validated)}"
    )


_HOSTILE_JIRA = [
    "http://acme.atlassian.net",  # not https
    "https://169.254.169.254",  # metadata IP literal
    "https://2130706433",  # packed-integer IPv4
    "https://acme.atlassian.net.evil.example.com",  # dot-suffix bypass attempt
    "https://evil.example.com",  # https but off-allowlist
]


@pytest.mark.parametrize("hostile", _HOSTILE_JIRA)
def test_c_construction_refuses_hostile_jira_endpoint(hostile: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        JiraClient(jira_settings(base_url=hostile))


def test_c_rejected_jira_endpoint_never_emits_a_request() -> None:
    recorder: list[httpx.Request] = []
    transport = httpx.MockTransport(jira_recording_transport(recorder))
    with pytest.raises(UnsafeEndpointError):
        JiraClient(jira_settings(base_url="https://169.254.169.254"), transport=transport)
    assert recorder == []  # construction fails first — the exfil-never-POSTs proof


# -- Zendesk (NE-093) ---------------------------------------------------------


def test_b_zendesk_base_url_flows_through_validate_source_url() -> None:
    tree = ast.parse((_SRC / "zendesk" / "client.py").read_text(encoding="utf-8"))
    validated: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _call_name(node.value) == "validate_source_url"
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    validated.add(target.id)
    assert "base" in validated, (
        f"{_GUARD_MESSAGE}\n  the Zendesk base URL must be assigned from "
        f"validate_source_url(...) into a variable named 'base'; validated={sorted(validated)}"
    )


_HOSTILE_ZENDESK = [
    "http://acme.zendesk.com",  # not https
    "https://169.254.169.254",  # metadata IP literal
    "https://2130706433",  # packed-integer IPv4
    "https://acme.zendesk.com.evil.example.com",  # dot-suffix bypass attempt
    "https://evil.example.com",  # https but off-allowlist
]


@pytest.mark.parametrize("hostile", _HOSTILE_ZENDESK)
def test_c_construction_refuses_hostile_zendesk_endpoint(hostile: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        ZendeskClient(zendesk_settings(base_url=hostile))


def test_c_rejected_zendesk_endpoint_never_emits_a_request() -> None:
    recorder: list[httpx.Request] = []
    transport = httpx.MockTransport(zendesk_recording_transport(recorder))
    with pytest.raises(UnsafeEndpointError):
        ZendeskClient(zendesk_settings(base_url="https://169.254.169.254"), transport=transport)
    assert recorder == []  # construction fails first — the exfil-never-POSTs proof


# -- Teams (NE-080) -----------------------------------------------------------


def test_b_teams_base_urls_flow_through_validate_source_url() -> None:
    tree = ast.parse((_SRC / "teams" / "graph.py").read_text(encoding="utf-8"))
    validated: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _call_name(node.value) == "validate_source_url"
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    validated.add(target.id)
    assert {"graph_base", "login_base"} <= validated, (
        f"{_GUARD_MESSAGE}\n  both the Teams Graph and login base URLs must be assigned "
        f"from validate_source_url(...); validated={sorted(validated)}"
    )


_HOSTILE_TEAMS = [
    "http://graph.microsoft.com/v1.0",  # not https
    "https://169.254.169.254/v1.0",  # metadata IP literal
    "https://2130706433/v1.0",  # packed-integer IPv4
    "https://graph.evil.example.com/v1.0",  # https but off-allowlist
]


@pytest.mark.parametrize("hostile", _HOSTILE_TEAMS)
def test_c_construction_refuses_hostile_teams_endpoint(hostile: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        TeamsGraphClient(teams_settings(graph_base_url=hostile))


def test_c_rejected_teams_endpoint_never_emits_a_request() -> None:
    recorder: list[httpx.Request] = []
    transport = httpx.MockTransport(teams_recording_transport(recorder))
    with pytest.raises(UnsafeEndpointError):
        TeamsGraphClient(
            teams_settings(graph_base_url="https://169.254.169.254/v1.0"), transport=transport
        )
    assert recorder == []  # construction fails first — the exfil-never-POSTs proof
