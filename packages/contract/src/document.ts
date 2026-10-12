/**
 * EnrichedCompany.to_document: the OpenSearch document shape. It must match the
 * Python byte for byte in meaning, since both languages will write documents to
 * the same index while the migration runs.
 */
import type { Address, EnrichedCompany, ExtractionMethod, IsoDateTime } from "./models.js";
import { pyRound } from "./pyround.js";

export interface CompanyDocument {
  record_id: string;
  schema_version: number;
  source: string;
  source_id: string;
  source_url: string;
  content_hash: string;
  name: string;
  name_normalized: string;
  legal_name: string | null;
  categories: string[];
  description: string | null;
  address: {
    line1: string | null;
    city: string | null;
    region: string | null;
    postal_code: string | null;
    country: string;
    full: string;
  };
  contact: { phone_e164: string | null; email: string | null; website: string | null };
  employee_count: number | null;
  founded_year: number | null;
  cluster_id: string | null;
  duplicate_of: string | null;
  is_canonical: boolean;
  extraction: { method: ExtractionMethod; confidence: number };
  first_seen_at: string;
  last_seen_at: string;
  enrichment?: {
    provider: string;
    industry: string | null;
    naics: string | null;
    revenue_usd: number | null;
    linkedin_url: string | null;
    technologies: string[];
    confidence: number;
    fetched_at: string;
  };
  completeness: number;
}

type DocumentBody = Omit<CompanyDocument, "completeness">;

const ISO_PARTS = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})?$/;

/**
 * What Python's `datetime.isoformat()` prints for the value Pydantic parses
 * from `value`. Not the same string Pydantic's JSON uses: isoformat writes UTC
 * as +00:00, not Z, and always prints six fraction digits or none.
 */
export function pyIsoformat(value: IsoDateTime): string {
  const match = ISO_PARTS.exec(value);
  if (!match) throw new RangeError(`not an ISO-8601 datetime: ${value}`);
  const [, base = "", fraction = "", offset = ""] = match;
  const micros = fraction.padEnd(6, "0");
  const fractionPart = /^0+$/.test(micros) ? "" : `.${micros}`;
  const zone = offset === "Z" ? "+00:00" : offset;
  return `${base}${fractionPart}${zone}`;
}

/** Address.as_text: the non-empty parts, comma-separated. */
export function addressAsText(address: Address): string {
  return [address.line1, address.line2, address.city, address.region, address.postal_code]
    .filter((part): part is string => Boolean(part))
    .join(", ");
}

/** _completeness: share of the fields that make a record useful, for ranking. */
export function completeness(doc: DocumentBody): number {
  const checks = [
    Boolean(doc.description),
    Boolean(doc.address.city),
    Boolean(doc.address.postal_code),
    Boolean(doc.contact.phone_e164),
    Boolean(doc.contact.website),
    doc.categories.length > 0, // [] is truthy in JS, falsy in Python
    doc.enrichment !== undefined,
    Boolean(doc.employee_count),
  ];
  return pyRound(checks.filter(Boolean).length / checks.length, 4);
}

export function toDocument(item: EnrichedCompany): CompanyDocument {
  const c = item.company;
  const e = item.enrichment;
  const body: DocumentBody = {
    record_id: c.record_id,
    schema_version: item.schema_version,
    source: c.source,
    source_id: c.source_id,
    source_url: c.source_url,
    content_hash: c.content_hash,
    name: c.name,
    name_normalized: c.name_normalized,
    legal_name: c.legal_name,
    categories: c.categories,
    description: c.description,
    address: {
      line1: c.address.line1,
      city: c.address.city,
      region: c.address.region,
      postal_code: c.address.postal_code,
      country: c.address.country,
      full: addressAsText(c.address),
    },
    contact: {
      phone_e164: c.contact.phone_e164,
      email: c.contact.email,
      website: c.contact.website,
    },
    // Python: `e.employee_count if e and e.employee_count else c.employee_count`,
    // so an enrichment count of 0 falls back to the company's own.
    employee_count: e?.employee_count ? e.employee_count : c.employee_count,
    founded_year: c.founded_year,
    cluster_id: item.cluster_id,
    duplicate_of: item.duplicate_of,
    is_canonical: item.duplicate_of === null,
    extraction: {
      method: c.extraction_method,
      confidence: pyRound(c.extraction_confidence, 4),
    },
    first_seen_at: pyIsoformat(c.first_seen_at),
    last_seen_at: pyIsoformat(c.last_seen_at),
  };
  if (e !== null) {
    body.enrichment = {
      provider: e.provider,
      industry: e.industry,
      naics: e.naics,
      revenue_usd: e.revenue_usd,
      linkedin_url: e.linkedin_url,
      technologies: e.technologies,
      confidence: pyRound(e.confidence, 4),
      fetched_at: pyIsoformat(e.fetched_at),
    };
  }
  return { ...body, completeness: completeness(body) };
}
