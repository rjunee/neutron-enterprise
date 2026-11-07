# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ObjectStore WORM adapters — the two production cloud backends.

``s3`` (:class:`~ne.storage.s3.S3ObjectStore`) is backed by S3 Object Lock in
COMPLIANCE mode; ``azure_blob``
(:class:`~ne.storage.azure_blob.AzureBlobObjectStore`) is backed by Azure Blob
version-level immutability policies in ``Locked`` mode. Azure Blob is NOT
S3-compatible, so each cloud needs its own adapter (SPEC C9). Both register as
``ne.plugins.object_store`` entry points and resolve by NAME, so an operator
picks the cloud purely by config
(``NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE=s3|azure_blob``).
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.storage.azure_blob import AzureBlobObjectStore
from ne.storage.config import AzureBlobObjectStoreSettings, S3ObjectStoreSettings
from ne.storage.s3 import (
    GovernanceUpgradeRefusedError,
    ObjectLockNotEnabledError,
    ObjectLockPermissionError,
    S3ObjectStore,
    TooManyObjectVersionsError,
)

__all__ = [
    "AzureBlobObjectStore",
    "AzureBlobObjectStoreSettings",
    "GovernanceUpgradeRefusedError",
    "ObjectLockNotEnabledError",
    "ObjectLockPermissionError",
    "S3ObjectStore",
    "S3ObjectStoreSettings",
    "TooManyObjectVersionsError",
    "__version__",
]
