# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Backup tooling configuration.

Deliberately a plain pydantic ``BaseModel`` (extra='forbid') + ``tomllib`` loader,
NOT an ``NeSettings`` subclass: backup tooling is operational configuration, not
platform config, so it must stay OUT of the platform ``config_hash`` preimage.
CLI flags override any loaded value.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class BackupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Postgres reached via `docker exec` into the pinned pilot container; the
    # in-container unix socket gives trust auth so NO password is ever handled (C12).
    postgres_container: str = "ne-pilot-postgres-1"
    postgres_user: str = "ne"
    # Qdrant over loopback HTTP only (C2 — validated with validate_in_vpc_endpoint).
    qdrant_url: str = "http://127.0.0.1:6333"
    # Persistent volumes captured as cold tars via the pinned helper image. Must be
    # a subset of manifest.VOLUME_ALLOWLIST (enforced at parse/restore time).
    volumes: list[str] = ["caddy-data"]
    # Containers stopped for the destructive phase of a restore (keycloak holds a
    # live pg connection). The drill overrides this to [] (nothing to stop).
    stop_containers: list[str] = ["ne-pilot-keycloak-1"]
    compose_project: str = "ne-pilot"
    # Host-LOCAL plaintext staging root for create/restore (default: the system
    # temp dir via ``tempfile.gettempdir()``). MUST be OFF the backup dest mount so
    # plaintext DB dumps never land on the (often network) customer dest — that is
    # the P2-8 encryption-at-rest fix. Set to a disk-backed path (e.g.
    # ``/var/tmp/ne-backup``) when ``/tmp`` is tmpfs and the DBs are multi-GB.
    staging_dir: str | None = None
    # alpine:3.22 index digest — resolve at build time and record in
    # deploy/backup/README.md provenance. Container tooling only; never a Python dep.
    helper_image: str = (
        "alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce"
    )

    @classmethod
    def load(cls, path: Path | None) -> BackupConfig:
        """Load from a TOML file, or return defaults when ``path`` is None."""
        if path is None:
            return cls()
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)
