# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Reference in-memory adapters and the contract-conformance test-kit pattern.

This package intentionally imports nothing from :mod:`ne.contracts.testing.kits`
at import time, so importing :mod:`ne.contracts.testing.memory` (the dummy
adapters, used as real entry-point plugins) never requires ``pytest``. The
source-authority kits are stdlib-only and therefore safe to export here; older
pytest-backed kits remain under :mod:`ne.contracts.testing.kits`.
"""

from ne.contracts.testing.source_authority import (
    AuthorizedContinuationTokenCodecContractKit,
    CurrentAuthorizedArtifactSelectorContractKit,
    CurrentSourceAuthorityReaderContractKit,
    CurrentSourceAuthorityWriterContractKit,
    ImmutableKnowledgeArtifactWriteContractKit,
    ProtectedSourceExecutionWriterContractKit,
    ProtectedSourceOutputWriterContractKit,
    SourceBoundEvidenceProjectionResolverContractKit,
    SourceBoundResultResolverContractKit,
)

__all__ = [
    "AuthorizedContinuationTokenCodecContractKit",
    "CurrentAuthorizedArtifactSelectorContractKit",
    "CurrentSourceAuthorityReaderContractKit",
    "CurrentSourceAuthorityWriterContractKit",
    "ImmutableKnowledgeArtifactWriteContractKit",
    "ProtectedSourceExecutionWriterContractKit",
    "ProtectedSourceOutputWriterContractKit",
    "SourceBoundEvidenceProjectionResolverContractKit",
    "SourceBoundResultResolverContractKit",
]
