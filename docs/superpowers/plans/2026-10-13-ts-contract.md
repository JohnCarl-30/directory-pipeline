# TypeScript Contract (Migration Step 0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `@directory-pipeline/contract` TypeScript package whose Zod schemas and helper functions are proven, in CI, to agree with the Pydantic models in both directions.

**Architecture:** Python stays the source of truth. A Python script freezes the models' behaviour into JSON fixtures under `contract/fixtures/`; Vitest checks the hand-written Zod schemas and ported helpers against them; Vitest also writes TypeScript-built instances that a pytest validates with Pydantic. A pytest fails if the committed fixtures fall behind the models.

**Tech Stack:** Node 22 LTS, pnpm 10 workspace, TypeScript 6.0, Zod 4, Vitest 5 (+ Vite 8 peer), ESLint 10 + typescript-eslint 8 (strict-type-checked). Python side: Pydantic 2, pytest, mypy --strict, ruff.

**Spec:** `docs/superpowers/specs/2026-10-13-ts-contract-design.md`

## Global Constraints

- Node: CI uses `22.23.3` (pinned in `.node-version`); `engines.node` is `>=22.12`. Vitest 5 declares `^22.12.0 || ^24.0.0 || >=26.0.0`, so a local Node 25 prints an engines warning; if tests misbehave locally, switch to Node 22.
- TypeScript is pinned to `6.0.3`, not 7.x: typescript-eslint `8.71.2` requires `typescript >=4.8.4 <6.1.0`.
- Exact pins: `zod 4.6.7`, `vitest 5.0.4`, `vite 8.3.4`, `eslint 10.12.0`, `@eslint/js 10.0.1`, `typescript-eslint 8.71.2`, `@types/node 22.20.5`, `pnpm 10.33.0`.
- Installs are always `pnpm install --frozen-lockfile` (the counterpart of `uv sync --frozen`).
- tsconfig: `strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`.
- Datetimes are ISO-8601 **strings** in TypeScript, never `Date` objects.
- All TypeScript/Python comparisons are on **parsed values**, never bytes (`1.0` vs `1`).
- `SCHEMA_VERSION` is `4`.
- Python files must pass `ruff check`, `ruff format --check` and `mypy` (strict; `files = ["src", "tests", "scripts"]`).
- Nothing TypeScript goes into the Docker image; `.dockerignore` gains `node_modules`.
- Commits end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

1. **An empty `categories` list.** In Python `bool([])` is false; in JavaScript `[]` is truthy. `completeness` must count `[]` as missing. Pinned by the `duplicate_without_enrichment` document fixture (Task 5) and a direct unit test in Task 5.
2. **Timestamps TypeScript created itself.** A Zod default writes `…24.123Z` (milliseconds). `toDocument` must render that as Python's `isoformat()` would after Pydantic read it: `…24.123000+00:00`. Pinned by a `pyIsoformat` unit test in Task 5.
3. **Nested defaults.** In Zod 4, `.default({})` returns `{}` as-is without applying the inner schema's defaults, so `address` would lack `country: "US"`. Use `.prefault({})`. Pinned by the `CompanyRecord/required_only` minimal fixture in Task 2.
4. **The Pydantic check silently skipping in CI.** If Vitest never wrote `from-ts/cases.json`, the pytest skips; CI must fail instead. Pinned by `CONTRACT_REQUIRE_FROM_TS=1` in Task 6.
5. **Rounding to negative zero.** `round(-0.00001, 4)` is `-0.0` in Python. `pyRound` must keep the sign, and the comparison must use `Object.is`, because `toBe`/`toEqual` treat `0` and `-0` differently in confusing ways. Pinned in Task 3.

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/export_contract_fixtures.py` | Builds fixed model instances and writes every fixture file. |
| `contract/fixtures/*.json` | Committed fixtures (Python's answers). |
| `contract/fixtures/from-ts/cases.json` | Written by Vitest, gitignored. |
| `tests/test_contract_fixtures.py` | Staleness check + TypeScript-to-Pydantic check. |
| `package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml`, `.node-version` | Workspace and pins. |
| `tsconfig.base.json`, `eslint.config.js` | Shared strictness. |
| `packages/contract/src/models.ts` | Zod schemas, inferred types, `MODELS` registry. |
| `packages/contract/src/pyround.ts` | `pyRound`, CPython-compatible `round(x, n)`. |
| `packages/contract/src/ids.ts` | `recordId`, `contentHash`, `sourceIdFromUrl`. |
| `packages/contract/src/document.ts` | `pyIsoformat`, `addressAsText`, `completeness`, `toDocument`. |
| `packages/contract/src/index.ts` | Re-exports. |
| `packages/contract/test/fixtures.ts` | Fixture loader and fixture types. |
| `packages/contract/test/*.test.ts` | One test file per source file, plus `from-ts.test.ts`. |

Tasks in order: 1 Python exporter → 2 workspace + models → 3 pyRound → 4 ids → 5 document → 6 TypeScript-to-Python check, CI, Makefile, README.

---

### Task 1: Python fixture exporter and staleness check

**Files:**
- Create: `scripts/export_contract_fixtures.py`
- Create: `tests/test_contract_fixtures.py`
- Create: `contract/fixtures/` (generated: `models.json`, `minimal.json`, `invalid.json`, `documents.json`, `ids.json`, `pyround.json`)

**Interfaces:**
- Consumes: `directory_pipeline.domain.models` (all models, `CompanyRecord.make_record_id`, `RawListing.content_hash`, `EnrichedCompany.to_document`), `directory_pipeline.scraping.crawler.source_id_from_url`.
- Produces (fixture formats every later task reads):
  - `models.json`: `{"cases": [{"model": str, "case": str, "data": object}]}`
  - `minimal.json`: `{"cases": [{"model": str, "case": str, "input": object, "expected": object, "now_fields": [str]}]}`. `expected` has the `now_fields` paths (dotted) removed.
  - `invalid.json`: `{"cases": [{"model": str, "case": str, "data": object}]}`
  - `documents.json`: `{"cases": [{"case": str, "input": object, "document": object}]}`
  - `ids.json`: `{"record_ids": [{"source", "source_id", "expected"}], "content_hashes": [{"html", "expected"}], "source_ids": [{"url", "expected"}]}`
  - `pyround.json`: `{"cases": [[x, ndigits, expected], ...]}`
  - CLI: `python scripts/export_contract_fixtures.py [--out DIR]`, default `contract/fixtures`.

- [ ] **Step 1: Write the failing staleness test**

Create `tests/test_contract_fixtures.py`:

```python
"""The TypeScript contract (packages/contract) agrees with the Pydantic models.

Most of the checking is Vitest's, against fixtures this repo commits. This file
holds the two halves that need Python:

- the committed fixtures match a fresh export, so the TypeScript side is never
  tested against a stale picture of the models;
- JSON the TypeScript side wrote validates against the Pydantic models.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contract" / "fixtures"
EXPORTER = ROOT / "scripts" / "export_contract_fixtures.py"


def test_committed_fixtures_match_the_models(tmp_path: Path) -> None:
    subprocess.run([sys.executable, str(EXPORTER), "--out", str(tmp_path)], check=True)

    fresh = sorted(p.name for p in tmp_path.iterdir())
    assert fresh, "the exporter wrote nothing"
    for name in fresh:
        committed = FIXTURES / name
        hint = "run: python scripts/export_contract_fixtures.py"
        assert committed.exists(), f"contract/fixtures/{name} is missing; {hint}"
        assert committed.read_text(encoding="utf-8") == (tmp_path / name).read_text(
            encoding="utf-8"
        ), f"contract/fixtures/{name} is stale; {hint}"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_contract_fixtures.py`
Expected: FAIL, `CalledProcessError` (the exporter does not exist).

- [ ] **Step 3: Write the exporter**

Create `scripts/export_contract_fixtures.py`:

```python
"""Write the JSON fixtures the TypeScript contract package is tested against.

The Pydantic models in domain/models.py are the source of truth while Python and
TypeScript coexist. This script freezes their behaviour into contract/fixtures/:
packages/contract checks its Zod schemas and helpers against these files, and
tests/test_contract_fixtures.py fails when the committed copy falls behind the
models. Re-run it after any change to domain/models.py or source_id_from_url.

    python scripts/export_contract_fixtures.py            # rewrite the fixtures
    python scripts/export_contract_fixtures.py --out DIR  # write elsewhere

The output is deterministic: fixed timestamps, a seeded RNG, no dict-order luck.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from directory_pipeline.domain import models as m  # noqa: E402
from directory_pipeline.scraping.crawler import source_id_from_url  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "contract" / "fixtures"

# The three shapes Pydantic writes a datetime in: with microseconds, as a whole
# second (no fraction at all), and with a non-UTC offset.
T_MICROS = datetime(2026, 10, 12, 23, 10, 24, 954613, tzinfo=UTC)
T_WHOLE = datetime(2026, 1, 1, tzinfo=UTC)
T_OFFSET = datetime(2026, 3, 9, 8, 0, 0, 5, tzinfo=timezone(timedelta(hours=8)))

MODELS: dict[str, type[BaseModel]] = {
    cls.__name__: cls
    for cls in (
        m.RawListing,
        m.Address,
        m.Contact,
        m.CompanyRecord,
        m.Enrichment,
        m.EnrichedCompany,
        m.CrawlRequest,
        m.CrawlResult,
        m.ReindexRequest,
        m.ReindexResult,
    )
}

REQUIRED_RECORD: dict[str, Any] = {
    "record_id": m.CompanyRecord.make_record_id("demo-directory", "northwind-analytics"),
    "source": "demo-directory",
    "source_id": "northwind-analytics",
    "source_url": "http://localhost:8081/company/northwind-analytics",
    "name": "Northwind Analytics, Inc.",
}


# --- instances ----------------------------------------------------------------


def _address_full() -> m.Address:
    return m.Address(
        line1="412 Congress Ave",
        line2="Suite 800",
        city="Austin",
        region="TX",
        postal_code="78701",
    )


def _contact_full() -> m.Contact:
    return m.Contact(
        phone_e164="+15125550100",
        phone_raw="(512) 555-0100",
        email="hello@northwind.example",
        website="https://northwind.example",
    )


def _record_full() -> m.CompanyRecord:
    return m.CompanyRecord(
        **REQUIRED_RECORD,
        legal_name="Northwind Analytics Incorporated",
        name_normalized="northwind analytics",
        categories=["software", "analytics"],
        description="Real-time customer data platforms for mid-market retailers.",
        address=_address_full(),
        contact=_contact_full(),
        employee_count=138,
        founded_year=2014,
        extraction_method=m.ExtractionMethod.TEXT,
        extraction_confidence=0.9,
        content_hash="5d41402abc4b2a76",
        first_seen_at=T_MICROS,
        last_seen_at=T_OFFSET,
    )


def _record_sparse() -> m.CompanyRecord:
    """Every optional field empty, a name that needs stripping, non-ASCII text."""
    return m.CompanyRecord(
        record_id=m.CompanyRecord.make_record_id("demo-directory", "cafe-zurich"),
        source="demo-directory",
        source_id="cafe-zurich",
        source_url="http://localhost:8081/company/cafe-zurich",
        name="  Café Zürich — 東京 ",
        extraction_method=m.ExtractionMethod.LLM_REPAIRED,
        extraction_confidence=0.123456789,
        first_seen_at=T_WHOLE,
        last_seen_at=T_WHOLE,
    )


def _enrichment_full() -> m.Enrichment:
    return m.Enrichment(
        provider="mock-enrichment",
        provider_company_id="nw-001",
        industry="Software",
        naics="511210",
        revenue_usd=24_000_000,
        employee_count=140,
        linkedin_url="https://linkedin.example/company/northwind",
        technologies=["python", "opensearch"],
        confidence=0.87655,
        fetched_at=T_MICROS,
        cache_hit=True,
    )


def enriched_cases() -> list[tuple[str, m.EnrichedCompany]]:
    full = m.EnrichedCompany(
        company=_record_full(), enrichment=_enrichment_full(), cluster_id="c-northwind"
    )
    return [
        ("full", full),
        (
            "duplicate_without_enrichment",
            m.EnrichedCompany(
                company=_record_sparse(),
                cluster_id="c-northwind",
                duplicate_of=full.company.record_id,
            ),
        ),
        (
            # 0 is falsy, so to_document falls back to the company's own count.
            "enrichment_employee_count_zero",
            m.EnrichedCompany(
                company=_record_full(),
                enrichment=_enrichment_full().model_copy(update={"employee_count": 0}),
            ),
        ),
        (
            "no_employee_count_anywhere",
            m.EnrichedCompany(
                company=_record_sparse(),
                enrichment=m.Enrichment(provider="mock-enrichment", fetched_at=T_WHOLE),
            ),
        ),
    ]


def model_cases() -> list[tuple[str, BaseModel]]:
    cases: list[tuple[str, BaseModel]] = [
        (
            "full",
            m.RawListing(
                source="demo-directory",
                source_id="northwind-analytics",
                url="http://localhost:8081/company/northwind-analytics",
                html="<html><body>Café 東京</body></html>",
                fetched_at=T_MICROS,
                http_status=304,
                via_proxy="http://proxy-2:3128",
            ),
        ),
        ("full", _address_full()),
        ("empty", m.Address()),
        ("full", _contact_full()),
        ("full", _record_full()),
        ("sparse", _record_sparse()),
        ("full", _enrichment_full()),
        *enriched_cases(),
        (
            "continuation",
            m.CrawlRequest(
                categories=["software", "logistics"],
                max_pages=3,
                index_alias="companies",
                force_refetch=True,
                pending_urls=["http://localhost:8081/company/a"],
                carried_totals={"pages_crawled": 7, "documents_indexed": 12},
            ),
        ),
        (
            "finished",
            m.CrawlResult(
                run_id="66d1172b",
                pages_crawled=7,
                listings_found=12,
                records_extracted=12,
                records_enriched=10,
                duplicates_collapsed=2,
                documents_indexed=12,
                failures=["batch-3: timeout"],
                index_name="companies-v4-20261012",
                started_at=T_MICROS,
                finished_at=T_OFFSET,
            ),
        ),
        (
            "full",
            m.ReindexRequest(
                alias="companies",
                reason="schema 4",
                wait_for_completion=True,
                drop_old_index=True,
            ),
        ),
        (
            "first_build",
            m.ReindexResult(
                alias="companies",
                source_index=None,
                target_index="companies-v4-20261012",
                documents_copied=12,
                swapped=True,
                duration_s=1.25,
            ),
        ),
    ]
    return cases


# --- minimal inputs: what the defaults fill in ----------------------------------

# (model, case, input, fields whose default is "now"). Those fields differ on
# every run, so they are removed from `expected` and checked for shape only.
MINIMAL: list[tuple[str, str, dict[str, Any], list[str]]] = [
    (
        "RawListing",
        "required_only",
        {"source": "s", "source_id": "i", "url": "http://x/company/i", "html": "<p>hi</p>"},
        ["fetched_at"],
    ),
    ("Address", "required_only", {}, []),
    ("Contact", "required_only", {}, []),
    ("CompanyRecord", "required_only", REQUIRED_RECORD, ["first_seen_at", "last_seen_at"]),
    (
        "CompanyRecord",
        "padded_name",
        {**REQUIRED_RECORD, "name": "  Northwind  "},
        ["first_seen_at", "last_seen_at"],
    ),
    ("Enrichment", "required_only", {"provider": "mock-enrichment"}, ["fetched_at"]),
    (
        "EnrichedCompany",
        "required_only",
        {"company": REQUIRED_RECORD},
        ["company.first_seen_at", "company.last_seen_at"],
    ),
    ("CrawlRequest", "required_only", {}, []),
    ("CrawlResult", "required_only", {"run_id": "r"}, ["started_at"]),
    ("ReindexRequest", "required_only", {"alias": "companies"}, []),
    (
        "ReindexResult",
        "required_only",
        {
            "alias": "companies",
            "source_index": None,
            "target_index": "t",
            "documents_copied": 0,
            "swapped": False,
        },
        [],
    ),
]


def _drop(data: dict[str, Any], dotted: str) -> None:
    head, _, rest = dotted.partition(".")
    if rest:
        _drop(data[head], rest)
    else:
        del data[head]


def minimal_cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for model, case, data, now_fields in MINIMAL:
        expected = MODELS[model].model_validate(data).model_dump(mode="json")
        for field in now_fields:
            _drop(expected, field)
        out.append(
            {
                "model": model,
                "case": case,
                "input": data,
                "expected": expected,
                "now_fields": now_fields,
            }
        )
    return out


# --- inputs Pydantic rejects ---------------------------------------------------

# Only strict-mode rejections belong here. Pydantic's lax mode accepts some
# inputs Zod refuses ("5" for an int); those are deliberately not listed, since
# TypeScript never reads JSON that Python did not write.
INVALID: list[tuple[str, str, dict[str, Any]]] = [
    ("CompanyRecord", "blank_name", {**REQUIRED_RECORD, "name": "   "}),
    ("CompanyRecord", "missing_name", {k: v for k, v in REQUIRED_RECORD.items() if k != "name"}),
    ("CompanyRecord", "unknown_field", {**REQUIRED_RECORD, "nickname": "NW"}),
    ("CompanyRecord", "unknown_extraction_method", {**REQUIRED_RECORD, "extraction_method": "regex"}),
    ("Address", "unknown_field", {"zip": "78701"}),
    ("CrawlRequest", "fractional_max_pages", {"max_pages": 2.5}),
    (
        "ReindexResult",
        "missing_nullable_source_index",
        {"alias": "a", "target_index": "t", "documents_copied": 0, "swapped": False},
    ),
    ("EnrichedCompany", "null_company", {"company": None}),
    (
        "RawListing",
        "malformed_fetched_at",
        {"source": "s", "source_id": "i", "url": "u", "html": "", "fetched_at": "yesterday"},
    ),
]


def invalid_cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for model, case, data in INVALID:
        try:
            MODELS[model].model_validate(data)
        except ValidationError:
            out.append({"model": model, "case": case, "data": data})
            continue
        raise SystemExit(f"{model}/{case}: Pydantic accepted it, so it is not an invalid case")
    return out


# --- ids, hashes, rounding -----------------------------------------------------

RECORD_ID_INPUTS = [
    ("demo-directory", "northwind-analytics"),
    ("demo-directory", "café-東京"),
    ("a:b", "c"),
    ("", ""),
]
HTML_INPUTS = ["", "<p>hi</p>", "<html><body>Café 東京 🚀</body></html>", "line\r\nbreak"]
URL_INPUTS = [
    "http://localhost:8081/company/northwind-analytics",
    "http://localhost:8081/company/northwind-analytics/",
    "http://localhost:8081/company/northwind-analytics?page=2",
    "http://localhost:8081/company/northwind-analytics///",
    "http://localhost:8081/company/a?next=/b/c",
    "http://localhost:8081/company/a#reviews",
    "northwind",
    "/",
    "",
]


def ids_fixture() -> dict[str, Any]:
    return {
        "record_ids": [
            {"source": s, "source_id": i, "expected": m.CompanyRecord.make_record_id(s, i)}
            for s, i in RECORD_ID_INPUTS
        ],
        "content_hashes": [
            {
                "html": html,
                "expected": m.RawListing(
                    source="s", source_id="i", url="u", html=html, fetched_at=T_WHOLE
                ).content_hash,
            }
            for html in HTML_INPUTS
        ],
        "source_ids": [{"url": url, "expected": source_id_from_url(url)} for url in URL_INPUTS],
    }


# Values where a naive port goes wrong: binary values just under a decimal tie,
# exact ties (ties go to even), and a result of negative zero.
HAND_ROUNDING: list[tuple[float, int]] = [
    (2.675, 2),
    (0.5, 0),
    (1.5, 0),
    (2.5, 0),
    (-2.5, 0),
    (0.125, 2),
    (0.375, 2),
    (0.00005, 4),
    (-0.00001, 4),
    (0.12345, 4),
    (0.87655, 4),
    (0.123456789, 4),
    (123456.78915, 4),
    (1e-10, 4),
]


def rounding_rows() -> list[list[float]]:
    rng = random.Random(20261013)
    pairs = list(HAND_ROUNDING)
    for _ in range(2000):
        pairs.append((rng.uniform(-1, 1) * 10 ** rng.randint(-3, 6), rng.randint(0, 6)))
    for _ in range(500):
        # Exact binary fractions: these are the values that can be true ties.
        pairs.append((rng.randint(-(10**6), 10**6) / 2 ** rng.randint(1, 10), rng.randint(0, 3)))
    return [[x, n, round(x, n)] for x, n in pairs]


# --- writing -------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_rows(path: Path, rows: list[list[float]]) -> None:
    """One row per line: 2,500 rows at indent=2 would be 12,000 lines of diff."""
    body = ",\n".join(f"    {json.dumps(row)}" for row in rows)
    path.write_text('{\n  "cases": [\n' + body + "\n  ]\n}\n", encoding="utf-8")


def export(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    _write_json(
        out / "models.json",
        {
            "cases": [
                {"model": type(inst).__name__, "case": case, "data": inst.model_dump(mode="json")}
                for case, inst in model_cases()
            ]
        },
    )
    _write_json(out / "minimal.json", {"cases": minimal_cases()})
    _write_json(out / "invalid.json", {"cases": invalid_cases()})
    _write_json(
        out / "documents.json",
        {
            "cases": [
                {"case": case, "input": item.model_dump(mode="json"), "document": item.to_document()}
                for case, item in enriched_cases()
            ]
        },
    )
    _write_json(out / "ids.json", ids_fixture())
    _write_rows(out / "pyround.json", rounding_rows())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    export(parser.parse_args().out)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Generate the fixtures**

Run: `.venv/bin/python scripts/export_contract_fixtures.py && ls contract/fixtures`
Expected: `documents.json  ids.json  invalid.json  minimal.json  models.json  pyround.json`

Spot-check two facts the TypeScript side depends on:

Run: `grep -c '' contract/fixtures/pyround.json && grep -m3 '"2026-' contract/fixtures/models.json`
Expected: about 2,519 lines; timestamps like `"2026-10-12T23:10:24.954613Z"` and `"2026-03-09T08:00:00.000005+08:00"`.

- [ ] **Step 5: Run the test to verify it passes, then the Python gates**

Run: `.venv/bin/python -m pytest -q tests/test_contract_fixtures.py && make lint types`
Expected: `1 passed`; ruff and mypy clean. If `ruff format --check` fails, run `make fmt` and re-run.

- [ ] **Step 6: Commit**

```bash
git add scripts/export_contract_fixtures.py tests/test_contract_fixtures.py contract/fixtures
git commit -m "feat(contract): export Python model fixtures for the TypeScript contract

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: pnpm workspace and the Zod models

**Files:**
- Create: `package.json`, `pnpm-workspace.yaml`, `.node-version`, `tsconfig.base.json`, `eslint.config.js`
- Create: `packages/contract/package.json`, `packages/contract/tsconfig.json`
- Create: `packages/contract/src/models.ts`, `packages/contract/src/index.ts`
- Create: `packages/contract/test/fixtures.ts`, `packages/contract/test/models.test.ts`
- Modify: `.gitignore` (append), `.dockerignore` (append)
- Generated: `pnpm-lock.yaml`

**Interfaces:**
- Consumes: `contract/fixtures/models.json`, `minimal.json`, `invalid.json` (Task 1 formats).
- Produces:
  - Schemas (each also exported as a type of the same name via `z.output`): `ExtractionMethod`, `IsoDateTime`, `RawListing`, `Address`, `Contact`, `CompanyRecord`, `Enrichment`, `EnrichedCompany`, `CrawlRequest`, `CrawlResult`, `ReindexRequest`, `ReindexResult`.
  - `SCHEMA_VERSION: 4`
  - `MODELS` (record of name → schema) and `type ModelName = keyof typeof MODELS`.
  - Test helpers in `test/fixtures.ts`: `FIXTURES: URL`, `loadFixture<T>(name: string): T`, and the types `ModelCase`, `MinimalCase`, `InvalidCase`, `DocumentCase`, `IdFixtures`, `RoundingFixtures`.

- [ ] **Step 1: Create the workspace files**

`package.json`:

```json
{
  "name": "directory-pipeline-workspace",
  "private": true,
  "type": "module",
  "packageManager": "pnpm@10.33.0",
  "engines": {
    "node": ">=22.12"
  },
  "scripts": {
    "lint": "eslint .",
    "typecheck": "pnpm -r typecheck",
    "test": "pnpm -r test",
    "check": "pnpm lint && pnpm typecheck && pnpm test"
  },
  "devDependencies": {
    "@eslint/js": "10.0.1",
    "@types/node": "22.20.5",
    "eslint": "10.12.0",
    "typescript": "6.0.3",
    "typescript-eslint": "8.71.2",
    "vite": "8.3.4",
    "vitest": "5.0.4"
  }
}
```

`pnpm-workspace.yaml`:

```yaml
packages:
  - packages/*
```

`.node-version`:

```
22.23.3
```

`tsconfig.base.json`:

```json
{
  "compilerOptions": {
    "target": "ES2023",
    "lib": ["ES2023"],
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "types": ["node"],
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitOverride": true,
    "noFallthroughCasesInSwitch": true,
    "verbatimModuleSyntax": true,
    "isolatedModules": true,
    "skipLibCheck": true,
    "noEmit": true
  }
}
```

`eslint.config.js`:

```js
// Type-aware linting at typescript-eslint's strictest preset: the TypeScript
// counterpart of `mypy --strict` on the Python side.
import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";

export default defineConfig(
  { ignores: ["**/node_modules/**", ".venv/**", ".mypy_cache/**", "htmlcov/**"] },
  js.configs.recommended,
  tseslint.configs.strictTypeChecked,
  {
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
  },
  { files: ["**/*.js"], extends: [tseslint.configs.disableTypeChecked] },
);
```

`packages/contract/package.json`:

```json
{
  "name": "@directory-pipeline/contract",
  "version": "0.0.0",
  "private": true,
  "type": "module",
  "exports": {
    ".": "./src/index.ts"
  },
  "scripts": {
    "typecheck": "tsc --noEmit",
    "test": "vitest run"
  },
  "dependencies": {
    "zod": "4.6.7"
  }
}
```

`packages/contract/tsconfig.json`:

```json
{
  "extends": "../../tsconfig.base.json",
  "include": ["src", "test"]
}
```

Append to `.gitignore`:

```
# TypeScript
node_modules/
contract/fixtures/from-ts/
```

Append to `.dockerignore`:

```
node_modules
```

- [ ] **Step 2: Install**

Run: `pnpm install`
Expected: creates `pnpm-lock.yaml` and `node_modules/`. An engines warning on Node 25 is expected (see Global Constraints).

- [ ] **Step 3: Write the fixture helpers and the failing model tests**

`packages/contract/test/fixtures.ts`:

```ts
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
```

`packages/contract/test/models.test.ts`:

```ts
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
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `pnpm --filter @directory-pipeline/contract test`
Expected: FAIL, cannot resolve `../src/models.js`.

- [ ] **Step 5: Write the models**

`packages/contract/src/models.ts`:

```ts
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
```

`packages/contract/src/index.ts`:

```ts
export * from "./models.js";
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pnpm --filter @directory-pipeline/contract test`
Expected: PASS, every `models.json`, `minimal.json` and `invalid.json` case listed by name.

If `CompanyRecord/required_only` fails on a missing `address.country`, the nested default is using `.default` instead of `.prefault` (Review Focus 3).

- [ ] **Step 7: Lint and type-check**

Run: `pnpm check`
Expected: ESLint clean, `tsc` clean, tests pass.

- [ ] **Step 8: Commit**

```bash
git add package.json pnpm-workspace.yaml pnpm-lock.yaml .node-version tsconfig.base.json \
  eslint.config.js packages/contract .gitignore .dockerignore
git commit -m "feat(contract): pnpm workspace and Zod mirror of the domain models

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `pyRound`, Python's `round()` in TypeScript

**Files:**
- Create: `packages/contract/src/pyround.ts`
- Create: `packages/contract/test/pyround.test.ts`
- Modify: `packages/contract/src/index.ts`

**Interfaces:**
- Consumes: `contract/fixtures/pyround.json`; `loadFixture`, `RoundingFixtures` from `test/fixtures.ts`.
- Produces: `pyRound(x: number, ndigits: number): number`. Matches CPython's `round(x, ndigits)` for `ndigits >= 0`. NaN and ±Infinity pass through. Throws `RangeError` for negative or non-integer `ndigits`.

- [ ] **Step 1: Write the failing tests**

`packages/contract/test/pyround.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { pyRound } from "../src/pyround.js";
import { loadFixture, type RoundingFixtures } from "./fixtures.js";

describe("pyRound", () => {
  it("matches CPython on every fixture value", () => {
    const { cases } = loadFixture<RoundingFixtures>("pyround.json");
    expect(cases.length).toBeGreaterThan(2000);
    // Object.is, so 0 and -0 count as different, as they are in Python.
    const mismatches = cases.filter(([x, n, expected]) => !Object.is(pyRound(x, n), expected));
    expect(mismatches).toEqual([]);
  });

  it("rounds the binary value, not the decimal literal", () => {
    // 2.675 is stored as 2.67499999...; Math.round(x * 100) / 100 gives 2.68.
    expect(pyRound(2.675, 2)).toBe(2.67);
  });

  it("sends exact ties to even", () => {
    expect(pyRound(0.5, 0)).toBe(0);
    expect(pyRound(1.5, 0)).toBe(2);
    expect(pyRound(2.5, 0)).toBe(2);
    expect(pyRound(0.125, 2)).toBe(0.12);
  });

  it("keeps the sign of a result that rounds to zero", () => {
    expect(Object.is(pyRound(-0.00001, 4), -0)).toBe(true);
    expect(Object.is(pyRound(-0, 4), -0)).toBe(true);
  });

  it("passes non-finite values through", () => {
    expect(pyRound(Number.NaN, 2)).toBeNaN();
    expect(pyRound(Number.POSITIVE_INFINITY, 2)).toBe(Number.POSITIVE_INFINITY);
  });

  it("rejects ndigits Python's float rounding would treat differently", () => {
    expect(() => pyRound(1.5, -1)).toThrow(RangeError);
    expect(() => pyRound(1.5, 0.5)).toThrow(RangeError);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm --filter @directory-pipeline/contract test pyround`
Expected: FAIL, cannot resolve `../src/pyround.js`.

- [ ] **Step 3: Implement**

`packages/contract/src/pyround.ts`:

```ts
/**
 * Python's `round(x, ndigits)` for floats.
 *
 * CPython rounds the exact binary value of `x` to `ndigits` decimals, sending
 * exact ties to even, and returns the double nearest that decimal. The usual JS
 * idiom `Math.round(x * 10 ** n) / 10 ** n` differs twice over: the
 * multiplication rounds before the rounding does (2.675 -> 2.68, Python 2.67),
 * and Math.round sends ties up (0.5 -> 1, Python 0).
 *
 * This works on the exact value: a double is mantissa * 2^exponent, so
 * x * 10^n is an exact rational that BigInt can divide without error.
 */
export function pyRound(x: number, ndigits: number): number {
  if (!Number.isInteger(ndigits) || ndigits < 0) {
    throw new RangeError(`ndigits must be a non-negative integer, got ${String(ndigits)}`);
  }
  if (!Number.isFinite(x) || x === 0) return x;

  const { mantissa, exponent } = decompose(Math.abs(x));
  const scale = 10n ** BigInt(ndigits);

  let quotient: bigint;
  if (exponent >= 0) {
    quotient = (mantissa << BigInt(exponent)) * scale;
  } else {
    const numerator = mantissa * scale;
    const denominator = 1n << BigInt(-exponent);
    quotient = numerator / denominator;
    const twiceRemainder = (numerator % denominator) * 2n;
    if (twiceRemainder > denominator || (twiceRemainder === denominator && quotient % 2n === 1n)) {
      quotient += 1n;
    }
  }

  // Parsing the decimal string gives the nearest double, as CPython's strtod does.
  const magnitude = Number(`${quotient.toString()}e-${String(ndigits)}`);
  return x < 0 ? -magnitude : magnitude;
}

/** |x| as mantissa * 2^exponent, exactly. */
function decompose(x: number): { mantissa: bigint; exponent: number } {
  const view = new DataView(new ArrayBuffer(8));
  view.setFloat64(0, x);
  const bits = view.getBigUint64(0);
  const biasedExponent = Number((bits >> 52n) & 0x7ffn);
  const fraction = bits & 0xfffffffffffffn;
  if (biasedExponent === 0) {
    return { mantissa: fraction, exponent: -1074 }; // subnormal
  }
  return { mantissa: fraction | (1n << 52n), exponent: biasedExponent - 1075 };
}
```

Append to `packages/contract/src/index.ts`:

```ts
export * from "./pyround.js";
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pnpm --filter @directory-pipeline/contract test pyround`
Expected: PASS, 6 tests.

- [ ] **Step 5: Lint, type-check, commit**

Run: `pnpm check`
Expected: clean.

```bash
git add packages/contract/src/pyround.ts packages/contract/src/index.ts packages/contract/test/pyround.test.ts
git commit -m "feat(contract): pyRound, Python's round() on exact binary values

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Ids and hashes

**Files:**
- Create: `packages/contract/src/ids.ts`
- Create: `packages/contract/test/ids.test.ts`
- Modify: `packages/contract/src/index.ts`

**Interfaces:**
- Consumes: `contract/fixtures/ids.json`; `loadFixture`, `IdFixtures`.
- Produces:
  - `recordId(source: string, sourceId: string): string`: sha1 hex of `` `${source}:${sourceId}` `` (UTF-8).
  - `contentHash(html: string): string`: first 16 hex characters of sha256 of `html` (UTF-8).
  - `sourceIdFromUrl(url: string): string`: last non-empty path segment, query string removed. The fragment is **not** removed, because Python keeps it.

- [ ] **Step 1: Write the failing tests**

`packages/contract/test/ids.test.ts`:

```ts
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm --filter @directory-pipeline/contract test ids`
Expected: FAIL, cannot resolve `../src/ids.js`.

- [ ] **Step 3: Implement**

`packages/contract/src/ids.ts`:

```ts
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
```

Append to `packages/contract/src/index.ts`:

```ts
export * from "./ids.js";
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pnpm --filter @directory-pipeline/contract test ids`
Expected: PASS, 17 cases (4 + 4 + 9).

- [ ] **Step 5: Lint, type-check, commit**

Run: `pnpm check`
Expected: clean.

```bash
git add packages/contract/src/ids.ts packages/contract/src/index.ts packages/contract/test/ids.test.ts
git commit -m "feat(contract): recordId, contentHash and sourceIdFromUrl

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: The OpenSearch document

**Files:**
- Create: `packages/contract/src/document.ts`
- Create: `packages/contract/test/document.test.ts`
- Modify: `packages/contract/src/index.ts`

**Interfaces:**
- Consumes: `EnrichedCompany`, `Address`, `ExtractionMethod` (types) from `models.ts`; `pyRound` from `pyround.ts`; `contract/fixtures/documents.json`; `loadFixture`, `DocumentCase`.
- Produces:
  - `pyIsoformat(value: IsoDateTime): string`: Python's `datetime.isoformat()` of the value Pydantic would parse. `Z` becomes `+00:00`; a non-zero fraction is padded to 6 digits; a zero fraction is dropped. Throws `RangeError` on a non-ISO string.
  - `addressAsText(address: Address): string`
  - `interface CompanyDocument` (see code)
  - `completeness(doc: Omit<CompanyDocument, "completeness">): number`
  - `toDocument(item: EnrichedCompany): CompanyDocument`

- [ ] **Step 1: Write the failing tests**

`packages/contract/test/document.test.ts`:

```ts
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm --filter @directory-pipeline/contract test document`
Expected: FAIL, cannot resolve `../src/document.js`.

- [ ] **Step 3: Implement**

`packages/contract/src/document.ts`:

```ts
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
```

Append to `packages/contract/src/index.ts`:

```ts
export * from "./document.js";
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pnpm --filter @directory-pipeline/contract test document`
Expected: PASS, 4 document cases, 7 `pyIsoformat` cases, 1 completeness case.

- [ ] **Step 5: Lint, type-check, commit**

Run: `pnpm check`
Expected: clean.

```bash
git add packages/contract/src/document.ts packages/contract/src/index.ts packages/contract/test/document.test.ts
git commit -m "feat(contract): toDocument, the OpenSearch document shape

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: TypeScript to Pydantic, CI, Makefile, README

**Files:**
- Create: `packages/contract/test/from-ts.test.ts`
- Modify: `tests/test_contract_fixtures.py` (append)
- Modify: `.github/workflows/ci.yml` (new `typescript` job after `test`)
- Modify: `Makefile` (`install`, `check`, new `ts-check`, `.PHONY`)
- Modify: `README.md` ("Running it" section)

**Interfaces:**
- Consumes: `MODELS`, `ModelName`; `models.json`, `minimal.json`; `FIXTURES`, `loadFixture`, `ModelCase`, `MinimalCase`.
- Produces: `contract/fixtures/from-ts/cases.json` in the format `{"cases": [{"model": ModelName, "case": string, "data": object}]}`. The env var `CONTRACT_REQUIRE_FROM_TS=1` makes a missing file a failure instead of a skip.

- [ ] **Step 1: Write the failing Python test**

Append to `tests/test_contract_fixtures.py`, merging these imports with the existing ones at the top of the file:

```python
import json
import os
import re
from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from directory_pipeline.domain import models as m

FROM_TS = FIXTURES / "from-ts" / "cases.json"
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})?$")


def _normalize(value: Any) -> Any:
    """Compare datetimes as instants: TypeScript writes .123Z, Pydantic .123000Z."""
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, str) and _ISO.match(value):
        return datetime.fromisoformat(value)
    return value


def test_typescript_output_validates_in_pydantic() -> None:
    if not FROM_TS.exists():
        if os.environ.get("CONTRACT_REQUIRE_FROM_TS") == "1":
            pytest.fail(f"{FROM_TS} is missing: run `pnpm test` first")
        pytest.skip("no TypeScript output yet: run `pnpm test`")

    cases = json.loads(FROM_TS.read_text(encoding="utf-8"))["cases"]
    assert cases, "the TypeScript side wrote no cases"

    failures: list[str] = []
    for case in cases:
        label = f"{case['model']}/{case['case']}"
        try:
            parsed = getattr(m, case["model"]).model_validate(case["data"])
        except ValidationError as exc:
            failures.append(f"{label}: Pydantic rejected it\n{exc}")
            continue
        if _normalize(parsed.model_dump(mode="json")) != _normalize(case["data"]):
            failures.append(f"{label}: Pydantic read back a different value")
    assert not failures, "\n\n".join(failures)
```

Run: `CONTRACT_REQUIRE_FROM_TS=1 .venv/bin/python -m pytest -q tests/test_contract_fixtures.py`
Expected: FAIL, `from-ts/cases.json is missing`.

- [ ] **Step 2: Write the Vitest side**

`packages/contract/test/from-ts.test.ts`:

```ts
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
```

- [ ] **Step 3: Run both halves to verify they pass**

Run: `pnpm test && CONTRACT_REQUIRE_FROM_TS=1 .venv/bin/python -m pytest -q tests/test_contract_fixtures.py`
Expected: Vitest PASS; pytest `2 passed`.

- [ ] **Step 4: Add the CI job**

In `.github/workflows/ci.yml`, insert this job between the `test` job and the `audit` job:

```yaml
  typescript:
    # packages/contract against the Pydantic models, in both directions. The
    # Python half needs the uv environment, so this job sets up both toolchains.
    # (The staleness check, committed fixtures vs. a fresh export, runs in the
    # `test` job as part of the normal pytest run.)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: pnpm/action-setup@v4

      - uses: actions/setup-node@v4
        with:
          node-version-file: .node-version
          cache: pnpm

      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Install
        run: |
          pnpm install --frozen-lockfile
          uv sync --frozen --all-extras

      - name: Lint
        run: pnpm lint

      - name: Type check
        run: pnpm typecheck

      - name: Test
        run: pnpm test

      - name: TypeScript output validates in Pydantic
        # Fail, don't skip, if Vitest did not write its output: a skipped check
        # here would look exactly like a passing one.
        env:
          CONTRACT_REQUIRE_FROM_TS: "1"
        run: .venv/bin/python -m pytest -q tests/test_contract_fixtures.py
```

- [ ] **Step 5: Wire the Makefile**

In `Makefile`, replace:

```make
install: ## Create the venv from uv.lock (same resolution as CI)
	uv sync --frozen --all-extras
```

with:

```make
install: ## Create the venv from uv.lock and node_modules from pnpm-lock.yaml
	uv sync --frozen --all-extras
	pnpm install --frozen-lockfile
```

Replace:

```make
check: lint types test ## Everything CI's first job runs
```

with:

```make
check: lint types test ts-check ## Everything CI's test and typescript jobs run

ts-check: ## TypeScript lint, types and tests, then Pydantic over the TypeScript output
	pnpm check
	CONTRACT_REQUIRE_FROM_TS=1 $(PY) -m pytest -q tests/test_contract_fixtures.py
```

In the `.PHONY` line, add `ts-check` after `check`.

- [ ] **Step 6: Document it in the README**

In `README.md`, directly after the closing fence of the "### No dependencies" code block (the block ending `python scripts/bench_resolution.py   # entity resolution over 100k records (~55s)`), insert:

````markdown
### TypeScript (migration in progress)

The pipeline is moving to TypeScript one service at a time. Step 0 is
`packages/contract`: Zod schemas and helpers that mirror `domain/models.py`,
checked in CI against JSON the Python models write. `make install` needs
Node 22 and pnpm as well as uv.

```bash
pnpm check                                   # lint, tsc, vitest
python scripts/export_contract_fixtures.py   # after changing domain/models.py
```
````

- [ ] **Step 7: Run everything**

Run: `make check`
Expected: ruff, mypy, pytest (the staleness test passes; the TypeScript-output test may pass or skip depending on whether `from-ts/` exists yet), then `pnpm check`, then the required pytest run passes.

- [ ] **Step 8: Prove CI goes red on drift (the spec's "Done when")**

Make a deliberate drift, check both checks catch it, then revert:

```bash
# 1. Add a field to Contact in Python only.
sed -i '' 's/^    website: str | None = None$/    website: str | None = None\n    fax: str | None = None/' src/directory_pipeline/domain/models.py
grep -n "fax" src/directory_pipeline/domain/models.py
```
Expected: one match, inside `class Contact`.

```bash
# 2. Without re-exporting, the staleness check fails.
.venv/bin/python -m pytest -q tests/test_contract_fixtures.py::test_committed_fixtures_match_the_models
```
Expected: FAIL, `contract/fixtures/models.json is stale`.

```bash
# 3. After re-exporting, Zod rejects the new field.
.venv/bin/python scripts/export_contract_fixtures.py && pnpm test
```
Expected: FAIL in `models.test.ts`, `Contact/full` and others, with an unrecognized key `fax`.

```bash
# 4. Revert both.
git checkout -- src/directory_pipeline/domain/models.py contract/fixtures
make check
```
Expected: `git status --short` shows only this task's intended changes; `make check` passes.

- [ ] **Step 9: Commit**

```bash
git add packages/contract/test/from-ts.test.ts tests/test_contract_fixtures.py \
  .github/workflows/ci.yml Makefile README.md
git commit -m "feat(contract): check TypeScript output with Pydantic; CI job and make targets

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## After the last task

Push the branch and open a PR. Per the repo's rules, ask before pushing; then merge once CI is green (the new `typescript` job must be among the passing checks). PR #9 also edits `.github/workflows/ci.yml` (a Docker Hub login step in `integration`); if it merges first, rebase and keep both changes.
