# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-ONTOLOGY-FLYWHEEL: the boot-dir + governed-store overlay registry loader."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ne.brain_ontology.verified.errors import TemplateRegistryError
from ne.brain_ontology.verified.store import StoredVerifiedTemplate
from ne.brain_ontology.verified.templates import (
    load_template_registry,
    load_template_registry_with_overlay,
)


def _template(template_id: str, version: int, description: str) -> str:
    return f'''template_id = "{template_id}"
version = {version}
description = "{description}"
sparql = """
SELECT ?account ?name WHERE {{
  #BINDINGS#
  ?account <https://x/name> ?name .
}}
"""
[scope]
graph_classes = ["alpha"]
result_shape = "enumerable"
[answer]
columns = ["account", "name"]
citation_var = "account"
'''


def _row(
    template_id: str, version: int, toml_text: str, *, ident: int = 1
) -> StoredVerifiedTemplate:
    return StoredVerifiedTemplate(
        id=ident,
        template_id=template_id,
        version=version,
        template_toml=toml_text,
        toml_sha256=hashlib.sha256(toml_text.encode()).hexdigest(),
        proposal_id=f"sha256:p{ident}",
        actor="user:steward",
        config_hash="c" * 64,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def boot_dir(tmp_path: Path) -> Path:
    d = tmp_path / "boot"
    d.mkdir()
    (d / "accounts.toml").write_text(_template("accounts-directory", 1, "boot v1"))
    (d / "entity.toml").write_text(_template("entity-by-name", 1, "boot v1"))
    return d


def test_zero_rows_is_byte_identical_to_boot_only(boot_dir: Path) -> None:
    base = load_template_registry(boot_dir)
    overlaid = load_template_registry_with_overlay(boot_dir, [])
    # The parity anchor: an EMPTY governed store leaves the fingerprint (registry
    # hash AND the template_ids tuple) byte-identical to the boot-dir-only load.
    assert overlaid.fingerprint == base.fingerprint
    assert overlaid.fingerprint.registry_hash == base.fingerprint.registry_hash


def test_higher_version_shadows_boot(boot_dir: Path) -> None:
    base = load_template_registry(boot_dir)
    v2 = _template("accounts-directory", 2, "governed v2")
    overlaid = load_template_registry_with_overlay(boot_dir, [_row("accounts-directory", 2, v2)])
    # The effective registry serves v2 (highest wins) at the boot-order position.
    served = {t.template_id: t.version for t in overlaid.templates}
    assert served["accounts-directory"] == 2
    assert served["entity-by-name"] == 1
    assert overlaid.fingerprint.registry_hash != base.fingerprint.registry_hash


def test_new_template_id_extends_the_set(boot_dir: Path) -> None:
    base = load_template_registry(boot_dir)
    new = _template("orders-by-date", 1, "governed new")
    overlaid = load_template_registry_with_overlay(boot_dir, [_row("orders-by-date", 1, new)])
    served = {t.template_id for t in overlaid.templates}
    assert "orders-by-date" in served
    assert len(overlaid.templates) == len(base.templates) + 1
    assert overlaid.fingerprint.registry_hash != base.fingerprint.registry_hash


def test_boot_pair_divergence_is_fail_loud(boot_dir: Path) -> None:
    # A stored row at the SAME (id, version) as boot but a DIFFERENT body (sha)
    # is a fail-loud divergence — one body per slot.
    diverged = _template("accounts-directory", 1, "TAMPERED v1")
    with pytest.raises(TemplateRegistryError, match="diverges from the boot bundle"):
        load_template_registry_with_overlay(boot_dir, [_row("accounts-directory", 1, diverged)])


def test_identical_boot_pair_is_ignored(boot_dir: Path) -> None:
    # A stored row byte-identical to a boot template (same sha) is ignored (boot
    # wins) — the fingerprint stays the boot-only fingerprint.
    identical = (boot_dir / "accounts.toml").read_text()
    base = load_template_registry(boot_dir)
    overlaid = load_template_registry_with_overlay(
        boot_dir, [_row("accounts-directory", 1, identical)]
    )
    assert overlaid.fingerprint == base.fingerprint


def test_invalid_stored_toml_fails_whole_load(boot_dir: Path) -> None:
    # A tampered store row naming a banned keyword (SERVICE) fails the WHOLE load
    # fail-closed — a weaker registry can never boot.
    bad = _template("orders-by-date", 1, "x").replace(
        "?account <https://x/name> ?name .",
        "SERVICE <https://evil/sparql> { ?account <https://x/name> ?name . }",
    )
    with pytest.raises(TemplateRegistryError):
        load_template_registry_with_overlay(boot_dir, [_row("orders-by-date", 1, bad)])


def test_stored_metadata_must_match_parsed_toml(boot_dir: Path) -> None:
    # A row whose declared (template_id, version) disagrees with its parsed TOML
    # is refused (a tampered metadata guard).
    body = _template("orders-by-date", 1, "x")
    tampered = StoredVerifiedTemplate(
        id=9,
        template_id="orders-by-date",
        version=5,  # disagrees with the parsed version (1)
        template_toml=body,
        toml_sha256=hashlib.sha256(body.encode()).hexdigest(),
        proposal_id="sha256:p9",
        actor="user:steward",
        config_hash="c" * 64,
        created_at=datetime.now(UTC),
    )
    with pytest.raises(TemplateRegistryError, match="disagrees with its parsed TOML"):
        load_template_registry_with_overlay(boot_dir, [tampered])
