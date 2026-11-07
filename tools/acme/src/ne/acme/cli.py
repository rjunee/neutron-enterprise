# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne-acme`` command-line interface (stdlib argparse).

Subcommands:

* ``generate [--seed 74] [--out PATH]`` — generate the corpus and write the
  six canonical-JSON artifacts to ``PATH`` (defaults to
  :func:`default_fixtures_dir`).
* ``check [--seed 74] [--fixtures PATH]`` — regenerate in-memory and byte-diff
  against the committed fixtures; prints drifted artifact names and exits 1 on
  any drift, 0 when clean (the CI staleness gate).

Default fixtures directory (NE-CLEAN3 item 3): the old default hard-coded
``Path(__file__).resolve().parents[3]`` — a checkout-only assumption that
breaks once this package is pip-installed from a wheel/site-packages (no
``tools/acme`` ancestor exists there). :func:`default_fixtures_dir` instead
tries, in order: (1) the editable-checkout layout
(``tools/acme/fixtures/acme-v1``, via :func:`_checkout_fixtures_dir`) — ALWAYS
tried first, so dev/CI is immune to any stale installed copy; (2) the
wheel-data-scheme install location (``sysconfig.get_paths()['data']/acme-v1``,
via :func:`_installed_fixtures_dir`) — where the corpus lands when this
package is pip-installed (see ``tools/acme/pyproject.toml``'s
``[tool.uv.build-backend] data`` key); (3) falling back to the checkout path
even if it doesn't exist, preserving the historical missing-dir failure mode.
"""

from __future__ import annotations

import argparse
import sysconfig
from collections.abc import Mapping
from pathlib import Path

from ne.acme.generator import check, generate, write
from ne.acme.pools import DEFAULT_SEED

__all__ = ["default_fixtures_dir", "main"]

_CORPUS_DIRNAME = "acme-v1"


def _checkout_fixtures_dir() -> Path:
    """The editable-checkout layout: tools/acme/src/ne/acme/cli.py -> parents[3]
    == tools/acme."""
    return Path(__file__).resolve().parents[3] / "fixtures" / _CORPUS_DIRNAME


def _installed_fixtures_dir(scheme_paths: Mapping[str, str] | None = None) -> Path:
    """The wheel-data-scheme install location (``$prefix/acme-v1``).

    ``scheme_paths`` is the injectable test seam — pass a mapping with a
    ``"data"`` key to resolve with ZERO dependence on repo layout or an actual
    installed package; defaults to ``sysconfig.get_paths()`` (the real scheme
    for the running interpreter).
    """
    paths = scheme_paths if scheme_paths is not None else sysconfig.get_paths()
    return Path(paths["data"]) / _CORPUS_DIRNAME


def default_fixtures_dir() -> Path:
    """Resolve the default fixtures directory: checkout, else installed, else
    checkout (preserving the historical missing-dir failure mode)."""
    checkout = _checkout_fixtures_dir()
    if checkout.is_dir():
        return checkout
    installed = _installed_fixtures_dir()
    if installed.is_dir():
        return installed
    return checkout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ne-acme")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate", help="generate the Acme corpus")
    generate_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate_parser.add_argument("--out", type=Path, default=None)

    check_parser = subparsers.add_parser("check", help="check fixtures are fresh")
    check_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    check_parser.add_argument("--fixtures", type=Path, default=None)

    args = parser.parse_args(argv)

    if args.command == "generate":
        out = args.out if args.out is not None else default_fixtures_dir()
        corpus = generate(args.seed)
        write(corpus, args.seed, out)
        print(f"wrote Acme corpus (seed={args.seed}) to {out}")
        return 0

    if args.command == "check":
        fixtures = args.fixtures if args.fixtures is not None else default_fixtures_dir()
        drift = check(fixtures, args.seed)
        if drift:
            for name in drift:
                print(f"stale: {name}")
            return 1
        print(f"Acme fixtures are fresh (seed={args.seed})")
        return 0

    parser.error(f"unknown command: {args.command}")  # pragma: no cover - argparse guards this
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
