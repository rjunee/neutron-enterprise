# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Binds the dev Keycloak realm to the middleware's expected claims/audience.

Live realm import cannot run in CI (no docker on the build host, NE-006
precedent); this asserts structural consistency so a mapper drift is caught.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from ne.identity.config import OidcConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
REALM_PATH = REPO_ROOT / "deploy/compose/dev/keycloak/ne-dev-realm.json"
PILOT_REALM_PATH = REPO_ROOT / "deploy/compose/pilot/keycloak/ne-pilot-realm.json"


@pytest.fixture(scope="module")
def realm() -> dict[str, Any]:
    return json.loads(REALM_PATH.read_text())


@pytest.fixture(scope="module")
def pilot_realm() -> dict[str, Any]:
    return json.loads(PILOT_REALM_PATH.read_text())


def _scope(realm: dict[str, Any], name: str) -> dict[str, Any]:
    for scope in realm.get("clientScopes", []):
        if scope.get("name") == name:
            return scope
    raise AssertionError(f"client scope {name!r} not found")


def _mapper(scope: dict[str, Any], mapper_type: str) -> dict[str, Any]:
    for mapper in scope.get("protocolMappers", []):
        if mapper.get("protocolMapper") == mapper_type:
            return mapper
    raise AssertionError(f"mapper {mapper_type!r} not in scope {scope.get('name')!r}")


def test_realm_is_valid_json(realm: dict[str, Any]) -> None:
    assert realm["realm"] == "ne-dev"


def test_groups_present(realm: dict[str, Any]) -> None:
    names = {g["name"] for g in realm.get("groups", [])}
    assert {"platform-admins", "engineering"} <= names


def test_dev_user_in_platform_admins(realm: dict[str, Any]) -> None:
    dev = next(u for u in realm["users"] if u["username"] == "dev")
    assert "/platform-admins" in dev.get("groups", [])


def test_groups_mapper_emits_groups_claim(realm: dict[str, Any]) -> None:
    mapper = _mapper(_scope(realm, "ne-groups"), "oidc-group-membership-mapper")
    assert mapper["config"]["claim.name"] == "groups"
    assert mapper["config"]["full.path"] == "false"
    assert mapper["config"]["access.token.claim"] == "true"


def test_audience_mapper_targets_ne_api(realm: dict[str, Any]) -> None:
    mapper = _mapper(_scope(realm, "ne-api-audience"), "oidc-audience-mapper")
    assert mapper["config"]["included.custom.audience"] == "ne-api"
    assert mapper["config"]["access.token.claim"] == "true"


def test_console_default_client_scopes(realm: dict[str, Any]) -> None:
    console = next(c for c in realm["clients"] if c["clientId"] == "ne-console")
    scopes = console["defaultClientScopes"]
    assert {"basic", "ne-groups", "ne-api-audience"} <= set(scopes)


def test_customer_idp_broker_placeholder_disabled(realm: dict[str, Any]) -> None:
    idps = realm.get("identityProviders", [])
    customer = next(i for i in idps if i["alias"] == "customer-idp")
    assert customer["enabled"] is False
    # placeholder must not ship a real secret (secrets hygiene)
    assert customer["config"]["clientSecret"] == "REPLACE-PER-CUSTOMER"


def test_pilot_realm_is_valid_json(pilot_realm: dict[str, Any]) -> None:
    assert pilot_realm["realm"] == "ne-pilot"


def test_pilot_entra_provider_shape(pilot_realm: dict[str, Any]) -> None:
    idps = pilot_realm["identityProviders"]
    assert len(idps) == 1
    p = idps[0]
    assert p["alias"] == "entra"
    assert p["providerId"] == "oidc"
    assert p["enabled"] is False
    assert p["trustEmail"] is True
    assert p["storeToken"] is False
    cfg = p["config"]
    assert cfg["clientAuthMethod"] == "client_secret_post"
    assert cfg["syncMode"] == "FORCE"
    assert cfg["useJwksUrl"] == "true"


def test_pilot_entra_clientsecret_is_interpolation(pilot_realm: dict[str, Any]) -> None:
    cfg = pilot_realm["identityProviders"][0]["config"]
    # This — not the hygiene regex — is the realm's real literal-secret guard:
    # a JSON-quoted credential key never matches the hygiene scanner's
    # same-line key regex at all.
    assert cfg["clientSecret"] == "${NE_PILOT_ENTRA_BROKER_CLIENT_SECRET}"
    assert cfg["clientId"] == "${NE_PILOT_ENTRA_BROKER_CLIENT_ID}"


def test_pilot_entra_alias_matches_terraform_redirect(pilot_realm: dict[str, Any]) -> None:
    # DERIVE, don't transliterate: collect the alias segment straight out of
    # the Terraform-registered redirect URIs rather than hardcoding both sides
    # — this fails on drift of EITHER artifact.
    tf_dirs = [
        REPO_ROOT / "deploy/terraform-eks-entra-broker",
        REPO_ROOT / "deploy/terraform-azure/modules/identity",
    ]
    aliases: set[str] = set()
    for tf_dir in tf_dirs:
        for tf_file in tf_dir.glob("*.tf"):
            aliases.update(re.findall(r"/broker/([A-Za-z0-9_-]+)/endpoint", tf_file.read_text()))
    assert aliases, "no /broker/<alias>/endpoint redirect URI found in Terraform"
    assert aliases == {pilot_realm["identityProviders"][0]["alias"]}


def test_pilot_entra_endpoints_use_tenant_interpolation(pilot_realm: dict[str, Any]) -> None:
    cfg = pilot_realm["identityProviders"][0]["config"]
    for key in ("issuer", "authorizationUrl", "tokenUrl", "jwksUrl"):
        value = cfg[key]
        assert value.startswith("https://login.microsoftonline.com/")
        assert "${NE_PILOT_ENTRA_TENANT_ID}" in value


def test_pilot_entra_group_mapper_scaffold(pilot_realm: dict[str, Any]) -> None:
    mappers = pilot_realm["identityProviderMappers"]
    for group in ("approver", "steward", "ops"):
        matches = [
            m
            for m in mappers
            if m["identityProviderAlias"] == "entra"
            and m["identityProviderMapper"] == "oidc-advanced-group-idp-mapper"
            and m["config"]["syncMode"] == "FORCE"
            and m["config"]["group"] == f"/{group}"
        ]
        assert len(matches) == 1, f"expected exactly one mapper for group {group!r}"
        claims = json.loads(matches[0]["config"]["claims"])
        assert any(entry["key"] == "groups" for entry in claims)


# --- NE-BEW-GROUPS: the >150-group overage CRIT (design §4.2) --------------
#
# Part 1 of the fix (Terraform group_membership_claims -> ApplicationGroup)
# reduces the probability of overage but cannot eliminate it. These tests
# prove part 2: a token shaped like the residual overage case is structurally
# denied, not silently resolved to zero groups. Every link in the chain
# (indicator mapper -> marker role -> conditional subflow -> deny) is DERIVED
# from the realm artifact itself, never hardcoded twice, so drift in either
# the mapper config or the flow wiring fails the test that reads it.


def _resolve_claim_path(token: dict[str, Any], dot_path: str) -> Any:
    """Resolve a Keycloak-style dot-path into a nested claim dict.

    Mirrors AbstractClaimMapper.getClaimValue / splitClaimPath: a dotted key
    like "_claim_names.groups" walks nested dict levels. Missing at any level
    resolves to None (Keycloak: claim simply doesn't match).
    """
    current: Any = token
    for part in dot_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _single_value_matches(resolved: Any, expected: str, is_regex: bool) -> bool:
    # Keycloak normalizes booleans to "true"/"false" strings, else str().
    normalized = ("true" if resolved else "false") if isinstance(resolved, bool) else str(resolved)
    if is_regex:
        return re.fullmatch(expected, normalized) is not None
    return normalized == expected


def _claim_value_matches(resolved: Any, expected: str, is_regex: bool) -> bool:
    if resolved is None:
        return False
    # Entra emits list-valued claims (e.g. "groups": [<guid>, ...]) — mirrors
    # Keycloak's AbstractClaimMapper.valueEquals, which matches if ANY
    # element of a list-valued claim matches, not the stringified list.
    if isinstance(resolved, list):
        return any(_single_value_matches(item, expected, is_regex) for item in resolved)
    return _single_value_matches(resolved, expected, is_regex)


def _advanced_idp_role_grants(pilot_realm: dict[str, Any], token: dict[str, Any]) -> set[str]:
    """Semantics-faithful re-implementation of AdvancedClaimToRoleMapper matching.

    For each oidc-advanced-role-idp-mapper on alias "entra": AND across the
    mapper's claims entries (each entry is a dot-path key + expected value,
    matched as a regex when are.claim.values.regex == "true" else exact
    string equality). A mapper that matches grants its configured role.
    Returns the union of granted roles across all matching mappers.
    """
    granted: set[str] = set()
    for mapper in pilot_realm.get("identityProviderMappers", []):
        if (
            mapper.get("identityProviderAlias") != "entra"
            or mapper.get("identityProviderMapper") != "oidc-advanced-role-idp-mapper"
        ):
            continue
        config = mapper["config"]
        entries = json.loads(config["claims"])
        is_regex = config.get("are.claim.values.regex") == "true"
        if all(
            _claim_value_matches(_resolve_claim_path(token, entry["key"]), entry["value"], is_regex)
            for entry in entries
        ):
            granted.add(config["role"])
    return granted


def _claim_to_group_grants(pilot_realm: dict[str, Any], token: dict[str, Any]) -> set[str]:
    """Same matching semantics as _advanced_idp_role_grants, over the
    oidc-advanced-group-idp-mapper entries, returning granted Keycloak
    group paths instead of roles."""
    granted: set[str] = set()
    for mapper in pilot_realm.get("identityProviderMappers", []):
        if (
            mapper.get("identityProviderAlias") != "entra"
            or mapper.get("identityProviderMapper") != "oidc-advanced-group-idp-mapper"
        ):
            continue
        config = mapper["config"]
        entries = json.loads(config["claims"])
        is_regex = config.get("are.claim.values.regex") == "true"
        if all(
            _claim_value_matches(_resolve_claim_path(token, entry["key"]), entry["value"], is_regex)
            for entry in entries
        ):
            granted.add(config["group"])
    return granted


def _overage_marker_role(pilot_realm: dict[str, Any]) -> str:
    """Derive the overage marker role from the overage mappers' own config,
    NOT positionally from roles.realm[0]. If a future unit prepends an
    unrelated realm role to roles.realm, an index-0 read would silently
    start asserting against the wrong role name and the CRIT proof would
    degrade instead of failing loudly."""
    overage_mappers = [
        m
        for m in pilot_realm.get("identityProviderMappers", [])
        if m.get("identityProviderAlias") == "entra"
        and m.get("identityProviderMapper") == "oidc-advanced-role-idp-mapper"
    ]
    roles = {m["config"]["role"] for m in overage_mappers}
    assert len(roles) == 1, f"expected exactly one overage marker role, found {roles}"
    return next(iter(roles))


def test_pilot_entra_overage_marker_role_and_mappers(pilot_realm: dict[str, Any]) -> None:
    realm_roles = pilot_realm["roles"]["realm"]
    overage_roles = [r for r in realm_roles if r["name"] == "entra-groups-overage"]
    assert len(overage_roles) == 1, "expected exactly one entra-groups-overage realm role"

    overage_mappers = [
        m
        for m in pilot_realm["identityProviderMappers"]
        if m["identityProviderAlias"] == "entra"
        and m["identityProviderMapper"] == "oidc-advanced-role-idp-mapper"
    ]
    assert overage_mappers, "expected at least one oidc-advanced-role-idp-mapper on alias entra"

    indicator_mappers = [
        m
        for m in overage_mappers
        if any(entry["key"] == "_claim_names.groups" for entry in json.loads(m["config"]["claims"]))
    ]
    assert indicator_mappers, "expected an overage mapper keyed on _claim_names.groups"
    for m in indicator_mappers:
        assert m["config"]["are.claim.values.regex"] == "true"
        assert m["config"]["syncMode"] == "FORCE"

    targeted_roles = {m["config"]["role"] for m in overage_mappers}
    assert targeted_roles == {"entra-groups-overage"}, (
        "every overage mapper must target the SAME marker role"
    )
    for m in overage_mappers:
        assert m["config"]["syncMode"] == "FORCE"


def test_pilot_entra_overage_fail_loud_flow_wiring(pilot_realm: dict[str, Any]) -> None:
    marker_role = _overage_marker_role(pilot_realm)
    assert marker_role == "entra-groups-overage"

    idp = pilot_realm["identityProviders"][0]
    top_alias = idp["postBrokerLoginFlowAlias"]

    flows = {f["alias"]: f for f in pilot_realm["authenticationFlows"]}
    assert top_alias in flows, (
        f"postBrokerLoginFlowAlias {top_alias!r} not defined in authenticationFlows"
    )
    top_flow = flows[top_alias]

    top_executions = top_flow["authenticationExecutions"]
    assert len(top_executions) == 1
    top_exec = top_executions[0]
    assert top_exec["requirement"] == "CONDITIONAL"
    sub_alias = top_exec["flowAlias"]
    assert sub_alias in flows, f"subflow {sub_alias!r} referenced by top flow not defined"

    sub_flow = flows[sub_alias]
    sub_executions = sorted(sub_flow["authenticationExecutions"], key=lambda e: e["priority"])
    assert len(sub_executions) == 2

    condition_exec, deny_exec = sub_executions
    assert condition_exec["authenticator"] == "conditional-user-role"
    assert condition_exec["requirement"] == "REQUIRED"
    assert deny_exec["authenticator"] == "deny-access-authenticator"
    assert deny_exec["requirement"] == "REQUIRED"
    assert condition_exec["priority"] < deny_exec["priority"]

    configs = {c["alias"]: c["config"] for c in pilot_realm["authenticatorConfig"]}
    assert condition_exec["authenticatorConfig"] in configs
    assert deny_exec["authenticatorConfig"] in configs

    condition_config = configs[condition_exec["authenticatorConfig"]]
    assert condition_config["condUserRole"] == marker_role
    assert condition_config["negate"] == "false"

    deny_config = configs[deny_exec["authenticatorConfig"]]
    assert deny_config["denyErrorMessage"], "deny message must be non-empty (operator-actionable)"


def test_overage_shaped_token_fails_loud_not_silent(pilot_realm: dict[str, Any]) -> None:
    marker_role = _overage_marker_role(pilot_realm)

    # THE CRIT PROOF: exactly the token shape Entra emits on overage —
    # _claim_names.groups present (pointing at the Graph overage source), no
    # "groups" key at all.
    overage_token = {
        "_claim_names": {"groups": "src1"},
        "_claim_sources": {
            "src1": {"endpoint": "https://graph.microsoft.com/v1.0/me/getMemberObjects"}
        },
    }

    # This IS the silent-under-authz shape: zero platform groups resolved.
    assert _claim_to_group_grants(pilot_realm, overage_token) == set()

    # But the SAME token necessarily trips the deny chain proven wired in
    # test_pilot_entra_overage_fail_loud_flow_wiring — it can no longer
    # resolve silently.
    assert _advanced_idp_role_grants(pilot_realm, overage_token) == {marker_role}

    # The hasgroups belt (Entra's implicit-flow overage variant) also fires.
    hasgroups_token = {"hasgroups": True}
    assert _advanced_idp_role_grants(pilot_realm, hasgroups_token) == {marker_role}


def test_normal_token_not_denied(pilot_realm: dict[str, Any]) -> None:
    normal_token = {"groups": ["REPLACE-ENTRA-GROUP-ID-approver"]}

    # No over-blocking: a normal grouped login, and even a genuinely
    # zero-groups user without the overage indicator, must not be denied.
    assert _advanced_idp_role_grants(pilot_realm, normal_token) == set()

    # The approver Claim-to-Group mapper still fires normally.
    assert _claim_to_group_grants(pilot_realm, normal_token) == {"/approver"}


# --- NE-BEW-GROUPS-ZEROGROUPS: the user-session-note carrier for the
# zero-mapped-groups observability signal (ne.identity.provisioning) -------
#
# Keycloak stamps the built-in `identity_provider` USER SESSION NOTE on every
# brokered login (IdentityBrokerService.setUserSessionNote); the
# oidc-usersessionmodel-note-mapper below (precedent: the `basic` scope's
# auth_time mapper) emits it as a token claim so JitProvisioner can tell a
# brokered login apart from a local/service-account one. Both realms carry
# this in `ne-groups` (never a newly-declared `basic` scope in the dev realm
# — that would clobber Keycloak's built-in basic-scope mappers at import).


def _idp_note_mapper(realm: dict[str, Any]) -> dict[str, Any]:
    return _mapper(_scope(realm, "ne-groups"), "oidc-usersessionmodel-note-mapper")


def test_dev_realm_identity_provider_note_mapper(realm: dict[str, Any]) -> None:
    mapper = _idp_note_mapper(realm)
    assert mapper["config"]["user.session.note"] == "identity_provider"
    assert mapper["config"]["access.token.claim"] == "true"
    # Derived cross-artifact bind: realm claim.name and the code default must
    # agree, or drift in either artifact fails CI (not hardcoded twice).
    assert mapper["config"]["claim.name"] == OidcConfig.model_fields["idp_claim"].default


def test_pilot_realm_identity_provider_note_mapper(pilot_realm: dict[str, Any]) -> None:
    mapper = _idp_note_mapper(pilot_realm)
    assert mapper["config"]["user.session.note"] == "identity_provider"
    assert mapper["config"]["access.token.claim"] == "true"
    assert mapper["config"]["claim.name"] == OidcConfig.model_fields["idp_claim"].default


# --- access-token lifespan vs the run execution window (demo defect: a Run
# freezes the submitting bearer's exp into its authorization snapshot at
# admission, and the worker fail-closed re-checks that snapshot before every
# protected disclosure — so a token shorter than a run's execution window kills
# a legitimate run mid-flight with AuthorizationExpiredError).

CONSOLE_CLIENT_PATH = REPO_ROOT / "packages/ne-console/src/api/client.ts"


def _console_run_credential_floor_seconds() -> int:
    """Derived cross-artifact bind: parse the console's pre-submit freshness
    floor from its source at test time (same idiom as the Terraform-derived
    entra alias above), so neither side can drift silently."""
    source = CONSOLE_CLIENT_PATH.read_text()
    match = re.search(r"export const RUN_CREDENTIAL_MIN_REMAINING_SECONDS = (\d+);", source)
    assert match is not None, (
        "RUN_CREDENTIAL_MIN_REMAINING_SECONDS not found in the console client — "
        "the pre-submit run-credential freshness guard was removed or renamed"
    )
    return int(match.group(1))


#: Upper bound on ``accessTokenLifespan``. 900s is already 3x Keycloak's
#: conservative 300s default and sits below mainstream IdP defaults (Okta 3600s,
#: Entra ~60-90m), so it is defensible for a TLS-only single-tenant box. It is
#: NOT a knob for approval latency -- see the assertion message below.
RUN_CREDENTIAL_LIFESPAN_CEILING = 900


@pytest.mark.parametrize("realm_fixture", ["realm", "pilot_realm"])
def test_access_token_lifespan_covers_run_credential_floor(
    realm_fixture: str, request: pytest.FixtureRequest
) -> None:
    """The realm must be able to mint a token satisfying the console's
    pre-submit floor, and the floor itself must exceed the 400s agentic run
    budget cap — otherwise the console renews on every submit forever (floor
    unreachable) or an admitted run can still outlive its own credential."""
    realm_json = request.getfixturevalue(realm_fixture)
    lifespan = realm_json.get("accessTokenLifespan")
    floor = _console_run_credential_floor_seconds()
    assert isinstance(lifespan, int), (
        f"{realm_json['realm']}: accessTokenLifespan must be pinned explicitly — "
        "the Keycloak default (300s) is shorter than a single agentic run"
    )
    assert floor > 400, "the floor must exceed the 400s agentic run budget cap"
    assert lifespan >= floor, (
        f"{realm_json['realm']}: accessTokenLifespan {lifespan}s cannot satisfy "
        f"the console's {floor}s pre-submit run-credential floor"
    )
    assert lifespan <= RUN_CREDENTIAL_LIFESPAN_CEILING, (
        f"{realm_json['realm']}: accessTokenLifespan {lifespan}s exceeds the "
        f"{RUN_CREDENTIAL_LIFESPAN_CEILING}s ceiling. This bound exists to stop one "
        "specific shortcut: making Tier-2 approvals decidable by stretching the "
        "bearer's life to the approval SLA. A run owner's approval window and a "
        "bearer's replay window are different quantities and must not be conflated "
        "-- covering even the declared 4h tier2_sla_s would mint a >=14400s "
        "credential for EVERY principal and route. The sanctioned fix is deferred "
        "resume with owner re-authorization plus a per-action policy horizon "
        "(ApprovalsConfig already declares tier2_sla_s/expiry_s); see POST_DEMO.md "
        "'APPROVAL HORIZON'. Raise this ceiling only with that design, never instead of it."
    )
