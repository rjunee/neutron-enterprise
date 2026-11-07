# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import sqlalchemy as sa

from ne.contracts.types import Chunk, RetrievalResult, ScoredChunk
from ne.eval.config import EvalSettings
from ne.eval.fabrication import FabricationCandidate, FactClaim
from ne.eval.store import EvalRunStore
from ne.eval.thresholds import ThresholdSpec
from ne.worker.eval_runner import persist_fabrication_twin, run_route_a_once

if TYPE_CHECKING:
    from ne.api.platform import Platform


class _Expander:
    def expand(self, principal_id: str) -> set[str]:
        return {principal_id, "group:served"}


class _Read:
    def __init__(self) -> None:
        self.principals = []

    async def search(self, query, *, principal, top_k):
        self.principals.append(principal)
        chunk = Chunk(
            chunk_id="chunk-issued",
            document_id="doc-issued",
            text="A real served row.",
            acl=("group:served",),
        )
        return RetrievalResult(chunks=(ScoredChunk(chunk=chunk, score=1.0),), citations=())


def test_worker_route_a_writes_measured_row_with_composed_hash_and_current_principal(
    tmp_path,
) -> None:
    golden = tmp_path / "golden.jsonl"
    golden.write_text(
        json.dumps(
            {
                "id": "route-a",
                "question": "What row is served?",
                "reference_answer": "A real served row.",
                "relevant_document_ids": ["doc-issued"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    database = tmp_path / "eval.db"
    url = f"sqlite+pysqlite:///{database}"
    read = _Read()
    platform = cast(
        "Platform",
        SimpleNamespace(
            config_hash="composed-boot-hash",
            expander=_Expander(),
            retriever=read,
        ),
    )
    settings = EvalSettings(
        metrics=["recall_at_10"],
        thresholds=[ThresholdSpec(metric="recall_at_10", threshold=1.0)],
    )

    result = asyncio.run(
        run_route_a_once(
            platform=platform,
            database_url=url,
            settings=settings,
            principal_id="user:operator",
            suite_name="route-a-live",
            golden_path=golden,
        )
    )

    assert result.passed is True
    assert read.principals[0].groups == ("group:served",)
    [row] = EvalRunStore(sa.create_engine(url)).list_runs(suite="route-a-live")
    assert row.config_hash == "composed-boot-hash"
    assert row.metrics == {"recall_at_10": 1.0}


def test_b3_scored_twin_persists_only_the_score_and_composed_hash(tmp_path) -> None:
    database = tmp_path / "eval.db"
    url = f"sqlite+pysqlite:///{database}"
    platform = cast("Platform", SimpleNamespace(config_hash="composed-boot-hash"))
    subjects = ("mer-dsp-003", "mer-dsp-005")
    rows = [
        {"dispute_id": "mer-dsp-003", "provisional_credit_amount_cents": 20000},
        {"dispute_id": "mer-dsp-005", "provisional_credit_amount_cents": 90000},
    ]

    score = persist_fabrication_twin(
        platform=platform,
        database_url=url,
        candidate=FabricationCandidate(
            text="$1,100 across 2 served disputes.",
            refused=False,
            claims=(
                FactClaim("currency", 110000, subjects, "provisional_credit_amount_cents"),
                FactClaim("count", 2, subjects),
            ),
        ),
        served_rows=rows,
        subject_field="dispute_id",
        allowed_subjects=subjects,
        required_fields=["provisional_credit_amount_cents"],
        case_hash="b3-case-hash",
    )

    assert score.passed is True
    [row] = EvalRunStore(sa.create_engine(url)).list_runs(suite="meridian-b3-fabrication")
    assert row.config_hash == "composed-boot-hash"
    assert row.golden_set_hash == "b3-case-hash"
    assert row.metrics == {"subject_attributed_fabrication_pass_rate": 1.0}
