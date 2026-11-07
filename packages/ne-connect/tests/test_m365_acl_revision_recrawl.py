# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Independent evidence: what RELEASES an ACL-quarantined driveItem (NE-044).

The claim under test, end to end over the real pipeline (recorded Graph ->
``M365GraphConnector`` -> ``SyncRunner`` -> ``KnowledgeWriter`` -> Qdrant
``:memory:`` + real sqlite audit):

    When the M365 connector's mapper revision changes, a driveItem that was
    previously QUARANTINED (because the ACL mapper resolved zero principals for
    it) is re-crawled and its ACL is re-emitted with the correct principals —
    WITHOUT the item's own bytes having changed.

The concrete production incident this reconstructs: claims-encoded Entra groups
(``c:0t.c|tenant|<guid>``) arrived as a ``siteGroup`` with no ``id`` and were
dropped as "no usable AAD id", so the item resolved to ZERO principals and the
connector fail-closed quarantined it (no Record, no AclEntry, never
world-readable). ``e9ecb807`` corrected the mapper. It did NOT bump
``_SOURCE_LINK_MAPPER_VERSION``, and it touched no state.

Three runs against ONE state store and ONE unchanged driveItem fixture:

1. the PRE-FIX mapper                       -> the item is quarantined;
2. the CORRECTED mapper, revision UNCHANGED -> the item is STILL quarantined
   (the ANTI-VACUITY CONTROL, and the exact situation production is in);
3. the CORRECTED mapper, revision CHANGED   -> the item is re-crawled and its
   ACL re-emitted with the right principal, off byte-identical source metadata.

Run 2 is the load-bearing one: without it, run 3 proves nothing, because a
corrected mapper alone would explain the release just as well as the revision
bump does. Run 2 also pins the MECHANISM — the item is never even re-read,
because an incremental delta does not carry an item whose bytes did not change.

The revision is varied by monkeypatching ``ACL_MAPPER_REVISION`` in the CONTRACT
module's namespace — the member the fix under test adds to the mapper-revision
digest, read there at call time by ``m365_source_link_mapper_revision``. That
keeps the contract identity and the sync's revision consistent with each other
(they are derived from the same function) and, deliberately, binds this file to
the production change: revert the ``"acl_mapper_revision"`` member and the file
fails twice over — the digest stops moving, so run 3 never re-crawls, AND
``monkeypatch.setattr`` raises ``AttributeError`` on the attribute that is no
longer there. Patching ``_SOURCE_LINK_MAPPER_VERSION`` instead would also vary
the revision, but it was ALREADY a digest member before this change, so the file
would pass with the fix reverted — evidence of nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping, Sequence

import httpx
import pytest
import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.brain.parsed_evidence import PARSED_RECORD_METADATA_KEY
from ne.connect import ConnectorStateStore
from ne.connect.m365 import M365SyncOutcome, run_m365_sync
from ne.connect.m365 import connector as m365_connector
from ne.connect.m365 import contract as m365_contract
from ne.connect.m365.acl import ACL_MAPPER_REVISION, AclOutcome, map_permissions

from .conftest import WriterStack
from .m365_transport import (
    DELTA_LINK_1,
    DELTA_LINK_2,
    RecordedBodyParser,
    build_m365_transport,
    m365_settings,
)

_AclMapper = Callable[[Sequence[Mapping[str, object]]], AclOutcome]

_SOURCE = "m365-sharepoint"
_DRIVE = "drive-1"
#: The fixture driveItem this file drives. Its recorded permission collection is
#: replaced (payload only — the connector's own mapper still runs) with the
#: claims-encoded group grant the production incident was about; its driveItem
#: bytes in ``delta_page1/2.json`` are untouched and identical across every run.
_ITEM = "item-q-unresolvable"
_SOURCE_ID = f"{_DRIVE}:{_ITEM}"

_GROUP_GUID = "44444444-4444-4444-4444-444444444444"
_CLAIMS_LOGIN = f"c:0t.c|tenant|{_GROUP_GUID}"
_CLAIMS_PREFIX = "c:0t.c|tenant|"

_HOSTS = ("contoso.sharepoint.com",)

#: The exact provider bytes both mappers are asked to map.
_CLAIMS_PERMISSIONS: dict[str, object] = {
    "value": [
        {
            "id": "perm-claims-1",
            "roles": ["read"],
            "grantedToV2": {
                "siteGroup": {
                    "loginName": _CLAIMS_LOGIN,
                    "displayName": "Enterprise Risk",
                }
            },
        }
    ]
}


def _claims_guids(perms: Sequence[Mapping[str, object]]) -> set[str]:
    """Every claims-encoded object id the payload carries, read directly."""
    guids: set[str] = set()
    for perm in perms:
        granted = perm.get("grantedToV2")
        if not isinstance(granted, Mapping):
            continue
        for kind in ("siteGroup", "siteUser"):
            ident = granted.get(kind)
            if isinstance(ident, Mapping):
                login = str(ident.get("loginName") or "").lower()
                if login.startswith(_CLAIMS_PREFIX):
                    guids.add(login[len(_CLAIMS_PREFIX) :])
    return guids


def _pre_fix_map_permissions(perms: Sequence[Mapping[str, object]]) -> AclOutcome:
    """The ACL mapper as it behaved BEFORE ``e9ecb807``.

    Reconstructed as the exact DELTA the fix introduced — a claims-encoded
    ``siteGroup`` / ``siteUser`` login resolved to nothing and was dropped as
    having no usable AAD id — rather than as a second hand-written mapper, so
    every other branch stays byte-for-byte the production one.
    """
    outcome = map_permissions(perms)
    guids = _claims_guids(perms)
    return AclOutcome(
        principals=tuple(p for p in outcome.principals if p not in guids),
        dropped=outcome.dropped
        + tuple(f"siteGroup {guid!r}: no usable AAD id" for guid in sorted(guids)),
        tainted=outcome.tainted,
    )


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


def _quarantine_details(stack: WriterStack) -> list[str]:
    """The ``detail`` of every ``connector.acl_quarantine`` denied event, in order."""
    with stack.engine.begin() as conn:
        rows = [
            dict(r)
            for r in conn.execute(sa.select(ne_audit_log).order_by(ne_audit_log.c.id)).mappings()
        ]
    details: list[str] = []
    for row in rows:
        if row["action"] != "connector.acl_quarantine":
            continue
        assert row["outcome"] == "denied"
        payload = row["payload"]
        assert isinstance(payload, dict)
        details.append(str(payload["detail"]))
    return details


def test_the_pre_fix_and_corrected_mappers_really_differ_on_the_same_bytes() -> None:
    """Anti-vacuity floor: the two mappers disagree about THIS payload.

    Without this, run 2 of the scenario below is unfalsifiable — an item that
    stayed quarantined under a "corrected" mapper that in fact corrects nothing
    would look exactly the same.
    """
    perms = _CLAIMS_PERMISSIONS["value"]
    assert isinstance(perms, list)

    pre_fix = _pre_fix_map_permissions(perms)
    assert pre_fix.principals == ()  # zero principals -> the caller quarantines
    assert not pre_fix.tainted
    assert any("no usable AAD id" in reason for reason in pre_fix.dropped)

    corrected = map_permissions(perms)
    assert corrected.principals == (_GROUP_GUID,)
    assert not corrected.tainted


def test_only_a_mapper_revision_bump_releases_an_acl_quarantined_item(
    writer_stack: WriterStack, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()
    requests: list[httpx.Request] = []
    mapper: dict[str, _AclMapper] = {"impl": _pre_fix_map_permissions}

    def _dispatch(perms: Sequence[Mapping[str, object]]) -> AclOutcome:
        return mapper["impl"](perms)

    # The crawl's ONLY mapping seam. Patching it (not the acl module) leaves the
    # real ``map_permissions`` importable for the corrected run below.
    monkeypatch.setattr(m365_connector, "map_permissions", _dispatch)

    async def _run() -> M365SyncOutcome:
        return await run_m365_sync(
            m365_settings(source_link_allowed_hosts=_HOSTS),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_m365_transport(
                recorder=requests,
                permission_payload_overrides={_ITEM: _CLAIMS_PERMISSIONS},
            ),
            parser=RecordedBodyParser(),
        )

    def _permission_reads(item: str, since: int) -> int:
        suffix = f"/items/{item}/permissions"
        return sum(1 for request in requests[since:] if str(request.url).endswith(suffix))

    async def scenario() -> None:
        # -- run 1: the PRE-FIX mapper resolves zero principals -> quarantine ---
        mark = len(requests)
        first = await _run()
        assert first.mode == "backfill"
        assert _permission_reads(_ITEM, mark) == 1  # the item WAS read, and rejected
        assert first.acl_quarantined == 2  # this item + the fixture's forged one
        assert first.report.written == 5
        assert await stack.store.get([_item_id(_SOURCE_ID, _ITEM)]) == []
        details = _quarantine_details(stack)
        assert len(details) == 1
        assert _SOURCE_ID in details[0]

        quarantined_revision = state_store.load_mapper_revision(_SOURCE)
        assert quarantined_revision is not None
        assert state_store.load_state(_SOURCE) == {f"delta:{_DRIVE}": DELTA_LINK_1}

        # -- run 2: the CORRECTED mapper, revision UNCHANGED (THE CONTROL) -----
        # This is exactly where production is standing right now: e9ecb807 is
        # merged, nothing bumped the revision, and the quarantined items have
        # not moved.
        mapper["impl"] = map_permissions
        mark = len(requests)
        control = await _run()

        assert control.mode == "incremental"  # the delta cursor survived
        assert state_store.load_mapper_revision(_SOURCE) == quarantined_revision
        # The run really did work with the corrected mapper — it is not a no-op
        # that trivially changes nothing.
        assert control.report.written == 1
        assert _permission_reads("item-user", mark) == 1
        # ...but the quarantined item was NEVER RE-READ. Its bytes did not
        # change, so the incremental delta does not carry it, so no corrected
        # mapping is ever computed for it. THIS is why the fix alone released
        # nothing.
        # NOTE: this assertion is FIXTURE-GUARANTEED, not a measured
        # discrimination. ``item-q-unresolvable`` appears only in
        # ``delta_page2.json``; run 2 reads ``delta_incremental.json``, whose
        # ``value`` holds only ``item-user`` and a tombstone. So it cannot fail
        # for any mapper-related reason — it is a faithful model of "an
        # incremental delta does not carry an unchanged item", and nothing more.
        # The genuinely load-bearing anti-vacuity check is
        # ``test_the_pre_fix_and_corrected_mappers_really_differ_on_the_same_bytes``.
        assert _permission_reads(_ITEM, mark) == 0
        assert control.acl_quarantined == 0  # nothing new quarantined, nothing released
        assert await stack.store.get([_item_id(_SOURCE_ID, _ITEM)]) == []  # STILL quarantined
        assert _quarantine_details(stack) == details  # no second quarantine event
        assert state_store.load_state(_SOURCE) == {f"delta:{_DRIVE}": DELTA_LINK_2}

        # -- run 3: the CORRECTED mapper AND a CHANGED revision ---------------
        monkeypatch.setattr(
            m365_contract,
            "ACL_MAPPER_REVISION",
            ACL_MAPPER_REVISION + 1,
        )
        mark = len(requests)
        released = await _run()

        assert released.mode == "backfill"  # the revision bump dropped the delta state
        bumped_revision = state_store.load_mapper_revision(_SOURCE)
        assert bumped_revision is not None and bumped_revision != quarantined_revision
        # the item is re-crawled: its permission collection is read again...
        assert _permission_reads(_ITEM, mark) == 1
        # ...and its ACL is now EMITTED, with the principal the corrected mapper
        # resolves out of the claims-encoded login.
        [chunk] = await stack.store.get([_item_id(_SOURCE_ID, _ITEM)])
        assert chunk.acl == (_GROUP_GUID,)
        assert released.acl_quarantined == 1  # only the genuinely forged item is left
        assert _SOURCE_ID not in _quarantine_details(stack)[-1]

        # ...off UNCHANGED item bytes: the stored record carries the same eTag
        # and lastModifiedDateTime the quarantining run 1 already saw in
        # ``delta_page2.json``. Nothing about the item changed — only the mapping.
        payload = chunk.metadata[PARSED_RECORD_METADATA_KEY]
        assert payload["etag"] == '"etag-unres-v1"'
        assert payload["last_modified"] == "2026-06-06T12:00:00Z"
        assert payload["size"] == 4096

    asyncio.run(scenario())
