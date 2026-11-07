// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Proof the self-hosted font stack is committed and self-contained (design §1.2,
// C1/C2): the 7 woff2 files exist and are non-empty, the 2 OFL license texts are
// present, fonts.css declares all faces with font-display:block, and there is NO
// external URL anywhere in fonts.css (zero external requests, by construction).
//
// The pairing is Source Sans 3 (UI, 400/500/600/700) + Fira Mono (data,
// 400/500/700), per design/round3/type/RECOMMENDATION.md. There is no variable
// face and no width axis in the vendored set.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const STYLES = resolve(__dirname);
const FONTS = resolve(STYLES, "..", "assets", "fonts");
const fontsCss = readFileSync(resolve(STYLES, "fonts.css"), "utf-8");
// fonts.css with its CSS comments stripped, so a negative assertion about a
// DECLARATION cannot be satisfied or broken by prose that merely names it.
const fontsDecls = fontsCss.replace(/\/\*[\s\S]*?\*\//g, "");

const WOFF2 = [
  "source-sans-3-latin-400-normal.woff2",
  "source-sans-3-latin-500-normal.woff2",
  "source-sans-3-latin-600-normal.woff2",
  "source-sans-3-latin-700-normal.woff2",
  "fira-mono-latin-400-normal.woff2",
  "fira-mono-latin-500-normal.woff2",
  "fira-mono-latin-700-normal.woff2",
];

const LICENSES = ["OFL-Source-Sans-3.txt", "OFL-Fira-Mono.txt"];

describe("self-hosted fonts (§1.2)", () => {
  it("commits 7 non-empty woff2 files", () => {
    for (const f of WOFF2) {
      const st = statSync(resolve(FONTS, f));
      expect(st.size, f).toBeGreaterThan(0);
    }
  });

  it("commits exactly the vendored set — no retired face is left behind", () => {
    // A stale woff2 is dead weight in the bundle AND an unrecorded third-party
    // artifact: NOTICE and the provenance README enumerate the set by name.
    const onDisk = readdirSync(FONTS)
      .filter((f) => f.endsWith(".woff2"))
      .sort();
    expect(onDisk).toEqual([...WOFF2].sort());
  });

  it("commits the 2 OFL license texts naming the SIL license", () => {
    for (const l of LICENSES) {
      const text = readFileSync(resolve(FONTS, l), "utf-8");
      expect(text.toUpperCase(), l).toContain("SIL OPEN FONT LICENSE");
    }
  });

  it("declares Source Sans 3 400/500/600/700 and Fira Mono 400/500/700", () => {
    expect(fontsCss).toContain('font-family: "Source Sans 3"');
    expect(fontsCss).toContain('font-family: "Fira Mono"');
    // 7 @font-face blocks total (4 UI + 3 mono), one per vendored file.
    expect(fontsCss.match(/@font-face/g)?.length).toBe(7);
    expect(fontsCss.match(/font-family: "Source Sans 3"/g)?.length).toBe(4);
    expect(fontsCss.match(/font-family: "Fira Mono"/g)?.length).toBe(3);
    for (const weight of [400, 500, 600, 700]) {
      expect(fontsCss, `weight ${weight}`).toContain(`font-weight: ${weight}`);
    }
    // Static per-weight faces only: no variable range, no width axis. A
    // font-stretch here would select a width the vendored files do not carry.
    expect(fontsDecls).not.toMatch(/font-weight:\s*\d+\s+\d+/);
    expect(fontsDecls).not.toMatch(/font-stretch\s*:/);
    expect(fontsDecls).not.toContain("woff2-variations");
  });

  it("every declared face points at a committed file, and every committed file is declared", () => {
    // Catches both halves of the drift: a face whose url 404s at runtime, and a
    // vendored file nothing loads.
    const urls = [...fontsCss.matchAll(/url\("\.\.\/assets\/fonts\/([^"]+)"\)/g)].map((m) => m[1]);
    expect([...urls].sort()).toEqual([...WOFF2].sort());
    for (const f of urls) expect(statSync(resolve(FONTS, f!)).size).toBeGreaterThan(0);
  });

  it("every @font-face uses font-display: block", () => {
    const faces = fontsCss.match(/@font-face/g)?.length ?? 0;
    const displays = fontsCss.match(/font-display:\s*block/g)?.length ?? 0;
    expect(displays).toBe(faces);
  });

  it("contains no external URL (no CDN / Google Fonts — C1/C2)", () => {
    expect(fontsCss.includes("htt" + "p")).toBe(false);
  });
});
