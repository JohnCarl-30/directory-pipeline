/**
 * Writes TypeScript-built instances for tests/test_contract_fixtures.py to
 * validate with Pydantic. This direction catches what parsing Python's JSON
 * cannot: a Zod default Python would refuse, or one Python reads differently.
 */
import { mkdirSync, writeFileSync } from "node:fs";

import { expect, it } from "vitest";

import { MODELS } from "../src/models.js";
import { FIXTURES, loadFixture, type MinimalCase, type ModelCase } from "./fixtures.js";

it("writes TypeScript-built instances for the Pydantic check", () => {
  const minimal = loadFixture<{ cases: MinimalCase[] }>("minimal.json").cases;
  const models = loadFixture<{ cases: ModelCase[] }>("models.json").cases;

  const cases = [
    // Defaults filled in by Zod, including timestamps TypeScript generated.
    ...minimal.map((c) => ({
      model: c.model,
      case: `defaults/${c.case}`,
      data: MODELS[c.model].parse(c.input),
    })),
    // Python's own values, after a pass through Zod.
    ...models.map((c) => ({
      model: c.model,
      case: `roundtrip/${c.case}`,
      data: MODELS[c.model].parse(c.data),
    })),
  ];

  const dir = new URL("from-ts/", FIXTURES);
  mkdirSync(dir, { recursive: true });
  writeFileSync(new URL("cases.json", dir), `${JSON.stringify({ cases }, null, 2)}\n`);
  expect(cases.length).toBe(minimal.length + models.length);
});
