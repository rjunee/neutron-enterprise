// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { AppBootstrap } from "./AppBootstrap";
import { applyTheme, readTheme } from "./rail/theme";
import { applyDensity, readDensity } from "./primitives/density";
import "./styles/fonts.css";
import "./styles/tokens.css";
import "./styles/primitives.css";
import "./styles/system.css";
import "./styles.css";
import "./styles/shell.css";
import "./styles/table.css";

// Applied BEFORE the first paint, not in a React effect: setting data-theme
// after mount makes an explicitly-light console flash dark on an OS-dark machine
// (and the reverse) on every single load.
applyTheme(readTheme());
// Same reasoning for density: applying it after mount reflows every table on
// screen one frame after the first paint.
applyDensity(readDensity());

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("root element missing");

// NE-CONSOLE-ISSUER: AppBootstrap fetches /config.json and initializes OIDC from
// the RUNTIME config before rendering App. StrictMode double-invokes the bootstrap
// effect (a double idempotent GET) — harmless; guarded by a cancelled flag.
createRoot(rootEl).render(
  <StrictMode>
    <AppBootstrap>
      <App />
    </AppBootstrap>
  </StrictMode>,
);
