// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// HashPlate (design §1.2 hash typography): every hash / config_hash / seq / IRI
// renders first-6 + last-6 at full --text with the middle dimmed to --text-dim.
// (--text-faint is banned for text: it measures below 4.5:1 in both themes.)
// Hover/focus reveals the FULL value at full text with a copy affordance. When
// there is no value (or unattested=true) it renders an honest hollow "UNATTESTED"
// placard naming the route that will populate it — NEVER a fabricated hash
// (§0 law 5). Display-only: the client never computes or verifies hashes.

import { useRef, useState } from "react";
import { SURFACE_EXIT, SURFACE_POP, useSurfacePresence } from "./surface";

export interface HashPlateProps {
  /** The hash string. Absent → renders the hollow unattested placard. */
  value?: string;
  /** Serial-plate weight (config_hash serial plate, chain verdict — §1.6). */
  plate?: boolean;
  /** Collapse the dimmed middle to a single ellipsis instead of the raw chars. */
  collapsed?: boolean;
  /** Force the honest hollow state even if a value is (later) supplied. */
  unattested?: boolean;
}

const HEAD_TAIL = 6;

export function HashPlate({
  value,
  plate = false,
  collapsed = false,
  unattested = false,
}: HashPlateProps) {
  const [open, setOpen] = useState(false);
  // §4.1 — the popover pops in place (--motion-snap) and fades out over the
  // same --motion-snap. It is a popover, not a page: nothing about the hash it
  // reveals is gated on either animation, and the full value is in the DOM the
  // frame the operator hovers.
  const popoverRef = useRef<HTMLSpanElement | null>(null);
  const popover = useSurfacePresence(open, popoverRef);

  const cls = ["ne-hashplate", plate ? "ne-hashplate--plate" : ""].filter(Boolean).join(" ");

  if (unattested || !value) {
    // Honest hollow state — awaiting the route that will stamp the real hash.
    return (
      <span className={`${cls} ne-hashplate--unattested`} title="awaiting GET /v1/posture">
        <span className="ne-label">UNATTESTED</span>
      </span>
    );
  }

  // Short values render whole at full text — no dimmed middle to speak of.
  if (value.length <= HEAD_TAIL * 2) {
    return <span className={cls}>{value}</span>;
  }

  const head = value.slice(0, HEAD_TAIL);
  const tail = value.slice(-HEAD_TAIL);
  const middle = value.slice(HEAD_TAIL, -HEAD_TAIL);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value ?? "");
    } catch {
      // Clipboard unavailable (e.g. insecure context) — nothing to surface;
      // the value is still visible in the popover for manual selection.
    }
  }

  return (
    <span className={cls}>
      <button
        type="button"
        className="ne-hashplate__trigger"
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        aria-expanded={open}
        aria-label={`hash ${value}`}
      >
        <span className="ne-hashplate__end">{head}</span>
        <span className="ne-hashplate__mid">{collapsed ? "…" : middle}</span>
        <span className="ne-hashplate__end">{tail}</span>
      </button>
      {popover.present && (
        <span
          ref={popoverRef}
          className={`ne-hashplate__popover ${popover.exiting ? SURFACE_EXIT : SURFACE_POP}`}
          role="status"
          // A surface on its way out has already been dismissed. `inert` takes
          // the fading node out of the tab order and out of the accessibility
          // tree in one attribute — a still-tabbable COPY button inside an
          // aria-hidden ghost would be the accessibility defect this exit is
          // supposed to be too small to introduce.
          inert={popover.exiting}
        >
          <span className="ne-hashplate__full">{value}</span>
          <button
            type="button"
            className="ne-hashplate__copy"
            aria-label="copy full hash"
            onClick={copy}
          >
            COPY
          </button>
        </span>
      )}
    </span>
  );
}
