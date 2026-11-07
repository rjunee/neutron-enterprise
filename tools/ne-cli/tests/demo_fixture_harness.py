# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO fixture-mode ne-api builder (NOT a test file).

A trimmed, vendored adaptation of packages/ne-api/tests/conftest.py: the REAL
governed spine (OidcAuthMiddleware over a fake TokenVerifier, DurableApprovalGate,
GovernedToolExecutor, PrincipalAclFilter, PostgresAuditSink over SQLite, the REAL
GovernedPdp over a scripted OPA wire) runs for real; only network EDGES are faked
(the model gateway, embedder/sparse, the Qdrant store).

NE-DEMO-LIVE-SEAMS: the demo's FIVE governed surfaces are now exercised through the
REAL production demo seams (``ne.api.demo_seams`` + the ApiSettings demo flags), not
test-local bindings:

  (a) T2 graph — ``seams.brain_graph_settings=BrainGraphSettings(enabled=True)`` +
      ``seams.graph_store`` seeded from the governed ``corpus.build_graph_snapshot``;
  (b) T3 verified answers — ``ApiSettings(demo_ontology_lift=True)`` runs the REAL boot
      ``PropertyGraphLift`` into the REAL ``oxigraph`` triple store (resolved through the
      real entry point) over the ``corpus.write_verified_templates`` bundle. NE-VAF-U5:
      that bundle now carries ``[routing]``, so the composition root also builds a REAL
      ``ne.runtime.verified_router.VerifiedRouter`` — T3 chat turns route, clarify, or
      are refused DETERMINISTICALLY, never via the model gateway;
  (c) tier-2 HITL — ``ApiSettings(demo_writeback_tool=True)`` registers the production
      ``write.external.demo.send`` binding through the frozen C6 executor. NE-DEMO-
      WRITEBACK-LIVE: passing ``write_action_clients=`` to :func:`build_demo_app`
      ADDITIONALLY sets ``ApiSettings(write_actions_enabled=True)`` +
      ``AppSeams.write_action_clients``, registering the REAL NE-050
      ``write.internal.crm.note`` (+ ticket/draft) bindings through the SAME frozen
      C6 executor — the seam-injection precedent means no connector env is needed
      (``platform.py``'s ``write_action_env_settings`` resolution is skipped
      whenever a seam client is already injected);
  (d) NE-168 delegation — ``ApiSettings(demo_delegation=True)`` composes a
      ``GovernedSubAgentDispatcher`` (triage/reason -> private-vllm, adjudicate ->
      frontier-api, RUNTIME GATE closed) exposed via the ops-gated
      ``POST /v1/demo/delegate``;
  (e) NE-GA-G7 grounded-hallucination — ``ApiSettings(demo_grounded_hallucination=True)``
      composes ``DemoGroundedHallucinationGateway`` OVER this harness's gateway seam.
      The ``[grounded-hallucination]``-marked refuse leg's fabrication now comes from
      that REAL production seam, not test-local scripting: this
      ``PromptRoutedGateway`` no longer branches on the marker at all (it only ever
      sees the UNMARKED pass-leg turns + the unmarked ``grounding-assist`` branch
      still needed for the PASS leg's assist determinism) — the seam intercepts every
      marked turn before it ever reaches this gateway.

No pytest-asyncio (repo policy): async is driven via :func:`run`. The gateway is
PROMPT-ROUTED (not a flat queue) so cross-turn completion ordering can never drift.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import httpx
import sqlalchemy as sa
from starlette.routing import Mount

from ne.api import AppSeams, create_app
from ne.api.demo_seams import (
    DEMO_GROUNDED_ASSIST_JUNK,
    DEMO_HALLUCINATED_DRAFT,
    DEMO_WRITEBACK_ACTION,
)
from ne.api.deps import ApiState
from ne.api.platform import WriteActionClients
from ne.audit.config import AuditSettings
from ne.cli.demo.arc import CRM_WRITEBACK_ACTION
from ne.cli.demo.live import DEMO_VERIFIED_MAX_RESULT_TOKENS
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.identity import TokenClaims
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Chunk, Lane, ScoredChunk, SparseVector
from ne.gateway.config import GatewaySettings, ModelDef
from ne.govern.opa import OpaClient
from ne.govern.pdp import GovernedPdp
from ne.identity.config import IdentitySettings, OidcConfig
from ne.identity.errors import TokenVerificationError
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import ApprovalSettings, RuntimeSettings

ISSUER = "https://idp.ne.test/realms/ne-test"
AUDIENCE = "ne-api"
_DECISION_PATH = "/v1/data/ne/govern/decision"

#: Re-exported from the production seam (grep-provable single source of the action).
WRITEBACK_ACTION = DEMO_WRITEBACK_ACTION


def run(coro: Any) -> Any:
    """Drive a coroutine to completion (no pytest-asyncio in this repo)."""
    return asyncio.run(coro)


# --- NE-DEMO-WRITEBACK-LIVE: fake write-action connector clients -------------
#
# Structural doubles for the narrow ``ne.runtime.reference_actions`` Protocols,
# mirroring packages/ne-api/tests/test_write_actions_platform.py's
# _FakeCrmClient/_FakeTicketClient/_FakeDraftClient shapes verbatim. Re-declared
# here rather than imported — cross-test-package imports are impossible (ne-cli
# and ne-api are separate installed packages under separate tests/ trees).


class FakeCrmClient:
    """Records ``(sobject, fields)`` calls; returns a fixed fake Note id."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def create_record(self, sobject: str, fields: Mapping[str, str]) -> str:
        self.calls.append((sobject, dict(fields)))
        return "003000000000001AAA"


class FakeTicketClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int | str, dict[str, str]]] = []

    async def update_record(
        self, *, table: str, key_column: str, key_value: int | str, fields: Mapping[str, str]
    ) -> int:
        self.calls.append((table, key_column, key_value, dict(fields)))
        return 1


class FakeDraftClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def create_draft(self, *, mailbox: str, subject: str, body: str, to: str) -> str:
        self.calls.append((mailbox, subject, body, to))
        return "AAMkAG-draft-1"


# --- fake token verifier (vendored) -----------------------------------------


class FakeTokenVerifier:
    """Maps registered opaque tokens → verified ``TokenClaims``; else raises."""

    def __init__(self, _oidc: OidcConfig | None = None) -> None:
        self._tokens: dict[str, TokenClaims] = {}

    def mint(
        self,
        subject: str,
        *,
        groups: Sequence[str] = (),
        department: str | None = None,
    ) -> str:
        token = uuid.uuid4().hex
        claims: dict[str, Any] = {"sub": subject, "iss": ISSUER, "groups": list(groups)}
        if department is not None:
            claims["department"] = department
        self._tokens[token] = TokenClaims(
            subject=subject,
            issuer=ISSUER,
            claims=claims,
            verified_expires_at=(datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0),
        )
        return token

    async def verify(self, token: str) -> TokenClaims:
        claims = self._tokens.get(token)
        if claims is None:
            raise TokenVerificationError("unknown token")
        return claims


# --- Rego-faithful scripted OPA transport (below the REAL GovernedPdp) -------


def _classify(action: str) -> int | None:
    tiers: set[int] = set()
    if action.startswith("read.") or action.startswith("draft."):
        tiers.add(0)
    if action.startswith("write.internal."):
        tiers.add(1)
    if action.startswith("write.external."):
        tiers.add(2)
    if ".pii." in action:
        tiers.add(2)
    if action.startswith("finance.") or action.startswith("legal."):
        tiers.add(3)
    return max(tiers) if tiers else None


def _rego_faithful_doc(action: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    if action == "subagent.route":
        # Mirrors packages/ne-govern/policies/ne/govern/subagent.rego (NE-169,
        # bundle 1.1.0): private/unpinned resolved lanes are tier-0 allow;
        # frontier is a total deny; anything else is unclassified.
        route_tier = (context or {}).get("route_tier")
        if route_tier in ("private", "unpinned"):
            return {
                "effect": "allow",
                "tier": 0,
                "reason": "tier 0: read/internal draft — autonomous, logged",
                "obligations": {},
                "policy_version": "1.1.0",
            }
        if route_tier == "frontier":
            return {
                "effect": "deny",
                "tier": None,
                "reason": (
                    "subagent.route: frontier-lane routing requires an explicit "
                    "grant — default deny"
                ),
                "obligations": {},
                "policy_version": "1.1.0",
            }
        return {
            "effect": "deny",
            "tier": None,
            "reason": "unclassified action",
            "obligations": {},
            "policy_version": "1.1.0",
        }

    tier = _classify(action)
    effect = "deny" if tier is None else ("allow" if tier <= 1 else "require-approval")
    return {
        "effect": effect,
        "tier": tier,
        "reason": f"rego-faithful: {effect} (tier {tier})",
        "obligations": {},
        "policy_version": "1.0.0",
    }


def _rego_faithful_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == _DECISION_PATH
        payload = json.loads(request.content)
        action = payload["input"]["action"]
        context = payload["input"].get("context") or {}
        return httpx.Response(200, json={"result": _rego_faithful_doc(action, context)})

    return httpx.MockTransport(handler)


class RegoFaithfulPdpFactory:
    """A ``policy_decision_point`` plugin returning the REAL GovernedPdp over a
    scripted OPA transport (OPA is a network wire edge)."""

    @staticmethod
    def from_settings(_settings: Any, *, sink: Any) -> GovernedPdp:
        client = httpx.AsyncClient(base_url="http://opa.test", transport=_rego_faithful_transport())
        return GovernedPdp(OpaClient("http://opa.test", client=client), sink)


# --- prompt-routed model gateway --------------------------------------------


def _tool_call_completion(name: str, arguments: dict[str, Any]) -> Completion:
    line = json.dumps({"tool_call": {"name": name, "arguments": arguments}})
    return Completion(text=line, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


def _text_completion(text: str) -> Completion:
    return Completion(text=text, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


#: NE-HGATE-3: re-exported from the production seam (grep-provable single source of
#: the assist-junk bytes) — like ``_HALLUCINATED_DRAFT`` below. Scripted for EVERY
#: ``task="grounding-assist"`` completion regardless of branch or kwargs. It carries
#: an UNGROUNDABLE NUMERAL (424242), so ``check_grounding`` refuses it on a
#: ``numeral-ungrounded`` FACT-class finding at EVERY tier x profile — never
#: prompt-exempt, never advisory — pinning ``assist_used=False`` on both grounded
#: arc legs deterministically. A numeral-free junk would be ADOPTED at STANDARD
#: (advisory-only findings), breaking the refuse-to-hallucinate beat.
_ASSIST_JUNK = DEMO_GROUNDED_ASSIST_JUNK

#: NE-GA-G7: re-exported from the production seam (grep-provable single source of
#: the fabricated draft's bytes) — the ``[grounded-hallucination]`` leg's fabrication
#: is now supplied by the REAL ``DemoGroundedHallucinationGateway``, not this
#: gateway. Kept under this module's original name so ``test_demo_arc.py``'s
#: drift-tripwire import (``from .demo_fixture_harness import _HALLUCINATED_DRAFT``)
#: stays valid.
_HALLUCINATED_DRAFT = DEMO_HALLUCINATED_DRAFT

#: The tool-result content prefix ``GatewayModel._map_request_parts`` renders for
#: a ``ToolReturnPart`` (model.py:158-163): ``f"{tool_name} returned: {content}"``.
#: NE-CTXB-8: the grounded beat now targets a SINGLE server-side verified COUNT
#: aggregate (``read.kg.orders_per_account_count``) instead of the two directory
#: reads — the model reasons over ~dozens of per-account group rows, not two
#: 500-row tables (THIS is the layer that scales). The old two-fetch
#: accounts→orders chain constants are gone with it.
_AGG_TOOL_RETURNED = "read.kg.orders_per_account_count returned:"


def _last_user_index(messages: Any) -> int | None:
    """Index of the LAST ``role == "user"`` message, or ``None``."""
    idx: int | None = None
    for i, m in enumerate(messages):
        if getattr(m, "role", None) == "user":
            idx = i
    return idx


def _tool_contents_after_last_user(messages: Any) -> list[str]:
    """Every ``role == "tool"`` message's content AFTER the last user message.

    TURN-scoping (see the module docstring): a session is get-or-create across
    turns, so a later turn's history still carries an earlier turn's tool
    returns. Bounding the scan to strictly after the LAST user message means a
    prior turn's fetches can never leak into this turn's scripted draft.
    """
    idx = _last_user_index(messages)
    if idx is None:
        return []
    return [
        getattr(m, "content", "") or ""
        for m in messages[idx + 1 :]
        if getattr(m, "role", None) == "tool"
    ]


def _parse_compact_table(raw: str) -> tuple[list[str], list[list[str]]] | None:
    """Parse the §4.2 COMPACT model-facing rendering back into (columns, rows).

    The compact render (``ne.brain_ontology.verified.answers.render_answer_text``)
    is, per fetch:

        [showing {returned_rows} of {total or unknown} rows{, truncated (reason)}]
        col | col | ...
        ---+----+---            <- the '-+-' separator rule (line index 2)
        v | v | ...             <- one line per row (line index 3+)

    Returns ``None`` for anything that is not this shape (fewer than 3 lines, or
    a missing header). The tool-name prefix (``read.kg.accounts_directory`` vs
    ``read.kg.orders_directory``) — NOT a ``template_id`` (the compact render
    carries none) — is the caller's discriminator.
    """
    lines = raw.splitlines()
    if len(lines) < 3 or not lines[0].startswith("[showing "):
        return None
    columns = lines[1].split(" | ")
    rows = [ln.split(" | ") for ln in lines[3:] if ln]
    return columns, rows


def _grounded_draft(tool_contents: list[str]) -> str:
    """The scripted PASS-leg draft: reads the SINGLE server-side verified COUNT
    aggregate (``read.kg.orders_per_account_count``, parsed from THIS turn's
    tool-return content) into a per-account open-order-count answer — plan-time
    verified (byte-for-byte) to pass the REAL ``ne.govern.grounding.check_grounding``
    gate against the real acme fixtures with ZERO findings.

    NE-CTXB-8 (design §5.4): the grounded beat no longer joins two 500-row
    directory tables in the model's head — it targets ONE aggregate whose
    ``kind='count'`` rewrite already grouped the placed orders per account
    SERVER-SIDE. The model reasons over ~dozens of ``name | account | n_orders``
    group rows (truncated=false at demo scale) — THIS is the layer that scales.
    The scripted model parses the SAME compact ``col | col`` table a real model
    reads (NE-CTXB-5 §4.2: the verified-read return is the compact rendering, not
    the JSON), keyed off the tool-name PREFIX the gateway renders.

    Grounding rationale (probe-verified, zero findings):
      * the word ``"placed"`` is now UNGROUNDABLE (the aggregate corpus carries
        NO status cells — only ``account``/``name``/``n_orders``) and MUST NOT
        appear in the draft;
      * ``"open"`` and ``"orders"`` ground as prompt / template-id tokens
        (``G_QUESTION`` carries "open orders"; the template_id carries "orders");
      * each numeral (an ``n_orders`` aggregate cell) grounds as the CTXB-4
        aggregate-cell fact with its row's ``name`` cell as the single-sibling
        identity anchor (F3 — a DERIVED-aggregate numeral grounds when the
        sentence names its sibling row's identity for that group).

    Every count line is ALWAYS plural ("N open orders.", even for N == 1): the
    bare singular "order" is an ungroundable ("open", "order") token run (the
    corpus/prompt phrases only ever carry "orders"), while "orders" breaks the
    run cleanly (RISK 4 — retained precedent).
    """
    aggregate_table: tuple[list[str], list[list[str]]] | None = None
    for content in tool_contents:
        if content.startswith(_AGG_TOOL_RETURNED):
            _prefix, _sep, raw = content.partition("returned: ")
            aggregate_table = _parse_compact_table(raw)

    if aggregate_table is None:
        # Should never happen in fixture mode (the gateway only reaches this
        # branch after the aggregate fetch succeeded) — fail closed to a draft
        # that cannot possibly ground rather than raise inside a gateway stub.
        return "grounded draft unavailable: missing fetched data this turn."

    columns, rows = aggregate_table
    name_ix = columns.index("name")
    n_ix = columns.index("n_orders")

    lines = [
        "Verified accounts and orders data listed.",
        "Accounts with no open orders are not listed.",
    ]
    named = sorted((row[name_ix], int(row[n_ix])) for row in rows)
    for name, count in named:
        lines.append(f"{name}: {count} open orders.")
    return "\n".join(lines)


class PromptRoutedGateway:
    """A ``ModelGateway`` that routes on the LAST non-system message — TURN-scoped.

    ne-api reuses ONE chat session per principal, so a flat completion queue (or a
    scan for "any tool_call in history") would drift across turns. This gateway
    inspects only the LAST non-system message: a fresh USER prompt bearing a
    sentinel emits the matching tool_call; a TOOL-RESULT message (this turn already
    called its tool) emits a summary text.

    NE-VAF-U5: T3 chat now routes DETERMINISTICALLY through the composition-root
    ``ne.runtime.verified_router.VerifiedRouter`` turn pre-step — the model is
    NEVER consulted on the verified path (ROUTE/CLARIFY/REFUSE all bypass
    ``complete`` entirely; see ``agent.py``'s router short-circuit). This gateway
    therefore carries NO channel that can emit ``template_id`` — the old
    ``'[verified-answer]'`` tool-call branch and the ``'template_id'`` text-echo
    branch (the pre-U5 H1 forgery vector: free model text could flip the arc's
    "verified" bit) are gone. Only the tier-2 write-back sentinel remains.

    NE-GA-G6 / NE-CTXB-8: the grounded-analyst PASS leg is ALSO scripted here,
    deterministically, so the fixture arc exercises the REAL grounding gate
    (``ne.govern.grounding``) rather than a stub: a fresh ``g_question`` turn now
    fetches the SINGLE server-side verified COUNT aggregate
    (``read.kg.orders_per_account_count``) and emits a genuinely-grounded
    per-account draft (PASS) — the model reasons over ~dozens of pre-grouped rows
    instead of joining two 500-row directory tables in its head (the layer that
    scales). NE-CTXB-5 (§4.2): the scripted ``_grounded_draft`` parses the SAME
    compact model-facing table a real model sees (the verified-read return is the
    compact rendering — the JSON shape no longer reaches the model); the tool-name
    prefix is the discriminator (the compact render carries no ``template_id``). The
    ``task == "grounding-assist"`` branch is checked FIRST (before any
    message-content routing) because the G4 assist is a bare 2-message
    completion the composition root issues on EITHER gate branch — it is never
    reached via the "last non-system message" scan the ordinary turn uses.

    NE-GA-G7: the ``[grounded-hallucination]``-marked REFUSE leg is NO LONGER
    scripted here — this gateway carries no branch on that marker at all. The
    production ``DemoGroundedHallucinationGateway`` seam (composed over this
    gateway instance by ``build_platform`` when ``demo_grounded_hallucination``
    is on) intercepts every marked turn before it ever reaches ``complete``
    below, so the fixture arc proves the REAL demo seam end-to-end (the
    NE-DEMO-LIVE-SEAMS doctrine) instead of test-local scripting.
    """

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if request.task == "grounding-assist":
            return _text_completion(_ASSIST_JUNK)

        last_role = ""
        last_content = ""
        for m in request.messages:
            role = getattr(m, "role", None)
            if role and role != "system":
                last_role = role
                last_content = getattr(m, "content", "") or ""
        # A fresh user prompt → decide whether to call a tool.
        if last_role == "user":
            if "[writeback]" in last_content:
                # NE-DEMO-WRITEBACK-LIVE: the real CRM write-back's prompt renders
                # its args k='v'-sorted (ne.cli.demo.arc._writeback_prompt) — parse
                # them back out here, mirroring a real model reading values off the
                # prompt. Every OTHER writeback_action (the inert demo default)
                # keeps the byte-identical {"to": "ops"} emission.
                if CRM_WRITEBACK_ACTION in last_content:
                    parsed = dict(re.findall(r"(\w+)='([^']*)'", last_content))
                    return _tool_call_completion(
                        CRM_WRITEBACK_ACTION,
                        {k: parsed[k] for k in ("record_id", "title", "body")},
                    )
                return _tool_call_completion(WRITEBACK_ACTION, {"to": "ops"})
            if "how many open orders" in last_content:
                # NE-CTXB-8: ONE server-side verified COUNT aggregate fetch —
                # the model no longer fetches accounts THEN orders and joins.
                return _tool_call_completion("read.kg.orders_per_account_count", {})
            return _text_completion("acknowledged")
        # A tool result → summarize.
        if "BLOCKED" in last_content:
            return _text_completion("The governed action was blocked by policy.")
        if last_content.startswith(_AGG_TOOL_RETURNED):
            return _text_completion(
                _grounded_draft(_tool_contents_after_last_user(request.messages))
            )
        return _text_completion("sent")

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError("PromptRoutedGateway does not stream")


# --- brain edges (vendored) --------------------------------------------------


class StaticEmbedder:
    @property
    def model_id(self) -> str:
        return "static"

    @property
    def dimension(self) -> int:
        return 4

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]


class StaticSparseEncoder:
    @property
    def model_id(self) -> str:
        return "static-sparse"

    async def encode(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [SparseVector(indices=(), values=()) for _ in texts]

    async def encode_queries(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [SparseVector(indices=(), values=()) for _ in texts]


class PassthroughReranker:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def model_id(self) -> str:
        return "fake-reranker"

    async def rerank(
        self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
    ) -> Sequence[ScoredChunk]:
        self.calls += 1
        return tuple(ScoredChunk(chunk=c, score=1.0) for c in chunks[:top_k])


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return value


def _match_any(chunk: Chunk, filters: Mapping[str, Any] | None) -> bool:
    from ne.brain.trimming import ACL_ANY_KEY

    if not filters:
        return True
    if ACL_ANY_KEY in filters:
        return any(_match_any(chunk, sub) for sub in filters[ACL_ANY_KEY])
    for key, wanted in filters.items():
        if key == "acl":
            if not (set(chunk.acl) & set(wanted)):
                return False
        elif key == "document_id":
            if chunk.document_id not in set(wanted):
                return False
        else:
            return False
    return True


class InMemoryFilteredChunkStore:
    """A ChunkStore + FilteredChunkFetch honoring MatchAny on the 'acl' key."""

    def __init__(self, chunks: Sequence[Chunk] = ()) -> None:
        self._chunks: dict[str, Chunk] = {c.chunk_id: c for c in chunks}
        self.seen_filters: list[Any] = []

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        for c in chunks:
            self._chunks[c.chunk_id] = c.model_copy(update={"metadata": _plain(c.metadata)})

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        return [self._chunks[c] for c in chunk_ids if c in self._chunks]

    async def query(
        self,
        *,
        dense: Sequence[float] | None = None,
        sparse: SparseVector | None = None,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[ScoredChunk]:
        self.seen_filters.append(dict(filters) if filters else None)
        visible = [c for c in self._chunks.values() if _match_any(c, filters)]
        return [ScoredChunk(chunk=c, score=1.0) for c in visible[:top_k]]

    async def get_filtered(
        self, chunk_ids: Sequence[str], *, filters: Mapping[str, Any] | None = None
    ) -> Sequence[Chunk]:
        self.seen_filters.append(dict(filters) if filters else None)
        out: list[Chunk] = []
        for cid in chunk_ids:
            c = self._chunks.get(cid)
            if c is not None and _match_any(c, filters):
                out.append(c)
        return out

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        for cid in chunk_ids:
            self._chunks.pop(cid, None)


# --- T3 verified-answers config (the REAL seams do the seeding at boot) -------


def _ontology_settings(
    tmp_path: Path,
    *,
    verified_max_result_tokens: int | None = DEMO_VERIFIED_MAX_RESULT_TOKENS,
    grounding_strictness: Literal["strict", "standard"] = "strict",
    grounding_gate_profile: Literal["ng1-frozen", "ng2"] = "ng2",
    grounded_agent_name: str = "grounded-analyst",
    grounded_assist_enabled: bool = True,
) -> Any:
    """OntologySettings over the ``corpus.write_verified_templates`` bundle.

    ``resolve_triple_store`` opens the REAL ``oxigraph`` triple store through the entry
    point (in-memory, ``oxigraph_path=None``) and the REAL boot ``PropertyGraphLift``
    (``demo_ontology_lift=True``) populates it — no hand-seeded store remains.

    NE-CTXB-8: ``verified_max_result_tokens`` defaults to the production
    ``DEMO_VERIFIED_MAX_RESULT_TOKENS`` (8192) so the fixture composition mirrors
    the packing-ON demo overlay. Tests thread a SMALL cap (1024) to exercise the
    honest-truncation packing flow.

    NE-HGATE-4: ``grounding_strictness`` defaults to ``"strict"`` — the
    OntologySettings upgrade-default posture (B-P1-2) — so every existing caller is
    byte-identical; the overlay-boots test threads ``"standard"`` to prove the demo
    posture boots the real composition root.
    """
    from ne.brain_ontology.config import OntologySettings
    from ne.cli.demo import corpus

    directory = corpus.write_verified_templates(tmp_path / "verified-templates")
    return OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        verified_max_result_tokens=verified_max_result_tokens,
        grounding_strictness=grounding_strictness,
        grounding_gate_profile=grounding_gate_profile,
        backend="oxigraph",
        # NE-GA-G6: the demo's grounded-analyst exhibit — the ONE agent_name
        # whose turns get the grounding post-step + verified-reads-only toolset.
        # Existing callers keep the original defaults; the overlay composition
        # test passes the exact parsed deployment values through this seam.
        grounded_agent_name=grounded_agent_name,
        grounded_assist_enabled=grounded_assist_enabled,
        # The default True keeps the older fixture arc's assist branch active.
        # The production overlay test explicitly passes False to prove the
        # deployed exhibit is a pure deterministic hard gate.
        grounded_agent_enabled=True,
    )


def _demo_graph_store() -> Any:
    """A reference graph store seeded from the governed ACL-labeled corpus snapshot."""
    from ne.brain_graph.testing.memory import InMemoryGraphStore
    from ne.cli.demo import corpus

    snapshot = corpus.build_graph_snapshot(doc_ids=corpus.demo_doc_subset())
    store = InMemoryGraphStore()

    async def _seed() -> None:
        await store.upsert_nodes(snapshot["nodes"])
        await store.upsert_edges(snapshot["edges"])

    run(_seed())
    return store


def _admit_demo_graph_authority(graph_store: Any, runtime_url: str) -> None:
    """Stamp the static graph before the boot-time ontology lift consumes it."""
    from ne.brain.current_source_authority import (
        SqlAlchemyCurrentSourceAuthorityWriter,
        bind_deployment_construction_id,
        ensure_source_authority_schema,
        persist_current_acl_snapshot,
    )
    from ne.contracts.source_authority import (
        M365SourcePoint,
        SourceAdmissionFence,
        SourceAuthorityCandidate,
        SourceAuthorityStamp,
        SourceAuthorityState,
        SourceContentArtifactIdentity,
        SourceContractIdentity,
        SourceIdentity,
        SourceReconciliationScope,
    )

    engine = sa.create_engine(runtime_url)
    ensure_source_authority_schema(engine)
    construction_id = bind_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(
        engine, construction_id, freshness_sla_seconds=Decimal("86400")
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint",
        descriptor_schema_version=1,
        descriptor_hash=hashlib.sha256(b"ne-demo-fixture-source-contract-v1").hexdigest(),
    )

    async def _admit_all() -> None:
        rows = (*graph_store._nodes.values(), *graph_store._edges.values())
        labels = tuple(sorted({label for props in rows for label in props.get("acl", ())}))
        canonical_graph = json.dumps(
            {"edges": graph_store._edges, "nodes": graph_store._nodes},
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode()
        content_hash = hashlib.sha256(canonical_graph).hexdigest()
        acl_hash = hashlib.sha256(
            json.dumps(
                {"acl": list(labels)},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        identity = SourceIdentity(
            deployment_id=construction_id,
            source_kind="m365-sharepoint",
            connector_instance_id="ne-demo-fixture-sync",
            point=M365SourcePoint(
                graph_tenant_id="ne-demo-fixture",
                site_id="",
                drive_id="ne-demo-fixture",
                item_id="graph-dataset",
            ),
            generation=1,
        )
        artifact = SourceContentArtifactIdentity(
            source_identity=identity,
            source_snapshot_hash=content_hash,
            content_snapshot_hash=content_hash,
            acl_snapshot_hash=acl_hash,
            source_contract=contract,
            artifact_schema_version=1,
        )
        claim = await writer.claim(
            SourceReconciliationScope(path=("ne-demo-fixture-sync", "graph-dataset")),
            expected_head_revision=None,
            lease_seconds=60,
        )
        revision = f"fixture-{content_hash}"
        persist_current_acl_snapshot(
            engine,
            identity_hash=identity.identity_hash,
            artifact_id=artifact.artifact_id,
            acl_snapshot_hash=acl_hash,
            labels=labels,
        )
        receipt = await writer.finalize(
            claim,
            SourceAdmissionFence(
                deployment_id=identity.deployment_id,
                connector_instance_id=identity.connector_instance_id,
                source_generation=identity.generation,
                external_source_revision=revision,
                contract_epoch=1,
                reconciliation_scope=claim.scope,
                reconciliation_epoch=claim.reconciliation_epoch,
                fencing_token=claim.fencing_token,
            ),
            SourceAuthorityCandidate(
                source_identity=identity,
                artifact_identity=artifact,
                source_revision=revision,
                source_contract=contract,
                contract_epoch=1,
                acl_snapshot_hash=acl_hash,
                state=SourceAuthorityState.PRESENT,
                authority_as_of=datetime.now(UTC) - timedelta(seconds=1),
            ),
        )
        await writer.release(claim)
        stamp = SourceAuthorityStamp(
            source_identity=identity,
            artifact_identity=artifact,
            selected_head_revision=receipt.source_revision,
            acl_snapshot_hash=receipt.acl_snapshot_hash,
            source_contract=receipt.source_contract,
            contract_epoch=receipt.contract_epoch,
            authority_receipt_id=receipt.receipt_id,
            authority_receipt_hash=receipt.receipt_hash,
        ).model_dump(mode="json")
        for props in rows:
            props["source_authority_stamps"] = [stamp]

    try:
        run(_admit_all())
    finally:
        engine.dispose()


# --- the harness -------------------------------------------------------------


def get_state(app: Any) -> ApiState:
    for route in app.routes:
        if isinstance(route, Mount) and route.path == "/api":
            middleware: Any = route.app
            inner = middleware._app
            state = inner.state.ne_api
            assert isinstance(state, ApiState)
            return state
    raise AssertionError("no /api mount found")


@dataclass
class DemoAppHarness:
    app: Any
    verifier: FakeTokenVerifier
    chunk_store: InMemoryFilteredChunkStore
    gateway: PromptRoutedGateway
    audit_url: str
    runtime_url: str
    perms_url: str

    def bearer(
        self,
        subject: str,
        *,
        groups: Sequence[str] = (),
        department: str | None = None,
    ) -> dict[str, str]:
        token = self.verifier.mint(subject, groups=groups, department=department)
        return {"Authorization": f"Bearer {token}"}

    def client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app)
        return httpx.AsyncClient(transport=transport, base_url="http://ne.test")


def build_demo_app(
    tmp_path: Path,
    *,
    chunks: Sequence[Chunk] = (),
    verified_answers: bool = True,
    writeback_tool: bool = True,
    delegation: bool = True,
    grounded_hallucination: bool = True,
    verified_max_result_tokens: int | None = DEMO_VERIFIED_MAX_RESULT_TOKENS,
    gateway_settings: GatewaySettings | None = None,
    grounding_strictness: Literal["strict", "standard"] = "strict",
    grounding_gate_profile: Literal["ng1-frozen", "ng2"] = "ng2",
    grounded_agent_name: str = "grounded-analyst",
    grounded_assist_enabled: bool = True,
    chat_agents: tuple[str, ...] = ("console-chat", "grounded-analyst"),
    write_action_clients: WriteActionClients | None = None,
    protected_transport: bool = False,
) -> DemoAppHarness:
    """Build a fixture-mode ne-api with the demo's T3 + tier-2 governance wired.

    NE-CTXB-8: ``verified_max_result_tokens`` defaults to the production packing
    budget (8192) so the fixture composition mirrors packing-ON; the truncation
    tests pass a small cap (e.g. 1024) to prove honest-truncated directory reads.

    NE-HGATE-4: ``gateway_settings`` (when given) REPLACES the stub GatewaySettings
    so the overlay-boots test can drive the composition root's boot fail-loud check
    (platform.py:1181-1194) with the demo overlay's EXACT parsed task map; defaults
    reproduce today's stub object byte-for-byte. ``grounding_strictness`` threads the
    demo posture through to ``OntologySettings`` (default ``"strict"`` — byte-compatible
    with every existing caller).

    NE-DEMO-WRITEBACK-LIVE: ``write_action_clients`` (default ``None`` — byte-
    identical to pre-unit) additionally enables ``ApiSettings.write_actions_enabled``
    and injects the given :class:`WriteActionClients` seam, so the REAL
    ``write.internal.crm.note`` (+ ticket/draft) bindings register through the SAME
    frozen C6 executor as the inert demo tool — no connector env required (the
    seam-injection precedent, ``platform.py:649``).
    """
    from ne.api.settings import ApiSettings
    from ne.brain_graph.config import BrainGraphSettings

    reset_config_hash_for_testing()
    registry = PluginRegistry()
    verifier = FakeTokenVerifier()
    registry.register("token_verifier", "fake", lambda oidc: verifier)
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)

    # T3: the REAL 'oxigraph' triple_store entry point + the REAL boot lift populate
    # the store — NO hand-seeded store / test-local registration remains.
    ontology_settings = (
        _ontology_settings(
            tmp_path,
            verified_max_result_tokens=verified_max_result_tokens,
            grounding_strictness=grounding_strictness,
            grounding_gate_profile=grounding_gate_profile,
            grounded_agent_name=grounded_agent_name,
            grounded_assist_enabled=grounded_assist_enabled,
        )
        if verified_answers
        else None
    )

    chunk_store = InMemoryFilteredChunkStore(chunks)
    gateway = PromptRoutedGateway()

    key = uuid.uuid4().hex
    runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
    audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
    perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"

    graph_store = _demo_graph_store()
    _admit_demo_graph_authority(graph_store, runtime_url)
    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience=AUDIENCE),
            principal_claims=("email", "preferred_username", "name", "department"),
        ),
        runtime_settings=RuntimeSettings(
            session_database_url=runtime_url,
            approvals=ApprovalSettings(poll_interval_s=0.05),
        ),
        audit_settings=AuditSettings(database_url=audit_url),
        permissions_settings=PermissionsSettings(database_url=perms_url),
        gateway_settings=gateway_settings
        if gateway_settings is not None
        else GatewaySettings(
            models=[
                ModelDef(
                    name="stub",
                    litellm_model="openai/stub",
                    provider="stub",
                    lane=Lane.FRONTIER_API,
                    context_window=32768,
                )
            ]
        ),
        gateway=gateway,
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=chunk_store,
        reranker=PassthroughReranker(),
        plugin_registry=registry,
        # T2: the REAL graph tier over a governed ACL-labeled reference snapshot.
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=graph_store,
        ontology_settings=ontology_settings,
        # NE-DEMO-WRITEBACK-LIVE: seam-injected fakes — no connector env needed.
        write_action_clients=write_action_clients,
        agent_private_payload_configured=protected_transport,
    )
    protected_env = (
        {
            "NE_AGENT_PRIVATE_PAYLOAD_KEY_ID": "demo-fixture-k1",
            "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64": ("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s"),
            "NE_AGENT_TRANSPORT_GENERATION": "agent-transport-v1",
        }
        if protected_transport
        else {}
    )
    with patch.dict(os.environ, protected_env):
        app = create_app(
            ApiSettings(
                qdrant_url="http://qdrant.test",
                qdrant_collection="t",
                tei_url="http://tei.test",
                # NE-GA-G6: "grounded-analyst" is the ONE agent_name the composition
                # root's GroundingPolicy wiring keys off (OntologySettings.
                # grounded_agent_name, default unchanged); every other agent_name
                # (console-chat) is byte-unaffected.
                chat_agents=chat_agents,
                approver_roles=("approvers",),
                ops_roles=("ops",),
                # The FIVE production demo seams (default-off elsewhere).
                demo_writeback_tool=writeback_tool,
                demo_ontology_lift=verified_answers,
                demo_delegation=delegation,
                # NE-GA-G7: requires the grounded agent enabled (verified_answers=True
                # is what turns ontology_settings.grounded_agent_enabled on above) —
                # AND'd so a verified_answers=False build (the degrade-leg tests) never
                # trips build_platform's fail-loud misconfig guard.
                demo_grounded_hallucination=(verified_answers and grounded_hallucination),
                # NE-DEMO-WRITEBACK-LIVE: on ONLY when a seam client is injected —
                # byte-identical default-off otherwise (the demo_writeback_tool
                # precedent). A seam-injected write_action_clients suppresses the
                # env-settings resolution (platform.py:649), so no NE_SQL_*/
                # NE_SALESFORCE_*/NE_M365_* env is needed in CI.
                write_actions_enabled=write_action_clients is not None,
            ),
            seams=seams,
        )
    return DemoAppHarness(
        app=app,
        verifier=verifier,
        chunk_store=chunk_store,
        gateway=gateway,
        audit_url=audit_url,
        runtime_url=runtime_url,
        perms_url=perms_url,
    )
