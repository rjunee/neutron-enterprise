# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""`ne pilot` — the per-customer pilot config bundle (NE-070).

A bundle parameterizes a hardened pilot (deploy/compose/pilot) for one customer.
It is reproducible (same inputs → identical ``config_hash`` and byte-identical
``manifest.json``) and carries secret NAMES/paths only — never secret values.

Layout::

    <bundle>/
      config/ne-runtime.toml     # module settings (empty = platform defaults)
      config/ne-govern.toml
      config/ne-secrets.toml      # selects the SecretStore adapter (sops-file)
      secrets/required.json       # env var -> {path, field} (NAMES only)
      manifest.json               # {schema_version, customer, hostname,
                                  #  config_hash, files:{relpath:sha256},
                                  #  manifest_hash}

The ``manifest_hash`` is a sha256 over the canonical JSON of every manifest field
EXCEPT itself — it brings ``customer``/``hostname`` (which ``ne up`` TRUSTS for
``KC_HOSTNAME`` + the Caddy TLS vhost) and the ``config_hash``/``files`` records
under one tamper-evidence check. ``ne pilot verify`` is FAIL-CLOSED on it: a
manifest with no ``manifest_hash`` (a pre-hardening bundle, or one with the field
stripped) is REJECTED and must be re-promoted — grandfathering would reopen the
hostname-tamper hole.

``config_hash`` is the COMPOSED NE-CKC12 platform hash over exactly the module
set ``ne.runtime.factory.freeze_platform_config`` freezes (``ne-runtime`` +
``ne-govern``) — the SAME hash stamped on every ``AuditEvent`` — computed under
:func:`ne.cli.fingerprint._scrubbed_determinism_guard` so ambient ``NE_*`` env /
cwd ``ne.toml`` cannot leak into a customer's bundle identity. ``ne-secrets.toml``
is carried for the installer's SecretStore but is deliberately NOT in the
config_hash preimage (it selects a backend, it is not platform config).

**Dev→pilot promotion** = pointing ``--runtime-config`` / ``--govern-config`` at a
dev deployment's per-module TOMLs; because ``RuntimeSettings`` / ``GovernSettings``
are ``extra='forbid'`` they cannot share one namespaced ``ne.toml``, so the bundle
keeps one file per module (resolving the SPEC config-namespacing note for NE-070).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import click

# Env var -> SecretStore {path, field}. NAMES/paths ONLY — never a value (C12).
REQUIRED_SECRETS: dict[str, dict[str, str]] = {
    "NE_PILOT_POSTGRES_PASSWORD": {"path": "pilot/postgres", "field": "password"},
    "NE_PILOT_KEYCLOAK_ADMIN_PASSWORD": {"path": "pilot/keycloak", "field": "admin_password"},
    # NE-087 (O-4): the OpenFGA preshared key. ALWAYS required — compose interpolates
    # the ${NE_PILOT_OPENFGA_API_TOKEN:?} on the openfga service at PARSE time
    # regardless of the selected profiles, so the secret must be populated even for
    # a plain (non-rebac) bring-up. Pre-087 bundles surface the missing NAME loudly
    # (resolution union), never a cryptic compose interpolation error.
    "NE_PILOT_OPENFGA_API_TOKEN": {"path": "pilot/openfga", "field": "api_token"},
    # PRE-009A (A2): the Agent Run private-payload KEK and its key id. NAMES/paths
    # only, never a value (C12) -- the material lives solely at
    # `pilot/agent-run-private-payload-kek` and is provisioned by
    # `ne private-payload-keys provision`.
    #
    # ALWAYS required, following the NE-087 OpenFGA precedent above: a bundle
    # promoted before A2 has neither name in its recorded required.json, and the
    # resolution UNION below (`{**REQUIRED_SECRETS, **from_bundle}`) makes such a
    # bundle fail `ne up` with the missing-secret NAME until its store is
    # populated. That is the intended loud failure, not a regression -- an
    # unprovisioned deployment must not start a runtime that cannot open its own
    # private rows.
    "NE_AGENT_PRIVATE_PAYLOAD_KEY_ID": {
        "path": "pilot/agent-run-private-payload-kek",
        "field": "key_id",
    },
    "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64": {
        "path": "pilot/agent-run-private-payload-kek",
        "field": "kek",
    },
    # 015A0-AUTHORITY-CORE: the source-authority continuation HMAC key and its key
    # id. NAMES/paths only, never a value (C12) -- the material lives solely at
    # `pilot/source-authority-continuation`.
    #
    # ALWAYS required, the same NE-087/PRE-009A precedent as the four above: the
    # resolution UNION (`{**REQUIRED_SECRETS, **from_bundle}`) makes a bundle
    # promoted before this unit fail `ne up` with the missing NAME until its store
    # is populated. Note the asymmetry with the composition root, and it is
    # deliberate: `ne up` fails LOUDLY on an unprovisioned name, while a replica
    # that receives an unusable value fails CONTINUATION readiness silently. The
    # first is an operator with a bundle in hand; the second is a log sink.
    "NE_SOURCE_AUTHORITY_CONTINUATION_HMAC_KEY_ID": {
        "path": "pilot/source-authority-continuation",
        "field": "key_id",
    },
    "NE_SOURCE_AUTHORITY_CONTINUATION_HMAC_KEY_B64": {
        "path": "pilot/source-authority-continuation",
        "field": "hmac_key_b64",
    },
}

# NE-BEW-INJECT (NE-BROKER-ENTRA-WIRE U2): env var -> SecretStore {path, field}
# for the three Entra broker values consumed by the U1 realm's disabled-by-
# default `entra` identityProvider entry. NAMES/paths only, never a value
# (C12). Resolved BEST-EFFORT at `ne up`: an absent `pilot/entra-broker` path
# is a clean no-op (see `resolve_optional_secrets` for exactly what "no-op"
# means at each layer, each backed by a test or artifact); a PRESENT path
# with any field MISSING OR EMPTY fails `ne up` loudly, naming only the
# affected env vars (empty-string is treated identically to missing — see
# `_resolve_optional_secrets_async`, P3-1: a half-populated path must not be
# silently indistinguishable from no broker at all).
#
# CRITICAL: these names must NEVER be folded into REQUIRED_SECRETS or written
# into secrets/required.json. `_resolve_required_secrets_async`'s
# `{**REQUIRED_SECRETS, **from_bundle}` union would silently promote an
# optional name to mandatory, turning a deployment's total absence of Entra
# config into a stack-wide `ne up` failure — exactly the bug this unit exists
# to prevent.
OPTIONAL_SECRETS: dict[str, dict[str, str]] = {
    "NE_PILOT_ENTRA_TENANT_ID": {"path": "pilot/entra-broker", "field": "tenant_id"},
    "NE_PILOT_ENTRA_BROKER_CLIENT_ID": {"path": "pilot/entra-broker", "field": "client_id"},
    "NE_PILOT_ENTRA_BROKER_CLIENT_SECRET": {
        "path": "pilot/entra-broker",
        "field": "client_secret",
    },
}

SCHEMA_VERSION = 1

# Top-level bundle subdirs that are runtime-generated and NOT part of the sealed
# bundle identity: `runtime/` holds the installer's ephemeral 0600 env file
# (up.py writes it AFTER promote, from resolved SecretStore values). It must be
# excluded from both the manifest and verify so `ne up`/`ne pilot verify` stay
# idempotent on a bundle that has been brought up. It also must never carry a
# secret value into the hashed/committed surface (C12).
_EPHEMERAL_SUBDIRS = frozenset({"runtime"})

# The composition-root module set the config_hash is composed over (NE-CKC12).
# Mirrors ne.runtime.factory.freeze_platform_config exactly.
_MODULE_KEYS = ("ne-runtime", "ne-govern")

# Empty-but-valid TOML written when a module config is not supplied. FIXED text
# (no timestamp / no host data) so an omitted-config bundle is byte-reproducible.
_DEFAULT_CONFIGS: dict[str, str] = {
    "ne-runtime.toml": (
        "# ne-runtime settings for this pilot.\n"
        "# Empty = platform defaults (RuntimeSettings). Add keys to override.\n"
    ),
    "ne-govern.toml": (
        "# ne-govern settings for this pilot.\n"
        "# Empty = platform defaults (GovernSettings). Add keys to override.\n"
    ),
    "ne-secrets.toml": (
        "# ne-secrets settings for this pilot (selects the SecretStore adapter).\n"
        "# Empty = platform defaults (SecretsSettings; store = 'sops-file').\n"
    ),
    # NE-087 (O-4): ReBAC store/model pin for `ne up --profile rebac`. Default is
    # comment-only (empty store_id/authorization_model_id ⇒ ReBAC NOT bootstrapped ⇒
    # `ne up` injects no NE_REBAC_* app env). Populate it from the bootstrap CLI
    # (`python -m ne.permissions_rebac.bootstrap`) then re-promote.
    "ne-rebac.toml": (
        "# ne-permissions-rebac store/model pin for this pilot (NE-087 O-4).\n"
        "# Empty store_id/authorization_model_id = ReBAC not yet bootstrapped; the\n"
        "# rebac compose profile still starts the server for the bootstrap phase.\n"
        "# Fill these from `python -m ne.permissions_rebac.bootstrap`, then re-promote:\n"
        "# enabled = true\n"
        '# api_url = "http://openfga:8080"\n'
        '# store_id = ""\n'
        '# authorization_model_id = ""\n'
    ),
}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iter_bundle_files(bundle_dir: Path) -> list[tuple[Path, str]]:
    """Sealed bundle files as (path, relposix), sorted, deterministic.

    Excludes ``manifest.json`` (it records the others) and any ``_EPHEMERAL_SUBDIRS``
    subtree (installer-generated runtime state, never part of the sealed identity).
    Shared by :func:`build_manifest` and :func:`verify` so they enumerate the
    EXACT same set — the invariant the P1 idempotency fix depends on.
    """
    out: list[tuple[Path, str]] = []
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        rel = path.relative_to(bundle_dir)
        if rel.parts and rel.parts[0] in _EPHEMERAL_SUBDIRS:
            continue
        out.append((path, rel.as_posix()))
    return out


def _canonical_json(doc: dict[str, Any]) -> str:
    """Canonical JSON: sorted keys, tight separators — byte-reproducible."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_manifest_hash(manifest: dict[str, Any]) -> str:
    """sha256 over the canonical JSON of every manifest field EXCEPT manifest_hash.

    Brings customer/hostname (and the files/config_hash records) under
    tamper-evidence; ne up trusts manifest['hostname'] for KC_HOSTNAME + the Caddy
    TLS vhost, so it must not sit outside the integrity check (NE-070-FOLLOWUP
    security fix). Kept SEPARATE from config_hash, which must stay byte-identical to
    the composition-root freeze stamped on AuditEvents (NE-CKC12).
    """
    preimage = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    return hashlib.sha256(_canonical_json(preimage).encode("utf-8")).hexdigest()


def _write_config(dest_dir: Path, filename: str, source: str | None) -> None:
    dest = dest_dir / filename
    if source is None:
        dest.write_text(_DEFAULT_CONFIGS[filename], encoding="utf-8")
    else:
        src = Path(source)
        if not src.is_file():
            raise click.ClickException(f"config file not found: {src}")
        # Copy VERBATIM (preserve bytes so the hash reflects the operator's file).
        dest.write_bytes(src.read_bytes())


def compute_bundle_config_hash(config_dir: Path) -> str:
    """The composed NE-CKC12 platform config_hash for a bundle's config/ dir.

    Lazy imports (mirrors ne.cli.fingerprint): ne-contracts/ne-runtime/ne-govern
    pull a heavy graph, so importing them here keeps `ne --help` fast. Paths are
    resolved ABSOLUTE before entering the determinism guard, which chdirs to a
    scratch dir — a relative toml path would otherwise dangle.
    """
    from ne.cli.fingerprint import _scrubbed_determinism_guard
    from ne.contracts.config import compute_platform_config_hash
    from ne.govern.config import GovernSettings
    from ne.runtime.config import RuntimeSettings

    runtime_toml = (config_dir / "ne-runtime.toml").resolve()
    govern_toml = (config_dir / "ne-govern.toml").resolve()
    with _scrubbed_determinism_guard():
        modules = {
            "ne-runtime": RuntimeSettings.from_config_file(runtime_toml),
            "ne-govern": GovernSettings.from_config_file(govern_toml),
        }
        return compute_platform_config_hash(modules)


def build_manifest(bundle_dir: Path, customer: str, hostname: str) -> dict[str, Any]:
    """Assemble the reproducible manifest for an already-materialized bundle dir.

    No timestamp is stored — the manifest is a pure function of the inputs, so two
    promotions from identical inputs produce byte-identical manifests.
    """
    config_hash = compute_bundle_config_hash(bundle_dir / "config")
    files: dict[str, str] = {}
    for path, rel in _iter_bundle_files(bundle_dir):
        files[rel] = _sha256_file(path)
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "customer": customer,
        "hostname": hostname,
        "config_hash": config_hash,
        "files": files,
    }
    # Additive integrity field over all of the above (readers unaffected;
    # deterministic → byte-reproducibility preserved).
    doc["manifest_hash"] = compute_manifest_hash(doc)
    return doc


@click.group("pilot")
def pilot() -> None:
    """Per-customer pilot config bundles (promote / verify)."""


@pilot.command("promote")
@click.option("--customer", required=True, help="Customer name (stamped in the manifest).")
@click.option("--hostname", required=True, help="Pilot TLS hostname (NE_PILOT_HOSTNAME).")
@click.option(
    "--out",
    "out",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Output bundle directory (created).",
)
@click.option("--runtime-config", type=click.Path(exists=True), default=None)
@click.option("--govern-config", type=click.Path(exists=True), default=None)
@click.option("--secrets-config", type=click.Path(exists=True), default=None)
@click.option(
    "--rebac-config",
    type=click.Path(exists=True),
    default=None,
    help="NE-087 config/ne-rebac.toml (store/model pin from the bootstrap CLI).",
)
def promote(
    customer: str,
    hostname: str,
    out: Path,
    runtime_config: str | None,
    govern_config: str | None,
    secrets_config: str | None,
    rebac_config: str | None,
) -> None:
    """Stamp+seal a reproducible pilot bundle for CUSTOMER at HOSTNAME.

    Dev→pilot promotion: pass a dev deployment's per-module TOMLs via
    --runtime-config / --govern-config / --secrets-config; omitted configs mean
    platform defaults. --rebac-config seals a bootstrapped ReBAC store/model pin
    (tamper-evident id pinning); omitted = the comment-only default (ReBAC off).
    """
    config_dir = out / "config"
    secrets_dir = out / "secrets"
    config_dir.mkdir(parents=True, exist_ok=True)
    secrets_dir.mkdir(parents=True, exist_ok=True)

    _write_config(config_dir, "ne-runtime.toml", runtime_config)
    _write_config(config_dir, "ne-govern.toml", govern_config)
    _write_config(config_dir, "ne-secrets.toml", secrets_config)
    _write_config(config_dir, "ne-rebac.toml", rebac_config)

    # Secret NAMES/paths only — never a value (C12).
    (secrets_dir / "required.json").write_text(
        _canonical_json(dict(REQUIRED_SECRETS)) + "\n", encoding="utf-8"
    )

    manifest = build_manifest(out, customer, hostname)
    (out / "manifest.json").write_text(_canonical_json(manifest) + "\n", encoding="utf-8")

    click.echo(f"pilot bundle written: {out}")
    click.echo(f"  customer:    {customer}")
    click.echo(f"  hostname:    {hostname}")
    click.echo(f"  config_hash: {manifest['config_hash']}")


def verify_bundle(bundle_dir: Path) -> dict[str, Any]:
    """Verify a bundle: every file sha256 and the composed config_hash match.

    Raises ``click.ClickException`` naming the exact mismatched entry on any
    tampered byte (fail-closed). Returns the parsed, verified manifest dict on
    success — ``ne pilot verify`` and the K8s installer (``ne k8s
    render``/``ne k8s up``, tools/ne-cli/src/ne/cli/k8s.py) share this single
    verification path so a tampered bundle can never reach either.
    """
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        raise click.ClickException(f"no manifest.json in {bundle_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"manifest.json is not valid JSON: {exc}") from exc

    recorded_files: dict[str, str] = manifest.get("files", {})
    # Every recorded file must still hash to its recorded value.
    for rel, expected in sorted(recorded_files.items()):
        path = bundle_dir / rel
        if not path.is_file():
            raise click.ClickException(f"bundle file missing: {rel}")
        actual = _sha256_file(path)
        if actual != expected:
            raise click.ClickException(f"file sha256 mismatch: {rel}")

    # No unrecorded file may have appeared (added tamper). The ephemeral
    # runtime/ subtree is excluded by _iter_bundle_files, so a bundle that has
    # been brought up (runtime/pilot.env present) still verifies clean.
    for _path, rel in _iter_bundle_files(bundle_dir):
        if rel not in recorded_files:
            raise click.ClickException(f"unrecorded file present: {rel}")

    # The composed config_hash must recompute to the recorded value.
    recomputed = compute_bundle_config_hash(bundle_dir / "config")
    if recomputed != manifest.get("config_hash"):
        raise click.ClickException(
            f"config_hash mismatch: recorded {manifest.get('config_hash')} recomputed {recomputed}"
        )

    # Manifest integrity: customer/hostname/config_hash/files must not have been
    # edited after promote. Fail-closed — a missing manifest_hash is itself a
    # rejection (a pre-hardening bundle or a stripped field), never grandfathered.
    recorded_mh = manifest.get("manifest_hash")
    if not isinstance(recorded_mh, str):
        raise click.ClickException(
            "manifest has no manifest_hash — tampered or pre-hardening bundle; "
            "re-run `ne pilot promote` (verify is fail-closed on this field)"
        )
    if compute_manifest_hash(manifest) != recorded_mh:
        raise click.ClickException(
            "manifest integrity mismatch: a protected manifest field "
            "(customer/hostname/config_hash/files) was modified after promote"
        )
    return manifest


@pilot.command("verify")
@click.argument("bundle_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
def verify(bundle_dir: Path) -> None:
    """Verify a bundle: every file sha256 and the composed config_hash match.

    Exits nonzero naming the exact mismatched entry on any tampered byte.
    """
    manifest = verify_bundle(bundle_dir)
    click.echo(f"bundle OK: {bundle_dir} (config_hash {manifest['config_hash']})")


async def _resolve_required_secrets_async(bundle: Path) -> dict[str, str]:
    from ne.contracts.errors import SecretNotFoundError
    from ne.secrets import SecretsSettings, create_secret_store

    from_bundle: dict[str, dict[str, str]] = json.loads(
        (bundle / "secrets" / "required.json").read_text(encoding="utf-8")
    )
    # Resolve the UNION of the current REQUIRED_SECRETS and the bundle's recorded
    # required.json: a pre-087 bundle (no NE_PILOT_OPENFGA_API_TOKEN in its
    # required.json) still fails with the missing-secret NAME here instead of a
    # cryptic compose ${...:?} interpolation error at `ne up` (NE-087 RISK-2).
    required: dict[str, dict[str, str]] = {**REQUIRED_SECRETS, **from_bundle}
    settings = SecretsSettings.from_config_file(bundle / "config" / "ne-secrets.toml")
    store = create_secret_store(settings)

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for env_var, spec in required.items():
        try:
            version = await store.read(spec["path"])
            resolved[env_var] = version.data[spec["field"]]
        except (SecretNotFoundError, KeyError):
            missing.append(env_var)
    if missing:
        raise click.ClickException(
            "missing pilot secrets in the SecretStore: "
            + ", ".join(sorted(missing))
            + " (populate them, e.g. via `ne` secret tooling, then retry)"
        )
    return resolved


def resolve_required_secrets(bundle: Path) -> dict[str, str]:
    """Resolve ``secrets/required.json`` through the bundle's SecretStore.

    Mirrors ``ne.cli.up._write_runtime_env``'s resolution exactly (same
    SecretsSettings/create_secret_store code path) but returns the resolved
    env-var -> value mapping instead of writing an env file — the shared seam
    for both the pilot Compose installer (``up.py``) and the K8s installer
    (``ne k8s up``, which applies the result as a Secret via ``kubectl`` on
    STDIN, never argv/disk). Values are NEVER echoed or logged (C12).
    """
    import asyncio

    return asyncio.run(_resolve_required_secrets_async(bundle))


async def _resolve_optional_secrets_async(bundle: Path) -> dict[str, str]:
    from ne.contracts.errors import SecretNotFoundError
    from ne.secrets import SecretsSettings, create_secret_store

    settings = SecretsSettings.from_config_file(bundle / "config" / "ne-secrets.toml")
    store = create_secret_store(settings)

    # All OPTIONAL_SECRETS specs currently share the single "pilot/entra-broker"
    # path — read it ONCE (not once per env var) and map every field from the
    # single SecretVersion. Avoids three SecretStore round trips (and, for a
    # remote adapter like OpenBao, three separate client/auth handshakes) on
    # every `ne up`, even in the overwhelmingly common absent-path case (P3-3).
    paths = {spec["path"] for spec in OPTIONAL_SECRETS.values()}
    assert len(paths) == 1, "OPTIONAL_SECRETS specs must share one SecretStore path"
    (path,) = paths
    try:
        version = await store.read(path)
    except SecretNotFoundError:
        # The whole path is absent — the common case (no Entra brokering
        # configured for this customer). Clean no-op: contribute nothing.
        return {}

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for env_var, spec in OPTIONAL_SECRETS.items():
        # An empty-string field is treated IDENTICALLY to a missing field
        # (P3-1): the Terraform-created `${prefix}-entra-broker-client-secret`
        # populates ONLY client_secret, so the natural mid-configuration state
        # is one field written and two absent/empty — silently resolving an
        # empty string would make that half-configured broker indistinguishable
        # from no broker at all, which is worse than failing loudly.
        value = version.data.get(spec["field"])
        if not value:
            missing.append(env_var)
        else:
            resolved[env_var] = value
    if missing:
        raise click.ClickException(
            "Entra broker secret path pilot/entra-broker is present but has a "
            "missing or empty value for: " + ", ".join(sorted(missing)) + " — "
            "populate every field (tenant_id, client_id, client_secret) at the "
            "path with a non-empty value in ONE write, or remove the path "
            "entirely (see deploy/compose/pilot/keycloak/README.md)"
        )
    return resolved


def resolve_optional_secrets(bundle: Path) -> dict[str, str]:
    """Resolve ``OPTIONAL_SECRETS`` through the bundle's SecretStore, best-effort.

    Best-effort optional resolution for the pilot Compose tier only (k8s/Helm
    parity is NE-BEW U5). "Clean no-op" on an absent ``pilot/entra-broker``
    SecretStore path means, concretely, at each layer:

    * this function returns ``{}`` (test: ``test_resolve_optional_secrets_
      absent_path_returns_empty``), so ``_write_runtime_env`` writes none of
      the three ``NE_PILOT_ENTRA_*`` keys into the runtime env file;
    * with those keys absent from the env file, compose's null-value
      pass-through declarations for the same three keys
      (``deploy/compose/pilot/compose.yaml``) OMIT them from the keycloak
      container's env entirely (structural test:
      ``test_keycloak_entra_env_is_null_value_pass_through``) — the same
      container-env shape the U1 realm merged with;
    * that this shape is tolerated by Keycloak's ``--import-realm`` for the
      disabled ``entra`` identityProvider entry is proven only at runtime, by
      ``.github/workflows/no-egress.yml``'s real ``ne up`` against a bundle
      with no ``pilot/entra-broker`` path — no unit test in this repo can
      prove that (it starts no Keycloak container).

    A path that IS present but has a missing OR empty-string value for one or
    more fields fails loud (P3-1: empty-string is treated as missing so a
    half-configured broker can never silently pass as "no broker"), naming
    only the affected env-var NAMES. Values are NEVER echoed or logged (C12).
    """
    import asyncio

    return asyncio.run(_resolve_optional_secrets_async(bundle))
