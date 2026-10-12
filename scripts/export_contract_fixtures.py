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
    # Python's str.strip() and JavaScript's trim() disagree on six characters:
    # only Python strips \x1c-\x1f and \x85, only JavaScript strips U+FEFF.
    (
        "CompanyRecord",
        "name_python_only_whitespace",
        {**REQUIRED_RECORD, "name": "\x85\x1cNorthwind\x1d\x1e\x1f"},
        ["first_seen_at", "last_seen_at"],
    ),
    (
        "CompanyRecord",
        "name_leading_bom_kept",
        {**REQUIRED_RECORD, "name": "﻿Northwind"},
        ["first_seen_at", "last_seen_at"],
    ),
    (
        "CompanyRecord",
        "name_only_bom_is_not_blank",
        {**REQUIRED_RECORD, "name": "﻿"},
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
    (
        "CompanyRecord",
        "unknown_extraction_method",
        {**REQUIRED_RECORD, "extraction_method": "regex"},
    ),
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
                {
                    "case": case,
                    "input": item.model_dump(mode="json"),
                    "document": item.to_document(),
                }
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
