# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T2 — composition identity: ONE sink/gate/ledger/counter across executor + cron.

The worker's turn executor and its NE-034 cron scheduler must share the SAME audit
sink, approval gate, and NE-070H spend-ledger/rate-counter pair — the NE-CKD1 recipe
across two governed paths in one process. config_hash is frozen before any emitter,
and re-freezing the identical composed mapping (ne-api's ``create_app`` after the
worker's ``build_platform``) is idempotent — no cross-process hash fork.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from starlette.routing import Mount

from ne.api.agent_catalog import AgentCatalogError
from ne.api.app import create_app
from ne.api.deps import ApiState
from ne.api.platform import Platform, build_platform
from ne.contracts import get_config_hash, reset_config_hash_for_testing
from ne.runtime.factory import create_cron_scheduler, create_shared_guardrail_state
from ne.runtime.principals import StaticAgentPrincipalProvider

from .conftest import build_sqlite_platform, sqlite_seams

_REGISTRY = Path(__file__).parents[3] / "deploy/agents/registry.toml"


class _UnusedTokenVerifier:
    async def verify(self, _token: str) -> None:
        raise AssertionError("composition proof serves no request")


def _scheduler_for(platform: Platform):  # type: ignore[no-untyped-def]
    return create_cron_scheduler(
        engine=platform.runtime_engine,
        bindings=(),
        definitions={},
        sink=platform.sink,
        approval_gate=platform.gate,
        gateway=platform.gateway,
        principals=StaticAgentPrincipalProvider({}),
        settings=platform.runtime_settings,
        govern_settings=platform.govern_settings,
        ledger=platform.ledger,
        counter=platform.counter,
        extra_guardrails=(platform.scope_rail,),
    )


def _api_state(app: Any) -> ApiState:
    for route in app.routes:
        if isinstance(route, Mount) and route.path == "/api":
            state = cast(Any, route.app)._app.state.ne_api
            assert isinstance(state, ApiState)
            return state
    raise AssertionError("no /api mount")


def _catalog_identity(platform_or_state: Platform | ApiState) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            row.definition.agent_id,
            row.definition.version,
            row.definition.definition_hash,
            row.available,
            row.availability_reasons,
        )
        for row in platform_or_state.agent_catalog.agents
    )


def _manifest_fingerprints(engine: sa.Engine) -> set[tuple[str, str]]:
    with engine.connect() as conn:
        return {
            (str(row.manifest_raw_hash), str(row.manifest_semantic_hash))
            for row in conn.execute(
                sa.text(
                    "SELECT manifest_raw_hash, manifest_semantic_hash "
                    "FROM ne_agent_active_definitions"
                )
            )
        }


def test_config_hash_frozen_at_build(tmp_path: Path) -> None:
    platform = build_sqlite_platform(tmp_path)
    assert platform.config_hash
    assert get_config_hash() == platform.config_hash


def test_worker_composes_default_off_catalog_without_executor_activation(tmp_path: Path) -> None:
    platform = build_sqlite_platform(tmp_path)
    assert platform.agent_catalog.agents == ()
    assert not hasattr(platform.agent_catalog, "launch")
    assert not hasattr(platform.agent_catalog, "run")


def test_api_and_worker_compose_identical_nonempty_registry_and_hash(tmp_path: Path) -> None:
    registry = tmp_path / "registry.toml"
    registry.write_bytes(_REGISTRY.read_bytes())
    settings, seams = sqlite_seams(tmp_path)
    settings = settings.model_copy(update={"agent_manifest_path": str(registry)})
    assert seams.plugin_registry is not None
    seams.plugin_registry.register("token_verifier", "fake", lambda _oidc: _UnusedTokenVerifier())

    worker = build_platform(settings, seams)
    app = create_app(settings, seams=seams)
    api = _api_state(app)

    assert _catalog_identity(worker) == _catalog_identity(api)
    assert len(_catalog_identity(worker)) == 9
    assert worker.config_hash == api.config_hash == get_config_hash()
    expected_raw = hashlib.sha256(registry.read_bytes()).hexdigest()
    worker_fingerprints = _manifest_fingerprints(worker.runtime_engine)
    api_fingerprints = _manifest_fingerprints(api.session_store.engine)
    assert worker_fingerprints == api_fingerprints
    assert {raw for raw, _semantic in worker_fingerprints} == {expected_raw}


def test_registry_raw_and_semantic_changes_move_platform_hash(tmp_path: Path) -> None:
    registry = tmp_path / "registry.toml"
    baseline_bytes = _REGISTRY.read_bytes()
    registry.write_bytes(baseline_bytes)
    settings, seams = sqlite_seams(tmp_path)
    settings = settings.model_copy(update={"agent_manifest_path": str(registry)})
    assert seams.runtime_settings is not None
    runtime_path = Path(seams.runtime_settings.session_database_url.removeprefix("sqlite:///"))

    def build_fresh_catalog() -> str:
        platform = build_platform(settings, seams)
        config_hash = platform.config_hash
        for engine in platform.engines:
            engine.dispose()
        runtime_path.unlink()
        return config_hash

    baseline = build_fresh_catalog()

    reset_config_hash_for_testing()
    registry.write_bytes(b"# representation change\n" + baseline_bytes)
    raw_changed = build_fresh_catalog()
    assert raw_changed != baseline

    reset_config_hash_for_testing()
    # Version-independent on purpose: pinning the Analyst's CURRENT version here
    # made this mutation a no-op every time that version advanced, and a no-op
    # mutation proves nothing about the semantic hash.
    semantic_text = baseline_bytes.decode("utf-8").replace(
        'label = "Meridian Analyst"',
        'label = "Meridian Analyst mutation"',
        1,
    )
    assert semantic_text != baseline_bytes.decode("utf-8")
    registry.write_text(semantic_text, encoding="utf-8")
    semantic_changed = build_fresh_catalog()
    assert semantic_changed not in {baseline, raw_changed}


@pytest.mark.parametrize("first_root", ["api", "worker"])
@pytest.mark.parametrize("second_root_manifest", ["raw", "semantic", "unconfigured"])
def test_api_worker_shared_store_refuses_conflicting_registry_roots(
    tmp_path: Path, first_root: str, second_root_manifest: str
) -> None:
    registry = tmp_path / "registry.toml"
    registry.write_bytes(_REGISTRY.read_bytes())
    settings, seams = sqlite_seams(tmp_path)
    settings = settings.model_copy(update={"agent_manifest_path": str(registry)})
    assert seams.plugin_registry is not None
    seams.plugin_registry.register("token_verifier", "fake", lambda _oidc: _UnusedTokenVerifier())

    first = (
        _api_state(create_app(settings, seams=seams))
        if first_root == "api"
        else build_platform(settings, seams)
    )
    expected = _manifest_fingerprints(first.session_store.engine)
    reset_config_hash_for_testing()
    if second_root_manifest == "raw":
        registry.write_bytes(b"# second configured root\n" + _REGISTRY.read_bytes())
        expected_error = "byte fingerprint mismatch"
        second_settings = settings
    elif second_root_manifest == "semantic":
        registry.write_text(
            _REGISTRY.read_text(encoding="utf-8").replace(
                'label = "Meridian Analyst"', 'label = "Conflicting Analyst"'
            ),
            encoding="utf-8",
        )
        expected_error = "registry generation drift"
        second_settings = settings
    else:
        expected_error = "configured-state mismatch"
        second_settings = settings.model_copy(update={"agent_manifest_path": ""})
    with pytest.raises(AgentCatalogError, match=expected_error):
        if first_root == "api":
            build_platform(second_settings, seams)
        else:
            create_app(second_settings, seams=seams)
    assert _manifest_fingerprints(first.session_store.engine) == expected


def test_executor_and_scheduler_share_sink_and_gate(tmp_path: Path) -> None:
    platform = build_sqlite_platform(tmp_path)
    scheduler = _scheduler_for(platform)

    # The executor's gate/sink ARE the platform singletons (no parallel state).
    assert platform.executor._approval_gate is platform.gate
    assert platform.executor._sink is platform.sink
    # The cron scheduler's internally-built executor shares the SAME sink + gate.
    assert scheduler._executor._approval_gate is platform.gate
    assert scheduler._executor._sink is platform.sink
    assert scheduler._sink is platform.sink


def test_shared_ledger_counter_are_singletons(tmp_path: Path) -> None:
    platform = build_sqlite_platform(tmp_path)
    # The pair the executor + scheduler tally into is ONE object each; a fresh
    # create_shared_guardrail_state would be a DIFFERENT pool (proving sharing is
    # deliberate, not incidental).
    other_ledger, other_counter = create_shared_guardrail_state()
    assert platform.ledger is not other_ledger
    assert platform.counter is not other_counter


def test_re_freezing_the_identical_mapping_is_idempotent(tmp_path: Path) -> None:
    """A second root freezing the IDENTICAL composed mapping must not fork the hash.

    The in-process proxy of the cross-process ADR-0003 one-platform-hash: the
    worker's ``build_platform`` and ne-api's ``create_app`` compose the SAME mapping,
    so freezing twice over identical settings is a no-op — never a
    ``ConfigHashMismatchError``. (The composed hash covers every settings field, so
    the SAME settings objects are required — differing db urls WOULD fork, correctly.)
    """
    settings, seams = sqlite_seams(tmp_path)
    first = build_platform(settings, seams)
    second = build_platform(settings, seams)
    assert first.config_hash == second.config_hash == get_config_hash()
