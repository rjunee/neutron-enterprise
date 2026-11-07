# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ShareService — the ONLY tuple writer: validate -> known -> read -> PDP -> audit."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence

import pytest

from ne.contracts.governance import DecisionEffect
from ne.contracts.types import Principal
from ne.permissions_rebac.config import RebacSettings
from ne.permissions_rebac.errors import (
    BackendError,
    RebacAuditError,
    ShareNotPermittedError,
    ShareRequiresApprovalError,
    ShareValidationError,
    TupleAbsentError,
)
from ne.permissions_rebac.ports import Tuple3
from ne.permissions_rebac.share import ShareService
from ne.permissions_rebac.testing.fakes import (
    FailingAuditSink,
    FakeKnownPrincipals,
    FakeReadProbe,
    InMemoryOpenFga,
    RecordingAuditSink,
    StaticPdp,
)

SHARER = Principal(id="u-sharer")
DOC = "d1"
VIEWER_TUPLE = ("user:jane", "viewer", "document:d1")
OWNER_TUPLE = ("user:u-sharer", "owner", "document:d1")


def _settings() -> RebacSettings:
    return RebacSettings(enabled=True, store_id="s1", authorization_model_id="m1")


def _service(
    *,
    client: InMemoryOpenFga | None = None,
    pdp: StaticPdp | None = None,
    sink: object | None = None,
    readable: set[tuple[str, str]] | None = None,
    known: set[str] | None = None,
) -> tuple[ShareService, InMemoryOpenFga, StaticPdp, RecordingAuditSink]:
    client = client or InMemoryOpenFga()
    pdp = pdp or StaticPdp(DecisionEffect.ALLOW)
    sink = sink if sink is not None else RecordingAuditSink()
    svc = ShareService(
        client,
        FakeKnownPrincipals(known if known is not None else {"jane", "proj-x"}),
        FakeReadProbe(readable if readable is not None else {("u-sharer", "d1")}),
        pdp,
        sink,  # type: ignore[arg-type]
        settings=_settings(),
    )
    return svc, client, pdp, sink  # type: ignore[return-value]


# --- T-6 validation negatives BEFORE any client/pdp call -------------------


@pytest.mark.parametrize(
    ("document_id", "subject", "relation"),
    [
        ("d1", "acl:tenant-all", "viewer"),
        ("d1", "group:acl:public-link", "viewer"),
        ("d1", "user:acl:x", "viewer"),
        ("acl:d", "user:jane", "viewer"),
        ("", "user:jane", "viewer"),
        ("d1", "user:", "viewer"),
        ("d1", "user:jane", "owner"),  # relation != viewer
        ("d1", "user:jane#member", "viewer"),  # caller-supplied '#'
        ("d1", "jane", "viewer"),  # no type prefix
    ],
)
def test_validation_negatives_reject_before_side_effects(
    document_id: str, subject: str, relation: str
) -> None:
    svc, client, pdp, sink = _service()
    with pytest.raises(ShareValidationError):
        asyncio.run(svc.share(document_id, subject, relation, sharer=SHARER))
    assert pdp.requests == []
    assert client.tuples == set()
    assert sink.events == []


def test_unknown_subject_rejected_before_pdp() -> None:
    svc, client, pdp, _ = _service(known=set())  # jane not known
    with pytest.raises(ShareValidationError):
        asyncio.run(svc.share(DOC, "user:jane", sharer=SHARER))
    assert pdp.requests == []
    assert client.tuples == set()


# --- T-10 share-requires-read + PDP gate -----------------------------------


def test_read_probe_false_blocks_before_pdp() -> None:
    svc, client, pdp, _ = _service(readable=set())  # sharer cannot read d1
    with pytest.raises(ShareNotPermittedError):
        asyncio.run(svc.share(DOC, "user:jane", sharer=SHARER))
    assert pdp.requests == []  # T-10: pdp.decide NOT called on read failure
    assert client.tuples == set()


def test_pdp_deny_writes_no_tuple() -> None:
    svc, client, _pdp, sink = _service(pdp=StaticPdp(DecisionEffect.DENY))
    with pytest.raises(ShareNotPermittedError):
        asyncio.run(svc.share(DOC, "user:jane", sharer=SHARER))
    assert client.tuples == set()
    assert sink.events == []


def test_pdp_require_approval_writes_no_tuple() -> None:
    svc, client, _pdp, sink = _service(pdp=StaticPdp(DecisionEffect.REQUIRE_APPROVAL))
    with pytest.raises(ShareRequiresApprovalError):
        asyncio.run(svc.share(DOC, "user:jane", sharer=SHARER))
    assert client.tuples == set()
    assert sink.events == []


# --- T-9 audit completeness ------------------------------------------------


def test_failing_audit_sink_blocks_tuple() -> None:
    svc, client, _, _ = _service(sink=FailingAuditSink())
    with pytest.raises(RebacAuditError):
        asyncio.run(svc.share(DOC, "user:jane", sharer=SHARER))
    assert client.tuples == set()


def test_successful_share_audits_and_writes_exact_tuples() -> None:
    svc, client, pdp, sink = _service()
    asyncio.run(svc.share(DOC, "user:jane", sharer=SHARER))
    # T-2 exactness: exactly the viewer + owner tuples, nothing else.
    assert client.tuples == {VIEWER_TUPLE, OWNER_TUPLE}
    assert pdp.requests[0].action == "rebac.share"
    assert pdp.requests[0].resource == "document:d1"
    # exactly one rebac.share.created audit event with correct provenance.
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.action == "rebac.share.created"
    assert event.resource == "document:d1"
    assert event.actor == "u-sharer"
    assert event.provenance["subject"] == "user:jane"
    assert event.provenance["relation"] == "viewer"
    assert event.provenance["policy_version"] == "fake-1"


def test_group_share_writes_member_tuple() -> None:
    svc, client, _, _ = _service(readable={("u-sharer", "d1")})
    asyncio.run(svc.share(DOC, "group:proj-x", sharer=SHARER))
    assert ("group:proj-x#member", "viewer", "document:d1") in client.tuples


def test_no_second_owner_tuple_when_already_owner() -> None:
    client = InMemoryOpenFga(tuples={OWNER_TUPLE})
    svc, client, _, _ = _service(client=client)
    asyncio.run(svc.share(DOC, "user:jane", sharer=SHARER))
    assert client.tuples == {OWNER_TUPLE, VIEWER_TUPLE}  # owner not duplicated


# --- revoke ----------------------------------------------------------------


def test_revoke_is_pdp_gated_audited_before_delete() -> None:
    client = InMemoryOpenFga(tuples={VIEWER_TUPLE, OWNER_TUPLE})
    svc, client, pdp, sink = _service(client=client)
    asyncio.run(svc.revoke(DOC, "user:jane", actor=SHARER))
    assert pdp.requests[0].action == "rebac.revoke"
    assert pdp.requests[0].context["actor_is_owner"] is True
    assert sink.events[0].action == "rebac.share.revoked"
    assert VIEWER_TUPLE not in client.tuples  # deleted
    assert OWNER_TUPLE in client.tuples  # revoke targets the subject only


def test_revoke_idempotent_on_missing_tuple() -> None:
    svc, client, pdp, sink = _service()  # no tuples
    asyncio.run(svc.revoke(DOC, "user:jane", actor=SHARER))  # no error
    assert client.tuples == set()
    # The fake genuinely raises TupleAbsentError here (NE-REBAC-REVOKE-
    # IDEMPOTENT) — this exercises the SAME catch path production exercises,
    # not a fake that quietly no-ops before revoke's catch ever runs.
    assert pdp.requests[0].action == "rebac.revoke"
    assert sink.events[0].action == "rebac.share.revoked"


def test_revoke_propagates_non_absent_backend_errors() -> None:
    class _DeleteExplodes(InMemoryOpenFga):
        async def delete_tuples(self, tuples: Sequence[Tuple3]) -> None:
            self._guard()
            raise BackendError("openfga delete_tuples failed")

    client = _DeleteExplodes(tuples={VIEWER_TUPLE, OWNER_TUPLE})
    svc, client, _, _ = _service(client=client)
    with pytest.raises(BackendError) as excinfo:
        asyncio.run(svc.revoke(DOC, "user:jane", actor=SHARER))
    # The narrow-catch proof: a non-absent BackendError propagates as itself,
    # NOT as (or swallowed by) TupleAbsentError.
    assert not isinstance(excinfo.value, TupleAbsentError)


def test_revoke_deny_blocks() -> None:
    client = InMemoryOpenFga(tuples={VIEWER_TUPLE})
    svc, client, _, _ = _service(client=client, pdp=StaticPdp(DecisionEffect.DENY))
    with pytest.raises(ShareNotPermittedError):
        asyncio.run(svc.revoke(DOC, "user:jane", actor=SHARER))
    assert VIEWER_TUPLE in client.tuples  # not deleted


# --- list_shares -----------------------------------------------------------


def test_list_shares_by_document_and_by_principal() -> None:
    client = InMemoryOpenFga(
        tuples={VIEWER_TUPLE, OWNER_TUPLE, ("user:jane", "viewer", "document:d2")}
    )
    svc, *_ = _service(client=client)
    by_doc = asyncio.run(svc.list_shares(document_id="d1"))
    assert set(by_doc) == {VIEWER_TUPLE, OWNER_TUPLE}
    by_user = asyncio.run(svc.list_shares(principal_id="jane"))
    assert VIEWER_TUPLE in by_user


def test_list_shares_requires_exactly_one_selector() -> None:
    svc, *_ = _service()
    with pytest.raises(ShareValidationError):
        asyncio.run(svc.list_shares())
    with pytest.raises(ShareValidationError):
        asyncio.run(svc.list_shares(document_id="d1", principal_id="jane"))


# --- T-9 structural: no sink-less / pdp-less constructor -------------------


def test_constructor_has_no_defaults_for_governance_deps() -> None:
    params = inspect.signature(ShareService.__init__).parameters
    for name in ("client", "known", "read_probe", "pdp", "sink"):
        assert params[name].default is inspect.Parameter.empty
