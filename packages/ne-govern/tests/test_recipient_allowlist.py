# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""RecipientAllowlistGuardrail (C12): deny-by-default exfil control."""

from __future__ import annotations

import pytest

from ne.govern.config import RecipientSettings
from ne.govern.errors import GuardrailConfigError
from ne.govern.guardrails import RecipientAllowlistGuardrail, build_tool_call_payload

from .conftest import make_principal

_P = make_principal()


def _payload(*, recipients=(), egress_urls=()):
    return build_tool_call_payload(
        principal=_P, tool="send", recipients=recipients, egress_urls=egress_urls
    )


def test_exact_recipient_allowed() -> None:
    g = RecipientAllowlistGuardrail(RecipientSettings(allowed_recipients=("ok@corp.com",)))
    assert g.check(_payload(recipients=["ok@corp.com"])).passed


def test_domain_recipient_allowed() -> None:
    g = RecipientAllowlistGuardrail(RecipientSettings(allowed_recipient_domains=("corp.com",)))
    assert g.check(_payload(recipients=["anyone@corp.com"])).passed


def test_unlisted_recipient_denied() -> None:
    g = RecipientAllowlistGuardrail(RecipientSettings(allowed_recipients=("ok@corp.com",)))
    verdict = g.check(_payload(recipients=["evil@attacker.com"]))
    assert not verdict.passed
    assert verdict.rule == "recipients.not-allowlisted"


def test_empty_allowlist_with_recipient_denies_by_default() -> None:
    g = RecipientAllowlistGuardrail(RecipientSettings())
    verdict = g.check(_payload(recipients=["anyone@corp.com"]))
    assert not verdict.passed
    assert verdict.rule == "recipients.not-allowlisted"


def test_display_name_form_is_malformed() -> None:
    g = RecipientAllowlistGuardrail(RecipientSettings(allowed_recipient_domains=("x.com",)))
    verdict = g.check(_payload(recipients=["Bob <b@x.com>"]))
    assert not verdict.passed
    assert verdict.rule == "recipients.malformed"


def test_no_outbound_passes() -> None:
    g = RecipientAllowlistGuardrail(RecipientSettings())
    assert g.check(_payload()).passed


def test_egress_exact_host_allowed() -> None:
    g = RecipientAllowlistGuardrail(RecipientSettings(allowed_egress_hosts=("api.corp.com",)))
    assert g.check(_payload(egress_urls=["https://api.corp.com/v1"])).passed


def test_egress_wildcard_host_allowed() -> None:
    g = RecipientAllowlistGuardrail(RecipientSettings(allowed_egress_hosts=("*.corp.com",)))
    assert g.check(_payload(egress_urls=["https://api.corp.com/v1"])).passed
    assert g.check(_payload(egress_urls=["https://corp.com/v1"])).passed


def test_egress_userinfo_trick_blocked() -> None:
    g = RecipientAllowlistGuardrail(
        RecipientSettings(allowed_egress_hosts=("allowed.example.com",))
    )
    verdict = g.check(_payload(egress_urls=["https://allowed.example.com@evil.com/x"]))
    assert not verdict.passed
    assert verdict.rule == "egress.userinfo-forbidden"


def test_egress_bad_scheme_denied() -> None:
    g = RecipientAllowlistGuardrail(RecipientSettings(allowed_egress_hosts=("host",)))
    verdict = g.check(_payload(egress_urls=["ftp://host/x"]))
    assert not verdict.passed
    assert verdict.rule == "egress.bad-scheme"


def test_egress_unlisted_ip_literal_denied() -> None:
    g = RecipientAllowlistGuardrail(RecipientSettings(allowed_egress_hosts=("10.0.0.1",)))
    assert g.check(_payload(egress_urls=["https://10.0.0.1/x"])).passed
    verdict = g.check(_payload(egress_urls=["https://10.0.0.2/x"]))
    assert not verdict.passed
    assert verdict.rule == "egress.not-allowlisted"


# --- fail-closed: present-but-malformed outbound shape (Codex P2 continuation) ---

# Permissive allowlist so any block provably comes from the SHAPE, not the allowlist.
_PERMISSIVE = RecipientSettings(
    allowed_recipient_domains=("corp.com",), allowed_egress_hosts=("*.corp.com",)
)


@pytest.mark.parametrize("malformed", [{"to": "evil@x.com"}, 42, {"evil@x.com"}, b"evil@x.com"])
def test_malformed_recipients_shape_blocked(malformed) -> None:
    g = RecipientAllowlistGuardrail(_PERMISSIVE)
    p = _payload()
    p["recipients"] = malformed
    verdict = g.check(p)
    assert not verdict.passed
    assert verdict.rule == "recipients.malformed-shape"


@pytest.mark.parametrize(
    "malformed", [{"u": "https://evil.example"}, 3.5, {"https://evil.example"}]
)
def test_malformed_egress_urls_shape_blocked(malformed) -> None:
    g = RecipientAllowlistGuardrail(_PERMISSIVE)
    p = _payload()
    p["egress_urls"] = malformed
    verdict = g.check(p)
    assert not verdict.passed
    assert verdict.rule == "egress.malformed-shape"


def test_non_string_recipient_item_blocked() -> None:
    g = RecipientAllowlistGuardrail(_PERMISSIVE)
    p = _payload()
    p["recipients"] = [{"email": "evil@x.com"}]
    verdict = g.check(p)
    assert not verdict.passed
    assert verdict.rule == "recipients.malformed-shape"


def test_bare_string_recipient_still_single() -> None:
    g = RecipientAllowlistGuardrail(_PERMISSIVE)
    p = _payload()
    p["recipients"] = "anyone@corp.com"
    assert g.check(p).passed


def test_absent_or_none_outbound_still_passes() -> None:
    g = RecipientAllowlistGuardrail(_PERMISSIVE)
    p = _payload()
    del p["recipients"]
    del p["egress_urls"]
    verdict = g.check(p)
    assert verdict.passed
    assert verdict.rule == "recipients.no-outbound"

    p2 = _payload()
    p2["recipients"] = None
    p2["egress_urls"] = None
    verdict2 = g.check(p2)
    assert verdict2.passed
    assert verdict2.rule == "recipients.no-outbound"


def test_malformed_shape_detail_never_echoes_value() -> None:
    g = RecipientAllowlistGuardrail(_PERMISSIVE)
    p = _payload()
    p["recipients"] = {"exfil": "SENTINEL-DO-NOT-LEAK@evil.example"}
    verdict = g.check(p)
    assert not verdict.passed
    assert "SENTINEL-DO-NOT-LEAK" not in verdict.detail


# --- allowed_recipients SHAPE, enforced at the origin (NE-U8-F1) -------------
#
# ``allowed_recipients`` is exact-match and sits next to ``allowed_recipient_domains``,
# so a bare domain in the wrong key is a natural operator slip. Inside THIS
# guardrail such an entry is inert (proven below) — the operator gets no signal —
# but ne-api's composition root also feeds this tuple to the pre-egress redaction
# exemption, where a bare domain becomes a substring wildcard. Rejecting it at
# settings construction is therefore free: nothing it could have matched is lost.


@pytest.mark.parametrize("bad", ["example.com", "ryan", "@example.com", "Bob <b@x.com>", "a@b"])
def test_non_address_allowed_recipients_entry_is_rejected_at_construction(bad: str) -> None:
    with pytest.raises(GuardrailConfigError) as exc:
        RecipientSettings(allowed_recipients=(bad,))
    assert "allowed_recipients" in str(exc.value)
    assert "allowed_recipient_domains" in str(exc.value)


def test_domain_entry_in_the_domains_key_is_still_accepted() -> None:
    """The tightening is on the exact-match key ONLY — the sibling is untouched."""
    settings = RecipientSettings(allowed_recipient_domains=("example.com",))
    g = RecipientAllowlistGuardrail(settings)
    assert g.check(_payload(recipients=["anyone@example.com"])).passed


def test_the_rejected_entry_would_have_been_inert_here_anyway() -> None:
    """Why rejecting breaks nothing: the guardrail requires a valid address and
    then compares for EXACT equality, so a bare-domain entry can never match.

    Constructed through ``model_construct`` to bypass the new validator — this is
    the behaviour the pre-fix config would have had, and it is a silent no-op.
    """
    inert = RecipientSettings.model_construct(allowed_recipients=("example.com",))
    g = RecipientAllowlistGuardrail(inert)
    assert not g.check(_payload(recipients=["attacker@example.com"])).passed
    assert not g.check(_payload(recipients=["example.com"])).passed
