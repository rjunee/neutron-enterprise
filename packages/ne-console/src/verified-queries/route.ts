// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

export const VERIFIED_QUERIES_PATH = "/verified-queries";

export function selectedTemplateFrom(input: URLSearchParams | string): string | null {
  const params = typeof input === "string" ? new URLSearchParams(input) : input;
  const value = params.get("template");
  return value === null || value === "" ? null : value;
}

export function withSelectedTemplate(
  current: URLSearchParams | string,
  templateId: string | null,
): URLSearchParams {
  const params = new URLSearchParams(typeof current === "string" ? current : current.toString());
  if (templateId === null || templateId === "") params.delete("template");
  else params.set("template", templateId);
  return params;
}

export function verifiedQueryHref(templateId: string): string {
  const params = new URLSearchParams({ template: templateId });
  return `${VERIFIED_QUERIES_PATH}?${params.toString()}`;
}

/** The page's two views: the approved-query workspace and the governed Tier-3
 * record browser. Anything unrecognized falls back to "queries" (never a 404 —
 * the param is presentation state). */
export type VerifiedQueriesView = "queries" | "browse";

export function viewFrom(input: URLSearchParams | string): VerifiedQueriesView {
  const params = typeof input === "string" ? new URLSearchParams(input) : input;
  return params.get("view") === "browse" ? "browse" : "queries";
}

export function browseClassFrom(input: URLSearchParams | string): string | null {
  const params = typeof input === "string" ? new URLSearchParams(input) : input;
  const value = params.get("cls");
  return value === null || value === "" ? null : value;
}

export function browseRecordFrom(input: URLSearchParams | string): string | null {
  const params = typeof input === "string" ? new URLSearchParams(input) : input;
  const value = params.get("record");
  return value === null || value === "" ? null : value;
}

export function withBrowseState(
  current: URLSearchParams | string,
  state: { view: VerifiedQueriesView; cls?: string | null; record?: string | null },
): URLSearchParams {
  const params = new URLSearchParams(typeof current === "string" ? current : current.toString());
  if (state.view === "browse") params.set("view", "browse");
  else {
    params.delete("view");
    params.delete("cls");
    params.delete("record");
  }
  if (state.cls !== undefined) {
    if (state.cls === null || state.cls === "") params.delete("cls");
    else params.set("cls", state.cls);
  }
  if (state.record !== undefined) {
    if (state.record === null || state.record === "") params.delete("record");
    else params.set("record", state.record);
  }
  return params;
}

/** Deep link into the Tier-3 record browser (used by the Ontology page roster). */
export function t3BrowseHref(className?: string): string {
  const params = new URLSearchParams({ view: "browse" });
  if (className !== undefined && className !== "") params.set("cls", className);
  return `${VERIFIED_QUERIES_PATH}?${params.toString()}`;
}
