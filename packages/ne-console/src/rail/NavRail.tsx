// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §1.2 — THE RAIL. Wordmark, deployment name, navigation, Settings at the foot.
// Nothing else.
//
// WHAT LEFT, AND WHERE IT WENT:
//   * the twelve-constraint annunciator → Posture. It is a compliance widget; it
//     was parked next to the chat box, where nobody asking a question is auditing
//     twelve controls. Twelve rows of persistent chrome bought one destination.
//   * the config-hash plate, the chain lamp, the air-gap row, `TRIMMED AS:` →
//     the top bar's status popover, which is now the ONE home of global state.
//   * the guided-tour button and the theme toggle → the account menu.
// Nothing was preserved "out of caution": the rail is 240px of navigation, and a
// destination is the only thing allowed to live in it.
//
// ROLE FILTERING IS LEGIBILITY, NOT AUTHORIZATION (C7). An item is dropped only
// on a positive server refusal from GET /v1/me. While that answer is IN FLIGHT
// a gated item's slot renders as a skeleton — pending is not permitted, so no
// link is ever shown and then snatched away — and a deployment that answers
// with no advisory renders everything (unknown is not denied). A hidden route
// typed into the address bar still mounts, still asks, and still renders the
// server's own refusal — see test/nav-refusal.test.tsx.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { NavLink, useLocation } from "react-router";
import type { NavLinkProps } from "react-router";
import { useRuntimeConfig } from "../AppBootstrap";
import { entitlementsOf } from "../auth/entitlements";
import { usePrincipal } from "../auth/PrincipalContext";
import { useAuditPoll } from "../hooks/useAuditPoll";
import { usePendingApprovals } from "../hooks/usePendingApprovals";
import { NeutronMark } from "../assets/NeutronMark";
import { RailIcon } from "./RailIcon";
import { NAV_FOOT, NAV_SECTIONS, railSections } from "./navSections";
import type { RailSlot } from "./navSections";
import { useCountTick } from "./railMotion";

/**
 * §1.2 — the deployment name, or the serving host alone when it is absent.
 *
 * "No instruction to configure it anywhere in the UI." The unnamed placard used
 * to carry "name this deployment in config.json" as permanent brand chrome, in
 * every screenshot of every demo, addressed to someone who is not reading the
 * sidebar. The host is a fact; the to-do was a developer note.
 */
function DeploymentName() {
  const config = useRuntimeConfig();
  const name =
    config.deployment_name ?? (typeof window === "undefined" ? "" : window.location.host);
  return <div className="ne-rail__deployment">{name}</div>;
}

/**
 * The accessible name of the Approvals badge (§1.2). Pure + exported so every
 * combination is checkable without a provider.
 *
 * The VISIBLE text is `{count}` alone — that is the spec, and a queue count is
 * the one number in this product people read a hundred times a day. The
 * accessible name names both quantities, so a screen-reader user is not asked to
 * infer "overdue" from a tint they cannot see. The two never disagree: the label
 * is the visible numeral plus the word it belongs to.
 */
export function badgeLabel(count: number, overdue: number): string {
  return overdue > 0 ? `${count} pending, ${overdue} overdue` : `${count} pending`;
}

/**
 * The pending count, live on every route.
 *
 * A refused or unread queue renders NOTHING — never a zero. A key that was never
 * shown the queue must not be told there is nothing in it, and an empty queue
 * needs no annunciation either.
 */
function ApprovalsBadge({ collapsed, id }: { collapsed: boolean; id: string }) {
  const { status, count, breached } = usePendingApprovals();
  // §4.4 — the badge tick. The epoch rises only when the pending count rises
  // between two KNOWN polled readings (railMotion.ts): never on first load, a
  // decrease, an unchanged reading, or a refused/errored queue (count is null
  // there, per the provider's contract). The epoch is the element's key, so an
  // increase remounts the badge and restarts the one-shot; every other render
  // keeps the node and CSS never replays a finished animation.
  const tick = useCountTick(count);
  if (status !== "ok" || count === null || count === 0) return null;
  const overdue = breached ?? 0;
  const label = badgeLabel(count, overdue);
  return (
    <span
      id={id}
      key={tick}
      className={`ne-rail__badge${overdue > 0 ? " ne-rail__badge--overdue" : ""}${
        tick > 0 ? " ne-rail__badge--tick" : ""
      }`}
      title={collapsed ? undefined : label}
      aria-label={label}
    >
      {/* NOT COLOUR-ONLY. The overdue state changes the tint AND adds this
          glyph, so a reader who sees no colour still sees that the queue has
          something past its deadline. The badge's TEXT stays the count alone. */}
      {overdue > 0 ? (
        <span className="ne-rail__badgeglyph" aria-hidden="true">
          !
        </span>
      ) : null}
      <span aria-hidden="true">{count}</span>
    </span>
  );
}

/**
 * A destination whose surface answer is still in flight (§2.4's loading
 * vocabulary): a shimmer bar holding the destination's 32px row, so nothing
 * jumps when the verdict lands. `.ne-skeleton` is the ONE selector the motion
 * law permits to loop (primitives.css law 4 cl. 1). `aria-hidden`: the rail's
 * single visually-hidden "Loading navigation" status line speaks for every
 * pending slot at once.
 */
function RailPendingSlot() {
  return (
    <span className="ne-rail__slotpending" aria-hidden="true">
      <span className="ne-skeleton" />
    </span>
  );
}

function RailSlotNode({
  slot,
  collapsed,
  end = false,
  transit = null,
}: {
  slot: RailSlot;
  collapsed: boolean;
  end?: boolean;
  transit?: number | null;
}) {
  // A PENDING slot never pulses (and never badges): its permission is unknown,
  // so it has no activity to depict — a transit that maps to a pending route
  // is dropped here, not deferred. The ledger row is the fact's record; the
  // dot is a redundant channel that only rides a destination the rail is
  // actually showing (railMotion.test.tsx pins this).
  if (slot.kind === "pending") return <RailPendingSlot />;
  return (
    <RailLink label={slot.label} to={slot.to} collapsed={collapsed} end={end} transit={transit} />
  );
}

interface TooltipPosition {
  readonly left: number;
  readonly pathname: string;
  readonly top: number;
}

interface TooltipArbiterValue {
  readonly activeId: string | null;
  readonly claim: (id: string) => void;
  readonly dismissAll: () => void;
  readonly release: (id: string) => void;
}

const TooltipArbiterContext = createContext<TooltipArbiterValue | null>(null);

function TooltipArbiter({ children }: { children: ReactNode }) {
  const [owners, setOwners] = useState<readonly string[]>([]);
  const claim = useCallback((id: string) => {
    setOwners((current) => [...current.filter((owner) => owner !== id), id]);
  }, []);
  const release = useCallback((id: string) => {
    setOwners((current) => current.filter((owner) => owner !== id));
  }, []);
  const dismissAll = useCallback(() => {
    setOwners([]);
  }, []);
  const value = useMemo(
    () => ({ activeId: owners.at(-1) ?? null, claim, dismissAll, release }),
    [claim, dismissAll, owners, release],
  );
  return <TooltipArbiterContext.Provider value={value}>{children}</TooltipArbiterContext.Provider>;
}

function useTooltipArbiter(): TooltipArbiterValue {
  const value = useContext(TooltipArbiterContext);
  if (value === null) throw new Error("TooltipNavLink requires TooltipArbiter");
  return value;
}

function TooltipNavLink({
  ariaLabel,
  ariaLabelledby,
  children,
  className,
  collapsed,
  end,
  label,
  title,
  to,
}: {
  ariaLabel?: string;
  ariaLabelledby?: string;
  children: ReactNode;
  className: NavLinkProps["className"];
  collapsed: boolean;
  end?: boolean;
  label: string;
  title?: string;
  to: string;
}) {
  const location = useLocation();
  const anchorRef = useRef<HTMLAnchorElement | null>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const frame = useRef<number | null>(null);
  const focusOwned = useRef(false);
  const pointerOwned = useRef(false);
  const tooltipId = useId();
  const { activeId, claim, dismissAll, release } = useTooltipArbiter();
  const [position, setPosition] = useState<TooltipPosition | null>(null);
  const cancelDismiss = useCallback(() => {
    if (closeTimer.current !== null) clearTimeout(closeTimer.current);
    closeTimer.current = null;
  }, []);
  const cancelFrame = useCallback(() => {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
  }, []);
  const dismiss = useCallback(() => {
    cancelDismiss();
    cancelFrame();
    focusOwned.current = false;
    pointerOwned.current = false;
    dismissAll();
    setPosition(null);
  }, [cancelDismiss, cancelFrame, dismissAll]);
  const dismissOwned = useCallback(() => {
    cancelDismiss();
    cancelFrame();
    focusOwned.current = false;
    pointerOwned.current = false;
    release(tooltipId);
    setPosition(null);
  }, [cancelDismiss, cancelFrame, release, tooltipId]);
  const scheduleDismiss = useCallback(() => {
    cancelDismiss();
    closeTimer.current = setTimeout(() => {
      if (!focusOwned.current && !pointerOwned.current) {
        release(tooltipId);
        setPosition(null);
      }
    }, 80);
  }, [cancelDismiss, release, tooltipId]);
  const releaseFocus = useCallback(() => {
    focusOwned.current = false;
    if (!pointerOwned.current) {
      cancelDismiss();
      cancelFrame();
      release(tooltipId);
      setPosition(null);
    }
  }, [cancelDismiss, cancelFrame, release, tooltipId]);
  const measure = useCallback(() => {
    if (!collapsed || anchorRef.current === null) return;
    const rect = anchorRef.current.getBoundingClientRect();
    const railRight =
      anchorRef.current.closest(".ne-rail")?.getBoundingClientRect().right ?? rect.right;
    const center = rect.top + rect.height / 2;
    setPosition({
      left: Math.max(rect.right, railRight),
      pathname: location.pathname,
      top: Math.min(Math.max(center, 16), Math.max(16, window.innerHeight - 16)),
    });
  }, [collapsed, location.pathname]);
  const show = useCallback(() => {
    cancelDismiss();
    claim(tooltipId);
    measure();
  }, [cancelDismiss, claim, measure, tooltipId]);

  const visiblePosition =
    collapsed && activeId === tooltipId && position?.pathname === location.pathname
      ? position
      : null;

  useEffect(() => {
    if (collapsed) return;
    cancelDismiss();
    const timer = setTimeout(dismiss, 0);
    return () => clearTimeout(timer);
  }, [cancelDismiss, collapsed, dismiss]);

  useEffect(() => {
    if (position === null || position.pathname === location.pathname) return;
    const timer = setTimeout(dismiss, 0);
    return () => clearTimeout(timer);
  }, [dismiss, location.pathname, position]);

  useEffect(
    () => () => {
      cancelDismiss();
      cancelFrame();
      release(tooltipId);
    },
    [cancelDismiss, cancelFrame, release, tooltipId],
  );

  useEffect(() => {
    if (visiblePosition === null) return;
    const repositionFocused = () => {
      if (!focusOwned.current) {
        dismissOwned();
        return;
      }
      cancelFrame();
      frame.current = requestAnimationFrame(() => {
        frame.current = null;
        measure();
      });
    };
    window.addEventListener("resize", repositionFocused);
    window.addEventListener("scroll", repositionFocused, true);
    return () => {
      window.removeEventListener("resize", repositionFocused);
      window.removeEventListener("scroll", repositionFocused, true);
    };
  }, [cancelFrame, dismissOwned, measure, visiblePosition]);

  useEffect(() => {
    if (visiblePosition === null) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") dismiss();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [dismiss, visiblePosition]);

  const tooltip =
    visiblePosition === null || typeof document === "undefined"
      ? null
      : createPortal(
          <span
            className="ne-rail__tooltip-hit"
            style={{ left: visiblePosition.left, top: visiblePosition.top }}
            onMouseEnter={() => {
              pointerOwned.current = true;
              cancelDismiss();
              claim(tooltipId);
            }}
            onMouseLeave={() => {
              pointerOwned.current = false;
              scheduleDismiss();
            }}
          >
            <span id={tooltipId} className="ne-rail__tooltip" role="tooltip">
              {label}
            </span>
          </span>,
          document.body,
        );
  return (
    <>
      <NavLink
        ref={anchorRef}
        to={to}
        end={end}
        className={className}
        title={title}
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledby}
        aria-describedby={visiblePosition === null ? undefined : tooltipId}
        onFocus={() => {
          focusOwned.current = true;
          show();
        }}
        onBlur={releaseFocus}
        onMouseEnter={() => {
          pointerOwned.current = true;
          show();
        }}
        onMouseLeave={() => {
          pointerOwned.current = false;
          scheduleDismiss();
        }}
        onClick={dismiss}
        onKeyDown={(event) => {
          if (event.key === "Escape") dismiss();
        }}
      >
        {children}
      </NavLink>
      {tooltip}
    </>
  );
}

function RailLink({
  label,
  to,
  collapsed,
  end = false,
  transit = null,
}: {
  label: string;
  to: string;
  collapsed: boolean;
  end?: boolean;
  /** §4.3 — non-null when a governed event transited this destination in the
   * shared poll's CURRENT batch; the value is that batch's latestSeq, used as
   * the dot's key so back-to-back fresh batches on one route restart the
   * one-shot instead of silently coalescing. */
  transit?: number | null;
}) {
  const labelId = useId();
  const badgeId = useId();
  return (
    <TooltipNavLink
      to={to}
      end={end}
      collapsed={collapsed}
      className={({ isActive }) => `ne-rail__node${isActive ? " active" : ""}`}
      label={label}
      title={collapsed ? undefined : label}
      ariaLabelledby={`${labelId}${to === "/approvals" ? ` ${badgeId}` : ""}`}
    >
      {/* THE DESTINATION'S MARK (§1.2, round-3 render). Decorative: the name
          is the label span below, which `aria-labelledby` points at — so the
          collapsed rail, where that label is display:none, still names every
          link. This replaced the two-letter monogram that stood in for it. */}
      <RailIcon to={to} />
      <span id={labelId} className="ne-rail__nodelabel">
        {label}
      </span>
      {/* The transit dot is a REDUNDANT channel (law 4 cl. 6): the fact it
            depicts — a governed event landed on this stage — is a row in the
            audit ledger. It carries no text and is hidden from AT. */}
      {transit !== null ? (
        <span key={transit} className="ne-rail__transit" aria-hidden="true" />
      ) : null}
      {to === "/approvals" ? <ApprovalsBadge collapsed={collapsed} id={badgeId} /> : null}
    </TooltipNavLink>
  );
}

export function NavRail({ collapsed = false }: { collapsed?: boolean }) {
  const principal = usePrincipal();
  const sections = railSections(NAV_SECTIONS, entitlementsOf(principal));
  const anyPending = sections.some((section) =>
    section.kind === "group"
      ? section.slots.some((s) => s.kind === "pending")
      : section.kind === "pending",
  );
  // §4.3 — the rail transit pulse reads the shared audit poll's CURRENT batch
  // and nothing else. `transits` is a single-poll pulse: [] on first load (no
  // fabricated activity for events that merely already existed — law 4 cl. 1,
  // enforced in useAuditPoll and inherited here by rendering from it
  // verbatim), one entry per UNIQUE live route in a fresh batch
  // (TRANSIT_ROUTES), and [] again on the next quiet poll, which
  // unmounts the long-finished dot: a rail at rest holds zero animation
  // elements. No pulse state is kept here to get wrong.
  const { transits, latestSeq } = useAuditPoll();
  const transitFor = (to: string) =>
    transits.includes(to) && latestSeq !== null ? latestSeq : null;

  return (
    <TooltipArbiter>
      <nav className="ne-rail" aria-label="Primary">
        <TooltipNavLink
          to="/"
          className="ne-rail__wordmark"
          collapsed={collapsed}
          label="Neutron Enterprise"
          ariaLabel="Neutron Enterprise"
        >
          <NeutronMark size={22} />
          <span className="ne-rail__nodelabel">Neutron Enterprise</span>
        </TooltipNavLink>
        <DeploymentName />

        <div className="ne-rail__nav">
          {anyPending ? (
            <span className="ne-visually-hidden" role="status">
              Loading navigation
            </span>
          ) : null}
          {sections.map((section) =>
            section.kind === "group" ? (
              <div className="ne-rail__group" key={section.label}>
                <span className="ne-rail__grouplabel">{section.label.toUpperCase()}</span>
                {section.slots.map((slot) => (
                  <RailSlotNode
                    key={slot.to}
                    slot={slot}
                    collapsed={collapsed}
                    transit={transitFor(slot.to)}
                  />
                ))}
              </div>
            ) : (
              // A direct-link section (Home, Approvals): a bare node, no group
              // label. Home needs `end` so it is not "active" on every route.
              <RailSlotNode
                key={section.to}
                slot={section}
                collapsed={collapsed}
                end={section.kind === "link" && section.label === "Home"}
                transit={transitFor(section.to)}
              />
            ),
          )}
        </div>

        <div className="ne-rail__foot">
          <RailLink label={NAV_FOOT.label} to={NAV_FOOT.to} collapsed={collapsed} />
        </div>
      </nav>
    </TooltipArbiter>
  );
}
