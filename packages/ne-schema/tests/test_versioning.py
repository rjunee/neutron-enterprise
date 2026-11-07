# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Schema-versioning-in-git discipline: YAML version == manifest == JSON Schema
== CHANGELOG, and the manifest records the exact source sha256 and linkml pin."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ne.schema.generated import _events_manifest, _manifest

_PKG = Path(__file__).resolve().parents[1]
_SCHEMA = _PKG / "src" / "ne" / "schema" / "schemas" / "operational.yaml"
_JSON_SCHEMA = _PKG / "src" / "ne" / "schema" / "generated" / "operational.schema.json"
_EVENTS_SCHEMA = _PKG / "src" / "ne" / "schema" / "schemas" / "events.yaml"
_EVENTS_JSON_SCHEMA = _PKG / "src" / "ne" / "schema" / "generated" / "events.schema.json"
_CHANGELOG = _PKG / "CHANGELOG.md"
_REQUIREMENTS_IN = _PKG / "codegen" / "requirements.in"


def _version_of(path: Path, label: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"(?m)^version:\s*(\S+)\s*$", text)
    assert match is not None, f"{label} must carry a top-level `version:`"
    return match.group(1)


def _yaml_version() -> str:
    return _version_of(_SCHEMA, "operational.yaml")


def test_version_agreement_across_artifacts() -> None:
    version = _yaml_version()
    assert version == "1.1.0"
    assert version == _manifest.SCHEMA_VERSION
    json_schema = json.loads(_JSON_SCHEMA.read_text(encoding="utf-8"))
    assert json_schema["version"] == version


def test_changelog_has_entry_for_version() -> None:
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    assert f"## {_yaml_version()}" in changelog


def test_manifest_source_sha256_matches_committed_yaml() -> None:
    expected = hashlib.sha256(_SCHEMA.read_bytes()).hexdigest()
    assert expected == _manifest.SOURCE_SHA256


def test_manifest_linkml_version_matches_pin() -> None:
    pin_text = _REQUIREMENTS_IN.read_text(encoding="utf-8")
    match = re.search(r"(?m)^linkml==(\S+)\s*$", pin_text)
    assert match is not None, "requirements.in must pin linkml=="
    assert match.group(1) == _manifest.LINKML_VERSION


# --- events schema: the same versioning-in-git discipline, second source -----


def _events_yaml_version() -> str:
    return _version_of(_EVENTS_SCHEMA, "events.yaml")


def test_events_version_agreement_across_artifacts() -> None:
    version = _events_yaml_version()
    assert version == "1.1.0"
    assert version == _events_manifest.SCHEMA_VERSION
    json_schema = json.loads(_EVENTS_JSON_SCHEMA.read_text(encoding="utf-8"))
    assert json_schema["version"] == version


def test_events_changelog_has_entry_for_version() -> None:
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    assert f"## events {_events_yaml_version()}" in changelog


def test_events_manifest_source_sha256_matches_committed_yaml() -> None:
    expected = hashlib.sha256(_EVENTS_SCHEMA.read_bytes()).hexdigest()
    assert expected == _events_manifest.SOURCE_SHA256


def test_events_manifest_linkml_version_matches_pin() -> None:
    pin_text = _REQUIREMENTS_IN.read_text(encoding="utf-8")
    match = re.search(r"(?m)^linkml==(\S+)\s*$", pin_text)
    assert match is not None, "requirements.in must pin linkml=="
    assert match.group(1) == _events_manifest.LINKML_VERSION
