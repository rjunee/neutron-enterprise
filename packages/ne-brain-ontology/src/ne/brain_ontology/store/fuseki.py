# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Portions derived from jupyter-naas/abi
# libs/naas-abi-core/naas_abi_core/services/triple_store/adaptors/secondary/
# ApacheJenaTDB2.py @ a3e15eff03902091f23acfd9edbc95dc9695183f — MIT License,
# Copyright (C) 2025 Naas.ai. De-naased: the ABI adapter's operational knowledge
# (Fuseki/TDB2 is Multi-Reader/Single-Writer; transient write-lock contention
# surfaces as HTTP 500/503 and is retried with exponential backoff + jitter;
# 4xx are non-retryable; a boot-time ASK probe verifies reachability) is carried
# over. Everything else is rewritten fresh: sync `requests`+SPARQL-Update writes
# become async httpx over the SPARQL 1.1 Graph Store Protocol; the ABI
# service-container / event-bus / KeyValueService coupling is dropped;
# OntologySettings replaces ABI config objects.
"""Apache Jena Fuseki (TDB2) production backend over HTTP (NE-085a).

``FusekiBackend`` implements the package-local
:class:`~ne.brain_ontology.store.protocols.TripleStoreBackend` against a Fuseki
DATASET endpoint (e.g. ``http://fuseki:3030/ne``). Reads go to ``{url}/query``;
writes go to ``{url}/data`` via the SPARQL 1.1 Graph Store Protocol (POST=add,
PUT=replace, DELETE=drop). Fuseki itself is NOT distributed with this package —
it is an out-of-process service accessed over the version-stable SPARQL 1.1
protocol (adapter validated by the LIVE-PARKED conformance lane; unit-tested via
``httpx.MockTransport``).

C2: the adapter only ever talks to the operator-configured in-VPC URL and never
issues SPARQL ``LOAD`` / ``SERVICE`` — the gateway's deterministic pre-check and
the GSP write path make network egress structurally impossible.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, cast

import httpx

from .protocols import DatasetSpec

_SPARQL_RESULTS_JSON = "application/sparql-results+json"
_N_TRIPLES = "application/n-triples"
_TURTLE = "text/turtle"
_RETRYABLE_STATUS = frozenset({500, 503})


class FusekiError(RuntimeError):
    """A Fuseki HTTP call failed (non-retryable status, or retries exhausted)."""

    def __init__(self, operation: str, status_code: int | None, attempts: int) -> None:
        super().__init__(f"Fuseki {operation} failed (status={status_code}, attempts={attempts})")
        self.operation = operation
        self.status_code = status_code
        self.attempts = attempts


class FusekiBackend:
    """Async httpx backend for a Fuseki/TDB2 dataset endpoint."""

    #: 015A0-AUTHORITY-T3 negative capability. An out-of-process HTTP dataset cannot
    #: hand a governed read a Store object whose bytes are fixed for the life of that
    #: object: between the primary, count and citation queries of ONE source-bearing
    #: invocation another writer may commit, and observing matching hashes either side
    #: of an unfenced query is not proof of a snapshot. So source-bearing governed
    #: local T3 is UNAVAILABLE on Fuseki, refused before any HTTP request is issued.
    #: This is a capability declaration, not a claim that Fuseki is unsupported for
    #: the ordinary (non-source-bearing) T3 read/write surface, which is unchanged.
    supports_immutable_release_snapshot: ClassVar[bool] = False

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        auth: tuple[str, str] | None = None,
        timeout: float = 60.0,
        retry_attempts: int = 5,
        retry_base_delay_s: float = 0.1,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._query_endpoint = f"{self._base}/query"
        self._data_endpoint = f"{self._base}/data"
        self._retry_attempts = retry_attempts
        self._retry_base_delay_s = retry_base_delay_s
        self._client = client or httpx.AsyncClient(
            auth=httpx.BasicAuth(*auth) if auth is not None else None,
            timeout=timeout,
        )

    # -- retry (MRSW ops knowledge, de-naased) --------------------------------

    async def _write(
        self, method: str, *, graph_iri: str, content: bytes, content_type: str, operation: str
    ) -> None:
        """Issue a Graph Store Protocol write, retrying transient 500/503.

        Fuseki/TDB2 holds a single-writer lock at the dataset level; a write
        blocked by another transaction returns 500/503 transiently. We retry with
        exponential backoff + jitter, bounded by ``retry_attempts``. 4xx (and any
        other non-retryable status) raise a typed :class:`FusekiError` at once.
        """
        params = {"graph": graph_iri}
        for attempt in range(self._retry_attempts + 1):
            response = await self._client.request(
                method,
                self._data_endpoint,
                params=params,
                content=content,
                headers={"Content-Type": content_type},
            )
            if response.status_code in _RETRYABLE_STATUS and attempt < self._retry_attempts:
                await self._backoff(attempt)
                continue
            if response.status_code >= 400:
                raise FusekiError(operation, response.status_code, attempt + 1)
            return
        raise AssertionError("unreachable: write retry loop must return or raise")

    async def _post_query(
        self, query: str, *, dataset: DatasetSpec, accept: str, operation: str
    ) -> httpx.Response:
        """POST a read query, retrying transient 500/503. Reads are not locked."""
        # httpx's QueryParams typing is invariant over list[tuple]; the runtime
        # accepts our (str, str) pairs (repeatable graph-uri params).
        params = httpx.QueryParams(cast("Any", _dataset_params(dataset)))
        for attempt in range(self._retry_attempts + 1):
            response = await self._client.post(
                self._query_endpoint,
                params=params,
                content=query.encode("utf-8"),
                headers={"Content-Type": "application/sparql-query", "Accept": accept},
            )
            if response.status_code in _RETRYABLE_STATUS and attempt < self._retry_attempts:
                await self._backoff(attempt)
                continue
            if response.status_code >= 400:
                raise FusekiError(operation, response.status_code, attempt + 1)
            return response
        raise AssertionError("unreachable: query retry loop must return or raise")

    async def _backoff(self, attempt: int) -> None:
        delay = self._retry_base_delay_s * (2**attempt) + random.uniform(0, 0.1)
        await asyncio.sleep(delay)

    # -- TripleStoreBackend ----------------------------------------------------

    async def select(self, query: str, *, dataset: DatasetSpec) -> Sequence[Mapping[str, str]]:
        response = await self._post_query(
            query, dataset=dataset, accept=_SPARQL_RESULTS_JSON, operation="select"
        )
        data = json.loads(response.text)
        variables = data.get("head", {}).get("vars", [])
        rows: list[dict[str, str]] = []
        for binding in data.get("results", {}).get("bindings", []):
            row: dict[str, str] = {}
            for var in variables:
                if var in binding:
                    row[var] = binding[var]["value"]
            rows.append(row)
        return rows

    async def ask(self, query: str, *, dataset: DatasetSpec) -> bool:
        response = await self._post_query(
            query, dataset=dataset, accept=_SPARQL_RESULTS_JSON, operation="ask"
        )
        return bool(json.loads(response.text).get("boolean", False))

    async def construct_nt(self, query: str, *, dataset: DatasetSpec) -> bytes:
        response = await self._post_query(
            query, dataset=dataset, accept=_N_TRIPLES, operation="construct"
        )
        return response.content

    async def insert_nt(self, data: bytes, *, graph_iri: str) -> None:
        await self._write(
            "POST", graph_iri=graph_iri, content=data, content_type=_N_TRIPLES, operation="insert"
        )

    async def load_turtle(self, data: bytes, *, graph_iri: str) -> None:
        await self._write(
            "POST", graph_iri=graph_iri, content=data, content_type=_TURTLE, operation="load"
        )

    async def replace_graph_turtle(self, data: bytes, *, graph_iri: str) -> None:
        await self._write(
            "PUT", graph_iri=graph_iri, content=data, content_type=_TURTLE, operation="replace"
        )

    async def delete_graph(self, graph_iri: str) -> None:
        for attempt in range(self._retry_attempts + 1):
            response = await self._client.delete(self._data_endpoint, params={"graph": graph_iri})
            if response.status_code in _RETRYABLE_STATUS and attempt < self._retry_attempts:
                await self._backoff(attempt)
                continue
            # 404 = graph already absent; treat as a successful no-op delete.
            if response.status_code == 404:
                return
            if response.status_code >= 400:
                raise FusekiError("delete_graph", response.status_code, attempt + 1)
            return

    async def graphs(self) -> Sequence[str]:
        rows = await self.select(
            "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }",
            dataset=DatasetSpec(),
        )
        return [row["g"] for row in rows if "g" in row]

    async def health(self) -> bool:
        try:
            return await self.ask("ASK {}", dataset=DatasetSpec())
        except (FusekiError, httpx.HTTPError):
            return False

    async def aclose(self) -> None:
        await self._client.aclose()


def _dataset_params(dataset: DatasetSpec) -> list[tuple[str, str]]:
    """Map a :class:`DatasetSpec` to Fuseki query-protocol graph params.

    Empty spec → no params (the dataset's configured union default graph applies;
    the deploy runbook sets ``tdb2:unionDefaultGraph true``). Otherwise emit one
    ``default-graph-uri`` / ``named-graph-uri`` per graph.
    """
    params: list[tuple[str, str]] = []
    params.extend(("default-graph-uri", g) for g in dataset.default_graphs)
    params.extend(("named-graph-uri", g) for g in dataset.named_graphs)
    return params
