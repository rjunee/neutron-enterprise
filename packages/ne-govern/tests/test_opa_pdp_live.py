# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end proof against a REAL OPA 1.18.2 — one this test STARTS and OWNS.

Every decision vector goes through the real ``GovernedPdp`` + ``OpaClient`` over a
real HTTP wire to a real OPA evaluating the shipped ``packages/ne-govern/policies``
bundle, auditing into a ``RecordingAuditSink``. That is the coverage this module has
always carried, and it is unchanged. What changed is WHOSE engine answers.

⛔ IT NO LONGER UPLOADS ANYTHING, TO ANYTHING. This module used to ``PUT`` the working
tree's Rego into whatever OPA answered ``NE_GOVERN_OPA_URL``. On 2026-08-30 that took
governance down: the variable defaulted to ``http://localhost:8181``, which on a box
running the stack IS the deployed PDP, so a plain ``pytest packages/ne-govern``
uploaded a working tree into live governance — every module held twice, two
``tiers.rego`` versions answering one query, HTTP 500 ``eval_conflict_error`` instead
of decisions. It failed CLOSED, so nothing was wrongly allowed; governance was
degraded by running a unit test.

The consent gate added then — an EXPLICIT ``NE_GOVERN_OPA_URL``, no default, on the
principle that reachability is not consent — did NOT close the class, and the reason
is worth keeping: this file's own documented recipe was
``NE_GOVERN_OPA_URL=http://localhost:8181``, so following the instructions satisfied
the gate. A recipe that names the deployed port is a loaded gun in every stale
worktree on the box. On 2026-09-09 the same shape in ``packages/ne-mcp`` fired again —
12 failed governance decisions over ~90 seconds (16:08:30Z → 16:09:58Z), planted by a
run from a stale worktree. ``opa check`` cannot see that state: a merged-conflict
bundle only fails when it is EVALUATED.

So the fix here is structural, not procedural: the proof BRINGS ITS OWN PDP.
``conftest.disposable_opa_url`` starts the pinned ``opa`` binary serving the shipped
bundle READ-ONLY FROM DISK on a kernel-assigned loopback port and tears it down after.
There is no policy write left in this file, and no ``NE_GOVERN_OPA_URL`` read left to
aim one with. ``packages/ne-mcp/tests/test_opa_pdp_mutation_guardian.py`` enforces
that for every test in the repo, and its allowlist is now EMPTY.

To run it::

    uv run pytest packages/ne-govern/tests/test_opa_pdp_live.py -q

No sidecar, no ``ne dev up``, no variable. It runs wherever the ``opa`` binary is on
PATH — including CI, which installs the sha256-pinned 1.18.2 — and skips only where
the binary is absent. It was previously skipped unless an operator named a PDP, so it
effectively never ran on a PR; it does now.

WHAT WAS RETIRED WITH THE UPLOAD, AND WHY IT IS NOT LOST COVERAGE. The old
``_load_policies_into_opa`` uploaded ``version.rego``/``tiers.rego`` first because each
``PUT`` compiles the incoming module against whatever the server ALREADY holds for its
siblings, so a cross-reference could transiently go unsafe against a long-lived
sidecar. That ordering constraint was a property of the UPLOAD MECHANISM, not of the
policy: OPA compiles a from-disk load root as one unit, so the hazard does not exist
here and there is nothing left to order. The ``opa-live-shared-server`` xdist group
went with it — the fixture is session-scoped, so each worker owns its own server and
there is no shared external process left to race. Nothing about a DEPLOYED engine was
being proved that a local one does not prove: the subject is the shipped bundle plus
the real HTTP/PDP/audit path, all of which are byte-identical here.
"""

from __future__ import annotations

import asyncio

from ne.contracts.governance import DecisionEffect, PolicyRequest
from ne.contracts.types import Principal
from ne.govern.opa import OpaClient
from ne.govern.pdp import GovernedPdp

from .conftest import RecordingAuditSink, load_vectors


def test_all_vectors_through_the_live_pdp(disposable_opa_url: str) -> None:
    vectors = load_vectors()
    sink = RecordingAuditSink()
    pdp = GovernedPdp(OpaClient(disposable_opa_url), sink)

    async def scenario() -> None:
        for vector in vectors:
            request = PolicyRequest(
                principal=Principal(id=vector["input"]["principal"]["id"]),
                action=vector["input"]["action"],
                resource=vector["input"]["resource"],
                context=vector["input"].get("context", {}),
            )
            decision = await pdp.decide(request)
            assert decision.effect == DecisionEffect(vector["expected"]["effect"])
        await pdp.aclose()

    asyncio.run(scenario())
    assert len(sink.events) == len(vectors)
