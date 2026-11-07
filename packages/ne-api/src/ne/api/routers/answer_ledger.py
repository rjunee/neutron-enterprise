# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Principal-owned Answer Ledger HTTP surface (BD-2)."""

from __future__ import annotations

import functools
from typing import Annotated, Literal
from urllib.parse import urlencode

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ne.api.answer_ledger import AnswerLedgerEvidenceUnavailableError, AnswerLedgerService
from ne.api.deps import (
    ApiState,
    api_state,
    bind_authenticated_request_context,
    current_principal,
)
from ne.brain.answer_ledger import (
    AnswerEvent,
    AnswerQuestion,
    AnswerQuestionAlreadyPinnedError,
    AnswerQuestionNotFoundError,
    CorpusChunkStamp,
    ReaskSchedule,
    SqlAlchemyAnswerLedgerStore,
    classify_drift,
)
from ne.brain.current_source_filter import HistoricalSourceDisclosureEvaluator
from ne.brain.trimming import acl_match_labels
from ne.contracts.source_authority import SourceAuthorityDecision
from ne.contracts.types import Principal
from ne.runtime.source_results import (
    MissingAuthenticatedContextError,
    require_authenticated_context,
)

router = APIRouter(dependencies=[Depends(bind_authenticated_request_context)])

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class PinQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2_000)


class ReaskScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule: ReaskSchedule | None


class CorpusChunkView(_View):
    chunk_id: str
    document_id: str
    content_hash: str
    document_version: int
    document_href: str
    document_title: str | None


class RootCauseView(_View):
    answer_changed: bool
    corpus_changed: bool
    config_changed: bool
    corpus_diff_hrefs: list[str]
    config_diff_href: str | None


class AnswerEventView(_View):
    disclosure_status: Literal["available"] = "available"
    event_id: str
    occurred_at: str
    answer: str
    corpus_stamp: list[CorpusChunkView]
    config_hash: str
    served_model_id: str
    trace_id: str
    trace_href: str
    classification: str | None
    root_cause: RootCauseView | None


class WithheldAnswerEventView(_View):
    disclosure_status: Literal["content_withheld", "retryable_unknown"]
    event_id: str
    occurred_at: str


class PinnedQuestionView(_View):
    question_id: str
    owner_id: str
    question: str
    created_at: str
    reask_schedule: ReaskSchedule | None = None


class AnswerLedgerView(_View):
    question: PinnedQuestionView | None
    events: list[AnswerEventView | WithheldAnswerEventView]


def _service(state: ApiState) -> AnswerLedgerService:
    return AnswerLedgerService(
        store=SqlAlchemyAnswerLedgerStore(state.audit_engine),
        retriever=state.current_retriever,
        lineage=state.document_lineage,
        gateway=state.gateway,
        sink=state.sink,
        expander=state.expander,
        runtime_settings=state.runtime_settings,
        config_hash=state.config_hash,
        top_k=state.settings.retrieval_top_k,
    )


def _question_view(question: AnswerQuestion) -> PinnedQuestionView:
    return PinnedQuestionView(
        question_id=question.question_id,
        owner_id=question.owner_id,
        question=question.question,
        created_at=question.created_at.isoformat(),
        reask_schedule=question.reask_schedule,
    )


def _document_href(item: CorpusChunkStamp) -> str:
    return "/library?" + urlencode(
        {
            "tab": "documents",
            "document": item.document_id,
            "version": item.document_version,
        }
    )


def _event_view(
    event: AnswerEvent,
    previous: AnswerEvent | None,
    document_titles: dict[tuple[str, int], str],
) -> AnswerEventView:
    classification = classify_drift(previous, event) if previous is not None else None
    corpus_changed = previous is not None and previous.corpus_stamp != event.corpus_stamp
    config_changed = previous is not None and previous.config_hash != event.config_hash
    root = None
    if previous is not None:
        previous_docs = {item.document_id: item for item in previous.corpus_stamp}
        current_docs = {item.document_id: item for item in event.corpus_stamp}
        changed_docs = sorted(
            document_id
            for document_id in set(previous_docs) | set(current_docs)
            if previous_docs.get(document_id) != current_docs.get(document_id)
        )
        links = [
            _document_href(current_docs.get(document_id) or previous_docs[document_id])
            for document_id in changed_docs
        ]
        config_href = None
        if config_changed:
            config_href = "/posture?" + urlencode(
                {"from": previous.config_hash, "to": event.config_hash}
            )
        root = RootCauseView(
            answer_changed=previous.answer != event.answer,
            corpus_changed=corpus_changed,
            config_changed=config_changed,
            corpus_diff_hrefs=links,
            config_diff_href=config_href,
        )
    return AnswerEventView(
        event_id=event.event_id,
        occurred_at=event.occurred_at.isoformat(),
        answer=event.answer,
        corpus_stamp=[
            CorpusChunkView(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                content_hash=item.content_hash,
                document_version=item.document_version,
                document_href=_document_href(item),
                document_title=document_titles.get((item.document_id, item.document_version)),
            )
            for item in event.corpus_stamp
        ],
        config_hash=event.config_hash,
        served_model_id=event.served_model_id,
        trace_id=event.trace_id,
        trace_href=f"/trace/{event.trace_id}",
        classification=classification,
        root_cause=root,
    )


def _authorized_document_titles(
    events: tuple[AnswerEvent, ...], principal: Principal, state: ApiState
) -> dict[tuple[str, int], str]:
    """Resolve historical titles through the same ACL-trimmed lineage seam as Library."""
    labels = acl_match_labels(principal, state.expander)
    titles: dict[tuple[str, int], str] = {}
    checked: set[tuple[str, int]] = set()
    for event in events:
        for item in event.corpus_stamp:
            key = (item.document_id, item.document_version)
            if key in checked:
                continue
            checked.add(key)
            journey = state.document_lineage.journey(
                item.document_id,
                labels,
                version_seq=item.document_version,
            )
            if journey is not None and journey.selected.display_name.strip():
                titles[key] = journey.selected.display_name.strip()
    return titles


async def _ledger(principal: Principal, state: ApiState) -> AnswerLedgerView:
    store = SqlAlchemyAnswerLedgerStore(state.audit_engine)
    question = await anyio.to_thread.run_sync(store.question, principal.id)
    if question is None:
        return AnswerLedgerView(question=None, events=[])
    events = await anyio.to_thread.run_sync(store.timeline, principal.id)
    # The timeline is a HISTORICAL read: every event before the newest is
    # expected to rest on superseded corpus versions — that drift is the whole
    # point of the ledger.  Evaluating retained stamps against the CURRENT
    # admitted truth would withhold every pre-supersession event (and reset the
    # drift chain, so the newest event misreports itself as a baseline).  The
    # historical evaluator keeps withholding fabricated stamps, removed
    # sources, and principals outside the CURRENT ACL snapshot.
    evaluator: object | None = state.source_authority_evaluator
    if state.source_authority_reader is not None:
        evaluator = HistoricalSourceDisclosureEvaluator(
            state.source_authority_reader,
            state.document_lineage.engine,
            group_expander=state.expander,
        )
    try:
        context_hash = require_authenticated_context().context_hash
    except MissingAuthenticatedContextError:
        context_hash = None
    disclosures: list[Literal["available", "content_withheld", "retryable_unknown"]] = []
    for event in events:
        if evaluator is None or context_hash is None or not event.corpus_stamp:
            disclosures.append("retryable_unknown")
            continue
        disposition: Literal["available", "content_withheld", "retryable_unknown"] = "available"
        for contributor in event.corpus_stamp:
            if contributor.authority_stamp is None:
                disposition = "retryable_unknown"
                break
            try:
                decision = await evaluator.evaluate(
                    contributor.authority_stamp,
                    principal=principal,
                    access_context_hash=context_hash,
                )
            except Exception:
                disposition = "retryable_unknown"
                break
            if decision.decision is SourceAuthorityDecision.PERMANENT_WITHHELD:
                disposition = "content_withheld"
                break
            if decision.decision is not SourceAuthorityDecision.AUTHORIZED:
                disposition = "retryable_unknown"
                break
        disclosures.append(disposition)
    authorized_events = tuple(
        event
        for event, disclosure in zip(events, disclosures, strict=True)
        if disclosure == "available"
    )
    document_titles = await anyio.to_thread.run_sync(
        functools.partial(_authorized_document_titles, authorized_events, principal, state)
    )
    views: list[AnswerEventView | WithheldAnswerEventView] = []
    previous: AnswerEvent | None = None
    for event, disclosure in zip(events, disclosures, strict=True):
        if disclosure != "available":
            views.append(
                WithheldAnswerEventView(
                    disclosure_status=disclosure,
                    event_id=event.event_id,
                    occurred_at=event.occurred_at.isoformat(),
                )
            )
            previous = None
        else:
            views.append(_event_view(event, previous, document_titles))
            previous = event
    return AnswerLedgerView(question=_question_view(question), events=views)


@router.get("/v1/answer-ledger")
async def answer_ledger(principal: _Principal, state: _State) -> AnswerLedgerView:
    return await _ledger(principal, state)


@router.post("/v1/answer-ledger/pin")
async def pin_question(
    body: PinQuestionRequest, principal: _Principal, state: _State
) -> AnswerLedgerView:
    try:
        await _service(state).pin(principal, body.question)
    except AnswerQuestionAlreadyPinnedError as exc:
        raise HTTPException(status_code=409, detail="one question is already pinned") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _ledger(principal, state)


@router.post("/v1/answer-ledger/reask")
async def reask(principal: _Principal, state: _State) -> AnswerLedgerView:
    store = SqlAlchemyAnswerLedgerStore(state.audit_engine)
    question = await anyio.to_thread.run_sync(store.question, principal.id)
    if question is None:
        raise HTTPException(status_code=404, detail="no question is pinned")
    try:
        await _service(state).reask(principal, question)
    except AnswerLedgerEvidenceUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _ledger(principal, state)


@router.put("/v1/answer-ledger/schedule")
async def schedule_reask(
    body: ReaskScheduleRequest, principal: _Principal, state: _State
) -> AnswerLedgerView:
    try:
        await _service(state).schedule(principal, body.schedule)
    except AnswerQuestionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="no question is pinned") from exc
    return await _ledger(principal, state)
