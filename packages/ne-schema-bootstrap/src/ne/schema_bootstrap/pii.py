# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic PII detection (design §3.3).

Two independent signals, both regex/lookup-based — no scoring model, no
randomness, so re-runs over an unchanged sample directory produce identical
flags: a column-NAME heuristic (family lookup on the normalized name, fires
regardless of the column's inferred type) and a column-VALUE shape heuristic
(fraction of sampled non-null values matching an email/phone/national-id
pattern) that runs ONLY when the column's inferred type is ``"string"``
(mirroring :func:`ne.schema_bootstrap.profiles._format_pattern`'s existing
gate — see :func:`pii_flags`'s docstring for why this gate matters). A
flagged column has its exemplars AND distinct-value list suppressed by the
profiler (:mod:`ne.schema_bootstrap.profiles`).
"""

from __future__ import annotations

import re

from .config import DEFAULT_CONFIG, BootstrapConfig
from .model import to_snake_case as normalize_name

__all__ = ["PII_NAME_FAMILIES", "normalize_name", "pii_flags"]

PII_NAME_FAMILIES: dict[str, frozenset[str]] = {
    "name": frozenset(
        {"name", "full_name", "first_name", "given_name", "last_name", "family_name", "surname"}
    ),
    "email": frozenset({"email", "email_address", "e_mail"}),
    "phone": frozenset({"phone", "mobile", "telephone", "fax", "phone_number"}),
    "national_id": frozenset(
        {"ssn", "national_id", "tax_id", "nino", "sin", "passport", "passport_number"}
    ),
    "dob": frozenset({"dob", "date_of_birth", "birth_date", "birthdate"}),
    "address": frozenset(
        {
            "address",
            "street",
            "street_address",
            "city",
            "postcode",
            "zip",
            "zip_code",
            "postal_code",
            "home_address",
        }
    ),
}

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_PATTERN = re.compile(r"^\+?[0-9()\[\] .-]{7,20}$")
NATIONAL_ID_PATTERN = re.compile(r"^\d{3}-\d{2}-\d{4}$")
# ISO-8601 date shape (``YYYY-MM-DD``): digits-and-dashes only, so it
# otherwise satisfies PHONE_PATTERN's charset and length bounds outright.
# Excluded explicitly so an ordinary opaquely-named date column (e.g.
# "opened_at") is never falsely PII-suppressed as a phone-number column.
_ISO_DATE_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Any character that is not a digit. PHONE_PATTERN's charset is digits PLUS
# optional separators, so a bare digit run (an order number, an account id)
# needs no separator at all to match it — over-firing on any plain 7-20
# digit numeric column. A phone shape requires at least one explicit
# separator (or a leading "+"), mirroring the ISO-date exclusion above: both
# guard the same failure mode (a value-shape charset too permissive for the
# label it assigns).
_NON_DIGIT = re.compile(r"\D")


def _match_name_family(normalized: str) -> str | None:
    """Exact match OR normalized name ends with ``_<token>`` for any token."""
    for family, tokens in PII_NAME_FAMILIES.items():
        for token in tokens:
            if normalized == token or normalized.endswith("_" + token):
                return family
    return None


def _is_phone_shaped(value: str) -> bool:
    if not PHONE_PATTERN.match(value):
        return False
    if _ISO_DATE_SHAPE.match(value):
        return False
    if NATIONAL_ID_PATTERN.match(value):
        # An SSN-shaped value (\d{3}-\d{2}-\d{4}) also clears the phone
        # charset/length floor; excluded so "shape:national_id" below is
        # reachable rather than permanently shadowed by this branch.
        return False
    if not _NON_DIGIT.search(value):
        return False  # bare digit run: indistinguishable from a plain identifier
    return len(re.findall(r"\d", value)) >= 7


def pii_flags(
    name: str,
    non_null_values: list[str],
    *,
    inferred_type: str,
    config: BootstrapConfig = DEFAULT_CONFIG,
) -> tuple[bool, str | None]:
    """Return ``(flagged, reason)`` for a column.

    ``reason`` is one of ``"name:<family>"`` (column-name heuristic, fires
    regardless of ``inferred_type``) or ``"shape:email"`` / ``"shape:phone"``
    / ``"shape:national_id"`` (value-shape heuristic, checked in that order,
    gated on ``config.pii_value_shape_threshold``).

    ``inferred_type`` is a REQUIRED keyword (no default) so every caller
    states the column's inferred type explicitly — every value-shape check
    runs ONLY when ``inferred_type == "string"``, mirroring
    :func:`ne.schema_bootstrap.profiles._format_pattern`'s existing gate.
    Without it, an ordinary numeric column (e.g. ``amount=12345.67`` or
    ``balance=-1234567``) can satisfy the phone-shape charset (digits plus
    ``.``/``-`` as "separators") purely by coincidence of its string
    rendering, silently blanking min/max/exemplars on a common-case numeric
    column that was never PII in the first place (see
    ``tests/test_pii.py::test_numeric_amount_and_balance_columns_not_flagged_as_phone``).
    """
    family = _match_name_family(normalize_name(name))
    if family is not None:
        return True, f"name:{family}"

    if inferred_type != "string" or not non_null_values:
        return False, None

    threshold = config.pii_value_shape_threshold
    total = len(non_null_values)

    email_matches = sum(1 for v in non_null_values if EMAIL_PATTERN.match(v))
    if email_matches / total >= threshold:
        return True, "shape:email"

    phone_matches = sum(1 for v in non_null_values if _is_phone_shaped(v))
    if phone_matches / total >= threshold:
        return True, "shape:phone"

    national_id_matches = sum(1 for v in non_null_values if NATIONAL_ID_PATTERN.match(v))
    if national_id_matches / total >= threshold:
        return True, "shape:national_id"

    return False, None
