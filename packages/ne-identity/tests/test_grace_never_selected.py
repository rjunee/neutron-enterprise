# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GRACE exclusion — a rotated-out credential is NEVER handed to a NEW selection.

Codex P2: ``select_credential`` could hand an old GRACE credential (possibly
broader-scoped after a narrowing rotation) to a NEW dispatch under
``round_robin``/``random`` (and ``least_used`` when the ACTIVE cred was more
used). New selections now draw ONLY from ACTIVE credentials across all four
strategies; GRACE stays valid solely for in-flight holders (``validate``/
``resolve_secret`` by id), and with no ACTIVE credential the selection fails
closed with ``CredentialNotFoundError`` — never a GRACE fallback.
"""

from __future__ import annotations

import pytest

from ne.identity.agents import (
    AgentCredentialConfig,
    AgentCredentialService,
    CredentialScope,
    CredentialState,
)
from ne.identity.errors import CredentialExpiredError, CredentialNotFoundError

from .conftest import (
    ListSink,
    MutableClock,
    run,
    sample_agent,
    sample_scope,
)


def _service(
    strategy: str,
    agent_store: object,
    agent_sink: ListSink,
    agent_clock: MutableClock,
) -> AgentCredentialService:
    return AgentCredentialService(
        agent_store,  # type: ignore[arg-type]
        agent_sink,
        AgentCredentialConfig(
            strategy=strategy,  # type: ignore[arg-type]
            grace_seconds=300.0,
            rotate_after_seconds=86400.0,
        ),
        clock=agent_clock,
    )


def test_round_robin_never_selects_grace_after_narrowing_rotation(
    agent_store: object,
    agent_sink: ListSink,
    agent_clock: MutableClock,
    frozen_agent_config_hash: str,
) -> None:
    svc = _service("round_robin", agent_store, agent_sink, agent_clock)
    agent = sample_agent()
    old = run(svc.issue(agent, sample_scope("read.notes", "read.docs"), actor="op"))
    new = run(svc.rotate(agent, actor="op", scope=CredentialScope(tools=("read.notes",))))
    for _ in range(20):
        selected = svc.select_credential(agent)
        assert selected.credential_id == new.credential_id
        assert selected.credential_id != old.credential_id
        assert selected.scope.tools == ("read.notes",)
        assert "read.docs" not in selected.scope.tools


def test_random_never_selects_grace_after_narrowing_rotation(
    agent_store: object,
    agent_sink: ListSink,
    agent_clock: MutableClock,
    frozen_agent_config_hash: str,
) -> None:
    svc = _service("random", agent_store, agent_sink, agent_clock)
    agent = sample_agent()
    old = run(svc.issue(agent, sample_scope("read.notes", "read.docs"), actor="op"))
    new = run(svc.rotate(agent, actor="op", scope=CredentialScope(tools=("read.notes",))))
    for _ in range(50):
        selected = svc.select_credential(agent)
        assert selected.credential_id == new.credential_id
        assert selected.credential_id != old.credential_id
        assert selected.scope.tools == ("read.notes",)
        assert "read.docs" not in selected.scope.tools


def test_fill_first_never_selects_grace_after_narrowing_rotation(
    agent_store: object,
    agent_sink: ListSink,
    agent_clock: MutableClock,
    frozen_agent_config_hash: str,
) -> None:
    svc = _service("fill_first", agent_store, agent_sink, agent_clock)
    agent = sample_agent()
    old = run(svc.issue(agent, sample_scope("read.notes", "read.docs"), actor="op"))
    new = run(svc.rotate(agent, actor="op", scope=CredentialScope(tools=("read.notes",))))
    for _ in range(5):
        selected = svc.select_credential(agent)
        assert selected.credential_id == new.credential_id
        assert selected.credential_id != old.credential_id
        assert selected.scope.tools == ("read.notes",)
        assert "read.docs" not in selected.scope.tools


def test_least_used_never_selects_grace_even_when_active_more_used(
    agent_store: object,
    agent_sink: ListSink,
    agent_clock: MutableClock,
    frozen_agent_config_hash: str,
) -> None:
    svc = _service("least_used", agent_store, agent_sink, agent_clock)
    agent = sample_agent()
    old = run(svc.issue(agent, sample_scope("read.notes", "read.docs"), actor="op"))
    new = run(svc.rotate(agent, actor="op", scope=CredentialScope(tools=("read.notes",))))
    # Bump the ACTIVE entry's use_count above the GRACE entry's 0. Pre-fix,
    # least_used would then deterministically pick the (less-used) GRACE entry.
    for _ in range(3):
        assert svc.select_credential(agent).credential_id == new.credential_id
    for _ in range(3):
        selected = svc.select_credential(agent)
        assert selected.credential_id == new.credential_id
        assert selected.credential_id != old.credential_id
        assert selected.scope.tools == ("read.notes",)
        assert "read.docs" not in selected.scope.tools


def test_no_active_credential_fails_closed_not_grace_fallback(
    agent_store: object,
    agent_sink: ListSink,
    agent_clock: MutableClock,
    frozen_agent_config_hash: str,
) -> None:
    svc = _service("round_robin", agent_store, agent_sink, agent_clock)
    agent = sample_agent()
    old = run(svc.issue(agent, sample_scope("read.notes", "read.docs"), actor="op"))
    new = run(svc.rotate(agent, actor="op", scope=CredentialScope(tools=("read.notes",))))
    # Revoke the ACTIVE cred so ONLY the GRACE cred is live.
    run(svc.revoke(new.credential_id, actor="op"))
    with pytest.raises(CredentialNotFoundError):
        svc.select_credential(agent)
    # The in-flight holder of the GRACE cred is unaffected by the fail-closed
    # new-selection path.
    assert svc.validate(old.credential_id).state is CredentialState.GRACE


def test_active_cooling_does_not_fall_back_to_grace(
    agent_store: object,
    agent_sink: ListSink,
    agent_clock: MutableClock,
    frozen_agent_config_hash: str,
) -> None:
    svc = _service("round_robin", agent_store, agent_sink, agent_clock)
    agent = sample_agent()
    old = run(svc.issue(agent, sample_scope("read.notes", "read.docs"), actor="op"))
    new = run(svc.rotate(agent, actor="op", scope=CredentialScope(tools=("read.notes",))))
    # Park the ACTIVE cred (402 billing => 1800s cooldown). GRACE availability
    # must NOT satisfy the selection.
    svc.report_failure(new.credential_id, 402)
    with pytest.raises(CredentialNotFoundError):
        svc.select_credential(agent)
    assert svc.validate(old.credential_id).state is CredentialState.GRACE


def test_grace_still_validates_for_existing_holder_while_new_selects_get_active(
    agent_store: object,
    agent_sink: ListSink,
    agent_clock: MutableClock,
    frozen_agent_config_hash: str,
) -> None:
    svc = _service("round_robin", agent_store, agent_sink, agent_clock)
    agent = sample_agent()
    old = run(svc.issue(agent, sample_scope("read.notes", "read.docs"), actor="op"))
    new = run(svc.rotate(agent, actor="op", scope=CredentialScope(tools=("read.notes",))))
    # New selections get ACTIVE; the GRACE holder still validates + resolves.
    assert svc.select_credential(agent).credential_id == new.credential_id
    assert run(svc.resolve_secret(old.credential_id)).get_secret_value()
    assert svc.validate(old.credential_id).state is CredentialState.GRACE
    # The grace window still closes on schedule.
    agent_clock.advance(301.0)
    with pytest.raises(CredentialExpiredError):
        svc.validate(old.credential_id)
