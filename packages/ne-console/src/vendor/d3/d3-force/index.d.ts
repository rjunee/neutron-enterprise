// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// HAND-WRITTEN type declarations (OURS — not vendored) for the SUBSET of the
// vendored d3-force@3.0.0 ES-module API the Graph Chamber uses. Kept minimal and
// precise: only the forces and simulation methods chamberSim.ts calls are typed.
// The vendored .js sources are byte-comparable to upstream (see ../README.md);
// this declaration is our contract surface over them, so tsc type-checks our use
// without pulling @types/d3-force into the npm closure.

/** A mutable simulation node — d3-force writes x/y/vx/vy in place. */
export interface SimulationNodeDatum {
  index?: number;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  /** Fixed position (pins the node); null/undefined = free. */
  fx?: number | null;
  fy?: number | null;
}

/** A link between two nodes. `source`/`target` are node refs (or ids pre-init). */
export interface SimulationLinkDatum<N extends SimulationNodeDatum> {
  source: N | string | number;
  target: N | string | number;
  index?: number;
}

/** A force is a function the simulation invokes per tick, optionally with an
 * `initialize` hook and chainable accessors. We treat it opaquely. */
export interface Force<N extends SimulationNodeDatum, L> {
  (alpha: number): void;
  initialize?(nodes: N[], random: () => number): void;
}

export interface Simulation<N extends SimulationNodeDatum, L extends SimulationLinkDatum<N>> {
  nodes(): N[];
  nodes(nodes: N[]): this;
  force(name: string): Force<N, L> | undefined;
  force(name: string, force: Force<N, L> | null): this;
  alpha(): number;
  alpha(alpha: number): this;
  alphaMin(): number;
  alphaMin(min: number): this;
  alphaDecay(): number;
  alphaDecay(decay: number): this;
  alphaTarget(): number;
  alphaTarget(target: number): this;
  velocityDecay(): number;
  velocityDecay(decay: number): this;
  tick(iterations?: number): this;
  stop(): this;
  restart(): this;
  on(typenames: string, listener: ((this: Simulation<N, L>) => void) | null): this;
  find(x: number, y: number, radius?: number): N | undefined;
}

export function forceSimulation<N extends SimulationNodeDatum, L extends SimulationLinkDatum<N>>(
  nodes?: N[],
): Simulation<N, L>;

export interface LinkForce<N extends SimulationNodeDatum, L extends SimulationLinkDatum<N>>
  extends Force<N, L> {
  links(): L[];
  links(links: L[]): this;
  id(id: (node: N, i: number, nodes: N[]) => string): this;
  distance(distance: number | ((link: L) => number)): this;
  strength(strength: number | ((link: L) => number)): this;
}
export function forceLink<N extends SimulationNodeDatum, L extends SimulationLinkDatum<N>>(
  links?: L[],
): LinkForce<N, L>;

export interface ManyBodyForce<N extends SimulationNodeDatum> extends Force<N, never> {
  strength(strength: number | ((node: N) => number)): this;
  distanceMax(max: number): this;
  distanceMin(min: number): this;
}
export function forceManyBody<N extends SimulationNodeDatum>(): ManyBodyForce<N>;

export interface CenterForce<N extends SimulationNodeDatum> extends Force<N, never> {
  x(x: number): this;
  y(y: number): this;
  strength(strength: number): this;
}
export function forceCenter<N extends SimulationNodeDatum>(x?: number, y?: number): CenterForce<N>;

export interface CollideForce<N extends SimulationNodeDatum> extends Force<N, never> {
  radius(radius: number | ((node: N) => number)): this;
  strength(strength: number): this;
}
export function forceCollide<N extends SimulationNodeDatum>(
  radius?: number | ((node: N) => number),
): CollideForce<N>;

export interface PositioningForce<N extends SimulationNodeDatum> extends Force<N, never> {
  x(x: number | ((node: N) => number)): this;
  y(y: number | ((node: N) => number)): this;
  strength(strength: number): this;
}
export function forceX<N extends SimulationNodeDatum>(x?: number): PositioningForce<N>;
export function forceY<N extends SimulationNodeDatum>(y?: number): PositioningForce<N>;
