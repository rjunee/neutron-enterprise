# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne-acme-seed`` command-line interface (stdlib argparse — no new dep).

Subcommands:

* ``slice`` — print the pinned 12 doc ids + record counts; NO network (CI-safe).
* ``push --tenant {m365,salesforce,slack}`` — seed the slice into a live tenant
  and write the per-tenant manifest.
* ``unseed --tenant …`` — invert a tenant's seed.
* ``check --tenant …`` — a cheap owner-run connectivity/preflight probe.

Output is COUNTS + the manifest path only — never a token, key material, or a
full URL (C12). Credentials come exclusively from the connector settings'
``NE_*`` env / PEM-path fields; the seeder never reads key material itself.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ne.acme_seed.errors import SeedPreflightError
from ne.acme_seed.m365 import M365Seeder, SeedGraphClient
from ne.acme_seed.manifest import SeedManifest, Tenant, default_manifest_dir
from ne.acme_seed.personas import PrincipalsMap, default_principals_path
from ne.acme_seed.salesforce import SalesforceSeeder
from ne.acme_seed.slack import SeedSlackClient, SlackSeeder
from ne.acme_seed.slice import SliceSelection, slice_corpus
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.salesforce.config import SalesforceConnectorSettings, SalesforceObjectConfig
from ne.connect.slack.config import SlackConnectorSettings

__all__ = ["main"]

_OWNER = "acme-seed"


def _load_slice(args: argparse.Namespace) -> SliceSelection:
    fixtures = getattr(args, "fixtures", None)
    return slice_corpus(Path(fixtures) if fixtures else None)


def _principals(args: argparse.Namespace) -> PrincipalsMap:
    path = Path(args.principals) if args.principals else default_principals_path()
    return PrincipalsMap.load(path)


def _manifest_dir(args: argparse.Namespace) -> Path:
    return Path(args.manifest_dir) if args.manifest_dir else default_manifest_dir()


def _m365_settings() -> M365ConnectorSettings:
    return M365ConnectorSettings(owner=_OWNER)


def _salesforce_settings() -> SalesforceConnectorSettings:
    # objects= only satisfies the connector validator; the seeder never crawls.
    return SalesforceConnectorSettings(
        owner=_OWNER,
        objects=(SalesforceObjectConfig(name="Account", content_fields=("Name",)),),
    )


def _slack_settings() -> SlackConnectorSettings:
    return SlackConnectorSettings(owner=_OWNER)


def _print_manifest_summary(manifest: SeedManifest, path: Path) -> None:
    print(
        f"{manifest.tenant}: {len(manifest.docs)} docs, {len(manifest.records)} records, "
        f"{len(manifest.channels)} channels, {len(manifest.messages)} messages, "
        f"{len(manifest.permsets)} permsets, {len(manifest.warnings)} warnings"
    )
    for warning in manifest.warnings:
        print(f"  warning: {warning}")
    print(f"manifest: {path}")


def _cmd_slice(args: argparse.Namespace) -> int:
    selection = _load_slice(args)
    print(f"corpus: {selection.corpus_version}")
    print(f"docs ({len(selection.docs)}):")
    for doc in selection.docs:
        grants = ",".join(selection.named_persona_grants(doc)) if doc.visibility == "named" else ""
        suffix = f" -> {grants}" if grants else ""
        print(f"  {doc.doc_id} [{doc.visibility}]{suffix}")
    print(
        f"records: {len(selection.accounts)} accounts, {len(selection.people)} people, "
        f"{len(selection.tickets)} tickets"
    )
    return 0


def _cmd_push(args: argparse.Namespace) -> int:
    tenant: Tenant = args.tenant
    selection = _load_slice(args)
    manifest_dir = _manifest_dir(args)
    if tenant == "m365":
        manifest = asyncio.run(M365Seeder(_m365_settings(), _principals(args)).push(selection))
    elif tenant == "salesforce":
        manifest = asyncio.run(SalesforceSeeder(_salesforce_settings()).push(selection))
    else:
        manifest = asyncio.run(SlackSeeder(_slack_settings(), _principals(args)).push(selection))
    path = manifest.save(manifest_dir)
    _print_manifest_summary(manifest, path)
    return 0


def _cmd_unseed(args: argparse.Namespace) -> int:
    tenant: Tenant = args.tenant
    manifest_dir = _manifest_dir(args)
    if tenant == "m365":
        asyncio.run(M365Seeder(_m365_settings(), _principals(args)).unseed(manifest_dir))
    elif tenant == "salesforce":
        asyncio.run(SalesforceSeeder(_salesforce_settings()).unseed(manifest_dir))
    else:
        asyncio.run(SlackSeeder(_slack_settings(), _principals(args)).unseed(manifest_dir))
    print(f"unseeded {tenant}")
    return 0


async def _check_m365() -> None:
    settings = _m365_settings()
    client = SeedGraphClient(settings)
    site_id = settings.site_ids[0]
    async with client.session() as http:
        await client.get_json(http, f"{client.graph_base}/sites/{site_id}/drive/root")


async def _check_slack() -> None:
    client = SeedSlackClient(_slack_settings())
    async with client.session() as http:
        await client.call_write(http, "auth.test", {})


def _cmd_check(args: argparse.Namespace) -> int:
    tenant: Tenant = args.tenant
    if tenant == "m365":
        asyncio.run(_check_m365())
    elif tenant == "salesforce":
        seeder = SalesforceSeeder(_salesforce_settings())

        async def _run() -> None:
            async with seeder._client.session() as http:
                await seeder.check(http)

        asyncio.run(_run())
    else:
        asyncio.run(_check_slack())
    print(f"{tenant}: check ok")
    return 0


def _add_tenant(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tenant", required=True, choices=("m365", "salesforce", "slack"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ne-acme-seed")
    subparsers = parser.add_subparsers(dest="command", required=True)

    slice_parser = subparsers.add_parser("slice", help="print the deterministic slice (no network)")
    slice_parser.add_argument("--fixtures", default=None)

    push_parser = subparsers.add_parser("push", help="seed a tenant")
    _add_tenant(push_parser)
    push_parser.add_argument("--manifest-dir", default=None)
    push_parser.add_argument("--principals", default=None)
    push_parser.add_argument("--fixtures", default=None)

    unseed_parser = subparsers.add_parser("unseed", help="invert a tenant's seed")
    _add_tenant(unseed_parser)
    unseed_parser.add_argument("--manifest-dir", default=None)
    unseed_parser.add_argument("--principals", default=None)

    check_parser = subparsers.add_parser("check", help="probe a tenant (owner-run)")
    _add_tenant(check_parser)

    args = parser.parse_args(argv)

    try:
        if args.command == "slice":
            return _cmd_slice(args)
        if args.command == "push":
            return _cmd_push(args)
        if args.command == "unseed":
            return _cmd_unseed(args)
        if args.command == "check":
            return _cmd_check(args)
    except SeedPreflightError as exc:
        print(f"preflight error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown command: {args.command}")  # pragma: no cover
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
