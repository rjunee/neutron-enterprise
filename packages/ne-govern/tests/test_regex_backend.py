# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""RegexRedactor: the air-gap supplement recognizers + Luhn positive/negative pair."""

from __future__ import annotations

from ne.govern.redaction import RegexRedactor

_R = RegexRedactor()


def test_email_redacted() -> None:
    result = _R.redact("contact bob@example.com now")
    assert "<REDACTED:EMAIL_ADDRESS>" in result.text
    assert "EMAIL_ADDRESS" in result.redactions


def test_phone_redacted() -> None:
    result = _R.redact("call +1 415-555-2671 today")
    assert "<REDACTED:PHONE_NUMBER>" in result.text
    assert "PHONE_NUMBER" in result.redactions


def test_ssn_redacted() -> None:
    result = _R.redact("ssn 123-45-6789 on file")
    assert "<REDACTED:US_SSN>" in result.text
    assert "US_SSN" in result.redactions


def test_ip_address_redacted_and_octets_validated() -> None:
    result = _R.redact("host 192.168.1.1 up")
    assert "<REDACTED:IP_ADDRESS>" in result.text
    # An out-of-range octet quad is NOT a valid IPv4 and is left untouched.
    untouched = _R.redact("version 999.999.999.999 here")
    assert "999.999.999.999" in untouched.text
    assert "IP_ADDRESS" not in untouched.redactions


def test_iban_redacted() -> None:
    result = _R.redact("iban GB82WEST12345698765432 end")
    assert "<REDACTED:IBAN_CODE>" in result.text
    assert "IBAN_CODE" in result.redactions


def test_credit_card_luhn_valid_redacted_invalid_untouched() -> None:
    valid = _R.redact("card 4111 1111 1111 1111 ok")
    assert "<REDACTED:CREDIT_CARD>" in valid.text
    assert "CREDIT_CARD" in valid.redactions

    invalid = _R.redact("card 4111 1111 1111 1112 ok")
    assert "4111 1111 1111 1112" in invalid.text
    assert "CREDIT_CARD" not in invalid.redactions


def test_category_filter_only_runs_selected() -> None:
    result = _R.redact("bob@example.com and 123-45-6789", categories=["EMAIL_ADDRESS"])
    assert "<REDACTED:EMAIL_ADDRESS>" in result.text
    assert "123-45-6789" in result.text
    assert result.redactions == ("EMAIL_ADDRESS",)


def test_clean_text_returned_verbatim() -> None:
    result = _R.redact("nothing sensitive here at all")
    assert result.text == "nothing sensitive here at all"
    assert result.redactions == ()
