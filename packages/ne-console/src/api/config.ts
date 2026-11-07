// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The SINGLE source of the governed API base URL. This is the ONLY file in the
// codebase permitted to read import.meta.env.VITE_API_BASE_URL (enforced by the
// thin-client guardian test). It defaults to the RELATIVE path "/api" so the
// SPA can never be pointed at an absolute, ungoverned data-plane host by
// omission — any deployment override is an explicit, reviewable env value that
// still must resolve to the governed ne-serve/ne-gateway surface.

const env = import.meta.env as ImportMetaEnv & { readonly VITE_API_BASE_URL?: string };

export const API_BASE: string = env.VITE_API_BASE_URL ?? "/api";
