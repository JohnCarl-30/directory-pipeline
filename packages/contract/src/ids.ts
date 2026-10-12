/**
 * Keys that must be computed identically in both languages. A drift here is
 * silent: documents land under a different id, or incremental re-crawl looks
 * up hashes under keys nothing was written with, and nothing raises.
 */
import { createHash } from "node:crypto";

/** CompanyRecord.make_record_id: re-running the pipeline overwrites, never duplicates. */
export function recordId(source: string, sourceId: string): string {
  return createHash("sha1").update(`${source}:${sourceId}`, "utf8").digest("hex");
}

/** RawListing.content_hash: skips unchanged pages on re-crawl. */
export function contentHash(html: string): string {
  return createHash("sha256").update(html, "utf8").digest("hex").slice(0, 16);
}

/**
 * scraping.crawler.source_id_from_url: the last non-empty path segment, stable
 * across query-string churn. Like the Python, it keeps a #fragment.
 */
export function sourceIdFromUrl(url: string): string {
  const path = (url.split("?", 1)[0] ?? "").replace(/\/+$/, "");
  const last = path.slice(path.lastIndexOf("/") + 1);
  return last || path;
}
