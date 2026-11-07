// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §1.4 — PAGE ANATOMY. A page is a title row and content.
//
// The 40px title row is an h1 at left and, at most, one primary action at right.
// There is NO purpose line, NO status strip, NO hero readout and NO page foot:
// §1.3 deletes all four. Their combined cost on the shipped console was the top
// third of every screen spent explaining the screen to someone who navigated to
// it on purpose.
//
// "No prose between title and toolbar, ever." That rule is enforceable only if
// there is nowhere to put prose, so this component has no slot for it.

import type { ReactNode } from "react";

export function PageHeader({
  title,
  meta,
  action,
  tabs,
}: {
  /** Sentence case. The page name, nothing else. */
  title: string;
  /**
   * THE FACTS MARGIN — a short count beside the title, never a sentence about
   * the screen. "9 documents visible" is the reader's own question answered in
   * three words; "Documents you can see, filtered by your role" is the lede the
   * standard deletes. The slot takes a count, not prose, and a screen that has
   * no count yet (loading, refused, failed) passes nothing rather than a zero.
   */
  meta?: ReactNode;
  /** At most one primary action, right-aligned. */
  action?: ReactNode;
  /** In-page tabs, when the screen has them (Library, Models, Improvement). */
  tabs?: ReactNode;
}) {
  return (
    <header className="ne-titlerow">
      <div className="ne-titlerow__main">
        <h1 className="ne-titlerow__h1">{title}</h1>
        {meta === undefined || meta === null ? null : (
          <span className="ne-titlerow__meta">{meta}</span>
        )}
        {tabs ? <div className="ne-titlerow__tabs">{tabs}</div> : null}
      </div>
      {action ? <div className="ne-titlerow__action">{action}</div> : null}
    </header>
  );
}

/** The list-screen shell: title row, then whatever the screen puts under it
 * (toolbar → table → pagination row). Exists so every list screen has the same
 * geometry without copying the same three wrappers. */
export function ListScreen({
  title,
  meta,
  action,
  tabs,
  children,
}: {
  title: string;
  meta?: ReactNode;
  action?: ReactNode;
  tabs?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="ne-screen">
      <PageHeader title={title} meta={meta} action={action} tabs={tabs} />
      <div className="ne-screen__body">{children}</div>
    </div>
  );
}
