import { readFileSync } from "node:fs";

import type { ModelName } from "../src/models.js";

/** contract/fixtures at the repo root: Python writes it, both languages read it. */
export const FIXTURES = new URL("../../../contract/fixtures/", import.meta.url);

// The caller names the fixture's shape; JSON.parse cannot check it, and the
// tests that use each fixture are what would catch a wrong one.
// eslint-disable-next-line @typescript-eslint/no-unnecessary-type-parameters
export function loadFixture<T>(name: string): T {
  return JSON.parse(readFileSync(new URL(name, FIXTURES), "utf8")) as T;
}

export interface ModelCase {
  model: ModelName;
  case: string;
  data: unknown;
}

export interface MinimalCase {
  model: ModelName;
  case: string;
  input: unknown;
  expected: Record<string, unknown>;
  now_fields: string[];
}

export interface InvalidCase {
  model: ModelName;
  case: string;
  data: unknown;
}

export interface DocumentCase {
  case: string;
  input: unknown;
  document: Record<string, unknown>;
}

export interface IdFixtures {
  record_ids: { source: string; source_id: string; expected: string }[];
  content_hashes: { html: string; expected: string }[];
  source_ids: { url: string; expected: string }[];
}

export interface RoundingFixtures {
  cases: [x: number, ndigits: number, expected: number][];
}
