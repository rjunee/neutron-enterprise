# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CLI for generating and checking the committed Meridian fixture."""

from __future__ import annotations

import argparse
import sys
import sysconfig
from pathlib import Path

from ne.meridian.constants import MERIDIAN_DECLARATION
from ne.meridian.generator import (
    DEFAULT_SEED,
    check_fixture,
    generate,
    serialize,
    write_fixture,
)


def _checkout_fixtures() -> Path:
    return Path(__file__).resolve().parents[3] / "fixtures" / "meridian-v1"


def default_fixtures_dir() -> Path:
    checkout = _checkout_fixtures()
    if checkout.is_dir():
        return checkout
    return Path(sysconfig.get_paths()["data"]) / "meridian-v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ne-meridian")
    sub = parser.add_subparsers(dest="command", required=True)
    generate_parser = sub.add_parser("generate")
    generate_parser.add_argument("--out", type=Path, default=default_fixtures_dir())
    generate_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    check_parser = sub.add_parser("check")
    check_parser.add_argument("--fixtures", type=Path, default=default_fixtures_dir())
    check_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        records, corpus, truth, assets = generate(MERIDIAN_DECLARATION, seed=args.seed)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    files = serialize(records, corpus, truth, assets, seed=args.seed)
    if args.command == "generate":
        write_fixture(files, args.out)
        print(f"Wrote {len(files)} Meridian fixture files to {args.out}")
        return 0
    drift = check_fixture(files, args.fixtures)
    if drift:
        print("Meridian fixture drift: " + ", ".join(drift), file=sys.stderr)
        return 1
    print("Meridian fixtures are fresh")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
