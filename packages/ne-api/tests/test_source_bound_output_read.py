# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# ONE ANSWER, ONE READ -- WHICHEVER TABLE THE FINALIZER CHOSE.
#
# `run_finalizer._protect_output` picks the storage table on one condition:
#
#     ne_agent_protected_source_outputs if deliveries else ne_agent_protected_turn_contents
#
# Until this unit, each console surface read exactly one of them.  The Output
# pane read only `protected_turn_content`; the Evidence drawer's `resolve_output`
# bailed on anything that was not `protected_source_output`.  Disjoint by
# construction -- so an IMPLEMENTATION DETAIL, which table the bytes landed in,
# decided which screen a reader had to go and find their answer on.  Perversely,
# the better the answer -- one that actually read source records and produced
# citations -- the more hidden it was, because it landed in a drawer that looks
# like a debugging panel.
#
# THE STORAGE SPLIT IS LEGITIMATE AND IS NOT TOUCHED.  Source-derived bytes
# carry `SourceAuthorityStamp`s and must be re-checked against CURRENT source
# authority before disclosure; source-free bytes have no stamps and nothing to
# re-check.  What is unified is the READ, not the authorization: this reader
# performs the exact `resolve_output` call the Evidence drawer performs, with
# the same principal/context/attempt triple and the same served-hash equality
# check.  These tests pin that it cannot be shortened.

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

from ne.api.evidence_projection import (
    MaterializedDisclosure,
    PersistedRunEvidence,
    SourceBoundRunOutputReader,
)
from ne.api.governance_projection import PersistedAuditReceipt
from ne.contracts.types import Principal

NOW = datetime(2026, 8, 27, tzinfo=UTC)
H = "a" * 64
ANSWER = b"Meridian holds 4 subjects, per the 2026-06 portfolio record."
PRINCIPAL = Principal(id="owner-1", groups=(), claims={"department": "claims"})


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _run(**overrides: object) -> PersistedRunEvidence:
    fields: dict[str, object] = {
        "run_id": "run-1",
        "owner_principal_id": "owner-1",
        "agent_id": "agent-1",
        "agent_version": 3,
        "status": "completed",
        "config_hash": H,
        "capability_snapshot_hash": "b" * 64,
        "output_verdict": "grounded",
        "mapping_state": "complete",
        "protected_output_kind": "protected_source_output",
        "protected_output_ref": "output-1",
        "protected_output_hash": _hash(ANSWER),
        "event_ids": frozenset({"event-terminal"}),
        "delivery_receipt_ids": frozenset({"delivery-a"}),
        "evidence": (),
        "claims": (),
        "audit_receipts": (
            PersistedAuditReceipt(
                run_audit_receipt_id="audit-receipt-1",
                run_id="run-1",
                run_event_id="event-terminal",
                attempt_id="attempt-1",
                decision_kind="terminal",
                step_id=None,
                tool_call_id=None,
                approval_id=None,
                audit_event_id="audit-event-1",
                audit_sequence=7,
                audit_record_hash="f" * 64,
                config_hash=H,
                created_at=NOW,
            ),
        ),
    }
    fields.update(overrides)
    return PersistedRunEvidence(**fields)  # type: ignore[arg-type]


class _Reader:
    def __init__(self, run: PersistedRunEvidence | None) -> None:
        self.run = run
        self.calls: list[tuple[str, str, str]] = []

    async def load_exact(
        self, *, agent_id: str, run_id: str, owner_principal_id: str
    ) -> PersistedRunEvidence | None:
        self.calls.append((agent_id, run_id, owner_principal_id))
        return self.run


class _Materializer:
    """Records the authorization triple it was handed, and answers as told."""

    def __init__(self, output: MaterializedDisclosure) -> None:
        self.output = output
        self.calls: list[tuple[str, str, str, str]] = []

    async def resolve_locator(self, evidence: object, **kwargs: object):
        raise AssertionError("the single-Run answer read must not resolve locators")

    async def resolve_output(self, run: PersistedRunEvidence, **kwargs: object):
        self.calls.append(
            (
                run.run_id,
                str(kwargs["principal_id"]),
                str(kwargs["access_context_hash"]),
                str(kwargs["projection_attempt_id"]),
            )
        )
        return self.output

    async def resolve_claim(self, run: object, claim: object, **kwargs: object):
        raise AssertionError("the single-Run answer read must not resolve claims")


def _reader(
    run: PersistedRunEvidence | None,
    output: MaterializedDisclosure,
    *,
    visible: bool = True,
) -> tuple[SourceBoundRunOutputReader, _Reader, _Materializer]:
    projection_reader = _Reader(run)
    materializer = _Materializer(output)
    return (
        SourceBoundRunOutputReader(
            visible_agent=lambda agent_id, principal: visible,
            reader=projection_reader,
            materializer=materializer,
        ),
        projection_reader,
        materializer,
    )


def _disclose(reader: SourceBoundRunOutputReader):  # type: ignore[no-untyped-def]
    return asyncio.run(
        reader.disclose(
            agent_id="agent-1",
            run_id="run-1",
            principal=PRINCIPAL,
            access_context_hash="c" * 64,
            projection_attempt_id="attempt-uuid",
        )
    )


def test_a_source_derived_answer_is_disclosed_inline_after_reauthorization() -> None:
    """The headline: the good answer stops being the hidden one.

    And the authorization it went through is asserted, not assumed -- the exact
    principal, context hash and projection attempt id are the ones the caller
    supplied, because a reauthorization against a substituted context is not a
    reauthorization at all.
    """

    reader, projection_reader, materializer = _reader(
        _run(), MaterializedDisclosure("authorized", ANSWER)
    )
    disclosed = _disclose(reader)

    assert disclosed.state == "available"
    assert disclosed.text == ANSWER.decode()
    assert projection_reader.calls == [("agent-1", "run-1", "owner-1")]
    assert materializer.calls == [("run-1", "owner-1", "c" * 64, "attempt-uuid")]


def test_a_withheld_source_derived_answer_says_withheld_and_never_blank() -> None:
    """THE PAIRED HALF, and the reason `state` exists at all.

    `output_preview: str | None` collapses "produced nothing" and "you may no
    longer read what was produced" into one blank rectangle.  They are different
    facts: the second one is the governance system working.  A refusal -- of any
    kind -- is not an empty answer and must never render as one.
    """

    reader, _, _ = _reader(_run(), MaterializedDisclosure("withheld"))
    disclosed = _disclose(reader)

    assert disclosed.state == "withheld"
    assert disclosed.text is None


def test_an_unknown_authority_decision_fails_closed() -> None:
    """A retryable-unknown source authority is not a licence to disclose."""

    reader, _, _ = _reader(_run(), MaterializedDisclosure("unknown"))
    assert _disclose(reader).state == "unavailable"


def test_bytes_that_do_not_match_the_served_hash_are_never_disclosed() -> None:
    """The integrity check the Evidence drawer performs, performed here too.

    Without it the unified read would trust whatever the resolver handed back,
    which is exactly the shortcut that turns one read surface into one weaker
    read surface.
    """

    reader, _, _ = _reader(_run(), MaterializedDisclosure("authorized", b"different bytes"))
    assert _disclose(reader).state == "unavailable"


def test_a_source_free_terminal_is_left_to_the_projection() -> None:
    """This reader owns the source-BOUND store only.

    A `protected_turn_content` terminal has no stamps, so routing it through
    here would claim a source reauthorization that did not happen, and would
    double-read bytes the projection already served.
    """

    reader, _, materializer = _reader(
        _run(protected_output_kind="protected_turn_content"),
        MaterializedDisclosure("authorized", ANSWER),
    )
    assert _disclose(reader).state == "unavailable"
    assert materializer.calls == []


def test_a_hidden_agent_is_not_confirmed_by_a_known_run_id() -> None:
    """Visibility first, exactly as the evidence projection orders it."""

    reader, projection_reader, _ = _reader(
        _run(), MaterializedDisclosure("authorized", ANSWER), visible=False
    )
    assert _disclose(reader).state == "unavailable"
    assert projection_reader.calls == []


def test_a_run_the_owner_join_does_not_return_discloses_nothing() -> None:
    """Ownership is the reader's, not this class's, and it is still enforced."""

    reader, _, materializer = _reader(None, MaterializedDisclosure("authorized", ANSWER))
    assert _disclose(reader).state == "unavailable"
    assert materializer.calls == []


# ---------------------------------------------------------------------------
# TWO SURFACES, ONE TRUTH.
#
# Measured live on run f06530da, completed with verdict=verified, read by its
# owner:
#
#     GET /runs/{id}            -> output_state = "unavailable"
#     GET /runs/{id}/evidence   -> "Answer withheld — access changed"
#
# Same run, same reader, same `resolve_output` call, two different answers. The
# disagreement WAS the diagnosis: `SourceBoundRunOutputReader` reads the
# materializer's three decisions as three values and returns "unavailable" for
# `unknown`, while `EvidenceProjector` keyed its withhold on where the bytes
# were STORED rather than on what the materializer decided, and so reported an
# access decision that had never been made.
#
# These pin both surfaces to the same rule, decision by decision. Anything that
# makes them disagree again fails here.


def _project(output: MaterializedDisclosure) -> str:
    """The evidence projection's `output.state` for one materializer answer."""

    from ne.api.evidence_projection import EvidenceProjector

    projector = EvidenceProjector(
        visible_agent=lambda agent_id, principal: True,
        reader=_Reader(_run()),
        materializer=_Materializer(output),
    )
    envelope = asyncio.run(
        projector.project(
            agent_id="agent-1",
            run_id="run-1",
            principal=PRINCIPAL,
            access_context_hash="c" * 64,
            projection_attempt_id="attempt-uuid",
        )
    )
    return envelope.output.state


def test_an_unresolved_answer_is_UNAVAILABLE_on_both_surfaces_never_withheld() -> None:
    """`unknown` is a resolution FAILURE, and neither surface may call it a decision.

    This is the bug that cost a day: the projection said "access changed" about
    a reader whose access had not changed, for every grounded answer on the
    deployment, because the resolver could not resolve any of them.
    """

    reader, _, _ = _reader(_run(), MaterializedDisclosure("unknown"))
    assert _disclose(reader).state == "unavailable"
    assert _project(MaterializedDisclosure("unknown")) == "unavailable"


def test_a_real_withhold_is_WITHHELD_on_both_surfaces() -> None:
    """And the fix does not mute a genuine refusal, which is the other failure."""

    reader, _, _ = _reader(_run(), MaterializedDisclosure("withheld"))
    assert _disclose(reader).state == "withheld"
    assert _project(MaterializedDisclosure("withheld")) == "withheld"


def test_an_authorized_answer_is_AVAILABLE_on_both_surfaces() -> None:
    reader, _, _ = _reader(_run(), MaterializedDisclosure("authorized", ANSWER))
    assert _disclose(reader).state == "available"
    assert _project(MaterializedDisclosure("authorized", ANSWER)) == "available"
