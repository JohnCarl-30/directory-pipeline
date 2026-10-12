import { describe, expect, it } from "vitest";

import { completeness, pyIsoformat, toDocument } from "../src/document.js";
import { EnrichedCompany } from "../src/models.js";
import { loadFixture, type DocumentCase } from "./fixtures.js";

const documents = loadFixture<{ cases: DocumentCase[] }>("documents.json").cases;

describe("toDocument matches EnrichedCompany.to_document", () => {
  it.each(documents.map((c) => [c.case, c] as const))("%s", (_n, c) => {
    expect(toDocument(EnrichedCompany.parse(c.input))).toEqual(c.document);
  });
});

describe("pyIsoformat", () => {
  it.each([
    ["2026-10-12T23:10:24.954613Z", "2026-10-12T23:10:24.954613+00:00"],
    ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00+00:00"],
    ["2026-03-09T08:00:00.000005+08:00", "2026-03-09T08:00:00.000005+08:00"],
    // A timestamp TypeScript created itself: milliseconds, as toISOString writes.
    ["2026-10-12T23:10:24.123Z", "2026-10-12T23:10:24.123000+00:00"],
    ["2026-10-12T23:10:24.000Z", "2026-10-12T23:10:24+00:00"],
    ["2026-10-12T23:10:24", "2026-10-12T23:10:24"],
  ])("%s -> %s", (input, expected) => {
    expect(pyIsoformat(input)).toBe(expected);
  });

  it("rejects a non-ISO string", () => {
    expect(() => pyIsoformat("yesterday")).toThrow(RangeError);
  });
});

describe("completeness", () => {
  it("counts an empty categories list as missing, as Python's bool([]) does", () => {
    const doc = toDocument(
      EnrichedCompany.parse({
        company: {
          record_id: "r",
          source: "s",
          source_id: "i",
          source_url: "u",
          name: "N",
          categories: [],
        },
      }),
    );
    expect(doc.categories).toEqual([]);
    expect(completeness(doc)).toBe(0);
  });
});
