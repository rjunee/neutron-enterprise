# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""`ne dev` — wraps `docker compose` around the local dev stack.

The compose file lives at deploy/compose/dev/compose.yaml (see its README
for the service list, image pins, and licenses). Langfuse (and its ops
subsystem) is behind the "langfuse" compose profile per SPEC §3.3 — the
base `up` never starts it unless --langfuse is passed. `down` always tears
down the langfuse profile too, regardless of how the stack was started, so
a stack started with --langfuse doesn't leak containers/volumes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click

from ne.cli.checks import _find_repo_root

_PROJECT_NAME = "ne-dev"

_ENDPOINT_SUMMARY = """\
Dev stack is up. Endpoints:
  postgres       localhost:5432
  qdrant         http://localhost:6333 (grpc 6334)
  keycloak       http://localhost:8081
  opa            http://localhost:8181
  tei-embed      http://localhost:8085
  tei-rerank     http://localhost:8086
  openbao        http://localhost:8200
  otel-collector grpc:4317 http:4318
Run `ne dev seed` to seed smoke-test data.\
"""

_LANGFUSE_ENDPOINT_SUMMARY = "  langfuse-web   http://localhost:3000"
_REBAC_ENDPOINT_SUMMARY = "  openfga        http://localhost:8087 (rebac profile; NE-087)"


def _dev_dir(root: Path) -> Path:
    return root / "deploy" / "compose" / "dev"


def _compose_file(root: Path) -> Path:
    return _dev_dir(root) / "compose.yaml"


def _compose_base(root: Path, *, langfuse: bool, rebac: bool = False) -> list[str]:
    cmd = [
        "docker",
        "compose",
        "--project-name",
        _PROJECT_NAME,
        "-f",
        str(_compose_file(root)),
    ]
    if langfuse:
        cmd += ["--profile", "langfuse"]
    if rebac:
        cmd += ["--profile", "rebac"]
    return cmd


def _run(cmd: list[str]) -> int:
    try:
        result = subprocess.run(cmd, check=False)
    except FileNotFoundError:
        click.echo(
            "docker not found — install Docker Desktop or the docker CLI with the compose plugin",
            err=True,
        )
        return 1
    return result.returncode


def _require_compose_file(root: Path) -> None:
    if not _compose_file(root).is_file():
        raise click.ClickException(
            f"compose file not found: {_compose_file(root)} "
            "(expected deploy/compose/dev/compose.yaml)"
        )


@click.group("dev")
def dev() -> None:
    """Local development stack (docker compose)."""


@dev.command("up")
@click.option("--langfuse", is_flag=True, help="Also start the Langfuse profile.")
@click.option("--rebac", is_flag=True, help="Also start the OpenFGA rebac profile (NE-087).")
def up(langfuse: bool, rebac: bool) -> None:
    """Start the dev stack (base services; +Langfuse / +OpenFGA with the flags)."""
    root = _find_repo_root(Path.cwd())
    _require_compose_file(root)
    cmd = [*_compose_base(root, langfuse=langfuse, rebac=rebac), "up", "--detach", "--wait"]
    rc = _run(cmd)
    if rc == 0:
        click.echo(_ENDPOINT_SUMMARY)
        if langfuse:
            click.echo(_LANGFUSE_ENDPOINT_SUMMARY)
        if rebac:
            click.echo(_REBAC_ENDPOINT_SUMMARY)
    else:
        raise click.exceptions.Exit(rc)


@dev.command("down")
@click.option("--volumes", is_flag=True, help="Also remove named volumes.")
def down(volumes: bool) -> None:
    """Stop the dev stack (always tears down the Langfuse profile too)."""
    root = _find_repo_root(Path.cwd())
    _require_compose_file(root)
    cmd = [*_compose_base(root, langfuse=True, rebac=True), "down"]
    if volumes:
        cmd.append("--volumes")
    rc = _run(cmd)
    if rc != 0:
        raise click.exceptions.Exit(rc)


@dev.command("ps")
def ps() -> None:
    """Show status of dev-stack containers."""
    root = _find_repo_root(Path.cwd())
    _require_compose_file(root)
    cmd = [*_compose_base(root, langfuse=True, rebac=True), "ps"]
    rc = _run(cmd)
    if rc != 0:
        raise click.exceptions.Exit(rc)


@dev.command("seed")
def seed() -> None:
    """Idempotently seed smoke-test data (Qdrant collection, OpenBao secret)."""
    root = _find_repo_root(Path.cwd())
    script = _dev_dir(root) / "seed" / "seed.sh"
    rc = _run(["bash", str(script)])
    if rc != 0:
        raise click.exceptions.Exit(rc)
