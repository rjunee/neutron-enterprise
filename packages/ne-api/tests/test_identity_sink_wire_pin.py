# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-T2X-5-RECORDER-WIRE (instance 2, folded in from NE-IDENTITY-SINK-WIRE-PIN).

``ne.api.app.create_app`` calls ``build_identity(..., audit_sink=platform.sink)``
at app.py:338 so a brokered login that resolves ZERO mapped groups lands in the
queryable, hash-chained platform audit log (NE-BEW-GROUPS-ZEROGROUPS). Argus
proved by mutation that DELETING that keyword argument left BOTH the ne-api and
ne-identity suites fully green (687 passed) while the ``identity.
brokered_login.zero_groups`` event silently degraded to log-only. PR #339
closed the ``ne-identity`` half (``build_identity`` itself asserts the sink's
identity); this file closes the ``ne-api`` composition-root half.

app.py is NOT edited by this unit — its wiring already exists; this test is
the RED WIRE that makes deleting it loud. The assertion is BEHAVIOURAL: build
the real app and check the installed middleware's provisioner carries
``platform.sink`` BY IDENTITY (``is``), never a presence/not-None check (which
would pass any sink a factory might invent and still lose the platform's
hash-chained audit chain), and NEVER a static/AST/substring assertion over the
``app.py`` source text (the #335/#336/#337 vacuity shape this repo has already
shipped three times — that shape is explicitly banned here; this test would be
satisfied by the literal string appearing in a comment, so it deliberately
avoids that check entirely).

Private-attribute-access precedent: ``test_composition.py`` asserts
``state.executor._sink is state.sink``; ne-identity's
``test_zero_groups_signal.py:316`` asserts ``provisioner._audit_sink is sink``.
"""

from __future__ import annotations

from starlette.routing import Mount

from ne.identity.middleware import OidcAuthMiddleware

from .conftest import AppBuilder, get_state


def test_api_mount_provisioner_carries_platform_sink_by_identity(make_app: AppBuilder) -> None:
    h = make_app()
    state = get_state(h.app)

    mount = next(r for r in h.app.routes if isinstance(r, Mount) and r.path == "/api")
    mw = mount.app
    assert isinstance(mw, OidcAuthMiddleware)

    # BY IDENTITY — a presence check (`is not None`) would pass any sink the
    # factory might invent and still silently lose the platform's ONE
    # hash-chained audit chain (C10/C7). This is the exact assertion whose
    # ABSENCE let Argus delete `audit_sink=platform.sink` from app.py:338 and
    # keep 687 tests green.
    assert mw._provisioner._audit_sink is state.sink
