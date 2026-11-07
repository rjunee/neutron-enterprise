# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Build a throwaway `ne pilot promote` bundle + secret store for the NE-073
no-egress CI harness.

This is the CI-shaped equivalent of an operator running:

    ne pilot promote --customer <cust> --hostname <host> \
        --secrets-config <ne-secrets.toml> --out <bundle>

followed by seeding the required pilot secrets into the resolved SecretStore.
The store lives in a directory OUTSIDE the bundle (``--store-dir``): if the
plaintext secret file were written inside the bundle, `ne pilot verify` would
reject it as an unrecorded file (see tools/ne-cli/src/ne/cli/pilotcfg.py).

All secret values are runtime-generated random tokens (``secrets.token_urlsafe``)
that exist only for the lifetime of the CI job; they are never printed or
logged (C12). The store uses the ``plain`` FileStoreConfig codec
(:class:`ne.secrets.codecs.PlainJsonCodec`) so the CI bundle never needs the
``sops`` binary installed.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import subprocess
import sys
from pathlib import Path

_CANONICAL_256_BIT_SECRET_ENVS = frozenset(
    {
        "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64",
        "NE_SOURCE_AUTHORITY_CONTINUATION_HMAC_KEY_B64",
    }
)


def build_ci_secret_payloads(
    required_secrets: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Return valid throwaway payloads grouped by SecretStore path.

    Most required fields accept an opaque token. Cryptographic key fields are
    different: their production loaders require canonical unpadded base64url
    encoding of at least 256 bits. ``token_urlsafe(32)`` is exactly that shape.
    """
    by_path: dict[str, dict[str, str]] = {}
    for env_name, spec in required_secrets.items():
        path = spec["path"]
        field = spec["field"]
        byte_count = 32 if env_name in _CANONICAL_256_BIT_SECRET_ENVS else 24
        by_path.setdefault(path, {})[field] = secrets.token_urlsafe(byte_count)
    return by_path


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a throwaway pilot bundle + secret store for CI air-gap testing."
    )
    parser.add_argument("--out", required=True, type=Path, help="bundle output directory")
    parser.add_argument(
        "--store-dir",
        required=True,
        type=Path,
        help="directory (OUTSIDE --out) to hold the plain-mode secret store file",
    )
    parser.add_argument(
        "--customer", default="ci-airgap", help="customer name stamped in the manifest"
    )
    parser.add_argument(
        "--hostname", default="airgap.ci.local", help="pilot TLS hostname (NE_PILOT_HOSTNAME)"
    )
    parser.add_argument("--protected-platform-database-url")
    parser.add_argument("--protected-dbos-database-url")
    return parser.parse_args(argv)


def _write_secrets_toml(store_dir: Path) -> Path:
    """Write the ne-secrets.toml selecting the plain-mode FileSecretStore.

    ``mode = "plain"`` selects :class:`ne.secrets.codecs.PlainJsonCodec` (the
    dev/CI codec) so no ``sops`` binary is required anywhere in this harness.
    """
    secrets_json = store_dir / "secrets.ne.json"
    toml_path = store_dir / "ne-secrets.toml"
    toml_path.write_text(
        f'store = "sops-file"\n\n[file]\npath = "{secrets_json}"\nmode = "plain"\n',
        encoding="utf-8",
    )
    return toml_path


async def _seed_secrets(
    secrets_toml: Path,
    *,
    protected_platform_database_url: str | None,
    protected_dbos_database_url: str | None,
) -> None:
    """Write random throwaway values for every REQUIRED_SECRETS entry.

    Never prints a resolved value (C12) — only the env-var NAMES are echoed.
    """
    from ne.cli.pilotcfg import REQUIRED_SECRETS
    from ne.secrets import SecretsSettings, create_secret_store

    settings = SecretsSettings.from_config_file(secrets_toml)
    store = create_secret_store(settings)

    # Group by secret path so each path is written once with all its fields
    # (postgres.password, keycloak.admin_password).
    by_path = build_ci_secret_payloads(REQUIRED_SECRETS)

    for path, data in by_path.items():
        await store.write(path, data)
        print(f"seeded CI secret at path {path!r} (field(s): {sorted(data)}, value not shown)")

    if (protected_platform_database_url is None) != (protected_dbos_database_url is None):
        raise ValueError("both protected bootstrap database URLs are required together")
    if protected_platform_database_url is not None and protected_dbos_database_url is not None:
        await store.write(
            "k8s/protected-transport-bootstrap",
            {
                "platform_database_url": protected_platform_database_url,
                "dbos_database_url": protected_dbos_database_url,
            },
        )
        print(
            "seeded CI secret at path 'k8s/protected-transport-bootstrap' "
            "(field(s): ['dbos_database_url', 'platform_database_url'], value not shown)"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    args.store_dir.mkdir(parents=True, exist_ok=True)
    args.store_dir.chmod(0o700)
    secrets_toml = _write_secrets_toml(args.store_dir)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    promote_cmd = [
        "uv",
        "run",
        "ne",
        "pilot",
        "promote",
        "--customer",
        args.customer,
        "--hostname",
        args.hostname,
        "--secrets-config",
        str(secrets_toml),
        "--out",
        str(args.out),
    ]
    subprocess.run(promote_cmd, check=True)

    asyncio.run(
        _seed_secrets(
            secrets_toml,
            protected_platform_database_url=args.protected_platform_database_url,
            protected_dbos_database_url=args.protected_dbos_database_url,
        )
    )

    verify_cmd = ["uv", "run", "ne", "pilot", "verify", str(args.out)]
    subprocess.run(verify_cmd, check=True)

    print(f"CI pilot bundle ready: {args.out}")
    print(f"CI secret store: {args.store_dir} (plain mode, throwaway values)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
