// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE FINDER-BUTTON CASCADE CHECK — a real browser, because jsdom cannot
// resolve which of two tied-specificity CSS rules wins.
//
// The defect this exists for (owner-reported, more than once, 2026-08-26): on
// https://demo.example.host/knowledge/graph at desktop width, "Find entities"
// and "Close finder" render but do nothing. `.ne-workbar__findbtn` and
// `.ne-graphwork__finder-close` (graphWorkspace.css) hide those buttons
// outside narrow layout; `.ne-btn` (styles/table.css) shows every button.
// Same specificity (0,1,0) — the winner is whichever rule the BUNDLER placed
// later in the shipped stylesheet, an assembly detail no vitest test reads.
// `.ne-btn` won, so the buttons rendered and re-showed at every width.
//
// jsdom does no layout and cannot even parse a stylesheet's cascade the way a
// browser does — `getComputedStyle` in jsdom does not apply an external
// stylesheet's specificity/order rules the way a real engine does, so a vitest
// assertion is structurally unable to see this fight, and a regex over the
// CSS source text (this repo already has one — GraphWorkspace.test.tsx
// "declares the three-column and narrow sheet layout without motion") cannot
// either: the SOURCE text is fine, it is the deployed BUNDLE's rule order
// that was broken.
//
// So this check measures the real thing:
//   1. `vite build` — the REAL compiled stylesheet the console ships, with
//      table.css and graphWorkspace.css merged in whatever order the bundler
//      actually produces.
//   2. `vitest run graphFinderButtons.emit.test.tsx` — the REAL DOM React
//      renders for the desktop, narrow-closed, and narrow-open arms (the
//      narrow-open arm is reached via a real click through GraphWorkspace's
//      own onClick/state wiring, not a hand-authored `aria-expanded="true"`).
//   3. headless Chromium over CDP (no new npm dependency: Node's built-in
//      WebSocket speaks the protocol directly) lays each fixture out at the
//      matching viewport and reads getComputedStyle + getBoundingClientRect
//      for both buttons — the level that actually resolves the cascade.
//
// Usage:  node scripts/graph-finder-buttons-cascade-check.mjs [--keep] [--chrome <path>]
// Exit 0 = every assertion held. Exit 1 = a button resolved the wrong way.
// Exit 2 = the check could not run (no Chromium, build failed) — NOT a pass.

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
  process.stderr.write(`graph-finder-buttons-cascade-check: ${message}\n`);
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
  const profile = mkdtempSync(join(tmpdir(), "ne-finder-cascade-"));
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
 * The emitted `.ne-graphwork` under the SAME ancestor chain the app gives it
 * (App.tsx: `.ne-shell > .ne-shell__col > main.ne-shell__stage`), matching
 * scripts/graph-canvas-layout-check.mjs's harness so the workbar's own
 * `--topbar-height` subtraction is exercised the same way.
 */
function harnessHtml(cssHref, markup, width) {
  const staged = `<div class="ne-shell">
    <div data-harness="rail"></div>
    <div class="ne-shell__col">
      <div data-harness="topbar"></div>
      <main class="ne-shell__stage" id="ne-main">${markup}</main>
    </div>
  </div>`;
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <link rel="stylesheet" href="${cssHref}" />
    <style>
      [data-harness="topbar"] { height: var(--topbar-height); }
      [data-harness="rail"] { height: 100%; }
      html, body { margin: 0; width: ${width}px; }
    </style>
  </head>
  <body>${staged}</body>
</html>`;
}

// --------------------------------------------------------------- assertions
const MEASURE = `(() => {
  const describe = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const style = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return {
      display: style.display,
      hidden: el.hidden,
      w: Math.round(r.width),
      h: Math.round(r.height),
    };
  };
  return JSON.stringify({
    findbtn: describe(".ne-workbar__findbtn"),
    finderCloseBtn: describe(".ne-graphwork__finder-close"),
    finder: describe(".ne-graphwork__finder"),
  });
})()`;

/** Every fixture the check lays out, its viewport, and what must hold. */
const CASES = [
  {
    id: "desktop",
    // Desktop (>1024px): the sidebar is permanently open, so BOTH toggle
    // buttons are meaningless and must be unreachable — this is the exact
    // shape of the owner's defect ("render but do nothing").
    width: 1440,
    height: 900,
    assert: (m) => [
      ["find-entities button is display:none", m.findbtn?.display === "none"],
      ["find-entities button has no box (unreachable)", m.findbtn?.w === 0 && m.findbtn?.h === 0],
      ["close-finder button is display:none", m.finderCloseBtn?.display === "none"],
      [
        "close-finder button has no box (unreachable)",
        m.finderCloseBtn?.w === 0 && m.finderCloseBtn?.h === 0,
      ],
    ],
  },
  {
    id: "narrow-closed",
    // ≤64rem/1024px, sheet not opened: the toggle must be visible and
    // reachable (it is the only way in); the sheet itself is [hidden].
    width: 900,
    height: 800,
    assert: (m) => [
      ["find-entities button is not display:none", m.findbtn?.display !== "none"],
      ["find-entities button has a box (reachable)", m.findbtn?.w > 0 && m.findbtn?.h > 0],
      ["finder sheet is hidden", m.finder?.hidden === true],
    ],
  },
  {
    id: "narrow-open",
    // ≤64rem/1024px, sheet opened by a real click (see the emit test): the
    // close button must now be visible and reachable — narrow mode must keep
    // working exactly as before; this fix must not regress it.
    width: 900,
    height: 800,
    assert: (m) => [
      ["find-entities button is not display:none", m.findbtn?.display !== "none"],
      ["close-finder button is not display:none", m.finderCloseBtn?.display !== "none"],
      [
        "close-finder button has a box (reachable)",
        m.finderCloseBtn?.w > 0 && m.finderCloseBtn?.h > 0,
      ],
    ],
  },
];

// -------------------------------------------------------------------- main
// Outside the package: node_modules can be a symlink shared with another
// worktree, and a check must not write into someone else's tree.
const fixtures = join(tmpdir(), "ne-graph-finder-fixtures");
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

process.stdout.write("→ rendering the real finder-button DOM (vitest)\n");
process.env.NE_GRAPH_FINDER_EMIT = fixtures;
run(
  process.execPath,
  [
    join(pkgRoot, "node_modules", "vitest", "vitest.mjs"),
    "run",
    "src/knowledge/graph/graphFinderButtons.emit.test.tsx",
  ],
  "vitest emit",
);

const markup = {};
for (const testCase of CASES) {
  const path = join(fixtures, `${testCase.id}.html`);
  if (!existsSync(path)) die(2, `fixture ${testCase.id}.html was not emitted`);
  markup[testCase.id] = readFileSync(path, "utf8");
}

const chromium = findChromium();
if (!chromium) die(2, "no Chromium found (pass --chrome <path> or set NE_CHROMIUM)");
process.stdout.write(`→ laying out in ${chromium}\n\n`);

const cssHref = pathToFileURL(join(distAssets, cssFile)).href;
const pages = [];
for (const testCase of CASES) {
  const file = join(fixtures, `${testCase.id}.html`.replace(".html", "-page.html"));
  writeFileSync(file, harnessHtml(cssHref, markup[testCase.id], testCase.width), "utf8");
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
      { width: page.width, height: page.height, deviceScaleFactor: 1, mobile: false },
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

    process.stdout.write(`${page.id} (${page.width}x${page.height})\n`);
    process.stdout.write(`  findbtn:        ${JSON.stringify(measured.findbtn)}\n`);
    process.stdout.write(`  finderCloseBtn: ${JSON.stringify(measured.finderCloseBtn)}\n`);
    process.stdout.write(`  finder:         ${JSON.stringify(measured.finder)}\n`);
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
  try {
    rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
  } catch {
    /* the OS will reap it */
  }
  if (keep) process.stdout.write(`fixtures kept in ${fixtures}\n`);
  else rmSync(fixtures, { recursive: true, force: true });
}

if (failures > 0) die(1, `${failures} cascade assertion(s) failed`);
process.stdout.write("graph-finder-buttons-cascade-check: every button resolved correctly\n");
