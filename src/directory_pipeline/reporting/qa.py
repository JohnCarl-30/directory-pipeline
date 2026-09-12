"""Pandas QA over a batch of extracted records.

A pipeline that reports "12,000 records indexed" and nothing else is lying by
omission. These checks answer the question that actually matters before a
reindex: *did quality change, and where?*

Everything here is intentionally cheap and columnar -- the same code runs over a
50-record demo batch or a Parquet file of ten million, with `to_parquet` instead
of `to_csv` and a chunked read at the front.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from ..domain.models import EnrichedCompany

# Field-level expectations. Falling below `threshold` is a release blocker, not
# a curiosity -- these are the fields downstream search and filtering depend on.
COVERAGE_EXPECTATIONS: dict[str, float] = {
    "name": 1.00,
    "city": 0.85,
    "region": 0.80,
    "postal_code": 0.70,
    "phone_e164": 0.70,
    "website": 0.75,
    "categories": 0.60,
    "description": 0.60,
}


def to_frame(companies: list[EnrichedCompany]) -> pd.DataFrame:
    """Flatten to a columnar view. One row per record, including duplicates."""
    rows: list[dict[str, Any]] = []
    for item in companies:
        c = item.company
        e = item.enrichment
        rows.append(
            {
                "record_id": c.record_id,
                "source": c.source,
                "source_id": c.source_id,
                "name": c.name,
                "name_normalized": c.name_normalized,
                "city": c.address.city,
                "region": c.address.region,
                "postal_code": c.address.postal_code,
                "country": c.address.country,
                "phone_e164": c.contact.phone_e164,
                "phone_raw": c.contact.phone_raw,
                "email": c.contact.email,
                "website": c.contact.website,
                "categories": ", ".join(c.categories) if c.categories else None,
                "description": c.description,
                "employee_count": e.employee_count if e and e.employee_count else c.employee_count,
                "founded_year": c.founded_year,
                "extraction_method": c.extraction_method.value,
                "extraction_confidence": c.extraction_confidence,
                "industry": e.industry if e else None,
                "revenue_usd": e.revenue_usd if e else None,
                "enriched": e is not None,
                "enrichment_cache_hit": bool(e.cache_hit) if e else False,
                "cluster_id": item.cluster_id,
                "duplicate_of": item.duplicate_of,
                "is_canonical": item.duplicate_of is None,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame

    # Downcast the low-cardinality columns. On a 10M-row frame this is the
    # difference between 8GB and 1GB of RAM.
    for column in ("source", "region", "country", "extraction_method", "industry"):
        if column in frame:
            frame[column] = frame[column].astype("category")
    return frame


def coverage(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-field fill rate against expectations."""
    if frame.empty:
        return pd.DataFrame(columns=["field", "filled", "total", "coverage", "expected", "pass"])

    total = len(frame)
    rows = []
    for field, expected in COVERAGE_EXPECTATIONS.items():
        if field not in frame:
            continue
        filled = int(frame[field].notna().sum())
        rate = filled / total if total else 0.0
        rows.append(
            {
                "field": field,
                "filled": filled,
                "total": total,
                "coverage": round(rate, 4),
                "expected": expected,
                "pass": rate >= expected,
            }
        )
    return pd.DataFrame(rows).sort_values("coverage")


def validity(frame: pd.DataFrame) -> pd.DataFrame:
    """Format checks on the normalized fields. Filled is not the same as valid."""
    if frame.empty:
        return pd.DataFrame(columns=["check", "violations", "sample"])

    checks: list[dict[str, Any]] = []

    def record(name: str, mask: pd.Series, column: str) -> None:
        bad = frame.loc[mask, column].dropna()
        checks.append(
            {
                "check": name,
                "violations": int(len(bad)),
                "sample": ", ".join(bad.astype(str).head(3)),
            }
        )

    phones = frame["phone_e164"]
    record(
        "phone not E.164",
        phones.notna() & ~phones.fillna("").str.match(r"^\+\d{8,15}$"),
        "phone_e164",
    )

    sites = frame["website"]
    record(
        "website not https origin",
        sites.notna() & ~sites.fillna("").str.startswith("https://"),
        "website",
    )

    emails = frame["email"]
    record(
        "email malformed",
        emails.notna() & ~emails.fillna("").str.contains(r"^[^@\s]+@[^@\s]+\.\w{2,}$", regex=True),
        "email",
    )

    zips = frame["postal_code"]
    record(
        "postal_code not 5 digits",
        zips.notna() & ~zips.fillna("").str.match(r"^\d{5}$"),
        "postal_code",
    )

    regions = frame["region"].astype("string")
    record(
        "region not 2-letter code",
        regions.notna() & ~regions.fillna("").str.match(r"^[A-Z]{2}$"),
        "region",
    )

    years = frame["founded_year"]
    record(
        "founded_year implausible",
        years.notna() & ((years < 1600) | (years > 2026)),
        "founded_year",
    )

    return pd.DataFrame(checks)


def duplicate_summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Clusters with more than one member, and what collapsed into what."""
    if frame.empty or "cluster_id" not in frame:
        return pd.DataFrame(columns=["cluster_id", "members", "canonical", "collapsed"])

    grouped = frame.groupby("cluster_id", observed=True)
    rows = []
    for cluster_id, group in grouped:
        if len(group) < 2:
            continue
        canonical = group.loc[group["is_canonical"], "name"]
        rows.append(
            {
                "cluster_id": cluster_id,
                "members": len(group),
                "canonical": canonical.iloc[0] if len(canonical) else "(none)",
                "collapsed": " | ".join(group.loc[~group["is_canonical"], "name"].tolist()),
            }
        )
    columns = ["cluster_id", "members", "canonical", "collapsed"]
    if not rows:
        # No clusters with >1 member. Return the right *shape*, not an empty
        # frame with no columns -- callers sort and index on these names.
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows).sort_values("members", ascending=False)


def summary(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"records": 0}

    coverage_frame = coverage(frame)
    validity_frame = validity(frame)
    failing = coverage_frame.loc[~coverage_frame["pass"], "field"].tolist()

    return {
        "records": int(len(frame)),
        "canonical_records": int(frame["is_canonical"].sum()),
        "duplicates_collapsed": int((~frame["is_canonical"]).sum()),
        "clusters": int(frame["cluster_id"].nunique()),
        "enriched": int(frame["enriched"].sum()),
        "enrichment_rate": round(float(frame["enriched"].mean()), 4),
        "cache_hit_rate": round(float(frame["enrichment_cache_hit"].mean()), 4),
        "mean_extraction_confidence": round(float(frame["extraction_confidence"].mean()), 4),
        "extraction_methods": frame["extraction_method"].value_counts().to_dict(),
        "total_validity_violations": int(validity_frame["violations"].sum()),
        "coverage_failures": failing,
        "quality_gate": "PASS"
        if not failing and validity_frame["violations"].sum() == 0
        else "FAIL",
    }


def write_reports(frame: pd.DataFrame, out_dir: str | Path = "reports") -> dict[str, str]:
    """Write CSV (inspection) + Parquet (the version you actually query later)."""
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    for name, data in (
        ("records", frame),
        ("coverage", coverage(frame)),
        ("validity", validity(frame)),
        ("duplicates", duplicate_summary(frame)),
    ):
        if data.empty:
            continue
        csv_path = path / f"{name}.csv"
        data.to_csv(csv_path, index=False)
        written[name] = str(csv_path)

    if not frame.empty:
        try:
            parquet_path = path / "records.parquet"
            frame.to_parquet(parquet_path, index=False)
            written["records_parquet"] = str(parquet_path)
        except Exception:
            pass  # pyarrow not installed; CSV is enough for the demo
    return written
