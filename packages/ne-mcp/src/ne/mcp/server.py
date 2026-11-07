# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GovernedMcpServer — a THIN MCP protocol adapter over the C6 governed executor.

No-bypass argument (C6, THE CRUX). This adapter holds ZERO tool handlers and ZERO
tool bindings. Its ONLY route from an inbound MCP ``call_tool`` to a tool effect is
``await self._executor.execute(name, arguments, principal=self._principal)`` — the
single NE-033 governed pipeline (guardrails → PDP → HITL → audit). Consequences:

  * Every MCP-arriving call — known tool, unknown tool, well-formed or malformed
    args — reaches the audited governance path. There is no handler off this object
    to reach without the executor (a structural test asserts this module references
    no tool-binding type and invokes no bound callable directly).
  * ``@call_tool(validate_input=False)`` is LOAD-BEARING. With the SDK default
    (``True``) the lowlevel server jsonschema-rejects malformed arguments BEFORE our
    governance runs — recreating the exact *unaudited pre-governance rejection* class
    that bounced NE-033 (the pydantic-ai ToolManager pre-validation). With ``False``
    the SDK forwards every request straight to our handler, so even a malformed or
    unknown call gets a full guardrails→PDP→audit record.
  * An MCP-invoked Tier-3 action STILL blocks on a named human approval (the executor
    routes Tier ≥ 2 through the HITL gate); a rejected approval surfaces as an MCP
    ``isError`` result with ZERO handler invocation.
  * The principal is established at CONSTRUCTION from the authenticated session (one
    adapter instance per authenticated caller) and passed keyword-only into every
    ``execute`` — a client-supplied argument can NEVER become the principal, so there
    is no anonymous tool execution over MCP.

Error surface. The enumerated ordinary governance/handler errors (``_GOVERNED_ERRORS``:
PDP deny, unknown tool, guardrail *violation*, HITL rejection, handler error) are caught
and rendered as ``isError`` CallToolResults whose text is a redaction-disciplined
``"{ErrorClass}: {msg}"`` (the ne-runtime / ne-govern messages already carry only tool
names, tiers, statuses, and error classes — never raw values).

Fail-closed governance-INFRA errors are a DIFFERENT contract. The mcp 1.x lowlevel
server wraps our ``call_tool`` handler in ``except Exception: return
_make_error_result(str(e))`` (mcp/server/lowlevel/server.py:589-590), so any exception we
let propagate is converted AT THE SDK SEAM into an ``isError`` result carrying the RAW
internal exception string — a C12 status-only violation that also silently downgrades the
documented withhold semantics. So the infra classes
(``RuntimeAuditError`` / ``AuditEmissionError`` / ``GuardrailEvaluationError`` /
``PolicyBackendError``) are caught IN OUR HANDLER, before the SDK can, and rendered as
STATUS-ONLY sanitized ``isError`` results with stable ``NE_MCP_*`` codes (never
``str(exc)``). The result is still WITHHELD and the tool still never executes. Observability
is intact: guardrail/PDP infra failures are already audited (``guardrail.error`` /
``pdp.decide`` error) by the engine/PDP before they raise, and an audit-sink failure — which
cannot itself be audited to a down sink — is surfaced by the DISTINCT
``NE_MCP_AUDIT_UNAVAILABLE`` code (the not-silently-lost signal). A final defense-in-depth
``except Exception`` guard keeps ANY unexpected error from crossing the seam as ``str(e)``;
it logs the raw exception SERVER-SIDE (stdlib ``logging`` on logger ``ne.mcp.server``, with
traceback) for diagnosability while the client still sees only the sanitized code — the raw
detail never crosses the seam. ``BaseException`` (cancellation) is deliberately NOT caught.

Catalog safety (invariant-audit Finding D, 2026-07-23). The SDK's own ``call_tool``
path refreshes its tool cache by re-invoking the list-tools handler BEFORE our
sanitized ``_call_tool`` try/except chain runs at all, and it does so INSIDE the SDK's
own ``str(e)`` catch-all (the same seam described above) — so a raising list-tools
handler would leak a raw exception string, bypassing the ``NE_MCP_*`` sanitization
contract entirely, with no defense-in-depth guard able to intervene. So the advertised
catalog is computed ONCE, at construction: both admissible executors freeze their
catalog before ``GovernedMcpServer.__init__`` returns (``GovernedToolExecutor``
builds its bindings once and exposes no mutator; ``ToolsetScopedExecutor``'s granted
set is a frozenset), and the list-tools handler merely returns a copy of that frozen
snapshot — it cannot raise. A bad spec now fails LOUDLY at construction, before any
transport exists, instead of leaking at the SDK seam. Pinned by
``test_list_tools_catalog_is_stable_and_matches_specs_exactly`` (no-behavior-change)
and ``test_tool_cache_refresh_path_cannot_leak_raw_exception`` (the Finding D
regression pin).

Write-back idempotency scope (NE-ACTION-DETERMINISTIC-REQID, NE-ACTION-SCOPE-WIRING
§3.2). Every ``call_tool`` runs under a TWO-LEVEL ``action_scope`` contract, so an
NE-050 governed write action derives a stable dedup key when the caller supplies no
explicit ``request_id`` (the model-facing schemas no longer advertise the field):

  * LEVEL 1 (always-on fallback). Every ``call_tool`` dispatched within ONE
    ``GovernedMcpServer`` instance runs under one opaque per-connection scope
    (``mcp:<uuid4>``, minted once at construction). With no ``_meta`` turn id this is
    BYTE-IDENTICAL to the pre-U2 contract: same-payload derived writes dedup within
    one connection and never across instances (pinned by
    ``test_demo_server_composition.py::test_action_scope_dedups_within_instance_not_across_instances``
    — UNMODIFIED — and
    ``test_server_governed.py::test_no_meta_scope_is_the_connection_scope``). A new
    ``GovernedMcpServer`` instance — a new session — always gets a fresh scope.
  * LEVEL 2 (opt-in, per-turn). A client that attaches ``TURN_META_KEY``
    (``"example.host/turn-id"``) to a call's ``_meta``, with a value matching
    ``^[A-Za-z0-9._:-]{1,128}$``, gets the STRICTLY TIGHTER scope
    ``{connection}:turn:{token}`` (pinned by
    ``test_valid_turn_token_composes_connection_prefixed_scope`` and
    ``test_meta_turn_ids_subdivide_the_connection_scope``). The connection prefix
    means a client can only subdivide its OWN connection's dedup space — the same
    token supplied on two different connections never collides (pinned by
    ``test_same_turn_token_across_connections_does_not_collide``). An absent or
    invalid token falls back to Level 1 with the call still fully governed, and an
    invalid token logs a server-side warning WITHOUT the raw value (pinned by
    ``test_invalid_turn_token_falls_back_and_is_still_governed`` and
    ``test_invalid_meta_token_falls_back_to_connection_scope``).

``progressToken`` is deliberately NOT repurposed as a turn signal — it is a
DECLARED ``Meta`` field with per-request progress-reporting semantics, the wrong
granularity for a turn boundary.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from typing import Any

from ne.contracts.types import Principal
from ne.govern.errors import (
    AuditEmissionError,
    GuardrailEvaluationError,
    GuardrailViolation,
    PolicyBackendError,
)
from ne.mcp import _sdk
from ne.runtime import GovernedToolExecutor, ToolsetScopedExecutor, action_scope
from ne.runtime.errors import (
    ActionValidationError,
    ApprovalIntegrityError,
    ApprovalNotGrantedError,
    RuntimeAuditError,
    ToolDeniedError,
    ToolExecutionError,
    ToolNotRegisteredError,
    ToolsetViolationError,
)

_logger = logging.getLogger(__name__)

#: The governance/handler errors rendered as MCP ``isError`` results (redaction-safe
#: messages). Audit/guardrail-engine faults are deliberately EXCLUDED — they propagate.
_GOVERNED_ERRORS: tuple[type[Exception], ...] = (
    ToolNotRegisteredError,
    ToolDeniedError,
    ApprovalNotGrantedError,
    ApprovalIntegrityError,
    ToolExecutionError,
    ToolsetViolationError,
    GuardrailViolation,
    # NE-ACTION-VALIDATE-BEFORE-GATE: a pre-gate action-input refusal is an ordinary
    # governed refusal rendered "{ErrorClass}: {msg}" (field names only, C12) — NOT
    # the sanitized NE_MCP_EXECUTION_UNAVAILABLE infra code.
    ActionValidationError,
)

# --- fail-closed governance-infra errors (the SDK-seam boundary) ----------
#
# The mcp 1.x lowlevel server wraps our call_tool handler in
# ``except Exception: return _make_error_result(str(e))`` (verified at
# mcp/server/lowlevel/server.py:589-590). Any exception we let propagate is therefore
# stringified into a CLIENT-VISIBLE error at that seam — leaking the raw internal
# message (C12 status-only violation) and silently downgrading the withhold semantics.
# So the fail-closed governance-infra classes are caught IN OUR HANDLER, before the SDK
# can, and rendered as STATUS-ONLY sanitized results with stable NE_MCP_* codes. Audit
# faults from either seam (ne-runtime RuntimeAuditError / ne-govern AuditEmissionError)
# share the same DISTINCT audit-down code.
_AUDIT_INFRA_ERRORS: tuple[type[Exception], ...] = (RuntimeAuditError, AuditEmissionError)

#: Sanitized, status-only surfaces (status phrase + stable code ONLY — never str(exc),
#: exc args, tool/resource/action names, paths, endpoints, or config detail).
_SANITIZED_AUDIT_UNAVAILABLE = (
    "governed execution unavailable: NE_MCP_AUDIT_UNAVAILABLE (result withheld, fail closed)"
)
_SANITIZED_GUARDRAIL_UNAVAILABLE = (
    "governed execution unavailable: NE_MCP_GUARDRAIL_UNAVAILABLE (result withheld, fail closed)"
)
_SANITIZED_POLICY_UNAVAILABLE = (
    "governed execution unavailable: NE_MCP_POLICY_UNAVAILABLE (result withheld, fail closed)"
)
_SANITIZED_INTERNAL = (
    "governed execution unavailable: NE_MCP_EXECUTION_UNAVAILABLE (result withheld, fail closed)"
)

#: The ``_meta`` key a turn-signaling client may attach to ``call_tool`` requests to
#: opt into per-turn (rather than per-connection) action-idempotency scoping
#: (NE-ACTION-SCOPE-WIRING §3.2). MCP reserves un-prefixed ``_meta`` keys for the
#: protocol itself; custom keys must take a domain prefix — ours is ``example.host``.
TURN_META_KEY = "example.host/turn-id"

#: Opaque, charset-bounded turn tokens only (correlation.py PII discipline) — never
#: model- or task-derived text.
_TURN_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _error_result(text: str) -> _sdk.types.CallToolResult:
    """An ``isError`` CallToolResult carrying exactly ``text`` (single text block)."""
    return _sdk.types.CallToolResult(
        isError=True,
        content=[_sdk.types.TextContent(type="text", text=text)],
    )


def _thaw(value: Any) -> Any:
    """Recursively convert a FrozenDict/Mapping tree into plain dict/list JSON.

    ``ToolSpec.input_schema`` is a recursively-frozen ``FrozenDict``; the MCP SDK
    expects a mutable JSON-shaped ``dict`` for ``Tool.inputSchema``. This copies the
    tree into plain ``dict`` / ``list`` primitives (no frozen values leak into the
    protocol payload).
    """
    if isinstance(value, Mapping):
        return {str(k): _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    return value


class GovernedMcpServer:
    """A thin, non-bypassable MCP server over the ONE governed executor (C6)."""

    def __init__(
        self,
        executor: GovernedToolExecutor | ToolsetScopedExecutor,
        *,
        principal: Principal,
        server_name: str = "neutron-enterprise",
    ) -> None:
        if executor is None:
            raise TypeError("GovernedMcpServer requires a non-None governed executor (C6)")
        if principal is None:
            raise TypeError(
                "GovernedMcpServer requires a non-None principal — no anonymous tool "
                "execution over MCP (C6)"
            )
        self._executor = executor
        self._principal = principal
        # The ALWAYS-ON action-idempotency scope minted per server instance
        # (NE-ACTION-DETERMINISTIC-REQID) — one governed CONNECTION/session is the
        # fallback unit-of-work (Level 1 of the two-level contract; see the module
        # docstring). A turn-signaling client subdivides this per-connection scope via
        # `TURN_META_KEY` on `call_tool` `_meta` (Level 2) — see
        # `_client_turn_token`/`_compose_action_scope` and the module docstring for the
        # full contract and its pinning tests. The token here stays a bare random id,
        # NEVER model- or task-derived (the correlation.py PII discipline); a fresh
        # instance gets a fresh scope, so there is no cross-session over-dedup.
        self._session_scope = f"mcp:{uuid.uuid4()}"
        self._server: _sdk.Server[Any, Any] = _sdk.Server(server_name)
        self._register()

    @property
    def server(self) -> _sdk.Server[Any, Any]:
        """The underlying lowlevel MCP :class:`Server` (for wiring a transport)."""
        return self._server

    def _client_turn_token(self) -> str | None:
        """The validated ``TURN_META_KEY`` value from the inbound request's ``_meta``.

        Returns ``None`` (never raises) when: there is no ambient MCP request context
        (``LookupError`` — e.g. called outside a request), no ``_meta`` was sent, the
        key is absent, or the value fails the opaque-token shape check
        (``_TURN_TOKEN_RE``). An invalid value logs a WARNING that names the key, the
        value's type, and its length ONLY — the raw client-controlled value is NEVER
        logged (correlation.py PII discipline).
        """
        try:
            ctx = self._server.request_context
        except LookupError:
            return None  # outside any inbound MCP request context
        meta = ctx.meta
        if meta is None:
            return None
        raw = (meta.model_extra or {}).get(TURN_META_KEY)
        if raw is None:
            return None
        if not isinstance(raw, str) or _TURN_TOKEN_RE.fullmatch(raw) is None:
            _logger.warning(
                "ignoring invalid %s _meta turn token (type=%s, len=%s); falling back "
                "to the per-connection action scope",
                TURN_META_KEY,
                type(raw).__name__,
                len(raw) if isinstance(raw, str) else "n/a",
            )
            return None
        return raw

    def _compose_action_scope(self) -> str:
        """The two-level action scope for the current call (module docstring §L1/L2)."""
        token = self._client_turn_token()
        if token is None:
            return self._session_scope
        return f"{self._session_scope}:turn:{token}"

    def _register(self) -> None:
        server = self._server

        # Catalog safety (invariant-audit Finding D, 2026-07-23; see module docstring).
        # The mcp SDK's call_tool path (lowlevel server.py: `_get_cached_tool_definition`
        # at line 531, running even with validate_input=False) re-invokes the list-tools
        # handler on a tool-name cache miss (lines 487-489) — INSIDE the SDK's own
        # `except Exception: return _make_error_result(str(e))` catch-all (lines 589-590),
        # entirely BEFORE our sanitized `_call_tool` try/except chain runs. A raising
        # list-tools handler would therefore leak a raw exception string at that seam. So
        # the catalog is built ONCE here, eagerly, at construction time — never inside the
        # handler — and the handler body becomes raise-free. This is safe because
        # tool_specs cannot change after construction: GovernedToolExecutor builds its
        # bindings once in __init__ with no mutator anywhere in this repo, and
        # ToolsetScopedExecutor's granted set is a frozenset. A bad spec now fails LOUDLY
        # at construction (no transport exists yet) instead of leaking at the SDK seam.
        # Pinned by test_list_tools_catalog_is_stable_and_matches_specs_exactly
        # (no-behavior-change) and test_tool_cache_refresh_path_cannot_leak_raw_exception
        # (the Finding D regression pin).
        catalog: tuple[_sdk.types.Tool, ...] = tuple(
            _sdk.types.Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=_thaw(spec.input_schema) or {"type": "object"},
            )
            for spec in self._executor.tool_specs
        )

        @server.list_tools()
        async def _list_tools() -> list[_sdk.types.Tool]:
            # SPECS ONLY — this adapter holds zero tool bindings. Returns a fresh copy of
            # the construction-time catalog above; it is raise-free by construction.
            return list(catalog)

        @server.call_tool(validate_input=False)  # validate_input=False is LOAD-BEARING (C6)
        async def _call_tool(name: str, arguments: dict[str, Any]) -> _sdk.types.CallToolResult:
            # The except chain is ORDERED (readability; the classes are disjoint):
            # (1) ordinary governed errors keep the informative surface; (2-4) fail-closed
            # INFRA classes are code-sanitized before the SDK can stringify them; (5) a
            # defense-in-depth blanket guard ensures NOTHING reaches the SDK's str(e).
            try:
                # Ambient scope gives ActionTools a derived dedup identity when the
                # (deliberately no-longer-advertised) request_id is absent. Extraction
                # runs INSIDE this try so an unexpected extraction error still hits the
                # defense-in-depth blanket guard below, never the SDK's str(e) seam.
                scope = self._compose_action_scope()
                with action_scope(scope):
                    result = await self._executor.execute(
                        name, arguments or {}, principal=self._principal
                    )
            except _GOVERNED_ERRORS as exc:
                # (1) UNCHANGED surface: PDP deny, unknown tool, guardrail violation, HITL
                # rejection, handler error — redaction-safe "{ErrorClass}: {msg}".
                return _error_result(f"{type(exc).__name__}: {exc}")
            except _AUDIT_INFRA_ERRORS:
                # (2) The audit sink itself failed, so NO audit re-attempt is made (it would
                # fail again); the DISTINCT code is the not-silently-lost signal.
                return _error_result(_SANITIZED_AUDIT_UNAVAILABLE)
            except GuardrailEvaluationError:
                # (3) The engine already audited a `guardrail.error` event before raising,
                # so the audit-of-failure channel is intact.
                return _error_result(_SANITIZED_GUARDRAIL_UNAVAILABLE)
            except PolicyBackendError:
                # (4) GovernedPdp already audited the `pdp.decide` error event before
                # re-raising, so the audit-of-failure channel is intact.
                return _error_result(_SANITIZED_POLICY_UNAVAILABLE)
            except Exception:
                # (5) Defense-in-depth: NOTHING may reach the SDK's `str(e)` catch-all.
                # Do NOT catch BaseException — cancellation must propagate. The raw
                # exception (with traceback) is logged SERVER-SIDE for diagnosability; it
                # never crosses to the client, which only ever sees the sanitized code.
                _logger.exception(
                    "unexpected error in governed MCP call_tool; returning sanitized "
                    "NE_MCP_EXECUTION_UNAVAILABLE (result withheld, fail closed)"
                )
                return _error_result(_SANITIZED_INTERNAL)
            return _sdk.types.CallToolResult(
                isError=False,
                content=[_sdk.types.TextContent(type="text", text=result)],
            )

    async def run_stdio(self) -> None:
        """Serve this governed adapter over stdio to a real external MCP client.

        Production entry point (LIVE-PARKABLE; CI exercises the same handlers over the
        in-memory transport). Every ``call_tool`` still routes through the governed
        executor — the transport is orthogonal to the C6 pipeline.
        """
        async with _sdk.stdio_server() as (read, write):
            await self._server.run(read, write, self._server.create_initialization_options())
