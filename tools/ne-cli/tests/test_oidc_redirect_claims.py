# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-OIDC-REDIRECT-GUARD: every OIDC redirect target the console CLAIMS must be
DECLARED by every realm the repo ships — and vice versa.

The incident this guard exists for: the console's "Compare as another user…"
popup redirected to /auth/compare-callback, a real mounted route, but the pilot
realm's `ne-console` client declared only /auth/callback. Keycloak rejected the
redirect as unauthorized, the popup died on an IdP error page, and the feature
could not have worked in ANY environment — found by the owner clicking it in a
live demo. `post.logout.redirect.uris` was likewise undeclared, so sign-out
stranded the user on Keycloak's logged-out page. This was the SECOND instance of
the claimed-vs-declared shape: `test_pilot_stack.py::
test_pilot_realm_seeded_role_users` already guarded group paths after the same
failure, but only group paths — a guard that enumerates one instance of a shape
misses the next (docs/testing/vacuous-guards.md V3).

HOW THE EXPECTATION IS DERIVED (the load-bearing property): the claimed paths
are NOT a hardcoded list. Every non-test console source under
packages/ne-console/src is comment-stripped and then swept for the four OIDC
redirect keys (`redirect_uri` / `popup_redirect_uri` / `post_logout_redirect_uri`
/ `popup_post_logout_redirect_uri`) in ANY syntactic position — keyed property
(`redirect_uri: <expr>`), quoted property (`"redirect_uri": <expr>`),
assignment (`const redirect_uri = <expr>`), and object shorthand
(`{ redirect_uri }`). Every occurrence must be attributed to a claim the
resolver can reduce to an absolute path through the codebase's idioms
(origin-anchored template literals, string concatenation, same-file helper
functions, and const path constants such as COMPARE_CALLBACK_PATH — a
same-named const with conflicting definitions fails loud instead of picking
one). An occurrence in a position the extractor does not understand, or a
value idiom the resolver does not understand, FAILS LOUD naming the site: in
live code a key token can only produce a claim or a loud failure, never a
silent drop. A mention in a COMMENT is prose and produces nothing.

Stated boundary of the comment scanner (a lexical scan, not a TS parser): it
understands line/block comments, single/double-quoted strings (single-line, so
an apostrophe in JSX prose cannot open a phantom string), and template
literals; it does NOT lex JS regex literals, so a regex literal containing
`//` or `/*` upstream of a claim could desync it. That desync is turned loud
for the claims the console makes today by the recall control
(test_derivation_recalls_the_known_claim_sites), and the extractor's own
planted-RED fixtures (test_extractor_*) pin each attribution form and each
fail-loud path bidirectionally.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CONSOLE_SRC = REPO_ROOT / "packages" / "ne-console" / "src"
APP_TSX = CONSOLE_SRC / "App.tsx"

# Every realm the repo ships that declares the `ne-console` public client.
REALM_FILES = (
    REPO_ROOT / "deploy" / "compose" / "pilot" / "keycloak" / "ne-pilot-realm.json",
    REPO_ROOT / "deploy" / "compose" / "dev" / "keycloak" / "ne-dev-realm.json",
)

# The one wildcard redirect declaration this repo sanctions: a PORT wildcard on
# the dev loopback (vite's shifting dev ports). Any other wildcard — same-host
# (`https://${NE_PILOT_HOSTNAME}/*`) or foreign — is attack surface, not
# convenience: see test_wildcard_redirect_declarations_only_on_the_dev_loopback.
_SANCTIONED_WILDCARD = "http://localhost:*"

# The OIDC settings keys that make a URI a redirect target the IdP must accept.
# Order matters in the alternation: longest first so `redirect_uri` never
# matches inside a longer key.
_SIGNIN_KEYS = frozenset({"redirect_uri", "popup_redirect_uri"})
_POST_LOGOUT_KEYS = frozenset({"post_logout_redirect_uri", "popup_post_logout_redirect_uri"})
_KEY_TOKEN = re.compile(
    r"(?P<q>[\"'])?"
    r"\b(?P<key>popup_post_logout_redirect_uri|popup_redirect_uri"
    r"|post_logout_redirect_uri|redirect_uri)\b"
    r"(?(q)(?P=q))"
)
# A key token attributed as a property with a value expression (`key: expr`,
# quoted or not), as an assignment (`const key = expr`, `obj.key = expr` —
# `(?![=>])` keeps ==/===/=> comparisons and arrows out), or as an object
# shorthand (`{ key }` / `{ key, ... }`).
_PROP_VALUE = re.compile(r"\s*:\s*(?P<expr>[^\n]+)")
_ASSIGNED_VALUE = re.compile(r"\s*=(?![=>])\s*(?P<expr>[^\n]+)")
_SHORTHAND_AFTER = re.compile(r"\s*[,}]")

# `${window.location.origin}<rest>` template literal.
_ORIGIN_TEMPLATE = re.compile(r"^`\$\{window\.location\.origin\}(?P<rest>[^`]*)`$")
# `window.location.origin + <rest>` concatenation.
_ORIGIN_CONCAT = re.compile(r"^window\.location\.origin\s*\+\s*(?P<rest>.+)$")
_TEMPLATE_IDENT = re.compile(r"^\$\{(?P<name>\w+)\}$")
_STRING_LITERAL = re.compile(r"""^(["'])(?P<value>[^"']*)\1$""")
_CALL = re.compile(r"^(?P<name>\w+)\(\)$")
_IDENT = re.compile(r"^(?P<name>\w+)$")


@dataclass(frozen=True)
class Claim:
    path: str
    kind: str  # "signin" | "post_logout"
    source: str  # "<relative file>:<line>"


def _console_sources() -> list[Path]:
    files = [
        p
        for pattern in ("*.ts", "*.tsx")
        for p in CONSOLE_SRC.rglob(pattern)
        if ".test." not in p.name
        and not p.name.endswith(".d.ts")
        and "test" not in p.relative_to(CONSOLE_SRC).parts[:-1]
    ]
    assert files, f"no console sources under {CONSOLE_SRC} — wrong tree?"
    return sorted(files)


def _quote_closes_on_line(text: str, i: int, quote: str) -> bool:
    """Is there an unescaped closing `quote` before the next newline after i?"""
    j = i + 1
    while j < len(text) and text[j] != "\n":
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return True
        j += 1
    return False


def _strip_comments(text: str) -> str:
    """Blank out // and /* */ comment contents (newlines kept, so offsets and
    line numbers survive), leaving string and template-literal contents intact.

    Lexical, not a TS parser. Single/double quotes open a string only when the
    closing quote sits on the same line (JS strings are single-line), so an
    apostrophe in JSX prose does not desync the scan. Template literals span
    lines; quotes and `//` inside any string/template/comment are inert. Regex
    literals are NOT lexed — the module docstring states this boundary and the
    recall control backstops it.
    """
    out = list(text)
    i, n = 0, len(text)
    state = "code"  # code | line | block | string | template
    string_quote = ""
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                out[i] = out[i + 1] = " "
                state = "line"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                out[i] = out[i + 1] = " "
                state = "block"
                i += 2
                continue
            if ch in "'\"" and _quote_closes_on_line(text, i, ch):
                state, string_quote = "string", ch
            elif ch == "`":
                state = "template"
            i += 1
            continue
        if state == "line":
            if ch == "\n":
                state = "code"
            else:
                out[i] = " "
            i += 1
            continue
        if state == "block":
            if ch == "*" and nxt == "/":
                out[i] = out[i + 1] = " "
                state = "code"
                i += 2
                continue
            if ch != "\n":
                out[i] = " "
            i += 1
            continue
        # string / template states: contents are preserved verbatim.
        if ch == "\\":
            i += 2
            continue
        if (state == "string" and ch == string_quote) or (state == "template" and ch == "`"):
            state = "code"
        i += 1
    return "".join(out)


def _lookup_const(name: str, texts: dict[Path, str], site: str) -> str | None:
    """Resolve `(export) const NAME(: type)? = <string or template>` repo-wide.

    A name defined with CONFLICTING values across console sources fails loud:
    a first-match lookup would silently resolve the claim to whichever file
    sorts first — the wrong path half the time, and never a test failure.
    """
    pattern = re.compile(
        rf"(?:export\s+)?const\s+{re.escape(name)}(?:\s*:\s*[\w.\[\]<>| ]+)?\s*=\s*"
        r"(?P<value>\"[^\"\n]*\"|'[^'\n]*'|`[^`\n]*`)"
    )
    found: dict[str, list[str]] = {}
    for path, text in texts.items():
        for m in pattern.finditer(text):
            found.setdefault(m.group("value"), []).append(str(path))
    if len(found) > 1:
        detail = "; ".join(
            f"{value} in {', '.join(sorted(paths))}" for value, paths in sorted(found.items())
        )
        pytest.fail(
            f"const {name} (needed to resolve the OIDC redirect claim at {site}) has "
            f"conflicting definitions across console sources: {detail} — rename one; "
            "a first-match lookup would silently resolve to the wrong value"
        )
    return next(iter(found)) if found else None


def _lookup_function_return(name: str, text: str) -> str | None:
    """Resolve a same-file `function NAME(): T { return <expr>; }` helper."""
    m = re.search(
        rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*(?::\s*[\w.\[\]<>| ]+)?\s*"
        r"\{\s*return\s+(?P<expr>[^;]+);",
        text,
    )
    return m.group("expr").strip() if m else None


def _resolve_path(expr: str, file_text: str, texts: dict[Path, str], site: str) -> str:
    """Resolve one claim-site value expression to the absolute path it claims.

    Understands the idioms the console codebase uses. Anything else fails LOUD:
    a resolver that silently skipped an unknown idiom would narrow the claimed
    set and let the next undeclared redirect ship green — the exact defect this
    guard exists to prevent.
    """
    expr = expr.strip().rstrip(";,").strip()

    m = _ORIGIN_TEMPLATE.match(expr)
    if m:
        rest = m.group("rest")
        ident = _TEMPLATE_IDENT.match(rest)
        if ident:
            const = _const_or_fail(ident.group("name"), texts, site)
            return _resolve_path(const, file_text, texts, site)
        if rest.startswith("/") and "${" not in rest:
            return rest
        pytest.fail(_unresolvable(site, expr, f"template tail {rest!r}"))

    m = _ORIGIN_CONCAT.match(expr)
    if m:
        return _resolve_path(m.group("rest"), file_text, texts, site)

    m = _STRING_LITERAL.match(expr)
    if m:
        value = m.group("value")
        if value.startswith("/"):
            return value
        pytest.fail(_unresolvable(site, expr, "string literal is not an absolute path"))

    if expr.startswith("`"):
        # A template literal NOT anchored on window.location.origin.
        inner = expr.strip("`")
        if inner.startswith("/") and "${" not in inner:
            return inner
        pytest.fail(_unresolvable(site, expr, "template literal not origin-anchored"))

    m = _CALL.match(expr)
    if m:
        returned = _lookup_function_return(m.group("name"), file_text)
        if returned is None:
            pytest.fail(_unresolvable(site, expr, f"no same-file function {m.group('name')}()"))
        return _resolve_path(returned, file_text, texts, site)

    m = _IDENT.match(expr)
    if m:
        return _resolve_path(_const_or_fail(m.group("name"), texts, site), file_text, texts, site)

    pytest.fail(_unresolvable(site, expr, "unrecognized expression shape"))


def _const_or_fail(name: str, texts: dict[Path, str], site: str) -> str:
    value = _lookup_const(name, texts, site)
    if value is None:
        pytest.fail(_unresolvable(site, name, f"no `const {name} = <string>` found in console src"))
    return value


def _unresolvable(site: str, expr: str, why: str) -> str:
    return (
        f"OIDC redirect claim at {site} cannot be resolved to a path ({why}): {expr!r}. "
        "Do NOT weaken this guard — extend _resolve_path to understand the new idiom, "
        "so the claimed set stays derived from source instead of silently narrowing."
    )


def _extract_claims(texts: dict[Path, str], root: Path) -> list[Claim]:
    """Sweep comment-stripped sources for the four key tokens in any syntactic
    position and attribute each occurrence to a claim — or fail loud naming the
    site. This sweep is what makes "never silently drops a claim" true one
    layer ABOVE the value resolver: the previous `key\\s*:`-only extraction was
    itself an enumeration, and a planted object-shorthand claim shipped
    invisibly through it (vacuous-guards.md §4 row 15)."""
    stripped = {p: _strip_comments(t) for p, t in texts.items()}
    claims: list[Claim] = []
    for path in sorted(stripped):
        text = stripped[path]
        for m in _KEY_TOKEN.finditer(text):
            key = m.group("key")
            line_no = text[: m.start()].count("\n") + 1
            site = f"{path.relative_to(root)}:{line_no}"
            kind = "signin" if key in _SIGNIN_KEYS else "post_logout"
            after = text[m.end() :]
            prop = _PROP_VALUE.match(after) or _ASSIGNED_VALUE.match(after)
            if prop:
                resolved = _resolve_path(prop.group("expr"), text, stripped, site)
                claims.append(Claim(path=resolved, kind=kind, source=site))
                continue
            before = text[: m.start()].rstrip()
            if (
                m.group("q") is None
                and _SHORTHAND_AFTER.match(after)
                and before.endswith(("{", ","))
            ):
                # Object shorthand `{ redirect_uri }`: the claim's value is the
                # in-scope binding of the same name — resolved as an identifier,
                # which fails loud if no such const exists.
                resolved = _resolve_path(key, text, stripped, site)
                claims.append(Claim(path=resolved, kind=kind, source=site))
                continue
            line_text = text.splitlines()[line_no - 1].strip()
            pytest.fail(
                f"OIDC redirect key token `{key}` at {site} sits in a syntactic position "
                f"the claim extractor does not understand (line: {line_text!r}). Do NOT "
                "weaken this guard and do NOT ignore the token — extend _extract_claims/"
                "_resolve_path to attribute it, so the claimed set stays derived from "
                "source instead of silently narrowing."
            )
    return claims


def console_redirect_claims() -> list[Claim]:
    """Every OIDC redirect path the console hands the IdP, derived from source."""
    texts = {p: p.read_text(encoding="utf-8") for p in _console_sources()}
    return _extract_claims(texts, REPO_ROOT)


def _route_is_mounted(app_text: str, path: str) -> bool:
    """Does App.tsx mount a route at `path`? Comment-stripped first: a
    commented-out `<Route path="...">` is a dead route, and a raw substring
    check would have counted it as mounted."""
    return f'path="{path}"' in _strip_comments(app_text)


# --- realm side -----------------------------------------------------------------


def _client(realm_file: Path) -> dict:
    realm = json.loads(realm_file.read_text(encoding="utf-8"))
    clients = [c for c in realm["clients"] if c["clientId"] == "ne-console"]
    assert len(clients) == 1, f"{realm_file}: expected exactly one ne-console client"
    return clients[0]


_URI = re.compile(r"^(?P<origin>[a-z][a-z0-9+.-]*://[^/]*)(?P<path>/.*)?$")


def _split(uri: str) -> tuple[str, str]:
    m = _URI.match(uri)
    assert m, f"undecomposable redirect URI {uri!r}"
    return m.group("origin"), m.group("path") or ""


def _matches(declared: str, candidate: str) -> bool:
    """Keycloak matching: exact, or prefix when the declared value ends in `*`."""
    if declared.endswith("*"):
        return candidate.startswith(declared[:-1])
    return candidate == declared


def _origins(declared: list[str]) -> set[str]:
    return {_split(d)[0] for d in declared}


def _effective_post_logout(client: dict) -> list[str]:
    """Keycloak semantics: an unset attribute (and the literal "+") means the
    post-logout redirect URIs fall back to the client's redirectUris."""
    attr = client.get("attributes", {}).get("post.logout.redirect.uris")
    if attr is None or attr == "+":
        return list(client["redirectUris"])
    return attr.split("##")


# --- extractor self-tests: planted-RED offenders + should-pass controls ----------
# These run the REAL extraction entrypoint over synthetic in-memory trees, so
# every attribution form and every fail-loud path has a pinned probe in both
# directions (vacuous-guard rule: mutate bidirectionally, keep a passing control).


def _claims_of(sources: dict[str, str]) -> list[Claim]:
    root = Path("/console-fixture")
    return _extract_claims({root / name: text for name, text in sources.items()}, root)


def test_extractor_attributes_a_shorthand_claim() -> None:
    """THE review probe that previously shipped invisibly: a real claim built
    as a const + object shorthand in a non-test source file left all guard
    tests green while claiming an undeclared, unmounted redirect path — the
    exact incident class this guard exists to prevent. Both the assignment
    site and the shorthand site must now surface as claims."""
    claims = _claims_of(
        {
            "probe.ts": (
                "const redirect_uri = `${window.location.origin}/auth/probe-callback`;\n"
                "export function probeConfig() {\n"
                "  return { redirect_uri };\n"
                "}\n"
            )
        }
    )
    assert [(c.kind, c.path) for c in claims] == [
        ("signin", "/auth/probe-callback"),
        ("signin", "/auth/probe-callback"),
    ]
    assert [c.source for c in claims] == ["probe.ts:1", "probe.ts:3"]


def test_extractor_attributes_a_quoted_property_claim() -> None:
    """`"redirect_uri": ...` — the closing quote sits between the identifier
    and the colon, so the previous `key\\s*:` extraction silently dropped it."""
    claims = _claims_of(
        {
            "quoted.ts": (
                "const cfg = {\n"
                '  "redirect_uri": `${window.location.origin}/auth/q-callback`,\n'
                "};\n"
            )
        }
    )
    assert [(c.kind, c.path, c.source) for c in claims] == [
        ("signin", "/auth/q-callback", "quoted.ts:2")
    ]


def test_extractor_ignores_comment_mentions() -> None:
    """Comment-awareness control: prose mentions of every key token produce
    neither a claim nor a failure, while a real claim in the same file still
    surfaces — so the sweep cannot be 'fixed' by matching comments too."""
    claims = _claims_of(
        {
            "commented.ts": (
                "// prose: redirect_uri and post_logout_redirect_uri live in the realm\n"
                "/* popup_redirect_uri in a block comment,\n"
                "   popup_post_logout_redirect_uri too */\n"
                "const cfg = {\n"
                "  redirect_uri: `${window.location.origin}/auth/callback`,\n"
                "};\n"
            )
        }
    )
    assert [(c.kind, c.path, c.source) for c in claims] == [
        ("signin", "/auth/callback", "commented.ts:5")
    ]


def test_extractor_fails_loud_on_an_unattributable_token() -> None:
    """A key token in live code that is neither a property, an assignment, nor
    a shorthand must fail naming the site — never be silently dropped."""
    with pytest.raises(pytest.fail.Exception, match=r"weird\.ts:1"):
        _claims_of({"weird.ts": 'url.searchParams.append("redirect_uri", target);\n'})


def test_extractor_fails_loud_on_shorthand_without_a_resolvable_const() -> None:
    """A shorthand whose binding the resolver cannot see (here: destructuring)
    must fail naming the site, not resolve to nothing."""
    with pytest.raises(pytest.fail.Exception, match=r"orphan\.ts:2"):
        _claims_of(
            {
                "orphan.ts": (
                    "export function f(o: Record<string, string>) {\n"
                    "  const { redirect_uri } = o;\n"
                    "  return o;\n"
                    "}\n"
                )
            }
        )


def test_extractor_fails_loud_on_conflicting_const_definitions() -> None:
    """Two same-named consts with different values: a first-match lookup would
    silently resolve the claim to whichever file sorts first."""
    with pytest.raises(pytest.fail.Exception, match="conflicting definitions"):
        _claims_of(
            {
                "a.ts": 'export const CB_PATH = "/auth/a-callback";\n',
                "b.ts": 'export const CB_PATH = "/auth/b-callback";\n',
                "c.ts": (
                    "const cfg = {\n  redirect_uri: `${window.location.origin}${CB_PATH}`,\n};\n"
                ),
            }
        )


def test_route_mount_check_ignores_commented_out_routes() -> None:
    """Bidirectional pin for the mount check: a live route counts, a
    commented-out route does not (a raw substring check counted both)."""
    app = (
        "<Routes>\n"
        '  {/* <Route path="/auth/dead" element={<Dead />} /> */}\n'
        '  <Route path="/auth/live" element={<Live />} />\n'
        "</Routes>\n"
    )
    assert _route_is_mounted(app, "/auth/live")
    assert not _route_is_mounted(app, "/auth/dead")


# --- the guard ------------------------------------------------------------------


def test_console_claims_at_least_one_signin_redirect() -> None:
    """Extraction-liveness control: a console claiming zero sign-in redirects
    cannot authenticate at all, so an empty derivation means the RESOLVER lost
    the source idiom, not that the console stopped doing OIDC."""
    claims = console_redirect_claims()
    assert any(c.kind == "signin" for c in claims), (
        "derived zero sign-in redirect claims from packages/ne-console/src — "
        "the extraction has gone stale; fix _KEY_TOKEN/_extract_claims/_resolve_path"
    )


def test_derivation_recalls_the_known_claim_sites() -> None:
    """Recall control (should-pass): the claims the console is KNOWN to make
    today must all be derived. This is not the guard — the guard stays
    open-ended — it is the control that proves the extractor still sees
    through the real files' idioms, so a comment-scanner desync or an
    over-eager exclusion cannot quietly thin the derivation while the
    set-comparison tests stay green on what remains."""
    got = {(c.kind, c.path) for c in console_redirect_claims()}
    assert {
        ("signin", "/auth/callback"),
        ("signin", "/auth/compare-callback"),
        ("post_logout", "/"),
    } <= got, f"known console claims missing from the derivation: {got}"


def test_every_claimed_redirect_path_is_a_mounted_route() -> None:
    """An IdP-accepted redirect that 404s in the console is the same dead popup
    with a different error page. Every claimed sign-in path must be a real
    route (the popup/redirect must land on handler code, outside auth)."""
    app = APP_TSX.read_text(encoding="utf-8")
    for claim in console_redirect_claims():
        if claim.kind != "signin":
            continue
        assert _route_is_mounted(app, claim.path), (
            f"console claims OIDC redirect path {claim.path!r} ({claim.source}) "
            f"but App.tsx mounts no live route for it"
        )


@pytest.mark.parametrize("realm_file", REALM_FILES, ids=lambda p: p.stem)
def test_realm_declares_every_console_claimed_redirect(realm_file: Path) -> None:
    """FORWARD direction — the incident: every sign-in redirect path the console
    claims must be declared for every origin the realm serves. Keycloak rejects
    an undeclared redirect_uri outright, so a missing declaration is not drift,
    it is the feature dead in every environment that imports this realm."""
    client = _client(realm_file)
    declared = client["redirectUris"]
    claimed = [c for c in console_redirect_claims() if c.kind == "signin"]
    for origin in sorted(_origins(declared)):
        for claim in claimed:
            candidate = origin + claim.path
            assert any(_matches(d, candidate) for d in declared), (
                f"{realm_file.name}: console claims OIDC redirect path {claim.path!r} "
                f"({claim.source}) but client ne-console does not declare it for origin "
                f"{origin!r} — add {candidate!r} to redirectUris or the IdP rejects the "
                f"redirect and the flow dies on Keycloak's error page"
            )


@pytest.mark.parametrize("realm_file", REALM_FILES, ids=lambda p: p.stem)
def test_realm_declares_no_unclaimed_redirect(realm_file: Path) -> None:
    """REVERSE direction — the bidirectional half: a redirect URI no console
    code claims is pure OAuth attack surface (a code-interception target that
    exists only in the IdP's trust store). Wildcard entries are exempt from
    this path-level direction only because they are pinned outright by
    test_wildcard_redirect_declarations_only_on_the_dev_loopback — the sole
    survivor of that pin is the dev loopback port wildcard, and the forward
    test still binds it."""
    client = _client(realm_file)
    claimed_paths = {c.path for c in console_redirect_claims() if c.kind == "signin"}
    for declared in client["redirectUris"]:
        if declared.endswith("*"):
            continue
        _, path = _split(declared)
        assert path in claimed_paths, (
            f"{realm_file.name}: client ne-console declares redirect URI {declared!r} "
            f"but no console source claims path {path!r} — remove the declaration or "
            f"add the console flow that uses it"
        )


@pytest.mark.parametrize("realm_file", REALM_FILES, ids=lambda p: p.stem)
def test_wildcard_redirect_declarations_only_on_the_dev_loopback(realm_file: Path) -> None:
    """A `*` in a redirect declaration authorizes EVERY matching URI as a
    code/token-interception target: combined with any open redirect or XSS on
    the host, the IdP will deliver an authorization code wherever the attacker
    parks it. A same-host wildcard (`https://${NE_PILOT_HOSTNAME}/*`) is NOT a
    lesser version of a foreign one — on the deploy hostname it is the whole
    class. The single sanctioned wildcard in this repo is the dev realm's
    `http://localhost:*` (a PORT wildcard on the loopback for vite's shifting
    dev ports); every other wildcard, in redirectUris or in
    post.logout.redirect.uris, fails here regardless of host."""
    client = _client(realm_file)
    declared = list(client["redirectUris"])
    attr = client.get("attributes", {}).get("post.logout.redirect.uris")
    if attr and attr != "+":
        declared += attr.split("##")
    for uri in declared:
        if "*" not in uri:
            continue
        assert uri == _SANCTIONED_WILDCARD, (
            f"{realm_file.name}: wildcard redirect declaration {uri!r} — wildcards are "
            f"never a production convenience; declare exact paths (the only sanctioned "
            f"wildcard is the dev loopback {_SANCTIONED_WILDCARD!r})"
        )


@pytest.mark.parametrize("realm_file", REALM_FILES, ids=lambda p: p.stem)
def test_realm_accepts_every_console_post_logout_target(realm_file: Path) -> None:
    """Sign-out half of the incident: the console's post_logout_redirect_uri
    must be accepted for every origin, or sign-out dies on a Keycloak
    'Invalid redirect uri' page instead of returning to the sign-in form."""
    client = _client(realm_file)
    effective = _effective_post_logout(client)
    claimed = [c for c in console_redirect_claims() if c.kind == "post_logout"]
    assert claimed, (
        "console claims no post_logout_redirect_uri at all — sign-out would strand "
        "the user on Keycloak's logged-out page (the owner-reported defect); "
        "buildOidcConfig must claim one, and this derivation must find it"
    )
    for origin in sorted(_origins(client["redirectUris"])):
        for claim in claimed:
            candidate = origin + claim.path
            assert any(_matches(d, candidate) for d in effective), (
                f"{realm_file.name}: console claims post-logout path {claim.path!r} "
                f"({claim.source}) but client ne-console does not accept it for origin "
                f"{origin!r} — declare {candidate!r} in the client's "
                f"post.logout.redirect.uris attribute (## separated)"
            )


def test_pilot_post_logout_targets_are_all_claimed() -> None:
    """Reverse direction for the pilot's explicit post-logout list: every
    declared post-logout target must be one the console actually claims."""
    pilot = REALM_FILES[0]
    client = _client(pilot)
    attr = client.get("attributes", {}).get("post.logout.redirect.uris")
    assert attr and attr != "+", (
        f"{pilot.name}: post.logout.redirect.uris must be declared explicitly — "
        "unset falls back to redirectUris, which routes sign-out onto the OIDC "
        "callback (the second owner-reported symptom) instead of the console root"
    )
    claimed_paths = {c.path for c in console_redirect_claims() if c.kind == "post_logout"}
    for declared in attr.split("##"):
        _, path = _split(declared)
        assert path in claimed_paths, (
            f"{pilot.name}: post.logout.redirect.uris accepts {declared!r} but no "
            f"console source claims post-logout path {path!r}"
        )
