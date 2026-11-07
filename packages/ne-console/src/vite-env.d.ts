// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
/// <reference types="vite/client" />

// NE-CONSOLE-ISSUER: the baked-issuer build-time constants are GONE — the console
// now reads its OIDC config at runtime from /config.json (see src/api/runtimeConfig
// .ts). The only remaining build-time knob is the governed API base env var, named
// solely in src/api/config.ts (thin-client guardian rule d), which reads it via a
// local cast against vite/client's ambient ImportMetaEnv. No local ImportMetaEnv /
// ImportMeta declarations are needed here (they would re-introduce the banned
// tokens the structural guardian scans for).
