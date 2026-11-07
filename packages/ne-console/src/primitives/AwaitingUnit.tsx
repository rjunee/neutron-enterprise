// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// AwaitingUnit — the honest empty-state placard (design §0 law 5, §4 preamble).
// A view whose governed route has not shipped renders this instead of faking
// data: it names the route that will feed it and states plainly that nothing
// here is simulated.
//
// It does NOT name the build ticket. An internal unit id on a customer-facing
// screen tells an operator nothing they can act on and reads as unfinished
// software — the owner's objection to Posture documenting its own Python
// modules, in a different place.

import type { ReactNode } from "react";

export interface AwaitingUnitProps {
  /** Human title of the view (signage type). */
  title: string;
  /** The governed route(s) that will feed it, e.g. "GET /v1/posture". */
  feeds?: string;
  /** Optional in-page links to already-live related routes. */
  children?: ReactNode;
}

export function AwaitingUnit({ title, feeds, children }: AwaitingUnitProps) {
  return (
    <section className="ne-awaiting">
      <h1 className="ne-awaiting__title">{title}</h1>
      <span className="ne-label">NOT YET AVAILABLE</span>
      <p className="ne-awaiting__body">
        This view is not available in this deployment. It will render live governed data
        {feeds ? ` from ${feeds}` : ""} once it is. Nothing here is simulated.
      </p>
      {children && <div className="ne-awaiting__links">{children}</div>}
    </section>
  );
}
