# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CKB2: systemic classify_license robustness (free-text normalization +
embedded-full-license-text detection).

Self-contained per the test_c4_qdrant_closure.py precedent: this module does
NOT import fixtures/helpers from other test modules, so it duplicates the
small helpers (real_policy/REPO_ROOT, write_lock, resolver_from,
_installed_version, _FakeMetadata/_patch_metadata) it needs.

Proves two things over `tools/ne-cli/src/ne/cli/licenses.py`:

1. A curated free-text-license-name -> SPDX alias map
   (``_FREE_TEXT_TO_SPDX``), consulted strictly AFTER the banned-dominance
   sweep, resolves common non-SPDX license strings (e.g. "Apache License
   2.0", "MIT License", "BSD License") to their SPDX id — while a
   spelled-out copyleft/banned name is NEVER mapped to a free id.
2. Conservative embedded-full-license-text detection
   (``_detect_license_text``) resolves a `License` metadata field that IS an
   entire license body (e.g. tiktoken's MIT text) via an exact first-line
   header match, fails closed on anything not confidently recognized, and
   never overrides banned-dominance.

The permanent systemic proof (test_recurring_closure_resolves_without_known)
shows the real recurring set (multidict/tiktoken/protobuf/fastembed) now
resolves FREE via this new logic alone, with their `[known]` entries
stripped out of the policy for the duration of that one test — the
COMMITTED license-policy.toml keeps those entries (belt-and-suspenders).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from importlib import metadata as _im
from pathlib import Path

import pytest

from ne.cli.licenses import (
    _FREE_TEXT_TO_SPDX,
    BANNED,
    FREE,
    GPL_NEEDS_APPROVAL,
    UNKNOWN,
    Policy,
    _detect_license_text,
    classify_license,
    default_policy_path,
    resolve_license,
    resolve_license_from_metadata,
    scan,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def real_policy() -> Policy:
    return Policy.load(default_policy_path(REPO_ROOT))


def _installed_version(name: str, default: str = "1.0.0") -> str:
    """Return the REAL installed version of `name`, or `default` if absent.

    Never hardcode a specific version literal here — the whole point is that
    this fixture tracks whatever is actually installed, so it stays correct
    as dependency versions move. The `default` fallback only matters for
    dists genuinely absent from this environment.
    """
    try:
        return _im.version(name)
    except _im.PackageNotFoundError:
        return default


def write_lock(tmp_path: Path, entries: list[dict]) -> Path:
    """Write a minimal synthetic uv.lock with the given package entries."""
    lines = ["version = 1", 'requires-python = ">=3.12"', ""]
    for e in entries:
        lines.append("[[package]]")
        lines.append(f'name = "{e["name"]}"')
        lines.append(f'version = "{e.get("version", "1.0.0")}"')
        lines.append('source = { registry = "https://pypi.org/simple" }')
        lines.append('wheels = [{ url = "https://example/x.whl", hash = "sha256:deadbeef" }]')
        lines.append("")
    path = tmp_path / "uv.lock"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def resolver_from(mapping: dict[str, str]) -> Callable[[str], str | None]:
    return lambda name: mapping.get(name)


def _override(cls: str, name: str = "dep", version: str = "1.0.0") -> dict:
    return {
        "name": name,
        "version": version,
        "class": cls,
        "justification": "test",
        "approved_by": "owner@example.com",
        "date": "2026-07-04",
    }


class _FakeMetadata:
    """Stand-in for ``importlib.metadata.metadata(name)`` return value."""

    def __init__(
        self,
        single: dict[str, str] | None = None,
        multi: dict[str, list[str]] | None = None,
    ) -> None:
        self._single = single or {}
        self._multi = multi or {}

    def get(self, key: str, default: object = None) -> object:
        return self._single.get(key, default)

    def get_all(self, key: str) -> list[str] | None:
        return self._multi.get(key)


def _patch_metadata(monkeypatch: pytest.MonkeyPatch, md: _FakeMetadata) -> None:
    # ``metadata`` is ``from importlib import metadata`` inside licenses.py, so
    # patch the ``metadata`` callable on that module object (monkeypatch restores).
    # NB: the ``licenses`` click Command re-exported by ne.cli shadows the module
    # attribute, so resolve the module via sys.modules rather than attribute access.
    import importlib

    _licenses = importlib.import_module("ne.cli.licenses")
    monkeypatch.setattr(_licenses.metadata, "metadata", lambda name: md)


# --------------------------------------------------------------------------- #
# Fixture texts.
# --------------------------------------------------------------------------- #

_MIT_FULL_TEXT = (
    "MIT License\n"
    "\n"
    "Copyright (c) 2022 OpenAI, Shantanu Jain\n"
    "\n"
    "Permission is hereby granted, free of charge, to any person obtaining a copy\n"
    'of this software and associated documentation files (the "Software"), to deal\n'
    "in the Software without restriction, including without limitation the rights\n"
    "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell\n"
    "copies of the Software, and to permit persons to whom the Software is\n"
    "furnished to do so, subject to the following conditions:\n"
    "\n"
    "The above copyright notice and this permission notice shall be included in all\n"
    "copies or substantial portions of the Software.\n"
    "\n"
    'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\n'
    "IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\n"
    "FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\n"
    "AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\n"
    "LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\n"
    "OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\n"
    "SOFTWARE.\n"
)

_APACHE2_FULL_TEXT = (
    "Apache License\n"
    "    Version 2.0, January 2004\n"
    "    http://www.apache.org/licenses/\n"
    "\n"
    "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION\n"
    "\n"
    "1. Definitions.\n"
    "\n"
    '"License" shall mean the terms and conditions for use, reproduction,\n'
    "and distribution as defined by Sections 1 through 9 of this document.\n"
)

_MPL2_TEXT = (
    "Mozilla Public License Version 2.0\n"
    "==================================\n"
    "\n"
    "1. Definitions\n"
    "\n"
    '1.1. "Contributor"\n'
    "    means each individual or legal entity that creates, contributes to\n"
    "    the creation of, or owns Covered Software.\n"
)

_BSD3_TEXT = (
    "BSD 3-Clause License\n"
    "\n"
    "Redistribution and use in source and binary forms, with or without\n"
    "modification, are permitted provided that the following conditions are met:\n"
    "\n"
    "1. Redistributions of source code must retain the above copyright notice, this\n"
    "   list of conditions and the following disclaimer.\n"
)

_AGPL3_TEXT = (
    "GNU AFFERO GENERAL PUBLIC LICENSE\n"
    "                       Version 3, 19 November 2007\n"
    "\n"
    " Copyright (C) 2007 Free Software Foundation, Inc. <https://fsf.org/>\n"
    " Everyone is permitted to copy and distribute verbatim copies\n"
    " of this license document, but changing it is not allowed.\n"
    "\n"
    "                            Preamble\n"
    "\n"
    "  The GNU Affero General Public License is a free, copyleft license for\n"
    "software and other kinds of works, specifically designed to ensure\n"
    "cooperation with the community in the case of network server software.\n"
)

_GPL2_TEXT = (
    "GNU GENERAL PUBLIC LICENSE\n"
    "                       Version 2, June 1991\n"
    "\n"
    " Copyright (C) 1989, 1991 Free Software Foundation, Inc.\n"
    " 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA\n"
    " Everyone is permitted to copy and distribute verbatim copies\n"
    " of this license document, but changing it is not allowed.\n"
    "\n"
    "                            Preamble\n"
    "\n"
    "  The licenses for most software are designed to take away your\n"
    "freedom to share and change it.\n"
)

_GARBAGE_TEXT = (
    "This software is provided under our custom Acme Corp terms. You may use "
    "it internally but redistribution requires written consent from the Acme "
    "Corp legal department. No warranty is provided. Contact "
    "legal@acmecorp.example for licensing inquiries regarding commercial "
    "redistribution rights and further terms of use for this proprietary "
    "internal toolkit software package."
)


# --------------------------------------------------------------------------- #
# (1) Free-text license-name normalization.
# --------------------------------------------------------------------------- #

_FREE_TEXT_FORMS = [
    ("Apache License 2.0", "Apache-2.0"),
    ("Apache License, Version 2.0", "Apache-2.0"),
    ("Apache Software License", "Apache-2.0"),
    ("Apache 2.0", "Apache-2.0"),
    ("Apache License", "Apache-2.0"),
    ("MIT License", "MIT"),
    ("The MIT License", "MIT"),
    ("BSD License", "BSD-3-Clause"),
    ("3-Clause BSD License", "BSD-3-Clause"),
    ("New BSD License", "BSD-3-Clause"),
    ("2-Clause BSD License", "BSD-2-Clause"),
    ("Simplified BSD License", "BSD-2-Clause"),
    ("Mozilla Public License 2.0 (MPL 2.0)", "MPL-2.0"),
    ("ISC License (ISCL)", "ISC"),
    ("Python Software Foundation License", "PSF-2.0"),
]


@pytest.mark.parametrize("form,spdx", _FREE_TEXT_FORMS)
def test_free_text_form_resolves_to_spdx_and_free(
    monkeypatch: pytest.MonkeyPatch, form: str, spdx: str
) -> None:
    _patch_metadata(monkeypatch, _FakeMetadata(single={"License": form}))
    policy = real_policy()
    resolved = resolve_license_from_metadata("dep", policy)
    assert resolved == spdx
    assert classify_license(resolved or "", policy) == FREE


def test_alias_normalization_case_and_whitespace_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = real_policy()
    for form in ("apache license 2.0", "  Apache   License  2.0 "):
        _patch_metadata(monkeypatch, _FakeMetadata(single={"License": form}))
        assert resolve_license_from_metadata("dep", policy) == "Apache-2.0"


def test_alias_map_values_are_policy_clean() -> None:
    """Structural proof of the CRITICAL SECURITY INVARIANT: the alias map can
    never map a banned/copyleft free-text name to a free SPDX id."""
    policy = real_policy()
    for key, value in _FREE_TEXT_TO_SPDX.items():
        classification = classify_license(value, policy)
        assert classification != BANNED, (key, value)
        assert classification != UNKNOWN, (key, value)
        assert classification in {FREE, GPL_NEEDS_APPROVAL, "lgpl-case-by-case"}, (
            key,
            value,
            classification,
        )
        if any(
            marker in key
            for marker in (
                "gpl",
                "general public",
                "affero",
                "sspl",
                "business source",
                "non-commercial",
            )
        ):
            assert classification != FREE, (key, value, classification)


def test_alias_rescue_applies_via_injected_resolver() -> None:
    policy = real_policy()
    assert "somedep" not in policy.known
    resolved = resolve_license(
        "somedep", policy, resolver=resolver_from({"somedep": "Apache License 2.0"})
    )
    assert resolved == "Apache-2.0"


# --------------------------------------------------------------------------- #
# Copyleft/banned free-text forms must never be free-mappable.
# --------------------------------------------------------------------------- #


def test_spelled_out_gpl_fails_gate_not_rescuable_not_free_overridable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_metadata(monkeypatch, _FakeMetadata(single={"License": "GNU General Public License v3"}))
    policy = real_policy()
    resolved = resolve_license_from_metadata("dep", policy)
    assert resolved == "GPL-3.0"
    assert classify_license(resolved or "", policy) == GPL_NEEDS_APPROVAL

    lock = write_lock(tmp_path, [{"name": "dep"}])
    report = scan(lock, policy)
    assert not report.ok
    pkg = next(p for p in report.packages if p.name == "dep")
    assert pkg.classification == GPL_NEEDS_APPROVAL

    # A planted [known] entry does NOT rescue: the rescue is keyed strictly on
    # UNKNOWN, and "GPL-3.0" classifies GPL_NEEDS_APPROVAL, not UNKNOWN.
    policy_known = dataclasses.replace(policy, known={"dep": "MIT"})
    resolved2 = resolve_license("dep", policy_known)
    assert resolved2 == "GPL-3.0"

    # A class="free" override cannot whitelist it either (class-mismatch, fail closed).
    policy_override = dataclasses.replace(
        policy, overrides={("dep", "1.0.0"): _override("free", name="dep")}
    )
    report2 = scan(lock, policy_override)
    assert not report2.ok
    assert any("does not satisfy" in e and "dep" in e for e in report2.policy_errors)


def test_spelled_out_affero_still_banned_non_overridable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    md = _FakeMetadata(
        single={"License": "GNU Affero General Public License v3"},
        multi={"Classifier": ["License :: OSI Approved :: MIT License"]},
    )
    _patch_metadata(monkeypatch, md)
    policy = dataclasses.replace(real_policy(), known={"dep": "MIT"})
    resolved = resolve_license_from_metadata("dep", policy)
    assert resolved == "GNU Affero General Public License v3"
    assert classify_license(resolved or "", policy) == BANNED

    lock = write_lock(tmp_path, [{"name": "dep"}])
    report = scan(lock, policy)
    assert not report.ok
    assert any("BANNED class (non-overridable)" in v for v in report.violations)

    policy2 = dataclasses.replace(
        policy,
        overrides={("dep", "1.0.0"): _override("out-of-process-approved", name="dep")},
    )
    report2 = scan(lock, policy2)
    assert any("dep" in e and "never overridable" in e for e in report2.policy_errors)


# --------------------------------------------------------------------------- #
# (2) Embedded full-license-text detection.
# --------------------------------------------------------------------------- #


def test_embedded_mit_text_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_metadata(monkeypatch, _FakeMetadata(single={"License": _MIT_FULL_TEXT}))
    policy = real_policy()
    resolved = resolve_license_from_metadata("dep", policy)
    assert resolved == "MIT"
    assert classify_license(resolved or "", policy) == FREE


def test_embedded_apache2_text_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_metadata(monkeypatch, _FakeMetadata(single={"License": _APACHE2_FULL_TEXT}))
    policy = real_policy()
    resolved = resolve_license_from_metadata("dep", policy)
    assert resolved == "Apache-2.0"
    assert classify_license(resolved or "", policy) == FREE


def test_embedded_mpl2_text_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_metadata(monkeypatch, _FakeMetadata(single={"License": _MPL2_TEXT}))
    policy = real_policy()
    resolved = resolve_license_from_metadata("dep", policy)
    assert resolved == "MPL-2.0"
    assert classify_license(resolved or "", policy) == FREE


def test_embedded_bsd3_header_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_metadata(monkeypatch, _FakeMetadata(single={"License": _BSD3_TEXT}))
    policy = real_policy()
    resolved = resolve_license_from_metadata("dep", policy)
    assert resolved == "BSD-3-Clause"
    assert classify_license(resolved or "", policy) == FREE


def test_embedded_agpl_text_banned_dominates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    md = _FakeMetadata(
        single={"License": _AGPL3_TEXT},
        multi={"Classifier": ["License :: OSI Approved :: MIT License"]},
    )
    _patch_metadata(monkeypatch, md)
    policy = real_policy()
    resolved = resolve_license_from_metadata("dep", policy)
    assert resolved == _AGPL3_TEXT.strip()
    assert classify_license(resolved or "", policy) == BANNED

    lock = write_lock(tmp_path, [{"name": "dep"}])
    report = scan(lock, policy)
    assert not report.ok

    policy2 = dataclasses.replace(
        policy, overrides={("dep", "1.0.0"): _override("free", name="dep")}
    )
    report2 = scan(lock, policy2)
    assert any("dep" in e and "never overridable" in e for e in report2.policy_errors)


def test_embedded_gpl2_text_not_detected_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _detect_license_text(_GPL2_TEXT) is None

    _patch_metadata(monkeypatch, _FakeMetadata(single={"License": _GPL2_TEXT}))
    policy = real_policy()
    resolved = resolve_license_from_metadata("dep", policy)
    assert resolved is None

    lock = write_lock(tmp_path, [{"name": "dep"}])
    report = scan(lock, policy)
    assert not report.ok
    pkg = next(p for p in report.packages if p.name == "dep")
    assert pkg.classification == UNKNOWN


def test_ambiguous_or_garbage_text_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _detect_license_text(_GARBAGE_TEXT) is None

    _patch_metadata(monkeypatch, _FakeMetadata(single={"License": _GARBAGE_TEXT}))
    policy = real_policy()
    resolved = resolve_license_from_metadata("dep", policy)
    assert resolved is None

    lock = write_lock(tmp_path, [{"name": "dep"}])
    report = scan(lock, policy)
    assert not report.ok

    # Precision guard: a header WITHOUT the required guard sentence must not match.
    near_miss = "MIT License\nAll rights reserved. Proprietary. No permission granted."
    assert _detect_license_text(near_miss) is None


def test_detection_and_alias_not_applied_without_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy path (no policy arg) is bit-identical to pre-NE-CKB2 behaviour."""
    _patch_metadata(monkeypatch, _FakeMetadata(single={"License": "Apache License 2.0"}))
    assert resolve_license_from_metadata("dep") == "Apache License 2.0"

    _patch_metadata(monkeypatch, _FakeMetadata(single={"License": _MIT_FULL_TEXT}))
    assert resolve_license_from_metadata("dep") is None


# --------------------------------------------------------------------------- #
# Rescue-chain ordering: [known] -> alias -> detection.
# --------------------------------------------------------------------------- #


def test_known_rescue_still_fires_for_non_alias_unknown() -> None:
    policy = dataclasses.replace(real_policy(), known={"somedep": "MIT"})
    resolver = resolver_from({"somedep": "Custom Acme Internal License v7"})
    assert classify_license("Custom Acme Internal License v7", policy) == UNKNOWN
    assert _FREE_TEXT_TO_SPDX.get("custom acme internal license v7") is None
    assert _detect_license_text("Custom Acme Internal License v7") is None
    resolved = resolve_license("somedep", policy, resolver=resolver)
    assert resolved == "MIT"


def test_known_beats_alias_for_specific_package() -> None:
    policy = dataclasses.replace(real_policy(), known={"somedep": "MPL-2.0"})
    resolver = resolver_from({"somedep": "BSD License"})
    resolved = resolve_license("somedep", policy, resolver=resolver)
    assert resolved == "MPL-2.0"


# --------------------------------------------------------------------------- #
# THE systemic proof: the recurring closure resolves without [known].
# --------------------------------------------------------------------------- #

_RECURRING_NAMES = ["multidict", "tiktoken", "protobuf", "fastembed"]

_RECURRING_RESOLVER_MAPPING = {
    "multidict": "Apache License 2.0",
    "protobuf": "3-Clause BSD License",
    "fastembed": "Apache License",
    "tiktoken": _MIT_FULL_TEXT,
}


def _stripped_policy() -> Policy:
    base = real_policy()
    for name in _RECURRING_NAMES:
        assert name in base.known, f"{name} missing from committed [known] (belt-and-suspenders)"
    return dataclasses.replace(
        base, known={k: v for k, v in base.known.items() if k not in _RECURRING_NAMES}
    )


def test_recurring_closure_resolves_without_known(tmp_path: Path) -> None:
    stripped = _stripped_policy()

    # (a) injected resolver, synthetic lock pinned to REAL installed versions.
    lock = write_lock(
        tmp_path, [{"name": n, "version": _installed_version(n)} for n in _RECURRING_NAMES]
    )
    report = scan(lock, stripped, resolver=resolver_from(_RECURRING_RESOLVER_MAPPING))
    assert report.ok, report.violations + report.policy_errors
    for pkg in report.packages:
        assert pkg.classification == FREE, f"{pkg.name}: {pkg.classification} ({pkg.license})"

    # (b) end-to-end against REAL installed metadata (default resolver).
    for name in _RECURRING_NAMES:
        resolved = resolve_license(name, stripped)
        assert resolved is not None, name
        assert classify_license(resolved, stripped) == FREE, (name, resolved)

    # The committed policy still carries the [known] entries untouched.
    committed = real_policy()
    for name in _RECURRING_NAMES:
        assert name in committed.known


def test_planted_agpl_still_rejected_with_new_logic(tmp_path: Path) -> None:
    """Negative control: the stripped-[known] closure is not a rubber stamp —
    a planted BANNED dep in the same closure still fails the gate."""
    stripped = _stripped_policy()
    names = [*_RECURRING_NAMES, "pymupdf"]
    lock = write_lock(tmp_path, [{"name": n, "version": _installed_version(n)} for n in names])
    mapping = dict(_RECURRING_RESOLVER_MAPPING)
    mapping["pymupdf"] = "AGPL-3.0"
    report = scan(lock, stripped, resolver=resolver_from(mapping))
    assert not report.ok
    pkg = next(p for p in report.packages if p.name == "pymupdf")
    assert pkg.classification == BANNED
    assert any("pymupdf" in v and "BANNED" in v for v in report.violations)
