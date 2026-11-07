# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The raise SITE — the ledger-safe field that a class name cannot substitute for.

``ne.contracts.failure``'s driver-shaped projection is covered against real
sqlite/psycopg faults in ``packages/ne-connect/tests/test_failure_projection.py``
(where those drivers are declared dependencies). What lives here is the part with
no driver in it: the site derivation and the ledger field mapping built from it,
which is what the governed tool executor records for a failed ``tool.execute``.
"""

from __future__ import annotations

from pathlib import Path

from ne.contracts.failure import failure_code, failure_fields, failure_site


def _raised_here() -> LookupError:
    try:
        raise LookupError("knowledge source evidence unavailable")
    except LookupError as exc:
        return exc


def _raised_there() -> LookupError:
    try:
        raise LookupError("knowledge source evidence unavailable")
    except LookupError as exc:
        return exc


def test_two_identical_exceptions_from_two_lines_get_two_sites() -> None:
    """The defect in one assertion: same class, same message, different origin."""
    first = failure_site(_raised_here())
    second = failure_site(_raised_there())
    assert first is not None and second is not None
    assert first != second
    # Line numbers are not hard-coded (any edit above would shift them); the
    # invariant is that the two sites NAME their own function and disagree.
    assert ":_raised_here:" in first
    assert ":_raised_there:" in second
    assert int(first.rsplit(":", 1)[1]) < int(second.rsplit(":", 1)[1])


def test_a_site_names_the_module_and_the_function() -> None:
    site = failure_site(_raised_here())
    assert site is not None
    module, qualname, lineno = site.rsplit(":", 2)
    assert module.endswith("test_failure_site")
    assert qualname == "_raised_here"
    assert lineno.isdigit()


def test_an_exception_that_was_never_raised_has_no_site() -> None:
    """Constructed, not raised: no traceback, so no site is INVENTED."""
    assert failure_site(LookupError("never raised")) is None
    assert failure_fields(LookupError("never raised")) == {"error_class": "LookupError"}


def test_fields_lead_with_the_class_and_add_only_derivable_keys() -> None:
    fields = failure_fields(_raised_here())
    assert next(iter(fields)) == "error_class"
    assert fields["error_class"] == "LookupError"
    assert "error_site" in fields
    # No driver underneath, nothing wrapped: those keys stay absent rather than
    # being filled with a placeholder nobody measured.
    assert set(fields) == {"error_class", "error_site"}


def test_fields_never_carry_the_message() -> None:
    """C12: the message may interpolate a tool ARGUMENT, so it is not a ledger field."""

    def _raise_with_a_value() -> ValueError:
        try:
            raise ValueError("trace not found: 0f8b2c1d-secret-corpus-identifier")
        except ValueError as exc:
            return exc

    fields = failure_fields(_raise_with_a_value())
    assert "secret-corpus-identifier" not in repr(fields)
    assert all("not found" not in value for value in fields.values())


def test_a_wrapped_cause_records_both_sites() -> None:
    def _inner() -> None:
        raise KeyError("missing")

    def _wrapped() -> LookupError:
        try:
            try:
                _inner()
            except KeyError as exc:
                raise LookupError("evidence unavailable") from exc
        except LookupError as outer:
            return outer
        raise AssertionError("the wrapper must raise")

    fields = failure_fields(_wrapped())

    assert fields["error_class"] == "LookupError"
    assert fields["error_origin"] == "KeyError"
    # The re-raise site and the site that actually failed are different frames,
    # and the row keeps both — the reclassification is visible, not hidden.
    assert fields["error_origin_site"] != fields["error_site"]
    assert "._inner:" in fields["error_origin_site"]


def test_the_site_reads_code_identity_and_never_frame_locals() -> None:
    """Structural guardian: ``f_locals`` holds argument VALUES. It is never read."""
    import ne.contracts.failure as module

    source = Path(module.__file__ or "").read_text(encoding="utf-8")
    assert "f_locals" not in source


# --- shape enforcement on driver codes (retrospective review, finding 1) -----
#
# This module's discipline is that provider text never reaches the ledger, and
# failure_fields' output goes straight into the tool.execute audit detail. The
# codes below are read off the DRIVER, which reads them off the SERVER, so the
# "closed identifier" property has to be ENFORCED rather than documented.


class _FakeDriverError(Exception):
    def __init__(self, *, sqlstate: str | None = None, sqlite_errorname: str | None = None) -> None:
        super().__init__("driver fault")
        if sqlstate is not None:
            self.sqlstate = sqlstate
        if sqlite_errorname is not None:
            self.sqlite_errorname = sqlite_errorname


def test_a_real_sqlstate_still_reaches_the_ledger() -> None:
    """The fix must not cost a single genuine code — both drivers' real shapes pass."""
    for code in ("40P01", "55P03", "57014", "23505", "53300"):
        assert failure_code(_FakeDriverError(sqlstate=code)) == code


def test_a_real_sqlite_errorname_still_reaches_the_ledger() -> None:
    for name in ("SQLITE_READONLY", "SQLITE_BUSY", "SQLITE_ERROR", "SQLITE_CONSTRAINT_UNIQUE"):
        assert failure_code(_FakeDriverError(sqlite_errorname=name)) == name


def test_a_server_supplied_payload_is_DROPPED_not_recorded() -> None:
    """The finding itself: an over-long or non-conforming 'code' must not reach audit.

    A SQLSTATE is five alphanumerics. Anything else is not a code — it is text
    from across a trust boundary, and this module exists to keep that out of the
    ledger. Dropping to None is the same conservative choice the module already
    makes for a fault carrying no code at all.
    """
    for hostile in (
        "40P01; postgres://user:hunter2@db/neutron",
        "x" * 4096,
        "40P0",
        "40P011",
        "40 01",
        "SELECT * FROM ne_audit_log",
    ):
        assert failure_code(_FakeDriverError(sqlstate=hostile)) is None


def test_a_nonconforming_sqlite_name_is_DROPPED() -> None:
    for hostile in ("sqlite_readonly", "SQLITE_", "NOT_SQLITE_BUSY", "SQLITE_" + "A" * 200):
        assert failure_code(_FakeDriverError(sqlite_errorname=hostile)) is None


def test_the_dropped_code_leaves_the_rest_of_the_row_intact() -> None:
    """Dropping the code must not cost error_class or error_site — a failure that
    is recorded must still state WHAT failed, which is this module's purpose."""
    try:
        raise _FakeDriverError(sqlstate="x" * 500)
    except _FakeDriverError as exc:
        fields = failure_fields(exc)
    assert fields["error_class"] == "_FakeDriverError"
    assert "error_site" in fields
    assert "error_code" not in fields
