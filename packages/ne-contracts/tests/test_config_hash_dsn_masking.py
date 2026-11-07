# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""config_hash URL-userinfo masking (ADR-0006 / bulletproofing P1-1 / C12).

Proves that DB-password userinfo carried in plain-``str`` DSN fields never
enters the SHA-256 ``config_hash`` preimage, that rotation is hash-invariant,
and that configs WITHOUT URL userinfo hash byte-for-byte identically to the
legacy (pre-masking) formula (scoping the one-time reshuffle to userinfo-bearing
configs only).

TEST-ONLY cross-package imports: the composed-settings guard
(``test_module_settings_dsn_secret_never_in_hash_preimage``) imports the real
settings classes of eight other packages to prove the leak is closed in
production configs, not just in local fixtures. This is safe under workspace CI
(``uv sync --all-packages`` + root ``pytest`` make every package importable) and
is invisible to ``lint-imports`` (``.importlinter`` checks only ``src`` root
packages — ``ne.contracts`` ``src`` still imports no module, so C5 src
decoupling is untouched). If a per-package-isolation test lane is ever
introduced, this file needs a skip marker.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel

from ne.contracts import (
    compute_config_hash,
    compute_platform_config_hash,
    reset_config_hash_for_testing,
)
from ne.contracts.config import (
    _canonical_config_preimage,
    _canonical_platform_preimage,
)

SECRET = "s3cr3t-hunter2-do-not-leak"


@pytest.fixture(autouse=True)
def _reset_frozen_hash() -> Iterator[None]:
    reset_config_hash_for_testing()
    yield
    reset_config_hash_for_testing()


class _Dsn(BaseModel):
    """Local model with a plain-``str`` DSN, mirroring every module's DSN field."""

    dsn: str = "postgresql://ne:orig@postgres:5432/ne"


def _legacy_config_hash(model: BaseModel) -> str:
    """The OLD (pre-ADR-0006) formula: sha256 over the raw JSON dump, no masking."""
    tree = model.model_dump(mode="json")
    canonical = json.dumps(tree, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _legacy_platform_hash(modules: dict[str, BaseModel]) -> str:
    """The OLD (pre-ADR-0006) combined-dict formula, no masking."""
    combined: dict[str, Any] = {k: s.model_dump(mode="json") for k, s in modules.items()}
    canonical = json.dumps(combined, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_dsn_password_rotation_does_not_change_hash() -> None:
    a = compute_config_hash(_Dsn(dsn="postgresql://ne:PW_ONE@postgres:5432/ne"))
    b = compute_config_hash(_Dsn(dsn="postgresql://ne:PW_TWO@postgres:5432/ne"))
    assert a == b, "config_hash must be independent of the DSN password"


def test_dsn_username_and_password_userinfo_fully_masked() -> None:
    # User-only userinfo (no password) is rotation-invariant.
    u1 = compute_config_hash(_Dsn(dsn="postgresql+psycopg://userA@host/db"))
    u2 = compute_config_hash(_Dsn(dsn="postgresql+psycopg://userB@host/db"))
    assert u1 == u2
    # Password-only userinfo (no username) is rotation-invariant.
    p1 = compute_config_hash(_Dsn(dsn="redis://:pwA@host:6379/0"))
    p2 = compute_config_hash(_Dsn(dsn="redis://:pwB@host:6379/0"))
    assert p1 == p2


def test_hash_changes_when_non_userinfo_parts_change() -> None:
    base = compute_config_hash(_Dsn(dsn="postgresql://ne:pw@postgres:5432/ne"))
    # Every non-userinfo component still contributes to the digest.
    assert compute_config_hash(_Dsn(dsn="postgresql://ne:pw@OTHERHOST:5432/ne")) != base
    assert compute_config_hash(_Dsn(dsn="postgresql://ne:pw@postgres:6543/ne")) != base
    assert compute_config_hash(_Dsn(dsn="postgresql://ne:pw@postgres:5432/other_db")) != base
    assert (
        compute_config_hash(_Dsn(dsn="postgresql://ne:pw@postgres:5432/ne?sslmode=require")) != base
    )


class _NoUserinfo(BaseModel):
    """Strings that must all hash byte-for-byte identically to the legacy formula."""

    sqlite: str = "sqlite:///x.db"
    https: str = "https://graph.microsoft.com/v1.0"
    pg: str = "postgresql://localhost:5432/postgres"
    mail: str = "mailto:a@b"
    prose: str = "not a url"
    scheme_relative: str = "//user:pass@host"
    empty: str = ""
    number: int = 7


def test_hash_unchanged_for_configs_without_userinfo() -> None:
    model = _NoUserinfo()
    # NEW compute equals the OLD inline formula -> byte-for-byte preimage preservation.
    assert compute_config_hash(model) == _legacy_config_hash(model)
    # Same guarantee for the platform variant with the legacy combined-dict formula.
    mapping: dict[str, BaseModel] = {"mod": model}
    assert compute_platform_config_hash(mapping) == _legacy_platform_hash(mapping)


class _Inner(BaseModel):
    dsn: str = "postgresql://ne:orig@postgres:5432/ne"


class _Nested(BaseModel):
    inner: _Inner = _Inner()
    by_name: dict[str, str] = {"primary": "postgresql://ne:orig@postgres:5432/ne"}
    keyed_by_dsn: dict[str, int] = {"postgresql://ne:orig@postgres:5432/ne": 1}
    urls: list[str] = ["postgresql://ne:orig@postgres:5432/ne"]


def _nested_with(password: str) -> _Nested:
    dsn = f"postgresql://ne:{password}@postgres:5432/ne"
    return _Nested(
        inner=_Inner(dsn=dsn),
        by_name={"primary": dsn},
        keyed_by_dsn={dsn: 1},
        urls=[dsn],
    )


def test_nested_dsn_masked() -> None:
    a = compute_config_hash(_nested_with("PW_ONE"))
    b = compute_config_hash(_nested_with("PW_TWO"))
    assert a == b, "userinfo masked in sub-model, dict value, dict key, and list"
    preimage = _canonical_config_preimage(_nested_with(SECRET))
    assert SECRET not in preimage
    assert "***:***@" in preimage


def test_malformed_dsn_userinfo_still_masked() -> None:
    """A DSN that ``urlsplit`` refuses to parse must still not leak its password.

    An unterminated IPv6 literal makes ``urlsplit`` raise ``ValueError``; the
    conservative raw-string fallback must keep the secret out of the preimage and
    keep the hash rotation-invariant (bias = mask-more-never-leak, C12).
    """
    from urllib.parse import urlsplit

    malformed = f"postgresql://ne:{SECRET}@[invalid/db"
    # Precondition: this really is a urlsplit-raising input (else the test is moot).
    with pytest.raises(ValueError):
        urlsplit(malformed)

    settings = _Dsn(dsn=malformed)
    preimage = _canonical_config_preimage(settings)
    assert SECRET not in preimage
    assert f":{SECRET}@" not in preimage
    assert "***:***@" in preimage

    # Rotation-invariant even though the authority is malformed.
    rotated = _Dsn(dsn=f"postgresql://ne:rotated-{SECRET}@[invalid/db")
    assert compute_config_hash(settings) == compute_config_hash(rotated)

    # Password-only malformed userinfo (redis://:pw@[bad) is covered too.
    p1 = compute_config_hash(_Dsn(dsn="redis://:pwA@[bad"))
    p2 = compute_config_hash(_Dsn(dsn="redis://:pwB@[bad"))
    assert p1 == p2

    # A malformed URL with no valid scheme before "://" is left untouched (no
    # over-masking of arbitrary prose).
    from ne.contracts.config import _mask_url_userinfo

    prose = "foo bar://x:y@[bad"
    assert _mask_url_userinfo(prose) == prose


def test_preimage_contains_no_secret_and_has_sentinel() -> None:
    settings = _Dsn(dsn=f"postgresql://ne:{SECRET}@postgres:5432/ne")
    single = _canonical_config_preimage(settings)
    platform = _canonical_platform_preimage({"mod": settings})
    assert SECRET not in single
    assert SECRET not in platform
    assert "***:***@" in single
    assert "***:***@" in platform


def _module_settings_cases() -> list[tuple[str, str, str, dict[str, str]]]:
    """(module, settings_class, dsn_field, required_kwargs) for the guard.

    Kept as (import_path, class_name, ...) so the parametrize ids are readable
    and the imports happen lazily inside the test.
    """
    api_kwargs = {
        "qdrant_url": "http://qdrant:6333",
        "qdrant_collection": "ne_chunks",
        "tei_url": "http://tei:80",
    }
    return [
        ("ne.audit.config", "AuditSettings", "database_url", {}),
        ("ne.runtime.config", "RuntimeSettings", "session_database_url", {}),
        ("ne.permissions.config", "PermissionsSettings", "database_url", {}),
        ("ne.flywheel.config", "FlywheelSettings", "signal_database_url", {}),
        ("ne.api.settings", "ApiSettings", "pgvector_dsn", api_kwargs),
        ("ne.api.settings", "ApiSettings", "eval_database_url", api_kwargs),
        ("ne.api.settings", "ApiSettings", "dbos_system_database_url", api_kwargs),
        ("ne.obs.config", "ObsReportSettings", "audit_database_url", {}),
        ("ne.obs.config", "ObsReportSettings", "eval_database_url", {}),
        ("ne.obs.config", "ObsReportSettings", "signal_database_url", {}),
        ("ne.worker.settings", "WorkerSettings", "dbos_database_url", {}),
        ("ne.brain_graph.config", "BrainGraphSettings", "database_url", {}),
        ("ne.brain_graph.config", "BrainGraphSettings", "ledger_url", {}),
    ]


@pytest.mark.parametrize(
    ("import_path", "class_name", "dsn_field", "required_kwargs"),
    _module_settings_cases(),
    ids=[f"{c[1]}.{c[2]}" for c in _module_settings_cases()],
)
def test_module_settings_dsn_secret_never_in_hash_preimage(
    import_path: str,
    class_name: str,
    dsn_field: str,
    required_kwargs: dict[str, str],
) -> None:
    """THE composed-settings guard (brief requirement): real module DSNs.

    Constructs each DSN-carrying module settings class with a secret-bearing DSN
    and asserts the secret never appears in either preimage, that both hashes are
    invariant under password rotation, and that both are sensitive to a host
    change (proving only userinfo is masked).
    """
    import importlib

    settings_cls = getattr(importlib.import_module(import_path), class_name)

    def build(dsn: str) -> BaseModel:
        return settings_cls(**required_kwargs, **{dsn_field: dsn})

    secret_dsn = f"postgresql://ne:{SECRET}@postgres:5432/ne"
    settings = build(secret_dsn)

    single = _canonical_config_preimage(settings)
    platform = _canonical_platform_preimage({"mod": settings})
    assert SECRET not in single
    assert SECRET not in platform
    # No "://user:pass@" with a real secret survives into the hashed json.
    assert f":{SECRET}@" not in single
    assert f":{SECRET}@" not in platform

    # Rotation-invariant: only the password changes.
    rotated_dsn = f"postgresql://ne:rotated-{SECRET}@postgres:5432/ne"
    assert compute_config_hash(settings) == compute_config_hash(build(rotated_dsn))
    assert compute_platform_config_hash({"mod": settings}) == compute_platform_config_hash(
        {"mod": build(rotated_dsn)}
    )

    # Sensitive to a host change (everything but userinfo still contributes).
    host_changed = build("postgresql://ne:pw@OTHERHOST:5432/ne")
    assert compute_config_hash(settings) != compute_config_hash(host_changed)
    assert compute_platform_config_hash({"mod": settings}) != compute_platform_config_hash(
        {"mod": host_changed}
    )
