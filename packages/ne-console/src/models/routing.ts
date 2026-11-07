// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE ROUTING TAB'S PURE PART — reshaping only, and deliberately so.
//
// WHICH MODEL A TASK MAY BE REPOINTED TO IS THE SERVER'S JUDGEMENT. It combines
// the boot-composed model set, the gateway's context-budget arithmetic, the tier
// table and the LIVE egress posture. Every one of those lives behind the API;
// a console that recomputed any of them would be transliterating server-side
// facts, and the first divergence would be a menu offering a change the server
// refuses (C7). So nothing below decides anything: `selectableTargets` FILTERS
// on the server's own `selectable` flag, and `changeConsequence` renders the
// server's `action`/`tier` into a sentence.
//
// The one thing this file does own is WORDING, which is a console concern.

import type { RouteTargetView, TaskRoutingView } from "../api/types";

/** The models the SERVER marked selectable, in server order. */
export function selectableTargets(row: TaskRoutingView): RouteTargetView[] {
  return row.targets.filter((target) => target.selectable);
}

/** Targets the server refused, with its reason — shown so a missing option is
 * explained rather than silently absent. */
export function refusedTargets(row: TaskRoutingView): RouteTargetView[] {
  return row.targets.filter((target) => !target.selectable && target.reason !== null);
}

/** True when this deployment offers this task any change at all. */
export function isConfigurable(row: TaskRoutingView): boolean {
  return selectableTargets(row).length > 0;
}

/** "Deployment default" / "Governed change" — where the running model came from. */
export function sourceLabel(row: TaskRoutingView): string {
  return row.source === "override" ? "Governed change" : "Deployment default";
}

/**
 * What submitting this target will do, in the server's terms.
 *
 * The TIER is the server's, read off the target — this function does not decide
 * that a frontier repoint needs approval, it reports that the server said so.
 * Tier 0/1 would apply immediately; tier 2+ parks for a named human. Both
 * sentences name the consequence before the click (the air-gap row's §2.5
 * discipline).
 */
export function changeConsequence(target: RouteTargetView): string {
  const parks = target.tier >= 2;
  const lane =
    target.lane === "frontier-api"
      ? `Route to ${target.model} — prompts for this task will leave the deployment`
      : `Route to ${target.model} — stays in-VPC`;
  return parks ? `${lane}; requires approval` : `${lane}; applies now`;
}

/** The lane word the table shows. Cosmetic only; the value is the server's. */
export function laneLabel(lane: string): string {
  if (lane === "private-vllm") return "In-VPC";
  if (lane === "frontier-api") return "Frontier";
  return lane;
}
