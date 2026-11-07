# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy for ne-obs (KPI/U-P-B reporting, NE-068)."""

from __future__ import annotations


class ObsError(Exception):
    """Base class for every error raised by ne-obs."""


class ObsReportError(ObsError):
    """A KPI/U-P-B report could not be built or configured.

    Raised for a malformed KPI-lead configuration string, or a missing
    read-source database URL when constructing report data sources from
    settings (fail loud rather than silently defaulting).
    """
