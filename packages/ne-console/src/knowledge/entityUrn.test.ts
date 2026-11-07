// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { parseEntityUrn } from "./entityUrn";

describe("parseEntityUrn", () => {
  it("extracts the key from a well-formed urn:ne:entity URN", () => {
    expect(parseEntityUrn("urn:ne:entity:abc123")).toBe("abc123");
  });

  it("preserves keys that themselves contain colons/slashes", () => {
    expect(parseEntityUrn("urn:ne:entity:acct/acme:1")).toBe("acct/acme:1");
  });

  it("rejects a different urn namespace", () => {
    expect(parseEntityUrn("urn:ne:other:x")).toBeNull();
  });

  it("rejects a bare key", () => {
    expect(parseEntityUrn("abc123")).toBeNull();
  });

  it("rejects the empty string and an empty key", () => {
    expect(parseEntityUrn("")).toBeNull();
    expect(parseEntityUrn("urn:ne:entity:")).toBeNull();
  });
});
