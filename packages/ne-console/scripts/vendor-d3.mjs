// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-134 d3-vendoring script (plain node, ZERO deps; lives OUTSIDE src/ so the
// thin-client + token-hardcode guardians never scan it). It fetches the pinned,
// immutable jsDelivr ES-module sources for d3-force (+ its d3-quadtree,
// d3-dispatch, d3-timer siblings) into src/vendor/d3/<pkg>/, prints the sha256
// of every file for the provenance README, and applies EXACTLY three
// deterministic bare-import rewrites so the committed sources resolve
// relatively (no bundler bare-specifier resolution, no package.json entry).
//
// C1/C2: this fetch happens ONLY at vendor time. The artifacts are COMMITTED and
// bundled by Vite from local disk — the running console makes ZERO external
// requests. NO d3 package is added to package.json/package-lock.json, so the npm
// closure (and npm_license_gate.py's inputs) are UNCHANGED; these are static
// committed sources, ISC-licensed (C4), the fonts precedent exactly.
//
// C4 + C11: each package's ISC LICENSE text is fetched alongside; versions,
// upstream URLs, sha256 of every file, and the 3 rewrites are recorded in
// src/vendor/d3/README.md.
//
// Run: node scripts/vendor-d3.mjs

import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const VENDOR_DIR = join(HERE, "..", "src", "vendor", "d3");

// Pinned versions (re-verified 2026-07-11 against registry.npmjs.org /latest —
// all ISC). NEVER bump a version without re-verifying the file list against the
// jsDelivr data API at the SAME pinned version.
const PACKAGES = [
  {
    name: "d3-force",
    version: "3.0.0",
    files: [
      "center.js",
      "collide.js",
      "constant.js",
      "index.js",
      "jiggle.js",
      "lcg.js",
      "link.js",
      "manyBody.js",
      "radial.js",
      "simulation.js",
      "x.js",
      "y.js",
    ],
  },
  {
    name: "d3-quadtree",
    version: "3.0.1",
    files: [
      "add.js",
      "cover.js",
      "data.js",
      "extent.js",
      "find.js",
      "index.js",
      "quad.js",
      "quadtree.js",
      "remove.js",
      "root.js",
      "size.js",
      "visit.js",
      "visitAfter.js",
      "x.js",
      "y.js",
    ],
  },
  {
    name: "d3-dispatch",
    version: "3.0.1",
    files: ["dispatch.js", "index.js"],
  },
  {
    name: "d3-timer",
    version: "3.0.1",
    files: ["index.js", "interval.js", "timeout.js", "timer.js"],
  },
];

// EXACTLY three deterministic bare-import rewrites so the committed d3-force
// sources resolve their sibling packages by relative path (no bundler bare
// resolution, no package.json). Recorded verbatim in the README.
const REWRITES = {
  "d3-force/simulation.js": [
    [/from\s+"d3-dispatch"/g, 'from "../d3-dispatch/index.js"'],
    [/from\s+"d3-timer"/g, 'from "../d3-timer/index.js"'],
  ],
  "d3-force/collide.js": [[/from\s+"d3-quadtree"/g, 'from "../d3-quadtree/index.js"']],
  "d3-force/manyBody.js": [[/from\s+"d3-quadtree"/g, 'from "../d3-quadtree/index.js"']],
};

async function fetchText(url) {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(
      `FETCH FAILED (${res.status}) for ${url} — STOP: correct the file list against ` +
        `https://data.jsdelivr.com/v1/packages/npm/<pkg>@<ver> at the SAME pinned version; ` +
        `NEVER substitute a different version or a CDN runtime link.`,
    );
  }
  return await res.text();
}

function sha256(text) {
  return createHash("sha256").update(text, "utf-8").digest("hex");
}

async function main() {
  const provenance = [];
  for (const pkg of PACKAGES) {
    const dir = join(VENDOR_DIR, pkg.name);
    await mkdir(dir, { recursive: true });
    console.log(`=== ${pkg.name}@${pkg.version} ===`);
    for (const file of pkg.files) {
      const url = `https://cdn.jsdelivr.net/npm/${pkg.name}@${pkg.version}/src/${file}`;
      let text = await fetchText(url);
      const rewriteKey = `${pkg.name}/${file}`;
      const rules = REWRITES[rewriteKey];
      if (rules) {
        for (const [pattern, replacement] of rules) {
          if (!pattern.test(text)) {
            throw new Error(
              `expected rewrite target ${pattern} not found in ${rewriteKey} — upstream drift; STOP.`,
            );
          }
          text = text.replace(pattern, replacement);
        }
      }
      const sha = sha256(text);
      await writeFile(join(dir, file), text);
      console.log(`  ${file}\t${text.length} bytes\tsha256=${sha}${rules ? "  (rewritten)" : ""}`);
      provenance.push({ file: `${pkg.name}/${file}`, sha });
    }
    // ISC license text for the package root.
    const licenseUrl = `https://cdn.jsdelivr.net/npm/${pkg.name}@${pkg.version}/LICENSE`;
    const license = await fetchText(licenseUrl);
    if (!license.includes("ISC")) {
      throw new Error(`LICENSE for ${pkg.name} did not contain the ISC header — STOP.`);
    }
    await writeFile(join(dir, `LICENSE-${pkg.name}`), license);
    console.log(`  LICENSE-${pkg.name}\t${license.length} bytes (ISC)`);
  }
  console.log(
    "\ndone — commit the artifacts + record versions/sha256/rewrites in src/vendor/d3/README.md",
  );
}

main().catch((err) => {
  console.error(String(err instanceof Error ? err.message : err));
  process.exit(1);
});
