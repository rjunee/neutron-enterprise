# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``ne.schema_bootstrap.pii`` and profile-level PII suppression."""

from __future__ import annotations

from pathlib import Path

from ne.schema_bootstrap.config import BootstrapConfig
from ne.schema_bootstrap.pii import pii_flags
from ne.schema_bootstrap.profiles import profile_samples


def test_name_family_flags_email_column() -> None:
    flagged, reason = pii_flags("email", ["a@example.com", "b@example.com"], inferred_type="string")
    assert flagged is True
    assert reason == "name:email"


def test_name_family_flags_ssn_suffix_column() -> None:
    flagged, reason = pii_flags("customer_ssn", ["123-45-6789"], inferred_type="string")
    assert flagged is True
    assert reason == "name:national_id"


def test_name_family_flags_date_of_birth_column() -> None:
    flagged, reason = pii_flags("date_of_birth", ["1990-01-01"], inferred_type="date")
    assert flagged is True
    assert reason == "name:dob"


def test_value_shape_flags_opaque_column_name_mostly_email() -> None:
    values = ["u1@example.com"] * 8 + ["n/a", "n/a"]
    flagged, reason = pii_flags("reachpoint", values, inferred_type="string")
    assert flagged is True
    assert reason == "shape:email"


def test_negative_status_column_with_codes_not_flagged() -> None:
    flagged, reason = pii_flags("status", ["active", "inactive", "pending"], inferred_type="string")
    assert flagged is False
    assert reason is None


def test_value_shape_flags_genuine_phone_numbers() -> None:
    # Positive control for "shape:phone" — no test previously exercised this
    # branch at all, so it was free to drift silently.
    values = ["+1 (555) 123-4567", "+1 (555) 987-6543", "(555) 222-3333"]
    flagged, reason = pii_flags("reachpoint", values, inferred_type="string")
    assert flagged is True
    assert reason == "shape:phone"


def test_phone_shaped_string_column_still_flags() -> None:
    # Bidirectional control for the 2026-07-28 arbiter-ruling fix below: the
    # STRING-typed case of a genuinely phone-shaped column must still flag —
    # the new `inferred_type` gate must narrow the false-positive (numeric
    # columns), not the true positive (an opaquely-named string column of
    # real phone numbers).
    values = ["(415) 555-0134", "(212) 555-0182", "(310) 555-0199"]
    flagged, reason = pii_flags("contact", values, inferred_type="string")
    assert flagged is True
    assert reason == "shape:phone"


def test_numeric_amount_and_balance_columns_not_flagged_as_phone() -> None:
    # 2026-07-28 SPEC §6.1.1 arbiter ruling: the value-shape checks ran
    # regardless of the column's inferred type, so an ordinary numeric
    # column's string rendering could coincidentally satisfy the phone-shape
    # charset (digits plus "." or "-" as "separators") — the arbiter
    # reproduced this exactly at HEAD. Gating value-shape checks on
    # `inferred_type == "string"` (mirroring `_format_pattern`'s existing
    # gate) fixes it: a genuinely numeric column is never value-shape-PII
    # checked at all, regardless of what its digits happen to look like.
    amount_flagged, amount_reason = pii_flags(
        "amount", ["12345.67", "23456.78", "34567.89"], inferred_type="float"
    )
    assert (amount_flagged, amount_reason) == (False, None)

    balance_flagged, balance_reason = pii_flags(
        "balance", ["-1234567", "-2345678", "-3456789"], inferred_type="int"
    )
    assert (balance_flagged, balance_reason) == (False, None)


def test_value_shape_flags_opaque_national_id_shaped_column() -> None:
    # 2026-07-28 Argus P2: NATIONAL_ID_PATTERN-matching values (11 chars,
    # 9 digits, two dashes) also clear PHONE_PATTERN's charset/length floor,
    # and the phone check ran first with no exclusion — so this branch was
    # unreachable dead code and every SSN-shaped opaque column was mislabeled
    # "shape:phone".
    values = ["123-45-6789", "987-65-4321", "111-22-3333"]
    flagged, reason = pii_flags("ref_code", values, inferred_type="string")
    assert flagged is True
    assert reason == "shape:national_id"


def test_bare_digit_run_not_flagged_as_phone() -> None:
    # 2026-07-28 Argus P2: PHONE_PATTERN's charset is digits plus OPTIONAL
    # separators, so a bare separator-free digit run (an order number, an
    # account id) needs no separator at all to match — over-firing on any
    # plain 7-20 digit numeric column. A phone shape now requires at least
    # one explicit separator character (or a leading "+").
    values = ["1000001", "1000002", "1000003", "1000004"]
    flagged, reason = pii_flags("order_number", values, inferred_type="string")
    assert flagged is False
    assert reason is None


def test_iso_date_shaped_values_not_falsely_flagged_as_phone() -> None:
    # ISO-8601 dates ("2024-03-01") satisfy the phone-shape charset
    # (digits/dashes only, >=7 digits) and would false-positive as
    # "shape:phone" without an explicit date-shape exclusion — an ordinary
    # opaquely-named date column must not be PII-suppressed on that basis.
    values = ["2024-03-01", "2024-01-15", "2024-02-10"]
    flagged, reason = pii_flags("opened_at", values, inferred_type="string")
    assert flagged is False
    assert reason is None


def test_value_shape_threshold_boundary_at_50_percent_flags() -> None:
    values = [f"u{i}@example.com" for i in range(5)] + [f"junk{i}" for i in range(5)]
    flagged, reason = pii_flags(
        "reachpoint",
        values,
        inferred_type="string",
        config=BootstrapConfig(pii_value_shape_threshold=0.5),
    )
    assert flagged is True
    assert reason == "shape:email"


def test_value_shape_threshold_boundary_at_40_percent_does_not_flag() -> None:
    values = [f"u{i}@example.com" for i in range(4)] + [f"junk{i}" for i in range(6)]
    flagged, reason = pii_flags(
        "reachpoint",
        values,
        inferred_type="string",
        config=BootstrapConfig(pii_value_shape_threshold=0.5),
    )
    assert flagged is False
    assert reason is None


def test_pii_exemplars_absent_from_serialized_profile(tmp_path: Path) -> None:
    lines = ["id,email,plan"]
    for i in range(6):
        lines.append(f"c{i},user{i}@customer-example.com,gold")
    (tmp_path / "customers.csv").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    serialized = bundle.profile.model_dump_json()

    # Value-absence over the SERIALIZED form: no raw flagged-column value
    # appears anywhere in the JSON.
    for i in range(6):
        assert f"user{i}@customer-example.com" not in serialized

    # Positive control: the sibling non-PII column's exemplar values DO
    # appear, proving serialization includes exemplars generally (the
    # absence assertion above is not vacuous).
    assert "gold" in serialized

    (table,) = bundle.profile.tables
    (email_col,) = [c for c in table.columns if c.name == "email"]
    assert email_col.pii_flag is True
    assert email_col.exemplars == []
    assert email_col.exemplars_suppressed is True
    assert email_col.distinct_values is None
    # Stats are retained even though values are suppressed.
    assert email_col.non_null_count == 6
    assert email_col.distinct_count == 6


def test_credential_exemplar_scrubbed_in_serialized_profile(tmp_path: Path) -> None:
    (tmp_path / "configs.csv").write_text(
        "id,note\nn1,AKIAABCDEFGHIJKLMNOP\nn2,AKIAABCDEFGHIJKLMNOP\n"
    )

    bundle = profile_samples(tmp_path)
    serialized = bundle.profile.model_dump_json()

    assert "[REDACTED]" in serialized
    assert "AKIAABCDEFGHIJKLMNOP" not in serialized


def test_pii_min_max_suppressed_in_serialized_profile(tmp_path: Path) -> None:
    # A name-flagged column ("dob") is orderable (infers as `date`), so an
    # unconditional _min_max computation would leak two real customer values
    # (the earliest/latest birth dates) into min_value/max_value even though
    # exemplars and distinct_values are already suppressed for this column.
    dates = ["1961-03-04", "1975-08-19", "1988-11-30", "1994-02-14", "1999-12-31"]
    lines = ["patient_id,dob"] + [f"p{i},{d}" for i, d in enumerate(dates)]
    (tmp_path / "patients.csv").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    serialized = bundle.profile.model_dump_json()

    (table,) = bundle.profile.tables
    (dob_col,) = [c for c in table.columns if c.name == "dob"]

    assert dob_col.pii_flag is True
    assert dob_col.pii_reason == "name:dob"
    assert dob_col.min_value is None
    assert dob_col.max_value is None

    # Non-vacuity control: the raw dates DID load and get counted — the
    # suppression is of the min/max VALUES, not of profiling the column.
    assert dob_col.non_null_count == 5

    # Bound proven over the SERIALIZED form, not just the in-memory object.
    for d in dates:
        assert d not in serialized
