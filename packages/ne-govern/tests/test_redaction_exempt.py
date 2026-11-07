# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AllowlistExemptRedactor: deployment-owned addresses survive; everything else redacts.

NE-CS-USABLE-ADDRESS: pre-egress redaction (NE-162) masked the governed CRM
read's recorded email into ``<REDACTED:EMAIL_ADDRESS>``, so the model could
never hand ``draft.email.create`` a usable recipient and the S5 approval beat
never parked. The exemption is EXACT configured addresses only — the send
allowlist and the draft mailbox — with boundary guards so a longer address
containing an exempted one still redacts.
"""

from __future__ import annotations

import pytest

from ne.govern.errors import RedactionConfigError, RedactionError
from ne.govern.factory import create_redactor
from ne.govern.redaction.exempt import AllowlistExemptRedactor
from ne.govern.redaction.regex_backend import RegexRedactor
from ne.govern.redaction.stage import RedactionStage

RECIPIENT = "owner@example.com"
MAILBOX = "owner@contoso.onmicrosoft.com"


def _exempt(*addresses: str) -> AllowlistExemptRedactor:
    return AllowlistExemptRedactor(RedactionStage(RegexRedactor()), addresses)


def test_exempt_address_survives_and_other_addresses_still_redact() -> None:
    text = f'{{"email": "{RECIPIENT}", "cc": "victim@example.com"}}'
    result = _exempt(RECIPIENT).redact(text)
    assert RECIPIENT in result.text
    assert "victim@example.com" not in result.text
    assert "<REDACTED:EMAIL_ADDRESS>" in result.text
    # The non-exempt hit still counts as a redaction.
    assert "EMAIL_ADDRESS" in result.redactions


def test_both_configured_addresses_survive_one_body() -> None:
    text = f"draft in {MAILBOX} to the recorded address {RECIPIENT}."
    result = _exempt(RECIPIENT, MAILBOX).redact(text)
    assert MAILBOX in result.text
    assert RECIPIENT in result.text
    assert "<REDACTED" not in result.text


def test_exemption_is_case_insensitive_and_preserves_source_spelling() -> None:
    result = _exempt(RECIPIENT).redact("Contact Owner@Example.COM today")
    assert "Owner@Example.COM" in result.text


def test_longer_address_containing_exempted_one_still_redacts() -> None:
    for other in (f"x{RECIPIENT}", f"{RECIPIENT}.evil.com", f"bob.{RECIPIENT}"):
        result = _exempt(RECIPIENT).redact(f"send to {other} now")
        assert other not in result.text, other
        assert "<REDACTED:EMAIL_ADDRESS>" in result.text


def test_trailing_sentence_punctuation_does_not_defeat_the_exemption() -> None:
    result = _exempt(RECIPIENT).redact(f"The recorded address is {RECIPIENT}. Thanks.")
    assert RECIPIENT in result.text


def test_sentinel_codepoints_in_input_fail_closed_to_full_redaction() -> None:
    # Adversarial text carrying the private-use sentinels: the exemption is
    # skipped entirely and the address redacts as normal (masking, not leaking).
    text = f"\ue0000\ue001 {RECIPIENT}"
    result = _exempt(RECIPIENT).redact(text)
    assert RECIPIENT not in result.text
    assert "<REDACTED:EMAIL_ADDRESS>" in result.text


def test_non_address_pii_still_redacts_next_to_an_exempt_address() -> None:
    result = _exempt(RECIPIENT).redact(f"{RECIPIENT} ssn 123-45-6789")
    assert RECIPIENT in result.text
    assert "123-45-6789" not in result.text


def test_inner_failure_still_fails_closed() -> None:
    class _Boom:
        def redact(self, text: str, *, categories=None):  # type: ignore[no-untyped-def]
            raise ValueError("backend exploded")

    wrapped = AllowlistExemptRedactor(RedactionStage(_Boom()), (RECIPIENT,))
    with pytest.raises(RedactionError):
        wrapped.redact(f"to {RECIPIENT} and victim@example.com")


def test_factory_with_no_exemptions_is_unchanged() -> None:
    redactor = create_redactor()
    assert isinstance(redactor, RedactionStage)


def test_factory_with_exemptions_wraps_the_stage() -> None:
    redactor = create_redactor(exempt_addresses=(RECIPIENT,))
    assert isinstance(redactor, AllowlistExemptRedactor)


# --- exemption-entry SHAPE: a non-address entry is a substring wildcard -------
#
# NE-U8-F1. ``AllowlistExemptRedactor`` turns each entry into
# ``re.escape(entry)`` between two token-boundary guards. That is EXACTLY right
# for a full address and catastrophic for anything shorter: a bare domain
# ``example.com`` matches inside ``attacker@example.com`` (the ``@`` is not in the
# left-guard's class, so the guard permits the match) and a bare local part
# ``owner`` matches inside ``owner@evil.example``. Either would carry an
# attacker-chosen address through pre-egress redaction to the frontier provider.
#
# The entries reach the redactor from ``guardrails.recipients.allowed_recipients``
# and ``ApiSettings.email_draft_allowed_mailboxes``. A bare domain in the FORMER
# is a natural operator slip (its sibling key is ``allowed_recipient_domains``)
# and is INERT in its own guardrail, so nothing warns the operator. The redactor
# therefore refuses it at construction: the composition root builds this at boot,
# so a malformed entry refuses the boot rather than silently widening the mask.


@pytest.mark.parametrize(
    ("entry", "victim"),
    [
        ("example.com", "attacker@example.com"),
        ("ryan", "ryan@evil.example"),
        ("@example.com", "attacker@example.com"),
        ("example", "example@evil.example"),
    ],
)
def test_non_address_exemption_entry_is_refused_not_made_a_substring_wildcard(
    entry: str, victim: str
) -> None:
    with pytest.raises(RedactionConfigError) as exc:
        _exempt(entry)
    assert entry in str(exc.value)
    assert "bare email address" in str(exc.value)

    # And the refusal is what keeps the victim masked: had the entry been
    # accepted, this is the body that would have reached the frontier provider
    # with `victim` intact.
    assert victim not in _exempt(RECIPIENT).redact(f"mail {victim} now").text


def test_a_refused_entry_beside_a_valid_one_still_refuses_the_whole_redactor() -> None:
    """No partial construction: one bad entry is not silently dropped."""
    with pytest.raises(RedactionConfigError):
        _exempt(RECIPIENT, "example.com")


def test_factory_refuses_a_non_address_exemption() -> None:
    """The boot path: ``create_redactor`` cannot hand back a wildcard redactor."""
    with pytest.raises(RedactionConfigError):
        create_redactor(exempt_addresses=(RECIPIENT, "example.com"))


def test_whitespace_and_case_variants_of_a_real_address_still_construct() -> None:
    """The tightening must not reject the legitimate configured shapes."""
    redactor = _exempt(f"  {RECIPIENT}  ", RECIPIENT.upper(), MAILBOX)
    result = redactor.redact(f"{RECIPIENT} and {MAILBOX} and victim@example.com")
    assert RECIPIENT in result.text
    assert MAILBOX in result.text
    assert "victim@example.com" not in result.text
