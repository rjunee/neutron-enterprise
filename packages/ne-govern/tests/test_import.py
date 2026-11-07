# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test + public-surface guarantees for ne-govern."""

from __future__ import annotations

import subprocess
import sys

import ne.govern


def test_import_and_version() -> None:
    assert ne.govern.__version__ == "0.1.0"


def test_public_surface_exports_pdp_and_factory() -> None:
    assert "GovernedPdp" in ne.govern.__all__
    assert "create_pdp" in ne.govern.__all__


def test_opa_client_is_not_exported() -> None:
    # OpaClient (and _RawOpaResult) must never be part of the public surface:
    # only the audited GovernedPdp can mint a PolicyDecision (C6).
    assert "OpaClient" not in ne.govern.__all__
    assert not hasattr(ne.govern, "OpaClient")
    assert not hasattr(ne.govern, "_RawOpaResult")


def test_cedar_is_lazy_and_unexported() -> None:
    # The cedarpy PDP alternate (NE-092) is an OPTIONAL [cedar] extra: it must
    # never be part of ne.govern's public surface, and importing ne.govern
    # alone must never pull in cedarpy or ne.govern.cedar.
    assert "CedarGovernedPdp" not in ne.govern.__all__
    assert not hasattr(ne.govern, "CedarGovernedPdp")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ne.govern, sys; "
            "raise SystemExit(1 if ('cedarpy' in sys.modules or "
            "'ne.govern.cedar' in sys.modules) else 0)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
