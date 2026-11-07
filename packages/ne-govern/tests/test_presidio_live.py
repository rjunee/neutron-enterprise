# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED end-to-end test against the real isolated Presidio env.

Skipped unless NE_PRESIDIO_LIVE=1. Building the env downloads presidio + spaCy +
the ~560 MB en_core_web_lg model into packages/ne-govern/.presidio-venv. To run:

    NE_PRESIDIO_LIVE=1 uv run pytest packages/ne-govern/tests/test_presidio_live.py -q

(The first run builds the hash-pinned venv via `uv venv` + `uv pip install
--require-hashes -r packages/ne-govern/presidio_env/requirements.txt`; subsequent
runs reuse it while the requirements-sha marker matches.)

PERSON + EMAIL_ADDRESS + US_SSN are ALL asserted hard. The NE-CKC11 custom
context-gated recognizer (NeUsSsnContextRecognizer, registered in
presidio_env/redact_text.py) closes the presidio-analyzer 2.2.363 sample-number
under-redaction gap, so the bare-US_SSN case is a hard assertion again.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ne.govern.redaction import PresidioRedactor, RedactionStage
from ne.govern.redaction.exempt import AllowlistExemptRedactor

if os.environ.get("NE_PRESIDIO_LIVE") != "1":
    pytest.skip(
        "LIVE-PARKED: real Presidio env pending — re-run with "
        "`NE_PRESIDIO_LIVE=1 uv run pytest packages/ne-govern/tests/test_presidio_live.py -q` "
        "(builds the ~560 MB en_core_web_lg venv)",
        allow_module_level=True,
    )

_REPO_ROOT = Path(__file__).resolve().parents[3]


_PARAGRAPH = "John Smith emailed john.smith@example.com; his SSN is 123-45-6789."


def test_person_email_paragraph_is_redacted() -> None:
    """PERSON + EMAIL_ADDRESS must be redacted end-to-end."""
    stage = RedactionStage(PresidioRedactor(_REPO_ROOT))
    result = stage.redact(_PARAGRAPH)
    assert "John Smith" not in result.text
    assert "john.smith@example.com" not in result.text
    assert "PERSON" in result.redactions
    assert "EMAIL_ADDRESS" in result.redactions


def test_ssn_paragraph_is_redacted() -> None:
    # The builtin UsSsnRecognizer invalidates 123-45-6789 as a blocklisted sample
    # number; coverage comes from the context-gated NeUsSsnContextRecognizer
    # registered in presidio_env/redact_text.py (NE-CKC11).
    stage = RedactionStage(PresidioRedactor(_REPO_ROOT))
    result = stage.redact(_PARAGRAPH)
    assert "123-45-6789" not in result.text
    assert "US_SSN" in result.redactions


def test_ssn_spaced_and_dotted_variants_with_context_are_redacted() -> None:
    """Spaced and dotted 3-2-4 SSN forms redact as US_SSN when SSN context precedes."""
    stage = RedactionStage(PresidioRedactor(_REPO_ROOT))
    for paragraph, ssn in (
        ("Her SSN is 123 45 6789.", "123 45 6789"),
        ("social security number: 123.45.6789", "123.45.6789"),
    ):
        result = stage.redact(paragraph)
        assert ssn not in result.text
        assert "US_SSN" in result.redactions


def test_nine_digit_without_ssn_context_is_not_redacted_as_ssn() -> None:
    """A bare 9-digit order number without SSN context is NOT redacted as US_SSN.

    Asserts only on redactions (not text): other weak builtin entities like
    US_BANK_NUMBER may legitimately fire at the default threshold 0 — that is
    pre-existing behavior, out of scope.
    """
    stage = RedactionStage(PresidioRedactor(_REPO_ROOT))
    result = stage.redact("Order number 123456789 was shipped on Tuesday.")
    assert "US_SSN" not in result.redactions


def test_delimited_number_without_ssn_context_is_not_redacted_as_ssn() -> None:
    """A delimited 3-2-4 number without SSN context is NOT redacted as US_SSN.

    The builtin invalidates this sample number, so any US_SSN here could only
    come from the NEW recognizer — this directly proves it is context-gated.
    """
    stage = RedactionStage(PresidioRedactor(_REPO_ROOT))
    result = stage.redact("Tracking ref 123-45-6789 was closed without action.")
    assert "US_SSN" not in result.redactions


# --- NE-U8-F3: the allowlist exemption against the REAL Presidio backend ------
#
# The exemption's mechanism is private-use-area sentinels swapped in around each
# configured address before the inner redactor runs and swapped back afterwards.
# Against the deployed backend that inner redactor is a SUBPROCESS in an isolated
# venv, reached over a JSON/utf-8 file protocol. The transport half of that claim
# is proven in CI in test_redaction_exempt_presidio.py (real PresidioRedactor,
# fake in-venv worker); this is the other half — the real analyzer/anonymizer
# pipeline, which is the piece that could in principle re-emit or normalize the
# codepoints out of the text it hands back.


def test_exempt_address_survives_the_real_presidio_pipeline() -> None:
    recipient = "owner@example.com"
    redactor = AllowlistExemptRedactor(RedactionStage(PresidioRedactor(_REPO_ROOT)), (recipient,))

    result = redactor.redact(
        f"John Smith asked us to write to {recipient}; copy john.smith@example.com."
    )

    assert recipient in result.text
    assert "john.smith@example.com" not in result.text
    assert "\ue000" not in result.text
    assert "\ue001" not in result.text
    assert "EMAIL_ADDRESS" in result.redactions


def test_superstring_of_the_exempt_address_still_masks_under_real_presidio() -> None:
    recipient = "owner@example.com"
    redactor = AllowlistExemptRedactor(RedactionStage(PresidioRedactor(_REPO_ROOT)), (recipient,))

    result = redactor.redact("Mail attacker.owner@example.com.evil.com immediately.")

    assert "attacker.owner@example.com.evil.com" not in result.text
    assert "EMAIL_ADDRESS" in result.redactions
