# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Owner-run CLI; all CI coverage injects mocked transports and performs no egress."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.salesforce.config import SalesforceConnectorSettings, SalesforceObjectConfig
from ne.meridian_seed.errors import SeedError, SeedPreflightError
from ne.meridian_seed.m365 import M365Seeder
from ne.meridian_seed.manifest import SeedManifest, SeedStage, Tenant, default_manifest_dir
from ne.meridian_seed.personas import PrincipalsMap, default_principals_path
from ne.meridian_seed.salesforce import SalesforceSeeder
from ne.meridian_seed.slice import SliceSelection, slice_corpus

_OWNER = "meridian-seed"
_WRITE_CONFIRMATION = "MERIDIAN-DEMO-TENANT"


def _selection(args: argparse.Namespace) -> SliceSelection:
    return slice_corpus(Path(args.fixtures) if getattr(args, "fixtures", None) else None)


def _manifest_dir(args: argparse.Namespace) -> Path:
    return (
        Path(args.manifest_dir) if getattr(args, "manifest_dir", None) else default_manifest_dir()
    )


def _principals(args: argparse.Namespace) -> PrincipalsMap:
    path = Path(args.principals) if args.principals else default_principals_path()
    return PrincipalsMap.load(path)


def _m365_settings() -> M365ConnectorSettings:
    return M365ConnectorSettings(owner=_OWNER)


def _salesforce_settings() -> SalesforceConnectorSettings:
    return SalesforceConnectorSettings(
        owner=_OWNER,
        objects=(SalesforceObjectConfig(name="Account", content_fields=("Name",)),),
    )


def _require_confirmation(args: argparse.Namespace) -> None:
    if args.confirm != _WRITE_CONFIRMATION:
        raise SeedError(
            f"refusing tenant mutation; pass --confirm {_WRITE_CONFIRMATION} after verifying scope"
        )


def _require_generation(args: argparse.Namespace) -> str:
    value = getattr(args, "generation_id", None)
    if not value:
        raise SeedError(
            "--generation-id is required for every M365 seed command; choose it once and "
            "record it externally"
        )
    return value


def _prior_m365_manifest(manifest_dir: Path) -> SeedManifest | None:
    path = manifest_dir / "m365.json"
    if not path.is_file():
        return None
    try:
        return SeedManifest.load(manifest_dir, "m365")
    except (OSError, ValueError):
        raise SeedPreflightError(
            "saved M365 manifest is unreadable or invalid; refusing before provider I/O"
        ) from None


def _cmd_slice(args: argparse.Namespace) -> int:
    selection = _selection(args)
    print(f"corpus: {selection.corpus_version}")
    print(
        f"documents: {len(selection.documents_for_stage('initial'))} initial, "
        f"{len(selection.documents_for_stage('supersession'))} supersession"
    )
    print(
        f"salesforce: {len(selection.accounts)} accounts, {len(selection.contacts)} contacts, "
        f"{len(selection.cases)} cases"
    )
    print(f"operational graph: {len(selection.operational_graph)} driveItems")
    return 0


def _cmd_push(args: argparse.Namespace) -> int:
    _require_confirmation(args)
    tenant: Tenant = args.tenant
    manifest_dir = _manifest_dir(args)
    selection = _selection(args)
    if tenant == "m365":
        stage: SeedStage = args.stage
        generation = _require_generation(args)
        manifest = asyncio.run(
            M365Seeder(_m365_settings(), _principals(args)).push(
                selection,
                stage=stage,
                prior_manifest=_prior_m365_manifest(manifest_dir),
                generation_id=generation,
            )
        )
    else:
        if args.stage != "initial":
            raise SeedError("Salesforce supports only the initial stage")
        manifest = asyncio.run(SalesforceSeeder(_salesforce_settings()).push(selection))
    path = manifest.save(manifest_dir)
    print(
        f"{tenant}: {len(manifest.docs)} docs, {len(manifest.records)} records, "
        f"stages {[entry.stage for entry in manifest.stage_history]}"
    )
    print(f"manifest: {path}")
    return 0


def _cmd_unseed(args: argparse.Namespace) -> int:
    _require_confirmation(args)
    tenant: Tenant = args.tenant
    manifest_dir = _manifest_dir(args)
    if tenant == "m365":
        generation = _require_generation(args)
        asyncio.run(
            M365Seeder(_m365_settings(), _principals(args)).unseed(
                manifest_dir, generation_id=generation
            )
        )
    else:
        asyncio.run(SalesforceSeeder(_salesforce_settings()).unseed(manifest_dir))
    print(f"unseeded {tenant}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    if args.tenant == "m365":
        generation = _require_generation(args)
        result = asyncio.run(
            M365Seeder(_m365_settings(), _principals(args)).check(
                _selection(args),
                prior_manifest=_prior_m365_manifest(_manifest_dir(args)),
                generation_id=generation,
            )
        )
        print(f"m365 check: {result.status}")
        if result.detail:
            print(result.detail)
        if result.completed_stages:
            print(f"completed stages: {', '.join(result.completed_stages)}")
        return 0
    seeder = SalesforceSeeder(_salesforce_settings())

    async def run() -> None:
        async with seeder._client.session() as http:
            await seeder.check(http)

    asyncio.run(run())
    print(f"{args.tenant}: check ok")
    return 0


def _cmd_adopt_legacy_m365(args: argparse.Namespace) -> int:
    _require_confirmation(args)
    generation = _require_generation(args)
    manifest_dir = _manifest_dir(args)
    prior = _prior_m365_manifest(manifest_dir)
    if prior is None:
        raise SeedPreflightError("no saved legacy M365 manifest exists")
    already_bound = prior.m365_target is not None
    manifest = asyncio.run(
        M365Seeder(_m365_settings(), _principals(args)).adopt_legacy(
            _selection(args), prior_manifest=prior, generation_id=generation
        )
    )
    path = manifest_dir / "m365.json" if already_bound else manifest.save(manifest_dir)
    print(f"m365 legacy adoption: {'already bound' if already_bound else 'adopted'}")
    print(f"manifest: {path}")
    return 0


def _tenant(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tenant", required=True, choices=("m365", "salesforce"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ne-meridian-seed")
    sub = parser.add_subparsers(dest="command", required=True)
    slice_parser = sub.add_parser("slice")
    slice_parser.add_argument("--fixtures")
    push = sub.add_parser("push")
    _tenant(push)
    push.add_argument(
        "--stage", choices=("initial", "operational_graph", "supersession"), default="initial"
    )
    push.add_argument("--fixtures")
    push.add_argument("--manifest-dir")
    push.add_argument("--principals")
    push.add_argument("--confirm", required=True)
    push.add_argument("--generation-id", help="M365 only: operator-chosen persistence generation")
    unseed = sub.add_parser("unseed")
    _tenant(unseed)
    unseed.add_argument("--manifest-dir")
    unseed.add_argument("--principals")
    unseed.add_argument("--confirm", required=True)
    unseed.add_argument("--generation-id", help="M365 only: operator-chosen persistence generation")
    check = sub.add_parser("check")
    _tenant(check)
    check.add_argument("--fixtures")
    check.add_argument("--manifest-dir")
    check.add_argument("--principals")
    check.add_argument("--generation-id", help="M365 only: operator-chosen persistence generation")
    adopt = sub.add_parser("adopt-legacy-m365")
    adopt.add_argument("--fixtures")
    adopt.add_argument("--manifest-dir")
    adopt.add_argument("--principals")
    adopt.add_argument("--confirm", required=True)
    adopt.add_argument("--generation-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "slice":
            return _cmd_slice(args)
        if args.command == "push":
            return _cmd_push(args)
        if args.command == "unseed":
            return _cmd_unseed(args)
        if args.command == "check":
            return _cmd_check(args)
        if args.command == "adopt-legacy-m365":
            return _cmd_adopt_legacy_m365(args)
    except SeedError as exc:
        print(f"seed error: {exc}", file=sys.stderr)
        return 1
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
