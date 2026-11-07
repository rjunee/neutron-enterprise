// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The ONE renderer for a metadata value (metadataView.ts): scalars as text,
// structures as labelled rows — shared by Search's result pane and Ask's
// source pane so the same provenance stamp cannot render as fields on one
// screen and as a JSON blob one screen over.

import { metadataView, type MetadataView } from "./metadataView";

export function MetadataValueView({ view }: { view: MetadataView }) {
  if (view.kind === "scalar") return <>{view.text}</>;
  return (
    <dl className="ne-search__meta ne-search__meta--nested">
      {view.fields.map(([key, inner]) => (
        <div className="ne-search__metarow" key={key}>
          <dt>{key}</dt>
          <dd>
            <MetadataValueView view={inner} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** Convenience: a raw served value straight to its rendering. */
export function MetadataValueOf({ value }: { value: unknown }) {
  return <MetadataValueView view={metadataView(value)} />;
}
