// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The typed-args form MODEL — schema→field mapping and value coercion. Pure
// logic, no DOM: proves the form is GENERATED from the strict args_schema
// (str/int/bool/date/datetime), carries pattern/maxLength/required, flags an
// unsupported type fail-closed, and coerces DOM values to JSON (int rejects
// NaN/float; datetime-local→naive ISO; bool passthrough).

import { describe, expect, it } from "vitest";
import type { ArgsJsonSchema } from "../../api/types";
import { coerceArgValue, hasUnsupportedField, parseArgsSchema } from "./argsForm";

const MIXED_SCHEMA: ArgsJsonSchema = {
  title: "mixed_Args",
  type: "object",
  additionalProperties: false,
  required: ["s", "n", "d"],
  properties: {
    s: { title: "S", type: "string", pattern: "^[a-z]{1,8}$", maxLength: 8 },
    n: { title: "N", type: "integer" },
    b: { title: "B", type: "boolean" },
    d: { title: "D", type: "string", format: "date" },
    dt: { title: "DT", type: "string", format: "date-time" },
  },
};

describe("parseArgsSchema", () => {
  it("leaves the title absent when the schema carries none, so the field falls back to its name", () => {
    const fields = parseArgsSchema({
      type: "object",
      properties: { q: { type: "string" } },
    });
    expect(fields[0]?.title).toBeUndefined();
  });

  it("maps str/int/bool/date/datetime kinds and carries pattern/maxLength/required", () => {
    const fields = parseArgsSchema(MIXED_SCHEMA);
    // `title` rides along because REDESIGN §7 labels each input with the
    // schema's own field title rather than its wire name.
    expect(fields).toEqual([
      {
        name: "s",
        kind: "string",
        required: true,
        pattern: "^[a-z]{1,8}$",
        maxLength: 8,
        title: "S",
      },
      { name: "n", kind: "int", required: true, title: "N" },
      { name: "b", kind: "bool", required: false, title: "B" },
      { name: "d", kind: "date", required: true, title: "D" },
      { name: "dt", kind: "datetime", required: false, title: "DT" },
    ]);
  });

  it("maps a plain string (an iri arg is wire-indistinguishable) to kind string", () => {
    const schema: ArgsJsonSchema = {
      type: "object",
      required: ["account"],
      properties: { account: { title: "Account", type: "string" } },
    };
    const fields = parseArgsSchema(schema);
    expect(fields).toEqual([{ name: "account", kind: "string", required: true, title: "Account" }]);
  });

  it("carries a server-authored string enum as the only permitted values", () => {
    const fields = parseArgsSchema({
      type: "object",
      required: ["status"],
      properties: {
        status: { title: "Status", type: "string", enum: ["open", "pending", "closed"] },
      },
    });
    expect(fields).toEqual([
      {
        name: "status",
        kind: "enum",
        required: true,
        title: "Status",
        permittedValues: ["open", "pending", "closed"],
      },
    ]);
    expect(coerceArgValue("enum", "pending", fields[0]?.permittedValues)).toEqual({
      ok: true,
      value: "pending",
    });
    expect(coerceArgValue("enum", "invented", fields[0]?.permittedValues)).toEqual({
      ok: false,
      reason: "must be one of: open, pending, closed",
    });
  });

  it("fails closed on malformed enum schema instead of guessing a text input", () => {
    const fields = parseArgsSchema({
      type: "object",
      properties: { status: { type: "string", enum: [] } },
    });
    expect(fields[0]?.kind).toBe("unsupported");
    expect(hasUnsupportedField(fields)).toBe(true);
  });

  it("returns an empty field list for a zero-arg template", () => {
    expect(parseArgsSchema({ type: "object", properties: {} })).toEqual([]);
    expect(parseArgsSchema({ type: "object" })).toEqual([]);
  });

  it("flags an unrecognized type as unsupported (fail-closed)", () => {
    const schema: ArgsJsonSchema = {
      type: "object",
      required: ["blob"],
      properties: { blob: { title: "Blob", type: "array" } },
    };
    const fields = parseArgsSchema(schema);
    expect(fields[0]?.kind).toBe("unsupported");
    expect(hasUnsupportedField(fields)).toBe(true);
  });

  it("hasUnsupportedField is false for an all-supported form", () => {
    expect(hasUnsupportedField(parseArgsSchema(MIXED_SCHEMA))).toBe(false);
  });

  // NE-KG-MULTIKEY: the server's scalar|list union for a list_max arg — the
  // EXACT Pydantic shape (`anyOf` of a constrained scalar and an array whose
  // `items` repeat the identical constraints, plus min/maxItems).
  it("maps the server's scalar-or-list union to its element kind + listMax", () => {
    const scalar = { type: "string", pattern: "^mer-cus-[0-9]{3}$", maxLength: 11 };
    const fields = parseArgsSchema({
      type: "object",
      required: ["q_customer_id"],
      properties: {
        q_customer_id: {
          title: "Q Customer Id",
          anyOf: [scalar, { type: "array", items: scalar, minItems: 1, maxItems: 24 }],
        },
      },
    });
    expect(fields).toEqual([
      {
        name: "q_customer_id",
        kind: "string",
        required: true,
        pattern: "^mer-cus-[0-9]{3}$",
        maxLength: 11,
        title: "Q Customer Id",
        listMax: 24,
      },
    ]);
    expect(hasUnsupportedField(fields)).toBe(false);
  });

  it("maps an enum scalar-or-list union with the closed vocabulary per member", () => {
    const scalar = { type: "string", enum: ["Columbia", "Richmond"] };
    const fields = parseArgsSchema({
      type: "object",
      required: ["q_county"],
      properties: {
        q_county: {
          title: "Q County",
          anyOf: [scalar, { type: "array", items: scalar, minItems: 1, maxItems: 4 }],
        },
      },
    });
    expect(fields).toEqual([
      {
        name: "q_county",
        kind: "enum",
        required: true,
        permittedValues: ["Columbia", "Richmond"],
        title: "Q County",
        listMax: 4,
      },
    ]);
  });

  it("stays fail-closed, WITH a named reason, for any other anyOf shape", () => {
    const drifted = parseArgsSchema({
      type: "object",
      properties: {
        odd: {
          anyOf: [
            { type: "string", maxLength: 8 },
            { type: "array", items: { type: "string", maxLength: 9 }, minItems: 1, maxItems: 4 },
          ],
        },
      },
    });
    expect(drifted[0]?.kind).toBe("unsupported");
    expect(drifted[0]?.unsupportedReason).toMatch(/anyOf/);

    const intList = { type: "integer" };
    const ints = parseArgsSchema({
      type: "object",
      properties: {
        n: { anyOf: [intList, { type: "array", items: intList, minItems: 1, maxItems: 4 }] },
      },
    });
    expect(ints[0]?.kind).toBe("unsupported");
    expect(hasUnsupportedField(ints)).toBe(true);
  });

  it("names the missing type in the unsupported reason for a bare field", () => {
    const fields = parseArgsSchema({
      type: "object",
      properties: { blob: { type: "array" } },
    });
    expect(fields[0]?.unsupportedReason).toMatch(/type 'array'/);
  });
});

describe("coerceArgValue", () => {
  it("int: accepts integers, rejects float and non-numeric", () => {
    expect(coerceArgValue("int", "42")).toEqual({ ok: true, value: 42 });
    expect(coerceArgValue("int", "1.5")).toEqual({ ok: false, reason: "'1.5' is not an integer" });
    expect(coerceArgValue("int", "x")).toEqual({ ok: false, reason: "'x' is not an integer" });
    expect(coerceArgValue("int", "")).toMatchObject({ ok: false });
  });

  it("int: rejects values that Number would silently round beyond the safe range", () => {
    expect(coerceArgValue("int", "9007199254740991")).toEqual({
      ok: true,
      value: Number.MAX_SAFE_INTEGER,
    });
    expect(coerceArgValue("int", "-9007199254740991")).toEqual({
      ok: true,
      value: Number.MIN_SAFE_INTEGER,
    });
    expect(coerceArgValue("int", "9007199254740993")).toEqual({
      ok: false,
      reason: "'9007199254740993' is not an integer",
    });
    expect(coerceArgValue("int", "-9007199254740993")).toEqual({
      ok: false,
      reason: "'-9007199254740993' is not an integer",
    });
  });

  it("bool: passes the checkbox boolean through", () => {
    expect(coerceArgValue("bool", true)).toEqual({ ok: true, value: true });
    expect(coerceArgValue("bool", false)).toEqual({ ok: true, value: false });
  });

  it("date: passes YYYY-MM-DD through, rejects empty", () => {
    expect(coerceArgValue("date", "2026-07-07")).toEqual({ ok: true, value: "2026-07-07" });
    expect(coerceArgValue("date", "")).toMatchObject({ ok: false });
  });

  it("datetime: appends :00 to a datetime-local value (naive ISO)", () => {
    expect(coerceArgValue("datetime", "2026-07-07T09:30")).toEqual({
      ok: true,
      value: "2026-07-07T09:30:00",
    });
    expect(coerceArgValue("datetime", "")).toMatchObject({ ok: false });
  });

  it("string: passes through unchanged", () => {
    expect(coerceArgValue("string", "Acme Corp")).toEqual({ ok: true, value: "Acme Corp" });
  });

  it("unsupported: never coerces", () => {
    expect(coerceArgValue("unsupported", "x")).toMatchObject({ ok: false });
  });
});
