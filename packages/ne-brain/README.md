# ne-brain

Tier-1 hybrid RAG — Docling parse, structure-aware chunking, dense+sparse retrieval with server-side fusion, rerank, citations, and the governed KnowledgeWrite surface.

Status: the Qdrant store adapter ships with NE-021; the production retrieval
pipeline (`RetrievalPipeline` = hybrid retrieve -> TEI rerank -> citations, plus
the `TEIReranker` adapter) ships with NE-022; the ingest stages (Docling parse,
structure-aware chunking, optional contextual enrichment) ship with NE-020.

## Ingestion stages (NE-020)

Three composable stages under `ne.brain.ingest`. NE-026 orchestrates them into
an idempotent/resumable pipeline; this package only ships the stage contracts
and the stage implementations, not the orchestrator:

```
document
  -> PARSE   (DoclingParser)         -> ParsedDocument (DocBlock*)
  -> CHUNK   (StructuralChunker)     -> tuple[Chunk, ...]           (frozen ne-contracts Chunk)
  -> ENRICH  (ContextualEnricher)    -> tuple[Chunk, ...]           (OPTIONAL — off by default)
       |
       v
  NE-026 pipeline spine (idempotent/resumable) -> NE-023 KnowledgeWrite
```

### PARSE contract

`ne.brain.ingest.contracts.ParsedDocument` (`schema_version = "ne.parsed_document/1"`)
is a package-local, parser-agnostic stage contract — NOT a `ne-contracts`
addition (`ne-contracts` stays FROZEN). It carries a flat, document-ordered
tuple of `DocBlock`s: `kind` (`heading|paragraph|table|list_item|code|caption|
other`), `text`, `level` (heading level, `None` otherwise), and `page`
(1-based page from parser provenance, `None` when the source format has no
pages, e.g. HTML). `DocumentParser.parse(path, *, document_id) -> ParsedDocument`
is the seam any parser implementation (docling today; docling-serve HTTP or
another backend later) slots into without touching chunk/enrich (C5).

### CHUNK: `StructuralChunker`

Structure-aware, NOT naive fixed-size chunking (`max_chars=1800`,
`min_chars=200`, `include_headings_in_text=True`, all overridable):

* A heading stack tracks the current heading path; a `HEADING` block of level
  `L` pops every stacked heading `>= L`, then pushes. A heading whose section
  produced at least one chunk is "covered" and updates context only; a heading
  popped (or reached at end of document) while still UNCOVERED — a title-only,
  trailing, or immediately-replaced sibling heading — is emitted as its own
  `text` chunk (ancestor-path metadata only) so its text is never lost to
  retrieval (NE-020 P2b fold; nested body-less headings collapse into one
  ancestor-prefixed chunk).
* `TABLE` blocks are ATOMIC: the running buffer is always flushed first, then
  the table is emitted as its own chunk (`metadata['content_type'] = 'table'`)
  — never split, even past `max_chars`.
* Other block kinds accumulate into a section buffer, flushed on a heading-path
  change or when appending would exceed `max_chars`.
* A single block longer than `max_chars` is split at sentence boundaries
  (`r'(?<=[.!?])\s+'`), hard-split as a last resort.
* A post-flush fragment shorter than `min_chars` merges backward into the
  immediately preceding chunk if it shares the same heading path and is itself
  a text chunk (never a table).

Every emitted chunk is a frozen `ne.contracts.types.Chunk` whose metadata
carries `headings` (outermost -> innermost tuple), `pages` (sorted tuple of
non-`None` constituent-block pages — the ABI LIFT-PATTERN teardown §3.3
page-number-in-chunk-metadata pattern), `content_type`, `sequence`, and
`source_name`/`source_format`. `chunk_id = f'{document_id}:{i:04d}'` with `i`
the running emission index: chunking is a pure function of
`(ParsedDocument, params)`, so ids/order/text are deterministic — NE-026's
idempotent resume depends on this.

### ENRICH (optional): `ContextualEnricher`

Off by default (`enabled=False`, identity — the injected gateway is never
called). When enabled, it implements the Anthropic contextual-retrieval
pattern: one `CompletionRequest(task="ingest.enrich")` per chunk, asking a
model for 1-2 sentences situating the chunk within the document, then
prepending that context to the chunk's text before it is embedded/indexed.
Concurrency is bounded (`max_concurrency`, default 4) via an `asyncio.Semaphore`;
`asyncio.gather` preserves input order. Gateway errors PROPAGATE — quarantine/
resume semantics belong to NE-026, not this stage.

**C3 note:** this is the *only* stage that calls a model, and it does so
exclusively through the constructor-injected `ne.contracts.models.ModelGateway`
PROTOCOL. `ne.brain` imports no provider SDK and no `ne.gateway` concrete
implementation anywhere (`grep -rn 'litellm\|openai\|anthropic\|ne.gateway'
packages/ne-brain/src` is empty; regression-guarded by
`test_no_provider_sdk_or_gateway_import`).

### Isolation rationale — why docling lives in its own venv (C4)

`docling` itself is MIT, but its `standard` extra (needed for the real layout
pipeline) pulls `torch`, and on linux `torch` in turn pulls **proprietary,
platform-conditional NVIDIA CUDA runtime wheels** (`nvidia-cublas`,
`nvidia-cudnn-cu13`, `nvidia-nccl-cu13`, ...) — these can never classify as
license-gate-clean (`FREE`) in a universal platform lock scanned by the C4
license gate. An `optional-dependencies` extra on `ne-brain`'s own
`pyproject.toml` would *still* resolve into the shared `uv.lock` the gate
scans, so only full process isolation is C4-clean: `docling` and its entire
transitive closure (including torch/NVIDIA) are pinned + hashed in
`parser_env/requirements.txt` and installed into an isolated venv at
`packages/ne-brain/.parser-venv` (git-ignored, never committed) — exactly the
pattern `ne.schema.codegen` uses to isolate `linkml`. The platform `uv.lock`
gains **zero** new dependencies for NE-020; `test_parser_env_policy.py`
mechanically asserts `uv.lock` contains no `docling`/`docling-core`/
`docling-slim`/`docling-ibm-models`/`torch`/`nvidia-*` distribution.

`ne.brain.ingest.docling_parser` (platform side) imports **nothing** from
docling — it only manages the isolated venv (build/refresh keyed on a
`requirements.txt` sha256 marker, `--require-hashes` install) and runs
`parser_env/parse_doc.py` (the in-venv driver, which does the real
`docling.document_converter.DocumentConverter` call) as a subprocess,
adapting its JSON output to `ParsedDocument`.

A **docling-serve** HTTP deployment (a separately-hosted docling service) is a
possible deploy-tier swap for NE-026/NE-070 that would remove the subprocess
entirely — the `DocumentParser` contract is parser-agnostic either way (C5).

**Locating the parser env outside a monorepo checkout.** `DoclingParser()`
(no args) discovers `parser_env/` and the `.parser-venv` working directory by
walking up from its own file for a directory containing `uv.lock` — this
covers every deployment shape that exists today (dev checkout, CI, and an
OCI image built by copying the whole `uv` workspace in, per SPEC §2 C9). If a
future deployment instead installs `ne-brain` as a standalone wheel with no
workspace tree around it (e.g. a slimmed production image built from
published wheels), that walk-up has nothing to find. For that shape, vendor
`packages/ne-brain/parser_env/` into the image at any path and point
`DoclingParser` at it explicitly — either constructor kwargs:

```python
DoclingParser(parser_env_dir=Path("/opt/ne/parser_env"), venv_dir=Path("/var/lib/ne/parser-venv"))
```

or the equivalent environment variables (used when no `root`/explicit dirs
are passed, e.g. by the `document_parser`/`docling` entry point resolving to
a bare `DoclingParser()`):

```sh
export NE_PARSER_ENV_DIR=/opt/ne/parser_env
export NE_PARSER_VENV_DIR=/var/lib/ne/parser-venv
```

If neither an override nor a monorepo checkout is found, the constructor
raises a `RuntimeError` naming both override mechanisms rather than a bare
"`uv.lock` not found" error.

### Security posture: CVE-2026-24009 / CVE-2026-44018 + format allowlist

Two 2026 docling advisories, both fixed below this package's pin
(`docling-core==2.86.0`; the CVE-2026-24009 fix landed in `2.48.4`):

* **CVE-2026-24009** (CRITICAL, 9.8) — RCE via unsafe PyYAML parsing of
  YAML-format documents.
* **CVE-2026-44018** (moderate) — unsafe archive/XML handling in the
  METS-GBS backend.

Beyond the version pin, `DocumentConverter(allowed_formats=[...])` in
`parser_env/parse_doc.py` hard-allowlists input formats to
**PDF/DOCX/PPTX/HTML/MD** — the YAML and METS-GBS backends are structurally
unreachable regardless of upstream fix status. `docling` is not in the CISA
KEV catalog (checked 2026-07-04). HF Hub telemetry is disabled
(`HF_HUB_DISABLE_TELEMETRY=1`) before docling is imported in the in-venv
driver.

### Fixtures and the live parser test

`tests/fixtures/ingest/sample_report.html` is a real ~2-page HTML document
(heading levels, a nested `<h3>`, a 4x3 `<table>`, a `<ul>` list, and one
section long enough to force a sentence-boundary split). Its recorded
companion, `sample_report.parsed.json`, was produced by an ACTUAL docling
2.110.0 run in the isolated venv:

```sh
uv venv packages/ne-brain/.parser-venv --python 3.12
uv pip install --python packages/ne-brain/.parser-venv/bin/python \
    --require-hashes -r packages/ne-brain/parser_env/requirements.txt
uv run python -m ne.brain.ingest.docling_parser \
    packages/ne-brain/tests/fixtures/ingest/sample_report.html \
    packages/ne-brain/tests/fixtures/ingest/sample_report.parsed.json \
    --document-id sample-report
```

CI never needs the docling runtime for the fixture-based tests (they replay
the committed JSON via `parsed_document_from_json`). The real parser is still
exercised: `tests/test_docling_parse_live.py` re-parses the HTML fixture with
the real `DoclingParser` and asserts the result matches the committed JSON
(proving the fixture is honest), but is **LIVE-PARKED** (module-level skip)
unless `packages/ne-brain/.parser-venv/bin/python` already exists or
`NE_BUILD_PARSER_ENV=1` is set:

```sh
NE_BUILD_PARSER_ENV=1 uv run pytest packages/ne-brain/tests/test_docling_parse_live.py -q
```

`tests/fixtures/ingest/sample_paged.parsed.json` is a hand-authored,
schema-true `ParsedDocument` (headings, a table, and blocks split across pages
1 and 2 within one heading) that exercises the `pages` metadata path in CI,
since the HTML fixture — like all HTML — carries no page provenance.

### Entry points (plugin slots, NE-020 additions)

| Slot              | Name         | Class                                     |
|-------------------|--------------|--------------------------------------------|
| `document_parser`  | `docling`    | `ne.brain.ingest.docling_parser.DoclingParser` |
| `chunker`          | `structural` | `ne.brain.ingest.chunker.StructuralChunker`    |
| `enricher`         | `contextual` | `ne.brain.ingest.enricher.ContextualEnricher`  |

## Qdrant store adapter (NE-021)

`ne.brain.qdrant.QdrantChunkStore` implements `ne.contracts.knowledge.ChunkStore`
over a single Qdrant collection with **two named vectors per point**:

* `dense` — cosine distance, sized to the injected `Embedder`'s `dimension`
  (Qwen3-Embedding-0.6B / 1024 dims via `ne.brain.embedder.TEIEmbedder`, which
  calls a TEI HTTP server — no model runs in-process).
* `sparse` — BM25 term weights produced **client-side** by
  `ne.brain.sparse.FastEmbedBM25Encoder` (FastEmbed's `Qdrant/bm25` model) at
  BOTH ingest (`encode`, document mode) and query time (`encode_queries`,
  query mode, flat weight-1 tokens). The collection's sparse vector params set
  `Modifier.IDF`, so the actual BM25 IDF term is applied **server-side** by
  Qdrant.

Hybrid queries (`ChunkStore.query(dense=..., sparse=...)`) issue **one**
`query_points` call: a `dense` prefetch, a `sparse` prefetch, and an outer
`FusionQuery(fusion=Fusion.RRF)` — fusion is entirely server-side, never
client-merged. Single-signal queries (`dense=...` xor `sparse=...`) issue a
plain `query_points(using=...)` call. `query(dense=None, sparse=None)` raises
`ValueError`.

`ne.brain.retriever.HybridRetriever` implements the retrieval side of
`ne.contracts.knowledge.KnowledgeRead` (`search`/`retrieve` + citations) on
top of any `ChunkStore` + `Embedder` + sparse query encoder.

## pgvector store alternate (NE-091, optional)

`ne.brain.pgvector.store.PgvectorChunkStore` is a **drop-in alternate** to
`QdrantChunkStore`, backed by a Postgres [`pgvector`](https://github.com/pgvector/pgvector)
table, registered under the SAME `ne.plugins.vector_store` entry-point slot as
`qdrant` (name `pgvector`). It exists as the retrieval-plane "no vector-DB
lock-in" proof (the NE-092 PDP-alternate precedent applied to the vector store):
a Postgres-first customer can run retrieval on the platform Postgres with **no
separate Qdrant service**. **Qdrant remains the byte-unchanged default.**

**Install:**

```
pip install 'ne-brain[pgvector]'
```

`psycopg` is an **optional extra** (`[project.optional-dependencies] pgvector`)
plus a **dev dependency** (so the swap-parity + conformance suites run in CI under
`uv sync --all-packages`) — **never** a `[project.dependencies]` runtime dep. A
Qdrant-only deployment never installs or imports it; `import ne.brain` alone never
imports `ne.brain.pgvector` or `psycopg` (test-enforced by
`test_pgvector_is_lazy_and_unexported`). psycopg is LGPL-3.0, owner-approved
case-by-case (`tools/supply-chain/license-policy.toml`) and is already present in
the shipped `ne-app` image closure via `dbos` — so on that image config alone
activates the store (no image change).

**Select it:**

```
NE_API_VECTOR_STORE=pgvector
NE_API_PGVECTOR_DSN=postgresql://user:pass@platform-postgres:5432/ne
NE_API_PGVECTOR_TABLE=ne_chunks   # optional (default ne_chunks)
```

`platform.py`'s `_default_chunk_store` then resolves `PgvectorChunkStore` via the
`PluginRegistry` seam (app.py untouched — the 085c/NE-092 discipline). Qdrant
stays the default; `NE_API_QDRANT_URL` may be a placeholder when unused. An empty
DSN under `vector_store=pgvector` is a **boot `ValueError`** (fail loud).

**Schema / index choices** (per `table` `t`):

* `{t}(chunk_id text PK, payload jsonb NOT NULL, dense vector(dim) NOT NULL)` —
  **HNSW** cosine index `USING hnsw (dense vector_cosine_ops)`; **GIN**
  `USING gin (payload jsonb_path_ops)` for the `@>` filter legs.
* `{t}_sparse(chunk_id -> {t} ON DELETE CASCADE, term_index bigint, weight real,
  PRIMARY KEY(chunk_id, term_index))` — a sparse **inverted index**; btree on
  `term_index`.

Dense vectors travel as parameterized text literals cast `::vector`, sparse as
`bigint[]`/`real[]` arrays — **no pgvector Python client lib** (zero new deps).
Hybrid retrieval mirrors `qdrant-client` 1.18.x local mode **exactly**: the dense
leg scores `1 - (dense <=> qvec)` (cosine), the sparse leg applies Qdrant's IDF
`ln((N - df + 0.5)/(df + 0.5) + 1)` (with `N`/`df` GLOBAL/unfiltered, like
`_rescore_idf`), and the two legs fuse with Qdrant's exact RRF (`score += 1/(pos0
+ 2)`, `K=2`) **in-adapter** below the frozen seam.

**C7 (load-bearing):** the store applies the SAME ACL trim as Qdrant — identical
`ne.brain.trimming.acl_match_labels` labels + the SAME filter grammar (incl.
NE-087 `$any`), compiled by `ne.brain.pgvector.filters.compile_filters` into
fail-closed, **fully parameterized** JSONB-containment SQL applied server-side
inside **every** leg's `WHERE` (before fusion) and into `get_filtered`. A
principal sees exactly the same permitted chunks in both backends; a chunk with an
empty/missing `acl` matches nothing; a caller-supplied `$`-key is rejected.
`permission_filter` emits the SAME mapping `PrincipalAclFilter` emits.

**Postgres-first deploy note.** Runs on the **platform Postgres** (NE-081 PG17+),
NO separate Qdrant service. The `pgvector` extension **>= 0.8.2 is REQUIRED**
(CVE-2026-3172 — a parallel-HNSW-build buffer overflow fixed in 0.8.2; the
dev/test container `pgvector/pgvector:pg18` tracks it). The role needs `CREATE
EXTENSION` privilege, or the extension must be pre-provisioned. **C2:** the
adapter talks ONLY to the in-network Postgres DSN — no egress, no telemetry.
**C10:** single-tenant. A single lazily-opened async connection + `asyncio.Lock`
serializes store I/O (correctness over throughput; `psycopg_pool` deliberately not
adopted to keep zero new deps — a documented future knob).

**Live tests.**

```
docker run --name ne-pgvector -e POSTGRES_PASSWORD=pgv -p 5457:5432 \
    -d pgvector/pgvector:pg18
NE_PGVECTOR_DSN=postgresql://postgres:pgv@localhost:5457/postgres \
    uv run pytest \
    packages/ne-brain/tests/test_pgvector_store_conformance_server.py \
    packages/ne-brain/tests/test_swap_parity_qdrant_pgvector.py -q
```

Both suites LIVE-PARK (module-level skip) when no reachable pgvector Postgres is
configured; the Qdrant side of the parity suite runs in-process (`:memory:`).

### Semantic-gap analysis (SPEC-required — conclusion: NO C7-weakening gap)

Each divergence from the Qdrant store is equal-or-stricter, and the swap-parity
suite bounds it:

1. **Server-side RRF (Qdrant) vs in-adapter RRF (pgvector) below the same seam.**
   Research-brief-sanctioned; the fusion constant (`K=2`) and formula are mirrored
   verbatim from `qdrant_client/hybrid/fusion.py`. Only tie-ORDER can differ (equal
   RRF scores) — bounded by the hybrid top-10 overlap floors (>= 0.8/query, >= 0.9
   mean). Single-leg rankings are IDENTICAL (asserted exactly).
2. **Filtered ANN: Qdrant filters INSIDE HNSW; pgvector post-filters the index
   scan** (`hnsw.iterative_scan = strict_order` on >= 0.8). This can only
   **UNDER**-return under a selective filter, **never over-return** — so it is
   C7-safe (a swap can never widen a permission-trimmed read). The parity suite
   bounds the recall gap; tiny-corpus exact planning makes it zero in tests.
3. **`update_payload`: Qdrant `set_payload` CREATES a missing point; pgvector
   UPDATEs only.** Stricter (never invents a row); the spine only passes chunks it
   just fetched, so every id already exists.
4. **IDF in SQL vs Qdrant's server modifier.** SAME formula, SAME statistics
   (`N` = row count, `df` = global document frequency) — asserted by exact
   single-leg sparse parity.
5. **float32 storage rounding** (`vector`/`real` columns) matches Qdrant's numpy
   float32 — rankings are identical on well-separated scores.

Conclusion: **no C7-weakening gap** — enforced by the parity suite's ACL-identity
+ hidden-chunk non-leak assertions.

## Retrieval pipeline (NE-022)

`ne.brain.pipeline.RetrievalPipeline` is the **production T1 retrieval path**.
It implements `ne.contracts.knowledge.KnowledgeRead` by composing the
`HybridRetriever` with a rerank stage and a citation rebuild:

```
query
  -> TEIEmbedder dense embed + FastEmbedBM25 sparse encode
  -> QdrantChunkStore server-side RRF fusion   (candidate_k = top_k × candidate_multiplier, default 4)
  -> TEIReranker /rerank                         (down to top_k)
  -> citations with source-system IDs (C7)
  -> RetrievalResult
```

`search()` over-fetches `candidate_k` fused candidates, reranks down to `top_k`,
and rebuilds citations in **reranked order** (1:1 aligned). `retrieve()`
(fetch-by-id) is not ranked, so it delegates to the underlying retriever with
no rerank stage. Empty candidate sets short-circuit (no rerank call). Because
EVERY fused candidate is scored by the TEI reranker in one call, raising a
caller-configured `top_k` (e.g. `ApiSettings.retrieval_top_k`, SPEC §5
NE-CTXB-6) multiplies per-search TEI rerank calls/tokens (and underlying
retrieval load) by `candidate_multiplier`.

On a non-empty candidate set, `search()` returns a
`RerankedRetrievalResult(RetrievalResult)` — an ADDITIVE subclass carrying one
extra field, `candidate_count`, the number of fused, ACL-trimmed candidates
retrieved BEFORE reranking down to `top_k`. Callers that only know the base
`KnowledgeRead` Protocol shape see a plain `RetrievalResult` (conformance
unaffected); callers that want the pre-rerank count (e.g. ne-api's chat
knowledge-tool `[showing M of N retrieved chunks]` header) `isinstance`-narrow
to `RerankedRetrievalResult`. The empty-candidates short-circuit returns the
PLAIN base `RetrievalResult` (not the subclass) — pydantic v2 equality is
class-sensitive, and `test_c7_provable_trimming.py` pins base-class equality
on that path.

The pipeline adds **no second permission path**: the permission-filter hook is
the underlying `HybridRetriever`'s `acl_filters` seam (stub until NE-043), so
ACL trimming happens exactly once, below the pipeline, on both `search` and
`retrieve`.

### Reranker decision (re-verified 2026-07-04)

`ne.brain.reranker.TEIReranker` implements `ne.contracts.knowledge.Reranker`
over TEI's HTTP `/rerank` endpoint (mirroring `TEIEmbedder`). It POSTs
`{"query": str, "texts": [str, ...], "raw_scores": false}` and parses the
`[{"index": int, "score": float}, ...]` array; it **never trusts the server's
ordering** — results are re-sorted client-side (descending score, ties broken
by ascending original index) and truncated to `top_k`. Batches larger than
`batch_size` are merged with a global client-side sort. Malformed entries
(non-mapping, out-of-range index, non-float-able score) raise `ValueError`
(fail loud).

**Model:** the current TEI release (`v1.9.3` == the dev-stack pin
`ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.3`, no drift) does
**not** support `Qwen3-Reranker-0.6B`: `Qwen3ForSequenceClassification` is
unsupported and Qwen3-Reranker is a decoder model needing prompt-initialized
scoring. huggingface/text-embeddings-inference issues
[#643](https://github.com/huggingface/text-embeddings-inference/issues/643) and
[#691](https://github.com/huggingface/text-embeddings-inference/issues/691) are
both OPEN with no maintainer commitment (referenced PR #835 unmerged). The
adapter therefore defaults to the **SPEC-named fallback**
`BAAI/bge-reranker-v2-m3` (Apache-2.0), which is exactly the model NE-006's
dev stack already serves at `tei-rerank` on port `8086`. **Revisit trigger:**
TEI release notes gaining Qwen3 reranker support — then flip the `model_id`
param (a config-level change) plus the dev-stack compose entry (owned
elsewhere).

### C3 note — registry-tag for the reranker

The TEI rerank call is **in-VPC inference**, explicitly exempt from ne-gateway
routing per C3 (no provider SDK import, no gateway call), but it MUST be
**registry-tagged**. NE-011's model registry should register this ModelEntry at
assembly time:

| field          | value                                    |
|----------------|------------------------------------------|
| `model_id`     | `BAAI/bge-reranker-v2-m3`                 |
| `provider`     | `tei` (`vllm` when `NE_API_RERANK_BACKEND=vllm` selects `VllmReranker`) |
| `license`      | `Apache-2.0`                             |
| `origin`       | `BAAI (China)`                           |
| `lane`         | `PRIVATE_VLLM` (in-VPC)                   |
| `capabilities` | `("rerank",)`                            |

`VllmReranker` (NE-SEARCH-LATENCY) is a second `Reranker` adapter over vLLM's
Jina-compatible `/rerank` — the SAME model on a different server (a GPU-served
vLLM instance; e.g. `deploy/inference-operator/vast-rig.sh serve rerank` for the demo rig).
It is a latency lane, never a model swap: the demo-box attribution measured the
4-CPU TEI container at ~1s per query/chunk pair on real ~600-char chunks (~40s
per `top_k=10 * candidate_multiplier=4` search) vs ~0.12s for the same 40 pairs
on GPU. In that one side-by-side run (2026-08-04) the top-10 ordering was
identical — a measurement of two servers, re-measured rather than assumed each
time the lane is stood up. What CI pins is the adapter half:
`tests/test_reranker_parity.py` holds that both adapters, given equivalent
backend responses, produce identical ordering, tie-break and `top_k`.

An out-of-VPC vLLM host must be explicitly allowlisted
(`NE_API_RERANK_ALLOWED_HOSTS` → the constructor's `allowed_hosts`, C2
fail-closed), and **an engaged air gap dominates that allowlist entirely** — a
public IP literal and a DNS name alike, because under the gap the operator
allowlist contributes nothing and only provably in-VPC shapes pass (the
`ne.storage.s3` rule). Checked at construction and again before every request, so
a governed runtime air-gap flip stops an already-built adapter from sending
(`ne.net.ssrf.allowlist_revoked_by_air_gap`, which also names the tracked
`ne.gateway.egress` divergence). Two operational facts the adapter
docstring states in full: there is **no automatic fallback** (a dead rerank
backend hard-fails `/v1/brain/search`), and the demo lane is **cleartext and
unauthenticated** (synthetic corpus only).

AIR_GAP accounting: same weights-precache note as the embedder (below) — the
reranker weights are pulled once and served by TEI, never bundled here.

### Entry points (plugin slots)

| Slot              | Name             | Class                                |
|-------------------|------------------|---------------------------------------|
| `vector_store`     | `qdrant`         | `ne.brain.qdrant.QdrantChunkStore`    |
| `vector_store`     | `pgvector`       | `ne.brain.pgvector.store.PgvectorChunkStore` (NE-091, optional `[pgvector]` extra) |
| `sparse_encoder`   | `fastembed-bm25` | `ne.brain.sparse.FastEmbedBM25Encoder`|
| `embedder`         | `tei`            | `ne.brain.embedder.TEIEmbedder`       |
| `reranker`         | `tei`            | `ne.brain.reranker.TEIReranker`       |

Resolve via `PluginRegistry().resolve("vector_store", "qdrant")` etc.
(`ne.contracts.plugins`).

### Kit-law decided semantics

* `ChunkStore.get()` **omits** unknown chunk ids (never raises), preserving
  the caller's input order for the ids that do exist.
* `ChunkStore.query(dense=None, sparse=None)` raises `ValueError` — at least
  one signal is required.
* Filter grammar (`filters: Mapping[str, Any]`): keys are payload field paths
  used **verbatim** (e.g. `"acl"`, `"metadata.team"` for a nested field); a
  `str`/`int`/`bool` value means equality (`MatchValue`); a `list`/`tuple`
  value means "any of" (`MatchAny`); anything else raises `ValueError`.

### NE-043 security-trimming seam (seam wired, hook stubbed until NE-043)

Every `Chunk` carries an `acl: tuple[str, ...]` field, stored in the point
payload and keyword-indexed (`create_payload_index(field_name="acl", ...)`),
so filtering by ACL group (`filters={"acl": ["group-a"]}`) already works.
`QdrantChunkStore.permission_filter(principal)` and
`HybridRetriever(..., acl_filters=...)` are the stubbed hooks — both return/
default to `None` today, meaning **no principal-based trimming happens yet**.

The seam is now WIRED on both retrieval paths: when `acl_filters` is
supplied, `search()` merges the ACL mapping into its server-side query
filter, and `retrieve()` uses `QdrantChunkStore.get_filtered` (a point-id
AND ACL predicate, filtered inside Qdrant) — so a principal can never
`retrieve()` a chunk `search()` would hide. `retrieve()` fails closed
(`NotImplementedError`) on a store without filtered fetch-by-id rather than
silently falling back to the unscoped fetch. The hook still defaults to
`None` until NE-043 supplies the real principal -> filter mapping, and
`ChunkStore.get()` stays unscoped-by-design for the NE-030 audit path.

**Filter composition (NE-043 filter-merge note — fixed here in NE-022).**
When both a caller filter and an ACL filter are present, `search()` composes
them with `ne.brain.retriever.and_combine_filters`: **AND-combine on key
collision (intersection), never overwrite**, so NE-043 inherits correct
composition. In the store's filter grammar (`str`/`int`/`bool` = equality,
`list`/`tuple` = any-of) the collision rules are: scalar ∧ equal-scalar → the
scalar; scalar ∧ unequal-scalar → `[]`; scalar ∧ list → the scalar if it is in
the list else `[]`; list ∧ list → caller-ordered intersection. An empty list
`[]` deliberately encodes **match-nothing** (`_build_filter` turns it into
`MatchAny(any=[])`, which matches zero points on both the query and scroll
paths). This replaced the earlier `merged |= acl` overwrite, which let a
colliding caller filter silently widen or replace the ACL filter.

### Running the conformance kit live

The `ChunkStoreContractKit` / `KnowledgeReadContractKit` (under
`ne.brain.testing.kits`, see "kit location" below) always run in CI against
`qdrant-client`'s in-process `:memory:` local mode. They can additionally run
against a real Qdrant server (the actual conformance claim).

> ⛔ **The server must be one you own, and this page will not name it for you.**
> On 2026-09-10 these tests took the live demo's Qdrant to `status: red`: the URL
> defaulted to the dev-stack port, the skip fired only when that was
> *unreachable*, and on a box running the stack it is very much reachable — so an
> ordinary `pytest packages/ne-brain` wrote into production and left **985 orphan
> collections** behind, until segments × collections exhausted the process's file
> descriptors. There is no default any more, and no recipe here names a port,
> because the equivalent OPA outage was re-armed by developers following exactly
> such a recipe. Point `NE_QDRANT_URL` at a scratch Qdrant — never at the
> deployed stack — and every collection the run creates is dropped in fixture
> teardown (`ne.brain.testing.qdrant_live`), on pass, failure and error alike.

```sh
NE_QDRANT_URL=<your scratch qdrant> uv run pytest packages/ne-brain -q
```

The TEI embedder and reranker are read-only, so they keep plain reachability
gates and their dev-stack defaults:

```sh
ne dev up
uv run pytest packages/ne-brain -q     # picks up TEI :8085 / :8086 when running
```

The full-pipeline server-mode conformance run needs both: a named scratch Qdrant
and a reachable reranker. When either is missing the corresponding tests skip
with a `LIVE-PARKED` message.

### Air-gap / telemetry note

`FastEmbedBM25Encoder` downloads the `Qdrant/bm25` model assets (~18 small
files) from the Hugging Face hub on first use via `huggingface-hub`; in an
air-gapped deployment, pre-cache these assets (or set `HF_HUB_OFFLINE=1` once
cached). Set `HF_HUB_DISABLE_TELEMETRY=1` (tests do this automatically via
`conftest.py`) to disable `huggingface-hub`'s telemetry ping. `qdrant-client`
itself has no client-side telemetry; the dev-stack Qdrant server already runs
with `QDRANT__TELEMETRY_DISABLED=true`.

### Vendored BM25 assets (NE-CKC9)

The `Qdrant/bm25` model assets are vendored in-repo under
`assets/fastembed/models--Qdrant--bm25/` (HF-cache layout, revision
`e499a1f8…`, Apache-2.0) so CI's fail-closed Checkpoint-C baseline loads them
**fully offline** — immune to GH Actions cache eviction and HuggingFace 429s.
`ci.yml` stages them into `FASTEMBED_CACHE_PATH` with `HF_HUB_OFFLINE=1`; see
`assets/fastembed/README.md` for provenance and the update trigger.

### Version drift note

SPEC §3.3 snapshots Qdrant 1.17; NE-006's dev-stack compose pins the server
image at `qdrant/qdrant:v1.18.2`. This adapter aligns its client dependency
to `qdrant-client>=1.18.0,<2` to match the running server's minor version
(repeating NE-006's own drift note).

### Kit-location note (flagged deviation)

`ne.contracts` is FROZEN as of v1.0.0, so the `ChunkStore`/`KnowledgeRead`
conformance kits cannot live under `ne.contracts.testing.kits` even though
SPEC section 4 says kits ship alongside their Protocol. They ship here, under
`ne.brain.testing.kits`, using the identical kit-law pattern
(`ne.contracts.testing.kits`'s `<Protocol>ContractKit` convention). A small
additive-exempt contracts rider is proposed to relocate them into
`ne.contracts.testing.kits` so NE-091 (pgvector) can reuse them without
re-deriving the same tests.

## Pipeline spine (NE-026)

`ne.brain.ingest.IngestionPipeline` orchestrates the three NE-020 stages plus
embed+index into a `ChunkStore`, backed by a durable `IngestLedger`:

```
IngestSource(path, document_id, acl, metadata)
      │  content_hash = sha256(file bytes)      ← the version identity
      ▼
  ledger.get_state ──► same hash + same acl + same metadata?  ──► UNCHANGED (no-op)
      │                 same hash but changed acl/metadata?     ──► cheap payload
      │                     refresh (set_payload, no re-embed)  ──► INDEXED
      │                 quarantine row for (doc,hash)?          ──► QUARANTINED (no retry)
      ▼
  ledger.begin
      ▼
  PARSE  ──► artifact? reuse : parser.parse → validate → save+checkpoint(PARSED)
      ▼
  CHUNK  ──► artifact? reuse : chunker.chunk → save+checkpoint(CHUNKED)
      │      (serialize → RELOAD: fresh & resumed paths see identical chunks)
      ▼
  ENRICH ──► (only if an Enricher is injected) artifact? reuse : enrich → save+checkpoint(ENRICHED)
      ▼
  INDEX  ──► store.upsert(new) → store.delete(stale) → ledger.record_indexed
      ▼
  IngestOutcome(status ∈ {INDEXED, UNCHANGED, QUARANTINED, FAILED}, chunk_count, reason)
```

`ingest_batch` runs sequentially and NEVER aborts on a per-document outcome.
Re-calling `ingest()` on the same source IS the resume API.

**Idempotency (ACL/metadata-aware).** The short-circuit is no longer
content-hash-only: identical bytes with an unchanged `acl` AND unchanged
`metadata` short-circuits to `UNCHANGED` (zero parser/store calls); identical
bytes with a *changed* `acl`/`metadata` takes the cheap payload-refresh path
(see the P2 fold below) instead of a full rebuild. Chunk ids are stable
(`document_id:NNNN`) and map to stable UUIDv5 point ids in Qdrant
(`point_id_for`), so even a forced re-upsert overwrites in place — re-ingesting
identical bytes with an unchanged acl/metadata adds zero points (asserted via
`client.count`).

**Resume.** Each completed stage persists its output as an artifact keyed by
`(document_id, content_hash, stage)` and checkpoints. A resumed run replays only
the stages with no artifact yet: a crash after PARSE re-uses the parse artifact
(no re-parse); a crash after CHUNK re-uses parse + chunk. The
**serialize-then-reload normalization** at the CHUNK/ENRICH checkpoints (chunk
JSON is written and immediately re-read for all downstream use) guarantees the
resumed path sees byte-identical `Chunk` objects to a from-scratch run (tuple↔
list metadata drift would otherwise break resumed-vs-fresh parity).

**Doc versioning + supersede ordering.** A changed document (new content hash,
same `document_id`) supersedes the old version. The ledger preserves the
live-in-index version's chunk ids across the restart, and INDEX runs
`upsert(new) → delete(stale = old_ids − new_ids) → ledger.record_indexed` in
that order. Rationale: upsert-new-**before**-delete-stale means a crash anywhere
in the window never makes the document unretrievable, and the ledger's `INDEXED`
state (committed LAST, in one transaction that also prunes artifacts) implies
the index holds exactly that version's chunks. The accepted trade-off is a
bounded, single-document transient window in which a query may briefly see
chunks from BOTH versions of that one document; it heals on the next resume. The
alternative — version-suffixed chunk ids — would break `chunk_id` stability and
citations, so it is rejected.

**Failure taxonomy — QUARANTINE vs FAILED.** Document-caused failures (unreadable
source, parse/validation error, chunker error, empty/no-chunk result) go to the
**quarantine lane**: recorded and inspectable via `ledger.list_quarantined()`,
the batch continues, and the index is never partially written (nothing is
upserted before parse+chunk validation passes). A quarantined document is NOT
retried on identical bytes unless `force=True`. Infrastructure-caused failures
(enricher/gateway error, embed/index/store error) are `FAILED`/retryable: the
checkpoint stays at the last completed stage so a later resume finishes without
redoing completed work.

**Ledger seam.** `IngestLedger` is a package-local Protocol (like
`ParsedDocument` — `ne-contracts` stays FROZEN). The shipped `SqliteIngestLedger`
is a local, single-connection stdlib-`sqlite3` store where every mutation is a
committed transaction (so checkpoints survive a process restart). The production
ne-ingest process is expected to swap a Postgres-backed ledger in at this exact
seam later; the spine depends only on the Protocol.

**No entry-point.** The spine is a package-local orchestrator, not a
frozen-contract slot adapter, so — unlike the store/embedder/parser/chunker/
enricher — it deliberately has NO `ne.plugins.*` entry-point.

**C3.** The spine imports no gateway/provider code; enrichment reaches models
only through the injected `Enricher` Protocol.

### NE-020 P2 folds (landed with NE-026)

* **P2a — parser-env rebuild lock.** Two concurrent `parse()` calls could both
  enter the docling venv (re)build path — one `rmtree`-ing `venv_dir` while
  another installs into it. `ensure_env_at` now serialises the rebuild behind a
  cross-process/thread `filelock.FileLock` whose lock file is a **sibling** of
  `venv_dir` (`<venv_dir>.lock`, so it survives the `rmtree`), with a
  1800-second timeout and an in-lock re-check of the marker fast-path so only the
  first waiter actually rebuilds. On timeout it raises an actionable
  `RuntimeError` naming the lock path.
* **P2b — body-less heading chunk.** A `HEADING` with no following body (a
  title-only doc, a trailing heading, or a sibling heading immediately replaced
  at the same level) was previously dropped, losing its text to retrieval. The
  chunker now tracks heading "coverage" and emits an uncovered heading as its
  own `text` chunk (ancestor-path metadata only; nested bare headings collapse
  into one ancestor-prefixed chunk). See the CHUNK section above.

### NE-026 P2 folds (Codex round, post-merge)

* **P2a — stale-ACL security fix.** The content-hash `UNCHANGED` short-circuit
  ignored `IngestSource.acl`/`metadata`, so re-ingesting identical bytes with a
  *narrowed* ACL left stale (over-permissive) ACL labels live in the vector
  store — a principal the new ACL excludes could still be served those chunks by
  the NE-043 read-path filter. The short-circuit is now 3-way: unchanged
  bytes+acl+metadata → `UNCHANGED`; unchanged bytes but changed acl/metadata → a
  cheap payload-only correction via a package-local `PayloadUpdatable` capability
  Protocol (`QdrantChunkStore.update_payload` → Qdrant `set_payload`, NO
  re-embed/re-encode); anything else → the full pipeline. Stores lacking
  `PayloadUpdatable` fall back to a full `upsert_chunks` (correct, but not cheap).
  `PayloadUpdatable` follows the `retriever.FilteredChunkFetch` precedent — a
  duck-typed, `runtime_checkable` capability, NOT a change to the frozen
  `ne-contracts` `ChunkStore`.
* **P2b — source-metadata propagation.** `IngestSource.metadata` was defined but
  never reached indexed chunks. It is now merged into every indexed chunk's
  metadata under the reserved, namespaced `source` key (`metadata['source']`) at
  INDEX time — on both the fresh-index path and the cheap payload-refresh path —
  never flat-merged, so it can never collide with the chunker's structural keys
  or the parser's `doc.metadata`. It is usable by retrieval via the existing
  verbatim filter grammar as `filters={'metadata.source.<key>': <value>}`.
* **P2c — resume-path stale-ACL variant.** The full-pipeline INDEX step stamped
  `acl=c.acl` (the acl baked into a chunk object at CHUNK time) instead of the
  CURRENT `source.acl`. On a resume after a crash past the CHUNKED checkpoint,
  retried with a narrowed acl, this indexed the STALE wider acl while the ledger
  recorded the narrower one as live -- an index/ledger disagreement that let an
  excluded principal keep retrieving. The INDEX step now always stamps
  `source.acl` (mirroring the existing `_merged_metadata` re-stamp), on both the
  fresh path (a no-op there, since `source.acl` already equals the chunks' acl)
  and the resume path (where it corrects the staleness). Safe because the
  chunker/enricher always apply ONE uniform, document-level acl across every
  chunk of a document -- there is no per-chunk acl to lose.

## KnowledgeWrite API (NE-023)

The governed *write path* — the platform writes back curated knowledge with
provenance, not just reads it (owner C7). `ne.brain.write.KnowledgeWriter`
implements the frozen `ne.contracts.knowledge.KnowledgeWrite` Protocol
(`upsert` / `curate` / `quarantine`) over the real NE-021 `QdrantChunkStore`.

```
KnowledgeItem / CuratedKnowledgeItem
  -> SHAPE VALIDATE (JSON-Schema Draft 2020-12, BEFORE store entry)
       |-- pass -> PROVENANCE STAMP -> ChunkStore.upsert_chunks + AuditEvent
       |-- fail -> WriteQuarantine (full payload parked) + ShapeViolation + denied AuditEvent
```

### API surface

* `upsert(items)` — validate + provenance-stamp + index a batch. Returns an
  `UpsertReceipt(accepted, quarantined)` partitioned in input order; a per-item
  failure never aborts the batch (mirrors the NE-026 spine). Store+audit is
  **atomic** (NE-CKD6): if a per-item audit append fails *after* the store commit,
  the writer unwinds the not-yet-audited items (park-before-delete) and raises
  `KnowledgeAuditError`.
* `curate(item_id, patch, *, provenance)` — re-validate + re-stamp an existing
  item. `patch` keys are exactly `{'content', 'metadata'}`; an invalid patch
  leaves the store byte-identical, emits `ShapeViolation(quarantined=False)`, and
  raises `ShapeValidationError` (quarantining good data on a bad patch would
  destroy it). If the post-store audit append fails, curate **restores the
  original (already-audited) chunk** and parks the attempted patch, then raises
  `KnowledgeAuditError` (NE-CKD6).
* `quarantine(item_id, reason)` — a manual curator pull: parks the stored payload
  **before** deleting it from the store (never lose data), audits
  `knowledge.quarantine`, and emits **no** `ShapeViolation` (it is not a shape
  failure). An audit failure here is **fail-safe by ordering** (park+delete
  precede the audit) — the item is deliberately NOT unwound; it raises
  `KnowledgeAuditError` (NE-CKD6).

Governance attributes are TYPED fields on `CuratedKnowledgeItem`
(`acl`, `provenance_class`, `shape`) — there are no reserved input-metadata keys.
A plain `KnowledgeItem` takes the writer defaults (`acl=()`, the writer default
class, the writer default shape).

### Validation payload

Shape schemas are written against a stable payload shape regardless of item
subclass: `{'item_id', 'content', 'metadata', 'provenance'}`. A write that fails
its shape contract **does not enter the store**.

### Reserved `provenance` metadata key

Every stored chunk carries a first-class provenance stamp under the reserved
`metadata['provenance']` key (alongside the NE-026 spine's reserved
`metadata['source']` key). The stamp has six fields — `provenance_class`
(`ingested` / `curated` / `learned-by-agent`), `source`, `actor`, `written_at`,
`write_id`, `shape` (`"<name>@<version>"`). It is written **wholesale** (never
deep-merged) and is queryable via the store's verbatim dotted filter grammar as
`filters={'metadata.provenance.provenance_class': <class>}` — the C7 audit/replay
hook ("why does the brain believe X"). `provenance_from_chunk(chunk)` reads it
back.

### Quarantine lane

`SqliteWriteQuarantine` mirrors the NE-026 ingest ledger (synchronous stdlib
`sqlite3`, single connection, committed transactions). It is item-granular and
keyed on `item_id` with latest-failure-wins semantics: a failed write parks its
FULL payload + reason (inspectable, not silently dropped), and a subsequent
successful `upsert` of the same id clears the parked row.

### Event emission

`ShapeViolation` events go through `OutboxShapeViolationSink` (the ne-events
Postgres outbox, a line-for-line mirror of ne-gateway's `OutboxCostSink`);
`AuditEvent`s go through the frozen `AuditSink` seam. Both are built via the
ne-events `new_*` factories, so `config_hash` is stamped from the frozen process
hash — if a host has not frozen it, `ConfigHashNotFrozenError` propagates rather
than an event being emitted unstamped (hosts freeze at process start). Both
`audit_sink` and `violation_sink` are **REQUIRED** constructor arguments —
constructing a `KnowledgeWriter` without either raises `SinkRequiredError`. A
governed write with no audit transport is fail-open, so the writer matches the
sink-required-by-construction discipline of the rest of the governed stack
(Checkpoint-D boundary-audit #5, NE-CKD5); the quarantine lane is equally **not**
optional.

### Atomic store+audit (NE-CKD6)

A governed write is **atomic under a single audit-store fault: store+audit
together or neither**. The audit append happens per-item *after* the store
mutation; if it fails, the write is unwound so no caller of the frozen
`KnowledgeWrite` seam can ever observe a durable+retrievable chunk without a
matching audit record. The per-surface discipline:

* **`upsert`** — unwinds every not-yet-audited item via **park-before-delete**
  (full payload into the quarantine lane, then one `store.delete`; the unwind is
  itself audited best-effort). Items whose success-audit already durably appended
  stay (per-item store+audit atomicity); `KnowledgeAuditError.audited` /
  `.unwound` report the split.
* **`curate`** — parks the attempted patch (curator work is never lost) and
  **restores the original chunk** (already audited by its prior upsert), so the
  store never holds unaudited-modified content.
* **`quarantine`** — fail-safe by ordering: park+delete precede the audit, so an
  audit failure leaves the item over-parked/under-retrievable (never a
  retrievable-but-unaudited chunk); it is deliberately NOT unwound.

Every failure raises the typed `KnowledgeAuditError` (or the distinct, loud
`ConfigHashNotFrozenError` when the config hash was never frozen — the unwind
still runs). A **retry after sink recovery heals idempotently**: the same ids
re-upsert and re-audit, and the parked rows clear on the durable re-write. Because
the fix lives in the writer, a shared audit sink between the writer and a caller
(e.g. the NE-037 `MemoryPromoter`) is safe — proven in
`packages/ne-runtime/tests/test_memory_promotion_shared_sink_atomicity.py`.

**Known double-fault residual** (tracked, loud, non-blocking): if the audit store
is down AND the vector store *also* fails during the unwind (delete/restore), the
chunk may remain retrievable without its audit record. No distributed transaction
spans Qdrant + the audit store; this residual raises `KnowledgeAuditError` with an
explicit residual message to be acted on, and is out of scope for 2PC/outbox
machinery here.

### Read-path auditing (NE-164)

`HybridRetriever.retrieve()` (T1 fetch-by-id) optionally emits ONE
config_hash-stamped `AuditEvent` per call, mirroring the write-path pattern
above over the same frozen `AuditSink` seam. Auditing is OFF by default when
the caller explicitly passes `None` — a REQUIRED `audit_sink: AuditSink |
None` keyword-only constructor arg (NE-VG-2: required since it has no
legitimate default; `None` means no event machinery runs at all, and every
pre-NE-164 composition stays byte-identical, but the choice must now be
stated explicitly — an omitted `audit_sink` is a construction-time type
error). `RetrievalPipeline.retrieve()` forwards an optional
`trace_id: str | None = None` kw-only param straight through to a
`HybridRetriever` delegate (via `isinstance` narrowing — the declared
`retriever: KnowledgeRead` constructor type stays untouched, so conformance
kits are unaffected); a non-`HybridRetriever` delegate simply drops it.

**Event shape** — `action="knowledge.retrieve"`, `resource="knowledge://chunks"`,
`outcome=success`, `actor=principal.id`, and a deterministic `detail` grammar:

```
candidates=<requested> returned=<returned> acl_trim_delta=<trimmed>
```

**C7 counts-only rule** — the event carries the query SHAPE only: candidate/
returned COUNTS and the ACL-trim DELTA. It NEVER carries an ACL value, a
principal label, a chunk id, chunk text, or raw query content — the same
names/counts/classes discipline as the write path. The leak-assertion tests in
`test_retrieval_read_audit.py` prove this over `model_dump_json()`.

**Delta upper-bound semantics** — `acl_trim_delta` is `requested - returned`
when the ACL filter actually ran (a non-empty `acl_filters` mapping). On a
single filtered `get_filtered` fetch, a requested id that does not exist in the
store at all is indistinguishable from one that exists but was trimmed by the
filter, so the delta OVER-reports trims, never under-reports — a second
unscoped store call to disambiguate was rejected (it would itself be a C7
red-team surface, and doubles store load). When trimming is DISABLED
(`acl_filters is None`), a missing id is NOT an ACL trim and the delta is
always `0`. The match-nothing `{}` short-circuit reports the ENTIRE requested
count as trimmed, with no store call at all.

**Fail-closed sink-append** — when an `audit_sink` IS injected, a sink
`append` failure (or `ConfigHashNotFrozenError`) PROPAGATES: the read is never
served unaudited, mirroring the write path's NE-CKD6 discipline. A completed
`retrieve()` call ALWAYS emits when a sink is wired — all three completion
branches (match-nothing, ACL-filtered, and trimming-disabled) emit before
returning; the fail-closed `NotImplementedError` refusal branch emits nothing
(the security exception propagates first).

**Write-rate** — exactly ONE `AuditEvent` per `retrieve()` call, never
per-chunk, onto the hash-chained audit sink (design doc
`ne-agentic-routing-trace-demo.md` §9 open question 3 flags the per-retrieval
write-rate; sampling/aggregation is an explicitly deferred open question, not
built here).

**trace_id-as-data threading** — `ne-brain` gains NO `ne.runtime` import
anywhere; `trace_id` is threaded purely as an optional string parameter
(NE-166's philosophy). `ne-api`'s `/v1/brain/retrieve` route resolves
`current_run_trace_id()` (the NE-166 run-scoped correlation id, `None` outside
any run scope) and passes it down via `isinstance` narrowing against the
concrete `HybridRetriever`/`RetrievalPipeline` classes; the composition root
(`ne-api/platform.py`) wires the ONE platform audit sink into the retriever at
construction.

**Scoped follow-up (NOT built here)** — `search()`-path read events, T2
`GraphRetriever` / T3 verified-answer read events, and `trace_id` threading
through `GraphRetriever` are explicitly out of scope for this unit (kept M per
orchestrator scope ruling); a `GraphRetriever`-wrapped delegate at the ne-api
route falls through to a plain `retrieve()` call with no trace_id and emits no
read-path event of its own.

### Validator choice — jsonschema (GX/pandera adopt-decision)

Per build-vs-adopt §11 the write path ADOPTS the trusted validator already in the
locked closure — `jsonschema` 4.26.0 (MIT), Draft 2020-12. Great Expectations /
pandera were CONSIDERED (the SPEC row says "JSON-Schema/GX") and REJECTED for v1:
GX Core drags a pandas-scale closure into the sovereign runtime lock and is a §10
stewardship watch item. pandera/Soda remain the SPEC-named future swaps behind the
same `ShapeContract` seam. An invalid schema fails LOUD at `ShapeContract`
construction (not at write time).

### Composition boundary vs NE-026, and NE-037

NE-026's spine ingests **documents** (chunk ids `document_id:NNNN`, always
containing `':'`); NE-023 owns item-granular **knowledge** where
`chunk_id == item_id`. The namespaces cannot collide because spine ids always
contain `':'` — and the writer REJECTS an `item_id` containing `':'`
(`ValueError`) on **all three** governed-write surfaces (`upsert`, `curate`,
`quarantine`; the latter two added by NE-CKC2) BEFORE any store fetch/park/delete,
so a governed write can never delete or rewrite a spine chunk. NE-037 promotes
agent learnings back into the brain via
`CuratedKnowledgeItem(provenance_class=ProvenanceClass.LEARNED_BY_AGENT)` — a
first-class provenance class proven by a round-trip test.

## Security hardening (NE-CKC2)

### C2 endpoint validation

`TEIEmbedder` and `TEIReranker` POST corpus text / query+chunk text to a
**config-supplied** `base_url`. That endpoint is validated at **construction
time** by `ne.brain.sovereignty.validate_in_vpc_endpoint` (SSRF shape + in-VPC /
allowlist, fail-closed): a bad or exfil-shaped endpoint raises `UnsafeEndpointError`
and the client is never constructed, so it never receives a byte of corpus. The
`allowed_hosts=(...)` constructor kwarg is the escape hatch for private-DNS hosts
that are not dotless / `.local` / `.internal` and not private IP literals — it
mirrors ne-gateway's `allowed_private_vllm_hosts`.

`validate_in_vpc_endpoint` is a **deliberate, documented package-local mirror** of
`ne.gateway.ssrf.validate_endpoint_url(require_https=False, allow_private=True)` +
`ne.gateway.egress.EgressPolicy._is_in_vpc` + the AIR_GAP env read, NOT an import
of them: `import ne.gateway.ssrf` executes `ne/gateway/__init__.py` → `cost` →
`_litellm` → `litellm` at import time, so import-only reuse would drag litellm and
the ne-gateway runtime closure into ne-brain's import graph — the C3 coupling this
mirror exists to avoid. The mirror additionally folds the packed-integer-IPv4
rejection (audit #10) in unconditionally (stricter than the gateway's current
export). The orchestrator may later hoist a shared validator into a neutral
package and collapse this ~120-line mirror.

The durable enforcement is the structural guardian in
`tests/test_c2_egress_guardian.py`: it fails CI if any ne-brain module other than
`embedder.py` / `reranker.py` imports an outbound-HTTP client, or if any
`self._base_url` assignment there is not wrapped in `validate_in_vpc_endpoint`, and
it red-teams both clients (hostile endpoint → `UnsafeEndpointError`, MockTransport
proof that zero requests leave).

`ne-serve`'s `ServeClient` endpoint validation is **NE-CKC7 scope** — explicitly
out of this diff.

### AIR_GAP ingest posture

Ingest setup may egress while provisioning the parser venv or FastEmbed BM25
assets inside the customer VPC. Document parsing itself does not inherit a
connector's source-network authority. Under `AIR_GAP` (read identically to
ne-gateway; the read can only ADD restriction) setup remains fail-closed:

- **Parser venv**: a pre-built venv is the sanctioned path (run `ensure_env_at` at
  image-build/pre-staging time, or point `NE_PARSER_ENV_DIR` + `NE_PARSER_VENV_DIR`
  at a pre-built venv). Under AIR_GAP a current venv is a silent no-op; an
  absent/stale one raises `AirGapViolationError` BEFORE any `uv` subprocess. Any
  NON-AIR_GAP env-provisioning failure surfaces as the retryable
  `ParserEnvironmentError`, so the spine records it **FAILED/retryable** (never
  QUARANTINE — a transient infra outage cannot become a permanent silent index
  gap).
- **FastEmbed BM25**: `HF_HUB_OFFLINE=1` is forced (and `HF_HUB_DISABLE_TELEMETRY=1`
  unconditionally) into the environment BEFORE the model loader runs, so a cold
  cache raises with no network attempt; the sanctioned path is a pre-staged
  `FASTEMBED_CACHE_PATH`.
- **docling weights**: `parser_env/parse_doc.py` forces `HF_HUB_OFFLINE=1`
  unconditionally before importing Docling. When the standard PDF layout model
  is not locally staged, only that exact artifact-unavailable branch falls back
  to pinned `pypdfium2` embedded-text extraction. Text-free/scanned PDFs fail as
  document conversion failures; arbitrary Docling/runtime failures do not use
  the fallback. Pre-stage Docling artifacts when OCR/layout recovery is required.

### ACL hook contract (closed)

The `HybridRetriever.acl_filters` hook now has an exact, fail-closed contract on
BOTH `search()` and `retrieve()`, seeding the C7 guardian NE-043 completes:

- `None` (hook absent, or hook returns `None`) → trimming **disabled**;
- non-None empty mapping `{}` → **match-nothing** (empty result, no store call, no
  embedding) — a groupless principal can no longer receive the full corpus;
- non-empty mapping → server-side filter, AND-combined with any caller filter.

## Security trimming (C7, NE-043)

Query-time principal filtering is **real** as of NE-043: a user provably cannot
retrieve what they cannot read at source, on BOTH principal-scoped read paths
(`KnowledgeRead.search` AND `KnowledgeRead.retrieve` fetch-by-id). Trimming
happens server-side in Qdrant via the `acl` payload filter; `ChunkStore.get` stays
**unscoped-by-design** (raw, NE-030-audited) — do NOT scope it.

The concrete group expander lives in `ne-permissions`; ne-brain consumes it through
the package-local `ne.brain.trimming.GroupExpander` Protocol (no ne-brain →
ne-permissions import edge — that would cycle via ne-connect → ne-brain). Wire it
at the **composition root**:

```python
from ne.brain import HybridRetriever, PrincipalAclFilter, QdrantChunkStore, RetrievalPipeline
from ne.permissions.expand import PrincipalExpander

expander = PrincipalExpander(principal_store)  # from build_permissions_stack(...)
store = QdrantChunkStore(client, collection, embedder, sparse, group_expander=expander)
retriever = HybridRetriever(
    store, embedder, sparse, acl_filters=PrincipalAclFilter(expander), audit_sink=audit_sink
)
pipeline = RetrievalPipeline(retriever, reranker)
```

Fail-closed rules (the NE-CKC2 lesson, structurally enforced):

- an **unknown / unsynced** principal matches ONLY its own id (plus verified
  token-claim groups) — never `TENANT_ALL`, never widened;
- a chunk with an **empty / missing** ACL matches **nothing** (`MatchAny` over an
  empty array never matches) — empty-means-match-all is impossible;
- `QdrantChunkStore.permission_filter` can no longer return `None` (return type
  narrowed to `models.Filter`); `PrincipalAclFilter` never returns `None`/`{}`;
- an **expander error propagates** — it never degrades to an unscoped fetch;
- `acl_filters=None` is an explicitly **un-trimmed** composition for fixtures/eval
  only; production always injects `PrincipalAclFilter`.

`PrincipalExpander.expand` is **sync** (pure indexed reads); if the host loop needs
an async surface, wrap it in `asyncio.to_thread` at the composition root (per the
ne-permissions README). The C7 guardian test (`test_c7_trimming_guardian.py`) pins
the set of principal-scoped read paths and fails CI if a new one is added without
being registered and proven to apply the ACL hook.
