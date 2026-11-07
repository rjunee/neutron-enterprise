# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint-C retrieval baseline proof — the full pipeline, green in CI, no servers.

Runs the WHOLE T1 retrieval + eval stack over the committed 100-question public
SQuAD golden set with ONLY CI-runnable components (no live TEI/vLLM/remote
Qdrant):

    squad_corpus.jsonl
      -> StructuralChunker
      -> QdrantChunkStore(':memory:')  [HashEmbedder dense + FastEmbed BM25 sparse, RRF]
      -> HybridRetriever -> RetrievalPipeline(LexicalReranker, candidate_multiplier=4)
      -> KnowledgeReadEvalTarget (extractive answerer)
      -> RagEvalSuite (LexicalJudge, fail-closed calibration, EvalRunStore)

and asserts the SPEC §5 gate thresholds (recall@10 >= .85, faithfulness >= .80,
context_precision >= .70, context_recall >= .80, tolerance bands). The judged
metrics use the deterministic :class:`~ne.eval.baseline.LexicalJudge`; the real
cross-family vLLM gateway judge (NE-013) is LIVE-PARKED. The one network touch is
the FastEmbed BM25 asset fetch. Its availability is gated by the
``NE_REQUIRE_PUBLIC_BASELINE`` flag (NE-CKC6): when the flag is unset (local dev)
the module SKIPS if the asset is unreachable, but when ``NE_REQUIRE_PUBLIC_BASELINE=1``
(CI, fail-closed) an unreachable asset is a HARD FAILURE — so "gate green" can
never silently mean "gate never ran".
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, NoReturn

import pytest
import sqlalchemy as sa

from ne.brain.ingest.chunker import StructuralChunker
from ne.brain.ingest.contracts import BlockKind, DocBlock, ParsedDocument
from ne.brain.pipeline import RetrievalPipeline
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.retriever import HybridRetriever
from ne.brain.sparse import FastEmbedBM25Encoder
from ne.brain.testing.embedding import HashEmbedder
from ne.contracts.config import compute_config_hash
from ne.contracts.evalobs import EvalRunResult
from ne.contracts.types import Principal
from ne.eval.baseline import KnowledgeReadEvalTarget, LexicalJudge, LexicalReranker
from ne.eval.calibration import (
    CalibrationArtifact,
    CalibrationItem,
    run_calibration,
    save_artifact,
)
from ne.eval.config import EvalSettings
from ne.eval.golden import GoldenSet, golden_set_hash, load_golden_set
from ne.eval.rubrics import get_rubric
from ne.eval.store import EvalRunStore, ensure_eval_schema
from ne.eval.suite import RagEvalSuite
from ne.eval.thresholds import DEFAULT_THRESHOLDS, MetricStatus, evaluate_thresholds
from ne.eval.trend import check_regression, load_baseline

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

_PUBLIC = Path(__file__).resolve().parent.parent / "goldensets" / "public"
CORPUS_PATH = _PUBLIC / "squad_corpus.jsonl"
GOLDEN_PATH = _PUBLIC / "squad_golden.jsonl"
BASELINE_PATH = _PUBLIC / "baseline.rag-public.json"
CALIBRATION_LABELS = _PUBLIC / "calibration"

SUITE_NAME = "rag-public"
JUDGE_MODEL_ID = "ne-lexical-judge-v1"
JUDGE_FAMILY = "heuristic-lexical"
EVALUATED_FAMILY = "extractive-baseline"

#: Rubrics whose committed human labels the LexicalJudge is calibrated against.
_LABELED_RUBRICS = ("faithfulness_verdicts", "context_precision", "context_recall")


@pytest.fixture(scope="module")
def bm25(baseline_guard: Callable[[str], NoReturn]) -> FastEmbedBM25Encoder:
    """A shared FastEmbed BM25 encoder.

    If the model assets can't load, the NE-CKC6 ``baseline_guard`` decides the
    outcome: it SKIPS (``LIVE-PARKED``) when ``NE_REQUIRE_PUBLIC_BASELINE`` is
    unset (local dev) and FAILS when the flag is ``"1"`` (CI, fail-closed) — so a
    missing asset can never green the Checkpoint-C gate by silently skipping it.
    """
    encoder = FastEmbedBM25Encoder()
    try:
        from fastembed import SparseTextEmbedding

        encoder._model = SparseTextEmbedding(encoder.model_id)
    except Exception as exc:
        baseline_guard(f"fastembed BM25 model assets unavailable ({exc!r})")
    return encoder


@pytest.fixture(scope="module")
def golden() -> GoldenSet:
    return load_golden_set(GOLDEN_PATH)


@pytest.fixture(scope="module")
def target(bm25: FastEmbedBM25Encoder) -> KnowledgeReadEvalTarget:
    """Ingest the public corpus into :memory: Qdrant and build the retrieval target."""
    corpus = [json.loads(line) for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines()]
    chunker = StructuralChunker()
    all_chunks = []
    for row in corpus:
        doc = ParsedDocument(
            document_id=row["document_id"],
            source_name=row["title"],
            source_format="md",
            blocks=(
                DocBlock(kind=BlockKind.HEADING, text=row["title"], level=1),
                DocBlock(kind=BlockKind.PARAGRAPH, text=row["text"]),
            ),
        )
        all_chunks.extend(chunker.chunk(doc))

    from qdrant_client import AsyncQdrantClient

    embedder = HashEmbedder(64)
    store = QdrantChunkStore(AsyncQdrantClient(":memory:"), "rag_public_baseline", embedder, bm25)

    async def _ingest() -> None:
        await store.ensure_collection()
        await store.upsert_chunks(all_chunks)

    asyncio.run(_ingest())
    pipeline = RetrievalPipeline(
        HybridRetriever(store, embedder, bm25, audit_sink=None),
        LexicalReranker(),
        candidate_multiplier=4,
    )
    return KnowledgeReadEvalTarget(pipeline, Principal(id="eval-ci"), top_k=10)


@pytest.fixture(scope="module")
def calibration_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build fail-closed calibration artifacts for every judged rubric.

    The three binary-verdict rubrics are calibrated against committed human labels
    (accuracy asserted >= 0.9). The decomposition rubric (faithfulness_statements)
    has no binary verdict, so its artifact is constructed directly — the same
    precedent as ``promptfoo/gate_provider.py``.
    """
    directory = tmp_path_factory.mktemp("calibration")
    judge = LexicalJudge()
    for rubric_id in _LABELED_RUBRICS:
        rubric = get_rubric(rubric_id)
        artifact = run_calibration(judge, rubric, CALIBRATION_LABELS / f"{rubric_id}_labels.jsonl")
        assert artifact.accuracy >= 0.9, f"{rubric_id} calibration accuracy {artifact.accuracy}"
        save_artifact(artifact, directory)

    stmt_rubric = get_rubric("faithfulness_statements")
    save_artifact(
        CalibrationArtifact(
            judge_model_id=judge.judge_model_id,
            judge_family=judge.judge_family,
            rubric_id=stmt_rubric.id,
            rubric_version=stmt_rubric.version,
            rubric_hash=stmt_rubric.content_hash,
            labels_hash="decomposition-no-binary-verdict",
            items=(CalibrationItem(item_id="decomp", human_verdict=1, judge_verdict=1),),
            accuracy=1.0,
            cohen_kappa=1.0,
            created_at=datetime.now(UTC),
        ),
        directory,
    )
    return directory


def _settings() -> EvalSettings:
    return EvalSettings(
        judge_plugin="baseline-lexical",
        judge_model_id=JUDGE_MODEL_ID,
        judge_family=JUDGE_FAMILY,
        evaluated_family=EVALUATED_FAMILY,
    )


class _Run(NamedTuple):
    result: EvalRunResult
    store: EvalRunStore
    config_hash: str
    golden: GoldenSet


@pytest.fixture(scope="module")
def run_result(
    target: KnowledgeReadEvalTarget,
    calibration_dir: Path,
    golden: GoldenSet,
    tmp_path_factory: pytest.TempPathFactory,
) -> _Run:
    db_path = tmp_path_factory.mktemp("evalstore") / "evalruns.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    ensure_eval_schema(engine)
    store = EvalRunStore(engine)
    config_hash = compute_config_hash(_settings())
    suite = RagEvalSuite(
        name=SUITE_NAME,
        golden=golden,
        target=target,
        judge=LexicalJudge(),
        thresholds=DEFAULT_THRESHOLDS,
        evaluated_family=EVALUATED_FAMILY,
        calibration_dir=str(calibration_dir),
        store=store,
        top_k=10,
    )
    result = suite.run_sync(config_hash=config_hash)
    return _Run(result=result, store=store, config_hash=config_hash, golden=golden)


def test_gate_thresholds_met(run_result: _Run) -> None:
    """The full-pipeline gate passes with zero FAIL metrics (the Checkpoint-C proof)."""
    assert run_result.result.passed is True
    statuses = evaluate_thresholds(run_result.result.metrics, DEFAULT_THRESHOLDS)
    assert all(status is not MetricStatus.FAIL for status in statuses.values()), statuses


def test_spec_metric_floors(run_result: _Run) -> None:
    """Each SPEC metric is at/above its threshold-minus-tolerance floor (literal numbers)."""
    metrics = run_result.result.metrics
    assert metrics["recall_at_10"] >= 0.83
    assert metrics["faithfulness"] >= 0.77
    assert metrics["context_precision"] >= 0.67
    assert metrics["context_recall"] >= 0.77


def test_run_recorded_with_config_hash(run_result: _Run) -> None:
    """The eval run is persisted with the real config_hash + golden_set_hash, no judge errors."""
    runs = run_result.store.list_runs(suite=SUITE_NAME)
    assert len(runs) == 1
    row = runs[0]
    assert row.config_hash == run_result.config_hash
    assert row.config_hash == compute_config_hash(_settings())
    assert row.golden_set_hash == golden_set_hash(run_result.golden)
    assert row.judge_error_rate == 0.0
    assert row.passed is True


def test_no_regression_vs_committed_baseline(run_result: _Run) -> None:
    """The committed ratchet matches this golden set and shows no regression."""
    baseline = load_baseline(BASELINE_PATH)
    assert baseline.suite == SUITE_NAME
    assert baseline.golden_set_hash == golden_set_hash(run_result.golden)
    assert check_regression(baseline, run_result.result.metrics) == []


def test_calibration_artifacts_agree_with_humans() -> None:
    """LexicalJudge agrees with the committed human labels on every judged rubric (>= 0.9)."""
    judge = LexicalJudge()
    for rubric_id in _LABELED_RUBRICS:
        artifact = run_calibration(
            judge, get_rubric(rubric_id), CALIBRATION_LABELS / f"{rubric_id}_labels.jsonl"
        )
        assert artifact.accuracy >= 0.9, (rubric_id, artifact.accuracy)
