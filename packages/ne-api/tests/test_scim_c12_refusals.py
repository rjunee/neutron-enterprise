# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C12 sweep over the SCIM refusal surface: a refusal names the FIELD, never the VALUE.

SCIM payloads are user identity attributes — userName, externalId, displayName,
group member ids — so a refusal that echoes what it rejected widens PII into
whatever reads it. Two carriers, both swept here:

* the refusal MESSAGE (and therefore the ``detail`` on the wire), and
* the FORMATTED EXCEPTION CHAIN (``traceback.format_exception`` — the text any
  ``logger.exception`` / Sentry / OTel ``record_exception`` would ship). A
  chained ``pydantic.ValidationError`` renders the rejected value as
  ``input_value=...`` even when the message above it is clean.

Two guards DISCOVER what they cover instead of listing it, because a hand-listed
set is the same quantifier defect one refactor later:

``test_every_scim_refusal_site_is_literal_or_explicitly_justified``
    Walks the AST of every module in ``ne.api.scim`` and finds every
    construction of every ``BaseException`` subclass bound in that package
    (discovered by import, not named here) plus the two non-exception ways a
    ``detail`` reaches the wire. Each argument must be a string literal, a name
    resolving to one, or one of a small DENY-BY-DEFAULT allowlist of justified
    expressions — the name says "or explicitly justified" because that allowlist
    is real and is the whole escape hatch. A new refusal that interpolates
    anything fails this test the day it is written.

    What it does NOT cover, stated so the name is not read wider than the
    mechanism: the bodies of the two allowlisted composers/conduits
    (``describe_validation_error`` and ``_error_response``), pinned by their own
    tests and by their call sites respectively; and any exception raised OUTSIDE
    this package that reaches the mount, which surfaces as a 500 rather than a
    SCIM refusal.

``test_every_declared_route_refuses_the_sentinel_matrix_without_echoing_it``
    Universal over ROUTES (read from the app's own route table, so a new
    endpoint is swept automatically), existential over PAYLOADS (the named
    matrix in :func:`_sentinel_variants`). It asserts the wire, which is the
    carrier the AST guard cannot see: a leak introduced INSIDE an allowlisted
    composer is invisible to the AST walk and shows up only here.

The remaining tests are per-site, each exercised at ITS OWN raise rather than
through one representative lane, and each named for exactly what it covers:

===========================================  =========================================
site                                          pinned by
===========================================  =========================================
``_parse_patch_request`` JSON-decode link     ``__cause__``/``__suppress_context__``
``_parse_patch_request`` pydantic link        sentinel sweep + ``__cause__``
``GET /Users`` filter link                    sentinel sweep + ``__cause__``
``GET /Groups`` filter link                   sentinel sweep + ``__cause__``
``_patch_group_sync`` members-path link       sentinel sweep + ``__cause__``
``_on_validation_error`` (FastAPI-bound)      sentinel sweep on the wire body
service-layer 400/404/409 lanes               sentinel sweep on the wire body
``describe_validation_error`` loc guards      one case per guard, INDEPENDENTLY
===========================================  =========================================

The JSON-decode link is pinned STRUCTURALLY only, and the entry says so: its
reachable causes (``json.JSONDecodeError``, ``UnicodeDecodeError``) render a
line/column position and never body content, so no sentinel sweep can
discriminate that link — an honest ``__cause__`` assert is the whole guard
there. Every other row carries a should-fire CONTROL proving the sweep would
catch a leak where one exists.
"""

from __future__ import annotations

import ast
import importlib
import json
import pkgutil
import re
import traceback
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.routing import iter_route_contexts
from pydantic import ValidationError
from starlette.requests import Request
from starlette.routing import Mount

import ne.api.scim as scim_pkg
from ne.api.scim.filters import ScimFilterError, parse_eq_filter, parse_members_value_path
from ne.api.scim.models import PatchRequest, ScimUser, describe_validation_error
from ne.api.scim.router import _parse_patch_request
from ne.api.scim.service import ScimBadRequestError

from .conftest import AppBuilder, run

_TOKEN = "a" * 32

# Obviously synthetic — no real identity attribute, and unique enough that a
# substring sweep over a whole traceback cannot match it by accident.
_SENTINEL = "SENTINEL-scim-c12-not-a-real-identity-4f19"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _formatted(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


class _FakeRequest:
    """Just enough ``Request`` for ``_parse_patch_request``: an awaitable ``json()``."""

    def __init__(self, raw: bytes | Any) -> None:
        self._raw = raw

    async def json(self) -> Any:
        if isinstance(self._raw, bytes):
            return json.loads(self._raw)
        return self._raw


def _request(raw: bytes | Any) -> Request:
    return cast(Request, _FakeRequest(raw))


def _scim_app(app: Any) -> Any:
    return next(r.app for r in app.routes if isinstance(r, Mount) and r.path == "/scim/v2")


def _endpoint(app: Any, method: str, path: str) -> Any:
    """The route's own callable, so a raise site can be observed at ITS raise.

    Through the ASGI stack the ``ScimServiceError`` handler converts the
    exception into a 400 response and the exception object — the thing carrying
    ``__cause__`` and the formatted chain — is gone.

    ``iter_route_contexts`` (not a raw ``app.routes`` walk) because fastapi
    0.139 keeps included routers as a lazy ``_IncludedRouter`` placeholder; see
    ``test_extraction_wiring._walk_composed_routes`` for the same resolution.
    """
    return next(
        ctx.endpoint
        for ctx in iter_route_contexts(_scim_app(app).routes)
        if ctx.path == path and method in (ctx.methods or ())
    )


# --- site: _parse_patch_request, JSON-decode link ---------------------------


def test_malformed_json_refusal_severs_the_chain_and_reports_only_a_position() -> None:
    """STRUCTURAL pin only — and that is the whole honest claim for this link.

    ``json.JSONDecodeError`` renders ``Expecting ... : line L column C`` and
    never the document, so a sentinel in the body cannot discriminate ``from
    None`` from ``from exc`` here. What CAN be asserted is that the chain is
    severed and that the position — the entire diagnostic value of the cause —
    survives in the refusal itself. The ``except`` clause is unbounded, which is
    why the link is severed at all: nothing there pins the cause class.
    """
    body = b'{"userName": "' + _SENTINEL.encode() + b'", '  # truncated: a syntax error

    with pytest.raises(ScimBadRequestError) as ei:
        run(_parse_patch_request(_request(body)))

    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__
    assert ei.value.scim_type == "invalidSyntax"
    assert "malformed JSON body" in ei.value.detail
    assert "line 1 column" in ei.value.detail  # position survives severing
    assert _SENTINEL not in _formatted(ei.value)

    # CONTROL (should-fire on the OPPOSITE property): the sweep shape above is
    # not vacuous — a cause whose rendering DOES carry the sentinel is caught by
    # the identical ``_formatted`` assertion.
    leaky = ScimBadRequestError("invalidSyntax", "malformed JSON body")
    leaky.__cause__ = ValueError(_SENTINEL)
    assert _SENTINEL in _formatted(leaky)


# --- site: _parse_patch_request, pydantic link ------------------------------


def test_patch_envelope_refusal_names_fields_never_the_rejected_value() -> None:
    """The PATCH envelope is ``extra="forbid"``: an unmapped key is a hard refusal.

    The supplied KEY is caller text too (pydantic reports it in ``loc`` for
    ``extra_forbidden``), so it is counted, not quoted.
    """
    payload = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "replace", "path": "userName", "value": _SENTINEL}],
        _SENTINEL: "x",
    }

    with pytest.raises(ScimBadRequestError) as ei:
        run(_parse_patch_request(_request(payload)))

    assert ei.value.scim_type == "invalidSyntax"
    assert ei.value.detail == "request failed validation: 1 undeclared field rejected"
    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__
    assert _SENTINEL not in _formatted(ei.value), "exception chain leaks the rejected value"

    # CONTROL (should-fire): the raw pydantic error under the SAME assertion
    # shapes carries the sentinel in both carriers — message and chain.
    with pytest.raises(ValidationError) as control:
        PatchRequest.model_validate(payload)
    assert _SENTINEL in str(control.value)
    assert _SENTINEL in _formatted(control.value)


def test_patch_envelope_refusal_names_a_declared_field_that_is_missing() -> None:
    """Presence control for the composer: a DECLARED field IS named, so the
    value-free sweep above is not passing merely because the message is empty."""
    payload = {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"]}

    with pytest.raises(ScimBadRequestError) as ei:
        run(_parse_patch_request(_request(payload)))

    assert ei.value.detail == "request failed validation: 'Operations' is required"


def test_describe_validation_error_never_reads_input_msg_or_ctx() -> None:
    """The composer itself, at the boundary: same errors in, no value out.

    ``msg`` and ``ctx`` are not simply unused-by-accident — a custom validator's
    message and a constraint's context can both embed the rejected value, so the
    composer reads only ``loc`` and ``type``.
    """
    with pytest.raises(ValidationError) as control:
        ScimUser.model_validate({"externalId": _SENTINEL, "active": _SENTINEL})

    raw = control.value.errors()
    message = describe_validation_error(raw)
    assert _SENTINEL not in message
    assert "'userName' is required" in message
    assert "'active' is invalid (bool_parsing)" in message

    # CONTROL (should-fire): those same errors DO carry the value; the composer
    # is what removes it, not an absence of anything to remove.
    assert any(_SENTINEL in str(error.get("input")) for error in raw)
    assert _SENTINEL in str(control.value)


# --- site: GET /Users and GET /Groups filter links --------------------------


@pytest.mark.parametrize(
    ("path", "supported"),
    [("/Users", "externalId, userName"), ("/Groups", "displayName, externalId")],
)
def test_list_filter_refusal_names_the_supported_attributes_never_the_value(
    make_app: AppBuilder, path: str, supported: str
) -> None:
    h = make_app(scim_bearer_token=_TOKEN)
    endpoint = _endpoint(h.app, "GET", path)

    with pytest.raises(ScimBadRequestError) as ei:
        run(endpoint(filter=f'notAnAttr eq "{_SENTINEL}"', startIndex=1, count=100))

    assert ei.value.scim_type == "invalidFilter"
    assert ei.value.detail == f"filter attribute is not supported; supported: {supported}"
    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__
    assert _SENTINEL not in _formatted(ei.value)

    # The other refusal branch of the same parser: an unparseable expression.
    with pytest.raises(ScimBadRequestError) as ei2:
        run(endpoint(filter=f'userName eq "{_SENTINEL}" and 1=1', startIndex=1, count=100))
    assert ei2.value.detail == 'filter must have the shape <attr> eq "<value>"'
    assert ei2.value.__cause__ is None
    assert _SENTINEL not in _formatted(ei2.value)


def test_filter_parser_itself_never_puts_the_value_in_its_error() -> None:
    """The wrapper is the SOURCE: its message is copied verbatim into the
    ``ScimBadRequestError`` above, so severing the chain there would be theatre
    if the value were still in the text being copied."""
    for bad in (f'userName eq "{_SENTINEL}" and 1=1', f'notAnAttr eq "{_SENTINEL}"'):
        with pytest.raises(ScimFilterError) as ei:
            parse_eq_filter(bad, {"username": "userName", "externalid": "externalId"})
        assert _SENTINEL not in ei.value.detail
        assert _SENTINEL not in _formatted(ei.value)

    with pytest.raises(ScimFilterError) as ei3:
        parse_members_value_path(f'members[value eq "{_SENTINEL}" or 1=1]')
    assert ei3.value.scim_type == "invalidPath"
    assert _SENTINEL not in ei3.value.detail
    assert _SENTINEL not in _formatted(ei3.value)

    # CONTROL (should-fire): the same assertion shape over an error that DOES
    # carry the value fails the sweep.
    leaky = ScimFilterError("invalidFilter", f"unsupported filter: {_SENTINEL!r}")
    assert _SENTINEL in leaky.detail
    assert _SENTINEL in _formatted(leaky)


# --- site: _patch_group_sync members-path link (governed WRITE lane) ---------


def test_group_patch_members_path_refusal_is_value_free_on_the_write_lane(
    make_app: AppBuilder,
) -> None:
    """The governed write lane: this path filter carries the id of the principal
    being REMOVED from a group, which is the whole point of the operation."""
    h = make_app(scim_bearer_token=_TOKEN)

    async def _seed() -> None:
        async with h.client() as client:
            r = await client.post(
                "/scim/v2/Groups",
                json={"displayName": "c12-sweep", "externalId": "grp-c12-sweep"},
                headers=_headers(),
            )
            assert r.status_code == 201

    run(_seed())

    patch_group = _endpoint(h.app, "PATCH", "/Groups/{group_id}")
    body = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "remove", "path": f'members[value eq "{_SENTINEL}" or 1=1]'}],
    }

    with pytest.raises(ScimBadRequestError) as ei:
        run(patch_group(group_id="grp-c12-sweep", request=_request(body)))

    assert ei.value.scim_type == "invalidPath"
    assert ei.value.detail == 'members path filter must have the shape members[value eq "<id>"]'
    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__
    assert _SENTINEL not in _formatted(ei.value)

    # CONTROL (should-fire): the SAME two assertion shapes over a refusal that
    # does carry the id — equality on ``detail`` and the ``_formatted`` sweep —
    # both catch it, so neither is passing by construction on this lane.
    leaky = ScimBadRequestError("invalidPath", f"cannot remove member {_SENTINEL!r}")
    leaky.__cause__ = ScimFilterError("invalidPath", _SENTINEL)
    assert leaky.detail != 'members path filter must have the shape members[value eq "<id>"]'
    assert _SENTINEL in leaky.detail
    assert _SENTINEL in _formatted(leaky)


# --- site: _on_validation_error (FastAPI-bound bodies and query params) ------


def test_fastapi_bound_body_refusal_returns_no_identity_attribute_on_the_wire(
    make_app: AppBuilder,
) -> None:
    """``str(RequestValidationError)`` renders each error's ``input`` — and for a
    missing top-level field that input is the WHOLE request body, so one mistyped
    ``active`` would return every attribute the IdP just pushed."""
    h = make_app(scim_bearer_token=_TOKEN)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/scim/v2/Users",
                json={"externalId": _SENTINEL, "displayName": _SENTINEL, "active": "maybe"},
                headers=_headers(),
            )
            assert r.status_code == 400
            assert r.headers["content-type"].startswith("application/scim+json")
            body = r.json()
            # Presence controls: this IS the refusal, and it names the fields.
            assert body["scimType"] == "invalidSyntax"
            assert "'body.userName' is required" in body["detail"]
            assert "'body.active' is invalid (bool_parsing)" in body["detail"]
            assert _SENTINEL not in r.text

            # The query-parameter half of the same handler.
            r2 = await client.get("/scim/v2/Users", params={"count": _SENTINEL}, headers=_headers())
            assert r2.status_code == 400
            assert r2.json()["scimType"] == "invalidSyntax"
            assert "'query.count' is invalid" in r2.json()["detail"]
            assert _SENTINEL not in r2.text

    run(_t())


# --- the two DISCOVERY guards ------------------------------------------------
#
# Everything below this line either enumerates its own subject from the source /
# the route table, or is named for exactly the cases it lists. There is no
# hand-written list standing behind a universal name.

_SCIM_SRC = Path(str(scim_pkg.__file__)).parent

# DENY BY DEFAULT. A refusal argument that is not a literal (or a name resolving
# to one) fails unless its exact unparsed source appears here with a reason. The
# point of the allowlist is that widening it is a visible, reviewable act.
_JUSTIFIED_EXPRESSIONS: dict[str, str] = {
    # Conduits. ``exc`` is our own ScimFilterError/ScimServiceError — every
    # construction of those is itself scanned by this test — or the Starlette
    # HTTPException raised by ne.api.scim.auth (the constant "unauthorized",
    # never the supplied token) / by Starlette routing ("Not Found").
    "exc.detail": "verbatim detail of an already-scanned refusal, or Starlette's fixed vocabulary",
    "exc.scim_type": "a SCIM scimType token from a fixed vocabulary, not caller text",
    "exc.status": "an HTTP status integer",
    "exc.status_code": "an HTTP status integer",
    # A position in the request, never its text.
    "exc.lineno": "json.JSONDecodeError line number",
    "exc.colno": "json.JSONDecodeError column number",
    # The server's OWN published filter vocabulary: both call sites pass a
    # module-level constant map (_USER_FILTER_ATTRS / _GROUP_FILTER_ATTRS).
    "', '.join(sorted(allowed_attrs.values()))": "server-published filter attribute names",
}

#: Calls whose result is value-free by their own tests, not by inspection here.
_VALUE_FREE_COMPOSERS = {
    "describe_validation_error": "the field-only composer, swept by its own tests above",
}

#: Functions whose refusal construction only forwards their PARAMETERS — checked
#: instead at each of their call sites, which this same scan covers.
_CONDUIT_FUNCTIONS = {"_error_response": "forwards its parameters; every call site is scanned"}


class _Assignments:
    """Name -> every expression assigned to it in one scope."""

    def __init__(self) -> None:
        self.bindings: dict[str, list[ast.expr]] = {}

    def collect(self, node: ast.AST) -> None:
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        self.bindings.setdefault(target.id, []).append(stmt.value)
            elif (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.value is not None
            ):
                self.bindings.setdefault(stmt.target.id, []).append(stmt.value)


class _RefusalScan:
    """Every refusal construction in ``ne.api.scim``, found rather than listed."""

    def __init__(self, sources: dict[str, str] | None = None) -> None:
        self.sources = sources if sources is not None else self._package_sources()
        self.trees = {name: ast.parse(src) for name, src in self.sources.items()}
        self.module_scope: dict[str, _Assignments] = {}
        self.imported: dict[str, dict[str, tuple[str, str]]] = {}
        for name, tree in self.trees.items():
            scope = _Assignments()
            for stmt in tree.body:
                if isinstance(stmt, ast.Assign | ast.AnnAssign):
                    scope.collect(stmt)
            self.module_scope[name] = scope
            redirects: dict[str, tuple[str, str]] = {}
            for stmt in ast.walk(tree):
                if isinstance(stmt, ast.ImportFrom) and (stmt.module or "").startswith(
                    "ne.api.scim."
                ):
                    origin = (stmt.module or "").rsplit(".", 1)[-1]
                    for alias in stmt.names:
                        redirects[alias.asname or alias.name] = (origin, alias.name)
            self.imported[name] = redirects
        self.constructors = self._refusal_constructors()

    @staticmethod
    def _package_sources() -> dict[str, str]:
        return {
            info.name: (_SCIM_SRC / f"{info.name}.py").read_text()
            for info in pkgutil.iter_modules([str(_SCIM_SRC)])
        }

    @staticmethod
    def _refusal_constructors() -> frozenset[str]:
        """Discovered by IMPORT: every ``BaseException`` subclass bound in any
        module of the package, plus the two non-exception ways a ``detail``
        reaches the wire. Nothing here names a SCIM error class explicitly, so a
        new one is scanned the moment it is bound."""
        names = {"ScimError", "_error_response"}
        for info in pkgutil.iter_modules([str(_SCIM_SRC)]):
            module = importlib.import_module(f"ne.api.scim.{info.name}")
            names.update(
                attr
                for attr, obj in vars(module).items()
                if isinstance(obj, type) and issubclass(obj, BaseException)
            )
        return frozenset(names)

    def _resolve(self, name: str, module: str, local: _Assignments) -> list[ast.expr] | None:
        if name in local.bindings:
            return local.bindings[name]
        if name in self.module_scope[module].bindings:
            return self.module_scope[module].bindings[name]
        origin, original = self.imported[module].get(name, ("", ""))
        if origin in self.module_scope and original in self.module_scope[origin].bindings:
            return self.module_scope[origin].bindings[original]
        return None

    def _value_free(self, node: ast.expr, module: str, local: _Assignments, depth: int = 0) -> bool:
        if depth > 12:  # pathological nesting is refused, not explored
            return False
        deeper = depth + 1
        if isinstance(node, ast.Constant):
            return True
        if ast.unparse(node) in _JUSTIFIED_EXPRESSIONS:
            return True
        if isinstance(node, ast.Name):
            bound = self._resolve(node.id, module, local)
            return bool(bound) and all(self._value_free(v, module, local, deeper) for v in bound)
        if isinstance(node, ast.JoinedStr):
            return all(self._value_free(v, module, local, deeper) for v in node.values)
        if isinstance(node, ast.FormattedValue):
            return self._value_free(node.value, module, local, deeper)
        if isinstance(node, ast.IfExp):
            return self._value_free(node.body, module, local, deeper) and self._value_free(
                node.orelse, module, local, deeper
            )
        if isinstance(node, ast.Dict):
            entries = [*node.keys, *node.values]
            return all(
                e is not None and self._value_free(e, module, local, deeper) for e in entries
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id in _VALUE_FREE_COMPOSERS
        return False

    def run(self) -> tuple[list[str], list[str]]:
        """Return ``(sites, violations)`` — one entry per refusal construction."""
        sites: list[str] = []
        violations: list[str] = []
        for module, tree in self.trees.items():
            parents: dict[ast.AST, ast.AST] = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called = node.func
                name = (
                    called.id
                    if isinstance(called, ast.Name)
                    else called.attr
                    if isinstance(called, ast.Attribute)
                    else None
                )
                if name not in self.constructors:
                    continue
                local = _Assignments()
                conduit = False
                enclosing: ast.AST | None = parents.get(node)
                while enclosing is not None:
                    if isinstance(enclosing, ast.FunctionDef | ast.AsyncFunctionDef):
                        conduit = conduit or enclosing.name in _CONDUIT_FUNCTIONS
                        for stmt in enclosing.body:
                            local.collect(stmt)
                    enclosing = parents.get(enclosing)
                if conduit:
                    continue
                sites.append(f"{module}.py:{node.lineno} {name}")
                for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                    if not self._value_free(arg, module, local):
                        violations.append(f"{module}.py:{node.lineno} {name} <- {ast.unparse(arg)}")
        return sites, violations


def test_every_scim_refusal_site_is_literal_or_explicitly_justified() -> None:
    """The ONE universal in this module, and it enumerates its own subject.

    Every module of ``ne.api.scim`` is parsed; every construction of every
    ``BaseException`` subclass bound anywhere in the package (found by import,
    not listed here) plus the two non-exception ways a ``detail`` reaches the
    wire is checked. Each argument must be a string literal, a name resolving to
    one, or an entry of the deny-by-default ``_JUSTIFIED_EXPRESSIONS``. A refusal
    site added tomorrow is scanned tomorrow; a hand-listed set would not be.

    This pins the MESSAGE carrier at the source. The wire carrier is pinned by
    the route sweep below, and the exception-chain carrier by the per-site
    ``__cause__`` asserts above.
    """
    scan = _RefusalScan()
    sites, violations = scan.run()

    assert not violations, "refusal arguments that are not value-free:\n" + "\n".join(violations)

    # Presence controls: the scan really did find the surface, so "no violations"
    # cannot be the result of scanning nothing.
    assert {"ScimBadRequestError", "ScimConflictError", "ScimNotFoundError", "ScimFilterError"} <= (
        scan.constructors
    )
    modules_with_sites = {site.split(".py:")[0] for site in sites}
    assert modules_with_sites == {"auth", "filters", "router", "service"}
    assert len(sites) >= 30, sites

    # CONTROL (should-fire, and BIDIRECTIONAL): the identical scan over a module
    # written the way this surface used to be written flags the two interpolating
    # refusals and passes the literal one between them — so the clean result above
    # is the scan discriminating, not the scan being unable to fail.
    leaky = _RefusalScan(
        sources={
            "service": "\n".join(
                [
                    "from ne.api.scim.filters import ScimFilterError",
                    "def f(value, pid):",
                    "    raise ScimFilterError('invalidValue', f'bad members value: {value!r}')",
                    "    raise ScimFilterError('invalidValue', 'members value is not supported')",
                    "    raise ScimFilterError('uniqueness', 'id taken: ' + pid)",
                ]
            )
        }
    )
    leaky_sites, leaky_violations = leaky.run()
    assert len(leaky_sites) == 3, leaky_sites
    assert len(leaky_violations) == 2, leaky_violations
    assert any("{value!r}" in v for v in leaky_violations)
    assert any("+ pid" in v for v in leaky_violations)


# --- the wire carrier, universal over ROUTES --------------------------------

_SEED_USER_ID = "usr-c12-seed"
_SEED_USER_NAME = "c12-seed-user"
_SEED_GROUP_ID = "grp-c12-seed"
_SEED_GROUP_NAME = "c12-seed-group"

_PATCH_SCHEMAS = ["urn:ietf:params:scim:api:messages:2.0:PatchOp"]


def _sentinel_variants(resource_id: str) -> list[tuple[str, dict[str, Any], Any]]:
    """The named payload matrix — ``(id, query, body)``.

    Each row puts ``_SENTINEL`` in a DIFFERENT syntactic position of a SCIM
    request: a path segment (supplied by the caller of this function), a
    protocol query parameter, an undeclared query parameter, a scalar body
    attribute, a typed body attribute, a nested member object, an undeclared
    envelope key, a PATCH verb, a PATCH ``value`` (scalar / list item), a PATCH
    path filter, and the resource id of a duplicate create.
    """

    def _patch(**operation: Any) -> dict[str, Any]:
        return {"schemas": _PATCH_SCHEMAS, "Operations": [operation]}

    return [
        ("path-only", {}, None),
        ("filter-unsupported-attr", {"filter": f'notAnAttr eq "{_SENTINEL}"'}, None),
        ("filter-malformed", {"filter": f'userName eq "{_SENTINEL}" and 1=1'}, None),
        ("protocol-param-bad-type", {"count": _SENTINEL}, None),
        ("undeclared-query-param", {_SENTINEL: _SENTINEL}, None),
        ("body-not-an-object", {}, _SENTINEL),
        ("body-scalar-attribute", {}, {"externalId": _SENTINEL, "active": "maybe"}),
        ("body-typed-attribute", {}, {"externalId": _SENTINEL, "members": _SENTINEL}),
        ("body-nested-member", {}, {"displayName": _SENTINEL, "members": [{"nope": _SENTINEL}]}),
        (
            "envelope-undeclared-key",
            {},
            {**_patch(op="replace", path="userName", value=_SENTINEL), _SENTINEL: 1},
        ),
        ("patch-unsupported-verb", {}, _patch(op=_SENTINEL, path="members", value=[])),
        ("patch-members-value-scalar", {}, _patch(op="replace", path="members", value=_SENTINEL)),
        (
            "patch-members-value-item",
            {},
            _patch(op="add", path="members", value=[{"nope": _SENTINEL}]),
        ),
        (
            "patch-members-path-filter",
            {},
            _patch(op="remove", path=f'members[value eq "{_SENTINEL}" or 1=1]'),
        ),
        ("patch-active-not-a-bool", {}, _patch(op="replace", path="active", value=_SENTINEL)),
        (
            "patch-username-not-a-string",
            {},
            _patch(op="replace", path="userName", value=[_SENTINEL]),
        ),
        (
            "duplicate-resource-id",
            {},
            {
                "userName": f"{_SENTINEL}-dup",
                "displayName": f"{_SENTINEL}-dup",
                "externalId": resource_id,
            },
        ),
        (
            "duplicate-unique-name",
            {},
            {
                "userName": _SEED_USER_NAME,
                "displayName": _SEED_GROUP_NAME,
                "externalId": f"{_SENTINEL}-other",
            },
        ),
    ]


def test_every_declared_route_refuses_the_sentinel_matrix_without_echoing_it(
    make_app: AppBuilder,
) -> None:
    """Universal over ROUTES, existential over PAYLOADS — and the name says so.

    The routes are read from the mounted app's own table, so an endpoint added
    later is swept without anyone remembering to add it; the payloads are the
    named matrix in :func:`_sentinel_variants`. Two passes: one with the sentinel
    itself as the path id (the 404 lanes), one with seeded ids that exist (the
    400/409 service lanes behind them).

    Only responses >= 400 are asserted. A 2xx SCIM response is the resource
    representation and echoing the attributes back is its entire purpose; the
    refusals are what must not.
    """
    h = make_app(scim_bearer_token=_TOKEN)
    seen_status: set[int] = set()
    seen_scim_type: set[str] = set()
    checked = 0

    routes: list[tuple[str, str]] = sorted(
        {
            (str(method), str(ctx.path))
            for ctx in iter_route_contexts(_scim_app(h.app).routes)
            for method in (ctx.methods or ())
            if method != "HEAD" and ctx.path
        }
    )
    # Presence control on the discovery itself: the app really does expose the
    # Users/Groups surface, so an empty or truncated route table cannot make the
    # sweep below pass by sweeping nothing.
    assert {"/Users", "/Users/{user_id}", "/Groups", "/Groups/{group_id}"} <= {
        route for _method, route in routes
    }, routes

    async def _t() -> None:
        nonlocal checked
        async with h.client() as client:
            for body in (
                {"userName": _SEED_USER_NAME, "externalId": _SEED_USER_ID},
                {"displayName": _SEED_GROUP_NAME, "externalId": _SEED_GROUP_ID},
            ):
                seed = await client.post(
                    "/scim/v2/Users" if "userName" in body else "/scim/v2/Groups",
                    json=body,
                    headers=_headers(),
                )
                assert seed.status_code == 201, seed.text

            for seeded in (False, True):
                for method, route in routes:
                    # DELETE is exercised only against a non-existent id: run
                    # against the seeded ids it would remove the fixtures the
                    # later rows in the same pass depend on.
                    if seeded and method == "DELETE":
                        continue
                    is_group = route.startswith("/Groups")
                    resource_id = (
                        (_SEED_GROUP_ID if is_group else _SEED_USER_ID) if seeded else _SENTINEL
                    )
                    url = "/scim/v2" + re.sub(r"\{[^}]+\}", resource_id, route)
                    for variant, query, payload in _sentinel_variants(resource_id):
                        r = await client.request(
                            method, url, params=query, json=payload, headers=_headers()
                        )
                        if r.status_code < 400:
                            continue
                        checked += 1
                        seen_status.add(r.status_code)
                        try:
                            parsed = r.json()
                        except ValueError:  # pragma: no cover - all errors are JSON
                            parsed = None
                        if isinstance(parsed, dict) and parsed.get("scimType"):
                            seen_scim_type.add(str(parsed["scimType"]))
                        assert _SENTINEL not in r.text, (
                            f"{method} {url} [{variant}] {r.status_code}: {r.text}"
                        )

    run(_t())

    # Presence controls: the matrix really reached the refusal lanes, so the
    # sweep above cannot be passing because everything answered 2xx. Each entry
    # below is produced by a DIFFERENT composer.
    assert {400, 404, 409} <= seen_status, seen_status
    assert {
        "invalidFilter",
        "invalidPath",
        "invalidSyntax",
        "invalidValue",
        "uniqueness",
    } <= seen_scim_type, seen_scim_type
    assert checked >= 100, checked


# --- the service-layer lanes, named one at a time ---------------------------


@pytest.mark.parametrize(
    ("lane", "method", "path", "payload", "status", "detail"),
    [
        (
            "400 members value",
            "PATCH",
            f"/scim/v2/Groups/{_SEED_GROUP_ID}",
            {
                "schemas": _PATCH_SCHEMAS,
                "Operations": [{"op": "replace", "path": "members", "value": _SENTINEL}],
            },
            400,
            "unsupported members value; expected a member object with a 'value' key, "
            "a string id, or a list of either",
        ),
        (
            "400 members value item",
            "PATCH",
            f"/scim/v2/Groups/{_SEED_GROUP_ID}",
            {
                "schemas": _PATCH_SCHEMAS,
                "Operations": [{"op": "add", "path": "members", "value": [{"no": _SENTINEL}]}],
            },
            400,
            "unsupported members value item; expected a member object with a 'value' key, "
            "or a string id",
        ),
        (
            "400 active",
            "PATCH",
            f"/scim/v2/Users/{_SEED_USER_ID}",
            {
                "schemas": _PATCH_SCHEMAS,
                "Operations": [{"op": "replace", "path": "active", "value": _SENTINEL}],
            },
            400,
            "'active' must be a boolean",
        ),
        (
            "400 PATCH verb",
            "PATCH",
            f"/scim/v2/Users/{_SEED_USER_ID}",
            {"schemas": _PATCH_SCHEMAS, "Operations": [{"op": _SENTINEL, "value": 1}]},
            400,
            "unsupported PATCH op; supported: add, remove, replace",
        ),
        (
            "409 duplicate resource id",
            "POST",
            "/scim/v2/Users",
            {"userName": f"{_SENTINEL}-dup", "externalId": _SEED_USER_ID},
            409,
            "resource id already provisioned; the id derives from externalId, else userName",
        ),
        (
            "409 duplicate userName",
            "POST",
            "/scim/v2/Users",
            {"userName": _SEED_USER_NAME, "externalId": f"{_SENTINEL}-other"},
            409,
            "userName already provisioned",
        ),
        (
            "404 unknown user",
            "GET",
            f"/scim/v2/Users/{_SENTINEL}",
            None,
            404,
            "no such user",
        ),
        (
            "404 unknown group",
            "GET",
            f"/scim/v2/Groups/{_SENTINEL}",
            None,
            404,
            "no such group",
        ),
    ],
)
def test_service_refusal_lane_names_the_field_on_the_wire(
    make_app: AppBuilder,
    lane: str,
    method: str,
    path: str,
    payload: Any,
    status: int,
    detail: str,
) -> None:
    """One row per service-layer refusal lane, asserted by EQUALITY on ``detail``.

    The 409s and 404s are here on a decision, not by omission. Both echo an
    identifier the caller supplied in the same request, which is not the same
    class of disclosure as returning a value they never sent — but naming the
    value buys the caller nothing (they can recompute the id from their own
    body; the 404 path segment is already in the request line) while it does put
    an identity attribute into every log that reads the refusal. Uniform beats
    nearly-uniform here for a second reason: only a rule without exceptions can
    be checked by construction, which is what
    ``test_every_scim_refusal_site_is_literal_or_explicitly_justified`` does.
    """
    h = make_app(scim_bearer_token=_TOKEN)

    async def _t() -> None:
        async with h.client() as client:
            for seed in (
                {"userName": _SEED_USER_NAME, "externalId": _SEED_USER_ID},
                {"displayName": _SEED_GROUP_NAME, "externalId": _SEED_GROUP_ID},
            ):
                r = await client.post(
                    "/scim/v2/Users" if "userName" in seed else "/scim/v2/Groups",
                    json=seed,
                    headers=_headers(),
                )
                assert r.status_code == 201, r.text

            r = await client.request(method, path, json=payload, headers=_headers())
            assert r.status_code == status, (lane, r.text)
            assert r.json()["detail"] == detail, lane
            assert _SENTINEL not in r.text

    run(_t())


# --- describe_validation_error: the two loc guards, pinned SEPARATELY --------


def test_a_caller_supplied_loc_is_refused_a_name_under_any_error_type() -> None:
    """``_field_path``'s membership test, pinned WITHOUT ``extra_forbidden``.

    The two guards in the composer are mutually redundant for TODAY's models —
    a caller-chosen key normally arrives as ``extra_forbidden``, so either guard
    alone keeps the surface clean and either could be deleted with the suite
    still green. This case removes that cover: a caller-shaped ``loc`` under a
    NON-``extra_forbidden`` type, which is exactly the shape the docstring
    promises is "covered the day it appears".
    """
    caller_key = [{"type": "value_error", "loc": ("body", _SENTINEL), "input": _SENTINEL}]
    message = describe_validation_error(caller_key)
    assert _SENTINEL not in message
    assert message == "request failed validation: 1 undeclared field rejected"

    # CONTROL (should PASS): a loc built only from published names IS spoken, so
    # the assertion above is not passing because the composer says nothing.
    published = [{"type": "value_error", "loc": ("body", "userName")}]
    assert describe_validation_error(published) == (
        "request failed validation: 'body.userName' is invalid (value_error)"
    )
    # ...and a list INDEX inside an otherwise published path stays speakable.
    indexed = [{"type": "missing", "loc": ("body", "members", 0, "value")}]
    assert describe_validation_error(indexed) == (
        "request failed validation: 'body.members.0.value' is required"
    )


def test_extra_forbidden_is_counted_as_undeclared_even_for_a_published_name() -> None:
    """The ``extra_forbidden`` branch, pinned WITHOUT the membership test.

    This branch is a CLASSIFICATION guard, not the value guard, and the case
    that pins it says so: a caller may name an extra key after a field this
    server publishes elsewhere (``displayName`` is not declared on the PatchOp
    envelope), at which point ``_field_path`` would render it happily and the
    refusal would misdescribe a rejected EXTRA key as an invalid DECLARED field.
    Echoing a name the server itself publishes is not a value leak — which is
    precisely why the membership test cannot stand in for this branch.
    """
    published_extra = [{"type": "extra_forbidden", "loc": ("body", "displayName")}]
    assert describe_validation_error(published_extra) == (
        "request failed validation: 1 undeclared field rejected"
    )

    # The same thing on the real envelope, so the unit case above is not a shape
    # only this test can produce.
    payload = {
        "schemas": _PATCH_SCHEMAS,
        "Operations": [{"op": "replace", "path": "userName", "value": _SENTINEL}],
        "displayName": _SENTINEL,
    }
    with pytest.raises(ScimBadRequestError) as ei:
        run(_parse_patch_request(_request(payload)))
    assert ei.value.detail == "request failed validation: 1 undeclared field rejected"

    # CONTROL (should PASS): the SAME loc under a declared-field error IS named,
    # so what the assertions above pin is the classification, not a muted composer.
    assert describe_validation_error([{"type": "missing", "loc": ("body", "displayName")}]) == (
        "request failed validation: 'body.displayName' is required"
    )
