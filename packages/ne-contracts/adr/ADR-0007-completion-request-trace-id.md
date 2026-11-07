# 0007 — CompletionRequest trace_id

## Status

Accepted (2026-07-15; NE-166 — Correlation spine, the first unit of the
"Routing Trace" demo).

## Context

Every `PlatformEvent`/`AuditEvent`/`CostEvent` already carries a `trace_id`
field, but nothing MINTS or THREADS a stable, explicit per-run id today:

1. **The gateway derives `trace_id` only from the live OTel span**
   (`_current_trace_id`, `ne.gateway.gateway`), which returns `None` under the
   platform's DEFAULT no-op tracer — the common case absent an installed SDK
   `TracerProvider`.
2. **The runtime emitters never populate it.** `dispatch._emit` and
   `middleware._emit` (`ne-runtime`) construct their `AuditEvent`s with no
   `trace_id` at all, so every `subagent.dispatch`/`subagent.complete`/
   `tool.execute` event is unstamped regardless of tracer state.
3. **`CompletionRequest` had no correlation field.** Even if a caller HELD a
   run-scoped id, there was no cross-process DATA carrier to get it from the
   agent-runtime process into the gateway process — the only channel a
   `ModelGateway` implementation sees is the frozen `CompletionRequest` itself.

The result: under the default no-op tracer (the common deployment case unless
an OTel SDK exporter is wired up), every event produced during one agent run
carries an independent `None`, so "which events belong to this one run" is
unanswerable from the audit/cost trail alone — defeating the correlation
purpose `trace_id` was added for.

The fix must not break the Checkpoint-A freeze of `ne-contracts`: every
existing constructor, wire producer, and contract model stays byte-for-byte
compatible, and it must not touch `config_hash` (a request model, not
platform config).

## Decision

Add a **single additive optional field** to `CompletionRequest`:

- `trace_id: str | None = None` — an OPAQUE run-scoped correlation id (shape:
  a `uuid4().hex`, same length as an OTel trace id but never derived from a
  span) carried AS DATA from the agent-runtime process into the gateway
  process. The default `None` keeps every existing constructor and every
  pre-1.4.0 wire producer valid.

The companion `ne-runtime` change (same unit) mints this id at the run root
(a `ContextVar`-backed scope, `ne.runtime.correlation`) and threads it through
`GatewayModel.request` onto `CompletionRequest.trace_id`, and through
`dispatch._emit`/`middleware._emit` onto the runtime's own `AuditEvent`s — so
a single id decorates every event of one run, independent of tracer
configuration.

On the gateway side, `LiteLLMGateway.complete`/`_stream` prefer
`request.trace_id` when present and fall back to `_current_trace_id()` (the
live OTel span) only when it is absent — so a caller that DOES run under a
real tracer and does NOT supply an explicit id keeps today's span-derived
behavior unchanged, while a caller that mints an explicit id gets correlation
even with no span active at all.

`trace_id` carries **no PII and no message content** — it is an opaque
identifier only, never derived from or containing task text, arguments, or
any user-supplied value.

### Alternatives rejected

- **Deriving trace_id exclusively from OTel, and requiring every deployment to
  wire an SDK `TracerProvider`.** Rejected: it makes correlation depend on
  optional instrumentation config rather than being a structural platform
  guarantee, and the whole point of this unit is that it holds even under the
  default no-op tracer.
- **Carrying the id only in `params` (the untyped `AnyMap`).** Rejected:
  `params` is forwarded toward litellm call kwargs (minus the one reserved
  `kpi_tag` key); an ad-hoc key there is unvalidated, easy to typo, and not
  visibly part of the frozen contract surface the way a named field is.
- **Making `trace_id` required.** Rejected: not additive — it would break
  every existing `CompletionRequest(...)` constructor and every pre-1.4.0 wire
  producer, violating the Checkpoint-A freeze discipline.

## Consequences

- **Purely additive MINOR bump 1.3.0 → 1.4.0.** No existing field is removed,
  renamed, or reordered; no existing API signature or observable behavior
  changes. `CompletionRequest` stays `frozen=True` (Checkpoint-A immutability),
  and `config_hash` is entirely untouched — no `freeze_config_hash`/
  `freeze_platform_config_hash` call is added, moved, or re-invoked for any
  per-request or per-child copy. This honors the Checkpoint-A freeze
  discipline (ADR-0001 freeze scope, SPEC §6): additive-only + ADR + version
  bump.
- **Correlation becomes a structural guarantee, not an instrumentation
  dependency.** Once `ne-runtime` threads an explicit id and the gateway
  prefers it over the span, every audit/cost event produced during one agent
  run shares one non-null `trace_id` even with zero OTel SDK configuration.
- **Span-derivation is preserved as an enrichment fallback.** A caller that
  passes no `trace_id` and runs under a real tracer still gets the span's id
  stamped exactly as before (`ne.gateway.tests.test_instrumentation`'s
  span-fallback tests are unchanged by this ADR).

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the
unit's contract diff surface is `packages/ne-contracts/**`. It is indexed from
the repo-wide `docs/adr/README.md` for discovery.
