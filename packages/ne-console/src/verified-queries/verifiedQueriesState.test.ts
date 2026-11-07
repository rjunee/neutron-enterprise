// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import type { VerifiedTemplateWire } from "../api/types";
import {
  catalogMatchesHumanDescription,
  classifyVerifiedFailure,
  ownsRequest,
  snapshotInvokeRequest,
  templateShortLabel,
  verifiedQueriesOwnerId,
} from "./verifiedQueriesState";

const TEMPLATE = {
  template_id: "internal-secret-needle",
  version: 4,
  title: "Every visible payment due this week.",
} as VerifiedTemplateWire;

describe("verified query orchestration state", () => {
  it("binds request ownership to both the client instance and principal", () => {
    const first = {};
    const second = {};
    const owner = verifiedQueriesOwnerId(first, "alice");
    expect(verifiedQueriesOwnerId(first, "alice")).toBe(owner);
    expect(verifiedQueriesOwnerId(first, "bob")).not.toBe(owner);
    const secondOwner = verifiedQueriesOwnerId(second, "alice");
    expect(secondOwner).not.toBe(owner);
    expect(ownsRequest({ owner, sequence: 2 }, { owner, sequence: 2 })).toBe(true);
    expect(ownsRequest({ owner, sequence: 2 }, { owner: secondOwner, sequence: 2 })).toBe(false);
    expect(ownsRequest({ owner, sequence: 3 }, { owner, sequence: 2 })).toBe(false);
  });

  it("searches the description AND the short label shown in the catalog", () => {
    expect(catalogMatchesHumanDescription(TEMPLATE, "payment due")).toBe(true);
    // The catalog column now shows the id-derived short label ("Internal secret
    // needle"), so searching what the user SEES must match…
    expect(catalogMatchesHumanDescription(TEMPLATE, "secret needle")).toBe(true);
    // …while the raw hyphenated technical id still does not.
    expect(catalogMatchesHumanDescription(TEMPLATE, "secret-needle")).toBe(false);
  });

  it("derives readable short labels from template ids", () => {
    expect(templateShortLabel("disaster-declarations")).toBe("Disaster declarations");
    expect(templateShortLabel("open-disputes-by-status")).toBe("Open disputes by status");
    expect(templateShortLabel("autoloans-for-customer")).toBe("Autoloans for customer");
    expect(templateShortLabel("t3-records-in-class")).toBe("T3 records in class");
    expect(templateShortLabel("t3-record")).toBe("T3 record");
  });

  it("freezes the exact id@version request used by retry", () => {
    const args = { county: "Richmond" };
    const request = snapshotInvokeRequest(TEMPLATE, args);
    args.county = "Columbia";
    expect(request).toEqual({
      template_id: "internal-secret-needle",
      version: 4,
      args: { county: "Richmond" },
    });
    expect(Object.isFrozen(request)).toBe(true);
    expect(Object.isFrozen(request.args)).toBe(true);
  });

  it("keeps refusal, invalid args, and faults distinct", () => {
    expect(classifyVerifiedFailure(new ApiError("no", 403))).toEqual({
      kind: "refused",
      detail: null,
    });
    expect(classifyVerifiedFailure(new ApiError("bad", 422, "invalid-args"))).toEqual({
      kind: "invalid",
      detail: "invalid-args",
    });
    expect(classifyVerifiedFailure(new ApiError("guardrail", 422, "guardrail-violation"))).toEqual({
      kind: "invalid",
      detail: "guardrail-violation",
    });
    expect(classifyVerifiedFailure(new ApiError("other", 422, "other-token"))).toEqual({
      kind: "error",
      detail: "other-token",
    });
    expect(classifyVerifiedFailure(new Error("offline"))).toEqual({ kind: "error", detail: null });
  });
});
