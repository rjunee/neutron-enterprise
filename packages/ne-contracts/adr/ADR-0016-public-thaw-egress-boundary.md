# 0016 — Public `thaw` egress-boundary converter

## Status

Accepted (2026-07-29; NE-GATEWAY-FROZENDICT — a live rig extraction leg
broke wiring `response_format` through the gateway with
`TypeError: Unsupported response_format type - FrozenDict(...)`).

## Context

`ne.contracts.frozen` already contained a private recursive serializer
(`_thaw`) used by exactly one SANCTIONED call site: as the pydantic
`serialization` hook wired into `_FrozenDictAnnotation`, so
`model_dump()`/`model_dump_json()` emit plain nested `dict`/`list`
structures for a `FrozenDict`-typed field (ADR-0005 et seq.). It also had
one pre-existing PRIVATE cross-package import reaching around the module
boundary: `ne-api`'s `wire.py` already does
`from ne.contracts.frozen import _thaw` (`packages/ne-api/src/ne/api/wire.py`,
used at its egress-body-building call site) — a private name imported
across a package boundary, exactly the shape this ADR's Decision closes by
making the name public instead of leaving it a private name with an
unofficial second caller.

`CompletionRequest.params` is an `AnyMap` (`ne.contracts.frozen.AnyMap`):
pydantic coerces the field to a `FrozenDict`, which — per `_freeze_value` —
recursively freezes every NESTED mapping value into a `FrozenDict` too, not
just the top level. `ne.gateway.gateway.LiteLLMGateway._call_params` merges
`route.params` with `dict(request.params)`; the outer `dict(...)` call only
un-freezes the OUTERMOST layer — every nested mapping (e.g. a
`response_format: {"type": "json_schema", "json_schema": {...}}` param)
stayed a `FrozenDict` one layer down, and was forwarded to litellm's
`Router.acompletion` unchanged.

litellm's `type_to_response_format_param`
(`litellm/llms/base_llm/base_utils.py`, vendored 1.90.3) requires
`isinstance(response_format, dict)` and raises `TypeError` on any other
`Mapping` — a bare `FrozenDict` included, verified live against the
vendored source in this worktree's venv. The live rig reproduced this
exact failure end-to-end.

**The freeze cannot be relaxed to fix this.** It is load-bearing for two
independent reasons, both pre-dating this ADR:

1. **Hashability.** Every `ne-contracts` value object with a mapping field
   relies on `FrozenDict` to stay hashable (a plain `dict` field makes
   `hash(model)` raise `TypeError` — the defect `FrozenDict` itself was
   introduced to close).
2. **Pickle/deepcopy for the DBOS durable runtime.** `FrozenDict.__reduce__`
   is the only reason a map-bearing contract model survives
   `pickle.dumps`/`copy.deepcopy` (SPEC §4 / NE-033); un-freezing the field
   type would reopen that hole.

**And even an UNFROZEN `Mapping` would still fail the same check.**
litellm's `isinstance(x, dict)` test rejects any `Mapping` ABC
implementation that is not a literal `dict` — a hypothetical unfrozen
custom mapping type would raise exactly the same `TypeError`. Changing the
contract's TYPE therefore would not have fixed this defect; the boundary
handing values to litellm must normalize to literal builtins regardless of
what the contract-side type is. The thaw is the root fix, not a
workaround around the freeze.

## Decision

Promote the existing recursive serializer to a public export, and use it
at the gateway's egress boundary.

1. **Rename the module-level function `_thaw` to `thaw` and export it from
   `ne.contracts.__all__`.** `_thaw = thaw` remains as a compatibility
   alias (same object, `_thaw is thaw`) so the existing private import in
   `ne-api`'s `wire.py` (`from ne.contracts.frozen import _thaw`) keeps
   working unchanged. No behavior changes: the function body, and its use
   as the pydantic serializer for `_FrozenDictAnnotation`, are untouched.

2. **`ne.gateway.gateway.LiteLLMGateway._call_params` becomes `thaw`'s
   second sanctioned caller.** The merged per-call param dict is built by
   thawing each value from `{**route.params, **dict(request.params)}`,
   converting every nested `FrozenDict`/tuple/frozenset to plain
   `dict`/`list` at arbitrary depth before the params ever reach litellm.
   `request.messages` needed no change: `ChatMessage.model_dump()` already
   goes through the pydantic serializer (sanctioned caller #1), so message
   bodies were already plain builtins.

3. **MINOR version bump, 1.12.0 → 1.13.0.** Purely additive: one new public
   name, no signature or behavior change to any existing export, no field
   changes to any contract model.

## Rejected alternatives

- **Un-freeze `AnyMap`/change `CompletionRequest.params`'s runtime type.**
  Rejected on both grounds in Context: it would reopen the hashability and
  DBOS-pickling holes `FrozenDict` exists to close, AND it would not even
  fix the defect — litellm's `isinstance(x, dict)` check rejects any
  non-`dict` `Mapping`, frozen or not.
- **A gateway-local recursive walker duplicating the existing logic.**
  Rejected: `ne.contracts.frozen`'s `_thaw` already implements exactly this
  walk correctly (it is the pydantic serializer's actual implementation,
  proven correct by the existing `model_dump` test coverage). `ne-mcp`'s
  `server.py` already carries an independent, NEAR-duplicate local
  implementation of a similar walk — not byte-for-byte the same walk: it
  additionally coerces every mapping key with `str(k)` (this module's
  `thaw` does not touch keys) and it converts only `list`/`tuple` (this
  module's `thaw` also converts `set`/`frozenset`). That divergence is a
  known failure mode this ADR does not want to add a THIRD, differently-
  shaped copy of; promoting the canonical implementation to a public
  export is derive-don't-duplicate applied correctly for this unit's own
  new caller. Repointing `ne-mcp` at the public export is left to a
  follow-up (see Consequences) precisely because that repoint is not a
  drop-in swap — the `str(k)` divergence must be resolved deliberately,
  not silently, and the existing `ne-mcp` test coverage
  (`test_server_governed.py`) builds its expected value by calling the
  local `_thaw` itself, so it cannot catch a behavior change from the
  repoint on its own; the follow-up must add a non-self-referential
  assertion first.

## Consequences

- **Purely additive MINOR bump 1.12.0 → 1.13.0.** Every pre-1.13 caller of
  `ne.contracts` is unaffected; `_thaw` keeps working for `ne-api`'s
  existing private import.
- **`uv.lock` regenerates the `ne-contracts` package entry only** — via
  plain `uv lock`/`uv sync`, never hand-edited (repo shared-file policy
  §1.4).
- **The gateway's egress boundary now forwards nested request params as
  literal builtins on every lane** (`PRIVATE_VLLM` byte-identical pass
  through, `FRONTIER_API` redacted-but-not-refrozen) — proven by
  `packages/ne-gateway/tests/test_frozen_params_egress.py`'s
  router-kwargs-capture boundary tests, including a control arm proving
  litellm's converter still rejects a raw `FrozenDict` (the tripwire for a
  future litellm relaxation of the `isinstance` check).
- **Named follow-ups, OUTSIDE this unit's `mayTouch`:**
  - Repoint `ne-api`'s `wire.py` from `from ne.contracts.frozen import
    _thaw` to `from ne.contracts import thaw`.
  - Repoint `ne-mcp`'s `server.py` near-duplicate local walker at the
    public `thaw` export, deleting the local implementation. NOT a blind
    swap: the local walker coerces mapping keys with `str(k)` (`thaw` does
    not) and only converts `list`/`tuple` (`thaw` also converts
    `set`/`frozenset`) — the follow-up must decide whether `ne-mcp`'s
    string-keying behavior is load-bearing for any `ToolSpec.input_schema`
    with non-`str` keys before repointing, and must add a
    non-self-referential assertion first: the existing
    `test_server_governed.py` coverage builds its expected value by
    calling the local `_thaw` itself, so swapping the implementation moves
    both sides of that assertion together and cannot catch a behavior
    change from the repoint.
  - `ne-brain-graph`'s `response_format` wiring is now UNBLOCKED (the
    gateway boundary no longer rejects it); the absence-pin test
    (`test_build_request_omits_response_format_pending_gateway_support`)
    stays green and untouched by this unit — flipping it to actually wire
    `response_format` is a separate follow-up unit.
- **What would be BREAKING later (forbidden in this and every later
  unit):** removing the `_thaw` alias before every consumer is repointed;
  changing `thaw`'s recursion to stop at a fixed depth (the whole point of
  this promotion is arbitrary-depth normalization); re-freezing values
  `thaw` hands back.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the
unit's contract diff surface is `packages/ne-contracts/**`, matching
ADR-0015's precedent.
