#!/usr/bin/env python3
"""End-to-end pipeline run with no Docker, no Temporal, no credentials.

Starts the two mock services in-process, then runs the same code the Temporal
activities call -- crawl, extract, enrich, resolve, QA -- and indexes into
OpenSearch if one happens to be reachable.

The point is that every component is exercised by its real implementation.
Nothing here is a stub standing in for the production path; only the
*orchestration* is replaced, and that is exactly the layer Temporal owns.

    python scripts/run_local.py
    python scripts/run_local.py --categories software logistics --max-pages 3
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from directory_pipeline.config import Settings  # noqa: E402
from directory_pipeline.domain.models import EnrichedCompany  # noqa: E402
from directory_pipeline.enrichment.provider import EnrichmentProvider  # noqa: E402
from directory_pipeline.extraction.agent import ExtractionError, Extractor  # noqa: E402
from directory_pipeline.fixtures.mock_directory import app as directory_app  # noqa: E402
from directory_pipeline.fixtures.mock_enrichment import app as enrichment_app  # noqa: E402
from directory_pipeline.observability import METRICS, configure_logging  # noqa: E402
from directory_pipeline.reporting import qa  # noqa: E402
from directory_pipeline.resolution.adjudicator import Adjudicator  # noqa: E402
from directory_pipeline.resolution.entity import generate_candidates, resolve  # noqa: E402
from directory_pipeline.scraping.crawler import DirectoryCrawler  # noqa: E402
from directory_pipeline.search.index import SearchIndex  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class BackgroundServer:
    """Runs a uvicorn app on a daemon thread for the life of the script."""

    def __init__(self, app, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        self.server = uvicorn.Server(config)
        self.port = port
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{self.port}/healthz", timeout=0.5)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError(f"server on port {self.port} did not become healthy")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def banner(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m\n" + "-" * len(text))


async def run(args: argparse.Namespace) -> int:
    configure_logging(level="WARNING", json_output=False)

    directory_port, enrichment_port = free_port(), free_port()
    directory = BackgroundServer(directory_app, directory_port)
    enrichment = BackgroundServer(enrichment_app, enrichment_port)
    directory.start()
    enrichment.start()
    print(f"mock directory  -> http://127.0.0.1:{directory_port}")
    print(f"mock enrichment -> http://127.0.0.1:{enrichment_port}")

    settings = Settings(
        directory_base_url=f"http://127.0.0.1:{directory_port}",
        enrichment_base_url=f"http://127.0.0.1:{enrichment_port}",
        crawl_rps=args.rps,
        crawl_burst=int(args.rps * 2),
        opensearch_url=args.opensearch_url,
    )

    crawler = DirectoryCrawler(settings)
    extractor = Extractor(settings)
    enricher = EnrichmentProvider(settings)
    adjudicator = Adjudicator(settings)

    try:
        # 1. discover -------------------------------------------------------
        banner("1. Discover")
        urls: list[str] = []
        for category in args.categories:
            found = await crawler.discover(category, args.max_pages)
            print(f"  {category:<14} {len(found)} listings")
            urls.extend(found)
        urls = list(dict.fromkeys(urls))
        print(f"  total unique listings: {len(urls)}")

        # 2. fetch + extract ------------------------------------------------
        banner("2. Fetch and extract")
        mode = "LLM-assisted" if settings.llm_extraction_enabled else "DOM only (no API key set)"
        print(f"  extraction mode: {mode}")
        records = []
        async for listing in crawler.fetch_details(urls, "demo-directory"):
            try:
                record = await extractor.extract(listing)
            except ExtractionError as exc:
                print(f"  skipped {listing.url}: {exc}")
                continue
            records.append(record)
            print(
                f"  {record.name:<34} "
                f"conf={record.extraction_confidence:.2f} "
                f"via={record.extraction_method.value:<12} "
                f"{record.address.city or '-'}, {record.address.region or '-'}"
            )

        if not records:
            print("no records extracted")
            return 1

        # 3. enrich ---------------------------------------------------------
        banner("3. Enrich")
        enrichments = await enricher.enrich_many(records)
        companies = [
            EnrichedCompany(company=r, enrichment=e)
            for r, e in zip(records, enrichments, strict=True)
        ]
        hits = sum(1 for e in enrichments if e)
        print(f"  enriched {hits}/{len(records)} records")

        async with httpx.AsyncClient(timeout=5) as probe:
            stats = (await probe.get(f"http://127.0.0.1:{enrichment_port}/v1/_stats")).json()
        print(
            f"  billable upstream calls: {stats['total_billable_calls']} "
            f"(cache + single-flight collapsed the rest)"
        )

        # 4. resolve --------------------------------------------------------
        banner("4. Resolve duplicates")
        candidates = generate_candidates(records)
        borderline = [c for c in candidates if c.needs_review]
        for candidate in sorted(candidates, key=lambda c: -c.score):
            verdict = "MATCH " if candidate.is_match else "review"
            print(
                f"  {verdict} {candidate.score:.2f}  "
                f"{candidate.left.name[:28]:<28} <-> {candidate.right.name[:28]}"
            )

        accepted = []
        if borderline and adjudicator.enabled:
            print(f"  adjudicating {len(borderline)} borderline pair(s) with the model...")
            accepted = await adjudicator.adjudicate_many(borderline)
            print(f"  model confirmed {len(accepted)} as the same company")
        elif borderline:
            print(f"  {len(borderline)} borderline pair(s) left unmerged (no ANTHROPIC_API_KEY)")

        cluster_of, canonical_of = resolve(records, accepted=accepted)
        companies = [
            c.model_copy(
                update={
                    "cluster_id": cluster_of.get(c.company.record_id),
                    "duplicate_of": (
                        None
                        if canonical_of.get(c.company.record_id) == c.company.record_id
                        else canonical_of.get(c.company.record_id)
                    ),
                }
            )
            for c in companies
        ]
        collapsed = sum(1 for c in companies if c.duplicate_of)
        print(
            f"  {len(records)} records -> {len(set(cluster_of.values()))} clusters "
            f"({collapsed} marked duplicate)"
        )

        # 5. QA -------------------------------------------------------------
        banner("5. QA report (pandas)")
        frame = qa.to_frame(companies)
        print(qa.coverage(frame).to_string(index=False))
        violations = qa.validity(frame)
        if int(violations["violations"].sum()):
            print("\n" + violations[violations["violations"] > 0].to_string(index=False))
        else:
            print("\n  no validity violations")

        duplicates = qa.duplicate_summary(frame)
        if not duplicates.empty:
            print("\n" + duplicates.to_string(index=False))

        written = qa.write_reports(frame, args.reports_dir)
        summary = qa.summary(frame)
        print(f"\n  quality gate: {summary['quality_gate']}")
        for path in written.values():
            print(f"  wrote {path}")

        # 6. index ----------------------------------------------------------
        banner("6. Index into OpenSearch")
        index = SearchIndex(settings)
        if await index.ping():
            await index.bootstrap()
            count = await index.index_documents(companies)
            await index.client.indices.refresh(index=settings.opensearch_alias)
            print(f"  indexed {count} documents into alias '{settings.opensearch_alias}'")
            print(f"  {await index.stats()}")
        else:
            print(f"  OpenSearch not reachable at {settings.opensearch_url} - skipped.")
            print("  Start it with: docker compose up -d opensearch")
        await index.aclose()

        banner("Metrics")
        snapshot = METRICS.snapshot()
        for counter in snapshot["counters"]:
            if counter["value"]:
                labels = " ".join(f"{k}={v}" for k, v in counter["labels"].items())
                print(f"  {counter['name']:<32} {counter['value']:>8.0f}  {labels}")
        for name, timing in snapshot["timings"].items():
            print(
                f"  {name:<32} n={timing['count']:<4} p50={timing['p50'] * 1000:.0f}ms "
                f"p95={timing['p95'] * 1000:.0f}ms"
            )
        return 0

    finally:
        await crawler.aclose()
        await enricher.aclose()
        directory.stop()
        enrichment.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--categories",
        nargs="+",
        default=["software", "logistics", "healthcare", "finance", "manufacturing", "energy"],
    )
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--rps", type=float, default=20.0)
    parser.add_argument("--opensearch-url", default="http://localhost:9200")
    parser.add_argument("--reports-dir", default="reports")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(run(args))
    return 130


if __name__ == "__main__":
    raise SystemExit(main())
