// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// A PRINCIPAL, RENDERED FOR A READER — named only when the governed directory
// derives it from a server-owned record reference the reader may already see.
//
// `c21456b9-c8dc-4458-b393-bc3230db0081` was the ACTOR column on /audit, the
// "who asked" line on every decision sheet, and the front page's "since your last
// visit" feed. The brief lists a raw UUID on the front page as a KNOWN DEFECT
// that the redesign fixed; it was not fixed, it was relocated.
//
// WHAT THIS DOES NOT DO. It never asks to name an arbitrary id and never invents
// a name. Unknown, blank, refused or unavailable resolutions retain the stable
// short-id fallback; the full identifier remains under Inspect/title.

import type { DirectoryEntry, PrincipalReference } from "../directory/DirectoryContext";
import { usePrincipalName } from "../directory/DirectoryContext";
import { useInspect } from "./InspectContext";

/** A v4-shaped UUID. Ids like `per-0038` are already short and stay verbatim. */
const UUIDISH = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The short form a reader can hold in their head, or the id itself when it is
 * already short. Pure + exported for its own test. */
export function shortPrincipal(id: string): string {
  if (UUIDISH.test(id)) return id.slice(0, 8);
  return id;
}

export function displayPrincipal(id: string, entry: DirectoryEntry | undefined): string {
  if (entry !== undefined) return `${entry.name}${entry.removed ? " (removed)" : ""}`;
  return shortPrincipal(id);
}

export function PrincipalLabel({ id, reference }: { id: string; reference?: PrincipalReference }) {
  const { inspect } = useInspect();
  const entry = usePrincipalName(id, reference);
  if (entry !== undefined) {
    return (
      <span className="ne-principal" title={id}>
        <span>{displayPrincipal(id, entry)}</span>
        {inspect ? <span className="ne-mono">{id}</span> : null}
      </span>
    );
  }
  const short = shortPrincipal(id);
  if (inspect || short === id) {
    return (
      <span className="ne-mono" title={id}>
        {id}
      </span>
    );
  }
  return (
    <span className="ne-principal" title={id}>
      <span className="ne-mono">{short}</span>
      <span className="ne-principal__note">
        principal id, shortened — press I to show it in full
      </span>
    </span>
  );
}
