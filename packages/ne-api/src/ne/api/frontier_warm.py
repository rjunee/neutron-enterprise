# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The governed frontier warm call (NE-FRONTIER-LIVE) — how a frontier lane
earns the evidence :mod:`ne.api.model_selection` requires, without inventing a
second egress path.

Why this module exists
----------------------
:mod:`ne.api.model_selection` measures frontier availability from ONE fact: a
recent (``FRONTIER_OBSERVATION_MAX_AGE_S`` = 60s) successful
``ne.gateway.model.observed`` record for that alias, on the verified audit
chain, under THIS process ``config_hash``.  That rule is deliberate and is NOT
relaxed here — "an actual frontier call is the only truthful liveness signal".

The consequence, observed on the live demo, is a bootstrap deadlock: no run may
be authorized onto the frontier lane until a frontier call has succeeded, and no
frontier call happens because none is authorized.  A deployment that stages a
real frontier lane (``ne demo up --frontier anthropic``) therefore looked
IDENTICAL to one that had no key at all — and where the route carried private
fallbacks it degraded onto them SILENTLY, which is the failure mode the whole
swap beat exists to rule out.

What this does instead
----------------------
Issue the missing call, through the SAME governed gateway production traffic
uses.  There is no second client, no second policy path, no "probe mode":

* ``lane=Lane.FRONTIER_API`` **and** ``requested_model=<alias>`` together mean
  :meth:`LiteLLMGateway._candidates` filters the route's fallback chain down to
  frontier-lane candidates only.  A warm call therefore CANNOT roll onto a
  private model and mint evidence for the wrong lane — the false-success shape
  BD-15 exists to prevent;
* the C2 egress guard runs (allowlisted host, or refusal), and under ``AIR_GAP``
  ``EgressPolicy._check_frontier`` refuses BEFORE transport — so an air-gapped
  deployment produces no success evidence and the lane goes dark on its own,
  within the 60s window, with no separate air-gap branch in this module;
* pre-egress redaction runs, because it runs for every ``FRONTIER_API``
  candidate (``_prepare_egress_body``);
* the attempt is audited either way, which IS the evidence.

The prompt is a fixed, non-customer literal (:data:`WARM_PROMPT`), so warming
egresses no customer text, and the call carries an explicit output ceiling
(:data:`WARM_MAX_TOKENS`) so a call the platform issues on its OWN initiative
can never run away.  That ceiling is a BOUND, not a reservation: billing follows
the tokens actually generated, and this one-word instruction was measured
generating 4-20 of them.  Reading the bound as if it were the spend is what set
it to 16 and made ~7% of warms fail — see the constant, which carries the
measurements and the honest worst case.

Posture disclosure (say this out loud, do not bury it)
------------------------------------------------------
This is a real, billable, outbound provider call that the platform makes on its
own initiative when a run is being authorized onto a frontier route.  It is
default-OFF: :attr:`ne.api.settings.ApiSettings.frontier_warm_enabled` is
``False`` and no composition wires a warmer unless a frontier lane was
deliberately staged.  With it off, ``ModelSelectionService`` behaviour is
byte-identical to before this module existed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

import anyio

from ne.contracts.models import CompletionRequest, ModelGateway
from ne.contracts.types import ChatMessage, Lane
from ne.gateway.config import ModelDef

__all__ = [
    "WARM_DEBOUNCE_S",
    "WARM_MAX_TOKENS",
    "WARM_PROMPT",
    "FrontierWarm",
    "GatewayFrontierWarm",
]

logger = logging.getLogger(__name__)

#: Fixed, non-customer text.  Never assembled from a request, a session, or a
#: corpus — a warm call must not be able to egress anything a user typed.
WARM_PROMPT = "Reply with exactly one word: ok"

#: Output ceiling for a warm call.  A BOUND on a self-issued provider call,
#: sized from live measurement rather than from a cost intuition or a folklore
#: token minimum — both of which produced the 16 this replaces.
#:
#: WHAT 16 ACTUALLY DID.  ``anthropic/claude-fable-5`` (the demo's
#: ``frontier-best`` alias) is in ``live._THINKING_DISABLE_REFUSED``: the
#: provider 400s a thinking disable on it, so the think channel is always
#: available and the model decides per call whether to use it.  When it does,
#: the first generated tokens go there.  Under a 16-token ceiling that reply can
#: reach ``finish_reason='length'`` with ``thinking_blocks=1`` and no answer, so
#: ``LiteLLMGateway`` raises ``ReasoningWithoutAnswerError``, records
#: ``ne.gateway.model.observed`` with outcome ``error``, and the warm earns the
#: lane nothing.
#:
#: HOW OFTEN — counted on the live chain, not assumed.  Since 2026-08-27 the
#: alias has 26,934 successful warms against 2,507 errors; 7.1% error over the
#: last 12h.  So the lane DOES evidence, ~93% of the time: it is not dark, it
#: never "stayed dark for a whole process lifetime", and any comment saying so
#: is wrong.  The honest defect is narrower and still worth fixing: ~7% of warms
#: burn a cycle, and each one leaves the lane past the 60s evidence window for
#: ~3.4s until the expiry-anchored retry lands.  The control isolates the cause
#: to the think channel and nothing else — ``claude-sonnet-5``, thinking
#: disabled, is 1,258 successes / 0 errors over the same period.
#:
#: WHY 256.  Two calls at each of six ceilings, live 2026-09-09, through this
#: same litellm chokepoint at one model/prompt/param shape:
#:
#:     ceiling   run 1    run 2   answered   thinking blocks   completion tokens
#:          16   3.273    3.225    yes/yes             0 , 0            4 ,  4
#:          64   3.118    3.219    yes/yes             0 , 0            4 ,  4
#:         256   3.541    3.233    yes/yes             0 , 0            4 ,  4
#:         512   3.310    3.372    yes/yes             1 , 0           18 ,  4
#:        1024   3.240    3.236    yes/yes             0 , 0            4 ,  4
#:        2048   3.224    3.408    yes/yes             1 , 0           20 ,  4
#:
#: Three readings, each of which retires a plausible belief:
#:
#: * WALL CLOCK IS FLAT IN THE CEILING.  3.118-3.541s across a 128x range, with
#:   no trend.  Cross-checked against the governed path itself (n=676 over 6h at
#:   ceiling 16): p50 3.327s, p95 3.878s — the probe matches production p50 to
#:   0.05s.  Raising the ceiling neither costs nor buys latency.
#: * THE CEILING IS A BOUND, NOT A RESERVATION.  Billing is on tokens actually
#:   generated, and the generation measured here is 4 completion tokens when
#:   the model does not think and 18-20 when it does, at EVERY ceiling that
#:   let the reply finish.  So raising 16 -> 256 does not buy 240 tokens of
#:   spend.  What it changes is the ~7% of warms that 16 was TRUNCATING:
#:   generation stops AT the ceiling, so those billed 16 and bought nothing
#:   (no answer token, so ``ReasoningWithoutAnswerError`` and no evidence),
#:   where at 256 the same reply completes at ~20 and earns the lane.  The
#:   delta is therefore a few tokens on a minority of warms, spent on replies
#:   that now count — not a 16x multiplier.  "Keep it small to keep it cheap"
#:   was the wrong instinct because what the small ceiling actually bought was
#:   truncation; it was NOT free, and the honest worst case is the bound
#:   itself.  Reasoning length is the provider's choice, not ours: a reply
#:   that generated all the way to the bound would bill 256, and nothing in
#:   this module can stop it.  That is precisely why the bound stops at 256
#:   instead of 2048 — see the next bullet.
#: * 256 IS >12x THE LARGEST THINKING-CARRYING REPLY EVER OBSERVED (20 tokens).
#:   That margin is the whole point of the number.  Higher buys nothing the
#:   measurements can see while loosening the bound on an unattended,
#:   self-issued provider call, so it stops here rather than at 2048.
#:
#: THERE IS NO TOKEN MINIMUM AT WORK.  Nothing here rests on a floor below which
#: reasoning is impossible, because no such floor exists for this parameter: the
#: live error rows at ceiling 16 carry ``thinking_blocks=1``, a thinking block
#: emitted under a 16-token ceiling.  The 1024 that circulates as "the smallest
#: thinking allocation the API accepts" is a stale ``budget_tokens`` minimum
#: from a previous model generation; ``budget_tokens`` does not exist on this
#: one (the API 400s it), and ``max_tokens`` is a different parameter bounding
#: the WHOLE reply.  Re-derive this number from the observed reply SIZE above,
#: never from a claimed minimum.
#:
#: THE CEILING ITSELF STAYS.  Deleting it in favour of the lane's declared
#: ``max_output_tokens`` was rejected: this is a call the platform makes on its
#: own initiative (see the posture disclosure above), and unattended-and-
#: unbounded is a worse posture than unattended-and-bounded.  The bound was
#: never the problem; its size was.
WARM_MAX_TOKENS = 256

#: Minimum spacing between warm calls for the SAME alias from ONE process.  The
#: evidence window is 60s, so this only suppresses storms (concurrent
#: submissions), never the warm a cold lane actually needs.
WARM_DEBOUNCE_S = 5.0


class FrontierWarm(Protocol):
    """The seam :class:`ne.api.model_selection.ModelSelectionService` calls.

    Implementations MUST NOT raise: a warm that failed is simply a lane with no
    evidence, and the caller's existing absence handling is the correct
    outcome.
    """

    async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None: ...


class GatewayFrontierWarm:
    """Warm frontier aliases by issuing one governed completion each."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        prompt: str = WARM_PROMPT,
        max_tokens: int = WARM_MAX_TOKENS,
        debounce_s: float = WARM_DEBOUNCE_S,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gateway = gateway
        self._prompt = prompt
        self._max_tokens = max_tokens
        self._debounce_s = debounce_s
        self._clock = clock
        self._last: dict[str, datetime] = {}
        self._lock = anyio.Lock()

    async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None:
        # Serialized so N concurrent submissions produce ONE warm per alias,
        # not N: the debounce below is only meaningful under the lock.
        async with self._lock:
            for model in models:
                if model.lane is not Lane.FRONTIER_API:
                    # Defensive: a private alias here would mean the caller's
                    # lane split broke. Warming it would be a pointless private
                    # call, never an egress — but it is still not this seam's job.
                    continue
                if self._recently_warmed(model.name):
                    continue
                self._last[model.name] = self._clock()
                await self._warm_one(model, task=task)

    def _recently_warmed(self, name: str) -> bool:
        last = self._last.get(name)
        if last is None:
            return False
        return (self._clock() - last).total_seconds() < self._debounce_s

    async def _warm_one(self, model: ModelDef, *, task: str) -> None:
        request = CompletionRequest(
            messages=(ChatMessage(role="user", content=self._prompt),),
            task=task,
            # Both together are load-bearing: see the module docstring. Dropping
            # `lane` would let a route's private fallback answer a FRONTIER warm.
            lane=Lane.FRONTIER_API,
            requested_model=model.name,
            params={"max_tokens": self._max_tokens},
        )
        try:
            await self._gateway.complete(request)
        except Exception:
            # Names only (C12): the provider's own error text can echo prompt
            # substrings, and the gateway already recorded the attempt on the
            # chain. A failed warm is absence of evidence, never an exception
            # the selection path has to interpret.
            logger.warning(
                "frontier warm call did not succeed for model %r on task %r; "
                "the lane stays unevidenced",
                model.name,
                task,
            )
