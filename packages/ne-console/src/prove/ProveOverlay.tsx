// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// PULL-TO-PROVE IS DELETED (§20). The overlay reconstructed an artifact's
// provenance as an eight-stage "evidence thread" in which seven stages read
// "not available in this deployment" — a modal whose content was mostly an
// account of what it could not show. Progressive disclosure is the detail pane
// now, and a citation opens the source pane on Ask.
//
// WHAT SURVIVES HERE, AND WHY. The authed shell still mounts <ProveProvider> and
// <ProveOverlay>; App.tsx belongs to the shell rebuild and is not this unit's to
// edit, so removing the files outright would break the build for a change this
// unit cannot land. These two are inert: the provider passes its children
// through, the overlay renders nothing, and there is no code path back to the
// modal. They go with the shell's next pass at its own imports.

import type { ReactNode } from "react";

export function ProveProvider({ children }: { children: ReactNode }) {
  return <>{children}</>;
}

export function ProveOverlay() {
  return null;
}
