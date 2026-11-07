// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { traceDecisionHref, traceEventAnchor } from "./traceLink";

describe("trace decision links", () => {
  it("uses the same exact-event anchor for the link and the target row", () => {
    const anchor = traceEventAnchor("gate:event:9");
    expect(anchor).toBe("trace-event-gate:event:9");
    expect(traceDecisionHref({ traceId: "trace/123", eventId: "gate:event:9" })).toBe(
      "/trace/trace%2F123#trace-event-gate%3Aevent%3A9",
    );
  });

  it("links only to the run when the server supplies no exact event id", () => {
    expect(traceDecisionHref({ traceId: "trace-123" })).toBe("/trace/trace-123");
  });
});
