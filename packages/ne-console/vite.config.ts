/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA itself only ever calls the RELATIVE API base (src/api/config.ts →
// `/api`). This dev-only proxy forwards that base to a locally running governed
// backend (ne-serve/ne-gateway) so `npm run dev` works against `ne dev up`.
// It changes nothing about the thin-client invariant: the browser never learns
// an absolute, ungoverned data-plane URL — the proxy target is dev-host only.
// Dev-only proxy target. Defaults to a local `ne dev up`; point it at a running
// deployment with NE_CONSOLE_DEV_API when iterating on UI against real data
// (e.g. NE_CONSOLE_DEV_API=https://demo.example.host npm run dev). Dev server
// only — never read by the built bundle, so the thin-client invariant holds:
// the browser still calls the relative `/api` base and never learns an
// absolute data-plane URL.
const DEV_API_TARGET = process.env.NE_CONSOLE_DEV_API ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: DEV_API_TARGET,
        changeOrigin: true,
        secure: true,
      },
      // NE-CONSOLE-ISSUER: dev mode fetches the runtime OIDC config from the
      // local ne-api — no baked issuer; `npm run dev` login needs `ne dev up`.
      "/config.json": {
        target: DEV_API_TARGET,
        changeOrigin: true,
        secure: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
