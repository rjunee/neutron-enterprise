# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
r"""The ONE compose-dotenv line serializer (NE-CLI-DOTENV-UNIFY).

Both Compose's ``--env-file`` (``ne.cli.up``) and a service's ``env_file:``
(``ne.cli.demo.live``) are read by the SAME compose-go dotenv parser, so
there is exactly one wire format here and there must be exactly one
serializer for it.

History: the single-quote idiom below was introduced for NE-DEMO-WRITEBACK-LIVE
(Codex caught, as a P1, that an unquoted ``#``/``$``/embedded-``"`` value is
silently truncated or re-interpolated by Compose) and lived as a private
``_dotenv_line`` in ``demo/live.py``. A second, independently-written
serializer (``up.py``'s ``_write_env_file``, escaping only ``$`` to ``$$``)
later reopened the SAME bug class in a second location — three silent
corruption paths confirmed at runtime by Argus against real containers
(Compose 2.36.2): ``val #trailing`` truncated at the space-hash, leading/
trailing whitespace silently stripped, and wrapping double quotes silently
stripped (NE-BEW-INJECT, PR #331, Argus P2). Filed for the structural reason
(two serializers for one wire format), not because either path was live-
exploitable at the time.

Every value is single-quoted: compose-go's dotenv parser takes a
single-quoted value fully literally — no ``#`` comment start, no ``$VAR``/
``${VAR}`` expansion, no escape processing. Three shapes cannot be
represented on a single dotenv line at all and fail loud instead of silently
corrupting the secret: a raw newline/carriage-return (would inject an extra
``KEY=VALUE`` line), an embedded single quote (cannot be escaped inside a
single-quoted dotenv value), and a value ending in an ODD number of
backslashes. That last one is not obvious and was caught only by empirical
verification, not by reading the spec: compose-go treats a backslash
immediately before the closing ``'`` as an ESCAPED quote and keeps scanning
into the FOLLOWING line looking for the real terminator. Confirmed live on
Compose 2.36.2 — a file whose first line is ``NE_A='abc\'`` and second line
is ``NE_B='secret-value'`` fails ``docker compose --env-file t.env config``
with ``line 2: unexpected character "'" in variable name "secret-value'"``,
i.e. compose ECHOES THE NEXT KEY'S VALUE to stderr, a C12 leak, while also
killing the bring-up with an opaque error. An EVEN backslash run is safe
(``NE_A='abc\\'`` round-trips as ``abc\\``) and an interior backslash is
safe (``NE_C='mid\dle'`` round-trips as ``mid\dle``) — only a trailing ODD
run is hazardous, which is why the guard checks the trailing run's parity,
not "does this value contain a backslash". Every failure message names the
KEY only, never the value (C12).

Compose-version note: the idiom above was empirically verified against
Compose 2.36.2; upstream compose-go's ``.env``/``env_file``/interpolation
behavior is documented identical from v2.24+ through the current v5.3.1
(deliberate 2.x -> 5.0.0 major-version jump, Dec 2025) — see
github.com/docker/compose/releases and the v5 migration notes. This module
carries no compose-version-conditional behavior.

The normative test authority for this module's exact byte-level behavior is
``tools/ne-cli/tests/test_envfile.py`` — read that file, not this docstring,
for the guaranteed output shape of any specific input.
"""

from __future__ import annotations

import re

import click

#: A POSIX-shell-compatible env var NAME. Keys are NOT all compile-time
#: constants — ``_resolve_required_secrets_async`` (``pilotcfg.py``) and
#: ``_write_teardown_env`` (``up.py``) both build their key set as
#: ``{**REQUIRED_SECRETS, **from_bundle}`` where ``from_bundle`` comes from
#: ``json.loads(bundle/'secrets'/'required.json')``, so env-var NAMES flow in
#: from a bundle file. Without this guard a key carrying a newline emits TWO
#: dotenv lines from ONE call — e.g.
#: ``dotenv_line("A\nNE_PILOT_POSTGRES_PASSWORD=owned", "placeholder")``
#: returned ``"A\nNE_PILOT_POSTGRES_PASSWORD=owned='placeholder'\n"``, an
#: injected override of a password variable (Argus P2, reproduced by the
#: orchestrator before fixing). A bare ``=`` in the key does the same thing
#: within one line.
#:
#: The gap was PRE-EXISTING (the deleted ``$``->``$$`` serializer had it too)
#: and is mitigated — ``secrets/required.json`` is inside the sealed manifest
#: so tampering is detectable by ``ne pilot verify``, and the most reachable
#: path (``ne down``) writes only ``_TEARDOWN_PLACEHOLDER`` values. It is
#: fixed here anyway because it is precisely the structural class this unit
#: exists to close: ONE shared serializer for one wire format that validated
#: only HALF the line it emitted.
_ENV_KEY_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def dotenv_line(key: str, value: str) -> str:
    """Serialize one ``KEY='VALUE'``\\n line for a compose-go dotenv file.

    Raises ``click.ClickException`` (naming ``key`` only, never ``value`` —
    C12) for any value that cannot be safely represented: a newline/carriage
    -return (would inject an additional line), a control character (refused
    on principle — env values must be single-line printable text), an
    embedded single quote (cannot be escaped inside a single-quoted value),
    or a value ending in an odd number of backslashes (compose-go reads the
    last one as an escaped closing quote and bleeds into the next line —
    confirmed live on Compose 2.36.2, see the module docstring).
    Also rejects a ``key`` that is not a plain env var NAME
    (``[A-Za-z_][A-Za-z0-9_]*``) — see :data:`_ENV_KEY_RE` for why keys are
    not trustworthy inputs and what a newline or ``=`` in one would inject.
    See ``tools/ne-cli/tests/test_envfile.py`` for the exact-byte contract.
    """
    if not _ENV_KEY_RE.match(key):
        # The key is the untrusted thing here, so it is quoted via !r (which
        # escapes a newline rather than letting it break the message across
        # lines) and truncated, instead of interpolated raw.
        shown = repr(key[:64])
        raise click.ClickException(
            f"refusing to serialize env key {shown}: not a plain env var name "
            "([A-Za-z_][A-Za-z0-9_]*). A key carrying a newline or '=' would "
            "emit an ADDITIONAL dotenv line from a single call, silently "
            "overriding another variable."
        )
    if "\n" in value or "\r" in value:
        raise click.ClickException(
            f"{key}'s value contains a newline/carriage-return, which cannot be "
            "represented on a single Compose env_file line."
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise click.ClickException(
            f"refusing to serialize {key}: value contains a control character — "
            "env values must be single-line printable text (C12)"
        )
    if "'" in value:
        raise click.ClickException(
            f"{key}'s value contains a single quote ('), which cannot be safely "
            "escaped inside a single-quoted Compose env_file value. Rotate/"
            "reformat this value to avoid embedded single quotes."
        )
    if (len(value) - len(value.rstrip("\\"))) % 2 == 1:
        raise click.ClickException(
            f"{key}'s value ends in an odd number of backslashes, which "
            "compose-go's dotenv parser reads as an escaped closing quote — "
            "this would corrupt the rest of the env file. Rotate/reformat "
            "this value to avoid a trailing odd backslash run."
        )
    return f"{key}='{value}'\n"
