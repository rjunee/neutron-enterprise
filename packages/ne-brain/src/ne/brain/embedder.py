# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TEI (Text Embeddings Inference) dense embedder.

Implements :class:`ne.contracts.knowledge.Embedder` over TEI's HTTP ``/embed``
endpoint (Apache-2.0 server; consumed as a separate deployed service — no new
Python dependency beyond ``httpx``). The dev-stack pins
``ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.3`` serving
``Qwen/Qwen3-Embedding-0.6B`` (1024 dims) on port 8085.

C2 (sovereignty/egress): the config-supplied ``base_url`` is validated at
CONSTRUCTION time (:func:`ne.brain.sovereignty.validate_in_vpc_endpoint` — SSRF
shape + in-VPC/allowlist, fail-closed). A bad or exfil-shaped endpoint raises
:class:`~ne.brain.sovereignty.UnsafeEndpointError` and this embedder never POSTs
a byte of corpus text to it. The ``allowed_hosts`` escape hatch admits a host the
in-VPC shape rules would otherwise refuse: a private-DNS name that is not dotless
/ ``.local`` / ``.internal``, **or a public IP literal** (the rented-rig shape).
The structural guardian in ``tests/test_c2_egress_guardian.py`` keeps this the
only sanctioned outbound-HTTP shape in ne-brain.

AIR_GAP dominates the grant ENTIRELY — under an engaged gap a host admitted only
by ``allowed_hosts`` is refused whether it is an IP literal or a DNS name, and
only provably in-VPC shapes pass. It is checked TWICE, because the two moments
differ — the same rule and the same reason as
:mod:`ne.brain.reranker` (:func:`ne.net.ssrf.allowlist_revoked_by_air_gap` is the
single implementation):

* at **construction**, inside ``validate_in_vpc_endpoint``; and
* before **every batch**, because the platform's air gap is a *runtime* posture.
  A governed ``write.internal.network.egress.restrict`` flip engages it in a
  process whose embedder was built hours earlier, and an embedder validated
  while the gap was open would otherwise keep POSTing corpus text after it
  closed. Construction-time validation cannot see a later flip.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import Callable, Sequence
from urllib.parse import urlparse

import httpx

from ne.brain.sovereignty import (
    air_gap_posture,
    assert_air_gap_permits,
    validate_in_vpc_endpoint,
)

_DEFAULT_MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
_DEFAULT_DIMENSION = 1024

#: Deadline for the TCP HANDSHAKE ALONE, in seconds. Same value, same reasoning
#: and same non-configurability as :data:`ne.brain.reranker._CONNECT_TIMEOUT_S`,
#: which carries the full justification (a single ``timeout=`` scalar makes the
#: operator's long READ budget the connect deadline too, and against a
#: black-holed address the kernel's ~130 s SYN-retry ceiling then fires first).
#: The embed lane sets its own budget via ``NE_TEI_EMBED_TIMEOUT_S`` and shares
#: the defect exactly. ``test_dead_backend_fails_fast.py`` pins the two equal so
#: the sibling copies cannot drift.
_CONNECT_TIMEOUT_S = 5.0


def _client_timeout(read_timeout: float) -> httpx.Timeout:
    """Per-phase deadlines: the operator's budget everywhere except connect."""
    return httpx.Timeout(read_timeout, connect=_CONNECT_TIMEOUT_S)


def _endpoint_label(base_url: str) -> str:
    """``scheme://host:port`` for an error message — host and port ONLY.

    Rebuilt from the PARSED url, so userinfo/path/query structurally cannot ride
    along into a log line. See the reranker's copy for the full reasoning.
    """
    parsed = urlparse(base_url)
    host = parsed.hostname or "?"
    port = parsed.port
    return f"{parsed.scheme}://{host}:{port}" if port else f"{parsed.scheme}://{host}"


class EmbedBackendUnavailableError(httpx.TransportError):
    """An embed backend could not be reached, and this error NAMES it.

    A distinct class from the reranker's so ne-api's class-name-only 502 detail
    says WHICH lane died; a ``httpx.TransportError`` subclass so it keeps
    mapping to that 502 through ``_BACKEND_ERRORS``. The endpoint is in the
    message, which reaches the server log and never the response body.
    """


class TEIEmbedder:
    """Dense embedder client for a TEI server. Implements ``Embedder``."""

    def __init__(
        self,
        base_url: str,
        *,
        model_id: str = _DEFAULT_MODEL_ID,
        dimension: int = _DEFAULT_DIMENSION,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        batch_size: int = 32,
        allowed_hosts: Sequence[str] = (),
        air_gap: Callable[[], bool] | None = None,
    ) -> None:
        # C2: validate the config-supplied endpoint at construction (SSRF +
        # in-VPC/allowlist, fail-closed). A bad endpoint raises UnsafeEndpointError
        # and never receives a byte of corpus text. `allowed_hosts` admits a
        # private-DNS name or a public IP literal; an engaged air gap revokes
        # either, here AND before every batch (see module docstring).
        self._base_url = validate_in_vpc_endpoint(
            base_url, allowed_hosts=allowed_hosts, air_gap=air_gap_posture(air_gap)
        )
        self._air_gap = air_gap
        self._model_id = model_id
        self._dimension = dimension
        self._timeout = timeout
        self._batch_size = batch_size
        self._owns_client = client is None
        self._client = client
        #: Owned clients, ONE PER EVENT LOOP (2026-08-28 cron-offload incident):
        #: an ``httpx.AsyncClient`` binds its pool to the first loop that uses
        #: it, and this composed embedder is shared by ne-worker's DBOS turn
        #: loop (query embed) and its persistent cron-tick loop (connector-sync
        #: chunk embed). Weak keys drop a departed loop's client. An INJECTED
        #: client keeps single-instance behavior (single-loop tests/compositions).
        self._owned_by_loop: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, httpx.AsyncClient
        ] = weakref.WeakKeyDictionary()
        self._owned_lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return self._dimension

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        loop = asyncio.get_running_loop()
        with self._owned_lock:
            owned = self._owned_by_loop.get(loop)
            if owned is None:
                owned = httpx.AsyncClient(timeout=_client_timeout(self._timeout))
                self._owned_by_loop[loop] = owned
            return owned

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        client = self._get_client()
        vectors: list[list[float]] = []
        items = list(texts)
        for start in range(0, len(items), self._batch_size):
            # INSIDE the loop, not before it: a corpus ingest is many batches over
            # many seconds, and a governed flip lands between two of them. Checking
            # once before the loop refuses the first batch and lets the rest out.
            assert_air_gap_permits(self._base_url, self._air_gap, what="corpus text")
            batch = items[start : start + self._batch_size]
            try:
                response = await client.post(
                    f"{self._base_url}/embed",
                    json={"inputs": batch, "normalize": True},
                )
            except httpx.TransportError as exc:
                try:
                    request = exc.request
                except RuntimeError:  # httpx raises when the transport never set one
                    request = None
                raise EmbedBackendUnavailableError(
                    f"embed (TEI) backend {_endpoint_label(self._base_url)} unreachable: "
                    f"{type(exc).__name__}: {exc}",
                    request=request,
                ) from exc
            response.raise_for_status()
            vectors.extend(response.json())
        return vectors

    async def aclose(self) -> None:
        """Close the CURRENT loop's owned client. A no-op when injected.

        Owned clients on other loops can only be closed from their own loop;
        they are weakly held and dropped with their loop.
        """
        if not self._owns_client:
            return
        loop = asyncio.get_running_loop()
        with self._owned_lock:
            owned = self._owned_by_loop.pop(loop, None)
        if owned is not None:
            await owned.aclose()
