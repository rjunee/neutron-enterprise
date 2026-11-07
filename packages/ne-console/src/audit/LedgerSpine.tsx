// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — THE LEDGER SPINE, drawn. One element per rendered row, in the seq
// gutter: a hairline, a node, and — on the entry the server named — a physical
// gap with a red tick across it.
//
// STATIC BY CONSTRUCTION. No animation, no transition, no timing: there is
// nothing here for `prefers-reduced-motion` to collapse, and the tamper
// evidence is identical in a screenshot, on a printed export of the page and on
// the fortieth open of the day. §6.2 demolished the verification sweep that
// used to travel this gutter — the server verifies as a unit and reports a
// verdict, and no scan the client never observed may be replayed as motion.
// This is that meaning, told truthfully and at rest.
//
// THE GAP IS GEOMETRY, NOT COLOUR. The hairline stops short of the row edge and
// a red tick sits in the void it leaves, so the break reads as a cut chain
// before any colour is perceived (§0: nothing is encoded by colour alone).
//
// aria-hidden: every fact it carries is already spoken — the BREAK pill on the
// entry, the verdict strip, and the `role="alert"` band above the table. The
// spine is the redundant channel, never the only one.

import type { SpineMark } from "./spine";

export function LedgerSpine({ mark }: { mark: SpineMark | null }) {
  if (mark === null) return null;
  const classes = [
    "ne-audit__spine",
    `ne-audit__spine--${mark.tier}`,
    mark.cut === null ? "" : `ne-audit__spine--cut-${mark.cut}`,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <span className={classes} data-spine={mark.tier} aria-hidden="true">
      <span className="ne-audit__spineline" />
      <span className="ne-audit__spinenode" />
      {mark.cut === null ? null : (
        <span className={`ne-audit__spinecut ne-audit__spinecut--${mark.cut}`} />
      )}
    </span>
  );
}
