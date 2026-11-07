// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The docked record pane (screens/graph.html: "the record pane is docked, not
// a route change — node click fills it, the canvas never moves"). This is the
// floor's third column, not a modal: no focus trap, no resize grip, `<aside>`
// rather than `role="dialog"` — it sits beside the canvas at all times a node
// is selected. It still moves focus to its own Close button on open/subject
// change and returns focus to the opener on close, and Escape still closes it,
// because those are accessibility guarantees worth keeping even for an
// always-visible pane.

import { useEffect, useLayoutEffect, useRef } from "react";
import { Link } from "react-router";
import { Empty, Loading, LoadError, Refused } from "../../primitives/states";
import type { EntityRecordData } from "../useEntityRecord";
import type { ReadState } from "../readState";
import { GraphPathsFinder } from "./GraphPathsFinder";
import type { GraphEntityHref } from "./GraphPathsFinder";
import { GraphRecordContent, OwnedEntityRecordRead, RecordIdentity } from "./GraphRecord";
import "./graphRecord.css";

export interface GraphQuickDrawerProps {
  readonly entityKey: string;
  readonly entityHref: GraphEntityHref;
  readonly returnFocusTo?: HTMLElement | null;
  readonly onClose: () => void;
}

export function GraphQuickDrawer({
  entityKey,
  entityHref,
  returnFocusTo,
  onClose,
}: GraphQuickDrawerProps) {
  return (
    <OwnedEntityRecordRead entityKey={entityKey}>
      {(read) => (
        <GraphQuickDrawerView
          {...read}
          entityKey={entityKey}
          entityHref={entityHref}
          returnFocusTo={returnFocusTo}
          onClose={onClose}
        />
      )}
    </OwnedEntityRecordRead>
  );
}

function GraphQuickDrawerView({
  entityKey,
  entityHref,
  returnFocusTo,
  onClose,
  state,
  retry,
}: GraphQuickDrawerProps & {
  state: ReadState<EntityRecordData>;
  retry: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<Element | null>(null);
  const hasExplicitReturnFocus = useRef(returnFocusTo !== undefined);

  // Mount once: remember the opener (unless the host names one explicitly)
  // and hand focus to the pane. The cleanup returns focus there when this
  // pane instance finally unmounts (the host closes it).
  useEffect(() => {
    if (!hasExplicitReturnFocus.current) returnFocusRef.current = document.activeElement;
    closeRef.current?.focus();
    return () => {
      const back = returnFocusRef.current;
      if (back instanceof HTMLElement && document.contains(back)) back.focus();
    };
  }, []);

  // A selection can move to a different entity without this component
  // unmounting (the host re-renders the same slot with a new entityKey). Each
  // such change is its own atomic interaction: the latest opener is
  // authoritative and focus is back in the pane before the browser paints.
  useLayoutEffect(() => {
    if (returnFocusTo !== undefined) returnFocusRef.current = returnFocusTo;
    closeRef.current?.focus();
  }, [entityKey, returnFocusTo]);

  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);
  useEffect(() => {
    function onDocumentKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      onCloseRef.current();
    }
    document.addEventListener("keydown", onDocumentKeyDown);
    return () => document.removeEventListener("keydown", onDocumentKeyDown);
  }, []);

  const node = state.phase === "ok" ? state.value.node : null;
  const title = node?.display_name ?? "Entity record";
  return (
    <aside className="ne-record" aria-label={title}>
      <div className="ne-record__head">
        <div>
          <h2 className="ne-record__title">{title}</h2>
          {node === null ? null : <RecordIdentity node={node} />}
        </div>
        <button
          ref={closeRef}
          type="button"
          className="ne-record__close"
          aria-label="Close"
          onClick={onClose}
        >
          <span aria-hidden="true">×</span>
        </button>
      </div>
      <div className="ne-record__body">
        {state.phase === "loading" ? (
          <Loading things="the entity record" />
        ) : state.phase === "refused" ? (
          <Refused thing="the knowledge graph" page="Graph" />
        ) : state.phase === "off" ? (
          <Empty message="The knowledge graph isn't enabled on this deployment." />
        ) : state.phase === "error" ? (
          <LoadError things="the entity record" onRetry={retry} />
        ) : state.value.node === null ? (
          <Empty message="No visible entity record was returned." />
        ) : (
          <GraphRecordContent
            data={{ ...state.value, node: state.value.node }}
            entityHref={entityHref}
            quick
          />
        )}
      </div>
      <div className="ne-record__foot">
        <Link className="ne-btn" to={entityHref(entityKey, "record")}>
          Open full record
        </Link>
        <GraphPathsFinder key={entityKey} from={entityKey} entityHref={entityHref} />
      </div>
    </aside>
  );
}
