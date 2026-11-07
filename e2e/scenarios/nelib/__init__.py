# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Support library for the demo-scenario E2E suite (``e2e/scenarios``).

Everything here drives the GOVERNED HTTP surface only — Keycloak password
grants and the ``/api/v1`` routers. No CLI, no direct PostgreSQL, exactly like
the S9 reproducibility trials this suite grew out of
(``research/s9-trials-2026-08-28/driver-agentic_run.py`` in the project tree).
"""
