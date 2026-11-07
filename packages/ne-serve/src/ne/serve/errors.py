# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy for ne-serve (the in-VPC vLLM serving client).

All failures raised by :class:`ne.serve.client.ServeClient` derive from
:class:`ServeError`, so callers can catch the whole surface with one except.
"""

from __future__ import annotations


class ServeError(Exception):
    """Base class for every error raised by ne-serve."""


class ServeConnectionError(ServeError):
    """The vLLM OpenAI-compatible server could not be reached or errored."""


class ServeHealthError(ServeError):
    """The server's ``/health`` probe did not return 200 (not ready)."""


class StructuredOutputViolationError(ServeError):
    """A structured-output response body was not parseable as JSON.

    Raised by :meth:`ServeClient.generate_json` when the model's returned
    content cannot be ``json.loads``-ed despite a ``json_schema`` response
    format having been requested.
    """


class UnsafeEndpointError(ServeError):
    """The config-supplied serve endpoint is SSRF-shaped or not provably in-VPC.

    Raised at :class:`ne.serve.client.ServeClient` CONSTRUCTION time
    (fail-closed, C2) by :func:`ne.serve.sovereignty.validate_in_vpc_endpoint`
    so a bad/exfil ``base_url`` never receives a single byte. Messages name ONLY
    the parsed hostname, never the full URL, so a credentialed endpoint cannot
    leak a secret into logs.
    """


class WeightIntegrityError(ServeError):
    """Air-gap weight staging or verification failed (fail-closed at the boundary).

    Raised by :func:`ne.serve.download.download_weights` /
    :func:`ne.serve.download.verify_weights` on an unresolvable revision, a
    missing / tampered / extra staged file, or a pre-v2 manifest — the one place
    the C11 digest-pinning story otherwise breaks (the air-gap boundary).
    """
