# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne-backup`` — the thin Click surface over the click-free hardened core.

Secrets are never echoed; ``keygen`` prints only the KEK fingerprint. Every typed
:class:`~ne.backup.errors.NeBackupError` is mapped to a ``click.ClickException``
so the core stays click-free while the CLI still exits cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import click

from ne.backup import __version__
from ne.backup.config import BackupConfig
from ne.backup.errors import NeBackupError
from ne.backup.keys import keygen as _keygen
from ne.backup.keys import resolve_kek
from ne.backup.manifest import BackupKind
from ne.backup.postgres import Postgres
from ne.backup.qdrant import Qdrant
from ne.backup.runner import DockerRunner
from ne.backup.runtime import (
    Services,
    create_backup,
    list_backups,
    prune_backups,
    restore_backup,
    verify_backup,
)
from ne.backup.volumes import VolumeArchiver


def _build_services(config: BackupConfig, *, with_qdrant: bool = True) -> Services:
    runner = DockerRunner()
    qdrant = Qdrant(config.qdrant_url) if with_qdrant else None
    return Services(
        postgres=Postgres(runner, config.postgres_container, config.postgres_user),
        volumes=VolumeArchiver(runner, config.compose_project, config.helper_image),
        runner=runner,
        qdrant=qdrant,
        echo=click.echo,
    )


def _load_config(config_path: str | None) -> BackupConfig:
    try:
        return BackupConfig.load(Path(config_path) if config_path else None)
    except (OSError, ValueError) as error:
        raise click.ClickException(f"invalid backup config: {error}") from error


@click.group()
@click.version_option(__version__, prog_name="ne-backup")
def main() -> None:
    """Pilot-tier backup/restore with customer-managed encryption at rest."""


@main.command()
@click.option(
    "--bundle", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option(
    "--rotate",
    is_flag=True,
    help="Replace an existing KEK. WARNING: orphans every backup made under the old key.",
)
def keygen(bundle: Path, rotate: bool) -> None:
    """Generate a customer KEK into the bundle SecretStore (prints fingerprint ONLY).

    Refuses to overwrite an existing KEK unless --rotate is passed.
    """
    try:
        fingerprint = _keygen(bundle, rotate=rotate)
    except NeBackupError as error:
        raise click.ClickException(str(error)) from error
    verb = "rotated" if rotate else "provisioned"
    click.echo(f"KEK {verb}. fingerprint: {fingerprint}")
    click.echo(
        "The key lives ONLY in the bundle SecretStore. Losing it makes backups unrecoverable."
    )
    if rotate:
        click.echo(
            "WARNING: backups made under the previous key can no longer be restored by this tool."
        )


@main.command()
@click.option(
    "--bundle", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option("--dest", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option("--config", "config_path", default=None, type=click.Path(exists=True, dir_okay=False))
@click.option("--note", default="", help="Free-text note stored in the manifest.")
@click.option(
    "--kind",
    type=click.Choice(["scheduled", "on-demand"]),
    default="on-demand",
    help="Backup kind (safety backups are created only by restore).",
)
def create(bundle: Path, dest: Path, config_path: str | None, note: str, kind: str) -> None:
    """Back up postgres + qdrant + the config bundle + allowlisted volumes."""
    config = _load_config(config_path)
    kind_literal = cast(BackupKind, kind)
    try:
        kek = resolve_kek(bundle)
        services = _build_services(config)
        manifest = create_backup(
            config=config,
            services=services,
            dest=dest,
            kek=kek,
            kind=kind_literal,
            note=note,
            bundle=bundle,
            staging_root=Path(config.staging_dir) if config.staging_dir else None,
        )
    except NeBackupError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"Backup complete: {manifest.id}")
    click.echo(
        f"  databases={len(manifest.databases)} "
        f"collections={len(manifest.qdrant_collections)} "
        f"volumes={len(manifest.volumes)} artifacts={len(manifest.artifacts)}"
    )


@main.command(name="list")
@click.option("--dest", required=True, type=click.Path(file_okay=False, path_type=Path))
def list_cmd(dest: Path) -> None:
    """List backups (newest first)."""
    for manifest in list_backups(dest):
        click.echo(
            f"{manifest.id}  {manifest.kind:<10}  {manifest.created_at}  "
            f"dbs={len(manifest.databases)} cols={len(manifest.qdrant_collections)}"
        )


@main.command()
@click.argument("backup_id")
@click.option(
    "--dest", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option(
    "--bundle",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="If given, also decrypt-verify every artifact's plaintext hash (needs the KEK).",
)
@click.option("--config", "config_path", default=None, type=click.Path(exists=True, dir_okay=False))
def verify(backup_id: str, dest: Path, bundle: Path | None, config_path: str | None) -> None:
    """Verify a backup's integrity (ciphertext hashes; plaintext too with --bundle)."""
    config = _load_config(config_path)
    try:
        kek = resolve_kek(bundle) if bundle is not None else None
        report = verify_backup(
            dest,
            backup_id,
            kek,
            staging_root=Path(config.staging_dir) if config.staging_dir else None,
        )
    except NeBackupError as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(report, indent=2, sort_keys=True))


@main.command()
@click.argument("backup_id")
@click.option(
    "--bundle", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option(
    "--dest", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option("--config", "config_path", default=None, type=click.Path(exists=True, dir_okay=False))
@click.option("--restore-bundle-to", default=None, type=click.Path(file_okay=False, path_type=Path))
@click.option("--no-safety", is_flag=True, help="Skip the pre-restore safety backup (dangerous).")
@click.option("--yes", is_flag=True, help="Do not prompt for confirmation.")
def restore(
    backup_id: str,
    bundle: Path,
    dest: Path,
    config_path: str | None,
    restore_bundle_to: Path | None,
    no_safety: bool,
    yes: bool,
) -> None:
    """Restore a backup (safety-snapshot-first unless --no-safety)."""
    if not yes:
        click.confirm(
            f"Restore {backup_id} — this WIPES and replaces current pilot data. Continue?",
            abort=True,
        )
    config = _load_config(config_path)
    try:
        kek = resolve_kek(bundle)
        services = _build_services(config)
        report = restore_backup(
            config=config,
            services=services,
            dest=dest,
            backup_id=backup_id,
            kek=kek,
            bundle=bundle,
            restore_bundle_to=restore_bundle_to,
            no_safety=no_safety,
            staging_root=Path(config.staging_dir) if config.staging_dir else None,
        )
    except NeBackupError as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(report, indent=2, sort_keys=True))


@main.command()
@click.option(
    "--dest", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option("--keep", default=7, type=int, help="Keep the N newest backups (default 7).")
def prune(dest: Path, keep: int) -> None:
    """Delete old backups, keeping the newest N (never the newest safety backup)."""
    try:
        deleted = prune_backups(dest, keep, echo=click.echo)
    except NeBackupError as error:
        raise click.ClickException(str(error)) from error
    for backup_id in deleted:
        click.echo(f"pruned {backup_id}")
    click.echo(f"pruned {len(deleted)} backup(s); kept {keep} newest.")
