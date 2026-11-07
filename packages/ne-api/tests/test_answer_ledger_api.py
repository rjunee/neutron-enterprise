# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from ne.api.answer_ledger import AnswerLedgerService
from ne.brain.answer_ledger import SqlAlchemyAnswerLedgerStore
from ne.contracts.models import Completion, Usage
from ne.contracts.types import Chunk, Principal

from .conftest import AppBuilder, RaisingGateway, bind_current_document_source, get_state


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _chunk(owner: str = "user:ada") -> Chunk:
    return Chunk(
        chunk_id="chunk-quarterly",
        document_id="doc-quarterly",
        text="Operating margin improved to 18 percent after support automation.",
        metadata={"record_id": "Quarterly operating review", "connector": "wiki"},
        acl=(owner,),
    )


def _seed_lineage(harness: Any, chunk: Chunk, *, upsert: bool = True) -> Chunk:
    lineage = get_state(harness.app).document_lineage
    prepared = lineage.prepare(chunk)
    lineage.finalize(
        prepared,
        audit_event_id=f"seed-audit-event:{chunk.chunk_id}",
        occurred_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    stamp = bind_current_document_source(
        harness.app,
        document_id=chunk.document_id,
        labels=chunk.acl,
        content=chunk.text.encode(),
    )
    current = chunk.model_copy(
        update={"metadata": {**chunk.metadata, "ne_source_authority_stamp": stamp.model_dump()}}
    )
    if upsert:
        _run(harness.chunk_store.upsert_chunks((current,)))
    return current


async def _request(app: Any, method: str, path: str, headers: dict[str, str], **kwargs: Any):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ne.test"
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


def test_pin_reask_persists_exact_provenance_and_isolates_owner(make_app: AppBuilder) -> None:
    served = _chunk()
    completion = Completion(
        text="Margin improved after support automation.",
        model_id="meridian-private-35b",
        usage=Usage(prompt_tokens=31, completion_tokens=7),
    )
    harness = make_app(chunks=[served], completions=[completion])
    served = _seed_lineage(harness, served)
    ada = harness.bearer("user:ada")
    grace = harness.bearer("user:grace")

    pinned = _run(
        _request(
            harness.app,
            "POST",
            "/api/v1/answer-ledger/pin",
            ada,
            json={"question": "What changed in operating performance?"},
        )
    )
    assert pinned.status_code == 200
    assert pinned.json()["question"]["owner_id"] == "user:ada"
    assert pinned.json()["question"]["reask_schedule"] is None

    answered = _run(_request(harness.app, "POST", "/api/v1/answer-ledger/reask", ada))
    assert answered.status_code == 200
    body = answered.json()
    event = body["events"][0]
    assert event["answer"] == completion.text
    assert event["served_model_id"] == completion.model_id
    assert event["config_hash"] == get_state(harness.app).config_hash
    assert event["classification"] is None
    assert event["trace_id"]
    assert event["trace_href"] == f"/trace/{event['trace_id']}"
    assert event["corpus_stamp"] == [
        {
            "chunk_id": served.chunk_id,
            "document_id": served.document_id,
            "content_hash": hashlib.sha256(served.text.encode()).hexdigest(),
            "document_version": 1,
            "document_href": "/library?tab=documents&document=doc-quarterly&version=1",
            "document_title": "Quarterly operating review",
        }
    ]
    request = harness.gateway.requests[0]
    assert request.trace_id == event["trace_id"]
    assert request.principal_id == "user:ada"
    assert served.text in request.messages[1].content

    hidden = _run(_request(harness.app, "GET", "/api/v1/answer-ledger", grace))
    assert hidden.status_code == 200
    assert hidden.json() == {"question": None, "events": []}


def test_superseded_answer_is_withheld_before_historical_title_projection(
    make_app: AppBuilder,
) -> None:
    served = _chunk(owner="finance")
    harness = make_app(
        chunks=[served],
        completions=[
            Completion(
                text="A governed answer.",
                model_id="private-model",
                usage=Usage(prompt_tokens=1, completion_tokens=1),
            )
        ],
    )
    served = _seed_lineage(harness, served)
    allowed = harness.bearer("user:ada", groups=["finance"])
    revoked = harness.bearer("user:ada", groups=[])
    assert (
        _run(
            _request(
                harness.app,
                "POST",
                "/api/v1/answer-ledger/pin",
                allowed,
                json={"question": "What changed?"},
            )
        ).status_code
        == 200
    )
    answered = _run(_request(harness.app, "POST", "/api/v1/answer-ledger/reask", allowed))
    assert answered.json()["events"][0]["corpus_stamp"][0]["document_title"] == (
        "Quarterly operating review"
    )

    current = Chunk(
        chunk_id="chunk-quarterly-current",
        document_id=served.document_id,
        text="A newer version visible directly to Ada.",
        metadata={"record_id": "Current operating review", "connector": "wiki"},
        acl=("user:ada",),
    )
    current = _seed_lineage(harness, current)
    current_journey = get_state(harness.app).document_lineage.journey(
        served.document_id,
        ("user:ada",),
    )
    assert current_journey is not None
    assert current_journey.selected.version_seq == 2
    assert current_journey.selected.display_name == "Current operating review"

    after_revocation = _run(_request(harness.app, "GET", "/api/v1/answer-ledger", revoked))
    assert after_revocation.status_code == 200
    event = after_revocation.json()["events"][0]
    assert event["disclosure_status"] == "content_withheld"
    assert set(event) == {"disclosure_status", "event_id", "occurred_at"}
    assert "A governed answer." not in after_revocation.text


def test_schedule_is_explicit_principal_owned_and_round_trips_authoritative_state(
    make_app: AppBuilder,
) -> None:
    harness = make_app(chunks=[])
    ada = harness.bearer("user:ada")
    grace = harness.bearer("user:grace")
    assert (
        _run(
            _request(
                harness.app,
                "POST",
                "/api/v1/answer-ledger/pin",
                ada,
                json={"question": "What changed?"},
            )
        ).status_code
        == 200
    )

    enabled = _run(
        _request(
            harness.app,
            "PUT",
            "/api/v1/answer-ledger/schedule",
            ada,
            json={"schedule": "hourly"},
        )
    )
    assert enabled.status_code == 200
    assert enabled.json()["question"]["reask_schedule"] == "hourly"
    assert _run(_request(harness.app, "GET", "/api/v1/answer-ledger", grace)).json() == {
        "question": None,
        "events": [],
    }

    disabled = _run(
        _request(
            harness.app,
            "PUT",
            "/api/v1/answer-ledger/schedule",
            ada,
            json={"schedule": None},
        )
    )
    assert disabled.status_code == 200
    assert disabled.json()["question"]["reask_schedule"] is None


def test_schedule_rejects_missing_question_and_unknown_cadence(make_app: AppBuilder) -> None:
    harness = make_app(chunks=[])
    ada = harness.bearer("user:ada")
    missing = _run(
        _request(
            harness.app,
            "PUT",
            "/api/v1/answer-ledger/schedule",
            ada,
            json={"schedule": "daily"},
        )
    )
    assert missing.status_code == 404

    invalid = _run(
        _request(
            harness.app,
            "PUT",
            "/api/v1/answer-ledger/schedule",
            ada,
            json={"schedule": "monthly"},
        )
    )
    assert invalid.status_code == 422


def test_schedule_restores_exact_prior_state_when_audit_append_fails(make_app: AppBuilder) -> None:
    harness = make_app(chunks=[])
    state = get_state(harness.app)
    store = SqlAlchemyAnswerLedgerStore(state.audit_engine)
    store.pin("user:ada", "What changed?")

    class _RaisingSink:
        async def append(self, event: Any) -> None:
            raise RuntimeError("audit unavailable")

        async def verify(
            self, *, start: datetime | None = None, end: datetime | None = None
        ) -> Any:
            raise AssertionError("schedule rollback must not verify the audit chain")

    service = AnswerLedgerService(
        store=store,
        retriever=state.retriever,
        lineage=state.document_lineage,
        gateway=state.gateway,
        sink=_RaisingSink(),
        expander=state.expander,
        runtime_settings=state.runtime_settings,
        config_hash=state.config_hash,
        top_k=state.settings.retrieval_top_k,
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        _run(service.schedule(Principal(id="user:ada"), "hourly"))

    restored = store.question("user:ada")
    assert restored is not None
    assert restored.reask_schedule is None
    assert restored.reask_scheduled_at is None


def test_second_answer_does_not_infer_drift_across_a_withheld_predecessor(
    make_app: AppBuilder,
) -> None:
    """A withheld predecessor breaks the drift chain — never inferred across it.

    Supersession alone no longer withholds a historical event (temporal
    provenance: the ledger's whole point is showing pre-supersession answers).
    The withheld predecessor here is manufactured the way it really happens: the
    first answer rested on a version whose HISTORICAL ACL required a group the
    reader has since lost; the new current version admits them directly, so the
    second re-ask succeeds while the first event is withheld from them.
    """

    served = _chunk(owner="finance")
    harness = make_app(
        chunks=[served],
        completions=[
            Completion(
                text="Before",
                model_id="model-a",
                usage=Usage(prompt_tokens=1, completion_tokens=1),
            ),
            Completion(
                text="After",
                model_id="model-b",
                usage=Usage(prompt_tokens=1, completion_tokens=1),
            ),
        ],
    )
    served = _seed_lineage(harness, served)
    with_finance = harness.bearer("user:ada", groups=["finance"])
    without_finance = harness.bearer("user:ada", groups=[])
    assert (
        _run(
            _request(
                harness.app,
                "POST",
                "/api/v1/answer-ledger/pin",
                with_finance,
                json={"question": "What changed?"},
            )
        ).status_code
        == 200
    )
    assert (
        _run(_request(harness.app, "POST", "/api/v1/answer-ledger/reask", with_finance)).status_code
        == 200
    )

    store = SqlAlchemyAnswerLedgerStore(get_state(harness.app).audit_engine)
    first = store.timeline("user:ada")[0]
    # Controlled fixture mutation at the storage seam: it makes BOTH causal
    # inputs differ, proving corpus precedence and the independent config flag.
    with store.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE ne_answer_ledger_events SET config_hash = ? WHERE event_id = ?",
            ("prior-config", first.event_id),
        )
    updated = served.model_copy(
        update={"text": served.text + " New source sentence.", "acl": ("user:ada",)}
    )
    updated = _seed_lineage(harness, updated)
    _run(harness.chunk_store.upsert_chunks([updated]))

    response = _run(_request(harness.app, "POST", "/api/v1/answer-ledger/reask", without_finance))
    assert response.status_code == 200
    second = response.json()["events"][1]
    assert response.json()["events"][0]["disclosure_status"] == "content_withheld"
    assert second["classification"] is None
    assert second["root_cause"] is None


def test_superseded_predecessor_stays_visible_and_drift_classifies_for_authorized_reader(
    make_app: AppBuilder,
) -> None:
    """Temporal provenance: both answers, each bound to its own document version.

    A reader whose access never changed re-asks after the source document was
    edited and re-synced.  The pre-supersession event must STAY readable (it is
    the exhibit for "how was the historical answer derived"), the new event must
    classify drift with ``corpus_changed`` and a versioned diff link, and the
    two events must stamp two different document versions.
    """

    served = _chunk()
    harness = make_app(
        chunks=[served],
        completions=[
            Completion(
                text="Before",
                model_id="model-a",
                usage=Usage(prompt_tokens=1, completion_tokens=1),
            ),
            Completion(
                text="After",
                model_id="model-b",
                usage=Usage(prompt_tokens=1, completion_tokens=1),
            ),
        ],
    )
    served = _seed_lineage(harness, served)
    ada = harness.bearer("user:ada")
    assert (
        _run(
            _request(
                harness.app,
                "POST",
                "/api/v1/answer-ledger/pin",
                ada,
                json={"question": "What changed?"},
            )
        ).status_code
        == 200
    )
    assert (
        _run(_request(harness.app, "POST", "/api/v1/answer-ledger/reask", ada)).status_code == 200
    )

    updated = served.model_copy(update={"text": served.text + " New source sentence."})
    updated = _seed_lineage(harness, updated)
    _run(harness.chunk_store.upsert_chunks([updated]))

    response = _run(_request(harness.app, "POST", "/api/v1/answer-ledger/reask", ada))
    assert response.status_code == 200
    events = response.json()["events"]
    assert [event["disclosure_status"] for event in events] == ["available", "available"]
    first, second = events
    assert first["answer"] == "Before"
    assert second["answer"] == "After"
    assert first["corpus_stamp"][0]["document_version"] == 1
    assert second["corpus_stamp"][0]["document_version"] == 2
    assert second["root_cause"] is not None
    assert second["root_cause"]["answer_changed"] is True
    assert second["root_cause"]["corpus_changed"] is True
    assert second["root_cause"]["corpus_diff_hrefs"] == [
        "/library?tab=documents&document=doc-quarterly&version=2"
    ]


def test_no_partial_event_when_evidence_is_unavailable(make_app: AppBuilder) -> None:
    no_evidence = make_app(chunks=[])
    token = no_evidence.bearer("user:ada")
    assert (
        _run(
            _request(
                no_evidence.app,
                "POST",
                "/api/v1/answer-ledger/pin",
                token,
                json={"question": "What changed?"},
            )
        ).status_code
        == 200
    )
    refused = _run(_request(no_evidence.app, "POST", "/api/v1/answer-ledger/reask", token))
    assert refused.status_code == 409
    assert (
        _run(_request(no_evidence.app, "GET", "/api/v1/answer-ledger", token)).json()["events"]
        == []
    )


def test_no_partial_event_when_lineage_is_unavailable(make_app: AppBuilder) -> None:
    served = _chunk()
    no_lineage = make_app(chunks=[served])
    token = no_lineage.bearer("user:ada")
    assert (
        _run(
            _request(
                no_lineage.app,
                "POST",
                "/api/v1/answer-ledger/pin",
                token,
                json={"question": "What changed?"},
            )
        ).status_code
        == 200
    )
    refused = _run(_request(no_lineage.app, "POST", "/api/v1/answer-ledger/reask", token))
    assert refused.status_code == 409
    assert no_lineage.gateway.requests == []
    assert (
        SqlAlchemyAnswerLedgerStore(get_state(no_lineage.app).audit_engine).timeline("user:ada")
        == ()
    )


def test_no_partial_event_when_retrieved_bytes_race_a_newer_lineage_version(
    make_app: AppBuilder,
) -> None:
    old = _chunk()
    harness = make_app(chunks=[old])
    newer = old.model_copy(update={"text": old.text + " New version."})
    newer = _seed_lineage(harness, newer, upsert=False)
    token = harness.bearer("user:ada")
    assert (
        _run(
            _request(
                harness.app,
                "POST",
                "/api/v1/answer-ledger/pin",
                token,
                json={"question": "What changed?"},
            )
        ).status_code
        == 200
    )

    refused = _run(_request(harness.app, "POST", "/api/v1/answer-ledger/reask", token))
    assert refused.status_code == 409
    assert harness.gateway.requests == []
    assert (
        SqlAlchemyAnswerLedgerStore(get_state(harness.app).audit_engine).timeline("user:ada") == ()
    )


def test_no_partial_event_when_gateway_fails(make_app: AppBuilder) -> None:
    served = _chunk()
    harness = make_app(chunks=[served], gateway=RaisingGateway())
    served = _seed_lineage(harness, served)
    token = harness.bearer("user:ada")
    assert (
        _run(
            _request(
                harness.app,
                "POST",
                "/api/v1/answer-ledger/pin",
                token,
                json={"question": "What changed?"},
            )
        ).status_code
        == 200
    )

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=harness.app, raise_app_exceptions=False),
            base_url="http://ne.test",
        ) as client:
            return await client.post("/api/v1/answer-ledger/reask", headers=token)

    assert _run(call()).status_code == 500
    assert (
        SqlAlchemyAnswerLedgerStore(get_state(harness.app).audit_engine).timeline("user:ada") == ()
    )


def test_unstamped_ledger_event_projects_only_the_retryable_safe_shell(
    make_app: AppBuilder,
) -> None:
    served = _chunk()
    harness = make_app(
        chunks=[served],
        completions=[
            Completion(
                text="private answer",
                model_id="private-model",
                usage=Usage(prompt_tokens=1, completion_tokens=1),
            )
        ],
    )
    served = _seed_lineage(harness, served)
    token = harness.bearer("user:ada")
    assert (
        _run(
            _request(
                harness.app,
                "POST",
                "/api/v1/answer-ledger/pin",
                token,
                json={"question": "What changed?"},
            )
        ).status_code
        == 200
    )
    response = _run(_request(harness.app, "POST", "/api/v1/answer-ledger/reask", token))
    assert response.status_code == 200
    store = SqlAlchemyAnswerLedgerStore(get_state(harness.app).audit_engine)
    stored = store.timeline("user:ada")[0]
    contributor = stored.corpus_stamp[0]
    with store.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE ne_answer_ledger_events SET corpus_stamp = ? WHERE event_id = ?",
            (
                json.dumps(
                    [
                        {
                            "chunk_id": contributor.chunk_id,
                            "document_id": contributor.document_id,
                            "content_hash": contributor.content_hash,
                            "document_version": contributor.document_version,
                        }
                    ]
                ),
                stored.event_id,
            ),
        )
    response = _run(_request(harness.app, "GET", "/api/v1/answer-ledger", token))
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    event = response.json()["events"][0]
    assert event["disclosure_status"] == "retryable_unknown"
    assert set(event) == {"disclosure_status", "event_id", "occurred_at"}
    assert "private answer" not in response.text
