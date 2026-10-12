import { describe, expect, it } from "vitest";

import { contentHash, recordId, sourceIdFromUrl } from "../src/ids.js";
import { loadFixture, type IdFixtures } from "./fixtures.js";

const ids = loadFixture<IdFixtures>("ids.json");

describe("recordId matches CompanyRecord.make_record_id", () => {
  it.each(ids.record_ids.map((c) => [`${c.source}:${c.source_id}`, c] as const))("%s", (_n, c) => {
    expect(recordId(c.source, c.source_id)).toBe(c.expected);
  });
});

describe("contentHash matches RawListing.content_hash", () => {
  it.each(ids.content_hashes.map((c) => [JSON.stringify(c.html), c] as const))("%s", (_n, c) => {
    expect(contentHash(c.html)).toBe(c.expected);
  });
});

describe("sourceIdFromUrl matches scraping.crawler.source_id_from_url", () => {
  it.each(ids.source_ids.map((c) => [JSON.stringify(c.url), c] as const))("%s", (_n, c) => {
    expect(sourceIdFromUrl(c.url)).toBe(c.expected);
  });
});
