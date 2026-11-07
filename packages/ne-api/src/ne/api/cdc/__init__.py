# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CDC sink mount (NE-084) — the ``/cdc/v1`` webhook endpoint on ne-api.

A SEPARATE small FastAPI app (the NE-089 SCIM pattern), mounted OUTSIDE
``build_identity``'s OIDC wrap and gated by its OWN Standard-Webhooks HMAC
signature (NOT OIDC — Debezium is a machine caller). The mount is ABSENT unless
``ApiSettings.cdc_webhook_secret`` is configured (fail-closed default ⇒ 404).
"""

from __future__ import annotations

from ne.api.cdc.app import build_cdc_sink_app

__all__ = ["build_cdc_sink_app"]
