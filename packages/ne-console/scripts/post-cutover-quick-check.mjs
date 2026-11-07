#!/usr/bin/env node
// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// POST-CUTOVER QUICK CHECK — the ~3-minute per-deploy liveness battery (unit C1,
// --quick mode). Run this after every incremental deploy to prove the live site
// is still good, without waiting on the full promotion-gate battery.
//
// Checks, in order:
//   1. GET /        -> 200
//   2. GET /healthz -> 200
//   3. One real Keycloak login as `ops` (real Authorization-Code+PKCE redirect,
//      real Keycloak login form, real return) and the console renders.
//   4. The authenticated production Graph API returns EXACTLY 45 nodes, 40
//      relationships, truncated=false (asserted exact, never "at least").
//   5. Zero console errors, zero page errors, zero failed HTTP requests, and
//      zero non-GET requests to the app's own API across the whole run.
//
// Same approach as packages/ne-console/scripts/graph-canvas-layout-check.mjs:
// headless Chromium driven over CDP using Node's built-in WebSocket. No new
// npm dependency, no lockfile change — the browser is already on the box.
//
// The ops password is NEVER a CLI argument or logged value. Pipe it on stdin:
//   printf '%s' "$NE_OPS_PASSWORD" | node scripts/post-cutover-quick-check.mjs \
//     --url https://demo.example.host
// or export NE_QUICK_OPS_PASSWORD before running (still never printed, never
// written to disk). The value is held in memory only long enough to fill the
// login form and is discarded (overwritten) immediately after.
//
// Exit 0 = every assertion held. Exit 1 = a real assertion failed (a genuine
// finding about the live site — e.g. the graph count is wrong, or a console
// error fired). Exit 2 = the harness itself could not run (no Chromium, no
// password supplied, CDP connect failure, unhandled crash) — NOT a pass, and
// must never be reported as one.

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const argv = process.argv.slice(2);
function argValue(name, fallback) {
  const i = argv.indexOf(name);
  return i === -1 ? fallback : argv[i + 1];
}

const BASE_URL = argValue("--url", "https://demo.example.host").replace(/\/+$/, "");
const OPS_USER = argValue("--ops-user", "ops");
const CHROME_OVERRIDE = argValue("--chrome", null);
const NAV_TIMEOUT_MS = Number(argValue("--nav-timeout-ms", "45000"));

function die(code, message) {
  process.stderr.write(`post-cutover-quick-check: ${message}\n`);
  process.exit(code);
}

// ------------------------------------------------------------- the password
// stdin, if piped, wins; otherwise NE_QUICK_OPS_PASSWORD. Never a CLI arg.
function readOpsPassword() {
  if (!process.stdin.isTTY) {
    try {
      const fromStdin = readFileSync(0, "utf8").replace(/[\r\n]+$/, "");
      if (fromStdin.length > 0) return fromStdin;
    } catch {
      /* fall through to env */
    }
  }
  const fromEnv = process.env.NE_QUICK_OPS_PASSWORD;
  if (fromEnv && fromEnv.length > 0) return fromEnv;
  return null;
}

let opsPassword = readOpsPassword();
if (!opsPassword) {
  die(
    2,
    "no ops password supplied — pipe it on stdin or set NE_QUICK_OPS_PASSWORD " +
      "(never as a CLI argument, never written to a file)",
  );
}

// ------------------------------------------------------------------- results
const results = {
  url: BASE_URL,
  started_at: new Date().toISOString(),
  checks: [],
  console_errors: [],
  page_errors: [],
  http_failures: [],
  blocked_mutations: [],
};

function record(name, ok, detail) {
  results.checks.push({ name, ok, detail: detail ?? null });
  process.stdout.write(`${ok ? "ok  " : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}\n`);
}

// ---------------------------------------------------------------- 1/2: health
async function checkHealth() {
  for (const [name, path] of [
    ["GET / -> 200", "/"],
    ["GET /healthz -> 200", "/healthz"],
  ]) {
    try {
      const response = await fetch(`${BASE_URL}${path}`, { redirect: "manual" });
      record(name, response.status === 200, `status=${response.status}`);
    } catch (error) {
      // Unreachable at the network level: the harness cannot evaluate anything
      // else meaningfully either, but this specific check is a genuine FAIL,
      // not a harness problem — report it as such and let the rest try anyway.
      record(name, false, `network error: ${String(error.message || error).slice(0, 200)}`);
    }
  }
}

// --------------------------------------------------------------- Chromium/CDP
function findChromium() {
  const candidates = [];
  if (CHROME_OVERRIDE) candidates.push(CHROME_OVERRIDE);
  if (process.env.NE_CHROMIUM) candidates.push(process.env.NE_CHROMIUM);
  const cache = join(process.env.HOME ?? "", ".cache", "ms-playwright");
  if (existsSync(cache)) {
    for (const entry of readdirSync(cache)) {
      if (entry.startsWith("chromium-"))
        candidates.push(join(cache, entry, "chrome-linux", "chrome"));
    }
  }
  candidates.push("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome");
  return candidates.find((path) => path && existsSync(path)) ?? null;
}

class Cdp {
  #socket;
  #nextId = 1;
  #pending = new Map();
  #listeners = new Set();

  constructor(socket) {
    this.#socket = socket;
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id !== undefined) {
        const entry = this.#pending.get(message.id);
        if (entry) {
          this.#pending.delete(message.id);
          if (message.error) entry.reject(new Error(JSON.stringify(message.error)));
          else entry.resolve(message.result);
        }
        return;
      }
      for (const listener of this.#listeners) listener(message);
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

  on(handler) {
    this.#listeners.add(handler);
    return () => this.#listeners.delete(handler);
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
  const profile = mkdtempSync(join(tmpdir(), "ne-quick-check-"));
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

// ------------------------------------------------------------------- polling
async function waitUntil(fn, { timeoutMs, intervalMs = 200, description }) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const value = await fn();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error(
    `timed out waiting for: ${description}${lastError ? ` (last error: ${lastError.message})` : ""}`,
  );
}

// ---------------------------------------------------------------- the battery
async function runBrowserBattery() {
  const chromium = findChromium();
  if (!chromium) die(2, "no Chromium found (pass --chrome <path> or set NE_CHROMIUM)");

  let launched;
  try {
    launched = await launchChromium(chromium);
  } catch (error) {
    die(2, `Chromium launch failed: ${error.message}`);
  }
  const { child, profile, wsUrl } = launched;

  let cdp;
  try {
    cdp = await Cdp.connect(wsUrl);
  } catch (error) {
    child.kill("SIGKILL");
    die(2, `CDP connect failed: ${error.message}`);
  }

  let sessionId;
  try {
    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    ({ sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true }));

    await cdp.send("Page.enable", {}, sessionId);
    await cdp.send("Runtime.enable", {}, sessionId);
    await cdp.send("Network.enable", {}, sessionId);
    await cdp.send(
      "Emulation.setDeviceMetricsOverride",
      { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false },
      sessionId,
    );

    // ---- zero console/page errors ----
    cdp.on((message) => {
      if (message.sessionId !== sessionId) return;
      if (message.method === "Runtime.consoleAPICalled" && message.params.type === "error") {
        const text = (message.params.args || [])
          .map((a) => (a.value !== undefined ? String(a.value) : (a.description ?? a.type)))
          .join(" ")
          .slice(0, 300);
        results.console_errors.push(text);
      }
      if (message.method === "Runtime.exceptionThrown") {
        const text = String(
          message.params.exceptionDetails?.exception?.description ??
            message.params.exceptionDetails?.text ??
            "unknown page error",
        ).slice(0, 300);
        results.page_errors.push(text);
      }
    });

    // ---- zero failed HTTP requests (excluding our own deliberate blocks) ----
    const requestMeta = new Map(); // requestId -> {url, method}
    const blockedUrls = new Set();
    cdp.on((message) => {
      if (message.sessionId !== sessionId) return;
      if (message.method === "Network.requestWillBeSent") {
        requestMeta.set(message.params.requestId, {
          url: message.params.request.url,
          method: message.params.request.method,
        });
      }
      if (message.method === "Network.responseReceived") {
        const meta = requestMeta.get(message.params.requestId);
        const status = message.params.response.status;
        const url = message.params.response.url;
        if (status >= 400 && !blockedUrls.has(url)) {
          results.http_failures.push({ url, method: meta?.method ?? "?", status });
        }
      }
    });

    // ---- read-only guard: block every non-GET/HEAD/OPTIONS request to the
    // app's own API, EXCEPT the two read-via-POST endpoints this check's own
    // login+view flow legitimately exercises — confirmed by reading the
    // router source, not guessed:
    //   - POST /v1/graph/query       — an anchored subgraph READ (no
    //     `require_role` mutation gate; routers/graph.py's own docstring:
    //     "reads are trimmed at source"). This check calls it itself.
    //   - POST /v1/principals/resolve — id-to-display-name lookup, issued
    //     automatically by the console shell right after login to label an
    //     actor; routers/principals.py shows it only reads
    //     `state.perm_store.display_names` and emits an AUDIT record of the
    //     read (`api.principal.resolve`) — the same "every read is audited"
    //     pattern as graph reads, not a state mutation.
    // Anything else non-GET (approve, reject, sync trigger, draft create,
    // ...) is failed at the network layer and recorded — the run must not
    // merely trust that nothing was clicked.
    const SAFE_READ_POST_SUFFIXES = ["/v1/graph/query", "/v1/principals/resolve"];
    await cdp.send("Fetch.enable", { patterns: [{ urlPattern: "*/api/*" }] }, sessionId);
    cdp.on((message) => {
      if (message.sessionId !== sessionId) return;
      if (message.method !== "Fetch.requestPaused") return;
      const { requestId, request } = message.params;
      const method = request.method;
      const isSafeRead =
        method === "GET" ||
        method === "HEAD" ||
        method === "OPTIONS" ||
        (method === "POST" &&
          SAFE_READ_POST_SUFFIXES.some((suffix) => new URL(request.url).pathname.endsWith(suffix)));
      if (isSafeRead) {
        cdp.send("Fetch.continueRequest", { requestId }, sessionId).catch(() => {});
      } else {
        blockedUrls.add(request.url);
        results.blocked_mutations.push({ method, url: request.url });
        cdp
          .send("Fetch.failRequest", { requestId, errorReason: "BlockedByClient" }, sessionId)
          .catch(() => {});
      }
    });

    // ---- 3: one real login ----
    await cdp.send("Page.navigate", { url: `${BASE_URL}/` }, sessionId);

    const evalHref = async () => {
      const { result } = await cdp.send(
        "Runtime.evaluate",
        { expression: "window.location.href", returnByValue: true },
        sessionId,
      );
      return result.value;
    };

    let loginReached = false;
    try {
      await waitUntil(
        async () => {
          const href = await evalHref();
          return href && new URL(href).origin !== new URL(BASE_URL).origin;
        },
        { timeoutMs: 20000, description: "redirect to the Keycloak login page" },
      );
      loginReached = true;
    } catch (error) {
      record("login: redirected to Keycloak", false, error.message);
    }

    let loginSubmitted = false;
    if (loginReached) {
      record("login: redirected to Keycloak", true);
      const fillAndSubmit = `(() => {
        const set = (el, value) => {
          const proto = Object.getPrototypeOf(el);
          const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
          if (setter) setter.call(el, value); else el.value = value;
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
        };
        const user = document.querySelector('#username, input[name="username"]');
        const pass = document.querySelector('#password, input[name="password"]');
        if (!user || !pass) return false;
        set(user, ${JSON.stringify(OPS_USER)});
        set(pass, ${JSON.stringify(opsPassword)});
        const btn = document.querySelector('#kc-login, button[type="submit"], input[type="submit"]');
        if (!btn) return false;
        btn.click();
        return true;
      })()`;
      try {
        await waitUntil(
          async () => {
            const { result } = await cdp.send(
              "Runtime.evaluate",
              {
                expression:
                  "!!(document.querySelector('#username, input[name=\"username\"]') && document.querySelector('#password, input[name=\"password\"]'))",
                returnByValue: true,
              },
              sessionId,
            );
            return result.value === true;
          },
          { timeoutMs: 20000, description: "Keycloak login form to render" },
        );
        const { result } = await cdp.send(
          "Runtime.evaluate",
          { expression: fillAndSubmit, returnByValue: true },
          sessionId,
        );
        loginSubmitted = result.value === true;
        record("login: form submitted", loginSubmitted);
      } catch (error) {
        record("login: form submitted", false, error.message);
      }
    }

    // The password is only needed up to the point of submission. Wipe it now
    // regardless of outcome — it must never linger in memory longer than it
    // has to.
    opsPassword = "\0".repeat(opsPassword.length);

    let loginReturned = false;
    if (loginSubmitted) {
      try {
        await waitUntil(
          async () => {
            const href = await evalHref();
            return href && new URL(href).origin === new URL(BASE_URL).origin;
          },
          { timeoutMs: NAV_TIMEOUT_MS, description: "return to the console after login" },
        );
        loginReturned = true;
      } catch (error) {
        record("login: returned to the console", false, error.message);
      }
    }

    let consoleRendered = false;
    if (loginReturned) {
      try {
        await waitUntil(
          async () => {
            const { result } = await cdp.send(
              "Runtime.evaluate",
              {
                expression:
                  "document.readyState === 'complete' && !!document.querySelector('.ne-shell')",
                returnByValue: true,
              },
              sessionId,
            );
            return result.value === true;
          },
          { timeoutMs: 15000, description: "the console shell to render" },
        );
        consoleRendered = true;
      } catch (error) {
        record("login: console rendered", false, error.message);
      }
      record("login: returned to the console", true);
      if (consoleRendered) record("login: console rendered", true);
    }

    // ---- 4: exact graph counts, via the same authenticated session ----
    if (consoleRendered) {
      const fetchGraph = `(async () => {
        const findToken = () => {
          for (let i = 0; i < sessionStorage.length; i++) {
            const key = sessionStorage.key(i);
            if (!key || !key.startsWith("oidc.user:")) continue;
            try {
              const parsed = JSON.parse(sessionStorage.getItem(key));
              if (parsed && parsed.access_token) return parsed.access_token;
            } catch {}
          }
          return null;
        };
        const token = findToken();
        if (!token) return { error: "no OIDC access token found in sessionStorage" };
        const headers = { Authorization: "Bearer " + token };

        // Paginate entity discovery to enumerate every visible node.
        const keys = [];
        const byClass = {};
        let cursor = null;
        let discoveryStatus = null;
        for (let page = 0; page < 10; page++) {
          const qs = new URLSearchParams({ limit: "100" });
          if (cursor) qs.set("cursor", cursor);
          const res = await fetch("/api/v1/graph/entities?" + qs.toString(), { headers });
          discoveryStatus = res.status;
          if (res.status !== 200) return { error: "graph/entities status " + res.status };
          const body = await res.json();
          for (const e of body.entities) {
            keys.push(e.key);
            byClass[e.entity_class] = (byClass[e.entity_class] || 0) + 1;
          }
          cursor = body.next_cursor;
          if (!cursor) break;
        }

        if (keys.length === 0) {
          return {
            error: "graph/entities returned zero entities",
            discoveryStatus,
            discoveryCount: 0,
            byClass,
          };
        }

        const queryRes = await fetch("/api/v1/graph/query", {
          method: "POST",
          headers: { ...headers, "Content-Type": "application/json" },
          body: JSON.stringify({
            anchor_keys: keys,
            hops: 1,
            limit_nodes: 500,
            limit_edges: 2000,
          }),
        });
        const queryStatus = queryRes.status;
        if (queryStatus !== 200) return { error: "graph/query status " + queryStatus };
        const queryBody = await queryRes.json();
        return {
          discoveryStatus,
          discoveryCount: keys.length,
          byClass,
          queryStatus,
          nodes: queryBody.nodes.length,
          edges: queryBody.edges.length,
          truncated: queryBody.truncated,
        };
      })()`;
      try {
        const { result } = await cdp.send(
          "Runtime.evaluate",
          { expression: fetchGraph, awaitPromise: true, returnByValue: true },
          sessionId,
        );
        const graph = result.value;
        results.graph = graph;
        if (graph.error) {
          record("graph: exact 45 nodes / 40 relationships / truncated=false", false, graph.error);
        } else {
          const ok =
            graph.discoveryCount === 45 &&
            graph.nodes === 45 &&
            graph.edges === 40 &&
            graph.truncated === false;
          record(
            "graph: exact 45 nodes / 40 relationships / truncated=false",
            ok,
            `discovery=${graph.discoveryCount} nodes=${graph.nodes} edges=${graph.edges} ` +
              `truncated=${graph.truncated} byClass=${JSON.stringify(graph.byClass)}`,
          );
        }
      } catch (error) {
        record("graph: exact 45 nodes / 40 relationships / truncated=false", false, error.message);
      }
    } else {
      record(
        "graph: exact 45 nodes / 40 relationships / truncated=false",
        false,
        "skipped — login/render did not complete",
      );
    }

    // Let any trailing console/network activity settle before tallying.
    await new Promise((r) => setTimeout(r, 1000));
  } finally {
    try {
      cdp.close();
    } catch {
      /* already closed */
    }
    child.kill("SIGKILL");
    await new Promise((doneWait) =>
      child.exitCode === null ? child.once("exit", doneWait) : doneWait(),
    );
    try {
      rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
    } catch {
      /* the OS will reap it */
    }
  }

  record(
    "zero console errors",
    results.console_errors.length === 0,
    `count=${results.console_errors.length}`,
  );
  record(
    "zero page errors",
    results.page_errors.length === 0,
    `count=${results.page_errors.length}`,
  );
  record(
    "zero failed HTTP requests",
    results.http_failures.length === 0,
    `count=${results.http_failures.length}`,
  );
  record(
    "zero blocked (non-GET) mutation attempts",
    results.blocked_mutations.length === 0,
    `count=${results.blocked_mutations.length}`,
  );
}

// ------------------------------------------------------------------- main
try {
  await checkHealth();
  await runBrowserBattery();
} catch (error) {
  process.stderr.write(`post-cutover-quick-check: unhandled error: ${error.stack || error}\n`);
  process.stdout.write(`${JSON.stringify(results, null, 2)}\n`);
  process.exit(2);
}

results.finished_at = new Date().toISOString();
process.stdout.write(`${JSON.stringify(results, null, 2)}\n`);

const failed = results.checks.filter((c) => !c.ok);
if (failed.length > 0) {
  process.stderr.write(`post-cutover-quick-check: ${failed.length} assertion(s) failed\n`);
  process.exit(1);
}
process.stdout.write("post-cutover-quick-check: all quick-check assertions passed\n");
process.exit(0);
