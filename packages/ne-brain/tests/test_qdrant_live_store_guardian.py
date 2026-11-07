# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Guardian: a test must never address a Qdrant it did not itself create.

⛔ THIS CLOSES THE DEFECT CLASS THAT TOOK THE LIVE VECTOR STORE DOWN.

2026-09-10 — the demo's Qdrant went ``status: red``: optimizer ``Failed to remove
segment: Too many open files (os error 24)``, every write ``Can't write WAL: "no
segments available"``, and the corpus seed of that night's deploy failed with it.
986 collections existed. **Exactly one was real** (``ne_chunks``); the other 985
were orphaned test collections — ``kit_<uuid4>``, ``kw_<uuid4>``,
``spine_<uuid4>``, ``rag_public_live_<uuid4>`` — accumulated by conformance and
integration tests over weeks. Segments x collections had exhausted the process's
file-descriptor ceiling.

Two defects compounded, and this guardian asserts against both:

1. **The address was DEFAULTED.** ``os.environ.get("NE_QDRANT_URL", <the
   deployed store's own address>)``, skipping only when that was UNREACHABLE. On
   a box running the stack that port IS the deployed store, so an ordinary
   ``pytest packages/ne-brain`` wrote into production. REACHABILITY IS NOT
   CONSENT.
2. **Nothing was ever deleted.** Not on failure, not on error, not on success —
   ``create_store()`` minted a collection per kit case and walked away.

This is the SAME class the OPA incidents taught twice
(``packages/ne-mcp/tests/test_opa_pdp_mutation_guardian.py``, 2026-08-30 and
2026-09-09) and the same fix shape: the address is never defaulted, the resource
is owned by the test that made it, and a SOURCE-SCANNING guardian keeps the rule
absolute so a new offender FAILS rather than quietly joining a list.

⭐ **THE SOURCE RULES ARE THE SECOND LINE, NOT THE ONLY ONE.** A source scan is
only ever as wide as its globs, and the first version of this guardian proved it:
the sanctioned accessor had moved into ``src/`` and the globs covered ``tests/``
only, so the exact 2026-09-10 arming expression could be pasted back into the
accessor and this file stayed green. So the rules below scan the helper surface
too **and** ``ne.brain.testing.qdrant_live`` now refuses, at runtime, to address a
Qdrant no ``NE_*QDRANT_URL`` variable is holding — which is what a default is.
:func:`test_an_unnamed_address_is_refused_by_every_door` proves that by calling
the doors, not by reading them.

⭐ **CLIENT CONSTRUCTION IS MATCHED ON THE RESOLVED VENDOR SYMBOL**, not on a name
that ends in ``QdrantClient``, and **the vendor's default is the deployed store**:
``AsyncQdrantClient()`` with no arguments is ``http://localhost:6333``, and
``location=`` — not ``url=`` — is its first parameter. Matching on the local name
let ``AsyncQdrantClient()``, ``(host=..., port=...)``, ``(**opts)`` and a renamed
import through; matching the import binding closes all four at once, and stops
flagging the in-tree fakes (``_QdrantClient``, ``_FakeAsyncQdrantClient``) that
forced the over-broad escape hatch in the first place.

It needs no Qdrant, no docker and no network, and contains ZERO skip markers
(self-checked below, on the AST as well as the text), so it runs on every CI job
and cannot degrade to a green skip the way the live-parked proofs it guards
deliberately do.
"""

from __future__ import annotations

import ast
import asyncio
import itertools
import logging
import os
import re
import types
import urllib.parse
from pathlib import Path

import pytest

# ⭐ ONE IMPLEMENTATION OF "LOOPBACK", SHARED BY BOTH LAYERS OF THIS DEFENCE.
# Round 5 got past the runtime refusal AND both source rules below with the same
# five spellings, because each layer carried its own two-spelling notion of what
# loopback means. The janitor's is address-based (``inet_aton`` shorthand, all of
# ``127.0.0.0/8``, IPv4-mapped IPv6, RFC 6761 names), so the rules here use THAT
# one rather than a regex of their own: a spelling that gets past one layer can no
# longer get past the other for free. Imported at module scope on purpose — a
# per-function import would let the two drift apart again.
from ne.brain.testing.qdrant_live import is_loopback_host

# tests/ -> ne-brain/ -> packages/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]

_TEST_GLOBS = ("packages/*/tests/**/*.py", "tools/*/tests/**/*.py", "e2e/**/*.py")

#: EVERY first-party ``src/`` module, and rules 1 and 3 scan all of it.
#:
#: ⛔ ``src/**/testing/**`` WAS THE ANSWER LAST ROUND AND IT WAS THE ROUND-1
#: LESSON REPEATED ONE DIRECTORY UP. Round 1 found the loaded gun had moved from
#: ``tests/`` into ``src/``; the repair widened the patrol to ``src/**/testing/**``
#: only, so a review planted ``packages/ne-brain/src/ne/brain/scratch_vectors.py``
#: — ``AsyncQdrantClient(url="http://localhost:6333")``, called from a one-line
#: test — and every rule stayed green. There is no path predicate that separates
#: "test support" from "a module a test can import", so the patrol is the whole
#: tree and the few production modules that legitimately address a configured
#: store are named in :data:`_SANCTIONED_SRC_URL_CLIENTS`, one exact set that a
#: new file cannot join by accident.
_SRC_GLOBS = ("packages/*/src/**/*.py", "tools/*/src/**/*.py")

#: Every first-party Python file. The "no defaulted address" and "no source writes
#: a consent variable" rules run over ALL of it — both are wrong in a helper nobody
#: has written yet just as much as in a test, with no legitimate exception anywhere.
_FIRST_PARTY_GLOBS = (*_TEST_GLOBS, *_SRC_GLOBS)

#: Prose a human might FOLLOW. Deliberately wider than the package READMEs it used
#: to be: the doc tree is exactly where someone would write "export NE_QDRANT_URL=
#: ..." . ``deploy/**`` is NOT here — it is operator-facing runtime configuration
#: for the app's own container (``NE_API_QDRANT_URL``), not a recipe for a test.
#:
#: ⭐ That reasoning is now enforced rather than merely asserted here: the janitor
#: used to accept the very variable this comment calls "not a test's", so the file
#: contradicted itself. See
#: :func:`test_the_deployed_configurations_variables_are_not_test_consent`.
_PROSE_GLOBS = (
    "*.md",
    "docs/**/*.md",
    "packages/*/README.md",
    "packages/*/docs/**/*.md",
    "tools/*/README.md",
    "tools/*/docs/**/*.md",
    "e2e/**/*.md",
)

#: Every env var that can carry a Qdrant address into a test.
_QDRANT_URL_VAR_RE = re.compile(r"NE_[A-Z0-9_]*QDRANT_URL")

#: ``host:port`` as it appears inside a string — a bracketed IPv6 literal, or
#: anything that could be a hostname or an IPv4 address in any spelling.
#:
#: ⛔ THE REGEX FINDS CANDIDATES; :func:`is_loopback_host` DECIDES. The pair it
#: replaces — ``(?:localhost|127\.0\.0\.1):\d+`` — was a two-spelling list, and
#: ``localhost.``, ``127.1``, ``2130706433``, ``127.0.0.2`` and
#: ``::ffff:127.0.0.1`` all walked past it into a real 127.0.0.1 listener. Deciding
#: the host with the janitor's own address semantics is what stops these rules
#: sharing the runtime refusal's blind spot; see :func:`_loopback_ports`.
_HOST_PORT_RE = re.compile(r"(?:\[([0-9A-Fa-f:.]+)\]|([A-Za-z0-9_.\-]+)):(\d{1,5})\b")

#: Qdrant's own two ports. A file that can build a Qdrant client has no business
#: spelling a loopback address on either, however that string is assembled.
_QDRANT_PORTS = frozenset({6333, 6334})


def _loopback_ports(text: str) -> list[int]:
    """Every port in ``text`` reached at an address meaning THIS MACHINE.

    ⚠️ Same limit as :func:`is_loopback_host`, stated here too because a source rule
    that quietly claims more than the runtime does is how round 5 happened: this
    sees ADDRESS spellings plus ``localhost``/``*.localhost``. A recipe that writes
    some other name for this box — ``ip6-localhost``, the machine's hostname, a
    DNS record pointed at 127.0.0.1 — is NOT seen by these rules, and nothing in
    this file should be read as saying otherwise.
    """
    ports: list[int] = []
    for bracketed, bare, port in _HOST_PORT_RE.findall(text):
        host = (bracketed or bare).lower().rstrip(".")
        if host and is_loopback_host(host):
            ports.append(int(port))
    return ports


#: The in-process, per-process vendor store. Never a server, never shared, and the
#: right answer for almost every test — ``AsyncQdrantClient(":memory:")``.
_LOCAL_MODE = ":memory:"

#: The vendor package. Import bindings out of it are what the client rule matches.
_VENDOR = "qdrant_client"

#: The single sanctioned way to reach a Qdrant SERVER from a test.
_JANITOR = "ne.brain.testing.qdrant_live"
_JANITOR_PATH = Path("packages/ne-brain/src/ne/brain/testing/qdrant_live.py")

#: EMPTY, AND IT STAYS EMPTY. ⛔ NO TEST MAY BUILD A QDRANT CLIENT AGAINST A URL.
#:
#: Building the client is what makes the store addressable; registering the
#: collection is what makes it disposable. Splitting those two decisions across
#: different files is how 985 collections were created by code that had no idea it
#: owned anything. So the constructor lives in ONE place —
#: ``ne.brain.testing.qdrant_live.OwnedQdrantCollections.client`` — beside the
#: teardown that drops what it hands out, and no test source builds its own.
#:
#: The assertion is an EXACT set comparison rather than a subset check, so a new
#: URL-addressing test FAILS this guardian instead of silently joining a list.
#: Re-populating this set is not a fix for that failure — using the janitor is.
_SANCTIONED_URL_CLIENTS: set[Path] = set()

#: The deployed application. Addressing the configured vector store is its JOB —
#: it is the thing ``NE_API_QDRANT_URL`` exists to configure — so it cannot go
#: through a test janitor, and it creates no per-test collections to leak.
_APP_PLATFORM_PATH = Path("packages/ne-api/src/ne/api/platform.py")

#: ...and across ALL of ``src/``, exactly these two files may build one.
#:
#: EXACT SET, not a subset check: a new ``src/`` module that constructs a server
#: client fails here and has to argue for itself, which is what the planted
#: ``scratch_vectors.py`` never had to do.
_SANCTIONED_SRC_URL_CLIENTS = {_JANITOR_PATH, _APP_PLATFORM_PATH}

#: The ONE place a Qdrant URL var may appear beside a loopback address in prose.
#:
#: ``tools/ne-cli/tests/test_workflow_authority_gate.py`` asserts what
#: ``.github/workflows/ci.yml`` sets on the promoted-proof STEP, where
#: ``localhost:6333`` is the job's OWN service container — created and destroyed
#: by that job. It parses YAML and opens no socket.
#:
#: Everything else that pairs those two strings is a recipe, and a recipe naming
#: the deployed port is the loaded gun the OPA incident's second failure was made
#: of: the consent gate held, and then the file's own documented instructions
#: satisfied it.
_SANCTIONED_LOOPBACK_RECIPES = {
    Path("tools/ne-cli/tests/test_workflow_authority_gate.py"),
}


def _glob(globs: tuple[str, ...]) -> set[Path]:
    paths: set[Path] = set()
    for pattern in globs:
        paths.update(p for p in _REPO_ROOT.glob(pattern) if p.is_file())
    paths.discard(Path(__file__).resolve())
    return paths


def _test_sources() -> list[Path]:
    return sorted(_glob(_TEST_GLOBS))


def _src_sources() -> list[Path]:
    return sorted(_glob(_SRC_GLOBS))


def _first_party_sources() -> list[Path]:
    return sorted(_glob(_FIRST_PARTY_GLOBS))


def _scanned_prose() -> list[Path]:
    """Test sources PLUS the developer-facing doc tree — see :data:`_PROSE_GLOBS`."""
    return sorted(_glob(_TEST_GLOBS) | _glob(_PROSE_GLOBS))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _rel(path: Path) -> Path:
    return path.relative_to(_REPO_ROOT)


# --------------------------------------------------------------------------- #
# Rule 1: nothing but the janitor builds a client against a Qdrant SERVER.
# --------------------------------------------------------------------------- #


#: ``importlib.import_module("qdrant_client")`` and ``__import__("qdrant_client")``
#: bind the vendor module without producing an ``Import`` node at all, so a rule
#: that reads only the import statements saw an empty binding set and short-
#: circuited to "this file cannot address a Qdrant". Both are matched as imports.
_DYNAMIC_IMPORTERS = frozenset({"import_module", "__import__"})


def _dynamic_import_target(node: ast.expr) -> str | None:
    """The module name of a dynamic import expression, if that is what this is."""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name not in _DYNAMIC_IMPORTERS:
        return None
    target = node.args[0]
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        return target.value
    return None


def _is_module(name: str, module: str) -> bool:
    return name == module or name.startswith(module + ".")


def _vendor_bindings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Local names bound to a vendor client CLASS, and to the vendor MODULE.

    Resolving the import is the whole point: ``from qdrant_client import
    AsyncQdrantClient as _C`` binds ``_C``, and a rule that matched the local
    name's suffix saw nothing at all. Conversely a test's own
    ``class _FakeAsyncQdrantClient`` is not bound to anything vendor, so it stops
    being a false offender and the over-broad "no args means a fake" escape — the
    one that let ``AsyncQdrantClient()``, i.e. ``http://localhost:6333``, through —
    is not needed.

    ⭐ FOUR MORE BINDING FORMS, ALL OF WHICH WENT GREEN AS PLANTED OFFENDERS:

    * ``class _Scratch(AsyncQdrantClient): pass`` — a ``ClassDef`` is not a
      ``Call``, so the subclass bound a brand-new name that constructed a real
      server client while bypassing the janitor entirely. That is the CLEANUP half
      of the outage, not just the addressing half, so it matters most.
    * ``importlib.import_module("qdrant_client")`` — no import statement exists,
      so the binding set was empty and every rule short-circuited.
    * ``m = importlib.import_module("qdrant_client")`` then ``m.QdrantClient(...)``
      — the same, one assignment later.
    * ⭐ **ROUND 6: ``CLIENT = AsyncQdrantClient`` — a plain rebind of an ALREADY
      IMPORTED name.** Subclasses were resolved to a fixpoint and dynamic-import
      aliases were read, but the shortest alias of all was not: an ``Assign`` whose
      value is simply the imported ``Name``. Combined with round 4's indirect
      environment write, ``CLIENT(url=os.environ[KEY])`` returned ``False`` from all
      four source rules AND never went through :class:`OwnedQdrantCollections`, so
      there was no runtime check and no cleanup registration either.

    Both subclassing and aliasing are resolved TOGETHER to a fixpoint, so an alias
    of a subclass of an alias binds too.

    ⚠️ WHAT STILL DOES NOT BIND, STATED RATHER THAN LEFT TO THE NEXT ROUND. This
    resolves NAME bindings whose value the AST can evaluate to the vendor: direct
    and chained assignment, annotated assignment, walrus, and tuple unpacking of a
    tuple literal. It does NOT resolve a vendor class that arrives through a
    container (``{"c": AsyncQdrantClient}["c"]``, a list, a dataclass field), a
    ``getattr(qdrant_client, "AsyncQdrantClient")`` lookup, a function parameter or
    return value, a class attribute, a ``globals()`` write, or a rebind inside a
    conditional branch. Each of those makes the vendor reachable under a name this
    function does not know, and rule 1 will call the file harmless. That is the
    permanent shape of a source scan — see the module docstring's statement that the
    RUNTIME refusal, not this file, is the load-bearing half.
    """
    classes: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if not _is_module(module, _VENDOR):
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name.endswith("QdrantClient"):
                    classes.add(bound)
                else:
                    modules.add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _is_module(alias.name, _VENDOR):
                    modules.add(alias.asname or alias.name.split(".")[0])

    # ...then every subclass AND every alias of anything already known, together,
    # to a fixpoint. One loop rather than two passes, because an alias of a
    # subclass and a subclass of an alias are both reachable only by iterating.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name not in classes and any(
                    _is_vendor_client_expr(base, classes, modules) for base in node.bases
                ):
                    classes.add(node.name)
                    changed = True
                continue
            for name, value in _name_bindings(node):
                if name in classes or name in modules:
                    continue
                if _is_vendor_client_expr(value, classes, modules):
                    classes.add(name)
                    changed = True
                elif _is_vendor_module_expr(value, modules) or (
                    (imported := _dynamic_import_target(value)) is not None
                    and _is_module(imported, _VENDOR)
                ):
                    modules.add(name)
                    changed = True
    return classes, modules


def _name_bindings(node: ast.AST) -> list[tuple[str, ast.expr]]:
    """``(bound name, the expression bound to it)`` for the direct binding forms.

    ⛔ ONLY THE FORMS WHERE THE SOURCE SAYS WHAT THE NAME IS. ``a = b = C``,
    ``a: T = C``, ``(a := C)`` and ``a, b = C, D`` all state the binding outright;
    a container subscript, a ``getattr``, a call's return value and a function
    parameter do not, and guessing at those is how a rule starts producing false
    offenders. :func:`_vendor_bindings` documents the gap that leaves.
    """
    bindings: list[tuple[str, ast.expr]] = []

    def bind(target: ast.expr, value: ast.expr) -> None:
        if isinstance(target, ast.Name):
            bindings.append((target.id, value))
        elif (
            isinstance(target, ast.Tuple | ast.List)
            and isinstance(value, ast.Tuple | ast.List)
            and len(target.elts) == len(value.elts)
        ):
            for element, bound in zip(target.elts, value.elts, strict=True):
                bind(element, bound)

    if isinstance(node, ast.Assign):
        for target in node.targets:
            bind(target, node.value)
    elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
        bind(node.target, node.value)
    return bindings


def _unwrap(node: ast.expr) -> ast.expr:
    """``(A := X)`` is ``X``. The walrus binds AND evaluates, so a rule that reads
    only the binding misses ``(A := AsyncQdrantClient)(url=...)`` at the call site."""
    while isinstance(node, ast.NamedExpr):
        node = node.value
    return node


def _root_name(node: ast.expr) -> str:
    node = _unwrap(node)
    while isinstance(node, ast.Attribute):
        node = _unwrap(node.value)
    return node.id if isinstance(node, ast.Name) else ""


def _is_vendor_module_expr(node: ast.expr, modules: set[str]) -> bool:
    """Does this expression evaluate to the vendor module (or a submodule of it)?"""
    node = _unwrap(node)
    imported = _dynamic_import_target(node)
    if imported is not None:
        return _is_module(imported, _VENDOR)
    return _root_name(node) in modules if modules else False


def _is_vendor_client_expr(node: ast.expr, classes: set[str], modules: set[str]) -> bool:
    """Is this expression a vendor client CLASS — as a name, or off the module?"""
    node = _unwrap(node)
    if isinstance(node, ast.Name):
        return node.id in classes
    if isinstance(node, ast.Attribute) and node.attr.endswith("QdrantClient"):
        return _is_vendor_module_expr(node.value, modules)
    return False


def _is_vendor_client_call(node: ast.Call, classes: set[str], modules: set[str]) -> bool:
    return _is_vendor_client_expr(node.func, classes, modules)


#: Arguments that can only mean "a server over there".
_SERVER_KWARGS = frozenset({"url", "host", "port", "grpc_port", "https", "api_key", "prefix"})


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """``NAME -> str`` for every unambiguous string binding in the module.

    ⭐ WITHOUT THIS, ONE EXTRA VARIABLE HIDES THE ADDRESS. The literal
    ``"http://localhost:6333"`` was caught, and so was ``"http://" + "local" +
    "host:6333"`` — but ``_A = "http://local"; _B = "host:6333"`` then ``_A + _B``
    folded to ``None`` and every rule that reads folded strings went quiet. A name
    bound twice to different text is dropped rather than guessed at.
    """
    seen: dict[str, str | None] = {}
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        value = node.value
        text = _provable_string(value) if value is not None else None
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id in seen and seen[target.id] != text:
                seen[target.id] = None  # ambiguous: two different bindings
            else:
                seen[target.id] = text
    return {name: text for name, text in seen.items() if text is not None}


def _provable_string(node: ast.expr, consts: dict[str, str] | None = None) -> str | None:
    """The ``str`` this expression provably is, or ``None`` if the source cannot say.

    A literal, an f-string, a ``+`` chain of those and — with ``consts`` — a name
    bound unambiguously to one of them are provably strings. A call and, above all,
    ``None`` are not.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name) and consts is not None:
        return consts.get(node.id)
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "\x00"
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _provable_string(node.left, consts)
        right = _provable_string(node.right, consts)
        return None if left is None or right is None else left + right
    return None


def _is_local_mode(node: ast.Call, consts: dict[str, str] | None = None) -> bool:
    """Is this vendor construction the in-process store, provably, from the source?

    ⛔ THE VENDOR DEFAULT IS NOT LOCAL MODE. ``AsyncQdrantClient()`` resolves to
    ``AsyncQdrantRemote`` at ``http://localhost:6333`` — the deployed store on this
    box, and the shortest possible spelling of the outage. So local mode has to be
    stated: ``":memory:"`` (positionally or as ``location=``) or an on-disk
    ``path=``. Anything else, ``**opts`` included, is a server.

    ⭐ **AND ``path=None`` IS NOT AN ON-DISK PATH — IT IS THE DEPLOYED STORE.**
    Verified against the installed vendor package: the constructor branches on
    ``elif path is not None``, so ``AsyncQdrantClient(path=None)`` falls through to
    ``AsyncQdrantRemote(url=None, host=None)`` — i.e. ``http://localhost:6333``,
    the identical endpoint as the no-argument call this rule already refuses.
    Matching on the mere PRESENCE of the ``path`` keyword reported that safe. So
    the VALUE has to be a provable string (:func:`_provable_string`); a name, a
    call or ``None`` cannot prove local mode any more than ``**opts`` can. Spell a
    computed path as ``path=f"{tmp_path}"`` and the source proves it again.
    """
    keywords = {kw.arg for kw in node.keywords}
    if None in keywords:  # **opts — the source cannot prove local mode
        return False
    if len(node.args) > 1:
        return False
    if keywords & _SERVER_KWARGS:
        return False

    location: ast.expr | None = node.args[0] if node.args else None
    for kw in node.keywords:
        if kw.arg == "location":
            location = kw.value
    if location is not None:
        return _provable_string(location, consts) == _LOCAL_MODE
    for kw in node.keywords:
        if kw.arg == "path":
            return _provable_string(kw.value, consts) is not None
    return False


def _addresses_a_qdrant_server(tree: ast.Module) -> bool:
    """⛔ NO "NO IMPORTS MEANS NO REACH" SHORT-CIRCUIT.

    That early return is what made ``importlib.import_module("qdrant_client")
    .AsyncQdrantClient(url=...)`` invisible: it produces no import statement, so
    the binding sets came back empty and the rule declared the file harmless
    before looking at a single call.
    """
    classes, modules = _vendor_bindings(tree)
    consts = _module_constants(tree)
    return any(
        isinstance(node, ast.Call)
        and _is_vendor_client_call(node, classes, modules)
        and not _is_local_mode(node, consts)
        for node in ast.walk(tree)
    )


# --------------------------------------------------------------------------- #
# Rule 2: the address is never defaulted, anywhere in first-party Python.
# --------------------------------------------------------------------------- #


def _names_a_qdrant_url_var(node: ast.expr) -> bool:
    """``"NE_QDRANT_URL"`` — or the CONSTANT holding it, which is how it is spelled
    now that the accessor lives in a module with a ``NE_QDRANT_URL_VAR``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(_QDRANT_URL_VAR_RE.fullmatch(node.value))
    identifier = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
    return bool(_QDRANT_URL_VAR_RE.search(identifier))


def _is_env_read_of_a_qdrant_var(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call) or not node.args:
        return False
    func = node.func
    getter = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if getter not in ("get", "getenv", "setdefault"):
        return False
    return _names_a_qdrant_url_var(node.args[0])


def _defaults_a_qdrant_address(tree: ast.Module) -> bool:
    """The arming expression, in either of its two spellings.

    ``os.environ.get(NE_..._QDRANT_URL, "http://...")`` is what aimed 985
    collections at production: it turns "the operator named a store" into
    "whatever answers the deployment's port", and it makes the accompanying
    reachability skip a lie. ``os.environ.get(...) or "http://..."`` is the same
    expression with the fallback moved one token to the right, so both are here.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and len(node.args) >= 2
            and _is_env_read_of_a_qdrant_var(node)
        ):
            fallback = node.args[1]
            if isinstance(fallback, ast.Constant) and fallback.value:
                return True
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            values = node.values
            for read, fallback in itertools.pairwise(values):
                if not _is_env_read_of_a_qdrant_var(read):
                    continue
                if isinstance(fallback, ast.Constant) and fallback.value:
                    return True
    return False


# --------------------------------------------------------------------------- #
# Rule 2b: nothing WRITES a test-consent variable. Naming is only consent if the
# thing doing the naming is the operator.
# --------------------------------------------------------------------------- #

#: Every call that can put a value into the environment under a chosen name.
_ENV_WRITERS = frozenset({"setdefault", "setenv", "putenv"})


def _consent_var_writes(tree: ast.Module) -> list[ast.expr | None]:
    """Every write of a TEST-CONSENT variable, as the VALUE each one assigns.

    ⛔ THE NAMING DESIGN CANNOT DEFEND ITSELF AGAINST THIS, AND SAYING SO IS THE
    POINT. "An address exists only if the environment holds it" makes a defaulted
    READ inert — and does exactly nothing about a WRITE. Both of these were
    planted and every rule stayed green, with ``qdrant_server_url()``,
    ``operator_named_servers()`` and ``OwnedQdrantCollections`` all then accepting
    the deployed address, because by the time they looked, the environment really
    did name it::

        os.environ.setdefault("NE_QDRANT_URL", _A + _B)
        os.environ["NE_QDRANT_URL"] = _A + _B

    No runtime check can tell that apart from an operator who exported it. So the
    hole is closed HERE, by a source rule — which is a guardian, not a structure,
    and is therefore only ever as wide as :data:`_FIRST_PARTY_GLOBS`. The janitor's
    module docstring says so in those words rather than claiming a guarantee it
    cannot give.

    ``delenv``/``del`` are not writes: removing consent is always safe.
    """
    from ne.brain.testing.qdrant_live import TEST_CONSENT_URL_VARS

    consent = set(TEST_CONSENT_URL_VARS)

    def names_consent(node: ast.expr) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value in consent
        identifier = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
        if not identifier:
            return False
        # ``NE_QDRANT_URL_VAR`` / ``janitor.NE_TEST_QDRANT_URL_VAR`` — the constants
        # the janitor exports for exactly these names.
        return any(identifier.startswith(var) for var in consent)

    writes: list[ast.expr | None] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.expr)
                    and names_consent(target.slice)
                ):
                    writes.append(node.value)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called in _ENV_WRITERS and names_consent(node.args[0]):
                writes.append(node.args[1] if len(node.args) > 1 else None)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is not None and names_consent(key):
                    writes.append(value)
    return writes


def test_no_first_party_source_writes_a_test_consent_variable() -> None:
    """⭐ MANUFACTURED CONSENT IS STILL CONSENT TO THE RUNTIME.

    Setting ``NE_QDRANT_URL`` from inside the process makes every door in the
    janitor open on the address that was set — correctly, because the environment
    now names it. The variable belongs to the OPERATOR (a shell export, a CI step's
    ``env:``), and no first-party source may assign it.
    """
    offenders = sorted(
        _rel(path) for path in _first_party_sources() if _consent_var_writes(_parse(path))
    )
    assert offenders == [], (
        "a first-party source WRITES a Qdrant test-consent variable. That is not a "
        "default the naming rule can make inert — it is the naming itself, forged. "
        "The variable is the operator's to set. "
        f"Offenders: {offenders}"
    )


#: The only values this guardian may itself put into a consent variable.
#:
#: It must write them — it proves the doors by calling them — so it is excluded
#: from the rule above and constrained by this one instead: a scheme with no server
#: behind it, and the empty string. The other two families are below.
_HARMLESS_TEST_ADDRESSES = frozenset({"", "ftp://nowhere"})

#: ⛔ THE ONE PORT THIS GUARDIAN MAY POINT A CONSENT VARIABLE AT ON THIS MACHINE.
#:
#: ``http://127.0.0.1:1`` used to be a LITERAL in the set above, which made the
#: guarantee "this exact string" rather than "an address that reaches nothing" —
#: and round 5 is the whole argument against pinning a guarantee to one spelling.
#: So the rule is the property instead: port 1 (tcpmux, privileged, and not
#: Qdrant's 6333/6334) at any address :func:`is_loopback_host` recognises. That is
#: the SAME guarantee the literal gave, widened to the spellings of the same dead
#: endpoint — exactly the precedent ``.invalid`` set below, and not a relaxation:
#: no live-store address can qualify, because the live store is not on port 1.
_REFUSED_LOOPBACK_PORT = 1

#: ...plus the whole RFC 2606 reserved family. ``.invalid`` is guaranteed by the
#: RFC never to resolve, so ``http://qdrant.invalid:6333`` is exactly as dead as a
#: refused port and needs no entry of its own — which matters because round 4's
#: tests need a *deployment-shaped* address (a real-looking host on Qdrant's own
#: port) that still cannot be reached. ⛔ This is the same guarantee as the frozen
#: set above, not a relaxation of it: a host that merely CONTAINS "invalid" does
#: not qualify, only one whose final label is ``invalid``.
_RESERVED_DEAD_TLD = ".invalid"


def _is_provably_dead(text: str) -> bool:
    """Can this address provably reach nothing? See :data:`_HARMLESS_TEST_ADDRESSES`."""
    if text in _HARMLESS_TEST_ADDRESSES:
        return True
    candidate = text if "://" in text else f"http://{text}"
    try:
        parts = urllib.parse.urlsplit(candidate)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return False
    if host.endswith(_RESERVED_DEAD_TLD):
        return True
    return port == _REFUSED_LOOPBACK_PORT and is_loopback_host(host)


def test_this_guardian_never_points_a_consent_variable_at_a_real_server() -> None:
    """The self-exemption above, bounded. Otherwise the guard exempts the guard."""
    tree = _parse(Path(__file__).resolve())
    consts = _module_constants(tree)
    written: list[str] = []
    for value in _consent_var_writes(tree):
        if value is None:
            continue  # a delete, not a write
        text = _provable_string(value, consts)
        # An address the source cannot prove is exactly as bad as a live one here.
        written.append(text if text is not None else f"<unprovable: {ast.unparse(value)}>")
    assert written, "this guardian must actually exercise the consent doors"
    stray = sorted({text for text in written if not _is_provably_dead(text)})
    assert stray == [], (
        "this guardian sets a consent variable to an address that is not provably "
        f"dead: {stray}. Its own tests must never be able to reach a real store."
    )


# --------------------------------------------------------------------------- #
# Rule 3: no source that can build a Qdrant client spells the deployed address.
# --------------------------------------------------------------------------- #


def _folded_strings(tree: ast.Module) -> list[str]:
    """Every string this module can assemble out of constants, ``+`` and f-strings.

    ``"http://" + "localhost" + ":6333"`` is the same loaded gun as the literal,
    and a line-oriented regex over the raw text does not see it.
    """

    consts = _module_constants(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant | ast.BinOp | ast.JoinedStr | ast.Name):
            text = _provable_string(node, consts)
            if text is not None:
                found.append(text)
    return found


def _can_reach_qdrant(tree: ast.Module) -> bool:
    """Does this module reach the vendor client or the janitor? Then it can aim.

    ⭐ ``import ne.brain.testing.qdrant_live as j`` IS A REACH. The rule read
    ``ImportFrom`` only, so the plain ``import ... as`` form — followed by
    ``j.qdrant_server_url() or "http://localhost:6333"`` — was not a module that
    could reach a Qdrant, and the fallback beside it went unread. Dynamic imports
    of either module count too.
    """
    classes, modules = _vendor_bindings(tree)
    if classes or modules:
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _is_module(node.module or "", _JANITOR):
            return True
        if isinstance(node, ast.Import) and any(
            _is_module(alias.name, _JANITOR) for alias in node.names
        ):
            return True
        if isinstance(node, ast.Call):
            imported = _dynamic_import_target(node)
            if imported is not None and (
                _is_module(imported, _JANITOR) or _is_module(imported, _VENDOR)
            ):
                return True
    return False


def _spells_the_deployed_qdrant(tree: ast.Module) -> bool:
    return _can_reach_qdrant(tree) and any(
        any(port in _QDRANT_PORTS for port in _loopback_ports(text))
        for text in _folded_strings(tree)
    )


def _aims_a_qdrant_var_at_loopback(text: str) -> bool:
    return any(
        _QDRANT_URL_VAR_RE.search(line) and _loopback_ports(line) for line in text.splitlines()
    )


# --------------------------------------------------------------------------- #
# The rules.
# --------------------------------------------------------------------------- #


def test_no_test_builds_its_own_qdrant_server_client() -> None:
    offenders = {_rel(path) for path in _test_sources() if _addresses_a_qdrant_server(_parse(path))}
    assert offenders == _SANCTIONED_URL_CLIENTS, (
        "a test builds a Qdrant client against a server address. Tests that need a "
        f"real Qdrant must go through {_JANITOR}.OwnedQdrantCollections, which drops "
        "every collection it hands out in fixture teardown — "
        f"unexpected: {sorted(offenders - _SANCTIONED_URL_CLIENTS)}, "
        f"no longer present: {sorted(_SANCTIONED_URL_CLIENTS - offenders)}"
    )


def test_only_two_src_modules_build_a_qdrant_server_client_anywhere() -> None:
    """The room the guard did not patrol — TWICE, one directory apart.

    Round 1: ``tests/`` only, and the accessor had moved to ``src/``. Round 2:
    ``src/**/testing/**`` only, and a review planted ``scratch_vectors.py`` one
    directory up. The patrol is now the whole of ``src/`` and the exceptions are an
    exact set of two named files.
    """
    offenders = {_rel(path) for path in _src_sources() if _addresses_a_qdrant_server(_parse(path))}
    assert offenders == _SANCTIONED_SRC_URL_CLIENTS, (
        "a module under src/ builds a Qdrant client against a server address. Only "
        f"{_JANITOR} (which drops what it creates) and the deployed app itself may — "
        f"unexpected: {sorted(offenders - _SANCTIONED_SRC_URL_CLIENTS)}, "
        f"no longer present: {sorted(_SANCTIONED_SRC_URL_CLIENTS - offenders)}"
    )


def test_no_first_party_source_defaults_the_qdrant_address() -> None:
    """Scanned over ALL first-party Python, not just ``tests/``.

    The first cut of this rule scanned test paths only, and the accessor it exists
    to protect had just moved into ``src/`` — so the exact 2026-09-10 expression
    could be restored in the one file that matters and this stayed green.
    """
    offenders = sorted(
        _rel(path) for path in _first_party_sources() if _defaults_a_qdrant_address(_parse(path))
    )
    assert offenders == [], (
        "a Qdrant URL env var must never carry a non-empty default: a default is how "
        "an unattended run ends up writing to the DEPLOYED store. "
        f"Offenders: {offenders}"
    )


def test_no_qdrant_capable_source_spells_the_deployed_address() -> None:
    """A module that can build a Qdrant client may not contain its address.

    Catches the fallback that dodges the ``get(..., default)`` shape by splitting
    itself up — ``_URL = os.environ.get("NE_QDRANT_URL")`` followed by
    ``if not _URL: _URL = "http://" + "localhost" + ":6333"``.
    """
    offenders = sorted(
        _rel(path)
        for path in _test_sources() + _src_sources()
        if _spells_the_deployed_qdrant(_parse(path))
    )
    assert offenders == [], (
        "a module that imports the Qdrant client or the janitor also spells a "
        "loopback Qdrant address. On a box running the stack that address IS the "
        f"deployed vector store; ask {_JANITOR}.qdrant_server_url() instead. "
        f"Offenders: {offenders}"
    )


def test_no_recipe_aims_a_qdrant_var_at_a_loopback_service() -> None:
    """A documented recipe naming the deployed port re-arms every gate above it."""
    offenders = {
        _rel(path)
        for path in _scanned_prose()
        if _aims_a_qdrant_var_at_loopback(path.read_text(encoding="utf-8"))
    }
    assert offenders == _SANCTIONED_LOOPBACK_RECIPES, (
        "a Qdrant URL var is named beside a loopback address. On a box running the "
        "stack that address IS the deployed vector store, and following the "
        "instructions is what re-armed the OPA outage after its consent gate held — "
        f"unexpected: {sorted(offenders - _SANCTIONED_LOOPBACK_RECIPES)}, "
        f"no longer present: {sorted(_SANCTIONED_LOOPBACK_RECIPES - offenders)}"
    )


# --------------------------------------------------------------------------- #
# Non-vacuity: the sanctioned path really is safe, proved by calling it.
# --------------------------------------------------------------------------- #


def test_an_unnamed_address_is_refused_by_every_door(monkeypatch: pytest.MonkeyPatch) -> None:
    """⭐ THE STRUCTURAL HALF. A defaulted address is one the environment does not
    hold — so with the environment empty, no door in the janitor opens onto one.

    This is what makes the source rules a second line of defence instead of the
    only one: however a fallback is spelled, in whatever module, the address it
    produces is not named, and naming is the consent.
    """
    from ne.brain.testing.qdrant_live import (
        OwnedQdrantCollections,
        UnnamedQdrantServer,
        operator_named_servers,
        qdrant_server_url,
        server_reachable,
        sweep_named_server,
    )

    for name in list(__import__("os").environ):
        if _QDRANT_URL_VAR_RE.fullmatch(name):
            monkeypatch.delenv(name, raising=False)

    assert operator_named_servers() == frozenset()
    assert qdrant_server_url() is None

    # The deployed store's own address, handed to each door explicitly.
    deployed = "http://" + "localhost" + ":6333"
    assert server_reachable(deployed) is False
    assert server_reachable(None) is False
    with pytest.raises(UnnamedQdrantServer):
        OwnedQdrantCollections(deployed)
    assert sweep_named_server() == []

    # And an empty value is not a naming either.
    monkeypatch.setenv("NE_QDRANT_URL", "")
    assert qdrant_server_url() is None
    with pytest.raises(UnnamedQdrantServer):
        OwnedQdrantCollections(deployed)


def test_a_malformed_named_address_parks_rather_than_erroring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd ``NE_QDRANT_URL`` is a parked skip, not a collection error.

    ``QdrantClient(url=...)`` parses eagerly and raises ``ValueError: Unknown
    scheme``; raised from ``server_reachable`` it turns every module-level park in
    this package into a COLLECTION ERROR, which under ``-n auto`` is the "Different
    tests were collected between gw0 and gwN" flake class.
    """
    monkeypatch.setenv("NE_QDRANT_URL", "ftp://nowhere")
    from ne.brain.testing.qdrant_live import qdrant_server_url, server_reachable

    assert server_reachable(qdrant_server_url()) is False


def test_the_janitor_registers_before_it_creates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-vacuity: the sanctioned path must actually own what it hands out.

    An empty allowlist above is only a real rule if the ONE alternative it points
    at genuinely tears down. ``collection()`` must register the name it returns.
    """
    dead = "http://127.0.0.1:1"
    monkeypatch.setenv("NE_QDRANT_URL", dead)
    from ne.brain.testing.qdrant_live import SWEEPABLE_COLLECTION_PREFIXES, OwnedQdrantCollections

    owner = OwnedQdrantCollections(dead)
    first = owner.collection("guardian")
    second = owner.collection("guardian")
    assert first != second
    assert owner.pending == (first, second)

    # A prefix the session sweep would not recognise is refused at mint time, so
    # the sweep can never fall behind the tests it is supposed to recover from.
    with pytest.raises(ValueError, match="SWEEPABLE_COLLECTION_PREFIXES"):
        owner.collection("kit_but_new")
    assert "guardian" in SWEEPABLE_COLLECTION_PREFIXES


def test_the_janitor_forgets_a_name_only_when_the_delete_SUCCEEDED(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⭐ THE ORPHAN MECHANISM ITSELF, ASSERTED AGAINST.

    The shape this replaces drained the registry BEFORE attempting anything and
    suppressed every error including the whole ``asyncio.run``. So a teardown that
    could not reach the server — a transient timeout, or the very ``status: red``
    store this exists to prevent — left the collections behind, forgot their names,
    and reported success. That is precisely how 985 of them accumulated.

    ``http://127.0.0.1:1`` refuses the connection, so the deletes really fail.
    """
    dead = "http://127.0.0.1:1"
    monkeypatch.setenv("NE_QDRANT_URL", dead)
    import ne.brain.testing.qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import OwnedQdrantCollections

    # This test leaks ON PURPOSE; keep the deliberate leak out of the process-wide
    # retry registry so the session hook does not report it as a real one.
    monkeypatch.setattr(janitor_module, "_LEAKING_OWNERS", [])
    owner = OwnedQdrantCollections(dead)
    names = (owner.collection("kit"), owner.collection("spine"))

    with caplog.at_level(logging.WARNING, logger="ne.brain.testing.qdrant_live"):
        owner.drop_all()

    assert owner.pending == names, "a failed delete must NOT drain the registry"
    assert any(name in caplog.text for name in names), (
        "a teardown that leaked collections must SAY SO — silence is what let this run for weeks"
    )


def test_the_janitor_deletes_every_collection_it_handed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-vacuity, at the vendor seam: ``drop_all`` really issues the deletes.

    Mocked AT the ``qdrant_client`` seam and no further — the janitor's own logic
    runs for real. Without a server there is no other way to prove the teardown
    does anything, and "the registry emptied" alone would be a claim about a list.
    """
    monkeypatch.setenv("NE_QDRANT_URL", "http://127.0.0.1:1")
    import ne.brain.testing.qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import OwnedQdrantCollections

    deleted: list[str] = []
    closed: list[bool] = []

    class _RecordingClient:
        def __init__(self, url: str, check_compatibility: bool = True) -> None:
            self.url = url

        async def delete_collection(self, name: str) -> bool:
            deleted.append(name)
            # The vendor is annotated ``-> bool``; a fake that returns None would
            # be a fake of an API that does not exist.
            return True

        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(janitor_module, "AsyncQdrantClient", _RecordingClient)
    owner = OwnedQdrantCollections("http://127.0.0.1:1")
    names = [owner.collection("kit"), owner.collection("spine")]
    owner.drop_all()
    # ...and a second call is a no-op, not a second round of deletes.
    owner.drop_all()

    assert deleted == names
    assert closed == [True]
    assert owner.pending == ()


def test_the_sweep_can_only_ever_be_aimed_at_the_named_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recovery path for teardown that never ran — and its blast radius.

    A killed xdist worker, a cancelled CI job and a ``SIGKILL`` all bypass
    ``finally``, so ``drop_all`` alone cannot be the whole answer. The sweep is,
    and the two things that make it safe are asserted here: it takes NO address
    argument (so it can never be pointed at a discovered store), and it deletes
    only ``<sanctioned prefix>_<uuid4 hex>`` — never ``ne_chunks``, the one real
    collection that shared the store with the 985 orphans.
    """
    import ne.brain.testing.qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import sweep_named_server

    seen: list[str] = []

    class _Collection:
        def __init__(self, name: str) -> None:
            self.name = name

    class _Collections:
        def __init__(self, names: list[str]) -> None:
            self.collections = [_Collection(n) for n in names]

    present = [
        "ne_chunks",
        "kit_" + "0" * 32,
        "spine_" + "a" * 32,
        "rag_public_live_" + "b" * 32,
        "kw_" + "c" * 32,
        "kit_not_a_uuid",
        "customer_prod_vectors",
    ]

    class _RecordingClient:
        def __init__(self, url: str, timeout: int = 0, check_compatibility: bool = True) -> None:
            self.url = url

        def get_collections(self) -> _Collections:
            return _Collections(present)

        def delete_collection(self, name: str) -> bool:
            seen.append(name)
            return True

        def close(self) -> None:
            return None

    monkeypatch.setattr(janitor_module, "QdrantClient", _RecordingClient)

    # No server named: no client is even built, and nothing is deleted.
    monkeypatch.delenv("NE_QDRANT_URL", raising=False)
    assert sweep_named_server() == []
    assert seen == []

    monkeypatch.setenv("NE_QDRANT_URL", "http://127.0.0.1:1")
    dropped = sweep_named_server()
    assert seen == sorted(dropped) or set(seen) == set(dropped)
    assert set(dropped) == {
        "kit_" + "0" * 32,
        "spine_" + "a" * 32,
        "rag_public_live_" + "b" * 32,
        "kw_" + "c" * 32,
    }
    assert "ne_chunks" not in seen
    assert "customer_prod_vectors" not in seen
    assert "kit_not_a_uuid" not in seen


# --------------------------------------------------------------------------- #
# Round 3: the four defects the second review round found in the repair itself.
# --------------------------------------------------------------------------- #

#: Everything the OPERATOR deploys. Whatever Qdrant variables live in here
#: configure the running application, and must therefore never be able to grant a
#: TEST consent — see :func:`test_the_deployed_configurations_variables_are_not_test_consent`.
_DEPLOY_GLOBS = ("deploy/**/*",)


def _deployed_qdrant_url_vars() -> set[str]:
    """Every ``NE_*QDRANT_URL`` variable the ``deploy/`` tree configures."""
    found: set[str] = set()
    for path in sorted(_glob(_DEPLOY_GLOBS)):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        found.update(_QDRANT_URL_VAR_RE.findall(text))
    return found


def test_the_deployed_configurations_variables_are_not_test_consent() -> None:
    """⭐ PRODUCTION CONFIGURATION IS NOT A TEST'S PERMISSION TO WRITE.

    The first repair accepted every ``NE_*QDRANT_URL`` variable as consent. That
    set includes ``NE_API_QDRANT_URL`` — the DEPLOYED APP'S OWN setting, which on
    this box holds the live store's address. So a run carrying nothing but
    production configuration got its consent granted by production configuration,
    and ``OwnedQdrantCollections`` opened onto the store the 985 orphans landed in
    while ``qdrant_server_url()`` correctly answered ``None``.

    Derived from ``deploy/**`` rather than enumerated, so adding a Qdrant variable
    to the deployment and then also listing it as test consent fails HERE.
    """
    from ne.brain.testing.qdrant_live import TEST_CONSENT_URL_VARS

    deployed = _deployed_qdrant_url_vars()
    assert deployed, (
        "no NE_*QDRANT_URL variable found anywhere under deploy/ — this rule has "
        "gone vacuous and would pass whatever the consent set contained"
    )
    overlap = sorted(deployed & set(TEST_CONSENT_URL_VARS))
    assert overlap == [], (
        "a variable the DEPLOYED application is configured with is being treated as "
        f"test consent: {overlap}. Consent must mean a variable that exists in order "
        "to grant test consent; anything else lets production configuration point a "
        "test at production."
    )


def test_a_production_qdrant_variable_alone_opens_no_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same rule, proved by CALLING the doors with only production config set.

    ``qdrant_server_url()`` already returned ``None`` here — and
    ``OwnedQdrantCollections`` accepted the address anyway. That gap is the whole
    defect, so it is asserted at the door, not at the constant.
    """
    from ne.brain.testing.qdrant_live import (
        OwnedQdrantCollections,
        UnnamedQdrantServer,
        operator_named_servers,
        qdrant_server_url,
        server_reachable,
        sweep_named_server,
    )

    for name in list(__import__("os").environ):
        if _QDRANT_URL_VAR_RE.fullmatch(name):
            monkeypatch.delenv(name, raising=False)

    deployed = "http://" + "localhost" + ":6333"
    monkeypatch.setenv("NE_API_QDRANT_URL", deployed)

    assert operator_named_servers() == frozenset()
    assert qdrant_server_url() is None
    assert server_reachable(deployed) is False
    with pytest.raises(UnnamedQdrantServer):
        OwnedQdrantCollections(deployed)
    assert sweep_named_server() == []


#: ``<source>`` -> is it a SERVER client (i.e. an offender)?
#:
#: Every one of these is a real vendor behaviour, checked against the installed
#: package: ``location``/``path``/``url``/``host`` are branched on by the
#: constructor, and everything that is not ``":memory:"`` or a non-``None``
#: ``path`` ends up in ``AsyncQdrantRemote`` — default ``http://localhost:6333``.
_CLIENT_FORMS: tuple[tuple[str, bool], ...] = (
    ('AsyncQdrantClient(":memory:")', False),
    ('AsyncQdrantClient(location=":memory:")', False),
    ('AsyncQdrantClient(path="/var/tmp/q")', False),
    ('AsyncQdrantClient(path=f"{tmp}/q")', False),
    ("AsyncQdrantClient(path=None)", True),
    ("AsyncQdrantClient(path=maybe_none)", True),
    ("AsyncQdrantClient()", True),
    ("AsyncQdrantClient(location=None)", True),
    ('AsyncQdrantClient(url="http://elsewhere:6333")', True),
    ('AsyncQdrantClient(host="elsewhere", port=6333)', True),
    ("AsyncQdrantClient(**opts)", True),
)


@pytest.mark.parametrize(("call", "is_server"), _CLIENT_FORMS)
def test_the_client_rule_classifies_every_vendor_construction(call: str, is_server: bool) -> None:
    """⭐ ``path=None`` IS THE DEPLOYED STORE, AND THE RULE USED TO CALL IT LOCAL.

    The vendor branches on ``elif path is not None``, so ``path=None`` falls
    through to ``AsyncQdrantRemote(url=None, host=None)`` — ``localhost:6333``,
    the identical endpoint as the bare ``AsyncQdrantClient()`` this rule already
    refuses. Matching the mere PRESENCE of the keyword reported it safe, which
    made ``path=None`` a one-token way past the guardian.
    """
    source = f"from {_VENDOR} import AsyncQdrantClient\nc = {call}\n"
    assert _addresses_a_qdrant_server(ast.parse(source)) is is_server


def test_the_vendors_own_branch_confirms_path_none_is_remote() -> None:
    """Non-vacuity for the rule above: asserted against the INSTALLED vendor.

    A guardian that encodes a belief about a third-party constructor has to check
    the belief, or it is guessing. This constructs both forms and reads which
    implementation the vendor chose. ``check_compatibility=False`` so not even a
    version handshake can leave the process: on this box the endpoint the wrong
    branch resolves to is the DEPLOYED store.
    """
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.local.async_qdrant_local import AsyncQdrantLocal

    remote = AsyncQdrantClient(path=None, check_compatibility=False)
    assert not isinstance(remote._client, AsyncQdrantLocal), (
        "the vendor branches on `elif path is not None`, so path=None is REMOTE"
    )
    assert isinstance(AsyncQdrantClient(_LOCAL_MODE)._client, AsyncQdrantLocal)


def test_a_delete_the_server_REJECTED_is_kept_and_reported(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⭐ THE SAME FORGET-A-LIVE-COLLECTION BUG, ONE SEAM OVER.

    ``delete_collection`` is annotated ``-> bool`` and RETURNS ``False`` when the
    server declines — it does not raise. The repair fixed the raising path and
    ignored the return value, so a declined delete still drained the registry,
    ``.pending`` still came back empty, and the janitor still reported success
    over a collection that is still there.
    """
    monkeypatch.setenv("NE_QDRANT_URL", "http://127.0.0.1:1")
    import ne.brain.testing.qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import OwnedQdrantCollections

    class _RejectingClient:
        def __init__(self, url: str, check_compatibility: bool = True) -> None:
            self.url = url

        async def delete_collection(self, name: str) -> bool:
            return not name.startswith("kit_")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(janitor_module, "AsyncQdrantClient", _RejectingClient)
    monkeypatch.setattr(janitor_module, "_LEAKING_OWNERS", [])  # deliberate leak
    owner = OwnedQdrantCollections("http://127.0.0.1:1")
    rejected = owner.collection("kit")
    accepted = owner.collection("spine")

    with caplog.at_level(logging.WARNING, logger="ne.brain.testing.qdrant_live"):
        owner.drop_all()

    assert owner.pending == (rejected,), (
        "a delete the server REJECTED must leave the name registered; forgetting it "
        "is exactly how a live collection becomes an orphan nobody is tracking"
    )
    assert accepted not in owner.pending
    assert rejected in caplog.text


def test_the_sweep_does_not_report_a_REJECTED_delete_as_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sweep_named_server`` had the identical defect, and it is the LAST line."""
    import ne.brain.testing.qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import sweep_named_server

    rejected = "kit_" + "0" * 32
    accepted = "spine_" + "a" * 32

    class _Collection:
        def __init__(self, name: str) -> None:
            self.name = name

    class _Collections:
        def __init__(self, names: list[str]) -> None:
            self.collections = [_Collection(n) for n in names]

    class _RejectingClient:
        def __init__(self, url: str, timeout: int = 0, check_compatibility: bool = True) -> None:
            self.url = url

        def get_collections(self) -> _Collections:
            return _Collections([rejected, accepted])

        def delete_collection(self, name: str) -> bool:
            return name != rejected

        def close(self) -> None:
            return None

    monkeypatch.setattr(janitor_module, "QdrantClient", _RejectingClient)
    monkeypatch.setenv("NE_QDRANT_URL", "http://127.0.0.1:1")

    assert sweep_named_server() == [accepted], (
        "the sweep must not count a REJECTED delete as swept: reporting a leak "
        "cleaned up when it is still there is worse than not sweeping at all"
    )


def test_a_failed_teardown_is_actually_retried_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⭐ "KEPT FOR RETRY" IS A CLAIM ABOUT A LIST UNTIL SOMETHING RETRIES.

    Both fixtures called ``drop_all()`` once and discarded the owner, so a name
    left in ``.pending`` died with the fixture. :func:`retry_pending_drops` is the
    thing that comes back for it — and it touches only names THIS process minted,
    which is what makes it safe on an xdist worker (unlike the bulk sweep).
    """
    monkeypatch.setenv("NE_QDRANT_URL", "http://127.0.0.1:1")
    import ne.brain.testing.qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import OwnedQdrantCollections, retry_pending_drops

    attempts: list[str] = []
    reject = {"on": True}

    class _FlakyClient:
        def __init__(self, url: str, check_compatibility: bool = True) -> None:
            self.url = url

        async def delete_collection(self, name: str) -> bool:
            attempts.append(name)
            return not reject["on"]

        async def close(self) -> None:
            return None

    monkeypatch.setattr(janitor_module, "AsyncQdrantClient", _FlakyClient)
    monkeypatch.setattr(janitor_module, "_LEAKING_OWNERS", [])

    owner = OwnedQdrantCollections("http://127.0.0.1:1")
    name = owner.collection("guardian")
    owner.drop_all()
    assert owner.pending == (name,)
    assert [owner] == janitor_module._LEAKING_OWNERS, (
        "an owner that leaked must be reachable after the fixture that made it is "
        "gone, or the retry has nothing to retry"
    )

    # Still failing at session end: reported as leaked, not silently forgotten.
    assert retry_pending_drops() == (name,)
    assert attempts == [name, name]

    # The store recovers; the retry drains it and the owner deregisters itself.
    reject["on"] = False
    assert retry_pending_drops() == ()
    assert owner.pending == ()
    assert janitor_module._LEAKING_OWNERS == []
    assert attempts == [name, name, name]


#: The two conftests that hand out ``OwnedQdrantCollections``. Both must wire the
#: session-end recovery, and both must gate the BULK sweep on being the controller.
_QDRANT_CONFTESTS = (
    Path("packages/ne-brain/tests/conftest.py"),
    Path("packages/ne-eval/tests/conftest.py"),
)


def _called_names(tree: ast.AST) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


@pytest.mark.parametrize("relative", _QDRANT_CONFTESTS, ids=lambda p: str(p))
def test_every_qdrant_conftest_wires_the_session_recovery(relative: Path) -> None:
    """⭐ AN UNCALLED RECOVERY IS NOT A RECOVERY.

    ``sweep_named_server`` existed, was tested, and was invoked from NOWHERE but
    its own unit test — so a SIGKILLed xdist worker leaked forever, which is the
    path by which 985 collections accumulated between green runs. Asserted
    structurally here so deleting the hook fails a test rather than quietly
    restoring the leak.
    """
    path = _REPO_ROOT / relative
    tree = _parse(path)
    hook = _function(tree, "pytest_sessionfinish")
    assert hook is not None, (
        f"{relative} hands out OwnedQdrantCollections but has no pytest_sessionfinish"
    )
    called = _called_names(hook)
    assert "retry_pending_drops" in called, (
        f"{relative}: drop_all keeps a failed name for retry, but nothing in this "
        "conftest ever retries it — the owner dies with the fixture"
    )
    assert "sweep_named_server" in called, (
        f"{relative}: nothing recovers the collections a KILLED worker left behind; "
        "a finally block cannot run in a process that was SIGKILLed"
    )


@pytest.mark.parametrize("relative", _QDRANT_CONFTESTS, ids=lambda p: str(p))
def test_every_qdrant_conftest_sweeps_from_the_CONTROLLER_ONLY(relative: Path) -> None:
    """⛔ THE BLAST RADIUS GUARD, ASSERTED IN THE SOURCE.

    The bulk sweep deletes every ``<prefix>_<uuid4>`` on the named server —
    including the ones a SIBLING xdist worker is using right now. It is safe at
    exactly one moment: the CONTROLLER's ``pytest_sessionfinish``, which runs
    after every worker has exited. ``config.workerinput`` exists only in a worker,
    so the guard must appear, and it must come BEFORE the sweep call.
    """
    path = _REPO_ROOT / relative
    hook = _function(_parse(path), "pytest_sessionfinish")
    assert hook is not None
    body = ast.unparse(hook)
    assert "workerinput" in body, (
        f"{relative}: pytest_sessionfinish sweeps without checking workerinput; from "
        "a worker that deletes its siblings' live collections mid-session"
    )

    guard_line = min(
        node.lineno
        for node in ast.walk(hook)
        if isinstance(node, ast.Constant) and node.value == "workerinput"
    )
    sweep_line = min(
        node.lineno
        for node in ast.walk(hook)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "sweep_named_server"
    )
    assert guard_line < sweep_line, (
        f"{relative}: the workerinput guard must precede sweep_named_server(); after "
        "it, the worker has already deleted its siblings' collections"
    )


# --------------------------------------------------------------------------- #
# Round 3b: the planted offenders an independent review got GREEN past round 2.
# --------------------------------------------------------------------------- #

_ELSEWHERE = "http://elsewhere:6333"

#: Each of these was planted in a real file and every rule passed. They are the
#: binding forms ``_vendor_bindings`` could not resolve, so rule 1's "everything
#: goes through the janitor" — the CLEANUP half of the outage fix — was optional.
_PLANTED_CLIENT_OFFENDERS: tuple[tuple[str, str], ...] = (
    (
        "subclass",
        "from qdrant_client import AsyncQdrantClient\n"
        "class _Scratch(AsyncQdrantClient):\n    pass\n"
        f'c = _Scratch(url="{_ELSEWHERE}")\n',
    ),
    (
        "subclass of a subclass",
        "from qdrant_client import QdrantClient\n"
        "class _A(QdrantClient):\n    pass\n"
        "class _B(_A):\n    pass\n"
        f'c = _B(url="{_ELSEWHERE}")\n',
    ),
    (
        "importlib, inline",
        "import importlib\n"
        'c = importlib.import_module("qdrant_client")'
        f'.AsyncQdrantClient(url="{_ELSEWHERE}")\n',
    ),
    (
        "importlib, bound to a name",
        "import importlib\n"
        'm = importlib.import_module("qdrant_client")\n'
        f'c = m.QdrantClient(url="{_ELSEWHERE}")\n',
    ),
    (
        "importlib, class bound to a name",
        "import importlib\n"
        'C = importlib.import_module("qdrant_client").AsyncQdrantClient\n'
        f'c = C(url="{_ELSEWHERE}")\n',
    ),
    (
        "__import__",
        f'c = __import__("qdrant_client").QdrantClient(url="{_ELSEWHERE}")\n',
    ),
)


@pytest.mark.parametrize(
    ("label", "source"), _PLANTED_CLIENT_OFFENDERS, ids=[p[0] for p in _PLANTED_CLIENT_OFFENDERS]
)
def test_a_planted_server_client_is_caught_however_the_vendor_was_bound(
    label: str, source: str
) -> None:
    """⭐ SIX FORMS THAT BOUND THE VENDOR WITHOUT AN IMPORT THE RULE COULD READ.

    The subclass is the one that matters most: rule 1 exists to force every server
    client through ``OwnedQdrantCollections`` so its collections are registered for
    teardown, and a two-line subclass bypassed the janitor completely — which is
    the mechanism that put 985 collections in the live store, not merely the
    address that aimed them.
    """
    assert _addresses_a_qdrant_server(ast.parse(source)) is True, (
        f"planted offender {label!r} was reported harmless"
    )


def test_an_in_tree_fake_client_is_still_not_an_offender() -> None:
    """The other direction: widening must not start flagging the in-tree fakes.

    ``_FakeAsyncQdrantClient`` is bound to nothing vendor. If this ever fails, the
    rule has gone back to matching NAMES, which is what forced the over-broad
    "no arguments means a fake" escape hatch that let the outage form through.
    """
    source = (
        "class _FakeAsyncQdrantClient:\n    pass\n"
        'c = _FakeAsyncQdrantClient(url="http://elsewhere:6333")\n'
    )
    assert _addresses_a_qdrant_server(ast.parse(source)) is False


def test_the_scan_surface_covers_src_outside_the_testing_package() -> None:
    """Non-vacuity for the widened globs — the planted ``scratch_vectors.py`` room.

    ``packages/ne-api/src/ne/api/platform.py`` is not under any ``testing/``
    directory, so its presence proves rule 1 now patrols ordinary ``src/``.
    """
    scanned = {_rel(path) for path in _src_sources()}
    assert _APP_PLATFORM_PATH in scanned
    assert not any("testing" in part for part in _APP_PLATFORM_PATH.parts)
    assert _JANITOR_PATH in scanned


def test_a_fallback_split_across_two_names_is_still_the_deployed_address() -> None:
    """⭐ ONE EXTRA VARIABLE USED TO HIDE THE WHOLE ADDRESS.

    ``"http://" + "local" + "host:6333"`` was folded and caught; ``_A + _B`` with
    the halves bound to names folded to ``None`` and the rule went quiet. The
    ``import ... as`` form is here too: rule 3 read ``ImportFrom`` only, so this
    module did not even count as one that could reach a Qdrant.
    """
    source = (
        "import ne.brain.testing.qdrant_live as j\n"
        '_A = "http://local"\n'
        '_B = "host:6333"\n'
        "URL = j.qdrant_server_url() or _A + _B\n"
    )
    tree = ast.parse(source)
    assert _can_reach_qdrant(tree) is True
    assert _spells_the_deployed_qdrant(tree) is True


@pytest.mark.parametrize(
    "source",
    (
        '_A = "http://local"\n_B = "host:6333"\nos.environ["NE_QDRANT_URL"] = _A + _B\n',
        'os.environ.setdefault("NE_QDRANT_URL", _A + _B)\n',
        'os.environ.setdefault("NE_TEST_QDRANT_URL", "http://elsewhere:6333")\n',
        'os.putenv("NE_QDRANT_URL", url)\n',
        'os.environ.update({"NE_QDRANT_URL": url})\n',
    ),
)
def test_a_planted_write_of_a_consent_variable_is_caught(source: str) -> None:
    """The forged-naming forms. Each one made every door in the janitor open."""
    assert _consent_var_writes(ast.parse(source)), f"planted write went unseen: {source!r}"


def test_writing_the_deployed_apps_own_variable_is_not_a_consent_write() -> None:
    """...and the rule stays narrow: ``NE_API_QDRANT_URL`` is not consent to forge.

    ``packages/ne-api/tests/test_beat3_governed_draft_email_chain.py`` legitimately
    sets it to a fake host to configure the app under test. Flagging that would be
    a false alarm, and false alarms are how allowlists get repopulated.
    """
    source = 'monkeypatch.setenv("NE_API_QDRANT_URL", "http://qdrant.test")\n'
    assert _consent_var_writes(ast.parse(source)) == []


# --------------------------------------------------------------------------- #
# Round 4: the source scan lost. The guarantee is a RUNTIME REFUSAL now.
# --------------------------------------------------------------------------- #

#: A deployment address that resolves NOWHERE. Every test below is about which
#: addresses get REFUSED, so none of them may be able to connect to anything even
#: if a rule regressed — and ``.invalid`` is reserved by RFC 2606 for exactly that,
#: which is why :func:`_is_provably_dead` accepts the whole family.
_DEPLOYED = "http://qdrant.invalid:6333"

#: The round-4 bypass, verbatim. Two indirections at once: the variable being
#: written is a ``Name`` (so the write rule sees no consent variable), and the value
#: is COPIED out of the deployed application's own production setting (so no address
#: is ever spelled). Measured against round 3: all four source rules returned
#: ``False`` and ``OwnedQdrantCollections`` accepted the result.
_ROUND_FOUR_BYPASS = (
    "import os\n"
    "from ne.brain.testing.qdrant_live import OwnedQdrantCollections, qdrant_server_url\n"
    'KEY = "NE_QDRANT_URL"\n'
    'os.environ[KEY] = os.environ["NE_API_QDRANT_URL"]\n'
    "owner = OwnedQdrantCollections(qdrant_server_url())\n"
)


@pytest.fixture(autouse=True)
def _no_ambient_deployment_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub deployment Qdrant variables the LAUNCHING SHELL happened to export.

    The runtime refusal reads the environment, so a developer who exports
    ``NE_API_QDRANT_URL`` would otherwise change the outcome of tests in this file
    that have nothing to do with it — the same class of local-only trap as the umask
    and locale ones, where the output is evidence of nothing. Tests that need a
    deployment variable set it themselves, and an autouse fixture runs first, so they
    win.
    """
    from ne.brain.testing.qdrant_live import TEST_CONSENT_URL_VARS

    for name in list(os.environ):
        if "QDRANT" in name and name not in TEST_CONSENT_URL_VARS:
            monkeypatch.delenv(name, raising=False)


def test_the_round_four_bypass_still_defeats_every_source_rule() -> None:
    """⭐ THE HONEST HALF: THE SOURCE RULES DO NOT CATCH IT, AND ARE NOT CLAIMED TO.

    This file has claimed completeness three times and been wrong three times, each
    time because a reviewer found one more indirection a scan could not read. So this
    asserts the LIMIT rather than a fix: every source rule returns ``False`` on the
    bypass, that is expected and permanent, and it is the reason the guarantee moved
    to :func:`test_the_round_four_bypass_is_refused_at_runtime` below.

    Extending the write rule to cover an indirect subscript would mean flagging every
    ``os.environ[name] = value`` in the repo, which is how allowlists get repopulated
    and how a rule stops being read. The source rules stay the cheap EARLY signal they
    are good at being.
    """
    tree = ast.parse(_ROUND_FOUR_BYPASS)
    assert _addresses_a_qdrant_server(tree) is False
    assert _consent_var_writes(tree) == []
    assert _defaults_a_qdrant_address(tree) is False
    assert _spells_the_deployed_qdrant(tree) is False


def test_the_deployment_variable_set_is_derived_from_the_deploy_tree() -> None:
    """The refusal's input is read off ``deploy/**``, and it is not vacuous.

    Same reasoning as ``test_the_deployed_configurations_variables_are_not_test_
    consent``: a hand-written list is a list of what someone remembered, and this
    module has been defeated four times by something nobody remembered.
    """
    from ne.brain.testing.qdrant_live import (
        TEST_CONSENT_URL_VARS,
        deployment_qdrant_url_vars,
    )

    derived = deployment_qdrant_url_vars()
    assert "NE_API_QDRANT_URL" in derived, (
        "the deployed app's own Qdrant variable is not being derived from deploy/ — "
        f"the runtime refusal has gone vacuous. Derived: {sorted(derived)}"
    )
    assert derived & set(TEST_CONSENT_URL_VARS) == set(), (
        "a consent variable is being treated as a deployment variable; the janitor "
        "would refuse its own consent"
    )
    narrow = _deployed_qdrant_url_vars() - set(TEST_CONSENT_URL_VARS)
    assert narrow <= derived, (
        "this guardian derives a deployment variable the janitor's own scan misses: "
        f"{sorted(narrow - derived)}"
    )


def test_a_new_deployment_variable_is_refused_without_editing_the_janitor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """⭐ DERIVATION, PROVED BY ADDING ONE — with a name no pattern would match.

    ``NE_API_VECTOR_ENDPOINT`` is not spelled ``*QDRANT*URL*``, so the environment
    pattern that backstops the refusal cannot see it. Only reading ``deploy/**`` can,
    which is what makes the derivation load-bearing rather than decorative: add a
    vector-store address to a chart and it is refused in the same commit, with no
    edit to the janitor.
    """
    from ne.brain.testing import qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import (
        DeploymentQdrantServer,
        OwnedQdrantCollections,
        deployment_qdrant_url_vars,
    )

    fake_deploy = tmp_path / "deploy"
    fake_deploy.mkdir()
    (fake_deploy / "values.yaml").write_text(
        "env:\n  NE_API_VECTOR_ENDPOINT: http://vectors.invalid:6333\n", encoding="utf-8"
    )
    monkeypatch.setattr(janitor_module, "_repo_root", lambda: tmp_path)
    janitor_module._qdrant_url_vars_under.cache_clear()
    try:
        assert "NE_API_VECTOR_ENDPOINT" in deployment_qdrant_url_vars()
        monkeypatch.setenv("NE_API_VECTOR_ENDPOINT", "http://vectors.invalid:6333")
        monkeypatch.setenv("NE_QDRANT_URL", "http://vectors.invalid:6333")
        with pytest.raises(DeploymentQdrantServer):
            OwnedQdrantCollections("http://vectors.invalid:6333")
    finally:
        janitor_module._qdrant_url_vars_under.cache_clear()


def test_the_round_four_bypass_is_refused_at_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """⛔ THE LOAD-BEARING RULE. The naming is genuine and the address is refused.

    The bypass copies the deployed application's own address into a consent variable,
    so ``operator_named_servers()`` truthfully reports that the operator named it —
    every naming check from round 3 is satisfied. It does not matter: the janitor now
    compares the ADDRESS against what the deployment is configured with, so the
    refusal cannot be routed around by a new spelling, a new variable name, or an
    indirection nobody has written yet.

    ``KEY`` is a ``Name`` node here, as planted, and the assertion below is the
    "copied out of production" step made explicit — this guardian may not point a
    consent variable at anything that is not provably dead
    (:func:`test_this_guardian_never_points_a_consent_variable_at_a_real_server`),
    so the value is a literal that both variables are set to rather than a read of
    one at runtime.
    """
    from ne.brain.testing.qdrant_live import (
        DeploymentQdrantServer,
        OwnedQdrantCollections,
        operator_named_servers,
        qdrant_server_url,
        server_reachable,
        sweep_named_server,
    )

    key = "NE_QDRANT_URL"  # an indirect NAME node, exactly as planted
    monkeypatch.setenv("NE_API_QDRANT_URL", _DEPLOYED)
    monkeypatch.setenv(key, _DEPLOYED)
    assert os.environ[key] == os.environ["NE_API_QDRANT_URL"]

    # Consent by naming is SATISFIED — that is the whole point of this test.
    assert operator_named_servers() == frozenset({_DEPLOYED})

    # ...and every door is shut anyway.
    assert qdrant_server_url() is None
    assert server_reachable(os.environ[key]) is False
    assert sweep_named_server() == []
    with pytest.raises(DeploymentQdrantServer) as raised:
        OwnedQdrantCollections(os.environ[key])
    assert "NE_API_QDRANT_URL" in str(raised.value)


def test_a_deployment_address_in_another_spelling_is_refused_at_the_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end proof that the comparison normalises: scheme-less consent.

    Round 1 already found a scheme-less ``NE_QDRANT_URL`` reaching the deployed
    store, so an equality check that a missing ``http://`` defeats is the same defect
    one spelling over. The exhaustive spelling matrix is
    :data:`_SAME_STORE_SPELLINGS` below; this is the one that goes through the real
    door, and it can use a provably dead address to do it.
    """
    from ne.brain.testing.qdrant_live import (
        DeploymentQdrantServer,
        OwnedQdrantCollections,
        qdrant_server_url,
    )

    monkeypatch.setenv("NE_API_QDRANT_URL", "http://qdrant.invalid:6333")
    monkeypatch.setenv("NE_QDRANT_URL", "qdrant.invalid:6333")

    assert qdrant_server_url() is None
    with pytest.raises(DeploymentQdrantServer):
        OwnedQdrantCollections("qdrant.invalid:6333")


#: ``(what the deployment holds, what a consent variable holds)`` — the SAME store
#: both times, and therefore refused both times. Exercised against the refusal
#: itself rather than through a consent variable, so this guardian never arms one
#: with a value its own source cannot prove dead.
_SAME_STORE_SPELLINGS: tuple[tuple[str, str], ...] = (
    ("http://qdrant.invalid:6333", "http://qdrant.invalid:6333/"),
    ("http://qdrant.invalid:6333/", "http://qdrant.invalid:6333"),
    ("http://qdrant.invalid:6333", "qdrant.invalid:6333"),
    ("qdrant.invalid:6333", "http://qdrant.invalid:6333"),
    ("http://qdrant.invalid:6333", "http://QDRANT.INVALID:6333"),
    ("http://qdrant.invalid:6333", "  http://qdrant.invalid:6333  "),
    ("http://qdrant.invalid", "http://qdrant.invalid:80"),
    ("https://qdrant.invalid", "https://qdrant.invalid:443"),
    ("http://127.0.0.1:6399", "http://localhost:6399"),
    ("http://localhost:6399", "http://127.0.0.1:6399"),
)

#: ...and the other direction: genuinely different stores, which must NOT be
#: confused for the deployment or the live-parked proofs stop running.
_DIFFERENT_STORE_SPELLINGS: tuple[tuple[str, str], ...] = (
    ("http://qdrant.invalid:6333", "http://qdrant.invalid:6334"),
    ("http://qdrant.invalid:6333", "http://other.invalid:6333"),
    ("http://qdrant.invalid:6333", "http://qdrant.invalid:6333/tenant"),
    ("http://qdrant.invalid:6333/tenant", "http://qdrant.invalid:6333"),
    ("https://qdrant.invalid", "https://qdrant.invalid:6333"),
)


@pytest.mark.parametrize(("deployed", "candidate"), _SAME_STORE_SPELLINGS)
def test_the_same_store_in_another_spelling_is_still_the_deployment(
    monkeypatch: pytest.MonkeyPatch, deployed: str, candidate: str
) -> None:
    """Normalise before comparing, or a trailing slash is a way past the refusal."""
    from ne.brain.testing import qdrant_live as janitor_module

    monkeypatch.setenv("NE_API_QDRANT_URL", deployed)
    assert janitor_module._deployment_refusal(candidate) is not None, (
        f"{candidate!r} is the deployment at {deployed!r} wearing another spelling, "
        "and the refusal did not recognise it"
    )


@pytest.mark.parametrize(("deployed", "candidate"), _DIFFERENT_STORE_SPELLINGS)
def test_a_genuinely_different_store_is_not_mistaken_for_the_deployment(
    monkeypatch: pytest.MonkeyPatch, deployed: str, candidate: str
) -> None:
    """The mutation in the other direction. A refusal that refuses everything is a
    broken janitor, not a safe one: the live-parked proofs must still be able to run
    against a store of their own."""
    from ne.brain.testing import qdrant_live as janitor_module

    monkeypatch.setenv("NE_API_QDRANT_URL", deployed)
    assert janitor_module._deployment_refusal(candidate) is None, (
        f"{candidate!r} is not the deployment at {deployed!r} and was refused anyway"
    )


def test_a_loopback_store_is_refused_while_a_deployment_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⭐ ONE STORE, TWO ADDRESSES — the published-port hole in the equality rule.

    The deployed app addresses Qdrant by its compose-network name; the host addresses
    the SAME container on the port it publishes. Comparing values alone would refuse
    the first spelling and wave the second through, so when any deployment Qdrant
    variable is present at all, a loopback Qdrant is presumed to be that deployment.

    The door-level half uses ``http://127.0.0.1:1`` — a provably dead loopback — and
    the same address is ACCEPTED by
    :func:`test_a_loopback_store_is_addressable_when_no_deployment_is_configured`
    below. That pair is the whole discriminator: the environment, never the address.
    """
    from ne.brain.testing import qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import (
        DeploymentQdrantServer,
        OwnedQdrantCollections,
        qdrant_server_url,
    )

    monkeypatch.setenv("NE_API_QDRANT_URL", "http://qdrant:6333")

    # The published-port spelling of the live demo store, never armed as consent.
    refusal = janitor_module._deployment_refusal("http://" + "localhost" + ":6333")
    assert refusal is not None and "LOOPBACK" in refusal

    monkeypatch.setenv("NE_QDRANT_URL", "http://127.0.0.1:1")
    assert qdrant_server_url() is None
    with pytest.raises(DeploymentQdrantServer) as raised:
        OwnedQdrantCollections("http://127.0.0.1:1")
    assert "LOOPBACK" in str(raised.value)


def test_a_loopback_store_is_addressable_when_no_deployment_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⭐ THE REGRESSION THIS RULE COULD EASILY HAVE CAUSED, ASSERTED SO IT CANNOT.

    ``.github/workflows/ci.yml`` points ``NE_QDRANT_URL`` at the job's OWN Qdrant
    service container, which answers on a loopback address — the same address that on
    this box is the deployed demo store. **No address literal can tell those two
    apart.** The environment can, and that is the entire discriminator: CI sets no
    deployment variable, so nothing is refused there; a box running the stack does,
    so it is.

    Refusing a hardcoded loopback address instead would have taken out the promoted
    source-authority gate and every live-parked proof, which is why no such literal
    exists anywhere in the janitor.

    ⛔ NO SOCKET IS OPENED HERE. ``OwnedQdrantCollections`` performs no I/O in its
    constructor and ``collection()`` only registers a name; ``server_reachable`` is
    the one door that connects, and it is not called.
    """
    from ne.brain.testing import qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import (
        OwnedQdrantCollections,
        deployment_qdrant_addresses,
        qdrant_server_url,
    )

    monkeypatch.setenv("NE_QDRANT_URL", "http://127.0.0.1:1")

    assert deployment_qdrant_addresses() == {}
    assert janitor_module._deployment_refusal("http://" + "localhost" + ":6333") is None
    assert qdrant_server_url() == "http://127.0.0.1:1"
    owner = OwnedQdrantCollections("http://127.0.0.1:1")
    assert owner.collection("kit") in owner.pending


def test_a_consent_address_that_is_not_the_deployments_still_opens_every_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-vacuity with a deployment variable actually present: a DIFFERENT store is
    still the operator's to name.

    ⛔ NO SOCKET IS OPENED HERE — constructor and name registration only, and
    ``.invalid`` resolves nowhere in any case.
    """
    from ne.brain.testing.qdrant_live import OwnedQdrantCollections, qdrant_server_url

    elsewhere = "http://scratch.invalid:6333"
    monkeypatch.setenv("NE_API_QDRANT_URL", _DEPLOYED)
    monkeypatch.setenv("NE_QDRANT_URL", elsewhere)

    assert qdrant_server_url() == elsewhere
    owner = OwnedQdrantCollections(elsewhere)
    assert owner.url == elsewhere
    assert owner.collection("kit") in owner.pending


@pytest.mark.parametrize("malformed", ("http://h:notaport", "http://", "://x", "?", ""))
def test_a_malformed_address_is_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, malformed: str
) -> None:
    """The refusal runs at COLLECTION time in every live-parked module, so it may not
    raise on junk.

    ``urlsplit(...).port`` raises ``ValueError`` on a non-numeric port, and an
    exception at collection is the "Different tests were collected between gw0 and
    gwN" flake class this janitor exists to remove.
    """
    from ne.brain.testing import qdrant_live as janitor_module

    monkeypatch.setenv("NE_API_QDRANT_URL", _DEPLOYED)
    assert janitor_module._endpoint(malformed) is None
    assert janitor_module._deployment_refusal(malformed) is None


# --------------------------------------------------------------------------- #
# Round 5: the "spelling-independent" half was a five-element frozen set.
# --------------------------------------------------------------------------- #

#: ⭐ EVERY ONE OF THESE REACHES THE LISTENER ON 127.0.0.1, AND THE FIVE-ELEMENT
#: SET THAT USED TO BE THE WHOLE HOST NORMALISATION CONTAINED FIVE OF THEM.
#:
#: Both refusal rules stood on that set — rule 1 compares normalised endpoints,
#: rule 2 fires only when the normalised host IS loopback — so a spelling outside
#: it was neither equal to the deployment's value nor recognised as this machine,
#: and BOTH rules returned ``None``: ``qdrant_server_url()`` handed the address
#: back, ``OwnedQdrantCollections`` opened on it, and the bulk sweep could be aimed
#: at it. The first five entries are the ones a review dialled against a real
#: 127.0.0.1 listener through ``httpx`` (the client stack ``qdrant_client`` uses);
#: the octal and hex forms ``httpx`` itself rejects are here anyway, because a
#: socket does not and refusing them costs nothing. The last five are the round 1-4
#: forms, kept so the repair cannot regress them.
_LOOPBACK_SPELLINGS: tuple[str, ...] = (
    "localhost.",  # the DNS root label
    "127.1",  # inet_aton short form
    "2130706433",  # the same address as one integer
    "127.0.0.2",  # the rest of 127.0.0.0/8 is this machine too
    "::ffff:127.0.0.1",  # IPv4-mapped IPv6
    "0177.0.0.1",  # octal, which glibc reads and httpx does not
    "0x7f.0.0.1",  # hexadecimal, likewise
    "scratch.localhost",  # RFC 6761 6.3: everything under .localhost is loopback
    "LOCALHOST",  # case, which urlsplit already folded
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::",
    "::1",
)

#: The mutation in the other direction. A refusal that refuses everything is a
#: broken janitor: the live-parked proofs must still be able to name a store of
#: their own, and CI's throwaway container is exactly such a store.
_NOT_LOOPBACK_SPELLINGS: tuple[str, ...] = (
    "qdrant.invalid",
    "localhost.example.invalid",  # a NAME under .invalid, not under .localhost
    "notlocalhost",
    "10.0.0.1",
    "192.168.1.1",
    "128.0.0.1",  # one bit outside 127.0.0.0/8
    "2164260865",  # 129.0.0.1 as one integer
    "::2",
    "fe80::1",
    "::ffff:10.0.0.1",  # IPv4-mapped, and not mapped to loopback
)


def _authority(host: str) -> str:
    """``host`` as it appears in a URL: an IPv6 literal needs its brackets."""
    return f"[{host}]" if ":" in host else host


def test_the_source_rules_and_the_runtime_refusal_share_ONE_loopback_predicate() -> None:
    """⛔ THE ROUND-5 DEFECT WAS THAT EVERY LAYER HAD ITS OWN TWO SPELLINGS.

    The janitor carried a five-element frozen set and this file carried
    ``(?:localhost|127.0.0.1)`` twice, so one table of spellings walked through
    all three. There is now one function, and this asserts the source rules use the
    janitor's rather than a copy that can drift away from it again.
    """
    from ne.brain.testing import qdrant_live as janitor_module

    assert is_loopback_host is janitor_module.is_loopback_host


@pytest.mark.parametrize("host", _LOOPBACK_SPELLINGS)
def test_every_loopback_spelling_is_recognised_as_this_machine(host: str) -> None:
    """The predicate itself, before any rule that stands on it."""
    assert is_loopback_host(host) is True, (
        f"{host!r} reaches the listener on 127.0.0.1 and was not recognised as it"
    )


@pytest.mark.parametrize("host", _NOT_LOOPBACK_SPELLINGS)
def test_a_host_that_is_not_this_machine_is_not_called_loopback(host: str) -> None:
    """Over-refusing is not safety: it parks the proofs and breaks CI."""
    assert is_loopback_host(host) is False, (
        f"{host!r} is somebody else's server and was classified as this machine"
    )


@pytest.mark.parametrize("host", _LOOPBACK_SPELLINGS)
def test_every_loopback_spelling_is_refused_while_a_deployment_is_configured(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """⭐ RULE 2, ACROSS THE WHOLE TABLE. The deployment address here is NOT
    loopback, so only the loopback presumption can produce the refusal — which is
    precisely the rule the frozen set disarmed.

    ⛔ NO SOCKET IS OPENED: ``_deployment_refusal`` parses strings and reads the
    environment, and the address it is handed is never dialled.
    """
    from ne.brain.testing import qdrant_live as janitor_module

    monkeypatch.setenv("NE_API_QDRANT_URL", "http://qdrant.invalid:6333")
    refusal = janitor_module._deployment_refusal(f"http://{_authority(host)}:6333")
    assert refusal is not None and "LOOPBACK" in refusal, (
        f"http://{_authority(host)}:6333 is this machine's Qdrant and was waved through"
    )


@pytest.mark.parametrize("host", _LOOPBACK_SPELLINGS)
def test_every_loopback_spelling_is_the_same_endpoint_as_the_deployments(host: str) -> None:
    """...and RULE 1 too: the normalisation folds them all to one endpoint, so a
    deployment variable holding ANY of these spellings is matched by any other."""
    from ne.brain.testing import qdrant_live as janitor_module

    assert janitor_module._endpoint(f"http://{_authority(host)}:6399") == janitor_module._endpoint(
        "http://127.0.0.1:6399"
    ), f"{host!r} did not normalise to the same endpoint as 127.0.0.1"


@pytest.mark.parametrize("host", _NOT_LOOPBACK_SPELLINGS)
def test_a_store_that_is_not_this_machine_is_still_addressable(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """The load-bearing other direction, at the refusal: a different store stays a
    different store even with a deployment configured."""
    from ne.brain.testing import qdrant_live as janitor_module

    monkeypatch.setenv("NE_API_QDRANT_URL", "http://qdrant.invalid:6333")
    assert janitor_module._deployment_refusal(f"http://{_authority(host)}:6334") is None, (
        f"{host!r} is not the deployment and not this machine, and was refused anyway"
    )


@pytest.mark.parametrize("host", _LOOPBACK_SPELLINGS)
def test_every_loopback_spelling_is_ACCEPTED_when_no_deployment_is_configured(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """⭐ THE CI CASE, ACROSS THE WHOLE TABLE — AND IT IS AS LOAD-BEARING AS THE
    REFUSAL.

    ``.github/workflows/ci.yml`` points ``NE_QDRANT_URL`` at the job's OWN Qdrant
    service container, which answers on a loopback address. Widening what counts as
    loopback would have been a way to break every live-parked proof and the promoted
    source-authority gate with it, so the discriminator stays the ENVIRONMENT: no
    deployment variable, no refusal, whatever the address.

    The autouse fixture has scrubbed every deployment Qdrant variable, which is
    exactly the CI situation.
    """
    from ne.brain.testing import qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import deployment_qdrant_addresses

    monkeypatch.delenv("NE_QDRANT_URL", raising=False)
    assert deployment_qdrant_addresses() == {}
    assert janitor_module._deployment_refusal(f"http://{_authority(host)}:6333") is None, (
        f"http://{_authority(host)}:6333 was refused with no deployment configured; "
        "that is CI's own service container, and refusing it parks every live proof"
    )


def test_a_loopback_SPELLING_is_refused_at_the_door_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The end-to-end half: through the real doors, on a shorthand address.

    ``http://127.1:1`` is ``127.0.0.1`` written short, on the refused port — dead by
    :func:`_is_provably_dead`, so this guardian may arm a consent variable with it,
    and it is a spelling the frozen set did not contain. With a deployment
    configured, every door is shut on it.

    ⛔ NO SOCKET IS OPENED: the constructor performs no I/O, and ``server_reachable``
    (the one door that connects) refuses before dialling.
    """
    from ne.brain.testing.qdrant_live import (
        DeploymentQdrantServer,
        OwnedQdrantCollections,
        operator_named_servers,
        qdrant_server_url,
        server_reachable,
        sweep_named_server,
    )

    monkeypatch.setenv("NE_API_QDRANT_URL", "http://qdrant.invalid:6333")
    monkeypatch.setenv("NE_QDRANT_URL", "http://127.1:1")

    # Naming is satisfied — the refusal is about the ADDRESS, as in round 4.
    assert operator_named_servers() == frozenset({"http://127.1:1"})
    assert qdrant_server_url() is None
    assert server_reachable("http://127.1:1") is False
    assert sweep_named_server() == []
    with pytest.raises(DeploymentQdrantServer) as raised:
        OwnedQdrantCollections("http://127.1:1")
    assert "LOOPBACK" in str(raised.value)


def test_the_same_door_ACCEPTS_that_shorthand_with_no_deployment_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """...and the CI case at the door, on the same address. The pair is the whole
    discriminator: the environment, never the address.

    ⛔ NO SOCKET IS OPENED — constructor and name registration only.
    """
    from ne.brain.testing.qdrant_live import OwnedQdrantCollections, qdrant_server_url

    monkeypatch.setenv("NE_QDRANT_URL", "http://127.1:1")
    assert qdrant_server_url() == "http://127.1:1"
    owner = OwnedQdrantCollections("http://127.1:1")
    assert owner.collection("guardian") in owner.pending


@pytest.mark.parametrize("host", _LOOPBACK_SPELLINGS)
def test_the_source_rules_see_every_loopback_spelling_too(host: str) -> None:
    """⛔ THE LAYER THAT KEPT THE BLIND SPOT AFTER THE RUNTIME LOST IT.

    Both source rules matched ``(?:localhost|127.0.0.1)`` and nothing else, so a
    planted module could spell this machine's Qdrant — and a documented recipe could
    aim a consent variable at it — in any of these forms and stay green.
    """
    authority = _authority(host)
    source = f'import ne.brain.testing.qdrant_live as j\nURL = "http://{authority}:6333"\n'
    assert _spells_the_deployed_qdrant(ast.parse(source)) is True, (
        f"a Qdrant-capable module spelling http://{authority}:6333 was reported harmless"
    )
    assert _aims_a_qdrant_var_at_loopback(f"    export NE_QDRANT_URL=http://{authority}:6333\n"), (
        f"a recipe aiming NE_QDRANT_URL at http://{authority}:6333 was reported harmless"
    )


@pytest.mark.parametrize("host", _NOT_LOOPBACK_SPELLINGS)
def test_the_source_rules_do_not_flag_a_store_that_is_not_this_machine(host: str) -> None:
    """The other direction again: a rule that flags every address is an allowlist
    generator, and repopulating allowlists is how this file lost four rounds."""
    authority = _authority(host)
    source = f'import ne.brain.testing.qdrant_live as j\nURL = "http://{authority}:6333"\n'
    assert _spells_the_deployed_qdrant(ast.parse(source)) is False
    assert not _aims_a_qdrant_var_at_loopback(f"NE_QDRANT_URL=http://{authority}:6333\n")


def test_the_source_rule_still_only_fires_on_qdrants_own_ports() -> None:
    """Rule 3's scope is unchanged by the widening: a loopback address on some
    OTHER port is not a Qdrant address, and flagging it would be noise."""
    source = 'import ne.brain.testing.qdrant_live as j\nURL = "http://127.1:8080"\n'
    assert _spells_the_deployed_qdrant(ast.parse(source)) is False
    source = 'import ne.brain.testing.qdrant_live as j\nURL = "http://127.1:6334"\n'
    assert _spells_the_deployed_qdrant(ast.parse(source)) is True


def test_the_provably_dead_predicate_covers_the_spellings_of_the_refused_port() -> None:
    """The self-exemption's own widening, asserted in both directions.

    ``http://127.0.0.1:1`` was a literal in a frozen set; the guarantee it stood for
    is "an address that reaches nothing", and that guarantee does not depend on how
    127.0.0.1 is written. It still does not extend to a live port.
    """
    assert _is_provably_dead("http://127.1:1")
    assert _is_provably_dead("http://127.0.0.1:1")
    assert _is_provably_dead("http://[::1]:1")
    assert _is_provably_dead("http://qdrant.invalid:6333")
    assert not _is_provably_dead("http://127.1:6333"), (
        "Qdrant's own port on this machine is the LIVE STORE; it is never dead"
    )
    assert not _is_provably_dead("http://127.0.0.1:6333")
    assert not _is_provably_dead("http://elsewhere.example:1")


# --------------------------------------------------------------------------- #
# Round 6: three gaps a final review found in the round-5 repair.
# --------------------------------------------------------------------------- #

#: ⭐ THE ROUND-6 BYPASS, VERBATIM. ``CLIENT = AsyncQdrantClient`` is a plain
#: module-level rebind — not an import alias, not a subclass, not a dynamic import
#: — and ``_vendor_bindings`` knew about all three of those and not this one. Paired
#: with round 4's indirect environment write it defeated ALL FOUR source rules AND
#: never touched :class:`OwnedQdrantCollections`, so nothing registered the
#: collection for teardown and nothing swept it: an orphan by construction.
_ROUND_SIX_ALIAS_BYPASS = (
    "import os\n"
    "from qdrant_client import AsyncQdrantClient\n"
    "CLIENT = AsyncQdrantClient\n"
    'KEY = "NE_QDRANT_URL"\n'
    'os.environ[KEY] = os.environ["NE_API_QDRANT_URL"]\n'
    "c = CLIENT(url=os.environ[KEY])\n"
)


def test_the_round_six_alias_bypass_is_caught_by_rule_one() -> None:
    """⭐ THE CLIENT RULE SEES THE ALIAS NOW — and that is the half that matters.

    Rule 1 exists to force every server client through ``OwnedQdrantCollections``
    so its collections are REGISTERED FOR TEARDOWN. A bare ``CLIENT =
    AsyncQdrantClient`` skipped the janitor entirely, which is the mechanism that
    put 985 collections in the live store, not merely the address that aimed them.
    """
    assert _addresses_a_qdrant_server(ast.parse(_ROUND_SIX_ALIAS_BYPASS)) is True


def test_the_round_six_bypass_still_defeats_the_other_three_source_rules() -> None:
    """⭐ THE HONEST HALF, SAID AGAIN RATHER THAN QUIETLY DROPPED.

    Catching the alias does NOT make the address rules see this. The consent
    variable is written through an indirect ``Name`` key and the value is copied out
    of production, so the write rule, the default rule and the spelled-address rule
    all still return ``False`` — exactly as
    :func:`test_the_round_four_bypass_still_defeats_every_source_rule` pins for
    round 4. The ADDRESS half of this bypass is held by the runtime refusal, and
    nothing here should be read as saying otherwise.
    """
    tree = ast.parse(_ROUND_SIX_ALIAS_BYPASS)
    assert _consent_var_writes(tree) == []
    assert _defaults_a_qdrant_address(tree) is False
    assert _spells_the_deployed_qdrant(tree) is False


#: ``(label, source)`` — every NAME-binding form the fixpoint now resolves.
_PLANTED_ALIAS_OFFENDERS: tuple[tuple[str, str], ...] = (
    (
        "plain rebind of an import",
        "from qdrant_client import AsyncQdrantClient\n"
        "CLIENT = AsyncQdrantClient\n"
        f'c = CLIENT(url="{_ELSEWHERE}")\n',
    ),
    (
        "alias of an alias",
        "from qdrant_client import QdrantClient\n"
        "A = QdrantClient\n"
        "B = A\n"
        f'c = B(url="{_ELSEWHERE}")\n',
    ),
    (
        "alias of a subclass",
        "from qdrant_client import QdrantClient\n"
        "class _S(QdrantClient):\n    pass\n"
        "A = _S\n"
        f'c = A(url="{_ELSEWHERE}")\n',
    ),
    (
        "subclass of an alias",
        "from qdrant_client import QdrantClient\n"
        "A = QdrantClient\n"
        "class _S(A):\n    pass\n"
        f'c = _S(url="{_ELSEWHERE}")\n',
    ),
    (
        "chained assignment",
        "from qdrant_client import QdrantClient\n"
        "A = B = QdrantClient\n"
        f'c = B(url="{_ELSEWHERE}")\n',
    ),
    (
        "annotated assignment",
        "from qdrant_client import QdrantClient\n"
        "A: type = QdrantClient\n"
        f'c = A(url="{_ELSEWHERE}")\n',
    ),
    (
        "walrus, called in place",
        f'from qdrant_client import QdrantClient\nc = (A := QdrantClient)(url="{_ELSEWHERE}")\n',
    ),
    (
        "tuple unpacking",
        "from qdrant_client import QdrantClient, AsyncQdrantClient\n"
        "A, B = QdrantClient, AsyncQdrantClient\n"
        f'c = B(url="{_ELSEWHERE}")\n',
    ),
    (
        "module alias, twice over",
        f'import qdrant_client\nm = qdrant_client\nn = m\nc = n.QdrantClient(url="{_ELSEWHERE}")\n',
    ),
    (
        "alias of a dynamic import's class",
        "import importlib\n"
        'C = importlib.import_module("qdrant_client").AsyncQdrantClient\n'
        "D = C\n"
        f'c = D(url="{_ELSEWHERE}")\n',
    ),
)


@pytest.mark.parametrize(
    ("label", "source"), _PLANTED_ALIAS_OFFENDERS, ids=[p[0] for p in _PLANTED_ALIAS_OFFENDERS]
)
def test_a_planted_alias_of_the_vendor_client_is_caught(label: str, source: str) -> None:
    """Aliases resolve to a fixpoint, interleaved with subclassing in one loop."""
    assert _addresses_a_qdrant_server(ast.parse(source)) is True, (
        f"planted alias offender {label!r} was reported harmless"
    )


#: ⚠️ THE FORMS THE FIXPOINT STILL DOES NOT RESOLVE. Pinned as a LIMIT, not left to
#: be discovered as round 7's finding: in each of these the vendor class reaches a
#: call through something the AST cannot evaluate, and rule 1 calls the file
#: harmless. Widening to cover them means guessing at values, which produces false
#: offenders — and a rule that cries wolf is how allowlists get repopulated.
_UNRESOLVED_BINDING_FORMS: tuple[tuple[str, str], ...] = (
    (
        "through a dict",
        "from qdrant_client import QdrantClient\n"
        'D = {"c": QdrantClient}\n'
        f'c = D["c"](url="{_ELSEWHERE}")\n',
    ),
    (
        "through getattr",
        "import qdrant_client\n"
        'C = getattr(qdrant_client, "QdrantClient")\n'
        f'c = C(url="{_ELSEWHERE}")\n',
    ),
    (
        "through a function's return value",
        "from qdrant_client import QdrantClient\n"
        "def _pick():\n    return QdrantClient\n"
        f'c = _pick()(url="{_ELSEWHERE}")\n',
    ),
    (
        "through a parameter",
        f'def _build(cls):\n    return cls(url="{_ELSEWHERE}")\n',
    ),
)


@pytest.mark.parametrize(
    ("label", "source"),
    _UNRESOLVED_BINDING_FORMS,
    ids=[p[0] for p in _UNRESOLVED_BINDING_FORMS],
)
def test_the_binding_forms_the_source_rule_still_cannot_resolve(label: str, source: str) -> None:
    """⛔ ASSERTING THE LIMIT, NOT A FIX. This file has claimed a closed set five
    times and been wrong five times. These four are open, they are expected to be
    open, and the guarantee for them is the RUNTIME refusal plus the fact that a
    client built this way still never registers a collection for teardown — which
    is a real gap, recorded here rather than papered over."""
    assert _addresses_a_qdrant_server(ast.parse(source)) is False, (
        f"{label!r} is now resolved — good, but this pinned limit must be updated "
        "rather than left asserting something that is no longer true"
    )


def test_an_alias_of_an_in_tree_fake_is_still_not_an_offender() -> None:
    """The other direction for the widening: aliasing a FAKE binds nothing vendor."""
    source = (
        "class _FakeAsyncQdrantClient:\n    pass\n"
        "C = _FakeAsyncQdrantClient\n"
        f'c = C(url="{_ELSEWHERE}")\n'
    )
    assert _addresses_a_qdrant_server(ast.parse(source)) is False


def test_an_alias_used_for_local_mode_is_still_not_an_offender() -> None:
    """...and an alias constructing the in-process store is not a server client."""
    source = (
        'from qdrant_client import AsyncQdrantClient\nC = AsyncQdrantClient\nc = C(":memory:")\n'
    )
    assert _addresses_a_qdrant_server(ast.parse(source)) is False


# --------------------------------------------------------------------------- #
# Round 6, defect 2: the deployment-variable derivation missed real names.
# --------------------------------------------------------------------------- #

#: ⭐ A DEPLOYMENT VARIABLE THAT IS IN THE TREE RIGHT NOW AND WAS DERIVED BY
#: NOTHING. ``deploy/compose/dev/seed/seed.sh`` sets a bare ``QDRANT_URL``; the
#: derivation regex required a character BEFORE ``QDRANT``, so an inherited
#: ``QDRANT_URL`` activated NEITHER refusal rule while the janitor's docstring
#: claimed every deployment address variable was derived automatically.
_BARE_DEPLOYMENT_VAR = "QDRANT_URL"


def test_the_bare_deployment_variable_in_the_tree_is_derived() -> None:
    """Non-vacuous against the REAL ``deploy/`` tree, not a synthetic one."""
    from ne.brain.testing.qdrant_live import deployment_qdrant_url_vars

    derived = deployment_qdrant_url_vars()
    assert _BARE_DEPLOYMENT_VAR in derived, (
        f"{_BARE_DEPLOYMENT_VAR} is set by deploy/compose/dev/seed/seed.sh and is "
        f"not being derived. Derived: {sorted(derived)}"
    )


#: Variable-name shapes the widened pattern must see. Each is an address variable.
_DERIVABLE_VARIABLE_NAMES = (
    "QDRANT_URL",
    "NE_QDRANT_URL",
    "NE_API_QDRANT_URL",
    "NE_API_QDRANT_DSN",
    "NE_API_VECTOR_HOSTNAME",
    "NE_API_VECTOR_ENDPOINT",
    "VECTOR_URI",
    "QDRANT_HOST",
    "NE_VECTORDB_SERVER",
)

#: ...and shapes it must NOT see, because refusing everything parks every proof.
#: ``QDRANT__SERVICE__HOST`` is Qdrant's own BIND address, not a client's target:
#: treating it as a deployment address would refuse every loopback store from
#: inside the container that runs Qdrant.
_NON_ADDRESS_VARIABLE_NAMES = (
    "VECTOR_SIZE",
    "QDRANT_COLLECTION",
    "QDRANT_API_KEY",
    "QDRANT__SERVICE__HOST",
    "NE_VECTOR_DIMENSIONS",
)


@pytest.mark.parametrize("name", _DERIVABLE_VARIABLE_NAMES)
def test_the_widened_derivation_sees_every_address_variable_shape(name: str) -> None:
    from ne.brain.testing import qdrant_live as janitor_module

    assert janitor_module._DEPLOYMENT_ADDRESS_VAR_RE.fullmatch(name) is not None, (
        f"{name} carries a vector-store address and the derivation would miss it"
    )


@pytest.mark.parametrize("name", _NON_ADDRESS_VARIABLE_NAMES)
def test_the_widened_derivation_does_not_swallow_non_address_variables(name: str) -> None:
    """Over-breadth is not free HERE, unlike in the refusal itself: every derived
    name that happens to be set makes ``deployment_qdrant_addresses()`` non-empty,
    which arms the loopback presumption and would park CI's own throwaway store."""
    from ne.brain.testing import qdrant_live as janitor_module

    assert janitor_module._DEPLOYMENT_ADDRESS_VAR_RE.fullmatch(name) is None, (
        f"{name} is not an address and was derived as one; a value of it in the "
        "environment would refuse every loopback Qdrant, CI's included"
    )


#: ⚠️ WHAT A NAME HEURISTIC CANNOT DO, PINNED. None of these contains ``QDRANT`` or
#: ``VECTOR``, so nothing derives them and an inherited value of one is invisible to
#: both refusal rules. Residual limit 4 in the janitor's module docstring says so in
#: the same words, and names the only real fix: have the deploy path WRITE its
#: address somewhere the test session reads, instead of guessing variable names.
_UNDERIVABLE_VARIABLE_NAMES = (
    "NE_API_STORE_URL",
    "EMBEDDINGS_BACKEND",
    "RETRIEVER_TARGET",
    "KNOWLEDGE_INDEX_ENDPOINT",
)


@pytest.mark.parametrize("name", _UNDERIVABLE_VARIABLE_NAMES)
def test_the_deployment_variable_shapes_the_derivation_still_cannot_see(name: str) -> None:
    """⛔ ASSERTING THE LIMIT. Widening the pattern until it matches these would mean
    matching every ``*_URL`` in the tree, which arms the loopback presumption on any
    box with an unrelated URL exported — the over-refusal that breaks CI."""
    from ne.brain.testing import qdrant_live as janitor_module

    assert janitor_module._DEPLOYMENT_ADDRESS_VAR_RE.fullmatch(name) is None, (
        f"{name} is now derived — good, but this pinned limit must be updated"
    )


def test_a_bare_deployment_variable_now_arms_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end for the widening: the tree's own ``QDRANT_URL``, inherited.

    ⛔ NO SOCKET IS OPENED — ``_deployment_refusal`` parses strings and reads the
    environment.
    """
    from ne.brain.testing import qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import deployment_qdrant_addresses

    monkeypatch.setenv(_BARE_DEPLOYMENT_VAR, "http://qdrant.invalid:6333")
    assert _BARE_DEPLOYMENT_VAR in deployment_qdrant_addresses()
    # Rule 1: the same address under another spelling.
    assert janitor_module._deployment_refusal("qdrant.invalid:6333") is not None
    # Rule 2: a deployment is configured, so a loopback store is presumed to be it.
    refusal = janitor_module._deployment_refusal("http://" + "localhost" + ":6333")
    assert refusal is not None and "LOOPBACK" in refusal


# --------------------------------------------------------------------------- #
# Round 6, defect 3: the sweep was blind where the REQUIRED gate runs.
# --------------------------------------------------------------------------- #

#: Two provably dead addresses that are DIFFERENT endpoints. ⚠️ Not two loopback
#: addresses: every loopback spelling folds to one token (:data:`_LOOPBACK_CANON`),
#: which is the point of round 5 — so the second store is an RFC 2606 ``.invalid``
#: host, which resolves nowhere and is accepted by :func:`_is_provably_dead`.
_DEAD_A = "http://127.0.0.1:1"
_DEAD_B = "http://scratch.invalid:6333"

#: The REQUIRED CI gate that reads the OTHER consent variable, and the prefix it
#: mints. ``.github/workflows/ci.yml`` sets ``NE_TEST_QDRANT_URL`` on that step
#: alone, so a killed worker there leaves ``authority_<uuid4>`` collections behind.
_REQUIRED_GATE = Path("packages/ne-brain/tests/test_immutable_artifact_store_postgres_qdrant.py")
_REQUIRED_GATE_PREFIX = "authority"


#: What every server the sweep visits is pretending to hold.
_SWEEP_PRESENT = ("ne_chunks", "authority_" + "d" * 32, "kit_" + "e" * 32)


class _SweepRecorder:
    """A ``QdrantClient`` stand-in that records which SERVERS the sweep visited."""

    def __init__(self, visited: list[str]) -> None:
        self._visited = visited

    def __call__(
        self, url: str, timeout: int = 0, check_compatibility: bool = True
    ) -> _SweepRecorder:
        self._url = url
        self._visited.append(url)
        return self

    def get_collections(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            collections=[types.SimpleNamespace(name=name) for name in _SWEEP_PRESENT]
        )

    def delete_collection(self, name: str) -> bool:
        return True

    def close(self) -> None:
        return None


def test_the_sweep_covers_the_server_the_OTHER_consent_variable_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⭐ THE RECOVERY MECHANISM HAD THE ORPHAN BUG IT EXISTS TO FIX.

    Both ``NE_QDRANT_URL`` and ``NE_TEST_QDRANT_URL`` grant ownership, and the
    second is the one CI sets on the promoted source-authority step — a REQUIRED
    gate whose module mints ``authority_<uuid4>`` collections. The sweep took its
    target from ``qdrant_server_url()``, which reads ``NE_QDRANT_URL`` alone, so a
    killed worker running that gate left orphans on a server this recovery never
    opened a connection to.
    """
    import ne.brain.testing.qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import sweep_named_server

    visited: list[str] = []
    monkeypatch.setattr(janitor_module, "QdrantClient", _SweepRecorder(visited))
    monkeypatch.delenv("NE_QDRANT_URL", raising=False)
    monkeypatch.setenv("NE_TEST_QDRANT_URL", _DEAD_A)

    dropped = sweep_named_server()
    assert visited == [_DEAD_A], (
        "the promoted gate's server was never visited by the session sweep; that is "
        "the orphan-accumulation path this train exists to close"
    )
    assert set(dropped) == {"authority_" + "d" * 32, "kit_" + "e" * 32}
    assert "ne_chunks" not in dropped


def test_the_sweep_covers_BOTH_consent_named_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two variables, two stores, both swept — and each visited exactly once."""
    import ne.brain.testing.qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import consented_server_urls, sweep_named_server

    visited: list[str] = []
    monkeypatch.setattr(janitor_module, "QdrantClient", _SweepRecorder(visited))
    monkeypatch.setenv("NE_QDRANT_URL", _DEAD_A)
    monkeypatch.setenv("NE_TEST_QDRANT_URL", _DEAD_B)

    assert consented_server_urls() == (_DEAD_A, _DEAD_B)
    sweep_named_server()
    assert visited == [_DEAD_A, _DEAD_B]


def test_two_spellings_of_one_store_are_swept_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """De-duplicated on the NORMALISED endpoint, not on the raw string — ``127.1``
    and ``127.0.0.1`` are one server and sweeping it twice would be the janitor
    double-counting its own work."""
    import ne.brain.testing.qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import consented_server_urls, sweep_named_server

    visited: list[str] = []
    monkeypatch.setattr(janitor_module, "QdrantClient", _SweepRecorder(visited))
    monkeypatch.setenv("NE_QDRANT_URL", _DEAD_A)
    monkeypatch.setenv("NE_TEST_QDRANT_URL", "http://127.1:1")

    assert consented_server_urls() == (_DEAD_A,)
    sweep_named_server()
    assert visited == [_DEAD_A]


def test_the_sweep_refuses_a_deployment_address_in_EITHER_consent_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⛔ THE BLAST-RADIUS RULE SURVIVES THE WIDENING. Covering the second variable
    must not make it a second way to aim a BULK DELETE at production."""
    import ne.brain.testing.qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import consented_server_urls, sweep_named_server

    visited: list[str] = []
    monkeypatch.setattr(janitor_module, "QdrantClient", _SweepRecorder(visited))
    monkeypatch.setenv("NE_API_QDRANT_URL", _DEPLOYED)
    monkeypatch.delenv("NE_QDRANT_URL", raising=False)
    monkeypatch.setenv("NE_TEST_QDRANT_URL", "qdrant.invalid:6333")

    assert consented_server_urls() == ()
    assert sweep_named_server() == []
    assert visited == []


def test_the_sweep_still_opens_nothing_when_neither_variable_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ne.brain.testing.qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import sweep_named_server

    visited: list[str] = []
    monkeypatch.setattr(janitor_module, "QdrantClient", _SweepRecorder(visited))
    monkeypatch.delenv("NE_QDRANT_URL", raising=False)
    monkeypatch.delenv("NE_TEST_QDRANT_URL", raising=False)

    assert sweep_named_server() == []
    assert visited == []


def test_the_required_gates_own_prefix_is_one_the_sweep_recognises() -> None:
    """Non-vacuity for the fix above, tied to the REAL module rather than a string.

    Sweeping the right server buys nothing if the names that gate leaves behind do
    not match :data:`SWEEPABLE_COLLECTION_PREFIXES`. It mints through
    ``OwnedQdrantCollections.collection("authority")``, and ``collection()`` refuses
    any prefix the sweep cannot recognise — so this asserts the gate really does
    mint that way, in that file.
    """
    from ne.brain.testing.qdrant_live import SWEEPABLE_COLLECTION_PREFIXES

    assert _REQUIRED_GATE_PREFIX in SWEEPABLE_COLLECTION_PREFIXES
    tree = _parse(_REPO_ROOT / _REQUIRED_GATE)
    minted = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "collection"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    assert minted == {_REQUIRED_GATE_PREFIX}, (
        f"{_REQUIRED_GATE} mints {sorted(minted)}; the sweep recognises "
        f"{sorted(SWEEPABLE_COLLECTION_PREFIXES)}"
    )


def test_an_owned_client_refuses_to_create_a_collection_it_did_not_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⭐ THE PREFIX GATE WAS ONLY ON THE NAMES A TEST ASKED FOR.

    ``collection()`` refuses an unsweepable prefix — but a test holding an owned
    client could always pass a name of its own straight to ``create_collection``,
    producing a collection registered for no teardown and matching no sweep
    pattern. That is an orphan by construction, so the client refuses.

    ⚠️ Building a client DOES construct the vendor's transport (against port 1 here,
    which reaches nothing, and with the version handshake disabled), but the refusal
    raises before any request is issued.
    """
    from ne.brain.testing.qdrant_live import OwnedQdrantCollections, UnownedQdrantCollection

    monkeypatch.setenv("NE_QDRANT_URL", _DEAD_A)
    owner = OwnedQdrantCollections(_DEAD_A)
    client = owner.client()
    try:
        with pytest.raises(UnownedQdrantCollection, match="never minted it"):
            asyncio.run(client.create_collection("ne_chunks"))
        with pytest.raises(UnownedQdrantCollection):
            asyncio.run(client.recreate_collection("kit_" + "0" * 32))
    finally:
        asyncio.run(client.close())


def test_an_owned_client_DOES_create_a_collection_it_minted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction — a refusal that refuses everything is a broken janitor.

    The vendor's own ``create_collection`` is replaced so the delegation is proved
    without a server; the janitor's guard runs for real.
    """
    from qdrant_client import AsyncQdrantClient

    from ne.brain.testing.qdrant_live import OwnedQdrantCollections

    created: list[str] = []

    async def _fake(self: object, collection_name: str, *args: object, **kwargs: object) -> bool:
        created.append(collection_name)
        return True

    monkeypatch.setattr(AsyncQdrantClient, "create_collection", _fake)
    monkeypatch.setenv("NE_QDRANT_URL", _DEAD_A)
    owner = OwnedQdrantCollections(_DEAD_A)
    name = owner.collection("kit")
    client = owner.client()
    try:
        assert asyncio.run(client.create_collection(name)) is True
    finally:
        asyncio.run(client.close())
    assert created == [name]


# --------------------------------------------------------------------------- #
# Round 7: only LOOPBACK was canonicalised; every other IP literal stayed raw.
# --------------------------------------------------------------------------- #

#: The deployment address these tests configure — a NON-loopback IPv4, so only the
#: address-equality rule (rule 1) can produce a refusal and the loopback presumption
#: (rule 2) contributes nothing. ⛔ NO SOCKET IS OPENED anywhere in this section:
#: ``_deployment_refusal`` and ``_endpoint`` parse strings and read ``os.environ``.
_V4_DEPLOYMENT = "10.0.0.1"

#: Every spelling an HTTP client dials as ``10.0.0.1``. Round 5 gave loopback real
#: address semantics and left this family on STRING EQUALITY, so with the deployment
#: on ``10.0.0.1`` all but the first two were waved through — and past the refusal
#: the whole chain opens, the sweep's BULK DELETE included.
_V4_SAME_ADDRESS: tuple[str, ...] = (
    "10.0.0.1",
    "10.0.0.1.",  # the DNS root label
    "167772161",  # the whole address as one integer
    "10.0.0.01",  # a zero-padded octet
    "012.0.0.1",  # octal, which glibc reads and httpx does not
    "0x0a.0.0.1",  # hexadecimal, likewise
    "10.1",  # inet_aton shorthand: the last part fills the missing bytes
    "10.0.1",  # ...and the three-part form
    "::ffff:10.0.0.1",  # IPv4-mapped IPv6
    "::ffff:a00:1",  # ...the same mapping written as IPv6 groups
)

#: The mutation in the other direction, at the SAME port. A janitor that refuses
#: everything is broken: these are somebody else's servers and every one of them
#: must stay addressable, or the live-parked proofs are parked for good.
_V4_OTHER_ADDRESSES: tuple[str, ...] = (
    "10.0.0.2",  # one bit out
    "167772162",  # ...the same, as an integer
    "10.0.1.1",
    "11.0.0.1",
    "192.168.1.1",
    "qdrant.invalid",  # a NAME, which is never resolved here
    "10.0.0.1.invalid",  # a NAME that merely starts with the digits
)

#: An IPv6 deployment, compressed per RFC 5952, plus spellings of it. Codex
#: reported this half of the defect: expanded and compressed forms of one address
#: compared unequal, so the refusal returned ``None``.
_V6_DEPLOYMENT = "2001:db8::1"

_V6_SAME_ADDRESS: tuple[str, ...] = (
    "2001:db8::1",
    "2001:0db8:0000:0000:0000:0000:0000:0001",  # fully expanded
    "2001:db8:0:0:0:0:0:1",  # partially expanded
    "2001:DB8::1",  # upper case
    "2001:0db8::0001",  # leading zeros in the groups that remain
)

_V6_OTHER_ADDRESSES: tuple[str, ...] = (
    "2001:db8::2",
    "2001:db8::1:0",
    "fe80::1",
    "::2",
)


def _refusal_with_deployment(
    monkeypatch: pytest.MonkeyPatch, deployment: str, candidate: str
) -> str | None:
    """``_deployment_refusal(candidate)`` with ``deployment`` configured, no I/O."""
    from ne.brain.testing import qdrant_live as janitor_module

    monkeypatch.setenv("NE_API_QDRANT_URL", f"http://{_authority(deployment)}:6333")
    return janitor_module._deployment_refusal(f"http://{_authority(candidate)}:6333")


@pytest.mark.parametrize("host", _V4_SAME_ADDRESS)
def test_every_IPv4_spelling_of_the_deployment_address_is_refused(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """⛔ THE ROUND-7 DEFECT. Measured on the round-6 code with the deployment at
    ``10.0.0.1``: ``167772161`` and ``10.0.0.01`` were NOT refused, because
    ``_endpoint`` canonicalised a loopback host and passed every other IP literal
    through as a raw string. One named set with real semantics and string equality
    for everything else is the frozen set's shape one layer out.
    """
    refusal = _refusal_with_deployment(monkeypatch, _V4_DEPLOYMENT, host)
    assert refusal is not None and "DEPLOYED" in refusal, (
        f"{host!r} is {_V4_DEPLOYMENT} written differently — the address the deployed "
        "application is configured with — and it was waved through"
    )


@pytest.mark.parametrize("host", _V4_OTHER_ADDRESSES)
def test_a_different_IPv4_address_is_still_accepted(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """The load-bearing other direction: canonicalising must not fold two DIFFERENT
    addresses together, or every throwaway store on the box becomes unaddressable."""
    assert _refusal_with_deployment(monkeypatch, _V4_DEPLOYMENT, host) is None, (
        f"{host!r} is not {_V4_DEPLOYMENT} and not this machine, and was refused anyway"
    )


@pytest.mark.parametrize("host", _V6_SAME_ADDRESS)
def test_every_IPv6_spelling_of_the_deployment_address_is_refused(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """Expanded vs compressed vs upper case: one address, one endpoint."""
    refusal = _refusal_with_deployment(monkeypatch, _V6_DEPLOYMENT, host)
    assert refusal is not None and "DEPLOYED" in refusal, (
        f"[{host}] is {_V6_DEPLOYMENT} written differently and it was waved through"
    )


@pytest.mark.parametrize("host", _V6_OTHER_ADDRESSES)
def test_a_different_IPv6_address_is_still_accepted(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    assert _refusal_with_deployment(monkeypatch, _V6_DEPLOYMENT, host) is None, (
        f"[{host}] is a different address from {_V6_DEPLOYMENT} and was refused anyway"
    )


@pytest.mark.parametrize("host", _V4_SAME_ADDRESS + _V6_SAME_ADDRESS)
def test_the_deployment_may_hold_ANY_spelling_and_still_match(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """⭐ SYMMETRY, WHICH IS THE POINT OF CANONICALISING RATHER THAN SPECIAL-CASING.

    The variable that holds the odd spelling may be the DEPLOYMENT's — a compose
    file is as free to write ``10.0.0.01`` as a test is — so the fold has to happen
    on both sides of the comparison, not on the candidate only.
    """
    canonical = _V4_DEPLOYMENT if host in _V4_SAME_ADDRESS else _V6_DEPLOYMENT
    refusal = _refusal_with_deployment(monkeypatch, host, canonical)
    assert refusal is not None and "DEPLOYED" in refusal, (
        f"the deployment spelled {host!r} did not match its own canonical form"
    )


@pytest.mark.parametrize("host", _V4_SAME_ADDRESS)
def test_every_IPv4_spelling_is_ONE_endpoint(host: str) -> None:
    """The normalisation itself, under the refusal that stands on it."""
    from ne.brain.testing import qdrant_live as janitor_module

    assert janitor_module._endpoint(f"http://{_authority(host)}:6399/") == janitor_module._endpoint(
        f"http://{_V4_DEPLOYMENT}:6399"
    ), f"{host!r} normalised to a different endpoint from {_V4_DEPLOYMENT}"


@pytest.mark.parametrize("host", _V6_SAME_ADDRESS)
def test_every_IPv6_spelling_is_ONE_endpoint(host: str) -> None:
    from ne.brain.testing import qdrant_live as janitor_module

    assert janitor_module._endpoint(f"http://{_authority(host)}:6399/") == janitor_module._endpoint(
        f"http://{_authority(_V6_DEPLOYMENT)}:6399"
    ), f"[{host}] normalised to a different endpoint from {_V6_DEPLOYMENT}"


@pytest.mark.parametrize("host", _V4_OTHER_ADDRESSES + _V6_OTHER_ADDRESSES)
def test_a_different_address_is_a_different_endpoint(host: str) -> None:
    """Mutation in the other direction at the normalisation, not just the refusal."""
    from ne.brain.testing import qdrant_live as janitor_module

    reference = _V4_DEPLOYMENT if host in _V4_OTHER_ADDRESSES else _V6_DEPLOYMENT
    assert janitor_module._endpoint(f"http://{_authority(host)}:6399") != janitor_module._endpoint(
        f"http://{_authority(reference)}:6399"
    ), f"{host!r} was folded onto {reference}, which is a different server"


def test_canonicalising_addresses_did_not_make_a_NAME_an_address() -> None:
    """⛔ THE LIMIT, ASSERTED SO THE DOCSTRING CANNOT QUIETLY OVER-CLAIM AGAIN.

    Residual limit 3 says the numeric half is exhaustive and the NAME half is not:
    nothing here resolves, because this runs at collection time in every live-parked
    module and ``socket`` is deliberately not imported. So a name and the address it
    resolves to are DIFFERENT endpoints, two different names for one host are
    different endpoints, and both of those reach the live store unrefused. If this
    test ever has to change, the docstring changes in the same edit.
    """
    from ne.brain.testing import qdrant_live as janitor_module

    assert janitor_module._endpoint("http://qdrant.invalid:6399") != janitor_module._endpoint(
        f"http://{_V4_DEPLOYMENT}:6399"
    )
    assert janitor_module._endpoint("http://qdrant.invalid:6399") != janitor_module._endpoint(
        "http://vectors.invalid:6399"
    )
    # ...and a name is left exactly as it was found, lowercased, not parsed.
    endpoint = janitor_module._endpoint("http://Qdrant.Invalid.:6399")
    assert endpoint is not None and endpoint[0] == "qdrant.invalid"


def test_a_NON_loopback_store_is_ACCEPTED_when_no_deployment_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⭐ THE CI CASE FOR THE WIDENED FOLD. The environment stays the whole
    discriminator: with no deployment variable present, no address is refused —
    which is what keeps CI's own throwaway Qdrant addressable.
    """
    from ne.brain.testing import qdrant_live as janitor_module
    from ne.brain.testing.qdrant_live import deployment_qdrant_addresses

    monkeypatch.delenv("NE_QDRANT_URL", raising=False)
    assert deployment_qdrant_addresses() == {}
    for host in _V4_SAME_ADDRESS + _V6_SAME_ADDRESS:
        assert janitor_module._deployment_refusal(f"http://{_authority(host)}:6333") is None, (
            f"{host!r} was refused with no deployment configured; the discriminator "
            "is the ENVIRONMENT, never the address"
        )


def test_ONE_parse_decides_loopback_and_canonical_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """⛔ THE ROUND-5 RULE SURVIVES ROUND 7: still ONE predicate, one parse.

    Round 5's fix was that every layer stopped carrying its own table of spellings.
    Widening the fold to every numeric host must not reintroduce a second parser
    beside :func:`is_loopback_host` — so the public predicate is the loopback
    question asked of the same function :func:`_endpoint` normalises with, and
    breaking that function breaks both.
    """
    from ne.brain.testing import qdrant_live as janitor_module

    assert is_loopback_host is janitor_module.is_loopback_host
    assert is_loopback_host("127.1") is True
    assert is_loopback_host(_V4_DEPLOYMENT) is False

    monkeypatch.setattr(janitor_module, "_canonical_host", lambda host: None)
    assert is_loopback_host("127.0.0.1") is False, (
        "is_loopback_host answered without consulting _canonical_host, so the two "
        "layers have a parser each again — which is exactly the round-5 defect"
    )
    assert janitor_module._endpoint("http://127.0.0.1:6333") is None, (
        "_endpoint normalised without consulting _canonical_host"
    )


# --------------------------------------------------------------------------- #
# Self-check: this guardian cannot be turned off.
# --------------------------------------------------------------------------- #

#: Spelled in pieces so the self-check does not find ITSELF.
_PYTEST = "py" + "test"
_SKIP_IDENTIFIERS = ("skip", "skipif", "xfail", "importorskip", "SkipTest")


def test_guardian_cannot_be_skipped() -> None:
    """No marker, and NO MODULE-LEVEL SKIP EITHER.

    The text check used to look for ``mark.`` forms only, so a single
    module-level skip call — which needs no marker at all — silently disabled
    every rule in this file while it reported ``passed``. So the AST is checked
    too: no identifier that can park a test may appear anywhere in this module,
    as a call, a decorator or a bare attribute.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    for identifier in _SKIP_IDENTIFIERS:
        for form in (f"{_PYTEST}.{identifier}", f"{_PYTEST}.mark.{identifier}"):
            assert form not in source, f"guardian file must not contain {form}"

    tree = ast.parse(source)
    for node in ast.walk(tree):
        found = ""
        if isinstance(node, ast.Attribute):
            found = node.attr
        elif isinstance(node, ast.Name):
            found = node.id
        assert found not in _SKIP_IDENTIFIERS, (
            f"guardian file must not reference {found!r}: this file is the evidence "
            "floor for the 2026-09-10 outage and must run on every job"
        )
