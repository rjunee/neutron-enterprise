# 0028 — `Completion.finish_reason`

## Status

Accepted (2026-08-27; NE-FRONTIER-OUTPUT).

## Context

`Completion` carried the answer, the model that produced it, its usage and its
cost. It did not carry the provider's reason for stopping. The gateway READ
that reason — it is what distinguishes the empty-content
`ReasoningExhaustedError` case from an ordinary empty reply — and then
discarded it.

The consequence is a silent degrade, not a cosmetic gap. A completion that hit
the output budget but still returned content (`finish_reason == "length"` with
real text) is a PARTIAL answer: the model was mid-sentence when it was cut off.
It reached every consumer through a `Completion` that was byte-indistinguishable
from a whole one, and the gateway's own success audit row did not record the
reason either — so a truncated answer was not detectable at the time, and not
detectable afterwards. A cut-off answer presented as a complete one is exactly
the failure class this product's governance exists to make impossible.

## Decision

1. Add `finish_reason: str | None = None` to `Completion`. It is the provider's
   status token carried VERBATIM (`"stop"`, `"length"`, `"tool_calls"`, ...),
   or `None` when the provider reported nothing. It is a status token, never
   text: no prompt or response substring can ride it (C12).

2. It is deliberately NOT normalized to a Neutron enum. Providers agree on
   `"length"` and `"stop"` and disagree on the rest; minting an enum here would
   make the contract a place where a provider fact is quietly re-stated.

3. Add one derived reading, `Completion.truncated`, defined as
   `finish_reason == "length"` — the single question a caller must be able to
   ask ("is this the whole answer?"). `None` is NOT read as truncated: absence
   of evidence is not evidence of a cut.

4. A truncated completion is still SERVED. The partial content is real work and
   the caller keeps it; what changes is that it can no longer be mistaken for a
   complete answer. Turning truncation into a failure was rejected — see below.

5. Additive MINOR bump, 1.24.0 to 1.25.0. A pre-1.25.0 producer omits the key
   and validates with `finish_reason is None`, which reads as `truncated is
   False`.

## Rejected alternatives

**Raise a typed error, the way `ReasoningExhaustedError` does for empty
content.** That discards a real partial answer, and on a route with no declared
fallback — which is exactly the demo's `agent` route under `--frontier` — it
converts a partial answer into a hard failure. It would also burn a second full
generation on any route that does declare one, redoing work the caller already
holds.

**Append a truncation marker to `Completion.text`.** The marker would then be
inside the answer, and every structured consumer of this seam parses that text:
the runtime's tool-call parse, the eval judge's JSON parse, the graph
extractor's wall validation. The fact must ride ALONGSIDE the text, not inside
it.

**Record the reason on every audit row.** Rejected so the PRESENCE of the key is
the signal. A complete answer's success row is byte-identical to the row it
produced before this decision; only a truncated one grows a `finish_reason` key
and a `TRUNCATED` detail.
