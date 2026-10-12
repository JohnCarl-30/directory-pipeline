import { describe, expect, it } from "vitest";

import { IsoDateTime, MODELS, SCHEMA_VERSION } from "../src/models.js";
import { loadFixture, type InvalidCase, type MinimalCase, type ModelCase } from "./fixtures.js";

const models = loadFixture<{ cases: ModelCase[] }>("models.json").cases;
const minimal = loadFixture<{ cases: MinimalCase[] }>("minimal.json").cases;
const invalid = loadFixture<{ cases: InvalidCase[] }>("invalid.json").cases;

const named = <T extends { model: string; case: string }>(cases: T[]) =>
  cases.map((c) => [`${c.model}/${c.case}`, c] as const);

/** Remove a dotted path from a parsed object and return what was there. */
function takePath(value: unknown, path: string): unknown {
  const [head, ...rest] = path.split(".");
  if (head === undefined || typeof value !== "object" || value === null) {
    throw new Error(`no ${path} in ${JSON.stringify(value)}`);
  }
  const record = value as Record<string, unknown>;
  if (rest.length > 0) return takePath(record[head], rest.join("."));
  const taken = record[head];
  Reflect.deleteProperty(record, head);
  return taken;
}

describe("Python's JSON parses and serializes back unchanged", () => {
  it.each(named(models))("%s", (_name, c) => {
    const parsed = MODELS[c.model].parse(c.data);
    const roundTripped: unknown = JSON.parse(JSON.stringify(parsed));
    expect(roundTripped).toEqual(c.data);
  });
});

describe("defaults match Pydantic's", () => {
  it.each(named(minimal))("%s", (_name, c) => {
    const parsed = MODELS[c.model].parse(c.input) as Record<string, unknown>;
    for (const path of c.now_fields) {
      expect(IsoDateTime.safeParse(takePath(parsed, path)).success).toBe(true);
    }
    expect(parsed).toEqual(c.expected);
  });
});

describe("inputs Pydantic rejects are rejected", () => {
  it.each(named(invalid))("%s", (_name, c) => {
    expect(MODELS[c.model].safeParse(c.data).success).toBe(false);
  });
});

it("SCHEMA_VERSION matches Python", () => {
  expect(SCHEMA_VERSION).toBe(4);
});

it("a TypeScript default timestamp is a valid IsoDateTime", () => {
  expect(IsoDateTime.safeParse(new Date().toISOString()).success).toBe(true);
});
