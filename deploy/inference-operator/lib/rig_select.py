# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GPU card-class refusal shared by the Vast adapter (`vast-rig.sh`).

CARD CLASS IS REFUSED BY NAME (`CONSUMER_GPU_DENY_RE`), never by capability
number. A modded 48GB RTX 4090 reports compute capability 8.9 and 49140 MiB of
VRAM — it clears every numeric floor we have — and then dies at vLLM init with
`CUDA error 804: forward compatibility was attempted on non supported HW`,
because the CUDA forward-compatibility layer is datacenter/workstation only. A
numeric filter cannot express "this card lies about what it can do"; a name can.
"""

from __future__ import annotations

import re

# Consumer GeForce card classes, refused BY NAME at selection time.
#
# DERIVATION (2026-08-05, two live rigs): an RTX 4090 modded to 48GB reports
# compute_cap 890 and gpu_ram 49140 — clearing both the SM 8.9 FP8 floor and
# the 48000 MiB VRAM floor — and then fails `cudaGetDeviceCount()` with error
# 804. The forward-compatibility layer the pinned vLLM image relies on is
# datacenter/workstation-GPU only; GeForce silicon rejects it at any vLLM
# version. Because these are also the cheapest 48GB-class parts in existence,
# any cheapest-first selector picks them repeatedly.
#
# SHAPE: the pattern is applied with `re.search` to a CASE-FOLDED, whitespace-
# STRIPPED copy of the card name (see `_is_denied_card`), so it survives the
# spellings providers actually use — "RTX 4090", "RTX4090", "GeForce RTX 4090",
# "RTX 4090D", "RTX 3090 Ti", "RTX 4090 48GB". The digits are matched as a
# bounded family (20xx-50xx GeForce tiers plus GTX) rather than a literal list,
# so a future RTX 6090 is refused without an edit.
#
# WHAT IT DELIBERATELY DOES NOT MATCH, and why that is the hard part: the
# professional cards whose names merely LOOK like GeForce — "RTX 6000 Ada",
# "RTX A6000", "RTX PRO 6000", "RTX 5880 Ada", "Quadro RTX 8000". These are the
# cards we actually want, one of which (RTX 6000 Ada) demonstrably served this
# model on 2026-08-04. The discriminator is that a GeForce tier number is
# exactly four digits ending in a non-zero-padded hundreds digit (4090, 3090,
# 5090, 2080) whereas the professional parts are either four digits ending
# `000`/`880` or carry an `A`/`PRO`/`Quadro` marker. Encoded below as: a
# four-digit number whose last two digits are `60`, `70`, `80` or `90` AND
# which is not preceded by a professional marker.
#
# DECLARED ENUMERATION RESIDUAL (docs/testing/vacuous-guards.md V3): this is a
# NAME test, not a capability probe. A GeForce SKU spelled entirely outside
# this family slips through selection — which is exactly why `doctor`'s
# on-box CUDA-init probe independently catches error 804 in seconds, before
# any weight pull. Neither layer is trusted alone.
CONSUMER_GPU_DENY_RE = re.compile(
    r"""
    (?:^|[^a-z0-9])                 # start, or a non-alphanumeric boundary
    (?:geforce[^a-z0-9]*)?          # optional explicit brand
    (?:rtx|gtx)                     # the consumer brands
    [^a-z0-9]*                      # optional separator ("RTX 4090" / "RTX4090")
    (?![a-z])                       # not "RTX A6000" / "RTX PRO 6000"
    \d{2}[6-9]0                     # 4-digit tier ending 60/70/80/90
    (?![0-9])                       # not a longer number
    """,
    re.VERBOSE,
)

# Professional markers that veto the deny pattern outright. Checked BEFORE the
# regex so that a name carrying one can never be refused, no matter how the
# rest of it is spelled. "RTX PRO 6000" ends in 6000 (not a 60/70/80/90 tier)
# so the regex already misses it; this list is the belt to that braces, and it
# is what makes adding a new professional part safe.
PROFESSIONAL_CARD_MARKERS = ("quadro", "rtx pro", "rtx a", "ada", "tesla", "sxm")


def _is_denied_card(card: str) -> bool:
    """True when `card` names a consumer GeForce part we refuse to rent."""
    folded = " ".join(card.lower().split())
    if any(marker in folded for marker in PROFESSIONAL_CARD_MARKERS):
        return False
    return CONSUMER_GPU_DENY_RE.search(folded) is not None
