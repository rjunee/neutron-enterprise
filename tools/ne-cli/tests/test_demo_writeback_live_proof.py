# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO-WRITEBACK-LIVE — the OWNER-GATED live proof.

Drives the demo arc's Beat 4 over the REAL governed spine (OIDC/PDP/C6
executor/audit) with a REAL ``SalesforceWriteClient`` against the owner's
Salesforce dev tenant, and asserts a REAL, org-minted Note id lands in the
``tool.execute`` audit trail — not the fake ``_FakeCrmClient`` id, and not the
inert demo string.

NE-CS-SEND (owner decision 2026-08-27): ``write.internal.crm.note`` is genuinely
TIER-1 now — autonomous + digest — so Beat 4 for this action is the AUTONOMOUS
pair (:func:`ne.cli.demo.arc._run_autonomous_writeback`): the write executes
with no human gate, and the control step asserts NO approval was ever queued.
The deny→HITL-approve exhibit moved to the Tier-2 ``write.external.email.send``
(proven in packages/ne-api/tests/test_beat3_governed_draft_email_chain.py and
live via the Meridian Customer Support agent flow).

Green-by-skip discipline (the worm-live-proof.yml pattern used everywhere in
this repo for owner-gated live checks): the WHOLE module is skipped, cleanly,
whenever ``NE_DEMO_WRITEBACK_LIVE_PROOF`` is not exactly ``"1"``. Two further
skip points inside the test (Salesforce settings validation, the meridian-seed
manifest) also skip rather than error or fail, naming what is missing but
never a value (C12).

Owner env recipe (see docs/demo/README.md "Live write-back (--live-writeback)"):

    NE_DEMO_WRITEBACK_LIVE_PROOF=1
    NE_SALESFORCE_AUTH_METHOD=jwt_bearer
    NE_SALESFORCE_CLIENT_ID=<your connected app / org value>
    NE_SALESFORCE_USERNAME=<your connected app / org value>
    NE_SALESFORCE_INSTANCE_URL=<your connected app / org value>
    NE_SALESFORCE_PRIVATE_KEY_PEM_PATH=~/ne-owner-secrets/salesforce/ne-sf-key.pem
    NE_SALESFORCE_OWNER=demo-ops
    NE_SALESFORCE_OBJECTS='[{"name": "Account", "content_fields": ["Name"]}]'
    NE_MERIDIAN_SEED_MANIFEST_DIR=~/.ne/meridian-seed   # optional — this is the default

The created Note is a harmless, throwaway dev-org artifact.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

import click
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NE_DEMO_WRITEBACK_LIVE_PROOF") != "1",
    reason=(
        "owner-gated live Salesforce write proof — set NE_DEMO_WRITEBACK_LIVE_PROOF=1 "
        "with NE_SALESFORCE_* exported (see this module's docstring for the exact env "
        "recipe) to run it"
    ),
)


def test_live_crm_note_writeback_via_governed_arc(tmp_path: Path) -> None:
    """Beat 4 (Tier-1 autonomous) over the real spine + a real Salesforce write."""
    import sqlalchemy as sa
    from pydantic import ValidationError

    from ne.api.platform import WriteActionClients
    from ne.audit.store import ne_audit_log
    from ne.cli.demo import live
    from ne.cli.demo.arc import CRM_WRITEBACK_ACTION, StepStatus, _run_writeback_pair
    from ne.cli.demo.corpus import build_scenario
    from ne.connect.salesforce.client import SalesforceClient
    from ne.connect.salesforce.config import SalesforceConnectorSettings
    from ne.connect.salesforce.write import SalesforceWriteClient

    from .demo_fixture_harness import (
        FakeDraftClient,
        FakeTicketClient,
        build_demo_app,
        get_state,
        run,
    )

    try:
        settings = SalesforceConnectorSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False)[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        pytest.skip(f"NE_SALESFORCE_* not fully configured: {loc}: {first.get('msg')}")

    manifest_dir = Path(
        os.environ.get("NE_MERIDIAN_SEED_MANIFEST_DIR", str(Path.home() / ".ne" / "meridian-seed"))
    )
    try:
        record_id = live._live_writeback_target(manifest_dir)
    except click.ClickException as exc:
        pytest.skip(str(exc))

    # The REAL crm leg; ticket/draft are fakes — this proof exercises only Beat 4
    # (the CRM write), never the ticket/draft legs.
    crm_client = SalesforceWriteClient(SalesforceClient(settings), api_version=settings.api_version)
    clients = WriteActionClients(crm=crm_client, ticket=FakeTicketClient(), draft=FakeDraftClient())

    scenario = build_scenario()
    # Minimal boot: T2/T3/delegation/hallucination seams are irrelevant to Beat 4
    # and only add boot cost (the real oxigraph lift, the sub-agent dispatcher).
    h = build_demo_app(
        tmp_path,
        write_action_clients=clients,
        verified_answers=False,
        delegation=False,
    )

    reader = h.bearer(
        scenario.reader_id, groups=scenario.reader_groups, department=scenario.reader_department
    )
    approver = h.bearer("approver-demo", groups=("approvers",), department="operations")

    # uuid suffix: re-runs must be DISTINCT (a repeated request_id would
    # dedup-collapse into 'deduplicated' rather than minting a fresh Note).
    title = f"Neutron demo live-proof note {uuid.uuid4().hex[:8]}"
    args = {
        "record_id": record_id,
        "title": title,
        "body": "Created by the NE-DEMO-WRITEBACK-LIVE owner-gated live proof.",
    }

    async def _drive():
        async with h.client() as client:
            return await _run_writeback_pair(
                client,
                "/api",
                reader=reader,
                approver=approver,
                scenario=scenario,
                writeback_action=CRM_WRITEBACK_ACTION,
                args=args,
                poll_deadline_s=30.0,
            )

    auto, control = run(_drive())
    for engine in get_state(h.app).engines:
        engine.dispose()

    assert auto.status is StepStatus.OK, auto.response_excerpt
    assert control.status is StepStatus.OK, control.response_excerpt
    assert control.evidence.get("pending_after") is False

    audit_engine = sa.create_engine(h.audit_url)
    try:
        with audit_engine.connect() as conn:
            chain = conn.execute(
                sa.select(
                    ne_audit_log.c.seq,
                    ne_audit_log.c.action,
                    ne_audit_log.c.outcome,
                    ne_audit_log.c.actor,
                    ne_audit_log.c.payload,
                ).order_by(ne_audit_log.c.seq)
            ).all()
    finally:
        audit_engine.dispose()

    for row in chain:
        prov = row.payload.get("provenance") or {}
        print(f"CHAIN seq={row.seq} {row.action}[{row.outcome}] actor={row.actor} prov={prov}")

    rows = [r for r in chain if r.action == "tool.execute"]
    targets = [
        json.loads(row.payload.get("detail") or "{}").get("target")
        for row in rows
        if row.outcome == "success"
    ]
    # A real Salesforce id begins "002" for Note; unfakeable — the org minted it.
    real_note_ids = [t for t in targets if t and re.fullmatch(r"002[a-zA-Z0-9]{12,15}", t)]
    assert real_note_ids, f"no real Salesforce Note id (002...) among audit targets: {targets}"
    assert "003000000000001AAA" not in targets  # the CI fixture's fake id, never here

    # -- NE-CS-SEND: the Tier-1 chain carries NO approval records at all --------
    # The autonomous write's honest audit shape is the ABSENCE of a human gate:
    # no approval.requested, no approval.granted, no approval.rejected — and the
    # real org-minted write IS on the chain, audited, with its target id.
    approval_rows = [r for r in chain if r.action.startswith("approval.")]
    assert approval_rows == [], (
        f"a Tier-1 internal write queued approval records: "
        f"{[(r.action, r.outcome) for r in approval_rows]}"
    )
    execute_seq = next(
        r.seq
        for r in chain
        if r.action == "tool.execute"
        and r.outcome == "success"
        and json.loads(r.payload.get("detail") or "{}").get("target") == real_note_ids[0]
    )
    print(
        f"\nLIVE PROOF: created Salesforce Note {real_note_ids[0]!r} "
        f"on Account {record_id!r} via the governed demo arc's Beat 4 "
        f"(Tier-1 autonomous); write audited at seq={execute_seq}."
    )
