# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE unit headline: the ACL permission trap over the REAL PrincipalExpander.

Mirrors the ne-brain-graph D2 provable-trimming discipline: a real
``PrincipalStore``/``PrincipalExpander`` proves principal A cannot see a
team-B-labelled skill via search, view (no oracle), OR the active set; B can;
``()``-labelled is global; ``TENANT_ALL`` is hidden from store-unknown principals;
and an expander raise propagates (deny-by-erroring) from BOTH tools.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.contracts.types import Principal
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import TENANT_ALL, PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore, ensure_permissions_schema
from ne.skills.active_set import ActiveSetComputer
from ne.skills.errors import SkillNotFoundError
from ne.skills.registry import SqlAlchemySkillRegistry, ensure_skills_schema
from ne.skills.tools import VIEW_TOOL_NAME, build_search_handler, build_view_handler
from ne.skills.visibility import GroupExpander

from .conftest import make_pack

NOW = datetime(2026, 7, 20, tzinfo=UTC)
A = Principal(id="u-a")
B = Principal(id="u-b")
GHOST = Principal(id="u-ghost")
GRANTED = frozenset({VIEW_TOOL_NAME})


class _Env:
    def __init__(
        self, registry: SqlAlchemySkillRegistry, expander: PrincipalExpander, settings
    ) -> None:
        self.registry = registry
        self.expander = expander
        self.settings = settings


@pytest.fixture
def env(tmp_path: Path, settings) -> _Env:
    perm_engine = sa.create_engine(f"sqlite:///{tmp_path / 'perms.db'}")
    ensure_permissions_schema(perm_engine)
    pstore = PrincipalStore(perm_engine)
    pstore.upsert_principals(
        [
            SyncedPrincipal(principal_id="u-a", kind=PrincipalKind.USER),
            SyncedPrincipal(principal_id="u-b", kind=PrincipalKind.USER),
        ],
        source="test",
        now=NOW,
    )
    pstore.set_member_groups("u-a", ["grp:team-a"], source="test", now=NOW)
    pstore.set_member_groups("u-b", ["grp:team-b"], source="test", now=NOW)
    expander = PrincipalExpander(pstore)

    skills_engine = sa.create_engine(f"sqlite:///{tmp_path / 'skills.db'}")
    ensure_skills_schema(skills_engine)
    registry = SqlAlchemySkillRegistry(skills_engine)
    registry.register_pack(
        make_pack("team-b-skill", triggers=("secret handshake",), acl_labels=("grp:team-b",)),
        origin="bundle",
    )
    registry.register_pack(make_pack("global-skill", acl_labels=()), origin="bundle")
    registry.register_pack(
        make_pack("tenant-skill", triggers=("everyone all",), acl_labels=(TENANT_ALL,)),
        origin="bundle",
    )
    return _Env(registry, expander, settings)


def _search(env: _Env, principal: Principal) -> str:
    handler = build_search_handler(env.registry, env.expander, env.settings, lambda: principal)
    return asyncio.run(handler({"query": "secret handshake"}))


def _view(env: _Env, principal: Principal, name: str) -> str:
    handler = build_view_handler(env.registry, env.expander, env.settings, lambda: principal)
    return asyncio.run(handler({"name": name}))


def _active(env: _Env, principal: Principal) -> set[str]:
    computer = ActiveSetComputer(env.registry, env.expander, env.settings)
    return {p.name for p in computer.compute(principal, granted_tools=GRANTED, substrate_id="x")}


def test_search_trap(env: _Env) -> None:
    assert _search(env, A) == "no skills matched"
    assert "team-b-skill" in _search(env, B)


def test_view_no_oracle(env: _Env) -> None:
    with pytest.raises(SkillNotFoundError) as invisible:
        _view(env, A, "team-b-skill")
    with pytest.raises(SkillNotFoundError) as absent:
        _view(env, A, "nonexistent-skill")
    # Both are the SAME error type + message FORMAT (only the name differs) — an
    # invisible skill is indistinguishable from a nonexistent one (no oracle).
    assert str(invisible.value) == "skill not found: team-b-skill"
    assert str(absent.value) == "skill not found: nonexistent-skill"


def test_view_visible_for_owner(env: _Env) -> None:
    body = _view(env, B, "team-b-skill")
    assert body.startswith("# team-b-skill")


def test_active_set_trap(env: _Env) -> None:
    assert "team-b-skill" not in _active(env, A)
    assert "team-b-skill" in _active(env, B)


def test_global_visible_to_both(env: _Env) -> None:
    assert "global-skill" in _active(env, A)
    assert "global-skill" in _active(env, B)


def test_tenant_all_hidden_from_unknown(env: _Env) -> None:
    # store-known A/B see TENANT_ALL; store-unknown GHOST does not.
    assert "tenant-skill" in _active(env, A)
    assert "tenant-skill" not in _active(env, GHOST)
    # but a ()-labelled (global) skill remains visible even to GHOST.
    assert "global-skill" in _active(env, GHOST)


def test_expander_raise_propagates(env: _Env) -> None:
    class _Boom:
        def expand(self, principal: Principal | str) -> frozenset[str]:
            raise RuntimeError("expander down")

    boom: GroupExpander = _Boom()
    search = build_search_handler(env.registry, boom, env.settings, lambda: A)
    view = build_view_handler(env.registry, boom, env.settings, lambda: A)
    with pytest.raises(RuntimeError):
        asyncio.run(search({"query": "secret handshake"}))
    with pytest.raises(RuntimeError):
        asyncio.run(view({"name": "global-skill"}))
