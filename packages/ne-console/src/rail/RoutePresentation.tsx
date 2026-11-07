// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useLayoutEffect, useRef } from "react";
import { useLocation } from "react-router";
import { NAV_FOOT, NAV_SECTIONS } from "./navSections";

export interface RoutePresentationValue {
  readonly section: string | null;
  readonly title: string;
}

/** One shared source for route breadcrumb, browser title and navigation focus. */
function presentationFor(pathname: string): RoutePresentationValue | null {
  for (const section of NAV_SECTIONS) {
    if (section.items) {
      const item = section.items.find((candidate) => candidate.to === pathname);
      if (item) return { section: section.label, title: item.label };
    } else if (section.to === pathname) {
      return { section: null, title: section.label };
    }
  }
  if (pathname === "/agents" || pathname.startsWith("/agents/")) {
    return { section: "Execution", title: "Agents" };
  }
  return NAV_FOOT.to === pathname ? { section: null, title: NAV_FOOT.label } : null;
}

export function RoutePresentation() {
  const location = useLocation();
  const previousPathname = useRef<string | null>(null);
  const presentation = presentationFor(location.pathname);
  const browserTitle =
    presentation === null || presentation.title === "Home"
      ? "Neutron Enterprise"
      : `${presentation.title} · Neutron Enterprise`;

  useEffect(() => {
    document.title = browserTitle;
  }, [browserTitle]);

  useLayoutEffect(() => {
    const isInitialMount = previousPathname.current === null;
    const isRouteChange = previousPathname.current !== location.pathname;
    previousPathname.current = location.pathname;
    // The initial mount (a plain page load or reload) is not a navigation —
    // moving focus here would paint the focus-visible outline around the
    // main pane before the user has done anything. Only a genuine, later
    // route change should pull focus to #ne-main.
    if (isInitialMount) return;
    if (isRouteChange) {
      document.getElementById("ne-main")?.focus();
    }
  }, [location.pathname]);

  if (presentation === null) return null;
  return (
    <nav className="ne-route-presentation" aria-label="Breadcrumb">
      {presentation.section === null ? null : (
        <>
          <span>{presentation.section}</span>
          <span aria-hidden="true">/</span>
        </>
      )}
      <span aria-current="page">{presentation.title}</span>
    </nav>
  );
}
