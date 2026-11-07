// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §1.4 — PROGRESSIVE DISCLOSURE IS THE DETAIL PANE, ALWAYS AVAILABLE.
//
// A right-side panel, 420px, resizable, Esc closes, focus-trapped. It NEVER
// navigates away from the list: the list keeps its scroll position and its
// selection, so "open a row, read it, close it, open the next" costs two keys
// instead of two page loads. That loop is the second week of use.
//
// This replaces the global Inspect mode and the evidence drawer (§20): a mode
// you have to switch on before detail exists is a mode most people never
// discover, and detail that only appears in a mode is detail that is missing.
//
// The focus trap is a real one — Tab and Shift+Tab cycle inside the pane, focus
// enters on open and RETURNS to the element that opened it on close. A pane you
// can Tab out of silently drops the keyboard user back onto the list behind it.

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode, PointerEvent as ReactPointerEvent } from "react";
import { SURFACE_PANE } from "./surface";

const MIN_WIDTH = 320;
const MAX_WIDTH = 720;
const DEFAULT_WIDTH = 420;

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function DetailPane({
  title,
  subtitle,
  onClose,
  children,
  footer,
  returnFocusTo,
  focusKey,
}: {
  /** The object this pane is about. Sentence case. */
  title: ReactNode;
  /** One optional line under the title — an id, a state, a timestamp. */
  subtitle?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  /** Pinned to the pane's foot: the submit control, when there is one. */
  footer?: ReactNode;
  /** Explicit current opener for a pane whose subject can change in place. */
  returnFocusTo?: HTMLElement | null;
  /** Refocus the pane when its selected subject changes without remounting. */
  focusKey?: string;
}) {
  const paneRef = useRef<HTMLDivElement | null>(null);
  const returnFocusRef = useRef<Element | null>(null);
  const hasExplicitReturnFocus = useRef(returnFocusTo !== undefined);
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  const headingId = useId();

  const focusPane = useCallback(() => {
    const first = paneRef.current?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? paneRef.current)?.focus();
  }, []);

  useEffect(() => {
    if (!hasExplicitReturnFocus.current) returnFocusRef.current = document.activeElement;
    focusPane();
    return () => {
      const back = returnFocusRef.current;
      if (back instanceof HTMLElement && document.contains(back)) back.focus();
    };
  }, [focusPane]);

  // A layout effect makes an in-place subject switch one atomic interaction:
  // the latest opener is authoritative and focus is back inside the pane
  // before the browser can paint or accept another input.
  useLayoutEffect(() => {
    if (returnFocusTo !== undefined) returnFocusRef.current = returnFocusTo;
    if (focusKey !== undefined) focusPane();
  }, [focusKey, focusPane, returnFocusTo]);

  // "Esc closes" (header comment) must hold WHEREVER focus is. The pane is
  // aria-modal="false" — a click on the list behind it parks focus outside the
  // pane, and an Esc handler that lives only on the pane's own keydown then
  // goes dead: the owner sat in front of exactly that, a pane he could not
  // close. Document-level, so the promise survives a stray click. An inner
  // surface that owns its own Escape (the decision confirm, a filter box with
  // text, the command palette) declares so via stopPropagation/preventDefault
  // and this listener never sees or acts on the event.
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);
  useEffect(() => {
    function onDocumentKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      closeRef.current();
    }
    document.addEventListener("keydown", onDocumentKeyDown);
    return () => document.removeEventListener("keydown", onDocumentKeyDown);
  }, []);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const nodes = [...(paneRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
      if (nodes.length === 0) return;
      const first = nodes[0]!;
      const last = nodes[nodes.length - 1]!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    },
    [onClose],
  );

  // The resize grip. Pointer capture, so a drag that leaves the 8px grip keeps
  // resizing instead of stopping dead at the edge of the handle.
  const onGripDown = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const grip = event.currentTarget;
    grip.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startWidth = paneRef.current?.getBoundingClientRect().width ?? DEFAULT_WIDTH;
    function move(e: PointerEvent) {
      const next = startWidth + (startX - e.clientX);
      setWidth(Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, next)));
    }
    function up() {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", up);
    }
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
  }, []);

  return (
    <div
      // §4.1: the pane arrives from the edge it is docked to, so it reads as
      // detail OF the row it came from rather than as a page change. The
      // animation dresses a surface that is ALREADY interactive — content is
      // in the DOM and focus has moved in this same commit (design §7, input
      // path). Nothing below waits on it.
      className={`ne-detail ${SURFACE_PANE}`}
      role="dialog"
      aria-modal="false"
      aria-labelledby={headingId}
      ref={paneRef}
      tabIndex={-1}
      style={{ width: `${width}px` }}
      onKeyDown={onKeyDown}
    >
      <div
        className="ne-detail__grip"
        role="separator"
        aria-label="Resize detail pane"
        aria-orientation="vertical"
        onPointerDown={onGripDown}
      />
      <header className="ne-detail__head">
        <div>
          <h2 className="ne-detail__title" id={headingId}>
            {title}
          </h2>
          {subtitle ? <p className="ne-detail__subtitle">{subtitle}</p> : null}
        </div>
        <button type="button" className="ne-detail__close" aria-label="Close" onClick={onClose}>
          <span aria-hidden="true">×</span>
        </button>
      </header>
      <div className="ne-detail__body">{children}</div>
      {footer ? <footer className="ne-detail__foot">{footer}</footer> : null}
    </div>
  );
}

/** A labelled field inside a detail pane — the pane's whole layout vocabulary,
 * so no screen invents a second one. */
export function DetailField({
  label,
  children,
  mono,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="ne-detail__field">
      <dt className="ne-detail__label">{label}</dt>
      <dd className={`ne-detail__value${mono ? " ne-mono" : ""}`}>{children}</dd>
    </div>
  );
}

export function DetailFields({ children }: { children: ReactNode }) {
  return <dl className="ne-detail__fields">{children}</dl>;
}
