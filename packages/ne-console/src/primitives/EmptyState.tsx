// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// REDESIGN §1.4 — <EmptyState>. Empty IS the demo surface on most of these
// routes, so it is a DESIGNED state, not a fallback: a plain heading that is not
// a fault, one sentence naming what would put something here, and at most one
// action that produces it. A presenter must never face a blank screen.

import type { ReactNode } from "react";
import { Link } from "react-router";

export function EmptyState({
  title,
  children,
  action,
  aside,
}: {
  title: string;
  children: ReactNode;
  action?: { label: string; to: string };
  /** Optional second block — e.g. a preview of the anatomy of a real row. */
  aside?: ReactNode;
}) {
  return (
    <section className="ne-empty">
      <h2 className="ne-empty__title">{title}</h2>
      <div className="ne-empty__body">{children}</div>
      {action ? (
        <Link className="ne-empty__action" to={action.to}>
          {action.label}
        </Link>
      ) : null}
      {aside ? <div className="ne-empty__aside">{aside}</div> : null}
    </section>
  );
}
