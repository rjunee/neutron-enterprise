# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed systemd condition for provider-network activation."""

from __future__ import annotations

from pathlib import Path

from ne.inference_operator.cli import _air_gap_from_file

AIR_GAP_POLICY = Path("/etc/neutron/inference-operator/environment")
PROVIDER_GID = 10002


def provider_control_enabled(
    path: Path = AIR_GAP_POLICY,
    *,
    owner_uid: int = 0,
    reader_gid: int = PROVIDER_GID,
) -> bool:
    """Return true only for the exact safe, explicit AIR_GAP=false policy."""
    try:
        return not _air_gap_from_file(path, owner_uid=owner_uid, reader_gid=reader_gid)
    except (Exception, SystemExit):
        return False


def main() -> None:
    raise SystemExit(0 if provider_control_enabled() else 1)


if __name__ == "__main__":
    main()
