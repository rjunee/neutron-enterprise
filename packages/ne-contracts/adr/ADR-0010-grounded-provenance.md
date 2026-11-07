# 0010 — GroundedProvenance + AgentRunResult.grounded (structural grounded-answer provenance)

## Status

Accepted (2026-07-16; NE-GA-G0).

## Context

The grounded-agent tier (docs/architecture/anti-hallucination-grounding.md) layers a
deterministic grounding gate and an optional one-pass model assist on top of
the existing verified-answer-first path. Layer 0 (G0) is the contracts-only
foundation for that gate's output: a structural record of *why* an answer
counts as grounded — which verified fetches backed it, how many citations it
carries, which gate version ran, whether the deterministic fallback fired,
whether the model assist ran, and how many tokens were grounded only by
prompt echo.

Exactly as ADR-0009 established for `VerifiedProvenance`, the only sound
channel for this record is one the model's own generated text has **no path
into at all** — a field populated solely by code, from the deterministic
grounding gate's own bookkeeping, never from anything the model writes. A
text-based convention (a fenced block, a magic substring) is forgeable by
construction: a hijacked or merely eager model can reproduce any text
pattern it has seen, whether by direct fabrication or by copying a real
artifact out of prior conversation history. Structural, router/gate-
constructed contracts are not.

This unit (G0) delivers ONLY the contract shape for that channel. Populating
it (the grounding gate's own logic, G1; the deterministic fallback and
runtime post-step, G3; the one-pass model assist, G4) and wiring it through
`sse.py`/`workflows.py` end to end (G5) are later units' jobs. G0 must not
touch any gate, router, `agent.py`, `sse.py`, or `workflows.py` — it is
contracts-only, an exact sibling of the merged NE-VAF-U0.

As with `trace_id` (ADR-0007), `route_lane_hint` (ADR-0008), and
`VerifiedProvenance` (ADR-0009), this must not break the Checkpoint-A freeze
of `ne-contracts`: every existing constructor, wire producer, and contract
model stays byte-for-byte compatible, and it must not touch `config_hash` —
`GroundedProvenance` is per-request data (the outcome of one turn's
grounding gate), not platform configuration.

## Decision

Add a new **frozen, `extra='forbid'`** sub-model to `ne.contracts.runtime`:

```python
class GroundedProvenance(_Frozen):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fetches: tuple[VerifiedProvenance, ...]
    citation_count: int
    gate: str
    fallback: bool
    assist_used: bool
    prompt_echo_count: int
```

`fetches` embeds the already-defined `VerifiedProvenance` (ADR-0009) rather
than duplicating its shape — the grounding gate's per-fetch provenance IS a
verified-fetch provenance, recorded once per verified retrieval consulted
this turn. `citation_count` is the number of citations present in the final
answer; `gate` is the gate version string (bound by G1's `GATE_VERSION`, not
this unit); `fallback` records whether the deterministic verified-table
fallback fired; `assist_used` records whether the one-pass model assist ran;
`prompt_echo_count` counts tokens grounded only by prompt echo (no verified
fetch or assist backing them).

All six fields are **required** (no defaults): provenance is all-or-nothing
— a `GroundedProvenance` either fully describes a grounded answer's gate
outcome or does not exist at all (`None` on the carrier). Optionality lives
entirely on the carrier fields, not on the provenance object itself.
`extra='forbid'` is the same deliberate hardening ADR-0009 applies: a
provenance carrier should reject any smuggled key rather than silently
accept one. `fetches=()` (an empty tuple) is a valid, meaningful value — a
grounded answer need not cite every verified fetch made this turn, and a
prompt-echo-only or fallback-only answer may carry zero fetches while still
being a complete, well-formed `GroundedProvenance`.

Add a single **additive optional** field to `AgentRunResult`:

- `grounded: GroundedProvenance | None = None`

And, in `ne-api`'s `turn_wire.py` (the transport-independent frame shared by
`ne-api` and `ne-worker`), the matching additive fields:

- `TurnFrame.grounded: GroundedProvenance | None = None` (appended after the
  NE-VAF-U0 `verified` field, same additive-tail discipline).
- `TurnStepResult.grounded: GroundedProvenance | None = None` (appended
  after its `verified` field).
- `done_frame(seq, session_id, output, *, verified=None, grounded:
  GroundedProvenance | None = None)` — **keyword-only**, so every existing
  positional call site is untouched.
- `TurnFrame.sse_payload()`'s `done` branch surfaces `grounded` on the
  browser-facing payload when non-`None`, mirroring exactly how `verified`
  is surfaced there today (turn_wire.py's existing `if self.verified is not
  None:` block). **Unlike U0** — where the `sse_payload` surfacing was
  deliberately deferred to a later unit (U4) — this unit ships the
  surfacing immediately, because the mechanism it mirrors (`verified`)
  already surfaces in the merged code, and design doc §6's G5 file list
  never touches `turn_wire.py` again; G5's enqueue==inline parity DoD
  requires the relay-path renderer to already carry the field. When
  `grounded` is `None` the payload is unchanged, so this remains
  behavior-free: no code path in this unit ever constructs a non-`None`
  `GroundedProvenance` to exercise the branch outside tests.

`GroundedProvenance` is constructed **only** by the deterministic grounding
gate's runtime post-step (G3). No code path anywhere lets model-generated
text populate it — the model has no constructor call it can make; it can
only produce text that a downstream, non-model-judgment gate either does or
does not recognize as grounded. That gate logic, not this contract, is what
actually closes the forgeability gap; this unit only ensures the channel
exists and cannot be widened by accident.

### Alternatives rejected

- **Structured-text convention inside `output`.** Rejected outright, for the
  same reason ADR-0009 rejects it: any text convention over `output` is a
  replay-forgery vector — a model can reproduce it verbatim from prior
  context.
- **A separate top-level frame type (e.g. a new `grounded` `FrameType`)
  instead of a field on `done`.** Rejected for G0: SPEC §2.3 payload shapes
  and a new frame type are gate/sse/worker wiring decisions (G1-G5 scope); a
  trailing-default field is the minimal additive contract change that keeps
  this unit contracts-only.
- **Defaulted/optional fields on `GroundedProvenance` itself.** Rejected:
  provenance is meaningful only as a complete artifact description; partial
  provenance is not a lesser guarantee, it is a bug. All-or-nothing lives on
  the `T | None` carrier, matching the `VerifiedProvenance` precedent
  (ADR-0009) and the `_Frozen` config precedent throughout this file.
- **Deferring `sse_payload()` surfacing to a later unit, replaying U0's
  posture exactly.** Considered, but rejected for G0 specifically: G5's file
  list (design doc §6) never revisits `turn_wire.py`, so deferring the
  surfacing here would leave no unit that ever flips it. Shipping the
  no-op-when-`None` surfacing now costs nothing (no unit yet constructs a
  non-`None` value in production) and avoids a dangling TODO.

## Consequences

- **Purely additive MINOR bump 1.6.0 → 1.7.0.** No existing field is
  removed, renamed, or reordered; no existing API signature or observable
  behavior changes. `AgentRunResult`, `TurnFrame`, and `TurnStepResult` stay
  compatible with every pre-1.7.0 constructor call, proven by the full
  pre-existing test suite passing unmodified. `config_hash` is entirely
  untouched — `GroundedProvenance` is per-request data, not platform
  configuration (ADR-0007/ADR-0008/ADR-0009 precedent).
- **Byte-identical wire when `grounded` is absent.** Every `exclude_none`
  serialization of a `done` frame, `AgentRunResult`, or `TurnStepResult`
  produced without a `grounded` value is byte-for-byte identical to the
  pre-1.7.0 output; this is pinned by tests against the exact literal
  captured at HEAD 1ded8ae, before any NE-GA-G0 edit.
- **Mixed-version wire hazard, same accepted posture as ADR-0009.** After
  this change, a new-code frame's `model_dump(mode="json")` carries
  `"grounded": null`, which an OLD-code `TurnFrame` (`extra="forbid"`) would
  reject if read by a pre-1.7.0 process. Both `ne-api` and `ne-worker`
  deploy from one repo/image, so this is an accepted, already-precedented
  posture, not a new risk class.
- **G1-G5 own everything downstream of the contract.** G1 defines the
  deterministic gate core (and its `GATE_VERSION`, the real value of the
  `gate` field); G3 constructs `GroundedProvenance` inside the runtime
  post-step, including the deterministic verified-table fallback; G4 adds
  the one-pass model assist that can set `assist_used`; G5 wires the whole
  path through `sse.py`/`workflows.py` so a real, non-`None`
  `GroundedProvenance` reaches the browser via the surfacing this unit
  already ships. None of that behavior is in scope here.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the
unit's contract diff surface is `packages/ne-contracts/**`. It is indexed
from the repo-wide `docs/adr/README.md` for discovery.
