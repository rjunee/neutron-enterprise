# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Confined host-side inference target lifecycle operator."""

from ne.inference_operator.manifest import PrivateManifest, load_private_manifest
from ne.inference_operator.operator import HostInferenceTargetOperator

__all__ = ["HostInferenceTargetOperator", "PrivateManifest", "load_private_manifest"]
