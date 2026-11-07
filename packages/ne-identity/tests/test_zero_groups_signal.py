# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-BEW-GROUPS-ZEROGROUPS: a brokered login resolving zero MAPPED groups.

Makes the missed-Entra-app-group-assignment shape OBSERVABLE (audit event +
structured WARNING) without ever blocking the login. The control tests are
the load-bearing ones here: an ordinary brokered login WITH mapped groups, a
local (non-brokered) login, and a deployment with no role mapping configured
must all stay silent, or the signal is unconditional noise (PR #337 lesson).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.identity import TokenClaims
from ne.identity.config import IdentitySettings, OidcConfig, RoleMappingConfig
from ne.identity.provisioning import InMemoryPrincipalStore, JitProvisioner
from ne.identity.roles import ROLE_PREFIX
from ne.schema import AuditOutcome

from .conftest import TEST_AUDIENCE, TEST_ISSUER, FailingSink, ListSink, run

_LOGGER_NAME = "ne.identity.provisioning"

# Pinned as a LITERAL, deliberately NOT imported from ne.identity.provisioning:
# importing the module's own ZERO_GROUPS_AUDIT_ACTION constant here would make
# the assertion below vacuous under a mutation that changes that constant's
# value (both sides would drift together and the mutation would stay green —
# exactly the "field-presence-without-value-discrimination" defect class
# flagged in this unit's brief). The literal must match
# ne.identity.provisioning.ZERO_GROUPS_AUDIT_ACTION by construction, not import.
_EXPECTED_ZERO_GROUPS_ACTION = "identity.brokered_login.zero_groups"

# Sentinel meaning "omit this claim entirely" (as opposed to setting it to
# None / "" / a real value) so tests can precisely control claim SHAPE.
_OMIT = object()


def _claims(
    idp: object = _OMIT, groups: object = _OMIT, subject: str = "user-1", **extra: object
) -> TokenClaims:
    payload: dict[str, object] = {"sub": subject, "iss": TEST_ISSUER}
    if idp is not _OMIT:
        payload["identity_provider"] = idp
    if groups is not _OMIT:
        payload["groups"] = groups
    payload.update(extra)
    return TokenClaims(subject=subject, issuer=TEST_ISSUER, claims=payload)


class _FakeClock:
    """An injectable, advanceable monotonic-style clock for dedup tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _mapped_config() -> RoleMappingConfig:
    return RoleMappingConfig(
        mapping={"platform-admins": ("admin",), "engineering": ("developer",)},
        default_roles=("viewer",),
    )


def _warning_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER_NAME and r.levelno == logging.WARNING]


def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER_NAME and r.levelno == logging.ERROR]


@pytest.fixture(autouse=True)
def _frozen_config_hash() -> Iterator[None]:
    """Freeze a config hash so new_audit_event() can stamp it (SPEC §4).

    Scoped to this module only: reset in the finally so no other test file's
    assumptions about an unfrozen hash are disturbed.
    """
    reset_config_hash_for_testing()
    freeze_config_hash(
        IdentitySettings(oidc=OidcConfig(issuer=TEST_ISSUER, audience=TEST_AUDIENCE))
    )
    try:
        yield
    finally:
        reset_config_hash_for_testing()


@pytest.fixture(autouse=True)
def _capture_provisioning_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)


# --- fire cases -------------------------------------------------------------


def test_brokered_no_groups_claim_fires(caplog: pytest.LogCaptureFixture) -> None:
    store = InMemoryPrincipalStore()
    sink = ListSink()
    prov = JitProvisioner(store, _mapped_config(), audit_sink=sink)

    principal = run(prov.provision(_claims(idp="entra")))

    # The login still SUCCEEDS — default roles only, never blocked.
    assert principal.id == "user-1"
    assert principal.groups == (f"{ROLE_PREFIX}viewer",)
    assert run(store.get("user-1")) == principal

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.action == _EXPECTED_ZERO_GROUPS_ACTION
    assert event.actor == "user-1"
    assert event.resource == "principal:user-1"
    assert event.outcome == AuditOutcome.success
    assert event.provenance == {"idp": "entra", "idp_group_count": "0", "mapped_group_count": "0"}

    warnings = _warning_records(caplog)
    assert len(warnings) == 1
    assert warnings[0].getMessage() == (
        "brokered login resolved zero mapped platform groups (idp=entra, idp_group_count=0)"
    )


def test_brokered_empty_groups_list_fires() -> None:
    # The OTHER missed-assignment token shape: an explicit empty groups list.
    store = InMemoryPrincipalStore()
    sink = ListSink()
    prov = JitProvisioner(store, _mapped_config(), audit_sink=sink)

    run(prov.provision(_claims(idp="entra", groups=[])))

    assert len(sink.events) == 1
    assert sink.events[0].provenance["idp_group_count"] == "0"
    assert sink.events[0].provenance["mapped_group_count"] == "0"


def test_brokered_all_unmapped_groups_fires() -> None:
    # Groups ARE present but none is a mapping key (e.g. a stale/typo'd
    # config) — same silent under-authorization, must still fire. Pins the
    # "zero MAPPED groups" (not "zero groups in the claim") definition.
    store = InMemoryPrincipalStore()
    sink = ListSink()
    prov = JitProvisioner(store, _mapped_config(), audit_sink=sink)

    run(prov.provision(_claims(idp="entra", groups=["unknown-team"])))

    assert len(sink.events) == 1
    assert sink.events[0].provenance == {
        "idp": "entra",
        "idp_group_count": "1",
        "mapped_group_count": "0",
    }


# --- control / silence cases (the load-bearing tests) -----------------------


def test_brokered_login_with_mapped_groups_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    """CONTROL: an ordinary successful brokered login must stay silent."""
    store = InMemoryPrincipalStore()
    sink = ListSink()
    prov = JitProvisioner(store, _mapped_config(), audit_sink=sink)

    principal = run(prov.provision(_claims(idp="entra", groups=["platform-admins"])))

    assert f"{ROLE_PREFIX}admin" in principal.groups
    assert sink.events == []
    assert _warning_records(caplog) == []


@pytest.mark.parametrize("idp_value", [_OMIT, "", 123])
def test_local_login_zero_groups_is_silent(
    idp_value: object, caplog: pytest.LogCaptureFixture
) -> None:
    """No / empty / non-str identity_provider claim => not brokered => silent.

    Covers: no claim at all (local/service-account login), an empty-string
    value, and a non-str value (a claim shape the signal must not mistake for
    a real IdP alias).
    """
    store = InMemoryPrincipalStore()
    sink = ListSink()
    prov = JitProvisioner(store, _mapped_config(), audit_sink=sink)

    run(prov.provision(_claims(idp=idp_value)))

    assert sink.events == []
    assert _warning_records(caplog) == []


def test_no_mapping_configured_never_fires(caplog: pytest.LogCaptureFixture) -> None:
    """A group-less deployment (empty role mapping) never fires, ever."""
    store = InMemoryPrincipalStore()
    sink = ListSink()
    config = RoleMappingConfig(mapping={}, default_roles=("viewer",))
    prov = JitProvisioner(store, config, audit_sink=sink)

    run(prov.provision(_claims(idp="entra")))

    assert sink.events == []
    assert _warning_records(caplog) == []


# --- observable-not-blocking -------------------------------------------------


def test_sink_failure_never_blocks_login(caplog: pytest.LogCaptureFixture) -> None:
    store = InMemoryPrincipalStore()
    sink = FailingSink()
    prov = JitProvisioner(store, _mapped_config(), audit_sink=sink)

    # provision() must NOT raise even though the sink append fails.
    principal = run(prov.provision(_claims(idp="entra")))
    assert principal.id == "user-1"

    # The WARNING still fired (signal not fully silent under sink outage).
    warnings = _warning_records(caplog)
    assert len(warnings) == 1

    # And the failure itself was logged at exception/ERROR level, not lost.
    errors = _error_records(caplog)
    assert len(errors) == 1


def test_no_sink_still_warns(caplog: pytest.LogCaptureFixture) -> None:
    """The default build_identity() path (no sink wired) still warns."""
    store = InMemoryPrincipalStore()
    prov = JitProvisioner(store, _mapped_config())  # audit_sink defaults to None

    principal = run(prov.provision(_claims(idp="entra")))
    assert principal.id == "user-1"

    warnings = _warning_records(caplog)
    assert len(warnings) == 1


# --- overage-deny boundary untouched (regression guard) ---------------------


def test_overage_indicator_shaped_claims_are_not_special_cased() -> None:
    """The overage-deny path lives entirely in Keycloak realm config (a
    separate CONDITIONAL subflow keyed on a realm role) — JitProvisioner
    never sees or reasons about `_claim_names`/`hasgroups`. This test pins
    that provisioning treats such a token exactly like any other
    zero-mapped-groups brokered login: it still provisions successfully and
    still fires the (unrelated) observability signal, proving the two
    mechanisms are decoupled and NE-BEW-GROUPS part 2 is untouched by this
    unit (part 2's own behavior is proven in test_realm_config.py).
    """
    store = InMemoryPrincipalStore()
    sink = ListSink()
    prov = JitProvisioner(store, _mapped_config(), audit_sink=sink)

    principal = run(
        prov.provision(
            _claims(
                idp="entra",
                _claim_names={"groups": "src1"},
            )
        )
    )

    assert principal.id == "user-1"
    assert len(sink.events) == 1


# --- production wiring (Argus P1) -------------------------------------------
# The signal is only reachable in production if build_identity actually hands
# its audit_sink and idp_claim down to the JitProvisioner. Argus proved by
# mutation that deleting `audit_sink=audit_sink` from factory.py left BOTH
# suites fully green while the queryable audit event silently disappeared,
# degrading the deliverable to log-only. A feature whose production wiring is
# untested is a feature that can be removed without anything going red.


def test_build_identity_threads_audit_sink_and_idp_claim_to_the_provisioner() -> None:
    """``build_identity`` must pass BOTH the audit sink and the configured
    ``idp_claim`` through to the ``JitProvisioner`` it constructs.

    Asserts IDENTITY of the sink object (``is``), not merely that some sink is
    present — a presence check is satisfied by any sink, including a default
    the factory invented, which would still lose the platform's real audit
    chain. Same discrimination rule as the value-vs-presence assertions above.
    """
    from ne.identity.factory import build_identity

    sink = ListSink()
    settings = IdentitySettings(
        oidc=OidcConfig(
            issuer=TEST_ISSUER,
            audience=TEST_AUDIENCE,
            idp_claim="identity_provider",
        ),
        roles=_mapped_config(),
    )

    async def _app(scope: object, receive: object, send: object) -> None:  # pragma: no cover
        raise AssertionError("the wrapped app must not be invoked by this test")

    middleware = build_identity(settings, _app, store=InMemoryPrincipalStore(), audit_sink=sink)

    provisioner = middleware._provisioner
    assert isinstance(provisioner, JitProvisioner)
    assert provisioner._audit_sink is sink, (
        "build_identity must thread the PLATFORM audit sink into the provisioner — "
        "without it the zero-groups signal silently degrades to log-only in production"
    )
    assert provisioner._idp_claim == "identity_provider", (
        "build_identity must thread the configured idp_claim — a wrong/absent claim "
        "makes the brokered-login discriminator always False, disabling the signal"
    )
