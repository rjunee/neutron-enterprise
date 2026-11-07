// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// SURFACE ENTER/EXIT GRAMMAR — the code half of surface.css (motion design
// §4.1). Two exports: the class vocabulary, and the one hook that lets a
// surface which owns its own dismissal play an exit before it goes.
//
// WHY THE CLASSES ARE CONSTANTS. A second surface that spells the class inline
// is a second vocabulary the guardian cannot follow, and the guardian in
// surface.test.tsx proves both directions of the join: every class the
// stylesheet defines has a call site, and every `ne-surface-*` string in the
// product is a class the stylesheet defines. Dead motion CSS is exactly how
// `.ne-motion-settle` sat under law 4 declaring `transition: all` for three
// months (design §2).
//
// WHY THE HOOK EXISTS AND WHAT IT REFUSES TO DO. React unmounts a surface the
// instant its owner says closed, so an exit animation needs the node held for
// one more beat. Law 4 cl. 7 permits exactly this and nothing more: "removing
// the animated node itself". So:
//   * The phase change is a RENDER-phase update, not an effect. When the
//     operator closes a surface it is non-interactive (pointer-events: none)
//     and on its way out in the SAME commit — nothing waits a tick to react.
//   * No application state is downstream of it. The owner's `open` already
//     changed; `present` only decides whether a fading node is still painted.
//   * There is no `animationend` handler and no timer. The wait is the Web
//     Animations `finished` promise of the animations actually running on the
//     node — so when nothing is animating (jsdom, an engine without WAAPI, a
//     surface whose CSS never loaded) the list is empty and the node is dropped
//     immediately. The failure mode of a stalled exit is a surface that never
//     goes away, and that failure is not reachable from here.

import { useEffect, useState } from "react";
import type { RefObject } from "react";
import "./surface.css";

/** Detail/span/decision panes: translateX + fade over --motion-rod (§4.1). */
export const SURFACE_PANE = "ne-surface-pane";

/** Popovers and the palette: scale + fade over --motion-snap (§4.1). */
export const SURFACE_POP = "ne-surface-pop";

/** Every exit, both kinds: fade over --motion-snap (§3 asymmetry rule). */
export const SURFACE_EXIT = "ne-surface-exit";

export type SurfacePhase = "open" | "exiting" | "closed";

export interface SurfacePresence {
  /** Render the surface while true — open, or exiting and still painted. */
  present: boolean;
  /** True only while the surface is fading out. Pair with SURFACE_EXIT. */
  exiting: boolean;
  phase: SurfacePhase;
}

/**
 * @param open  the owner's state — true while the surface should be on screen.
 * @param ref   the CALLER's ref, attached to the animating node. The hook does
 *              not hand a ref back: a ref returned from a hook and read during
 *              render is exactly what `react-hooks/refs` rejects, and rightly —
 *              presence must be derivable without touching the DOM. The ref is
 *              read only inside the effect, and only to ask what is animating.
 */
export function useSurfacePresence(
  open: boolean,
  ref: RefObject<HTMLElement | null>,
): SurfacePresence {
  const [phase, setPhase] = useState<SurfacePhase>(open ? "open" : "closed");

  // Render-phase transition, deliberately not an effect. An effect would let
  // one frame render with the surface already gone, and the exit would flash
  // the node back in — which is worse than no exit at all.
  if (open && phase !== "open") setPhase("open");
  else if (!open && phase === "open") setPhase("exiting");

  useEffect(() => {
    if (phase !== "exiting") return;
    const node = ref.current;
    const running = node && typeof node.getAnimations === "function" ? node.getAnimations() : [];
    if (running.length === 0) {
      // Nothing is animating: there is nothing to wait for, and waiting anyway
      // would strand the surface on screen forever.
      setPhase("closed");
      return;
    }
    let live = true;
    void Promise.allSettled(running.map((a) => a.finished)).then(() => {
      if (live) setPhase("closed");
    });
    return () => {
      live = false;
    };
  }, [phase, ref]);

  return { present: phase !== "closed", exiting: phase === "exiting", phase };
}
