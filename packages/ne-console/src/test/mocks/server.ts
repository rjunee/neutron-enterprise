// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { setupServer } from "msw/node";
import { handlers } from "./handlers";

/** The shared MSW node server; lifecycle is driven from src/test/setup.ts. */
export const server = setupServer(...handlers);
