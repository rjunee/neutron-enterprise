# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed errors for ne-backup.

The core (crypto/tarsafe/manifest/runtime) stays click-free and raises these
typed errors; :mod:`ne.backup.cli` maps them to ``click.ClickException`` at the
surface. This keeps the hardened core unit-testable without a Click dependency
in the assertion path.
"""

from __future__ import annotations


class NeBackupError(Exception):
    """Base class for every ne-backup error."""


class ManifestError(NeBackupError):
    """A manifest is malformed, names an out-of-allowlist volume, an invalid SQL
    identifier, or an unsafe artifact name."""


class UnsafeArchiveError(NeBackupError):
    """A tar member (or its link target) would escape the extraction root."""


class KeyResolutionError(NeBackupError):
    """The customer KEK could not be resolved from the SecretStore, or it does
    not decode to exactly 32 bytes."""


class KeyExistsError(NeBackupError):
    """``keygen`` was asked to provision a KEK, but one already exists in the
    bundle SecretStore. Overwriting it would orphan every backup made under the
    old key (they fail the fingerprint check and become unrestorable), so a
    deliberate rotation flag is required to replace it."""


class BackupCryptoError(NeBackupError):
    """A structural failure in the NEB1 envelope (bad magic / truncated frame).

    AEAD authentication failures surface as ``cryptography.exceptions.InvalidTag``
    directly so callers can distinguish tamper/truncation/wrong-key from a
    malformed container.
    """


class RestoreError(NeBackupError):
    """A restore aborted (pre-flight validation failed, or a destructive step
    failed partway through)."""
