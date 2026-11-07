# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C12-safe failure projection — a failure that is recorded must state WHAT failed.

Two callers share this module, which is why it lives in ``ne-contracts`` (the one
package both depend on) rather than in either of them: the connector sync
(``ne.connect.bridge`` / ``ne connect sync``) and the governed tool executor
(``ne.runtime.middleware``, whose ``tool.execute`` audit row carried an exception
CLASS and nothing else — a ``LookupError`` there names none of the dozen distinct
call sites that raise one).

``ne connect sync`` used to print ``{source}: ERROR {type(exc).__name__}`` and the
``connector.sync`` audit detail carried ``error_class=`` alone. That is not a
diagnosis: ``OperationalError`` covers connection-refused, too-many-clients,
server-closed-connection, deadlock, statement/lock timeout, sqlite
readonly/locked and a dozen more, and the class alone cannot tell them apart.
Worse, the class that reaches the CLI is often a RECLASSIFICATION — SQLAlchemy
re-raises the DBAPI fault as ``sqlalchemy.exc.<Class>`` and the driver itself
maps SQLSTATE classes to its own hierarchy (a duplicate key is ``23505`` ⇒
``IntegrityError``, a deadlock is ``40P01`` ⇒ ``DeadlockDetected``, which
subclasses ``OperationalError``; sqlite reports "already exists" / "readonly" /
"locked" as ``OperationalError`` while only a constraint violation is
``IntegrityError``). The projection below walks that chain so the reclassification
is VISIBLE instead of being the thing that hides the cause.

What may be emitted, and why it is safe
---------------------------------------
The C12 rule this package lives by is that PROVIDER MESSAGES never enter the
ledger: they can embed credentials, source rows, or a credential-bearing URL.
The projection therefore separates two audiences:

* :func:`failure_code` / :func:`failure_origin` — CODES and CLASS NAMES only
  (a five-character SQLSTATE, a ``SQLITE_*`` error name, a dotted class name).
  Every one is a closed, code-authored identifier with no payload in it. These
  are what the AUDIT LEDGER gets, so the ``*/15`` cron — whose stderr nobody
  ever reads — records a cause instead of a shrug.
* :func:`failure_site` / :func:`failure_fields` — the RAISE SITE
  (``module:function:lineno``, read off the exception's own traceback) and the
  ledger-safe field mapping built from it. A source location is code the repo
  wrote: it is the one identifier that separates two raises of the same class
  with the same message, and it cannot carry a value.
* :func:`describe_failure` — the operator's terminal line. It adds the DDL/SQL
  identifiers from the driver diagnostic (constraint / table / schema / column
  names — schema identifiers, not rows) and a bounded, redacted message.

A MESSAGE IS NOT LEDGER-SAFE and no function here puts one in the ledger.
Exception messages in this repo interpolate arguments (``f"trace not found:
{trace_id}"`` is a real one, in the governed ``read.trace`` tool), and a tool's
arguments may be protected content. The message is therefore reachable only
through :func:`describe_failure`, whose audience is the operator's log.

The message is taken from ``diag.message_primary`` for psycopg errors, NEVER
``str(exc)``: libpq's rendered error appends ``DETAIL:``, which for a unique
violation is ``Key (a, b)=(...) already exists`` — i.e. actual row values.
For the same reason a SQLAlchemy ``StatementError`` is never stringified either
(its ``str()`` appends ``[parameters: ...]``); its ``.orig`` is projected and its
``.statement`` — parameterized SQL text, code rather than data — is carried
separately. Everything that is stringified goes through :func:`redact_secrets`
and is truncated.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

__all__ = [
    "describe_failure",
    "failure_code",
    "failure_detail",
    "failure_fields",
    "failure_origin",
    "failure_site",
    "redact_secrets",
]

#: Longest message/statement fragment carried into a terminal line.
_MAX_MESSAGE = 400
_MAX_STATEMENT = 300

#: Deepest exception chain walked. A cycle is impossible (``__cause__`` chains
#: are acyclic in practice) but a bound keeps one pathological wrapper from
#: producing an unbounded line.
_MAX_DEPTH = 8

#: Keys whose VALUE is credential material wherever it appears as ``k=v`` /
#: ``k: v`` — driver messages routinely echo the connection string back.
_SECRET_KEYS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "authorization",
    "auth",
    "credential",
    "credentials",
)

#: ``scheme://user:password@host`` — the credential-bearing DSN/URL shape. The
#: userinfo is replaced wholesale: a username is an identity, not a diagnosis.
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/\s@]+@")

#: The value of a secret key. The auth-scheme alternative comes FIRST because
#: ``authorization=Bearer <token>`` puts the credential AFTER a space: matching
#: the bare token shape would redact the word ``Bearer`` and print the token.
_SECRET_VALUE = r"(?:(?:Bearer|Basic|Digest|Token|JWT)\s+[^\s,;)]+|\"[^\"]*\"|'[^']*'|[^\s,;)]+)"

_KV_SECRET = re.compile(r"(?i)\b(" + "|".join(_SECRET_KEYS) + r")(\s*[=:]\s*)" + _SECRET_VALUE)


def redact_secrets(text: str) -> str:
    """Strip credential material out of a driver/provider message.

    Two shapes, both fail-closed by REPLACEMENT (never by "looks fine, keep it"):
    the userinfo of any ``scheme://user:pass@host`` URL, and the value of any
    ``password=``/``token=``/``secret=``-style key. Whitespace is collapsed so a
    multi-line libpq message stays one greppable line.
    """
    redacted = _URL_CREDENTIALS.sub(r"\g<scheme>[redacted]@", text)
    redacted = _KV_SECRET.sub(r"\1\2[redacted]", redacted)
    return " ".join(redacted.split())


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield ``exc`` and the causes beneath it, outermost first.

    Prefers SQLAlchemy's ``.orig`` (the real DBAPI exception) over ``__cause__``
    so the DRIVER error — the only object carrying the SQLSTATE and the
    diagnostic — is reached even when a wrapper re-raises without ``from``.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_DEPTH):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        yield current
        nxt = getattr(current, "orig", None)
        if not isinstance(nxt, BaseException):
            nxt = current.__cause__ or current.__context__
        current = nxt


def _qualname(exc: BaseException) -> str:
    cls = type(exc)
    module = getattr(cls, "__module__", "")
    if not module or module == "builtins":
        return cls.__qualname__
    return f"{module}.{cls.__qualname__}"


#: A Postgres SQLSTATE is exactly five alphanumerics; a sqlite3 error name is
#: ``SQLITE_`` plus upper-case word characters. Both are ENFORCED rather than
#: assumed — see :func:`failure_code`.
_SQLSTATE_RE = re.compile(r"\A[0-9A-Za-z]{5}\Z")
_SQLITE_ERRORNAME_RE = re.compile(r"\ASQLITE_[A-Z0-9_]{1,40}\Z")


def failure_code(exc: BaseException) -> str | None:
    """The driver's own error CODE for ``exc``, or ``None`` if it has none.

    A five-character SQLSTATE for Postgres (``40P01`` deadlock, ``55P03`` lock
    timeout, ``57014`` statement timeout, ``23505`` unique violation, ``53300``
    too many clients — the distinctions ``OperationalError`` erases), or
    sqlite3's ``SQLITE_*`` error name (``SQLITE_READONLY``, ``SQLITE_BUSY``,
    ``SQLITE_ERROR``).

    SHAPE-ENFORCED, not shape-assumed (retrospective review of the 2026-08-27
    lineage, finding 1). This module's whole discipline is that provider text
    never enters the ledger, and it holds that line for MESSAGES —
    :func:`failure_fields` deliberately has no message key. But these codes are
    read off the DRIVER, which reads them off the SERVER: that is a trust
    boundary, and "closed identifier" was an assumption with only ``isinstance``
    and non-empty behind it. ``failure_fields`` puts the result straight into the
    ``tool.execute`` audit detail (``ne.runtime.middleware``), so a hostile or
    simply buggy server could have written arbitrary-length text into a
    governance row.

    Anything not matching the documented shape is dropped to ``None`` — the same
    conservative choice the module already makes for a fault with no code at all.
    A real code is never lost by this: both patterns admit every value either
    driver actually produces.
    """
    for link in _chain(exc):
        for attr in ("sqlstate", "pgcode"):
            value = getattr(link, attr, None)
            if isinstance(value, str) and _SQLSTATE_RE.match(value.strip()):
                return value.strip()
        name = getattr(link, "sqlite_errorname", None)
        if isinstance(name, str) and _SQLITE_ERRORNAME_RE.match(name.strip()):
            return name.strip()
    return None


def failure_origin(exc: BaseException) -> str | None:
    """The dotted class of the DEEPEST cause, when it is not ``exc``'s own class.

    This is the reclassification made legible: ``OperationalError`` raised by
    SQLAlchemy over ``psycopg.errors.DeadlockDetected`` says something the outer
    class cannot. ``None`` when nothing is wrapped, so an unwrapped failure's
    detail line stays exactly as it was.
    """
    deepest = exc
    for link in _chain(exc):
        deepest = link
    if deepest is exc:
        return None
    return _qualname(deepest)


def failure_detail(mode: str, exc: BaseException) -> str:
    """The ``connector.sync`` audit detail: mode + class, plus code/origin when known.

    CODES AND CLASS NAMES ONLY — this string goes into the durable ledger, which
    is the ONLY record of a scheduled sync's failure. ``error_code`` and
    ``error_origin`` are appended together and only for a driver-shaped fault, so
    a plain application error's detail is byte-identical to what it was before.
    """
    detail = f"mode={mode} error_class={type(exc).__name__}"
    code = failure_code(exc)
    if code is None:
        return detail
    detail += f" error_code={code}"
    origin = failure_origin(exc)
    if origin is not None:
        detail += f" error_origin={origin}"
    return detail


#: Longest ``module:function:lineno`` site string carried. A dotted module path
#: plus a qualname is code-authored and short; the bound is a belt-and-braces
#: guard against a pathological generated module name, never a redaction.
_MAX_SITE = 200


def _site_of(exc: BaseException) -> str | None:
    """``module:function:lineno`` for the frame where ``exc`` was raised.

    The LAST frame of the traceback is the raise site; the earlier frames are the
    callers that propagated it. Only ``co_qualname``/``__name__``/``tb_lineno``
    are read — the frame's LOCALS (which hold argument values) are never touched,
    which is what makes a site safe for the ledger where a message is not.
    """
    tb = exc.__traceback__
    if tb is None:
        return None
    while tb.tb_next is not None:
        tb = tb.tb_next
    code = tb.tb_frame.f_code
    module = tb.tb_frame.f_globals.get("__name__")
    qualname = getattr(code, "co_qualname", code.co_name)
    prefix = f"{module}:" if isinstance(module, str) and module else ""
    return f"{prefix}{qualname}:{tb.tb_lineno}"[:_MAX_SITE]


def failure_site(exc: BaseException) -> str | None:
    """Where ``exc`` was raised, or ``None`` for an exception with no traceback.

    This is the field the ``tool.execute`` audit row was missing. ``LookupError``
    alone does not distinguish ``ne.api.tools:_search`` raising "no evidence
    context" from the same module raising "no source stamps" thirty lines later,
    let alone from the twelve identical ``LookupError("Run claim evidence is
    unavailable")`` raises inside ``ne.runtime.run_store``. A line number does.
    """
    return _site_of(exc)


def failure_fields(exc: BaseException) -> dict[str, str]:
    """The LEDGER-SAFE field mapping for a failed operation. Names, codes, sites.

    ``error_class`` is always first and is exactly what the executor recorded
    before this module existed, so an existing row's one field keeps its exact
    meaning; every other key is ADDITIVE and is omitted when it cannot be derived
    (an exception with no traceback, a fault with no driver code, an unwrapped
    exception). There is deliberately no message key: see the module docstring.
    """
    fields: dict[str, str] = {"error_class": type(exc).__name__}
    site = failure_site(exc)
    if site is not None:
        fields["error_site"] = site
    code = failure_code(exc)
    if code is not None:
        fields["error_code"] = code
    origin = failure_origin(exc)
    if origin is not None:
        fields["error_origin"] = origin
        # The wrapped cause's own raise site — the site inside the driver/library
        # that actually failed, as opposed to the site that re-raised. Omitted
        # when it is the same frame, so the row never repeats itself.
        deepest = exc
        for link in _chain(exc):
            deepest = link
        origin_site = _site_of(deepest)
        if origin_site is not None and origin_site != site:
            fields["error_origin_site"] = origin_site
    return fields


def _diagnostic_identifiers(exc: BaseException) -> str:
    """Schema identifiers from a psycopg ``Diagnostic`` — names, never rows.

    ``constraint_name`` is the single most useful field a duplicate-key failure
    carries: it names the relation whose rule was broken. ``message_detail`` and
    ``message_hint`` are deliberately NOT read — those embed the offending key's
    VALUES.
    """
    parts: list[str] = []
    for link in _chain(exc):
        diag = getattr(link, "diag", None)
        if diag is None:
            continue
        for label, attr in (
            ("schema", "schema_name"),
            ("relation", "table_name"),
            ("column", "column_name"),
            ("constraint", "constraint_name"),
        ):
            value = getattr(diag, attr, None)
            if isinstance(value, str) and value.strip():
                parts.append(f"{label}={value.strip()}")
        if parts:
            break
    return " ".join(parts)


def _message(exc: BaseException) -> str:
    """The safest non-empty message in the chain, redacted and bounded.

    psycopg's ``diag.message_primary`` wins wherever it exists — it is the bare
    server message without libpq's ``DETAIL:`` row values. A SQLAlchemy
    ``StatementError`` is skipped entirely (its ``str()`` appends the bound
    ``[parameters: ...]``); its ``.orig`` is the next link and speaks for it.
    """
    for link in _chain(exc):
        diag = getattr(link, "diag", None)
        primary = getattr(diag, "message_primary", None) if diag is not None else None
        if isinstance(primary, str) and primary.strip():
            return redact_secrets(primary)[:_MAX_MESSAGE]
        if getattr(link, "statement", None) is not None and getattr(link, "orig", None) is not None:
            # SQLAlchemy StatementError — its str() carries bound parameters.
            continue
        text = str(link)
        if text.strip():
            return redact_secrets(text)[:_MAX_MESSAGE]
    return ""


def describe_failure(exc: BaseException) -> tuple[str, ...]:
    """Operator-facing lines naming the cause of ``exc``. Never empty.

    Line 1 is the class, the redacted message, and — when the fault came from a
    database driver — its code, the schema identifiers it named, and the wrapped
    origin class. Line 2, when present, is the failing SQL: parameterized
    statement TEXT only, which is code the repo wrote, with the bound parameters
    (which are source data) left out.
    """
    head = type(exc).__name__
    message = _message(exc)
    if message:
        head += f": {message}"
    annotations: list[str] = []
    code = failure_code(exc)
    if code is not None:
        annotations.append(f"code={code}")
    identifiers = _diagnostic_identifiers(exc)
    if identifiers:
        annotations.append(identifiers)
    origin = failure_origin(exc)
    if origin is not None:
        annotations.append(f"via={origin}")
    if annotations:
        head += " [" + " ".join(annotations) + "]"
    lines = [head]
    for link in _chain(exc):
        statement = getattr(link, "statement", None)
        if isinstance(statement, str) and statement.strip():
            lines.append("statement: " + redact_secrets(statement)[:_MAX_STATEMENT])
            break
    return tuple(lines)
