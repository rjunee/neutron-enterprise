// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-130 font-vendoring script (plain node, ZERO deps; lives OUTSIDE src/ so the
// thin-client guardian + eslint ts-globs never scan it). It fetches the pinned,
// immutable fontsource woff2 + OFL license files into src/assets/fonts/ and
// prints the sha256 of each download for the provenance README.
//
// C1/C2: this fetch happens ONLY at build time. The artifacts are COMMITTED and
// served locally via @font-face — the running console makes ZERO external
// requests. NO @fontsource package is added to package.json (OFL-1.1 is not in
// the npm license policy's free list and that policy file is out of lane), so
// the fonts never enter the npm closure; they are static committed assets.
//
// Run: node scripts/vendor-fonts.mjs

import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FONTS_DIR = join(HERE, "..", "src", "assets", "fonts");

// Pinned, immutable (version-locked) jsDelivr URLs → committed local filenames.
const WOFF2 = [
  {
    url: "https://cdn.jsdelivr.net/npm/@fontsource/source-sans-3@5.3.0/files/source-sans-3-latin-400-normal.woff2",
    out: "source-sans-3-latin-400-normal.woff2",
  },
  {
    url: "https://cdn.jsdelivr.net/npm/@fontsource/source-sans-3@5.3.0/files/source-sans-3-latin-500-normal.woff2",
    out: "source-sans-3-latin-500-normal.woff2",
  },
  {
    url: "https://cdn.jsdelivr.net/npm/@fontsource/source-sans-3@5.3.0/files/source-sans-3-latin-600-normal.woff2",
    out: "source-sans-3-latin-600-normal.woff2",
  },
  {
    url: "https://cdn.jsdelivr.net/npm/@fontsource/source-sans-3@5.3.0/files/source-sans-3-latin-700-normal.woff2",
    out: "source-sans-3-latin-700-normal.woff2",
  },
  {
    url: "https://cdn.jsdelivr.net/npm/@fontsource/fira-mono@5.3.0/files/fira-mono-latin-400-normal.woff2",
    out: "fira-mono-latin-400-normal.woff2",
  },
  {
    url: "https://cdn.jsdelivr.net/npm/@fontsource/fira-mono@5.3.0/files/fira-mono-latin-500-normal.woff2",
    out: "fira-mono-latin-500-normal.woff2",
  },
  {
    url: "https://cdn.jsdelivr.net/npm/@fontsource/fira-mono@5.3.0/files/fira-mono-latin-700-normal.woff2",
    out: "fira-mono-latin-700-normal.woff2",
  },
];

// The OFL license text for each package root.
const LICENSES = [
  {
    url: "https://cdn.jsdelivr.net/npm/@fontsource/source-sans-3@5.3.0/LICENSE",
    out: "OFL-Source-Sans-3.txt",
  },
  {
    url: "https://cdn.jsdelivr.net/npm/@fontsource/fira-mono@5.3.0/LICENSE",
    out: "OFL-Fira-Mono.txt",
  },
];

async function download(url, out, { text = false } = {}) {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(
      `FETCH FAILED (${res.status}) for ${url} — STOP: do NOT substitute a CDN link.`,
    );
  }
  const buf = Buffer.from(await res.arrayBuffer());
  const sha = createHash("sha256").update(buf).digest("hex");
  const dest = join(FONTS_DIR, out);
  await writeFile(dest, buf);
  console.log(`${out}\t${buf.length} bytes\tsha256=${sha}`);
  if (text && !buf.toString("utf-8").includes("SIL OPEN FONT LICENSE")) {
    throw new Error(`license text for ${out} did not contain the SIL OFL header`);
  }
  return { out, sha, bytes: buf.length };
}

async function main() {
  await mkdir(FONTS_DIR, { recursive: true });
  console.log("=== woff2 binaries ===");
  for (const f of WOFF2) await download(f.url, f.out);
  console.log("=== OFL license texts ===");
  for (const l of LICENSES) await download(l.url, l.out, { text: true });
  console.log(
    "done — commit the artifacts + record the sha256 hashes in src/assets/fonts/README.md",
  );
}

main().catch((err) => {
  console.error(String(err instanceof Error ? err.message : err));
  process.exit(1);
});
