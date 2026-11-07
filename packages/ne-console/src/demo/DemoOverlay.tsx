// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE GUIDED TOUR — the panel.
//
// AN OVERLAY, NEVER PAGE CONTENT (§4). The tour was once the body of the home
// route: the product opened on its own walkthrough. Then it became an in-flow
// panel on that route, which is the same mistake with fewer pixels — the home
// screen is where an approver starts a decision, and a walkthrough pushed into
// that flow is still the first thing they have to get past. It is now what every
// product ships: a dismissible overlay, raised once on a genuine first run, and
// reopened from the account menu.
//
// Two presentations, one body:
//   first run   raised unprompted, ONCE, on the home screen only, for a key with
//               no queue waiting on it. It introduces itself and says plainly
//               that dismissing it is permanent.
//   summoned    the same stations, opened from the account menu, on any route.
//
// Dismissing is ONE action in both — it closes and it persists (tourStorage.ts).
// Escape does exactly what the button does, so the keyboard path is not weaker.
//
// Nothing here is simulated: every link is a route the console registers (the
// station registry's own test holds that against the router), and the egress
// figure is read live from this deployment rather than scripted.
//
// THE BODY IS A BRIEF, NOT A PARAGRAPH. Each station renders as four labelled
// lines — who does this job, what the job is, the click path, and what they
// should see — because the reader is meant to perform it, and a block of prose
// is what a feature tour looks like.

import { useEffect } from "react";
import { Link, useLocation } from "react-router";
import { usePosture } from "../posture/PostureContext";
import { formatCount } from "../primitives/format";
import { useDemo } from "./DemoContext";
import { STATIONS } from "./stations";
import type { StationDef } from "./stations";
import "./demo.css";

/** Guard matchMedia (jsdom / older engines). */
function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

/** True when a keydown originated inside an editable field (ignore nav there). */
function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  return target.isContentEditable;
}

/**
 * THE DEPLOYMENT'S OWN EGRESS FIGURE, rendered inside the station that talks
 * about egress — so a script and a readout can never disagree again. A refused
 * or unreachable posture read renders as what it is; neither is a figure.
 */
function LiveEgress() {
  const { status, posture } = usePosture();
  const hosts = posture?.egress_allowlist ?? null;
  return (
    <div className="ne-demo__live">
      <span className="ne-demo__livelabel">External hosts allowed on this deployment</span>
      <span className="ne-demo__livevalue">
        {status === "refused"
          ? "Not visible to your role"
          : hosts === null
            ? status === "error"
              ? "Couldn’t load"
              : "Loading…"
            : formatCount(hosts.length)}
      </span>
      {hosts !== null && hosts.length > 0 ? (
        <span className="ne-demo__livehosts ne-mono">{hosts.join(", ")}</span>
      ) : null}
    </div>
  );
}

export interface DemoOverlayProps {
  /** Station list — a test seam; defaults to the shipped registry. */
  stations?: readonly StationDef[];
}

export function DemoOverlay({ stations = STATIONS }: DemoOverlayProps) {
  const { open, mode } = useDemo();
  const { pathname } = useLocation();

  // THE UNPROMPTED PANEL IS SCOPED TO THE HOME SCREEN. On a working route the
  // reader came for the route; a walkthrough over it is an interruption. A
  // panel someone ASKED for is a different thing, and opens anywhere.
  const shown = open && (mode === "summoned" || pathname === "/");

  // Flag the document while the panel is up so the page reserves height beneath
  // it — without this a fixed panel can cover a control the page has no room to
  // scroll clear of, which is exactly what happened to the composer on Ask.
  useEffect(() => {
    if (typeof document === "undefined") return;
    const root = document.documentElement;
    if (!shown) {
      delete root.dataset.neTour;
      return;
    }
    root.dataset.neTour = "open";
    return () => {
      delete root.dataset.neTour;
    };
  }, [shown]);

  if (!shown) return null;
  if (stations.length === 0) return null;
  return <TourPanel stations={stations} />;
}

function TourPanel({ stations }: { stations: readonly StationDef[] }) {
  const { mode, index, checks, checkoffStatus, next, prev, goTo, dismiss } = useDemo();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // A surface closer to the user (the detail pane's document-level Esc,
      // the palette) marks the event handled; the tour must not also act on it.
      if (e.defaultPrevented) return;
      if (isEditableTarget(e.target)) return;
      if (e.key === "ArrowRight") {
        e.preventDefault();
        next();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        prev();
      } else if (e.key === "Escape") {
        dismiss();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [next, prev, dismiss]);

  const reduced = prefersReducedMotion();
  const station = stations[Math.min(index, stations.length - 1)] ?? stations[0]!;
  const checkedSeq = checks[station.id];
  const firstRun = mode === "first-run";

  return (
    <div
      className={`ne-demo ${reduced ? "ne-demo--reduced" : "ne-demo--motion"}`}
      role="dialog"
      aria-modal="false"
      aria-label="guided tour"
    >
      {/* The dismiss control is the FIRST focusable element in the panel. It used
          to sit at the end of the panel's own scroll, roughly 40th in tab order:
          the single most common day-two action could not be found. */}
      <div className="ne-demo__bar">
        <span className="ne-demo__barlabel">
          {firstRun ? "First run · Guided tour" : "Guided tour"} · Station {station.numeral} of{" "}
          {stations.length}
        </span>
        <button
          type="button"
          className="ne-btn ne-btn--quiet"
          onClick={dismiss}
          aria-label={firstRun ? "dismiss the guided tour for good" : "close the guided tour"}
        >
          {firstRun ? "Dismiss — don’t show again" : "Close"}
        </button>
      </div>

      {firstRun ? (
        <p className="ne-demo__intro">
          {stations.length} stations, end to end. Dismiss it and it will not open again in this
          browser; reopen it any time from <strong>Guided tour</strong> in the account menu. Escape
          closes it too.
        </p>
      ) : null}

      <div className="ne-demo__scroll">
        <div className="ne-demo__main">
          <div className="ne-demo__numeral" aria-hidden="true">
            {station.numeral}
          </div>

          <div className="ne-demo__body">
            <h2 className="ne-demo__heading">{station.title}</h2>
            {/* WHO / THE JOB / DO THIS / YOU SHOULD SEE. Four labelled lines,
                because a station is a task someone performs and a paragraph of
                narration is the thing this tour stopped being. */}
            <dl className="ne-demo__brief">
              <dt className="ne-demo__brieflabel">Who</dt>
              <dd className="ne-demo__briefvalue">{station.role}</dd>
              <dt className="ne-demo__brieflabel">The job</dt>
              <dd className="ne-demo__briefvalue">{station.job}</dd>
              <dt className="ne-demo__brieflabel">Do this</dt>
              <dd className="ne-demo__briefvalue">{station.path}</dd>
              <dt className="ne-demo__brieflabel">You should see</dt>
              <dd className="ne-demo__briefvalue ne-demo__briefvalue--dim">{station.see}</dd>
            </dl>
            {station.liveReadout === "egress" ? <LiveEgress /> : null}
            <div className="ne-demo__links">
              {station.links.map((link) => (
                <Link key={link.to + link.label} to={link.to} className="ne-demo__link">
                  {link.label}
                </Link>
              ))}
            </div>
            {checkedSeq === undefined ? null : (
              <p className="ne-demo__checked">Checked off at entry {formatCount(checkedSeq)}.</p>
            )}
          </div>
        </div>
      </div>

      <div className="ne-demo__strip">
        <div className="ne-demo__stations">
          {stations.map((s, i) => {
            const done = checks[s.id] !== undefined;
            const isCurrent = i === index;
            return (
              <button
                key={s.id}
                type="button"
                className={[
                  "ne-demo__stationbtn",
                  isCurrent ? "ne-demo__stationbtn--current" : "",
                  done ? "ne-demo__stationbtn--done" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                aria-current={isCurrent ? "step" : undefined}
                aria-label={`station ${s.numeral} ${s.title}${done ? " — checked off" : ""}`}
                onClick={() => goTo(i)}
              >
                {s.numeral}
              </button>
            );
          })}
        </div>

        {/* Stated ONCE, about the key rather than about each station: check-off
            reads the audit log, and a key without it will never see a check. */}
        {checkoffStatus === "requires-ops-key" ? (
          <span className="ne-demo__checknote">
            Stations check off against the audit log, which your role doesn’t include. No station
            will check off.
          </span>
        ) : null}

        <div className="ne-demo__controls">
          <button
            type="button"
            className="ne-btn ne-btn--quiet"
            onClick={prev}
            disabled={index === 0}
            aria-label="previous station"
          >
            Previous
          </button>
          <button
            type="button"
            className="ne-btn ne-btn--quiet"
            onClick={next}
            disabled={index >= stations.length - 1}
            aria-label="next station"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}
