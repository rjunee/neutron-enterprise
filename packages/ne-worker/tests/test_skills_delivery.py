# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-011F exact Skill-bundle reconcile and readiness controls."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa

from ne.api.skills_delivery import (
    SkillDeliveryUnavailable,
    reconcile_required_agent_skills,
    reconcile_required_agent_skills_from_environment,
    verify_required_agent_skills,
    verify_required_agent_skills_from_environment,
)
from ne.audit import PostgresAuditSink, ensure_audit_schema, list_records, records_by_event_ids
from ne.skills.config import SkillsSettings
from ne.skills.registry import ensure_skills_schema
from ne.worker.main import _append_skill_delivery_receipt

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_MANIFEST = REPO_ROOT / "deploy/agents/registry.toml"
SKILL_SOURCE = REPO_ROOT / "deploy/skills/meridian-hardship-draft"
PINNED_CONTENT_HASH = "dbc78dc3c2416336b124ac5aab0ed38a0d267496db12f08e3366c3fee5e2fb9d"
RAW_SKILL_HASH = "cdcf8cc87d6042d06a56afb6ff4b5494c192387c5b9abca4d99a9fae4854221d"
DELIVERY_FINGERPRINT = "f415a79e5ea653f4af14c29858b8cf5ec2f98edc1a5ef84e33fbeb2a2e9eb613"


@pytest.fixture
def engine(tmp_path: Path):
    value = sa.create_engine(f"sqlite:///{tmp_path / 'skills.db'}")
    ensure_skills_schema(value)
    yield value
    value.dispose()


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    shutil.copytree(SKILL_SOURCE, root / SKILL_SOURCE.name)
    return root


def test_reconcile_admits_exact_manifest_pin_and_readiness_rechecks_bytes(
    engine: sa.Engine, tmp_path: Path
) -> None:
    bundle = _bundle(tmp_path)
    receipt = reconcile_required_agent_skills(
        engine,
        SkillsSettings(bundle_dir=str(bundle)),
        agent_manifest_path=str(AGENT_MANIFEST),
        expected_raw_hash=RAW_SKILL_HASH,
    )

    assert receipt.pins == (("meridian-hardship-draft", 3, PINNED_CONTENT_HASH),)
    assert receipt.raw_bundle_sha256 == RAW_SKILL_HASH
    assert json.loads(receipt.audit_detail) == {
        "delivery_fingerprint": receipt.fingerprint,
        "pins": [
            {
                "content_hash": PINNED_CONTENT_HASH,
                "skill_id": "meridian-hardship-draft",
                "version": 3,
            }
        ],
        "raw_bundle_sha256": RAW_SKILL_HASH,
    }
    assert receipt.audit_detail == json.dumps(
        json.loads(receipt.audit_detail),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    assert receipt.fingerprint == DELIVERY_FINGERPRINT
    assert (
        verify_required_agent_skills(
            engine,
            SkillsSettings(bundle_dir=str(bundle)),
            agent_manifest_path=str(AGENT_MANIFEST),
            expected_raw_hash=RAW_SKILL_HASH,
        )
        == receipt
    )


def test_configured_manifest_refuses_missing_bundle_even_with_stale_registry(
    engine: sa.Engine, tmp_path: Path
) -> None:
    bundle = _bundle(tmp_path)
    settings = SkillsSettings(bundle_dir=str(bundle))
    reconcile_required_agent_skills(
        engine,
        settings,
        agent_manifest_path=str(AGENT_MANIFEST),
        expected_raw_hash=RAW_SKILL_HASH,
    )
    shutil.rmtree(bundle)

    with pytest.raises(SkillDeliveryUnavailable, match="bundle directory"):
        verify_required_agent_skills(
            engine,
            settings,
            agent_manifest_path=str(AGENT_MANIFEST),
            expected_raw_hash=RAW_SKILL_HASH,
        )


def test_mounted_byte_tamper_refuses_ready_after_successful_reconcile(
    engine: sa.Engine, tmp_path: Path
) -> None:
    bundle = _bundle(tmp_path)
    settings = SkillsSettings(bundle_dir=str(bundle))
    reconcile_required_agent_skills(
        engine,
        settings,
        agent_manifest_path=str(AGENT_MANIFEST),
        expected_raw_hash=RAW_SKILL_HASH,
    )
    skill_md = bundle / "meridian-hardship-draft/SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8").replace(
            "Never send by any other means", "Send by any means"
        ),
        encoding="utf-8",
    )

    with pytest.raises(SkillDeliveryUnavailable, match="rollout identity mismatch"):
        verify_required_agent_skills(
            engine,
            settings,
            agent_manifest_path=str(AGENT_MANIFEST),
            expected_raw_hash=RAW_SKILL_HASH,
        )


def test_manifest_hash_mismatch_refuses_reconcile(engine: sa.Engine, tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    manifest = tmp_path / "registry.toml"
    manifest.write_text(
        AGENT_MANIFEST.read_text(encoding="utf-8").replace(PINNED_CONTENT_HASH, "0" * 64),
        encoding="utf-8",
    )

    with pytest.raises(SkillDeliveryUnavailable, match="mounted Skill pin mismatch"):
        reconcile_required_agent_skills(
            engine,
            SkillsSettings(bundle_dir=str(bundle)),
            agent_manifest_path=str(manifest),
            expected_raw_hash=RAW_SKILL_HASH,
        )


def test_default_off_manifest_and_bundle_are_an_honest_noop(engine: sa.Engine) -> None:
    receipt = reconcile_required_agent_skills(
        engine,
        SkillsSettings(bundle_dir=""),
        agent_manifest_path="",
        expected_raw_hash="",
    )
    assert receipt.pins == ()


def test_api_readiness_environment_reads_the_shared_reconciled_registry(
    engine: sa.Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    reconcile_required_agent_skills(
        engine,
        SkillsSettings(bundle_dir=str(bundle)),
        agent_manifest_path=str(AGENT_MANIFEST),
        expected_raw_hash=RAW_SKILL_HASH,
    )
    monkeypatch.setenv("NE_API_AGENT_MANIFEST_PATH", str(AGENT_MANIFEST))
    monkeypatch.setenv("NE_SKILLS_BUNDLE_DIR", str(bundle))
    monkeypatch.setenv("NE_FLYWHEEL_SIGNAL_DATABASE_URL", str(engine.url))
    monkeypatch.setenv("NE_SKILL_BUNDLE_RAW_SHA256", RAW_SKILL_HASH)

    receipt = verify_required_agent_skills_from_environment()

    assert receipt.pins == (("meridian-hardship-draft", 3, PINNED_CONTENT_HASH),)


def test_worker_environment_reconciles_and_disposes_before_platform(
    engine: sa.Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ne.api.skills_delivery as delivery

    bundle = _bundle(tmp_path)
    monkeypatch.setenv("NE_API_AGENT_MANIFEST_PATH", str(AGENT_MANIFEST))
    monkeypatch.setenv("NE_SKILLS_BUNDLE_DIR", str(bundle))
    monkeypatch.setenv("NE_FLYWHEEL_SIGNAL_DATABASE_URL", str(engine.url))
    monkeypatch.setenv("NE_SKILL_BUNDLE_RAW_SHA256", RAW_SKILL_HASH)
    disposed = 0
    real_dispose = engine.dispose

    def record_dispose() -> None:
        nonlocal disposed
        disposed += 1
        real_dispose()

    monkeypatch.setattr(delivery, "create_engine_with_timeouts", lambda _url: engine)
    monkeypatch.setattr(engine, "dispose", record_dispose)

    receipt = reconcile_required_agent_skills_from_environment()

    assert receipt.pins == (("meridian-hardship-draft", 3, PINNED_CONTENT_HASH),)
    assert disposed == 1


def test_api_readiness_refuses_an_unreconciled_bundle_without_writing(
    engine: sa.Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    monkeypatch.setenv("NE_API_AGENT_MANIFEST_PATH", str(AGENT_MANIFEST))
    monkeypatch.setenv("NE_SKILLS_BUNDLE_DIR", str(bundle))
    monkeypatch.setenv("NE_FLYWHEEL_SIGNAL_DATABASE_URL", str(engine.url))
    monkeypatch.setenv("NE_SKILL_BUNDLE_RAW_SHA256", RAW_SKILL_HASH)

    with pytest.raises(SkillDeliveryUnavailable, match="not admitted"):
        verify_required_agent_skills_from_environment()

    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT count(*) FROM ne_skills")) == 0


def test_registry_tamper_refuses_readiness_without_exposing_procedure(
    engine: sa.Engine, tmp_path: Path
) -> None:
    bundle = _bundle(tmp_path)
    settings = SkillsSettings(bundle_dir=str(bundle))
    reconcile_required_agent_skills(
        engine,
        settings,
        agent_manifest_path=str(AGENT_MANIFEST),
        expected_raw_hash=RAW_SKILL_HASH,
    )
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE ne_skill_versions SET body = :body WHERE name = :name"),
            {"body": "PROCEDURE_SENTINEL", "name": "meridian-hardship-draft"},
        )

    with pytest.raises(SkillDeliveryUnavailable, match="not admitted") as caught:
        verify_required_agent_skills(
            engine,
            settings,
            agent_manifest_path=str(AGENT_MANIFEST),
            expected_raw_hash=RAW_SKILL_HASH,
        )
    assert "PROCEDURE_SENTINEL" not in str(caught.value)


def test_worker_reconciles_before_installing_globals_or_launching_dbos() -> None:
    source = (REPO_ROOT / "packages/ne-worker/src/ne/worker/main.py").read_text(encoding="utf-8")
    reconcile = source.index("skill_receipt = await asyncio.to_thread(")
    compose = source.index("platform = build_platform(api_settings, seams)")
    append = source.index(
        "skill_receipt_identity = await _append_skill_delivery_receipt(platform, skill_receipt)"
    )
    install = source.index("set_worker_platform(platform)")
    dbos = source.index("dbos_instance = _launch_dbos(")
    assert source.count("await _append_skill_delivery_receipt(platform, skill_receipt)") == 1
    assert reconcile < compose < append < install < dbos


def test_worker_persists_one_exact_content_free_receipt_per_boot(
    engine: sa.Engine, tmp_path: Path
) -> None:
    bundle = _bundle(tmp_path / "CREDENTIAL_SENTINEL")
    receipt = reconcile_required_agent_skills(
        engine,
        SkillsSettings(bundle_dir=str(bundle)),
        agent_manifest_path=str(AGENT_MANIFEST),
        expected_raw_hash=RAW_SKILL_HASH,
    )
    audit_engine = sa.create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    ensure_audit_schema(audit_engine)
    platform = SimpleNamespace(
        config_hash="c" * 64,
        sink=PostgresAuditSink(audit_engine),
        audit_engine=audit_engine,
        credential="CREDENTIAL_SENTINEL",
    )
    try:
        first = asyncio.run(_append_skill_delivery_receipt(cast(Any, platform), receipt))
        assert len(list_records(audit_engine, action="skill.delivery.reconciled")) == 1
        second = asyncio.run(_append_skill_delivery_receipt(cast(Any, platform), receipt))
        records = records_by_event_ids(audit_engine, [first.event_id, second.event_id])
        assert len(list_records(audit_engine, action="skill.delivery.reconciled")) == 2
    finally:
        audit_engine.dispose()

    assert len(records) == 2
    assert first.event_id != second.event_id
    assert first.audit_sequence < second.audit_sequence
    assert first.audit_record_hash != second.audit_record_hash
    assert [record.seq for record in records] == [
        first.audit_sequence,
        second.audit_sequence,
    ]
    assert [record.record_hash for record in records] == [
        first.audit_record_hash,
        second.audit_record_hash,
    ]
    for record in records:
        assert record.action == "skill.delivery.reconciled"
        assert record.actor == "system:ne-worker"
        assert record.config_hash == "c" * 64
        assert record.resource == "skill-delivery:required-agent-skills"
        assert record.outcome == "success"
        assert record.payload["provenance"] == {
            "delivery_fingerprint": receipt.fingerprint,
            "producer": "ne-worker.boot",
        }
        assert record.payload["detail"] == receipt.audit_detail
        detail = json.loads(record.payload["detail"])
        assert detail["raw_bundle_sha256"] == RAW_SKILL_HASH
        assert detail["delivery_fingerprint"] == receipt.fingerprint
        serialized = json.dumps(record.payload, sort_keys=True)
        assert str(bundle) not in serialized
        assert "CREDENTIAL_SENTINEL" not in serialized
        assert "Create the draft through the governed draft tool" not in serialized


def test_worker_receipt_append_failure_propagates_without_a_persisted_identity(
    engine: sa.Engine, tmp_path: Path
) -> None:
    class FailingSink:
        async def append(self, _event: object) -> None:
            raise RuntimeError("AUDIT_APPEND_SENTINEL")

    receipt = reconcile_required_agent_skills(
        engine,
        SkillsSettings(bundle_dir=str(_bundle(tmp_path))),
        agent_manifest_path=str(AGENT_MANIFEST),
        expected_raw_hash=RAW_SKILL_HASH,
    )
    audit_engine = sa.create_engine(f"sqlite:///{tmp_path / 'audit-failure.db'}")
    ensure_audit_schema(audit_engine)
    platform = SimpleNamespace(
        config_hash="d" * 64,
        sink=FailingSink(),
        audit_engine=audit_engine,
    )
    try:
        with pytest.raises(RuntimeError, match="AUDIT_APPEND_SENTINEL"):
            asyncio.run(_append_skill_delivery_receipt(cast(Any, platform), receipt))
        with audit_engine.connect() as conn:
            assert conn.scalar(sa.text("SELECT count(*) FROM ne_audit_log")) == 0
    finally:
        audit_engine.dispose()


def test_production_api_factory_verifies_before_create_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ne.api.app as app
    import ne.api.skills_delivery as delivery

    calls: list[str] = []
    sentinel = object()
    monkeypatch.setattr(
        delivery,
        "verify_required_agent_skills_from_environment",
        lambda: calls.append("verify"),
    )
    monkeypatch.setattr(app, "ApiSettings", lambda: object())

    def create(_settings: object) -> object:
        calls.append("create")
        return sentinel

    monkeypatch.setattr(app, "create_app", create)

    assert app.app_factory() is sentinel
    assert calls == ["verify", "create"]
