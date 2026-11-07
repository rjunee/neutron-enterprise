# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ToolArgGuardrail (C12): injection-resistant, reject-by-default arg validation."""

from __future__ import annotations

import pytest

from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.govern.guardrails import ToolArgGuardrail, build_tool_call_payload

from .conftest import make_principal

_P = make_principal()


def _g(tools):
    return ToolArgGuardrail(tools)


def _payload(tool, args):
    return build_tool_call_payload(principal=_P, tool=tool, args=args)


def test_unknown_tool_denied() -> None:
    verdict = _g({}).check(_payload("mystery", {}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.unknown-tool"


def test_unknown_arg_denied() -> None:
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec()})})
    verdict = g.check(_payload("t", {"b": "x"}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.unknown-arg"


def test_required_missing_denied() -> None:
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec(required=True)})})
    verdict = g.check(_payload("t", {}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.missing-arg"


def test_path_traversal_denied() -> None:
    g = _g({"read": ToolArgPolicy(args={"p": ArgSpec(kind=ToolArgKind.path)})})
    verdict = g.check(_payload("read", {"p": "../../etc/passwd"}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.path-traversal"


def test_absolute_path_denied() -> None:
    g = _g({"read": ToolArgPolicy(args={"p": ArgSpec(kind=ToolArgKind.path)})})
    verdict = g.check(_payload("read", {"p": "/etc/passwd"}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.path-traversal"


def test_relative_path_allowed() -> None:
    g = _g({"read": ToolArgPolicy(args={"p": ArgSpec(kind=ToolArgKind.path)})})
    assert g.check(_payload("read", {"p": "docs/readme.md"})).passed


def test_sql_injection_identifier_denied() -> None:
    g = _g({"q": ToolArgPolicy(args={"id": ArgSpec(kind=ToolArgKind.identifier)})})
    verdict = g.check(_payload("q", {"id": "1; DROP TABLE users"}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.dangerous-pattern"


def test_command_injection_identifier_denied() -> None:
    g = _g({"q": ToolArgPolicy(args={"id": ArgSpec(kind=ToolArgKind.identifier)})})
    for payload_val in ("`whoami`", "$(rm -rf /)", "a|b"):
        verdict = g.check(_payload("q", {"id": payload_val}))
        assert not verdict.passed
        assert verdict.rule == "toolargs.dangerous-pattern"


def test_oversized_total_denied() -> None:
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec(max_length=100_000)}, max_total_bytes=10)})
    verdict = g.check(_payload("t", {"a": "x" * 50}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.oversized"


def test_oversized_per_arg_denied() -> None:
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec(max_length=5)})})
    verdict = g.check(_payload("t", {"a": "x" * 6}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.oversized-arg"


def test_binary_control_char_denied() -> None:
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec()})})
    verdict = g.check(_payload("t", {"a": "hello\x00world"}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.binary"


def test_non_json_serializable_denied() -> None:
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec()})})
    verdict = g.check(_payload("t", {"a": object()}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.unserializable"


def test_text_kind_tolerates_ordinary_punctuation() -> None:
    # Documented decision: free text legitimately contains punctuation, so the
    # dangerous-pattern scan does NOT run on text kind.
    g = _g({"note": ToolArgPolicy(args={"body": ArgSpec(kind=ToolArgKind.text)})})
    assert g.check(_payload("note", {"body": "Hi Bob; see the (attached) file & reply!"})).passed


def test_valid_policy_conformant_call_passes() -> None:
    g = _g(
        {
            "send": ToolArgPolicy(
                args={
                    "to": ArgSpec(kind=ToolArgKind.email, required=True),
                    "count": ArgSpec(kind=ToolArgKind.number),
                    "urgent": ArgSpec(kind=ToolArgKind.boolean),
                }
            )
        }
    )
    verdict = g.check(_payload("send", {"to": "a@b.com", "count": 3, "urgent": True}))
    assert verdict.passed


def test_number_kind_rejects_bool_and_string() -> None:
    g = _g({"t": ToolArgPolicy(args={"n": ArgSpec(kind=ToolArgKind.number)})})
    assert not g.check(_payload("t", {"n": True})).passed
    assert not g.check(_payload("t", {"n": "3"})).passed
    assert g.check(_payload("t", {"n": 3})).passed
    assert g.check(_payload("t", {"n": 3.5})).passed


# --- fail-closed: present-but-malformed args shape (Codex P2 continuation) ---


@pytest.mark.parametrize("malformed", [["x"], "x", 7, True, b"x", {"x"}])
def test_non_mapping_args_blocked_fail_closed(malformed) -> None:
    # A tool with NO required args is the exact pre-fix bypass: a non-mapping args
    # payload was coerced to {} and sailed through. It must now BLOCK.
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec()})})
    p = _payload("t", {})
    p["args"] = malformed
    verdict = g.check(p)
    assert not verdict.passed
    assert verdict.rule == "toolargs.malformed-args"


def test_malformed_args_detail_never_echoes_value() -> None:
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec()})})
    p = _payload("t", {})
    p["args"] = ["SENTINEL-DO-NOT-LEAK"]
    verdict = g.check(p)
    assert not verdict.passed
    assert "SENTINEL-DO-NOT-LEAK" not in verdict.detail


def test_absent_or_none_args_still_empty() -> None:
    # Absent or explicitly-None args stay benign ({}) for a no-required-arg tool.
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec()})})
    p = _payload("t", {})
    del p["args"]
    assert g.check(p).passed
    p2 = _payload("t", {})
    p2["args"] = None
    assert g.check(p2).passed

    # A required-arg tool with args absent still fails with missing-arg, NOT
    # malformed-args (no behavior regression).
    g_req = _g({"t": ToolArgPolicy(args={"a": ArgSpec(required=True)})})
    p3 = _payload("t", {})
    del p3["args"]
    verdict = g_req.check(p3)
    assert not verdict.passed
    assert verdict.rule == "toolargs.missing-arg"


# --- CRLF / control-char injection through structured kinds (NE-CKC4) ---
# Structured kinds (identifier/path/email/url) reject ALL control characters incl.
# CR/LF/TAB (the header/log-injection payload) and DEL; the _IDENTIFIER_RE /
# _EMAIL_RE are \Z-anchored so a trailing newline no longer slips past.


def test_identifier_trailing_newline_denied() -> None:
    # The $-anchor hole: '$' matched BEFORE a trailing '\n'; '\Z' does not, and the
    # control-char check catches it first regardless.
    g = _g({"q": ToolArgPolicy(args={"id": ArgSpec(kind=ToolArgKind.identifier)})})
    verdict = g.check(_payload("q", {"id": "db1\n"}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.control-chars"


def test_identifier_crlf_denied() -> None:
    g = _g({"q": ToolArgPolicy(args={"id": ArgSpec(kind=ToolArgKind.identifier)})})
    verdict = g.check(_payload("q", {"id": "safe\r\ninjected"}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.control-chars"


def test_identifier_del_char_denied() -> None:
    # DEL (0x7F) is a control char _is_binary deliberately misses.
    g = _g({"q": ToolArgPolicy(args={"id": ArgSpec(kind=ToolArgKind.identifier)})})
    verdict = g.check(_payload("q", {"id": "db\x7fname"}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.control-chars"


def test_path_crlf_denied() -> None:
    g = _g({"read": ToolArgPolicy(args={"p": ArgSpec(kind=ToolArgKind.path)})})
    verdict = g.check(_payload("read", {"p": "logs/app\r\n.log"}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.control-chars"


def test_path_tab_denied() -> None:
    g = _g({"read": ToolArgPolicy(args={"p": ArgSpec(kind=ToolArgKind.path)})})
    verdict = g.check(_payload("read", {"p": "a\tb"}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.control-chars"


def test_url_embedded_newline_denied() -> None:
    # urlsplit STRIPS embedded \n, so the old code parsed this as a valid URL and
    # passed it; the control-char check now blocks it before _check_kind.
    g = _g({"fetch": ToolArgPolicy(args={"u": ArgSpec(kind=ToolArgKind.url)})})
    verdict = g.check(_payload("fetch", {"u": "http://example.com/\npath"}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.control-chars"


def test_email_crlf_denied() -> None:
    # SMTP header injection: any rule is acceptable, but it MUST block.
    g = _g({"send": ToolArgPolicy(args={"to": ArgSpec(kind=ToolArgKind.email)})})
    verdict = g.check(_payload("send", {"to": "a@b.com\r\nBcc: evil@x.com"}))
    assert not verdict.passed
    assert verdict.rule.startswith("toolargs.")


def test_text_kind_still_allows_newlines_and_tabs() -> None:
    # No-weakening pin: free text DELIBERATELY keeps \t\n\r (legitimate prose).
    g = _g({"note": ToolArgPolicy(args={"body": ArgSpec(kind=ToolArgKind.text)})})
    assert g.check(_payload("note", {"body": "line1\nline2\ttabbed"})).passed


def test_control_char_detail_never_echoes_value() -> None:
    g = _g({"read": ToolArgPolicy(args={"p": ArgSpec(kind=ToolArgKind.path)})})
    verdict = g.check(_payload("read", {"p": "logs/x\r\nSECRET-DO-NOT-LEAK"}))
    assert not verdict.passed
    assert "SECRET-DO-NOT-LEAK" not in verdict.detail
    assert "\r" not in verdict.detail
    assert "\n" not in verdict.detail


# --- NE-KG-MULTIKEY: bounded list arguments ------------------------------------


def test_a_list_value_is_still_rejected_when_the_spec_declares_no_list_max() -> None:
    """The default is unchanged: scalar-only, a list is a type error exactly as before."""
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec(kind=ToolArgKind.text)})})
    verdict = g.check(_payload("t", {"a": ["x", "y"]}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.wrong-type"


def test_a_bounded_list_passes_when_the_spec_opts_in() -> None:
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec(kind=ToolArgKind.text, list_max=3)})})
    assert g.check(_payload("t", {"a": ["x", "y"]})).passed
    # A scalar still passes through the unchanged scalar path.
    assert g.check(_payload("t", {"a": "x"})).passed


def test_a_list_over_its_cardinality_cap_is_denied() -> None:
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec(kind=ToolArgKind.text, list_max=2)})})
    verdict = g.check(_payload("t", {"a": ["x", "y", "z"]}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.oversized-list"


def test_an_empty_list_is_denied() -> None:
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec(kind=ToolArgKind.text, list_max=2)})})
    verdict = g.check(_payload("t", {"a": []}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.empty-list"


def test_every_member_faces_the_scalar_gate_and_one_bad_member_fails_the_call() -> None:
    """Members are validated through the SAME body — including the injection scan."""
    g = _g(
        {
            "t": ToolArgPolicy(
                args={"a": ArgSpec(kind=ToolArgKind.identifier, list_max=4, max_length=8)}
            )
        }
    )
    assert g.check(_payload("t", {"a": ["ok-1", "ok-2"]})).passed
    assert g.check(_payload("t", {"a": ["ok-1", "a; rm -rf"]})).rule == "toolargs.oversized-arg"
    assert g.check(_payload("t", {"a": ["ok-1", "a;rm"]})).rule == "toolargs.dangerous-pattern"
    assert g.check(_payload("t", {"a": ["ok-1", "sp ce"]})).rule == "toolargs.invalid-identifier"
    assert g.check(_payload("t", {"a": ["ok-1", 7]})).rule == "toolargs.wrong-type"
    assert g.check(_payload("t", {"a": ["ok-1", "toolong-x"]})).rule == "toolargs.oversized-arg"


def test_a_nested_list_member_is_rejected_not_recursed_into() -> None:
    """The cap bounds LEAVES, not just top-level members: no nesting escape hatch."""
    g = _g({"t": ToolArgPolicy(args={"a": ArgSpec(kind=ToolArgKind.text, list_max=4)})})
    verdict = g.check(_payload("t", {"a": ["ok", ["nested"] * 500]}))
    assert not verdict.passed
    assert verdict.rule == "toolargs.wrong-type"
