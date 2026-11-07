// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-CONSOLE-ISSUER: fetchRuntimeConfig fetch/parse/validate. Fail-closed — an
// HTTP error or a missing/empty required field rejects (never a fallback issuer);
// extra keys are tolerated.

import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { RUNTIME_CONFIG } from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { fetchRuntimeConfig, RuntimeConfigError } from "./runtimeConfig";

describe("fetchRuntimeConfig", () => {
  it("returns the parsed config on the success path", async () => {
    const cfg = await fetchRuntimeConfig();
    expect(cfg).toEqual(RUNTIME_CONFIG);
  });

  it("rejects on an HTTP error (fail-closed)", async () => {
    server.use(http.get("*/config.json", () => HttpResponse.json({}, { status: 500 })));
    await expect(fetchRuntimeConfig()).rejects.toBeInstanceOf(RuntimeConfigError);
  });

  // THE BOOTSTRAP PAGE IS FULL-VIEWPORT AND RENDERS `message`. What a viewer
  // reads when the config read fails is a sentence; the status and the client's
  // own string survive on the developer slot, for the console and for
  // telemetry. Both halves asserted here: dropping either is the defect.
  it("splits the reader's sentence from the client-composed string", async () => {
    server.use(http.get("*/config.json", () => HttpResponse.json({}, { status: 500 })));
    const err = await fetchRuntimeConfig().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(RuntimeConfigError);
    const failure = err as RuntimeConfigError;
    expect(failure.message).toBe("The server is unavailable.");
    expect(failure.userMessage).toBe("The server is unavailable.");
    expect(failure.status).toBe(500);
    expect(failure.developerMessage).toBe("runtime config fetch failed: HTTP 500");
  });

  // A 200 carrying an unusable body is NOT a transport failure, and must not
  // borrow the sentence for one — the server answered.
  it("says the configuration is unusable, not that the server is unavailable", async () => {
    server.use(
      http.get("*/config.json", () => HttpResponse.json({ ...RUNTIME_CONFIG, issuer: "" })),
    );
    const err = (await fetchRuntimeConfig().catch((e: unknown) => e)) as RuntimeConfigError;
    expect(err.message).toBe("The deployment’s configuration isn’t usable.");
    expect(err.developerMessage).toMatch(/issuer/);
  });

  // TWO FAILURES NOBODY WRITES A STRING FOR. Both `await`s here used to be
  // bare, so what a viewer read at the bootstrap was whatever the runtime threw
  // — and what escaped was not a RuntimeConfigError at all.
  it("wraps a REJECTED fetch: status 0, and the sentence for no response", async () => {
    server.use(http.get("*/config.json", () => HttpResponse.error()));
    const err = await fetchRuntimeConfig().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(RuntimeConfigError);
    const failure = err as RuntimeConfigError;
    // This is the status-<100 arm of the house vocabulary. Before the wrap
    // nothing in the console could construct a transport error for a fetch that
    // never got a response, so the arm existed and was unreachable.
    expect(failure.status).toBe(0);
    expect(failure.message).toBe("The server couldn’t be reached.");
    expect(failure.userMessage).toBe("The server couldn’t be reached.");
    // The engine's own words survive where they belong, and nowhere else.
    expect(failure.developerMessage).toMatch(/rejected/);
    expect(failure.message).not.toMatch(/Failed to fetch/);
  });

  it("wraps an UNPARSEABLE body: a 200 of HTML is an unusable config, not a 500", async () => {
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
    const err = await fetchRuntimeConfig().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(RuntimeConfigError);
    const failure = err as RuntimeConfigError;
    expect(failure.status).toBe(200);
    expect(failure.message).toBe("The deployment’s configuration isn’t usable.");
    // V8's parser string is a developer fact. It is not a sentence, and it is
    // exactly what the bootstrap page used to show full-viewport.
    expect(failure.developerMessage).toMatch(/did not parse/);
    expect(failure.message).not.toMatch(/doctype|JSON|token/i);
  });

  it("rejects when the issuer key is missing", async () => {
    server.use(
      http.get("*/config.json", () =>
        HttpResponse.json({ client_id: "ne-console", audience: "ne-console", api_base: "/api" }),
      ),
    );
    const err = (await fetchRuntimeConfig().catch((e: unknown) => e)) as RuntimeConfigError;
    expect(err).toBeInstanceOf(RuntimeConfigError);
    expect(err.developerMessage).toMatch(/issuer/);
  });

  it("rejects when the issuer is an empty string", async () => {
    server.use(
      http.get("*/config.json", () => HttpResponse.json({ ...RUNTIME_CONFIG, issuer: "" })),
    );
    await expect(fetchRuntimeConfig()).rejects.toBeInstanceOf(RuntimeConfigError);
  });

  it("tolerates extra keys", async () => {
    server.use(
      http.get("*/config.json", () => HttpResponse.json({ ...RUNTIME_CONFIG, extra: "ignored" })),
    );
    const cfg = await fetchRuntimeConfig();
    expect(cfg).toEqual(RUNTIME_CONFIG);
  });
});
