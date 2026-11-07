# 0009 — VerifiedProvenance + AgentRunResult.verified (structural verified-answer provenance)

## Status

Accepted (2026-07-16; NE-VAF-U0 — verified-answer-first Layer 0).

## Context

The chat `done` frame carries the model's raw generated text as `output`, and
the demo's verification check today is a **substring test over that text**
(`"template_id" in output`, `ne.demo.arc`). No text-based check over model
output can ever be sound, because a hijacked or merely eager model has two
independent ways to satisfy it without the answer actually having come from a
verified retrieval path:

1. **Direct forgery.** The model simply emits the literal string
   `"template_id"` (or a hand-built JSON blob that looks like a
   `VerifiedAnswer`) in its free-text response. Nothing structural stops a
   model from producing text that merely resembles a verified artifact.
2. **Replay forgery.** A real `VerifiedAnswer` JSON block that appears
   *anywhere* in the conversation history (e.g. a `ToolReturnPart` from an
   earlier, unrelated verified call) can be copied verbatim into the model's
   current free-text output — the substring check cannot distinguish "this
   run's own verified retrieval" from "text copied from history."

The only sound fix is a channel the model's own generated text has **no path
into at all** — a field that can only be populated by code, from a parsed,
structural artifact, never from anything the model writes. That is precisely
what a router-constructed contract object gives us: the model cannot assign to
a field it never sees a constructor for.

This unit (U0) delivers ONLY the contract shape for that channel. Populating
it (parsing the `VerifiedAnswer` artifact and constructing `VerifiedProvenance`
inside a deterministic `VerifiedRouter`) is U3's job; asserting on it in the
demo (replacing the substring check with a structural presence/shape check) is
U5's job. U0 must not touch any router, `sse.py`, `workflows.py`, `agent.py`,
or `arc.py` — it is contracts-only.

As with the `trace_id` (ADR-0007) and `route_lane_hint` (ADR-0008) precedents,
this must not break the Checkpoint-A freeze of `ne-contracts`: every existing
constructor, wire producer, and contract model stays byte-for-byte compatible,
and it must not touch `config_hash` — `VerifiedProvenance` is per-request data
(the outcome of one turn's retrieval), not platform configuration.

## Decision

Add a new **frozen, `extra='forbid'`** sub-model to `ne.contracts.runtime`:

```python
class VerifiedProvenance(_Frozen):
    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    template_version: int
    config_hash: str
    row_count: int
    truncated: bool
```

`template_version` is `int` to match the field's actual type in the source
`VerifiedAnswer` artifact (`ne.brain_ontology.verified.answers`, `.tools`) that
U3 will construct this contract from — Pydantic v2 does not coerce `int` to
`str` in lax mode, so a `str` field here would make U3's construction call
fail validation on day one.

All five fields are **required** (no defaults): provenance is all-or-nothing
— a `VerifiedProvenance` either fully describes a verified answer's origin or
does not exist at all (`None` on the carrier). Optionality lives entirely on
the carrier fields, not on the provenance object itself. `extra='forbid'` is
deliberate hardening beyond the other frozen models in this file: a
provenance carrier should reject any smuggled key rather than silently accept
one.

Add a single **additive optional** field to `AgentRunResult`:

- `verified: VerifiedProvenance | None = None`

And, in `ne-api`'s `turn_wire.py` (the transport-independent frame shared by
`ne-api` and `ne-worker`), the matching additive fields:

- `TurnFrame.verified: VerifiedProvenance | None = None` (appended after the
  NE-137 `audit_seq` field, same additive-tail discipline).
- `done_frame(seq, session_id, output, *, verified: VerifiedProvenance | None
  = None)` — **keyword-only**, so the sole production call site
  (`ne-worker/src/ne/worker/workflows.py:82`, positional) is untouched.

`VerifiedProvenance` is constructed **only** by the deterministic
`VerifiedRouter` (U3) from a parsed `VerifiedAnswer` artifact. No code path
anywhere lets model-generated text populate it — the model has no
constructor call it can make; it can only produce text that a downstream,
non-model-judgment parser either does or does not recognize as a verified
artifact. That parse-and-construct step, not this contract, is what actually
closes H1; this unit only ensures the channel exists and cannot be widened by
accident.

### Alternatives rejected

- **Structured-text convention inside `output` (e.g. a fenced JSON block with
  a required key).** Rejected outright: this is exactly the replay-forgery
  vector in Context #2 — any text convention over `output` is, by
  construction, something the model can reproduce verbatim from prior
  context. The guarantee must live off the text channel entirely.
- **A separate top-level frame type (e.g. a new `verified` `FrameType`)
  instead of a field on `done`.** Rejected for U0: SPEC §2.3 payload shapes
  and a new frame type are router/sse/worker wiring decisions (U3/U4/U5
  scope); a trailing-default field is the minimal additive contract change
  that keeps this unit contracts-only and does not force any wiring decision
  prematurely.
- **Defaulted/optional fields on `VerifiedProvenance` itself.** Rejected:
  provenance is meaningful only as a complete artifact description; partial
  provenance is not a lesser guarantee, it is a bug. All-or-nothing lives on
  the `T | None` carrier, matching the `_Frozen` config precedent throughout
  this file (e.g. `MemoryEvent`, `SubAgentResult`) where the *carrier*, not
  the *model*, is where optionality belongs.

## Consequences

- **Purely additive MINOR bump 1.5.0 → 1.6.0.** No existing field is removed,
  renamed, or reordered; no existing API signature or observable behavior
  changes. `AgentRunResult` and `TurnFrame` stay compatible with every
  pre-1.6.0 constructor call, proven by the full pre-existing test suite
  passing unmodified. `config_hash` is entirely untouched — `VerifiedProvenance`
  is per-request data, not platform configuration (ADR-0007/ADR-0008
  precedent).
- **Byte-identical wire when `verified` is absent.** Every `exclude_none`
  serialization of a `done` frame or `AgentRunResult` produced without a
  `verified` value is byte-for-byte identical to the pre-1.6.0 output; this is
  pinned by tests against the exact pre-change literal captured at HEAD.
  `TurnFrame.sse_payload()` (the browser-facing encoder) is untouched in this
  unit — the `done` payload stays exactly `{type, session_id, output}` until
  U4 deliberately surfaces `verified` there.
- **Mixed-version wire hazard, same posture as NE-137.** After this change, a
  new-code frame's `model_dump(mode="json")` carries `"verified": null`,
  which an OLD-code `TurnFrame` (`extra="forbid"`) would reject if read by a
  pre-1.6.0 process. Both `ne-api` and `ne-worker` deploy from one
  repo/image, so this is an accepted, already-precedented posture, not a new
  risk class.
- **U3/U4/U5 own everything downstream of the contract.** U3 constructs
  `VerifiedProvenance` inside the deterministic `VerifiedRouter`; U4 wires it
  through `sse.py`/`workflows.py` onto the emitted frame and (deliberately)
  flips the `sse_payload()` pin once `verified` is meant to reach the
  browser; U5 replaces the demo's forgeable `"template_id" in output`
  substring check with a structural assertion against
  `AgentRunResult.verified`/`TurnFrame.verified`. None of that is in scope
  here.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the
unit's contract diff surface is `packages/ne-contracts/**`. It is indexed from
the repo-wide `docs/adr/README.md` for discovery.
