# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-086 — emit the installer-mode C12 Secret for the Zarf deploy path.

Exact mirror of `ne k8s up` step (e) (tools/ne-cli/src/ne/cli/k8s.py): resolve
the bundle's required secrets through its SecretStore and print the namespaced
Opaque Secret manifest as JSON on STDOUT for `kubectl apply -f -`.

`ne k8s up` normally creates this Secret itself (kubectl on STDIN) as part of a
helm-driven install. On the Zarf path, Zarf — not helm-via-ne — drives the
chart install, so the Secret must be applied out-of-band; this script is that
seam. C12: secret values leave ONLY via the STDOUT pipe into `kubectl apply -f
-` (stdin) — never argv, never a disk file, never a log line. Nothing else is
printed to stdout, so the pipe carries exactly the one manifest.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emit the installer-mode C12 Secret manifest.")
    parser.add_argument(
        "--bundle",
        required=True,
        type=Path,
        help="Per-customer config bundle (from `ne pilot promote`).",
    )
    parser.add_argument("--namespace", default="neutron", help="Target namespace.")
    parser.add_argument("--name", default="ne-platform-secrets", help="Secret object name.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Imported lazily so `--help` needs no ne-cli import; resolution runs the
    # bundle's SecretStore exactly as `ne k8s up` does.
    from ne.cli.pilotcfg import resolve_required_secrets

    resolved = resolve_required_secrets(args.bundle)
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": args.name, "namespace": args.namespace},
        "type": "Opaque",
        "stringData": resolved,
    }
    # STDOUT is the ONLY sink and it is piped straight into `kubectl apply -f -`.
    sys.stdout.write(json.dumps(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
