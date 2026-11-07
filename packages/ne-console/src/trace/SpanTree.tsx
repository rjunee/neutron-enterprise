// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §13b — the ordered span tree with kind-typed detail on selection.
//
// One row per span, in the server's order, as a conventional vertical timeline.
// A row names what the span IS in words — "Model call", "Policy decision" — not
// its wire kind: a reader who has to learn eight three-letter codes to read a
// run is reading the emitter's vocabulary instead of the run.
//
// THE ROWS ARE NOT SORTABLE, AND THAT IS THE POINT: the order is the order the
// run happened in, with each span nested under the one that caused it. A sort
// control here would offer to destroy the only structure the tree carries.
//
// THIN CLIENT (C7): every sequence, duration, lane and outcome on screen is a
// server field, verbatim. There is no browser clock, progress estimate, or
// client-derived timestamp.

import { useCallback, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent } from "react";
import type { TraceNodeView, TraceRollupView } from "../api/types";
import { formatDuration } from "../primitives/format";
import {
  flatten,
  kindLabel,
  laneOf,
  laneTier,
  nodeName,
  routingFact,
  rowFact,
  timelineFacts,
} from "./format";
import { traceEventAnchor } from "./traceLink";

interface SpanTreeBaseProps {
  nodes: readonly TraceNodeView[];
  /** Retained for existing embedded readers; the timeline never derives progress from it. */
  rollup?: TraceRollupView;
  selectedId: string | null;
}

export type SpanTreeProps = SpanTreeBaseProps &
  (
    | {
        capability: "interactive";
        onSelect: (node: TraceNodeView, opener: HTMLElement) => void;
      }
    | {
        capability: "readOnly";
        onSelect?: never;
      }
  );

export function SpanTree(props: SpanTreeProps) {
  const { nodes, selectedId } = props;
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [focused, setFocused] = useState(0);
  const treeRef = useRef<HTMLDivElement | null>(null);
  // A rollup is data, never an interaction contract. Embedded readers must
  // declare their capability explicitly so adding or removing summary data
  // cannot silently turn event selection off.
  const selectable = props.capability === "interactive";

  const toggle = useCallback((eventId: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(eventId)) next.delete(eventId);
      else next.add(eventId);
      return next;
    });
  }, []);

  const rows = flatten(nodes, collapsed);
  const active = rows.length === 0 ? 0 : Math.min(focused, rows.length - 1);

  const focusRow = useCallback((index: number) => {
    setFocused(index);
    treeRef.current?.querySelectorAll<HTMLElement>("[role='treeitem']")[index]?.focus();
  }, []);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>, node: TraceNodeView, index: number) => {
      switch (event.key) {
        case "ArrowDown":
        case "j":
          event.preventDefault();
          focusRow(Math.min(rows.length - 1, index + 1));
          break;
        case "ArrowUp":
        case "k":
          event.preventDefault();
          focusRow(Math.max(0, index - 1));
          break;
        case "ArrowRight":
          if (node.children.length > 0 && collapsed.has(node.event_id)) {
            event.preventDefault();
            toggle(node.event_id);
          } else if (node.children.length > 0) {
            event.preventDefault();
            focusRow(Math.min(rows.length - 1, index + 1));
          }
          break;
        case "ArrowLeft":
          if (node.children.length > 0 && !collapsed.has(node.event_id)) {
            event.preventDefault();
            toggle(node.event_id);
          } else {
            const depth = rows[index]?.depth ?? 0;
            for (let candidate = index - 1; candidate >= 0; candidate -= 1) {
              if (rows[candidate]?.depth === depth - 1) {
                event.preventDefault();
                focusRow(candidate);
                break;
              }
            }
          }
          break;
        case "Home":
          event.preventDefault();
          focusRow(0);
          break;
        case "End":
          event.preventDefault();
          focusRow(rows.length - 1);
          break;
        case "Enter":
        case " ":
          if (!selectable) break;
          event.preventDefault();
          props.onSelect(node, event.currentTarget);
          break;
      }
    },
    [collapsed, focusRow, props, rows, selectable, toggle],
  );

  return (
    <div
      className="ne-trace__tree"
      role="tree"
      aria-label="Spans"
      data-readonly={!selectable || undefined}
      ref={treeRef}
    >
      {rows.length === 0 ? <p className="ne-trace__treeempty">No recorded events.</p> : null}
      {rows.map(({ node, depth }, index) => {
        const hasChildren = node.children.length > 0;
        const isCollapsed = collapsed.has(node.event_id);
        const selected = selectedId === node.event_id;
        const lane = laneOf(node);
        const tier = laneTier(lane, node.route?.tier ?? null);
        const duration = node.subagent?.duration_ms ?? null;
        const label = kindLabel(node.kind);
        // The row's result fact, from served counts/enums only (format.ts).
        // A served ZERO renders visually distinct: a fetch that matched
        // nothing must be catchable at a glance, not by reading raw JSON.
        const fact = rowFact(node);
        const typedFacts = timelineFacts(node);
        const state = node.pdp?.effect ?? node.approval?.decision ?? node.outcome ?? null;
        // A model call names the TASK it served next to the model that served
        // it: the run's routing is the thing a reader is here to see, and it
        // should not require opening every span to read. Server field, verbatim.
        const task = node.model_call?.task ?? null;
        // A fallback is marked ON THE ROW, because a fallback that only shows
        // up in a detail pane is a fallback nobody notices. Absent/unknown
        // positions get no mark — the mark states a fact, not its negation.
        const routing = routingFact(node.model_call);

        return (
          <div
            key={node.event_id}
            id={traceEventAnchor(node.event_id)}
            role="treeitem"
            aria-level={depth + 1}
            aria-selected={selectable ? selected : undefined}
            aria-expanded={hasChildren ? !isCollapsed : undefined}
            tabIndex={index === active ? 0 : -1}
            className={`ne-trace__span${selectable ? " ne-trace__span--interactive" : ""}${selected ? " ne-trace__span--selected" : ""}`}
            style={{ "--ne-span-depth": depth } as CSSProperties}
            data-kind={node.kind}
            data-lane-tier={tier}
            data-state={state?.toLowerCase()}
            onFocus={() => setFocused(index)}
            onClick={
              selectable
                ? (event) => {
                    event.currentTarget.focus();
                    props.onSelect(node, event.currentTarget);
                  }
                : undefined
            }
            onKeyDown={(event) => onKeyDown(event, node, index)}
          >
            <span className="ne-trace__rail">
              <span className="ne-trace__marker" aria-hidden="true" />
              {hasChildren ? (
                <button
                  type="button"
                  tabIndex={-1}
                  className="ne-trace__disclosurebtn"
                  aria-label={`${isCollapsed ? "Expand" : "Collapse"} ${nodeName(node)}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    // Pointer activation focuses the nested disclosure button.
                    // Return focus to its owning treeitem before the row set
                    // changes so Arrow/Home/End work immediately afterward.
                    event.currentTarget.closest<HTMLElement>("[role='treeitem']")?.focus();
                    toggle(node.event_id);
                  }}
                  onKeyDown={(event) => event.stopPropagation()}
                >
                  <span aria-hidden="true">{isCollapsed ? "▸" : "▾"}</span>
                </button>
              ) : null}
            </span>
            <span className="ne-trace__spancontent">
              <span className="ne-trace__spanhead">
                <span className="ne-trace__spankind">{label ?? "Event"}</span>
                <span className="ne-trace__spanseq">Sequence {node.seq}</span>
              </span>
              <span className="ne-trace__spanname">
                {nodeName(node)}
                {task === null || task === "" ? null : (
                  <span className="ne-trace__spantask">{task}</span>
                )}
                {routing?.fallback ? (
                  <span className="ne-trace__spanfallback" title={routing.text}>
                    fallback
                  </span>
                ) : null}
              </span>
              {typedFacts.length > 0 || fact !== null ? (
                <span className="ne-trace__spanfacts">
                  {typedFacts.map((item) => (
                    <span
                      className={`ne-trace__typedfact ne-trace__typedfact--${item.tone}`}
                      data-fact={item.key}
                      key={item.key}
                    >
                      <span className="ne-trace__typedfactlabel">{item.label}</span>
                      <span aria-hidden="true"> · </span>
                      <span className={item.mono ? "ne-mono" : undefined}>{item.value}</span>
                    </span>
                  ))}
                  {fact === null ? null : (
                    <span
                      className={`ne-trace__spanfact${fact.empty ? " ne-trace__spanfact--empty" : ""}`}
                    >
                      {fact.text}
                    </span>
                  )}
                </span>
              ) : null}
            </span>
            <span className="ne-trace__spanmeta">
              {lane ? <span className="ne-trace__spanlane ne-mono">{lane}</span> : null}
              {duration === null ? null : (
                <span className="ne-trace__spandur ne-num">{formatDuration(duration)}</span>
              )}
            </span>
          </div>
        );
      })}
    </div>
  );
}
