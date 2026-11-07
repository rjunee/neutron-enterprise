// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-CONSOLE-ISSUER: the bootstrap gate proof. The OIDC tree initializes from the
// RUNTIME-fetched issuer (a probe reads it via useRuntimeConfig), never a baked
// constant; a config-fetch failure renders a role="alert" error and NEVER the
// authed app (fail-closed — no silent wrong-issuer login); Retry recovers.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { AppBootstrap, useRuntimeConfig } from "./AppBootstrap";
import { RUNTIME_CONFIG } from "./test/mocks/fixtures";
import { server } from "./test/mocks/server";

/** A probe that renders the issuer from the RUNTIME config — it can only render
 * once AppBootstrap has fetched /config.json and provided the context. */
function IssuerProbe() {
  const cfg = useRuntimeConfig();
  return <div data-testid="issuer">{cfg.issuer}</div>;
}

describe("AppBootstrap", () => {
  it("initializes the OIDC tree from the runtime-fetched issuer", async () => {
    render(
      <AppBootstrap>
        <IssuerProbe />
      </AppBootstrap>,
    );

    // Transient loading state before the fetch resolves.
    expect(screen.getByText("Loading configuration…")).toBeInTheDocument();

    // The probe renders the RUNTIME issuer, proving the tree initialized from the
    // fetched value, not a Vite-baked constant.
    expect(await screen.findByText(RUNTIME_CONFIG.issuer)).toBeInTheDocument();
  });

  it("fails closed: a config-fetch error renders an alert and never the app", async () => {
    server.use(http.get("*/config.json", () => HttpResponse.json({}, { status: 500 })));

    render(
      <AppBootstrap>
        <IssuerProbe />
      </AppBootstrap>,
    );

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByText(/sign-in is unavailable/i)).toBeInTheDocument();
    // The authed subtree NEVER rendered — no silent wrong-issuer login.
    expect(screen.queryByTestId("issuer")).not.toBeInTheDocument();
  });

  it("shows the viewer a sentence, never the client's own transport string", async () => {
    // THE FIRST SCREEN A VIEWER CAN SEE. This page renders the thrown error's
    // `message` full-viewport above a Retry button, and that message used to be
    // "runtime config fetch failed: HTTP 500" — a status code and a route, to
    // whoever happened to be in the room when the network hiccuped at load. The
    // transport-provenance guardian could not see it: it excludes src/api/ as
    // the seam, and this throw was inside it.
    server.use(http.get("*/config.json", () => HttpResponse.json({}, { status: 500 })));

    render(
      <AppBootstrap>
        <IssuerProbe />
      </AppBootstrap>,
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The server is unavailable.");
    expect(alert.textContent).not.toMatch(/HTTP/);
    expect(alert.textContent).not.toMatch(/\b5\d{2}\b/);
  });

  it("says the configuration is unusable when the server answered but the body is not", async () => {
    // A 200 with an unusable body is not a transport failure, and must not
    // borrow the sentence for one — the server answered.
    server.use(http.get("*/config.json", () => HttpResponse.json({ issuer: "" })));

    render(
      <AppBootstrap>
        <IssuerProbe />
      </AppBootstrap>,
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The deployment’s configuration isn’t usable.");
    expect(alert.textContent).not.toMatch(/issuer/);
  });

  // THE TWO FAILURES THAT WALKED PAST THE FIRST REPAIR. Neither has a string
  // the console wrote, so neither had a construction site to fix: the reader's
  // page simply rendered whatever the runtime threw, full-viewport.
  it("shows a sentence when the fetch REJECTS, not the engine's 'Failed to fetch'", async () => {
    server.use(http.get("*/config.json", () => HttpResponse.error()));

    render(
      <AppBootstrap>
        <IssuerProbe />
      </AppBootstrap>,
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The server couldn’t be reached.");
    expect(alert.textContent).not.toMatch(/Failed to fetch/);
    expect(screen.queryByTestId("issuer")).not.toBeInTheDocument();
  });

  it("shows a sentence when the body is HTML, not the parser's 'Unexpected token'", async () => {
    // An edge rewrite or an SPA fallback answering /config.json with index.html.
    server.use(
      http.get(
        "*/config.json",
        () =>
          new HttpResponse("<!doctype html><html><body>Bad Gateway</body></html>", {
            status: 200,
            headers: { "Content-Type": "text/html" },
          }),
      ),
    );

    render(
      <AppBootstrap>
        <IssuerProbe />
      </AppBootstrap>,
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The deployment’s configuration isn’t usable.");
    expect(alert.textContent).not.toMatch(/doctype|Unexpected token|JSON/i);
    expect(screen.queryByTestId("issuer")).not.toBeInTheDocument();
  });

  it("Retry recovers after the config becomes available", async () => {
    server.use(http.get("*/config.json", () => HttpResponse.json({}, { status: 500 })));

    render(
      <AppBootstrap>
        <IssuerProbe />
      </AppBootstrap>,
    );

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());

    // Restore the default (success) handler and retry.
    server.resetHandlers();
    fireEvent.click(screen.getByText("Retry"));

    expect(await screen.findByText(RUNTIME_CONFIG.issuer)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
