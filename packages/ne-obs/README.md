# ne-obs

Observability, signals, and KPI instrumentation. In this unit (NE-005) it ships
the **single GenAI OpenTelemetry semconv wrapper**.

## GenAI semconv helper (`ne.obs.genai`)

`ne.obs.genai` is the **one and only** place the platform touches
`opentelemetry.semconv`. No other module may import it directly.

- `genai_attributes(*, operation, provider=None, request_model=None,
  response_model=None, input_tokens=None, output_tokens=None, lane=None,
  cost_usd=None, kpi_tag=None, config_hash=None) -> dict[str, str|int|float]` —
  builds the attribute dict, omitting `None` entries. `config_hash` defaults to
  the frozen process hash (`ne.contracts.get_config_hash()`); stamping is
  non-optional, so `ConfigHashNotFrozenError` propagates rather than an unstamped
  span being emitted.
- `genai_span(tracer, name, **same kwargs)` — a context manager opening a
  `CLIENT` span with those attributes; on an exception it records the exception,
  sets status `ERROR`, and re-raises.

The wrapper owns its **own** stable attribute literals (`ATTR_OPERATION_NAME`,
`ATTR_PROVIDER_NAME`, …) plus platform extras (`ATTR_NE_CONFIG_HASH`,
`ATTR_NE_KPI_TAG`, `ATTR_NE_LANE`, `ATTR_NE_COST_USD`). It uses
`gen_ai.provider.name`, **not** the deprecated `gen_ai.system`.

## Outbox → OTel-logs exporter (`ne.obs.otlp`)

The real day-one SIEM export (P2-21). `ne-worker`'s outbox drain uses this seam to
ship audit/cost/approval events to the **in-cluster** `otel-collector`, which the
customer forwards to their SIEM — the platform itself never egresses (C2).

- `build_otlp_log_record_exporter(endpoint) -> LogRecordExporter` — runs
  `ne.net.validate_in_vpc_endpoint` FIRST (accepts a dotless service name /
  private IP / `.internal` host; **rejects any external SIEM host** so a
  misconfigured worker fails BOOT loud), then returns an OTLP/HTTP
  `OTLPLogExporter(endpoint=f'{validated}/v1/logs')`.
- `OutboxOtelLogHandler(exporter, *, service_name='ne-worker')` — a `drain_once`
  handler that maps each `OutboxRecord` to one OTel `LogRecord` (body = the event
  payload; the outbox coordinates ride as `ne.event_type` / `ne.event_id` /
  `ne.actor` / `ne.config_hash` / `ne.outbox_id` / `ne.kpi_tag` attributes) and
  exports the batch **synchronously**. **No** `LoggerProvider` /
  `BatchLogRecordProcessor` — async batching would let the drain ack a window
  before delivery, breaking the outbox at-least-once contract; a non-SUCCESS
  export raises `OutboxExportError`, leaving the cursor unmoved for redelivery.

## Pin policy — pinned AND wrapped (SPEC §10)

OpenTelemetry's GenAI semantic conventions are **still in development**: they were
moved out of the main `semantic-conventions` repo into the dedicated
`open-telemetry/semantic-conventions-genai` repo, which has **no releases yet**.
The `gen_ai.*` constants still ship as *incubating* attributes in
`opentelemetry-semantic-conventions`, so we:

1. **exact-pin** `opentelemetry-semantic-conventions==0.64b0` (the lockstep pair
   of `opentelemetry-sdk` 1.43.0, which itself pins that semconv), and
2. **wrap** it behind this one helper with our own literals, and
3. gate drift with `tests/test_semconv_pin.py`, which cross-checks our literals
   against the pinned package's `gen_ai` constants — a future rename fails loudly
   here instead of silently changing our spans.

See `NOTICE` for the OpenTelemetry dependency licenses (all Apache-2.0).

## U-P-B KPI reporting (NE-068)

`ne.obs.kpi`/`ne.obs.collect`/`ne.obs.report` implement the Utilization /
Proficiency / Business-value monthly KPI report: a deterministic generator
that turns two collected `WindowMetrics` (a baseline month and a current
month) into a `MonthlyUpbReport` — typed baseline->current deltas per named
KPI, named KPI leads, and a conservative hours-saved FLOOR — plus deterministic
markdown/JSON renderers (no charting dependency).

### Read-only data seams

The reporting layer is a **read-only consumer** of three already-merged
packages' public APIs. It never calls a write/append surface on any of them:

- **Usage** — `ne.audit.replay(engine, start=..., end=...)` over the
  tamper-evident, payload-authoritative audit chain (ne-audit, NE-005+).
- **Quality (evals)** — `ne.eval.store.EvalRunStore.list_runs(limit=...)`
  (ne-eval, NE-024).
- **Quality (correction signals)** —
  `ne.flywheel.SqlAlchemyCorrectionSignalStore.read_signals(after_id=..., limit=..., since=..., until=...)`,
  the NE-064 cursor-paginated read API, looped to exhaustion.

### Canonical KPI registry (C8: business KPIs lead)

In report order (`ne.obs.report.KPI_DEFS`) — the hours-saved-floor metric is
deliberately **last**, a supporting metric only, per SPEC C8:

| KPI | unit | direction |
| --- | --- | --- |
| `utilization.agent_turns` | turns | higher_is_better |
| `utilization.active_users` | users | higher_is_better |
| `utilization.tool_calls` | calls | higher_is_better |
| `proficiency.eval_pass_rate` | ratio | higher_is_better |
| `proficiency.negative_signal_rate` | signals/turn | lower_is_better |
| `business_value.hours_saved_floor` | hours (floor) | higher_is_better |

### Hours-saved floor methodology

The exact, verbatim text embedded in every report (`report.methodology`,
`ne.obs.report.METHODOLOGY`):

> Hours-saved floor methodology (conservative, defensible): (1) count only
> SUCCESSFUL assisted agent turns in the window (audit outcome=success on the
> configured turn actions); (2) subtract every negative correction signal in
> the window (thumbs-down, wrong-source, re-query, approval-rejection)
> one-for-one, floored at zero — a turn a user pushed back on claims zero
> savings; (3) multiply by a per-turn minutes-saved assumption (default 2.0
> minutes) and a conservatism discount (default 0.5); (4) divide by 60 and
> round to 2 decimals. Every assumption is chosen to UNDER-claim: this is a
> floor, not an estimate. Per SPEC C8, hours-saved is a supporting metric
> only; the named business KPIs lead the report.

Both defaults are configurable **per engagement** via
`NE_OBS_MINUTES_SAVED_PER_TURN` / `NE_OBS_FLOOR_DISCOUNT` (or the equivalent
`ne.toml` keys) — but any change must keep the floor defensible: negative
signals only ever reduce it, and neither knob may be raised to inflate the
claim beyond a conservative estimate.

### KPI-leads configuration

Named leads are config, parsed via `ne.obs.config.leads_from_settings`. Each
value is a `"Name <email>"` string keyed by the KPI name it owns:

```toml
# ne.toml
[kpi_leads]
"business_value.hours_saved_floor" = "Ada Lovelace <ada@acme.com>"
"utilization.agent_turns" = "Grace Hopper <grace@acme.com>"
```

or via the environment (`NE_OBS_KPI_LEADS` as a JSON object with the same
shape). A malformed `"Name <email>"` value raises `ObsReportError` naming the
offending KPI key — fail loud, never silently dropped.

### Determinism contract

`generate_monthly_report`/`render_markdown`/`render_json` are pure functions
of their inputs: the same two `WindowMetrics` (+ leads) always produce a
byte-identical report and markdown. No wall clock, no randomness, no
set/dict iteration order anywhere in the output — every report iterates
tuples in a fixed, canonical order.

### Known gaps (scaffolded, not patched upstream)

1. **No first-class session/turn events yet.** Agent turns are proxied by
   successful gateway-completion audit actions (`ne.gateway.complete` /
   `ne.gateway.stream`); sessions are counted only from provenance
   `session_id` when an emitter stamps it, else the window's `sessions` field
   is `None` and the report's `gaps` name the instrumentation gap. True
   turn/session instrumentation is upstream-emitter work, out of scope here.
2. **`ne-eval` `list_runs` has no time-window filter.** The window is applied
   in-memory over the newest `EVAL_SCAN_LIMIT` runs — fine at pilot scale; an
   upstream filter is future ne-eval work, not patched here.
3. **`ne.audit.replay` loads the full window into memory.** Acceptable for
   monthly pilot volumes; noted for scale, not "fixed" by adding methods to
   ne-audit.
