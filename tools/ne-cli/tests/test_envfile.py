# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Normative byte-level contract for ``ne.cli.envfile.dotenv_line``.

NE-CLI-DOTENV-UNIFY: this is the ONE serializer shared by ``ne.cli.up`` and
``ne.cli.demo.live``. Every adversarial-value assertion here is an exact ``==``
comparison (or an ``in``-with-trailing-newline exact-line form) — never a bare
substring/startswith — per the repo's anti-vacuous-guard policy: a test that
merely checks the value appears SOMEWHERE in the line would pass under the
weak ``$``-only-escape serializer this unit removes.
"""

from __future__ import annotations

import shlex

import click
import pytest

from ne.cli.envfile import dotenv_line

# Argus's three confirmed silent-corruption paths (Compose 2.36.2, against
# real containers) plus the pre-existing $-expansion and control-row cases.
_ADVERSARIAL_VALUES = [
    "val #trailing",  # Argus path 1: unquoted `#` starts an inline comment
    "  lead-and-trail  ",  # Argus path 2: unquoted leading/trailing ws stripped
    '"dquoted"',  # Argus path 3: wrapping double quotes silently stripped
    "a$b",  # unquoted $VAR/${VAR} expansion mid-value
    "${VAR}",  # unquoted ${VAR} expansion
    "#lead",  # unquoted leading `#` — whole value read as a comment
    '[{"table": "tickets", "id": 1}]',  # JSON carrying embedded double quotes
    "",  # empty value
    "hex-abc123",  # control row: a plain value must also round-trip via '='
    "abc\\\\",  # even (2) trailing backslashes — safe, must round-trip
    "mid\\dle",  # interior backslash — safe, no trailing run at all
]


@pytest.mark.parametrize("value", _ADVERSARIAL_VALUES)
def test_dotenv_line_single_quotes_adversarial_values_exact_bytes(value: str) -> None:
    line = dotenv_line("NE_X", value)
    # Exact full-line equality — never substring/startswith (anti-vacuous-guard).
    assert line == f"NE_X='{value}'\n"
    # shlex is a WEAKER approximation of single-quote literalness, not an
    # equivalent authority: it diverges from compose-go's dotenv parser on a
    # trailing ODD backslash run (shlex.split("'abc\\'") == ['abc\\'], while
    # compose-go reads the same bytes as an escaped closing quote and bleeds
    # into the next line — see test_dotenv_line_rejects_trailing_odd_backslash
    # _run_key_only below). That divergence can never reach dotenv_line's
    # output because the odd-backslash guard rejects it before this point, so
    # for every value that actually reaches here, shlex agrees with compose-go
    # and this assertion is a valid extra check — just not an independent
    # semantics authority in general.
    _, _, rhs = line.rstrip("\n").partition("=")
    assert shlex.split(rhs) == [value]


@pytest.mark.parametrize(
    "value",
    [
        "abc\\",  # odd (1) trailing backslash
        "abc\\\\\\",  # odd (3) trailing backslashes
    ],
)
def test_dotenv_line_rejects_trailing_odd_backslash_run_key_only(value: str) -> None:
    """Compose 2.36.2, confirmed live: a trailing odd backslash run before the
    closing quote is read as an escaped ``'`` and compose keeps scanning into
    the NEXT line, echoing that line's value in its parse error (a C12 leak)
    and killing the whole env file. Even runs and interior backslashes are
    safe and are covered as accept rows in
    ``test_dotenv_line_single_quotes_adversarial_values_exact_bytes`` above —
    this guard is bidirectional: it must reject odd runs and accept even/
    interior ones.
    """
    with pytest.raises(click.ClickException) as exc_info:
        dotenv_line("NE_KEY", value)
    message = str(exc_info.value)
    assert "NE_KEY" in message
    assert value not in message


def test_dotenv_line_rejects_newline_and_cr_key_only() -> None:
    """The newline/CR branch, locked by its OWN message.

    Argus P3 (mutation-confirmed): asserting only "key present, value absent"
    did NOT lock this branch. Deleting the ``"\\n" in value or "\\r" in value``
    check left 103 tests GREEN, because the ``ord(ch) < 0x20`` guard below
    subsumes ``\\n`` (0x0A) and ``\\r`` (0x0D) and its message also satisfies
    those two assertions. The behavioural contract held either way, so this
    was never a correctness hole — but a test NAMED for a branch must fail
    when that branch is removed, or it is a guard satisfiable by exactly the
    change it forbids. Asserting the newline-specific wording locks the
    branch's only real contribution: the better operator-facing error.
    """
    for bad in ("a\nb", "a\rb"):
        with pytest.raises(click.ClickException) as exc_info:
            dotenv_line("NE_KEY", bad)
        message = str(exc_info.value)
        assert "NE_KEY" in message
        assert "newline/carriage-return" in message, (
            "this test is named for the newline/CR branch, so it must assert that "
            f"branch's own message — got: {message!r}"
        )
        assert "a\nb" not in message
        assert "a\rb" not in message
        assert bad not in message


@pytest.mark.parametrize(
    "bad_key",
    [
        "A\nNE_PILOT_POSTGRES_PASSWORD=owned",  # Argus P2: injects a SECOND line
        "K=INJECT",  # injects an override within one line
        "has space",
        "1LEADING_DIGIT",
        "",
    ],
)
def test_dotenv_line_rejects_non_name_keys(bad_key: str) -> None:
    """Keys are NOT trustworthy inputs — they flow in from a bundle file.

    Before this guard, ``dotenv_line("A\\nNE_PILOT_POSTGRES_PASSWORD=owned",
    "placeholder")`` returned ``"A\\nNE_PILOT_POSTGRES_PASSWORD=owned=
    'placeholder'\\n"`` — ONE call emitting TWO dotenv lines, silently
    overriding a password variable (reproduced directly before fixing).
    """
    with pytest.raises(click.ClickException) as exc_info:
        dotenv_line(bad_key, "harmless")
    message = str(exc_info.value)
    assert "not a plain env var name" in message
    # The rejected key is shown via repr, so a newline in it can never break
    # the message across lines (which is how it would injection-shape a log).
    assert "\n" not in message


def test_dotenv_line_accepts_the_real_key_shapes() -> None:
    """Control: the guard must not reject keys this repo actually emits.

    Without this, a guard that rejected EVERY key would pass every rejection
    case above and look like a working discriminator — the invalid-probe trap.
    """
    for good in ("NE_PILOT_ENTRA_CLIENT_SECRET", "_LEADING_UNDERSCORE", "A1"):
        assert dotenv_line(good, "v") == f"{good}='v'\n"


@pytest.mark.parametrize("bad", ["\x01", "\x7f", "has\ttab"])
def test_dotenv_line_rejects_control_chars_key_only(bad: str) -> None:
    with pytest.raises(click.ClickException) as exc_info:
        dotenv_line("NE_KEY", bad)
    message = str(exc_info.value)
    assert "NE_KEY" in message
    assert bad not in message


def test_dotenv_line_rejects_embedded_single_quote_key_only() -> None:
    with pytest.raises(click.ClickException) as exc_info:
        dotenv_line("NE_KEY", "it's")
    message = str(exc_info.value)
    assert "NE_KEY" in message
    assert "it's" not in message
