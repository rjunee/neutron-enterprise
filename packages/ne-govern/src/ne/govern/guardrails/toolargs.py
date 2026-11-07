# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Injection-resistant tool-argument validation (C12) — reject-by-default.

Deterministic validation of the arguments a (possibly hijacked) agent asks a tool
to run with. Everything is reject-by-default: an unknown tool, a non-mapping args
payload, an unexpected argument, a missing required argument, an oversized/binary
payload, or a value that violates its declared ``kind`` is BLOCKED. This is the
code-enforced barrier
that stops a prompt-injected model from smuggling a path traversal, a shell/SQL
injection, or a binary blob through a tool call.

The dangerous-pattern scan (shell metacharacters, SQL-injection markers) runs on
the structured ``identifier``/``path``/``email``/``url`` kinds — kinds whose
legitimate values never contain those characters. Those same structured kinds
also reject ALL control characters (every C0 control incl. CR/LF/TAB, plus DEL):
their legitimate values never carry them, and CR/LF are the header/log-injection
payload — so a CRLF smuggled through a structured arg is BLOCKED here. ``text``
is free-form and is checked ONLY for binary/oversize/NUL content (a documented
decision: legitimate prose contains punctuation AND legitimate whitespace, so it
DELIBERATELY keeps ``\t``/``\n``/``\r`` and pattern-scanning it would be all
false positives — free text that reaches a shell/SQL sink is the sink's
parameterization bug, not this stage's; structured args are where injection is
deterministically decidable).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from ne.contracts.governance import GuardrailVerdict
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.govern.emails import is_valid_email
from ne.govern.guardrails._common import failed, get_tool, passed

_STRING_KINDS = frozenset(
    {
        ToolArgKind.text,
        ToolArgKind.identifier,
        ToolArgKind.path,
        ToolArgKind.email,
        ToolArgKind.url,
    }
)
_SCANNED_KINDS = frozenset(
    {ToolArgKind.identifier, ToolArgKind.path, ToolArgKind.email, ToolArgKind.url}
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,256}\Z")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
# Shell metacharacters + SQL-injection markers. Structured kinds never legitimately
# carry these, so any occurrence is a rejected injection attempt.
_SHELL_METACHARS = set(";|&`$(){}<>")
_SQL_MARKERS = ("'", "--", "/*", "*/", "union select")


class ToolArgGuardrail:
    """Validates tool-call arguments against a per-tool allowlisted policy."""

    def __init__(self, tools: Mapping[str, ToolArgPolicy]) -> None:
        self._tools = dict(tools)

    def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict:
        tool = get_tool(payload)
        if tool is None or tool not in self._tools:
            return failed("toolargs.unknown-tool", "tool not in the arg-policy allowlist")
        policy = self._tools[tool]

        raw_args = payload.get("args")
        args: Mapping[str, Any]
        if raw_args is None:
            args = {}
        elif isinstance(raw_args, Mapping):
            args = raw_args
        else:
            return failed(
                "toolargs.malformed-args",
                "args is present but not a mapping (reject-by-default)",
            )

        for key in args:
            if key not in policy.args:
                return failed("toolargs.unknown-arg", f"unexpected argument {key!r}")
        for key, spec in policy.args.items():
            if spec.required and key not in args:
                return failed("toolargs.missing-arg", f"required argument {key!r} missing")

        try:
            serialized = json.dumps(args)
        except (TypeError, ValueError):
            return failed("toolargs.unserializable", "argument value is not JSON-native")
        if len(serialized.encode("utf-8")) > policy.max_total_bytes:
            return failed("toolargs.oversized", "serialized args exceed max_total_bytes")

        for key, value in args.items():
            verdict = self._check_arg(key, value, policy.args[key])
            if not verdict.passed:
                return verdict
        return passed("toolargs.valid")

    def _check_arg(self, key: str, value: Any, spec: ArgSpec) -> GuardrailVerdict:
        # NE-KG-MULTIKEY: a spec declaring ``list_max`` accepts EITHER a scalar (the
        # unchanged path below) or a bounded array. An array is validated MEMBER BY
        # MEMBER through this very method, so every member faces the identical
        # kind/length/binary/injection gate a scalar faces — there is one validation
        # body, so the two strictnesses cannot drift. A single failing member fails the
        # WHOLE call (the loop returns on the first failure); no member is ever dropped.
        # A spec WITHOUT ``list_max`` never reaches this branch, so a list value keeps
        # failing ``toolargs.wrong-type`` exactly as before.
        if spec.list_max is not None and isinstance(value, list):
            if not value:
                return failed("toolargs.empty-list", f"argument {key!r} is an empty list")
            if len(value) > spec.list_max:
                return failed(
                    "toolargs.oversized-list",
                    f"argument {key!r} exceeds its declared list cardinality cap",
                )
            for member in value:
                # SCALAR gate only — deliberately NOT a recursive ``_check_arg``. A
                # nested list would otherwise pass its own cardinality cap while
                # smuggling an arbitrary number of leaves under a single "member",
                # which is exactly the unbounded shape the cap exists to forbid. A
                # list member is therefore checked as a value, and a nested list
                # fails the kind's type gate.
                verdict = self._check_scalar_arg(key, member, spec)
                if not verdict.passed:
                    return verdict
            return passed("toolargs.list-ok")

        return self._check_scalar_arg(key, value, spec)

    def _check_scalar_arg(self, key: str, value: Any, spec: ArgSpec) -> GuardrailVerdict:
        """The unchanged single-value gate — kind, length, binary, injection scan."""
        if spec.kind in _STRING_KINDS:
            if not isinstance(value, str):
                return failed("toolargs.wrong-type", f"argument {key!r} must be a string")
            if len(value) > spec.max_length:
                return failed("toolargs.oversized-arg", f"argument {key!r} exceeds max_length")
            if _is_binary(value):
                return failed("toolargs.binary", f"argument {key!r} contains binary/control bytes")

        if spec.kind in _SCANNED_KINDS and isinstance(value, str) and _has_control_chars(value):
            return failed("toolargs.control-chars", f"argument {key!r} contains control characters")

        if spec.kind in _SCANNED_KINDS and isinstance(value, str) and _has_dangerous_pattern(value):
            return failed(
                "toolargs.dangerous-pattern", f"argument {key!r} matched an injection pattern"
            )

        return _check_kind(key, value, spec.kind)


def _has_control_chars(value: str) -> bool:
    """True if the string carries ANY C0 control char (incl. ``\\t\\n\\r``) or DEL.

    Applied to the structured ``identifier``/``path``/``email``/``url`` kinds,
    whose legitimate values never carry control characters; this catches the
    CR/LF header/log-injection payload that ``_is_binary`` deliberately permits
    for the free-text kind, plus DEL (0x7F), which ``_is_binary`` also misses.
    """
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _is_binary(value: str) -> bool:
    """True if the string carries NUL/control bytes or un-encodable surrogates."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return any(ord(ch) < 0x20 and ch not in "\t\n\r" for ch in value)


def _has_dangerous_pattern(value: str) -> bool:
    if any(ch in _SHELL_METACHARS for ch in value):
        return True
    lowered = value.lower()
    return any(marker in lowered for marker in _SQL_MARKERS)


def _check_kind(key: str, value: Any, kind: ToolArgKind) -> GuardrailVerdict:
    if kind is ToolArgKind.text:
        return passed("toolargs.text-ok")
    if kind is ToolArgKind.identifier:
        if not _IDENTIFIER_RE.match(str(value)):
            return failed("toolargs.invalid-identifier", f"argument {key!r} is not an identifier")
        return passed("toolargs.identifier-ok")
    if kind is ToolArgKind.path:
        problem = _path_problem(str(value))
        if problem is not None:
            return failed("toolargs.path-traversal", f"argument {key!r}: {problem}")
        return passed("toolargs.path-ok")
    if kind is ToolArgKind.email:
        if not is_valid_email(str(value)):
            return failed("toolargs.invalid-email", f"argument {key!r} is not a bare email")
        return passed("toolargs.email-ok")
    if kind is ToolArgKind.url:
        problem = _url_problem(str(value))
        if problem is not None:
            return failed("toolargs.invalid-url", f"argument {key!r}: {problem}")
        return passed("toolargs.url-ok")
    if kind is ToolArgKind.number:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return failed("toolargs.not-number", f"argument {key!r} is not a number")
        return passed("toolargs.number-ok")
    if kind is ToolArgKind.boolean:
        if not isinstance(value, bool):
            return failed("toolargs.not-boolean", f"argument {key!r} is not a boolean")
        return passed("toolargs.boolean-ok")
    return failed(
        "toolargs.unknown-kind", f"argument {key!r} has an unknown kind"
    )  # pragma: no cover


def _path_problem(value: str) -> str | None:
    if value.startswith("/"):
        return "absolute path"
    if _DRIVE_RE.match(value):
        return "drive-letter path"
    if "\\" in value:
        return "backslash in path"
    if "\x00" in value:
        return "NUL in path"
    if any(segment == ".." for segment in value.split("/")):
        return "parent-directory segment"
    return None


def _url_problem(value: str) -> str | None:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(value)
    except ValueError:
        return "unparseable URL"
    if parts.scheme not in ("http", "https"):
        return "scheme must be http/https"
    if parts.username is not None or parts.password is not None:
        return "userinfo forbidden"
    if not parts.hostname:
        return "missing host"
    return None
