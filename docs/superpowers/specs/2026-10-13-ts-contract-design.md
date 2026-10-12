# TypeScript migration, step 0: workspace and shared contract

Date: 2026-10-13
Status: proposed

## Context

The pipeline is moving from Python to TypeScript, one service at a time, with the
stack working after every step. The order agreed:

| # | Piece |
|---|---|
| **0** | **TypeScript workspace and shared contract (this spec)** |
| 1 | Mock directory and enrichment services, rebuilt with larger, messier, still-known-answer data |
| 2 | API, search console, MCP server, query builder |
| 3 | Activities, one group per task queue: index, enrich, scrape+extract, resolve |
| 4 | Workflows |
| 5 | Remove the Python |

A piece-by-piece migration works because services meet only through JSON: Temporal
payloads (the Pydantic models in `src/directory_pipeline/domain/models.py`,
serialized by `pydantic_data_converter`) and OpenSearch documents
(`EnrichedCompany.to_document()`). Nothing imports across services. Every later
step therefore depends on one thing: TypeScript reading and writing exactly the
JSON Python does. Step 0 builds that, and the CI check that keeps it true while
both languages coexist.

## Goal

A `@directory-pipeline/contract` package whose Zod schemas and helper functions
are proven, in CI, to agree with the Python models in both directions.

Out of scope: any running TypeScript service. Step 0 ships a library and tests.

## Decisions

- **Runtime: Node 22 LTS.** The Temporal TypeScript SDK requires Node for
  workers; Bun is not an option for steps 3-4, so it is not used here either.
- **pnpm workspace** at the repo root, alongside the Python. One repo, one
  `docker compose up` for the mixed stack, and the migration stays readable in
  git history. Versions pinned via `pnpm-lock.yaml`, installed with
  `--frozen-lockfile`, the counterpart of `uv sync --frozen`.
- **Zod schemas are hand-written**, not generated from the Pydantic models. They
  are the TypeScript that outlives the Python, so they should read as idiomatic
  TypeScript; CI, not codegen, guarantees they agree.
- **Strictness matches the Python side:** `strict`, `noUncheckedIndexedAccess`,
  `exactOptionalPropertyTypes` in a shared `tsconfig.base.json`; ESLint with
  `typescript-eslint` strict-type-checked; Vitest for tests.

## Layout

```
package.json               root: workspace scripts, engines.node, packageManager
pnpm-workspace.yaml        packages/*
pnpm-lock.yaml
tsconfig.base.json
eslint.config.js
packages/contract/
  package.json             @directory-pipeline/contract
  src/models.ts            Zod schemas + inferred types
  src/ids.ts               recordId, contentHash, sourceIdFromUrl
  src/document.ts          toDocument, completeness, addressAsText
  src/pyround.ts           Python-compatible round()
  src/index.ts
  test/
contract/fixtures/         JSON written by Python, committed
scripts/export_contract_fixtures.py
tests/test_contract_fixtures.py
```

`contract/fixtures/` sits at the repo root, not inside the package, because
both languages read it.

## What the contract covers

**Schemas**, one per model in `models.py`: `RawListing`, `Address`, `Contact`,
`ExtractionMethod`, `CompanyRecord`, `Enrichment`, `EnrichedCompany`,
`CrawlRequest`, `CrawlResult`, `ReindexRequest`, `ReindexResult`, and
`SCHEMA_VERSION` (4).

| Pydantic | Zod |
|---|---|
| `extra="forbid"` | `z.strictObject` |
| `Field(default=...)`, `default_factory` | `.default(...)`; datetime factories default to "now" |
| `name` validator (strip, non-blank) | `z.string().trim().min(1)` |
| `int` | `z.number().int()` |
| `float` | `z.number()` |
| `str \| None = None` | `z.string().nullable().default(null)` |
| `StrEnum` | `z.enum([...])` |
| `dict[str, int]` | `z.record(z.string(), z.number().int())` |
| `datetime` | ISO-8601 **string**, validated, never converted to `Date` |

**Functions** that define keys or document shape, so a drift would be silent:

| Python | TypeScript |
|---|---|
| `CompanyRecord.make_record_id` (sha1 of `source:source_id`) | `recordId` |
| `RawListing.content_hash` (sha256, first 16 hex) | `contentHash` |
| `scraping.crawler.source_id_from_url` | `sourceIdFromUrl` |
| `Address.as_text` | `addressAsText` |
| `EnrichedCompany.to_document` | `toDocument` |
| `_completeness` | `completeness` |

`source_id_from_url` lives in the crawler, not the models, but its own docstring
says a second implementation that drifted would break incremental re-crawl with
no error. It is part of the contract.

## Three traps, and how each is handled

1. **Datetime precision.** Pydantic writes microseconds
   (`2026-10-12T23:10:24.954613Z`); a JS `Date` keeps milliseconds. Converting
   would rewrite every timestamp that passes through TypeScript. Datetimes stay
   strings end to end; a later step that needs arithmetic converts locally and
   never writes the result back.

2. **Rounding.** `to_document` calls Python's `round(x, 4)`, which rounds the
   exact binary value with ties to even: `round(2.675, 2)` is `2.67` and
   `round(0.5)` is `0`, where `Math.round(x * 100) / 100` gives `2.68` and `1`.
   `pyRound` reproduces CPython's result. It is tested against a table of values
   CPython computed, including exact ties and a few thousand random floats from
   the fixture export.

3. **Number formatting.** Python serializes the float `1.0` as `1.0`;
   `JSON.stringify` writes `1`. Byte-identical JSON is therefore impossible, and
   not needed: Pydantic accepts `1` for a float field. All comparisons are on
   **parsed values** (deep equality), never on bytes.

## How agreement is enforced

**Python to TypeScript.** `scripts/export_contract_fixtures.py` builds a fixed
set of model instances (defaults only, every field filled, and edge cases:
unicode names, whitespace-padded names, empty lists, nulls everywhere,
`enrichment` absent and present, `employee_count` falling back from enrichment
to company), with fixed timestamps so the output is deterministic. For each it
writes:

- the model's JSON (`model_dump(mode="json")`)
- for `EnrichedCompany`: the expected `to_document()`
- for `CompanyRecord`/`RawListing`: the expected record id and content hash
- for a list of URLs: the expected `source_id_from_url`
- the `pyRound` table

Vitest then, for every fixture: parses it with the matching schema, serializes
the result, and deep-equals the original; runs `toDocument`, `recordId`,
`contentHash`, `sourceIdFromUrl` and `pyRound` and deep-equals Python's answers.
It also checks that invalid inputs Python rejects (blank name, unknown field,
wrong enum value) are rejected by Zod.

**TypeScript to Python.** A Vitest test writes TypeScript-built instances,
including ones that rely on Zod defaults, to `contract/fixtures/from-ts/`.
`tests/test_contract_fixtures.py` validates each with the matching Pydantic
model and fails on rejection. This catches a Zod default that Python would
refuse, which the other direction cannot.

**Staleness.** CI regenerates the Python fixtures and runs
`git diff --exit-code contract/fixtures`. Changing a Python model without
re-exporting fails CI; re-exporting without updating Zod fails the TypeScript
tests. The `from-ts` fixtures are generated in CI, not committed.

## CI

A new `typescript` job: `pnpm install --frozen-lockfile`, `eslint`,
`tsc --noEmit`, `vitest run`. The existing `test` job gains the fixture
regeneration and diff check. The TypeScript-to-Python check runs in the
`typescript` job, which uses the project's `uv` environment for the one pytest
file.

`make install` additionally runs `pnpm install --frozen-lockfile`; `make check`
runs the TypeScript checks after the Python ones.

## Done when

- Every model and function in the tables above has a TypeScript counterpart.
- All fixtures round-trip in both directions; CI is green with the new job.
- Changing a field in `models.py` without touching Zod turns CI red (verified
  once by doing it).
- The Docker image is unchanged: nothing TypeScript is built into it yet. `.dockerignore`
  gains `node_modules` so the build context stays small.
