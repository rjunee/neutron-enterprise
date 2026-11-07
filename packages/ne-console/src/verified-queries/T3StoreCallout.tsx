// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The two-graphs signpost + Tier-3 class roster rendered on the OPERATIONAL
// graph surfaces (Ontology, Graph). The roster is the server-advertised closed
// enum on the reviewed `t3-records-in-class` template — never a console-invented
// class list; when it is not served, the callout says only what is true.

import { useEffect, useState } from "react";
import { useAuth } from "react-oidc-context";
import { Link } from "react-router";
import { useApiClient } from "../auth/useApiClient";
import { T3_CLASS_TEMPLATE_ID, t3ClassRoster } from "./T3Browser";
import { t3BrowseHref } from "./route";
import "./verifiedQueries.css";

type RosterState =
  { phase: "loading" } | { phase: "ok"; classes: string[] } | { phase: "unavailable" };

export function T3StoreCallout({ surface }: { surface: "ontology" | "graph" }) {
  // The signpost half of this callout is static; the roster half needs the
  // governed API client, which needs a live OIDC session. Render the static
  // half alone when no session context exists (defensive: some host screens
  // are testable without an auth provider) instead of crashing the host page.
  const auth = useAuth() as ReturnType<typeof useAuth> | undefined;
  if (auth === undefined || auth === null) {
    return <CalloutShell surface={surface} roster={{ phase: "unavailable" }} />;
  }
  return <AuthedCallout surface={surface} />;
}

function AuthedCallout({ surface }: { surface: "ontology" | "graph" }) {
  const client = useApiClient();
  const [roster, setRoster] = useState<RosterState>({ phase: "loading" });

  useEffect(() => {
    let owned = true;
    void client.verifiedTemplates().then(
      (catalog) => {
        if (!owned) return;
        const classes = catalog.enabled ? t3ClassRoster(catalog) : [];
        setRoster(classes.length > 0 ? { phase: "ok", classes } : { phase: "unavailable" });
      },
      () => {
        if (owned) setRoster({ phase: "unavailable" });
      },
    );
    return () => {
      owned = false;
    };
  }, [client]);

  return <CalloutShell surface={surface} roster={roster} />;
}

function CalloutShell({ surface, roster }: { surface: "ontology" | "graph"; roster: RosterState }) {
  const surfaceName = surface === "ontology" ? "model explorer" : "graph";
  return (
    <aside className="ne-t3-callout" aria-label="Tier-3 verified store">
      <p>
        <strong>Two graphs live in this deployment.</strong> This {surfaceName} shows the{" "}
        <strong>operational graph</strong> — records mirrored from connected systems, carrying those
        systems&rsquo; own permissions, freely traversable here. The{" "}
        <strong>Tier-3 verified store</strong> is separate: a hash-verified, versioned release that
        only pre-approved queries can read, where every row carries a receipt so a claim can be
        re-proved later. Browse it under{" "}
        <Link to={t3BrowseHref()}>Verified Queries → Browse records</Link>.
      </p>
      {roster.phase === "ok" ? (
        <ul className="ne-t3-callout__classes" aria-label="Verified store classes">
          {roster.classes.map((className) => (
            <li key={className}>
              <Link to={t3BrowseHref(className)}>{className}</Link>
            </li>
          ))}
        </ul>
      ) : roster.phase === "unavailable" ? (
        <p className="ne-t3-browser__note">
          The verified store&rsquo;s class roster is not served to this session (the{" "}
          <code>{T3_CLASS_TEMPLATE_ID}</code> template is absent or refused), so no class list is
          shown here.
        </p>
      ) : null}
    </aside>
  );
}
