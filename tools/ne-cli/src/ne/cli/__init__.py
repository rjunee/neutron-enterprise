# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The `ne` developer CLI for the Neutron Enterprise monorepo."""

from __future__ import annotations

import click

from ne.cli.agent_private_keys import private_payload_keys
from ne.cli.agent_transport import agent_transport
from ne.cli.audit import audit
from ne.cli.checks import check
from ne.cli.connect import connect
from ne.cli.demo import demo
from ne.cli.dev import dev
from ne.cli.k8s import k8s
from ne.cli.licenses import licenses
from ne.cli.lockcheck import lock
from ne.cli.migrate import migrate
from ne.cli.ontology import ontology
from ne.cli.permissions import permissions
from ne.cli.pilotcfg import pilot
from ne.cli.release import release
from ne.cli.schema import schema
from ne.cli.source_authority_keys import source_authority_keys
from ne.cli.up import down, up

__version__ = "0.1.0"

# Register the supply-chain policy checks on the existing `ne check` group.
check.add_command(licenses)
check.add_command(lock)


@click.group()
@click.version_option(__version__, prog_name="ne")
def main() -> None:
    """Neutron Enterprise developer CLI."""


@main.command("version")
def version() -> None:
    """Print the ne CLI version."""
    click.echo(__version__)


main.add_command(check)
main.add_command(release)
main.add_command(dev)
main.add_command(schema)
main.add_command(audit)
main.add_command(migrate)
main.add_command(up)
main.add_command(down)
main.add_command(pilot)
main.add_command(k8s)
main.add_command(connect)
main.add_command(permissions)
main.add_command(ontology)
main.add_command(demo)
main.add_command(private_payload_keys)
main.add_command(agent_transport)
main.add_command(source_authority_keys)
