# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE C2 egress structural guardian (plan §2.1, C2 registry entry).

Makes the audit-#1 class — "a bare HTTP client to a config-supplied URL inside
ne-brain, POSTing corpus/query text without SSRF/in-VPC validation" — impossible
to reintroduce silently. Three layers:

(a) IMPORT ALLOWLIST — only ``embedder.py`` and ``reranker.py`` under
    ``src/ne/brain`` may import an outbound-HTTP client library. A new importer
    fails CI until it is routed through ``validate_in_vpc_endpoint`` and the
    author consciously extends ``ALLOWED_HTTP_MODULES`` here.
(b) VALIDATED CONSTRUCTION — in each allowlisted module every ``self._base_url``
    assignment must be a ``validate_in_vpc_endpoint(...)`` call, so the endpoint
    is validated at ``__init__`` time before any client can POST.
(c) BEHAVIORAL RED-TEAM — both clients raise ``UnsafeEndpointError`` at
    construction on hostile endpoints and construct on in-VPC/allowlisted ones,
    and a MockTransport-recording client proves ZERO requests ever leave when the
    endpoint is rejected.

Scope note: qdrant-client / an injected ``AsyncQdrantClient`` endpoint is the
composition root's responsibility (ne-brain never constructs one FROM CONFIG);
ne-serve's ``ServeClient`` outbound endpoint is guarded by NE-CKC7, out of this
diff.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from ne.brain.embedder import TEIEmbedder
from ne.brain.reranker import TEIReranker, VllmReranker
from ne.brain.sovereignty import UnsafeEndpointError

_SRC = Path(__file__).resolve().parents[1] / "src" / "ne" / "brain"

# The ONLY ne-brain modules permitted to import an outbound-HTTP client library.
ALLOWED_HTTP_MODULES = {"embedder.py", "reranker.py"}

_FORBIDDEN_TOP_LEVEL = {"httpx", "requests", "aiohttp", "urllib3", "socket"}
_FORBIDDEN_DOTTED_PREFIXES = ("urllib.request", "http.client")

_GUARD_MESSAGE = (
    "ne-brain outbound HTTP is C2-guarded: construct clients only with a base "
    "endpoint validated by ne.brain.sovereignty.validate_in_vpc_endpoint at "
    "__init__ time, then consciously extend ALLOWED_HTTP_MODULES in this guardian."
)


def _module_imports_http_client(tree: ast.AST) -> bool:
    """True if ``tree`` imports any forbidden outbound-HTTP client module."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _FORBIDDEN_TOP_LEVEL or alias.name.startswith(_FORBIDDEN_DOTTED_PREFIXES):
                    return True
        elif isinstance(node, ast.ImportFrom):
            # Absolute imports only (node.level == 0); a relative import cannot
            # reach a stdlib/third-party HTTP client.
            if node.level != 0 or node.module is None:
                continue
            top = node.module.split(".")[0]
            if top in _FORBIDDEN_TOP_LEVEL or node.module.startswith(_FORBIDDEN_DOTTED_PREFIXES):
                return True
    return False


def test_a_only_allowlisted_modules_import_an_http_client() -> None:
    offenders: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _module_imports_http_client(tree):
            offenders.add(path.relative_to(_SRC).as_posix())

    # Allowlisted modules live directly under src/ne/brain.
    expected = {name for name in ALLOWED_HTTP_MODULES}
    assert offenders == expected, f"{_GUARD_MESSAGE}\n  offenders={sorted(offenders)}"


def _is_self_base_url(target: ast.expr) -> bool:
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "_base_url"
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    )


def _call_is_validate_in_vpc_endpoint(value: ast.expr) -> bool:
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if isinstance(func, ast.Name):
        return func.id == "validate_in_vpc_endpoint"
    if isinstance(func, ast.Attribute):
        return func.attr == "validate_in_vpc_endpoint"
    return False


@pytest.mark.parametrize("module_name", sorted(ALLOWED_HTTP_MODULES))
def test_b_base_url_assignments_go_through_the_validator(module_name: str) -> None:
    tree = ast.parse((_SRC / module_name).read_text(encoding="utf-8"))
    base_url_assignments = [
        assign
        for assign in ast.walk(tree)
        if isinstance(assign, ast.Assign) and any(_is_self_base_url(t) for t in assign.targets)
    ]
    assert base_url_assignments, f"{module_name}: expected a self._base_url assignment"
    for assign in base_url_assignments:
        assert _call_is_validate_in_vpc_endpoint(assign.value), (
            f"{module_name}: a self._base_url assignment is NOT wrapped in "
            f"validate_in_vpc_endpoint(...). {_GUARD_MESSAGE}"
        )


# -- Layer (c): behavioral red-team --------------------------------------------

_HOSTILE = [
    "http://169.254.169.254",
    "http://metadata.google.internal",
    "https://evil.example.com",
    "http://2852039166",
]

#: EVERY ne-brain client that POSTs corpus/query text to a config-supplied URL.
#: A new adapter that is not listed here is outside this guardian's behavioral
#: sweep — which is exactly how ``VllmReranker`` shipped with an air-gap hole its
#: sibling did not have. ``test_c_factories_cover_every_outbound_client`` below
#: makes the omission itself fail, so the list cannot silently fall behind again.
_FACTORIES: list[tuple[str, Callable[..., object]]] = [
    ("TEIEmbedder", lambda url, **kw: TEIEmbedder(url, **kw)),
    ("TEIReranker", lambda url, **kw: TEIReranker(url, **kw)),
    ("VllmReranker", lambda url, **kw: VllmReranker(url, **kw)),
]


@pytest.mark.parametrize(("name", "factory"), _FACTORIES, ids=[f[0] for f in _FACTORIES])
@pytest.mark.parametrize("hostile", _HOSTILE)
def test_c_construction_refuses_hostile_endpoints(
    name: str, factory: Callable[..., object], hostile: str
) -> None:
    with pytest.raises(UnsafeEndpointError):
        factory(hostile)


@pytest.mark.parametrize(("name", "factory"), _FACTORIES, ids=[f[0] for f in _FACTORIES])
def test_c_construction_accepts_in_vpc_and_allowlisted_endpoints(
    name: str, factory: Callable[..., object]
) -> None:
    factory("http://tei.local")
    factory("http://localhost:8085")
    factory("https://tei.corp.example.com", allowed_hosts=("tei.corp.example.com",))


def test_c_factories_cover_every_outbound_client() -> None:
    """The list above must be DERIVED from the source, not maintained by memory.

    Every class in an allowlisted module that validates a ``self._base_url`` is an
    outbound client carrying corpus/query text, and must be swept by layer (c).
    ``VllmReranker`` was added to ``reranker.py`` and NOT to ``_FACTORIES``, so the
    behavioral red-team never touched it and it shipped an AIR_GAP hole its sibling
    ``TEIReranker`` would have been caught on. This test is what makes the next
    such omission fail here instead of in a review.
    """
    declared: set[str] = set()
    for module_name in ALLOWED_HTTP_MODULES:
        tree = ast.parse((_SRC / module_name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            validates_base_url = any(
                isinstance(assign, ast.Assign)
                and any(_is_self_base_url(t) for t in assign.targets)
                and _call_is_validate_in_vpc_endpoint(assign.value)
                for assign in ast.walk(node)
            )
            if validates_base_url:
                declared.add(node.name)

    assert declared == {name for name, _ in _FACTORIES}, (
        "an ne-brain outbound HTTP client is missing from this guardian's "
        f"_FACTORIES sweep. {_GUARD_MESSAGE}\n  in source={sorted(declared)}\n"
        f"  swept={sorted(name for name, _ in _FACTORIES)}"
    )


def _recording_client(recorded: list[httpx.Request]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json=[])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(("name", "factory"), _FACTORIES, ids=[f[0] for f in _FACTORIES])
def test_c_rejected_endpoint_never_emits_a_request(
    name: str, factory: Callable[..., object]
) -> None:
    # Even with a live (recording) transport handed in alongside a hostile URL,
    # construction fails first and ZERO requests ever leave — the exfil-never-
    # POSTs proof.
    recorded: list[httpx.Request] = []
    client = _recording_client(recorded)
    with pytest.raises(UnsafeEndpointError):
        factory("http://169.254.169.254", client=client)
    assert recorded == []


# -- Layer (d): AIR_GAP dominates the allowlist ---------------------------------
#
# The allowlist is the ONE way a config can aim a ne-brain client at a host that
# is not provably in-VPC. That makes it an egress path, and an egress path a
# sealed deployment cannot shut is not an air gap. The rule and its derivation
# from ne-gateway's precedent live in ne.net.ssrf.allowlist_revoked_by_air_gap;
# this sweeps EVERY outbound client against it, both directions.


@pytest.mark.parametrize(
    ("endpoint", "allowlisted"),
    [
        ("http://8.8.8.8:1837", "8.8.8.8"),  # public IP literal
        ("http://dns.google:1837", "dns.google"),  # the NAME it resolves to
        ("https://tei.corp.example.com", "tei.corp.example.com"),  # corporate FQDN
    ],
)
@pytest.mark.parametrize(("name", "factory"), _FACTORIES, ids=[f[0] for f in _FACTORIES])
def test_d_air_gap_revokes_any_allowlist_only_host(
    name: str,
    factory: Callable[..., object],
    endpoint: str,
    allowlisted: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host admitted ONLY by the allowlist is refused under the gap however it
    is spelled — the IP literal, the name that resolves to it, and a corporate
    FQDN alike. The spelling-dependence was the defect."""
    monkeypatch.setenv("AIR_GAP", "1")
    recorded: list[httpx.Request] = []
    client = _recording_client(recorded)
    with pytest.raises(UnsafeEndpointError, match="AIR_GAP is engaged"):
        factory(endpoint, allowed_hosts=(allowlisted,), client=client)
    assert recorded == []


@pytest.mark.parametrize(("name", "factory"), _FACTORIES, ids=[f[0] for f in _FACTORIES])
def test_d_air_gap_control_same_call_constructs_with_the_gap_open(
    name: str, factory: Callable[..., object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTROL: the identical construction succeeds with the gap disengaged, so
    the denial above is the air gap and not the allowlist failing generally."""
    monkeypatch.delenv("AIR_GAP", raising=False)
    factory("http://8.8.8.8:1837", allowed_hosts=("8.8.8.8",))


@pytest.mark.parametrize(("name", "factory"), _FACTORIES, ids=[f[0] for f in _FACTORIES])
def test_d_air_gap_leaves_the_in_vpc_lane_alive(
    name: str, factory: Callable[..., object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC C2: AIR_GAP kills EGRESS, not the in-VPC open-weight lane. A sealed
    deployment's on-box sidecars must still construct."""
    monkeypatch.setenv("AIR_GAP", "1")
    factory("http://tei.local")
    factory("http://localhost:8085")
    # Allowlisting a host whose SHAPE already proves it in-VPC must stay a no-op:
    # it needed no grant, so the gap has nothing to revoke.
    factory("http://tei-rerank", allowed_hosts=("tei-rerank",))
    factory("http://10.0.0.5:8085", allowed_hosts=("10.0.0.5",))
