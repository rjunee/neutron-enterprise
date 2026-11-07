# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint-A rehearsal: the fail-closed license gate (SPEC §2 C4).

These tests plant banned/unknown/copyleft dependencies and assert the scanner
fails closed, and that the override mechanism is structurally unable to
whitelist the banned class.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable
from importlib import metadata as importlib_metadata
from pathlib import Path

import pytest
from click.testing import CliRunner

from ne.cli import main
from ne.cli.licenses import (
    BANNED,
    FREE,
    GPL_NEEDS_APPROVAL,
    LGPL_NEEDS_APPROVAL,
    UNKNOWN,
    Policy,
    classify_license,
    default_policy_path,
    resolve_license,
    resolve_license_from_metadata,
    scan,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def real_policy() -> Policy:
    return Policy.load(default_policy_path(REPO_ROOT))


def write_lock(tmp_path: Path, entries: list[dict]) -> Path:
    """Write a minimal synthetic uv.lock with the given package entries.

    ``first_party`` defaults to an ``editable`` source; pass ``source_kind`` to
    force ``virtual`` / ``directory`` / ``editable`` explicitly.
    """
    lines = ["version = 1", 'requires-python = ">=3.12"', ""]
    for e in entries:
        lines.append("[[package]]")
        lines.append(f'name = "{e["name"]}"')
        lines.append(f'version = "{e.get("version", "1.0.0")}"')
        if e.get("first_party"):
            kind = e.get("source_kind", "editable")
            lines.append(f'source = {{ {kind} = "packages/{e["name"]}" }}')
        else:
            lines.append('source = { registry = "https://pypi.org/simple" }')
            if e.get("hashed", True):
                lines.append(
                    'wheels = [{ url = "https://example/x.whl", hash = "sha256:deadbeef" }]'
                )
        lines.append("")
    path = tmp_path / "uv.lock"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def resolver_from(mapping: dict[str, str]) -> Callable[[str], str | None]:
    return lambda name: mapping.get(name)


def test_banned_agpl_fails(tmp_path: Path) -> None:
    lock = write_lock(tmp_path, [{"name": "evil-agpl"}])
    report = scan(lock, real_policy(), resolver=resolver_from({"evil-agpl": "AGPL-3.0-only"}))
    assert not report.ok
    assert any("evil-agpl" in v for v in report.violations)


@pytest.mark.parametrize(
    "spdx",
    [
        "SSPL-1.0",
        "BUSL-1.1",
        "FSL-1.1-MIT",
        "CC-BY-NC-4.0",
        "Fair Source License v0.9",
        "Commons-Clause",
        "Elastic License 1.0",
    ],
)
def test_banned_patterns_fail(spdx: str) -> None:
    assert classify_license(spdx, real_policy()) == BANNED


def test_unknown_license_fails_closed(tmp_path: Path) -> None:
    lock = write_lock(tmp_path, [{"name": "mystery"}])
    report = scan(lock, real_policy(), resolver=resolver_from({}))  # unresolvable
    assert not report.ok
    assert any("mystery" in v for v in report.violations)
    assert classify_license("", real_policy()) == UNKNOWN


def test_override_cannot_whitelist_banned(tmp_path: Path) -> None:
    policy = dataclasses.replace(
        real_policy(),
        overrides={
            ("evil-agpl", "1.0.0"): {
                "name": "evil-agpl",
                "version": "1.0.0",
                "class": "free",
                "justification": "we really want it",
                "approved_by": "someone",
                "date": "2026-07-03",
            }
        },
    )
    lock = write_lock(tmp_path, [{"name": "evil-agpl"}])
    report = scan(lock, policy, resolver=resolver_from({"evil-agpl": "AGPL-3.0-only"}))
    assert not report.ok
    # Must be a POLICY ERROR, not a silent green.
    assert any("evil-agpl" in e and "never overridable" in e for e in report.policy_errors)


def test_gpl_in_process_fails_without_approval() -> None:
    assert classify_license("GPL-3.0-only", real_policy()) == GPL_NEEDS_APPROVAL


def test_lgpl_case_by_case_requires_override(tmp_path: Path) -> None:
    assert classify_license("LGPL-3.0-only", real_policy()) == LGPL_NEEDS_APPROVAL
    lock = write_lock(tmp_path, [{"name": "somelgpl"}])
    report = scan(lock, real_policy(), resolver=resolver_from({"somelgpl": "LGPL-2.1-or-later"}))
    assert not report.ok


def test_lgpl_with_approved_override_passes(tmp_path: Path) -> None:
    policy = dataclasses.replace(
        real_policy(),
        overrides={
            ("somelgpl", "1.0.0"): {
                "name": "somelgpl",
                "version": "1.0.0",
                "class": "case-by-case-approved",
                "justification": "dynamically linked LGPL shared lib",
                "approved_by": "owner@example.com",
                "date": "2026-07-03",
            }
        },
    )
    lock = write_lock(tmp_path, [{"name": "somelgpl"}])
    report = scan(lock, policy, resolver=resolver_from({"somelgpl": "LGPL-2.1-or-later"}))
    assert report.ok, report.violations + report.policy_errors


def test_elastic2_in_env_fails(tmp_path: Path) -> None:
    lock = write_lock(tmp_path, [{"name": "elastic-thing"}])
    report = scan(lock, real_policy(), resolver=resolver_from({"elastic-thing": "Elastic-2.0"}))
    assert not report.ok


def test_spdx_or_expression_any_free_passes() -> None:
    assert classify_license("Apache-2.0 OR GPL-3.0-only", real_policy()) == FREE
    assert classify_license("MIT OR LGPL-3.0", real_policy()) == FREE


def test_spdx_complex_expression_fails_closed() -> None:
    assert classify_license("(MIT AND GPL-3.0-only)", real_policy()) == UNKNOWN


def test_first_party_packages_exempt_and_apache(tmp_path: Path) -> None:
    lock = write_lock(tmp_path, [{"name": "ne-thing", "first_party": True}])
    # Correct first-party license -> allowed, exempt from third-party classes.
    report = scan(lock, real_policy(), resolver=resolver_from({"ne-thing": "Apache-2.0"}))
    assert report.ok
    assert report.packages[0].first_party is True
    # A first-party package declaring a different license -> violation.
    bad = scan(lock, real_policy(), resolver=resolver_from({"ne-thing": "MIT"}))
    assert not bad.ok


def test_real_repo_gate_passes() -> None:
    """The gate self-hosts: it must be green on the actual synced repo."""
    cwd = Path.cwd()
    try:
        os.chdir(REPO_ROOT)
        result = CliRunner().invoke(main, ["check", "licenses"])
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0, result.output


def test_env_dist_missing_from_lock_fails(tmp_path: Path) -> None:
    lock = write_lock(tmp_path, [{"name": "click", "first_party": False}])
    report = scan(
        lock,
        real_policy(),
        resolver=resolver_from({"click": "BSD-3-Clause"}),
        installed_names={"click", "ghost-package"},
    )
    assert not report.ok
    assert any("ghost-package" in v and "drift" in v for v in report.violations)


# --------------------------------------------------------------------------- #
# C4 override MUST class-match the resolved classification (fail closed).
# --------------------------------------------------------------------------- #


def _override(cls: str, name: str = "somedep", version: str = "1.0.0") -> dict:
    return {
        "name": name,
        "version": version,
        "class": cls,
        "justification": "test",
        "approved_by": "owner@example.com",
        "date": "2026-07-03",
    }


def test_free_override_on_gpl_dep_fails_class_mismatch(tmp_path: Path) -> None:
    policy = dataclasses.replace(real_policy(), overrides={("somedep", "1.0.0"): _override("free")})
    lock = write_lock(tmp_path, [{"name": "somedep"}])
    report = scan(lock, policy, resolver=resolver_from({"somedep": "GPL-3.0-only"}))
    assert not report.ok
    assert any("does not satisfy" in e and "somedep" in e for e in report.policy_errors)
    assert any("somedep" in v for v in report.violations)


def test_free_override_on_unknown_dep_fails_class_mismatch(tmp_path: Path) -> None:
    policy = dataclasses.replace(real_policy(), overrides={("somedep", "1.0.0"): _override("free")})
    lock = write_lock(tmp_path, [{"name": "somedep"}])
    report = scan(lock, policy, resolver=resolver_from({}))  # unresolvable -> UNKNOWN
    assert not report.ok
    assert any("does not satisfy" in e for e in report.policy_errors)


def test_out_of_process_override_on_gpl_passes(tmp_path: Path) -> None:
    policy = dataclasses.replace(
        real_policy(),
        overrides={("somedep", "1.0.0"): _override("out-of-process-approved")},
    )
    lock = write_lock(tmp_path, [{"name": "somedep"}])
    report = scan(lock, policy, resolver=resolver_from({"somedep": "GPL-3.0-only"}))
    assert report.ok, report.violations + report.policy_errors


def test_case_by_case_override_on_gpl_fails(tmp_path: Path) -> None:
    # case-by-case-approved satisfies LGPL, NOT GPL — must fail closed on GPL.
    policy = dataclasses.replace(
        real_policy(),
        overrides={("somedep", "1.0.0"): _override("case-by-case-approved")},
    )
    lock = write_lock(tmp_path, [{"name": "somedep"}])
    report = scan(lock, policy, resolver=resolver_from({"somedep": "GPL-3.0-only"}))
    assert not report.ok
    assert any("does not satisfy" in e for e in report.policy_errors)


# --------------------------------------------------------------------------- #
# Spelled-out banned-license names classify BANNED (not overridable).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "spelled_out",
    [
        "GNU AFFERO GPL 3.0",
        "GNU Affero General Public License v3.0",
        "Server Side Public License v1",
        "Business Source License 1.1",
        "PolyForm Non-Commercial 1.0.0",
        "CC BY-NC-SA 4.0",
    ],
)
def test_spelled_out_banned_forms_classify_banned(spelled_out: str) -> None:
    assert classify_license(spelled_out, real_policy()) == BANNED


def test_override_cannot_whitelist_spelled_out_affero(tmp_path: Path) -> None:
    policy = dataclasses.replace(
        real_policy(),
        overrides={("evil", "1.0.0"): _override("out-of-process-approved", name="evil")},
    )
    lock = write_lock(tmp_path, [{"name": "evil"}])
    report = scan(
        lock, policy, resolver=resolver_from({"evil": "GNU Affero General Public License v3.0"})
    )
    assert not report.ok
    # Banned is structurally non-overridable -> policy error.
    assert any("evil" in e and "never overridable" in e for e in report.policy_errors)


# --------------------------------------------------------------------------- #
# First-party fail-closed (item 12): virtual root exempt, uninstalled member fails.
# --------------------------------------------------------------------------- #


def test_virtual_root_stays_exempt(tmp_path: Path) -> None:
    lock = write_lock(
        tmp_path, [{"name": "neutron-enterprise", "first_party": True, "source_kind": "virtual"}]
    )
    # A virtual package has no installable metadata -> resolver never consulted.
    report = scan(lock, real_policy(), resolver=resolver_from({}))
    assert report.ok, report.violations + report.policy_errors
    assert report.packages[0].first_party is True
    assert report.packages[0].allowed is True


def test_first_party_directory_uninstalled_fails_closed(tmp_path: Path) -> None:
    lock = write_lock(
        tmp_path, [{"name": "ne-thing", "first_party": True, "source_kind": "directory"}]
    )
    # Directory member whose metadata is NOT installed -> cannot verify -> fail closed.
    report = scan(lock, real_policy(), resolver=resolver_from({}))
    assert not report.ok
    assert any("ne-thing" in v and "cannot verify" in v for v in report.violations)


# --------------------------------------------------------------------------- #
# P2 #2: banned dominates across ALL metadata sources (C4 defense-in-depth).
# --------------------------------------------------------------------------- #


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


def test_banned_license_field_dominates_permissive_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    md = _FakeMetadata(
        single={"License": "AGPL-3.0"},
        multi={"Classifier": ["License :: OSI Approved :: MIT License"]},
    )
    _patch_metadata(monkeypatch, md)
    result = resolve_license_from_metadata("dep", real_policy())
    assert result == "AGPL-3.0"
    assert classify_license(result or "", real_policy()) == BANNED


def test_banned_license_field_dominates_license_expression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    md = _FakeMetadata(single={"License-Expression": "MIT", "License": "SSPL-1.0"})
    _patch_metadata(monkeypatch, md)
    assert resolve_license_from_metadata("dep", real_policy()) == "SSPL-1.0"


def test_permissive_dep_resolves_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = real_policy()
    # (a) License-Expression only.
    _patch_metadata(monkeypatch, _FakeMetadata(single={"License-Expression": "Apache-2.0"}))
    assert resolve_license_from_metadata("dep", policy) == "Apache-2.0"
    # (b) single permissive classifier.
    _patch_metadata(
        monkeypatch,
        _FakeMetadata(multi={"Classifier": ["License :: OSI Approved :: MIT License"]}),
    )
    assert resolve_license_from_metadata("dep", policy) == "MIT"
    # (c) dual permissive classifiers -> OR expression.
    _patch_metadata(
        monkeypatch,
        _FakeMetadata(
            multi={
                "Classifier": [
                    "License :: OSI Approved :: MIT License",
                    "License :: OSI Approved :: Apache Software License",
                ]
            }
        ),
    )
    assert resolve_license_from_metadata("dep", policy) == "MIT OR Apache-2.0"


def test_legacy_no_policy_precedence_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    md = _FakeMetadata(
        single={"License": "AGPL-3.0"},
        multi={"Classifier": ["License :: OSI Approved :: MIT License"]},
    )
    _patch_metadata(monkeypatch, md)
    # policy=None -> legacy precedence-only path: classifier wins, banned field ignored.
    assert resolve_license_from_metadata("dep") == "MIT"


def test_banned_field_dep_fails_gate_and_is_non_overridable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = write_lock(tmp_path, [{"name": "masked-agpl"}])
    md = _FakeMetadata(
        single={"License": "AGPL-3.0"},
        multi={"Classifier": ["License :: OSI Approved :: MIT License"]},
    )
    _patch_metadata(monkeypatch, md)
    # resolver DEFAULTED (None) -> the real policy-aware metadata path runs.
    report = scan(lock, real_policy())
    assert not report.ok
    pkg = next(p for p in report.packages if p.name == "masked-agpl")
    assert pkg.classification == BANNED
    assert any("masked-agpl" in v for v in report.violations)
    # An out-of-process override cannot whitelist the banned class.
    policy = dataclasses.replace(
        real_policy(),
        overrides={
            ("masked-agpl", "1.0.0"): _override("out-of-process-approved", name="masked-agpl")
        },
    )
    report2 = scan(lock, policy)
    assert any("masked-agpl" in e and "never overridable" in e for e in report2.policy_errors)


# --------------------------------------------------------------------------- #
# P3(b): PEP 503 canonicalization of override / known lookup keys.
# --------------------------------------------------------------------------- #


def test_override_name_pep503_canonicalized_via_policy_load(tmp_path: Path) -> None:
    base = default_policy_path(REPO_ROOT).read_text(encoding="utf-8")
    override_block = (
        "\n[[overrides]]\n"
        'name = "Some_LGPL.Dep"\n'
        'version = "1.0.0"\n'
        'class = "case-by-case-approved"\n'
        'justification = "dynamically linked LGPL shared lib"\n'
        'approved_by = "owner@example.com"\n'
        'date = "2026-07-03"\n'
    )
    policy_file = tmp_path / "policy.toml"
    policy_file.write_text(base + override_block, encoding="utf-8")
    policy = Policy.load(policy_file)
    lock = write_lock(tmp_path, [{"name": "some-lgpl-dep"}])
    report = scan(lock, policy, resolver=resolver_from({"some-lgpl-dep": "LGPL-2.1-or-later"}))
    assert report.ok, report.violations + report.policy_errors


def test_known_lookup_canonicalized() -> None:
    policy = dataclasses.replace(real_policy(), known={"foo-bar": "MIT"})
    assert resolve_license("Foo_Bar", policy, resolver=lambda n: None) == "MIT"


# --------------------------------------------------------------------------- #
# NE-CKA3: raw License-field banned dominance (any length / multi-line).
# --------------------------------------------------------------------------- #


def test_long_form_banned_license_field_dominates_permissive_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    md = _FakeMetadata(
        single={"License": "GNU Affero General Public License v3"},
        multi={"Classifier": ["License :: OSI Approved :: MIT License"]},
    )
    _patch_metadata(monkeypatch, md)
    result = resolve_license_from_metadata("dep", real_policy())
    assert result == "GNU Affero General Public License v3"
    assert classify_license(result or "", real_policy()) == BANNED


def test_multiline_banned_license_field_dominates_permissive_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    md = _FakeMetadata(
        single={"License": "GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3, 19 November 2007"},
        multi={"Classifier": ["License :: OSI Approved :: MIT License"]},
    )
    _patch_metadata(monkeypatch, md)
    result = resolve_license_from_metadata("dep", real_policy())
    assert classify_license(result or "", real_policy()) == BANNED


def test_long_form_banned_field_dep_fails_gate_and_is_non_overridable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = write_lock(tmp_path, [{"name": "masked-agpl-long"}])
    md = _FakeMetadata(
        single={"License": "GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3, 19 November 2007"},
        multi={"Classifier": ["License :: OSI Approved :: MIT License"]},
    )
    _patch_metadata(monkeypatch, md)
    report = scan(lock, real_policy())
    assert not report.ok
    pkg = next(p for p in report.packages if p.name == "masked-agpl-long")
    assert pkg.classification == BANNED
    policy = dataclasses.replace(
        real_policy(),
        overrides={
            ("masked-agpl-long", "1.0.0"): _override(
                "out-of-process-approved", name="masked-agpl-long"
            )
        },
    )
    report2 = scan(lock, policy)
    assert any("masked-agpl-long" in e and "never overridable" in e for e in report2.policy_errors)


def test_long_permissive_license_field_precedence_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    md = _FakeMetadata(
        single={"License": "MIT License with additional permissions granted here"},
        multi={"Classifier": ["License :: OSI Approved :: Apache Software License"]},
    )
    _patch_metadata(monkeypatch, md)
    # Classifier precedence intact; the long non-banned field stays ignored for
    # the positive result (the fix must not widen FREE resolution).
    assert resolve_license_from_metadata("dep", real_policy()) == "Apache-2.0"


# --------------------------------------------------------------------------- #
# NE-CKA3: bind license resolution to the LOCKED version (fail closed).
# --------------------------------------------------------------------------- #


def test_lock_env_version_mismatch_fails_closed(tmp_path: Path) -> None:
    # click is a direct ne-cli dep, always installed, never version 0.0.0.
    lock = write_lock(tmp_path, [{"name": "click", "version": "0.0.0"}])
    report = scan(lock, real_policy(), resolver=resolver_from({"click": "BSD-3-Clause"}))
    assert not report.ok
    assert any("verified against wrong version" in v for v in report.violations)
    pkg = report.packages[0]
    assert pkg.allowed is False
    assert pkg.license == "UNKNOWN"


def test_lock_env_version_match_passes(tmp_path: Path) -> None:
    installed = importlib_metadata.version("click")
    lock = write_lock(tmp_path, [{"name": "click", "version": installed}])
    report = scan(lock, real_policy(), resolver=resolver_from({"click": "BSD-3-Clause"}))
    assert report.ok, report.violations + report.policy_errors


def test_uninstalled_dist_skips_version_binding(tmp_path: Path) -> None:
    lock = write_lock(tmp_path, [{"name": "ghost-dep"}])
    report = scan(lock, real_policy(), resolver=resolver_from({"ghost-dep": "MIT"}))
    assert report.ok, report.violations + report.policy_errors


# --------------------------------------------------------------------------- #
# [known] rescue for resolved-but-UNKNOWN (NE-CKB1)
# --------------------------------------------------------------------------- #


def test_known_rescues_resolved_unknown_license(tmp_path: Path) -> None:
    """A present-but-non-SPDX license string (e.g. protobuf's PyPI '3-Clause BSD
    License') that classifies UNKNOWN is rescued by a matching [known] entry."""
    policy = dataclasses.replace(real_policy(), known={"somedep": "BSD-3-Clause"})
    resolver = resolver_from({"somedep": "3-Clause BSD License"})
    assert classify_license("3-Clause BSD License", policy) == UNKNOWN
    resolved = resolve_license("somedep", policy, resolver=resolver)
    assert resolved == "BSD-3-Clause"
    assert classify_license(resolved, policy) == FREE

    lock = write_lock(tmp_path, [{"name": "somedep"}])
    report = scan(lock, policy, resolver=resolver)
    assert report.ok, report.violations + report.policy_errors


def test_known_cannot_rescue_banned_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """BANNED is non-overridable: a raw AGPL License field dominates a permissive
    classifier BEFORE any [known] rescue could apply, and a [known] entry for the
    same package cannot mask it."""
    md = _FakeMetadata(
        single={"License": "AGPL-3.0"},
        multi={"Classifier": ["License :: OSI Approved :: MIT License"]},
    )
    _patch_metadata(monkeypatch, md)
    policy = dataclasses.replace(real_policy(), known={"dep": "MIT"})
    resolved = resolve_license("dep", policy)
    assert resolved == "AGPL-3.0"
    assert classify_license(resolved or "", policy) == BANNED

    lock = write_lock(tmp_path, [{"name": "dep"}])
    report = scan(lock, policy)
    assert not report.ok
    assert any("BANNED class (non-overridable)" in v for v in report.violations)


def test_known_does_not_rescue_lgpl(tmp_path: Path) -> None:
    """The rescue is keyed strictly on UNKNOWN — it never fires for LGPL."""
    policy = dataclasses.replace(real_policy(), known={"somedep": "MIT"})
    resolver = resolver_from({"somedep": "LGPL-2.1"})
    resolved = resolve_license("somedep", policy, resolver=resolver)
    assert resolved == "LGPL-2.1"
    assert classify_license(resolved, policy) == LGPL_NEEDS_APPROVAL

    lock = write_lock(tmp_path, [{"name": "somedep"}])
    report = scan(lock, policy, resolver=resolver)
    assert not report.ok
    assert any("case-by-case" in v.lower() or "LGPL" in v for v in report.violations)


def test_known_value_classifying_banned_still_fails(tmp_path: Path) -> None:
    """A [known] value that itself classifies BANNED is not an escape hatch: it
    still fails via classify_license in scan (absent-metadata fallback path)."""
    policy = dataclasses.replace(real_policy(), known={"somedep": "AGPL-3.0"})
    resolved = resolve_license("somedep", policy, resolver=resolver_from({}))
    assert resolved == "AGPL-3.0"
    assert classify_license(resolved, policy) == BANNED

    lock = write_lock(tmp_path, [{"name": "somedep"}])
    report = scan(lock, policy, resolver=resolver_from({}))
    assert not report.ok
    assert any("somedep" in v for v in report.violations)
    assert not any("wrong version" in v for v in report.violations)
