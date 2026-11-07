# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The exemption against the DEPLOYED backend: PresidioRedactor, not RegexRedactor.

NE-U8-F3. ``GovernSettings.redactor`` defaults to ``"presidio"``, so every
deployed ``AllowlistExemptRedactor`` wraps a ``RedactionStage(PresidioRedactor)``
— and ``PresidioRedactor`` does not redact in-process: it serialises the text to
JSON, writes it to a temp file, runs an interpreter from an ISOLATED venv against
``presidio_env/redact_text.py``, and reads a JSON file back. The exemption's whole
mechanism is that private-use-area sentinels (U+E000/U+E001) survive that
round-trip unchanged. ``exempt.py`` ASSERTS they do; before this module nothing
proved it, and every existing exemption test wrapped ``RegexRedactor()``, which
never leaves the process.

What is proven here, and how:

* ``test_sentinels_survive_the_presidio_subprocess_round_trip`` and
  ``test_exemption_holds_through_the_real_presidio_transport`` exercise the REAL
  ``PresidioRedactor`` — the real ``json.dumps``/utf-8 temp file/``subprocess.run``/
  ``json.loads`` transport, the real ``ensure_env_at`` fast-path — against a FAKE
  in-venv worker (the established ``test_presidio_backend.py`` pattern: a tmp venv
  whose ``bin/python`` symlinks this interpreter, a preset requirements-sha marker
  so no network rebuild is attempted, and a stdlib worker honouring the JSON
  contract). The MODEL is faked; the ISOLATION BOUNDARY — the only thing that can
  eat a private-use codepoint — is real, so these run in CI.
* ``test_presidio_live.py`` carries the same assertion against the real Presidio
  model, live-parked behind ``NE_PRESIDIO_LIVE=1`` (building the env downloads the
  ~560 MB ``en_core_web_lg``), following this repo's existing convention for
  backend-gated tests.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from ne.govern.redaction.exempt import _SENTINEL_CLOSE, _SENTINEL_OPEN, AllowlistExemptRedactor
from ne.govern.redaction.presidio_backend import PresidioRedactor
from ne.govern.redaction.stage import RedactionStage

RECIPIENT = "owner@example.com"

# A stdlib worker that mimics the real redact_text.py contract: every address is
# replaced with <REDACTED:EMAIL_ADDRESS>, everything else is echoed back
# VERBATIM. It also copies the text it RECEIVED into a side file, so the test can
# assert the sentinels arrived intact rather than inferring it from the output.
_EMAIL_WORKER = r"""
import argparse, json, re
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--input", required=True, type=Path)
p.add_argument("--output", required=True, type=Path)
a = p.parse_args()
req = json.loads(a.input.read_text(encoding="utf-8"))
text = req["text"]
Path(__file__).with_name("received.txt").write_text(text, encoding="utf-8")
out, n = re.subn(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                 "<REDACTED:EMAIL_ADDRESS>", text)
a.output.write_text(
    json.dumps({"text": out, "redactions": ["EMAIL_ADDRESS"] if n else []}),
    encoding="utf-8",
)
"""


def _fake_presidio_env(tmp_path: Path) -> tuple[Path, Path]:
    """A (presidio_env_dir, venv_dir) pair whose worker runs for real, offline."""
    env_dir = tmp_path / "presidio_env"
    env_dir.mkdir()
    (env_dir / "requirements.txt").write_text("presidio-analyzer==2.2.363\n")
    (env_dir / "redact_text.py").write_text(_EMAIL_WORKER, encoding="utf-8")

    venv_dir = tmp_path / ".presidio-venv"
    (venv_dir / "bin").mkdir(parents=True)
    py = venv_dir / "bin" / "python"
    py.symlink_to(sys.executable)
    # Preset the sha marker so ensure_env_at treats the env as already built and
    # never attempts a (network) uv pip install.
    want = hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()
    (venv_dir / ".requirements.sha256").write_text(want)
    # No chmod: ``sys.executable`` is by definition executable, and the symlink
    # inherits its target's mode. A ``chmod(py.resolve(), ...)`` here would
    # RESOLVE THE SYMLINK and land on the host interpreter running this suite —
    # outside tmp_path entirely. On a read-only interpreter that raises OSError
    # (EROFS); on a writable one it silently mutates the host's permission bits
    # and the test still passes, which is the worse outcome. Nothing in this
    # module may touch a path outside ``tmp_path``.
    return env_dir, venv_dir


def test_sentinels_survive_the_presidio_subprocess_round_trip(tmp_path: Path) -> None:
    """The exact claim in exempt.py's module docstring, proven not asserted.

    The wrapper substitutes ``\\ue000<n>\\ue001`` for each exempted address BEFORE
    handing the text to the inner redactor and substitutes it back afterwards. If
    the JSON/utf-8/subprocess hop mangled, dropped, or escaped either codepoint,
    the restore would silently fail and the address would reach the caller as a
    sentinel (or as ``<REDACTED:EMAIL_ADDRESS>``) instead of itself.
    """
    env_dir, venv_dir = _fake_presidio_env(tmp_path)
    backend = PresidioRedactor(presidio_env_dir=env_dir, venv_dir=venv_dir)
    redactor = AllowlistExemptRedactor(RedactionStage(backend), (RECIPIENT,))

    result = redactor.redact(f"write to {RECIPIENT} today")

    # (1) The worker really did see the sentinels, byte-for-byte.
    received = (env_dir / "received.txt").read_text(encoding="utf-8")
    assert f"{_SENTINEL_OPEN}0{_SENTINEL_CLOSE}" in received
    assert RECIPIENT not in received, "the exempted address must not reach the subprocess"

    # (2) And they round-tripped back out intact, so the restore landed.
    assert result.text == f"write to {RECIPIENT} today"
    assert _SENTINEL_OPEN not in result.text
    assert _SENTINEL_CLOSE not in result.text


def test_exemption_holds_through_the_real_presidio_transport(tmp_path: Path) -> None:
    """The deployed shape end-to-end: exempt survives, everything else masks."""
    env_dir, venv_dir = _fake_presidio_env(tmp_path)
    backend = PresidioRedactor(presidio_env_dir=env_dir, venv_dir=venv_dir)
    redactor = AllowlistExemptRedactor(RedactionStage(backend), (RECIPIENT,))

    result = redactor.redact(f'{{"to": "{RECIPIENT}", "cc": "victim@example.com"}}')

    assert RECIPIENT in result.text
    assert "victim@example.com" not in result.text
    assert "<REDACTED:EMAIL_ADDRESS>" in result.text
    assert result.redactions == ("EMAIL_ADDRESS",)


def test_a_superstring_of_the_exempt_address_still_masks_through_presidio(
    tmp_path: Path,
) -> None:
    """The boundary guard is not weakened by the isolation hop."""
    env_dir, venv_dir = _fake_presidio_env(tmp_path)
    backend = PresidioRedactor(presidio_env_dir=env_dir, venv_dir=venv_dir)
    redactor = AllowlistExemptRedactor(RedactionStage(backend), (RECIPIENT,))

    result = redactor.redact("mail attacker.owner@example.com.evil.com now")

    assert "attacker.owner@example.com.evil.com" not in result.text
    assert "<REDACTED:EMAIL_ADDRESS>" in result.text
