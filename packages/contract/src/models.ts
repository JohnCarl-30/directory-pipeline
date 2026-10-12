/**
 * Zod mirror of src/directory_pipeline/domain/models.py.
 *
 * Python is the source of truth while both languages coexist. Every schema is
 * checked against JSON the Python models wrote (contract/fixtures), so a drift
 * in either direction fails CI rather than corrupting a Temporal payload.
 *
 * Mapping rules:
 * - `extra="forbid"` is `z.strictObject`.
 * - `str | None = None` is `.nullable().default(null)`.
 * - A nested model's default uses `.prefault({})`, not `.default({})`: Zod 4
 *   returns a `.default` value as-is, which would skip the inner defaults.
 * - Datetimes stay ISO strings. Python writes microseconds; a `Date` keeps
 *   milliseconds, so converting would rewrite every timestamp passing through.
 */
import { z } from "zod";

export const SCHEMA_VERSION = 4;

/** The shapes Pydantic writes: optional 1-6 digit fraction, `Z`, an offset, or naive. */
export const IsoDateTime = z
  .string()
  .regex(
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})?$/,
    "expected an ISO-8601 datetime",
  );
export type IsoDateTime = z.output<typeof IsoDateTime>;

const now = (): string => new Date().toISOString();
const nullableString = () => z.string().nullable().default(null);
const nullableInt = () => z.number().int().nullable().default(null);
const stringList = () => z.array(z.string()).default(() => []);

/** How a record's fields were obtained, cheapest and most reliable first. */
export const ExtractionMethod = z.enum(["dom", "text", "llm", "llm_repaired"]);
export type ExtractionMethod = z.output<typeof ExtractionMethod>;

export const RawListing = z.strictObject({
  source: z.string(),
  source_id: z.string(),
  url: z.string(),
  html: z.string(),
  fetched_at: IsoDateTime.default(now),
  http_status: z.number().int().default(200),
  via_proxy: nullableString(),
});
export type RawListing = z.output<typeof RawListing>;

export const Address = z.strictObject({
  line1: nullableString(),
  line2: nullableString(),
  city: nullableString(),
  region: nullableString(),
  postal_code: nullableString(),
  country: z.string().default("US"),
});
export type Address = z.output<typeof Address>;

export const Contact = z.strictObject({
  phone_e164: nullableString(),
  phone_raw: nullableString(),
  email: nullableString(),
  website: nullableString(),
});
export type Contact = z.output<typeof Contact>;

export const CompanyRecord = z.strictObject({
  record_id: z.string(),
  source: z.string(),
  source_id: z.string(),
  source_url: z.string(),
  name: z.string().trim().min(1, "company name is required"),
  legal_name: nullableString(),
  name_normalized: z.string().default(""),
  categories: stringList(),
  description: nullableString(),
  address: Address.prefault({}),
  contact: Contact.prefault({}),
  employee_count: nullableInt(),
  founded_year: nullableInt(),
  extraction_method: ExtractionMethod.default("dom"),
  extraction_confidence: z.number().default(1.0),
  content_hash: z.string().default(""),
  first_seen_at: IsoDateTime.default(now),
  last_seen_at: IsoDateTime.default(now),
});
export type CompanyRecord = z.output<typeof CompanyRecord>;

export const Enrichment = z.strictObject({
  provider: z.string(),
  provider_company_id: nullableString(),
  industry: nullableString(),
  naics: nullableString(),
  revenue_usd: nullableInt(),
  employee_count: nullableInt(),
  linkedin_url: nullableString(),
  technologies: stringList(),
  confidence: z.number().default(0.0),
  fetched_at: IsoDateTime.default(now),
  cache_hit: z.boolean().default(false),
});
export type Enrichment = z.output<typeof Enrichment>;

export const EnrichedCompany = z.strictObject({
  company: CompanyRecord,
  enrichment: Enrichment.nullable().default(null),
  cluster_id: nullableString(),
  duplicate_of: nullableString(),
  schema_version: z.number().int().default(SCHEMA_VERSION),
});
export type EnrichedCompany = z.output<typeof EnrichedCompany>;

// --- Workflow I/O: replayed by running workflows, so add-only like the rest. ---

export const CrawlRequest = z.strictObject({
  source: z.string().default("demo-directory"),
  categories: z.array(z.string()).default(() => ["software"]),
  max_pages: z.number().int().default(5),
  batch_size: z.number().int().default(25),
  enrich: z.boolean().default(true),
  index_alias: nullableString(),
  force_refetch: z.boolean().default(false),
  pending_urls: stringList(),
  carried_totals: z.record(z.string(), z.number().int()).default(() => ({})),
});
export type CrawlRequest = z.output<typeof CrawlRequest>;

export const CrawlResult = z.strictObject({
  run_id: z.string(),
  pages_crawled: z.number().int().default(0),
  listings_found: z.number().int().default(0),
  records_extracted: z.number().int().default(0),
  records_enriched: z.number().int().default(0),
  duplicates_collapsed: z.number().int().default(0),
  documents_indexed: z.number().int().default(0),
  failures: stringList(),
  index_name: nullableString(),
  started_at: IsoDateTime.default(now),
  finished_at: IsoDateTime.nullable().default(null),
});
export type CrawlResult = z.output<typeof CrawlResult>;

export const ReindexRequest = z.strictObject({
  alias: z.string(),
  reason: z.string().default("mapping change"),
  target_schema_version: z.number().int().default(SCHEMA_VERSION),
  wait_for_completion: z.boolean().default(false),
  drop_old_index: z.boolean().default(false),
});
export type ReindexRequest = z.output<typeof ReindexRequest>;

export const ReindexResult = z.strictObject({
  alias: z.string(),
  // Required but nullable in Python (`str | None` with no default): absent is an error.
  source_index: z.string().nullable(),
  target_index: z.string(),
  documents_copied: z.number().int(),
  swapped: z.boolean(),
  old_index_dropped: z.boolean().default(false),
  duration_s: z.number().default(0.0),
});
export type ReindexResult = z.output<typeof ReindexResult>;

/** Every model by its Python class name, as the fixtures refer to them. */
export const MODELS = {
  RawListing,
  Address,
  Contact,
  CompanyRecord,
  Enrichment,
  EnrichedCompany,
  CrawlRequest,
  CrawlResult,
  ReindexRequest,
  ReindexResult,
} as const;
export type ModelName = keyof typeof MODELS;
