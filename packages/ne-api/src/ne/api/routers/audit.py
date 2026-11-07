# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Audit read routes (spec §1.5) — ops-gated, server-computed tamper verdict.

R2 DECISIONS (documented here, rendered verbatim by the console):

* ORDERING — ``ne.audit.list_records`` returns rows ``seq`` DESC (newest-first),
  the ops-console default; the console sends only ``limit`` (no cursor).
* VERIFICATION SCOPE — **never the page.**  The verdict a page carries describes
  the CHAIN, not the 25 rows returned, and the two bullets below say exactly
  which chain state and by which proof.  ``limit`` and ``anchor_seq`` are
  presentation only: they change which rows are returned and NOTHING about the
  verdict.  (The previous wording here claimed the verdict "describes exactly
  the slice of chain the page reports on" while the bullet above it recorded
  that the console sends no window at all — the two contradicted each other,
  and the second one was never true of the unfiltered read.)

  - **FILTERED read (``from``/``to`` supplied)** — ``verify_chain(start, end)``
    over the SAME window as the filter, unchanged and still O(chain) in wall
    time.  ``ne.audit.verify._verify_bounded`` is as sound as the full verify
    for deletion/reorder/gap attacks: it runs a GLOBAL density pre-check and
    anchors both ends of the window, so a break OUTSIDE the window still flips
    ``valid``.  ``checked`` is the window length.
  - **UNFILTERED read (no ``from``/``to`` — what the console actually sends)** —
    the process's ONE :class:`ne.audit.AnchoredChainVerifier`
    (``ApiState.chain_verifier``, shared with ``ModelSelectionService``).  The
    request proves, in O(rows appended since the last full verify), that the
    chain still EXTENDS a head that a real ``verify_chain`` blessed; the O(chain)
    walk itself runs on the background ``run_chain_anchor_refresher`` loop, off
    every request.  It replaces an UNBOUNDED ``verify_chain`` that re-hashed the
    whole ledger to report on one page — **measured 35.44 s at 357,821 rows on
    the live demo**, which is what parked the console on "Loading the audit log".

    ⚠ **WHAT THAT VERDICT CLAIMS, precisely, because it is not the same claim.**
    ``valid`` still covers THE WHOLE CHAIN — it was never narrowed to the page —
    but it is now a COMPOSITE: records ``1..A`` were re-hashed in full by a
    ``verify_chain`` that completed ``anchor_age_s`` seconds ago (≤ 900 s,
    ``ne.audit.CHAIN_ANCHOR_MAX_AGE_S``, ENFORCED — a stale anchor is refused
    and the call pays the full walk inline), and records ``A+1..head`` were
    re-hashed on THIS call.  ``detail`` states that split in words and is served
    verbatim.  The one tamper class this gives up is an UNPROPAGATED edit at
    ``seq <= A`` (a payload changed without recomputing its ``record_hash``, a
    column swapped, a prefix row deleted): it is invisible to the extension
    proof and is caught by the next full ``verify_chain`` — within ~300 s in
    this process, because the refresher loop is itself a caller.  Every other
    class, INCLUDING a self-consistent rewrite at or below the anchor, is caught
    on the request itself.  ``ne.audit.anchor``'s module docstring is the
    authority on that boundary; do not restate it more strongly here.
  - ``checked`` on an unfiltered page is the number of records the verdict
    COVERS (the whole chain), not the number re-hashed on this call — otherwise
    the console's "N records verified" would collapse to the extension length
    and report a 357,821-record ledger as "3 records verified".  The split
    between the two lives in ``detail``.

Both routes are restricted to ``ApiSettings.ops_roles`` (the audit log names
actors/actions platform-wide) — empty ⇒ 403 for everyone (fail closed), audited
``api.audit.unauthorized``. Sync library reads (``list_records``/``verify_chain``/
``replay``) run in ONE threadpool hop so the event loop never blocks.

⚠ ``GET /v1/audit/export`` still runs an unbounded ``verify_chain`` when it is
called with no window (``_export_preamble``) and is NOT changed here: an export
is an operator-initiated download, not a polled page, and its preamble asserts
the verdict over the exact rows it streams.  Named so the remaining cost is
recorded rather than forgotten.  Its BREAKS do propagate — every walk in this
process, this one included, withdraws the shared anchor through
:func:`ne.audit.verify.register_chain_walk_observer`.
"""

from __future__ import annotations

import csv
import functools
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

import anyio.to_thread
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.responses import StreamingResponse

from ne.api.authz import require_role
from ne.api.deps import ApiState, api_state, current_principal
from ne.api.wire import (
    AuditEventsPageView,
    AuditRecordView,
    ChainVerificationView,
    ReplayResultView,
    audit_anchor_view,
    audit_record_view,
    chain_verification_view,
)
from ne.audit import (
    MAX_PAGE_ROWS,
    AnchoredChainVerifier,
    AnchoredVerification,
    AnchorResolution,
    ChainVerification,
    count_records,
    find_approval_decision,
    list_records,
    list_records_anchored,
    replay,
    verify_chain,
)
from ne.audit.store import AuditRecord
from ne.contracts.types import Principal

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]

_RESOURCE = "api:audit"


def _parse_iso(value: str, param: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{param}: invalid ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HTTPException(
            status_code=422,
            detail=f"{param}: timezone offset required; local wall-clock text is ambiguous",
        )
    return parsed.astimezone(UTC)


def _parse_window(from_: str | None, to: str | None) -> tuple[datetime | None, datetime | None]:
    """Parse an unambiguous instant window shared by list and export.

    A naive ISO string has meaning only in an unstated machine/browser zone, so
    accepting it lets the same visible filter select different rows at different
    hops. Normalise aware values to UTC and reject a reversed window explicitly
    instead of returning an apparently-valid empty result.
    """
    start = _parse_iso(from_, "from") if from_ is not None else None
    end = _parse_iso(to, "to") if to is not None else None
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="from: must not be later than to")
    return start, end


def _anchored_verification_view(av: AnchoredVerification) -> ChainVerificationView:
    """Render an :class:`ne.audit.AnchoredVerification` onto the page's wire verdict.

    IT BUILDS THE WIRE VIEW DIRECTLY, and that is the point of the function.
    It used to flatten onto a :class:`~ne.audit.verify.ChainVerification` first
    — the result of a FULL walk — and hand that to
    :func:`~ne.api.wire.chain_verification_view`.  That intermediate has no
    ``basis`` and no ``contradiction`` field to flatten them ONTO, so both were
    dropped on the floor: every consumer downstream saw an anchored verdict and
    a full-walk verdict as the same bytes, and the console's whole verdict
    pipeline was written on the assumption that every verdict is a repeatable
    full walk.  Three defects followed from that one gap.  The flattening step
    is gone.

    Four fields need care and none is a formality:

    ``checked`` — the number of records the verdict COVERS, never the number
    re-hashed on this call.  For an anchored proof that is ``head_seq`` (the
    chain is dense ``1..head_seq``, proven by the blessing full verify over the
    prefix and by the extension proof's own density check over the tail), which
    is exactly what the full walk used to report.  Reporting the extension
    length instead would turn the console's "N records verified" into "0 records
    verified" on a quiet ledger — a true statement about the call, and a
    thoroughly false picture of the ledger.  The split between "verified now"
    and "verified ``anchor_age_s`` ago" is in ``detail``, which is served
    verbatim.

    ``first_break_seq`` / ``first_break_event_id`` / ``reason`` — taken from the
    underlying ``verify_chain`` result (:attr:`AnchoredVerification.chain`)
    whenever a real full walk ran, so a break keeps NAMING its record.  The
    console renders "Break at entry N" from that field; dropping it would
    downgrade every named break to "the server did not name the entry".  An
    anchored proof carries no ``chain`` (it ran no full walk) and deliberately
    names no record: :func:`ne.audit.verified_extension` returns a bare ``None``
    on failure and the verifier's response to that is to fall back to a full
    walk, which is the thing that names breaks — so an invalid anchored-basis
    verdict cannot reach here.

    ``basis`` — copied VERBATIM from the verifier.  It is what tells a consumer
    whether ``checked`` came from a walk of the whole ledger on this request or
    from a prefix blessed up to :data:`~ne.audit.CHAIN_ANCHOR_MAX_AGE_S` ago
    plus the rows appended since, and therefore what a screen is allowed to
    SAY.  It is also an ORDERING: a verdict may not withdraw a standing break
    that a stronger proof confirmed.

    ``contradiction`` — likewise verbatim.  It marks the one-shot refusals
    (``valid`` false with the walk itself passing) that the read-during-append
    race cannot manufacture, so a consumer that suppresses flapping by
    requiring consecutive disagreeing reads can still believe a verdict it will
    only ever see once.  See :attr:`AnchoredVerification.contradiction`.

    ⚠ ``valid`` is taken from :attr:`AnchoredVerification.valid`, NOT from
    ``chain.valid``: the two DISAGREE on the contradicted/unconfirmed paths,
    where the ledger is internally consistent but the verifier still refuses
    because "extends the head I verified" was disproved.  Taking the weaker of
    the two is the fail-closed direction and is the one the verifier computed.
    """
    covered = (
        av.head_seq if av.basis == "anchored-extension" and av.head_seq is not None else av.checked
    )
    named = av.chain
    return ChainVerificationView(
        valid=av.valid,
        basis=av.basis,
        contradiction=av.contradiction,
        checked=covered,
        first_break_seq=named.first_break_seq if named is not None else None,
        first_break_event_id=named.first_break_event_id if named is not None else None,
        reason=named.reason if named is not None else None,
        detail=av.detail,
    )


def _page_verification(
    engine: sa.Engine,
    verifier: AnchoredChainVerifier | None,
    *,
    start: datetime | None,
    end: datetime | None,
    full: bool,
) -> ChainVerificationView:
    """The page's chain verdict — anchored when unfiltered, windowed when not.

    A ``from``/``to`` read keeps ``verify_chain(start, end)`` EXACTLY as before:
    the anchor is a single ``(seq, record_hash)`` at the chain HEAD and proves
    nothing about an arbitrary interior window, so there is no incremental proof
    to give a windowed read.  It is also not the slow path in practice — the
    console's polled page sends no window at all.  That verdict goes out as
    ``basis="windowed"``, which is neither of the other two claims: a row
    tampered outside the window is invisible to it by construction.

    ⭐ **IT IS NO LONGER A DEAD END, AND THAT IS NOT DONE HERE.**  A windowed
    walk can legitimately prove the chain BROKEN — including below the anchor,
    the one class the anchored path structurally cannot see — and this route
    used to return that verdict and tell the shared verifier nothing, so the
    process's trust root stayed blessed and the very next unfiltered poll served
    an ``anchored-extension`` PASS over a chain a walk in this same process had
    just disproved.  The fix is NOT a call added at this line: every invalid
    verdict out of :func:`~ne.audit.verify.verify_chain`, from any caller,
    now withdraws the anchor through
    :func:`ne.audit.verify.register_chain_walk_observer` — see
    :meth:`ne.audit.AnchoredChainVerifier.observe_chain_walk` for why a windowed
    verdict may MAKE that finding while still being far too weak to withdraw
    one.  Nothing on this path has to remember to do it.

    ``full`` is the operator's explicit RE-VERIFICATION SWEEP (``?full_verify``
    — see :func:`audit_events`).  It runs the real O(chain) walk and re-blesses
    the anchor.  It exists because a break is only ever confirmed off a
    full-chain verdict, a console must not let a weaker proof withdraw a
    stronger proof's finding, and an anchored extension proof is a weaker proof
    — so without this the one control an operator has to clear a standing break
    could never produce a verdict strong enough to clear it.

    ``verifier is None`` (no ``chain_verifier`` composed — every ``ApiState``
    the production app builds has one; a test harness may not) falls back to the
    unbounded ``verify_chain``.  Same verdict, same guarantee, today's cost:
    a missing verifier must never mean a weaker answer.
    """
    if start is not None or end is not None:
        # A WINDOW OUTRANKS ``full``: both narrow the walk, and there is no
        # arrangement in which a bounded walk is also a whole-chain proof. The
        # verdict says so on the wire rather than pretending to be one.
        return chain_verification_view(verify_chain(engine, start=start, end=end), basis="windowed")
    if verifier is None:
        return chain_verification_view(verify_chain(engine))
    return _anchored_verification_view(verifier.full_verify() if full else verifier.verify())


def _read_page(
    engine: sa.Engine,
    *,
    verifier: AnchoredChainVerifier | None,
    actor: str | None,
    action: str | None,
    start: datetime | None,
    end: datetime | None,
    limit: int,
    anchor_seq: int | None,
    full_verify: bool,
) -> tuple[tuple[AuditRecord, ...], ChainVerificationView, AnchorResolution | None]:
    """One threadpool hop: the filtered page, its window verification verdict,
    and — when the read is anchored — the server's anchor-reachability verdict.

    An anchored read (``anchor_seq`` set) returns the page CONTAINING that seq
    in the filtered set, not the most recent page; the resolution states found /
    filtered_out / not_found plus the chain head (C7: reachability is decided
    HERE, never inferred client-side). Both reads share the same filter builder
    and the same payload-authoritative row seam; anchoring changes only which
    window of the same rows is returned.
    """
    if anchor_seq is None:
        records = list_records(
            engine, actor=actor, action=action, start=start, end=end, limit=limit
        )
        resolution: AnchorResolution | None = None
    else:
        records, resolution = list_records_anchored(
            engine,
            anchor_seq=anchor_seq,
            actor=actor,
            action=action,
            start=start,
            end=end,
            limit=limit,
        )
    verification = _page_verification(engine, verifier, start=start, end=end, full=full_verify)
    return records, verification, resolution


async def _require_ops(principal: Principal, state: ApiState) -> None:
    await require_role(
        principal,
        state.settings.ops_roles,
        sink=state.sink,
        audit_action="api.audit.unauthorized",
        resource=_RESOURCE,
        http_detail="not authorized",
    )


@router.get("/v1/audit/events")
async def audit_events(
    principal: _Principal,
    state: _State,
    actor: str | None = None,
    action: str | None = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    anchor_seq: Annotated[int | None, Query(ge=1)] = None,
    full_verify: bool = False,
) -> AuditEventsPageView:
    """The ledger page read. ``anchor_seq`` (the ``?seq=`` deep link's fetch)
    returns the page CONTAINING that entry instead of the most recent page, and
    the response's ``anchor`` field carries the server's reachability verdict —
    same ops chokepoint, same limit bound, same read seam as the unanchored
    page; anchoring grants access to nothing time-windowed filtering or export
    could not already reach.

    ``full_verify`` is the console's RE-VERIFY control, and it changes only the
    VERDICT, never which rows come back. The polled read is anchored — O(rows
    appended since the last full walk) — and that is the right default for a
    5-second poll. It is the wrong thing for the one control an operator has to
    withdraw a standing break: a break is only ever confirmed off a full-chain
    walk, a weaker proof must not withdraw a stronger proof's finding, and an
    anchored extension proof IS weaker (it does not re-read the prefix at all).
    So the sweep asks for the real walk and pays the O(chain) cost — which is
    exactly what every page read cost before the anchor existed, on a control a
    human clicks rather than on a timer.

    It grants no data and no authority the default read does not: same ops
    chokepoint, same rows, same projection."""
    await _require_ops(principal, state)
    start, end = _parse_window(from_, to)
    records, verification, resolution = await anyio.to_thread.run_sync(
        functools.partial(
            _read_page,
            state.audit_engine,
            verifier=state.chain_verifier,
            actor=actor,
            action=action,
            start=start,
            end=end,
            limit=limit,
            anchor_seq=anchor_seq,
            full_verify=full_verify,
        )
    )
    return AuditEventsPageView(
        events=[audit_record_view(r) for r in records],
        verification=verification,
        anchor=audit_anchor_view(resolution) if resolution is not None else None,
    )


@router.get("/v1/audit/approval-decisions/{request_id}")
async def approval_decision(
    request_id: str,
    principal: _Principal,
    state: _State,
) -> AuditRecordView:
    """Resolve one approval receipt to its authoritative terminal chain row.

    This is deliberately not a newest-page search: it selects the exact
    hash-covered ``provenance.approval_id`` conjunctively with the gate's two
    terminal action names. A miss is a server-owned 404 fact; the browser never
    converts absence from a capped page into an in-flight write claim.
    """
    await _require_ops(principal, state)
    record = await anyio.to_thread.run_sync(
        functools.partial(find_approval_decision, state.audit_engine, request_id)
    )
    if record is None:
        raise HTTPException(status_code=404, detail="approval decision audit entry not found")
    return audit_record_view(record)


#: The exported CSV's wire columns — every cell derives from an
#: :class:`AuditRecordView` (the SAME projection ``GET /v1/audit/events`` serves,
#: C7/C12 by construction). Order is the on-disk contract a compliance reviewer's
#: spreadsheet macro will pin against; changing it is a wire-breaking change.
_EXPORT_COLUMNS: tuple[str, ...] = (
    "seq",
    "id",
    "event_id",
    "event_type",
    "config_hash",
    "actor",
    "occurred_at",
    "kpi_tag",
    "trace_id",
    "action",
    "resource",
    "outcome",
    "payload",
    "prev_hash",
    "record_hash",
)


#: CSV/Excel formula-injection prefixes (CWE-1236): a cell opening with any of
#: these is live syntax to a spreadsheet (``=``/``+``/``-``/``@`` start a
#: formula, a leading tab/CR can smuggle one past leading-whitespace trimming).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe_cell(value: str) -> str:
    """Neutralise formula injection at RENDER time only.

    A cell beginning with a formula-prefix character is escaped with a leading
    apostrophe, the standard "force text" convention Excel/Sheets/LibreOffice
    all honour — the cell renders as the literal string, never executes. This
    is cosmetic escaping of the CSV BYTES; it does not touch the underlying
    wire value, so an ``event_id``/``actor``/``action`` used for IDENTITY
    matching in tests is unaffected unless the SOURCE value itself starts with
    one of these characters (in which case the escaped literal, apostrophe
    included, is exactly what a reviewer's spreadsheet will show — matching
    what actually got exported).
    """
    if value and value[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def _export_row(view: AuditRecordView) -> list[str]:
    """One CSV data row from an :class:`AuditRecordView` — nothing but a copy.

    Optional text columns render ``None`` as ``""`` (CSV convention); ``payload``
    is the canonical JSON encoding (sorted keys, no whitespace) so a reviewer's
    diff/hash tooling gets a stable byte sequence. Every cell is passed through
    :func:`_csv_safe_cell` (CWE-1236) — audited ``actor``/``resource``/etc. can
    carry caller-supplied text, and this artifact is explicitly built to be
    opened in Excel.
    """

    def _s(value: object | None) -> str:
        return "" if value is None else str(value)

    cells = [
        str(view.seq),
        str(view.id),
        view.event_id,
        view.event_type,
        view.config_hash,
        view.actor,
        view.occurred_at.isoformat(),
        _s(view.kpi_tag),
        _s(view.trace_id),
        _s(view.action),
        _s(view.resource),
        _s(view.outcome),
        json.dumps(view.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        view.prev_hash,
        view.record_hash,
    ]
    return [_csv_safe_cell(cell) for cell in cells]


def _export_preamble(
    engine: sa.Engine,
    *,
    actor: str | None,
    action: str | None,
    start: datetime | None,
    end: datetime | None,
) -> tuple[int, ChainVerification]:
    """One threadpool hop: the filtered TOTAL row count AND the SAME window's
    chain-verification verdict, computed together so both describe exactly the
    filters/window the export streams (the ``_read_page`` pattern, export-shaped).
    """
    total = count_records(engine, actor=actor, action=action, start=start, end=end)
    verification = verify_chain(engine, start=start, end=end)
    return total, verification


def _preamble_lines(
    *,
    actor: str | None,
    action: str | None,
    start: datetime | None,
    end: datetime | None,
    generated_at: datetime,
    total: int,
    rows_exported: int,
    truncated: bool,
    verification: ChainVerification,
) -> list[str]:
    """The self-describing comment preamble (filters + verdict) prepended to the
    export so the CSV is never a naked table a reviewer could misattribute.
    Booleans render lowercase; an absent value renders as ``-`` (never blank —
    blank would be indistinguishable from a truncated line under a text viewer).

    SECURITY: ``actor``/``action`` are attacker-controlled query parameters,
    while a failed verification can name an attacker-tampered stored event id
    in both ``first_break_event_id`` and ``detail``. Every text value is passed
    through ``_f``, which escapes CR/LF to the literal two-character sequences
    ``\\r``/``\\n`` BEFORE interpolation. Neither query input nor the database
    content this artifact exists to examine can therefore forge or terminate a
    preamble line or inject a second ``# verification_valid`` / ``# truncated``
    line above the genuine server-computed one.
    """

    def _f(value: str | None) -> str:
        if value is None:
            return "-"
        return value.replace("\r", "\\r").replace("\n", "\\n")

    def _b(value: bool) -> str:
        return "true" if value else "false"

    return [
        "# neutron-audit-export v1",
        f"# generated_at: {generated_at.isoformat()}",
        f"# filter_actor: {_f(actor)}",
        f"# filter_action: {_f(action)}",
        f"# filter_from: {_f(start.isoformat() if start is not None else None)}",
        f"# filter_to: {_f(end.isoformat() if end is not None else None)}",
        f"# rows_matched: {total}",
        f"# rows_exported: {rows_exported}",
        f"# truncated: {_b(truncated)}",
        f"# verification_valid: {_b(verification.valid)}",
        f"# verification_checked: {verification.checked}",
        (
            "# verification_first_break_seq: "
            + _f(
                str(verification.first_break_seq)
                if verification.first_break_seq is not None
                else None
            )
        ),
        f"# verification_first_break_event_id: {_f(verification.first_break_event_id)}",
        f"# verification_reason: {_f(verification.reason)}",
        f"# verification_detail: {_f(verification.detail)}",
    ]


def _trailer_lines(*, rows_written: int, truncated_confirmed: bool) -> list[str]:
    """The DEFINITIVE completion sentinel, appended AFTER the last data row.

    The preamble's ``rows_matched``/``rows_exported``/``truncated`` are an
    up-front ESTIMATE (``count_records`` runs once, before any row is read —
    see the route docstring's race note). These trailer values are FACTS
    observed by the stream itself:

    * ``rows_written`` — the actual number of rows placed on the wire, counted
      as each batch is yielded (never a prediction).
    * ``truncated_confirmed`` — one extra keyset read, anchored at the ``seq``
      of the LAST row actually exported, taken after the loop finishes. If a
      row still exists past that anchor, the export IS incomplete relative to
      the live table at that instant — regardless of what the up-front count
      said. This closes the count/stream race the up-front estimate cannot:
      a row inserted between the count and the first page read (which can
      silently bump an older matching row out of an uncursored first page)
      is caught here, because the confirmation anchor is the true last row
      SENT, not the row count taken before streaming began.
    * ``export_complete: true`` is written ONLY if the generator reaches this
      point normally. An interrupted stream (an exception, a client
      disconnect, a timeout) never emits it — so a partial file is detectable
      by this line's ABSENCE, never by trusting a value that could be wrong.
    """

    def _b(value: bool) -> str:
        return "true" if value else "false"

    return [
        f"# rows_written: {rows_written}",
        f"# truncated_confirmed: {_b(truncated_confirmed)}",
        "# export_complete: true",
    ]


@router.get("/v1/audit/export")
async def audit_export(
    principal: _Principal,
    state: _State,
    actor: str | None = None,
    action: str | None = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> StreamingResponse:
    """A governed, streamed CSV export of the SAME window ``GET /v1/audit/events``
    filters describe — never more, never a silent fewer.

    GOVERNANCE (C6): identical chokepoint to every other audit read — ``await
    _require_ops(...)`` runs FIRST, before any chain access; an unentitled key
    gets the identical 403 the ledger read gets and the attempt is audited
    ``api.audit.unauthorized`` via :func:`require_role`. There is no bespoke
    authorization path and no side-door read: only ``list_records`` /
    ``count_records`` / ``verify_chain`` (all payload-authoritative, C6) touch
    the chain.

    PROJECTION (C7/C12): every CSV cell is built from :func:`audit_record_view`
    — byte-for-byte the SAME wire view ``GET /v1/audit/events`` serves. The file
    is assembled 100% server-side; there is no client-supplied trimming.

    ORDERING: rows stream ``seq`` DESC, matching the ledger API's page order. The
    keyset cursor (``before_seq``, STRICT ``<``) anchors the whole stream at the
    FIRST page's head, so the export is an internally-consistent snapshot even
    though later pages are read moments after the first — a row appended
    mid-stream (with a higher seq than the anchor) is simply never reached.
    ``rows_matched`` is computed once, up front, at request time: under
    concurrent writes it can therefore skew slightly from what the loop below
    ultimately streams — the CSV DATA ROWS are always the authority a reviewer
    should trust over the preamble's up-front estimate. That estimate is backed
    by a definitive TRAILER (see :func:`_trailer_lines`), appended after the
    last data row: ``rows_written`` (the real count placed on the wire) and
    ``truncated_confirmed`` (one extra keyset read anchored at the last row
    actually sent, closing the count/stream race the preamble cannot) are
    FACTS, not forecasts, and ``export_complete: true`` only appears if the
    generator finished normally — its ABSENCE is how a reviewer detects a
    partial file, rather than trusting a value that could be silently wrong.

    BOUNDED, NEVER SILENTLY TRUNCATED: the CSV ROW STREAM reads in
    ``audit_export_batch_rows``-sized keyset batches (bounded memory — the full
    result set is never materialised) up to ``audit_export_max_rows`` rows total.
    Exceeding that cap is NEVER silent: the preamble's ``truncated``/
    ``rows_matched``/``rows_exported`` lines, the trailer's
    ``truncated_confirmed``, AND the ``X-Neutron-Export-Truncated`` response
    header all say so.

    VERDICT COST IS SEPARATE FROM ROW-STREAM COST: the embedded chain verdict
    (``verify_chain`` over the SAME ``from``/``to`` window) has its own,
    different cost shape. When a window is supplied, the verify walk is bounded
    to that window's row range (``ne.audit.verify._verify_bounded``). An export
    with NO ``from``/``to`` runs the FULL, unbounded chain verify
    (``_verify_full``), which materialises every row up to the head to compute
    the verdict — this is independent of, and NOT bounded by,
    ``audit_export_batch_rows``/``audit_export_max_rows`` above. A reviewer
    exporting an unwindowed, multi-million-row ledger pays that full-walk cost
    on every such request.

    READ-ONLY (C10): the success path performs ZERO chain writes — exactly the
    ``GET /v1/audit/events`` discipline (only an UNAUTHORIZED attempt is
    audited).
    """
    await _require_ops(principal, state)
    start, end = _parse_window(from_, to)

    settings = state.settings
    engine = state.audit_engine
    max_rows = settings.audit_export_max_rows
    effective_cap = min(limit, max_rows) if limit else max_rows
    batch_rows = settings.audit_export_batch_rows

    generated_at = datetime.now(UTC)
    total, verification = await anyio.to_thread.run_sync(
        functools.partial(
            _export_preamble,
            engine,
            actor=actor,
            action=action,
            start=start,
            end=end,
        )
    )
    truncated = total > effective_cap
    rows_exported = min(total, effective_cap)

    async def _stream() -> AsyncIterator[str]:
        preamble = _preamble_lines(
            actor=actor,
            action=action,
            start=start,
            end=end,
            generated_at=generated_at,
            total=total,
            rows_exported=rows_exported,
            truncated=truncated,
            verification=verification,
        )
        header_buf = io.StringIO()
        csv.writer(header_buf, lineterminator="\r\n").writerow(_EXPORT_COLUMNS)
        yield "\r\n".join(preamble) + "\r\n" + header_buf.getvalue()

        remaining = effective_cap
        written = 0
        before: int | None = None
        while remaining > 0:
            # Clamped against list_records' OWN ceiling, derived not restated.
            # Without this an oversized batch_rows comes back short on the first
            # page, `len(page) < want` reads as end-of-results, and the stream
            # stops early while the preamble still claims completeness.
            want = min(batch_rows, remaining, MAX_PAGE_ROWS)
            page = await anyio.to_thread.run_sync(
                functools.partial(
                    list_records,
                    engine,
                    actor=actor,
                    action=action,
                    start=start,
                    end=end,
                    limit=want,
                    before_seq=before,
                )
            )
            if not page:
                break
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\r\n")
            for record in page:
                writer.writerow(_export_row(audit_record_view(record)))
            yield buf.getvalue()
            written += len(page)
            remaining -= len(page)
            before = page[-1].seq
            if len(page) < want:
                break

        # DEFINITIVE truncation check (see _trailer_lines): one more keyset
        # read anchored at the LAST row actually exported. Existence of a
        # further matching row past that anchor means the export IS
        # incomplete relative to the table's live state — this is checked
        # AFTER every row has been sent, so it is a fact about what was
        # actually written, not a guess taken before streaming began.
        more = await anyio.to_thread.run_sync(
            functools.partial(
                list_records,
                engine,
                actor=actor,
                action=action,
                start=start,
                end=end,
                limit=1,
                before_seq=before,
            )
        )
        trailer = _trailer_lines(rows_written=written, truncated_confirmed=bool(more))
        yield "\r\n".join(trailer) + "\r\n"

    filename = f"neutron-audit-export-{generated_at.strftime('%Y%m%dT%H%M%SZ')}.csv"
    return StreamingResponse(
        _stream(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Neutron-Export-Truncated": "true" if truncated else "false",
            "X-Neutron-Export-Rows": str(rows_exported),
        },
    )


@router.get("/v1/audit/replay")
async def audit_replay(
    principal: _Principal,
    state: _State,
    trace_id: str | None = None,
    event_id: str | None = None,
) -> ReplayResultView:
    await _require_ops(principal, state)
    if trace_id is None and event_id is None:
        raise HTTPException(
            status_code=422,
            detail="replay requires at least one selector: trace_id or event_id",
        )
    result = await anyio.to_thread.run_sync(
        functools.partial(replay, state.audit_engine, trace_id=trace_id, event_id=event_id)
    )
    return ReplayResultView(
        events=[audit_record_view(r) for r in result.events],
        config_hashes=list(result.config_hashes),
        context_ids=list(result.context_ids),
    )
