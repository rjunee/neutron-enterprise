# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exact Skill-bundle admission and read-only production readiness.

The worker calls :func:`reconcile_required_agent_skills_from_environment` before
constructing the shared platform.  API production composition calls the
read-only verifier before ``create_app``.  Both roots therefore bind the same
mounted raw bytes, Agent pin, and hash-verified shared-registry row without an
API dependency on ``ne.worker``.

This module deliberately does not compute an ActiveSet or materialize a Skill;
those are PRE-011B responsibilities.  Errors and receipts are metadata-only and
never expose procedure text.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa

from ne.api.agent_manifest import AgentManifestError, load_agent_manifest
from ne.audit import create_engine_with_timeouts, ensure_transactional_sqlite
from ne.contracts.skills import AuthoringStandard, SkillPack
from ne.events import new_audit_event
from ne.schema import AuditEvent, AuditOutcome
from ne.skills.config import SkillsSettings
from ne.skills.packio import pack_from_dir
from ne.skills.reconcile import reconcile_bundle_dir
from ne.skills.registry import SqlAlchemySkillRegistry, ensure_skills_schema

SkillPin = tuple[str, int, str]
_RAW_HASH_ENV = "NE_SKILL_BUNDLE_RAW_SHA256"
_DATABASE_URL_ENV = "NE_FLYWHEEL_SIGNAL_DATABASE_URL"
_MANIFEST_PATH_ENV = "NE_API_AGENT_MANIFEST_PATH"


class SkillDeliveryUnavailable(RuntimeError):
    """The configured Skill bundle is absent, invalid, or not reconciled."""


@dataclass(frozen=True, slots=True)
class SkillDeliveryReceipt:
    """Content-free readiness receipt over exact required Skill pins.

    ``audit_detail`` is the canonical, closed metadata projection the worker may
    persist after the platform's real audit sink and config hash exist.  It
    deliberately contains no bundle path, procedure body, or credential value.
    """

    pins: tuple[SkillPin, ...]
    raw_bundle_sha256: str
    fingerprint: str
    audit_detail: str


def _required_pins(agent_manifest_path: str) -> tuple[SkillPin, ...]:
    try:
        loaded = load_agent_manifest(agent_manifest_path)
    except AgentManifestError as exc:
        raise SkillDeliveryUnavailable("configured Agent manifest is unavailable") from exc

    pins: dict[str, tuple[int, str]] = {}
    for agent in loaded.manifest.agents:
        for skill in agent.skills:
            value = (skill.version, skill.content_hash)
            prior = pins.setdefault(skill.skill_id, value)
            if prior != value:
                raise SkillDeliveryUnavailable("Agent manifest contains conflicting Skill pins")
    return tuple((name, version, digest) for name, (version, digest) in sorted(pins.items()))


def _fingerprint(raw_hash: str, pins: tuple[SkillPin, ...]) -> str:
    canonical = json.dumps(
        {"raw_hash": raw_hash, "pins": pins},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(b"neutron:skill-delivery:v1\0" + canonical).hexdigest()


def _receipt(raw_hash: str, pins: tuple[SkillPin, ...]) -> SkillDeliveryReceipt:
    fingerprint = _fingerprint(raw_hash, pins)
    audit_detail = json.dumps(
        {
            "delivery_fingerprint": fingerprint,
            "pins": [
                {"content_hash": content_hash, "skill_id": skill_id, "version": version}
                for skill_id, version, content_hash in pins
            ],
            "raw_bundle_sha256": raw_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return SkillDeliveryReceipt(
        pins=pins,
        raw_bundle_sha256=raw_hash,
        fingerprint=fingerprint,
        audit_detail=audit_detail,
    )


def skill_delivery_audit_event(receipt: SkillDeliveryReceipt, *, config_hash: str) -> AuditEvent:
    """Build the closed worker-boot audit event from delivery metadata only."""

    return new_audit_event(
        action="skill.delivery.reconciled",
        resource="skill-delivery:required-agent-skills",
        outcome=AuditOutcome.success,
        actor="system:ne-worker",
        detail=receipt.audit_detail,
        provenance={
            "delivery_fingerprint": receipt.fingerprint,
            "producer": "ne-worker.boot",
        },
        config_hash=config_hash,
    )


def _expected_raw_hash(value: str, *, required: bool) -> str:
    if not required and not value:
        return ""
    if (
        len(value) != 64
        or value.lower() != value
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise SkillDeliveryUnavailable("Skill bundle rollout identity is unavailable")
    return value


def _mounted_pack(root: Path, pin: SkillPin, *, expected_raw_hash: str) -> SkillPack:
    name, version, expected_content_hash = pin
    skill_md = root / name / "SKILL.md"
    try:
        raw_hash = hashlib.sha256(skill_md.read_bytes()).hexdigest()
    except OSError as exc:
        raise SkillDeliveryUnavailable("required mounted Skill is unavailable") from exc
    if raw_hash != expected_raw_hash:
        raise SkillDeliveryUnavailable("mounted Skill rollout identity mismatch")
    result = pack_from_dir(root / name, standard=AuthoringStandard())
    if not isinstance(result, SkillPack):
        raise SkillDeliveryUnavailable("required mounted Skill is unavailable")
    if (result.name, result.version, result.content_hash) != (
        name,
        version,
        expected_content_hash,
    ):
        raise SkillDeliveryUnavailable("mounted Skill pin mismatch")
    return result


def verify_required_agent_skills(
    engine: sa.Engine,
    settings: SkillsSettings,
    *,
    agent_manifest_path: str,
    expected_raw_hash: str,
) -> SkillDeliveryReceipt:
    """Read-only exact mount + manifest + shared-registry readiness check."""

    pins = _required_pins(agent_manifest_path)
    raw_hash = _expected_raw_hash(expected_raw_hash, required=bool(pins))
    if not pins:
        return _receipt(raw_hash, ())
    if not settings.bundle_dir:
        raise SkillDeliveryUnavailable("required Skill bundle directory is not configured")
    root = Path(settings.bundle_dir)
    if not root.is_dir():
        raise SkillDeliveryUnavailable("required Skill bundle directory is unavailable")

    registry = SqlAlchemySkillRegistry(engine)
    for pin in pins:
        mounted = _mounted_pack(root, pin, expected_raw_hash=raw_hash)
        admitted = registry.get_current(pin[0])
        if admitted is None:
            raise SkillDeliveryUnavailable("required Skill is not admitted")
        if (admitted.name, admitted.version, admitted.content_hash) != (
            mounted.name,
            mounted.version,
            mounted.content_hash,
        ):
            raise SkillDeliveryUnavailable("shared Skill registry pin mismatch")

    return _receipt(raw_hash, pins)


def reconcile_required_agent_skills(
    engine: sa.Engine | None,
    settings: SkillsSettings | None,
    *,
    agent_manifest_path: str,
    expected_raw_hash: str,
) -> SkillDeliveryReceipt:
    """Reconcile as the worker, then require exact mount/manifest/row equality."""

    pins = _required_pins(agent_manifest_path)
    raw_hash = _expected_raw_hash(expected_raw_hash, required=bool(pins))
    if not pins:
        return _receipt(raw_hash, ())
    if engine is None or settings is None:
        raise SkillDeliveryUnavailable("shared Skill registry is unavailable")
    if not settings.bundle_dir or not Path(settings.bundle_dir).is_dir():
        raise SkillDeliveryUnavailable("required Skill bundle directory is unavailable")
    for pin in pins:
        _mounted_pack(Path(settings.bundle_dir), pin, expected_raw_hash=raw_hash)
    reconcile_bundle_dir(engine, settings)
    return verify_required_agent_skills(
        engine,
        settings,
        agent_manifest_path=agent_manifest_path,
        expected_raw_hash=raw_hash,
    )


def verify_required_agent_skills_from_environment() -> SkillDeliveryReceipt:
    """Verify production delivery from environment without schema or row writes."""

    agent_manifest_path = os.environ.get(_MANIFEST_PATH_ENV, "")
    pins = _required_pins(agent_manifest_path)
    raw_hash = _expected_raw_hash(os.environ.get(_RAW_HASH_ENV, ""), required=bool(pins))
    settings = SkillsSettings()  # type: ignore[call-arg]
    database_url = os.environ.get(_DATABASE_URL_ENV, "")
    if pins and not database_url:
        raise SkillDeliveryUnavailable("shared Skill registry is unavailable")
    if not database_url:
        return _receipt(raw_hash, ())
    engine = create_engine_with_timeouts(database_url)
    try:
        return verify_required_agent_skills(
            engine,
            settings,
            agent_manifest_path=agent_manifest_path,
            expected_raw_hash=raw_hash,
        )
    finally:
        engine.dispose()


def reconcile_required_agent_skills_from_environment() -> SkillDeliveryReceipt:
    """Worker-only precomposition reconcile, disposing its temporary engine."""

    agent_manifest_path = os.environ.get(_MANIFEST_PATH_ENV, "")
    pins = _required_pins(agent_manifest_path)
    raw_hash = _expected_raw_hash(os.environ.get(_RAW_HASH_ENV, ""), required=bool(pins))
    settings = SkillsSettings()  # type: ignore[call-arg]
    database_url = os.environ.get(_DATABASE_URL_ENV, "")
    if not pins:
        return _receipt(raw_hash, ())
    if not database_url:
        raise SkillDeliveryUnavailable("shared Skill registry is unavailable")
    engine = create_engine_with_timeouts(database_url)
    try:
        ensure_transactional_sqlite(engine)
        ensure_skills_schema(engine)
        return reconcile_required_agent_skills(
            engine,
            settings,
            agent_manifest_path=agent_manifest_path,
            expected_raw_hash=raw_hash,
        )
    finally:
        engine.dispose()


def main() -> int:
    """Container init/readiness entry point; emit no path, pin, or row detail."""

    try:
        verify_required_agent_skills_from_environment()
    except Exception:
        print("Skill delivery unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
