# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the per-customer pilot config bundle (`ne pilot`, NE-070).

Covers the composed NE-CKC12 config_hash, byte-identical reproducibility,
tamper detection, config_hash parity with the composition-root freeze (the SAME
hash stamped on AuditEvents), and C12 (no secret value ever lands in a bundle).
"""

from __future__ import annotations

import asyncio
import json
import re
import stat
from collections.abc import Iterator
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from ne.cli import main
from ne.cli.pilotcfg import (
    _MODULE_KEYS,
    OPTIONAL_SECRETS,
    REQUIRED_SECRETS,
    resolve_optional_secrets,
)
from ne.secrets import FileSecretStore

#: A base64url KEK sentinel distinct from the password sentinel.
_KEK_SENTINEL = "S0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0s"
_CONTINUATION_SENTINEL = "Q09OVElOVUFUSU9OQ09OVElOVUFUSU9OQ09OVElOVUE"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture
def reset_config_hash() -> Iterator[None]:
    from ne.contracts import reset_config_hash_for_testing

    reset_config_hash_for_testing()
    try:
        yield
    finally:
        reset_config_hash_for_testing()


def _promote(out: Path, *, hostname: str = "localhost", secrets_config: str | None = None) -> None:
    args = ["pilot", "promote", "--customer", "acme", "--hostname", hostname, "--out", str(out)]
    if secrets_config is not None:
        args += ["--secrets-config", secrets_config]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output


def _manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def test_promote_writes_64hex_config_hash(tmp_path) -> None:
    out = tmp_path / "b"
    _promote(out)
    manifest = _manifest(out)
    assert _HEX64.match(manifest["config_hash"])
    assert manifest["schema_version"] == 1
    assert manifest["customer"] == "acme"
    assert manifest["hostname"] == "localhost"
    # required.json carries NAMES only.
    required = json.loads((out / "secrets" / "required.json").read_text())
    assert required == REQUIRED_SECRETS


def test_promote_writes_default_ne_rebac_toml(tmp_path) -> None:
    # NE-087 (O-4): promote seals a comment-only default config/ne-rebac.toml (ReBAC
    # off) so a plain bundle carries the file; it is recorded in the manifest.
    out = tmp_path / "b"
    _promote(out)
    rebac = out / "config" / "ne-rebac.toml"
    assert rebac.is_file()
    assert "store_id" in rebac.read_text(encoding="utf-8")
    assert "config/ne-rebac.toml" in _manifest(out)["files"]


def test_promote_rebac_config_copied_verbatim_and_reproducible(tmp_path) -> None:
    src = tmp_path / "ne-rebac.toml"
    body = 'enabled = true\napi_url = "http://openfga:8080"\nstore_id = "01S"\n'
    src.write_text(body, encoding="utf-8")

    a, b = tmp_path / "a", tmp_path / "b"
    for out in (a, b):
        result = CliRunner().invoke(
            main,
            [
                "pilot",
                "promote",
                "--customer",
                "acme",
                "--hostname",
                "localhost",
                "--out",
                str(out),
                "--rebac-config",
                str(src),
            ],
        )
        assert result.exit_code == 0, result.output
    # Copied verbatim (byte-for-byte) and byte-reproducible across promotions.
    assert (a / "config" / "ne-rebac.toml").read_text(encoding="utf-8") == body
    assert (a / "manifest.json").read_bytes() == (b / "manifest.json").read_bytes()


def test_promote_is_byte_reproducible(tmp_path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _promote(a)
    _promote(b)
    # Same inputs → byte-identical manifest AND equal config_hash (no timestamp).
    assert (a / "manifest.json").read_bytes() == (b / "manifest.json").read_bytes()
    assert _manifest(a)["config_hash"] == _manifest(b)["config_hash"]


def test_verify_passes_intact_bundle(tmp_path) -> None:
    out = tmp_path / "b"
    _promote(out)
    result = CliRunner().invoke(main, ["pilot", "verify", str(out)])
    assert result.exit_code == 0, result.output
    assert "bundle OK" in result.output


def test_verify_detects_tampered_config_file(tmp_path) -> None:
    out = tmp_path / "b"
    _promote(out)
    # Flip a byte in a config file WITHOUT updating the manifest.
    runtime_toml = out / "config" / "ne-runtime.toml"
    runtime_toml.write_text(runtime_toml.read_text() + "max_requests = 999\n", encoding="utf-8")
    result = CliRunner().invoke(main, ["pilot", "verify", str(out)])
    assert result.exit_code != 0
    assert "ne-runtime.toml" in result.output


def test_verify_detects_edited_manifest_field(tmp_path) -> None:
    out = tmp_path / "b"
    _promote(out)
    manifest = _manifest(out)
    manifest["config_hash"] = "0" * 64  # forge the hash
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = CliRunner().invoke(main, ["pilot", "verify", str(out)])
    assert result.exit_code != 0
    assert "config_hash mismatch" in result.output


def test_verify_detects_unrecorded_file(tmp_path) -> None:
    out = tmp_path / "b"
    _promote(out)
    (out / "config" / "sneaky.toml").write_text("x = 1\n", encoding="utf-8")
    result = CliRunner().invoke(main, ["pilot", "verify", str(out)])
    assert result.exit_code != 0
    assert "unrecorded file" in result.output


def test_config_hash_parity_with_composition_root(tmp_path, reset_config_hash) -> None:
    # The bundle's config_hash MUST equal ne.runtime.factory.freeze_platform_config's
    # default composed hash — the SAME hash stamped on every AuditEvent (NE-CKC12).
    from ne.cli.fingerprint import _scrubbed_determinism_guard
    from ne.runtime.factory import freeze_platform_config

    with _scrubbed_determinism_guard():
        frozen = freeze_platform_config()

    assert set(_MODULE_KEYS) == {"ne-runtime", "ne-govern"}
    out = tmp_path / "b"
    _promote(out)
    assert _manifest(out)["config_hash"] == frozen


def test_runtime_env_excluded_from_manifest_and_verify(tmp_path) -> None:
    # P1 regression: the installer's ephemeral runtime/pilot.env (written by
    # `ne up` AFTER promote) must NOT be recorded in the manifest nor flagged by
    # verify — otherwise a brought-up bundle fails verify and a second `ne up`
    # (which verifies the bundle) breaks idempotency with a false tamper error.
    out = tmp_path / "b"
    _promote(out)
    # No runtime/ entry recorded at promote time.
    assert not any(rel.startswith("runtime/") for rel in _manifest(out)["files"])

    # Simulate a bring-up leaving a plaintext env behind.
    runtime = out / "runtime"
    runtime.mkdir()
    (runtime / "pilot.env").write_text("NE_PILOT_POSTGRES_PASSWORD=hunter2\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["pilot", "verify", str(out)])
    assert result.exit_code == 0, result.output
    assert "bundle OK" in result.output


def test_no_secret_value_ever_lands_in_a_bundle(tmp_path) -> None:
    # Plant a sentinel secret VALUE in a FileSecretStore, promote a bundle that
    # references it, and prove the value appears in NO bundle file.
    sentinel = "S3NT1NEL-never-in-a-bundle"
    secrets_json = tmp_path / "secrets.ne.json"
    store = FileSecretStore(secrets_json)

    async def _seed() -> None:
        await store.write("pilot/postgres", {"password": sentinel})
        await store.write("pilot/keycloak", {"admin_password": sentinel})
        await store.write("pilot/openfga", {"api_token": sentinel})
        # PRE-009A (A2): the private-payload KEK gets its OWN sentinel, so this
        # sweep genuinely covers the key material and not only the passwords.
        await store.write(
            "pilot/agent-run-private-payload-kek", {"key_id": "k1", "kek": _KEK_SENTINEL}
        )
        await store.write(
            "pilot/source-authority-continuation",
            {"key_id": "sa-k1", "hmac_key_b64": _CONTINUATION_SENTINEL},
        )

    asyncio.run(_seed())
    sec_toml = tmp_path / "ne-secrets.toml"
    sec_toml.write_text(f'[file]\npath = "{secrets_json}"\nmode = "plain"\n', encoding="utf-8")

    out = tmp_path / "b"
    _promote(out, secrets_config=str(sec_toml))

    for path in out.rglob("*"):
        if path.is_file():
            body = path.read_text(encoding="utf-8", errors="ignore")
            assert sentinel not in body, path
            assert _KEK_SENTINEL not in body, path


# --- manifest integrity (NE-070-FOLLOWUP fix 1) ------------------------------
def test_verify_fails_on_tampered_manifest_hostname(tmp_path) -> None:
    out = tmp_path / "b"
    _promote(out)
    m = _manifest(out)
    m["hostname"] = "evil.example.com"
    (out / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    result = CliRunner().invoke(main, ["pilot", "verify", str(out)])
    assert result.exit_code != 0
    assert "manifest integrity" in result.output


def test_verify_fails_on_tampered_manifest_customer(tmp_path) -> None:
    out = tmp_path / "b"
    _promote(out)
    m = _manifest(out)
    m["customer"] = "mallory"
    (out / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    result = CliRunner().invoke(main, ["pilot", "verify", str(out)])
    assert result.exit_code != 0
    assert "manifest integrity" in result.output


def test_verify_fails_on_missing_manifest_hash(tmp_path) -> None:
    out = tmp_path / "b"
    _promote(out)
    m = _manifest(out)
    del m["manifest_hash"]
    (out / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    result = CliRunner().invoke(main, ["pilot", "verify", str(out)])
    assert result.exit_code != 0
    assert "manifest_hash" in result.output


def test_verify_fails_on_forged_manifest_hash(tmp_path) -> None:
    out = tmp_path / "b"
    _promote(out)
    m = _manifest(out)
    m["manifest_hash"] = "0" * 64
    (out / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    result = CliRunner().invoke(main, ["pilot", "verify", str(out)])
    assert result.exit_code != 0
    assert "manifest integrity" in result.output


def test_manifest_hash_is_64hex_and_reproducible(tmp_path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _promote(a)
    _promote(b)
    assert _HEX64.match(_manifest(a)["manifest_hash"])
    assert _manifest(a)["manifest_hash"] == _manifest(b)["manifest_hash"]


def test_env_generation_resolves_sentinel_into_0600_env_under_bundle(tmp_path) -> None:
    from ne.cli.up import _write_runtime_env

    sentinel = "S3NT1NEL-only-in-runtime-env"
    secrets_json = tmp_path / "secrets.ne.json"
    store = FileSecretStore(secrets_json)

    async def _seed() -> None:
        await store.write("pilot/postgres", {"password": sentinel})
        await store.write("pilot/keycloak", {"admin_password": sentinel})
        await store.write("pilot/openfga", {"api_token": sentinel})
        await store.write(
            "pilot/agent-run-private-payload-kek", {"key_id": "k1", "kek": _KEK_SENTINEL}
        )
        await store.write(
            "pilot/source-authority-continuation",
            {"key_id": "sa-k1", "hmac_key_b64": _CONTINUATION_SENTINEL},
        )

    asyncio.run(_seed())
    sec_toml = tmp_path / "ne-secrets.toml"
    sec_toml.write_text(f'[file]\npath = "{secrets_json}"\nmode = "plain"\n', encoding="utf-8")
    out = tmp_path / "b"
    _promote(out, secrets_config=str(sec_toml))

    env_path = _write_runtime_env(out)
    # The ONLY place the value is resolved to; 0600; inside the bundle, never deploy/.
    assert env_path == out / "runtime" / "pilot.env"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert sentinel in env_path.read_text()
    assert "deploy" not in env_path.parts


# --- NE-BEW-INJECT: optional Entra broker secrets ----------------------------

_ENTRA_TENANT = "ETtenant-sentinel-aaa"
_ENTRA_CLIENT = "ETclient-sentinel-bbb"
_ENTRA_SECRET = "ETsecret-sentinel-ccc"


def _entra_bundle(tmp_path: Path, *, entra_fields: dict[str, str] | None) -> Path:
    """A promoted bundle whose SecretStore carries the three REQUIRED secrets
    plus (optionally) a `pilot/entra-broker` path with the given field set."""
    secrets_json = tmp_path / "secrets.ne.json"
    store = FileSecretStore(secrets_json)

    async def _seed() -> None:
        await store.write("pilot/postgres", {"password": "pg"})
        await store.write("pilot/keycloak", {"admin_password": "kc"})
        await store.write("pilot/openfga", {"api_token": "of"})
        if entra_fields is not None:
            await store.write("pilot/entra-broker", entra_fields)

    asyncio.run(_seed())
    sec_toml = tmp_path / "ne-secrets.toml"
    sec_toml.write_text(f'[file]\npath = "{secrets_json}"\nmode = "plain"\n', encoding="utf-8")
    out = tmp_path / "bundle"
    _promote(out, secrets_config=str(sec_toml))
    return out


def test_optional_secrets_are_names_only_and_never_in_required_json(tmp_path) -> None:
    assert set(OPTIONAL_SECRETS) == {
        "NE_PILOT_ENTRA_TENANT_ID",
        "NE_PILOT_ENTRA_BROKER_CLIENT_ID",
        "NE_PILOT_ENTRA_BROKER_CLIENT_SECRET",
    }
    for name, spec in OPTIONAL_SECRETS.items():
        assert spec["path"] == "pilot/entra-broker"
        field = name.removeprefix("NE_PILOT_ENTRA_").removeprefix("BROKER_").lower()
        assert spec["field"] == field
    # A fresh promote's required.json must carry NONE of the optional names —
    # the required-union footgun (pilotcfg.py's {**REQUIRED_SECRETS, **from_bundle})
    # would otherwise silently promote them to mandatory and make an ordinary
    # Entra-less deployment fail `ne up`.
    out = tmp_path / "b"
    _promote(out)
    required = json.loads((out / "secrets" / "required.json").read_text())
    for name in OPTIONAL_SECRETS:
        assert name not in required


def test_resolve_optional_secrets_absent_path_returns_empty(tmp_path) -> None:
    bundle = _entra_bundle(tmp_path, entra_fields=None)  # no pilot/entra-broker path at all
    assert resolve_optional_secrets(bundle) == {}


def test_resolve_optional_secrets_resolves_all_three(tmp_path) -> None:
    bundle = _entra_bundle(
        tmp_path,
        entra_fields={
            "tenant_id": _ENTRA_TENANT,
            "client_id": _ENTRA_CLIENT,
            "client_secret": _ENTRA_SECRET,
        },
    )
    resolved = resolve_optional_secrets(bundle)
    assert resolved == {
        "NE_PILOT_ENTRA_TENANT_ID": _ENTRA_TENANT,
        "NE_PILOT_ENTRA_BROKER_CLIENT_ID": _ENTRA_CLIENT,
        "NE_PILOT_ENTRA_BROKER_CLIENT_SECRET": _ENTRA_SECRET,
    }


def test_resolve_optional_secrets_missing_field_names_only(tmp_path) -> None:
    bundle = _entra_bundle(tmp_path, entra_fields={"tenant_id": _ENTRA_TENANT})
    with pytest.raises(click.ClickException) as exc_info:
        resolve_optional_secrets(bundle)
    message = str(exc_info.value)
    assert "NE_PILOT_ENTRA_BROKER_CLIENT_ID" in message
    assert "NE_PILOT_ENTRA_BROKER_CLIENT_SECRET" in message
    assert "NE_PILOT_ENTRA_TENANT_ID" not in message  # the one field that WAS present
    assert _ENTRA_TENANT not in message


def test_resolve_optional_secrets_empty_string_field_treated_as_missing(tmp_path) -> None:
    # P3-1: the Terraform-created `${prefix}-entra-broker-client-secret`
    # populates ONLY client_secret; an operator following the naive path could
    # write tenant_id/client_id from elsewhere and leave one field an empty
    # string rather than truly absent. Empty-string must fail exactly like a
    # missing field — never resolve silently as "" (indistinguishable from no
    # broker at the compose layer, since compose ALSO omits it).
    bundle = _entra_bundle(
        tmp_path,
        entra_fields={
            "tenant_id": _ENTRA_TENANT,
            "client_id": _ENTRA_CLIENT,
            "client_secret": "",
        },
    )
    with pytest.raises(click.ClickException) as exc_info:
        resolve_optional_secrets(bundle)
    message = str(exc_info.value)
    assert "NE_PILOT_ENTRA_BROKER_CLIENT_SECRET" in message
    assert "NE_PILOT_ENTRA_TENANT_ID" not in message  # present and non-empty
    assert "NE_PILOT_ENTRA_BROKER_CLIENT_ID" not in message  # present and non-empty


def test_resolve_optional_secrets_reads_path_once(tmp_path, monkeypatch) -> None:
    # P3-3: resolving all three OPTIONAL_SECRETS fields must cost exactly ONE
    # SecretStore.read (they all share the "pilot/entra-broker" path) — not
    # three, which would be three round trips (and, for a remote adapter like
    # OpenBao, three separate client/auth handshakes) on every `ne up`.
    import ne.secrets as ne_secrets

    bundle = _entra_bundle(
        tmp_path,
        entra_fields={
            "tenant_id": _ENTRA_TENANT,
            "client_id": _ENTRA_CLIENT,
            "client_secret": _ENTRA_SECRET,
        },
    )
    real_settings = ne_secrets.SecretsSettings.from_config_file(
        bundle / "config" / "ne-secrets.toml"
    )
    real_store = ne_secrets.create_secret_store(real_settings)
    counter = {"reads": 0}

    class _CountingStore:
        async def read(self, path: str, *, version: int | None = None):
            counter["reads"] += 1
            return await real_store.read(path, version=version)

    monkeypatch.setattr(
        ne_secrets, "create_secret_store", lambda settings, registry=None: _CountingStore()
    )

    resolved = resolve_optional_secrets(bundle)
    assert counter["reads"] == 1
    assert resolved == {
        "NE_PILOT_ENTRA_TENANT_ID": _ENTRA_TENANT,
        "NE_PILOT_ENTRA_BROKER_CLIENT_ID": _ENTRA_CLIENT,
        "NE_PILOT_ENTRA_BROKER_CLIENT_SECRET": _ENTRA_SECRET,
    }
