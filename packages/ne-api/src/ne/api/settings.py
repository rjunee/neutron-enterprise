# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ApiSettings — the ne-api composition-root configuration (spec §3.4).

``env_prefix='NE_API_'``, ``extra='forbid'`` (a typo in ``ne.toml`` fails loudly),
``toml_file='ne.toml'`` (house style). ALL §3.4 fields are declared now so the
104b/c slices add routes without churning the settings model (and therefore
without changing the frozen ``config_hash``). Everything else rides the existing
module settings (``NE_RUNTIME_*``, ``NE_IDENTITY_*``, ``NE_GATEWAY_*``, ...).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from ne.api.inference_lifecycle import InferenceLifecycleConfig
from ne.contracts.config import NeSettings


class ApiSettings(NeSettings):
    """Layered settings for the ne-api service (spec §3.4)."""

    model_config = SettingsConfigDict(
        env_prefix="NE_API_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    #: Brain store endpoint (required — no default so a misconfig fails at boot).
    qdrant_url: str
    #: Chunk collection name (required).
    qdrant_collection: str
    #: Embedder (TEI) endpoint (required).
    tei_url: str
    #: The in-cluster TEI reranker endpoint (SPEC §3.1 rerank stage). Default is the
    #: pilot/dev ``tei-rerank`` service name + container port 80 (confirmed against
    #: deploy/compose/pilot/compose.yaml — the published ``127.0.0.1:8086`` is
    #: host-only). ``build_platform`` wraps the retriever in
    #: ``RetrievalPipeline(HybridRetriever, TEIReranker(url), candidate_multiplier=4)``
    #: by DEFAULT (rerank-on). An empty string ⇒ the rerank stage is EXPLICITLY
    #: disabled — build_platform degrades to the bare HybridRetriever with a logged
    #: warning (an env without tei-rerank), NEVER the default. A set-but-invalid /
    #: exfil-shaped URL fails LOUD at boot (``UnsafeEndpointError`` via
    #: ``ne.brain.sovereignty``, C2). NOTE: adding this field is an EXPECTED
    #: release-level ``config_hash`` schema change (the 104b/104c precedent verbatim —
    #: ApiSettings freezes into the hash under ``"ne-api"``), PR-flagged, NOT a
    #: mid-process mutation.
    tei_rerank_url: str = "http://tei-rerank:80"

    #: Which rerank-server ADAPTER speaks to ``tei_rerank_url`` (NE-SEARCH-LATENCY).
    #: ``tei`` (default, byte-identical composition) keeps :class:`ne.brain.reranker.
    #: TEIReranker`; ``vllm`` selects :class:`ne.brain.reranker.VllmReranker` over
    #: vLLM's Jina-compatible ``/rerank`` (e.g. the demo GPU rig serving the SAME
    #: ``BAAI/bge-reranker-v2-m3``). This is a SERVER swap, never a model swap — the
    #: measured demo-box motivation (CPU TEI ~40s vs GPU vLLM ~0.12s for 40
    #: candidates) is recorded in the adapter docstring, together with what that
    #: one side-by-side measurement does and does not establish. The
    #: ``tei_rerank_url`` empty-string ⇒ rerank-disabled semantics apply to BOTH
    #: backends. Selecting ``vllm`` also arms an operational trap the adapter
    #: docstring spells out: a rerank
    #: backend that dies HARD-FAILS every ``/v1/brain/search`` (no fallback, by
    #: decision), after up to ``NE_TEI_RERANK_TIMEOUT_S``. NOTE: adding this field
    #: is an EXPECTED release-level ``config_hash`` schema change (the
    #: ``tei_rerank_url`` precedent verbatim), PR-flagged, NOT a mid-process
    #: mutation.
    rerank_backend: Literal["tei", "vllm"] = "tei"
    #: Extra hosts the rerank endpoint may resolve to beyond the in-VPC shapes
    #: ``ne.brain.sovereignty.validate_in_vpc_endpoint`` accepts (mirrors
    #: ne-gateway's ``allowed_private_vllm_hosts`` escape hatch — hosts only,
    #: never secrets, C12). Empty (default) ⇒ in-VPC-only, fail-closed: an
    #: external rerank host (e.g. a rented demo GPU) is a per-deployment,
    #: explicitly-listed decision.
    #:
    #: **AIR_GAP dominates this allowlist ENTIRELY.** Under an engaged air gap a
    #: host admitted only by this setting is refused — a public IP literal and a
    #: DNS name alike — because the platform rule (origin: :mod:`ne.storage.s3`,
    #: "an env var must not be able to punch a hole through the air gap") is that
    #: the operator allowlist contributes nothing under the gap and only provably
    #: in-VPC shapes pass. Enforced at construction AND before every request, so a
    #: governed runtime air-gap flip stops an already-built reranker from sending
    #: (:func:`ne.net.ssrf.allowlist_revoked_by_air_gap`). Consequence for the
    #: demo: with ANY external GPU rerank host listed here, however it is spelled,
    #: engaging the air gap makes search fail CLOSED — revert to ``tei`` before the
    #: air-gap beat.
    #:
    #: The demo lane this exists for is cleartext ``http://`` to a vLLM server
    #: started with no ``--api-key``: synthetic corpus only, never a real one.
    #:
    #: NOTE: adding this field is an EXPECTED release-level ``config_hash`` schema
    #: change (the ``tei_rerank_url`` precedent verbatim), PR-flagged, NOT a
    #: mid-process mutation.
    rerank_allowed_hosts: tuple[str, ...] = ()
    #: The served-model NAME the ``vllm`` rerank backend puts in the request body
    #: (``vllm serve --served-model-name``). Used ONLY when ``rerank_backend`` is
    #: ``vllm``; the TEI backend serves exactly one model and takes no name. The
    #: default matches ``deploy/inference-operator/vast-rig.sh serve rerank``. NOTE: adding this
    #: field is an EXPECTED release-level ``config_hash`` schema change (the
    #: ``tei_rerank_url`` precedent verbatim), PR-flagged, NOT a mid-process
    #: mutation.
    rerank_served_model_name: str = "rerank"

    #: Truncate the text sent to the rerank SERVER to this many characters
    #: (NE-RERANK-CPU). ``None`` (default) ⇒ off, the chunk is scored verbatim.
    #: Threaded into BOTH adapters, so it is a property of the rerank STAGE, not
    #: of one server. It never changes what is RETURNED: a ``ScoredChunk`` always
    #: carries the full ``Chunk``, so reader text, citations and the context
    #: budget are byte-identical either way.
    #:
    #: This is a QUALITY knob wearing a latency costume, and the honest numbers
    #: are in :mod:`ne.brain.reranker`'s module docstring: on the live corpus
    #: (~701-char chunks, 20 candidates, ``tei-rerank`` at 12 cpus) a 600-char
    #: view took a search from 31.2 s to ~24-28 s and already reordered the
    #: top 5; the values fast enough to change the demo's shape (300 and below)
    #: changed which chunk ranked FIRST. Default off because a cross-encoder that
    #: scores less than it was given is a silent retrieval downgrade, and the
    #: measured latency it buys does not pay for that — the CPU-lane fix that
    #: does pay is cores (``tei-rerank`` cpus 4 -> 12, 78.1 s -> 31.2 s, ordering
    #: unchanged) and, past that, the GPU rerank lane.
    #:
    #: NOTE: adding this field is an EXPECTED release-level ``config_hash`` schema
    #: change (the ``tei_rerank_url`` precedent verbatim), PR-flagged, NOT a
    #: mid-process mutation. It belongs IN the hash — unlike the
    #: ``NE_TEI_RERANK_TIMEOUT_S`` escape hatch, this one changes what the
    #: platform scores, so a deployment that turns it on must be distinguishable.
    rerank_scoring_char_limit: int | None = Field(default=None, ge=1)

    #: The chat knowledge-tool retrieval fan-out (SPEC §5 NE-CTXB-6). DECIDED NAME
    #: ``retrieval_top_k`` — NOT bare ``top_k`` — because ``ApiSettings`` serves many
    #: surfaces (the skills ``search_top_k`` field and the ``/v1/brain`` route's own
    #: per-request ``top_k`` both coexist on this service; an unqualified name would
    #: be ambiguous). This is threaded into ``build_chat_bindings(top_k=...)`` at the
    #: composition root (``build_platform``), which is itself the ``top_k`` argument
    #: ``RetrievalPipeline.search`` reranks candidates down to — there is no second
    #: rerank-top_k knob. Default 5 keeps the composed platform BYTE-IDENTICAL to the
    #: previous hardcoded ``build_chat_bindings`` default. ``ge=1`` fails loud on a
    #: non-positive value; there is deliberately NO upper bound — the cost control is
    #: the documented coupling below, not a clamp. COST COUPLING: the
    #: ``RetrievalPipeline`` over-fetches ``candidate_k = retrieval_top_k *
    #: candidate_multiplier`` (``candidate_multiplier=4`` in ``build_platform``) fused
    #: candidates and reranks ALL of them through the in-VPC TEI reranker in one call
    #: — raising ``retrieval_top_k`` multiplies per-search TEI rerank calls/tokens (and
    #: underlying retrieval load) by the SAME multiplier. NOTE: adding this field is an
    #: EXPECTED release-level ``config_hash`` schema change (the ``tei_rerank_url``/
    #: ``vector_store`` precedent verbatim — ``ApiSettings`` freezes into the hash
    #: under ``"ne-api"``), PR-flagged, NOT a mid-process mutation.
    retrieval_top_k: int = Field(default=5, ge=1)

    #: Vector-store backend selection (NE-091). ``qdrant`` (default) keeps the
    #: composed platform BYTE-IDENTICAL; ``pgvector`` selects the Postgres-first
    #: :class:`ne.brain.pgvector.store.PgvectorChunkStore` (registered under the
    #: SAME ``ne.plugins.vector_store`` entry-point slot) so retrieval runs on the
    #: platform Postgres with NO separate Qdrant service. NOTE: adding these three
    #: fields changes the composed ``config_hash`` vs pre-091 — an EXPECTED
    #: release-level schema change (the 104b/104c precedent; ApiSettings freezes
    #: into the hash under ``"ne-api"``), flagged in the PR body, NOT a
    #: mid-process mutation.
    vector_store: Literal["qdrant", "pgvector"] = "qdrant"
    #: Postgres DSN for the pgvector store (required IFF ``vector_store=pgvector``;
    #: empty then is a boot ``ValueError`` — fail loud). C2: in-network Postgres.
    pgvector_dsn: str = ""
    #: pgvector chunk table name (``vector_store=pgvector`` only).
    pgvector_table: str = "ne_chunks"

    #: HITL approval role pre-check (empty ⇒ nobody — fail closed). Wired in 104b.
    approver_roles: tuple[str, ...] = ()
    #: Steward role pre-check (empty ⇒ nobody). Wired in 104c.
    steward_roles: tuple[str, ...] = ()
    #: Audit-read role pre-check (empty ⇒ nobody). Wired in 104b.
    ops_roles: tuple[str, ...] = ()
    #: NE-AUDIT-EXPORT — server hard cap on ONE ``GET /v1/audit/export`` CSV.
    #: Exceeding it is NEVER silent: the CSV preamble's ``rows_matched``/
    #: ``rows_exported``/``truncated`` lines and the ``X-Neutron-Export-Truncated``
    #: response header both say so.
    #: NOTE: adding this field is an EXPECTED release-level config_hash schema
    #: change (the 104b/104c precedent — ApiSettings freezes into the hash under
    #: "ne-api").
    audit_export_max_rows: int = 100_000
    #: NE-AUDIT-EXPORT — the keyset batch size ``GET /v1/audit/export`` streams in
    #: (bounds memory: the full result set is never materialised). A value above
    #: ``ne.audit.MAX_PAGE_ROWS`` is CLAMPED by the export loop rather than
    #: honoured — the loop derives that ceiling instead of restating it, so an
    #: oversized value costs nothing and can never end the stream early.
    #: NOTE: adding this field is an EXPECTED release-level config_hash schema
    #: change (the 104b/104c precedent — ApiSettings freezes into the hash under
    #: "ne-api").
    audit_export_batch_rows: int = 500
    #: createSession agent_name allowlist.
    chat_agents: tuple[str, ...] = ("console-chat",)
    #: PRE-008 declarative Agent registry. Empty is the fail-closed, empty-catalog
    #: default; a non-empty path must exist and validate at process boot.
    agent_manifest_path: str = ""
    #: Canonical default Ask activation. Empty is fail-closed/dormant.
    default_ask_generation: Literal["", "default-ask/canonical-run/v1"] = ""
    #: Optional static console dist path — DECLARED for §3.4 parity but NOT wired
    #: here (the static console mount is NE-103b, not 104a).
    serve_console_dist: str = ""
    #: The PUBLIC SPA/PKCE client_id that ``GET /config.json`` advertises to the
    #: console at runtime (NE-CONSOLE-ISSUER). A plain ``str`` public value — NEVER
    #: a secret (the SPA is a public PKCE client with no credential). The pilot
    #: realm's public client is ``ne-console``, so this default matches
    #: deploy/compose/pilot; a per-customer deployment overrides it via
    #: ``NE_API_CONSOLE_CLIENT_ID``. NOTE: adding this field changes the composed
    #: ``config_hash`` vs merged main — the 104b/104c release-level precedent
    #: (see ``eval_database_url``/``scim_bearer_token`` above and the ``app.py``
    #: module docstring), PR-flagged, NOT a mid-process mutation.
    console_client_id: str = "ne-console"
    #: SSE comment keep-alive cadence, seconds (spec §2.4).
    sse_keepalive_s: float = 15.0

    #: Read engine URL for ``GET /v1/eval/runs`` (spec §1.6 #11). Empty ⇒ the route
    #: answers 503 (fail LOUD, never silently empty); the pilot sets
    #: ``NE_API_EVAL_DATABASE_URL`` to the platform Postgres. NOTE: adding this
    #: field (and the ``ne-obs`` extra_modules entry in ``app.py`` step 2) changes
    #: the composed ``config_hash`` vs the just-merged 104a — an EXPECTED
    #: release-level schema change, called out in the PR body, NOT a mid-process
    #: mutation.
    eval_database_url: str = ""

    #: Turn timeout (R9): a turn that has not finished by this deadline emits one
    #: terminal ``error`` frame; the pending approval row stays actionable.
    turn_timeout_s: float = 120.0
    #: Fail-safe local bind host; the NE-103a image overrides via env.
    bind_host: str = "127.0.0.1"
    #: Fail-safe local bind port; the NE-103a image overrides via env.
    bind_port: int = 8000

    #: Turn execution placement (NE-103c). ``inline`` (default) runs the turn
    #: in-process — NE-104a's byte-identical passthrough. ``enqueue`` hands the turn
    #: to the durable ``ne-worker`` (``DBOSClient``, ``workflow_id=turn_id``) and
    #: relays the worker's frame stream. The pilot ships ``enqueue``.
    #:
    #: HASH note (release-level, PR-flagged): adding this field + ``dbos_system_
    #: database_url`` changes the composed ``config_hash`` vs the just-merged
    #: NE-103b — the 104b/104c precedent (ApiSettings freezes into the hash under
    #: the ``"ne-api"`` key), NOT a mid-process mutation.
    turn_execution: Literal["inline", "enqueue"] = "inline"
    #: The DBOS system database URL for ne-api's ``DBOSClient`` (enqueue mode). An
    #: empty value in ``enqueue`` mode is a boot ``ValueError`` (fail loud). This is
    #: the SAME dedicated ``ne_dbos`` database the worker launches its schema in.
    dbos_system_database_url: str = ""

    #: SCIM 2.0 provisioning bearer secret (NE-089). Empty (default) ⇒ the
    #: `/scim/v2` mount is ABSENT (fail-closed default — feature off means the
    #: endpoint 404s, not "unauthenticated but present"). When set, MUST be
    #: >=16 chars — `create_app` fails LOUD (`ValueError`) at factory time on a
    #: shorter secret (a weak-secret guard, not a runtime 401). `SecretStr` so
    #: `compute_config_hash` masks it (`ne.contracts.config`) — the value never
    #: enters the composed config_hash, never logs, never appears in an audit
    #: `detail`. NOTE: adding this field is an EXPECTED release-level
    #: `config_hash` schema change (the 104b/104c precedent verbatim — see the
    #: module docstring in `app.py` and the PR body), NOT a mid-process mutation.
    scim_bearer_token: SecretStr = SecretStr("")

    #: CDC sub-minute freshness lane (NE-084). Empty (default) ⇒ the `/cdc/v1`
    #: mount is ABSENT (fail-closed default — the endpoint 404s, not
    #: "unauthenticated but present"). When set, MUST decode via
    #: `ne.connect.cdc.decode_webhook_secret` (a `whsec_`+base64, 24..64 raw
    #: bytes) — `build_platform` fails LOUD (`ValueError`) at factory time on a
    #: malformed secret. `SecretStr` so `compute_config_hash` masks it
    #: (`ne.contracts.config`) — the value never enters the composed config_hash,
    #: never logs, never appears in an audit `detail`. NOTE: adding this field
    #: (and the two below) is an EXPECTED release-level `config_hash` schema
    #: change (the NE-089 `scim_bearer_token` precedent verbatim — see the PR
    #: body), NOT a mid-process mutation.
    cdc_webhook_secret: SecretStr = SecretStr("")
    #: JSON document for `ne.connect.cdc.CdcSinkConfig.from_json` (the per-table
    #: key/content/ACL/PII map). REQUIRED non-empty when `cdc_webhook_secret` is
    #: set — `build_platform` fails LOUD otherwise (a CDC lane with no governed
    #: table map would silently quarantine every event as unknown-table).
    cdc_tables: str = ""
    #: Filesystem path for the CDC quarantine lane (`SqliteWriteQuarantine`).
    #: REQUIRED non-empty when `cdc_webhook_secret` is set. Document the tmpfs
    #: caveat: losing the lane record loses INSPECTABILITY, never safety —
    #: quarantined rows were never written to the knowledge store.
    cdc_quarantine_path: str = ""

    #: NE-DEMO-LIVE-SEAMS — DEFAULT-OFF demo-deployment seams. All default
    #: to the byte-identical no-op; the demo overlay (never a production default)
    #: turns them on. NOTE: adding these fields is an EXPECTED release-level
    #: `config_hash` schema change (the `tei_rerank_url`/`vector_store` precedent
    #: verbatim — ApiSettings freezes into the hash under `"ne-api"`), PR-flagged,
    #: NOT a mid-process mutation. The DEMO's differing config_hash is legitimate
    #: (it enables optional modules) and honestly stamped.
    #:
    #: Load the manifest-verified Meridian banking projection into the exact T3
    #: store resolved by the production composition root. Empty (default) is a
    #: no-op. A non-empty path requires verified answers and all six packaged
    #: Meridian templates; missing or malformed fixture inputs fail at boot.
    demo_meridian_fixture_path: str = ""
    #: Run the boot-time `PropertyGraphLift` from the composed graph store into the
    #: T3 ValidatingTripleStore so a verified-answer template returns a real table.
    #: Requires the ontology read surface AND the graph tier enabled (else a boot
    #: `ValueError`).
    demo_ontology_lift: bool = False
    #: Register the governed TIER-2 `write.external.demo.send` binding + its
    #: ToolArgPolicy through the frozen C6 executor so the write-back arc can
    #: deny-then-HITL-approve-then-execute LIVE.
    demo_writeback_tool: bool = False
    #: NE-168 — construct a `GovernedSubAgentDispatcher` (3 AgentDefinitions:
    #: triage/reason -> private-vllm, adjudicate -> frontier-api with the NE-161
    #: runtime gate left CLOSED) and expose the ops-gated `POST /v1/demo/delegate`
    #: route so `ne demo` visibly exercises governed multi-lane agent-to-model
    #: delegation under ONE run trace_id. Default OFF (byte-identical): the
    #: dispatcher is `None`, the route answers 404. NOTE: adding this field is an
    #: EXPECTED release-level `config_hash` schema change (the `demo_writeback_tool`
    #: precedent verbatim — ApiSettings freezes into the hash under `"ne-api"`),
    #: PR-flagged, NOT a mid-process mutation.
    demo_delegation: bool = False
    #: NE-WORKFLOWS — construct the workflow drafting dispatcher and expose the
    #: ops-gated `GET /v1/workflows` + `POST /v1/workflows/{id}/run` routes. The
    #: offered catalog is derived from the exact governed tool menu: write workflows
    #: require their write bindings, while
    #: read-only Meridian workflows compose with `write_actions_enabled=False`.
    #: Default OFF (byte-identical): the dispatcher is `None` and both routes answer
    #: 404. NOTE: adding this field is an
    #: EXPECTED release-level `config_hash` schema change (the `demo_delegation`
    #: precedent verbatim), PR-flagged, NOT a mid-process mutation.
    workflows_enabled: bool = False
    #: NE-FRONTIER-LIVE — allow the governed model-selection authority to issue
    #: ONE real frontier completion (`ne.api.frontier_warm`) when a run is being
    #: AUTHORIZED onto a frontier route that has no current serving evidence.
    #: Without this a deliberately staged frontier lane can never earn the
    #: 60-second observation `ModelSelectionService` requires, so it stays
    #: invisible in the picker and — where the route declares private fallbacks
    #: — degrades onto them SILENTLY. Default OFF and byte-identical: no warmer
    #: is constructed, the selection authority makes no provider call of its own,
    #: and every existing catalog/authorize path behaves exactly as before. Turn
    #: it on ONLY where a frontier lane was deliberately staged (`ne demo up
    #: --frontier <provider>` sets it); it is a real, billable outbound call the
    #: platform makes on its own initiative, and under AIR_GAP it is unreachable
    #: (frontier candidates are already dropped before the warm branch) so the
    #: flag can never loosen a sealed deployment. Display reads never warm.
    #: NOTE: adding this field is an EXPECTED release-level `config_hash` schema
    #: change (the `workflows_enabled` precedent verbatim), PR-flagged.
    frontier_warm_enabled: bool = False
    #: NE-GA-G7 — a DEFAULT-OFF demo-only seam: wraps the composed model gateway
    #: in a `DemoGroundedHallucinationGateway` (`ne.api.demo_seams`) that, for
    #: prompts carrying the existing `[grounded-hallucination]` marker, scripts
    #: a fabricated ungrounded draft over the grounded-analyst agent — so the
    #: demo's refuse leg fires LIVE against the real pod model (not only in the
    #: CI fixture arc). The REAL `ne.govern.grounding` gate still runs on the
    #: fabricated draft and refuses it (never weakened — the seam only supplies
    #: the fabrication). Requires `OntologySettings.grounded_agent_enabled`
    #: (else a boot `ValueError`: the seam only makes sense over the
    #: grounded-analyst exhibit). Default OFF (byte-identical): `build_platform`
    #: never constructs the wrapper. NOTE: adding this field is an EXPECTED
    #: release-level `config_hash` schema change (the `demo_delegation`
    #: precedent verbatim — `ApiSettings` freezes into the hash under
    #: "ne-api"), PR-flagged, NOT a mid-process mutation.
    demo_grounded_hallucination: bool = False

    #: NE-ACTION-WIRE — register the 3 NE-050 reference governed write actions
    #: (``write.internal.crm.note`` Tier-2 / ``write.external.ticket.update``
    #: Tier-3 / ``draft.email.create`` Tier-2) as standard bindings in the SAME
    #: frozen C6 executor, composed via ``build_action_bindings`` (never a second
    #: executor). Default OFF (byte-identical): `build_platform` never constructs
    #: the dedup store or the write-action clients, and none of the 3 tool names
    #: are registered. Exposure when enabled is EVERY non-grounded chat agent,
    #: HITL-gated purely by the existing PDP/approval-tier system (the grounded
    #: agent stays structurally excluded via its existing ``ToolsetScopedExecutor``
    #: wrap). Enabling this WITHOUT the full connector settings for every leg
    #: (``NE_SQL_*`` / ``NE_SALESFORCE_*`` / ``NE_M365_*``) is a loud boot
    #: ``ValueError`` naming every missing leg — never a silently-absent tool.
    #: NOTE: adding this field is an EXPECTED release-level ``config_hash`` schema
    #: change (the ``demo_writeback_tool`` precedent verbatim — ``ApiSettings``
    #: freezes into the hash under ``"ne-api"``), PR-flagged, NOT a mid-process
    #: mutation.
    write_actions_enabled: bool = False
    #: Select an exact subset of the reference governed write actions without
    #: forcing unrelated connector legs to be configured.  This is the
    #: general-purpose successor to the legacy all-three switch above: each
    #: selected tool resolves, hashes, and constructs only its own connector.
    #: The tuple must be sorted and duplicate-free so semantically identical
    #: deployments cannot freeze to different config hashes.  Setting both
    #: selectors is rejected instead of inventing precedence.
    write_action_tools: tuple[
        Literal[
            "draft.email.create",
            "write.external.email.send",
            "write.external.ticket.update",
            "write.internal.crm.note",
        ],
        ...,
    ] = ()
    #: Exact normalized mailbox identities that ``draft.email.create`` may own.
    #: This is destination authority, not a credential.  Empty is fail-closed
    #: whenever the draft action is selected; the provider wrapper checks exact
    #: membership before token acquisition or request construction.
    email_draft_allowed_mailboxes: tuple[str, ...] = ()
    #: NE-CS-SEND — exact normalized recipient addresses ``write.external.email.send``
    #: may ever deliver to, enforced against the draft's ACTUAL Graph-read recipient
    #: set after the Tier-2 human approval and before any egress. Empty is
    #: fail-closed whenever the send action is selected: a deployment must opt in
    #: to every reachable address one by one (no domain wildcards).
    email_send_allowed_recipients: tuple[str, ...] = ()
    #: Closed, config-hashed inference lifecycle registry and local UDS transport.
    #: Empty targets keep lifecycle disabled/read-only; no executable detail is
    #: admitted by this model (ADR-0021).
    inference_lifecycle: InferenceLifecycleConfig = InferenceLifecycleConfig()

    @field_validator("email_draft_allowed_mailboxes", "email_send_allowed_recipients")
    @classmethod
    def _normalize_email_draft_mailboxes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not mailbox.strip() for mailbox in value):
            raise ValueError("email address allowlists cannot contain an empty value")
        return tuple(sorted({mailbox.strip().casefold() for mailbox in value}))

    @model_validator(mode="after")
    def _validate_write_action_selection(self) -> ApiSettings:
        if self.write_actions_enabled and self.write_action_tools:
            raise ValueError("write_actions_enabled and write_action_tools are mutually exclusive")
        canonical = tuple(sorted(set(self.write_action_tools)))
        if self.write_action_tools != canonical:
            raise ValueError("write_action_tools must be sorted and duplicate-free")
        draft_selected = "draft.email.create" in self.write_action_tools
        if draft_selected and not self.email_draft_allowed_mailboxes:
            raise ValueError(
                "draft.email.create requires at least one email_draft_allowed_mailboxes entry"
            )
        # The RECIPIENT allowlist is the send's hard opt-in (where mail can go);
        # the mailbox allowlist stays optional here for parity with the draft
        # action's enabled-mode semantics (empty ⇒ the connector credential is
        # the mailbox authority, exactly as for drafts).
        send_selected = "write.external.email.send" in self.write_action_tools
        if send_selected and not self.email_send_allowed_recipients:
            raise ValueError(
                "write.external.email.send requires at least one "
                "email_send_allowed_recipients entry (deny-by-default: every reachable "
                "recipient is an explicit opt-in)"
            )
        return self
