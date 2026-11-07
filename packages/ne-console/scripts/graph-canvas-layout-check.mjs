// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE GRAPH FLOOR LAYOUT CHECK — a real browser, because vitest cannot see this.
//
// The defect this exists for: `.ne-graphwork__canvas` was a block box whose one
// child (GraphChamber) is absolutely positioned, so its auto height resolved to
// 0 and `overflow: hidden` clipped the whole graph away. The workbar still said
// "3 nodes · 2 edges" — the data was there, the box was not.
//
// The suite could not catch it and still cannot: vitest runs in jsdom, which
// does NO layout, so getBoundingClientRect() is all zeros and a collapsed box
// is indistinguishable from a correct one. The existing graph "layout" tests
// assert stylesheet TEXT with regexes, and a regex over CSS source cannot
// measure a box — that instrument is exactly the one that already failed here.
//
// So this check measures the real thing:
//   1. `vite build` — the REAL compiled stylesheet the console ships.
//   2. `vitest run graphFloorLayout.emit.test.tsx` — the REAL DOM React renders
//      for every arm of `.ne-graphwork__main`, plus the legacy Library > Graph
//      host of GraphChamber.
//   3. headless Chromium over CDP (no new npm dependency: Node's built-in
//      WebSocket speaks the protocol directly) lays that out at a wide and a
//      narrow viewport and reports the boxes.
//
// Usage:  node scripts/graph-canvas-layout-check.mjs [--keep] [--chrome <path>]
// Exit 0 = every assertion held. Exit 1 = a box collapsed. Exit 2 = the check
// could not run (no Chromium, build failed) — which is NOT a pass.

import { spawn, spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pkgRoot = resolve(here, "..");

const argv = process.argv.slice(2);
const keep = argv.includes("--keep");
const chromeArgIndex = argv.indexOf("--chrome");
const chromeOverride = chromeArgIndex === -1 ? null : argv[chromeArgIndex + 1];

function die(code, message) {
  process.stderr.write(`graph-canvas-layout-check: ${message}\n`);
  process.exit(code);
}

/** Chromium: an explicit path, $NE_CHROMIUM, a Playwright cache, or a system one. */
function findChromium() {
  const candidates = [];
  if (chromeOverride) candidates.push(chromeOverride);
  if (process.env.NE_CHROMIUM) candidates.push(process.env.NE_CHROMIUM);
  const cache = join(process.env.HOME ?? "", ".cache", "ms-playwright");
  if (existsSync(cache)) {
    for (const entry of readdirSync(cache)) {
      if (entry.startsWith("chromium-"))
        candidates.push(join(cache, entry, "chrome-linux", "chrome"));
      if (entry.startsWith("chromium_headless_shell-"))
        candidates.push(join(cache, entry, "chrome-linux", "headless_shell"));
    }
  }
  candidates.push("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome");
  return candidates.find((path) => path && existsSync(path)) ?? null;
}

function run(command, args, label) {
  const result = spawnSync(command, args, { cwd: pkgRoot, stdio: "inherit", env: process.env });
  if (result.status !== 0) die(2, `${label} failed (exit ${result.status})`);
}

// ---------------------------------------------------------------- CDP client
class Cdp {
  #socket;
  #nextId = 1;
  #pending = new Map();

  constructor(socket) {
    this.#socket = socket;
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      const entry = this.#pending.get(message.id);
      if (!entry) return;
      this.#pending.delete(message.id);
      if (message.error) entry.reject(new Error(JSON.stringify(message.error)));
      else entry.resolve(message.result);
    });
  }

  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((resolveOpen, rejectOpen) => {
      socket.addEventListener("open", resolveOpen, { once: true });
      socket.addEventListener("error", () => rejectOpen(new Error(`cannot connect to ${url}`)), {
        once: true,
      });
    });
    return new Cdp(socket);
  }

  send(method, params = {}, sessionId) {
    const id = this.#nextId++;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolvePromise, rejectPromise) => {
      this.#pending.set(id, { resolve: resolvePromise, reject: rejectPromise });
      this.#socket.send(JSON.stringify(payload));
    });
  }

  close() {
    this.#socket.close();
  }
}

async function launchChromium(binary) {
  const profile = mkdtempSync(join(tmpdir(), "ne-graph-layout-"));
  const child = spawn(
    binary,
    [
      "--headless=new",
      "--no-sandbox",
      "--disable-gpu",
      "--disable-dev-shm-usage",
      "--hide-scrollbars",
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      "about:blank",
    ],
    { stdio: ["ignore", "ignore", "pipe"] },
  );
  const wsUrl = await new Promise((resolveUrl, rejectUrl) => {
    let buffered = "";
    const timer = setTimeout(
      () => rejectUrl(new Error("Chromium did not report a DevTools port")),
      30000,
    );
    child.stderr.on("data", (chunk) => {
      buffered += String(chunk);
      const match = buffered.match(/ws:\/\/[^\s]+/);
      if (match) {
        clearTimeout(timer);
        resolveUrl(match[0]);
      }
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      rejectUrl(new Error(`Chromium exited early (${code}): ${buffered.slice(-400)}`));
    });
  });
  return { child, profile, wsUrl };
}

// ------------------------------------------------------------------ harness
/**
 * The emitted `.ne-graphwork` (or legacy host) under the SAME ancestor chain the
 * app gives it — `.ne-shell > .ne-shell__col > main.ne-shell__stage` (App.tsx) —
 * so the stage's padding and the shell's rail column are in play, not a bare
 * body. The two `data-harness` boxes stand in for NavRail and TopBar, whose own
 * markup is irrelevant to the floor's geometry; only the topbar's height is,
 * and that is taken from the same token `.ne-graphwork` subtracts.
 */
function harnessHtml(cssHref, markup, { narrowFrame = false, extraCss = [] } = {}) {
  const body = narrowFrame ? `<div class="ne-narrowframe--graph">${markup}</div>` : markup;
  const staged = `<div class="ne-shell">
    <div data-harness="rail"></div>
    <div class="ne-shell__col">
      <div data-harness="topbar"></div>
      <main class="ne-shell__stage" id="ne-main">${body}</main>
    </div>
  </div>`;
  const links = [cssHref, ...extraCss]
    .map((href) => `<link rel="stylesheet" href="${href}" />`)
    .join("\n    ");
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    ${links}
    <style>
      /* Harness-only stand-ins for NavRail/TopBar. Nothing here styles anything
         under .ne-graphwork — the floor is measured against the shipped CSS. */
      [data-harness="topbar"] { height: var(--topbar-height); }
      [data-harness="rail"] { height: 100%; }
    </style>
  </head>
  <body>${staged}</body>
</html>`;
}

// --------------------------------------------------------------- assertions
const MEASURE = `(() => {
  const box = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height) };
  };
  return JSON.stringify({
    work: box(".ne-graphwork"),
    floor: box(".ne-graphwork__floor"),
    main: box(".ne-graphwork__main"),
    canvas: box(".ne-graphwork__canvas"),
    chamber: box(".ne-chamber"),
    viewport: box(".ne-chamber__viewport"),
    state: box(".ne-graphwork__main > .ne-state"),
    record: box(".ne-graph-record"),
    legend: box(".ne-legend"),
    controls: box(".ne-chamber__viewport-controls"),
  });
})()`;

const VIEWPORTS = [
  { name: "wide", width: 1440, height: 900 },
  // 900 < 64rem: the narrow treatment (folded rail sheet, overlaying record).
  { name: "narrow", width: 900, height: 800 },
];

/** Every case the check lays out, and what must hold for it. */
function cases(arms) {
  const list = [];
  for (const viewport of VIEWPORTS) {
    list.push({
      id: `${viewport.name}/canvas`,
      markup: arms.canvas,
      viewport,
      options: {},
      assert: (m) => [
        ["canvas has height", m.canvas && m.canvas.h > 0],
        ["canvas has width", m.canvas && m.canvas.w > 0],
        ["canvas fills the main column", m.canvas && m.main && m.canvas.h === m.main.h],
        [
          "chamber fills the canvas",
          m.chamber && m.canvas && m.chamber.h === m.canvas.h && m.chamber.w === m.canvas.w,
        ],
        ["chamber viewport has height", m.viewport && m.viewport.h > 0],
        ["legend is on the canvas", m.legend && m.legend.h > 0],
        ["view controls are on the canvas", m.controls && m.controls.h > 0],
      ],
    });
    for (const arm of ["loading", "empty", "refused", "error"]) {
      list.push({
        id: `${viewport.name}/${arm}`,
        markup: arms[arm],
        viewport,
        options: {},
        assert: (m) => [
          [`${arm} state block has height`, m.state && m.state.h > 0],
          [`${arm} state block spans the main column`, m.state && m.main && m.state.w === m.main.w],
          [
            `${arm} state block is not stretched to the floor`,
            m.state && m.main && m.state.h < m.main.h,
          ],
        ],
      });
    }
    list.push({
      id: `${viewport.name}/record`,
      markup: arms.record,
      viewport,
      options: {},
      assert: (m) => [
        ["record view has height", m.record && m.record.h > 0],
        ["record view spans the main column", m.record && m.main && m.record.w === m.main.w],
        ["record view keeps its own content height", m.record && m.record.h > 100],
      ],
    });
  }
  // The gallery's forced-narrow frame reuses the narrow treatment statically.
  list.push({
    id: "gallery/narrowframe",
    markup: arms.canvas,
    viewport: VIEWPORTS[0],
    options: { narrowFrame: true },
    assert: (m) => [
      ["framed canvas has height", m.canvas && m.canvas.h > 0],
      [
        "framed canvas fills the frame's main column",
        m.canvas && m.main && m.canvas.h === m.main.h,
      ],
      ["framed chamber fills the canvas", m.chamber && m.canvas && m.chamber.h === m.canvas.h],
    ],
  });
  // GraphChamber's OTHER host: library.css's own clamped chamber, which the
  // `.ne-graphwork .ne-chamber` override must never reach.
  // LibraryPage is no longer routed, so library.css is tree-shaken out of the
  // shipped bundle; the guard links the source file directly so the SCOPING
  // invariant (.ne-graphwork .ne-chamber must not reach this host) is still
  // measured against the real stylesheet.
  list.push({
    id: "legacy/library-tab",
    markup: arms.legacy,
    viewport: VIEWPORTS[0],
    options: { extraCss: [pathToFileURL(join(pkgRoot, "src", "knowledge", "library.css")).href] },
    assert: (m) => [
      ["legacy chamber keeps library.css's clamped height", m.chamber && m.chamber.h >= 480],
      [
        "legacy chamber is not absolutely positioned into a 0-height box",
        m.chamber && m.chamber.w > 0,
      ],
      ["legacy chamber viewport has height", m.viewport && m.viewport.h > 0],
    ],
  });
  return list;
}

// -------------------------------------------------------------------- main
// Outside the package: node_modules can be a symlink shared with another
// worktree, and a check must not write into someone else's tree.
const fixtures = join(tmpdir(), "ne-graph-layout-fixtures");
rmSync(fixtures, { recursive: true, force: true });
mkdirSync(fixtures, { recursive: true });

process.stdout.write("→ building the real stylesheet (vite build)\n");
run(
  process.execPath,
  [join(pkgRoot, "node_modules", "vite", "bin", "vite.js"), "build"],
  "vite build",
);

const distAssets = join(pkgRoot, "dist", "assets");
const cssFile = readdirSync(distAssets).find((name) => name.endsWith(".css"));
if (!cssFile) die(2, "no compiled stylesheet in dist/assets");

process.stdout.write("→ rendering the real graph floor DOM (vitest)\n");
process.env.NE_GRAPH_LAYOUT_EMIT = fixtures;
run(
  process.execPath,
  [
    join(pkgRoot, "node_modules", "vitest", "vitest.mjs"),
    "run",
    "src/knowledge/graph/graphFloorLayout.emit.test.tsx",
  ],
  "vitest emit",
);

const arms = {};
for (const name of ["canvas", "loading", "empty", "refused", "error", "record", "legacy"]) {
  const path = join(fixtures, `${name}.html`);
  if (!existsSync(path)) die(2, `fixture ${name}.html was not emitted`);
  arms[name] = readFileSync(path, "utf8");
}

const chromium = findChromium();
if (!chromium) die(2, "no Chromium found (pass --chrome <path> or set NE_CHROMIUM)");
process.stdout.write(`→ laying out in ${chromium}\n\n`);

const cssHref = pathToFileURL(join(distAssets, cssFile)).href;
const pages = [];
for (const testCase of cases(arms)) {
  const file = join(fixtures, `${testCase.id.replace(/\//g, "-")}.html`);
  writeFileSync(file, harnessHtml(cssHref, testCase.markup, testCase.options), "utf8");
  pages.push({ ...testCase, url: pathToFileURL(file).href });
}

const { child, profile, wsUrl } = await launchChromium(chromium);
const cdp = await Cdp.connect(wsUrl);

let failures = 0;
try {
  for (const page of pages) {
    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
    await cdp.send(
      "Emulation.setDeviceMetricsOverride",
      {
        width: page.viewport.width,
        height: page.viewport.height,
        deviceScaleFactor: 1,
        mobile: false,
      },
      sessionId,
    );
    await cdp.send("Page.enable", {}, sessionId);
    await cdp.send("Page.navigate", { url: page.url }, sessionId);
    for (let i = 0; i < 200; i++) {
      const { result } = await cdp.send(
        "Runtime.evaluate",
        { expression: "document.readyState", returnByValue: true },
        sessionId,
      );
      if (result.value === "complete") break;
      await new Promise((r) => setTimeout(r, 25));
    }
    const { result } = await cdp.send(
      "Runtime.evaluate",
      { expression: MEASURE, returnByValue: true },
      sessionId,
    );
    const measured = JSON.parse(result.value);
    await cdp.send("Target.closeTarget", { targetId });

    const boxes = Object.entries(measured)
      .filter(([, value]) => value !== null)
      .map(([name, value]) => `${name} ${value.w}x${value.h}`)
      .join("  ");
    process.stdout.write(`${page.id}\n  ${boxes}\n`);
    for (const [what, held] of page.assert(measured)) {
      if (held) {
        process.stdout.write(`  ok   ${what}\n`);
      } else {
        failures += 1;
        process.stdout.write(`  FAIL ${what}\n`);
      }
    }
    process.stdout.write("\n");
  }
} finally {
  cdp.close();
  child.kill("SIGKILL");
  await new Promise((done) => (child.exitCode === null ? child.once("exit", done) : done()));
  // Chromium can still be flushing its profile as it dies; a leftover temp
  // directory must never turn a passing layout check into a failure.
  try {
    rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
  } catch {
    /* the OS will reap it */
  }
  if (keep) process.stdout.write(`fixtures kept in ${fixtures}\n`);
  else rmSync(fixtures, { recursive: true, force: true });
}

if (failures > 0) die(1, `${failures} layout assertion(s) failed`);
process.stdout.write("graph-canvas-layout-check: every box laid out\n");
