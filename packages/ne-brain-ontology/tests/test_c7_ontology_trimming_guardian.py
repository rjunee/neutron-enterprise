# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The Tier-3 C7 guardian (permanent, unskippable) — ports ne-brain's (T1) and
ne-brain-graph's (T2) no-bypass mechanism to ne-brain-ontology (plan §2.1, the
sibling-path question raised by the 2026-07-23 invariant-audit's Finding C).

(a) STRUCTURAL SWEEP — every function/method in ``src/ne/brain_ontology`` that
    takes a ``principal`` arg is PINNED to
    ``REGISTERED_PRINCIPAL_SCOPED_FUNCTIONS``; the gateway + store adapters are
    asserted principal-FREE — T3 scopes A-PRIORI, via the trusted
    ``DatasetSpec`` that ``scoped_dataset`` builds, never inside the
    store/gateway layer itself (mirrors T1's ``QdrantChunkStore`` /
    T2's store-adapter principal-free assertions).
(b) BEHAVIORAL NO-BYPASS — for every registered function, prove BOTH a
    restrictive and a permissive principal route correctly: a restrictive
    principal is denied fail-closed with the store PROVABLY untouched; a
    permissive principal's scoped dataset reaches the store SERVER-SIDE on
    every read (the main read AND the provenance double-check), and an
    out-of-scope entity never appears in the answer.
(c) LABEL-ALGEBRA INVARIANT — ``principal_labels`` never returns empty;
    ``graphs_for_labels`` refuses an empty label set; a non-matching label set
    denies fail-closed — the escape branches (widen-to-full-union) are
    unreachable in production (the T3 twin of T1's production-hook invariant).
(d) RAW-SPARQL CONFINEMENT — ``.sparql(`` call sites (the UNSCOPED steward
    read surface on ``ValidatingTripleStore``) are pinned by (module, enclosing
    function) — not by module alone — so a new call dropped anywhere else in an
    allowlisted file still trips CI; non-stub ``async def sparql`` implementations
    live ONLY in the allowlisted modules. Mirrors T2's ``.cypher(`` confinement,
    tightened to function-level pinning for the call-site layer.

FUTURE AUTHOR: if you add a principal-taking function to ne-brain-ontology,
this guardian WILL fail until you (1) add it to
``REGISTERED_PRINCIPAL_SCOPED_FUNCTIONS`` AND (2) add a covering entry to
``_BEHAVIORAL_COVERAGE`` proving it denies a restrictive principal and
narrows/scopes a permissive one. That friction is intentional: an un-trimmed
principal-scoped read path is a C7 authorization bypass — the exact NE-021
bug shape this guardian exists to make impossible-by-construction.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from ne.brain_ontology import graphs_for_labels, load_template_registry
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.lift.graph_to_rdf import entity_iri
from ne.brain_ontology.store.protocols import DatasetSpec
from ne.brain_ontology.verified import (
    GovernedLocalT3Instance,
    ReleaseScopeError,
    SelectedReleaseRegistry,
    build_meridian_release,
    execute_template,
    governed_local_t3_scope,
    load_meridian_template_registry,
    principal_labels,
    publish_meridian_release,
    release_scoped_dataset,
    scoped_dataset,
    source_authorized_dataset,
)
from ne.brain_ontology.verified.errors import VerifiedAnswerScopeError

from .verified_helpers import (
    FIXTURES_DIR,
    EmptyExpander,
    RaisingExpander,
    StubExpander,
    account,
    make_gateway,
    principal,
    seed_accounts,
)

# tests -> ne-brain-ontology -> packages -> repo root.
_REPO = Path(__file__).resolve().parents[3]
_SRC = Path(__file__).resolve().parents[1] / "src" / "ne" / "brain_ontology"

_TPL = {t.template_id: t for t in load_template_registry(FIXTURES_DIR).templates}
_MERIDIAN_FIXTURE = _REPO / "tools" / "meridian" / "fixtures" / "meridian-v1"

REGISTERED_PRINCIPAL_SCOPED_FUNCTIONS = {
    ("verified/answers.py", "execute_template"),
    ("verified/scope.py", "principal_labels"),
    ("verified/scope.py", "scoped_dataset"),
    # 015A0-AUTHORITY-T3 mutable source-bearing variant: template capability and
    # source-native ACL are independent mandatory gates.  The latter is always
    # intersected with the lift's admitted contributor graph set.
    ("verified/scope.py", "source_authorized_dataset"),
    # 015A0-AUTHORITY-T3: the SOURCE-BEARING variant. It computes the IDENTICAL
    # principal trim as `scoped_dataset` (it delegates to it) and then confines the
    # result to the pinned immutable release's own graph set — it can only ever
    # narrow, never widen. The release is its FIRST parameter, so a head must
    # already be pinned before any DatasetSpec exists.
    ("verified/scope.py", "release_scoped_dataset"),
    # 'expand' is the GroupExpander Protocol's method — it has no runtime body of
    # its own here (it is a structural Protocol, injected at the composition
    # root); it is behaviorally covered through `principal_labels`, its only
    # conforming consumer in this package.
    ("verified/scope.py", "expand"),
}

_GUARD = (
    "C7 ontology guardian: the set of principal-scoped functions in "
    "ne-brain-ontology changed. A new such surface is an un-trimmed authorization "
    "bypass until it is registered in REGISTERED_PRINCIPAL_SCOPED_FUNCTIONS AND "
    "behaviorally covered in _BEHAVIORAL_COVERAGE (the NE-021 bug-shape trip-wire)."
)

# `.sparql(` (the UNSCOPED steward/audit read surface — full graph union, C12
# SELECT/ASK-only + banned-keyword refusal) may be called ONLY from these two
# documented, operator/steward-facing surfaces:
#   - packages/ne-api/src/ne/api/routers/ontology.py: the class-browser route
#     (`ontology_classes`) DOES carry a request principal, but that principal is
#     never used to scope the query — safety instead comes from the route's
#     module-level hardcoded `_CLASSES_QUERY` constant, whose LOAD-BEARING
#     `urn:ne:ontology:*` graph filter (see that file's own comment above the
#     constant) confines the union to schema graphs regardless of caller. No
#     caller-supplied query text ever reaches `.sparql(`.
#   - tools/ne-cli/src/ne/cli/ontology.py: the NE-ONTOLOGY-INSPECT steward CLI's
#     `ontology graphs` and `ontology sparql <query>` commands — operator-facing,
#     run out-of-band from any request principal, with live rejection tests in
#     tools/ne-cli/tests/test_ontology_cli.py.
# DRIFT NOTE: the 2026-07-23 invariant-audit findings report (Finding C) records
# only the first entry as of its publish date; the ne-cli entry point was merged
# afterward. Both route through the SAME C12 gate on `ValidatingTripleStore`, so
# this is the recorded resolution of that drift, not a new bypass — see the
# NE-INV-T3-GUARDIAN commit message for the full rationale.
#
# Pinned by (file, enclosing-function) — NOT by file alone — so a NEW `.sparql(`
# call added anywhere else in either module (e.g. a second route dropped into
# ontology.py, or a new ne-cli subcommand) still trips this guardian. Each
# tuple below is the ONE function per file that is documented and safe: the
# ne-api entry is principal-carrying but query-fixed (see rationale above);
# the ne-cli entries are operator/steward-facing and carry no request
# principal at all.
_SPARQL_CALL_ALLOWLIST = {
    ("packages/ne-api/src/ne/api/routers/ontology.py", "ontology_classes"),
    ("tools/ne-cli/src/ne/cli/ontology.py", "graphs"),
    ("tools/ne-cli/src/ne/cli/ontology.py", "sparql"),
}

# The ONLY real (non-stub) `async def sparql` implementation is the gateway's
# steward read surface. The `ne.contracts.knowledge.KnowledgeRead`-side
# `async def sparql(self, query: str) -> ...: ...` is a type-only Protocol stub
# (a `...` body) and is exempted below via `_is_stub_body`, never counted here.
_SPARQL_DEF_ALLOWLIST = {"packages/ne-brain-ontology/src/ne/brain_ontology/gateway.py"}


def _param_names(func: ast.AsyncFunctionDef | ast.FunctionDef) -> list[str]:
    a = func.args
    return [arg.arg for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


def _is_stub_body(func: ast.AsyncFunctionDef) -> bool:
    body = [
        n
        for n in func.body
        if not (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )
    ]
    return (
        len(body) == 1
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and body[0].value.value is ...
    )


# -- Layer (a): structural AST sweep -------------------------------------------


def _principal_scoped_functions() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_SRC).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and "principal" in (
                _param_names(node)
            ):
                found.add((rel, node.name))
    return found


def test_a_principal_scoped_functions_are_pinned() -> None:
    assert _principal_scoped_functions() == REGISTERED_PRINCIPAL_SCOPED_FUNCTIONS, _GUARD


def test_a_gateway_and_store_are_principal_free() -> None:
    # T3 scoping happens a-priori (the DatasetSpec scoped_dataset builds); the
    # gateway and every store backend must stay unscoped-by-design so a scoped
    # path can never masquerade inside the store/gateway layer.
    rels = ["gateway.py", *(f"store/{p.name}" for p in sorted((_SRC / "store").glob("*.py")))]
    assert len(rels) > 1  # non-vacuity: gateway.py + at least one store/*.py file
    for rel in rels:
        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                assert "principal" not in _param_names(node), (
                    f"{rel}:{node.name} must stay unscoped-by-design (no principal "
                    f"arg) — T3 scopes a-priori via the trusted DatasetSpec built by "
                    f"scoped_dataset, never inside the store/gateway. {_GUARD}"
                )


# -- Layer (b): behavioral no-bypass -------------------------------------------


def test_b_principal_labels_restrictive_vs_permissive() -> None:
    p = principal("user:a", ("alpha",))
    # Permissive expander: the label set widens to id union groups.
    assert principal_labels(p, StubExpander()) == frozenset({"user:a", "alpha"})
    # Empty expander: never empty — always contains at least principal.id.
    assert principal_labels(p, EmptyExpander()) == frozenset({"user:a"})
    # Raising expander: deny-by-erroring — the exception propagates, never
    # caught-and-falls-back to an unscoped result.
    with pytest.raises(RuntimeError, match="backend down"):
        principal_labels(p, RaisingExpander())


def test_b_scoped_dataset_denies_restrictive_and_narrows_permissive() -> None:
    tpl = _TPL["accounts-directory"]
    # Restrictive: the principal's only label ('gamma') is outside the
    # template's graph_classes ('alpha'/'beta') — fail-closed denial, never a
    # full-union fallback.
    restrictive = principal("user:c", ("gamma",))
    with pytest.raises(VerifiedAnswerScopeError, match="fail-closed"):
        scoped_dataset(restrictive, StubExpander(), tpl)

    # Permissive: the principal's label matches — the dataset narrows to
    # EXACTLY that label's named graph, never named_graphs.
    permissive = principal("user:a", ("alpha",))
    dataset = scoped_dataset(permissive, StubExpander(), tpl)
    assert dataset.default_graphs == graphs_for_labels(["alpha"]).default_graphs
    assert dataset.named_graphs == ()


def test_b_release_scoped_dataset_denies_restrictive_and_confines_permissive() -> None:
    """The source-bearing dataset denies a restrictive principal and only ever narrows.

    Restrictive: a principal whose labels miss the template's graph_classes is denied
    fail-closed by the SAME label algebra, before the release is consulted.
    Permissive: a matching principal gets EXACTLY the unscoped trim's graphs when the
    release carries them, and is refused — never widened — when it does not.
    """

    async def scenario() -> None:
        build = build_meridian_release(_MERIDIAN_FIXTURE)
        registry = SelectedReleaseRegistry(
            governed_local_t3_scope(
                GovernedLocalT3Instance(
                    deployment_id="deployment-c7",
                    dataset_instance_id="meridian-instance-c7",
                    generation=1,
                ),
                build.descriptor.dataset_name,
            )
        )
        release = await publish_meridian_release(
            registry, build, settings=OntologySettings(include_ontology_graph=False)
        )
        meridian_tpl = next(
            t
            for t in load_meridian_template_registry().templates
            if t.template_id == "customers-in-counties"
        )

        restrictive = principal("user:c", ("gamma",))
        with pytest.raises(VerifiedAnswerScopeError, match="fail-closed"):
            release_scoped_dataset(release, restrictive, StubExpander(), meridian_tpl)

        permissive = principal("user:a", ("bank.customer.read",))
        dataset = release_scoped_dataset(release, permissive, StubExpander(), meridian_tpl)
        assert dataset == scoped_dataset(permissive, StubExpander(), meridian_tpl)
        assert dataset.default_graphs == graphs_for_labels(["bank.customer.read"]).default_graphs
        assert dataset.named_graphs == ()

        # A label the principal legitimately holds whose graph is NOT in this release
        # is refused rather than silently read from another release's graph set.
        foreign = _TPL["accounts-directory"]
        holder = principal("user:a", tuple(foreign.graph_classes))
        with pytest.raises(ReleaseScopeError, match="outside selected release"):
            release_scoped_dataset(release, holder, StubExpander(), foreign)

    asyncio.run(scenario())


def test_b_source_authorized_dataset_conjoins_capability_and_source_acl() -> None:
    tpl = next(
        t
        for t in load_meridian_template_registry().templates
        if t.template_id == "customers-in-counties"
    )
    source_acl = "306773cc-b05a-43b4-bbb2-7858bfdb67dc"
    source_graphs = graphs_for_labels((source_acl,)).default_graphs

    # A source ACL cannot substitute for the template's product capability.
    with pytest.raises(VerifiedAnswerScopeError, match="fail-closed"):
        source_authorized_dataset(
            principal("user:a", (source_acl,)),
            StubExpander(),
            tpl,
            authorized_contributor_graphs=source_graphs,
        )

    # Conversely, the capability cannot select a source graph unless the
    # principal's expanded grants contain an ACL admitted by the lift.
    with pytest.raises(VerifiedAnswerScopeError, match="source_authority_unavailable"):
        source_authorized_dataset(
            principal("user:a", ("bank.customer.read",)),
            StubExpander(),
            tpl,
            authorized_contributor_graphs=source_graphs,
        )

    dataset = source_authorized_dataset(
        principal("user:a", ("bank.customer.read", source_acl)),
        StubExpander(),
        tpl,
        authorized_contributor_graphs=source_graphs,
    )
    assert dataset.default_graphs == source_graphs
    assert dataset.named_graphs == ()


class _ExplodingStore:
    """Every method raises — proves the store is never reached for a denied principal."""

    _MSG = "store reached despite out-of-scope principal"

    async def sparql_scoped(
        self, query: str, *, dataset: DatasetSpec
    ) -> Sequence[Mapping[str, str]]:
        raise AssertionError(self._MSG)

    def ontology_version(self) -> Any:
        raise AssertionError(self._MSG)


def test_b_execute_template_restrictive_principal_denied_store_untouched() -> None:
    async def scenario() -> None:
        with pytest.raises(VerifiedAnswerScopeError):
            await execute_template(
                _TPL["accounts-directory"],
                {},
                store=_ExplodingStore(),
                expander=StubExpander(),
                principal=principal("user:c", ("gamma",)),
                config_hash="a" * 64,
                max_rows=500,
            )

    asyncio.run(scenario())


class _DatasetRecordingStore:
    """Delegates to a real gateway, recording every DatasetSpec it is called with."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.datasets: list[DatasetSpec] = []

    async def sparql_scoped(
        self, query: str, *, dataset: DatasetSpec
    ) -> Sequence[Mapping[str, str]]:
        self.datasets.append(dataset)
        return await self._inner.sparql_scoped(query, dataset=dataset)

    def ontology_version(self) -> Any:
        return self._inner.ontology_version()


def test_b_execute_template_permissive_principal_scoped_dataset_reaches_store() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(
            gw,
            [
                account("acc_alpha", ["alpha"]),
                account("acc_beta", ["beta"]),
                account("acc_both", ["alpha", "beta"]),
            ],
        )
        store = _DatasetRecordingStore(gw)
        ans = await execute_template(
            _TPL["accounts-directory"],
            {},
            store=store,
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash="a" * 64,
            max_rows=500,
        )

        assert store.datasets, "execute_template must reach sparql_scoped at least once"
        expected_default = graphs_for_labels(["alpha"]).default_graphs
        for dataset in store.datasets:
            assert dataset.default_graphs == expected_default
            assert dataset.named_graphs == ()

        seen = set(ans.citations) | {row[0] for row in ans.rows}
        assert entity_iri("acc_alpha") in seen
        assert entity_iri("acc_both") in seen
        # The beta-only entity never appears — the ACL scope reached the store
        # server-side on both the main read and the provenance double-check.
        assert entity_iri("acc_beta") not in seen

    asyncio.run(scenario())


# Every registered principal-scoped function must have a covering behavioral
# test. Registering a function without behavioral coverage fails collection.
_BEHAVIORAL_COVERAGE: dict[tuple[str, str], str] = {
    ("verified/answers.py", "execute_template"): (
        "test_b_execute_template_permissive_principal_scoped_dataset_reaches_store"
    ),
    ("verified/scope.py", "principal_labels"): "test_b_principal_labels_restrictive_vs_permissive",
    ("verified/scope.py", "scoped_dataset"): (
        "test_b_scoped_dataset_denies_restrictive_and_narrows_permissive"
    ),
    ("verified/scope.py", "source_authorized_dataset"): (
        "test_b_source_authorized_dataset_conjoins_capability_and_source_acl"
    ),
    ("verified/scope.py", "expand"): "test_b_principal_labels_restrictive_vs_permissive",
    ("verified/scope.py", "release_scoped_dataset"): (
        "test_b_release_scoped_dataset_denies_restrictive_and_confines_permissive"
    ),
}

assert set(_BEHAVIORAL_COVERAGE) == REGISTERED_PRINCIPAL_SCOPED_FUNCTIONS, _GUARD


# -- Layer (c): label-algebra invariant -----------------------------------------


def test_c_label_algebra_never_empty_never_full_union() -> None:
    # principal_labels always contains principal.id, even over an expander that
    # returns nothing.
    p = principal("user:a", ())
    assert principal_labels(p, EmptyExpander()) == frozenset({"user:a"})

    # graphs_for_labels refuses to widen an empty label set to the full union.
    with pytest.raises(ValueError, match="empty label set"):
        graphs_for_labels([])

    # A non-matching label set is a fail-closed denial, never a union fallback.
    with pytest.raises(VerifiedAnswerScopeError, match="fail-closed"):
        scoped_dataset(
            principal("user:z", ("nowhere",)), StubExpander(), _TPL["accounts-directory"]
        )


# -- Layer (d): raw-SPARQL confinement -------------------------------------------


def _swept_files() -> list[Path]:
    # Sidecar third-party venvs (e.g. packages/ne-brain-graph/lightrag_env,
    # packages/ne-schema/codegen) sit at PACKAGE ROOT, never under */src, so this
    # glob structurally excludes them — do not "improve" it to rglob whole
    # packages.
    return [
        *(_REPO / "packages").glob("*/src/**/*.py"),
        *(_REPO / "tools").glob("*/src/**/*.py"),
    ]


def _rel_to_repo(path: Path) -> str:
    return path.relative_to(_REPO).as_posix()


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function_name(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    # Walks UP to the nearest enclosing def, not down — a call nested inside a
    # closure/comprehension inside the allowlisted function still attributes to
    # that function, which is the intent (the allowlist pins the outer, callable
    # surface, not every internal lambda).
    cur: ast.AST | None = parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef):
            return cur.name
        cur = parents.get(cur)
    return "<module>"


def test_d_sparql_call_sites_confined() -> None:
    files = _swept_files()
    # Non-vacuity: a repo-layout move must fail loud, never pass on an empty glob.
    assert len(files) > 100, f"only {len(files)} files swept — repo layout may have moved"
    allowlisted_files = {rel for rel, _func in _SPARQL_CALL_ALLOWLIST}
    for rel in allowlisted_files:
        assert (_REPO / rel).is_file(), f"allowlisted {rel} does not exist on disk"

    # Pinned by (file, enclosing-function) — see the allowlist comment above. A
    # new `.sparql(` call in an UNLISTED function, even inside an allowlisted
    # FILE, is an offender: this is the primary bypass this layer must catch (a
    # future principal-serving route/command added beside a documented one).
    offenders: set[tuple[str, str]] = set()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = _rel_to_repo(path)
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            # `sparql_scoped` is a DIFFERENT attribute name and never matches here.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sparql"
            ):
                site = (rel, _enclosing_function_name(node, parents))
                if site not in _SPARQL_CALL_ALLOWLIST:
                    offenders.add(site)
    assert not offenders, f"raw .sparql( call outside the allowlist: {offenders}"


def test_d_sparql_definitions_confined() -> None:
    files = _swept_files()
    assert len(files) > 100, f"only {len(files)} files swept — repo layout may have moved"
    for rel in _SPARQL_DEF_ALLOWLIST:
        assert (_REPO / rel).is_file(), f"allowlisted {rel} does not exist on disk"

    # AsyncFunctionDef-only: the ne-cli steward CLI's `def sparql` click command
    # is a SYNC function and is structurally exempt from this sweep.
    definers: set[str] = set()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = _rel_to_repo(path)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "sparql"
                and not _is_stub_body(node)
            ):
                definers.add(rel)
    assert definers == _SPARQL_DEF_ALLOWLIST, f"unexpected `async def sparql` impls: {definers}"
