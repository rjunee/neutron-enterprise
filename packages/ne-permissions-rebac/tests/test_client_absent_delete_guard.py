# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Unit-seam proof for the ``_client._is_absent_delete`` narrow-catch guard.

Codex finding C1 (NE-REBAC-REVOKE-IDEMPOTENT re-dispatch, review round 2): a
message-substring-only check can misclassify an unrelated validation error as
an absent-delete if the rejected tuple's own id happens to embed the marker
text (OpenFGA echoes the offending tuple verbatim into ``error_message`` for
malformed-tuple-shape rejections — see
``test_openfga_live.py::test_live_non_absent_delete_error_not_swallowed`` for
the real-server proof of that echo behavior). ``_is_absent_delete`` now
requires ``status == 400`` AND ``code == "write_failed_due_to_invalid_input"``
as a structural pre-check BEFORE the message substring is even consulted, so
a crafted/malformed id containing the marker text cannot force a false
``TupleAbsentError`` classification. This file proves that guard directly at
the unit seam (no server required); the live suite proves the real server's
actual signatures still classify correctly.
"""

from __future__ import annotations

from openfga_sdk.exceptions import ApiException, ServiceException, ValidationException

from ne.permissions_rebac._client import _ABSENT_DELETE_MARKER, _is_absent_delete


class _FakeParsedException:
    """Stand-in for the SDK's decoded error-body model.

    ``ApiException.code`` and ``.error_message`` read ``self._parsed_exception
    .code`` / ``.message`` when set — this mirrors that shape without needing
    a live response to decode.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message


def _validation_exception(
    *, status: int | None, code: str | None, message: str
) -> ValidationException:
    exc = ValidationException(status=status, reason="Bad Request")
    if code is not None:
        exc.parsed_exception = _FakeParsedException(code=code, message=message)
    else:
        # No parsed body at all: .code reads None, .error_message falls back
        # to .reason ("Bad Request"), which never contains the marker.
        pass
    return exc


def test_matches_the_real_servers_exact_signature() -> None:
    """status=400 + code=write_failed_due_to_invalid_input + marker -> True."""
    exc = _validation_exception(
        status=400,
        code="write_failed_due_to_invalid_input",
        message=(
            "cannot delete a tuple which does not exist: user: 'user:nobody', "
            "relation: 'viewer', object: 'document:ghost': tuple to be written "
            "already existed or the tuple to be deleted did not exist"
        ),
    )
    assert _is_absent_delete(exc) is True


def test_wrong_code_with_marker_in_message_is_rejected() -> None:
    """C1: a DIFFERENT code carrying the marker text must NOT classify as absent-delete.

    Simulates a malformed-tuple id that happens to embed the marker string —
    OpenFGA's ``validation_error`` family echoes the offending tuple verbatim
    into ``error_message``, so a substring-only check could be fooled. The
    status+code structural pre-check must reject this before the substring is
    ever consulted.
    """
    exc = _validation_exception(
        status=400,
        code="validation_error",
        message=(
            "Invalid tuple 'document:d1#viewer@user:"
            f"{_ABSENT_DELETE_MARKER}-smuggled'. Reason: the 'user' field is malformed"
        ),
    )
    assert _is_absent_delete(exc) is False


def test_right_code_wrong_status_is_rejected() -> None:
    """Defense in depth: a non-400 status with the right code/message still declines."""
    exc = _validation_exception(
        status=409,
        code="write_failed_due_to_invalid_input",
        message=_ABSENT_DELETE_MARKER,
    )
    assert _is_absent_delete(exc) is False


def test_right_code_and_status_wrong_message_is_rejected() -> None:
    """The duplicate-write sibling: same code+status, different message, still declines.

    OpenFGA reuses ``write_failed_due_to_invalid_input`` for BOTH duplicate-
    write and absent-delete rejections, so the message substring remains the
    sole discriminator between those two once the structural pre-check
    passes.
    """
    exc = _validation_exception(
        status=400,
        code="write_failed_due_to_invalid_input",
        message=(
            "cannot write a tuple which already exists: user: 'user:jane', "
            "relation: 'viewer', object: 'document:d1': tuple to be written "
            "already existed or the tuple to be deleted did not exist"
        ),
    )
    assert _is_absent_delete(exc) is False


def test_non_validation_exception_is_rejected() -> None:
    """A differently-typed OpenApiException (e.g. a service/backend fault) never matches."""
    exc = ServiceException(status=503, reason="Service Unavailable")
    assert _is_absent_delete(exc) is False


def test_generic_api_exception_is_rejected() -> None:
    """A plain ApiException (not the ValidationException subtype) never matches."""
    exc = ApiException(status=400, reason="Bad Request")
    assert _is_absent_delete(exc) is False
