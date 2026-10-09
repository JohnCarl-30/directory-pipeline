#!/usr/bin/env python3
"""Entity resolution at scale: does the blocking actually hold up?

`resolution/entity.py` opens by saying that 100k companies is 5 billion
comparisons and that blocking is what makes the problem tractable. Everything
measured in this repo until now ran on 12 records, where that claim is
untestable: 12 records is 66 comparisons, and brute force would have been
fine.

This generates a corpus with **known** answers and measures four things the
demo cannot:

  1. how many comparisons blocking actually avoids, against n(n-1)/2;
  2. how many true duplicate pairs it loses in exchange -- blocking recall is
     the ceiling on everything downstream, because a pair that never shares a
     key is never scored;
  3. what the `len(bucket) > 200` guard costs, which is the one place the
     implementation knowingly throws comparisons away;
  4. whether the scorer still separates duplicates from the two hard negatives
     at volume.

The corpus has four populations, because a benchmark made only of easy
positives measures the generator:

  singletons     one record, no duplicate anywhere
  duplicates     2-3 records of one company, perturbed the way real directory
                 listings differ: legal-suffix swaps, word order, typos, a
                 missing website, a missing phone
  branches       same company, same corporate domain, genuinely different city
                 -- a chain, not a duplicate. Must NOT merge, and the
                 `location_conflict` weight exists to hold them in review.
  near misses    different companies, similar names, same city, different
                 domain and phone -- the realistic hard negative. "Harbor
                 Point Labs" and "Harbor Point Legal" on one street.

Every number below is reproducible: one seeded RNG, and the real
`blocking_keys`, `score_pair`, `generate_candidates` and `resolve` from the
package. Nothing here reimplements the thing it measures.

    python scripts/bench_resolution.py                  # 100k records
    python scripts/bench_resolution.py --records 10000
    python scripts/bench_resolution.py --markdown       # docs-ready tables
"""

from __future__ import annotations

import argparse
import random
import resource
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from directory_pipeline.domain.models import (  # noqa: E402
    Address,
    CompanyRecord,
    Contact,
)
from directory_pipeline.extraction.normalize import (  # noqa: E402
    normalize_company_name,
    normalize_phone,
)
from directory_pipeline.resolution.entity import (  # noqa: E402
    MATCH_THRESHOLD,
    REVIEW_THRESHOLD,
    blocking_keys,
    generate_candidates,
    resolve,
)

# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------

# 60 x 32 x 52 = 99,840 distinct base names, which covers a 100k-record corpus
# without the generator falling back to serial numbers. Deliberately no shared
# leading qualifier ("North", "United"): `nm:{first_token}:{locality}` is one of
# the blocking keys, so a word list where most names begin with the same handful
# of tokens measures the generator's taste rather than the blocking's selectivity.
HEADS = [
    "Harbor",
    "Summit",
    "Cascade",
    "Meridian",
    "Northwind",
    "Atlas",
    "Vantage",
    "Quarry",
    "Brightline",
    "Silverpine",
    "Ironwood",
    "Redstone",
    "Clearwater",
    "Lakeshore",
    "Foxglove",
    "Greystone",
    "Highmark",
    "Juniper",
    "Keystone",
    "Lantern",
    "Marlin",
    "Nightingale",
    "Orchard",
    "Pinnacle",
    "Quicksilver",
    "Ravenwood",
    "Stonebridge",
    "Thornton",
    "Umbra",
    "Vermillion",
    "Westgate",
    "Yarrow",
    "Zephyr",
    "Alder",
    "Birchwood",
    "Copperfield",
    "Dovetail",
    "Eastbrook",
    "Fairhaven",
    "Glenmoor",
    "Hollybrook",
    "Inglewood",
    "Jasper",
    "Kingsley",
    "Larkspur",
    "Maplewood",
    "Norcross",
    "Oakhurst",
    "Pemberton",
    "Quillon",
    "Rosewood",
    "Saltmarsh",
    "Tanglewood",
    "Underhill",
    "Verdant",
    "Whitfield",
    "Xavier",
    "Yorkshire",
    "Ashgrove",
    "Blackwood",
]
TAILS = [
    "Point",
    "Ridge",
    "Field",
    "Harbor",
    "Grove",
    "Creek",
    "Hollow",
    "Vale",
    "Crossing",
    "Landing",
    "Terrace",
    "Gate",
    "Mill",
    "Forge",
    "Row",
    "Park",
    "Bay",
    "Bluff",
    "Cove",
    "Reach",
    "Basin",
    "Chase",
    "Dell",
    "Edge",
    "Ferry",
    "Glade",
    "Heath",
    "Isle",
    "Junction",
    "Knoll",
    "Loop",
    "Meadow",
]
KINDS = [
    "Labs",
    "Analytics",
    "Systems",
    "Logistics",
    "Partners",
    "Capital",
    "Robotics",
    "Health",
    "Foods",
    "Energy",
    "Freight",
    "Advisors",
    "Technologies",
    "Industrial",
    "Diagnostics",
    "Networks",
    "Materials",
    "Interactive",
    "Studios",
    "Instruments",
    "Aerospace",
    "Biosciences",
    "Chemical",
    "Dynamics",
    "Electric",
    "Fabrication",
    "Geomatics",
    "Hydraulics",
    "Imaging",
    "Joinery",
    "Kinetics",
    "Laminates",
    "Metrology",
    "Nutrition",
    "Optics",
    "Packaging",
    "Quarries",
    "Refining",
    "Surfaces",
    "Textiles",
    "Utilities",
    "Ventures",
    "Welding",
    "Composites",
    "Granite",
    "Marine",
    "Outfitters",
    "Precision",
    "Timber",
    "Vinyl",
    "Waterworks",
    "Automation",
]
SUFFIXES = ["Inc.", "LLC", "Corp.", "Ltd.", "Co.", "Group", "Holdings", ""]

CITIES = [
    ("Austin", "TX", "787"),
    ("Boston", "MA", "021"),
    ("Seattle", "WA", "981"),
    ("Portland", "OR", "972"),
    ("Denver", "CO", "802"),
    ("Atlanta", "GA", "303"),
    ("Chicago", "IL", "606"),
    ("Columbus", "OH", "432"),
    ("Phoenix", "AZ", "850"),
    ("Nashville", "TN", "372"),
    ("Raleigh", "NC", "276"),
    ("San Diego", "CA", "921"),
    ("Minneapolis", "MN", "554"),
    ("Kansas City", "MO", "641"),
    ("Tampa", "FL", "336"),
    ("Pittsburgh", "PA", "152"),
    ("Salt Lake City", "UT", "841"),
    ("Omaha", "NE", "681"),
    ("Richmond", "VA", "232"),
    ("Hartford", "CT", "061"),
]

# "Legal" against "Labs", "Partnership" against "Partners": the pairs a human
# reading two listings has to think about.
NEAR_MISS_KINDS = [
    ("Labs", "Legal"),
    ("Analytics", "Analytical"),
    ("Systems", "Systems Group"),
    ("Partners", "Partnership"),
    ("Capital", "Capital Advisors"),
    ("Health", "Healthcare"),
    ("Foods", "Food Group"),
    ("Freight", "Freightways"),
]


@dataclass
class Corpus:
    records: list[CompanyRecord] = field(default_factory=list)
    # record_id -> the entity it really belongs to. Two records share a label
    # only when they are genuinely the same company.
    truth: dict[str, str] = field(default_factory=dict)
    # Pairs the resolver is expected to merge.
    duplicate_pairs: set[tuple[str, str]] = field(default_factory=set)
    # Pairs it must not merge, kept apart so the two failure modes can be
    # reported separately -- they fail for different reasons and have
    # different fixes.
    branch_pairs: set[tuple[str, str]] = field(default_factory=set)
    near_miss_pairs: set[tuple[str, str]] = field(default_factory=set)


class Generator:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self._used_names: set[str] = set()
        self._serial = 0

    def _next_id(self) -> str:
        self._serial += 1
        return f"bench-{self._serial:07d}"

    def _base_name(self) -> str:
        """A name no other base company has.

        Distinctness matters: two unrelated companies that happen to generate
        the same name would be a generator artifact scored as a false positive,
        and the hard negatives are supposed to be the deliberate ones.
        """
        for _ in range(50):
            name = f"{self.rng.choice(HEADS)} {self.rng.choice(TAILS)} {self.rng.choice(KINDS)}"
            if name not in self._used_names:
                self._used_names.add(name)
                return name
        # Exhausted the word lists; disambiguate rather than collide.
        name = f"{self.rng.choice(HEADS)} {self.rng.choice(TAILS)} {self._serial}"
        self._used_names.add(name)
        return name

    def _record(
        self,
        *,
        name: str,
        city: tuple[str, str, str],
        domain: str | None,
        phone: str | None,
        email: str | None,
        postal: str,
    ) -> CompanyRecord:
        source_id = self._next_id()
        return CompanyRecord(
            record_id=CompanyRecord.make_record_id("bench", source_id),
            source="bench",
            source_id=source_id,
            source_url=f"https://bench.test/company/{source_id}",
            name=name,
            name_normalized=normalize_company_name(name),
            address=Address(
                line1=f"{self.rng.randint(1, 9999)} Main St",
                city=city[0],
                region=city[1],
                postal_code=postal,
            ),
            contact=Contact(
                phone_e164=phone,
                email=email,
                website=f"https://{domain}" if domain else None,
            ),
            employee_count=self.rng.choice([None, 12, 48, 120, 460, 1200]),
            founded_year=self.rng.randint(1950, 2020),
        )

    def _domain_for(self, name: str) -> str:
        slug = "".join(c for c in name.lower() if c.isalnum())[:28]
        return f"{slug}.com"

    def _phone(self) -> str:
        area, exchange = self.rng.randint(200, 989), self.rng.randint(200, 999)
        raw = f"({area}) {exchange}-{self.rng.randint(1000, 9999)}"
        return normalize_phone(raw) or raw

    def _postal(self, city: tuple[str, str, str]) -> str:
        return f"{city[2]}{self.rng.randint(10, 99)}"

    # -- perturbations, i.e. the ways one company's two listings differ ----

    def _perturb(self, name: str) -> str:
        choice = self.rng.random()
        words = name.split()
        if choice < 0.30:  # legal suffix added or swapped
            return f"{name} {self.rng.choice(SUFFIXES)}".strip()
        if choice < 0.50 and len(words) >= 3:  # word order
            words[0], words[1] = words[1], words[0]
            return " ".join(words)
        if choice < 0.75:  # typo: transpose two adjacent characters
            idx = self.rng.randrange(1, max(2, len(name) - 2))
            chars = list(name)
            chars[idx], chars[idx + 1] = chars[idx + 1], chars[idx]
            return "".join(chars)
        if choice < 0.90:  # abbreviate the trailing word
            return " ".join(words[:-1] + [words[-1][:4] + "."])
        return name  # byte-identical listing on two directories

    def entity(self, kind: str, corpus: Corpus) -> None:
        name = self._base_name()
        city = self.rng.choice(CITIES)
        domain = self._domain_for(name)
        phone = self._phone()
        email = f"hello@{domain}"
        postal = self._postal(city)
        label = f"ent-{self._serial}-{kind}"

        primary = self._record(
            name=name, city=city, domain=domain, phone=phone, email=email, postal=postal
        )
        corpus.records.append(primary)
        corpus.truth[primary.record_id] = label

        if kind == "singleton":
            return

        if kind == "duplicate":
            ids = [primary.record_id]
            for _ in range(self.rng.choice([1, 1, 2])):
                # One signal goes missing per variant, which is the normal
                # case: directories carry different subsets of the same facts.
                drop = self.rng.random()
                variant = self._record(
                    name=self._perturb(name),
                    city=city,
                    domain=None if drop < 0.25 else domain,
                    phone=None if 0.25 <= drop < 0.45 else phone,
                    email=None if drop < 0.60 else email,
                    postal=postal if self.rng.random() < 0.8 else self._postal(city),
                )
                corpus.records.append(variant)
                corpus.truth[variant.record_id] = label
                ids.append(variant.record_id)
            for a, b in combinations(sorted(ids), 2):
                corpus.duplicate_pairs.add((a, b))
            return

        if kind == "branch":
            # Same corporate website, different city. Every location of a chain
            # looks like this, and merging them loses every branch but one.
            other = self.rng.choice([c for c in CITIES if c[0] != city[0]])
            branch = self._record(
                name=name,
                city=other,
                domain=domain,
                phone=self._phone(),
                email=email,
                postal=self._postal(other),
            )
            corpus.records.append(branch)
            corpus.truth[branch.record_id] = f"{label}-branch"
            corpus.branch_pairs.add(
                tuple(sorted((primary.record_id, branch.record_id)))  # type: ignore[arg-type]
            )
            return

        if kind == "near_miss":
            # A different company that reads like this one.
            head_tail = " ".join(name.split()[:2])
            a_kind, b_kind = self.rng.choice(NEAR_MISS_KINDS)
            twin_name = f"{head_tail} {b_kind}"
            if twin_name in self._used_names:
                twin_name = f"{head_tail} {b_kind} {self._serial}"
            self._used_names.add(twin_name)
            twin_domain = self._domain_for(twin_name)
            twin = self._record(
                name=twin_name,
                city=city,
                domain=twin_domain,
                phone=self._phone(),
                email=f"hello@{twin_domain}",
                postal=postal,
            )
            corpus.records.append(twin)
            corpus.truth[twin.record_id] = f"{label}-twin"
            corpus.near_miss_pairs.add(
                tuple(sorted((primary.record_id, twin.record_id)))  # type: ignore[arg-type]
            )


def build_corpus(target: int, seed: int) -> Corpus:
    """Grow until `target` records exist. Mix is fixed so runs compare."""
    rng = random.Random(seed)
    gen = Generator(rng)
    corpus = Corpus()
    # 70% of entities are singletons, which is roughly what a real directory
    # looks like -- duplicates are the interesting minority, not the bulk.
    weights = [("singleton", 0.70), ("duplicate", 0.18), ("branch", 0.07), ("near_miss", 0.05)]
    kinds = [k for k, _ in weights]
    probs = [p for _, p in weights]
    while len(corpus.records) < target:
        gen.entity(rng.choices(kinds, probs)[0], corpus)
    return corpus


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------


def peak_rss_mb() -> float:
    """ru_maxrss is bytes on macOS and kilobytes on Linux."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


@dataclass
class Timing:
    label: str
    seconds: float


def measure_blocking(records: list[CompanyRecord]) -> tuple[dict[str, list[str]], float]:
    start = time.perf_counter()
    buckets: dict[str, list[str]] = defaultdict(list)
    for record in records:
        for key in blocking_keys(record):
            buckets[key].append(record.record_id)
    return buckets, time.perf_counter() - start


def pairs_from_buckets(
    buckets: dict[str, list[str]], cap: int
) -> tuple[set[tuple[str, str]], set[tuple[str, str]], int]:
    """Pairs blocking offers, pairs the size cap discards, and bucket count.

    Mirrors `generate_candidates`' traversal exactly, including the cap, so
    "what the guard costs" is measured against what the implementation does
    rather than against what it could do.
    """
    offered: set[tuple[str, str]] = set()
    discarded: set[tuple[str, str]] = set()
    oversized = 0
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        if len(bucket) > cap:
            oversized += 1
            for a, b in combinations(sorted(bucket), 2):
                discarded.add((a, b))
            continue
        for a, b in combinations(sorted(bucket), 2):
            offered.add((a, b))
    # A pair reachable through any in-cap bucket is not lost, even if some
    # other oversized bucket also held it.
    return offered, discarded - offered, oversized


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=20261009)
    parser.add_argument("--bucket-cap", type=int, default=200, help="match entity.py's guard")
    parser.add_argument("--markdown", action="store_true", help="emit docs-ready tables")
    args = parser.parse_args(argv)

    timings: list[Timing] = []

    t0 = time.perf_counter()
    corpus = build_corpus(args.records, args.seed)
    timings.append(Timing("generate corpus", time.perf_counter() - t0))
    records = corpus.records
    n = len(records)

    buckets, block_s = measure_blocking(records)
    timings.append(Timing("blocking keys", block_s))

    offered, lost_to_cap, oversized = pairs_from_buckets(buckets, args.bucket_cap)

    t0 = time.perf_counter()
    candidates = generate_candidates(records)
    timings.append(Timing("generate_candidates", time.perf_counter() - t0))

    # Candidates are passed in, which is what every caller now does. Without
    # it `resolve` rescores the whole set -- the measurement that motivated
    # the parameter, and worth keeping reproducible: drop `candidates=` here
    # and this stage goes from milliseconds back to the cost of the stage above.
    t0 = time.perf_counter()
    cluster_of, canonical_of = resolve(records, candidates=candidates)
    timings.append(Timing("resolve (clusters)", time.perf_counter() - t0))

    brute = n * (n - 1) // 2
    matched_pairs = {
        tuple(sorted((c.left.record_id, c.right.record_id))) for c in candidates if c.is_match
    }
    review_pairs = {
        tuple(sorted((c.left.record_id, c.right.record_id))) for c in candidates if c.needs_review
    }

    dup = corpus.duplicate_pairs
    blocking_recall = len(dup & offered) / len(dup) if dup else 0.0
    dup_lost_to_cap = len(dup & lost_to_cap)
    found = len(dup & matched_pairs)
    recall = found / len(dup) if dup else 0.0
    precision = found / len(matched_pairs) if matched_pairs else 0.0

    branch_merged = len(corpus.branch_pairs & matched_pairs)
    branch_review = len(corpus.branch_pairs & review_pairs)
    twin_merged = len(corpus.near_miss_pairs & matched_pairs)
    twin_review = len(corpus.near_miss_pairs & review_pairs)

    true_entities = len(set(corpus.truth.values()))
    found_clusters = len(set(cluster_of.values()))
    canonical = sum(1 for rid, can in canonical_of.items() if rid == can)

    scored = len(offered)
    total_s = sum(t.seconds for t in timings)

    def row(label: str, value: str) -> str:
        return f"| {label} | {value} |" if args.markdown else f"  {label:<34}{value}"

    def head(title: str, cols: tuple[str, str]) -> None:
        if args.markdown:
            print(f"\n**{title}**\n\n| {cols[0]} | {cols[1]} |\n|---|---|")
        else:
            print(f"\n\033[1m{title}\033[0m\n" + "-" * len(title))

    head("Corpus", ("", ""))
    print(row("records", f"{n:,}"))
    print(row("true entities", f"{true_entities:,}"))
    print(row("duplicate pairs seeded", f"{len(dup):,}"))
    print(row("branch pairs (must not merge)", f"{len(corpus.branch_pairs):,}"))
    print(row("near-miss pairs (must not merge)", f"{len(corpus.near_miss_pairs):,}"))

    head("Blocking", ("", ""))
    print(row("brute force n(n-1)/2", f"{brute:,}"))
    print(row("pairs offered by blocking", f"{scored:,}"))
    print(row("comparisons avoided", f"{(1 - scored / brute) * 100:.4f}%"))
    print(row("reduction factor", f"{brute / max(scored, 1):,.0f}x"))
    print(row("buckets", f"{len(buckets):,}"))
    print(row(f"buckets over cap ({args.bucket_cap})", f"{oversized:,}"))
    print(row("pairs discarded by the cap", f"{len(lost_to_cap):,}"))
    print(row("blocking recall (ceiling)", f"{blocking_recall * 100:.2f}%"))
    print(row("  of which lost to the cap", f"{dup_lost_to_cap:,}"))

    head("Scoring", ("", ""))
    print(row(f"pairs at or above review ({REVIEW_THRESHOLD})", f"{len(candidates):,}"))
    print(row(f"pairs at or above match ({MATCH_THRESHOLD})", f"{len(matched_pairs):,}"))
    print(row("duplicate pairs recovered", f"{found:,} / {len(dup):,}"))
    print(row("recall", f"{recall * 100:.2f}%"))
    print(row("precision", f"{precision * 100:.2f}%"))

    head("Hard negatives", ("", ""))
    print(row("branches wrongly merged", f"{branch_merged:,} / {len(corpus.branch_pairs):,}"))
    print(row("branches held in review", f"{branch_review:,}"))
    print(row("near misses wrongly merged", f"{twin_merged:,} / {len(corpus.near_miss_pairs):,}"))
    print(row("near misses held in review", f"{twin_review:,}"))

    head("Clustering", ("", ""))
    print(row("clusters produced", f"{found_clusters:,}"))
    print(row("canonical records", f"{canonical:,}"))
    print(row("records folded as duplicates", f"{n - canonical:,}"))

    head("Cost", ("", ""))
    for timing in timings:
        print(row(timing.label, f"{timing.seconds:.2f}s"))
    print(row("total", f"{total_s:.2f}s"))
    print(row("records/sec (blocking)", f"{n / max(block_s, 1e-9):,.0f}"))
    print(row("pairs/sec (scoring)", f"{scored / max(total_s, 1e-9):,.0f}"))
    print(row("peak RSS", f"{peak_rss_mb():,.0f} MB"))

    # A benchmark that cannot fail is a demo. These are the two claims the
    # module makes that would be wrong rather than merely worse than hoped.
    problems = []
    if scored >= brute:
        problems.append("blocking offered no reduction")
    if dup and blocking_recall < 0.5:
        problems.append(f"blocking recall {blocking_recall:.1%} -- most duplicates never compared")
    if problems:
        print("\nFAILED: " + "; ".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
