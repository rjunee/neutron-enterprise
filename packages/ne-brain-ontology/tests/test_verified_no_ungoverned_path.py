# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085c: no ungoverned path — verified/ imports no runtime/govern, exposes no raw SPARQL."""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from ne.brain_ontology.verified import build_verified_answer_tools, load_template_registry

from .verified_helpers import (
    FIXTURES_DIR,
    RecordingStore,
    StubExpander,
    account,
    make_gateway,
    seed_accounts,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "ne" / "brain_ontology"


def test_importing_verified_pulls_no_runtime_or_govern() -> None:
    # A CLEAN interpreter: importing the verified layer must NOT drag in ne.runtime
    # or ne.govern (the brain-ontology import lane, C3). Run in a subprocess so a
    # prior test that imported those modules cannot mask the violation.
    code = (
        "import sys; import ne.brain_ontology.verified as v; "
        "assert 'ne.runtime' not in sys.modules, 'ne.runtime leaked'; "
        "assert 'ne.govern' not in sys.modules, 'ne.govern leaked'; "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_toolbinding_token_absent_from_package_src() -> None:
    # The ToolBinding/ToolArgPolicy conversion lives ONLY in the ne-api seam.
    for path in _SRC.rglob("*.py"):
        assert "ToolBinding" not in path.read_text(encoding="utf-8"), path


def test_factory_takes_a_registry_not_raw_sparql() -> None:
    params = inspect.signature(build_verified_answer_tools).parameters
    assert "registry" in params
    # No free-form-SPARQL parameter exists anywhere on the public factory surface.
    assert not {"sparql", "query", "query_text"} & set(params)


def test_handler_with_no_principal_fails_before_store_touch() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_alpha", ["alpha"])])
        rec = RecordingStore(gw)
        reg = load_template_registry(FIXTURES_DIR)
        from ne.brain_ontology.config import OntologySettings

        defs = build_verified_answer_tools(
            registry=reg,
            store=rec,
            expander=StubExpander(),
            principal_provider=lambda: None,  # unbound principal → fail closed
            settings=OntologySettings(verified_max_rows=500),
            config_hash="a" * 64,
        )
        directory = next(d for d in defs if d.spec.name == "read.kg.accounts_directory")
        with pytest.raises(RuntimeError, match="no turn principal"):
            await directory.handler({})
        assert rec.scoped_calls == 0

    asyncio.run(scenario())
