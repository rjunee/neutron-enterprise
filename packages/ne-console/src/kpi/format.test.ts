// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { formatKpiValue } from "./format";

describe("formatKpiValue: ratio→3dp, hours→1dp, count→integer, fallback 2dp", () => {
  it("formats each unit per the server's declared unit", () => {
    expect(formatKpiValue(0.8333, "ratio")).toBe("0.833");
    expect(formatKpiValue(41.5, "hours")).toBe("41.5");
    expect(formatKpiValue(342.0, "count")).toBe("342");
    // Fallback (unknown/null unit): 2dp for a fraction, bare integer otherwise.
    expect(formatKpiValue(1.234, null)).toBe("1.23");
    expect(formatKpiValue(7, null)).toBe("7");
  });
});
