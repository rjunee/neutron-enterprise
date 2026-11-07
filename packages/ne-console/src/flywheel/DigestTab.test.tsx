// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §11 — DIGEST, on the two things a column header is for.
//
// A header is the cheapest place to say what a number means and the cheapest
// place to lie about it. Both numbers on the Skill health card arrive as bare
// floats, so the header is the ONLY thing on screen that can say which
// direction is bad and what the scale is:
//
//   * `ratio` is ne/skills/retirement.py:151-155 — `fail_count /
//     (success_count + fail_count)`, i.e. the Failed column over the Fires
//     column, which are the two cells immediately to its left.
//   * `score` is ne/skills/near_dup.py — a SIMILARITY (Jaccard, cosine, or a
//     flat 1.0 for a shared trigger phrase), so higher is more alike. "Score"
//     admitted a reading in which 0.87 is a distance and the pair is far apart.
//
// The week window is asserted BY SHAPE, never against a literal: `week_start`
// and `week_end` are calendar dates, and a test that pinned them to a rendered
// month would be deciding its own result from the runner's timezone.

import { render, screen, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { StewardDigestView } from "../api/types";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { DigestTab } from "./DigestTab";

/** A digest carrying the Gardener fold, which the shared fixture omits. The
 * numbers are chosen so a header naming the wrong division would be caught:
 * 3/6 = 0.50 is neither the Fires nor the Failed cell. */
const DIGEST_WITH_SKILLS: StewardDigestView = {
  week_start: "2026-06-29",
  week_end: "2026-07-05",
  generated_at: "2026-07-07T10:00:00Z",
  totals: { proposed: 0, approved: 0, rejected: 0, applied: 0 },
  by_lane: [],
  skills: {
    ran_at: "2026-07-01T00:00:00Z",
    retirement_candidates: [],
    failure_flags: [{ name: "quarterly-close", known_fires: 6, failed: 3, ratio: 0.5 }],
    trigger_overlaps: [],
    near_dups: [{ a: "quarterly-close", b: "quarter-end-close", method: "jaccard", score: 0.87 }],
    unknown_requires_tools: [],
    stale_proposals: [],
  },
};

function renderDigest(view: StewardDigestView = DIGEST_WITH_SKILLS) {
  server.use(http.get("*/api/v1/steward/digest", () => HttpResponse.json(view)));
  render(
    <AuthStub>
      <MemoryRouter>
        <DigestTab />
      </MemoryRouter>
    </AuthStub>,
  );
}

/** Header labels with DataTable's active-sort glyph (▲/▼) stripped — the glyph
 * is the table's own state indicator, not part of what the column claims. */
function headers(name: string): string[] {
  return within(screen.getByRole("table", { name }))
    .getAllByRole("columnheader")
    .map((th) => (th.textContent ?? "").replace(/[▲▼]$/u, ""));
}

describe("§11 digest — the Skill health headers say what the number is", () => {
  it("the failure-flag ratio header names the division, and the two columns it divides are on screen", async () => {
    renderDigest();
    const table = await screen.findByRole("table", { name: "Failure flags" });

    // Not "Ratio": the header states the numerator and the denominator, and
    // both of them are columns of this same table.
    expect(headers("Failure flags")).toEqual(["Name", "Fires", "Failed", "Failed / fires"]);
    const cells = [...table.querySelectorAll("tbody tr.ne-tr td")].map((td) => td.textContent);
    expect(cells).toEqual(["quarterly-close", "6", "3", "0.50"]);
  });

  it("the near-duplicate score header says it is a similarity, and the pair columns name skills", async () => {
    renderDigest();
    await screen.findByRole("table", { name: "Near-duplicates" });

    // Not "A"/"B"/"Score". Each pair cell holds a full skill name, and the
    // number is a similarity — higher is more alike, which "Score" never said.
    expect(headers("Near-duplicates")).toEqual(["Skill A", "Skill B", "Method", "Similarity"]);
  });

  it("the week window keeps the server's calendar dates, with no invented time of day", async () => {
    renderDigest();
    await screen.findByText("Week totals");

    // BY SHAPE. `week_start`/`week_end` are dates on the wire; formatting them
    // through the console's instant formatters would print a midnight that
    // nobody sent and, west of UTC, the day before.
    const times = [...document.querySelectorAll("time")];
    expect(times.map((t) => t.getAttribute("datetime"))).toEqual(["2026-06-29", "2026-07-05"]);
    for (const t of times) {
      expect(t.textContent).toMatch(/^\d{4}-\d{2}-\d{2}$/);
      expect(t.textContent).toBe(t.getAttribute("datetime"));
    }
  });
});
