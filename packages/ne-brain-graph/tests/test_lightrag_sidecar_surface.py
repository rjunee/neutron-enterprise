# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Static guard cited by the four lightrag-hku VEX statements in
``tools/supply-chain/vex/openvex.json`` (CVE-2026-85734 / GHSA-frch-4w6v-q5xx and
CVE-2026-85740 / GHSA-vv3m-f8x4-7377).

Those statements claim the vulnerable code (the bundled API server's ``/login``
handler and the native-markdown parser's image-download guard) is outside the
sidecar's execute path. This file proves only what each test name says, by
PARSING every ``*.py`` file under ``lightrag_env/`` (``ast``, discovered by walking
the directory, so a new or split-out sidecar module is covered the moment it
exists), the first-party Python tree (which files import ``lightrag``) and the
hash-pinned lockfile (per-line pin regex), never by substring-matching prose.
It does not prove non-exploitability and does not execute the sidecar; the live
functional test is ``test_lightrag_sidecar.py`` (LIVE-PARKED until the venv is
built).

Residuals a static guard cannot close: a dynamic import whose module name is
computed at run time (``importlib.import_module("lightrag." + "api")``) is not
seen by the import walk, and the lockfile parser recognises only ``name==``
pin lines, so a direct-reference requirement (``starlette @ https://...``) would
not be seen by the fastapi/uvicorn/starlette check.

The ``only_need_context`` guard binds the flag to every *direct* ``aquery`` call:
each must pass a plain name that its own function scope binds only to a
``QueryParam(...)`` call carrying the literal keyword ``only_need_context=True``,
with no attribute store on that name. A ``QueryParam`` built via a computed name
(``getattr(lightrag, "QueryParam")(...)``) or with the flag only inside
``**kwargs`` is REJECTED (fail closed), not accepted. What still evades it:
``QueryParam`` rebound to a different callable; the flag cleared through a
helper or ``setattr(param, ...)`` rather than an attribute-store statement; an
``aquery`` reached by alias or string dispatch (``q = rag.aquery``,
``getattr(rag, "aquery")``) rather than called directly; and the other LightRAG
query entrypoints (sync ``query``, ``aquery_llm``/``aquery_data`` and the like),
which this guard does not look for.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

_ENV_DIR = Path(__file__).resolve().parents[1] / "lightrag_env"


def _env_scripts() -> tuple[str, ...]:
    """Every ``*.py`` under ``lightrag_env/`` (recursive), as sorted relative paths."""
    return tuple(
        sorted(
            p.relative_to(_ENV_DIR).as_posix()
            for p in _ENV_DIR.rglob("*.py")
            if "__pycache__" not in p.parts
        )
    )


_SCRIPTS = _env_scripts()


def _repo_root() -> Path:
    for candidate in (Path(__file__).resolve(), *Path(__file__).resolve().parents):
        if (candidate / "uv.lock").is_file():
            return candidate
    raise AssertionError("could not locate repo-root uv.lock")


_ROOT = _repo_root()
_LEDGER = _ROOT / "tools" / "supply-chain" / "vex" / "openvex.json"

# First-party Python trees walked for ``lightrag`` importers; environment and
# vendored directories are not first-party code.
_FIRST_PARTY_TREES = ("packages", "tools", "apps")
_NON_FIRST_PARTY_PARTS = frozenset(
    {".venv", ".lightrag-venv", "node_modules", "__pycache__", ".git", "site-packages"}
)

_FORBIDDEN_MODULE_ROOTS = ("lightrag.api", "lightrag.parser")

# Exact-name match on the called attribute/function: ``ainsert_custom_kg`` (the
# custom-KG path the sidecar uses) is NOT ``ainsert`` (the document pipeline).
_FORBIDDEN_CALLS = frozenset(
    {"ainsert", "insert", "apipeline_enqueue_documents", "apipeline_process_enqueue_documents"}
)

_SERVER_CLOSURE = frozenset({"fastapi", "uvicorn", "starlette"})

_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", re.MULTILINE)

_LIGHTRAG_IDS = {
    "CVE-2026-85734": "GHSA-frch-4w6v-q5xx",
    "GHSA-frch-4w6v-q5xx": "CVE-2026-85734",
    "CVE-2026-85740": "GHSA-vv3m-f8x4-7377",
    "GHSA-vv3m-f8x4-7377": "CVE-2026-85740",
}


# ---------------------------------------------------------------------------
# Pure helpers (fixture-drivable)
# ---------------------------------------------------------------------------
def _imported_modules(tree: ast.Module) -> set[str]:
    """Every ``import x`` name and every ``from m import a`` as ``m`` and ``m.a``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _string_constants(tree: ast.Module) -> set[str]:
    """Every str literal (catches ``importlib.import_module("lightrag.api")``)."""
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _forbidden_imports(tree: ast.Module) -> set[str]:
    return {
        name
        for name in _imported_modules(tree) | _string_constants(tree)
        if any(name == root or name.startswith(f"{root}.") for root in _FORBIDDEN_MODULE_ROOTS)
    }


def _called_names(tree: ast.Module) -> set[str]:
    """Direct call targets: ``func.attr`` for ``x.f(...)``, ``func.id`` for ``f(...)``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                names.add(node.func.id)
    return names


def _referenced_names(tree: ast.Module) -> set[str]:
    """Every ``ast.Attribute.attr`` and every ``ast.Name.id``, called or not.

    This sees a method passed by reference (``ingest = rag.ainsert``,
    ``functools.partial(rag.ainsert, ...)``, ``run_in_executor(None, rag.insert, ...)``,
    ``for fn in (rag.ainsert,)``), which a call-target-only walk misses.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def _forbidden_calls(tree: ast.Module) -> set[str]:
    """Forbidden names that are called, referenced as an attribute/name, OR appear
    as a str literal.

    The reference half catches a bound method passed or aliased before it is
    called; the literal half catches string dispatch such as
    ``getattr(rag, "ainsert")(...)``. Both over-approximate (any ``.insert``
    attribute or bare ``"insert"`` string trips it), which is the fail-closed
    direction for a guard a VEX ``not_affected`` statement relies on.
    """
    return (
        _called_names(tree) | _referenced_names(tree) | _string_constants(tree)
    ) & _FORBIDDEN_CALLS


def _imports_lightrag(tree: ast.Module) -> bool:
    return any(
        name == "lightrag" or name.startswith("lightrag.") for name in _imported_modules(tree)
    )


def _pinned_names(text: str) -> set[str]:
    """PEP 503-normalised names of every ``name==version`` pin line."""
    return {re.sub(r"[-_.]+", "-", name).lower() for name in _PIN_RE.findall(text)}


_FunctionScope = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


def _scope_nodes(scope: _FunctionScope) -> list[ast.AST]:
    """Every node whose innermost enclosing function scope is ``scope``."""
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        out.append(node)
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            stack.extend(ast.iter_child_nodes(node))
    return out


def _is_context_only_query_param(value: ast.expr) -> bool:
    """``QueryParam(..., only_need_context=True, ...)`` with a literal ``True``."""
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "QueryParam"
    ):
        return False
    flags = [kw.value for kw in value.keywords if kw.arg == "only_need_context"]
    return len(flags) == 1 and isinstance(flags[0], ast.Constant) and flags[0].value is True


def _aquery_param_findings(tree: ast.Module) -> tuple[int, list[str]]:
    """(number of direct ``aquery`` calls, list of violations).

    A call complies when its ``param`` (keyword, or second positional argument)
    is a plain name that its own function scope binds at least once, every
    binding being ``name = QueryParam(..., only_need_context=True)``, and no
    ``name.attr = ...`` store appears anywhere in that scope.
    """
    scopes: list[_FunctionScope] = [tree]
    scopes.extend(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
    )
    count = 0
    violations: list[str] = []
    for scope in scopes:
        nodes = _scope_nodes(scope)
        calls = [
            n
            for n in nodes
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "aquery"
        ]
        for call in calls:
            count += 1
            where = f"aquery@line{call.lineno}"
            param_kw = [kw.value for kw in call.keywords if kw.arg == "param"]
            if param_kw:
                param = param_kw[0]
            elif len(call.args) >= 2:
                param = call.args[1]
            else:
                violations.append(f"{where}: no param")
                continue
            if not isinstance(param, ast.Name):
                violations.append(f"{where}: param is not a plain name")
                continue
            name = param.id
            args = scope.args if not isinstance(scope, ast.Module) else None
            if args is not None and name in {
                a.arg
                for a in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg)
                if a is not None
            }:
                violations.append(f"{where}: param {name!r} is a function argument")
                continue
            stores = [
                n
                for n in nodes
                if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store)
            ]
            good = [
                n
                for n in nodes
                if isinstance(n, ast.Assign)
                and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id == name
                and _is_context_only_query_param(n.value)
            ]
            if not stores:
                violations.append(f"{where}: param {name!r} is not bound in its scope")
            elif len(stores) != len(good):
                violations.append(
                    f"{where}: param {name!r} is bound to something other than "
                    "QueryParam(only_need_context=True)"
                )
            if any(
                isinstance(n, ast.Attribute)
                and isinstance(n.ctx, ast.Store | ast.Del)
                and isinstance(n.value, ast.Name)
                and n.value.id == name
                for n in nodes
            ):
                violations.append(f"{where}: param {name!r} has an attribute store")
    return count, violations


def _script_tree(script: str) -> ast.Module:
    return ast.parse((_ENV_DIR / script).read_text(encoding="utf-8"), filename=script)


# ---------------------------------------------------------------------------
# The shipped sidecar scripts
# ---------------------------------------------------------------------------
def test_lightrag_env_walk_discovers_project_py_and_query_py() -> None:
    """Positive control: the directory walk the two guards below iterate over is
    not empty and includes both scripts the sidecar runs."""
    assert {"project.py", "query.py"} <= set(_SCRIPTS)


@pytest.mark.parametrize("script", _SCRIPTS, ids=list(_SCRIPTS))
def test_lightrag_env_python_files_import_nothing_from_lightrag_api_or_lightrag_parser(
    script: str,
) -> None:
    assert _forbidden_imports(_script_tree(script)) == set()


@pytest.mark.parametrize("script", _SCRIPTS, ids=list(_SCRIPTS))
def test_lightrag_env_python_files_neither_call_nor_name_ainsert_insert_or_the_enqueue_pipeline(
    script: str,
) -> None:
    assert _forbidden_calls(_script_tree(script)) == set()


def _first_party_lightrag_importers() -> set[str]:
    importers: set[str] = set()
    for tree_name in _FIRST_PARTY_TREES:
        base = _ROOT / tree_name
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(_ROOT)
            if _NON_FIRST_PARTY_PARTS & set(rel.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "lightrag" not in text:  # cheap prefilter; the AST decides
                continue
            if _imports_lightrag(ast.parse(text, filename=str(rel))):
                importers.add(rel.as_posix())
    return importers


def test_only_lightrag_env_project_py_and_query_py_import_lightrag_in_packages_tools_and_apps() -> (
    None
):
    assert _first_party_lightrag_importers() == {
        "packages/ne-brain-graph/lightrag_env/project.py",
        "packages/ne-brain-graph/lightrag_env/query.py",
    }


def test_ast_walker_sees_the_scripts_real_imports_and_calls() -> None:
    """Positive control: the walkers above are not blind to these scripts."""
    project = _script_tree("project.py")
    assert {"lightrag", "lightrag.utils", "lightrag.kg.shared_storage"} <= _imported_modules(
        project
    )
    assert {"ainsert_custom_kg", "initialize_storages", "finalize_storages"} <= _called_names(
        project
    )

    query = _script_tree("query.py")
    assert "lightrag" in _imported_modules(query)
    assert {"aquery", "QueryParam"} <= _called_names(query)


def test_query_py_passes_a_QueryParam_with_only_need_context_true_to_every_aquery_call() -> None:
    count, violations = _aquery_param_findings(_script_tree("query.py"))
    assert violations == []
    # Non-vacuity: a query.py with no aquery call at all must not pass silently.
    assert count >= 1


# (case, source, expected_violation_count); every fixture has exactly one aquery call.
_AQUERY_FIXTURES: list[tuple[str, str, int]] = [
    (
        "param-dropped",
        "async def f(rag, q):\n"
        "    param = QueryParam(mode='local', only_need_context=True)\n"
        "    return await rag.aquery(q)\n",
        1,
    ),
    (
        "param-unbound-other",
        "async def f(rag, q):\n"
        "    param = QueryParam(only_need_context=True)\n"
        "    other = dict(mode='local')\n"
        "    return await rag.aquery(q, param=other)\n",
        1,
    ),
    (
        "param-renamed-in-call-only",
        "async def f(rag, q):\n"
        "    param = QueryParam(only_need_context=True)\n"
        "    return await rag.aquery(q, param=params)\n",
        1,
    ),
    (
        "flag-false",
        "async def f(rag, q):\n"
        "    param = QueryParam(only_need_context=False)\n"
        "    return await rag.aquery(q, param=param)\n",
        1,
    ),
    (
        "flag-non-literal",
        "async def f(rag, q, flag):\n"
        "    param = QueryParam(only_need_context=flag)\n"
        "    return await rag.aquery(q, param=param)\n",
        1,
    ),
    (
        "flag-only-in-kwargs",
        "async def f(rag, q):\n"
        "    param = QueryParam(**{'only_need_context': True})\n"
        "    return await rag.aquery(q, param=param)\n",
        1,
    ),
    (
        "computed-name-QueryParam",
        "import lightrag\n"
        "async def f(rag, q):\n"
        "    param = getattr(lightrag, 'QueryParam')(only_need_context=True)\n"
        "    return await rag.aquery(q, param=param)\n",
        1,
    ),
    (
        "rebound-after-construction",
        "async def f(rag, q):\n"
        "    param = QueryParam(only_need_context=True)\n"
        "    param = QueryParam()\n"
        "    return await rag.aquery(q, param=param)\n",
        1,
    ),
    (
        "flag-cleared-by-attribute-store",
        "async def f(rag, q):\n"
        "    param = QueryParam(only_need_context=True)\n"
        "    param.only_need_context = False\n"
        "    return await rag.aquery(q, param=param)\n",
        1,
    ),
    (
        "bound-in-another-scope",
        "param = QueryParam(only_need_context=True)\n"
        "async def f(rag, q):\n"
        "    return await rag.aquery(q, param=param)\n",
        1,
    ),
    (
        "param-is-a-function-argument",
        "async def f(rag, q, param):\n    return await rag.aquery(q, param=param)\n",
        1,
    ),
    (
        "control-keyword",
        "async def f(rag, q):\n"
        "    param = QueryParam(mode='local', only_need_context=True, top_k=5)\n"
        "    return await rag.aquery(q, param=param)\n",
        0,
    ),
    (
        "control-positional",
        "async def f(rag, q):\n"
        "    param = QueryParam(only_need_context=True)\n"
        "    return await rag.aquery(q, param)\n",
        0,
    ),
]


@pytest.mark.parametrize(
    ("case", "source", "expected"),
    _AQUERY_FIXTURES,
    ids=[c for c, _, _ in _AQUERY_FIXTURES],
)
def test_aquery_param_guard_rejects_dropped_unbound_or_non_context_only_params_in_fixture_scripts(
    case: str, source: str, expected: int
) -> None:
    count, violations = _aquery_param_findings(ast.parse(source))
    assert count == 1, case
    assert len(violations) == expected, (case, violations)


def test_aquery_param_guard_counts_zero_aquery_calls_in_a_fixture_without_one() -> None:
    """The non-vacuity half: a script with no aquery call reports count 0, which
    the query.py test above rejects."""
    source = "async def f(rag):\n    param = QueryParam(only_need_context=True)\n"
    assert _aquery_param_findings(ast.parse(source)) == (0, [])


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import lightrag\n", True),
        ("def f():\n    from lightrag.utils import EmbeddingFunc\n", True),
        ("from ne.brain_graph.lightrag import projector\n", False),
        ('LIGHTRAG = "lightrag"\n', False),
    ],
    ids=["import-lightrag", "nested-from-lightrag-utils", "first-party-namesake", "string-only"],
)
def test_lightrag_importer_check_distinguishes_lightrag_imports_from_namesakes_in_fixtures(
    source: str, expected: bool
) -> None:
    assert _imports_lightrag(ast.parse(source)) is expected


# (case, source, kind, expected): kind "imports" checks _forbidden_imports,
# kind "calls" checks _forbidden_calls. ``None`` = non-empty.
_FIXTURE_SCRIPTS: list[tuple[str, str, str, set[str] | None]] = [
    (
        "from-lightrag-api",
        "from lightrag.api import x\n",
        "imports",
        {"lightrag.api", "lightrag.api.x"},
    ),
    (
        "from-lightrag-import-parser",
        "from lightrag import parser\n",
        "imports",
        {"lightrag.parser"},
    ),
    (
        "importlib-parser-routing",
        'import importlib\nimportlib.import_module("lightrag.parser.routing")\n',
        "imports",
        None,
    ),
    ("await-ainsert", 'async def f(rag):\n    await rag.ainsert("x")\n', "calls", {"ainsert"}),
    ("enqueue", "rag.apipeline_enqueue_documents([])\n", "calls", {"apipeline_enqueue_documents"}),
    (
        "getattr-string-dispatch",
        'async def f(rag):\n    await getattr(rag, "ainsert")("x")\n',
        "calls",
        {"ainsert"},
    ),
    (
        "bound-method-alias",
        'async def f(rag):\n    ingest = rag.ainsert\n    await ingest("doc")\n',
        "calls",
        {"ainsert"},
    ),
    (
        "functools-partial",
        'import functools\nasync def f(rag):\n    await functools.partial(rag.ainsert, "doc")()\n',
        "calls",
        {"ainsert"},
    ),
    (
        "tuple-iteration",
        'async def f(rag):\n    for fn in (rag.ainsert,):\n        await fn("doc")\n',
        "calls",
        {"ainsert"},
    ),
    (
        "run-in-executor-insert",
        "import asyncio\n"
        "async def f(rag):\n"
        '    await asyncio.get_running_loop().run_in_executor(None, rag.insert, "doc")\n',
        "calls",
        {"insert"},
    ),
    (
        "control-ainsert-custom-kg",
        "async def f(rag):\n    await rag.ainsert_custom_kg({})\n",
        "calls",
        set(),
    ),
]


@pytest.mark.parametrize(
    ("case", "source", "kind", "expected"),
    _FIXTURE_SCRIPTS,
    ids=[c for c, _, _, _ in _FIXTURE_SCRIPTS],
)
def test_guard_flags_lightrag_api_imports_dynamic_parser_imports_and_ainsert_calls_references_and_getattr_dispatch_in_fixture_scripts(  # noqa: E501
    case: str, source: str, kind: str, expected: set[str] | None
) -> None:
    tree = ast.parse(source)
    found = _forbidden_imports(tree) if kind == "imports" else _forbidden_calls(tree)
    if expected is None:
        assert found, case
    else:
        assert found == expected, case


# ---------------------------------------------------------------------------
# The shipped lockfile and requirements.in
# ---------------------------------------------------------------------------
def test_sidecar_lockfile_pins_no_fastapi_uvicorn_or_starlette() -> None:
    names = _pinned_names((_ENV_DIR / "requirements.txt").read_text(encoding="utf-8"))
    assert names & _SERVER_CLOSURE == set()
    # Parser-sees-pins control: a parser returning nothing must fail here.
    assert {"lightrag-hku", "numpy", "nano-vectordb"} <= names


def test_lockfile_parser_flags_a_fastapi_pin_in_a_fixture_lockfile() -> None:
    fixture = (
        "aiohttp==3.14.3 \\\n"
        "    --hash=sha256:aa\n"
        "fastapi==0.115.0 \\\n"
        "    --hash=sha256:bb\n"
        "Uvicorn==0.30.0 ; python_version >= '3.12' \\\n"
    )
    names = _pinned_names(fixture)
    assert names == {"aiohttp", "fastapi", "uvicorn"}
    assert names & _SERVER_CLOSURE == {"fastapi", "uvicorn"}


def test_sidecar_requirements_in_pins_lightrag_hku_1_5_4_without_the_api_extra() -> None:
    lines = (_ENV_DIR / "requirements.in").read_text(encoding="utf-8").splitlines()
    requirements = [
        line.strip() for line in lines if line.strip() and not line.strip().startswith("#")
    ]
    assert requirements == ["lightrag-hku==1.5.4"]


# ---------------------------------------------------------------------------
# The dir-scan ledger, bound to the pinned version
# ---------------------------------------------------------------------------
def test_dir_scan_ledger_carries_the_four_lightrag_hku_statements_with_cve_and_ghsa_names_at_the_pinned_version() -> (  # noqa: E501
    None
):
    lockfile = (_ENV_DIR / "requirements.txt").read_text(encoding="utf-8")
    match = re.search(r"^lightrag-hku==(\S+)", lockfile, re.MULTILINE)
    assert match is not None
    pin = match.group(1)
    product = f"pkg:pypi/lightrag-hku@{pin}"

    statements = json.loads(_LEDGER.read_text(encoding="utf-8"))["statements"]
    pinned = [s for s in statements if s["products"] == [{"@id": product}]]
    assert {s["vulnerability"]["name"] for s in pinned} == set(_LIGHTRAG_IDS)
    assert len(pinned) == len(_LIGHTRAG_IDS)
    for statement in pinned:
        name = statement["vulnerability"]["name"]
        assert statement["status"] == "not_affected", name
        assert statement["justification"] == "vulnerable_code_not_in_execute_path", name
        assert statement["impact_statement"].strip(), name
        assert statement["vulnerability"]["aliases"] == [_LIGHTRAG_IDS[name]], name

    # A lightrag-hku bump without a ledger rewrite leaves statements at a
    # foreign version; that must fail here.
    foreign = {
        p["@id"]
        for s in statements
        for p in s["products"]
        if p["@id"].startswith("pkg:pypi/lightrag-hku@") and p["@id"] != product
    }
    assert foreign == set()
