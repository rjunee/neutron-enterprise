# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-009A A3: the worker inherits the API's Agent Run recipe, byte for byte.

One recipe, two processes. The worker must NOT compose its own engine, schema,
cipher or readiness check -- that is precisely how two roots come to disagree
about which key is current, with the worker happily minting rows the API cannot
open. These controls prove the worker composes only through `build_platform` and
receives an identical secret reference.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import ne.worker.main as _worker_main
from ne.api.platform import (
    AGENT_PRIVATE_PAYLOAD_KEK_ENV,
    AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV,
    build_platform,
)

_source = inspect.getsourcefile(_worker_main)
assert _source is not None
WORKER_MAIN = Path(_source)


def test_the_worker_never_composes_its_own_agent_run_store() -> None:
    """No private engine/schema/cipher construction anywhere in the worker."""
    source = WORKER_MAIN.read_text(encoding="utf-8")
    for forbidden in (
        "AgentRunStore(",
        "ensure_run_schema(",
        "RunPrivatePayloadCipher(",
        "StaticPrivatePayloadKeys(",
        "_compose_agent_run_store(",
    ):
        assert forbidden not in source, forbidden
    # Positive control: it DOES compose through the shared recipe, so the absence
    # above is inheritance rather than the feature being missing entirely.
    assert "build_platform(api_settings, seams)" in source


def test_the_worker_never_reads_the_key_environment_itself() -> None:
    """Both roots take the key from ONE place: build_platform, after freeze."""
    source = WORKER_MAIN.read_text(encoding="utf-8")
    assert AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV not in source
    assert AGENT_PRIVATE_PAYLOAD_KEK_ENV not in source


def test_both_roots_share_one_composition_entry_point() -> None:
    """`build_platform` is the single recipe, so API/worker parity is structural."""
    tree = ast.parse(WORKER_MAIN.read_text(encoding="utf-8"))
    imported_from = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and any(alias.name == "build_platform" for alias in node.names)
    }
    assert imported_from == {"ne.api.platform"}
    assert "agent_run_store" in inspect.getsource(build_platform)


def test_the_worker_consumes_the_composed_store_from_the_shared_platform_field() -> None:
    """Requirement 7's worker half: a matching key must compose for BOTH roots.

    The sibling controls prove the worker never composes its OWN store. That alone
    is satisfied by a worker that ignores Agent Run storage entirely -- absence and
    inheritance are indistinguishable from the outside. This asserts the positive
    half: the worker actually READS `platform.agent_run_store`, the single field
    `build_platform` populates, so the store the API composed is the store the
    worker has.
    """
    tree = ast.parse(WORKER_MAIN.read_text(encoding="utf-8"))
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "agent_run_store"
        and isinstance(node.value, ast.Name)
        and node.value.id == "platform"
    ]
    assert reads, "the worker never reads platform.agent_run_store"
    # And it reads the SAME field build_platform assigns -- not a lookalike.
    assert "agent_run_store=agent_run_store" in inspect.getsource(build_platform)


def test_canonical_run_is_a_registered_dbos_workflow_not_a_process_local_task() -> None:
    """PRE-010B: the request path can enqueue only the named worker workflow."""

    import ne.worker.workflows as workflows

    source = inspect.getsource(workflows)
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    run = functions["run_agent_run_workflow"]
    step = functions["execute_agent_run_step"]
    assert any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "ne_workflow"
        and any(
            keyword.arg == "name"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == "agent.run"
            for keyword in decorator.keywords
        )
        for decorator in run.decorator_list
    )
    assert any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "ne_step"
        for decorator in step.decorator_list
    )
    assert "asyncio.create_task" not in inspect.getsource(workflows.run_agent_run_workflow)
